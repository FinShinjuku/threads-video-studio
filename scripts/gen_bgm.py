#!/usr/bin/env python3
"""Instrumental BGM candidates.

  python3 gen_bgm.py --project t004-camp --prompt "warm acoustic guitar, hopeful, 92 bpm" --count 2
  python3 gen_bgm.py --project t004-camp --prompt "..." --provider fal --fal-model lyria2

Providers (checked 2026-10-10):
  elevenlabs (default)  POST https://api.elevenlabs.io/v1/music  - billed in ElevenLabs credits
  fal elevenlabs-music  fal-ai/elevenlabs/music  - $0.60 per output minute, rounded up to whole minutes
  fal lyria2            fal-ai/lyria2            - $0.10 per 30 s clip; fixed length (~30 s), --seconds ignored

Candidates go to bgm/candidates/<provider>-<timestamp>-<n>.mp3 with a .json sidecar.
Pick one and copy it to bgm/bgm.mp3 (assemble.py uses that file).
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
        raise RuntimeError(f"unexpected music response ({ctype}): {raw[:200]!r}")
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
        raise RuntimeError(f"no audio in response: {str(res)[:300]}")
    raw = dest.with_name(dest.stem + ".download")
    common.media_url_to_file(url, raw)
    # normalise to mp3 (lyria2 returns wav)
    common.run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
               "-codec:a", "libmp3lame", "-b:a", "192k", str(dest))
    raw.unlink(missing_ok=True)
    return {"endpoint": ep, "params": {k: v for k, v in body.items() if k != "prompt"}, "seed": res.get("seed")}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate instrumental BGM candidates into bgm/candidates/.")
    ap.add_argument("--project", help="project folder or slug (default: current folder)")
    ap.add_argument("--prompt", required=True, help="style/mood/tempo/instruments (instrumental is forced)")
    ap.add_argument("--seconds", type=int, help="length in seconds (default: video length + 1, min 10)")
    ap.add_argument("--count", type=int, default=1, help="number of candidates (1-4, default 1)")
    ap.add_argument("--provider", choices=["elevenlabs", "fal"], default="elevenlabs")
    ap.add_argument("--fal-model", choices=sorted(FAL_MODELS), default="elevenlabs-music")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and cost; generate nothing")
    a = ap.parse_args()

    project = common.project_dir(a.project)
    seconds = a.seconds or default_seconds(project)
    if not 3 <= seconds <= 300:
        raise ValueError("--seconds out of range. Allowed: 3-300")
    if not 1 <= a.count <= 4:
        raise ValueError("--count out of range. Allowed: 1-4")
    name = "elevenlabs" if a.provider == "elevenlabs" else f"fal-{a.fal_model}"
    if a.provider == "fal":
        cost = f"~${FAL_MODELS[a.fal_model]['usd'](seconds) * a.count:.2f}"
        if a.fal_model == "lyria2":
            cost += " (lyria2 length is fixed at ~30 s)"
    else:
        cost = "ElevenLabs credits (depends on your plan)"
    print(f"{a.count} x {seconds}s via {name}; cost: {cost}")
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
        print(f"saved {dest.relative_to(project)} ({time.time() - t0:.0f}s)")
    print("choose one and copy it to bgm/bgm.mp3")


if __name__ == "__main__":
    common.cli(main)
