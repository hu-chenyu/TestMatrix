"""
AST 精准回归最小 POC 脚本（Day45）

定位:
    为 Day94-95「基于覆盖率映射 + AST import 拓扑的精准回归用例选择」
    工程化打地基，验证三件事在真实数据上成立:
        (a) 解析 coverage.py 的 .coverage（SQLite）建立「用例与源码文件」映射
        (b) 用标准库 ast 解析 src/ 的 import 关系建立反向影响面
        (c) 对若干代表性 src 文件输出「选中比例 / 漏检率」双指标

只读工具: 不改动 src/ 任何一行，也不动 .coverage 本体（sqlite3 只读打开）。

关键前置条件（Day45 实测踩坑，务必先读）:
    .coverage 默认**没有**每用例上下文。pytest-cov 4.x 的
    `--cov-context` 默认不开启，必须显式加 `--cov-context=test`，
    否则 context 表里只有一行空串（收集期/导入期覆盖），
    「用例与文件」映射根本无从谈起。生成命令:
        py -m pytest --cov=src --cov-context=test --cov-report= -q
    开启后 context 名形如:
        tests/api_demo/test_x.py::TestC::test_y[参数]|setup
        tests/api_demo/test_x.py::TestC::test_y[参数]|run
        tests/api_demo/test_x.py::TestC::test_y[参数]|teardown
    因此: ① 需按 `|phase` 拆名还原 node id；② 默认只取 `|run`
    阶段（setup/teardown 几乎每条用例都覆盖 conftest 与框架代码，
    计入会让映射退化成"全量"，精准选择的意义归零）。

命令行用法（PowerShell）:
    py -m scripts.ast_regression_poc --help
    py -m scripts.ast_regression_poc                       # 默认 5 个代表文件
    py -m scripts.ast_regression_poc --output json
    py -m scripts.ast_regression_poc --modified-files src/core/executors.py
    py -m scripts.ast_regression_poc --phase all          # 含 setup/teardown 对比
    py -m scripts.ast_regression_poc --stats-only          # 只看映射表/依赖图统计

运行方式兼容:
    同时支持 `py -m scripts.ast_regression_poc` 与直接
    `py scripts/ast_regression_poc.py`；后者 sys.path 默认只含
    scripts/ 目录，故先插入项目根再导入 src.*
"""

import argparse
import ast
import json
import sqlite3
import sys
from collections import deque
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.logger import LogManager  # noqa: E402

logger = LogManager.get_logger()

# .coverage 默认位置候选（coverage.py 的默认 data_file 就是项目根 .coverage）
DEFAULT_COVERAGE_CANDIDATES = (".coverage", "output/.coverage")

# 默认源码目录（相对项目根）
DEFAULT_SRC_DIR = "src"

# coverage 上下文的阶段分隔符：context 名形如 "<node id>|run"
PHASE_SEPARATOR = "|"

# 默认只统计 run 阶段（见模块 docstring「关键前置条件」）
DEFAULT_PHASE = "run"

# 覆盖采集期/导入期使用的空上下文（非测试用例，须排除在用例外）
EMPTY_CONTEXT = ""

# 代表性实验文件（覆盖「大文件核心 / 主题模块 / Web 入口 / 边缘配置 / 叶子工具」
# 五种形态，Day45 验收要求 5 个文件；缺任一文件时按自动规则补位）
DEFAULT_REPRESENTATIVE_FILES = (
    "src/core/case_manager.py",       # 最大文件 + 核心域，直接测试密集
    "src/core/executors.py",          # 执行器（当日主题模块）
    "src/web/routes/executions.py",   # Web 触发入口，import 入度最高
    "src/web/config.py",              # 边缘配置模块，直接测试少
    "src/common/time_utils.py",       # 最小工具文件（依赖图叶子）
)

# 覆盖率数据库中必须存在的表（缺一即判定 schema 不受支持）
REQUIRED_TABLES = ("file", "context", "line_bits")


def normalize_rel_path(path: str | Path, project_root: Path) -> str:
    """
    把任意形态的路径归一为「相对项目根 + 正斜杠」形式

    归一化的必要性（两处来源形态不同）:
        - .coverage 的 file.path 是**绝对路径**，且 Windows 下是反斜杠
        - git diff / 用户输入的修改文件是**相对路径 + 正斜杠**
    两边不归一就永远匹配不上，选中集合会恒为空。

    参数:
        path (str | Path): 待归一的路径
        project_root (Path): 项目根目录

    返回:
        str: 相对项目根的 POSIX 风格路径；不在项目根内时返回原字符串
    """
    raw = str(path)
    try:
        absolute = Path(raw)
        if not absolute.is_absolute():
            absolute = (project_root / absolute).resolve()
        return absolute.resolve().relative_to(project_root).as_posix()
    except (ValueError, OSError):
        # ValueError: 不在 project_root 之下（如 site-packages 里的文件）
        # OSError: Windows 上跨盘符 resolve 失败
        return raw.replace("\\", "/")


def is_out_of_project(relative_path: str) -> bool:
    """
    判断归一后的路径是否落在项目根之外（库外文件）

    **为什么不能用「首段含冒号」判盘符**（Day45 CI Run #56 实测教训）：
    初版写的是 `":" in path.split("/")[0]`，这在 Windows 上成立
    （首段是 `C:`），在 Linux 上恒为假——`/tmp/.../mod.py` 的
    `split("/")[0]` 是**空串**，于是 site-packages/标准库文件被当成
    项目内文件收进反向索引。单测在 Windows 上全绿，CI 的
    ubuntu-24.04 上变红：判据写死了平台，而 CI 跑的是另一个平台。

    修法不是"换成 `Path.is_absolute()`"（那仍依赖**运行**平台，
    导致单测在本机无法证伪——变异退回旧判据本地依然全绿），
    而是**按两种路径形态各自判断绝对性**：工具无论在 Windows 还是
    Linux 上跑，都既认盘符路径也认 POSIX 绝对路径。
    这样判据本身不含平台假设，单测在任意平台都能钉住它。

    参数:
        relative_path (str): normalize_rel_path 归一后的路径

    返回:
        bool: True 表示库外（两种形态任一为绝对路径 / 以 .. 上跳 / 空串）
    """
    if not relative_path:
        return True
    if relative_path.startswith(".."):
        return True
    return (
        PureWindowsPath(relative_path).is_absolute()
        or PurePosixPath(relative_path).is_absolute()
    )


