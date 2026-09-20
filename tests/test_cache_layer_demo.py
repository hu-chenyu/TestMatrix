"""
TestMatrix Day31: Redis缓存层测试
（CacheClient封装正确性 + 路由缓存命中 + 失效策略与故障降级）

测试覆盖（10条，三组）:
    A组 CacheClient单测（5条，直接测src.core.cache）:
        1. test_disabled_passthrough
           TM_REDIS_ENABLED未设（默认关闭）时全部方法no-op
        2. test_set_get_roundtrip
           fake后端读写往返，中文/嵌套结构原样还原
        3. test_get_miss_returns_none
           未写入的key返回None，不抛异常
        4. test_invalidate_cases_list
           前缀失效只删tm:cases:list:*，其他前缀不受影响
        5. test_fake_backend_independent_per_test
           reset_backend重建后端，前后两个fake实例数据隔离
    B组 路由层缓存命中（3条，走Flask test client）:
        6. test_cases_list_cache_hit
           同参数二次请求命中缓存不回源；换参数新key回源
        7. test_reports_summary_cache_hit
           批次终态后两次summary仅回源一次
        8. test_different_limits_separate_keys
           trend不同limit各自一key、各回源一次
    C组 失效策略与降级（2条）:
        9. test_case_write_invalidates_list
           创建用例后列表缓存被主动失效，同参数重新回源
        10. test_redis_down_degrades_gracefully
            后端命令全部抛ConnectionError时接口仍200数据正确

测试铁律:
    - 全程只用fake://内存后端（fakeredis），绝不依赖本机真实
      Redis进程；function级fixture每条用例reset_backend重建实例
    - spy一律patch到真实调用点（CaseManager/ReportRepository
      类属性，路由运行时在类对象上查找方法，patch类即生效）
    - 触发执行后轮询走GET /api/executions/<id>/status至终态，
      禁止固定sleep赌时序；conftest autouse fixture已全局禁用
      真实通知渠道，执行链路零真实邮件/零网络
    - 全程loguru日志，无print
"""

import time
from pathlib import Path
from typing import Iterator, Optional

import allure
import pytest
import redis
from flask.testing import FlaskClient
from unittest.mock import patch

from src.core import event_bus
from src.core.cache import CacheClient, cache_client
from src.core.case_manager import CaseManager
from src.core.report_analyzer import ReportRepository
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 轮询参数: 批次模拟执行约0.01s/条，预算远超实际耗时防flaky
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL = 0.3


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布:
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
                    description="缓存层测试种子用例",
                )
            )


