# @describe Get Bybit technical indicators
# @option --symbol <SYMBOL> Symbol
# @option --interval <INTERVAL> Interval
# @option --limit <NUMBER> Limit
#!/usr/bin/env python3
import json
import argparse
import bybit_core

def _ema(data, period):
    if len(data) < period: return None
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def _rsi(data, period=14):
    if len(data) < period + 1: return None
    deltas = [data[i] - data[i-1] for i in range(1, len(data))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def _atr(highs, lows, closes, period=14):
    if len(highs) < period + 1: return None
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(highs))]
    return sum(tr[-period:]) / period

def _bollinger_bands(data, period=20):
    if len(data) < period: return None
    sma = sum(data[-period:]) / period
    variance = sum((x - sma) ** 2 for x in data[-period:]) / period
    std_dev = variance ** 0.5
    return {"upper": sma + (2 * std_dev), "middle": sma, "lower": sma - (2 * std_dev)}

def run(symbol="BTCUSDT", interval="60", limit=100):
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    resp = bybit_core.api_request("GET", "/v5/market/kline", params=params)
    
    if resp.get("retCode") != 0:
        return {"success": False, "error": resp.get("retMsg")}
    
    klines = resp.get("result", {}).get("list", [])
    if not klines: return {"success": False, "error": "No data"}
        
    closes = [float(k[4]) for k in reversed(klines)]
    highs = [float(k[2]) for k in reversed(klines)]
    lows = [float(k[3]) for k in reversed(klines)]
    
    current_price = closes[-1]
    
    rsi14 = _rsi(closes, 14)
    ema9 = _ema(closes, 9)
    ema26 = _ema(closes, 26)
    
    ema12 = _ema(closes, 12); ema26 = _ema(closes, 26); macd_line = (ema12 - ema26) if (ema12 and ema26) else 0
    signal_line = _ema(closes, 9)
    
    return {
        "success": True,
        "symbol": symbol,
        "current_price": current_price,
        "rsi": {"rsi14": rsi14},
        "macd": {"macd_line": macd_line, "signal": signal_line, "histogram": macd_line - signal_line}
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="60")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(run(args.symbol, args.interval, args.limit), indent=2))
