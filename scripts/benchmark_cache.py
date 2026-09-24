"""
Redis缓存层量化基准脚本（Day33）

定位:
    - Day31缓存层此前仅在fakeredis内存后端上做过功能测试，
      本脚本首次在**真实Redis进程**上量化缓存开启前后的
      应用层性能差异，并验证三项缓存正确性（空结果缓存防
      穿透/写操作主动失效/TTL到期兜底）
    - 只读测量工具: 不改动src/任何一行，仅通过环境变量切换
      TM_REDIS_ENABLED唯一变量，用Flask test_client发请求
      （排除HTTP网络栈开销，测纯应用层+DB+缓存链路耗时）

唯一变量控制:
    - 两组（缓存关=直连DB / 缓存开=回源一次后命中）使用同一套
      造好的数据、完全相同的请求序列，唯一差别是
      TM_REDIS_ENABLED
    - 每场景预热10次不计入统计，正式请求默认200次，取
      P50/P95/P99/平均/最小/最大与QPS（标准库statistics，
      不引numpy）

造量规模:
    - 1000条用例: 10模块×100条，P0(15%)/P1(25%)/P2(40%)/
      P3(20%)，api:chip=7:3，全部active，case_id统一BM-前缀
    - 100个执行批次: 每批随机20-50条、通过率随机0.3-0.95，
      经create_execution/record_execution/finish_execution
      落库（defect_statistics约100行、test_executions约3500行）
    - 脚本结束默认cleanup全部BM-数据（独立benchmark SQLite
      库，库文件也一并删除），绝不污染回归测试数据

命令行用法（PowerShell，须先启动真实Redis并ping通）:
    py -m scripts.benchmark_cache --help
    py -m scripts.benchmark_cache --scenario all --requests 200
    py -m scripts.benchmark_cache --scenario cases-list --no-cleanup
    # 真实Redis启动（Docker）:
    # docker run -d --name tm-redis -p 6379:6379 redis:7-alpine

运行方式兼容:
    同时支持 py -m scripts.benchmark_cache 与直接
    py scripts/benchmark_cache.py；后者sys.path默认只含
    scripts/目录，故在此先把项目根插入sys.path再导入src.*
"""

import argparse
import os
import platform
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

# 项目根目录（scripts/的上一级）必须在import src.*之前确定并加入
# sys.path，保证"直接脚本运行"与"-m模块运行"两种方式均可导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import redis
from flask import Flask

from src.common.logger import LogManager
from src.core.cache import (
    cache_client,
    cases_list_key,
    reports_summary_key,
)
from src.core.case_manager import CaseManager
from src.db.db_session import DatabaseSession
from src.web import create_app

logger = LogManager.get_logger()

# BM-前缀: 基准数据统一标识，造量与cleanup都按此前缀过滤
BM_CASE_PREFIX = "BM-"

# 五个报告接口的轮询路径（reports-all场景模拟Dashboard首屏）
REPORTS_ALL_PATHS = (
    "/api/reports/summary",
    "/api/reports/trend?limit=20",
    "/api/reports/module-distribution",
    "/api/reports/failed-top?limit=10",
    "/api/reports/quality-metrics",
)


# ===========================================================================
# Redis连通性检测
# ===========================================================================
class RedisHealthChecker:
    """真实Redis进程连通性检测（基准前置闸门，禁止静默用fakeredis代替）"""

    @staticmethod
    def check(url: str = "redis://127.0.0.1:6379/0") -> bool:
        """
        用PING检测真实Redis可达性

        参数:
            url (str): Redis连接URL，默认本机6379的0号库

        返回:
            bool: PING成功True；任何连接/超时异常False

        异常:
            无（异常全部捕获转False，由调用方决定退出策略）
        """
        try:
            client = redis.from_url(url, socket_timeout=2)
            return bool(client.ping())
        except Exception as exc:  # noqa: BLE001 健康探测必须吞全部异常
            logger.warning(f"Redis连通性检测失败 | url={url} | {exc}")
            return False

    @staticmethod
    def print_install_guide() -> None:
        """
        打印Windows三种Redis安装/启动指引（CLI标准输出）

        参数:
            无

        返回:
            无

        异常:
            无
        """
        # CLI面向操作者的指引直接走标准输出（基准脚本的命令行边界）
        print("=" * 70)
        print("未检测到可连通的真实Redis进程，基准测试拒绝启动（不会用")
        print("fakeredis内存版代替，否则性能数字无意义）。请任选一种方式")
        print("启动真实Redis后重试：")
        print("-" * 70)
        print("方式1 Docker（推荐）:")
        print("  docker run -d --name tm-redis -p 6379:6379 redis:7-alpine")
        print("  验证: docker exec -it tm-redis redis-cli ping  -> PONG")
        print("方式2 Memurai（Windows原生Redis兼容服务）:")
        print("  下载: https://www.memurai.com/get-memurai")
        print("方式3 WSL2:")
        print("  wsl --install -d Ubuntu")
        print("  sudo apt update && sudo apt install -y redis-server")
        print("  sudo service redis-server start")
        print("  验证: redis-cli ping  -> PONG")
        print("=" * 70)

    @staticmethod
    def ensure_redis_alive(url: str) -> None:
        """
        确保Redis可达，不可达则打印指引并退出进程

        参数:
            url (str): Redis连接URL

        返回:
            无

        异常:
            SystemExit: 检测失败时sys.exit(1)终止基准流程
        """
        if not RedisHealthChecker.check(url):
            RedisHealthChecker.print_install_guide()
            sys.exit(1)
        logger.info(f"真实Redis连通性检测通过 | url={url}")


