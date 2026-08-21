"""
pytest全局配置模块（tests/conftest.py）

核心职责:
    1. 全局fixture:
       - api_server        本地模拟API服务（随机端口，会话级共享，零外部依赖）
       - http_client       HTTP统一客户端（绑定模拟服务，连接池会话级复用）
       - case_trace_logger 用例级trace_id追踪日志（autouse自动生效）
    2. 日志钩子:
       - pytest_configure    会话初始化（Loguru全局配置）
       - pytest_sessionstart Allure环境信息写入
       - pytest_sessionfinish 会话统计汇总日志
    3. 失败自动记录:
       - pytest_runtest_makereport钩子采集用例结果，失败时自动输出
         ERROR日志（含trace_id与失败堆栈）并附加Allure失败详情附件

设计说明:
    Demo用例默认打向conftest内置的本地Flask模拟服务（模拟典型业务后端:
    登录认证/用户查询/健康检查三类接口），保证pytest开箱即跑、可离线重复执行;
    真实被测服务通过.env的TM_BASE_URL配置，由后续阶段用例按需接入。
"""

import sys
import threading
import time
from pathlib import Path

import allure
import pytest
import yaml
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

# ---------------------------------------------------------------------------
# 路径兜底: 确保项目根目录在sys.path中（兼容从任意目录启动pytest的场景）
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.env_manager import env_manager  # noqa: E402 (路径兜底后导入)
from src.common.http_client import HttpClient  # noqa: E402
from src.common.logger import LogManager  # noqa: E402

logger = LogManager.get_logger()

# YAML测试数据根目录
TESTDATA_YAML_DIR = PROJECT_ROOT / "testdata" / "yaml"


# ===========================================================================
# 本地模拟API服务
# ===========================================================================
def _create_mock_app() -> Flask:
    """
    创建本地模拟API应用（Flask）

    模拟典型业务后端三类接口，供Demo用例验证框架完整链路:
        - POST /api/login        用户登录（账密正确签发Token）
        - GET  /api/users/<id>   用户信息查询（需Bearer Token认证）
        - GET  /api/ping         服务健康检查

    参数:
        无

    返回:
        Flask: 配置完成的Flask应用实例
    """
    app = Flask("testmatrix_mock_api")
    # 中文消息原样返回（默认jsonify会转义为\\uXXXX，不利于日志与报告可读性）
    app.json.ensure_ascii = False

    # 模拟用户数据库（用户名 -> 用户信息）
    mock_users = {
        "admin": {
            "password": "123456",
            "user_id": 1,
            "role": "admin",
            "email": "admin@testmatrix.com",
        },
        "tester": {
            "password": "test123",
            "user_id": 2,
            "role": "tester",
            "email": "tester@testmatrix.com",
        },
    }
    # 已签发的有效Token集合（登录成功写入，查询接口校验）
    valid_tokens = set()

    @app.post("/api/login")
    def login():
        """模拟登录接口: 账密正确签发Token，错误返回业务码"""
        data = request.get_json(silent=True) or {}
        username = data.get("username", "")
        password = data.get("password", "")
        if not username or not password:
            # 参数缺失: HTTP 400 + 业务码1001
            return jsonify({"code": 1001, "msg": "用户名或密码参数缺失"}), 400
        user = mock_users.get(username)
        if user is None or user["password"] != password:
            # 账密错误: HTTP 200 + 业务码1002（体现"HTTP成功不等于业务成功"校验点）
            return jsonify({"code": 1002, "msg": "用户名或密码错误"})
        token = f"tm-token-{username}-{len(valid_tokens) + 1:04d}"
        valid_tokens.add(token)
        return jsonify({
            "code": 0,
            "msg": "success",
            "data": {"token": token, "username": username, "role": user["role"]},
        })

    @app.get("/api/users/<int:user_id>")
    def get_user(user_id):
        """模拟用户查询接口: Bearer Token认证通过后返回用户信息"""
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token not in valid_tokens:
            return jsonify({"code": 2001, "msg": "未授权或令牌无效"}), 401
        for username, info in mock_users.items():
            if info["user_id"] == user_id:
                return jsonify({
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "user_id": user_id,
                        "username": username,
                        "role": info["role"],
                        "email": info["email"],
                    },
                })
        return jsonify({"code": 2002, "msg": "用户不存在"})

    @app.get("/api/ping")
    def ping():
        """模拟健康检查接口: 返回服务存活标识与自定义版本响应头"""
        response = jsonify({
            "code": 0,
            "msg": "pong",
            "data": {"service": "testmatrix-mock-api", "version": "1.0.0"},
        })
        response.headers["X-Service-Version"] = "1.0.0"
        return response

    return app


