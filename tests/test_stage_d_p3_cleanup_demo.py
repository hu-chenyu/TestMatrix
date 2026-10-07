"""
TestMatrix 阶段D: P3 清理项的回归测试

背景:
    阶段1 审查的 30 条 P3 里，与"死代码/失效配置""文档漂移""未知字段"
    相关的条目在阶段D 逐条复核后落地。复核中有三条**旧报告结论已被
    阶段2 的修复作废**，本文件同时把"不该改的"也用测试锁死，避免后来
    者照着过期报告重犯。

    A组 版本号单一事实源
        1. 两个对外端点都取自 src.__version__
        2. 对外版本号仍是 1.0.0（未因统一而变更契约）
    B组 update_case 可更新字段白名单（纵深防御）
        3. 未知字段被忽略且不落库
        4. 未知字段产生可检索的 warning（不再静默丢弃）
        5. 白名单内字段照常更新
        6. 不可变字段仍静默剔除（兼容前端回传完整对象）
        7. 已知+未知混传时，已知生效、未知忽略
    C组 被复核推翻的旧结论（锁死"不要照着旧报告改"）
        8. MySQL host/database 刻意不编码
        9. logger.py 注释里的文件名与实际文件一致

测试铁律:
    - 涉及数据库的用例独立临时 SQLite 库，teardown 严格 reset
    - 断言只锁对外契约与字段行为，不绑定日志文案全文
    - 全程 loguru，无 print
"""

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

