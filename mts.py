#!/usr/bin/env python3
"""
mcp_tool_server.py – Enhanced local MCP tool server (Production Hardened v3.8)
Maintains full backward compatibility with all tool names, configurations, and parameter specs.
Adds advanced telemetry, robust caching, deep asynchronous/multithreaded safety, 
enhanced JSON‑RPC 2.0 schema discovery, resilient error recovery, self-diagnostic loops,
and an expanded configuration system.
"""

# ---------------------------------------------------------------------------
# Imports & Global Settings
# ---------------------------------------------------------------------------
import ast
import base64
import contextlib
import difflib
import fnmatch
import hashlib
import html as html_module
import io
import ipaddress
import itertools
import json
import logging
import os
import platform
import py_compile
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

# Terminal Colors for Termux / Visual Feedback
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Optional pydantic – falls back gracefully if missing
try:
    from pydantic import BaseModel, Field, ValidationError, field_validator
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False

# Optional psutil – falls back gracefully if missing
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
SANDBOX_ROOT = os.path.realpath(os.path.abspath(os.path.expanduser(
    os.getenv("MCP_SANDBOX_ROOT", "/data/data/com.termux/files/home")
)))
CONFIG_PATH = os.path.join(SANDBOX_ROOT, "mts_config.json")

# Load optional external config file (JSON) – allows users to tweak limits dynamically
_user_cfg: Dict[str, Any] = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _user_cfg = json.load(f)
    except Exception:
        _user_cfg = {}

def _cfg_val(key: str, env_name: str, default: Any, cast_type: Callable = str) -> Any:
    if key in _user_cfg:
        try:
            return cast_type(_user_cfg[key])
        except Exception:
            pass
    env_val = os.getenv(env_name)
    if env_val is not None:
        try:
            if cast_type == bool:
                return env_val.lower() in ("1", "true", "yes")
            return cast_type(env_val)
        except Exception:
            pass
    return default

# Core operational limits & settings
API_TOKEN = _cfg_val("api_token", "MCP_API_TOKEN", "")
REQUIRE_AUTH = _cfg_val("require_auth", "MCP_REQUIRE_AUTH", False, bool)
HTTP_PORT = _cfg_val("http_port", "MCP_HTTP_PORT", 8000, int)
LOG_LEVEL = _cfg_val("log_level", "MCP_LOG_LEVEL", "INFO")
ENABLE_DANGEROUS = _cfg_val("enable_dangerous", "MCP_ENABLE_DANGEROUS", False, bool)

# Search & request limits
MCP_MAX_SEARCH_RESULTS = _cfg_val("max_search_results", "MCP_MAX_SEARCH_RESULTS", 50, int)
MCP_MAX_REQUEST_BYTES = _cfg_val("max_request_bytes", "MCP_MAX_REQUEST_BYTES", 2 * 1024 * 1024, int)
MCP_MAX_RETRY_ATTEMPTS = _cfg_val("max_retry_attempts", "MCP_MAX_RETRY_ATTEMPTS", 3, int)
MCP_MAX_LINKS = _cfg_val("max_links", "MCP_MAX_LINKS", 500, int)
MCP_MAX_FEED_ITEMS = _cfg_val("max_feed_items", "MCP_MAX_FEED_ITEMS", 100, int)
MAX_HTTP_BYTES = _cfg_val("max_http_bytes", "MCP_MAX_HTTP_BYTES", 2 * 1024 * 1024, int)
MAX_TEXT_CHARS = _cfg_val("max_text_chars", "MCP_MAX_TEXT_CHARS", 64000, int)
HTTP_TIMEOUT = max(2, min(_cfg_val("http_timeout", "MCP_HTTP_TIMEOUT", 15, int), 120))
ALLOW_PRIVATE_NETWORKS = _cfg_val("allow_private_networks", "MCP_ALLOW_PRIVATE_NETWORKS", False, bool)
CORS_ORIGIN = _cfg_val("cors_origin", "MCP_CORS_ORIGIN", "*")
RETRY_BACKOFF = _cfg_val("retry_backoff", "MCP_RETRY_BACKOFF", 0.5, float)
MEMORY_FILE = os.path.expanduser("~/.mcp_memory.json")

# Ensure core sandbox directory exists safely
os.makedirs(SANDBOX_ROOT, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging Setup – Structured, Color-Aware
# ---------------------------------------------------------------------------
class _ColourFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        if record.levelno == logging.INFO:
            return f"{GREEN}{msg}{RESET}"
        if record.levelno == logging.WARNING:
            return f"{YELLOW}{msg}{RESET}"
        if record.levelno >= logging.ERROR:
            return f"{RED}{msg}{RESET}"
        return msg

log = logging.getLogger("mcp")
log.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
if log.handlers:
    for h in list(log.handlers):
        log.removeHandler(h)
handler = logging.StreamHandler()
handler.setFormatter(_ColourFormatter())
log.addHandler(handler)
log.propagate = False

# ---------------------------------------------------------------------------
# Telemetry & Server Diagnostics State Tracking
# ---------------------------------------------------------------------------
_server_stats_lock = threading.Lock()
_SERVER_STATS = {
    "start_time": time.time(),
    "total_requests": 0,
    "successful_calls": 0,
    "failed_calls": 0,
}

_tool_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
    "calls": 0,
    "success": 0,
    "failed": 0,
    "total_time": 0.0,
})

def _record_tool(name: str, success: bool, duration: float):
    with _server_stats_lock:
        stats = _tool_stats[name]
        stats["calls"] += 1
        if success:
            stats["success"] += 1
        else:
            stats["failed"] += 1
        stats["total_time"] += duration

# ---------------------------------------------------------------------------
# In-Memory Cache with Expiration (TTL)
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_response_cache: Dict[str, Tuple[float, Any]] = {}

def _cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        if key in _response_cache:
            expires, val = _response_cache[key]
            if time.time() < expires:
                return val
            del _response_cache[key]
    return None

def _cache_set(key: str, val: Any, ttl_seconds: float = 60.0):
    with _cache_lock:
        _response_cache[key] = (time.time() + ttl_seconds, val)

# ---------------------------------------------------------------------------
# Path Safety Verification
# ---------------------------------------------------------------------------
def _safe_path(filepath: str) -> str:
    """Resolve *filepath* inside SANDBOX_ROOT and reject path traversal attempts."""
    if not isinstance(filepath, str) or not filepath.strip():
        raise ValueError("filepath must be a non-empty string")
    root = os.path.realpath(SANDBOX_ROOT)
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(filepath)))
    try:
        if os.path.commonpath((root, candidate)) != root:
            raise ValueError(f"Access denied: path '{filepath}' is outside sandbox root '{SANDBOX_ROOT}'")
    except ValueError:
        raise ValueError(f"Access denied: path '{filepath}' is outside sandbox root '{SANDBOX_ROOT}'")
    return candidate

