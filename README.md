# TARS — Telegram Media Downloader Bot

A private, production-ready Telegram bot that downloads YouTube and Facebook
media, uploads it to Cloudflare R2, and returns a signed temporary download
link.  No file is ever sent directly through Telegram.

---

## Features

- `/mp3 <url>` — extract highest-quality audio, convert to MP3
- `/mp4 <url>` — download highest-quality MP4 (merges video+audio via FFmpeg)
- Upload to Cloudflare R2 (S3-compatible)
- Presigned download links valid for 24 hours
- Private: only specific Telegram user IDs can use the bot
- Async throughout — event loop never blocked
- 2 GB file size limit
- Automatic cleanup of all temporary files

---

## Project Structure

```
tars/
├── app/
│   ├── bot.py          # Telegram handlers, auth, polling entrypoint
│   ├── downloader.py   # yt-dlp + FFmpeg async download logic
│   ├── storage.py      # Cloudflare R2 upload + presigned URL
│   ├── config.py       # Environment variable loader
│   ├── utils.py        # URL validation, filenames, formatting
│   └── requirements.txt
├── Dockerfile
├── .dockerignore
├── .env.example
└── README.md
```

---

## Environment Variables

| Variable        | Required | Description                                                       |
|-----------------|----------|-------------------------------------------------------------------|
| `BOT_TOKEN`     | Yes      | Telegram bot token from [@BotFather](https://t.me/BotFather)     |
| `ALLOWED_USERS` | Yes      | Comma-separated Telegram user IDs (e.g. `123456,789012`)         |
| `R2_ACCESS_KEY` | Yes      | Cloudflare R2 API access key ID                                  |
| `R2_SECRET_KEY` | Yes      | Cloudflare R2 API secret access key                              |
| `R2_BUCKET`     | Yes      | Name of the R2 bucket                                            |
| `R2_ENDPOINT`   | Yes      | R2 S3 endpoint: `https://<account_id>.r2.cloudflarestorage.com` |

> To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

---

## Local Development

### Prerequisites

- Python 3.11+
- `ffmpeg` installed and on `$PATH`
- A Cloudflare R2 bucket with an API token (Object Read & Write permissions)

### Setup

```bash
# 1. Clone / enter the repo
cd tars

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r app/requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your real values

# 5. Run the bot
cd app
python bot.py
```

---

## Docker

Build and run locally with Docker:

```bash
# Build
docker build -t tars-bot .

# Run (pass env vars inline or via --env-file)
docker run --rm \
  --env-file .env \
  tars-bot
```

---

## Deploying to Railway

Railway detects the `Dockerfile` automatically.

### Step-by-step

1. **Create a new Railway project** at [railway.app](https://railway.app).

2. **Connect your GitHub repository** (or push this code to a GitHub repo first).

3. **Set environment variables** in the Railway dashboard:
   - Go to your service → **Variables** tab
   - Add each variable from the table above

4. **Deploy** — Railway builds the Docker image and starts the container.
   The bot uses polling so no public URL or webhook is needed.

### Railway-specific notes

- Set the **Root Directory** to `/` (the repo root, where `Dockerfile` lives).
- No `PORT` environment variable is needed — the bot does not listen on any port.
- Railway will automatically restart the container if it crashes.
- For zero-downtime redeploys, Railway drains the old container before starting the new one.

---

## Cloudflare R2 Setup

1. Log in to the [Cloudflare dashboard](https://dash.cloudflare.com).
2. Go to **R2 Object Storage** → **Create bucket**.
3. Note your **Account ID** (shown in the right sidebar on the R2 overview page).
4. Go to **R2 → Manage R2 API Tokens** → **Create API Token**.
   - Set permissions to **Object Read & Write** for your bucket.
   - Copy the **Access Key ID** and **Secret Access Key** — they are shown only once.
5. Set `R2_ENDPOINT` to `https://<your-account-id>.r2.cloudflarestorage.com`.

> Files are not stored permanently. Each upload uses a UUID-based key and the
> presigned URL expires after 24 hours.  Consider adding an R2 lifecycle rule
> to auto-delete objects older than 2 days as an extra safety net.

---

## Security Notes

- Unauthorized users are silently ignored (no response given).
- URL validation rejects anything that is not a YouTube or Facebook HTTP/HTTPS link before any subprocess is spawned.
- All filenames are UUID4-based — no user input ever reaches the filesystem path.
- The bot runs as a non-root user inside the Docker container.
- No secrets are hardcoded; the process fails immediately at startup if any required variable is missing.

---

## Error Reference

| Scenario              | Bot response                                                |
|-----------------------|-------------------------------------------------------------|
| Unauthorized user     | *(silent — no reply)*                                       |
| Missing URL argument  | Usage hint with example                                     |
| Invalid URL           | "Invalid URL. Please provide a valid YouTube or Facebook link." |
| Download failure      | "Download failed. The URL may be unsupported or unavailable." |
| File > 2 GB           | "File exceeds the 2 GB size limit."                        |
| Upload failure        | "Upload failed. Please try again later."                    |
