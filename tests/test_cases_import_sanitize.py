"""
导入接口错误消息路径脱敏测试（Day42-coverage 子目标A）

背景: 导入失败时 CaseManager 抛出的错误消息含服务端临时目录绝对路径与
    操作系统账户名（如 C:\\Users\\<账户名>\\AppData\\Local\\Temp\\tm_case_import_xxx\\a.yaml），
    该消息经前端 toast 直接展示给终端用户，属信息泄露。
    src/web/routes/cases.py 的 _sanitize_error_message 负责把绝对路径
    收敛为 basename，同时保留错误类型/文件名/行列号等诊断信息。

覆盖两层:
    1. 单元层: _sanitize_error_message 对 Windows/Unix 路径的脱敏效果
    2. 接口层: POST /api/cases/import 端到端验证——
       不支持后缀与畸形YAML两种失败路径的响应 message 均不含服务端路径特征；
       正常导入不受影响（回归保障）
"""

import io

import allure
import pytest
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes.cases import _sanitize_error_message

# 服务端路径泄露特征：命中任一即视为未脱敏
LEAK_MARKERS = (
    "C:" + "\\",           # Windows 盘符
    "/tmp/",
    "/home/",
    "/private/",
    "Users",
    "AppData",
    "Local" + "\\" + "Temp",
    "tm_case_import_",
)

# 构造真实形态的 Windows 泄露样本（用变量拼接，避免硬编码盘符路径字面量）
_WIN_DIR = (
    "C:" + "\\Users" + "\\ci_user"
    + "\\AppData" + "\\Local" + "\\Temp" + "\\tm_case_import_abc"
)
_WIN_FILE = _WIN_DIR + "\\bad.yaml"
_WIN_LEAK = (
    "用例数据加载失败: [数据文件 " + _WIN_FILE + "] "
    "YAML语法解析失败: while parsing a block mapping\n"
    '  in "' + _WIN_FILE + '", line 1, column 3\n'
    "expected <block end>, but found '<scalar>'\n"
    '  in "' + _WIN_FILE + '", line 2, column 23'
)


