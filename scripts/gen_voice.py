#!/usr/bin/env python3
"""One-pass narration with ElevenLabs v3 + character timestamps, then cut timing.

  python3 gen_voice.py --project t004-camp --dry-run            # show the text that would be sent
  python3 gen_voice.py --project t004-camp --tag "[cheerfully]"  # generate (paid)
  python3 gen_voice.py --project t004-camp --retime              # recompute timing from voice/align.json (free)

The whole script goes in ONE request so the voice stays consistent across lines:
  POST /v1/text-to-speech/{voice_id}/with-timestamps  (model eleven_v3, language_code ja)
Lines are joined with blank lines; an optional single leading emotion tag is prepended.
voice/yomi.json replacements are applied to the SPOKEN text only; captions keep the original line.

Outputs: voice/all.mp3, voice/align.json, voice/lines.json (cut, text, spoken, start, end) and the
start/dur of every cut in shots.json: each cut switches 0.15 s before the next line starts; the last cut
ends 1.0 s after the last line.
"""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

import common
from yomi_check import apply_yomi, load_yomi, read_script_lines

API = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128"
LEAD = 0.15   # switch to the next cut this long before its line starts
TAIL = 1.0    # keep the last cut this long after the last line ends


def build_text(spoken_lines: list[str], tag: str | None) -> str:
    body = "\n\n".join(s.strip() for s in spoken_lines)
    return f"{tag.strip()} {body}" if tag else body


def _owner_index(chars: list[str]) -> tuple[str, list[int]]:
    joined, owner = "", []
    for i, c in enumerate(chars):
        joined += c
        owner.extend([i] * len(c))
    return joined, owner


def line_spans(alignment: dict, spoken_lines: list[str], tag: str | None = None) -> list[tuple[float, float]]:
    """(start, end) seconds of each spoken line, found in the character alignment of the full text."""
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    if not (len(chars) == len(starts) == len(ends)):
        raise ValueError("alignment arrays have different lengths")
    joined, owner = _owner_index(chars)

    spans: list[tuple[float, float]] = []
    cursor = 0
    if tag and joined.startswith(tag.strip()):
        cursor = len(tag.strip())
    ok = True
    for line in spoken_lines:
        target = line.strip()
        idx = joined.find(target, cursor) if target else -1
        if idx < 0:
            ok = False
            break
        last = idx + len(target) - 1
        spans.append((float(starts[owner[idx]]), float(ends[owner[last]])))
        cursor = last + 1
    if ok:
        return spans

    # Fallback: the service normalised some characters; use the blank-line separators instead.
    body_start = len(tag.strip()) if tag and joined.startswith(tag.strip()) else 0
    body = joined[body_start:]
    segs = []
    for m in re.finditer(r"[^\n]*\S[^\n]*(?:\n[^\n]*\S[^\n]*)*", body):  # blocks separated by blank lines
        s, e = m.start(), m.end()
        while body[s].isspace():
            s += 1
        while body[e - 1].isspace():
            e -= 1
        segs.append((body_start + s, body_start + e - 1))
    if len(segs) != len(spoken_lines):
        raise RuntimeError(f"could not locate the lines in the alignment ({len(segs)} segments for "
                           f"{len(spoken_lines)} lines). Regenerate, or check voice/script.txt and yomi.json")
    return [(float(starts[owner[a]]), float(ends[owner[b]])) for a, b in segs]


def cut_timing(spans: list[tuple[float, float]], lead: float = LEAD, tail: float = TAIL) -> list[tuple[float, float]]:
    """(start, dur) per cut: cut 1 starts at 0; cut i>1 starts `lead` s before its line; last ends `tail` after."""
    if not spans:
        return []
    starts = [0.0] + [max(0.0, s - lead) for s, _ in spans[1:]]
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            raise RuntimeError(f"line {i + 1} starts before line {i}; check the alignment")
    out = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else spans[-1][1] + tail
        out.append((round(st, 3), round(end - st, 3)))
    return out


