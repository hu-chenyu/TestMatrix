"""
测试报告解析与统计模块（第二阶段实现中）

已实现能力（Day6）:
    - AllureResult数据模型: dataclass映射Allure *-result.json用例级结构，
      含uuid/name/status/labels/参数化参数/失败详情等字段
    - ReportAnalyzer结果解析器:
        * scan_results_dir     扫描结果目录，返回全部*-result.json路径（排除container）
        * parse_result_file    解析单个JSON文件为AllureResult对象
        * parse_results_dir    批量解析整个目录（单个失败跳过不中断）
        * get_by_status        按状态筛选结果列表
        * get_failed_results   获取失败结果（failed+broken均视为失败）

已实现能力（Day7）:
    - 统计聚合数据模型: StatisticsResult（批次级）/ ModuleStat（模块级）/
      PriorityStat（优先级级）/ FailedCaseDetail（失败明细）四个dataclass
    - ReportStatistics统计聚合引擎:
        * aggregate            主入口，消费结果列表计算全部批次级指标
        * 耗时统计             总/平均/最大/最小/P95耗时（小样本取最大值近似）
        * 双维度分组           按模块（feature/suite/parentSuite/full_name逐级
                               fallback提取）与优先级（severity）分组统计
        * 失败明细             failed+broken用例的uuid/模块/优先级/错误信息提取
        * to_dict              统计结果转字典（便于入库与JSON序列化）

已实现能力（Day8）:
    - ReportRepository统计结果仓储:
        * save_statistics       StatisticsResult写入defect_statistics表
                               （failed剔除broken后入库，broken映射error字段）
        * get_by_execution_id   按批次号查询单条统计
        * get_latest_statistics 最近N条统计（created_at降序）
        * get_trend_data        通过率趋势数据（时间升序字典列表，
                               created_at转字符串便于ECharts消费）
        * get_pass_rate_trend   通过率浮点列表（前端折线图直接消费）
    - db模型采用函数内延迟导入，规避core与db模块循环依赖

规划能力:
    - 测试报告邮件推送（基于smtplib，依赖TM_EMAIL_*配置）
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.common.logger import LogManager

logger = LogManager.get_logger()

# Allure结果文件后缀（用例级结果，区别于*-container.json步骤容器）
RESULT_SUFFIX = "-result.json"

# Allure合法执行状态集合
VALID_STATUSES = ("passed", "failed", "broken", "skipped", "unknown")

# 失败类状态（failed=断言失败，broken=环境/代码异常，均需人工关注）
FAILED_STATUSES = ("failed", "broken")

# P95百分位计算的小样本阈值（不足该数量直接取最大值近似）
P95_MIN_SAMPLE_SIZE = 20

# 模块名/优先级提取失败时的降级默认值
UNKNOWN_LABEL = "unknown"


@dataclass
class AllureResult:
    """
    Allure用例级结果数据模型

    映射Allure *-result.json的单条用例结果结构，
    缺失字段由解析器填充默认值（status="unknown"，start/stop=0），
    保证脏数据不抛异常、链路可容错。

    字段说明:
        uuid           结果唯一标识（Allure生成）
        name           用例名（如test_login_success）
        full_name      全限定名（如tests.api_demo.TestLogin#test_login_success）
        status         执行结果: passed/failed/broken/skipped/unknown
        description    用例描述（docstring）
        start          开始时间（毫秒时间戳）
        stop           结束时间（毫秒时间戳）
        history_id     历史标识（对应historyId，同用例多次执行保持一致）
        labels         标签字典（labels数组转dict，同name的value合并为list，
                       key含severity/feature/story/tag/suite等）
        parameters     参数化参数列表（如[{"name": "case", "value": "TM-0001"}]）
        status_details 失败详情（statusDetails的message/trace，通过时为None）
    """

    uuid: str = ""
    name: str = ""
    full_name: str = ""
    status: str = "unknown"
    description: str = ""
    start: int = 0
    stop: int = 0
    history_id: str = ""
    labels: Dict[str, List[str]] = field(default_factory=dict)
    parameters: List[Dict] = field(default_factory=list)
    status_details: Optional[Dict] = None

    @property
    def duration_ms(self) -> int:
        """
        用例执行耗时（毫秒）

        参数:
            无

        返回:
            int: stop - start；时间戳缺失或异常时返回0

        异常:
            无
        """
        return max(self.stop - self.start, 0)

    def get_label(self, name: str) -> List[str]:
        """
        按标签名获取标签值列表（便捷方法）

        参数:
            name (str): 标签名（如severity/feature/story/tag/suite）

        返回:
            List[str]: 该标签名下的全部值；标签不存在时返回空列表

        异常:
            无
        """
        return self.labels.get(name, [])


class ReportAnalyzer:
    """
    Allure结果解析器

    负责将Allure生成的 *-result.json 文件解析为AllureResult对象，
    供后续统计聚合与入库消费。解析层做到容错:
    目录不存在抛FileNotFoundError，单个JSON损坏仅warning并跳过，
    字段缺失填默认值，绝不因单文件脏数据中断整批解析。
    """

    @staticmethod
    def scan_results_dir(results_dir: Union[str, Path]) -> List[str]:
        """
        扫描Allure结果目录，返回全部用例级结果文件路径

        参数:
            results_dir (str | Path): Allure结果目录（如output/allure_results）

        返回:
            List[str]: 全部 *-result.json 文件的绝对路径列表
                      （自动排除 *-container.json 步骤容器文件），
                      目录为空时返回空列表

        异常:
            FileNotFoundError: 目录不存在时抛出
        """
        directory = Path(results_dir)
        if not directory.is_dir():
            raise FileNotFoundError(f"Allure结果目录不存在: {results_dir}")

        result_files = sorted(
            str(file_path.resolve())
            for file_path in directory.glob(f"*{RESULT_SUFFIX}")
            if file_path.is_file()
        )
        logger.info(
            f"Allure结果目录扫描完成 | 目录: {directory} | "
            f"用例级结果文件: {len(result_files)}个"
        )
        return result_files

    @staticmethod
    def parse_result_file(file_path: Union[str, Path]) -> AllureResult:
        """
        解析单个 *-result.json 文件为AllureResult对象

        解析规则（缺失字段容错，不抛异常）:
            - uuid/name/full_name/status/description/history_id缺失时填空串，
              其中status缺失填"unknown"
            - start/stop缺失或非数值时填0
            - labels数组转Dict[str, List[str]]（同name的value合并为list）
            - parameters缺失时为空列表，statusDetails缺失时为None

        参数:
            file_path (str | Path): 单个 *-result.json 文件路径

        返回:
            AllureResult: 解析后的用例级结果对象

        异常:
            FileNotFoundError: 文件不存在时抛出
            json.JSONDecodeError: JSON语法非法时抛出（批量解析层捕获跳过）
            ValueError: 合法JSON但顶层非对象（dict）时抛出（批量解析层捕获跳过）
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"结果文件不存在: {file_path}")

        with open(path, encoding="utf-8") as file_handle:
            data = json.load(file_handle)

        # 顶层必须是JSON对象（dict）；合法JSON但顶层为列表/字符串/数字时
        # 后续data.get会抛AttributeError，此处统一抛ValueError，
        # 由批量解析层按"单文件损坏跳过"策略捕获
        if not isinstance(data, dict):
            raise ValueError(
                f"结果文件顶层结构必须是JSON对象，实际为: {type(data).__name__}"
            )

        # 字符串字段统一用 `or 默认值` 兜底：字段显式为null时
        # data.get会取到None，str(None)会得到字符串"None"而非预期默认值
        result = AllureResult(
            uuid=str(data.get("uuid") or ""),
            name=str(data.get("name") or ""),
            full_name=str(data.get("fullName") or ""),
            status=str(data.get("status") or "unknown"),
            description=str(data.get("description") or ""),
            start=ReportAnalyzer._safe_int(data.get("start")),
            stop=ReportAnalyzer._safe_int(data.get("stop")),
            history_id=str(data.get("historyId") or ""),
            labels=ReportAnalyzer._merge_labels(data.get("labels", [])),
            parameters=list(data.get("parameters", []) or []),
            status_details=data.get("statusDetails") or None,
        )
        logger.debug(
            f"结果文件解析成功 | {path.name} | 用例: {result.name} | "
            f"状态: {result.status} | 耗时: {result.duration_ms}ms"
        )
        return result

    @staticmethod
    def parse_results_dir(results_dir: Union[str, Path]) -> List[AllureResult]:
        """
        批量解析整个Allure结果目录

        单个文件JSON语法非法时记录warning日志并跳过，
        不中断整体解析（保证批量解析的健壮性）。

        参数:
            results_dir (str | Path): Allure结果目录

        返回:
            List[AllureResult]: 全部解析成功的用例级结果对象列表

        异常:
            FileNotFoundError: 目录不存在时抛出（由scan_results_dir透传）
        """
        result_files = ReportAnalyzer.scan_results_dir(results_dir)

        results: List[AllureResult] = []
        skipped = 0
        for file_path in result_files:
            try:
                results.append(ReportAnalyzer.parse_result_file(file_path))
            except (
                json.JSONDecodeError,  # JSON语法非法
                ValueError,             # 顶层结构非JSON对象
                AttributeError,         # 字段形态异常
                TypeError,              # 字段类型异常
                KeyError,               # 必需键缺失
            ) as exc:
                # 单文件任何形态损坏都只warning跳过，保证整批解析不中断
                skipped += 1
                logger.warning(
                    f"结果文件解析失败已跳过 | 文件: {file_path} | "
                    f"{type(exc).__name__}: {exc}"
                )

        logger.info(
            f"Allure结果目录批量解析完成 | 目录: {results_dir} | "
            f"成功: {len(results)}个 | 跳过(损坏): {skipped}个"
        )
        return results

    @staticmethod
    def get_by_status(results: List[AllureResult], status: str) -> List[AllureResult]:
        """
        按执行状态筛选结果列表

        参数:
            results (List[AllureResult]): 待筛选的结果对象列表
            status (str): 目标状态（passed/failed/broken/skipped/unknown）

        返回:
            List[AllureResult]: 状态匹配的结果子列表（保持原顺序）

        异常:
            无
        """
        matched = [result for result in results if result.status == status]
        logger.info(f"按状态筛选完成 | 状态: {status} | 命中: {len(matched)}条")
        return matched

    @staticmethod
    def get_failed_results(results: List[AllureResult]) -> List[AllureResult]:
        """
        获取全部失败结果（failed+broken均视为失败）

        failed为断言失败（功能缺陷疑似），broken为环境/代码异常，
        两者均需人工关注，统一归入失败集合。

        参数:
            results (List[AllureResult]): 待筛选的结果对象列表

        返回:
            List[AllureResult]: 状态为failed或broken的结果子列表（保持原顺序）

        异常:
            无
        """
        failed = [
            result for result in results if result.status in FAILED_STATUSES
        ]
        logger.info(
            f"失败结果筛选完成 | 失败(failed+broken): {len(failed)}条 | "
            f"输入总数: {len(results)}"
        )
        return failed

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_labels(raw_labels: Union[list, None]) -> Dict[str, List[str]]:
        """
        labels数组转标签字典（内部方法）

        转换规则:
            [{"name": "tag", "value": "api"}, {"name": "tag", "value": "smoke"}]
            转为 {"tag": ["api", "smoke"]}（同name的value合并为list）

        参数:
            raw_labels (list | None): 原始labels数组（元素为name/value字典）

        返回:
            Dict[str, List[str]]: 标签名字典；入参为空/非法时返回空字典

        异常:
            无
        """
        merged: Dict[str, List[str]] = {}
        if not isinstance(raw_labels, list):
            return merged
        for item in raw_labels:
            if not isinstance(item, dict):
                continue
            label_name = item.get("name")
            label_value = item.get("value")
            if not label_name or label_value is None:
                continue
            merged.setdefault(str(label_name), []).append(str(label_value))
        return merged

    @staticmethod
    def _safe_int(value) -> int:
        """
        安全整数转换（内部方法）

        参数:
            value (Any): 待转换值（毫秒时间戳，可能缺失或类型异常）

        返回:
            int: 转换成功返回整数值；None/非法类型/转换异常时返回0

        异常:
            无
        """
        if value is None or isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


