#!/usr/bin/env python3
"""Image-to-video per cut on the fal queue. Resumable and safe to re-run.

  python3 gen_video.py --project t004-camp --cut c01 --dry-run     # cost estimate only
  python3 gen_video.py --project t004-camp --cut c01 --draft       # cheap/fast preview -> clips/c01-draft.mp4
  python3 gen_video.py --project t004-camp --all                   # all cuts, up to 5 in parallel

Reads prompt / frame / end_frame / refs / dur / model from shots.json. `dur` is rounded UP to the
next duration the model accepts. Nothing unsupported is sent silently: invalid values stop with "Allowed: ...".

Resume: the request id is written to clips/<cut>.task right after submission. Re-running picks it up.
Finished clips (clips/<cut>.mp4) are skipped unless --force. Audio is never generated.

Drafts: --draft --variants 3 makes clips/<cut>-draft-v1.mp4 ... v3 (each with its own .task).
Cost: --dry-run --with-draft prints the draft plan, the final plan and the total of both.
Input images (frame / end_frame / refs) must be inside the project folder.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import common

R_480_720_1080_4K = ["480p", "720p", "1080p", "4k"]

# Allowed values and prices were read from https://fal.ai/models/<endpoint>/llms.txt on 2026-10-10.
# Seedance price = tokens * rate, tokens/s = w*h*24/1024 (9:16 sizes: 480x854, 720x1280, 1080x1920, 2160x3840).
MODELS: dict[str, dict] = {
    "seedance20": {
        "endpoint": "bytedance/seedance-2.0/image-to-video",
        "mode": "first",                       # start frame (+ optional end frame)
        "image_key": "image_url", "end_key": "end_image_url",
        "durations": list(range(4, 16)), "dur_format": "{}",
        "resolutions": R_480_720_1080_4K, "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("seedance20-fast", "480p"),
        # USD/s, checked 2026-10-10 ($0.014 per 1k tokens; 4k $0.008 per 1k tokens)
        "usd_per_sec": {"480p": 0.1345, "720p": 0.3034, "1080p": 0.682, "4k": 1.555},
    },
    "seedance20-fast": {
        "endpoint": "bytedance/seedance-2.0/fast/image-to-video",
        "mode": "first",
        "image_key": "image_url", "end_key": "end_image_url",
        "durations": list(range(4, 16)), "dur_format": "{}",
        "resolutions": ["480p", "720p"], "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("seedance20-fast", "480p"),
        # USD/s, checked 2026-10-10 ($0.0112 per 1k tokens)
        "usd_per_sec": {"480p": 0.1076, "720p": 0.2419},
    },
    "seedance20-ref": {
        "endpoint": "bytedance/seedance-2.0/reference-to-video",
        "mode": "ref",                         # refs -> image_urls (@Image1, @Image2 ... in the prompt)
        "refs_key": "image_urls", "max_refs": 9,
        "durations": list(range(4, 16)), "dur_format": "{}",
        "resolutions": R_480_720_1080_4K, "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("seedance20-ref", "480p"),
        # USD/s, checked 2026-10-10 (image references only; same token rate as image-to-video)
        "usd_per_sec": {"480p": 0.1345, "720p": 0.3034, "1080p": 0.682, "4k": 1.555},
    },
    "veo31-fast": {
        "endpoint": "fal-ai/veo3.1/fast/image-to-video",
        "mode": "first",
        "image_key": "image_url",
        "durations": [4, 6, 8], "dur_format": "{}s",
        "resolutions": ["720p", "1080p", "4k"], "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("veo31-fast", "720p"),
        "deprecated": "2026-10-22",            # fal API page: "This endpoint will be deprecated on October 22, 2026."
        # USD/s without audio, checked 2026-10-10
        "usd_per_sec": {"720p": 0.10, "1080p": 0.10, "4k": 0.30},
    },
    "veo31-fast-flf": {
        "endpoint": "fal-ai/veo3.1/fast/first-last-frame-to-video",
        "mode": "first-last",                  # end_frame is required
        "image_key": "first_frame_url", "end_key": "last_frame_url", "end_required": True,
        "durations": [4, 6, 8], "dur_format": "{}s",
        "resolutions": ["720p", "1080p", "4k"], "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("veo31-fast-flf", "720p"),
        "deprecated": "2026-10-22",            # fal API page: "This endpoint will be deprecated on October 22, 2026."
        # USD/s without audio, checked 2026-10-10
        "usd_per_sec": {"720p": 0.10, "1080p": 0.10, "4k": 0.30},
    },
    "kling30": {
        "endpoint": "fal-ai/kling-video/v3/pro/image-to-video",
        "mode": "first",
        "image_key": "start_image_url", "end_key": "end_image_url",
        "durations": list(range(3, 16)), "dur_format": "{}",
        # No resolution / aspect_ratio parameter (re-checked 2026-10-10): output follows the start image,
        # so the frame itself must be 9:16. duration "3".."15".
        "resolutions": [], "default_resolution": None,
        "aspect_ratio": None, "audio_key": "generate_audio",
        "draft": ("kling30", None),
        # USD/s audio off, checked 2026-10-10
        "usd_per_sec": {None: 0.112},
    },
}

MAX_PARALLEL = 5
MAX_VARIANTS = 4
RID_RE = re.compile(r"[A-Za-z0-9-]{1,128}")
POLL_SEC = 10
_print_lock = threading.Lock()


def say(*a) -> None:
    with _print_lock:
        print(*a, flush=True)


# --- pure helpers (unit-tested) -------------------------------------------------------------

def round_up_duration(model: str, dur: float | None) -> int:
    """Smallest allowed duration >= dur. Missing/zero dur -> the model's minimum."""
    allowed = MODELS[model]["durations"]
    if not dur or dur <= 0:
        return allowed[0]
    need = math.ceil(round(float(dur), 3) - 1e-9)
    for d in allowed:
        if d >= need:
            return d
    raise ValueError(f"duration {dur}s too long for {model}. Allowed: {', '.join(map(str, allowed))} (seconds)")


