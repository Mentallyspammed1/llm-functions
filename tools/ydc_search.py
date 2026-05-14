#!/usr/bin/env python3
# @describe Web Search Tool using You.com (YDC) API with domain filtering
# @option --query! <TEXT>                          Search query
# @option --count <NUM>                            Number of results (default: 10)
# @option --include-domains <DOMAINS>              Comma-separated domains to include
# @option --exclude-domains <DOMAINS>              Comma-separated domains to exclude
# @flag   --verbose                                Enable verbose output
"""
ydc_search.py - Web Search Tool using You.com (YDC) API
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
    count: int = 10,
    include_domains: str = None,
    exclude_domains: str = None,
    verbose: bool = False,
):
    setup_logging(verbose)
    load_env()
    return search_ydc(query, count, include_domains, exclude_domains)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="You.com Search Tool")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--query", dest="query_opt", help="Search query (alternative)")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--include-domains")
    parser.add_argument("--exclude-domains")
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
                count=args.count,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                verbose=args.verbose,
            ),
            indent=2,
        )
    )