import allure
import pytest
from loguru import logger as loguru_logger
from src import __version__
from src.core.case_manager import (
    UPDATABLE_CASE_FIELDS,
    CaseManager,
)
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 项目根目录（本文件位于 tests/ 下，向上两级即为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _WarningCapture:
    """
    loguru WARNING 级日志捕获器（context manager）

    为什么不用 pytest 的 caplog: loguru 默认**不向标准 logging 传播**
    （需显式配置 propagate），caplog 只能看到标准 logging 通道的记录，
    对本项目日志一条都抓不到——用了会得到一个永远为空的列表并据此
    误判"没打 warning"。

    参数:
        无

    返回:
        无
    """

    def __init__(self) -> None:
        """初始化空的捕获缓冲"""
        self.messages: list[str] = []
        self._sink_id: int | None = None

    def __enter__(self) -> "_WarningCapture":
        """注册 loguru sink 开始收集"""
        self._sink_id = loguru_logger.add(
            self.messages.append, level="WARNING"
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """移除 sink（不吞异常，原样透传）"""
        if self._sink_id is not None:
            loguru_logger.remove(self._sink_id)
            self._sink_id = None


def _capture_warnings() -> _WarningCapture:
    """构造一个 WARNING 日志捕获器（内部工具）"""
    return _WarningCapture()


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture
def seeded_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """
    独立临时 SQLite 库 + 单条种子用例

    环境准备:
        - TM_DB_TYPE=sqlite + 临时路径，reset 引擎单例后 init_db
        - 写入一条 active 用例 TM-UC-0001 供更新类用例使用

    teardown:
        - reset 引擎（不释放句柄则临时文件删不掉）后删除库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    db_path = tmp_path / "stage_d.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    with DatabaseSession.session_scope() as session:
        session.add(
            models.TestCase(
                case_id="TM-UC-0001",
                name="用户登录成功校验",
                module="用户中心",
                priority="P1",
                case_type="api",
                status="active",
                description="阶段D种子用例",
                creator="admin",
            )
        )
    yield db_path
    DatabaseSession.reset()
    db_path.unlink(missing_ok=True)


# ===========================================================================
# A组: 版本号单一事实源
# ===========================================================================
@allure.feature("阶段D P3清理")
@allure.story("版本号单一事实源")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestVersionSingleSource:
    """对外端点不再各自硬编码版本号字面量"""

    def test_both_endpoints_report_package_version(self, seeded_db: Path) -> None:
        """
        GET / 与 GET /api/version 的 version 字段都等于 src.__version__

        改版本时只需改 src/__init__.py 一处：此前两处各写 "1.0.0"、
        __init__ 写 "0.1.0"，改版本漏改一处就会对外报出互相矛盾的数字，
        而这种矛盾只有用户真去比对两个端点时才会被发现。
        """
        client = create_app("test").test_client()

        index_data = client.get("/").get_json()["data"]
        version_data = client.get("/api/version").get_json()["data"]

        assert index_data["version"] == __version__, (
            f"首页应取自 src.__version__({__version__})，实际 {index_data['version']}"
        )
        assert version_data["version"] == __version__, (
            f"/api/version 应取自 src.__version__({__version__})，"
            f"实际 {version_data['version']}"
        )

    def test_public_version_contract_unchanged(self, seeded_db: Path) -> None:
        """
        统一来源不改变对外契约: 平台版本号仍为 1.0.0

        统一的是"来源"不是"取值"。若改成 0.1.0 会让已按 1.0.0 对接的
        客户端与文档全部失配，故此处显式锁死取值。
        """
        client = create_app("test").test_client()

        data = client.get("/api/version").get_json()["data"]

        assert data["version"] == "1.0.0", "对外版本号契约不应因本次统一而变化"
        assert data["api_version"] == "v1", "API版本号 v1 是独立契约，不受影响"


# ===========================================================================
# B组: update_case 可更新字段白名单
# ===========================================================================
@allure.feature("阶段D P3清理")
@allure.story("update_case 字段白名单")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestUpdateCaseAllowlist:
    """未知字段不再静默丢弃，不可变字段仍兼容剔除"""

    def test_unknown_field_is_ignored_not_persisted(
        self, seeded_db: Path
    ) -> None:
        """
        未知字段被忽略且不落库

        修复前是逐字段裸 setattr：字段名不存在时 ORM 只在实例上挂一个
        临时属性，DB 里什么都没变，调用方却拿到 200 + 看起来正常的
        返回值，毫无察觉地丢掉这次更新。
        """
        result = CaseManager.update_case(
            "TM-UC-0001",
            {"name": "改名成功", "not_a_real_column": "应当被忽略"},
        )

        assert result["name"] == "改名成功", "白名单内字段必须照常生效"
        assert "not_a_real_column" not in result, (
            "未知字段不得出现在返回结果中"
        )
        stored = CaseManager.get_case("TM-UC-0001")
        assert stored is not None
        assert not hasattr(stored, "not_a_real_column"), (
            "未知字段不得作为临时属性残留在 ORM 实例上"
        )

    def test_unknown_field_emits_warning(self, seeded_db: Path) -> None:
        """
        未知字段产生可检索的 warning

        白名单的全部价值就在这条：把"字段名写错"从静默丢弃变成日志里
        看得见的一行。断言只校验"提到该字段名且级别为 WARNING"，
        不锁死整句文案，避免格式化调整误报。
        """
        captured = _capture_warnings()

        with captured:
            CaseManager.update_case("TM-UC-0001", {"typo_fieldname": "x"})

        assert any(
            "typo_fieldname" in text for text in captured.messages
        ), f"未知字段应产生含字段名的 WARNING，实际日志: {captured.messages}"

    def test_allowlisted_fields_all_update(self, seeded_db: Path) -> None:
        """
        白名单内字段全部可更新（防止白名单写漏导致合法更新被拦）

        白名单是"允许"列表，写漏一个就是功能回退——因此这里遍历
        UPDATABLE_CASE_FIELDS 逐个验证，而不是只测一两个代表字段。

        Day47 新增 source_ref（pytest 可执行目标，人工补录/修正用），
        故探测字典同步扩充；白名单与探测值必须始终全等——白名单加了
        source_ref 而探测字典没加，本断言会红，正是它该红的时候。
        """
        probe_values = {
            "name": "全字段更新校验",
            "module": "订单中心",
            "priority": "P0",
            "case_type": "chip",
            "status": "disabled",
            "description": "白名单全字段遍历",
            "creator": "qa-bot",
            "source_ref": "tests/test_update_case.py",
        }
        assert set(probe_values) == set(UPDATABLE_CASE_FIELDS), (
            "探测字典必须覆盖白名单全部字段，否则本测试会漏检"
        )

        result = CaseManager.update_case("TM-UC-0001", probe_values)

        for field_name, expected in probe_values.items():
            if field_name == "source_ref":
                # source_ref 刻意**不在** _to_dict 的响应字段集内：Day47
                # 只放开录入侧（写），未改 GET 响应的字段集合契约，
                # 改它会让既有"详情返回全量字段"的精确断言全部失效。
                # 故该字段改为直接查库校验"确实已落库"。
                continue
            assert result[field_name] == expected, (
                f"白名单字段 {field_name} 应可更新"
            )

        with DatabaseSession.session_scope() as session:
            row = (
                session.query(models.TestCase)
                .filter_by(case_id="TM-UC-0001")
                .one()
            )
        assert row.source_ref == "tests/test_update_case.py", (
            "白名单字段 source_ref 应可更新并落库"
        )

    def test_immutable_fields_still_silently_stripped(
        self, seeded_db: Path
    ) -> None:
        """
        不可变字段仍走静默剔除，不触发 warning

        与未知字段的差别很重要: 前端编辑表单会回传完整对象（含 case_id），
        这是**兼容行为**不是错误，记 warning 会在每次正常保存时刷噪声，
        久而久之没人再看日志——真正要抓的拼写错误反而被淹没。
        """
        captured = _capture_warnings()

        with captured:
            result = CaseManager.update_case(
                "TM-UC-0001",
                {"case_id": "TM-UC-9999", "id": 9999, "name": "兼容剔除"},
            )

        assert result["case_id"] == "TM-UC-0001", "case_id 不可被更新"
        assert result["name"] == "兼容剔除", "同批次合法字段应正常生效"
        assert not any(
            "case_id" in text for text in captured.messages
        ), f"不可变字段属兼容剔除，不应记 WARNING，实际: {captured.messages}"

    def test_mixed_known_and_unknown_fields(
        self, seeded_db: Path
    ) -> None:
        """
        已知+未知混传: 已知生效、未知忽略、整次调用不失败

        这是最贴近真实误用的形态——前端或脚本改了字段名，连同正确字段
        一起提交。行为上必须"能改的都改了"，而不是整批拒绝。
        """
        result = CaseManager.update_case(
            "TM-UC-0001",
            {
                "module": "支付中心",
                "name": "混传校验",
                "module_typo": "应当被忽略",
            },
        )

        assert result["module"] == "支付中心", "合法字段必须生效"
        assert result["name"] == "混传校验", "合法字段必须生效"
        assert "module_typo" not in result, "未知字段必须被忽略"


# ===========================================================================
# C组: 被复核推翻的旧结论（锁死"不要照着旧报告改"）
# ===========================================================================
@allure.feature("阶段D P3清理")
@allure.story("旧结论纠正守卫")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestSupersededFindings:
    """旧审查报告里已被推翻的结论，用测试防止被照单重犯"""

    def test_mysql_host_and_database_stay_unencoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        host/database 不得编码（审查报告曾要求编码，属错误结论）

        host 是主机名/IP、database 是路径段名，二者按字面量解析；编码后
        反解析会失败。照着旧报告"补齐编码"会引入新 bug，故此条专门
        锁死"不该编码"。
        """
        from src.db.db_session import DatabaseSession

        monkeypatch.setenv("TM_DB_TYPE", "mysql")
        monkeypatch.setenv("TM_DB_MYSQL_HOST", "10.0.0.5")
        monkeypatch.setenv("TM_DB_MYSQL_DATABASE", "tm db")

        url = DatabaseSession._build_db_url()

        assert "@10.0.0.5:3306/tm db?charset=utf8mb4" in url, (
            f"host/database 必须原样保留，实际: {url}"
        )

    def test_logger_root_comment_names_actual_file(self) -> None:
        """
        logger.py 的项目根目录注释里的文件名必须与实际文件一致

        该注释原写作"env.py 位于 src/common/ 下"，而本文件是 logger.py
        （env_manager.py 才是 env 相关模块）。这类漂移会让后来者照着
        注释去找一个不存在的文件。
        """
        logger_source = (PROJECT_ROOT / "src" / "common" / "logger.py").read_text(
            encoding="utf-8"
        )
        root_comment_lines = [
            line
            for line in logger_source.splitlines()
            if line.startswith("# 项目根目录")
        ]

        assert root_comment_lines, "应存在项目根目录注释"
        assert "logger.py" in root_comment_lines[0], (
            f"注释应指向本文件 logger.py，实际: {root_comment_lines[0]}"
        )
        assert "env.py" not in root_comment_lines[0], (
            "注释不得再引用 env.py（那是 env_manager.py 的职责）"
        )
