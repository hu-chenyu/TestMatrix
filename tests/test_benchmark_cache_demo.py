"""
TestMatrix Day33: Redis缓存层量化基准组件测试
（健康检测/延迟统计/造量清理/场景执行器/穿透防护验证）

测试覆盖（10条，三组）:
    A组 基础组件单测（5条）:
        1. test_redis_health_checker_check_success
           ping成功时check()为True
        2. test_redis_health_checker_check_failure
           ping抛ConnectionError时check()为False且安装指引不抛
        3. test_latency_stats_percentiles
           100个递增样本的分位数/均值/极值口径
        4. test_latency_stats_qps
           QPS=样本数/总耗时
        5. test_benchmark_data_seeder_seed_and_cleanup
           BM-造量与cleanup幂等清理、非BM-数据不受影响
    B组 基准执行器（3条，Flask test client + fakeredis）:
        6. test_runner_cases_list_cache_hit
           缓存开启10请求全采集，第2次起key已存在（命中）
        7. test_runner_cases_list_cache_disabled
           缓存关闭10请求全采集且缓存中无key
        8. test_runner_reports_summary_cache_hit
           summary场景10请求，tm:reports:summary被写入
    C组 穿透防护验证（2条）:
        9. test_runner_penetration_empty_result_and_invalidation
           空结果缓存与写后主动失效均为True
        10. test_runner_penetration_ttl_expires
            TTL=1时1.5秒后缓存自动过期（唯一允许sleep的测试）

测试铁律:
    - 全部走fake://内存后端，零真实Redis进程依赖
    - 小规模造量（50用例/5批次），function级独立临时SQLite库
    - worker线程不涉及（基准为同步测量，队列开关全部清除）
    - loguru，无print
"""

import time
from pathlib import Path
from typing import Iterator

import fakeredis
import pytest
import redis
from flask.testing import FlaskClient
from unittest.mock import patch

from scripts.benchmark_cache import (
    BenchmarkDataSeeder,
    CacheBenchmarkRunner,
    LatencyStats,
    RedisHealthChecker,
)
from src.core.cache import cache_client, cases_list_key, reports_summary_key
from src.core import event_bus
from src.db.db_session import DatabaseSession
from src.web import create_app

