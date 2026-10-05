"""
AST 精准回归最小 POC 脚本单元测试（Day45）

测试对象: scripts/ast_regression_poc.py 的三类能力 + CLI
    1. CoverageMapper: 解析 .coverage（SQLite）建立「用例↔源码文件」映射
    2. ImportGraph:     AST 解析 import 关系、建立反向影响面、检测循环依赖
    3. RegressionSelector: 直接覆盖 ∪ import 影响面的并集选择与双指标

自包含铁律（本日验收要求）:
    - 不依赖项目真实的 .coverage 文件：全部用 tmp_path 现造 SQLite 夹具
    - 不依赖项目真实 src/ 目录：全部用 tmp_path 现造包结构
    - 不起真实子进程、不连网络、不碰真实数据库

命名沿用项目约定: test_场景_预期结果
"""

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ast_regression_poc import (  # noqa: E402
    CoverageMapper,
    ImportGraph,
    RegressionSelector,
    lines_to_numbits,
    main,
    normalize_rel_path,
    numbits_to_lines,
    parse_args,
    pick_representative_files,
)

# coverage.py 7.x 的真实 schema（与 coverage.sqldata.SCHEMA 同构），
# 只保留本 POC 实际读到的四张表
COVERAGE_SCHEMA_SQL = """
CREATE TABLE coverage_schema (version integer);
CREATE TABLE meta (key text, value text, unique (key));
CREATE TABLE file (id integer primary key, path text, unique (path));
CREATE TABLE context (id integer primary key, context text, unique (context));
CREATE TABLE line_bits (
    file_id integer,
    context_id integer,
    numbits blob,
    unique (file_id, context_id)
);
"""


def build_coverage_file(path: Path, project_root: Path, data: dict) -> None:
    """
    按 coverage.py schema 造一个 .coverage 夹具文件

    参数:
        path (Path): 输出文件路径
        project_root (Path): 夹具中的源码路径以此为根（写绝对路径）
        data (dict): {context 名: {相对路径: [行号, ...]}}

    返回:
        None
    """
    connection = sqlite3.connect(path)
    try:
        connection.executescript(COVERAGE_SCHEMA_SQL)
        connection.execute("INSERT INTO coverage_schema VALUES (7)")
        connection.execute("INSERT INTO meta VALUES ('version', '7.16.2')")
        file_ids: dict[str, int] = {}
        context_ids: dict[str, int] = {}
        for context_name, file_lines in data.items():
            cursor = connection.execute(
                "INSERT INTO context (context) VALUES (?)", (context_name,)
            )
            context_id = int(cursor.lastrowid or 0)
            context_ids[context_name] = context_id
            for relative, lines in file_lines.items():
                if relative not in file_ids:
                    cursor = connection.execute(
                        "INSERT INTO file (path) VALUES (?)",
                        (str(project_root / relative),),
                    )
                    file_ids[relative] = int(cursor.lastrowid or 0)
                connection.execute(
                    "INSERT INTO line_bits (file_id, context_id, numbits) VALUES (?, ?, ?)",
                    (file_ids[relative], context_id, lines_to_numbits(lines)),
                )
        connection.commit()
    finally:
        connection.close()


class _StubMapper:
    """
    覆盖率映射表的极简替身（只实现 RegressionSelector 用到的三个接口）

    为什么不用 mock: 选择器只依赖「文件→用例」查询与用例全集两件事，
    用一个 12 行的真实对象替身比 MagicMock 更贴近真实契约——
    mock 的方法可以返回任何值，写错调用名也不会失败。
    """

    def __init__(self, tests_by_file: dict) -> None:
        """
        参数:
            tests_by_file (dict): {相对路径: {node id, ...}}
        """
        self._tests_by_file = {path: set(tests) for path, tests in tests_by_file.items()}

    def get_tests_for_file(self, relative_path: str) -> set:
        """返回覆盖该文件的用例集合"""
        return set(self._tests_by_file.get(relative_path, set()))

    def get_files_for_test(self, test_id: str) -> set:
        """返回该用例覆盖的文件集合（反查）"""
        return {
            path for path, tests in self._tests_by_file.items() if test_id in tests
        }

    @property
    def all_tests(self) -> set:
        """用例全集（所有替身数据里的 node id 并集）"""
        return set().union(*self._tests_by_file.values()) if self._tests_by_file else set()


