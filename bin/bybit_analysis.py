#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
"""
Bybit Advanced Analysis Tools
Uses pandas and pandas_ta for technical indicators
"""
import pandas as pd
import pandas_ta as ta
import json
import os
from pybit.unified_trading import HTTP
from argc import argc as Argc

# Configuration
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
USE_TOR = os.getenv("USE_TOR", "false").lower() == "true"
TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050")

session = HTTP(
    testnet=TESTNET,
    proxy=TOR_PROXY if USE_TOR else None
)

# @cmd Get technical indicators
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --interval! Interval (1, 5, 15, 60, 120, 240, D, W, M)
# @option --limit Number of klines (default: 100)
def bybit_get_indicators(symbol, interval, limit=100):
    """Calculate RSI, EMA, ATR, MACD, Bollinger Bands indicators"""
    data = session.get_kline(
        category="linear", 
        symbol=symbol, 
        interval=interval, 
        limit=limit
    )["result"]["list"]
    
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
    df = df.astype({
        "open": float, "high": float, "low": float, 
        "close": float, "vol": float
    })
    
    # Calculate indicators
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["ema_9"] = ta.ema(df["close"], length=9)
    df["ema_20"] = ta.ema(df["close"], length=20)
    df["ema_50"] = ta.ema(df["close"], length=50)
    df["ema_200"] = ta.ema(df["close"], length=200)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["macd"], df["macd_signal"], df["macd_hist"] = ta.macd(df["close"])
    df["bb_upper"], df["bb_middle"], df["bb_lower"] = ta.bbands(df["close"], length=20)
    df["stoch_k"], df["stoch_d"] = ta.stoch(df["high"], df["low"], df["close"])
    
    # Get latest values
    latest = df.iloc[-1]
    result = {
        "symbol": symbol,
        "interval": interval,
        "close": round(latest["close"], 2),
        "rsi": round(latest["rsi"], 2) if pd.notna(latest["rsi"]) else None,
        "ema_9": round(latest["ema_9"], 2) if pd.notna(latest["ema_9"]) else None,
        "ema_20": round(latest["ema_20"], 2) if pd.notna(latest["ema_20"]) else None,
        "ema_50": round(latest["ema_50"], 2) if pd.notna(latest["ema_50"]) else None,
        "ema_200": round(latest["ema_200"], 2) if pd.notna(latest["ema_200"]) else None,
        "atr": round(latest["atr"], 2) if pd.notna(latest["atr"]) else None,
        "macd": round(latest["macd"], 4) if pd.notna(latest["macd"]) else None,
        "macd_signal": round(latest["macd_signal"], 4) if pd.notna(latest["macd_signal"]) else None,
        "macd_hist": round(latest["macd_hist"], 4) if pd.notna(latest["macd_hist"]) else None,
        "bb_upper": round(latest["bb_upper"], 2) if pd.notna(latest["bb_upper"]) else None,
        "bb_middle": round(latest["bb_middle"], 2) if pd.notna(latest["bb_middle"]) else None,
        "bb_lower": round(latest["bb_lower"], 2) if pd.notna(latest["bb_lower"]) else None,
        "stoch_k": round(latest["stoch_k"], 2) if pd.notna(latest["stoch_k"]) else None,
        "stoch_d": round(latest["stoch_d"], 2) if pd.notna(latest["stoch_d"]) else None,
    }
    print(json.dumps(result))

# @cmd Multi-timeframe analysis
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --timeframes Comma-separated timeframes (default: 15,60,240,D)
def bybit_analyze_symbol(symbol, timeframes="15,60,240,D"):
    """Analyze symbol across multiple timeframes"""
    analysis = {}
    tf_list = timeframes.split(",")
    
    for tf in tf_list:
        data = session.get_kline(
            category="linear", 
            symbol=symbol, 
            interval=tf.strip(), 
            limit=50
        )["result"]["list"]
        
        closes = [float(c[4]) for c in data]
        volumes = [float(c[5]) for c in data]
        
        current = closes[0]
        previous = closes[1]
        change_pct = ((current - previous) / previous) * 100
        
        # Simple trend detection
        trend = "Bullish" if current > previous else "Bearish"
        
        # Volume analysis
        avg_vol = sum(volumes) / len(volumes)
        vol_ratio = volumes[0] / avg_vol if avg_vol > 0 else 1
        
        analysis[tf.strip()] = {
            "trend": trend,
            "change_pct": round(change_pct, 2),
            "close": current,
            "volume_ratio": round(vol_ratio, 2),
            "signal": "Strong" if abs(change_pct) > 2 else "Weak"
        }
    
    print(json.dumps(analysis))

