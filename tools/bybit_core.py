# bybit_core.py
import os
import requests
import json
import time
import hmac
import hashlib
import urllib.parse
import random
import logging
from typing import Any, Dict, Mapping, Optional

# ---------------------------------------------------------------------------
# Module-level logger (gated so we never auto-spam callers' logs)
# ---------------------------------------------------------------------------
log = logging.getLogger("bybit.core")
if not log.handlers and os.environ.get("BYBIT_CORE_LOG"):
    logging.basicConfig(level=os.environ.get("BYBIT_CORE_LOG", "INFO").upper(),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
__version__ = "2.0.0"

_RECV_WINDOW_DEFAULT = 20000
_RECV_WINDOW_MAX     = 60000
_DEFAULT_TIMEOUT     = 30
_MAX_RETRIES         = 5

_BASE_URL_MAINNET = "https://api.bybit.com"
_BASE_URL_TESTNET = "https://api-testnet.bybit.com"

# Default price-scaling used by Bybit V5: price is an integer = price * 10^priceScale.
# priceScale = -log10(tickSize).  For most USDT perps tickSize = 0.001, so scale = 3.
_DEFAULT_PRICE_SCALE = 3

_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504,
                             520, 521, 522, 523, 524, 525, 526, 527, 528, 529, 530})

_RETRYABLE_RET_CODES = frozenset({
    10006, 10018, 130006, 130018, 130021, 130029, 130105, 130106, 131203,
})