def lines_to_numbits(lines: list[int]) -> bytes:
    """
    把行号列表编码为 coverage.py 的 numbits 位图（numbits_to_lines 的逆运算）

    存在的理由不是 POC 运行需要，而是让位图格式本身可被**双向验证**：
    单元测试用它造 .coverage 夹具、再用解码器读回同一批行号，
    格式若理解错一个 bit，往返断言立刻变红——单向解码的测试
    无法区分"解码正确"与"编码与解码一起理解错"。

    参数:
        lines (list[int]): 行号列表（可无序，内部去重）

    返回:
        bytes: numbits 位图字节串
    """
    if not lines:
        return b""
    highest = max(lines)
    buffer = bytearray(highest // 8 + 1)
    for line in set(lines):
        buffer[line // 8] |= 1 << (line % 8)
    return bytes(buffer)


def numbits_to_lines(blob: bytes) -> list[int]:
    """
    解码 coverage.py 的 numbits 位图，得到被执行过的行号列表

    格式（与 coverage.numbits.numbits_to_nums 语义一致）:
        第 n 行 → 第 n//8 字节的第 n%8 位；该位为 1 表示该行被执行。
    这里自带实现而不是 `from coverage.numbits import ...`:
        1) 单元测试可不装 coverage 也能验证解码逻辑（自包含）
        2) POC 不引入对 coverage 内部 API 的硬依赖

    参数:
        blob (bytes): line_bits.numbits 原始字节

    返回:
        list[int]: 升序行号列表；空 blob 返回空列表
    """
    lines: list[int] = []
    for byte_index, byte_value in enumerate(blob):
        if not byte_value:
            continue
        for bit_index in range(8):
            if byte_value & (1 << bit_index):
                lines.append(byte_index * 8 + bit_index)
    return lines


class CoverageMapper:
    """
    .coverage 解析器：用例与源码文件双向映射

    职责边界（只做解析与查询，不做任何选择决策）:
        - 读 coverage.py 的 SQLite 存储（file/context/line_bits 三表）
        - 拆 context 名的 `|phase` 后缀，还原 pytest node id
        - 双向索引：test_id → 覆盖文件集合 / 文件 → 覆盖它的 test_id 集合
    选择策略属 RegressionSelector，两者不混（同 6.12 统计查询收敛仓储的口径）。
    """

    def __init__(
        self,
        coverage_path: str | Path,
        project_root: Path | None = None,
        phase: str = DEFAULT_PHASE,
    ) -> None:
        """
        初始化映射表解析器

        参数:
            coverage_path (str | Path): .coverage 文件路径
            project_root (Path | None): 项目根目录（None 时取脚本上级目录）
            phase (str): 统计哪个执行阶段的覆盖，"run"（默认）/
                         "setup"/"teardown" 单阶段，或 "all" 全阶段合并
                         ——setup/teardown 覆盖 conftest 等共享代码，
                         单列对比可见其对选中面的放大效应

        返回:
            None

        异常:
            无（文件不存在/表缺失在 load() 内抛，见 load 的异常说明）
        """
        self.coverage_path = Path(coverage_path)
        self.project_root = project_root or PROJECT_ROOT
        self.phase = phase
        # node id → 该用例执行过的项目内源码文件（相对 posix 路径）
        self._files_by_test: dict[str, set[str]] = {}
        # 源码文件（相对 posix 路径）→ 覆盖过它的 node id 集合
        self._tests_by_file: dict[str, set[str]] = {}
        # 采集期/导入期空上下文的行数据，不计入用例维度
        self._collection_lines: dict[str, list[int]] = {}
        # 解析过程中的诊断计数（跳过行数、库外文件数等）
        self.diagnostics: dict[str, int] = {}
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        """
        映射表是否已加载

        返回:
            bool: load() 成功返回后为 True
        """
        return self._loaded

    @staticmethod
    def split_context(raw_context: str) -> tuple[str, str]:
        """
        拆分 coverage context 名，还原 (node id, 阶段)

        参数:
            raw_context (str): context 表原始值，形如
                               "tests/a.py::test_b|run"

        返回:
            tuple[str, str]: (node id, 阶段)；无 `|` 后缀时阶段为空串
        """
        if PHASE_SEPARATOR in raw_context:
            node_id, _, phase = raw_context.rpartition(PHASE_SEPARATOR)
            return node_id, phase
        return raw_context, ""

    def _phase_matches(self, phase: str) -> bool:
        """
        判断某阶段的行数据是否计入本次统计

        参数:
            phase (str): 该 context 实际所处的阶段

        返回:
            bool: phase=="all" 时全收；否则要求阶段精确相等
        """
        return self.phase == "all" or phase == self.phase

    def _matches_phase_filter(self, phase: str) -> bool:
        """阶段过滤（与 _phase_matches 同义，保留独立命名便于阅读）"""
        return self._phase_matches(phase)

    def load(self) -> None:
        """
        读取 .coverage 并建立双向映射

        流程:
            1. 探查 schema：确认 file/context/line_bits 三表存在
            2. 读 file 表建 file_id → 相对路径 映射（库外文件跳过）
            3. 读 context 表拆 node id 与阶段，空上下文归入采集期
            4. 读 line_bits 表解码 numbits，按阶段过滤后建双向索引

        参数:
            无

        返回:
            None

        异常:
            FileNotFoundError: .coverage 文件不存在
            ValueError: 缺少必需的表（coverage.py 版本/schema 不受支持），
                        或 context 表为空（未用 --cov-context=test 生成）
        """
        if not self.coverage_path.is_file():
            raise FileNotFoundError(f"覆盖率数据文件不存在: {self.coverage_path}")

        self._files_by_test.clear()
        self._tests_by_file.clear()
        self._collection_lines.clear()
        self.diagnostics = {
            "库外文件数": 0,
            "非项目源码行数据": 0,
            "阶段过滤掉的行数据": 0,
            "无行数据文件数": 0,
        }

        # 只读连接：.coverage 是 coverage.py 的数据资产，POC 无写需求，
        # 只读打开可避免误改（也避开 Windows 上库被占用时的写锁问题）
        connection = sqlite3.connect(f"file:{self.coverage_path}?mode=ro", uri=True)
        try:
            self._check_schema(connection)
            path_by_id = self._load_file_table(connection)
            context_by_id = self._load_context_table(connection)
            self._load_line_bits(connection, path_by_id, context_by_id)
        finally:
            # 异常路径也必须关连接：Windows 下不关会让 .coverage 文件
            # 保持句柄占用，后续 coverage combine/erase 直接 WinError 145
            connection.close()

        self._loaded = True
        logger.info(
            f"覆盖率映射表加载完成 | 文件: {self.coverage_path.name} | "
            f"阶段: {self.phase} | 用例数: {len(self._files_by_test)} | "
            f"源码文件数: {len(self._tests_by_file)}"
        )

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        """
        探查并校验 coverage.py 的 SQLite schema

        参数:
            connection (sqlite3.Connection): 已打开的只读连接

        返回:
            None

        异常:
            ValueError: 缺少 file/context/line_bits 任一表
        """
        cursor = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            raise ValueError(
                f"覆盖率数据 schema 不受支持，缺少表: {missing}；"
                f"实际表: {sorted(tables)}"
            )

    def _load_file_table(self, connection: sqlite3.Connection) -> dict[int, str]:
        """
        读 file 表，建 file_id → 项目内相对路径 映射

        参数:
            connection (sqlite3.Connection): 只读连接

        返回:
            dict[int, str]: file_id 到相对 posix 路径的映射；
                            项目根之外的文件（标准库/第三方）不收录
        """
        cursor = connection.execute("SELECT id, path FROM file")
        path_by_id: dict[int, str] = {}
        for file_id, raw_path in cursor.fetchall():
            relative = normalize_rel_path(raw_path, self.project_root)
            if is_out_of_project(relative):
                # 库外文件（site-packages/标准库）：与回归选择无关，
                # 收录只会让"文件→用例"反向索引失真
                self.diagnostics["库外文件数"] += 1
                continue
            path_by_id[int(file_id)] = relative
        return path_by_id

    def _load_context_table(self, connection: sqlite3.Connection) -> dict[int, str]:
        """
        读 context 表，建 context_id → 统计口径下的 node id 映射

        参数:
            connection (sqlite3.Connection): 只读连接

        返回:
            dict[int, str]: 计入统计的 context_id → node id；
                            空上下文（采集期）与阶段不匹配的 context 直接略过
        """
        cursor = connection.execute("SELECT id, context FROM context")
        context_by_id: dict[int, str] = {}
        for context_id, raw_context in cursor.fetchall():
            text = str(raw_context)
            if text == EMPTY_CONTEXT:
                # 空上下文 = 收集期/导入期执行，发生在任何用例之前，
                # 与"某条用例执行了哪些代码"无关，绝不能算进用例维度
                continue
            node_id, phase = self.split_context(text)
            if not self._matches_phase_filter(phase):
                continue
            if not node_id:
                continue
            context_by_id[int(context_id)] = node_id
        return context_by_id

    def _load_line_bits(
        self,
        connection: sqlite3.Connection,
        path_by_id: dict[int, str],
        context_by_id: dict[int, str],
    ) -> None:
        """
        读 line_bits 表并建立双向索引

        参数:
            connection (sqlite3.Connection): 只读连接
            path_by_id (dict[int, str]): file_id → 相对路径
            context_by_id (dict[int, str]): 计入统计的 context_id → node id

        返回:
            None
        """
        cursor = connection.execute("SELECT file_id, context_id, numbits FROM line_bits")
        for file_id, context_id, blob in cursor.fetchall():
            relative = path_by_id.get(int(file_id))
            if relative is None:
                self.diagnostics["非项目源码行数据"] += 1
                continue
            node_id = context_by_id.get(int(context_id))
            if node_id is None:
                self.diagnostics["阶段过滤掉的行数据"] += 1
                continue
            lines = numbits_to_lines(blob)
            if not lines:
                self.diagnostics["无行数据文件数"] += 1
                continue
            self._files_by_test.setdefault(node_id, set()).add(relative)
            self._tests_by_file.setdefault(relative, set()).add(node_id)

    def get_tests_for_file(self, relative_path: str) -> set[str]:
        """
        查询覆盖某源码文件的所有用例

        参数:
            relative_path (str): 相对项目根的源码文件路径

        返回:
            set[str]: node id 集合；无覆盖时返回空集合
        """
        return set(self._tests_by_file.get(relative_path, set()))

    def get_files_for_test(self, test_id: str) -> set[str]:
        """
        查询某用例覆盖的所有源码文件

        参数:
            test_id (str): pytest node id

        返回:
            set[str]: 相对路径集合；无记录时返回空集合
        """
        return set(self._files_by_test.get(test_id, set()))

    @property
    def all_tests(self) -> set[str]:
        """
        用例全集（映射表中出现过的全部 node id）

        返回:
            set[str]: node id 集合
        """
        return set(self._files_by_test)

    @property
    def all_files(self) -> set[str]:
        """
        被任何用例覆盖过的源码文件全集

        返回:
            set[str]: 相对路径集合
        """
        return set(self._tests_by_file)

    def get_statistics(self) -> dict[str, Any]:
        """
        映射表统计信息

        返回:
            dict: 含用例数/源码文件数/平均每用例覆盖文件数/
                  平均每文件被覆盖用例数/单文件覆盖极值，
                  以及库外文件数等诊断计数
        """
        total_tests = len(self._files_by_test)
        total_files = len(self._tests_by_file)
        per_test_counts = [len(files) for files in self._files_by_test.values()]
        per_file_counts = [len(tests) for tests in self._tests_by_file.values()]
        return {
            "覆盖率文件": str(self.coverage_path),
            "统计阶段": self.phase,
            "用例总数": total_tests,
            "源码文件总数": total_files,
            "平均每用例覆盖文件数": round(
                sum(per_test_counts) / total_tests, 4
            ) if total_tests else 0.0,
            "平均每文件被覆盖用例数": round(
                sum(per_file_counts) / total_files, 4
            ) if total_files else 0.0,
            "单文件最多被覆盖用例数": max(per_file_counts, default=0),
            "单用例最多覆盖文件数": max(per_test_counts, default=0),
            "零覆盖文件数": self._count_zero_coverage_files(),
            "诊断": dict(self.diagnostics),
        }

    def _count_zero_coverage_files(self) -> int:
        """
        统计被测量但没有任何用例到达的源码文件数

        **口径必须与 _load_file_table 一致**（Day45-fix2 P3-3）：
        两者都读同一张 `file` 表，若此处不过滤库外文件，
        零覆盖数会把 site-packages/标准库也算进来，与"库外文件数"
        诊断项自相矛盾（当前数据集库外数为 0 故看不出问题，
        一旦 coverage 测到库外路径，两个数字就对不上）。
        因此复用同一个 is_out_of_project 判据，不另写一份。

        返回:
            int: 项目内零覆盖文件数量
        """
        connection = sqlite3.connect(f"file:{self.coverage_path}?mode=ro", uri=True)
        try:
            cursor = connection.execute("SELECT path FROM file")
            all_relative = {
                relative
                for relative in (
                    normalize_rel_path(row[0], self.project_root) for row in cursor.fetchall()
                )
                if not is_out_of_project(relative)
            }
        finally:
            connection.close()
        return len(all_relative - set(self._tests_by_file))


class ImportGraph:
    """
    模块 import 依赖图（标准库 ast 解析）

    边的方向约定:
        A `import B` → 记 A → B（A 依赖 B）
        因此「修改 B 会影响谁」= 沿边**反向**可达的模块集合，
        get_affected_modules 走的就是反向 BFS。
    """

    def __init__(self, src_dir: str | Path, project_root: Path | None = None) -> None:
        """
        初始化依赖图解析器

        参数:
            src_dir (str | Path): 源码目录（默认 src/）
            project_root (Path | None): 项目根目录（None 时取脚本上级目录）

        返回:
            None
        """
        self.project_root = project_root or PROJECT_ROOT
        self.src_dir = Path(src_dir)
        # 模块名（点分）→ 相对项目根的 posix 路径
        self.modules: dict[str, str] = {}
        # 模块名 → 它直接 import 的模块名集合（仅项目内模块）
        self.dependencies: dict[str, set[str]] = {}
        # 反向索引：模块名 → 直接依赖它的模块名集合
        self.dependents: dict[str, set[str]] = {}
        self.parse_errors: list[str] = []

    @staticmethod
    def module_name_for(relative_path: str) -> str:
        """
        由相对路径推模块名（__init__.py 表示包本身）

        参数:
            relative_path (str): 相对项目根的 posix 路径，如 src/core/__init__.py

        返回:
            str: 点分模块名，如 src.core / src.core.executors
        """
        without_suffix = relative_path
        if without_suffix.endswith(".py"):
            without_suffix = without_suffix[: -len(".py")]
        parts = [part for part in without_suffix.split("/") if part]
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @staticmethod
    def _package_of(module_name: str, is_package: bool) -> str:
        """
        求模块所属包（相对导入的基准）

        参数:
            module_name (str): 模块名
            is_package (bool): 该模块是否为包（__init__.py）

        返回:
            str: 包名；顶层模块返回空串
        """
        if is_package:
            return module_name
        parent, _, _ = module_name.rpartition(".")
        return parent

    def _resolve_relative(
        self, current_module: str, current_is_package: bool, node: ast.ImportFrom
    ) -> list[str]:
        """
        解析相对导入为绝对模块名

        参数:
            current_module (str): 当前文件所属模块名
            current_is_package (bool): 当前文件是否为 __init__.py
            node (ast.ImportFrom): from . / from .. import ... 节点

        返回:
            list[str]: 解析出的绝对模块名列表（无 module 时只返回包自身）
        """
        package = self._package_of(current_module, current_is_package)
        # level=1 表示"当前包"，level=2 表示"当前包的父包"，逐级上跳
        parts = package.split(".") if package else []
        for _ in range(max(node.level - 1, 0)):
            if not parts:
                break
            parts.pop()
        if node.module:
            parts.extend(node.module.split("."))
        return [".".join(parts)] if parts else []

    def build(self) -> None:
        """
        扫描源码目录并建立依赖图

        流程:
            1. 递归收集 .py 文件并登记模块名
            2. 逐文件 ast.parse，收集 Import / ImportFrom 节点
            3. 绝对导入直接取名；相对导入按包层级解析
            4. 只保留命中已登记模块的边（第三方/标准库不入图）
            5. 建反向索引 dependents

        注: ast.walk 会走进 if TYPE_CHECKING 块与 try/except ImportError
        分支——对"影响面"分析这是**保守正确**的选择：类型检查期的导入
        改签名同样会打断调用方，宁可多选不可漏选。

        参数:
            无

        返回:
            None

        异常:
            无（单个文件语法错误记入 parse_errors 后继续，
                绝不让一个坏文件毁掉整张图）
        """
        self.modules.clear()
        self.dependencies.clear()
        self.dependents.clear()
        self.parse_errors.clear()

        for python_file in sorted(self.src_dir.rglob("*.py")):
            relative = normalize_rel_path(python_file, self.project_root)
            module_name = self.module_name_for(relative)
            if not module_name:
                continue
            self.modules[module_name] = relative
            self.dependencies.setdefault(module_name, set())
            self.dependents.setdefault(module_name, set())

        for module_name, relative in self.modules.items():
            is_package = relative.endswith("/__init__.py")
            for target in self._extract_imports(self.modules[module_name], module_name, is_package):
                if target not in self.dependencies:
                    # 目标不在项目内（标准库/第三方）：不入图，
                    # 否则每条边都指向图外节点，反向可达性全被稀释
                    continue
                self.dependencies[module_name].add(target)
                self.dependents[target].add(module_name)

        logger.info(
            f"import 依赖图构建完成 | 目录: {self.src_dir} | 模块数: "
            f"{len(self.modules)} | 依赖边数: {self.total_edges} | "
            f"解析失败: {len(self.parse_errors)}"
        )

    def _extract_imports(
        self, absolute_path: str, module_name: str, is_package: bool
    ) -> set[str]:
        """
        解析单个文件的 import 语句，产出目标模块名集合

        参数:
            absolute_path (str): 模块在项目内的相对路径（拼回项目根后读）
            module_name (str): 当前模块名
            is_package (bool): 当前文件是否为 __init__.py

        返回:
            set[str]: 目标模块名集合
        """
        file_path = self.project_root / absolute_path
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            self.parse_errors.append(f"{absolute_path}: {type(exc).__name__}: {exc}")
            logger.warning(f"import 解析跳过（语法/读取问题） | 文件: {absolute_path} | {exc}")
            return set()

        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                # import a.b.c → 目标模块 a.b.c（as 别名不影响依赖关系）
                for alias in node.names:
                    targets.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    targets.update(self._resolve_relative(module_name, is_package, node))
                elif node.module:
                    targets.add(node.module)
                    # from pkg import mod 中，mod 本身也可能是项目内模块
                    for alias in node.names:
                        targets.add(f"{node.module}.{alias.name}")
        return targets

    @property
    def total_edges(self) -> int:
        """
        依赖边总数

        返回:
            int: 所有模块出度之和
        """
        return sum(len(targets) for targets in self.dependencies.values())

    def get_dependencies(self, module_name: str) -> set[str]:
        """
        查询某模块直接依赖的模块

        参数:
            module_name (str): 模块名

        返回:
            set[str]: 直接依赖集合；模块不存在时返回空集合
        """
        return set(self.dependencies.get(module_name, set()))

    def get_dependents(self, module_name: str) -> set[str]:
        """
        查询直接依赖某模块的模块（反向一层）

        参数:
            module_name (str): 模块名

        返回:
            set[str]: 直接依赖方集合；模块不存在时返回空集合
        """
        return set(self.dependents.get(module_name, set()))

    def get_affected_modules(self, module_name: str, include_self: bool = False) -> set[str]:
        """
        反向影响面：修改某模块会（传递）影响哪些模块

        算法: 反向边 BFS（deque 队列 + visited 去重），O(V+E)，
        不用递归以免深依赖链触发递归上限。

        参数:
            module_name (str): 被修改的模块名
            include_self (bool): 是否把模块自身计入结果。
                                 叶子模块无任何依赖方，visited 初始为空，
                                 需显式 add 才能兑现该参数（否则 include_self
                                 对叶子恒无效——Day45 单测实测踩到）

        返回:
            set[str]: 受影响模块集合（传递依赖闭包）
        """
        if module_name not in self.dependencies:
            return set()

        visited: set[str] = {module_name} if include_self else set()
        queue: deque[str] = deque([module_name])
        while queue:
            current = queue.popleft()
            for dependent in self.dependents.get(current, set()):
                if dependent in visited:
                    continue
                visited.add(dependent)
                queue.append(dependent)

        if not include_self:
            visited.discard(module_name)
        return visited

    def get_statistics(self) -> dict[str, Any]:
        """
        依赖图统计信息

        返回:
            dict: 模块数/依赖边数/平均出度/平均入度/零入度模块数/
                  零出度模块数/解析失败文件数
        """
        module_count = len(self.modules)
        out_degrees = [len(targets) for targets in self.dependencies.values()]
        in_degrees = [len(users) for users in self.dependents.values()]
        return {
            "源码目录": str(self.src_dir),
            "模块总数": module_count,
            "依赖边总数": self.total_edges,
            "平均出度": round(sum(out_degrees) / module_count, 4) if module_count else 0.0,
            "平均入度": round(sum(in_degrees) / module_count, 4) if module_count else 0.0,
            "零出度模块数": sum(1 for degree in out_degrees if degree == 0),
            "零入度模块数": sum(1 for degree in in_degrees if degree == 0),
            "最大入度模块": self._top_module_by(self.dependents),
            "解析失败文件数": len(self.parse_errors),
            "解析失败明细": list(self.parse_errors),
        }

    def _top_module_by(self, mapping: dict[str, set[str]]) -> str:
        """
        取某方向入度最大的模块（并列取字典序最小，结果稳定可复现）

        参数:
            mapping (dict[str, set[str]]): dependents 或 dependencies

        返回:
            str: 模块名；无模块时返回空串
        """
        if not mapping:
            return ""
        return sorted(mapping.items(), key=lambda item: (-len(item[1]), item[0]))[0][0]

    def find_cycles(self) -> list[list[str]]:
        """
        循环依赖检测（DFS 三色标记）

        为什么必须查: 存在环时「反向影响面」会把环内所有模块都算作互相
        影响，传递闭包失去区分度（选中面会被环撑大）。检测出来是给
        Day94 工程化的告警信号，不阻断分析。

        参数:
            无

        返回:
            list[list[str]]: 每个环一个模块名列表（首尾同一模块表示闭合），
                              无环时返回空列表
        """
        white, gray, black = 0, 1, 2
        color: dict[str, int] = dict.fromkeys(self.dependencies, white)
        cycles: list[list[str]] = []
        seen_signatures: set[tuple[str, ...]] = set()

        def visit(node: str, stack: list[str]) -> None:
            color[node] = gray
            stack.append(node)
            for neighbor in sorted(self.dependencies.get(node, set())):
                if color.get(neighbor, white) == gray:
                    # 回到栈内节点 → 找到环，截取栈中对应片段
                    start = stack.index(neighbor)
                    cycle = stack[start:]
                    signature = tuple(sorted(cycle))
                    if signature not in seen_signatures:
                        seen_signatures.add(signature)
                        cycles.append(cycle + [neighbor])
                elif color.get(neighbor, white) == white:
                    visit(neighbor, stack)
            stack.pop()
            color[node] = black

        for module in sorted(self.dependencies):
            if color.get(module, white) == white:
                visit(module, [])
        return cycles


class RegressionSelector:
    """
    精准回归用例选择器

    选择公式（并集）:
        选中(文件F) = 直接覆盖 F 的用例
                    ∪ 直接覆盖「反向影响面内任一模块」的用例
    并集而非交集：真实修改既可能让 F 自身行为变错，也可能让调用方行为变错，
    任一维度漏掉都可能造成回归漏检。
    """

    def __init__(self, mapper: CoverageMapper, graph: ImportGraph) -> None:
        """
        初始化选择器

        参数:
            mapper (CoverageMapper): 覆盖率映射表解析器
            graph (ImportGraph): import 依赖图

        返回:
            None
        """
        self.mapper = mapper
        self.graph = graph

    def module_for_file(self, relative_path: str) -> str:
        """
        查源码文件对应的模块名

        参数:
            relative_path (str): 相对项目根的 posix 路径

        返回:
            str: 模块名；不在依赖图中时返回空串
        """
        module_name = ImportGraph.module_name_for(relative_path)
        return module_name if module_name in self.graph.modules else ""

    def select_for_file(self, relative_path: str) -> dict[str, Any]:
        """
        计算单个修改文件的选中用例集与双指标

        参数:
            relative_path (str): 相对项目根的 posix 路径

        返回:
            dict: 选中用例集合/直接覆盖用例集合/影响面模块集合/
                  选中比例/漏检率/仅靠 import 的召回率 等实验数据
        """
        direct_tests = self.mapper.get_tests_for_file(relative_path)
        module_name = self.module_for_file(relative_path)
        affected_modules = self.graph.get_affected_modules(module_name) if module_name else set()

        # 影响面内模块的覆盖用例并集
        impact_tests: set[str] = set()
        for module in affected_modules:
            module_path = self.graph.modules.get(module)
            if module_path:
                impact_tests |= self.mapper.get_tests_for_file(module_path)

        selected = direct_tests | impact_tests
        total_tests = len(self.mapper.all_tests)

        # 漏检率: 直接覆盖 F 却没被选中的用例占比。
        # 选择公式含"直接覆盖"项，理论上恒为 0 —— 它的价值是**回归守卫**：
        # 一旦有人把选择公式改成纯 import 召回，这条断言立刻变红。
        missed = direct_tests - selected
        miss_rate = (len(missed) / len(direct_tests)) if direct_tests else 0.0

        # 补充指标: 只用 import 影响面（不含直接覆盖）能召回多少直接覆盖用例。
        # 这个数不是恒真的，它量化了"第二维度到底贡献了多少"，
        # 是 Day94 判断 import 拓扑是否值得保留的唯一依据。
        import_only_hit = direct_tests & impact_tests
        import_only_recall = (len(import_only_hit) / len(direct_tests)) if direct_tests else 0.0

        return {
            "文件": relative_path,
            "模块名": module_name,
            "影响面模块数": len(affected_modules),
            "影响面模块": sorted(affected_modules),
            "直接覆盖用例数": len(direct_tests),
            "影响面贡献用例数": len(impact_tests - direct_tests),
            "选中用例数": len(selected),
            "选中用例": sorted(selected),
            "漏检用例": sorted(missed),
            "选中比例": round(len(selected) / total_tests, 4) if total_tests else 0.0,
            "漏检率": round(miss_rate, 4),
            "仅靠import的召回率": round(import_only_recall, 4),
        }

    def select(self, modified_files: list[str]) -> dict[str, Any]:
        """
        对多个修改文件做选择并汇总

        参数:
            modified_files (list[str]): 相对项目根的 posix 路径列表

        返回:
            dict: {"逐文件": [...], "汇总": {...}}
        """
        per_file = [self.select_for_file(path) for path in modified_files]
        union: set[str] = set()
        for item in per_file:
            union |= set(item["选中用例"])
        total_tests = len(self.mapper.all_tests)

        # 汇总层面的漏检率: 全部修改文件的直接覆盖用例并集是否全被选中
        direct_union: set[str] = set()
        for item in per_file:
            direct_of_file = self.mapper.get_tests_for_file(item["文件"])
            direct_union |= {
                test for test in item["选中用例"] if test in direct_of_file
            }
        missed_union = direct_union - union
        return {
            "逐文件": per_file,
            "汇总": {
                "修改文件数": len(modified_files),
                "用例总数": total_tests,
                "选中用例数": len(union),
                "选中用例": sorted(union),
                "汇总选中比例": round(len(union) / total_tests, 4) if total_tests else 0.0,
                "直接覆盖用例并集数": len(direct_union),
                "漏检用例数": len(missed_union),
                "漏检率": round(
                    len(missed_union) / len(direct_union), 4
                ) if direct_union else 0.0,
            },
        }


def pick_representative_files(
    graph: ImportGraph, mapper: CoverageMapper, limit: int = 5
) -> list[str]:
    """
    挑选代表性实验文件

    顺序（**先清单后规则**，不可调换）:
        1. DEFAULT_REPRESENTATIVE_FILES 清单内仍存在的文件，按清单顺序取用。
           清单是按验收口径人工选定的五种形态（大文件核心 / 主题模块 /
           Web 入口 / 边缘配置 / 叶子工具），每种形态的选中比例量级不同，
           混在一起才能看出算法的分档能力。
        2. 清单有缺项时按数据规则补位：行数最大 → 入度最高（核心枢纽）
           → 零入度叶子中最小 → 覆盖率最高。
           规则排在清单之后是实测结论（Day45）：入度最高的 logger.py
           会被 1180 条用例全量覆盖、选中比例 100%，零入度最小的
           db/__init__.py 是 6 行空文件、选中比例 0%——两者都落在
           「算法失效」区间，用它们当实验样本会误判算法。
           改到 Day94 工程化时若要按纯规则自动挑选，须先加过滤：
           排除选中比例 >90%（近乎全选）与 0%（零覆盖）的文件。

    参数:
        graph (ImportGraph): 已构建的依赖图
        mapper (CoverageMapper): 已加载的映射表
        limit (int): 目标文件数

    返回:
        list[str]: 相对项目根的 posix 路径列表
    """
    candidates = sorted(graph.modules.values())
    if not candidates:
        return []

    def line_count(relative_path: str) -> int:
        try:
            text = (graph.project_root / relative_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0
        return len(text.splitlines())

    def coverage_count(relative_path: str) -> int:
        return len(mapper.get_tests_for_file(relative_path))

    module_for_path = {path: ImportGraph.module_name_for(path) for path in candidates}
    in_degree = {
        path: len(graph.dependents.get(module_for_path[path], set())) for path in candidates
    }

    chosen: list[str] = []
    seen: set[str] = set()

    def append(path: str) -> None:
        if path and path not in seen and len(chosen) < limit:
            seen.add(path)
            chosen.append(path)

    # 优先层: 人工选定的代表性清单（形态覆盖优先于数据极值）
    for path in DEFAULT_REPRESENTATIVE_FILES:
        if len(chosen) >= limit:
            break
        if path in candidates:
            append(path)

    # 补位层: 数据规则（清单缺项或目录结构变化时兜底）
    append(max(candidates, key=line_count))
    append(max(candidates, key=lambda path: (in_degree[path], line_count(path))))
    leaf_candidates = [
        path for path in candidates if in_degree[path] == 0 and line_count(path) > 0
    ]
    if leaf_candidates:
        append(min(leaf_candidates, key=line_count))
    for path in sorted(candidates, key=lambda item: (-coverage_count(item), item)):
        if len(chosen) >= limit:
            break
        append(path)
    return chosen[:limit]


def render_text_report(
    result: dict[str, Any],
    mapper: CoverageMapper,
    graph: ImportGraph,
    cycles: list[list[str]],
) -> None:
    """
    打印实验结果（text 输出格式）

    打印而非 logger: 本脚本是命令行报告工具，报告正文走 stdout
    （与 scripts/benchmark_cache.py 同一惯例），过程日志才走 loguru。

    参数:
        result (dict): RegressionSelector.select 的返回
        mapper (CoverageMapper): 映射表（取统计）
        graph (ImportGraph): 依赖图（取统计）
        cycles (list): 环列表

    返回:
        None
    """
    print("=" * 78)
    print("TestMatrix AST 精准回归最小 POC（Day45）")
    print("=" * 78)

    print("\n【一】覆盖率映射表统计（.coverage → 用例与源码文件）")
    for key, value in mapper.get_statistics().items():
        print(f"  {key}: {value}")

    print("\n【二】import 依赖图统计（src/ AST 拓扑 → 反向影响面）")
    for key, value in graph.get_statistics().items():
        if key == "解析失败明细" and value:
            print(f"  {key}: {value}")
        else:
            print(f"  {key}: {value}")

    print("\n【三】循环依赖检测")
    if cycles:
        for index, cycle in enumerate(cycles, start=1):
            print(f"  环{index}: {' -> '.join(cycle)}")
    else:
        print("  未检测到循环依赖")

    print("\n【四】逐文件实验数据（人为修改 → 选中比例 / 漏检率）")
    header = (
        f"  {'文件':<34}{'行数':>7}{'影响面':>8}{'直接覆盖':>10}"
        f"{'选中':>8}{'选中比例':>10}{'漏检率':>9}{'import召回':>11}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for item in result["逐文件"]:
        line_total = 0
        try:
            line_total = len(
                (graph.project_root / item["文件"]).read_text(encoding="utf-8").splitlines()
            )
        except (OSError, UnicodeDecodeError):
            line_total = 0
        print(
            f"  {item['文件']:<34}{line_total:>7}{item['影响面模块数']:>8}"
            f"{item['直接覆盖用例数']:>10}{item['选中用例数']:>8}"
            f"{item['选中比例'] * 100:>9.2f}%{item['漏检率'] * 100:>8.2f}%"
            f"{item['仅靠import的召回率'] * 100:>10.2f}%"
        )

    summary = result["汇总"]
    print("\n【五】汇总")
    print(f"  修改文件数: {summary['修改文件数']}")
    print(f"  用例总数: {summary['用例总数']}")
    print(f"  选中用例数: {summary['选中用例数']}")
    print(f"  汇总选中比例: {summary['汇总选中比例'] * 100:.2f}%")
    print(f"  漏检用例数: {summary['漏检用例数']}（漏检率 {summary['漏检率'] * 100:.2f}%）")
    print("=" * 78)


def resolve_coverage_path(explicit: str | None) -> Path:
    """
    定位 .coverage 文件（显式指定优先，否则按默认候选顺序探测）

    参数:
        explicit (str | None): 显式指定的覆盖率文件路径

    返回:
        Path: .coverage 文件路径

    异常:
        FileNotFoundError: 显式路径不存在，或全部候选都不存在
    """
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"指定的覆盖率文件不存在: {path}")
        return path
    for candidate in DEFAULT_COVERAGE_CANDIDATES:
        path = PROJECT_ROOT / candidate
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"未找到覆盖率数据文件（候选: {list(DEFAULT_COVERAGE_CANDIDATES)}）；"
        "请先运行 py -m pytest --cov=src --cov-context=test --cov-report= -q 生成"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    解析命令行参数

    参数:
        argv (list[str] | None): 参数列表（None 时取 sys.argv）

    返回:
        argparse.Namespace: 解析结果
    """
    parser = argparse.ArgumentParser(
        prog="ast_regression_poc",
        description=(
            "AST 精准回归最小 POC：解析 .coverage 建立用例与源码文件映射、"
            "用 AST import 拓扑算反向影响面、输出选中比例与漏检率"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  py -m scripts.ast_regression_poc\n"
            "  py -m scripts.ast_regression_poc --output json\n"
            "  py -m scripts.ast_regression_poc --modified-files src/core/executors.py\n"
            "  py -m scripts.ast_regression_poc --phase all --stats-only\n"
        ),
    )
    parser.add_argument(
        "--coverage", default=None,
        help="覆盖率数据文件路径（默认按 .coverage → output/.coverage 顺序探测）",
    )
    parser.add_argument(
        "--src-dir", default=DEFAULT_SRC_DIR,
        help=f"源码目录（默认 {DEFAULT_SRC_DIR}/）",
    )
    parser.add_argument(
        "--modified-files", default=None,
        help="要分析的修改文件列表（逗号分隔，相对项目根或绝对路径）",
    )
    parser.add_argument(
        "--phase", default=DEFAULT_PHASE,
        choices=["run", "setup", "teardown", "all"],
        help="统计哪个执行阶段的覆盖（默认 run；setup/teardown 覆盖共享代码，"
             "会显著放大选中面，all 用于对比观察）",
    )
    parser.add_argument(
        "--output", default="text", choices=["text", "json"],
        help="输出格式（默认 text）",
    )
    parser.add_argument(
        "--stats-only", action="store_true",
        help="只输出映射表与依赖图统计，不做用例选择实验",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    CLI 主入口

    流程:
        1. 解析参数、定位 .coverage
        2. 构建覆盖率映射表与 import 依赖图
        3. 确定待分析文件（显式指定或自动挑选 5 个代表文件）
        4. 跑选择实验并按 text/json 输出

    参数:
        argv (list[str] | None): 参数列表（None 时取 sys.argv）

    返回:
        int: 进程退出码，0 成功 / 1 参数或数据错误
    """
    args = parse_args(argv)
    try:
        coverage_path = resolve_coverage_path(args.coverage)
        raw_src_dir = Path(args.src_dir)
        src_dir = raw_src_dir if raw_src_dir.is_absolute() else PROJECT_ROOT / raw_src_dir

        mapper = CoverageMapper(coverage_path, project_root=PROJECT_ROOT, phase=args.phase)
        mapper.load()

        graph = ImportGraph(src_dir, project_root=PROJECT_ROOT)
        graph.build()
        cycles = graph.find_cycles()

        if args.stats_only:
            payload = {
                "覆盖率映射表统计": mapper.get_statistics(),
                "import依赖图统计": graph.get_statistics(),
                "循环依赖": cycles,
            }
            if args.output == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                render_text_report(
                    {"逐文件": [], "汇总": {
                        "修改文件数": 0, "用例总数": len(mapper.all_tests),
                        "选中用例数": 0, "汇总选中比例": 0.0,
                        "直接覆盖用例并集数": 0, "漏检用例数": 0, "漏检率": 0.0,
                    }},
                    mapper, graph, cycles,
                )
            return 0

        if args.modified_files:
            raw_paths = [item.strip() for item in args.modified_files.split(",") if item.strip()]
            modified_files = [
                normalize_rel_path(path, PROJECT_ROOT) for path in raw_paths
            ]
            unknown = [path for path in modified_files if path not in graph.modules.values()]
            if unknown:
                logger.warning(f"以下文件不在 import 依赖图内（按空影响面处理）: {unknown}")
        else:
            modified_files = pick_representative_files(graph, mapper, limit=5)
            if not modified_files:
                logger.error(f"源码目录内未解析到任何模块: {src_dir}")
                return 1

        selector = RegressionSelector(mapper, graph)
        result = selector.select(modified_files)
        result["覆盖率映射表统计"] = mapper.get_statistics()
        result["import依赖图统计"] = graph.get_statistics()
        result["循环依赖"] = cycles

        if args.output == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            render_text_report(result, mapper, graph, cycles)
        return 0
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"POC 执行失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
