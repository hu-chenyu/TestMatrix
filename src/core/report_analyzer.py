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

规划能力（Day7-8）:
    - 统计聚合: 批次级指标计算（用例总数/通过率/耗时分布/失败明细，
      按模块/优先级分组）
    - 汇总数据写入defect_statistics表，支撑Web平台ECharts看板
    - 趋势数据生成与测试报告邮件推送（基于smtplib，依赖TM_EMAIL_*配置）
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from src.common.logger import LogManager

logger = LogManager.get_logger()

# Allure结果文件后缀（用例级结果，区别于*-container.json步骤容器）
RESULT_SUFFIX = "-result.json"

# Allure合法执行状态集合
VALID_STATUSES = ("passed", "failed", "broken", "skipped", "unknown")

# 失败类状态（failed=断言失败，broken=环境/代码异常，均需人工关注）
FAILED_STATUSES = ("failed", "broken")


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
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"结果文件不存在: {file_path}")

        with open(path, encoding="utf-8") as file_handle:
            data = json.load(file_handle)

        result = AllureResult(
            uuid=str(data.get("uuid", "")),
            name=str(data.get("name", "")),
            full_name=str(data.get("fullName", "")),
            status=str(data.get("status", "unknown")),
            description=str(data.get("description", "") or ""),
            start=ReportAnalyzer._safe_int(data.get("start")),
            stop=ReportAnalyzer._safe_int(data.get("stop")),
            history_id=str(data.get("historyId", "")),
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
            except json.JSONDecodeError as exc:
                skipped += 1
                logger.warning(
                    f"结果文件JSON解析失败已跳过 | 文件: {file_path} | {exc}"
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
