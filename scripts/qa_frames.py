#!/usr/bin/env python3
"""QA contact sheets and numeric checks for clips and the final video (local ffmpeg only, no API).

  python3 qa_frames.py --project t004-camp --cut c01     # clips/c01.mp4 at 4 fps, 8 columns
  python3 qa_frames.py --project t004-camp --all         # every clip + seam differences between cuts
  python3 qa_frames.py --project t004-camp --final       # final/*.mp4 at 2 fps

Checks
  freeze  : ffmpeg freezedetect (n=0.003, d=0.4) - a frozen picture longer than 0.4 s looks dead
  motion  : mean absolute difference between consecutive sampled frames (0-1; ~0 = static)
  seams   : mean absolute difference between the last frame of a clip and the first frame of the next
            (0-1). Hard cuts are normally large; for continuous shots a large value means a visible jump.
Writes qa/<name>-sheet.jpg and merges results into qa/qa.json. Prints a summary.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import tempfile
import time
from pathlib import Path

import common

FREEZE_NOISE = 0.003
FREEZE_MIN = 0.4
SEAM_JUMP = 0.25
COLS = 8


# --- parsing / math (unit-tested) -----------------------------------------------------------

def parse_freezedetect(log: str, clip_duration: float | None = None) -> list[dict]:
    """Freeze spans from ffmpeg freezedetect log lines. An unterminated span runs to the clip end."""
    spans: list[dict] = []
    cur: dict | None = None
    for m in re.finditer(r"lavfi\.freezedetect\.freeze_(start|duration|end):\s*([0-9.]+)", log):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            cur = {"start": val, "end": None, "dur": None}
            spans.append(cur)
        elif cur is not None and kind == "duration":
            cur["dur"] = val
        elif cur is not None and kind == "end":
            cur["end"] = val
            if cur["dur"] is None:
                cur["dur"] = round(val - cur["start"], 3)
            cur = None
    for s in spans:
        if s["end"] is None:
            s["end"] = clip_duration
            s["dur"] = round(clip_duration - s["start"], 3) if clip_duration is not None else None
    return spans


def read_ppm(data: bytes) -> tuple[int, int, bytes]:
    """Parse a binary PPM (P6, maxval 255). Returns (width, height, rgb bytes)."""
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 PPM")
    fields, pos = [], 2
    while len(fields) < 3:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        start = pos
        while not data[pos:pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    pos += 1  # single whitespace after maxval
    w, h, maxval = fields
    if maxval != 255:
        raise ValueError("only maxval 255 is supported")
    pix = data[pos:pos + w * h * 3]
    if len(pix) != w * h * 3:
        raise ValueError("truncated PPM")
    return w, h, pix


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute byte difference, normalised to 0-1."""
    if len(a) != len(b) or not a:
        raise ValueError("buffers must be the same non-zero size")
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


# --- ffmpeg ------------------------------------------------------------------------------------

def _ffmpeg_bytes(*args: str) -> bytes:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.decode(errors='replace').strip()[:300]}")
    return r.stdout


def contact_sheet(video: Path, out: Path, fps: float, dur: float) -> None:
    rows = max(1, math.ceil(dur * fps / COLS))
    out.parent.mkdir(parents=True, exist_ok=True)
    common.run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
               "-vf", f"fps={fps},scale=240:-2,tile={COLS}x{rows}:padding=4:margin=4",
               "-frames:v", "1", "-q:v", "3", str(out))


def freeze_spans(video: Path, dur: float) -> list[dict]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-map", "0:v:0",
                        "-vf", f"freezedetect=n={FREEZE_NOISE}:d={FREEZE_MIN}", "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"freezedetect failed: {r.stderr.strip()[-300:]}")
    return parse_freezedetect(r.stderr, dur)


def motion_score(video: Path, fps: float) -> float | None:
    w, h = 64, 112
    raw = _ffmpeg_bytes("-i", str(video), "-vf", f"fps={fps},scale={w}:{h},format=gray",
                        "-f", "rawvideo", "-")
    n = len(raw) // (w * h)
    if n < 2:
        return None
    frames = [raw[i * w * h:(i + 1) * w * h] for i in range(n)]
    return round(sum(mean_abs_diff(frames[i], frames[i + 1]) for i in range(n - 1)) / (n - 1), 4)