# 小规模造量参数（单元测试用，真实基准为1000用例/100批次）
SMALL_CASE_COUNT = 50
SMALL_BATCH_COUNT = 5


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture
def benchmark_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    基准组件测试应用fixture（function级临时SQLite库+小规模造量）

    准备: 队列开关清除（不起worker）、临时SQLite库、50 BM-用例、
    5个执行批次；teardown: cleanup基准数据、复位引擎/事件通道、
    删除库文件。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_path = tmp_path / "benchmark_unit.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    # 队列必须关闭: 基准是同步测量，禁止worker消费干扰
    monkeypatch.delenv("TM_TASK_QUEUE_ENABLED", raising=False)
    monkeypatch.delenv("TM_TASK_WORKER_ENABLED", raising=False)
    DatabaseSession.reset()
    DatabaseSession.init_db()

    seeder = BenchmarkDataSeeder()
    seeder.seed_cases(count=SMALL_CASE_COUNT)
    seeder.seed_executions(batch_count=SMALL_BATCH_COUNT)

    app = create_app("test")
    yield app.test_client()

    # teardown: 清BM数据→复位引擎与通道→删临时库
    seeder.cleanup()
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    fakeredis内存后端fixture（function级隔离）

    设置缓存开关与fake协议并reset单例后端；teardown复位。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[None]: 无数据，仅环境与后端生命周期
    """
    monkeypatch.setenv("TM_REDIS_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    cache_client.reset_backend()
    yield
    cache_client.reset_backend()


# ===========================================================================
# A组: 基础组件单测
# ===========================================================================
@pytest.mark.api
@pytest.mark.regression
class TestBenchmarkComponents:
    """健康检测/延迟统计/造量清理组件"""

    def test_redis_health_checker_check_success(self) -> None:
        """
        ping成功即健康: monkeypatch redis.from_url返回fakeredis
        实例（ping返回True），check()应为True
        """
        fake_backend = fakeredis.FakeRedis(decode_responses=True)
        with patch(
            "scripts.benchmark_cache.redis.from_url",
            return_value=fake_backend,
        ):
            assert RedisHealthChecker.check("redis://fake-host:6379/0") is True

    def test_redis_health_checker_check_failure(self) -> None:
        """
        ping故障即不健康且指引可打印: 假后端ping抛
        ConnectionError时check()为False；print_install_guide本身
        不抛异常（仅stdout输出安装方式）
        """

        class _DownRedis:
            """ping即抛连接异常的假后端"""

            def ping(self) -> bool:
                """模拟Redis不可达"""
                raise redis.ConnectionError("模拟宕机: ping失败")

        with patch(
            "scripts.benchmark_cache.redis.from_url",
            return_value=_DownRedis(),
        ):
            assert (
                RedisHealthChecker.check("redis://fake-host:6379/0") is False
            )
        # 安装指引面向CLI操作者，三种方式打印不抛异常即可
        RedisHealthChecker.print_install_guide()

    def test_latency_stats_percentiles(self) -> None:
        """
        分位数口径: 1-100ms递增样本，P50=50.5、P95≈95.05
        （inclusive口径，与95.5差0.5以内）、P99≈99.0、均值50.5、
        最小1、最大100；to_dict含全部指标键
        """
        stats = LatencyStats()
        for value in range(1, 101):
            stats.add(float(value))

        assert stats.p50() == pytest.approx(50.5)
        assert stats.p95() == pytest.approx(95.5, abs=0.5)
        assert stats.p99() == pytest.approx(99.0, abs=0.1)
        assert stats.mean() == pytest.approx(50.5)
        assert stats.min() == 1.0
        assert stats.max() == 100.0

        metric_dict = stats.to_dict()
        assert set(metric_dict.keys()) == {
            "count", "p50", "p95", "p99", "mean", "min", "max", "qps"
        }

    def test_latency_stats_qps(self) -> None:
        """
        QPS计算: 200个1ms样本、总耗时0.2s，QPS≈1000
        """
        stats = LatencyStats()
        for _ in range(200):
            stats.add(1.0)
        assert stats.qps(0.2) == pytest.approx(1000.0)

    def test_benchmark_data_seeder_seed_and_cleanup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        造量与清理: 造50条BM-用例后分页查询total>=50且case_id
        全为BM-前缀；先放一条非BM用例，cleanup后BM-清零、
        非BM-用例保留（cleanup幂等不误删）
        """
        db_path = tmp_path / "seeder_unit.db"
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
        DatabaseSession.reset()
        DatabaseSession.init_db()
        try:
            # 非BM-的存量用例（cleanup不应触碰）
            from src.core.case_manager import CaseManager

            CaseManager.create_case(
                {
                    "case_id": "TM-KEEP-0001",
                    "name": "存量保留用例",
                    "module": "default",
                }
            )

            seeder = BenchmarkDataSeeder()
            seeder.seed_cases(count=SMALL_CASE_COUNT)
            # page_size受MAX_PAGE_SIZE=100约束，50 BM-+1存量共51条
            # 一页可取全；total为过滤后总数，与分页上限无关
            page_data = CaseManager.list_cases_paged(
                status=None, page=1, page_size=100
            )
            bm_cases = [
                item for item in page_data["items"]
                if item["case_id"].startswith("BM-")
            ]
            assert page_data["total"] >= 50
            assert len(bm_cases) == SMALL_CASE_COUNT
            assert all(
                item["case_id"].startswith("BM-MOD") for item in bm_cases
            )

            # 清理后BM-清零，非BM-保留
            seeder.cleanup()
            after_data = CaseManager.list_cases_paged(
                status=None, page=1, page_size=100
            )
            remaining_ids = [
                item["case_id"] for item in after_data["items"]
            ]
            assert not any(
                case_id.startswith("BM-") for case_id in remaining_ids
            ), "cleanup后不应残留BM-用例"
            assert "TM-KEEP-0001" in remaining_ids, "非BM-用例不应被误删"
        finally:
            DatabaseSession.reset()
            db_path.unlink(missing_ok=True)


