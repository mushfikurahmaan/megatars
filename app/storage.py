"""
storage.py — Cloudflare R2 (S3-compatible) upload and presigned URL generation.

boto3 is a synchronous library; all blocking calls are executed inside
asyncio's default ThreadPoolExecutor so the event loop is never stalled.
"""

import asyncio
import logging
import os
from functools import partial

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import config

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when an R2 upload or presign operation fails."""


def _build_client():
    """
    Construct a boto3 S3 client pointed at the Cloudflare R2 endpoint.

    Cloudflare R2 is S3-compatible but requires path-style addressing and
    does not support chunked transfer encoding the same way AWS does, so
    we disable it explicitly via the config.
    """
    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT,
        aws_access_key_id=config.R2_ACCESS_KEY,
        aws_secret_access_key=config.R2_SECRET_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
        region_name="auto",  # R2 ignores region but boto3 requires one
    )


# Module-level client; constructed once on first use.  Safe to reuse across
# threads because boto3 clients are thread-safe for read/upload operations.
_client = None
_client_lock = asyncio.Lock()


async def _get_client():
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:  # double-checked locking
                loop = asyncio.get_running_loop()
                _client = await loop.run_in_executor(None, _build_client)
    return _client


def _upload_sync(client, local_path: str, object_key: str) -> None:
    """Synchronous upload helper — runs in a thread pool."""
    try:
        client.upload_file(
            Filename=local_path,
            Bucket=config.R2_BUCKET,
            Key=object_key,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(f"Upload to R2 failed: {exc}") from exc


def _presign_sync(client, object_key: str) -> str:
    """Generate a presigned GET URL — runs in a thread pool."""
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": config.R2_BUCKET, "Key": object_key},
            ExpiresIn=config.PRESIGNED_URL_TTL_SECONDS,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(f"Presign failed: {exc}") from exc


async def upload_and_sign(local_path: str, object_key: str) -> str:
    """
    Upload *local_path* to R2 under *object_key*, generate a presigned
    download URL valid for 24 hours, delete the local file, and return
    the URL.

    Raises StorageError on any R2 failure.
    """
    client = await _get_client()
    loop = asyncio.get_running_loop()

    logger.info("Uploading %s → R2:%s", local_path, object_key)
    try:
        await loop.run_in_executor(None, partial(_upload_sync, client, local_path, object_key))
    except StorageError:
        raise  # already logged by caller

    logger.info("Generating presigned URL for %s", object_key)
    url: str = await loop.run_in_executor(None, partial(_presign_sync, client, object_key))

    # Best-effort local cleanup; log but never raise on failure here
    try:
        os.remove(local_path)
        logger.info("Deleted local file: %s", local_path)
    except OSError as exc:
        logger.warning("Could not delete local file %s: %s", local_path, exc)

    return url
