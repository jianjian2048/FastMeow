"""Account manifest: the single source of truth mapping
``account_key`` -> ``jid``.

The Go sidecar is intentionally stateless about the user-chosen account
key; it knows JIDs (those live in its sqlite session store) but does
**not** persist the human-readable ``account_key`` -> ``jid`` mapping.
Holding that mapping on the Python side serves two goals:

1. **Stable identifiers across re-pairings.** When a user logs out and
   re-pairs ``"alice"``, the JID changes but the user code keeps using
   ``"alice"`` everywhere.
2. **Crash-safe registry.** If the sidecar crashes mid-session and
   restarts, FastMeow needs to know which accounts to re-attach. The
   manifest is replayed on startup.

Concurrency model
-----------------
A single :class:`Manifest` instance is owned by one
:class:`fastmeow.FastMeow` app. All mutations go through ``async``
methods guarded by an :class:`asyncio.Lock`, so concurrent
``add_account`` calls from user code are safe.

Cross-process safety: the on-disk file is written atomically (tmp +
``os.replace``) and protected by a best-effort exclusive file lock
(``msvcrt`` on Windows, ``fcntl`` on POSIX). Two FastMeow processes
sharing the same ``session-dir`` is **not a supported configuration**
(the underlying sqlite session store doesn't support it either), but
the lock prevents the most common foot-gun: starting the app twice by
accident from two terminals.

File format
-----------
JSON with a wrapping object so we can add fields later without
breaking the parser:

.. code-block:: json

    {
      "version": 1,
      "accounts": {
        "alice": {"jid": "1234567890:7@s.whatsapp.net"},
        "bob":   {"jid": ""}
      }
    }

Empty string ``jid`` means "registered but never paired yet".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from fastmeow.exceptions import ManifestError

log = logging.getLogger(__name__)

_MANIFEST_VERSION = 1
_FILENAME = "accounts.json"
_LOCKFILE = "accounts.lock"


__all__ = ["Manifest", "ManifestEntry"]


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One row in the manifest."""

    account_key: str
    jid: str
    """Empty string means the account is registered but unpaired."""

    @property
    def is_paired(self) -> bool:
        return bool(self.jid)


# ---------------------------------------------------------------------------
# Cross-platform exclusive file lock
# ---------------------------------------------------------------------------


