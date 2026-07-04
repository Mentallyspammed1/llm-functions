#!/usr/bin/env python3
# ==============================================================================
# bybit_micro_scalper_v2.py — Bybit Micro‑Profit Scalper (v4.0)
#
# @describe Bybit Micro‑Profit Scalper – automated micro‑profit scalping using order‑book imbalance and momentum.
# @meta title Bybit Micro‑Profit Scalper (v2)
# @meta description High‑frequency scalping tool optimized for tiny $0.02‑$0.20 net profit targets using order‑book and momentum conditions.
# @option --api-key!               Bybit API key.
# @option --api-secret!            Bybit API secret.
# @option --symbol=BTCUSDT         Target crypto derivative trading pair.
# @option --qty=0.01               Order size/quantity (base asset).
# @option --target-profit=0.05     Desired micro‑profit target in USDT (0.02‑0.20 recommended).
# @option --maker-fee=0.0002       Maker fee tier (default 0.02% = 0.0002).
# @option --trailing-stop=0.001    <Optional> Stop distance as decimal fraction of entry (0.001 = 0.1%).
# @flag --balance-check            Pre‑order USDT balance check (flag).
# @option --max-spread-bps=50      <Optional> Skip if bid‑ask spread exceeds basis points.
# @flag --dry-run                  No order placement (flag).
# @option --mode=rest              Market data: rest | ws.
# @option --ws-timeout=8           One‑shot WS wait (non‑loop or first connect).
# @flag --testnet                  Testnet hosts (flag).
# @option --loop                   Continuous daemon until SIGINT/SIGTERM (flag).
# @option --loop-interval=2.0      Seconds between evaluation cycles.
# @option --cooldown=30            Seconds after successful order before another entry.
# @option --max-iterations=0       Stop after N iterations (0 = unlimited).
# @option --private-ws             Private order/execution WS logs (flag).
# @option --position-guard         Skip new entry if open position exists on symbol (flag).
# @flag --use-trading-stop-tp      Use native TP via trading stop (flag).
# @option --ws-fallback-rest       On WS failure, use REST for that cycle (flag).
# ==============================================================================

import sys
import math
from statistics import stdev
from collections import deque
import time
import signal
import hmac
import hashlib
import json
import threading
import requests
from typing import Any, Callable, Dict, List, Optional, Tuple
import logging
import os
from pathlib import Path

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
    env_path = Path(__file__).with_name('.env')
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT   = 10
MARKET_RETRIES    = 3
RECV_WINDOW       = 30000
DEFAULT_TAKER_FEE = 0.00055

MOMENTUM_LONG   =  0.0001
MOMENTUM_SHORT  = -0.0001
IMBALANCE_LONG  =  0.05
IMBALANCE_SHORT = -0.05

MICRO_PROFIT_TIERS  = [0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
MAX_TP_DISTANCE_PCT = 0.05

# Circuit breaker: pause after this many consecutive fetch/order failures
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_PAUSE_SEC = 60.0

# WS reconnect
WS_MAX_RECONNECTS    = 10
WS_RECONNECT_DELAY   = 3.0

# Server time re-sync interval (iterations)
SERVER_TIME_SYNC_INTERVAL = 100

_SHUTDOWN = threading.Event()


def _handle_signal(_signum, _frame) -> None:
    _SHUTDOWN.set()


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

def get_proxies() -> Optional[Dict[str, str]]:
    proxy = os.getenv("BYBIT_TOR_PROXY")
    if not proxy and os.getenv("TOR_ENABLED") == "true":
        proxy = f"socks5://127.0.0.1:{os.getenv('TOR_SOCKS_PORT', '9050')}"
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


def get_ws_run_options() -> Dict[str, Any]:
    from urllib.parse import urlparse
    proxy = os.getenv("BYBIT_TOR_PROXY")
    if not proxy and os.getenv("TOR_ENABLED") == "true":
        proxy = f"socks5://127.0.0.1:{os.getenv('TOR_SOCKS_PORT', '9050')}"
    if not proxy:
        return {}
    try:
        parsed = urlparse(proxy)
        opts: Dict[str, Any] = {
            "http_proxy_host": parsed.hostname,
            "http_proxy_port": parsed.port,
        }
        opts["proxy_type"] = "socks5" if parsed.scheme.startswith("socks") else "http"
        return opts
    except Exception as exc:
        logger.warning("Failed to parse proxy URL %s: %s", proxy, exc)
        return {}


# ---------------------------------------------------------------------------
# URL routing
# ---------------------------------------------------------------------------

def base_urls(testnet: bool) -> Dict[str, str]:
    if testnet:
        return {
            "rest": "https://api-testnet.bybit.com",
            "ws_public_linear": "wss://stream-testnet.bybit.com/v5/public/linear",
            "ws_private": "wss://stream-testnet.bybit.com/v5/private",
        }
    return {
        "rest": "https://api.bybit.com",
        "ws_public_linear": "wss://stream.bybit.com/v5/public/linear",
        "ws_private": "wss://stream.bybit.com/v5/private",
    }


DEFAULT_LEVERAGE = 1.0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_argv(argv: List[str]) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        raw = argv[i]
        if not raw.startswith("--"):
            i += 1
            continue
        if "=" in raw:
            k, v = raw.split("=", 1)
            args[k[2:].replace("-", "_")] = v
            i += 1
            continue
        key = raw[2:].replace("-", "_")
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if nxt and not nxt.startswith("--"):
            args[key] = nxt
            i += 2
        else:
            args[key] = True
            i += 1
    return args


def emit(obj: Dict[str, Any]) -> None:
    """Write JSON output to stdout — sole output channel for downstream consumers."""
    print(json.dumps(obj, indent=2), flush=True)


# ---------------------------------------------------------------------------
# Rate limiter — simple token bucket to avoid Bybit IP bans
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket rate limiter for REST API calls."""

    def __init__(self, calls_per_second: float = 5.0) -> None:
        self._interval  = 1.0 / max(calls_per_second, 0.1)
        self._last_call = 0.0
        self._lock      = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now     = time.time()
            elapsed = now - self._last_call
            wait    = self._interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()


_rate_limiter = RateLimiter(calls_per_second=8.0)

# ---------------------------------------------------------------------------
# Shared persistent HTTP session — connection pooling
# ---------------------------------------------------------------------------
_http_session: Optional[requests.Session] = None
_http_session_lock = threading.Lock()


def _get_http_session() -> requests.Session:
    """Return a module-level persistent requests.Session (thread-safe init)."""
    global _http_session
    if _http_session is None:
        with _http_session_lock:
            if _http_session is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=4,
                    pool_maxsize=8,
                    max_retries=0,          # Retry logic handled manually
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                proxies = get_proxies()
                if proxies:
                    s.proxies.update(proxies)
                _http_session = s
    return _http_session


# ---------------------------------------------------------------------------
# Authentication & signed requests
# ---------------------------------------------------------------------------

def generate_signature(secret: str, timestamp: int, api_key: str, recv_window: int, payload: str) -> str:
    param_str = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(
        secret.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


_SERVER_TIME_OFFSET_MS: Optional[int] = None
_SERVER_TIME_LOCK = threading.Lock()


def server_time_ms(base_url: str) -> int:
    """Return local clock adjusted by cached Bybit server offset."""
    global _SERVER_TIME_OFFSET_MS
    with _SERVER_TIME_LOCK:
        offset = _SERVER_TIME_OFFSET_MS
    if offset is None:
        sync_server_time(base_url)
        with _SERVER_TIME_LOCK:
            offset = _SERVER_TIME_OFFSET_MS or 0
    return int(time.time() * 1000) + offset


def sync_server_time(base_url: str) -> None:
    """Fetch Bybit server time and cache the local-clock offset."""
    global _SERVER_TIME_OFFSET_MS
    try:
        session = _get_http_session()
        r = session.get(f"{base_url}/v5/market/time", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json().get("result", {})
        if "timeNano" in data:
            server_ms = int(int(data["timeNano"]) / 1_000_000)
        else:
            server_ms = int(data.get("timeSecond", int(time.time()))) * 1000
        local_ms  = int(time.time() * 1000)
        with _SERVER_TIME_LOCK:
            _SERVER_TIME_OFFSET_MS = server_ms - local_ms
        logger.info("Bybit time sync offset_ms=%s", _SERVER_TIME_OFFSET_MS)
    except Exception as exc:
        logger.warning("Bybit time sync failed, using local clock: %s", exc)
        with _SERVER_TIME_LOCK:
            if _SERVER_TIME_OFFSET_MS is None:
                _SERVER_TIME_OFFSET_MS = 0


def send_signed_post(
    base_url: str,
    endpoint: str,
    payload_dict: Dict[str, Any],
    api_key: str,
    api_secret: str,
    use_server_time: bool = True,
) -> Dict[str, Any]:
    _rate_limiter.acquire()
    timestamp    = server_time_ms(base_url) if use_server_time else int(time.time() * 1000)
    payload_json = json.dumps(payload_dict, separators=(",", ":"))
    signature    = generate_signature(api_secret, timestamp, api_key, RECV_WINDOW, payload_json)
    headers = {
        "X-BAPI-API-KEY":      api_key,
        "X-BAPI-SIGN":         signature,
        "X-BAPI-TIMESTAMP":    str(timestamp),
        "X-BAPI-RECV-WINDOW":  str(RECV_WINDOW),
        "Content-Type":        "application/json",
    }
    session = _get_http_session()
    r = session.post(
        base_url + endpoint,
        headers=headers,
        data=payload_json,
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def send_signed_get(
    base_url: str,
    endpoint: str,
    query: Dict[str, Any],
    api_key: str,
    api_secret: str,
    use_server_time: bool = True,
) -> Dict[str, Any]:
    _rate_limiter.acquire()
    timestamp = server_time_ms(base_url) if use_server_time else int(time.time() * 1000)
    qs        = "&".join(f"{k}={query[k]}" for k in sorted(query.keys()))
    signature = generate_signature(api_secret, timestamp, api_key, RECV_WINDOW, qs)
    headers   = {
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-SIGN":        signature,
        "X-BAPI-TIMESTAMP":   str(timestamp),
        "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
    }
    url     = f"{base_url}{endpoint}?{qs}" if qs else f"{base_url}{endpoint}"
    session = _get_http_session()
    r       = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def ws_auth_message(api_key: str, api_secret: str) -> str:
    expires = int((time.time() + 10) * 1000)
    sign    = hmac.new(
        api_secret.encode("utf-8"),
        f"GET/realtime{expires}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps({"op": "auth", "args": [api_key, expires, sign]})


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Pause execution after too many consecutive failures."""

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, pause_sec: float = CIRCUIT_BREAKER_PAUSE_SEC) -> None:
        self.threshold  = threshold
        self.pause_sec  = pause_sec
        self._failures  = 0
        self._open_until = 0.0
        self._lock       = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = time.time() + self.pause_sec
                logger.warning(
                    "Circuit breaker OPEN after %d failures — pausing %.0fs",
                    self._failures, self.pause_sec,
                )
                self._failures = 0

    def is_open(self) -> bool:
        with self._lock:
            return time.time() < self._open_until

    def seconds_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - time.time())


_circuit_breaker = CircuitBreaker()

