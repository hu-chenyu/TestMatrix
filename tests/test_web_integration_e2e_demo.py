"""
TestMatrix Day28: 跨API端到端集成测试
（Flask test client 跨 blueprint，用例→执行→报告全链路数据一致性）

测试覆盖（15条，三组）:
    A组 跨blueprint核心旅程（7条）:
        1. test_e2e_create_case_trigger_execute_verify_detail
           建用例→触发→轮询终态→详情核对单条结果
        2. test_e2e_execution_flows_to_reports_summary
           执行结果流入reports汇总（计数/加权通过率对得上）
        3. test_e2e_execution_flows_to_reports_module_distribution
           执行结果流入reports模块分布（两模块各2条）
        4. test_e2e_module_filter_executes_subset_only
           模块筛选只执行子集且reports统计同步收窄
        5. test_e2e_disabled_case_excluded_from_trigger
           disabled用例被触发排除（列表/执行明细双重核对）
        6. test_e2e_import_cases_then_execute
           YAML导入→列表可查→触发执行结果与奇偶规则一致
        7. test_e2e_multiple_batches_reports_trend
           双批次执行→trend两条记录逐条核对
    B组 全API联调冒烟（4条）:
        8. test_e2e_health_version_root_smoke
           base蓝图三接口三段式响应齐全
        9. test_e2e_case_crud_lifecycle
           POST→GET→PUT→GET→DELETE→GET全生命周期
        10. test_e2e_cases_list_combined_filters
           模块+优先级+状态组合筛选与关键字搜索
        11. test_e2e_executions_list_detail_status_consistent
           列表/详情/状态三接口同批次数据一致
    C组 错误边界端到端（4条）:
        12. test_e2e_trigger_no_active_cases_400
           全部禁用后触发400"无符合条件的用例"
        13. test_e2e_trigger_invalid_executor_400
           非法executor触发400"executor非法"
        14. test_e2e_nonexistent_execution_404_chain
           不存在批次三接口（详情/状态/事件流）均404
        15. test_e2e_nonexistent_case_404_chain
           不存在用例三接口（GET/PUT/DELETE）均404

集成测试定位:
    - 既有9个web测试文件单blueprint各测各的，本文件跨4个blueprint
      （base/cases/executions/reports）组合调用，验证"用例→执行→报告"
      全链路数据一致性: 执行明细的逐条结果与reports的聚合数字必须
      对得上，而不是只断言各接口返回200
    - 轮询批次终态一律走GET /api/executions/<id>/status真实HTTP，
      禁止直调CaseManager.get_execution_status（集成测试必须走API
      层，见PROJECT_CONTEXT 7.12）
    - 触发后台线程的用例统一_wait_batch_terminal轮询至终态后再断言
      （禁止固定sleep赌时序）；conftest的autouse fixture已全局禁用
      真实通知渠道，触发执行零真实邮件/零网络

测试基建:
    function级临时SQLite文件库（每条用例独立库，保证summary等聚合
    断言精确无残留）+ Flask test client + 种子4条active用例
    （TM-UC-0001/0002用户中心、TM-OD-0001/0002订单中心，
    末位奇偶各半，模拟执行必有通过有失败便于交叉核对）。
"""

import time
from io import BytesIO
from pathlib import Path
from typing import Iterator, Optional

import allure
import pytest
import yaml
from flask.testing import FlaskClient

