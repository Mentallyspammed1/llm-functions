#!/usr/bin/env python3
# ==============================================================================
# osint_vsearch_engine.py — Pyrmethus Master OSINT & Media Intelligence Platform v4.0.0
# Unified Header Analysis · Multi-Backend OSINT Image Search · Video Scraper Engine
#
# @describe Unified OSINT, Media Intelligence, and Video Search Platform (Pyrmethus Edition)
#
# @option --mode <MODE>            headers | search | vsearch | pipeline (default: search)
#
# ── headers mode ──────────────────────────────────────────────────────────────
# @option --url <URL>              Target URL
# @option --method <METHOD>        HEAD | GET | OPTIONS (default: HEAD)
# @option --follow-redirects       Follow HTTP redirects (default: true)
# @option --output <FORMAT>        json | pretty (default: json)
#
# ── search / vsearch / pipeline mode ──────────────────────────────────────────
# @option --query <TEXT>           Search query
# @option --engine <ENGINE>        Video search engine (default: pexels)
# @option --backend <NAME>         yandex|bing|bing_dl|e621|rule34|danbooru (default: yandex)
# @option --limit <NUM>            Maximum results (default: 15)
# @option --page <NUM>             Starting page number (default: 1)
# @option --platform <NAME>        Restrict to domain
# @option --tags <TAG>             Extra tag (repeatable)
# @option --save <PATH>            Save JSON results
# @option --html <PATH>            Save HTML gallery
# @option --csv <PATH>             Save CSV report
# @flag   --download               Download found media/images
# @flag   --download-thumbs        Download thumbnail images locally
# @option --media-dir <PATH>       Download directory (default: ~/osint_media/)
# @flag   --verify-headers         Verify each URL's headers before downloading
# @flag   --mime-check             Reject downloads whose Content-Type isn't image/media
# @flag   --deep                   Multi-page deep search
#
# ── shared ────────────────────────────────────────────────────────────────────
# @option --user-agent <UA>        Custom User-Agent header
# @option --timeout <SECONDS>      Request timeout in seconds (default: 10)
# @flag   --ignore-ssl             Bypass SSL verification
# @option --cache-dir <PATH>       On-disk HTTP cache dir (default: ~/.osint_cache/)
# @option --cache-ttl <SECONDS>    Cache TTL in seconds (default: 3600)
# @option --workers <NUM>          Parallel workers (default: 4)
# @flag   --use-cache              Enable result caching
# @flag   --no-color               Disable ANSI color output
# @flag   --verbose                Enable detailed debug logging
#
# @env LLM_OUTPUT=/dev/stdout      Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import html
import json
import logging
import os
import random
import re
import signal
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Guard third-party optional imports
try:
    import requests
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from bs4 import BeautifulSoup

    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Silence noisy third-party loggers
logging.getLogger().setLevel(logging.CRITICAL)
for _n in ("better_bing_image_downloader", "urllib3", "requests", "tqdm", "bs4"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

# ── Optional Dependencies Probe ───────────────────────────────────────────────
_BING_DL_AVAILABLE = False
_BING_DL_FN: Any = None
_BING_DL_SIG = "unavailable"
_BING_DL_ERR = ""

try:
    import better_bing_image_downloader as _bbid

    _candidates: List[Tuple[str, Any]] = []
    if hasattr(_bbid, "downloader"):
        _d = _bbid.downloader
        if callable(_d):
            _candidates.append(("pkg.downloader", _d))
        elif hasattr(_d, "downloader") and callable(_d.downloader):
            _candidates.append(("pkg.downloader.downloader", _d.downloader))
        elif hasattr(_d, "download_images") and callable(_d.download_images):
            _candidates.append(("pkg.downloader.download_images", _d.download_images))
    if hasattr(_bbid, "download") and callable(_bbid.download):
        _candidates.append(("pkg.download", _bbid.download))
    if hasattr(_bbid, "Downloader"):
        for _mn in ("download", "download_images", "run", "__call__"):
            _m = getattr(_bbid.Downloader, _mn, None)
            if callable(_m):
                _candidates.append((f"Downloader.{_mn}", _m))
                break

    if _candidates:
        _BING_DL_FN, _BING_DL_SIG = _candidates[0][1], _candidates[0][0]
        _BING_DL_AVAILABLE = True
    else:
        _BING_DL_ERR = "No callable found in better_bing_image_downloader"
except ImportError as _ie:
    _BING_DL_ERR = str(_ie)
except Exception as _oe:
    _BING_DL_ERR = f"probe error: {_oe}"

try:
    from colorama import init as _cinit

    _cinit(autoreset=True)
except ImportError:
    pass

# ==============================================================================
# CONSTANTS & CONFIGURATION
# ==============================================================================

__version__ = "4.0.0"

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_TIMEOUT = 124
EXIT_PERMISSION_DENIED = 126
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPTED = 130

_DOWNLOAD_TIMEOUT = float(os.environ.get("OSINT_DOWNLOAD_TIMEOUT", "20"))
_REQUEST_TIMEOUT = float(os.environ.get("OSINT_REQUEST_TIMEOUT", "10"))
_MAX_RETRIES = int(os.environ.get("OSINT_MAX_RETRIES", "3"))
_RETRY_BACKOFF = float(os.environ.get("OSINT_RETRY_BACKOFF", "2.0"))
_RATE_INTERVAL = float(os.environ.get("OSINT_RATE_INTERVAL", "1.2"))
_DEBUG = os.environ.get("OSINT_DEBUG", "0").lower() in ("1", "true")
_IGNORE_SSL = os.environ.get("OSINT_IGNORE_SSL", "0").lower() in ("1", "true")
_DEFAULT_WORKERS = int(os.environ.get("OSINT_WORKERS", "4"))
_DEFAULT_MEDIA_DIR = os.environ.get("OSINT_MEDIA_DIR", "~/osint_media/")
_DEFAULT_CACHE_DIR = os.environ.get("OSINT_CACHE_DIR", "~/.osint_cache/")
_CACHE_TTL = int(os.environ.get("OSINT_CACHE_TTL", "3600"))

_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_IMAGE_EXTS = re.compile(
    r"\.(jpe?g|png|gif|webp|avif|bmp|tiff?)(\?.*)?$", re.IGNORECASE
)

_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.I)

_SECURITY_HEADERS = [
    "strict-transport-security",
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
    "x-xss-protection",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
]

_ALLOWED_METHODS = ("HEAD", "GET", "OPTIONS", "POST")

NEON_CYAN = "\033[38;5;51m"
NEON_GREEN = "\033[38;5;46m"
NEON_RED = "\033[38;5;196m"
NEON_YELLOW = "\033[38;5;226m"
NEON_PURPLE = "\033[38;5;129m"
NEON_PINK = "\033[38;5;198m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\033\[[0-9;?]*[a-zA-Z]")

