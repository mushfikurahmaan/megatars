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

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
from downloader import (
    DownloadError,
    FileTooLargeError,
    download_audio,
    download_video,
)
from storage import StorageError, upload_and_sign
from utils import format_duration, generate_filename, is_valid_url, title_to_filename

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
        "Commands:\n"
        "  /mp3 &lt;url&gt; — Download audio as MP3\n"
        "  /mp4 &lt;url&gt; — Download video as MP4\n\n"
        "Supported: YouTube, Facebook, Instagram",
        parse_mode=ParseMode.HTML,
    )


@allowed_users_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


@allowed_users_only
async def cmd_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /mp3 <url>

    1. Validate URL
    2. Download best audio and convert to MP3
    3. Upload to R2
    4. Reply with signed download link
    """
    url = _parse_url(context)
    if not url:
        await update.message.reply_text(
            "Usage: <code>/mp3 &lt;url&gt;</code>\n"
            "Example: <code>/mp3 https://www.youtube.com/watch?v=dQw4w9WgXcQ</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not is_valid_url(url):
        await update.message.reply_text(
            "<b>Invalid URL.</b> Please provide a valid YouTube, Facebook, or Instagram link.",
            parse_mode=ParseMode.HTML,
        )
        return

    processing_msg = await update.message.reply_text("Processing your audio request...")
    tmp_dir: str | None = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="tars_mp3_")
        logger.info("mp3 request | user=%s | url=%s", update.effective_user.id, url)

        result = await download_audio(url, tmp_dir)

        object_key = title_to_filename(result["title"], "mp3")
        signed_url = await upload_and_sign(result["path"], object_key)

        reply = (
            "<b>Audio ready</b>\n\n"
            f"<b>Title:</b> {_escape(result['title'])}\n"
            f"<b>Duration:</b> {format_duration(result['duration'])}\n"
            f"<b>Uploader:</b> {_escape(result.get('uploader') or 'Unknown')}\n\n"
            f"<b>Download:</b> <a href=\"{signed_url}\">Click here</a>\n"
            "<b>Link expires in 24 hours</b>"
        )
        await _safe_edit(processing_msg, reply)

    except FileTooLargeError:
        await _safe_edit(processing_msg, "File exceeds the 2 GB size limit.")
    except DownloadError as exc:
        logger.error("Download error: %s", exc)
        await _safe_edit(
            processing_msg,
            "Download failed. The URL may be unsupported or unavailable.",
        )
    except StorageError as exc:
        logger.error("Storage error: %s", exc)
        await _safe_edit(processing_msg, "Upload failed. Please try again later.")
    except Exception as exc:
        logger.exception("Unexpected error in cmd_mp3: %s", exc)
        await _safe_edit(processing_msg, "An unexpected error occurred. Please try again.")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@allowed_users_only
async def cmd_mp4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /mp4 <url>

    1. Validate URL
    2. Download best quality MP4 (merges video+audio with FFmpeg if needed)
    3. Upload to R2
    4. Reply with signed download link
    """
    url = _parse_url(context)
    if not url:
        await update.message.reply_text(
            "Usage: <code>/mp4 &lt;url&gt;</code>\n"
            "Example: <code>/mp4 https://www.youtube.com/watch?v=dQw4w9WgXcQ</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not is_valid_url(url):
        await update.message.reply_text(
            "<b>Invalid URL.</b> Please provide a valid YouTube, Facebook, or Instagram link.",
            parse_mode=ParseMode.HTML,
        )
        return

    processing_msg = await update.message.reply_text("Processing your video request...")
    tmp_dir: str | None = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="tars_mp4_")
        logger.info("mp4 request | user=%s | url=%s", update.effective_user.id, url)

        result = await download_video(url, tmp_dir)

        object_key = title_to_filename(result["title"], "mp4")
        signed_url = await upload_and_sign(result["path"], object_key)

        reply = (
            "<b>Video ready</b>\n\n"
            f"<b>Title:</b> {_escape(result['title'])}\n"
            f"<b>Duration:</b> {format_duration(result['duration'])}\n"
            f"<b>Resolution:</b> {result.get('resolution') or 'Unknown'}\n"
            f"<b>Uploader:</b> {_escape(result.get('uploader') or 'Unknown')}\n\n"
            f"<b>Download:</b> <a href=\"{signed_url}\">Click here</a>\n"
            "<b>Link expires in 24 hours</b>"
        )
        await _safe_edit(processing_msg, reply)

    except FileTooLargeError:
        await _safe_edit(processing_msg, "File exceeds the 2 GB size limit.")
    except DownloadError as exc:
        logger.error("Download error: %s", exc)
        await _safe_edit(
            processing_msg,
            "Download failed. The URL may be unsupported or unavailable.",
        )
    except StorageError as exc:
        logger.error("Storage error: %s", exc)
        await _safe_edit(processing_msg, "Upload failed. Please try again later.")
    except Exception as exc:
        logger.exception("Unexpected error in cmd_mp4: %s", exc)
        await _safe_edit(processing_msg, "An unexpected error occurred. Please try again.")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions; never let them crash the polling loop."""
    logger.error("Unhandled exception", exc_info=context.error)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


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
