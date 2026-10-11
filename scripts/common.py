"""threads-video-studio の各スクリプトで共通に使う関数（Python 3.10+、標準ライブラリのみ）。

設定は次の順に探し、最初に見つかった空でない値を使う:
  1. Claude のプラグイン設定画面で入力した値
     （スクリプトには CLAUDE_PLUGIN_OPTION_<KEY> として渡る。README 参照）。
  2. 通常の環境変数（FAL_KEY、ELEVENLABS_API_KEY など）。
  3. 今のフォルダ（または3階層上まで）かワークスペースフォルダにある `tvs-config.json`（Cowork 用）。
  4. `setup.py` が書くユーザー設定ファイル（~/.config/threads-video-studio/config.json、権限 0600）。

秘密値は表示しない。値があることを示すときは `mask()` を使う。
"""

from __future__ import annotations

import json
import os
import stat
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TVS_CONFIG_DIR", Path.home() / ".config" / "threads-video-studio"))
CONFIG_FILE = CONFIG_DIR / "config.json"

# キー -> (代わりに見る環境変数, 秘密値か, 説明)
SETTINGS: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "fal_key": (("FAL_KEY", "FAL_API_KEY"), True, "fal.ai の API キー（画像・動画・音楽の生成）"),
    "elevenlabs_api_key": (("ELEVENLABS_API_KEY",), True, "ElevenLabs の API キー（ナレーション・音楽）"),
    "threads_access_token": (("THREADS_ACCESS_TOKEN",), True, "Threads の長期アクセストークン（投稿）"),
    "threads_user_id": (("THREADS_USER_ID",), False, "Threads のユーザー ID（任意。トークンから調べる）"),
    "media_bucket": (("TVS_MEDIA_BUCKET",), False, "動画の一時 URL を作る非公開 S3 バケット（投稿）"),
    "aws_profile": (("AWS_PROFILE",), False, "バケットに使う AWS CLI のプロファイル（任意）"),
    "aws_region": (("AWS_REGION",), False, "バケットの AWS リージョン（任意）"),
    "cta_url": (("TVS_CTA_URL",), False, "誘導先のリンク（例: LINE 公式アカウントの URL）"),
    "workspace": (("TVS_WORKSPACE",), False, "プロジェクトを作るフォルダ（既定は ~/threads-videos）"),
    "narrator_voice_id": (("TVS_NARRATOR_VOICE_ID",), False, "ナレーションに使う ElevenLabs の既定の声 ID"),
}


def _plugin_option(key: str) -> str:
    for name in (f"CLAUDE_PLUGIN_OPTION_{key.upper()}", f"CLAUDE_PLUGIN_OPTION_{key}"):
        v = os.environ.get(name, "").strip()
        if v and not v.startswith("${"):  # 未入力の ${user_config.*} プレースホルダー
            return v
    return ""


LOCAL_CONFIG_NAME = "tvs-config.json"  # Cowork 用: 接続したフォルダに置く


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def load_file() -> dict:
    """ユーザー設定ファイル（~/.config/threads-video-studio/config.json）。"""
    return _read_json(CONFIG_FILE)


def _local_files() -> list[Path]:
    """今のフォルダ、その親（3階層まで）、ワークスペースフォルダにある tvs-config.json。"""
    cwd = Path.cwd()
    cands = [cwd / LOCAL_CONFIG_NAME] + [p / LOCAL_CONFIG_NAME for p in list(cwd.parents)[:3]]
    ws = os.environ.get("TVS_WORKSPACE", "").strip() or str(_read_json(CONFIG_FILE).get("workspace", "")).strip()
    if ws:
        cands.append(Path(os.path.expanduser(ws)) / LOCAL_CONFIG_NAME)
    seen, out = set(), []
    for c in cands:
        if c not in seen and c.is_file():
            seen.add(c)
            _ensure_private(c)
            out.append(c)
    return out


_PERM_WARNED: set = set()


def _ensure_private(path: Path) -> None:
    """キーを含む設定ファイルは他のユーザーから読めてはいけない。chmod 600 にして警告する（止めはしない）。"""
    try:
        mode = path.stat().st_mode
        if mode & 0o077:
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                msg = f"警告: {path} が他のユーザーから読める状態だったので、権限を 600 に変えた"
            except OSError:
                msg = f"警告: {path} が他のユーザーから読める。chmod 600 '{path}' を実行してください"
            if str(path) not in _PERM_WARNED:
                _PERM_WARNED.add(str(path))
                print(msg, file=sys.stderr)
    except OSError:
        pass