from src.core import event_bus
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 轮询参数: 批次模拟执行约0.01s/条，预算远超实际耗时防flaky（口径同trigger测试）
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL_SECONDS = 0.3


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布:
        - 用户中心: TM-UC-0001（奇→failed）、TM-UC-0002（偶→passed）
        - 订单中心: TM-OD-0001（奇→failed）、TM-OD-0002（偶→passed）
    模拟执行结果可手算: 全量4条=2过2挂（通过率0.5），
    用户中心/订单中心各1过1挂，供reports交叉断言。

    参数:
        无

    返回:
        无
    """
    seeds = [
        ("TM-UC-0001", "用户登录成功校验", "用户中心"),
        ("TM-UC-0002", "用户登录密码错误校验", "用户中心"),
        ("TM-OD-0001", "订单创建成功校验", "订单中心"),
        ("TM-OD-0002", "订单状态流转校验", "订单中心"),
    ]
    with DatabaseSession.session_scope() as session:
        for case_id, name, module in seeds:
            session.add(
                models.TestCase(
                    case_id=case_id,
                    name=name,
                    module=module,
                    priority="P1",
                    case_type="api",
                    status="active",
                    description="跨API集成测试种子用例",
                )
            )


@pytest.fixture
def e2e_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    跨API集成测试客户端fixture（function级临时SQLite文件库）

    每条用例独立建库+种子造数（保证summary/trend等聚合断言精确
    无跨用例残留），前后重置DatabaseSession引擎单例（防Windows
    文件句柄残留与测试间引擎状态污染）；teardown额外复位事件
    通道注册表——触发执行会建通道，终态虽自动清理，多复位一次
    兜底防daemon线程late publish串测试（见PROJECT_CONTEXT 7.13）；
    清除TM_EXECUTOR环境变量保证未显式传executor的触发确定走
    simulated执行器。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield测试客户端，teardown完成
                              引擎重置+通道复位+临时库文件清理
    """
    db_path = tmp_path / "integration_e2e.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_path.unlink(missing_ok=True)


def _wait_batch_terminal(
    client: FlaskClient, execution_id: str
) -> dict:
    """
    轮询批次状态至终态finished/failed（内部方法，走真实HTTP）

    集成测试铁律: 终态轮询必须走GET /api/executions/<id>/status
    接口层，禁止直调CaseManager.get_execution_status——本文件验证
    的是API全链路，核心层直调会绕过响应封装与路由解析。
    同时防teardown竞态（7.12）: 触发过后台线程的用例结束前必须
    等批次进入终态，否则fixture的DatabaseSession.reset()可能与
    执行中的线程竞态。

    参数:
        client (FlaskClient): Flask测试客户端
        execution_id (str): 执行批次号

    返回:
        dict: 终态状态字典（含status与冗余计数）

    异常:
        AssertionError: 轮询预算内未达终态时断言失败
    """
    status_data: Optional[dict] = None
    for _ in range(POLL_MAX_ATTEMPTS):
        response = client.get(f"/api/executions/{execution_id}/status")
        assert response.status_code == 200, "轮询期间状态查询应始终200"
        status_data = response.get_json()["data"]
        if status_data["status"] in ("finished", "failed"):
            return status_data
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"批次在轮询预算内未达终态: {execution_id}, "
        f"最后状态: {status_data['status'] if status_data else '无响应'}"
    )
    raise AssertionError("不可达: pytest.fail后代码不会执行到此处")


def _create_case_api(
    client: FlaskClient,
    case_id: str,
    name: str,
    module: str,
    case_type: str = "api",
    priority: str = "P1",
) -> dict:
    """
    走真实HTTP创建用例（内部方法）

    参数:
        client (FlaskClient): Flask测试客户端
        case_id (str): 用例业务编号
        name (str): 用例名称
        module (str): 所属模块
        case_type (str): 用例类型，默认api
        priority (str): 优先级，默认P1

    返回:
        dict: 创建成功后的用例完整字段

    异常:
        AssertionError: 创建未返回201时断言失败
    """
    response = client.post(
        "/api/cases/",
        json={
            "case_id": case_id,
            "name": name,
            "module": module,
            "case_type": case_type,
            "priority": priority,
        },
    )
    assert response.status_code == 201, "创建用例应返回201"
    return response.get_json()["data"]