@pytest.fixture
def coverage_fixture(tmp_path):
    """
    构造一个覆盖 3 个用例 × 3 个源码文件的覆盖率夹具

    结构（刻意设计成可手算）:
        tests/test_a.py::test_one|run  覆盖 src/a.py(10,11) + src/b.py(5)
        tests/test_a.py::test_one|setup 覆盖 src/conftest_helper.py(1)
        tests/test_b.py::test_two|run  覆盖 src/b.py(5,6)
        tests/test_b.py::test_three|run 覆盖 src/c.py(1) + 库外 site-packages 文件
        空上下文 ""                    覆盖 src/a.py(1)（收集期，不应进用例维度）

    返回:
        tuple[Path, Path]: (project_root, .coverage 文件路径)
    """
    project_root = tmp_path / "proj"
    (project_root / "src").mkdir(parents=True)
    for name in ("a.py", "b.py", "c.py", "conftest_helper.py"):
        (project_root / "src" / name).write_text("x = 1\n", encoding="utf-8")
    # 库外文件：不在 project_root 下，映射表应跳过
    outside = tmp_path / "outside" / "site-packages"
    outside.mkdir(parents=True)
    (outside / "third.py").write_text("y = 2\n", encoding="utf-8")

    coverage_path = project_root / ".coverage"
    build_coverage_file(
        coverage_path,
        project_root,
        {
            "tests/test_a.py::test_one|run": {"src/a.py": [10, 11], "src/b.py": [5]},
            "tests/test_a.py::test_one|setup": {"src/conftest_helper.py": [1]},
            "tests/test_b.py::test_two|run": {"src/b.py": [5, 6]},
            "tests/test_b.py::test_three|run": {
                "src/c.py": [1],
                "../outside/site-packages/third.py": [2],
            },
            "": {"src/a.py": [1]},
        },
    )
    return project_root, coverage_path


@pytest.fixture
def graph_fixture(tmp_path):
    """
    构造一个带菱形依赖 + 一个环的源码目录

    依赖形态:
        app  → service, util
        service → repo
        repo → util
        util  → 无（叶子）
        a → b → a（独立的小环，用于验证环检测）
        lonely → 无依赖、且不被任何模块依赖（孤立叶子）

    返回:
        tuple[Path, ImportGraph]: (project_root, 已 build 的依赖图)
    """
    project_root = tmp_path / "graphproj"
    package = project_root / "src"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    sources = {
        "app.py": "from src.service import run\nfrom src.util import helper\n",
        "service.py": "from src.repo import save\nimport src.util\n",
        "repo.py": "from .util import helper\n",
        "util.py": "VALUE = 1\n",
        "a.py": "from src.b import thing\n",
        "b.py": "from src.a import other\n",
        "lonely.py": "ONLY = True\n",
    }
    for name, content in sources.items():
        (package / name).write_text(content, encoding="utf-8")

    graph = ImportGraph(package, project_root=project_root)
    graph.build()
    return project_root, graph


# ===========================================================================
# 位图编解码（.coverage line_bits.numbits 格式）
# ===========================================================================


def test_numbits_编解码往返_行号完全一致():
    """构造的行号经编码再解码后应完全一致（含 0/8/9/63 等位边界）"""
    lines = [0, 1, 7, 8, 9, 63, 64, 200]
    assert numbits_to_lines(lines_to_numbits(lines)) == lines


def test_numbits_空输入_返回空行号列表():
    """空位图/空列表应返回空列表而非抛异常"""
    assert numbits_to_lines(b"") == []
    assert lines_to_numbits([]) == b""


def test_numbits_重复与乱序行号_去重后按升序返回():
    """重复行号去重、输入乱序时输出应仍为升序"""
    assert numbits_to_lines(lines_to_numbits([9, 3, 9, 1])) == [1, 3, 9]


# ===========================================================================
# 路径归一化与 context 拆分
# ===========================================================================