# ======================================================================
# 统计聚合数据模型（Day7）
# ======================================================================
@dataclass
class ModuleStat:
    """
    模块级统计数据模型

    单个业务模块（feature维度）的执行统计。

    字段说明:
        name           模块名（提取自feature/suite/parentSuite/full_name）
        total          该模块用例总数
        passed         该模块通过数
        failed         该模块失败数（failed+broken合计）
        pass_rate      该模块通过率（0.0-1.0，4位小数；total=0时0.0）
        avg_duration_ms 该模块平均耗时（毫秒，2位小数；total=0时0.0）
    """

    name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0
    avg_duration_ms: float = 0.0


@dataclass
class PriorityStat:
    """
    优先级级统计数据模型

    单个优先级（severity维度：blocker/critical/normal/minor/trivial）的执行统计。

    字段说明:
        name       优先级名（提取自labels的severity）
        total      该优先级用例总数
        passed     该优先级通过数
        failed     该优先级失败数（failed+broken合计）
        pass_rate  该优先级通过率（0.0-1.0，4位小数；total=0时0.0）
    """

    name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0


@dataclass
class FailedCaseDetail:
    """
    失败用例明细数据模型

    单条失败用例（failed/broken）的定位与错误信息，
    供失败分析与通知推送消费。

    字段说明:
        uuid          结果唯一标识
        name          用例名
        full_name     全限定名
        status        失败状态（failed或broken）
        duration_ms   执行耗时（毫秒）
        module        所属模块（提取不到填"unknown"）
        priority      优先级（提取不到填"unknown"）
        error_message 错误信息（statusDetails的message，缺省空串）
        error_trace   错误堆栈（statusDetails的trace，缺省空串）
    """

    uuid: str
    name: str
    full_name: str
    status: str
    duration_ms: int = 0
    module: str = UNKNOWN_LABEL
    priority: str = UNKNOWN_LABEL
    error_message: str = ""
    error_trace: str = ""
    owner: str = ""


