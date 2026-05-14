#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
"""
Bybit Market Data Tools
Public market data endpoints - no authentication required
"""
import json
import os
from pybit.unified_trading import HTTP
from argc import argc as Argc

# Configuration
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
USE_TOR = os.getenv("USE_TOR", "false").lower() == "true"
TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050")

# Bybit V5 Market Endpoints
session = HTTP(
    testnet=TESTNET,
    proxy=TOR_PROXY if USE_TOR else None
)

# @cmd Get orderbook depth
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --limit Depth limit (default: 50)
def bybit_get_orderbook(symbol, limit=50):
    """Fetch L2 orderbook with specified depth"""
    result = session.get_orderbook(category="linear", symbol=symbol, limit=limit)
    print(json.dumps(result))

# @cmd Get ticker info
# @option --symbol! Trading pair (e.g., BTCUSDT)
def bybit_get_ticker(symbol):
    """Get 24h ticker information for a symbol"""
    result = session.get_tickers(category="linear", symbol=symbol)
    print(json.dumps(result))

# @cmd Get candlestick data
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --interval! Interval (1, 5, 15, 60, 120, 240, D, W, M)
# @option --limit Number of klines (default: 100)
def bybit_get_klines(symbol, interval, limit=100):
    """Get historical kline/candlestick data"""
    result = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    print(json.dumps(result))

# @cmd Get instrument info
# @option --symbol! Trading pair (e.g., BTCUSDT)
def bybit_get_instrument(symbol):
    """Get instrument specifications (tick size, lot size, etc.)"""
    result = session.get_instruments_info(category="linear", symbol=symbol)
    print(json.dumps(result))

# @cmd Get funding rate
# @option --symbol! Trading pair (e.g., BTCUSDT)
def bybit_get_funding_rate(symbol):
    """Get current funding rate for a symbol"""
    result = session.get_funding_rate(category="linear", symbol=symbol)
    print(json.dumps(result))

# @cmd Get risk limit
# @option --symbol! Trading pair (e.g., BTCUSDT)
def bybit_get_risk_limit(symbol):
    """Get risk limit for a symbol"""
    result = session.get_risk_limit(category="linear", symbol=symbol)
    print(json.dumps(result))

if __name__ == "__main__":
    Argc().run()
