#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
"""
Bybit Advanced Analysis Tools
Native Python implementation (No pandas/pandas_ta dependencies)
"""
import json
import os
import statistics
from datetime import datetime, timezone
from typing import List

from argc import argc as Argc
from pybit.unified_trading import HTTP

# Configuration
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
USE_TOR = os.getenv("USE_TOR", "false").lower() == "true"
TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050")

http_kwargs = {"testnet": TESTNET}
if USE_TOR:
    http_kwargs["proxies"] = {"http": TOR_PROXY, "https": TOR_PROXY}

session = HTTP(**http_kwargs)


def _ema(data: List[float], period: int) -> List[float]:
    if not data:
        return []
    k = 2 / (period + 1)
    ema = [data[0]]
    for val in data[1:]:
        ema.append(val * k + ema[-1] * (1 - k))
    return ema


def _rsi(prices: List[float], period: int = 14) -> List[float]:
    if len(prices) <= period:
        return []
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi = [100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100]

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi.append(100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100)

    return [None] * period + rsi


# @cmd Get market regime
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --interval <TEXT> Interval (default: 60)
# @option --lookback <INT> Number of klines (default: 100)
def bybit_get_market_regime(symbol, interval="60", lookback=100):
    """Classifies market as TRENDING_UP, TRENDING_DOWN, RANGING, or VOLATILE."""
    res = session.get_kline(
        category="linear", symbol=symbol, interval=interval, limit=lookback
    )["result"]["list"]
    if len(res) < 30:
        print(json.dumps({"status": "error", "msg": "Insufficient data"}))
        return

    closes = [float(k[4]) for k in reversed(res)]
    ema_short = _ema(closes, 10)[-1]
    ema_long = _ema(closes, 30)[-1]

    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))
    ]
    volatility = statistics.stdev(returns) * 100

    if volatility > 2.0:
        regime = "VOLATILE"
    elif ema_short > ema_long * 1.002:
        regime = "TRENDING_UP"
    elif ema_short < ema_long * 0.998:
        regime = "TRENDING_DOWN"
    else:
        regime = "RANGING"

    result = {"symbol": symbol, "regime": regime, "volatility": round(volatility, 4)}
    print(json.dumps(result))


# @cmd Get signal confluence
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --intervals <TEXT> Comma-separated intervals (default: 5,15,60,240)
def bybit_get_confluence(symbol, intervals="5,15,60,240"):
    """Analyzes EMA trend and RSI momentum across multiple timeframes."""
    tf_list = [i.strip() for i in intervals.split(",")]
    scores = []
    details = {}

    for tf in tf_list:
        try:
            res = session.get_kline(
                category="linear", symbol=symbol, interval=tf, limit=50
            )
            if res.get("retCode") != 0:
                details[tf] = {"error": res.get("retMsg")}
                continue

            klines = res["result"]["list"]
            closes = [float(k[4]) for k in reversed(klines)]

            ema = _ema(closes, 20)[-1]
            price = closes[-1]
            rsi_list = _rsi(closes, 14)
            rsi = rsi_list[-1] if rsi_list else 50

            trend = 1 if price > ema else -1
            momentum = 1 if rsi > 55 else (-1 if rsi < 45 else 0)

            details[tf] = {
                "rsi": round(rsi, 2),
                "trend": "BULLISH" if trend > 0 else "BEARISH",
                "momentum": momentum,
            }
            scores.append(trend + momentum)
        except Exception as e:
            details[tf] = {"error": str(e)}
            continue

    total_score = sum(scores)
    max_possible = len(tf_list) * 2

    if total_score >= max_possible * 0.7:
        rec = "STRONG_BUY"
    elif total_score > 0:
        rec = "BUY"
    elif total_score <= -max_possible * 0.7:
        rec = "STRONG_SELL"
    elif total_score < 0:
        rec = "SELL"
    else:
        rec = "NEUTRAL"

    result = {
        "symbol": symbol,
        "recommendation": rec,
        "confluence_score": total_score,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(result))


