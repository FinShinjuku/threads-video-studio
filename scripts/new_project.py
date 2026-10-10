#!/usr/bin/env python3
"""Create a project skeleton for one video.

  python3 new_project.py t004-camp --type story
  python3 new_project.py t005-intro --type intro --cuts 6 --workspace ~/threads-videos

Creates <workspace>/<slug>/ with brief.md, shots.json and the standard sub-folders (see SPEC.md).
Safe to re-run: existing files are never overwritten, missing folders are added.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import common

SUBDIRS = ["storyboard", "character/refs", "frames", "clips", "voice", "bgm/candidates", "qa", "video", "final"]
TYPES = {
    # type -> (default cut count, default seconds per cut, target length)
    "story": (3, 4.0, "10-15s"),
    "intro": (5, 5.0, "20-30s"),
}

BRIEF = """# {slug}

- type: {type} (target {target})
- audience:
- thesis (one sentence):
- feeling after watching:
- call to action:

## Notes
"""


def make_shots(slug: str, vtype: str, n: int, sec: float) -> dict:
    cuts, t = [], 0.0
    for i in range(1, n + 1):
        cuts.append({
            "id": f"c{i:02d}",
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
    ap = argparse.ArgumentParser(description="Create a video project folder (brief.md, shots.json, sub-folders).")
    ap.add_argument("slug", help="folder name, e.g. t004-camp (letters, digits, - and _)")
    ap.add_argument("--type", choices=sorted(TYPES), default="story")
    ap.add_argument("--cuts", type=int, help="number of cuts (default: story=3, intro=5)")
    ap.add_argument("--workspace", help="parent folder (default: setting 'workspace' or ~/threads-videos)")
    a = ap.parse_args()

    common.check_id(a.slug, "slug")
    n_default, sec, target = TYPES[a.type]
    n = a.cuts or n_default
    if not 1 <= n <= 30:
        raise ValueError("--cuts must be between 1 and 30")

    root = Path(a.workspace).expanduser() if a.workspace else common.workspace()
    root.mkdir(parents=True, exist_ok=True)
    proj = common.safe_path(root, a.slug, "slug")
    for d in SUBDIRS:
        common.safe_path(proj, d, "folder").mkdir(parents=True, exist_ok=True)

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

    print(f"project: {proj}")
    print("created: " + (", ".join(created) if created else "nothing (already exists)"))
    print(f"cuts: {n} ({a.type})")


if __name__ == "__main__":
    common.cli(main)
