"""
TestMatrix 阶段B-2: P2 安全与健壮性修复的回归测试

测试覆盖（本文件 6 组，对应本阶段修复的 P2）:
    A组 P2-1 /health 生产环境不外泄数据库内部细节
        1. test_health_production_masks_db_error_detail
        2. test_health_testing_shows_detail_for_debugging
    B组 P2-5 生产环境缺 SECRET_KEY fail-fast
        3. test_production_missing_secret_key_raises
        4. test_production_with_secret_key_works
        5. test_dev_missing_secret_key_still_random
        6. test_production_app_factory_raises_without_secret_key
    C组 P2-9 异常分流改类型判断（不再依赖错误文案子串）
        7. test_db_error_containing_notfound_word_is_not_404
        8. test_typed_not_found_is_translated_to_404
        9. test_typed_conflict_is_translated_to_409
        10. test_legacy_substring_routing_still_works
        11. test_typed_exceptions_are_backward_compatible
    D组 P2-23 create_case 并发唯一约束冲突转 409
        12. test_concurrent_duplicate_case_raises_typed_conflict
        13. test_duplicate_case_via_api_returns_409
    E组 P2-12 get_bool 无法识别的值返回 default
        14. test_get_bool_unrecognized_returns_default
        15. test_get_bool_explicit_values_unchanged
        16. test_get_bool_absent_returns_default
    F组 P2-14 / P2-15 / P2-17 / P2-28
        17. test_parse_results_dir_tolerates_deleted_file
        18. test_parse_results_dir_returns_partial_results
        19. test_numeric_case_id_is_normalized
        20. test_boolean_and_float_normalized
        21. test_missing_required_field_still_raises
        22. test_numeric_case_id_via_yaml_works
        23. test_filter_cases_with_none_tags_does_not_raise
        24. test_filter_cases_normal_flow_unchanged
        25. test_router_skips_channel_without_message_template

测试铁律（对齐 PROJECT_CONTEXT.md 7.12/7.19 + 验收清单第三条）:
    - 无固定 sleep 赌时序
    - 每条用例独立临时 SQLite 库，teardown 严格复位引擎与事件通道
    - 全程 fake:// 内存后端，零真实 Redis / 零真实网络
    - 全程 loguru，无 print
"""

