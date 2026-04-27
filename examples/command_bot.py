"""斜杠命令机器人。

从仓库根目录运行::

    python examples/command_bot.py

这演示了：
    * 分发 ``/command arg1 arg2`` 形式的消息。
    * 使用 ``F.text.startswith("/")`` 作为粗略预过滤，然后在处理器内部解析命令名和参数。
    * 处理器局部的注册表模式便于扩展（通过向 ``COMMANDS`` 添加一个函数来新增命令）。
    * 使用 ``ctx.reply`` 进行带引用的聊天内回复；对发送过程进行 ``MessageSendError`` 处理。

首次运行时，请用 WhatsApp 应用扫描打印出的二维码
（设置 -> 已关联设备 -> 关联设备）。后续运行时，
sidecar 会复用 ``./sessions`` 中保存的会话。

试着从另一个 WhatsApp 账户向机器人发送这些消息::

    /help
    /ping
    /echo hello world
    /whoami
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastmeow import (
    Ctx,
    F,
    FastMeow,
    MessageEvent,
    MessageSendError,
    Router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

router = Router(name="commands")


# --- 命令注册表 -----------------------------------------------------
#
# 每个命令都是一个异步函数 ``(ctx, args) -> str``，它会返回
# 回复文本。返回空字符串表示“无回复”。通过编写一个函数并将其添加到下面的 COMMANDS 中即可新增命令。

CommandFn = Callable[[Ctx, list[str]], Awaitable[str]]


async def cmd_help(_ctx: Ctx, _args: list[str]) -> str:
    return (
        "Available commands:\n"
        "  /help          - show this message\n"
        "  /ping          - liveness check\n"
        "  /echo <text>   - echo the rest of the line\n"
        "  /whoami        - show which account answered you"
    )


async def cmd_ping(_ctx: Ctx, _args: list[str]) -> str:
    return "pong"


async def cmd_echo(_ctx: Ctx, args: list[str]) -> str:
    if not args:
        return "usage: /echo <text>"
    return " ".join(args)


async def cmd_whoami(ctx: Ctx, _args: list[str]) -> str:
    # ctx.account_key is the user-chosen id (e.g. "demo"); account_jid is
    # the WhatsApp-issued JID for that account. Both come from the
    # _EventBase metadata attached by the dispatcher.
    return f"account_key={ctx.account_key}\naccount_jid={ctx.account_jid}"


COMMANDS: dict[str, CommandFn] = {
    "/help": cmd_help,
    "/ping": cmd_ping,
    "/echo": cmd_echo,
    "/whoami": cmd_whoami,
}


# --- Single dispatch handler ---------------------------------------------


@router.message(F.text.startswith("/") & ~F.from_me)
async def on_command(msg: MessageEvent, ctx: Ctx) -> None:
    """解析 ``/cmd args...`` 并分发到 ``COMMANDS``。

    ``F.text.startswith("/")`` 过滤器会让其他所有消息都不进入这个处理器。``~F.from_me``
    可防止机器人对自己发出的回声作出反应（这些回声会以 from_me=True 的 MessageEvent 到达）。
    """
    # 不带参数的 split() 也会压缩连续空白，这正是我们对聊天命令行所需要的。
    parts = msg.text.split()
    if not parts:
        return

    name, args = parts[0].lower(), parts[1:]
    handler = COMMANDS.get(name)
    if handler is None:
        await ctx.reply(f"unknown command: {name}. try /help")
        return

    try:
        body = await handler(ctx, args)
    except Exception:
        logging.exception("command %s failed", name)
        await ctx.reply(f"{name} failed; see logs.")
        return

    if not body:
        return

    try:
        await ctx.reply(body)
    except MessageSendError:
        # 网络抖动、收件人屏蔽了我们等情况。不要让处理器循环崩溃——记录日志然后继续。
        logging.exception("reply to %s failed", msg.chat_jid)


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")

        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")
        print("send /help from another WhatsApp account to try it out.")

        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
