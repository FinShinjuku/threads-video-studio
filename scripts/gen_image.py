#!/usr/bin/env python3
"""Text-to-image or image edit on fal (start frames, character sheets, fixes).

  # text to image
  python3 gen_image.py --prompt "..." --out frames/c01.png --project t004-camp
  # edit / reference: pass one or more --image (first = image to edit, others = references)
  python3 gen_image.py --prompt "Change only the sky to sunset" --image frames/c01.png --out frames/c01-fix.png
  python3 gen_image.py --model nano-banana-pro --prompt "..." --image character/sheet.png --out frames/c02.png

Models (endpoints checked 2026-10-10 via https://fal.ai/models/<endpoint>/llms.txt):
  seedream45       fal-ai/bytedance/seedream/v4.5/text-to-image | .../edit   $0.04 / image
  nano-banana-pro  fal-ai/nano-banana-pro | fal-ai/nano-banana-pro/edit      $0.15 / image (4K: x2)

Writes <out> and <out>.json (sidecar: model, endpoint, prompt, inputs, size, seed, cost estimate).
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import common

MODELS = {
    "seedream45": {
        "t2i": "fal-ai/bytedance/seedream/v4.5/text-to-image",
        "edit": "fal-ai/bytedance/seedream/v4.5/edit",
        "max_images": 10,
        "usd_per_image": 0.04,  # checked 2026-10-10
    },
    "nano-banana-pro": {
        "t2i": "fal-ai/nano-banana-pro",
        "edit": "fal-ai/nano-banana-pro/edit",
        "max_images": 14,  # limit not documented in llms.txt; 14 is a conservative cap
        "usd_per_image": 0.15,  # checked 2026-10-10; 4K output is billed double
    },
}
NANO_ASPECTS = ["21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16"]


def parse_size(s: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in s.lower().split("x"))
    except ValueError:
        raise ValueError(f"--size must look like 1440x2560, got {s!r}") from None
    if w <= 0 or h <= 0:
        raise ValueError("--size must be positive")
    return w, h


def seedream_size(w: int, h: int) -> dict:
    """Seedream 4.5: width and height in [1920, 4096], or total pixels in [2560*1440, 4096*4096]."""
    px = w * h
    if (1920 <= w <= 4096 and 1920 <= h <= 4096) or (2560 * 1440 <= px <= 4096 * 4096):
        return {"width": w, "height": h}
    raise ValueError(f"size {w}x{h} not accepted by seedream45. Allowed: width and height 1920-4096, "
                     f"or total pixels {2560 * 1440}-{4096 * 4096} (e.g. 1440x2560, 2160x3840)")


def nano_params(w: int, h: int) -> dict:
    """nano-banana-pro has no pixel size: map to the closest aspect ratio and a resolution tier."""
    r = w / h
    best = min(NANO_ASPECTS, key=lambda a: abs(math.log(r) - math.log(int(a.split(":")[0]) / int(a.split(":")[1]))))
    long_side = max(w, h)
    res = "1K" if long_side <= 1024 else "2K" if long_side <= 2600 else "4K"
    return {"aspect_ratio": best, "resolution": res}


def build_body(model: str, prompt: str, size: tuple[int, int], image_uris: list[str], out_suffix: str,
               seed: int | None) -> tuple[str, dict]:
    m = MODELS[model]
    endpoint = m["edit"] if image_uris else m["t2i"]
    body: dict = {"prompt": prompt, "num_images": 1}
    if model == "seedream45":
        body["image_size"] = seedream_size(*size)
        body["max_images"] = 1
    else:
        body.update(nano_params(*size))
        body["output_format"] = "jpeg" if out_suffix in (".jpg", ".jpeg") else "png"
    if image_uris:
        if len(image_uris) > m["max_images"]:
            raise ValueError(f"too many --image for {model}. Allowed: up to {m['max_images']}")
        body["image_urls"] = image_uris
    if seed is not None:
        body["seed"] = seed
    return endpoint, body


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate or edit an image on fal (seedream45 / nano-banana-pro).")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--image", action="append", default=[], help="input image (repeatable): edit source / references")
    ap.add_argument("--model", choices=sorted(MODELS), default="seedream45")
    ap.add_argument("--size", default="1440x2560", help="WxH (default 1440x2560, 9:16)")
    ap.add_argument("--out", required=True, help="output file (.png or .jpg)")
    ap.add_argument("--project", help="project folder or slug; --out/--image must stay inside it (default: current folder)")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out")
    ap.add_argument("--dry-run", action="store_true", help="show the request (without image data) and cost only")
    a = ap.parse_args()

    project = common.project_dir(a.project) if a.project else None
    base = project or Path.cwd()  # outputs and inputs must stay inside the project (or the current folder)
    out = common.safe_path(base, a.out, "--out")
    if out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("--out must end with .png or .jpg")
    if out.exists() and not a.force and not a.dry_run:
        raise RuntimeError(f"{out} exists (use --force to overwrite)")
    inputs = [common.safe_path(base, p, "--image") for p in a.image]
    for p in inputs:
        if not p.exists():
            raise FileNotFoundError(f"input image not found: {p}")
    size = parse_size(a.size)
    cost = MODELS[a.model]["usd_per_image"] * (2 if a.model == "nano-banana-pro" and max(size) > 2600 else 1)

    if a.dry_run:
        endpoint, body = build_body(a.model, a.prompt, size, ["<image>"] * len(inputs), out.suffix.lower(), a.seed)
        print(f"endpoint: {endpoint}")
        print(f"body: { {k: v for k, v in body.items() if k != 'prompt'} }")
        print(f"estimated cost: ${cost:.2f}")
        return

    uris = [common.data_uri(p, max_width=2048) for p in inputs]
    endpoint, body = build_body(a.model, a.prompt, size, uris, out.suffix.lower(), a.seed)
    t0 = time.time()
    res = common.fal_run(endpoint, body)
    images = res.get("images") or []
    if not images or not images[0].get("url"):
        raise RuntimeError(f"no image in response: {str(res)[:300]}")
    img = images[0]
    tmp = out.with_name(out.stem + ".download")
    common.media_url_to_file(img["url"], tmp)
    ctype = (img.get("content_type") or "").lower()
    want_png = out.suffix.lower() == ".png"
    if (want_png and "png" not in ctype) or (not want_png and "jpeg" not in ctype and "jpg" not in ctype):
        common.run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp), str(out))
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(out)

    sidecar = {
        "model": a.model, "endpoint": endpoint, "prompt": a.prompt,
        "inputs": [str(p) for p in inputs], "size": a.size,
        "params": {k: v for k, v in body.items() if k not in ("prompt", "image_urls")},
        "seed": res.get("seed"), "width": img.get("width"), "height": img.get("height"),
        "estimated_cost_usd": cost, "seconds": round(time.time() - t0, 1),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    common.save_json(out.with_name(out.name + ".json"), sidecar)
    print(f"saved {out} ({sidecar['seconds']}s, ~${cost:.2f})")


if __name__ == "__main__":
    common.cli(main)
