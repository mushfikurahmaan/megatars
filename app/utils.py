"""
utils.py — Shared utility helpers.

Provides URL validation, safe filename generation, and human-readable
formatting functions used across the rest of the application.
"""

import re
import uuid
from urllib.parse import urlparse

# Patterns that identify supported media sources
_SUPPORTED_HOSTS: tuple[re.Pattern, ...] = (
    re.compile(r"(www\.)?(youtube\.com|youtu\.be)", re.IGNORECASE),
    re.compile(r"(www\.)?(facebook\.com|fb\.watch|fb\.com)", re.IGNORECASE),
    re.compile(r"(www\.)?(instagram\.com)", re.IGNORECASE),
    re.compile(r"(www\.)?(pinterest\.com|pin\.it)", re.IGNORECASE),
)


def is_valid_url(url: str) -> bool:
    """
    Return True only when *url* is an http/https link pointing to a
    supported media host (YouTube, Facebook, Instagram, or Pinterest).

    This guards against command injection — we never pass arbitrary
    strings to the shell; even so, limiting accepted URLs is an
    additional safety layer.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.netloc.lower()
    return any(pattern.search(host) for pattern in _SUPPORTED_HOSTS)


def generate_filename(ext: str) -> str:
    """
    Return a collision-proof filename: ``<uuid4_hex>.<ext>``.

    Used for local temp files where the name doesn't matter to the user.
    """
    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", ext)
    return f"{uuid.uuid4().hex}.{safe_ext}"


def title_to_filename(title: str, ext: str) -> str:
    """
    Convert a media title into a safe, human-readable R2 object key.

    Example: "Rick Astley - Never Gonna Give You Up" → "Rick_Astley_-_Never_Gonna_Give_You_Up_a3f9c2.mp3"

    Rules applied:
    - Replace whitespace with underscores
    - Strip characters unsafe for S3 keys and most filesystems
    - Truncate to 180 chars to stay well within S3's 1024-byte key limit
    - Append a short random suffix to prevent collisions when the same
      title is requested concurrently by different users
    """
    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", ext)
    # Keep letters, digits, spaces, hyphens, parentheses, dots
    sanitized = re.sub(r"[^\w\s\-\(\)\.]", "", title, flags=re.UNICODE)
    sanitized = re.sub(r"\s+", "_", sanitized.strip())
    sanitized = sanitized[:180] if len(sanitized) > 180 else sanitized
    suffix = uuid.uuid4().hex[:6]
    return f"{sanitized}_{suffix}.{safe_ext}"


def format_duration(seconds: int | float | None) -> str:
    """Convert a duration in seconds to a human-readable ``HH:MM:SS`` string."""
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(num_bytes: int | float | None) -> str:
    """Return a human-readable file size string (e.g. ``45.2 MB``)."""
    if num_bytes is None:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"
