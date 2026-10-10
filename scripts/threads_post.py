#!/usr/bin/env python3
"""Posting queue and scheduled publishing for Threads.

Queue: <workspace>/posts.json (a JSON array, written atomically, guarded by a lock file).

APPROVAL GATE (most important)
  `approve` must only be run by Claude when a human has explicitly said OK in the chat after seeing the
  final video and the post text. Never approve on your own judgement, never to "get past" a check.
  This script never sets status=approved by itself; only the `approve` subcommand does.
  Publishing cannot be undone (Threads has no edit and no unsend).

Status values: draft -> approved -> publishing -> published   (or failed)

publish-due publishes only items that satisfy ALL of:
  status == approved, approved_by non-empty, scheduled_at <= now, no published_id,
  and the text / video file are unchanged since approval.
Double-post protection:
  - an item with published_id is never sent again;
  - the item is written as status=publishing (and read back) BEFORE any API call;
  - an item left in `publishing` (crash, unknown outcome) is never retried automatically: a human checks
    Threads, then runs `mark-published` or re-drafts it;
  - a lock file prevents two runs at the same time.

Subcommands:
  draft --project <dir> --text-file post.txt [--topic AI活用] [--at "YYYY-MM-DD HH:MM"] [--hook ..] [--format ..]
  approve <slug> --by <name>          (human OK in chat only)
  schedule <slug> --at "YYYY-MM-DD HH:MM"
  status
  publish-due [--dry-run] [--max 3]
  mark-published <slug> --post-id <id> [--permalink URL]   (record a post made by hand)
  refresh-token                       (extend the long-lived token, saved to the config file)
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
# queue
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
        raise RuntimeError(f"{path} is not valid JSON ({e}); fix it by hand before continuing") from None
    if not isinstance(data, list):
        raise RuntimeError(f"{path} must be a JSON array")
    return data


def save_queue(path: Path, items: list[dict]) -> None:
    api.atomic_write_json(path, items)
    if load_queue(path) != items:
        raise RuntimeError(f"readback mismatch after writing {path}")


def find(items: list[dict], slug: str) -> dict | None:
    return next((i for i in items if i.get("slug") == slug), None)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_text(text: str) -> list[str]:
    errs = []
    if not text.strip():
        errs.append("post text is empty")
    if len(text) > MAX_TEXT:
        errs.append(f"post text is {len(text)} characters (max {MAX_TEXT})")
    n = len(URL_RE.findall(text))
    if n > MAX_LINKS:
        errs.append(f"post text has {n} links (max {MAX_LINKS})")
    return errs


def validate_topic(topic: str) -> list[str]:
    if not topic:
        return []
    errs = []
    if len(topic) > 50:
        errs.append("topic must be 50 characters or fewer")
    if "." in topic or "&" in topic:
        errs.append("topic must not contain '.' or '&'")
    return errs


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(item: dict) -> str:
    """Hash of exactly what the human approved: text, topic, scheduled time, and the media
    (media_url string if given, else the SHA-256 of the video file's contents)."""
    h = hashlib.sha256()
    for k in ("text", "topic", "scheduled_at", "media_url"):
        h.update(k.encode() + b"=" + str(item.get(k, "")).encode() + b"\0")
    if not str(item.get("media_url", "")).strip():
        vf = Path(str(item.get("video_file", ""))).expanduser()
        h.update(b"video=" + (file_sha256(vf).encode() if vf.is_file() else b"missing"))
    return h.hexdigest()


def eligible(item: dict, now) -> tuple[bool, str]:
    """Is this item due for automatic publishing? Order matters: published_id is checked first."""
    if str(item.get("published_id", "")).strip():
        return False, "already published (published_id set)"
    st = item.get("status", "")
    if st == PUBLISHING:
        return False, "left in 'publishing' (outcome unknown) - check Threads by hand, never auto-retried"
    if st != APPROVED:
        return False, f"status={st or 'empty'}"
    if not str(item.get("approved_by", "")).strip():
        return False, "approved_by is empty (no human approval)"
    t = api.parse_time(item.get("scheduled_at", ""))
    if t is None:
        return False, "scheduled_at is empty or invalid"
    if t > now:
        return False, f"scheduled for {api.iso(t)}"
    return True, "ok"


def check_before_publish(item: dict) -> str | None:
    """Content checks right before publishing. Returns an error message or None."""
    errs = validate_text(item.get("text", "")) + validate_topic(item.get("topic", ""))
    if errs:
        return "; ".join(errs)
    if item.get("status") == APPROVED and item.get("approved_hash") != content_hash(item):
        return "text, topic, time or video changed after approval - re-approve before publishing"
    url = str(item.get("media_url", "")).strip()
    if url:
        if not url.startswith("https://"):
            return "media_url must be an https URL"
        return None
    vf = item.get("video_file", "")
    if not vf or not Path(vf).expanduser().is_file():
        return f"video file not found: {vf or '(empty)'}"
    return None


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

def media_url_for(item: dict, stage=None) -> str:
    url = str(item.get("media_url", "")).strip()
    if url:
        return url
    if stage is None:
        import media_host
        stage = media_host.stage
    return stage(str(Path(item["video_file"]).expanduser()))


def publish_one(g, uid: str, item: dict, stage=None, sleep=time.sleep, clock=time.monotonic,
                on_phase=None) -> dict:
    """Create a VIDEO container -> wait for FINISHED (max 10 min) -> threads_publish.

    on_phase("published_call") is invoked right before the publish call, so the caller knows that from
    then on the outcome is ambiguous on failure. Returns {"published_id", "permalink", "published_at"}.
    """
    params = {"media_type": "VIDEO", "video_url": media_url_for(item, stage), "text": item.get("text", "")}
    if item.get("topic"):
        params["topic_tag"] = item["topic"]
    cid = str(g.post(f"{api.check_id(uid)}/threads", **params).get("id", "")).strip()
    if not cid:
        raise RuntimeError("no container id returned")
    cid = api.check_id(cid)
    start = clock()
    while True:
        st = g.get(cid, fields="status,error_message")
        s = st.get("status")
        if s == "FINISHED":
            break
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {cid} {s}: {st.get('error_message', '')}")
        if clock() - start > POLL_TIMEOUT_S:
            raise RuntimeError(f"container {cid} not FINISHED within {POLL_TIMEOUT_S}s (status={s})")
        sleep(POLL_INTERVAL_S)
    if on_phase:
        on_phase("publish_call")
    pid = str(g.post(f"{uid}/threads_publish", creation_id=cid).get("id", "")).strip()
    if not pid:
        raise RuntimeError(f"container {cid} published but no post id returned - check Threads")
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
    """graph / uid / stage / sleep / clock / now are injection points for tests."""
    now = now or api.now()
    items = load_queue(qpath)
    targets, invalid = [], []
    for it in items:
        ok, why = eligible(it, now)
        if not ok:
            if it.get("status") in (APPROVED, PUBLISHING):
                print(f"skip {it.get('slug')}: {why}")
            continue
        err = check_before_publish(it)
        (invalid if err else targets).append((it, err))
        print(api.redact(f"{'[dry-run] ' if dry_run else ''}{'cannot publish' if err else 'due'} "
                         f"{it.get('slug')}: {err or 'ok'}"))
    summary = {"due": len(targets), "invalid": len(invalid), "published": [], "failed": [], "unknown": []}
    if dry_run:
        return summary
    for it, err in invalid:
        it.update(status=FAILED, error=api.redact(err), updated_at=api.iso(api.now()))
        summary["failed"].append({"slug": it["slug"], "error": err})
    if invalid:
        save_queue(qpath, items)
    if not targets:
        return summary
    if graph is None:
        graph = api.Graph(common.require("threads_access_token"))
    uid = api.user_id(graph, uid or common.get("threads_user_id"))

    for it, _ in targets[:max_n]:
        slug = it["slug"]
        # re-read and re-check right before publishing (concurrent edits)
        items = load_queue(qpath)
        cur = find(items, slug)
        if cur is None or not eligible(cur, now)[0] or check_before_publish(cur):
            print(f"skip {slug}: changed on re-read")
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
                # outcome unknown: leave it in `publishing` so it is never retried automatically
                cur.update(error=f"outcome unknown, check Threads by hand: {msg}", updated_at=api.iso(api.now()))
                summary["unknown"].append({"slug": slug, "error": msg})
            else:
                cur.update(status=FAILED, error=msg, updated_at=api.iso(api.now()))
                summary["failed"].append({"slug": slug, "error": msg})
            save_queue(qpath, items)
            print(f"failed {slug}: {msg}")
            continue
        items = load_queue(qpath)
        cur = find(items, slug)
        cur.update(status=PUBLISHED, error="", updated_at=api.iso(api.now()), **res)
        save_queue(qpath, items)
        summary["published"].append({"slug": slug, **res})
        print(f"published {slug}: {res['published_id']} {res['permalink']}")
    return summary


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def resolve_project(p: str) -> Path:
    cand = Path(p).expanduser()
    if not cand.is_absolute() and not cand.exists():
        cand = common.workspace() / p
    cand = cand.resolve()
    if not cand.is_dir():
        sys.exit(f"project folder not found: {p}")
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
        sys.exit(f"invalid time: {s!r} (use \"YYYY-MM-DD HH:MM\")")
    return api.iso(t)


def cta_link(cta_url: str, slug: str) -> str:
    """cta_url with t=<slug> added (keeps existing query parameters, replaces an existing t)."""
    u = urllib.parse.urlsplit(cta_url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "t"]
    q.append(("t", slug))
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), u.fragment))


