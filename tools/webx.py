#!/usr/bin/env python3
# @describe Web Search Tool with Content Crawling (Alternative)
"""
google_search.py - Web Search Tool with Content Crawling

A robust tool for performing web searches and optionally crawling content
from search result URLs. Uses curlie/wget2/curl/wget for HTTP fetching.
Includes multiple search backends, retry logic, bot-detection handling,
response validation, and graceful error propagation (no simulated results).
"""

import json
import sys
import re
import argparse
import logging
import time
import random
import shutil
import subprocess
import urllib.parse
import base64
from typing import List, Dict, Any, Optional, Literal, Tuple
from html.parser import HTMLParser
from dataclasses import dataclass, asdict

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


# =========================================================================
# Constants
# =========================================================================

_USER_AGENT_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
]

_BOT_DETECTION_PATTERNS = [
    "captcha",
    "verify you are human",
    "unusual traffic",
    "automated requests",
    "robot",
    "blocked",
    "access denied",
    "rate limit",
    "too many requests",
    "please complete the security check",
    "enable javascript",
    "challenge-platform",
]

_BLOCKED_TITLE_PATTERNS = [
    "<title>access denied</title>",
    "<title>403 forbidden</title>",
    "<title>sorry</title>",
    "<title>blocked</title>",
    "<title>just a moment</title>",
    "<title>attention required</title>",
]


# =========================================================================
# Tool detection
# =========================================================================

CURLIE_BIN = shutil.which("curlie")
WGET2_BIN = shutil.which("wget2")
CURL_BIN = shutil.which("curl")
WGET_BIN = shutil.which("wget")


def _preferred_fetcher() -> str:
    """Return the name of the best available HTTP CLI tool."""
    if CURLIE_BIN:
        return "curlie"
    if WGET2_BIN:
        return "wget2"
    if CURL_BIN:
        return "curl"
    if WGET_BIN:
        return "wget"
    raise EnvironmentError(
        "No supported HTTP tool found. Install one of: curlie, wget2, curl, wget"
    )


def _get_tool_version(tool_name: str) -> Optional[str]:
    """Get the version string of a CLI tool for diagnostics."""
    binary_map = {
        "curlie": CURLIE_BIN,
        "wget2": WGET2_BIN,
        "curl": CURL_BIN,
        "wget": WGET_BIN,
    }
    binary = binary_map.get(tool_name)
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        first_line = result.stdout.strip().split("\n")
        return first_line[0] if first_line else None
    except Exception:
        return None


# =========================================================================
# Data structures
# =========================================================================


@dataclass
class SearchResult:
    """Represents a single search result with optional crawled content."""

    position: int
    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""
    error: str = ""


@dataclass
class SearchMetadata:
    """Metadata about the search operation itself."""

    query: str = ""
    engine_used: str = ""
    fetcher_tool: str = ""
    fetcher_version: str = ""
    total_results: int = 0
    search_time_ms: int = 0
    had_bot_detection: bool = False
    retries_used: int = 0


# =========================================================================
# HTML text extractor (stdlib-only fallback)
# =========================================================================


class ContentExtractor(HTMLParser):
    """HTML parser to extract clean text content from web pages."""

    def __init__(self, max_length: int = 3000):
        super().__init__()
        self.max_length = max_length
        self.content: List[str] = []
        self._skip = False
        self.current_length = 0
        self._title_parts: List[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "path"):
            self._skip = True
        elif tag == "title":
            self._in_title = True
        elif tag in (
            "p",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "span",
            "td",
            "th",
            "blockquote",
            "pre",
            "article",
            "section",
            "main",
            "header",
            "footer",
        ):
            if self.content and self.content[-1] not in (" ", "\n"):
                self.content.append(" ")
        elif tag == "br":
            self.content.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = False
        elif tag == "title":
            self._in_title = False
        elif tag in (
            "p",
            "div",
            "li",
            "tr",
            "blockquote",
            "article",
            "section",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        ):
            if self.content and self.content[-1] != "\n":
                self.content.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data.strip())
        if not self._skip and data.strip():
            words = data.split()
            for word in words:
                if self.current_length >= self.max_length:
                    return
                self.content.append(word)
                self.current_length += len(word) + 1

    def get_text(self) -> str:
        """Return cleaned text content."""
        text = " ".join(self.content)
        return " ".join(text.split())

    def get_title(self) -> str:
        """Return the page title if found."""
        return " ".join(self._title_parts).strip()


