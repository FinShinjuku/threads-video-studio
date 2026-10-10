"""仕上げと投稿のオフラインテスト: assemble（字幕・BGM の音量下げ）、threads_post、threads_insights。

ネットワーク・API・ffmpeg は使わない。実行: python3 -m unittest discover -s scripts/tests
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("TVS_CONFIG_DIR", tempfile.mkdtemp())  # 本物の設定には触れない

import assemble  # noqa: E402
import threads_api as api  # noqa: E402
import threads_insights as ins  # noqa: E402
import threads_post as tp  # noqa: E402

NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


class FakeGraph:
    """呼び出しを記録する。VIDEO 投稿1件分について Threads Graph API のように振る舞う。"""

    def __init__(self, fail_at: str | None = None, statuses=("IN_PROGRESS", "FINISHED")):
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_at = fail_at
        self.statuses = list(statuses)

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path == "me":
            return {"id": "42", "username": "someone"}
        if path.endswith("/insights"):
            return {"data": [{"name": "views", "values": [{"value": 120}]},
                             {"name": "likes", "total_value": {"value": 7}}]}
        if path == "1001":
            if self.fail_at == "container_error":
                return {"status": "ERROR", "error_message": "bad video"}
            return {"status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}
        if path == "2002":
            return {"id": "2002", "permalink": "https://www.threads.net/@x/post/abc", "timestamp": "2026-10-10T03:00:00+0000"}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, **params):
        self.calls.append(("POST", path, params))
        if path.endswith("/threads"):
            return {"id": "1001"}
        if path.endswith("/threads_publish"):
            if self.fail_at == "publish":
                raise RuntimeError("network error during publish")
            return {"id": "2002"}
        raise AssertionError(f"unexpected POST {path}")

    def posted(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def item(**kw):
    base = {"slug": "t001-a", "text": "hello", "topic": "AI活用", "media_url": "https://example.com/v.mp4",
            "video_file": "", "status": "approved", "approved_by": "Reviewer", "approved_at": "x",
            "scheduled_at": "2026-10-10 11:00", "published_id": ""}
    base.update(kw)
    if base["status"] == "approved" and "approved_hash" not in kw:
        base["approved_hash"] = tp.content_hash(base)
    return base


class EligibleTest(unittest.TestCase):
    def test_due_item(self):
        self.assertEqual(tp.eligible(item(), NOW), (True, "ok"))

    def test_not_due(self):
        self.assertFalse(tp.eligible(item(scheduled_at="2026-10-10 13:00"), NOW)[0])
        self.assertFalse(tp.eligible(item(scheduled_at=""), NOW)[0])

    def test_needs_human_approval(self):
        self.assertFalse(tp.eligible(item(approved_by=""), NOW)[0])
        self.assertFalse(tp.eligible(item(status="draft"), NOW)[0])

    def test_published_or_publishing_never_due(self):
        self.assertFalse(tp.eligible(item(published_id="3003"), NOW)[0])
        ok, why = tp.eligible(item(status="publishing"), NOW)
        self.assertFalse(ok)
        self.assertIn("publishing", why)


class PublishDueTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.q = self.ws / "posts.json"

    def run_due(self, items, graph, dry=False):
        api.atomic_write_json(self.q, items)
        return tp.publish_due(self.q, dry, 3, graph=graph, uid="42", sleep=lambda s: None, now=NOW)

    def test_publishes_and_writes_back(self):
        g = FakeGraph()
        s = self.run_due([item()], g)
        self.assertEqual(len(s["published"]), 1)
        create = g.posted("/threads")[0][2]
        self.assertEqual(create["media_type"], "VIDEO")
        self.assertEqual(create["topic_tag"], "AI活用")
        self.assertEqual(create["video_url"], "https://example.com/v.mp4")
        back = tp.load_queue(self.q)[0]
        self.assertEqual(back["status"], "published")
        self.assertEqual(back["published_id"], "2002")
        self.assertTrue(back["permalink"].startswith("https://"))
        # 2回目の実行: 何も送り直さない
        g2 = FakeGraph()
        tp.publish_due(self.q, False, 3, graph=g2, uid="42", sleep=lambda s: None, now=NOW)
        self.assertEqual(g2.calls, [])

    def test_publishing_item_is_not_retried(self):
        g = FakeGraph()
        s = self.run_due([item(status="publishing")], g)
        self.assertEqual(g.calls, [])
        self.assertEqual(s["due"], 0)
        self.assertEqual(tp.load_queue(self.q)[0]["status"], "publishing")

    def test_published_id_is_never_sent(self):
        g = FakeGraph()
        self.run_due([item(published_id="3003")], g)
        self.assertEqual(g.calls, [])

    def test_dry_run_makes_no_calls_or_writes(self):
        g = FakeGraph()
        s = self.run_due([item()], g, dry=True)
        self.assertEqual(s["due"], 1)
        self.assertEqual(g.calls, [])
        self.assertEqual(tp.load_queue(self.q)[0]["status"], "approved")

    def test_marked_publishing_before_api(self):
        seen = {}

        class Spy(FakeGraph):
            def post(inner, path, **params):
                if path.endswith("/threads"):
                    seen["status"] = tp.load_queue(self.q)[0]["status"]
                return super().post(path, **params)

        self.run_due([item()], Spy())
        self.assertEqual(seen["status"], "publishing")

    def test_unknown_outcome_stays_publishing(self):
        s = self.run_due([item()], FakeGraph(fail_at="publish"))
        self.assertEqual(len(s["unknown"]), 1)
        back = tp.load_queue(self.q)[0]
        self.assertEqual(back["status"], "publishing")
        self.assertIn("結果不明", back["error"])

    def test_container_error_marks_failed(self):
        s = self.run_due([item()], FakeGraph(fail_at="container_error"))
        self.assertEqual(len(s["failed"]), 1)
        self.assertEqual(tp.load_queue(self.q)[0]["status"], "failed")

    def test_changed_after_approval_is_blocked(self):
        it = item()
        it["approved_hash"] = tp.content_hash(it)
        it["text"] = "edited after approval"
        g = FakeGraph()
        s = self.run_due([it], g)
        self.assertEqual(g.calls, [])
        self.assertEqual(s["invalid"], 1)
        self.assertEqual(tp.load_queue(self.q)[0]["status"], "failed")

    def test_max_per_run(self):
        g = FakeGraph()
        items = [item(slug=f"s{i}") for i in range(5)]
        api.atomic_write_json(self.q, items)
        s = tp.publish_due(self.q, False, 2, graph=g, uid="42", sleep=lambda s: None, now=NOW)
        self.assertEqual(len(s["published"]), 2)


class TextValidationTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(tp.validate_text("こんにちは https://example.com"), [])

    def test_too_long(self):
        self.assertTrue(any("上限 500" in e for e in tp.validate_text("あ" * 501)))
        self.assertEqual(tp.validate_text("あ" * 500), [])

    def test_too_many_links(self):
        text = " ".join(f"https://example.com/{i}" for i in range(6))
        self.assertTrue(any("リンク" in e for e in tp.validate_text(text)))

    def test_empty_and_topic(self):
        self.assertTrue(tp.validate_text("  "))
        self.assertTrue(tp.validate_topic("a.b"))
        self.assertEqual(tp.validate_topic("AI活用"), [])


class DraftApproveTest(unittest.TestCase):
    def test_draft_approve_redraft(self):
        ws = Path(tempfile.mkdtemp())
        proj = ws / "t001-a"
        (proj / "final").mkdir(parents=True)
        (proj / "shots.json").write_text(json.dumps({"slug": "t001-a", "type": "story", "cuts": []}))
        (proj / "final" / "t001-a-post.mp4").write_bytes(b"fake")
        (proj / "post.txt").write_text("一行目のフック\n本文", encoding="utf-8")
        args = ["--workspace", str(ws)]
        self.assertEqual(tp.main(args + ["draft", "--project", str(proj), "--text-file", "post.txt",
                                         "--at", "2026-10-11 09:00"]), 0)
        q = tp.load_queue(ws / "posts.json")
        self.assertEqual(q[0]["status"], "draft")
        self.assertEqual(q[0]["hook"], "一行目のフック")
        self.assertEqual(q[0]["format"], "story")
        with self.assertRaises(SystemExit):  # --note は必須
            tp.main(args + ["approve", "t001-a", "--by", "Reviewer"])
        self.assertEqual(tp.main(args + ["approve", "t001-a", "--by", "Reviewer", "--note", "OK、これで投稿して"]), 0)
        q = tp.load_queue(ws / "posts.json")
        self.assertEqual(q[0]["status"], "approved")
        self.assertEqual(q[0]["approval_note"], "OK、これで投稿して")
        # 時刻を変えると承認が外れる
        self.assertEqual(tp.main(args + ["schedule", "t001-a", "--at", "2026-10-12 09:00"]), 0)
        self.assertEqual(tp.load_queue(ws / "posts.json")[0]["status"], "draft")
        self.assertEqual(tp.main(args + ["approve", "t001-a", "--by", "Reviewer", "--note", "OK"]), 0)
        # 承認済みを編集すると下書きに戻り、承認が消える
        self.assertEqual(tp.main(args + ["draft", "--project", str(proj), "--text-file", "post.txt"]), 0)
        q = tp.load_queue(ws / "posts.json")
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["status"], "draft")
        self.assertEqual(q[0]["approved_by"], "")
        self.assertEqual(q[0]["scheduled_at"][:16], "2026-10-12T09:00")

    def test_draft_rejects_long_text(self):
        ws = Path(tempfile.mkdtemp())
        proj = ws / "p"
        (proj / "final").mkdir(parents=True)
        (proj / "final" / "p-post.mp4").write_bytes(b"x")
        (proj / "post.txt").write_text("あ" * 501, encoding="utf-8")
        with self.assertRaises(SystemExit):
            tp.main(["--workspace", str(ws), "draft", "--project", str(proj), "--text-file", "post.txt"])


class LockTest(unittest.TestCase):
    def test_second_lock_is_refused(self):
        p = Path(tempfile.mkdtemp()) / ".lock"
        with api.FileLock(p):
            with self.assertRaises(api.LockHeld):
                with api.FileLock(p):
                    pass
        with api.FileLock(p):  # 抜けたあとは外れている
            pass


class CaptionTest(unittest.TestCase):
    def test_short_line_stays_one_line(self):
        self.assertEqual(assemble.split_caption("友達5人で、海辺のキャンプへ！"), ["友達5人で、海辺のキャンプへ！"])

    def test_two_lines_break_at_punctuation(self):
        self.assertEqual(assemble.split_caption("あなたの「やってみたい」、LINEで教えて！"),
                         ["あなたの「やってみたい」、", "LINEで教えて！"])

    def test_long_line_wraps_and_cards_hold_two_lines(self):
        lines = assemble.split_caption("現場のこだわり、動画に撮るだけで、マニュアルに変身！")
        self.assertEqual(lines, ["現場のこだわり、", "動画に撮るだけで、", "マニュアルに変身！"])
        cards = assemble.caption_cards({"line": "現場のこだわり、動画に撮るだけで、マニュアルに変身！"})
        self.assertEqual([len(c) for c in cards], [2, 1])
        for ln in lines:
            self.assertLessEqual(assemble.visible_width(ln), 16)

    def test_no_break_inside_highlight_or_word(self):
        for ln in assemble.split_caption("[[海辺のキャンプ]]で仲間がAIを毎週末に使って料理を楽しんでいる話"):
            self.assertEqual(ln.count("[["), ln.count("]]"))
        self.assertNotIn("A", "".join(x[-1] for x in assemble.split_caption("あいうえおかきくけこさしすせそAIたちつてと")[:-1]))

    def test_manual_caption_wins(self):
        cards = assemble.caption_cards({"line": "ignored", "caption": ["友達5人で、", "[[海辺のキャンプ]]へ！"]})
        self.assertEqual(cards, [["友達5人で、", "[[海辺のキャンプ]]へ！"]])

    def test_markup(self):
        self.assertEqual(assemble.markup("[[海辺のキャンプ]]へ！"), '<span class="em">海辺のキャンプ</span>へ！')
        self.assertEqual(assemble.markup("……みんな<b>"), '<span class="ell">……</span>みんな&lt;b&gt;')


class DuckingTest(unittest.TestCase):
    def test_merge_short_gaps(self):
        lines = [{"start": 0.2, "end": 1.0}, {"start": 1.5, "end": 2.0}, {"start": 3.0, "end": 4.0}]
        wins = assemble.duck_windows(lines)
        self.assertEqual(len(wins), 2)
        self.assertAlmostEqual(wins[0][0], 0.1)
        self.assertAlmostEqual(wins[0][1], 2.1)
        self.assertAlmostEqual(wins[1][0], 2.9)

    def test_expression_levels(self):
        expr = assemble.duck_expr([[1.0, 2.0]], 5.0)
        self.assertIn("-20.0", expr)
        f = eval("lambda t: " + expr.replace("if(", "_if(").replace("between(", "_between("),  # noqa: S307
                 {"_if": lambda c, a, b: a if c else b, "_between": lambda t, lo, hi: lo <= t <= hi})
        self.assertAlmostEqual(f(0.5), 0.0)
        self.assertAlmostEqual(f(1.5), -20.0)
        self.assertAlmostEqual(f(4.0), 0.0)


class LearningsTest(unittest.TestCase):
    def test_verdict(self):
        self.assertEqual(ins.verdict(500, [100, 100]), ins.TOO_FEW)
        self.assertEqual(ins.verdict(150, [100, 100, 100]), ins.GREW)
        self.assertEqual(ins.verdict(67, [100, 100, 100]), ins.DROPPED)
        self.assertEqual(ins.verdict(100, [90, 100, 110]), ins.FLAT)

    def test_build_learnings_checkpoints(self):
        t0 = NOW - dt.timedelta(days=8)
        posts = [{"slug": "s1", "published_id": "2002", "_t": t0, "hook": "h", "format": "story"}]
        snaps = ins.snapshots([
            {"fetched_at": api.iso(t0 + dt.timedelta(hours=10)), "post_id": "2002", "views": "10"},
            {"fetched_at": api.iso(t0 + dt.timedelta(hours=50)), "post_id": "2002", "views": "300", "likes": "30"},
            {"fetched_at": api.iso(t0 + dt.timedelta(days=7, hours=1)), "post_id": "2002", "views": "900"},
        ])
        existing = [{"slug": f"old{i}", "checkpoint": "48h", "views": "100",
                     "posted_at": api.iso(t0 - dt.timedelta(days=i + 1))} for i in range(3)]
        rows = ins.build_learnings(posts, snaps, existing, NOW)
        by = {r["checkpoint"]: r for r in rows}
        self.assertEqual(by["48h"]["views"], 300)
        self.assertEqual(by["48h"]["verdict"], ins.GREW)
        self.assertEqual(by["48h"]["insight"], "")
        self.assertEqual(by["7d"]["verdict"], ins.TOO_FEW)
        # 振り返り済みの行は作り直さない
        again = ins.build_learnings(posts, snaps, existing + rows, NOW)
        self.assertEqual(again, [])

    def test_pull_and_review_files(self):
        ws = Path(tempfile.mkdtemp())
        pub = api.iso(NOW - dt.timedelta(hours=60))
        api.atomic_write_json(ws / "posts.json", [item(status="published", published_id="2002", published_at=pub,
                                                       hook="h", format="story")])
        g = FakeGraph()
        self.assertEqual(ins.pull(ws, 30, graph=g, now=NOW)["pulled"], 1)
        rows = ins.read_csv(ws / "metrics.csv")
        self.assertEqual(rows[0]["views"], "120")
        self.assertEqual(rows[0]["likes"], "7")
        ins.review(ws, now=NOW)
        learn = ins.read_csv(ws / "learnings.csv")
        self.assertEqual([(r["slug"], r["checkpoint"], r["verdict"]) for r in learn], [("t001-a", "48h", ins.TOO_FEW)])
        self.assertEqual(list(learn[0].keys())[:4], ["slug", "checkpoint", "hook", "format"])
        self.assertTrue((ws / "reports" / f"{NOW.date().isoformat()}.md").exists())


class ApprovalHashTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.video = self.d / "v.mp4"
        self.video.write_bytes(b"original video")

    def approved(self, **kw):
        return item(media_url="", video_file=str(self.video), **kw)

    def test_unchanged_is_ok(self):
        self.assertIsNone(tp.check_before_publish(self.approved()))

    def test_any_change_blocks(self):
        for field, value in (("text", "changed"), ("topic", "別トピック"), ("scheduled_at", "2026-10-10 10:00")):
            it = self.approved()
            it[field] = value
            self.assertIn("承認後に", tp.check_before_publish(it), field)

    def test_video_content_change_blocks(self):
        it = self.approved()
        self.video.write_bytes(b"replaced video")  # 同じパスで中身だけ違う
        self.assertIn("承認後に", tp.check_before_publish(it))

    def test_media_url_change_blocks(self):
        it = item()
        it["media_url"] = "https://example.com/other.mp4"
        self.assertIn("承認後に", tp.check_before_publish(it))


class CtaLinkTest(unittest.TestCase):
    def test_appends_tracked_link(self):
        self.assertEqual(tp.add_cta("本文", "https://lin.ee/abc", "t001-a"), "本文\nhttps://lin.ee/abc?t=t001-a")

    def test_keeps_existing_query(self):
        self.assertEqual(tp.cta_link("https://example.com/r?src=th&t=old", "t002"),
                         "https://example.com/r?src=th&t=t002")

    def test_no_duplicate(self):
        text = "本文 https://lin.ee/abc?t=x"
        self.assertEqual(tp.add_cta(text, "https://lin.ee/abc", "t001-a"), text)
        self.assertEqual(tp.add_cta("本文", "", "t001-a"), "本文")


class FakeResp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class GraphSafetyTest(unittest.TestCase):
    def test_bearer_header_and_no_token_in_url(self):
        seen = []

        def opener(req, timeout=60):
            seen.append(req)
            return FakeResp({"id": "42"})

        g = api.Graph("SECRET_TOKEN_123", opener=opener)
        g.get("me", fields="id")
        g.post("42/threads", text="x", access_token="sneaky")
        for req in seen:
            self.assertEqual(req.get_header("Authorization"), "Bearer SECRET_TOKEN_123")
            self.assertNotIn("SECRET_TOKEN", req.full_url)
            self.assertNotIn(b"access_token", req.data or b"")
            self.assertTrue(req.full_url.startswith("https://graph.threads.net/v1.0/"))

    def test_refuses_absolute_or_odd_paths(self):
        g = api.Graph("t", opener=lambda *a, **k: self.fail("送ってはいけない"))
        for bad in ("https://evil.example/x", "//evil.example/x", "../me", "abc/threads", "42/threads?x=1",
                    "42/../me"):
            with self.assertRaises(RuntimeError):
                g.get(bad)

    def test_ids_digits_only(self):
        self.assertEqual(api.check_id("123"), "123")
        for bad in ("12a", "", "1/2", "https://x"):
            with self.assertRaises(RuntimeError):
                api.check_id(bad)

    def test_paging_only_on_graph_host(self):
        pages = [{"data": [1], "paging": {"next": "https://evil.example/next?x=1"}}]
        g = api.Graph("t", opener=lambda req, timeout=60: FakeResp(pages.pop(0)))
        it = g.paged("42/threads")
        self.assertEqual(next(it), 1)
        with self.assertRaises(RuntimeError):
            next(it)
        self.assertTrue(api.is_graph_url("https://graph.threads.net/v1.0/42/threads?after=x"))
        self.assertFalse(api.is_graph_url("http://graph.threads.net/v1.0/42"))

    def test_redact(self):
        url = ("https://b.s3.amazonaws.com/threads/x.mp4?X-Amz-Algorithm=AWS4&X-Amz-Credential=AKIA123"
               "&X-Amz-Signature=deadbeef")
        out = api.redact(f"failed {url} Authorization: Bearer abc.def access_token=zzz9")
        for leak in ("AKIA123", "deadbeef", "abc.def", "zzz9"):
            self.assertNotIn(leak, out)
        self.assertIn("https://b.s3.amazonaws.com/threads/x.mp4", out)
        self.assertNotIn("SECRETVAL", api.redact("x SECRETVAL y", "SECRETVAL"))

    def test_failed_publish_error_is_redacted_in_queue(self):
        ws = Path(tempfile.mkdtemp())
        q = ws / "posts.json"
        it = item(media_url="", video_file=str(ws / "v.mp4"))
        (ws / "v.mp4").write_bytes(b"v")
        it["approved_hash"] = tp.content_hash(it)
        api.atomic_write_json(q, [it])

        def stage(path):
            raise RuntimeError("upload failed https://b.s3/x.mp4?X-Amz-Signature=leak123")

        tp.publish_due(q, False, 3, graph=FakeGraph(), uid="42", stage=stage, sleep=lambda s: None, now=NOW)
        self.assertNotIn("leak123", q.read_text())


class LockFallbackTest(unittest.TestCase):
    def test_excl_lock_without_fcntl(self):
        p = Path(tempfile.mkdtemp()) / ".lock"
        with api.FileLock(p, use_fcntl=False):
            self.assertTrue(p.exists())
            with self.assertRaises(api.LockHeld):
                with api.FileLock(p, use_fcntl=False):
                    pass
        self.assertFalse(p.exists())

    def test_stale_lock_is_removed(self):
        p = Path(tempfile.mkdtemp()) / ".lock"
        p.write_text("999999 0")  # 死んだ pid で、とても古い
        with api.FileLock(p, use_fcntl=False):
            pass
        p.write_text(f"{os.getpid()} 0")  # 生きている pid だが、放置とみなす期限より古い
        with api.FileLock(p, use_fcntl=False):
            pass


class PathSafetyTest(unittest.TestCase):
    def test_safe_path(self):
        base = Path(tempfile.mkdtemp())
        self.assertEqual(api.safe_path(base, "clips/c01.mp4"), (base / "clips/c01.mp4").resolve())
        for bad in ("../x.png", "/etc/passwd", "frames/../../x"):
            with self.assertRaises(RuntimeError):
                api.safe_path(base, bad)

    def test_check_slug(self):
        self.assertEqual(api.check_slug("t004-camp"), "t004-camp")
        for bad in ("../x", "a/b", "", ".hidden", "a..b"):
            with self.assertRaises(RuntimeError):
                api.check_slug(bad)

    def test_assemble_refuses_outside_paths(self):
        proj = Path(tempfile.mkdtemp())
        with self.assertRaises(RuntimeError):
            assemble.prep_clip(proj, {"id": "c01", "frame": "../../etc/x.png"}, 1.0, 24, proj / "o.mp4", {})
        with self.assertRaises(RuntimeError):
            assemble.prep_clip(proj, {"id": "../c01"}, 1.0, 24, proj / "o.mp4", {})


class CaptionTimingTest(unittest.TestCase):
    def test_ends_at_line_end_plus_hold(self):
        cuts = [{"id": "c01", "line": "一つ目", "start": 0, "dur": 3},
                {"id": "c02", "line": "二つ目", "start": 3, "dur": 3},
                {"id": "c03", "line": "三つ目", "start": 6, "dur": 3}]
        lines = [{"cut": "c01", "start": 0.3, "end": 1.5}, {"cut": "c02", "start": 3.2, "end": 5.0},
                 {"cut": "c03", "start": 4.95, "end": 8.0}]
        tl = assemble.caption_timeline(cuts, lines, 9.0)
        self.assertEqual([(s, e) for s, e, _, _ in tl], [(0.2, 1.7), (3.1, 4.85), (4.85, 8.2)])

    def test_multiple_cards_share_the_line(self):
        cuts = [{"id": "c01", "caption": ["あ", "い", "う"], "start": 0, "dur": 4}]
        tl = assemble.caption_timeline(cuts, [{"cut": "c01", "start": 1.0, "end": 3.0}], 4.0)
        self.assertEqual(len(tl), 2)
        self.assertEqual(tl[0][0], 0.9)
        self.assertEqual(tl[0][1], tl[1][0])
        self.assertEqual(tl[1][1], 3.2)


class MediaHostOutputTest(unittest.TestCase):
    def test_url_hidden_by_default(self):
        import contextlib
        import io
        import media_host
        f = Path(tempfile.mkdtemp()) / "v.mp4"
        f.write_bytes(b"x")
        orig = media_host.stage_with_location
        media_host.stage_with_location = lambda p, s=3600: ("https://b.s3/x?X-Amz-Signature=abc", "s3://b/k")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                media_host.main([str(f)])
            self.assertNotIn("X-Amz", buf.getvalue())
            self.assertIn("s3://b/k", buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                media_host.main([str(f), "--show-url"])
            self.assertIn("X-Amz-Signature", buf.getvalue())
        finally:
            media_host.stage_with_location = orig


class RunnerTest(unittest.TestCase):
    def test_runner_copy(self):
        import install_scheduler
        d = Path(tempfile.mkdtemp()) / "runner"
        out = install_scheduler.install_runner(d)
        self.assertEqual({p.name for p in out}, set(install_scheduler.RUNNER_FILES))
        self.assertIn(str(install_scheduler.RUNNER_DIR / "threads_post.py"),
                      install_scheduler.render_plist("/usr/bin/python3", 900, 3, Path("/tmp/l.log")))


if __name__ == "__main__":
    unittest.main()
