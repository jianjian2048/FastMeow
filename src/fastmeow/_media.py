"""Streaming assembler for media upload / download (internal).

This module is responsible for three concerns only:

* Translating outbound :class:`fastmeow.types.Media` into an async iterator
  of ``SendMediaRequest`` (one ``SendMediaInit`` frame followed by 256 KiB
  ``chunk`` frames).
* Aggregating inbound ``DownloadMediaChunk`` streams into ``bytes``
  (``download_media``) or writing them incrementally to disk
  (``download_media_to``).
* Normalizing ``MediaSource`` inputs (bytes-like or path) into a unified byte
  stream and enforcing minimal contracts (``mime_type`` required, non-empty
  payload, ``MediaKind`` not UNSPECIFIED).

Design rules:

* **Zero intermediate buffering.** bytes-like sources use ``memoryview``
  slicing; path sources are read incrementally with binary ``open()``.
  Downloads are written through ``bytearray`` / file objects in chunks.
* **No MIME sniffing.** FastMeow does not depend on ``mimetypes`` /
  ``filetype``; the caller declares ``mime_type`` -- mirroring the
  ``internal/media`` contract on the Go side.
* **Sync filesystem IO is offloaded.** Python file IO is blocking; this
  module uses :func:`asyncio.to_thread` to keep the event loop responsive
  under 100+ concurrent accounts.

This module performs NO RPC calls; :mod:`fastmeow._transport` wires the
streams into ``GatewayServiceStub.SendMedia`` / ``DownloadMedia``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, TYPE_CHECKING

from ._generated.fastmeow.v1 import gateway_pb2 as pb
from .exceptions import MediaUnsupportedTypeError
from .types import Media, MediaKind, MediaSource

if TYPE_CHECKING:
    from .types import MediaInfo

# Streaming chunk size; mirrors ``internal/media`` on the Go side (256 KiB).
# Smaller values amplify gRPC framing overhead; larger values raise per-frame
# latency. 256 KiB is the empirical sweet spot. Do NOT change without also
# updating the Go side.
CHUNK_SIZE = 256 * 1024


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


@contextmanager
def _open_source(media: Media) -> Iterator[tuple[IO[bytes] | memoryview, int]]:
    """Synchronously open ``media.source`` and yield ``(reader, declared_size)``.

    * bytes-like -> a ``memoryview`` (zero-copy) plus the exact byte length.
    * path-like  -> a binary file object; length comes from ``stat().st_size``.

    ``declared_size`` is sent as ``SendMediaInit.declared_file_length`` so the
    Go sidecar can cross-check the streamed total and raise
    :class:`fastmeow.exceptions.MediaUploadError` on mismatch.
    """
    src = media.source
    if isinstance(src, (bytes, bytearray, memoryview)):
        view = src if isinstance(src, memoryview) else memoryview(src)
        view = view.cast("B") if view.itemsize != 1 else view
        size = view.nbytes
        if size == 0:
            raise MediaUnsupportedTypeError("media.source is empty")
        yield view, size
        return

    if isinstance(src, (str, os.PathLike)):
        path = Path(os.fspath(src))
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise MediaUnsupportedTypeError(
                f"cannot stat media source {path}: {exc}"
            ) from exc
        if size == 0:
            raise MediaUnsupportedTypeError(f"media source {path} is empty")
        fp = path.open("rb")
        try:
            yield fp, size
        finally:
            fp.close()
        return

    raise MediaUnsupportedTypeError(  # pragma: no cover - typing guards this
        f"unsupported media source type: {type(src).__name__}"
    )


def _validate_media(media: Media) -> None:
    """Lightweight pre-flight validation; complements the Go-side checks.

    Python only blocks obvious mistakes to avoid empty round-trips; the
    authoritative limits (size cap, supported kinds, etc.) live in the
    sidecar.
    """
    if media.kind == MediaKind.UNSPECIFIED:
        raise MediaUnsupportedTypeError(
            "media.kind must not be UNSPECIFIED on outbound send"
        )
    if not media.mime_type:
        raise MediaUnsupportedTypeError(
            "media.mime_type is required (FastMeow does not sniff)"
        )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def iter_send_requests(
    *,
    account_key: str,
    to_jid: str,
    media: Media,
    client_msg_id: str,
    quoted_message_id: str = "",
) -> AsyncIterator[pb.SendMediaRequest]:
    """Yield the ``SendMediaRequest`` stream consumed by the sidecar.

    The first frame is ``init`` (metadata + declared length); subsequent
    frames carry ``chunk`` payloads. The sidecar uploads via
    ``UploadReader`` and then dispatches the WhatsApp message.
    """
    _validate_media(media)

    with _open_source(media) as (reader, declared_size):
        init = pb.SendMediaInit(
            account_key=account_key,
            to_jid=to_jid,
            client_msg_id=client_msg_id,
            quoted_message_id=quoted_message_id,
            kind=media.kind.to_proto(),
            mime_type=media.mime_type,
            file_name=media.file_name,
            caption=media.caption,
            thumbnail_jpeg=media.thumbnail,
            voice_note=media.voice_note,
            declared_file_length=declared_size,
        )
        yield pb.SendMediaRequest(init=init)

        sent = 0
        if isinstance(reader, memoryview):
            for offset in range(0, declared_size, CHUNK_SIZE):
                end = min(offset + CHUNK_SIZE, declared_size)
                yield pb.SendMediaRequest(chunk=bytes(reader[offset:end]))
                sent = end
        else:
            while True:
                chunk = await asyncio.to_thread(reader.read, CHUNK_SIZE)
                if not chunk:
                    break
                yield pb.SendMediaRequest(chunk=chunk)
                sent += len(chunk)

        if sent != declared_size:  # pragma: no cover - only on IO faults
            raise MediaUnsupportedTypeError(
                f"streamed {sent} bytes but declared {declared_size}"
            )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def collect_download(
    stream: AsyncIterator[pb.DownloadMediaChunk],
) -> bytes:
    """Aggregate download chunks into in-memory ``bytes``.

    Suitable for small / known-bounded payloads. For large files prefer
    :func:`write_download_to` to avoid the full-buffer memory peak.
    """
    buf = bytearray()
    async for msg in stream:
        if msg.chunk:
            buf.extend(msg.chunk)
    return bytes(buf)


async def write_download_to(
    stream: AsyncIterator[pb.DownloadMediaChunk],
    path: str | os.PathLike[str],
) -> Path:
    """Stream download chunks to ``path`` and return the resolved path.

    * Filesystem IO runs in a worker thread to keep the event loop free.
    * On any failure the partially-written file is best-effort removed;
      removal errors are swallowed so they do not mask the original cause.
    * The parent directory must already exist; this helper does not call
      ``mkdir -p`` -- the caller controls the layout.
    """
    target = Path(os.fspath(path))
    fp: IO[bytes] = await asyncio.to_thread(target.open, "wb")
    try:
        async for msg in stream:
            if msg.chunk:
                await asyncio.to_thread(fp.write, msg.chunk)
    except BaseException:
        await asyncio.to_thread(fp.close)
        with suppress(OSError):  # best-effort cleanup
            await asyncio.to_thread(target.unlink)
        raise
    else:
        await asyncio.to_thread(fp.close)
    return target


def build_download_request(
    *, account_key: str, info: MediaInfo
) -> pb.DownloadMediaRequest:
    """Translate a public :class:`MediaInfo` back into wire form.

    The public surface renames ``file_length`` -> ``size`` and
    ``thumbnail_jpeg`` -> ``thumbnail``; this helper centralizes the reverse
    mapping so call sites stay uniform.
    """
    return pb.DownloadMediaRequest(
        account_key=account_key,
        media=pb.MediaInfo(
            kind=info.kind.to_proto(),
            mime_type=info.mime_type,
            file_length=info.size,
            direct_path=info.direct_path,
            media_key=info.media_key,
            file_sha256=info.file_sha256,
            file_enc_sha256=info.file_enc_sha256,
            caption=info.caption,
            file_name=info.file_name,
            thumbnail_jpeg=info.thumbnail,
            width=info.width,
            height=info.height,
            duration_seconds=info.duration_seconds,
            voice_note=info.voice_note,
            animated=info.animated,
            url=info.url,
        ),
    )


# ---------------------------------------------------------------------------
# Typed convenience adapters (Phase 4.3 Batch 5)
# ---------------------------------------------------------------------------
#
# The public ``send_image`` / ``send_video`` / ``send_audio`` / ``send_document``
# / ``send_sticker`` helpers all delegate here. We keep the module-level
# constructor table and the ``build_media`` factory private (no underscore-free
# re-exports) so the ``Media`` dataclass remains the single authoritative
# wire-shape; the typed wrappers are pure ergonomics.
#
# MIME defaults follow the most common WhatsApp-side encodings:
# * IMAGE   -> ``image/jpeg``  (most camera roll uploads land as JPEG)
# * VIDEO   -> ``video/mp4``
# * AUDIO   -> ``audio/ogg; codecs=opus`` for voice notes (PTT requirement),
#              ``audio/mpeg`` otherwise.
# * DOC     -> ``application/octet-stream`` (caller usually overrides)
# * STICKER -> ``image/webp``
#
# We deliberately do NOT call ``mimetypes.guess_type`` to keep the SDK free of
# implicit OS-dependent behavior; the caller may always pass ``mime_type=``
# explicitly to override the default.

_DEFAULT_MIME: dict[MediaKind, str] = {
    MediaKind.IMAGE: "image/jpeg",
    MediaKind.VIDEO: "video/mp4",
    MediaKind.AUDIO: "audio/mpeg",
    MediaKind.DOCUMENT: "application/octet-stream",
    MediaKind.STICKER: "image/webp",
}

_VOICE_NOTE_MIME = "audio/ogg; codecs=opus"


def _default_file_name(source: MediaSource) -> str:
    """Best-effort filename extraction for ``DOCUMENT``-style payloads.

    Path-like sources fall back to ``Path(...).name``; bytes-like sources
    cannot infer a name and return ``""`` -- callers must pass ``file_name=``
    explicitly when sending a document from memory.
    """
    if isinstance(source, (str, os.PathLike)):
        return Path(os.fspath(source)).name
    return ""


def build_media(
    kind: MediaKind,
    source: MediaSource,
    *,
    mime_type: str | None = None,
    file_name: str | None = None,
    caption: str = "",
    thumbnail: bytes | None = None,
    voice_note: bool = False,
) -> Media:
    """Construct a :class:`Media` instance for typed convenience senders.

    This is the single funnel through which ``send_image`` / ``send_video`` /
    ``send_audio`` / ``send_document`` / ``send_sticker`` must pass. Keeping
    one constructor here means default-MIME / default-filename policy is
    centralized and trivially auditable.
    """
    if kind == MediaKind.UNSPECIFIED:
        raise MediaUnsupportedTypeError(
            "build_media requires a concrete MediaKind, not UNSPECIFIED"
        )
    if kind == MediaKind.AUDIO and voice_note and mime_type is None:
        resolved_mime = _VOICE_NOTE_MIME
    else:
        resolved_mime = mime_type if mime_type is not None else _DEFAULT_MIME[kind]
    resolved_name = (
        file_name if file_name is not None else _default_file_name(source)
    )
    return Media(
        kind=kind,
        source=source,
        mime_type=resolved_mime,
        file_name=resolved_name,
        caption=caption,
        thumbnail=thumbnail if thumbnail is not None else b"",
        voice_note=voice_note,
    )