# ---------------------------------------------------------------------------
# Session-level PnL tracker
# ---------------------------------------------------------------------------

class SessionPnL:
    """Thread-safe running totals for session profit/loss accounting."""

    def __init__(self) -> None:
        self._lock           = threading.Lock()
        self.gross_usdt      = 0.0
        self.fees_usdt       = 0.0
        self.net_usdt        = 0.0
        self.trade_count     = 0
        self.win_count       = 0
        self.loss_count      = 0
        self.session_start   = time.time()

    def record(self, net_pnl: float, fees: float) -> None:
        with self._lock:
            self.net_usdt  += net_pnl
            self.fees_usdt += fees
            self.trade_count += 1
            if net_pnl >= 0:
                self.win_count += 1
            else:
                self.loss_count += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = time.time() - self.session_start
            win_rate = self.win_count / self.trade_count if self.trade_count > 0 else 0.0
            return {
                "session_net_usdt":  round(self.net_usdt, 6),
                "session_fees_usdt": round(self.fees_usdt, 6),
                "trade_count":       self.trade_count,
                "win_rate":          round(win_rate, 4),
                "session_elapsed_s": round(elapsed, 1),
            }


_session_pnl = SessionPnL()

# ---------------------------------------------------------------------------
# Market data helpers
# ---------------------------------------------------------------------------