def check_resolution(model: str, res: str | None) -> str | None:
    allowed = MODELS[model]["resolutions"]
    if not allowed:
        if res:
            raise ValueError(f"{model} has no resolution option (output follows the start image). Allowed: (none)")
        return None
    if res not in allowed:
        raise ValueError(f"resolution {res!r} not supported by {model}. Allowed: {', '.join(allowed)}")
    return res


def pick_model_and_res(cut: dict, model_arg: str | None, res_arg: str | None, draft: bool) -> tuple[str, str | None]:
    model = model_arg or cut.get("model") or "seedance20"
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}. Allowed: {', '.join(MODELS)}")
    if draft:
        model, res = MODELS[model]["draft"]
        if res_arg:
            res = res_arg
        elif res is None and MODELS[model]["resolutions"]:
            res = MODELS[model]["resolutions"][0]
    else:
        res = res_arg or MODELS[model]["default_resolution"]
    return model, check_resolution(model, res)


def estimate_cost(model: str, res: str | None, seconds: int) -> float:
    return round(MODELS[model]["usd_per_sec"][res] * seconds, 3)


def task_state(clips: Path, name: str, force: bool = False) -> str:
    """'done' (mp4 exists), 'resume' (.task pending), 'locked' (.task.locked only), or 'new'."""
    if (clips / f"{name}.task").exists():
        return "resume"
    if (clips / f"{name}.mp4").exists() and not force:
        return "done"
    if (clips / f"{name}.task.locked").exists():
        return "locked"
    return "new"


def read_task(path: Path) -> dict:
    raw = path.read_text().strip()
    try:
        t = json.loads(raw)
    except ValueError:
        t = None
    if not isinstance(t, dict):
        t = {"request_id": raw}  # plain request id (older format)
    rid = t.get("request_id")
    if not isinstance(rid, str) or not RID_RE.fullmatch(rid):
        raise RuntimeError(f"{path.name} has no valid request_id (allowed: letters, digits and '-')")
    return t


def queue_urls(endpoint: str, request_id: str) -> tuple[str, str]:
    """Status and result URLs, always rebuilt from a registry endpoint + validated request id (never taken
    from a saved file, so the API key is only ever sent to queue.fal.run). Queue paths use only the base
    app (owner/app = first two segments), e.g. fal-ai/veo3.1/fast/image-to-video -> fal-ai/veo3.1."""
    if endpoint not in {m["endpoint"] for m in MODELS.values()}:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    if not isinstance(request_id, str) or not RID_RE.fullmatch(request_id):
        raise ValueError("invalid request id (allowed: letters, digits and '-')")
    app = "/".join(endpoint.split("/")[:2])
    base = f"https://queue.fal.run/{app}/requests/{request_id}"
    return base + "/status", base


