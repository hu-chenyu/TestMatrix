"""测试库文件清理工具。

为什么单独抽一个模块:
  阶段0 用 `contextlib.suppress(PermissionError)` 兜住了 Windows 文件锁，
  但静默吞掉会让"连接未及时释放"这个信号彻底消失。这里改为**警告不失败**:
  正常路径零输出，真撞锁时告警进入 pytest 的 warnings summary，
  既不把用例升级成 teardown ERROR，也不放弃可观测性。
"""

import warnings
from pathlib import Path


def safe_unlink(path: Path) -> None:
  """
  删除测试库文件（Windows 文件锁场景降级为警告）

  参数:
    path (Path): 待删除的临时库文件路径

  返回:
    无

  异常:
    无（PermissionError 已转为 warning；其余异常照常抛出）
  """
  try:
    path.unlink(missing_ok=True)
  except PermissionError as exc:
    # tmp_path 由 pytest 会话级保留策略兜底清理，残留不影响后续用例
    warnings.warn(
      f"测试库文件仍被占用，未删除: {path} ({exc})",
      stacklevel=2,
    )
