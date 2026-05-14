#!/usr/bin/env python3
# @describe Bybit Account & Position Tools - Get balance, positions, orders, and PnL
# @option --coin           Filter by coin (default: USDT)
# @option --symbol         Filter by symbol (e.g., BTCUSDT)
# @option --action         Action: balance|positions|open_orders|closed_pnl|executions
# @option --order_id       Order ID for executions lookup
# @option --limit          Number of results (default: 20)
# @option --use_tor       Route through Tor proxy (default: true)
"""
Bybit Account & Position Tools
Requires BYBIT_API_KEY and BYBIT_API_SECRET
"""
import os
import sys
import json
import argparse

# Load .env if exists
from pathlib import Path
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key, val)

from pybit.unified_trading import HTTP

# Tor proxy support
USE_TOR = os.getenv("BYBIT_USE_TOR", "true").lower() == "true"
PROXY = "socks5h://127.0.0.1:9050" if USE_TOR else None

# Use testnet from .env
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

session = HTTP(
    testnet=TESTNET,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
    proxy=PROXY
)


def bybit_get_balance(coin=None):
    """Get wallet balance"""
    try:
        result = session.get_wallet_balance(accountType="UNIFIED", coin=coin)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_positions(symbol=None):
    """View open positions"""
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_open_orders(symbol=None):
    """Get open orders"""
    try:
        result = session.get_open_orders(category="linear", symbol=symbol)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_closed_pnl(symbol=None, limit=20):
    """Get closed PnL history"""
    try:
        result = session.get_closed_pnl(category="linear", symbol=symbol, limit=limit)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_executions(symbol=None, order_id=None):
    """Get execution history"""
    try:
        result = session.get_executions(category="linear", symbol=symbol, orderId=order_id)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def main():
    parser = argparse.ArgumentParser(description="Bybit Account Tools")
    parser.add_argument("--action", default="balance", help="Action to perform")
    parser.add_argument("--coin", default=None, help="Coin to filter")
    parser.add_argument("--symbol", default=None, help="Symbol to filter")
    parser.add_argument("--order_id", default=None, help="Order ID")
    parser.add_argument("--limit", type=int, default=20, help="Result limit")
    args = parser.parse_args()

    if args.action == "balance":
        bybit_get_balance(args.coin)
    elif args.action == "positions":
        bybit_get_positions(args.symbol)
    elif args.action == "open_orders":
        bybit_get_open_orders(args.symbol)
    elif args.action == "closed_pnl":
        bybit_get_closed_pnl(args.symbol, args.limit)
    elif args.action == "executions":
        bybit_get_executions(args.symbol, args.order_id)
    else:
        print(json.dumps({"error": f"Unknown action: {args.action}"}))


if __name__ == "__main__":
    main()
