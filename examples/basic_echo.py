"""最小回声机器人。

从仓库根目录运行::

    python examples/basic_echo.py

这演示了：
    * 使用带有 ``F`` 魔法过滤器的 :class:`Router`。
    * 将其挂载到 :class:`FastMeow` 应用上。
    * 首次运行时通过 ``on_qr="terminal"`` 进行二维码配对。
    * 通过 ``ctx.reply`` 在同一聊天中回复。

首次运行时，请用 WhatsApp 应用扫描打印出的二维码
（设置 -> 已关联设备 -> 关联设备）。后续运行时，
sidecar 会复用 ``./sessions`` 中保存的会话。

按 Ctrl+C 停止；监督器会在退出时清理 sidecar。
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

router = Router(name="echo")


@router.connected()
async def announce_online(event: ConnectedEvent, ctx: Ctx) -> None:
    """在每个账户完成（重新）连接时记录日志。"""
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message(F.text == "ping")
async def pong(msg: MessageEvent, ctx: Ctx) -> None:
    """Hello-world 过滤器：仅响应字面文本 ``ping``。"""
    await ctx.reply("pong")


@router.message(F.is_dm & ~F.from_me)
async def echo(msg: MessageEvent, ctx: Ctx) -> None:
    """通用 DM 回声。过滤器可与 ``&``、``|``、``~`` 组合。

    首个匹配优先：上面的 ``F.text == "ping"`` 处理器会优先处理
    ``"ping"`` 消息，所以这个处理器不会看到它们。
    """
    if not msg.text:
        return  # 忽略非文本负载（Phase 1 仅提供文本）。
    await ctx.reply(f"echo: {msg.text}")


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")

        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")

        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
