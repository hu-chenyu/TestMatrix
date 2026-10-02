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
