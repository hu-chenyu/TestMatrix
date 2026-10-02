"""
TestMatrix 大扫除 v3 · Commit A：HttpClient 查询串脱敏并行路径修复

背景（v2 审查 V2-P1-1）
------------------------------------
`SENSITIVE_QUERY_FIELDS`（含裸 `key`）只作用于 URL 字符串（`_safe_url`），
而 requests 生态传查询参数的正规做法是 `params=` 字典——该字典走
`_mask_data`，用的是**不含裸 `key`** 的 `SENSITIVE_BODY_FIELDS`。
结果是同一份凭据存在两条并行的日志路径：

    client.get("/cgi-bin/webhook/send", params={"key": "<真实企微 key>"})
    # URL 里的 ?key= 会被 _safe_url 打码
    # 但 params 字典原样进日志 -> 明文

模块自己的用法示例（http_client.py 顶部）就是 `params={"key": "value"}`。

修复
------------------------------------
`_mask_data` 增加 `fields` 参数，按**数据的语义位置**选择字段表：
`params` 传 `SENSITIVE_QUERY_FIELDS`，`json`/`data` 传
`SENSITIVE_BODY_FIELDS`。

为什么 body 侧仍不打码裸 `key`
------------------------------------
请求体里名为 `key` 的字段通常是业务数据（字典键、分片键、排序键），
打码会显著削弱日志的排障价值；而真正的 API 凭据命名已由
`api_key`/`apikey` 覆盖。`params` 语义上就是查询串，不存在这个歧义，
故两者口径必须分家——这正是本次修复的核心。

测试铁律
------------------------------------
- 零真实网络：全部用桩替身替换 session.request
- loguru 日志断言用临时 sink，不用 caplog（loguru 不经 stdlib 通道）
- 断言不锁死日志文案全文，只校验"敏感值不出现 + 非敏感值仍在"
"""

from collections.abc import Iterator
from typing import Any

import allure
import pytest
import requests
from loguru import logger as loguru_logger
from src.common.http_client import (
    SENSITIVE_BODY_FIELDS,
    SENSITIVE_QUERY_FIELDS,
    HttpClient,
)

# 占位凭据：非真实密钥，仅用于断言日志中是否出现
PLACEHOLDER_KEY = "PLACEHOLDER-NOT-A-REAL-KEY"
PLACEHOLDER_TOKEN = "PLACEHOLDER-NOT-A-REAL-TOKEN"


class _LogCapture:
    """loguru 日志捕获器（context manager）"""

    def __init__(self, level: str = "DEBUG") -> None:
        """
        初始化捕获缓冲

        参数:
            level (str): 捕获级别，默认 DEBUG（需含前置请求日志）
        """
        self.messages: list[str] = []
        self._level = level
        self._sink_id: int | None = None

    def __enter__(self) -> "_LogCapture":
        """注册 loguru sink 开始收集"""
        self._sink_id = loguru_logger.add(
            self.messages.append, level=self._level
        )
        return self

    def __exit__(self, *_exc: Any) -> None:
        """移除 sink（不吞异常）"""
        if self._sink_id is not None:
            loguru_logger.remove(self._sink_id)
            self._sink_id = None


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[HttpClient]:
    """
    零网络的 HttpClient 夹具：替换 session.request 为桩

    teardown:
        monkeypatch 自动恢复；client.close() 释放会话

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量与属性补丁工具

    返回:
        Iterator[HttpClient]: yield 已桩化的 HttpClient 实例
    """
    client = HttpClient(base_url="https://api.example.com")
    response = requests.Response()
    response.status_code = 200
    response._content = b"{}"
    monkeypatch.setattr(
        client.session, "request", lambda *a, **k: response
    )
    yield client
    client.close()


