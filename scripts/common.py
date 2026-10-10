"""Shared helpers for threads-video-studio scripts (Python 3.10+, standard library only).

Configuration is resolved in this order, first non-empty wins:
  1. Plugin settings entered in Claude's plugin configuration screen
     (exposed to scripts as CLAUDE_PLUGIN_OPTION_<KEY>, see README).
  2. Plain environment variables (FAL_KEY, ELEVENLABS_API_KEY, ...).
  3. `tvs-config.json` in the current folder (or up to 3 parents) or in the workspace folder (Cowork).
  4. The user config file written by `setup.py` (~/.config/threads-video-studio/config.json, mode 0600).

Secrets are never printed. Use `mask()` when showing that a value exists.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TVS_CONFIG_DIR", Path.home() / ".config" / "threads-video-studio"))
CONFIG_FILE = CONFIG_DIR / "config.json"

# key -> (environment variable fallbacks, is_secret, description)
SETTINGS: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "fal_key": (("FAL_KEY", "FAL_API_KEY"), True, "fal.ai API key (image, video, music generation)"),
    "elevenlabs_api_key": (("ELEVENLABS_API_KEY",), True, "ElevenLabs API key (narration, music)"),
    "threads_access_token": (("THREADS_ACCESS_TOKEN",), True, "Threads long-lived access token (posting)"),
    "threads_user_id": (("THREADS_USER_ID",), False, "Threads user id (optional, looked up from the token)"),
    "media_bucket": (("TVS_MEDIA_BUCKET",), False, "Private S3 bucket for temporary video URLs (posting)"),
    "aws_profile": (("AWS_PROFILE",), False, "AWS CLI profile used for the bucket (optional)"),
    "aws_region": (("AWS_REGION",), False, "AWS region of the bucket (optional)"),
    "cta_url": (("TVS_CTA_URL",), False, "Call-to-action link, e.g. your LINE official account URL"),
    "workspace": (("TVS_WORKSPACE",), False, "Folder where projects are created (default ~/threads-videos)"),
    "narrator_voice_id": (("TVS_NARRATOR_VOICE_ID",), False, "Default ElevenLabs voice id for narration"),
}


def _plugin_option(key: str) -> str:
    for name in (f"CLAUDE_PLUGIN_OPTION_{key.upper()}", f"CLAUDE_PLUGIN_OPTION_{key}"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return ""


LOCAL_CONFIG_NAME = "tvs-config.json"  # Cowork: placed in the connected folder


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def load_file() -> dict:
    """User config file (~/.config/threads-video-studio/config.json)."""
    return _read_json(CONFIG_FILE)


def _local_files() -> list[Path]:
    """tvs-config.json in the current folder, its parents (up to 3) and the workspace folder."""
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
    """A config file holding keys must not be readable by group/other: chmod 600 and warn (never fatal)."""
    try:
        mode = path.stat().st_mode
        if mode & 0o077:
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                msg = f"warning: {path} was readable by other users; permissions changed to 600"
            except OSError:
                msg = f"warning: {path} is readable by other users; run: chmod 600 '{path}'"
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


def get(key: str, default: str = "") -> str:
    v = _plugin_option(key)
    if v:
        return v
    for env in SETTINGS.get(key, ((),))[0]:
        v = os.environ.get(env, "").strip()
        if v:
            return v
    for f in _local_files():
        v = str(_read_json(f).get(key, "")).strip()
        if v:
            return v
    v = str(load_file().get(key, "")).strip()
    return v or default


def require(key: str) -> str:
    v = get(key)
    if not v:
        desc = SETTINGS.get(key, ((), False, key))[2]
        sys.exit(f"Missing setting '{key}' ({desc}). Enter it in the plugin settings, "
                 f"or run: python3 scripts/setup.py set {key}")
    return v


def mask(v: str) -> str:
    """Only says whether a value exists (never its length or any characters)."""
    return "set" if v else "(empty)"


def workspace() -> Path:
    return Path(os.path.expanduser(get("workspace", str(Path.home() / "threads-videos"))))


class HttpError(RuntimeError):
    """HTTP error response (status >= 400). `.code` and `.body` (scrubbed, truncated) are available."""

    def __init__(self, code: int, method: str, url: str, body: str):
        self.code = code
        self.body = scrub(body)
        super().__init__(scrub(f"HTTP {code} {method} {url.split('?')[0]}: {body}"))


class NetworkError(RuntimeError):
    """Transient network failure that persisted through all retries."""


def http_request(method: str, url: str, headers: dict, data: bytes | None = None, timeout: int = 120,
                 retries: int = 3) -> tuple[bytes, str]:
    """Raw request; returns (body, content_type). Retries network errors and HTTP 5xx, never HTTP 4xx.
    Use retries=1 for non-idempotent paid POSTs (a retry after a timeout could be billed twice)."""
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
    raise NetworkError(scrub(f"network error {method} {url.split('?')[0]}: {last}"))


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 120,
              retries: int = 3) -> dict:
    """JSON request with retry on transient network errors (never on HTTP 4xx)."""
    data = json.dumps(body).encode() if body is not None else None
    raw, _ = http_request(method, url, {"Content-Type": "application/json", **headers}, data, timeout, retries)
    return json.loads(raw) if raw else {}


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    return dest


def run(*cmd: str, quiet: bool = True) -> str:
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def duration(path: Path) -> float:
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)))


def data_uri(path: Path, max_width: int = 1080) -> str:
    """Downscale an image to JPEG and return a data URI (keeps request bodies small)."""
    import base64
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "img.jpg"
        run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(out))
        return "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode()


# --- secrets hygiene -------------------------------------------------------------------------

def secret_values() -> list[str]:
    """All configured secret values (longest first), for scrubbing."""
    vals = []
    for key, (_envs, is_secret, _desc) in SETTINGS.items():
        if is_secret:
            v = get(key)
            if v and len(v) >= 6:
                vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def scrub(text: str) -> str:
    """Remove any configured secret value from a message before it is printed or saved."""
    text = str(text)
    for v in secret_values():
        text = text.replace(v, "***")
    return text


def die(msg: str, code: int = 1) -> None:
    """Exit non-zero with a one-line, scrubbed reason."""
    print("error: " + scrub(str(msg)).replace("\n", " ")[:1200], file=sys.stderr)
    sys.exit(code)


def cli(main) -> None:
    """Run `main()` and turn expected failures into a one-line scrubbed error."""
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted", 130)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        die(f"{type(e).__name__}: {e}")


# --- project helpers -------------------------------------------------------------------------

def project_dir(arg: str | None) -> Path:
    """--project accepts a path or a slug under the workspace. Defaults to the current folder."""
    if not arg:
        p = Path.cwd()
    else:
        p = Path(os.path.expanduser(arg))
        if not p.exists() and not p.is_absolute():
            alt = workspace() / check_id(arg, "project slug")
            if alt.exists():
                p = alt
    if not (p / "shots.json").exists():
        raise RuntimeError(f"not a project folder (no shots.json): {p}")
    return p.resolve()


ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


def check_id(value: str, what: str = "id") -> str:
    """Slugs and cut ids: letters, digits, '_' and '-' only (1-64 chars). They become file names."""
    import re
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        raise ValueError(f"invalid {what} {value!r}: allowed characters are A-Z a-z 0-9 _ - (1-64)")
    return value


def safe_path(base: Path, rel: str | Path, what: str = "path") -> Path:
    """Resolve `rel` against `base` (following symlinks) and require the result to stay inside `base`.
    Absolute paths are accepted only when they resolve inside `base`."""
    if rel is None or str(rel).strip() == "":
        raise ValueError(f"empty {what}")
    root = Path(base).resolve()
    p = Path(os.path.expanduser(str(rel)))
    full = (p if p.is_absolute() else root / p).resolve()
    if full != root and root not in full.parents:
        raise ValueError(f"{what} {str(rel)!r} points outside {root}")
    return full


def resolve(project: Path | None, path: str | Path) -> Path:
    """Absolute paths stay; relative paths are relative to the project folder (or cwd)."""
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
    """Atomic JSON write (UTF-8, Japanese kept readable)."""
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
    """Synchronous fal call (https://fal.run/<endpoint>). Not retried: a retry could be billed twice."""
    return http_json("POST", f"https://fal.run/{endpoint}", fal_headers(), body, timeout=timeout, retries=1)


def media_url_to_file(url: str, dest: Path) -> Path:
    """Save a result URL (https or data URI) to dest."""
    if url.startswith("data:"):
        import base64
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(url.split(",", 1)[1]))
        return dest
    return download(url, dest)
