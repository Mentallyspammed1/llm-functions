#!/usr/bin/env python3
"""
BYBIT REALM - Production-Grade Trading System Tool for LLM Functions v3.2

Fixes in this version:
  • Added dotenv support - auto-loads .env from multiple locations
  • Added PySocks support - direct SOCKS5 proxy via PySocks library
  • New TOR_USE_PYSOCKS environment variable to control PySocks usage
  • Corrected HMAC-SHA256 signature construction (GET + POST)
  • Fixed 403 Forbidden – query string now included in GET signature payload
  • raise_for_status() moved after API-level retCode check
  • Tor tiers skip correctly when use_tor=False
  • _tier_torsocks / _tier_direct only attempted when appropriate
  • Unified API error parsing with retCode != 0 detection
  • Health check uses correct public endpoint
  • Singleton dispatcher reset support added
  • All original features preserved

Usage:
    Set environment variables BYBIT_API_KEY and BYBIT_API_SECRET before use.
    Optional: BYBIT_USE_TESTNET, TOR_ENABLED, TOR_SOCKS_PORT, TOR_USE_PYSOCKS
    
    The tool will automatically look for a .env file in:
      - The same directory as the script
      - The current working directory
      - ~/.config/bybit/.env
    
    Tor support tiers (when TOR_ENABLED=true):
      1. PySocks (socks5h via PySocks library) - if TOR_USE_PYSOCKS=true
      2. requests SOCKS5 proxy
      3. torsocks binary
      4. direct connection (fallback)
"""
from typing import Optional, List, Dict, Any, Literal, Tuple, Callable
import os
import sys
import json
import time
import math
import hmac
import logging
import hashlib
import threading
import subprocess
import shutil
import statistics
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# ── dotenv support ───────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    # Try to load .env from multiple locations
    env_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bybit.env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.expanduser("~/.config/bybit/.env"),
    ]
    for env_path in env_paths:
        if os.path.exists(env_path):
            load_dotenv(env_path)
            logging.info(f"Loaded environment from {env_path}")
            break
    else:
        # Try default load_dotenv() behavior
        load_dotenv()
except ImportError:
    pass  # dotenv not available, continue without it

# ── Optional dependencies ─────────────────────────────────────
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# Add WebSocket imports
try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False

# ── Tor support ─────────────────────────────────────────────────
try:
    import socks
    PYSOCKS_AVAILABLE = True
except ImportError:
    PYSOCKS_AVAILABLE = False


from logging.handlers import RotatingFileHandler

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
log_handler = RotatingFileHandler("trading_bot.log", maxBytes=10*1024*1024, backupCount=5)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), log_handler],
)
logger = logging.getLogger("BybitRealm")


# ─────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────
class OrderSide(str, Enum):
    BUY  = "Buy"
    SELL = "Sell"


class OrderType(str, Enum):
    LIMIT       = "Limit"
    MARKET      = "Market"
    LIMIT_MAKER = "LimitMaker"
    STOP        = "Stop"
    STOP_LIMIT  = "StopLimit"


class Category(str, Enum):
    LINEAR  = "linear"
    INVERSE = "inverse"
    SPOT    = "spot"
    OPTION  = "option"