def add_cta(text: str, cta_url: str, slug: str) -> str:
    """Append the tracked CTA link unless the text already has a link starting with cta_url."""
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
        sys.exit(f"text file not found: {a.text_file}")
    text = add_cta(tf.read_text(encoding="utf-8").strip(), common.get("cta_url"), slug)
    errs = validate_text(text) + validate_topic(a.topic or "")
    if errs:
        sys.exit("invalid post: " + "; ".join(errs))
    video = project / "final" / f"{slug}-post.mp4"
    if not a.media_url and not video.is_file():
        sys.exit(f"{video} not found - run assemble.py --project {project} --render first")
    items = load_queue(qpath)
    cur = find(items, slug)
    ts = api.iso(api.now())
    if cur and (cur.get("published_id") or cur.get("status") in (PUBLISHED, PUBLISHING)):
        sys.exit(f"{slug} is {cur.get('status')} - it cannot be edited (Threads has no edit). Use a new slug.")
    if cur is None:
        cur = {"slug": slug, "created_at": ts}
        items.append(cur)
    elif cur.get("status") == APPROVED:
        print(f"note: {slug} was approved; editing returns it to draft (approve again after the human OK)")
    cur.update({
        "project": str(project), "video_file": str(video), "media_url": a.media_url or "", "text": text,
        "topic": a.topic if a.topic is not None else cur.get("topic", ""),
        "hook": a.hook if a.hook is not None else cur.get("hook", text.splitlines()[0][:40]),
        "format": a.format if a.format is not None else cur.get("format", project_format(project)),
        "status": DRAFT, "approved_by": "", "approved_at": "", "approved_hash": "", "approval_note": "",
        "error": "", "updated_at": ts,
    })
    if a.at:
        cur["scheduled_at"] = parse_at(a.at)
    cur.setdefault("scheduled_at", "")
    save_queue(qpath, items)
    api.atomic_write_json(project / "post.json", cur)
    print(f"draft saved: {slug} ({len(text)} chars, scheduled_at={cur['scheduled_at'] or 'not set'})")
    print("next: show the video and text to the human; run `approve` only after an explicit OK in chat")
    return 0


