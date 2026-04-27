"""在一个 FastMeow 应用下运行多个 WhatsApp 账户。

从仓库根目录运行::

    python examples/multi_account.py

这演示了：
    * 一个 :class:`FastMeow` 实例承载多个账户。
    * 一个统一的 :class:`Router`，其处理器会为 *每个* 账户运行；
      ``ctx.account_key`` 会在分发时区分它们。
    * 通过 :func:`asyncio.gather` 并行完成所有账户的配对。
    * 将入站消息通过接收它们的 *同一个* 账户路由回去（ctx 具有账户作用域，因此 ``ctx.reply`` 和
      ``ctx.send`` 始终使用正确的身份）。

每个账户都使用自己位于
``./sessions/<account_key>/`` 下的会话子目录。首次运行时，你会被要求为每个账户扫描一个二维码；
后续运行会自动重连。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastmeow import (
    ConnectedEvent,
    Ctx,
    F,
    FastMeow,
    MessageEvent,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


ACCOUNT_KEYS = ["alice", "bob"]


router = Router(name="multi")


@router.connected()
async def on_connected(event: ConnectedEvent, ctx: Ctx) -> None:
    """每当账户（重新）连接时，每个账户触发一次。"""
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message((F.text == "whoami") & ~F.from_me)
async def whoami(msg: MessageEvent, ctx: Ctx) -> None:
    """回复处理了这条消息的账户。

    ``alice`` 和 ``bob`` 都共享这个处理器，但 ``ctx`` 会按事件重建，
    因此每次回复都能正确标识接收入站消息的账户。
    """
    await ctx.reply(f"you reached '{ctx.account_key}' ({ctx.account_jid})")


@router.message(F.is_dm & ~F.from_me)
async def echo(msg: MessageEvent, ctx: Ctx) -> None:
    if not msg.text:
        return
    # 给回声加上 account_key 标签，这样当你同时连接多个身份时，就能明显看出是哪个身份作出的回应。
    await ctx.reply(f"[{ctx.account_key}] echo: {msg.text}")


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)

        # add_account 是同步返回 / 异步完成的：调用会立即返回一个 AccountHandle，
        # 但连接会在后台建立。``handle.ready()`` 才是真正等待 CONNECTED 状态的地方。
        handles = [
            app.add_account(key, on_qr="terminal") for key in ACCOUNT_KEYS
        ]

        print(f"waiting for {len(handles)} accounts to connect...")
        await asyncio.gather(*(h.ready(timeout=180) for h in handles))
        for h in handles:
            print(f"  ready: {h.account_key} -> {h.jid}")

        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
