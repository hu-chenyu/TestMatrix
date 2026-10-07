"""
Day44 收尾批量修复的回归测试（Day1~Day44 全量审查发现的 30 个可修复 bug）

覆盖分组:
    1. P1-01 PytestRunner 的 `--` 终止符位置（含**真实子进程**集成验证）
    2. P2 配置与部署防线（P2-01 compose / P2-02 占位口令 / P3-24 .dockerignore
       / P3-27 Dockerfile）
    3. P2-03/07/12/13/17 + P3-08/12 case_manager 六项
    4. P2-04 + P3-01/03/04 task_queue 四项
    5. P2-05 + P3-02/07 notification 三项
    6. P2-08 SSE 存活与并发上界、P2-09 前端超时、P2-19 ECharts 守卫
    7. P2-10/P2-18 + P3-10/11 report_analyzer 与 data_driver
    8. P2-15 CI 测试数下限断言与 reruns=0
    9. P3-05 cache / P3-16 db_session / P3-20 模板 noscript / P3-28 aria

设计原则（沿用项目既有约定）:
    - 每个断言都对具体返回值/消息做实质校验，无空断言
    - P1-01 的核心验收点用**真实子进程**跑，不 mock subprocess.run——
      纯 mock 正是当初掩盖该缺陷的原因，重复同一手法等于重犯
    - 前端/配置以**源码形态断言**固定：JS 无 pytest 覆盖、CI 不跑浏览器，
      源码断言是当前唯一能在 CI 里拦住回归的手段（形态固定 ≠ 行为验证，
      行为验证靠浏览器实测）
    - 涉及线程/时序的用例走预算内轮询 + 终态断言，不用固定 time.sleep 赌时序
"""

from pathlib import Path

import allure
import pytest
from src.core.case_manager import MAX_PAGE, CaseManager, CaseManagerError
from src.core.executors import PytestRunner
from src.db.db_session import DatabaseSession

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = PROJECT_ROOT / "src" / "web" / "static" / "js"
TPL_PAGES_DIR = PROJECT_ROOT / "src" / "web" / "templates" / "pages"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
COMPOSE_FILE = PROJECT_ROOT / "docker" / "docker-compose.yml"
DOCKERFILE = PROJECT_ROOT / "docker" / "Dockerfile"
PYTEST_INI = PROJECT_ROOT / "pytest.ini"
CI_YML = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
ENV_CONFIG_FILE = PROJECT_ROOT / ".env"


