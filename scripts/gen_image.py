#!/usr/bin/env python3
"""fal で文章から画像を作る、または画像を編集する（開始フレーム・キャラクター設定画・修正）。

  # 文章から画像
  python3 gen_image.py --prompt "..." --out frames/c01.png --project t004-camp
  # 編集・参照: --image を1つ以上渡す（1つ目 = 編集する画像、残り = 参照画像）
  python3 gen_image.py --prompt "Change only the sky to sunset" --image frames/c01.png --out frames/c01-fix.png
  python3 gen_image.py --model nano-banana-pro --prompt "..." --image character/sheet.png --out frames/c02.png

モデル（エンドポイントは 2026-10-10 に https://fal.ai/models/<endpoint>/llms.txt で確認）:
  seedream45       fal-ai/bytedance/seedream/v4.5/text-to-image | .../edit   1枚 $0.04
  nano-banana-pro  fal-ai/nano-banana-pro | fal-ai/nano-banana-pro/edit      1枚 $0.15（4K は2倍）

<out> と <out>.json（付随情報: モデル・エンドポイント・プロンプト・入力・サイズ・seed・費用の見積もり）を書く。
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
        "usd_per_image": 0.04,  # 2026-10-10 確認
    },
    "nano-banana-pro": {
        "t2i": "fal-ai/nano-banana-pro",
        "edit": "fal-ai/nano-banana-pro/edit",
        "max_images": 14,  # llms.txt に上限の記載がないので、控えめに 14 とする
        "usd_per_image": 0.15,  # 2026-10-10 確認。4K 出力は2倍の料金
    },
}
NANO_ASPECTS = ["21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16"]


def parse_size(s: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in s.lower().split("x"))
    except ValueError:
        raise ValueError(f"--size は 1440x2560 の形で指定する（指定値: {s!r}）") from None
    if w <= 0 or h <= 0:
        raise ValueError("--size は正の数にする")
    return w, h


def seedream_size(w: int, h: int) -> dict:
    """Seedream 4.5: 幅と高さが 1920〜4096、または総画素数が 2560*1440〜4096*4096。"""
    px = w * h
    if (1920 <= w <= 4096 and 1920 <= h <= 4096) or (2560 * 1440 <= px <= 4096 * 4096):
        return {"width": w, "height": h}
    raise ValueError(f"サイズ {w}x{h} は seedream45 では使えない。使える値: 幅と高さが 1920-4096、"
                     f"または総画素数が {2560 * 1440}-{4096 * 4096}（例: 1440x2560, 2160x3840）")


def nano_params(w: int, h: int) -> dict:
    """nano-banana-pro は画素数で指定できないので、いちばん近い縦横比と解像度の段階に置き換える。"""
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
            raise ValueError(f"{model} には --image が多すぎる。使える値: {m['max_images']} 枚まで")
        body["image_urls"] = image_uris
    if seed is not None:
        body["seed"] = seed
    return endpoint, body


def main() -> None:
    ap = argparse.ArgumentParser(description="fal で画像を作る・編集する（seedream45 / nano-banana-pro）。")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--image", action="append", default=[], help="入力画像（複数回指定できる）: 編集元・参照画像")
    ap.add_argument("--model", choices=sorted(MODELS), default="seedream45")
    ap.add_argument("--size", default="1440x2560", help="幅x高さ（既定 1440x2560、9:16）")
    ap.add_argument("--out", required=True, help="出力ファイル（.png か .jpg）")
    ap.add_argument("--project", help="プロジェクトのフォルダまたはスラッグ。--out と --image はこの中に置く（省略時は今のフォルダ）")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--force", action="store_true", help="既にある --out を上書きする")
    ap.add_argument("--dry-run", action="store_true", help="リクエスト（画像データは除く）と費用だけ表示する")
    a = ap.parse_args()

    project = common.project_dir(a.project) if a.project else None
    base = project or Path.cwd()  # 入出力はプロジェクト（または今のフォルダ）の中に限る
    out = common.safe_path(base, a.out, "--out")
    if out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("--out は .png か .jpg で終わる名前にする")
    if out.exists() and not a.force and not a.dry_run:
        raise RuntimeError(f"{out} は既にある（上書きするなら --force）")
    inputs = [common.safe_path(base, p, "--image") for p in a.image]
    for p in inputs:
        if not p.exists():
            raise FileNotFoundError(f"入力画像が見つからない: {p}")
    size = parse_size(a.size)
    cost = MODELS[a.model]["usd_per_image"] * (2 if a.model == "nano-banana-pro" and max(size) > 2600 else 1)

    if a.dry_run:
        endpoint, body = build_body(a.model, a.prompt, size, ["<image>"] * len(inputs), out.suffix.lower(), a.seed)
        print(f"エンドポイント: {endpoint}")
        print(f"本文: { {k: v for k, v in body.items() if k != 'prompt'} }")
        print(f"費用の見積もり: ${cost:.2f}")
        return

    uris = [common.data_uri(p, max_width=2048) for p in inputs]
    endpoint, body = build_body(a.model, a.prompt, size, uris, out.suffix.lower(), a.seed)
    t0 = time.time()
    res = common.fal_run(endpoint, body)
    images = res.get("images") or []
    if not images or not images[0].get("url"):
        raise RuntimeError(f"応答に画像がない: {str(res)[:300]}")
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
    print(f"保存した {out}（{sidecar['seconds']}秒、約 ${cost:.2f}）")


if __name__ == "__main__":
    common.cli(main)
