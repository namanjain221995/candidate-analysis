"""S3 storage helpers for the transcript service."""

from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from config import Settings


def build_s3_client(settings: Settings):
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 8, "mode": "standard"},
        ),
    )


def object_size(s3, bucket: str, key: str) -> int:
    resp = s3.head_object(Bucket=bucket, Key=key)
    return int(resp.get("ContentLength") or 0)


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def download_file(s3, bucket: str, key: str, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest_path))


def upload_text(s3, bucket: str, key: str, content: str) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def prefix_of(key: str) -> str:
    """'a/b/c.mp4' -> 'a/b/'  (empty string if no slash)."""
    return key.rsplit("/", 1)[0] + "/" if "/" in key else ""


def sibling_key(video_key: str, sibling_name: str) -> str:
    """Key for a file living in the same prefix as the video."""
    return prefix_of(video_key) + sibling_name
