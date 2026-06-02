"""S3 helpers for the LLM stage."""

from pathlib import Path
from typing import List, Optional

import boto3
from botocore.config import Config

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def build_s3(settings):
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        config=Config(signature_version="s3v4", retries={"max_attempts": 8}),
    )


def read_text(s3, bucket, key) -> str:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8", errors="replace")


def list_prefix(s3, bucket, prefix) -> List[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def download(s3, bucket, key, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest))


def prefix_of(key: str) -> str:
    return key.rsplit("/", 1)[0] + "/" if "/" in key else ""


def parent_prefix(prefix: str) -> str:
    """Prefix of the parent folder. '.../Day 1/HR(.)/' -> '.../Day 1/'."""
    p = prefix.rstrip("/")
    return p.rsplit("/", 1)[0] + "/" if "/" in p else ""


def deliverable_name_from_prefix(prefix: str) -> str:
    return prefix.rstrip("/").rsplit("/", 1)[-1]


def training_steps_prefix(key: str) -> Optional[str]:
    marker = "trainingSteps/"
    i = key.find(marker)
    return key[: i + len(marker)] if i != -1 else None


def find_resume_pdf(s3, bucket, ts_prefix) -> Optional[str]:
    if not ts_prefix:
        return None
    for k in list_prefix(s3, bucket, ts_prefix + "resume pdf/"):
        if k.lower().endswith(".pdf"):
            return k
    return None


def find_first_image(s3, bucket, folder_prefix) -> Optional[str]:
    for k in list_prefix(s3, bucket, folder_prefix):
        if k.lower().endswith(IMAGE_EXTS):
            return k
    return None


def find_first_text(s3, bucket, folder_prefix, *, exclude_suffixes=()) -> Optional[str]:
    for k in list_prefix(s3, bucket, folder_prefix):
        low = k.lower()
        if low.endswith(".txt") and not any(low.endswith(s) for s in exclude_suffixes):
            return k
    return None