# ==============================================================================
# UI & ANSI HELPERS
# ==============================================================================


class Ansi:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def _c(cls, code: str, m: str) -> str:
        return f"{code}{m}{cls.RESET}"

    @classmethod
    def red(cls, m: str) -> str:
        return cls._c(cls.RED, m)

    @classmethod
    def green(cls, m: str) -> str:
        return cls._c(cls.GREEN, m)

    @classmethod
    def yellow(cls, m: str) -> str:
        return cls._c(cls.YELLOW, m)

    @classmethod
    def cyan(cls, m: str) -> str:
        return cls._c(cls.CYAN, m)

    @classmethod
    def magenta(cls, m: str) -> str:
        return cls._c(cls.MAGENTA, m)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in (
        "dumb",
        "",
    )


def _cprint(
    text: str, file: Any = None, no_color: bool = False, end: str = "\n"
) -> None:
    target = file or sys.stderr
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def _debug(m: str) -> None:
    if _DEBUG:
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _cprint(Ansi.cyan(f"[DBG {ts}] {m}"))


def _warn(m: str) -> None:
    _cprint(Ansi.yellow(f"[WARN] {m}"))


def _err(m: str) -> None:
    _cprint(Ansi.red(f"[ERR ] {m}"))


def _info(m: str) -> None:
    _cprint(Ansi.green(f"[INFO] {m}"))


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    if not _is_tty() or no_color:
        return

    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "SUCCESS" if success else "FAILED"

    box_w = 68
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}", no_color=no_color)
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [OSINT & MEDIA INTELLIGENCE v{__version__}]{RESET} {status_color}{BOLD}{status_symbol} {status_text}{RESET}",
        no_color=no_color,
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Mode:{RESET}     {data.get('mode', 'N/A')}",
        no_color=no_color,
    )
    if "query" in data or "parameters" in data:
        q = data.get("query") or data.get("parameters", {}).get("query", "N/A")
        _cprint(
            f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Query:{RESET}    {q}", no_color=no_color
        )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Count:{RESET}    {NEON_YELLOW}{data.get('count', len(data.get('results', [])))}{RESET}",
        no_color=no_color,
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Timestamp:{RESET}{DIM} {data.get('timestamp', 'N/A')}{RESET}",
        no_color=no_color,
    )

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(
            f"{NEON_PURPLE}│{RESET} {NEON_RED}Error:{RESET}    {data['error']}",
            no_color=no_color,
        )

    results = data.get("results", [])
    if results:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(
            f"{NEON_PURPLE}│{RESET} {BOLD}Results Preview (Top {min(len(results), 5)}):{RESET}",
            no_color=no_color,
        )
        for i, item in enumerate(results[:5], 1):
            title = item.get("title", "Untitled")[:50]
            link = item.get("url") or item.get("link", "#")
            _cprint(
                f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}{i}.{RESET} {title}",
                no_color=no_color,
            )
            _cprint(
                f"{NEON_PURPLE}│{RESET}      {DIM}↳ {link}{RESET}", no_color=no_color
            )
        if len(results) > 5:
            _cprint(
                f"{NEON_PURPLE}│{RESET}   {DIM}... and {len(results) - 5} more results{RESET}",
                no_color=no_color,
            )

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}", no_color=no_color)


# ==============================================================================
# EXCEPTION & SERIALIZATION MODELS
# ==============================================================================


class ToolError(Exception):
    def __init__(
        self, message: str, exit_code: int = EXIT_ERROR, details: Optional[dict] = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


class ToolJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        if isinstance(obj, datetime.timedelta):
            return obj.total_seconds()
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


# ==============================================================================
# SANDBOX & PATH UTILITIES
# ==============================================================================


def _validate_sandbox(path: Path) -> bool:
    """Validate path lies within allowed user/system sandbox locations."""
    allowed_roots: list[Path] = [
        Path.home().resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]

    prefix = os.environ.get("PREFIX")
    if prefix:
        allowed_roots.append(Path(prefix).resolve())
        allowed_roots.append((Path(prefix) / "tmp").resolve())

    llm_root = os.environ.get("LLM_ROOT_DIR")
    if llm_root:
        allowed_roots.append(Path(llm_root).resolve())

    if Path("/data/data/com.termux").exists():
        allowed_roots.append(Path("/data/data/com.termux").resolve())

    try:
        resolved = path.resolve()
        s = str(resolved)
        return any(s.startswith(str(root)) for root in allowed_roots)
    except OSError:
        return False


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _coerce_timeout(v: Any, default: float = _REQUEST_TIMEOUT) -> float:
    try:
        return max(0.5, min(float(v), 120.0))
    except:
        return default


def _ua() -> str:
    return random.choice(_USER_AGENTS)


def _has_image_ext(url: str) -> bool:
    return bool(_IMAGE_EXTS.search(url.split("?", maxsplit=1)[0]))


def _safe_filename(url: str, fallback_ext: str = "jpg") -> str:
    name = url.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    name = urllib.parse.unquote(name)
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name)[:160].strip(" .")
    if not name or _WINDOWS_RESERVED.match(name):
        ext = fallback_ext
        m = _IMAGE_EXTS.search(url)
        if m:
            ext = m.group(1)
        name = f"img_{hashlib.md5(url.encode()).hexdigest()[:12]}.{ext}"
    if "." not in name:
        name = f"{name}.{fallback_ext}"
    return name


def _validate_url(url: str) -> Optional[str]:
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            return f"Unsupported scheme '{p.scheme}'. Only http/https allowed."
        if not p.netloc:
            return "URL missing host/netloc."

        host = p.hostname or ""
        # SSRF Guard for internal addresses in default mode
        if not _DEBUG:
            if host.lower() in (
                "localhost",
                "127.0.0.1",
                "0.0.0.0",
                "::1",
                "169.254.169.254",
            ):
                return "Access to local/loopback IP address blocked for security."
            if (
                host.startswith("10.")
                or host.startswith("192.168.")
                or (
                    host.startswith("172.") and 16 <= int(host.split(".")[1] or 0) <= 31
                )
            ):
                return "Access to private network IP address blocked for security."
        return None
    except Exception as e:
        return f"URL parse error: {e}"


def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _classify_status(code: int) -> str:
    if code < 200:
        return "informational"
    if code < 300:
        return "success"
    if code < 400:
        return "redirection"
    if code < 500:
        return "client_error"
    return "server_error"


def _normalise_headers(raw: dict) -> dict:
    return {str(k).lower().strip(): str(v).strip() for k, v in raw.items()}


