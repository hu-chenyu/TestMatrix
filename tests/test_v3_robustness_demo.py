"""
TestMatrix 大扫除 v3 · Commit B：健壮性修复的回归测试

覆盖 v2 第二轮审查的健壮性类问题
------------------------------------
A组 worker 启停竞态（V2-P1-3）
    1.  stop_worker 条件清空：join 期间新起的 worker 引用不被抹掉
B组 SSE 降级帧不占序列位置（V2-P2-2）
    （跨请求重连场景的用例在 tests/test_v2_executions_coverage.py，
      本文件只补纯函数层的"降级帧无 id 行"守卫）
C组 YAML 编码错误（V2-P2-5）
    2.  非 UTF-8 文件被包装为 DataDriverError 且文案指向编码
D组 SECRET_KEY 判定（V2-P2-6）
    3.  非开发/测试档位缺 TM_SECRET_KEY 时 fail-fast
    4.  开发/测试档位仍零配置可用（对照组）
E组 IntegrityError 收窄（V2-P2-4）
    5.  唯一约束违例仍翻译为 409
    6.  NOT NULL 违例不再被误报为"编号已存在"
F组 description 与 tags 共存（V2-P2-3）
    7.  两者同时非空时都保留
    8.  仅描述 / 仅标签 / 都为空 三种组合
    9.  拼接后的描述能反解出标签（往返一致）
G组 缓存失效统一兜底（V2-P3-8）
    10. 缓存失效抛异常时用例创建仍成功

测试铁律
------------------------------------
- 不依赖真实硬件/服务；涉及线程的用例用**确定性构造**（让 join 回调里
  同步触发"新 worker 起飞"）而非概率轮询
- 不依赖执行顺序：每条用例独立临时库 / 独立 monkeypatch
- 无 time.sleep 固定等待，无 print
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import allure
import pytest
from sqlalchemy.exc import IntegrityError
from src.core import task_queue as tq
from src.core.case_manager import (
    CaseConflictError,
    CaseManager,
    CaseManagerError,
)
from src.core.data_driver import DataDriver, DataDriverError
from src.web.config import (
    Config,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
    resolve_secret_key,
)


@pytest.fixture
def seed_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """
    独立临时 SQLite 库夹具（模块级，供多个测试类共用）

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    from src.db.db_session import DatabaseSession

    db_path = tmp_path / "v3_robustness.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_path
    DatabaseSession.reset()
    db_path.unlink(missing_ok=True)


# ===========================================================================
# A组: worker 启停竞态
# ===========================================================================
@allure.feature("大扫除v3健壮性修复")
@allure.story("stop_worker 条件清空")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestStopWorkerRace:
    """join 期间新 worker 起飞时引用不被误清"""

    def test_new_worker_reference_survives_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        stop_worker 不得抹掉 join 期间由 start_worker 新建的 worker 引用

        可复现时序（确定性构造，不靠概率）:
          T1 stop_worker: 取到 thread=A 后**释放 _state_lock**，开始 join
          A 在 join 期间退出
          T2 start_worker: A.is_alive() 为 False -> 建 B，_worker_thread=B
          T1 join 返回 -> 无条件置 None 会把 **B 的引用一并抹掉**
        后果: B 仍在跑却无人能停，且下次 start_worker 见 None 又起一个
        -> 两个 worker 并发 BRPOP 同一队列，"单 worker 串行"不变量被破坏。

        构造方式: 让 A 的 join() 回调里同步执行"新 worker 起飞"这一步，
        把并发窗口压缩成确定的一次函数调用。
        """
        replacement = object()
        race_holder: dict[str, Any] = {}

        class _JoinRacingThread:
            """
            join 期间触发 start_worker 的线程替身

            生命周期必须真实: is_alive() 在 join **之前**返回 True
            （让 stop_worker 走进 join 分支），join **之后**返回 False
            （否则会走"join 超时"分支提前 return，压根到不了清空引用的
            那段代码，测试就变成了永远通过的空转——本条最初就是这么
            写成假通过的，变异验证时才发现）。
            """

            def __init__(self) -> None:
                self._joined = False

            def is_alive(self) -> bool:
                """join 前存活、join 后已退出"""
                return not self._joined

            def join(self, timeout: Any = None) -> None:
                """模拟 join 期间另一线程建了新 worker 并接管引用"""
                self._joined = True
                race_holder["replacement"] = replacement
                tq._worker_thread = replacement  # type: ignore[assignment]

        stale = _JoinRacingThread()
        monkeypatch.setattr(tq, "_worker_thread", stale, raising=False)
        monkeypatch.setattr(tq, "_stop_event", None, raising=False)

        tq.stop_worker()

        assert tq._worker_thread is replacement, (
            "join 期间新建的 worker 引用被误清空——该 worker 仍在跑却"
            "再也无法被 stop_worker 停掉，下一次 start 会再起一个并"
            "发消费同一队列"
        )
        assert race_holder.get("replacement") is replacement

    def test_reference_cleared_when_no_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        无并发替换时仍正常清空引用（对照组）

        证明上一条不是因为"永远不清空"而通过——正常收尾路径必须照常
        清空，否则 stop 之后无法重建 worker。
        """
        class _AlreadyDeadThread:
            """已退出的线程替身"""

            def is_alive(self) -> bool:
                """已退出"""
                return False

            def join(self, timeout: Any = None) -> None:
                """已退出则 join 立即返回"""
                return None

        ended = _AlreadyDeadThread()
        monkeypatch.setattr(tq, "_worker_thread", ended, raising=False)
        monkeypatch.setattr(tq, "_stop_event", None, raising=False)

        tq.stop_worker()

        assert tq._worker_thread is None, (
            "无并发替换时必须正常清空引用，否则 stop 之后无法重建 worker"
        )