# ===========================================================================
# B组: 基准执行器（fakeredis + Flask test client）
# ===========================================================================
@pytest.mark.api
@pytest.mark.regression
class TestBenchmarkRunner:
    """缓存关/开两组场景执行与key写入验证"""

    def test_runner_cases_list_cache_hit(
        self, benchmark_app: FlaskClient, fake_redis: None
    ) -> None:
        """
        列表缓存命中: 开启缓存跑10次，样本数=10；首次回源后key
        已写入，后续请求命中（get_json非None）
        """
        runner = CacheBenchmarkRunner(
            benchmark_app.application, "fake://0"
        )
        stats = runner.run_scenario_cases_list(
            cache_enabled=True, request_count=10
        )
        assert len(stats.samples) == 10

        key = cases_list_key(
            {
                "module": None,
                "priority": None,
                "case_type": None,
                "status": "active",
                "keyword": None,
                "page": 1,
                "page_size": 20,
            }
        )
        assert cache_client.get_json(key) is not None, (
            "首次回源后应已回写缓存"
        )

    def test_runner_cases_list_cache_disabled(
        self, benchmark_app: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        缓存关闭直连DB: 跑10次样本数=10，且开关关闭时
        get_json恒为None（缓存key不存在）
        """
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        cache_client.reset_backend()
        runner = CacheBenchmarkRunner(
            benchmark_app.application, "fake://0"
        )
        stats = runner.run_scenario_cases_list(
            cache_enabled=False, request_count=10
        )
        assert len(stats.samples) == 10

        key = cases_list_key(
            {
                "module": None,
                "priority": None,
                "case_type": None,
                "status": "active",
                "keyword": None,
                "page": 1,
                "page_size": 20,
            }
        )
        assert cache_client.get_json(key) is None, (
            "缓存关闭时不应写入任何key"
        )

    def test_runner_reports_summary_cache_hit(
        self, benchmark_app: FlaskClient, fake_redis: None
    ) -> None:
        """
        汇总缓存命中: 开启缓存跑10次，样本数=10，
        tm:reports:summary固定key存在
        """
        runner = CacheBenchmarkRunner(
            benchmark_app.application, "fake://0"
        )
        stats = runner.run_scenario_reports_summary(
            cache_enabled=True, request_count=10
        )
        assert len(stats.samples) == 10
        assert cache_client.get_json(reports_summary_key()) is not None, (
            "summary首次回源后应已缓存"
        )


# ===========================================================================
# C组: 穿透防护验证
# ===========================================================================
@pytest.mark.api
@pytest.mark.regression
class TestPenetrationProtection:
    """空结果防穿透/写后失效/TTL兜底三项正确性"""

    def test_runner_penetration_empty_result_and_invalidation(
        self, benchmark_app: FlaskClient, fake_redis: None
    ) -> None:
        """
        空结果缓存与主动失效: run_scenario_penetration返回的
        empty_result_cached与invalidation_works均为True
        （ttl_expires由独立用例验证，此处不等待sleep）
        """
        runner = CacheBenchmarkRunner(
            benchmark_app.application, "fake://0"
        )
        # TTL子场景会sleep 1.5s，这里直接验证前两项: 先单独复刻
        runner._configure(cache_enabled=True)
        empty_params = {
            "module": "nonexistent_module_xyz",
            "priority": None,
            "case_type": None,
            "status": "active",
            "keyword": None,
            "page": 1,
            "page_size": 20,
        }
        response = benchmark_app.get(
            "/api/cases/?module=nonexistent_module_xyz&page=1&page_size=20"
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["total"] == 0
        assert cache_client.get_json(cases_list_key(empty_params)) is not None

        # 完整方法的前两项结论也必须为True（含探针用例创建/删除）
        verdict = runner.run_scenario_penetration()
        assert verdict["empty_result_cached"] is True
        assert verdict["invalidation_works"] is True

    def test_runner_penetration_ttl_expires(
        self, benchmark_app: FlaskClient, fake_redis: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        TTL到期兜底（唯一允许固定sleep的测试）: TM_CACHE_TTL=1
        时回写的列表缓存1.5秒后自动过期，get_json返回None。
        1.5s>1s TTL是确定性等待，非时序赌运气，不会flaky。
        """
        monkeypatch.setenv("TM_CACHE_TTL", "1")
        runner = CacheBenchmarkRunner(
            benchmark_app.application, "fake://0"
        )
        runner._configure(cache_enabled=True)

        response = benchmark_app.get("/api/cases/?page=1&page_size=20")
        assert response.status_code == 200
        key = cases_list_key(
            {
                "module": None,
                "priority": None,
                "case_type": None,
                "status": "active",
                "keyword": None,
                "page": 1,
                "page_size": 20,
            }
        )
        assert cache_client.get_json(key) is not None, "写入后应立即存在"

        time.sleep(1.5)  # 等待超过1秒TTL，验证SETEX兜底过期
        assert cache_client.get_json(key) is None, "TTL到期后缓存应自动失效"