def http_get_json(url: str, session: requests.Session) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(MARKET_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("retCode", 0) != 0:
                raise RuntimeError(data.get("retMsg", "retCode != 0"))
            return data
        except Exception as exc:
            last_err = exc
            backoff  = 0.25 * (2 ** attempt)   # Exponential backoff
            time.sleep(backoff)
    raise RuntimeError(f"GET failed after {MARKET_RETRIES} attempts: {last_err}")


def parse_orderbook_side(side: List[List[str]]) -> Tuple[float, float]:
    if not side:
        return 0.0, 0.0
    return float(side[0][0]), sum(float(level[1]) for level in side)


def kline_close(row: Any) -> float:
    if isinstance(row, (list, tuple)):
        return float(row[4])
    if isinstance(row, dict):
        return float(row.get("close", row.get("c", 0)))
    raise TypeError(f"Unexpected kline row type: {type(row)}")


def fetch_tick_size(base_url: str, symbol: str, tick_cache: Dict[str, float], api_key: str = "", api_secret: str = "") -> float:
    """Fetch instrument tick size with credential fallback to env vars."""
    if symbol in tick_cache:
        return tick_cache[symbol]

    key    = api_key    or os.getenv("BYBIT_API_KEY", "")
    secret = api_secret or os.getenv("BYBIT_API_SECRET", "")
    if not key or not secret:
        logger.warning("API credentials missing for tick size fetch — using default 0.01")
        tick_cache[symbol] = 0.01
        return 0.01

    try:
        resp      = send_signed_get(
            base_url, "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol}, key, secret,
        )
        inst_list = resp.get("result", {}).get("list", [])
        if inst_list:
            tick_cache[symbol] = float(inst_list[0]["priceFilter"]["tickSize"])
            logger.info("Tick size for %s: %s", symbol, tick_cache[symbol])
            return tick_cache[symbol]
    except Exception as exc:
        logger.warning("Failed to fetch instrument info for %s: %s", symbol, exc)

    logger.warning("Using default tick size 0.01 for %s", symbol)
    tick_cache[symbol] = 0.01
    return 0.01


def build_market_snapshot(
    best_bid: float,
    best_ask: float,
    bid_vol: float,
    ask_vol: float,
    closes: List[float],
    tick_size: float,
    source: str,
) -> Dict[str, Any]:
    if best_bid <= 0 or best_ask <= 0:
        raise RuntimeError(f"Invalid top of book: bid={best_bid} ask={best_ask}")
    if best_ask < best_bid:
        logger.warning("Crossed book detected: ask=%.6f < bid=%.6f", best_ask, best_bid)

    denominator  = bid_vol + ask_vol
    imbalance    = (bid_vol - ask_vol) / denominator if denominator > 0 else 0.0
    mid          = (best_bid + best_ask) / 2.0
    spread_bps   = ((best_ask - best_bid) / mid) * 10_000.0 if mid > 0 else 0.0
    momentum     = 0.0
    if len(closes) >= 2 and closes[-2] > 0:
        momentum = (closes[-1] - closes[-2]) / closes[-2]

    return {
        "best_bid":   best_bid,
        "best_ask":   best_ask,
        "bid_vol":    bid_vol,
        "ask_vol":    ask_vol,
        "imbalance":  imbalance,
        "momentum":   momentum,
        "tick_size":  tick_size,
        "spread_bps": spread_bps,
        "source":     source,
        "closes":     list(closes),
    }


# ---------------------------------------------------------------------------
# Multi-timeframe analyzer
# ---------------------------------------------------------------------------

class MultiTimeframeAnalyzer:
    """Track price across 1m / 5m / 15m buckets for trend confirmation."""

    def __init__(self) -> None:
        # Each deque entry: (bucket_key, close_price)
        self.timeframes: Dict[str, deque] = {
            "1m":  deque(maxlen=60),
            "5m":  deque(maxlen=12),
            "15m": deque(maxlen=4),
        }
        self._bucket_minutes = {"1m": 1, "5m": 5, "15m": 15}
        self._lock = threading.Lock()

    def update(self, close_price: float, timestamp: float) -> None:
        current_minute = int(timestamp / 60)
        with self._lock:
            for tf_name, bucket_minutes in self._bucket_minutes.items():
                bucket = current_minute // bucket_minutes
                dq     = self.timeframes[tf_name]
                if dq and dq[-1][0] == bucket:
                    # Replace last entry (update current candle close)
                    dq[-1] = (bucket, close_price)
                else:
                    dq.append((bucket, close_price))

    def get_trend_signal(self) -> Tuple[float, str]:
        scores: List[float] = []
        with self._lock:
            for tf_name, data in self.timeframes.items():
                prices = [p[1] for p in data]
                if len(prices) < 2:
                    continue
                sma       = sum(prices) / len(prices)
                tf_score  = (prices[-1] - sma) / sma if sma != 0 else 0.0
                scores.append(tf_score)

        if not scores:
            return 0.0, "neutral"

        avg_score = sum(scores) / len(scores)
        if avg_score > 0.005:
            return min(1.0, avg_score * 100), "bullish"
        if avg_score < -0.005:
            return min(1.0, abs(avg_score) * 100), "bearish"
        return 0.0, "neutral"


# ---------------------------------------------------------------------------
# Micro-pattern analyzer
# ---------------------------------------------------------------------------

class MicroPatternAnalyzer:
    """Detect micro-patterns in spread and price acceleration."""

    def __init__(self, window_size: int = 10) -> None:
        self._bid_asks:       deque = deque(maxlen=window_size)
        self._price_movements: deque = deque(maxlen=window_size)
        self._lock = threading.Lock()

    def update(self, best_bid: float, best_ask: float) -> None:
        with self._lock:
            entry = {
                "bid":       best_bid,
                "ask":       best_ask,
                "spread":    best_ask - best_bid,
                "mid":       (best_bid + best_ask) / 2.0,
                "timestamp": time.time(),
            }
            if self._bid_asks:
                self._price_movements.append(entry["mid"] - self._bid_asks[-1]["mid"])
            self._bid_asks.append(entry)

    def detect_acceleration(self) -> float:
        with self._lock:
            movements = list(self._price_movements)
        if len(movements) < 4:
            return 0.0
        recent_avg = sum(movements[-3:]) / 3
        older      = movements[:-3]
        older_avg  = sum(older) / len(older) if older else recent_avg
        return recent_avg - older_avg

    def detect_spread_compression(self) -> bool:
        with self._lock:
            spreads = [ba["spread"] for ba in list(self._bid_asks)[-5:]]
        if len(spreads) < 5:
            return False
        recent_avg = sum(spreads[-2:]) / 2
        older_avg  = sum(spreads[:3]) / 3
        return recent_avg < older_avg * 0.90

    def get_entry_signal(self) -> Dict[str, Any]:
        acceleration     = self.detect_acceleration()
        spread_compress  = self.detect_spread_compression()
        signal_strength  = 0.0
        if abs(acceleration) > 0.0001:
            signal_strength += min(0.5, abs(acceleration) * 100)
        if spread_compress:
            signal_strength += 0.3
        return {
            "acceleration":       acceleration,
            "spread_compression": spread_compress,
            "signal_strength":    signal_strength,
            "direction":          "buy" if acceleration > 0 else "sell" if acceleration < 0 else "neutral",
        }


# ---------------------------------------------------------------------------
# Volatility helper
# ---------------------------------------------------------------------------

def _realized_volatility(returns: List[float]) -> float:
    if len(returns) >= 2:
        return stdev(returns)
    if len(returns) == 1:
        return abs(returns[0])
    return 0.001


def parse_trailing_stop(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if raw is True:
        return 0.001
    if isinstance(raw, bool):
        return None
    s = str(raw).strip().lower()
    if not s or s in ("false",):
        return None
    if s == "true":
        return 0.001
    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dynamic quantity sizing
# ---------------------------------------------------------------------------

def calculate_optimal_qty(
    market: Dict[str, Any],
    base_qty: float,
    target_profit: float,
    volatility_window: int = 20,
    max_position_risk: float = 0.02,
    account_balance: float  = 1000.0,
    session_win_rate: float = 0.55,         # Updated from live session stats
    session_avg_rr: float   = 1.2,
) -> float:
    closes = market.get("closes", [])
    if len(closes) < volatility_window:
        return base_qty

    window  = closes[-volatility_window:]
    returns = [
        (window[i] - window[i - 1]) / window[i - 1]
        for i in range(1, len(window))
        if window[i - 1] != 0
    ]
    vol = _realized_volatility(returns)

    if vol < 0.0005:
        vol_multiplier = 1.5
    elif vol < 0.001:
        vol_multiplier = 1.2
    elif vol < 0.002:
        vol_multiplier = 1.0
    else:
        vol_multiplier = 0.7

    if target_profit <= 0.03:
        profit_multiplier = 1.5
    elif target_profit <= 0.05:
        profit_multiplier = 1.3
    elif target_profit <= 0.10:
        profit_multiplier = 1.0
    else:
        profit_multiplier = 0.8

    # Kelly sizing from live session win rate (not hardcoded)
    kelly = (session_win_rate * session_avg_rr - (1.0 - session_win_rate)) / session_avg_rr
    kelly = max(0.10, min(0.25, kelly))

    optimal_qty   = base_qty * vol_multiplier * profit_multiplier * (1.0 + kelly)
    entry_price   = (market["best_bid"] + market["best_ask"]) / 2.0
    position_val  = optimal_qty * entry_price
    max_allowed   = account_balance * max_position_risk

    if position_val > max_allowed and entry_price > 0:
        optimal_qty = max_allowed / entry_price

    return round(max(optimal_qty, base_qty), 8)


# ---------------------------------------------------------------------------
# Adaptive momentum thresholds
# ---------------------------------------------------------------------------

def calculate_adaptive_momentum_thresholds(closes: List[float], lookback: int = 20) -> Tuple[float, float]:
    if len(closes) < lookback:
        return 0.00005, -0.00005

    window  = closes[-lookback:]
    changes = [
        abs((window[i] - window[i - 1]) / window[i - 1])
        for i in range(1, len(window))
        if window[i - 1] != 0
    ]
    if not changes:
        return 0.00005, -0.00005

    avg_change   = sum(changes) / len(changes)
    vol_mult     = max(0.2, min(2.0, avg_change / 0.001))
    long_thresh  =  0.00005 * vol_mult
    short_thresh = -0.00005 * vol_mult
    return long_thresh, short_thresh


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------

def evaluate_signal_v2(
    market: Dict[str, Any],
    qty: float,
    target_profit: float,
    maker_fee: float,
    volume_profile: Optional[Dict[str, Any]] = None,
    position_info: Optional[Dict[str, Any]]  = None,
) -> Optional[Dict[str, Any]]:
    """Enhanced signal with adaptive thresholds and OBI fallback."""
    best_bid   = market["best_bid"]
    best_ask   = market["best_ask"]
    imbalance  = market["imbalance"]
    momentum   = market["momentum"]
    tick_size  = market["tick_size"]
    closes     = market.get("closes", [])

    mom_long, mom_short = calculate_adaptive_momentum_thresholds(closes)

    volume_multiplier = 1.0
    if volume_profile:
        avg_vol = volume_profile.get("avg_volume", 1)
        cur_vol = volume_profile.get("current_volume", 1)
        if avg_vol > 0:
            ratio = cur_vol / avg_vol
            volume_multiplier = max(0.5, min(1.5, ratio))

    mom_long_adj  = mom_long  * volume_multiplier
    mom_short_adj = mom_short * volume_multiplier
    imb_long      =  0.01 * volume_multiplier
    imb_short     = -0.01 * volume_multiplier

    spread_ok = market.get("spread_bps", 100.0) < 30.0

    buy_conditions  = momentum > mom_long_adj  and imbalance > imb_long  and spread_ok
    sell_conditions = momentum < mom_short_adj and imbalance < imb_short and spread_ok
    buy_obi_only    = spread_ok and imbalance >  0.60 and abs(momentum) < abs(mom_long_adj)  * 2
    sell_obi_only   = spread_ok and imbalance < -0.60 and abs(momentum) < abs(mom_short_adj) * 2

    # Determine cost basis for TP calculation accounting for existing positions
    def _base_entry_for_tp(side: str, raw_entry: float) -> float:
        if position_info and position_info.get("size", 0) > 0:
            pos_size  = position_info["size"]
            pos_entry = position_info["entry_price"]
            pos_side  = position_info.get("side", "")
            if pos_side == side:
                return (pos_size * pos_entry + qty * raw_entry) / (pos_size + qty)
            return pos_entry
        return raw_entry

    def _try_tp(raw_entry: float, side: str) -> Optional[float]:
        base = _base_entry_for_tp(side, raw_entry)
        return solve_tp_with_floor(
            base, side, qty, maker_fee,
            min_net=max(0.02, target_profit * 0.95),
            market=market,
            tick_size=tick_size,
        )

    side:        Optional[str] = None
    entry_price: float         = 0.0
    exit_price:  float         = 0.0
    signal_strength: float     = 0.0

    if buy_conditions:
        side        = "Buy"
        entry_price = best_bid
        tp          = _try_tp(entry_price, side)
        if tp is None:
            return None
        exit_price      = tp
        denom_m         = max(abs(mom_long_adj), 1e-10)
        denom_i         = max(abs(imb_long), 1e-10)
        signal_strength = abs(momentum) / denom_m * 0.4 + abs(imbalance) / denom_i * 0.4 + 0.2

    elif sell_conditions:
        side        = "Sell"
        entry_price = best_ask
        tp          = _try_tp(entry_price, side)
        if tp is None:
            return None
        exit_price      = tp
        denom_m         = max(abs(mom_short_adj), 1e-10)
        denom_i         = max(abs(imb_short), 1e-10)
        signal_strength = abs(momentum) / denom_m * 0.4 + abs(imbalance) / denom_i * 0.4 + 0.2

    elif buy_obi_only:
        side        = "Buy"
        entry_price = best_bid
        tp          = _try_tp(entry_price, side)
        if tp is None:
            return None
        exit_price      = tp
        signal_strength = 0.35 + abs(imbalance) * 0.3

    elif sell_obi_only:
        side        = "Sell"
        entry_price = best_ask
        tp          = _try_tp(entry_price, side)
        if tp is None:
            return None
        exit_price      = tp
        signal_strength = 0.35 + abs(imbalance) * 0.3

    if not side:
        return None

    confidence = min(1.0, signal_strength)
    if confidence < 0.25:
        return None

    return {
        "side":             side,
        "entry_price":      entry_price,
        "exit_price":       exit_price,
        "momentum":         momentum,
        "imbalance":        imbalance,
        "tick_size":        tick_size,
        "confidence":       confidence,
        "volume_multiplier": volume_multiplier,
    }


# ---------------------------------------------------------------------------
# TP math
# ---------------------------------------------------------------------------

def net_profit_to_tp(entry_price: float, side: str, net_target: float, qty: float, maker_fee: float) -> float:
    """Closed-form TP price for exact net target after maker round-trip fees."""
    if side == "Buy":
        denom = qty * (1.0 - maker_fee)
        if denom <= 0:
            raise ValueError("Invalid qty/fee for Buy TP solve")
        return (net_target + entry_price * qty * (1.0 + maker_fee)) / denom
    else:
        denom = qty * (1.0 + maker_fee)
        if denom <= 0:
            raise ValueError("Invalid qty/fee for Sell TP solve")
        return (entry_price * qty * (1.0 - maker_fee) - net_target) / denom


def net_profit_to_tp_advanced(
    entry_price: float,
    side: str,
    net_target: float,
    qty: float,
    maker_fee: float,
    market_conditions: Optional[Dict[str, Any]] = None,
) -> float:
    """TP with optional market-condition dynamic adjustment."""
    base_tp = net_profit_to_tp(entry_price, side, net_target, qty, maker_fee)
    if not market_conditions:
        return base_tp

    imbalance   = market_conditions.get("imbalance", 0.0)
    spread_bps  = market_conditions.get("spread_bps", 10.0)
    adj_factor  = 1.0

    if side == "Buy"  and imbalance > 0.1:
        adj_factor = 1.1
    elif side == "Sell" and imbalance < -0.1:
        adj_factor = 1.1

    if spread_bps < 5:
        adj_factor *= 1.05

    min_distance = entry_price * 0.0002
    adjusted_tp  = entry_price + (base_tp - entry_price) * adj_factor

    if side == "Buy":
        adjusted_tp = max(adjusted_tp, entry_price + min_distance)
    else:
        adjusted_tp = min(adjusted_tp, entry_price - min_distance)

    return adjusted_tp


def optimize_take_profit(
    market: Dict[str, Any],
    entry_price: float,
    side: str,
    target_profit: float,
    qty: float,
    maker_fee: float,
    position_info: Optional[Dict[str, Any]] = None,
) -> float:
    base_entry = entry_price
    if position_info and position_info.get("size", 0) > 0:
        pos_size  = position_info["size"]
        pos_entry = position_info["entry_price"]
        pos_side  = position_info.get("side", "")
        if pos_side == side:
            base_entry = (pos_size * pos_entry + qty * entry_price) / (pos_size + qty)
        else:
            base_entry = pos_entry

    tiers = sorted(set(MICRO_PROFIT_TIERS + [target_profit]))
    tiers = [t for t in tiers if 0.02 <= t <= 0.20] or [target_profit]

    for net_tgt in tiers:
        try:
            tp       = net_profit_to_tp_advanced(base_entry, side, net_tgt, qty, maker_fee, market)
            dist_pct = abs(tp - entry_price) / entry_price
            if side == "Buy"  and tp <= base_entry:
                continue
            if side == "Sell" and tp >= base_entry:
                continue
            if dist_pct > MAX_TP_DISTANCE_PCT:
                continue
            return tp
        except Exception:
            continue

    try:
        return net_profit_to_tp_advanced(base_entry, side, target_profit, qty, maker_fee, market)
    except Exception:
        delta = target_profit / (base_entry * qty) if base_entry * qty > 0 else 0.0
        return base_entry * (1.0 + delta) if side == "Buy" else base_entry * (1.0 - delta)


def calculate_adaptive_stop_loss(
    entry_price: float,
    side: str,
    market_volatility: float,
    tick_size: float,
    base_stop_distance: float = 0.002,
) -> Tuple[float, float]:
    if market_volatility > 0.005:
        vol_mult = 1.5
    elif market_volatility < 0.001:
        vol_mult = 0.7
    else:
        vol_mult = 1.0

    adjusted_distance = base_stop_distance * vol_mult
    if side == "Buy":
        sl = round_to_tick(entry_price * (1.0 - adjusted_distance), tick_size)
    else:
        sl = round_to_tick(entry_price * (1.0 + adjusted_distance), tick_size)
    return sl, adjusted_distance


# ---------------------------------------------------------------------------
# Market data: REST
# ---------------------------------------------------------------------------

def get_market_data_rest(
    base_url: str,
    symbol: str,
    tick_cache: Dict[str, float],
    api_key: str,
    api_secret: str,
) -> Dict[str, Any]:
    """Fetch order book + klines via signed REST; uses persistent session."""
    ob_resp = send_signed_get(
        base_url, "/v5/market/orderbook",
        {"category": "linear", "symbol": symbol, "limit": 5},
        api_key, api_secret,
    )
    ob       = ob_resp.get("result", {})
    best_bid, bid_vol = parse_orderbook_side(ob.get("b", []))
    best_ask, ask_vol = parse_orderbook_side(ob.get("a", []))

    klines = send_signed_get(
        base_url, "/v5/market/kline",
        {"category": "linear", "symbol": symbol, "interval": "1", "limit": 5},
        api_key, api_secret,
    )["result"]["list"]

    if len(klines) < 2:
        raise RuntimeError("Insufficient kline data")

    closes   = [kline_close(k) for k in reversed(klines)]
    tick_sz  = fetch_tick_size(base_url, symbol, tick_cache, api_key, api_secret)
    snapshot = build_market_snapshot(best_bid, best_ask, bid_vol, ask_vol, closes, tick_sz, "rest")
    _circuit_breaker.record_success()
    return snapshot


# ---------------------------------------------------------------------------
# Market data: WebSocket one-shot
# ---------------------------------------------------------------------------

def get_market_data_ws_oneshot(
    ws_url: str,
    base_url: str,
    symbol: str,
    timeout_sec: float,
    tick_cache: Dict[str, float],
    api_key: str,
    api_secret: str,
) -> Dict[str, Any]:
    try:
        import websocket
    except ImportError:
        raise RuntimeError("mode=ws requires: pip install websocket-client")

    state: Dict[str, Any] = {
        "best_bid": 0.0, "best_ask": 0.0,
        "bid_vol":  0.0, "ask_vol":  0.0,
        "closes": [], "error": None,
        "last_kline_start": None,
    }
    done = threading.Event()

    def _is_complete() -> bool:
        return state["best_bid"] > 0 and state["best_ask"] > 0 and len(state["closes"]) >= 1

    def on_message(_ws, message: str) -> None:
        try:
            msg   = json.loads(message)
            if msg.get("op") == "subscribe" and msg.get("success") is False:
                state["error"] = msg.get("ret_msg", "subscribe failed")
                done.set()
                return
            topic = msg.get("topic", "")
            data  = msg.get("data")
            if not data:
                return
            if topic.startswith("orderbook"):
                d = data if isinstance(data, dict) else {}
                bids = d.get("b", [])
                asks = d.get("a", [])
                if bids:
                    state["best_bid"] = float(bids[0][0])
                    state["bid_vol"]  = float(bids[0][1])
                if asks:
                    state["best_ask"] = float(asks[0][0])
                    state["ask_vol"]  = float(asks[0][1])
            elif topic.startswith("kline"):
                rows = data if isinstance(data, list) else [data]
                for row in rows:
                    c          = kline_close(row)
                    start_time = row[0] if isinstance(row, (list, tuple)) else row.get("start", 0)
                    if state["last_kline_start"] == start_time and state["closes"]:
                        state["closes"][-1] = c
                    else:
                        state["closes"].append(c)
                        state["last_kline_start"] = start_time
                        if len(state["closes"]) > 30:
                            state["closes"].pop(0)
            if _is_complete():
                done.set()
        except Exception as exc:
            state["error"] = str(exc)
            done.set()

    def on_open(ws) -> None:
        ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.1.{symbol}", f"kline.1.{symbol}"]}))

    ws_app  = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
    run_opts: Dict[str, Any] = {"ping_interval": 20, "ping_timeout": 10}
    run_opts.update(get_ws_run_options())
    thread = threading.Thread(target=lambda: ws_app.run_forever(**run_opts), daemon=True)
    thread.start()
    done.wait(timeout=timeout_sec)
    ws_app.close()

    if state["error"]:
        raise RuntimeError(state["error"])
    if not _is_complete():
        raise RuntimeError(f"WS market data timeout after {timeout_sec}s: {state}")

    for attempt in range(3):
        try:
            tick_size = fetch_tick_size(base_url, symbol, tick_cache, api_key, api_secret)
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))

    snapshot = build_market_snapshot(
        state["best_bid"], state["best_ask"],
        state["bid_vol"],  state["ask_vol"],
        state["closes"],   tick_size, "ws",
    )
    _circuit_breaker.record_success()
    return snapshot


