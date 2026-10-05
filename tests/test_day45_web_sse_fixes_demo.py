"""
Day45 全量 bug 审查第 3 批修复的配套测试（Web 与 SSE）

对应 5 个被修复的问题（每条都有"回退即变红"的变异验证）:
    问题1 (P1/P2) SSE 并发名额可被 5 个 HEAD 请求永久占满
        → 引入幂等的 _SseSlotLease 凭证，三条路径归还（生成器 finally /
          call_on_close / HEAD 即时归还）
    问题2 (P2) SSE 单流存活上界在"事件持续到达"时永不判定
        → 存活判定提到主循环每轮统一执行，与事件到达与否无关
    问题3 (P2) trigger 筛选值类型不校验，静默降级为全量回归
        → module/priority/tags 类型与枚举不合即 400
    问题4 (P1) 用例写操作不失效报告缓存，三类统计返回错数据
        → 统一 _invalidate_case_related_caches 同时清两个前缀 + CLI 路径补失效
    问题5 (P2) 日志注入：request.path 与筛选值未单行化
        → 抽取 src/web/utils.sanitize_log_field，两处调用

测试链路铁律:
    - 零 skip / 零 xfail / 不断言 True
    - 占位值一律 PLACEHOLDER_*，不构造形似真密钥的串
    - SSE 用例全部走 Flask test_client，不起真实服务
    - 时序用轮询/上限，不靠固定 sleep 赌时序
"""

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.db_session import DatabaseSession  # noqa: E402
from src.web.routes import executions as ex  # noqa: E402
from src.web.utils import MAX_LOG_FIELD_LENGTH, sanitize_log_field  # noqa: E402

PLACEHOLDER_CRED = "PLACEHOLDER-NOT-A-REAL-CRED"


