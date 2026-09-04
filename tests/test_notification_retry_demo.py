"""
通知重试与死信验证用例（第二阶段Day14）

验证目标:
    1. 重试策略: 首次成功零等待 / 二次成功单次等待 / 耗尽序列精确1,2,4
    2. 退避可配置: max_retries=4序列1,2,4,8
    3. 异常口径: send抛异常同样重试且fail_reason含类型名 / 未启用不重试不死信
    4. 死信落库: 字段完整实查 / 仓储故障不外抛
    5. 仓储CRUD: 批次隔离/计数/升序
    6. 端到端: 双渠道一成一败+死信隔离+jitter区间断言

测试基建:
    FakeNotifier（可配第几次成功/抛异常/开关）+ 记录型fake_sleeper
    （零真实sleep）+ 临时SQLite（对齐Day8模式）。
"""

from unittest.mock import patch

import allure
import pytest

from src.common.env_manager import env_manager
from src.core.notification import (
    BaseNotifier,
    Notification,
    NotificationDeadLetterRepository,
    NotificationRouter,
)
from src.core.report_analyzer import StatisticsResult
from src.db.db_session import DatabaseSession

# 路由器env基线（重试参数走显式传参，不依赖env）
ROUTER_ENV = {
    "TM_NOTIFY_STRATEGY": "all",
    "TM_NOTIFY_AT_ALL": "false",
    "TM_NOTIFY_OWNER_MOBILES": "",
}


class FakeNotifier(BaseNotifier):
    """可控的fake渠道（第N次起成功/抛异常/开关/调用记录）"""

    def __init__(
        self,
        channel_name: str = "fake",
        succeed_from: int = 1,
        raise_exception: bool = False,
        enabled: bool = True,
    ):
        """
        初始化fake渠道

        参数:
            channel_name (str): 渠道名
            succeed_from (int): 第几次调用起返回成功（1=首次即成功）
            raise_exception (bool): True时send恒抛异常
            enabled (bool): is_enabled返回值
        """
        self.channel_name = channel_name
        self._succeed_from = succeed_from
        self._raise = raise_exception
        self._enabled = enabled
        self.send_count = 0

    def is_enabled(self) -> bool:
        return self._enabled

    def send(self, notification):
        self.send_count += 1
        if self._raise:
            raise ConnectionError("模拟渠道连接失败")
        return self.send_count >= self._succeed_from


class FakeSleeper:
    """记录型等待函数（收集每次delay，不真实等待）"""

    def __init__(self):
        self.delays: list = []

    def __call__(self, seconds: float):
        self.delays.append(seconds)


class BrokenRepo:
    """恒抛异常的坏仓储（验证死信落库故障不外抛）"""

    def save_dead_letter(self, **kwargs):
        raise RuntimeError("数据库不可用")


def _mock_get(config: dict):
    """env_manager.get替身"""
    def _get(key, default=None):
        value = config.get(key)
        return value if value is not None else default
    return _get


def _mock_get_bool(config: dict):
    """env_manager.get_bool替身"""
    def _get_bool(key, default=False):
        value = config.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return _get_bool


def patch_env(config: dict):
    """双方法mock上下文（构造期+发送期全程生效）"""
    return (
        patch.object(env_manager, "get", side_effect=_mock_get(config)),
        patch.object(env_manager, "get_bool", side_effect=_mock_get_bool(config)),
    )


def make_stat(**overrides) -> StatisticsResult:
    """构造全通过统计（默认10条100%）"""
    defaults = {
        "total": 10, "passed": 10, "failed": 0, "broken": 0,
        "skipped": 0, "pass_rate": 1.0,
        "total_duration_ms": 1000, "avg_duration_ms": 100.0,
        "p95_duration_ms": 150.0, "min_duration_ms": 50, "max_duration_ms": 200,
    }
    defaults.update(overrides)
    return StatisticsResult(**defaults)