# ===========================================================================
# 数据驱动支撑
# ===========================================================================
def load_yaml_data(filename: str) -> dict:
    """
    加载YAML测试数据文件（数据驱动统一入口）

    参数:
        filename (str): testdata/yaml/目录下的文件名，如 api_login_data.yaml

    返回:
        dict: YAML解析后的字典数据

    异常:
        FileNotFoundError: 数据文件不存在时抛出（附当前可用文件列表提示）
        ValueError: YAML格式解析失败或顶层结构非字典时抛出
    """
    file_path = TESTDATA_YAML_DIR / filename
    if not file_path.exists():
        available = [item.name for item in TESTDATA_YAML_DIR.glob("*.yaml")]
        raise FileNotFoundError(
            f"测试数据文件不存在: {file_path}，当前可用文件: {available}"
        )
    try:
        with open(file_path, encoding="utf-8") as file_handle:
            data = yaml.safe_load(file_handle)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML解析失败: {file_path}，错误详情: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"YAML顶层结构必须为字典: {file_path}")
    logger.debug(f"测试数据加载完成 | {file_path} | 顶层键: {list(data.keys())}")
    return data


# ===========================================================================
# 全局fixture
# ===========================================================================
@pytest.fixture(scope="session")
def api_server():
    """
    本地模拟API服务fixture（会话级）

    在本机随机端口启动Flask模拟服务，整个测试会话共享同一实例，
    会话结束后自动关闭，保证用例零外部依赖、可离线重复执行。

    参数:
        无

    返回:
        str: 模拟服务基础地址，如 http://127.0.0.1:54321
    """
    # port=0表示由操作系统分配随机可用端口，避免端口冲突
    server = make_server("127.0.0.1", 0, _create_mock_app())
    port = server.server_port
    thread = threading.Thread(
        target=server.serve_forever, name="testmatrix-mock-api", daemon=True
    )
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    logger.info(f"本地模拟API服务已启动 | {base_url}")
    yield base_url
    server.shutdown()
    server.server_close()
    logger.info("本地模拟API服务已关闭")


@pytest.fixture(scope="session")
def http_client(api_server):
    """
    HTTP统一客户端fixture（会话级）

    基于本地模拟服务构建HttpClient，连接池随会话复用，
    会话结束后关闭释放资源。

    参数:
        api_server (str): 模拟服务基础地址（由api_server fixture提供）

    返回:
        HttpClient: HTTP统一客户端实例
    """
    client = HttpClient(base_url=api_server, timeout=10, max_retries=1)
    yield client
    client.close()


@pytest.fixture(autouse=True)
def case_trace_logger(request):
    """
    用例级追踪日志fixture（autouse，全用例自动生效）

    每条用例执行前后输出绑定trace_id的边界日志，
    配合日志文件实现单用例全链路追踪。

    参数:
        request (pytest.FixtureRequest): 当前用例请求对象

    返回:
        Generator: yield前后分别输出用例开始/结束日志
    """
    case_logger = LogManager.bind_trace_id(request.node.nodeid)
    case_logger.info(f"用例开始 >>> {request.node.name}")
    start_time = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start_time
    case_logger.info(f"用例结束 <<< {request.node.name} | 耗时: {elapsed:.3f}s")


