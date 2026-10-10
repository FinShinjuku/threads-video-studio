"""Threads Graph API client and small file helpers shared by threads_post.py and threads_insights.py.

Standard library only. The access token is never printed: every error message is scrubbed.
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
# time
# ---------------------------------------------------------------------------

def now() -> dt.datetime:
    """Current local time, timezone-aware."""
    return dt.datetime.now().astimezone()


def iso(t: dt.datetime) -> str:
    return t.astimezone().isoformat(timespec="seconds")


def parse_time(s: str) -> dt.datetime | None:
    """Accept ISO 8601, 'YYYY-MM-DD HH:MM[:SS]' and Threads' '+0000' form. No timezone = local time."""
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
# files
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Write via a temp file in the same folder, fsync, then rename (never leaves a half-written file)."""
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
except ImportError:  # Windows
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
    """Exclusive, non-blocking lock on a lock file.

    With fcntl (macOS/Linux): flock, released automatically if the process dies.
    Without fcntl (Windows): the lock file is created with O_CREAT|O_EXCL and holds "<pid> <epoch>";
    a lock whose process is gone, or which is older than STALE_LOCK_S, is treated as stale and removed.
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
                    raise LockHeld(f"another run (pid {pid}) holds {self.path.name}; try again later") from None
                try:
                    self.path.unlink()  # stale lock
                except FileNotFoundError:
                    pass
        raise LockHeld(f"could not take {self.path.name}; try again later")

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
                raise LockHeld(f"another run holds {self.path.name}; try again later") from None
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
# paths
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_slug(slug, what: str = "slug") -> str:
    """Slugs / cut ids become file names: letters, digits, '_' and '-' only (same rule as common.check_id)."""
    fn = getattr(common, "check_id", None)
    s = slug if isinstance(slug, str) else str(slug)
    if fn:
        try:
            return fn(s, what)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
    if not _SLUG_RE.match(s):
        raise RuntimeError(f"invalid {what} {s[:40]!r} (letters, digits, _ - only)")
    return s


def safe_path(base: Path, rel) -> Path:
    """Resolve `rel` under `base` and refuse anything that ends up outside it (.., absolute, symlinks)."""
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
        raise RuntimeError(f"path {str(rel)[:80]!r} is outside the project folder")
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
    """Remove secrets before anything is printed or saved: configured secret values, the given secret,
    Bearer values, access_token=, X-Amz-* signature parts, and every URL query string."""
    msg = str(msg)
    try:
        msg = common.scrub(msg)
    except Exception:  # noqa: BLE001 - scrubbing must never fail
        pass
    if secret:
        msg = msg.replace(secret, "***")
    for rx, rep in _REDACT:
        msg = rx.sub(rep, msg)
    return msg


scrub = redact  # backwards-compatible name

_ID_RE = re.compile(r"^[0-9]{1,40}$")


def check_id(v) -> str:
    """Threads object ids are numeric. Anything else is refused before it reaches a URL."""
    v = str(v).strip()
    if not _ID_RE.match(v):
        raise RuntimeError(f"invalid Threads id {v[:40]!r} (digits only)")
    return v


_PATH_RE = re.compile(r"^(me|[0-9]{1,40})(/[a-z_]{1,40})?$")


def check_path(path: str) -> str:
    """Only relative Graph paths like `me`, `<id>`, `<id>/threads`, `<id>/insights` are allowed."""
    p = str(path).strip().lstrip("/")
    if not _PATH_RE.match(p):
        raise RuntimeError(f"refused Graph path {redact(p)[:60]!r} (relative '<id>[/edge]' only)")
    return p


def is_graph_url(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" and u.hostname == GRAPH_HOST


class Graph:
    """Minimal Threads Graph API client. The token goes in an Authorization header, never in the URL.
    Only relative paths are accepted; paging URLs are followed only on https://graph.threads.net."""

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
            raise RuntimeError(redact(f"Threads API {where}: network error {e}", self.token)) from None

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
                raise RuntimeError("refused paging URL outside https://graph.threads.net")
            res = self._send("GET", nxt, None, "GET paging")


def user_id(g, configured: str = "") -> str:
    """Configured threads_user_id, else look it up from the token."""
    if configured:
        return check_id(configured)
    me = g.get("me", fields="id,username")
    uid = str(me.get("id", "")).strip()
    if not uid:
        raise RuntimeError("could not resolve the Threads user id from the token (/me returned no id)")
    return check_id(uid)


def refresh_long_lived(token: str, opener=urllib.request.urlopen) -> dict:
    """Extend a long-lived token. Returns {"access_token", "expires_in"} (never print it).
    Tries the Authorization header first; falls back to the documented query parameter."""
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
        raise RuntimeError(redact(f"token refresh failed: HTTP {e.code} {e.read().decode(errors='replace')[:300]}",
                                  token)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RuntimeError(redact(f"token refresh failed: network error {e}", token)) from None
    if not res.get("access_token"):
        raise RuntimeError("token refresh: no access_token in the response")
    return {"access_token": res["access_token"], "expires_in": int(res.get("expires_in") or 0)}
