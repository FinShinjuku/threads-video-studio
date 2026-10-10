#!/usr/bin/env python3
"""threads-video-studio MCP server (standard library only).

Runs on the user's computer, either as
  - the Claude Desktop / Cowork extension (MCP Bundle, settings entered in Customize > Connectors), or
  - the Claude Code plugin connector (.mcp.json, settings entered in /plugin > Configure options).

Every tool that calls a paid API runs here, on the host, with the keys from the settings form,
so the keys never have to be copied into a chat, a sandbox or a shared folder.
Tools take a project *slug* (folder name under the workspace), not a path, so the same call works
from Claude Code and from Cowork's sandbox.

Long operations (video generation, rendering, posting) run as background jobs; poll them with
studio_job. Secrets are never returned: all output passes through common.redact-style scrubbing.
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
VERSION = "0.2.0"
OPT_PREFIX = "TVS_OPT_"


# ---------------------------------------------------------------- settings
def configured() -> dict[str, str]:
    """Values entered in the settings form (unfilled placeholders are ignored)."""
    out = {}
    for key in common.SETTINGS:
        v = os.environ.get(OPT_PREFIX + key.upper(), "").strip()
        if v and not v.startswith("${"):
            out[key] = v
    return out


def child_env() -> dict[str, str]:
    """Environment for the scripts: settings form values exported under the names common.get reads."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(OPT_PREFIX)}
    for key, v in configured().items():
        names = common.SETTINGS[key][0]
        if names:
            env[names[0]] = v
    env["PYTHONUNBUFFERED"] = "1"
    return env


def scrub(text: str) -> str:
    for key, v in configured().items():
        if common.SETTINGS[key][1] and len(v) >= 8:  # secrets only
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
        check(slug, "slug")
    elif not slug or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in slug):
        raise ValueError("slug must match ^[A-Za-z0-9_-]{1,64}$")
    return workspace() / slug


# ---------------------------------------------------------------- running scripts
def run_script(script: str, args: list[str], timeout: int = 900) -> str:
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=child_env(), cwd=str(workspace()), timeout=timeout)
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    out = scrub(out.strip())[-12000:]
    return (out or "(no output)") + ("" if r.returncode == 0 else f"\n[exit {r.returncode}]")


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
    return f"started job {jid} ({label}). Check progress with studio_job(job_id=\"{jid}\")."


def job_status(job_id: str = "") -> str:
    d = jobs_dir()
    if not job_id:
        rows = []
        for m in sorted(d.glob("*.json"))[-15:]:
            j = json.loads(m.read_text())
            rows.append(f"{j['id']}  {'running' if alive(j['pid']) else 'finished'}  {j['label']}")
        return "\n".join(rows) or "no jobs"
    if any(c not in "0123456789abcdef-" for c in job_id):
        raise ValueError("bad job id")
    meta, log = d / f"{job_id}.json", d / f"{job_id}.log"
    if not meta.exists():
        return f"unknown job {job_id}"
    j = json.loads(meta.read_text())
    tail = log.read_text(errors="replace")[-6000:] if log.exists() else ""
    return f"{j['label']}: {'running' if alive(j['pid']) else 'finished'}\n{scrub(tail)}"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:  # reap if it is our finished child
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


# ---------------------------------------------------------------- tools
def t_status(_):
    cfg = configured()
    lines = [f"workspace: {workspace()}", "settings:"]
    for key, (_, secret, desc) in common.SETTINGS.items():
        v = cfg.get(key) or common.get(key)
        lines.append(f"- {key}: {('set' if v else '(empty)') if secret else (v or '(empty)')}  — {desc}")
    lines.append("tools on this computer:")
    for tool in ("ffmpeg", "ffprobe", "npx", "aws"):
        lines.append(f"- {tool}: {'ok' if shutil.which(tool) else 'missing'}")
    return "\n".join(lines)


def t_save_settings(_):
    cfg = configured()
    if not cfg:
        return "Nothing to save: no values were entered in the settings form."
    data = common.load_file()
    data.update(cfg)
    common.save_file(data)
    return f"Saved {len(cfg)} setting(s) to {common.CONFIG_FILE} (owner-only): {', '.join(sorted(cfg))}"


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
        return f"workspace {ws} does not exist yet"
    rows = []
    for p in sorted(ws.iterdir()):
        if (p / "shots.json").exists():
            finals = sorted((p / "final").glob("*.mp4")) if (p / "final").exists() else []
            rows.append(f"{p.name}  clips={len(list((p / 'clips').glob('*.mp4'))) if (p / 'clips').exists() else 0}"
                        f"  final={'yes' if finals else 'no'}")
    return "\n".join(rows) or "no projects"


def t_read_shots(a):
    return (project(a["slug"]) / "shots.json").read_text()


def t_write_shots(a):
    data = a["shots"] if isinstance(a["shots"], dict) else json.loads(a["shots"])
    if not isinstance(data.get("cuts"), list):
        raise ValueError("shots must contain a 'cuts' list")
    p = project(a["slug"]) / "shots.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)
    return f"saved {p} ({len(data['cuts'])} cuts)"


def t_write_text(a):
    allowed = {"brief.md", "voice/script.txt", "voice/yomi.json", "post.txt"}
    if a["file"] not in allowed:
        raise ValueError(f"file must be one of {sorted(allowed)}")
    p = project(a["slug"]) / a["file"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a["text"])
    return f"saved {p}"


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
            raise ValueError("approve needs 'by' and 'note' (the person's own words of approval)")
        return run_script("threads_post.py", [*ws, "approve", a["slug"], "--by", a["by"], "--note", a["note"]])
    if cmd == "schedule":
        return run_script("threads_post.py", [*ws, "schedule", a["slug"], "--at", a["at"]])
    if cmd == "publish_due":
        args = [*ws, "publish-due"] + (["--dry-run"] if a.get("dry_run") else []) + ["--max", str(int(a.get("max", 1)))]
        return run_script("threads_post.py", args) if a.get("dry_run") else start_job("threads_post.py", args, "publish-due")
    raise ValueError("action must be one of status, draft, approve, schedule, publish_due")


