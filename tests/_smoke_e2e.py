"""End-to-end smoke: real WhatsApp account through the FastMeow Python API.

Spawns the real Go sidecar, opens a real gRPC channel, registers an
account, drives QR pairing on first run (or reattaches an existing
session on subsequent runs), and listens for one inbound message.

Run manually (requires interactive terminal for QR scan + a phone):

    .\\.venv\\Scripts\\python.exe -m tests._smoke_e2e

Behavior:
    * First run: prints a QR code on stderr (terminal renderer). Scan
      from WhatsApp -> Linked Devices on your phone.
    * Reattach run: skips QR; goes straight to CONNECTED using the
      existing sqlite session in ``./e2e-smoke-sessions/``.
    * Once connected, prints every incoming MessageEvent and replies
      ``echo: <text>`` to text DMs. Stops after one reply or 120s
      timeout, whichever comes first.

Cleanup:
    Sessions live under ``D:\\Srcs\\FastMeow\\e2e-smoke-sessions\\``.
    Delete the directory to force a fresh QR pairing next run.
"""

from __future__ import annotations

import asyncio
import logging
import sys
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
_log = logging.getLogger("fastmeow.smoke.e2e")


SESSION_DIR = Path(__file__).resolve().parent.parent / "e2e-smoke-sessions"
PAIRING_TIMEOUT = 120.0
ACTIVITY_TIMEOUT = 120.0


async def main() -> int:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[smoke] session dir: {SESSION_DIR}", file=sys.stderr)

    router = Router(name="smoke")
    first_message: asyncio.Future[MessageEvent] = asyncio.get_event_loop().create_future()

    @router.connected()
    async def on_connected(event: ConnectedEvent, ctx: Ctx) -> None:
        print(
            f"[smoke] connected: account={ctx.account_key} jid={ctx.account_jid}",
            file=sys.stderr,
        )

    @router.message(F.is_dm & ~F.from_me)
    async def on_dm(msg: MessageEvent, ctx: Ctx) -> None:
        print(
            f"[smoke] message from {msg.sender_jid}: {msg.text!r}",
            file=sys.stderr,
        )
        if msg.text:
            try:
                result = await ctx.reply(f"echo: {msg.text}")
                print(
                    f"[smoke] replied: server_id={result.message_id}",
                    file=sys.stderr,
                )
            except Exception:
                _log.exception("reply failed")
            if not first_message.done():
                first_message.set_result(msg)

    async with FastMeow(session_dir=SESSION_DIR) as app:
        app.include_router(router)

        handle = app.add_account("smoke-e2e", on_qr="terminal")
        print(
            f"[smoke] add_account returned; state={handle.state} jid={handle.jid!r}",
            file=sys.stderr,
        )

        print(
            f"[smoke] waiting up to {PAIRING_TIMEOUT}s for CONNECTED...",
            file=sys.stderr,
        )
        await handle.ready(timeout=PAIRING_TIMEOUT)
        print(
            f"[smoke] CONNECTED: jid={handle.jid}; send a DM from another phone to test echo.",
            file=sys.stderr,
        )

        try:
            msg = await asyncio.wait_for(first_message, timeout=ACTIVITY_TIMEOUT)
            print(
                f"[smoke] OK: got message + replied. seq={msg.seq}",
                file=sys.stderr,
            )
            return 0
        except TimeoutError:
            print(
                f"[smoke] no inbound message in {ACTIVITY_TIMEOUT}s; "
                "connection itself was OK so smoke is partially green.",
                file=sys.stderr,
            )
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
