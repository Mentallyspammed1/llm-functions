#!/usr/bin/env python3
# @describe Google Search Tool (You.com API primary with DuckDuckGo fallback)
# @option --query! <TEXT> Search query
# @option --limit <NUM> Maximum results (default: 10)
"""
google_search.py - Google Search Tool using You.com and DuckDuckGo fallbacks
"""

import os
import json
import sys
import argparse
from typing import List, Dict, Any
from ydc_search import search_ydc

def run(query: str, limit: int = 10) -> List[Dict[str, Any]]:
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
