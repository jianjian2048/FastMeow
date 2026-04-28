"""媒体消息示例 bot —— 演示 Phase 4.3 Media。

从仓库根目录运行::

    python examples/media_bot.py

可选环境变量（未设置则跳过对应命令）：

* ``FASTMEOW_DEMO_IMAGE``    —— 本地图片路径（默认 ``./examples/assets/demo.jpg`` 若存在）
* ``FASTMEOW_DEMO_DOCUMENT`` —— 本地文档路径
* ``FASTMEOW_DEMO_AUDIO``    —— 本地音频路径（``/voice`` 命令将以 PTT 发送）
* ``FASTMEOW_DOWNLOAD_DIR``  —— 入站媒体落盘目录，默认 ``./downloads``

支持的命令：

* ``/image``    —— 把配置的图片以图片消息回复源消息
* ``/document`` —— 把配置的文档以文档消息回复源消息
* ``/voice``    —— 把配置的音频以语音条（PTT）回复源消息

被动行为：所有入站图片 / 视频 / 音频 / 文档 / 贴纸都会被自动下载到
``DOWNLOAD_DIR`` 下，文件名形如 ``<message_id>.<ext>``。

首次运行时请扫码配对（设置 -> 已关联设备 -> 关联设备）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastmeow import (
    ConnectedEvent,
    Ctx,
    F,
    FastMeow,
    MessageEvent,
    Router,
)
from fastmeow.types import MediaKind

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("media_bot")


_EXTENSION_BY_KIND: dict[MediaKind, str] = {
    MediaKind.IMAGE: "jpg",
    MediaKind.VIDEO: "mp4",
    MediaKind.AUDIO: "ogg",
    MediaKind.DOCUMENT: "bin",
    MediaKind.STICKER: "webp",
}


def _maybe_path(env_key: str) -> Path | None:
    """从环境变量读取媒体源路径；不存在或文件缺失则返回 ``None``。"""
    raw = os.environ.get(env_key)
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    if not p.is_file():
        logger.warning("env %s -> %s does not exist; skipping", env_key, p)
        return None
    return p


DOWNLOAD_DIR = Path(
    os.environ.get("FASTMEOW_DOWNLOAD_DIR", "./downloads")
).expanduser().resolve()
IMAGE_PATH = _maybe_path("FASTMEOW_DEMO_IMAGE")
DOCUMENT_PATH = _maybe_path("FASTMEOW_DEMO_DOCUMENT")
AUDIO_PATH = _maybe_path("FASTMEOW_DEMO_AUDIO")


router = Router(name="media")


@router.connected()
async def announce_online(event: ConnectedEvent, ctx: Ctx) -> None:
    """连接成功时打印一行日志，方便确认会话已就绪。"""
    print(f"[{ctx.account_key}] online as {ctx.account_jid}")


@router.message(F.text == "/image")
async def send_demo_image(msg: MessageEvent, ctx: Ctx) -> None:
    """把配置的图片以图片消息回复源消息。"""
    if IMAGE_PATH is None:
        await ctx.reply("FASTMEOW_DEMO_IMAGE 未配置；请设置环境变量后重试。")
        return
    await ctx.reply_image(IMAGE_PATH, caption="hello from FastMeow")


@router.message(F.text == "/document")
async def send_demo_document(msg: MessageEvent, ctx: Ctx) -> None:
    """把配置的文档以文档消息回复源消息。"""
    if DOCUMENT_PATH is None:
        await ctx.reply("FASTMEOW_DEMO_DOCUMENT 未配置；请设置环境变量后重试。")
        return
    await ctx.reply_document(DOCUMENT_PATH, caption="here is the file")


@router.message(F.text == "/voice")
async def send_demo_voice(msg: MessageEvent, ctx: Ctx) -> None:
    """把配置的音频以语音条（PTT）回复源消息。"""
    if AUDIO_PATH is None:
        await ctx.reply("FASTMEOW_DEMO_AUDIO 未配置；请设置环境变量后重试。")
        return
    await ctx.reply_audio(AUDIO_PATH, voice_note=True)


@router.message(F.has_media & ~F.from_me)
async def archive_inbound_media(msg: MessageEvent, ctx: Ctx) -> None:
    """入站媒体自动落盘。

    使用 :meth:`Ctx.download_media_to` 走流式下载，避免大文件占满内存。
    文件名采用 ``<message_id>.<ext>``，便于与原消息一一对照。
    """
    assert msg.media is not None  # F.has_media 已保证
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = _EXTENSION_BY_KIND.get(msg.media.kind, "bin")
    target = DOWNLOAD_DIR / f"{msg.message_id}.{ext}"
    saved = await ctx.download_media_to(msg.media, target)
    logger.info(
        "saved %s media from %s to %s (%d bytes)",
        msg.media.kind.value,
        msg.sender_jid,
        saved,
        msg.media.size,
    )


async def main() -> None:
    session_dir = Path("./sessions").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    async with FastMeow(session_dir=session_dir) as app:
        app.include_router(router)
        handle = app.add_account("demo", on_qr="terminal")

        print("waiting for pairing/connect...")
        await handle.ready(timeout=120)
        print(f"connected: {handle.account_key} -> {handle.jid}")
        print(f"download dir: {DOWNLOAD_DIR}")

        try:
            await app.run_forever()
        except KeyboardInterrupt:
            print("interrupted; shutting down")


if __name__ == "__main__":
    asyncio.run(main())
