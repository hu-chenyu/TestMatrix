"""
TestMatrix 大扫除 v2 · 任务一：data_driver 边缘路径覆盖补齐

覆盖目标
--------
本文件针对 src/core/data_driver.py 的 22 条未覆盖语句（87% -> 100%）。
未覆盖清单与对应场景：

    行号      场景                                  本文件用例
    --------  ------------------------------------  ------------------
    154       不支持的文件后缀（.txt）              test_unsupported_suffix_rejected
    204-205   filter_cases 传入空列表              test_filter_empty_input_returns_empty
    267-269   YAML 文件读取 OSError（权限/句柄）    test_yaml_read_oserror_wrapped
    285-288   YAML 顶层字典无列表值键              test_yaml_dict_without_list_key
    289-293   YAML 顶层字典存在多个列表键          test_yaml_dict_with_multiple_list_keys
    295-297   YAML 顶层既非列表也非字典（标量）    test_yaml_scalar_top_level_rejected
    325-327   Excel 文件打开失败                   test_excel_open_failure_wrapped
    337       指定 sheet 不存在                    test_missing_sheet_rejected
    351       Excel 首行表头全为空                  test_excel_blank_header_rejected
    408       用例条目不是字典（列表里混入标量）    test_non_dict_case_rejected
    449       tags 字段类型非法（int/None）         test_invalid_tags_type_rejected
    483       数据文件路径为空                     test_empty_path_rejected
    489       相对路径的 PROJECT_ROOT 兜底分支      test_relative_path_resolved_against_cwd
    521       筛选维度传入空列表                   test_empty_list_dimension_treated_as_no_filter
    523-524   筛选维度类型非法（int/dict）          test_invalid_dimension_type_treated_as_no_filter

设计要点
--------
- 全部走真实文件（tmp_path），不 mock 文件系统：这些分支的价值恰恰在于
  与真实 openpyxl / yaml / pathlib 的交互，mock 掉就测不到真实行为
- 不覆盖的语句：无。本文件 22 条目标语句全部可测，无"设计上不可达"项
- 失败断言一律校验异常类型 + 关键信息片段，不锁死完整文案

测试铁律（对齐 PROJECT_CONTEXT.md 7.16 + 验收清单第三条）
- 不依赖真实硬件/服务，不访问项目 testdata/（禁改目录）
- 每条用例独立 tmp_path 文件，无跨例残留
- 无 time.sleep 固定等待，无 print
"""

import builtins
from pathlib import Path

import allure
import pytest
import yaml
from src.core.data_driver import DataDriver, DataDriverError

# 构造合法用例的最小字段集（其余校验项由各自用例单独构造）
_VALID_CASE = {
    "case_id": "TM-VD-0001",
    "name": "数据驱动边缘用例",
    "module": "用户中心",
}


def _write_yaml(path: Path, payload: object) -> Path:
    """把任意可 YAML 序列化的对象写成测试文件并返回路径"""
    path.write_text(
        yaml.dump(payload, allow_unicode=True), encoding="utf-8"
    )
    return path