# ===========================================================================
# A组: params 字典按查询串字段表脱敏
# ===========================================================================
@allure.feature("大扫除v3安全修复")
@allure.story("params 字典脱敏")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestParamsMasking:
    """params 位置的凭据必须打码"""

    @pytest.mark.parametrize(
        "param_name",
        ["key", "token", "access_token", "api_key", "apikey", "password"],
        ids=["key", "token", "access_token", "api_key", "apikey", "password"],
    )
    def test_params_credential_is_masked(
        self, stub_client: HttpClient, param_name: str
    ) -> None:
        """
        params 中的凭据字段必须打码

        修复前这些字段走请求体字段表，`key` 因不在表中而原样进日志。
        """
        capture = _LogCapture()
        with capture:
            stub_client.get("/cgi-bin/webhook/send", params={param_name: PLACEHOLDER_KEY})

        joined = "\n".join(capture.messages)
        assert PLACEHOLDER_KEY not in joined, (
            f"params.{param_name} 的值泄露进了日志:\n{joined}"
        )

    def test_params_business_fields_preserved(
        self, stub_client: HttpClient
    ) -> None:
        """
        params 中的非敏感字段必须原样保留（脱敏不能把日志打成无用信息）

        分页/筛选类参数（page、module、q 等）不打码，否则排障时无法
        看出"这次请求到底查了什么"。
        """
        capture = _LogCapture()
        with capture:
            stub_client.get("/api/cases", params={"page": "2", "module": "用户中心"})

        joined = "\n".join(capture.messages)
        assert "'page': '2'" in joined or '"page": "2"' in joined, (
            f"非敏感 params 应原样保留:\n{joined}"
        )
        assert "用户中心" in joined

    def test_same_credential_masked_in_both_url_and_params(
        self, stub_client: HttpClient
    ) -> None:
        """
        同一份凭据写在 URL 与写在 params 时，日志里都必须不可见

        这是本条修复的核心断言：修复前 URL 侧打码、params 侧明文，
        两条路径行为不一致；修复后两者口径统一。
        """
        capture = _LogCapture()
        with capture:
            stub_client.get(
                f"/send?key={PLACEHOLDER_KEY}",
                params={"key": PLACEHOLDER_KEY},
            )

        joined = "\n".join(capture.messages)
        assert PLACEHOLDER_KEY not in joined, (
            f"URL 或 params 任一侧泄露了凭据:\n{joined}"
        )

    def test_nested_params_list_masked(self, stub_client: HttpClient) -> None:
        """
        params 传 list 时同样按查询串字段表逐项打码

        `_mask_data` 对 list 递归时必须把 fields 传下去，否则嵌套层
        又会退回请求体字段表。
        """
        capture = _LogCapture()
        with capture:
            stub_client.get(
                "/batch", params=[{"key": PLACEHOLDER_KEY}, {"page": "1"}]
            )

        joined = "\n".join(capture.messages)
        assert PLACEHOLDER_KEY not in joined, (
            f"list 型 params 的嵌套凭据泄露:\n{joined}"
        )


# ===========================================================================
# B组: 请求体侧口径不被误改
# ===========================================================================
@allure.feature("大扫除v3安全修复")
@allure.story("请求体脱敏口径")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestBodyMaskingUnchanged:
    """body 侧行为不得被本次修复带偏"""

    def test_body_credential_still_masked(self, stub_client: HttpClient) -> None:
        """
        body 中的凭据字段继续打码（回归守卫）

        本次给 `_mask_data` 加了默认参数，若默认值写错，body 侧会
        静默失去脱敏——这条锁死它没变。
        """
        capture = _LogCapture()
        with capture:
            stub_client.post("/login", json={"password": PLACEHOLDER_TOKEN})

        assert PLACEHOLDER_TOKEN not in "\n".join(capture.messages)

    def test_body_business_key_not_masked(self, stub_client: HttpClient) -> None:
        """
        body 中名为 key 的业务字段**不**打码（刻意决策，防止被"顺手对齐"）

        请求体里的 `key` 通常是业务数据（字典键、分片键）。真正需要
        保护的 API 凭据命名是 api_key/apikey，已在字段表内。这条把
        "params 打码、body 不打码"的口径分家钉死，防止将来有人为了
        "一致性"把裸 key 塞进 SENSITIVE_BODY_FIELDS——那会让所有含
        key 字段的业务请求日志变得不可读。
        """
        capture = _LogCapture()
        with capture:
            stub_client.post("/query", json={"key": "order-20260916-0001"})

        joined = "\n".join(capture.messages)
        assert "order-20260916-0001" in joined, (
            f"body 的业务 key 不应被打码:\n{joined}"
        )

    def test_default_field_set_is_body_semantics(self) -> None:
        """
        `_mask_data` 的默认字段表是请求体口径

        直接锁定默认值，防止将来把默认改成查询串表导致 body 侧行为
        在无调用方察觉的情况下变化。
        """
        import inspect

        signature = inspect.signature(HttpClient._mask_data)
        default = signature.parameters["fields"].default

        assert default == SENSITIVE_BODY_FIELDS

    def test_query_credential_set_covers_body_credential_set(self) -> None:
        """
        结构不变量: 请求体凭据集合 ⊆ 查询串凭据集合

        查询串侧可以更宽（含裸 key），但不能比请求体侧窄——否则会出现
        "同名凭据放 body 打码、放 query 反而明文"这种反直觉缺口
        （access_token 当初就是这么漏进 body 表之外的）。
        """
        assert set(SENSITIVE_BODY_FIELDS) <= set(SENSITIVE_QUERY_FIELDS), (
            "查询串凭据表必须覆盖请求体凭据表，差集: "
            f"{sorted(set(SENSITIVE_BODY_FIELDS) - set(SENSITIVE_QUERY_FIELDS))}"
        )
