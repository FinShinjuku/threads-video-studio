#!/usr/bin/env python3
"""歌なし BGM の候補を作る。

  python3 gen_bgm.py --project t004-camp --prompt "warm acoustic guitar, hopeful, 92 bpm" --count 2
  python3 gen_bgm.py --project t004-camp --prompt "..." --provider fal --fal-model lyria2

生成元（2026-10-10 確認）:
  elevenlabs（既定）    POST https://api.elevenlabs.io/v1/music  - ElevenLabs のクレジットで課金
  fal elevenlabs-music  fal-ai/elevenlabs/music  - 出力1分あたり $0.60（分単位で切り上げ）
  fal lyria2            fal-ai/lyria2            - 30秒のクリップ1本 $0.10。長さは約30秒固定で --seconds は無視

候補は bgm/candidates/<provider>-<timestamp>-<n>.mp3 に、付随情報の .json と一緒に保存する。
1つ選んで bgm/bgm.mp3 にコピーする（assemble.py はそのファイルを使う）。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import common

EL_URL = "https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128"
FAL_MODELS = {
    "elevenlabs-music": {"endpoint": "fal-ai/elevenlabs/music", "usd": lambda s: 0.60 * math.ceil(s / 60)},
    "lyria2": {"endpoint": "fal-ai/lyria2", "usd": lambda s: 0.10},
}


def default_seconds(project: Path) -> int:
    cuts = common.load_shots(project).get("cuts", [])
    if cuts and cuts[-1].get("dur"):
        return max(10, math.ceil(float(cuts[-1].get("start") or 0) + float(cuts[-1]["dur"]) + 1))
    return 30


def gen_elevenlabs(prompt: str, seconds: int, dest: Path) -> dict:
    headers = {"xi-api-key": common.require("elevenlabs_api_key"), "Content-Type": "application/json"}
    body = {"prompt": prompt, "music_length_ms": seconds * 1000, "force_instrumental": True}
    raw, ctype = common.http_request("POST", EL_URL, headers, json.dumps(body).encode(), timeout=600, retries=1)
    if "json" in ctype.lower() or len(raw) < 1000:
        raise RuntimeError(f"音楽の応答が想定外（{ctype}）: {raw[:200]!r}")
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(raw)
    tmp.replace(dest)
    return {"endpoint": "elevenlabs /v1/music", "params": {k: v for k, v in body.items() if k != "prompt"}}


def gen_fal(model: str, prompt: str, seconds: int, dest: Path) -> dict:
    ep = FAL_MODELS[model]["endpoint"]
    if model == "elevenlabs-music":
        body = {"prompt": prompt, "music_length_ms": seconds * 1000, "force_instrumental": True,
                "output_format": "mp3_44100_128"}
    else:
        body = {"prompt": prompt, "negative_prompt": "vocals, singing, speech, low quality"}
    res = common.fal_run(ep, body)
    url = (res.get("audio") or {}).get("url")
    if not url:
        raise RuntimeError(f"応答に音声がない: {str(res)[:300]}")
    raw = dest.with_name(dest.stem + ".download")
    common.media_url_to_file(url, raw)
    # mp3 にそろえる（lyria2 は wav を返す）
    common.run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
               "-codec:a", "libmp3lame", "-b:a", "192k", str(dest))
    raw.unlink(missing_ok=True)
    return {"endpoint": ep, "params": {k: v for k, v in body.items() if k != "prompt"}, "seed": res.get("seed")}


def main() -> None:
    ap = argparse.ArgumentParser(description="歌なし BGM の候補を bgm/candidates/ に作る。")
    ap.add_argument("--project", help="プロジェクトのフォルダまたはスラッグ（省略時は今のフォルダ）")
    ap.add_argument("--prompt", required=True, help="曲調・雰囲気・テンポ・楽器（歌なしに固定）")
    ap.add_argument("--seconds", type=int, help="長さ（秒）。省略時は動画の長さ + 1、最低 10")
    ap.add_argument("--count", type=int, default=1, help="候補の数（1-4、既定 1）")
    ap.add_argument("--provider", choices=["elevenlabs", "fal"], default="elevenlabs")
    ap.add_argument("--fal-model", choices=sorted(FAL_MODELS), default="elevenlabs-music")
    ap.add_argument("--dry-run", action="store_true", help="計画と費用だけ出す。生成はしない")
    a = ap.parse_args()

    project = common.project_dir(a.project)
    seconds = a.seconds or default_seconds(project)
    if not 3 <= seconds <= 300:
        raise ValueError("--seconds が範囲外。使える値: 3-300")
    if not 1 <= a.count <= 4:
        raise ValueError("--count が範囲外。使える値: 1-4")
    name = "elevenlabs" if a.provider == "elevenlabs" else f"fal-{a.fal_model}"
    if a.provider == "fal":
        cost = f"~${FAL_MODELS[a.fal_model]['usd'](seconds) * a.count:.2f}"
        if a.fal_model == "lyria2":
            cost += "（lyria2 の長さは約30秒固定）"
    else:
        cost = "ElevenLabs のクレジット（プランによる）"
    print(f"{name} で {seconds}秒 x {a.count} 本。費用: {cost}")
    if a.dry_run:
        return

    out_dir = common.safe_path(project, "bgm/candidates")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(1, a.count + 1):
        dest = out_dir / f"{name}-{stamp}-{i}.mp3"
        t0 = time.time()
        info = gen_elevenlabs(a.prompt, seconds, dest) if a.provider == "elevenlabs" else \
            gen_fal(a.fal_model, a.prompt, seconds, dest)
        meta = {"provider": a.provider, "model": None if a.provider == "elevenlabs" else a.fal_model,
                "prompt": a.prompt, "seconds_requested": seconds, **info,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            meta["seconds_actual"] = round(common.duration(dest), 2)
        except RuntimeError:
            pass
        common.save_json(dest.with_name(dest.name + ".json"), meta)
        print(f"保存した {dest.relative_to(project)}（{time.time() - t0:.0f}秒）")
    print("1つ選んで bgm/bgm.mp3 にコピーする")


if __name__ == "__main__":
    common.cli(main)