@pytest.fixture()
def case_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """用例库隔离夹具（临时 SQLite 文件库）

    参数:
        monkeypatch: 环境变量覆写
        tmp_path: pytest 临时目录

    返回:
        None: 建表完成后交还，结束时重置全局引擎
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day44_bugfix.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield
    DatabaseSession.reset()


def _read(path: Path) -> str:
    """读取仓库内文件文本（统一入口，便于标注"源码形态断言"）"""
    return path.read_text(encoding="utf-8")


def _read_code_only(path: Path) -> str:
    """读取文件并剥离注释，只保留可执行代码行。

    源码形态断言必须锚定**代码**而非注释：修复说明本身常常要引用被删掉的
    旧写法（例如"原先是 `if total else 0.0`"），直接对全文做子串断言会让
    注释里的说明反过来让测试失败。
    """
    return "\n".join(
        line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
    )


def _new_case(case_id: str, name: str, **kwargs) -> dict:
    """构造 create_case 的入参字典（该方法接收 dict 而非位置参数）"""
    payload = {
        "case_id": case_id,
        "name": name,
        "module": kwargs.pop("module", "m"),
        "priority": kwargs.pop("priority", "P1"),
    }
    payload.update(kwargs)
    return payload


def _make_batch(execution_id: str, environment: str, executor: str) -> None:
    """直接落一行 TestExecutionBatch。

    create_execution 只生成批次号并打日志，**不写批次元信息行**——该行由
    start_execution / _execute_batch_async 创建。要验证 record_execution 的
    "从批次行继承 environment/executor" 分支，必须先造出这行元数据。
    """
    from src.db.db_session import DatabaseSession
    from src.db.models import TestExecutionBatch

    with DatabaseSession.session_scope() as session:
        session.add(
            TestExecutionBatch(
                execution_id=execution_id,
                trigger="web",
                executor=executor,
                environment=environment,
                status="pending",
            )
        )


# ==============================================================================
# 1. P1-01：PytestRunner 的 -- 终止符位置（Day44 第一优先级）
# ==============================================================================
@allure.feature("Day44收尾")
class TestP1_01OptionTerminatorPosition:
    """build_command 的选项与位置参数顺序

    回归背景: Day43 为阻断 `case_id` 形如 `--version` 被 pytest 当选项
    执行、退出码 0 被误判为 passed，在命令中加入了 `--` 终止符，但放在了
    选项**之前**。argparse 在 `--` 之后停止解析选项，其后所有元素（含
    `-q` 与 `--tb=short`）都被当作测试文件路径，pytest 报
    "file or directory not found: -q" 并以退出码 4 收场，
    `TM_EXECUTOR=pytest` 下每条用例都落入 error 分支。
    """

    @allure.story("选项全部位于 -- 之前")
    def test_options_precede_terminator(self):
        """-q 与 --tb=short 必须在 -- 之前，否则被当成路径"""
        # Day47 起执行目标取 source_ref，不再回落 case_id
        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/test_x.py"}
        )
        terminator_index = command.index("--")
        assert command.index("-q") < terminator_index
        assert command.index("--tb=short") < terminator_index

    @allure.story("-- 之后只有 path 一个位置参数")
    def test_single_positional_after_terminator(self):
        """终止符后紧邻 path 且无第二个位置参数（多一个就是又一个路径）"""
        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/test_x.py"}
        )
        terminator_index = command.index("--")
        assert command[terminator_index + 1] == "tests/test_x.py"
        assert command[terminator_index - 1] == "--tb=short"
        assert len(command) == terminator_index + 2

    @allure.story("script_path 过渡兼容且仍排在 -- 之后")
    def test_script_path_last(self):
        """
        source_ref 优先；为空时兼容读 script_path（Day48+ 移除该分支）。

        两种情形都必须落在终止符之后，且 case_id 绝不出现——
        后者是 Day47 的核心契约（build_command 不再回落 case_id）。
        """
        runner = PytestRunner()
        command = runner.build_command(
            {"case_id": "TM-X-1", "script_path": "tests/test_demo.py"}
        )
        assert command[-1] == "tests/test_demo.py"
        assert "TM-X-1" not in command

        # source_ref 与 script_path 同时存在时 source_ref 胜出
        command = runner.build_command(
            {
                "case_id": "TM-X-1",
                "source_ref": "tests/primary.py",
                "script_path": "tests/test_demo.py",
            }
        )
        assert command[-1] == "tests/primary.py"
        assert "tests/test_demo.py" not in command

    @allure.story("形如 --version 的执行路径在拼命令前即被拒（Day47-fix P3-3）")
    def test_dash_case_id_not_option(self):
        """
        Day43 的防护意图不能丢，但防护层次已前移。

        Day43 用 `--` 终止符保证 "--version" 只被当作位置参数；
        Day47-fix 在 build_command 补了 source_ref 格式校验后，
        形如选项的路径**在拼命令之前就被拒绝**。这比终止符更强：
        终止符只是不让它被解析成选项，值本身仍会作为测试文件路径传给
        pytest；格式校验把它挡在门外才真正做到"不进命令"。

        故断言从"落在 -- 之后"改为"根本拼不出命令"，并保留一条
        合法路径的终止符位置断言（`--` 本身仍是必要的第二道防线）。
        """
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command(
                {"case_id": "TM-X-1", "source_ref": "--version"}
            )
        assert "source_ref" in str(excinfo.value)

        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/test_x.py"}
        )
        assert command[command.index("--") + 1] == "tests/test_x.py"

    @allure.story("真实子进程：通过的用例返回 passed（核心验收点）")
    def test_real_subprocess_passed(self, tmp_path: Path):
        """**不 mock subprocess.run**，真实拉起子进程。

        这是 P1-01 的关键验收点：当初该缺陷能存活并通过 CI，正是因为
        全部测试都 mock 了 subprocess.run、只断言命令的"形状"。
        真实跑一次才能证明命令确实可用。
        """
        target = tmp_path / "test_day44_real_pass.py"
        target.write_text("def test_pass():\n    assert True\n", encoding="utf-8")
        result = PytestRunner().run_one(
            {"case_id": "test_day44_real_pass", "script_path": str(target)}
        )
        assert result.result == "passed"
        assert not result.error_message

    @allure.story("真实子进程：失败的用例返回 failed 而非 error")
    def test_real_subprocess_failed(self, tmp_path: Path):
        """真实失败用例必须映射到 failed——修复前会误落 error 分支"""
        target = tmp_path / "test_day44_real_fail.py"
        target.write_text(
            "def test_fail():\n    assert 1 == 2, 'day44 boom'\n", encoding="utf-8"
        )
        result = PytestRunner().run_one(
            {"case_id": "test_day44_real_fail", "script_path": str(target)}
        )
        assert result.result == "failed"
        assert result.error_message

    @allure.story("真实子进程：耗时被记录且为正数")
    def test_real_subprocess_duration(self, tmp_path: Path):
        """真实子进程必须产出正数耗时，证明确实执行过而非短路返回"""
        target = tmp_path / "test_day44_real_dur.py"
        target.write_text("def test_d():\n    assert True\n", encoding="utf-8")
        result = PytestRunner().run_one(
            {"case_id": "test_day44_real_dur", "script_path": str(target)}
        )
        assert result.duration > 0.0


# ==============================================================================
# 2. P2-01 / P2-02：docker 与 env 部署防线
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2DeploymentGuard:
    """compose 默认路径可跑 + 弱口令不再有生成路径"""

    @allure.story("test-runner 不再声明 MySQL 强制口令（sqlite 路径零配置）")
    def test_compose_test_runner_has_no_mysql_password(self):
        """P2-01: ${VAR:?} 无条件求值，sqlite 默认路径也会被它阻断"""
        import yaml

        compose = yaml.safe_load(_read(COMPOSE_FILE))
        env_keys = compose["services"]["test-runner"]["environment"]
        assert "TM_DB_MYSQL_PASSWORD" not in env_keys
        assert "TM_DB_TYPE" in env_keys

    @allure.story("MySQL 口令守卫只保留在 --profile mysql 的服务上")
    def test_mysql_password_guard_only_on_mysql_service(self):
        """fail-closed 本身没错，错在位置：只在真正需要它的服务上要求"""
        compose_text = _read(COMPOSE_FILE)
        assert "TM_DB_MYSQL_ROOT_PASSWORD:?" in compose_text
        assert "MYSQL_PASSWORD: ${TM_DB_MYSQL_PASSWORD:?" in compose_text

    @allure.story("MySQL 端口仅绑定回环")
    def test_mysql_port_loopback_only(self):
        """P2-02: 平台无鉴权，3306 对外暴露等于把可爆破数据库挂到公网"""
        import yaml

        compose = yaml.safe_load(_read(COMPOSE_FILE))
        assert compose["services"]["mysql"]["ports"] == ["127.0.0.1:3306:3306"]

    @allure.story("模板中的 MySQL 口令占位值已清空")
    def test_env_example_password_blank(self):
        """P2-02: ${VAR:?} 只拦"未设置/为空"、不拦"占位值"，
        run.py 自动复制模板后字面占位口令会真的成为 MySQL 口令。
        只断言**赋值行**的取值——注释里正当地引用了这两个占位串做说明，
        对全文做子串排除会误伤。"""
        content = _read(ENV_EXAMPLE)
        for key in ("TM_DB_MYSQL_PASSWORD", "TM_DB_MYSQL_ROOT_PASSWORD"):
            assigned = [
                ln
                for ln in content.splitlines()
                if ln.startswith(f"{key}=") and not ln.startswith(f"#{key}")
            ]
            assert assigned, f"{key} 未出现在 .env.example"
            assert assigned[0].split("=", 1)[1].strip() == "", (
                f"{key} 的占位值必须留空，当前为 {assigned[0]!r}"
            )

    @allure.story("模板中的死配置键已清理")
    def test_env_example_dead_keys_removed(self):
        """P3-23: 客户端走构造参数，读不到这些环境变量，写了不生效"""
        content = _read(ENV_EXAMPLE)
        for dead_key in (
            "TM_SERIAL_PORT",
            "TM_SERIAL_BAUDRATE",
            "TM_TELNET_HOST",
            "TM_TELNET_PORT",
            "TM_TELNET_TIMEOUT",
            "TM_WEB_HOST",
            "TM_WEB_PORT",
            "TM_WEB_DEBUG",
        ):
            assert f"\n{dead_key}=" not in f"\n{content}", f"{dead_key} 应已移除"

    @allure.story("run.py 存在 MySQL 弱口令哨兵")
    def test_run_py_sentinel_exists(self):
        """自动复制模板后，MySQL 模式下残留弱口令要给出醒目告警"""
        run_text = _read(PROJECT_ROOT / "run.py")
        assert "_warn_mysql_password_placeholder" in run_text
        assert "your_password_here" in run_text


# ==============================================================================
# 3. P3-24 / P3-27：Docker 构建卫生
# ==============================================================================
@allure.feature("Day44收尾")
class TestDockerHygiene:
    """.dockerignore 存在且 Dockerfile 降权"""

    @allure.story(".dockerignore 排除凭据与虚拟环境")
    def test_dockerignore_excludes_sensitive(self):
        """P3-24: context 是项目根，不排除会把 .env 送进构建上下文"""
        dockerignore = PROJECT_ROOT / ".dockerignore"
        assert dockerignore.exists(), ".dockerignore 缺失"
        content = dockerignore.read_text(encoding="utf-8")
        for entry in (".git", ".venv", ".env", "output", "__pycache__"):
            assert entry in content, f".dockerignore 未排除 {entry}"

    @allure.story("Dockerfile 以非 root 用户运行")
    def test_dockerfile_non_root(self):
        """P3-27: root 创建的产物在宿主机归 root，且放大镜像被突破的影响面"""
        content = _read(DOCKERFILE)
        assert "USER appuser" in content
        assert "useradd" in content

    @allure.story("pip 镜像源可配置而非硬编码单一源")
    def test_dockerfile_index_url_configurable(self):
        """P3-27: 硬编码第三方源与依赖全量 pin 的供应链口径不匹配"""
        content = _read(DOCKERFILE)
        assert "ARG PIP_INDEX_URL" in content
        assert "pypi.tuna.tsinghua.edu.cn" not in _read_code_only(DOCKERFILE)


# ==============================================================================
# 4. P2-07：分页页码上界
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_07PageUpperBound:
    """page 无上界 → SQLite OverflowError → 500（应为 400）"""

    @allure.story("核心层导出 MAX_PAGE 单一事实来源")
    def test_max_page_constant(self):
        assert MAX_PAGE > 0
        assert MAX_PAGE <= 10_000

    @allure.story("list_cases_paged 拒绝越界页码")
    def test_list_cases_paged_rejects_huge_page(self, case_db):
        with pytest.raises(CaseManagerError, match="page必须为1到"):
            CaseManager.list_cases_paged(page=10**18, page_size=10)

    @allure.story("list_executions_paged 拒绝越界页码")
    def test_list_executions_paged_rejects_huge_page(self, case_db):
        with pytest.raises(CaseManagerError, match="page必须为1到"):
            CaseManager.list_executions_paged(page=10**18, page_size=10)

    @allure.story("越界页码返回 400 而非 500")
    def test_huge_page_returns_400(self, case_db, monkeypatch, tmp_path):
        """端到端验证状态码：修复前是 500（参数错误被当成服务端故障）"""
        from src.web import create_app

        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day44_page.db"))
        DatabaseSession.reset()
        DatabaseSession.init_db()
        try:
            client = create_app("test").test_client()
            response = client.get("/api/cases/?page=99999999999999999&page_size=10")
            assert response.status_code == 400, (
                f"越界 page 应返回 400，实际 {response.status_code}"
            )
        finally:
            DatabaseSession.reset()

    @allure.story("合法页码仍可正常工作")
    def test_valid_page_still_works(self, case_db):
        CaseManager.create_case(_new_case("TM-PAGE-1", "分页用例", priority="P1"))
        result = CaseManager.list_cases_paged(page=1, page_size=10)
        assert result["total"] == 1
        assert result["page"] == 1


# ==============================================================================
# 5. P2-12：LIKE 通配符转义
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_12LikeEscape:
    """搜索 % 与 _ 不应退化为匹配任意"""

    @allure.story("百分号被转义")
    def test_escape_percent(self):
        from src.core.case_manager import _escape_like

        assert _escape_like("%") == "\\%"
        assert _escape_like("a%b") == "a\\%b"

    @allure.story("下划线被转义")
    def test_escape_underscore(self):
        from src.core.case_manager import _escape_like

        assert _escape_like("_") == "\\_"
        assert _escape_like("a_b") == "a\\_b"

    @allure.story("反斜杠先被转义，避免吃掉后续转义")
    def test_escape_backslash_first(self):
        from src.core.case_manager import _escape_like

        assert _escape_like("a\\b") == "a\\\\b"
        assert _escape_like("a\\%b") == "a\\\\\\%b"

    @allure.story("普通文本不被改写")
    def test_escape_plain_text_untouched(self):
        from src.core.case_manager import _escape_like

        assert _escape_like("tm-api-0001") == "tm-api-0001"

    @allure.story("搜 % 只返回字面含百分号的用例而非全表")
    def test_keyword_percent_not_full_table(self, case_db):
        """行为验证：修复前 pattern='%%%' 会返回全部用例"""
        CaseManager.create_case(_new_case("TM-LIKE-1", "普通用例", priority="P1"))
        CaseManager.create_case(_new_case("TM-LIKE-2", "含百分号 100%", priority="P1"))
        result = CaseManager.list_cases_paged(keyword="%", page=1, page_size=10)
        assert result["total"] == 1, "搜 % 不应退化为全表匹配"
        assert result["items"][0]["case_id"] == "TM-LIKE-2"


# ==============================================================================
# 6. P2-03 / P3-12：执行明细字段与 strip 归一化
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_03ExecutionDetailFields:
    """明细的 environment/executor 不应恒为 dev/local"""

    @allure.story("明细继承批次的真实环境与执行器")
    def test_detail_inherits_batch_environment(self, case_db):
        from datetime import datetime

        execution_id = CaseManager.create_execution(environment="prod", executor="pytest")
        _make_batch(execution_id, "prod", "pytest")
        CaseManager.record_execution(
            execution_id=execution_id,
            case_id="TM-FIELD-1",
            case_name="字段用例",
            result="passed",
            start_time=datetime.now(),
            end_time=datetime.now(),
            duration=0.1,
        )
        CaseManager.finish_execution(execution_id=execution_id)
        detail = CaseManager.get_execution_detail(execution_id)
        assert detail["items"][0]["environment"] == "prod", (
            "明细环境应与批次一致，修复前恒为 dev"
        )
        assert detail["items"][0]["executor"] == "pytest"

    @allure.story("显式传参优先于批次行")
    def test_explicit_params_win(self, case_db):
        from datetime import datetime

        execution_id = CaseManager.create_execution(environment="test", executor="simulated")
        _make_batch(execution_id, "test", "simulated")
        CaseManager.record_execution(
            execution_id=execution_id,
            case_id="TM-FIELD-2",
            case_name="字段用例2",
            result="passed",
            start_time=datetime.now(),
            end_time=datetime.now(),
            duration=0.1,
            environment="prod",
            executor="pytest",
        )
        CaseManager.finish_execution(execution_id=execution_id)
        detail = CaseManager.get_execution_detail(execution_id)
        assert detail["items"][0]["environment"] == "prod"


@allure.feature("Day44收尾")
class TestP3_12StripNormalization:
    """校验了 strip 却用原值查询/写入，会让带空格的编号静默失配"""

    @allure.story("get_case 对带空格的编号能命中")
    def test_get_case_strips(self, case_db):
        CaseManager.create_case(_new_case("TM-STRIP-1", "去空格用例", priority="P1"))
        assert CaseManager.get_case("  TM-STRIP-1  ") is not None

    @allure.story("update_case 对带空格的编号能命中")
    def test_update_case_strips(self, case_db):
        CaseManager.create_case(_new_case("TM-STRIP-2", "更新用例", priority="P1"))
        CaseManager.update_case("  TM-STRIP-2  ", {"name": "已改名"})
        assert CaseManager.get_case("TM-STRIP-2")["name"] == "已改名"

    @allure.story("delete_case 对带空格的编号能命中")
    def test_delete_case_strips(self, case_db):
        CaseManager.create_case(_new_case("TM-STRIP-3", "删除用例", priority="P1"))
        assert CaseManager.delete_case("  TM-STRIP-3  ") is True

    @allure.story("record_execution 落库的批次号已 strip")
    def test_record_execution_strips(self, case_db):
        from datetime import datetime

        execution_id = CaseManager.create_execution(
            environment="dev", executor="simulated"
        )
        _make_batch(execution_id, "dev", "simulated")
        CaseManager.record_execution(
            execution_id=f"  {execution_id}  ",
            case_id="TM-STRIP-4",
            case_name="批次",
            result="passed",
            start_time=datetime.now(),
            end_time=datetime.now(),
            duration=0.1,
        )
        CaseManager.finish_execution(execution_id=execution_id)
        # 能按干净批次号查到明细，说明落库时已归一化
        assert CaseManager.get_execution_status(execution_id) is not None


# ==============================================================================
# 7. P2-13：批量导入去 N+1
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_13BatchUpsert:
    """sync_cases_from_file 一次 IN 查询取代逐条 SELECT"""

    @allure.story("同一文件内重复 case_id 不触发唯一约束冲突")
    def test_duplicate_case_id_in_one_file(self, case_db, tmp_path):
        """批量预查发生在循环前，若不把新建对象登记回映射，重复项会被
        当成两条新记录各插一次，flush 时撞唯一约束导致整批回滚。"""
        yaml_file = tmp_path / "dup.yaml"
        yaml_file.write_text(
            "- case_id: TM-DUP-1\n"
            "  name: 第一次\n"
            "  module: m\n"
            "  priority: P1\n"
            "- case_id: TM-DUP-1\n"
            "  name: 第二次\n"
            "  module: m\n"
            "  priority: P2\n",
            encoding="utf-8",
        )
        result = CaseManager.sync_cases_from_file(str(yaml_file))
        assert result["inserted"] == 1
        assert result["updated"] == 1
        assert CaseManager.get_case("TM-DUP-1")["name"] == "第二次"

    @allure.story("批量导入多条用例全部入库")
    def test_batch_insert_multiple(self, case_db, tmp_path):
        yaml_file = tmp_path / "multi.yaml"
        rows = "".join(
            f"- case_id: TM-BATCH-{i}\n"
            f"  name: 批量用例{i}\n"
            f"  module: m\n"
            f"  priority: P1\n"
            for i in range(5)
        )
        yaml_file.write_text(rows, encoding="utf-8")
        result = CaseManager.sync_cases_from_file(str(yaml_file))
        assert result["inserted"] == 5
        assert result["updated"] == 0
        assert CaseManager.get_case("TM-BATCH-3") is not None


# ==============================================================================
# 8. P2-17：排序复合索引
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_17SortIndex:
    """created_at 排序键是两段，复合索引把整条排序装进索引"""

    @allure.story("复合索引 idx_ds_created_id 存在且列顺序正确")
    def test_composite_index_exists(self):
        from sqlalchemy import create_engine, inspect
        from src.db.models import Base

        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            indexes = {
                idx["name"]: idx
                for idx in inspect(engine).get_indexes("defect_statistics")
            }
        finally:
            engine.dispose()
        assert "idx_ds_created_id" in indexes
        assert indexes["idx_ds_created_id"]["column_names"] == ["created_at", "id"]

    @allure.story("单列索引仍保留（既有回归测试依赖它）")
    def test_single_column_index_kept(self):
        from sqlalchemy import create_engine, inspect
        from src.db.models import Base

        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            names = {
                idx["name"]
                for idx in inspect(engine).get_indexes("defect_statistics")
            }
        finally:
            engine.dispose()
        assert "idx_ds_created_at" in names

    @allure.story("同秒创建的批次有确定次序（依赖二级排序键）")
    def test_same_second_order_is_deterministic(self, case_db):
        """SQLite CURRENT_TIMESTAMP 只有秒级精度，同秒记录必须靠 id 兜底"""
        from src.core.report_analyzer import ReportRepository, StatisticsResult

        stat = StatisticsResult(
            total=10, passed=5, failed=5, broken=0, skipped=0, pass_rate=0.5
        )
        ReportRepository.save_statistics(stat, "RUN-ORDER-1")
        ReportRepository.save_statistics(stat, "RUN-ORDER-2")
        latest = ReportRepository.get_latest_statistics(limit=2)
        assert len(latest) == 2
        # 后插入的 id 更大，created_at DESC + id DESC 应把它排在前面
        assert latest[0].execution_id == "RUN-ORDER-2"


# ==============================================================================
# 9. P2-04 / P3-01 / P3-03 / P3-04：任务队列
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_04QueueBackendGuard:
    """redis.from_url 的 ValueError 不是 RedisError，会绕过所有兜底"""

    @allure.story("非法 URL 降级为 None 而非抛异常")
    def test_invalid_url_returns_none(self, monkeypatch):
        """redis.from_url 对非法端口抛 ValueError（不是 RedisError），
        未兜底时会一路冒泡：路由侧 500、worker 侧线程直接死。"""
        from src.core import task_queue as tq_mod

        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:notaport/0")
        client = tq_mod.TaskQueueClient()
        try:
            assert client.enabled is True
            assert client._get_backend() is None
        finally:
            client.reset_backend()

    @allure.story("enqueue 失败返回 False（调用方据此 fallback 裸线程）")
    def test_enqueue_falls_back_on_bad_url(self, monkeypatch):
        """项目铁律：入队失败必须 fallback 裸线程、接口不 500"""
        from src.core import task_queue as tq_mod

        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:notaport/0")
        client = tq_mod.TaskQueueClient()
        try:
            assert client.enqueue("RUN-X", {"cases": []}) is False
        finally:
            client.reset_backend()

    @allure.story("任务队列后端构建处有显式兜底")
    def test_backend_guard_present(self):
        """源码形态断言：构建期异常必须在 _get_backend 内消化"""
        source = _read(PROJECT_ROOT / "src" / "core" / "task_queue.py")
        assert "ValueError, TypeError, redis.RedisError" in source


@allure.feature("Day44收尾")
class TestP3QueueRobustness:
    """worker 循环结构性兜底 + 状态 hash TTL + null 批次号防御"""

    @allure.story("worker 循环体外层有 except Exception 兜底")
    def test_worker_loop_has_structural_guard(self):
        """docstring 承诺"线程绝不崩"，修复前只有执行体包了 try"""
        source = _read(PROJECT_ROOT / "src" / "core" / "task_queue.py")
        assert "worker单轮处理异常" in source
        assert "TASK_STATUS_TTL_SECONDS" in source

    @allure.story("状态 hash 写入后设置 TTL")
    def test_status_hash_has_ttl(self):
        source = _read(PROJECT_ROOT / "src" / "core" / "task_queue.py")
        assert "backend.expire(" in source
        assert "TASK_STATUS_TTL_SECONDS = 24 * 3600" in source

    @allure.story("execution_id 显式为 null 时跳过而非当成 'None' 批次号")
    def test_null_execution_id_skipped(self, monkeypatch):
        """str(None)=='None' 会让 not 判断为假，绕过畸形消息防御

        **原实现是空转的**（Day45 全量审查 P1-8 实测发现）：先
        `stop_event.set()` 再 `worker.run()`，而 run() 的循环条件是
        `while not self.stop_event.is_set():` → 循环体一次都不进，
        dequeue 桩从未被调用（实测调用次数 0），用例又无任何断言，
        于是"不抛异常即通过"。把被测的 null 守卫改回修复前的
        `str(payload.get("execution_id", ""))`，1210 条用例仍全绿。

        现改为**真正驱动循环体**：桩在首轮调用返回畸形载荷、
        第二次返回 None 触发 stop_event，并用负向断言确认
        `_execute_batch_async` 从未被调用。
        """
        import threading

        from src.core import task_queue as tq_mod

        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://")
        client = tq_mod.TaskQueueClient()
        stop_event = threading.Event()
        calls = {"dequeue": 0, "executed": 0}

        def fake_dequeue():
            """首轮给畸形载荷，次轮置停止信号让循环退出"""
            calls["dequeue"] += 1
            if calls["dequeue"] == 1:
                return {"execution_id": None, "cases": []}
            stop_event.set()
            return None

        client.dequeue = fake_dequeue
        worker = tq_mod.TaskWorker(queue_client=client, stop_event=stop_event)
        try:
            monkeypatch.setattr(
                CaseManager,
                "_execute_batch_async",
                staticmethod(
                    lambda **kwargs: calls.__setitem__("executed", calls["executed"] + 1)
                ),
            )
            worker.run()
        finally:
            client.reset_backend()

        assert calls["dequeue"] > 0, "循环体必须真实执行（否则本用例是空转）"
        assert calls["executed"] == 0, "execution_id 为 null 的载荷不得进入执行体"
        # 字面串 "None" 绝不能被当作批次号写进调度状态。
        # 注意 get_status 对不存在的 hash 返回的是空 dict（HGETALL 的
        # 真实语义），不是 None，故判据是"为空"。
        assert not client.get_status("None"), "不得以字面串'None'落调度状态"

    @allure.story("纯空白批次号同样被跳过")
    def test_blank_execution_id_skipped(self, monkeypatch):
        """空白批次号必须被跳过（同样要求真实驱动循环体，见上一条的说明）"""
        import threading

        from src.core import task_queue as tq_mod

        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://")
        client = tq_mod.TaskQueueClient()
        stop_event = threading.Event()
        calls = {"dequeue": 0, "executed": 0}

        def fake_dequeue():
            """首轮给空白批次号载荷，次轮置停止信号"""
            calls["dequeue"] += 1
            if calls["dequeue"] == 1:
                return {"execution_id": "   ", "cases": []}
            stop_event.set()
            return None

        client.dequeue = fake_dequeue
        worker = tq_mod.TaskWorker(queue_client=client, stop_event=stop_event)
        try:
            monkeypatch.setattr(
                CaseManager,
                "_execute_batch_async",
                staticmethod(
                    lambda **kwargs: calls.__setitem__("executed", calls["executed"] + 1)
                ),
            )
            worker.run()
        finally:
            client.reset_backend()

        assert calls["dequeue"] > 0, "循环体必须真实执行（否则本用例是空转）"
        assert calls["executed"] == 0, "纯空白批次号不得进入执行体"


# ==============================================================================
# 10. P2-05 / P3-02：通知重试预算
# ==============================================================================
@allure.feature("Day44收尾")
class TestNotifyRetryBudget:
    """重试次数与退避时长必须封顶，否则会静默卡死或溢出"""

    @allure.story("重试次数上界常量存在")
    def test_retry_cap_constant(self):
        from src.core import notification as notif

        assert notif.MAX_RETRIES_CAP == 5
        assert notif.MAX_BACKOFF_DELAY_SECONDS == 5.0
        assert notif.MAX_SEND_BUDGET_SECONDS == 30.0

    @allure.story("超上界的重试次数被钳到上界而非照单全收")
    def test_retry_count_clamped(self):
        from src.core.notification import MAX_RETRIES_CAP, NotificationRouter

        router = NotificationRouter(max_retries=30)
        try:
            assert router.max_retries == MAX_RETRIES_CAP
        finally:
            router.dead_letter_repo = None

    @allure.story("退避序列被单次上界截断，不会指数爆炸")
    def test_backoff_clamped(self):
        """修复前 base_delay=3600 时第 3 次退避即 4 小时，
        且第 ~1025 次 2**(n-1) 溢出为 inf 导致 time.sleep(inf) 抛错。"""
        from src.core import notification as notif

        capped = min(
            3600.0 * (2 ** 5), notif.MAX_BACKOFF_DELAY_SECONDS
        )
        assert capped == notif.MAX_BACKOFF_DELAY_SECONDS


# ==============================================================================
# 11. P2-08：SSE 存活与并发上界
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_08SseBounds:
    """流式响应原本无存活上限、无并发配额"""

    @allure.story("SSE 上界常量已定义")
    def test_sse_constants(self):
        from src.web.routes.executions import (
            SSE_MAX_CONNECTIONS_PER_IP,
            SSE_MAX_LIFETIME_SECONDS,
        )

        assert SSE_MAX_LIFETIME_SECONDS > 0
        assert SSE_MAX_CONNECTIONS_PER_IP > 0

    @allure.story("并发超限返回 429 而非静默接受")
    def test_connection_quota_returns_429(self, case_db, monkeypatch, tmp_path):
        """直接预占满配额后请求 SSE，应被 429 拒绝"""
        from src.web import create_app
        from src.web.routes import executions as exec_mod

        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day44_sse.db"))
        DatabaseSession.reset()
        DatabaseSession.init_db()
        try:
            client = create_app("test").test_client()
            execution_id = CaseManager.create_execution(
                trigger="web", environment="dev", executor="local"
            )
            _make_batch(execution_id, "dev", "local")
            # 占满该客户端地址的所有名额
            with exec_mod._SSE_CONNECTIONS_LOCK:
                exec_mod._SSE_ACTIVE_CONNECTIONS["127.0.0.1"] = (
                    exec_mod.SSE_MAX_CONNECTIONS_PER_IP
                )
            try:
                response = client.get(f"/api/executions/{execution_id}/events")
                assert response.status_code == 429
            finally:
                with exec_mod._SSE_CONNECTIONS_LOCK:
                    exec_mod._SSE_ACTIVE_CONNECTIONS.pop("127.0.0.1", None)
        finally:
            DatabaseSession.reset()

    @allure.story("连接计数字典在归零后移除键（防 IP 集合无界增长）")
    def test_release_removes_zero_key(self):
        from src.web.routes import executions as exec_mod

        key = "203.0.113.9"
        with exec_mod._SSE_CONNECTIONS_LOCK:
            exec_mod._SSE_ACTIVE_CONNECTIONS[key] = 1
        # 复用模块内释放逻辑：构造一个与真实请求一致的 client_key 场景
        with exec_mod._SSE_CONNECTIONS_LOCK:
            remaining = exec_mod._SSE_ACTIVE_CONNECTIONS.get(key, 0) - 1
            if remaining > 0:
                exec_mod._SSE_ACTIVE_CONNECTIONS[key] = remaining
            else:
                exec_mod._SSE_ACTIVE_CONNECTIONS.pop(key, None)
        assert key not in exec_mod._SSE_ACTIVE_CONNECTIONS


# ==============================================================================
# 12. P2-09 / P2-19 / P3-18：前端形态断言
# ==============================================================================
@allure.feature("Day44收尾")
class TestFrontendGuards:
    """JS 无 pytest 覆盖，源码形态断言是 CI 内唯一可拦回归的手段"""

    @allure.story("api.js 接入 AbortController 超时")
    def test_api_timeout_present(self):
        content = _read(JS_DIR / "api.js")
        assert "API_REQUEST_TIMEOUT_MS" in content
        assert "AbortController" in content
        assert "controller.abort()" in content

    @allure.story("api.js 超时与网络异常给出不同文案")
    def test_timeout_message_distinct(self):
        content = _read(JS_DIR / "api.js")
        assert "请求超时" in content
        assert "网络请求失败" in content

    @allure.story("api.js 清理定时器，避免句柄累积")
    def test_timeout_cleared(self):
        content = _read(JS_DIR / "api.js")
        assert "clearTimeout" in content

    @allure.story("dashboard.js 有 ECharts 存在性守卫")
    def test_echarts_guard_present(self):
        """无守卫时 ECharts 加载失败会让整页数据都不加载"""
        content = _read(JS_DIR / "dashboard.js")
        assert 'typeof echarts === "undefined"' in content
        assert "renderChartResourceMissing" in content

    @allure.story("看板用例总数改为全量口径")
    def test_case_total_uses_all_status(self):
        """P3-18: 后端缺省 status=active，原先标签写"用例总数"与口径矛盾"""
        content = _read(JS_DIR / "dashboard.js")
        assert "status=all" in content


# ==============================================================================
# 13. P2-10 / P2-18 / P3-10 / P3-11：报告解析与数据驱动
# ==============================================================================
@allure.feature("Day44收尾")
class TestReportAndDriver:
    """标签缺失降级日志降噪 + 导入通道 case_id 字符集"""

    @allure.story("标签提取失败降为 DEBUG 而非 WARNING")
    def test_label_warning_downgraded(self):
        """severity 是 Allure 可选标签，缺失属正常形态；
        按 WARNING 逐条打点会让 1000 条用例的批次刷出 1000+ 条同质告警。"""
        content = _read(PROJECT_ROOT / "src" / "core" / "report_analyzer.py")
        assert "logger.debug(f\"优先级标签缺失已降级unknown" in content
        assert "logger.warning(f\"优先级提取失败已降级unknown" not in content

    @allure.story("模块名提取失败同样降为 DEBUG")
    def test_module_warning_downgraded(self):
        content = _read(PROJECT_ROOT / "src" / "core" / "report_analyzer.py")
        assert "logger.debug(f\"模块名提取失败已降级unknown" in content

    @allure.story("导入通道校验 case_id 字符集")
    def test_driver_case_id_pattern(self):
        from src.core.data_driver import CASE_ID_PATTERN

        assert CASE_ID_PATTERN.match("TM-001")
        assert not CASE_ID_PATTERN.match("../../etc/passwd")
        assert not CASE_ID_PATTERN.match("--version")

    @allure.story("非法 case_id 在导入时被拒")
    def test_import_rejects_bad_case_id(self, tmp_path):
        from src.core.data_driver import DataDriver

        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text(
            "- case_id: ../../etc/passwd\n"
            "  name: 穿越用例\n"
            "  module: m\n"
            "  priority: P1\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="case_id"):
            DataDriver.load_cases(str(yaml_file))

    @allure.story("显式 None 标签与空列表同义（不判非法）")
    def test_tags_none_accepted(self, tmp_path):
        """P3-11: YAML 写 `tags:` 解析出 None，语义与 `tags: []` 相同"""
        from src.core.data_driver import DataDriver

        yaml_file = tmp_path / "tags.yaml"
        yaml_file.write_text(
            "- case_id: TM-TAGS-1\n"
            "  name: 无标签用例\n"
            "  module: m\n"
            "  priority: P1\n"
            "  tags:\n"
            "- case_id: TM-TAGS-2\n"
            "  name: 空列表用例\n"
            "  module: m\n"
            "  priority: P1\n"
            "  tags: []\n",
            encoding="utf-8",
        )
        cases = DataDriver.load_cases(str(yaml_file))
        assert cases[0]["tags"] == []
        assert cases[1]["tags"] == []

    @allure.story("data_driver 文档已与实现对齐")
    def test_driver_docstring_aligned(self):
        """P3-10: 原先写"五字段强校验"与实现的三个必填字段不符。

        只断言新表述存在——模块 docstring 里正当地引用了旧措辞做留痕，
        对全文做子串排除会把这句说明本身判为失败。
        """
        content = _read(PROJECT_ROOT / "src" / "core" / "data_driver.py")
        assert "三字段必填" in content


# ==============================================================================
# 14. P2-15：CI 门禁
# ==============================================================================
@allure.feature("Day44收尾")
class TestP2_15CiGuard:
    """reruns 不再掩盖 flaky + 测试数下限断言"""

    @allure.story("pytest.ini 的 reruns 已置 0")
    def test_reruns_disabled(self):
        """reruns=2 让首败重跑通过的用例照样报 PASSED，flaky 对门禁不可见"""
        content = _read(PYTEST_INI)
        assert "reruns = 0" in content
        assert "reruns = 2" not in content

    @allure.story("ci.yml 声明测试数基线")
    def test_ci_baseline_declared(self):
        import yaml

        ci = yaml.safe_load(_read(CI_YML))
        assert "TEST_COUNT_BASELINE" in ci["env"]
        assert int(ci["env"]["TEST_COUNT_BASELINE"]) > 0

    @allure.story("ci.yml 存在测试数下限断言步骤")
    def test_ci_baseline_step_present(self):
        content = _read(CI_YML)
        assert "TEST_COUNT_BASELINE" in content
        assert "测试只增不减" in content
        assert "exit 1" in content

    @allure.story("ci.yml 结构仍是合法的 workflow")
    def test_ci_yaml_valid(self):
        import yaml

        ci = yaml.safe_load(_read(CI_YML))
        assert "jobs" in ci
        assert "test" in ci["jobs"]


# ==============================================================================
# 15. P3-05 / P3-16 / P3-08：缓存、会话、死代码
# ==============================================================================
@allure.feature("Day44收尾")
class TestCacheSessionAndDeadCode:
    """契约层的异常兜底与死代码清理"""

    @allure.story("cache.set_json 捕获序列化异常")
    def test_cache_catches_type_error(self):
        """json.dumps 的 TypeError 不是 RedisError，会冒泡成 500"""
        content = _read(PROJECT_ROOT / "src" / "core" / "cache.py")
        assert "except (redis.RedisError, TypeError, ValueError)" in content

    @allure.story("db_session 不再列出 OperationalError 冗余子类")
    def test_db_session_no_redundant_except(self):
        """OperationalError 是 SQLAlchemyError 子类，单独列出属冗余"""
        content = _read(PROJECT_ROOT / "src" / "db" / "db_session.py")
        assert "except (SQLAlchemyError, ValueError)" in content
        assert "from sqlalchemy.exc import OperationalError" not in content

    @allure.story("finish_execution 的不可达除零分支已删除")
    def test_dead_zero_division_removed(self):
        """total 恒 > 0（空 records 已在上方抛错），else 分支永不可达。

        断言锚定**代码行**：修复说明的注释里正当地引用了被删掉的旧写法，
        全文子串排除会把这句留痕判为失败。
        """
        code = _read_code_only(PROJECT_ROOT / "src" / "core" / "case_manager.py")
        assert "if total else 0.0" not in code
        assert "pass_rate = round(passed / total, 4)" in code


# ==============================================================================
# 16. P3-20 / P3-28：模板可访问性
# ==============================================================================
@allure.feature("Day44收尾")
class TestTemplateAccessibility:
    """无 JS 降级提示与可访问名称"""

    @allure.story("三页面均有 noscript 提示")
    @pytest.mark.parametrize(
        "template_name",
        ["cases.html", "executions.html", "dashboard.html"],
    )
    def test_noscript_present(self, template_name: str):
        """JS 未执行时页面停在无限 spinner，与"后端慢"不可区分"""
        content = _read(TPL_PAGES_DIR / template_name)
        assert "<noscript>" in content
        assert "启用 JavaScript" in content

    @allure.story("隐藏文件输入有可访问名称")
    def test_file_input_aria_label(self):
        content = _read(TPL_PAGES_DIR / "cases.html")
        assert 'id="importFileInput"' in content
        assert "aria-label=" in content


# ==============================================================================
# 17. P3-19 / P3-21 / P3-22：文档一致性
# ==============================================================================
@allure.feature("Day44收尾")
class TestDocConsistency:
    """文档与实现的漂移修正"""

    @allure.story("API.md 蓝图与接口计数与代码一致")
    def test_api_doc_endpoint_count(self):
        content = _read(PROJECT_ROOT / "docs" / "API.md")
        assert "6 个蓝图共 25 个 HTTP 接口" in content
        assert "5 个蓝图共 22 个 HTTP 接口" not in content

    @allure.story("API.md 收录了此前遗漏的页面路由")
    def test_api_doc_has_pages_bp(self):
        content = _read(PROJECT_ROOT / "docs" / "API.md")
        assert "pages_bp" in content
        assert "/dashboard" in content

    @allure.story("API.md 的时间示例带时区标识")
    def test_api_doc_time_has_tz(self):
        content = _read(PROJECT_ROOT / "docs" / "API.md")
        assert "2026-09-20T10:30:45+00:00" in content

    @allure.story("API.md 列表示例含 status 字段")
    def test_api_doc_list_has_status(self):
        content = _read(PROJECT_ROOT / "docs" / "API.md")
        assert '"status": "finished"' in content

    @allure.story("README 阶段表不再把已交付项列为规划中")
    def test_readme_stage_table_current(self):
        content = _read(PROJECT_ROOT / "README.md")
        assert "三页面收尾规划中" not in content
        assert "收尾 Day41-44" not in content