class _FileLock:
    """Best-effort exclusive lock on a sentinel file.

    Used only to detect "another FastMeow process is already running on
    this session-dir". Released on context exit. Never blocks: on
    contention we raise immediately with a friendly message.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        # Open or create. We hold the handle for the lifetime of the
        # Manifest; closing it releases the OS-level lock.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+b")  # noqa: SIM115 — closed in release()
        try:
            if sys.platform == "win32":
                import msvcrt

                # Lock 1 byte at offset 0; LK_NBLCK = non-blocking.
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise ManifestError(
                f"Another FastMeow process appears to be using session "
                f"directory {self._path.parent} (failed to acquire "
                f"exclusive lock on {self._path.name}). "
                f"Stop the other process or pick a different session_dir."
            ) from exc

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    # Already released or never acquired; ignore.
                    pass
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class Manifest:
    """In-memory + on-disk account_key -> jid registry.

    Construct via :meth:`open` (async) which acquires the file lock and
    loads any pre-existing JSON. Use as an async context manager or call
    :meth:`close` explicitly when shutting down.
    """

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir
        self._path = session_dir / _FILENAME
        self._lock_path = session_dir / _LOCKFILE
        self._file_lock = _FileLock(self._lock_path)
        # Maps account_key -> jid (jid may be empty string).
        self._entries: dict[str, str] = {}
        self._mutex = asyncio.Lock()
        self._opened = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def open(cls, session_dir: Path | str) -> Manifest:
        """Acquire the inter-process lock and load the manifest from disk."""
        m = cls(Path(session_dir))
        await asyncio.to_thread(m._open_sync)
        return m

    def _open_sync(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._file_lock.acquire()
        try:
            self._entries = self._load_from_disk()
        except Exception:
            # If we fail to load, drop the lock so we don't leave a
            # zombie sentinel file behind.
            self._file_lock.release()
            raise
        self._opened = True

    async def close(self) -> None:
        if not self._opened:
            return
        await asyncio.to_thread(self._file_lock.release)
        self._opened = False

    async def __aenter__(self) -> Manifest:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- read APIs (no locking needed: dict reads are atomic in CPython
    #    and we don't expose mutation through these) ---------------------

    def __contains__(self, account_key: object) -> bool:
        return isinstance(account_key, str) and account_key in self._entries

    def __iter__(self) -> Iterator[ManifestEntry]:
        for key, jid in self._entries.items():
            yield ManifestEntry(account_key=key, jid=jid)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, account_key: str) -> ManifestEntry | None:
        jid = self._entries.get(account_key)
        if jid is None:
            return None
        return ManifestEntry(account_key=account_key, jid=jid)

    def jid_for(self, account_key: str) -> str:
        """Return the JID for ``account_key``, or empty string if unpaired.

        Raises :class:`KeyError` if the key is unknown. Use ``in`` first
        if you're not sure.
        """
        return self._entries[account_key]

    def keys(self) -> list[str]:
        return list(self._entries.keys())

    # -- mutation APIs -----------------------------------------------------

    async def register(self, account_key: str, *, jid: str = "") -> ManifestEntry:
        """Register a new account or no-op if it already exists with the
        same JID.

        If the account exists with a *different* non-empty JID, raises
        :class:`ManifestError` rather than silently clobbering — the
        sidecar's session store is the authority on JIDs and we never
        want to drift away from it.
        """
        async with self._mutex:
            self._require_open()
            existing = self._entries.get(account_key)
            if existing is not None:
                # Allow upgrading "" -> real JID (post-pair flow), but
                # never overwrite a non-empty JID with a different one.
                if existing and jid and existing != jid:
                    raise ManifestError(
                        f"account_key {account_key!r} already mapped to "
                        f"jid {existing!r}; refusing to overwrite with "
                        f"{jid!r}"
                    )
                if existing == jid or not jid:
                    return ManifestEntry(account_key=account_key, jid=existing)
            self._entries[account_key] = jid
            await asyncio.to_thread(self._write_atomic, dict(self._entries))
            return ManifestEntry(account_key=account_key, jid=jid)

    async def update_jid(self, account_key: str, jid: str) -> ManifestEntry:
        """Bind a JID to an already-registered account_key.

        Called by the dispatcher when a ``PairSuccessEvent`` arrives.
        Idempotent if the JID is already correct. Raises
        :class:`ManifestError` on drift.
        """
        async with self._mutex:
            self._require_open()
            existing = self._entries.get(account_key)
            if existing is None:
                raise ManifestError(f"cannot update_jid for unknown account_key {account_key!r}")
            if existing and existing != jid:
                raise ManifestError(
                    f"account_key {account_key!r} already paired to "
                    f"{existing!r}; sidecar reported new jid {jid!r}. "
                    f"This usually means the underlying session store "
                    f"was deleted out from under us."
                )
            if existing == jid:
                return ManifestEntry(account_key=account_key, jid=jid)
            self._entries[account_key] = jid
            await asyncio.to_thread(self._write_atomic, dict(self._entries))
            return ManifestEntry(account_key=account_key, jid=jid)

    async def remove(self, account_key: str) -> None:
        """Forget an account. Logs a warning if the key is unknown but
        does not raise — removal is idempotent by design.
        """
        async with self._mutex:
            self._require_open()
            if account_key not in self._entries:
                log.warning("manifest.remove: unknown account_key %r", account_key)
                return
            del self._entries[account_key]
            await asyncio.to_thread(self._write_atomic, dict(self._entries))

    # -- internals ---------------------------------------------------------

    def _require_open(self) -> None:
        if not self._opened:
            raise ManifestError("manifest is closed")

    def _load_from_disk(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestError(f"failed to read manifest {self._path}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest {self._path} is corrupt (invalid JSON): {exc}") from exc
        return self._parse(data)

    @staticmethod
    def _parse(data: Any) -> dict[str, str]:
        if not isinstance(data, dict):
            raise ManifestError("manifest root must be a JSON object")
        version = data.get("version")
        if version != _MANIFEST_VERSION:
            raise ManifestError(
                f"manifest version {version!r} is not supported by this "
                f"FastMeow build (expected {_MANIFEST_VERSION}). Refusing "
                f"to start to avoid silent data loss."
            )
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            raise ManifestError("manifest 'accounts' must be a JSON object")
        out: dict[str, str] = {}
        for key, val in accounts.items():
            if not isinstance(key, str) or not key:
                raise ManifestError(f"manifest account key must be non-empty string, got {key!r}")
            if not isinstance(val, dict):
                raise ManifestError(
                    f"manifest entry for {key!r} must be an object, got {type(val).__name__}"
                )
            jid = val.get("jid", "")
            if not isinstance(jid, str):
                raise ManifestError(
                    f"manifest jid for {key!r} must be a string, got {type(jid).__name__}"
                )
            out[key] = jid
        return out

    def _write_atomic(self, snapshot: dict[str, str]) -> None:
        """Write ``snapshot`` to disk atomically.

        Uses tmp-file-in-same-dir + ``os.replace`` so that readers either
        see the old file or the new file, never a half-written one.
        """
        payload: dict[str, Any] = {
            "version": _MANIFEST_VERSION,
            "accounts": {key: {"jid": jid} for key, jid in sorted(snapshot.items())},
        }
        body = json.dumps(payload, indent=2, sort_keys=False) + "\n"

        # NamedTemporaryFile on Windows can't be reopened, so we manage
        # the tmp file by hand.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".accounts.", suffix=".tmp", dir=str(self._session_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            # Best-effort cleanup; swallow secondary errors.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