_PERMANENT_RET_CODES = frozenset({
    10001, 10002, 10003, 10004, 10005, 10007, 10010, 10017,
})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def get_config() -> Dict[str, Any]:
    """Return the runtime config.

    Re-evaluated on every call so env-var changes (e.g. toggling Tor from a
    wrapper script) take effect immediately.
    """
    testnet = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
    base_url = _BASE_URL_TESTNET if testnet else _BASE_URL_MAINNET

    proxy_url = os.environ.get("BYBIT_PROXY_URL", "socks5h://127.0.0.1:9050")
    use_proxy = os.environ.get("BYBIT_USE_TOR", "true").lower() in ("1", "true", "yes")

    try:
        timeout = float(os.environ.get("BYBIT_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    try:
        recv_window = int(os.environ.get("BYBIT_RECV_WINDOW", str(_RECV_WINDOW_DEFAULT)))
    except (TypeError, ValueError):
        recv_window = _RECV_WINDOW_DEFAULT
    recv_window = max(1, min(_RECV_WINDOW_MAX, recv_window))

    try:
        max_retries = int(os.environ.get("BYBIT_MAX_RETRIES", str(_MAX_RETRIES)))
    except (TypeError, ValueError):
        max_retries = _MAX_RETRIES
    if max_retries < 0:
        max_retries = 0

    sign_type = os.environ.get("BYBIT_SIGN_TYPE", "2")  # 2 = HMAC

    try:
        price_scale = int(os.environ.get("BYBIT_PRICE_SCALE", str(_DEFAULT_PRICE_SCALE)))
    except (TypeError, ValueError):
        price_scale = _DEFAULT_PRICE_SCALE
    if price_scale < 0:
        price_scale = 0

    return {
        "api_key":      os.environ.get("BYBIT_API_KEY", "").strip(),
        "api_secret":   os.environ.get("BYBIT_API_SECRET", "").strip(),
        "base_url":     base_url,
        "testnet":      testnet,
        "use_proxy":    use_proxy,
        "proxy_url":    proxy_url,
        "proxies":      {"http": proxy_url, "https": proxy_url} if use_proxy else None,
        "timeout":      timeout,
        "recv_window":  recv_window,
        "sign_type":    sign_type,
        "max_retries":  max_retries,
        "price_scale":  price_scale,
    }


# ---------------------------------------------------------------------------
# Session management (connection pooling)
# ---------------------------------------------------------------------------
_SESSION: Optional[requests.Session] = None


def _build_session() -> requests.Session:
    sess = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        pool_block=False,
    )
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.headers.update({
        "User-Agent": f"bybit-core/{__version__} (+python-requests)",
        "Accept":     "application/json",
    })
    return sess


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def reset_session() -> None:
    """Close and drop the cached Session (e.g. after toggling the proxy)."""
    global _SESSION
    if _SESSION is not None:
        try:
            _SESSION.close()
        except Exception:
            pass
    _SESSION = None


# ---------------------------------------------------------------------------
# Price scaling helpers
# ---------------------------------------------------------------------------
def to_int_price(price: Any, scale: Optional[int] = None) -> str:
    """Convert a decimal price to the integer string Bybit expects.

    Bybit's order APIs require prices as integers = price * 10**scale.
    Default scale comes from `BYBIT_PRICE_SCALE` (or 3).

    >>> to_int_price("0.842")
    '842'
    >>> to_int_price(0.0199, scale=4)
    '199'
    """
    if scale is None:
        scale = get_config().get("price_scale", _DEFAULT_PRICE_SCALE)
    if scale < 0:
        scale = 0
    try:
        scaled = int(round(float(price) * (10 ** scale)))
        return str(scaled)
    except (TypeError, ValueError):
        return str(price)


def from_int_price(int_price: Any, scale: Optional[int] = None) -> float:
    """Inverse of `to_int_price`: convert an integer price back to decimal."""
    if scale is None:
        scale = get_config().get("price_scale", _DEFAULT_PRICE_SCALE)
    if scale < 0:
        scale = 0
    try:
        return float(int_price) / (10 ** scale)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------
def _sorted_query_string(params: Mapping[str, Any]) -> str:
    """Return the canonical query string used both for signing and the request.

    Rules (per Bybit V5):
      * keys sorted lexicographically
      * None values are dropped
      * values are stringified
    """
    items = [(k, str(v)) for k, v in sorted(params.items()) if v is not None]
    return urllib.parse.urlencode(items)


def _sign(cfg: Dict[str, Any], ts: str, payload: str) -> str:
    msg = f"{ts}{cfg['api_key']}{cfg['recv_window']}{payload}"
    return hmac.new(
        cfg["api_secret"].encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Server time / health
# ---------------------------------------------------------------------------
def server_time() -> Optional[int]:
    """Return Bybit's server time in milliseconds (or None on failure)."""
    resp = api_request("GET", "/v5/market/time", signed=False)
    if resp.get("retCode") == 0:
        r = (resp.get("result") or {})
        try:
            return int(r.get("timeSecond", 0)) * 1000
        except (TypeError, ValueError):
            pass
        try:
            return int(r.get("time"))
        except (TypeError, ValueError, KeyError):
            return None
    return None


def health_check() -> bool:
    """Lightweight liveness probe via a public, unsigned endpoint."""
    return api_request("GET", "/v5/market/time", signed=False).get("retCode") == 0


def is_rate_limited(response: Mapping[str, Any]) -> bool:
    """True when the response indicates a transient rate-limit condition."""
    return response.get("retCode") in _RETRYABLE_RET_CODES


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with full jitter, capped at 30s."""
    base = min(30.0, (2 ** attempt) * 0.5)
    time.sleep(random.uniform(0, base))


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------
def api_request(
    method: str,
    endpoint: str,
    params: Optional[Mapping[str, Any]] = None,
    signed: bool = False,
    *,
    timeout: Optional[float] = None,
    recv_window: Optional[int] = None,
    sign_type: Optional[str] = None,
    retries: Optional[int] = None,
    price_scale: Optional[int] = None,
    price_fields: Optional[list] = None,
) -> Dict[str, Any]:
    """Bybit V5 REST call.

    Compatible with the original signature: api_request(method, endpoint,
    params, signed). Extra keyword arguments are honoured but never required.

    Parameters
    ----------
    price_scale : int, optional
        Multiplier used by `price_fields` to convert decimal prices to the
        integer representation Bybit expects. Defaults to `BYBIT_PRICE_SCALE`
        (or 3).
    price_fields : list[str], optional
        Parameter names that should be auto-scaled to integers before signing.
        Example: ["price", "stopLoss", "takeProfit", "activePrice"].
    """
    if not isinstance(method, str):
        return {"retCode": -1, "retMsg": f"method must be a string, got {type(method).__name__}"}
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        return {"retCode": -1, "retMsg": f"Unsupported HTTP method: {method}"}

    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    cfg = get_config()

    if recv_window is not None:
        try:
            cfg = dict(cfg)
            cfg["recv_window"] = max(1, min(_RECV_WINDOW_MAX, int(recv_window)))
        except (TypeError, ValueError):
            pass
    if sign_type is None:
        sign_type = cfg.get("sign_type", "2")
    if retries is None:
        retries = int(cfg.get("max_retries", _MAX_RETRIES))
    if timeout is None:
        timeout = cfg.get("timeout", _DEFAULT_TIMEOUT)

    if price_scale is None:
        price_scale = int(cfg.get("price_scale", _DEFAULT_PRICE_SCALE))
    if price_scale < 0:
        price_scale = 0

    if signed and (not cfg["api_key"] or not cfg["api_secret"]):
        return {"retCode": 10002, "retMsg": "Missing BYBIT_API_KEY / BYBIT_API_SECRET"}

    # Auto-scale decimal price fields to integer strings (e.g. 0.842 -> "842")
    if params and price_fields and price_scale > 0:
        scaled = {}
        for k, v in params.items():
            if k in price_fields and v not in (None, ""):
                scaled[k] = to_int_price(v, price_scale)
            else:
                scaled[k] = v
        params = scaled

    body_str: Optional[str] = None
    full_url: str
    query_str: str = ""

    if method == "GET":
        query_str = _sorted_query_string(params or {})
        full_url = f"{cfg['base_url']}{endpoint}" + (f"?{query_str}" if query_str else "")
    else:
        body_str = json.dumps(params or {}, separators=(",", ":"), ensure_ascii=False)
        full_url = f"{cfg['base_url']}{endpoint}"

    headers: Dict[str, str] = {
        "Content-Type":     "application/json",
        "X-BAPI-SIGN-TYPE": str(sign_type),
    }

    if signed:
        ts = str(int(time.time() * 1000))
        payload = body_str if method != "GET" else query_str
        sig = _sign(cfg, ts, payload)
        headers.update({
            "X-BAPI-API-KEY":    cfg["api_key"],
            "X-BAPI-SIGN":       sig,
            "X-BAPI-TIMESTAMP":  ts,
            "X-BAPI-RECV-WINDOW": str(cfg["recv_window"]),
        })

    sess = _get_session()
    proxies = cfg["proxies"]

    last_resp: Dict[str, Any] = {"retCode": -1, "retMsg": "no response"}

    for attempt in range(retries + 1):
        try:
            if method == "GET":
                resp = sess.get(full_url, headers=headers,
                                proxies=proxies, timeout=timeout)
            else:
                resp = sess.request(method, full_url, data=body_str,
                                    headers=headers, proxies=proxies, timeout=timeout)

            if resp.status_code != 200:
                last_resp = {
                    "retCode": resp.status_code,
                    "retMsg": f"HTTP {resp.status_code}: {resp.text[:512]}",
                }
                if resp.status_code in _RETRY_STATUSES and attempt < retries:
                    log.warning("HTTP %s on %s %s (attempt %d/%d) — backing off",
                                resp.status_code, method, endpoint, attempt + 1, retries)
                    _sleep_backoff(attempt)
                    continue
                return last_resp

            try:
                last_resp = resp.json()
            except ValueError:
                return {
                    "retCode": -1,
                    "retMsg": f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:512]}",
                }

            rc = last_resp.get("retCode")
            if rc in _RETRYABLE_RET_CODES and attempt < retries:
                log.warning("Bybit retCode %s on %s %s (attempt %d/%d) — backing off",
                            rc, method, endpoint, attempt + 1, retries)
                _sleep_backoff(attempt)
                continue

            return last_resp

        except requests.exceptions.ProxyError as e:
            last_resp = {"retCode": -1, "retMsg": f"Proxy error: {e}"}
            log.error("Proxy error on %s %s: %s", method, endpoint, e)
            if attempt < min(retries, 2):
                _sleep_backoff(attempt)
                continue
            return last_resp
        except requests.exceptions.SSLError as e:
            return {"retCode": -1, "retMsg": f"SSL error: {e}"}
        except requests.exceptions.Timeout as e:
            last_resp = {"retCode": -1, "retMsg": f"Timeout after {timeout}s: {e}"}
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            return last_resp
        except requests.exceptions.ConnectionError as e:
            last_resp = {"retCode": -1, "retMsg": f"Connection error: {e}"}
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            return last_resp
        except requests.exceptions.RequestException as e:
            return {"retCode": -1, "retMsg": f"Request error: {e}"}

    return last_resp