def edge_frame(video: Path, last: bool, tmpdir: Path) -> bytes:
    out = tmpdir / ("last.ppm" if last else "first.ppm")
    pre = ["-sseof", "-0.5"] if last else []
    post = ["-update", "1"] if last else ["-frames:v", "1"]
    _ffmpeg_bytes("-y", *pre, "-i", str(video), "-vf", "scale=144:256", *post, "-c:v", "ppm", str(out))
    return read_ppm(out.read_bytes())[2]


def seam_diff(a: Path, b: Path) -> float:
    with tempfile.TemporaryDirectory() as d:
        return round(mean_abs_diff(edge_frame(a, True, Path(d)), edge_frame(b, False, Path(d))), 4)


# --- main --------------------------------------------------------------------------------------

def clip_path(project: Path, name: str) -> Path:
    return common.safe_path(project, f"clips/{common.check_id(name, 'clip name')}.mp4", "clip")


def check_video(video: Path, name: str, qa_dir: Path, fps: float) -> dict:
    dur = common.duration(video)
    sheet = qa_dir / f"{name}-sheet.jpg"
    contact_sheet(video, sheet, fps, dur)
    spans = freeze_spans(video, dur)
    return {"file": str(video), "duration": round(dur, 3), "sheet": str(sheet), "fps": fps,
            "freeze_spans": spans, "flag_freeze": bool(spans), "motion": motion_score(video, fps)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Contact sheets + freeze / motion / seam checks.")
    ap.add_argument("--project", help="project folder or slug (default: current folder)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cut", action="append", help="clip name, e.g. c01 or c01-draft (repeatable)")
    g.add_argument("--all", action="store_true", help="all cuts in shots.json that have clips/<id>.mp4")
    g.add_argument("--final", action="store_true", help="final/*.mp4")
    ap.add_argument("--fps", type=float, help="sampling fps (default 4 for clips, 2 for final)")
    a = ap.parse_args()

    project = common.project_dir(a.project)
    qa_dir = common.safe_path(project, "qa")
    qa_dir.mkdir(exist_ok=True)
    qa_path = qa_dir / "qa.json"
    qa = common.load_json(qa_path, default={})
    qa.setdefault("clips", {})
    lines = []

    if a.final:
        fps = a.fps or 2.0
        files = sorted(f for f in common.safe_path(project, "final").glob("*.mp4")
                       if common.safe_path(project, f, "final file"))
        if not files:
            raise FileNotFoundError("no final/*.mp4")
        qa.setdefault("final", {})
        for f in files:
            r = check_video(f, f.stem, qa_dir, fps)
            qa["final"][f.name] = r
            lines.append(_summary(f.name, r))
    else:
        fps = a.fps or 4.0
        if a.all:
            names = [common.check_id(c.get("id", ""), "cut id") for c in common.load_shots(project).get("cuts", [])]
            names = [n for n in names if clip_path(project, n).exists()]
            if not names:
                raise FileNotFoundError("no clips/<cut>.mp4 found")
        else:
            names = [common.check_id(n, "clip name") for n in a.cut]
        for n in names:
            f = clip_path(project, n)
            if not f.exists():
                raise FileNotFoundError(f"clip not found: clips/{n}.mp4")
            r = check_video(f, n, qa_dir, fps)
            qa["clips"][n] = r
            lines.append(_summary(n, r))
        if a.all and len(names) > 1:
            seams = []
            for x, y in zip(names, names[1:]):
                d = seam_diff(clip_path(project, x), clip_path(project, y))
                seams.append({"from": x, "to": y, "diff": d, "jump": d > SEAM_JUMP})
                lines.append(f"seam {x}->{y}: diff {d:.3f}" + ("  (large: fine for a hard cut, "
                                                                 "a jump for a continuous shot)" if d > SEAM_JUMP else ""))
            qa["seams"] = seams

    qa["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    common.save_json(qa_path, qa)
    for ln in lines:
        print(ln)
    print(f"written: {qa_path.relative_to(project)}")


def _summary(name: str, r: dict) -> str:
    s = f"{name}: {r['duration']:.2f}s motion {r['motion'] if r['motion'] is not None else 'n/a'}"
    if r["freeze_spans"]:
        spans = ", ".join(f"{x['start']:.2f}-{x['end']:.2f}s" if x["end"] is not None else f"{x['start']:.2f}s-"
                          for x in r["freeze_spans"])
        s += f"  FREEZE {spans}"
    return s + f"  sheet {Path(r['sheet']).name}"


if __name__ == "__main__":
    common.cli(main)
