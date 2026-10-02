"""
通用增强断言库测试（Day42-coverage 覆盖率补全）

覆盖 src/common/assertion.py 原 51% 覆盖率的盲区，聚焦三块:
    1. 内部工具函数的异常分支: _extract_json（非法JSON）/
       _parse_json_path（索引非数组/索引越界/字段非字典/字段不存在）
    2. 响应类断言的失败分支: assert_status_code / assert_response_time /
       assert_header（不存在 + 值不匹配）
    3. JSON 类与通用逻辑断言的失败分支: assert_json_value /
       assert_json_contains / assert_json_path_exists / assert_equal /
       assert_not_equal / assert_contains / assert_is_empty /
       assert_is_not_empty / assert_greater / assert_less

设计原则:
    - 每个用例都断言"抛出了正确的异常类型"且"异常消息包含关键片段"，
      不使用恒真式空断言
    - mock response 用显式 helper 构造，避免各用例重复搭建
    - 不修改任何被测源码，全部通过公开函数与私有工具函数直接调用验证
"""

import json
from typing import Any
from unittest.mock import MagicMock

import allure
import pytest
from src.common.assertion import (
    _extract_json,
    _parse_json_path,
    assert_contains,
    assert_equal,
    assert_greater,
    assert_header,
    assert_is_empty,
    assert_is_not_empty,
    assert_json_contains,
    assert_json_path_exists,
    assert_json_value,
    assert_less,
    assert_not_equal,
    assert_response_time,
    assert_status_code,
)

TEST_URL = "http://test/api/cases"


