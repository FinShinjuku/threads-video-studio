#!/usr/bin/env python3
"""Threads の投稿の数値を取得し、48時間後・7日後の振り返りを書く。

  pull    <workspace>/posts.json の公開済みの投稿それぞれについて、インサイト
          （views・likes・replies・reposts・quotes・shares）を取得し、日付付きの記録を
          <workspace>/metrics.csv に1行ずつ追記する。
  review  公開済みの投稿ごとに、公開から48時間・7日以降の記録があれば、(slug, checkpoint) ごとに1行を
          <workspace>/learnings.csv に足し（追記のみ。上書きしない）、まとめを
          <workspace>/reports/YYYY-MM-DD.md に書く。

判定（決まった計算で出す）: この投稿の views を、同じ時点での「それより前の投稿」の views の中央値で割った比。
  比 >= 1.5 -> 伸びた / 比 <= 0.67 -> 伸びなかった / それ以外 -> 普通 / 前の投稿が3本未満 -> 比較対象不足
`insight` と `next_action` は空のままにし、人（またはチャットの Claude）が書き込む。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import threads_api as api  # noqa: E402
from threads_post import load_queue, queue_path  # noqa: E402

METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
METRIC_COLS = ["fetched_at", "date", "slug", "post_id", *METRICS]
LEARN_COLS = ["slug", "checkpoint", "hook", "format", *METRICS, "engagement_rate", "verdict", "insight",
              "next_action", "post_id", "posted_at", "age_hours", "reviewed_at"]
CHECKPOINTS = (("48h", 48), ("7d", 168))
MIN_SAMPLES, HI, LO = 3, 1.5, 0.67
GREW, FLAT, DROPPED, TOO_FEW = "伸びた", "普通", "伸びなかった", "比較対象不足"


def to_int(v) -> int:
    try:
        return int(float(str(v).strip() or 0))
    except ValueError:
        return 0


def verdict(views: float, past_views: list[float]) -> str:
    if len(past_views) < MIN_SAMPLES:
        return TOO_FEW
    med = statistics.median(past_views)
    if med <= 0:
        return GREW if views > 0 else FLAT
    ratio = views / med
    if ratio >= HI:
        return GREW
    if ratio <= LO:
        return DROPPED
    return FLAT


def engagement_rate(m: dict):
    v = to_int(m.get("views"))
    if v <= 0:
        return ""
    return round(sum(to_int(m.get(k)) for k in METRICS[1:]) / v, 4)


# ---------------------------------------------------------------------------
# csv
# ---------------------------------------------------------------------------

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    api.atomic_write_text(path, buf.getvalue())


def published(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        pid = str(it.get("published_id", "")).strip()
        t = api.parse_time(it.get("published_at", ""))
        if pid and t:
            out.append({**it, "_t": t})
    return sorted(out, key=lambda x: x["_t"])


# ---------------------------------------------------------------------------
# 取得（pull）
# ---------------------------------------------------------------------------

def _metric_value(m: dict) -> int:
    if "total_value" in m:
        return (m["total_value"] or {}).get("value", 0) or 0
    return sum(v.get("value", 0) or 0 for v in m.get("values", []))


def post_insights(g, post_id: str) -> dict:
    data = g.get(f"{post_id}/insights", metric=",".join(METRICS)).get("data", [])
    vals = {m.get("name"): _metric_value(m) for m in data}
    return {k: vals.get(k, 0) for k in METRICS}


def pull(ws: Path, days: int, graph=None, now=None) -> dict:
    now = now or api.now()
    posts = [p for p in published(load_queue(queue_path(ws))) if now - p["_t"] <= dt.timedelta(days=days)]
    if not posts:
        print("対象期間に公開済みの投稿が無い")
        return {"pulled": 0, "errors": 0}
    graph = graph or api.Graph(common.require("threads_access_token"))
    path = ws / "metrics.csv"
    rows = read_csv(path)
    n = errs = 0
    for p in posts:
        try:
            m = post_insights(graph, p["published_id"])
        except RuntimeError as e:
            errs += 1
            print(f"スキップ {p['slug']}: {api.redact(str(e))[:200]}")
            continue
        rows.append({"fetched_at": api.iso(now), "date": now.date().isoformat(), "slug": p["slug"],
                     "post_id": p["published_id"], **m})
        n += 1
        print(f"{p['slug']}: " + " ".join(f"{k}={m[k]}" for k in METRICS))
    if n:
        write_csv(path, METRIC_COLS, rows)
    return {"pulled": n, "errors": errs}


# ---------------------------------------------------------------------------
# 振り返り（review）
# ---------------------------------------------------------------------------

def snapshots(rows: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for r in rows:
        t = api.parse_time(r.get("fetched_at", ""))
        if t:
            by.setdefault(str(r.get("post_id", "")), []).append({**r, "_t": t})
    for v in by.values():
        v.sort(key=lambda r: r["_t"])
    return by


def build_learnings(posts: list[dict], snaps: dict, existing: list[dict], now) -> list[dict]:
    """まだ行が無く、その時点の記録がある (slug, checkpoint) の組について、新しい行を作る。"""
    have = {(r.get("slug"), r.get("checkpoint")) for r in existing}
    new = []
    for cp, hours in CHECKPOINTS:
        hist = [(api.parse_time(r.get("posted_at", "")), to_int(r.get("views")))
                for r in existing if r.get("checkpoint") == cp]
        hist = [(t, v) for t, v in hist if t]
        for p in posts:
            if (p["slug"], cp) in have:
                continue
            due = p["_t"] + dt.timedelta(hours=hours)
            snap = next((s for s in snaps.get(str(p["published_id"]), []) if s["_t"] >= due), None)
            if snap is None:
                continue
            row = {"slug": p["slug"], "checkpoint": cp, "hook": p.get("hook", ""), "format": p.get("format", ""),
                   **{k: to_int(snap.get(k)) for k in METRICS}, "engagement_rate": engagement_rate(snap),
                   "insight": "", "next_action": "", "post_id": p["published_id"], "posted_at": api.iso(p["_t"]),
                   "age_hours": round((snap["_t"] - p["_t"]).total_seconds() / 3600, 1),
                   "reviewed_at": api.iso(now)}
            row["verdict"] = verdict(row["views"], [v for t, v in hist if t < p["_t"]])
            hist.append((p["_t"], row["views"]))
            new.append(row)
    return new


def render_report(day: dt.date, posts: list[dict], snaps: dict, new_rows: list[dict]) -> str:
    L = [f"# Threads 振り返り {day.isoformat()}", "", "## 投稿ごとの最新の数値", ""]
    table = []
    for p in posts:
        s = (snaps.get(str(p["published_id"])) or [None])[-1]
        if s:
            table.append({"slug": p["slug"], "hook": p.get("hook", ""), "s": s, "views": to_int(s.get("views")),
                          "er": engagement_rate(s), "posted": api.iso(p["_t"])[:16]})
    if table:
        L += ["| slug | posted | views | likes | replies | reposts | ER | hook |", "|---|---|---:|---:|---:|---:|---:|---|"]
        for r in table:
            s = r["s"]
            L.append(f"| {r['slug']} | {r['posted']} | {r['views']} | {to_int(s.get('likes'))} | "
                     f"{to_int(s.get('replies'))} | {to_int(s.get('reposts'))} | {r['er'] or '-'} | "
                     f"{str(r['hook']).replace('|', '｜')[:40]} |")
        ranked = sorted(table, key=lambda r: -r["views"])
        L += ["", f"- 最も多い: {ranked[0]['slug']}（{ranked[0]['views']} views）",
              f"- 最も少ない: {ranked[-1]['slug']}（{ranked[-1]['views']} views）"]
    else:
        L.append("（まだ数値が無い。`threads_insights.py pull` を実行してください）")
    L += ["", "## 新しい振り返り", ""]
    if new_rows:
        L += ["| slug | checkpoint | views | ER | verdict |", "|---|---|---:|---:|---|"]
        L += [f"| {r['slug']} | {r['checkpoint']} | {r['views']} | {r['engagement_rate'] or '-'} | {r['verdict']} |"
              for r in new_rows]
        L += ["", "learnings.csv のこれらの行の `insight` と `next_action` を書き込んでください。"]
    else:
        L.append("（今回は無し）")
    return "\n".join(L) + "\n"


def review(ws: Path, now=None) -> dict:
    now = now or api.now()
    posts = published(load_queue(queue_path(ws)))
    snaps = snapshots(read_csv(ws / "metrics.csv"))
    lpath = ws / "learnings.csv"
    existing = read_csv(lpath)
    new_rows = build_learnings(posts, snaps, existing, now)
    if new_rows:
        write_csv(lpath, LEARN_COLS, existing + new_rows)
    report = render_report(now.date(), posts, snaps, new_rows)
    rpath = ws / "reports" / f"{now.date().isoformat()}.md"
    api.atomic_write_text(rpath, report)
    for r in new_rows:
        print(f"{r['slug']} {r['checkpoint']}: views={r['views']} -> {r['verdict']}")
    print(f"振り返り: +{len(new_rows)} 行（{lpath}）、レポート: {rpath}")
    return {"new": len(new_rows), "report": str(rpath)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Threads の数値を取得し、48時間後・7日後の振り返りを書く。")
    ap.add_argument("--workspace", help="ワークスペースのフォルダを指定する（既定: 設定の値）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="公開済みの投稿の数値を metrics.csv に追記する")
    p.add_argument("--days", type=int, default=30, help="N 日以内に公開した投稿だけを対象にする（既定 30）")
    sub.add_parser("review", help="48時間・7日の行を learnings.csv に書き、レポートを作る")
    a = ap.parse_args(argv)
    ws = Path(a.workspace).expanduser() if a.workspace else common.workspace()
    try:
        with api.FileLock(ws / ".insights.lock"):
            if a.cmd == "pull":
                s = pull(ws, a.days)
                return 1 if s["errors"] and not s["pulled"] else 0
            review(ws)
            return 0
    except api.LockHeld as e:
        print(f"スキップ: {e}", file=sys.stderr)
        return 0
    except RuntimeError as e:
        print(f"エラー: {api.redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
