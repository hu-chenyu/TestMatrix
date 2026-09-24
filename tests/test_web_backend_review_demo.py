"""
TestMatrix Day34: Web后端+Redis阶段缓冲消化补测
（边界值与异常路径回归，覆盖此前未专门断言的接口行为）

测试覆盖（8条，四组）:
    A组 用例列表边界（2条）:
        1. test_cases_list_all_filters_combined
           七维筛选同时传（module/priority/case_type/status/
           keyword/page/page_size），断言分页结构与命中数
        2. test_cases_list_invalid_page_returns_400
           page=0与page=-1均返回400（对齐路由实际校验，不500）
    B组 执行查询边界（3条）:
        3. test_execution_status_nonexistent_returns_404
           不存在批次的status接口返回404
        4. test_execution_detail_nonexistent_returns_404
           不存在批次的detail接口返回404
        5. test_sse_events_endpoint_returns_stream
           已完成批次/events响应Content-Type含text/event-stream，
           且有限帧序列以终态帧收尾（DB重建分支可自行收敛）
    C组 报告参数边界（1条）:
        6. test_reports_failed_top_invalid_limit_returns_400
           limit=abc返回400（对齐路由try/except ValueError）
    D组 默认关闭链路（2条）:
        7. test_queue_disabled_trigger_uses_thread
           TM_TASK_QUEUE_ENABLED缺省关闭时POST /trigger走裸线程，
           202受理后轮询至finished，4条2过2挂
        8. test_cache_disabled_list_still_works
           TM_REDIS_ENABLED缺省关闭时列表接口直连DB正常返回

测试铁律:
    - 全程临时SQLite文件库 + 4条active种子（奇偶各半），零外部依赖
    - 缓存/队列缺省关闭（delenv兜底），不构造任何真实Redis连接
    - 后台批次一律走/status API轮询至终态，禁止固定sleep
    - 全程loguru，无print
"""

import time
from pathlib import Path
from typing import Iterator, Optional

import allure
import pytest
from flask.testing import FlaskClient

from src.core import event_bus
from src.core.cache import cache_client
from src.core.task_queue import stop_worker, task_queue_client
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 轮询预算: 模拟执行约0.01s/条，20次×0.3s远超实际耗时防flaky
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL = 0.3