# @cmd Get technical indicators
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --interval! <TEXT> Interval (1, 5, 15, 60, 120, 240, D, W, M)
# @option --limit <INT> Number of klines (default: 100)
def bybit_get_indicators(symbol, interval, limit=100):
    """Calculate RSI, EMA, ATR indicators"""
    res = session.get_kline(
        category="linear", symbol=symbol, interval=interval, limit=limit
    )["result"]["list"]
    closes = [float(k[4]) for k in reversed(res)]
    highs = [float(k[2]) for k in reversed(res)]
    lows = [float(k[3]) for k in reversed(res)]

    rsi = _rsi(closes, 14)[-1]
    ema20 = _ema(closes, 20)[-1]
    ema50 = _ema(closes, 50)[-1]

    # ATR
    tr = [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    ]
    atr = sum(tr[-14:]) / 14 if len(tr) >= 14 else 0

    result = {
        "symbol": symbol,
        "interval": interval,
        "close": closes[-1],
        "rsi": round(rsi, 2) if rsi else None,
        "ema_20": round(ema20, 2),
        "ema_50": round(ema50, 2),
        "atr": round(atr, 4),
    }
    print(json.dumps(result))


# @cmd Multi-timeframe analysis
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --timeframes <TEXT> Comma-separated timeframes (default: 15,60,240,D)
def bybit_analyze_symbol(symbol, timeframes="15,60,240,D"):
    """Analyze symbol across multiple timeframes"""
    analysis = {}
    for tf in timeframes.split(","):
        data = session.get_kline(
            category="linear", symbol=symbol, interval=tf.strip(), limit=50
        )["result"]["list"]
        closes = [float(c[4]) for k in data]  # Wrong indexing in legacy, fixed to [4]
        current, previous = float(data[0][4]), float(data[1][4])
        change_pct = ((current - previous) / previous) * 100
        analysis[tf.strip()] = {
            "trend": "Bullish" if current > previous else "Bearish",
            "change_pct": round(change_pct, 2),
            "close": current,
        }
    print(json.dumps(analysis))


# @cmd Analyze orderbook depth
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --limit <INT> Depth level (default: 25)
def bybit_analyze_orderbook(symbol, limit=25):
    res = session.get_orderbook(category="linear", symbol=symbol, limit=limit)
    bids, asks = res["result"]["b"], res["result"]["a"]
    bid_vol = sum(float(x[1]) for x in bids)
    ask_vol = sum(float(x[1]) for x in asks)
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    result = {
        "symbol": symbol,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": round(((best_ask - best_bid) / best_bid) * 100, 4),
        "bid_vol": round(bid_vol, 2),
        "ask_vol": round(ask_vol, 2),
        "imbalance": round(bid_vol / ask_vol, 2) if ask_vol > 0 else 0,
        "sentiment": "Bullish" if bid_vol > ask_vol else "Bearish",
    }
    print(json.dumps(result))


# @cmd Get volume profile
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --interval <TEXT> Interval (1, 5, 15, 60, 120, 240, D)
# @option --limit <INT> Number of klines (default: 100)
def bybit_get_volume_profile(symbol, interval="60", limit=100):
    data = session.get_kline(
        category="linear", symbol=symbol, interval=interval, limit=limit
    )["result"]["list"]
    vols = [float(k[5]) for k in data]
    closes = [float(k[4]) for k in data]
    typical = [(float(k[2]) + float(k[3]) + float(k[4])) / 3 for k in data]
    vwap = sum(t * v for t, v in zip(typical, vols)) / sum(vols) if sum(vols) > 0 else 0
    print(
        json.dumps(
            {
                "symbol": symbol,
                "vwap": round(vwap, 2),
                "avg_vol": round(statistics.mean(vols), 2),
            }
        )
    )


# @cmd Get support and resistance levels
# @option --symbol! <TEXT> Trading pair (e.g., BTCUSDT)
# @option --interval <TEXT> Interval (default: 60)
# @option --limit <INT> Number of klines (default: 100)
def bybit_get_support_resistance(symbol, interval="60", limit=100):
    data = session.get_kline(
        category="linear", symbol=symbol, interval=interval, limit=limit
    )["result"]["list"]
    highs = [float(k[2]) for k in data]
    lows = [float(k[3]) for k in data]
    res, sup = [], []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            res.append(highs[i])
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            sup.append(lows[i])
    current = float(data[0][4])
    print(
        json.dumps(
            {
                "symbol": symbol,
                "current": current,
                "support": sorted(set([round(s, 2) for s in sup[-5:]])),
                "resistance": sorted(set([round(r, 2) for r in res[-5:]])),
            }
        )
    )


if __name__ == "__main__":
    Argc().run()
