#!/usr/bin/env python3
# =============================================================================
# nsfw_search.py — NSFW OSINT Search Tool
#
# @describe Performs advanced NSFW-focused searches and optionally downloads
#           discovered images/media to local storage.
# @option --query! <TEXT>  The search query or target identifier
# @option --platform <TEXT>  Specific platform to target (e.g., twitter, reddit, onlyfans)
# @flag --deep              Enable deep search (more queries, more results)
# @option --limit! <NUM>    Number of results to return
# @option --tags* <TEXT>    Additional tags to refine the search
# @option --save <TEXT>     Path to save results JSON
# @flag --download          Enable automatic downloading of images found in results
# @option --media_dir <TEXT> Directory to save downloaded images (default: \~/nsfw_media/)
#
# Optional pip:
#   pip install ddgs duckduckgo-search bing-image-downloader
#
# Env:
#   NSFW_SEARCH_BACKEND=auto|ddgs|bing|ddg_html
#   NSFW_BING_ADULT_FILTER=off|on
#   NSFW_BING_DOWNLOAD=1
#   NSFW_BING_MIN_INTERVAL_SEC=1.0
#   NSFW_BING_JITTER_SEC=0.35
#   NSFW_BING_MAX_PAGES=8
#   NSFW_DOWNLOAD_INTERVAL_SEC=0
#   NSFW_DEBUG=1
# =============================================================================

import argparse
import json
import os
import sys
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import BoundedSemaphore
import datetime
import urllib.request
import urllib.error
import urllib.parse
import re
import hashlib
import html as html_lib
import tempfile
import shutil
import logging
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