import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from src.common.env_manager import env_manager
from src.core import event_bus
from src.core.case_manager import (
    CaseConflictError,
    CaseManager,
    CaseManagerError,
    CaseNotFoundError,
)
from src.core.data_driver import DataDriver, DataDriverError
from src.core.notification import BaseNotifier, Notification, NotificationRouter
from src.core.report_analyzer import ReportAnalyzer, StatisticsResult
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.config import DevelopmentConfig, ProductionConfig, resolve_secret_key


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture
def db_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    隔离临时 SQLite 库环境（每条用例独立文件，用例间零污染）

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture
        tmp_path (Path): pytest临时目录fixture

    返回:
        Path: 本条用例的库文件路径
    """
    db_file = tmp_path / "p2_hardening.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_file.unlink(missing_ok=True)


# ===========================================================================
# A组: P2-1 /health 外泄
# ===========================================================================
class TestHealthLeakGate:
    """P2-1: /health 按 TESTING 门控决定是否回显数据库内部细节"""

    def test_health_production_masks_db_error_detail(self) -> None:
        """
        生产模式（TESTING=False）不得回显连接URL与SQL

        回归点: 修复前无条件回显 str(exc)，SQLAlchemy 异常文本含完整连接URL
        （MySQL 模式下含用户名/库名）与SQL片段，而 /health 无鉴权且常被
        监控轮询。
        """
        app = create_app("dev")
        app.config["TESTING"] = False
        client = app.test_client()

        leaked = (
            "(sqlite3.OperationalError) unable to open database file\n"
            "[SQL: SELECT 1]\n"
            "mysql+pymysql://root:Sup3rSecret@10.0.0.5:3306/testmatrix"
        )
        with patch(
            "src.web.routes.base._probe_database", side_effect=RuntimeError(leaked)
        ):
            response = client.get("/health")

        body = response.get_data(as_text=True)
        assert response.status_code == 503
        assert "Sup3rSecret" not in body, "生产模式泄露数据库密码"
        assert "mysql+pymysql://" not in body, "生产模式泄露连接URL"
        assert "SELECT 1" not in body, "生产模式泄露SQL"
        assert "RuntimeError" in body, "生产模式仍应保留异常类型名供排障"

    def test_health_testing_shows_detail_for_debugging(
        self, db_path_env: Path
    ) -> None:
        """
        TESTING 模式回显完整详情（口径同 6.7 与 exceptions.py，不回归）
        """
        client = create_app("test").test_client()
        assert client.application.config["TESTING"] is True

        detail = "诊断细节: 连接到 mysql+pymysql://root:Secret@h/db 失败"
        with patch(
            "src.web.routes.base._probe_database", side_effect=RuntimeError(detail)
        ):
            response = client.get("/health")

        body = response.get_data(as_text=True)
        assert response.status_code == 503
        assert "Secret" in body, "TESTING 模式应回显详情供联调定位"


# ===========================================================================
# B组: P2-5 SECRET_KEY fail-fast
# ===========================================================================
class TestSecretKeyFailFast:
    """P2-5: 生产环境缺 SECRET_KEY 必须拒绝启动"""

    def test_production_missing_secret_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        生产环境未配置 TM_SECRET_KEY 时抛 ValueError

        回归点: 修复前只 print 警告并降级为进程级随机密钥——"配置错了却
        在运行中悄悄降级"的最坏形态：服务看着正常，登录态却随机失效
        （每次重启即变 + 多 worker 各不相同）。
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)

        with pytest.raises(ValueError, match="TM_SECRET_KEY"):
            resolve_secret_key(ProductionConfig)

    def test_production_with_secret_key_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产环境配置了密钥时正常返回（不回归）"""
        monkeypatch.setenv("TM_SECRET_KEY", "explicit-prod-key")
        assert resolve_secret_key(ProductionConfig) == "explicit-prod-key"

    def test_dev_missing_secret_key_still_random(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        dev/test 缺密钥仍降级为随机 key（本地零配置体验不回归）
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)
        generated = resolve_secret_key(DevelopmentConfig)
        assert len(generated) == 64, "随机密钥应为 32 字节 hex"
        assert generated != resolve_secret_key(DevelopmentConfig), "两次应不同"

    def test_production_app_factory_raises_without_secret_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        create_app("prod") 缺密钥时整体启动失败

        回归点: fail-fast 必须作用在应用工厂这一真实入口上，而不只是
        单测直接调 resolve_secret_key。
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)
        with pytest.raises(ValueError, match="TM_SECRET_KEY"):
            create_app("prod")