# ===========================================================================
# A组: 跨blueprint核心旅程
# ===========================================================================
@allure.feature("跨API集成测试")
@allure.story("用例→执行→报告核心旅程")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestE2ECoreJourney:
    """跨cases/executions/reports蓝图的端到端核心旅程测试"""

    def test_e2e_create_case_trigger_execute_verify_detail(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        建用例→触发→执行→详情核对: POST新建TM-E2E-0002（末位2→
        偶→passed），全量触发轮询至finished，批次详情items含该用例
        且result=="passed"（创建的用例真实进入执行批次）
        """
        _create_case_api(
            e2e_client, "TM-E2E-0002", "端到端创建触发校验", "用户中心"
        )

        trigger_response = e2e_client.post("/api/executions/trigger")
        assert trigger_response.status_code == 202, "触发应返回202受理"
        execution_id = trigger_response.get_json()["data"]["execution_id"]

        # 轮询走API层至终态（全量5条=种子4+新建1）
        final = _wait_batch_terminal(e2e_client, execution_id)
        assert final["status"] == "finished", "批次最终应为finished"
        assert final["total_cases"] == 5, "新建用例应计入执行批次"

        detail_response = e2e_client.get(f"/api/executions/{execution_id}")
        assert detail_response.status_code == 200
        detail = detail_response.get_json()["data"]
        assert len(detail["items"]) == 5, "执行明细应含全部5条用例"
        target = next(
            (
                item
                for item in detail["items"]
                if item["case_id"] == "TM-E2E-0002"
            ),
            None,
        )
        assert target is not None, "新建用例应出现在执行明细中"
        assert target["result"] == "passed", "末位偶数用例应通过"
        assert target["error_message"] is None, "通过用例无失败堆栈"

    def test_e2e_execution_flows_to_reports_summary(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        执行结果流入reports汇总: 全量触发（4条种子）→finished后
        汇总total_batches==1、total_executed==4、passed==2、failed==2、
        加权通过率0.5（执行明细逐条结果与reports聚合数字对得上）
        """
        trigger_response = e2e_client.post("/api/executions/trigger")
        execution_id = trigger_response.get_json()["data"]["execution_id"]
        final = _wait_batch_terminal(e2e_client, execution_id)
        assert final["status"] == "finished"

        summary_response = e2e_client.get("/api/reports/summary")
        assert summary_response.status_code == 200
        summary = summary_response.get_json()["data"]

        assert summary["total_batches"] == 1, "完成批次数应为1"
        assert summary["total_executed"] == 4, "累计执行数应为4"
        assert summary["passed"] == 2, "末位偶数2条应通过"
        assert summary["failed"] == 2, "末位奇数2条应失败"
        assert summary["error"] == 0, "模拟执行无error结果"
        assert summary["skipped"] == 0, "模拟执行无skipped结果"
        assert summary["overall_pass_rate"] == 0.5, (
            "加权通过率应为总通过2/总执行4=0.5"
        )
        assert summary["latest_batch"]["execution_id"] == execution_id, (
            "最新批次应为刚完成的批次"
        )

    def test_e2e_execution_flows_to_reports_module_distribution(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        执行结果流入reports模块分布: 全量触发后模块分布列表长度==2，
        用户中心与订单中心各total==2（明细的module归属与分布聚合
        一致，每模块1过1挂）
        """
        trigger_response = e2e_client.post("/api/executions/trigger")
        execution_id = trigger_response.get_json()["data"]["execution_id"]
        _wait_batch_terminal(e2e_client, execution_id)

        dist_response = e2e_client.get("/api/reports/module-distribution")
        assert dist_response.status_code == 200
        distribution = dist_response.get_json()["data"]

        assert len(distribution) == 2, "种子仅两个模块，分布应为2项"
        by_module = {item["module"]: item for item in distribution}
        assert by_module["用户中心"]["total"] == 2, "用户中心应执行2条"
        assert by_module["订单中心"]["total"] == 2, "订单中心应执行2条"
        assert by_module["用户中心"]["passed"] == 1, "用户中心1过1挂"
        assert by_module["用户中心"]["failed"] == 1
        assert by_module["订单中心"]["passed"] == 1, "订单中心1过1挂"
        assert by_module["订单中心"]["failed"] == 1

    def test_e2e_module_filter_executes_subset_only(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        模块筛选只执行子集: trigger带module=用户中心只执行2条TM-UC，
        详情items全为TM-UC前缀，reports汇总total_executed同步收窄为2
        （筛选参数贯穿执行与报告两层）
        """
        trigger_response = e2e_client.post(
            "/api/executions/trigger", json={"module": "用户中心"}
        )
        assert trigger_response.status_code == 202
        execution_id = trigger_response.get_json()["data"]["execution_id"]

        final = _wait_batch_terminal(e2e_client, execution_id)
        assert final["status"] == "finished"
        assert final["total_cases"] == 2, "模块筛选应只命中用户中心2条"

        detail_response = e2e_client.get(f"/api/executions/{execution_id}")
        detail = detail_response.get_json()["data"]
        assert len(detail["items"]) == 2, "执行明细应仅2条"
        assert all(
            item["case_id"].startswith("TM-UC")
            for item in detail["items"]
        ), "明细应全为TM-UC前缀"

        summary_response = e2e_client.get("/api/reports/summary")
        summary = summary_response.get_json()["data"]
        assert summary["total_executed"] == 2, "reports统计应同步收窄为2"

    def test_e2e_disabled_case_excluded_from_trigger(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        disabled用例被触发排除: 新建TM-DIS-0001后PUT置disabled，
        全量触发执行明细不含该用例（仍为种子4条，disabled不进批次）
        """
        _create_case_api(
            e2e_client, "TM-DIS-0001", "端到端禁用排除校验", "用户中心"
        )
        put_response = e2e_client.put(
            "/api/cases/TM-DIS-0001", json={"status": "disabled"}
        )
        assert put_response.status_code == 200, "禁用更新应成功"

        trigger_response = e2e_client.post("/api/executions/trigger")
        execution_id = trigger_response.get_json()["data"]["execution_id"]
        final = _wait_batch_terminal(e2e_client, execution_id)
        assert final["status"] == "finished"
        assert final["total_cases"] == 4, "disabled用例不应计入批次"

        detail_response = e2e_client.get(f"/api/executions/{execution_id}")
        detail = detail_response.get_json()["data"]
        case_ids = {item["case_id"] for item in detail["items"]}
        assert "TM-DIS-0001" not in case_ids, "disabled用例不应被执行"

    def test_e2e_import_cases_then_execute(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        导入→执行全链路: multipart上传2条YAML（TM-IMP-0001/0002）
        inserted==2，active列表可查到导入用例，触发执行明细含
        TM-IMP-0001（末位1奇→failed）与TM-IMP-0002（末位2偶→passed）
        """
        import_cases = [
            {
                "case_id": "TM-IMP-0001",
                "name": "集成导入用例一",
                "module": "用户中心",
                "case_type": "api",
                "priority": "P1",
            },
            {
                "case_id": "TM-IMP-0002",
                "name": "集成导入用例二",
                "module": "订单中心",
                "case_type": "api",
                "priority": "P1",
            },
        ]
        yaml_content = yaml.dump(
            import_cases, allow_unicode=True, sort_keys=False
        )
        import_response = e2e_client.post(
            "/api/cases/import",
            data={
                "file": (BytesIO(yaml_content.encode("utf-8")), "cases.yaml")
            },
            content_type="multipart/form-data",
        )
        assert import_response.status_code == 200, "导入应返回200"
        import_data = import_response.get_json()["data"]
        assert import_data["inserted"] == 2, "空冲突导入应全部新增"

        list_response = e2e_client.get("/api/cases/?status=active")
        list_data = list_response.get_json()["data"]
        listed_ids = {item["case_id"] for item in list_data["items"]}
        assert "TM-IMP-0001" in listed_ids, "导入用例应可查到"
        assert list_data["total"] == 6, "种子4+导入2应为6条active"

        trigger_response = e2e_client.post("/api/executions/trigger")
        execution_id = trigger_response.get_json()["data"]["execution_id"]
        _wait_batch_terminal(e2e_client, execution_id)

        detail_response = e2e_client.get(f"/api/executions/{execution_id}")
        detail = detail_response.get_json()["data"]
        results = {
            item["case_id"]: item["result"] for item in detail["items"]
        }
        assert len(detail["items"]) == 6, "全量应执行6条"
        assert results["TM-IMP-0001"] == "failed", "末位奇数应失败"
        assert results["TM-IMP-0002"] == "passed", "末位偶数应通过"

    def test_e2e_multiple_batches_reports_trend(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        双批次trend: 连续两次全量触发（各自轮询至finished），trend
        返回2条记录，每条total_cases==4、pass_rate==0.5（多批次
        聚合与批次级冗余统计一致）
        """
        for _ in range(2):
            trigger_response = e2e_client.post("/api/executions/trigger")
            execution_id = trigger_response.get_json()["data"]["execution_id"]
            final = _wait_batch_terminal(e2e_client, execution_id)
            assert final["status"] == "finished", "每批均应正常完成"

        trend_response = e2e_client.get("/api/reports/trend?limit=10")
        assert trend_response.status_code == 200
        trend = trend_response.get_json()["data"]

        assert len(trend) == 2, "两次完成批次应产生2条趋势记录"
        for entry in trend:
            assert entry["total_cases"] == 4, "每批应执行4条"
            assert entry["passed"] == 2
            assert entry["failed"] == 2
            assert entry["pass_rate"] == 0.5, "每批通过率应为0.5"


# ===========================================================================
# B组: 全API联调冒烟
# ===========================================================================
@allure.feature("跨API集成测试")
@allure.story("全API联调冒烟")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestE2EApiSmoke:
    """base/cases/executions蓝图全接口联调冒烟测试"""

    def test_e2e_health_version_root_smoke(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        base蓝图冒烟: GET /、/health、/api/version均200，三段式
        code/message/data字段齐全（统一响应契约在三个入口一致）
        """
        for path in ("/", "/health", "/api/version"):
            response = e2e_client.get(path)
            assert response.status_code == 200, f"{path}应返回200"
            body = response.get_json()
            assert set(body.keys()) >= {"code", "message", "data"}, (
                f"{path}响应应含三段式字段"
            )
            assert body["code"] == 200, f"{path}业务码应为200"

    def test_e2e_case_crud_lifecycle(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        用例CRUD全生命周期: POST创建201→GET详情name一致→PUT改名
        200→GET复核已更新→DELETE 204→GET 404（单用例贯穿增查改删）
        """
        created = _create_case_api(
            e2e_client, "TM-CRUD-E2E-0001", "生命周期原始名称", "用户中心"
        )
        assert created["name"] == "生命周期原始名称"

        get_response = e2e_client.get("/api/cases/TM-CRUD-E2E-0001")
        assert get_response.status_code == 200
        assert get_response.get_json()["data"]["name"] == "生命周期原始名称"

        put_response = e2e_client.put(
            "/api/cases/TM-CRUD-E2E-0001",
            json={"name": "生命周期更新名称"},
        )
        assert put_response.status_code == 200, "更新应返回200"

        reget_response = e2e_client.get("/api/cases/TM-CRUD-E2E-0001")
        assert (
            reget_response.get_json()["data"]["name"] == "生命周期更新名称"
        ), "更新后名称应已生效"

        delete_response = e2e_client.delete("/api/cases/TM-CRUD-E2E-0001")
        assert delete_response.status_code == 204, "删除成功应返回204"

        after_response = e2e_client.get("/api/cases/TM-CRUD-E2E-0001")
        assert after_response.status_code == 404, "删除后查询应404"

    def test_e2e_cases_list_combined_filters(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        列表组合筛选: 新建TM-E2E-0003（用户中心/P2）后，
        module+priority+status组合筛选items全为用户中心+P1+active且
        total==2（P2新建用例被正确排除）；关键字搜索命中新建用例
        """
        _create_case_api(
            e2e_client,
            "TM-E2E-0003",
            "端到端组合筛选专用用例",
            "用户中心",
            priority="P2",
        )

        filtered_response = e2e_client.get(
            "/api/cases/?module=用户中心&priority=P1&status=active"
        )
        assert filtered_response.status_code == 200
        filtered = filtered_response.get_json()["data"]
        assert filtered["total"] == 2, "用户中心P1active应为种子2条"
        assert all(
            item["module"] == "用户中心"
            and item["priority"] == "P1"
            and item["status"] == "active"
            for item in filtered["items"]
        ), "筛选结果应全部满足三个维度条件"

        keyword_response = e2e_client.get(
            "/api/cases/?keyword=组合筛选专用"
        )
        keyword_data = keyword_response.get_json()["data"]
        keyword_ids = {item["case_id"] for item in keyword_data["items"]}
        assert "TM-E2E-0003" in keyword_ids, "关键字应命中新建用例名称"

    def test_e2e_executions_list_detail_status_consistent(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        列表/详情/状态三接口数据一致: 触发至finished后，批次列表
        items[0]的execution_id与触发返回一致，详情summary与列表
        首条逐字段一致，状态接口的冗余计数与详情汇总一致
        """
        trigger_response = e2e_client.post("/api/executions/trigger")
        execution_id = trigger_response.get_json()["data"]["execution_id"]
        _wait_batch_terminal(e2e_client, execution_id)

        list_response = e2e_client.get("/api/executions/")
        assert list_response.status_code == 200
        list_data = list_response.get_json()["data"]
        assert list_data["total"] == 1, "仅一个已完成批次"
        first_item = list_data["items"][0]
        assert first_item["execution_id"] == execution_id, (
            "列表首条应为刚完成的批次"
        )

        detail_response = e2e_client.get(f"/api/executions/{execution_id}")
        summary = detail_response.get_json()["data"]["summary"]
        for field in (
            "execution_id",
            "total_cases",
            "passed",
            "failed",
            "error",
            "skipped",
            "pass_rate",
            "created_at",
        ):
            assert summary[field] == first_item[field], (
                f"详情与列表首条的{field}应一致"
            )

        status_response = e2e_client.get(
            f"/api/executions/{execution_id}/status"
        )
        status_data = status_response.get_json()["data"]
        assert status_data["status"] == "finished", "状态应为finished"
        for field in (
            "total_cases",
            "passed",
            "failed",
            "error",
            "skipped",
            "pass_rate",
        ):
            assert status_data[field] == summary[field], (
                f"状态接口与详情汇总的{field}应一致"
            )


# ===========================================================================
# C组: 错误边界端到端
# ===========================================================================
@allure.feature("跨API集成测试")
@allure.story("错误边界端到端")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestE2EErrorBoundary:
    """跨API错误边界（400/404）端到端测试"""

    def test_e2e_trigger_no_active_cases_400(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        无可用用例触发: 4条种子全部PUT置disabled后全量触发，
        返回400且message含"无符合条件的用例"
        """
        for case_id in (
            "TM-UC-0001",
            "TM-UC-0002",
            "TM-OD-0001",
            "TM-OD-0002",
        ):
            put_response = e2e_client.put(
                f"/api/cases/{case_id}", json={"status": "disabled"}
            )
            assert put_response.status_code == 200, "禁用更新应成功"

        trigger_response = e2e_client.post("/api/executions/trigger")
        body = trigger_response.get_json()
        assert trigger_response.status_code == 400, "无可用用例应400"
        assert "无符合条件的用例" in body["message"]
        assert body["code"] == 400

    def test_e2e_trigger_invalid_executor_400(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        非法执行器触发: executor=invalid不在合法值集合，返回400且
        message含"executor非法"（参数校验先于用例筛选）
        """
        trigger_response = e2e_client.post(
            "/api/executions/trigger", json={"executor": "invalid"}
        )
        body = trigger_response.get_json()
        assert trigger_response.status_code == 400, "非法执行器应400"
        assert "executor非法" in body["message"]
        assert body["code"] == 400

    def test_e2e_nonexistent_execution_404_chain(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        不存在批次404链: GET /executions/RUN-GHOST、/status、/events
        三个接口均404（events为流式接口但批次不存在时在流开始前
        即转JSON错误响应，非挂死流）
        """
        ghost_id = "RUN-GHOST-20990101-000000"
        for suffix in ("", "/status", "/events"):
            response = e2e_client.get(f"/api/executions/{ghost_id}{suffix}")
            assert response.status_code == 404, (
                f"不存在批次的{suffix or '详情'}应404"
            )
            body = response.get_json()
            assert body["code"] == 404, "业务码应为404"

    def test_e2e_nonexistent_case_404_chain(
        self, e2e_client: FlaskClient
    ) -> None:
        """
        不存在用例404链: GET/PUT/DELETE /cases/GHOST-CASE三接口
        均404统一错误格式
        """
        assert (
            e2e_client.get("/api/cases/GHOST-CASE").status_code == 404
        ), "查询不存在用例应404"

        put_response = e2e_client.put(
            "/api/cases/GHOST-CASE", json={"name": "幽灵更新"}
        )
        assert put_response.status_code == 404, "更新不存在用例应404"

        delete_response = e2e_client.delete("/api/cases/GHOST-CASE")
        assert delete_response.status_code == 404, "删除不存在用例应404"
