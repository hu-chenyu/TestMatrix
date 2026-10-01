"""Redis 任务队列 worker 独立常驻启动脚本（Day41 前40天遗漏修复）。

用途:
    TM_TASK_QUEUE_ENABLED=true 时，trigger 路由只把任务 LPUSH 进 Redis，
    需要一个独立进程常驻执行 BRPOP 消费。本脚本在独立终端中启动
    src.core.task_queue 的模块级单 worker（串行消费），并提供：
        - 启动自检（队列开关 / 队列 key / BRPOP 超时 / Redis PING / 建表）
        - Ctrl+C（Windows 下另支持 Ctrl+Break）优雅停止：
          置 stop_event → worker 在当前 BRPOP 超时边界退出 → join 收尾
        - 队列开关关闭时打印警告但进程仍常驻（不崩溃），
          便于先用本脚本验证部署，再切 .env 开关

用法:
    py scripts/start_worker.py            # 前台运行，Ctrl+C 停止

前置:
    1. .env 中 TM_REDIS_ENABLED=true、TM_TASK_QUEUE_ENABLED=true
    2. Redis 已在 TM_REDIS_URL（默认 redis://127.0.0.1:6379/0）可达
    3. Web 进程另开终端启动：py run.py

退出码:
    0 = 收到停止信号并优雅退出
    1 = 启动自检阶段发生未预期异常
"""

import os
import signal
import sys
import threading
from pathlib import Path

# 允许从 scripts/ 目录直接运行：把项目根加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.task_queue import (  # noqa: E402
    start_worker,
    stop_worker,
    task_queue_client,
)
from src.db.db_session import DatabaseSession  # noqa: E402

# 主进程阻塞等待的退出事件：信号处理器置位后 main 线程解除阻塞
_exit_event = threading.Event()


def _handle_stop_signal(signum: int, _frame: object) -> None:
    """信号处理：通知 worker 停止并解除主进程阻塞。

    参数:
        signum (int): 收到的信号编号（仅用于日志）
        _frame (object): 中断时的栈帧（本脚本不使用）

    返回:
        无
    """
    print(f"\n收到停止信号({signum})，正在通知 worker 收尾……")
    try:
        stop_worker()
    finally:
        _exit_event.set()


def _ping_redis() -> tuple[bool, str]:
    """对 Redis 做一次 PING 探活，返回连通状态与说明文字。

    返回:
        tuple[bool, str]: (是否连通, 状态说明)；
            队列关闭后端未构建时返回 (False, "后端未构建")；
            PING 异常时返回 (False, "异常类型: 异常信息")
    """
    # 运维脚本一次性状态探测，复用客户端懒加载入口（不新增公共 API）
    backend = task_queue_client._get_backend()
    if backend is None:
        return False, "后端未构建（队列开关关闭或后端依赖缺失）"
    try:
        backend.ping()
    except Exception as exc:  # noqa: BLE001 探活只展示状态，不允许打断启动
        return False, f"连接失败：{type(exc).__name__}: {exc}"
    return True, "PING 成功"


def main() -> int:
    """worker 常驻主入口：自检建表 → 起 worker 线程 → 阻塞等停止信号。

    返回:
        int: 进程退出码，0=优雅停止，1=启动阶段异常
    """
    print("=" * 64)
    print("TestMatrix 任务队列 Worker（独立常驻进程）")
    print("=" * 64)

    queue_enabled = task_queue_client.enabled
    if queue_enabled:
        # 独立 worker 进程自我授权：Web 与本进程共用同一份 .env，
        # 若 .env 里 TM_TASK_WORKER_ENABLED=false（Web 不起 in-app worker，
        # 保证全局只有本进程一个消费者），进程级环境变量优先级高于 .env，
        # 在此置位即可让 start_worker() 实际创建消费线程
        os.environ["TM_TASK_WORKER_ENABLED"] = "true"
    worker_enabled = task_queue_client.worker_enabled
    print(f"  TM_TASK_QUEUE_ENABLED = {queue_enabled}")
    print(f"  TM_TASK_WORKER_ENABLED = {worker_enabled}（脚本进程随队列开关自动置位）")
    print(f"  Redis URL             = {task_queue_client.redis_url}")
    print(f"  队列 key              = {task_queue_client.queue_key}")
    print(f"  BRPOP 超时            = {task_queue_client.brpop_timeout}s")

    if not queue_enabled:
        print(
            "[警告] 任务队列未启用（TM_TASK_QUEUE_ENABLED!=true），"
            "worker 不会处理任何任务。"
        )
        print("       进程仍保持常驻；如需消费任务，请在 .env 中开启后重启本脚本。")

    redis_ok, redis_hint = _ping_redis()
    print(f"  Redis 连接状态        = {'正常' if redis_ok else '不可达'}（{redis_hint}）")
    if queue_enabled and not redis_ok:
        print("[警告] Redis 当前不可达：worker 会持续重试，请确认 Redis 已启动。")

    # 独立进程无 Web 工厂兜底，先幂等建表（含批次/死信/通知历史等全部表），
    # 保证 worker 消费到首个任务时 _execute_batch_async 的落库链路可用
    try:
        DatabaseSession.init_db()
        print("  数据库表结构          = 已就绪（init_db 幂等）")
    except Exception as exc:  # noqa: BLE001 建表失败明确告知但不静默吞掉
        print(f"[失败] 数据库初始化失败：{type(exc).__name__}: {exc}")
        return 1

    # 注册停止信号：Windows 支持 SIGINT 与 SIGBREAK；
    # SIGTERM 仅在平台提供时注册（POSIX 下由 kill/killall 触发）
    signal.signal(signal.SIGINT, _handle_stop_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows 专属：Ctrl+Break
        signal.signal(signal.SIGBREAK, _handle_stop_signal)  # type: ignore[attr-defined]
    if hasattr(signal, "SIGTERM") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, _handle_stop_signal)

    worker_thread = start_worker()
    if worker_thread is None:
        print("[提示] start_worker() 未创建消费线程（开关关闭），进程空转待命。")
    else:
        print(f"  worker 线程           = 已启动（name={worker_thread.name}，daemon=True）")
    print("-" * 64)
    print("Worker 已就绪，等待队列任务……按 Ctrl+C 优雅停止。")
    print("-" * 64)

    # worker 是 daemon 线程，主线程必须阻塞，否则进程立即退出
    try:
        _exit_event.wait()
    except KeyboardInterrupt:
        # 信号处理器已覆盖常规路径，此处为双保险
        stop_worker()

    print("Worker 已停止，进程退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