# ===========================================================================
# A组: 文件格式与路径解析
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("data_driver 文件格式与路径")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestDataDriverFileHandling:
    """不支持的后缀 / 路径为空 / 相对路径解析"""

    def test_unsupported_suffix_rejected(self, tmp_path: Path) -> None:
        """
        不支持的文件后缀必须显式拒绝（行154）

        静默回落任何默认解析会让人以为"读进来了"，实际数据全丢。
        """
        bad_file = tmp_path / "cases.txt"
        bad_file.write_text("随便写点什么", encoding="utf-8")

        with pytest.raises(DataDriverError, match="不支持的文件格式"):
            DataDriver.load_cases(bad_file)

    def test_empty_path_rejected(self) -> None:
        """
        数据文件路径为空必须显式拒绝（行483）

        空路径会一路走到"文件不存在"并打印一长串候选位置，错误信息
        完全指不到"你压根没传路径"这个真因。
        """
        for empty_path in ("", "   "):
            with pytest.raises(DataDriverError, match="路径不能为空"):
                DataDriver.load_cases(empty_path)

    def test_missing_file_error_lists_attempted_paths(
        self, tmp_path: Path
    ) -> None:
        """
        文件不存在时错误信息须列出已尝试位置（排障刚需）

        相对路径会依次尝试 cwd 与项目根，两处都没找到才是根因，
        错误信息必须把这两个候选位置都说出来。
        """
        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(tmp_path / "not_exists.yaml")

        message = str(excinfo.value)
        assert "数据文件不存在" in message
        assert "已尝试位置" in message, "错误信息应列出候选路径，否则排障无从下手"

    def test_relative_path_resolved_against_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        相对路径按 cwd 解析成功即返回（行489 的 candidates 分支）

        必须先 chdir 到临时目录：相对路径只在 [cwd/路径, 项目根/路径]
        两处候选里找，不切目录的话文件压根不在候选范围内。
        验证的是"相对路径能在候选中命中"，不锁定命中的是哪一处——
        那是运行环境的产物，断言死会让换个 cwd 就红。
        """
        target = tmp_path / "relative_cases.yaml"
        _write_yaml(target, [{**_VALID_CASE, "priority": "P1"}])
        monkeypatch.chdir(tmp_path)

        loaded = DataDriver.load_cases("relative_cases.yaml")

        assert len(loaded) == 1
        assert loaded[0]["case_id"] == "TM-VD-0001"


# ===========================================================================
# B组: YAML 解析异常路径
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("data_driver YAML 异常路径")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestDataDriverYamlErrors:
    """读取失败 / 顶层结构非法 / 顶层字典歧义"""

    def test_yaml_read_oserror_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        YAML 文件读取 OSError 被包装为 DataDriverError（行267-269）

        触发方式: 替换内建 open 使其抛 OSError，模拟真实 OS 层读取失败
        （权限不足 / 文件被独占锁定）。走真实文件系统制造这类错误在
        Windows 上不可靠（要改 ACL 或独占句柄），而本条要验的是
        "OSError ��被包装成 DataDriverError 且不冒泡"这一层契约。
        """
        yaml_file = tmp_path / "cases.yaml"
        yaml_file.write_text("[]", encoding="utf-8")
        real_open = builtins.open

        def _raise_permission_denied(*args, **kwargs):
            """模拟无权限读取"""
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(builtins, "open", _raise_permission_denied)

        with pytest.raises(DataDriverError, match="文件读取失败"):
            DataDriver.load_cases(yaml_file)

        monkeypatch.setattr(builtins, "open", real_open)

    def test_non_utf8_yaml_escapes_as_unicode_decode_error(
        self, tmp_path: Path
    ) -> None:
        """
        **已知缺陷**（大扫除 v2 审查发现，待修）: 非 UTF-8 的 YAML 文件
        让 UnicodeDecodeError 原样逃逸，未被包装为 DataDriverError。

        实测根因: _load_yaml 只 catch (yaml.YAMLError, OSError)，而
        UnicodeDecodeError 继承自 UnicodeError -> ValueError，既不是
        YAMLError 也不是 OSError，两条 except 都接不住。而解码错误是在
        safe_load(file_handle) 读取句柄时才发生的，不在 open() 阶段，
        因此也不可能被前一处的 open 失败路径覆盖。

        影响: 用例导入接口遇到 GBK 编码的 YAML 时会返回 500 且响应体是
        裸的 codec 错误文本，而不是 400 + "文件编码错误，请用 UTF-8"。

        本条**如实断言当前实际行为**而非期望行为：任务一只允许新建测试
        文件、不改业务源码。修复时把本条改为断言 DataDriverError 即可，
        该缺陷已记入 docs/bug_audit_report_v2_20261002.md。
        """
        binary_file = tmp_path / "cases.yaml"
        # 0xFF 不是合法 UTF-8 起始字节，读取时必抛解码错误
        binary_file.write_bytes(b"\xff\xfe\x00binary")

        with pytest.raises(UnicodeDecodeError):
            DataDriver.load_cases(binary_file)

    def test_yaml_syntax_error_wrapped(self, tmp_path: Path) -> None:
        """
        YAML 语法错误走 YAMLError 分支（对照组）

        与上一条成对：证明读取失败与语法失败确实分流到了不同分支，
        而不是都掉进同一个 except。
        """
        broken = tmp_path / "cases.yaml"
        broken.write_text("cases: [ {unclosed: ", encoding="utf-8")

        with pytest.raises(DataDriverError, match="YAML语法解析失败"):
            DataDriver.load_cases(broken)

    def test_yaml_dict_without_list_key(self, tmp_path: Path) -> None:
        """
        顶层字典中找不到值为列表的键必须报错（行285-288）

        典型误用: 顶层写成 {meta: {...}, cases: {...}}，cases 的值也是
        字典。静默返回空列表会让调用方以为"文件里没有用例"。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml", {"meta": {"version": "v1"}}
        )

        with pytest.raises(DataDriverError, match="未找到值为列表的键"):
            DataDriver.load_cases(yaml_file)

    def test_yaml_dict_with_multiple_list_keys(self, tmp_path: Path) -> None:
        """
        顶层字典存在多个列表键必须报错并列出全部候选（行289-293）

        静默取第一个列表键是最坏的失败模式：文件里同时有 cases 与
        fixtures 时，会把 fixtures 当用例导进去，且不报任何错。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            {"cases": [{**_VALID_CASE, "priority": "P1"}], "fixtures": [1, 2]},
        )

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(yaml_file)

        message = str(excinfo.value)
        assert "存在多个列表键" in message
        assert "cases" in message and "fixtures" in message, (
            "错误信息应列出全部候选键名，便于判断该拆哪个"
        )

    def test_yaml_scalar_top_level_rejected(self, tmp_path: Path) -> None:
        """
        YAML 顶层既非列表也非字典（标量）必须报错（行295-297）

        文件内容写成单个字符串/数字时，safe_load 返回标量而非容器。
        """
        scalar_file = _write_yaml(tmp_path / "cases.yaml", "只有一行文本")

        with pytest.raises(DataDriverError, match="顶层结构非法"):
            DataDriver.load_cases(scalar_file)

    def test_yaml_single_list_key_extracted(self, tmp_path: Path) -> None:
        """
        顶层字典含唯一列表键时正常提取（正向对照）

        证明上一条"多个列表键报错"不是因为字典格式本身不被支持。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            {"cases": [{**_VALID_CASE, "priority": "P2"}]},
        )

        loaded = DataDriver.load_cases(yaml_file)

        assert len(loaded) == 1
        assert loaded[0]["priority"] == "P2"


# ===========================================================================
# C组: Excel 解析异常路径
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("data_driver Excel 异常路径")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestDataDriverExcelErrors:
    """文件打不开 / sheet 不存在 / 表头全空"""

    def test_excel_open_failure_wrapped(self, tmp_path: Path) -> None:
        """
        Excel 文件打开失败被包装为 DataDriverError（行325-327）

        用后缀合法但内容不是 zip 容器的文件触发：用户把 .xls 改名成
        .xlsx 是极常见的操作，openpyxl 抛的异常类型多样，统一包装后
        上层不必逐个 catch。
        """
        fake_excel = tmp_path / "cases.xlsx"
        fake_excel.write_bytes(b"this is definitely not a zip container")

        with pytest.raises(DataDriverError, match="Excel文件打开失败"):
            DataDriver.load_cases(fake_excel)

    def test_missing_sheet_rejected(self, tmp_path: Path) -> None:
        """
        指定 sheet 不存在必须报错并列出可用 sheet（行337）

        错误信息列出 sheetnames 是排障刚需：用户往往只是打错了一个字，
        让他看见"当前可用 sheet: [...]"就能立刻自查。
        """
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["case_id", "name", "module", "priority"])
        sheet.append(["TM-VD-0002", "指定sheet用例", "订单中心", "P1"])
        real_excel = tmp_path / "cases.xlsx"
        workbook.save(real_excel)

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(real_excel, sheet_name="不存在的Sheet")

        message = str(excinfo.value)
        assert "指定sheet不存在" in message
        assert "Sheet1" in message, "错误信息应列出可用 sheet 名"

    def test_named_sheet_loaded(self, tmp_path: Path) -> None:
        """
        按名称指定存在的 sheet 时正常读取（正向对照）

        与上一条成对，证明拒绝的是"不存在的 sheet"而非"按名取 sheet"本身。
        """
        from openpyxl import Workbook

        workbook = Workbook()
        first = workbook.active
        first.title = "封面"
        first.append(["这行数据不应该被读到"])
        second = workbook.create_sheet("用例")
        second.append(["case_id", "name", "module", "priority"])
        second.append(["TM-VD-0003", "按名取sheet", "支付中心", "P0"])
        real_excel = tmp_path / "cases.xlsx"
        workbook.save(real_excel)

        loaded = DataDriver.load_cases(real_excel, sheet_name="用例")

        assert len(loaded) == 1
        assert loaded[0]["case_id"] == "TM-VD-0003"

    def test_excel_blank_header_rejected(self, tmp_path: Path) -> None:
        """
        Excel 首行表头全为空必须报错（行351）

        用户新建 Excel 直接从第二行开始填数据、首行留空是高频操作。
        若不拦，取到的字段名全是空串，所有行都变成"缺 case_id"，
        错误信息完全指不到"表头行没填"这个真因。
        """
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append([None, "", "   "])
        sheet.append(["TM-VD-0004", "表头为空", "用户中心", "P1"])
        real_excel = tmp_path / "cases.xlsx"
        workbook.save(real_excel)

        with pytest.raises(DataDriverError, match="表头全部为空"):
            DataDriver.load_cases(real_excel)

    def test_excel_empty_content_rejected(self, tmp_path: Path) -> None:
        """
        Excel 内容完全为空（无表头行）必须报错（对照组）

        与上一条区分开：一个是"有行但表头空"，一个是"压根没有行"。
        两条错误文案不同，混用会让排障误判。
        """
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.save(tmp_path / "cases.xlsx")

        with pytest.raises(DataDriverError, match="内容为空"):
            DataDriver.load_cases(tmp_path / "cases.xlsx")


# ===========================================================================
# D组: 用例校验异常路径
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("data_driver 用例字段校验")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestDataDriverCaseValidation:
    """非字典条目 / tags 类型非法 / 必填字段缺失"""

    def test_non_dict_case_rejected(self, tmp_path: Path) -> None:
        """
        用例条目不是字典必须报错并说明实际类型（行408）

        顶层列表里混入标量（如手滑多写了一个 `- xxx`）时，
        直接 .get() 会抛 AttributeError 逃出成 500，而不是给出可读
        的数据文件错误。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [{**_VALID_CASE, "priority": "P1"}, "这是一条误写的字符串"],
        )

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(yaml_file)

        message = str(excinfo.value)
        assert "结构非法" in message
        assert "str" in message, "错误信息应说明实际类型，便于定位是哪一行写错"

    @pytest.mark.parametrize(
        "bad_tags",
        [None, {"a": "b"}],
        ids=["none", "dict"],
    )
    def test_invalid_tags_type_rejected(
        self, tmp_path: Path, bad_tags: object
    ) -> None:
        """
        tags 归一后仍非 str/非 list 时报错（行449）

        真正能走到这条 else 分支的只有 None 与复合字典：
        int/float 会被 _normalize_scalar 先归一成字符串（见下一条用例），
        list 走 elif 分支。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [{**_VALID_CASE, "priority": "P1", "tags": bad_tags}],
        )

        with pytest.raises(DataDriverError, match="'tags'非法"):
            DataDriver.load_cases(yaml_file)

    def test_numeric_tags_normalized_to_string(self, tmp_path: Path) -> None:
        """
        数字型 tags 被归一为字符串而非拒绝（**有意设计**，非缺陷）

        _normalize_scalar 的 docstring 明确点名 tags=123 这个场景：
        Excel 把纯数字单元格存为 int/float，硬性要求 str 会让用户
        手填的数字标签被误判为"字段非法"。故此处锁定归一后的取值，
        防止后来者把它当 bug 改回严格校验。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [
                {**_VALID_CASE, "case_id": "TM-VD-0020", "priority": "P1",
                 "tags": 123},
                {**_VALID_CASE, "case_id": "TM-VD-0021", "priority": "P1",
                 "tags": 45.6},
            ],
        )

        loaded = DataDriver.load_cases(yaml_file)

        assert loaded[0]["tags"] == ["123"], "整数标签应归一为字符串列表"
        assert loaded[1]["tags"] == ["45.6"], "小数标签应归一为字符串列表"

    def test_tags_string_and_list_accepted(self, tmp_path: Path) -> None:
        """
        tags 的两种合法形态均被接受（正向对照）

        证明上一条拦的是"类型非法"而非"tags 非空"。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [
                {
                    **_VALID_CASE,
                    "case_id": "TM-VD-0005",
                    "priority": "P1",
                    "tags": "smoke, regression",
                },
                {**_VALID_CASE, "case_id": "TM-VD-0006", "priority": "P1",
                 "tags": ["smoke", "chip"]},
            ],
        )

        loaded = DataDriver.load_cases(yaml_file)

        assert loaded[0]["tags"] == ["smoke", "regression"]
        assert loaded[1]["tags"] == ["smoke", "chip"]

    def test_missing_required_field_reports_location(self, tmp_path: Path) -> None:
        """
        必填字段缺失须报出定位（第几条用例）与字段名

        定位信息是数据驱动排障的主要抓手：用户要看的是"第几条有问题"，
        而不只是"有个字段缺失"。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [
                {**_VALID_CASE, "priority": "P1"},
                {"name": "缺编号的用例", "module": "用户中心", "priority": "P1"},
            ],
        )

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(yaml_file)

        message = str(excinfo.value)
        assert "第2条用例" in message, "错误信息应带用例序号定位"
        assert "case_id" in message, "错误信息应指名是哪个字段"