_DOWNLOAD_TIMEOUT_SEC = float(os.environ.get("NSFW_DOWNLOAD_TIMEOUT_SEC", "15"))
_MAX_HTTP_READ_BYTES = 3 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = int(os.environ.get("NSFW_MAX_DOWNLOAD_BYTES", str(15 * 1024 * 1024)))
_MAX_GIF_DOWNLOAD_BYTES = int(os.environ.get("NSFW_MAX_GIF_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))
_MAX_GIF_FRAMES = int(os.environ.get("NSFW_MAX_GIF_FRAMES", "500"))
_MAX_FILENAME_LEN = 200
_MAX_TAGS = 20
_MAX_DEEP_QUERIES = 12
_DEFAULT_UA = os.environ.get(
    "NSFW_USER_AGENT",
    "Mozilla/5.0 (compatible; nsfw_search/1.3; +https://www.bing.com/images)"
)

_BING_MIN_INTERVAL = float(os.environ.get("NSFW_BING_MIN_INTERVAL_SEC", "1.0"))
_BING_JITTER_SEC = float(os.environ.get("NSFW_BING_JITTER_SEC", "0.35"))
_BING_MAX_PAGES = max(1, int(os.environ.get("NSFW_BING_MAX_PAGES", "8")))
_DOWNLOAD_INTERVAL = max(0.0, float(os.environ.get("NSFW_DOWNLOAD_INTERVAL_SEC", "0")))

_SAFESEARCH_OFF = "off"
_DDG_SAFESEARCH = os.environ.get("NSFW_DDG_SAFESEARCH", "off").strip().lower()
_BING_ADULT_FILTER = os.environ.get("NSFW_BING_ADULT_FILTER", "off").strip().lower()
_GOOGLE_MIN_INTERVAL = 1.0

class RateLimitState:
    def __init__(self, min_interval: float = 1.0, jitter: float = 0.1):
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.min_interval = min_interval
        self.jitter = jitter

    def wait(self, consecutive_errors: int = 0):
        with self.lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self.last_request)
            if wait > 0:
                jitter = random.uniform(0, self.jitter)
                backoff = min(2 ** consecutive_errors, 60) if consecutive_errors else 0
                time.sleep(wait + jitter + backoff)
            self.last_request = time.monotonic()

_bing_rl = RateLimitState(min_interval=_BING_MIN_INTERVAL, jitter=_BING_JITTER_SEC)
_download_rl = RateLimitState(min_interval=_DOWNLOAD_INTERVAL, jitter=0.0)
_google_rl = RateLimitState(min_interval=_GOOGLE_MIN_INTERVAL, jitter=0.0)

def _google_rate_limit_wait(consecutive_errors: int = 0) -> None:
    _google_rl.wait(consecutive_errors)

import logging.handlers

def setup_logging_enhanced(level: str = "INFO", max_bytes: int = 10*1024*1024, backup_count: int = 5):
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(console_handler)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            "nsfw_search.log", maxBytes=max_bytes, backupCount=backup_count, mode="a"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logger.addHandler(file_handler)
    except Exception as e:
        _debug(f"Failed to create log file handler: {e}")

def _safe_json_parse(data: str) -> dict:
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        _debug(f"JSON parse error: {e}")
        return {}

_PLATFORM_SITE_MAP = {
    "twitter": "twitter.com", "x": "twitter.com", "reddit": "reddit.com",
    "onlyfans": "onlyfans.com", "instagram": "instagram.com", "tiktok": "tiktok.com",
    "imgur": "imgur.com", "tumblr": "tumblr.com", "fansly": "fansly.com",
    "chaturbate": "chaturbate.com", "pornhub": "pornhub.com", "ph": "pornhub.com",
    "xvideos": "xvideos.com", "xv": "xvideos.com", "xhamster": "xhamster.com",
    "xnxx": "xnxx.com", "redtube": "redtube.com", "youporn": "youporn.com",
    "spankbang": "spankbang.com", "sb": "spankbang.com", "eporner": "eporner.com",
    "beeg": "beeg.com", "txxx": "txxx.com", "hclips": "hclips.com",
    "motherless": "motherless.com", "hqporner": "hqporner.com", "youjizz": "youjizz.com",
    "fapster": "fapster.xxx", "fapster.xxx": "fapster.xxx", "tube.xxx": "tube.xxx",
    "porn.xxx": "porn.xxx", "manyvids": "manyvids.com", "imagefap": "imagefap.com",
}

_TUBE_PLATFORM_KEYS = frozenset({
    "pornhub", "ph", "xvideos", "xv", "xhamster", "xnxx", "spankbang", "sb",
    "eporner", "youjizz", "fapster", "fapster.xxx", "hqporner",
})


def _debug(msg: str) -> None:
    if (os.environ.get("NSFW_DEBUG") or "").strip().lower() in ("1", "true", "yes"):
        print(f"[nsfw_search] {msg}", file=sys.stderr)


def llm_emit(text: str) -> None:
    out = (os.environ.get("LLM_OUTPUT") or "").strip()
    if not out or out == "/dev/stdout":
        print(text, end="" if text.endswith("\n") else "\n")
        return
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def load_env() -> None:
    """Load environment variables from .env file in parent repository root."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

def _search_images_google_cse(query: str, limit: int) -> Tuple[List[dict], str]:
    """Query Google CSE API for images."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    cx = os.environ.get("GOOGLE_SEARCH_ENGINE_ID")
    if not api_key or not cx:
        return [], "google_cse"
        
    _google_rate_limit_wait()
    url = "https://www.googleapis.com/customsearch/v1"
    safe_query = urllib.parse.quote(query, safe='')
    params = urllib.parse.urlencode({
        "key": api_key,
        "cx": cx,
        "q": safe_query,  # FIX: use encoded query
        "searchType": "image",
        "num": str(min(limit, 10))
    })
    try:
        status, body_bytes, hdrs = _http_get_safe(f"{url}?{params}")
        if status == 200:
            data = _safe_json_parse(body_bytes.decode("utf-8", errors="replace"))
            results = []
            for item in data.get("items", []):
                results.append({
                    "title": item.get("title"),
                    "url": item.get("link"),
                    "page_url": item.get("image", {}).get("contextLink", ""),
                    "snippet": item.get("snippet", ""),
                    "score": max(0.1, 1.0 - len(results) * 0.03),
                    "type": "image",
                    "source": "google_cse"
                })
            return results, "google_cse"
    except Exception as e:
        _debug(f"Google CSE image search failed: {e}")
    return [], "google_cse"

def _bing_rate_limit_wait(consecutive_errors: int = 0) -> None:
    _bing_rl.wait(consecutive_errors)

def _download_rate_limit_wait(consecutive_errors: int = 0) -> None:
    if _DOWNLOAD_INTERVAL > 0:
        _download_rl.wait(consecutive_errors)


def _http_get_safe(url: str, **kwargs) -> Tuple[int, bytes, dict]:
    max_retries = 3
    retry_codes = {429, 500, 502, 503, 504}
    for attempt in range(max_retries):
        try:
            status, body, headers = _http_get(url, **kwargs)
            if status in retry_codes:
                if attempt < max_retries - 1:
                    wait_time = min(2 ** attempt * 5, 60)
                    time.sleep(wait_time)
                    continue
            return status, body, headers
        except urllib.error.HTTPError as e:
            if e.code in retry_codes and attempt < max_retries - 1:
                wait_time = min(2 ** attempt * 5, 60)
                time.sleep(wait_time)
                continue
            raise
    return _http_get(url, **kwargs)

def validate_url(url: str) -> Tuple[bool, str]:
    if not url: return False, "Empty URL"
    if len(url) > 2048: return False, "URL exceeds maximum length"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https", "file"): return False, f"Invalid URL scheme: {parsed.scheme}"
    if parsed.scheme in ("http", "https"):
        if not parsed.netloc: return False, "No domain in URL"
        try:
            import ipaddress
            host = parsed.hostname
            if host:
                ip = ipaddress.ip_address(host)
                if ip.is_private: return False, "URL points to private IP address"
        except ValueError:
            pass
    for pattern in [r'file:///', r'javascript:', r'data:', r'vbscript:']:
        if re.search(pattern, url, re.IGNORECASE): return False, f"Dangerous URL pattern detected: {pattern}"
    return True, "Valid"

def _monitor_memory_usage():
    try:
        import psutil
        process = psutil.Process()
        memory_percent = process.memory_percent()
        if memory_percent > 80: _debug(f"High memory usage: {memory_percent:.1f}%")
        if memory_percent > 95: raise MemoryError(f"Memory usage critical: {memory_percent:.1f}%")
        return memory_percent
    except ImportError:
        pass
    return 0

def download_image_safe(url: str, folder: str, **kwargs) -> str:
    try:
        _monitor_memory_usage()
        return download_image(url, folder, **kwargs)
    except MemoryError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"

def _http_get(
    url: str,
    *,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    max_bytes: int = _MAX_HTTP_READ_BYTES,
) -> Tuple[int, bytes, dict]:
    hdrs = {"User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp:
        status = getattr(resp, "status", 200) or 200
        hdict = dict(resp.headers.items())
        chunks = []
        total = 0
        while True:
            block = resp.read(65536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                break
            chunks.append(block)
        return status, b"".join(chunks), hdict


def _safe_filename_from_url(url: str) -> str:
    # Remove query parameters more safely
    url_part = url.split('?')[0]
    base = re.sub(r'[\\/*?:"<>|]', '_', url_part.split('/')[-1])
    base = base.strip("._")[:_MAX_FILENAME_LEN]
    if base and len(base) >= 4 and "." in base:
        return base
    digest = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:16]
    # Add extension detection
    path = urllib.parse.urlparse(url).path.lower()
    ext = ".jpg"
    for possible_ext in [".png", ".gif", ".webp", ".bmp", ".jpeg", ".avif"]:
        if path.endswith(possible_ext):
            ext = possible_ext
            break
    return f"img_{digest}{ext}"


def _unique_path(folder: str, filename: str) -> str:
    filepath = os.path.join(folder, filename)
    if not os.path.exists(filepath):
        return filepath
    name, ext = os.path.splitext(filename)
    for i in range(1, 1000):
        candidate = os.path.join(folder, f"{name}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(
        folder, f"{name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    )


def validate_image_content(filepath: str) -> bool:
    try:
        if not os.path.exists(filepath): return False
        if os.path.getsize(filepath) == 0: return False
        with open(filepath, 'rb') as f: header = f.read(12)
        signatures = {
            b'\xff\xd8\xff': 'jpeg',
            b'\x89PNG\r\n\x1a\n': 'png',
            b'GIF87a': 'gif',
            b'GIF89a': 'gif',
            b'RIFF': 'webp',
        }
        for sig in signatures:
            if header.startswith(sig): return True
        return False
    except Exception as e:
        _debug(f"Image validation failed: {e}")
        return False

def sanitize_filename(filename: str) -> str:
    filename = os.path.basename(filename)
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:250] + ext[:5]
    return filename

def download_image(url: str, folder: str, *, retry: bool = True) -> str:
    last_err = ""
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        _download_rate_limit_wait()
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return "Error: invalid or unsupported URL scheme"

            os.makedirs(folder, exist_ok=True)
            filepath = _unique_path(folder, _safe_filename_from_url(url))

            headers = {
                "User-Agent": _DEFAULT_UA,
                "Accept": "image/*,*/*;q=0.8",
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            }
            req = urllib.request.Request(url, headers=headers)
            if "multipart/related" in url:
                req.add_header("Content-Type", "multipart/related")

            with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as response:
                ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream" or ctype == "multipart/related"):
                    return f"Error: unexpected content type {ctype}"

                is_gif = ".gif" in url.lower() or "gif" in ctype
                max_bytes = _MAX_GIF_DOWNLOAD_BYTES if is_gif else _MAX_DOWNLOAD_BYTES

                chunks: List[bytes] = []
                total = 0
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        return "Error: download exceeded size limit"
                    chunks.append(block)

            with open(filepath, "wb") as out_file:
                for block in chunks:
                    out_file.write(block)
            return filepath
        except urllib.error.HTTPError as e:
            last_err = f"Error: HTTP {e.code} {e.reason}"
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(min(5.0, 1.0 + attempt))
                continue
            return last_err
        except urllib.error.URLError as e:
            last_err = f"Error: {e.reason}"
            if attempt + 1 < attempts:
                time.sleep(1.0)
                continue
            return last_err
        except Exception as e:
            return f"Error: {str(e)}"
    return last_err or "Error: unknown"


def _normalize_platform(platform: Optional[str]) -> str:
    if not platform:
        return "all"
    return platform.strip().lower() or "all"


def _resolve_site_domain(platform: str) -> str:
    if platform == "all":
        return "all"
    key = platform.strip().lower()
    if key in _PLATFORM_SITE_MAP:
        return _PLATFORM_SITE_MAP[key]
    if re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", key):
        return key
    return key


def _build_queries(query: str, platform: str, deep: bool, tags: List[str]) -> List[str]:
    base = query.strip()

    def with_site(q: str) -> str:
        if platform == "all":
            return q
        site = _resolve_site_domain(platform)
        if "site:" in q.lower():
            return q
        return f"site:{site} {q}"

    queries: List[str] = [with_site(base)]
    if tags:
        queries.append(with_site(f"{base} {' '.join(tags)}"))
        if deep:
            for t in tags[:10]:
                queries.append(with_site(f"{base} {t}"))

    if deep and platform == "all":
        queries.extend([f"{base} profile username", f"{base} archive OR mirror"])
    elif deep and platform in _TUBE_PLATFORM_KEYS:
        site = _resolve_site_domain(platform)
        queries.extend([
            f"site:{site} {base} full video",
            f"site:{site} {base} HD",
        ])

    seen = set()
    out: List[str] = []
    for q in queries:
        qn = re.sub(r"\s+", " ", q).strip()
        if qn and qn not in seen:
            seen.add(qn)
            out.append(qn)
        if len(out) >= _MAX_DEEP_QUERIES:
            break
    return out or [base]


def _dedupe_results(items: List[dict], key: str = "url") -> List[dict]:
    seen = set()
    out: List[dict] = []
    for it in items:
        u = (it.get(key) or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(it)
    return out


def _load_ddgs_class() -> Tuple[Optional[Any], str]:
    forced = (os.environ.get("NSFW_SEARCH_BACKEND") or "").strip().lower()
    if forced in ("html", "ddg_html", "urllib"):
        return None, "ddg_html"
    for mod_name, cls_name in (("ddgs", "DDGS"), ("duckduckgo_search", "DDGS")):
        try:
            mod = __import__(mod_name, fromlist=[cls_name])
            return getattr(mod, cls_name), mod_name
        except ImportError:
            continue
    return None, "ddg_html"


class _DDGHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_result_link = False
        self._href_buf = ""
        self._title_buf = ""
        self.results: List[dict] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        ad = dict(attrs)
        if tag == "a" and "result__a" in (ad.get("class") or "").split():
            self._in_result_link = True
            self._href_buf = ad.get("href") or ""
            self._title_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_result_link:
            title = html_lib.unescape(re.sub(r"\s+", " ", self._title_buf).strip())
            href = self._resolve_ddg_redirect(self._href_buf)
            if href and title:
                self.results.append({"title": title, "url": href, "snippet": "", "score": 0.5})
            self._in_result_link = False

    def handle_data(self, data: str) -> None:
        if self._in_result_link:
            self._title_buf += data

    @staticmethod
    def _resolve_ddg_redirect(href: str) -> str:
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        if "uddg=" in href:
            parsed = urllib.parse.urlparse(href)
            uddg = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
            if uddg:
                return urllib.parse.unquote(uddg)
        return href


def _search_ddg_html(query: str, limit: int) -> List[dict]:
    data = urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode("utf-8")
    try:
        _, raw, _ = _http_get_safe("https://html.duckduckgo.com/html/",
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
            max_bytes=2 * 1024 * 1024,
        )
    except Exception as e:
        _debug(f"ddg_html failed: {e}")
        return []
    parser = _DDGHTMLParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return parser.results[:limit]


def _iter_ddgs_results(method: str, **kwargs: Any) -> List[dict]:
    DDGS, _ = _load_ddgs_class()
    if DDGS is None:
        return []
    rows: List[dict] = []
    try:
        with DDGS() as ddgs:
            gen = getattr(ddgs, method)(**kwargs)
            if gen:
                for item in gen:
                    if isinstance(item, dict):
                        rows.append(item)
    except Exception as e:
        _debug(f"DDGS iteration error: {e}")
    return rows


def _search_text_ddgs(queries: List[str], limit: int) -> Tuple[List[dict], str]:
    DDGS, backend = _load_ddgs_class()
    if DDGS is None:
        merged: List[dict] = []
        for q in queries:
            merged.extend(_search_ddg_html(q, limit))
            if len(merged) >= limit:
                break
        return _dedupe_results(merged)[:limit], "ddg_html"

    merged: List[dict] = []
    per_query = max(limit, 10) if len(queries) > 1 else limit
    for q in queries:
        try:
            raw = _iter_ddgs_results(
                "text", keywords=q, max_results=per_query, safesearch="off", region="wt-wt",
            )
        except TypeError:
            raw = _iter_ddgs_results("text", keywords=q, max_results=per_query)
        for i, r in enumerate(raw):
            href = r.get("href") or r.get("url") or ""
            merged.append({
                "title": r.get("title") or href or "untitled",
                "url": href,
                "snippet": (r.get("body") or r.get("snippet") or "")[:500],
                "score": max(0.1, 1.0 - i * 0.03),
            })
        if len(_dedupe_results(merged)) >= limit:
            break
    return _dedupe_results(merged)[:limit], backend


def _search_images_ddgs(query: str, limit: int) -> Tuple[List[dict], str]:
    DDGS, backend = _load_ddgs_class()
    if DDGS is None:
        return [], "ddg_html"
    try:
        raw = _iter_ddgs_results(
            "images", keywords=query, max_results=limit, safesearch="off", region="wt-wt",
        )
    except TypeError:
        raw = _iter_ddgs_results("images", keywords=query, max_results=limit)
    out: List[dict] = []
    for i, r in enumerate(raw):
        img_url = r.get("image") or r.get("thumbnail") or ""
        if not img_url:
            continue
        out.append({
            "title": r.get("title") or f"Image {i + 1}",
            "url": img_url,
            "page_url": r.get("url") or img_url,
            "source": r.get("source") or "",
            "snippet": "",
            "score": max(0.1, 1.0 - i * 0.04),
            "type": "image",
        })
    return _dedupe_results(out)[:limit], backend


def _bing_adult_param() -> str:
    mode = _BING_ADULT_FILTER
    return "off" if mode in ("off", "false", "0", "disable") else "strict" 


def _bing_rate_meta() -> dict:
    return {
        "bing_min_interval_sec": _BING_MIN_INTERVAL,
        "bing_jitter_sec": _BING_JITTER_SEC,
        "bing_max_pages": _BING_MAX_PAGES,
        "bing_adult_filter": _BING_ADULT_FILTER
    }


def _search_images_bing_scrape(query: str, limit: int) -> List[dict]:
    results: List[dict] = []
    seen = set()
    adlt = _bing_adult_param()
    offset = 0
    page_size = 35
    pages = 0

    while len(results) < limit and pages < _BING_MAX_PAGES:
        _bing_rate_limit_wait()
        pages += 1
        params = urllib.parse.urlencode({
            "q": query, "first": str(offset), "count": str(page_size), "adlt": adlt,
        })
        url = f"https://www.bing.com/images/async?{params}"
        try:
            status, body_bytes, hdrs = _http_get_safe(url, headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.bing.com/images/search",
                },
            )
            if status == 429:
                ra = hdrs.get("Retry-After", "5")
                pause = float(ra) if str(ra).isdigit() else 5.0
                time.sleep(min(pause, 60.0))
                continue
            body = body_bytes.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5.0)
                continue
            _debug(f"bing scrape HTTP {e.code}")
            break
        except Exception as e:
            _debug(f"bing scrape error: {e}")
            break

        found = False
        for murl in re.findall(r'murl&quot;:&quot;([^&]+?)&quot;', body):
            found = True
            img = html_lib.unescape(urllib.parse.unquote(murl))
            if not img.startswith(("http://", "https://")) or img in seen:
                continue
            seen.add(img)
            results.append({
                "title": f"Bing image {len(results) + 1}",
                "url": img,
                "page_url": img,
                "snippet": "",
                "score": max(0.1, 1.0 - len(results) * 0.03),
                "type": "image",
                "source": "bing",
            })
            if len(results) >= limit:
                break
        if not found:
            break
        offset += page_size

    return results[:limit]


def _search_images_bing_package(query: str, limit: int) -> List[dict]:
    try:
        from bing_image_downloader import downloader as bing_dl  # type: ignore
    except ImportError:
        return []

    _bing_rate_limit_wait()
    tmp = tempfile.mkdtemp(prefix="nsfw_bing_")
    try:
        bing_dl.download(
            query,
            limit=limit,
            output_dir=tmp,
            adult_filter_off=_bing_adult_param() == "off",
            force_replace=True,
            timeout=_DOWNLOAD_TIMEOUT_SEC,
        )
        out: List[dict] = []
        for root, _, files in os.walk(tmp):
            for fn in sorted(files):
                if len(out) >= limit:
                    break
                path = os.path.join(root, fn)
                if os.path.isfile(path):
                    out.append({
                        "title": fn,
                        "url": f"file://{path}",
                        "page_url": "",
                        "local_path": path,
                        "snippet": "",
                        "score": max(0.1, 1.0 - len(out) * 0.03),
                        "type": "image",
                        "source": "bing-image-downloader",
                    })
        return out
    except Exception as e:
        _debug(f"bing package download: {e}")
        return []
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:
            _debug(f"Failed to cleanup temp directory {tmp}: {e}")


def _search_images_bing(query: str, limit: int) -> Tuple[List[dict], str]:
    if (os.environ.get("NSFW_BING_DOWNLOAD") or "").strip().lower() in ("1", "true", "yes"):
        rows = _search_images_bing_package(query, limit)
        if rows:
            return rows, "bing-image-downloader"

    rows = _search_images_bing_scrape(query, limit)
    try:
        import bing_image_downloader  # noqa: F401
        label = "bing-image-downloader+scrape" if rows else "bing_scrape"
    except ImportError:
        label = "bing_scrape"
    return rows, label


def _looks_like_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return bool(re.search(r"\.(jpe?g|png|gif|webp|bmp|avif)(\?|$)", path))


def _pip_hints() -> List[str]:
    hints = []
    if _load_ddgs_class()[0] is None:
        hints.append("pip install ddgs")
    try:
        import bing_image_downloader  # noqa: F401
    except ImportError:
        hints.append("pip install bing-image-downloader")
    return hints


def perform_search(
    query: str,
    limit: int,
    platform: Optional[str] = None,
    deep: bool = False,
    tags: Optional[List[str]] = None,
    download: bool = False,
) -> Tuple[List[dict], dict]:
    platform_norm = _normalize_platform(platform)
    applied_tags = [str(t).strip() for t in (tags or []) if t and str(t).strip()][:_MAX_TAGS]
    queries = _build_queries(query, platform_norm, deep, applied_tags)
    primary_q = queries[0]
    backend_pref = (os.environ.get("NSFW_SEARCH_BACKEND") or "auto").strip().lower()

    meta: dict = {
        "queries_executed": queries,
        "search_backend": None,
        "mode": "images" if download else "web",
        "pip_hints": _pip_hints(),
        "safe_search_status": {
            "ddg": _DDG_SAFESEARCH,
            "bing": _BING_ADULT_FILTER,
            "all_disabled": _DDG_SAFESEARCH == "off" and _BING_ADULT_FILTER == "off"
        },
        **_bing_rate_meta(),
    }

    if download:
        results: List[dict] = []
        backend: Optional[str] = None

        if backend_pref in ("google", "google-image", "google_image", "google_cse", "google-cse"):
            results, backend = _search_images_google_cse(primary_q, limit)
        elif backend_pref in ("bing", "bing-image", "bing_image"):
            results, backend = _search_images_bing(primary_q, limit)
        elif backend_pref == "ddgs":
            results, backend = _search_images_ddgs(primary_q, limit)
        elif backend_pref == "ddg_html":
            results, backend = [], "ddg_html"
        elif backend_pref == "auto":
            results, backend = _search_images_google_cse(primary_q, limit)
            if not results:
                results, backend = _search_images_bing(primary_q, limit)
            if not results:
                results, backend = _search_images_ddgs(primary_q, limit)
        else:
            results, backend = _search_images_ddgs(primary_q, limit)

        meta["search_backend"] = backend

        if not results:
            web_rows, b2 = _search_text_ddgs(queries, limit * 2)
            meta["search_backend"] = b2
            results = [{**r, "type": "link"} for r in web_rows if _looks_like_image_url(r.get("url", ""))][:limit]
        if not results:
            web_rows, b3 = _search_text_ddgs(queries, limit)
            meta["search_backend"] = b3
            meta["mode"] = "web_fallback"
            results = web_rows
    else:
        if backend_pref in ("google", "google-image", "google_image", "google_cse", "google-cse"):
            results, backend = _search_images_google_cse(primary_q, limit)
            meta["mode"] = "images_preview"
        elif backend_pref in ("bing", "bing-image", "bing_image"):
            results, backend = _search_images_bing(primary_q, min(limit, 50))
            meta["mode"] = "images_preview"
        else:
            results, backend = _search_text_ddgs(queries, limit)
        meta["search_backend"] = backend

    if not results:
        meta["warning"] = "No results; check network, rate limits, or install pip_hints packages."

    return results, meta


def validate_limit(limit_value) -> int:
    try:
        limit = max(1, min(int(limit_value), 100))
    except (TypeError, ValueError):
        limit = 10
    except OverflowError:
        limit = 100
    return limit

class DownloadManager:
    def __init__(self, max_concurrent: int = 3):
        self.semaphore = BoundedSemaphore(max_concurrent)
        self.executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._download_lock = threading.Lock()
        self._last_download_time = 0.0

    def download_with_limit(self, url: str, folder: str) -> str:
        with self.semaphore:
            self._rate_limit()
            return download_image(url, folder)

    def _rate_limit(self):
        with self._download_lock:
            now = time.monotonic()
            wait = _DOWNLOAD_INTERVAL - (now - self._last_download_time)
            if wait > 0: time.sleep(wait)
            self._last_download_time = time.monotonic()

    def batch_download(self, urls: List[str], folder: str) -> List[dict]:
        futures = []
        results = []
        for url in urls:
            future = self.executor.submit(self.download_with_limit, url, folder)
            futures.append((url, future))
        for url, future in futures:
            try:
                path = future.result(timeout=_DOWNLOAD_TIMEOUT_SEC + 5)
                results.append({"url": url, "local_path": path, "ok": not str(path).startswith("Error:")})
            except Exception as e:
                results.append({"url": url, "local_path": f"Error: {str(e)}", "ok": False})
        return results

def validate_configuration() -> List[str]:
    warnings = []
    if _DOWNLOAD_INTERVAL < 0: warnings.append("NSFW_DOWNLOAD_INTERVAL_SEC should be >= 0")
    if _BING_MIN_INTERVAL < 0.5: warnings.append("NSFW_BING_MIN_INTERVAL_SEC should be >= 0.5")
    if _MAX_DOWNLOAD_BYTES > 100 * 1024 * 1024: warnings.append("NSFW_MAX_DOWNLOAD_BYTES is very high (>100MB)")
    if _MAX_GIF_DOWNLOAD_BYTES > 200 * 1024 * 1024: warnings.append("NSFW_MAX_GIF_DOWNLOAD_BYTES is very high (>200MB)")
    if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("NSFW_SEARCH_BACKEND"):
        warnings.append("No search backend configured.")
    return warnings

def run(
    query: str,
    limit: int,
    platform: Optional[str] = None,
    deep: bool = False,
    tags: Optional[List[str]] = None,
    save: Optional[str] = None,
    download: bool = False,
    media_dir: str = "~/nsfw_media/",
    **_: Any,
) -> dict:
    load_env()
    setup_logging_enhanced(os.environ.get("NSFW_LOG_LEVEL", "INFO"))

    config_warnings = validate_configuration()
    if config_warnings:
        for warning in config_warnings:
            logging.warning(warning)

    media_dir = media_dir or "~/nsfw_media/"
    target_dir = os.path.abspath(os.path.expanduser(media_dir))

    if ".." in target_dir or "~" in target_dir:
        return {
            "status": "error", "success": False, "error": "Invalid media_dir path",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    query = (query or "").strip()
    if not query:
        return {
            "status": "error", "success": False, "error": "query must be non-empty",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    limit = validate_limit(limit)
    platform_norm = _normalize_platform(platform)
    applied_tags = [str(t).strip() for t in (tags or []) if t and str(t).strip()][:_MAX_TAGS]

    search_params = {
        "target_query": query, "target_platform": platform_norm,
        "depth": "high" if deep else "standard", "result_limit": limit,
        "applied_tags": applied_tags, "download_enabled": download,
    }

    results, search_meta = perform_search(
        query=query, limit=limit, platform=platform_norm, deep=deep,
        tags=applied_tags, download=download,
    )

    downloaded_files: List[dict] = []
    if download and results:
        os.makedirs(target_dir, exist_ok=True)
        download_manager = DownloadManager(max_concurrent=3)
        urls = []

        for res in results:
            url = res.get("url") or ""
            existing = res.get("local_path")
            if existing and os.path.isfile(existing):
                dest = _unique_path(target_dir, _safe_filename_from_url(url))
                try:
                    shutil.copy2(existing, dest)
                    downloaded_files.append({"url": url or existing, "local_path": dest, "ok": True})
                except Exception as e:
                    downloaded_files.append({"url": url or existing, "local_path": f"Error: {e}", "ok": False})
            elif url.startswith("http"):
                urls.append(url)

        if urls:
            batch_results = download_manager.batch_download(urls, target_dir)
            downloaded_files.extend(batch_results)

    ok_downloads = sum(1 for d in downloaded_files if d.get("ok"))
    status = "success"

    if not results:
        status = "partial"
    elif download and downloaded_files and ok_downloads == 0:
        status = "partial"

    output_data = {
        "status": status, "success": status != "error", "config_warnings": config_warnings,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "parameters": search_params, "search_meta": search_meta, "results": results,
        "count": len(results), "downloads": downloaded_files if download else "Disabled",
    }

    if save:
        try:
            save_path = os.path.expanduser(save)
            parent = os.path.dirname(save_path)
            if parent: os.makedirs(parent, exist_ok=True)
            tmp_path = f"{save_path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(output_data, indent=4, ensure_ascii=False))
            os.replace(tmp_path, save_path)
            output_data["save_status"] = f"Saved to {save_path}"
        except Exception as e:
            output_data["save_status"] = f"Save failed: {str(e)}"

    return output_data

def _filter_run_kwargs(kwargs: dict) -> dict:
    valid_params = {
        "query", "limit", "platform", "deep", "tags",
        "save", "download", "media_dir"
    }
    return {k: v for k, v in kwargs.items() if k in valid_params}

if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1].lstrip().startswith(("{", "[")):
        try:
            kwargs = json.loads(sys.argv[1])
            if not isinstance(kwargs, dict):
                raise ValueError("expected JSON object")
            llm_emit(json.dumps(run(**_filter_run_kwargs(kwargs)), ensure_ascii=False))
            sys.exit(0)
        except TypeError as err:
            llm_emit(json.dumps({"success": False, "error": f"Invalid arguments: {err}"}))
            sys.exit(1)
        except Exception as err:
            llm_emit(json.dumps({"success": False, "error": f"JSON argument parse error: {err}"}))
            sys.exit(1)

    parser = argparse.ArgumentParser(description="NSFW OSINT Search Tool")
    parser.add_argument("--query", required=True)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--tags", action="append")
    parser.add_argument("--save", default=None)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--media_dir", default="~/nsfw_media/")
    ns = parser.parse_args()
    llm_emit(json.dumps(run(
        query=ns.query,
        platform=ns.platform,
        deep=ns.deep,
        limit=ns.limit,
        tags=ns.tags,
        save=ns.save,
        download=ns.download,
        media_dir=ns.media_dir,
    ), ensure_ascii=False))