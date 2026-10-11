#!/usr/bin/env python3
"""1本の動画の HyperFrames プロジェクトを組み立てる。指定があれば書き出しまで行う。

入力（プロジェクトフォルダ）:
  shots.json         カット（start/dur は gen_voice.py が入れる）、line / caption、cta
  clips/<cut>.mp4    生成したクリップ（クリップが無ければ frames/<cut>.png を静止画として使う）
  voice/all.mp3      通しのナレーション1本（どのカットにも line が無い、字幕だけの動画なら不要）
  voice/lines.json   行ごとのタイミング [{"cut": "c01", "start": 0.2, "end": 4.8, ...}, ...]
  bgm/bgm.mp3        BGM（任意）

出力:
  video/index.html, video/assets/*  （HyperFrames プロジェクト。字幕・CTA ボタン・音声）
  video/build.json                  （タイミングと警告）
  --render 指定時: final/<slug>-final.mp4（1080x1920）と final/<slug>-post.mp4（投稿用に圧縮）

ルール:
  - カットより短いクリップは最後のコマで埋める。0.4秒を超えて埋めると警告を出す。
    止まった絵は死んで見えるので、長めのクリップを作り直すか、カットを短くする。
  - 字幕: `caption` があればその行を使う。無ければ `line` を自動で1〜2行（1行約15字）に分ける。
    [[語]] = 強調する語（黄色）。三点リーダー（……）は行の中ほどの高さに上げる。
  - cta.show が false でなければ、最後のカットに CTA ボタン（cta.label、緑）を出す。社名やロゴは出さない。
  - BGM はナレーションの間 20 dB 下げる（0.7秒より近い行はまとめる）。0.5秒でフェードイン、1.2秒でフェードアウト。
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
CAPTION_WIDTH = 15          # 字幕1行の目安の文字数
CAPTION_BOX_PX = 840        # 字幕に使える幅（1080 - 左72 - 右160、縁取りの分を引く）
CAPTION_FONT_PX = 60
DUCK_GAP_S = 0.7
DUCK_PAD_S = 0.1
DUCK_DB = -20.0
FADE_IN_S, FADE_OUT_S = 0.5, 1.2
DEFAULT_CTA = {"label": "LINEで話してみる", "sub": "プロフィールのリンクから", "show": True}

BREAK_AFTER = set("、。，,！？!?…‥」』）)】〕・ 　")
NO_LINE_START = set("、。，,！？!?」』）)】〕ーぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")


# ---------------------------------------------------------------------------
# 字幕
# ---------------------------------------------------------------------------

def _units(text: str) -> list[str]:
    """分けられない単位: [[...]] の強調、英数字の連なり、または1文字。"""
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
    """全角文字に換算したおおよその幅（ASCII は約 0.55）。[[ ]] の記号は数えない。"""
    t = text.replace("[[", "").replace("]]", "")
    return sum(0.55 if ord(c) < 0x2E80 and c not in "…‥" else 1.0 for c in t)


def _is_hira(ch: str) -> bool:
    return "\u3041" <= ch <= "\u309f"


def _soft_break(left: str, right: str) -> bool:
    """文節の切れ目らしい所: ひらがなの後に漢字・カタカナ・英数字・開きかっこが続く所。"""
    return _is_hira(left[-1]) and not _is_hira(right[0]) and right[0] not in NO_LINE_START


def _split_run(units: list[str], width: float) -> list[list[str]]:
    """句読点の無い連なりを均等に分ける。ひらがな→漢字/カタカナの境目を優先する。"""
    widths = [visible_width(u) for u in units]
    total = sum(widths)
    if total <= width + 1 or len(units) < 2:  # 少し長い行は font_px() で文字を小さくする
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
    """ナレーション1行を、1行あたり約 `width` 文字の字幕行に分ける。

    句読点の後で句に分け、width 以下の行に詰める。1行より長い句は、いちばん均等になる文節の切れ目で
    分ける。2行程度に収まるなら、いちばん均等になる句読点で分ける。[[ ]] の中や英数字の連なりでは分けない。
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
    """1カット分の字幕カード（1枚1〜2行）。"""
    cap = cut.get("caption")
    if isinstance(cap, str):
        cap = [cap]
    lines = [str(x) for x in cap if str(x).strip()] if cap else split_caption(str(cut.get("line", "")))
    return [lines[i:i + 2] for i in range(0, len(lines), 2)]