# ===========================================================================
# E组: 筛选维度归一化
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("data_driver 筛选维度归一化")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestDataDriverFilterNormalization:
    """空输入 / 空列表维度 / 非法类型维度"""

    def test_filter_empty_input_returns_empty(self) -> None:
        """
        filter_cases 传入空列表直接返回空结果（行204-205）

        空输入是调用方"筛选无命中"的自然结果，不该打 error 级日志，
        但需要一条 warning 表明是输入为空而非筛选条件过严。
        """
        assert DataDriver.filter_cases([]) == [], "空输入应返回空列表而非报错"

    def test_empty_list_dimension_treated_as_no_filter(self) -> None:
        """
        筛选维度传入空列表视为"不过滤该维度"（行521）

        前端多选控件在用户清空选择时提交的就是空列表；此时按"无匹配"
        处理会让列表凭空清空，语义上应是"不按该维度筛"。
        """
        cases = [
            {**_VALID_CASE, "case_id": "TM-VD-0010", "priority": "P0",
             "module": "用户中心"},
            {**_VALID_CASE, "case_id": "TM-VD-0011", "priority": "P1",
             "module": "订单中心"},
        ]

        matched = DataDriver.filter_cases(cases, module=[], priority=[], tags=[])

        assert len(matched) == 2, "全维度空列表应等价于不过滤，返回全部"

    def test_invalid_dimension_type_treated_as_no_filter(self) -> None:
        """
        筛选维度类型非法（int/dict）视为不过滤（行523-524）

        与空列表同策略：宁可放行也不能让非法类型把列表清空——那会
        让用户以为数据丢了。区别是类型非法更可能是调用方的 bug，
        故记 warning 提示。
        """
        cases = [
            {**_VALID_CASE, "case_id": "TM-VD-0012", "priority": "P1",
             "module": "用户中心"},
        ]

        matched = DataDriver.filter_cases(
            cases, module=123, priority={"p": "P1"}, tags=3.14
        )

        assert len(matched) == 1, "维度类型非法应视为不过滤而非无匹配"

    def test_real_dimension_still_filters(self) -> None:
        """
        合法维度仍正常过滤（正向对照）

        证明上面三条拦的是"无效输入"，没有把过滤功能整体放水。
        """
        cases = [
            {**_VALID_CASE, "case_id": "TM-VD-0013", "priority": "P0",
             "module": "用户中心"},
            {**_VALID_CASE, "case_id": "TM-VD-0014", "priority": "P1",
             "module": "订单中心"},
        ]

        matched = DataDriver.filter_cases(cases, module="订单中心")

        assert len(matched) == 1
        assert matched[0]["case_id"] == "TM-VD-0014"

    def test_priority_mismatch_filters_out(self) -> None:
        """
        优先级不匹配的用例被跳过（行219）

        与"维度非法视为不过滤"成对：维度**有效**但不命中时，必须真的
        过滤掉——否则上面的容错分支会把整个筛选功能放水。
        """
        cases = [
            {**_VALID_CASE, "case_id": "TM-VD-0015", "priority": "P0",
             "module": "用户中心"},
            {**_VALID_CASE, "case_id": "TM-VD-0016", "priority": "P1",
             "module": "用户中心"},
        ]

        matched = DataDriver.filter_cases(cases, priority="P1")

        assert len(matched) == 1
        assert matched[0]["case_id"] == "TM-VD-0016"

    def test_tag_no_intersection_filters_out(self) -> None:
        """
        标签无交集的用例被跳过（行225），且 tags=None 不炸（行223 兜底）

        调用方自造字典绕过 load_cases 规范化时 tags 可能是 None，
        `set(None)` 会抛 TypeError 中断整条筛选——本条同时锁住这个
        兜底与"无交集必须过滤掉"两件事。
        """
        cases = [
            {**_VALID_CASE, "case_id": "TM-VD-0017", "priority": "P1",
             "tags": ["smoke"]},
            {**_VALID_CASE, "case_id": "TM-VD-0018", "priority": "P1",
             "tags": None},
        ]

        matched = DataDriver.filter_cases(cases, tags="regression")

        assert matched == [], "无交集与tags=None都应被过滤掉而非报错"


