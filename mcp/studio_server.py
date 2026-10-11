#!/usr/bin/env python3
"""threads-video-studio の MCP サーバー（標準ライブラリのみ）。

利用者のパソコン上で、次のどちらかとして動く:
  - Claude Desktop / Cowork の拡張機能（MCP Bundle。設定は「カスタマイズ > コネクタ」で入力）
  - Claude Code プラグインのコネクタ（.mcp.json。設定は /plugin > Configure options で入力）

有料 API を呼ぶツールはすべてここ（利用者のパソコン）で、設定画面のキーを使って動く。
そのためキーをチャット・サンドボックス・共有フォルダにコピーする必要が無い。
ツールはパスではなくプロジェクトの *スラッグ*（ワークスペース内のフォルダ名）を受け取るので、
Claude Code からも Cowork のサンドボックスからも同じ呼び方で使える。

時間のかかる処理（動画生成・書き出し・投稿）はバックグラウンドのジョブとして動かし、studio_job で
進み具合を見る。秘密値は返さない。出力はすべて common.redact と同じやり方で秘密値を消してから返す。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("TVS_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import common  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "0.4.0"
OPT_PREFIX = "TVS_OPT_"


# ---------------------------------------------------------------- 設定
def configured() -> dict[str, str]:
    """設定画面で入力された値（未入力のプレースホルダーは無視する）。"""
    out = {}
    for key in common.SETTINGS:
        v = os.environ.get(OPT_PREFIX + key.upper(), "").strip()
        if v and not v.startswith("${"):
            out[key] = v
    return out


def child_env() -> dict[str, str]:
    """スクリプトに渡す環境変数。設定画面の値を common.get が読む名前で渡す。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(OPT_PREFIX)}
    for key, v in configured().items():
        names = common.SETTINGS[key][0]
        if names:
            env[names[0]] = v
    env["PYTHONUNBUFFERED"] = "1"
    return env


def scrub(text: str) -> str:
    for key, v in configured().items():
        if common.SETTINGS[key][1] and len(v) >= 8:  # 秘密値だけ
            text = text.replace(v, "***")
    fn = getattr(common, "scrub", None)
    return fn(text) if callable(fn) else text


def workspace() -> Path:
    ws = configured().get("workspace") or common.get("workspace") or str(Path.home() / "threads-videos")
    p = Path(os.path.expanduser(ws))
    p.mkdir(parents=True, exist_ok=True)
    return p


def project(slug: str) -> Path:
    check = getattr(common, "check_id", None)
    if callable(check):
        check(slug, "スラッグ")
    elif not slug or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in slug):
        raise ValueError("スラッグは ^[A-Za-z0-9_-]{1,64}$ に合う形にする")
    return workspace() / slug


# ---------------------------------------------------------------- スクリプトの実行
def run_script(script: str, args: list[str], timeout: int = 900) -> str:
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=child_env(), cwd=str(workspace()), timeout=timeout)
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    out = scrub(out.strip())[-12000:]
    return (out or "(出力なし)") + ("" if r.returncode == 0 else f"\n[終了コード {r.returncode}]")


