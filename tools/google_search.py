#!/usr/bin/env python3
# @describe Google Search Tool with Content Crawling (You.com API primary)
"""
google_search.py - Google Search Tool with Content Crawling
"""

import json
import argparse
import logging
import time
import urllib.request
import urllib.error
import urllib.parse
import os
from typing import List, Dict, Any, Literal
from html.parser import HTMLParser
from dataclasses import dataclass

try:
    import requests
    from bs4 import BeautifulSoup

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from googlesearch import search

    HAS_GOOGLESEARCH = True
except ImportError:
    HAS_GOOGLESEARCH = False


@dataclass
class SearchResult:
    """Represents a single search result with optional crawled content."""

    position: int
    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""
    error: str = ""


class ContentExtractor(HTMLParser):
    """HTML parser to extract clean text content from web pages."""

    def __init__(self, max_length: int = 3000):
        super().__init__()
        self.max_length = max_length
        self.content = []
        self.in_script = False
        self.in_style = False
        self.current_length = 0

    def handle_starttag(self, tag, attrs):
        if tag in ["script", "style"]:
            self.in_script = True
        elif tag in ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "span"]:
            if self.content and self.content[-1] not in [" ", "\n"]:
                self.content.append(" ")

    def handle_endtag(self, tag):
        if tag in ["script", "style"]:
            self.in_script = False
        elif tag in ["p", "div", "li"]:
            if self.content and self.content[-1] != "\n":
                self.content.append("\n")

    def handle_data(self, data):
        if not self.in_script and data.strip():
            words = data.split()
            for word in words:
                if self.current_length >= self.max_length:
                    return
                self.content.append(word)
                self.current_length += len(word) + 1

    def get_text(self) -> str:
        text = " ".join(self.content)
        return " ".join(text.split())


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")


def validate_query(query: str) -> None:
    if not query or not query.strip():
        raise ValueError("Search query cannot be empty")


def crawl_url(
    url: str, timeout: int = 10, max_length: int = 3000, verbose: bool = False
) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type:
                return f"[Non-HTML content: {content_type}]"
            html_content = response.read().decode("utf-8", errors="ignore")
            parser = ContentExtractor(max_length)
            parser.feed(html_content)
            return parser.get_text()
    except Exception as e:
        return f"[Error: {str(e)}]"


def get_ydc_api_key():
    key = os.environ.get("YDC_API_KEY")
    if not key:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    if "YDC_API_KEY=" in line:
                        return line.split("=", 1)[1].strip()
    return key


def search_ydc(query: str, count: int = 10) -> List[Dict[str, Any]]:
    api_key = get_ydc_api_key()
    if not api_key:
        return []
    url = f"https://ydc-index.io/v1/search?query={urllib.parse.quote(query)}&count={count}"
    headers = {"X-API-Key": api_key}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("results", {}).get("web", []):
            results.append(
                {
                    "position": len(results) + 1,
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "snippet": " ".join(item.get("snippets", []))
                    or item.get("description", ""),
                }
            )
        return results
    except Exception:
        return []


def fallback_google_search(
    query: str, num_results: int = 10, timeout: int = 10
) -> List[str]:
    # Simplified fallback: just return some URLs if we can scrape them
    return ["https://docs.python.org/3/tutorial/"]


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
    setup_logging(verbose)
    validate_query(query)
    results = search_ydc(query, num_results)
    if not results:
        # Final fallback/error
        raise RuntimeError(
            "Search failed. Please check your internet connection or API Key."
        )
    if crawl:
        for i, res in enumerate(results):
            res["content"] = crawl_url(res["url"], timeout, content_length, verbose)
            if i < len(results) - 1:
                time.sleep(pause)
    return results


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
        return search_google(
            query,
            limit,
            lang,
            region,
            safe_search,
            pause,
            crawl,
            content_length,
            timeout,
            verbose,
        )
    except Exception as e:
        return [{"error": str(e)}]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.query, args.limit, verbose=args.verbose), indent=2))
