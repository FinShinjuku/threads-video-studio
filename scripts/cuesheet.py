#!/usr/bin/env python3
"""shots.json から、動画全体の流れを1行1カットで並べたキューシート（Excel）を作る。

  python3 cuesheet.py --project t004-camp                      # cuesheet.xlsx を書き出す（無料）
  python3 cuesheet.py --project t004-camp --check              # 書き出さずに抜け・尺のずれだけ確認する
  python3 cuesheet.py --project t004-camp --import cuesheet.xlsx  # 人が書いた「備考・ご指示」などを shots.json に戻す

1行 = 1カット。何秒から何秒まで、何を読み上げ、何を映すか、素材はどこまでできたかを1枚で見せる。
シートは「キューシート」「この表の見方」「確認」の3枚。標準ライブラリだけで xlsx を読み書きする。

取り込み（--import）で shots.json に戻すのは、計画の列（章・素材ID・絵の内容・素材の種類・備考・ご指示）だけ。
ナレーション・字幕・プロンプトがシートで書き換えられていても、自動では戻さない（ナレの作り直しや
承認のやり直しにつながるため）。違いを一覧にして知らせるので、Claude が人に確かめてから shots.json を直す。
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import common

SHEET = "キューシート"
KINDS = {"ai": "AI生成", "text": "文字カット", "screen": "画面収録", "footage": "実写素材"}
KIND_BY_LABEL = {v: k for k, v in KINDS.items()}
TYPE_TARGET = {"short": (5.0, 10.0), "story": (10.0, 15.0), "intro": (20.0, 30.0)}
CHARS_PER_SEC_MAX = 6.0

# (見出し, 幅, 取り込み時に shots.json へ戻すキー or None)
COLUMNS = [
    ("#", 5, None),
    ("章", 14, "chapter"),
    ("カットID", 8, None),
    ("開始", 8, None),
    ("秒数", 6, None),
    ("ナレーション（読み上げる文）", 44, None),
    ("字幕", 30, None),
    ("素材ID", 10, "asset_id"),
    ("絵の内容", 30, "picture"),
    ("動作", 22, None),
    ("カメラ", 18, None),
    ("素材の種類", 11, "kind"),
    ("状態", 13, None),
    ("カット動画ファイル", 24, None),
    ("動画プロンプト", 50, None),
    ("モデル", 14, None),
    ("備考・ご指示", 36, "note"),
]
WATCHED = {"ナレーション（読み上げる文）": "line", "字幕": "caption", "動画プロンプト": "prompt"}

GUIDE = [
    ("この表は何か", "動画1本の流れを、1行1カットで並べたキューシートです。shots.json（カットの正本）から作ります。"),
    ("#・カットID", "上から再生される順番です。カットID（c01 など）がファイル名と shots.json の目印です。"),
    ("章", "まとまりの名前です。章が変わる最初の行にだけ書いてあります。"),
    ("開始・秒数", "そのカットが始まる時刻（分:秒）と長さです。ナレーションを作ると、声に合わせて自動で決まります。"),
    ("ナレーション", "そのカットで読み上げる文です。1秒あたり6字を超えると早口になるので「確認」シートに出ます。"),
    ("字幕", "画面に出す文字です。空なら、ナレーションを自動で区切って出します。[[ ]] で囲んだ1語が強調されます。"),
    ("素材ID・絵の内容", "どの素材を使い、何を映すかを日本語で書いた欄です。"),
    ("動作・カメラ", "そのカットで動くもの1つと、カメラの動き1つです。"),
    ("素材の種類", "AI生成／文字カット（文字だけの静止画）／画面収録／実写素材 のどれかです。"),
    ("状態", "生成済（動画あり）／下書きのみ（安いモードの動画だけ）／静止画のみ（起点画像だけ）／未着手。"),
    ("カット動画ファイル", "動画が入る場所です。画面収録や実写も、この名前で置けば組み込まれます。"),
    ("備考・ご指示", "直してほしいことを自由に書いてください。この列と、章・素材ID・絵の内容・素材の種類は、"
                  "表を戻すと shots.json に取り込まれます。ナレーション・字幕・プロンプトを書き換えた場合は、"
                  "自動では反映せず、確認してから直します。"),
    ("色", "状態の欄：オレンジ＝生成済、黄＝下書きのみ、青＝文字カット、灰＝未着手。"),
]


# ---------------------------------------------------------------- 中身を作る
def fmt_time(sec) -> str:
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return ""
    m, r = divmod(s, 60)
    return f"{int(m)}:{r:04.1f}"


def caption_text(cut: dict) -> str:
    cap = cut.get("caption") or []
    return "\n".join(cap) if isinstance(cap, list) else str(cap)


def cut_state(project: Path, cut: dict) -> tuple[str, str]:
    """(状態, 動画ファイル) を、作品フォルダにあるファイルから決める。"""
    cid = cut["id"]
    clip, draft = project / "clips" / f"{cid}.mp4", project / "clips" / f"{cid}-draft.mp4"
    frame = project / (cut.get("frame") or f"frames/{cid}.png")
    if clip.exists():
        return "生成済", f"clips/{cid}.mp4"
    if draft.exists():
        return "下書きのみ", f"clips/{cid}-draft.mp4"
    if frame.exists():
        return ("文字カット" if cut.get("kind") == "text" else "静止画のみ"), str(frame.relative_to(project))
    return "未着手", f"clips/{cid}.mp4"


def rows(project: Path, shots: dict) -> list[list]:
    out, prev_chapter = [], None
    for i, cut in enumerate(shots.get("cuts", []), 1):
        chapter = cut.get("chapter") or ""
        state, path = cut_state(project, cut)
        out.append([
            i,
            chapter if chapter != prev_chapter else "",
            cut["id"],
            fmt_time(cut.get("start")),
            round(float(cut.get("dur") or 0), 2),
            cut.get("line", ""),
            caption_text(cut),
            cut.get("asset_id", ""),
            cut.get("picture", ""),
            cut.get("action", ""),
            cut.get("camera", ""),
            KINDS.get(cut.get("kind") or "ai", cut.get("kind") or ""),
            state,
            path,
            cut.get("prompt", ""),
            cut.get("model", ""),
            cut.get("note", ""),
        ])
        prev_chapter = chapter or prev_chapter
    return out


def check(shots: dict) -> list[str]:
    """抜け・尺のずれ・早口を見つける。空のリストなら問題なし。"""
    issues, cuts = [], shots.get("cuts", [])
    if not cuts:
        return ["カットが1つも無い"]
    for i, c in enumerate(cuts):
        cid, dur = c.get("id", f"#{i + 1}"), float(c.get("dur") or 0)
        for key, label in (("line", "ナレーション"), ("picture", "絵の内容"), ("action", "動作"),
                           ("camera", "カメラ"), ("prompt", "動画プロンプト")):
            if c.get("kind") in ("text", "screen", "footage") and key in ("action", "camera", "prompt"):
                continue  # AI で生成しないカットに動画プロンプトは要らない
            if key == "line" and any(str(t).strip() for t in c.get("caption") or []):
                continue  # 字幕があればナレなし（字幕だけの短い動画）でよい
            if not str(c.get(key) or "").strip():
                issues.append(f"{cid}: {label}が空")
        if dur <= 0:
            issues.append(f"{cid}: 秒数が0")
        n = len(re.sub(r"\s|\[\[|\]\]", "", c.get("line", "")))
        if dur > 0 and n / dur > CHARS_PER_SEC_MAX:
            issues.append(f"{cid}: ナレーションが1秒あたり{n / dur:.1f}字（{CHARS_PER_SEC_MAX:g}字まで）。原稿を削るか尺を延ばす")
        if i + 1 < len(cuts) and c.get("start") is not None and cuts[i + 1].get("start") is not None:
            gap = float(cuts[i + 1]["start"]) - (float(c["start"]) + dur)
            if abs(gap) > 0.05:
                kind = "すき間" if gap > 0 else "重なり"
                issues.append(f"{cid}→{cuts[i + 1].get('id')}: {abs(gap):.2f}秒の{kind}（開始＋秒数が次の開始と合わない）")
    total = sum(float(c.get("dur") or 0) for c in cuts)
    lo_hi = TYPE_TARGET.get(shots.get("type", ""))
    if lo_hi and not lo_hi[0] <= total <= lo_hi[1]:
        issues.append(f"全体 {total:.1f}秒が、型 {shots.get('type')} の目安 {lo_hi[0]:g}〜{lo_hi[1]:g}秒から外れている")
    return issues


# ---------------------------------------------------------------- xlsx を書く
def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


STATE_STYLE = {"生成済": 3, "下書きのみ": 4, "文字カット": 5, "未着手": 6}
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Yu Gothic"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Yu Gothic"/></font></fonts>
<fills count="7"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF2F2F2F"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFCE4D6"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEDEDED"/></patternFill></fill></fills>
<borders count="2"><border/><border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right><top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom></border></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="7">
<xf xfId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf xfId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf xfId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf xfId="0" fillId="3" borderId="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf xfId="0" fillId="4" borderId="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf xfId="0" fillId="5" borderId="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf xfId="0" fillId="6" borderId="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _cell(ref: str, value, style: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = escape(str(value or ""))
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def sheet_xml(header: list[str], body: list[list], widths: list[int], freeze: bool, state_col: int | None = None) -> str:
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths, 1))
    xml_rows = ['<row r="1">' + "".join(_cell(f"{col_letter(j)}1", h, 1) for j, h in enumerate(header, 1)) + "</row>"]
    for r, row in enumerate(body, 2):
        cells = []
        for j, v in enumerate(row, 1):
            style = STATE_STYLE.get(v, 2) if state_col == j else 2
            cells.append(_cell(f"{col_letter(j)}{r}", v, style))
        xml_rows.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    last = f"{col_letter(len(header))}{max(1, len(body) + 1)}"
    pane = ('<sheetViews><sheetView workbookViewId="0"><pane xSplit="3" ySplit="1" topLeftCell="D2" '
            'activePane="bottomRight" state="frozen"/></sheetView></sheetViews>') if freeze else ""
    flt = f'<autoFilter ref="A1:{last}"/>' if freeze else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
            f'{pane}<cols>{cols}</cols><sheetData>{"".join(xml_rows)}</sheetData>{flt}'
            '<pageMargins left="0.3" right="0.3" top="0.4" bottom="0.4" header="0.2" footer="0.2"/>'
            '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>')


def write_xlsx(path: Path, sheets: list[tuple[str, str]]) -> None:
    n = len(sheets)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, n + 1)) + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1))
          + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                         for i in range(1, n + 1))
               + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               + "</Relationships>")
    tmp = path.with_suffix(".xlsx.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", STYLES)
        for i, (_, xml) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    tmp.replace(path)


def export(project: Path, shots: dict, out: Path) -> list[str]:
    issues = check(shots)
    header = [c[0] for c in COLUMNS]
    body = rows(project, shots)
    total = sum(float(c.get("dur") or 0) for c in shots.get("cuts", []))
    summary = [["作品", shots.get("slug", project.name)], ["型", shots.get("type", "")],
               ["カット数", len(body)], ["全体の尺（秒）", round(total, 2)]]
    write_xlsx(out, [
        (SHEET, sheet_xml(header, body, [c[1] for c in COLUMNS], True, header.index("状態") + 1)),
        ("この表の見方", sheet_xml(["項目", "説明"], [list(g) for g in GUIDE], [18, 90], False)),
        ("確認", sheet_xml(["項目", "内容"], summary + [["要確認", i] for i in issues] + ([["要確認", "なし"]] if not issues else []),
                         [16, 90], False)),
    ])
    return issues


# ---------------------------------------------------------------- xlsx を読む
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{{{NS['m']}}}t"))


def read_sheet(path: Path, name: str = SHEET) -> list[dict]:
    """名前が name のシートを、1行目を見出しにした dict のリストで返す（Excel で保存し直したものも読める）。"""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rid = next((s.get(f"{{{NS['r']}}}id") for s in wb.iter(f"{{{NS['m']}}}sheet") if s.get("name") == name), None)
        if rid is None:
            raise ValueError(f"シート「{name}」が見つからない")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.iter(f"{{{NS['pr']}}}Relationship") if r.get("Id") == rid)
        target = target.lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = [_text(si) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{{{NS['m']}}}si")]
        ws = ET.fromstring(z.read(target))
    table = []
    for row in ws.iter(f"{{{NS['m']}}}row"):
        vals = {}
        for c in row.iter(f"{{{NS['m']}}}c"):
            col = re.sub(r"\d", "", c.get("r", ""))
            t, v = c.get("t"), c.find(f"{{{NS['m']}}}v")
            if t == "inlineStr":
                vals[col] = _text(c)
            elif t == "s" and v is not None:
                vals[col] = shared[int(v.text)]
            else:
                vals[col] = v.text if v is not None else ""
        table.append(vals)
    if not table:
        return []
    head = {col: str(h).strip() for col, h in table[0].items()}
    return [{head[col]: val for col, val in r.items() if col in head} for r in table[1:]]


def import_sheet(shots: dict, records: list[dict]) -> tuple[int, list[str]]:
    """計画の列を shots.json に戻す。(変更したカット数, 反映しなかった違いの一覧) を返す。"""
    by_id = {c["id"]: c for c in shots.get("cuts", [])}
    changed, skipped, chapter = 0, [], ""
    for rec in records:
        cid = str(rec.get("カットID") or "").strip()
        if not cid:
            continue
        cut = by_id.get(cid)
        if cut is None:
            skipped.append(f"{cid}: shots.json に無いカット（行の追加は shots.json で行う）")
            continue
        chapter = str(rec.get("章") or "").strip() or chapter  # 章は最初の行にだけ書いてあるので引き継ぐ
        new = {"chapter": chapter}
        for head, _, key in COLUMNS:
            if key and key != "chapter" and head in rec:
                val = str(rec.get(head) or "").strip()
                new[key] = KIND_BY_LABEL.get(val, val) if key == "kind" else val
        if new.get("kind") in ("", "ai") and not cut.get("kind"):
            new.pop("kind", None)  # 既定（AI生成）のままなら shots.json に書き足さない
        diff = {k: v for k, v in new.items() if (cut.get(k) or "") != v}
        if diff:
            cut.update(diff)
            changed += 1
        for head, key in WATCHED.items():
            if head not in rec:
                continue
            now = caption_text(cut) if key == "caption" else str(cut.get(key) or "")
            if str(rec.get(head) or "").strip() != now.strip():
                skipped.append(f"{cid}: 「{head}」がシートで書き換えられている（自動では反映しない）")
    return changed, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="shots.json から1行1カットのキューシート（xlsx）を作る／人の指示を取り込む（無料）。")
    ap.add_argument("--project", required=True, help="作品フォルダ（スラッグか絶対パス）")
    ap.add_argument("--out", default="cuesheet.xlsx", help="書き出し先（作品フォルダからの相対。既定 cuesheet.xlsx）")
    ap.add_argument("--check", action="store_true", help="書き出さずに、抜け・尺のずれ・早口だけを確認する")
    ap.add_argument("--import", dest="import_path", help="人が直したキューシート（xlsx）から、章・素材ID・絵の内容・素材の種類・備考を取り込む")
    a = ap.parse_args(argv)

    project = common.project_dir(a.project)
    shots = common.load_shots(project)
    if a.import_path:
        src = common.resolve(project, a.import_path)
        changed, skipped = import_sheet(shots, read_sheet(src))
        if changed:
            common.save_shots(project, shots)
        print(f"取り込んだ: {changed} カットを shots.json に反映")
        for s in skipped:
            print(f"  反映していない: {s}")
        return 0
    if a.check:
        issues = check(shots)
    else:
        out = common.safe_path(project, a.out, "--out")
        issues = export(project, shots, out)
        print(f"書き出した: {out}（{len(shots.get('cuts', []))} カット）")
    print("確認: 問題なし" if not issues else f"確認: {len(issues)} 件")
    for i in issues:
        print(f"  - {i}")
    return 0


if __name__ == "__main__":
    common.cli(main)