def jobs_dir() -> Path:
    d = workspace() / ".jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start_job(script: str, args: list[str], label: str) -> str:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    log = jobs_dir() / f"{jid}.log"
    meta = jobs_dir() / f"{jid}.json"
    with open(log, "w") as fh:
        p = subprocess.Popen([sys.executable, str(SCRIPTS / script), *args], stdout=fh, stderr=subprocess.STDOUT,
                             env=child_env(), cwd=str(workspace()), start_new_session=True)
    meta.write_text(json.dumps({"id": jid, "pid": p.pid, "label": label, "script": script, "args": args,
                                "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
    return f"ジョブ {jid}（{label}）を開始した。進み具合は studio_job(job_id=\"{jid}\") で確認する。"


def job_status(job_id: str = "") -> str:
    d = jobs_dir()
    if not job_id:
        rows = []
        for m in sorted(d.glob("*.json"))[-15:]:
            j = json.loads(m.read_text())
            rows.append(f"{j['id']}  {'実行中' if alive(j['pid']) else '終了'}  {j['label']}")
        return "\n".join(rows) or "ジョブは無い"
    if any(c not in "0123456789abcdef-" for c in job_id):
        raise ValueError("ジョブ ID が不正")
    meta, log = d / f"{job_id}.json", d / f"{job_id}.log"
    if not meta.exists():
        return f"不明なジョブ {job_id}"
    j = json.loads(meta.read_text())
    tail = log.read_text(errors="replace")[-6000:] if log.exists() else ""
    return f"{j['label']}: {'実行中' if alive(j['pid']) else '終了'}\n{scrub(tail)}"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:  # 終わった自分の子プロセスなら回収する
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


# ---------------------------------------------------------------- ツール
def t_status(_):
    cfg = configured()
    lines = [f"ワークスペース: {workspace()}", "設定:"]
    for key, (_, secret, desc) in common.SETTINGS.items():
        v = cfg.get(key) or common.get(key)
        lines.append(f"- {key}: {('設定済み' if v else '(未設定)') if secret else (v or '(未設定)')}  — {desc}")
    lines.append("このパソコンのツール:")
    for tool in ("ffmpeg", "ffprobe", "npx", "aws"):
        lines.append(f"- {tool}: {'あり' if shutil.which(tool) else '無い'}")
    return "\n".join(lines)


def t_save_settings(_):
    cfg = configured()
    if not cfg:
        return "保存するものが無い: 設定画面に値が入力されていない。"
    data = common.load_file()
    data.update(cfg)
    common.save_file(data)
    return f"{len(cfg)} 件の設定を {common.CONFIG_FILE}（本人だけが読める）に保存した: {', '.join(sorted(cfg))}"


def t_paths(_):
    return json.dumps({"plugin_root": str(ROOT), "scripts": str(SCRIPTS), "workspace": str(workspace()),
                       "config_file": str(common.CONFIG_FILE)}, indent=1)


def t_new_project(a):
    project(a["slug"])
    args = [a["slug"], "--type", a.get("type", "story"), "--workspace", str(workspace())]
    if a.get("cuts"):
        args += ["--cuts", str(int(a["cuts"]))]
    return run_script("new_project.py", args)


def t_list_projects(_):
    ws = workspace()
    if not ws.exists():
        return f"ワークスペース {ws} はまだ無い"
    rows = []
    for p in sorted(ws.iterdir()):
        if (p / "shots.json").exists():
            finals = sorted((p / "final").glob("*.mp4")) if (p / "final").exists() else []
            rows.append(f"{p.name}  clips={len(list((p / 'clips').glob('*.mp4'))) if (p / 'clips').exists() else 0}"
                        f"  final={'あり' if finals else 'なし'}")
    return "\n".join(rows) or "プロジェクトは無い"


def t_read_shots(a):
    return (project(a["slug"]) / "shots.json").read_text()


def t_write_shots(a):
    data = a["shots"] if isinstance(a["shots"], dict) else json.loads(a["shots"])
    if not isinstance(data.get("cuts"), list):
        raise ValueError("shots には 'cuts' のリストが必要")
    p = project(a["slug"]) / "shots.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)
    return f"保存した: {p}（{len(data['cuts'])} カット）"


def t_write_text(a):
    allowed = {"brief.md", "world.md", "voice/script.txt", "voice/yomi.json", "post.txt", "post-thread.txt"}
    if a["file"] not in allowed:
        raise ValueError(f"file は {sorted(allowed)} のどれかにする")
    p = project(a["slug"]) / a["file"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a["text"])
    return f"保存した: {p}"


def t_gen_image(a):
    args = ["--project", a["slug"], "--prompt", a["prompt"], "--out", a["out"], "--model", a.get("model", "seedream45")]
    for img in a.get("images", []) or []:
        args += ["--image", img]
    if a.get("size"):
        args += ["--size", a["size"]]
    if a.get("dry_run"):
        args.append("--dry-run")
    if a.get("force"):
        args.append("--force")
    return run_script("gen_image.py", args, timeout=600)


def t_gen_video(a):
    args = ["--project", a["slug"]]
    args += ["--all"] if a.get("all") or not a.get("cut") else ["--cut", a["cut"]]
    for flag in ("draft", "dry_run", "with_draft", "force"):
        if a.get(flag):
            args.append("--" + flag.replace("_", "-"))
    if a.get("variants"):
        args += ["--variants", str(int(a["variants"]))]
    if a.get("model"):
        args += ["--model", a["model"]]
    if a.get("dry_run"):
        return run_script("gen_video.py", args, timeout=120)
    return start_job("gen_video.py", args, f"gen_video {a['slug']} {a.get('cut') or 'all'}{' draft' if a.get('draft') else ''}")


def t_yomi(a):
    return run_script("yomi_check.py", ["--project", a["slug"]] + (["--suggest"] if a.get("suggest") else []), 120)


def t_gen_voice(a):
    args = ["--project", a["slug"]]
    if a.get("voice_id"):
        args += ["--voice-id", a["voice_id"]]
    if a.get("tag"):
        args += ["--tag", a["tag"]]
    for flag in ("dry_run", "retime", "force"):
        if a.get(flag):
            args.append("--" + flag.replace("_", "-"))
    return run_script("gen_voice.py", args, timeout=600)


def t_gen_bgm(a):
    args = ["--project", a["slug"], "--prompt", a["prompt"], "--seconds", str(int(a.get("seconds", 45)))]
    if a.get("provider"):
        args += ["--provider", a["provider"]]
    if a.get("count"):
        args += ["--count", str(int(a["count"]))]
    if a.get("dry_run"):
        args.append("--dry-run")
    return run_script("gen_bgm.py", args, timeout=600)


def t_qa(a):
    args = ["--project", a["slug"]]
    if a.get("final"):
        args.append("--final")
    elif a.get("cut"):
        args += ["--cut", a["cut"]]
    else:
        args.append("--all")
    return run_script("qa_frames.py", args, timeout=600)


def t_cuesheet(a):
    args = ["--project", str(project(a["slug"]))]
    act = a.get("action", "export")
    if act == "check":
        args.append("--check")
    elif act == "import":
        args += ["--import", a.get("file") or "cuesheet.xlsx"]
    elif act != "export":
        raise ValueError("action は export・check・import のどれかにする")
    return run_script("cuesheet.py", args, timeout=120)


def t_assemble(a):
    args = ["--project", a["slug"]] + (["--render"] if a.get("render", True) else [])
    return start_job("assemble.py", args, f"assemble {a['slug']}") if a.get("render", True) else run_script("assemble.py", args)


def t_post(a):
    cmd = a["action"]
    ws = ["--workspace", str(workspace())]
    if cmd == "status":
        return run_script("threads_post.py", [*ws, "status"])
    if cmd == "draft":
        args = [*ws, "draft", "--project", str(project(a["slug"])), "--text-file", str(project(a["slug"]) / "post.txt")]
        for k in ("topic", "at", "hook", "format"):
            if a.get(k):
                args += ["--" + k, a[k]]
        return run_script("threads_post.py", args)
    if cmd == "approve":
        if not a.get("by") or not a.get("note"):
            raise ValueError("approve には 'by' と 'note'（本人が書いた承認の言葉）が必要")
        return run_script("threads_post.py", [*ws, "approve", a["slug"], "--by", a["by"], "--note", a["note"]])
    if cmd == "schedule":
        return run_script("threads_post.py", [*ws, "schedule", a["slug"], "--at", a["at"]])
    if cmd == "publish_due":
        args = [*ws, "publish-due"] + (["--dry-run"] if a.get("dry_run") else []) + ["--max", str(int(a.get("max", 1)))]
        return run_script("threads_post.py", args) if a.get("dry_run") else start_job("threads_post.py", args, "publish-due")
    raise ValueError("action は status・draft・approve・schedule・publish_due のどれかにする")


def t_insights(a):
    return run_script("threads_insights.py", ["--workspace", str(workspace()), a.get("action", "pull")], timeout=600)


def t_job(a):
    return job_status(a.get("job_id", ""))


S = {"type": "string"}
B = {"type": "boolean"}
N = {"type": "integer"}
SLUG = {"type": "string", "description": "ワークスペース内のプロジェクトのフォルダ名。例 t004-camp"}


def tool(name, desc, props=None, required=None, fn=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or []}}, fn


TOOLS = dict((t["name"], (t, f)) for t, f in [
    tool("studio_status", "どの設定が入力済みか（秘密値は設定済みかどうかだけ）、ワークスペース、ffmpeg・npx・aws が入っているかを表示する。", fn=t_status),
    tool("studio_save_settings", "設定画面の値を、本人だけが読めるローカルの設定ファイルにコピーする（シェルのスクリプトからも使えるようにするため）。返すのはキーの名前だけ。", fn=t_save_settings),
    tool("studio_paths", "このパソコン上のプラグイン・スクリプト・ワークスペース・設定ファイルのパスを返す。", fn=t_paths),
    tool("studio_new_project", "brief.md と shots.json を入れた動画プロジェクトのフォルダを作る。", {"slug": SLUG, "type": {"type": "string", "enum": ["short", "story", "intro"]}, "cuts": N}, ["slug"], t_new_project),
    tool("studio_list_projects", "ワークスペースのプロジェクトを、クリップ数と完成動画の有無付きで一覧にする。", fn=t_list_projects),
    tool("studio_read_shots", "プロジェクトの shots.json（カット・プロンプト・タイミング）を読む。", {"slug": SLUG}, ["slug"], t_read_shots),
    tool("studio_write_shots", "プロジェクトの shots.json を置き換える。オブジェクト全体を渡す。", {"slug": SLUG, "shots": {"type": "object"}}, ["slug", "shots"], t_write_shots),
    tool("studio_write_text", "プロジェクトの brief.md・world.md（世界観設計）・voice/script.txt（1カット1行のナレーション）・voice/yomi.json・post.txt（フック＝1投稿目）・post-thread.txt（ツリーの本文＝2投稿目以降）を書く。", {"slug": SLUG, "file": {"type": "string", "enum": ["brief.md", "world.md", "voice/script.txt", "voice/yomi.json", "post.txt", "post-thread.txt"]}, "text": S}, ["slug", "file", "text"], t_write_text),
    tool("studio_gen_image", "有料。画像を生成・編集する（絵コンテ・キャラクターシート・開始フレーム）。パスはプロジェクト基準。料金は dry_run で確かめる。", {"slug": SLUG, "prompt": S, "out": S, "model": {"type": "string", "enum": ["seedream45", "nano-banana-pro"]}, "images": {"type": "array", "items": S}, "size": S, "dry_run": B, "force": B}, ["slug", "prompt", "out"], t_gen_image),
    tool("studio_gen_video", "有料。開始フレームから動画のカットを生成する（バックグラウンドのジョブ。途中から再開できる）。承認の前に dry_run（+with_draft）で見積もる。", {"slug": SLUG, "cut": S, "all": B, "draft": B, "variants": N, "model": S, "dry_run": B, "with_draft": B, "force": B}, ["slug"], t_gen_video),
    tool("studio_yomi_check", "生成の前にナレーションの読みを確かめる。suggest=true で voice/yomi.json に読みの候補を追記する。", {"slug": SLUG, "suggest": B}, ["slug"], t_yomi),
    tool("studio_gen_voice", "有料（少額）。ナレーション全体をタイムスタンプ付きで1回で生成し、カットのタイミングを shots.json に書く。retime=true ならタイミングだけ無料で計算し直す。", {"slug": SLUG, "voice_id": S, "tag": S, "dry_run": B, "retime": B, "force": B}, ["slug"], t_gen_voice),
    tool("studio_gen_bgm", "有料。歌なしの BGM の候補を生成する。", {"slug": SLUG, "prompt": S, "seconds": N, "provider": {"type": "string", "enum": ["elevenlabs", "fal"]}, "count": N, "dry_run": B}, ["slug", "prompt"], t_gen_bgm),
    tool("studio_qa", "1カット・全カット・完成動画について、確認用のコンタクトシートを作り、止まったフレームや不自然なつなぎ目を見つける。", {"slug": SLUG, "cut": S, "final": B}, ["slug"], t_qa),
    tool("studio_cuesheet", "無料。shots.json から、1行1カットで何秒に何を読み上げ・何を映し・素材がどこまでできたかを並べたキューシート（<作品>/cuesheet.xlsx）を作る（export）。check は抜け・尺のずれ・早口だけを確認する。import は人が書いた備考・ご指示などを shots.json に取り込む（file はプロジェクト基準のパス）。", {"slug": SLUG, "action": {"type": "string", "enum": ["export", "check", "import"]}, "file": S}, ["slug"], t_cuesheet),
    tool("studio_assemble", "字幕・BGM・CTA を組み立て、final/<slug>-final.mp4 と -post.mp4 を書き出す（バックグラウンドのジョブ）。", {"slug": SLUG, "render": B}, ["slug"], t_assemble),
    tool("studio_post", "Threads の投稿キュー。action=status|draft|approve|schedule|publish_due。'approve' は本人がチャットで明示的に OK と言ったときだけ使い、その言葉を note に渡す。投稿は取り消せない。", {"action": {"type": "string", "enum": ["status", "draft", "approve", "schedule", "publish_due"]}, "slug": SLUG, "topic": S, "at": S, "hook": S, "format": S, "by": S, "note": S, "dry_run": B, "max": N}, ["action"], t_post),
    tool("studio_insights", "Threads の数値を取得する（pull）か、48時間・7日の振り返りを書く（review）。", {"action": {"type": "string", "enum": ["pull", "review"]}}, [], t_insights),
    tool("studio_job", "バックグラウンドのジョブの進み具合とログの末尾を表示する。job_id が空なら最近のジョブを一覧にする。", {"job_id": S}, [], t_job),
])


# ---------------------------------------------------------------- JSON-RPC
def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        method, mid = req.get("method"), req.get("id")
        if mid is None:
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "threads-video-studio", "version": VERSION}})
        elif method == "tools/list":
            reply(mid, {"tools": [t for t, _ in TOOLS.values()]})
        elif method == "tools/call":
            p = req.get("params", {})
            entry = TOOLS.get(p.get("name"))
            if not entry:
                reply(mid, error={"code": -32602, "message": f"不明なツール {p.get('name')}"})
                continue
            try:
                args = p.get("arguments") or {}
                if "slug" in args:
                    project(str(args["slug"]))  # 作業の前に、単純なフォルダ名以外を拒む
                text = entry[1](args)
                reply(mid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # noqa: BLE001
                reply(mid, {"content": [{"type": "text", "text": f"エラー: {scrub(str(e))}"}], "isError": True})
        elif method == "ping":
            reply(mid, {})
        else:
            reply(mid, error={"code": -32601, "message": f"メソッドが無い: {method}"})


if __name__ == "__main__":
    main()
