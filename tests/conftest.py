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

import os
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


@pytest.fixture(autouse=True)
def _disable_real_notification_channels(monkeypatch):
    """
    全局禁用真实通知渠道fixture（autouse，全用例自动生效）

    纵深防御（Day27）: 批次执行编排已接入自动通知（_execute_batch_async
    终态旁路调用notify_execution_result），任何测试路径触及该链路时，
    通过环境变量保证零真实邮件/零真实网络连接:
        - TM_EMAIL_ENABLED=false   邮件渠道send内部直接跳过
        - TM_WECHAT_ENABLED=false  企微渠道send内部直接跳过
        - TM_NOTIFY_MAX_RETRIES=0  失败重试次数归零（消除退避等待）

    实现说明: env_manager.get实时读os.getenv，load_dotenv(override=False)
    不会覆盖已存在的环境变量，monkeypatch.setenv必然生效；测试内自行
    setenv同名变量时后写覆盖（既有通知测试均mock env_manager.get，
    与本fixture零冲突）。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Generator: yield无数据，环境变量仅在本用例作用域内生效
    """
    monkeypatch.setenv("TM_EMAIL_ENABLED", "false")
    monkeypatch.setenv("TM_WECHAT_ENABLED", "false")
    monkeypatch.setenv("TM_NOTIFY_MAX_RETRIES", "0")
    yield


# ===========================================================================
# 日志与报告钩子
# ===========================================================================
def pytest_configure(config):
    """
    会话初始化钩子: 完成Loguru全局配置 + 旁路捕获终态摘要（仅CI生效）

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
    _install_summary_capture(config)


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

    # 注解回传覆盖 setup/call/teardown 三个阶段：setup/teardown 的 ERROR
    # 同样会让 pytest exit 1，但不会走到下面的 call 阶段采集分支
    if report.failed:
        _emit_github_failure_annotation(item, report)

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


def _emit_github_failure_annotation(item, report) -> None:
    """
    测试失败时向 GitHub Actions 发出 error 注解（仅CI环境生效）

    背景: CI 日志下载接口需鉴权，未认证环境只能读到 annotation。CI 在
    Linux runner 上出现"本地全绿、CI 判红"且无法取回失败用例名时，
    靠注解回传失败证据是唯一可靠手段。

    实现: GitHub workflow command 格式 `::error title=..::message`，
    消息中的换行编码为 %0A（否则注解被截断在第一行）。
    必须直写 sys.__stdout__ 绕过 pytest 的输出捕获，否则注解不会出现在
    runner 日志里。

    非 CI 环境（GITHUB_ACTIONS 未设置）直接返回，无任何副作用。

    参数:
        item (pytest.Item): 失败用例对象
        report (pytest.TestReport): 失败报告

    返回:
        无
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        raw = str(report.longrepr)
        # 只保留末尾的错误摘要行，去掉冗长的框架堆栈噪声
        tail_lines = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith(("self.", "return "))
        ]
        summary = " | ".join(tail_lines[-6:])[:900]
        message = f"{item.nodeid} :: {summary}"
        _write_github_annotation("error", f"pytest-failed-{report.when}", message)
    except Exception:  # noqa: BLE001 注解是诊断辅助，失败不得影响测试结果
        pass


def _install_summary_capture(config) -> None:
    """
    旁路捕获 pytest 终态摘要输出（仅CI环境生效）

    存在性: pytest 的失败出口不止"用例失败"一种——
      1) 用例/夹具失败 -> makereport 失败事件（_emit_github_failure_annotation 覆盖）
      2) --cov-fail-under 未达标 -> 全部用例通过但 exit 1（无任何失败事件）
      3) collection 阶段导入失败 -> 直接 exit，无 makereport
    后两类逐用例钩子完全捕获不到。因此改为旁路 reporter.write_line，
    把终态摘要里与失败/覆盖率相关的行统一回传。

    参数:
        config (pytest.Config): pytest配置对象

    返回:
        无
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    # 必须挂在 _tw.line 而非 reporter.write_line: pytest 的
    # short_test_summary 用 self._tw.line 直接写，绕过 write_line，
    # 挂在 write_line 上会漏掉全部 FAILED 行
    terminal_writer = getattr(reporter, "_tw", None)
    if terminal_writer is None:
        return
    original_line = terminal_writer.line
    captured: list = []

    def _tee_line(s, **markup) -> None:
        """原始写行不变，同时把行收进缓冲区"""
        captured.append(s)
        original_line(s, **markup)

    terminal_writer.line = _tee_line
    config._tm_captured_summary_lines = captured


def _write_github_annotation(level: str, title: str, message: str) -> None:
    """
    输出一条 GitHub Actions workflow command 注解（仅CI环境生效）

    参数:
        level (str): 注解级别，error/warning/notice
        title (str): 注解标题
        message (str): 注解正文（内部完成转义）

    返回:
        无
    """
    safe = (
        message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    )
    sys.__stdout__.write(f"::{level} title={title}::{safe}\n")
    sys.__stdout__.flush()


@pytest.hookimpl(trylast=True)
def _emit_github_diagnosis_annotation(config, exitstatus) -> None:
    """
    会话最末: 把失败证据合并为**单条**注解回传（仅CI环境生效）

    挂在 pytest_sessionfinish 而非 pytest_terminal_summary:
    short test summary 由主 reporter 的 pytest_terminal_summary 打印，
    conftest 的同名钩子注册更晚、反而先于它执行，此时缓冲区还是空的。
    sessionfinish 是最后一个钩子，一定在全部摘要输出之后。

    为什么只发一条: GitHub 对 workflow command 生成的注解有数量上限，
    逐用例各发一条会被截断丢弃（实测：逐用例注解全部丢失，仅幸存一条），
    合并为单条可一次拿到全部证据。

    参数:
        config (pytest.Config): pytest配置对象
        exitstatus (int): pytest退出码

    返回:
        无
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        stats = getattr(reporter, "stats", {}) if reporter else {}
        parts = [
            f"{key}={len(stats.get(key, []))}"
            for key in ("passed", "failed", "error", "skipped", "rerun")
        ]
        chunks = [f"exitstatus={exitstatus} " + " ".join(parts)]

        # 直接从 reporter.stats 取失败/错误报告（TestReport 自带 nodeid 与
        # longrepr），比解析终端输出可靠：short test summary 走 _tw.line
        # 直接写，绕过多层包装，文本解析易漏。
        failed_reports = list(stats.get("failed", [])) + list(
            stats.get("error", [])
        )
        for report in failed_reports[:10]:
            nodeid = getattr(report, "nodeid", "<unknown>")
            longrepr = str(getattr(report, "longrepr", "") or "")
            tail = [ln.strip() for ln in longrepr.splitlines() if ln.strip()]
            chunks.append(f"FAILED[{nodeid}] :: " + " | ".join(tail[-5:])[:600])

        # 覆盖率门禁未达标时没有任何失败报告，需单独识别
        lines = getattr(config, "_tm_captured_summary_lines", None) or []
        for line in lines:
            if "Required test coverage" in line:
                chunks.append(line.strip()[:300])

        _write_github_annotation(
            "error", "pytest-diagnosis", " || ".join(chunks)[:3500]
        )
    except Exception:  # noqa: BLE001 注解是诊断辅助，失败不得影响测试结果
        pass


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
    # 临时诊断: 把失败证据回传为GitHub注解（仅CI生效，修绿后随本段一并还原）
    _emit_github_diagnosis_annotation(session.config, exitstatus)
