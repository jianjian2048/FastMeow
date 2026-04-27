# Changelog

All notable changes to FastMeow will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-27

Initial public release. FastMeow is a Pythonic, async-native WhatsApp
automation SDK powered by an embedded `whatsmeow` Go sidecar.

### Added

- **Core SDK** (`fastmeow`)
  - `FastMeow` application class with async context-manager lifecycle.
  - `AccountHandle` returned by `app.add_account()` for status / readiness.
  - `Router` with mountable sub-routers and `include_router()`.
  - `F` magic filter DSL (aiogram-style): attribute access, comparison,
    `&` / `|` / `~` combinators, regex with named-group injection.
  - `Ctx` context object with `account_key`, `account_jid`, and
    `ctx.reply()` helper for `MessageEvent` handlers.
  - Multi-account support: 100+ concurrent accounts on a single host,
    isolated session directories per account.
  - QR pairing via `on_qr="terminal"` with `qrcode` rendered in-terminal.
  - Event types: `MessageEvent`, `ConnectedEvent`, `DisconnectedEvent`,
    `QREvent`, `PairSuccessEvent`, `LoggedOutEvent`, `UnknownEvent`.
  - Domain types: `Account`, `AccountState`, `SendResult`.
  - Typed exception hierarchy rooted at `FastMeowError`
    (`ConfigurationError`, `AccountError`, `MessagingError`, `SidecarError`,
    `TransportError`, `ManifestError`, `DispatchError`,
    `BackpressureError`, `HandlerSignatureError`, plus specific subclasses).

- **Go sidecar** (`cmd/fastmeow-sidecar`)
  - `CGO_ENABLED=0` build — no C toolchain required at install time.
  - One sidecar process per `FastMeow` app instance, managed as a child.
  - Localhost-only gRPC with auth-token handshake.
  - Wraps [`go.mau.fi/whatsmeow`](https://github.com/tulir/whatsmeow).
  - gRPC API: `Ping`, `EnsureAccount`, `Connect`, `Disconnect`, `Logout`,
    `SendMessage`, `StreamEvents`, `Shutdown` (`protocolVersion=1`).
  - Per-account session storage under `<session_dir>/<account_key>/`.

- **Distribution**
  - Pre-built platform wheels:
    - Linux x86_64 (manylinux2014)
    - macOS arm64 (12.0+)
    - Windows x86_64
  - Automated multi-platform release pipeline cross-compiled from a
    single Linux runner (`.github/workflows/release.yml`).
  - PyPI publication via OIDC Trusted Publisher (no API tokens).

- **Documentation**
  - `README.md` (English) and `README.zh-CN.md` (Simplified Chinese).
  - Source code comments and docstrings in Simplified Chinese.
  - Examples: `basic_echo`, `command_bot`, `broadcast`, `multi_account`.

### Requirements

- Python 3.12+
- No Go toolchain or C compiler required for end users.

[Unreleased]: https://github.com/jianjian2048/FastMeow/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jianjian2048/FastMeow/releases/tag/v0.1.0