@pytest.fixture()
def import_client(monkeypatch, tmp_path):
    """
    导入接口测试客户端fixture（临时SQLite空库）

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture
        tmp_path (Path): pytest临时目录fixture

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端，前后重置引擎单例
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "cases_sanitize_api.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield create_app("test").test_client()
    DatabaseSession.reset()


def assert_no_leak(message: str) -> None:
    """断言错误消息不含任何服务端路径特征"""
    for marker in LEAK_MARKERS:
        assert marker not in message, f"错误消息泄露路径特征 {marker!r}: {message}"


# ===========================================================================
# 1. 单元层: _sanitize_error_message
# ===========================================================================
@allure.feature("导入错误脱敏")
class TestSanitizeErrorMessage:
    """脱敏函数的路径识别与诊断信息保留"""

    @allure.story("Windows 临时目录路径收敛为文件名")
    def test_windows_path_reduced_to_basename(self):
        out = _sanitize_error_message(_WIN_LEAK)
        assert_no_leak(out)
        assert "bad.yaml" in out

    @allure.story("脱敏后保留错误类型/行列号等诊断信息")
    def test_diagnostic_info_preserved(self):
        out = _sanitize_error_message(_WIN_LEAK)
        assert "YAML语法解析失败" in out
        assert "while parsing a block mapping" in out
        assert "line 1, column 3" in out
        assert "line 2, column 23" in out

    @allure.story("Unix /tmp 路径收敛为文件名")
    def test_unix_tmp_path(self):
        out = _sanitize_error_message("数据文件 /tmp/tm_case_import_x/deep/a.yaml 解析失败")
        assert_no_leak(out)
        assert "a.yaml" in out
        assert "解析失败" in out

    @allure.story("Unix /home 路径收敛为文件名")
    def test_unix_home_path(self):
        out = _sanitize_error_message("open /home/ci/project/data.yaml failed")
        assert_no_leak(out)
        assert "data.yaml" in out

    @allure.story("macOS /private 路径收敛为文件名")
    def test_macos_private_path(self):
        out = _sanitize_error_message("bad file /private/var/folders/xy/tm_case_import_1/a.yaml")
        assert_no_leak(out)
        assert "a.yaml" in out

    @allure.story("无路径的消息原样返回，不做无谓改写")
    def test_message_without_path_unchanged(self):
        raw = "用例数据加载失败: 列表缺少 case_id 字段"
        assert _sanitize_error_message(raw) == raw

    @allure.story("空消息安全返回")
    def test_empty_message(self):
        assert _sanitize_error_message("") == ""

    @allure.story("同一条消息中的多处路径全部被脱敏")
    def test_multiple_paths_all_sanitized(self):
        out = _sanitize_error_message(
            "first /tmp/tm_case_import_1/a.yaml then "
            "/tmp/tm_case_import_2/b.yaml failed"
        )
        assert_no_leak(out)
        assert "a.yaml" in out
        assert "b.yaml" in out

    @allure.story("Unix 常见根目录前缀全部覆盖")
    @pytest.mark.parametrize(
        "prefix",
        ["/etc", "/srv", "/data", "/mnt", "/www"],
        ids=["etc", "srv", "data", "mnt", "www"],
    )
    def test_unix_common_prefixes_covered(self, prefix):
        """
        etc / srv / data / mnt / www 五个前缀必须纳入脱敏（V2-P3-6）

        修复前前缀元组只有 tmp/home/Users/var/opt/usr/root/private/
        Applications，容器化部署常见的 /etc、/data、/mnt 全部零匹配，
        路径原样回显。已实证 `/data/uploads/secret/cases.yaml` 与
        `/etc/testmatrix/upload/cases.yaml` 均未脱敏。
        """
        out = _sanitize_error_message(
            f"数据文件 {prefix}/uploads/secret/cases.yaml 解析失败"
        )

        assert f"{prefix}/uploads" not in out, f"{prefix} 前缀未脱敏: {out}"
        assert "secret" not in out, f"{prefix} 下的子目录未脱敏: {out}"
        assert "cases.yaml" in out, f"文件名应保留: {out}"

    @allure.story("原有 Unix 前缀不受扩展影响")
    @pytest.mark.parametrize(
        "prefix",
        ["/tmp", "/home", "/var", "/opt", "/usr", "/root", "/private"],
        ids=["tmp", "home", "var", "opt", "usr", "root", "private"],
    )
    def test_existing_unix_prefixes_still_covered(self, prefix):
        """扩充分缀后原有前缀仍正常脱敏（防前缀表被改坏）"""
        out = _sanitize_error_message(
            f"数据文件 {prefix}/tm_case_import_x/a.yaml 解析失败"
        )

        assert "tm_case_import_x" not in out, f"{prefix} 前缀未脱敏: {out}"
        assert "a.yaml" in out

    @allure.story("同一条消息中的两处 Windows 路径都被脱敏")
    def test_two_windows_paths_in_one_message(self):
        """
        同一条消息里的**两条** Windows 路径都必须脱敏（v5 修复的能力回退）

        回归点: v3 给 _PATH_CHAR 加了空格排除，却漏改两个 name 类。
        basename 组因允许空格而吞下 `a.yaml in D:`（连同下一个盘符
        前缀），re.sub 非重叠扫描从 `D:` 之后继续，剩余 `\\y\\b.ini`
        不再以 `[A-Za-z]:\\` 开头 → 零匹配 → 第二条路径原样回显。
        实证（v3）: `see C:\\x\\a.yaml in D:\\y\\b.ini` -> `see a.yaml in D:\\y\\b.ini`
        而 v2 是 `see a.yaml in b.ini`，即能力回退。
        既有两条"多处路径"用例都用 POSIX 路径，恰好绕开了这个洞。
        """
        out = _sanitize_error_message(
            "see " + "C:" + "\\x\\a.yaml in D:" + "\\y\\b.ini"
        )

        assert "D:" not in out, f"第二个盘符前缀泄露: {out}"
        assert "b.ini" in out, f"第二个文件名应保留: {out}"
        assert "a.yaml" in out, f"第一个文件名应保留: {out}"

    @allure.story("多空格目录名收敛（账户名不泄露）")
    def test_multi_space_directory_name(self):
        """
        目录名含**连续多个空格**时也必须完整收敛（v5 修复）

        回归点: v3 的空格前瞻只支持单个空格，`John  Smith` 这类多空格
        目录名会让前瞻失败、匹配在此终止，账户名与剩余全路径原样回显。
        放宽为 {1,} 后收敛，且散文断开能力不受影响（见下一条用例）。
        """
        out = _sanitize_error_message(
            "open C:" + "\\Users" + "\\John  Smith" + "\\secret.yaml failed"
        )

        assert "John" not in out, f"多空格账户名泄露: {out}"
        assert "secret.yaml" in out, f"文件名应保留: {out}"

    @allure.story("多空格放宽后散文断开能力不回退")
    def test_multi_space_relaxation_keeps_prose_break(self):
        """
        放宽为 {1,} 后，"散文 + 新路径"仍必须在散文处断开

        这是放宽的代价检查: 若一改成 ` *` 之类的前瞻，`a.yaml then /tmp/...`
        会被整体匹配，第二条路径的目录结构又会被吞进去。
        """
        out = _sanitize_error_message(
            "first /tmp/tm_case_import_1/a.yaml then /tmp/tm_case_import_2/b.yaml failed"
        )

        assert "tm_case_import_1" not in out, f"第一条目录泄露: {out}"
        assert "tm_case_import_2" not in out, f"第二条目录泄露: {out}"
        assert "a.yaml" in out and "b.yaml" in out

    # ------------------------------------------------------------------
    # v3 新增: 含空格路径（v2 审查 V2-P1-2）
    #
    # 修复前字符类排除 \s，含空格的路径整条匹配失败、**原样回显**；
    # 更隐蔽的是部分匹配会把安全的 `C:\Users\` 前缀打码、留下操作系统
    # 账户名与其后全部路径——看起来生效了，实际泄露更多。
    # 既有样本用无空格的 ci_user，恰好绕开了这个洞。
    # ------------------------------------------------------------------

    @allure.story("Windows 含空格账户名收敛为文件名")
    def test_windows_path_with_spaces_reduced_to_basename(self):
        """账户名含空格（John Smith）时也必须完整脱敏"""
        spaced_dir = (
            "C:" + "\\Users" + "\\John Smith"
            + "\\AppData" + "\\Local" + "\\Temp" + "\\tm_case_import_abc"
        )
        spaced_file = spaced_dir + "\\bad.yaml"
        out = _sanitize_error_message(
            "用例数据加载失败: [数据文件 " + spaced_file + "] "
            "YAML语法解析失败: while parsing a block mapping"
        )

        assert "John Smith" not in out, f"含空格的账户名泄露: {out}"
        assert "tm_case_import_abc" not in out, f"临时目录名泄露: {out}"
        assert "C:" + "\\" not in out, f"盘符泄露: {out}"
        assert "AppData" not in out, f"中间目录泄露: {out}"
        assert "bad.yaml" in out, f"文件名应保留: {out}"
        assert "YAML语法解析失败" in out, "错误类型应保留"

    @allure.story("Windows Program Files 路径收敛为文件名")
    def test_windows_program_files_path_reduced_to_basename(self):
        """Program Files 是最典型的含空格真实路径，修复前完全零匹配"""
        program_files = (
            "C:" + "\\Program Files" + "\\TestMatrix" + "\\data" + "\\cases.yaml"
        )
        out = _sanitize_error_message("数据文件 " + program_files + " 解析失败")

        assert "Program Files" not in out, f"含空格目录泄露: {out}"
        assert "TestMatrix" not in out, f"中间目录泄露: {out}"
        assert "C:" + "\\" not in out, f"盘符泄露: {out}"
        assert "cases.yaml" in out, f"文件名应保留: {out}"
        assert "解析失败" in out

    @allure.story("Windows 无目录短路径收敛为文件名")
    def test_windows_short_path_reduced_to_basename(self):
        """C:\\a.yaml 这类无目录短路径也必须收敛（目录段是可选的）"""
        out = _sanitize_error_message("数据文件 C:" + "\\bad.yaml 解析失败")

        assert "C:" + "\\" not in out, f"盘符泄露: {out}"
        assert "bad.yaml" in out, f"文件名应保留: {out}"

    @allure.story("含空格路径在引号内正确终止")
    def test_spaced_path_stops_at_quote(self):
        """路径后紧跟引号与行列号时，诊断信息必须原样保留"""
        spaced_file = (
            "C:" + "\\Users" + "\\John Smith" + "\\AppData" + "\\t" + "\\a.yaml"
        )
        out = _sanitize_error_message(
            '  in "' + spaced_file + '", line 1, column 3'
        )

        assert "John Smith" not in out, f"含空格账户名泄露: {out}"
        assert "a.yaml" in out, f"文件名应保留: {out}"
        assert "line 1, column 3" in out, "行列号等诊断信息必须原样保留"

    @allure.story("Unix 含单空格目录收敛为文件名")
    def test_unix_path_with_spaces_reduced_to_basename(self):
        """/tmp/my case/deep/a.yaml 这类含空格 Unix 路径也必须脱敏"""
        out = _sanitize_error_message(
            "数据文件 /tmp/my case/deep/a.yaml 解析失败"
        )

        assert "my case" not in out, f"含空格目录泄露: {out}"
        assert "a.yaml" in out, f"文件名应保留: {out}"

    @allure.story("多处路径含空格时仍逐条脱敏")
    def test_multiple_spaced_paths_all_sanitized(self):
        """
        允许空格不得导致贪婪匹配跨过散文吞掉下一条路径

        这是本次修复最容易引入的回归: 空格一旦无条件放行，
        `/tmp/x/a.yaml then /tmp/y/b.yaml` 会被整体匹配、只剩 b.yaml。

        目录名用**单空格**（`tm case`）——多条空格目录与"散文后接新路径"
        在正则层面无法区分，属已记录的已知限制，不在此构造。
        """
        out = _sanitize_error_message(
            "first /tmp/tm case/a.yaml then /tmp/tm other/b.yaml failed"
        )

        assert "tm case" not in out, f"第一条路径目录泄露: {out}"
        assert "tm other" not in out, f"第二条路径目录泄露: {out}"
        assert "a.yaml" in out, f"第一个文件名应保留: {out}"
        assert "b.yaml" in out, f"第二个文件名应保留: {out}"
        assert "failed" in out


# ===========================================================================
# 2. 接口层: POST /api/cases/import
# ===========================================================================
@allure.feature("导入接口脱敏")
class TestImportEndpointSanitization:
    """端到端验证导入失败路径不泄露服务端绝对路径"""

    @allure.story("不支持的后缀: 消息仅含后缀与白名单，无路径")
    def test_unsupported_suffix_message_clean(self, import_client):
        data = {"file": (io.BytesIO(b"x"), "notes.txt")}
        resp = import_client.post(
            "/api/cases/import", data=data, content_type="multipart/form-data"
        )
        assert resp.status_code == 400
        message = resp.get_json()["message"]
        assert_no_leak(message)
        assert ".txt" in message
        assert ".yaml/.yml/.xlsx" in message

    @allure.story("上传名含路径分隔符时只回显 basename")
    def test_traversal_filename_reduced_to_basename(self, import_client):
        data = {"file": (io.BytesIO(b"x"), "../../etc/passwd.txt")}
        resp = import_client.post(
            "/api/cases/import", data=data, content_type="multipart/form-data"
        )
        assert resp.status_code == 400
        message = resp.get_json()["message"]
        assert_no_leak(message)
        assert ".." not in message

    @allure.story("畸形YAML: 解析失败消息不含临时目录绝对路径")
    def test_malformed_yaml_message_clean(self, import_client):
        broken = b"- case_id: A\n   bad_indent: 1\n  - broken\n"
        data = {"file": (io.BytesIO(broken), "broken.yaml")}
        resp = import_client.post(
            "/api/cases/import", data=data, content_type="multipart/form-data"
        )
        assert resp.status_code == 400
        message = resp.get_json()["message"]
        assert_no_leak(message)
        # 脱敏后仍保留可诊断信息：文件名 + 错误类型 + 行列号
        assert "broken.yaml" in message
        assert "line" in message

    @allure.story("回归: 正常YAML导入成功且响应不含路径")
    def test_valid_yaml_import_still_works(self, import_client, tmp_path):
        good = (
            b"- case_id: SAN-001\n"
            b"  name: \xe5\x90\x88\xe6\xb3\x95\xe5\xaf\xbc\xe5\x85\xa5\xe7\x94\xa8\xe4\xbe\x8b\n"
            b"  module: default\n"
            b"  priority: P2\n"
        )
        data = {"file": (io.BytesIO(good), "good.yaml")}
        resp = import_client.post(
            "/api/cases/import", data=data, content_type="multipart/form-data"
        )
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["data"]["inserted"] == 1
        assert payload["data"]["file_name"] == "good.yaml"
        # 入库后可查回，说明脱敏改动未破坏主流程
        listed = import_client.get("/api/cases/?page=1&page_size=10&status=all")
        assert listed.status_code == 200
        case_ids = [c["case_id"] for c in listed.get_json()["data"]["items"]]
        assert "SAN-001" in case_ids

    @allure.story("回归: 重复导入同一文件幂等，不产生重复用例")
    def test_reimport_is_idempotent(self, import_client):
        good = (
            b"- case_id: SAN-002\n"
            b"  name: \xe5\x8c\x95\xe6\x96\x87\xe6\xa1\x88\xe4\xbe\x8b\n"
            b"  module: default\n"
            b"  priority: P3\n"
        )
        for _ in range(2):
            resp = import_client.post(
                "/api/cases/import",
                data={"file": (io.BytesIO(good), "idem.yaml")},
                content_type="multipart/form-data",
            )
            assert resp.status_code == 200
        listed = import_client.get("/api/cases/?page=1&page_size=50&status=all")
        ids = [c["case_id"] for c in listed.get_json()["data"]["items"]]
        assert ids.count("SAN-002") == 1

    @allure.story("未上传文件仍返回原有提示，不受影响")
    def test_missing_file_still_rejected(self, import_client):
        resp = import_client.post(
            "/api/cases/import", data={}, content_type="multipart/form-data"
        )
        assert resp.status_code == 400
        assert "未上传用例数据文件" in resp.get_json()["message"]
