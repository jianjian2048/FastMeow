"""Phase 4.3 媒体单元测试。

覆盖范围（plan §2.10）：

* ``_media.build_media``：默认 MIME 表 / 语音条 / file_name 推断 / UNSPECIFIED 守卫。
* ``_media.iter_send_requests``：按 256 KiB 分帧、init 字段、空源拒绝。
* ``_media.collect_download`` / ``write_download_to``：聚合 / 流式落盘 / 失败清理。
* ``_BoundClient`` typed send：5 路 typed 方法转发到 transport 时 ``Media``
  字段（kind / mime / file_name / voice_note）正确组装。
* ``Ctx.reply_*``：仅 :class:`MessageEvent` 上下文允许；其他事件抛
  :class:`ReplyNotAvailableError`，且默认引用源消息。
* ``Ctx.download_media`` / ``download_media_to``：任意上下文可用。
* ``MessageEvent`` 媒体谓词与 ``F.has_media`` / ``F.is_image`` 等魔法过滤器。
* ``MediaKind`` proto 双向映射；``MediaInfo`` from_proto 往返。
* ``event_from_proto`` 处理 ``media_message`` oneof 折叠为 :class:`MessageEvent`。

这些测试不依赖真实 sidecar；通过最小 fake transport / pb 构造直接驱动公开层。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fastmeow import (
    AccountClient,
    ConnectedEvent,
    Ctx,
    F,
    MessageEvent,
)
from fastmeow._dispatcher import _BoundClient
from fastmeow._generated.fastmeow.v1 import gateway_pb2 as pb
from fastmeow._media import (
    CHUNK_SIZE,
    build_media,
    collect_download,
    iter_send_requests,
    write_download_to,
)
from fastmeow.exceptions import (
    MediaUnsupportedTypeError,
    ReplyNotAvailableError,
)
from fastmeow.types import (
    Media,
    MediaInfo,
    MediaKind,
    SendResult,
    event_from_proto,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)


def _send_result(message_id: str = "WAID") -> SendResult:
    return SendResult(message_id=message_id, server_timestamp=_FIXED_TS, deduped=False)


def _media_info(
    *,
    kind: MediaKind = MediaKind.IMAGE,
    mime_type: str = "image/jpeg",
    size: int = 4,
) -> MediaInfo:
    return MediaInfo(
        kind=kind,
        mime_type=mime_type,
        size=size,
        direct_path="/v/t/some-path.enc",
        media_key=b"\x01" * 32,
        file_sha256=b"\x02" * 32,
        file_enc_sha256=b"\x03" * 32,
    )


class FakeMediaTransport:
    """最小 transport：只实现 send_media / download_media* 用于 _BoundClient 测试。"""

    def __init__(
        self,
        *,
        download_payload: bytes = b"hello-bytes",
        send_result: SendResult | None = None,
        send_raises: BaseException | None = None,
    ) -> None:
        self.send_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.download_to_calls: list[dict[str, Any]] = []
        self._download_payload = download_payload
        self._send_result = send_result or _send_result()
        self._send_raises = send_raises

    async def send_media(
        self,
        *,
        account_key: str,
        to_jid: str,
        media: Media,
        client_msg_id: str | None = None,
        quoted_message_id: str = "",
    ) -> SendResult:
        self.send_calls.append(
            {
                "account_key": account_key,
                "to_jid": to_jid,
                "media": media,
                "client_msg_id": client_msg_id,
                "quoted_message_id": quoted_message_id,
            }
        )
        if self._send_raises is not None:
            raise self._send_raises
        return self._send_result

    async def download_media(
        self, *, account_key: str, info: MediaInfo
    ) -> bytes:
        self.download_calls.append({"account_key": account_key, "info": info})
        return self._download_payload

    async def download_media_to(
        self, *, account_key: str, info: MediaInfo, path: str | Path
    ) -> Path:
        target = Path(path)
        target.write_bytes(self._download_payload)
        self.download_to_calls.append(
            {"account_key": account_key, "info": info, "path": target}
        )
        return target


def _make_bound(
    transport: FakeMediaTransport | None = None,
) -> tuple[_BoundClient, FakeMediaTransport]:
    tx = transport or FakeMediaTransport()
    bound = _BoundClient(_transport=tx, account_key="alice", jid="1@s.whatsapp.net")  # type: ignore[arg-type]
    return bound, tx


_BASE_FIELDS: dict[str, Any] = {
    "seq": 1,
    "sidecar_id": "default",
    "account_key": "alice",
    "account_jid": "1@s.whatsapp.net",
    "observed_at": _FIXED_TS,
}


def _make_message_event(*, message_id: str = "MSG1", text: str = "hi") -> MessageEvent:
    return MessageEvent(
        **_BASE_FIELDS,
        message_id=message_id,
        chat_jid="2@s.whatsapp.net",
        sender_jid="2@s.whatsapp.net",
        from_me=False,
        is_group=False,
        text=text,
    )


def _make_connected_event() -> ConnectedEvent:
    return ConnectedEvent(**_BASE_FIELDS)


def _make_ctx(
    *,
    event: Any,
    transport: FakeMediaTransport | None = None,
) -> tuple[Ctx, FakeMediaTransport]:
    bound, tx = _make_bound(transport)
    client: AccountClient = bound  # type: ignore[assignment]
    ctx = Ctx(
        account_key="alice",
        account_jid="1@s.whatsapp.net",
        event=event,
        client=client,
    )
    return ctx, tx


# ---------------------------------------------------------------------------
# 1. build_media — 默认值 / 守卫
# ---------------------------------------------------------------------------


def test_build_media_image_default_mime_and_filename() -> None:
    m = build_media(MediaKind.IMAGE, b"raw")
    assert m.kind is MediaKind.IMAGE
    assert m.mime_type == "image/jpeg"
    assert m.file_name == ""
    assert m.voice_note is False
    assert m.thumbnail == b""


def test_build_media_video_default_mime() -> None:
    m = build_media(MediaKind.VIDEO, b"raw")
    assert m.mime_type == "video/mp4"


def test_build_media_audio_default_mime() -> None:
    m = build_media(MediaKind.AUDIO, b"raw")
    assert m.mime_type == "audio/mpeg"
    assert m.voice_note is False


def test_build_media_audio_voice_note_switches_to_opus() -> None:
    m = build_media(MediaKind.AUDIO, b"raw", voice_note=True)
    assert m.mime_type == "audio/ogg; codecs=opus"
    assert m.voice_note is True


def test_build_media_audio_voice_note_respects_explicit_mime() -> None:
    m = build_media(
        MediaKind.AUDIO, b"raw", voice_note=True, mime_type="audio/aac"
    )
    assert m.mime_type == "audio/aac"
    assert m.voice_note is True


def test_build_media_document_default_mime_and_path_filename(tmp_path: Path) -> None:
    f = tmp_path / "report.pdf"
    f.write_bytes(b"PDF-data")
    m = build_media(MediaKind.DOCUMENT, f)
    assert m.mime_type == "application/octet-stream"
    assert m.file_name == "report.pdf"


def test_build_media_document_bytes_has_no_default_filename() -> None:
    m = build_media(MediaKind.DOCUMENT, b"PDF-data")
    assert m.file_name == ""


def test_build_media_sticker_default_mime() -> None:
    m = build_media(MediaKind.STICKER, b"raw")
    assert m.mime_type == "image/webp"


def test_build_media_unspecified_rejected() -> None:
    with pytest.raises(MediaUnsupportedTypeError):
        build_media(MediaKind.UNSPECIFIED, b"raw")


def test_build_media_explicit_overrides_filename() -> None:
    m = build_media(
        MediaKind.DOCUMENT, b"data", file_name="custom.bin", mime_type="text/plain"
    )
    assert m.file_name == "custom.bin"
    assert m.mime_type == "text/plain"


# ---------------------------------------------------------------------------
# 2. iter_send_requests — 帧化 / init 字段
# ---------------------------------------------------------------------------


async def _drain(it: AsyncIterator[pb.SendMediaRequest]) -> list[pb.SendMediaRequest]:
    out: list[pb.SendMediaRequest] = []
    async for frame in it:
        out.append(frame)
    return out


@pytest.mark.asyncio
async def test_iter_send_requests_init_then_chunks_bytes() -> None:
    payload = b"\x00" * (CHUNK_SIZE + 100)
    media = Media(
        kind=MediaKind.IMAGE,
        source=payload,
        mime_type="image/jpeg",
        caption="hi",
    )
    frames = await _drain(
        iter_send_requests(
            account_key="alice",
            to_jid="2@s.whatsapp.net",
            media=media,
            client_msg_id="cmsg",
        )
    )
    # 1 init + 2 chunks (CHUNK_SIZE + 100 -> 2 frames)
    assert len(frames) == 3
    assert frames[0].WhichOneof("frame") == "init"
    assert frames[0].init.declared_file_length == len(payload)
    assert frames[0].init.account_key == "alice"
    assert frames[0].init.to_jid == "2@s.whatsapp.net"
    assert frames[0].init.client_msg_id == "cmsg"
    assert frames[0].init.caption == "hi"
    assert frames[0].init.kind == pb.MediaKind.MEDIA_KIND_IMAGE
    assert frames[1].WhichOneof("frame") == "chunk"
    assert frames[2].WhichOneof("frame") == "chunk"
    assert len(frames[1].chunk) == CHUNK_SIZE
    assert len(frames[2].chunk) == 100


@pytest.mark.asyncio
async def test_iter_send_requests_path_streaming(tmp_path: Path) -> None:
    payload = b"\x42" * (CHUNK_SIZE * 2)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)
    media = Media(
        kind=MediaKind.DOCUMENT,
        source=f,
        mime_type="application/octet-stream",
        file_name="big.bin",
    )
    frames = await _drain(
        iter_send_requests(
            account_key="alice",
            to_jid="2@s.whatsapp.net",
            media=media,
            client_msg_id="cmsg",
        )
    )
    assert frames[0].init.file_name == "big.bin"
    assert frames[0].init.declared_file_length == len(payload)
    chunks = [f.chunk for f in frames[1:]]
    assert b"".join(chunks) == payload
    assert all(len(c) <= CHUNK_SIZE for c in chunks)


@pytest.mark.asyncio
async def test_iter_send_requests_empty_bytes_rejected() -> None:
    media = Media(kind=MediaKind.IMAGE, source=b"", mime_type="image/jpeg")
    with pytest.raises(MediaUnsupportedTypeError):
        await _drain(
            iter_send_requests(
                account_key="alice",
                to_jid="2",
                media=media,
                client_msg_id="cmsg",
            )
        )


@pytest.mark.asyncio
async def test_iter_send_requests_unspecified_rejected() -> None:
    media = Media(
        kind=MediaKind.UNSPECIFIED, source=b"raw", mime_type="application/octet-stream"
    )
    with pytest.raises(MediaUnsupportedTypeError):
        await _drain(
            iter_send_requests(
                account_key="alice", to_jid="2", media=media, client_msg_id="x"
            )
        )


@pytest.mark.asyncio
async def test_iter_send_requests_missing_mime_rejected() -> None:
    media = Media(kind=MediaKind.IMAGE, source=b"raw", mime_type="")
    with pytest.raises(MediaUnsupportedTypeError):
        await _drain(
            iter_send_requests(
                account_key="alice", to_jid="2", media=media, client_msg_id="x"
            )
        )


# ---------------------------------------------------------------------------
# 3. download — collect / write_to / 失败清理
# ---------------------------------------------------------------------------


async def _gen(chunks: list[bytes]) -> AsyncIterator[pb.DownloadMediaChunk]:
    for c in chunks:
        yield pb.DownloadMediaChunk(chunk=c)


@pytest.mark.asyncio
async def test_collect_download_aggregates_chunks() -> None:
    out = await collect_download(_gen([b"foo", b"bar", b"baz"]))
    assert out == b"foobarbaz"


@pytest.mark.asyncio
async def test_write_download_to_streams_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    result = await write_download_to(_gen([b"foo", b"bar"]), target)
    assert result == target
    assert target.read_bytes() == b"foobar"


@pytest.mark.asyncio
async def test_write_download_to_cleans_partial_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "broken.bin"

    async def boom() -> AsyncIterator[pb.DownloadMediaChunk]:
        yield pb.DownloadMediaChunk(chunk=b"partial")
        raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError, match="stream failed"):
        await write_download_to(boom(), target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# 4. _BoundClient typed send — 转发字段正确
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_send_image_uses_default_mime_and_caption() -> None:
    bound, tx = _make_bound()
    res = await bound.send_image(
        "2@s.whatsapp.net", b"img-bytes", caption="hi"
    )
    assert res.message_id == "WAID"
    assert len(tx.send_calls) == 1
    call = tx.send_calls[0]
    assert call["account_key"] == "alice"
    assert call["to_jid"] == "2@s.whatsapp.net"
    assert call["quoted_message_id"] == ""
    media = call["media"]
    assert media.kind is MediaKind.IMAGE
    assert media.mime_type == "image/jpeg"
    assert media.caption == "hi"


@pytest.mark.asyncio
async def test_bound_send_video_default_mime() -> None:
    bound, tx = _make_bound()
    await bound.send_video("2@s", b"video-bytes")
    media = tx.send_calls[0]["media"]
    assert media.kind is MediaKind.VIDEO
    assert media.mime_type == "video/mp4"


@pytest.mark.asyncio
async def test_bound_send_audio_voice_note_uses_opus() -> None:
    bound, tx = _make_bound()
    await bound.send_audio("2@s", b"audio", voice_note=True)
    media = tx.send_calls[0]["media"]
    assert media.kind is MediaKind.AUDIO
    assert media.voice_note is True
    assert media.mime_type == "audio/ogg; codecs=opus"


@pytest.mark.asyncio
async def test_bound_send_document_path_infers_filename(tmp_path: Path) -> None:
    f = tmp_path / "spec.txt"
    f.write_text("hi", encoding="utf-8")
    bound, tx = _make_bound()
    await bound.send_document("2@s", f)
    media = tx.send_calls[0]["media"]
    assert media.kind is MediaKind.DOCUMENT
    assert media.file_name == "spec.txt"
    assert media.mime_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_bound_send_sticker_default_webp() -> None:
    bound, tx = _make_bound()
    await bound.send_sticker("2@s", b"webp-bytes")
    media = tx.send_calls[0]["media"]
    assert media.kind is MediaKind.STICKER
    assert media.mime_type == "image/webp"


@pytest.mark.asyncio
async def test_bound_send_media_quoted_id_passthrough() -> None:
    bound, tx = _make_bound()
    media = build_media(MediaKind.IMAGE, b"raw")
    await bound.send_media("2@s", media, quoted_message_id="QID")
    assert tx.send_calls[0]["quoted_message_id"] == "QID"


@pytest.mark.asyncio
async def test_bound_download_media_returns_bytes() -> None:
    bound, tx = _make_bound(FakeMediaTransport(download_payload=b"PAYLOAD"))
    info = _media_info()
    out = await bound.download_media(info)
    assert out == b"PAYLOAD"
    assert tx.download_calls == [{"account_key": "alice", "info": info}]


@pytest.mark.asyncio
async def test_bound_download_media_to_writes_file(tmp_path: Path) -> None:
    bound, tx = _make_bound(FakeMediaTransport(download_payload=b"DISK"))
    info = _media_info()
    target = tmp_path / "saved.bin"
    out = await bound.download_media_to(info, target)
    assert out == target
    assert target.read_bytes() == b"DISK"
    assert tx.download_to_calls[0]["path"] == target


# ---------------------------------------------------------------------------
# 5. Ctx.reply_* — 守卫 / 引用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctx_reply_image_quotes_by_default() -> None:
    ctx, tx = _make_ctx(event=_make_message_event())
    await ctx.reply_image(b"img", caption="cap")
    call = tx.send_calls[0]
    assert call["to_jid"] == "2@s.whatsapp.net"
    assert call["quoted_message_id"] == "MSG1"
    assert call["media"].caption == "cap"


@pytest.mark.asyncio
async def test_ctx_reply_image_quoted_false_drops_quote() -> None:
    ctx, tx = _make_ctx(event=_make_message_event())
    await ctx.reply_image(b"img", quoted=False)
    assert tx.send_calls[0]["quoted_message_id"] == ""


@pytest.mark.asyncio
async def test_ctx_reply_audio_voice_note_propagates() -> None:
    ctx, tx = _make_ctx(event=_make_message_event())
    await ctx.reply_audio(b"a", voice_note=True)
    media = tx.send_calls[0]["media"]
    assert media.voice_note is True
    assert media.mime_type == "audio/ogg; codecs=opus"


@pytest.mark.asyncio
async def test_ctx_reply_document_explicit_filename() -> None:
    ctx, tx = _make_ctx(event=_make_message_event())
    await ctx.reply_document(b"data", file_name="x.bin", caption="c")
    media = tx.send_calls[0]["media"]
    assert media.file_name == "x.bin"
    assert media.caption == "c"


@pytest.mark.asyncio
async def test_ctx_reply_sticker_round_trip() -> None:
    ctx, tx = _make_ctx(event=_make_message_event())
    await ctx.reply_sticker(b"webp")
    assert tx.send_calls[0]["media"].kind is MediaKind.STICKER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["reply_image", "reply_video", "reply_audio", "reply_document", "reply_sticker"],
)
async def test_ctx_reply_media_rejects_non_message_event(method: str) -> None:
    ctx, _ = _make_ctx(event=_make_connected_event())
    with pytest.raises(ReplyNotAvailableError, match=method):
        await getattr(ctx, method)(b"raw")


@pytest.mark.asyncio
async def test_ctx_download_media_works_outside_message_event() -> None:
    ctx, tx = _make_ctx(
        event=_make_connected_event(),
        transport=FakeMediaTransport(download_payload=b"X"),
    )
    info = _media_info()
    out = await ctx.download_media(info)
    assert out == b"X"
    assert tx.download_calls[0]["info"] == info


@pytest.mark.asyncio
async def test_ctx_download_media_to_works_outside_message_event(tmp_path: Path) -> None:
    ctx, tx = _make_ctx(
        event=_make_connected_event(),
        transport=FakeMediaTransport(download_payload=b"Y"),
    )
    info = _media_info()
    out = await ctx.download_media_to(info, tmp_path / "x.bin")
    assert out.read_bytes() == b"Y"
    assert tx.download_to_calls[0]["info"] == info


# ---------------------------------------------------------------------------
# 6. MessageEvent 谓词 + F 魔法过滤器
# ---------------------------------------------------------------------------


def _msg_with_media(kind: MediaKind) -> MessageEvent:
    info = _media_info(kind=kind, mime_type="image/jpeg")
    return MessageEvent(
        **_BASE_FIELDS,
        message_id="MSG1",
        chat_jid="2@s.whatsapp.net",
        sender_jid="2@s.whatsapp.net",
        from_me=False,
        is_group=False,
        text="",
        caption="cap",
        media=info,
    )


def test_message_event_predicates_no_media() -> None:
    msg = _make_message_event()
    assert msg.has_media is False
    assert msg.is_image is False
    assert msg.is_video is False
    assert msg.is_audio is False
    assert msg.is_document is False
    assert msg.is_sticker is False


@pytest.mark.parametrize(
    ("kind", "attr"),
    [
        (MediaKind.IMAGE, "is_image"),
        (MediaKind.VIDEO, "is_video"),
        (MediaKind.AUDIO, "is_audio"),
        (MediaKind.DOCUMENT, "is_document"),
        (MediaKind.STICKER, "is_sticker"),
    ],
)
def test_message_event_predicates_per_kind(kind: MediaKind, attr: str) -> None:
    msg = _msg_with_media(kind)
    assert msg.has_media is True
    assert getattr(msg, attr) is True
    # 其他四个谓词必须为 False —— 互斥。
    others = {"is_image", "is_video", "is_audio", "is_document", "is_sticker"} - {attr}
    for other in others:
        assert getattr(msg, other) is False


def test_f_has_media_via_magic() -> None:
    msg = _msg_with_media(MediaKind.IMAGE)
    assert F.has_media.resolve(msg).passed is True
    plain = _make_message_event()
    assert F.has_media.resolve(plain).passed is False


def test_f_is_image_via_magic() -> None:
    img = _msg_with_media(MediaKind.IMAGE)
    vid = _msg_with_media(MediaKind.VIDEO)
    assert F.is_image.resolve(img).passed is True
    assert F.is_image.resolve(vid).passed is False


def test_f_combinator_has_media_and_not_from_me() -> None:
    msg = _msg_with_media(MediaKind.AUDIO)
    expr = F.has_media & ~F.from_me
    assert expr.resolve(msg).passed is True


# ---------------------------------------------------------------------------
# 7. MediaKind / MediaInfo wire round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        MediaKind.IMAGE,
        MediaKind.VIDEO,
        MediaKind.AUDIO,
        MediaKind.DOCUMENT,
        MediaKind.STICKER,
    ],
)
def test_media_kind_proto_round_trip(kind: MediaKind) -> None:
    assert MediaKind.from_proto(kind.to_proto()) is kind


def test_media_kind_unspecified_round_trip() -> None:
    assert MediaKind.from_proto(MediaKind.UNSPECIFIED.to_proto()) is MediaKind.UNSPECIFIED


def test_media_info_from_proto_preserves_fields() -> None:
    raw = pb.MediaInfo(
        kind=pb.MediaKind.MEDIA_KIND_VIDEO,
        mime_type="video/mp4",
        file_length=1024,
        direct_path="/v/abc",
        media_key=b"K" * 32,
        file_sha256=b"S" * 32,
        file_enc_sha256=b"E" * 32,
        caption="cap",
        file_name="x.mp4",
        thumbnail_jpeg=b"thumb",
        width=640,
        height=480,
        duration_seconds=12,
        voice_note=False,
        animated=False,
        url="https://example/v",
    )
    info = MediaInfo.from_proto(raw)
    assert info.kind is MediaKind.VIDEO
    assert info.mime_type == "video/mp4"
    assert info.size == 1024
    assert info.file_name == "x.mp4"
    assert info.thumbnail == b"thumb"
    assert info.width == 640
    assert info.height == 480
    assert info.duration_seconds == 12
    assert info.url == "https://example/v"


# ---------------------------------------------------------------------------
# 8. event_from_proto: media_message oneof
# ---------------------------------------------------------------------------


def test_event_from_proto_translates_media_message() -> None:
    from google.protobuf.timestamp_pb2 import Timestamp

    ts_pb = Timestamp()
    ts_pb.FromDatetime(_FIXED_TS)
    media_pb = pb.MediaInfo(
        kind=pb.MediaKind.MEDIA_KIND_IMAGE,
        mime_type="image/jpeg",
        file_length=10,
        direct_path="/v/img",
        media_key=b"K" * 32,
        file_sha256=b"S" * 32,
        file_enc_sha256=b"E" * 32,
    )
    mm = pb.MediaMessageEvent(
        message_id="MID",
        chat_jid="2@s.whatsapp.net",
        sender_jid="2@s.whatsapp.net",
        from_me=False,
        is_group=False,
        caption="cap",
        media=media_pb,
    )
    resp = pb.StreamEventsResponse(
        seq=1,
        sidecar_id="default",
        account_key="alice",
        account_jid="1@s.whatsapp.net",
        observed_at=ts_pb,
        media_message=mm,
    )
    evt = event_from_proto(resp)
    assert isinstance(evt, MessageEvent)
    assert evt.text == ""
    assert evt.caption == "cap"
    assert evt.media is not None
    assert evt.media.kind is MediaKind.IMAGE
    assert evt.has_media is True
    assert evt.is_image is True


# ---------------------------------------------------------------------------
# (no extra fixtures; rely on pytest-asyncio's auto mode from pyproject.toml)
# ---------------------------------------------------------------------------
