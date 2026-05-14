# @describe Get ticker data from Bybit
# @option --category <linear|inverse> Product type
# @option --symbol <SYMBOL> Symbol
# @option --use-tor <BOOL> Use Tor
#!/usr/bin/env python3
import os
import sys
import json
import argparse
import requests
from typing import Optional
import bybit_core

def run(category: str, symbol: Optional[str] = None, testnet: bool = False, use_tor: bool = True):
    params = {"category": category}
    if symbol: params["symbol"] = symbol
    
    # Simple public request
    cfg = bybit_core.get_config()
    cfg["use_tor"] = use_tor
    resp = requests.get(f"https://api.bybit.com/v5/market/tickers", params=params, proxies=cfg['proxies'] if use_tor else None, timeout=30).json()
    
    return json.dumps(resp, indent=2)

if __name__ == "__main__":
    import requests
    from typing import Optional
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True)
    parser.add_argument("--symbol")
    parser.add_argument("--use-tor", type=lambda x: str(x).lower() == "true", default=True)
    args = parser.parse_args()
    print(run(args.category, args.symbol, use_tor=args.use_tor))
