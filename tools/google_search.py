#!/usr/bin/env python3
# @describe Google Search Tool (Google Custom Search primary, with You.com/DuckDuckGo fallbacks)
# @option --query! <TEXT> Search query
# @option --limit <NUM> Maximum results (default: 10)
"""google_search.py - Google Search Tool with fallback chains"""
import os
import json
import sys
import argparse
import requests
import logging
from typing import List, Dict, Any

# ----------------------------------------------------------------------
# Import search backend – try ydc_search first, then web_search, then a stub
# ----------------------------------------------------------------------
try:
    # Primary backend: ydc_search (You.com/DuckDuckGo API)
    from ydc_search import search_ydc as _ydc_search
except ImportError:
    try:
        # Fallback backend: web_search (generic web search)
        from web_search import web_search as _web_search

        def _ydc_search(query: str, count: int = 10) -> List[Dict[str, Any]]:
            """
            Wrapper around web_search that returns results in the same shape
            expected by the rest of the code (list of dicts with keys
            'type', 'title', 'url', 'snippet', 'age').
            """
            # web_search may return a list directly or a dict with an "output" key
            raw = _web_search(query=query, limit=count)
            if isinstance(raw, dict) and "output" in raw:
                return raw["output"]
            return raw if isinstance(raw, list) else []
    except ImportError:
        # Minimal stub – returns an empty list when no backend is available
        def _ydc_search(query: str, count: int = 10) -> List[Dict[str, Any]]:
            return []

# ----------------------------------------------------------------------
# Environment loading
# ----------------------------------------------------------------------
def _load_env() -> None:
    """Load variables from a local .env file into os.environ."""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

# ----------------------------------------------------------------------
# Google Custom Search Engine (CSE) wrapper
# ----------------------------------------------------------------------
def _search_google_cse(query: str, count: int = 10) -> List[Dict[str, Any]]:
    """Perform a Google CSE query using API key & CX from environment."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": min(count, 10),
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("items", []):
            results.append(
                {
                    "type": "web",
                    "title": item.get("title"),
                    "url": item.get("link"),
                    "snippet": item.get("snippet", ""),
                    "age": None,
                }
            )
        return results
    except Exception as exc:  # pragma: no cover – defensive logging
        logging.warning(f"Google CSE search failed: {exc}")
        return []

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def run(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Execute a search query.

    1. Try Google CSE (real API).
    2. Fallback to the ydc_search backend (You.com/DuckDuckGo).
    3. If that also fails, the stub returns an empty list.
    """
    _load_env()

    # 1️⃣ Google CSE – the preferred source
    results = _search_google_cse(query, limit)
    if results:
        return results

    # 2️⃣ ydc_search backend (may be the real ydc_search or the web_search wrapper)
    return _ydc_search(query, limit)


# ----------------------------------------------------------------------
# CLI entry point (used when the tool is invoked directly)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # When called via the dispatcher with a JSON payload, e.g.
    #   {"query": "foo", "limit": 5}
    if len(sys.argv) > 1 and (sys.argv[1].startswith("{") or sys.argv[1].startswith("[")):
        try:
            kwargs = json.loads(sys.argv[1])
            query_val = kwargs.get("query")
            limit_val = kwargs.get("limit", 10)
            if not query_val:
                print(json.dumps([{"error": "Query is required"}]))
                sys.exit(1)
            print(json.dumps(run(query_val, limit_val), indent=2))
            sys.exit(0)
        except Exception as err:  # pragma: no cover
            print(json.dumps([{"error": f"JSON argument parse error: {err}"}]))
            sys.exit(1)

    # Otherwise treat as a simple CLI: python google_search.py --query "foo" --limit 5
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.query, args.limit), indent=2))
