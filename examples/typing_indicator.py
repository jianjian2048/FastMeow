"""输入指示与回执示例（Phase 4.2）。

从仓库根目录运行::

    python examples/typing_indicator.py

这演示了：
    * 用 ``ctx.client.send_chat_presence`` 在回复前显示“正在输入…”。
    * 用 ``ctx.client.mark_read`` 把对方消息标为已读，并触发蓝色双勾。
    * 用 ``send_presence`` 广播账号级 available / unavailable。
    * 通过 ``@router.on_receipt()`` / ``@router.on_presence()`` /
      ``@router.on_chat_presence()`` 监听 soft event；
      只要注册了任意一个，dispatcher 会自动让 sidecar 把这些事件推送上来，
      否则它们会在 sidecar 出口被静默丢弃以省带宽。

首次运行请用 WhatsApp 应用扫描终端打印的二维码
（设置 -> 已关联设备 -> 关联设备）。从另一个 WhatsApp 账号给本机器人
发任意文字，即可看到 “typing…” 指示与已读回执。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastmeow import (
    ChatPresenceEvent,
    ChatPresenceMedia,
    ChatPresenceState,
    Ctx,
    F,
    FastMeow,
    MessageEvent,
    MessageSendError,
    PresenceEvent,
    PresenceType,
    ReceiptEvent,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

router = Router(name="typing-indicator")


# --- Soft event 监听器 ----------------------------------------------------
#
# 注册以下任意一个装饰器后，dispatcher 会在 stream_events 请求中
# 把 include_soft_events 标志置为 True，sidecar 才会把回执 / 在线状态
# 翻译成事件推上来。否则它们会在 Go 侧被静默丢弃。


@router.on_receipt()
async def on_receipt(event: ReceiptEvent, ctx: Ctx) -> None:
    """对方对我们发的消息产生 delivered/read/played 等回执时触发。"""
    logging.info(
        "[%s] receipt from=%s type=%s ids=%s",
        ctx.account_key,
        event.sender_jid,
        event.receipt_type.name,
        event.message_ids,
    )


@router.on_presence()
async def on_presence(event: PresenceEvent, ctx: Ctx) -> None:
    """订阅过的联系人 available / unavailable 时触发。"""
    logging.info(
        "[%s] presence %s unavailable=%s last_seen=%s",
        ctx.account_key,
        event.from_jid,
        event.unavailable,
        event.last_seen,
    )


@router.on_chat_presence()
async def on_chat_presence(event: ChatPresenceEvent, ctx: Ctx) -> None:
    """对方在某个聊天里 “正在输入…” / “正在录音…” 时触发。"""
    logging.info(
        "[%s] chat_presence chat=%s sender=%s state=%s media=%s",
        ctx.account_key,
        event.chat_jid,
        event.sender_jid,
        event.state.name,
        event.media.name,
    )


# --- 主消息处理器 ---------------------------------------------------------


@router.message(F.text & ~F.from_me)
async def on_text(msg: MessageEvent, ctx: Ctx) -> None:
    """收到对方消息后：标已读 -> 显示输入中 -> 假装思考 -> 回复 -> 暂停输入。"""
    client = ctx.client

    # 1. 立刻把对方那条消息标为已读（蓝色双勾）。
    try:
        await client.mark_read(
            chat_jid=msg.chat_jid,
            sender_jid=msg.sender_jid,
            message_ids=[msg.message_id],
        )
    except MessageSendError:
        logging.exception("[%s] mark_read failed", ctx.account_key)

    # 2. 显示 “正在输入…”。
    try:
        await client.send_chat_presence(
            msg.chat_jid,
            state=ChatPresenceState.COMPOSING,
            media=ChatPresenceMedia.TEXT,
        )
    except MessageSendError:
        logging.exception("[%s] send_chat_presence(COMPOSING) failed", ctx.account_key)

    # 3. 假装思考一会儿，让对方真的看到 “typing…”。
    await asyncio.sleep(1.5)

    # 4. 真正回复。
    try:
        await ctx.reply(f"你说的是：{msg.text}")
    except MessageSendError:
        logging.exception("[%s] reply failed", ctx.account_key)

    # 5. 关掉输入指示。
    try:
        await client.send_chat_presence(
            msg.chat_jid,
            state=ChatPresenceState.PAUSED,
            media=ChatPresenceMedia.TEXT,
        )
    except MessageSendError:
        logging.exception("[%s] send_chat_presence(PAUSED) failed", ctx.account_key)


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")

        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")

        # 广播 “在线”，对方在聊天列表里能看到 last seen 更新。
        try:
            await handle.client.send_presence(PresenceType.AVAILABLE)
        except MessageSendError:
            logging.exception("send_presence(AVAILABLE) failed")

        print("send a text message from another account to see typing+read receipts.")
        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")
        finally:
            # 优雅地标记下线。
            with contextlib.suppress(MessageSendError):
                await handle.client.send_presence(PresenceType.UNAVAILABLE)


if __name__ == "__main__":
    asyncio.run(main())