def make_response(
    status_code: int = 200,
    json_data: Any = None,
    text: str = "",
    headers: dict | None = None,
    elapsed_seconds: float = 0.1,
    json_raises: bool = False,
) -> MagicMock:
    """
    构造 mock requests.Response 对象供断言函数测试

    参数:
        status_code (int): 模拟HTTP状态码
        json_data (Any): json() 返回值；json_raises=True 时忽略
        text (str): 响应体文本（非法JSON时用于断言"原始内容"片段）
        headers (dict | None): 响应头字典
        elapsed_seconds (float): 耗时秒数
        json_raises (bool): 为 True 时 json() 抛 ValueError

    返回:
        MagicMock: 具备 status_code/text/url/headers/elapsed/json 的 mock 对象
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.url = TEST_URL
    resp.headers = headers or {}
    resp.elapsed.total_seconds.return_value = elapsed_seconds
    if json_raises:
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1")
    else:
        resp.json.return_value = json_data
    return resp


# ===========================================================================
# 1. 内部工具: _extract_json
# ===========================================================================
@allure.feature("断言库内部工具")
class TestExtractJson:
    """_extract_json 的正常解析与非法JSON异常分支"""

    @allure.story("合法JSON正常返回解析结果")
    def test_extract_json_valid(self):
        resp = make_response(json_data={"code": 0, "data": {"id": 7}})
        assert _extract_json(resp) == {"code": 0, "data": {"id": 7}}

    @allure.story("JSONDecodeError 转 AssertionError 并附原始内容")
    def test_extract_json_decode_error(self):
        raw = "{not-json"
        resp = make_response(text=raw, json_raises=True)
        with pytest.raises(AssertionError) as exc:
            _extract_json(resp)
        message = str(exc.value)
        assert "响应体不是合法JSON" in message
        assert raw in message

    @allure.story("原始内容超200字符时截断展示")
    def test_extract_json_truncates_long_text(self):
        long_text = "x" * 500
        resp = make_response(text=long_text, json_raises=True)
        with pytest.raises(AssertionError) as exc:
            _extract_json(resp)
        message = str(exc.value)
        assert "x" * 200 in message
        assert "x" * 201 not in message

    @allure.story("底层异常类型为JSONDecodeError时同样被捕获")
    def test_extract_json_jsondecodeerror_subclass(self):
        resp = make_response(text="<html>", json_raises=True)
        resp.json.side_effect = json.JSONDecodeError("bad", "<html>", 0)
        with pytest.raises(AssertionError) as exc:
            _extract_json(resp)
        assert "响应体不是合法JSON" in str(exc.value)


# ===========================================================================
# 2. 内部工具: _parse_json_path
# ===========================================================================
@allure.feature("JSON路径解析")
class TestParseJsonPath:
    """_parse_json_path 的取值成功路径与四类异常分支"""

    @allure.story("纯字段名路径")
    def test_plain_field(self):
        assert _parse_json_path({"args": {"key": "v"}}, "args.key") == "v"

    @allure.story("数组索引路径 items[0].id")
    def test_array_index_path(self):
        data = {"items": [{"id": 11}, {"id": 22}]}
        assert _parse_json_path(data, "items[1].id") == 22

    @allure.story("多段混合路径 list[0].info.type")
    def test_mixed_path(self):
        data = {"list": [{"info": {"type": "T"}}]}
        assert _parse_json_path(data, "list[0].info.type") == "T"

    @allure.story("索引作用于非list时抛TypeError")
    def test_index_on_non_list_raises_type_error(self):
        with pytest.raises(TypeError) as exc:
            _parse_json_path({"items": {"a": 1}}, "items[0]")
        assert "期望数组" in str(exc.value)
        assert "dict" in str(exc.value)

    @allure.story("索引越界时抛IndexError并报出实际长度")
    def test_index_out_of_range_raises_index_error(self):
        with pytest.raises(IndexError) as exc:
            _parse_json_path({"items": [1, 2]}, "items[5]")
        message = str(exc.value)
        assert "越界" in message
        assert "数组长度: 2" in message

    @allure.story("字段路径作用于非dict时抛TypeError")
    def test_field_on_non_dict_raises_type_error(self):
        with pytest.raises(TypeError) as exc:
            _parse_json_path({"args": [1, 2]}, "args.key")
        assert "期望字典" in str(exc.value)
        assert "list" in str(exc.value)

    @allure.story("字段不存在时抛KeyError并列出可用字段")
    def test_missing_field_raises_key_error(self):
        with pytest.raises(KeyError) as exc:
            _parse_json_path({"args": {"a": 1}}, "args.missing")
        message = str(exc.value)
        assert "不存在" in message
        assert "可用字段" in message

    @allure.story("空路径按字面量空字段名处理，字典中无此键则抛KeyError")
    def test_empty_path_treated_as_empty_field_name(self):
        # "".split(".") -> [""], 段为空字符串，字典里不存在该键
        with pytest.raises(KeyError) as exc:
            _parse_json_path({"a": 1}, "")
        assert "字段''不存在" in str(exc.value)

    @allure.story("索引段前无字段名时（形如 [0]）仍能正确拆解")
    def test_bare_index_path(self):
        assert _parse_json_path([{"id": 5}], "[0].id") == 5


# ===========================================================================
# 3. 响应类断言
# ===========================================================================
@allure.feature("响应类断言")
class TestResponseAssertions:
    """assert_status_code / assert_response_time / assert_header"""

    @allure.story("单值期望匹配则通过")
    def test_status_code_pass_single(self):
        assert_status_code(make_response(200), 200)

    @allure.story("列表期望命中其一则通过")
    def test_status_code_pass_list(self):
        assert_status_code(make_response(201), [200, 201, 204])

    @allure.story("元组期望同样按集合处理")
    def test_status_code_pass_tuple(self):
        assert_status_code(make_response(302), (301, 302))

    @allure.story("状态码不匹配抛AssertionError并带实际/期望/URL")
    def test_status_code_fail(self):
        resp = make_response(404, text="not found")
        with pytest.raises(AssertionError) as exc:
            assert_status_code(resp, 200)
        message = str(exc.value)
        assert "状态码断言失败" in message
        assert "实际 404" in message
        assert "[200]" in message
        assert TEST_URL in message

    @allure.story("耗时未超阈值则通过")
    def test_response_time_pass(self):
        assert_response_time(make_response(elapsed_seconds=0.05), 100)

    @allure.story("耗时恰好等于阈值不算超限（严格大于才失败）")
    def test_response_time_boundary_equal(self):
        assert_response_time(make_response(elapsed_seconds=0.1), 100)

    @allure.story("耗时超阈值抛AssertionError并报出毫秒数")
    def test_response_time_fail(self):
        resp = make_response(elapsed_seconds=2.0)
        with pytest.raises(AssertionError) as exc:
            assert_response_time(resp, 100)
        message = str(exc.value)
        assert "响应耗时断言失败" in message
        assert "2000.0ms" in message
        assert "100ms" in message

    @allure.story("响应头存在且值匹配则通过")
    def test_header_pass_with_value(self):
        resp = make_response(headers={"Content-Type": "application/json"})
        assert_header(resp, "Content-Type", "application/json")

    @allure.story("expected为None时只校验存在性")
    def test_header_pass_existence_only(self):
        resp = make_response(headers={"X-Trace": "abc"})
        assert_header(resp, "X-Trace")

    @allure.story("响应头不存在时抛AssertionError并回显实际响应头")
    def test_header_missing_raises(self):
        resp = make_response(headers={"A": "1"})
        with pytest.raises(AssertionError) as exc:
            assert_header(resp, "X-Missing")
        message = str(exc.value)
        assert "X-Missing" in message
        assert "不存在" in message

    @allure.story("响应头值不匹配时抛AssertionError并带实际/期望")
    def test_header_value_mismatch_raises(self):
        resp = make_response(headers={"Content-Type": "text/html"})
        with pytest.raises(AssertionError) as exc:
            assert_header(resp, "Content-Type", "application/json")
        message = str(exc.value)
        assert "text/html" in message
        assert "application/json" in message


# ===========================================================================
# 4. JSON 数据断言
# ===========================================================================
@allure.feature("JSON数据断言")
class TestJsonAssertions:
    """assert_json_value / assert_json_contains / assert_json_path_exists"""

    @allure.story("路径值相等则通过")
    def test_json_value_pass(self):
        resp = make_response(json_data={"data": {"code": 0}})
        assert_json_value(resp, "data.code", 0)

    @allure.story("路径解析失败统一转AssertionError")
    def test_json_value_path_error_raises_assertion(self):
        resp = make_response(json_data={"data": {}})
        with pytest.raises(AssertionError) as exc:
            assert_json_value(resp, "data.absent", 1)
        message = str(exc.value)
        assert "解析失败" in message
        assert "data.absent" in message

    @allure.story("值不相等时抛AssertionError并用repr展示实际/期望")
    def test_json_value_mismatch_raises(self):
        resp = make_response(json_data={"data": {"code": 500}})
        with pytest.raises(AssertionError) as exc:
            assert_json_value(resp, "data.code", 0)
        message = str(exc.value)
        assert "JSON值断言失败" in message
        assert "500" in message

    @allure.story("响应非JSON时先由_extract_json拦截")
    def test_json_value_non_json_response_raises(self):
        resp = make_response(text="oops", json_raises=True)
        with pytest.raises(AssertionError) as exc:
            assert_json_value(resp, "a", 1)
        assert "不是合法JSON" in str(exc.value)

    @allure.story("子集全部匹配则通过")
    def test_json_contains_pass(self):
        resp = make_response(json_data={"code": 0, "msg": "ok", "extra": 1})
        assert_json_contains(resp, {"code": 0, "msg": "ok"})

    @allure.story("空期望子集恒通过")
    def test_json_contains_empty_subset(self):
        assert_json_contains(make_response(json_data={"a": 1}), {})

    @allure.story("响应顶层非dict时抛AssertionError并报实际类型")
    def test_json_contains_top_level_not_dict(self):
        resp = make_response(json_data=[1, 2, 3])
        with pytest.raises(AssertionError) as exc:
            assert_json_contains(resp, {"code": 0})
        message = str(exc.value)
        assert "顶层不是对象" in message
        assert "list" in message

    @allure.story("字段缺失时差异信息含'缺失'")
    def test_json_contains_field_missing(self):
        resp = make_response(json_data={"code": 0})
        with pytest.raises(AssertionError) as exc:
            assert_json_contains(resp, {"code": 0, "msg": "ok"})
        message = str(exc.value)
        assert "字段'msg'缺失" in message
        assert "JSON子集断言失败" in message

    @allure.story("字段值不匹配时差异信息含实际/期望")
    def test_json_contains_value_mismatch(self):
        resp = make_response(json_data={"code": 500})
        with pytest.raises(AssertionError) as exc:
            assert_json_contains(resp, {"code": 0})
        message = str(exc.value)
        assert "实际: 500" in message
        assert "期望: 0" in message

    @allure.story("路径存在性成立则通过（含数组索引）")
    def test_json_path_exists_pass(self):
        resp = make_response(json_data={"items": [{"id": 3}]})
        assert_json_path_exists(resp, "items[0].id")

    @allure.story("路径不存在时抛AssertionError并带完整路径")
    def test_json_path_exists_missing_raises(self):
        resp = make_response(json_data={"items": []})
        with pytest.raises(AssertionError) as exc:
            assert_json_path_exists(resp, "items[0]")
        message = str(exc.value)
        assert "路径存在性断言失败" in message
        assert "items[0]" in message

    @allure.story("路径存在性遇非JSON响应时先报JSON错误")
    def test_json_path_exists_non_json_response(self):
        resp = make_response(text="<html/>", json_raises=True)
        with pytest.raises(AssertionError) as exc:
            assert_json_path_exists(resp, "a")
        assert "不是合法JSON" in str(exc.value)


# ===========================================================================
# 5. 通用逻辑断言
# ===========================================================================
@allure.feature("通用逻辑断言")
class TestLogicAssertions:
    """assert_equal / assert_not_equal / assert_contains / 空值 / 大小比较"""

    @allure.story("相等则通过（无附加信息）")
    def test_equal_pass(self):
        assert_equal(3, 3)

    @allure.story("不相等时抛AssertionError")
    def test_equal_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_equal(1, 2)
        assert "相等断言失败" in str(exc.value)

    @allure.story("不相等时附加message被拼接到错误信息")
    def test_equal_fail_with_message(self):
        with pytest.raises(AssertionError) as exc:
            assert_equal("a", "b", message="对比用户名")
        assert "附加信息: 对比用户名" in str(exc.value)

    @allure.story("不相等断言通过")
    def test_not_equal_pass(self):
        assert_not_equal(1, 2)

    @allure.story("两值相等时抛AssertionError并带附加信息")
    def test_not_equal_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_not_equal(7, 7, message="不应相同")
        message = str(exc.value)
        assert "不相等断言失败" in message
        assert "附加信息: 不应相同" in message

    @allure.story("字符串包含子串则通过")
    def test_contains_str_pass(self):
        assert_contains("hello world", "world")

    @allure.story("列表包含元素则通过")
    def test_contains_list_pass(self):
        assert_contains([1, 2, 3], 2)

    @allure.story("字典包含键则通过")
    def test_contains_dict_pass(self):
        assert_contains({"k": "v"}, "k")

    @allure.story("container不支持in运算时抛AssertionError而非TypeError")
    def test_contains_unsupported_type_raises_assertion(self):
        with pytest.raises(AssertionError) as exc:
            assert_contains(123, 1)
        message = str(exc.value)
        assert "不支持包含判断" in message
        assert "int" in message

    @allure.story("不包含时抛AssertionError并带附加信息")
    def test_contains_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_contains([1, 2], 9, message="查用例ID")
        message = str(exc.value)
        assert "包含断言失败" in message
        assert "附加信息: 查用例ID" in message

    @allure.story("空值断言通过: None/空串/空列表/空字典")
    def test_is_empty_pass(self):
        assert_is_empty(None)
        assert_is_empty("")
        assert_is_empty([])
        assert_is_empty({})

    @allure.story("非空值触发空值断言失败并带附加信息")
    def test_is_empty_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_is_empty([0], message="期望为空列表")
        message = str(exc.value)
        assert "为空断言失败" in message
        assert "附加信息: 期望为空列表" in message

    @allure.story("非空断言通过")
    def test_is_not_empty_pass(self):
        assert_is_not_empty([0])

    @allure.story("空值触发非空断言失败并带附加信息")
    def test_is_not_empty_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_is_not_empty("", message="字段必填")
        message = str(exc.value)
        assert "非空断言失败" in message
        assert "附加信息: 字段必填" in message

    @allure.story("大于断言通过（整数与浮点）")
    def test_greater_pass(self):
        assert_greater(10, 9)
        assert_greater(1.5, 1.4)

    @allure.story("小于等于阈值触发大于断言失败并带附加信息")
    def test_greater_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_greater(5, 5, message="阈值5")
        message = str(exc.value)
        assert "大于断言失败" in message
        assert "附加信息: 阈值5" in message

    @allure.story("小于断言通过")
    def test_less_pass(self):
        assert_less(1, 2)

    @allure.story("大于等于阈值触发小于断言失败并带附加信息")
    def test_less_fail(self):
        with pytest.raises(AssertionError) as exc:
            assert_less(3, 3, message="上限3")
        message = str(exc.value)
        assert "小于断言失败" in message
        assert "附加信息: 上限3" in message
