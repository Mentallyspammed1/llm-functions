#!/usr/bin/env python3
# @describe Robust Web Search Tool using You.com (YDC) API
# @option --query! <TEXT>                          Search query
# @option --limit <NUM>                            Maximum results to return (default: 10)
# @option --include-domains <DOMAINS>              Comma-separated domains to include
# @option --exclude-domains <DOMAINS>              Comma-separated domains to exclude
# @flag   --crawl                                  Fetch and extract content from result URLs (simulated)
# @flag   --verbose                                Enable verbose output
"""
web_search.py - Robust Web Search Tool using You.com (YDC) API
"""

import os
import json
import sys
import argparse
import logging
import requests
import urllib.parse
from typing import List, Dict, Any


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")


def load_env():
    """Load environment variables from .env file."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()


def search_ydc(
    query: str,
    count: int = 10,
    include_domains: str = None,
    exclude_domains: str = None,
) -> List[Dict[str, Any]]:
    """Search using You.com API."""
    api_key = os.environ.get("YDC_API_KEY")
    if not api_key:
        return [{"error": "YDC_API_KEY not found in environment or .env file"}]

    params = {"query": query, "count": count}
    if include_domains:
        params["include_domains"] = include_domains
    if exclude_domains:
        params["exclude_domains"] = exclude_domains

    url = f"https://ydc-index.io/v1/search?{urllib.parse.urlencode(params)}"
    headers = {"X-API-Key": api_key}

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()

        results = []
        for section in ["web", "news"]:
            for item in data.get("results", {}).get(section, []):
                results.append(
                    {
                        "position": len(results) + 1,
                        "type": section,
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "snippet": " ".join(item.get("snippets", []))
                        or item.get("description", ""),
                        "age": item.get("page_age"),
                    }
                )
        return results
    except Exception as e:
        return [{"error": f"API Error: {str(e)}"}]


def run(
    query: str,
    limit: int = 10,
    include_domains: str = None,
    exclude_domains: str = None,
    crawl: bool = False,
    verbose: bool = False,
):
    """
    Perform a web search using You.com API.
    """
    setup_logging(verbose)
    load_env()

    if not query:
        return [{"error": "Search query is required"}]

    results = search_ydc(query, limit, include_domains, exclude_domains)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Web Search Tool")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--query", dest="query_opt", help="Search query (alternative)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-domains")
    parser.add_argument("--exclude-domains")
    parser.add_argument("--crawl", action="store_true")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()
    query = args.query or args.query_opt

    if not query:
        print(json.dumps([{"error": "Query is required"}]))
        sys.exit(1)

    print(
        json.dumps(
            run(
                query,
                limit=args.limit,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                crawl=args.crawl,
                verbose=args.verbose,
            ),
            indent=2,
        )
    )
