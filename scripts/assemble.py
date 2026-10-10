#!/usr/bin/env python3
"""Build the HyperFrames project for one video, and optionally render it.

Inputs (project folder):
  shots.json         cuts with start/dur (filled by gen_voice.py), line / caption, cta
  clips/<cut>.mp4    generated clips (frames/<cut>.png is used as a still if a clip is missing)
  voice/all.mp3      one continuous narration
  voice/lines.json   per-line timing [{"cut": "c01", "start": 0.2, "end": 4.8, ...}, ...]
  bgm/bgm.mp3        optional background music

Outputs:
  video/index.html, video/assets/*  (HyperFrames project; captions, CTA button, audio)
  video/build.json                  (timings and warnings)
  with --render: final/<slug>-final.mp4 (1080x1920) and final/<slug>-post.mp4 (compressed for upload)

Rules:
  - A clip shorter than its cut is padded with its last frame. More than 0.4s of padding is a WARN:
    a frozen picture looks dead - regenerate a longer clip or shorten the cut.
  - Captions: `caption` lines if given, else `line` split automatically into 1-2 lines (~15 chars each).
    [[word]] = one highlighted word (yellow). Ellipses (……) are lifted to the middle of the line.
  - The last cut shows the CTA button (cta.label, green) unless cta.show is false. No company name or logo.
  - BGM is ducked by 20 dB under narration (lines closer than 0.7s are merged), 0.5s fade-in, 1.2s fade-out.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from threads_api import check_slug, safe_path  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_ROOT / "templates" / "hyperframes" / "index.html.tmpl"
HYPERFRAMES = "hyperframes@0.8.143"
FONT_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/zenmarugothic/"
FONT_FILES = ("ZenMaruGothic-Bold.ttf", "ZenMaruGothic-Black.ttf", "OFL.txt")
FREEZE_WARN_S = 0.4
CAPTION_WIDTH = 15          # target characters per caption line
CAPTION_BOX_PX = 840        # usable caption width (1080 - 72 left - 160 right, minus outline)
CAPTION_FONT_PX = 60
DUCK_GAP_S = 0.7
DUCK_PAD_S = 0.1
DUCK_DB = -20.0
FADE_IN_S, FADE_OUT_S = 0.5, 1.2
DEFAULT_CTA = {"label": "LINEで話してみる", "sub": "プロフィールのリンクから", "show": True}

BREAK_AFTER = set("、。，,！？!?…‥」』）)】〕・ 　")
NO_LINE_START = set("、。，,！？!?」』）)】〕ーぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------

def _units(text: str) -> list[str]:
    """Unbreakable units: a [[...]] highlight, a run of ASCII letters/digits, or one character."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("[[", i):
            j = text.find("]]", i + 2)
            if j != -1:
                out.append(text[i:j + 2])
                i = j + 2
                continue
        m = re.match(r"[…‥]+", text[i:])
        if m:
            out.append(m.group(0))
            i += len(m.group(0))
            continue
        m = re.match(r"[A-Za-z0-9.,%:+\-/]+", text[i:])
        if m and m.group(0)[0].isalnum():
            out.append(m.group(0))
            i += len(m.group(0))
            continue
        out.append(text[i])
        i += 1
    return out


def visible_width(text: str) -> float:
    """Approximate width in full-width characters (ASCII counts ~0.55). [[ ]] markers are not counted."""
    t = text.replace("[[", "").replace("]]", "")
    return sum(0.55 if ord(c) < 0x2E80 and c not in "…‥" else 1.0 for c in t)


def _is_hira(ch: str) -> bool:
    return "\u3041" <= ch <= "\u309f"


def _soft_break(left: str, right: str) -> bool:
    """Likely phrase boundary: hiragana followed by kanji, katakana, a letter/digit or an opening bracket."""
    return _is_hira(left[-1]) and not _is_hira(right[0]) and right[0] not in NO_LINE_START


def _split_run(units: list[str], width: float) -> list[list[str]]:
    """Split a run without punctuation into balanced pieces, preferring hiragana->kanji/katakana boundaries."""
    widths = [visible_width(u) for u in units]
    total = sum(widths)
    if total <= width + 1 or len(units) < 2:  # slightly long lines are shrunk by font_px()
        return [units]
    best, best_cost, left = None, None, 0.0
    for k in range(1, len(units)):
        left += widths[k - 1]
        if units[k][0] in NO_LINE_START:
            continue
        cost = abs(total - 2 * left) + (0 if _soft_break(units[k - 1], units[k]) else 4)
        if best_cost is None or cost < best_cost:
            best, best_cost = k, cost
    if best is None:
        return [units]
    return _split_run(units[:best], width) + _split_run(units[best:], width)


def split_caption(text: str, width: int = CAPTION_WIDTH) -> list[str]:
    """Split one narration line into caption lines of about `width` characters.

    The text is cut into phrases after punctuation; phrases are packed into lines of <= width; a phrase
    longer than a line is split at the most balanced phrase-like boundary. Up to ~2 lines, the most balanced
    punctuation break is used. Never breaks inside [[ ]] or a run of letters/digits.
    """
    text = text.strip()
    if visible_width(text) <= width:
        return [text] if text else []
    units = _units(text)
    widths = [visible_width(u) for u in units]
    total = sum(widths)
    breaks = [k for k in range(1, len(units))
              if units[k - 1][-1] in BREAK_AFTER and units[k][0] not in NO_LINE_START]
    if total <= 2 * width + 1:
        ok = [k for k in breaks if max(sum(widths[:k]), total - sum(widths[:k])) <= width + 1]
        if ok:
            k = min(ok, key=lambda k: abs(total - 2 * sum(widths[:k])))
            return ["".join(units[:k]).strip(), "".join(units[k:]).strip()]
    phrases, prev = [], 0
    for k in breaks + [len(units)]:
        phrases += _split_run(units[prev:k], width)
        prev = k
    lines: list[list[str]] = []
    for ph in phrases:
        if lines and visible_width("".join(lines[-1] + ph)) <= width + 0.5:
            lines[-1] = lines[-1] + ph
        else:
            lines.append(list(ph))
    return ["".join(ln).strip() for ln in lines if "".join(ln).strip()]


def caption_cards(cut: dict) -> list[list[str]]:
    """Caption cards (each 1-2 lines) for one cut."""
    cap = cut.get("caption")
    if isinstance(cap, str):
        cap = [cap]
    lines = [str(x) for x in cap if str(x).strip()] if cap else split_caption(str(cut.get("line", "")))
    return [lines[i:i + 2] for i in range(0, len(lines), 2)]


def markup(line: str) -> str:
    """Escape, then [[word]] -> highlight span, runs of ellipsis -> lifted span."""
    s = html.escape(line, quote=False)
    s = re.sub(r"\[\[(.+?)\]\]", r'<span class="em">\1</span>', s)
    return re.sub(r"([…‥]+)", r'<span class="ell">\1</span>', s)


def font_px(lines: list[str]) -> int:
    w = max((visible_width(x) for x in lines), default=1)
    return min(CAPTION_FONT_PX, int(CAPTION_BOX_PX / max(w, 1) / 1.02))


CAPTION_HOLD_S = 0.2


def caption_timeline(cuts: list[dict], lines: list[dict], total: float) -> list[tuple[float, float, list[str], int]]:
    """(start, end, card lines, cut index) for every caption card.

    A line's cards share its spoken span in proportion to their length. The last card of a line ends at the
    line's real end + 0.2s (never past the next caption's start), so captions do not linger over pauses.
    Without line timing the cut's own span is used.
    """
    spans = []  # (start, end_of_line, cards, cut index)
    for i, c in enumerate(cuts):
        cc = caption_cards(c)
        if not cc:
            continue
        ln = line_for(c, i, lines, len(cuts))
        s0 = max(0.0, float(ln["start"]) - 0.1) if ln else float(c["start"])
        e0 = float(ln["end"]) + CAPTION_HOLD_S if ln else float(c["start"]) + float(c["dur"])
        spans.append((s0, min(e0, total), cc, i))
    out = []
    for n, (s0, e0, cc, i) in enumerate(spans):
        weights = [sum(visible_width(x) for x in card) or 1 for card in cc]
        next_start = spans[n + 1][0] if n + 1 < len(spans) else total
        end_line = min(e0, next_start)
        t = s0
        for j, (card, w) in enumerate(zip(cc, weights)):
            e = t + (end_line - s0) * w / sum(weights) if j < len(cc) - 1 else end_line
            out.append((round(t, 2), round(e, 2), card, i))
            t = e
    return out


# ---------------------------------------------------------------------------
# BGM ducking
# ---------------------------------------------------------------------------

def duck_windows(lines: list[dict], pad: float = DUCK_PAD_S, gap: float = DUCK_GAP_S) -> list[list[float]]:
    """Narration spans (padded); spans closer than `gap` seconds are merged so the music does not pump."""
    wins: list[list[float]] = []
    for ln in sorted(lines, key=lambda x: float(x["start"])):
        s, e = max(0.0, float(ln["start"]) - pad), float(ln["end"]) + pad
        if wins and s - wins[-1][1] < gap:
            wins[-1][1] = max(wins[-1][1], e)
        else:
            wins.append([s, e])
    return wins


def duck_expr(wins: list[list[float]], total: float, base_db: float = 0.0, duck_db: float = DUCK_DB) -> str:
    """Piecewise-linear dB envelope for ffmpeg's volume filter (down in 0.15s, back up in 0.3s)."""
    low = base_db + duck_db
    pts = [(0.0, base_db)]
    for s, e in wins:
        pts += [(max(s - 0.15, 0.0), base_db), (s, low), (e, low), (e + 0.3, base_db)]
    pts.append((total + 5, base_db))
    expr = f"{base_db}"
    for (t0, d0), (t1, d1) in reversed(list(zip(pts, pts[1:]))):
        if t1 <= t0:
            continue
        seg = f"({d0}+(t-{t0:.3f})/{t1 - t0:.3f}*({d1}-({d0})))"
        expr = f"if(between(t,{t0:.3f},{t1:.3f}),{seg},{expr})"
    return expr


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

def load_lines(project: Path) -> list[dict]:
    p = project / "voice" / "lines.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("lines", [])
    return [d for d in data if "start" in d and "end" in d]


def line_for(cut: dict, idx: int, lines: list[dict], n_cuts: int) -> dict | None:
    for ln in lines:
        if ln.get("cut") == cut["id"] or ln.get("id") == cut["id"]:
            return ln
    return lines[idx] if len(lines) == n_cuts else None


def ffmpeg(*args: str) -> None:
    common.run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args)


def ensure_fonts(dest: Path) -> bool:
    """Copy Zen Maru Gothic (OFL) into dest, downloading once into a local cache. False if unavailable."""
    dest.mkdir(parents=True, exist_ok=True)
    caches = [PLUGIN_ROOT / "assets" / "fonts", Path.home() / ".cache" / "threads-video-studio" / "fonts"]
    for name in FONT_FILES:
        if (dest / name).exists():
            continue
        src = next((c / name for c in caches if (c / name).exists()), None)
        if src is None:
            # never write into the plugin folder (it is replaced on update); download to the user cache only
            for c in caches[1:]:
                try:
                    c.mkdir(parents=True, exist_ok=True)
                    tmp = c / (name + ".part")
                    with urllib.request.urlopen(FONT_BASE + name, timeout=60) as r:
                        tmp.write_bytes(r.read())
                    tmp.replace(c / name)
                    src = c / name
                    break
                except OSError:
                    continue
        if src is None:
            return False
        shutil.copy2(src, dest / name)
    return True


def prep_clip(project: Path, cut: dict, dur: float, fps: int, out: Path, cache: dict) -> tuple[float, str]:
    """Make assets/<cut>.mp4 exactly long enough. Returns (frozen seconds, source description)."""
    cid = check_slug(cut["id"], "cut id")
    src = safe_path(project, f"clips/{cid}.mp4")
    vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps={fps},format=yuv420p"
    if src.exists():
        clip_dur = common.duration(src)
        frozen = max(0.0, dur - clip_dur)
        key = f"{src}:{src.stat().st_mtime_ns}:{src.stat().st_size}:{dur:.3f}:{fps}"
        if cache.get(cut["id"]) == key and out.exists():
            return frozen, "clip"
        pad = f",tpad=stop_mode=clone:stop_duration={frozen + 0.2:.3f}" if frozen > 0 else ""
        ffmpeg("-i", str(src), "-vf", vf + pad, "-t", f"{dur + 0.1:.3f}", "-an", "-c:v", "libx264",
               "-crf", "16", "-preset", "medium", str(out))
        cache[cut["id"]] = key
        return frozen, "clip"
    frame = safe_path(project, cut.get("frame") or f"frames/{cid}.png")
    if not frame.exists():
        raise RuntimeError(f"{cut['id']}: neither clips/{cut['id']}.mp4 nor {frame.relative_to(project)} exists")
    ffmpeg("-loop", "1", "-i", str(frame), "-vf", vf, "-t", f"{dur + 0.1:.3f}", "-an", "-c:v", "libx264",
           "-crf", "16", "-preset", "medium", str(out))
    cache.pop(cut["id"], None)
    return dur, "still frame (no clip)"


def prep_bgm(src: Path, lines: list[dict], total: float, out: Path, base_db: float) -> list[list[float]]:
    wins = duck_windows(lines)
    expr = duck_expr(wins, total, base_db)
    ffmpeg("-stream_loop", "-1", "-i", str(src), "-t", f"{total:.3f}", "-af",
           f"aresample=48000,volume='pow(10,({expr})/20)':eval=frame,"
           f"afade=t=in:st=0:d={FADE_IN_S},afade=t=out:st={max(0.0, total - FADE_OUT_S):.3f}:d={FADE_OUT_S}",
           "-ac", "2", str(out))
    return wins


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def build(project: Path, bgm_db: float = 0.0) -> dict:
    shots = json.loads((project / "shots.json").read_text(encoding="utf-8"))
    slug = check_slug(shots.get("slug") or project.name)
    fps = int(shots.get("fps") or 24)
    cuts = shots.get("cuts") or []
    if not cuts:
        raise RuntimeError("shots.json has no cuts")
    for c in cuts:
        check_slug(str(c.get("id", "")), "cut id")
        if c.get("start") is None or not c.get("dur"):
            raise RuntimeError(f"{c.get('id')}: start/dur missing - run gen_voice.py first (it fills the timing)")
    narration = project / "voice" / "all.mp3"
    if not narration.exists():
        raise RuntimeError("voice/all.mp3 not found - run gen_voice.py first")
    lines = load_lines(project)
    narr_dur = common.duration(narration)
    cut_end = max(float(c["start"]) + float(c["dur"]) for c in cuts)
    total = round(max(cut_end, narr_dur + 0.4), 2)

    video = project / "video"
    assets = video / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache_path = video / ".prep.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    warnings: list[str] = []

    # video
    vids = []
    for i, c in enumerate(cuts):
        start = float(c["start"])
        dur = (total - start) if i == len(cuts) - 1 else float(c["dur"])
        frozen, how = prep_clip(project, c, dur, fps, assets / f"{c['id']}.mp4", cache)
        if frozen > FREEZE_WARN_S:
            warnings.append(f"WARN {c['id']}: {frozen:.2f}s of frozen picture ({how}); a frozen picture looks dead "
                            f"- regenerate a longer clip or shorten the cut")
        elif frozen > 0:
            print(f"note {c['id']}: padded {frozen:.2f}s with the last frame")
        vids.append(f'      <video id="v_{c["id"]}" class="full clip" src="assets/{c["id"]}.mp4" muted playsinline '
                    f'data-start="{start:.2f}" data-duration="{dur:.2f}" data-media-start="0" data-track-index="0"></video>')
    common_tmp = cache_path.with_suffix(".tmp")
    common_tmp.write_text(json.dumps(cache, indent=1))
    common_tmp.replace(cache_path)

    # captions
    cta = {**DEFAULT_CTA, **(shots.get("cta") or {})}
    show_cta = bool(cta.get("show", True)) and str(cta.get("label", "")).strip() != ""
    cards = caption_timeline(cuts, lines, total)
    subs, sub_js = [], []
    for k, (s, e, card, ci) in enumerate(cards):
        d = round(max(0.3, e - s), 2)
        over = " over-cta" if show_cta and ci == len(cuts) - 1 else ""
        px = font_px(card)
        style = f' style="font-size:{px}px"' if px != CAPTION_FONT_PX else ""
        inner = "".join(f'<span class="l"{style}>{markup(x)}</span>' for x in card)
        subs.append(f'      <div id="sub{k + 1}" class="sub clip{over}" data-start="{s:.2f}" data-duration="{d:.2f}" '
                    f'data-track-index="2"><div class="in">{inner}</div></div>')
        sub_js.append(f'["#sub{k + 1}", {s:.2f}, {d:.2f}]')

    # CTA
    last_start = float(cuts[-1]["start"])
    if show_cta:
        label = str(cta["label"]).strip()
        cta_size = min(56, int(560 / max(visible_width(label), 1)))
        hint = f'<div id="hint" class="hint">{html.escape(str(cta.get("sub") or ""))}</div>' if cta.get("sub") else ""
        cta_html = (f'      <!-- call to action on the last cut (visual only; the link goes in the post text) -->\n'
                    f'      <div id="cta" class="layer clip" data-start="{last_start:.2f}" '
                    f'data-duration="{total - last_start:.2f}" data-track-index="3">\n'
                    f'        <div class="cta">\n'
                    f'          <div id="btn" class="btn"><div class="bubble"><div class="dots"><i></i><i></i><i></i>'
                    f'</div></div><span class="txt">{html.escape(label)}</span></div>\n'
                    f'          {hint}\n        </div>\n      </div>')
        cta_js = (f'      tl.fromTo("#btn", {{ scale: 0.82, opacity: 0 }}, {{ scale: 1, opacity: 1, duration: 0.5, '
                  f'ease: "back.out(1.8)" }}, {last_start + 0.5:.2f});\n'
                  f'      tl.to("#btn", {{ scale: 1.04, duration: 0.4, repeat: 5, yoyo: true, ease: "sine.inOut" }}, '
                  f'{last_start + 1.2:.2f});\n')
        if hint:
            cta_js += (f'      tl.fromTo("#hint", {{ y: 14, opacity: 0 }}, {{ y: 0, opacity: 1, duration: 0.4, '
                       f'ease: "power2.out" }}, {last_start + 0.9:.2f});\n')
    else:
        cta_html, cta_js, cta_size = "      <!-- CTA hidden (cta.show=false) -->", "", 56

    # audio
    shutil.copy2(narration, assets / "narration.mp3")
    audio = [f'      <audio id="narration" src="assets/narration.mp3" data-start="0" data-duration="{narr_dur:.2f}" '
             f'data-track-index="10" data-volume="1"></audio>']
    bgm_src = project / "bgm" / "bgm.mp3"
    wins = []
    if bgm_src.exists():
        wins = prep_bgm(bgm_src, lines, total, assets / "bgm.wav", bgm_db)
        if not lines:
            warnings.append("WARN bgm: voice/lines.json missing - BGM is not ducked under the narration")
        audio.append(f'      <audio id="bgm" src="assets/bgm.wav" data-start="0" data-duration="{total:.2f}" '
                     f'data-track-index="11" data-volume="1"></audio>')
    else:
        (assets / "bgm.wav").unlink(missing_ok=True)
        audio.append("      <!-- no BGM (bgm/bgm.mp3 not found) -->")

    # fonts
    fonts_ok = ensure_fonts(assets / "fonts")
    if fonts_ok:
        font_face = "\n".join(
            f'      @font-face {{ font-family: "Zen Maru Gothic"; src: url("assets/fonts/{f}") format("truetype"); '
            f'font-weight: {w}; }}' for f, w in (("ZenMaruGothic-Bold.ttf", 700), ("ZenMaruGothic-Black.ttf", 900)))
        font_link = ""
    else:
        warnings.append("WARN fonts: could not download Zen Maru Gothic; using Google Fonts online (needs network "
                        "at render time)")
        font_face = ""
        font_link = ('    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:'
                     'wght@700;900&display=swap" />')

    page = TEMPLATE.read_text(encoding="utf-8")
    for k, v in {"TITLE": html.escape(slug), "TOTAL": f"{total:.2f}", "FONT_FACE": font_face, "FONT_LINK": font_link,
                 "VIDEOS": "\n".join(vids), "SUBS": "\n".join(subs), "SUB_JS": ", ".join(sub_js), "CTA": cta_html,
                 "CTA_JS": cta_js.rstrip("\n"), "CTA_SIZE": str(cta_size), "AUDIO": "\n".join(audio)}.items():
        page = page.replace("{{" + k + "}}", v)
    (video / "index.html").write_text(page, encoding="utf-8")
    (video / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        "media": {"autoProxy": True}, "authoringSkill": "general-video"}, indent=2) + "\n")
    (video / "package.json").write_text(json.dumps({
        "name": re.sub(r"[^a-z0-9-]", "-", slug.lower()) or "video", "private": True, "type": "module",
        "scripts": {k: f"npx --yes {HYPERFRAMES} {k if k != 'dev' else 'preview'}"
                    for k in ("dev", "check", "render")}}, indent=2) + "\n")
    meta = video / "meta.json"
    if not meta.exists():
        meta.write_text(json.dumps({"id": slug, "name": slug}, indent=2) + "\n")

    report = {"slug": slug, "total": total, "narration": round(narr_dur, 2), "captions": len(cards),
              "cta": show_cta, "bgm": bgm_src.exists(), "duck_windows": [[round(a, 2), round(b, 2)] for a, b in wins],
              "fonts_local": fonts_ok, "warnings": warnings}
    (video / "build.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report


def render(project: Path, slug: str) -> tuple[Path, Path]:
    final_dir = project / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    final = final_dir / f"{slug}-final.mp4"
    post = final_dir / f"{slug}-post.mp4"
    try:
        r = subprocess.run(["npx", "--yes", HYPERFRAMES, "render", "-o", str(final)], cwd=str(project / "video"))
    except FileNotFoundError:
        raise RuntimeError("npx not found - install Node.js 18+") from None
    if r.returncode != 0 or not final.exists():
        raise RuntimeError("hyperframes render failed (see output above)")
    tmp = post.with_name(post.stem + ".part.mp4")
    ffmpeg("-i", str(final), "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-maxrate", "1.6M",
           "-bufsize", "3.2M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           str(tmp))
    tmp.replace(post)
    return final, post


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the HyperFrames project from shots.json + voice + BGM; "
                                             "optionally render final and upload-ready MP4s.")
    ap.add_argument("--project", required=True, help="project folder (absolute, or a slug under the workspace)")
    ap.add_argument("--render", action="store_true", help=f"render with npx {HYPERFRAMES} and compress for posting")
    ap.add_argument("--bgm-db", type=float, default=0.0, help="BGM gain in dB between narration lines (default 0)")
    a = ap.parse_args(argv)
    project = Path(a.project).expanduser()
    if not project.is_dir() and not project.is_absolute():
        project = common.workspace() / a.project
    if not (project / "shots.json").exists():
        print(f"error: {project}/shots.json not found", file=sys.stderr)
        return 1
    try:
        rep = build(project.resolve(), a.bgm_db)
        for w in rep["warnings"]:
            print(w)
        print(f"built {project / 'video' / 'index.html'}: {rep['total']}s, {rep['captions']} captions, "
              f"cta={'on' if rep['cta'] else 'off'}, bgm={'on' if rep['bgm'] else 'off'}")
        if a.render:
            final, post = render(project.resolve(), rep["slug"])
            print(f"rendered {final}\nupload copy {post} ({os.path.getsize(post) / 1e6:.1f} MB)")
        return 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