# ===========================================================================
# C组: P2-9 异常分流改类型判断
# ===========================================================================
class TestTypedExceptionRouting:
    """P2-9: 路由层异常翻译按类型判定，不依赖错误文案子串"""

    def test_db_error_containing_notfound_word_is_not_404(
        self, db_path_env: Path
    ) -> None:
        """
        含"不存在"字样的数据库异常不得被误判为业务404

        回归点: 修复前路由层用 `if "不存在" not in str(exc)` 判定是否转404。
        任何 DB 异常消息里恰好含"不存在"（"表 test_cases 不存在"、
        "database does not exist"）都会被静默转成 404，真实故障被吞掉、
        客户端看到误导性的"资源不存在"。
        """
        client = create_app("test").test_client()
        poisoned = (
            "用例更新数据库异常: no such table: test_cases (表 test_cases 不存在)"
        )

        with patch.object(
            CaseManager, "update_case", side_effect=CaseManagerError(poisoned)
        ):
            response = client.put("/api/cases/TM-NOT-EXIST", json={"name": "x"})

        assert response.status_code == 500, (
            f"含'不存在'字样的数据库异常应走500兜底，不得被误判为404，"
            f"实际 {response.status_code}"
        )

    def test_typed_not_found_is_translated_to_404(
        self, db_path_env: Path
    ) -> None:
        """类型化的 CaseNotFoundError 仍正确转 404（正向不回归）"""
        client = create_app("test").test_client()
        with patch.object(
            CaseManager,
            "update_case",
            side_effect=CaseNotFoundError("用例不存在"),
        ):
            response = client.put("/api/cases/TM-GONE", json={"name": "x"})

        assert response.status_code == 404, "类型化异常应转404"

    def test_typed_conflict_is_translated_to_409(
        self, db_path_env: Path
    ) -> None:
        """类型化的 CaseConflictError 仍正确转 409（正向不回归）"""
        client = create_app("test").test_client()
        with patch.object(
            CaseManager,
            "create_case",
            side_effect=CaseConflictError("用例编号已存在"),
        ):
            response = client.post(
                "/api/cases/", json={"case_id": "TM-DUP", "name": "x"}
            )

        assert response.status_code == 409, "类型化冲突异常应转409"

    def test_legacy_generic_error_no_longer_matches_substring(
        self, db_path_env: Path
    ) -> None:
        """
        泛型 CaseManagerError 不再走子串分流，一律 500

        回归点: 本次改造的核心价值就在于去掉文案依赖。核心层 6 个与路由
        相关的抛出点已全部类型化，泛型 CaseManagerError 一定是真实故障
        （数据库异常/参数非法），必须原样上抛走500。此前靠子串兜底会把
        含"已存在"/"不存在"字样的数据库错误误转成409/404。
        """
        client = create_app("test").test_client()
        with patch.object(
            CaseManager,
            "create_case",
            side_effect=CaseManagerError("数据库异常: 表已存在 (table exists)"),
        ):
            response = client.post(
                "/api/cases/", json={"case_id": "TM-DUP2", "name": "x"}
            )

        assert response.status_code == 500, (
            f"泛型异常不得被子串误判为409，实际 {response.status_code}"
        )

    def test_typed_exceptions_are_backward_compatible(self) -> None:
        """
        新异常类型必须仍被 `except CaseManagerError` 捕获

        这是整个改造的安全底线：子类化而非替换，既有捕获点行为不变。
        """
        for exc_type in (CaseNotFoundError, CaseConflictError):
            instance = exc_type("msg", {"operation": "x"})
            assert isinstance(instance, CaseManagerError), (
                f"{exc_type.__name__} 必须继承 CaseManagerError，"
                "否则既有 except CaseManagerError 捕获点会漏接"
            )
            assert instance.context == {"operation": "x"}, "context 应透传"


# ===========================================================================
# D组: P2-23 并发唯一约束冲突
# ===========================================================================
class TestCreateCaseConflict:
    """P2-23: 并发建用例的唯一约束冲突转 409 而非 500"""

    def test_concurrent_duplicate_case_raises_typed_conflict(
        self, db_path_env: Path
    ) -> None:
        """
        并发同编号创建：后到者被转为 CaseConflictError 而非裸 500

        回归点: create_case 是 check-then-act（先SELECT查重再INSERT），
        并发下双方查重都未命中，由DB唯一约束在flush阶段拦截。修复前走
        通用 SQLAlchemyError 分支 → 500，且 str(exc) 含完整 INSERT 语句与
        全部列值（进日志、进 TESTING 响应体）。

        复现手法: 真实起两个线程，同时调用 create_case 写入同一编号。
        第一个线程成功，第二个线程必然撞唯一约束。
        """
        payload = {
            "case_id": "TM-RACE-0001",
            "name": "并发用例",
            "module": "用户中心",
            "priority": "P1",
        }

        errors: list = []
        successes: list = []
        start_barrier = threading.Barrier(2)

        def _worker() -> None:
            """并发创建同一编号，记录成功与异常"""
            try:
                start_barrier.wait(timeout=5)
                successes.append(CaseManager.create_case(dict(payload)))
            except CaseManagerError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        # 核心断言: 所有异常都必须是类型化的 CaseConflictError
        assert errors, "并发同编号创建必须至少有一个撞唯一约束"
        for exc in errors:
            assert isinstance(exc, CaseConflictError), (
                f"唯一约束冲突必须被转为 CaseConflictError，"
                f"实际 {type(exc).__name__}: {exc}"
            )
            # 关键: 冲突异常不得携带 INSERT 语句与绑定参数
            assert "INSERT INTO" not in str(exc), (
                "冲突异常消息不得包含完整 INSERT 语句（避免结构与数据泄露）"
            )

    def test_integrity_error_preserves_cause_chain(
        self, db_path_env: Path
    ) -> None:
        """
        IntegrityError 路径必须保留异常链（from exc）

        回归点: 并发下冲突有两个来源——SELECT 预检命中（无底层异常，不
        该有 __cause__）与唯一约束拦截（有 __cause__）。本用例把预检
        强制为"未命中"，确定性地走 IntegrityError 分支并校验异常链。

        同时验证冲突消息不含 INSERT 语句——修复前 str(exc) 会把完整
        INSERT 与全部列值带出去（进日志、进 TESTING 响应体）。
        """
        from sqlalchemy.orm import Query

        # 先建一条基线记录，使后续 INSERT 必然撞唯一约束
        CaseManager.create_case(
            {
                "case_id": "TM-BASE-0001",
                "name": "基线冲突用例",
                "module": "用户中心",
                "priority": "P1",
            }
        )

        # 只让"预检查重.first()"返回空，确定性地制造 check-then-act 窗口；
        # 不用 patch Session.query——那会把 flush 路径一起打断，INSERT 根本不发生
        def _precheck_miss(self, *args, **kwargs):
            """预检查重恒定返回空，制造并发窗口"""
            return None

        with patch.object(Query, "first", _precheck_miss):
            with pytest.raises(CaseConflictError) as exc_info:
                CaseManager.create_case(
                    {
                        "case_id": "TM-BASE-0001",
                        "name": "重复用例",
                        "module": "用户中心",
                        "priority": "P1",
                    }
                )

        cause = exc_info.value.__cause__
        assert cause is not None, "IntegrityError 分支必须保留异常链（from exc）"
        assert "UNIQUE constraint" in str(cause), (
            f"异常链应指向底层唯一约束错误，实际 {cause!r}"
        )
        assert "INSERT INTO" not in str(exc_info.value), (
            "对外异常消息不得含完整 INSERT 语句"
        )

    def test_duplicate_case_via_api_returns_409(
        self, db_path_env: Path
    ) -> None:
        """重复创建走真实 API 应返回 409（正向不回归）"""
        client = create_app("test").test_client()
        payload = {"case_id": "TM-DUP-0001", "name": "重复用例"}

        first = client.post("/api/cases/", json=payload)
        assert first.status_code == 201, f"首次创建应201，实际 {first.status_code}"

        second = client.post("/api/cases/", json=payload)
        assert second.status_code == 409, (
            f"重复创建应409，实际 {second.status_code}"
        )
        assert "已存在" in second.get_json()["message"]


