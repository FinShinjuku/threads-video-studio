#!/usr/bin/env python3
"""Pre-generation reading check for the narration script (no API calls).

  python3 yomi_check.py --project t004-camp            # report
  python3 yomi_check.py --project t004-camp --suggest  # also append suggested readings to voice/yomi.json

Input: voice/script.txt (one narration line per cut; blank lines ignored). If it is missing or empty,
the `line` fields of shots.json are used.
voice/yomi.json is a flat map {"written form": "reading"}. It changes only what is SPOKEN; captions keep
the original text.

With `pyopenjtalk` installed (optional), each line's reading is shown in katakana. Without it, the script
lists what a machine can find: Latin letters, numbers, symbols and kanji that are often misread.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import common

# Kanji / words with several common readings. Value = typical readings (for the human to decide).
RISKY = {
    "上手": "じょうず / うわて / かみて",
    "一日": "いちにち / ついたち",
    "今日": "きょう / こんにち",
    "明日": "あした / あす / みょうにち",
    "人気": "にんき / ひとけ",
    "大事": "だいじ / おおごと",
    "生物": "せいぶつ / なまもの",
    "最中": "さいちゅう / もなか",
    "市場": "しじょう / いちば",
    "風車": "ふうしゃ / かざぐるま",
    "下手": "へた / しもて / したて",
    "色紙": "しきし / いろがみ",
    "創": "そう / つく(る)",
    "生": "せい / しょう / い(きる) / なま",
    "間": "かん / ま / あいだ",
    "方": "ほう / かた",
    "日": "にち / ひ / か / じつ",
    "行": "い(く) / おこな(う) / こう / ぎょう",
    "後": "あと / ご / うし(ろ) / のち",
    "分": "ふん / ぶん / わ(かる)",
    "何": "なに / なん",
    "辛": "から(い) / つら(い)",
    "空": "そら / から / くう",
    "床": "ゆか / とこ",
    "家": "いえ / うち / か / け",
    "米": "こめ / べい",
}

LETTERS = {
    "A": "エー", "B": "ビー", "C": "シー", "D": "ディー", "E": "イー", "F": "エフ", "G": "ジー", "H": "エイチ",
    "I": "アイ", "J": "ジェー", "K": "ケー", "L": "エル", "M": "エム", "N": "エヌ", "O": "オー", "P": "ピー",
    "Q": "キュー", "R": "アール", "S": "エス", "T": "ティー", "U": "ユー", "V": "ブイ", "W": "ダブリュー",
    "X": "エックス", "Y": "ワイ", "Z": "ゼット",
}
DIGITS = ["ぜろ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
SYMBOLS = set("%&/+#@~=<>*^$|\\_")

RE_LATIN = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ][A-Za-zＡ-Ｚａ-ｚ0-9.\-']*")
RE_NUM = re.compile(r"[0-9０-９][0-9０-９,，.]*")


def read_script_lines(project: Path) -> list[str]:
    p = common.safe_path(project, "voice/script.txt")
    lines = []
    if p.exists():
        lines = [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
    if not lines:
        shots = common.load_shots(project)
        lines = [(c.get("line") or "").strip() for c in shots.get("cuts", [])]
        if not all(lines):
            raise RuntimeError("voice/script.txt is empty and some cuts in shots.json have no 'line'")
    return lines


def load_yomi(project: Path) -> dict:
    data = common.load_json(common.safe_path(project, "voice/yomi.json"), default={})
    if not isinstance(data, dict):
        raise ValueError("voice/yomi.json must be a flat object {\"written\": \"reading\"}")
    return {str(k): str(v) for k, v in data.items() if str(k) and str(v)}


def apply_yomi(text: str, yomi: dict) -> str:
    """Replace written forms with readings, longest key first, in one pass (no re-replacement)."""
    if not yomi:
        return text
    keys = sorted(yomi, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys))
    return pat.sub(lambda m: yomi[m.group(0)], text)


def _zen2han(s: str) -> str:
    return s.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ，",
                                     "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz,"))


def number_kana(n: int) -> str:
    """Integer (0 <= n < 10^12) to hiragana, e.g. 40 -> よんじゅう, 300 -> さんびゃく."""
    if n == 0:
        return "ぜろ"
    special = {"百": {3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"},
               "千": {3: "さんぜん", 8: "はっせん"}}

    def below_10000(x: int) -> str:
        out = ""
        for unit, val, kana in (("千", 1000, "せん"), ("百", 100, "ひゃく"), ("十", 10, "じゅう")):
            d, x = divmod(x, val)
            if d == 0:
                continue
            if d == 1:
                out += kana
            elif unit in special and d in special[unit]:
                out += special[unit][d]
            else:
                out += DIGITS[d] + kana
        if x:
            out += DIGITS[x]
        return out

    parts = []
    for val, kana in ((10 ** 8, "おく"), (10 ** 4, "まん")):
        d, n = divmod(n, val)
        if d:
            parts.append(below_10000(d) + kana)
    if n:
        parts.append(below_10000(n))
    return "".join(parts)


def suggest_reading(token: str, g2p=None) -> str | None:
    t = _zen2han(token)
    if g2p is not None:
        try:
            r = g2p(t, kana=True)
            if r and r != t:
                return r
        except Exception:
            pass
    if re.fullmatch(r"[0-9][0-9,]*", t):
        return number_kana(int(t.replace(",", "")))
    if re.fullmatch(r"[A-Z]{1,3}", t):  # short acronyms only; LINE, NASA etc. are words
        return "".join(LETTERS[c] for c in t)
    return None


def scan_line(text: str) -> list[tuple[str, str]]:
    """Items a machine can flag without a dictionary: (kind, token)."""
    found: list[tuple[str, str]] = []
    seen = set()

    def add(kind: str, tok: str) -> None:
        if (kind, tok) not in seen:
            seen.add((kind, tok))
            found.append((kind, tok))

    for m in RE_LATIN.finditer(text):
        add("latin", m.group(0).rstrip(".-'"))
    for m in RE_NUM.finditer(text):
        add("number", m.group(0).rstrip(",，."))
    for ch in text:
        if ch in SYMBOLS:
            add("symbol", ch)
    covered = ""
    for w in sorted(RISKY, key=len, reverse=True):
        if w in text and (len(w) > 1 or w not in covered):
            add("kanji", w)
            covered += w
    return found


def get_g2p():
    try:
        import pyopenjtalk  # optional dependency
        return pyopenjtalk.g2p
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Check how the narration script will be read (before paying for TTS).")
    ap.add_argument("--project", help="project folder or slug (default: current folder)")
    ap.add_argument("--suggest", action="store_true",
                    help="append machine suggestions (numbers, acronyms, pyopenjtalk readings) to voice/yomi.json")
    a = ap.parse_args()

    project = common.project_dir(a.project)
    lines = read_script_lines(project)
    yomi = load_yomi(project)
    g2p = get_g2p()
    if g2p is None:
        print("warning: pyopenjtalk not installed - showing machine-detectable risks only "
              "(pip install pyopenjtalk for full readings)")

    suggestions: dict[str, str] = {}
    for i, line in enumerate(lines, 1):
        spoken = apply_yomi(line, yomi)
        print(f"\n[{i:02d}] {line}")
        if spoken != line:
            print(f"     spoken: {spoken}")
        if g2p is not None:
            try:
                print(f"     reading: {g2p(spoken, kana=True)}")
            except Exception as e:
                print(f"     reading: (pyopenjtalk failed: {e})")
        for kind, tok in scan_line(spoken):
            if kind == "kanji":
                print(f"     check {kind}: {tok}  ({RISKY[tok]})")
                continue
            sug = suggest_reading(tok, g2p) if kind != "symbol" else None
            print(f"     check {kind}: {tok}" + (f"  -> suggest {sug}" if sug else ""))
            if sug and tok not in yomi:
                suggestions[tok] = sug

    if a.suggest:
        if suggestions:
            merged = dict(yomi)
            added = {k: v for k, v in suggestions.items() if k not in merged}
            merged.update(added)
            common.save_json(common.safe_path(project, "voice/yomi.json"), merged)
            print(f"\nadded {len(added)} suggestion(s) to voice/yomi.json - review them before generating:")
            for k, v in added.items():
                print(f"  {k} -> {v}")
        else:
            print("\nno machine suggestions to add")
    elif suggestions:
        print(f"\n{len(suggestions)} suggestion(s) available; re-run with --suggest to add them to voice/yomi.json")


if __name__ == "__main__":
    common.cli(main)
