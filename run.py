"""
TestMatrix 一键启动脚本（本地开发 / 开源用户使用）

用途:
    为开源用户提供零配置的 Web 平台启动入口，免去手动设置
    FLASK_APP 环境变量、记忆 flask 命令的成本；启动后浏览器
    打开 /dashboard 即可看到可视化界面。

用法:
    py run.py                  # 默认监听 127.0.0.1:5000
    py run.py --port 8080      # 指定监听端口
    py run.py --host 0.0.0.0   # 允许局域网内其他机器访问
    py run.py --debug          # 开启调试模式（代码改动自动重载）

说明:
    1. 本脚本仅用于本地/开发环境启动，生产环境请用 gunicorn/uwsgi
       等 WSGI 服务器部署；
    2. Redis 缓存与任务队列默认关闭（TM_REDIS_ENABLED /
       TM_TASK_QUEUE_ENABLED 默认 false），平台在纯 SQLite 下
       即可完整运行，无需额外起 Redis 进程。
"""

# argparse: 标准库命令行参数解析器，用于接收 --host/--port/--debug
import argparse
import shutil
from pathlib import Path

# env_manager: 读 .env 配置（load_dotenv 在其模块导入时已执行）
from src.common.env_manager import env_manager

# create_app: Flask 应用工厂，返回已完成蓝图注册/异常处理/钩子装配的应用实例
from src.web.app import create_app

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


def ensure_env_file() -> None:
    """首次启动引导：.env 缺失时从 .env.example 复制默认配置并打印可选功能提示。

    不阻塞启动——基础平台在零配置（纯 SQLite、通知/Redis 全关）下即可运行；
    本函数只负责让新用户知道"有哪些可选开关、对应哪个环境变量"。

    返回:
        无
    """
    if ENV_FILE.exists():
        _warn_enabled_but_misconfigured()
        return
    if ENV_EXAMPLE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        print("+" + "=" * 62 + "+")
        print("|  未检测到 .env，已从 .env.example 创建默认配置             |")
        print("|                                                            |")
        print("|  基础功能（Web/SQLite/模拟执行/CI）零配置即可用，以下为可选项：|")
        print("|  · 企微通知：TM_WECHAT_ENABLED=true + TM_WECHAT_WEBHOOK_URL |")
        print("|  · 邮件通知：TM_EMAIL_ENABLED=true + TM_EMAIL_SMTP_*        |")
        print("|  · Redis缓存：TM_REDIS_ENABLED=true                         |")
        print("|  · Redis队列：TM_TASK_QUEUE_ENABLED=true                    |")
        print("+ " + "-" * 60 + " +")
        print(f"  配置文件：{ENV_FILE}（已加入 .gitignore，不会提交）")
    else:
        print("[警告] .env 与 .env.example 均不存在，将使用全部内置默认值启动。")


def _warn_enabled_but_misconfigured() -> None:
    """开关打开但关键配置为空时打印警告（功能会静默失效）。

    返回:
        无
    """
    if env_manager.get_bool("TM_WECHAT_ENABLED", False) and not env_manager.get(
        "TM_WECHAT_WEBHOOK_URL", ""
    ):
        print("[警告] TM_WECHAT_ENABLED=true 但 TM_WECHAT_WEBHOOK_URL 为空，企微通知将不生效。")
    if env_manager.get_bool("TM_EMAIL_ENABLED", False) and (
        not env_manager.get("TM_EMAIL_SMTP_HOST", "")
        or "example.com" in str(env_manager.get("TM_EMAIL_SMTP_HOST", ""))
    ):
        print("[警告] TM_EMAIL_ENABLED=true 但 SMTP 主机未配置/仍为 example.com，邮件通知将不生效。")


def _warn_non_loopback_binding(host: str) -> None:
    """绑定非本机回环地址时打印显式安全告警（不阻断启动）。

    平台不含任何鉴权机制，docs/API.md 已声明"仅内网/本机使用，禁止暴露
    公网"。默认绑 127.0.0.1 时无外暴露风险；一旦用户显式传入 0.0.0.0
    等对外地址，启动瞬间必须给出醒目提示，避免无意识公网部署。

    参数:
        host (str): --host 实际绑定的监听地址

    返回:
        None
    """
    # 仅回环地址视为安全：127.0.0.1 与 localhost（其余如 0.0.0.0/内网IP均告警）
    loopback_hosts = {"127.0.0.1", "localhost"}
    if host in loopback_hosts:
        return
    print("!" + "=" * 62 + "!")
    print("!  安全告警：当前绑定 " + host)
    print("!  本服务无任何鉴权，仅限可信内网部署，禁止暴露公网！")
    print("!  详见 docs/API.md；本机访问请使用默认值 127.0.0.1")
    print("!" + "=" * 62 + "!")


def parse_args() -> argparse.Namespace:
    """
    解析命令行启动参数

    参数:
        无（从 sys.argv 读取命令行参数）

    返回:
        argparse.Namespace: 解析后的参数对象，含 host/port/debug 三个属性：
            - host: 监听地址，默认 127.0.0.1（仅本机可访问）
            - port: 监听端口，默认 5000
            - debug: 是否开启调试模式，默认 False
    """
    # 创建解析器，description 会显示在 --help 帮助信息顶部
    parser = argparse.ArgumentParser(
        description="TestMatrix 一键启动脚本",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # --host: 绑定的网卡地址；127.0.0.1 仅本机访问，0.0.0.0 对外开放
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（0.0.0.0 表示允许局域网访问）",
    )
    # --port: Web 服务监听端口，注意不要与本机其他服务冲突
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="监听端口",
    )
    # --debug: 开启后 Flask 自动重载代码并显示详细错误页，仅开发时使用
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启调试模式（代码改动自动重载）",
    )
    # 执行解析并返回命名空间对象
    return parser.parse_args()


def main() -> None:
    """
    启动脚本主入口：解析参数 → 创建应用 → 打印访问地址 → 启动 Web 服务

    参数:
        无

    返回:
        无（app.run 为阻塞调用，直到用户按 Ctrl+C 停止）
    """
    # 1. 解析命令行参数
    args = parse_args()

    # 1.5 首次启动 .env 引导（缺失自动复制+提示；开关空配置告警），不阻塞
    ensure_env_file()

    # 2. 通过应用工厂创建 Flask 实例（不传 config_name 时按 TM_ENV 环境变量，
    #    未设置时走默认开发配置）
    app = create_app()

    # 3. 拼接用户实际需要访问的页面地址（前端入口为 /dashboard）
    dashboard_url = f"http://{args.host}:{args.port}/dashboard"

    # 4. 打印启动横幅（启动脚本的标准输出，方便用户直接看到访问地址；
    #    按 Ctrl+C 可停止服务）
    print("=" * 64)
    print("TestMatrix 通用自动化测试效能平台已启动")
    print(f"  前端入口 : {dashboard_url}")
    print(f"  用例管理 : http://{args.host}:{args.port}/cases")
    print(f"  执行记录 : http://{args.host}:{args.port}/executions")
    print("  停止服务 : 按 Ctrl+C")
    print("=" * 64)

    # 4.5 非回环绑定安全告警（仅提示不阻断；默认 127.0.0.1 不触发）
    _warn_non_loopback_binding(args.host)

    # 5. 启动 Flask 内置开发服务器（阻塞）；use_reloader 跟随 debug 开关，
    #    关闭 reloader 可避免调试模式下进程被拉起两次导致的钩子/线程重复问题
    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=args.debug,
    )


# 标准 Python 入口判断：直接 py run.py 时执行 main()，被 import 时不启动
if __name__ == "__main__":
    main()