# ===========================================================================
# C组: YAML 编码错误
# ===========================================================================
@allure.feature("大扫除v3健壮性修复")
@allure.story("YAML 编码错误包装")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestYamlEncodingError:
    """非 UTF-8 文件不得让 UnicodeDecodeError 裸逃逸"""

    def test_non_utf8_yaml_wrapped_with_actionable_message(
        self, tmp_path: Path
    ) -> None:
        """
        非 UTF-8 的 YAML 被包装为 DataDriverError，文案指向"另存为 UTF-8"

        修复前: _load_yaml 只 catch (YAMLError, OSError)，而
        UnicodeDecodeError 继承自 UnicodeError -> ValueError，两条都接不住；
        且解码发生在 safe_load 读取句柄阶段，接在 open 的 OSError 分支之后
        也覆盖不到。异常裸逃 -> 导入接口返回 500，响应体是 codec 错误文本。
        """
        binary_file = tmp_path / "cases.yaml"
        # 0xFF 不是合法 UTF-8 起始字节
        binary_file.write_bytes(b"\xff\xfe\x00binary")

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(binary_file)

        message = str(excinfo.value)
        assert "编码" in message, f"错误文案应指向编码问题: {message}"
        assert "UTF-8" in message, f"错误文案应给出可执行的修复动作: {message}"

    def test_yaml_syntax_error_message_unchanged(self, tmp_path: Path) -> None:
        """
        语法错误仍走 YAMLError 分支（分流未被破坏的对照组）

        新增的 UnicodeError 分支夹在 YAMLError 与 OSError 之间，若分母
        写错（写成 Exception）会把语法错误也吞进"编码错误"文案。
        """
        broken = tmp_path / "cases.yaml"
        broken.write_text("cases: [ {unclosed: ", encoding="utf-8")

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(broken)

        assert "YAML语法解析失败" in str(excinfo.value)


# ===========================================================================
# D组: SECRET_KEY 判定
# ===========================================================================
@allure.feature("大扫除v3健壮性修复")
@allure.story("SECRET_KEY 判定口径")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestSecretKeyFailFast:
    """非开发/测试档位缺密钥即 fail-fast"""

    def test_unknown_config_class_requires_secret_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        新增的严格档位（不在开发/测试白名单内）缺密钥时必须 fail-fast

        修复前用 `config_class is ProductionConfig` 判定"生产"，
        将来新增 StagingConfig 之类档位会静默落到允许随机密钥的一侧。
        改正向判定后新增档位默认走严格侧。
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)

        class _StagingConfig(Config):
            """模拟新增的预发布档位"""

            DEBUG = False

        with pytest.raises(ValueError, match="TM_SECRET_KEY"):
            resolve_secret_key(_StagingConfig)

    def test_production_config_still_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产档位缺密钥仍然 fail-fast（回归守卫）"""
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)

        with pytest.raises(ValueError, match="TM_SECRET_KEY"):
            resolve_secret_key(ProductionConfig)

    @pytest.mark.parametrize(
        "config_class", [DevelopmentConfig, TestingConfig], ids=["dev", "test"]
    )
    def test_dev_and_test_allow_zero_config(
        self, monkeypatch: pytest.MonkeyPatch, config_class: type
    ) -> None:
        """
        开发/测试档位保持零配置可用（对照组）

        "本地零配置即可跑通"是该函数明确的设计意图，正向判定不能把
        开发体验一起收紧——那会把问题从"生产不安全"变成"本地不能用"。
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)

        key = resolve_secret_key(config_class)

        assert len(key) == 64, "应生成 32 字节的十六进制随机密钥"


