#!/usr/bin/env python3
"""クリップと完成動画の確認用コンタクトシートと数値チェック（ローカルの ffmpeg だけ。API は使わない）。

  python3 qa_frames.py --project t004-camp --cut c01     # clips/c01.mp4 を 4 fps、8 列で
  python3 qa_frames.py --project t004-camp --all         # 全クリップ + カット間のつなぎ目の差
  python3 qa_frames.py --project t004-camp --final       # final/*.mp4 を 2 fps で

チェック
  freeze  : ffmpeg の freezedetect（n=0.003, d=0.4）。0.4秒より長く止まった絵は死んで見える
  motion  : 続けて抜き出したコマ同士の平均絶対差（0-1。ほぼ 0 = 動きなし）
  seams   : あるクリップの最後のコマと次のクリップの最初のコマの平均絶対差（0-1）。
            普通のカット切り替えなら大きいのが普通。続きのショットで大きいと、跳びが目に見える。
qa/<name>-sheet.jpg を書き、結果を qa/qa.json にまとめる。要約を表示する。
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


# --- 解析と計算（単体テストあり） -----------------------------------------------------------

def parse_freezedetect(log: str, clip_duration: float | None = None) -> list[dict]:
    """ffmpeg freezedetect のログ行から静止区間を取り出す。終わりの無い区間はクリップの最後までとする。"""
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
    """バイナリ PPM（P6、maxval 255）を読む。(幅, 高さ, RGB のバイト列) を返す。"""
    if not data.startswith(b"P6"):
        raise ValueError("P6 形式の PPM ではない")
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
    pos += 1  # maxval の後の空白1文字
    w, h, maxval = fields
    if maxval != 255:
        raise ValueError("maxval は 255 だけに対応")
    pix = data[pos:pos + w * h * 3]
    if len(pix) != w * h * 3:
        raise ValueError("PPM が途中で切れている")
    return w, h, pix


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """バイトごとの平均絶対差（0-1 に正規化）。"""
    if len(a) != len(b) or not a:
        raise ValueError("2つのバッファは同じ大きさで、空でないこと")
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


# --- ffmpeg ------------------------------------------------------------------------------------

def _ffmpeg_bytes(*args: str) -> bytes:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg が失敗した: {r.stderr.decode(errors='replace').strip()[:300]}")
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
        raise RuntimeError(f"freezedetect が失敗した: {r.stderr.strip()[-300:]}")
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
    return common.safe_path(project, f"clips/{common.check_id(name, 'クリップ名')}.mp4", "クリップ")


def check_video(video: Path, name: str, qa_dir: Path, fps: float) -> dict:
    dur = common.duration(video)
    sheet = qa_dir / f"{name}-sheet.jpg"
    contact_sheet(video, sheet, fps, dur)
    spans = freeze_spans(video, dur)
    return {"file": str(video), "duration": round(dur, 3), "sheet": str(sheet), "fps": fps,
            "freeze_spans": spans, "flag_freeze": bool(spans), "motion": motion_score(video, fps)}


def main() -> None:
    ap = argparse.ArgumentParser(description="コンタクトシートを作り、静止・動き・つなぎ目をチェックする。")
    ap.add_argument("--project", help="プロジェクトのフォルダまたはスラッグ（省略時は今のフォルダ）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cut", action="append", help="クリップ名。例: c01 や c01-draft（複数回指定できる）")
    g.add_argument("--all", action="store_true", help="shots.json のカットのうち clips/<id>.mp4 があるもの全部")
    g.add_argument("--final", action="store_true", help="final/*.mp4 を確認する")
    ap.add_argument("--fps", type=float, help="コマを抜き出す fps（既定はクリップ 4、完成動画 2）")
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
                       if common.safe_path(project, f, "完成ファイル"))
        if not files:
            raise FileNotFoundError("final/*.mp4 が無い")
        qa.setdefault("final", {})
        for f in files:
            r = check_video(f, f.stem, qa_dir, fps)
            qa["final"][f.name] = r
            lines.append(_summary(f.name, r))
    else:
        fps = a.fps or 4.0
        if a.all:
            names = [common.check_id(c.get("id", ""), "カット ID") for c in common.load_shots(project).get("cuts", [])]
            names = [n for n in names if clip_path(project, n).exists()]
            if not names:
                raise FileNotFoundError("clips/<cut>.mp4 が見つからない")
        else:
            names = [common.check_id(n, "クリップ名") for n in a.cut]
        for n in names:
            f = clip_path(project, n)
            if not f.exists():
                raise FileNotFoundError(f"クリップが無い: clips/{n}.mp4")
            r = check_video(f, n, qa_dir, fps)
            qa["clips"][n] = r
            lines.append(_summary(n, r))
        if a.all and len(names) > 1:
            seams = []
            for x, y in zip(names, names[1:]):
                d = seam_diff(clip_path(project, x), clip_path(project, y))
                seams.append({"from": x, "to": y, "diff": d, "jump": d > SEAM_JUMP})
                lines.append(f"つなぎ目 {x}->{y}: 差 {d:.3f}" + ("  （大きい: 普通のカット切り替えなら問題なし、"
                                                                 "続きのショットなら跳んで見える）" if d > SEAM_JUMP else ""))
            qa["seams"] = seams

    qa["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    common.save_json(qa_path, qa)
    for ln in lines:
        print(ln)
    print(f"書き出した: {qa_path.relative_to(project)}")


def _summary(name: str, r: dict) -> str:
    s = f"{name}: {r['duration']:.2f}秒 動き {r['motion'] if r['motion'] is not None else '測定不可'}"
    if r["freeze_spans"]:
        spans = ", ".join(f"{x['start']:.2f}-{x['end']:.2f}s" if x["end"] is not None else f"{x['start']:.2f}s-"
                          for x in r["freeze_spans"])
        s += f"  静止 {spans}"
    return s + f"  シート {Path(r['sheet']).name}"


if __name__ == "__main__":
    common.cli(main)