def markup(line: str) -> str:
    """エスケープしてから、[[語]] を強調の span に、三点リーダーの連なりを持ち上げ用の span にする。"""
    s = html.escape(line, quote=False)
    s = re.sub(r"\[\[(.+?)\]\]", r'<span class="em">\1</span>', s)
    return re.sub(r"([…‥]+)", r'<span class="ell">\1</span>', s)


def font_px(lines: list[str]) -> int:
    w = max((visible_width(x) for x in lines), default=1)
    return min(CAPTION_FONT_PX, int(CAPTION_BOX_PX / max(w, 1) / 1.02))


CAPTION_HOLD_S = 0.2


def caption_timeline(cuts: list[dict], lines: list[dict], total: float) -> list[tuple[float, float, list[str], int]]:
    """字幕カードごとに (開始, 終了, カードの行, カット番号) を返す。

    1行のカードは、その行の読み上げ時間を文字の長さに応じて分け合う。行の最後のカードは実際の行末 + 0.2秒で
    終わる（次の字幕の開始は越えない）ので、間の部分で字幕が残り続けない。
    行のタイミングが無いときはカット自体の時間を使う。
    """
    spans = []  # (開始, 行末, カード, カット番号)
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
# BGM を下げる処理
# ---------------------------------------------------------------------------

def duck_windows(lines: list[dict], pad: float = DUCK_PAD_S, gap: float = DUCK_GAP_S) -> list[list[float]]:
    """ナレーションの区間（前後に余白付き）。`gap` 秒より近い区間はまとめ、音楽が上下に揺れないようにする。"""
    wins: list[list[float]] = []
    for ln in sorted(lines, key=lambda x: float(x["start"])):
        s, e = max(0.0, float(ln["start"]) - pad), float(ln["end"]) + pad
        if wins and s - wins[-1][1] < gap:
            wins[-1][1] = max(wins[-1][1], e)
        else:
            wins.append([s, e])
    return wins


def duck_expr(wins: list[list[float]], total: float, base_db: float = 0.0, duck_db: float = DUCK_DB) -> str:
    """ffmpeg の volume フィルター用の折れ線の dB 包絡線（0.15秒で下げ、0.3秒で戻す）。"""
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
# 入力
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
    """Zen Maru Gothic（OFL）を dest にコピーする。初回だけローカルのキャッシュにダウンロードする。手に入らなければ False。"""
    dest.mkdir(parents=True, exist_ok=True)
    caches = [PLUGIN_ROOT / "assets" / "fonts", Path.home() / ".cache" / "threads-video-studio" / "fonts"]
    for name in FONT_FILES:
        if (dest / name).exists():
            continue
        src = next((c / name for c in caches if (c / name).exists()), None)
        if src is None:
            # プラグインのフォルダには書かない（更新で置き換わる）。ダウンロードはユーザーのキャッシュにだけ
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
    """ちょうど必要な長さの assets/<cut>.mp4 を作る。(止めた秒数, 素材の説明) を返す。"""
    cid = check_slug(cut["id"], "カット ID")
    src = safe_path(project, f"clips/{cid}.mp4")
    vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps={fps},format=yuv420p"
    if src.exists():
        clip_dur = common.duration(src)
        frozen = max(0.0, dur - clip_dur)
        key = f"{src}:{src.stat().st_mtime_ns}:{src.stat().st_size}:{dur:.3f}:{fps}"
        if cache.get(cut["id"]) == key and out.exists():
            return frozen, "クリップ"
        pad = f",tpad=stop_mode=clone:stop_duration={frozen + 0.2:.3f}" if frozen > 0 else ""
        ffmpeg("-i", str(src), "-vf", vf + pad, "-t", f"{dur + 0.1:.3f}", "-an", "-c:v", "libx264",
               "-crf", "16", "-preset", "medium", str(out))
        cache[cut["id"]] = key
        return frozen, "クリップ"
    frame = safe_path(project, cut.get("frame") or f"frames/{cid}.png")
    if not frame.exists():
        raise RuntimeError(f"{cut['id']}: clips/{cut['id']}.mp4 も {frame.relative_to(project)} も無い")
    ffmpeg("-loop", "1", "-i", str(frame), "-vf", vf, "-t", f"{dur + 0.1:.3f}", "-an", "-c:v", "libx264",
           "-crf", "16", "-preset", "medium", str(out))
    cache.pop(cut["id"], None)
    return dur, "静止画（クリップなし）"