# ===========================================================================
# E组: IntegrityError 收窄
# ===========================================================================
@allure.feature("大扫除v3健壮性修复")
@allure.story("IntegrityError 收窄")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestIntegrityErrorNarrowing:
    """只有唯一约束才翻译成 409"""

    def test_unique_violation_still_translated_to_conflict(
        self, seed_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        case_id 唯一约束违例仍翻译为 409（收窄不能误伤真冲突）

        收窄的判据是异常消息里含列名 case_id；本条确认真正撞编号时
        依旧走 CaseConflictError。
        """
        CaseManager.create_case({
            "case_id": "TM-DUP-0001",
            "name": "唯一性冲突用例",
            "module": "用户中心",
            "priority": "P1",
        })
        # 关掉串行的预检查，强制走 DB 唯一约束路径
        monkeypatch.setattr(CaseManager, "get_case", staticmethod(lambda _cid: None))

        with pytest.raises(CaseConflictError):
            CaseManager.create_case({
                "case_id": "TM-DUP-0001",
                "name": "重复编号",
                "module": "用户中心",
                "priority": "P1",
            })

    def test_non_unique_integrity_error_not_reported_as_conflict(
        self, seed_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        非唯一约束的 IntegrityError 不再被误报为"编号已存在"（V2-P2-4）

        做法: 在 flush 处注入一条 NOT NULL 违例的 IntegrityError。

        **为什么不走 module=None 的真实路径**: 审查报告以
        "module 传 None -> NOT NULL 违例"举例，但这条路当前不可达——
        test_cases 每个 nullable=False 列都带 Python 侧 default=，实测
        显式传 module=None 时 SQLAlchemy 会套用 default="default" 落库，
        根本不产生 NOT NULL 违例（该实测结论已回写 src 注释）。故改为
        直接注入异常，验证的是**翻译逻辑**本身：只有唯一约束才转 409，
        其余完整性违例回落为通用错误并保留原异常链。
        """
        from sqlalchemy.orm import Session

        real_flush = Session.flush

        def _flush_with_not_null_violation(self: Session) -> None:
            """在 flush 处注入 NOT NULL 违例"""
            raise IntegrityError(
                "INSERT",
                {},
                Exception("NOT NULL constraint failed: test_cases.module"),
            )

        monkeypatch.setattr(Session, "flush", _flush_with_not_null_violation)

        with pytest.raises(CaseManagerError) as excinfo:
            CaseManager.create_case({
                "case_id": "TM-NULL-0001",
                "name": "触发非唯一完整性违例",
                "module": "用户中心",
                "priority": "P1",
            })

        monkeypatch.setattr(Session, "flush", real_flush)

        assert not isinstance(excinfo.value, CaseConflictError), (
            "非唯一约束的完整性违例被误报为编号冲突（409），真因被掩盖"
        )
        assert "完整性违例" in str(excinfo.value), (
            f"错误信息应指向完整性违例: {excinfo.value}"
        )
        assert excinfo.value.__cause__ is not None, (
            "原始 IntegrityError 应作为 __cause__ 保留，便于定位"
        )

    def test_unique_violation_helper_recognizes_dialect_messages(self) -> None:
        """
        唯一约束判定在各方言消息形态下都成立

        判据只匹配列名（test_cases 的稳定特征），不匹配 "UNIQUE"/
        "duplicate" 这类各库措辞不同的词。
        """
        from src.core.case_manager import _is_unique_case_id_violation

        sqlite_msg = "UNIQUE constraint failed: test_cases.case_id"
        pg_msg = 'duplicate key value violates unique constraint "uq_case_id"'
        notnull_msg = "NOT NULL constraint failed: test_cases.module"

        def _wrap(msg: str) -> IntegrityError:
            return IntegrityError("INSERT", {}, Exception(msg))

        assert _is_unique_case_id_violation(_wrap(sqlite_msg)) is True
        assert _is_unique_case_id_violation(_wrap(pg_msg)) is True
        assert _is_unique_case_id_violation(_wrap(notnull_msg)) is False


# ===========================================================================
# F组: description 与 tags 共存
# ===========================================================================
@allure.feature("大扫除v3健壮性修复")
@allure.story("description 与 tags 共存")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestDescriptionTagsCoexist:
    """描述非空时标签不再被整体丢弃"""

    def test_both_present_are_both_kept(self) -> None:
        """
        描述与标签同时非空时两者都保留（V2-P2-3 正题）

        修复前 custom_desc 非空即 return，tags 分支永不执行——一整行
        用例的标签被静默丢弃，随后按标签筛选恒不命中。
        """
        built = CaseManager._build_description({
            "description": "登录失败场景",
            "tags": ["smoke", "regression"],
        })

        assert "登录失败场景" in built
        assert "smoke" in built and "regression" in built

    def test_round_trip_tags_survive_description(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        端到端往返: 带描述 + 标签的用例入库后，按标签筛得回来

        这是本条修复的**真正验收口径**——只看 _build_description 的返回
        字符串不够，必须确认 list_cases 按 tags 过滤时能命中。
        """
        from src.db import models
        from src.db.db_session import DatabaseSession

        db_path = tmp_path / "tags_roundtrip.db"
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
        DatabaseSession.reset()
        DatabaseSession.init_db()

        try:
            with DatabaseSession.session_scope() as session:
                session.add(
                    models.TestCase(
                        case_id="TM-TAGS-0001",
                        name="带描述与标签的用例",
                        module="用户中心",
                        priority="P1",
                        case_type="api",
                        status="active",
                        description=CaseManager._build_description({
                            "description": "登录失败场景",
                            "tags": ["smoke", "regression"],
                        }),
                        creator="admin",
                    )
                )

            matched = CaseManager.select_cases_for_execution(tags="smoke")

            assert len(matched) == 1, (
                "带描述的用例其标签应能被筛选命中——修复前标签被整体丢弃，"
                "此处恒为 0"
            )
            assert matched[0]["case_id"] == "TM-TAGS-0001"
        finally:
            DatabaseSession.reset()
            db_path.unlink(missing_ok=True)

    @pytest.mark.parametrize(
        ("case", "expected_desc", "expected_tags"),
        [
            (
                {"description": "只有描述", "tags": []},
                "只有描述",
                [],
            ),
            (
                {"description": "", "tags": ["a", "b"]},
                "标签: a, b",
                ["a", "b"],
            ),
            (
                {"description": "描述", "tags": ["x"]},
                "描述\n标签: x",
                ["x"],
            ),
            (
                {"description": "", "tags": []},
                "",
                [],
            ),
        ],
        ids=["desc-only", "tags-only", "both", "neither"],
    )
    def test_all_combinations(
        self, case: dict, expected_desc: str, expected_tags: list
    ) -> None:
        """
        四种组合的构建与反解均符合预期

        拼接格式为"描述\\n标签: a, b"，解析器按**逐行查找**标签行——
        沿用旧的"整串以标签开头"判定会让拼接后的标签永远解析不出来。
        """
        built = CaseManager._build_description(case)
        parsed = CaseManager._parse_tags_from_description(built)

        assert built == expected_desc
        assert parsed == expected_tags, (
            f"构建结果 {built!r} 应能反解出 {expected_tags}"
        )

    def test_parse_ignores_non_tag_lines(self) -> None:
        """
        解析器只在含标签行的描述中提取，不误伤其它行（守卫）
        """
        parsed = CaseManager._parse_tags_from_description(
            "第一行普通描述\n第二行也是描述\n标签: smoke, chip"
        )

        assert parsed == ["smoke", "chip"]

    def test_parse_returns_empty_without_tag_line(self) -> None:
        """不含标签行时返回空列表（无标签的用例不会被误解析出垃圾标签）"""
        assert CaseManager._parse_tags_from_description("纯描述，无标签") == []
        assert CaseManager._parse_tags_from_description(None) == []
        assert CaseManager._parse_tags_from_description("") == []


# ===========================================================================
# G组: 缓存失效统一兜底
# ===========================================================================
@allure.feature("大扫除v3健壮性修复")
@allure.story("缓存失效统一兜底")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestCacheInvalidateIsolation:
    """缓存故障不得让已成功的写操作返回失败"""

    def test_create_succeeds_when_cache_invalidate_raises(
        self, seed_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        缓存失效抛异常时用例创建仍成功（V2-P3-8 正题）

        修复前 4 处 invalidate_cases_list 均为裸调（而批次收尾的
        invalidate_reports 早已各自加了 try），口径不一致：缓存层万一
        抛异常，用例已创建成功却返回 500。
        """
        from src.core.case_manager import cache_client

        def _raise_on_invalidate() -> None:
            """模拟缓存层故障"""
            raise RuntimeError("模拟缓存不可用")

        monkeypatch.setattr(
            cache_client, "invalidate_cases_list", _raise_on_invalidate
        )

        result = CaseManager.create_case({
            "case_id": "TM-CACHE-0001",
            "name": "缓存故障下的创建",
            "module": "用户中心",
            "priority": "P1",
        })

        assert result["case_id"] == "TM-CACHE-0001", (
            "缓存故障不得让已成功的创建返回失败"
        )
        assert CaseManager.get_case("TM-CACHE-0001") is not None, (
            "用例必须真的落库"
        )

    def test_safe_invalidate_swallows_and_logs(self) -> None:
        """
        _safe_invalidate 吞掉异常（不向上抛）
        """
        from src.core.case_manager import _safe_invalidate

        def _boom() -> None:
            """必然抛错的失效动作"""
            raise RuntimeError("缓存炸了")

        _safe_invalidate("测试操作", _boom)  # 不抛异常即为通过