def _analyse_headers(h: dict) -> dict:
    present = [x for x in _SECURITY_HEADERS if x in h]
    missing = [x for x in _SECURITY_HEADERS if x not in h]
    return {
        "security_headers_present": present,
        "security_headers_missing": missing,
        "security_score_pct": round(len(present) / len(_SECURITY_HEADERS) * 100),
    }


def _extract_server_info(h: dict) -> dict:
    keys = (
        "server",
        "x-powered-by",
        "via",
        "x-cache",
        "cf-ray",
        "content-type",
        "content-length",
        "cache-control",
        "etag",
        "last-modified",
        "expires",
        "age",
    )
    return {k: h[k] for k in keys if k in h}


def _is_image_content_type(ct: str) -> bool:
    return ct.lower().startswith("image/") or ct.lower().startswith("video/")


# ==============================================================================
# THREAD-SAFE RATE LIMITER
# ==============================================================================


class RateLimitState:
    """Thread-safe per-backend rate limiter with randomized jitter."""

    def __init__(self, min_interval: float = _RATE_INTERVAL):
        import threading

        self._lock = threading.Lock()
        self.last: float = 0.0
        self.min_interval = min_interval

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            jitter = random.uniform(0.05, 0.35)
            gap = (self.min_interval + jitter) - (now - self.last)
            if gap > 0:
                time.sleep(gap)
            self.last = time.monotonic()


_rl_lock = concurrent.futures.thread.threading.Lock()
_rl: Dict[str, RateLimitState] = {
    "yandex": RateLimitState(1.5),
    "bing": RateLimitState(1.2),
    "bing_dl": RateLimitState(0.3),
    "e621": RateLimitState(1.0),
    "rule34": RateLimitState(0.8),
    "danbooru": RateLimitState(0.8),
    "vsearch": RateLimitState(0.5),
    "generic": RateLimitState(0.5),
    "headers": RateLimitState(0.2),
}


def _get_rate_limiter(backend: str) -> RateLimitState:
    with _rl_lock:
        if backend not in _rl:
            _rl[backend] = RateLimitState(0.8)
        return _rl[backend]


# ==============================================================================
# ON-DISK ATOMIC HTTP CACHE
# ==============================================================================


class HttpCache:
    """Content-addressed atomic file cache with TTL support."""

    def __init__(self, cache_dir: str = _DEFAULT_CACHE_DIR, ttl: int = _CACHE_TTL):
        self.root = Path(cache_dir).expanduser()
        self.ttl = ttl
        if not _validate_sandbox(self.root):
            self.root = Path(_DEFAULT_CACHE_DIR).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _key_path(self, url: str, method: str = "GET") -> Path:
        digest = hashlib.sha256(f"{method}::{url}".encode()).hexdigest()
        return self.root / digest[:2] / digest

    def get(self, url: str, method: str = "GET") -> Optional[bytes]:
        p = self._key_path(url, method)
        if p.exists():
            try:
                age = time.time() - p.stat().st_mtime
                if age < self.ttl:
                    _debug(f"[cache] HIT age={age:.0f}s {url[:60]}")
                    return p.read_bytes()
                _debug(f"[cache] STALE age={age:.0f}s {url[:60]}")
            except Exception:
                pass
        return None

    def put(self, url: str, data: bytes, method: str = "GET") -> None:
        p = self._key_path(url, method)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp_{random.randint(1000, 9999)}")
        try:
            tmp.write_bytes(data)
            tmp.replace(p)
            _debug(f"[cache] PUT {len(data):,} B {url[:60]}")
        except Exception as e:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            _debug(f"[cache] PUT failed: {e}")


_cache = HttpCache()


class ToolCache:
    def __init__(self, cache_dir: Optional[str] = None) -> None:
        self.cache_dir = (
            Path(cache_dir).expanduser() if cache_dir else Path.home() / ".osint_cache"
        )
        if not _validate_sandbox(self.cache_dir):
            self.cache_dir = Path.home() / ".osint_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _make_key(self, key_data: str) -> str:
        return hashlib.sha256(key_data.encode("utf-8")).hexdigest()

    def get(self, key_data: str, ttl_seconds: int = 3600) -> Optional[Any]:
        key = self._make_key(key_data)
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            try:
                mtime = cache_file.stat().st_mtime
                if time.time() - mtime < ttl_seconds:
                    with open(cache_file, encoding="utf-8") as f:
                        return json.load(f)
            except Exception:
                pass
        return None

    def set(self, key_data: str, value: Any) -> None:
        key = self._make_key(key_data)
        cache_file = self.cache_dir / f"{key}.json"
        tmp_file = cache_file.with_suffix(f".tmp_{random.randint(1000, 9999)}")
        if _validate_sandbox(cache_file):
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(value, f, cls=ToolJSONEncoder)
                tmp_file.replace(cache_file)
            except Exception:
                if tmp_file.exists():
                    tmp_file.unlink(missing_ok=True)


# ==============================================================================
# AUTHORITATIVE HTTP FETCH ENGINE
# ==============================================================================


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_ssl_ctx(ignore_ssl: bool = _IGNORE_SSL) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ignore_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    method: str = "GET",
    timeout: float = _REQUEST_TIMEOUT,
    retries: int = _MAX_RETRIES,
    backend: str = "generic",
    ignore_ssl: bool = _IGNORE_SSL,
    follow_redirects: bool = True,
    use_cache: bool = True,
) -> Optional[bytes]:
    """Single authoritative fetch function with retry, rate-limit, and cache."""
    if use_cache and method.upper() in ("GET", "HEAD"):
        cached = _cache.get(url, method)
        if cached is not None:
            return cached

    rl = _get_rate_limiter(backend)
    ctx = _build_ssl_ctx(ignore_ssl)

    base_headers: Dict[str, str] = {
        "User-Agent": _ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
        "Cache-Control": "no-cache",
        "DNT": "1",
    }
    if headers:
        base_headers.update(headers)

    last_exc: Exception = RuntimeError("No fetch attempts made")

    for attempt in range(1, retries + 1):
        rl.wait()
        try:
            handlers: list = [urllib.request.HTTPSHandler(context=ctx)]
            if not follow_redirects:
                handlers.append(_NoRedirectHandler())
            opener = urllib.request.build_opener(*handlers)

            req = urllib.request.Request(
                url, headers=base_headers, method=method.upper()
            )
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read()

            _debug(f"[{backend}] {len(data):,} B ← {url[:70]}")

            if use_cache and method.upper() in ("GET", "HEAD"):
                _cache.put(url, data, method)

            return data

        except urllib.error.HTTPError as e:
            _debug(f"[{backend}] HTTP {e.code} attempt {attempt}/{retries}")
            if e.code in (429, 503):
                wait = (_RETRY_BACKOFF**attempt) + random.uniform(0.1, 1.5)
                time.sleep(wait)
            elif e.code in (403, 404):
                return None
            last_exc = e

        except (urllib.error.URLError, socket.timeout, ConnectionResetError) as e:
            _debug(f"[{backend}] Network err attempt {attempt}: {e}")
            time.sleep(_RETRY_BACKOFF * attempt)
            last_exc = e

        except Exception as e:
            _debug(f"[{backend}] Unexpected attempt {attempt}: {e}")
            last_exc = e

    _warn(f"[{backend}] All {retries} attempts failed: {last_exc} — {url[:70]}")
    return None