# ===========================================================================
# 日志与报告钩子
# ===========================================================================
def pytest_configure(config):
    """
    会话初始化钩子: 完成Loguru全局配置

    参数:
        config (pytest.Config): pytest配置对象

    返回:
        无
    """
    LogManager.setup(
        log_level=env_manager.log_level,
        log_dir=env_manager.log_dir,
    )
    session_logger = LogManager.get_logger()
    session_logger.info("=" * 80)
    session_logger.info(
        f"TestMatrix测试会话启动 | 环境: {env_manager.current_env} | "
        f"日志级别: {env_manager.log_level}"
    )


def pytest_sessionstart(session):
    """
    会话启动钩子: 写入Allure报告环境信息文件

    在Allure结果目录生成environment.properties，报告Environment栏
    展示项目名、运行环境、Python版本等元信息。

    参数:
        session (pytest.Session): 测试会话对象

    返回:
        无
    """
    allure_dir = session.config.getoption("--alluredir", default=None)
    if not allure_dir:
        return
    allure_path = Path(allure_dir)
    allure_path.mkdir(parents=True, exist_ok=True)
    content = (
        f"Project=TestMatrix\n"
        f"Environment={env_manager.current_env}\n"
        f"TargetService=local_mock_api\n"
        f"Python={sys.version.split()[0]}\n"
    )
    (allure_path / "environment.properties").write_text(content, encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    用例执行结果采集钩子: 失败自动日志记录

    对call阶段（真正执行用例体的阶段）的结果进行采集:
        - 失败: ERROR级日志（含trace_id、耗时、失败堆栈），并尝试附加Allure失败详情
        - 跳过: WARNING级日志
        - 通过: DEBUG级日志

    参数:
        item (pytest.Item): 当前用例对象
        call (pytest.CallInfo): 本次调用信息

    返回:
        无（hookwrapper模式，yield后处理结果）
    """
    outcome = yield
    report = outcome.get_result()

    # setup/teardown阶段失败由error报表体现，此处仅采集call阶段
    if report.when != "call":
        return

    trace_logger = LogManager.bind_trace_id(item.nodeid)
    if report.failed:
        trace_logger.error(
            f"用例执行失败 | 耗时: {report.duration:.3f}s\n"
            f"失败详情:\n{report.longrepr}"
        )
        # 失败堆栈附加到Allure报告（附件失败不阻断测试主流程）
        try:
            allure.attach(
                body=str(report.longrepr),
                name="失败详情-自动附加",
                attachment_type=allure.attachment_type.TEXT,
            )
        except Exception:  # noqa: BLE001 附件为增强能力，失败仅降级为警告
            trace_logger.warning("失败详情附加Allure附件未成功（不影响测试结果）")
    elif report.skipped:
        trace_logger.warning(f"用例跳过 | {report.longrepr}")
    else:
        trace_logger.debug(f"用例执行通过 | 耗时: {report.duration:.3f}s")


def pytest_sessionfinish(session, exitstatus):
    """
    会话结束钩子: 输出汇总统计日志

    参数:
        session (pytest.Session): 测试会话对象
        exitstatus (int): pytest退出码（0=全部通过，1=存在失败）

    返回:
        无
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter else {}
    summary = (
        f"测试会话结束 | 通过: {len(stats.get('passed', []))} | "
        f"失败: {len(stats.get('failed', []))} | "
        f"错误: {len(stats.get('error', []))} | "
        f"跳过: {len(stats.get('skipped', []))} | "
        f"重跑: {len(stats.get('rerun', []))} | 退出码: {exitstatus}"
    )
    session_logger = LogManager.get_logger()
    session_logger.info(summary)
    session_logger.info("=" * 80)
