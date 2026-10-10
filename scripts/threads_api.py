"""Threads Graph API のクライアントと、threads_post.py・threads_insights.py が共通で使うファイル操作。

標準ライブラリのみ。アクセストークンは表示しない（エラーメッセージはすべて秘密値を消してから出す）。
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

GRAPH_HOST = "graph.threads.net"
GRAPH = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"


# ---------------------------------------------------------------------------
# 時刻
# ---------------------------------------------------------------------------

def now() -> dt.datetime:
    """今のローカル時刻（タイムゾーン付き）。"""
    return dt.datetime.now().astimezone()


def iso(t: dt.datetime) -> str:
    return t.astimezone().isoformat(timespec="seconds")


def parse_time(s: str) -> dt.datetime | None:
    """ISO 8601、'YYYY-MM-DD HH:MM[:SS]'、Threads の '+0000' 形式を受け付ける。タイムゾーンが無ければローカル時刻とみなす。"""
    s = (s or "").strip().replace("/", "-")
    if not s:
        return None
    s = re.sub(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):",
               lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d} {int(m[4]):02d}:", s)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


# ---------------------------------------------------------------------------
# ファイル
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """同じフォルダの一時ファイルに書いて fsync し、名前を変える（書きかけのファイルを残さない）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data, mode: int = 0o644) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n", mode)


class LockHeld(RuntimeError):
    pass


try:
    import fcntl as _fcntl
except ImportError:  # Windows の場合
    _fcntl = None

STALE_LOCK_S = 3 * 3600


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class FileLock:
    """ロックファイルによる排他ロック（待たずに失敗する）。

    fcntl がある場合（macOS/Linux）: flock を使う。プロセスが死ねば自動で外れる。
    fcntl が無い場合（Windows）: O_CREAT|O_EXCL でロックファイルを作り、"<pid> <epoch>" を書く。
    持ち主のプロセスが居ないか、STALE_LOCK_S より古いロックは放置されたものとみなして消す。
    """

    def __init__(self, path: Path, use_fcntl: bool | None = None):
        self.path = Path(path)
        self.fd: int | None = None
        self.use_fcntl = (_fcntl is not None) if use_fcntl is None else use_fcntl
        self.owned_file = False

    def _acquire_excl(self) -> None:
        for _ in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
                os.write(self.fd, f"{os.getpid()} {int(time.time())}".encode())
                self.owned_file = True
                return
            except FileExistsError:
                try:
                    pid_s, ts_s = (self.path.read_text().split() + ["0", "0"])[:2]
                    pid, ts = int(pid_s), float(ts_s)
                except (OSError, ValueError):
                    pid, ts = 0, 0.0
                if _pid_alive(pid) and time.time() - ts < STALE_LOCK_S:
                    raise LockHeld(f"別の実行（pid {pid}）が {self.path.name} を使っている。あとで再実行してください") from None
                try:
                    self.path.unlink()  # 放置されたロック
                except FileNotFoundError:
                    pass
        raise LockHeld(f"{self.path.name} のロックを取れなかった。あとで再実行してください")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.use_fcntl:
            self._acquire_excl()
            return self
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _fcntl.flock(self.fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as e:
            os.close(self.fd)
            self.fd = None
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockHeld(f"別の実行が {self.path.name} を使っている。あとで再実行してください") from None
            raise
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()} {int(time.time())}".encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            if self.use_fcntl:
                _fcntl.flock(self.fd, _fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        if self.owned_file:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.owned_file = False
        return False


# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_slug(slug, what: str = "スラッグ") -> str:
    """スラッグとカット ID はファイル名になるので、英数字・'_'・'-' のみ（common.check_id と同じ規則）。"""
    fn = getattr(common, "check_id", None)
    s = slug if isinstance(slug, str) else str(slug)
    if fn:
        try:
            return fn(s, what)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
    if not _SLUG_RE.match(s):
        raise RuntimeError(f"{what} {s[:40]!r} が不正（英数字・_・- のみ）")
    return s


def safe_path(base: Path, rel) -> Path:
    """`rel` を `base` の中で解決し、外に出るもの（..・絶対パス・シンボリックリンク）は拒む。"""
    base_r = Path(base).resolve()
    fn = getattr(common, "safe_path", None)
    p = None
    if fn:
        try:
            p = Path(fn(base_r, rel)).resolve()
        except TypeError:
            p = None
        except ValueError as e:
            raise RuntimeError(str(e)) from None
    if p is None:
        q = Path(os.path.expanduser(str(rel)))
        p = (q if q.is_absolute() else base_r / q).resolve()
    if p != base_r and base_r not in p.parents:
        raise RuntimeError(f"パス {str(rel)[:80]!r} がプロジェクトフォルダの外にある")
    return p


# ---------------------------------------------------------------------------
# Graph API
# ---------------------------------------------------------------------------

_REDACT = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
    (re.compile(r"(?i)(access_token|client_secret|refresh_token)=[^&\s\"'<>]+"), r"\1=***"),
    (re.compile(r"(?i)X-Amz-[A-Za-z-]+=[^&\s\"'<>]+"), "X-Amz-***"),
    (re.compile(r"(https?://[^\s\"'<>?#]+)\?[^\s\"'<>]*"), r"\1?***"),
]


