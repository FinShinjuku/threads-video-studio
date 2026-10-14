#!/usr/bin/env python3
"""カットごとに fal のキューで画像から動画を作る。途中から再開でき、何度実行しても安全。

  python3 gen_video.py --project t004-camp --cut c01 --dry-run     # 費用の見積もりだけ
  python3 gen_video.py --project t004-camp --cut c01 --draft       # 安く速い下書き -> clips/c01-draft.mp4
  python3 gen_video.py --project t004-camp --all                   # 全カット（最大5本並行）

shots.json から prompt / frame / end_frame / refs / dur / model を読む。`dur` はモデルが受け付ける
長さに切り上げる。対応していない値は黙って送らず、「使える値: ...」を出して止まる。

再開: 送信直後に request id を clips/<cut>.task に書く。再実行するとそこから続ける。
できあがったクリップ（clips/<cut>.mp4）は --force がなければ飛ばす。音声は作らない。

下書き: --draft --variants 3 で clips/<cut>-draft-v1.mp4 〜 v3 を作る（それぞれ別の .task）。
費用: --dry-run --with-draft で下書きと本番の計画、両方の合計を出す。
入力画像（frame / end_frame / refs）はプロジェクトフォルダの中に置く。
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

# 使える値と料金は 2026-10-10 に https://fal.ai/models/<endpoint>/llms.txt から確認した。
# Seedance の料金 = トークン数 × 単価、1秒あたりトークン数 = w*h*24/1024（9:16 のサイズ: 480x854, 720x1280, 1080x1920, 2160x3840）。
MODELS: dict[str, dict] = {
    "seedance20": {
        "endpoint": "bytedance/seedance-2.0/image-to-video",
        "mode": "first",                       # 開始フレーム（終了フレームは任意）
        "image_key": "image_url", "end_key": "end_image_url",
        "durations": list(range(4, 16)), "dur_format": "{}",
        "resolutions": R_480_720_1080_4K, "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("seedance20-fast", "480p"),
        # 1秒あたり USD、2026-10-10 確認（1k トークンあたり $0.014。4k は 1k トークンあたり $0.008）
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
        # 1秒あたり USD、2026-10-10 確認（1k トークンあたり $0.0112）
        "usd_per_sec": {"480p": 0.1076, "720p": 0.2419},
    },
    "seedance20-ref": {
        "endpoint": "bytedance/seedance-2.0/reference-to-video",
        "mode": "ref",                         # refs -> image_urls（プロンプトでは @Image1, @Image2 ... で参照）
        "refs_key": "image_urls", "max_refs": 9,
        "durations": list(range(4, 16)), "dur_format": "{}",
        "resolutions": R_480_720_1080_4K, "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("seedance20-ref", "480p"),
        # 1秒あたり USD、2026-10-10 確認（画像参照のみ。トークン単価は image-to-video と同じ）
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
        "deprecated": "2026-11-17",            # fal の掲示（starts_with "fal-ai/veo3.1/"）: "This endpoint will be deprecated on November 17, 2026."（2026-10-14 確認）
        # 音声なしの1秒あたり USD、2026-10-10 確認
        "usd_per_sec": {"720p": 0.10, "1080p": 0.10, "4k": 0.30},
    },
    "veo31-fast-flf": {
        "endpoint": "fal-ai/veo3.1/fast/first-last-frame-to-video",
        "mode": "first-last",                  # end_frame が必須
        "image_key": "first_frame_url", "end_key": "last_frame_url", "end_required": True,
        "durations": [4, 6, 8], "dur_format": "{}s",
        "resolutions": ["720p", "1080p", "4k"], "default_resolution": "720p",
        "aspect_ratio": "9:16", "audio_key": "generate_audio",
        "draft": ("veo31-fast-flf", "720p"),
        "deprecated": "2026-11-17",            # fal の掲示（starts_with "fal-ai/veo3.1/"）: "This endpoint will be deprecated on November 17, 2026."（2026-10-14 確認）
        # 音声なしの1秒あたり USD、2026-10-10 確認
        "usd_per_sec": {"720p": 0.10, "1080p": 0.10, "4k": 0.30},
    },
    "kling30": {
        "endpoint": "fal-ai/kling-video/v3/pro/image-to-video",
        "mode": "first",
        "image_key": "start_image_url", "end_key": "end_image_url",
        "durations": list(range(3, 16)), "dur_format": "{}",
        # resolution / aspect_ratio の指定はない（2026-10-10 再確認）。出力は開始画像に合わせるので、
        # フレーム画像そのものを 9:16 にする。duration は "3"〜"15"。
        "resolutions": [], "default_resolution": None,
        "aspect_ratio": None, "audio_key": "generate_audio",
        "draft": ("kling30", None),
        # 音声オフの1秒あたり USD、2026-10-10 確認
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


# --- 純粋な補助関数（単体テストあり） -------------------------------------------------------------

def round_up_duration(model: str, dur: float | None) -> int:
    """dur 以上で最小の使える長さを返す。dur が無いか 0 ならモデルの最小値。"""
    allowed = MODELS[model]["durations"]
    if not dur or dur <= 0:
        return allowed[0]
    need = math.ceil(round(float(dur), 3) - 1e-9)
    for d in allowed:
        if d >= need:
            return d
    raise ValueError(f"長さ {dur}秒 は {model} には長すぎる。使える値: {', '.join(map(str, allowed))}（秒）")


def check_resolution(model: str, res: str | None) -> str | None:
    allowed = MODELS[model]["resolutions"]
    if not allowed:
        if res:
            raise ValueError(f"{model} には解像度の指定がない（出力は開始画像に合わせる）。使える値: (なし)")
        return None
    if res not in allowed:
        raise ValueError(f"解像度 {res!r} は {model} では使えない。使える値: {', '.join(allowed)}")
    return res


def pick_model_and_res(cut: dict, model_arg: str | None, res_arg: str | None, draft: bool) -> tuple[str, str | None]:
    model = model_arg or cut.get("model") or "seedance20"
    if model not in MODELS:
        raise ValueError(f"不明なモデル {model!r}。使える値: {', '.join(MODELS)}")
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
    """'done'（mp4 がある）、'resume'（.task が残っている）、'locked'（.task.locked だけある）、'new' のどれかを返す。"""
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
        t = {"request_id": raw}  # request id だけの古い形式
    rid = t.get("request_id")
    if not isinstance(rid, str) or not RID_RE.fullmatch(rid):
        raise RuntimeError(f"{path.name} に正しい request_id がない（使える文字: 英字・数字・'-'）")
    return t


def queue_urls(endpoint: str, request_id: str) -> tuple[str, str]:
    """状態確認と結果取得の URL を返す。毎回、登録済みのエンドポイントと検証済みの request id から組み立てる
    （保存ファイルの URL は使わないので、API キーは queue.fal.run にしか送られない）。キューのパスは
    先頭2区切り（owner/app）だけを使う。例: fal-ai/veo3.1/fast/image-to-video -> fal-ai/veo3.1。"""
    if endpoint not in {m["endpoint"] for m in MODELS.values()}:
        raise ValueError(f"不明なエンドポイント {endpoint!r}")
    if not isinstance(request_id, str) or not RID_RE.fullmatch(request_id):
        raise ValueError("request id が正しくない（使える文字: 英字・数字・'-'）")
    app = "/".join(endpoint.split("/")[:2])
    base = f"https://queue.fal.run/{app}/requests/{request_id}"
    return base + "/status", base


def deprecation_warning(model: str, today: str | None = None) -> str | None:
    dep = MODELS[model].get("deprecated")
    if not dep:
        return None
    today = today or time.strftime("%Y-%m-%d")
    when = "で廃止済み" if today >= dep else "で廃止予定"
    return (f"警告: fal によると {model}（{MODELS[model]['endpoint']}）は {dep} {when}。"
            f"seedance20 / kling30 を使う")


def job_name(cut_id: str, draft: bool, variant: int | None = None) -> str:
    name = common.check_id(cut_id, "カット ID") + ("-draft" if draft else "")
    return name + (f"-v{variant}" if variant else "")


def is_locked_error(e: Exception) -> bool:
    return isinstance(e, common.HttpError) and e.code == 403 and "locked" in (e.body or "").lower()


# --- リクエストの組み立て -----------------------------------------------------------------------

def build_body(model: str, cut: dict, project: Path, prompt: str, seconds: int, res: str | None,
               images: bool = True) -> dict:
    m = MODELS[model]
    def uri(p):
        path = common.safe_path(project, p, f"{cut['id']} の画像")
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
            raise ValueError(f"{cut['id']}: {model} には 'frame' が必要")
        body[m["image_key"]] = uri(frame)
        end = cut.get("end_frame")
        if m.get("end_required") and not end:
            raise ValueError(f"{cut['id']}: {model} には 'end_frame' が必要")
        if end:
            if not m.get("end_key"):
                raise ValueError(f"{cut['id']}: {model} は終了フレームを使えない。"
                                 f"終了フレームを使えるモデル: seedance20, seedance20-fast, veo31-fast-flf, kling30")
            body[m["end_key"]] = uri(end)
    elif m["mode"] == "ref":
        refs = list(cut.get("refs") or []) or ([cut["frame"]] if cut.get("frame") else [])
        if not refs:
            raise ValueError(f"{cut['id']}: {model} には 'refs'（または 'frame'）が必要")
        if len(refs) > m["max_refs"]:
            raise ValueError(f"{cut['id']}: refs が多すぎる。使える値: {m['max_refs']} 枚まで")
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
        if p and not common.safe_path(project, p, f"{cut['id']} の画像").exists():
            raise FileNotFoundError(f"{cut['id']}: 入力画像が見つからない: {p}")


# --- 1カット分 ---------------------------------------------------------------------------------

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
        return (f"{self.name}: {self.model} {self.res or '(元画像のまま)'} {self.seconds}s "
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
            raise RuntimeError(f"{name}: {task_file.name} のモデル {saved_model!r} が不明")
        endpoint = MODELS[saved_model]["endpoint"]  # ファイルにある URL やエンドポイント文字列は使わない
        say(f"{name}: 再開 {task['request_id']}")
    else:
        body = build_body(job.model, job.cut, job.project, job.prompt, job.seconds, job.res)
        sub = common.http_json("POST", f"https://queue.fal.run/{endpoint}", headers, body, timeout=180, retries=1)
        rid = sub.get("request_id")
        if not rid or not RID_RE.fullmatch(str(rid)):
            raise RuntimeError(f"{name}: 送信の応答に request_id がないか、正しくない")
        task = {"request_id": rid, "model": job.model, "resolution": job.res, "duration": job.seconds,
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "estimated_cost_usd": job.cost}
        common.save_json(task_file, task)
        say(f"{name}: キューに登録 {rid}（約 ${job.cost:.2f}）")

    rid = task["request_id"]
    status_url, result_url = queue_urls(endpoint, rid)
    t0, last = time.time(), ""
    try:
        while True:
            try:
                st = common.http_json("GET", status_url, headers, timeout=60)
            except common.NetworkError as e:
                say(f"{name}: 状態確認中に通信エラー。やり直す（{e}）")
                time.sleep(POLL_SEC)
                continue
            s = st.get("status", "")
            if s != last:
                say(f"{name}: {s}")
                last = s
            if s == "COMPLETED":
                break
            if time.time() - t0 > timeout:
                raise RuntimeError(f"{name}: {timeout}秒たっても {s or '待機中'}。再実行すると続きから再開する（{task_file.name} は残してある）")
            time.sleep(POLL_SEC)
        while True:
            try:
                res = common.http_json("GET", result_url, headers, timeout=120)
                break
            except common.NetworkError as e:
                say(f"{name}: 結果の取得中に通信エラー。やり直す（{e}）")
                time.sleep(POLL_SEC)
    except common.HttpError as e:
        if is_locked_error(e):
            locked = clips / f"{name}.task.locked"
            task_file.replace(locked)
            raise RuntimeError(
                f"{name}: アカウントがロックされた（残高切れ）。ロック中に完成した結果は取り出せない。"
                f"残高を追加してから作り直す。{task_file.name} を {locked.name} に移した") from None
        if e.code in (404, 410):
            raise RuntimeError(f"{name}: リクエスト {rid} が見つからないか期限切れ（{e.code}）。"
                               f"{task_file.name} を消すと作り直せる") from None
        raise

    video = res.get("video") or {}
    if not video.get("url"):
        detail = res.get("detail") or res.get("error") or str(res)[:300]
        raise RuntimeError(f"{name}: 生成は終わったが動画がない: {detail} "
                           f"（{task_file.name} を消すとやり直せる）")
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
    say(f"{name}: 完了 -> {out.relative_to(job.project)}（{time.time() - t0:.0f}秒）")
    return str(out)


# --- main --------------------------------------------------------------------------------------

def plan_jobs(project: Path, cuts: dict, ids: list[str], model_arg: str | None, res_arg: str | None,
              draft: bool, force: bool, variants: int | None = None, estimate_only: bool = False) -> list[Job]:
    jobs, missing = [], set()
    for cid in ids:
        cut = cuts[cid]
        model, res = pick_model_and_res(cut, model_arg, res_arg, draft)
        for v in (range(1, variants + 1) if variants else [None]):
            job = Job(project, cut, model, res, draft, force, v)
            if job.state in ("new", "locked"):
                if not job.prompt:
                    raise ValueError(f"{cid}: shots.json の prompt が空")
                try:
                    check_inputs(cut, project, model)
                except FileNotFoundError as e:
                    if not estimate_only:
                        raise
                    missing.add(str(e))  # 見積もりは起点画像の清書前（G1）にも出せるようにする
                build_body(model, cut, project, job.prompt, job.seconds, res, images=False)  # 項目の検証だけ
            jobs.append(job)
    for msg in sorted(missing):
        say(f"注意: {msg}（見積もりだけ出す。生成の前に用意する）")
    return jobs


def print_plan(title: str, jobs: list[Job]) -> float:
    todo = [j for j in jobs if j.state != "done"]
    spend = sum(j.cost for j in todo if j.state in ("new", "locked"))
    if title:
        print(f"== {title}")
    for j in jobs:
        print(j.describe())
    print(f"新規送信: {sum(1 for j in todo if j.state in ('new', 'locked'))}、"
          f"再開: {sum(1 for j in todo if j.state == 'resume')}、"
          f"新たにかかる費用の見積もり: ${spend:.2f}")
    return spend


def main() -> None:
    ap = argparse.ArgumentParser(description="fal でカットごとに動画クリップを作る（途中から再開できる）。")
    ap.add_argument("--project", help="プロジェクトのフォルダまたはスラッグ（省略時は今のフォルダ）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cut", action="append", help="カット id。例: c01（複数回指定できる）")
    g.add_argument("--all", action="store_true", help="shots.json の全カット")
    ap.add_argument("--model", help=f"shots.json のモデルを上書きする（{', '.join(MODELS)}）")
    ap.add_argument("--resolution", help="解像度を上書きする（モデルごとに検証する）")
    ap.add_argument("--draft", action="store_true",
                    help="下書きの設定で作る（seedance20 は seedance20-fast 480p、ほかは最低解像度）。"
                         "clips/<cut>-draft.mp4 に書く")
    ap.add_argument("--variants", type=int,
                    help=f"--draft と併用: 1カットにつき N 本の下書き -> clips/<cut>-draft-v1.mp4 ...（1〜{MAX_VARIANTS}）")
    ap.add_argument("--dry-run", action="store_true", help="計画と費用の見積もりだけ出す。送信はしない")
    ap.add_argument("--with-draft", action="store_true",
                    help="--dry-run と併用: 下書きと本番をまとめて見積もり、合計を出す")
    ap.add_argument("--force", action="store_true", help="クリップがあっても作り直す")
    ap.add_argument("--timeout", type=int, default=2400, help="1カットあたりの最大待ち時間（秒、既定 2400）")
    a = ap.parse_args()

    if a.variants is not None:
        if not a.draft and not a.with_draft:
            raise ValueError("--variants には --draft（または --dry-run --with-draft）が必要")
        if not 1 <= a.variants <= MAX_VARIANTS:
            raise ValueError(f"--variants が範囲外。使える値: 1-{MAX_VARIANTS}")
    if a.with_draft and not a.dry_run:
        raise ValueError("--with-draft は見積もり用。--dry-run と一緒に使う")

    project = common.project_dir(a.project)
    shots = common.load_shots(project)
    cuts = {}
    for c in shots.get("cuts", []):
        cuts[common.check_id(c.get("id", ""), "カット ID")] = c
    ids = list(cuts) if a.all else a.cut
    for cid in ids:
        common.check_id(cid, "カット ID")
        if cid not in cuts:
            raise ValueError(f"不明なカット {cid!r}。使える値: {', '.join(cuts)}")

    if a.with_draft:
        drafts = plan_jobs(project, cuts, ids, a.model, None, True, a.force, a.variants, estimate_only=True)
        finals = plan_jobs(project, cuts, ids, a.model, a.resolution, False, a.force, estimate_only=True)
        _warn_deprecated(drafts + finals)
        total = print_plan("下書き", drafts) + print_plan("本番", finals)
        print(f"合計（下書き + 本番）: ${total:.2f}")
        return

    jobs = plan_jobs(project, cuts, ids, a.model, a.resolution, a.draft, a.force, a.variants, estimate_only=a.dry_run)
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
            except Exception as e:  # 全カットの結果を出してから、まとめて失敗にする
                failures.append(f"{futs[f].name}: {common.scrub(str(e))}")
    if failures:
        for msg in failures:
            print("失敗 " + msg, file=sys.stderr)
        raise RuntimeError(f"{len(todo)} カット中 {len(failures)} カットが失敗")


def _warn_deprecated(jobs: list[Job]) -> None:
    for model in sorted({j.model for j in jobs}):
        w = deprecation_warning(model)
        if w:
            print(w, file=sys.stderr)


if __name__ == "__main__":
    common.cli(main)
