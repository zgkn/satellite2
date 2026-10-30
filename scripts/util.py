"""Small helpers: logging, GitHub annotations, S3 client."""
from __future__ import annotations

import os


class UserError(Exception):
    """An expected failure whose message is meant to be read by the user."""


def log(msg: str) -> None:
    print(msg, flush=True)


def annotate(level: str, msg: str) -> None:
    """Print a message, plus a GitHub Actions annotation when running there."""
    print(f"{level.upper()}: {msg}", flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{msg.replace(chr(10), ' ')}", flush=True)


def s3_client(pool: int = 16):
    """Anonymous S3 client. Adaptive retries ride out the occasional 'SlowDown' throttle."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(
            signature_version=UNSIGNED,
            max_pool_connections=pool,
            retries={"max_attempts": 8, "mode": "adaptive"},
        ),
    )
