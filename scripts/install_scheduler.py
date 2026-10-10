#!/usr/bin/env python3
"""Install a macOS launchd job that runs `threads_post.py publish-due` every 15 minutes.

Default is a dry run: it prints the plist it would write and the launchctl commands.
  --apply      write ~/Library/LaunchAgents/<label>.plist and `launchctl bootstrap` it (re-installs if present)
  --uninstall  `launchctl bootout` the job and remove the plist (with --apply)

Notes
  - launchd does not see the plugin settings entered in Claude, so the job reads the local config file
    (scripts/setup.py writes it). The Threads token (and media_bucket for local videos) must be there.
  - The job does not run from the (versioned) plugin folder: the scripts are copied to a stable runner folder,
    ~/.config/threads-video-studio/runner/, and the plist points there. Re-run with --apply after a plugin
    update to refresh the copy.
  - macOS may block background access to ~/Desktop, ~/Documents and ~/Downloads; keep the workspace elsewhere
    (default ~/threads-videos) or grant Full Disk Access to the Python binary.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "templates" / "launchd" / "com.threads-video-studio.publish.plist.template"
LABEL = "com.threads-video-studio.publish"
RUNNER_DIR = common.CONFIG_DIR / "runner"
RUNNER_FILES = ("common.py", "threads_api.py", "threads_post.py", "media_host.py")
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def render_plist(python: str, interval: int, max_n: int, log: Path) -> str:
    values = {"__LABEL__": LABEL, "__PYTHON__": python, "__SCRIPT__": str(RUNNER_DIR / "threads_post.py"),
              "__MAX__": str(max_n), "__PATH__": PATH, "__CONFIG_DIR__": str(common.CONFIG_DIR),
              "__INTERVAL__": str(interval), "__LOG__": str(log)}
    text = TEMPLATE.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace(k, escape(v))
    return text


def preflight() -> list[str]:
    warns = []
    cfg = common.load_file()
    if not cfg.get("threads_access_token"):
        warns.append("the config file has no threads_access_token - launchd cannot see plugin settings; "
                     "run: python3 scripts/setup.py set threads_access_token")
    if not cfg.get("media_bucket"):
        warns.append("the config file has no media_bucket - posts with a local video_file will fail "
                     "(or give --media-url when drafting)")
    ws = str(common.workspace())
    home = str(Path.home())
    if any(ws.startswith(f"{home}/{d}") for d in ("Desktop", "Documents", "Downloads")):
        warns.append(f"workspace {ws} is in a privacy-protected folder; background jobs may be denied access")
    return warns


def install_runner(dest: Path = RUNNER_DIR) -> list[Path]:
    """Copy the scripts the job needs to a stable folder (atomic per file). Returns the copied paths."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in RUNNER_FILES:
        src = HERE / name
        tmp = dest / (name + ".tmp")
        tmp.write_bytes(src.read_bytes())
        tmp.replace(dest / name)
        if (dest / name).read_bytes() != src.read_bytes():
            raise RuntimeError(f"runner copy readback failed: {name}")
        out.append(dest / name)
    return out


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"launchctl {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the 15-minute publish-due launchd job (macOS). "
                                             "Dry run by default.")
    ap.add_argument("--apply", action="store_true", help="actually write the plist and bootstrap it")
    ap.add_argument("--uninstall", action="store_true", help="remove the job (with --apply)")
    ap.add_argument("--interval", type=int, default=900, help="seconds between runs (default 900)")
    ap.add_argument("--max", type=int, default=3, help="max posts per run (default 3)")
    ap.add_argument("--python", default=sys.executable, help="python3 to run the job with")
    a = ap.parse_args(argv)
    if sys.platform != "darwin":
        print("error: launchd is macOS only - use templates/cowork-scheduled-task.md or cron instead", file=sys.stderr)
        return 1
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    log = Path.home() / "Library" / "Logs" / "threads-video-studio" / "publish.log"
    domain = f"gui/{os.getuid()}"

    if a.uninstall:
        if not a.apply:
            print(f"[dry-run] launchctl bootout {domain}/{LABEL}\n[dry-run] rm {plist}")
            return 0
        launchctl("bootout", f"{domain}/{LABEL}", check=False)
        plist.unlink(missing_ok=True)
        print(f"removed {LABEL}")
        return 0

    text = render_plist(a.python, a.interval, a.max, log)
    for w in preflight():
        print(f"warning: {w}")
    if not a.apply:
        print(f"[dry-run] would write {plist}:\n{text}")
        print(f"[dry-run] copy {', '.join(RUNNER_FILES)} -> {RUNNER_DIR}/")
        print(f"[dry-run] launchctl bootout {domain}/{LABEL} (if loaded)")
        print(f"[dry-run] launchctl bootstrap {domain} {plist}")
        print("re-run with --apply to install")
        return 0
    try:
        install_runner()
        plist.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        tmp = plist.with_suffix(".plist.tmp")
        tmp.write_text(text, encoding="utf-8")
        subprocess.run(["plutil", "-lint", str(tmp)], check=True, capture_output=True)
        tmp.replace(plist)
        launchctl("bootout", f"{domain}/{LABEL}", check=False)
        launchctl("bootstrap", domain, str(plist))
        launchctl("print", f"{domain}/{LABEL}")
    except (RuntimeError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"installed {LABEL}: every {a.interval}s, runner {RUNNER_DIR}, log {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
