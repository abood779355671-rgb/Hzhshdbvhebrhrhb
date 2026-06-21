# ==============================================================================
# song.py - Search & Download Command (بحث)
# ==============================================================================
# Downloads a track from YouTube and sends it directly as an audio file in the
# chat. This is completely independent from the voice-chat playback system
# (/تشغيل) - it never joins the voice chat, it just searches, downloads, and
# uploads the file.
#
# Command:
#   بحث <اسم الأغنية>                → بحث نصي، يأخذ أول نتيجة
#   بحث <رابط يوتيوب>                → تحميل مباشر من الرابط
#   ردّ على رسالة فيها رابط + بحث    → تحميل الرابط المردود عليه
#
# Notes:
# - يعيد استخدام yt.url() / yt.valid() / yt.search() / yt.download() الموجودة
#   فعلياً في core/youtube.py (نفس المنطق المستخدم في /تشغيل بالضبط).
# - الملف يُحمَّل بصيغة m4a/opus (بدون تحويل إلى mp3) لتفادي خطوة ffmpeg
#   إضافية وإبطاء العملية - تيليجرام يدعم هذه الصيغ كملفات صوتية عادية.
# - يحترم نفس كاش التحميل في downloads/ المستخدم من قبل /تشغيل.
# ==============================================================================

import os
import logging

from pyrogram import filters
from pyrogram.types import Message
from PIL import Image

from UltraMusic import app, config, lang, yt
from UltraMusic.helpers import command, thumb

logger = logging.getLogger(__name__)


def _prepare_thumb(path: str) -> str | None:
    """
    Resize a raw downloaded thumbnail to fit Telegram's strict thumb limits
    (max 320x320, JPEG, well under 200KB). Returns the path to the resized
    file, or None on failure.

    YouTube thumbnails (maxresdefault/hqdefault) are usually much bigger than
    this, which makes Telegram reject the whole reply_audio() call with a
    PHOTO_INVALID_DIMENSIONS-style error - this was the actual cause of
    "فشل إرسال الملف الصوتي".
    """
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((320, 320))
            out_path = path.rsplit(".", 1)[0] + "_thumb.jpg"
            img.save(out_path, "JPEG", quality=85, optimize=True)
        return out_path
    except Exception as e:
        logger.warning(f"⚠️ Failed to resize song thumbnail: {e}")
        return None


@app.on_message(command(["بحث"]) & filters.group & ~app.bl_users)
@lang.language()
async def search_song(_, m: Message):
    # Auto-delete the command message itself (consistent with /تشغيل)
    try:
        await m.delete()
    except Exception:
        pass

    # ── Resolve the query: a link (typed or replied-to) or plain search text ──
    url = yt.url(m)
    if url and not yt.valid(url):
        return await m.reply_text(m.lang["song_unsupported"])

    if url:
        query = url
    elif len(m.command) > 1:
        query = m.text.split(None, 1)[1]
    else:
        return await m.reply_text(m.lang["song_usage"])

    status = await m.reply_text(m.lang["song_searching"])

    # ── Search (cached 10 min, same cache used by /تشغيل) ──
    track = await yt.search(query, m.id)
    if not track:
        return await status.edit_text(m.lang["song_not_found"])

    if track.is_live:
        return await status.edit_text(m.lang["song_live_unsupported"])

    if track.duration_sec and track.duration_sec > config.SONG_DOWNLOAD_LIMIT:
        return await status.edit_text(
            m.lang["song_too_long"].format(config.SONG_DOWNLOAD_LIMIT // 60)
        )

    await status.edit_text(m.lang["song_downloading"].format(track.title))

    # ── Download (audio only, reuses the existing downloads/ cache) ──
    try:
        file_path = await yt.download(track.id)
    except Exception as e:
        logger.error(f"❌ yt.download failed for {track.id}: {e}", exc_info=True)
        file_path = None

    if not file_path or not os.path.isfile(file_path):
        return await status.edit_text(m.lang["song_download_failed"])

    # ── Thumbnail (best effort - send without one if it fails) ──
    thumb_path = None
    if track.thumbnail:
        try:
            raw_thumb = await thumb.save_thumb(
                f"cache/song_{track.id}.jpg", track.thumbnail
            )
            thumb_path = _prepare_thumb(raw_thumb)
            if raw_thumb and os.path.isfile(raw_thumb) and raw_thumb != thumb_path:
                try:
                    os.remove(raw_thumb)
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"⚠️ Thumbnail download failed for {track.id}: {e}")
            thumb_path = None

    await status.edit_text(m.lang["song_uploading"])

    # ── Send the audio file directly in the chat ──
    async def _send(with_thumb: bool):
        await app.send_chat_action(chat_id=m.chat.id, action="upload_audio")
        await m.reply_audio(
            file_path,
            title=track.title,
            performer=track.channel_name or "YouTube",
            duration=track.duration_sec or 0,
            thumb=thumb_path if with_thumb else None,
            caption=m.lang["song_caption"].format(track.title),
        )

    try:
        try:
            await _send(with_thumb=True)
        except Exception as e:
            if thumb_path:
                # Retry once without the thumbnail instead of failing outright -
                # covers any remaining edge case Telegram rejects.
                logger.warning(
                    f"⚠️ reply_audio with thumb failed for {track.id}, retrying without thumb: {e}"
                )
                await _send(with_thumb=False)
            else:
                raise
    except Exception as e:
        logger.error(f"❌ Failed to send song {track.id}: {e}", exc_info=True)
        return await status.edit_text(m.lang["song_send_failed"])
    finally:
        # Clean up the temporary thumbnail (the audio file itself stays
        # cached in downloads/ so /تشغيل or /بحث can reuse it instantly).
        if thumb_path and os.path.isfile(thumb_path):
            try:
                os.remove(thumb_path)
            except OSError:
                pass

    try:
        await status.delete()
    except Exception:
        pass