# @cmd Analyze orderbook depth
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --limit Depth level (default: 25)
def bybit_analyze_orderbook(symbol, limit=25):
    """Analyze orderbook depth, imbalance, and walls"""
    res = session.get_orderbook(
        category="linear", 
        symbol=symbol, 
        limit=limit
    )
    
    bids = res["result"]["b"]
    asks = res["result"]["a"]
    
    bid_vol = sum(float(x[1]) for x in bids)
    ask_vol = sum(float(x[1]) for x in asks)
    
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    spread = best_ask - best_bid
    spread_pct = (spread / best_bid) * 100
    
    # Find large orders (walls) - orders > 10% of total side volume
    bid_walls = [float(x[1]) for x in bids if float(x[1]) > bid_vol * 0.1]
    ask_walls = [float(x[1]) for x in asks if float(x[1]) > ask_vol * 0.1]
    
    # Mid-price volume imbalance
    mid_vol_bid = sum(float(x[1]) for x in bids[:5])
    mid_vol_ask = sum(float(x[1]) for x in asks[:5])
    
    result = {
        "symbol": symbol,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 5),
        "spread_pct": round(spread_pct, 4),
        "bid_vol": round(bid_vol, 2),
        "ask_vol": round(ask_vol, 2),
        "imbalance": round(bid_vol / ask_vol, 2),
        "sentiment": "Bullish" if bid_vol > ask_vol else "Bearish",
        "bid_walls": len(bid_walls),
        "ask_walls": len(ask_walls),
        "mid_bid_vol": round(mid_vol_bid, 2),
        "mid_ask_vol": round(mid_vol_ask, 2),
        "mid_imbalance": round(mid_vol_bid / mid_vol_ask, 2) if mid_vol_ask > 0 else 0
    }
    print(json.dumps(result))

# @cmd Get market depth (full orderbook)
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --limit Depth level (default: 50)
def bybit_get_depth(symbol, limit=50):
    """Get full orderbook depth with aggregated prices"""
    res = session.get_orderbook(
        category="linear", 
        symbol=symbol, 
        limit=limit
    )
    print(json.dumps(res["result"]))

# @cmd Get volume profile
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --interval Interval (1, 5, 15, 60, 120, 240, D)
# @option --limit Number of klines (default: 100)
def bybit_get_volume_profile(symbol, interval="60", limit=100):
    """Calculate volume profile and VWAP"""
    data = session.get_kline(
        category="linear", 
        symbol=symbol, 
        interval=interval, 
        limit=limit
    )["result"]["list"]
    
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
    
    # Calculate VWAP
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (df["typical_price"] * df["vol"]).cumsum() / df["vol"].cumsum()
    
    # Volume weighted average
    vwap = df["vwap"].iloc[-1]
    avg_vol = df["vol"].mean()
    max_vol = df["vol"].max()
    min_vol = df["vol"].min()
    
    result = {
        "symbol": symbol,
        "interval": interval,
        "vwap": round(vwap, 2),
        "avg_volume": round(avg_vol, 2),
        "max_volume": round(max_vol, 2),
        "min_volume": round(min_vol, 2),
        "volume_std": round(df["vol"].std(), 2)
    }
    print(json.dumps(result))

# @cmd Get support and resistance levels
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --interval Interval (default: 60)
# @option --limit Number of klines (default: 100)
def bybit_get_support_resistance(symbol, interval="60", limit=100):
    """Calculate support and resistance levels"""
    data = session.get_kline(
        category="linear", 
        symbol=symbol, 
        interval=interval, 
        limit=limit
    )["result"]["list"]
    
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
    df = df.astype({"high": float, "low": float, "close": float})
    
    # Find local maxima (resistance) and minima (support)
    highs = df["high"].values
    lows = df["low"].values
    
    # Simple pivot points
    resistance = []
    support = []
    
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            resistance.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            support.append(lows[i])
    
    current_price = df["close"].iloc[-1]
    
    # Find nearest levels
    nearest_support = max([s for s in support if s < current_price], default=None)
    nearest_resistance = min([r for r in resistance if r > current_price], default=None)
    
    result = {
        "symbol": symbol,
        "current_price": current_price,
        "nearest_support": round(nearest_support, 2) if nearest_support else None,
        "nearest_resistance": round(nearest_resistance, 2) if nearest_resistance else None,
        "support_levels": sorted(set([round(s, 2) for s in support[-5:]])),
        "resistance_levels": sorted(set([round(r, 2) for r in resistance[-5:]]))
    }
    print(json.dumps(result))

if __name__ == "__main__":
    Argc().run()
