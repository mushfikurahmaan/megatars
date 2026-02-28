# ── Stage 1: dependency installation ──────────────────────────────────────────
# Using slim to keep image size small; ffmpeg is installed from apt.
FROM python:3.11-slim AS base

# System deps: ffmpeg (required by yt-dlp for merging video+audio streams)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before copying source so Docker can cache this
# layer and skip it on subsequent builds when only source files change.
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: application source ────────────────────────────────────────────────
COPY app/ .

# Run as non-root for security
RUN useradd --no-create-home --shell /bin/false tars
USER tars

# No port needed — the bot uses polling, not a webhook.
CMD ["python", "bot.py"]