def t_insights(a):
    return run_script("threads_insights.py", ["--workspace", str(workspace()), a.get("action", "pull")], timeout=600)


def t_job(a):
    return job_status(a.get("job_id", ""))


S = {"type": "string"}
B = {"type": "boolean"}
N = {"type": "integer"}
SLUG = {"type": "string", "description": "project folder name under the workspace, e.g. t004-camp"}


def tool(name, desc, props=None, required=None, fn=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or []}}, fn


TOOLS = dict((t["name"], (t, f)) for t, f in [
    tool("studio_status", "Show which settings are entered (secrets shown only as set/empty), the workspace, and whether ffmpeg/npx/aws are installed.", fn=t_status),
    tool("studio_save_settings", "Copy the settings form values into the owner-only local config file so shell scripts can use them too. Returns only key names.", fn=t_save_settings),
    tool("studio_paths", "Return plugin, scripts, workspace and config file paths on this computer.", fn=t_paths),
    tool("studio_new_project", "Create a video project folder with brief.md and shots.json.", {"slug": SLUG, "type": {"type": "string", "enum": ["story", "intro"]}, "cuts": N}, ["slug"], t_new_project),
    tool("studio_list_projects", "List projects in the workspace with clip and final-video status.", fn=t_list_projects),
    tool("studio_read_shots", "Read a project's shots.json (cuts, prompts, timing).", {"slug": SLUG}, ["slug"], t_read_shots),
    tool("studio_write_shots", "Replace a project's shots.json. Pass the whole object.", {"slug": SLUG, "shots": {"type": "object"}}, ["slug", "shots"], t_write_shots),
    tool("studio_write_text", "Write brief.md, voice/script.txt (one narration line per cut), voice/yomi.json or post.txt in a project.", {"slug": SLUG, "file": {"type": "string", "enum": ["brief.md", "voice/script.txt", "voice/yomi.json", "post.txt"]}, "text": S}, ["slug", "file", "text"], t_write_text),
    tool("studio_gen_image", "PAID. Generate or edit an image (storyboard, character sheet, start frame). Paths are relative to the project. Use dry_run for the price.", {"slug": SLUG, "prompt": S, "out": S, "model": {"type": "string", "enum": ["seedream45", "nano-banana-pro"]}, "images": {"type": "array", "items": S}, "size": S, "dry_run": B, "force": B}, ["slug", "prompt", "out"], t_gen_image),
    tool("studio_gen_video", "PAID. Generate video cuts from start frames (background job; resumable). Use dry_run (+with_draft) for the estimate before approval.", {"slug": SLUG, "cut": S, "all": B, "draft": B, "variants": N, "model": S, "dry_run": B, "with_draft": B, "force": B}, ["slug"], t_gen_video),
    tool("studio_yomi_check", "Check narration readings before generating; suggest=true appends suggestions to voice/yomi.json.", {"slug": SLUG, "suggest": B}, ["slug"], t_yomi),
    tool("studio_gen_voice", "PAID (small). Generate the whole narration in one pass with timestamps and write cut timing into shots.json. retime=true recomputes timing for free.", {"slug": SLUG, "voice_id": S, "tag": S, "dry_run": B, "retime": B, "force": B}, ["slug"], t_gen_voice),
    tool("studio_gen_bgm", "PAID. Generate instrumental BGM candidates.", {"slug": SLUG, "prompt": S, "seconds": N, "provider": {"type": "string", "enum": ["elevenlabs", "fal"]}, "count": N, "dry_run": B}, ["slug", "prompt"], t_gen_bgm),
    tool("studio_qa", "Make QA contact sheets and detect frozen frames / harsh seams for a cut, all cuts, or the final video.", {"slug": SLUG, "cut": S, "final": B}, ["slug"], t_qa),
    tool("studio_assemble", "Assemble captions, BGM and CTA and render final/<slug>-final.mp4 and -post.mp4 (background job).", {"slug": SLUG, "render": B}, ["slug"], t_assemble),
    tool("studio_post", "Threads posting queue. action=status|draft|approve|schedule|publish_due. 'approve' ONLY after the person explicitly said OK in chat; pass their words as note. Posting cannot be undone.", {"action": {"type": "string", "enum": ["status", "draft", "approve", "schedule", "publish_due"]}, "slug": SLUG, "topic": S, "at": S, "hook": S, "format": S, "by": S, "note": S, "dry_run": B, "max": N}, ["action"], t_post),
    tool("studio_insights", "Pull Threads metrics (pull) or write the 48h/7d review (review).", {"action": {"type": "string", "enum": ["pull", "review"]}}, [], t_insights),
    tool("studio_job", "Show a background job's progress and log tail, or list recent jobs when job_id is empty.", {"job_id": S}, [], t_job),
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
                reply(mid, error={"code": -32602, "message": f"unknown tool {p.get('name')}"})
                continue
            try:
                args = p.get("arguments") or {}
                if "slug" in args:
                    project(str(args["slug"]))  # reject anything but a plain folder name before doing work
                text = entry[1](args)
                reply(mid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # noqa: BLE001
                reply(mid, {"content": [{"type": "text", "text": f"error: {scrub(str(e))}"}], "isError": True})
        elif method == "ping":
            reply(mid, {})
        else:
            reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