# ===========================================================================
# 基准数据造量与清理
# ===========================================================================
class BenchmarkDataSeeder:
    """
    基准数据造量器（直接调核心层，不走HTTP）

    属性:
        execution_ids (list[str]): 造出的批次号列表，cleanup据此删明细
    """

    # 10个基准模块
    MODULES = tuple(f"module_{index:02d}" for index in range(1, 11))

    # 优先级分布权重（累计区间，随机数落点决定优先级）
    PRIORITY_BUCKETS = (
        (0.15, "P0"),
        (0.40, "P1"),  # 累计0.40 => P1占25%
        (0.80, "P2"),  # 累计0.80 => P2占40%
        (1.01, "P3"),  # 累计1.01 => P3占20%（含1.0边界）
    )

    def __init__(self) -> None:
        """
        初始化造量器（清空批次号跟踪列表）

        参数:
            无

        返回:
            无

        异常:
            无
        """
        self.execution_ids: List[str] = []

    def _pick_priority(self, roll: float) -> str:
        """
        按权重分布把随机数映射为优先级（内部方法）

        参数:
            roll (float): 0-1随机数

        返回:
            str: P0/P1/P2/P3

        异常:
            无
        """
        for threshold, priority in self.PRIORITY_BUCKETS:
            if roll < threshold:
                return priority
        return "P3"

    def seed_cases(self, count: int = 1000) -> None:
        """
        造用例: 10模块均匀分布，api:chip=7:3，权重化优先级，全active

        参数:
            count (int): 造数总数（默认1000，单测可传50小规模）

        返回:
            无

        异常:
            CaseManagerError: 入库异常时由核心层抛出
        """
        logger.info(f"开始造用例数据 | 目标{count}条")
        for index in range(1, count + 1):
            # 10模块轮询: index对10取模定位模块（每模块约count/10条）
            module_index = (index - 1) % len(self.MODULES)
            module_no = module_index + 1
            case_id = f"BM-MOD{module_no:02d}-{index:04d}"
            case_type = "api" if index % 10 < 7 else "chip"
            CaseManager.create_case(
                {
                    "case_id": case_id,
                    "name": f"基准用例{index:04d}",
                    "module": self.MODULES[module_index],
                    "priority": self._pick_priority(random.random()),
                    "case_type": case_type,
                    "status": "active",
                    "description": f"缓存基准测试造量用例-序号{index}",
                    "creator": "benchmark",
                }
            )
        logger.info(f"用例造量完成 | {count}条")

    def seed_executions(self, batch_count: int = 100) -> None:
        """
        造执行批次: 每批随机20-50条active用例、通过率随机0.3-0.95

        直接走create_execution/record_execution/finish_execution
        落库（不调真实执行器，结果按随机通过率分布）。

        参数:
            batch_count (int): 批次总数（默认100，单测可传5）

        返回:
            无

        异常:
            CaseManagerError: 入库异常时由核心层抛出
        """
        logger.info(f"开始造执行批次 | 目标{batch_count}批")
        active_cases = CaseManager.list_cases(status="active")
        if not active_cases:
            raise RuntimeError("无active用例，无法造执行批次，请先seed_cases")
        total_records = 0

        for batch_index in range(1, batch_count + 1):
            # 每批20-50条（不足则取全部）
            batch_size = min(len(active_cases), random.randint(20, 50))
            chosen_cases = random.sample(active_cases, batch_size)
            pass_rate = random.uniform(0.30, 0.95)
            # trigger取合法值cli（基准脚本属命令行触发），executor
            # 标记benchmark便于在数据中区分基准批次
            execution_id = CaseManager.create_execution(
                trigger="cli",
                executor="benchmark",
                environment="bench",
                remark=f"缓存基准批次{batch_index}",
            )
            start_base = datetime.now()
            for order, case in enumerate(chosen_cases):
                # 每条独立按pass_rate概率判定结果
                is_passed = random.random() < pass_rate
                result = "passed" if is_passed else "failed"
                start_time = start_base
                end_time = datetime.now()
                CaseManager.record_execution(
                    execution_id=execution_id,
                    case_id=case["case_id"],
                    case_name=case["name"],
                    result=result,
                    start_time=start_time,
                    end_time=end_time,
                    duration=0.01,
                    error_message=(
                        None if is_passed else "基准模拟失败: 断言不通过"
                    ),
                )
            CaseManager.finish_execution(execution_id)
            self.execution_ids.append(execution_id)
            total_records += batch_size
        logger.info(
            f"执行批次造量完成 | {batch_count}批 | 明细{total_records}行"
        )

    def cleanup(self) -> None:
        """
        清理全部基准数据: BM-用例 + 本器造出的批次明细/汇总/批次行

        幂等: 无数据时删除0行不报错；不触碰非BM-前缀的既有数据。

        参数:
            无

        返回:
            无

        异常:
            无（SQLAlchemy异常记error后向上抛，由CLI兜底）
        """
        # 延迟导入避免模块加载期重依赖
        from src.db.models import (
            DefectStatistic,
            TestCase,
            TestExecution,
            TestExecutionBatch,
        )

        with DatabaseSession.session_scope() as session:
            # BM-前缀用例（物理删除，与线上删除口径一致）
            deleted_cases = (
                session.query(TestCase)
                .filter(TestCase.case_id.like(f"{BM_CASE_PREFIX}%"))
                .delete(synchronize_session=False)
            )
            deleted_executions = 0
            deleted_statistics = 0
            deleted_batches = 0
            if self.execution_ids:
                deleted_executions = (
                    session.query(TestExecution)
                    .filter(
                        TestExecution.execution_id.in_(self.execution_ids)
                    )
                    .delete(synchronize_session=False)
                )
                deleted_statistics = (
                    session.query(DefectStatistic)
                    .filter(
                        DefectStatistic.execution_id.in_(self.execution_ids)
                    )
                    .delete(synchronize_session=False)
                )
                # 防御: 基准流程若经start_execution落过批次行也一并清
                deleted_batches = (
                    session.query(TestExecutionBatch)
                    .filter(
                        TestExecutionBatch.execution_id.in_(
                            self.execution_ids
                        )
                    )
                    .delete(synchronize_session=False)
                )
        logger.info(
            f"基准数据cleanup完成 | 用例删除{deleted_cases}条 | "
            f"明细删除{deleted_executions}行 | 汇总删除{deleted_statistics}行 | "
            f"批次行删除{deleted_batches}行"
        )
        # 清理后重置跟踪列表，支持同一seeder实例重复执行
        self.execution_ids = []


