#!/usr/bin/env python3
"""Stage a local video on a private S3 bucket and print a 1-hour presigned https URL.

The Threads publishing API pulls media from a public URL, so the local file is copied to a private
bucket and a short-lived presigned URL is handed to Threads. The URL is never stored (it expires).

Settings (see common.SETTINGS): media_bucket (required), aws_profile, aws_region (optional).
Requires the `aws` CLI. Arguments are passed to subprocess as a list (no shell word-splitting issues).

Usage:
  python3 media_host.py <file>                 # upload; prints the s3:// location (URL hidden)
  python3 media_host.py <file> --show-url      # also print the presigned URL
  python3 media_host.py --create-bucket        # create/harden the bucket (private, SSE-S3, delete after 1 day)
  python3 media_host.py --create-bucket --dry-run
  python3 media_host.py --check                # read back the bucket settings only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from threads_api import redact  # noqa: E402

PRESIGN_S = 3600  # covers container creation + up to 10 minutes of processing
PREFIX = "threads"
LIFECYCLE = {"Rules": [{"ID": "expire-after-1-day", "Status": "Enabled", "Filter": {"Prefix": ""},
                        "Expiration": {"Days": 1},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]}
ENCRYPTION = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
PUBLIC_BLOCK = {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True}


def local_path(v: str) -> Path | None:
    """Expand ~ and return the Path if it is an existing file, else None."""
    v = (v or "").strip()
    if not v:
        return None
    p = Path(os.path.expanduser(v))
    return p if p.is_file() else None


def _base_args() -> list[str]:
    args = []
    if common.get("aws_profile"):
        args += ["--profile", common.get("aws_profile")]
    if common.get("aws_region"):
        args += ["--region", common.get("aws_region")]
    return args


def aws(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    cmd = ["aws", *args, *_base_args()]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("aws CLI not found on PATH (install AWS CLI v2)") from None
    if check and r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {redact(r.stderr.strip())[:300]}")
    return r


def stage(path: str | Path, presign_s: int = PRESIGN_S) -> str:
    """Upload the file and return a presigned https URL (for other scripts; never print or store it)."""
    return stage_with_location(path, presign_s)[0]


def stage_with_location(path: str | Path, presign_s: int = PRESIGN_S) -> tuple[str, str]:
    """Upload the file; return (presigned URL, s3://bucket/key)."""
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"file not found: {p}")
    bucket = common.require("media_bucket")
    key = f"{PREFIX}/{dt.date.today():%Y%m%d}/{uuid.uuid4().hex[:12]}-{p.name}"
    aws("s3", "cp", str(p), f"s3://{bucket}/{key}", "--only-show-errors")
    url = aws("s3", "presign", f"s3://{bucket}/{key}", "--expires-in", str(presign_s)).stdout.strip()
    if not url.startswith("https://"):
        raise RuntimeError("presigned URL is not https")
    return url, f"s3://{bucket}/{key}"


def _json(r: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def check_bucket(bucket: str) -> dict:
    """Read back the hardening settings. Returns {"public_block", "encryption", "lifecycle"} -> bool."""
    pab = _json(aws("s3api", "get-public-access-block", "--bucket", bucket, check=False))
    enc = _json(aws("s3api", "get-bucket-encryption", "--bucket", bucket, check=False))
    lc = _json(aws("s3api", "get-bucket-lifecycle-configuration", "--bucket", bucket, check=False))
    cfg = pab.get("PublicAccessBlockConfiguration", {})
    rules = enc.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    algos = {r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") for r in rules}
    days = [r.get("Expiration", {}).get("Days") for r in lc.get("Rules", []) if r.get("Status") == "Enabled"]
    return {"public_block": all(cfg.get(k) is True for k in PUBLIC_BLOCK),
            "encryption": bool(algos & {"AES256", "aws:kms", "aws:kms:dsse"}),
            "lifecycle": any(d == 1 for d in days)}


def create_bucket(dry_run: bool = False) -> int:
    bucket = common.require("media_bucket")
    region = common.get("aws_region")
    steps = []
    if aws("s3api", "head-bucket", "--bucket", bucket, check=False).returncode != 0:
        create = ["s3api", "create-bucket", "--bucket", bucket]
        if region and region != "us-east-1":
            create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
        steps.append(create)
    steps += [
        ["s3api", "put-public-access-block", "--bucket", bucket,
         "--public-access-block-configuration", json.dumps(PUBLIC_BLOCK)],
        ["s3api", "put-bucket-encryption", "--bucket", bucket,
         "--server-side-encryption-configuration", json.dumps(ENCRYPTION)],
        ["s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket,
         "--lifecycle-configuration", json.dumps(LIFECYCLE)],
    ]
    for s in steps:
        print(("[dry-run] " if dry_run else "") + "aws " + " ".join(s[:2]))
        if not dry_run:
            aws(*s)
    if dry_run:
        return 0
    back = check_bucket(bucket)
    for k, ok in back.items():
        print(f"readback {k}: {'OK' if ok else 'NG'}")
    if not all(back.values()):
        print("error: bucket settings did not read back as expected", file=sys.stderr)
        return 1
    print(f"ok: bucket {bucket} is private, encrypted, and deletes objects after 1 day")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage a local file on private S3 and print a 1-hour presigned URL.")
    ap.add_argument("file", nargs="?", help="local file to upload")
    ap.add_argument("--create-bucket", action="store_true",
                    help="create the bucket (if missing) with public access block, SSE-S3 and 1-day lifecycle")
    ap.add_argument("--check", action="store_true", help="read back the bucket hardening settings")
    ap.add_argument("--dry-run", action="store_true", help="with --create-bucket: print the steps only")
    ap.add_argument("--expires-in", type=int, default=PRESIGN_S)
    ap.add_argument("--show-url", action="store_true",
                    help="print the presigned URL (it grants access to the file for an hour; hidden by default)")
    a = ap.parse_args(argv)
    try:
        if a.create_bucket:
            return create_bucket(a.dry_run)
        if a.check:
            back = check_bucket(common.require("media_bucket"))
            for k, ok in back.items():
                print(f"{k}: {'OK' if ok else 'NG'}")
            return 0 if all(back.values()) else 1
        if not a.file:
            ap.error("give a file, or --create-bucket / --check")
        url, loc = stage_with_location(a.file, a.expires_in)
        print(url if a.show_url else f"uploaded: {loc} (presigned URL hidden; use --show-url to print it)")
        return 0
    except RuntimeError as e:
        print(f"error: {redact(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
