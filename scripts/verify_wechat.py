"""企微通知接入验证脚本（手动执行，不进测试套件，会发真实消息）。

用途:
    配置好 .env 中的 TM_WECHAT_WEBHOOK_URL 后，运行本脚本向企微群机器人
    发送一条测试 markdown 消息，验证整条链路（.env 读取 → webhook 校验 →
    requests.post → 企微 errcode=0）真实可用。

用法:
    py scripts/verify_wechat.py

退出码:
    0 = 企微返回 errcode=0（发送成功）
    1 = webhook 未配置 / 网络异常 / 企微返回非 0
"""

import datetime
import sys
from pathlib import Path

# 允许从 scripts/ 目录直接运行：把项目根加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402
from src.common.env_manager import env_manager  # noqa: E402
from src.core.notification import Notification, WeChatNotifier  # noqa: E402

WEBHOOK_KEY = "TM_WECHAT_WEBHOOK_URL"
ENABLED_KEY = "TM_WECHAT_ENABLED"
TIMEOUT_SECONDS = 10


def main() -> int:
    """发送一条企微验证消息，返回进程退出码。

    返回:
        int: 0 成功 / 1 失败
    """
    webhook = str(env_manager.get(WEBHOOK_KEY, ""))
    enabled = env_manager.get_bool(ENABLED_KEY, False)

    print("=" * 60)
    print("TestMatrix 企微通知接入验证")
    print("=" * 60)
    print(f"  {ENABLED_KEY} = {enabled}")
    print(f"  {_webhook_hint(webhook)}")

    if not enabled:
        print(f"[提示] {ENABLED_KEY} 未置为 true，仍继续发送以验证链路。")
    if not webhook:
        print("[失败] 请先在 .env 中填入 TM_WECHAT_WEBHOOK_URL（企微群机器人 webhook）")
        return 1

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notifier = WeChatNotifier()
    notification = Notification(
        title="TestMatrix 企微通知接入验证",
        content=(
            "### TestMatrix 企微通知接入验证\n"
            f"> 时间：{timestamp}\n"
            "> 状态：**链路连通**\n"
            "> 说明：这是一条来自 verify_wechat.py 的测试消息，收到即代表"
            "批次完成通知可正常推送到本群。"
        ),
    )

    try:
        ok = notifier.send(notification)
    except Exception as exc:  # 防御性兜底，正常 Notifier 契约不抛
        print(f"[异常] 发送过程抛出异常：{type(exc).__name__}: {exc}")
        return 1

    if not ok:
        print("[失败] notifier.send 返回 False，请检查 webhook 与网络（见上方日志）。")
        return 1

    # 直接再发一次裸请求以展示企微原始 errcode/errmsg（send 内部已判定 errcode=0）
    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"> verify_wechat 二次确认（{timestamp}）：errcode 自检"},
        }
        resp = requests.post(webhook, json=payload, timeout=TIMEOUT_SECONDS)
        body = resp.json()
        errcode = body.get("errcode")
        errmsg = body.get("errmsg")
        print(f"[企微原始返回] errcode={errcode} errmsg={errmsg}")
        if errcode == 0:
            print("[成功] 两条验证消息已发送，请到企微群确认收到。")
            return 0
        print(f"[失败] 企微返回非 0：errcode={errcode} errmsg={errmsg}")
        return 1
    except Exception as exc:
        # 第一条已成功时，二次自检异常不改变总体结论
        print(f"[提示] 二次自检请求异常（首条已发送成功）：{type(exc).__name__}: {exc}")
        print("[成功] 首条验证消息发送成功。")
        return 0


def _webhook_hint(webhook: str) -> str:
    """脱敏展示 webhook 配置状态。"""
    if not webhook:
        return f"{WEBHOOK_KEY} = （空）"
    return f"{WEBHOOK_KEY} = {webhook[:48]}..."


if __name__ == "__main__":
    raise SystemExit(main())