class CircuitState(str, Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class Signal(str, Enum):
    STRONG_BUY  = "STRONG_BUY"
    BUY         = "BUY"
    NEUTRAL     = "NEUTRAL"
    SELL        = "SELL"
    STRONG_SELL = "STRONG_SELL"


class TimeInForce(str, Enum):
    GTC       = "GTC"
    IOC       = "IOC"
    FOK       = "FOK"
    POST_ONLY = "PostOnly"


class PositionIdx(int, Enum):
    ONE_WAY    = 0
    HEDGE_BUY  = 1
    HEDGE_SELL = 2


# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────
@dataclass
class TradingConfig:
    """Central configuration – all values sourced from environment variables."""

    # ── Auth ──────────────────────────────────────────────────
    api_key:    str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY",    "")) # API keys are securely loaded from environment variables or .env files
    api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", "")) # API secrets are securely loaded from environment variables or .env files

    # ── Network ───────────────────────────────────────────────
    testnet:         bool = field(default_factory=lambda: os.getenv("BYBIT_USE_TESTNET", "false").lower() == "true")
    use_tor:         bool = field(default_factory=lambda: os.getenv("TOR_ENABLED",       "false").lower() == "true")
    tor_socks_port:  int  = field(default_factory=lambda: int(os.getenv("TOR_SOCKS_PORT", "9050")))
    tor_use_pysocks: bool = field(default_factory=lambda: os.getenv("TOR_USE_PYSOCKS",   "true").lower() == "true")
    # ── PySocks Geo Routing ───────────────────────────────────
    pysocks_enabled: bool = field(default_factory=lambda: os.getenv("PYSOCKS_ENABLED",   "true").lower() == "true")
    pysocks_host:    str  = field(default_factory=lambda: os.getenv("PYSOCKS_HOST",       "127.0.0.1"))
    pysocks_port:    int  = field(default_factory=lambda: int(os.getenv("PYSOCKS_PORT",   "9050")))
    pysocks_region:  str  = field(default_factory=lambda: os.getenv("PYSOCKS_REGION",     ""))
    pysocks_global:  bool = field(default_factory=lambda: os.getenv("PYSOCKS_GLOBAL",     "false").lower() == "true")
    request_timeout: int  = 15
    max_retries:     int  = 3

    # ── Circuit Breaker ───────────────────────────────────────
    cb_failure_threshold: int   = 5
    cb_recovery_timeout:  float = 60.0
    cb_cooldown:          float = 30.0

    # ── Rate Limiting ─────────────────────────────────────────
    rate_limit_calls:  int   = 10
    rate_limit_window: float = 1.0

    # ── Risk Management ───────────────────────────────────────
    max_position_usdt:    float = 1000.0
    default_leverage:     int   = 1
    default_stop_loss:    float = 0.02
    default_take_profit:  float = 0.04
    max_orders_per_batch: int   = 20

    # ── Iceberg ───────────────────────────────────────────────
    iceberg_min_slices: int   = 3
    iceberg_max_slices: int   = 10
    iceberg_delay:      float = 0.5

    @classmethod
    def from_file(cls, path: str = "trading_config.json") -> 'TradingConfig':
        """Load configuration from a JSON file with environment variable fallback."""
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                
                settings = data.get("trading_settings", {})
                net      = data.get("network", {})
                cb       = data.get("circuit_breaker", {})
                rl       = data.get("rate_limit", {})
                
                # Support both nested and top-level keys for flexibility
                use_tor = data.get("use_tor")
                if use_tor is None:
                    use_tor = net.get("use_tor", os.getenv("TOR_ENABLED", "false").lower() == "true")
                
                tor_port = data.get("tor_socks_port")
                if tor_port is None:
                    tor_port = net.get("tor_socks_port", int(os.getenv("TOR_SOCKS_PORT", "9050")))

                return cls(
                    api_key          = data.get("api_key") or os.getenv("BYBIT_API_KEY", ""),
                    api_secret       = data.get("api_secret") or os.getenv("BYBIT_API_SECRET", ""),
                    use_tor          = bool(use_tor),
                    tor_socks_port   = int(tor_port),
                    max_retries      = net.get("max_retries", 3),
                    cb_failure_threshold = cb.get("failure_threshold", 5),
                    rate_limit_calls = rl.get("calls", 10),
                    max_position_usdt = settings.get("max_position_usdt", 1000.0),
                    default_leverage = settings.get("leverage", 1),
                )
            except Exception as e:
                logger.error(f"Error loading config from {path}: {e}")
        return cls()

    @property
    def base_url(self) -> str:
        if self.testnet:
            return "https://api-testnet.bybit.com"
        
        # Primary and backup endpoints
        endpoints = [
            "https://api.bybit.com",
            "https://api.bytick.com",
            "https://api-pro.bybit.com",
            "https://api.bybit.nl"
        ]
        
        # Use a cyclic rotation based on current time (per hour) 
        # or just stick to a robust default. 
        # For now, we'll implement a fallback mechanism in the request layer,
        # but return the primary one here.
        return "https://api.bybit.com"

    def get_endpoints(self) -> List[str]:
        """Return list of available endpoints for rotation."""
        if self.testnet:
            return ["https://api-testnet.bybit.com"]
        return [
            "https://api.bytick.com",   # Often less restricted than bybit.com
            "https://api.bybit.com",
            "https://api-pro.bybit.com",
            "https://api.bybit.nl"
        ]

    def validate(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "BYBIT_API_KEY and BYBIT_API_SECRET must be set "
                "as environment variables or in trading_config.json."
            )


class WebSocketManager:
    """Manage WebSocket connections for real-time data"""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.ws_url = "wss://stream.bybit.com/v5/public/linear" if not config.testnet else "wss://stream-testnet.bybit.com/v5/public/linear"
        self.ws = None
        self.subscriptions = {}
        self.running = False

    def connect(self):
        if not WEBSOCKET_AVAILABLE:
            raise RuntimeError("websocket-client library not installed")
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        self.running = True
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()

    def subscribe_orderbook(self, symbol: str, callback: Callable):
        """Subscribe to orderbook updates"""
        if not self.ws:
            self.connect()
        msg = {
            "op": "subscribe",
            "args": [f"orderbook.200.{symbol}"]
        }
        self.subscriptions[f"orderbook.{symbol}"] = callback
        self.ws.send(json.dumps(msg))

    def _on_message(self, ws, message):
        data = json.loads(message)
        # FIX: Properly format the f-string - use data["topic"] instead of data[topic]
        topic = data.get("topic", "")
        if topic and topic in self.subscriptions:
            callback = self.subscriptions[topic]
            callback(data.get("data", {}))
    
    def _on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")
        self.running = False
    
    def _on_close(self, ws, close_status_code, close_msg):
        logger.info("WebSocket closed, attempting reconnect in 5s...")
        time.sleep(5)
        self.connect()
    
    def _on_open(self, ws):
        logger.info("WebSocket connected")



# ─────────────────────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────────────────────
class RateLimiter:
    """Enhanced rate limiter with proper handling"""

    def __init__(self, max_calls: int, window: float) -> None:
        self._max_calls = max_calls
        self._window    = window
        self._calls: deque = deque()
        self._lock  = threading.Lock()
        self._cooldown_until = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            # Check if we're in cooldown
            if now < self._cooldown_until:
                sleep_time = self._cooldown_until - now
                logger.warning(f"Rate limit cooldown active, sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
                now = time.monotonic()

            # Clean old calls
            while self._calls and self._calls[0] <= now - self._window:
                self._calls.popleft()

            if len(self._calls) >= self._max_calls:
                sleep_for = self._window - (now - self._calls[0])
                if sleep_for > 0:
                    logger.debug("Rate limiter sleeping %.3fs", sleep_for)
                    time.sleep(sleep_for)
                    now = time.monotonic()
            self._calls.append(now)

    def set_cooldown(self, seconds: float):
        """Set a cooldown period after hitting rate limits"""
        self._cooldown_until = time.monotonic() + seconds

    @property
    def current_usage(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(1 for c in self._calls if c > now - self._window)


# ─────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────
class CircuitBreaker:
    """Three-state circuit breaker with thread-safe state transitions."""

    def __init__(
        self,
        failure_threshold: int   = 5,
        recovery_timeout:  float = 60.0,
        cooldown:          float = 30.0,
    ) -> None:
        self._threshold        = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._cooldown         = cooldown
        self._state            = CircuitState.CLOSED
        self._failure_count    = 0
        self._last_failure_ts  = 0.0
        self._lock             = threading.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        with self._lock:
            self._maybe_transition()
            if self._state == CircuitState.OPEN:
                wait = self._recovery_timeout - (time.monotonic() - self._last_failure_ts)
                raise RuntimeError(f"Circuit OPEN – retry in {max(0, wait):.1f}s")
        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc

    def reset(self) -> None:
        with self._lock:
            self._state         = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("Circuit manually reset → CLOSED")

    def _maybe_transition(self) -> None:
        if (
            self._state == CircuitState.OPEN
            and time.monotonic() - self._last_failure_ts >= self._recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info("Circuit → HALF_OPEN (testing recovery)")

    def _on_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state != CircuitState.CLOSED:
                logger.info("Circuit → CLOSED")
            self._state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count  += 1
            self._last_failure_ts = time.monotonic()
            logger.warning("Circuit failure %d/%d", self._failure_count, self._threshold)
            if (
                self._state == CircuitState.HALF_OPEN
                or self._failure_count >= self._threshold
            ):
                self._state = CircuitState.OPEN
                logger.error("Circuit → OPEN (cooldown %.0fs)", self._cooldown)
                time.sleep(self._cooldown)


# ─────────────────────────────────────────────────────────────
# TOR / NETWORK MANAGER
# ─────────────────────────────────────────────────────────────
class TorManager:
    """
    Multi-tier network layer.
    Tier 1 → SOCKS5 proxy via PySocks + requests  (only when use_tor=True)
    Tier 2 → torsocks binary                        (only when use_tor=True)
    Tier 3 → direct connection                      (always available)

    FIX: tiers 1 & 2 are skipped entirely when use_tor=False,
         preventing unnecessary failures from polluting the circuit breaker.
    """

    def __init__(
        self,
        enabled:     bool,
        socks_port:  int,
        timeout:     int,
        max_retries: int,
        use_pysocks: bool = True,
    ) -> None:
        self.enabled       = enabled
        self.socks_port    = socks_port
        self.timeout       = timeout
        self._proxy_url    = f"socks5h://127.0.0.1:{socks_port}" if enabled else None
        self._torsocks_bin = shutil.which("torsocks") if enabled else None
        self._session      = self._build_session(max_retries) if REQUESTS_AVAILABLE else None
        
        # PySocks session for direct SOCKS5 support
        self._socks_session = None
        if enabled and use_pysocks and PYSOCKS_AVAILABLE and REQUESTS_AVAILABLE:
            self._socks_session = self._build_socks_session(max_retries, socks_port)
            logger.info("PySocks SOCKS5 session initialized on port %d", socks_port)
        elif enabled and use_pysocks and not PYSOCKS_AVAILABLE:
            logger.warning("TOR_USE_PYSOCKS=true but PySocks library not installed")

    def request(
        self,
        method:    str,
        url:       str,
        headers:   dict,
        params:    Optional[dict] = None,
        json_data: Optional[dict] = None,
        signed:    bool = True, # Indicate if the request requires authentication
    ) -> dict:
        """
        Try tiers in order.
        If the request is signed OR Tor is disabled, use direct connection.
        Otherwise (public request with Tor enabled), try tiered approach.
        
        Tier order with Tor enabled and public request:
          1. PySocks (socks5h via PySocks library)
          2. requests SOCKS5 proxy
          3. torsocks binary
          4. direct connection
        """
        
        # If request is signed OR Tor is disabled, use direct connection exclusively.
        # Signed requests MUST be direct. Public requests use proxy if enabled.
        if signed or not self.enabled:
            tiers = [self._tier_direct]
        else:
            # Use tiered approach for public requests with Tor enabled
            tiers = [self._tier_pysocks, self._tier_proxy, self._tier_torsocks, self._tier_direct]

        last_exc: Optional[Exception] = None
        for tier in tiers:
            try:
                # The 'signed' parameter is not passed to the tier methods themselves,
                # but used here to determine which tier to use.
                return tier(method, url, headers, params, json_data)
            except Exception as exc:
                last_exc = exc
                logger.warning("Network tier %s failed: %s", tier.__name__, exc)

        raise ConnectionError(
            f"All network tiers exhausted. Last error: {last_exc}"
        )

    # ── internal helpers ─────────────────────────────────────
    @staticmethod
    def _build_socks_session(max_retries: int, socks_port: int):
        """Build a session with PySocks for direct SOCKS5 proxy support."""
        session = requests.Session()
        
        # Configure retry strategy
        retry = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        # Set up SOCKS5 proxy using PySocks
        session.proxies = {
            "http": f"socks5h://127.0.0.1:{socks_port}",
            "https": f"socks5h://127.0.0.1:{socks_port}",
        }
        
        return session

    @staticmethod
    def _parse_response(resp) -> dict:
        """Fix: Check HTTP status first, then parse JSON"""
        if not resp.ok:
            try:
                data = resp.json()
                ret_code = data.get("retCode", 0)
                if ret_code != 0:
                    raise RuntimeError(
                        f"Bybit API error HTTP {resp.status_code} retCode={ret_code}: "
                        f"{data.get('retMsg', 'unknown')}"
                    )
            except (ValueError, RuntimeError):
                resp.raise_for_status()
                raise

        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            raise

        ret_code = data.get("retCode", 0)
        if ret_code != 0:
            raise RuntimeError(
                f"Bybit API error retCode={ret_code}: {data.get('retMsg', 'unknown')}"
            )
        return data

    def _tier_pysocks(self, method, url, headers, params, json_data) -> dict:
        """Tier 1: Direct SOCKS5 proxy via PySocks library."""
        if not PYSOCKS_AVAILABLE:
            raise RuntimeError("PySocks (socks) library not available")
        if not REQUESTS_AVAILABLE:
            raise RuntimeError("requests library not available")
        if not self._socks_session:
            raise RuntimeError("SOCKS session not initialized")
        
        resp = self._socks_session.request(
            method, url,
            headers=headers, params=params, json=json_data,
            timeout=self.timeout,
        )
        return self._parse_response(resp)

    def _tier_proxy(self, method, url, headers, params, json_data) -> dict:
        if not REQUESTS_AVAILABLE:
            raise RuntimeError("requests library not available")
        proxies = {"http": self._proxy_url, "https": self._proxy_url}
        resp = self._session.request(
            method, url,
            headers=headers, params=params, json=json_data,
            proxies=proxies, timeout=self.timeout,
        )
        return self._parse_response(resp)

    def _tier_torsocks(self, method, url, headers, params, json_data) -> dict:
        if not self._torsocks_bin:
            raise RuntimeError("torsocks binary not found")

        cmd = [
            self._torsocks_bin, "curl", "-s",
            "-X", method,
        ]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if json_data:
            cmd += ["-d", json.dumps(json_data, separators=(",", ":"))]
        if params:
            qs  = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
        cmd.append(url)

        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=self.timeout + 5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"torsocks curl failed: {result.stderr.strip()}")
        if not result.stdout.strip():
            raise RuntimeError("torsocks curl returned empty response")

        data     = json.loads(result.stdout)
        ret_code = data.get("retCode", 0)
        if ret_code != 0:
            raise RuntimeError(
                f"Bybit API error retCode={ret_code}: {data.get('retMsg', 'unknown')}"
            )
        return data

    def _tier_direct(self, method, url, headers, params, json_data) -> dict:
        if not REQUESTS_AVAILABLE:
            raise RuntimeError("requests library not available")
        resp = self._session.request(
            method, url,
            headers=headers, params=params, json=json_data,
            timeout=self.timeout,
        )
        return self._parse_response(resp)

    @staticmethod
    def _build_session(max_retries: int):
        session = requests.Session()
        retry   = Retry(
            total=max_retries,
            backoff_factor=0.5,
            # 403 intentionally excluded – retrying a bad signature wastes quota
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://",  adapter)
        return session


# ─────────────────────────────────────────────────────────────
# INSTRUMENT / PRICE FILTER CACHE
# ─────────────────────────────────────────────────────────────
@dataclass
class LotSizeFilter:
    qty_step:      float
    min_order_qty: float
    max_order_qty: float
    min_notional:  float = 0.0

    def adjust(self, qty: float) -> float:
        if self.qty_step <= 0:
            return qty
        precision = max(0, -int(math.floor(math.log10(self.qty_step))))
        adjusted  = round(round(qty / self.qty_step) * self.qty_step, precision)
        return float(min(self.max_order_qty, adjusted))


@dataclass
class PriceFilter:
    tick_size: float
    min_price: float = 0.0
    max_price: float = 1e12

    def adjust(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        precision = max(0, -int(math.floor(math.log10(self.tick_size))))
        adjusted  = round(round(price / self.tick_size) * self.tick_size, precision)
        return float(max(self.min_price, min(self.max_price, adjusted)))


@dataclass
class InstrumentInfo:
    lot_size:  LotSizeFilter
    price_flt: PriceFilter
    symbol:    str
    status:    str   = "Trading"
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_stale(self) -> bool:
        return time.time() - self.fetched_at > 3600


# ─────────────────────────────────────────────────────────────
# MARKET INTELLIGENCE DATACLASSES
# ─────────────────────────────────────────────────────────────
@dataclass
class MomentumResult:
    symbol:       str
    imbalance:    float
    signal:       Signal
    buy_vol:      float
    sell_vol:     float
    vwap:         float = 0.0
    avg_trade_sz: float = 0.0
    timestamp:    float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol":       self.symbol,
            "imbalance":    round(self.imbalance,    4),
            "signal":       self.signal.value,
            "buy_vol":      round(self.buy_vol,      4),
            "sell_vol":     round(self.sell_vol,     4),
            "vwap":         round(self.vwap,         4),
            "avg_trade_sz": round(self.avg_trade_sz, 4),
            "timestamp":    self.timestamp,
        }


@dataclass
class PnLReport:
    symbol:       str
    total_pnl:    float
    win_count:    int
    loss_count:   int
    win_rate:     float
    avg_win:      float
    avg_loss:     float
    largest_win:  float
    largest_loss: float
    total_fees:   float
    trade_count:  int

    def to_dict(self) -> dict:
        return {
            "symbol":       self.symbol,
            "total_pnl":    round(self.total_pnl,    4),
            "win_count":    self.win_count,
            "loss_count":   self.loss_count,
            "win_rate":     round(self.win_rate,     4),
            "avg_win":      round(self.avg_win,      4),
            "avg_loss":     round(self.avg_loss,     4),
            "largest_win":  round(self.largest_win,  4),
            "largest_loss": round(self.largest_loss, 4),
            "total_fees":   round(self.total_fees,   4),
            "trade_count":  self.trade_count,
        }


# ─────────────────────────────────────────────────────────────
# MAIN DISPATCHER
# ─────────────────────────────────────────────────────────────
class BybitToolDispatcher:
    """Central dispatcher for all Bybit API interactions."""

    _RECV_WINDOW = "5000"

    def __init__(self, config: TradingConfig) -> None:
        config.validate()
        self.config  = config
        self.tor     = TorManager(
            enabled     = config.use_tor,
            socks_port  = config.tor_socks_port,
            timeout     = config.request_timeout,
            max_retries = config.max_retries,
            use_pysocks = config.tor_use_pysocks,
        )
        self.circuit = CircuitBreaker(
            failure_threshold = config.cb_failure_threshold,
            recovery_timeout  = config.cb_recovery_timeout,
            cooldown          = config.cb_cooldown,
        )
        self.limiter = RateLimiter(
            max_calls = config.rate_limit_calls,
            window    = config.rate_limit_window,
        )
        self._instr_cache: Dict[str, InstrumentInfo] = {}
        self._cache_lock   = threading.Lock()

    # ══════════════════════════════════════════════════════════
    # AUTH & REQUEST
    # ══════════════════════════════════════════════════════════
    def _sign(self, payload: str, timestamp: str) -> str:
        """
        FIX: Bybit V5 signature format:
             HMAC-SHA256( timestamp + api_key + recv_window + payload )
        """
        raw = f"{timestamp}{self.config.api_key}{self._RECV_WINDOW}{payload}"
        return hmac.new(
            self.config.api_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_get_query(self, params: dict) -> str:
        """Fix: Use proper ampersand character and handle boolean serialization"""
        def format_val(v):
            if isinstance(v, bool):
                return str(v).lower()
            return str(v)
        return "&".join(f"{k}={format_val(v)}" for k, v in sorted(params.items()))

    def api_request(
        self,
        method:    str,
        endpoint:  str,
        params:    Optional[dict] = None,
        json_data: Optional[dict] = None,
        signed:    bool = True,
    ) -> dict:
        self.limiter.acquire()

        # Normalization: ensure symbol is uppercase and category is string value
        if params:
            if "symbol" in params and isinstance(params["symbol"], str):
                params["symbol"] = params["symbol"].upper()
            if "category" in params and isinstance(params["category"], (Category, Enum)):
                params["category"] = params["category"].value

        if json_data:
            if "symbol" in json_data and isinstance(json_data["symbol"], str):
                json_data["symbol"] = json_data["symbol"].upper()
            if "category" in json_data and isinstance(json_data["category"], (Category, Enum)):
                json_data["category"] = json_data["category"].value

        ts  = str(int(time.time() * 1000))

        # ── Build the payload string used for signing ─────────
        if method == "POST":
            # Manually construct sorted JSON to guarantee no spaces
            json_str = json.dumps(json_data, sort_keys=True, separators=(",", ":"))
            payload_str = json_str
        else:
            # GET: sign the sorted query string
            payload_str = self._build_get_query(params or {})

        logger.debug("Signature payload_str: %s", payload_str)

        # Only add auth headers for signed requests
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if signed:
            headers.update({
                "X-BAPI-API-KEY":     self.config.api_key,
                "X-BAPI-TIMESTAMP":   ts,
                "X-BAPI-RECV-WINDOW": self._RECV_WINDOW,
                "X-BAPI-SIGN":        self._sign(payload_str, ts),
            })

        # Endpoint Rotation Logic
        endpoints = self.config.get_endpoints()
        last_exc: Optional[Exception] = None

        for base_url in endpoints:
            url = f"{base_url}{endpoint}"
            try:
                logger.debug("%s %s params=%s signed=%s", method, url, params, signed)
                return self.circuit.call(
                    self.tor.request,
                    method, url, headers,
                    params    if method == "GET"  else None,
                    json_data if method == "POST" else None,
                    signed=signed # Pass 'signed' to TorManager.request
                )
            except Exception as exc:
                last_exc = exc
                # If 403 or connection issue, try next endpoint
                if "403" in str(exc) or "404" not in str(exc):
                    logger.warning("Endpoint %s failed: %s. Trying next...", base_url, exc)
                    continue
                raise

        raise ConnectionError(f"All API endpoints exhausted. Last error: {last_exc}")

    # ══════════════════════════════════════════════════════════
    # INSTRUMENT / LOT-SIZE + PRICE FILTER
    # ══════════════════════════════════════════════════════════
    def _fetch_instrument(self, symbol: str, category: str) -> InstrumentInfo:
        """Fetch and cache instrument info – thread-safe with TTL."""
        with self._cache_lock:
            info = self._instr_cache.get(symbol)
            if info and not info.is_stale:
                return info

        logger.info("Fetching instrument info for %s …", symbol)
        resp = self.api_request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": category, "symbol": symbol},
            signed=False,
        )
        try:
            item = resp["result"]["list"][0]
            lot  = item["lotSizeFilter"]
            pft  = item.get("priceFilter", {})

            lsf = LotSizeFilter(
                qty_step      = float(lot.get("qtyStep") or lot.get("basePrecision", 1)),
                min_order_qty = float(lot["minOrderQty"]),
                max_order_qty = float(lot.get("maxOrderQty", 1e9)),
                min_notional  = float(lot.get("minNotionalValue", 0)),
            )
            pf = PriceFilter(
                tick_size = float(pft.get("tickSize", 0.01)),
                min_price = float(pft.get("minPrice", 0)),
                max_price = float(pft.get("maxPrice", 1e12)),
            )
            info = InstrumentInfo(
                lot_size  = lsf,
                price_flt = pf,
                symbol    = symbol,
                status    = item.get("status", "Trading"),
                fetched_at = time.time()
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                f"Could not parse instrument info for {symbol}: {exc}"
            ) from exc

        with self._cache_lock:
            self._instr_cache[symbol] = info

        return info

    def adjust_quantity(
        self,
        symbol:   str,
        qty:      float,
        category: str = Category.LINEAR,
    ) -> float:
        info     = self._fetch_instrument(symbol, category)
        adjusted = info.lot_size.adjust(qty)
        logger.debug("%s qty %.8f → %.8f", symbol, qty, adjusted)
        return adjusted

    def adjust_price(
        self,
        symbol:   str,
        price:    float,
        category: str = Category.LINEAR,
    ) -> float:
        info     = self._fetch_instrument(symbol, category)
        adjusted = info.price_flt.adjust(price)
        logger.debug("%s price %.8f → %.8f", symbol, price, adjusted)
        return adjusted

    # ══════════════════════════════════════════════════════════
    # SINGLE ORDER
    # ══════════════════════════════════════════════════════════
    def place_order(
        self,
        symbol:        str,
        side:          OrderSide,
        qty:           float,
        price:         Optional[float] = None,
        order_type:    OrderType        = OrderType.LIMIT,
        category:      Category         = Category.LINEAR,
        stop_loss:     Optional[float]  = None,
        take_profit:   Optional[float]  = None,
        reduce_only:   bool             = False,
        time_in_force: TimeInForce      = TimeInForce.GTC,
        position_idx:  PositionIdx      = PositionIdx.ONE_WAY,
        client_oid:    Optional[str]    = None,
        trailing_stop: Optional[float]  = None,
    ) -> dict:
        # Convert qty, price, sl, and tp to float if passed as string; handle empty strings as None
        qty = float(qty) if qty is not None else 0.0

        def safe_float(val) -> Optional[float]:
            if val is None or str(val).strip() == "":
                return None
            return float(val)

        price       = safe_float(price)
        stop_loss   = safe_float(stop_loss)
        take_profit = safe_float(take_profit)
        trailing_stop = safe_float(trailing_stop)
        # Ensure adj_qty is initialized
        adj_qty = self.adjust_quantity(symbol, qty, category)

        payload: Dict[str, Any] = {
            "category":    category,
            "symbol":      symbol,
            "side":        side,
            "orderType":   order_type,
            "qty":         str(adj_qty),
            "timeInForce": time_in_force,
            "positionIdx": int(position_idx),
        }

        if price is not None:
            payload["price"] = str(self.adjust_price(symbol, price, category))
        if stop_loss is not None:
            payload["stopLoss"]    = str(self.adjust_price(symbol, stop_loss,   category))
        if take_profit is not None:
            payload["takeProfit"]  = str(self.adjust_price(symbol, take_profit, category))
        if trailing_stop is not None:
            payload["trailingStop"] = str(trailing_stop)
        if reduce_only:
            payload["reduceOnly"] = True
        if client_oid:
            payload["orderLinkId"] = client_oid

        logger.info(
            "Placing %s %s %s @ %s qty=%s",
            category, side, symbol, price or "MARKET", adj_qty,
        )
        return self.api_request("POST", "/v5/order/create", json_data=payload)

    # ══════════════════════════════════════════════════════════
    # BATCH ORDERS
    # ══════════════════════════════════════════════════════════
    def execute_scalp_batch(self, order_list: List[dict]) -> dict:
        """Place up to 20 orders in a single API call."""
        if not order_list:
            raise ValueError("order_list must not be empty")
        if len(order_list) > self.config.max_orders_per_batch:
            raise ValueError(
                f"Bybit batch API supports max "
                f"{self.config.max_orders_per_batch} orders per request"
            )

        batch = []
        for o in order_list:
            cat     = o.get("category", Category.LINEAR)
            adj_qty = self.adjust_quantity(o["symbol"], float(o["qty"]), cat)
            entry: Dict[str, Any] = {
                "category":    cat,
                "symbol":      o["symbol"],
                "side":        o["side"],
                "orderType":   o.get("orderType", OrderType.LIMIT),
                "qty":         str(adj_qty),
                "timeInForce": o.get("timeInForce", TimeInForce.GTC),
            }
            if "price" in o:
                entry["price"] = str(
                    self.adjust_price(o["symbol"], float(o["price"]), cat)
                )
            if "stopLoss" in o:
                entry["stopLoss"] = str(
                    self.adjust_price(o["symbol"], float(o["stopLoss"]), cat)
                )
            if "takeProfit" in o:
                entry["takeProfit"] = str(
                    self.adjust_price(o["symbol"], float(o["takeProfit"]), cat)
                )
            if "orderLinkId" in o:
                entry["orderLinkId"] = o["orderLinkId"]
            batch.append(entry)

        logger.info("Submitting batch of %d orders …", len(batch))
        return self.api_request(
            "POST",
            "/v5/order/create-batch",
            json_data={"category": Category.LINEAR, "request": batch},
        )

    # ══════════════════════════════════════════════════════════
    # ICEBERG ORDERS
    # ══════════════════════════════════════════════════════════
    def place_iceberg_order(
        self,
        symbol:      str,
        side:        OrderSide,
        total_qty:   float,
        price:       float,
        slices:      int             = 5,
        category:    Category        = Category.LINEAR,
        stop_loss:   Optional[float] = None,
        take_profit: Optional[float] = None,
        delay:       Optional[float] = None,
    ) -> List[dict]:
        slices = max(
            self.config.iceberg_min_slices,
            min(self.config.iceberg_max_slices, slices),
        )
        delay     = delay if delay is not None else self.config.iceberg_delay
        slice_qty = total_qty / slices
        results   = []

        logger.info(
            "Iceberg: %s %s %s total=%.4f in %d slices @ %.4f",
            category, side, symbol, total_qty, slices, price,
        )
        for i in range(slices):
            result = self.safe_execute(
                self.place_order,
                symbol      = symbol,
                side        = side,
                qty         = slice_qty,
                price       = price,
                category    = category,
                stop_loss   = stop_loss   if i == 0 else None,
                take_profit = take_profit if i == 0 else None,
                client_oid  = f"iceberg_{symbol}_{int(time.time())}_{i}",
            )
            results.append({"slice": i + 1, "result": result})
            logger.info("Iceberg slice %d/%d placed", i + 1, slices)
            if i < slices - 1:
                time.sleep(delay)

        return results

    # ══════════════════════════════════════════════════════════
    # CANCEL / AMEND ORDERS
    # ══════════════════════════════════════════════════════════
    def cancel_order(
        self,
        symbol:   str,
        order_id: str,
        category: Category = Category.LINEAR,
    ) -> dict:
        return self.api_request(
            "POST",
            "/v5/order/cancel",
            json_data={"category": category, "symbol": symbol, "orderId": order_id},
        )

    def amend_order(
        self,
        symbol:      str,
        order_id:    str,
        qty:         Optional[float] = None,
        price:       Optional[float] = None,
        category:    Category = Category.LINEAR,
        stop_loss:   Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        """Amend an existing order."""
        payload: Dict[str, Any] = {
            "category": category,
            "symbol":   symbol,
            "orderId":  order_id,
        }
        if qty: payload["qty"] = str(self.adjust_quantity(symbol, qty, category))
        if price: payload["price"] = str(self.adjust_price(symbol, price, category))
        if stop_loss: payload["stopLoss"] = str(self.adjust_price(symbol, stop_loss, category))
        if take_profit: payload["takeProfit"] = str(self.adjust_price(symbol, take_profit, category))
        
        return self.api_request("POST", "/v5/order/amend", json_data=payload)

    def get_order_history(self, symbol: Optional[str] = None, category: Category = Category.LINEAR, limit: int = 50) -> List[dict]:
        """Fetch historical orders."""
        params: Dict[str, Any] = {"category": category, "limit": limit}
        if symbol: params["symbol"] = symbol.upper()
        resp = self.api_request("GET", "/v5/order/history", params=params, signed=True)
        return resp.get("result", {}).get("list", [])

    def calculate_position_size(self, symbol: str, entry_price: float, sl_price: float, risk_usdt: float, category: Category = Category.LINEAR) -> float:
        """Calculate quantity for a risk-defined position."""
        risk_per_unit = abs(entry_price - sl_price)
        if risk_per_unit == 0: return 0.0
        qty = risk_usdt / risk_per_unit
        return self.adjust_quantity(symbol, qty, category)

    def amend_order(
        self,
        symbol:      str,
        order_id:    str,
        qty:         Optional[float] = None,
        price:       Optional[float] = None,
        category:    Category = Category.LINEAR,
        stop_loss:   Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        payload: Dict[str, Any] = {
            "category": category,
            "symbol":   symbol,
            "orderId":  order_id,
        }
        if qty is not None:
            payload["qty"]        = str(self.adjust_quantity(symbol, qty,         category))
        if price is not None:
            payload["price"]      = str(self.adjust_price(symbol,    price,       category))
        if stop_loss is not None:
            payload["stopLoss"]   = str(self.adjust_price(symbol,    stop_loss,   category))
        if take_profit is not None:
            payload["takeProfit"] = str(self.adjust_price(symbol,    take_profit, category))
        return self.api_request("POST", "/v5/order/amend", json_data=payload)

    def get_open_orders(
        self,
        symbol:   Optional[str] = None,
        category: Category = Category.LINEAR,
        limit:    int = 50,
    ) -> List[dict]:
        """Fetch open orders using unified dispatcher."""
        params: Dict[str, Any] = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol.upper()
        
        # Use signed=True as open orders are private account data
        resp = self.api_request("GET", "/v5/order/realtime", params=params, signed=True)
        return resp.get("result", {}).get("list", [])

    # ══════════════════════════════════════════════════════════
    # POSITIONS & BALANCE
    # ══════════════════════════════════════════════════════════
    def get_positions(
        self,
        category: Category = Category.LINEAR,
        symbol:   Optional[str] = None,
    ) -> List[dict]:
        params: Dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        resp = self.api_request("GET", "/v5/position/list", params=params)
        return resp.get("result", {}).get("list", [])

    def get_wallet_balance(self, account_type: str = "UNIFIED") -> dict:
        resp = self.api_request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": account_type},
        )
        return resp.get("result", {})

    def set_leverage(
        self,
        symbol:        str,
        leverage:      int,
        category:      Category = Category.LINEAR,
        buy_leverage:  Optional[int] = None,
        sell_leverage: Optional[int] = None,
    ) -> dict:
        payload = {
            "category":     category,
            "symbol":       symbol,
            "buyLeverage":  str(buy_leverage  or leverage),
            "sellLeverage": str(sell_leverage or leverage),
        }
        return self.api_request(
            "POST", "/v5/position/set-leverage", json_data=payload
        )

    def set_trading_stop(
        self,
        symbol:        str,
        stop_loss:     Optional[float] = None,
        take_profit:   Optional[float] = None,
        trailing_stop: Optional[float] = None,
        category:      Category = Category.LINEAR,
        position_idx:  PositionIdx = PositionIdx.ONE_WAY,
    ) -> dict:
        payload: Dict[str, Any] = {
            "category":    category,
            "symbol":      symbol,
            "positionIdx": int(position_idx),
        }
        if stop_loss is not None:
            payload["stopLoss"]     = str(self.adjust_price(symbol, stop_loss,   category))
        if take_profit is not None:
            payload["takeProfit"]   = str(self.adjust_price(symbol, take_profit, category))
        if trailing_stop is not None:
            payload["trailingStop"] = str(trailing_stop)
        return self.api_request(
            "POST", "/v5/position/trading-stop", json_data=payload
        )

    def get_pnl_history(
        self,
        symbol:   Optional[str] = None,
        category: Category = Category.LINEAR,
        limit:    int = 100,
    ) -> List[dict]:
        params: Dict[str, Any] = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        resp = self.api_request("GET", "/v5/position/closed-pnl", params=params)
        return resp.get("result", {}).get("list", [])

    def get_pnl_report(
        self,
        symbol:   str,
        category: Category = Category.LINEAR,
        limit:    int = 100,
    ) -> PnLReport:
        records = self.get_pnl_history(symbol=symbol, category=category, limit=limit)
        if not records:
            return PnLReport(
                symbol=symbol, total_pnl=0, win_count=0, loss_count=0,
                win_rate=0, avg_win=0, avg_loss=0, largest_win=0,
                largest_loss=0, total_fees=0, trade_count=0,
            )
        pnls   = [float(r.get("closedPnl", 0)) for r in records]
        fees   = [float(r.get("cumExecFee", 0)) for r in records]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return PnLReport(
            symbol       = symbol,
            total_pnl    = sum(pnls),
            win_count    = len(wins),
            loss_count   = len(losses),
            win_rate     = len(wins) / len(pnls) if pnls else 0,
            avg_win      = statistics.mean(wins)   if wins   else 0,
            avg_loss     = statistics.mean(losses) if losses else 0,
            largest_win  = max(wins)               if wins   else 0,
            largest_loss = min(losses)             if losses else 0,
            total_fees   = sum(fees),
            trade_count  = len(pnls),
        )

    def get_order_history(
        self,
        symbol:   Optional[str] = None,
        category: Category = Category.LINEAR,
        limit:    int = 50,
    ) -> List[dict]:
        params: Dict[str, Any] = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        resp = self.api_request("GET", "/v5/order/history", params=params)
        return resp.get("result", {}).get("list", [])

    # ══════════════════════════════════════════════════════════
    # MARKET DATA
    # ══════════════════════════════════════════════════════════
    def get_ticker(
        self,
        symbol:   str,
        category: Category = Category.LINEAR,
    ) -> dict:
        resp = self.api_request(
            "GET",
            "/v5/market/tickers",
            params={"category": category, "symbol": symbol},
            signed=False,
        )
        items = resp.get("result", {}).get("list", [])
        return items[0] if items else {}

    def get_orderbook(
        self,
        symbol:   str,
        limit:    int      = 25,
        category: Category = Category.LINEAR,
    ) -> dict:
        return self.api_request(
            "GET",
            "/v5/market/orderbook",
            params={"category": category, "symbol": symbol, "limit": limit},
            signed=False,
        )

    def get_klines(
        self,
        symbol:   str,
        interval: str      = "1",
        limit:    int      = 200,
        category: Category = Category.LINEAR,
    ) -> List[list]:
        # Normalize interval strings
        mapping = {
            "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
            "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
            "1d": "D", "1w": "W", "1M": "M"
        }
        interval = mapping.get(str(interval).lower(), interval)

        resp = self.api_request(
            "GET",
            "/v5/market/kline",
            params={
                "category": category,
                "symbol":   symbol,
                "interval": interval,
                "limit":    limit,
            },
            signed=False,
        )
        return resp.get("result", {}).get("list", [])

    def get_recent_trades(
        self,
        symbol:   str,
        limit:    int      = 500,
        category: Category = Category.LINEAR,
    ) -> List[dict]:
        resp = self.api_request(
            "GET",
            "/v5/market/recent-trade",
            params={"category": category, "symbol": symbol, "limit": limit},
            signed=False,
        )
        return resp.get("result", {}).get("list", [])

    def get_open_interest(
        self,
        symbol:        str,
        interval_time: str      = "5min",
        category:      Category = Category.LINEAR,
        limit:         int      = 50,
    ) -> List[dict]:
        resp = self.api_request(
            "GET",
            "/v5/market/open-interest",
            params={
                "category":     category,
                "symbol":       symbol,
                "intervalTime": interval_time,
                "limit":        limit,
            },
            signed=False,
        )
        return resp.get("result", {}).get("list", [])

    def get_liquidations(
        self,
        symbol:   str,
        category: Category = Category.LINEAR,
        limit:    int      = 200,
    ) -> List[dict]:
        """Fetch recent liquidation data from the recent-trade endpoint."""
        resp = self.api_request(
            "GET",
            "/v5/market/recent-trade",
            params={
                "category": category,
                "symbol": symbol,
                "limit": limit,
                "tickType": "Liquidation"
            },
            signed=False,
        )
        return resp.get("result", {}).get("list", [])

    # ══════════════════════════════════════════════════════════
    # ADVANCED TECHNICAL INDICATORS
    # ══════════════════════════════════════════════════════════
    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """Calculate Relative Strength Index (RSI)."""
        if len(prices) < period + 1:
            return 50.0
        
        deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        gains  = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calculate_ema(self, prices: List[float], period: int = 20) -> float:
        """Calculate Exponential Moving Average (EMA)."""
        if len(prices) < period:
            return prices[-1]
        
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def calculate_atr(self, ohlcv_list: List[dict], period: int = 14) -> float:
        """Calculate Average True Range (ATR)."""
        if len(ohlcv_list) < period + 1:
            return 0.0
            
        tr_list = []
        for i in range(1, len(ohlcv_list)):
            high = float(ohlcv_list[i]['high'])
            low = float(ohlcv_list[i]['low'])
            prev_close = float(ohlcv_list[i-1]['close'])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)
            
        return sum(tr_list[-period:]) / period

    def calculate_bollinger_bands(self, prices: List[float], period: int = 20, std_dev: float = 2.0) -> dict:
        """Calculate Bollinger Bands."""
        if len(prices) < period:
            return {"upper": 0.0, "middle": 0.0, "lower": 0.0}
        
        subset = prices[-period:]
        mean = sum(subset) / period
        variance = sum((x - mean) ** 2 for x in subset) / period
        std = math.sqrt(variance)
        
        return {
            "middle": round(mean, 4),
            "upper": round(mean + (std * std_dev), 4),
            "lower": round(mean - (std * std_dev), 4)
        }

    def calculate_macd(self, prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
        """Calculate MACD series and signal line."""
        if len(prices) < slow + signal:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

        def get_ema_series(data, p):
            k = 2 / (p + 1)
            ema_list = [data[0]]
            for val in data[1:]:
                ema_list.append(val * k + ema_list[-1] * (1 - k))
            return ema_list

        fast_ema = get_ema_series(prices, fast)
        slow_ema = get_ema_series(prices, slow)
        
        # Build MACD line series
        macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
        
        # Signal line is EMA of MACD line
        signal_line_series = get_ema_series(macd_line, signal)
        
        cur_macd = macd_line[-1]
        cur_signal = signal_line_series[-1]
        
        return {
            "macd": round(cur_macd, 4),
            "signal": round(cur_signal, 4),
            "histogram": round(cur_macd - cur_signal, 4)
        }

    # ══════════════════════════════════════════════════════════
    # TREND ANALYSIS ENGINE
    # ══════════════════════════════════════════════════════════

    def get_trend_analysis(
        self,
        symbol:   str,
        category: Category = Category.LINEAR,
        interval: str      = "60",
        lookback_periods: int = 200,
        include_advanced_indicators: bool = True
    ) -> dict:
        """
        Advanced Multi-Indicator Trend Analysis with Consensus Scoring.
        Combines MAs, MACD, RSI, Volatility, and Order Flow into a weighted score.
        """
        try:
            # 1. Data Collection (Current Timeframe)
            # Use limit=200 for stability and sufficient lookback for EMA200
            klines = self.get_klines(symbol, interval=interval, limit=lookback_periods, category=category)
            if not klines or len(klines) < 50:
                count = len(klines) if klines else 0
                return {"status": "error", "msg": f"Insufficient kline data for {symbol} (found {count}, need >=50)"}

            # Bybit returns newest first, reverse to oldest -> newest for indicator calculation
            klines.reverse()

            closes = [float(k[4]) for k in klines]
            highs  = [float(k[2]) for k in klines]
            lows   = [float(k[3]) for k in klines]
            vols   = [float(k[5]) for k in klines]
            
            current_price = closes[-1]
            
            # 2. Technical Indicators
            ema9   = self.calculate_ema(closes, period=9)
            ema21  = self.calculate_ema(closes, period=21)
            ema50  = self.calculate_ema(closes, period=50)
            ema200 = self.calculate_ema(closes, period=200)
            
            rsi   = self.calculate_rsi(closes, period=14)
            macd  = self.calculate_macd(closes)
            bb    = self.calculate_bollinger_bands(closes)
            
            ohlcv_list = [{'high': h, 'low': l, 'close': c} for h, l, c in zip(highs, lows, closes)]
            atr   = self.calculate_atr(ohlcv_list, period=14)
            
            momentum = self.get_market_momentum(symbol, category=category)
            
            # 3. Scoring System (-100 to +100)
            score = 0
            
            # MA Trend (30%)
            ma_score = 0
            if current_price > ema21: ma_score += 15
            else: ma_score -= 15
            if ema9 > ema21: ma_score += 15
            else: ma_score -= 15
            score += ma_score
            
            # MACD Trend (20%)
            macd_score = 0
            if macd['macd'] > macd['signal']: macd_score += 10
            else: macd_score -= 10
            if macd['macd'] > 0: macd_score += 10
            else: macd_score -= 10
            score += macd_score
            
            # RSI Momentum (15%)
            rsi_score = 0
            if 40 <= rsi <= 60: rsi_score = 0
            elif rsi > 60: rsi_score = 15 if rsi < 80 else 5
            elif rsi < 40: rsi_score = -15 if rsi > 20 else -5
            score += rsi_score
            
            # Volume Confirmation (10%)
            vol_avg = sum(vols[-20:]) / 20
            vol_score = 0
            if vols[-1] > vol_avg:
                if (closes[-1] > closes[-2]): vol_score += 10
                else: vol_score -= 10
            score += vol_score
            
            # Order Flow (10%)
            flow_score = 0
            sig = momentum.get('signal', 'NEUTRAL')
            if "STRONG_BUY" in sig: flow_score += 10
            elif "BUY" in sig: flow_score += 5
            elif "STRONG_SELL" in sig: flow_score -= 10
            elif "SELL" in sig: flow_score -= 5
            score += flow_score
            
            # Price Action / Alignment (15%)
            pa_score = 0
            if current_price > ema50: pa_score += 7.5
            else: pa_score -= 7.5
            if ema50 > ema200: pa_score += 7.5
            else: pa_score -= 7.5
            score += pa_score

            # 4. Multi-Timeframe Check (Consistency)
            mtf_alignment = "NOT_CHECKED"
            # If current is 1h, check 4h
            if interval == "60" or interval == "1h":
                klines_4h = self.get_klines(symbol, interval="240", limit=50, category=category)
                if klines_4h:
                    closes_4h = [float(k[4]) for k in klines_4h]
                    ema20_4h = self.calculate_ema(closes_4h, 20)
                    if (current_price > ema20_4h and score > 0): mtf_alignment = "ALIGNED_BULLISH"
                    elif (current_price < ema20_4h and score < 0): mtf_alignment = "ALIGNED_BEARISH"
                    else: mtf_alignment = "MIXED"

            # 5. Classification
            if score >= 60:   trend = "STRONG_BULLISH"
            elif score >= 20:  trend = "BULLISH"
            elif score <= -60: trend = "STRONG_BEARISH"
            elif score <= -20: trend = "BEARISH"
            else:             trend = "NEUTRAL"

            # 6. Risk Metrics (ATR-based)
            stop_loss   = current_price - (atr * 2.0) if score >= 0 else current_price + (atr * 2.0)
            take_profit = current_price + (atr * 4.0) if score >= 0 else current_price - (atr * 4.0)

            # 7. Guidance
            advice = "WAIT"
            if trend == "STRONG_BULLISH": advice = "STRONG_BUY"
            elif trend == "BULLISH": 
                advice = "BUY_ON_DIP" if current_price <= ema9 * 1.005 else "HOLD_LONG"
            elif trend == "STRONG_BEARISH": advice = "STRONG_SELL"
            elif trend == "BEARISH":
                advice = "SELL_ON_RALLY" if current_price >= ema9 * 0.995 else "HOLD_SHORT"

            if rsi > 75: advice = "TAKE_PROFIT / AVOID_LONG"
            if rsi < 25: advice = "WATCH_FOR_REVERSAL / AVOID_SHORT"

            result = {
                "symbol":         symbol,
                "interval":       interval,
                "score":          round(score, 2),
                "trend":          trend,
                "mtf_alignment":  mtf_alignment,
                "current_price":  round(current_price, 4),
                "indicators": {
                    "rsi":   round(rsi, 2),
                    "macd":  macd,
                    "ema9":  round(ema9, 4),
                    "ema21": round(ema21, 4),
                    "ema50": round(ema50, 4),
                    "ema200": round(ema200, 4),
                    "atr":   round(atr, 4),
                    "bb":    bb
                },
                "risk_guidance": {
                    "suggested_stop_loss":   round(stop_loss, 4),
                    "suggested_take_profit": round(take_profit, 4),
                    "risk_reward_ratio":     2.0
                },
                "action_advice":  advice,
                "timestamp":      time.time()
            }
            
            if include_advanced_indicators:
                result["advanced"] = {
                    "adx": self.calculate_adx(highs, lows, closes),
                    "cci": self.calculate_cci(highs, lows, closes),
                    "stoch_rsi": self.calculate_stoch_rsi(closes)
                }
            return result
        except Exception as e:
            logger.error("Trend analysis failed: %s", e)
            return {"status": "error", "msg": str(e)}

    def calculate_stoch_rsi(self, prices: List[float], period: int = 14) -> dict:
        """Calculate Stochastic RSI."""
        if len(prices) < period:
            return {"stoch_rsi": 0.0, "k": 0.0, "d": 0.0}
            
        rsi_values = []
        for i in range(period, len(prices) + 1):
            rsi_values.append(self.calculate_rsi(prices[i-period:i], period=period))
            
        rsi_min = min(rsi_values)
        rsi_max = max(rsi_values)
        
        if rsi_max == rsi_min:
            stoch_rsi = 0.0
        else:
            stoch_rsi = (rsi_values[-1] - rsi_min) / (rsi_max - rsi_min)
            
        return {"stoch_rsi": round(stoch_rsi, 4), "rsi": round(rsi_values[-1], 2)}

    def calculate_cci(self, highs: List[float], lows: List[float], closes: List[float], period: int = 20) -> float:
        """Calculate Commodity Channel Index (CCI)."""
        if len(closes) < period:
            return 0.0
            
        tp = [(h + l + c) / 3 for h, l, c in zip(highs[-period:], lows[-period:], closes[-period:])]
        sma = sum(tp) / period
        mean_dev = sum(abs(x - sma) for x in tp) / period
        
        return round((tp[-1] - sma) / (0.015 * mean_dev) if mean_dev != 0 else 0.0, 2)

    def calculate_donchian_channels(self, highs: List[float], lows: List[float], period: int = 20) -> dict:
        """Calculate Donchian Channels."""
        if len(highs) < period:
            return {"upper": 0.0, "lower": 0.0}
        upper = max(highs[-period:])
        lower = min(lows[-period:])
        return {"upper": round(upper, 4), "lower": round(lower, 4)}

    def calculate_adx(self, highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """Calculate Average Directional Index (ADX)."""
        if len(closes) < period * 2:
            return 0.0
        
        # Simplification: Calculate TR, +DM, -DM for the period
        tr_list, pos_dm, neg_dm = [], [], []
        for i in range(1, len(closes)):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            pd = (highs[i]-highs[i-1]) if (highs[i]-highs[i-1]) > (lows[i-1]-lows[i]) else 0
            nd = (lows[i-1]-lows[i]) if (lows[i-1]-lows[i]) > (highs[i]-highs[i-1]) else 0
            tr_list.append(tr); pos_dm.append(pd); neg_dm.append(nd)

        # Smooth (simple average used here for brevity)
        atr = sum(tr_list[-period:]) / period
        adx = 100 * abs(sum(pos_dm[-period:]) - sum(neg_dm[-period:])) / (sum(pos_dm[-period:]) + sum(neg_dm[-period:]) + 1e-9)
        return round(adx, 2)

    def calculate_fib_pivots(self, high: float, low: float, close: float) -> dict:
        """Calculate standard Fibonacci Pivot Points (R3, R2, R1, P, S1, S2, S3)."""
        pivot = (high + low + close) / 3
        range_val = high - low
        
        return {
            "R3": round(pivot + (range_val * 1.0), 4),
            "R2": round(pivot + (range_val * 0.618), 4),
            "R1": round(pivot + (range_val * 0.382), 4),
            "P":  round(pivot, 4),
            "S1": round(pivot - (range_val * 0.382), 4),
            "S2": round(pivot - (range_val * 0.618), 4),
            "S3": round(pivot - (range_val * 1.0), 4)
        }



    def get_funding_rate(
        self,
        symbol:   str,
        category: Category = Category.LINEAR,
    ) -> dict:
        resp = self.api_request(
            "GET",
            "/v5/market/funding/history",
            params={"category": category, "symbol": symbol, "limit": 1},
            signed=False,
        )
        items = resp.get("result", {}).get("list", [])
        return items[0] if items else {}

    # ══════════════════════════════════════════════════════════
    # RISK HELPERS
    # ══════════════════════════════════════════════════════════
    def calculate_sl_tp(
        self,
        entry_price: float,
        side:        OrderSide,
        sl_pct:      Optional[float] = None,
        tp_pct:      Optional[float] = None,
    ) -> Tuple[float, float]:
        sl_pct = sl_pct if sl_pct is not None else self.config.default_stop_loss
        tp_pct = tp_pct if tp_pct is not None else self.config.default_take_profit
        if side == OrderSide.BUY:
            sl = round(entry_price * (1 - sl_pct), 8)
            tp = round(entry_price * (1 + tp_pct), 8)
        else:
            sl = round(entry_price * (1 + sl_pct), 8)
            tp = round(entry_price * (1 - tp_pct), 8)
        logger.debug("SL/TP %s entry=%.4f sl=%.4f tp=%.4f", side, entry_price, sl, tp)
        return sl, tp

    def calculate_position_size(
        self,
        symbol:      str,
        entry_price: float,
        sl_price:    float,
        risk_usdt:   float,
        category:    Category = Category.LINEAR,
    ) -> float:
        price_diff = abs(entry_price - sl_price)
        if price_diff == 0:
            return 0.0
        raw_qty = risk_usdt / price_diff
        return self.adjust_quantity(symbol, raw_qty, category)

    def calculate_position_risk(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        account_balance: float,
        risk_percentage: float = 0.02
    ) -> dict:
        """Calculate optimal position size based on risk"""
        # Calculate risk per unit
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit == 0:
            return {"error": "Stop loss equals entry price"}
        # Calculate maximum risk amount
        max_risk = account_balance * risk_percentage
        # Calculate position size
        position_size = max_risk / risk_per_unit
        # Adjust for lot size
        adjusted_size = self.adjust_quantity(symbol, position_size)
        # Calculate actual risk
        actual_risk = adjusted_size * risk_per_unit
        risk_percentage_actual = actual_risk / account_balance if account_balance > 0 else 0

        return {
            "symbol": symbol,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "position_size": adjusted_size,
            "max_risk": max_risk,
            "actual_risk": actual_risk,
            "risk_percentage": risk_percentage_actual,
            "risk_per_unit": risk_per_unit
        }

    # ══════════════════════════════════════════════════════════
    # SAFE EXECUTE
    # ══════════════════════════════════════════════════════════
    def safe_execute(
        self,
        fn:          Callable,
        *args,
        max_retries: int   = 3,
        base_delay:  float = 1.0,
        **kwargs,
    ) -> Any:
        """Execute fn with exponential backoff; aborts when circuit is OPEN."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except RuntimeError as exc:
                if "Circuit OPEN" in str(exc):
                    logger.error("Circuit OPEN – aborting safe_execute: %s", exc)
                    return {"status": "circuit_open", "msg": str(exc)}
                last_exc = exc
                delay    = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Attempt %d/%d failed: %s – retrying in %.1fs",
                    attempt, max_retries, exc, delay,
                )
                time.sleep(delay)
            except Exception as exc:
                last_exc = exc
                delay    = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Attempt %d/%d failed: %s – retrying in %.1fs",
                    attempt, max_retries, exc, delay,
                )
                time.sleep(delay)

        logger.error("All %d attempts exhausted. Last: %s", max_retries, last_exc)
        return {"status": "error", "msg": str(last_exc)}

    # ══════════════════════════════════════════════════════════
    # DIAGNOSTICS
    # ══════════════════════════════════════════════════════════
    def health_check(self) -> dict:
        """
        FIX: /v5/market/time is a public endpoint that does NOT require
        signing – previously the signed=True default was causing 403s
        on health checks when keys were invalid.
        
        Enhanced with PySocks Geo Routing info.
        """
        try:
            resp = self.api_request(
                "GET", "/v5/market/time",
                signed=False,
            )
            
            # Get geo IP info if PySocks is available
            geo_ip = "N/A"
            geo_location = {"error": "PySocks not available"}
            if PYSOCKS_AVAILABLE and self.config.pysocks_enabled:
                try:
                    geo_router = PySocksGeoRouter(
                        proxy_host=self.config.pysocks_host,
                        proxy_port=self.config.pysocks_port,
                        rdns=True
                    )
                    geo_ip = geo_router.get_public_ip()
                    geo_location = geo_router.get_geo_location()
                    geo_router.close()
                except Exception:
                    pass
            
            return {
                "status":            "ok",
                "circuit":           self.circuit.state.value,
                "circuit_fails":     self.circuit.failure_count,
                "rate_usage":        self.limiter.current_usage,
                "server_time":       resp.get("result", {}).get("timeNano"),
                "base_url":          self.config.base_url,
                "tor_enabled":       self.config.use_tor,
                "tor_use_pysocks":   self.config.tor_use_pysocks,
                "pysocks_available": PYSOCKS_AVAILABLE,
                "pysocks_enabled":   self.config.pysocks_enabled,
                "pysocks_host":      self.config.pysocks_host,
                "pysocks_port":      self.config.pysocks_port,
                "pysocks_region":    self.config.pysocks_region,
                "pysocks_global":    self.config.pysocks_global,
                "geo_ip":           geo_ip,
                "geo_location":    geo_location,
                "testnet":          self.config.testnet,
                "cache_symbols":    list(self._instr_cache.keys()),
            }
        except Exception as exc:
            return {"status": "error", "msg": str(exc)}


# ─────────────────────────────────────────────────────────────
# SINGLETON DISPATCHER
# ─────────────────────────────────────────────────────────────
_dispatcher: Optional[BybitToolDispatcher] = None
_disp_lock   = threading.Lock()


def _get_dispatcher() -> BybitToolDispatcher:
    """Lazily create the singleton dispatcher – thread-safe double-checked locking."""
    global _dispatcher
    if _dispatcher is None:
        with _disp_lock:
            if _dispatcher is None:
                # Load config from file if it exists, otherwise use defaults
                config = TradingConfig.from_file("trading_config.json")
                _dispatcher = BybitToolDispatcher(config)
    return _dispatcher


def _reset_dispatcher() -> None:
    """Force recreation of the singleton (useful after config changes)."""
    global _dispatcher
    with _disp_lock:
        _dispatcher = None


# ─────────────────────────────────────────────────────────────
# TOOL ENTRY POINT
# ─────────────────────────────────────────────────────────────
def run(
    action: Literal[
        "health_check",
        "place_order",
        "amend_order",
        "cancel_order",
        "cancel_all_orders",
        "get_open_orders",
        "get_order_history",
        "get_positions",
        "get_wallet_balance",
        "set_leverage",
        "set_trading_stop",
        "get_ticker",
        "get_orderbook",
        "get_klines",
        "get_recent_trades",
        "get_open_interest",
        "get_liquidations",
        "get_market_momentum",
        "get_market_health",
        "get_funding_rate",
        "calculate_bollinger_bands",
        "calculate_macd",
        "calculate_stoch_rsi",
        "calculate_cci",
        "calculate_donchian_channels",
        "calculate_adx",
        "calculate_kelly_criterion",
        "calculate_fib_pivots",
        "calculate_trade_pnl",
        "calculate_profit_target",
        "calculate_sl_tp",
        "calculate_position_size",
        "get_pnl_history",
        "get_pnl_report",
        "batch_orders",
        "iceberg_order",
        "reset_circuit",
    ],
    # ── Order fields ──────────────────────────────────────────
    symbol:         Optional[str]   = None,
    side:           Optional[Literal["Buy", "Sell"]] = None,
    qty:            Optional[float] = None,
    price:          Optional[float] = None,
    order_type:     Optional[Literal["Limit", "Market", "LimitMaker", "Stop", "StopLimit"]] = None,
    category:       Optional[Literal["linear", "inverse", "spot", "option"]] = None,
    order_id:       Optional[str]   = None,
    stop_loss:      Optional[float] = None,
    take_profit:    Optional[float] = None,
    trailing_stop:  Optional[float] = None,
    reduce_only:    Optional[bool]  = False,
    time_in_force:  Optional[Literal["GTC", "IOC", "FOK", "PostOnly"]] = None,
    position_idx:   Optional[int]   = None,
    client_oid:     Optional[str]   = None,
    # ── Account ───────────────────────────────────────────────
    leverage:       Optional[int]   = None,
    buy_leverage:   Optional[int]   = None,
    sell_leverage:  Optional[int]   = None,
    account_type:   Optional[str]   = "UNIFIED",
    # ── Market data ───────────────────────────────────────────
    limit:          Optional[int]   = 25,
    interval:       Optional[str]   = "1",
    interval_time:  Optional[str]   = "5min",
    # ── Momentum ──────────────────────────────────────────────
    strong_threshold: Optional[float] = 0.20,
    mild_threshold:   Optional[float] = 0.08,
    # ── Risk helpers ──────────────────────────────────────────
    sl_pct:         Optional[float] = None,
    tp_pct:         Optional[float] = None,
    risk_usdt:      Optional[float] = None,
    sl_price:       Optional[float] = None,
    # ── Batch / Iceberg ───────────────────────────────────────
    orders:         Optional[List[Dict[str, Any]]] = None,
    slices:         Optional[int]   = 5,
    delay:          Optional[float] = None,
) -> dict:
    """
    Bybit Trading Tool – Execute any supported trading operation.

    Args:
        action: Operation to perform (e.g., 'place_order', 'get_ticker', 'get_trend_analysis', 'get_wallet_balance')
        symbol: Trading symbol (e.g., 'BTCUSDT')
        side: Order side ('Buy' or 'Sell')
        qty: Order quantity
        price: Limit price (also used as entry_price for sl/tp calc)
        order_type: Order type (e.g., 'Limit', 'Market', 'Stop')
        category: Product category (e.g., 'linear', 'spot', 'inverse')
        order_id: Existing order ID for amend/cancel
        stop_loss: Stop loss price
        take_profit: Take profit price
        trailing_stop: Trailing stop distance in price units
        reduce_only: Whether the order is reduce-only
        time_in_force: Time in force (e.g., 'GTC', 'PostOnly')
        position_idx: Position index (0=one-way, 1=hedge-buy, 2=hedge-sell)
        client_oid: Client order link ID
        leverage: Leverage for the position
        buy_leverage: Independent buy-side leverage
        sell_leverage: Independent sell-side leverage
        account_type: Account type (e.g., 'UNIFIED', 'CONTRACT')
        limit: Result count for list endpoints
        interval: Kline interval (e.g., '1', '60', 'D')
        interval_time: Open interest interval (e.g., '5min', '1h')
        strong_threshold: Momentum strong-signal cutoff
        mild_threshold: Momentum mild-signal cutoff
        sl_pct: Stop-loss as decimal fraction of entry
        tp_pct: Take-profit as decimal fraction of entry
        risk_usdt: Max USDT risk for position sizing
        sl_price: Explicit SL price for position sizing
        orders: List of order dicts for batch operations
        slices: Number of slices for iceberg orders
        delay: Seconds between iceberg slices
    """
    bot = _get_dispatcher()

    try:
        # Handle category - default to LINEAR
        if category and str(category).strip():
            cat = Category(str(category).strip())
        else:
            cat = Category.LINEAR
        
        # Handle time_in_force - default to GTC
        if time_in_force and str(time_in_force).strip():
            tif = TimeInForce(str(time_in_force).strip())
        else:
            tif = TimeInForce.GTC
        
        # Handle position_idx: accept int or string representation of int
        pidx = PositionIdx.ONE_WAY
        if position_idx is not None:
            try:
                pidx = PositionIdx(int(position_idx))
            except (ValueError, TypeError):
                pass

        # ── Diagnostics ───────────────────────────────────────
        if action == "health_check":
            return bot.health_check()

        elif action == "reset_circuit":
            bot.circuit.reset()
            return {"status": "ok", "msg": "Circuit breaker reset to CLOSED"}

        # ── Place Order ───────────────────────────────────────
        elif action == "place_order":
            if not symbol or not side or qty is None:
                return {"status": "error", "msg": "symbol, side, and qty are required"}
            return bot.place_order(
                symbol        = symbol,
                side          = OrderSide(side),
                qty           = qty,
                price         = price,
                order_type    = OrderType(order_type or "Limit"),
                category      = cat,
                stop_loss     = stop_loss,
                take_profit   = take_profit,
                reduce_only   = reduce_only or False,
                time_in_force = tif,
                position_idx  = pidx,
                client_oid    = client_oid,
                trailing_stop = trailing_stop,
            )

        # ── Amend Order ───────────────────────────────────────
        elif action == "amend_order":
            if not symbol or not order_id:
                return {"status": "error", "msg": "symbol and order_id are required"}
            return bot.amend_order(
                symbol=symbol, order_id=order_id, qty=qty, price=price, 
                category=cat, stop_loss=stop_loss, take_profit=take_profit
            )

        elif action == "get_order_history":
            return {
                "orders": bot.get_order_history(symbol=symbol, category=cat, limit=limit or 50)
            }
            
        elif action == "calculate_position_size":
            if not symbol or price is None or sl_price is None or risk_usdt is None:
                return {"status": "error", "msg": "symbol, price, sl_price, and risk_usdt are required"}
            return {
                "symbol": symbol,
                "quantity": bot.calculate_position_size(symbol, price, sl_price, risk_usdt, cat)
            }

        # ── Cancel ────────────────────────────────────────────
        elif action == "cancel_order":
            if not symbol or not order_id:
                return {"status": "error", "msg": "symbol and order_id are required"}
            return bot.cancel_order(symbol=symbol, order_id=order_id, category=cat)

        elif action == "cancel_all_orders":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.cancel_all_orders(symbol=symbol, category=cat)

        # ── Order Queries ─────────────────────────────────────
        elif action == "get_open_orders":
            return {
                "orders": bot.get_open_orders(
                    symbol=symbol, category=cat, limit=limit or 50
                )
            }

        elif action == "get_order_history":
            return {
                "orders": bot.get_order_history(
                    symbol=symbol, category=cat, limit=limit or 50
                )
            }

        # ── Positions ─────────────────────────────────────────
        elif action == "get_positions":
            return {"positions": bot.get_positions(category=cat, symbol=symbol)}

        # ── Balance ───────────────────────────────────────────
        elif action == "get_wallet_balance":
            return bot.get_wallet_balance(account_type=account_type or "UNIFIED")

        # ── Leverage ──────────────────────────────────────────
        elif action == "set_leverage":
            if not symbol or leverage is None:
                return {"status": "error", "msg": "symbol and leverage are required"}
            return bot.set_leverage(
                symbol        = symbol,
                leverage      = leverage,
                category      = cat,
                buy_leverage  = buy_leverage,
                sell_leverage = sell_leverage,
            )

        # ── Trading Stop ──────────────────────────────────────
        elif action == "set_trading_stop":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.set_trading_stop(
                symbol        = symbol,
                stop_loss     = stop_loss,
                take_profit   = take_profit,
                trailing_stop = trailing_stop,
                category      = cat,
                position_idx  = pidx,
            )

        # ── Market Data ───────────────────────────────────────
        elif action == "get_ticker":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_ticker(symbol=symbol, category=cat)

        elif action == "get_orderbook":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_orderbook(symbol=symbol, limit=limit or 25, category=cat)

        elif action == "get_klines":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return {
                "klines": bot.get_klines(
                    symbol=symbol, interval=interval or "1",
                    limit=limit or 200, category=cat,
                )
            }

        elif action == "get_recent_trades":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return {
                "trades": bot.get_recent_trades(
                    symbol=symbol, limit=limit or 500, category=cat
                )
            }

        elif action == "get_open_interest":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return {
                "open_interest": bot.get_open_interest(
                    symbol=symbol, interval_time=interval_time or "5min",
                    category=cat, limit=limit or 50,
                )
            }

        elif action == "get_liquidations":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return {
                "liquidations": bot.get_liquidations(
                    symbol=symbol, category=cat, limit=limit or 200
                )
            }

        # ── Market Intelligence ───────────────────────────────
        elif action == "get_market_momentum":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_market_momentum(
                symbol           = symbol,
                category         = cat,
                strong_threshold = strong_threshold or 0.20,
                mild_threshold   = mild_threshold   or 0.08,
            )

        elif action == "get_trend_analysis":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_trend_analysis(
                symbol   = symbol,
                category = cat,
                interval = interval or "60",
                lookback_periods = limit or 200,
                include_advanced_indicators = True
            )

        elif action == "get_market_health":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_market_health(symbol=symbol, category=cat)

        elif action == "get_funding_rate":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_funding_rate(symbol=symbol, category=cat)

        # ── Risk Helpers ──────────────────────────────────────
        elif action == "calculate_bollinger_bands":
            try:
                import ast
                prices = ast.literal_eval(str(qty)) if qty else []
                # Use --period (passed as 'limit') and --std-dev (passed as 'sl_pct')
                period = limit or 20
                std = sl_pct or 2.0
                return bot.calculate_bollinger_bands(prices, period=period, std_dev=std)
            except Exception as e:
                return {"status": "error", "msg": f"Invalid prices list: {e}"}

        elif action == "calculate_macd":
            try:
                import ast
                prices = ast.literal_eval(str(qty)) if qty else []
                return bot.calculate_macd(prices)
            except Exception as e:
                return {"status": "error", "msg": f"Invalid prices list: {e}"}

        elif action == "calculate_stoch_rsi":
            try:
                import ast
                prices = ast.literal_eval(str(qty)) if qty else []
                return bot.calculate_stoch_rsi(prices, period=limit or 14)
            except Exception as e:
                return {"status": "error", "msg": f"Invalid prices list: {e}"}

        elif action == "calculate_cci":
            try:
                import ast
                # Expecting qty='[highs]', price='[lows]', sl_price='[closes]'
                highs = ast.literal_eval(str(qty)) if qty else []
                lows = ast.literal_eval(str(price)) if price else []
                closes = ast.literal_eval(str(sl_price)) if sl_price else []
                return {"cci": bot.calculate_cci(highs, lows, closes)}
            except Exception as e:
                return {"status": "error", "msg": f"Invalid lists: {e}"}

        elif action == "calculate_donchian_channels":
            try:
                import ast
                highs = ast.literal_eval(str(qty)) if qty else []
                lows = ast.literal_eval(str(price)) if price else []
                return bot.calculate_donchian_channels(highs, lows)
            except Exception as e:
                return {"status": "error", "msg": f"Invalid lists: {e}"}

        elif action == "calculate_adx":
            try:
                import ast
                highs = ast.literal_eval(str(qty)) if qty else []
                lows = ast.literal_eval(str(price)) if price else []
                closes = ast.literal_eval(str(sl_price)) if sl_price else []
                return {"adx": bot.calculate_adx(highs, lows, closes)}
            except Exception as e:
                return {"status": "error", "msg": f"Invalid lists: {e}"}

        elif action == "calculate_kelly_criterion":
            if sl_pct is None or tp_pct is None:
                return {"status": "error", "msg": "sl_pct (win_rate) and tp_pct (win/loss ratio) are required"}
            return {"kelly_fraction": bot.calculate_kelly_criterion(sl_pct, tp_pct)}

        elif action == "calculate_fib_pivots":
            if price is None or qty is None or sl_price is None:
                return {"status": "error", "msg": "High (price), Low (qty), and Close (sl_price) are required"}
            return bot.calculate_fib_pivots(high=price, low=qty, close=sl_price)

        elif action == "calculate_trade_pnl":
            if price is None or qty is None or sl_price is None or not side:
                return {"status": "error", "msg": "entry (price), exit (sl_price), qty, and side are required"}
            return bot.calculate_trade_pnl(entry=price, exit=sl_price, qty=qty, side=side)

        elif action == "calculate_profit_target":
            if price is None or sl_price is None:
                return {"status": "error", "msg": "price (entry) and sl_price are required"}
            return bot.calculate_profit_target(entry_price=price, sl_price=sl_price)

        elif action == "calculate_sl_tp":
            if not side or price is None:
                return {"status": "error", "msg": "side and price are required"}
            sl, tp = bot.calculate_sl_tp(
                entry_price = price,
                side        = OrderSide(side),
                sl_pct      = sl_pct,
                tp_pct      = tp_pct,
            )
            return {
                "symbol":      symbol,
                "entry_price": price,
                "side":        side,
                "stop_loss":   sl,
                "take_profit": tp,
                "sl_pct":      sl_pct or bot.config.default_stop_loss,
                "tp_pct":      tp_pct or bot.config.default_take_profit,
            }

        elif action == "calculate_position_size":
            if not symbol or price is None or sl_price is None or risk_usdt is None:
                return {
                    "status": "error",
                    "msg":    "symbol, price, sl_price, and risk_usdt are required",
                }
            qty_out = bot.calculate_position_size(
                symbol      = symbol,
                entry_price = price,
                sl_price    = sl_price,
                risk_usdt   = risk_usdt,
                category    = cat,
            )
            return {
                "symbol":      symbol,
                "entry_price": price,
                "sl_price":    sl_price,
                "risk_usdt":   risk_usdt,
                "quantity":    qty_out,
            }

        # ── PnL ───────────────────────────────────────────────
        elif action == "get_pnl_history":
            return {
                "pnl_history": bot.get_pnl_history(
                    symbol=symbol, category=cat, limit=limit or 100
                )
            }

        elif action == "get_pnl_report":
            if not symbol:
                return {"status": "error", "msg": "symbol is required"}
            return bot.get_pnl_report(
                symbol=symbol, category=cat, limit=limit or 100
            ).to_dict()

        # ── Batch Orders ──────────────────────────────────────
        elif action == "batch_orders":
            if not orders:
                return {"status": "error", "msg": "orders list is required"}
            return bot.safe_execute(bot.execute_scalp_batch, orders)

        # ── Iceberg Order ─────────────────────────────────────
        elif action == "iceberg_order":
            if not symbol or not side or qty is None or price is None:
                return {
                    "status": "error",
                    "msg":    "symbol, side, qty, and price are required",
                }
            results = bot.place_iceberg_order(
                symbol      = symbol,
                side        = OrderSide(side),
                total_qty   = qty,
                price       = price,
                slices      = int(slices) if slices else 5,
                category    = cat,
                stop_loss   = stop_loss,
                take_profit = take_profit,
                delay       = float(delay) if delay is not None else 0.5,
            )
            return {"status": "ok", "iceberg_results": results}

        else:
            return {
                "status": "error",
                "msg":    f"Unknown action: '{action}'. "
                          "Valid actions: health_check, place_order, amend_order, "
                          "cancel_order, cancel_all_orders, get_open_orders, "
                          "get_order_history, get_positions, get_wallet_balance, "
                          "set_leverage, set_trading_stop, get_ticker, get_orderbook, "
                          "get_klines, get_recent_trades, get_open_interest, "
                          "get_liquidations, get_market_momentum, get_funding_rate, "
                          "calculate_sl_tp, calculate_position_size, get_pnl_history, "
                          "get_pnl_report, batch_orders, iceberg_order, reset_circuit",
            }

    except Exception as exc:
        logger.exception("run() unhandled exception for action=%s", action)
        return {"status": "error", "msg": str(exc)}


# ─────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Bybit Trading Tool – CLI Interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bybit-realm.py --action health_check
  python bybit-realm.py --action get_ticker        --symbol BTCUSDT --category spot
  python bybit-realm.py --action get_market_momentum --symbol BTCUSDT
  python bybit-realm.py --action get_wallet_balance --account-type UNIFIED
  python bybit-realm.py --action get_positions     --category linear
  python bybit-realm.py --action calculate_sl_tp   --side Buy  --price 65000
  python bybit-realm.py --action get_pnl_report    --symbol BTCUSDT
  python bybit-realm.py --action get_klines        --symbol BTCUSDT --interval 15 --limit 100
  python bybit-realm.py --action get_orderbook     --symbol BTCUSDT --limit 10
        """,
    )

    # ── Configuration ─────────────────────────────────────────
    parser.add_argument("--config", default="trading_config.json", help="Path to JSON configuration file")
    
    # ── Core ──────────────────────────────────────────────────
    parser.add_argument("--action",         required=True,                help="Action to perform")
    parser.add_argument("--symbol",                                        help="Trading symbol e.g. BTCUSDT")
    parser.add_argument("--side",                                          help="Buy | Sell")
    parser.add_argument("--qty",            type=float,                    help="Order quantity")
    parser.add_argument("--price",          type=float,                    help="Order / entry price")
    parser.add_argument("--order-type",     dest="order_type",             help="Limit | Market | LimitMaker | Stop | StopLimit")
    parser.add_argument("--category",       default="linear",              help="linear | inverse | spot | option")
    parser.add_argument("--order-id",       dest="order_id",               help="Order ID")
    parser.add_argument("--stop-loss",      dest="stop_loss",  type=float, help="Stop loss price")
    parser.add_argument("--take-profit",    dest="take_profit",type=float, help="Take profit price")
    parser.add_argument("--trailing-stop",  dest="trailing_stop",type=float,help="Trailing stop distance")
    parser.add_argument("--reduce-only",    dest="reduce_only",action="store_true")
    parser.add_argument("--time-in-force",  dest="time_in_force",default="GTC", help="GTC | IOC | FOK | PostOnly")
    parser.add_argument("--position-idx",   dest="position_idx",type=int,default=0)
    parser.add_argument("--client-oid",     dest="client_oid",             help="Client order link ID")

    # ── Account ───────────────────────────────────────────────
    parser.add_argument("--leverage",       type=int,                      help="Leverage")
    parser.add_argument("--buy-leverage",   dest="buy_leverage",  type=int)
    parser.add_argument("--sell-leverage",  dest="sell_leverage", type=int)
    parser.add_argument("--account-type",   dest="account_type",  default="UNIFIED")

    # ── Market data ───────────────────────────────────────────
    parser.add_argument("--limit",          type=int, default=25)
    parser.add_argument("--interval",       default="1",                   help="Kline interval")
    parser.add_argument("--interval-time",  dest="interval_time",default="5min")

    # ── Momentum ──────────────────────────────────────────────
    parser.add_argument("--strong-threshold", dest="strong_threshold", type=float, default=0.20)
    parser.add_argument("--mild-threshold",   dest="mild_threshold",   type=float, default=0.08)

    # ── Risk ──────────────────────────────────────────────────
    parser.add_argument("--sl-pct",         dest="sl_pct",    type=float)
    parser.add_argument("--tp-pct",         dest="tp_pct",    type=float)
    parser.add_argument("--risk-usdt",      dest="risk_usdt", type=float)
    parser.add_argument("--sl-price",       dest="sl_price",  type=float)

    # ── Iceberg ───────────────────────────────────────────────
    parser.add_argument("--slices",         type=int,   default=5)
    parser.add_argument("--delay",          type=float)

    # ── Output ────────────────────────────────────────────────
    parser.add_argument("--output",                                        help="Output file path")
    parser.add_argument("--orders-file",    dest="orders_file",            help="JSON file with batch order list")

    args = parser.parse_args()

    # ── Initialize dispatcher with custom config ──────────────
    config = TradingConfig.from_file(args.config)
    bot = BybitToolDispatcher(config)
    
    # ── Load batch orders from file ───────────────────────────
    orders_data = None
    if getattr(args, "orders_file", None):
        with open(args.orders_file) as f:
            orders_data = json.load(f)

    result = run(
        action           = args.action,
        symbol           = args.symbol,
        side             = args.side,
        qty              = args.qty,
        price            = args.price,
        order_type       = args.order_type,
        category         = args.category,
        order_id         = args.order_id,
        stop_loss        = args.stop_loss,
        take_profit      = args.take_profit,
        trailing_stop    = args.trailing_stop,
        reduce_only      = args.reduce_only,
        time_in_force    = args.time_in_force,
        position_idx     = args.position_idx,
        client_oid       = args.client_oid,
        leverage         = args.leverage,
        buy_leverage     = args.buy_leverage,
        sell_leverage    = args.sell_leverage,
        account_type     = args.account_type,
        limit            = args.limit,
        interval         = args.interval,
        interval_time    = args.interval_time,
        strong_threshold = args.strong_threshold,
        mild_threshold   = args.mild_threshold,
        sl_pct           = args.sl_pct,
        tp_pct           = args.tp_pct,
        risk_usdt        = args.risk_usdt,
        sl_price         = args.sl_price,
        orders           = orders_data,
        slices           = args.slices,
        delay            = args.delay,
    )

    output_path = args.output or os.environ.get("LLM_OUTPUT")
    if output_path:
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        logger.info("Result written to %s", output_path)
    else:
        print(json.dumps(result, indent=2))

# ── PySocks Geo IP Routing Enhancement ───────────────────────
class PySocksGeoRouter:
    """
    Route all network requests through PySocks SOCKS5 proxy for geo IP manipulation.
    Supports multiple proxy servers for different geographic regions.
    """

    # Default SOCKS5 proxy configurations for different regions
    PROXY_CONFIGS = {
        "us_east": {"host": "127.0.0.1", "port": 9050, "rdns": True},
        "us_west": {"host": "127.0.0.1", "port": 9051, "rdns": True},
        "europe": {"host": "127.0.0.1", "port": 9052, "rdns": True},
        "asia": {"host": "127.0.0.1", "port": 9053, "rdns": True}
    }

    def __init__(
        self,
        proxy_type: int = None,
        proxy_host: str = "127.0.0.1",
        proxy_port: int = 9050,
        rdns: bool = True,
        username: Optional[str] = None,
        password: Optional[str] = None
    ):
        """Initialize PySocks router with default proxy settings."""
        try:
            import socks
            self.proxy_type = proxy_type or socks.SOCKS5
        except ImportError:
            self.proxy_type = 5  # Default to SOCKS5
        
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.rdns = rdns
        self.username = username
        self.password = password
        self._original_socket = None
        self._session = self._create_socks_session()

    def _create_socks_session(self):
        """Create a requests session with SOCKS5 proxy configured."""
        if not REQUESTS_AVAILABLE:
            return None
            
        session = requests.Session()
        proxy_url = f"socks5h://{self.proxy_host}:{self.proxy_port}"
        if self.username and self.password:
            proxy_url = f"socks5h://{self.username}:{self.password}@{self.proxy_host}:{self.proxy_port}"
        
        session.proxies = {"http": proxy_url, "https": proxy_url}
        
        retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        return session

    def set_proxy(self, proxy_host: str, proxy_port: int, proxy_type: int = None, rdns: bool = True):
        """Change the SOCKS proxy settings dynamically."""
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        if proxy_type:
            self.proxy_type = proxy_type
        self.rdns = rdns
        self._session = self._create_socks_session()

    def set_region(self, region: str):
        """Set proxy based on geographic region."""
        if region not in self.PROXY_CONFIGS:
            raise ValueError(f"Unsupported region: {region}. Available: {list(self.PROXY_CONFIGS.keys())}")
        config = self.PROXY_CONFIGS[region]
        self.set_proxy(config["host"], config["port"], rdns=config.get("rdns", True))

    def enable_global_proxy(self):
        """Enable global proxy by monkey-patching socket.socket."""
        if not PYSOCKS_AVAILABLE:
            return
            
        import socket
        import socks
        
        if self._original_socket is None:
            self._original_socket = socket.socket
            socks.set_default_proxy(socks.SOCKS5, self.proxy_host, self.proxy_port, rdns=self.rdns)
            socket.socket = socks.socksocket

    def disable_global_proxy(self):
        """Disable global proxy by restoring original socket."""
        if self._original_socket is not None:
            import socket
            socket.socket = self._original_socket
            self._original_socket = None

    def request(self, method: str, url: str, headers: Optional[dict] = None, 
                params: Optional[dict] = None, json_data: Optional[dict] = None, 
                timeout: int = 15) -> dict:
        """Make a request through the PySocks proxy."""
        if not self._session:
            raise RuntimeError("requests library not available")
        resp = self._session.request(method=method, url=url, headers=headers or {}, 
                                     params=params, json=json_data, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def get_public_ip(self) -> str:
        """Get the public IP address as seen through the proxy."""
        try:
            resp = self._session.get("https://api.ipify.org?format=json", timeout=10)
            return resp.json().get("ip", "Unknown")
        except Exception:
            return "Unknown"

    def get_geo_location(self) -> dict:
        """Get geographic location as seen through the proxy."""
        try:
            resp = self._session.get("http://ip-api.com/json/", timeout=10)
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    def close(self):
        """Clean up resources."""
        self.disable_global_proxy()
        if self._session:
            self._session.close()


def configure_pysocks_from_env():
    """Configure PySocks geo routing based on environment variables."""
    if os.getenv("PYSOCKS_ENABLED", "true").lower() != "true":
        return None

    host = os.getenv("PYSOCKS_HOST", "127.0.0.1")
    port = int(os.getenv("PYSOCKS_PORT", "9050"))
    region = os.getenv("PYSOCKS_REGION", None)
    global_proxy = os.getenv("PYSOCKS_GLOBAL", "false").lower() == "true"

    router = PySocksGeoRouter(proxy_host=host, proxy_port=port, rdns=True)

    if region:
        router.set_region(region)

    if global_proxy:
        router.enable_global_proxy()

    return router