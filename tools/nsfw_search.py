#!/usr/bin/env python3
# @describe Unified OSINT / Media Intelligence Platform (Pyrmethus Edition)
#
# Three tools, one coherent engine:
#   MODE 1 — headers   : Fetch & analyse HTTP headers for a URL
#   MODE 2 — search    : Multi-backend image/content search (OSINT)
#   MODE 3 — pipeline  : search → header-verify → download in one pass
#
# @option --mode <MODE>            headers | search | pipeline  (default: search)
#
# ── headers mode ──────────────────────────────────────────────────────────────
# @option --url <URL>              Target URL
# @option --method <METHOD>        HEAD | GET | OPTIONS  (default: HEAD)
# @option --follow-redirects       Follow HTTP redirects  (default: true)
# @option --output <FORMAT>        json | pretty          (default: json)
#
# ── search / pipeline mode ────────────────────────────────────────────────────
# @option --query <TEXT>           Search query
# @option --limit <NUM>            Maximum results        (default: 15)
# @option --platform <NAME>        Restrict to domain
# @option --tags <TAG>             Extra tag (repeatable)
# @option --backend <NAME>         yandex|bing|bing_dl|e621|rule34|danbooru
# @option --save <PATH>            Save JSON results
# @option --html <PATH>            Save HTML gallery
# @flag   --download               Download found images
# @option --media-dir <PATH>       Download directory     (default: ~/osint_media/)
# @flag   --verify-headers         Verify each URL's headers before downloading
# @flag   --mime-check             Reject downloads whose Content-Type isn't image/*
# @flag   --deep                   Multi-page deep search
#
# ── shared ────────────────────────────────────────────────────────────────────
# @option --user-agent <UA>        Custom User-Agent
# @option --timeout <SECONDS>      Request timeout        (default: 10)
# @flag   --ignore-ssl             Bypass SSL verification
# @option --cache-dir <PATH>       On-disk HTTP cache dir (default: ~/.osint_cache/)
# @option --workers <NUM>          Parallel download workers (default: 4)
# =============================================================================

from __future__ import annotations

import argparse
import datetime
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import socket
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════════════
# 0.  SILENCE NOISY THIRD-PARTY LOGGERS
# ══════════════════════════════════════════════════════════════════════════════
logging.getLogger().setLevel(logging.CRITICAL)
for _n in ("better_bing_image_downloader", "urllib3", "requests", "tqdm"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

# ══════════════════════════════════════════════════════════════════════════════
# 1.  OPTIONAL DEPENDENCIES
# ══════════════════════════════════════════════════════════════════════════════

# ── better-bing-image-downloader ─────────────────────────────────────────────
_BING_DL_AVAILABLE  = False
_BING_DL_FN: Any    = None
_BING_DL_SIG        = "unavailable"
_BING_DL_ERR        = ""

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
        _BING_DL_ERR = f"No callable found. dir={[x for x in dir(_bbid) if not x.startswith('_')]}"
except ImportError as _ie:
    _BING_DL_ERR = str(_ie)
except Exception as _oe:
    _BING_DL_ERR = f"probe error: {_oe}"

# ── colorama ──────────────────────────────────────────────────────────────────
try:
    from colorama import init as _cinit
    _cinit(autoreset=True)
except ImportError:
    pass

# ══════════════════════════════════════════════════════════════════════════════
# 2.  RUNTIME CONSTANTS & CONFIG
# ══════════════════════════════════════════════════════════════════════════════
_DOWNLOAD_TIMEOUT  = float(os.environ.get("OSINT_DOWNLOAD_TIMEOUT", "20"))
_REQUEST_TIMEOUT   = float(os.environ.get("OSINT_REQUEST_TIMEOUT",  "10"))
_MAX_RETRIES       = int(  os.environ.get("OSINT_MAX_RETRIES",  "3"))
_RETRY_BACKOFF     = float(os.environ.get("OSINT_RETRY_BACKOFF", "2.0"))
_RATE_INTERVAL     = float(os.environ.get("OSINT_RATE_INTERVAL", "1.2"))
_DEBUG             = os.environ.get("OSINT_DEBUG", "0").lower() in ("1", "true")
_IGNORE_SSL        = os.environ.get("OSINT_IGNORE_SSL", "0").lower() in ("1", "true")
_DEFAULT_WORKERS   = int(os.environ.get("OSINT_WORKERS", "4"))
_DEFAULT_MEDIA_DIR = os.environ.get("OSINT_MEDIA_DIR", "~/osint_media/")
_DEFAULT_CACHE_DIR = os.environ.get("OSINT_CACHE_DIR", "~/.osint_cache/")
_CACHE_TTL         = int(os.environ.get("OSINT_CACHE_TTL", "3600"))   # seconds

_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
]

_IMAGE_EXTS = re.compile(
    r'\.(jpe?g|png|gif|webp|avif|bmp|tiff?)(\?.*)?$', re.IGNORECASE
)

_SECURITY_HEADERS = [
    "strict-transport-security", "content-security-policy",
    "x-frame-options", "x-content-type-options", "referrer-policy",
    "permissions-policy", "x-xss-protection",
    "cross-origin-opener-policy", "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
]

_ALLOWED_METHODS = ("HEAD", "GET", "OPTIONS", "POST")

# ══════════════════════════════════════════════════════════════════════════════
# 3.  ANSI / LOGGING
# ══════════════════════════════════════════════════════════════════════════════

class Ansi:
    RED = '\033[31m'; GREEN = '\033[32m'; YELLOW = '\033[33m'
    CYAN = '\033[36m'; MAGENTA = '\033[35m'; BOLD = '\033[1m'; RESET = '\033[0m'

    @classmethod
    def _c(cls, code: str, m: str) -> str: return f"{code}{m}{cls.RESET}"
    @classmethod
    def red(cls, m):     return cls._c(cls.RED,     m)
    @classmethod
    def green(cls, m):   return cls._c(cls.GREEN,   m)
    @classmethod
    def yellow(cls, m):  return cls._c(cls.YELLOW,  m)
    @classmethod
    def cyan(cls, m):    return cls._c(cls.CYAN,    m)
    @classmethod
    def magenta(cls, m): return cls._c(cls.MAGENTA, m)


def _debug(m: str) -> None:
    if _DEBUG:
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(Ansi.cyan(f"[DBG {ts}] {m}"), file=sys.stderr)

