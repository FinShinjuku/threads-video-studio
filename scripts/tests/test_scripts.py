"""threads-video-studio のスクリプトのオフライン単体テスト（通信も ffmpeg も不要）。

実行: python3 -m unittest discover -s scripts/tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("TVS_CONFIG_DIR", tempfile.mkdtemp())  # 本物の設定には触れない

import common  # noqa: E402
import cuesheet  # noqa: E402
import gen_image  # noqa: E402
import gen_video  # noqa: E402
import gen_voice  # noqa: E402
import new_project  # noqa: E402
import qa_frames  # noqa: E402
import yomi_check  # noqa: E402


def fake_alignment(text: str, step: float = 0.1, pause_on: str = "\n", pause: float = 0.5) -> dict:
    chars, starts, ends, t = [], [], [], 0.0
    for c in text:
        d = pause if c == pause_on else step
        chars.append(c)
        starts.append(round(t, 3))
        t += d
        ends.append(round(t, 3))
    return {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}


class TestVideoRegistry(unittest.TestCase):
    def test_registry_complete(self):
        for name, m in gen_video.MODELS.items():
            with self.subTest(model=name):
                self.assertTrue(m["endpoint"])
                self.assertTrue(m["durations"])
                self.assertEqual(m["durations"], sorted(m["durations"]))
                self.assertIn(m["default_resolution"], m["resolutions"] or [None])
                for r in (m["resolutions"] or [None]):
                    self.assertIn(r, m["usd_per_sec"])
                draft_model, draft_res = m["draft"]
                self.assertIn(draft_model, gen_video.MODELS)
                self.assertIn(draft_res, gen_video.MODELS[draft_model]["resolutions"] or [None])

    def test_resolution_rejected_with_allowed(self):
        with self.assertRaises(ValueError) as cm:
            gen_video.check_resolution("veo31-fast", "480p")
        self.assertIn("使える値: 720p, 1080p, 4k", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            gen_video.check_resolution("seedance20-fast", "1080p")
        self.assertIn("使える値:", str(cm.exception))
        with self.assertRaises(ValueError):
            gen_video.check_resolution("kling30", "720p")
        self.assertIsNone(gen_video.check_resolution("kling30", None))

    def test_duration_round_up(self):
        r = gen_video.round_up_duration
        self.assertEqual(r("seedance20", 3.2), 4)
        self.assertEqual(r("seedance20", 4.0), 4)
        self.assertEqual(r("seedance20", 4.01), 5)
        self.assertEqual(r("seedance20", None), 4)
        self.assertEqual(r("veo31-fast", 4.5), 6)
        self.assertEqual(r("veo31-fast", 6.0), 6)
        self.assertEqual(r("veo31-fast", 7.1), 8)
        self.assertEqual(r("kling30", 2.4), 3)
        with self.assertRaises(ValueError) as cm:
            r("veo31-fast", 8.2)
        self.assertIn("使える値: 4, 6, 8", str(cm.exception))
        with self.assertRaises(ValueError):
            r("seedance20", 15.5)

    def test_draft_selection(self):
        cut = {"id": "c01", "model": "seedance20"}
        self.assertEqual(gen_video.pick_model_and_res(cut, None, None, True), ("seedance20-fast", "480p"))
        self.assertEqual(gen_video.pick_model_and_res(cut, None, None, False), ("seedance20", "720p"))
        self.assertEqual(gen_video.pick_model_and_res(cut, "veo31-fast", None, True), ("veo31-fast", "720p"))
        self.assertEqual(gen_video.pick_model_and_res(cut, "kling30", None, True), ("kling30", None))
        with self.assertRaises(ValueError):
            gen_video.pick_model_and_res(cut, "nope", None, False)

    def test_body_fields(self):
        p = Path("/tmp")
        cut = {"id": "c01", "frame": "frames/c01.png", "end_frame": None, "refs": []}
        b = gen_video.build_body("veo31-fast", cut, p, "walk", 6, "720p", images=False)
        self.assertEqual(b["duration"], "6s")
        self.assertIs(b["generate_audio"], False)
        self.assertEqual(b["aspect_ratio"], "9:16")
        self.assertIn("image_url", b)
        b = gen_video.build_body("kling30", cut, p, "walk", 5, None, images=False)
        self.assertNotIn("resolution", b)
        self.assertNotIn("aspect_ratio", b)
        self.assertIn("start_image_url", b)
        with self.assertRaises(ValueError):
            gen_video.build_body("veo31-fast-flf", cut, p, "walk", 6, "720p", images=False)
        with self.assertRaises(ValueError):  # veo31-fast は終了フレームを使えない
            gen_video.build_body("veo31-fast", dict(cut, end_frame="frames/e.png"), p, "x", 6, "720p", images=False)
        b = gen_video.build_body("seedance20-ref", dict(cut, refs=["a.png", "b.png"]), p, "x", 4, "480p", images=False)
        self.assertEqual(len(b["image_urls"]), 2)

    def test_cost(self):
        self.assertAlmostEqual(gen_video.estimate_cost("veo31-fast", "720p", 6), 0.6)
        self.assertAlmostEqual(gen_video.estimate_cost("kling30", None, 5), 0.56)

    def test_queue_urls(self):
        s, r = gen_video.queue_urls("fal-ai/veo3.1/fast/image-to-video", "abc")
        self.assertEqual(r, "https://queue.fal.run/fal-ai/veo3.1/requests/abc")
        self.assertEqual(s, r + "/status")

    def test_locked_error(self):
        e = common.HttpError(403, "GET", "https://x", '{"detail":"User is locked. Reason: Exhausted balance"}')
        self.assertTrue(gen_video.is_locked_error(e))
        self.assertFalse(gen_video.is_locked_error(common.HttpError(403, "GET", "https://x", "forbidden")))


class TestTaskResume(unittest.TestCase):
    def test_caption_only_cut_needs_no_line(self):
        shots = {"type": "short", "cuts": [
            {"id": "c01", "picture": "畑", "caption": ["朝5時。"], "line": "", "action": "a", "camera": "static",
             "prompt": "p", "start": 0.0, "dur": 3.0},
            {"id": "c02", "picture": "縁側", "caption": [], "line": "", "action": "a", "camera": "static",
             "prompt": "p", "start": 3.0, "dur": 3.0}]}
        issues = "\n".join(cuesheet.check(shots))
        self.assertNotIn("c01: ナレーションが空", issues)  # 字幕だけの短い動画
        self.assertIn("c02: ナレーションが空", issues)

    def test_estimate_without_frames(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            cuts = {"c01": {"id": "c01", "prompt": "p", "model": "kling30", "frame": "frames/c01.png", "dur": 2.5}}
            jobs = gen_video.plan_jobs(proj, cuts, ["c01"], None, None, False, False, estimate_only=True)
            self.assertEqual(jobs[0].seconds, 3)
            with self.assertRaises(FileNotFoundError):  # 生成するときは起点画像が要る
                gen_video.plan_jobs(proj, cuts, ["c01"], None, None, False, False)

    def test_states(self):
        with tempfile.TemporaryDirectory() as d:
            clips = Path(d)
            self.assertEqual(gen_video.task_state(clips, "c01"), "new")
            (clips / "c01.task.locked").write_text("x")
            self.assertEqual(gen_video.task_state(clips, "c01"), "locked")
            (clips / "c01.mp4").write_bytes(b"0")
            self.assertEqual(gen_video.task_state(clips, "c01"), "done")
            self.assertEqual(gen_video.task_state(clips, "c01", force=True), "locked")
            (clips / "c01.task").write_text(json.dumps({"request_id": "r1"}))
            self.assertEqual(gen_video.task_state(clips, "c01"), "resume")

    def test_read_task_formats(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c01.task"
            p.write_text("plain-id\n")
            self.assertEqual(gen_video.read_task(p)["request_id"], "plain-id")
            p.write_text(json.dumps({"request_id": "j1", "endpoint": "x/y/z"}))
            self.assertEqual(gen_video.read_task(p)["endpoint"], "x/y/z")


class TestVoice(unittest.TestCase):
    LINES = ["やっほー！AIで、カタチにしてるんだ！", "40秒でたたき台！", "LINEで教えて！"]

    def test_spans_with_tag(self):
        text = gen_voice.build_text(self.LINES, "[cheerfully]")
        self.assertTrue(text.startswith("[cheerfully] やっほー"))
        al = fake_alignment(text)
        spans = gen_voice.line_spans(al, self.LINES, "[cheerfully]")
        self.assertEqual(len(spans), 3)
        first = text.index("や")
        self.assertAlmostEqual(spans[0][0], al["character_start_times_seconds"][first])
        last = text.index("教えて！") + len("教えて！") - 1
        self.assertAlmostEqual(spans[2][1], al["character_end_times_seconds"][last])
        self.assertTrue(spans[0][1] < spans[1][0] < spans[1][1] < spans[2][0])

    def test_spans_after_yomi(self):
        yomi = {"AI": "エーアイ", "40": "よんじゅう"}
        spoken = [yomi_check.apply_yomi(ln, yomi) for ln in self.LINES]
        al = fake_alignment(gen_voice.build_text(spoken, None))
        spans = gen_voice.line_spans(al, spoken, None)
        self.assertEqual(spans[0][0], 0.0)
        self.assertEqual(len(spans), 3)

    def test_spans_fallback_on_normalised_text(self):
        text = gen_voice.build_text(self.LINES, None)
        al = fake_alignment(text.replace("40", "４０"))  # サービス側で数字が正規化された場合
        spans = gen_voice.line_spans(al, self.LINES, None)
        self.assertEqual(len(spans), 3)
        self.assertEqual(spans[0][0], 0.0)

    def test_cut_timing(self):
        spans = [(0.2, 4.8), (6.0, 11.24), (11.96, 16.28)]
        t = gen_voice.cut_timing(spans)
        self.assertEqual(t[0], (0.0, 5.85))
        self.assertEqual(t[1], (5.85, 5.96))
        self.assertEqual(t[2][0], 11.81)
        self.assertAlmostEqual(t[2][0] + t[2][1], 17.28)


class TestYomi(unittest.TestCase):
    def test_apply_longest_first_no_rereplace(self):
        yomi = {"AI": "エーアイ", "AIOS": "エーアイオーエス", "エー": "XX"}
        self.assertEqual(yomi_check.apply_yomi("AIOSとAI", yomi), "エーアイオーエスとエーアイ")
        self.assertEqual(yomi_check.apply_yomi("abc", {}), "abc")

    def test_number_kana(self):
        nk = yomi_check.number_kana
        self.assertEqual(nk(40), "よんじゅう")
        self.assertEqual(nk(300), "さんびゃく")
        self.assertEqual(nk(1000), "せん")
        self.assertEqual(nk(8000), "はっせん")
        self.assertEqual(nk(10), "じゅう")
        self.assertEqual(nk(20000), "にまん")

    def test_scan_and_suggest(self):
        found = yomi_check.scan_line("AIで40秒、未来を創る人の間")
        kinds = {k for k, _ in found}
        self.assertEqual(kinds, {"latin", "number", "kanji"})
        self.assertIn(("kanji", "創"), found)
        self.assertEqual(yomi_check.suggest_reading("AI"), "エーアイ")
        self.assertEqual(yomi_check.suggest_reading("１０"), "じゅう")
        self.assertIsNone(yomi_check.suggest_reading("Threads"))
        self.assertIsNone(yomi_check.suggest_reading("LINE"))


class TestQa(unittest.TestCase):
    LOG = """
