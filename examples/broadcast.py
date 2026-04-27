"""向一个 JID 列表广播消息。

从仓库根目录运行::

    # 1. edit RECIPIENTS below to real JIDs you control
    # 2. run it
    python examples/broadcast.py

这演示了：
    * 在不使用 WhatsApp 广播列表功能的情况下进行一对多发送
      （该功能有一些注意事项）——只是对 ``send_text`` 的普通异步扇出。
    * 通过 ``@router.connected()`` 触发工作，因此只在
      账户完全在线后发送。
    * 使用 ``ctx.send(jid, text)``（而不是 ``ctx.reply``），因为
      没有可回复的入站消息。
    * 读取 :class:`SendResult` 以暴露 WhatsApp 返回的
      ``message_id`` 和 sidecar 的幂等 ``deduped`` 标志。
    * 通过 :class:`asyncio.Semaphore` 进行有界并发——WhatsApp
      会对过于激进的扇出进行限流（并可能封号）。请根据
      你的账户状况调整 ``MAX_CONCURRENT`` 和 ``DELAY_SECONDS``。
    * 广播完成后干净退出（不需要 run_forever）。

JID 格式提示：用户使用 ``<不含加号的手机号>@s.whatsapp.net``
（例如 ``14155551234@s.whatsapp.net``），群组则使用 ``<id>@g.us``。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastmeow import (
    ConnectedEvent,
    Ctx,
    FastMeow,
    InvalidJIDError,
    MessageSendError,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# --- 配置 ---------------------------------------------------------------

# 替换为你拥有的、并且有权联系的真实 JID。
RECIPIENTS: list[str] = [
    "14155551234@s.whatsapp.net",
    "14155555678@s.whatsapp.net",
]

BROADCAST_TEXT = "Hello from FastMeow! (broadcast example)"

# 保守默认值，以便遵守 WhatsApp 的速率限制。
MAX_CONCURRENT = 3
DELAY_SECONDS = 0.5

# 在广播运行时发出信号，这样 main() 就可以关闭应用。
done = asyncio.Event()


router = Router(name="broadcast")


@router.connected()
async def on_connected(event: ConnectedEvent, ctx: Ctx) -> None:
    """在此账户完全连接后，执行广播扇出。

    ConnectedEvent 会在 ``handle.ready()`` 解析后以及每次后续重连时
    触发。我们使用 ``done`` 来确保只在第一次连接时发送。
    """
    if done.is_set():
        return
    logging.info("[%s] connected; broadcasting to %d recipients",
                 ctx.account_key, len(RECIPIENTS))

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def send_one(jid: str) -> None:
        async with sem:
            try:
                # ctx.send 是 ctx.reply 的非回复等价形式：它会使用此 ctx
                # 的账户向任意 JID 发送消息。
                result = await ctx.send(jid, BROADCAST_TEXT)
            except InvalidJIDError:
                logging.warning("skipping malformed jid: %s", jid)
                return
            except MessageSendError:
                logging.exception("send to %s failed", jid)
                return
            tag = " (deduped)" if result.deduped else ""
            logging.info("sent to %s -> %s%s", jid, result.message_id, tag)
            await asyncio.sleep(DELAY_SECONDS)

    await asyncio.gather(*(send_one(jid) for jid in RECIPIENTS))
    done.set()


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")

        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")

        # 广播在 @router.connected() 处理器内部完成。
        # 等待该信号，然后退出（不需要 run_forever）。
        await done.wait()
        print("broadcast complete; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