def make_router(notifiers, repo=None, **retry_kwargs) -> NotificationRouter:
    """
    构造带fake sleeper的router（显式重试参数+fake仓储）

    参数:
        notifiers (list): 渠道列表
        repo: 死信仓储（None用真实仓储）
        **retry_kwargs: max_retries/base_delay/use_jitter

    返回:
        tuple: (router, sleeper)
    """
    sleeper = FakeSleeper()
    get_patch, bool_patch = patch_env(ROUTER_ENV)
    with get_patch, bool_patch:
        router = NotificationRouter(
            notifiers=notifiers,
            sleeper=sleeper,
            dead_letter_repo=repo,
            **retry_kwargs,
        )
    return router, sleeper


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（对齐Day8模式）

    参数:
        tmp_path (Path): 临时目录
        monkeypatch: 环境变量覆写

    返回:
        Path: 临时库文件路径
    """
    db_file = tmp_path / "test_dead_letter.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    # Windows下先释放连接再删库文件
    DatabaseSession.reset()
    if db_file.exists():
        db_file.unlink()


@allure.feature("通知模块")
@allure.story("重试策略")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestRetryStrategy:
    """指数退避重试策略验证"""

    def test_first_attempt_success(self, temp_db):
        """
        首次成功: send仅1次、sleeper零调用、无死信

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("email", succeed_from=1)
        router, sleeper = make_router(
            [notifier], repo=NotificationDeadLetterRepository(), max_retries=3
        )

        results = router.notify(make_stat(), "RUN-R1")

        assert results == {"email": True}
        assert notifier.send_count == 1
        assert sleeper.delays == []
        assert NotificationDeadLetterRepository.count_all() == 0

    def test_retry_then_success(self, temp_db):
        """
        失败1次后第2次成功: send 2次、等待1次且delay≈1.0、
        最终True、无死信

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("email", succeed_from=2)
        router, sleeper = make_router(
            [notifier], repo=NotificationDeadLetterRepository(), max_retries=3
        )

        results = router.notify(make_stat(), "RUN-R2")

        assert results == {"email": True}
        assert notifier.send_count == 2
        assert len(sleeper.delays) == 1
        assert sleeper.delays[0] == pytest.approx(1.0)
        assert NotificationDeadLetterRepository.count_all() == 0

    def test_retry_exhausted_default(self, temp_db):
        """
        默认耗尽: max_retries=3恒False，sleep序列精确[1,2,4]、
        send共4次、最终False、死信1条且attempts=4

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("email", succeed_from=999)  # 恒失败
        repo = NotificationDeadLetterRepository()
        router, sleeper = make_router(
            [notifier], repo=repo, max_retries=3
        )

        results = router.notify(make_stat(), "RUN-R3")

        assert results == {"email": False}
        assert notifier.send_count == 4
        assert sleeper.delays == [1.0, 2.0, 4.0]
        dead_letters = repo.list_by_execution_id("RUN-R3")
        assert len(dead_letters) == 1
        assert dead_letters[0]["attempts"] == 4
        assert dead_letters[0]["fail_reason"] == "send返回False"

    def test_backoff_configurable(self, temp_db):
        """
        退避可配置: max_retries=4时序列精确[1,2,4,8]

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("wechat", succeed_from=999)
        router, sleeper = make_router(
            [notifier], repo=NotificationDeadLetterRepository(), max_retries=4
        )

        results = router.notify(make_stat(), "RUN-R4")

        assert results == {"wechat": False}
        assert notifier.send_count == 5
        assert sleeper.delays == [1.0, 2.0, 4.0, 8.0]

    def test_send_exception_retried(self, temp_db):
        """
        异常口径: send恒抛异常同样重试，最终False不外抛，
        fail_reason含异常类型名

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("email", raise_exception=True)
        repo = NotificationDeadLetterRepository()
        router, sleeper = make_router(
            [notifier], repo=repo, max_retries=2
        )

        # 不抛异常即通过（异常被内部消化）
        results = router.notify(make_stat(), "RUN-R5")

        assert results == {"email": False}
        assert notifier.send_count == 3
        dead_letters = repo.list_by_execution_id("RUN-R5")
        assert "ConnectionError" in dead_letters[0]["fail_reason"]

    def test_disabled_channel_no_retry_no_dead_letter(self, temp_db):
        """
        未启用渠道: send零调用、sleeper零调用、直接False、
        不写死信（配置性跳过不是发送失败）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("email", enabled=False, succeed_from=999)
        repo = NotificationDeadLetterRepository()
        router, sleeper = make_router(
            [notifier], repo=repo, max_retries=3
        )

        results = router.notify(make_stat(), "RUN-R6")

        assert results == {"email": False}
        assert notifier.send_count == 0
        assert sleeper.delays == []
        assert repo.count_all() == 0


@allure.feature("通知模块")
@allure.story("死信落库")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestDeadLetterPersistence:
    """死信落库与仓储验证"""

    def test_dead_letter_fields_complete(self, temp_db):
        """
        死信字段完整: channel/execution_id/title/content全文/
        level/attempts/status="dead"逐字段实查断言

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        notifier = FakeNotifier("wechat", succeed_from=999)
        repo = NotificationDeadLetterRepository()
        router, _ = make_router([notifier], repo=repo, max_retries=1)

        results = router.notify(make_stat(), "RUN-DL1")

        assert results == {"wechat": False}
        dead_letters = repo.list_by_execution_id("RUN-DL1")
        assert len(dead_letters) == 1
        letter = dead_letters[0]
        assert letter["channel"] == "wechat"
        assert letter["execution_id"] == "RUN-DL1"
        assert "RUN-DL1" in letter["title"]  # 通知标题含批次号
        assert letter["level"] == "info"  # 全通过→info
        assert letter["attempts"] == 2  # 1+max_retries=2
        assert letter["status"] == "dead"
        # content为完整消息体（企微markdown摘要全文）
        assert "测试批次执行完成" in letter["content"]
        assert "用例总数" in letter["content"]

    def test_broken_repo_not_propagate(self):
        """
        仓储故障隔离: 注入save恒抛异常的坏repo，
        notify仍正常返回结果dict、不外抛

        参数:
            无

        返回:
            无
        """
        notifier = FakeNotifier("email", succeed_from=999)
        router, sleeper = make_router(
            [notifier], repo=BrokenRepo(), max_retries=1
        )

        # 死信落库失败被消化，notify正常返回
        results = router.notify(make_stat(), "RUN-BROKEN")

        assert results == {"email": False}
        assert notifier.send_count == 2

    def test_repository_crud(self, temp_db):
        """
        仓储CRUD: 两条不同批批次死信，批次隔离正确、
        count_all=2、list_all按id升序

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        repo = NotificationDeadLetterRepository()

        first_id = repo.save_dead_letter(
            channel="email", execution_id="RUN-A", title="标题A",
            content="内容A", level="warning",
            fail_reason="send返回False", attempts=4,
        )
        second_id = repo.save_dead_letter(
            channel="wechat", execution_id="RUN-B", title="标题B",
            content="内容B", level="critical",
            fail_reason="ConnectionError: 超时", attempts=2,
        )
        assert first_id < second_id

        # 批次隔离
        run_a = repo.list_by_execution_id("RUN-A")
        assert len(run_a) == 1
        assert run_a[0]["channel"] == "email"
        assert run_a[0]["title"] == "标题A"
        run_b = repo.list_by_execution_id("RUN-B")
        assert len(run_b) == 1
        assert run_b[0]["channel"] == "wechat"

        # 总数与升序
        assert repo.count_all() == 2
        all_letters = repo.list_all()
        assert [item["id"] for item in all_letters] == [first_id, second_id]


@allure.feature("通知模块")
@allure.story("端到端重试")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.api
@pytest.mark.regression
class TestRetryEndToEnd:
    """双渠道重试端到端验证"""

    def test_two_channels_mixed_results_with_jitter(self, temp_db):
        """
        端到端: 邮件首次成功、企微恒失败耗尽，
        results={"email":True,"wechat":False}，死信表仅wechat 1条；
        开启jitter后每次delay落在[base×2^k, base×2^k×1.25]区间

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        email = FakeNotifier("email", succeed_from=1)
        wechat = FakeNotifier("wechat", succeed_from=999)
        repo = NotificationDeadLetterRepository()
        router, sleeper = make_router(
            [email, wechat], repo=repo, max_retries=3, use_jitter=True
        )

        results = router.notify(make_stat(), "RUN-E2E")

        assert results == {"email": True, "wechat": False}
        # 死信仅wechat 1条（email成功不死信）
        dead_letters = repo.list_by_execution_id("RUN-E2E")
        assert len(dead_letters) == 1
        assert dead_letters[0]["channel"] == "wechat"
        assert dead_letters[0]["attempts"] == 4

        # jitter区间断言: 第k次等待∈[base×2^(k-1), ×1.25]
        base_delays = [1.0, 2.0, 4.0]
        assert len(sleeper.delays) == 3
        for actual, base in zip(sleeper.delays, base_delays):
            assert base <= actual <= base * 1.25, (
                f"delay {actual} 不在 [{base}, {base * 1.25}] 区间"
            )