[freezedetect @ 0x6000] lavfi.freezedetect.freeze_start: 1.2
[freezedetect @ 0x6000] lavfi.freezedetect.freeze_duration: 0.8
[freezedetect @ 0x6000] lavfi.freezedetect.freeze_end: 2
frame=  120 fps=0.0 q=-0.0 size=N/A
[freezedetect @ 0x6000] lavfi.freezedetect.freeze_start: 4.5
"""

    def test_parse_freezedetect(self):
        spans = qa_frames.parse_freezedetect(self.LOG, 5.0)
        self.assertEqual(spans[0], {"start": 1.2, "end": 2.0, "dur": 0.8})
        self.assertEqual(spans[1], {"start": 4.5, "end": 5.0, "dur": 0.5})
        self.assertEqual(qa_frames.parse_freezedetect("nothing"), [])

    def test_ppm_and_diff(self):
        a = b"P6\n# c\n2 1\n255\n" + bytes([0, 0, 0, 255, 255, 255])
        w, h, pix = qa_frames.read_ppm(a)
        self.assertEqual((w, h), (2, 1))
        self.assertAlmostEqual(qa_frames.mean_abs_diff(pix, bytes(6)), 0.5)
        self.assertEqual(qa_frames.mean_abs_diff(pix, pix), 0.0)


class TestMisc(unittest.TestCase):
    def test_scrub(self):
        os.environ["FAL_KEY"] = "secret-value-123456"
        try:
            self.assertEqual(common.scrub("bad key secret-value-123456!"), "bad key ***!")
            e = common.HttpError(401, "POST", "https://x", "key secret-value-123456 invalid")
            self.assertNotIn("secret-value", str(e))
        finally:
            del os.environ["FAL_KEY"]

    def test_image_sizes(self):
        self.assertEqual(gen_image.seedream_size(1440, 2560), {"width": 1440, "height": 2560})
        with self.assertRaises(ValueError):
            gen_image.seedream_size(720, 1280)
        self.assertEqual(gen_image.nano_params(1440, 2560), {"aspect_ratio": "9:16", "resolution": "2K"})
        self.assertEqual(gen_image.nano_params(1080, 1920)["aspect_ratio"], "9:16")
        self.assertEqual(gen_image.nano_params(1024, 1024), {"aspect_ratio": "1:1", "resolution": "1K"})

    def test_new_project_shots(self):
        s = new_project.make_shots("t1", "intro", 5, 5.0)
        self.assertEqual([c["id"] for c in s["cuts"]], ["c01", "c02", "c03", "c04", "c05"])
        self.assertEqual(s["cuts"][2]["start"], 10.0)


class TestRunJobOffline(unittest.TestCase):
    """通信をモックした run_job: .task からの再開と、403 'locked' の扱い。"""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.proj = Path(self.d.name)
        (self.proj / "clips").mkdir()
        self.saved = (common.fal_headers, common.http_json, common.media_url_to_file, gen_video.POLL_SEC)
        common.fal_headers = lambda: {}
        gen_video.POLL_SEC = 0

    def tearDown(self):
        common.fal_headers, common.http_json, common.media_url_to_file, gen_video.POLL_SEC = self.saved
        self.d.cleanup()

    def job(self):
        cut = {"id": "c01", "prompt": "p", "frame": "frames/c01.png", "dur": 4}
        return gen_video.Job(self.proj, cut, "seedance20", "720p", False, False)

    def test_resume_completes_and_removes_task(self):
        (self.proj / "clips" / "c01.task").write_text(json.dumps(
            {"request_id": "r1", "endpoint": "bytedance/seedance-2.0/image-to-video"}))
        calls = []

        def fake(method, url, headers, body=None, **kw):
            calls.append((method, url))
            if method == "POST":
                raise AssertionError("再送信してはいけない")
            if url.endswith("/status"):
                if sum(1 for c in calls if c[1].endswith("/status")) == 1:
                    raise common.NetworkError("通信エラー")  # 不安定な状態確認でも止まらない
                return {"status": "COMPLETED"}
            return {"video": {"url": "https://x/v.mp4"}, "seed": 7}

        common.http_json = fake
        common.media_url_to_file = lambda url, dest: dest.write_bytes(b"mp4") or dest
        gen_video.run_job(self.job(), timeout=60)
        self.assertTrue((self.proj / "clips" / "c01.mp4").exists())
        self.assertFalse((self.proj / "clips" / "c01.task").exists())
        meta = json.loads((self.proj / "clips" / "c01.json").read_text())
        self.assertEqual(meta["request_id"], "r1")
        self.assertIn("https://queue.fal.run/bytedance/seedance-2.0/requests/r1/status", [c[1] for c in calls])

    def test_locked_moves_task(self):
        (self.proj / "clips" / "c01.task").write_text("r2")

        def fake(method, url, headers, body=None, **kw):
            raise common.HttpError(403, method, url, '{"detail": "User is locked"}')

        common.http_json = fake
        with self.assertRaises(RuntimeError) as cm:
            gen_video.run_job(self.job(), timeout=60)
        self.assertIn("残高を追加", str(cm.exception))
        self.assertTrue((self.proj / "clips" / "c01.task.locked").exists())
        self.assertFalse((self.proj / "clips" / "c01.task").exists())


class TestReviewFixes(unittest.TestCase):
    def test_queue_urls_rebuilt_and_validated(self):
        s, r = gen_video.queue_urls("bytedance/seedance-2.0/fast/image-to-video", "a1-b2")
        self.assertEqual(r, "https://queue.fal.run/bytedance/seedance-2.0/requests/a1-b2")
        for bad in ("../x", "a/b", "a?b", "", "a b"):
            with self.assertRaises(ValueError):
                gen_video.queue_urls("fal-ai/veo3.1/fast/image-to-video", bad)
        with self.assertRaises(ValueError):
            gen_video.queue_urls("evil.example/app", "abc")

    def test_read_task_rejects_bad_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c01.task"
            p.write_text(json.dumps({"request_id": "x/../y"}))
            with self.assertRaises(RuntimeError):
                gen_video.read_task(p)
            p.write_text("12345")  # JSON として数値だけでも、id の文字列として扱う
            self.assertEqual(gen_video.read_task(p)["request_id"], "12345")

    def test_resume_ignores_saved_urls(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            (proj / "clips").mkdir()
            (proj / "clips" / "c01.task").write_text(json.dumps({
                "request_id": "r9", "model": "kling30", "endpoint": "evil/x",
                "status_url": "https://evil.example/s", "response_url": "https://evil.example/r"}))
            seen = []
            saved = (common.fal_headers, common.http_json, common.media_url_to_file)
            common.fal_headers = lambda: {"Authorization": "Key k"}

            def fake(method, url, headers, body=None, **kw):
                seen.append(url)
                return {"status": "COMPLETED"} if url.endswith("/status") else {"video": {"url": "https://v"}}

            common.http_json = fake
            common.media_url_to_file = lambda url, dest: dest.write_bytes(b"x") or dest
            try:
                job = gen_video.Job(proj, {"id": "c01", "prompt": "p", "frame": "f.png", "dur": 4},
                                    "seedance20", "720p", False, False)
                gen_video.run_job(job, timeout=30)
            finally:
                common.fal_headers, common.http_json, common.media_url_to_file = saved
            self.assertTrue(seen)
            self.assertTrue(all(u.startswith("https://queue.fal.run/fal-ai/kling-video/requests/r9") for u in seen), seen)

    def test_safe_path_and_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "frames").mkdir()
            self.assertEqual(common.safe_path(root, "frames/c01.png"), root / "frames" / "c01.png")
            self.assertEqual(common.safe_path(root, str(root / "a.png")), root / "a.png")
            for bad in ("../x.png", "/etc/passwd", "frames/../../x", ""):
                with self.assertRaises(ValueError):
                    common.safe_path(root, bad)
            outside = tempfile.mkdtemp()
            os.symlink(outside, root / "link")
            with self.assertRaises(ValueError):
                common.safe_path(root, "link/x.png")
        self.assertEqual(common.check_id("c01-a_b"), "c01-a_b")
        for bad in ("../c01", "c 01", "", "a" * 65, "c01/x", "c01.mp4"):
            with self.assertRaises(ValueError):
                common.check_id(bad)

    def test_build_body_rejects_outside_frame(self):
        cut = {"id": "c01", "frame": "../outside.png", "end_frame": None, "refs": []}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                gen_video.build_body("seedance20", cut, Path(d), "p", 4, "720p", images=False)
            with self.assertRaises(ValueError):
                gen_video.check_inputs(dict(cut, frame="/etc/hosts"), Path(d), "seedance20")

    def test_deprecation(self):
        self.assertEqual(gen_video.MODELS["veo31-fast"]["deprecated"], "2026-10-22")
        self.assertEqual(gen_video.MODELS["veo31-fast-flf"]["deprecated"], "2026-10-22")
        self.assertIn("廃止予定", gen_video.deprecation_warning("veo31-fast", "2026-10-10"))
        self.assertIn("廃止済み", gen_video.deprecation_warning("veo31-fast-flf", "2026-10-23"))
        self.assertIsNone(gen_video.deprecation_warning("seedance20"))
        self.assertEqual(gen_video.MODELS["kling30"]["durations"], list(range(3, 16)))
        self.assertIsNone(gen_video.MODELS["kling30"]["aspect_ratio"])

    def test_variant_names_and_plans(self):
        self.assertEqual(gen_video.job_name("c01", True, 2), "c01-draft-v2")
        self.assertEqual(gen_video.job_name("c01", True), "c01-draft")
        self.assertEqual(gen_video.job_name("c01", False), "c01")
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            (proj / "frames").mkdir()
            (proj / "frames" / "c01.png").write_bytes(b"x")
            (proj / "clips").mkdir()
            (proj / "clips" / "c01-draft-v1.mp4").write_bytes(b"x")
            cuts = {"c01": {"id": "c01", "prompt": "p", "frame": "frames/c01.png", "dur": 4, "model": "seedance20"}}
            jobs = gen_video.plan_jobs(proj, cuts, ["c01"], None, None, True, False, 3)
            self.assertEqual([j.name for j in jobs], ["c01-draft-v1", "c01-draft-v2", "c01-draft-v3"])
            self.assertEqual([j.state for j in jobs], ["done", "new", "new"])
            forced = gen_video.plan_jobs(proj, cuts, ["c01"], None, None, True, True, 3)
            self.assertEqual(forced[0].state, "new")
            final = gen_video.plan_jobs(proj, cuts, ["c01"], None, None, False, False)
            total = gen_video.print_plan("draft", jobs) + gen_video.print_plan("final", final)
            self.assertAlmostEqual(total, 2 * 0.43 + 1.214, places=2)

    def test_mask_and_settings(self):
        self.assertEqual(common.mask("abcdef"), "設定済み")
        self.assertEqual(common.mask(""), "(未設定)")
        self.assertNotIn("byteplus_api_key", common.SETTINGS)

    def test_local_config_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "tvs-config.json"
            cfg.write_text(json.dumps({"cta_url": "https://example.com"}))
            os.chmod(cfg, 0o644)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(common.get("cta_url"), "https://example.com")
            finally:
                os.chdir(cwd)
            self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)


class TestCuesheet(unittest.TestCase):
    def make(self, d: Path) -> Path:
        proj = d / "t900"
        for sub in ("clips", "frames"):
            (proj / sub).mkdir(parents=True)
        cuts = [
            {"id": "c01", "chapter": "つかみ", "picture": "メモ", "line": "このメモが、サイトになる。", "action": "a",
             "camera": "static", "prompt": "p", "start": 0.0, "dur": 4.0},
            {"id": "c02", "picture": "完成画面", "line": "指示は1行。", "kind": "screen", "start": 4.0, "dur": 3.0},
            {"id": "c03", "picture": "", "line": "あ" * 30, "kind": "text", "start": 7.5, "dur": 3.0},
        ]
        common.save_shots(proj, {"slug": "t900", "type": "story", "cuts": cuts})
        (proj / "clips" / "c01.mp4").write_bytes(b"x")
        (proj / "frames" / "c03.png").write_bytes(b"x")
        return proj

    def test_short_type(self):
        shots = new_project.make_shots("t901", "short", *new_project.TYPES["short"][:1], new_project.TYPES["short"][1])
        self.assertEqual(len(shots["cuts"]), 2)
        total = sum(c["dur"] for c in shots["cuts"])
        lo, hi = cuesheet.TYPE_TARGET["short"]
        self.assertTrue(lo <= total <= hi)

    def test_check_finds_gaps_and_fast_lines(self):
        with tempfile.TemporaryDirectory() as d:
            proj = self.make(Path(d))
            issues = "\n".join(cuesheet.check(common.load_shots(proj)))
            self.assertIn("c02→c03: 0.50秒のすき間", issues)
            self.assertIn("c03: ナレーションが1秒あたり10.0字", issues)
            self.assertIn("c03: 絵の内容が空", issues)
            self.assertNotIn("c02: 動画プロンプトが空", issues)  # 画面収録は生成しない

    def test_states(self):
        with tempfile.TemporaryDirectory() as d:
            proj = self.make(Path(d))
            body = cuesheet.rows(proj, common.load_shots(proj))
            state = [r[12] for r in body]
            self.assertEqual(state, ["生成済", "未着手", "文字カット"])
            self.assertEqual([r[1] for r in body], ["つかみ", "", ""])
            self.assertEqual(body[1][3], "0:04.0")

    def test_roundtrip_imports_only_planning_columns(self):
        with tempfile.TemporaryDirectory() as d:
            proj = self.make(Path(d))
            out = proj / "cuesheet.xlsx"
            cuesheet.export(proj, common.load_shots(proj), out)
            recs = cuesheet.read_sheet(out)
            self.assertEqual([r["カットID"] for r in recs], ["c01", "c02", "c03"])
            recs[1]["備考・ご指示"] = "もっと寄りで"
            recs[1]["絵の内容"] = "完成画面（スマホ）"
            recs[2]["ナレーション（読み上げる文）"] = "書き換えた"
            shots = common.load_shots(proj)
            changed, skipped = cuesheet.import_sheet(shots, recs)
            c = {x["id"]: x for x in shots["cuts"]}
            self.assertEqual(c["c02"]["note"], "もっと寄りで")
            self.assertEqual(c["c02"]["picture"], "完成画面（スマホ）")
            self.assertEqual(c["c02"]["chapter"], "つかみ")  # 章は前の行から引き継ぐ
            self.assertEqual(c["c03"]["line"], "あ" * 30)  # ナレは自動で戻さない
            self.assertTrue(any("c03" in s and "ナレーション" in s for s in skipped))
            self.assertNotIn("kind", c["c01"])
            self.assertGreaterEqual(changed, 1)


if __name__ == "__main__":
    unittest.main()
