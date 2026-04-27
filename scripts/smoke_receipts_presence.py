"""Phase 4.2 回执 / 在线状态 真账号 smoke 脚本。

**用途**：在真实 WhatsApp 账号上端到端验证 4 个新 RPC + 3 个新事件，
确认 sidecar 转发的回执 / 在线状态 / 聊天在线状态能被 SDK 正确解码。
**不在 CI 中运行**——仅供开发者本地手动验证。

**运行**::

    python scripts/smoke_receipts_presence.py

**前置条件**：
    * ``./sessions/`` 下已有配对会话（首次运行会打印 QR）。
    * 设 ``$FASTMEOW_SMOKE_PEER`` 为对端 JID，例如
      ``$Env:FASTMEOW_SMOKE_PEER="8613800138000@s.whatsapp.net"``。
      该对端会收到一条测试消息，其「已读 / 输入中」回应将被本脚本捕获。

**覆盖**：
    * **出站 RPC**：``send_presence(AVAILABLE)`` → ``subscribe_presence`` →
      ``send_chat_presence(COMPOSING)`` → ``send_chat_presence(PAUSED)`` →
      发送一条带文本的消息 → 等待 peer 回读 → ``mark_read``。
    * **入站事件**：监听 60 秒，打印收到的 ReceiptEvent / PresenceEvent /
      ChatPresenceEvent，验证 ``include_soft_events`` 自动开启。

**期望**：peer 在线并主动回看消息时，至少能看到一个 ReceiptEvent；
peer 切换前后台时能看到 PresenceEvent；peer 输入回复时能看到
ChatPresenceEvent。无 peer 配合也能验证出站 RPC 不抛错。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from fastmeow import (
    ChatPresenceEvent,
    ChatPresenceMedia,
    ChatPresenceState,
    Ctx,
    FastMeow,
    PresenceEvent,
    PresenceType,
    ReceiptEvent,
    ReceiptType,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smoke_receipts_presence")


def _ok(step: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"OK  {step}{suffix}")


router = Router(name="smoke_rp")


@router.on_receipt()
async def _on_receipt(event: ReceiptEvent, ctx: Ctx) -> None:
    print(
        f"  RECV ReceiptEvent type={event.receipt_type.value} "
        f"chat={event.chat_jid} sender={event.sender_jid} "
        f"ids={list(event.message_ids)}"
    )


@router.on_presence()
async def _on_presence(event: PresenceEvent, ctx: Ctx) -> None:
    print(
        f"  RECV PresenceEvent from={event.from_jid} "
        f"unavailable={event.unavailable} last_seen={event.last_seen}"
    )


@router.on_chat_presence()
async def _on_chat_presence(event: ChatPresenceEvent, ctx: Ctx) -> None:
    print(
        f"  RECV ChatPresenceEvent chat={event.chat_jid} "
        f"sender={event.sender_jid} "
        f"state={event.state.value} media={event.media.value}"
    )


async def main() -> int:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    peer = os.environ.get("FASTMEOW_SMOKE_PEER")
    listen_seconds = int(os.environ.get("FASTMEOW_SMOKE_LISTEN", "60"))

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("smoke", on_qr="terminal")
        print("waiting for pairing/connect (timeout 120s)...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")
        client = handle.client

        # 1) send_presence —— 全局上线广播。
        await client.send_presence(PresenceType.AVAILABLE)
        _ok("send_presence", "AVAILABLE")

        if not peer:
            print("FASTMEOW_SMOKE_PEER not set; skipping peer-bound RPCs and listen")
            await client.send_presence(PresenceType.UNAVAILABLE)
            _ok("send_presence", "UNAVAILABLE")
            print("ALL OK")
            return 0

        # 2) subscribe_presence —— 订阅对端 presence，使后续 PresenceEvent 能到达。
        await client.subscribe_presence(peer)
        _ok("subscribe_presence", peer)

        # 3) send_chat_presence —— 装作正在输入然后暂停。
        await client.send_chat_presence(
            peer, ChatPresenceState.COMPOSING, media=ChatPresenceMedia.TEXT
        )
        _ok("send_chat_presence", "COMPOSING")
        await asyncio.sleep(2)
        await client.send_chat_presence(peer, ChatPresenceState.PAUSED)
        _ok("send_chat_presence", "PAUSED")

        # 4) 发一条文本消息，给 peer 回读 / 输入回应的入口。
        result = await client.send_text(peer, "fastmeow phase 4.2 smoke")
        _ok("send_text", result.message_id)

        # 5) 主动 mark_read —— 即使 peer 没有回消息，也验证 RPC 不抛错。
        await client.mark_read(
            peer, peer, [result.message_id], receipt_type=ReceiptType.READ
        )
        _ok("mark_read self-message", "READ")

        # 6) 被动监听 —— 等待来自 peer 的回执 / presence / chat presence。
        print(
            f"listening {listen_seconds}s for ReceiptEvent / PresenceEvent / "
            f"ChatPresenceEvent from {peer} (please open the chat / type a reply)..."
        )
        await asyncio.sleep(listen_seconds)

        await client.send_presence(PresenceType.UNAVAILABLE)
        _ok("send_presence", "UNAVAILABLE")

    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