def test_normalize_相对路径与反斜杠_归一为正斜杠相对路径(tmp_path):
    """两种来源形态（相对 posix / 绝对反斜杠）应归一到同一个键"""
    project_root = tmp_path / "proj"
    target = project_root / "src" / "core" / "executors.py"
    target.parent.mkdir(parents=True)
    target.write_text("x=1\n", encoding="utf-8")

    assert normalize_rel_path("src/core/executors.py", project_root) == "src/core/executors.py"
    assert normalize_rel_path(str(target), project_root) == "src/core/executors.py"


def test_normalize_项目根之外的文件_原样返回不抛异常(tmp_path):
    """库外文件归一化不应抛 ValueError（回归守卫：7.2 同类问题）

    库外路径返回**反斜杠已转正斜杠**的绝对路径：此时无法做相对化，
    统一分隔符仍能让调用方用 startswith/endswith 做库外判定，
    且与库内记录的路径形态保持一致。
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    outside = tmp_path / "elsewhere" / "mod.py"
    outside.parent.mkdir()
    outside.write_text("x=1\n", encoding="utf-8")

    normalized = normalize_rel_path(str(outside), project_root)

    assert normalized == str(outside).replace("\\", "/")
    assert "\\" not in normalized
    # 库外判定依据：首段含盘符冒号
    assert ":" in normalized.split("/")[0]


def test_split_context_带阶段后缀与不带后缀_都能正确拆分():
    """pytest-cov 的 context 形如 node|run，也存在无后缀的历史格式"""
    split = CoverageMapper.split_context
    assert split("tests/t.py::test_x|run") == ("tests/t.py::test_x", "run")
    assert split("tests/t.py::test_x") == ("tests/t.py::test_x", "")
    # 参数化 id 内的 [ ] 与 :: 不得影响按最后一个 | 切分
    assert split("tests/t.py::test_x[TM-1-a|b]|teardown") == (
        "tests/t.py::test_x[TM-1-a|b]",
        "teardown",
    )


# ===========================================================================
# CoverageMapper
# ===========================================================================


def test_coverage_映射表解析_双向索引与用例全集正确(coverage_fixture):
    """run 阶段下反向映射应精确到用例，setup 阶段用例不应混入"""
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="run")
    mapper.load()

    assert mapper.is_loaded
    assert mapper.get_tests_for_file("src/b.py") == {
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    }
    assert mapper.get_files_for_test("tests/test_b.py::test_two") == {"src/b.py"}
    assert mapper.all_tests == {
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
        "tests/test_b.py::test_three",
    }
    # 库外文件（../outside/...）不进入反向索引
    assert "tests/test_b.py::test_three" not in mapper.get_tests_for_file(
        "../outside/site-packages/third.py"
    )


def test_coverage_阶段过滤_切换到setup只统计setup阶段用例(coverage_fixture):
    """--phase 是显式口径：setup 与 run 必须互相隔离而不是合并"""
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="setup")
    mapper.load()

    assert mapper.all_tests == {"tests/test_a.py::test_one"}
    assert mapper.get_tests_for_file("src/conftest_helper.py") == {"tests/test_a.py::test_one"}
    # run 阶段独有的文件在 setup 口径下应无覆盖
    assert mapper.get_tests_for_file("src/b.py") == set()


def test_coverage_空上下文_不计入用例维度但计入零覆盖统计(coverage_fixture):
    """空 context 是收集期覆盖，不能伪造成一条用例"""
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="all")
    mapper.load()

    assert "" not in mapper.all_tests
    assert all(node_id for node_id in mapper.all_tests)
    assert mapper.get_statistics()["诊断"]["库外文件数"] == 1


def test_coverage_统计信息_平均值与用例数可手算核对(coverage_fixture):
    """映射表统计应与逐条查询结果自洽"""
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="run")
    mapper.load()
    stats = mapper.get_statistics()

    assert stats["用例总数"] == 3
    assert stats["源码文件总数"] == 3
    # 逐用例覆盖文件数: test_one=2(a,b) / test_two=1(b) / test_three=1(c，库外不计) → 4/3
    assert stats["平均每用例覆盖文件数"] == pytest.approx(4 / 3, abs=1e-4)
    # 逐文件被覆盖用例数: a=1 / b=2 / c=1 → 4/3
    assert stats["平均每文件被覆盖用例数"] == pytest.approx(4 / 3, abs=1e-4)
    assert stats["单文件最多被覆盖用例数"] == 2


def test_coverage_文件不存在或schema缺表_抛明确异常(tmp_path):
    """两类错误输入都要有可读的失败原因，不能静默产出空映射"""
    with pytest.raises(FileNotFoundError, match="覆盖率数据文件不存在"):
        CoverageMapper(tmp_path / "nope.db", project_root=tmp_path).load()

    broken = tmp_path / "broken.db"
    connection = sqlite3.connect(broken)
    connection.execute("CREATE TABLE meta (key text, value text)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="schema 不受支持"):
        CoverageMapper(broken, project_root=tmp_path).load()


# ===========================================================================
# ImportGraph
# ===========================================================================


def test_import图_解析相对与绝对导入_依赖边正确(graph_fixture):
    """from .x / from src.x / import src.x 三种形态都要落到绝对模块名"""
    _, graph = graph_fixture

    assert graph.get_dependencies("src.app") == {"src.service", "src.util"}
    assert graph.get_dependencies("src.repo") == {"src.util"}
    assert graph.get_dependencies("src.util") == set()
    # __init__.py 自身作为包模块登记，不应产生空模块名
    assert "src" in graph.modules


def test_import图_反向影响面_传递依赖闭包正确(graph_fixture):
    """修改 util 应波及 service/repo/app 三层传递依赖，但不含 util 自身"""
    _, graph = graph_fixture

    affected = graph.get_affected_modules("src.util")
    assert affected == {"src.service", "src.repo", "src.app"}
    assert "src.util" not in affected
    # include_self=True 时才把自己算进去
    assert "src.util" in graph.get_affected_modules("src.util", include_self=True)
    # 叶子模块改自身不影响任何调用方
    assert graph.get_affected_modules("src.util") == affected
    assert graph.get_affected_modules("src.lonely") == set()


def test_import图_循环依赖_能检测到环且只报一次(graph_fixture):
    """a → b → a 构成环；去重后只应产出一个环"""
    _, graph = graph_fixture
    cycles = graph.find_cycles()

    assert len(cycles) == 1
    members = set(cycles[0])
    assert members == {"src.a", "src.b"}
    # 环表示首尾同一模块闭合
    assert cycles[0][0] == cycles[0][-1]


def test_import图_无环时_返回空列表(tmp_path):
    """单文件无依赖的目录不应误报循环依赖"""
    project_root = tmp_path / "acyclic"
    package = project_root / "src"
    package.mkdir(parents=True)
    (package / "only.py").write_text("X = 1\n", encoding="utf-8")

    graph = ImportGraph(package, project_root=project_root)
    graph.build()
    assert graph.find_cycles() == []


def test_import图_语法错误文件_记入parse_errors不中断建图(tmp_path):
    """单个坏文件必须降级跳过，不能让整张依赖图构建失败"""
    project_root = tmp_path / "badsrc"
    package = project_root / "src"
    package.mkdir(parents=True)
    (package / "good.py").write_text("from src.other import x\n", encoding="utf-8")
    (package / "other.py").write_text("x = 1\n", encoding="utf-8")
    (package / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")

    graph = ImportGraph(package, project_root=project_root)
    graph.build()

    assert len(graph.parse_errors) == 1
    assert "broken.py" in graph.parse_errors[0]
    assert graph.get_dependencies("src.good") == {"src.other"}
    assert graph.get_statistics()["解析失败文件数"] == 1


def test_import图_统计信息_边数与模块数可手算核对(graph_fixture):
    """依赖图统计应与逐条查询结果自洽"""
    _, graph = graph_fixture
    stats = graph.get_statistics()

    assert stats["模块总数"] == len(graph.modules)
    assert stats["依赖边总数"] == graph.total_edges
    assert stats["依赖边总数"] >= 4
    assert stats["最大入度模块"] == "src.util"


# ===========================================================================
# RegressionSelector
# ===========================================================================


def test_选择器_并集口径_直接覆盖与影响面取并集(coverage_fixture, graph_fixture):
    """选中 = 直接覆盖 ∪ 影响面覆盖；两者互不覆盖的部分必须进并集

    用 src/b.py 作样本：直接覆盖 {test_one, test_two}，而依赖它的
    src.a.py 只被 {test_one} 覆盖——若实现退化为"只取直接覆盖"，
    影响面维度将完全测不出来。
    """
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="run")
    mapper.load()
    _, graph = graph_fixture

    selector = RegressionSelector(mapper, graph)
    item = selector.select_for_file("src/b.py")

    # 直接覆盖 b.py 的用例
    assert item["直接覆盖用例数"] == 2
    # b ← a（a 依赖 b），影响面 = {a}；a 覆盖 test_one，已在直接覆盖内
    assert item["影响面模块"] == ["src.a"]
    assert item["选中用例"] == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    assert item["选中比例"] == pytest.approx(2 / 3, abs=1e-4)
    # 直接覆盖是并集的一项，故漏检必为 0（回归守卫）
    assert item["漏检率"] == 0.0
    assert item["漏检用例"] == []
    # 影响面维度对 b.py 的净增量为 0（test_one 已被直接覆盖命中）
    assert item["影响面贡献用例数"] == 0


def test_选择器_影响面维度_额外召回直接覆盖之外的用例(tmp_path):
    """构造"改 util 会波及 app、但 util 自身无直接测试"的场景，量化 import 维度贡献"""
    project_root = tmp_path / "selproj"
    package = project_root / "src"
    package.mkdir(parents=True)
    for name in ("app.py", "util.py"):
        (package / name).write_text("x = 1\n", encoding="utf-8")
    # app 依赖 util，但用例只覆盖 app（util 零直接覆盖）
    (package / "app.py").write_text("from src.util import helper\n", encoding="utf-8")

    coverage_path = project_root / ".coverage"
    build_coverage_file(
        coverage_path,
        project_root,
        {"tests/test_app.py::test_app|run": {"src/app.py": [1, 2]}},
    )
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="run")
    mapper.load()
    graph = ImportGraph(package, project_root=project_root)
    graph.build()

    selector = RegressionSelector(mapper, graph)
    item = selector.select_for_file("src/util.py")

    # util 直接覆盖 0 条，但 app 覆盖的用例必须被影响面召回——
    # 这正是"只用覆盖率映射会漏检"的真实场景
    assert item["直接覆盖用例数"] == 0
    assert item["影响面模块数"] == 1
    assert item["选中用例数"] == 1
    assert item["选中用例"] == ["tests/test_app.py::test_app"]
    # 直接覆盖为空时漏检率定义为 0（无样本，不应报 NaN）
    assert item["漏检率"] == 0.0
    # 仅靠 import 的召回率同样无样本，取 0
    assert item["仅靠import的召回率"] == 0.0


def test_选择器_多文件汇总_并集去重后比例正确(coverage_fixture, graph_fixture):
    """汇总按并集去重，同一用例被两个文件覆盖只计一次"""
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="run")
    mapper.load()
    _, graph = graph_fixture

    selector = RegressionSelector(mapper, graph)
    result = selector.select(["src/b.py", "src/a.py"])
    summary = result["汇总"]

    assert summary["修改文件数"] == 2
    assert summary["用例总数"] == 3
    # b.py 覆盖 test_one+test_two；a.py 覆盖 test_one → 并集 2 条（去重生效）
    assert summary["选中用例数"] == 2
    assert summary["汇总选中比例"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["漏检率"] == 0.0


def test_选择器_叶子模块_影响面含唯一依赖方不漏计(graph_fixture):
    """src.a.py 的唯一依赖方是 src.b.py，影响面必须算出 1 个模块

    回归守卫：影响面为空的错误实现会让"改 a.py 也要跑 b.py 的用例"
    这条规则静默失效，且叶子模块（入度 1）是最容易被写错的一档。
    """
    _, graph = graph_fixture
    mapper = _StubMapper({"src/a.py": {"tests/t::test_a"}})
    selector = RegressionSelector(mapper, graph)

    item = selector.select_for_file("src/a.py")

    assert item["模块名"] == "src.a"
    assert item["影响面模块数"] == 1
    assert item["影响面模块"] == ["src.b"]


def test_选择器_文件不在依赖图内_影响面为空不抛异常(coverage_fixture, graph_fixture):
    """覆盖率里有、但 import 图里没有的文件走空影响面分支，不应崩溃"""
    project_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=project_root, phase="run")
    mapper.load()
    _, graph = graph_fixture

    selector = RegressionSelector(mapper, graph)
    # src/conftest_helper.py 只在 setup 阶段被覆盖，run 口径下无覆盖；
    # 用 src/c.py 验证"图内有、run 口径零覆盖"的组合
    item = selector.select_for_file("src/c.py")

    assert item["文件"] == "src/c.py"
    assert item["直接覆盖用例数"] == 1
    assert item["选中用例"] == ["tests/test_b.py::test_three"]
    # src.c 不在 graph_fixture 的模块集合里 → 模块名为空、影响面 0
    assert item["模块名"] == ""
    assert item["影响面模块数"] == 0


def test_代表文件挑选_按形态规则产出指定数量且不重复(graph_fixture, coverage_fixture):
    """自动挑选应产出去重后的 N 个文件，且都是图内真实文件"""
    cov_root, coverage_path = coverage_fixture
    mapper = CoverageMapper(coverage_path, project_root=cov_root, phase="run")
    mapper.load()
    graph_root, graph = graph_fixture

    picked = pick_representative_files(graph, mapper, limit=4)

    assert len(picked) == 4
    assert len(set(picked)) == 4
    assert all(path in graph.modules.values() for path in picked)
    assert all((graph_root / path).is_file() for path in picked)


# ===========================================================================
# CLI
# ===========================================================================


def test_cli_参数解析_默认值与显式覆盖均正确():
    """默认口径（run 阶段 / src / text）与显式传参都要落到 argparse 结果上"""
    defaults = parse_args([])
    assert defaults.coverage is None
    assert defaults.src_dir == "src"
    assert defaults.tests_dir == "tests"
    assert defaults.phase == "run"
    assert defaults.output == "text"
    assert defaults.modified_files is None
    assert defaults.stats_only is False

    explicit = parse_args(
        [
            "--coverage", "out/.coverage",
            "--phase", "all",
            "--output", "json",
            "--modified-files", "src/a.py,src/b.py",
            "--stats-only",
        ]
    )
    assert explicit.coverage == "out/.coverage"
    assert explicit.phase == "all"
    assert explicit.output == "json"
    assert explicit.modified_files == "src/a.py,src/b.py"
    assert explicit.stats_only is True


def test_cli_非法阶段值_解析阶段即报错():
    """非法 enum 值应在 argparse 层就拒掉，不进业务逻辑"""
    with pytest.raises(SystemExit):
        parse_args(["--phase", "notaphase"])


def test_cli_缺覆盖率文件_返回非零退出码不抛异常(tmp_path, monkeypatch):
    """数据缺失要转成退出码 1，而不是让栈回溯打到用户脸上"""
    monkeypatch.setattr(
        "scripts.ast_regression_poc.PROJECT_ROOT", tmp_path
    )
    monkeypatch.setattr(
        "scripts.ast_regression_poc.DEFAULT_COVERAGE_CANDIDATES", ("absent.db",)
    )
    assert main(["--coverage", str(tmp_path / "missing.db")]) == 1


def test_cli_文本与JSON双输出_退出码均为0(coverage_fixture, graph_fixture, capsys, monkeypatch):
    """两种输出格式都要能跑通并产出可解析内容"""
    project_root, coverage_path = coverage_fixture
    graph_root, _ = graph_fixture
    monkeypatch.setattr("scripts.ast_regression_poc.PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        "scripts.ast_regression_poc.DEFAULT_COVERAGE_CANDIDATES", (str(coverage_path),)
    )
    monkeypatch.setattr(
        "scripts.ast_regression_poc.DEFAULT_REPRESENTATIVE_FILES",
        ("src/util.py", "src/app.py"),
    )

    assert main(["--output", "json", "--modified-files", "src/util.py"]) == 0
    payload = capsys.readouterr().out
    assert '"选中比例"' in payload

    assert main(["--stats-only", "--output", "json"]) == 0
    assert '"覆盖率映射表统计"' in capsys.readouterr().out