def _fetch_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    backend: str = "generic",
    **kw: Any,
) -> Optional[Any]:
    raw = _fetch(url, headers=headers, backend=backend, **kw)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        _debug(f"[{backend}] JSON decode error: {e}")
        return None


def _envelope(
    success: bool,
    mode: str,
    data: Optional[dict] = None,
    error: Optional[str] = None,
    **extra: Any,
) -> dict:
    out: dict = {
        "success": success,
        "mode": mode,
        "timestamp": _ts(),
    }
    if data:
        out.update(data)
    if error:
        out["error"] = error
    out.update(extra)
    return out


def _make_image_result(
    source: str,
    url: str,
    page_url: str,
    idx: int,
    title: str = "",
    snippet: str = "",
    score: float = 1.0,
) -> dict:
    return {
        "id": f"{source}_{idx + 1}",
        "title": title or f"{source.capitalize()} Media {idx + 1}",
        "url": url,
        "page_url": page_url,
        "snippet": snippet or f"{source.capitalize()} result",
        "score": round(max(0.0, score - idx * 0.02), 2),
        "type": "image",
        "source": source,
    }


# ==============================================================================
# MODE 1 — HEADERS ANALYSIS
# ==============================================================================


def _headers_mode(
    url: str,
    method: str = "HEAD",
    user_agent: Optional[str] = None,
    timeout: float = _REQUEST_TIMEOUT,
    ignore_ssl: bool = False,
    follow_redirects: bool = True,
    output_fmt: str = "json",
) -> dict:
    method = (method or "HEAD").upper().strip()
    ignore_ssl = _coerce_bool(ignore_ssl)
    timeout = _coerce_timeout(timeout)
    if method not in _ALLOWED_METHODS:
        method = "HEAD"

    ua_err = _validate_url(url)
    if ua_err:
        return _envelope(False, "headers", error=ua_err)

    extra_h: Dict[str, str] = {}
    if user_agent:
        extra_h["User-Agent"] = user_agent

    start = time.perf_counter()
    try:
        ctx = _build_ssl_ctx(ignore_ssl)
        base_h = {
            "User-Agent": user_agent or _ua(),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
        }
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if not _coerce_bool(follow_redirects):
            handlers.append(_NoRedirectHandler())
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=base_h, method=method)

        elapsed = 0.0
        raw_headers = {}
        status_code = 0
        final_url = url
        body_snippet = ""

        try:
            _get_rate_limiter("headers").wait()
            t0 = time.perf_counter()
            with opener.open(req, timeout=timeout) as r:
                elapsed = time.perf_counter() - t0
                raw_headers = _normalise_headers(dict(r.info()))
                status_code = r.getcode()
                final_url = r.geturl()
                if method == "GET":
                    body_snippet = r.read(512).decode("utf-8", errors="replace")

        except urllib.error.HTTPError as he:
            elapsed = time.perf_counter() - start
            raw_headers = _normalise_headers(dict(he.headers))
            status_code = he.code
            final_url = url
            if method == "GET":
                try:
                    body_snippet = he.read(512).decode("utf-8", errors="replace")
                except Exception:
                    pass

        result_data = {
            "url": final_url,
            "original_url": url,
            "status_code": status_code,
            "status_category": _classify_status(status_code),
            "elapsed_ms": round(elapsed * 1000, 2),
            "method": method,
            "ssl_verified": not ignore_ssl,
            "redirected": final_url != url,
            "headers": raw_headers,
            "server_info": _extract_server_info(raw_headers),
            "security_audit": _analyse_headers(raw_headers),
        }
        if body_snippet:
            result_data["body_snippet"] = body_snippet

        return _envelope(True, "headers", data=result_data)

    except ssl.SSLError as e:
        return _envelope(False, "headers", error=str(e), error_type="ssl_error")
    except socket.timeout:
        return _envelope(
            False, "headers", error=f"Timed out after {timeout}s", error_type="timeout"
        )
    except Exception as e:
        return _envelope(False, "headers", error=str(e), error_type="unexpected")


def _format_headers_pretty(result: dict) -> str:
    if not result.get("success"):
        return f"ERROR: {result.get('error')}"
    lines = [
        f"  URL          : {result.get('url')}",
        f"  Status       : {result.get('status_code')} ({result.get('status_category')})",
        f"  Elapsed      : {result.get('elapsed_ms')} ms",
        f"  Method       : {result.get('method')}",
        f"  SSL verified : {result.get('ssl_verified')}",
        f"  Redirected   : {result.get('redirected')}",
        "",
        "  Server info:",
    ]
    for k, v in (result.get("server_info") or {}).items():
        lines.append(f"    {k:<30}: {v}")
    audit = result.get("security_audit", {})
    lines += [
        "",
        "  Security audit:",
        f"    Score   : {audit.get('security_score_pct')}%",
        f"    Present : {', '.join(audit.get('security_headers_present') or ['-'])}",
        f"    Missing : {', '.join(audit.get('security_headers_missing') or ['-'])}",
        "",
        "  All headers:",
    ]
    for k, v in sorted((result.get("headers") or {}).items()):
        lines.append(f"    {k:<36}: {v}")
    return "\n".join(lines)


# ==============================================================================
# OSINT SEARCH BACKENDS
# ==============================================================================

_YANDEX_PATS = [
    re.compile(
        r'"origUrl"\s*:\s*"(https?://[^"]+?\.(?:jpe?g|png|gif|webp|avif))"', re.I
    ),
    re.compile(
        r'"img_href"\s*:\s*"(https?://[^"]+?\.(?:jpe?g|png|gif|webp|avif))"', re.I
    ),
    re.compile(
        r'https?://[^\s"\'<>]+?\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s"\'<>]*)?', re.I
    ),
]


