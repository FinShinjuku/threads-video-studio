#!/usr/bin/env python3
"""Minimal stdio MCP server for threads-video-studio (standard library only).

Why it exists: plugin settings entered in Claude's configuration screen reach MCP servers
(through ${user_config.*} in .mcp.json) but are not guaranteed to reach shell commands that
Claude runs. This server bridges the two:

- studio_status         which settings and tools are available (secrets are masked)
- studio_save_settings  copy the configured settings into the local config file (mode 0600)
                        that the scripts read, so `python3 scripts/*.py` works from the shell
- studio_paths          plugin root, scripts folder, config file and workspace paths

It never returns a secret value.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("TVS_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402

PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "studio_status",
     "description": "Show which threads-video-studio settings are set (masked) and whether ffmpeg, npx, aws and pyopenjtalk are available.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "studio_save_settings",
     "description": "Copy the settings entered in the plugin configuration screen into the local config file (0600) so the plugin scripts can read them. Returns only which keys were saved.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "studio_paths",
     "description": "Return the plugin root, scripts folder, config file path and workspace folder.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def configured() -> dict[str, str]:
    out = {}
    for key in common.SETTINGS:
        v = os.environ.get(f"TVS_OPT_{key.upper()}", "").strip()
        if v and not v.startswith("${"):
            out[key] = v
    return out


def status() -> str:
    cfg = configured()
    file_cfg = common.load_file()
    lines = ["Settings (plugin screen / config file):"]
    for key, (_, secret, desc) in common.SETTINGS.items():
        a = cfg.get(key, "")
        b = str(file_cfg.get(key, ""))
        show = common.mask if secret else (lambda v: v or "(empty)")
        lines.append(f"- {key}: screen={show(a)} | file={show(b)}  — {desc}")
    lines.append("Tools:")
    for tool in ("ffmpeg", "ffprobe", "npx", "aws"):
        lines.append(f"- {tool}: {'ok' if shutil.which(tool) else 'missing'}")
    try:
        import pyopenjtalk  # noqa: F401
        lines.append("- pyopenjtalk: ok")
    except ImportError:
        lines.append("- pyopenjtalk: missing (pip install pyopenjtalk) — reading check falls back to a basic scan")
    return "\n".join(lines)


def save_settings() -> str:
    cfg = configured()
    if not cfg:
        return "Nothing to save: no values were entered in the plugin configuration screen."
    data = common.load_file()
    data.update(cfg)
    common.save_file(data)
    return f"Saved {len(cfg)} setting(s) to {common.CONFIG_FILE}: {', '.join(sorted(cfg))}"


def paths() -> str:
    return json.dumps({"plugin_root": str(ROOT), "scripts": str(ROOT / "scripts"),
                       "config_file": str(common.CONFIG_FILE), "workspace": str(common.workspace())}, indent=1)


HANDLERS = {"studio_status": status, "studio_save_settings": save_settings, "studio_paths": paths}


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
        if mid is None:  # notification
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "threads-video-studio", "version": "0.1.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = req.get("params", {}).get("name")
            fn = HANDLERS.get(name)
            if not fn:
                reply(mid, error={"code": -32602, "message": f"unknown tool {name}"})
                continue
            try:
                reply(mid, {"content": [{"type": "text", "text": fn()}]})
            except Exception as e:  # noqa: BLE001
                reply(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif method == "ping":
            reply(mid, {})
        else:
            reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
