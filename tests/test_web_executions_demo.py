"""
TestMatrix Day22: 执行记录查询API测试（GET /api/executions/）

测试覆盖:
    列表:
        1. test_list_empty             空库返回200，total=0，items=[]
        2. test_list_success           单批次列表含完整汇总字段
        3. test_list_order_desc        多批次按created_at倒序，最新在前
        4. test_list_pagination        5批次分页: page_size=2共3页，末页1条
        5. test_list_invalid_page      page=0返回400
        6. test_list_invalid_page_size page_size=200超出上限返回400
    详情:
        7. test_detail_success         批次详情含summary汇总与items明细
        8. test_detail_failed_stack    failed明细error_message完整保留堆栈
        9. test_detail_not_found       不存在批次返回404统一错误格式
        10. test_detail_items_order    items按id升序与写入顺序一致

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证；执行数据全部经
    核心层create_execution/record_execution/finish_execution真实
    写入（零mock零真实外部依赖）。
"""

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import allure
import pytest
from flask.testing import FlaskClient

from src.core.case_manager import CaseManager
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 模拟失败堆栈文本（多行结构，验证原样透传不截断）
FAILED_STACK = (
    "AssertionError: expected 200 got 500\n"
    '  File "tests/test_login.py", line 42, in test_login\n'
    "    assert response.status_code == 200"
)


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（3条）

    为执行明细提供真实关联的用例元信息（case_id与明细记录对齐）。

    参数:
        无

    返回:
        无
    """
    with DatabaseSession.session_scope() as session:
        for case_id, name in [
            ("TM-UC-0001", "用户登录成功校验"),
            ("TM-UC-0002", "用户登录密码错误校验"),
            ("TM-UC-0003", "用户信息查询校验"),
        ]:
            session.add(
                models.TestCase(
                    case_id=case_id,
                    name=name,
                    module="用户中心",
                    priority="P1",
                    case_type="api",
                    status="active",
                    description="执行记录API测试种子用例",
                )
            )


def _create_finished_batch(
    records: list[tuple[int, str, Optional[str]]]
) -> str:
    """
    经核心层造一个已完成执行批次（内部方法）

    造数链路: create_execution生成批次号 -> 逐条record_execution写
    明细 -> finish_execution聚合upsert汇总记录，与生产链路完全一致。

    参数:
        records (list[tuple]): (用例序号, result, error_message)元组
            列表，用例序号1-3对应seed_cases的TM-UC-000N，
            error_message仅failed/error必填

    返回:
        str: 执行批次号
    """
    execution_id = CaseManager.create_execution(
        executor="tester", environment="dev"
    )
    for index, result, error_message in records:
        start_time = datetime.now()
        end_time = start_time + timedelta(seconds=0.5)
        CaseManager.record_execution(
            execution_id=execution_id,
            case_id=f"TM-UC-{index:04d}",
            case_name=f"用户校验用例{index}",
            result=result,
            start_time=start_time,
            end_time=end_time,
            duration=0.5,
            error_message=error_message,
        )
    CaseManager.finish_execution(execution_id)
    return execution_id


@pytest.fixture
def executions_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    执行记录API测试客户端fixture（临时SQLite文件库）

    将数据库指向pytest临时目录独立库文件并完成建表+用例种子造数
    （执行批次由各测试方法自行经核心层创建），前后重置
    DatabaseSession引擎单例，防止Windows下SQLite文件句柄残留导致
    tmp_path清理失败，也避免测试间引擎状态污染。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield测试客户端，前后完成引擎重置
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "TM_DB_SQLITE_PATH", str(tmp_path / "executions_api.db")
    )
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# 执行记录API测试
# ===========================================================================
@allure.feature("执行记录API")
@allure.story("执行记录查询")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestExecutionsQueryApi:
    """GET /api/executions/ 批次列表与 GET /api/executions/<id> 批次详情测试"""

    # ------------------------------------------------------------------
    # 批次列表
    # ------------------------------------------------------------------
    def test_list_empty(self, executions_client: FlaskClient) -> None:
        """
        空库查询: 无任何已完成批次，返回200且total=0、items=[]，
        不报错（空表count与分页查询均为正常路径）
        """
        response = executions_client.get("/api/executions/")
        data = response.get_json()

        assert response.status_code == 200, "空库查询应返回200"
        assert data["data"]["total"] == 0, "空库批次总数应为0"
        assert data["data"]["items"] == [], "空库items应为空列表"
        assert data["data"]["total_pages"] == 0, "空库总页数应为0"

    def test_list_success(self, executions_client: FlaskClient) -> None:
        """
        列表成功: 1个含3条明细的批次，列表返回200、total=1，
        items[0]含完整汇总字段且各结果计数正确
        """
        execution_id = _create_finished_batch([
            (1, "passed", None),
            (2, "failed", FAILED_STACK),
            (3, "skipped", None),
        ])

        response = executions_client.get("/api/executions/")
        data = response.get_json()

        assert response.status_code == 200
        page_data = data["data"]
        assert page_data["total"] == 1, "已完成批次总数应为1"
        assert page_data["page"] == 1, "默认页码应为1"
        assert page_data["page_size"] == 20, "默认每页条数应为20"
        assert page_data["total_pages"] == 1
        assert len(page_data["items"]) == 1

        item = page_data["items"][0]
        # 汇总字段完整性（序列化静态方法的全量字段）
        assert {
            "execution_id", "total_cases", "passed", "failed", "error",
            "skipped", "pass_rate", "created_at",
        } == set(item.keys()), "批次汇总应含完整字段"
        assert item["execution_id"] == execution_id
        assert item["total_cases"] == 3
        assert item["passed"] == 1
        assert item["failed"] == 1
        assert item["error"] == 0
        assert item["skipped"] == 1
        assert item["pass_rate"] == 0.3333, "通过率保留4位小数"
        # datetime已序列化为ISO字符串（不能断言等于datetime对象）
        assert isinstance(item["created_at"], str)

    def test_list_order_desc(self, executions_client: FlaskClient) -> None:
        """
        倒序排序: 分次finish 3个批次，列表items[0]为最后完成的批次
        （created_at倒序，最新完成批次在前）
        """
        first_id = _create_finished_batch([(1, "passed", None)])
        # created_at为数据库秒级精度，跨秒落库保证倒序可判定
        time.sleep(1.1)
        second_id = _create_finished_batch([(2, "passed", None)])
        time.sleep(1.1)
        third_id = _create_finished_batch([(3, "passed", None)])

        response = executions_client.get("/api/executions/")
        data = response.get_json()

        assert response.status_code == 200
        page_data = data["data"]
        assert page_data["total"] == 3
        actual_ids = [item["execution_id"] for item in page_data["items"]]
        assert actual_ids == [third_id, second_id, first_id], (
            "列表应按created_at倒序，最后完成的批次排首位"
        )

    def test_list_pagination(self, executions_client: FlaskClient) -> None:
        """
        分页查询: 5个批次，page=1&page_size=2返回2条/total=5/
        total_pages=3；page=3&page_size=2返回末页1条
        """
        for _ in range(5):
            _create_finished_batch([(1, "passed", None)])

        response = executions_client.get("/api/executions/?page=1&page_size=2")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"]["total"] == 5, "已完成批次总数应为5"
        assert data["data"]["total_pages"] == 3, "5条按每页2条应为3页"
        assert len(data["data"]["items"]) == 2, "第一页应返回2个批次"

        last_response = executions_client.get(
            "/api/executions/?page=3&page_size=2"
        )
        last_data = last_response.get_json()

        assert last_response.status_code == 200
        assert len(last_data["data"]["items"]) == 1, "末页应只剩1个批次"
        assert last_data["data"]["total"] == 5

    def test_list_invalid_page(self, executions_client: FlaskClient) -> None:
        """
        非法页码: page=0返回400统一错误格式，不抛500
        """
        response = executions_client.get("/api/executions/?page=0")
        data = response.get_json()

        assert response.status_code == 400, "page=0应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert data["data"] is None, "校验失败响应data应为null"
        assert "page" in data["message"], "错误信息应说明page参数问题"

        # 非整数页码同样400
        invalid_response = executions_client.get("/api/executions/?page=abc")
        assert invalid_response.status_code == 400, "page=abc应返回400"

    def test_list_invalid_page_size(
        self, executions_client: FlaskClient
    ) -> None:
        """
        超限页大小: page_size=200超出上限100，返回400统一错误格式
        """
        response = executions_client.get("/api/executions/?page_size=200")
        data = response.get_json()

        assert response.status_code == 400, "page_size=200应返回400"
        assert data["code"] == 400, "响应体code应为400"
        assert "page_size" in data["message"], "错误信息应说明page_size问题"

    # ------------------------------------------------------------------
    # 批次详情
    # ------------------------------------------------------------------
    def test_detail_success(self, executions_client: FlaskClient) -> None:
        """
        详情成功: 批次含passed/failed/skipped各1条，返回summary汇总
        与items明细，各结果计数正确、明细字段完整
        """
        execution_id = _create_finished_batch([
            (1, "passed", None),
            (2, "failed", FAILED_STACK),
            (3, "skipped", None),
        ])

        response = executions_client.get(f"/api/executions/{execution_id}")
        data = response.get_json()

        assert response.status_code == 200
        detail = data["data"]
        assert {"summary", "items"} == set(detail.keys()), (
            "详情应为summary+items双层结构"
        )

        summary = detail["summary"]
        assert summary["execution_id"] == execution_id
        assert summary["total_cases"] == 3
        assert summary["passed"] == 1
        assert summary["failed"] == 1
        assert summary["skipped"] == 1

        items = detail["items"]
        assert len(items) == 3, "明细应返回全部3条"
        # 明细字段完整性（序列化静态方法的全量字段）
        assert {
            "id", "execution_id", "case_id", "case_name", "result",
            "start_time", "end_time", "duration", "error_message",
            "environment", "executor", "created_at",
        } == set(items[0].keys()), "明细应含单用例全量字段"
        # datetime已序列化为ISO字符串（不能断言等于datetime对象）
        assert all(isinstance(item["start_time"], str) for item in items), (
            "start_time应为ISO格式字符串"
        )
        # 可空字段原样保留None（不转空串）
        passed_item = next(
            item for item in items if item["result"] == "passed"
        )
        assert passed_item["error_message"] is None, (
            "通过用例error_message应原样为None"
        )

    def test_detail_failed_stack(
        self, executions_client: FlaskClient
    ) -> None:
        """
        失败堆栈: failed明细的error_message完整保留模拟堆栈文本
        （含AssertionError关键词与多行结构），不截断
        """
        execution_id = _create_finished_batch([
            (1, "passed", None),
            (2, "failed", FAILED_STACK),
        ])

        response = executions_client.get(f"/api/executions/{execution_id}")
        data = response.get_json()

        assert response.status_code == 200
        items = data["data"]["items"]
        failed_items = [item for item in items if item["result"] == "failed"]
        assert len(failed_items) == 1, "失败明细应为1条"

        error_message = failed_items[0]["error_message"]
        assert "AssertionError" in error_message, "堆栈应含异常类型关键词"
        assert error_message == FAILED_STACK, "失败堆栈应原样完整返回不截断"

    def test_detail_not_found(self, executions_client: FlaskClient) -> None:
        """
        批次不存在: 查询不存在的批次号，返回404统一错误格式，
        detail携带查询的批次号
        """
        response = executions_client.get("/api/executions/RUN-NOT-EXIST")
        data = response.get_json()

        assert response.status_code == 404, "查询不存在批次应返回404"
        assert data["code"] == 404, "响应体code应为404"
        assert "执行批次不存在" in data["message"], "错误信息应说明批次不存在"
        assert data["data"] == {"execution_id": "RUN-NOT-EXIST"}, (
            "detail应携带查询批次号"
        )

    def test_detail_items_order(
        self, executions_client: FlaskClient
    ) -> None:
        """
        明细排序: items按id升序，与record_execution写入顺序一致
        （即执行先后顺序）
        """
        execution_id = _create_finished_batch([
            (1, "passed", None),
            (2, "failed", FAILED_STACK),
            (3, "skipped", None),
        ])

        response = executions_client.get(f"/api/executions/{execution_id}")
        data = response.get_json()

        assert response.status_code == 200
        items = data["data"]["items"]
        actual_case_ids = [item["case_id"] for item in items]
        assert actual_case_ids == [
            "TM-UC-0001", "TM-UC-0002", "TM-UC-0003",
        ], "items应按id升序，与写入顺序一致"
        record_ids = [item["id"] for item in items]
        assert record_ids == sorted(record_ids), "明细id应单调递增"
