#!/usr/bin/env python3
"""
Bybit API Base Module (Professional Edition - Tor-Ready)
Provides: api_request, Technical Indicators (RSI, EMA, ATR),
Risk Management, Tor Integration, and Connectivity Utilities.
"""

import os
import sys
import json
import time
import hmac
import hashlib
import math
import socket
import re
import requests
import urllib.parse
import logging
from typing import Dict, Any, Optional, List, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# --------------------------------------------------------------------------
# Custom Exceptions
# --------------------------------------------------------------------------
class BybitError(Exception):
    """Base exception for Bybit API errors."""
    pass


class TorError(Exception):
    """Exception for Tor-related errors."""
    pass


# --------------------------------------------------------------------------
# Logging Setup
# --------------------------------------------------------------------------
def setup_logging(config: Dict[str, Any]) -> None:
    log_level = logging.INFO
    logger = logging.getLogger("bybit")
    logger.setLevel(log_level)
    if not logger.handlers:
        console_handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            "\033[96m[BYBIT] %(levelname)s: %(message)s\033[0m"
        )
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)


# --------------------------------------------------------------------------
# Tor Configuration
# --------------------------------------------------------------------------
TOR_SOCKS_HOST: str = os.environ.get("TOR_SOCKS_HOST", "127.0.0.1")
TOR_SOCKS_PORT: int = int(os.environ.get("TOR_SOCKS_PORT", "9050"))
TOR_CTRL_HOST: str = os.environ.get("TOR_CTRL_HOST", "127.0.0.1")
TOR_CTRL_PORT: int = int(os.environ.get("TOR_CTRL_PORT", "9051"))

# socks5h:// — DNS resolved on Tor exit node (prevents DNS leaks)
_SOCKS5H_PROXY: str = f"socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}"
TOR_PROXIES: Dict[str, str] = {
    "http": _SOCKS5H_PROXY,
    "https": _SOCKS5H_PROXY,
}

# Tor-aware timeouts (wider due to Tor latency)
TOR_CONNECT_TIMEOUT: float = float(os.environ.get("TOR_CONNECT_TIMEOUT", "15"))
TOR_READ_TIMEOUT: float = float(os.environ.get("TOR_READ_TIMEOUT", "30"))
TOR_TIMEOUT: Tuple[float, float] = (TOR_CONNECT_TIMEOUT, TOR_READ_TIMEOUT)

# Neutral User-Agent (avoids fingerprinting)
_TOR_USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"

# Symbol validation regex
SYMBOL_PATTERN = re.compile(r'^[A-Z]{2,10}USDT?$')

# Minimum wait between NEWNYM signals (Tor enforces 10s minimum)
MIN_NEWNYM_INTERVAL: float = float(os.environ.get("MIN_NEWNYM_INTERVAL", "12"))


# --------------------------------------------------------------------------
# Tor Utilities
# --------------------------------------------------------------------------
def _socks_port_open() -> bool:
    """Check if Tor SOCKS port is accepting connections."""
    try:
        with socket.create_connection((TOR_SOCKS_HOST, TOR_SOCKS_PORT), timeout=5):
            return True
    except OSError:
        return False


