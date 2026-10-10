#!/usr/bin/env python3
"""ローカルの動画を非公開の S3 バケットに置き、1時間有効な署名付き https URL を作る。

Threads の投稿 API は公開 URL から動画を取りに来る。そこでローカルのファイルを非公開バケットにコピーし、
短時間だけ有効な署名付き URL を Threads に渡す。URL は保存しない（期限が切れるため）。

設定（common.SETTINGS 参照）: media_bucket（必須）、aws_profile・aws_region（任意）。
`aws` CLI が必要。引数は subprocess にリストで渡す（シェルの単語分割の問題が起きない）。

使い方:
  python3 media_host.py <file>                 # アップロードし、s3:// の場所を表示する（URL は伏せる）
  python3 media_host.py <file> --show-url      # 署名付き URL も表示する
  python3 media_host.py --create-bucket        # バケットを作り、守りを固める（非公開・SSE-S3・1日で削除）
  python3 media_host.py --create-bucket --dry-run
  python3 media_host.py --check                # バケットの設定を読み戻すだけ
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

PRESIGN_S = 3600  # コンテナ作成と最大10分の処理を十分にまかなう長さ
PREFIX = "threads"
LIFECYCLE = {"Rules": [{"ID": "expire-after-1-day", "Status": "Enabled", "Filter": {"Prefix": ""},
                        "Expiration": {"Days": 1},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]}
ENCRYPTION = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
PUBLIC_BLOCK = {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True}


def local_path(v: str) -> Path | None:
    """~ を展開し、既存のファイルなら Path を、そうでなければ None を返す。"""
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
        raise RuntimeError("PATH に aws CLI が無い（AWS CLI v2 を入れてください）") from None
    if check and r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} が失敗: {redact(r.stderr.strip())[:300]}")
    return r


def stage(path: str | Path, presign_s: int = PRESIGN_S) -> str:
    """ファイルをアップロードし、署名付き https URL を返す（他のスクリプト用。表示も保存もしない）。"""
    return stage_with_location(path, presign_s)[0]


def stage_with_location(path: str | Path, presign_s: int = PRESIGN_S) -> tuple[str, str]:
    """ファイルをアップロードし、(署名付き URL, s3://bucket/key) を返す。"""
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"ファイルが無い: {p}")
    bucket = common.require("media_bucket")
    key = f"{PREFIX}/{dt.date.today():%Y%m%d}/{uuid.uuid4().hex[:12]}-{p.name}"
    aws("s3", "cp", str(p), f"s3://{bucket}/{key}", "--only-show-errors")
    url = aws("s3", "presign", f"s3://{bucket}/{key}", "--expires-in", str(presign_s)).stdout.strip()
    if not url.startswith("https://"):
        raise RuntimeError("署名付き URL が https ではない")
    return url, f"s3://{bucket}/{key}"


def _json(r: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def check_bucket(bucket: str) -> dict:
    """守りの設定を読み戻す。{"public_block", "encryption", "lifecycle"} -> bool を返す。"""
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
        print(f"読み戻し {k}: {'OK' if ok else 'NG'}")
    if not all(back.values()):
        print("エラー: バケットの設定を読み戻したら、期待どおりになっていない", file=sys.stderr)
        return 1
    print(f"OK: バケット {bucket} は非公開・暗号化済みで、置いたものは1日で消える")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ローカルのファイルを非公開の S3 に置き、1時間有効な署名付き URL を作る。")
    ap.add_argument("file", nargs="?", help="アップロードするローカルのファイル")
    ap.add_argument("--create-bucket", action="store_true",
                    help="バケットを作る（無ければ）。公開アクセスのブロック・SSE-S3・1日で削除のルールを付ける")
    ap.add_argument("--check", action="store_true", help="バケットの守りの設定を読み戻す")
    ap.add_argument("--dry-run", action="store_true", help="--create-bucket と併用: 手順を表示するだけ")
    ap.add_argument("--expires-in", type=int, default=PRESIGN_S)
    ap.add_argument("--show-url", action="store_true",
                    help="署名付き URL を表示する（1時間はファイルを誰でも取れる URL なので、既定では伏せる）")
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
            ap.error("ファイルを渡すか、--create-bucket / --check を付けてください")
        url, loc = stage_with_location(a.file, a.expires_in)
        print(url if a.show_url else f"アップロードした: {loc}（署名付き URL は伏せた。表示するには --show-url）")
        return 0
    except RuntimeError as e:
        print(f"エラー: {redact(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