def cmd_approve(a, qpath: Path) -> int:
    items = load_queue(qpath)
    cur = find(items, a.slug)
    if cur is None:
        sys.exit(f"no queued post with slug {a.slug}")
    if cur.get("status") != DRAFT:
        sys.exit(f"{a.slug} is {cur.get('status')}; only a draft can be approved")
    if not a.by.strip():
        sys.exit("--by must name the person who approved")
    if not a.note.strip():
        sys.exit("--note must quote the human's approval message from the chat")
    err = check_before_publish({**cur, "approved_hash": ""})
    if err:
        sys.exit(f"cannot approve: {err}")
    cur.update(status=APPROVED, approved_by=a.by.strip(), approved_at=api.iso(api.now()),
               approval_note=api.redact(a.note.strip())[:500], updated_at=api.iso(api.now()))
    cur["approved_hash"] = content_hash(cur)
    save_queue(qpath, items)
    print(f"approved: {a.slug} by {cur['approved_by']} (scheduled_at={cur.get('scheduled_at') or 'not set'})")
    return 0


def cmd_schedule(a, qpath: Path) -> int:
    items = load_queue(qpath)
    cur = find(items, a.slug)
    if cur is None:
        sys.exit(f"no queued post with slug {a.slug}")
    if cur.get("published_id") or cur.get("status") in (PUBLISHED, PUBLISHING):
        sys.exit(f"{a.slug} is {cur.get('status')}; cannot reschedule")
    new_at = parse_at(a.at)
    changed = new_at != cur.get("scheduled_at")
    cur.update(scheduled_at=new_at, updated_at=api.iso(api.now()))
    if changed and cur.get("status") == APPROVED:
        cur.update(status=DRAFT, approved_by="", approved_at="", approved_hash="", approval_note="")
        print(f"note: {a.slug} was approved; changing the time returned it to draft - "
              f"it needs a new human OK and `approve` again")
    save_queue(qpath, items)
    print(f"scheduled: {a.slug} at {cur['scheduled_at']} (status={cur.get('status')})")
    return 0


