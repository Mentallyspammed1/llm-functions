#!/usr/bin/env python3
# @describe Bybit Advanced Analysis - Technical indicators, orderbook, multi-TF analysis
# @option --symbol!        Trading pair (e.g., BTCUSDT)
# @option --action         Action: indicators|analyze|orderbook|smart_order
# @option --interval       Kline interval: 1|3|5|15|30|60|120|240|D (default: 60)
# @option --limit          Number of candles (default: 100)
# @option --side           Order side for smart_order: Buy|Sell
# @option --qty            Quantity for smart_order
# @option --risk_pct       Risk percentage for smart_order (default: 1.0)
# @option --use_tor       Route through Tor proxy (default: true)
"""
Bybit Advanced Analysis Tools
Technical indicators and orderbook analysis
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

# Try to import pandas_ta, fallback to basic calculations
try:
    import pandas as pd
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False


def bybit_get_indicators(symbol, interval=60, limit=100):
    """Get technical indicators (RSI, EMA, ATR)"""
    try:
        data = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit
        )["result"]["list"]
        
        if PANDAS_TA_AVAILABLE:
            df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
            
            # Calculate indicators
            df["rsi"] = ta.rsi(df["close"], length=14)
            df["ema_20"] = ta.ema(df["close"], length=20)
            df["ema_50"] = ta.ema(df["close"], length=50)
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
            
            # Get latest values
            latest = df.tail(1)
            result = {
                "symbol": symbol,
                "interval": interval,
                "close": float(latest["close"].values[0]),
                "rsi": float(latest["rsi"].values[0]),
                "ema_20": float(latest["ema_20"].values[0]),
                "ema_50": float(latest["ema_50"].values[0]),
                "atr": float(latest["atr"].values[0])
            }
        else:
            # Basic calculation without pandas_ta
            closes = [float(c[4]) for c in data]
            result = {
                "symbol": symbol,
                "interval": interval,
                "close": closes[0],
                "note": "Install pandas_ta for indicators"
            }
        
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_analyze_symbol(symbol):
    """Multi-timeframe analysis (15m, 1h, 4h, 1D)"""
    try:
        results = {}
        for tf, name in [("15", "15m"), ("60", "1h"), ("240", "4h"), ("D", "1D")]:
            data = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=tf,
                limit=20
            )["result"]["list"]
            
            closes = [float(c[4]) for c in data]
            
            if PANDAS_TA_AVAILABLE:
                ema20 = ta.ema(pd.Series(closes), length=20).iloc[-1]
            else:
                ema20 = sum(closes[:20]) / 20  # Simple MA fallback
            
            current_price = closes[0]
            trend = "Bullish" if current_price > ema20 else "Bearish"
            momentum = "Strong" if abs(current_price - ema20) / ema20 > 0.01 else "Weak"
            
            results[name] = {
                "trend": trend,
                "momentum": momentum,
                "price": current_price,
                "ema20": ema20
            }
        
        print(json.dumps(results, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_analyze_orderbook(symbol, limit=25):
    """Analyze orderbook depth and imbalance"""
    try:
        res = session.get_orderbook(category="linear", symbol=symbol, limit=limit)
        
        bids = res["result"]["b"]
        asks = res["result"]["a"]
        
        bid_vol = sum(float(x[1]) for x in bids)
        ask_vol = sum(float(x[1]) for x in asks)
        
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        spread = best_ask - best_bid
        spread_pct = (spread / best_bid) * 100
        
        imbalance = bid_vol / ask_vol if ask_vol > 0 else 0
        
        result = {
            "symbol": symbol,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 5),
            "spread_pct": round(spread_pct, 4),
            "bid_vol": bid_vol,
            "ask_vol": ask_vol,
            "imbalance": round(imbalance, 2),
            "sentiment": "Bullish" if bid_vol > ask_vol else "Bearish"
        }
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_smart_order(symbol, side, qty, risk_pct=1.0):
    """Smart order with auto position sizing based on risk"""
    try:
        # Get current price
        ticker = session.get_tickers(category="linear", symbol=symbol)["result"]["list"][0]
        current_price = float(ticker["lastPrice"])
        
        # Get account balance
        balance = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]
        usdt_balance = float([c for c in balance["coin"] if c["coin"] == "USDT"][0]["walletBalance"])
        
        # Calculate position size
        risk_amount = usdt_balance * (risk_pct / 100)
        
        # Get ATR for stop loss distance
        klines = session.get_kline(category="linear", symbol=symbol, interval="60", limit=20)["result"]["list"]
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        
        if PANDAS_TA_AVAILABLE:
            atr = ta.atr(pd.Series(highs), pd.Series(lows), pd.Series(closes), length=14).iloc[-1]
        else:
            # Simple ATR calculation
            tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
            atr = sum(tr[-14:]) / 14
        
        sl_distance = atr * 2  # 2x ATR for stop loss
        position_size = risk_amount / sl_distance
        
        # Place market order
        result = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(round(position_size, 4))
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def main():
    parser = argparse.ArgumentParser(description="Bybit Analysis Tools")
    parser.add_argument("--action", default="indicators", help="Action to perform")
    parser.add_argument("--symbol", required=True, help="Trading pair")
    parser.add_argument("--interval", default="60", help="Kline interval")
    parser.add_argument("--limit", type=int, default=100, help="Limit")
    parser.add_argument("--side", default="Buy", help="Buy or Sell")
    parser.add_argument("--qty", type=float, default=None, help="Quantity")
    parser.add_argument("--risk_pct", type=float, default=1.0, help="Risk %")
    args = parser.parse_args()

    if args.action == "indicators":
        bybit_get_indicators(args.symbol, args.interval, args.limit)
    elif args.action == "analyze":
        bybit_analyze_symbol(args.symbol)
    elif args.action == "orderbook":
        bybit_analyze_orderbook(args.symbol)
    elif args.action == "smart_order":
        if not args.qty:
            print(json.dumps({"error": "--qty required for smart_order"}))
            return
        bybit_smart_order(args.symbol, args.side, args.qty, args.risk_pct)
    else:
        print(json.dumps({"error": f"Unknown action: {args.action}"}))


if __name__ == "__main__":
    main()
