# FastMeow
Pythonic, async-native WhatsApp automation SDK powered by an embedded whatsmeow Go sidecar.

## Why FastMeow
- **Zero-CGO Toolchain**: Built with `CGO_ENABLED=0`, requiring no C compiler or Go installed on your machine.
- **Sidecar Architecture**: Runs a specialized Go binary (whatsmeow) as a managed child process, communicating over high-speed gRPC.
- **Pythonic API**: Modern async/await design inspired by FastStream and aiogram, featuring magic filters and mountable routers.
- **Performance**: Capable of handling 100+ concurrent accounts on a single host.
- **Stability**: Unlike direct CGO bindings, the sidecar process isolates the Go runtime from the Python interpreter.

## Status
- **Alpha**: Project is in early development.
- **Phase 1+2**: Go sidecar and Python SDK are shipped and verified.
- **CI Verified**: Multi-platform wheels are tested via automated pipelines (mypy strict, ruff, 117+ tests).
- **Distribution**: Not yet on PyPI. Currently available via direct wheel installation.

## Installation
Currently, install FastMeow from a built wheel artifact:
```bash
pip install ./fastmeow-0.1.0-py3-none-manylinux2014_x86_64.whl
```

Once published, you will be able to install it via PyPI:
```bash
pip install fastmeow
```

Supported platforms: Linux (x86_64), macOS (arm64 12.0+), and Windows (x86_64).

## Quickstart
The following example implements a basic echo bot. On the first run, it will print a QR code in your terminal for pairing. Subsequent runs will reuse the session stored in the `./sessions/` directory.

```python
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

router = Router(name="echo")


@router.connected()
async def announce_online(event: ConnectedEvent, ctx: Ctx) -> None:
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message(F.text == "ping")
async def pong(msg: MessageEvent, ctx: Ctx) -> None:
    await ctx.reply("pong")


@router.message(F.is_dm & ~F.from_me)
async def echo(msg: MessageEvent, ctx: Ctx) -> None:
    if not msg.text:
        return
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
```

## Architecture
FastMeow uses a sidecar pattern to bridge the Go-based `whatsmeow` library with Python.

```
┌──────────────────────────────────────────────────────────────────┐
│ Your Python code                                                 │
│   router = Router(); @router.message(F.text == "ping") ...       │
└─────────────────────────────┬────────────────────────────────────┘
                              │ FastMeow SDK (async, Python 3.12+)
                              │   - Router / Filter / Ctx
                              │   - AccountHandle, multi-account
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ Embedded Go sidecar  (cmd/fastmeow-sidecar, CGO_ENABLED=0)       │
│   - one process per FastMeow app instance                        │
│   - localhost gRPC, auth-token handshake                         │
│   - wraps go.mau.fi/whatsmeow                                    │
│   - session storage in ./sessions/<account_key>/                 │
└─────────────────────────────┬────────────────────────────────────┘
                              │ WhatsApp Web protocol
                              ▼
                       WhatsApp servers
```

This approach eliminates the need for Docker or local Go toolchains. The sidecar binary is embedded within the platform-specific Python wheel and auto-discovered at runtime.

## Concepts

### FastMeow app
The main entry point, used as an async context manager to manage the lifecycle of the sidecar process and account connections.

### Router
Used to organize handlers. Routers can be mounted into the main app or other routers, allowing for modular bot design.

### F magic filter
A powerful, composable tool for event filtering. Supports logical operators like `&` (and), `|` (or), and `~` (not). Filters are evaluated in order; the first match wins.

### Ctx
The context object passed to every handler. It carries `account_key`, `account_jid`, and helper methods like `ctx.reply()` for quick responses.

### AccountHandle
Returned by `app.add_account()`. It provides methods to check connection status (`.ready()`) and retrieve account information like the JID.

### Multi-account
FastMeow supports managing multiple WhatsApp accounts within a single app instance. Each account operates with its own isolated session directory.

### QR pairing
Set `on_qr="terminal"` in `add_account` to print a QR code for initial pairing. Session data is persisted locally for automatic reconnection on subsequent starts.

## Multi-account example
Manage multiple identities with a single router:

```python
async with FastMeow(session_dir=session_dir) as app:
    app.include_router(router)
    
    # Alice and Bob share the same message handlers
    alice = app.add_account("alice", on_qr="terminal")
    bob = app.add_account("bob", on_qr="terminal")
    
    await asyncio.gather(alice.ready(), bob.ready())
    await app.run_forever()
```
Inside a handler, `ctx.account_key` will be "alice" or "bob" depending on which account received the event.

## Public API at a glance
Top-level exports from `fastmeow`:

- **Core**: `FastMeow`, `AccountHandle`
- **Routing**: `Router`, `SkipHandler`, `F`, `Filter`, `FilterResult`
- **Context**: `Ctx`, `AccountClient`
- **Events**: `Event`, `MessageEvent`, `ConnectedEvent`, `DisconnectedEvent`, `QREvent`, `PairSuccessEvent`, `LoggedOutEvent`, `UnknownEvent`
- **Domain**: `Account`, `AccountState`, `SendResult`
- **Exceptions**: `FastMeowError`, `ConfigurationError`, `AccountError`, `AccountAlreadyExistsError`, `AccountNotFoundError`, `MessagingError`, `MessageSendError`, `InvalidJIDError`, `ReplyNotAvailableError`, `PairingFailedError`, `PairingTimeoutError`, `SidecarError`, `SidecarStartupError`, `SidecarCrashedError`, `SidecarBinaryNotFoundError`, `TransportError`, `ManifestError`, `DispatchError`, `BackpressureError`, `HandlerSignatureError`

## Supported platforms
FastMeow provides pre-compiled sidecar binaries for:
- **Linux**: x86_64 (manylinux2014)
- **macOS**: arm64 (12.0+)
- **Windows**: x86_64

Requirements: Python 3.12+.

## Roadmap
- **Phase 3 ✅**: Release pipeline and multi-platform wheel automation.
- **Near-term**:
  - Official PyPI publication.
  - Expanded event types (presence, receipts).
  - Group management features.
- **Deferred**:
  - Media message support (images, video).
  - Advanced session management UI.

## Development
To set up a local development environment:
```bash
git clone https://github.com/jianjian2048/fastmeow
cd fastmeow
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src/fastmeow
```

## License
Distributed under the MIT License. See `LICENSE` for details.

## Acknowledgments
- [whatsmeow](https://github.com/tulir/whatsmeow) by tulir: The underlying Go library powering the sidecar.
- [neonize](https://github.com/krypton-byte/neonize): Inspiration for wrapping whatsmeow in Python.