def cmd_status(a, qpath: Path) -> int:
    items = load_queue(qpath)
    if not items:
        print(f"queue is empty ({qpath})")
        return 0
    now = api.now()
    for it in items:
        ok, why = eligible(it, now)
        extra = it.get("permalink") or it.get("error") or ("due now" if ok else why)
        print(api.redact(f"{it.get('slug', '?'):<24} {it.get('status', ''):<10} at={it.get('scheduled_at') or '-':<25} "
                         f"by={it.get('approved_by') or '-':<10} {extra}"))
        if it.get("approval_note"):
            print(f"{'':<24} approval note: \"{api.redact(it['approval_note'])}\"")
    return 0


def cmd_mark_published(a, qpath: Path) -> int:
    items = load_queue(qpath)
    cur = find(items, a.slug)
    if cur is None:
        sys.exit(f"no queued post with slug {a.slug}")
    api.check_id(a.post_id)
    if cur.get("published_id") and cur["published_id"] != a.post_id:
        sys.exit(f"{a.slug} already has published_id {cur['published_id']}")
    cur.update(status=PUBLISHED, published_id=a.post_id, permalink=a.permalink or cur.get("permalink", ""),
               published_at=cur.get("published_at") or api.iso(api.now()), error="",
               updated_at=api.iso(api.now()), published_by="manual")
    save_queue(qpath, items)
    print(f"recorded as published: {a.slug} -> {a.post_id}")
    return 0


