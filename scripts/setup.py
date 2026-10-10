#!/usr/bin/env python3
"""Show or change threads-video-studio settings from the shell.

  python3 setup.py status            # what is set (secrets masked) and where it comes from
  python3 setup.py set <key>         # prompt for a value without echoing it, save to the config file
  python3 setup.py import <file>     # merge a JSON file made with setup.html, then delete it if --delete
  python3 setup.py path              # print the config file path

Values are saved to ~/.config/threads-video-studio/config.json (mode 0600), or to the folder in
TVS_CONFIG_DIR. In Claude Code you can instead enter them in the plugin configuration screen and
run the `studio_save_settings` tool once.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def cmd_status() -> None:
    print(f"config file: {common.CONFIG_FILE} ({'exists' if common.CONFIG_FILE.exists() else 'not created'})")
    for key, (envs, secret, desc) in common.SETTINGS.items():
        v = common.get(key)
        shown = common.mask(v) if secret else (v or "(empty)")
        print(f"  {key:22} {shown:28} {desc}")


def cmd_set(key: str) -> None:
    if key not in common.SETTINGS:
        sys.exit(f"unknown key '{key}'. Known: {', '.join(common.SETTINGS)}")
    secret = common.SETTINGS[key][1]
    v = (getpass.getpass(f"{key}: ") if secret else input(f"{key}: ")).strip()
    data = common.load_file()
    if v:
        data[key] = v
    else:
        data.pop(key, None)
    common.save_file(data)
    print(f"saved {key}: {common.mask(v) if secret else (v or '(removed)')}")


def cmd_import(path: str, delete: bool) -> None:
    p = Path(os.path.expanduser(path))
    incoming = json.loads(p.read_text())
    unknown = [k for k in incoming if k not in common.SETTINGS]
    data = common.load_file()
    data.update({k: str(v).strip() for k, v in incoming.items() if k in common.SETTINGS and str(v).strip()})
    common.save_file(data)
    print(f"imported {len(incoming) - len(unknown)} key(s) into {common.CONFIG_FILE}"
          + (f"; ignored unknown: {', '.join(unknown)}" if unknown else ""))
    if delete:
        p.unlink()
        print(f"deleted {p}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("set"); s.add_argument("key")
    i = sub.add_parser("import"); i.add_argument("file"); i.add_argument("--delete", action="store_true")
    sub.add_parser("path")
    a = ap.parse_args()
    if a.cmd == "status":
        cmd_status()
    elif a.cmd == "set":
        cmd_set(a.key)
    elif a.cmd == "import":
        cmd_import(a.file, a.delete)
    else:
        print(common.CONFIG_FILE)


if __name__ == "__main__":
    main()