# ===========================================================================
# F组: 标量归一化与剩余校验分支
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("data_driver 标量归一化与剩余校验")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestDataDriverScalarAndRemaining:
    """bool 归一 / 非法 priority / Excel 空行跳过"""

    def test_bool_cells_normalized_to_text(self, tmp_path: Path) -> None:
        """
        Excel 的 bool 单元格归一为 "True"/"False"（行79）

        归一顺序刻意把 bool 放在 int 之前（bool 是 int 子类），否则
        Excel 里勾选的 TRUE 会变成 "1"，与用户看到的显示值对不上。
        本条锁住这个顺序决策。
        """
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["case_id", "name", "module", "priority", "tags"])
        sheet.append(["TM-VD-0019", "布尔单元格", "用户中心", "P1", True])
        sheet.append(["TM-VD-0022", "布尔单元格假", "用户中心", "P1", False])
        real_excel = tmp_path / "cases.xlsx"
        workbook.save(real_excel)

        loaded = DataDriver.load_cases(real_excel)

        assert loaded[0]["tags"] == ["True"], "bool True 应归一为字符串 'True'"
        assert loaded[1]["tags"] == ["False"], "bool False 应归一为字符串 'False'"

    def test_invalid_priority_rejected(self, tmp_path: Path) -> None:
        """
        非法 priority 值报错并列出合法取值（行435）

        大小写容错（P1/p1 均可）但取值必须是 P0-P3：写成 P9 或 HIGH 的
        用例进了库会让"按优先级筛选用例"静默漏掉它们。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [{**_VALID_CASE, "priority": "P9"}],
        )

        with pytest.raises(DataDriverError) as excinfo:
            DataDriver.load_cases(yaml_file)

        message = str(excinfo.value)
        assert "'priority'非法" in message
        assert "P0" in message, "错误信息应列出合法取值，便于自查"

    def test_priority_lowercase_is_normalized(self, tmp_path: Path) -> None:
        """
        小写 priority 归一为大写（正向对照）

        证明上一条拦的是"取值非法"而非"大小写"。
        """
        yaml_file = _write_yaml(
            tmp_path / "cases.yaml",
            [{**_VALID_CASE, "priority": "p1"}],
        )

        loaded = DataDriver.load_cases(yaml_file)

        assert loaded[0]["priority"] == "P1"

    def test_excel_blank_rows_skipped(self, tmp_path: Path) -> None:
        """
        Excel 中整行为空的行被跳过（行358-359）

        用户从别的表格粘贴数据常带一串空行，不跳过的话这些行会变成
        缺 case_id 的用例并在校验环节报出定位信息，把"有 3 条用例"
        说成"第 2/5/8 条用例必填字段缺失"，排障方向被带偏。
        """
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["case_id", "name", "module", "priority"])
        sheet.append(["TM-VD-0023", "第一行有效数据", "用户中心", "P1"])
        sheet.append([None, None, None, None])
        sheet.append(["   ", "", "  ", None])
        sheet.append(["TM-VD-0024", "空行之后的数据", "订单中心", "P2"])
        real_excel = tmp_path / "cases.xlsx"
        workbook.save(real_excel)

        loaded = DataDriver.load_cases(real_excel)

        assert len(loaded) == 2, "两个空行应被跳过，只剩 2 条有效用例"
        assert [item["case_id"] for item in loaded] == [
            "TM-VD-0023",
            "TM-VD-0024",
        ]