def _backend_yandex(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    eq = f"{query} -hentai -anime -3d -drawn"

    for page in range(10):
        if len(results) >= limit:
            break
        params = {
            "text": eq,
            "itype": "jpg,png,gif,webp",
            "p": str(page),
            "fyandex": "0",
            "family": "no",
        }
        raw = _fetch(
            "https://yandex.ru/images/search?" + urllib.parse.urlencode(params),
            headers={
                "Referer": "https://yandex.ru/",
                "Cookie": "fyandex=0;yp=1800000000.szm.1_00_0;",
            },
            backend="yandex",
        )
        if raw is None:
            break
        body = raw.decode("utf-8", errors="replace")
        found = 0
        for pat in _YANDEX_PATS:
            for m in pat.finditer(body):
                u = urllib.parse.unquote(m.group(1) if pat.groups else m.group(0))
                if u.startswith("http") and u not in seen:
                    seen.add(u)
                    results.append(_make_image_result("yandex", u, u, len(results)))
                    found += 1
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        if found == 0:
            break

    return results[:limit], "yandex"


_BING_PATS = [
    re.compile(r'&quot;murl&quot;:&quot;(https?://[^&"]+?)&quot;'),
    re.compile(r'"murl"\s*:\s*"(https?://[^"]+)"'),
    re.compile(r'data-src="(https?://[^"]+?\.(?:jpe?g|png|gif|webp))"'),
]


def _backend_bing(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    first = 1

    for _ in range(10):
        if len(results) >= limit:
            break
        params = {"q": query, "first": str(first), "count": "35", "adlt": "off"}
        raw = _fetch(
            "https://www.bing.com/images/search?" + urllib.parse.urlencode(params),
            headers={
                "Referer": "https://www.bing.com/",
                "Cookie": "SRCHHPGUSR=ADLT=OFF;",
            },
            backend="bing",
        )
        if raw is None:
            break
        html_str = raw.decode("utf-8", errors="replace")
        found = 0
        for pat in _BING_PATS:
            for m in pat.finditer(html_str):
                u = urllib.parse.unquote(m.group(1))
                if u.startswith("http") and u not in seen and _has_image_ext(u):
                    seen.add(u)
                    results.append(_make_image_result("bing", u, u, len(results)))
                    found += 1
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        if found == 0:
            break
        first += 35

    return results[:limit], "bing"


def _backend_bing_dl(
    query: str, limit: int, media_dir: str = _DEFAULT_MEDIA_DIR
) -> Tuple[List[dict], str]:
    if not _BING_DL_AVAILABLE or _BING_DL_FN is None:
        _warn(f"bing_dl unavailable ({_BING_DL_ERR}), falling back to bing")
        return _backend_bing(query, limit)

    target = Path(media_dir).expanduser()
    if not _validate_sandbox(target):
        target = Path(_DEFAULT_MEDIA_DIR).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    kwargs: Dict[str, Any] = {"limit": limit, "output_dir": str(target)}
    try:
        _BING_DL_FN(query, **kwargs)
    except Exception as e:
        _warn(f"[bing_dl] Execution error: {e}")

    query_folder = target / query
    if not query_folder.is_dir():
        safe = re.sub(r'[\\/*?:"<>|]', "_", query)
        query_folder = target / safe

    results: List[dict] = []
    if query_folder.is_dir():
        for fname in sorted(query_folder.iterdir()):
            if fname.is_file() and _has_image_ext(fname.name):
                fp = str(fname)
                results.append(
                    {
                        "id": f"bing_dl_{len(results) + 1}",
                        "title": fname.name,
                        "url": f"file://{fp}",
                        "page_url": f"file://{fp}",
                        "local_path": fp,
                        "snippet": "Bing DL download",
                        "score": round(max(0.0, 1.0 - len(results) * 0.02), 2),
                        "type": "image",
                        "source": "bing_dl",
                    }
                )
                if len(results) >= limit:
                    break

    return results[:limit], "bing_dl"


def _backend_e621(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    page, per = 1, min(limit, 100)

    while len(results) < limit:
        params = {"tags": f"{query} type:gif", "limit": str(per), "page": str(page)}
        data = _fetch_json(
            "https://e621.net/posts.json?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "PyrmethusOSINT/4.0"},
            backend="e621",
        )
        if not data:
            break
        posts = data.get("posts", [])
        if not posts:
            break
        for item in posts:
            f = item.get("file", {})
            furl = f.get("url")
            if furl and furl not in seen:
                seen.add(furl)
                results.append(
                    _make_image_result(
                        "e621",
                        furl,
                        f"https://e621.net/posts/{item.get('id')}",
                        len(results),
                        title=f"e621 #{item.get('id')}",
                        score=round(item.get("score", {}).get("total", 0) / 10.0, 2),
                    )
                )
                if len(results) >= limit:
                    break
        if len(posts) < per:
            break
        page += 1

    return results[:limit], "e621"


def _backend_rule34(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    pid, per = 0, min(limit, 100)

    while len(results) < limit:
        params = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": "1",
            "tags": query,
            "limit": str(per),
            "pid": str(pid),
        }
        data = _fetch_json(
            "https://api.rule34.xxx/index.php?" + urllib.parse.urlencode(params),
            headers={"Referer": "https://rule34.xxx/"},
            backend="rule34",
        )
        if not data or not isinstance(data, list):
            break
        for item in data:
            furl = item.get("file_url") or item.get("sample_url")
            if furl and furl not in seen:
                seen.add(furl)
                results.append(
                    _make_image_result(
                        "rule34",
                        furl,
                        f"https://rule34.xxx/index.php?page=post&s=view&id={item.get('id')}",
                        len(results),
                        title=f"Rule34 #{item.get('id')}",
                        score=float(item.get("score", 0)),
                    )
                )
                if len(results) >= limit:
                    break
        if len(data) < per:
            break
        pid += 1

    return results[:limit], "rule34"


def _backend_danbooru(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    page, per = 1, min(limit, 200)

    while len(results) < limit:
        params = {"tags": query, "limit": str(per), "page": str(page)}
        data = _fetch_json(
            "https://danbooru.donmai.us/posts.json?" + urllib.parse.urlencode(params),
            headers={"Referer": "https://danbooru.donmai.us/"},
            backend="danbooru",
        )
        if not data or not isinstance(data, list):
            break
        for item in data:
            furl = item.get("file_url") or item.get("large_file_url")
            if furl and furl not in seen:
                seen.add(furl)
                results.append(
                    _make_image_result(
                        "danbooru",
                        furl,
                        f"https://danbooru.donmai.us/posts/{item.get('id')}",
                        len(results),
                        title=f"Danbooru #{item.get('id')}",
                        score=float(item.get("score", 0)),
                    )
                )
                if len(results) >= limit:
                    break
        if len(data) < per:
            break
        page += 1

    return results[:limit], "danbooru"


_BACKENDS: Dict[str, Callable] = {
    "yandex": _backend_yandex,
    "bing": _backend_bing,
    "e621": _backend_e621,
    "rule34": _backend_rule34,
    "danbooru": _backend_danbooru,
}


# ==============================================================================
# VSEARCH (VIDEO ENGINE SCRAPER)
# ==============================================================================

ENGINE_MAP: dict[str, dict[str, Any]] = {
    "pexels": {
        "url": "https://www.pexels.com",
        "search_path": "/search/videos/{query}/?page={page}",
        "video_item_selector": "article.MediaCard_card__6_MG7, article[data-testid='video-card'], div.MediaCard_card__6_MG7",
        "link_selector": "a.MediaCard_content__kA4yf, a[data-testid='video-card-link'], a",
        "title_selector": "a.MediaCard_content__kA4yf, img",
        "title_attribute": "alt",
        "img_selector": "img",
    },
    "yahoo_video": {
        "url": "https://video.search.yahoo.com",
        "search_path": "/search/video?p={query}&b={page}",
        "video_item_selector": "li.tile, li.type-video, div.video-tile",
        "link_selector": "a.video-tile, a.tile-link, a",
        "link_attribute": "data-referenceurl",
        "title_selector": "p.tile-title, p.text-primary, p",
        "img_selector": "img.tile-image, img",
        "time_selector": "p.time, span.time",
    },
    "dailymotion": {
        "url": "https://www.dailymotion.com",
        "search_path": "/search/{query}",
        "video_item_selector": "div[data-testid='video-card'], div.video-card, article",
        "link_selector": "a[href*='/video/'], a",
        "title_selector": "span[title], div[title], h2, h3",
        "title_attribute": "title",
        "img_selector": "img",
        "time_selector": "span.duration",
    },
    "pornhub": {
        "url": "https://www.pornhub.com",
        "search_path": "/video/search?search={query}&page={page}",
        "video_item_selector": "li.pcVideoListItem, .videoBox, div[data-vid]",
        "link_selector": "a.title, a[href*='/view_video.php']",
        "title_selector": "span.title a, .title a",
        "img_selector": "img[data-mediabook], img[data-src], img",
        "time_selector": ".duration",
    },
    "xvideos": {
        "url": "https://www.xvideos.com",
        "search_path": "/?k={query}&p={page}",
        "video_item_selector": "div.thumb-block, div.mozaique, div.video-item",
        "link_selector": "a.thumb, a.title, a[href*='/video']",
        "title_selector": ".thumb-under a, .title a",
        "img_selector": "img[data-src], img[data-lazy], img",
        "time_selector": ".duration",
    },
}


def _execute_vsearch(
    query: str, engine: str, limit: int = 20, page: int = 1, timeout: int = 15
) -> list[dict[str, Any]]:
    if not BS4_AVAILABLE:
        _warn("BeautifulSoup4 not installed — vsearch falling back to pattern matching")
        return []

    cfg = ENGINE_MAP.get(engine) or ENGINE_MAP["pexels"]
    base_url = cfg["url"]
    search_path = cfg["search_path"].format(
        query=urllib.parse.quote_plus(query), page=page
    )
    target_url = urllib.parse.urljoin(base_url, search_path)

    raw = _fetch(target_url, timeout=timeout, backend="vsearch")
    if not raw:
        return []

    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
    items = soup.select(cfg["video_item_selector"])
    results: list[dict[str, Any]] = []

    for idx, item in enumerate(items[:limit]):
        title = "Untitled"
        title_el = (
            item.select_one(cfg["title_selector"])
            if cfg.get("title_selector")
            else None
        )
        if title_el:
            attr = cfg.get("title_attribute")
            title = (
                title_el.get(attr)
                if attr and title_el.get(attr)
                else title_el.get_text(strip=True)
            ) or "Untitled"

        link = target_url
        link_el = (
            item.select_one(cfg["link_selector"]) if cfg.get("link_selector") else None
        )
        if link_el and link_el.get("href"):
            link = urllib.parse.urljoin(base_url, link_el["href"])

        img_url = ""
        img_el = (
            item.select_one(cfg["img_selector"]) if cfg.get("img_selector") else None
        )
        if img_el:
            img_url = img_el.get("data-src") or img_el.get("src") or ""
            if img_url and not img_url.startswith("http"):
                img_url = urllib.parse.urljoin(base_url, img_url)

        results.append(
            {
                "id": f"{engine}_{idx + 1}",
                "title": html.unescape(title),
                "url": link,
                "page_url": link,
                "img_url": img_url,
                "source": engine,
                "type": "video",
            }
        )

    return results


# ==============================================================================
# HEADER VERIFICATION & PARALLEL MEDIA DOWNLOADS
# ==============================================================================


def _verify_url_headers(
    url: str,
    mime_check: bool = False,
    ignore_ssl: bool = False,
    timeout: float = _REQUEST_TIMEOUT,
) -> dict:
    if not url.startswith("http"):
        return {"verified": False, "verify_error": "Non-HTTP URL"}
    try:
        ctx = _build_ssl_ctx(ignore_ssl)
        req = urllib.request.Request(url, headers={"User-Agent": _ua()}, method="HEAD")
        _get_rate_limiter("headers").wait()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            hdrs = _normalise_headers(dict(r.info()))
            ct = hdrs.get("content-type", "")
            cl = hdrs.get("content-length", "unknown")
            code = r.getcode()
            ok = not mime_check or _is_image_content_type(ct)
            return {
                "verified": ok,
                "verify_status": code,
                "content_type": ct,
                "content_length": cl,
                "mime_ok": _is_image_content_type(ct),
            }
    except Exception as e:
        return {"verified": False, "verify_error": str(e)}


def _download_single(
    res: dict, folder: str, mime_check: bool = False, ignore_ssl: bool = False
) -> dict:
    url = res.get("url") or res.get("img_url") or ""
    if not url.startswith("http"):
        res["dl_error"] = "Non-HTTP URL"
        return res

    fname = _safe_filename(url)
    path = os.path.join(folder, fname)

    if os.path.exists(path) and os.path.getsize(path) > 0:
        res["local_path"] = path
        res["dl_skipped"] = True
        return res

    raw = _fetch(
        url,
        timeout=_DOWNLOAD_TIMEOUT,
        backend="generic",
        ignore_ssl=ignore_ssl,
        use_cache=False,
    )
    if raw is None:
        res["dl_error"] = "Fetch failed"
        return res

    if mime_check and len(raw) >= 12:
        magic = raw[:12]
        is_media = any(
            [
                magic[:3] == b"\xff\xd8\xff",  # JPEG
                magic[:8] == b"\x89PNG\r\n\x1a\n",  # PNG
                magic[:6] in (b"GIF87a", b"GIF89a"),  # GIF
                magic[:4] == b"RIFF" and raw[8:12] == b"WEBP",  # WEBP
                b"ftyp" in magic,  # MP4/AVIF
                magic[:2] == b"BM",  # BMP
            ]
        )
        if not is_media:
            res["dl_error"] = "Magic byte check failed"
            return res

    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(raw)
        res["local_path"] = path
        res["dl_bytes"] = len(raw)
    except OSError as e:
        res["dl_error"] = str(e)

    return res


def _parallel_download(
    results: List[dict],
    folder: str,
    workers: int = _DEFAULT_WORKERS,
    mime_check: bool = False,
    ignore_ssl: bool = False,
) -> Tuple[List[dict], dict]:
    os.makedirs(folder, exist_ok=True)
    ok = fail = skipped = 0
    updated: List[dict] = []
    max_w = min(32, max(1, workers))

    with ThreadPoolExecutor(max_workers=max_w) as pool:
        futures = {
            pool.submit(_download_single, res, folder, mime_check, ignore_ssl): res
            for res in results
        }
        for fut in as_completed(futures):
            r = fut.result()
            updated.append(r)
            if r.get("dl_skipped"):
                skipped += 1
            elif r.get("local_path"):
                ok += 1
            else:
                fail += 1

    stats = {"dl_ok": ok, "dl_fail": fail, "dl_skipped": skipped}
    return updated, stats


# ==============================================================================
# REPORT GENERATION (HTML & CSV)
# ==============================================================================


def _html_report(data: dict) -> str:
    params = data.get("parameters", {})
    query = html.escape(str(params.get("query", data.get("query", ""))))
    results = data.get("results", [])
    count = data.get("count", len(results))
    ts = html.escape(str(data.get("timestamp", "")))

    cards = ""
    for res in results:
        raw_img = res.get("local_path") or res.get("img_url") or res.get("url", "")
        img = html.escape(raw_img)
        link = html.escape(res.get("page_url") or res.get("url") or img)
        title = html.escape(res.get("title", "Media Item"))
        src = html.escape(res.get("source", "unknown"))

        _PH = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='250' height='200'%3E%3Crect width='100%25' height='100%25' fill='%2222'/%3E%3C/svg%3E"
        cards += f"""
<div class="card">
  <a href="{img}" target="_blank" rel="noopener noreferrer">
    <img src="{img}" alt="{title}" loading="lazy" onerror="this.src='{_PH}'">
  </a>
  <div class="card-info">
    <div class="card-title"><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a></div>
    <div class="card-tags"><span class="tag">{src}</span></div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><title>OSINT Report: {query}</title>
<style>
body{{background:#0d0d0d;color:#e0e0e0;font-family:system-ui,sans-serif;padding:20px}}
h1{{color:#bb86fc;font-size:22px}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px;margin-top:20px}}
.card{{background:#1a1a1a;border-radius:8px;overflow:hidden;border:1px solid #2a2a2a}}
.card img{{width:100%;height:180px;object-fit:cover;display:block}}
.card-info{{padding:12px}}
.card-title a{{color:#bb86fc;text-decoration:none;font-weight:600;font-size:13px}}
.tag{{background:#2d2d2d;padding:2px 6px;border-radius:4px;font-size:11px;color:#aaa}}
</style>
</head>
<body>
<h1>&#128269; OSINT / Media Intelligence Report: "{query}"</h1>
<div>Results: <strong>{count}</strong> | {ts}</div>
<div class="gallery">{cards}</div>
</body></html>"""


def _csv_report(results: list[dict], out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Title", "URL", "Page URL", "Source", "Local Path"])
        for r in results:
            writer.writerow(
                [
                    r.get("id"),
                    r.get("title"),
                    r.get("url"),
                    r.get("page_url"),
                    r.get("source"),
                    r.get("local_path", ""),
                ]
            )
    return str(out_path)


# ==============================================================================
# MAIN EXECUTION MODES
# ==============================================================================


def _search_mode(
    query: str,
    limit: int = 15,
    platform: Optional[str] = None,
    deep: bool = False,
    tags: Optional[List[str]] = None,
    backend: str = "yandex",
    download: bool = False,
    media_dir: str = _DEFAULT_MEDIA_DIR,
    verify_headers: bool = False,
    mime_check: bool = False,
    ignore_ssl: bool = False,
    workers: int = _DEFAULT_WORKERS,
    timeout: float = _REQUEST_TIMEOUT,
) -> dict:
    q = (query or "").strip()
    if tags:
        q = q + " " + " ".join(t for t in tags if t)
    if platform:
        q = f"site:{platform} {q}"
    limit = max(1, min(limit, 1000))

    key = backend.lower().strip()
    results: List[dict] = []
    used = key

    if key == "bing_dl":
        results, used = _backend_bing_dl(q, limit, media_dir)
    elif key in _BACKENDS:
        results, used = _BACKENDS[key](q, limit)
    else:
        results, used = _backend_yandex(q, limit)

    if not results and key == "yandex":
        results, used = _backend_bing(q, limit)

    if verify_headers and results:
        verified: List[dict] = []
        with ThreadPoolExecutor(max_workers=min(16, workers)) as pool:
            futs = {
                pool.submit(
                    _verify_url_headers, r["url"], mime_check, ignore_ssl, timeout
                ): r
                for r in results
            }
            for fut in as_completed(futs):
                r = futs[fut]
                vrf = fut.result()
                r.update(vrf)
                if not mime_check or vrf.get("mime_ok", True):
                    verified.append(r)
        results = verified

    dl_stats: dict = {}
    if download and results:
        target = str(Path(media_dir).expanduser())
        results, dl_stats = _parallel_download(
            results, target, workers, mime_check, ignore_ssl
        )

    return _envelope(
        True,
        "search",
        data={
            "parameters": {
                "query": query,
                "limit": limit,
                "backend": used,
                "download": download,
            },
            "search_meta": {"backend": used, "total_found": len(results)},
            "download_stats": dl_stats,
            "results": results,
            "count": len(results),
        },
    )


def run(
    mode: str = "search",
    url: Optional[str] = None,
    method: str = "HEAD",
    follow_redirects: bool = True,
    output_fmt: str = "json",
    query: Optional[str] = None,
    engine: str = "pexels",
    limit: int = 15,
    page: int = 1,
    platform: Optional[str] = None,
    deep: bool = False,
    tags: Optional[List[str]] = None,
    backend: str = "yandex",
    download: bool = False,
    download_thumbs: bool = False,
    media_dir: str = _DEFAULT_MEDIA_DIR,
    verify_headers: bool = False,
    mime_check: bool = False,
    workers: int = _DEFAULT_WORKERS,
    user_agent: Optional[str] = None,
    timeout: float = _REQUEST_TIMEOUT,
    ignore_ssl: bool = False,
    cache_dir: str = _DEFAULT_CACHE_DIR,
    cache_ttl: int = _CACHE_TTL,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
    save: Optional[str] = None,
    html: Optional[str] = None,
    csv: Optional[str] = None,
    **kwargs: Any,
) -> dict:
    global _cache
    if cache_dir != _DEFAULT_CACHE_DIR:
        _cache = HttpCache(cache_dir, ttl=cache_ttl)

    mode = (mode or "search").lower().strip()

    if mode == "headers":
        if not url:
            return _envelope(
                False, "headers", error="--url is required for headers mode"
            )
        res = _headers_mode(
            url=url,
            method=method,
            user_agent=user_agent,
            timeout=timeout,
            ignore_ssl=ignore_ssl,
            follow_redirects=follow_redirects,
            output_fmt=output_fmt,
        )
        if output_fmt == "pretty" and res.get("success"):
            print(_format_headers_pretty(res))
            return res
        _persist(res, save, html, csv)
        return res

    if mode == "vsearch":
        if not query:
            return _envelope(
                False, "vsearch", error="--query is required for vsearch mode"
            )
        vresults = _execute_vsearch(
            query=query, engine=engine, limit=limit, page=page, timeout=int(timeout)
        )
        if download_thumbs:
            target = str(Path(media_dir).expanduser())
            vresults, _ = _parallel_download(
                vresults, target, workers, mime_check, ignore_ssl
            )
        res = _envelope(
            True,
            "vsearch",
            data={
                "query": query,
                "engine": engine,
                "results": vresults,
                "count": len(vresults),
            },
        )
        _persist(res, save, html, csv)
        return res

    if mode in ("search", "pipeline"):
        if not query:
            return _envelope(False, mode, error=f"--query is required for {mode} mode")
        should_dl = download or (mode == "pipeline")
        should_verify = verify_headers or (mode == "pipeline")
        res = _search_mode(
            query=query,
            limit=limit,
            platform=platform,
            deep=deep,
            tags=tags,
            backend=backend,
            download=should_dl,
            media_dir=media_dir,
            verify_headers=should_verify,
            mime_check=mime_check,
            ignore_ssl=ignore_ssl,
            workers=workers,
            timeout=timeout,
        )
        _persist(res, save, html, csv)
        return res

    return _envelope(False, "unknown", error=f"Unknown mode '{mode}'")


def _persist(
    result: dict, save: Optional[str], html_path: Optional[str], csv_path: Optional[str]
) -> None:
    if save:
        try:
            sp = Path(save).expanduser()
            if _validate_sandbox(sp):
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text(
                    json.dumps(
                        result, indent=2, ensure_ascii=False, cls=ToolJSONEncoder
                    ),
                    encoding="utf-8",
                )
        except Exception as e:
            _err(f"JSON save error: {e}")

    if html_path:
        try:
            hp = Path(html_path).expanduser()
            if _validate_sandbox(hp):
                hp.parent.mkdir(parents=True, exist_ok=True)
                hp.write_text(_html_report(result), encoding="utf-8")
        except Exception as e:
            _err(f"HTML save error: {e}")

    if csv_path:
        try:
            cp = Path(csv_path).expanduser()
            if _validate_sandbox(cp):
                _csv_report(result.get("results", []), cp)
        except Exception as e:
            _err(f"CSV save error: {e}")


def write_llm_output(data: dict[str, Any]) -> None:
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = (
        json.dumps(data, indent=2, ensure_ascii=False, cls=ToolJSONEncoder) + "\n"
    )

    if out_path in {"/dev/stdout", "/dev/fd/1", "-"}:
        sys.stdout.write(json_payload)
        sys.stdout.flush()
    else:
        out_file_path = Path(out_path).expanduser().resolve()
        if _validate_sandbox(out_file_path):
            try:
                out_file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_file_path, "a", encoding="utf-8") as fp:
                    fp.write(json_payload)
            except OSError as err:
                sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
                sys.stdout.write(json_payload)
                sys.stdout.flush()


# ==============================================================================
# CLI ARGUMENT PARSER & INTERRUPT HANDLER
# ==============================================================================


class GracefulShutdown:
    def __init__(self) -> None:
        self.old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        self.old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        sys.stderr.write(f"\n[INFO] Interrupted by signal {signum}. Exiting...\n")
        sys.exit(EXIT_INTERRUPTED)

    def restore(self) -> None:
        signal.signal(signal.SIGINT, self.old_sigint)
        signal.signal(signal.SIGTERM, self.old_sigterm)


if __name__ == "__main__":
    shutdown_handler = GracefulShutdown()

    ap = argparse.ArgumentParser(
        prog="osint",
        description="Pyrmethus Master OSINT & Media Intelligence Engine v4.0.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument(
        "--mode", default="search", choices=["headers", "search", "vsearch", "pipeline"]
    )
    ap.add_argument("--url", default=None)
    ap.add_argument("--method", default="HEAD")
    ap.add_argument(
        "--follow-redirects", dest="follow_redirects", action="store_true", default=True
    )
    ap.add_argument(
        "--output", dest="output_fmt", default="json", choices=["json", "pretty"]
    )
    ap.add_argument("--query", default=None)
    ap.add_argument("--engine", default="pexels", choices=list(ENGINE_MAP.keys()))
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--platform", default=None)
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--tags", action="append", metavar="TAG")
    ap.add_argument(
        "--backend", default="yandex", choices=list(_BACKENDS.keys()) + ["bing_dl"]
    )
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--download-thumbs", dest="download_thumbs", action="store_true")
    ap.add_argument("--media-dir", dest="media_dir", default=_DEFAULT_MEDIA_DIR)
    ap.add_argument("--verify-headers", dest="verify_headers", action="store_true")
    ap.add_argument("--mime-check", dest="mime_check", action="store_true")
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    ap.add_argument("--user-agent", dest="user_agent", default=None)
    ap.add_argument("--timeout", type=float, default=_REQUEST_TIMEOUT)
    ap.add_argument("--ignore-ssl", dest="ignore_ssl", action="store_true")
    ap.add_argument("--cache-dir", dest="cache_dir", default=_DEFAULT_CACHE_DIR)
    ap.add_argument("--cache-ttl", dest="cache_ttl", type=int, default=_CACHE_TTL)
    ap.add_argument("--use-cache", dest="use_cache", action="store_true")
    ap.add_argument("--no-color", dest="no_color", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--save", default=None)
    ap.add_argument("--html", default=None)
    ap.add_argument("--csv", default=None)

    ns = ap.parse_args()
    args = vars(ns)

    try:
        res = run(**args)
        print_human_readable_ui(res, no_color=args.get("no_color", False))
        write_llm_output(res)
        sys.exit(EXIT_SUCCESS if res.get("success") else EXIT_ERROR)
    finally:
        shutdown_handler.restore()