# ---------------------------------------------------------------------------
# Persistent WebSocket feed
# ---------------------------------------------------------------------------

class PersistentPublicLinearFeed:
    """Daemon thread maintaining a live WS connection with auto-reconnect."""

    def __init__(self, ws_url: str, symbol: str, seed_closes: Optional[List[float]] = None) -> None:
        self.ws_url         = ws_url
        self.symbol         = symbol
        self._lock          = threading.Lock()
        self._best_bid      = 0.0
        self._best_ask      = 0.0
        self._bid_vol       = 0.0
        self._ask_vol       = 0.0
        self._closes:  List[float]  = list(seed_closes) if seed_closes else []
        self._last_kline_start: Optional[Any] = None
        self._ready         = False
        self._error: Optional[str] = None
        self._ws_app        = None
        self._thread: Optional[threading.Thread] = None
        self._reconnects    = 0
        self._stop_event    = threading.Event()

    def _on_message(self, _ws, message: str) -> None:
        try:
            msg   = json.loads(message)
            if msg.get("op") == "subscribe" and msg.get("success") is False:
                with self._lock:
                    self._error = msg.get("ret_msg", "subscribe failed")
                return
            topic = msg.get("topic", "")
            data  = msg.get("data")
            if not data:
                return
            with self._lock:
                if topic.startswith("orderbook"):
                    d = data if isinstance(data, dict) else {}
                    if d.get("b"):
                        self._best_bid = float(d["b"][0][0])
                        self._bid_vol  = float(d["b"][0][1])
                    if d.get("a"):
                        self._best_ask = float(d["a"][0][0])
                        self._ask_vol  = float(d["a"][0][1])
                elif topic.startswith("kline"):
                    rows = data if isinstance(data, list) else [data]
                    for row in rows:
                        c          = kline_close(row)
                        start_time = row[0] if isinstance(row, (list, tuple)) else row.get("start", 0)
                        if self._last_kline_start == start_time and self._closes:
                            self._closes[-1] = c
                        else:
                            self._closes.append(c)
                            self._last_kline_start = start_time
                            if len(self._closes) > 30:
                                self._closes.pop(0)
                if self._best_bid > 0 and self._best_ask > 0 and self._closes:
                    self._ready = True
        except Exception as exc:
            with self._lock:
                self._error = str(exc)

    def _on_open(self, ws) -> None:
        with self._lock:
            self._error = None     # Clear prior error on successful reconnect
            self._ready = False
        ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.1.{self.symbol}", f"kline.1.{self.symbol}"]}))
        logger.info("PersistentPublicLinearFeed: subscribed to %s", self.symbol)

    def _on_error(self, _ws, error) -> None:
        logger.warning("PersistentPublicLinearFeed WS error: %s", error)

    def _on_close(self, _ws, close_status_code, close_msg) -> None:
        logger.warning("PersistentPublicLinearFeed WS closed: %s %s", close_status_code, close_msg)
        if not self._stop_event.is_set():
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnects >= WS_MAX_RECONNECTS:
            logger.error("PersistentPublicLinearFeed: max reconnects (%d) reached", WS_MAX_RECONNECTS)
            with self._lock:
                self._error = "max_reconnects_exceeded"
            return
        delay = WS_RECONNECT_DELAY * (2 ** min(self._reconnects, 4))    # Exponential backoff
        logger.info("PersistentPublicLinearFeed: reconnecting in %.1fs (attempt %d)", delay, self._reconnects + 1)
        self._reconnects += 1
        time.sleep(delay)
        self._launch_ws()

    def _launch_ws(self) -> None:
        try:
            import websocket
        except ImportError:
            raise RuntimeError("mode=ws requires: pip install websocket-client")
        self._ws_app = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        run_opts: Dict[str, Any] = {"ping_interval": 20, "ping_timeout": 10}
        run_opts.update(get_ws_run_options())
        self._thread = threading.Thread(
            target=lambda: self._ws_app.run_forever(**run_opts),
            daemon=True,
            name="bybit-public-ws",
        )
        self._thread.start()

    def start(self) -> None:
        self._launch_ws()

    def wait_ready(self, timeout_sec: float) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._lock:
                if self._error:
                    return False
                if self._ready:
                    self._reconnects = 0    # Reset counter on clean connect
                    return True
            time.sleep(0.05)
        return False

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            if self._error:
                raise RuntimeError(f"WS feed error: {self._error}")
            if not self._ready:
                raise RuntimeError("WS feed not ready")
            return {
                "best_bid": self._best_bid,
                "best_ask": self._best_ask,
                "bid_vol":  self._bid_vol,
                "ask_vol":  self._ask_vol,
                "closes":   list(self._closes),
            }

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws_app:
            self._ws_app.close()


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

def round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 8)


def round_trip_fees(entry: float, exit_p: float, qty: float, entry_fee_rate: float, exit_fee_rate: float) -> float:
    return entry * qty * entry_fee_rate + exit_p * qty * exit_fee_rate


def estimate_net_profit_v2(
    side: str,
    entry: float,
    exit_p: float,
    qty: float,
    maker_fee: float,
    exit_is_taker: bool = False,
    taker_fee: float    = DEFAULT_TAKER_FEE,
) -> float:
    exit_fee = taker_fee if exit_is_taker else maker_fee
    fees     = round_trip_fees(entry, exit_p, qty, maker_fee, exit_fee)
    if side == "Buy":
        return (exit_p - entry) * qty - fees
    return (entry - exit_p) * qty - fees


def expected_market_close_pnl(
    position_side: str,
    avg_entry: float,
    close_qty: float,
    best_bid: float,
    best_ask: float,
    maker_fee: float,
    taker_fee: float,
) -> float:
    if position_side == "Buy":
        return estimate_net_profit_v2("Buy",  avg_entry, best_bid, close_qty, maker_fee, exit_is_taker=True, taker_fee=taker_fee)
    return estimate_net_profit_v2("Sell", avg_entry, best_ask, close_qty, maker_fee, exit_is_taker=True, taker_fee=taker_fee)


def estimate_net_profit(side: str, entry: float, exit_p: float, qty: float, maker_fee: float) -> float:
    return estimate_net_profit_v2(side, entry, exit_p, qty, maker_fee, exit_is_taker=False)


OrderRole  = str   # 'entry' | 'exit_reduce'
ExitStyle  = str   # 'passive' | 'join' | 'cross'


class FillProbabilityEstimate:
    def __init__(self, probability: float, confidence: float, factors: Dict[str, float], suggested_reprice_ticks: int) -> None:
        self.probability              = probability
        self.confidence               = confidence
        self.factors                  = factors
        self.suggested_reprice_ticks  = suggested_reprice_ticks


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def classify_limit_placement(
    side: str, limit_price: float, best_bid: float, best_ask: float, tick_size: float
) -> ExitStyle:
    tick_size = max(tick_size, 1e-8)
    if side == "Buy":
        if limit_price >= best_ask - tick_size * 0.5:
            return "cross"
        if abs(limit_price - best_bid) <= tick_size * 0.5:
            return "join"
        return "passive"
    if limit_price <= best_bid + tick_size * 0.5:
        return "cross"
    if abs(limit_price - best_ask) <= tick_size * 0.5:
        return "join"
    return "passive"


def estimate_limit_fill_probability(
    *,
    order_side: str,
    limit_price: float,
    best_bid: float,
    best_ask: float,
    bid_vol: float,
    ask_vol: float,
    momentum: float,
    spread_bps: float,
    tick_size: float,
    role: OrderRole        = "entry",
    horizon_sec: float     = 30.0,
    loop_interval: float   = 2.0,
) -> FillProbabilityEstimate:
    factors: Dict[str, float] = {}
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return FillProbabilityEstimate(0.0, 0.0, {"invalid_book": 0.0}, 0)

    denom     = bid_vol + ask_vol
    imbalance = (bid_vol - ask_vol) / denom if denom > 0 else 0.0
    placement = classify_limit_placement(order_side, limit_price, best_bid, best_ask, tick_size)
    placement_score = {"cross": 0.95, "join": 0.55, "passive": 0.25}[placement]
    factors["placement"] = placement_score

    if order_side == "Sell":
        flow_score = _sigmoid(imbalance * 4.0)
        mom_align  = _sigmoid(-momentum * 800.0)
    else:
        flow_score = _sigmoid(-imbalance * 4.0)
        mom_align  = _sigmoid(momentum * 800.0)

    factors["flow_imbalance"]   = flow_score
    factors["momentum_align"]   = mom_align

    if spread_bps <= 5:
        spread_score = 0.85
    elif spread_bps <= 15:
        spread_score = 0.65
    elif spread_bps <= 30:
        spread_score = 0.45
    else:
        spread_score = 0.25
    factors["spread_activity"] = spread_score

    cycles     = max(1.0, horizon_sec / max(loop_interval, 0.2))
    time_boost = 1.0 - math.exp(-cycles / 8.0)
    factors["time_horizon"] = time_boost

    role_mult = 1.0
    if role == "exit_reduce" and placement == "passive":
        role_mult = 0.7
    elif role == "entry" and placement == "passive":
        role_mult = 0.85

    raw = (
        0.35 * placement_score
        + 0.25 * flow_score
        + 0.20 * mom_align
        + 0.15 * spread_score
        + 0.05 * time_boost
    ) * role_mult

    probability = _clamp(raw)
    confidence  = 0.55 if denom > 0 else 0.25
    if spread_bps > 50:
        confidence *= 0.7

    suggested = 0
    if placement == "passive":
        suggested = 2
    elif placement == "join" and probability < 0.45:
        suggested = 1

    return FillProbabilityEstimate(probability, confidence, factors, suggested)