def cmd_refresh_token(a, qpath: Path) -> int:
    token = common.require("threads_access_token")
    res = api.refresh_long_lived(token)
    data = common.load_file()
    data["threads_access_token"] = res["access_token"]
    if res["expires_in"]:
        data["threads_token_expires_at"] = api.iso(api.now() + dt.timedelta(seconds=res["expires_in"]))
    common.save_file(data)
    if common.load_file().get("threads_access_token") != res["access_token"]:
        sys.exit("token refresh: readback of the config file failed")
    print(f"token extended ({res['expires_in'] // 86400 if res['expires_in'] else '?'} days), "
          f"saved to {common.CONFIG_FILE}")
    if common._plugin_option("threads_access_token") or any(
            os.environ.get(e) for e in common.SETTINGS["threads_access_token"][0]):
        print("warning: the token is also set in the plugin settings or an environment variable, which take "
              "precedence over the config file - update it there too (python3 scripts/setup.py set ... "
              "does not override them)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Threads posting queue (<workspace>/posts.json) and scheduled publishing.",
        epilog="APPROVAL: `approve` is an operating rule, not a technical check - run it only after a human has "
               "explicitly said OK in the chat after seeing the final video, text and time (the script cannot tell "
               "who is typing). Publishing cannot be undone.")
    ap.add_argument("--workspace", help="override the workspace folder (default: settings)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft", help="add or update a draft from a project's final/<slug>-post.mp4")
    d.add_argument("--project", required=True)
    d.add_argument("--text-file", required=True, help="post text file (max 500 chars, max 5 links)")
    d.add_argument("--topic", help="topic tag, e.g. AI活用 (sent as topic_tag)")
    d.add_argument("--at", help='schedule time "YYYY-MM-DD HH:MM" (local time)')
    d.add_argument("--media-url", help="use this https URL instead of staging the video file")
    d.add_argument("--hook", help="hook label for the review (default: first line of the text)")
    d.add_argument("--format", help="format label for the review (default: shots.json type)")

    p = sub.add_parser("approve", help="mark a draft approved - ONLY after an explicit human OK in chat",
                       description="Mark a draft as approved. OPERATING RULE: Claude runs this ONLY after a human "
                                   "has explicitly said OK in the chat, having seen the final video, the post text "
                                   "and the scheduled time. The script cannot technically verify that a human "
                                   "approved; --note records the human's own words for audit. Never approve on "
                                   "your own judgement.")
    p.add_argument("slug")
    p.add_argument("--by", required=True, help="name of the human who approved")
    p.add_argument("--note", required=True,
                   help="the approval message the human wrote in the chat, quoted verbatim")

    s = sub.add_parser("schedule", help="set or change the scheduled time")
    s.add_argument("slug")
    s.add_argument("--at", required=True, help='"YYYY-MM-DD HH:MM" (local time)')

    sub.add_parser("status", help="show the queue")

    pd = sub.add_parser("publish-due", help="publish approved items whose time has come")
    pd.add_argument("--dry-run", action="store_true", help="only show what would be published (no API calls)")
    pd.add_argument("--max", type=int, default=3, help="max posts per run (default 3)")

    m = sub.add_parser("mark-published", help="record a post that was published by hand")
    m.add_argument("slug")
    m.add_argument("--post-id", required=True)
    m.add_argument("--permalink")

    sub.add_parser("refresh-token", help="extend the long-lived Threads token and save it to the config file")

    a = ap.parse_args(argv)
    ws = Path(a.workspace).expanduser() if a.workspace else common.workspace()
    qpath = queue_path(ws)
    try:
        if a.cmd == "status":
            return cmd_status(a, qpath)
        if a.cmd == "publish-due" and a.dry_run:
            s = publish_due(qpath, True, a.max)
            print(f"[dry-run] due={s['due']} invalid={s['invalid']} (no API calls, no writes)")
            return 0
        with api.FileLock(lock_path(ws)):
            if a.cmd == "publish-due":
                s = publish_due(qpath, False, a.max)
                print(f"ok: published={len(s['published'])} failed={len(s['failed'])} unknown={len(s['unknown'])}")
                return 1 if s["unknown"] else 0
            return {"draft": cmd_draft, "approve": cmd_approve, "schedule": cmd_schedule,
                    "mark-published": cmd_mark_published, "refresh-token": cmd_refresh_token}[a.cmd](a, qpath)
    except api.LockHeld as e:
        print(f"skip: {e}", file=sys.stderr)
        return 0
    except RuntimeError as e:
        print(f"error: {api.redact(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
