"""
CaseManager 边界与数据库异常路径测试（Day42-coverage 覆盖率补全）

覆盖 src/core/case_manager.py 原 87% 覆盖率的盲区，分四组:
    1. 入参防御校验分支: 空批次号/空用例编号/非法分页参数（bool与int子类陷阱）
    2. 静态工具方法: _parse_tags_from_description / _normalize_values 的
       空值与非法类型分支
    3. SQLAlchemyError 包装: 各数据访问方法把底层DB异常统一转
       CaseManagerError 并带上 operation context（mock 会话层触发）
    4. 批次行缺失早返回: _update_batch_status 查不到批次行时告警并返回

设计原则:
    - 全部使用 tmp_path 临时SQLite库 + DatabaseSession.reset() 前后重置，
      不污染 output/testmatrix.db 正式库
    - DB异常分支通过 monkeypatch 替换 DatabaseSession 的会话入口，
      不依赖真实DB故障；断言同时校验异常类型与 context.operation
    - 每条用例都对具体返回值/异常消息/context 字段做实质断言，
      无空断言、无覆盖率排除注释
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import allure
import pytest
from sqlalchemy.exc import SQLAlchemyError
from src.core.case_manager import MAX_PAGE_SIZE, CaseManager, CaseManagerError
from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 项目自带测试数据（字段完整，可通过 DataDriver 校验）
DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（case_manager 覆盖率测试独享）

    参数:
        tmp_path (Path): pytest临时目录fixture
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture

    返回:
        Iterator[None]: 完成建表后yield，结束后重置引擎单例
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "cm_coverage.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield
    DatabaseSession.reset()


def boom_session(*args, **kwargs):
    """构造一个调用即抛 SQLAlchemyError 的会话入口替身"""
    raise SQLAlchemyError("模拟数据库不可用")


# ===========================================================================
# 1. 入参防御校验
# ===========================================================================
@allure.feature("CaseManager入参防御")
class TestValidationGuards:
    """核心方法在触达数据库前的入参拦截分支"""

    @allure.story("sync_cases_from_file 收到空路径")
    def test_sync_empty_path(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.sync_cases_from_file("   ")
        assert "路径不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "sync_cases_from_file"

    @allure.story("list_cases_paged page 传0")
    def test_list_cases_paged_page_zero(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_cases_paged(page=0)
        assert "page必须为大于等于1的整数" in str(exc.value)
        assert exc.value.context["operation"] == "list_cases_paged"

    @allure.story("list_cases_paged page 传bool（int子类陷阱）")
    def test_list_cases_paged_page_bool(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_cases_paged(page=True)
        assert "page必须为大于等于1的整数" in str(exc.value)

    @allure.story("list_cases_paged page 传非整数")
    def test_list_cases_paged_page_not_int(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_cases_paged(page="abc")
        assert "page必须为大于等于1的整数" in str(exc.value)

    @allure.story("list_cases_paged page_size 超上限")
    def test_list_cases_paged_size_too_large(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_cases_paged(page_size=MAX_PAGE_SIZE + 1)
        message = str(exc.value)
        assert f"page_size必须在1到{MAX_PAGE_SIZE}之间" in message
        assert exc.value.context["operation"] == "list_cases_paged"

    @allure.story("list_cases_paged page_size 传0与bool均被拒")
    @pytest.mark.parametrize("bad_size", [0, -1, True, 1.5])
    def test_list_cases_paged_size_invalid(self, temp_db, bad_size):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_cases_paged(page_size=bad_size)
        assert "page_size必须在" in str(exc.value)

    @allure.story("create_case 缺用例编号")
    def test_create_case_missing_id(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.create_case({"name": "无名用例", "priority": "P1"})
        assert "用例编号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "create_case"

    @allure.story("create_case 用例编号为纯空白")
    def test_create_case_blank_id(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.create_case({"case_id": "   ", "name": "N", "priority": "P1"})
        assert "用例编号不能为空" in str(exc.value)

    @allure.story("create_case 缺用例名称且context带case_id")
    def test_create_case_missing_name(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.create_case({"case_id": "TC-001", "priority": "P1"})
        assert "用例名称不能为空" in str(exc.value)
        assert exc.value.context["case_id"] == "TC-001"

    @allure.story("update_case 空用例编号")
    def test_update_case_blank_id(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.update_case("", {"name": "x"})
        assert "用例编号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "update_case"

    @allure.story("delete_case 空用例编号")
    def test_delete_case_blank_id(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.delete_case(None)
        assert "用例编号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "delete_case"

    @allure.story("record_execution 空用例编号")
    def test_record_execution_blank_case_id(self, temp_db):
        now = datetime(2026, 10, 2, 12, 0, 0)
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.record_execution(
                execution_id="BATCH-1", case_id="", case_name="N", result="passed",
                start_time=now, end_time=now, duration=0.1,
            )
        assert "用例编号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "record_execution"

    @allure.story("finish_execution 空批次号")
    def test_finish_execution_blank_id(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.finish_execution("")
        assert "执行批次号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "finish_execution"

    @allure.story("list_executions_paged 非法分页参数")
    @pytest.mark.parametrize("bad_page", [0, -5, True, "x"])
    def test_list_executions_paged_bad_page(self, temp_db, bad_page):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_executions_paged(page=bad_page)
        assert "page必须为大于等于1的整数" in str(exc.value)
        assert exc.value.context["operation"] == "list_executions_paged"

    @allure.story("list_executions_paged page_size 越界")
    @pytest.mark.parametrize("bad_size", [0, MAX_PAGE_SIZE + 1, True])
    def test_list_executions_paged_bad_size(self, temp_db, bad_size):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.list_executions_paged(page_size=bad_size)
        assert "page_size必须在" in str(exc.value)

    @allure.story("get_execution_detail 空批次号")
    def test_get_execution_detail_blank(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.get_execution_detail("  ")
        assert "执行批次号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "get_execution_detail"

    @allure.story("get_execution_status 空批次号")
    def test_get_execution_status_blank(self, temp_db):
        with pytest.raises(CaseManagerError) as exc:
            CaseManager.get_execution_status("")
        assert "执行批次号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == "get_execution_status"


# ===========================================================================
# 2. 静态工具方法
# ===========================================================================
@allure.feature("CaseManager静态工具")
class TestStaticHelpers:
    """_parse_tags_from_description / _normalize_values 的兜底分支"""

    @allure.story("description 为None时返回空列表")
    def test_tags_none(self):
        assert CaseManager._parse_tags_from_description(None) == []

    @allure.story("description 为空串时返回空列表")
    def test_tags_empty_string(self):
        assert CaseManager._parse_tags_from_description("") == []

    @allure.story("description 不以标签前缀开头时返回空列表")
    def test_tags_not_prefixed(self):
        assert CaseManager._parse_tags_from_description("这是自定义描述") == []

    @allure.story("带标签前缀时按逗号切分并剔除空项")
    def test_tags_parsed(self):
        assert CaseManager._parse_tags_from_description("标签: smoke, ui ,, api") == [
            "smoke", "ui", "api",
        ]

    @allure.story("str单值归一化为单元素列表")
    def test_normalize_single_str(self):
        assert CaseManager._normalize_values("  用户管理 ", "module") == ["用户管理"]

    @allure.story("str为纯空白时视为不过滤")
    def test_normalize_blank_str(self):
        assert CaseManager._normalize_values("   ", "module") == []

    @allure.story("空列表触发告警并返回空列表")
    def test_normalize_empty_list(self):
        assert CaseManager._normalize_values([], "priority") == []

    @allure.story("列表逐项strip并剔除空白项")
    def test_normalize_list(self):
        assert CaseManager._normalize_values([" P0 ", "", "P1"], "priority") == ["P0", "P1"]

    @allure.story("非str非list类型触发非法类型告警并返回空列表")
    @pytest.mark.parametrize("bad_value", [123, None, 1.5, {"a": 1}, (1, 2)])
    def test_normalize_illegal_type(self, bad_value):
        assert CaseManager._normalize_values(bad_value, "status") == []


# ===========================================================================
# 3. SQLAlchemyError 包装
# ===========================================================================
@allure.feature("CaseManager数据库异常包装")
class TestSqlAlchemyErrorWrapping:
    """底层DB异常统一转 CaseManagerError 并保留 operation 定位信息"""

    @allure.story("sync_cases_from_file 入库阶段DB异常")
    def test_sync_db_error(self, temp_db):
        # 用项目自带测试数据（字段完整，能通过 DataDriver 校验），
        # 确保流程走到 session_scope 入库阶段再触发DB异常
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.sync_cases_from_file(DATA_FILE)
        assert "用例入库数据库异常" in str(exc.value)
        # 入库阶段 context.operation 口径为 upsert（与数据加载阶段不同）
        assert exc.value.context["operation"] == "upsert"
        assert DATA_FILE.name in exc.value.context["file_path"]

    @allure.story("list_cases 查询DB异常")
    def test_list_cases_db_error(self, temp_db):
        with patch.object(DatabaseSession, "get_session", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.list_cases()
        assert "查询数据库异常" in str(exc.value)
        assert exc.value.context["operation"] == "list_cases"

    @allure.story("list_cases_paged 分页查询DB异常")
    def test_list_cases_paged_db_error(self, temp_db):
        with patch.object(DatabaseSession, "get_session", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.list_cases_paged()
        assert "分页查询数据库异常" in str(exc.value)
        assert exc.value.context["operation"] == "list_cases_paged"

    @allure.story("get_case 详情查询DB异常")
    def test_get_case_db_error(self, temp_db):
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.get_case("TC-001")
        assert "详情查询数据库异常" in str(exc.value)
        assert exc.value.context["case_id"] == "TC-001"

    @allure.story("create_case 建表写入DB异常")
    def test_create_case_db_error(self, temp_db):
        payload = {"case_id": "TC-ERR", "name": "N", "priority": "P1"}
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.create_case(payload)
        assert "创建数据库异常" in str(exc.value)
        assert exc.value.context["operation"] == "create_case"

    @allure.story("update_case 更新DB异常")
    def test_update_case_db_error(self, temp_db):
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.update_case("TC-001", {"name": "N2"})
        assert "更新数据库异常" in str(exc.value)
        assert exc.value.context["case_id"] == "TC-001"

    @allure.story("delete_case 删除DB异常")
    def test_delete_case_db_error(self, temp_db):
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.delete_case("TC-001")
        assert "删除数据库异常" in str(exc.value)
        assert exc.value.context["case_id"] == "TC-001"

    @allure.story("record_execution 明细写入DB异常")
    def test_record_execution_db_error(self, temp_db):
        now = datetime(2026, 10, 2, 12, 0, 0)
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.record_execution(
                    execution_id="BATCH-1", case_id="TC-001", case_name="N",
                    result="passed", start_time=now, end_time=now, duration=0.5,
                )
        assert "数据库异常" in str(exc.value)
        assert exc.value.context["execution_id"] == "BATCH-1"

    @allure.story("finish_execution 落位写入DB异常")
    def test_finish_execution_db_error(self, temp_db):
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.finish_execution("BATCH-1")
        assert "落位" in str(exc.value) or "数据库异常" in str(exc.value)
        assert exc.value.context["execution_id"] == "BATCH-1"

    @allure.story("list_executions_paged 分页查询DB异常")
    def test_list_executions_paged_db_error(self, temp_db):
        with patch.object(DatabaseSession, "get_session", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.list_executions_paged()
        assert "分页查询数据库异常" in str(exc.value)
        assert exc.value.context["operation"] == "list_executions_paged"

    @allure.story("get_execution_detail 详情查询DB异常")
    def test_get_execution_detail_db_error(self, temp_db):
        with patch.object(DatabaseSession, "get_session", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.get_execution_detail("BATCH-1")
        assert "数据库异常" in str(exc.value)
        assert exc.value.context["execution_id"] == "BATCH-1"

    @allure.story("start_execution 批次元信息落库DB异常")
    def test_start_execution_db_error(self, temp_db):
        # start_execution 内部依次调用 选例 → 建批次号 → 批次元信息落库，
        # 前两步各自有独立异常分支；此处把前两步 stub 成成功，
        # 只让第三次 session_scope（批次元信息落库）抛DB异常，
        # 从而精确命中"批次元信息入库数据库异常"分支
        with (
            patch.object(
                CaseManager, "select_cases_for_execution",
                return_value=[{"case_id": "TC-1", "name": "用例1"}],
            ),
            patch.object(CaseManager, "create_execution", return_value="BATCH-STUB-1"),
            patch.object(DatabaseSession, "session_scope", boom_session),
        ):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.start_execution(trigger="cli")
        assert "批次元信息入库数据库异常" in str(exc.value)
        assert exc.value.context["operation"] == "start_execution"
        assert exc.value.context["execution_id"] == "BATCH-STUB-1"

    @allure.story("get_execution_status 状态查询DB异常")
    def test_get_execution_status_db_error(self, temp_db):
        with patch.object(DatabaseSession, "get_session", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager.get_execution_status("BATCH-1")
        assert "状态查询数据库异常" in str(exc.value)
        assert exc.value.context["execution_id"] == "BATCH-1"

    @allure.story("_update_batch_status 状态更新DB异常")
    def test_update_batch_status_db_error(self, temp_db):
        with patch.object(DatabaseSession, "session_scope", boom_session):
            with pytest.raises(CaseManagerError) as exc:
                CaseManager._update_batch_status("BATCH-1", "finished")
        assert "状态更新数据库异常" in str(exc.value)
        assert exc.value.context["execution_id"] == "BATCH-1"


# ===========================================================================
# 4. 批次行缺失早返回
# ===========================================================================
@allure.feature("CaseManager批次行缺失")
class TestUpdateBatchStatusMissingRow:
    """_update_batch_status 查不到批次行时告警并静默返回（不抛异常）"""

    @staticmethod
    def _scope_with_none_row():
        """构造一个 query(...).filter_by(...).first() 恒返回 None 的会话域"""
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        scope = MagicMock()
        scope.__enter__.return_value = session
        scope.__exit__.return_value = False
        return scope

    @allure.story("批次行不存在时告警并返回None而非抛错")
    def test_missing_batch_row_returns_none(self, temp_db):
        with patch.object(
            DatabaseSession, "session_scope", return_value=self._scope_with_none_row()
        ):
            result = CaseManager._update_batch_status("BATCH-NOT-EXIST", "finished")
        assert result is None
