# Changelog

All notable changes to FastMeow will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-04-28

Phase 4.3 — media messages. Adds outbound and inbound media support
(image / video / audio / document / sticker) with streaming upload and
download between Python and the embedded Go sidecar. The wire format
gains a new ``SendMedia`` client-streaming RPC, a ``DownloadMedia``
server-streaming RPC, and a ``MediaMessageEvent`` oneof on
``StreamEventsResponse``. ``protocolVersion`` is bumped from ``1`` to
``2``; older ``0.2.x`` clients can still connect against the new
sidecar at the protocol level (no breaking changes to existing RPCs)
but will not see media events.

### Added

- **Typed send helpers** on ``AccountClient`` (and forwarded on
  ``Ctx``):
  - ``send_image`` / ``send_video`` / ``send_audio`` /
    ``send_document`` / ``send_sticker``.
  - Mirror ``Ctx.reply_image`` / ``reply_video`` / ``reply_audio`` /
    ``reply_document`` / ``reply_sticker`` for inline replies that
    automatically quote the source ``MessageEvent``.
  - Generic ``send_media`` accepting a fully-built ``Media`` for
    advanced use cases.
- **Inbound media** on ``MessageEvent``:
  - New optional fields ``caption`` and ``media`` (a ``MediaInfo``).
  - Predicates ``has_media``, ``is_image``, ``is_video``, ``is_audio``,
    ``is_document``, ``is_sticker`` for routing.
  - ``F.has_media`` / ``F.is_image`` / ``F.is_video`` / ``F.is_audio``
    / ``F.is_document`` / ``F.is_sticker`` magic-filter shortcuts.
- **Lazy download helpers**:
  - ``download_media(MediaInfo)`` — collect into ``bytes``.
  - ``download_media_to(MediaInfo, path)`` — stream straight to disk
    (256 KiB chunks; partial files are cleaned up on failure).
  - Available on both ``AccountClient`` and ``Ctx`` (download is not
    gated to ``MessageEvent`` contexts).
- **Domain types** exported from ``fastmeow``: ``Media``, ``MediaInfo``,
  ``MediaKind``, ``MediaSource``.
- **Exceptions**: ``MediaError`` base + ``MediaUnsupportedTypeError``,
  ``MediaUploadError``, ``MediaDownloadError``,
  ``MediaSizeLimitError`` for sidecar-side and validation failures.
- **Example**: ``examples/media_bot.py`` — sends images / documents /
  voice notes on command and archives all inbound media to disk.

### Changed

- ``protocolVersion`` handshake bumped from ``1`` to ``2``. Existing
  ``0.2.x`` Python clients can still complete the handshake against the
  new sidecar; they will simply ignore unknown ``MediaMessageEvent``
  frames.
- ``MessageEvent`` now carries optional ``caption`` and ``media``
  metadata. Existing handlers that ignore these fields are unaffected.
- ``__version__`` in ``fastmeow/__init__.py`` is now in lockstep with
  ``pyproject.toml`` (was previously stale at ``"0.1.0"``).
- ``README.md`` Public API section expanded to cover the new media
  surface.

### Fixed

- Pure media messages (no caption) are no longer silently dropped by
  the event translator — they now arrive as ``MessageEvent`` with
  ``media`` populated and an empty ``text`` body.

### Internal

- New Go package ``internal/media`` (whatsmeow ``Upload`` /
  ``Download`` wrappers, 88% coverage).
- New Python module ``fastmeow/_media.py`` (256 KiB streaming
  assembler; zero intermediate buffering for bytes-like sources).
- Test suite: **186 → 217+** unit tests; new ``tests/test_media.py``
  (55 tests) covers the assembler, the typed send helpers, the
  ``Ctx.reply_*`` guards, ``MessageEvent`` predicates, ``F``
  shortcuts, and the proto round-trip.

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

[Unreleased]: https://github.com/jianjian2048/FastMeow/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jianjian2048/FastMeow/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jianjian2048/FastMeow/compare/v0.1.0...v0.2.1
[0.1.0]: https://github.com/jianjian2048/FastMeow/releases/tag/v0.1.0
