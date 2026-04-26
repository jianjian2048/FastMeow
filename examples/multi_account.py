"""Run multiple WhatsApp accounts under one FastMeow app.

Run from the repo root::

    python examples/multi_account.py

What this demonstrates:
    * One :class:`FastMeow` instance hosting several accounts.
    * A single :class:`Router` whose handlers run for *every* account;
      ``ctx.account_key`` distinguishes them at dispatch time.
    * Pairing all accounts in parallel via :func:`asyncio.gather`.
    * Routing inbound messages back through the *same* account that
      received them (the ctx is account-scoped, so ``ctx.reply`` and
      ``ctx.send`` always use the right identity).

Each account uses its own session subdirectory under
``./sessions/<account_key>/``. On first run you'll be asked to scan a
QR code per account; subsequent runs auto-reconnect.
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
    """Fires once per account whenever it (re)connects."""
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message((F.text == "whoami") & ~F.from_me)
async def whoami(msg: MessageEvent, ctx: Ctx) -> None:
    """Reply with the account that handled this message.

    Both ``alice`` and ``bob`` share this handler, but ``ctx`` is
    rebuilt per event, so each reply correctly identifies the
    account that received the inbound message.
    """
    await ctx.reply(f"you reached '{ctx.account_key}' ({ctx.account_jid})")


@router.message(F.is_dm & ~F.from_me)
async def echo(msg: MessageEvent, ctx: Ctx) -> None:
    if not msg.text:
        return
    # Tag the echo with the account_key so it's obvious which identity
    # answered when you have several connected at once.
    await ctx.reply(f"[{ctx.account_key}] echo: {msg.text}")


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)

        # add_account is sync-return / async-complete: the call returns
        # immediately with an AccountHandle, but the connection is
        # established in the background. ``handle.ready()`` is what
        # actually awaits CONNECTED state.
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