@pytest.fixture
def cache_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    缓存启用场景fixture（function级，fake内存后端+临时SQLite库）

    环境准备:
        - TM_REDIS_ENABLED=true 开启缓存
        - TM_REDIS_URL=fake://0 触发fakeredis内存实例（零真实Redis）
        - reset_backend()丢弃历史后端，保证本用例拿到全新fake实例
        - 临时SQLite文件库+init_db+4条种子active用例

    teardown:
        - reset_backend()再清一次（环境变量由monkeypatch自动还原，
          后端实例需手动丢弃，避免fake数据串到后续用例）
        - DatabaseSession.reset()防Windows文件锁
        - event_bus.reset_channels()兜底清理执行批次残留通道
        - 临时库文件unlink

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量/属性补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_path = tmp_path / "cache_layer.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    # 开启缓存并指定fake协议；先复位单例后端，确保按新环境重建
    monkeypatch.setenv("TM_REDIS_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    cache_client.reset_backend()

    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()

    # teardown: 后端→引擎→事件通道→库文件，顺序与e2e口径一致
    cache_client.reset_backend()
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_path.unlink(missing_ok=True)


def _wait_batch_terminal(
    client: FlaskClient, execution_id: str
) -> dict:
    """
    轮询批次状态至终态finished/failed（内部方法，走真实HTTP）

    参数:
        client (FlaskClient): Flask测试客户端
        execution_id (str): 执行批次号

    返回:
        dict: 终态状态字典（含status与冗余计数）

    异常:
        AssertionError: 轮询预算内未达终态时pytest.fail
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


def _trigger_and_wait(client: FlaskClient) -> dict:
    """
    全量触发执行并轮询至批次终态（内部方法）

    参数:
        client (FlaskClient): Flask测试客户端

    返回:
        dict: 终态状态字典

    异常:
        AssertionError: 触发未返回202或批次最终非finished时失败
    """
    trigger_response = client.post("/api/executions/trigger")
    assert trigger_response.status_code == 202, "触发应返回202受理"
    execution_id = trigger_response.get_json()["data"]["execution_id"]
    final_status = _wait_batch_terminal(client, execution_id)
    assert final_status["status"] == "finished", "种子批次应正常finished"
    return final_status


# ===========================================================================
# A组: CacheClient单测（直接测src.core.cache，不走HTTP）
# ===========================================================================
@allure.feature("Redis缓存层")
@allure.story("CacheClient单元行为")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCacheClientUnit:
    """缓存客户端封装层单测（开关/往返/未命中/前缀失效/实例隔离）"""

    def test_disabled_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        默认关闭全no-op: TM_REDIS_ENABLED未设时enabled为False，
        set_json后get_json仍返回None（不构建任何后端）
        """
        # 显式删除开关，模拟默认/生产未配置场景
        monkeypatch.delenv("TM_REDIS_ENABLED", raising=False)
        client = CacheClient()

        assert client.enabled is False, "开关未设置时应默认关闭"
        assert client._get_backend() is None, "关闭时不应构建后端"

        client.set_json("tm:cases:list:disabled", {"items": []})
        assert client.get_json("tm:cases:list:disabled") is None, (
            "关闭时写入应为no-op，读取恒为None"
        )
        # 失效操作也应静默no-op不抛异常
        client.invalidate_cases_list()
        client.invalidate_reports()

    def test_set_get_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        fake后端读写往返: dict（含中文键值/嵌套字典/列表/数值）
        set后get原样还原，ensure_ascii=False保证中文不转义
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = CacheClient()

        payload = {
            "模块": "用户中心",
            "分页": {"page": 1, "page_size": 20, "total": 4},
            "标签": ["P1", "api", "中文标签"],
            "通过率": 0.5,
            "空值": None,
        }
        client.set_json("tm:test:roundtrip", payload)
        assert client.get_json("tm:test:roundtrip") == payload, (
            "缓存往返后数据结构与中文内容应完全一致"
        )

    def test_get_miss_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        未命中返回None: 从未写入的key在fake后端上get_json返回None，
        不抛异常（调用方据此走回源逻辑）
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = CacheClient()

        assert client.get_json("tm:cases:list:never-written") is None
        assert client.get_json("tm:reports:summary") is None

    def test_invalidate_cases_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        前缀失效精确性: 三个tm:cases:list:* key失效后全部miss，
        其他前缀（tm:other:*）key不受影响
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = CacheClient()

        # 三个列表key + 一个其他业务域key
        client.set_json("tm:cases:list:aaa", {"v": 1})
        client.set_json("tm:cases:list:bbb", {"v": 2})
        client.set_json("tm:cases:list:ccc", {"v": 3})
        client.set_json("tm:other:keep", {"v": 9})

        client.invalidate_cases_list()

        assert client.get_json("tm:cases:list:aaa") is None
        assert client.get_json("tm:cases:list:bbb") is None
        assert client.get_json("tm:cases:list:ccc") is None, (
            "tm:cases:list:前缀下的key应被全部删除"
        )
        assert client.get_json("tm:other:keep") == {"v": 9}, (
            "非目标前缀key不应被误删"
        )

    def test_fake_backend_independent_per_test(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        fake实例跨用例隔离: 模拟fixture重建动作——reset_backend后
        新后端是不同实例且读不到旧实例数据（连续用例数据零串扰）
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")

        # 用路由同款单例验证，才与真实请求链路的重建语义一致
        cache_client.reset_backend()
        backend_first = cache_client._get_backend()
        cache_client.set_json("tm:cases:list:isolation", {"批次": 1})
        assert cache_client.get_json("tm:cases:list:isolation") == {"批次": 1}

        # fixture teardown/setup等价动作: 丢弃旧实例并重建
        cache_client.reset_backend()
        backend_second = cache_client._get_backend()

        assert backend_first is not backend_second, "重建后应是全新fake实例"
        assert cache_client.get_json("tm:cases:list:isolation") is None, (
            "新fake实例不应读到旧实例的任何数据"
        )
        # 收尾复位，保持单例干净（env由monkeypatch自动还原）
        cache_client.reset_backend()


# ===========================================================================
# B组: 路由层缓存命中（Flask test client，spy patch真实调用点）
# ===========================================================================
@allure.feature("Redis缓存层")
@allure.story("路由层缓存命中")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestRouteCacheHit:
    """六个查询端点的缓存命中/key隔离行为验证"""

    def test_cases_list_cache_hit(self, cache_env: FlaskClient) -> None:
        """
        列表缓存命中: 首次?status=active回源1次；同参数二次请求
        命中缓存计数不变；改?status=disabled为新key再回源1次
        """
        with patch.object(
            CaseManager,
            "list_cases_paged",
            wraps=CaseManager.list_cases_paged,
        ) as spy:
            # 第一次请求: miss→回源→回写
            first_response = cache_env.get("/api/cases/?status=active")
            assert first_response.status_code == 200
            first_data = first_response.get_json()["data"]
            assert first_data["total"] == 4, "种子共4条active"
            assert spy.call_count == 1, "首次请求应回源一次"

            # 第二次同参数: 命中缓存，不回源
            second_response = cache_env.get("/api/cases/?status=active")
            assert second_response.status_code == 200
            assert second_response.get_json()["data"] == first_data, (
                "缓存命中数据应与回源结果一致"
            )
            assert spy.call_count == 1, "同参数第二次请求应命中缓存不回源"

            # 换参数: 新key miss→再回源一次
            disabled_response = cache_env.get(
                "/api/cases/?status=disabled"
            )
            assert disabled_response.status_code == 200
            assert disabled_response.get_json()["data"]["total"] == 0, (
                "种子无disabled用例"
            )
            assert spy.call_count == 2, "不同筛选参数应产生新key并回源"

    def test_reports_summary_cache_hit(self, cache_env: FlaskClient) -> None:
        """
        汇总缓存命中: 触发批次轮询finished后，两次GET summary仅
        回源ReportRepository.get_overview_summary一次，且数据为
        1批次/4执行/2过2挂的真实聚合（不是缓存了空库旧值）
        """
        _trigger_and_wait(cache_env)

        with patch.object(
            ReportRepository,
            "get_overview_summary",
            wraps=ReportRepository.get_overview_summary,
        ) as spy:
            first_response = cache_env.get("/api/reports/summary")
            assert first_response.status_code == 200
            first_data = first_response.get_json()["data"]
            assert spy.call_count == 1, "首次summary应回源一次"

            second_response = cache_env.get("/api/reports/summary")
            assert second_response.status_code == 200
            assert second_response.get_json()["data"] == first_data
            assert spy.call_count == 1, "第二次summary应命中缓存"

        # 命中的必须是批次完成后的新鲜聚合（失效埋点已清旧值）
        assert first_data["total_batches"] == 1
        assert first_data["total_executed"] == 4
        assert first_data["passed"] == 2
        assert first_data["failed"] == 2

    def test_different_limits_separate_keys(
        self, cache_env: FlaskClient
    ) -> None:
        """
        limit隔离: trend?limit=5与?limit=10是两个独立key，各自
        首次miss回源一次、二次命中，互不干扰
        """
        _trigger_and_wait(cache_env)

        with patch.object(
            ReportRepository,
            "get_trend_data",
            wraps=ReportRepository.get_trend_data,
        ) as spy:
            # limit=5: miss回源→命中
            response_5a = cache_env.get("/api/reports/trend?limit=5")
            assert response_5a.status_code == 200
            assert len(response_5a.get_json()["data"]) == 1
            assert spy.call_count == 1

            response_5b = cache_env.get("/api/reports/trend?limit=5")
            assert response_5b.status_code == 200
            assert spy.call_count == 1, "同limit二次请求应命中"

            # limit=10: 新key miss回源
            response_10a = cache_env.get("/api/reports/trend?limit=10")
            assert response_10a.status_code == 200
            assert len(response_10a.get_json()["data"]) == 1
            assert spy.call_count == 2, "不同limit应各自回源一次"

            response_10b = cache_env.get("/api/reports/trend?limit=10")
            assert response_10b.status_code == 200
            assert spy.call_count == 2, "同limit二次请求应命中"


# ===========================================================================
# C组: 失效策略与故障降级
# ===========================================================================
@allure.feature("Redis缓存层")
@allure.story("失效策略与故障降级")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCacheInvalidationAndDegrade:
    """写操作主动失效 + Redis故障静默降级验证"""

    def test_case_write_invalidates_list(
        self, cache_env: FlaskClient
    ) -> None:
        """
        写后失效: 首次列表回源1次并缓存→POST创建新用例（核心层
        成功后主动清列表缓存）→同参数再请求重新回源1次且可见新数据
        """
        with patch.object(
            CaseManager,
            "list_cases_paged",
            wraps=CaseManager.list_cases_paged,
        ) as spy:
            first_response = cache_env.get("/api/cases/?status=active")
            assert first_response.status_code == 200
            assert first_response.get_json()["data"]["total"] == 4
            assert spy.call_count == 1

            # 走真实HTTP创建: CaseManager.create_case成功后埋失效点
            create_response = cache_env.post(
                "/api/cases/",
                json={
                    "case_id": "TM-CACHE-0003",
                    "name": "缓存失效验证专用用例",
                    "module": "用户中心",
                    "priority": "P2",
                },
            )
            assert create_response.status_code == 201, "创建用例应返回201"

            second_response = cache_env.get("/api/cases/?status=active")
            assert second_response.status_code == 200
            second_data = second_response.get_json()["data"]
            assert spy.call_count == 2, (
                "写操作后缓存应已失效，同参数请求重新回源"
            )
            assert second_data["total"] == 5, "失效后应查到含新用例的5条"
            assert any(
                item["case_id"] == "TM-CACHE-0003"
                for item in second_data["items"]
            ), "新创建用例应出现在失效后重建的列表结果中"

    def test_redis_down_degrades_gracefully(
        self, cache_env: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        故障降级: 后端所有命令抛redis.ConnectionError时，用例列表
        与报告汇总接口仍200返回正确数据（get按miss回源、set/失效
        no-op），缓存故障绝不冒泡成API 500
        """

        class _DownRedis:
            """模拟Redis不可达: 所有命令抛ConnectionError的假后端"""

            def get(self, key: str) -> None:
                """读取命令抛连接异常（模拟Redis宕机）"""
                raise redis.ConnectionError(
                    "Error 99 connecting to fake-down:6379. 模拟宕机。"
                )

            def setex(self, name: str, time: int, value: str) -> None:
                """写入命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: setex失败")

            def scan_iter(self, match: Optional[str] = None):
                """SCAN命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: scan失败")

            def delete(self, key: str) -> None:
                """删除命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: delete失败")

            def close(self) -> None:
                """关闭命令在测试teardown被调用，降级对象无资源需释放"""
                return None

        # cache_env已setenv启用缓存，直接替换单例后端为宕机替身
        monkeypatch.setattr(cache_client, "_backend", _DownRedis())

        # 用例列表: get异常→miss回源→set异常no-op，响应仍正确
        cases_response = cache_env.get("/api/cases/?status=active")
        assert cases_response.status_code == 200, (
            "Redis故障时用例列表接口必须仍为200，禁止500"
        )
        cases_data = cases_response.get_json()["data"]
        assert cases_data["total"] == 4, "降级回源应返回真实4条种子"

        # 报告汇总: 同样静默降级，空库汇总返回零值结构
        summary_response = cache_env.get("/api/reports/summary")
        assert summary_response.status_code == 200, (
            "Redis故障时报告汇总接口必须仍为200，禁止500"
        )
        summary_data = summary_response.get_json()["data"]
        assert summary_data["total_batches"] == 0, "本例未触发批次，应为空库汇总"
        assert summary_data["overall_pass_rate"] == 0.0