# SSE流读取帧数上限: 已完成批次重建序列为6帧（1开始+4用例+1终态），
# 100帧上限是挂死兜底而非正常值
MAX_STREAM_FRAMES = 100


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布（SimulatedExecutor: 末位奇failed偶passed）:
        - 用户中心: TM-UC-0001（奇→failed）、TM-UC-0002（偶→passed）
        - 订单中心: TM-OD-0001（奇→failed）、TM-OD-0002（偶→passed）

    参数:
        无

    返回:
        无
    """
    seeds = [
        ("TM-UC-0001", "用户登录成功校验", "用户中心"),
        ("TM-UC-0002", "用户登录密码错误校验", "用户中心"),
        ("TM-OD-0001", "订单创建成功校验", "订单中心"),
        ("TM-OD-0002", "订单状态流转校验", "订单中心"),
    ]
    with DatabaseSession.session_scope() as session:
        for case_id, name, module in seeds:
            session.add(
                models.TestCase(
                    case_id=case_id,
                    name=name,
                    module=module,
                    priority="P1",
                    case_type="api",
                    status="active",
                    description="阶段回顾补测种子用例",
                )
            )


@pytest.fixture
def review_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    默认关闭场景fixture（function级，临时SQLite库 + 缓存/队列全关）

    环境准备:
        - 临时SQLite文件库 + init_db建表 + 4条active种子用例
        - 显式delenv缓存/队列/worker开关与fake URL，保证缺省false
          （本文件验证的是"默认关闭时链路行为逐字节不变"）
        - 缓存/队列单例reset_backend丢弃上一例残留后端
        - create_app("test")取Flask测试客户端

    teardown（顺序很重要）:
        - stop_worker兜底（默认开关下本就无线程，调用幂等）
        - 队列/缓存后端reset、事件通道注册表reset
        - DatabaseSession.reset防Windows文件锁后再unlink库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_path = tmp_path / "web_backend_review.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    # 缓存与队列缺省必须关闭: 显式删除防止本机环境变量污染断言
    monkeypatch.delenv("TM_REDIS_ENABLED", raising=False)
    monkeypatch.delenv("TM_REDIS_URL", raising=False)
    monkeypatch.delenv("TM_TASK_QUEUE_ENABLED", raising=False)
    monkeypatch.delenv("TM_TASK_WORKER_ENABLED", raising=False)
    task_queue_client.reset_backend()
    cache_client.reset_backend()

    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()

    client = create_app("test").test_client()
    yield client

    # teardown: worker兜底停止 → 后端/引擎/通道复位 → 删库文件
    stop_worker()
    task_queue_client.reset_backend()
    cache_client.reset_backend()
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_path.unlink(missing_ok=True)


def _wait_batch_terminal(
    client: FlaskClient, execution_id: str
) -> dict:
    """
    走真实HTTP轮询批次状态至终态（finished/failed）

    参数:
        client (FlaskClient): Flask测试客户端
        execution_id (str): 执行批次号

    返回:
        dict: 终态批次状态字典

    异常:
        AssertionError: 轮询期间状态码非200
        pytest.fail: 轮询预算内未达终态
    """
    status_data: Optional[dict] = None
    for _ in range(POLL_MAX_ATTEMPTS):
        response = client.get(f"/api/executions/{execution_id}/status")
        assert response.status_code == 200, "轮询期间状态查询应始终200"
        status_data = response.get_json()["data"]
        if status_data["status"] in ("finished", "failed"):
            return status_data
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"批次在轮询预算内未达终态: {execution_id}, "
        f"最后状态: {status_data['status'] if status_data else '无响应'}"
    )
    raise AssertionError("不可达: pytest.fail后代码不会执行到此处")


def _trigger_full_batch(client: FlaskClient) -> str:
    """
    全量触发执行（不带请求体即全量回归），返回批次号

    参数:
        client (FlaskClient): Flask测试客户端

    返回:
        str: 执行批次号

    异常:
        AssertionError: 触发未返回202或受理响应字段缺失时失败
    """
    response = client.post("/api/executions/trigger")
    assert response.status_code == 202, "队列缺省关闭时trigger仍应202受理"
    body = response.get_json()
    assert body["code"] == 202
    assert body["message"] == "执行批次已受理，后台执行中"
    assert body["data"]["status"] == "pending"
    assert body["data"]["total_cases"] == 4
    return body["data"]["execution_id"]


# ===========================================================================
# A组: 用例列表边界
# ===========================================================================
@allure.feature("Web后端阶段回顾补测")
@allure.story("用例列表七维筛选与分页边界")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestCasesListEdgeCases:
    """七维组合筛选与非法分页参数"""

    def test_cases_list_all_filters_combined(
        self, review_env: FlaskClient
    ) -> None:
        """
        七维筛选同时传: module=用户中心 + priority=P1 + case_type=api
        + status=active + keyword=登录 + page=1 + page_size=10，
        断言返回结构含total/total_pages/items且命中2条用户中心用例
        """
        response = review_env.get(
            "/api/cases/",
            query_string={
                "module": "用户中心",
                "priority": "P1",
                "case_type": "api",
                "status": "active",
                "keyword": "登录",
                "page": 1,
                "page_size": 10,
            },
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        # 分页结构五字段缺一不可
        assert set(data.keys()) == {
            "items", "total", "page", "page_size", "total_pages"
        }
        assert data["page"] == 1
        assert data["page_size"] == 10
        # 两条用户中心用例名称均含"登录"，订单中心不含该关键字
        assert data["total"] == 2
        assert data["total_pages"] == 1
        assert len(data["items"]) == 2
        assert {item["case_id"] for item in data["items"]} == {
            "TM-UC-0001", "TM-UC-0002"
        }

    def test_cases_list_invalid_page_returns_400(
        self, review_env: FlaskClient
    ) -> None:
        """
        page=0与page=-1均被路由层范围校验拦截返回400，
        统一错误格式且不允许穿透成500
        """
        for invalid_page in (0, -1):
            response = review_env.get(
                "/api/cases/", query_string={"page": invalid_page}
            )
            assert response.status_code == 400, (
                f"page={invalid_page}应返回400而非{response.status_code}"
            )
            body = response.get_json()
            assert body["code"] == 400
            assert "page" in body["message"]


# ===========================================================================
# B组: 执行查询边界与SSE
# ===========================================================================
@allure.feature("Web后端阶段回顾补测")
@allure.story("执行批次查询异常路径与SSE响应头")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestExecutionQueryEdgeCases:
    """不存在批次404与SSE流式响应头"""

    def test_execution_status_nonexistent_returns_404(
        self, review_env: FlaskClient
    ) -> None:
        """不存在批次号查status: 核心层返回None，路由转404统一格式"""
        response = review_env.get(
            "/api/executions/RUN-NOT-EXIST-0001/status"
        )
        assert response.status_code == 404
        body = response.get_json()
        assert body["code"] == 404
        assert body["data"]["execution_id"] == "RUN-NOT-EXIST-0001"

    def test_execution_detail_nonexistent_returns_404(
        self, review_env: FlaskClient
    ) -> None:
        """不存在批次号查detail: 无汇总记录统一404（与未finish同语义）"""
        response = review_env.get("/api/executions/RUN-NOT-EXIST-0002")
        assert response.status_code == 404
        body = response.get_json()
        assert body["code"] == 404
        assert body["data"]["execution_id"] == "RUN-NOT-EXIST-0002"

    def test_sse_events_endpoint_returns_stream(
        self, review_env: FlaskClient
    ) -> None:
        """
        已完成批次GET /events: Content-Type含text/event-stream；
        终态后通道已清理走DB重建分支，帧序列有限且以batch_finished
        终态帧收尾（流式响应可自行收敛，不挂死）
        """
        execution_id = _trigger_full_batch(review_env)
        terminal = _wait_batch_terminal(review_env, execution_id)
        assert terminal["status"] == "finished"

        response = review_env.get(
            f"/api/executions/{execution_id}/events", buffered=False
        )
        # 流式响应头在生成器迭代前即已确定
        assert "text/event-stream" in response.content_type

        # 限量排空流: 已完成批次为有限重建序列，必在帧数上限内收完
        frame_count = 0
        saw_terminal = False
        try:
            for chunk in response.iter_encoded():
                text = chunk.decode("utf-8")
                frame_count += 1
                if "event: batch_finished" in text:
                    saw_terminal = True
                if saw_terminal or frame_count >= MAX_STREAM_FRAMES:
                    break
        finally:
            response.close()
        assert saw_terminal, "已完成批次事件流应以batch_finished终态帧收尾"
        assert frame_count <= MAX_STREAM_FRAMES


# ===========================================================================
# C组: 报告参数边界
# ===========================================================================
@allure.feature("Web后端阶段回顾补测")
@allure.story("报告统计参数异常路径")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestReportsEdgeCases:
    """failed-top非法limit参数"""

    def test_reports_failed_top_invalid_limit_returns_400(
        self, review_env: FlaskClient
    ) -> None:
        """
        limit=abc无法转int: 路由_parse_int_param捕获ValueError
        抛ValidationError返回400（核心层ValueError转换为双保险）
        """
        response = review_env.get(
            "/api/reports/failed-top", query_string={"limit": "abc"}
        )
        assert response.status_code == 400
        body = response.get_json()
        assert body["code"] == 400
        assert "limit" in body["message"]


# ===========================================================================
# D组: 缓存/队列默认关闭链路
# ===========================================================================
@allure.feature("Web后端阶段回顾补测")
@allure.story("默认关闭时裸线程与直连DB链路")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestDefaultDisabledChain:
    """队列关闭走裸线程、缓存关闭直连DB，默认链路零行为漂移"""

    def test_queue_disabled_trigger_uses_thread(
        self, review_env: FlaskClient
    ) -> None:
        """
        TM_TASK_QUEUE_ENABLED缺省false: trigger 202受理后fallback
        裸线程执行，轮询至finished，详情4条明细2过2挂（奇偶规则）
        """
        execution_id = _trigger_full_batch(review_env)
        terminal = _wait_batch_terminal(review_env, execution_id)
        assert terminal["status"] == "finished"
        assert terminal["total_cases"] == 4
        assert terminal["passed"] == 2
        assert terminal["failed"] == 2

        # 详情走GET /api/executions/<execution_id>（summary+items）
        response = review_env.get(f"/api/executions/{execution_id}")
        assert response.status_code == 200
        items = response.get_json()["data"]["items"]
        assert len(items) == 4
        passed_count = sum(1 for item in items if item["result"] == "passed")
        failed_count = sum(1 for item in items if item["result"] == "failed")
        assert passed_count == 2
        assert failed_count == 2

    def test_cache_disabled_list_still_works(
        self, review_env: FlaskClient
    ) -> None:
        """
        TM_REDIS_ENABLED缺省false: get/set全no-op，列表接口
        直连DB分页查询正常返回200且数据与种子一致（4条active）
        """
        assert cache_client.enabled is False
        response = review_env.get(
            "/api/cases/", query_string={"page": 1, "page_size": 10}
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["total"] == 4
        assert len(data["items"]) == 4
        assert data["total_pages"] == 1