def deprecation_warning(model: str, today: str | None = None) -> str | None:
    dep = MODELS[model].get("deprecated")
    if not dep:
        return None
    today = today or time.strftime("%Y-%m-%d")
    when = "was deprecated on" if today >= dep else "will be deprecated on"
    return (f"WARN: {model} ({MODELS[model]['endpoint']}) {when} {dep} according to fal; "
            f"prefer seedance20 / kling30")


def job_name(cut_id: str, draft: bool, variant: int | None = None) -> str:
    name = common.check_id(cut_id, "cut id") + ("-draft" if draft else "")
    return name + (f"-v{variant}" if variant else "")


def is_locked_error(e: Exception) -> bool:
    return isinstance(e, common.HttpError) and e.code == 403 and "locked" in (e.body or "").lower()


# --- request building -----------------------------------------------------------------------

def build_body(model: str, cut: dict, project: Path, prompt: str, seconds: int, res: str | None,
               images: bool = True) -> dict:
    m = MODELS[model]
    def uri(p):
        path = common.safe_path(project, p, f"{cut['id']} image")
        return common.data_uri(path) if images else f"<{p}>"

    body: dict = {"prompt": prompt, "duration": m["dur_format"].format(seconds)}
    if res:
        body["resolution"] = res
    if m.get("aspect_ratio"):
        body["aspect_ratio"] = m["aspect_ratio"]
    if m.get("audio_key"):
        body[m["audio_key"]] = False
    if m["mode"] in ("first", "first-last"):
        frame = cut.get("frame")
        if not frame:
            raise ValueError(f"{cut['id']}: 'frame' is required for {model}")
        body[m["image_key"]] = uri(frame)
        end = cut.get("end_frame")
        if m.get("end_required") and not end:
            raise ValueError(f"{cut['id']}: 'end_frame' is required for {model}")
        if end:
            if not m.get("end_key"):
                raise ValueError(f"{cut['id']}: {model} does not take an end frame. "
                                 f"Allowed models with end frame: seedance20, seedance20-fast, veo31-fast-flf, kling30")
            body[m["end_key"]] = uri(end)
    elif m["mode"] == "ref":
        refs = list(cut.get("refs") or []) or ([cut["frame"]] if cut.get("frame") else [])
        if not refs:
            raise ValueError(f"{cut['id']}: 'refs' (or 'frame') is required for {model}")
        if len(refs) > m["max_refs"]:
            raise ValueError(f"{cut['id']}: too many refs. Allowed: up to {m['max_refs']}")
        body[m["refs_key"]] = [uri(r) for r in refs]
    return body


def check_inputs(cut: dict, project: Path, model: str) -> None:
    m = MODELS[model]
    paths = []
    if m["mode"] == "ref":
        paths = list(cut.get("refs") or []) or [cut.get("frame")]
    else:
        paths = [cut.get("frame"), cut.get("end_frame")]
    for p in paths:
        if p and not common.safe_path(project, p, f"{cut['id']} image").exists():
            raise FileNotFoundError(f"{cut['id']}: input image not found: {p}")


# --- one cut ---------------------------------------------------------------------------------

class Job:
    def __init__(self, project: Path, cut: dict, model: str, res: str | None, draft: bool, force: bool,
                 variant: int | None = None):
        self.project, self.cut, self.model, self.res, self.draft = project.resolve(), cut, model, res, draft
        self.variant = variant
        self.name = job_name(cut["id"], draft, variant)
        self.clips = common.safe_path(project, "clips")
        self.seconds = round_up_duration(model, cut.get("dur"))
        self.cost = estimate_cost(model, res, self.seconds)
        self.state = task_state(self.clips, self.name, force)
        self.prompt = (cut.get("prompt") or "").strip()

    def describe(self) -> str:
        return (f"{self.name}: {self.model} {self.res or '(native)'} {self.seconds}s "
                f"(dur {self.cut.get('dur')}) ~${self.cost:.2f} [{self.state}]")