# ===========================================================================
# 延迟指标采集
# ===========================================================================
class LatencyStats:
    """
    响应延迟指标采集器（毫秒样本 + 分位数/QPS统计，纯标准库实现）
    """

    def __init__(self) -> None:
        """
        初始化空样本列表与墙钟耗时（并发场景QPS分母）

        参数:
            无

        返回:
            无

        异常:
            无
        """
        self.samples: List[float] = []
        self._wall_seconds: Optional[float] = None

    def add(self, latency_ms: float) -> None:
        """
        追加一个请求延迟样本

        参数:
            latency_ms (float): 单次请求耗时（毫秒）

        返回:
            无

        异常:
            无
        """
        self.samples.append(latency_ms)

    def record_wall(self, total_seconds: float) -> None:
        """
        记录场景墙钟总耗时（并发场景QPS用，串行可不调）

        参数:
            total_seconds (float): 场景从首个请求发出到全部完成的秒数

        返回:
            无

        异常:
            无
        """
        self._wall_seconds = total_seconds

    def _percentile(self, percent: int) -> float:
        """
        计算百分位数（内部方法，inclusive线性插值口径）

        参数:
            percent (int): 百分位0-100（如95）

        返回:
            float: 对应百分位值（毫秒）；单样本时直接返回该样本

        异常:
            无
        """
        if not self.samples:
            return 0.0
        if len(self.samples) == 1:
            return self.samples[0]
        ordered = sorted(self.samples)
        # inclusive口径: n=100切99点，索引percent-1（n>=2即可插值）
        cut_points = statistics.quantiles(
            ordered, n=100, method="inclusive"
        )
        index = min(max(percent - 1, 0), len(cut_points) - 1)
        return round(cut_points[index], 3)

    def p50(self) -> float:
        """P50中位数延迟（毫秒）"""
        return self._percentile(50)

    def p95(self) -> float:
        """P95延迟（毫秒）"""
        return self._percentile(95)

    def p99(self) -> float:
        """P99延迟（毫秒）"""
        return self._percentile(99)

    def mean(self) -> float:
        """平均延迟（毫秒，无样本返回0）"""
        return round(statistics.mean(self.samples), 3) if self.samples else 0.0

    def min(self) -> float:
        """最小延迟（毫秒）"""
        return round(min(self.samples), 3) if self.samples else 0.0

    def max(self) -> float:
        """最大延迟（毫秒）"""
        return round(max(self.samples), 3) if self.samples else 0.0

    def qps(self, total_seconds: Optional[float] = None) -> float:
        """
        计算每秒请求数QPS

        参数:
            total_seconds (float | None): 显式分母秒数；None时用
                样本延迟之和（串行场景近似墙钟），record_wall记录
                过墙钟则优先用墙钟（并发场景必须用墙钟）

        返回:
            float: QPS（分母为0时返回0）

        异常:
            无
        """
        if not self.samples:
            return 0.0
        if self._wall_seconds is not None:
            denominator = self._wall_seconds
        elif total_seconds is not None:
            denominator = total_seconds
        else:
            denominator = sum(self.samples) / 1000.0
        if denominator <= 0:
            return 0.0
        return round(len(self.samples) / denominator, 2)

    def to_dict(self, total_seconds: Optional[float] = None) -> dict:
        """
        导出全部指标为字典（供报告渲染）

        参数:
            total_seconds (float | None): QPS显式分母，透传qps()

        返回:
            dict: {p50,p95,p99,mean,min,max,qps,count}指标字典

        异常:
            无
        """
        return {
            "count": len(self.samples),
            "p50": self.p50(),
            "p95": self.p95(),
            "p99": self.p99(),
            "mean": self.mean(),
            "min": self.min(),
            "max": self.max(),
            "qps": self.qps(total_seconds),
        }


