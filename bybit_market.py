#!/usr/bin/env python3
# @describe Bybit Market Data Tools - Get orderbook, ticker, klines, funding rate
# @option --symbol!        Trading pair (e.g., BTCUSDT)
# @option --action         Action: orderbook|ticker|klines|funding|instruments
# @option --limit          Orderbook/klines limit (default: 50)
# @option --interval       Kline interval: 1|3|5|15|30|60|120|240|D (default: 15)
# @option --use_tor       Route through Tor proxy (default: true)
"""
Bybit Market Data Tools
Public market data endpoints - no authentication required
"""
import os
import json
import argparse
from pathlib import Path

# Load .env if exists
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

TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

session = HTTP(testnet=TESTNET, proxy=PROXY)


def bybit_get_orderbook(symbol, limit=50):
    """Get orderbook depth (L2)"""
    try:
        result = session.get_orderbook(category="linear", symbol=symbol, limit=limit)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_ticker(symbol):
    """Get 24h ticker information"""
    try:
        result = session.get_tickers(category="linear", symbol=symbol)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_klines(symbol, interval="15", limit=100):
    """Get candlestick/kline data"""
    try:
        result = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_funding_rate(symbol):
    """Get current funding rate"""
    try:
        result = session.get_funding_rate_history(category="linear", symbol=symbol, limit=1)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_instruments(symbol):
    """Get instrument info (tick size, lot size, etc.)"""
    try:
        result = session.get_instruments_info(category="linear", symbol=symbol)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def main():
    parser = argparse.ArgumentParser(description="Bybit Market Tools")
    parser.add_argument("--action", default="ticker", help="Action to perform")
    parser.add_argument("--symbol", required=True, help="Trading pair")
    parser.add_argument("--limit", type=int, default=50, help="Limit")
    parser.add_argument("--interval", default="15", help="Kline interval")
    args = parser.parse_args()

    if args.action == "orderbook":
        bybit_get_orderbook(args.symbol, args.limit)
    elif args.action == "ticker":
        bybit_get_ticker(args.symbol)
    elif args.action == "klines":
        bybit_get_klines(args.symbol, args.interval, args.limit)
    elif args.action == "funding":
        bybit_get_funding_rate(args.symbol)
    elif args.action == "instruments":
        bybit_get_instruments(args.symbol)
    else:
        print(json.dumps({"error": f"Unknown action: {args.action}"}))


if __name__ == "__main__":
    main()