# ===========================================================================
# E组: P2-12 get_bool 契约
# ===========================================================================
class TestGetBoolContract:
    """P2-12: get_bool 对无法识别的值返回 default 而非 False"""

    def test_get_bool_unrecognized_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        拼写错误的配置值必须回落到 default

        回归点: 修复前对任何无法识别的值一律返回 False。调用方传
        default=True 时，拼错的配置（如 TM_TASK_WORKER_ENABLED=enabled）
        会静默变成"关闭"，把配置错误伪装成显式关闭，比直接报错更难排查。
        """
        monkeypatch.setenv("TM_P2_TEST_FLAG", "enabled")
        assert env_manager.get_bool("TM_P2_TEST_FLAG", True) is True, (
            "无法识别的值应返回 default=True，而非静默 False"
        )
        assert env_manager.get_bool("TM_P2_TEST_FLAG", False) is False, (
            "default=False 时同样返回 False"
        )

    def test_get_bool_explicit_values_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """明确的真/假取值行为不变（不回归）"""
        cases = [
            ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
            ("false", False), ("FALSE", False), ("0", False), ("no", False),
            ("off", False),
        ]
        for raw, expected in cases:
            monkeypatch.setenv("TM_P2_TEST_FLAG", raw)
            assert env_manager.get_bool("TM_P2_TEST_FLAG") is expected, (
                f"{raw!r} 应解析为 {expected}"
            )

    def test_get_bool_absent_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """键不存在时返回 default（不回归）"""
        monkeypatch.delenv("TM_P2_ABSENT", raising=False)
        assert env_manager.get_bool("TM_P2_ABSENT", True) is True
        assert env_manager.get_bool("TM_P2_ABSENT", False) is False


# ===========================================================================
# F1组: P2-14 结果目录容错
# ===========================================================================
class TestReportAnalyzerOsError:
    """P2-14: 单个结果文件被删除不得让整批统计丢失"""

    def test_parse_results_dir_tolerates_deleted_file(
        self, tmp_path: Path
    ) -> None:
        """
        扫描后被删除的结果文件应被跳过而非中断整批

        回归点: except 元组不含 OSError，而 parse_result_file 对已消失
        的文件抛 FileNotFoundError（OSError 子类）→ 穿透 except 令
        整批统计全部返回空，违背"绝不因单文件脏数据中断整批"的模块契约。
        """
        results_dir = tmp_path / "allure-results"
        results_dir.mkdir()

        (results_dir / "good-result.json").write_text(
            json.dumps({"name": "test_ok", "status": "passed", "statusDetails": {}}),
            encoding="utf-8",
        )
        (results_dir / "vanished-result.json").write_text("{}", encoding="utf-8")

        # 先取出原始实现再打补丁——side_effect 内若回调被 patch 后的
        # 自身会无限递归（RecursionError）
        original_parse = ReportAnalyzer.parse_result_file

        def _parse_with_race(file_path):
            """解析前删掉该文件，再走原始实现触发 FileNotFoundError"""
            Path(file_path).unlink(missing_ok=True)
            return original_parse(file_path)

        with patch.object(
            ReportAnalyzer, "parse_result_file", side_effect=_parse_with_race
        ):
            results = ReportAnalyzer.parse_results_dir(results_dir)

        assert isinstance(results, list), "应返回结果列表而非抛异常"
        assert results == [], "全部文件被删时结果应为空列表而非抛异常"

    def test_parse_results_dir_returns_partial_results(self) -> None:
        """部分文件损坏时其余仍应正常解析（不回归）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            results_dir = Path(tmpdir)
            # scan_results_dir 只匹配 *-result.json（自动排除 container 文件）
            (results_dir / "a-result.json").write_text(
                json.dumps(
                    {"name": "test_a", "status": "passed", "statusDetails": {}}
                ),
                encoding="utf-8",
            )
            (results_dir / "broken-result.json").write_text(
                "{不是JSON", encoding="utf-8"
            )

            results = ReportAnalyzer.parse_results_dir(results_dir)

            assert len(results) == 1, f"应保留1条有效结果，实际 {len(results)}"
            assert results[0].name == "test_a", (
                f"应保留的是有效结果 a，实际 {results[0].name}"
            )