def reduce_limit_price(
    position_side: str,
    best_bid: float,
    best_ask: float,
    tick_size: float,
    reprice_ticks: int = 0,
) -> float:
    if position_side == "Buy":
        px = best_bid - reprice_ticks * tick_size
    else:
        px = best_ask + reprice_ticks * tick_size
    return round_to_tick(max(px, tick_size), tick_size)


def choose_reduce_limit_with_fill_target(
    position_side: str,
    market: Dict[str, Any],
    tick_size: float,
    *,
    min_fill_probability: float  = 0.35,
    max_aggressive_ticks: int    = 3,
    role: OrderRole              = "exit_reduce",
    loop_interval: float         = 2.0,
) -> Tuple[Optional[float], FillProbabilityEstimate]:
    close_side = "Sell" if position_side == "Buy" else "Buy"
    best_bid   = market["best_bid"]
    best_ask   = market["best_ask"]

    for reprice in range(max_aggressive_ticks + 1):
        px  = reduce_limit_price(position_side, best_bid, best_ask, tick_size, reprice_ticks=reprice)
        est = estimate_limit_fill_probability(
            order_side=close_side, limit_price=px,
            best_bid=best_bid, best_ask=best_ask,
            bid_vol=market.get("bid_vol", 0.0), ask_vol=market.get("ask_vol", 0.0),
            momentum=market.get("momentum", 0.0), spread_bps=market.get("spread_bps", 99.0),
            tick_size=tick_size, role=role, loop_interval=loop_interval,
        )
        if est.probability >= min_fill_probability:
            return px, est

    px  = reduce_limit_price(position_side, best_bid, best_ask, tick_size, reprice_ticks=max_aggressive_ticks)
    est = estimate_limit_fill_probability(
        order_side=close_side, limit_price=px,
        best_bid=best_bid, best_ask=best_ask,
        bid_vol=market.get("bid_vol", 0.0), ask_vol=market.get("ask_vol", 0.0),
        momentum=market.get("momentum", 0.0), spread_bps=market.get("spread_bps", 99.0),
        tick_size=tick_size, role=role, loop_interval=loop_interval,
    )
    if est.probability < min_fill_probability * 0.75:
        return None, est
    return px, est


def entry_postonly_viable(
    side: str,
    entry_price: float,
    market: Dict[str, Any],
    tick_size: float,
    min_fill_probability: float,
    loop_interval: float,
) -> Tuple[bool, FillProbabilityEstimate]:
    est = estimate_limit_fill_probability(
        order_side=side, limit_price=entry_price,
        best_bid=market["best_bid"], best_ask=market["best_ask"],
        bid_vol=market.get("bid_vol", 0.0), ask_vol=market.get("ask_vol", 0.0),
        momentum=market.get("momentum", 0.0), spread_bps=market.get("spread_bps", 99.0),
        tick_size=tick_size, role="entry", horizon_sec=60.0, loop_interval=loop_interval,
    )
    return est.probability >= min_fill_probability, est


def build_reduce_exit_payload(
    symbol: str,
    position_side: str,
    close_qty: float,
    market: Dict[str, Any],
    tick_size: float,
    exit_order_type: str,
    exit_tif: str,
    reprice_ticks: int = 0,
) -> Dict[str, Any]:
    close_side = "Sell" if position_side == "Buy" else "Buy"
    payload: Dict[str, Any] = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        close_side,
        "qty":         str(close_qty),
        "positionIdx": 0,
        "reduceOnly":  True,
    }
    if exit_order_type == "market":
        payload["orderType"]    = "Market"
        payload["timeInForce"]  = "IOC"
        return payload

    px = reduce_limit_price(position_side, market["best_bid"], market["best_ask"], tick_size, reprice_ticks)
    payload["orderType"]   = "Limit"
    payload["price"]       = str(px)
    payload["timeInForce"] = exit_tif if exit_tif in ("PostOnly", "GTC", "IOC") else "PostOnly"
    return payload


def expected_close_pnl(
    position_side: str,
    avg_entry: float,
    close_qty: float,
    market: Dict[str, Any],
    tick_size: float,
    maker_fee: float,
    taker_fee: float,
    exit_order_type: str = "limit",
    reprice_ticks: int   = 0,
) -> Tuple[float, float]:
    if exit_order_type == "market":
        exit_px      = market["best_bid"] if position_side == "Buy" else market["best_ask"]
        exit_is_taker = True
        exit_fee     = taker_fee
    else:
        exit_px      = reduce_limit_price(position_side, market["best_bid"], market["best_ask"], tick_size, reprice_ticks)
        exit_is_taker = False
        exit_fee     = maker_fee

    fees = avg_entry * close_qty * maker_fee + exit_px * close_qty * exit_fee
    if position_side == "Buy":
        net = (exit_px - avg_entry) * close_qty - fees
    else:
        net = (avg_entry - exit_px) * close_qty - fees
    return net, exit_px


def place_reduce_exit_with_retry(
    base_url: str,
    symbol: str,
    position_side: str,
    close_qty: float,
    market: Dict[str, Any],
    tick_size: float,
    api_key: str,
    api_secret: str,
    exit_order_type: str,
    exit_tif: str,
    exit_reprice_ticks: int,
    exit_max_retries: int,
) -> Dict[str, Any]:
    last: Any = None
    for attempt in range(exit_max_retries + 1):
        payload = build_reduce_exit_payload(
            symbol, position_side, close_qty, market, tick_size,
            exit_order_type, exit_tif, reprice_ticks=attempt * exit_reprice_ticks,
        )
        try:
            res = send_signed_post(base_url, "/v5/order/create", payload, api_key, api_secret)
            if res.get("retCode") == 0:
                res["_exit_payload"] = payload
                _circuit_breaker.record_success()
                return res
            last = res
            if res.get("retCode") in (110007, 110017, 110043):
                time.sleep(0.15 * (2 ** attempt))
                continue
            break
        except Exception as exc:
            last = str(exc)
            _circuit_breaker.record_failure()
            time.sleep(0.15 * (2 ** attempt))
    return {"retCode": -1, "retMsg": f"reduce exit failed: {last}"}


def set_position_take_profit(
    base_url: str,
    symbol: str,
    position_side: str,
    position_size: float,
    avg_entry: float,
    tp_price: float,
    api_key: str,
    api_secret: str,
    full: bool                  = True,
    tp_size: Optional[float]    = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "category":      "linear",
        "symbol":        symbol,
        "positionIdx":   0,
        "takeProfit":    str(round(tp_price, 8)),
        "tpTriggerBy":   "LastPrice",
        "tpOrderType":   "Limit",
        "tpLimitPrice":  str(round(tp_price, 8)),
        "tpslMode":      "Full" if full else "Partial",
    }
    if not full and tp_size is not None:
        body["tpSize"] = str(tp_size)
    return send_signed_post(base_url, "/v5/position/trading-stop", body, api_key, api_secret)


def verify_tp_net(
    side: str, entry: float, tp: float, qty: float, maker_fee: float, min_net: float, tick_size: float
) -> bool:
    tp = round_to_tick(tp, tick_size)
    if side == "Buy"  and tp <= entry:
        return False
    if side == "Sell" and tp >= entry:
        return False
    net = estimate_net_profit_v2(side, entry, tp, qty, maker_fee, exit_is_taker=False)
    return net >= min_net - 1e-9


def solve_tp_with_floor(
    entry: float,
    side: str,
    qty: float,
    maker_fee: float,
    min_net: float,
    market: Optional[Dict[str, Any]] = None,
    tick_size: float = 0.01,
) -> Optional[float]:
    tiers = sorted(t for t in MICRO_PROFIT_TIERS if t >= min_net) or [min_net]
    for net_tgt in tiers:
        try:
            tp = net_profit_to_tp_advanced(entry, side, net_tgt, qty, maker_fee, market)
        except Exception:
            continue
        if verify_tp_net(side, entry, tp, qty, maker_fee, net_tgt, tick_size):
            return round_to_tick(tp, tick_size)
    return None


def evaluate_signal(
    market: Dict[str, Any], qty: float, target_profit: float, maker_fee: float
) -> Optional[Dict[str, Any]]:
    """Legacy signal evaluator — kept for compatibility."""
    best_bid  = market["best_bid"]
    best_ask  = market["best_ask"]
    imbalance = market["imbalance"]
    momentum  = market["momentum"]
    tick_size = market["tick_size"]
    side: Optional[str] = None
    entry_price = exit_price = 0.0

    if momentum > MOMENTUM_LONG and imbalance > IMBALANCE_LONG:
        side        = "Buy"
        entry_price = best_bid
        entry_fee   = entry_price * qty * maker_fee
        raw_exit    = (target_profit + entry_fee + entry_price * qty) / (qty * (1 - maker_fee))
        exit_price  = round_to_tick(raw_exit, tick_size)
    elif momentum < MOMENTUM_SHORT and imbalance < IMBALANCE_SHORT:
        side        = "Sell"
        entry_price = best_ask
        entry_fee   = entry_price * qty * maker_fee
        raw_exit    = ((entry_price * qty) - entry_fee - target_profit) / (qty * (1 + maker_fee))
        exit_price  = round_to_tick(raw_exit, tick_size)

    if not side:
        return None
    return {"side": side, "entry_price": entry_price, "exit_price": exit_price, "momentum": momentum, "imbalance": imbalance, "tick_size": tick_size}