def run_job(job: Job, timeout: int) -> str:
    clips, name = job.clips, job.name
    clips.mkdir(parents=True, exist_ok=True)
    task_file = clips / f"{name}.task"
    headers = common.fal_headers()
    endpoint = MODELS[job.model]["endpoint"]

    if task_file.exists():
        task = read_task(task_file)
        saved_model = task.get("model") or job.model
        if saved_model not in MODELS:
            raise RuntimeError(f"{name}: {task_file.name} names an unknown model {saved_model!r}")
        endpoint = MODELS[saved_model]["endpoint"]  # never a URL/endpoint string from the file
        say(f"{name}: resume {task['request_id']}")
    else:
        body = build_body(job.model, job.cut, job.project, job.prompt, job.seconds, job.res)
        sub = common.http_json("POST", f"https://queue.fal.run/{endpoint}", headers, body, timeout=180, retries=1)
        rid = sub.get("request_id")
        if not rid or not RID_RE.fullmatch(str(rid)):
            raise RuntimeError(f"{name}: missing or invalid request_id in submit response")
        task = {"request_id": rid, "model": job.model, "resolution": job.res, "duration": job.seconds,
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "estimated_cost_usd": job.cost}
        common.save_json(task_file, task)
        say(f"{name}: queued {rid} (~${job.cost:.2f})")

    rid = task["request_id"]
    status_url, result_url = queue_urls(endpoint, rid)
    t0, last = time.time(), ""
    try:
        while True:
            try:
                st = common.http_json("GET", status_url, headers, timeout=60)
            except common.NetworkError as e:
                say(f"{name}: poll network error, retrying ({e})")
                time.sleep(POLL_SEC)
                continue
            s = st.get("status", "")
            if s != last:
                say(f"{name}: {s}")
                last = s
            if s == "COMPLETED":
                break
            if time.time() - t0 > timeout:
                raise RuntimeError(f"{name}: still {s or 'pending'} after {timeout}s; re-run to resume ({task_file.name} kept)")
            time.sleep(POLL_SEC)
        while True:
            try:
                res = common.http_json("GET", result_url, headers, timeout=120)
                break
            except common.NetworkError as e:
                say(f"{name}: result network error, retrying ({e})")
                time.sleep(POLL_SEC)
    except common.HttpError as e:
        if is_locked_error(e):
            locked = clips / f"{name}.task.locked"
            task_file.replace(locked)
            raise RuntimeError(
                f"{name}: account is locked (balance ran out). Results completed while locked cannot be "
                f"retrieved. Top up, then generate again. Moved {task_file.name} -> {locked.name}") from None
        if e.code in (404, 410):
            raise RuntimeError(f"{name}: request {rid} not found or expired ({e.code}). "
                               f"Delete {task_file.name} to generate again.") from None
        raise

    video = res.get("video") or {}
    if not video.get("url"):
        detail = res.get("detail") or res.get("error") or str(res)[:300]
        raise RuntimeError(f"{name}: generation finished without a video: {detail} "
                           f"(delete {task_file.name} to try again)")
    out = clips / f"{name}.mp4"
    common.media_url_to_file(video["url"], out)
    meta = {
        "cut": job.cut["id"], "draft": job.draft, "variant": job.variant, "request_id": rid, "model": task.get("model", job.model),
        "endpoint": endpoint, "prompt": job.prompt, "duration": task.get("duration", job.seconds),
        "resolution": task.get("resolution", job.res), "frame": job.cut.get("frame"),
        "end_frame": job.cut.get("end_frame"), "refs": job.cut.get("refs") or [],
        "estimated_cost_usd": task.get("estimated_cost_usd", job.cost), "seed": res.get("seed"),
        "submitted_at": task.get("submitted_at"), "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    common.save_json(clips / f"{name}.json", meta)
    task_file.unlink(missing_ok=True)
    say(f"{name}: done -> {out.relative_to(job.project)} ({time.time() - t0:.0f}s)")
    return str(out)


# --- main --------------------------------------------------------------------------------------

def plan_jobs(project: Path, cuts: dict, ids: list[str], model_arg: str | None, res_arg: str | None,
              draft: bool, force: bool, variants: int | None = None) -> list[Job]:
    jobs = []
    for cid in ids:
        cut = cuts[cid]
        model, res = pick_model_and_res(cut, model_arg, res_arg, draft)
        for v in (range(1, variants + 1) if variants else [None]):
            job = Job(project, cut, model, res, draft, force, v)
            if job.state in ("new", "locked"):
                if not job.prompt:
                    raise ValueError(f"{cid}: empty prompt in shots.json")
                check_inputs(cut, project, model)
                build_body(model, cut, project, job.prompt, job.seconds, res, images=False)  # validates fields
            jobs.append(job)
    return jobs


def print_plan(title: str, jobs: list[Job]) -> float:
    todo = [j for j in jobs if j.state != "done"]
    spend = sum(j.cost for j in todo if j.state in ("new", "locked"))
    if title:
        print(f"== {title}")
    for j in jobs:
        print(j.describe())
    print(f"to submit: {sum(1 for j in todo if j.state in ('new', 'locked'))}, "
          f"to resume: {sum(1 for j in todo if j.state == 'resume')}, "
          f"estimated new spend: ${spend:.2f}")
    return spend


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate video clips per cut on fal (resumable).")
    ap.add_argument("--project", help="project folder or slug (default: current folder)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cut", action="append", help="cut id, e.g. c01 (repeatable)")
    g.add_argument("--all", action="store_true", help="all cuts in shots.json")
    ap.add_argument("--model", help=f"override shots.json model ({', '.join(MODELS)})")
    ap.add_argument("--resolution", help="override resolution (validated per model)")
    ap.add_argument("--draft", action="store_true",
                    help="draft settings (seedance20 -> seedance20-fast 480p, others lowest resolution); "
                         "writes clips/<cut>-draft.mp4")
    ap.add_argument("--variants", type=int,
                    help=f"with --draft: N drafts per cut -> clips/<cut>-draft-v1.mp4 ... (1-{MAX_VARIANTS})")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and cost estimate; submit nothing")
    ap.add_argument("--with-draft", action="store_true",
                    help="with --dry-run: estimate drafts and final clips together and print the total")
    ap.add_argument("--force", action="store_true", help="regenerate even if the clip exists")
    ap.add_argument("--timeout", type=int, default=2400, help="max seconds to wait per cut (default 2400)")
    a = ap.parse_args()

    if a.variants is not None:
        if not a.draft and not a.with_draft:
            raise ValueError("--variants needs --draft (or --dry-run --with-draft)")
        if not 1 <= a.variants <= MAX_VARIANTS:
            raise ValueError(f"--variants out of range. Allowed: 1-{MAX_VARIANTS}")
    if a.with_draft and not a.dry_run:
        raise ValueError("--with-draft is an estimate option; use it with --dry-run")

    project = common.project_dir(a.project)
    shots = common.load_shots(project)
    cuts = {}
    for c in shots.get("cuts", []):
        cuts[common.check_id(c.get("id", ""), "cut id")] = c
    ids = list(cuts) if a.all else a.cut
    for cid in ids:
        common.check_id(cid, "cut id")
        if cid not in cuts:
            raise ValueError(f"unknown cut {cid!r}. Allowed: {', '.join(cuts)}")

    if a.with_draft:
        drafts = plan_jobs(project, cuts, ids, a.model, None, True, a.force, a.variants)
        finals = plan_jobs(project, cuts, ids, a.model, a.resolution, False, a.force)
        _warn_deprecated(drafts + finals)
        total = print_plan("draft", drafts) + print_plan("final", finals)
        print(f"total (draft + final): ${total:.2f}")
        return

    jobs = plan_jobs(project, cuts, ids, a.model, a.resolution, a.draft, a.force, a.variants)
    _warn_deprecated(jobs)
    print_plan("", jobs)
    todo = [j for j in jobs if j.state != "done"]
    if a.dry_run or not todo:
        return

    failures = []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(todo))) as ex:
        futs = {ex.submit(run_job, j, a.timeout): j for j in todo}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:  # report every cut, then fail once
                failures.append(f"{futs[f].name}: {common.scrub(str(e))}")
    if failures:
        for msg in failures:
            print("FAILED " + msg, file=sys.stderr)
        raise RuntimeError(f"{len(failures)} of {len(todo)} cut(s) failed")


def _warn_deprecated(jobs: list[Job]) -> None:
    for model in sorted({j.model for j in jobs}):
        w = deprecation_warning(model)
        if w:
            print(w, file=sys.stderr)


if __name__ == "__main__":
    common.cli(main)
