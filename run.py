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

# create_app: Flask 应用工厂，返回已完成蓝图注册/异常处理/钩子装配的应用实例
from src.web.app import create_app


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