# =========================================================================
# Response validation
# =========================================================================


def _detect_bot_challenge(html: str) -> bool:
    """Check if the HTML response is a bot/CAPTCHA challenge page."""
    html_lower = html.lower()
    for pattern in _BOT_DETECTION_PATTERNS:
        if pattern in html_lower:
            return True
    for title_pattern in _BLOCKED_TITLE_PATTERNS:
        if title_pattern in html_lower:
            return True
    return False


def _validate_response(html: Optional[str], url: str) -> Tuple[bool, str]:
    """
    Validate an HTTP response body.

    Returns:
        (is_valid, reason_if_invalid)
    """
    if html is None:
        return False, "Response is None"
    stripped = html.strip()
    if not stripped:
        return False, "Empty response body"
    if len(stripped) < 100:
        return False, f"Suspiciously short response ({len(stripped)} bytes)"
    if _detect_bot_challenge(stripped):
        return False, "Bot detection / CAPTCHA page detected"
    return True, ""


# =========================================================================
# Low-level HTTP fetch via CLI tools
# =========================================================================


def _random_user_agent() -> str:
    """Pick a random user-agent string to reduce fingerprinting."""
    return random.choice(_USER_AGENT_POOL)


def _build_fetch_command(
    url: str,
    fetcher: str,
    timeout: int,
    user_agent: str,
    extra_headers: Optional[Dict[str, str]] = None,
    http11_only: bool = True,
    method: str = "GET",
    data: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Build the CLI command list for the given fetcher, supporting POST payloads.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra_headers:
        headers.update(extra_headers)

    if fetcher in ("curlie", "curl"):
        binary = CURLIE_BIN if fetcher == "curlie" else CURL_BIN
        cmd = [
            binary,
            "-s",
            "-S",
            "-L",
            "--compressed",
            "--max-time",
            str(timeout),
            "--max-redirs",
            "10",
        ]
        if http11_only:
            cmd.append("--http1.1")
        for key, val in headers.items():
            cmd.extend(["-H", f"{key}: {val}"])

        if method == "POST" and data:
            cmd.extend(["-X", "POST"])
            payload = urllib.parse.urlencode(data)
            cmd.extend(["-d", payload])

        cmd.append(url)

    elif fetcher == "wget2":
        cmd = [
            WGET2_BIN,
            "-q",
            "-O",
            "-",
            f"--timeout={timeout}",
            "--max-redirect=10",
            "--compression=auto",
        ]
        # REMOVED http11_only logic because wget2 does not use --http1.1
        for key, val in headers.items():
            cmd.append(f"--header={key}: {val}")

        if method == "POST" and data:
            payload = urllib.parse.urlencode(data)
            cmd.append(f"--post-data={payload}")

        cmd.append(url)

    else:  # wget legacy
        cmd = [
            WGET_BIN,
            "-q",
            "-O",
            "-",
            f"--timeout={timeout}",
            "--max-redirect=10",
        ]
        for key, val in headers.items():
            cmd.append(f"--header={key}: {val}")

        if method == "POST" and data:
            payload = urllib.parse.urlencode(data)
            cmd.append(f"--post-data={payload}")

        cmd.append(url)

    return cmd


def _fetch_url(
    url: str,
    timeout: int = 10,
    verbose: bool = False,
    user_agent: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    http11_only: bool = True,
    method: str = "GET",
    data: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Fetch a URL using the best available CLI tool."""
    logger = logging.getLogger(__name__)
    fetcher = _preferred_fetcher()
    ua = user_agent or _random_user_agent()

    cmd = _build_fetch_command(
        url=url,
        fetcher=fetcher,
        timeout=timeout,
        user_agent=ua,
        extra_headers=extra_headers,
        http11_only=http11_only,
        method=method,
        data=data,
    )

    if verbose:
        logger.debug("Fetch [%s] command: %s", fetcher, " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Subprocess timeout for %s", url)
        return None
    except FileNotFoundError:
        logger.error("Binary not found for fetcher '%s'", fetcher)
        return None

    if proc.returncode != 0:
        stderr_short = proc.stderr.strip()[:200]
        logger.warning(
            "%s failed (rc=%d) for %s: %s", fetcher, proc.returncode, url, stderr_short
        )
        return None

    is_valid, reason = _validate_response(proc.stdout, url)
    if not is_valid:
        logger.warning("Invalid response from %s: %s", url, reason)
        return None

    return proc.stdout


def _fetch_url_with_retry(
    url: str,
    timeout: int = 10,
    max_retries: int = 2,
    verbose: bool = False,
    user_agent: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    http11_only: bool = True,
    method: str = "GET",
    data: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], int]:
    """Fetch a URL with exponential backoff retry logic."""
    logger = logging.getLogger(__name__)
    retries_used = 0

    for attempt in range(max_retries + 1):
        ua = user_agent or _random_user_agent()

        result = _fetch_url(
            url,
            timeout=timeout,
            verbose=verbose,
            user_agent=ua,
            extra_headers=extra_headers,
            http11_only=http11_only,
            method=method,
            data=data,
        )

        if result is not None:
            return result, retries_used

        retries_used += 1

        if attempt < max_retries:
            delay = (2**attempt) + random.uniform(0.5, 1.5)
            logger.debug(
                "Retry %d/%d for %s in %.1fs", attempt + 1, max_retries, url, delay
            )
            time.sleep(delay)

    return None, retries_used


# =========================================================================
# Content extraction helpers
# =========================================================================


def _extract_text_bs4(html: str, max_length: int = 3000) -> Tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    for tag in soup(
        ["script", "style", "noscript", "svg", "nav", "footer", "iframe", "img"]
    ):
        tag.decompose()

    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"id": re.compile(r"content|main", re.I)})
        or soup.find("div", {"class": re.compile(r"content|main|post", re.I)})
        or soup.body
        or soup
    )

    text = main_content.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_length], title


def _extract_text_stdlib(html: str, max_length: int = 3000) -> Tuple[str, str]:
    parser = ContentExtractor(max_length)
    parser.feed(html)
    return parser.get_text(), parser.get_title()


def _extract_text(
    html: str,
    max_length: int = 3000,
) -> Tuple[str, str]:
    """Extract visible text and title from HTML."""
    if HAS_BS4:
        return _extract_text_bs4(html, max_length)
    return _extract_text_stdlib(html, max_length)


# =========================================================================
# URL crawling
# =========================================================================


def crawl_url(
    url: str,
    timeout: int = 10,
    max_length: int = 3000,
    verbose: bool = False,
    max_retries: int = 1,
) -> str:
    """Crawl a URL and extract text content."""
    logger = logging.getLogger(__name__)

    if verbose:
        logger.debug("Crawling URL: %s", url)

    html, retries = _fetch_url_with_retry(
        url,
        timeout=timeout,
        max_retries=max_retries,
        verbose=verbose,
    )

    if html is None:
        error_msg = f"[Error: Failed to fetch {url} after {retries} retries]"
        if verbose:
            logger.warning("Failed to crawl %s after %d retries", url, retries)
        return error_msg

    if not re.search(r"<\s*(html|head|body|div|p)\b", html[:2000], re.I):
        return "[Non-HTML content detected]"

    text, title = _extract_text(html, max_length)

    if verbose:
        logger.debug(
            "Extracted %d chars (title: %s) from %s",
            len(text),
            title[:60] if title else "(none)",
            url,
        )

    return text if text else "[No extractable text content]"


# =========================================================================
# Search result parsers
# =========================================================================


def _parse_duckduckgo_bs4(html: str, num_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")

    for r in soup.select(".result, .web-result"):
        a_tag = r.select_one(".result__a, .result-link")
        snippet_tag = r.select_one(".result__snippet, .result-snippet")
        if not a_tag:
            continue

        href = a_tag.get("href", "")

        if "uddg=" in href:
            try:
                parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                # FIX: parse_qs returns a dict of lists
                href = parsed_qs.get("uddg", [""])[0]
            except Exception:
                continue

        href = urllib.parse.unquote(href)

        if not href.startswith("http"):
            continue

        if "duckduckgo.com" in href:
            continue

        results.append(
            {
                "url": href,
                "title": a_tag.get_text(strip=True),
                "snippet": (snippet_tag.get_text(strip=True) if snippet_tag else ""),
            }
        )
        if len(results) >= num_results:
            break

    return results


def _parse_duckduckgo_regex(html: str, num_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    seen_urls = set()

    # FIX: Regex matches URL-encoded paths properly now
    for m in re.finditer(r"uddg=([^&\"\']+)", html):
        url = urllib.parse.unquote(m.group(1))
        if not url.startswith("http") or url in seen_urls or "duckduckgo.com" in url:
            continue
        seen_urls.add(url)
        results.append({"url": url, "title": "", "snippet": ""})
        if len(results) >= num_results:
            break

    if not results:
        for m in re.finditer(
            r'class="result__a"[^>]*href="(https?://[^"]+)"[^>]*>([^<]*)<', html
        ):
            url = urllib.parse.unquote(m.group(1))
            title = m.group(2).strip()
            if url in seen_urls or "duckduckgo.com" in url:
                continue
            seen_urls.add(url)
            results.append({"url": url, "title": title, "snippet": ""})
            if len(results) >= num_results:
                break

    return results


def _parse_duckduckgo(html: str, num_results: int) -> List[Dict[str, str]]:
    if HAS_BS4:
        results = _parse_duckduckgo_bs4(html, num_results)
        if results:
            return results
    return _parse_duckduckgo_regex(html, num_results)


def _parse_google_bs4(html: str, num_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    seen_urls = set()
    soup = BeautifulSoup(html, "html.parser")

    for a_tag in soup.select("a[href]"):
        href = a_tag.get("href", "")

        if "/url?q=" in href:
            try:
                parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                # FIX: extract the string from the list returned by parse_qs
                url = parsed_qs.get("q", [""])[0]
            except Exception:
                continue
        elif href.startswith("http"):
            url = href
        else:
            continue

        url = urllib.parse.unquote(url)

        skip_domains = [
            "google.com",
            "google.co.",
            "googleapis.com",
            "gstatic.com",
            "youtube.com",
            "accounts.google",
        ]
        if any(d in url for d in skip_domains):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = a_tag.get_text(strip=True)
        results.append({"url": url, "title": title, "snippet": ""})
        if len(results) >= num_results:
            break

    return results


def _parse_google_regex(html: str, num_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    seen_urls = set()

    for m in re.finditer(r'/url\?q=([^&"]+)', html):
        url = urllib.parse.unquote(m.group(1))
        if not url.startswith("http"):
            continue
        skip_domains = [
            "google.com",
            "google.co.",
            "googleapis.com",
            "gstatic.com",
            "youtube.com",
        ]
        if any(d in url for d in skip_domains):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({"url": url, "title": "", "snippet": ""})
        if len(results) >= num_results:
            break

    return results


def _parse_google(html: str, num_results: int) -> List[Dict[str, str]]:
    if HAS_BS4:
        results = _parse_google_bs4(html, num_results)
        if results:
            return results
    return _parse_google_regex(html, num_results)


def _parse_searx_json(html: str, num_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    try:
        data = json.loads(html)
        for item in data.get("results", []):
            url = item.get("url", "")
            if not url.startswith("http"):
                continue
            results.append(
                {
                    "url": url,
                    "title": item.get("title", ""),
                    "snippet": item.get("content", ""),
                }
            )
            if len(results) >= num_results:
                break
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return results


def _decode_bing_url(href: str) -> str:
    """
    Decode Bing redirect URL to extract the actual destination URL.
    Handles various encoding formats and provides graceful fallback.
    """
    if not href.startswith("https://www.bing.com/ck/a"):
        return href

    try:
        parsed = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(parsed.query)

        if "u" not in params or not params["u"]:
            return href

        encoded_url = params["u"][0]

        # Handle different encoding patterns
        if encoded_url.startswith("a1aHR0c"):
            # Base64 encoded URL with 'a1' prefix
            b64_part = encoded_url[2:]  # Remove 'a1' prefix
        elif encoded_url.startswith("aHR0c"):
            # Base64 encoded URL with 'a' prefix
            b64_part = encoded_url[1:]  # Remove 'a' prefix
        else:
            # Not a standard Bing encoding, return as-is
            return href

        # Clean up the base64 string
        b64_part = b64_part.strip()

        # Fix base64 padding issues
        padding_needed = len(b64_part) % 4
        if padding_needed:
            b64_part += "=" * (4 - padding_needed)

        # Remove any non-base64 characters that might have slipped in
        b64_part = re.sub(r"[^a-zA-Z0-9+/=]", "", b64_part)

        # Decode with error handling
        try:
            decoded_bytes = base64.b64decode(b64_part, validate=True)
            decoded_url = decoded_bytes.decode("utf-8")

            # Validate the decoded URL
            if decoded_url.startswith("http"):
                return decoded_url
            else:
                return href  # Fallback if decoded URL doesn't look valid

        except (base64.binascii.Error, UnicodeDecodeError):
            # If base64 decoding fails, return original href
            return href

    except Exception:
        # If anything else fails, return original href
        return href


def _parse_bing_bs4(html: str, num_results: int) -> List[Dict[str, str]]:
    """Parse Bing search results with improved URL decoding."""
    results: List[Dict[str, str]] = []
    if not HAS_BS4:
        return results

    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()

    for result in soup.select(".b_algo"):
        # Get title and URL
        h2 = result.select_one("h2")
        if not h2:
            continue

        title_link = h2.select_one("a")
        if not title_link:
            continue

        title = title_link.get_text(strip=True)
        href = title_link.get("href", "")

        # Decode Bing redirect URL
        clean_url = _decode_bing_url(href)

        # Skip non-HTTP URLs or duplicates
        if not clean_url.startswith("http") or clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)

        # Get description
        snippet = ""
        p = result.select_one("p")
        if p:
            snippet = p.get_text(strip=True)

        results.append(
            {
                "url": clean_url,
                "title": title,
                "snippet": snippet,
            }
        )

        if len(results) >= num_results:
            break

    return results


def _parse_brave_bs4(html: str, num_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    if not HAS_BS4:
        return results

    soup = BeautifulSoup(html, "html.parser")
    for r in soup.select(".snippet"):
        a_tag = r.select_one("a[href]")
        snippet_tag = r.select_one(".snippet-description")
        if not a_tag:
            continue
        href = a_tag.get("href", "")
        if not href.startswith("http"):
            continue
        results.append(
            {
                "url": href,
                "title": a_tag.get_text(strip=True),
                "snippet": (snippet_tag.get_text(strip=True) if snippet_tag else ""),
            }
        )
        if len(results) >= num_results:
            break
    return results


# =========================================================================
# Search engine orchestrator
# =========================================================================


def _build_search_engines(
    query: str,
    num_results: int,
    lang: str,
    region: str,
    safe_search: str = "moderate",
) -> List[Dict[str, Any]]:
    """Build the ordered list of search engine configurations."""
    encoded_query = urllib.parse.quote_plus(query)

    # Map universal safe_search parameters across engines
    {"strict": "1", "moderate": "-2", "off": "-1"}.get(safe_search, "-2")
    searx_safe = {"strict": "2", "moderate": "1", "off": "0"}.get(safe_search, "1")
    google_safe = {"strict": "active", "moderate": "active", "off": "off"}.get(
        safe_search, "active"
    )

    engines = [
        # 1. Bing Search (primary - working with proper URL decoding)
        {
            "name": "Bing",
            "url": (
                f"https://www.bing.com/search?q={encoded_query}"
                f"&setlang={lang}&cc={region}"
            ),
            "method": "GET",
            "parser": _parse_bing_bs4,
            "is_json": False,
            "extra_headers": None,
        },
        # 2. SearXNG JSON API — primary fallback
        {
            "name": "SearXNG (searx.xyz)",
            "url": (
                f"https://searx.xyz/search?q={encoded_query}"
                f"&format=json&language={lang}&safesearch={searx_safe}"
            ),
            "method": "GET",
            "parser": _parse_searx_json,
            "is_json": True,
            "extra_headers": {"Accept": "application/json"},
        },
        # 3. SearXNG alternate instance
        {
            "name": "SearXNG (search.brave.com)",
            "url": (
                f"https://search.brave.com/searx/search?q={encoded_query}"
                f"&format=json&language={lang}&safesearch={searx_safe}"
            ),
            "method": "GET",
            "parser": _parse_searx_json,
            "is_json": True,
            "extra_headers": {"Accept": "application/json"},
        },
        # 4. Brave Search
        {
            "name": "Brave",
            "url": (
                f"https://search.brave.com/search"
                f"?q={encoded_query}&source=web&safesearch={safe_search}"
            ),
            "method": "GET",
            "parser": _parse_brave_bs4,
            "is_json": False,
            "extra_headers": None,
        },
        # 5. Google (most likely to trigger bot detection)
        {
            "name": "Google",
            "url": (
                f"https://www.google.com/search?q={encoded_query}"
                f"&hl={lang}&gl={region}&num={num_results}&safe={google_safe}"
            ),
            "method": "GET",
            "parser": _parse_google,
            "is_json": False,
            "extra_headers": None,
        },
    ]

    return engines


def _web_search(
    query: str,
    num_results: int = 10,
    lang: str = "en",
    region: str = "us",
    safe_search: str = "moderate",
    timeout: int = 10,
    max_retries: int = 2,
    verbose: bool = False,
) -> Tuple[List[Dict[str, str]], SearchMetadata]:
    """Attempt to fetch search results from multiple engines sequentially."""
    logger = logging.getLogger(__name__)
    fetcher = _preferred_fetcher()

    metadata = SearchMetadata(
        query=query,
        fetcher_tool=fetcher,
        fetcher_version=_get_tool_version(fetcher) or "unknown",
    )

    start_time = time.monotonic()
    engines = _build_search_engines(query, num_results, lang, region, safe_search)
    total_retries = 0

    for engine in engines:
        try:
            logger.info("Trying %s search", engine["name"])

            html, retries = _fetch_url_with_retry(
                engine["url"],
                timeout=timeout,
                max_retries=max_retries,
                verbose=verbose,
                extra_headers=engine.get("extra_headers"),
                method=engine.get("method", "GET"),
                data=engine.get("data"),
            )
            total_retries += retries

            if html is None:
                logger.warning("%s returned no usable response", engine["name"])
                continue

            if not engine.get("is_json") and _detect_bot_challenge(html):
                logger.warning("%s returned bot detection page", engine["name"])
                metadata.had_bot_detection = True
                if verbose:
                    logger.debug(
                        "%s raw response (first 500): %s", engine["name"], html[:500]
                    )
                continue

            parsed = engine["parser"](html, num_results)

            if parsed:
                logger.info("Got %d results from %s", len(parsed), engine["name"])
                metadata.engine_used = engine["name"]
                metadata.total_results = len(parsed)
                metadata.retries_used = total_retries
                elapsed = int((time.monotonic() - start_time) * 1000)
                metadata.search_time_ms = elapsed
                return parsed, metadata

            logger.warning("%s returned no parseable results", engine["name"])
            if verbose:
                logger.debug(
                    "%s raw response (first 800): %s", engine["name"], html[:800]
                )

        except Exception as e:
            logger.warning("%s search failed: %s", engine["name"], e, exc_info=verbose)
            continue

    elapsed = int((time.monotonic() - start_time) * 1000)
    metadata.search_time_ms = elapsed
    metadata.retries_used = total_retries
    return [], metadata


# =========================================================================
# Logging
# =========================================================================


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# =========================================================================
# Query validation and normalization
# =========================================================================


def validate_query(query: str) -> str:
    if not query or not query.strip():
        raise ValueError("Search query cannot be empty")
    cleaned = query.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > 500:
        raise ValueError(f"Query too long ({len(cleaned)} chars, max 500)")
    return cleaned


# =========================================================================
# Main search orchestrator
# =========================================================================


def search_google(
    query: str,
    num_results: int = 10,
    lang: str = "en",
    region: str = "us",
    safe_search: str = "moderate",
    pause: float = 2.0,
    crawl: bool = False,
    content_length: int = 3000,
    timeout: int = 10,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """Perform a web search and optionally crawl content from results."""
    setup_logging(verbose)
    logger = logging.getLogger(__name__)
    query = validate_query(query)

    raw_results, metadata = _web_search(
        query,
        num_results=num_results,
        lang=lang,
        region=region,
        safe_search=safe_search,
        timeout=timeout,
        max_retries=2,
        verbose=verbose,
    )

    if not raw_results:
        error_detail = "All search engines failed."
        if metadata.had_bot_detection:
            error_detail += (
                " Bot detection was triggered. Try again later or from a different IP."
            )
        raise RuntimeError(f"{error_detail} No results available for query: {query}")

    logger.info(
        "Search complete: %d results from %s in %dms",
        metadata.total_results,
        metadata.engine_used,
        metadata.search_time_ms,
    )

    results: List[Dict[str, Any]] = []

    for i, item in enumerate(raw_results[:num_results], 1):
        result = SearchResult(
            position=i,
            url=item["url"],
            title=item.get("title", ""),
            snippet=item.get("snippet", ""),
            content="",
            error="",
        )

        if crawl:
            if verbose:
                logger.info("Crawling result %d: %s", i, item["url"])

            crawled = crawl_url(
                item["url"],
                timeout=timeout,
                max_length=content_length,
                verbose=verbose,
                max_retries=1,
            )

            if crawled and not crawled.startswith("[Error:"):
                result.content = crawled
            else:
                result.error = crawled or "Failed to fetch content"
                result.content = ""

            if not result.title and result.content:
                html, _ = _fetch_url_with_retry(
                    item["url"],
                    timeout=timeout,
                    max_retries=0,
                    verbose=False,
                )
                if html:
                    _, page_title = _extract_text(html, max_length=0)
                    if page_title:
                        result.title = page_title

            if i < len(raw_results[:num_results]):
                actual_pause = pause + random.uniform(0.2, 0.8)
                time.sleep(actual_pause)

        results.append(asdict(result))
        logger.debug("Result %d: %s", i, item["url"])

    return results


# =========================================================================
# Diagnostic function
# =========================================================================


def diagnose(verbose: bool = True) -> Dict[str, Any]:
    setup_logging(verbose)
    logger = logging.getLogger(__name__)
    report: Dict[str, Any] = {}

    fetcher = None
    try:
        fetcher = _preferred_fetcher()
    except EnvironmentError as e:
        report["fetcher_error"] = str(e)

    report["tools"] = {
        "curlie": CURLIE_BIN or "not found",
        "wget2": WGET2_BIN or "not found",
        "curl": CURL_BIN or "not found",
        "wget": WGET_BIN or "not found",
        "preferred": fetcher or "none",
    }

    if fetcher:
        ver = _get_tool_version(fetcher)
        report["tools"]["preferred_version"] = ver or "unknown"

    report["parsers"] = {
        "beautifulsoup4": HAS_BS4,
        "stdlib_fallback": True,
    }

    # Test Bing connectivity
    test_url = "https://www.bing.com/search?q=test"
    logger.info("Testing connectivity to Bing...")
    html = _fetch_url(test_url, timeout=10, verbose=verbose)

    if html:
        report["connectivity"] = "OK"
        report["response_length"] = len(html)
        report["bot_detected"] = _detect_bot_challenge(html)

        results = _parse_bing_bs4(html, 3)
        report["parse_test"] = {
            "results_found": len(results),
            "sample": results[:2] if results else [],
        }
    else:
        report["connectivity"] = "FAILED"

    logger.info("Diagnostic report: %s", json.dumps(report, indent=2))
    return report


# =========================================================================
# Public entry point (preserves existing signature)
# =========================================================================


def run(
    query: str,
    limit: int = 10,
    lang: str = "en",
    region: str = "us",
    safe_search: Literal["off", "moderate", "strict"] = "moderate",
    pause: float = 2.0,
    crawl: bool = False,
    content_length: int = 3000,
    timeout: int = 10,
    verbose: bool = False,
):
    try:
        results = search_google(
            query=query,
            num_results=limit,
            lang=lang,
            region=region,
            safe_search=safe_search,
            pause=pause,
            crawl=crawl,
            content_length=content_length,
            timeout=timeout,
            verbose=verbose,
        )
        return results
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except (ValueError, RuntimeError):
        raise
    except Exception as e:
        raise RuntimeError(f"An unexpected error occurred: {e}") from e


# =========================================================================
# CLI interface
# =========================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Web Search Tool with Content Crawling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "python tutorial"
  %(prog)s "latest AI news" --limit 5 --crawl --verbose
  %(prog)s --diagnose
        """,
    )
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=10,
        help="Number of results (default: 10)",
    )
    parser.add_argument("--lang", default="en", help="Language code")
    parser.add_argument("--region", default="us", help="Region code")
    parser.add_argument(
        "--safe-search",
        choices=["off", "moderate", "strict"],
        default="moderate",
        help="Safe search level",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=2.0,
        help="Pause between crawl requests (seconds)",
    )
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="Crawl content from result URLs",
    )
    parser.add_argument(
        "--content-length",
        type=int,
        default=3000,
        help="Max content length per page (chars)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Request timeout (seconds)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Run diagnostic check and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output as JSON (default)",
    )

    args = parser.parse_args()

    if args.diagnose:
        report = diagnose(verbose=True)
        print(json.dumps(report, indent=2))
        sys.exit(0)

    if not args.query:
        parser.error("Search query is required (or use --diagnose)")

    try:
        results = run(
            query=args.query,
            limit=args.limit,
            lang=args.lang,
            region=args.region,
            safe_search=args.safe_search,
            pause=args.pause,
            crawl=args.crawl,
            content_length=args.content_length,
            timeout=args.timeout,
            verbose=args.verbose,
        )
        print(json.dumps(results, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except (ValueError, RuntimeError) as e:
        logging.error(str(e))
        error_output = {"error": str(e), "query": args.query}
        print(json.dumps(error_output, indent=2), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logging.error("Unexpected error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
