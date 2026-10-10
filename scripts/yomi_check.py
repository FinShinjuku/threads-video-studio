#!/usr/bin/env python3
"""ナレーション原稿を生成前に読みの確認をする（API は呼ばない）。

  python3 yomi_check.py --project t004-camp            # 確認結果を出す
  python3 yomi_check.py --project t004-camp --suggest  # 読みの候補を voice/yomi.json にも追記する

入力: voice/script.txt（1カットにナレーション1行。空行は無視）。無いか空なら shots.json の `line` を使う。
voice/yomi.json は {"表記": "読み"} の平たい対応表。変わるのは読み上げだけで、字幕は元の文のまま。

`pyopenjtalk`（任意）が入っていれば、各行の読みをカタカナで表示する。無ければ、機械で見つけられるもの
（英字・数字・記号・読み間違えやすい漢字）を一覧にする。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import common

# よく使う読みが複数ある漢字・語。値 = 主な読み（どれにするかは人が決める）。
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
            raise RuntimeError("voice/script.txt が空で、shots.json に 'line' の無いカットがある")
    return lines


def load_yomi(project: Path) -> dict:
    data = common.load_json(common.safe_path(project, "voice/yomi.json"), default={})
    if not isinstance(data, dict):
        raise ValueError("voice/yomi.json は {\"表記\": \"読み\"} の平たいオブジェクトにする")
    return {str(k): str(v) for k, v in data.items() if str(k) and str(v)}


def apply_yomi(text: str, yomi: dict) -> str:
    """表記を読みに置き換える。長いキーから順に、1回で置き換える（置き換えた後をさらに置き換えない）。"""
    if not yomi:
        return text
    keys = sorted(yomi, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys))
    return pat.sub(lambda m: yomi[m.group(0)], text)


def _zen2han(s: str) -> str:
    return s.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ，",
                                     "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz,"))


def number_kana(n: int) -> str:
    """整数（0 <= n < 10^12）をひらがなにする。例: 40 -> よんじゅう、300 -> さんびゃく。"""
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
    if re.fullmatch(r"[A-Z]{1,3}", t):  # 短い略語だけ。LINE や NASA などは単語として読む
        return "".join(LETTERS[c] for c in t)
    return None


def scan_line(text: str) -> list[tuple[str, str]]:
    """辞書なしで機械が指摘できる項目: (種類, 文字列)。"""
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
        import pyopenjtalk  # 任意の依存パッケージ
        return pyopenjtalk.g2p
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="ナレーション原稿がどう読まれるかを確認する（有料の音声合成の前に）。")
    ap.add_argument("--project", help="プロジェクトのフォルダまたはスラッグ（省略時は今のフォルダ）")
    ap.add_argument("--suggest", action="store_true",
                    help="機械による読みの候補（数字・略語・pyopenjtalk の読み）を voice/yomi.json に追記する")
    a = ap.parse_args()

    project = common.project_dir(a.project)
    lines = read_script_lines(project)
    yomi = load_yomi(project)
    g2p = get_g2p()
    if g2p is None:
        print("警告: pyopenjtalk が入っていないので、機械で見つけられる注意点だけを表示する"
              "（全文の読みを見るには pip install pyopenjtalk）")

    suggestions: dict[str, str] = {}
    for i, line in enumerate(lines, 1):
        spoken = apply_yomi(line, yomi)
        print(f"\n[{i:02d}] {line}")
        if spoken != line:
            print(f"     読み上げ: {spoken}")
        if g2p is not None:
            try:
                print(f"     読み: {g2p(spoken, kana=True)}")
            except Exception as e:
                print(f"     読み: （pyopenjtalk が失敗した: {e}）")
        for kind, tok in scan_line(spoken):
            if kind == "kanji":
                print(f"     確認 {kind}: {tok}  （{RISKY[tok]}）")
                continue
            sug = suggest_reading(tok, g2p) if kind != "symbol" else None
            print(f"     確認 {kind}: {tok}" + (f"  -> 候補 {sug}" if sug else ""))
            if sug and tok not in yomi:
                suggestions[tok] = sug

    if a.suggest:
        if suggestions:
            merged = dict(yomi)
            added = {k: v for k, v in suggestions.items() if k not in merged}
            merged.update(added)
            common.save_json(common.safe_path(project, "voice/yomi.json"), merged)
            print(f"\nvoice/yomi.json に候補を {len(added)} 件追加した。生成の前に見直す:")
            for k, v in added.items():
                print(f"  {k} -> {v}")
        else:
            print("\n追加する候補は無い")
    elif suggestions:
        print(f"\n候補が {len(suggestions)} 件ある。--suggest を付けて再実行すると voice/yomi.json に追加する")


if __name__ == "__main__":
    common.cli(main)