# ===========================================================================
# F2组: P2-15 数值归一化
# ===========================================================================
class TestDataDriverNumericNormalization:
    """P2-15: Excel 数值型单元格应归一为字符串而非判缺失"""

    @staticmethod
    def _validate(case: dict) -> dict:
        """
        调用内部校验方法（签名: case, location, file_path）

        参数:
            case (dict): 待校验用例

        返回:
            dict: 归一化后的用例
        """
        return DataDriver._validate_case(
            case, location=2, file_path=Path("test.yaml")
        )

    def test_numeric_case_id_is_normalized(self) -> None:
        """
        case_id=1001（Excel 存为 int）应通过校验并归一为字符串

        回归点: 修复前硬性要求 str，Excel 手工录入的纯数字被自动存为
        数值类型，openpyxl 原样返回 int，报"必填字段缺失"——而单元格
        明明非空，错误信息把排障方向指反了。
        """
        validated = self._validate(
            {
                "case_id": 1001,
                "name": "数值编号用例",
                "module": "用户中心",
                "priority": "P1",
                "tags": "smoke,regression",
            }
        )
        assert validated["case_id"] == "1001", (
            f"数值 case_id 应归一为字符串 '1001'，实际 {validated['case_id']!r}"
        )
        assert validated["tags"] == ["smoke", "regression"], "逗号串应拆分为列表"

    def test_boolean_and_float_normalized(self) -> None:
        """bool 与整值 float 也要正确归一（不回归）"""
        validated = self._validate(
            {
                "case_id": 1.0,
                "name": "布尔名",
                "module": True,
                "priority": "P0",
                "tags": [1, 2],
            }
        )
        assert validated["case_id"] == "1", (
            f"整值浮点应归一为'1'，实际 {validated['case_id']!r}"
        )
        assert validated["module"] == "True", (
            f"bool 应归一为'True'，实际 {validated['module']!r}"
        )
        assert validated["tags"] == ["1", "2"], "数字标签列表应归一为字符串列表"

    def test_missing_required_field_still_raises(self) -> None:
        """真正缺失的必填字段仍必须报错（不回归）"""
        with pytest.raises(DataDriverError, match="case_id"):
            self._validate({"case_id": None, "name": "x", "module": "y"})

    def test_numeric_case_id_via_yaml_works(self) -> None:
        """YAML 中的数字编号同样应归一（不回归）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_file = Path(tmpdir) / "numeric.yaml"
            data_file.write_text(
                yaml.dump(
                    [
                        {
                            "case_id": 2002,
                            "name": "YAML数值编号",
                            "module": "用户中心",
                            "priority": "P2",
                            "tags": ["smoke"],
                        }
                    ],
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            cases = DataDriver.load_cases(data_file)

            assert len(cases) == 1
            assert cases[0]["case_id"] == "2002", (
                f"YAML 数字编号应归一为字符串，实际 {cases[0]['case_id']!r}"
            )


# ===========================================================================
# F3组: P2-17 filter_cases 容错
# ===========================================================================
class TestFilterCasesNoneTags:
    """P2-17: tags 为 None 时不得抛 TypeError"""

    def test_filter_cases_with_none_tags_does_not_raise(self) -> None:
        """
        用例 tags 为 None 时应正常跳过该用例而非整条筛选崩溃

        回归点: 修复前 set(case.get("tags", [])) 在 tags 显式为 None 时
        （.get 的默认值不生效）抛 TypeError: 'NoneType' object is not
        iterable，中断整条筛选。
        """
        cases = [
            {"case_id": "A", "module": "M", "priority": "P0", "tags": None},
            {"case_id": "B", "module": "M", "priority": "P0", "tags": ["smoke"]},
        ]
        matched = DataDriver.filter_cases(cases, tags=["smoke"])

        assert len(matched) == 1, f"应只命中带 smoke 标签的1条，实际 {len(matched)}"
        assert matched[0]["case_id"] == "B"

    def test_filter_cases_normal_flow_unchanged(self) -> None:
        """常规多维筛选行为不变（不回归）"""
        cases = [
            {"case_id": "A", "module": "用户", "priority": "P0", "tags": ["smoke"]},
            {"case_id": "B", "module": "订单", "priority": "P1", "tags": ["regression"]},
            {"case_id": "C", "module": "用户", "priority": "P2", "tags": ["smoke"]},
        ]
        matched = DataDriver.filter_cases(
            cases, module="用户", priority=["P0", "P2"], tags="smoke"
        )

        assert [c["case_id"] for c in matched] == ["A", "C"], (
            f"应命中A与C，实际 {[c['case_id'] for c in matched]}"
        )


# ===========================================================================
# F4组: P2-28 通知渠道缺模板
# ===========================================================================
class TestRouterUnknownChannel:
    """P2-28: 渠道缺少消息模板时显式判失败并告警"""

    def test_router_skips_channel_without_message_template(self) -> None:
        """
        自定义渠道无消息模板时应记 error + results[channel]=False

        回归点: 修复前 notifications[channel] 直接抛 KeyError，被下面的
        except Exception 吞掉只留一行 warning——该渠道永久静默不发送、
        也不写死信，排查时看不出任何痕迹。文档明确把"继承 BaseNotifier
        新增渠道"作为扩展点，这个扩展点实际是坏的。
        """
        send_calls: list = []

        class DingTalkNotifier(BaseNotifier):
            """测试替身：扩展渠道，但未在 _build_channel_notifications 补模板"""

            channel_name = "dingtalk"

            def send(self, notification: Notification) -> bool:
                """不应被调用（无模板时直接跳过）"""
                send_calls.append(notification)
                return True

        router = NotificationRouter()
        router.notifiers = [DingTalkNotifier()]
        router.strategy = "all"

        results = router.notify(_make_stat(), "RUN-P2-0001")

        assert results["dingtalk"] is False, (
            f"无模板渠道应显式判失败，实际 {results}"
        )
        assert send_calls == [], "无模板时不应真正调用该渠道的 send()"


# ===========================================================================
# 测试辅助
# ===========================================================================
def _make_stat() -> StatisticsResult:
    """
    构造最小的批次统计结果（通知路由入参）

    返回:
        StatisticsResult: 批次级统计结果
    """
    return StatisticsResult(
        total=4,
        passed=3,
        failed=1,
        broken=0,
        skipped=0,
        pass_rate=0.75,
        total_duration_ms=1000,
        avg_duration_ms=15.0,
        p95_duration_ms=50.0,
        min_duration_ms=5,
        max_duration_ms=200,
        by_module={},
        by_priority={},
        failed_details=[],
    )