@pytest.fixture
def exec_client(tmp_path, monkeypatch):
    """
    执行 API 测试客户端（临时 SQLite 库 + 2 条种子用例 + 真实触发一次）

    返回:
        tuple[FlaskClient, str]: (测试客户端, 已完成的批次号)
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day45_web3.db"))
    monkeypatch.setenv("TM_REDIS_ENABLED", "false")
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    DatabaseSession.reset()
    DatabaseSession.init_db()

    from src.core.case_manager import CaseManager
    from src.web import create_app

    # description 里带"标签: xxx"是本项目 tags 筛选的唯一载体
    # （test_cases 表无 tags 列，_build_description 把标签序列化进描述），
    # 且 create_case 直存 description、不走 _build_description，故手工写入
    CaseManager.create_case(
        {"case_id": "TM-W3-0001", "name": "用户登录", "module": "用户中心",
         "priority": "P0", "case_type": "api", "description": "标签: smoke"}
    )
    CaseManager.create_case(
        {"case_id": "TM-W3-0002", "name": "订单创建", "module": "订单中心",
         "priority": "P2", "case_type": "api", "description": "标签: chip"}
    )
    client = create_app("test").test_client()
    resp = client.post("/api/executions/trigger", json={"executor": "simulated"})
    execution_id = resp.get_json()["data"]["execution_id"]
    # 轮询至终态（禁固定 sleep 赌时序）
    for _ in range(200):
        status = client.get(f"/api/executions/{execution_id}/status").get_json()
        if status["data"]["status"] in ("finished", "failed"):
            break
        time.sleep(0.05)
    yield client, execution_id
    DatabaseSession.reset()


# ===========================================================================
# 问题1: HEAD 请求占名额不释放
# ===========================================================================


def test_问题1_HEAD请求后名额被释放(exec_client):
    """5 个 HEAD 曾把某 IP 名额永久占死，之后 GET 一律 429"""
    client, eid = exec_client
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    for _ in range(ex.SSE_MAX_CONNECTIONS_PER_IP):
        client.head(f"/api/executions/{eid}/events")

    resp = client.get(f"/api/executions/{eid}/events")
    resp.get_data()

    assert resp.status_code != 429, (
        f"连续 {ex.SSE_MAX_CONNECTIONS_PER_IP} 次 HEAD 后名额未释放，GET 被 429"
    )
    assert not ex._SSE_ACTIVE_CONNECTIONS, (
        f"请求结束后仍残留名额: {dict(ex._SSE_ACTIVE_CONNECTIONS)}"
    )


def test_问题1_正常GET请求后名额被释放(exec_client):
    """正常消费路径的名额释放不受影响（不回归）"""
    client, eid = exec_client
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    resp = client.get(f"/api/executions/{eid}/events")
    resp.get_data()

    assert resp.status_code == 200
    assert not ex._SSE_ACTIVE_CONNECTIONS, "正常 GET 后名额未释放"


def test_问题1_未消费完的流仍占用名额(exec_client):
    """
    名额必须覆盖流的真实存活期，而不是响应一返回就归还

    这是本条修复最容易被"改坏"的方向：任何挂在请求收尾钩子
    （after_request / teardown_request）上的归还逻辑，都在响应体
    开始被消费**之前**就执行了，配额会退化成摆设——无凭证客户端
    可无限开流占满工作线程，正是 Day44 P2-08 要堵的口子。

    只开一条流：两条并发的未消费流在 Flask 2.3 + Werkzeug 3.1 下
    会在第二次 get_data() 时撞上 RequestContext.pop 的
    "Popped wrong request context" 断言，那是框架自身的缺陷，
    与本项目无关，不在断言范围内。
    """
    client, eid = exec_client
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    stream = client.get(f"/api/executions/{eid}/events")

    assert ex._SSE_ACTIVE_CONNECTIONS.get("127.0.0.1") == 1, (
        f"流尚未消费完，名额却未持有: {dict(ex._SSE_ACTIVE_CONNECTIONS)}"
    )
    stream.get_data()
    assert not ex._SSE_ACTIVE_CONNECTIONS, "流消费完后名额未归还"


def test_问题1_名额归还幂等(exec_client):
    """
    同一凭证重复归还是空操作，不得把**别的连接**的名额一起扣掉

    判据设计：字典归零即删键，所以拿"键是否还在"当幂等判据是抓不住的
    ——过扣成 -1 也会走同一条 pop 分支。这里造 2 个并发名额，只归还
    其中一个凭证两次：正确实现停在 1，漏掉幂等闸门会一路扣到 0 并删键。
    """
    client, eid = exec_client
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    client.get(f"/api/executions/{eid}/events").get_data()
    assert ex._SSE_ACTIVE_CONNECTIONS == {}, (
        f"归还出现重复扣减: {dict(ex._SSE_ACTIVE_CONNECTIONS)}"
    )

    with ex._SSE_CONNECTIONS_LOCK:
        ex._SSE_ACTIVE_CONNECTIONS["9.9.9.9"] = 2  # 两条并发流
    lease = ex._SseSlotLease("9.9.9.9")
    lease.release()
    lease.release()

    assert ex._SSE_ACTIVE_CONNECTIONS.get("9.9.9.9") == 1, (
        f"释放不幂等，把另一条流的名额也扣了: {dict(ex._SSE_ACTIVE_CONNECTIONS)}"
    )
    ex._SSE_ACTIVE_CONNECTIONS.clear()


def test_问题1_被429拒绝的请求不占用名额(exec_client):
    """超限请求在占用前就 abort，不得留下名额或扣掉别人的额度"""
    client, eid = exec_client
    ex._SSE_ACTIVE_CONNECTIONS.clear()
    with ex._SSE_CONNECTIONS_LOCK:
        ex._SSE_ACTIVE_CONNECTIONS["127.0.0.1"] = ex.SSE_MAX_CONNECTIONS_PER_IP

    resp = client.get(f"/api/executions/{eid}/events")

    assert resp.status_code == 429
    assert ex._SSE_ACTIVE_CONNECTIONS["127.0.0.1"] == ex.SSE_MAX_CONNECTIONS_PER_IP, (
        f"429 路径动了别人的名额: {dict(ex._SSE_ACTIVE_CONNECTIONS)}"
    )
    ex._SSE_ACTIVE_CONNECTIONS.clear()


# ===========================================================================
# 问题2: SSE 存活上界每轮都判
# ===========================================================================


def test_问题2_事件持续到达时存活上界仍生效(exec_client, monkeypatch):
    """
    批次持续发事件（tick 不触发）时，存活上界必须仍然判定

    修复前该判定只写在心跳分支内，事件连续到达时永远走不到，
    1800s 护闸形同虚设。这里把上界调到 0.3s 并持续发事件验证。

    判据是"**流是否及时收流**"而不仅仅是"body 里有没有那句提示"：
    发布器跑满 5s 预算才停，一旦事件分支的判定被摘掉，流会一直活到
    发布器停手、再由心跳分支兜底才收——body 照样含那句话，只有量出
    收流耗时才区分得开。0.3s 与 5s 之间取 2s 阈值，余量充足不赌时序。
    """
    client, eid = exec_client
    monkeypatch.setattr(ex, "SSE_MAX_LIFETIME_SECONDS", 0.3)
    # 夹具里的批次已跑完，会走"通道存在但批次已终态"分支直接收流，
    # 根本进不了实时订阅分支。把状态打桩成 running 以命中分支三。
    monkeypatch.setattr(
        ex.CaseManager,
        "get_execution_status",
        lambda _eid: {
            "execution_id": _eid, "status": "running", "total_cases": 2,
            "passed": 0, "failed": 0, "error": 0, "skipped": 0,
            "pass_rate": 0.0, "start_time": None, "end_time": None,
            "duration": 0.0,
        },
    )
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    from src.core.event_bus import ExecutionEvent, get_channel

    channel = get_channel(eid, create=True)
    stop = threading.Event()

    def publisher():
        # 持续发非终态事件（case_finished 不在 TERMINAL_EVENT_TYPES 里，
        # 不会让流提前收尾），间隔 0.05s 远小于心跳间隔，因此 subscribe
        # 的 tick 几乎不触发——正是修复前判定不到存活上界的场景
        deadline = time.monotonic() + 5.0
        i = 0
        while not stop.is_set() and time.monotonic() < deadline:
            channel.publish(
                ExecutionEvent(
                    event_type="case_finished",
                    data={"case_id": f"TM-W3-{i:04d}", "result": "passed"},
                )
            )
            i += 1
            time.sleep(0.05)
        stop.set()

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        body = client.get(
            f"/api/executions/{eid}/events",
            buffered=False,
        ).get_data(as_text=True)
    finally:
        stop.set()
        thread.join(timeout=3)
    elapsed = time.monotonic() - started

    assert "stream-lifetime-exceeded" in body, (
        "事件持续到达时存活上界未触发（仍只在心跳分支判定）"
    )
    assert elapsed < 2.0, (
        f"事件持续到达时存活上界没兜住，流拖了 {elapsed:.1f}s 才收流"
        f"（上界 0.3s，实际拖到发布器停手）"
    )


def test_问题2_未超限时流正常继续(exec_client, monkeypatch):
    """上界足够大时不误收流（不回归）"""
    client, eid = exec_client
    monkeypatch.setattr(ex, "SSE_MAX_LIFETIME_SECONDS", 3600.0)
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    body = client.get(f"/api/executions/{eid}/events").get_data(as_text=True)

    assert "stream-lifetime-exceeded" not in body
    assert "batch_finished" in body or "batch_start" in body


def test_问题2_空闲流走心跳分支同样受上界约束(exec_client, monkeypatch):
    """
    一个事件都不来的空闲流，护闸只能靠心跳分支兜住

    这是与上一条互补的另一条触发路径：事件分支的判定被摘掉时，
    空闲流（本用例的场景）仍应由心跳分支收流，两条路径都得在位。
    """
    client, eid = exec_client
    monkeypatch.setattr(ex, "SSE_MAX_LIFETIME_SECONDS", 0.3)
    monkeypatch.setattr(ex, "HEARTBEAT_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        ex.CaseManager,
        "get_execution_status",
        lambda _eid: {
            "execution_id": _eid, "status": "running", "total_cases": 2,
            "passed": 0, "failed": 0, "error": 0, "skipped": 0,
            "pass_rate": 0.0, "start_time": None, "end_time": None,
            "duration": 0.0,
        },
    )
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    from src.core.event_bus import get_channel

    get_channel(eid, create=True)  # 只建通道，不发布任何事件
    body = client.get(f"/api/executions/{eid}/events").get_data(as_text=True)

    assert "stream-lifetime-exceeded" in body, (
        "空闲流在心跳分支上不受存活上界约束（护闸只在有事件时生效）"
    )


# ===========================================================================
# 问题3: trigger 筛选值类型/枚举校验
# ===========================================================================


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("module", {"a": 1}),
        ("priority", {"a": 1}),
        ("tags", {"a": 1}),
        ("module", 123),
        ("priority", "P9"),
        ("priority", ["P0", "P9"]),
        ("module", [{"a": 1}]),
        ("tags", ["smoke", 7]),
    ],
)
def test_问题3_非法筛选值返回400(exec_client, field, value):
    """
    非法类型/枚举必须 400，而不是静默忽略后跑全量

    注意 `tags="smoke"` 这种**裸字符串不在此列**：核心层
    `_normalize_values` 明确接受 str 单值，把它判为非法属于凭空
    收紧合法输入的行为。
    """
    client, _ = exec_client

    resp = client.post("/api/executions/trigger", json={field: value})

    assert resp.status_code == 400, (
        f"{field}={value!r} 未被拒绝（实际 {resp.status_code}）——"
        f"筛选条件被静默忽略会执行全量回归"
    )
    assert field in resp.get_json()["message"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("module", "用户中心"),
        ("module", ["用户中心", "订单中心"]),
        ("priority", "P0"),
        ("priority", ["P0", "P2"]),
        ("tags", ["smoke", "chip"]),
    ],
)
def test_问题3_合法筛选值正常受理(exec_client, field, value):
    """合法输入行为不变（不回归）"""
    client, _ = exec_client

    resp = client.post("/api/executions/trigger", json={field: value})

    assert resp.status_code == 202, f"{field}={value!r} 合法却被拒: {resp.get_json()}"


def test_问题3_空筛选条件正常全量受理(exec_client):
    """不传任何筛选 = 全量回归，仍 202（不回归）"""
    client, _ = exec_client

    resp = client.post("/api/executions/trigger", json={})

    assert resp.status_code == 202
    assert resp.get_json()["data"]["total_cases"] == 2


def test_问题3_优先级大小写不敏感但原值透传(exec_client):
    """查询侧由核心层 upper() 归一，路由层只校验不改动合法输入"""
    client, _ = exec_client

    resp = client.post("/api/executions/trigger", json={"priority": "p0"})

    assert resp.status_code == 202, "小写优先级应视为合法（核心层查询侧会归一）"


# ===========================================================================
# 问题4: 用例写操作失效报告缓存
# ===========================================================================


@pytest.fixture
def spy_cache(monkeypatch):
    """把两个失效方法换成计数器，用于验证"是否被调用"而非真实 Redis"""
    calls = {"cases": 0, "reports": 0}
    monkeypatch.setattr(
        ex.__dict__.get("cache_client", None) or _cache_client(),
        "invalidate_cases_list",
        lambda: calls.__setitem__("cases", calls["cases"] + 1),
    )
    monkeypatch.setattr(
        _cache_client(),
        "invalidate_reports",
        lambda: calls.__setitem__("reports", calls["reports"] + 1),
    )
    return calls


def _cache_client():
    from src.core.cache import cache_client

    return cache_client


def test_问题4_update_case后报告缓存被失效(exec_client, spy_cache):
    """改 module/priority/status 会改变报告聚合口径，必须一并失效报告缓存"""
    client, _ = exec_client
    spy_cache["reports"] = 0

    client.put("/api/cases/TM-W3-0001", json={"module": "新模块"})

    assert spy_cache["reports"] == 1, "update_case 未失效报告缓存"


def test_问题4_delete_case后报告缓存被失效(exec_client, spy_cache):
    """物理删除后用例数下降，质量度量分母随之变化"""
    client, _ = exec_client
    spy_cache["reports"] = 0

    client.delete("/api/cases/TM-W3-0002")

    assert spy_cache["reports"] == 1, "delete_case 未失效报告缓存"


def test_问题4_create_case后报告缓存被失效(exec_client, spy_cache):
    """新建用例同样改变 active 分母"""
    client, _ = exec_client
    spy_cache["reports"] = 0

    client.post(
        "/api/cases/",
        json={"case_id": "TM-W3-0003", "name": "新增", "module": "新模块"},
    )

    assert spy_cache["reports"] == 1, "create_case 未失效报告缓存"


def test_问题4_两个前缀都被失效(exec_client, spy_cache):
    """统一失效函数必须同时清 cases_list 与 reports 两个前缀"""
    client, _ = exec_client
    spy_cache["cases"] = 0
    spy_cache["reports"] = 0

    client.put("/api/cases/TM-W3-0001", json={"priority": "P3"})

    assert spy_cache["cases"] == 1, "用例列表缓存未失效"
    assert spy_cache["reports"] == 1, "报告缓存未失效"


def test_问题4_失效后报告统计返回最新数据(exec_client):
    """端到端：改 module 后立刻查模块分布，应看到新口径而非旧缓存"""
    client, _ = exec_client
    # 先查一次产生缓存
    before = client.get("/api/reports/module-distribution").get_json()["data"]
    assert before, "初始模块分布应有数据"

    client.put("/api/cases/TM-W3-0001", json={"module": "迁移后模块"})

    after = client.get("/api/reports/module-distribution").get_json()["data"]
    modules = {item["module"] for item in after}
    assert "迁移后模块" in modules, (
        f"改 module 后模块分布仍是旧口径: {modules}"
    )


def test_问题4_缓存故障不阻断用例写操作(exec_client, monkeypatch):
    """缓存是旁路能力：失效抛异常不得让用例创建/更新返回 500"""
    client, _ = exec_client

    def boom():
        raise RuntimeError("模拟缓存层故障")

    monkeypatch.setattr(_cache_client(), "invalidate_cases_list", boom)
    monkeypatch.setattr(_cache_client(), "invalidate_reports", boom)

    resp = client.post(
        "/api/cases/",
        json={"case_id": "TM-W3-BOOM", "name": "缓存故障下仍应成功"},
    )

    assert resp.status_code == 201, "缓存故障反噬了用例创建"


# ===========================================================================
# 问题5: 日志注入
# ===========================================================================


def _seed_case_with_module(module: str, case_id: str) -> None:
    """
    造一条 module 恰为给定值的用例

    受理埋点在 `start_execution` **成功之后**才落日志（core 筛选用例
    命中不到即抛 NoCasesSelectedError → 400，压根走不到埋点）。而 module
    筛选用的是 `TestCase.module.in_(...)` 精确匹配，所以要验证"夹带
    换行的筛选值会不会伪造日志行"，必须先有一条 module 就等于该畸形值
    的用例，请求才会被受理、埋点才会执行。
    """
    from src.core.case_manager import CaseManager

    CaseManager.create_case(
        {
            "case_id": case_id,
            "name": "夹带控制字符的模块",
            "module": module,
            "priority": "P2",
            "case_type": "api",
        }
    )


def _capture_trigger_logs(client, payload) -> list[str]:
    """触发一次受理并抓取期间产生的全部日志行（纯文本，不含格式套件）"""
    from src.common.logger import LogManager

    logger = LogManager.get_logger()
    captured: list[str] = []
    sink_id = logger.add(
        lambda message: captured.append(str(message)), level="INFO", format="{message}"
    )
    try:
        client.post("/api/executions/trigger", json=payload)
    finally:
        logger.remove(sink_id)
    return captured


def test_问题5_含换行的筛选值被单行化(exec_client):
    """
    受理埋点不得因筛选值夹带换行而多出一行伪造记录

    断言要点：只看"包含伪造文案的行数"是**测不出注入**的——未单行化时
    换行后的那一行照样包含该文案，计数仍为 1。真正能区分的是"有没有一行
    以伪造的日志级别标记开头"，那才是凭空多出来的审计记录。
    """
    client, _ = exec_client
    forged_value = "用户中心\n[INFO] 伪造的批次已受理 | evil"
    _seed_case_with_module(forged_value, "TM-W3-NL")

    captured = _capture_trigger_logs(client, {"module": forged_value})

    joined = "\n".join(captured)
    assert "批次已受理" in joined, "受理埋点未执行，断言无意义"
    standalone = [line for line in joined.splitlines() if line.startswith("[INFO]")]
    assert not standalone, f"日志被注入伪造记录: {standalone}"
    assert len([ln for ln in captured if "伪造的批次已受理" in ln]) == 1


def test_问题5_超长筛选值被截断(exec_client):
    """超长值必须截断，避免单条日志撑爆磁盘"""
    client, _ = exec_client
    long_value = "x" * 500
    _seed_case_with_module(long_value, "TM-W3-LONG")

    captured = _capture_trigger_logs(client, {"module": long_value})

    assert any("已截断" in line for line in captured), "超长筛选值未被截断"


def test_问题5_含换行的请求路径被单行化(exec_client):
    """
    request.path 按 PEP 3333 已被 URL 解码，%0d%0a 会变成真实换行

    用**真实攻击形态**（URL 里的 %0d%0a）而不是往 environ_base 里手塞
    控制字符：后者绕过了 WSGI 层的解码路径，测的不是生产上真实发生的
    那件事（且 environ_base 会整块替换 base environ，入口日志压根收不到
    这条请求，断言是空的）。
    """
    client, _ = exec_client
    from src.common.logger import LogManager

    logger = LogManager.get_logger()
    lines: list[str] = []
    sink_id = logger.add(
        lambda message: lines.append(str(message)), level="INFO", format="{message}"
    )
    try:
        client.get("/api/cases/a%0d%0a%5BINFO%5D%20forged")
    finally:
        logger.remove(sink_id)

    joined = "\n".join(lines)
    assert "forged" in joined, "入口日志未记录该请求，断言无意义"
    standalone = [line for line in joined.splitlines() if line.startswith("[INFO]")]
    assert not standalone, f"请求路径换行未单行化，日志被注入伪造行: {standalone}"


def test_sanitize_log_field_换行与控制字符折叠():
    """公共原语自身契约：控制字符全部折叠为空格"""
    assert sanitize_log_field("a\nb") == "a b"
    assert sanitize_log_field("a\r\nb") == "a  b"
    assert sanitize_log_field("a\x00\x1bb") == "a  b"
    assert "\n" not in sanitize_log_field("x" * 10 + "\n" + "y" * 10)


def test_sanitize_log_field_超长截断且正常值不变():
    """超长截断并标记；正常值原样返回"""
    long_value = "x" * (MAX_LOG_FIELD_LENGTH + 50)
    truncated = sanitize_log_field(long_value)
    assert len(truncated) < len(long_value)
    assert truncated.endswith("...[已截断]")

    assert sanitize_log_field("用户中心") == "用户中心"
    assert sanitize_log_field(123) == "123"
