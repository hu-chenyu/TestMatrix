"""
TestMatrix Day19: 用例列表API测试（GET /api/cases/）

测试覆盖:
    1. 默认分页: 不传参数，默认page=1/page_size=20返回第一页
    2. 自定义分页: page=2/page_size=5返回第二页5条
    3. 模块筛选: module=用户中心只返回该模块用例
    4. 优先级筛选: priority=P0（小写p0同样匹配，大小写不敏感）
    5. 类型筛选: case_type=api只返回api类型用例
    6. 状态筛选: status=all返回active+disabled全部用例
    7. 关键字搜索: keyword=登录匹配name或description含"登录"的用例
    8. 非法页码: page=0返回400 ValidationError
    9. 超限页大小: page_size=200返回400
    10. 响应结构: 分页五字段完整 + items元素字段完整

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证，零真实外部依赖。
"""

from pathlib import Path
from typing import Iterator, Union

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

    数据布局（27条: 25条active + 2条disabled，配合10条测试的
    确定性断言）:
        - 用户中心模块 12条active（P0x3含"登录"关键字x3 / P1x5 / P2x4）
        - 订单中心模块 10条active（P0x1 / P1x3 / P2x6）
        - 板卡通信模块 3条active（chip类型，P1x1 / P2x2）
        - 用户中心/订单中心各1条disabled

    参数:
        无

    返回:
        list[tuple]: (case_id, name, module, priority, case_type,
                      status, description)元组列表
    """
    return [
        # 用户中心模块（api类型，12条active）
        ("TM-UC-0001", "用户登录成功校验", "用户中心", "P0", "api", "active", "验证正确账密登录成功"),
        ("TM-UC-0002", "用户登录密码错误校验", "用户中心", "P0", "api", "active", "验证错误密码登录失败"),
        ("TM-UC-0003", "用户信息查询校验", "用户中心", "P0", "api", "active", "验证登录token查询用户信息"),
        ("TM-UC-0004", "用户信息更新校验", "用户中心", "P1", "api", "active", "验证更新用户资料"),
        ("TM-UC-0005", "用户头像上传校验", "用户中心", "P1", "api", "active", "验证头像上传"),
        ("TM-UC-0006", "用户注册校验", "用户中心", "P1", "api", "active", "验证新用户注册流程"),
        ("TM-UC-0007", "用户注销校验", "用户中心", "P1", "api", "active", "验证注销流程"),
        ("TM-UC-0008", "用户密码修改校验", "用户中心", "P1", "api", "active", "验证修改密码"),
        ("TM-UC-0009", "用户列表查询校验", "用户中心", "P2", "api", "active", "验证分页查询用户列表"),
        ("TM-UC-0010", "用户权限校验", "用户中心", "P2", "api", "active", "验证权限拦截"),
        ("TM-UC-0011", "用户禁用校验", "用户中心", "P2", "api", "active", "验证禁用用户"),
        ("TM-UC-0012", "用户导出校验", "用户中心", "P2", "api", "active", "验证导出用户数据"),
        # 订单中心模块（api类型，10条active）
        ("TM-OD-0001", "订单创建校验", "订单中心", "P0", "api", "active", "验证创建订单"),
        ("TM-OD-0002", "订单查询校验", "订单中心", "P1", "api", "active", "验证查询订单详情"),
        ("TM-OD-0003", "订单取消校验", "订单中心", "P1", "api", "active", "验证取消订单"),
        ("TM-OD-0004", "订单支付校验", "订单中心", "P1", "api", "active", "验证订单支付"),
        ("TM-OD-0005", "订单退款校验", "订单中心", "P2", "api", "active", "验证退款流程"),
        ("TM-OD-0006", "订单列表查询校验", "订单中心", "P2", "api", "active", "验证订单列表"),
        ("TM-OD-0007", "订单导出校验", "订单中心", "P2", "api", "active", "验证导出订单"),
        ("TM-OD-0008", "订单统计校验", "订单中心", "P2", "api", "active", "验证订单统计"),
        ("TM-OD-0009", "订单超时校验", "订单中心", "P2", "api", "active", "验证订单超时关闭"),
        ("TM-OD-0010", "订单评价校验", "订单中心", "P2", "api", "active", "验证订单评价"),
        # 板卡通信模块（chip类型，3条active）
        ("TM-CHIP-0001", "串口通信校验", "板卡通信", "P1", "chip", "active", "验证串口收发数据"),
        ("TM-CHIP-0002", "Telnet连接校验", "板卡通信", "P2", "chip", "active", "验证Telnet连接"),
        ("TM-CHIP-0003", "板卡重启校验", "板卡通信", "P2", "chip", "active", "验证板卡重启"),
        # 停用用例（status=disabled，2条）
        ("TM-UC-0101", "用户批量导入校验", "用户中心", "P1", "api", "disabled", "验证批量导入用户"),
        ("TM-OD-0101", "订单批量导入校验", "订单中心", "P2", "api", "disabled", "验证批量导入订单"),
    ]


def seed_cases() -> None:
    """
    造用例数据并入库（27条）

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
def cases_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    用例列表API测试客户端fixture（临时SQLite文件库）

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
        "TM_DB_SQLITE_PATH", str(tmp_path / "cases_list_api.db")
    )
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# 用例列表API测试
# ===========================================================================
@allure.feature("用例管理API")
@allure.story("用例列表查询")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCasesListApi:
    """GET /api/cases/ 分页与多维度筛选测试"""

    def test_list_cases_default_pagination(
        self, cases_client: FlaskClient
    ) -> None:
        """
        默认分页: 不传参数，默认page=1/page_size=20，
        25条active数据返回第一页20条、total=25、total_pages=2
        """
        response = cases_client.get("/api/cases/")
        data = response.get_json()

        assert response.status_code == 200, "默认查询应返回200"
        assert data["data"]["page"] == 1, "默认页码应为1"
        assert data["data"]["page_size"] == 20, "默认每页条数应为20"
        assert data["data"]["total"] == 25, "active用例总数应为25"
        assert data["data"]["total_pages"] == 2, "25条按每页20条应为2页"
        assert len(data["data"]["items"]) == 20, "第一页应返回20条"
        # 排序口径: P0优先且case_id升序，首条应为TM-OD-0001
        assert data["data"]["items"][0]["case_id"] == "TM-OD-0001"

    def test_list_cases_custom_pagination(
        self, cases_client: FlaskClient
    ) -> None:
        """
        自定义分页: page=2/page_size=5，返回排序后第6-10条共5条，
        total=25、total_pages=5
        """
        response = cases_client.get("/api/cases/?page=2&page_size=5")
        data = response.get_json()

        assert response.status_code == 200
        page_data = data["data"]
        assert page_data["page"] == 2
        assert page_data["page_size"] == 5
        assert page_data["total"] == 25
        assert page_data["total_pages"] == 5
        assert len(page_data["items"]) == 5, "第二页应返回5条"
        # 排序: P0x4 -> P1按case_id升序，第6-10条依次为
        # TM-OD-0002/0003/0004 -> TM-UC-0004/0005
        actual_ids = [item["case_id"] for item in page_data["items"]]
        assert actual_ids == [
            "TM-OD-0002", "TM-OD-0003", "TM-OD-0004",
            "TM-UC-0004", "TM-UC-0005",
        ], "第二页应返回排序后的第6-10条"

    def test_list_cases_filter_by_module(
        self, cases_client: FlaskClient
    ) -> None:
        """
        模块筛选: module=用户中心，只返回该模块12条active用例
        """
        response = cases_client.get("/api/cases/?module=用户中心")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"]["total"] == 12, "用户中心active用例应为12条"
        assert len(data["data"]["items"]) == 12
        assert all(
            item["module"] == "用户中心" for item in data["data"]["items"]
        ), "返回项必须全部属于用户中心模块"

    def test_list_cases_filter_by_priority(
        self, cases_client: FlaskClient
    ) -> None:
        """
        优先级筛选: priority=P0返回4条P0用例；
        传入小写p0同样命中（大小写不敏感，统一大写匹配）
        """
        response = cases_client.get("/api/cases/?priority=P0")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"]["total"] == 4, "active的P0用例应为4条"
        assert all(
            item["priority"] == "P0" for item in data["data"]["items"]
        ), "返回项必须全部为P0"

        # 小写p0同样匹配
        lower_response = cases_client.get("/api/cases/?priority=p0")
        lower_data = lower_response.get_json()
        assert lower_response.status_code == 200
        assert lower_data["data"]["total"] == 4, "小写p0应与P0等价命中"

    def test_list_cases_filter_by_case_type(
        self, cases_client: FlaskClient
    ) -> None:
        """
        类型筛选: case_type=api，只返回22条api类型用例
        （用户中心12条 + 订单中心10条，板卡chip类型3条被排除）
        """
        response = cases_client.get("/api/cases/?case_type=api")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"]["total"] == 22, "active的api用例应为22条"
        assert len(data["data"]["items"]) == 20, "默认每页20条"
        assert all(
            item["case_type"] == "api" for item in data["data"]["items"]
        ), "返回项必须全部为api类型"

    def test_list_cases_filter_by_status_all(
        self, cases_client: FlaskClient
    ) -> None:
        """
        状态筛选: status=all，返回active+disabled全部27条，
        且disabled用例（TM-UC-0101/TM-OD-0101）包含在结果中
        """
        response = cases_client.get("/api/cases/?status=all&page_size=100")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"]["total"] == 27, "全部状态用例应为27条"
        assert len(data["data"]["items"]) == 27
        returned_ids = {item["case_id"] for item in data["data"]["items"]}
        assert "TM-UC-0101" in returned_ids, "disabled用例TM-UC-0101应包含"
        assert "TM-OD-0101" in returned_ids, "disabled用例TM-OD-0101应包含"

    def test_list_cases_keyword_search(
        self, cases_client: FlaskClient
    ) -> None:
        """
        关键字搜索: keyword=登录，模糊匹配name或description，
        命中3条（TM-UC-0001/0002名称含"登录"，TM-UC-0003描述含"登录"）
        """
        response = cases_client.get("/api/cases/?keyword=登录")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"]["total"] == 3, "含'登录'关键字的active用例应为3条"
        matched_ids = {item["case_id"] for item in data["data"]["items"]}
        assert matched_ids == {"TM-UC-0001", "TM-UC-0002", "TM-UC-0003"}

    def test_list_cases_invalid_page(
        self, cases_client: FlaskClient
    ) -> None:
        """
        非法页码: page=0，返回400统一错误格式，不抛500
        """
        response = cases_client.get("/api/cases/?page=0")
        data = response.get_json()

        assert response.status_code == 400, "page=0应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert data["data"] is None, "校验失败响应data应为null"
        assert "page" in data["message"], "错误信息应说明page参数问题"

        # 非整数页码同样400
        invalid_response = cases_client.get("/api/cases/?page=abc")
        assert invalid_response.status_code == 400, "page=abc应返回400"

    def test_list_cases_page_size_exceed_max(
        self, cases_client: FlaskClient
    ) -> None:
        """
        超限页大小: page_size=200超出上限100，返回400统一错误格式
        """
        response = cases_client.get("/api/cases/?page_size=200")
        data = response.get_json()

        assert response.status_code == 400, "page_size=200应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert "page_size" in data["message"], "错误信息应说明page_size问题"

    def test_list_cases_response_format(
        self, cases_client: FlaskClient
    ) -> None:
        """
        响应结构: data包含items/total/page/page_size/total_pages
        五个分页字段，items元素含case_id/name/module/priority等完整字段
        """
        response = cases_client.get("/api/cases/?page_size=10")
        data = response.get_json()

        assert response.status_code == 200
        # 统一响应三字段
        assert {"code", "message", "data"} <= set(data.keys())
        # 分页结构五字段
        page_data = data["data"]
        assert {
            "items", "total", "page", "page_size", "total_pages"
        } == set(page_data.keys()), "data应为分页五字段结构"
        # items元素字段完整性（_to_dict全量字段）
        assert len(page_data["items"]) > 0, "应返回非空items"
        first_item = page_data["items"][0]
        assert {
            "id", "case_id", "name", "module", "priority", "case_type",
            "status", "description", "creator", "created_at", "updated_at",
        } == set(first_item.keys()), "items元素应含用例全量字段"
