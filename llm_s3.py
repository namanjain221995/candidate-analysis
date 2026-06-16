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


def list_objects(s3, bucket, prefix):
    """Like list_prefix but returns [(key, LastModified), ...] for newest-first picking."""
    objs = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objs.append((obj["Key"], obj["LastModified"]))
    return objs


def is_tagged(key: str, pass_marker: str, fail_marker: str) -> bool:
    """True if the key already carries a pass/fail tag (an earlier attempt)."""
    name = key.rsplit("/", 1)[-1]
    return pass_marker in name or fail_marker in name


def tag_folder_files(s3, bucket, folder_prefix, marker, *, pass_marker, fail_marker) -> int:
    """Append `marker` to every UNtagged file directly in folder_prefix (S3 copy+delete).

    Only files at this level (not nested subfolders) and not already tagged are renamed.
    The marker goes after the extension so a tagged file matches no router route and
    is never re-processed. Returns the number of files tagged."""
    count = 0
    for key in list_prefix(s3, bucket, folder_prefix):
        rel = key[len(folder_prefix):]
        if not rel or "/" in rel:           # skip nested objects / folder marker
            continue
        if pass_marker in rel or fail_marker in rel:
            continue
        new_key = key + marker
        try:
            s3.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": key}, Key=new_key)
            s3.delete_object(Bucket=bucket, Key=key)   # requires s3:DeleteObject
            count += 1
        except Exception as exc:
            # one file failing (e.g. missing DeleteObject perm) must not stop the rest
            print(f"[TAG-FILE-ERROR] {key}: {exc}")
    return count


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


def _untagged_base(key: str, pass_marker="(Pass)", fail_marker="(Fail)") -> str:
    """Key with any '(Pass)…'/'(Fail)…' tag suffix removed (for extension checks)."""
    for m in (pass_marker, fail_marker):
        i = key.find(m)
        if i != -1:
            return key[:i]
    return key


def find_first_image(s3, bucket, folder_prefix, *, exclude_prefix=None,
                     pass_marker="(Pass)", fail_marker="(Fail)") -> Optional[str]:
    """Newest image under folder_prefix (recursive). Prefers untagged (fresh)
    files, but falls back to the newest pass/fail-tagged image so combined
    analyses (e.g. Team Structure video + diagram) still get the image even when
    the image folder was scored — and its files tagged — before the video arrived.

    Pass exclude_prefix to ignore images inside a given subfolder — used when a
    video deliverable pulls a diagram from a SIBLING folder, not its own (Day 6
    'System Design Problem 1' video pulling 'Problem 2' diagram)."""
    untagged, tagged = [], []
    for key, lm in list_objects(s3, bucket, folder_prefix):
        if exclude_prefix and key.startswith(exclude_prefix):
            continue
        base = _untagged_base(key, pass_marker, fail_marker)
        if not base.lower().endswith(IMAGE_EXTS):
            continue
        if is_tagged(key, pass_marker, fail_marker):
            tagged.append((key, lm))
        else:
            untagged.append((key, lm))
    pool = untagged if untagged else tagged
    pool.sort(key=lambda x: x[1], reverse=True)
    return pool[0][0] if pool else None


def find_first_text(s3, bucket, folder_prefix, *, exclude_suffixes=(),
                    pass_marker="(Pass)", fail_marker="(Fail)") -> Optional[str]:
    """Newest .txt in the folder. Prefers untagged files, falls back to the
    newest tagged one (same reasoning as find_first_image)."""
    untagged, tagged = [], []
    for key, lm in list_objects(s3, bucket, folder_prefix):
        base = _untagged_base(key, pass_marker, fail_marker).lower()
        if not base.endswith(".txt") or any(base.endswith(s) for s in exclude_suffixes):
            continue
        if is_tagged(key, pass_marker, fail_marker):
            tagged.append((key, lm))
        else:
            untagged.append((key, lm))
    pool = untagged if untagged else tagged
    pool.sort(key=lambda x: x[1], reverse=True)
    return pool[0][0] if pool else None