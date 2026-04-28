# Changelog

All notable changes to FastMeow will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-04-28

Phase 4.1 (groups) and Phase 4.2 (receipts & presence) bundled into a
single patch release. `protocolVersion` remains `1`; the sidecar wire
format is fully backward-compatible with `0.1.0` clients.

### Added

- **Group management** (Phase 4.1) — 9 new RPCs on `AccountClient`:
  - Read: `list_groups`, `get_group_info`, `preview_group_invite`.
  - Write: `create_group`, `set_group_name`, `set_group_topic`,
    `add_group_participants`, `remove_group_participants`,
    `promote_group_participants`, `demote_group_participants`,
    `get_group_invite_link`, `join_group`, `leave_group`.
  - Domain types: `GroupInfo`, `GroupParticipant`,
    `GroupParticipantAction`, `GroupParticipantUpdateResult`.
  - Events: `GroupInfoEvent`, `JoinedGroupEvent`, `GroupParticipantUpdateEvent`.
  - Router: `@router.on_group_info`, `@router.on_joined_group`,
    `@router.on_group_participants`.
  - Exceptions: `GroupError`, `GroupNotFoundError`,
    `NotInGroupError`, `NotGroupAdminError`, `InviteLinkInvalidError`,
    `InviteLinkRevokedError`.

- **Receipts & presence** (Phase 4.2) — 4 new RPCs and 3 new events:
  - RPCs: `mark_read`, `send_presence`, `send_chat_presence`,
    `subscribe_presence`.
  - Events: `ReceiptEvent`, `PresenceEvent`, `ChatPresenceEvent`.
  - Enums: `ReceiptType`, `PresenceType`, `ChatPresenceState`,
    `ChatPresenceMedia`.
  - Router: `@router.on_receipt`, `@router.on_presence`,
    `@router.on_chat_presence`.
  - Soft-event introspection: the dispatcher automatically requests
    receipts / presence / chat-presence from the sidecar only when the
    application registers at least one matching handler. No opt-in flag
    is required.

### Changed

- `Router` now exposes `has_soft_event_handlers()` (used internally to
  drive `StreamEvents.IncludeSoftEvents`; safe for advanced users).
- README "Public API at a glance" expanded to cover the new surfaces.
- New examples: `examples/group_admin.py`, `examples/typing_indicator.py`.

### Internal

- Go: new `internal/groups` (Phase 4.1, 80%+ coverage),
  `internal/receipts` (87% cov), `internal/presence` (88% cov).
- Test suite grew from **117** (0.1.0) to **186** unit tests; Go test
  suite covers all new converters and handlers.

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

[Unreleased]: https://github.com/jianjian2048/FastMeow/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jianjian2048/FastMeow/compare/v0.1.0...v0.2.1
[0.1.0]: https://github.com/jianjian2048/FastMeow/releases/tag/v0.1.0