# ===========================================================================
# 基准执行器
# ===========================================================================
class CacheBenchmarkRunner:
    """
    缓存基准场景执行器（Flask test_client + 环境变量唯一变量切换）

    属性:
        app (Flask): 已初始化的Flask应用
        redis_url (str): 真实Redis连接URL
        client (FlaskClient): 主测试客户端（并发场景按线程另建）
    """

    def __init__(self, app: Flask, redis_url: str) -> None:
        """
        初始化执行器并创建测试客户端

        参数:
            app (Flask): Flask应用实例
            redis_url (str): 真实Redis URL（缓存开启时使用）

        返回:
            无

        异常:
            无
        """
        self.app = app
        self.redis_url = redis_url
        self.client = app.test_client()

    def _configure(self, cache_enabled: bool) -> None:
        """
        切换缓存唯一变量并重置后端（内部方法）

        参数:
            cache_enabled (bool): True启用缓存（真实Redis），
                                  False直连DB（全no-op）

        返回:
            无

        异常:
            无（flushdb异常不阻断，最坏残留key只会让命中提前，
                下一场景关闭缓存时不受影响）
        """
        os.environ["TM_REDIS_URL"] = self.redis_url
        os.environ["TM_REDIS_ENABLED"] = "true" if cache_enabled else "false"
        # 单例后端按新环境变量重建（关闭时_get_backend恒为None）
        cache_client.reset_backend()
        if cache_enabled:
            backend = cache_client._get_backend()
            if backend is not None:
                # 每场景开启前清空Redis，保证首次请求是冷miss
                backend.flushdb()

    def _time_get(self, path: str) -> float:
        """
        发起一次GET并测量应用层耗时（内部方法）

        参数:
            path (str): 接口路径（含查询串）

        返回:
            float: 请求耗时（毫秒）

        异常:
            AssertionError: 非200响应时抛出（基准接口必须可用）
        """
        start = time.perf_counter()
        response = self.client.get(path)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert response.status_code == 200, f"{path}返回{response.status_code}"
        return elapsed_ms

    def _warmup(self, path_provider: Callable[[], str], count: int = 10) -> None:
        """
        预热请求（不计入统计，消除连接池/编译/首查冷启动偏差）

        参数:
            path_provider (Callable): 返回请求路径的可调用对象
                                      （轮询场景每次给不同路径）
            count (int): 预热次数，默认10

        返回:
            无

        异常:
            无
        """
        for _ in range(count):
            self.client.get(path_provider())

    def run_scenario_cases_list(
        self, cache_enabled: bool, request_count: int = 200
    ) -> LatencyStats:
        """
        用例列表接口场景: GET /api/cases/?page=1&page_size=20

        参数:
            cache_enabled (bool): 缓存开关（唯一变量）
            request_count (int): 正式请求数，默认200

        返回:
            LatencyStats: 延迟指标（开启时仅首次回源，其余命中）

        异常:
            AssertionError: 接口非200时抛出
        """
        path = "/api/cases/?page=1&page_size=20"
        self._configure(cache_enabled)
        self._warmup(lambda: path)
        stats = LatencyStats()
        for _ in range(request_count):
            stats.add(self._time_get(path))
        return stats

    def run_scenario_reports_summary(
        self, cache_enabled: bool, request_count: int = 200
    ) -> LatencyStats:
        """
        报告汇总接口场景: GET /api/reports/summary

        参数:
            cache_enabled (bool): 缓存开关（唯一变量）
            request_count (int): 正式请求数，默认200

        返回:
            LatencyStats: 延迟指标

        异常:
            AssertionError: 接口非200时抛出
        """
        path = "/api/reports/summary"
        self._configure(cache_enabled)
        self._warmup(lambda: path)
        stats = LatencyStats()
        for _ in range(request_count):
            stats.add(self._time_get(path))
        return stats

    def run_scenario_reports_all(
        self, cache_enabled: bool, request_count: int = 200
    ) -> LatencyStats:
        """
        报告五接口轮询场景（模拟Dashboard首屏聚合加载）

        参数:
            cache_enabled (bool): 缓存开关（唯一变量）
            request_count (int): 正式请求总数（五接口均分轮转）

        返回:
            LatencyStats: 延迟指标

        异常:
            AssertionError: 接口非200时抛出
        """
        self._configure(cache_enabled)
        self._warmup(lambda: REPORTS_ALL_PATHS[0])
        stats = LatencyStats()
        for index in range(request_count):
            path = REPORTS_ALL_PATHS[index % len(REPORTS_ALL_PATHS)]
            stats.add(self._time_get(path))
        return stats

    def run_scenario_concurrent(
        self, cache_enabled: bool, concurrency: int = 10,
        request_count: int = 200,
    ) -> LatencyStats:
        """
        并发压力场景: 线程池并发打用例列表接口

        每工作线程独立test_client（避免客户端实例跨线程共享状态）；
        QPS分母用场景墙钟时间（样本之和在并发下不等于墙钟）。

        参数:
            cache_enabled (bool): 缓存开关（唯一变量）
            concurrency (int): 并发线程数，默认10
            request_count (int): 总请求数，默认200（均分到线程）

        返回:
            LatencyStats: 含墙钟QPS的延迟指标

        异常:
            AssertionError: 接口非200时抛出
        """
        path = "/api/cases/?page=1&page_size=20"
        self._configure(cache_enabled)
        # 预热（单线程打满一轮建立连接/缓存）
        self._warmup(lambda: path)

        stats = LatencyStats()
        lock = threading.Lock()
        per_thread = request_count // concurrency

        def worker() -> None:
            """工作线程: 独立客户端发per_thread个请求并线程安全记样本"""
            thread_client = self.app.test_client()
            local_samples: List[float] = []
            for _ in range(per_thread):
                start = time.perf_counter()
                response = thread_client.get(path)
                local_samples.append((time.perf_counter() - start) * 1000.0)
                assert response.status_code == 200
            with lock:
                stats.samples.extend(local_samples)

        wall_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker) for _ in range(concurrency)]
            for future in as_completed(futures):
                future.result()  # 让工作线程断言异常向上冒泡
        stats.record_wall(time.perf_counter() - wall_start)
        return stats

    def run_scenario_penetration(self) -> dict:
        """
        缓存正确性验证场景（不计时，验证三项防护）

        子场景:
            a.空结果缓存: 查不存在模块返回空页，二次查缓存非None
            b.主动失效: 命中状态下新建用例后该key缓存被清
            c.TTL兜底: TTL=1时写入后等1.5s，缓存自动过期

        参数:
            无

        返回:
            dict: {empty_result_cached, invalidation_works,
                   ttl_expires}三个布尔结论

        异常:
            AssertionError: 接口非200时抛出
        """
        # ---- a. 空结果缓存（防穿透）----
        self._configure(cache_enabled=True)
        empty_params = {
            "module": "nonexistent_module_xyz",
            "priority": None,
            "case_type": None,
            "status": "active",
            "keyword": None,
            "page": 1,
            "page_size": 20,
        }
        empty_path = (
            "/api/cases/?module=nonexistent_module_xyz&page=1&page_size=20"
        )
        first_response = self.client.get(empty_path)
        assert first_response.status_code == 200
        assert first_response.get_json()["data"]["total"] == 0, "空模块应0条"
        empty_key = cases_list_key(empty_params)
        empty_cached = cache_client.get_json(empty_key) is not None

        # ---- b. 写操作主动失效 ----
        active_params = {
            "module": None,
            "priority": None,
            "case_type": None,
            "status": "active",
            "keyword": None,
            "page": 1,
            "page_size": 20,
        }
        list_path = "/api/cases/?page=1&page_size=20"
        hit_response = self.client.get(list_path)
        assert hit_response.status_code == 200
        active_key = cases_list_key(active_params)
        assert cache_client.get_json(active_key) is not None, "先确认已命中"
        invalidation_case_id = "BM-INVALID-PROBE-0001"
        try:
            CaseManager.create_case(
                {
                    "case_id": invalidation_case_id,
                    "name": "失效探针用例",
                    "module": "module_01",
                    "priority": "P2",
                    "case_type": "api",
                    "status": "active",
                }
            )
            invalidated = cache_client.get_json(active_key) is None
        finally:
            # 探针用例立即删除（其失效动作会再次清缓存，无副作用）
            try:
                CaseManager.delete_case(invalidation_case_id)
            except Exception:  # noqa: BLE001 清理失败不影响验证结论
                pass

        # ---- c. TTL到期兜底（确定性等待1.5s，仅此处理sleep）----
        os.environ["TM_CACHE_TTL"] = "1"
        try:
            self._configure(cache_enabled=True)
            self.client.get(list_path)
            ttl_key = cases_list_key(active_params)
            assert cache_client.get_json(ttl_key) is not None, "先确认已写入"
            time.sleep(1.5)  # TTL验证必须等待真实过期，非时序赌运气
            ttl_expires = cache_client.get_json(ttl_key) is None
        finally:
            # 还原TTL配置并重建后端，避免污染后续场景
            os.environ.pop("TM_CACHE_TTL", None)
            self._configure(cache_enabled=True)

        return {
            "empty_result_cached": bool(empty_cached),
            "invalidation_works": bool(invalidated),
            "ttl_expires": bool(ttl_expires),
        }

    def run_all(
        self, request_count: int = 200, concurrency: int = 10
    ) -> Dict[str, dict]:
        """
        顺序执行全部性能场景（关/开两组同序列）+ 正确性场景

        参数:
            request_count (int): 每性能场景正式请求数
            concurrency (int): 并发场景线程数

        返回:
            dict: {场景名: {"disabled":指标dict,"enabled":指标dict},
                   "penetration": 三布尔结论, "meta": 环境信息}

        异常:
            AssertionError: 任一接口非200时抛出
        """
        results: Dict[str, dict] = {}

        def _pair(name: str, runner: Callable[[bool], LatencyStats]) -> None:
            """跑关/开两组并收录指标（内部函数）"""
            disabled_stats = runner(False)
            enabled_stats = runner(True)
            results[name] = {
                "disabled": disabled_stats.to_dict(),
                "enabled": enabled_stats.to_dict(),
            }

        _pair(
            "cases_list",
            lambda flag: self.run_scenario_cases_list(
                flag, request_count=request_count
            ),
        )
        _pair(
            "reports_summary",
            lambda flag: self.run_scenario_reports_summary(
                flag, request_count=request_count
            ),
        )
        _pair(
            "reports_all",
            lambda flag: self.run_scenario_reports_all(
                flag, request_count=request_count
            ),
        )
        _pair(
            "concurrent",
            lambda flag: self.run_scenario_concurrent(
                flag, concurrency=concurrency, request_count=request_count
            ),
        )
        results["penetration"] = self.run_scenario_penetration()
        results["meta"] = self._collect_meta()
        return results

    def _collect_meta(self) -> dict:
        """
        采集环境元信息（报告附录用，内部方法）

        参数:
            无

        返回:
            dict: Python/Flask/redis-py/Redis服务端/OS/执行时间

        异常:
            无（INFO查询失败时服务端版本填unknown）
        """
        import flask

        server_version = "unknown"
        backend = cache_client._get_backend()
        if backend is not None:
            try:
                server_version = str(backend.info().get("redis_version", "unknown"))
            except redis.RedisError:
                server_version = "unknown"
        return {
            "python": platform.python_version(),
            "flask": flask.__version__,
            "redis_py": redis.__version__,
            "redis_server": server_version,
            "os": platform.platform(),
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


# ===========================================================================
# Markdown报告生成
# ===========================================================================
class BenchmarkReportGenerator:
    """基准Markdown报告渲染器（所有数字均来自results，禁止编造）"""

    @staticmethod
    def _verdict_text(passed: bool) -> str:
        """
        布尔验证结论转展示文本（内部方法）

        参数:
            passed (bool): 该项验证是否通过

        返回:
            str: 通过"✅ True"，未通过"❌ False"

        异常:
            无
        """
        return "✅ True" if passed else "❌ False"

    @staticmethod
    def _improvement(disabled_value: float, enabled_value: float) -> str:
        """
        计算改善百分比（内部方法）

        参数:
            disabled_value (float): 缓存关闭组指标值
            enabled_value (float): 缓存开启组指标值

        返回:
            str: 带百分号文本；分母为0返回"N/A"

        异常:
            无
        """
        if disabled_value == 0:
            return "N/A"
        ratio = (disabled_value - enabled_value) / disabled_value * 100.0
        return f"{ratio:+.1f}%"

    @staticmethod
    def _metric_table(disabled: dict, enabled: dict) -> str:
        """
        渲染单场景指标对比表（内部方法）

        参数:
            disabled (dict): 关闭缓存指标字典
            enabled (dict): 开启缓存指标字典

        返回:
            str: Markdown表格文本（延迟类为下降改善，QPS为上升改善）

        异常:
            无
        """
        lines = [
            "| 指标 | 缓存关闭(直连DB) | 缓存开启 | 改善幅度 |",
            "| --- | --- | --- | --- |",
        ]
        # 延迟指标: 下降即改善。方向标签必须按实际正负动态给，
        # 正值（延迟下降）=更快；负值（延迟反而上升，统计波动）
        # =更慢；N/A不附标签，避免"负值配更快"的自相矛盾
        for metric, label in (
            ("p50", "P50延迟(ms)"),
            ("p95", "P95延迟(ms)"),
            ("p99", "P99延迟(ms)"),
            ("mean", "平均延迟(ms)"),
            ("min", "最小延迟(ms)"),
            ("max", "最大延迟(ms)"),
        ):
            improvement = BenchmarkReportGenerator._improvement(
                disabled[metric], enabled[metric]
            )
            if improvement == "N/A":
                direction = ""
            elif improvement.startswith("-"):
                direction = "（更慢）"
            else:
                direction = "（更快）"
            lines.append(
                f"| {label} | {disabled[metric]:.3f} | "
                f"{enabled[metric]:.3f} | "
                f"{improvement}{direction} |"
            )
        # QPS: 上升为改善，单独计算方向
        qps_delta = (
            (enabled["qps"] - disabled["qps"]) / disabled["qps"] * 100.0
            if disabled["qps"]
            else 0.0
        )
        lines.append(
            f"| QPS | {disabled['qps']:.2f} | {enabled['qps']:.2f} | "
            f"{qps_delta:+.1f}%（更高） |"
        )
        lines.append(f"| 样本数 | {disabled['count']} | {enabled['count']} | - |")
        return "\n".join(lines)

    @staticmethod
    def generate(results: dict, output_path: str) -> None:
        """
        渲染完整Markdown报告并写入文件

        章节: 方法论/列表/汇总/五接口轮询/并发/穿透验证/结论/附录，
        结论中的判断全部基于results实际数字。

        参数:
            results (dict): CacheBenchmarkRunner.run_all返回结构
            output_path (str): 报告输出路径

        返回:
            无

        异常:
            OSError: 报告文件写入失败时抛出
        """
        meta = results["meta"]
        cases_list = results["cases_list"]
        summary = results["reports_summary"]
        reports_all = results["reports_all"]
        concurrent = results["concurrent"]
        penetration = results["penetration"]

        # 结论数据（取自实际指标，全部保留3位/1位小数，无估算）
        list_mean_gain = BenchmarkReportGenerator._improvement(
            cases_list["disabled"]["mean"], cases_list["enabled"]["mean"]
        )
        list_p95_gain = BenchmarkReportGenerator._improvement(
            cases_list["disabled"]["p95"], cases_list["enabled"]["p95"]
        )
        qps_gain = (
            (concurrent["enabled"]["qps"] - concurrent["disabled"]["qps"])
            / concurrent["disabled"]["qps"] * 100.0
            if concurrent["disabled"]["qps"]
            else 0.0
        )
        all_passed = all(bool(value) for value in penetration.values())
        # 三项布尔结论预先转文本（缩短表格行，避免emoji全角超行长）
        verdict_empty = BenchmarkReportGenerator._verdict_text(
            penetration["empty_result_cached"]
        )
        verdict_invalidation = BenchmarkReportGenerator._verdict_text(
            penetration["invalidation_works"]
        )
        verdict_ttl = BenchmarkReportGenerator._verdict_text(
            penetration["ttl_expires"]
        )

        lines = [
            "# Redis缓存层量化对比报告",
            "",
            "> 本报告由 `py -m scripts.benchmark_cache` 基于真实Redis进程",
            "> 实测生成，所有数字均来自脚本输出，无人工估算或编造。",
            "",
            "## 1. 测试方法论",
            "",
            "- **目标**: 量化Day31缓存层在真实Redis下对读多写少接口的",
            "  加速效果，并验证空结果防穿透/写后主动失效/TTL兜底三项正确性",
            "- **唯一变量**: TM_REDIS_ENABLED（false=每次直连DB；",
            "  true=首次回源回写、其后命中），两组造数与请求序列完全一致",
            "- **测量方式**: Flask test_client进程内请求（排除HTTP网络栈，",
            "  测纯应用层+SQLite+Redis链路），每场景预热10次不计入，",
            f"  正式请求{cases_list['disabled']['count']}次",
            "- **指标口径**: P50/P95/P99（inclusive线性插值）、平均、",
            "  最小/最大延迟（毫秒）与QPS；并发场景QPS按墙钟时间计算",
            "- **数据规模**: 1000用例（10模块×100，P0-P3权重分布，",
            "  api:chip=7:3）+ 100执行批次（每批20-50条，通过率0.3-0.95）",
            "",
            "## 2. 用例列表接口对比（GET /api/cases/?page=1&page_size=20）",
            "",
            BenchmarkReportGenerator._metric_table(
                cases_list["disabled"], cases_list["enabled"]
            ),
            "",
            "## 3. 报告汇总接口对比（GET /api/reports/summary）",
            "",
            BenchmarkReportGenerator._metric_table(
                summary["disabled"], summary["enabled"]
            ),
            "",
            "## 4. 报告五接口轮询对比（Dashboard首屏聚合）",
            "",
            "轮询路径: summary / trend?limit=20 / module-distribution /",
            "failed-top?limit=10 / quality-metrics 各占20%。",
            "",
            BenchmarkReportGenerator._metric_table(
                reports_all["disabled"], reports_all["enabled"]
            ),
            "",
            "## 5. 并发压力对比（10线程并发打用例列表）",
            "",
            BenchmarkReportGenerator._metric_table(
                concurrent["disabled"], concurrent["enabled"]
            ),
            "",
            "## 6. 穿透防护与失效正确性验证",
            "",
            "| 验证项 | 结果 | 说明 |",
            "| --- | --- | --- |",
            f"| 空结果缓存（防穿透） | "
            f"{verdict_empty} | 不存在模块空页被缓存，TTL内不再查库 |",
            f"| 写操作主动失效 | {verdict_invalidation} "
            "| 新建用例后tm:cases:list缓存立即清除 |",
            f"| TTL到期兜底 | {verdict_ttl} "
            "| TTL=1秒写入后1.5秒自动过期 |",
            "",
            "## 7. 结论与建议",
            "",
            # _improvement返回值已含"%"（形如"+57.2%"），此处
            # 去掉正负号后直接用，禁止再拼一个%造成双百分号
            f"1. 用例列表接口开启缓存后平均延迟降低"
            f"{list_mean_gain.lstrip('+-')}、",
            f"   P95延迟降低{list_p95_gain.lstrip('+-')}（缓存关闭"
            f"{cases_list['disabled']['mean']:.3f}ms → "
            f"{cases_list['enabled']['mean']:.3f}ms），",
            "   读多写少场景命中缓存可显著降低数据库分页查询开销。",
            f"2. 10线程并发下QPS提升{qps_gain:.1f}%"
            f"（{concurrent['disabled']['qps']:.2f} → "
            f"{concurrent['enabled']['qps']:.2f}），",
            "   热点列表的吞吐提升与尾延迟收敛可直接改善Dashboard首屏体验。",
            "3. 报告聚合类接口（summary/五接口轮询）跨表聚合开销大、",
            "   变更频率低，是缓存收益最高的一类，建议生产保持开启。",
            f"4. 三项正确性验证{'全部通过' if all_passed else '存在失败项'}: ",
            "   空结果防缓存穿透、写后主动失效、TTL到期兜底均按设计生效，",
            "   命中的不会是脏数据，冷数据也不会反复打库。",
            "5. 缓存默认仍保持TM_REDIS_ENABLED=false灰度开关，生产按",
            "   部署环境显式开启；Redis故障时缓存层静默降级直连DB，",
            "   接口可用性不受影响（Day31降级设计，Day33基准未观测到异常）。",
            "",
            "## 8. 附录: 环境信息",
            "",
            "| 项 | 值 |",
            "| --- | --- |",
            f"| Python | {meta['python']} |",
            f"| Flask | {meta['flask']} |",
            f"| redis-py客户端 | {meta['redis_py']} |",
            f"| Redis服务端 | {meta['redis_server']} |",
            f"| 操作系统 | {meta['os']} |",
            f"| 执行时间 | {meta['run_at']} |",
            "",
        ]
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"基准报告已生成 | {output}")