def check_account_balance(base_url: str, api_key: str, api_secret: str, required_margin: float) -> bool:
    try:
        resp = send_signed_get(base_url, "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, api_key, api_secret)
    except Exception as exc:
        emit({"status": "error", "message": f"Balance check request failed: {exc}"})
        return False
    if resp.get("retCode") != 0:
        emit({"status": "error", "message": f"Balance check failed: {resp}"})
        return False

    free_usdt = 0.0
    for acct in resp.get("result", {}).get("list", []):
        tot_avail = acct.get("totalAvailableBalance")
        if tot_avail is not None and str(tot_avail).strip():
            free_usdt = max(free_usdt, float(tot_avail))
            continue
        for coin in acct.get("coin", []):
            if coin.get("coin") != "USDT":
                continue
            avail = coin.get("availableToWithdraw") or coin.get("availableBalance")
            if avail is not None and str(avail).strip():
                free_usdt = max(free_usdt, float(avail))
            else:
                wb     = float(coin.get("walletBalance") or 0)
                locked = float(coin.get("locked") or 0)
                free_usdt = max(free_usdt, wb - locked)

    if free_usdt < required_margin:
        emit({"status": "rejected", "message": f"Insufficient free balance (USDT {free_usdt:.4f}) to cover margin ({required_margin:.4f})"})
        return False
    return True


def has_open_position(base_url: str, api_key: str, api_secret: str, symbol: str) -> bool:
    try:
        resp = send_signed_get(base_url, "/v5/position/list", {"category": "linear", "symbol": symbol}, api_key, api_secret)
    except Exception:
        return True
    if resp.get("retCode") != 0:
        return True
    for pos in resp.get("result", {}).get("list", []):
        if float(pos.get("size") or 0) > 0:
            return True
    return False


def place_micro_order_with_retry(
    base_url: str,
    entry_payload: Dict[str, Any],
    api_key: str,
    api_secret: str,
    max_retries: int = 2,
    tick_size: float = 0.01,
) -> Dict[str, Any]:
    import copy
    last_error: Any = None
    for attempt in range(max_retries + 1):
        payload = copy.deepcopy(entry_payload)
        try:
            if attempt > 0 and "price" in payload:
                px = float(payload["price"])
                if payload.get("timeInForce") == "PostOnly":
                    offset = tick_size * attempt
                    payload["price"] = str(round(px + offset if payload["side"] == "Buy" else px - offset, 8))
                else:
                    payload["timeInForce"] = "IOC"

            response = send_signed_post(base_url, "/v5/order/create", payload, api_key, api_secret)
            if response.get("retCode") == 0:
                _circuit_breaker.record_success()
                return response
            last_error = response
            time.sleep(0.1 * (2 ** attempt))
        except Exception as exc:
            last_error = str(exc)
            _circuit_breaker.record_failure()
            time.sleep(0.1 * (2 ** attempt))
    return {"retCode": -1, "retMsg": f"All retries failed: {last_error}"}


# ---------------------------------------------------------------------------
# Position guard (dynamic)
# ---------------------------------------------------------------------------

class DynamicPositionGuard:
    def __init__(self, min_profit_to_override: float = 0.02, aggressive: bool = False) -> None:
        self.min_profit_to_override = min_profit_to_override
        self.aggressive             = aggressive

    def decide(self, entry_side: str, confidence: float, position_info: Dict[str, Any]) -> Tuple[bool, str]:
        if not position_info or position_info.get("size", 0) <= 0:
            return False, "no_position"

        pnl = position_info.get("unrealized_pnl", 0.0)

        if pnl > self.min_profit_to_override:
            if entry_side != position_info.get("side"):
                if self.aggressive or confidence > 0.6:
                    return False, "hedging_opportunity"
            else:
                if self.aggressive or confidence > 0.8:
                    return False, "strong_signal_scaling"

        if pnl < -self.min_profit_to_override:
            if entry_side == position_info.get("side"):
                return True, "existing_loss_same_direction"
            return True, "existing_loss_no_market_reversal"

        return True, "open_position"

    def check_position_guard_with_profit(
        self, base_url: str, api_key: str, api_secret: str,
        symbol: str, entry_side: str, confidence: float,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        try:
            resp = send_signed_get(base_url, "/v5/position/list", {"category": "linear", "symbol": symbol}, api_key, api_secret)
        except Exception:
            return True, "position_check_failed", {}

        if resp.get("retCode") != 0:
            return True, f"api_error: {resp.get('retMsg')}", {}

        for pos in resp.get("result", {}).get("list", []):
            size = float(pos.get("size") or 0)
            if size > 0:
                position_info = {
                    "size":               size,
                    "side":               pos.get("side"),
                    "entry_price":        float(pos.get("avgPrice") or 0),
                    "unrealized_pnl":     float(pos.get("unrealisedPnl") or 0),
                    "unrealized_pnl_pct": float(pos.get("unrealisedPnlPct") or 0),
                    "mark_price":         float(pos.get("markPrice") or 0),
                }
                should_skip, reason = self.decide(entry_side, confidence, position_info)
                return should_skip, reason, position_info

        return False, "no_position", {}


def enhanced_position_guard_check(
    args: Dict[str, Any],
    base_url: str,
    api_key: str,
    api_secret: str,
    symbol: str,
    sig: Dict[str, Any],
    position_guard_enabled: bool,
    iteration: int,
    emit_result: Callable[[Dict[str, Any]], None],
    position_info: Optional[Dict[str, Any]] = None,
) -> bool:
    if not position_guard_enabled:
        return False

    guard = DynamicPositionGuard(
        min_profit_to_override=float(args.get("position_guard_profit_override", 0.02)),
        aggressive=bool(args.get("position_guard_aggressive", False)),
    )

    if position_info is not None:
        should_skip, reason = guard.decide(sig["side"], sig.get("confidence", 0.5), position_info)
    else:
        should_skip, reason, position_info = guard.check_position_guard_with_profit(
            base_url, api_key, api_secret, symbol, sig["side"], sig.get("confidence", 0.5)
        )

    if should_skip:
        emit_result({
            "status": "skipped", "iteration": iteration,
            "position_guard": True, "reason": reason,
            "existing_position": position_info,
            "message": f"Position guard active: {reason}",
        })
        return True

    if position_info:
        emit_result({
            "status": "info", "iteration": iteration,
            "position_guard": True, "override": True,
            "reason": reason, "existing_position": position_info,
            "message": f"Position guard overridden: {reason}",
        })
    return False


# ---------------------------------------------------------------------------
# Private WS logger
# ---------------------------------------------------------------------------

class PrivateWsLogger:
    def __init__(self, ws_private_url: str, api_key: str, api_secret: str, symbol: str) -> None:
        self.ws_private_url = ws_private_url
        self.api_key        = api_key
        self.api_secret     = api_secret
        self.symbol         = symbol
        self._ws_app        = None

    def _on_message(self, _ws, message: str) -> None:
        try:
            msg   = json.loads(message)
            topic = msg.get("topic", "")
            if topic not in ("order.linear", "execution.linear", "order", "execution"):
                return
            data = msg.get("data")
            if not data:
                return
            for row in (data if isinstance(data, list) else [data]):
                if row.get("symbol") and row.get("symbol") != self.symbol:
                    continue
                net_pnl = float(row.get("closedPnl") or 0.0)
                fees    = float(row.get("execFee") or 0.0)
                if row.get("execType") == "Trade" and net_pnl != 0:
                    _session_pnl.record(net_pnl, fees)
                emit({
                    "status":       "ws_event",
                    "topic":        topic,
                    "symbol":       row.get("symbol"),
                    "order_status": row.get("orderStatus"),
                    "exec_type":    row.get("execType"),
                    "side":         row.get("side"),
                    "qty":          row.get("qty")  or row.get("execQty"),
                    "price":        row.get("price") or row.get("execPrice"),
                    "order_id":     row.get("orderId"),
                    "session_pnl":  _session_pnl.snapshot(),
                })
        except Exception:
            pass

    def _on_open(self, ws) -> None:
        ws.send(ws_auth_message(self.api_key, self.api_secret))
        time.sleep(0.3)
        ws.send(json.dumps({"op": "subscribe", "args": ["order.linear", "execution.linear"]}))

    def start(self) -> None:
        try:
            import websocket
        except ImportError:
            emit({"status": "error", "message": "private-ws requires: pip install websocket-client"})
            return
        self._ws_app = websocket.WebSocketApp(self.ws_private_url, on_open=self._on_open, on_message=self._on_message)
        run_opts: Dict[str, Any] = {"ping_interval": 20, "ping_timeout": 10}
        run_opts.update(get_ws_run_options())
        threading.Thread(target=lambda: self._ws_app.run_forever(**run_opts), daemon=True, name="bybit-private-ws").start()

    def stop(self) -> None:
        if self._ws_app:
            self._ws_app.close()


# ---------------------------------------------------------------------------
# Market data dispatcher
# ---------------------------------------------------------------------------

def fetch_market(
    args: Dict[str, Any],
    urls: Dict[str, str],
    tick_cache: Dict[str, float],
    public_feed: Optional[PersistentPublicLinearFeed],
) -> Dict[str, Any]:
    symbol     = args["symbol"]
    mode       = args["mode"]
    base_url   = urls["rest"]
    api_key    = args["api_key"]
    api_secret = args["api_secret"]

    if public_feed is not None:
        snap      = public_feed.snapshot()
        tick_size = fetch_tick_size(base_url, symbol, tick_cache, api_key, api_secret)
        return build_market_snapshot(
            snap["best_bid"], snap["best_ask"],
            snap["bid_vol"],  snap["ask_vol"],
            snap["closes"],   tick_size, "ws_persistent",
        )

    if mode == "ws":
        return get_market_data_ws_oneshot(
            urls["ws_public_linear"], base_url, symbol,
            args["ws_timeout"], tick_cache, api_key, api_secret,
        )
    return get_market_data_rest(base_url, symbol, tick_cache, api_key, api_secret)


# ---------------------------------------------------------------------------
# Single evaluation cycle
# ---------------------------------------------------------------------------

def _fetch_position(base_url: str, symbol: str, api_key: str, api_secret: str) -> Dict[str, Any]:
    """Fetch first open position for symbol; returns {} if none or on error."""
    try:
        resp = send_signed_get(base_url, "/v5/position/list", {"category": "linear", "symbol": symbol}, api_key, api_secret)
        if resp.get("retCode") == 0:
            for pos in resp.get("result", {}).get("list", []):
                size = float(pos.get("size") or 0)
                if size > 0:
                    return {
                        "size":               size,
                        "side":               pos.get("side"),
                        "entry_price":        float(pos.get("avgPrice") or 0),
                        "unrealized_pnl":     float(pos.get("unrealisedPnl") or 0),
                        "unrealized_pnl_pct": float(pos.get("unrealisedPnlPct") or 0),
                        "mark_price":         float(pos.get("markPrice") or 0),
                    }
    except Exception:
        pass
    return {}


def run_one_cycle(
    args: Dict[str, Any],
    urls: Dict[str, str],
    tick_cache: Dict[str, float],
    loop_state: Dict[str, Any],
    public_feed: Optional[PersistentPublicLinearFeed],
    emit_result: Callable[[Dict[str, Any]], None],
) -> None:
    api_key          = args["api_key"]
    api_secret       = args["api_secret"]
    symbol           = args["symbol"]
    base_qty         = args["qty"]
    target_profit    = args["target_profit"]
    maker_fee        = args["maker_fee"]
    trailing_stop    = args.get("trailing_stop")
    balance_check    = args["balance_check"]
    dry_run          = args["dry_run"]
    max_spread_bps   = args["max_spread_bps"]
    cooldown         = args["cooldown"]
    position_guard   = args["position_guard"]
    ws_fallback_rest = args["ws_fallback_rest"]
    base_url         = urls["rest"]

    iteration = loop_state.get("iteration", 0) + 1
    loop_state["iteration"] = iteration

    # ---- Circuit breaker check ----
    if _circuit_breaker.is_open():
        remaining = _circuit_breaker.seconds_remaining()
        emit_result({"status": "circuit_breaker", "iteration": iteration, "pause_seconds_remaining": round(remaining, 1), "message": "Circuit breaker open — too many consecutive failures"})
        return

    # ---- Server time sync ----
    if iteration == 1 or iteration % SERVER_TIME_SYNC_INTERVAL == 0:
        sync_server_time(base_url)

    # ---- Cooldown check ----
    now            = time.time()
    last_order_ts  = loop_state.get("last_order_ts", 0.0)
    if last_order_ts and (now - last_order_ts) < cooldown:
        remaining_cool = round(cooldown - (now - last_order_ts), 2)
        emit_result({"status": "cooldown", "iteration": iteration, "seconds_remaining": remaining_cool, "message": "Waiting after last order."})
        return

    # ---- Market data ----
    try:
        market = fetch_market(args, urls, tick_cache, public_feed)
    except Exception as exc:
        _circuit_breaker.record_failure()
        if args["mode"] == "ws" and ws_fallback_rest and public_feed is None:
            for retry in range(3):
                try:
                    market = get_market_data_rest(base_url, symbol, tick_cache, api_key, api_secret)
                    market["source"] = "rest_fallback"
                    break
                except Exception as exc2:
                    if retry == 2:
                        emit_result({"status": "error", "iteration": iteration, "message": f"Market data failed (ws+rest): {exc}; {exc2}"})
                        return
                    time.sleep(0.5 * (2 ** retry))
        else:
            emit_result({"status": "error", "iteration": iteration, "message": f"Market data fetch failed: {exc}"})
            return

    # ---- Account balance (cached, refreshed every 20 iterations) ----
    if "account_balance" not in loop_state or iteration % 20 == 0 or iteration == 1:
        try:
            resp = send_signed_get(base_url, "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, api_key, api_secret)
            if resp.get("retCode") == 0:
                tot = resp["result"]["list"][0].get("totalAvailableBalance")
                if tot:
                    loop_state["account_balance"] = float(tot)
        except Exception:
            pass

    account_balance = loop_state.get("account_balance", 1000.0)

    # ---- Session win rate for dynamic Kelly ----
    snap     = _session_pnl.snapshot()
    ses_wrate = snap["win_rate"] if snap["trade_count"] >= 10 else 0.55

    qty = calculate_optimal_qty(
        market, base_qty, target_profit,
        account_balance=account_balance,
        session_win_rate=ses_wrate,
    )

    # ---- Spread guard ----
    spread_bps = market["spread_bps"]
    if spread_bps > max_spread_bps:
        emit_result({"status": "skipped", "iteration": iteration, "spread_bps": round(spread_bps, 2), "data_source": market.get("source"), "message": "Spread too wide."})
        return

    # ---- Rolling momentum (volume-weighted micro-price) ----
    mid_prices = loop_state.setdefault("mid_prices", deque(maxlen=10))
    denom      = market["bid_vol"] + market["ask_vol"]
    current_mid = (
        (market["best_bid"] * market["ask_vol"] + market["best_ask"] * market["bid_vol"]) / denom
        if denom > 0 else (market["best_bid"] + market["best_ask"]) / 2.0
    )
    mid_prices.append(current_mid)
    price_momentum = (mid_prices[-1] - mid_prices[0]) / mid_prices[0] if len(mid_prices) >= 2 else 0.0

    obi_hist = loop_state.setdefault("obi_hist", deque(maxlen=5))
    obi_hist.append(market["imbalance"])
    obi_delta = (obi_hist[-1] - obi_hist[0]) / 10.0 if len(obi_hist) >= 2 else 0.0

    market["momentum"] = price_momentum if abs(price_momentum) > 0.000005 else obi_delta

    # ---- Multi-TF and micro-pattern analyzers ----
    if "multi_tf_analyzer" not in loop_state:
        loop_state["multi_tf_analyzer"] = MultiTimeframeAnalyzer()
    if "micro_pattern_analyzer" not in loop_state:
        loop_state["micro_pattern_analyzer"] = MicroPatternAnalyzer()

    if market.get("closes"):
        loop_state["multi_tf_analyzer"].update(market["closes"][-1], time.time())
    loop_state["micro_pattern_analyzer"].update(market["best_bid"], market["best_ask"])

    trend_score, trend_direction = loop_state["multi_tf_analyzer"].get_trend_signal()

    # ---- Volume profile ----
    vol_hist = loop_state.setdefault("vol_hist", deque(maxlen=60))
    cur_vol  = float(market.get("bid_vol", 0.0) + market.get("ask_vol", 0.0))
    vol_hist.append(cur_vol)
    avg_vol  = sum(vol_hist) / len(vol_hist) if vol_hist else cur_vol
    volume_profile = {"avg_volume": avg_vol, "current_volume": cur_vol}

    # ---- Position fetch (single call per cycle — eliminates duplicate) ----
    position_info = _fetch_position(base_url, symbol, api_key, api_secret)

    # ---- Native TP refresh for existing positions ----
    if args.get("use_trading_stop_tp") and position_info.get("size", 0) > 0 and not dry_run:
        pos_side  = position_info["side"]
        pos_size  = position_info["size"]
        pos_entry = position_info["entry_price"]
        tp = solve_tp_with_floor(
            pos_entry, pos_side, min(qty, pos_size), maker_fee,
            min_net=max(0.02, target_profit * 0.95),
            market=market, tick_size=market["tick_size"],
        )
        if tp is not None:
            try:
                ts_res = set_position_take_profit(
                    base_url, symbol, pos_side, pos_size, pos_entry, tp, api_key, api_secret,
                    full=(min(qty, pos_size) >= pos_size),
                    tp_size=None if min(qty, pos_size) >= pos_size else min(qty, pos_size),
                )
                if ts_res.get("retCode") == 0:
                    emit_result({"status": "info", "iteration": iteration, "message": "Position TP refreshed", "take_profit_price": tp})
            except Exception as exc:
                emit_result({"status": "info", "iteration": iteration, "message": f"TP refresh failed: {exc}"})

    # ---- Signal evaluation ----
    sig = evaluate_signal_v2(market, qty, target_profit, maker_fee, volume_profile, position_info)
    if sig:
        pattern_sig = loop_state["micro_pattern_analyzer"].get_entry_signal()
        if pattern_sig["signal_strength"] > 0.3 and pattern_sig["direction"] != "neutral":
            sig["confidence"] = min(1.0, sig["confidence"] * (1.0 + pattern_sig["signal_strength"]))

        early_cycle = loop_state.get("iteration", 0) < 30
        if trend_direction == "bullish" and sig["side"] == "Buy":
            sig["confidence"] = min(1.0, sig["confidence"] * (1.0 + trend_score))
        elif trend_direction == "bearish" and sig["side"] == "Sell":
            sig["confidence"] = min(1.0, sig["confidence"] * (1.0 + trend_score))
        elif trend_direction == "neutral" or early_cycle:
            pass
        else:
            sig = None

    if not sig:
        emit_result({
            "status": "skipped", "iteration": iteration,
            "momentum":   f"{market['momentum']:.8f}",
            "imbalance":  round(market["imbalance"], 2),
            "data_source": market.get("source"),
            "message":    "No clear micro-imbalance signal.",
        })
        return

    side        = sig["side"]
    entry_price = sig["entry_price"]
    exit_price  = sig["exit_price"]
    tick_size   = sig["tick_size"]

    # ---- TP sanity checks ----
    if abs(exit_price - entry_price) <= tick_size:
        emit_result({"status": "skipped", "iteration": iteration, "message": f"Target within one tick ({tick_size}). Increase qty or target."})
        return

    tp_distance_pct = abs(exit_price - entry_price) / entry_price
    if tp_distance_pct > MAX_TP_DISTANCE_PCT:
        min_net = min(MICRO_PROFIT_TIERS)
        min_move = entry_price * 0.0001
        min_qty  = max(qty, round(min_net / max(min_move, tick_size), 8))
        emit_result({
            "status": "skipped", "iteration": iteration,
            "message": f"No micro-profit tier reachable within 5% at qty={qty}. Increase --qty to ~{min_qty}.",
        })
        return

    net_est = estimate_net_profit_v2(side, entry_price, exit_price, qty, maker_fee, exit_is_taker=False)
    if net_est < 0.02:
        emit_result({"status": "skipped", "iteration": iteration, "message": f"TP net {net_est:.6f} < floor 0.02 USDT.", "entry": entry_price, "tp": exit_price})
        return

    # ---- Fill probability check ----
    min_fill_p    = float(args.get("min_fill_probability", 0.30))
    is_reduce_chk = bool(position_info and position_info.get("size", 0) > 0 and side != position_info.get("side"))
    if not is_reduce_chk:
        ok, fill_est = entry_postonly_viable(side, entry_price, market, tick_size, min_fill_p, args["loop_interval"])
        if not ok:
            emit_result({
                "status": "skipped", "iteration": iteration,
                "reason": "low_entry_fill_probability",
                "fill_probability": round(fill_est.probability, 4),
                "factors": fill_est.factors,
                "message": "PostOnly entry unlikely to fill.",
            })
            return

    # ---- Adaptive stop loss ----
    trailing_distance  = parse_trailing_stop(trailing_stop)
    stop_loss_price: Optional[float] = None
    if trailing_distance is not None:
        closes         = market.get("closes", [])
        if len(closes) >= 5:
            recent    = closes[-5:]
            pct_moves = [abs(recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent)) if recent[i - 1] != 0]
            mkt_vol   = sum(pct_moves) / len(pct_moves) if pct_moves else 0.002
        else:
            mkt_vol = 0.002
        stop_loss_price, _ = calculate_adaptive_stop_loss(entry_price, side, mkt_vol, tick_size, trailing_distance)

    # ---- Position guard ----
    if position_guard and not dry_run:
        if enhanced_position_guard_check(args, base_url, api_key, api_secret, symbol, sig, position_guard, iteration, emit_result, position_info=position_info):
            return

    # ---- Reduce-only path ----
    is_reduce = bool(position_info and position_info.get("size", 0) > 0 and side != position_info.get("side"))
    if is_reduce:
        close_qty       = min(qty, position_info["size"])
        exit_order_type = args.get("exit_order_type", "limit")
        exit_px, fill_est = choose_reduce_limit_with_fill_target(
            position_info["side"], market, tick_size,
            min_fill_probability=min_fill_p,
            max_aggressive_ticks=int(args.get("exit_max_aggressive_ticks", 3)),
            loop_interval=args["loop_interval"],
        )
        if exit_px is None:
            emit_result({
                "status": "skipped", "iteration": iteration,
                "reason": "low_exit_fill_probability",
                "fill_probability": round(fill_est.probability, 4),
                "factors": fill_est.factors,
                "message": "Limit reduce unlikely to fill; keep position.",
            })
            return

        exp_pnl, _ = expected_close_pnl(
            position_info["side"], position_info["entry_price"], close_qty,
            market, tick_size, maker_fee, float(args.get("taker_fee", DEFAULT_TAKER_FEE)),
            exit_order_type=exit_order_type,
        )
        max_loss = float(args.get("max_loss_close_usdt", 0.0))
        if exp_pnl < -max_loss:
            emit_result({
                "status": "skipped", "iteration": iteration,
                "reason": "market_close_would_realize_loss",
                "expected_close_pnl_usdt": round(exp_pnl, 6),
                "message": "Refusing reduce: expected loss exceeds threshold.",
            })
            return

        if dry_run:
            emit_result({"status": "dry_run", "iteration": iteration, "reduce_only": True, "exit_order_type": exit_order_type, "expected_close_pnl_usdt": round(exp_pnl, 6), "exit_limit_price": exit_px, "fill_probability": round(fill_est.probability, 4)})
            return

        entry_res = place_reduce_exit_with_retry(
            base_url, symbol, position_info["side"], close_qty, market, tick_size,
            api_key, api_secret, exit_order_type,
            args.get("exit_tif", "PostOnly"), int(args.get("exit_reprice_ticks", 1)), int(args.get("exit_max_retries", 2)),
        )
        if entry_res.get("retCode") != 0:
            emit_result({"status": "failed", "iteration": iteration, "stage": "reduce_execution", "response": entry_res, "message": "Reduce order rejected"})
        else:
            loop_state["last_order_ts"] = time.time()
            emit_result({"status": "success", "iteration": iteration, "reduce_only": True, "expected_close_pnl_usdt": round(exp_pnl, 6), "exit_limit_price": exit_px, "fill_probability": round(fill_est.probability, 4), "response": entry_res})
        return

    # ---- Margin / balance check ----
    required_margin = entry_price * qty * (1.0 + maker_fee) / args.get("leverage", 1.0)
    if balance_check and not check_account_balance(base_url, api_key, api_secret, required_margin):
        return

    # ---- Build entry order payload ----
    entry_payload: Dict[str, Any] = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "qty":         str(qty),
        "positionIdx": 0,
        "orderType":   "Limit",
        "price":       str(entry_price),
        "timeInForce": "PostOnly",
        "takeProfit":  str(exit_price),
        "tpOrderType": "Limit",
        "tpLimitPrice": str(exit_price),
        "tpTriggerBy": "LastPrice",
        "tpslMode":    "Partial" if position_info.get("size", 0) > 0 else "Full",
    }
    if position_info.get("size", 0) > 0:
        entry_payload["tpSize"] = str(qty)
    if stop_loss_price is not None:
        entry_payload["stopLoss"]     = str(stop_loss_price)
        entry_payload["slTriggerBy"]  = "LastPrice"

    if dry_run:
        emit_result({
            "status": "dry_run", "iteration": iteration,
            "direction": "LONG" if side == "Buy" else "SHORT",
            "metrics": {"momentum": f"{sig['momentum']:.8f}", "imbalance": round(sig["imbalance"], 2), "spread_bps": round(spread_bps, 2), "data_source": market.get("source")},
            "execution": {"entry_limit_price": entry_price, "take_profit_price": exit_price, "estimated_net_profit_usdt": round(net_est, 6), "tick_spread_required": round(abs(exit_price - entry_price) / tick_size, 1), "payload": entry_payload},
            "session_pnl": _session_pnl.snapshot(),
        })
        return

    entry_res = place_micro_order_with_retry(base_url, entry_payload, api_key, api_secret, tick_size=tick_size)

    if entry_res.get("retCode") != 0:
        hint = " (check system clock / API signature)" if entry_res.get("retCode") == 10004 else ""
        emit_result({"status": "failed", "iteration": iteration, "stage": "execution", "response": entry_res, "message": f"Order rejected{hint}"})
        return

    loop_state["last_order_ts"] = time.time()
    result: Dict[str, Any] = {
        "status":    "success",
        "iteration": iteration,
        "direction": "LONG" if side == "Buy" else "SHORT",
        "metrics": {
            "momentum":   f"{sig['momentum']:.8f}",
            "imbalance":  round(sig["imbalance"], 2),
            "spread_bps": round(spread_bps, 2),
            "data_source": market.get("source"),
        },
        "execution": {
            "entry_limit_price":        entry_price,
            "take_profit_price":        exit_price,
            "estimated_net_profit_usdt": round(net_est, 6),
            "tick_spread_required":      round(abs(exit_price - entry_price) / tick_size, 1),
            "order_id":                  entry_res.get("result", {}).get("orderId"),
        },
        "session_pnl": _session_pnl.snapshot(),
    }
    if trailing_distance is not None and stop_loss_price is not None:
        result["execution"]["trailing_stop"] = {"distance": trailing_distance, "stop_loss_price": stop_loss_price}
    emit_result(result)


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def run_daemon(args: Dict[str, Any]) -> None:
    urls       = base_urls(args["testnet"])
    tick_cache: Dict[str, float] = {}
    loop_state: Dict[str, Any]   = {"iteration": 0, "last_order_ts": 0.0}

    public_feed: Optional[PersistentPublicLinearFeed] = None
    if args["mode"] == "ws":
        seed_closes: List[float] = []
        try:
            klines_boot = send_signed_get(
                urls["rest"], "/v5/market/kline",
                {"category": "linear", "symbol": args["symbol"], "interval": "1", "limit": 30},
                args["api_key"], args["api_secret"],
            )["result"]["list"]
            seed_closes = [kline_close(k) for k in reversed(klines_boot)]
            logger.info("Seeded %d kline closes from REST", len(seed_closes))
        except Exception as exc:
            logger.warning("Could not seed klines: %s", exc)

        public_feed = PersistentPublicLinearFeed(urls["ws_public_linear"], args["symbol"], seed_closes=seed_closes)
        public_feed.start()
        if not public_feed.wait_ready(args["ws_timeout"]):
            emit({"status": "error", "message": "Persistent public WS failed to become ready"})
            public_feed.stop()
            if not args["ws_fallback_rest"]:
                return
            public_feed = None
            emit({"status": "warning", "message": "Continuing with REST fallback only"})

    private_logger: Optional[PrivateWsLogger] = None
    if args.get("private_ws"):
        private_logger = PrivateWsLogger(urls["ws_private"], args["api_key"], args["api_secret"], args["symbol"])
        private_logger.start()

    emit({
        "status":         "daemon_started",
        "symbol":         args["symbol"],
        "mode":           args["mode"],
        "ws_persistent":  public_feed is not None,
        "loop_interval":  args["loop_interval"],
        "cooldown":       args["cooldown"],
        "dry_run":        args["dry_run"],
        "position_guard": args["position_guard"],
        "private_ws":     bool(args.get("private_ws")),
        "message":        "Daemon loop running; SIGINT/SIGTERM to stop.",
    })

    max_iter = int(args.get("max_iterations", 0))
    try:
        while not _SHUTDOWN.is_set():
            if max_iter > 0 and loop_state["iteration"] >= max_iter:
                emit({"status": "daemon_stopped", "reason": "max_iterations_reached", "iteration": loop_state["iteration"]})
                break
            run_one_cycle(args, urls, tick_cache, loop_state, public_feed, emit)
            if _SHUTDOWN.wait(timeout=args["loop_interval"]):
                break
    finally:
        if public_feed:
            public_feed.stop()
        if private_logger:
            private_logger.stop()
        emit({"status": "daemon_stopped", "reason": "shutdown", "iteration": loop_state.get("iteration", 0), "session_pnl": _session_pnl.snapshot()})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(args: Dict[str, Any]) -> None:
    api_key    = args.get("api_key")    or os.getenv("BYBIT_API_KEY")
    api_secret = args.get("api_secret") or os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret:
        emit({"status": "error", "message": "api-key and api-secret are required"})
        return

    symbol        = args.get("symbol", "BTCUSDT")
    qty           = float(args.get("qty", 0.01))
    target_profit = float(args.get("target_profit", 0.05))
    maker_fee     = float(args.get("maker_fee", 0.0002))

    if not (0.02 <= target_profit <= 0.20):
        emit({"status": "error", "message": "target_profit must be 0.02–0.20 USDT"})
        return
    if qty <= 0:
        emit({"status": "error", "message": "qty must be positive"})
        return

    mode = str(args.get("mode", "rest")).lower()
    if mode not in ("rest", "ws"):
        emit({"status": "error", "message": "mode must be rest or ws"})
        return

    loop_interval = float(args.get("loop_interval", 2.0))
    if loop_interval < 0.2:
        emit({"status": "error", "message": "loop-interval must be >= 0.2 seconds"})
        return

    exit_order_type = str(args.get("exit_order_type", "limit")).lower()
    if exit_order_type not in ("limit", "market"):
        emit({"status": "error", "message": "exit-order-type must be limit or market"})
        return

    testnet  = bool(args.get("testnet", False))
    urls_base = base_urls(testnet)
    sync_server_time(urls_base["rest"])

    trailing_stop = parse_trailing_stop(args.get("trailing_stop"))

    pos_guard_agg = args.get("position_guard_aggressive", False)

    use_tp = args.get("use_trading_stop_tp", True)
    if isinstance(use_tp, str):
        use_tp = use_tp.lower() in ("true", "1", "yes")

    verbose = bool(args.get("verbose", False))
    if verbose:
        logger.setLevel(logging.DEBUG)

    normalized: Dict[str, Any] = {
        "api_key":                      api_key,
        "api_secret":                   api_secret,
        "symbol":                       symbol,
        "qty":                          qty,
        "target_profit":                target_profit,
        "maker_fee":                    maker_fee,
        "trailing_stop":                trailing_stop,
        "balance_check":                bool(args.get("balance_check", False)),
        "dry_run":                      bool(args.get("dry_run", False)),
        "max_spread_bps":               float(args.get("max_spread_bps", 50)),
        "mode":                         mode,
        "ws_timeout":                   float(args.get("ws_timeout", 8)),
        "testnet":                      testnet,
        "loop_interval":                loop_interval,
        "cooldown":                     float(args.get("cooldown", 30)),
        "max_iterations":               int(args.get("max_iterations", 0)),
        "private_ws":                   bool(args.get("private_ws", False)),
        "position_guard":               bool(args.get("position_guard", False)),
        "position_guard_profit_override": float(args.get("position_guard_profit_override", 0.02)),
        "position_guard_aggressive":    pos_guard_agg if isinstance(pos_guard_agg, bool) else str(pos_guard_agg).lower() in ("true", "1", "yes"),
        "ws_fallback_rest":             bool(args.get("ws_fallback_rest", False)),
        "leverage":                     float(args.get("leverage", DEFAULT_LEVERAGE)),
        "verbose":                      verbose,
        "exit_order_type":              exit_order_type,
        "exit_tif":                     str(args.get("exit_tif", "PostOnly")),
        "exit_reprice_ticks":           int(args.get("exit_reprice_ticks", 1)),
        "exit_max_retries":             int(args.get("exit_max_retries", 2)),
        "use_trading_stop_tp":          use_tp,
        "min_fill_probability":         float(args.get("min_fill_probability", 0.30)),
        "exit_max_aggressive_ticks":    int(args.get("exit_max_aggressive_ticks", 3)),
        "fill_horizon_sec":             float(args.get("fill_horizon_sec", 30.0)),
        "taker_fee":                    float(args.get("taker_fee", DEFAULT_TAKER_FEE)),
        "max_loss_close_usdt":          float(args.get("max_loss_close_usdt", 0.0)),
    }

    loop = bool(args.get("loop", False))
    if loop:
        signal.signal(signal.SIGINT,  _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        run_daemon(normalized)
        return

    urls       = base_urls(testnet)
    tick_cache: Dict[str, float] = {}
    run_one_cycle(normalized, urls, tick_cache, {"iteration": 0, "last_order_ts": 0.0}, None, emit)


def run(args: Dict[str, Any]) -> None:
    """Compatibility shim for argc runner."""
    main(args)


if __name__ == "__main__":
    main(parse_argv(sys.argv[1:]))
