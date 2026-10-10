#!/usr/bin/env python3
"""`threads_post.py publish-due` を15分ごとに動かす macOS の launchd ジョブを入れる。

既定は試し実行（dry run）。書く予定の plist と launchctl のコマンドを表示するだけ。
  --apply      ~/Library/LaunchAgents/<label>.plist を書き `launchctl bootstrap` する（既にあれば入れ直す）
  --uninstall  ジョブを `launchctl bootout` して plist を消す（--apply と併用）

注意
  - launchd は Claude で入力したプラグイン設定を読めない。ジョブはローカルの設定ファイル
    （scripts/setup.py が書く）を読むので、Threads のトークン（ローカル動画なら media_bucket も）をそこに入れておく。
  - ジョブは（バージョンごとに変わる）プラグインフォルダからは動かさない。スクリプトを固定の実行用フォルダ
    ~/.config/threads-video-studio/runner/ にコピーし、plist はそこを指す。プラグイン更新後は --apply で
    再実行してコピーを新しくする。
  - macOS はバックグラウンドからの ~/Desktop・~/Documents・~/Downloads へのアクセスを止めることがある。
    ワークスペースは別の場所（既定 ~/threads-videos）に置くか、Python にフルディスクアクセスを許可する。
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
        warns.append("設定ファイルに threads_access_token が無い。launchd はプラグイン設定を読めないので、"
                     "python3 scripts/setup.py set threads_access_token を実行してください")
    if not cfg.get("media_bucket"):
        warns.append("設定ファイルに media_bucket が無い。ローカルの video_file を使う投稿は失敗する"
                     "（下書き時に --media-url を渡せば回避できる）")
    ws = str(common.workspace())
    home = str(Path.home())
    if any(ws.startswith(f"{home}/{d}") for d in ("Desktop", "Documents", "Downloads")):
        warns.append(f"ワークスペース {ws} は macOS の保護対象フォルダにある。バックグラウンドのジョブから読めないことがある")
    return warns


def install_runner(dest: Path = RUNNER_DIR) -> list[Path]:
    """ジョブに必要なスクリプトを固定のフォルダにコピーする（ファイルごとに原子的）。コピー先のパスを返す。"""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in RUNNER_FILES:
        src = HERE / name
        tmp = dest / (name + ".tmp")
        tmp.write_bytes(src.read_bytes())
        tmp.replace(dest / name)
        if (dest / name).read_bytes() != src.read_bytes():
            raise RuntimeError(f"実行用フォルダへのコピーを読み戻したら一致しない: {name}")
        out.append(dest / name)
    return out


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"launchctl {args[0]} が失敗: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="15分ごとに publish-due を動かす launchd ジョブを入れる（macOS）。"
                                             "既定は試し実行。")
    ap.add_argument("--apply", action="store_true", help="実際に plist を書いて bootstrap する")
    ap.add_argument("--uninstall", action="store_true", help="ジョブを外す（--apply と併用）")
    ap.add_argument("--interval", type=int, default=900, help="実行の間隔（秒、既定 900）")
    ap.add_argument("--max", type=int, default=3, help="1回で投稿する最大件数（既定 3）")
    ap.add_argument("--python", default=sys.executable, help="ジョブを動かす python3")
    a = ap.parse_args(argv)
    if sys.platform != "darwin":
        print("エラー: launchd は macOS 専用。templates/cowork-scheduled-task.md か cron を使ってください", file=sys.stderr)
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
        print(f"{LABEL} を外した")
        return 0

    text = render_plist(a.python, a.interval, a.max, log)
    for w in preflight():
        print(f"警告: {w}")
    if not a.apply:
        print(f"[dry-run] {plist} に次を書く予定:\n{text}")
        print(f"[dry-run] {', '.join(RUNNER_FILES)} を {RUNNER_DIR}/ にコピー")
        print(f"[dry-run] launchctl bootout {domain}/{LABEL}（読み込み済みなら）")
        print(f"[dry-run] launchctl bootstrap {domain} {plist}")
        print("入れるには --apply を付けて再実行する")
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
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"{LABEL} を入れた: {a.interval} 秒ごと、実行用フォルダ {RUNNER_DIR}、ログ {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