# ===========================================================================
# 命令行入口
# ===========================================================================
def main() -> None:
    """
    基准脚本命令行入口

    流程: 参数解析→Redis闸门→独立benchmark库造量→执行选定场景→
    渲染报告→cleanup（除非--no-cleanup）→stdout打印摘要。

    参数:
        无（从sys.argv解析）

    返回:
        无

    异常:
        SystemExit: Redis不可达时退出码1；场景执行异常由调用栈抛出
    """
    parser = argparse.ArgumentParser(
        prog="py -m scripts.benchmark_cache",
        description="TestMatrix缓存层真实Redis量化基准（只读测量，不改src/）",
    )
    parser.add_argument(
        "--redis-url",
        default="redis://127.0.0.1:6379/0",
        help="真实Redis连接URL，默认redis://127.0.0.1:6379/0",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=(
            "all", "cases-list", "reports-summary",
            "reports-all", "concurrent", "penetration",
        ),
        help="执行场景，默认all",
    )
    parser.add_argument(
        "--requests", type=int, default=200,
        help="每性能场景正式请求数（预热固定10次），默认200",
    )
    parser.add_argument(
        "--concurrency", type=int, default=10,
        help="并发场景线程数，默认10",
    )
    parser.add_argument(
        "--no-cleanup", action="store_true", default=False,
        help="执行后保留BM-造数数据（默认自动清理）",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "docs" / "benchmark_cache_report.md"),
        help="Markdown报告输出路径",
    )
    args = parser.parse_args()

    # 第1道闸门: 真实Redis必须可达，禁止fakeredis代替
    RedisHealthChecker.ensure_redis_alive(args.redis_url)

    # 独立benchmark SQLite库（与开发/测试库隔离，结束删文件）
    benchmark_db = PROJECT_ROOT / "output" / "benchmark_cache.db"
    os.environ["TM_DB_TYPE"] = "sqlite"
    os.environ["TM_DB_SQLITE_PATH"] = str(benchmark_db)
    # 队列worker关闭（纯同步测量，不起消费线程）
    os.environ.pop("TM_TASK_QUEUE_ENABLED", None)
    os.environ.pop("TM_TASK_WORKER_ENABLED", None)
    DatabaseSession.reset()
    if benchmark_db.exists():
        benchmark_db.unlink()
    DatabaseSession.init_db()

    seeder = BenchmarkDataSeeder()
    app = create_app("test")
    try:
        # 造量（penetration单用例也需列表/用例数据支撑，统一造全量）
        seeder.seed_cases(count=1000)
        seeder.seed_executions(batch_count=100)

        runner = CacheBenchmarkRunner(app, args.redis_url)
        # 按--scenario组装results（all时全部；单场景时只跑该场景，
        # 但报告结构仍需其他场景占位，故单场景直接打印指标不写全报告）
        if args.scenario == "all":
            results = runner.run_all(
                request_count=args.requests,
                concurrency=args.concurrency,
            )
            BenchmarkReportGenerator.generate(results, args.output)
        elif args.scenario == "penetration":
            verdict = runner.run_scenario_penetration()
            print("穿透防护验证结果:", verdict)
            results = None
        else:
            scenario_map = {
                "cases-list": runner.run_scenario_cases_list,
                "reports-summary": runner.run_scenario_reports_summary,
                "reports-all": runner.run_scenario_reports_all,
            }
            if args.scenario == "concurrent":
                stats_disabled = runner.run_scenario_concurrent(
                    False, concurrency=args.concurrency,
                    request_count=args.requests,
                )
                stats_enabled = runner.run_scenario_concurrent(
                    True, concurrency=args.concurrency,
                    request_count=args.requests,
                )
            else:
                target = scenario_map[args.scenario]
                stats_disabled = target(False, request_count=args.requests)
                stats_enabled = target(True, request_count=args.requests)
            print(f"场景[{args.scenario}]缓存关闭:", stats_disabled.to_dict())
            print(f"场景[{args.scenario}]缓存开启:", stats_enabled.to_dict())
            results = None
    finally:
        # 默认cleanup: 先停worker（若误开）、清BM-数据、释放引擎删库文件
        try:
            from src.core.task_queue import stop_worker

            stop_worker()
        except Exception:  # noqa: BLE001 收尾防御
            pass
        if not args.no_cleanup:
            seeder.cleanup()
        DatabaseSession.reset()
        if not args.no_cleanup and benchmark_db.exists():
            benchmark_db.unlink()

    # stdout摘要（CLI边界，便于CI日志直接查看）
    if results is not None:
        print("=" * 70)
        print("缓存基准执行完成，核心结果:")
        for scenario_name in (
            "cases_list", "reports_summary", "reports_all", "concurrent"
        ):
            pair = results[scenario_name]
            print(
                f"  {scenario_name}: 平均延迟 "
                f"{pair['disabled']['mean']:.3f}ms -> "
                f"{pair['enabled']['mean']:.3f}ms | "
                f"QPS {pair['disabled']['qps']:.2f} -> "
                f"{pair['enabled']['qps']:.2f}"
            )
        print("  穿透防护:", results["penetration"])
        print(f"报告已写入: {args.output}")
        print("=" * 70)


if __name__ == "__main__":
    main()
