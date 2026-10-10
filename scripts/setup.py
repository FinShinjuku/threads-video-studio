#!/usr/bin/env python3
"""threads-video-studio の設定をシェルから確認・変更する。

  python3 setup.py status            # 何が設定済みか（秘密値は伏せる）
  python3 setup.py set <key>         # 値を画面に出さずに入力し、設定ファイルに保存する
  python3 setup.py import <file>     # setup.html で作った JSON を取り込む（--delete で取込後に削除）
  python3 setup.py path              # 設定ファイルのパスを表示する

値は ~/.config/threads-video-studio/config.json（権限 0600）か、TVS_CONFIG_DIR のフォルダに保存する。
Claude Code ではプラグインの設定画面で入力し、`studio_save_settings` ツールを1回実行してもよい。
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
    print(f"設定ファイル: {common.CONFIG_FILE}（{'あり' if common.CONFIG_FILE.exists() else '未作成'}）")
    for key, (envs, secret, desc) in common.SETTINGS.items():
        v = common.get(key)
        shown = common.mask(v) if secret else (v or "(未設定)")
        print(f"  {key:22} {shown:28} {desc}")


def cmd_set(key: str) -> None:
    if key not in common.SETTINGS:
        sys.exit(f"不明なキー '{key}'。使えるキー: {', '.join(common.SETTINGS)}")
    secret = common.SETTINGS[key][1]
    v = (getpass.getpass(f"{key}: ") if secret else input(f"{key}: ")).strip()
    data = common.load_file()
    if v:
        data[key] = v
    else:
        data.pop(key, None)
    common.save_file(data)
    print(f"保存した {key}: {common.mask(v) if secret else (v or '(削除)')}")


def cmd_import(path: str, delete: bool) -> None:
    p = Path(os.path.expanduser(path))
    incoming = json.loads(p.read_text())
    unknown = [k for k in incoming if k not in common.SETTINGS]
    data = common.load_file()
    data.update({k: str(v).strip() for k, v in incoming.items() if k in common.SETTINGS and str(v).strip()})
    common.save_file(data)
    print(f"{len(incoming) - len(unknown)} 件のキーを {common.CONFIG_FILE} に取り込んだ"
          + (f"。不明なキーは無視した: {', '.join(unknown)}" if unknown else ""))
    if delete:
        p.unlink()
        print(f"削除した: {p}")


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
