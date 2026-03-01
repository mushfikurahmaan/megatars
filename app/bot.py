"""
bot.py — TARS Telegram bot entrypoint.

Responsibilities:
  - Register command handlers (/mp3, /mp4, /start, /help)
  - Enforce the ALLOWED_USERS allowlist via a decorator
  - Orchestrate download → upload → reply for each command
  - Handle and surface errors to the user without crashing
  - Run the polling loop

Architecture note: all handlers are async. Blocking I/O (yt-dlp,
boto3) lives in downloader.py / storage.py and is either inherently
async or offloaded to a thread pool executor.
"""

import asyncio
import functools
import logging
import os
import shutil
import tempfile
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from downloader import (
    DownloadError,
    FileTooLargeError,
    download_audio,
    download_video,
)
from storage import StorageError, upload_and_sign
from utils import (
    extract_first_url,
    format_duration,
    generate_filename,
    is_valid_url,
    title_to_filename,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Suppress noisy httpx / httpcore debug output
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def allowed_users_only(handler: Callable) -> Callable:
    """
    Decorator that silently ignores updates from users not in ALLOWED_USERS.

    "Silently ignores" means the bot sends no reply, giving no indication
    to the unauthorized user that the bot even exists.
    """
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in config.ALLOWED_USERS:
            uid = user.id if user else "unknown"
            logger.warning("Rejected unauthorized user id=%s", uid)
            return
        return await handler(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_url(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Extract the URL argument from a command (e.g. /mp3 <url>)."""
    args = context.args
    if not args:
        return None
    return args[0].strip()


def _escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


async def _execute_audio(url: str, tmp_dir: str) -> str:
    """Download audio, upload to R2, return HTML success message. Raises on error."""
    result = await download_audio(url, tmp_dir)
    object_key = title_to_filename(result["title"], "mp3")
    signed_url = await upload_and_sign(result["path"], object_key)
    return (
        "<b>Audio ready</b>\n\n"
        f"<b>Title:</b> {_escape(result['title'])}\n"
        f"<b>Duration:</b> {format_duration(result['duration'])}\n"
        f"<b>Uploader:</b> {_escape(result.get('uploader') or 'Unknown')}\n\n"
        f"<b>Download:</b> <a href=\"{signed_url}\">Click here</a>\n"
        "<b>Link expires in 24 hours</b>"
    )


async def _execute_video(url: str, tmp_dir: str) -> str:
    """Download video, upload to R2, return HTML success message. Raises on error."""
    result = await download_video(url, tmp_dir)
    object_key = title_to_filename(result["title"], "mp4")
    signed_url = await upload_and_sign(result["path"], object_key)
    return (
        "<b>Video ready</b>\n\n"
        f"<b>Title:</b> {_escape(result['title'])}\n"
        f"<b>Duration:</b> {format_duration(result['duration'])}\n"
        f"<b>Resolution:</b> {result.get('resolution') or 'Unknown'}\n"
        f"<b>Uploader:</b> {_escape(result.get('uploader') or 'Unknown')}\n\n"
        f"<b>Download:</b> <a href=\"{signed_url}\">Click here</a>\n"
        "<b>Link expires in 24 hours</b>"
    )


async def _safe_edit(message, text: str) -> None:
    """Edit *message* with *text*; ignore errors if the message was deleted."""
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.debug("Could not edit message: %s", exc)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@allowed_users_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>TARS Media Downloader</b>\n\n"
        "Just <b>paste a URL</b> and I'll ask: Audio or Video?\n\n"
        "Or use commands:\n"
        "  /mp3 &lt;url&gt; — Audio as MP3\n"
        "  /mp4 &lt;url&gt; — Video as MP4\n\n"
        "Supported: YouTube, Facebook, Instagram, Pinterest",
        parse_mode=ParseMode.HTML,
    )


@allowed_users_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def _do_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Shared logic for audio download (used by cmd and callback)."""
    processing_msg = await update.message.reply_text("Processing your audio request...")
    tmp_dir: str | None = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="tars_mp3_")
        logger.info("mp3 request | user=%s | url=%s", update.effective_user.id, url)
        reply = await _execute_audio(url, tmp_dir)
        await _safe_edit(processing_msg, reply)
    except FileTooLargeError:
        await _safe_edit(processing_msg, "File exceeds the 2 GB size limit.")
    except DownloadError:
        logger.error("Download error for url=%s", url)
        await _safe_edit(processing_msg, "Download failed. The URL may be unsupported or unavailable.")
    except StorageError:
        logger.error("Storage error for url=%s", url)
        await _safe_edit(processing_msg, "Upload failed. Please try again later.")
    except Exception as exc:
        logger.exception("Unexpected error in mp3: %s", exc)
        await _safe_edit(processing_msg, "An unexpected error occurred. Please try again.")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _do_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Shared logic for video download (used by cmd and callback)."""
    processing_msg = await update.message.reply_text("Processing your video request...")
    tmp_dir: str | None = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="tars_mp4_")
        logger.info("mp4 request | user=%s | url=%s", update.effective_user.id, url)
        reply = await _execute_video(url, tmp_dir)
        await _safe_edit(processing_msg, reply)
    except FileTooLargeError:
        await _safe_edit(processing_msg, "File exceeds the 2 GB size limit.")
    except DownloadError:
        logger.error("Download error for url=%s", url)
        await _safe_edit(processing_msg, "Download failed. The URL may be unsupported or unavailable.")
    except StorageError:
        logger.error("Storage error for url=%s", url)
        await _safe_edit(processing_msg, "Upload failed. Please try again later.")
    except Exception as exc:
        logger.exception("Unexpected error in mp4: %s", exc)
        await _safe_edit(processing_msg, "An unexpected error occurred. Please try again.")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@allowed_users_only
async def cmd_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /mp3 <url> — download audio as MP3. """
    url = _parse_url(context)
    if not url:
        await update.message.reply_text(
            "Usage: <code>/mp3 &lt;url&gt;</code>\n"
            "Or just paste a URL and I'll ask!",
            parse_mode=ParseMode.HTML,
        )
        return
    if not is_valid_url(url):
        await update.message.reply_text(
            "<b>Invalid URL.</b> Supported: YouTube, Facebook, Instagram, Pinterest.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _do_audio(update, context, url)


@allowed_users_only
async def cmd_mp4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /mp4 <url> — download video as MP4. """
    url = _parse_url(context)
    if not url:
        await update.message.reply_text(
            "Usage: <code>/mp4 &lt;url&gt;</code>\n"
            "Or just paste a URL and I'll ask!",
            parse_mode=ParseMode.HTML,
        )
        return
    if not is_valid_url(url):
        await update.message.reply_text(
            "<b>Invalid URL.</b> Supported: YouTube, Facebook, Instagram, Pinterest.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _do_video(update, context, url)


# ---------------------------------------------------------------------------
# URL paste → Audio/Video choice
# ---------------------------------------------------------------------------

@allowed_users_only
async def msg_url_paste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When user pastes a URL, ask Audio or Video via inline keyboard."""
    text = update.message.text or ""
    url = extract_first_url(text)
    if not url:
        return  # No valid URL, ignore (don't reply to every message)
    context.user_data["pending_url"] = url
    keyboard = [
        [
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data="mp3"),
            InlineKeyboardButton("🎬 Video (MP4)", callback_data="mp4"),
        ],
    ]
    await update.message.reply_text(
        "Which format do you want?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@allowed_users_only
async def callback_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button press (Audio or Video)."""
    query = update.callback_query
    await query.answer()
    url = context.user_data.pop("pending_url", None)
    if not url:
        await query.edit_message_text("That link expired. Please paste the URL again.")
        return
    # Build a minimal update-like object so _do_audio/_do_video can use update.message.reply_text
    # They expect update.message — for callbacks, the "message" is the one with the buttons.
    # We need to reply in the chat. query.message.reply_text() sends a new message.
    # Create a simple wrapper so _do_audio receives something with .message that has .reply_text.
    class _CallbackUpdate:
        effective_user = update.effective_user
        message = query.message  # Has reply_text, chat, etc.

    fake_update = _CallbackUpdate()
    if query.data == "mp3":
        await _do_audio(fake_update, context, url)
    else:
        await _do_video(fake_update, context, url)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions; never let them crash the polling loop."""
    logger.error("Unhandled exception", exc_info=context.error)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mp3", cmd_mp3))
    app.add_handler(CommandHandler("mp4", cmd_mp4))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, msg_url_paste),
    )
    app.add_handler(CallbackQueryHandler(callback_format_choice, pattern="^(mp3|mp4)$"))
    app.add_error_handler(error_handler)

    logger.info(
        "Allowed users: %s",
        ", ".join(str(u) for u in config.ALLOWED_USERS),
    )

    if config.WEBHOOK_URL:
        port = int(os.environ.get("PORT", 8080))
        # Use root path — PTB listens on / by default; putting token in path causes
        # path mismatch + colon-in-token can break routing. Use secret_token instead.
        webhook_base = config.WEBHOOK_URL.rstrip("/")
        if not webhook_base.startswith(("http://", "https://")):
            webhook_base = f"https://{webhook_base}"
        webhook_url = f"{webhook_base}/"
        logger.info("Starting TARS bot (webhook mode) on port %d → %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting TARS bot (polling mode — no WEBHOOK_URL set)…")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()
