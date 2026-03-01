"""
config.py — Central configuration loader.

Reads all required environment variables at startup and fails fast with
a clear message if any are missing. No secrets are ever hardcoded here.
"""

import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Check your Railway / .env configuration."
        )
    return value


def _require_int_set(name: str) -> set[int]:
    raw = _require(name)
    try:
        return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}
    except ValueError:
        raise EnvironmentError(
            f"Environment variable '{name}' must be a comma-separated list of "
            f"integer Telegram user IDs. Got: {raw!r}"
        )


# Telegram
BOT_TOKEN: str = _require("BOT_TOKEN")

# Allowlist — set of Telegram user IDs permitted to use the bot
ALLOWED_USERS: set[int] = _require_int_set("ALLOWED_USERS")

# Cloudflare R2 (S3-compatible)
R2_ACCESS_KEY: str = _require("R2_ACCESS_KEY")
R2_SECRET_KEY: str = _require("R2_SECRET_KEY")
R2_BUCKET: str = _require("R2_BUCKET")
R2_ENDPOINT: str = _require("R2_ENDPOINT")

# Webhook (optional — set on Railway for webhook mode; unset for local polling)
# Must be full HTTPS URL, e.g. https://megatars-production.up.railway.app
WEBHOOK_URL: str | None = os.environ.get("WEBHOOK_URL")
# Optional secret to validate incoming webhook requests (recommended)
WEBHOOK_SECRET: str | None = os.environ.get("WEBHOOK_SECRET")

# Download constraints
MAX_FILE_SIZE_BYTES: int = 2 * 1024 ** 3  # 2 GB

# Presigned URL TTL
PRESIGNED_URL_TTL_SECONDS: int = 86_400  # 24 hours
