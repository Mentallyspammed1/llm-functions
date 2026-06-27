#!/usr/bin/env python3
# @describe Google Search Tool (Google Custom Search primary, with You.com/DuckDuckGo fallbacks)
# @option --query! <TEXT> Search query
# @option --limit <NUM> Maximum results (default: 10)
"""
google_search.py - Google Search Tool with fallback chains
"""

import os
import json
import sys
import argparse
import requests
import logging
from typing import List, Dict, Any
from ydc_search import search_ydc

# Make sure env variables from local .env are loaded
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

def search_google_cse(query: str, count: int = 10) -> List[Dict[str, Any]]:
    api_key = os.environ.get("GOOGLE_API_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return []
    
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": min(count, 10)
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for item in data.get("items", []):
            results.append({
                "type": "web",
                "title": item.get("title"),
                "url": item.get("link"),
                "snippet": item.get("snippet", ""),
                "age": None
            })
        return results
    except Exception as e:
        logging.warning(f"Google CSE search failed: {e}")
        return []

def run(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    load_env()
    # 1. Try actual Google Search CSE API
    results = search_google_cse(query, limit)
    if results:
        return results
    # 2. Fallback to YDC/DuckDuckGo
    return search_ydc(query, limit)

if __name__ == "__main__":
    # 1. Parse JSON input if passed by aichat's tool dispatcher
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
        except Exception as err:
            print(json.dumps([{"error": f"JSON argument parse error: {err}"}]))
            sys.exit(1)

    # 2. Fallback to standard CLI arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.query, args.limit), indent=2))