def apply_timing(project: Path, shots: dict, lines: list[str], spoken: list[str], spans) -> list[dict]:
    cuts = shots["cuts"]
    timing = cut_timing(spans)
    records = []
    for cut, text, sp, (s, e), (cs, cd) in zip(cuts, lines, spoken, spans, timing):
        cut["start"], cut["dur"] = cs, cd
        records.append({"cut": cut["id"], "text": text, "spoken": sp, "start": round(s, 3), "end": round(e, 3)})
    common.save_shots(project, shots)
    common.save_json(common.safe_path(project, "voice/lines.json"), records)
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the whole narration in one ElevenLabs v3 request and time the cuts.")
    ap.add_argument("--project", help="project folder or slug (default: current folder)")
    ap.add_argument("--voice-id", help="ElevenLabs voice id (default: setting narrator_voice_id)")
    ap.add_argument("--tag", default="", help='one leading emotion tag, e.g. "[cheerfully]" (default: none)')
    ap.add_argument("--stability", type=float, default=0.5, help="v3 stability 0.0 / 0.5 / 1.0 (default 0.5)")
    ap.add_argument("--dry-run", action="store_true", help="print the text that would be sent; no API call")
    ap.add_argument("--retime", action="store_true", help="recompute timing from the existing voice/align.json")
    ap.add_argument("--force", action="store_true", help="regenerate even if voice/all.mp3 exists")
    a = ap.parse_args()

    tag = a.tag.strip() or None
    if tag and not re.fullmatch(r"\[[^\[\]]{1,40}\]", tag):
        raise ValueError('--tag must be a single tag like "[cheerfully]"')
    if a.stability not in (0.0, 0.5, 1.0):
        raise ValueError("--stability for eleven_v3. Allowed: 0.0, 0.5, 1.0")

    project = common.project_dir(a.project)
    shots = common.load_shots(project)
    lines = read_script_lines(project)
    cuts = shots.get("cuts", [])
    for c in cuts:
        common.check_id(c.get("id", ""), "cut id")
    if len(lines) != len(cuts):
        raise RuntimeError(f"voice/script.txt has {len(lines)} line(s) but shots.json has {len(cuts)} cut(s); "
                           f"they must match (one narration line per cut)")
    spoken = [apply_yomi(ln, load_yomi(project)) for ln in lines]
    text = build_text(spoken, tag)
    vdir = common.safe_path(project, "voice")

    if a.retime:
        al = common.load_json(vdir / "align.json")
        recs = apply_timing(project, shots, lines, spoken, line_spans(al["alignment"], spoken, tag or _tag_of(al)))
        _report(recs, shots)
        return

    print(f"characters: {len(text)} (ElevenLabs bills by characters)")
    if a.dry_run:
        print(text)
        return
    if (vdir / "all.mp3").exists() and not a.force:
        raise RuntimeError("voice/all.mp3 exists (use --force to regenerate, or --retime to only redo timing)")

    voice = a.voice_id or common.require("narrator_voice_id")
    body = {"text": text, "model_id": "eleven_v3", "language_code": "ja",
            "voice_settings": {"stability": a.stability}}
    headers = {"xi-api-key": common.require("elevenlabs_api_key")}
    res = common.http_json("POST", API.format(voice=voice), headers, body, timeout=600, retries=1)
    if not res.get("audio_base64") or not res.get("alignment"):
        raise RuntimeError("response has no audio_base64/alignment")
    vdir.mkdir(parents=True, exist_ok=True)
    tmp = vdir / "all.mp3.part"
    tmp.write_bytes(base64.b64decode(res["audio_base64"]))
    tmp.replace(vdir / "all.mp3")
    common.save_json(vdir / "align.json", {"text": text, "voice_id": voice, "tag": tag,
                                           "alignment": res["alignment"],
                                           "normalized_alignment": res.get("normalized_alignment")})
    recs = apply_timing(project, shots, lines, spoken, line_spans(res["alignment"], spoken, tag))
    _report(recs, shots)
    try:
        print(f"audio: voice/all.mp3 ({common.duration(vdir / 'all.mp3'):.2f}s)")
    except RuntimeError:
        pass


def _tag_of(al: dict) -> str | None:
    m = re.match(r"\s*(\[[^\[\]]+\])", al.get("text", ""))
    return m.group(1) if m else None


def _report(recs: list[dict], shots: dict) -> None:
    for r, c in zip(recs, shots["cuts"]):
        print(f"{r['cut']}: line {r['start']:.2f}-{r['end']:.2f}s  cut start {c['start']:.2f} dur {c['dur']:.2f}  {r['text']}")
    last = shots["cuts"][-1]
    print(f"total: {last['start'] + last['dur']:.2f}s (shots.json and voice/lines.json updated)")


if __name__ == "__main__":
    common.cli(main)