@dataclass
class StatisticsResult:
    """
    批次级统计结果数据模型

    一次测试批次（或一次聚合分析）的全量指标，
    供入库（defect_statistics）、Web看板与通知推送消费。

    字段说明:
        total             用例总数
        passed            通过数（仅status=="passed"）
        failed            失败数（failed+broken合计）
        broken            中断数（broken，环境/代码异常）
        skipped           跳过数（计入total但不计入通过/失败）
        pass_rate         通过率（0.0-1.0，4位小数；total=0时0.0）
        total_duration_ms 全部用例耗时总和（毫秒）
        avg_duration_ms   平均耗时（毫秒，2位小数；total=0时0.0）
        max_duration_ms   最大耗时（毫秒；total=0时0）
        min_duration_ms   最小耗时（毫秒；total=0时0）
        p95_duration_ms   P95耗时（毫秒，2位小数；不足20条取最大值近似；
                          total=0时0.0）
        by_module         按模块分组统计（key=模块名）
        by_priority       按优先级分组统计（key=优先级名）
        failed_details    失败用例明细列表
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    broken: int = 0
    skipped: int = 0
    pass_rate: float = 0.0
    total_duration_ms: int = 0
    avg_duration_ms: float = 0.0
    max_duration_ms: int = 0
    min_duration_ms: int = 0
    p95_duration_ms: float = 0.0
    by_module: Dict[str, ModuleStat] = field(default_factory=dict)
    by_priority: Dict[str, PriorityStat] = field(default_factory=dict)
    failed_details: List[FailedCaseDetail] = field(default_factory=list)


class ReportStatistics:
    """
    批次级统计聚合引擎

    消费List[AllureResult]计算批次级指标:
    状态计数（通过率）、耗时分布（总/均/极值/P95）、
    双维度分组（模块/优先级）、失败明细提取。

    容错设计:
        - 输入空列表返回全零StatisticsResult，不抛异常
        - 单条result字段异常（如耗时非数值）用0兜底，不中断整体统计
        - 模块名/优先级提取失败warning日志并降级为"unknown"
    """

    @staticmethod
    def aggregate(results: List[AllureResult]) -> StatisticsResult:
        """
        统计聚合主入口（计算全部批次级指标）

        参数:
            results (List[AllureResult]): 用例级结果对象列表（通常来自
                                          ReportAnalyzer.parse_results_dir）

        返回:
            StatisticsResult: 批次级统计结果（空输入返回全零结果）

        异常:
            无（内部全部容错，绝不因单条脏数据中断）
        """
        stat = StatisticsResult()
        if not results:
            logger.info("统计聚合输入为空列表，返回全零统计结果")
            return stat

        # 1. 状态计数聚合
        stat.total = len(results)
        stat.passed = sum(1 for r in results if r.status == "passed")
        stat.failed = sum(1 for r in results if r.status in FAILED_STATUSES)
        stat.broken = sum(1 for r in results if r.status == "broken")
        stat.skipped = sum(1 for r in results if r.status == "skipped")
        stat.pass_rate = ReportStatistics._calc_pass_rate(
            stat.passed, stat.total
        )

        # 2. 耗时分布统计
        duration_stats = ReportStatistics._calc_duration_stats(results)
        stat.total_duration_ms = duration_stats["total"]
        stat.avg_duration_ms = duration_stats["avg"]
        stat.max_duration_ms = duration_stats["max"]
        stat.min_duration_ms = duration_stats["min"]
        stat.p95_duration_ms = duration_stats["p95"]

        # 3. 双维度分组统计
        stat.by_module = ReportStatistics._group_by_module(results)
        stat.by_priority = ReportStatistics._group_by_priority(results)

        # 4. 失败明细提取
        stat.failed_details = ReportStatistics._extract_failed_details(results)

        logger.info(
            f"统计聚合完成 | 总数: {stat.total} | 通过: {stat.passed} | "
            f"失败: {stat.failed}（含broken {stat.broken}）| 跳过: {stat.skipped} | "
            f"通过率: {stat.pass_rate:.2%} | 总耗时: {stat.total_duration_ms}ms | "
            f"平均: {stat.avg_duration_ms:.2f}ms | P95: {stat.p95_duration_ms:.2f}ms"
        )
        return stat

    @staticmethod
    def to_dict(stat: StatisticsResult) -> Dict[str, Any]:
        """
        StatisticsResult转字典（便于入库与JSON序列化）

        嵌套的ModuleStat/PriorityStat/FailedCaseDetail同步转字典，
        转换后可直接json.dumps序列化。

        参数:
            stat (StatisticsResult): 批次级统计结果对象

        返回:
            Dict[str, Any]: 全字段平铺展开的字典

        异常:
            无
        """
        return {
            "total": stat.total,
            "passed": stat.passed,
            "failed": stat.failed,
            "broken": stat.broken,
            "skipped": stat.skipped,
            "pass_rate": stat.pass_rate,
            "total_duration_ms": stat.total_duration_ms,
            "avg_duration_ms": stat.avg_duration_ms,
            "max_duration_ms": stat.max_duration_ms,
            "min_duration_ms": stat.min_duration_ms,
            "p95_duration_ms": stat.p95_duration_ms,
            "by_module": {
                name: {
                    "name": module_stat.name,
                    "total": module_stat.total,
                    "passed": module_stat.passed,
                    "failed": module_stat.failed,
                    "pass_rate": module_stat.pass_rate,
                    "avg_duration_ms": module_stat.avg_duration_ms,
                }
                for name, module_stat in stat.by_module.items()
            },
            "by_priority": {
                name: {
                    "name": priority_stat.name,
                    "total": priority_stat.total,
                    "passed": priority_stat.passed,
                    "failed": priority_stat.failed,
                    "pass_rate": priority_stat.pass_rate,
                }
                for name, priority_stat in stat.by_priority.items()
            },
            "failed_details": [
                {
                    "uuid": detail.uuid,
                    "name": detail.name,
                    "full_name": detail.full_name,
                    "status": detail.status,
                    "duration_ms": detail.duration_ms,
                    "module": detail.module,
                    "priority": detail.priority,
                    "error_message": detail.error_message,
                    "error_trace": detail.error_trace,
                    "owner": detail.owner,
                }
                for detail in stat.failed_details
            ],
        }

    # ------------------------------------------------------------------
    # 内部计算方法
    # ------------------------------------------------------------------
    @staticmethod
    def _calc_pass_rate(passed: int, total: int) -> float:
        """
        计算通过率（内部方法）

        参数:
            passed (int): 通过数
            total (int): 总数

        返回:
            float: passed/total保留4位小数；total=0时返回0.0

        异常:
            无
        """
        if total <= 0:
            return 0.0
        return round(passed / total, 4)

    @staticmethod
    def _calc_duration_stats(results: List[AllureResult]) -> Dict[str, Any]:
        """
        计算耗时分布统计（内部方法）

        P95计算规则:
            全部用例duration_ms升序排序，索引=ceil(0.95*n)-1（1-based转0-based）；
            不足20条时直接取最大值近似（样本过少百分位无统计意义）。

        参数:
            results (List[AllureResult]): 用例级结果对象列表

        返回:
            Dict[str, Any]: {"total", "avg", "max", "min", "p95"}耗时指标字典；
                            空列表时全部为0

        异常:
            无（duration_ms属性异常值已由AllureResult.duration_ms兜底为非负）
        """
        if not results:
            return {"total": 0, "avg": 0.0, "max": 0, "min": 0, "p95": 0.0}

        durations = [r.duration_ms for r in results]
        total = sum(durations)
        count = len(durations)
        sorted_durations = sorted(durations)

        # P95: 大样本取百分位，小样本（<20条）取最大值近似
        if count < P95_MIN_SAMPLE_SIZE:
            p95 = float(sorted_durations[-1])
        else:
            p95_index = math.ceil(0.95 * count) - 1
            p95 = float(sorted_durations[p95_index])

        return {
            "total": total,
            "avg": round(total / count, 2),
            "max": sorted_durations[-1],
            "min": sorted_durations[0],
            "p95": round(p95, 2),
        }

    @staticmethod
    def _group_by_module(results: List[AllureResult]) -> Dict[str, ModuleStat]:
        """
        按模块分组统计（内部方法）

        参数:
            results (List[AllureResult]): 用例级结果对象列表

        返回:
            Dict[str, ModuleStat]: key=模块名，value=该模块统计；
                                   空输入返回空字典

        异常:
            无
        """
        grouped: Dict[str, List[AllureResult]] = {}
        for result in results:
            module_name = ReportStatistics._extract_module(result)
            grouped.setdefault(module_name, []).append(result)

        stats: Dict[str, ModuleStat] = {}
        for module_name, module_results in grouped.items():
            passed = sum(1 for r in module_results if r.status == "passed")
            failed = sum(1 for r in module_results if r.status in FAILED_STATUSES)
            total_duration = sum(r.duration_ms for r in module_results)
            stats[module_name] = ModuleStat(
                name=module_name,
                total=len(module_results),
                passed=passed,
                failed=failed,
                pass_rate=ReportStatistics._calc_pass_rate(
                    passed, len(module_results)
                ),
                avg_duration_ms=round(
                    total_duration / len(module_results), 2
                ),
            )
            logger.debug(
                f"模块分组统计 | 模块: {module_name} | 总数: {len(module_results)} | "
                f"通过: {passed} | 失败: {failed} | 通过率: {stats[module_name].pass_rate:.2%}"
            )
        return stats

    @staticmethod
    def _group_by_priority(results: List[AllureResult]) -> Dict[str, PriorityStat]:
        """
        按优先级分组统计（内部方法）

        参数:
            results (List[AllureResult]): 用例级结果对象列表

        返回:
            Dict[str, PriorityStat]: key=优先级名，value=该优先级统计；
                                     空输入返回空字典

        异常:
            无
        """
        grouped: Dict[str, List[AllureResult]] = {}
        for result in results:
            priority_name = ReportStatistics._extract_priority(result)
            grouped.setdefault(priority_name, []).append(result)

        stats: Dict[str, PriorityStat] = {}
        for priority_name, priority_results in grouped.items():
            passed = sum(1 for r in priority_results if r.status == "passed")
            failed = sum(1 for r in priority_results if r.status in FAILED_STATUSES)
            stats[priority_name] = PriorityStat(
                name=priority_name,
                total=len(priority_results),
                passed=passed,
                failed=failed,
                pass_rate=ReportStatistics._calc_pass_rate(
                    passed, len(priority_results)
                ),
            )
            logger.debug(
                f"优先级分组统计 | 优先级: {priority_name} | 总数: {len(priority_results)} | "
                f"通过: {passed} | 失败: {failed} | "
                f"通过率: {stats[priority_name].pass_rate:.2%}"
            )
        return stats

    @staticmethod
    def _extract_failed_details(
        results: List[AllureResult],
    ) -> List[FailedCaseDetail]:
        """
        提取失败用例明细（内部方法）

        参数:
            results (List[AllureResult]): 用例级结果对象列表

        返回:
            List[FailedCaseDetail]: failed+broken用例的明细列表（保持原顺序）；
                                    无失败时返回空列表

        异常:
            无（status_details字段异常时以空串兜底）
        """
        details: List[FailedCaseDetail] = []
        for result in results:
            if result.status not in FAILED_STATUSES:
                continue
            status_details = result.status_details or {}
            if not isinstance(status_details, dict):
                status_details = {}
            # 负责人: labels的"owner"第一个值（通知@人消费），无则空串
            owner_values = result.get_label("owner")
            details.append(
                FailedCaseDetail(
                    uuid=result.uuid,
                    name=result.name,
                    full_name=result.full_name,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    module=ReportStatistics._extract_module(result),
                    priority=ReportStatistics._extract_priority(result),
                    error_message=str(status_details.get("message", "") or ""),
                    error_trace=str(status_details.get("trace", "") or ""),
                    owner=owner_values[0] if owner_values else "",
                )
            )
        return details

    # ------------------------------------------------------------------
    # 标签提取方法
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_module(result: AllureResult) -> str:
        """
        提取模块名（内部方法，按优先级fallback）

        提取链（依次尝试，取第一个命中）:
            1. labels的"feature"第一个值
            2. labels的"suite"第一个值
            3. labels的"parentSuite"第一个值
            4. full_name按"#"分割取前半部分，再按"."分割取倒数第二段
               （类名所在模块，如tests.api_demo.test_login.TestX#test_y
               取"test_login"）
            5. 都没有→"unknown"（含warning日志）

        参数:
            result (AllureResult): 用例级结果对象

        返回:
            str: 模块名

        异常:
            无
        """
        for label_name in ("feature", "suite", "parentSuite"):
            values = result.get_label(label_name)
            if values:
                return values[0]

        # full_name倒数第二段（类名所在模块）
        if result.full_name and "#" in result.full_name:
            class_path = result.full_name.split("#", 1)[0]
            path_parts = [part for part in class_path.split(".") if part]
            if len(path_parts) >= 2:
                return path_parts[-2]

        logger.warning(f"模块名提取失败已降级unknown | 用例: {result.name}")
        return UNKNOWN_LABEL

    @staticmethod
    def _extract_priority(result: AllureResult) -> str:
        """
        提取优先级（内部方法）

        提取规则:
            labels的"severity"第一个值（Allure标准值:
            blocker/critical/normal/minor/trivial）；没有→"unknown"。

        参数:
            result (AllureResult): 用例级结果对象

        返回:
            str: 优先级名

        异常:
            无
        """
        values = result.get_label("severity")
        if values:
            return values[0]
        logger.warning(f"优先级提取失败已降级unknown | 用例: {result.name}")
        return UNKNOWN_LABEL


# ======================================================================
# 统计结果仓储（Day8）
# ======================================================================
class ReportRepository:
    """
    统计结果数据仓储

    负责StatisticsResult持久化到defect_statistics表，
    以及基于历史批次数据的通过率趋势查询，
    供Web平台看板与通知推送消费。

    设计说明:
        - db层模型采用函数内延迟导入，规避core与db模块循环依赖
        - execution_id重复时由数据库unique约束抛IntegrityError，
          不做静默更新（批次统计唯一性由调用方保证）
    """

    @staticmethod
    def save_statistics(
        stat: StatisticsResult,
        execution_id: str,
        remark: str = "",
    ):
        """
        将StatisticsResult写入defect_statistics表

        字段映射规则:
            - total_cases = stat.total
            - passed      = stat.passed
            - failed      = stat.failed - stat.broken
              （stat.failed是failed+broken合计，剔除broken得到纯断言失败数）
            - error       = stat.broken（Allure的broken=环境/代码异常，
              对应表内error字段）
            - skipped     = stat.skipped
            - pass_rate   = stat.pass_rate
            - remark      = 入参remark（可传入耗时/分组/失败明细JSON扩展数据）

        参数:
            stat (StatisticsResult): 批次级统计结果对象
            execution_id (str): 执行批次号（AllureResult不含批次号，由调用方传入）
            remark (str): 备注信息（如to_dict的JSON扩展数据），默认空串

        返回:
            DefectStatistic: 入库后的ORM对象（含数据库生成的id与created_at）

        异常:
            ValueError: execution_id为空时抛出
            sqlalchemy.exc.IntegrityError: execution_id重复（unique约束）时
                                           由数据库抛出并记录error日志
            sqlalchemy.exc.SQLAlchemyError: 其他数据库异常时向上抛出
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import DefectStatistic

        if not execution_id or not str(execution_id).strip():
            raise ValueError("执行批次号不能为空")

        record = DefectStatistic(
            execution_id=execution_id,
            total_cases=stat.total,
            passed=stat.passed,
            failed=stat.failed - stat.broken,  # 剔除broken得到纯failed
            error=stat.broken,  # broken映射error（环境/代码异常）
            skipped=stat.skipped,
            pass_rate=stat.pass_rate,
            remark=remark,
        )
        try:
            with DatabaseSession.session_scope() as session:
                session.add(record)
                session.flush()  # 立即写库以获取自增id与created_at
                saved_id = record.id
                logger.info(
                    f"统计结果入库成功 | 批次: {execution_id} | "
                    f"总数: {stat.total} | 通过: {stat.passed} | "
                    f"失败: {record.failed} | 错误: {record.error} | "
                    f"通过率: {stat.pass_rate:.2%} | 记录ID: {saved_id}"
                )
                return record
        except Exception as exc:
            logger.error(
                f"统计结果入库失败 | 批次: {execution_id} | "
                f"异常: {type(exc).__name__}: {exc}"
            )
            raise

    @staticmethod
    def get_by_execution_id(execution_id: str):
        """
        按批次号查询单条统计记录

        参数:
            execution_id (str): 执行批次号

        返回:
            DefectStatistic | None: 统计记录ORM对象；不存在返回None

        异常:
            无（查询异常由session_scope记录日志后向上抛出）
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import DefectStatistic

        session = DatabaseSession.get_session()
        try:
            record = (
                session.query(DefectStatistic)
                .filter_by(execution_id=execution_id)
                .first()
            )
            if record is None:
                logger.debug(f"按批次号查询无记录 | 批次: {execution_id}")
            return record
        finally:
            session.close()

    @staticmethod
    def get_latest_statistics(limit: int = 10) -> List:
        """
        查询最近N条统计记录

        参数:
            limit (int): 返回条数上限，默认10

        返回:
            List[DefectStatistic]: 统计记录列表（created_at降序，最新在前）；
                                   空表返回空列表

        异常:
            无（查询异常由session记录日志后向上抛出）
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import DefectStatistic

        session = DatabaseSession.get_session()
        try:
            records = (
                session.query(DefectStatistic)
                .order_by(DefectStatistic.created_at.desc(), DefectStatistic.id.desc())
                .limit(limit)
                .all()
            )
            if not records:
                logger.debug(f"最近统计查询为空 | limit: {limit}")
            return records
        finally:
            session.close()

    @staticmethod
    def get_trend_data(limit: int = 20) -> List[Dict[str, Any]]:
        """
        生成通过率趋势数据（时间从早到晚）

        查询最近N条统计（created_at降序）后反转为升序返回，
        created_at转为ISO格式字符串，便于JSON序列化与ECharts消费。

        参数:
            limit (int): 返回条数上限，默认20

        返回:
            List[Dict[str, Any]]: 趋势字典列表（时间升序），格式:
                [{"execution_id", "pass_rate", "total_cases", "passed",
                  "failed", "error", "created_at"}, ...]
                空表返回空列表

        异常:
            无
        """
        records = ReportRepository.get_latest_statistics(limit=limit)
        # 降序查询后反转为时间升序（趋势图从左到右时间递增）
        records.reverse()
        return [
            {
                "execution_id": record.execution_id,
                "pass_rate": record.pass_rate,
                "total_cases": record.total_cases,
                "passed": record.passed,
                "failed": record.failed,
                "error": record.error,
                "created_at": record.created_at.isoformat()
                if record.created_at
                else "",
            }
            for record in records
        ]

    @staticmethod
    def get_pass_rate_trend(limit: int = 20) -> List[float]:
        """
        获取通过率浮点数列表（时间升序）

        便捷方法，供前端折线图直接消费。

        参数:
            limit (int): 返回条数上限，默认20

        返回:
            List[float]: 通过率列表（时间从早到晚）；空表返回空列表

        异常:
            无
        """
        return [
            item["pass_rate"] for item in ReportRepository.get_trend_data(limit=limit)
        ]
