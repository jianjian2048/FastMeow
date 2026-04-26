"""Minimal echo bot.

Run from the repo root::

    python examples/basic_echo.py

What this demonstrates:
    * Building a :class:`Router` with a ``F``-magic filter.
    * Mounting it on a :class:`FastMeow` app.
    * QR pairing via ``on_qr="terminal"`` on first run.
    * Replying in the same chat via ``ctx.reply``.

On first run, scan the printed QR with the WhatsApp app
(Settings -> Linked Devices -> Link a Device). On subsequent runs the
sidecar reuses the saved session in ``./sessions``.

Stop with Ctrl+C; the supervisor cleans up the sidecar on exit.
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
    """Log when each account finishes (re)connecting."""
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message(F.text == "ping")
async def pong(msg: MessageEvent, ctx: Ctx) -> None:
    """Hello-world filter: react only to the literal text ``ping``."""
    await ctx.reply("pong")


@router.message(F.is_dm & ~F.from_me)
async def echo(msg: MessageEvent, ctx: Ctx) -> None:
    """Catch-all DM echo. Filters compose with ``&``, ``|``, ``~``.

    First-match-wins: the ``F.text == "ping"`` handler above wins for
    ``"ping"`` messages, so this handler does not see them.
    """
    if not msg.text:
        return  # ignore non-text payloads (Phase 1 ships text only).
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