def redact(msg, secret: str | None = None) -> str:
    """表示・保存の前に秘密を消す: 設定済みの秘密値、渡された secret、Bearer の値、access_token=、
    X-Amz-* の署名部分、すべての URL のクエリ文字列。"""
    msg = str(msg)
    try:
        msg = common.scrub(msg)
    except Exception:  # noqa: BLE001 - 秘密値の消し込みで失敗してはいけない
        pass
    if secret:
        msg = msg.replace(secret, "***")
    for rx, rep in _REDACT:
        msg = rx.sub(rep, msg)
    return msg


scrub = redact  # 互換のための別名

_ID_RE = re.compile(r"^[0-9]{1,40}$")


def check_id(v) -> str:
    """Threads のオブジェクト ID は数字のみ。それ以外は URL に入る前に拒む。"""
    v = str(v).strip()
    if not _ID_RE.match(v):
        raise RuntimeError(f"Threads の ID {v[:40]!r} が不正（数字のみ）")
    return v


_PATH_RE = re.compile(r"^(me|[0-9]{1,40})(/[a-z_]{1,40})?$")


def check_path(path: str) -> str:
    """`me`・`<id>`・`<id>/threads`・`<id>/insights` のような相対の Graph パスだけを許す。"""
    p = str(path).strip().lstrip("/")
    if not _PATH_RE.match(p):
        raise RuntimeError(f"Graph パス {redact(p)[:60]!r} を拒否（相対の '<id>[/edge]' のみ可）")
    return p


def is_graph_url(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" and u.hostname == GRAPH_HOST


class Graph:
    """最小限の Threads Graph API クライアント。トークンは Authorization ヘッダーに入れ、URL には入れない。
    受け付けるのは相対パスのみ。ページ送りの URL は https://graph.threads.net のものだけたどる。"""

    def __init__(self, token: str, opener=urllib.request.urlopen, base: str = GRAPH):
        self.token = token
        self.opener = opener
        self.base = base

    def _send(self, method: str, url: str, body: bytes | None, where: str) -> dict:
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with self.opener(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(redact(f"Threads API {where}: HTTP {e.code} {e.read().decode(errors='replace')[:400]}",
                                      self.token)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise RuntimeError(redact(f"Threads API {where}: ネットワークエラー {e}", self.token)) from None

    def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        p = check_path(path)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params.pop("access_token", None)
        url = f"{self.base}/{p}"
        body = None
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
        else:
            body = urllib.parse.urlencode(params).encode()
        return self._send(method, url, body, f"{method} {p}")

    def get(self, path: str, **params) -> dict:
        return self._call("GET", path, params)

    def post(self, path: str, **params) -> dict:
        return self._call("POST", path, params)

    def paged(self, path: str, max_pages: int = 20, **params):
        res = self.get(path, **params)
        for _ in range(max_pages):
            yield from res.get("data", [])
            nxt = (res.get("paging") or {}).get("next")
            if not nxt:
                return
            if not is_graph_url(nxt):
                raise RuntimeError("https://graph.threads.net 以外のページ送り URL を拒否")
            res = self._send("GET", nxt, None, "GET paging")


def user_id(g, configured: str = "") -> str:
    """設定の threads_user_id を使う。無ければトークンから調べる。"""
    if configured:
        return check_id(configured)
    me = g.get("me", fields="id,username")
    uid = str(me.get("id", "")).strip()
    if not uid:
        raise RuntimeError("トークンから Threads のユーザー ID を調べられなかった（/me が id を返さない）")
    return check_id(uid)


def refresh_long_lived(token: str, opener=urllib.request.urlopen) -> dict:
    """長期トークンを延長する。{"access_token", "expires_in"} を返す（表示しないこと）。
    まず Authorization ヘッダーで試し、だめなら公式ドキュメントにあるクエリパラメータで送る。"""
    def attempt(url: str, headers: dict) -> dict:
        with opener(urllib.request.Request(url, method="GET", headers=headers), timeout=30) as r:
            return json.loads(r.read())
    base = f"{REFRESH_URL}?grant_type=th_refresh_token"
    try:
        try:
            res = attempt(base, {"Authorization": f"Bearer {token}"})
        except urllib.error.HTTPError as e:
            if e.code not in (400, 401):
                raise
            res = attempt(base + "&" + urllib.parse.urlencode({"access_token": token}), {})
    except urllib.error.HTTPError as e:
        raise RuntimeError(redact(f"トークンの延長に失敗: HTTP {e.code} {e.read().decode(errors='replace')[:300]}",
                                  token)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RuntimeError(redact(f"トークンの延長に失敗: ネットワークエラー {e}", token)) from None
    if not res.get("access_token"):
        raise RuntimeError("トークンの延長: 応答に access_token が無い")
    return {"access_token": res["access_token"], "expires_in": int(res.get("expires_in") or 0)}
