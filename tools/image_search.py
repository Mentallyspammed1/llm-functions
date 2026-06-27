#!/usr/bin/env python3
# @describe Search for images on the web using Google CSE or DuckDuckGo fallbacks.
# @option --query! <TEXT>                          Image search query
# @option --limit <NUM>                            Maximum number of results to return (default: 10)
"""
image_search.py - Search for images on the web.
"""

import os
import json
import sys
import argparse
import urllib.parse
import logging
import requests
from typing import List, Dict, Any

def load_env():
    # Check parent directory (root of repository) for .env file
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

def search_google_images(query: str, count: int = 10) -> List[Dict[str, Any]]:
    api_key = os.environ.get("GOOGLE_API_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return []
    
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "searchType": "image",
        "num": min(count, 10)
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title"),
                "image_url": item.get("link"),
                "thumbnail_url": item.get("image", {}).get("thumbnailLink"),
                "width": item.get("image", {}).get("width"),
                "height": item.get("image", {}).get("height"),
                "source": item.get("image", {}).get("contextLink")
            })
        return results
    except Exception as e:
        logging.warning(f"Google Image Search failed: {e}")
        return []

def search_ddg_images(query: str, count: int = 10) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        # Step 1: get vqd token
        url = f"https://duckduckgo.com/?q={urllib.parse.quote(query)}"
        res = session.get(url, timeout=10)
        res.raise_for_status()
        import re
        match = re.search(r"vqd=([0-9-]+)", res.text)
        if not match:
            match = re.search(r'vqd=["\']([^"\']+)["\']', res.text)
        if not match:
            return []
        vqd = match.group(1)
        
        # Step 2: query images API
        img_url = "https://duckduckgo.com/i.js"
        params = {
            "l": "us-en",
            "o": "json",
            "q": query,
            "vqd": vqd,
            "f": ",,,",
            "p": "1"
        }
        headers_img = {
            "Referer": "https://duckduckgo.com/"
        }
        res2 = session.get(img_url, params=params, headers=headers_img, timeout=10)
        res2.raise_for_status()
        data = res2.json()
        
        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title"),
                "image_url": item.get("image"),
                "thumbnail_url": item.get("thumbnail"),
                "width": item.get("width"),
                "height": item.get("height"),
                "source": item.get("source")
            })
            if len(results) >= count:
                break
        return results
    except Exception as e:
        logging.warning(f"DuckDuckGo image search failed: {e}")
        return []

def run(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    load_env()
    # 1. Try Google Image Search CSE first
    results = search_google_images(query, limit)
    if results:
        return results
    # 2. Fallback to DuckDuckGo Keyless Image Search
    return search_ddg_images(query, limit)

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
    parser = argparse.ArgumentParser(description="Image search tool")
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.query, args.limit), indent=2))
