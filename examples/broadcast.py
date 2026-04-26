"""Broadcast a message to a list of JIDs.

Run from the repo root::

    # 1. edit RECIPIENTS below to real JIDs you control
    # 2. run it
    python examples/broadcast.py

What this demonstrates:
    * Sending one-to-many *without* WhatsApp's broadcast-list feature
      (which has caveats) -- just a plain async fan-out of ``send_text``.
    * Triggering work from ``@router.connected()`` so we send only after
      the account is fully online.
    * Using ``ctx.send(jid, text)`` (not ``ctx.reply``) since there is
      no inbound message to reply to.
    * Reading :class:`SendResult` to surface the WhatsApp-issued
      ``message_id`` and the sidecar's idempotency ``deduped`` flag.
    * Bounded concurrency via :class:`asyncio.Semaphore` -- WhatsApp
      will rate-limit (and possibly ban) accounts that fan out too
      aggressively. Tune ``MAX_CONCURRENT`` and ``DELAY_SECONDS`` for
      your account's standing.
    * Exiting cleanly after the broadcast completes (no run_forever).

JID format reminder: ``<phone-without-plus>@s.whatsapp.net`` for users
(e.g. ``14155551234@s.whatsapp.net``) or ``<id>@g.us`` for groups.
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


# --- Config ---------------------------------------------------------------

# Replace with real JIDs you own / have consent to message.
RECIPIENTS: list[str] = [
    "14155551234@s.whatsapp.net",
    "14155555678@s.whatsapp.net",
]

BROADCAST_TEXT = "Hello from FastMeow! (broadcast example)"

# Conservative defaults to stay polite to WhatsApp's rate limits.
MAX_CONCURRENT = 3
DELAY_SECONDS = 0.5

# Signalled when the broadcast has run, so main() can shut the app down.
done = asyncio.Event()


router = Router(name="broadcast")


@router.connected()
async def on_connected(event: ConnectedEvent, ctx: Ctx) -> None:
    """Fan out the broadcast once this account is fully connected.

    ConnectedEvent fires after ``handle.ready()`` resolves *and* on
    every subsequent reconnect. We use ``done`` to ensure we send only
    on the first connect.
    """
    if done.is_set():
        return
    logging.info("[%s] connected; broadcasting to %d recipients",
                 ctx.account_key, len(RECIPIENTS))

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def send_one(jid: str) -> None:
        async with sem:
            try:
                # ctx.send is the non-reply equivalent of ctx.reply: it
                # sends to an arbitrary JID using this ctx's account.
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

        # The broadcast happens inside the @router.connected() handler.
        # Wait for that signal, then exit (no run_forever needed).
        await done.wait()
        print("broadcast complete; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
