"""
downloader.py — Media download logic using yt-dlp and FFmpeg.

All heavy work (subprocess execution) is done via asyncio subprocesses so
the event loop is never blocked.  The caller receives a structured result
dict; on failure a typed exception is raised so bot.py can craft the right
user-facing message.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TypedDict

from config import MAX_FILE_SIZE_BYTES
from utils import generate_filename

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed return value
# ---------------------------------------------------------------------------

class AudioResult(TypedDict):
    path: str          # absolute path to the downloaded MP3
    title: str
    duration: int | None   # seconds
    uploader: str | None


class VideoResult(TypedDict):
    path: str          # absolute path to the downloaded MP4
    title: str
    duration: int | None   # seconds
    resolution: str | None
    uploader: str | None


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DownloadError(Exception):
    """Raised when yt-dlp exits with a non-zero status."""


class FileTooLargeError(Exception):
    """Raised when the downloaded file exceeds MAX_FILE_SIZE_BYTES."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run(cmd: list[str]) -> tuple[str, str]:
    """
    Run *cmd* as an async subprocess and return (stdout, stderr).
    Raises DownloadError if the process exits with a non-zero code.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")

    if proc.returncode != 0:
        logger.error("yt-dlp failed (rc=%d): %s", proc.returncode, stderr[-2000:])
        raise DownloadError(f"yt-dlp exited with code {proc.returncode}")

    return stdout, stderr


async def _fetch_metadata(url: str) -> dict:
    """Return the yt-dlp JSON metadata for *url* without downloading."""
    stdout, _ = await _run([
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        url,
    ])
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise DownloadError(f"Could not parse yt-dlp metadata: {exc}") from exc


def _check_size(path: str) -> None:
    """Raise FileTooLargeError when the file at *path* exceeds the limit."""
    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE_BYTES:
        os.remove(path)
        raise FileTooLargeError(
            f"File size {size / (1024**3):.2f} GB exceeds the 2 GB limit."
        )


def _find_output_file(tmp_dir: str, expected_stem: str, expected_ext: str) -> str:
    """
    Locate the actual output file written by yt-dlp.

    yt-dlp may append metadata to the stem; we search for any file in
    *tmp_dir* whose name starts with *expected_stem* and ends with the
    expected extension.
    """
    for entry in Path(tmp_dir).iterdir():
        if entry.suffix.lower() == f".{expected_ext}" and entry.stem.startswith(expected_stem):
            return str(entry)

    # Fallback: pick the only file with the right extension
    candidates = list(Path(tmp_dir).glob(f"*.{expected_ext}"))
    if candidates:
        return str(candidates[0])

    raise DownloadError(
        f"yt-dlp finished but no .{expected_ext} file found in {tmp_dir}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def download_audio(url: str, tmp_dir: str) -> AudioResult:
    """
    Download the best available audio from *url* and convert it to MP3.

    Steps:
      1. Fetch metadata to get title / duration early (for user feedback).
      2. Run yt-dlp with bestaudio + FFmpeg post-processor to produce MP3.
      3. Verify the output file exists and is within size limits.
    """
    logger.info("Fetching metadata for audio: %s", url)
    meta = await _fetch_metadata(url)

    title: str = meta.get("title", "Unknown")
    duration: int | None = meta.get("duration")
    uploader: str | None = meta.get("uploader") or meta.get("channel")

    # Unique output template — yt-dlp will append the extension itself
    stem = generate_filename("PLACEHOLDER").rsplit(".", 1)[0]  # just the UUID hex
    output_template = os.path.join(tmp_dir, f"{stem}.%(ext)s")

    logger.info("Downloading audio: %s", title)
    await _run([
        "yt-dlp",
        "--no-playlist",
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",          # VBR best quality
        "--output", output_template,
        "--no-part",                      # avoid .part files
        "--no-mtime",
        url,
    ])

    path = _find_output_file(tmp_dir, stem, "mp3")
    _check_size(path)

    logger.info("Audio ready: %s (%d bytes)", path, os.path.getsize(path))
    return AudioResult(path=path, title=title, duration=duration, uploader=uploader)


async def download_video(url: str, tmp_dir: str) -> VideoResult:
    """
    Download the best available MP4 video from *url*.

    Format preference (in order):
      1. Best video (mp4) + best audio (m4a) merged by FFmpeg.
      2. Any pre-merged best-quality mp4.
      3. Absolute best available format (may require FFmpeg merge).
    """
    logger.info("Fetching metadata for video: %s", url)
    meta = await _fetch_metadata(url)

    title: str = meta.get("title", "Unknown")
    duration: int | None = meta.get("duration")
    uploader: str | None = meta.get("uploader") or meta.get("channel")

    # Attempt to derive resolution from metadata
    width: int | None = meta.get("width")
    height: int | None = meta.get("height")
    resolution: str | None = f"{width}x{height}" if width and height else None

    stem = generate_filename("PLACEHOLDER").rsplit(".", 1)[0]
    output_template = os.path.join(tmp_dir, f"{stem}.%(ext)s")

    logger.info("Downloading video: %s", title)
    await _run([
        "yt-dlp",
        "--no-playlist",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-part",
        "--no-mtime",
        url,
    ])

    path = _find_output_file(tmp_dir, stem, "mp4")
    _check_size(path)

    # If metadata didn't carry resolution, try parsing from the filename
    if not resolution:
        match = re.search(r"(\d{3,4}x\d{3,4})", path)
        if match:
            resolution = match.group(1)

    logger.info("Video ready: %s (%d bytes)", path, os.path.getsize(path))
    return VideoResult(
        path=path,
        title=title,
        duration=duration,
        resolution=resolution,
        uploader=uploader,
    )
