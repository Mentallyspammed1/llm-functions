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


def search_ddg(query: str, count: int = 10) -> List[Dict[str, Any]]:
    """Free DuckDuckGo fallback search using HTML scraping."""
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.post(url, data={"q": query}, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Simple regex parser to extract DuckDuckGo results without beautifulsoup dependency
        import re
        body = response.text
        results = []
        matches = re.findall(
            r'<a[^>]+class="result__url"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)',
            body,
            re.DOTALL
        )
        for link, title, snippet in matches:
            title_clean = re.sub(r'<[^>]+>', '', title).strip()
            snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()
            # Parse redirect URL
            if link.startswith("//duckduckgo.com/l/?uddg="):
                link = urllib.parse.parse_qs(urllib.parse.urlparse("https:" + link).query).get("uddg", [link])[0]
            elif link.startswith("/l/?uddg="):
                link = urllib.parse.parse_qs(urllib.parse.urlparse("https://duckduckgo.com" + link).query).get("uddg", [link])[0]
            
            results.append({
                "type": "web",
                "title": title_clean,
                "url": link,
                "snippet": snippet_clean,
                "age": None
            })
            if len(results) >= count:
                break
        return results
    except Exception as e:
        logging.error(f"DuckDuckGo fallback search failed: {e}")
        return []


def search_ydc(
    query: str,
    count: int = 10,
    include_domains: str = None,
    exclude_domains: str = None,
) -> List[Dict[str, Any]]:
    """Search using You.com API with automatic DuckDuckGo fallback."""
    api_key = os.environ.get("YDC_API_KEY") or os.environ.get("YOU_API_KEY") or "ydc-sk-3be25b63a354f86f-cZsqdcYZe3xHo2qxVUZxEmTI1wAzlfG8-23e9d3b8"
    
    # Try YDC API
    if api_key:
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
            if results:
                return results
        except Exception as e:
            logging.warning(f"YDC search failed: {e}. Falling back to DuckDuckGo...")

    # Fallback to DuckDuckGo search
    return search_ddg(query, count)


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
    # 1. Parse JSON input if passed by aichat's tool dispatcher
    if len(sys.argv) > 1 and (sys.argv[1].startswith("{") or sys.argv[1].startswith("[")):
        try:
            kwargs = json.loads(sys.argv[1])
            # Handle variable mappings from dispatcher
            query_val = kwargs.get("query")
            count_val = kwargs.get("count", 10)
            inc = kwargs.get("include_domains")
            exc = kwargs.get("exclude_domains")
            verb = kwargs.get("verbose", False)
            
            if not query_val:
                print(json.dumps([{"error": "Query is required"}]))
                sys.exit(1)
            print(json.dumps(run(query_val, count=count_val, include_domains=inc, exclude_domains=exc, verbose=verb), indent=2))
            sys.exit(0)
        except Exception as err:
            print(json.dumps([{"error": f"JSON argument parse error: {err}"}]))
            sys.exit(1)

    # 2. Fallback to standard CLI arguments
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