def _warn(m: str)  -> None: print(Ansi.yellow(f"[WARN] {m}"), file=sys.stderr)
def _err(m: str)   -> None: print(Ansi.red(   f"[ERR ] {m}"), file=sys.stderr)
def _info(m: str)  -> None: print(Ansi.green(  f"[INFO] {m}"), file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════════
# 4.  SHARED UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ("true", "1", "yes", "on")

def _coerce_timeout(v: Any, default: float = _REQUEST_TIMEOUT) -> float:
    try:    return max(0.5, min(float(v), 120.0))
    except: return default

def _ua() -> str:
    import random; return random.choice(_USER_AGENTS)

def _has_image_ext(url: str) -> bool:
    return bool(_IMAGE_EXTS.search(url.split('?')[0]))

def _safe_filename(url: str, fallback_ext: str = "jpg") -> str:
    name = url.split('/')[-1].split('?')[0]
    name = re.sub(r'[\\/*?:"<>|]', '_', name)[:160]
    if not name or '.' not in name:
        ext  = (_IMAGE_EXTS.search(url) or type('x', (), {'group': lambda s, i: fallback_ext})()).group(1)
        name = f"img_{hashlib.md5(url.encode()).hexdigest()[:12]}.{ext}"
    return name

def _validate_url(url: str) -> Optional[str]:
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            return f"Unsupported scheme '{p.scheme}'. Only http/https allowed."
        if not p.netloc:
            return "URL missing host/netloc."
        return None
    except Exception as e:
        return f"URL parse error: {e}"

def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _classify_status(code: int) -> str:
    if code < 200: return "informational"
    if code < 300: return "success"
    if code < 400: return "redirection"
    if code < 500: return "client_error"
    return "server_error"

def _normalise_headers(raw: dict) -> dict:
    return {k.lower().strip(): str(v).strip() for k, v in raw.items()}

def _analyse_headers(h: dict) -> dict:
    present = [x for x in _SECURITY_HEADERS if x in h]
    missing = [x for x in _SECURITY_HEADERS if x not in h]
    return {
        "security_headers_present": present,
        "security_headers_missing": missing,
        "security_score_pct":       round(len(present) / len(_SECURITY_HEADERS) * 100),
    }

def _extract_server_info(h: dict) -> dict:
    keys = ("server", "x-powered-by", "via", "x-cache", "cf-ray",
            "content-type", "content-length", "cache-control",
            "etag", "last-modified", "expires", "age")
    return {k: h[k] for k in keys if k in h}

def _is_image_content_type(ct: str) -> bool:
    return ct.lower().startswith("image/")


# ══════════════════════════════════════════════════════════════════════════════
# 5.  SHARED RATE LIMITER
# ══════════════════════════════════════════════════════════════════════════════

class RateLimitState:
    """Thread-safe per-backend rate limiter with jitter."""

    def __init__(self, min_interval: float = _RATE_INTERVAL):
        import threading
        self._lock         = threading.Lock()
        self.last: float   = 0.0
        self.min_interval  = min_interval

    def wait(self) -> None:
        import random
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval + random.uniform(0.05, 0.35) - (now - self.last)
            if gap > 0:
                time.sleep(gap)
            self.last = time.monotonic()


_rl: Dict[str, RateLimitState] = {
    "yandex":   RateLimitState(1.5),
    "bing":     RateLimitState(1.2),
    "bing_dl":  RateLimitState(0.3),
    "e621":     RateLimitState(1.0),
    "rule34":   RateLimitState(0.8),
    "danbooru": RateLimitState(0.8),
    "generic":  RateLimitState(0.5),
    "headers":  RateLimitState(0.2),
}


# ══════════════════════════════════════════════════════════════════════════════
# 6.  ON-DISK HTTP CACHE
# ══════════════════════════════════════════════════════════════════════════════

class HttpCache:
    """
    Simple content-addressed file cache.
    Key  = SHA-256(url + method).
    Value = raw bytes.
    TTL enforced via mtime.
    """

    def __init__(self, cache_dir: str = _DEFAULT_CACHE_DIR, ttl: int = _CACHE_TTL):
        self.root = Path(cache_dir).expanduser()
        self.ttl  = ttl
        self.root.mkdir(parents=True, exist_ok=True)

    def _key_path(self, url: str, method: str = "GET") -> Path:
        digest = hashlib.sha256(f"{method}::{url}".encode()).hexdigest()
        return self.root / digest[:2] / digest

    def get(self, url: str, method: str = "GET") -> Optional[bytes]:
        p = self._key_path(url, method)
        if p.exists():
            age = time.time() - p.stat().st_mtime
            if age < self.ttl:
                _debug(f"[cache] HIT age={age:.0f}s {url[:60]}")
                return p.read_bytes()
            _debug(f"[cache] STALE age={age:.0f}s {url[:60]}")
        return None

    def put(self, url: str, data: bytes, method: str = "GET") -> None:
        p = self._key_path(url, method)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        _debug(f"[cache] PUT {len(data):,} B {url[:60]}")

    def invalidate(self, url: str, method: str = "GET") -> None:
        p = self._key_path(url, method)
        if p.exists():
            p.unlink()


_cache = HttpCache()


# ══════════════════════════════════════════════════════════════════════════════
# 7.  UNIFIED HTTP FETCH  (retry + cache + rate-limit)
# ══════════════════════════════════════════════════════════════════════════════

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_ssl_ctx(ignore_ssl: bool = _IGNORE_SSL) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ignore_ssl:
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _fetch(
    url:              str,
    headers:          Optional[Dict[str, str]] = None,
    method:           str   = "GET",
    timeout:          float = _REQUEST_TIMEOUT,
    retries:          int   = _MAX_RETRIES,
    backend:          str   = "generic",
    ignore_ssl:       bool  = _IGNORE_SSL,
    follow_redirects: bool  = True,
    use_cache:        bool  = True,
) -> Optional[bytes]:
    """
    Single authoritative fetch function used by every backend and tool.
    Features: caching · retry/back-off · per-backend rate-limiting ·
              pluggable SSL · redirect control · rotating UA.
    """
    import random

    # ── cache lookup ──────────────────────────────────────────────────────────
    if use_cache and method.upper() in ("GET", "HEAD"):
        cached = _cache.get(url, method)
        if cached is not None:
            return cached

    rl = _rl.get(backend, _rl["generic"])
    ctx = _build_ssl_ctx(ignore_ssl)

    base_headers: Dict[str, str] = {
        "User-Agent":                _ua(),
        "Accept":                    "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate",
        "Connection":                "close",
        "Cache-Control":             "no-cache",
        "DNT":                       "1",
        "Upgrade-Insecure-Requests": "1",
    }
    if headers:
        base_headers.update(headers)

    last_exc: Exception = RuntimeError("no attempts made")

    for attempt in range(1, retries + 1):
        rl.wait()
        try:
            handlers: list = [urllib.request.HTTPSHandler(context=ctx)]
            if not follow_redirects:
                handlers.append(_NoRedirectHandler())
            opener = urllib.request.build_opener(*handlers)

            req = urllib.request.Request(url, headers=base_headers, method=method.upper())
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read()

            _debug(f"[{backend}] {len(data):,} B ← {url[:70]}")

            if use_cache and method.upper() in ("GET", "HEAD"):
                _cache.put(url, data, method)

            return data

        except urllib.error.HTTPError as e:
            _debug(f"[{backend}] HTTP {e.code} attempt {attempt}/{retries}")
            if e.code in (429, 503):
                wait = _RETRY_BACKOFF * attempt + random.uniform(0, 1.5)
                _debug(f"[{backend}] throttled — sleeping {wait:.1f}s")
                time.sleep(wait)
            elif e.code == 403:
                _warn(f"[{backend}] 403 Forbidden — {url[:70]}")
                return None
            last_exc = e

        except (urllib.error.URLError, socket.timeout, ConnectionResetError) as e:
            _debug(f"[{backend}] network err attempt {attempt}: {e}")
            time.sleep(_RETRY_BACKOFF * attempt)
            last_exc = e

        except Exception as e:
            _debug(f"[{backend}] unexpected attempt {attempt}: {e}")
            last_exc = e

    _warn(f"[{backend}] all {retries} attempts failed: {last_exc} — {url[:70]}")
    return None


def _fetch_json(
    url:     str,
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
        _debug(f"[{backend}] JSON decode: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 8.  STANDARD RESULT ENVELOPE
# ══════════════════════════════════════════════════════════════════════════════

def _envelope(
    success: bool,
    mode:    str,
    data:    Optional[dict] = None,
    error:   Optional[str]  = None,
    **extra: Any,
) -> dict:
    """
    Shared JSON envelope — every mode returns this shape.
    {success, mode, timestamp, ...data or error}
    """
    out: dict = {
        "success":   success,
        "mode":      mode,
        "timestamp": _ts(),
    }
    if data:
        out.update(data)
    if error:
        out["error"] = error
    out.update(extra)
    return out


def _make_image_result(
    source:   str,
    url:      str,
    page_url: str,
    idx:      int,
    title:    str   = "",
    snippet:  str   = "",
    score:    float = 1.0,
) -> dict:
    return {
        "id":       f"{source}_{idx + 1}",
        "title":    title or f"{source.capitalize()} Image {idx + 1}",
        "url":      url,
        "page_url": page_url,
        "snippet":  snippet or f"{source.capitalize()} result",
        "score":    round(max(0.0, score - idx * 0.02), 2),
        "type":     "image",
        "source":   source,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 9.  MODE 1 — HEADERS TOOL
# ══════════════════════════════════════════════════════════════════════════════

def _headers_mode(
    url:              str,
    method:           str   = "HEAD",
    user_agent:       Optional[str] = None,
    timeout:          float = _REQUEST_TIMEOUT,
    ignore_ssl:       bool  = False,
    follow_redirects: bool  = True,
    output_fmt:       str   = "json",
) -> dict:
    method     = (method or "HEAD").upper().strip()
    ignore_ssl = _coerce_bool(ignore_ssl)
    timeout    = _coerce_timeout(timeout)
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
        # Try normal fetch first
        raw = _fetch(
            url,
            headers    = extra_h or None,
            method     = method,
            timeout    = timeout,
            backend    = "headers",
            ignore_ssl = ignore_ssl,
            follow_redirects = follow_redirects,
            use_cache  = False,
        )

        # _fetch returns None on 403+ — fall through to HTTPError path
        # We need the actual response object for status/headers so we
        # do a second direct call to grab metadata.
        ctx      = _build_ssl_ctx(ignore_ssl)
        base_h   = {
            "User-Agent":      user_agent or _ua(),
            "Accept":          "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection":      "close",
        }
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if not _coerce_bool(follow_redirects):
            handlers.append(_NoRedirectHandler())
        opener = urllib.request.build_opener(*handlers)
        req    = urllib.request.Request(url, headers=base_h, method=method)

        elapsed      = 0.0
        raw_headers  = {}
        status_code  = 0
        final_url    = url
        body_snippet = ""

        try:
            _rl["headers"].wait()
            t0 = time.perf_counter()
            with opener.open(req, timeout=timeout) as r:
                elapsed     = time.perf_counter() - t0
                raw_headers = _normalise_headers(dict(r.info()))
                status_code = r.getcode()
                final_url   = r.geturl()
                if method == "GET":
                    body_snippet = r.read(512).decode("utf-8", errors="replace")

        except urllib.error.HTTPError as he:
            elapsed     = time.perf_counter() - start
            raw_headers = _normalise_headers(dict(he.headers))
            status_code = he.code
            final_url   = url
            if method == "GET":
                try:
                    body_snippet = he.read(512).decode("utf-8", errors="replace")
                except Exception:
                    pass

        result_data = {
            "url":             final_url,
            "original_url":    url,
            "status_code":     status_code,
            "status_category": _classify_status(status_code),
            "elapsed_ms":      round(elapsed * 1000, 2),
            "method":          method,
            "ssl_verified":    not ignore_ssl,
            "redirected":      final_url != url,
            "headers":         raw_headers,
            "server_info":     _extract_server_info(raw_headers),
            "security_audit":  _analyse_headers(raw_headers),
        }
        if body_snippet:
            result_data["body_snippet"] = body_snippet

        return _envelope(True, "headers", data=result_data)

    except ssl.SSLError as e:
        return _envelope(False, "headers", error=str(e),
                         error_type="ssl_error",
                         hint="Try --ignore-ssl to bypass certificate verification.")
    except socket.timeout:
        return _envelope(False, "headers", error=f"Timed out after {timeout}s",
                         error_type="timeout")
    except urllib.error.URLError as e:
        return _envelope(False, "headers",
                         error=str(e.reason if hasattr(e, "reason") else e),
                         error_type="url_error")
    except Exception as e:
        return _envelope(False, "headers", error=str(e),
                         error_type="unexpected",
                         traceback=traceback.format_exc() if _DEBUG else None)


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
        "", "  Server info:",
    ]
    for k, v in (result.get("server_info") or {}).items():
        lines.append(f"    {k:<30}: {v}")
    audit = result.get("security_audit", {})
    lines += [
        "", "  Security audit:",
        f"    Score   : {audit.get('security_score_pct')}%",
        f"    Present : {', '.join(audit.get('security_headers_present') or ['-'])}",
        f"    Missing : {', '.join(audit.get('security_headers_missing') or ['-'])}",
        "", "  All headers:",
    ]
    for k, v in sorted((result.get("headers") or {}).items()):
        lines.append(f"    {k:<36}: {v}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 10. SEARCH BACKENDS
# ══════════════════════════════════════════════════════════════════════════════

# ── Yandex ────────────────────────────────────────────────────────────────────

_YANDEX_PATS = [
    re.compile(r'"origUrl"\s*:\s*"(https?://[^"]+?\.(?:jpe?g|png|gif|webp|avif))"',  re.I),
    re.compile(r'"img_href"\s*:\s*"(https?://[^"]+?\.(?:jpe?g|png|gif|webp|avif))"', re.I),
    re.compile(r'"url"\s*:\s*"(https?://[^"]+?\.(?:jpe?g|png|gif|webp|avif))"',      re.I),
    re.compile(r'https?://[^\s"\'<>]+?\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s"\'<>]*)?', re.I),
]


def _backend_yandex(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    eq = f"{query} -hentai -anime -3d -drawn"

    for page in range(10):
        if len(results) >= limit:
            break
        params = {"text": eq, "itype": "jpg,png,gif,webp",
                  "p": str(page), "fyandex": "0", "family": "no"}
        raw = _fetch(
            "https://yandex.ru/images/search?" + urllib.parse.urlencode(params),
            headers={"Referer": "https://yandex.ru/",
                     "Cookie":  "fyandex=0; yp=1800000000.szm.1_00_0;"},
            backend="yandex",
        )
        if raw is None:
            break
        body  = raw.decode("utf-8", errors="replace")
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
        _debug(f"Yandex p{page}: +{found}")
        if found == 0:
            break

    return results[:limit], "yandex"


# ── Bing scrape ───────────────────────────────────────────────────────────────

_BING_PATS = [
    re.compile(r'&quot;murl&quot;:&quot;(https?://[^&"]+?)&quot;'),
    re.compile(r'"murl"\s*:\s*"(https?://[^"]+)"'),
    re.compile(r'"imgurl"\s*:\s*"(https?://[^"]+)"'),
    re.compile(r'data-src="(https?://[^"]+?\.(?:jpe?g|png|gif|webp))"'),
    re.compile(r'src="(https?://tse\d+[^"]+?\.(?:jpe?g|png|gif|webp))"'),
]


def _backend_bing(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    first = 1

    for _ in range(10):
        if len(results) >= limit:
            break
        params = {"q": query, "first": str(first), "count": "35",
                  "adlt": "off", "safeSearch": "Off"}
        raw = _fetch(
            "https://www.bing.com/images/search?" + urllib.parse.urlencode(params),
            headers={"Referer": "https://www.bing.com/",
                     "Cookie":  "SRCHHPGUSR=ADLT=OFF&DM=0&SRCHLANG=en;"},
            backend="bing",
        )
        if raw is None:
            break
        html  = raw.decode("utf-8", errors="replace")
        found = 0
        for pat in _BING_PATS:
            for m in pat.finditer(html):
                u = urllib.parse.unquote(m.group(1))
                if u.startswith("http") and u not in seen and _has_image_ext(u):
                    seen.add(u)
                    results.append(_make_image_result("bing", u, u, len(results)))
                    found += 1
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        _debug(f"Bing off{first}: +{found}")
        if found == 0:
            break
        first += 35

    return results[:limit], "bing"


# ── Bing DL ───────────────────────────────────────────────────────────────────

def _probe_bing_dl_kwargs(fn: Any) -> set:
    try:
        return set(inspect.signature(fn).parameters.keys())
    except Exception:
        return set()


def _backend_bing_dl(
    query: str, limit: int, media_dir: str = _DEFAULT_MEDIA_DIR
) -> Tuple[List[dict], str]:
    if not _BING_DL_AVAILABLE or _BING_DL_FN is None:
        _warn(f"bing_dl unavailable ({_BING_DL_ERR}), falling back to bing")
        return _backend_bing(query, limit)

    target = Path(media_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    _info(f"[bing_dl] {_BING_DL_SIG} | '{query}' limit={limit}")

    supported = _probe_bing_dl_kwargs(_BING_DL_FN)
    kwargs: Dict[str, Any] = {"limit": limit, "output_dir": str(target)}
    if "adult_filter_off" in supported: kwargs["adult_filter_off"] = True
    if "force_replace"    in supported: kwargs["force_replace"]    = False
    if "timeout"          in supported: kwargs["timeout"]          = int(_DOWNLOAD_TIMEOUT)
    if "verbose"          in supported: kwargs["verbose"]          = False

    _root = logging.getLogger()
    _prev = _root.level
    _root.setLevel(logging.CRITICAL)
    try:
        _BING_DL_FN(query, **kwargs)
    except TypeError:
        try:    _BING_DL_FN(query, limit)
        except Exception as e: _warn(f"[bing_dl] positional call failed: {e}")
    except AttributeError as e:
        _debug(f"[bing_dl] non-fatal AttributeError (tqdm hook): {e}")
    except Exception as e:
        _warn(f"[bing_dl] {e}")
    finally:
        _root.setLevel(_prev)

    query_folder = target / query
    if not query_folder.is_dir():
        safe = re.sub(r'[\\/*?:"<>|]', '_', query)
        query_folder = target / safe

    results: List[dict] = []
    if query_folder.is_dir():
        for fname in sorted(query_folder.iterdir()):
            if fname.is_file() and _has_image_ext(fname.name):
                fp = str(fname)
                results.append({
                    "id":         f"bing_dl_{len(results)+1}",
                    "title":      fname.name,
                    "url":        f"file://{fp}",
                    "page_url":   f"file://{fp}",
                    "local_path": fp,
                    "snippet":    "Bing DL download",
                    "score":      round(max(0.0, 1.0 - len(results)*0.02), 2),
                    "type":       "image",
                    "source":     "bing_dl",
                })
                if len(results) >= limit:
                    break

    _info(f"[bing_dl] indexed {len(results)} files")
    return results[:limit], "bing_dl"


# ── e621 ──────────────────────────────────────────────────────────────────────

def _backend_e621(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    page, per = 1, min(limit, 100)

    while len(results) < limit:
        params = {"tags": f"{query} type:gif", "limit": str(per), "page": str(page)}
        data = _fetch_json(
            "https://e621.net/posts.json?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "PyrmethusOSINT/5.0", "Accept": "application/json"},
            backend="e621",
        )
        if not data:
            break
        posts = data.get("posts", [])
        if not posts:
            break
        for item in posts:
            f    = item.get("file", {})
            furl = f.get("url")
            if furl and furl not in seen:
                seen.add(furl)
                results.append(_make_image_result(
                    "e621", furl, f"https://e621.net/posts/{item.get('id')}",
                    len(results),
                    title=f"e621 #{item.get('id')}",
                    snippet=(f"Score: {item.get('score',{}).get('total',0)} | "
                             f"Ext: {f.get('ext')} | Rating: {item.get('rating','?')}"),
                    score=round(item.get("score", {}).get("total", 0) / 10.0, 2),
                ))
                if len(results) >= limit:
                    break
        if len(posts) < per:
            break
        page += 1

    return results[:limit], "e621"


# ── Rule34 ────────────────────────────────────────────────────────────────────

def _backend_rule34(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    pid, per  = 0, min(limit, 100)

    while len(results) < limit:
        params = {"page": "dapi", "s": "post", "q": "index",
                  "json": "1", "tags": query, "limit": str(per), "pid": str(pid)}
        data = _fetch_json(
            "https://api.rule34.xxx/index.php?" + urllib.parse.urlencode(params),
            headers={"Accept": "application/json", "Referer": "https://rule34.xxx/"},
            backend="rule34",
        )
        if not data or not isinstance(data, list):
            break
        for item in data:
            furl = item.get("file_url") or item.get("sample_url")
            if furl and furl not in seen:
                seen.add(furl)
                results.append(_make_image_result(
                    "rule34", furl,
                    f"https://rule34.xxx/index.php?page=post&s=view&id={item.get('id')}",
                    len(results),
                    title=f"Rule34 #{item.get('id')}",
                    snippet=(f"Score: {item.get('score',0)} | "
                             f"Rating: {item.get('rating','?')}"),
                    score=float(item.get("score", 0)),
                ))
                if len(results) >= limit:
                    break
        if len(data) < per:
            break
        pid += 1

    return results[:limit], "rule34"


# ── Danbooru ──────────────────────────────────────────────────────────────────

def _backend_danbooru(query: str, limit: int) -> Tuple[List[dict], str]:
    results: List[dict] = []
    seen: set = set()
    page, per = 1, min(limit, 200)

    while len(results) < limit:
        params = {"tags": query, "limit": str(per), "page": str(page)}
        data = _fetch_json(
            "https://danbooru.donmai.us/posts.json?" + urllib.parse.urlencode(params),
            headers={"Accept": "application/json",
                     "Referer": "https://danbooru.donmai.us/"},
            backend="danbooru",
        )
        if not data or not isinstance(data, list):
            break
        for item in data:
            furl = item.get("file_url") or item.get("large_file_url")
            if furl and furl not in seen:
                seen.add(furl)
                results.append(_make_image_result(
                    "danbooru", furl,
                    f"https://danbooru.donmai.us/posts/{item.get('id')}",
                    len(results),
                    title=f"Danbooru #{item.get('id')}",
                    snippet=(f"Score: {item.get('score',0)} | "
                             f"Ext: {item.get('file_ext','?')}"),
                    score=float(item.get("score", 0)),
                ))
                if len(results) >= limit:
                    break
        if len(data) < per:
            break
        page += 1

    return results[:limit], "danbooru"


# ── Registry ──────────────────────────────────────────────────────────────────

_BACKENDS: Dict[str, Callable] = {
    "yandex":   _backend_yandex,
    "bing":     _backend_bing,
    "e621":     _backend_e621,
    "rule34":   _backend_rule34,
    "danbooru": _backend_danbooru,
}
_ALL_BACKENDS = list(_BACKENDS.keys()) + ["bing_dl"]


# ══════════════════════════════════════════════════════════════════════════════
# 11. HEADER VERIFICATION  (search → headers integration)
# ══════════════════════════════════════════════════════════════════════════════

def _verify_url_headers(
    url:        str,
    mime_check: bool = False,
    ignore_ssl: bool = False,
    timeout:    float = _REQUEST_TIMEOUT,
) -> dict:
    """
    Run a HEAD request against a search result URL.
    Returns lightweight verification info to be merged into the result.
    """
    if not url.startswith("http"):
        return {"verified": False, "verify_error": "non-http url"}
    try:
        ctx = _build_ssl_ctx(ignore_ssl)
        req = urllib.request.Request(url, headers={"User-Agent": _ua()}, method="HEAD")
        _rl["headers"].wait()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            hdrs = _normalise_headers(dict(r.info()))
            ct   = hdrs.get("content-type", "")
            cl   = hdrs.get("content-length", "unknown")
            code = r.getcode()

            ok = True
            if mime_check and not _is_image_content_type(ct):
                ok = False

            return {
                "verified":          ok,
                "verify_status":     code,
                "content_type":      ct,
                "content_length":    cl,
                "mime_ok":           _is_image_content_type(ct),
            }
    except urllib.error.HTTPError as e:
        return {"verified": False, "verify_status": e.code,
                "verify_error": f"HTTP {e.code}"}
    except Exception as e:
        return {"verified": False, "verify_error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# 12. PARALLEL DOWNLOAD WITH RESUME
# ══════════════════════════════════════════════════════════════════════════════

def _download_single(
    res:        dict,
    folder:     str,
    mime_check: bool  = False,
    ignore_ssl: bool  = False,
) -> dict:
    """
    Download one result dict to folder.
    Supports resume (skip-if-exists).
    Returns updated result dict.
    """
    url = res.get("url", "")
    if not url.startswith("http"):
        res["dl_error"] = "non-http url, skipping"
        return res

    fname = _safe_filename(url)
    path  = os.path.join(folder, fname)

    # Resume: already downloaded
    if os.path.exists(path) and os.path.getsize(path) > 0:
        _debug(f"[dl] resume skip: {path}")
        res["local_path"] = path
        res["dl_skipped"] = True
        return res

    raw = _fetch(url, timeout=_DOWNLOAD_TIMEOUT, backend="generic",
                 ignore_ssl=ignore_ssl, use_cache=False)
    if raw is None:
        res["dl_error"] = "fetch returned None"
        return res

    # Optional MIME check from content sniff
    if mime_check:
        try:
            # Check the first 12 bytes for magic numbers
            magic = raw[:12]
            is_img = any([
                magic[:3]  == b'\xff\xd8\xff',          # JPEG
                magic[:8]  == b'\x89PNG\r\n\x1a\n',     # PNG
                magic[:6]  in (b'GIF87a', b'GIF89a'),   # GIF
                magic[:4]  == b'RIFF',                   # WEBP
                magic[:4]  == b'\x00\x00\x00\x0c',      # AVIF/MP4
            ])
            if not is_img:
                res["dl_error"] = "MIME check failed (magic bytes)"
                return res
        except Exception:
            pass

    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(raw)
        res["local_path"] = path
        res["dl_bytes"]   = len(raw)
        _debug(f"[dl] saved {len(raw):,} B → {path}")
    except OSError as e:
        res["dl_error"] = str(e)

    return res


def _parallel_download(
    results:    List[dict],
    folder:     str,
    workers:    int   = _DEFAULT_WORKERS,
    mime_check: bool  = False,
    ignore_ssl: bool  = False,
) -> Tuple[List[dict], dict]:
    """
    Download results concurrently.
    Returns (updated_results, stats).
    """
    os.makedirs(folder, exist_ok=True)
    ok = fail = skipped = 0
    updated: List[dict] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
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
    _info(f"Downloads complete — ok={ok} fail={fail} skipped={skipped}")
    return updated, stats


# ══════════════════════════════════════════════════════════════════════════════
# 13. MODE 2 — SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def _search_mode(
    query:          str,
    limit:          int                 = 15,
    platform:       Optional[str]       = None,
    deep:           bool                = False,
    tags:           Optional[List[str]] = None,
    backend:        str                 = "yandex",
    download:       bool                = False,
    media_dir:      str                 = _DEFAULT_MEDIA_DIR,
    verify_headers: bool                = False,
    mime_check:     bool                = False,
    ignore_ssl:     bool                = False,
    workers:        int                 = _DEFAULT_WORKERS,
    timeout:        float               = _REQUEST_TIMEOUT,
) -> dict:
    q = (query or "").strip()
    if tags:
        q = q + " " + " ".join(t for t in tags if t)
    if platform:
        q = f"site:{platform} {q}"
    limit = max(1, min(limit, 1000))

    meta: dict = {
        "query_final":       q,
        "search_backend":    backend,
        "deep":              deep,
        "platform":          platform,
        "bing_dl_available": _BING_DL_AVAILABLE,
    }

    key = backend.lower().strip()
    results: List[dict] = []
    used = key

    if key == "bing_dl":
        results, used = _backend_bing_dl(q, limit, media_dir)
        download = False  # already saved
    elif key in _BACKENDS:
        results, used = _BACKENDS[key](q, limit)
    else:
        _warn(f"Unknown backend '{backend}' — defaulting to yandex")
        results, used = _backend_yandex(q, limit)

    # Fallback chain
    if not results and key == "yandex":
        _warn("Yandex 0 results → bing")
        results, used = _backend_bing(q, limit)
    if not results and key in ("yandex", "bing"):
        _warn("Bing 0 results → bing_dl")
        results, used = _backend_bing_dl(q, limit, media_dir)
        download = False

    meta["search_backend"] = used
    meta["total_found"]    = len(results)

    # Header verification pass
    if verify_headers and results:
        _info(f"Verifying headers for {len(results)} URLs...")
        verified: List[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_verify_url_headers, r["url"], mime_check,
                            ignore_ssl, timeout): r
                for r in results
            }
            for fut in as_completed(futs):
                r   = futs[fut]
                vrf = fut.result()
                r.update(vrf)
                if mime_check and not vrf.get("mime_ok", True):
                    _debug(f"[verify] dropped non-image: {r['url'][:60]}")
                    continue
                verified.append(r)
        results = verified
        meta["after_verify"] = len(results)

    # Download pass
    dl_stats: dict = {}
    if download and results:
        target = str(Path(media_dir).expanduser())
        results, dl_stats = _parallel_download(
            results, target, workers, mime_check, ignore_ssl
        )

    return _envelope(True, "search", data={
        "parameters": {
            "query":    query,
            "limit":    limit,
            "backend":  used,
            "download": download,
            "media_dir": media_dir,
        },
        "search_meta":     meta,
        "download_stats":  dl_stats,
        "results":         results,
        "count":           len(results),
    })


# ══════════════════════════════════════════════════════════════════════════════
# 14. MODE 3 — PIPELINE  (search → verify → download)
# ══════════════════════════════════════════════════════════════════════════════

def _pipeline_mode(
    query:      str,
    limit:      int   = 15,
    backend:    str   = "yandex",
    media_dir:  str   = _DEFAULT_MEDIA_DIR,
    mime_check: bool  = False,
    ignore_ssl: bool  = False,
    workers:    int   = _DEFAULT_WORKERS,
    **kwargs: Any,
) -> dict:
    """
    Full pipeline: search → HEAD-verify every URL → MIME-check → parallel download.
    Returns a unified envelope with per-stage stats.
    """
    _info(f"[pipeline] START query='{query}' backend={backend} limit={limit}")

    stage_search = _search_mode(
        query          = query,
        limit          = limit,
        backend        = backend,
        download       = False,
        verify_headers = True,
        mime_check     = mime_check,
        ignore_ssl     = ignore_ssl,
        workers        = workers,
        **{k: v for k, v in kwargs.items()
           if k in ("platform", "tags", "deep", "timeout")},
    )

    results = stage_search.get("results", [])
    _info(f"[pipeline] after verify: {len(results)} results")

    target = str(Path(media_dir).expanduser())
    results, dl_stats = _parallel_download(
        results, target, workers, mime_check, ignore_ssl
    )
    _info(f"[pipeline] DONE — {dl_stats}")

    return _envelope(True, "pipeline", data={
        "query":          query,
        "backend":        backend,
        "search_meta":    stage_search.get("search_meta", {}),
        "download_stats": dl_stats,
        "results":        results,
        "count":          len(results),
    })


# ══════════════════════════════════════════════════════════════════════════════
# 15. HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def _html_report(data: dict) -> str:
    params  = data.get("parameters", {})
    query   = params.get("query", data.get("query", ""))
    results = data.get("results", [])
    meta    = data.get("search_meta", {})
    count   = data.get("count", 0)
    ts      = data.get("timestamp", "")

    cards = ""
    for res in results:
        img  = res.get("local_path") or res.get("url", "")
        link = res.get("page_url", img)
        src  = res.get("source", "unknown")
        _PH  = ("data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 "
                 "width=%22250%22 height=%22200%22%3E%3Crect width=%22100%25%22 "
                 "height=%22100%25%22 fill=%22%23222%22/%3E%3Ctext x=%2250%25%22 "
                 "y=%2250%25%22 dominant-baseline=%22middle%22 text-anchor=%22middle%22 "
                 "fill=%22%23666%22 font-size=%2214%22%3ENo Preview%3C/text%3E%3C/svg%3E")
        score_tag = (f"<span class='tag'>Score: {res['score']}</span>"
                     if "score" in res else "")
        mime_tag  = (f"<span class='tag mime-ok'>✓ image</span>"
                     if res.get("mime_ok") else "")
        cards += f"""
<div class="card">
  <a href="{img}" target="_blank" rel="noopener noreferrer">
    <img src="{img}" alt="{res.get('title','')}" loading="lazy"
         onerror="this.src='{_PH}'">
  </a>
  <div class="card-info">
    <div class="card-title">
      <a href="{link}" target="_blank" rel="noopener noreferrer">{res.get('title','Image')}</a>
    </div>
    <div class="card-snippet">{res.get('snippet','')}</div>
    <div class="card-tags">
      <span class="tag src-{src}">{src}</span>{score_tag}{mime_tag}
    </div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OSINT: {query}</title>
<style>
:root{{--bg:#0d0d0d;--card:#1a1a1a;--txt:#e0e0e0;--acc:#bb86fc;--bdr:#2a2a2a}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:'Segoe UI',system-ui,sans-serif;padding:20px}}
header{{margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--bdr)}}
h1{{font-size:22px;color:var(--acc);margin-bottom:8px}}
.meta{{font-size:12px;color:#888}}.meta span{{margin-right:14px}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}}
.card{{background:var(--card);border-radius:10px;overflow:hidden;
       border:1px solid var(--bdr);transition:transform .2s,box-shadow .2s;
       display:flex;flex-direction:column}}
.card:hover{{transform:translateY(-4px);box-shadow:0 8px 24px rgba(0,0,0,.5)}}
.card img{{width:100%;height:200px;object-fit:cover;display:block;background:#111}}
.card-info{{padding:12px;flex:1;display:flex;flex-direction:column;gap:6px}}
.card-title{{font-size:13px;font-weight:600}}
.card-title a{{color:var(--acc);text-decoration:none}}
.card-snippet{{font-size:11px;color:#777}}
.card-tags{{margin-top:auto;display:flex;flex-wrap:wrap;gap:4px}}
.tag{{display:inline-block;background:#2d2d2d;padding:2px 7px;border-radius:4px;font-size:11px;color:#aaa}}
.mime-ok{{background:#1a3a1a;color:#5f5}}
.src-yandex{{border-left:3px solid #f00}}.src-bing{{border-left:3px solid #0078d4}}
.src-bing_dl{{border-left:3px solid #00b4d8}}.src-e621{{border-left:3px solid #00a7b5}}
.src-rule34{{border-left:3px solid #f60}}.src-danbooru{{border-left:3px solid #0075f8}}
footer{{margin-top:32px;text-align:center;font-size:12px;color:#444;
        padding-top:16px;border-top:1px solid var(--bdr)}}
</style>
</head>
<body>
<header>
  <h1>&#128269; "{query}"</h1>
  <div class="meta">
    <span>Mode: <strong>{data.get('mode','?')}</strong></span>
    <span>Backend: <strong>{meta.get('search_backend','?')}</strong></span>
    <span>Results: <strong>{count}</strong></span>
    <span>{ts}</span>
  </div>
</header>
<div class="gallery">{cards}</div>
<footer>Pyrmethus OSINT Platform — Unified Edition</footer>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# 16. PUBLIC run() — unified entry-point
# ══════════════════════════════════════════════════════════════════════════════

def run(
    mode:             str   = "search",
    # headers
    url:              Optional[str]       = None,
    method:           str                 = "HEAD",
    follow_redirects: bool                = True,
    output_fmt:       str                 = "json",
    # search / pipeline
    query:            Optional[str]       = None,
    limit:            int                 = 15,
    platform:         Optional[str]       = None,
    deep:             bool                = False,
    tags:             Optional[List[str]] = None,
    backend:          str                 = "yandex",
    download:         bool                = False,
    media_dir:        str                 = _DEFAULT_MEDIA_DIR,
    verify_headers:   bool                = False,
    mime_check:       bool                = False,
    workers:          int                 = _DEFAULT_WORKERS,
    # shared
    user_agent:       Optional[str]       = None,
    timeout:          float               = _REQUEST_TIMEOUT,
    ignore_ssl:       bool                = False,
    cache_dir:        str                 = _DEFAULT_CACHE_DIR,
    # output
    save:             Optional[str]       = None,
    html:             Optional[str]       = None,
    **kwargs: Any,
) -> dict:
    """
    Unified entry-point.
    mode='headers'  → HTTP header analysis
    mode='search'   → multi-backend image search
    mode='pipeline' → search + verify + download in one pass
    """
    # Reconfigure cache if custom dir given
    global _cache
    if cache_dir != _DEFAULT_CACHE_DIR:
        _cache = HttpCache(cache_dir)

    mode = (mode or "search").lower().strip()

    # ── headers ───────────────────────────────────────────────────────────────
    if mode == "headers":
        if not url:
            return _envelope(False, "headers", error="--url is required for headers mode")
        result = _headers_mode(
            url=url, method=method, user_agent=user_agent, timeout=timeout,
            ignore_ssl=ignore_ssl, follow_redirects=follow_redirects,
            output_fmt=output_fmt,
        )
        if output_fmt == "pretty" and result.get("success"):
            print(_format_headers_pretty(result))
            return result
        _persist(result, save, html)
        return result

    # ── search ────────────────────────────────────────────────────────────────
    if mode == "search":
        if not query:
            return _envelope(False, "search", error="--query is required for search mode")
        result = _search_mode(
            query=query, limit=limit, platform=platform, deep=deep,
            tags=tags, backend=backend, download=download, media_dir=media_dir,
            verify_headers=verify_headers, mime_check=mime_check,
            ignore_ssl=ignore_ssl, workers=workers, timeout=timeout,
        )
        _persist(result, save, html)
        return result

    # ── pipeline ──────────────────────────────────────────────────────────────
    if mode == "pipeline":
        if not query:
            return _envelope(False, "pipeline", error="--query is required for pipeline mode")
        result = _pipeline_mode(
            query=query, limit=limit, backend=backend, media_dir=media_dir,
            mime_check=mime_check, ignore_ssl=ignore_ssl, workers=workers,
            platform=platform, tags=tags, deep=deep, timeout=timeout,
        )
        _persist(result, save, html)
        return result

    return _envelope(False, "unknown", error=f"Unknown mode '{mode}'. Use: headers|search|pipeline")


def _persist(result: dict, save: Optional[str], html: Optional[str]) -> None:
    if save:
        try:
            sp = Path(save).expanduser()
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            result["save_status"] = f"Saved → {sp}"
            _info(f"JSON → {sp}")
        except Exception as e:
            result["save_status"] = f"Save failed: {e}"
            _err(f"JSON save: {e}")

    if html:
        try:
            hp = Path(html).expanduser()
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(_html_report(result), encoding="utf-8")
            result["html_status"] = f"HTML → {hp}"
            _info(f"HTML → {hp}")
        except Exception as e:
            result["html_status"] = f"HTML failed: {e}"
            _err(f"HTML save: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 17. CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _bing_status = (
        f"available ({_BING_DL_SIG})"
        if _BING_DL_AVAILABLE
        else f"NOT INSTALLED — pip install better-bing-image-downloader"
    )

    ap = argparse.ArgumentParser(
        prog="osint",
        description="Pyrmethus Unified OSINT Platform — headers | search | pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Modes:
  headers   Fetch & analyse HTTP headers for a URL
  search    Multi-backend image/content search
  pipeline  search → verify → parallel download (one command)

Backends (--backend):
  yandex    Yandex Images scrape (default)
  bing      Bing Images scrape
  bing_dl   better-bing-image-downloader [{_bing_status}]
  e621      e621 API (gif posts)
  rule34    Rule34 JSON API
  danbooru  Danbooru JSON API

Env vars:
  OSINT_DEBUG=1          verbose debug output
  OSINT_IGNORE_SSL=1     skip SSL verification globally
  OSINT_MAX_RETRIES=3    HTTP retry count
  OSINT_RATE_INTERVAL=1.2  min seconds between requests
  OSINT_WORKERS=4        parallel download workers
  OSINT_CACHE_TTL=3600   cache TTL in seconds

Examples:
  # Analyse headers
  osint --mode headers --url https://example.com --output pretty

  # Search and save gallery
  osint --mode search --query "sunset beach" --limit 30 \\
        --backend bing_dl --html gallery.html

  # Full pipeline: search → verify MIME → download 20 images
  osint --mode pipeline --query "mountain lake" --limit 20 \\
        --mime-check --workers 8 --media-dir ~/photos/
""",
    )

    ap.add_argument("--mode",             default="search",
                    choices=["headers","search","pipeline"],
                    help="Operation mode (default: search)")
    # headers
    ap.add_argument("--url",              default=None)
    ap.add_argument("--method",           default="HEAD")
    ap.add_argument("--follow-redirects", dest="follow_redirects",
                    action="store_true",  default=True)
    ap.add_argument("--output",           dest="output_fmt", default="json",
                    choices=["json","pretty"])
    # search / pipeline
    ap.add_argument("--query",            default=None)
    ap.add_argument("--limit",            type=int,   default=15)
    ap.add_argument("--platform",         default=None)
    ap.add_argument("--deep",             action="store_true")
    ap.add_argument("--tags",             action="append", metavar="TAG")
    ap.add_argument("--backend",          default="yandex", choices=_ALL_BACKENDS)
    ap.add_argument("--download",         action="store_true")
    ap.add_argument("--media-dir",        dest="media_dir", default=_DEFAULT_MEDIA_DIR)
    ap.add_argument("--verify-headers",   dest="verify_headers", action="store_true")
    ap.add_argument("--mime-check",       dest="mime_check",      action="store_true")
    ap.add_argument("--workers",          type=int,   default=_DEFAULT_WORKERS)
    # shared
    ap.add_argument("--user-agent",       dest="user_agent", default=None)
    ap.add_argument("--timeout",          type=float, default=_REQUEST_TIMEOUT)
    ap.add_argument("--ignore-ssl",       dest="ignore_ssl", action="store_true")
    ap.add_argument("--cache-dir",        dest="cache_dir",  default=_DEFAULT_CACHE_DIR)
    # output
    ap.add_argument("--save",             default=None)
    ap.add_argument("--html",             default=None)

    ns   = ap.parse_args()
    args = vars(ns)

    if args["backend"] == "bing_dl" and not _BING_DL_AVAILABLE:
        _err(f"bing_dl not available: {_BING_DL_ERR}")
        _err("Install: pip install better-bing-image-downloader")
        sys.exit(1)

    result = run(**args)
    print(json.dumps(result, indent=2, ensure_ascii=False))