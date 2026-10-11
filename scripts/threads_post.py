#!/usr/bin/env python3
"""Threads の投稿キューと予約公開。

キュー: <workspace>/posts.json（JSON 配列。原子的に書き、ロックファイルで守る）。

承認ゲート（最重要）
  `approve` を Claude が実行してよいのは、人が完成動画と投稿文を見たうえで、チャットで明示的に OK と
  言ったときだけ。自分の判断で承認しない。チェックを「通すため」に承認しない。
  このスクリプトが自分で status=approved にすることはない。そうするのは `approve` サブコマンドだけ。
  公開は取り消せない（Threads には編集も送信取り消しも無い）。

状態の値: draft -> approved -> publishing -> published   （または failed）

publish-due が公開するのは、次のすべてを満たすものだけ:
  status == approved、approved_by が空でない、scheduled_at <= 現在、published_id が無い、
  承認後に本文・動画ファイルが変わっていない。
二重投稿の防止:
  - published_id があるものは二度と送らない
  - API を呼ぶ前に status=publishing を書き（読み戻して確認し）ておく
  - `publishing` のまま残ったもの（クラッシュ・結果不明）は自動で再試行しない。人が Threads を確かめ、
    `mark-published` を実行するか下書きからやり直す
  - ロックファイルで同時に2つ動かない

本文ツリー（返信）:
  draft に --thread-file post-thread.txt を付けると、`---` だけの行で区切った各段落を、公開した投稿への
  返信として順に出す（1つ前の返信にぶら下げる）。本文も承認の対象（変えたら承認し直し）。
  返信ごとに、公開を呼ぶ前に thread_state=posting:<n> を書く。呼んだあとに失敗したら unknown:<n> にして
  自動では再試行しない（二重投稿の防止）。コンテナ作成で失敗したものは、次の実行で続きから出す（最大3回）。

サブコマンド:
  draft --project <dir> --text-file post.txt [--thread-file post-thread.txt] [--topic AI活用] [--at "YYYY-MM-DD HH:MM"] [--hook ..] [--format ..]
  approve <slug> --by <name>          （チャットで人が OK したときだけ）
  schedule <slug> --at "YYYY-MM-DD HH:MM"
  status
  publish-due [--dry-run] [--max 3]
  mark-published <slug> --post-id <id> [--permalink URL]   （手動で投稿したものを記録する）
  refresh-token                       （長期トークンを延長し、設定ファイルに保存する）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import threads_api as api  # noqa: E402

MAX_TEXT = 500
MAX_LINKS = 5
POLL_TIMEOUT_S = 600
POLL_INTERVAL_S = 10
DRAFT, APPROVED, PUBLISHING, PUBLISHED, FAILED = "draft", "approved", "publishing", "published", "failed"
URL_RE = re.compile(r"https?://[^\s<>\"'）」]+", re.I)


# ---------------------------------------------------------------------------
# キュー
# ---------------------------------------------------------------------------

def queue_path(ws: Path | None = None) -> Path:
    return (ws or common.workspace()) / "posts.json"


def lock_path(ws: Path | None = None) -> Path:
    return (ws or common.workspace()) / ".posts.lock"


def load_queue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RuntimeError(f"{path} が正しい JSON ではない（{e}）。手で直してから続けてください") from None
    if not isinstance(data, list):
        raise RuntimeError(f"{path} は JSON 配列でなければならない")
    return data


def save_queue(path: Path, items: list[dict]) -> None:
    api.atomic_write_json(path, items)
    if load_queue(path) != items:
        raise RuntimeError(f"{path} を書いたあと読み戻したら一致しない")


def find(items: list[dict], slug: str) -> dict | None:
    return next((i for i in items if i.get("slug") == slug), None)


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------

def validate_text(text: str) -> list[str]:
    errs = []
    if not text.strip():
        errs.append("投稿文が空")
    if len(text) > MAX_TEXT:
        errs.append(f"投稿文が {len(text)} 文字ある（上限 {MAX_TEXT} 文字）")
    n = len(URL_RE.findall(text))
    if n > MAX_LINKS:
        errs.append(f"投稿文にリンクが {n} 個ある（上限 {MAX_LINKS} 個）")
    return errs


def validate_topic(topic: str) -> list[str]:
    if not topic:
        return []
    errs = []
    if len(topic) > 50:
        errs.append("トピックは 50 文字以内にする")
    if "." in topic or "&" in topic:
        errs.append("トピックに '.' と '&' は使えない")
    return errs


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


THREAD_SEP = re.compile(r"^\s*---\s*$", re.M)
THREAD_MAX_TRIES = 3


def read_thread(path: Path) -> list[str]:
    """`---` だけの行で区切った本文ツリー。空の段落は捨てる。"""
    parts = [x.strip() for x in THREAD_SEP.split(path.read_text(encoding="utf-8"))]
    return [x for x in parts if x]


def content_hash(item: dict) -> str:
    """人が承認した内容そのもののハッシュ: 本文・トピック・予約時刻・動画
    （media_url があればその文字列、無ければ動画ファイルの中身の SHA-256）・本文ツリー（あれば）。"""
    h = hashlib.sha256()
    for k in ("text", "topic", "scheduled_at", "media_url"):
        h.update(k.encode() + b"=" + str(item.get(k, "")).encode() + b"\0")
    if item.get("thread"):
        h.update(b"thread=" + json.dumps(item["thread"], ensure_ascii=False).encode() + b"\0")
    if not str(item.get("media_url", "")).strip():
        vf = Path(str(item.get("video_file", ""))).expanduser()
        h.update(b"video=" + (file_sha256(vf).encode() if vf.is_file() else b"missing"))
    return h.hexdigest()


def eligible(item: dict, now) -> tuple[bool, str]:
    """自動公開の対象か。確認の順番に意味がある（published_id を最初に見る）。"""
    if str(item.get("published_id", "")).strip():
        return False, "公開済み（published_id がある）"
    st = item.get("status", "")
    if st == PUBLISHING:
        return False, "'publishing' のまま残っている（結果不明）。Threads を手で確かめてください。自動では再試行しない"
    if st != APPROVED:
        return False, f"status={st or 'empty'}"
    if not str(item.get("approved_by", "")).strip():
        return False, "approved_by が空（人の承認が無い）"
    t = api.parse_time(item.get("scheduled_at", ""))
    if t is None:
        return False, "scheduled_at が空か不正"
    if t > now:
        return False, f"{api.iso(t)} に予約済み"
    return True, "ok"


def check_before_publish(item: dict) -> str | None:
    """公開直前の内容チェック。エラーメッセージか None を返す。"""
    errs = validate_text(item.get("text", "")) + validate_topic(item.get("topic", ""))
    for i, part in enumerate(item.get("thread") or [], 2):
        errs += [f"{i}投稿目: {e}" for e in validate_text(part)]
    if errs:
        return "; ".join(errs)
    if item.get("status") == APPROVED and item.get("approved_hash") != content_hash(item):
        return "承認後に本文・トピック・時刻・動画のどれかが変わった。公開する前に承認し直してください"
    url = str(item.get("media_url", "")).strip()
    if url:
        if not url.startswith("https://"):
            return "media_url は https の URL にする"
        return None
    vf = item.get("video_file", "")
    if not vf or not Path(vf).expanduser().is_file():
        return f"動画ファイルが無い: {vf or '(未設定)'}"
    return None


# ---------------------------------------------------------------------------
# 公開
# ---------------------------------------------------------------------------

def media_url_for(item: dict, stage=None) -> str:
    url = str(item.get("media_url", "")).strip()
    if url:
        return url
    if stage is None:
        import media_host
        stage = media_host.stage
    return stage(str(Path(item["video_file"]).expanduser()))


def wait_container(g, cid: str, sleep=time.sleep, clock=time.monotonic, timeout: int = POLL_TIMEOUT_S) -> None:
    start = clock()
    while True:
        st = g.get(cid, fields="status,error_message")
        s = st.get("status")
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"コンテナ {cid} {s}: {st.get('error_message', '')}")
        if clock() - start > timeout:
            raise RuntimeError(f"コンテナ {cid} が {timeout} 秒以内に FINISHED にならない（status={s}）")
        sleep(POLL_INTERVAL_S)


def publish_reply(g, uid: str, text: str, reply_to: str, sleep=time.sleep, clock=time.monotonic,
                  on_phase=None) -> str:
    """文字だけの返信を1つ出し、投稿 ID を返す。公開を呼ぶ直前に on_phase("publish_call")。"""
    cid = str(g.post(f"{api.check_id(uid)}/threads", media_type="TEXT", text=text,
                     reply_to_id=api.check_id(reply_to)).get("id", "")).strip()
    if not cid:
        raise RuntimeError("返信のコンテナ ID が返ってこない")
    cid = api.check_id(cid)
    wait_container(g, cid, sleep, clock, timeout=120)
    if on_phase:
        on_phase("publish_call")
    pid = str(g.post(f"{uid}/threads_publish", creation_id=cid).get("id", "")).strip()
    if not pid:
        raise RuntimeError(f"返信のコンテナ {cid} を公開したが投稿 ID が返ってこない。Threads を確かめてください")
    return api.check_id(pid)


def thread_pending(item: dict) -> bool:
    """公開済みで、本文ツリーの返信がまだ残っていて、自動で続けてよいか。"""
    th = item.get("thread") or []
    if item.get("status") != PUBLISHED or not str(item.get("published_id", "")).strip() or not th:
        return False
    st = str(item.get("thread_state", ""))
    if st == "done" or st.startswith("unknown") or st.startswith("posting"):
        return False
    if int(item.get("thread_tries", 0)) >= THREAD_MAX_TRIES:
        return False
    return len(item.get("thread_ids") or []) < len(th)


def publish_thread(qpath: Path, slug: str, g, uid: str, sleep=time.sleep, clock=time.monotonic) -> dict:
    """本文ツリーの残りを、1つ前の投稿への返信として順に出す。キューに1つずつ書き戻す。"""
    out = {"posted": 0, "error": ""}
    while True:
        items = load_queue(qpath)
        cur = find(items, slug)
        if cur is None or not thread_pending(cur):
            return out
        ids = list(cur.get("thread_ids") or [])
        n = len(ids)
        parent = ids[-1] if ids else cur["published_id"]
        text = cur["thread"][n]
        phase = {"p": "container"}
        cur.update(thread_state=f"posting:{n + 2}", updated_at=api.iso(api.now()))
        save_queue(qpath, items)
        try:
            pid = publish_reply(g, uid, text, parent, sleep=sleep, clock=clock, on_phase=lambda p: phase.update(p=p))
        except Exception as e:  # noqa: BLE001
            msg = api.redact(e)[:300]
            items = load_queue(qpath)
            cur = find(items, slug)
            if phase["p"] == "publish_call":
                cur.update(thread_state=f"unknown:{n + 2}",
                           thread_error=f"{n + 2}投稿目の結果不明。Threads を手で確かめてください: {msg}")
            else:
                cur.update(thread_state=f"failed:{n + 2}", thread_error=msg,
                           thread_tries=int(cur.get("thread_tries", 0)) + 1)
            cur["updated_at"] = api.iso(api.now())
            save_queue(qpath, items)
            print(f"返信に失敗 {slug} {n + 2}投稿目: {msg}")
            out["error"] = msg
            return out
        items = load_queue(qpath)
        cur = find(items, slug)
        ids.append(pid)
        done = len(ids) >= len(cur["thread"])
        cur.update(thread_ids=ids, thread_state="done" if done else "", thread_error="", updated_at=api.iso(api.now()))
        save_queue(qpath, items)
        out["posted"] += 1
        print(f"返信した {slug} {n + 2}投稿目: {pid}")


def publish_one(g, uid: str, item: dict, stage=None, sleep=time.sleep, clock=time.monotonic,
                on_phase=None) -> dict:
    """VIDEO コンテナを作る -> FINISHED を待つ（最大10分）-> threads_publish。

    公開を呼ぶ直前に on_phase("published_call") を呼ぶ。呼び出し側はこれで、以降に失敗したら
    結果が分からないことを知る。{"published_id", "permalink", "published_at"} を返す。
    """
    params = {"media_type": "VIDEO", "video_url": media_url_for(item, stage), "text": item.get("text", "")}
    if item.get("topic"):
        params["topic_tag"] = item["topic"]
    cid = str(g.post(f"{api.check_id(uid)}/threads", **params).get("id", "")).strip()
    if not cid:
        raise RuntimeError("コンテナ ID が返ってこない")
    cid = api.check_id(cid)
    wait_container(g, cid, sleep, clock)
    if on_phase:
        on_phase("publish_call")
    pid = str(g.post(f"{uid}/threads_publish", creation_id=cid).get("id", "")).strip()
    if not pid:
        raise RuntimeError(f"コンテナ {cid} を公開したが投稿 ID が返ってこない。Threads を確かめてください")
    pid = api.check_id(pid)
    try:
        d = g.get(pid, fields="id,permalink,timestamp")
    except RuntimeError:
        d = {}
    t = api.parse_time(d.get("timestamp", ""))
    return {"published_id": pid, "permalink": d.get("permalink", ""),
            "published_at": api.iso(t) if t else api.iso(api.now())}


def publish_due(qpath: Path, dry_run: bool, max_n: int, graph=None, uid: str = "", stage=None,
                sleep=time.sleep, clock=time.monotonic, now=None) -> dict:
    """graph / uid / stage / sleep / clock / now はテストで差し替えるための引数。"""
    now = now or api.now()
    items = load_queue(qpath)
    targets, invalid = [], []
    for it in items:
        ok, why = eligible(it, now)
        if not ok:
            if it.get("status") in (APPROVED, PUBLISHING):
                print(f"スキップ {it.get('slug')}: {why}")
            continue
        err = check_before_publish(it)
        (invalid if err else targets).append((it, err))
        print(api.redact(f"{'[dry-run] ' if dry_run else ''}{'公開できない' if err else '公開対象'} "
                         f"{it.get('slug')}: {err or 'OK'}"))
    resumes = [it["slug"] for it in items if thread_pending(it)]
    for sl in resumes:
        print(f"{'[dry-run] ' if dry_run else ''}本文ツリーの続き {sl}")
    summary = {"due": len(targets), "invalid": len(invalid), "published": [], "failed": [], "unknown": [],
               "replies": 0}
    if dry_run:
        return summary
    for it, err in invalid:
        it.update(status=FAILED, error=api.redact(err), updated_at=api.iso(api.now()))
        summary["failed"].append({"slug": it["slug"], "error": err})
    if invalid:
        save_queue(qpath, items)
    if not targets and not resumes:
        return summary
    if graph is None:
        graph = api.Graph(common.require("threads_access_token"))
    uid = api.user_id(graph, uid or common.get("threads_user_id"))
    for sl in resumes:
        summary["replies"] += publish_thread(qpath, sl, graph, uid, sleep=sleep, clock=clock)["posted"]

    for it, _ in targets[:max_n]:
        slug = it["slug"]
        # 同時編集に備え、公開直前に読み直して確認し直す
        items = load_queue(qpath)
        cur = find(items, slug)
        if cur is None or not eligible(cur, now)[0] or check_before_publish(cur):
            print(f"スキップ {slug}: 読み直したら変わっていた")
            continue
        cur.update(status=PUBLISHING, error="", publishing_started_at=api.iso(api.now()))
        save_queue(qpath, items)
        phase = {"p": "container"}
        try:
            res = publish_one(graph, uid, cur, stage=stage, sleep=sleep, clock=clock,
                              on_phase=lambda p: phase.update(p=p))
        except Exception as e:  # noqa: BLE001
            msg = api.redact(e)[:400]
            items = load_queue(qpath)
            cur = find(items, slug)
            if phase["p"] == "publish_call":
                # 結果不明: 自動で再試行されないよう `publishing` のまま残す
                cur.update(error=f"結果不明。Threads を手で確かめてください: {msg}", updated_at=api.iso(api.now()))
                summary["unknown"].append({"slug": slug, "error": msg})
            else:
                cur.update(status=FAILED, error=msg, updated_at=api.iso(api.now()))
                summary["failed"].append({"slug": slug, "error": msg})
            save_queue(qpath, items)
            print(f"失敗 {slug}: {msg}")
            continue
        items = load_queue(qpath)
        cur = find(items, slug)
        cur.update(status=PUBLISHED, error="", updated_at=api.iso(api.now()), **res)
        save_queue(qpath, items)
        summary["published"].append({"slug": slug, **res})
        print(f"公開した {slug}: {res['published_id']} {res['permalink']}")
        if cur.get("thread"):
            summary["replies"] += publish_thread(qpath, slug, graph, uid, sleep=sleep, clock=clock)["posted"]
    return summary


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------

def resolve_project(p: str) -> Path:
    cand = Path(p).expanduser()
    if not cand.is_absolute() and not cand.exists():
        cand = common.workspace() / p
    cand = cand.resolve()
    if not cand.is_dir():
        sys.exit(f"プロジェクトフォルダが無い: {p}")
    return cand


def project_slug(project: Path) -> str:
    try:
        return json.loads((project / "shots.json").read_text(encoding="utf-8")).get("slug") or project.name
    except (OSError, ValueError):
        return project.name


def project_format(project: Path) -> str:
    try:
        return json.loads((project / "shots.json").read_text(encoding="utf-8")).get("type", "")
    except (OSError, ValueError):
        return ""


def parse_at(s: str) -> str:
    t = api.parse_time(s)
    if t is None:
        sys.exit(f"時刻が不正: {s!r}（\"YYYY-MM-DD HH:MM\" の形で指定する）")
    return api.iso(t)


def cta_link(cta_url: str, slug: str) -> str:
    """cta_url に t=<slug> を付けたもの（既存のクエリパラメータは残し、既存の t は置き換える）。"""
    u = urllib.parse.urlsplit(cta_url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "t"]
    q.append(("t", slug))
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), u.fragment))


def add_cta(text: str, cta_url: str, slug: str) -> str:
    """本文に cta_url で始まるリンクが無ければ、計測用の CTA リンクを末尾に足す。"""
    cta_url = (cta_url or "").strip()
    if not cta_url:
        return text
    base = cta_url.split("?")[0].split("#")[0].rstrip("/")
    if any(u.startswith(base) for u in URL_RE.findall(text)):
        return text
    return f"{text}\n{cta_link(cta_url, slug)}" if text else cta_link(cta_url, slug)


def cmd_draft(a, qpath: Path) -> int:
    project = resolve_project(a.project)
    slug = api.check_slug(project_slug(project))
    tf = Path(a.text_file).expanduser()
    if not tf.is_file():
        tf = project / a.text_file
    if not tf.is_file():
        sys.exit(f"投稿文のファイルが無い: {a.text_file}")
    text = add_cta(tf.read_text(encoding="utf-8").strip(), common.get("cta_url"), slug)
    errs = validate_text(text) + validate_topic(a.topic or "")
    if errs:
        sys.exit("投稿内容が不正: " + "; ".join(errs))
    video = project / "final" / f"{slug}-post.mp4"
    if not a.media_url and not video.is_file():
        sys.exit(f"{video} が無い。先に assemble.py --project {project} --render を実行してください")
    thread = None
    if a.thread_file:
        thf = Path(a.thread_file).expanduser()
        if not thf.is_file():
            thf = project / a.thread_file
        if not thf.is_file():
            sys.exit(f"本文ツリーのファイルが無い: {a.thread_file}")
        thread = read_thread(thf)
        terrs = [f"{i}投稿目: {e}" for i, part in enumerate(thread, 2) for e in validate_text(part)]
        if terrs:
            sys.exit("本文ツリーが不正: " + "; ".join(terrs))
    items = load_queue(qpath)
    cur = find(items, slug)
    ts = api.iso(api.now())
    if cur and (cur.get("published_id") or cur.get("status") in (PUBLISHED, PUBLISHING)):
        sys.exit(f"{slug} は {cur.get('status')} なので編集できない（Threads に編集は無い）。新しいスラッグを使ってください")
    if cur is None:
        cur = {"slug": slug, "created_at": ts}
        items.append(cur)
    elif cur.get("status") == APPROVED:
        print(f"注意: {slug} は承認済みだった。編集したので下書きに戻る（人の OK をもらってから、もう一度 approve する）")
    cur.update({
        "project": str(project), "video_file": str(video), "media_url": a.media_url or "", "text": text,
        "topic": a.topic if a.topic is not None else cur.get("topic", ""),
        "hook": a.hook if a.hook is not None else cur.get("hook", text.splitlines()[0][:40]),
        "format": a.format if a.format is not None else cur.get("format", project_format(project)),
        "status": DRAFT, "approved_by": "", "approved_at": "", "approved_hash": "", "approval_note": "",
        "error": "", "updated_at": ts,
        "thread": thread if thread is not None else cur.get("thread", []),
        "thread_ids": [], "thread_state": "", "thread_error": "", "thread_tries": 0,
    })
    if a.at:
        cur["scheduled_at"] = parse_at(a.at)
    cur.setdefault("scheduled_at", "")
    save_queue(qpath, items)
    api.atomic_write_json(project / "post.json", cur)
    print(f"下書きを保存した: {slug}（{len(text)} 文字、本文ツリー {len(cur['thread'])} 件、"
          f"scheduled_at={cur['scheduled_at'] or '未設定'}）")
    for i, part in enumerate(cur["thread"], 2):
        print(f"  {i}投稿目: {len(part)} 文字")
    print("次: 動画と本文を人に見せる。チャットで明示的に OK をもらってから `approve` を実行する")
    return 0


def cmd_approve(a, qpath: Path) -> int:
    items = load_queue(qpath)
    cur = find(items, a.slug)
    if cur is None:
        sys.exit(f"スラッグ {a.slug} の投稿がキューに無い")
    if cur.get("status") != DRAFT:
        sys.exit(f"{a.slug} は {cur.get('status')}。承認できるのは下書き（draft）だけ")
    if not a.by.strip():
        sys.exit("--by には承認した人の名前を入れる")
    if not a.note.strip():
        sys.exit("--note にはチャットでの人の承認メッセージをそのまま入れる")
    err = check_before_publish({**cur, "approved_hash": ""})
    if err:
        sys.exit(f"承認できない: {err}")
    cur.update(status=APPROVED, approved_by=a.by.strip(), approved_at=api.iso(api.now()),
               approval_note=api.redact(a.note.strip())[:500], updated_at=api.iso(api.now()))
    cur["approved_hash"] = content_hash(cur)
    save_queue(qpath, items)
    print(f"承認した: {a.slug}（承認者 {cur['approved_by']}、scheduled_at={cur.get('scheduled_at') or '未設定'}）")
    return 0


def cmd_schedule(a, qpath: Path) -> int:
    items = load_queue(qpath)
    cur = find(items, a.slug)
    if cur is None:
        sys.exit(f"スラッグ {a.slug} の投稿がキューに無い")
    if cur.get("published_id") or cur.get("status") in (PUBLISHED, PUBLISHING):
        sys.exit(f"{a.slug} は {cur.get('status')} なので予約を変えられない")
    new_at = parse_at(a.at)
    changed = new_at != cur.get("scheduled_at")
    cur.update(scheduled_at=new_at, updated_at=api.iso(api.now()))
    if changed and cur.get("status") == APPROVED:
        cur.update(status=DRAFT, approved_by="", approved_at="", approved_hash="", approval_note="")
        print(f"注意: {a.slug} は承認済みだった。時刻を変えたので下書きに戻った。"
              f"人の OK をもう一度もらい、`approve` し直す必要がある")
    save_queue(qpath, items)
    print(f"予約した: {a.slug} を {cur['scheduled_at']}（status={cur.get('status')}）")
    return 0


def cmd_status(a, qpath: Path) -> int:
    items = load_queue(qpath)
    if not items:
        print(f"キューは空（{qpath}）")
        return 0
    now = api.now()
    for it in items:
        ok, why = eligible(it, now)
        extra = it.get("permalink") or it.get("error") or ("今が公開時刻" if ok else why)
        print(api.redact(f"{it.get('slug', '?'):<24} {it.get('status', ''):<10} at={it.get('scheduled_at') or '-':<25} "
                         f"by={it.get('approved_by') or '-':<10} {extra}"))
        if it.get("approval_note"):
            print(f"{'':<24} 承認メモ: \"{api.redact(it['approval_note'])}\"")
    return 0


def cmd_mark_published(a, qpath: Path) -> int:
    items = load_queue(qpath)
    cur = find(items, a.slug)
    if cur is None:
        sys.exit(f"スラッグ {a.slug} の投稿がキューに無い")
    api.check_id(a.post_id)
    if cur.get("published_id") and cur["published_id"] != a.post_id:
        sys.exit(f"{a.slug} にはすでに published_id {cur['published_id']} がある")
    cur.update(status=PUBLISHED, published_id=a.post_id, permalink=a.permalink or cur.get("permalink", ""),
               published_at=cur.get("published_at") or api.iso(api.now()), error="",
               updated_at=api.iso(api.now()), published_by="manual")
    save_queue(qpath, items)
    print(f"公開済みとして記録した: {a.slug} -> {a.post_id}")
    return 0


def cmd_refresh_token(a, qpath: Path) -> int:
    token = common.require("threads_access_token")
    res = api.refresh_long_lived(token)
    ref = common.keychain_ref(common.raw_get("threads_access_token"))
    if ref:  # Keychain に置いている場合は Keychain を更新する
        common.write_keychain(ref[0], ref[1], res["access_token"])
        if common.read_keychain(*ref) != res["access_token"]:
            sys.exit("トークンの延長: Keychain を読み戻したら一致しない")
        days = res["expires_in"] // 86400 if res["expires_in"] else "?"
        print(f"トークンを延長した（{days} 日）。Keychain の {ref[0]} を更新した")
        return 0
    data = common.load_file()
    data["threads_access_token"] = res["access_token"]
    if res["expires_in"]:
        data["threads_token_expires_at"] = api.iso(api.now() + dt.timedelta(seconds=res["expires_in"]))
    common.save_file(data)
    if common.load_file().get("threads_access_token") != res["access_token"]:
        sys.exit("トークンの延長: 設定ファイルを読み戻したら一致しない")
    print(f"トークンを延長した（{res['expires_in'] // 86400 if res['expires_in'] else '?'} 日）。"
          f"{common.CONFIG_FILE} に保存した")
    if common._plugin_option("threads_access_token") or any(
            os.environ.get(e) for e in common.SETTINGS["threads_access_token"][0]):
        print("警告: トークンがプラグイン設定か環境変数にも入っていて、そちらが設定ファイルより優先される。"
              "そちらも更新してください（python3 scripts/setup.py set ... では上書きできない）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Threads の投稿キュー（<workspace>/posts.json）と予約公開。",
        epilog="承認: `approve` は運用ルールであって技術的なチェックではない。人が完成動画・本文・時刻を見て、"
               "チャットで明示的に OK と言ったあとにだけ実行する（誰が入力しているかはスクリプトには分からない）。"
               "公開は取り消せない。")
    ap.add_argument("--workspace", help="ワークスペースのフォルダを指定する（既定: 設定の値）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft", help="プロジェクトの final/<slug>-post.mp4 から下書きを追加・更新する")
    d.add_argument("--project", required=True)
    d.add_argument("--text-file", required=True, help="投稿文のファイル（500 文字以内、リンク 5 個まで）")
    d.add_argument("--thread-file", help="本文ツリー（`---` だけの行で区切る）。公開後に返信として順に出す")
    d.add_argument("--topic", help="トピックタグ。例 AI活用（topic_tag として送る）")
    d.add_argument("--at", help='予約時刻 "YYYY-MM-DD HH:MM"（ローカル時刻）')
    d.add_argument("--media-url", help="動画ファイルをアップロードせず、この https URL を使う")
    d.add_argument("--hook", help="振り返り用のフックの名前（既定: 本文の1行目）")
    d.add_argument("--format", help="振り返り用の形式の名前（既定: shots.json の type）")

    p = sub.add_parser("approve", help="下書きを承認済みにする。チャットで人が明示的に OK したときだけ",
                       description="下書きを承認済みにする。運用ルール: Claude がこれを実行してよいのは、人が完成動画・"
                                   "投稿文・予約時刻を見たうえで、チャットで明示的に OK と言ったときだけ。人が承認したことを"
                                   "スクリプトは技術的に確かめられないので、--note に人の言葉をそのまま残して監査に使う。"
                                   "自分の判断で承認しない。")
    p.add_argument("slug")
    p.add_argument("--by", required=True, help="承認した人の名前")
    p.add_argument("--note", required=True,
                   help="人がチャットに書いた承認メッセージ（そのまま引用）")

    s = sub.add_parser("schedule", help="予約時刻を設定・変更する")
    s.add_argument("slug")
    s.add_argument("--at", required=True, help='"YYYY-MM-DD HH:MM"（ローカル時刻）')

    sub.add_parser("status", help="キューを表示する")

    pd = sub.add_parser("publish-due", help="承認済みで予約時刻を過ぎたものを公開する")
    pd.add_argument("--dry-run", action="store_true", help="公開予定のものを表示するだけ（API は呼ばない）")
    pd.add_argument("--max", type=int, default=3, help="1回で投稿する最大件数（既定 3）")

    m = sub.add_parser("mark-published", help="手動で公開した投稿を記録する")
    m.add_argument("slug")
    m.add_argument("--post-id", required=True)
    m.add_argument("--permalink")

    sub.add_parser("refresh-token", help="Threads の長期トークンを延長し、設定ファイルに保存する")

    a = ap.parse_args(argv)
    ws = Path(a.workspace).expanduser() if a.workspace else common.workspace()
    qpath = queue_path(ws)
    try:
        if a.cmd == "status":
            return cmd_status(a, qpath)
        if a.cmd == "publish-due" and a.dry_run:
            s = publish_due(qpath, True, a.max)
            print(f"[dry-run] 公開対象={s['due']} 不正={s['invalid']}（API は呼ばず、書き込みもしない）")
            return 0
        with api.FileLock(lock_path(ws)):
            if a.cmd == "publish-due":
                s = publish_due(qpath, False, a.max)
                print(f"完了: 公開={len(s['published'])} 失敗={len(s['failed'])} 結果不明={len(s['unknown'])}")
                return 1 if s["unknown"] else 0
            return {"draft": cmd_draft, "approve": cmd_approve, "schedule": cmd_schedule,
                    "mark-published": cmd_mark_published, "refresh-token": cmd_refresh_token}[a.cmd](a, qpath)
    except api.LockHeld as e:
        print(f"スキップ: {e}", file=sys.stderr)
        return 0
    except RuntimeError as e:
        print(f"エラー: {api.redact(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