def save_file(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(CONFIG_FILE)


KEYCHAIN_PREFIX = "keychain:"


def keychain_ref(v: str) -> tuple[str, str] | None:
    """"keychain:<service>" か "keychain:<service>@<account>" なら (service, account) を返す。"""
    if not v.startswith(KEYCHAIN_PREFIX):
        return None
    svc, _, acct = v[len(KEYCHAIN_PREFIX):].partition("@")
    if not svc.strip():
        raise RuntimeError("keychain: の後にサービス名が無い")
    return svc.strip(), acct.strip()


def read_keychain(svc: str, acct: str = "") -> str:
    cmd = ["security", "find-generic-password", "-s", svc, "-w"] + (["-a", acct] if acct else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"Keychain に {svc}{'@' + acct if acct else ''} が無い（または読めない）")
    return r.stdout.strip()


def write_keychain(svc: str, acct: str, value: str) -> None:
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", svc, "-a", acct or os.environ.get("USER", ""),
                        "-T", "/usr/bin/security", "-w", value], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Keychain に {svc} を書けない: {r.stderr.strip()[:200]}")


def raw_get(key: str, default: str = "") -> str:
    """設定の生の値（keychain: の参照を解決しない）。"""
    v = _plugin_option(key)
    if v:
        return v
    for env in SETTINGS.get(key, ((),))[0]:
        v = os.environ.get(env, "").strip()
        if v and not v.startswith("${"):
            return v
    for f in _local_files():
        v = str(_read_json(f).get(key, "")).strip()
        if v:
            return v
    v = str(load_file().get(key, "")).strip()
    return v or default


def get(key: str, default: str = "") -> str:
    """設定の値。"keychain:<service>[@<account>]" と書かれていれば、macOS の Keychain から読む
    （秘密値を設定ファイルに平文で置かずに済む。launchd のジョブからも読める）。"""
    v = raw_get(key, default)
    ref = keychain_ref(v) if v else None
    return read_keychain(*ref) if ref else v


def require(key: str) -> str:
    v = get(key)
    if not v:
        desc = SETTINGS.get(key, ((), False, key))[2]
        sys.exit(f"設定 '{key}'（{desc}）がありません。プラグインの設定画面で入力するか、"
                 f"python3 scripts/setup.py set {key} を実行してください")
    return v


def mask(v: str) -> str:
    """値があるかどうかだけを返す（長さや文字は一切出さない）。"""
    return "設定済み" if v else "(未設定)"


def workspace() -> Path:
    return Path(os.path.expanduser(get("workspace", str(Path.home() / "threads-videos"))))


class HttpError(RuntimeError):
    """HTTP のエラー応答（ステータス 400 以上）。`.code` と `.body`（秘密値を消して切り詰めたもの）を持つ。"""

    def __init__(self, code: int, method: str, url: str, body: str):
        self.code = code
        self.body = scrub(body)
        super().__init__(scrub(f"HTTP {code} {method} {url.split('?')[0]}: {body}"))


class NetworkError(RuntimeError):
    """再試行をすべて使っても直らなかった一時的なネットワーク障害。"""


def http_request(method: str, url: str, headers: dict, data: bytes | None = None, timeout: int = 120,
                 retries: int = 3) -> tuple[bytes, str]:
    """生のリクエスト。(本文, content_type) を返す。ネットワークエラーと HTTP 5xx は再試行し、4xx は再試行しない。
    やり直すと結果が変わる有料の POST には retries=1 を使う（タイムアウト後の再試行で二重課金されうるため）。"""
    last = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:800]
            if e.code >= 500 and attempt < retries - 1:
                last = f"HTTP {e.code}: {msg}"
                time.sleep(5 * (attempt + 1))
                continue
            raise HttpError(e.code, method, url, msg) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(e)
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise NetworkError(scrub(f"ネットワークエラー {method} {url.split('?')[0]}: {last}"))


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 120,
              retries: int = 3) -> dict:
    """JSON のリクエスト。一時的なネットワークエラーは再試行する（HTTP 4xx は再試行しない）。"""
    data = json.dumps(body).encode() if body is not None else None
    raw, _ = http_request(method, url, {"Content-Type": "application/json", **headers}, data, timeout, retries)
    return json.loads(raw) if raw else {}


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    return dest


BROKEN_HINT = ("Homebrew で別の道具を入れたときに、共有ライブラリだけが新しくなって壊れた可能性が高い。"
               "`brew upgrade {tool}`（直らなければ `brew reinstall {tool}`）で直る")


def run(*cmd: str, quiet: bool = True) -> str:
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    if r.returncode != 0:
        msg = f"{cmd[0]} が失敗: {r.stderr.strip()[:400]}"
        if "Library not loaded" in r.stderr:
            msg += "\n" + BROKEN_HINT.format(tool=Path(cmd[0]).name)
        raise RuntimeError(msg)
    return r.stdout.strip()


