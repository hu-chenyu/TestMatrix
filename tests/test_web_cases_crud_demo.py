"""
TestMatrix Day20: 用例CRUD API测试（POST/GET详情/PUT/DELETE）

测试覆盖:
    创建:
        1. test_create_case_success          正常创建返回201，字段正确
        2. test_create_case_duplicate_id     重复case_id返回409
        3. test_create_case_missing_required 缺必填name返回400
        4. test_create_case_invalid_enum     priority=P5返回400
    查询:
        5. test_get_case_success             查询存在用例返回200
        6. test_get_case_not_found           查询不存在用例返回404
    更新:
        7. test_update_case_success          更新传入字段，未传字段不变
        8. test_update_case_not_found        更新不存在用例返回404
        9. test_update_case_invalid_enum     更新priority=P9返回400
        10. test_update_case_empty_body      PUT空字典返回400
    删除:
        11. test_delete_case_success         删除返回204，再查404
        12. test_delete_case_not_found       删除不存在用例返回404

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证，零mock零真实外部依赖。
"""

from pathlib import Path
from typing import Iterator

import allure
import pytest
from flask.testing import FlaskClient

from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app


# ===========================================================================
# 造数与夹具
# ===========================================================================
def _build_case_data() -> list[tuple]:
    """
    构建用例种子数据（内部方法）

    数据布局（4条，覆盖CRUD测试所需场景）:
        - TM-CRUD-0001 用户中心P1（详情/更新目标）
        - TM-CRUD-0002 用户中心P2（更新成功目标）
        - TM-CRUD-0003 订单中心P0（删除成功目标）
        - TM-CRUD-0004 板卡通信P2 chip类型disabled（多元数据覆盖）

    参数:
        无

    返回:
        list[tuple]: (case_id, name, module, priority, case_type,
                      status, description)元组列表
    """
    return [
        ("TM-CRUD-0001", "用例查询校验", "用户中心", "P1", "api", "active", "验证查询接口"),
        ("TM-CRUD-0002", "用例创建校验", "用户中心", "P2", "api", "active", "验证创建接口"),
        ("TM-CRUD-0003", "用例删除校验", "订单中心", "P0", "api", "active", "验证删除接口"),
        ("TM-CRUD-0004", "串口通信校验", "板卡通信", "P2", "chip", "disabled", "验证串口收发"),
    ]


def seed_cases() -> None:
    """
    造用例数据并入库（4条）

    参数:
        无

    返回:
        无
    """
    with DatabaseSession.session_scope() as session:
        for case_id, name, module, priority, case_type, status, description in (
            _build_case_data()
        ):
            session.add(
                models.TestCase(
                    case_id=case_id,
                    name=name,
                    module=module,
                    priority=priority,
                    case_type=case_type,
                    status=status,
                    description=description,
                )
            )


