#!/usr/bin/env python3
"""ElevenLabs v3 でナレーションを通しで1回生成し（文字ごとのタイムスタンプ付き）、カットの尺を決める。

  python3 gen_voice.py --project t004-camp --dry-run            # 送る予定の文章を表示する
  python3 gen_voice.py --project t004-camp --tag "[cheerfully]"  # 生成する（有料）
  python3 gen_voice.py --project t004-camp --retime              # voice/align.json から尺を計算し直す（無料）

行ごとに声色がぶれないよう、原稿全体を1回のリクエストで送る:
  POST /v1/text-to-speech/{voice_id}/with-timestamps（model eleven_v3、language_code ja）
行は空行でつなぐ。感情タグを1つだけ先頭に付けられる。
voice/yomi.json の置き換えは読み上げる文章にだけ使い、字幕は元の行のまま。

出力: voice/all.mp3、voice/align.json、voice/lines.json（cut, text, spoken, start, end）と、
shots.json の各カットの start/dur。各カットは次の行が始まる 0.15 秒前に切り替わり、最後のカットは
最後の行の 1.0 秒後に終わる。
"""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

import common
from yomi_check import apply_yomi, load_yomi, read_script_lines

API = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128"
LEAD = 0.15   # 次の行が始まるこの秒数前に次のカットへ切り替える
TAIL = 1.0    # 最後の行が終わってからこの秒数だけ最後のカットを残す


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
    """全文の文字アラインメントから、読み上げた各行の (開始, 終了) 秒を求める。"""
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    if not (len(chars) == len(starts) == len(ends)):
        raise ValueError("アラインメントの配列の長さがそろっていない")
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

    # 代替手段: サービス側で一部の文字が正規化されたときは、空行の区切りを使う。
    body_start = len(tag.strip()) if tag and joined.startswith(tag.strip()) else 0
    body = joined[body_start:]
    segs = []
    for m in re.finditer(r"[^\n]*\S[^\n]*(?:\n[^\n]*\S[^\n]*)*", body):  # 空行で区切られたかたまり
        s, e = m.start(), m.end()
        while body[s].isspace():
            s += 1
        while body[e - 1].isspace():
            e -= 1
        segs.append((body_start + s, body_start + e - 1))
    if len(segs) != len(spoken_lines):
        raise RuntimeError(f"アラインメントの中で行の位置が分からない（{len(spoken_lines)} 行に対して "
                           f"{len(segs)} 区切り）。作り直すか、voice/script.txt と yomi.json を確認する")
    return [(float(starts[owner[a]]), float(ends[owner[b]])) for a, b in segs]


def cut_timing(spans: list[tuple[float, float]], lead: float = LEAD, tail: float = TAIL) -> list[tuple[float, float]]:
    """カットごとの (start, dur)。1カット目は 0 から、2カット目以降は自分の行の `lead` 秒前から始まり、最後は `tail` 秒後に終わる。"""
    if not spans:
        return []
    starts = [0.0] + [max(0.0, s - lead) for s, _ in spans[1:]]
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            raise RuntimeError(f"{i + 1} 行目が {i} 行目より前に始まっている。アラインメントを確認する")
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
    ap = argparse.ArgumentParser(description="ElevenLabs v3 の1回のリクエストでナレーション全体を作り、カットの尺を決める。")
    ap.add_argument("--project", help="プロジェクトのフォルダまたはスラッグ（省略時は今のフォルダ）")
    ap.add_argument("--voice-id", help="ElevenLabs の voice id（省略時は設定の narrator_voice_id）")
    ap.add_argument("--tag", default="", help='先頭に付ける感情タグ1つ。例: "[cheerfully]"（既定: なし）')
    ap.add_argument("--stability", type=float, default=0.5, help="v3 の stability。0.0 / 0.5 / 1.0（既定 0.5）")
    ap.add_argument("--dry-run", action="store_true", help="送る予定の文章を表示するだけ。API は呼ばない")
    ap.add_argument("--retime", action="store_true", help="既にある voice/align.json から尺を計算し直す")
    ap.add_argument("--force", action="store_true", help="voice/all.mp3 があっても作り直す")
    a = ap.parse_args()

    tag = a.tag.strip() or None
    if tag and not re.fullmatch(r"\[[^\[\]]{1,40}\]", tag):
        raise ValueError('--tag は "[cheerfully]" のようなタグ1つにする')
    if a.stability not in (0.0, 0.5, 1.0):
        raise ValueError("eleven_v3 の --stability。使える値: 0.0, 0.5, 1.0")

    project = common.project_dir(a.project)
    shots = common.load_shots(project)
    lines = read_script_lines(project)
    cuts = shots.get("cuts", [])
    for c in cuts:
        common.check_id(c.get("id", ""), "カット ID")
    if len(lines) != len(cuts):
        raise RuntimeError(f"voice/script.txt は {len(lines)} 行、shots.json は {len(cuts)} カット。"
                           f"数をそろえる（1カットにナレーション1行）")
    spoken = [apply_yomi(ln, load_yomi(project)) for ln in lines]
    text = build_text(spoken, tag)
    vdir = common.safe_path(project, "voice")

    if a.retime:
        al = common.load_json(vdir / "align.json")
        recs = apply_timing(project, shots, lines, spoken, line_spans(al["alignment"], spoken, tag or _tag_of(al)))
        _report(recs, shots)
        return

    print(f"文字数: {len(text)}（ElevenLabs は文字数で課金）")
    if a.dry_run:
        print(text)
        return
    if (vdir / "all.mp3").exists() and not a.force:
        raise RuntimeError("voice/all.mp3 は既にある（作り直すなら --force、尺だけやり直すなら --retime）")

    voice = a.voice_id or common.require("narrator_voice_id")
    body = {"text": text, "model_id": "eleven_v3", "language_code": "ja",
            "voice_settings": {"stability": a.stability}}
    headers = {"xi-api-key": common.require("elevenlabs_api_key")}
    res = common.http_json("POST", API.format(voice=voice), headers, body, timeout=600, retries=1)
    if not res.get("audio_base64") or not res.get("alignment"):
        raise RuntimeError("応答に audio_base64 か alignment がない")
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
        print(f"音声: voice/all.mp3（{common.duration(vdir / 'all.mp3'):.2f}秒）")
    except RuntimeError:
        pass


def _tag_of(al: dict) -> str | None:
    m = re.match(r"\s*(\[[^\[\]]+\])", al.get("text", ""))
    return m.group(1) if m else None


def _report(recs: list[dict], shots: dict) -> None:
    for r, c in zip(recs, shots["cuts"]):
        print(f"{r['cut']}: 行 {r['start']:.2f}-{r['end']:.2f}秒  カット開始 {c['start']:.2f} 尺 {c['dur']:.2f}  {r['text']}")
    last = shots["cuts"][-1]
    print(f"合計: {last['start'] + last['dur']:.2f}秒（shots.json と voice/lines.json を更新した）")


if __name__ == "__main__":
    common.cli(main)
