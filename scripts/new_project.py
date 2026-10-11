#!/usr/bin/env python3
"""動画1本分のプロジェクトのひな形を作る。

  python3 new_project.py t004-camp --type story
  python3 new_project.py t005-intro --type intro --cuts 6 --workspace ~/threads-videos

<workspace>/<slug>/ に brief.md・shots.json・標準のサブフォルダを作る（SPEC.md 参照）。
何度実行してもよい。既存のファイルは上書きせず、足りないフォルダだけ足す。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import common

SUBDIRS = ["storyboard", "character/refs", "frames", "clips", "voice", "bgm/candidates", "qa", "video", "final"]
TYPES = {
    # 種類 -> (既定のカット数, 1カットの既定秒数, 目標の尺)
    "short": (2, 4.0, "5-10s"),
    "story": (3, 4.0, "10-15s"),
    "intro": (5, 5.0, "20-30s"),
}

BRIEF = """# {slug}

- 種類: {type}（目標 {target}）
- 見てほしい人:
- 伝えたいこと（1文）:
- 見終わったあとの気持ち:
- 誘導先（CTA）:

## メモ
"""


def make_shots(slug: str, vtype: str, n: int, sec: float) -> dict:
    cuts, t = [], 0.0
    for i in range(1, n + 1):
        cuts.append({
            "id": f"c{i:02d}",
            "chapter": "",
            "asset_id": "",
            "picture": "",
            "kind": "ai",
            "line": "",
            "caption": [],
            "action": "",
            "camera": "",
            "prompt": "",
            "model": "seedance20",
            "frame": f"frames/c{i:02d}.png",
            "end_frame": None,
            "refs": [],
            "start": round(t, 2),
            "dur": sec,
            "note": "",
        })
        t += sec
    return {
        "slug": slug,
        "type": vtype,
        "fps": 24,
        "cta": {"label": "LINEで話してみる", "sub": "プロフィールのリンクから", "show": True},
        "cuts": cuts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="動画プロジェクトのフォルダを作る（brief.md・shots.json・サブフォルダ）。")
    ap.add_argument("slug", help="フォルダ名。例 t004-camp（英数字・- ・_）")
    ap.add_argument("--type", choices=sorted(TYPES), default="story")
    ap.add_argument("--cuts", type=int, help="カット数（既定: story=3、intro=5）")
    ap.add_argument("--workspace", help="作成先の親フォルダ（既定: 設定 'workspace'、無ければ ~/threads-videos）")
    a = ap.parse_args()

    common.check_id(a.slug, "スラッグ")
    n_default, sec, target = TYPES[a.type]
    n = a.cuts or n_default
    if not 1 <= n <= 30:
        raise ValueError("--cuts は 1〜30 で指定する")

    root = Path(a.workspace).expanduser() if a.workspace else common.workspace()
    root.mkdir(parents=True, exist_ok=True)
    proj = common.safe_path(root, a.slug, "スラッグ")
    for d in SUBDIRS:
        common.safe_path(proj, d, "フォルダ").mkdir(parents=True, exist_ok=True)

    created = []
    brief = common.safe_path(proj, "brief.md")
    if not brief.exists():
        brief.write_text(BRIEF.format(slug=a.slug, type=a.type, target=target))
        created.append("brief.md")
    shots = common.safe_path(proj, "shots.json")
    if not shots.exists():
        common.save_json(shots, make_shots(a.slug, a.type, n, sec))
        created.append("shots.json")
    script = common.safe_path(proj, "voice/script.txt")
    if not script.exists():
        script.write_text("")
        created.append("voice/script.txt")

    print(f"プロジェクト: {proj}")
    print("作成: " + (", ".join(created) if created else "なし（すでにある）"))
    print(f"カット数: {n}（{a.type}）")


if __name__ == "__main__":
    common.cli(main)