def tool_health(tool: str, version_arg: str = "-version") -> str:
    """道具が「あり」「無い」「壊れている（理由）」のどれかを返す。入っていても起動できないことがある。"""
    path = shutil.which(tool)
    if not path:
        return "無い"
    try:
        r = subprocess.run([path, version_arg], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"壊れている（起動できない: {e}）"
    if r.returncode == 0:
        return "あり"
    err = (r.stderr or r.stdout).strip().splitlines()
    first = next((x.strip() for x in err if "Library not loaded" in x), err[0].strip() if err else f"終了コード {r.returncode}")
    hint = BROKEN_HINT.format(tool=tool) if "Library not loaded" in r.stderr else ""
    return f"壊れている（{first[:160]}）{' ' + hint if hint else ''}"


def duration(path: Path) -> float:
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)))


def data_uri(path: Path, max_width: int = 1080) -> str:
    """画像を縮小した JPEG にして data URI で返す（リクエスト本文を小さく保つため）。"""
    import base64
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "img.jpg"
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(out))
        return "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode()


# --- 秘密値の扱い -------------------------------------------------------------------------

def secret_values() -> list[str]:
    """設定済みの秘密値すべて（長い順）。消し込み用。"""
    vals = []
    for key, (_envs, is_secret, _desc) in SETTINGS.items():
        if is_secret:
            v = get(key)
            if v and len(v) >= 6:
                vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def scrub(text: str) -> str:
    """表示・保存する前に、メッセージから設定済みの秘密値を消す。"""
    text = str(text)
    for v in secret_values():
        text = text.replace(v, "***")
    return text


def die(msg: str, code: int = 1) -> None:
    """秘密値を消した1行の理由を出して、0以外で終了する。"""
    print("エラー: " + scrub(str(msg)).replace("\n", " ")[:1200], file=sys.stderr)
    sys.exit(code)


def cli(main) -> None:
    """`main()` を実行し、想定内の失敗を秘密値を消した1行のエラーにする。"""
    try:
        main()
    except KeyboardInterrupt:
        die("中断した", 130)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        die(f"{type(e).__name__}: {e}")


# --- プロジェクト関連 -------------------------------------------------------------------------

def project_dir(arg: str | None) -> Path:
    """--project にはパスか、ワークスペース内のスラッグを渡せる。省略時は今のフォルダ。"""
    if not arg:
        p = Path.cwd()
    else:
        p = Path(os.path.expanduser(arg))
        if not p.exists() and not p.is_absolute():
            alt = workspace() / check_id(arg, "プロジェクトのスラッグ")
            if alt.exists():
                p = alt
    if not (p / "shots.json").exists():
        raise RuntimeError(f"プロジェクトフォルダではない（shots.json が無い）: {p}")
    return p.resolve()


ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


def check_id(value: str, what: str = "ID") -> str:
    """スラッグとカット ID は英数字・'_'・'-' のみ（1〜64文字）。ファイル名になるため。"""
    import re
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        raise ValueError(f"{what} {value!r} が不正。使える文字は A-Z a-z 0-9 _ -（1〜64文字）")
    return value


def safe_path(base: Path, rel: str | Path, what: str = "パス") -> Path:
    """`rel` を `base` 基準で解決し（シンボリックリンクもたどる）、結果が `base` の中に収まることを求める。
    絶対パスは `base` の中を指すときだけ受け付ける。"""
    if rel is None or str(rel).strip() == "":
        raise ValueError(f"{what}が空")
    root = Path(base).resolve()
    p = Path(os.path.expanduser(str(rel)))
    full = (p if p.is_absolute() else root / p).resolve()
    if full != root and root not in full.parents:
        raise ValueError(f"{what} {str(rel)!r} が {root} の外を指している")
    return full


def resolve(project: Path | None, path: str | Path) -> Path:
    """絶対パスはそのまま。相対パスはプロジェクトフォルダ（無ければ今のフォルダ）基準。"""
    p = Path(os.path.expanduser(str(path)))
    if p.is_absolute() or project is None:
        return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    return (project / p).resolve()


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def save_json(path: Path, data) -> None:
    """JSON を原子的に書く（UTF-8、日本語はエスケープしない）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    tmp.replace(path)


def load_shots(project: Path) -> dict:
    return load_json(project / "shots.json")


def save_shots(project: Path, shots: dict) -> None:
    save_json(project / "shots.json", shots)


# --- fal ------------------------------------------------------------------------------------

def fal_headers() -> dict:
    return {"Authorization": f"Key {require('fal_key')}"}


def fal_run(endpoint: str, body: dict, timeout: int = 600) -> dict:
    """fal の同期呼び出し（https://fal.run/<endpoint>）。再試行しない（二重課金されうるため）。"""
    return http_json("POST", f"https://fal.run/{endpoint}", fal_headers(), body, timeout=timeout, retries=1)


def media_url_to_file(url: str, dest: Path) -> Path:
    """結果の URL（https か data URI）を dest に保存する。"""
    if url.startswith("data:"):
        import base64
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(url.split(",", 1)[1]))
        return dest
    return download(url, dest)