@pytest.fixture
def crud_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    用例CRUD API测试客户端fixture（临时SQLite文件库）

    将数据库指向pytest临时目录独立库文件并完成建表+造数，
    前后重置DatabaseSession引擎单例，防止Windows下SQLite文件
    句柄残留导致tmp_path清理失败，也避免测试间引擎状态污染。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield测试客户端，前后完成引擎重置
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "TM_DB_SQLITE_PATH", str(tmp_path / "cases_crud_api.db")
    )
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# 用例CRUD API测试
# ===========================================================================
@allure.feature("用例管理API")
@allure.story("用例增删改查")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCasesCrudApi:
    """POST/GET/PUT/DELETE /api/cases/ 用例CRUD测试"""

    # ------------------------------------------------------------------
    # 创建用例
    # ------------------------------------------------------------------
    def test_create_case_success(self, crud_client: FlaskClient) -> None:
        """
        创建成功: 传入case_id/name（可选字段缺省），返回201，
        响应体含新建用例数据，case_id/name正确且缺省字段
        应用Schema默认值（module=default/priority=P2/creator=admin）
        """
        payload = {
            "case_id": "TM-CRUD-0101",
            "name": "新增用例校验",
        }
        response = crud_client.post("/api/cases/", json=payload)
        data = response.get_json()

        assert response.status_code == 201, "创建成功应返回201"
        assert data["code"] == 201, "响应体code应为201"
        assert data["data"]["case_id"] == "TM-CRUD-0101", "新建用例编号应正确"
        assert data["data"]["name"] == "新增用例校验", "新建用例名称应正确"
        # Schema缺省值应用
        assert data["data"]["module"] == "default", "未传module应用默认值default"
        assert data["data"]["priority"] == "P2", "未传priority应用默认值P2"
        assert data["data"]["case_type"] == "api", "未传case_type应用默认值api"
        assert data["data"]["status"] == "active", "未传status应用默认值active"
        assert data["data"]["creator"] == "admin", "未传creator应用默认值admin"
        # 库端生成字段非空
        assert data["data"]["id"] > 0, "自增主键应已生成"
        assert data["data"]["created_at"] is not None, "创建时间应已生成"

        # 落库校验: 列表接口可查到新建用例
        list_response = crud_client.get(
            "/api/cases/?keyword=TM-CRUD-0101&status=all"
        )
        assert list_response.status_code == 200
        assert list_response.get_json()["data"]["total"] == 1, "新建用例应已落库"

    def test_create_case_duplicate_id(self, crud_client: FlaskClient) -> None:
        """
        创建冲突: 使用已存在的case_id（TM-CRUD-0001）创建，
        返回409 ConflictError，detail携带冲突的case_id
        """
        payload = {
            "case_id": "TM-CRUD-0001",
            "name": "重复编号用例",
        }
        response = crud_client.post("/api/cases/", json=payload)
        data = response.get_json()

        assert response.status_code == 409, "重复case_id应返回409"
        assert data["code"] == 409, "响应体code应为409"
        assert "已存在" in data["message"], "错误信息应说明编号已存在"
        assert data["data"] == {"case_id": "TM-CRUD-0001"}, "detail应携带冲突编号"

    def test_create_case_missing_required(self, crud_client: FlaskClient) -> None:
        """
        必填缺失: 请求体缺少必填字段name，返回400 ValidationError，
        错误信息包含name字段名
        """
        payload = {"case_id": "TM-CRUD-0102"}
        response = crud_client.post("/api/cases/", json=payload)
        data = response.get_json()

        assert response.status_code == 400, "缺少必填字段应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert "name" in data["message"], "错误信息应包含缺失字段名name"

    def test_create_case_invalid_enum(self, crud_client: FlaskClient) -> None:
        """
        枚举非法: priority=P5不在P0-P3合法值内，返回400，
        错误信息包含priority字段名
        """
        payload = {
            "case_id": "TM-CRUD-0103",
            "name": "非法优先级用例",
            "priority": "P5",
        }
        response = crud_client.post("/api/cases/", json=payload)
        data = response.get_json()

        assert response.status_code == 400, "priority=P5应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert "priority" in data["message"], "错误信息应包含字段名priority"

    # ------------------------------------------------------------------
    # 查询用例详情
    # ------------------------------------------------------------------
    def test_get_case_success(self, crud_client: FlaskClient) -> None:
        """
        查询成功: 查询已存在的case_id（TM-CRUD-0001），返回200，
        数据字段正确且完整（_to_dict全量11字段）
        """
        response = crud_client.get("/api/cases/TM-CRUD-0001")
        data = response.get_json()

        assert response.status_code == 200, "查询存在用例应返回200"
        assert data["code"] == 200, "响应体code应为200"
        case_data = data["data"]
        assert case_data["case_id"] == "TM-CRUD-0001"
        assert case_data["name"] == "用例查询校验"
        assert case_data["module"] == "用户中心"
        assert case_data["priority"] == "P1"
        assert case_data["case_type"] == "api"
        assert case_data["status"] == "active"
        # 字段完整性
        assert {
            "id", "case_id", "name", "module", "priority", "case_type",
            "status", "description", "creator", "created_at", "updated_at",
        } == set(case_data.keys()), "详情应返回用例全量字段"

    def test_get_case_not_found(self, crud_client: FlaskClient) -> None:
        """
        查询不存在: 查询不存在的case_id，返回404统一错误格式，
        detail携带查询的case_id
        """
        response = crud_client.get("/api/cases/TM-CRUD-9999")
        data = response.get_json()

        assert response.status_code == 404, "查询不存在用例应返回404"
        assert data["code"] == 404, "响应体code应为404"
        assert "用例不存在" in data["message"], "错误信息应说明用例不存在"
        assert data["data"] == {"case_id": "TM-CRUD-9999"}, "detail应携带查询编号"

    # ------------------------------------------------------------------
    # 更新用例
    # ------------------------------------------------------------------
    def test_update_case_success(self, crud_client: FlaskClient) -> None:
        """
        更新成功: 更新name和priority返回200，传入字段已更新，
        未传字段（module/case_type）保持原值不变
        """
        payload = {"name": "更新后的用例名称", "priority": "P0"}
        response = crud_client.put("/api/cases/TM-CRUD-0002", json=payload)
        data = response.get_json()

        assert response.status_code == 200, "更新成功应返回200"
        case_data = data["data"]
        # 传入字段已更新
        assert case_data["name"] == "更新后的用例名称", "name应已更新"
        assert case_data["priority"] == "P0", "priority应已更新"
        # 未传字段保持不变
        assert case_data["module"] == "用户中心", "未传module应保持原值"
        assert case_data["case_type"] == "api", "未传case_type应保持原值"
        assert case_data["status"] == "active", "未传status应保持原值"
        assert case_data["case_id"] == "TM-CRUD-0002", "业务编号应不变"

        # 落库校验: 重新查询详情确认更新已持久化
        refetch = crud_client.get("/api/cases/TM-CRUD-0002")
        assert refetch.status_code == 200
        refetch_data = refetch.get_json()["data"]
        assert refetch_data["name"] == "更新后的用例名称", "更新应已落库"
        assert refetch_data["priority"] == "P0", "更新应已落库"

    def test_update_case_not_found(self, crud_client: FlaskClient) -> None:
        """
        更新不存在: 更新不存在的case_id，返回404统一错误格式
        """
        payload = {"name": "不存在的用例"}
        response = crud_client.put("/api/cases/TM-CRUD-9999", json=payload)
        data = response.get_json()

        assert response.status_code == 404, "更新不存在用例应返回404"
        assert data["code"] == 404, "响应体code应为404"
        assert "用例不存在" in data["message"], "错误信息应说明用例不存在"

    def test_update_case_invalid_enum(self, crud_client: FlaskClient) -> None:
        """
        更新枚举非法: 更新时priority=P9，返回400，
        错误信息包含priority字段名，原数据未被修改
        """
        payload = {"priority": "P9"}
        response = crud_client.put("/api/cases/TM-CRUD-0001", json=payload)
        data = response.get_json()

        assert response.status_code == 400, "priority=P9应返回400"
        assert "priority" in data["message"], "错误信息应包含字段名priority"

        # 原数据未被修改
        refetch = crud_client.get("/api/cases/TM-CRUD-0001")
        assert refetch.get_json()["data"]["priority"] == "P1", "原priority应保持P1"

    def test_update_case_empty_body(self, crud_client: FlaskClient) -> None:
        """
        空更新: PUT空字典{}（无任何待更新字段），返回400，
        错误信息为"至少提供一个待更新字段"
        """
        response = crud_client.put("/api/cases/TM-CRUD-0001", json={})
        data = response.get_json()

        assert response.status_code == 400, "空body更新应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert "至少提供一个待更新字段" in data["message"], (
            "错误信息应说明至少提供一个待更新字段"
        )

    # ------------------------------------------------------------------
    # 删除用例
    # ------------------------------------------------------------------
    def test_delete_case_success(self, crud_client: FlaskClient) -> None:
        """
        删除成功: 删除已存在用例返回204且无响应体，
        再GET同一case_id返回404确认已删除
        """
        response = crud_client.delete("/api/cases/TM-CRUD-0003")

        assert response.status_code == 204, "删除成功应返回204"
        assert response.get_json() is None, "204响应不应携带JSON响应体"

        # 删除后查询应404
        refetch = crud_client.get("/api/cases/TM-CRUD-0003")
        assert refetch.status_code == 404, "删除后查询应返回404"

        # 列表确认（含全部状态）也不存在
        list_response = crud_client.get(
            "/api/cases/?keyword=TM-CRUD-0003&status=all"
        )
        assert list_response.get_json()["data"]["total"] == 0, (
            "删除的用例不应再出现在列表中"
        )

    def test_delete_case_not_found(self, crud_client: FlaskClient) -> None:
        """
        删除不存在: 删除不存在的case_id，返回404统一错误格式
        """
        response = crud_client.delete("/api/cases/TM-CRUD-9999")
        data = response.get_json()

        assert response.status_code == 404, "删除不存在用例应返回404"
        assert data["code"] == 404, "响应体code应为404"
        assert "用例不存在" in data["message"], "错误信息应说明用例不存在"