def prep_bgm(src: Path, lines: list[dict], total: float, out: Path, base_db: float) -> list[list[float]]:
    wins = duck_windows(lines)
    expr = duck_expr(wins, total, base_db)
    ffmpeg("-stream_loop", "-1", "-i", str(src), "-t", f"{total:.3f}", "-af",
           f"aresample=48000,volume='pow(10,({expr})/20)':eval=frame,"
           f"afade=t=in:st=0:d={FADE_IN_S},afade=t=out:st={max(0.0, total - FADE_OUT_S):.3f}:d={FADE_OUT_S}",
           "-ac", "2", str(out))
    return wins


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------

def build(project: Path, bgm_db: float = 0.0) -> dict:
    shots = json.loads((project / "shots.json").read_text(encoding="utf-8"))
    slug = check_slug(shots.get("slug") or project.name)
    fps = int(shots.get("fps") or 24)
    cuts = shots.get("cuts") or []
    if not cuts:
        raise RuntimeError("shots.json にカットが無い")
    for c in cuts:
        check_slug(str(c.get("id", "")), "カット ID")
        if c.get("start") is None or not c.get("dur"):
            raise RuntimeError(f"{c.get('id')}: start/dur が無い。先に gen_voice.py を実行する（タイミングを入れる）")
    narration = project / "voice" / "all.mp3"
    has_lines = any(str(c.get("line") or "").strip() for c in cuts)
    if not narration.exists() and has_lines:
        raise RuntimeError("voice/all.mp3 が無い。先に gen_voice.py を実行する")
    lines = load_lines(project)
    narr_dur = common.duration(narration) if narration.exists() else 0.0  # 字幕だけの動画はナレなし
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

    # 映像
    vids = []
    for i, c in enumerate(cuts):
        start = float(c["start"])
        dur = (total - start) if i == len(cuts) - 1 else float(c["dur"])
        frozen, how = prep_clip(project, c, dur, fps, assets / f"{c['id']}.mp4", cache)
        if frozen > FREEZE_WARN_S:
            warnings.append(f"警告 {c['id']}: {frozen:.2f}秒 絵が止まる（{how}）。止まった絵は死んで見える。"
                            f"長めのクリップを作り直すか、カットを短くする")
        elif frozen > 0:
            print(f"補足 {c['id']}: 最後のコマで {frozen:.2f}秒 埋めた")
        vids.append(f'      <video id="v_{c["id"]}" class="full clip" src="assets/{c["id"]}.mp4" muted playsinline '
                    f'data-start="{start:.2f}" data-duration="{dur:.2f}" data-media-start="0" data-track-index="0"></video>')
    common_tmp = cache_path.with_suffix(".tmp")
    common_tmp.write_text(json.dumps(cache, indent=1))
    common_tmp.replace(cache_path)

    # 字幕
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
        cta_html = (f'      <!-- 最後のカットの CTA（見た目だけ。リンクは投稿文に入れる） -->\n'
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
        cta_html, cta_js, cta_size = "      <!-- CTA は非表示（cta.show=false） -->", "", 56

    # 音声
    if narration.exists():
        shutil.copy2(narration, assets / "narration.mp3")
        audio = [f'      <audio id="narration" src="assets/narration.mp3" data-start="0" data-duration="{narr_dur:.2f}" '
                 f'data-track-index="10" data-volume="1"></audio>']
    else:
        (assets / "narration.mp3").unlink(missing_ok=True)
        audio = ["      <!-- ナレーションなし（字幕だけ） -->"]
    bgm_src = project / "bgm" / "bgm.mp3"
    wins = []
    if bgm_src.exists():
        wins = prep_bgm(bgm_src, lines, total, assets / "bgm.wav", bgm_db)
        if not lines and narration.exists():
            warnings.append("警告 BGM: voice/lines.json が無いので、ナレーションの間も BGM を下げていない")
        audio.append(f'      <audio id="bgm" src="assets/bgm.wav" data-start="0" data-duration="{total:.2f}" '
                     f'data-track-index="11" data-volume="1"></audio>')
    else:
        (assets / "bgm.wav").unlink(missing_ok=True)
        audio.append("      <!-- BGM なし（bgm/bgm.mp3 が無い） -->")

    # フォント
    fonts_ok = ensure_fonts(assets / "fonts")
    if fonts_ok:
        font_face = "\n".join(
            f'      @font-face {{ font-family: "Zen Maru Gothic"; src: url("assets/fonts/{f}") format("truetype"); '
            f'font-weight: {w}; }}' for f, w in (("ZenMaruGothic-Bold.ttf", 700), ("ZenMaruGothic-Black.ttf", 900)))
        font_link = ""
    else:
        warnings.append("警告 フォント: Zen Maru Gothic をダウンロードできなかった。オンラインの Google Fonts を使う"
                        "（書き出し時に通信が必要）")
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
        raise RuntimeError("npx が見つからない。Node.js 18 以上を入れる") from None
    if r.returncode != 0 or not final.exists():
        raise RuntimeError("hyperframes の書き出しに失敗した（上の出力を見る）")
    tmp = post.with_name(post.stem + ".part.mp4")
    ffmpeg("-i", str(final), "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-maxrate", "1.6M",
           "-bufsize", "3.2M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           str(tmp))
    tmp.replace(post)
    return final, post


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="shots.json・ナレーション・BGM から HyperFrames プロジェクトを組み立てる。"
                                             "指定があれば完成版と投稿用の MP4 も書き出す。")
    ap.add_argument("--project", required=True, help="プロジェクトのフォルダ（絶対パス、またはワークスペース内のスラッグ）")
    ap.add_argument("--render", action="store_true", help=f"npx {HYPERFRAMES} で書き出し、投稿用に圧縮する")
    ap.add_argument("--bgm-db", type=float, default=0.0, help="ナレーションの合間の BGM の音量（dB、既定 0）")
    a = ap.parse_args(argv)
    project = Path(a.project).expanduser()
    if not project.is_dir() and not project.is_absolute():
        project = common.workspace() / a.project
    if not (project / "shots.json").exists():
        print(f"エラー: {project}/shots.json が無い", file=sys.stderr)
        return 1
    try:
        rep = build(project.resolve(), a.bgm_db)
        for w in rep["warnings"]:
            print(w)
        print(f"組み立てた {project / 'video' / 'index.html'}: {rep['total']}秒、字幕 {rep['captions']} 枚、"
              f"CTA {'あり' if rep['cta'] else 'なし'}、BGM {'あり' if rep['bgm'] else 'なし'}")
        if a.render:
            final, post = render(project.resolve(), rep["slug"])
            print(f"書き出した {final}\n投稿用 {post}（{os.path.getsize(post) / 1e6:.1f} MB）")
        return 0
    except RuntimeError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
