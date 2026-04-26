"""Slash-command bot.

Run from the repo root::

    python examples/command_bot.py

What this demonstrates:
    * Dispatching ``/command arg1 arg2`` style messages.
    * Using ``F.text.startswith("/")`` as a coarse pre-filter, then
      parsing the command name and args inside the handler.
    * Handler-local registry pattern for extensibility (add a new
      command by adding a function to ``COMMANDS``).
    * ``ctx.reply`` for quoted in-chat responses; ``MessageSendError``
      handling around the send.

On first run, scan the printed QR with the WhatsApp app
(Settings -> Linked Devices -> Link a Device). On subsequent runs the
sidecar reuses the saved session in ``./sessions``.

Try sending these to the bot from another WhatsApp account::

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


# --- Command registry -----------------------------------------------------
#
# Each command is an async function ``(ctx, args) -> str`` that returns
# the reply text. Returning an empty string means "no reply". Add a new
# command by writing a function and adding it to COMMANDS below.

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
    """Parse ``/cmd args...`` and dispatch to ``COMMANDS``.

    The ``F.text.startswith("/")`` filter keeps every other message off
    this handler. ``~F.from_me`` prevents the bot from reacting to its
    own outbound echoes (which arrive as MessageEvent with from_me=True).
    """
    # split() with no args also collapses runs of whitespace, which is
    # what we want for a chat command line.
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
        # Network blip, recipient blocked us, etc. Don't crash the
        # handler loop -- log and move on.
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