def wait_for_tor_bootstrap(timeout_s: float = 120.0) -> None:
    """Block until Tor is bootstrapped or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _socks_port_open():
            logger = logging.getLogger("bybit")
            logger.info("Tor SOCKS port is open; assuming bootstrapped.")
            return
        time.sleep(2)
    raise TorError(f"Tor did not bootstrap within {timeout_s} seconds.")


def verify_tor_connection() -> str:
    """
    Verify traffic is routing through Tor.
    Returns exit node IP or raises TorError.
    """
    if not _socks_port_open():
        raise TorError(f"Tor SOCKS port {TOR_SOCKS_HOST}:{TOR_SOCKS_PORT} not reachable.")

    check_url = "https://check.torproject.org/api/ip"
    try:
        resp = requests.get(
            check_url,
            proxies=TOR_PROXIES,
            timeout=TOR_TIMEOUT,
            headers={"User-Agent": _TOR_USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise TorError(f"Tor check request failed: {exc}") from exc

    if not data.get("IsTor", False):
        raise TorError(f"Traffic NOT routing through Tor. IP: {data.get('IP', 'unknown')}")

    exit_ip = data.get("IP", "hidden")
    logger = logging.getLogger("bybit")
    logger.info(f"Tor connection confirmed. Exit node: {exit_ip}")
    return exit_ip


def renew_tor_circuit() -> bool:
    """
    Request a fresh Tor circuit via NEWNYM signal.
    Requires stem library and Tor control port.
    """
    try:
        from stem import Signal
        from stem.control import Controller
    except ImportError:
        logger = logging.getLogger("bybit")
        logger.warning("stem not installed; circuit renewal disabled.")
        return False

    try:
        ctrl = Controller.from_port(address=TOR_CTRL_HOST, port=TOR_CTRL_PORT)
        ctrl.authenticate()
        ctrl.signal(Signal.NEWNYM)
        ctrl.close()
        time.sleep(1.5)  # Allow circuit to build
        return True
    except Exception as exc:
        logger = logging.getLogger("bybit")
        logger.warning(f"Circuit renewal failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Tor-Ready Session Factory
# --------------------------------------------------------------------------
def build_tor_session() -> requests.Session:
    """
    Build a requests.Session pre-configured for Tor.
    - Uses socks5h:// proxy (DNS on exit node)
    - Neutral User-Agent
    - Retry logic for Tor circuit errors
    """
    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Attach Tor proxy
    session.proxies.update(TOR_PROXIES)

    # Neutral User-Agent
    session.headers.update({"User-Agent": _TOR_USER_AGENT})

    return session


# Global Tor-ready session
TOR_SESSION: requests.Session = build_tor_session()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def load_env() -> Dict[str, str]:
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
        os.path.expanduser("~/.config/aichat/llm-functions/.env"),
    ]
    loaded = {}
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if v.startswith("your_"):
                            continue
                        os.environ[k] = v
                        loaded[k] = v
    return loaded


def get_config() -> Dict[str, Any]:
    load_env()
    
    # Determine if using Tor
    use_tor = os.environ.get("BYBIT_USE_TOR", "false").lower() in ("true", "1", "yes")
    
    config = {
        "api_key": os.environ.get("BYBIT_API_KEY", ""),
        "api_secret": os.environ.get("BYBIT_API_SECRET", ""),
        "testnet": os.environ.get("BYBIT_TESTNET", "false").lower() in ("true", "1", "yes"),
        "recv_window": os.environ.get("BYBIT_RECV_WINDOW", "20000"),
        "base_url": "https://api.bybit.com"
        if os.environ.get("BYBIT_TESTNET", "false").lower() != "true"
        else "https://api-testnet.bybit.com",
        "use_tor": use_tor,
        "proxy": TOR_PROXIES if use_tor else None,
    }
    setup_logging(config)
    return config


# --------------------------------------------------------------------------
# API Engine (Tor-Ready)
# --------------------------------------------------------------------------
def _sign(
    api_secret: str, timestamp: str, api_key: str, recv_window: str, payload: str
) -> str:
    param_str = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(
        api_secret.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def api_request(
    method: str,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    signed: bool = False,
) -> Dict[str, Any]:
    config = get_config()
    url = f"{config['base_url']}{endpoint}"
    headers = {"Content-Type": "application/json"}

    # Sign request if needed
    if signed and config["api_key"]:
        ts = str(int(time.time() * 1000))
        rw = config["recv_window"]
        if method in ("POST", "PUT"):
            body = json.dumps(params or {}, separators=(',', ':'))
            signature = _sign(config["api_secret"], ts, config["api_key"], rw, body)
        else:
            qs = urllib.parse.urlencode(params or {})
            signature = _sign(config["api_secret"], ts, config["api_key"], rw, qs)

        headers.update(
            {
                "X-BAPI-API-KEY": config["api_key"],
                "X-BAPI-SIGN": signature,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": rw,
            }
        )

    # Use Tor session if enabled, otherwise regular requests
    session = TOR_SESSION if config["use_tor"] else requests.Session()
    proxies = TOR_PROXIES if config["use_tor"] else {}
    timeout = TOR_TIMEOUT if config["use_tor"] else 15

    max_retries = 3
    for attempt in range(max_retries):
        try:
            if method == "GET":
                resp = session.get(
                    url, params=params, headers=headers, proxies=proxies, timeout=timeout
                )
            elif method == "POST":
                resp = session.post(
                    url, data=json.dumps(params or {}, separators=(',', ':')), headers=headers, proxies=proxies, timeout=timeout
                )
            elif method == "PUT":
                resp = session.put(
                    url, data=json.dumps(params or {}, separators=(',', ':')), headers=headers, proxies=proxies, timeout=timeout
                )
            elif method == "DELETE":
                resp = session.delete(
                    url, json=params, headers=headers, proxies=proxies, timeout=timeout
                )
            else:
                return {"retCode": -1, "retMsg": f"Unsupported method: {method}"}

            return resp.json()
        except Exception as e:
            # On Tor circuit error, try renewing circuit and retrying
            if config["use_tor"] and attempt < max_retries - 1:
                logger = logging.getLogger("bybit")
                logger.warning(f"Tor circuit error: {e}. Retrying...")
                renew_tor_circuit()
                time.sleep(2)
                continue
            if attempt == max_retries - 1:
                return {"retCode": -1, "retMsg": str(e)}
            time.sleep(1)


# --------------------------------------------------------------------------
# Input Validation
# --------------------------------------------------------------------------
def validate_symbol(symbol: str) -> str:
    """Validate symbol against strict regex pattern."""
    if not symbol:
        raise ValueError("Symbol cannot be empty.")
    symbol_upper = symbol.upper()
    if not SYMBOL_PATTERN.match(symbol_upper):
        raise ValueError(f"Invalid symbol '{symbol}'. Must match ^[A-Z]{{2,10}}USDT?$")
    return symbol_upper


# --------------------------------------------------------------------------
# Technical Indicators
# --------------------------------------------------------------------------
def calculate_ema(data: List[float], period: int) -> List[float]:
    if len(data) < period:
        return []
    ema = [sum(data[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in data[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def calculate_rsi(data: List[float], period: int = 14) -> List[float]:
    if len(data) <= period:
        return []
    deltas = [data[i + 1] - data[i] for i in range(len(data) - 1)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi = []
    for i in range(period, len(deltas)):
        if avg_loss == 0:
            rs = 100
        else:
            rs = avg_gain / avg_loss
            rs = 100 - (100 / (1 + rs))
        rsi.append(rs)
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return rsi


def calculate_atr(
    highs: List[float], lows: List[float], closes: List[float], period: int = 14
) -> List[float]:
    if len(closes) <= period:
        return []
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
    atr = [sum(tr_list[:period]) / period]
    for i in range(period, len(tr_list)):
        atr.append((atr[-1] * (period - 1) + tr_list[i]) / period)
    return atr


# --------------------------------------------------------------------------
# Trading Logic
# --------------------------------------------------------------------------
def get_position_size(balance: float, risk_pct: float, stop_loss_dist: float) -> float:
    if stop_loss_dist <= 0:
        return 0
    risk_amount = balance * (risk_pct / 100)
    return risk_amount / stop_loss_dist


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(
        math.floor(value / step) * step, max(0, -int(math.floor(math.log10(step))))
    )


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- Advanced Trading Utilities ---


def calculate_fees(qty: float, price: float, fee_rate: float = 0.0006) -> float:
    """Calculate taker fees (default Bybit 0.06%)"""
    return qty * price * fee_rate


def get_breakeven_price(
    entry_price: float,
    side: str,
    qty: float,
    fee_rate: float = 0.0006,
    slippage: float = 0.0002,
) -> float:
    """
    Calculate the price needed to close in profit after open/close fees and slippage.
    """
    total_fee_rate = (fee_rate * 2) + slippage
    if side.lower() == "buy":
        return entry_price * (1 + total_fee_rate)
    else:
        return entry_price * (1 - total_fee_rate)


def check_profit_after_fees(
    entry_price: float,
    current_price: float,
    side: str,
    qty: float,
    fee_rate: float = 0.0006,
) -> Tuple[bool, float]:
    """
    Check if current price is in profit after deducting estimated fees.
    Returns (is_profitable, net_profit_usdt)
    """
    open_fee = calculate_fees(qty, entry_price, fee_rate)
    close_fee = calculate_fees(qty, current_price, fee_rate)
    total_fees = open_fee + close_fee

    if side.lower() == "buy":
        gross_profit = (current_price - entry_price) * qty
    else:
        gross_profit = (entry_price - current_price) * qty

    net_profit = gross_profit - total_fees
    return net_profit > 0, net_profit