# ---------------------------------------------------------------------------
# URL Safety Helpers
# ---------------------------------------------------------------------------
def _validated_http_url(url: str) -> str:
    """Validate that *url* is a well-formed http/https URL without credentials."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only valid http:// or https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed")
    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise ValueError("Invalid URL port")
    return urllib.parse.urlunsplit(parsed)

def _host_is_private(host: str) -> bool:
    """Detect private, loopback, link-local, reserved, multicast, or metadata hosts."""
    host = (host or "").strip().strip("[]").lower()
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
    except (OSError, ValueError):
        return False
    return False

def _assert_safe_remote(url: str) -> None:
    """Run security checks on remote URLs prior to establishing requests."""
    normalized = _validated_http_url(url)
    host = urllib.parse.urlsplit(normalized).hostname or ""
    if not ALLOW_PRIVATE_NETWORKS and _host_is_private(host):
        raise ValueError("Private, loopback, link-local, reserved, multicast, or metadata destinations are blocked")

# ---------------------------------------------------------------------------
# Retry Decorator with Exponential Back-off
# ---------------------------------------------------------------------------
def _retry_on_exception(max_attempts: int = MCP_MAX_RETRY_ATTEMPTS,
                        backoff: float = RETRY_BACKOFF,
                        allowed_exceptions: Tuple[Exception, ...] = (urllib.error.URLError, subprocess.TimeoutExpired, TimeoutError)):
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except allowed_exceptions as e:
                    attempt += 1
                    if attempt > max_attempts:
                        raise
                    sleep = backoff * (2 ** (attempt - 1))
                    log.warning("Retry %d/%d after %.2f sec due to %s", attempt, max_attempts, sleep, e)
                    time.sleep(sleep)
        return wrapper
    return decorator

# ---------------------------------------------------------------------------
# Temporary File Context Manager
# ---------------------------------------------------------------------------
@contextmanager
def temporary_file(suffix: str = "", delete: bool = True) -> Generator[io.TextIOWrapper, None, None]:
    fd, path = tempfile.mkstemp(suffix=suffix)
    file_obj = open(fd, "w+", encoding="utf-8")
    try:
        yield file_obj
    finally:
        with contextlib.suppress(Exception):
            file_obj.close()
        if delete:
            with contextlib.suppress(Exception):
                os.unlink(path)

# ---------------------------------------------------------------------------
# Chunked, Size-Limited Stream Readers
# ---------------------------------------------------------------------------
def _read_limited_response(resp, limit: int = MAX_HTTP_BYTES) -> bytes:
    limit = max(1024, int(limit))
    chunks, total = [], 0
    while total < limit:
        to_read = min(65536, limit - total)
        chunk = resp.read(to_read)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise ValueError(f"Response exceeded configured limit of {limit} bytes")
    return b"".join(chunks)

def _decode_response(data: bytes, content_type: str = "") -> str:
    charset = None
    m = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    return data.decode(charset or "utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Robust HTTP Fetching Engine
# ---------------------------------------------------------------------------
@_retry_on_exception()
def _fetch_url(url: str, timeout: int = HTTP_TIMEOUT, max_bytes: int = MAX_HTTP_BYTES) -> str:
    current = _validated_http_url(url)
    timeout = max(2, min(int(timeout), 120))
    opener = urllib.request.build_opener()
    for _ in range(6):
        _assert_safe_remote(current)
        req = urllib.request.Request(
            current,
            headers={"User-Agent": "MCP-Tool-Server/3.8 (+compatible; robust-fetcher)"},
            method="GET",
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                final = _validated_http_url(resp.geturl())
                if final != current:
                    _assert_safe_remote(final)
                body = _read_limited_response(resp, max_bytes)
                return _decode_response(body, resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} while fetching {current}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error while fetching {current}") from e
    raise ValueError("Too many redirects")

# ---------------------------------------------------------------------------
# Advanced Rich HTML Parser
# ---------------------------------------------------------------------------
class _RichHTMLParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "nav", "footer", "header", "form"}
    BLOCK = {"p", "div", "article", "section", "main", "li", "blockquote", "pre", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.links: List[Dict[str, str]] = []
        self.meta: Dict[str, str] = {}
        self.canonical = ""
        self._skip = 0
        self._title_depth = 0
        self._title_parts: List[str] = []
        self._anchor_depth = 0
        self._anchor: Optional[Dict[str, str]] = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr_dict = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._title_depth += 1
        elif tag == "meta":
            key = attr_dict.get("name") or attr_dict.get("property") or attr_dict.get("itemprop")
            if key:
                self.meta[key.lower()] = attr_dict.get("content", "")
        elif tag == "link" and "canonical" in (attr_dict.get("rel", "").lower().split()):
            self.canonical = attr_dict.get("href", "")
        elif tag == "a":
            self._anchor_depth = 1
            self._anchor = {"url": attr_dict.get("href", ""), "text": ""}
        elif self._anchor_depth:
            self._anchor_depth += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag == "a" and self._anchor_depth:
            if self._anchor and self._anchor.get("url"):
                self.links.append(self._anchor)
            self._anchor = None
            self._anchor_depth = 0
        elif self._anchor_depth:
            self._anchor_depth = max(0, self._anchor_depth - 1)
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        self.parts.append(clean)
        if self._title_depth:
            self._title_parts.append(clean)
        if self._anchor_depth and self._anchor is not None:
            self._anchor["text"] += (" " if self._anchor["text"] else "") + clean

    def text(self) -> str:
        value = " ".join(self.parts)
        if len(value) > MAX_TEXT_CHARS * 2:
            value = value[:MAX_TEXT_CHARS * 2]
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n\s*", "\n", value)
        return value.strip()

def _extract_html_details(html: str, base_url: str = "") -> dict:
    parser = _RichHTMLParser()
    parser.feed(html)
    title = " ".join(parser._title_parts).strip()
    canonical = urllib.parse.urljoin(base_url, parser.canonical) if parser.canonical else ""
    links, seen = [], set()
    for link in parser.links:
        absolute = urllib.parse.urljoin(base_url, link["url"])
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in ("http", "https") or absolute in seen:
            continue
        seen.add(absolute)
        links.append({"text": re.sub(r"\s+", " ", link["text"]).strip()[:500], "url": absolute})
        if len(links) >= MCP_MAX_LINKS:
            break
    image = parser.meta.get("og:image", "")
    return {
        "title": title,
        "text": parser.text(),
        "description": parser.meta.get("description") or parser.meta.get("og:description") or "",
        "image": urllib.parse.urljoin(base_url, image) if image else "",
        "canonical_url": canonical,
        "meta_tags": parser.meta,
        "links": links,
    }

# ---------------------------------------------------------------------------
# Persistent Memory Store
# ---------------------------------------------------------------------------
_memory_lock = threading.Lock()

def _load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_memory(data: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE) or ".", exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def memory_store(key: str, value: str, tags: str = "") -> str:
    if not key or not isinstance(key, str):
        return _json_result({"error": "Key must be a non-empty string"})
    with _memory_lock:
        mem = _load_memory()
        mem[key] = {
            "value": value,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "updated": time.time()
        }
        _save_memory(mem)
    return _json_result({"status": "stored", "key": key})

def memory_recall(key: str = "", tag: str = "") -> str:
    try:
        with _memory_lock:
            mem = _load_memory()
            if key:
                item = mem.get(key)
                if not item:
                    return _json_result({"error": f"Key '{key}' not found"})
                return _json_result({"key": key, **item})
            if tag:
                matched = {k: v for k, v in mem.items() if tag in v.get("tags", [])}
                return _json_result({"tag": tag, "count": len(matched), "items": matched})
            return _json_result({"count": len(mem), "items": mem})
    except Exception as e:
        return _json_result({"error": str(e)})

def memory_list() -> str:
    try:
        with _memory_lock:
            mem = _load_memory()
            summary = [{"key": k, "tags": v.get("tags", []), "updated": v.get("updated")} for k, v in mem.items()]
            return _json_result({"count": len(summary), "keys": summary})
    except Exception as e:
        return _json_result({"error": str(e)})

# ---------------------------------------------------------------------------
# JSON Serialisation Helper
# ---------------------------------------------------------------------------
def _json_result(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)

def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default

# ---------------------------------------------------------------------------
# Core Tools Implementation
# ---------------------------------------------------------------------------
def web_search(
    query: str,
    max_results: int = 5,
    page: int = 1,
    domain: str = "",
    safe_search: bool = False,
) -> str:
    max_results = _bounded_int(max_results, 5, 1, MCP_MAX_SEARCH_RESULTS)
    page = _bounded_int(page, 1, 1, 1000)
    safe_search = False

    query = str(query or "").replace(r'\"', '"').replace(r'\:', ':').strip()
    domain = str(domain or "").strip().lstrip("@")
    search_query = query
    if domain and not re.search(r"(?:^|\s)site:", search_query, re.I):
        search_query = f"site:{domain} {search_query}".strip()

    results, engine_errors = [], []

    def add_result(title, href, snippet, engine):
        if not href:
            return
        href = html_module.unescape(str(href)).strip()
        if href.startswith("//"):
            href = "https:" + href
        if not re.match(r"^https?://", href, re.I):
            return
        title = html_module.unescape(re.sub(r"<[^>]+>", "", str(title or ""))).strip()
        snippet = html_module.unescape(re.sub(r"<[^>]+>", "", str(snippet or ""))).strip()
        if not title and not snippet:
            return
        results.append({
            "title": title[:1000],
            "url": href,
            "snippet": snippet[:3000],
            "engine": engine,
            "adult_filter": "enabled" if safe_search else "disabled",
        })

    # Bing Search
    try:
        first_param = (page - 1) * 10 + 1
        params = urllib.parse.urlencode({
            "q": search_query,
            "first": first_param,
            "adlt": "strict" if safe_search else "off",
            "setlang": "en-us",
        })
        url = f"https://www.bing.com/search?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": "SRCHHPGUSR=ADLT=OFF",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        for m in re.finditer(r'<li[^>]+class=["\']b_algo["\'][^>]*>(.*?)</li>', body, re.S | re.I):
            item = m.group(1)
            h2_m = re.search(r'<h2[^>]*>(.*?)</h2>', item, re.S | re.I)
            if not h2_m:
                continue
            link_m = re.search(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', h2_m.group(1), re.S | re.I)
            if not link_m:
                continue
            raw_href, title_raw = link_m.group(1), link_m.group(2)
            real_url = raw_href
            u_match = re.search(r'(?:[?&]|&amp;|^)u=a1([a-zA-Z0-9_-]+)', raw_href)
            if u_match:
                b64_str = u_match.group(1).replace('-', '+').replace('_', '/')
                b64_str += '=' * (-len(b64_str) % 4)
                with contextlib.suppress(Exception):
                    real_url = base64.b64decode(b64_str).decode('utf-8')
            real_url = html_module.unescape(real_url)
            snippet_m = re.search(r'<p[^>]*>(.*?)</p>', item, re.S | re.I)
            snippet = snippet_m.group(1) if snippet_m else ""
            add_result(title_raw, real_url, snippet, "bing")
            if len(results) >= max_results:
                break
    except Exception as e:
        engine_errors.append(f"bing: {e}")

    # DuckDuckGo HTML Search
    if len(results) < max_results:
        try:
            params = urllib.parse.urlencode({"q": search_query, "kp": "-2", "kl": "us-en"})
            url = f"https://html.duckduckgo.com/html/?{params}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/154.0 Mobile Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                html_body = resp.read().decode("utf-8", errors="ignore")
            for m in re.finditer(
                r'<a[^>]+class=["\']result__a["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>(.*?)(?=<div[^>]+class=["\']result|\Z)',
                html_body, re.S | re.I,
            ):
                href, title_raw, tail = m.group(1), m.group(2), m.group(3)
                parsed_href = urllib.parse.urlparse(href)
                if parsed_href.hostname and "duckduckgo.com" in parsed_href.hostname:
                    qs = urllib.parse.parse_qs(parsed_href.query)
                    if "uddg" in qs:
                        href = urllib.parse.unquote(qs["uddg"][0])
                snippet_m = re.search(r'class=["\']result__snippet["\'][^>]*>(.*?)</(?:a|div)>', tail, re.S | re.I)
                add_result(title_raw, href, snippet_m.group(1) if snippet_m else "", "duckduckgo_html")
                if len(results) >= max_results:
                    break
        except Exception as e:
            engine_errors.append(f"duckduckgo_html: {e}")

    deduped, seen = [], set()
    for item in results:
        key = item["url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_results:
            break

    payload = {
        "query": query,
        "domain": domain,
        "page": page,
        "count": len(deduped),
        "safe_search": safe_search,
        "adult_results_allowed": not safe_search,
        "results": deduped,
    }
    if engine_errors:
        payload["engine_errors"] = engine_errors[:5]
    if not deduped:
        payload["error"] = f"No search results found for: {query}"
    return _json_result(payload)

def fetch_webpage_text(url: str, max_chars: int = 8000) -> str:
    try:
        max_chars = _bounded_int(max_chars, 8000, 256, MAX_TEXT_CHARS)
        html = _fetch_url(url)
        details = _extract_html_details(html, url)
        text = details["text"]
        return _json_result({
            "url": url,
            "title": details["title"],
            "length": min(len(text), max_chars),
            "truncated": len(text) > max_chars,
            "text": text[:max_chars]
        })
    except Exception as e:
        return _json_result({"error": f"Failed to fetch webpage: {e}"})

def read_file(filepath: str, start_line: int = 1, line_count: int = 500) -> str:
    try:
        path = _safe_path(filepath)
        start_line = _bounded_int(start_line, 1, 1, 10_000_000)
        line_count = _bounded_int(line_count, 500, 1, 20_000)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            start_idx = start_line - 1
            end_idx = start_idx + line_count
            sliced_lines = list(itertools.islice(f, start_idx, end_idx))
            f.seek(0)
            total_lines = sum(1 for _ in f)
            
        return _json_result({
            "filepath": filepath,
            "resolved_path": path,
            "total_lines": total_lines,
            "start_line": start_line,
            "returned_lines": len(sliced_lines),
            "content": "".join(sliced_lines)
        })
    except Exception as e:
        return _json_result({"error": f"Error reading file: {e}"})

def write_file(filepath: str, content: str) -> str:
    try:
        path = _safe_path(filepath)
        if len(content.encode("utf-8")) > MCP_MAX_REQUEST_BYTES:
            raise ValueError("Content exceeds configured file write limit")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return _json_result({"status": "success", "filepath": filepath, "bytes_written": len(content.encode("utf-8"))})
    except Exception as e:
        return _json_result({"error": f"Error writing file: {e}"})

def edit_file_replace(filepath: str, target_snippet: str, replacement_snippet: str, replace_all: bool = False) -> str:
    try:
        path = _safe_path(filepath)
        if not os.path.exists(path):
            return _json_result({"error": f"File '{filepath}' does not exist."})
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if target_snippet not in content:
            return _json_result({"error": f"Target snippet not found in {filepath}."})
        count = content.count(target_snippet)
        if not replace_all and count > 1:
            return _json_result({"error": f"Target snippet appears {count} times. Set replace_all=true."})
        updated = content.replace(target_snippet, replacement_snippet) if replace_all else content.replace(target_snippet, replacement_snippet, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
        return _json_result({"status": "success", "filepath": filepath, "replacements_made": count if replace_all else 1})
    except Exception as e:
        return _json_result({"error": f"Error editing file: {e}"})

def append_to_file(filepath: str, content: str) -> str:
    try:
        path = _safe_path(filepath)
        if len(content.encode("utf-8")) > MCP_MAX_REQUEST_BYTES:
            raise ValueError("Content exceeds configured write limit")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return _json_result({"status": "success", "filepath": filepath, "bytes_appended": len(content.encode("utf-8"))})
    except Exception as e:
        return _json_result({"error": f"Error appending: {e}"})

def list_directory(path: str = ".") -> str:
    try:
        target = _safe_path(path)
        items = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            with contextlib.suppress(OSError):
                is_dir = os.path.isdir(full)
                items.append({
                    "name": name,
                    "type": "directory" if is_dir else "file",
                    "size_bytes": os.path.getsize(full) if not is_dir else 0,
                    "modified_time": time.ctime(os.path.getmtime(full))
                })
        return _json_result({"path": target, "count": len(items), "items": items})
    except Exception as e:
        return _json_result({"error": f"Error listing directory: {e}"})

ALLOWED_BASH_CMDS: Set[str] = {"echo", "cat", "head", "tail", "wc", "grep", "sed", "awk", "chmod", "mkdir", "rm", "cp", "mv", "date", "sleep"}

def lint_code(language: str, code: str = "", filepath: str = "") -> str:
    try:
        target_code = code
        if filepath:
            with open(_safe_path(filepath), "r", encoding="utf-8") as f:
                target_code = f.read()
        lang = language.lower().strip()

        if lang == "python":
            try:
                ast.parse(target_code)
                with temporary_file(".py", delete=False) as tf:
                    tf.write(target_code)
                    tf.flush()
                    tmp_path = tf.name
                try:
                    result = subprocess.run([sys.executable or "python3", "-m", "py_compile", tmp_path],
                                          capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        return _json_result({"status": "ok", "language": "python", "message": "Syntax + compile OK"})
                    return _json_result({"status": "syntax_error", "language": "python", "error": result.stderr})
                finally:
                    with contextlib.suppress(Exception):
                        os.unlink(tmp_path)
            except SyntaxError as e:
                return _json_result({
                    "status": "syntax_error",
                    "language": "python",
                    "line": e.lineno,
                    "text": e.text,
                    "error": str(e),
                })

        elif lang in ("javascript", "js"):
            with temporary_file(".js", delete=False) as tf:
                tf.write(target_code)
                tf.flush()
                tmp_path = tf.name
            try:
                result = subprocess.run(["node", "--check", tmp_path], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    return _json_result({"status": "ok", "language": "javascript", "message": "Syntax OK"})
                return _json_result({"status": "syntax_error", "language": "javascript", "error": result.stderr})
            finally:
                with contextlib.suppress(Exception):
                    os.unlink(tmp_path)

        elif lang == "json":
            json.loads(target_code)
            return _json_result({"status": "ok", "language": "json", "message": "Valid JSON"})

        elif lang in ("bash", "sh"):
            if not any(cmd in target_code.split() for cmd in ALLOWED_BASH_CMDS):
                return _json_result({"error": "Shell code contains disallowed commands"})
            result = subprocess.run(["bash", "-n"], input=target_code, text=True, capture_output=True, timeout=5)
            if result.returncode == 0:
                return _json_result({"status": "ok", "language": "bash", "message": "Syntax OK"})
            return _json_result({"status": "syntax_error", "language": "bash", "error": result.stderr})

        return _json_result({"error": f"Unsupported language: {language}"})
    except Exception as e:
        return _json_result({"error": f"Linting failed: {e}"})

def execute_python_code(code: str, timeout_seconds: int = 30) -> str:
    if not ENABLE_DANGEROUS:
        return _json_result({"error": "Dangerous tool execution disabled. Set MCP_ENABLE_DANGEROUS=true."})
    start = time.time()
    try:
        with temporary_file(".py", delete=False) as tf:
            tf.write(code)
            tf.flush()
            tmp = tf.name
        result = subprocess.run(
            [sys.executable or "python3", tmp],
            cwd=SANDBOX_ROOT,
            capture_output=True, text=True, timeout=_bounded_int(timeout_seconds, 30, 1, 120)
        )
        _record_tool("python_exec", True, time.time() - start)
        return _json_result({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "execution_time_sec": round(time.time() - start, 3),
        })
    except subprocess.TimeoutExpired:
        _record_tool("python_exec", False, time.time() - start)
        return _json_result({"error": f"Timed out after {timeout_seconds}s"})
    except Exception as e:
        _record_tool("python_exec", False, time.time() - start)
        return _json_result({"error": str(e)})
    finally:
        with contextlib.suppress(Exception):
            if 'tmp' in locals() and os.path.exists(tmp):
                os.unlink(tmp)

def execute_javascript_code(code: str, timeout_seconds: int = 30) -> str:
    if not ENABLE_DANGEROUS:
        return _json_result({"error": "Dangerous tool execution disabled. Set MCP_ENABLE_DANGEROUS=true."})
    start = time.time()
    try:
        with temporary_file(".js", delete=False) as tf:
            tf.write(code)
            tf.flush()
            tmp = tf.name
        result = subprocess.run(
            ["node", tmp],
            cwd=SANDBOX_ROOT,
            capture_output=True, text=True, timeout=_bounded_int(timeout_seconds, 30, 1, 120)
        )
        _record_tool("js_exec", True, time.time() - start)
        return _json_result({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "execution_time_sec": round(time.time() - start, 3),
        })
    except FileNotFoundError:
        return _json_result({"error": "Node.js not installed"})
    except subprocess.TimeoutExpired:
        return _json_result({"error": f"Timed out after {timeout_seconds}s"})
    except Exception as e:
        return _json_result({"error": str(e)})
    finally:
        with contextlib.suppress(Exception):
            if 'tmp' in locals() and os.path.exists(tmp):
                os.unlink(tmp)

def execute_bash_command(command: str, timeout_seconds: int = 60) -> str:
    if not ENABLE_DANGEROUS:
        return _json_result({"error": "Dangerous tool execution disabled. Set MCP_ENABLE_DANGEROUS=true."})
    tokens = command.strip().split()
    if not tokens:
        return _json_result({"error": "Empty command"})
    if tokens[0] not in ALLOWED_BASH_CMDS:
        return _json_result({"error": f"Command '{tokens[0]}' is not in the allowed whitelist."})
    if any(op in command for op in [';', '&&', '||', '|', '`', '$(']):
        return _json_result({"error": "Command chaining and subshells are disabled for security."})
    try:
        start = time.time()
        res = subprocess.run(
            command, shell=True, cwd=SANDBOX_ROOT, capture_output=True, text=True,
            timeout=_bounded_int(timeout_seconds, 60, 1, 300)
        )
        _record_tool("bash_exec", True, time.time() - start)
        return _json_result({"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.returncode})
    except subprocess.TimeoutExpired:
        return _json_result({"error": f"Timed out after {timeout_seconds}s"})
    except Exception as e:
        return _json_result({"error": str(e)})

def ping() -> str:
    with _server_stats_lock:
        uptime = time.time() - _SERVER_STATS["start_time"]
        total_reqs = _SERVER_STATS["total_requests"]
        succ_calls = _SERVER_STATS["successful_calls"]
        fail_calls = _SERVER_STATS["failed_calls"]
    _record_tool("ping", True, 0)
    return _json_result({
        "status": "pong",
        "server": "mcp-enhanced-wizard",
        "version": "3.8.0",
        "time": time.time(),
        "uptime_seconds": round(uptime, 2),
        "telemetry": {
            "total_requests": total_reqs,
            "successful_calls": succ_calls,
            "failed_calls": fail_calls,
            "tool_stats": dict(_tool_stats)
        },
        "python_version": sys.version,
        "platform": platform.platform()
    })

def system_info() -> str:
    try:
        info = {
            "platform": platform.platform(),
            "python": sys.version,
            "processor": platform.processor(),
            "cwd": os.getcwd(),
            "sandbox_root": SANDBOX_ROOT,
            "user": os.getenv("USER", "unknown"),
            "termux": os.path.exists("/data/data/com.termux"),
            "env_vars_count": len(os.environ),
            "memory_usage_mb": psutil.virtual_memory().used / (1024**2) if HAS_PSUTIL else "N/A",
            "open_files": len(psutil.Process().open_files()) if HAS_PSUTIL else "N/A"
        }
        return _json_result(info)
    except Exception as e:
        return _json_result({"error": str(e)})

def file_checksum(filepath: str, algorithm: str = "sha256") -> str:
    try:
        path = _safe_path(filepath)
        if not os.path.exists(path):
            return _json_result({"error": f"File '{filepath}' not found"})
        hasher = getattr(hashlib, algorithm.lower(), hashlib.sha256)()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return _json_result({
            "filepath": filepath,
            "algorithm": algorithm,
            "checksum": hasher.hexdigest(),
            "size_bytes": os.path.getsize(path)
        })
    except Exception as e:
        return _json_result({"error": str(e)})

def search_files(directory: str = ".", pattern: str = "*", max_depth: int = 5) -> str:
    try:
        base = _safe_path(directory)
        matches = []
        base_depth = base.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(base):
            if root.count(os.sep) - base_depth > max_depth:
                dirs.clear()
                continue
            for filename in fnmatch.filter(files, pattern):
                full_path = os.path.join(root, filename)
                matches.append({
                    "path": full_path,
                    "name": filename,
                    "size_bytes": os.path.getsize(full_path)
                })
                if len(matches) >= 200:
                    break
            if len(matches) >= 200:
                break
        return _json_result({"directory": base, "pattern": pattern, "count": len(matches), "files": matches})
    except Exception as e:
        return _json_result({"error": str(e)})

def search_file_content(directory: str = ".", query: str = "", file_extension: str = "", case_insensitive: bool = False, is_regex: bool = False) -> str:
    try:
        base = _safe_path(directory)
        results = []
        pattern = re.compile(query, re.I if case_insensitive else 0) if (is_regex and query) else None
        query_check = query.lower() if (case_insensitive and not is_regex) else query

        for root, _, files in os.walk(base):
            for file in files:
                if file_extension:
                    ext = file_extension if file_extension.startswith(".") else f".{file_extension}"
                    if not file.endswith(ext):
                        continue
                path = os.path.join(root, file)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        for idx, line in enumerate(f, 1):
                            matched = bool(pattern.search(line)) if pattern else (query_check in line.lower() if case_insensitive else query_check in line)
                            if matched:
                                results.append({"filepath": path, "line_number": idx, "content": line.strip()})
                                if len(results) >= 200:
                                    break
                except Exception:
                    pass
                if len(results) >= 200:
                    break
            if len(results) >= 200:
                break
        return _json_result({"query": query, "match_count": len(results), "matches": results})
    except Exception as e:
        return _json_result({"error": str(e)})

def delete_file(filepath: str) -> str:
    try:
        path = _safe_path(filepath)
        if not os.path.exists(path):
            return _json_result({"error": f"File '{filepath}' does not exist"})
        if os.path.isdir(path):
            return _json_result({"error": f"'{filepath}' is a directory"})
        os.remove(path)
        return _json_result({"status": "success", "deleted": filepath})
    except Exception as e:
        return _json_result({"error": str(e)})

def make_directory(path: str) -> str:
    try:
        target = _safe_path(path)
        os.makedirs(target, exist_ok=True)
        return _json_result({"status": "success", "created": target})
    except Exception as e:
        return _json_result({"error": str(e)})

def copy_file(source: str, destination: str) -> str:
    try:
        src = _safe_path(source)
        dst = _safe_path(destination)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return _json_result({"status": "success", "source": src, "destination": dst})
    except Exception as e:
        return _json_result({"error": str(e)})

def move_file(source: str, destination: str) -> str:
    try:
        src = _safe_path(source)
        dst = _safe_path(destination)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return _json_result({"status": "success", "source": src, "destination": dst})
    except Exception as e:
        return _json_result({"error": str(e)})

def get_environment_variable(name: str = "") -> str:
    try:
        if name:
            return _json_result({name: os.getenv(name)})
        safe_env = {k: v for k, v in os.environ.items() if not any(x in k.lower() for x in ("token", "key", "secret", "auth", "pwd", "password"))}
        return _json_result(safe_env)
    except Exception as e:
        return _json_result({"error": str(e)})

def set_environment_variable(name: str, value: str) -> str:
    try:
        os.environ[name] = str(value)
        return _json_result({"status": "success", name: str(value)})
    except Exception as e:
        return _json_result({"error": str(e)})

def http_request(url: str, method: str = "GET", headers: str = "{}", data: str = "") -> str:
    try:
        _assert_safe_remote(url)
        req_headers = json.loads(headers) if headers else {}
        method = str(method or "GET").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError("Unsupported HTTP method")
        body = data.encode("utf-8") if data else None
        if body and len(body) > MCP_MAX_REQUEST_BYTES:
            raise ValueError("Request body exceeds limit")
        req = urllib.request.Request(_validated_http_url(url), data=body, headers={str(k): str(v) for k, v in req_headers.items()}, method=method)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = _read_limited_response(resp)
            return _json_result({
                "status_code": resp.status,
                "url": resp.geturl(),
                "content_type": resp.headers.get("Content-Type", ""),
                "response": _decode_response(raw, resp.headers.get("Content-Type", ""))[:5000]
            })
    except Exception as e:
        return _json_result({"error": str(e)})

def json_parse_validate(json_string: str) -> str:
    try:
        data = json.loads(json_string)
        return _json_result({"valid": True, "type": type(data).__name__, "parsed": data})
    except Exception as e:
        return _json_result({"valid": False, "error": str(e)})

def regex_search(pattern: str, text: str) -> str:
    try:
        matches = [m.groupdict() or m.group(0) for m in re.finditer(pattern, text)]
        return _json_result({"pattern": pattern, "count": len(matches), "matches": matches})
    except Exception as e:
        return _json_result({"error": str(e)})

def process_list() -> str:
    try:
        res = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        lines = res.stdout.strip().split("\n")
        return _json_result({"count": len(lines) - 1, "output": lines[:50]})
    except Exception as e:
        return _json_result({"error": str(e)})

def git_status(directory: str = ".") -> str:
    try:
        target = _safe_path(directory)
        res = subprocess.run(["git", "status", "-s"], cwd=target, capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return _json_result({"error": res.stderr.strip()})
        return _json_result({"directory": target, "status": res.stdout.strip().split("\n") if res.stdout else []})
    except Exception as e:
        return _json_result({"error": str(e)})

def git_diff(directory: str = ".") -> str:
    try:
        target = _safe_path(directory)
        res = subprocess.run(["git", "diff"], cwd=target, capture_output=True, text=True, timeout=10)
        return _json_result({"directory": target, "diff": res.stdout[:10000]})
    except Exception as e:
        return _json_result({"error": f"git_diff failed: {e}"})

def web_scrape_links(url: str, filter_domain: bool = False) -> str:
    try:
        html = _fetch_url(url)
        details = _extract_html_details(html, url)
        base_host = (urllib.parse.urlsplit(url).hostname or "").lower()
        links = []
        for item in details["links"]:
            host = (urllib.parse.urlsplit(item["url"]).hostname or "").lower()
            if filter_domain and host != base_host:
                continue
            links.append({**item, "text": item["text"] or "[No Text]", "is_external": host != base_host})
            if len(links) >= 300:
                break
        return _json_result({"source_url": url, "count": len(links), "links": links})
    except Exception as e:
        return _json_result({"error": f"web_scrape_links failed: {e}"})

def fetch_json_api(url: str, headers: str = "{}") -> str:
    try:
        _assert_safe_remote(url)
        req_headers = json.loads(headers) if headers else {}
        req_headers.setdefault("User-Agent", "MCP-Tool-Server/3.8")
        req = urllib.request.Request(_validated_http_url(url), headers={str(k): str(v) for k, v in req_headers.items()})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = _decode_response(_read_limited_response(resp), resp.headers.get("Content-Type", ""))
            data = json.loads(raw)
            return _json_result({"url": url, "status": resp.status, "content_type": resp.headers.get("Content-Type", ""), "data": data})
    except json.JSONDecodeError as e:
        return _json_result({"error": f"Invalid JSON response: {e}"})
    except Exception as e:
        return _json_result({"error": f"fetch_json_api failed: {e}"})

def web_download_file(url: str, destination_filepath: str) -> str:
    try:
        _assert_safe_remote(url)
        dst = _safe_path(destination_filepath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        req = urllib.request.Request(_validated_http_url(url), headers={"User-Agent": "MCP-Tool-Server/3.8"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(dst, "wb") as out:
            data = _read_limited_response(resp)
            out.write(data)
        return _json_result({"status": "success", "url": url, "saved_to": dst, "size_bytes": len(data)})
    except Exception as e:
        return _json_result({"error": f"web_download_file failed: {e}"})

def extract_metadata(url: str) -> str:
    try:
        html = _fetch_url(url)
        details = _extract_html_details(html, url)
        return _json_result({"url": url, "title": details["title"], "description": details["description"], "image": details["image"], "canonical_url": details["canonical_url"], "meta_tags": details["meta_tags"]})
    except Exception as e:
        return _json_result({"error": f"extract_metadata failed: {e}"})

def dns_lookup(domain: str) -> str:
    try:
        host = str(domain).strip()
        if "://" in host:
            host = urllib.parse.urlsplit(host).hostname or ""
        host = host.split("/", 1)[0].strip("[]")
        if not host or len(host) > 253:
            raise ValueError("Invalid hostname")
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addresses = sorted({i[4][0] for i in infos})
        return _json_result({"domain": host, "ip_addresses": addresses, "count": len(addresses)})
    except Exception as e:
        return _json_result({"error": f"dns_lookup failed: {e}"})

def check_url_status(url: str) -> str:
    try:
        _assert_safe_remote(url)
        normalized = _validated_http_url(url)
        start = time.time()
        req = urllib.request.Request(normalized, headers={"User-Agent": "MCP-Tool-Server/3.8"}, method="HEAD")
        try:
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code not in (405, 501):
                raise
            req = urllib.request.Request(normalized, headers={"User-Agent": "MCP-Tool-Server/3.8"}, method="GET")
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        with resp:
            final_url = _validated_http_url(resp.geturl())
            _assert_safe_remote(final_url)
            return _json_result({
                "url": url,
                "status_code": resp.status,
                "response_time_ms": round((time.time() - start) * 1000, 2),
                "final_url": final_url,
                "content_type": resp.headers.get("Content-Type")
            })
    except Exception as e:
        return _json_result({"error": f"check_url_status failed: {e}"})

def sitemap_parse(sitemap_url: str) -> str:
    try:
        xml_content = _fetch_url(sitemap_url)
        root = ET.fromstring(xml_content)
        urls = []
        for node in root:
            name = node.tag.split("}")[-1].lower()
            if name not in {"url", "sitemap"}:
                continue
            values = {child.tag.split("}")[-1].lower(): (child.text or "").strip() for child in node}
            if values.get("loc"):
                urls.append({"url": values["loc"], "lastmod": values.get("lastmod")})
        return _json_result({"sitemap_url": sitemap_url, "count": len(urls), "urls": urls[:200]})
    except Exception as e:
        return _json_result({"error": f"sitemap_parse failed: {e}"})

def rss_feed_parse(feed_url: str) -> str:
    try:
        xml_content = _fetch_url(feed_url)
        root = ET.fromstring(xml_content)
        items = []
        for node in root.iter():
            name = node.tag.split("}")[-1].lower()
            if name not in {"item", "entry"}:
                continue
            values = {}
            for child in node:
                key = child.tag.split("}")[-1].lower()
                value = child.attrib.get("href") if key == "link" else " ".join("".join(child.itertext()).split())
                if key in {"title", "description", "summary", "content", "pubdate", "published", "updated", "link", "id"}:
                    values.setdefault(key, value)
            items.append({
                "title": values.get("title", ""),
                "link": values.get("link", ""),
                "pub_date": values.get("pubdate") or values.get("published") or values.get("updated"),
                "description": values.get("description") or values.get("summary") or values.get("content", "")
            })
            if len(items) >= MCP_MAX_FEED_ITEMS:
                break
        return _json_result({"feed_url": feed_url, "count": len(items), "items": items})
    except Exception as e:
        return _json_result({"error": f"rss_feed_parse failed: {e}"})

def base64_encode_decode(text: str, mode: str = "encode") -> str:
    try:
        if mode.lower() == "encode":
            encoded = base64.b64encode(text.encode("utf-8")).decode("utf-8")
            return _json_result({"mode": "encode", "result": encoded})
        else:
            decoded = base64.b64decode(text.encode("utf-8")).decode("utf-8")
            return _json_result({"mode": "decode", "result": decoded})
    except Exception as e:
        return _json_result({"error": f"base64_encode_decode failed: {e}"})

def parse_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        return _json_result({
            "url": url,
            "scheme": parsed.scheme,
            "netloc": parsed.netloc,
            "hostname": parsed.hostname,
            "port": parsed.port,
            "path": parsed.path,
            "query_dict": urllib.parse.parse_qs(parsed.query),
            "fragment": parsed.fragment
        })
    except Exception as e:
        return _json_result({"error": f"parse_url failed: {e}"})

def compress_decompress_archive(archive_path: str, action: str = "extract", target_directory: str = ".") -> str:
    try:
        arc = _safe_path(archive_path)
        tgt = _safe_path(target_directory)
        if action == "extract":
            shutil.unpack_archive(arc, tgt)
            return _json_result({"status": "success", "action": "extracted", "archive": arc, "target": tgt})
        else:
            format_type = "zip" if arc.endswith(".zip") else "gztar"
            base_name = arc.rsplit(".", 1)[0]
            out = shutil.make_archive(base_name, format_type, tgt)
            return _json_result({"status": "success", "action": "created", "archive": out})
    except Exception as e:
        return _json_result({"error": f"compress_decompress_archive failed: {e}"})

def text_diff_compare(text1: str, text2: str) -> str:
    try:
        lines1 = text1.splitlines(keepends=True)
        lines2 = text2.splitlines(keepends=True)
        diff = list(difflib.unified_diff(lines1, lines2, fromfile="text1", tofile="text2"))
        return _json_result({"diff_lines_count": len(diff), "diff": "".join(diff)})
    except Exception as e:
        return _json_result({"error": f"text_diff_compare failed: {e}"})

def read_url_hardened(url: str, max_chars: int = 64000, max_redirects: int = 5) -> str:
    try:
        max_chars = _bounded_int(max_chars, 64000, 256, MAX_TEXT_CHARS)
        max_redirects = _bounded_int(max_redirects, 5, 0, 10)
        current = _validated_http_url(url)
        opener = urllib.request.build_opener()
        for hop in range(max_redirects + 1):
            _assert_safe_remote(current)
            req = urllib.request.Request(current, headers={"User-Agent": "MCP-Tool-Server/3.8"}, method="GET")
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                final_url = _validated_http_url(resp.geturl())
                _assert_safe_remote(final_url)
                data = _read_limited_response(resp)
                content_type = resp.headers.get("Content-Type", "")
                raw = _decode_response(data, content_type)
                if final_url != current and hop < max_redirects:
                    current = final_url
                if "html" in content_type.lower() or re.search(r"<html\b|<body\b", raw, re.I):
                    details = _extract_html_details(raw, final_url)
                    text = details["text"]
                    return _json_result({
                        "url": url,
                        "final_url": final_url,
                        "title": details["title"],
                        "length": min(len(text), max_chars),
                        "text": text[:max_chars]
                    })
                return _json_result({
                    "url": url,
                    "final_url": final_url,
                    "length": min(len(raw), max_chars),
                    "text": raw[:max_chars]
                })
        return _json_result({"error": "Too many redirects"})
    except Exception as e:
        return _json_result({"error": f"read_url_hardened failed: {e}"})

def file_tree(path: str = ".", max_depth: int = 4, ignore: str = ".git,__pycache__,node_modules,.venv") -> str:
    try:
        base = _safe_path(path)
        ignore_set = {p.strip() for p in ignore.split(",") if p.strip()}
        lines = []

        def walk(current: str, prefix: str = "", depth: int = 0):
            if depth > max_depth:
                return
            with contextlib.suppress(PermissionError):
                entries = sorted(os.listdir(current))
                dirs = [e for e in entries if os.path.isdir(os.path.join(current, e)) and e not in ignore_set]
                files = [e for e in entries if os.path.isfile(os.path.join(current, e)) and e not in ignore_set]

                for i, d in enumerate(dirs):
                    is_last = i == len(dirs) - 1
                    lines.append(f"{prefix}├── {d}/")
                    walk(os.path.join(current, d), prefix + ("    " if is_last else "│   "), depth + 1)
                for f in files:
                    lines.append(f"{prefix}├── {f}")

        walk(base)
        return _json_result({"tree": "\n".join(lines)})
    except Exception as e:
        return _json_result({"error": str(e)})

def safe_execute_python(code: str, timeout_seconds: int = 15) -> str:
    if not ENABLE_DANGEROUS:
        return _json_result({"error": "Dangerous tool execution disabled. Set MCP_ENABLE_DANGEROUS=true."})
    start = time.time()
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                return _json_result({"error": f"Security violation: access to dunder '{node.attr}' is disallowed."})
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if any(alias.name.startswith(x) for x in ("os", "subprocess", "sys", "socket", "urllib")):
                        return _json_result({"error": f"Import of '{alias.name}' is prohibited in safe mode"})

        safe_builtins = {
            "print", "len", "range", "str", "int", "float", "list", "dict", "set", "tuple",
            "bool", "abs", "min", "max", "sum", "sorted", "enumerate", "zip", "map",
            "filter", "round", "isinstance", "type", "Exception", "ValueError", "TypeError"
        }
        wrapper = (
            "allowed = " + repr(safe_builtins) + "\n"
            "b = {k: getattr(__builtins__, k) for k in allowed if hasattr(__builtins__, k)}\n"
            "__builtins__.clear()\n"
            "__builtins__.update(b)\n"
            + code
        )

        with temporary_file(".py", delete=False) as tf:
            tf.write(wrapper)
            tf.flush()
            tmp = tf.name

        clean_env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.getenv("PATH", "")}
        result = subprocess.run(
            [sys.executable or "python3", tmp],
            capture_output=True, text=True, timeout=_bounded_int(timeout_seconds, 15, 1, 60),
            env=clean_env
        )
        _record_tool("safe_python_exec", True, time.time() - start)
        return _json_result({
            "stdout": result.stdout[-8000:],
            "stderr": result.stderr[-2000:],
            "exit_code": result.returncode,
            "execution_time_sec": round(time.time() - start, 3),
            "restricted": True
        })
    except subprocess.TimeoutExpired:
        return _json_result({"error": f"Timed out after {timeout_seconds}s"})
    except Exception as e:
        return _json_result({"error": str(e)})
    finally:
        with contextlib.suppress(Exception):
            if 'tmp' in locals() and os.path.exists(tmp):
                os.unlink(tmp)

def apply_patch(filepath: str, patch: str) -> str:
    try:
        path = _safe_path(filepath)
        if not os.path.exists(path):
            return _json_result({"error": f"File not found: {filepath}"})
        if not patch.strip().startswith("---") and not patch.strip().startswith("diff"):
            with open(path, "w", encoding="utf-8") as f:
                f.write(patch)
            return _json_result({"status": "success", "mode": "full_replace", "filepath": filepath})

        res = subprocess.run(["patch", "-p0", "--forward", path], input=patch, text=True, capture_output=True, timeout=10)
        if res.returncode == 0:
            return _json_result({"status": "success", "filepath": filepath})
        return _json_result({"status": "failed", "filepath": filepath, "stderr": res.stderr or res.stdout})
    except Exception as e:
        return _json_result({"error": f"apply_patch failed: {e}"})

def parse_html_document(html: str, base_url: str = "", max_chars: int = 64000) -> str:
    try:
        max_chars = _bounded_int(max_chars, 64000, 256, MAX_TEXT_CHARS)
        details = _extract_html_details(html, base_url)
        details["text"] = details["text"][:max_chars]
        return _json_result(details)
    except Exception as e:
        return _json_result({"error": f"parse_html_document failed: {e}"})

def extract_structured_data(html: str, base_url: str = "") -> str:
    try:
        details = _extract_html_details(html, base_url)
        json_ld = []
        for match in re.finditer(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            raw = html_module.unescape(match.group(1)).strip()
            with contextlib.suppress(json.JSONDecodeError):
                json_ld.append(json.loads(raw))
        return _json_result({
            "title": details["title"],
            "canonical_url": details["canonical_url"],
            "description": details["description"],
            "json_ld": json_ld
        })
    except Exception as e:
        return _json_result({"error": f"extract_structured_data failed: {e}"})

def parse_url_headers(url: str) -> str:
    try:
        _assert_safe_remote(url)
        req = urllib.request.Request(_validated_http_url(url), headers={"User-Agent": "MCP-Tool-Server/3.8"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return _json_result({"url": url, "status_code": resp.status, "headers": headers})
    except Exception as e:
        return _json_result({"error": f"parse_url_headers failed: {e}"})

def parse_html_tables(html: str, max_tables: int = 20, max_rows: int = 200) -> str:
    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tables, self.table, self.row, self.cell = [], None, None, None
            self.in_cell = False
        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag == "table":
                self.table = []
            elif self.table is not None and tag == "tr":
                self.row = []
            elif self.table is not None and tag in ("td", "th") and self.row is not None:
                self.cell = []
                self.in_cell = True
        def handle_data(self, data):
            if self.in_cell and self.cell is not None:
                val = re.sub(r"\s+", " ", data).strip()
                if val:
                    self.cell.append(val)
        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in ("td", "th") and self.in_cell and self.row is not None:
                self.row.append(" ".join(self.cell or []))
                self.cell, self.in_cell = None, False
            elif tag == "tr" and self.table is not None and self.row is not None:
                if self.row:
                    self.table.append(self.row)
                self.row = None
            elif tag == "table" and self.table is not None:
                if self.table:
                    self.tables.append(self.table)
                self.table = None

    try:
        parser = TableParser()
        parser.feed(str(html or "")[:MAX_HTTP_BYTES])
        tables = parser.tables[:_bounded_int(max_tables, 20, 1, 100)]
        out = []
        for table in tables:
            rows = table[:_bounded_int(max_rows, 200, 1, 1000)]
            headers = rows[0] if rows else []
            data_rows = rows[1:] if headers else rows
            out.append({"headers": headers, "rows": data_rows, "row_count": len(data_rows)})
        return _json_result({"table_count": len(out), "tables": out})
    except Exception as e:
        return _json_result({"error": f"parse_html_tables failed: {e}"})

def extract_media_links(html: str, base_url: str = "", max_results: int = 500) -> str:
    class MediaParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.items = []
        def handle_starttag(self, tag, attrs):
            attrs_dict = dict(attrs)
            tag = tag.lower()
            for key in ("src", "data-src", "poster"):
                if attrs_dict.get(key):
                    self.items.append({"type": tag, "attribute": key, "url": attrs_dict[key]})

    try:
        parser = MediaParser()
        parser.feed(str(html or "")[:MAX_HTTP_BYTES])
        out, seen = [], set()
        for item in parser.items:
            absolute = urllib.parse.urljoin(base_url, item["url"])
            if urllib.parse.urlsplit(absolute).scheme not in ("http", "https"):
                continue
            key = (item["type"], absolute)
            if key in seen:
                continue
            seen.add(key)
            out.append({**item, "url": absolute})
            if len(out) >= _bounded_int(max_results, 500, 1, MCP_MAX_LINKS):
                break
        return _json_result({"count": len(out), "media": out})
    except Exception as e:
        return _json_result({"error": f"extract_media_links failed: {e}"})

def parse_robots_txt(text: str, user_agent: str = "*") -> str:
    try:
        groups, current = [], None
        for raw in str(text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = [x.strip() for x in line.split(":", 1)]
            low = key.lower()
            if low == "user-agent":
                if current is None or current.get("directives"):
                    current = {"agents": [], "directives": []}
                    groups.append(current)
                current["agents"].append(value)
            elif current is not None:
                current["directives"].append({"directive": low, "value": value})
        selected = []
        ua = user_agent.lower()
        for group in groups:
            if any(a == "*" or a.lower() in ua for a in group["agents"]):
                selected.extend(group["directives"])
        return _json_result({"user_agent": user_agent, "directives": selected})
    except Exception as e:
        return _json_result({"error": f"parse_robots_txt failed: {e}"})

def discover_feed_links(html: str, base_url: str = "") -> str:
    class FeedParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.items = []
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "link":
                return
            a = {str(k).lower(): (v or "") for k, v in attrs}
            rel = a.get("rel", "").lower().split()
            typ = a.get("type", "").lower()
            if "alternate" in rel and any(x in typ for x in ("rss", "atom", "json", "feed")):
                if a.get("href"):
                    self.items.append({"type": typ, "title": a.get("title", ""), "url": a["href"]})

    try:
        parser = FeedParser()
        parser.feed(str(html or "")[:MAX_HTTP_BYTES])
        seen, feeds = set(), []
        for item in parser.items:
            u = urllib.parse.urljoin(base_url, item["url"])
            if u in seen:
                continue
            seen.add(u)
            feeds.append({**item, "url": u})
        return _json_result({"count": len(feeds), "feeds": feeds[:100]})
    except Exception as e:
        return _json_result({"error": f"discover_feed_links failed: {e}"})

# ---------------------------------------------------------------------------
# Tool Registry & Schemas
# ---------------------------------------------------------------------------
TOOL_MAP: Dict[str, Callable] = {
    "web_search": web_search,
    "fetch_webpage_text": fetch_webpage_text,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file_replace": edit_file_replace,
    "append_to_file": append_to_file,
    "list_directory": list_directory,
    "lint_code": lint_code,
    "execute_python_code": execute_python_code,
    "execute_javascript_code": execute_javascript_code,
    "execute_bash_command": execute_bash_command,
    "ping": ping,
    "system_info": system_info,
    "file_checksum": file_checksum,
    "search_files": search_files,
    "search_file_content": search_file_content,
    "delete_file": delete_file,
    "make_directory": make_directory,
    "copy_file": copy_file,
    "move_file": move_file,
    "get_environment_variable": get_environment_variable,
    "set_environment_variable": set_environment_variable,
    "http_request": http_request,
    "json_parse_validate": json_parse_validate,
    "regex_search": regex_search,
    "process_list": process_list,
    "git_status": git_status,
    "web_scrape_links": web_scrape_links,
    "fetch_json_api": fetch_json_api,
    "web_download_file": web_download_file,
    "extract_metadata": extract_metadata,
    "dns_lookup": dns_lookup,
    "check_url_status": check_url_status,
    "sitemap_parse": sitemap_parse,
    "rss_feed_parse": rss_feed_parse,
    "read_url_hardened": read_url_hardened,
    "memory_store": memory_store,
    "memory_recall": memory_recall,
    "memory_list": memory_list,
    "safe_execute_python": safe_execute_python,
    "file_tree": file_tree,
    "apply_patch": apply_patch,
    "git_diff": git_diff,
    "base64_encode_decode": base64_encode_decode,
    "parse_url": parse_url,
    "compress_decompress_archive": compress_decompress_archive,
    "text_diff_compare": text_diff_compare,
    "parse_html_document": parse_html_document,
    "extract_structured_data": extract_structured_data,
    "parse_url_headers": parse_url_headers,
    "parse_html_tables": parse_html_tables,
    "extract_media_links": extract_media_links,
    "parse_robots_txt": parse_robots_txt,
    "discover_feed_links": discover_feed_links,
}

TOOL_SCHEMAS = {
    "web_search": {"query": "str", "max_results": "int=5", "page": "int=1", "domain": "str=", "safe_search": "bool=false"},
    "fetch_webpage_text": {"url": "str", "max_chars": "int=8000"},
    "read_file": {"filepath": "str", "start_line": "int=1", "line_count": "int=500"},
    "write_file": {"filepath": "str", "content": "str"},
    "edit_file_replace": {"filepath": "str", "target_snippet": "str", "replacement_snippet": "str", "replace_all": "bool=false"},
    "append_to_file": {"filepath": "str", "content": "str"},
    "list_directory": {"path": "str=."},
    "lint_code": {"language": "str", "code": "str=", "filepath": "str="},
    "execute_python_code": {"code": "str", "timeout_seconds": "int=30"},
    "execute_javascript_code": {"code": "str", "timeout_seconds": "int=30"},
    "execute_bash_command": {"command": "str", "timeout_seconds": "int=60"},
    "ping": {},
    "system_info": {},
    "file_checksum": {"filepath": "str", "algorithm": "str=sha256"},
    "search_files": {"directory": "str=.", "pattern": "str=*", "max_depth": "int=5"},
    "search_file_content": {"directory": "str=.", "query": "str", "file_extension": "str=", "case_insensitive": "bool=false", "is_regex": "bool=false"},
    "delete_file": {"filepath": "str"},
    "make_directory": {"path": "str"},
    "copy_file": {"source": "str", "destination": "str"},
    "move_file": {"source": "str", "destination": "str"},
    "get_environment_variable": {"name": "str="},
    "set_environment_variable": {"name": "str", "value": "str"},
    "http_request": {"url": "str", "method": "str=GET", "headers": "str={}", "data": "str="},
    "json_parse_validate": {"json_string": "str"},
    "regex_search": {"pattern": "str", "text": "str"},
    "process_list": {},
    "git_status": {"directory": "str=."},
    "web_scrape_links": {"url": "str", "filter_domain": "bool=false"},
    "fetch_json_api": {"url": "str", "headers": "str={}"},
    "web_download_file": {"url": "str", "destination_filepath": "str"},
    "extract_metadata": {"url": "str"},
    "dns_lookup": {"domain": "str"},
    "check_url_status": {"url": "str"},
    "sitemap_parse": {"sitemap_url": "str"},
    "rss_feed_parse": {"feed_url": "str"},
    "git_diff": {"directory": "str=."},
    "base64_encode_decode": {"text": "str", "mode": "str=encode"},
    "parse_url": {"url": "str"},
    "compress_decompress_archive": {"archive_path": "str", "action": "str=extract", "target_directory": "str=."},
    "text_diff_compare": {"text1": "str", "text2": "str"},
    "read_url_hardened": {"url": "str", "max_chars": "int=64000", "max_redirects": "int=5"},
    "memory_store": {"key": "str", "value": "str", "tags": "str="},
    "memory_recall": {"key": "str=", "tag": "str="},
    "memory_list": {},
    "safe_execute_python": {"code": "str", "timeout_seconds": "int=15"},
    "file_tree": {"path": "str=.", "max_depth": "int=4", "ignore": "str=.git,__pycache__,node_modules,.venv"},
    "apply_patch": {"filepath": "str", "patch": "str"},
    "parse_html_document": {"html": "str", "base_url": "str=", "max_chars": "int=64000"},
    "extract_structured_data": {"html": "str", "base_url": "str="},
    "parse_url_headers": {"url": "str"},
    "parse_html_tables": {"html": "str", "max_tables": "int=20", "max_rows": "int=200"},
    "extract_media_links": {"html": "str", "base_url": "str=", "max_results": "int=500"},
    "parse_robots_txt": {"text": "str", "user_agent": "str=*"},
    "discover_feed_links": {"html": "str", "base_url": "str="},
}

def _build_mcp_input_schema(tschema: dict) -> dict:
    properties = {}
    required = []
    for k, v in tschema.items():
        v_str = str(v)
        is_optional = "=" in v_str or v_str.endswith("=")
        param_type = v_str.split("=")[0].strip() if "=" in v_str else v_str.rstrip("=")
        
        json_type = "string"
        if param_type in ("int", "number"):
            json_type = "integer" if param_type == "int" else "number"
        elif param_type == "bool":
            json_type = "boolean"

        prop_def = {"type": json_type}
        if "=" in v_str:
            default_val = v_str.split("=", 1)[1]
            if json_type == "integer" and default_val.isdigit():
                prop_def["default"] = int(default_val)
            elif json_type == "boolean":
                prop_def["default"] = default_val.lower() == "true"
            elif default_val:
                prop_def["default"] = default_val

        properties[k] = prop_def
        if not is_optional:
            required.append(k)

    return {"type": "object", "properties": properties, "required": required}

# ---------------------------------------------------------------------------
# HTTP & JSON-RPC 2.0 Server Handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    MAX_BODY_BYTES = MCP_MAX_REQUEST_BYTES

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, code: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors()
        self.end_headers()
        with contextlib.suppress(Exception):
            self.wfile.write(body)

    def _check_auth(self) -> bool:
        if not REQUIRE_AUTH:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token == API_TOKEN:
                return True
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "token" in query and query["token"][0] == API_TOKEN:
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health", "/tools", "/mcp", "/mcp/v1/tools"):
            self._json(200, {
                "status": "online",
                "server": "mcp-enhanced-wizard",
                "protocolVersion": "2024-11-05",
                "total_tools": len(TOOL_SCHEMAS),
                "tools": TOOL_SCHEMAS
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        with _server_stats_lock:
            _SERVER_STATS["total_requests"] += 1

        if not self._check_auth():
            with _server_stats_lock:
                _SERVER_STATS["failed_calls"] += 1
            self._json(401, {"error": "unauthorized – set Authorization: Bearer <token>"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._json(400, {"error": "invalid Content-Length"})
            return

        if length < 0 or length > self.MAX_BODY_BYTES:
            self._json(413, {"error": f"request body exceeds limit of {self.MAX_BODY_BYTES} bytes"})
            return

        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._json(400, {"error": f"invalid JSON: {e}"})
            return

        path_parts = [p for p in self.path.split("?")[0].split("/") if p]
        func_name = None
        args = {}

        if len(path_parts) >= 1 and path_parts[-1] in TOOL_MAP:
            func_name = path_parts[-1]
            args = payload if isinstance(payload, dict) else {}

        if isinstance(payload, dict):
            if payload.get("jsonrpc") == "2.0":
                params = payload.get("params", {})
                if isinstance(params, dict):
                    func_name = func_name or params.get("name") or params.get("tool") or payload.get("method")
                    args = params.get("arguments") or params.get("args") or params.get("input") or args

            if not func_name:
                tool_info = payload.get("tool") or payload.get("function") or payload.get("call")
                if isinstance(tool_info, dict):
                    func_name = tool_info.get("func") or tool_info.get("name")
                    args = tool_info.get("args") or tool_info.get("arguments") or {}
                elif isinstance(tool_info, str):
                    func_name = tool_info
                    args = payload.get("args") or payload.get("arguments") or {}

            if not func_name:
                func_name = payload.get("func") or payload.get("name") or payload.get("tool") or payload.get("action")
                args = args or payload.get("args") or payload.get("arguments") or {}

            if isinstance(args, str):
                with contextlib.suppress(Exception):
                    args = json.loads(args)

        # JSON-RPC Protocol Intercepts
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            req_id = payload.get("id")
            method = payload.get("method")
            if method == "initialize":
                self._json(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mcp-enhanced-wizard", "version": "3.8.0"}
                    }
                })
                return
            elif method in ("notifications/initialized", "ping"):
                if req_id is not None:
                    self._json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
                else:
                    self._json(200, {"jsonrpc": "2.0", "result": {}})
                return
            elif method in ("tools/list", "list_tools"):
                mcp_tools = [
                    {
                        "name": tname,
                        "description": (TOOL_MAP[tname].__doc__.strip().split("\n")[0] if TOOL_MAP[tname].__doc__ else f"Tool {tname}"),
                        "inputSchema": _build_mcp_input_schema(tschema)
                    }
                    for tname, tschema in TOOL_SCHEMAS.items()
                ]
                self._json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": mcp_tools}})
                return

        if not func_name or not isinstance(func_name, str):
            with _server_stats_lock:
                _SERVER_STATS["failed_calls"] += 1
            if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
                self._json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "Missing tool name"}})
            else:
                self._json(400, {"error": "missing tool name"})
            return

        if not isinstance(args, dict):
            args = {}

        func = TOOL_MAP.get(func_name)
        if func is None:
            with _server_stats_lock:
                _SERVER_STATS["failed_calls"] += 1
            if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
                self._json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": f"Tool '{func_name}' not found"}})
            else:
                self._json(404, {"error": f"unknown tool '{func_name}'", "available": list(TOOL_MAP.keys())})
            return

        log.info("%sCalling tool: %s%s", GREEN, func_name, RESET)
        try:
            start_time = time.time()
            result = func(**args)
            duration = time.time() - start_time
            _record_tool(func_name, True, duration)

            with _server_stats_lock:
                _SERVER_STATS["successful_calls"] += 1

            if isinstance(result, str):
                with contextlib.suppress(Exception):
                    result = json.loads(result)

            if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
                content_text = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
                self._json(200, {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [{"type": "text", "text": content_text}], "isError": False}
                })
            else:
                self._json(200, result)
        except Exception as e:
            log.exception("Tool execution failure")
            with _server_stats_lock:
                _SERVER_STATS["failed_calls"] += 1
            _record_tool(func_name, False, 0.0)

            if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
                self._json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32603, "message": str(e)}})
            else:
                self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

# ---------------------------------------------------------------------------
# Server Launcher Entrypoint
# ---------------------------------------------------------------------------
def run_server():
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log.info("=" * 60)
    log.info("%sMCP-enhanced tool server (Production Hardened v3.8)%s", GREEN, RESET)
    log.info("Listening securely on http://0.0.0.0:%s", HTTP_PORT)
    log.info("Authentication Required: %s", REQUIRE_AUTH)
    log.info("Sandbox Root Directory: %s", SANDBOX_ROOT)
    log.info("Dangerous Execution Enabled: %s", ENABLE_DANGEROUS)
    log.info("Registered Tool Count: %d available tools", len(TOOL_MAP))
    log.info("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down tool server gracefully...")
        server.shutdown()
        server.server_close()

if __name__ == "__main__":
    run_server()
