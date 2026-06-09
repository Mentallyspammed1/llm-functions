#!/usr/bin/env python3
# @describe Google Search Tool (You.com API primary)
# @option --query! <TEXT> Search query
# @option --limit <NUM> Maximum results (default: 10)
"""
google_search.py - Google Search Tool using You.com (YDC) API
"""

import os
import json
import sys
import argparse
import requests
import urllib.parse
from typing import List, Dict, Any

def get_ydc_api_key() -> str:
    # Use the same fallback as web_search.py
    return os.environ.get("YDC_API_KEY") or os.environ.get("YOU_API_KEY") or "ydc-sk-3be25b63a354f86f-cZsqdcYZe3xHo2qxVUZxEmTI1wAzlfG8-23e9d3b8"

def search_ydc(query: str, count: int = 10) -> List[Dict[str, Any]]:
    api_key = get_ydc_api_key()
    if not api_key:
        return [{"error": "API key not configured"}]

    params = {"query": query, "count": count}
    url = f"https://ydc-index.io/v1/search?{urllib.parse.urlencode(params)}"
    headers = {"X-API-Key": api_key}
    
    try:
        # Respect environment proxies (socks5h://127.0.0.1:9050 from .env)
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for section in ["web", "news"]:
            for item in data.get("results", {}).get(section, []):
                results.append({
                    "position": len(results) + 1,
                    "type": section,
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": " ".join(item.get("snippets", [])) or item.get("description", ""),
                    "age": item.get("page_age")
                })
        return results
    except Exception as e:
        return [{"error": f"API request failed: {str(e)}"}]

def run(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    return search_ydc(query, limit)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.query, args.limit), indent=2))
