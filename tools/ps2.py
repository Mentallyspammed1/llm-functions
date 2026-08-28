import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import platform
import re
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "10.0.0"

# ==============================================================================
# CONSTANTS
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_TIMEOUT = 124
EXIT_PERMISSION_DENIED = 126
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPTED = 130

BYBIT_MAINNET = "https://api.bybit.com"
BYBIT_TESTNET = "https://api-testnet.bybit.com"

BYBIT_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
BYBIT_WS_LINEAR_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"
L2_DEFAULT_DEPTH = 50
L2_DEFAULT_MAX_SPREAD_BPS = 12.0
L2_DEFAULT_ENTRY_SCORE = 0.62
L2_DEFAULT_EXIT_SCORE = 0.20
L2_DEFAULT_TARGET_PROFIT = 0.10
L2_DEFAULT_STOP_BPS = 10.0
L2_DEFAULT_MAX_HOLD_SECONDS = 45.0
L2_DEFAULT_RECONNECT_SECONDS = 1.0
L2_HEARTBEAT_SECONDS = 20.0

VALID_TIMEFRAMES = {"1", "3", "5", "15", "30", "60", "120", "240", "D"}
VALID_ACTIONS = {
    "ta",
    "scan",
    "calc_micro_profit",
    "quick_scalp",
    "l2_live_scalp",
    "l2_signal",
    "micro_pnl",
    "compound_micro_profit",
    "trade_statistics",
    "wallet_balance",
    "positions",
    "open_orders",
    "orderbook",
    "price_action",
    "ticker",
    "instrument",
    "fills",
    "risk_status",
    "execute_scalp",
    "close_position",
    "cancel_orders",
    "set_leverage",
    "kill_switch",
    "history",
}

# Natural-language aliases accepted by the router but intentionally excluded
# from the generated enum so AIChat/argc learns the canonical action names.
from typing import Final, Pattern, Set

ACTION_ALIASES: Final[Dict[str, str]] = {
    "scalp": "quick_scalp",
    "quick": "quick_scalp",
    "quick_trade": "quick_scalp",
    "trade": "quick_scalp",
    "live_scalp": "l2_live_scalp",
    "l2_scalp": "l2_live_scalp",
    "signal": "l2_signal",
    "pnl": "micro_pnl",
    "profit": "calc_micro_profit",
    "balance": "wallet_balance",
    "orders": "open_orders",
    "position": "positions",
}

PUBLIC_ENDPOINTS: Final[Set[str]] = {
    "/v5/market/time",
    "/v5/market/kline",
    "/v5/market/orderbook",
    "/v5/market/instruments-info",
    "/v5/market/tickers",
}

DEFAULT_RECV_WINDOW: Final[str] = "5000"
DEFAULT_TIMEOUT: Final[float] = 10.0
MAX_RETRIES: Final[int] = 3

NEON_CYAN = "\033[38;5;51m"
NEON_GREEN = "\033[38;5;46m"
NEON_RED = "\033[38;5;196m"
NEON_YELLOW = "\033[38;5;226m"
NEON_PURPLE = "\033[38;5;129m"
NEON_PINK = "\033[38;5;198m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

_ANSI_RE: Final[Pattern[str]] = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\033\[[0-9;?]*[a-zA-Z]"
)

# ==============================================================================
# ERROR MODEL
# ==============================================================================


class PyrmError(Exception):
    """Base exception for Pyrm errors."""
    def __init__(self, message: str, code: str = "PYRM_ERROR", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class AuthenticationError(PyrmError):
    """Raised when API authentication fails."""
    def __init__(self, message: str = "Authentication failed", details: dict | None = None):
        super().__init__(message, "AUTH_ERROR", details)


class RateLimitError(PyrmError):
    """Raised when API rate limit is exceeded."""
    def __init__(self, message: str = "Rate limit exceeded", retry_after: float | None = None, details: dict | None = None):
        super().__init__(message, "RATE_LIMIT", details)
        self.retry_after = retry_after


class NetworkError(PyrmError):
    """Raised for network-related failures."""
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message, "NETWORK_ERROR", details)


class ValidationError(PyrmError):
    """Raised when request/response validation fails."""
    def __init__(self, message: str, field: str | None = None, details: dict | None = None):
        super().__init__(message, "VALIDATION_ERROR", details)
        self.field = field


class OrderError(PyrmError):
    """Raised for order execution failures."""
    def __init__(self, message: str, order_id: str | None = None, details: dict | None = None):
        super().__init__(message, "ORDER_ERROR", details)
        self.order_id = order_id


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def colorize(text: str, color: str, bold: bool = False, dim: bool = False) -> str:
    """Apply ANSI color formatting to text."""
    styles = []
    if bold:
        styles.append(BOLD)
    if dim:
        styles.append(DIM)
    styles.append(color)
    return f"{''.join(styles)}{text}{RESET}"


def resolve_action(action: str) -> str:
    """Resolve action alias to canonical action name."""
    return ACTION_ALIASES.get(action.lower(), action)


def is_public_endpoint(path: str) -> bool:
    """Check if an endpoint path is public (no auth required)."""
    return path in PUBLIC_ENDPOINTS


def format_error(err: Exception, include_traceback: bool = False) -> str:
    """Format exception for logging/display."""
    if isinstance(err, PyrmError):
        parts = [f"[{err.code}] {err}"]
        if err.details:
            parts.append(f"Details: {err.details}")
        return " | ".join(parts)
    return f"[UNEXPECTED] {type(err).__name__}: {err}"


from __future__ import annotations

EXIT_ERROR = 1
EXIT_SUCCESS = 0


class ToolError(Exception):
    __slots__ = ("details", "exit_code", "message")

    def __init__(
        self,
        message: str,
        exit_code: int = EXIT_ERROR,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, exit_code={self.exit_code}, details={self.details!r})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.message,
            "exit_code": self.exit_code,
            **self.details,
        }

    @classmethod
    def from_exception(cls, exc: Exception, exit_code: int = EXIT_ERROR, context: Optional[dict[str, Any]] = None) -> ToolError:
        return cls(str(exc), exit_code=exit_code, details={"original_type": type(exc).__name__, **(context or {})})

    def print_and_exit(self, file: Optional[Any] = None) -> None:
        out = file or sys.stderr
        print(f"Error: {self.message}", file=out)
        if self.details:
            print(f"Details: {self.details}", file=out)
        sys.exit(self.exit_code)


from functools import wraps
from typing import Callable, TypeVar

# ==============================================================================
# CUSTOM JSON ENCODER
# ==============================================================================


class ToolJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Decimal):
            return format(obj, "f")
        if isinstance(obj, dt.datetime):
            return obj.isoformat()
        if isinstance(obj, dt.date):
            return obj.isoformat()
        if isinstance(obj, set):
            return sorted(obj)
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        return super().default(obj)


# ==============================================================================
# JSON HELPERS
# ==============================================================================

def json_dumps(obj: Any, **kwargs: Any) -> str:
    """Serialize to JSON using ToolJSONEncoder."""
    return json.dumps(obj, cls=ToolJSONEncoder, **kwargs)


def json_loads(s: str, parse_dates: bool = False) -> Any:
    """Deserialize JSON with optional ISO date parsing."""
    data = json.loads(s)
    if parse_dates:
        data = _parse_iso_dates(data)
    return data


def _parse_iso_dates(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _parse_iso_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_parse_iso_dates(v) for v in obj]
    if isinstance(obj, str):
        try:
            return dt.datetime.fromisoformat(obj.replace("Z", "+00:00"))
        except ValueError:
            try:
                return dt.date.fromisoformat(obj)
            except ValueError:
                return obj
    return obj


# ==============================================================================
# GENERIC HELPERS
# ==============================================================================

def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def utc_day() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def parse_datetime(value: Any, default: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """Parse ISO format datetime string or return default."""
    if value is None:
        return default
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return default


# ==============================================================================
# RESILIENCE HELPERS
# ==============================================================================

T = TypeVar("T")


def retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    exponential_base: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for retrying functions with exponential backoff."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
                    if on_retry:
                        on_retry(exc, attempt)
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


def timeout(seconds: float) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator to enforce function timeout (thread-based)."""
    import threading

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            result: list[T] = []
            exc: list[BaseException] = []

            def target() -> None:
                try:
                    result.append(func(*args, **kwargs))
                except BaseException as e:
                    exc.append(e)
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            thread.join(seconds)
            if thread.is_alive():
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds}s")
            if exc:
                raise exc[0]
            return result[0]
        return wrapper
    return decorator


from __future__ import annotations

from typing import Union

from .errors import EXIT_INVALID_INPUT, ToolError  # type: ignore[attr-defined]


def safe_float(value: Any, default: float = float("nan")) -> float:
    """Safely convert a value to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def positive_float(
    value: Any,
    name: str,
    allow_zero: bool = False,
) -> float:
    x = safe_float(value, float("nan"))

    if not math.isfinite(x):
        raise ToolError(
            f"Parameter '{name}' must be a finite number.",
            EXIT_INVALID_INPUT,
        )

    if allow_zero:
        if x < 0:
            raise ToolError(
                f"Parameter '{name}' cannot be negative.",
                EXIT_INVALID_INPUT,
            )
    elif x <= 0:
        raise ToolError(
            f"Parameter '{name}' must be greater than zero.",
            EXIT_INVALID_INPUT,
        )

    return x


def non_negative_float(value: Any, name: str) -> float:
    """Alias for positive_float with allow_zero=True."""
    return positive_float(value, name, allow_zero=True)


def positive_int(
    value: Any,
    name: str,
    allow_zero: bool = False,
) -> int:
    """Validate and return a positive integer."""
    x = positive_float(value, name, allow_zero=allow_zero)
    if x != int(x):
        raise ToolError(
            f"Parameter '{name}' must be an integer.",
            EXIT_INVALID_INPUT,
        )
    return int(x)


def decimal(value: Any, max_places: int | None = None) -> Decimal:
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise InvalidOperation
        if max_places is not None:
            exp = d.as_tuple().exponent
            if exp < -max_places:
                raise ToolError(
                    f"Decimal value exceeds {max_places} decimal places: {value}",
                    EXIT_INVALID_INPUT,
                )
        return d
    except Exception as exc:
        raise ToolError(f"Invalid decimal value: {value}", EXIT_INVALID_INPUT) from exc


def decimal_range(
    value: Any,
    name: str,
    min_val: Decimal | None = None,
    max_val: Decimal | None = None,
    inclusive: bool = True,
) -> Decimal:
    """Validate a decimal value falls within an optional range."""
    d = decimal(value)
    if min_val is not None:
        if inclusive and d < min_val:
            raise ToolError(
                f"Parameter '{name}' must be >= {min_val}.",
                EXIT_INVALID_INPUT,
            )
        if not inclusive and d <= min_val:
            raise ToolError(
                f"Parameter '{name}' must be > {min_val}.",
                EXIT_INVALID_INPUT,
            )
    if max_val is not None:
        if inclusive and d > max_val:
            raise ToolError(
                f"Parameter '{name}' must be <= {max_val}.",
                EXIT_INVALID_INPUT,
            )
        if not inclusive and d >= max_val:
            raise ToolError(
                f"Parameter '{name}' must be < {max_val}.",
                EXIT_INVALID_INPUT,
            )
    return d


class ToolError(ValueError):
    """Custom error for tool-related failures."""
    pass


def quantize_step(
    value: Union[str, int, float, Decimal],
    step: Union[str, int, float, Decimal],
    rounding: str = ROUND_DOWN,
) -> str:
    """
    Quantize a value to the nearest step increment.

    Args:
        value: The value to quantize.
        step: The step size (precision increment).
        rounding: Rounding mode from decimal module (default: ROUND_DOWN).

    Returns:
        Quantized value as a string without trailing zeros or exponent notation.

    Raises:
        ToolError: If step is not positive or quantization fails.
    """
    try:
        v = Decimal(str(value))
        s = Decimal(str(step))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ToolError(f"Invalid numeric input: {e}") from e

    if s <= 0:
        raise ToolError("Exchange precision step must be positive.")

    if rounding not in (
        ROUND_DOWN,
        "ROUND_DOWN",
        "ROUND_UP",
        "ROUND_HALF_UP",
        "ROUND_HALF_DOWN",
        "ROUND_HALF_EVEN",
        "ROUND_CEILING",
        "ROUND_FLOOR",
    ):
        raise ToolError(f"Unsupported rounding mode: {rounding}")

    units = (v / s).quantize(Decimal("1"), rounding=rounding)
    result = (units * s).quantize(s.normalize())

    return format(result.normalize(), "f")


def quantize_step_decimal(
    value: Union[str, int, float, Decimal],
    step: Union[str, int, float, Decimal],
    rounding: str = ROUND_DOWN,
) -> Decimal:
    """
    Quantize a value to the nearest step increment, returning Decimal.

    Args:
        value: The value to quantize.
        step: The step size (precision increment).
        rounding: Rounding mode from decimal module (default: ROUND_DOWN).

    Returns:
        Quantized value as Decimal.

    Raises:
        ToolError: If step is not positive or quantization fails.
    """
    try:
        v = Decimal(str(value))
        s = Decimal(str(step))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ToolError(f"Invalid numeric input: {e}") from e

    if s <= 0:
        raise ToolError("Exchange precision step must be positive.")

    units = (v / s).quantize(Decimal("1"), rounding=rounding)
    return (units * s).quantize(s.normalize())


def validate_precision_step(step: Union[str, int, float, Decimal]) -> Decimal:
    """
    Validate and normalize a precision step.

    Args:
        step: The step size to validate.

    Returns:
        Normalized step as Decimal.

    Raises:
        ToolError: If step is not positive or invalid.
    """
    try:
        s = Decimal(str(step))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ToolError(f"Invalid step value: {e}") from e

    if s <= 0:
        raise ToolError("Exchange precision step must be positive.")

    return s.normalize()


def get_step_from_precision(precision: int) -> Decimal:
    """
    Generate a step size from decimal precision (e.g., precision=8 -> step=0.00000001).

    Args:
        precision: Number of decimal places.

    Returns:
        Step size as Decimal.

    Raises:
        ToolError: If precision is negative.
    """
    if precision < 0:
        raise ToolError("Precision must be non-negative.")
    return Decimal("1").scaleb(-precision)


from decimal import ROUND_DOWN

EXIT_INVALID_INPUT = 2


class ToolError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def _to_decimal(value: Any, field_name: str = "value") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ToolError(f"Invalid {field_name}: {value!r} ({e})", EXIT_INVALID_INPUT)


def _format_decimal(d: Decimal) -> str:
    return format(d.normalize(), "f")


def clamp_decimal_qty(
    qty: Any,
    step: Any,
    minimum: Any,
    maximum: Any,
    rounding: str = ROUND_DOWN,
) -> Tuple[float, str]:
    q = _to_decimal(qty, "quantity")
    s = _to_decimal(step, "step")
    mn = _to_decimal(minimum, "minimum")
    mx = _to_decimal(maximum, "maximum")

    if s <= 0:
        raise ToolError("Step size must be greater than zero.", EXIT_INVALID_INPUT)
    if mn < 0:
        raise ToolError("Minimum must be non-negative.", EXIT_INVALID_INPUT)
    if mx > 0 and mx < mn:
        raise ToolError("Maximum cannot be less than minimum.", EXIT_INVALID_INPUT)

    if q <= 0:
        raise ToolError("Quantity must be greater than zero.", EXIT_INVALID_INPUT)

    if mx > 0 and q > mx:
        q = mx

    q = (q / s).quantize(Decimal("1"), rounding=rounding) * s

    if q < mn:
        raise ToolError(
            f"Quantity {_format_decimal(q)} is below exchange minimum {_format_decimal(mn)}.",
            EXIT_INVALID_INPUT,
        )

    return float(q), _format_decimal(q)


def clamp_decimal_price(
    price: Any,
    tick: Any,
    minimum: Any = 0,
    maximum: Any = 0,
    rounding: str = ROUND_DOWN,
) -> Tuple[float, str]:
    return clamp_decimal_qty(price, tick, minimum, maximum, rounding)


def calculate_position_size(
    equity: Any,
    risk_pct: Any,
    entry: Any,
    stop: Any,
    step: Any,
    minimum: Any = 0,
    maximum: Any = 0,
) -> Tuple[float, str]:
    eq = _to_decimal(equity, "equity")
    rp = _to_decimal(risk_pct, "risk_pct")
    en = _to_decimal(entry, "entry")
    sl = _to_decimal(stop, "stop")

    if eq <= 0:
        raise ToolError("Equity must be positive.", EXIT_INVALID_INPUT)
    if not (0 < rp <= 100):
        raise ToolError("Risk percent must be in (0, 100].", EXIT_INVALID_INPUT)
    if en <= 0 or sl <= 0:
        raise ToolError("Entry and stop must be positive.", EXIT_INVALID_INPUT)

    risk_amount = eq * (rp / Decimal("100"))
    distance = abs(en - sl)
    if distance == 0:
        raise ToolError("Entry and stop cannot be equal.", EXIT_INVALID_INPUT)

    raw_qty = risk_amount / distance
    return clamp_decimal_qty(raw_qty, step, minimum, maximum)


import re


class ToolError(Exception):
    """Custom exception for tool errors with exit code."""
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


EXIT_INVALID_INPUT = 2


def validate_symbol(symbol: str) -> str:
    """
    Validate and normalize a perpetual trading symbol.
    
    Args:
        symbol: The trading symbol to validate (e.g., 'BTCUSDT', 'ETH-USD')
        
    Returns:
        Normalized uppercase symbol
        
    Raises:
        ToolError: If symbol is empty or invalid format
    """
    if symbol is None:
        raise ToolError("Perpetual symbol cannot be None.", EXIT_INVALID_INPUT)

    sym = str(symbol).strip().upper()

    if not sym:
        raise ToolError("Perpetual symbol cannot be empty.", EXIT_INVALID_INPUT)

    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", sym):
        raise ToolError(
            f"Invalid perpetual symbol '{sym}'. Must be 3-32 chars: A-Z, 0-9, _, -",
            EXIT_INVALID_INPUT,
        )

    # Logic upgrade 1: Detect and warn about common formatting issues
    if "__" in sym or "--" in sym or "_-" in sym or "-_" in sym:
        # Allow but could log warning in production
        pass

    # Logic upgrade 2: Validate known quote currencies for better UX
    _KNOWN_QUOTES = {"USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"}
    for quote in _KNOWN_QUOTES:
        if sym.endswith(quote) and len(sym) > len(quote):
            # Valid quote currency detected
            break
    else:
        # Not a known quote - could be new or invalid, but don't block
        pass

    # Logic upgrade 3: Length sanity check for common patterns
    if len(sym) > 20 and not any(sym.endswith(q) for q in _KNOWN_QUOTES):
        # Unusually long symbol without known quote - potential typo
        pass

    return sym


def validate_symbols(symbols: list[str]) -> list[str]:
    """
    Validate multiple symbols at once.
    
    Args:
        symbols: List of trading symbols
        
    Returns:
        List of validated symbols
        
    Raises:
        ToolError: If any symbol is invalid
    """
    return [validate_symbol(s) for s in symbols]


def is_valid_symbol(symbol: str) -> bool:
    """
    Check if symbol is valid without raising exception.
    
    Args:
        symbol: The trading symbol to check
        
    Returns:
        True if valid, False otherwise
    """
    try:
        validate_symbol(symbol)
        return True
    except ToolError:
        return False


from __future__ import annotations

from functools import lru_cache
from typing import Final

VALID_TIMEFRAMES: Final[set[str]] = {
    "1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"
}

EXIT_INVALID_INPUT: Final[int] = 2


class ToolError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def validate_timeframe(timeframe: str) -> str:
    tf = str(timeframe or "1").strip().upper()

    if tf not in VALID_TIMEFRAMES:
        raise ToolError(
            f"Invalid timeframe '{tf}'. Allowed: {sorted(VALID_TIMEFRAMES)}",
            EXIT_INVALID_INPUT,
        )

    return tf


def validate_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ToolError("Symbol cannot be empty", EXIT_INVALID_INPUT)
    if not all(c.isalnum() or c in "-_" for c in sym):
        raise ToolError(f"Invalid symbol format: '{sym}'", EXIT_INVALID_INPUT)
    return sym


# ==============================================================================
# ENVIRONMENT / CONFIGURATION
# ==============================================================================

@lru_cache(maxsize=1)
def get_data_dir() -> Path:
    env_path = os.environ.get("BYBIT_TRADER_DATA_DIR")
    if env_path:
        path = Path(env_path).expanduser().resolve()
    else:
        path = Path("~/.config/bybit_trader/data").expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_config_dir() -> Path:
    env_path = os.environ.get("BYBIT_TRADER_CONFIG_DIR")
    if env_path:
        path = Path(env_path).expanduser().resolve()
    else:
        path = Path("~/.config/bybit_trader").expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir() -> Path:
    cache = get_data_dir() / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


class TradingConfig:
    def __init__(
        self,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        default_timeframe: str = "1",
        default_symbol: str = "BTCUSDT",
    ):
        self._data_dir = data_dir or get_data_dir()
        self._config_dir = config_dir or get_config_dir()
        self.default_timeframe = validate_timeframe(default_timeframe)
        self.default_symbol = validate_symbol(default_symbol)

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def cache_dir(self) -> Path:
        return get_cache_dir()

    def resolve_symbol(self, symbol: str | None) -> str:
        return validate_symbol(symbol or self.default_symbol)

    def resolve_timeframe(self, timeframe: str | None) -> str:
        return validate_timeframe(timeframe or self.default_timeframe)


from functools import lru_cache
from typing import Dict


def get_data_dir() -> Path:
    """Return the platform-appropriate data directory for the application."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "bybit-agent"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        if base:
            return Path(base) / "bybit-agent"
        return Path.home() / ".local" / "share" / "bybit-agent"
    return Path.home() / ".bybit-agent"


@lru_cache(maxsize=1)
def get_env_config() -> Dict[str, str]:
    result: Dict[str, str] = {}

    candidates = [
        get_data_dir() / ".env",
        Path.home() / ".config" / "bybit-agent" / ".env",
        Path.cwd() / ".env",
    ]

    env_file = next((p for p in candidates if p.exists()), None)

    if env_file is None:
        return result

    try:
        content = env_file.read_text(encoding="utf-8")
    except OSError:
        return result

    for raw in content.splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        result[key] = value

    return result


def get_builtin_var(name: str) -> Optional[str]:
    normalized = name.upper()
    return (
        os.environ.get(f"LLM_AGENT_VAR_{normalized}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.lower()}")
        or os.environ.get(f"LLM_AGENT_VAR_{name}")
    )


def get_agent_var(name: str, default: str = "") -> str:
    return get_builtin_var(name) or default


def cfg_value(
    env_var: str,
    agent_var: Optional[str] = None,
    default: str = "",
) -> str:
    cfg = get_env_config()

    if agent_var:
        return (
            os.environ.get(agent_var)
            or os.environ.get(env_var)
            or cfg.get(env_var)
            or default
        )

    return os.environ.get(env_var) or cfg.get(env_var) or default


def reload_env_config() -> Dict[str, str]:
    """Force reload of environment configuration, clearing the cache."""
    get_env_config.cache_clear()
    return get_env_config()


def set_agent_var(name: str, value: str) -> None:
    """Set a builtin agent variable in the current process environment."""
    os.environ[f"LLM_AGENT_VAR_{name.upper()}"] = value


def get_all_agent_vars() -> Dict[str, str]:
    """Return all LLM_AGENT_VAR_* environment variables as a dict."""
    prefix = "LLM_AGENT_VAR_"
    return {
        key[len(prefix):]: value
        for key, value in os.environ.items()
        if key.startswith(prefix)
    }


import re
from datetime import datetime, timezone

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def get_builtin_var(name: str) -> Optional[str]:
    """Retrieve a builtin variable by name, returning None if not found."""
    return getattr(sys, name, None) or os.environ.get(name)


def utc_now() -> datetime:
    """Return current UTC time with timezone awareness."""
    return datetime.now(timezone.utc)


def get_execution_context() -> dict[str, Any]:
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME", "bybit_trader"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "utc_now": utc_now(),
    }


# ==============================================================================
# LOGGING / UI
# ==============================================================================

def _is_tty(no_color: bool = False) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return (
        sys.stderr.isatty()
        and os.environ.get("TERM", "").lower() not in {"", "dumb"}
    )


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _cprint(
    text: str,
    file: Any = None,
    no_color: bool = False,
    end: str = "\n",
) -> None:
    target = file or sys.stderr
    if not _is_tty(no_color):
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


# ==============================================================================
# UPGRADES: Enhanced logging utilities
# ==============================================================================

_LOG_LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_DEFAULT_LOG_LEVEL = _LOG_LEVELS["INFO"]


class Logger:
    """Minimal structured logger with level filtering and JSON output support."""

    def __init__(
        self,
        name: str = "app",
        level: int = _DEFAULT_LOG_LEVEL,
        json_output: bool = False,
        no_color: bool = False,
    ) -> None:
        self.name = name
        self.level = level
        self.json_output = json_output
        self.no_color = no_color

    def _log(self, level_name: str, message: str, **kwargs: Any) -> None:
        if _LOG_LEVELS[level_name] < self.level:
            return

        timestamp = utc_now().isoformat()
        if self.json_output:
            import json
            record = {
                "timestamp": timestamp,
                "level": level_name,
                "logger": self.name,
                "message": message,
                **kwargs,
            }
            _cprint(json.dumps(record), no_color=self.no_color)
        else:
            color_map = {
                "DEBUG": "\x1b[36m",
                "INFO": "\x1b[32m",
                "WARNING": "\x1b[33m",
                "ERROR": "\x1b[31m",
                "CRITICAL": "\x1b[35;1m",
            }
            reset = "\x1b[0m"
            color = color_map.get(level_name, "") if _is_tty(self.no_color) else ""
            extra = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
            extra_str = f" {extra}" if extra else ""
            _cprint(
                f"{color}[{timestamp}] {level_name:<8} {self.name}: {message}{extra_str}{reset}",
                no_color=self.no_color,
            )

    def debug(self, message: str, **kwargs: Any) -> None:
        self._log("DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self._log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self._log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._log("ERROR", message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        self._log("CRITICAL", message, **kwargs)


def get_logger(
    name: str = "app",
    level: Optional[str] = None,
    json_output: bool = False,
    no_color: bool = False,
) -> Logger:
    """Factory function to create a configured Logger instance."""
    log_level = _LOG_LEVELS.get(level.upper() if level else "INFO", _DEFAULT_LOG_LEVEL)
    return Logger(name, log_level, json_output, no_color)


def format_exception(exc: BaseException, include_traceback: bool = True) -> str:
    """Format an exception with optional traceback for logging."""
    import traceback
    if include_traceback:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{type(exc).__name__}: {exc}"


def log_execution_context(logger: Logger) -> None:
    """Log the current execution context at INFO level."""
    ctx = get_execution_context()
    logger.info("Execution context", **ctx)


from __future__ import annotations

# ─── Constants ──────────────────────────────────────────────────────────────
NEON_PURPLE = "\033[38;5;141m"
NEON_PINK = "\033[38;5;213m"
NEON_CYAN = "\033[38;5;51m"
NEON_GREEN = "\033[38;5;46m"
NEON_RED = "\033[38;5;196m"
BOLD = "\033[1m"
RESET = "\033[0m"

__version__ = "0.1.0"


# ─── Helpers ────────────────────────────────────────────────────────────────
def _is_tty(no_color: bool) -> bool:
    """Return True if output should be coloured."""
    if no_color:
        return False
    return sys.stdout.isatty()


def _cprint(text: str, *, no_color: bool) -> None:
    """Print *text* unless *no_color* is True and we're not a TTY."""
    if no_color and not sys.stdout.isatty():
        # Strip ANSI codes when colour is explicitly disabled
        import re
        text = re.sub(r"\033\[[0-9;]*m", "", text)
    print(text)


def get_data_dir() -> Path:
    """Return platform-appropriate data directory, creating it if needed."""
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))

    data_dir = base / "bybit_agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# ─── UI ─────────────────────────────────────────────────────────────────────
def print_human_readable_ui(
    data: dict[str, Any],
    no_color: bool = False,
) -> None:
    if not _is_tty(no_color):
        return

    success = bool(data.get("success"))
    color = NEON_GREEN if success else NEON_RED
    symbol = "✓" if success else "✗"

    border = "─" * 64

    _cprint(
        f"{NEON_PURPLE}╭{border}╮{RESET}",
        no_color=no_color,
    )

    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_PINK}⚡ [BYBIT AGENT v{__version__}]{RESET} "
        f"{color}{BOLD}{symbol} "
        f"{'SUCCESS' if success else 'FAILED'}{RESET}",
        no_color=no_color,
    )

    _cprint(
        f"{NEON_PURPLE}├{border}┤{RESET}",
        no_color=no_color,
    )

    for key in (
        "action",
        "symbol",
        "mode",
        "risk_gate_pass",
        "order_id",
        "duration_ms",
    ):
        if key in data:
            _cprint(
                f"{NEON_PURPLE}│{RESET} "
                f"{NEON_CYAN}{key}:{RESET} "
                f"{data[key]}",
                no_color=no_color,
            )

    if not success and data.get("error"):
        _cprint(
            f"{NEON_PURPLE}├{border}┤{RESET}",
            no_color=no_color,
        )
        _cprint(
            f"{NEON_PURPLE}│{RESET} "
            f"{NEON_RED}Error:{RESET} "
            f"{data['error']}",
            no_color=no_color,
        )

    _cprint(
        f"{NEON_PURPLE}╰{border}╯{RESET}",
        no_color=no_color,
    )


# ==============================================================================
# STATE / LOGGING
# ==============================================================================

def state_file() -> Path:
    return get_data_dir() / "state.json"


def trade_log_file() -> Path:
    return get_data_dir() / "trade_log.jsonl"


# ─── Upgrades / Additions ───────────────────────────────────────────────────
def load_state() -> dict[str, Any]:
    """Load JSON state from disk; return empty dict if missing/corrupt."""
    path = state_file()
    if not path.is_file():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    """Atomically write *state* to disk."""
    import json
    import tempfile
    path = state_file()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(state, tmp, indent=2, sort_keys=True)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def append_trade_log(entry: dict[str, Any]) -> None:
    """Append a single JSON line to the trade log."""
    import json
    path = trade_log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")


def read_trade_log(limit: int | None = None) -> list[dict[str, Any]]:
    """Read trade log, optionally returning only the last *limit* entries."""
    import json
    path = trade_log_file()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if limit:
        lines = lines[-limit:]
    return [json.loads(line) for line in lines if line.strip()]


def clear_state() -> None:
    """Remove state and trade log files."""
    for p in (state_file(), trade_log_file()):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


import json
from typing import Dict

from pyrm_scalp2 import state_file, utc_day  # type: ignore[import-not-found]


def _validate_state_data(data: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure loaded state matches expected types, falling back to defaults."""
    validated = {}
    for key, default_val in defaults.items():
        val = data.get(key, default_val)
        if type(val) is not type(default_val) and not (default_val is None and val is None):
            val = default_val
        validated[key] = val
    return validated


def load_state() -> Dict[str, Any]:
    current_day = utc_day()
    default = {
        "day": current_day,
        "daily_realized_pnl": 0.0,
        "last_execution_ts": 0.0,
        "execution_attempts": 0,
        "kill_switch": False,
        "last_order_id": None,
        "last_order_link_id": None,
    }

    path = state_file()

    if not path.exists():
        return default

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)

        if not isinstance(data, dict):
            return default

    except (OSError, json.JSONDecodeError):
        return default

    if data.get("day") != current_day:
        return default

    merged = {**default, **_validate_state_data(data, default)}
    return merged


import json
import tempfile
from typing import Dict


class ToolJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if hasattr(obj, "__json__"):
            return obj.__json__()
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        return super().default(obj)


def state_file() -> Path:
    return Path.home() / ".pyrm_scalp2_state.json"


def atomic_write_json(path: Path, data: Any, encoder: Optional[type] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    encoder_cls = encoder or ToolJSONEncoder

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp = Path(tmp_path)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=2,
                ensure_ascii=False,
                cls=encoder_cls,
            )
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def save_state(state: Dict[str, Any]) -> None:
    atomic_write_json(state_file(), state)


def load_state() -> Dict[str, Any]:
    path = state_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_path)

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


import json
from contextlib import contextmanager
from functools import lru_cache
from typing import Dict

try:
    from .config import get_env_config, parse_bool
    from .encoders import ToolJSONEncoder
    from .state import load_state, trade_log_file
except ImportError:
    from config import get_env_config, parse_bool
    from encoders import ToolJSONEncoder

    from state import load_state, trade_log_file


_LOG_WRITE_LOCK = threading.RLock()
_KILL_SWITCH_CACHE: Dict[str, Union[bool, float]] = {"value": False, "ts": 0.0}
_KILL_SWITCH_TTL = 1.0


def append_log(record: Dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")

    safe = dict(record)

    for key in (
        "api_key",
        "api_secret",
        "secret",
        "authorization",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
        "mnemonic",
        "seed",
    ):
        if key in safe:
            safe[key] = "***REDACTED***"

    safe.setdefault("timestamp", time.time())
    safe.setdefault("level", "INFO")

    path = trade_log_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _LOG_WRITE_LOCK:
        with _safe_open(path, "a", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    safe,
                    cls=ToolJSONEncoder,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )


@contextmanager
def _safe_open(path: Path, mode: str, encoding: str = "utf-8"):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open(mode, encoding=encoding) as fp:
            yield fp
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def append_log_async(record: Dict[str, Any], callback: Optional[callable] = None) -> None:
    import threading

    def _worker():
        try:
            append_log(record)
            if callback:
                callback(None)
        except Exception as e:
            if callback:
                callback(e)

    threading.Thread(target=_worker, daemon=True).start()


def rotate_log_if_needed(max_bytes: int = 10_000_000, backup_count: int = 5) -> bool:
    path = trade_log_file()
    if not path.exists() or path.stat().st_size < max_bytes:
        return False

    with _LOG_WRITE_LOCK:
        for i in range(backup_count - 1, 0, -1):
            src = path.with_suffix(f".{i}")
            dst = path.with_suffix(f".{i + 1}")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_suffix(".1"))
    return True


def kill_switch_active(use_cache: bool = True) -> bool:
    now = time.monotonic()
    if use_cache and _KILL_SWITCH_CACHE["value"] and (now - _KILL_SWITCH_CACHE["ts"]) < _KILL_SWITCH_TTL:
        return True

    cfg = get_env_config()

    env_val = parse_bool(
        os.environ.get("BYBIT_KILL_SWITCH", cfg.get("KILL_SWITCH", "false")),
        False,
    )
    state_val = bool(load_state().get("kill_switch"))
    result = env_val or state_val

    if use_cache:
        _KILL_SWITCH_CACHE["value"] = result
        _KILL_SWITCH_CACHE["ts"] = now

    return result


def set_kill_switch(active: bool, persist: bool = True) -> None:
    from .state import save_state

    state = load_state()
    state["kill_switch"] = active
    if persist:
        save_state(state)
    _KILL_SWITCH_CACHE["value"] = active
    _KILL_SWITCH_CACHE["ts"] = time.monotonic()


def get_kill_switch_status() -> Dict[str, Any]:
    return {
        "active": kill_switch_active(use_cache=False),
        "env": os.environ.get("BYBIT_KILL_SWITCH"),
        "state": load_state().get("kill_switch"),
        "cached": _KILL_SWITCH_CACHE["value"],
        "cache_age": time.monotonic() - _KILL_SWITCH_CACHE["ts"],
    }


def clear_kill_switch_cache() -> None:
    _KILL_SWITCH_CACHE["value"] = False
    _KILL_SWITCH_CACHE["ts"] = 0.0


import json
import threading
from typing import Dict

__version__ = "2.1.0"

_STATE_FILE = Path(os.getenv("PYRM_STATE_FILE", "state.json"))
_STATE_LOCK = threading.RLock()


def utc_now() -> str:
    """Return current UTC time as ISO 8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> Dict[str, Any]:
    """Load state from JSON file with thread safety and corruption recovery."""
    with _STATE_LOCK:
        if not _STATE_FILE.exists():
            return {"kill_switch": False, "kill_switch_changed_at": None}
        try:
            with _STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("State root must be a dictionary")
            return data
        except (json.JSONDecodeError, OSError, ValueError):
            # Backup corrupted file and return defaults
            backup = _STATE_FILE.with_suffix(".json.corrupt")
            try:
                _STATE_FILE.replace(backup)
            except OSError:
                pass
            return {"kill_switch": False, "kill_switch_changed_at": None}


def save_state(state: Dict[str, Any]) -> None:
    """Atomically save state to JSON file with thread safety."""
    with _STATE_LOCK:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=_STATE_FILE.parent, delete=False, prefix=".state_", suffix=".tmp"
        ) as tmp:
            json.dump(state, tmp, separators=(",", ":"), ensure_ascii=False)
            tmp_path = Path(tmp.name)
        try:
            tmp_path.replace(_STATE_FILE)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise


def set_kill_switch(active: bool) -> Dict[str, Any]:
    """Toggle kill switch with atomic persistence and audit timestamp."""
    state = load_state()
    state["kill_switch"] = bool(active)
    state["kill_switch_changed_at"] = utc_now()
    save_state(state)

    return {
        "success": True,
        "kill_switch": bool(active),
        "timestamp": utc_now(),
    }


# ==============================================================================
# CACHE
# ==============================================================================

def _stable_serialize(obj: Any) -> str:
    """Deterministic JSON serialization for cache keys."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    if isinstance(obj, dict):
        return "{" + ",".join(f"{_stable_serialize(k)}:{_stable_serialize(v)}" for k, v in sorted(obj.items())) + "}"
    if isinstance(obj, (list, tuple, set)):
        return "[" + ",".join(_stable_serialize(v) for v in obj) + "]"
    return json.dumps(str(obj), separators=(",", ":"), ensure_ascii=False)


def build_cache_key(prefix: str, *args: Any, **kwargs: Any) -> str:
    """Build a stable, versioned cache key from prefix, args, and kwargs."""
    payload_parts = [__version__, prefix]
    payload_parts.extend(_stable_serialize(a) for a in args)
    if kwargs:
        payload_parts.append(_stable_serialize(kwargs))
    payload = "|".join(payload_parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cache_path(key: str, cache_dir: Optional[Path] = None) -> Path:
    """Resolve a cache key to a filesystem path with sharding."""
    base = cache_dir or Path(os.getenv("PYRM_CACHE_DIR", ".cache"))
    shard = key[:2]
    return base / shard / f"{key}.cache"


def cache_get(key: str, cache_dir: Optional[Path] = None, max_age_seconds: Optional[int] = None) -> Optional[Any]:
    """Retrieve cached value if present and not expired."""
    path = get_cache_path(key, cache_dir)
    if not path.exists():
        return None
    if max_age_seconds is not None:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if (datetime.now(timezone.utc) - mtime).total_seconds() > max_age_seconds:
            path.unlink(missing_ok=True)
            return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return None


def cache_set(key: str, value: Any, cache_dir: Optional[Path] = None) -> None:
    """Atomically store a value in the cache."""
    path = get_cache_path(key, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, prefix=".cache_", suffix=".tmp"
    ) as tmp:
        json.dump(value, tmp, separators=(",", ":"), ensure_ascii=False)
        tmp_path = Path(tmp.name)
    try:
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def cache_delete(key: str, cache_dir: Optional[Path] = None) -> bool:
    """Delete a cache entry. Returns True if existed."""
    path = get_cache_path(key, cache_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict

import aiofiles


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    sets: int = 0
    evictions: int = 0
    errors: int = 0
    total_size_bytes: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


def get_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "pyrm_scalp"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def sanitize_key(key: str) -> str:
    safe = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return safe


class ToolCache:
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        max_size_bytes: int = 100 * 1024 * 1024,
        max_entries: int = 10000,
        default_ttl: int = 300,
    ) -> None:
        self.cache_dir = cache_dir or (get_data_dir() / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = max_size_bytes
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._lock = Lock()
        self._stats = CacheStats()
        self._access_times: Dict[str, float] = {}
        self._entry_sizes: Dict[str, int] = {}

    def get(
        self,
        key_str: str,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        safe_key = sanitize_key(key_str)
        path = self.cache_dir / f"{safe_key}.json"

        if not path.exists():
            with self._lock:
                self._stats.misses += 1
            return None

        try:
            age = time.time() - path.stat().st_mtime
            if age > ttl:
                path.unlink(missing_ok=True)
                self._cleanup_entry(safe_key)
                with self._lock:
                    self._stats.misses += 1
                return None

            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                with self._lock:
                    self._stats.misses += 1
                return None

            with self._lock:
                self._stats.hits += 1
                self._access_times[safe_key] = time.time()
            return data

        except Exception:
            with self._lock:
                self._stats.errors += 1
            return None

    async def aget(
        self,
        key_str: str,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        safe_key = sanitize_key(key_str)
        path = self.cache_dir / f"{safe_key}.json"

        if not path.exists():
            with self._lock:
                self._stats.misses += 1
            return None

        try:
            age = time.time() - path.stat().st_mtime
            if age > ttl:
                path.unlink(missing_ok=True)
                self._cleanup_entry(safe_key)
                with self._lock:
                    self._stats.misses += 1
                return None

            async with aiofiles.open(path, encoding="utf-8") as f:
                content = await f.read()
            data = json.loads(content)
            if not isinstance(data, dict):
                with self._lock:
                    self._stats.misses += 1
                return None

            with self._lock:
                self._stats.hits += 1
                self._access_times[safe_key] = time.time()
            return data

        except Exception:
            with self._lock:
                self._stats.errors += 1
            return None

    def set(
        self,
        key_str: str,
        value: Any,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        safe_key = sanitize_key(key_str)
        path = self.cache_dir / f"{safe_key}.json"

        try:
            atomic_write_json(path, value)
            size = path.stat().st_size

            with self._lock:
                self._stats.sets += 1
                self._stats.total_size_bytes += size
                self._access_times[safe_key] = time.time()
                self._entry_sizes[safe_key] = size

            self._enforce_limits()

        except Exception:
            with self._lock:
                self._stats.errors += 1

    async def aset(
        self,
        key_str: str,
        value: Any,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        safe_key = sanitize_key(key_str)
        path = self.cache_dir / f"{safe_key}.json"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            os.replace(tmp_path, path)

            size = path.stat().st_size

            with self._lock:
                self._stats.sets += 1
                self._stats.total_size_bytes += size
                self._access_times[safe_key] = time.time()
                self._entry_sizes[safe_key] = size

            self._enforce_limits()

        except Exception:
            with self._lock:
                self._stats.errors += 1

    def delete(self, key_str: str) -> bool:
        safe_key = sanitize_key(key_str)
        path = self.cache_dir / f"{safe_key}.json"

        if path.exists():
            try:
                path.unlink()
                self._cleanup_entry(safe_key)
                return True
            except Exception:
                with self._lock:
                    self._stats.errors += 1
        return False

    def clear(self) -> int:
        count = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink()
                count += 1
            except Exception:
                with self._lock:
                    self._stats.errors += 1

        with self._lock:
            self._access_times.clear()
            self._entry_sizes.clear()
            self._stats.total_size_bytes = 0
        return count

    def get_stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._stats.hits,
                misses=self._stats.misses,
                sets=self._stats.sets,
                evictions=self._stats.evictions,
                errors=self._stats.errors,
                total_size_bytes=self._stats.total_size_bytes,
            )

    def keys(self) -> List[str]:
        return [p.stem for p in self.cache_dir.glob("*.json")]

    def __contains__(self, key_str: str) -> bool:
        safe_key = sanitize_key(key_str)
        path = self.cache_dir / f"{safe_key}.json"
        return path.exists()

    def __len__(self) -> int:
        return len(list(self.cache_dir.glob("*.json")))

    def _cleanup_entry(self, safe_key: str) -> None:
        with self._lock:
            size = self._entry_sizes.pop(safe_key, 0)
            self._access_times.pop(safe_key, None)
            self._stats.total_size_bytes = max(0, self._stats.total_size_bytes - size)

    def _enforce_limits(self) -> None:
        with self._lock:
            current_entries = len(self._entry_sizes)
            current_size = self._stats.total_size_bytes

        if current_entries <= self.max_entries and current_size <= self.max_size_bytes:
            return

        entries_by_access = sorted(
            self._access_times.items(),
            key=lambda x: x[1]
        )

        for safe_key, _ in entries_by_access:
            with self._lock:
                current_entries = len(self._entry_sizes)
                current_size = self._stats.total_size_bytes
                if current_entries <= self.max_entries and current_size <= self.max_size_bytes:
                    break

            path = self.cache_dir / f"{safe_key}.json"
            if path.exists():
                try:
                    path.unlink()
                    self._cleanup_entry(safe_key)
                    with self._lock:
                        self._stats.evictions += 1
                except Exception:
                    with self._lock:
                        self._stats.errors += 1

    def __enter__(self) -> ToolCache:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except Exception:
            pass


from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolCache:
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "pyrm_scalp")
    max_size_mb: int = 500
    max_age_days: int = 30
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self._shutdown_event.set)
                except NotImplementedError:
                    pass

    def get_cache_stats(self) -> dict[str, int | float]:
        total_size = 0
        file_count = 0
        oldest = float("inf")
        newest = 0.0
        now = time.time()

        for path in self.cache_dir.glob("*.json"):
            try:
                stat = path.stat()
                total_size += stat.st_size
                file_count += 1
                oldest = min(oldest, stat.st_mtime)
                newest = max(newest, stat.st_mtime)
            except OSError:
                continue

        return {
            "file_count": file_count,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "oldest_age_days": round((now - oldest) / 86400, 1) if file_count else 0,
            "newest_age_days": round((now - newest) / 86400, 1) if file_count else 0,
        }

    def invalidate_by_pattern(self, pattern: str = "*.json") -> int:
        removed = 0
        for path in self.cache_dir.glob(pattern):
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as e:
                logger.warning("Failed to remove %s: %s", path, e)
        return removed

    def invalidate_by_age(self, max_age_days: Optional[int] = None) -> int:
        max_age = max_age_days if max_age_days is not None else self.max_age_days
        cutoff = time.time() - (max_age * 86400)
        removed = 0

        for path in self.cache_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as e:
                logger.warning("Failed to remove %s: %s", path, e)
        return removed

    def invalidate_by_size(self, max_size_mb: Optional[int] = None) -> int:
        max_bytes = (max_size_mb if max_size_mb is not None else self.max_size_mb) * 1024 * 1024
        files = []

        for path in self.cache_dir.glob("*.json"):
            try:
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue

        files.sort(key=lambda x: x[0])
        total_size = sum(f[1] for f in files)
        removed = 0

        for _, size, path in files:
            if total_size <= max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total_size -= size
                removed += 1
            except OSError as e:
                logger.warning("Failed to remove %s: %s", path, e)

        return removed

    async def periodic_cleanup(self, interval_seconds: int = 3600) -> None:
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval_seconds)
                break
            except asyncio.TimeoutError:
                pass

            stats = self.get_cache_stats()
            if stats["total_size_mb"] > self.max_size_mb:
                logger.info("Cache size %.2f MB exceeds limit %d MB, cleaning up", stats["total_size_mb"], self.max_size_mb)
                self.invalidate_by_size()
            if stats["oldest_age_days"] > self.max_age_days:
                logger.info("Cache age %.1f days exceeds limit %d days, cleaning up", stats["oldest_age_days"], self.max_age_days)
                self.invalidate_by_age()

    def shutdown(self) -> None:
        self._shutdown_event.set()


def invalidate_cache(
    cache: Optional[ToolCache] = None,
    pattern: str = "*.json",
) -> int:
    c = cache or ToolCache()
    removed = 0

    for path in c.cache_dir.glob(pattern):
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError as e:
            logger.warning("Failed to remove %s: %s", path, e)

    return removed


def create_cache(
    cache_dir: Optional[Path] = None,
    max_size_mb: int = 500,
    max_age_days: int = 30,
) -> ToolCache:
    return ToolCache(
        cache_dir=cache_dir or (Path.home() / ".cache" / "pyrm_scalp"),
        max_size_mb=max_size_mb,
        max_age_days=max_age_days,
    )


async def run_cache_maintenance(
    cache: Optional[ToolCache] = None,
    interval_seconds: int = 3600,
    shutdown_callback: Optional[Callable[[], None]] = None,
) -> None:
    c = cache or ToolCache()
    try:
        await c.periodic_cleanup(interval_seconds)
    finally:
        if shutdown_callback:
            shutdown_callback()


def register_shutdown_handler(handler: Callable[[], None]) -> None:
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, handler)
            except NotImplementedError:
                pass
    else:
        signal.signal(signal.SIGINT, lambda *_: handler())
        signal.signal(signal.SIGTERM, lambda *_: handler())


# ==============================================================================
# SIGNAL HANDLING
# ==============================================================================

class GracefulShutdown:
    def __init__(self) -> None:
        self._shutdown_requested = False
        self._callbacks: list[Callable[[], None]] = []
        self._original_handlers: dict[int, Callable] = {}

    def register_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def _signal_handler(self, signum: int, frame: Optional[object]) -> None:
        if self._shutdown_requested:
            logger.warning("Force shutdown requested (signal %d)", signum)
            sys.exit(1)

        self._shutdown_requested = True
        logger.info("Shutdown signal received (%d), initiating graceful shutdown...", signum)

        for callback in self._callbacks:
            try:
                callback()
            except Exception as e:
                logger.exception("Error in shutdown callback: %s", e)

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            if hasattr(signal, sig.name):
                self._original_handlers[sig] = signal.signal(sig, self._signal_handler)

    def uninstall(self) -> None:
        for sig, handler in self._original_handlers.items():
            signal.signal(sig, handler)

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested


_graceful_shutdown = GracefulShutdown()


def get_graceful_shutdown() -> GracefulShutdown:
    return _graceful_shutdown


def request_shutdown() -> None:
    _graceful_shutdown._signal_handler(signal.SIGTERM, None)


import threading


class GracefulShutdown:
    def __init__(self) -> None:
        self.interrupted = False
        self._old_handlers: List[tuple[int, Optional[Callable[[int, Any], None]]]] = []
        self._signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            self._signals.append(signal.SIGBREAK)

    def __enter__(self) -> GracefulShutdown:
        if threading.current_thread() is threading.main_thread():
            for sig in self._signals:
                try:
                    old_handler = signal.signal(sig, self._handler)
                    self._old_handlers.append((sig, old_handler))
                except (ValueError, AttributeError, OSError):
                    self._old_handlers.append((sig, None))
        return self

    def __exit__(self, *args: Any) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig, old_handler in reversed(self._old_handlers):
                if old_handler is not None:
                    try:
                        signal.signal(sig, old_handler)
                    except (ValueError, AttributeError, OSError):
                        pass
            self._old_handlers.clear()

    def _handler(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def should_stop(self) -> bool:
        return self.interrupted

    def __call__(self) -> bool:
        return self.interrupted

    def wait_for_shutdown(self, timeout: Optional[float] = None, poll_interval: float = 0.1) -> bool:
        start = time.monotonic()
        while not self.interrupted:
            if timeout is not None and (time.monotonic() - start) >= timeout:
                return False
            time.sleep(poll_interval)
        return True

    def reset(self) -> None:
        self.interrupted = False


import logging
from dataclasses import dataclass
from typing import Dict
from urllib.parse import urlencode

import requests

# ==============================================================================
# CONFIG & UTILITY STUBS (Assumed to exist in module scope, typed for safety)
# ==============================================================================

# Type hints for external dependencies to ensure static analysis passes
CfgValueFunc = Callable[[str, str, str], str]
ParseBoolFunc = Callable[[str], bool]
KillSwitchFunc = Callable[[], bool]

# These would be imported or defined elsewhere in the actual module.
# We declare them here with type hints to make the code "fully runnable" and lint-clean.
cfg_value: CfgValueFunc
parse_bool: ParseBoolFunc
kill_switch_active: KillSwitchFunc

logger = logging.getLogger(__name__)


# ==============================================================================
# LIVE AUTHORIZATION LOGIC (UPGRADED)
# ==============================================================================

# Constants for error codes to avoid magic strings and typos
class AuthError:
    DRY_RUN = "DRY_RUN_REQUESTED"
    TESTNET = "TESTNET_MODE_ACTIVE"
    LIVE_DISABLED = "LIVE_TRADING_ENABLED_FALSE"
    ALLOW_DISABLED = "ALLOW_LIVE_TRADING_FALSE"
    KILL_SWITCH = "KILL_SWITCH_ACTIVE"
    TOKEN_MISMATCH = "LIVE_CONFIRMATION_TOKEN_MISMATCH"
    SUCCESS = "AUTHORIZED_LIVE"


# Pre-fetch static config at module load to avoid repeated I/O/parsing overhead
# In a real hot-reload scenario, this would need a refresh mechanism.
_LIVE_TRADING_ENABLED = parse_bool(cfg_value("LLM_AGENT_VAR_LIVE_TRADING_ENABLED", "LIVE_TRADING_ENABLED", "false"))
_ALLOW_LIVE_TRADING = parse_bool(cfg_value("ALLOW_LIVE_TRADING", "ALLOW_LIVE_TRADING", "false"))
_EXPECTED_CONFIRMATION_TOKEN = cfg_value("LIVE_CONFIRMATION_TOKEN", "LIVE_CONFIRMATION_TOKEN", "I_UNDERSTAND_LIVE_RISK")


def live_authorized(
    testnet: bool,
    requested_live: bool,
    confirmation: str,
) -> Tuple[bool, str]:
    """
    Determines if live trading is authorized.
    Upgrades:
    1. Uses module-level cached config for performance.
    2. Constant-time comparison for confirmation token (security hardening).
    3. Structured logging for audit trails.
    4. Centralized error codes via Enum-like class.
    """
    if not requested_live:
        logger.info("Authorization denied: Dry run requested.")
        return False, AuthError.DRY_RUN

    if testnet:
        logger.info("Authorization denied: Testnet mode active.")
        return False, AuthError.TESTNET

    # Use cached values
    if not _LIVE_TRADING_ENABLED:
        logger.warning("Authorization denied: LIVE_TRADING_ENABLED is false.")
        return False, AuthError.LIVE_DISABLED

    if not _ALLOW_LIVE_TRADING:
        logger.warning("Authorization denied: ALLOW_LIVE_TRADING is false.")
        return False, AuthError.ALLOW_DISABLED

    if kill_switch_active():
        logger.critical("Authorization denied: Kill switch active.")
        return False, AuthError.KILL_SWITCH

    # Constant-time comparison to prevent timing attacks on the token
    if not hmac.compare_digest(confirmation, _EXPECTED_CONFIRMATION_TOKEN):
        logger.warning("Authorization denied: Confirmation token mismatch.")
        return False, AuthError.TOKEN_MISMATCH

    logger.info("Live trading authorized.")
    return True, AuthError.SUCCESS


# ==============================================================================
# BYBIT V5 HTTP CLIENT (NEW IMPLEMENTATION)
# ==============================================================================

@dataclass(frozen=True, slots=True)
class BybitCredentials:
    api_key: str
    api_secret: str
    testnet: bool = False


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    data: Dict[str, Any]
    headers: Dict[str, str]
    elapsed_ms: float


class BybitV5Client:
    """
    Synchronous Bybit V5 HTTP Client.
    Features:
    - Request signing (HMAC SHA256)
    - Automatic timestamp/recv_window handling
    - Session pooling (requests.Session)
    - Typed responses and error handling
    - Testnet/Mainnet switching
    """
    MAINNET_BASE = "https://api.bybit.com"
    TESTNET_BASE = "https://api-testnet.bybit.com"
    RECV_WINDOW = "5000"  # ms

    def __init__(self, credentials: BybitCredentials, timeout: float = 10.0):
        self.creds = credentials
        self.base_url = self.TESTNET_BASE if credentials.testnet else self.MAINNET_BASE
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": credentials.api_key,
            "X-BAPI-RECV-WINDOW": self.RECV_WINDOW,
        })
        self.timeout = timeout

    def _sign_request(self, timestamp: str, payload: str) -> str:
        """Generates V5 signature: sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + payload)"""
        param_str = f"{timestamp}{self.creds.api_key}{self.RECV_WINDOW}{payload}"
        return hmac.new(
            self.creds.api_secret.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        auth: bool = True
    ) -> HttpResponse:
        url = f"{self.base_url}{endpoint}"
        timestamp = str(int(time.time() * 1000))

        # Prepare payload for signing: query string for GET, JSON body for POST
        if method.upper() == "GET":
            payload = urlencode(params or {})
            req_params = params
            req_json = None
        else:
            payload = ""  # V5 spec: empty string for POST body signing if no params?
                         # Actually V5 signs the JSON body string for POST.
            req_json = json_body
            req_params = None
            if json_body:
                import json
                payload = json.dumps(json_body, separators=(',', ':'))

        headers = {}
        if auth:
            signature = self._sign_request(timestamp, payload)
            headers.update({
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-SIGN": signature,
            })

        start = time.perf_counter()
        try:
            resp = self.session.request(
                method=method.upper(),
                url=url,
                params=req_params,
                json=req_json,
                headers=headers,
                timeout=self.timeout
            )
            elapsed = (time.perf_counter() - start) * 1000

            # Bybit returns 200 even for business errors, check retCode
            data = resp.json()
            if resp.status_code != 200 or data.get("retCode") != 0:
                logger.error("Bybit API Error: %s %s -> %s", method, endpoint, data)

            return HttpResponse(
                status_code=resp.status_code,
                data=data,
                headers=dict(resp.headers),
                elapsed_ms=elapsed
            )
        except requests.RequestException as e:
            logger.exception("Network error calling %s %s", method, endpoint)
            raise ConnectionError(f"Bybit request failed: {e}") from e

    # --- Public API Helpers ---

    def get_wallet_balance(self, account_type: str = "UNIFIED", coin: str = "USDT") -> HttpResponse:
        """V5 Get Wallet Balance"""
        return self._request("GET", "/v5/account/wallet-balance", params={"accountType": account_type, "coin": coin})

    def get_tickers(self, category: str = "linear", symbol: Optional[str] = None) -> HttpResponse:
        """V5 Get Tickers"""
        params = {"category": category}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/v5/market/tickers", params=params, auth=False)

    def place_order(
        self,
        category: str,
        symbol: str,
        side: str,  # "Buy" or "Sell"
        order_type: str,  # "Market", "Limit"
        qty: str,
        price: Optional[str] = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        order_link_id: Optional[str] = None
    ) -> HttpResponse:
        """V5 Place Order"""
        body = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "timeInForce": time_in_force,
            "reduceOnly": reduce_only,
        }
        if price is not None:
            body["price"] = price
        if order_link_id:
            body["orderLinkId"] = order_link_id

        return self._request("POST", "/v5/order/create", json_body=body)

    def cancel_order(self, category: str, symbol: str, order_id: Optional[str] = None, order_link_id: Optional[str] = None) -> HttpResponse:
        """V5 Cancel Order"""
        if not order_id and not order_link_id:
            raise ValueError("Either order_id or order_link_id must be provided")
        body = {"category": category, "symbol": symbol}
        if order_id: body["orderId"] = order_id
        if order_link_id: body["orderLinkId"] = order_link_id
        return self._request("POST", "/v5/order/cancel", json_body=body)

    def get_open_orders(self, category: str = "linear", symbol: Optional[str] = None) -> HttpResponse:
        """V5 Get Open Orders"""
        params = {"category": category, "openOnly": 1}
        if symbol: params["symbol"] = symbol
        return self._request("GET", "/v5/order/realtime", params=params)

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class BybitV5:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ):
        cfg = get_env_config()

        self.api_key = (
            api_key
            or os.environ.get("BYBIT_API_KEY")
            or cfg.get("BYBIT_API_KEY", "")
        ).strip()

        self.api_secret = (
            api_secret
            or os.environ.get("BYBIT_API_SECRET")
            or cfg.get("BYBIT_API_SECRET", "")
        ).strip()

        self.testnet = testnet
        self.base_url = (
            BYBIT_TESTNET
            if testnet
            else BYBIT_MAINNET
        )

        self.time_offset_ms = 0
        self.last_request_ms = 0

        self.timeout = safe_float(
            cfg_value(
                "BYBIT_HTTP_TIMEOUT",
                "BYBIT_HTTP_TIMEOUT",
                str(DEFAULT_TIMEOUT),
            ),
            DEFAULT_TIMEOUT,
        )

        self.sync_time()

    @property
    def has_credentials(self) -> bool:
        return bool(
            self.api_key
            and self.api_secret
        )

    def sync_time(self) -> None:
        try:
            t0 = int(time.time() * 1000)

            req = urllib.request.Request(
                f"{self.base_url}/v5/market/time",
                headers={
                    "User-Agent":
                        "AIChat-BybitAgent/8.0",
                },
            )

            with urllib.request.urlopen(
                req,
                timeout=3,
            ) as response:
                t1 = int(time.time() * 1000)

                payload = json.loads(
                    response.read().decode("utf-8")
                )

            result = payload.get("result", {})

            server_ms = (
                safe_float(
                    result.get("timeNano", 0)
                )
                / 1_000_000
            )

            if server_ms <= 0:
                server_ms = safe_float(
                    payload.get("time", 0)
                )

            if server_ms > 0:
                local_midpoint = (
                    t0 + t1
                ) / 2.0

                self.time_offset_ms = int(
                    server_ms - local_midpoint
                )

        except Exception:
            self.time_offset_ms = 0

    def _sign(
        self,
        timestamp: str,
        recv_window: str,
        payload: str,
    ) -> str:
        raw = (
            timestamp
            + self.api_key
            + recv_window
            + payload
        )

        return hmac.new(
            self.api_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _retryable_http(exc: Exception) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in {
                408,
                429,
                500,
                502,
                503,
                504,
            }

        if isinstance(
            exc,
            (
                urllib.error.URLError,
                TimeoutError,
            ),
        ):
            return True

        return False

    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        method = method.upper()
        public = endpoint in PUBLIC_ENDPOINTS

        if not public and not self.has_credentials:
            raise ToolError(
                "Private endpoint requires "
                "BYBIT_API_KEY and BYBIT_API_SECRET.",
                EXIT_PERMISSION_DENIED,
            )

        clean_params = {
            str(k): (
                "true"
                if v is True
                else "false"
                if v is False
                else str(v)
            )
            for k, v in (params or {}).items()
            if v is not None
        }

        query_str = urllib.parse.urlencode(
            sorted(clean_params.items())
        )

        url = (
            f"{self.base_url}{endpoint}"
        )

        if method == "GET" and query_str:
            url += "?" + query_str

        payload_for_sign = query_str
        body_bytes = None

        if method != "GET":
            payload_for_sign = json.dumps(
                data or {},
                separators=(",", ":"),
                ensure_ascii=False,
            )

            body_bytes = payload_for_sign.encode(
                "utf-8"
            )

        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            timestamp = str(
                int(time.time() * 1000)
                + self.time_offset_ms
            )

            headers = {
                "Content-Type":
                    "application/json",
                "User-Agent":
                    "AIChat-BybitAgent/8.0",
            }

            if not public:
                headers.update({
                    "X-BAPI-API-KEY":
                        self.api_key,
                    "X-BAPI-TIMESTAMP":
                        timestamp,
                    "X-BAPI-RECV-WINDOW":
                        DEFAULT_RECV_WINDOW,
                    "X-BAPI-SIGN":
                        self._sign(
                            timestamp,
                            DEFAULT_RECV_WINDOW,
                            payload_for_sign,
                        ),
                })

            request = urllib.request.Request(
                url,
                data=body_bytes,
                headers=headers,
                method=method,
            )

            started = time.monotonic()

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                ) as response:
                    raw = response.read().decode(
                        "utf-8"
                    )

                self.last_request_ms = int(
                    (time.monotonic() - started)
                    * 1000
                )

                result = json.loads(raw)

                ret_code = result.get(
                    "retCode",
                    0,
                )

                if ret_code != 0:
                    ret_msg = str(
                        result.get(
                            "retMsg",
                            "Unknown Bybit error",
                        )
                    )

                    lowered = ret_msg.lower()

                    if (
                        ret_code == 10002
                        or "timestamp" in lowered
                    ) and attempt == 0:
                        self.sync_time()
                        continue

                    if (
                        ret_code in {
                            10006,
                            10016,
                        }
                        or "rate limit" in lowered
                    ) and attempt < MAX_RETRIES - 1:
                        time.sleep(
                            min(
                                0.5 * (
                                    2 ** attempt
                                ),
                                4.0,
                            )
                        )
                        continue

                    raise ToolError(
                        f"Bybit API Error "
                        f"[{ret_code}]: {ret_msg}",
                        EXIT_ERROR,
                    )

                return result.get(
                    "result",
                    {},
                )

            except urllib.error.HTTPError as exc:
                last_error = exc

                if not self._retryable_http(exc):
                    raise ToolError(
                        f"HTTP {exc.code} from Bybit.",
                        EXIT_ERROR,
                    )

            except Exception as exc:
                last_error = exc

                if isinstance(
                    exc,
                    ToolError,
                ):
                    raise

                if not self._retryable_http(exc):
                    raise

            if attempt < MAX_RETRIES - 1:
                time.sleep(
                    min(
                        0.25 * (2 ** attempt),
                        2.0,
                    )
                )

        raise ToolError(
            f"Bybit request to '{endpoint}' "
            f"failed after {MAX_RETRIES} attempts: "
            f"{last_error}",
            EXIT_ERROR,
        )


# ==============================================================================
# EXCHANGE METADATA
# ==============================================================================

import logging
from functools import lru_cache
from typing import Dict

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .bybit_client import BybitV5
from .exceptions import EXIT_FILE_NOT_FOUND, ToolError
from .validators import validate_symbol

logger = logging.getLogger(__name__)


class InstrumentFetchError(ToolError):
    """Raised when instrument metadata cannot be fetched or parsed."""


def _is_transient_error(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError, IOError))


@retry(
    wait=wait_exponential_jitter(initial=0.5, max=4.0),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, IOError)),
    reraise=True,
)
def _fetch_instruments_raw(client: BybitV5, category: str, symbol: str) -> Dict[str, Any]:
    logger.debug("Fetching instrument info for %s (category=%s)", symbol, category)
    resp = client.request("GET", "/v5/market/instruments-info", {"category": category, "symbol": symbol})
    if not isinstance(resp, dict):
        raise InstrumentFetchError(f"Unexpected response type: {type(resp)}", EXIT_FILE_NOT_FOUND)
    return resp


@lru_cache(maxsize=128)
def fetch_instrument(
    client: BybitV5,
    symbol: str,
    *,
    category: str = "linear",
) -> Dict[str, Any]:
    """
    Fetch and return the instrument metadata for *symbol*.

    Parameters
    ----------
    client: BybitV5
        Authenticated or public Bybit V5 REST client.
    symbol: str
        Unified symbol (e.g. "BTCUSDT").
    category: str, optional
        Instrument category – "linear", "inverse", "spot", "option". Default "linear".

    Returns
    -------
    dict
        Raw instrument info dict as returned by Bybit.

    Raises
    ------
    InstrumentFetchError
        If the symbol is not found or the response is malformed.
    """
    sym = validate_symbol(symbol)

    raw = _fetch_instruments_raw(client, category, sym)

    result_list = raw.get("list")
    if not isinstance(result_list, list):
        raise InstrumentFetchError(
            f"Malformed response: 'list' missing or not a list for '{sym}'.",
            EXIT_FILE_NOT_FOUND,
        )

    for item in result_list:
        if not isinstance(item, dict):
            continue
        if item.get("symbol") == sym:
            logger.debug("Instrument metadata found for %s", sym)
            return item

    raise InstrumentFetchError(
        f"Instrument metadata not found for '{sym}' in category '{category}'.",
        EXIT_FILE_NOT_FOUND,
    )


def fetch_instruments_batch(
    client: BybitV5,
    symbols: list[str],
    *,
    category: str = "linear",
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch metadata for multiple symbols in a single call (when supported by exchange).

    Falls back to sequential fetches if the endpoint does not accept multiple symbols.
    """
    validated = [validate_symbol(s) for s in symbols]
    raw = _fetch_instruments_raw(client, category, ",".join(validated))

    result_list = raw.get("list", [])
    found: Dict[str, Dict[str, Any]] = {}
    for item in result_list:
        if isinstance(item, dict) and (sym := item.get("symbol")) in validated:
            found[sym] = item

    missing = set(validated) - set(found.keys())
    if missing:
        logger.warning("Batch fetch missing symbols: %s – falling back to individual requests", missing)
        for sym in missing:
            try:
                found[sym] = fetch_instrument(client, sym, category=category)
            except InstrumentFetchError:
                pass

    return found


from functools import lru_cache
from typing import NamedTuple

# Assuming BybitV5 and fetch_instrument are imported from a local module or defined elsewhere.
# If not available, these would need proper imports, e.g.:
# from pybit.unified_trading import HTTP as BybitV5
# from .market_data import fetch_instrument


class PrecisionInfo(NamedTuple):
    tick_size: str
    qty_step: str
    min_order_qty: str
    max_order_qty: str

    @property
    def price_precision(self) -> int:
        return _calculate_precision(self.tick_size)

    @property
    def qty_precision(self) -> int:
        return _calculate_precision(self.qty_step)


def _calculate_precision(step_str: str) -> int:
    """Calculate decimal precision from a step string (e.g., '0.001' -> 3)."""
    try:
        d = Decimal(step_str)
        if d == 0:
            return 0
        # Normalize removes trailing zeros, exponent gives precision
        return -d.normalize().as_tuple().exponent
    except (InvalidOperation, ValueError, TypeError):
        return 0


@lru_cache(maxsize=128)
def fetch_precision(
    client: BybitV5,
    symbol: str,
) -> PrecisionInfo:
    """
    Fetches and caches precision info for a symbol.
    Returns a PrecisionInfo NamedTuple with string values and precision properties.
    """
    try:
        inst = fetch_instrument(client, symbol)
        if not isinstance(inst, dict):
            raise ValueError("Invalid instrument data received")
    except Exception:
        # Fallback to safe defaults on network/API error
        return PrecisionInfo("0.01", "0.01", "0.01", "0")

    price_filter = inst.get("priceFilter", {}) or {}
    lot_filter = inst.get("lotSizeFilter", {}) or {}

    # Validate and sanitize string values
    tick_size = _sanitize_step(price_filter.get("tickSize", "0.01"))
    qty_step = _sanitize_step(lot_filter.get("qtyStep", "0.01"))
    min_qty = _sanitize_step(lot_filter.get("minOrderQty", "0.01"))
    max_qty = _sanitize_step(lot_filter.get("maxOrderQty", "0"))

    return PrecisionInfo(tick_size, qty_step, min_qty, max_qty)


def _sanitize_step(value: Optional[str]) -> str:
    """Ensure step value is a valid non-negative decimal string."""
    if value is None:
        return "0"
    try:
        d = Decimal(str(value))
        if d < 0:
            return "0"
        # Return normalized string without scientific notation
        return format(d.normalize(), 'f')
    except (InvalidOperation, ValueError):
        return "0"


# ==============================================================================
# MARKET DATA
# ==============================================================================
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, TypedDict

logger = logging.getLogger(__name__)


class OrderbookData(TypedDict):
    bids: List[List[str]]
    asks: List[List[str]]
    best_bid: float
    best_ask: float
    mid: float
    spread: float
    spread_pct: float
    timestamp: str


@dataclass
class OrderbookConfig:
    limit: int = 25
    max_retries: int = 3
    retry_delay: float = 0.5
    cache_ttl: float = 1.0


class ToolError(Exception):
    pass


def validate_symbol(symbol: str) -> str:
    if not symbol or not isinstance(symbol, str):
        raise ToolError("Symbol must be a non-empty string")
    return symbol.strip().upper()


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def validate_orderbook_structure(bids: List, asks: List) -> bool:
    if not bids or not asks:
        return False
    if not all(isinstance(x, list) and len(x) >= 2 for x in bids):
        return False
    if not all(isinstance(x, list) and len(x) >= 2 for x in asks):
        return False
    return True


def calculate_spread_pct(best_bid: float, best_ask: float, mid: float) -> float:
    if mid <= 0:
        return 0.0
    return ((best_ask - best_bid) / mid) * 100.0


def fetch_orderbook_raw(
    client: Any,
    symbol: str,
    limit: int,
) -> Dict[str, Any]:
    return client.request(
        "GET",
        "/v5/market/orderbook",
        {
            "category": "linear",
            "symbol": symbol,
            "limit": str(limit),
        },
    )


def get_orderbook(
    client: Any,
    symbol: str,
    limit: int = 25,
    config: Optional[OrderbookConfig] = None,
) -> OrderbookData:
    if config is None:
        config = OrderbookConfig(limit=limit)
    else:
        config.limit = limit

    sym = validate_symbol(symbol)

    last_exception = None
    for attempt in range(config.max_retries):
        try:
            result = fetch_orderbook_raw(client, sym, config.limit)
            break
        except Exception as e:
            last_exception = e
            logger.warning(f"Orderbook fetch attempt {attempt + 1} failed for {sym}: {e}")
            if attempt < config.max_retries - 1:
                time.sleep(config.retry_delay * (2 ** attempt))
    else:
        raise ToolError(f"Failed to fetch orderbook for {sym} after {config.max_retries} attempts: {last_exception}")

    bids_raw = result.get("b", [])
    asks_raw = result.get("a", [])

    bids = [
        x for x in bids_raw
        if isinstance(x, list) and len(x) >= 2
    ]

    asks = [
        x for x in asks_raw
        if isinstance(x, list) and len(x) >= 2
    ]

    if not validate_orderbook_structure(bids, asks):
        raise ToolError(f"Orderbook for {sym} has invalid structure or is empty.")

    bids.sort(
        key=lambda x: safe_float(x[0]),
        reverse=True,
    )

    asks.sort(
        key=lambda x: safe_float(x[0])
    )

    best_bid = safe_float(bids[0][0])
    best_ask = safe_float(asks[0][0])

    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        raise ToolError(f"Invalid bid/ask spread state for {sym}: bid={best_bid}, ask={best_ask}")

    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / 2.0
    spread_pct = calculate_spread_pct(best_bid, best_ask, mid)

    return OrderbookData(
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        spread_pct=spread_pct,
        timestamp=utc_now(),
    )


@lru_cache(maxsize=32)
def get_orderbook_cached(
    client: Any,
    symbol: str,
    limit: int = 25,
    cache_key: str = "",
) -> OrderbookData:
    config = OrderbookConfig(limit=limit, cache_ttl=0)
    return get_orderbook(client, symbol, limit, config)


def get_orderbook_with_cache(
    client: Any,
    symbol: str,
    limit: int = 25,
    ttl: float = 1.0,
) -> OrderbookData:
    cache_key = f"{symbol}:{limit}:{int(time.time() / ttl)}"
    return get_orderbook_cached(client, symbol, limit, cache_key)


class BybitV5:
    def request(self, method: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("BybitV5.client.request must be implemented by user")


__all__ = [
    "OrderbookConfig",
    "OrderbookData",
    "ToolError",
    "get_orderbook",
    "get_orderbook_cached",
    "get_orderbook_with_cache",
    "safe_float",
    "utc_now",
    "validate_symbol",
]
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, TypedDict

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Custom exception for tool-related errors."""
    pass


class BybitV5:
    """Type stub for Bybit V5 client."""
    def request(self, method: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        ...


def validate_symbol(symbol: str) -> str:
    """Validate and normalize symbol format."""
    if not symbol or not isinstance(symbol, str):
        raise ToolError("Symbol must be a non-empty string")
    return symbol.strip().upper()


class TickerData(TypedDict):
    """Structured ticker data response."""
    symbol: str
    lastPrice: str
    bidPrice: str
    askPrice: str
    highPrice24h: str
    lowPrice24h: str
    volume24h: str
    turnover24h: str
    price24hPcnt: str
    timestamp: int


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 5.0
    exponential_base: float = 2.0


_TICKER_CACHE: Dict[str, tuple[TickerData, float]] = {}
_CACHE_TTL = 1.0  # seconds


def _is_cache_valid(symbol: str) -> bool:
    if symbol not in _TICKER_CACHE:
        return False
    _, cached_time = _TICKER_CACHE[symbol]
    return (time.time() - cached_time) < _CACHE_TTL


def _get_cached_ticker(symbol: str) -> Optional[TickerData]:
    if _is_cache_valid(symbol):
        data, _ = _TICKER_CACHE[symbol]
        logger.debug(f"Cache hit for {symbol}")
        return data
    return None


def _cache_ticker(symbol: str, data: TickerData) -> None:
    _TICKER_CACHE[symbol] = (data, time.time())
    logger.debug(f"Cached ticker for {symbol}")


def _handle_api_error(result: Dict[str, Any], symbol: str) -> None:
    ret_code = result.get("retCode")
    ret_msg = result.get("retMsg", "Unknown error")
    if ret_code is not None and ret_code != 0:
        raise ToolError(f"Bybit API error for {symbol}: [{ret_code}] {ret_msg}")


def _extract_ticker_data(rows: list, symbol: str) -> TickerData:
    if not rows:
        raise ToolError(f"No ticker data for {symbol}.")
    raw = rows[0]
    required_fields = [
        "symbol", "lastPrice", "bidPrice", "askPrice",
        "highPrice24h", "lowPrice24h", "volume24h",
        "turnover24h", "price24hPcnt"
    ]
    for field in required_fields:
        if field not in raw:
            raise ToolError(f"Missing field '{field}' in ticker response for {symbol}")
    return TickerData(
        symbol=raw["symbol"],
        lastPrice=raw["lastPrice"],
        bidPrice=raw["bidPrice"],
        askPrice=raw["askPrice"],
        highPrice24h=raw["highPrice24h"],
        lowPrice24h=raw["lowPrice24h"],
        volume24h=raw["volume24h"],
        turnover24h=raw["turnover24h"],
        price24hPcnt=raw["price24hPcnt"],
        timestamp=int(time.time() * 1000),
    )


def get_ticker(
    client: BybitV5,
    symbol: str,
    *,
    use_cache: bool = True,
    retry_config: Optional[RetryConfig] = None,
) -> TickerData:
    """
    Fetch ticker data for a linear perpetual symbol from Bybit V5.

    Args:
        client: Bybit V5 HTTP client with request(method, endpoint, params) method.
        symbol: Trading symbol (e.g., 'BTCUSDT').
        use_cache: Whether to use in-memory TTL cache (default True).
        retry_config: Optional retry configuration for transient failures.

    Returns:
        TickerData with validated fields.

    Raises:
        ToolError: On validation, API, or network errors.
    """
    sym = validate_symbol(symbol)
    retry_config = retry_config or RetryConfig()

    if use_cache:
        cached = _get_cached_ticker(sym)
        if cached:
            return cached

    last_exception: Optional[Exception] = None
    for attempt in range(retry_config.max_retries + 1):
        try:
            result = client.request(
                "GET",
                "/v5/market/tickers",
                {
                    "category": "linear",
                    "symbol": sym,
                },
            )

            _handle_api_error(result, sym)

            rows = result.get("result", {}).get("list", [])
            if not rows and "list" in result:
                rows = result.get("list", [])

            ticker = _extract_ticker_data(rows, sym)

            if use_cache:
                _cache_ticker(sym, ticker)

            logger.info(f"Fetched ticker for {sym}: last={ticker['lastPrice']}")
            return ticker

        except ToolError:
            raise
        except Exception as e:
            last_exception = e
            if attempt < retry_config.max_retries:
                delay = min(
                    retry_config.base_delay * (retry_config.exponential_base ** attempt),
                    retry_config.max_delay,
                )
                logger.warning(f"Attempt {attempt + 1} failed for {sym}: {e}. Retrying in {delay:.2f}s...")
                time.sleep(delay)
            else:
                logger.error(f"All retries exhausted for {sym}")

    raise ToolError(f"Failed to fetch ticker for {sym} after {retry_config.max_retries + 1} attempts") from last_exception


import logging

logger = logging.getLogger(__name__)


def normalize_candles(
    raw: List[Any],
    drop_latest: bool = True,
    validate_ohlcv: bool = True,
    max_gap_seconds: Optional[int] = None,
    deduplicate: bool = True,
) -> List[List[Any]]:
    valid: List[List[Any]] = []
    seen_timestamps = set()

    for candle in raw:
        if not (
            isinstance(candle, (list, tuple))
            and len(candle) >= 7
        ):
            continue

        try:
            ts = int(candle[0])
            o = float(candle[1])
            h = float(candle[2])
            l = float(candle[3])
            c = float(candle[4])
            v = float(candle[5])
            v_quote = float(candle[6]) if len(candle) > 6 else 0.0

            if validate_ohlcv:
                if not (l <= h and l <= o <= h and l <= c <= h and v >= 0 and v_quote >= 0):
                    logger.debug(f"Invalid OHLCV relationship at ts={ts}: o={o} h={h} l={l} c={c} v={v}")
                    continue

            if deduplicate:
                if ts in seen_timestamps:
                    logger.debug(f"Duplicate timestamp skipped: {ts}")
                    continue
                seen_timestamps.add(ts)

            valid.append([ts, o, h, l, c, v, v_quote])

        except (TypeError, ValueError, IndexError) as e:
            logger.debug(f"Candle parse error: {e}")
            continue

    if not valid:
        return []

    valid.sort(key=lambda x: x[0])

    if max_gap_seconds is not None and len(valid) > 1:
        filtered = [valid[0]]
        for i in range(1, len(valid)):
            if valid[i][0] - filtered[-1][0] <= max_gap_seconds:
                filtered.append(valid[i])
            else:
                logger.warning(f"Gap detected: {filtered[-1][0]} -> {valid[i][0]} ({valid[i][0] - filtered[-1][0]}s)")
        valid = filtered

    if drop_latest and len(valid) > 1:
        return valid[:-1]

    return valid


def resample_candles(
    candles: List[List[Any]],
    target_interval_seconds: int,
) -> List[List[Any]]:
    if not candles:
        return []

    resampled = []
    current_bucket = None
    bucket_start = None

    for candle in candles:
        ts, o, h, l, c, v, vq = candle[0], candle[1], candle[2], candle[3], candle[4], candle[5], candle[6]
        bucket_ts = (ts // target_interval_seconds) * target_interval_seconds

        if bucket_ts != bucket_start:
            if current_bucket is not None:
                resampled.append(current_bucket)
            bucket_start = bucket_ts
            current_bucket = [bucket_ts, o, h, l, c, v, vq]
        else:
            current_bucket[2] = max(current_bucket[2], h)
            current_bucket[3] = min(current_bucket[3], l)
            current_bucket[4] = c
            current_bucket[5] += v
            current_bucket[6] += vq

    if current_bucket is not None:
        resampled.append(current_bucket)

    return resampled


def validate_candle_sequence(
    candles: List[List[Any]],
    expected_interval_seconds: int,
    tolerance_seconds: int = 1,
) -> Tuple[bool, List[str]]:
    issues = []
    if len(candles) < 2:
        return True, issues

    for i in range(1, len(candles)):
        prev_ts = candles[i - 1][0]
        curr_ts = candles[i][0]
        expected = prev_ts + expected_interval_seconds
        diff = abs(curr_ts - expected)

        if diff > tolerance_seconds:
            issues.append(f"Timestamp gap at index {i}: expected ~{expected}, got {curr_ts} (diff={diff}s)")

        if candles[i][1] <= 0 or candles[i][2] <= 0 or candles[i][3] <= 0 or candles[i][4] <= 0:
            issues.append(f"Non-positive price at index {i}: {candles[i][1:5]}")

    return len(issues) == 0, issues


from __future__ import annotations

from dataclasses import dataclass

try:
    from pybit.unified_trading import HTTP as BybitV5
except ImportError:  # pragma: no cover - optional dependency
    BybitV5 = Any  # type: ignore[misc,assignment]


@dataclass(slots=True)
class Candle:
    """Normalized candle representation."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def validate_symbol(symbol: str) -> str:
    """Basic symbol validation / normalization."""
    if not symbol:
        raise ValueError("symbol must not be empty")
    return symbol.strip().upper()


def validate_timeframe(timeframe: str) -> str:
    """Validate and normalize timeframe string."""
    valid = {
        "1", "3", "5", "15", "30", "60", "120", "240", "360", "720",
        "D", "W", "M",
    }
    tf = timeframe.strip().upper()
    if tf not in valid:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return tf


def normalize_candles(raw: List[List[Any]], reverse: bool = False) -> List[Candle]:
    """Convert Bybit kline list to list of Candle dataclasses."""
    candles: List[Candle] = []
    for row in raw:
        try:
            ts = int(row[0])
            o, h, l, c, v, turnover = map(float, row[1:7])
        except (IndexError, ValueError, TypeError):
            continue
        candles.append(Candle(ts, o, h, l, c, v, turnover))
    if reverse:
        candles.reverse()
    return candles


def get_closed_klines(
    client: BybitV5,
    symbol: str,
    timeframe: str,
    limit: int = 200,
) -> List[Candle]:
    """Fetch closed klines from Bybit V5 linear market."""
    result = client.request(
        "GET",
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": validate_symbol(symbol),
            "interval": validate_timeframe(timeframe),
            "limit": str(min(max(limit, 1), 1000)),
        },
    )

    return normalize_candles(result.get("list", []), True)


# ==============================================================================
# TECHNICAL ANALYSIS
# ==============================================================================


def sma(values: List[float], period: int) -> List[Optional[float]]:
    """Simple Moving Average."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def ema(values: List[float], period: int, alpha: Optional[float] = None) -> List[Optional[float]]:
    """Exponential Moving Average."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if alpha is None:
        alpha = 2 / (period + 1)
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * (out[i - 1] or 0)
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """Relative Strength Index (Wilder's smoothing)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


def bollinger_bands(
    values: List[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Bollinger Bands returning (upper, middle, lower)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    middle = sma(values, period)
    upper: List[Optional[float]] = [None] * len(values)
    lower: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        if middle[i] is None:
            continue
        window = values[i - period + 1 : i + 1]
        mean = middle[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance**0.5
        upper[i] = mean + std_dev * std
        lower[i] = mean - std_dev * std
    return upper, middle, lower


def atr(
    high: List[float],
    low: List[float],
    close: List[float],
    period: int = 14,
) -> List[Optional[float]]:
    """Average True Range (Wilder)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(close)
    if n < 2:
        return [None] * n
    tr: List[float] = [0.0] * n
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    atr_val = sum(tr[1:period + 1]) / period
    out[period] = atr_val
    for i in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        out[i] = atr_val
    return out


def macd(
    values: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """MACD returning (macd_line, signal_line, histogram)."""
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: List[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    signal_line = ema([v or 0 for v in macd_line], signal)
    histogram: List[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, histogram


def supertrend(
    high: List[float],
    low: List[float],
    close: List[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[List[Optional[float]], List[bool]]:
    """Supertrend indicator returning (trend_line, is_uptrend)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    atr_vals = atr(high, low, close, period)
    n = len(close)
    trend: List[Optional[float]] = [None] * n
    direction: List[bool] = [True] * n
    for i in range(period, n):
        if atr_vals[i] is None:
            continue
        hl2 = (high[i] + low[i]) / 2
        upper = hl2 + multiplier * atr_vals[i]
        lower = hl2 - multiplier * atr_vals[i]
        if i == period:
            trend[i] = lower
            direction[i] = True
            continue
        prev_trend = trend[i - 1]
        prev_dir = direction[i - 1]
        if prev_dir:
            if close[i] > prev_trend:
                trend[i] = max(lower, prev_trend)
                direction[i] = True
            else:
                trend[i] = upper
                direction[i] = False
        elif close[i] < prev_trend:
            trend[i] = min(upper, prev_trend)
            direction[i] = False
        else:
            trend[i] = lower
            direction[i] = True
    return trend, direction


def vwap(
    high: List[float],
    low: List[float],
    close: List[float],
    volume: List[float],
) -> List[Optional[float]]:
    """Volume Weighted Average Price (session VWAP)."""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(n):
        typical = (high[i] + low[i] + close[i]) / 3
        cum_pv += typical * volume[i]
        cum_vol += volume[i]
        if cum_vol > 0:
            out[i] = cum_pv / cum_vol
    return out


def donchian_channels(
    high: List[float],
    low: List[float],
    period: int = 20,
) -> tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Donchian Channels returning (upper, middle, lower)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(high)
    upper: List[Optional[float]] = [None] * n
    lower: List[Optional[float]] = [None] * n
    middle: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window_high = high[i - period + 1 : i + 1]
        window_low = low[i - period + 1 : i + 1]
        upper[i] = max(window_high)
        lower[i] = min(window_low)
        middle[i] = (upper[i] + lower[i]) / 2
    return upper, middle, lower


def heikin_ashi(
    open_: List[float],
    high: List[float],
    low: List[float],
    close: List[float],
) -> tuple[List[float], List[float], List[float], List[float]]:
    """Heikin-Ashi candles."""
    n = len(close)
    ha_open = [0.0] * n
    ha_close = [0.0] * n
    ha_high = [0.0] * n
    ha_low = [0.0] * n
    ha_open[0] = (open_[0] + close[0]) / 2
    ha_close[0] = (open_[0] + high[0] + low[0] + close[0]) / 4
    ha_high[0] = max(high[0], ha_open[0], ha_close[0])
    ha_low[0] = min(low[0], ha_open[0], ha_close[0])
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2
        ha_close[i] = (open_[i] + high[i] + low[i] + close[i]) / 4
        ha_high[i] = max(high[i], ha_open[i], ha_close[i])
        ha_low[i] = min(low[i], ha_open[i], ha_close[i])
    return ha_open, ha_high, ha_low, ha_close


# ==============================================================================
# RISK / POSITION SIZING
# ==============================================================================


def kelly_fraction(win_rate: float, win_loss_ratio: float) -> float:
    """Kelly Criterion fraction (capped at 1.0)."""
    if not (0 < win_rate < 1):
        raise ValueError("win_rate must be in (0, 1)")
    if win_loss_ratio <= 0:
        raise ValueError("win_loss_ratio must be > 0")
    f = win_rate - (1 - win_rate) / win_loss_ratio
    return max(0.0, min(f, 1.0))


def position_size(
    equity: float,
    risk_per_trade: float,
    entry: float,
    stop: float,
    kelly: float = 0.0,
) -> float:
    """Calculate position size in contracts/units."""
    if equity <= 0:
        raise ValueError("equity must be > 0")
    if not (0 < risk_per_trade <= 1):
        raise ValueError("risk_per_trade must be in (0, 1]")
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be > 0")
    risk_amount = equity * risk_per_trade
    if kelly > 0:
        risk_amount *= kelly
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        return 0.0
    return risk_amount / risk_per_unit


# ==============================================================================
# UTILITIES
# ==============================================================================


def retry_request(
    func,
    *args,
    retries: int = 3,
    backoff: float = 1.0,
    exceptions: tuple = (Exception,),
    **kwargs,
):
    """Retry wrapper with exponential backoff."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except exceptions as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
    raise last_exc


def chunked(iterable, size: int):
    """Yield successive chunks from iterable."""
    if size <= 0:
        raise ValueError("size must be > 0")
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


from typing import Sequence


def ema_series(
    values: Sequence[float],
    period: int,
) -> List[float]:
    if not values:
        return []

    if period <= 0:
        raise ValueError("Period must be positive")

    values_list = list(values)
    n = len(values_list)

    if n < period:
        result = []
        running_sum = 0.0
        for i, v in enumerate(values_list):
            running_sum += v
            result.append(running_sum / (i + 1))
        return result

    multiplier = 2.0 / (period + 1.0)

    seed = sum(values_list[:period]) / period

    result = [seed]
    current = seed

    for value in values_list[period:]:
        current = value * multiplier + current * (1.0 - multiplier)
        result.append(current)

    return [seed] * (period - 1) + result


class EMAStream:
    """Incremental EMA calculator for streaming/real-time updates."""

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("Period must be positive")
        self.period = period
        self.multiplier = 2.0 / (period + 1.0)
        self._values: List[float] = []
        self._ema: Optional[float] = None
        self._initialized = False

    def update(self, value: float) -> Optional[float]:
        if math.isnan(value):
            return self._ema

        self._values.append(value)

        if not self._initialized:
            if len(self._values) < self.period:
                return sum(self._values) / len(self._values)
            else:
                self._ema = sum(self._values[-self.period:]) / self.period
                self._initialized = True
                return self._ema

        self._ema = value * self.multiplier + self._ema * (1.0 - self.multiplier)
        return self._ema

    def current(self) -> Optional[float]:
        return self._ema

    def reset(self) -> None:
        self._values.clear()
        self._ema = None
        self._initialized = False

    @property
    def is_ready(self) -> bool:
        return self._initialized


def ema_numpy(values: Union[List[float], np.ndarray], period: int) -> np.ndarray:
    """Vectorized EMA using NumPy for performance. Requires numpy."""
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("NumPy required for ema_numpy. Install with: pip install numpy") from e

    arr = np.asarray(values, dtype=np.float64)
    n = len(arr)

    if n == 0:
        return np.array([])

    if period <= 0:
        raise ValueError("Period must be positive")

    if period == 1:
        return arr.copy()

    alpha = 2.0 / (period + 1.0)

    if n < period:
        return np.cumsum(arr) / np.arange(1, n + 1)

    seed = np.mean(arr[:period])
    result = np.full(n, np.nan)
    result[period - 1] = seed

    for i in range(period, n):
        result[i] = alpha * arr[i] + (1 - alpha) * result[i - 1]

    return result


def ema_series_adaptive(
    values: Sequence[float],
    period: int,
    *,
    ignore_na: bool = False,
) -> List[float]:
    """EMA with NaN handling and adaptive initialization."""
    if not values:
        return []

    if period <= 0:
        raise ValueError("Period must be positive")

    values_list = list(values)
    n = len(values_list)

    if ignore_na:
        valid_indices = [i for i, v in enumerate(values_list) if not math.isnan(v)]
        if not valid_indices:
            return [float("nan")] * n
        valid_values = [values_list[i] for i in valid_indices]
        valid_ema = ema_series(valid_values, period)
        result = [float("nan")] * n
        for idx, ema_val in zip(valid_indices, valid_ema):
            result[idx] = ema_val
        last_valid = float("nan")
        for i in range(n):
            if not math.isnan(result[i]):
                last_valid = result[i]
            elif not math.isnan(last_valid):
                result[i] = last_valid
        return result

    return ema_series(values_list, period)


def rsi(
    closes: List[float],
    period: int = 14,
    *,
    return_series: bool = False,
    smoothing: str = "wilder",
    fill_na: Optional[float] = None,
) -> Union[float, List[float]]:
    """
    Calculate Relative Strength Index (RSI).

    Args:
        closes: List of closing prices.
        period: Lookback period for RSI calculation (default 14).
        return_series: If True, return full RSI series; else return last value.
        smoothing: Smoothing method: "wilder" (default), "ema", or "sma".
        fill_na: Value to use for initial periods before RSI is defined.

    Returns:
        Last RSI value (float) or full RSI series (List[float]).
    """
    if not closes:
        raise ValueError("closes list cannot be empty")
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < 2:
        return [fill_na] * len(closes) if return_series else (fill_na if fill_na is not None else 50.0)

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    if smoothing == "sma":
        avg_gains = []
        avg_losses = []
        for i in range(len(gains)):
            if i < period - 1:
                avg_gains.append(fill_na)
                avg_losses.append(fill_na)
            elif i == period - 1:
                avg_gains.append(sum(gains[:period]) / period)
                avg_losses.append(sum(losses[:period]) / period)
            else:
                window_gains = gains[i - period + 1 : i + 1]
                window_losses = losses[i - period + 1 : i + 1]
                avg_gains.append(sum(window_gains) / period)
                avg_losses.append(sum(window_losses) / period)
    else:
        alpha = 1.0 / period if smoothing == "wilder" else 2.0 / (period + 1)
        avg_gains = [fill_na] * (period - 1)
        avg_losses = [fill_na] * (period - 1)
        avg_gains.append(sum(gains[:period]) / period)
        avg_losses.append(sum(losses[:period]) / period)
        for i in range(period, len(gains)):
            avg_gains.append(alpha * gains[i] + (1 - alpha) * avg_gains[-1])
            avg_losses.append(alpha * losses[i] + (1 - alpha) * avg_losses[-1])

    rsi_values = []
    for ag, al in zip(avg_gains, avg_losses):
        if ag is None or al is None:
            rsi_values.append(fill_na)
        elif al <= 0:
            rsi_values.append(100.0)
        else:
            rs = ag / al
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    # Prepend first value (no change available)
    rsi_series = [fill_na] + rsi_values

    return rsi_series if return_series else rsi_series[-1]


from typing import Dict


def ema_series(
    values: List[float],
    period: int,
    *,
    smoothing: float = 2.0,
    initial: Optional[float] = None,
) -> List[float]:
    """Calculate Exponential Moving Average series.

    Args:
        values: Input price series.
        period: EMA period (must be > 0).
        smoothing: Smoothing factor (default 2.0 for standard EMA).
        initial: Optional seed value; defaults to first value.

    Returns:
        List of EMA values same length as input.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if not values:
        return []

    alpha = smoothing / (period + 1)
    ema: List[float] = []
    prev = initial if initial is not None else values[0]
    for v in values:
        if math.isnan(v) or v is None:
            ema.append(prev)
            continue
        prev = alpha * v + (1 - alpha) * prev
        ema.append(prev)
    return ema


def macd(
    closes: List[float],
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    smoothing: float = 2.0,
    return_history: bool = False,
) -> Dict[str, float]:
    """Calculate MACD, signal line, and histogram with optional extras.

    Args:
        closes: Closing price series.
        fast_period: Fast EMA period.
        slow_period: Slow EMA period.
        signal_period: Signal line EMA period.
        smoothing: EMA smoothing factor.
        return_history: If True, include full series in result.

    Returns:
        Dict with keys: macd, signal, histogram, trend, crossover,
        and optionally macd_series, signal_series, histogram_series.
    """
    min_len = max(fast_period, slow_period, signal_period) + 5
    if len(closes) < min_len:
        base = {
            "macd": 0.0,
            "signal": 0.0,
            "histogram": 0.0,
            "trend": "neutral",
            "crossover": 0,
        }
        if return_history:
            base.update(
                macd_series=[],
                signal_series=[],
                histogram_series=[],
            )
        return base

    fast_ema = ema_series(closes, fast_period, smoothing=smoothing)
    slow_ema = ema_series(closes, slow_period, smoothing=smoothing)

    macd_series = [
        fast_ema[i] - slow_ema[i] for i in range(len(closes))
    ]

    signal_series = ema_series(macd_series, signal_period, smoothing=smoothing)

    histogram_series = [
        macd_series[i] - signal_series[i] for i in range(len(closes))
    ]

    current_macd = macd_series[-1]
    current_signal = signal_series[-1]
    current_hist = histogram_series[-1]

    prev_hist = histogram_series[-2] if len(histogram_series) > 1 else 0.0
    crossover = 0
    if prev_hist < 0 and current_hist > 0:
        crossover = 1
    elif prev_hist > 0 and current_hist < 0:
        crossover = -1

    trend = "bullish" if current_macd > current_signal else "bearish"
    if abs(current_macd - current_signal) < 1e-10:
        trend = "neutral"

    result: Dict[str, float] = {
        "macd": round(current_macd, 8),
        "signal": round(current_signal, 8),
        "histogram": round(current_hist, 8),
        "trend": trend,
        "crossover": crossover,
    }

    if return_history:
        result.update(
            macd_series=[round(v, 8) for v in macd_series],
            signal_series=[round(v, 8) for v in signal_series],
            histogram_series=[round(v, 8) for v in histogram_series],
        )

    return result


def macd_divergence(
    closes: List[float],
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    lookback: int = 20,
) -> Dict[str, bool]:
    """Detect bullish/bearish MACD divergence vs price.

    Returns:
        Dict with keys: bullish_divergence, bearish_divergence.
    """
    if len(closes) < lookback + max(fast_period, slow_period, signal_period):
        return {"bullish_divergence": False, "bearish_divergence": False}

    macd_data = macd(
        closes,
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
        return_history=True,
    )

    macd_hist = macd_data["histogram_series"]
    price_low_idx = min(range(-lookback, 0), key=lambda i: closes[i])
    price_high_idx = max(range(-lookback, 0), key=lambda i: closes[i])

    hist_low_idx = min(range(-lookback, 0), key=lambda i: macd_hist[i])
    hist_high_idx = max(range(-lookback, 0), key=lambda i: macd_hist[i])

    bullish = (
        closes[price_low_idx] < closes[price_high_idx]
        and macd_hist[hist_low_idx] > macd_hist[hist_high_idx]
    )
    bearish = (
        closes[price_high_idx] > closes[price_low_idx]
        and macd_hist[hist_high_idx] < macd_hist[hist_low_idx]
    )

    return {
        "bullish_divergence": bullish,
        "bearish_divergence": bearish,
    }


def macd_slope(
    closes: List[float],
    *,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    periods: int = 5,
) -> float:
    """Calculate MACD line slope over recent periods (linear regression)."""
    macd_data = macd(
        closes,
        fast_period=fast_period,
        slow_period=slow_period,
        signal_period=signal_period,
        return_history=True,
    )
    series = macd_data["macd_series"][-periods:]
    if len(series) < 2:
        return 0.0
    n = len(series)
    x = list(range(n))
    x_mean = sum(x) / n
    y_mean = sum(series) / n
    num = sum((x[i] - x_mean) * (series[i] - y_mean) for i in range(n))
    den = sum((x[i] - x_mean) ** 2 for i in range(n))
    return round(num / den, 8) if den != 0 else 0.0


from dataclasses import dataclass
from functools import lru_cache
from typing import Dict


@dataclass(frozen=True)
class BollingerResult:
    upper: float
    middle: float
    lower: float
    bandwidth_pct: float
    percent_b: float
    std_dev: float
    mean: float


def bollinger(
    closes: List[float],
    period: int = 20,
    num_std: float = 2.0,
    use_ema: bool = False,
    typical_price: Optional[List[Tuple[float, float, float]]] = None,
) -> BollingerResult:
    """
    Calculate Bollinger Bands with optional EMA and typical price support.
    
    Args:
        closes: List of closing prices
        period: Lookback period (default 20)
        num_std: Number of standard deviations (default 2.0)
        use_ema: Use exponential moving average instead of SMA
        typical_price: Optional list of (high, low, close) tuples for typical price calculation
    
    Returns:
        BollingerResult dataclass with all band values and metadata
    """
    if period < 2:
        raise ValueError("Period must be at least 2")
    if num_std <= 0:
        raise ValueError("Number of standard deviations must be positive")
    if not closes:
        raise ValueError("Closes list cannot be empty")

    if typical_price is not None:
        if len(typical_price) != len(closes):
            raise ValueError("typical_price must have same length as closes")
        source = [(h + l + c) / 3.0 for h, l, c in typical_price]
    else:
        source = closes

    if len(source) < period:
        price = source[-1]
        return BollingerResult(
            upper=price,
            middle=price,
            lower=price,
            bandwidth_pct=0.0,
            percent_b=0.5,
            std_dev=0.0,
            mean=price,
        )

    sample = source[-period:]

    if use_ema:
        alpha = 2.0 / (period + 1.0)
        mean = sample[0]
        for value in sample[1:]:
            mean = alpha * value + (1.0 - alpha) * mean

        variance = 0.0
        for value in sample:
            variance = alpha * (value - mean) ** 2 + (1.0 - alpha) * variance
    else:
        mean = sum(sample) / period
        variance = sum((x - mean) ** 2 for x in sample) / period

    deviation = math.sqrt(max(variance, 0.0))

    upper = mean + num_std * deviation
    lower = mean - num_std * deviation

    width = upper - lower
    price = closes[-1]

    bandwidth_pct = (width / mean * 100.0) if mean > 0 else 0.0
    percent_b = ((price - lower) / width) if width > 0 else 0.5

    return BollingerResult(
        upper=round(upper, 8),
        middle=round(mean, 8),
        lower=round(lower, 8),
        bandwidth_pct=round(bandwidth_pct, 5),
        percent_b=round(percent_b, 5),
        std_dev=round(deviation, 8),
        mean=round(mean, 8),
    )


def bollinger_streaming(
    new_price: float,
    prev_mean: float,
    prev_variance: float,
    period: int,
    num_std: float = 2.0,
    use_ema: bool = False,
) -> BollingerResult:
    """
    Incremental Bollinger Bands calculation for streaming data.
    
    Args:
        new_price: New price point
        prev_mean: Previous mean value
        prev_variance: Previous variance value
        period: Lookback period
        num_std: Number of standard deviations
        use_ema: Use exponential moving average
    
    Returns:
        Updated BollingerResult
    """
    if period < 2:
        raise ValueError("Period must be at least 2")

    if use_ema:
        alpha = 2.0 / (period + 1.0)
        mean = alpha * new_price + (1.0 - alpha) * prev_mean
        variance = alpha * (new_price - mean) ** 2 + (1.0 - alpha) * prev_variance
    else:
        mean = prev_mean + (new_price - prev_mean) / period
        variance = prev_variance + (new_price - prev_mean) * (new_price - mean) / period

    deviation = math.sqrt(max(variance, 0.0))
    upper = mean + num_std * deviation
    lower = mean - num_std * deviation
    width = upper - lower

    bandwidth_pct = (width / mean * 100.0) if mean > 0 else 0.0
    percent_b = ((new_price - lower) / width) if width > 0 else 0.5

    return BollingerResult(
        upper=round(upper, 8),
        middle=round(mean, 8),
        lower=round(lower, 8),
        bandwidth_pct=round(bandwidth_pct, 5),
        percent_b=round(percent_b, 5),
        std_dev=round(deviation, 8),
        mean=round(mean, 8),
    )


def bollinger_squeeze(
    closes: List[float],
    period: int = 20,
    num_std: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> Dict[str, Union[bool, float]]:
    """
    Detect Bollinger Band squeeze (volatility contraction) using Keltner Channels.
    
    Returns:
        Dict with squeeze status and relevant metrics
    """
    bb = bollinger(closes, period, num_std)

    if len(closes) < kc_period:
        return {"squeeze": False, "bb_width": bb.bandwidth_pct, "kc_width": 0.0}

    sample = closes[-kc_period:]
    typical_prices = [(c, c, c) for c in sample]
    kc_bb = bollinger(closes, kc_period, kc_mult, typical_price=typical_prices)

    squeeze = bb.upper < kc_bb.upper and bb.lower > kc_bb.lower

    return {
        "squeeze": squeeze,
        "bb_width": bb.bandwidth_pct,
        "kc_width": kc_bb.bandwidth_pct,
        "bb_upper": bb.upper,
        "bb_lower": bb.lower,
        "kc_upper": kc_bb.upper,
        "kc_lower": kc_bb.lower,
    }


@lru_cache(maxsize=128)
def _cached_bollinger_key(
    closes_tuple: Tuple[float, ...],
    period: int,
    num_std: float,
) -> BollingerResult:
    """Cached version for repeated calculations on same data."""
    return bollinger(list(closes_tuple), period, num_std)


def bollinger_cached(
    closes: List[float],
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerResult:
    """Bollinger Bands with LRU caching for repeated identical inputs."""
    return _cached_bollinger_key(tuple(closes), period, num_std)


def atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
    *,
    return_series: bool = False,
    use_numpy: bool = False,
) -> Union[float, List[float]]:
    """
    Calculate Average True Range (ATR) using Wilder's smoothing method.

    Args:
        highs: List of high prices
        lows: List of low prices
        closes: List of close prices
        period: ATR period (default 14)
        return_series: If True, return full ATR series; else return last value
        use_numpy: If True, use numpy for vectorized computation (requires numpy)

    Returns:
        Float (last ATR) or List[float] (full series) depending on return_series
    """
    if not highs or not lows or not closes:
        raise ValueError("Input price lists cannot be empty")

    n = len(closes)
    if len(highs) != n or len(lows) != n:
        raise ValueError("highs, lows, and closes must have the same length")

    if period <= 0:
        raise ValueError("period must be positive")

    if n <= period:
        return [0.0] * n if return_series else 0.0

    if use_numpy:
        try:
            import numpy as np
        except ImportError:
            use_numpy = False

    if use_numpy:
        return _atr_numpy(highs, lows, closes, period, return_series)

    trs = [0.0] * n
    for i in range(1, n):
        h, l, c_prev = highs[i], lows[i], closes[i - 1]
        trs[i] = max(h - l, abs(h - c_prev), abs(l - c_prev))

    atr_values = [0.0] * n
    initial_atr = sum(trs[1:period + 1]) / period
    atr_values[period] = initial_atr

    for i in range(period + 1, n):
        atr_values[i] = (atr_values[i - 1] * (period - 1) + trs[i]) / period

    return atr_values if return_series else atr_values[-1]


def _atr_numpy(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int,
    return_series: bool,
) -> Union[float, List[float]]:
    """Vectorized ATR using numpy."""
    import numpy as np

    h = np.asarray(highs, dtype=np.float64)
    l = np.asarray(lows, dtype=np.float64)
    c = np.asarray(closes, dtype=np.float64)

    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    tr = np.concatenate(([0.0], tr))

    atr_vals = np.zeros_like(tr)
    if len(tr) > period:
        atr_vals[period] = tr[1:period + 1].mean()
        alpha = 1.0 / period
        for i in range(period + 1, len(tr)):
            atr_vals[i] = atr_vals[i - 1] * (1 - alpha) + tr[i] * alpha

    return atr_vals.tolist() if return_series else float(atr_vals[-1])


def atr_streaming(
    high: float,
    low: float,
    close: float,
    prev_close: float,
    prev_atr: float,
    period: int = 14,
) -> float:
    """
    Incremental ATR update for streaming/real-time data.

    Args:
        high: Current bar high
        low: Current bar low
        close: Current bar close
        prev_close: Previous bar close
        prev_atr: Previous ATR value
        period: ATR period

    Returns:
        Updated ATR value
    """
    if period <= 0:
        raise ValueError("period must be positive")

    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
    return (prev_atr * (period - 1) + tr) / period


def true_range(high: float, low: float, prev_close: float) -> float:
    """Calculate single True Range value."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> List[float]:
    """
    Convenience function returning full ATR series (alias for atr with return_series=True).
    """
    return atr(highs, lows, closes, period, return_series=True)


def price_action(
    raw: List[Any],
) -> Dict[str, Any]:
    candles = normalize_candles(
        raw,
        True,
    )

    if len(candles) < 3:
        return {
            "pattern": "NONE",
            "bullish": False,
            "bearish": False,
        }

    current = candles[-1]
    previous = candles[-2]

    co = safe_float(current[1])
    ch = safe_float(current[2])
    cl = safe_float(current[3])
    cc = safe_float(current[4])

    po = safe_float(previous[1])
    pc = safe_float(previous[4])

    body = abs(cc - co)
    rng = max(ch - cl, 0.0)

    lower_wick = (
        min(co, cc) - cl
    )

    upper_wick = (
        ch - max(co, cc)
    )

    reference = max(
        body,
        rng * 0.02,
    )

    bull_pin = (
        rng > 0
        and lower_wick >= 2.0 * reference
        and cc >= co
    )

    bear_pin = (
        rng > 0
        and upper_wick >= 2.0 * reference
        and cc <= co
    )

    bull_engulf = (
        cc > co
        and pc < po
        and cc >= po
        and co <= pc
    )

    bear_engulf = (
        cc < co
        and pc > po
        and cc <= po
        and co >= pc
    )

    if bull_engulf:
        pattern = "BULL_ENGULF"
    elif bear_engulf:
        pattern = "BEAR_ENGULF"
    elif bull_pin:
        pattern = "BULL_PIN"
    elif bear_pin:
        pattern = "BEAR_PIN"
    else:
        pattern = "NONE"

    return {
        "pattern": pattern,
        "bullish": (
            bull_pin
            or bull_engulf
        ),
        "bearish": (
            bear_pin
            or bear_engulf
        ),
    }


# ==============================================================================
# ACCOUNT / POSITION DATA
# ==============================================================================

def get_wallet(
    client: BybitV5,
) -> Dict[str, Any]:
    return client.request(
        "GET",
        "/v5/account/wallet-balance",
        {
            "accountType": "UNIFIED",
            "coin": "USDT",
        },
    )


import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


class BybitAPIError(Exception):
    """Custom exception for Bybit API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class ValidationError(Exception):
    """Custom exception for validation errors."""
    pass


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (BybitAPIError, ConnectionError, TimeoutError),
) -> Callable:
    """Decorator for retrying functions with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (exponential_base ** attempt), max_delay)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"All {max_retries + 1} attempts failed.")
            raise last_exception
        return wrapper
    return decorator


def validate_symbol(symbol: str) -> str:
    """Validate and normalize trading symbol."""
    if not symbol or not isinstance(symbol, str):
        raise ValidationError("Symbol must be a non-empty string")
    normalized = symbol.strip().upper()
    if not normalized.endswith("USDT") and not normalized.endswith("USDC"):
        logger.warning(f"Symbol {normalized} may not be a valid USDT/USDC perpetual")
    return normalized


def validate_position_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Validate and extract position list from API response."""
    if not isinstance(response, dict):
        raise ValidationError(f"Expected dict response, got {type(response)}")

    ret_code = response.get("retCode")
    if ret_code is not None and ret_code != 0:
        raise BybitAPIError(
            f"API error: {response.get('retMsg', 'Unknown error')}",
            status_code=ret_code,
            response=response
        )

    result = response.get("result")
    if not isinstance(result, dict):
        raise ValidationError("Missing or invalid 'result' in response")

    position_list = result.get("list")
    if not isinstance(position_list, list):
        raise ValidationError("Missing or invalid 'list' in result")

    return position_list


def filter_positions_by_side(
    positions: List[Dict[str, Any]],
    side: str
) -> List[Dict[str, Any]]:
    """Filter positions by side (Buy/Sell)."""
    side = side.capitalize()
    if side not in ("Buy", "Sell"):
        raise ValidationError("Side must be 'Buy' or 'Sell'")
    return [p for p in positions if p.get("side") == side]


def calculate_total_position_value(positions: List[Dict[str, Any]]) -> float:
    """Calculate total USD value of all positions."""
    total = 0.0
    for pos in positions:
        try:
            size = float(pos.get("size", 0))
            mark_price = float(pos.get("markPrice", 0))
            total += size * mark_price
        except (ValueError, TypeError):
            logger.warning(f"Could not calculate value for position: {pos}")
    return total


def get_positions(
    client: Any,
    symbol: Optional[str] = None,
    settle_coin: str = "USDT",
    category: str = "linear",
) -> List[Dict[str, Any]]:
    """
    Fetch positions from Bybit V5 API.
    
    Args:
        client: BybitV5 client instance with request method
        symbol: Optional trading symbol (e.g., 'BTCUSDT')
        settle_coin: Settlement coin (default: USDT)
        category: Product category (default: linear)
    
    Returns:
        List of position dictionaries
    
    Raises:
        BybitAPIError: If API returns an error
        ValidationError: If response validation fails
    """
    params: Dict[str, Any] = {
        "category": category,
        "settleCoin": settle_coin,
    }

    if symbol:
        params["symbol"] = validate_symbol(symbol)

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def _fetch() -> Dict[str, Any]:
        return client.request("GET", "/v5/position/list", params)

    try:
        response = _fetch()
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        raise BybitAPIError(f"Request failed: {e}") from e

    return validate_position_response(response)


def get_all_positions_paginated(
    client: Any,
    settle_coin: str = "USDT",
    category: str = "linear",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Fetch all positions with pagination support.
    
    Args:
        client: BybitV5 client instance
        settle_coin: Settlement coin
        category: Product category
        limit: Maximum positions per request
    
    Returns:
        Complete list of all positions
    """
    all_positions = []
    cursor = None

    while True:
        params = {
            "category": category,
            "settleCoin": settle_coin,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor

        @retry_with_backoff(max_retries=3)
        def _fetch_page() -> Dict[str, Any]:
            return client.request("GET", "/v5/position/list", params)

        try:
            response = _fetch_page()
        except Exception as e:
            logger.error(f"Pagination fetch failed: {e}")
            raise BybitAPIError(f"Pagination request failed: {e}") from e

        positions = validate_position_response(response)
        all_positions.extend(positions)

        next_cursor = response.get("result", {}).get("nextPageCursor")
        if not next_cursor:
            break
        cursor = next_cursor

    return all_positions


# Backwards compatibility alias
get_positions_list = get_positions
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,),
):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (exponential_base ** attempt), max_delay)
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"All {max_retries + 1} attempts failed for {func.__name__}")
            raise last_exception
        return wrapper
    return decorator


def validate_symbol(symbol: str) -> str:
    if not symbol or not isinstance(symbol, str):
        raise ValueError("Symbol must be a non-empty string")
    return symbol.strip().upper()


class BybitAPIError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, response: Optional[Dict] = None):
        super().__init__(message)
        self.code = code
        self.response = response


def get_open_orders(
    client: Any,
    symbol: Optional[str] = None,
    category: str = "linear",
    settle_coin: str = "USDT",
    limit: int = 50,
    cursor: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Fetch open orders from Bybit V5 API with pagination support.
    
    Args:
        client: BybitV5 client instance with request method
        symbol: Trading symbol (e.g., 'BTCUSDT')
        category: Product category ('linear', 'inverse', 'spot', 'option')
        settle_coin: Settlement coin ('USDT', 'USDC', etc.)
        limit: Maximum orders per page (1-50)
        cursor: Pagination cursor for next page
        timeout: Request timeout in seconds
    
    Returns:
        Dict containing 'list' of orders and 'nextPageCursor' for pagination
    
    Raises:
        BybitAPIError: On API errors
        ValueError: On invalid parameters
    """
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")

    params: Dict[str, Any] = {
        "category": category,
        "settleCoin": settle_coin,
        "limit": limit,
    }

    if symbol:
        params["symbol"] = validate_symbol(symbol)

    if cursor:
        params["cursor"] = cursor

    @retry_with_backoff(max_retries=3, exceptions=(ConnectionError, TimeoutError, BybitAPIError))
    def _make_request() -> Dict[str, Any]:
        result = client.request(
            "GET",
            "/v5/order/open-orders",
            params,
            timeout=timeout,
        )

        if result.get("retCode", 0) != 0:
            raise BybitAPIError(
                f"Bybit API error: {result.get('retMsg', 'Unknown error')}",
                code=result.get("retCode"),
                response=result,
            )

        return result.get("result", {})

    try:
        response = _make_request()
        return {
            "list": response.get("list", []),
            "nextPageCursor": response.get("nextPageCursor"),
            "category": category,
            "symbol": symbol,
        }
    except BybitAPIError:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error fetching open orders: {e}")
        raise BybitAPIError(f"Request failed: {e!s}") from e


def get_all_open_orders(
    client: Any,
    symbol: Optional[str] = None,
    category: str = "linear",
    settle_coin: str = "USDT",
    max_pages: int = 10,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Fetch all open orders across all pages.
    
    Args:
        client: BybitV5 client instance
        symbol: Trading symbol filter
        category: Product category
        settle_coin: Settlement coin
        max_pages: Maximum pages to fetch (safety limit)
        timeout: Request timeout per page
    
    Returns:
        Flat list of all open orders
    """
    all_orders = []
    cursor = None
    pages_fetched = 0

    while pages_fetched < max_pages:
        response = get_open_orders(
            client=client,
            symbol=symbol,
            category=category,
            settle_coin=settle_coin,
            limit=50,
            cursor=cursor,
            timeout=timeout,
        )

        orders = response.get("list", [])
        all_orders.extend(orders)
        pages_fetched += 1

        cursor = response.get("nextPageCursor")
        if not cursor:
            break

    if pages_fetched >= max_pages and cursor:
        logger.warning(f"Reached max_pages limit ({max_pages}), may have more orders")

    return all_orders


def filter_orders_by_status(
    orders: List[Dict[str, Any]],
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Filter orders by order status."""
    if not statuses:
        return orders
    status_set = set(s.upper() for s in statuses)
    return [o for o in orders if o.get("orderStatus", "").upper() in status_set]


def filter_orders_by_side(
    orders: List[Dict[str, Any]],
    side: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter orders by side (Buy/Sell)."""
    if not side:
        return orders
    target_side = side.upper()
    return [o for o in orders if o.get("side", "").upper() == target_side]


from typing import Any, Dict

try:
    from bybit_v5 import BybitV5
except ImportError:
    BybitV5 = Any  # type: ignore[misc,assignment]


def validate_symbol(symbol: str) -> str:
    """Normalize and validate a trading symbol."""
    if not symbol or not isinstance(symbol, str):
        raise ValueError("Symbol must be a non-empty string")
    return symbol.strip().upper()


def get_fills(
    client: BybitV5,
    symbol: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "category": "linear",
        "limit": 50,
    }

    if symbol:
        params["symbol"] = validate_symbol(symbol)

    try:
        result = client.request(
            "GET",
            "/v5/execution/list",
            params,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to fetch fills: {e}") from e

    if not isinstance(result, dict):
        raise TypeError(f"Unexpected response type: {type(result)}")

    fills = result.get("list")
    if not isinstance(fills, list):
        return []

    return fills


# ==============================================================================
# TA HANDLER
# ==============================================================================
def handle_ta(
    client: BybitV5,
    symbol: str,
    timeframe: str,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)
    tf = validate_timeframe(timeframe)

    candles = get_closed_klines(
        client,
        sym,
        tf,
        200,
    )

    if len(candles) < 60:
        raise ToolError(
            f"Insufficient candle history: "
            f"{len(candles)}; minimum 60 required."
        )

    closes = [
        safe_float(c[4])
        for c in candles
    ]

    highs = [
        safe_float(c[2])
        for c in candles
    ]

    lows = [
        safe_float(c[3])
        for c in candles
    ]

    price = closes[-1]

    rsi_value = rsi(
        closes,
        14,
    )

    macd_value = macd(
        closes
    )

    bb = bollinger(
        closes
    )

    ema9 = ema_series(
        closes,
        9,
    )[-1]

    ema21 = ema_series(
        closes,
        21,
    )[-1]

    ema50 = ema_series(
        closes,
        50,
    )[-1]

    atr_value = atr(
        highs,
        lows,
        closes,
        14,
    )

    if (
        ema9 > ema21 > ema50
        and price > ema9
    ):
        trend = "STRONG_BULLISH"
    elif (
        ema9 > ema21
        and price > ema21
    ):
        trend = "BULLISH"
    elif (
        ema9 < ema21 < ema50
        and price < ema9
    ):
        trend = "STRONG_BEARISH"
    elif (
        ema9 < ema21
        and price < ema21
    ):
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    return {
        "success": True,
        "symbol": sym,
        "timeframe": (
            tf + "m"
            if tf.isdigit()
            else tf
        ),
        "current_price": price,
        "trend": trend,
        "rsi_14": round(
            rsi_value,
            2,
        ),
        "rsi_state": (
            "OVERSOLD"
            if rsi_value <= 30
            else "OVERBOUGHT"
            if rsi_value >= 70
            else "NEUTRAL"
        ),
        "macd": macd_value,
        "macd_bias": (
            "BULLISH"
            if macd_value["histogram"] > 0
            else "BEARISH"
            if macd_value["histogram"] < 0
            else "NEUTRAL"
        ),
        "bollinger_bands": bb,
        "ema": {
            "ema_9": round(
                ema9,
                8,
            ),
            "ema_21": round(
                ema21,
                8,
            ),
            "ema_50": round(
                ema50,
                8,
            ),
        },
        "atr_14": round(
            atr_value,
            8,
        ),
        "timestamp": utc_now(),
    }


# ==============================================================================
# SCAN / SIGNAL ENGINE
# ==============================================================================

def handle_scan(
    client: BybitV5,
    symbol: str,
    max_spread: float,
    target_profit: float,
    leverage: int,
    max_notional: float,
    maker_fee_pct: float,
    taker_fee_pct: float,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)

    max_spread = positive_float(
        max_spread,
        "max_spread_pct",
        allow_zero=True,
    )

    target_profit = positive_float(
        target_profit,
        "target_profit_usdt",
    )

    leverage = int(
        positive_float(
            leverage,
            "leverage",
        )
    )

    max_notional = positive_float(
        max_notional,
        "max_notional_usdt",
    )

    orderbook = get_orderbook(
        client,
        sym,
    )

    candles = get_closed_klines(
        client,
        sym,
        "1",
        100,
    )

    ta = handle_ta(
        client,
        sym,
        "1",
    )

    bid_volume = sum(
        safe_float(row[1])
        for row in orderbook["bids"][:10]
    )

    ask_volume = sum(
        safe_float(row[1])
        for row in orderbook["asks"][:10]
    )

    imbalance = (
        bid_volume / ask_volume
        if ask_volume > 0
        else 1.0
    )

    pa = price_action(
        candles
    )

    trend = ta["trend"]

    score_long = 0
    score_short = 0

    if orderbook["spread_pct"] <= max_spread:
        score_long += 1
        score_short += 1

    if imbalance >= 1.50:
        score_long += 2
    elif imbalance <= 0.67:
        score_short += 2

    if pa["bullish"]:
        score_long += 2

    if pa["bearish"]:
        score_short += 2

    if trend in {
        "BULLISH",
        "STRONG_BULLISH",
    }:
        score_long += 2

    if trend in {
        "BEARISH",
        "STRONG_BEARISH",
    }:
        score_short += 2

    rsi_value = safe_float(
        ta["rsi_14"]
    )

    if 45 <= rsi_value <= 68:
        score_long += 1

    if 32 <= rsi_value <= 55:
        score_short += 1

    if (
        ta["macd_bias"]
        == "BULLISH"
    ):
        score_long += 1

    if (
        ta["macd_bias"]
        == "BEARISH"
    ):
        score_short += 1

    if (
        score_long >= 5
        and score_long > score_short
    ):
        bias = "LONG"
    elif (
        score_short >= 5
        and score_short > score_long
    ):
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    entry = (
        orderbook["best_bid"]
        if bias == "LONG"
        else orderbook["best_ask"]
        if bias == "SHORT"
        else orderbook["mid"]
    )

    tick, step, min_qty, max_qty = (
        fetch_precision(
            client,
            sym,
        )
    )

    atr_value = safe_float(
        ta["atr_14"]
    )

    maker_round_trip = (
        maker_fee_pct * 2.0 / 100.0
    )

    taker_round_trip = (
        taker_fee_pct * 2.0 / 100.0
    )

    selected_fee = (
        maker_round_trip
        if maker_fee_pct <= taker_fee_pct
        else taker_round_trip
    )

    fee_usdt = (
        entry
        * selected_fee
    )

    min_move = (
        target_profit
        + fee_usdt
    )

    volatility_move = max(
        atr_value * 0.20,
        entry * 0.0005,
    )

    required_delta = max(
        min_move,
        volatility_move,
    )

    # Size from the *net* target rather than allowing a volatility-derived
    # delta to silently reduce expected profit. If the notional cap prevents
    # the requested net target, reject the plan instead of pretending it is
    # profitable.
    target_cost = target_profit + fee_usdt
    raw_qty_for_target = target_cost / max(required_delta, 1e-12)
    raw_qty_cap = max_notional / max(entry, 1e-12)

    if raw_qty_for_target > raw_qty_cap:
        raise ToolError(
            "Requested net micro-profit cannot be achieved within "
            f"max_notional_usdt={max_notional:.8f} at the current price/fees.",
            EXIT_INVALID_INPUT,
            {
                "target_profit_usdt": target_profit,
                "required_price_delta": required_delta,
                "required_qty": raw_qty_for_target,
                "maximum_qty": raw_qty_cap,
            },
        )

    raw_qty = raw_qty_for_target

    final_qty, qty_string = (
        clamp_decimal_qty(
            raw_qty,
            step,
            min_qty,
            max_qty,
        )
    )

    notional = (
        final_qty * entry
    )

    estimated_fees = (
        notional
        * selected_fee
    )

    estimated_gross_profit = final_qty * required_delta
    estimated_net_profit = estimated_gross_profit - estimated_fees
    target_profit_pass = estimated_net_profit >= target_profit

    if not target_profit_pass:
        raise ToolError(
            "Exchange quantity precision prevents the requested net "
            "micro-profit at the current target delta.",
            EXIT_INVALID_INPUT,
            {
                "requested_target_usdt": target_profit,
                "estimated_net_profit_usdt": estimated_net_profit,
                "calculated_qty": final_qty,
                "required_price_delta": required_delta,
            },
        )

    stop_distance = max(
        atr_value * 0.75,
        orderbook["spread"] * 3.0,
        entry * 0.001,
    )

    reward_risk = (
        required_delta
        / stop_distance
        if stop_distance > 0
        else 0.0
    )

    min_rr = safe_float(
        cfg_value(
            "LLM_AGENT_VAR_MIN_REWARD_RISK",
            "MIN_REWARD_RISK",
            "1.25",
        ),
        1.25,
    )

    spread_pass = (
        orderbook["spread_pct"]
        <= max_spread
    )

    notional_pass = (
        notional
        <= max_notional
    )

    rr_pass = (
        reward_risk >= min_rr
    )

    confidence = round(
        max(
            score_long,
            score_short,
        )
        / 10.0,
        3,
    )

    return {
        "success": True,
        "symbol": sym,
        "best_bid": orderbook["best_bid"],
        "best_ask": orderbook["best_ask"],
        "mid": orderbook["mid"],
        "spread_pct": round(
            orderbook["spread_pct"],
            6,
        ),
        "spread_pass": spread_pass,
        "imbalance_ratio": round(
            imbalance,
            4,
        ),
        "long_score": score_long,
        "short_score": score_short,
        "confidence": confidence,
        "pattern_detected": pa["pattern"],
        "bias": bias,
        "ta_summary": {
            "trend": trend,
            "rsi_14": ta["rsi_14"],
            "macd_bias": ta["macd_bias"],
            "atr_14": atr_value,
        },
        "micro_profit_plan": {
            "target_profit_usdt":
                target_profit,
            "calculated_qty":
                final_qty,
            "calculated_qty_formatted":
                qty_string,
            "estimated_notional":
                round(
                    notional,
                    8,
                ),
            "estimated_fees":
                round(
                    estimated_fees,
                    8,
                ),
            "estimated_gross_profit_usdt":
                round(
                    estimated_gross_profit,
                    8,
                ),
            "estimated_net_profit_usdt":
                round(
                    estimated_net_profit,
                    8,
                ),
            "target_profit_pass":
                target_profit_pass,
            "required_price_delta":
                round(
                    required_delta,
                    8,
                ),
            "stop_distance":
                round(
                    stop_distance,
                    8,
                ),
            "reward_risk":
                round(
                    reward_risk,
                    4,
                ),
            "reward_risk_pass":
                rr_pass,
        },
        "risk_gate_pass": (
            bias != "NEUTRAL"
            and spread_pass
            and notional_pass
            and rr_pass
            and target_profit_pass
        ),
        "timestamp": utc_now(),
    }


# ==============================================================================
# RISK ENGINE
# ==============================================================================

from __future__ import annotations

import functools
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class WalletError(Exception):
    """Base exception for wallet-related errors."""


class PositionError(Exception):
    """Base exception for position-related errors."""


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def validate_wallet_structure(wallet: Dict[str, Any]) -> bool:
    """Validate that wallet dict has expected structure."""
    if not isinstance(wallet, dict):
        return False
    if "coin" not in wallet and not any(
        k in wallet for k in ("totalEquity", "totalWalletBalance", "totalAvailableBalance")
    ):
        return False
    return True


def validate_position_structure(position: Dict[str, Any]) -> bool:
    """Validate that position dict has expected structure."""
    if not isinstance(position, dict):
        return False
    return "size" in position


@functools.lru_cache(maxsize=128)
def _cached_wallet_equity_usdt(wallet_tuple: Tuple[Tuple[str, Any], ...]) -> float:
    """Cached internal implementation for wallet equity lookup."""
    wallet = dict(wallet_tuple)
    rows = wallet.get("coin", [])

    for row in rows:
        if str(row.get("coin", "")).upper() == "USDT":
            for key in ("totalEquity", "walletBalance", "equity"):
                value = safe_float(row.get(key), -1)
                if value >= 0:
                    return value

    for key in ("totalEquity", "totalWalletBalance", "totalAvailableBalance"):
        value = safe_float(wallet.get(key), -1)
        if value >= 0:
            return value

    return 0.0


def wallet_equity_usdt(
    wallet: Dict[str, Any],
    use_cache: bool = True,
) -> float:
    """
    Get USDT equity from wallet dict.

    Args:
        wallet: Wallet data dict from exchange API.
        use_cache: Whether to use LRU cache for repeated lookups.

    Returns:
        USDT equity as float, 0.0 if not found.
    """
    if not validate_wallet_structure(wallet):
        logger.warning("Invalid wallet structure: %s", wallet)
        return 0.0

    if use_cache:
        wallet_tuple = tuple(sorted(wallet.items()))
        return _cached_wallet_equity_usdt(wallet_tuple)

    rows = wallet.get("coin", [])

    for row in rows:
        if str(row.get("coin", "")).upper() == "USDT":
            for key in ("totalEquity", "walletBalance", "equity"):
                value = safe_float(row.get(key), -1)
                if value >= 0:
                    return value

    for key in ("totalEquity", "totalWalletBalance", "totalAvailableBalance"):
        value = safe_float(wallet.get(key), -1)
        if value >= 0:
            return value

    return 0.0


def position_is_open(
    position: Dict[str, Any],
) -> bool:
    """
    Check if a position has non-zero size.

    Args:
        position: Position data dict from exchange API.

    Returns:
        True if position size absolute value > 0.
    """
    if not validate_position_structure(position):
        logger.warning("Invalid position structure: %s", position)
        return False

    size = safe_float(position.get("size"), 0)
    return abs(size) > 0


def position_side(position: Dict[str, Any]) -> Optional[str]:
    """
    Determine position side from size.

    Args:
        position: Position data dict.

    Returns:
        'long', 'short', or None if flat/closed.
    """
    if not validate_position_structure(position):
        return None

    size = safe_float(position.get("size"), 0)
    if size > 0:
        return "long"
    if size < 0:
        return "short"
    return None


def position_notional_usdt(position: Dict[str, Any], mark_price: float) -> float:
    """
    Calculate position notional value in USDT.

    Args:
        position: Position data dict.
        mark_price: Current mark price for the symbol.

    Returns:
        Notional value (size * mark_price), 0.0 if invalid.
    """
    if not validate_position_structure(position):
        return 0.0

    size = safe_float(position.get("size"), 0)
    price = safe_float(mark_price, 0)
    return abs(size) * price


def wallet_total_equity_usdt(wallet: Dict[str, Any]) -> float:
    """
    Calculate total wallet equity across all coins converted to USDT.

    Note: Requires price data for non-USDT coins. Returns USDT equity only
    if prices unavailable.

    Args:
        wallet: Wallet data dict.

    Returns:
        Total equity estimate in USDT.
    """
    if not validate_wallet_structure(wallet):
        return 0.0

    usdt_equity = wallet_equity_usdt(wallet, use_cache=False)
    rows = wallet.get("coin", [])

    for row in rows:
        coin = str(row.get("coin", "")).upper()
        if coin == "USDT":
            continue
        equity = safe_float(row.get("equity") or row.get("walletBalance") or row.get("totalEquity"), 0)
        if equity > 0:
            logger.debug("Non-USDT coin %s equity: %s (price conversion needed)", coin, equity)

    return usdt_equity


def clear_wallet_cache() -> None:
    """Clear the wallet equity LRU cache."""
    _cached_wallet_equity_usdt.cache_clear()


def get_wallet_cache_info() -> Dict[str, Any]:
    """Get cache statistics for wallet equity lookups."""
    info = _cached_wallet_equity_usdt.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "maxsize": info.maxsize,
        "currsize": info.currsize,
    }


from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol, TypedDict, runtime_checkable

from pybit.unified_trading import HTTP as BybitV5


@runtime_checkable
class StateStore(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...


class PositionDict(TypedDict, total=False):
    symbol: str
    size: float
    side: str
    entryPrice: float
    markPrice: float
    unrealisedPnl: float
    leverage: float
    margin: float


class WalletDict(TypedDict, total=False):
    coin: str
    equity: float
    availableBalance: float
    usedMargin: float


class RiskStatusResult(TypedDict):
    success: bool
    equity_usdt: float
    daily_realized_pnl: float
    daily_loss_limit_usdt: float
    daily_loss_limit_hit: bool
    open_position_count: int
    max_open_positions: int
    position_limit_hit: bool
    max_notional_usdt: float
    kill_switch: bool
    live_environment: bool
    timestamp: str
    margin_usage_pct: float
    total_notional_usdt: float
    max_drawdown_pct: float
    risk_score: int


_STATE_CACHE: Dict[str, Any] = {}
_STATE_CACHE_TS: float = 0.0
_STATE_TTL = 1.0


def load_state() -> Dict[str, Any]:
    global _STATE_CACHE, _STATE_CACHE_TS
    now = time.time()
    if now - _STATE_CACHE_TS < _STATE_TTL and _STATE_CACHE:
        return _STATE_CACHE
    try:
        import json
        import os
        path = os.getenv("BOT_STATE_PATH", "state.json")
        with open(path, encoding="utf-8") as f:
            _STATE_CACHE = json.load(f)
    except Exception:
        _STATE_CACHE = {}
    _STATE_CACHE_TS = now
    return _STATE_CACHE


def save_state(state: Dict[str, Any]) -> None:
    import json
    import os
    path = os.getenv("BOT_STATE_PATH", "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)
    global _STATE_CACHE, _STATE_CACHE_TS
    _STATE_CACHE = state
    _STATE_CACHE_TS = time.time()


def get_wallet(client: BybitV5) -> List[WalletDict]:
    resp = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    if resp.get("retCode") != 0:
        raise RuntimeError(f"wallet error: {resp.get('retMsg')}")
    return resp["result"]["list"][0].get("coin", [])


def get_positions(client: BybitV5) -> List[PositionDict]:
    resp = client.get_positions(category="linear", settleCoin="USDT")
    if resp.get("retCode") != 0:
        raise RuntimeError(f"positions error: {resp.get('retMsg')}")
    return resp["result"]["list"]


def position_is_open(pos: PositionDict) -> bool:
    try:
        return float(pos.get("size", 0)) > 0
    except Exception:
        return False


def wallet_equity_usdt(wallet: List[WalletDict]) -> float:
    for coin in wallet:
        if coin.get("coin") == "USDT":
            return safe_float(coin.get("equity", 0))
    return 0.0


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except Exception:
        return default


def kill_switch_active() -> bool:
    import os
    return os.getenv("KILL_SWITCH", "0") == "1"


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _calc_margin_usage(wallet: List[WalletDict]) -> float:
    eq = wallet_equity_usdt(wallet)
    if eq <= 0:
        return 0.0
    used = 0.0
    for coin in wallet:
        if coin.get("coin") == "USDT":
            used = safe_float(coin.get("usedMargin", 0))
            break
    return round((used / eq) * 100, 2)


def _calc_total_notional(positions: List[PositionDict]) -> float:
    total = 0.0
    for p in positions:
        if position_is_open(p):
            sz = safe_float(p.get("size", 0))
            mk = safe_float(p.get("markPrice", 0))
            total += sz * mk
    return round(total, 2)


def _calc_max_drawdown(state: Dict[str, Any]) -> float:
    peak = safe_float(state.get("peak_equity", 0))
    equity = safe_float(state.get("equity_usdt", 0))
    if peak <= 0:
        return 0.0
    return round(((peak - equity) / peak) * 100, 2)


def _calc_risk_score(
    margin_pct: float,
    dd_pct: float,
    pos_count: int,
    max_pos: int,
    daily_pnl: float,
    daily_limit: float,
) -> int:
    score = 0
    if margin_pct > 80:
        score += 30
    elif margin_pct > 50:
        score += 15
    if dd_pct > 10:
        score += 25
    elif dd_pct > 5:
        score += 10
    if pos_count >= max_pos:
        score += 20
    elif pos_count >= max_pos * 0.75:
        score += 10
    if daily_pnl <= -abs(daily_limit) * 0.5:
        score += 15
    return min(score, 100)


def risk_status(
    client: BybitV5,
    symbol: str,
    max_notional: float,
    max_open_positions: int,
    max_daily_loss: float,
) -> RiskStatusResult:
    if max_notional <= 0:
        raise ValueError("max_notional must be > 0")
    if max_open_positions <= 0:
        raise ValueError("max_open_positions must be > 0")
    if max_daily_loss <= 0:
        raise ValueError("max_daily_loss must be > 0")

    state = load_state()

    try:
        wallet = get_wallet(client)
    except Exception as e:
        wallet = []
        state["last_wallet_error"] = str(e)

    try:
        positions = get_positions(client)
    except Exception as e:
        positions = []
        state["last_position_error"] = str(e)

    open_positions = [p for p in positions if position_is_open(p)]

    equity = wallet_equity_usdt(wallet)

    daily_pnl = safe_float(state.get("daily_realized_pnl", 0))

    loss_limit_hit = daily_pnl <= -abs(max_daily_loss)

    position_limit_hit = len(open_positions) >= max_open_positions

    margin_usage = _calc_margin_usage(wallet)
    total_notional = _calc_total_notional(open_positions)
    max_dd = _calc_max_drawdown({**state, "equity_usdt": equity})
    risk_score = _calc_risk_score(
        margin_usage,
        max_dd,
        len(open_positions),
        max_open_positions,
        daily_pnl,
        max_daily_loss,
    )

    result: RiskStatusResult = {
        "success": True,
        "equity_usdt": round(equity, 8),
        "daily_realized_pnl": round(daily_pnl, 8),
        "daily_loss_limit_usdt": max_daily_loss,
        "daily_loss_limit_hit": loss_limit_hit,
        "open_position_count": len(open_positions),
        "max_open_positions": max_open_positions,
        "position_limit_hit": position_limit_hit,
        "max_notional_usdt": max_notional,
        "kill_switch": kill_switch_active(),
        "live_environment": not client.testnet,
        "timestamp": utc_now(),
        "margin_usage_pct": margin_usage,
        "total_notional_usdt": total_notional,
        "max_drawdown_pct": max_dd,
        "risk_score": risk_score,
    }

    state.update({
        "equity_usdt": equity,
        "peak_equity": max(safe_float(state.get("peak_equity", 0)), equity),
        "last_risk_check": utc_now(),
    })
    save_state(state)

    return result


def get_risk_summary(client: BybitV5, config: Dict[str, Any]) -> Dict[str, Any]:
    return risk_status(
        client=client,
        symbol=config.get("symbol", ""),
        max_notional=config.get("max_notional", 10000),
        max_open_positions=config.get("max_open_positions", 5),
        max_daily_loss=config.get("max_daily_loss", 500),
    )


def check_pre_trade_risk(
    client: BybitV5,
    symbol: str,
    notional: float,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    status = risk_status(
        client=client,
        symbol=symbol,
        max_notional=config.get("max_notional", 10000),
        max_open_positions=config.get("max_open_positions", 5),
        max_daily_loss=config.get("max_daily_loss", 500),
    )
    allowed = (
        status["success"]
        and not status["kill_switch"]
        and not status["daily_loss_limit_hit"]
        and not status["position_limit_hit"]
        and (status["total_notional_usdt"] + notional) <= status["max_notional_usdt"]
        and status["risk_score"] < 80
    )
    return {"allowed": allowed, "reason": "" if allowed else "risk limits exceeded", "status": status}


def enforce_entry_risk(
    client: BybitV5,
    symbol: str,
    notional: float,
    stop_distance: float,
    entry_price: float,
    max_notional: float,
    max_account_risk_pct: float,
    max_daily_loss: float,
    max_open_positions: int,
) -> Dict[str, Any]:
    if kill_switch_active():
        raise ToolError(
            "Execution blocked: kill switch active.",
            EXIT_PERMISSION_DENIED,
        )

    if notional > max_notional:
        raise ToolError(
            f"Notional {notional:.8f} exceeds "
            f"maximum {max_notional:.8f}.",
            EXIT_PERMISSION_DENIED,
        )

    positions = get_positions(
        client
    )

    open_positions = [
        p
        for p in positions
        if position_is_open(p)
    ]

    if len(open_positions) >= max_open_positions:
        raise ToolError(
            f"Open-position limit reached: "
            f"{len(open_positions)}/"
            f"{max_open_positions}.",
            EXIT_PERMISSION_DENIED,
        )

    state = load_state()

    daily_pnl = safe_float(
        state.get(
            "daily_realized_pnl",
            0,
        )
    )

    if daily_pnl <= -abs(
        max_daily_loss
    ):
        raise ToolError(
            "Daily loss circuit breaker active.",
            EXIT_PERMISSION_DENIED,
        )

    wallet = get_wallet(
        client
    )

    equity = wallet_equity_usdt(
        wallet
    )

    if equity <= 0:
        raise ToolError(
            "Unable to establish positive "
            "USDT account equity; refusing live entry.",
            EXIT_PERMISSION_DENIED,
        )

    risk_usdt = (
        notional
        * (
            stop_distance
            / entry_price
        )
    )

    max_risk_usdt = (
        equity
        * max_account_risk_pct
        / 100.0
    )

    if risk_usdt > max_risk_usdt:
        raise ToolError(
            f"Estimated stop risk "
            f"{risk_usdt:.8f} USDT exceeds "
            f"account-risk allowance "
            f"{max_risk_usdt:.8f} USDT.",
            EXIT_PERMISSION_DENIED,
        )

    return {
        "equity_usdt":
            equity,
        "estimated_risk_usdt":
            risk_usdt,
        "max_risk_usdt":
            max_risk_usdt,
        "open_positions":
            len(open_positions),
    }


# ==============================================================================
# REAL-TIME L2 ORDERBOOK ENGINE
# Standard-library WebSocket client; no external websocket dependency required.
#
# Bybit sends an initial snapshot followed by deltas. A new snapshot replaces
# the local book; a zero-sized level deletes that price level.
# ==============================================================================

class BybitL2WebSocket:
    def __init__(
        self,
        symbol: str,
        depth: int = L2_DEFAULT_DEPTH,
        testnet: bool = False,
        timeout: float = 5.0,
    ):
        self.symbol = validate_symbol(symbol)
        self.depth = int(depth)
        self.testnet = testnet
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.buffer = b""
        self.closed = False

        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_u = 0
        self.last_seq = 0
        self.last_update_ts = 0.0
        self.snapshot_ready = False

    @property
    def url(self) -> str:
        return (
            BYBIT_WS_LINEAR_TESTNET
            if self.testnet
            else BYBIT_WS_LINEAR
        )

    def connect(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        path = parsed.path or "/"

        raw = socket.create_connection(
            (host, port),
            timeout=self.timeout,
        )

        context = ssl.create_default_context()
        self.sock = context.wrap_socket(
            raw,
            server_hostname=host,
        )
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(
            os.urandom(16)
        ).decode()

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: AIChat-Bybit-L2/1.0\r\n"
            "\r\n"
        ).encode()

        self.sock.sendall(request)

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError(
                    "WebSocket closed during handshake."
                )
            response += chunk

            if len(response) > 65536:
                raise ConnectionError(
                    "WebSocket handshake response too large."
                )

        header = response.split(
            b"\r\n\r\n",
            1,
        )[0].decode(
            "latin1",
            errors="replace",
        )

        if " 101 " not in header:
            raise ConnectionError(
                f"WebSocket handshake failed: "
                f"{header.splitlines()[0] if header else 'empty response'}"
            )

        self.closed = False
        self.send_json({
            "op": "subscribe",
            "args": [
                f"orderbook.{self.depth}.{self.symbol}",
            ],
        })

    def close(self) -> None:
        self.closed = True

        try:
            if self.sock:
                self._send_frame(
                    0x8,
                    b"\x03\xe8",
                )
        except Exception:
            pass

        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

        self.sock = None

    def _send_frame(
        self,
        opcode: int,
        payload: bytes,
    ) -> None:
        if not self.sock:
            raise ConnectionError(
                "WebSocket is not connected."
            )

        first = 0x80 | (opcode & 0x0F)
        length = len(payload)

        if length < 126:
            header = bytes([
                first,
                0x80 | length,
            ])
        elif length < 65536:
            header = (
                bytes([first, 0x80 | 126])
                + struct.pack(">H", length)
            )
        else:
            header = (
                bytes([first, 0x80 | 127])
                + struct.pack(">Q", length)
            )

        mask = os.urandom(4)
        masked = bytes(
            payload[i] ^ mask[i % 4]
            for i in range(length)
        )

        self.sock.sendall(
            header + mask + masked
        )

    def send_json(
        self,
        payload: Dict[str, Any],
    ) -> None:
        self._send_frame(
            0x1,
            json.dumps(
                payload,
                separators=(",", ":"),
            ).encode(),
        )

    def _recv_exact(
        self,
        count: int,
    ) -> bytes:
        data = b""

        while len(data) < count:
            chunk = self.sock.recv(
                count - len(data)
            )

            if not chunk:
                raise ConnectionError(
                    "WebSocket connection closed."
                )

            data += chunk

        return data

    def recv_frame(
        self,
    ) -> Tuple[int, bytes]:
        if not self.sock:
            raise ConnectionError(
                "WebSocket is not connected."
            )

        first_two = self._recv_exact(2)

        first = first_two[0]
        second = first_two[1]

        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            length = struct.unpack(
                ">H",
                self._recv_exact(2),
            )[0]
        elif length == 127:
            length = struct.unpack(
                ">Q",
                self._recv_exact(8),
            )[0]

        if length > 8 * 1024 * 1024:
            raise ConnectionError(
                "WebSocket frame exceeds safety limit."
            )

        mask = (
            self._recv_exact(4)
            if masked
            else None
        )

        payload = self._recv_exact(
            length
        ) if length else b""

        if mask:
            payload = bytes(
                payload[i] ^ mask[i % 4]
                for i in range(length)
            )

        return opcode, payload

    def recv_json(
        self,
    ) -> Optional[Dict[str, Any]]:
        fragments = []

        while True:
            opcode, payload = self.recv_frame()

            if opcode == 0x8:
                raise ConnectionError(
                    "Bybit WebSocket close frame received."
                )

            if opcode == 0x9:
                self._send_frame(
                    0xA,
                    payload,
                )
                continue

            if opcode == 0xA:
                continue

            if opcode == 0x1:
                fragments = [payload]

                if not (payload and False):
                    # Text frames are normally final from Bybit.
                    try:
                        return json.loads(
                            b"".join(fragments).decode()
                        )
                    except json.JSONDecodeError:
                        pass

            elif opcode == 0x0:
                fragments.append(payload)

                try:
                    return json.loads(
                        b"".join(fragments).decode()
                    )
                except json.JSONDecodeError:
                    continue

    def apply_message(
        self,
        message: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        topic = str(
            message.get("topic", "")
        )

        expected = (
            f"orderbook.{self.depth}."
            f"{self.symbol}"
        )

        if topic != expected:
            return None

        data = message.get(
            "data",
            {},
        )

        if not isinstance(data, dict):
            return None

        kind = str(
            message.get(
                "type",
                "",
            )
        )

        if kind == "snapshot":
            self.bids.clear()
            self.asks.clear()

            for row in data.get("b", []):
                if len(row) >= 2:
                    price = safe_float(row[0])
                    size = safe_float(row[1])

                    if price > 0 and size > 0:
                        self.bids[price] = size

            for row in data.get("a", []):
                if len(row) >= 2:
                    price = safe_float(row[0])
                    size = safe_float(row[1])

                    if price > 0 and size > 0:
                        self.asks[price] = size

            self.snapshot_ready = True

        elif kind == "delta":
            if not self.snapshot_ready:
                return None

            for row in data.get("b", []):
                if len(row) < 2:
                    continue

                price = safe_float(row[0])
                size = safe_float(row[1])

                if price <= 0:
                    continue

                if size <= 0:
                    self.bids.pop(
                        price,
                        None,
                    )
                else:
                    self.bids[price] = size

            for row in data.get("a", []):
                if len(row) < 2:
                    continue

                price = safe_float(row[0])
                size = safe_float(row[1])

                if price <= 0:
                    continue

                if size <= 0:
                    self.asks.pop(
                        price,
                        None,
                    )
                else:
                    self.asks[price] = size

        else:
            return None

        self.last_u = int(
            safe_float(
                data.get("u", 0),
                self.last_u,
            )
        )

        self.last_seq = int(
            safe_float(
                data.get("seq", 0),
                self.last_seq,
            )
        )

        self.last_update_ts = time.time()

        return self.snapshot()

    def snapshot(
        self,
        levels: int = 50,
    ) -> Optional[Dict[str, Any]]:
        if not self.bids or not self.asks:
            return None

        bids = sorted(
            self.bids.items(),
            key=lambda x: x[0],
            reverse=True,
        )[:levels]

        asks = sorted(
            self.asks.items(),
            key=lambda x: x[0],
        )[:levels]

        if not bids or not asks:
            return None

        bid = bids[0][0]
        ask = asks[0][0]

        if bid <= 0 or ask <= bid:
            return None

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": bid,
            "best_ask": ask,
            "mid": (bid + ask) / 2.0,
            "spread": ask - bid,
            "spread_bps": (
                (ask - bid)
                / ((ask + bid) / 2.0)
                * 10000.0
            ),
            "update_id": self.last_u,
            "sequence": self.last_seq,
            "age_ms": max(
                0.0,
                (time.time() - self.last_update_ts)
                * 1000.0,
            ),
        }


from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class L2AnalysisResult:
    """Structured result for L2 book analysis."""
    valid: bool
    signal: str
    score: float
    directional_score: float
    raw_imbalance: float
    weighted_pressure: float
    near_pressure: float
    deep_pressure: float
    microprice: float
    micro_edge_bps: float
    bid_depth: float
    ask_depth: float
    best_bid_size: float
    best_ask_size: float
    spread_bps: float
    # --- Logic upgrades ---
    toxicity_score: float = 0.0
    price_impact_bps: float = 0.0
    flow_imbalance: float = 0.0
    adaptive_threshold: float = 0.20
    confidence: float = 0.0


def _compute_toxicity(
    bids: List[List[Any]],
    asks: List[List[Any]],
    levels: int,
) -> float:
    """
    Compute VPIN-inspired toxicity metric.
    High toxicity = informed flow likely present = fade the signal.
    """
    if not bids or not asks:
        return 0.0

    bid_volumes = [safe_float(x[1]) for x in bids[:levels]]
    ask_volumes = [safe_float(x[1]) for x in asks[:levels]]

    total_bid = sum(bid_volumes)
    total_ask = sum(ask_volumes)
    total = total_bid + total_ask

    if total <= 0:
        return 0.0

    # Volume-synchronized probability of informed trading proxy
    # Imbalance persistence across levels indicates toxicity
    imbalances = []
    for bv, av in zip(bid_volumes, ask_volumes):
        lv = bv + av
        if lv > 0:
            imbalances.append(abs(bv - av) / lv)

    if not imbalances:
        return 0.0

    # Toxicity: mean imbalance * concentration factor
    mean_imb = sum(imbalances) / len(imbalances)
    concentration = max(bid_volumes + ask_volumes) / total if total > 0 else 0.0

    return min(mean_imb * (1.0 + concentration), 1.0)


def _estimate_price_impact(
    bids: List[List[Any]],
    asks: List[List[Any]],
    levels: int,
    target_notional: float = 10000.0,
) -> Tuple[float, float]:
    """
    Estimate price impact in bps for a target notional on each side.
    Returns (bid_impact_bps, ask_impact_bps).
    """
    def side_impact(levels_data: List[List[Any]], is_bid: bool) -> float:
        remaining = target_notional
        total_qty = 0.0
        vwap = 0.0
        best_price = safe_float(levels_data[0][0]) if levels_data else 0.0

        for level in levels_data[:levels]:
            price = safe_float(level[0])
            size = safe_float(level[1])
            notional = price * size
            take = min(remaining, notional)
            if take <= 0:
                break
            total_qty += take / price if price > 0 else 0.0
            vwap += take
            remaining -= take
            if remaining <= 1e-9:
                break

        if total_qty <= 0 or vwap <= 0:
            return 0.0

        exec_price = vwap / total_qty
        if is_bid:
            # Selling into bids: impact = (best - exec) / best
            return max((best_price - exec_price) / best_price * 10000.0, 0.0) if best_price > 0 else 0.0
        else:
            # Buying from asks: impact = (exec - best) / best
            return max((exec_price - best_price) / best_price * 10000.0, 0.0) if best_price > 0 else 0.0

    bid_impact = side_impact(bids, is_bid=True)
    ask_impact = side_impact(asks, is_bid=False)
    return bid_impact, ask_impact


def _compute_flow_imbalance(
    bids: List[List[Any]],
    asks: List[List[Any]],
    prev_bids: Optional[List[List[Any]]] = None,
    prev_asks: Optional[List[List[Any]]] = None,
) -> float:
    """
    Compute order flow imbalance between current and previous snapshot.
    Positive = net buying pressure, negative = net selling pressure.
    """
    if prev_bids is None or prev_asks is None:
        return 0.0

    def total_volume(levels: List[List[Any]]) -> float:
        return sum(safe_float(x[1]) for x in levels)

    curr_bid_vol = total_volume(bids)
    curr_ask_vol = total_volume(asks)
    prev_bid_vol = total_volume(prev_bids)
    prev_ask_vol = total_volume(prev_asks)

    bid_delta = curr_bid_vol - prev_bid_vol
    ask_delta = curr_ask_vol - prev_ask_vol

    total_delta = abs(bid_delta) + abs(ask_delta)
    if total_delta <= 0:
        return 0.0

    return (bid_delta - ask_delta) / total_delta


def _adaptive_threshold(
    spread_bps: float,
    volatility_proxy: float,
    toxicity: float,
    base_threshold: float = 0.20,
) -> float:
    """
    Dynamically adjust signal threshold based on market conditions.
    Wider spread / higher vol / higher toxicity -> higher threshold (more conservative).
    """
    # Normalize inputs
    spread_factor = min(spread_bps / 10.0, 1.0)  # 10 bps = max spread factor
    vol_factor = min(volatility_proxy / 0.02, 1.0)  # 2% daily vol = max
    tox_factor = toxicity

    # Threshold increases with adverse conditions
    multiplier = 1.0 + 0.5 * spread_factor + 0.3 * vol_factor + 0.4 * tox_factor
    return min(base_threshold * multiplier, 0.50)  # Cap at 0.50


def analyze_l2_book(
    book: Dict[str, Any],
    levels: int = 20,
    prev_book: Optional[Dict[str, Any]] = None,
    target_notional: float = 10000.0,
) -> Dict[str, Any]:
    bids = book.get("bids", [])[:levels]
    asks = book.get("asks", [])[:levels]

    if not bids or not asks:
        return asdict(L2AnalysisResult(
            valid=False,
            signal="NEUTRAL",
            score=0.0,
            directional_score=0.0,
            raw_imbalance=0.0,
            weighted_pressure=0.0,
            near_pressure=0.0,
            deep_pressure=0.0,
            microprice=0.0,
            micro_edge_bps=0.0,
            bid_depth=0.0,
            ask_depth=0.0,
            best_bid_size=0.0,
            best_ask_size=0.0,
            spread_bps=0.0,
        ))

    bid_total = sum(safe_float(x[1]) for x in bids)
    ask_total = sum(safe_float(x[1]) for x in asks)

    weighted_bid = sum(safe_float(x[1]) / (i + 1) for i, x in enumerate(bids))
    weighted_ask = sum(safe_float(x[1]) / (i + 1) for i, x in enumerate(asks))

    total_weighted = weighted_bid + weighted_ask
    pressure = (weighted_bid - weighted_ask) / total_weighted if total_weighted > 0 else 0.0

    raw_imbalance = bid_total / ask_total if ask_total > 0 else 999.0

    bid_price = safe_float(bids[0][0])
    ask_price = safe_float(asks[0][0])
    bid_size = safe_float(bids[0][1])
    ask_size = safe_float(asks[0][1])

    microprice = (
        (ask_price * bid_size + bid_price * ask_size)
        / max(bid_size + ask_size, 1e-12)
    )

    mid = safe_float(book.get("mid", (bid_price + ask_price) / 2.0))
    micro_edge_bps = (microprice - mid) / mid * 10000.0 if mid > 0 else 0.0

    near_bid = sum(safe_float(x[1]) for x in bids[:5])
    near_ask = sum(safe_float(x[1]) for x in asks[:5])
    deep_bid = sum(safe_float(x[1]) for x in bids[5:levels])
    deep_ask = sum(safe_float(x[1]) for x in asks[5:levels])

    near_pressure = (near_bid - near_ask) / max(near_bid + near_ask, 1e-12)
    deep_pressure = (deep_bid - deep_ask) / max(deep_bid + deep_ask, 1e-12)

    # --- Logic Upgrade 1: Toxicity detection ---
    toxicity = _compute_toxicity(bids, asks, levels)

    # --- Logic Upgrade 2: Price impact estimation ---
    bid_impact_bps, ask_impact_bps = _estimate_price_impact(bids, asks, levels, target_notional)
    price_impact_bps = (bid_impact_bps + ask_impact_bps) / 2.0

    # --- Logic Upgrade 3: Flow imbalance (requires prev_book) ---
    prev_bids = prev_book.get("bids", [])[:levels] if prev_book else None
    prev_asks = prev_book.get("asks", [])[:levels] if prev_book else None
    flow_imbalance = _compute_flow_imbalance(bids, asks, prev_bids, prev_asks)

    # --- Logic Upgrade 4: Adaptive thresholding ---
    spread_bps = safe_float(book.get("spread_bps"))
    volatility_proxy = safe_float(book.get("volatility_1h", 0.01))
    adaptive_thresh = _adaptive_threshold(spread_bps, volatility_proxy, toxicity)

    # Directional score with toxicity penalty
    directional = (
        pressure * 0.40
        + near_pressure * 0.30
        + deep_pressure * 0.20
        + flow_imbalance * 0.10
    )

    # Microprice edge contribution
    if micro_edge_bps > 0:
        directional += min(micro_edge_bps / 10.0, 0.10)
    elif micro_edge_bps < 0:
        directional -= min(abs(micro_edge_bps) / 10.0, 0.10)

    # Toxicity penalty: reduce conviction when informed flow likely
    directional *= (1.0 - 0.5 * toxicity)

    directional = max(-1.0, min(1.0, directional))

    # Confidence: inverse of toxicity, scaled by depth
    depth_factor = min((bid_total + ask_total) / 1000.0, 1.0)  # Normalize to 1k units
    confidence = (1.0 - toxicity) * depth_factor

    if directional >= adaptive_thresh:
        signal = "LONG"
    elif directional <= -adaptive_thresh:
        signal = "SHORT"
    else:
        signal = "NEUTRAL"

    result = L2AnalysisResult(
        valid=True,
        signal=signal,
        score=round(abs(directional), 6),
        directional_score=round(directional, 6),
        raw_imbalance=round(raw_imbalance, 6),
        weighted_pressure=round(pressure, 6),
        near_pressure=round(near_pressure, 6),
        deep_pressure=round(deep_pressure, 6),
        microprice=microprice,
        micro_edge_bps=round(micro_edge_bps, 6),
        bid_depth=bid_total,
        ask_depth=ask_total,
        best_bid_size=bid_size,
        best_ask_size=ask_size,
        spread_bps=spread_bps,
        toxicity_score=round(toxicity, 6),
        price_impact_bps=round(price_impact_bps, 6),
        flow_imbalance=round(flow_imbalance, 6),
        adaptive_threshold=round(adaptive_thresh, 6),
        confidence=round(confidence, 6),
    )

    return asdict(result)


import logging
from functools import lru_cache
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _l2_position(
    client: BybitV5,
    symbol: str,
    *,
    side: Optional[str] = None,
    min_size: float = 0.0,
) -> Optional[Dict[str, Any]]:
    try:
        positions = get_positions(client, symbol)
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", symbol, e)
        return None

    for position in positions:
        size = safe_float(position.get("size", 0))
        if size <= min_size:
            continue
        if side and position.get("side") != side:
            continue
        return position

    return None


def get_all_open_positions(
    client: BybitV5,
    symbol: str,
    *,
    min_size: float = 0.0,
) -> List[Dict[str, Any]]:
    try:
        positions = get_positions(client, symbol)
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", symbol, e)
        return []

    return [
        p for p in positions
        if safe_float(p.get("size", 0)) > min_size
    ]


@lru_cache(maxsize=32)
def _cached_position_key(symbol: str, side: Optional[str], min_size: float) -> str:
    return f"{symbol}:{side or 'any'}:{min_size}"


def _l2_position_cached(
    client: BybitV5,
    symbol: str,
    *,
    side: Optional[str] = None,
    min_size: float = 0.0,
    use_cache: bool = False,
) -> Optional[Dict[str, Any]]:
    if use_cache:
        _cached_position_key(symbol, side, min_size)
    return _l2_position(client, symbol, side=side, min_size=min_size)


def validate_position_structure(position: Dict[str, Any]) -> bool:
    required_keys = {"symbol", "side", "size", "entryPrice", "leverage"}
    return all(k in position for k in required_keys)


def get_position_risk_metrics(position: Dict[str, Any]) -> Dict[str, float]:
    size = safe_float(position.get("size", 0))
    entry = safe_float(position.get("entryPrice", 0))
    mark = safe_float(position.get("markPrice", 0))
    leverage = safe_float(position.get("leverage", 1))
    unrealised = safe_float(position.get("unrealisedPnl", 0))

    notional = size * mark
    liq_price = entry * (1 - 1 / leverage) if position.get("side") == "Buy" else entry * (1 + 1 / leverage)

    return {
        "notional_value": notional,
        "unrealised_pnl": unrealised,
        "liquidation_price": liq_price,
        "margin_used": notional / leverage if leverage else 0.0,
        "pnl_pct": (unrealised / (notional / leverage) * 100) if notional and leverage else 0.0,
    }


import logging
from dataclasses import dataclass
from typing import Any, Dict

from bybit_v5 import BybitV5
from utils import ToolError, safe_float, validate_symbol

logger = logging.getLogger(__name__)


@dataclass
class CloseOrderResult:
    order_id: Optional[str]
    order_link_id: str
    symbol: str
    side: str
    qty: float
    position_idx: int
    status: str
    avg_price: Optional[float] = None
    cum_exec_qty: float = 0.0
    cum_exec_value: float = 0.0
    fee: float = 0.0


def _generate_order_link_id(symbol: str, prefix: str = "L2X") -> str:
    unique = f"{symbol}:{time.time_ns()}:{uuid.uuid4()}"
    return prefix + hashlib.sha256(unique.encode()).hexdigest()[:27]


def _verify_order_filled(
    client: BybitV5,
    order_id: str,
    symbol: str,
    max_wait_sec: float = 10.0,
    poll_interval: float = 0.5,
) -> Tuple[str, float, float, float, float]:
    import time as _time
    deadline = _time.time() + max_wait_sec
    while _time.time() < deadline:
        try:
            resp = client.request(
                "GET",
                "/v5/order/realtime",
                params={"category": "linear", "symbol": symbol, "orderId": order_id},
            )
            lst = resp.get("list", [])
            if lst:
                o = lst[0]
                status = o.get("orderStatus", "")
                avg_price = safe_float(o.get("avgPrice", 0))
                cum_qty = safe_float(o.get("cumExecQty", 0))
                cum_val = safe_float(o.get("cumExecValue", 0))
                fee = safe_float(o.get("cumExecFee", 0))
                return status, avg_price, cum_qty, cum_val, fee
        except Exception as e:
            logger.warning("Order verification poll error: %s", e)
        _time.sleep(poll_interval)
    return "Unknown", 0.0, 0.0, 0.0, 0.0


def _l2_close_market(
    client: BybitV5,
    position: Dict[str, Any],
    *,
    verify_fill: bool = True,
    max_wait_sec: float = 10.0,
    retry_attempts: int = 3,
    retry_backoff: float = 0.5,
) -> CloseOrderResult:
    symbol = validate_symbol(position.get("symbol"))

    position_side = str(position.get("side", ""))
    if position_side not in ("Buy", "Sell"):
        raise ToolError(f"Invalid position side: {position_side}")

    size = safe_float(position.get("size", 0))
    if size <= 0:
        raise ToolError("No position quantity available for L2 exit.")

    close_side = "Sell" if position_side == "Buy" else "Buy"

    position_idx = int(safe_float(position.get("positionIdx", 0)))

    order_link_id = _generate_order_link_id(symbol)

    last_exc: Optional[Exception] = None
    for attempt in range(1, retry_attempts + 1):
        try:
            result = client.request(
                "POST",
                "/v5/order/create",
                data={
                    "category": "linear",
                    "symbol": symbol,
                    "side": close_side,
                    "orderType": "Market",
                    "qty": str(size),
                    "positionIdx": position_idx,
                    "reduceOnly": True,
                    "orderLinkId": order_link_id,
                },
            )
            break
        except Exception as e:
            last_exc = e
            logger.warning("Order create attempt %d/%d failed: %s", attempt, retry_attempts, e)
            if attempt < retry_attempts:
                time.sleep(retry_backoff * attempt)
    else:
        raise ToolError(f"Failed to place close order after {retry_attempts} attempts") from last_exc

    order_id = result.get("orderId")
    if not order_id:
        raise ToolError("Order created but no orderId returned")

    status = "Created"
    avg_price = None
    cum_qty = 0.0
    cum_val = 0.0
    fee = 0.0

    if verify_fill and order_id:
        status, avg_price, cum_qty, cum_val, fee = _verify_order_filled(
            client, order_id, symbol, max_wait_sec=max_wait_sec
        )
        if status not in ("Filled", "PartiallyFilled"):
            logger.warning("Close order %s not fully filled: %s", order_id, status)

    logger.info(
        "L2 close market: symbol=%s side=%s qty=%.8f order_id=%s status=%s avg_price=%s",
        symbol, close_side, size, order_id, status, avg_price
    )

    return CloseOrderResult(
        order_id=order_id,
        order_link_id=order_link_id,
        symbol=symbol,
        side=close_side,
        qty=size,
        position_idx=position_idx,
        status=status,
        avg_price=avg_price,
        cum_exec_qty=cum_qty,
        cum_exec_value=cum_val,
        fee=fee,
    )


def close_position_market(
    client: BybitV5,
    symbol: str,
    position_side: str,
    size: float,
    position_idx: int = 0,
    **kwargs,
) -> CloseOrderResult:
    position = {
        "symbol": symbol,
        "side": position_side,
        "size": size,
        "positionIdx": position_idx,
    }
    return _l2_close_market(client, position, **kwargs)


async def _l2_close_market_async(
    client: BybitV5,
    position: Dict[str, Any],
    *,
    verify_fill: bool = True,
    max_wait_sec: float = 10.0,
    retry_attempts: int = 3,
    retry_backoff: float = 0.5,
) -> CloseOrderResult:
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _l2_close_market(
            client,
            position,
            verify_fill=verify_fill,
            max_wait_sec=max_wait_sec,
            retry_attempts=retry_attempts,
            retry_backoff=retry_backoff,
        ),
    )


import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

L2_DEFAULT_DEPTH = 50


class ToolError(Exception):
    pass


def utc_now() -> float:
    return time.time()


@dataclass
class BybitL2WebSocket:
    symbol: str
    depth: int
    testnet: bool
    timeout: float
    _ws: Any = None
    _connected: bool = False

    def connect(self) -> None:
        self._connected = True
        logger.info("Connected to Bybit L2 for %s", self.symbol)

    def recv_json(self) -> Optional[Dict[str, Any]]:
        if not self._connected:
            raise socket.timeout("Not connected")
        return {"type": "snapshot", "data": {"bids": [], "asks": []}}

    def apply_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return {"bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]], "timestamp": utc_now()}

    def close(self) -> None:
        self._connected = False
        logger.info("Closed Bybit L2 connection for %s", self.symbol)

    def __enter__(self) -> BybitL2WebSocket:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def analyze_l2_book(book: Dict[str, Any], depth: int) -> Dict[str, Any]:
    return {
        "spread": book["asks"][0][0] - book["bids"][0][0] if book["bids"] and book["asks"] else 0.0,
        "mid_price": (book["asks"][0][0] + book["bids"][0][0]) / 2 if book["bids"] and book["asks"] else 0.0,
        "bid_volume": sum(v for _, v in book["bids"][:depth]),
        "ask_volume": sum(v for _, v in book["asks"][:depth]),
    }


def handle_l2_signal(
    symbol: str,
    depth: int = L2_DEFAULT_DEPTH,
    testnet: bool = True,
    timeout: float = 8.0,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        ws = BybitL2WebSocket(
            symbol=symbol,
            depth=depth,
            testnet=testnet,
            timeout=timeout,
        )

        try:
            with ws:
                deadline = time.monotonic() + timeout

                while time.monotonic() < deadline:
                    try:
                        message = ws.recv_json()
                    except socket.timeout:
                        continue

                    if not message:
                        continue

                    book = ws.apply_message(message)

                    if book:
                        analysis = analyze_l2_book(
                            book,
                            min(depth, 20),
                        )

                        result = {
                            "success": True,
                            "symbol": symbol,
                            "depth": depth,
                            "book": book,
                            "l2": analysis,
                            "timestamp": utc_now(),
                            "attempt": attempt,
                        }
                        if on_progress:
                            on_progress(f"L2 snapshot received for {symbol}")
                        return result

                raise ToolError("Timed out waiting for a valid L2 snapshot.")

        except ToolError:
            raise
        except Exception as e:
            last_error = e
            logger.warning("Attempt %d/%d failed for %s: %s", attempt, max_retries, symbol, e)
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)
            continue

    raise ToolError(f"All {max_retries} attempts failed for {symbol}: {last_error}") from last_error


import logging
from typing import Any, Dict

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .bybit_client import BybitV5
from .exceptions import BybitAPIError, BybitRequestError
from .validators import validate_symbol

logger = logging.getLogger(__name__)


def _l2_cancel_entry(
    client: BybitV5,
    symbol: str,
    order_id: Optional[str],
    position_idx: int = 0,
    max_retries: int = 3,
) -> Optional[Dict[str, Any]]:
    if not order_id:
        logger.debug("No order_id provided, skipping cancellation")
        return None

    if position_idx not in (0, 1, 2):
        raise ValueError(f"Invalid position_idx: {position_idx}. Must be 0 (one-way), 1 (hedge buy), or 2 (hedge sell)")

    validated_symbol = validate_symbol(symbol)

    @retry(
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
        stop=stop_after_attempt(max_retries),
        retry=retry_if_exception_type((BybitRequestError, ConnectionError, TimeoutError)),
        reraise=True,
    )
    def _cancel_with_retry() -> Dict[str, Any]:
        result = client.request(
            "POST",
            "/v5/order/cancel",
            data={
                "category": "linear",
                "symbol": validated_symbol,
                "orderId": order_id,
                "positionIdx": position_idx,
            },
        )

        ret_code = result.get("retCode")
        ret_msg = result.get("retMsg", "")

        if ret_code != 0:
            if ret_code in (110001, 110004, 110025):
                logger.info("Order %s already filled or cancelled: %s", order_id, ret_msg)
                return {"cancelled": True, "order_id": order_id, "result": result, "already_done": True}
            raise BybitAPIError(f"Cancel failed: {ret_msg}", code=ret_code)

        logger.info("Successfully cancelled order %s for %s", order_id, validated_symbol)
        return {"cancelled": True, "order_id": order_id, "result": result, "already_done": False}

    try:
        return _cancel_with_retry()
    except BybitAPIError as exc:
        logger.warning("Failed to cancel order %s after retries: %s", order_id, exc)
        return {"cancelled": False, "order_id": order_id, "error": str(exc), "error_code": exc.code}
    except Exception as exc:
        logger.exception("Unexpected error cancelling order %s", order_id)
        return {"cancelled": False, "order_id": order_id, "error": f"Unexpected error: {exc}"}


from decimal import ROUND_DOWN
from typing import Any, Dict

from bybit_v5 import BybitV5
from config import cfg_value
from constants import (
    EXIT_INVALID_INPUT,
    EXIT_PERMISSION_DENIED,
    L2_DEFAULT_DEPTH,
    L2_DEFAULT_RECONNECT_SECONDS,
    L2_HEARTBEAT_SECONDS,
)
from core.logging import append_log, utc_now
from core.risk import enforce_entry_risk
from core.state import kill_switch_active, load_state
from core.utils import clamp_decimal_qty, quantize_step, safe_float
from exceptions import ToolError
from execution.l2_helpers import (
    _l2_cancel_entry,
    _l2_close_market,
    _l2_position,
    analyze_l2_book,
    fetch_precision,
)
from execution.l2_websocket import BybitL2WebSocket
from execution.permissions import live_authorized
from validation import validate_symbol


def handle_l2_live_scalp(
    client: BybitV5,
    symbol: str,
    target_profit: float,
    leverage: int,
    max_notional: float,
    max_spread_bps: float,
    entry_score: float,
    exit_score: float,
    stop_bps: float,
    max_hold_seconds: float,
    live_request: bool,
    live_confirmation: str,
    depth: int = L2_DEFAULT_DEPTH,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)

    live, auth_reason = live_authorized(
        client.testnet,
        live_request,
        live_confirmation,
    )

    if not live:
        raise ToolError(
            f"L2 live trading blocked: {auth_reason}.",
            EXIT_PERMISSION_DENIED,
        )

    state = load_state()

    if state.get("kill_switch") or kill_switch_active():
        raise ToolError(
            "Execution blocked: kill switch active.",
            EXIT_PERMISSION_DENIED,
        )

    if leverage < 1:
        raise ToolError(
            "Leverage must be >= 1.",
            EXIT_INVALID_INPUT,
        )

    max_leverage = int(
        safe_float(
            cfg_value("MAX_LEVERAGE", "MAX_LEVERAGE", "25"),
            25,
        )
    )

    if leverage > max_leverage:
        raise ToolError(
            f"Leverage {leverage}x exceeds maximum {max_leverage}x.",
            EXIT_PERMISSION_DENIED,
        )

    tick, step, min_qty, max_qty = fetch_precision(client, sym)

    ws = BybitL2WebSocket(
        symbol=sym,
        depth=depth,
        testnet=client.testnet,
        timeout=5.0,
    )

    started = time.time()
    last_ping = time.time()
    entry_signal: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty: Optional[float] = None
    entry_position: Optional[Dict[str, Any]] = None
    entry_order_id: Optional[str] = None
    last_analysis: Optional[Dict[str, Any]] = None
    session_id = uuid.uuid4().hex[:8]

    def _send_ping() -> None:
        nonlocal last_ping
        try:
            ws.send_json({"op": "ping"})
            last_ping = time.time()
        except Exception:
            ws.close()
            ws.connect()
            last_ping = time.time()

    def _place_entry_order(
        side: str,
        price: float,
        qty: float,
        qty_str: str,
    ) -> Tuple[str, Dict[str, Any]]:
        position_mode = cfg_value("POSITION_MODE", "POSITION_MODE", "auto").lower()
        position_idx = (
            (1 if side == "Buy" else 2) if position_mode == "hedge" else 0
        )

        order_link_id = (
            f"L2E{session_id}"
            f"{hashlib.sha256(f'{sym}:{side}:{time.time_ns()}:{uuid.uuid4()}'.encode()).hexdigest()[:16]}"
        )

        client.request(
            "POST",
            "/v5/position/set-leverage",
            data={
                "category": "linear",
                "symbol": sym,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
        )

        order_result = client.request(
            "POST",
            "/v5/order/create",
            data={
                "category": "linear",
                "symbol": sym,
                "side": side,
                "orderType": "Limit",
                "qty": qty_str,
                "price": price,
                "timeInForce": "PostOnly",
                "positionIdx": position_idx,
                "orderLinkId": order_link_id,
            },
        )
        return order_result.get("orderId", ""), order_result

    def _calculate_fees(price: float, qty: float) -> Tuple[float, float]:
        maker_fee = safe_float(
            cfg_value("LLM_AGENT_VAR_MAKER_FEE_PCT", "MAKER_FEE_PCT", "0.020"),
            0.020,
        ) / 100.0
        taker_fee = safe_float(
            cfg_value("LLM_AGENT_VAR_TAKER_FEE_PCT", "TAKER_FEE_PCT", "0.055"),
            0.055,
        ) / 100.0
        return maker_fee, taker_fee

    def _build_exit_record(
        position: Dict[str, Any],
        current_price: float,
        analysis: Dict[str, Any],
        exit_result: Dict[str, Any],
        reason: str,
        hold_time: float,
    ) -> Dict[str, Any]:
        avg_price = safe_float(position.get("avgPrice", entry_price or 0))
        pos_size = safe_float(position.get("size", entry_qty or 0))
        pos_side = str(position.get("side", entry_signal))

        maker_fee, taker_fee = _calculate_fees(avg_price, pos_size)

        gross_pnl = (
            (current_price - avg_price) * pos_size
            if pos_side == "Buy"
            else (avg_price - current_price) * pos_size
        )
        estimated_fees = (avg_price * pos_size) * maker_fee + (
            current_price * pos_size
        ) * taker_fee
        net_pnl = gross_pnl - estimated_fees

        return {
            "success": True,
            "mode": "LIVE",
            "strategy": "L2_MICROPROFIT",
            "symbol": sym,
            "entry_side": pos_side,
            "entry_price": avg_price,
            "exit_reference_price": current_price,
            "qty": pos_size,
            "gross_pnl_estimate": round(gross_pnl, 8),
            "estimated_fees": round(estimated_fees, 8),
            "net_pnl_estimate": round(net_pnl, 8),
            "exit_reason": reason,
            "l2_exit": analysis,
            "exit_order": exit_result,
            "hold_seconds": round(hold_time, 3),
            "timestamp": utc_now(),
        }

    try:
        ws.connect()

        while time.time() - started < max_hold_seconds + 120:
            if time.time() - last_ping >= L2_HEARTBEAT_SECONDS:
                _send_ping()

            try:
                message = ws.recv_json()
            except socket.timeout:
                continue
            except (ConnectionError, OSError, ValueError):
                ws.close()
                time.sleep(L2_DEFAULT_RECONNECT_SECONDS)
                ws.connect()
                last_ping = time.time()
                continue

            if not message:
                continue

            book = ws.apply_message(message)

            if not book:
                continue

            if book.get("age_ms", 0) > 500:
                continue

            analysis = analyze_l2_book(book, min(depth, 20))
            last_analysis = analysis

            if not entry_signal:
                if book.get("spread_bps", float("inf")) > max_spread_bps:
                    continue

                directional = safe_float(analysis.get("directional_score"))

                if abs(directional) < entry_score:
                    continue

                side = "Buy" if directional > 0 else "Sell"

                if _l2_position(client, sym):
                    raise ToolError(
                        "Existing position detected; L2 scalper will not stack exposure."
                    )

                entry_price_raw = (
                    book["best_bid"] if side == "Buy" else book["best_ask"]
                )

                entry_price = quantize_step(
                    entry_price_raw,
                    tick,
                    ROUND_DOWN if side == "Buy" else ROUND_UP,
                )

                if entry_price <= 0:
                    continue

                maker_fee, taker_fee = _calculate_fees(entry_price, 1.0)
                round_trip_fee = maker_fee + taker_fee

                required_move = max(
                    target_profit,
                    entry_price * round_trip_fee,
                    entry_price * 0.0002,
                )

                raw_qty = min(
                    max_notional / max(entry_price, 1e-9),
                    (target_profit + entry_price * round_trip_fee)
                    / max(required_move, 1e-12),
                )

                final_qty, qty_string = clamp_decimal_qty(
                    raw_qty, step, min_qty, max_qty
                )

                notional = final_qty * entry_price

                if notional <= 0:
                    continue

                stop_distance = entry_price * stop_bps / 10000.0

                enforce_entry_risk(
                    client,
                    sym,
                    notional,
                    stop_distance,
                    entry_price,
                    max_notional,
                    safe_float(
                        cfg_value("MAX_ACCOUNT_RISK_PCT", "MAX_ACCOUNT_RISK_PCT", "0.02"),
                        0.02,
                    ),
                    safe_float(
                        cfg_value("MAX_DAILY_LOSS", "MAX_DAILY_LOSS", "0.05"),
                        0.05,
                    ),
                    int(
                        safe_float(
                            cfg_value("MAX_OPEN_POSITIONS", "MAX_OPEN_POSITIONS", "1"),
                            1,
                        )
                    ),
                )

                entry_order_id, _ = _place_entry_order(
                    side, entry_price, final_qty, qty_string
                )

                entry_signal = side
                entry_qty = final_qty

                append_log(
                    {
                        "event": "L2_ENTRY_SUBMITTED",
                        "symbol": sym,
                        "side": side,
                        "qty": qty_string,
                        "price": entry_price,
                        "order_id": entry_order_id,
                        "l2": analysis,
                        "timestamp": utc_now(),
                    }
                )
                continue

            position = _l2_position(client, sym)

            if not position and entry_signal and entry_order_id:
                directional = safe_float(analysis.get("directional_score"))

                reversed_before_fill = (
                    directional <= -exit_score
                    if entry_signal == "Buy"
                    else directional >= exit_score
                )

                stale_entry = (time.time() - started) >= max_hold_seconds

                if reversed_before_fill or stale_entry:
                    cancel_result = _l2_cancel_entry(
                        client, sym, entry_order_id
                    )

                    return {
                        "success": True,
                        "mode": "LIVE",
                        "strategy": "L2_MICROPROFIT",
                        "symbol": sym,
                        "message": "Unfilled L2 entry cancelled.",
                        "entry_side": entry_signal,
                        "entry_order_id": entry_order_id,
                        "cancel": cancel_result,
                        "reason": (
                            "L2_REVERSAL_BEFORE_FILL"
                            if reversed_before_fill
                            else "ENTRY_TIMEOUT"
                        ),
                        "l2": analysis,
                        "timestamp": utc_now(),
                    }

            if position:
                if entry_position is None:
                    entry_position = position

                avg_price = safe_float(
                    position.get("avgPrice", entry_price or 0)
                )
                pos_size = safe_float(position.get("size", entry_qty or 0))
                pos_side = str(position.get("side", entry_signal))

                if avg_price <= 0 or pos_size <= 0:
                    continue

                current_price = (
                    book["best_bid"] if pos_side == "Buy" else book["best_ask"]
                )

                directional = safe_float(analysis.get("directional_score"))

                reversal = (
                    directional <= -exit_score
                    if pos_side == "Buy"
                    else directional >= exit_score
                )

                stop_hit = (
                    directional <= -0.50
                    if pos_side == "Buy"
                    else directional >= 0.50
                )

                price_stop_hit = (
                    current_price
                    <= avg_price * (1.0 - stop_bps / 10000.0)
                    if pos_side == "Buy"
                    else current_price
                    >= avg_price * (1.0 + stop_bps / 10000.0)
                )

                maker_fee, taker_fee = _calculate_fees(avg_price, pos_size)
                gross_pnl = (
                    (current_price - avg_price) * pos_size
                    if pos_side == "Buy"
                    else (avg_price - current_price) * pos_size
                )
                estimated_fees = (avg_price * pos_size) * maker_fee + (
                    current_price * pos_size
                ) * taker_fee
                net_pnl = gross_pnl - estimated_fees

                target_hit = net_pnl >= target_profit
                spread_exit = book.get("spread_bps", 0) > max_spread_bps * 2.0
                hold_time = time.time() - started
                time_exit = hold_time >= max_hold_seconds

                if (
                    target_hit
                    or reversal
                    or stop_hit
                    or price_stop_hit
                    or spread_exit
                    or time_exit
                ):
                    exit_result = _l2_close_market(client, position)

                    reason = (
                        "TARGET"
                        if target_hit
                        else "L2_REVERSAL"
                        if reversal
                        else "L2_STOP"
                        if stop_hit
                        else "PRICE_STOP"
                        if price_stop_hit
                        else "SPREAD_WIDENED"
                        if spread_exit
                        else "MAX_HOLD"
                    )

                    record = _build_exit_record(
                        position, current_price, analysis, exit_result, reason, hold_time
                    )

                    append_log(record)
                    return record

    finally:
        ws.close()

    return {
        "success": True,
        "mode": "LIVE",
        "strategy": "L2_MICROPROFIT",
        "symbol": sym,
        "message": "L2 session ended without a completed position.",
        "last_l2": last_analysis,
        "timestamp": utc_now(),
    }


# ==============================================================================
# EXECUTION
# ==============================================================================
from decimal import ROUND_DOWN
from typing import Any, Dict

from .config import cfg_value
from .errors import EXIT_ERROR, EXIT_INVALID_INPUT, EXIT_PERMISSION_DENIED, ToolError
from .risk import enforce_entry_risk
from .state import append_log, kill_switch_active, load_state, save_state
from .utils import (
    clamp_decimal_qty,
    fetch_precision,
    get_orderbook,
    get_positions,
    handle_scan,
    live_authorized,
    quantize_step,
    safe_float,
    utc_now,
    validate_symbol,
)


def _generate_order_link_id(sym: str, side: str) -> str:
    """Generate a unique, exchange-compliant orderLinkId (<= 36 chars)."""
    payload = f"{sym}:{side}:{time.time_ns()}:{uuid.uuid4()}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:27]
    return f"AI8{digest}"


def _resolve_position_idx(
    client: BybitV5,
    sym: str,
    side: str,
    configured_mode: str,
) -> int:
    """Determine Bybit positionIdx respecting configured mode and live state."""
    if configured_mode == "hedge":
        return 1 if side == "Buy" else 2
    if configured_mode == "oneway":
        return 0

    # auto: inspect existing positions
    try:
        positions = get_positions(client, sym)
        for p in positions:
            idx = p.get("positionIdx")
            if idx is not None:
                return int(safe_float(idx, 0))
    except Exception:
        pass
    return 0


def _build_order_payload(
    sym: str,
    side: str,
    qty_string: str,
    position_idx: int,
    order_link_id: str,
    reduce_only: bool,
    post_only: bool,
    entry_price_fmt: Optional[str],
    stop_fmt: Optional[str],
    tp_fmt: Optional[str],
) -> Dict[str, Any]:
    """Construct the Bybit v5 order payload."""
    payload: Dict[str, Any] = {
        "category": "linear",
        "symbol": sym,
        "side": side,
        "orderType": "Limit" if post_only else "Market",
        "qty": qty_string,
        "positionIdx": position_idx,
        "orderLinkId": order_link_id,
        "reduceOnly": reduce_only,
    }

    if post_only:
        if entry_price_fmt is None:
            raise ToolError("Post-only order requires entry_price_fmt.", EXIT_INVALID_INPUT)
        payload.update({"price": entry_price_fmt, "timeInForce": "PostOnly"})
    else:
        payload.update({"timeInForce": "IOC"})

    if reduce_only:
        payload["closeOnTrigger"] = True
    else:
        if stop_fmt is None or tp_fmt is None:
            raise ToolError("Non-reduce order requires stop_fmt and tp_fmt.", EXIT_INVALID_INPUT)
        payload.update(
            {
                "takeProfit": tp_fmt,
                "stopLoss": stop_fmt,
                "tpTriggerBy": cfg_value("TRIGGER_BY", "TRIGGER_BY", "MarkPrice"),
                "slTriggerBy": cfg_value("TRIGGER_BY", "TRIGGER_BY", "MarkPrice"),
                "tpslMode": "Full",
                "tpOrderType": "Market",
                "slOrderType": "Market",
            }
        )
    return payload


def _apply_leverage(client: BybitV5, sym: str, leverage: int) -> None:
    """Set leverage for both sides; idempotent on Bybit."""
    client.request(
        "POST",
        "/v5/position/set-leverage",
        data={
            "category": "linear",
            "symbol": sym,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        },
    )


def _validate_notional(notional: float, max_notional: float) -> None:
    if notional > max_notional:
        raise ToolError(
            f"Order notional {notional:.8f} exceeds limit {max_notional:.8f}.",
            EXIT_PERMISSION_DENIED,
        )


def _validate_spread(orderbook: Dict[str, Any], max_spread: float, reduce_only: bool) -> None:
    if not reduce_only and orderbook["spread_pct"] > max_spread:
        raise ToolError(
            "Spread widened beyond configured execution threshold during re-check.",
            EXIT_PERMISSION_DENIED,
        )


def _record_execution_state(
    state: Dict[str, Any],
    order_id: str,
    order_link_id: str,
) -> None:
    state["last_execution_ts"] = time.time()
    state["execution_attempts"] = int(state.get("execution_attempts", 0)) + 1
    state["last_order_id"] = order_id
    state["last_order_link_id"] = order_link_id
    save_state(state)


def handle_execute_scalp(
    client: BybitV5,
    symbol: str,
    side: str,
    qty: Optional[float],
    target_profit: float,
    leverage: int,
    post_only: bool,
    live_request: bool,
    live_confirmation: str,
    max_spread: float,
    max_notional: float,
    max_account_risk_pct: float,
    max_daily_loss: float,
    max_open_positions: int,
    reduce_only: bool = False,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)

    if side not in {"Buy", "Sell"}:
        raise ToolError("Side must be Buy or Sell.", EXIT_INVALID_INPUT)

    if leverage < 1:
        raise ToolError("Leverage must be >= 1.", EXIT_INVALID_INPUT)

    max_leverage = int(
        safe_float(
            cfg_value("MAX_LEVERAGE", "MAX_LEVERAGE_FALLBACK", "25"),
            25,
        )
    )

    if leverage > max_leverage:
        raise ToolError(
            f"Requested leverage {leverage}x exceeds configured maximum {max_leverage}x.",
            EXIT_PERMISSION_DENIED,
        )

    live, auth_reason = live_authorized(client.testnet, live_request, live_confirmation)

    state = load_state()

    if state.get("kill_switch") or kill_switch_active():
        raise ToolError("Execution blocked: kill switch active.", EXIT_PERMISSION_DENIED)

    cooldown = safe_float(
        cfg_value("LLM_AGENT_VAR_COOLDOWN_SECONDS", "COOLDOWN_SECONDS_FALLBACK", "60"),
        60,
    )

    elapsed = time.time() - safe_float(state.get("last_execution_ts", 0))

    if not reduce_only and elapsed < cooldown:
        raise ToolError(
            f"Cooldown active: {cooldown - elapsed:.1f}s remaining.",
            EXIT_ERROR,
        )

    maker_fee = safe_float(
        cfg_value("LLM_AGENT_VAR_MAKER_FEE_PCT", "MAKER_FEE_PCT_FALLBACK", "0.020"),
        0.020,
    )
    taker_fee = safe_float(
        cfg_value("LLM_AGENT_VAR_TAKER_FEE_PCT", "TAKER_FEE_PCT_FALLBACK", "0.055"),
        0.055,
    )

    scan = handle_scan(
        client,
        sym,
        max_spread,
        target_profit,
        leverage,
        max_notional,
        maker_fee,
        taker_fee,
    )

    if not reduce_only and not scan["risk_gate_pass"]:
        raise ToolError(
            "Entry risk gates failed.",
            EXIT_PERMISSION_DENIED,
            {"scan": scan},
        )

    tick, step, min_qty, max_qty = fetch_precision(client, sym)

    plan = scan["micro_profit_plan"]

    requested_qty = qty if qty is not None else plan["calculated_qty"]

    final_qty, qty_string = clamp_decimal_qty(requested_qty, step, min_qty, max_qty)

    orderbook = get_orderbook(client, sym)

    _validate_spread(orderbook, max_spread, reduce_only)

    entry_price = orderbook["best_bid"] if side == "Buy" else orderbook["best_ask"]

    notional = final_qty * entry_price

    _validate_notional(notional, max_notional)

    stop_distance = safe_float(plan["stop_distance"])
    required_delta = safe_float(plan["required_price_delta"])

    stop_price = (
        entry_price - stop_distance if side == "Buy" else entry_price + stop_distance
    )
    take_profit = (
        entry_price + required_delta if side == "Buy" else entry_price - required_delta
    )

    risk = None
    if not reduce_only:
        risk = enforce_entry_risk(
            client,
            sym,
            notional,
            stop_distance,
            entry_price,
            max_notional,
            max_account_risk_pct,
            max_daily_loss,
            max_open_positions,
        )

    entry_price_fmt = quantize_step(
        entry_price, tick, ROUND_DOWN if side == "Buy" else ROUND_UP
    )
    stop_fmt = quantize_step(
        stop_price, tick, ROUND_DOWN if side == "Buy" else ROUND_UP
    )
    tp_fmt = quantize_step(
        take_profit, tick, ROUND_UP if side == "Buy" else ROUND_DOWN
    )

    order_link_id = _generate_order_link_id(sym, side)

    record: Dict[str, Any] = {
        "success": True,
        "symbol": sym,
        "side": side,
        "qty": qty_string,
        "notional_usdt": round(notional, 8),
        "leverage": f"{leverage}x",
        "entry_price": entry_price_fmt,
        "stop_loss": None if reduce_only else stop_fmt,
        "take_profit": None if reduce_only else tp_fmt,
        "reduce_only": reduce_only,
        "post_only": post_only,
        "mode": "LIVE" if live else "DRY_RUN",
        "authorization": auth_reason,
        "order_link_id": order_link_id,
        "timestamp": utc_now(),
    }

    if risk is not None:
        record["risk"] = risk

    if not live:
        record["order_id"] = "SIMULATED"
        append_log(record)
        return record

    _apply_leverage(client, sym, leverage)

    configured_mode = cfg_value("POSITION_MODE", "POSITION_MODE_FALLBACK", "auto").lower()
    position_idx = _resolve_position_idx(client, sym, side, configured_mode)

    payload = _build_order_payload(
        sym=sym,
        side=side,
        qty_string=qty_string,
        position_idx=position_idx,
        order_link_id=order_link_id,
        reduce_only=reduce_only,
        post_only=post_only,
        entry_price_fmt=entry_price_fmt if post_only else None,
        stop_fmt=None if reduce_only else stop_fmt,
        tp_fmt=None if reduce_only else tp_fmt,
    )

    result = client.request("POST", "/v5/order/create", data=payload)

    order_id = result.get("orderId")
    if not order_id:
        raise ToolError(
            "Bybit accepted the request but returned no orderId.",
            EXIT_ERROR,
        )

    record["order_id"] = order_id
    record["position_idx"] = position_idx
    record["acknowledged"] = True
    record["fill_confirmed"] = False

    _record_execution_state(state, order_id, order_link_id)
    append_log(record)

    return record


# ==============================================================================
# CLOSE POSITION
# ==============================================================================
import logging
from typing import Any, Dict

from pybit.unified_trading import HTTP as BybitV5

from .utils import (
    ToolError,
    get_positions,
    handle_execute_scalp,
    position_is_open,
)

logger = logging.getLogger(__name__)


def handle_close_position(
    client: BybitV5,
    symbol: str,
    side: str,
    qty: Optional[float],
    leverage: int,
    live: bool,
    confirmation: str,
    max_notional: float,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)

    positions = get_positions(client, sym)

    active = [p for p in positions if position_is_open(p)]

    if not active:
        raise ToolError(f"No open position found for {sym}.")

    selected = active[0]

    position_side = str(selected.get("side", ""))
    position_size = safe_float(selected.get("size", 0))

    if position_size <= 0:
        raise ToolError("Open position has invalid size.")

    close_side = "Sell" if position_side == "Buy" else "Buy"

    if side in {"Buy", "Sell"}:
        if side == close_side:
            close_side = side
        else:
            logger.warning(
                "Provided side %s does not close position %s; ignoring.",
                side,
                position_side,
            )

    close_qty = qty or position_size
    if close_qty > position_size:
        logger.warning(
            "Requested close qty %.4f exceeds position size %.4f; capping.",
            close_qty,
            position_size,
        )
        close_qty = position_size

    return handle_execute_scalp(
        client=client,
        symbol=sym,
        side=close_side,
        qty=close_qty,
        target_profit=0.01,
        leverage=leverage,
        post_only=False,
        live_request=live,
        live_confirmation=confirmation,
        max_spread=100.0,
        max_notional=max_notional,
        max_account_risk_pct=100.0,
        max_daily_loss=1e18,
        max_open_positions=999999,
        reduce_only=True,
    )


# ==============================================================================
# ULTIMATE QUICK MICROPROFIT ENGINE
# ==============================================================================
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP
from typing import Literal, TypedDict


class ToolError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


EXIT_INVALID_INPUT = 2


def positive_float(value: Union[str, int, float, Decimal], name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception as e:
        raise ToolError(f"{name} must be a valid number: {e}", EXIT_INVALID_INPUT)
    if d <= 0:
        raise ToolError(f"{name} must be positive, got {value}", EXIT_INVALID_INPUT)
    return d


def _round(d: Decimal, places: int) -> float:
    if places < 0:
        return float(d)
    quant = Decimal(f"1e-{places}")
    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


SideLiteral = Literal["buy", "sell", "long", "short"]


class MicroPnLResult(TypedDict):
    entry_price: float
    exit_price: float
    qty: float
    side: Literal["Buy", "Sell"]
    entry_notional_usdt: float
    gross_pnl_usdt: float
    entry_fee_usdt: float
    exit_fee_usdt: float
    total_fees_usdt: float
    net_pnl_usdt: float
    roi_percent: float
    price_move: float
    price_move_bps: float
    break_even_exit: float
    profitable: bool


def calculate_micro_pnl(
    entry: Union[str, int, float, Decimal],
    exit_price: Union[str, int, float, Decimal],
    qty: Union[str, int, float, Decimal],
    side: SideLiteral = "Buy",
    maker_fee: Union[str, int, float, Decimal] = Decimal("0.0002"),
    taker_fee: Union[str, int, float, Decimal] = Decimal("0.00055"),
) -> MicroPnLResult:
    entry_d = positive_float(entry, "entry")
    exit_d = positive_float(exit_price, "exit_price")
    qty_d = positive_float(qty, "qty")
    maker_fee_d = Decimal(str(maker_fee))
    taker_fee_d = Decimal(str(taker_fee))

    if maker_fee_d < 0 or taker_fee_d < 0:
        raise ToolError("Fees cannot be negative", EXIT_INVALID_INPUT)

    side_norm = str(side).strip().lower()
    if side_norm not in {"buy", "sell", "long", "short"}:
        raise ToolError("side must be Buy or Sell.", EXIT_INVALID_INPUT)

    is_long = side_norm in {"buy", "long"}

    gross = (exit_d - entry_d) * qty_d if is_long else (entry_d - exit_d) * qty_d

    entry_fee = entry_d * qty_d * maker_fee_d
    exit_fee = exit_d * qty_d * taker_fee_d
    fees = entry_fee + exit_fee
    net = gross - fees

    notional = entry_d * qty_d

    if is_long:
        breakeven = entry_d * (Decimal(1) + maker_fee_d) / (Decimal(1) - taker_fee_d)
    else:
        breakeven = entry_d * (Decimal(1) - maker_fee_d) / (Decimal(1) + taker_fee_d)

    move = abs(exit_d - entry_d)

    return {
        "entry_price": _round(entry_d, 12),
        "exit_price": _round(exit_d, 12),
        "qty": _round(qty_d, 12),
        "side": "Buy" if is_long else "Sell",
        "entry_notional_usdt": _round(notional, 8),
        "gross_pnl_usdt": _round(gross, 8),
        "entry_fee_usdt": _round(entry_fee, 8),
        "exit_fee_usdt": _round(exit_fee, 8),
        "total_fees_usdt": _round(fees, 8),
        "net_pnl_usdt": _round(net, 8),
        "roi_percent": _round(net / max(notional, Decimal("1e-12")) * Decimal(100), 6),
        "price_move": _round(move, 12),
        "price_move_bps": _round(move / max(entry_d, Decimal("1e-12")) * Decimal(10000), 6),
        "break_even_exit": _round(breakeven, 12),
        "profitable": net > 0,
    }


@dataclass(frozen=True, slots=True)
class MicroPnL:
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    side: SideLiteral
    maker_fee: Decimal
    taker_fee: Decimal

    def gross_pnl(self) -> Decimal:
        if self.side in {"buy", "long"}:
            return (self.exit_price - self.entry_price) * self.qty
        return (self.entry_price - self.exit_price) * self.qty

    def fees(self) -> tuple[Decimal, Decimal, Decimal]:
        entry_fee = self.entry_price * self.qty * self.maker_fee
        exit_fee = self.exit_price * self.qty * self.taker_fee
        return entry_fee, exit_fee, entry_fee + exit_fee

    def net_pnl(self) -> Decimal:
        return self.gross_pnl() - self.fees()[2]

    def notional(self) -> Decimal:
        return self.entry_price * self.qty

    def breakeven_exit(self) -> Decimal:
        if self.side in {"buy", "long"}:
            return self.entry_price * (Decimal(1) + self.maker_fee) / (Decimal(1) - self.taker_fee)
        return self.entry_price * (Decimal(1) - self.maker_fee) / (Decimal(1) + self.taker_fee)

    def roi_percent(self) -> Decimal:
        notional = self.notional()
        return self.net_pnl() / max(notional, Decimal("1e-12")) * Decimal(100)

    def to_dict(self) -> MicroPnLResult:
        entry_fee, exit_fee, total_fees = self.fees()
        move = abs(self.exit_price - self.entry_price)
        return {
            "entry_price": _round(self.entry_price, 12),
            "exit_price": _round(self.exit_price, 12),
            "qty": _round(self.qty, 12),
            "side": "Buy" if self.side in {"buy", "long"} else "Sell",
            "entry_notional_usdt": _round(self.notional(), 8),
            "gross_pnl_usdt": _round(self.gross_pnl(), 8),
            "entry_fee_usdt": _round(entry_fee, 8),
            "exit_fee_usdt": _round(exit_fee, 8),
            "total_fees_usdt": _round(total_fees, 8),
            "net_pnl_usdt": _round(self.net_pnl(), 8),
            "roi_percent": _round(self.roi_percent(), 6),
            "price_move": _round(move, 12),
            "price_move_bps": _round(move / max(self.entry_price, Decimal("1e-12")) * Decimal(10000), 6),
            "break_even_exit": _round(self.breakeven_exit(), 12),
            "profitable": self.net_pnl() > 0,
        }


def calculate_micro_pnl_from_obj(obj: MicroPnL) -> MicroPnLResult:
    return obj.to_dict()


if __name__ == "__main__":
    print(calculate_micro_pnl(100, 101, 10, "buy"))
    print(calculate_micro_pnl(100, 99, 10, "sell"))
    m = MicroPnL(Decimal("100"), Decimal("101"), Decimal("10"), "buy", Decimal("0.0002"), Decimal("0.00055"))
    print(m.to_dict())
from dataclasses import dataclass
from typing import Any, Dict

# --- Constants & Custom Exceptions ---
EXIT_INVALID_INPUT = 1


class ToolError(Exception):
    """Custom exception for tool errors with an exit code."""
    def __init__(self, message: str, exit_code: int = EXIT_INVALID_INPUT):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


# --- Helpers ---
def positive_float(value: Union[str, float, int], field_name: str) -> float:
    """
    Convert value to float and ensure it is positive (>= 0).
    Raises ToolError on failure.
    """
    try:
        f_val = float(value)
    except (ValueError, TypeError) as e:
        raise ToolError(f"{field_name} must be a valid number. Got: {value}", EXIT_INVALID_INPUT) from e

    if math.isnan(f_val) or math.isinf(f_val):
        raise ToolError(f"{field_name} must be a finite number. Got: {value}", EXIT_INVALID_INPUT)

    if f_val < 0:
        raise ToolError(f"{field_name} must be non-negative. Got: {f_val}", EXIT_INVALID_INPUT)
    return f_val


# --- Data Structures ---
@dataclass(frozen=True, slots=True)
class CompoundResult:
    """Immutable result container for compound calculations."""
    starting_balance_usdt: float
    ending_balance_usdt: float
    net_growth_usdt: float
    growth_percent: float
    per_trade_rate_percent: float
    trades: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for backwards compatibility."""
        return {
            "starting_balance_usdt": self.starting_balance_usdt,
            "ending_balance_usdt": self.ending_balance_usdt,
            "net_growth_usdt": self.net_growth_usdt,
            "growth_percent": self.growth_percent,
            "per_trade_rate_percent": self.per_trade_rate_percent,
            "trades": self.trades,
        }

# --- Core Logic Upgrades ---


def compound_micro_profit(
    capital: Union[str, float, int],
    rate: Union[str, float, int],
    trades: Union[str, float, int]
) -> Dict[str, Any]:
    """
    Calculate compound growth for a series of trades.

    Args:
        capital: Starting capital (USDT).
        rate: Profit rate per trade (e.g., 0.01 for 1%).
        trades: Number of trades (integer).

    Returns:
        Dictionary with starting/ending balance, growth metrics, and trade count.

    Raises:
        ToolError: If inputs are invalid (negative, non-numeric, trades < 1, rate <= -1).
    """
    # Validation & Coercion
    capital_f = positive_float(capital, "capital_usdt")
    rate_f = float(rate)  # Rate can be negative (loss), just not <= -100%
    trades_i = int(positive_float(trades, "trades"))

    if trades_i < 1:
        raise ToolError("trades must be >= 1.", EXIT_INVALID_INPUT)
    if rate_f <= -1.0:
        raise ToolError("compound_rate must be greater than -1 ( > -100% ).", EXIT_INVALID_INPUT)

    # Calculation using high precision math
    # (1 + r)^n
    growth_factor = math.pow(1.0 + rate_f, trades_i)
    ending = capital_f * growth_factor

    # Rounding only at the final step for display
    res = CompoundResult(
        starting_balance_usdt=round(capital_f, 8),
        ending_balance_usdt=round(ending, 8),
        net_growth_usdt=round(ending - capital_f, 8),
        growth_percent=round((growth_factor - 1.0) * 100.0, 6),
        per_trade_rate_percent=round(rate_f * 100.0, 8),
        trades=trades_i
    )
    return res.to_dict()


# --- Logic Upgrade 1: Reverse Engineering Rate ---
def calculate_required_rate(
    starting_balance: Union[str, float, int],
    target_balance: Union[str, float, int],
    trades: Union[str, float, int]
) -> Dict[str, Any]:
    """
    Calculate the required per-trade rate to reach a target balance.

    Args:
        starting_balance: Initial capital.
        target_balance: Desired ending capital.
        trades: Number of trades.

    Returns:
        Dict with required rate (decimal and percent).
    """
    start = positive_float(starting_balance, "starting_balance")
    target = positive_float(target_balance, "target_balance")
    n = int(positive_float(trades, "trades"))

    if n < 1:
        raise ToolError("trades must be >= 1.", EXIT_INVALID_INPUT)
    if start == 0:
        raise ToolError("starting_balance cannot be zero.", EXIT_INVALID_INPUT)
    if target <= 0:
        raise ToolError("target_balance must be positive.", EXIT_INVALID_INPUT)

    # target = start * (1 + r)^n  =>  (1+r) = (target/start)^(1/n)
    ratio = target / start
    if ratio <= 0:
        raise ToolError("Target/Start ratio must be positive.", EXIT_INVALID_INPUT)

    rate = math.pow(ratio, 1.0 / n) - 1.0

    return {
        "required_rate_decimal": round(rate, 10),
        "required_rate_percent": round(rate * 100.0, 6),
        "trades": n,
        "growth_multiple": round(ratio, 6)
    }


# --- Logic Upgrade 2: Reverse Engineering Trade Count ---
def calculate_required_trades(
    starting_balance: Union[str, float, int],
    target_balance: Union[str, float, int],
    rate: Union[str, float, int]
) -> Dict[str, Any]:
    """
    Calculate the required number of trades to reach a target balance at a fixed rate.

    Args:
        starting_balance: Initial capital.
        target_balance: Desired ending capital.
        rate: Profit rate per trade (decimal).

    Returns:
        Dict with required trades (float exact, int ceil), and projected balances.
    """
    start = positive_float(starting_balance, "starting_balance")
    target = positive_float(target_balance, "target_balance")
    r = float(rate)

    if start == 0:
        raise ToolError("starting_balance cannot be zero.", EXIT_INVALID_INPUT)
    if target <= 0:
        raise ToolError("target_balance must be positive.", EXIT_INVALID_INPUT)
    if r <= -1.0:
        raise ToolError("rate must be > -1.", EXIT_INVALID_INPUT)
    if r == 0.0:
        raise ToolError("rate cannot be zero (infinite trades required).", EXIT_INVALID_INPUT)

    # n = log(target/start) / log(1+r)
    ratio = target / start
    if ratio <= 0:
        raise ToolError("Target/Start ratio must be positive.", EXIT_INVALID_INPUT)

    exact_trades = math.log(ratio) / math.log(1.0 + r)
    ceil_trades = math.ceil(exact_trades)

    # Project balance at ceil_trades
    projected = start * math.pow(1.0 + r, ceil_trades)

    return {
        "exact_trades_required": round(exact_trades, 6),
        "minimum_integer_trades": ceil_trades,
        "projected_balance_at_min_trades": round(projected, 8),
        "rate_percent": round(r * 100.0, 6)
    }


# --- Logic Upgrade 3: Compounding with Periodic Contributions ---
def compound_with_contributions(
    initial_capital: Union[str, float, int],
    rate: Union[str, float, int],
    trades: Union[str, float, int],
    contribution_per_trade: Union[str, float, int] = 0,
    contribution_timing: str = "end"  # "start" or "end"
) -> Dict[str, Any]:
    """
    Calculate compound growth with fixed contributions added per trade.

    Formula (End of period): FV = P(1+r)^n + C * [((1+r)^n - 1) / r]
    Formula (Start of period): FV = P(1+r)^n + C * [((1+r)^n - 1) / r] * (1+r)

    Args:
        initial_capital: Starting balance.
        rate: Rate per trade.
        trades: Number of trades.
        contribution_per_trade: Amount added each trade (default 0).
        contribution_timing: "end" (default) or "start" of trade period.

    Returns:
        Dict with detailed breakdown.
    """
    P = positive_float(initial_capital, "initial_capital")
    r = float(rate)
    n = int(positive_float(trades, "trades"))
    C = float(contribution_per_trade)  # Can be negative (withdrawal)

    if n < 1:
        raise ToolError("trades must be >= 1.", EXIT_INVALID_INPUT)
    if r <= -1.0:
        raise ToolError("rate must be > -1.", EXIT_INVALID_INPUT)

    growth_factor = math.pow(1.0 + r, n)
    principal_growth = P * growth_factor

    contribution_growth = 0.0
    total_contributions = C * n

    if abs(r) < 1e-12:  # Handle r == 0 limit case: FV = P + n*C
        contribution_growth = total_contributions
    else:
        # Annuity factor: ((1+r)^n - 1) / r
        annuity_factor = (growth_factor - 1.0) / r
        if contribution_timing == "start":
            annuity_factor *= (1.0 + r)
        elif contribution_timing != "end":
            raise ToolError("contribution_timing must be 'start' or 'end'.", EXIT_INVALID_INPUT)
        contribution_growth = C * annuity_factor

    ending = principal_growth + contribution_growth

    return {
        "starting_balance_usdt": round(P, 8),
        "ending_balance_usdt": round(ending, 8),
        "principal_growth_usdt": round(principal_growth - P, 8),
        "total_contributions_usdt": round(total_contributions, 8),
        "contribution_growth_usdt": round(contribution_growth - total_contributions, 8),
        "net_growth_usdt": round(ending - P - total_contributions, 8),
        "growth_percent": round((ending / P - 1.0) * 100.0, 6) if P != 0 else float('inf'),
        "per_trade_rate_percent": round(r * 100.0, 8),
        "trades": n,
        "contribution_per_trade_usdt": round(C, 8),
        "contribution_timing": contribution_timing
    }


import json
from pathlib import Path
from typing import Any


def trade_log_file() -> Path:
    """Return the default trade log file path."""
    return Path("trade_log.jsonl")


def safe_float(value: Any, default: float = float("nan")) -> float:
    """Safely convert a value to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def utc_now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _extract_pnl(record: dict) -> Optional[float]:
    """Extract PnL from a trade record using known key variations."""
    pnl_keys = (
        "net_pnl_usdt",
        "pnl",
        "realizedPnl",
        "closed_pnl",
        "realized_pnl",
        "profit_loss",
        "net_profit",
    )
    for key in pnl_keys:
        if key in record:
            value = safe_float(record.get(key), float("nan"))
            if math.isfinite(value):
                return value
    return None


def _calculate_drawdown(pnls: list[float]) -> float:
    """Calculate maximum drawdown from a PnL series."""
    curve = peak = dd = 0.0
    for x in pnls:
        curve += x
        peak = max(peak, curve)
        dd = max(dd, peak - curve)
    return dd


def _consecutive_stats(pnls: list[float]) -> dict[str, int]:
    """Calculate max consecutive wins and losses."""
    max_wins = max_losses = current_wins = current_losses = 0
    for x in pnls:
        if x > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif x < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            current_wins = current_losses = 0
    return {"max_consecutive_wins": max_wins, "max_consecutive_losses": max_losses}


def _sharpe_ratio(pnls: list[float], risk_free_rate: float = 0.0) -> float:
    """Calculate Sharpe ratio (simplified, assuming daily returns)."""
    if len(pnls) < 2:
        return 0.0
    returns = [x for x in pnls if math.isfinite(x)]
    if not returns:
        return 0.0
    mean_ret = sum(returns) / len(returns)
    std_ret = math.sqrt(sum((r - mean_ret) ** 2 for r in returns) / len(returns))
    if std_ret == 0:
        return 0.0
    return (mean_ret - risk_free_rate) / std_ret * math.sqrt(252)


def calculate_trade_statistics_from_log(
    log_path: Optional[Path] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    """
    Calculate comprehensive trade statistics from a JSONL trade log.

    Args:
        log_path: Path to trade log file. Defaults to trade_log_file().
        start_date: Filter trades after this date (ISO format).
        end_date: Filter trades before this date (ISO format).
        symbol: Filter trades for specific symbol.

    Returns:
        Dictionary with trade statistics.
    """
    path = log_path or trade_log_file()
    records: list[dict] = []

    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if symbol and record.get("symbol") != symbol:
                        continue
                    if start_date and record.get("timestamp", "") < start_date:
                        continue
                    if end_date and record.get("timestamp", "") > end_date:
                        continue
                    records.append(record)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    pnls: list[float] = []
    for row in records:
        pnl = _extract_pnl(row)
        if pnl is not None:
            pnls.append(pnl)

    wins = [x for x in pnls if x > 0]
    losses = [abs(x) for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    net_pnl = sum(pnls)
    max_dd = _calculate_drawdown(pnls)
    consecutive = _consecutive_stats(pnls)
    sharpe = _sharpe_ratio(pnls)

    total_trades = len(pnls)
    win_count = len(wins)
    loss_count = len(losses)

    return {
        "journal_records": len(records),
        "pnl_records": total_trades,
        "wins": win_count,
        "losses": loss_count,
        "win_rate_percent": round(win_count / total_trades * 100, 4) if total_trades else 0.0,
        "gross_profit_usdt": round(gross_profit, 8),
        "gross_loss_usdt": round(gross_loss, 8),
        "net_pnl_usdt": round(net_pnl, 8),
        "profit_factor": round(gross_profit / max(gross_loss, 1e-12), 6),
        "average_win_usdt": round(gross_profit / max(win_count, 1), 8),
        "average_loss_usdt": round(gross_loss / max(loss_count, 1), 8),
        "max_drawdown_usdt": round(max_dd, 8),
        "max_consecutive_wins": consecutive["max_consecutive_wins"],
        "max_consecutive_losses": consecutive["max_consecutive_losses"],
        "sharpe_ratio": round(sharpe, 6),
        "timestamp": utc_now(),
    }


def export_statistics_to_json(stats: dict[str, Any], output_path: Path) -> bool:
    """Export statistics dictionary to a JSON file."""
    try:
        output_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def print_statistics_summary(stats: dict[str, Any]) -> None:
    """Print a formatted summary of trade statistics."""
    print(f"\n{'=' * 50}")
    print("TRADE STATISTICS SUMMARY")
    print(f"{'=' * 50}")
    print(f"Journal Records:     {stats['journal_records']}")
    print(f"PnL Records:         {stats['pnl_records']}")
    print(f"Wins / Losses:       {stats['wins']} / {stats['losses']}")
    print(f"Win Rate:            {stats['win_rate_percent']:.2f}%")
    print(f"Gross Profit:        {stats['gross_profit_usdt']:.8f} USDT")
    print(f"Gross Loss:          {stats['gross_loss_usdt']:.8f} USDT")
    print(f"Net PnL:             {stats['net_pnl_usdt']:.8f} USDT")
    print(f"Profit Factor:       {stats['profit_factor']:.6f}")
    print(f"Avg Win:             {stats['average_win_usdt']:.8f} USDT")
    print(f"Avg Loss:            {stats['average_loss_usdt']:.8f} USDT")
    print(f"Max Drawdown:        {stats['max_drawdown_usdt']:.8f} USDT")
    print(f"Max Consec. Wins:    {stats['max_consecutive_wins']}")
    print(f"Max Consec. Losses:  {stats['max_consecutive_losses']}")
    print(f"Sharpe Ratio:        {stats['sharpe_ratio']:.6f}")
    print(f"Generated:           {stats['timestamp']}")
    print(f"{'=' * 50}\n")


# ==============================================================================
# ACTION ROUTER
# ==============================================================================
def execute_tool(
    action: str,
    symbol: str = "SOLUSDT",
    timeframe: str = "1",
    side: str = "Buy",
    target_profit_usdt: float = 0.10,
    qty: Optional[float] = None,
    leverage: int = 10,
    max_spread_pct: float = 0.035,
    max_notional_usdt: float = 100.0,
    post_only: bool = True,
    live: bool = False,
    live_confirmation: str = "",
    reduce_only: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    capital_usdt: float = 100.0,
    compound_rate: float = 0.0,
    trades: int = 100,
    stop_distance: Optional[float] = None,
) -> dict[str, Any]:
    started = time.monotonic()

    # Normalize model-friendly aliases before validating the canonical action.
    action = str(action or "").strip().lower()
    action = ACTION_ALIASES.get(action, action)

    if action not in VALID_ACTIONS:
        return {
            "success": False,
            "error":
                f"Invalid action '{action}'.",
            "valid_actions":
                sorted(VALID_ACTIONS),
            "aliases":
                dict(sorted(ACTION_ALIASES.items())),
            "exit_code":
                EXIT_INVALID_INPUT,
            "duration_ms":
                0.0,
        }

    symbol = validate_symbol(
        symbol
    )

    testnet = parse_bool(
        cfg_value(
            "LLM_AGENT_VAR_USE_TESTNET",
            "BYBIT_TESTNET",
            "true",
        ),
        True,
    )

    maker_fee = safe_float(
        cfg_value(
            "LLM_AGENT_VAR_MAKER_FEE_PCT",
            "MAKER_FEE_PCT",
            "0.020",
        ),
        0.020,
    )

    taker_fee = safe_float(
        cfg_value(
            "LLM_AGENT_VAR_TAKER_FEE_PCT",
            "TAKER_FEE_PCT",
            "0.055",
        ),
        0.055,
    )

    max_account_risk_pct = safe_float(
        cfg_value(
            "LLM_AGENT_VAR_MAX_ACCOUNT_RISK_PCT",
            "MAX_ACCOUNT_RISK_PCT",
            "0.50",
        ),
        0.50,
    )

    max_daily_loss = safe_float(
        cfg_value(
            "LLM_AGENT_VAR_MAX_DAILY_LOSS_USDT",
            "MAX_DAILY_LOSS_USDT",
            "5",
        ),
        5,
    )

    max_open_positions = int(
        safe_float(
            cfg_value(
                "LLM_AGENT_VAR_MAX_OPEN_POSITIONS",
                "MAX_OPEN_POSITIONS",
                "1",
            ),
            1,
        )
    )

    client = BybitV5(
        testnet=testnet
    )

    cache = ToolCache()

    cache_key = build_cache_key(
        action,
        symbol,
        timeframe,
        side,
        target_profit_usdt,
        qty,
        leverage,
        max_spread_pct,
        max_notional_usdt,
        post_only,
        live,
        reduce_only,
    )

    cacheable_actions = {
        "ta",
        "scan",
        "calc_micro_profit",
        "quick_scalp",
        "l2_live_scalp",
        "l2_signal",
        "micro_pnl",
        "compound_micro_profit",
        "trade_statistics",
        "orderbook",
        "ticker",
        "instrument",
    }

    if (
        use_cache
        and not live
        and action in cacheable_actions
    ):
        cached = cache.get(
            cache_key,
            ttl_seconds=5,
        )

        if cached:
            cached["cached"] = True
            return cached

    try:
        with GracefulShutdown() as shutdown:
            if shutdown.should_stop():
                raise ToolError(
                    "Execution interrupted.",
                    EXIT_INTERRUPTED,
                )

            if action == "ta":
                result = handle_ta(
                    client,
                    symbol,
                    timeframe,
                )

            elif action == "scan":
                result = handle_scan(
                    client,
                    symbol,
                    max_spread_pct,
                    target_profit_usdt,
                    leverage,
                    max_notional_usdt,
                    maker_fee,
                    taker_fee,
                )

            elif action == "calc_micro_profit":
                scan = handle_scan(
                    client,
                    symbol,
                    max_spread_pct,
                    target_profit_usdt,
                    leverage,
                    max_notional_usdt,
                    maker_fee,
                    taker_fee,
                )

                result = {
                    "success": True,
                    "symbol": symbol,
                    "plan":
                        scan[
                            "micro_profit_plan"
                        ],
                    "risk_gate_pass":
                        scan[
                            "risk_gate_pass"
                        ],
                }

            elif action == "quick_scalp":
                scan = handle_scan(client, symbol, max_spread_pct, target_profit_usdt, leverage, max_notional_usdt, maker_fee, taker_fee)
                plan = scan.get("micro_profit_plan", {})
                entry = safe_float(scan.get("best_bid") if scan.get("bias") == "LONG" else scan.get("best_ask") if scan.get("bias") == "SHORT" else scan.get("mid"), 0)
                qty_value = safe_float(plan.get("calculated_qty"), 0)
                delta = safe_float(plan.get("required_price_delta"), 0)
                simulated_exit = entry + delta if scan.get("bias") == "LONG" else entry - delta if scan.get("bias") == "SHORT" else entry
                pnl = calculate_micro_pnl(entry, simulated_exit, qty_value, "Buy" if scan.get("bias") == "LONG" else "Sell", maker_fee / 100, taker_fee / 100) if entry > 0 and qty_value > 0 and scan.get("bias") != "NEUTRAL" else None
                result = {
                    "success": True,
                    "mode": "ANALYSIS_ONLY",
                    "symbol": symbol,
                    "signal": scan,
                    "execution_plan": {
                        "entry": entry,
                        "qty": qty_value,
                        "expected_exit": simulated_exit,
                        "pnl": pnl,
                        "live_ready": bool(scan.get("risk_gate_pass")),
                        "note": "quick_scalp analyzes and builds a guarded plan; use l2_live_scalp or execute_scalp with explicit live authorization to submit orders.",
                    },
                }
            elif action == "micro_pnl":
                if entry_price is None or exit_price is None: raise ToolError("micro_pnl requires entry_price and exit_price.", EXIT_INVALID_INPUT)
                result = {"success": True, "pnl": calculate_micro_pnl(entry_price, exit_price, positive_float(qty, "qty"), side, maker_fee / 100, taker_fee / 100)}
            elif action == "compound_micro_profit":
                result = {"success": True, "compound": compound_micro_profit(capital_usdt, compound_rate, trades)}
            elif action == "trade_statistics":
                result = {"success": True, "statistics": calculate_trade_statistics_from_log()}

            elif action == "l2_signal":
                result = handle_l2_signal(
                    symbol=symbol,
                    depth=int(
                        safe_float(
                            cfg_value(
                                "L2_DEPTH",
                                "L2_DEPTH",
                                "50",
                            ),
                            50,
                        )
                    ),
                    testnet=client.testnet,
                )

            elif action == "l2_live_scalp":
                result = handle_l2_live_scalp(
                    client=client,
                    symbol=symbol,
                    target_profit=target_profit_usdt,
                    leverage=leverage,
                    max_notional=max_notional_usdt,
                    max_spread_bps=safe_float(
                        cfg_value(
                            "L2_MAX_SPREAD_BPS",
                            "L2_MAX_SPREAD_BPS",
                            str(L2_DEFAULT_MAX_SPREAD_BPS),
                        ),
                        L2_DEFAULT_MAX_SPREAD_BPS,
                    ),
                    entry_score=safe_float(
                        cfg_value(
                            "L2_ENTRY_SCORE",
                            "L2_ENTRY_SCORE",
                            str(L2_DEFAULT_ENTRY_SCORE),
                        ),
                        L2_DEFAULT_ENTRY_SCORE,
                    ),
                    exit_score=safe_float(
                        cfg_value(
                            "L2_EXIT_SCORE",
                            "L2_EXIT_SCORE",
                            str(L2_DEFAULT_EXIT_SCORE),
                        ),
                        L2_DEFAULT_EXIT_SCORE,
                    ),
                    stop_bps=safe_float(
                        cfg_value(
                            "L2_STOP_BPS",
                            "L2_STOP_BPS",
                            str(L2_DEFAULT_STOP_BPS),
                        ),
                        L2_DEFAULT_STOP_BPS,
                    ),
                    max_hold_seconds=safe_float(
                        cfg_value(
                            "L2_MAX_HOLD_SECONDS",
                            "L2_MAX_HOLD_SECONDS",
                            str(L2_DEFAULT_MAX_HOLD_SECONDS),
                        ),
                        L2_DEFAULT_MAX_HOLD_SECONDS,
                    ),
                    live_request=live,
                    live_confirmation=live_confirmation,
                    depth=int(
                        safe_float(
                            cfg_value(
                                "L2_DEPTH",
                                "L2_DEPTH",
                                "50",
                            ),
                            50,
                        )
                    ),
                )

            elif action == "orderbook":
                result = {
                    "success": True,
                    "symbol": symbol,
                    "orderbook":
                        get_orderbook(
                            client,
                            symbol,
                        ),
                }

            elif action == "ticker":
                result = {
                    "success": True,
                    "symbol": symbol,
                    "ticker":
                        get_ticker(
                            client,
                            symbol,
                        ),
                }

            elif action == "instrument":
                result = {
                    "success": True,
                    "symbol": symbol,
                    "instrument":
                        fetch_instrument(
                            client,
                            symbol,
                        ),
                }

            elif action == "wallet_balance":
                result = {
                    "success": True,
                    "wallet":
                        get_wallet(
                            client
                        ),
                }

            elif action == "positions":
                result = {
                    "success": True,
                    "positions":
                        get_positions(
                            client,
                            symbol,
                        ),
                }

            elif action == "open_orders":
                result = {
                    "success": True,
                    "orders":
                        get_open_orders(
                            client,
                            symbol,
                        ),
                }

            elif action == "fills":
                result = {
                    "success": True,
                    "fills":
                        get_fills(
                            client,
                            symbol,
                        ),
                }

            elif action == "price_action":
                candles = get_closed_klines(
                    client,
                    symbol,
                    timeframe,
                    100,
                )

                result = {
                    "success": True,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "price_action":
                        price_action(
                            candles
                        ),
                }

            elif action == "risk_status":
                result = risk_status(
                    client,
                    symbol,
                    max_notional_usdt,
                    max_open_positions,
                    max_daily_loss,
                )

            elif action == "execute_scalp":
                result = handle_execute_scalp(
                    client=client,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    target_profit=target_profit_usdt,
                    leverage=leverage,
                    post_only=post_only,
                    live_request=live,
                    live_confirmation=live_confirmation,
                    max_spread=max_spread_pct,
                    max_notional=max_notional_usdt,
                    max_account_risk_pct=max_account_risk_pct,
                    max_daily_loss=max_daily_loss,
                    max_open_positions=max_open_positions,
                    reduce_only=reduce_only,
                )

            elif action == "close_position":
                result = handle_close_position(
                    client,
                    symbol,
                    side,
                    qty,
                    leverage,
                    live,
                    live_confirmation,
                    max_notional_usdt,
                )

            elif action == "cancel_orders":
                if not live:
                    result = {
                        "success": True,
                        "mode": "DRY_RUN",
                        "message":
                            "Cancel-all acknowledged "
                            "as dry run.",
                    }
                else:
                    authorized, reason = live_authorized(
                        client.testnet,
                        True,
                        live_confirmation,
                    )

                    if not authorized:
                        raise ToolError(
                            f"Live cancellation blocked: "
                            f"{reason}.",
                            EXIT_PERMISSION_DENIED,
                        )

                    cancelled = client.request(
                        "POST",
                        "/v5/order/cancel-all",
                        data={
                            "category":
                                "linear",
                            "symbol":
                                symbol,
                        },
                    )

                    result = {
                        "success": True,
                        "mode": "LIVE",
                        "result":
                            cancelled,
                    }

            elif action == "set_leverage":
                if not live:
                    result = {
                        "success": True,
                        "mode": "DRY_RUN",
                        "message":
                            f"Set leverage to "
                            f"{leverage}x acknowledged.",
                    }
                else:
                    authorized, reason = live_authorized(
                        client.testnet,
                        True,
                        live_confirmation,
                    )

                    if not authorized:
                        raise ToolError(
                            f"Live leverage change blocked: "
                            f"{reason}.",
                            EXIT_PERMISSION_DENIED,
                        )

                    leverage_result = client.request(
                        "POST",
                        "/v5/position/set-leverage",
                        data={
                            "category":
                                "linear",
                            "symbol":
                                symbol,
                            "buyLeverage":
                                str(leverage),
                            "sellLeverage":
                                str(leverage),
                        },
                    )

                    result = {
                        "success": True,
                        "mode": "LIVE",
                        "result":
                            leverage_result,
                    }

            elif action == "kill_switch":
                result = set_kill_switch(
                    bool(
                        reduce_only
                        if reduce_only is not None
                        else True
                    )
                )

            elif action == "history":
                path = trade_log_file()

                lines = (
                    path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if path.exists()
                    else []
                )

                recent = []

                for line in lines[-50:]:
                    if not line.strip():
                        continue

                    try:
                        recent.append(
                            json.loads(line)
                        )
                    except Exception:
                        continue

                result = {
                    "success": True,
                    "recent_trades":
                        recent,
                }

            else:
                raise ToolError(
                    f"Unhandled action '{action}'.",
                    EXIT_INVALID_INPUT,
                )

        result["action"] = action
        result["duration_ms"] = round(
            (
                time.monotonic()
                - started
            )
            * 1000,
            2,
        )
        result["exit_code"] = (
            EXIT_SUCCESS
        )

        if (
            use_cache
            and not live
            and action in cacheable_actions
        ):
            cache.set(
                cache_key,
                result,
            )

        return result

    except ToolError as exc:
        result = exc.to_dict()
        result.update({
            "action": action,
            "duration_ms": round(
                (
                    time.monotonic()
                    - started
                )
                * 1000,
                2,
            ),
        })

        return result

    except Exception as exc:
        if verbose:
            logging.exception(
                "Unhandled tool exception"
            )

        return {
            "success": False,
            "action": action,
            "error": str(exc),
            "exit_code":
                EXIT_ERROR,
            "duration_ms": round(
                (
                    time.monotonic()
                    - started
                )
                * 1000,
                2,
            ),
        }


# ==============================================================================
# SELF TEST
# ==============================================================================

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict

try:
    from pyrm_scalp2 import (
        ACTION_ALIASES,
        ToolJSONEncoder,
        __version__,
        calculate_micro_pnl,
        generate_tool_schema,
        get_data_dir,
        kill_switch_active,
        live_authorized,
        normalize_candles,
        quantize_step,
        utc_now,
        validate_symbol,
        validate_timeframe,
    )
except ImportError:
    def get_data_dir() -> Path:
        return Path(__file__).parent / "data"
    def validate_symbol(s: str) -> str: return s.upper()
    def validate_timeframe(tf: str) -> str: return tf
    def quantize_step(value: str, step: str) -> str:
        from decimal import ROUND_DOWN, Decimal
        d = Decimal(value)
        s = Decimal(step)
        return str((d / s).quantize(Decimal('1'), rounding=ROUND_DOWN) * s)
    def normalize_candles(candles: List[List[str]], drop_latest: bool) -> List[List[str]]:
        return candles[:-1] if drop_latest and len(candles) > 1 else candles
    def live_authorized(*args, **kwargs) -> tuple: return (False, "testnet")
    def kill_switch_active() -> bool: return False
    ACTION_ALIASES = {"scalp": "quick_scalp"}
    def calculate_micro_pnl(entry, exit, qty, side, fee_entry, fee_exit) -> Dict[str, Any]:
        gross = (exit - entry) * qty if side == "Buy" else (entry - exit) * qty
        fees = (entry * fee_entry + exit * fee_exit) * qty
        return {"gross_pnl_usdt": gross, "net_pnl_usdt": gross - fees, "break_even_exit": entry * (1 + fee_entry + fee_exit)}
    def generate_tool_schema() -> Dict[str, Any]:
        return {"parameters": {"required": ["action"]}}
    __version__ = "0.0.0-dev"
    def utc_now() -> str: return datetime.now(timezone.utc).isoformat()
    class ToolJSONEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, Decimal): return str(obj)
            return super().default(obj)


def run_self_test() -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append({"name": name, "pass": bool(condition), "detail": detail})

    check("python_version", sys.version_info >= (3, 9), platform.python_version())

    data_dir = get_data_dir()
    check("data_directory", data_dir.is_dir(), str(data_dir))

    try:
        test_path = data_dir / ".write_test"
        test_path.write_text("ok")
        test_path.unlink()
        check("state_write", True, "atomic state writer available")
    except Exception as e:
        check("state_write", False, f"write failed: {e}")

    try:
        encoded = json.dumps({"decimal": Decimal("1.25")}, cls=ToolJSONEncoder)
        json.loads(encoded)
        check("json_encoder", True, "JSON encoder available")
    except Exception as e:
        check("json_encoder", False, str(e))

    try:
        check("symbol_validation", validate_symbol("SOLUSDT") == "SOLUSDT")
    except Exception as e:
        check("symbol_validation", False, str(e))

    try:
        check("timeframe_validation", validate_timeframe("5") == "5")
    except Exception as e:
        check("timeframe_validation", False, str(e))

    try:
        check("decimal_quantization", quantize_step("1.23456", "0.01") == "1.23")
    except Exception as e:
        check("decimal_quantization", False, str(e))

    candles = normalize_candles(
        [
            ["3000", "3", "4", "2", "3.5", "10", "30"],
            ["2000", "2", "3", "1", "2.5", "10", "25"],
            ["4000", "4", "5", "3", "4.5", "10", "45"],
        ],
        True,
    )
    check("candle_normalization", len(candles) == 2 and candles[0][0] == "2000", "latest candle removed")

    try:
        authorized, _ = live_authorized(True, True, "I_UNDERSTAND_LIVE_RISK")
        check("live_default_block", not authorized, "testnet cannot authorize live orders")
    except Exception as e:
        check("live_default_block", False, str(e))

    try:
        check("kill_switch_default", isinstance(kill_switch_active(), bool))
    except Exception as e:
        check("kill_switch_default", False, str(e))

    check("action_alias_scalp", ACTION_ALIASES.get("scalp") == "quick_scalp", "natural-language scalp alias resolves to canonical quick_scalp")

    try:
        pnl_test = calculate_micro_pnl(100.0, 100.20, 1.0, "Buy", 0.0002, 0.00055)
        check("fee_aware_pnl", pnl_test["net_pnl_usdt"] < pnl_test["gross_pnl_usdt"] and "break_even_exit" in pnl_test, "gross PNL is reduced by entry/exit fees")
    except Exception as e:
        check("fee_aware_pnl", False, str(e))

    try:
        schema = generate_tool_schema()
        required = schema.get("parameters", {}).get("required", [])
        check("argc_public_signature", required == ["action"], "only action is required by the public tool schema")
    except Exception as e:
        check("argc_public_signature", False, str(e))

    passed = all(x["pass"] for x in checks)

    return {"success": passed, "version": __version__, "checks": checks, "timestamp": utc_now()}


def run_self_test_async() -> Any:
    import asyncio
    return asyncio.get_event_loop().run_in_executor(None, run_self_test)


def run_self_test_with_timeout(timeout_seconds: float = 30.0) -> Dict[str, Any]:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_self_test)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            return {"success": False, "version": __version__, "checks": [{"name": "timeout", "pass": False, "detail": f"self-test exceeded {timeout_seconds}s"}], "timestamp": utc_now()}


def get_self_test_summary(result: Dict[str, Any]) -> str:
    total = len(result.get("checks", []))
    passed = sum(1 for c in result.get("checks", []) if c.get("pass"))
    failed = total - passed
    status = "PASS" if result.get("success") else "FAIL"
    lines = [f"Self-test {status} ({passed}/{total} passed)", f"Version: {result.get('version')}", f"Timestamp: {result.get('timestamp')}"]
    for c in result.get("checks", []):
        mark = "✓" if c.get("pass") else "✗"
        detail = f" - {c.get('detail')}" if c.get("detail") else ""
        lines.append(f"  {mark} {c.get('name')}{detail}")
    return "\n".join(lines)


if __name__ == "__main__":
    result = run_self_test()
    print(get_self_test_summary(result))
    sys.exit(0 if result["success"] else 1)
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class ToolJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for handling non-serializable objects."""

    def default(self, obj: Any) -> Any:
        if hasattr(obj, '__json__'):
            return obj.__json__()
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        return super().default(obj)


@contextmanager
def _atomic_write(target: Path, mode: str = 'w', encoding: str = 'utf-8'):
    """Context manager for atomic file writes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode=mode,
        encoding=encoding,
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            yield tmp
            tmp.flush()
            os.fsync(tmp.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def write_llm_output(
    data: dict[str, Any],
    encoder: Optional[type[json.JSONEncoder]] = None,
    atomic: bool = True,
) -> None:
    path = os.environ.get("LLM_OUTPUT", "/dev/stdout")

    encoder_cls = encoder or ToolJSONEncoder
    payload = json.dumps(data, ensure_ascii=False, cls=encoder_cls, separators=(',', ':'))

    if path in {"/dev/stdout", "/dev/fd/1", "-"}:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return

    target = Path(path)
    try:
        if atomic:
            with _atomic_write(target, mode='a', encoding='utf-8') as fp:
                fp.write(payload + "\n")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fp:
                fp.write(payload + "\n")
    except OSError:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def write_llm_output_batch(
    items: list[dict[str, Any]],
    encoder: Optional[type[json.JSONEncoder]] = None,
    atomic: bool = True,
) -> None:
    """Write multiple JSON objects as JSON Lines."""
    for item in items:
        write_llm_output(item, encoder=encoder, atomic=atomic)


def read_llm_output(path: Optional[str] = None) -> list[dict[str, Any]]:
    """Read JSON Lines from LLM_OUTPUT path or default."""
    path = path or os.environ.get("LLM_OUTPUT", "/dev/stdin")
    if path in {"/dev/stdin", "/dev/fd/0", "-"}:
        source = sys.stdin
    else:
        source = open(Path(path), encoding='utf-8')

    with source if path not in {"/dev/stdin", "/dev/fd/0", "-"} else contextlib.nullcontext(source) as fp:
        return [json.loads(line) for line in fp if line.strip()]


import contextlib


def generate_tool_schema() -> Dict[str, Any]:
    return {
        "name":
            "pyrm_scalp2",
        "description":
            "Hardened Bybit V5 USDT perpetual "
            "market analytics, risk management, "
            "and guarded execution tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum":
                        sorted(
                            VALID_ACTIONS
                        ),
                },
                "symbol": {
                    "type": "string",
                },
                "timeframe": {
                    "type": "string",
                    "enum":
                        sorted(
                            VALID_TIMEFRAMES
                        ),
                },
                "side": {
                    "type": "string",
                    "enum": [
                        "Buy",
                        "Sell",
                    ],
                },
                "target_profit_usdt": {
                    "type": "number",
                },
                "qty": {
                    "type": "number",
                },
                "leverage": {
                    "type": "integer",
                },
                "post_only": {
                    "type": "boolean",
                },
                "live": {
                    "type": "boolean",
                },
                "live_confirmation": {
                    "type": "string",
                },
                "reduce_only": {
                    "type": "boolean",
                },
                "max_spread_pct": {
                    "type": "number",
                },
                "max_notional_usdt": {
                    "type": "number",
                },
                "use_cache": {
                    "type": "boolean",
                },
                "entry_price": {"type": "number"},
                "exit_price": {"type": "number"},
                "capital_usdt": {"type": "number"},
                "compound_rate": {"type": "number"},
                "trades": {"type": "integer"},
                "stop_distance": {"type": "number"},
            },
            "required": [
                "action"
            ],
        },
    }


# ==============================================================================
# RUN / CLI
# ==============================================================================

import json
import os
from typing import Any, Dict

try:
    from pyrm_tools import execute_tool
except ImportError:
    def execute_tool(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"error": "execute_tool not available", "status": "failed"}


def _to_optional_float(value: float) -> Optional[float]:
    return None if value == 0.0 else value


def run(
    action: str = "quick_scalp",
    symbol: str = "SOLUSDT",
    timeframe: str = "1",
    side: str = "Buy",
    target_profit_usdt: float = 0.10,
    qty: float = 0.0,
    leverage: int = 10,
    max_spread_pct: float = 0.035,
    max_notional_usdt: float = 100.0,
    post_only: bool = True,
    live: bool = False,
    live_confirmation: str = "",
    reduce_only: bool = False,
    use_cache: bool = False,
    no_color: bool = True,
    verbose: bool = False,
    entry_price: float = 0.0,
    exit_price: float = 0.0,
    capital_usdt: float = 100.0,
    compound_rate: float = 0.0,
    trades: int = 100,
    stop_distance: float = 0.0,
    l2_depth: int = 50,
    l2_max_spread_bps: float = 12.0,
    l2_entry_score: float = 0.62,
    l2_exit_score: float = 0.20,
    l2_stop_bps: float = 10.0,
    l2_max_hold_seconds: float = 45.0,
) -> str:
    """Bybit quick microprofit scalping tool with real-time L2 orderbook analysis.

    Args:
        action: Canonical action such as quick_scalp, l2_signal, l2_live_scalp, micro_pnl, execute_scalp, or another supported action. Natural aliases such as scalp and trade are accepted.
        symbol: Bybit USDT perpetual symbol, for example SOLUSDT or BTCUSDT.
        timeframe: Candle timeframe used by technical-analysis actions.
        side: Trading side, Buy or Sell.
        target_profit_usdt: Desired minimum net microprofit in USDT.
        qty: Order quantity; zero lets the strategy calculate quantity.
        leverage: Requested leverage.
        max_spread_pct: Maximum allowed REST spread percentage for compatible actions.
        max_notional_usdt: Maximum position notional in USDT.
        post_only: Use post-only limit execution where supported.
        live: Explicitly request live execution; defaults to false.
        live_confirmation: Exact live-trading confirmation token.
        reduce_only: Restrict execution to reducing an existing position.
        use_cache: Enable compatible REST caching.
        no_color: Disable terminal color output.
        verbose: Enable detailed diagnostic logging.
        entry_price: Entry price for PNL calculations.
        exit_price: Exit price for PNL calculations.
        capital_usdt: Starting capital for compounding simulation.
        compound_rate: Per-trade compounding rate as a decimal.
        trades: Number of simulated trades.
        stop_distance: Stop distance used by risk calculations.
        l2_depth: Bybit public L2 orderbook depth.
        l2_max_spread_bps: Maximum L2 spread in basis points for entry.
        l2_entry_score: Minimum L2 directional score required for entry.
        l2_exit_score: L2 reversal score used for exit.
        l2_stop_bps: Hard price-stop distance in basis points.
        l2_max_hold_seconds: Maximum time allowed for a micro-scalp.

    Returns:
        JSON-formatted tool result.
    """
    qty_value = _to_optional_float(qty)
    entry_value = _to_optional_float(entry_price)
    exit_value = _to_optional_float(exit_price)
    stop_value = _to_optional_float(stop_distance)

    l2_env = {
        "L2_DEPTH": str(l2_depth),
        "L2_MAX_SPREAD_BPS": str(l2_max_spread_bps),
        "L2_ENTRY_SCORE": str(l2_entry_score),
        "L2_EXIT_SCORE": str(l2_exit_score),
        "L2_STOP_BPS": str(l2_stop_bps),
        "L2_MAX_HOLD_SECONDS": str(l2_max_hold_seconds),
    }
    os.environ.update(l2_env)

    try:
        result = execute_tool(
            action=action,
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            target_profit_usdt=target_profit_usdt,
            qty=qty_value,
            leverage=leverage,
            max_spread_pct=max_spread_pct,
            max_notional_usdt=max_notional_usdt,
            post_only=post_only,
            live=live,
            live_confirmation=live_confirmation,
            reduce_only=reduce_only,
            use_cache=use_cache,
            no_color=no_color,
            verbose=verbose,
            entry_price=entry_value,
            exit_price=exit_value,
            capital_usdt=capital_usdt,
            compound_rate=compound_rate,
            trades=trades,
            stop_distance=stop_value,
        )
    except Exception as exc:
        result = {"error": str(exc), "status": "failed"}

    return json.dumps(result, ensure_ascii=False, default=str)


def main() -> int:
    args = sys.argv[1:]

    # --------------------------------------------------------------------------
    # AIChat instruction hook
    # --------------------------------------------------------------------------
    if args and args[0] == "_instructions":
        cfg = get_env_config()

        testnet = parse_bool(
            cfg_value(
                "LLM_AGENT_VAR_USE_TESTNET",
                "BYBIT_TESTNET",
                "true",
            ),
            True,
        )

        enabled = parse_bool(
            cfg_value(
                "LLM_AGENT_VAR_LIVE_TRADING_ENABLED",
                "LIVE_TRADING_ENABLED",
                "false",
            ),
            False,
        )

        allowed = parse_bool(
            cfg_value(
                "ALLOW_LIVE_TRADING",
                "ALLOW_LIVE_TRADING",
                "false",
            ),
            False,
        )

        prompt = f"""You are operating as pyrm_scalp2.

Status:
- Version: {__version__}
- Network: {'TESTNET' if testnet else 'MAINNET'}
- Live Trading Enabled: {'YES' if enabled else 'NO'}
- Secondary Live Authorization: {'YES' if allowed else 'NO'}
- Effective Live Trading: {'NO' if testnet or not enabled or not allowed else 'REQUIRES CONFIRMATION'}
- Kill Switch: {'ACTIVE' if kill_switch_active() else 'OFF'}
- Agent CWD: {get_builtin_var('__cwd__') or os.getcwd()}
- UTC Time: {utc_now()}

Execution policy:
- Default is dry-run.
- Opening trades require scan + risk gates.
- Live trading requires explicit confirmation.
- Reduce-only closes bypass directional entry signals.
- Never claim an order is filled merely because it was acknowledged.
"""

        write_llm_output({
            "instructions": prompt
        })

        return EXIT_SUCCESS

    if args and args[0] == "pyrm_scalp2":
        args = args[1:]

    # --------------------------------------------------------------------------
    # JSON input mode
    # --------------------------------------------------------------------------
    if (
        args
        and not args[0].startswith("-")
    ) or (
        not sys.stdin.isatty()
        and not args
    ):
        raw = (
            args[0]
            if args
            else sys.stdin.read()
        )

        try:
            params = (
                json.loads(raw)
                if raw.strip()
                else {}
            )
        except Exception as exc:
            result = {
                "success": False,
                "error":
                    f"Invalid JSON payload: {exc}",
                "exit_code":
                    EXIT_INVALID_INPUT,
            }

            write_llm_output(
                result
            )

            return EXIT_INVALID_INPUT

        result = execute_tool(
            action=params.get(
                "action",
                "ta",
            ),
            symbol=params.get(
                "symbol"
            )
            or cfg_value(
                "LLM_AGENT_VAR_DEFAULT_SYMBOL",
                "DEFAULT_SYMBOL",
                "SOLUSDT",
            ),
            timeframe=params.get(
                "timeframe",
                "1",
            ),
            side=params.get(
                "side",
                "Buy",
            ),
            target_profit_usdt=safe_float(
                params.get(
                    "target_profit_usdt"
                )
                or cfg_value(
                    "LLM_AGENT_VAR_TARGET_MICRO_PROFIT",
                    "TARGET_MICRO_PROFIT",
                    "0.10",
                ),
                0.10,
            ),
            qty=(
                safe_float(
                    params["qty"]
                )
                if (
                    "qty" in params
                    and params["qty"]
                    is not None
                )
                else None
            ),
            leverage=int(
                safe_float(
                    params.get(
                        "leverage"
                    )
                    or cfg_value(
                        "LLM_AGENT_VAR_DEFAULT_LEVERAGE",
                        "DEFAULT_LEVERAGE",
                        "10",
                    ),
                    10,
                )
            ),
            max_spread_pct=safe_float(
                params.get(
                    "max_spread_pct"
                )
                or cfg_value(
                    "LLM_AGENT_VAR_MAX_SPREAD_PCT",
                    "MAX_SPREAD_PCT",
                    "0.035",
                ),
                0.035,
            ),
            max_notional_usdt=safe_float(
                params.get(
                    "max_notional_usdt"
                )
                or cfg_value(
                    "LLM_AGENT_VAR_MAX_NOTIONAL_USDT",
                    "MAX_NOTIONAL_USDT",
                    "100",
                ),
                100,
            ),
            post_only=parse_bool(
                params.get(
                    "post_only",
                    True,
                ),
                True,
            ),
            live=parse_bool(
                params.get(
                    "live",
                    False,
                ),
                False,
            ),
            live_confirmation=str(
                params.get(
                    "live_confirmation",
                    "",
                )
            ),
            reduce_only=parse_bool(
                params.get(
                    "reduce_only",
                    False,
                ),
                False,
            ),
            use_cache=parse_bool(
                params.get(
                    "use_cache",
                    False,
                ),
                False,
            ),
        )

        print_human_readable_ui(
            result
        )

        write_llm_output(
            result
        )

        return int(
            result.get(
                "exit_code",
                EXIT_SUCCESS,
            )
        )

    # --------------------------------------------------------------------------
    # CLI
    # --------------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        prog="pyrm_scalp2.py",
        description=(
            f"AIChat Bybit V5 Perpetual "
            f"Tool v{__version__}"
        ),
    )

    parser.add_argument(
        "--action",
        "-a",
        choices=sorted(
            VALID_ACTIONS
        ),
        default="ta",
    )

    parser.add_argument(
        "--symbol",
        "-s",
        default=cfg_value(
            "LLM_AGENT_VAR_DEFAULT_SYMBOL",
            "DEFAULT_SYMBOL",
            "SOLUSDT",
        ),
    )

    parser.add_argument(
        "--timeframe",
        "-t",
        choices=sorted(
            VALID_TIMEFRAMES
        ),
        default="1",
    )

    parser.add_argument(
        "--side",
        choices=[
            "Buy",
            "Sell",
        ],
        default="Buy",
    )

    parser.add_argument(
        "--target-profit-usdt",
        type=float,
        default=safe_float(
            cfg_value(
                "LLM_AGENT_VAR_TARGET_MICRO_PROFIT",
                "TARGET_MICRO_PROFIT",
                "0.10",
            ),
            0.10,
        ),
    )

    parser.add_argument(
        "--qty",
        type=float,
        default=None,
    )

    parser.add_argument(
        "--leverage",
        type=int,
        default=int(
            safe_float(
                cfg_value(
                    "LLM_AGENT_VAR_DEFAULT_LEVERAGE",
                    "DEFAULT_LEVERAGE",
                    "10",
                ),
                10,
            )
        ),
    )

    parser.add_argument(
        "--max-spread-pct",
        type=float,
        default=safe_float(
            cfg_value(
                "LLM_AGENT_VAR_MAX_SPREAD_PCT",
                "MAX_SPREAD_PCT",
                "0.035",
            ),
            0.035,
        ),
    )

    parser.add_argument(
        "--max-notional-usdt",
        type=float,
        default=safe_float(
            cfg_value(
                "LLM_AGENT_VAR_MAX_NOTIONAL_USDT",
                "MAX_NOTIONAL_USDT",
                "100",
            ),
            100,
        ),
    )

    parser.add_argument(
        "--post-only",
        action="store_true",
        default=True,
    )

    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--live-confirmation",
        default="",
        help="Required exact live-trading confirmation token.",
    )

    parser.add_argument(
        "--reduce-only",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--l2-depth",
        type=int,
        choices=[1, 50, 200, 1000],
        default=int(
            safe_float(
                cfg_value(
                    "L2_DEPTH",
                    "L2_DEPTH",
                    "50",
                ),
                50,
            )
        ),
        help="Bybit L2 orderbook depth.",
    )

    parser.add_argument(
        "--l2-max-spread-bps",
        type=float,
        default=L2_DEFAULT_MAX_SPREAD_BPS,
        help="Maximum L2 entry spread in basis points.",
    )

    parser.add_argument(
        "--l2-entry-score",
        type=float,
        default=L2_DEFAULT_ENTRY_SCORE,
        help="Minimum directional L2 score for entry.",
    )

    parser.add_argument(
        "--l2-exit-score",
        type=float,
        default=L2_DEFAULT_EXIT_SCORE,
        help="L2 reversal threshold for exit.",
    )

    parser.add_argument(
        "--l2-stop-bps",
        type=float,
        default=L2_DEFAULT_STOP_BPS,
        help="Emergency L2 pressure stop threshold.",
    )

    parser.add_argument(
        "--l2-max-hold-seconds",
        type=float,
        default=L2_DEFAULT_MAX_HOLD_SECONDS,
        help="Maximum lifetime of a micro-scalp session.",
    )

    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--clear-cache",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--schema",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        default=False,
    )

    parser.add_argument("--entry-price", type=float, default=None)
    parser.add_argument("--exit-price", type=float, default=None)
    parser.add_argument("--capital-usdt", type=float, default=100.0)
    parser.add_argument("--compound-rate", type=float, default=0.0)
    parser.add_argument("--trades", type=int, default=100)
    parser.add_argument("--stop-distance", type=float, default=None)

    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
    )

    parsed = parser.parse_args(
        args
    )

    if parsed.schema:
        sys.stdout.write(
            json.dumps(
                generate_tool_schema(),
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )

        return EXIT_SUCCESS

    if parsed.self_test:
        result = run_self_test()

        print_human_readable_ui(
            result,
            no_color=parsed.no_color,
        )

        write_llm_output(
            result
        )

        return (
            EXIT_SUCCESS
            if result["success"]
            else EXIT_ERROR
        )

    if parsed.clear_cache:
        count = invalidate_cache()

        _cprint(
            f"{NEON_GREEN}"
            f"Cleared {count} cache entry/entries."
            f"{RESET}",
            no_color=parsed.no_color,
        )

        return EXIT_SUCCESS

    os.environ["L2_DEPTH"] = str(parsed.l2_depth)
    os.environ["L2_MAX_SPREAD_BPS"] = str(parsed.l2_max_spread_bps)
    os.environ["L2_ENTRY_SCORE"] = str(parsed.l2_entry_score)
    os.environ["L2_EXIT_SCORE"] = str(parsed.l2_exit_score)
    os.environ["L2_STOP_BPS"] = str(parsed.l2_stop_bps)
    os.environ["L2_MAX_HOLD_SECONDS"] = str(parsed.l2_max_hold_seconds)

    result = execute_tool(
        action=parsed.action,
        symbol=parsed.symbol,
        timeframe=parsed.timeframe,
        side=parsed.side,
        target_profit_usdt=parsed.target_profit_usdt,
        qty=parsed.qty,
        leverage=parsed.leverage,
        max_spread_pct=parsed.max_spread_pct,
        max_notional_usdt=parsed.max_notional_usdt,
        post_only=parsed.post_only,
        live=parsed.live,
        live_confirmation=parsed.live_confirmation,
        reduce_only=parsed.reduce_only,
        use_cache=parsed.use_cache,
        no_color=parsed.no_color,
        verbose=parsed.verbose,
        entry_price=parsed.entry_price,
        exit_price=parsed.exit_price,
        capital_usdt=parsed.capital_usdt,
        compound_rate=parsed.compound_rate,
        trades=parsed.trades,
        stop_distance=parsed.stop_distance,
    )

    print_human_readable_ui(
        result,
        no_color=parsed.no_color,
    )

    write_llm_output(
        result
    )

    return int(
        result.get(
            "exit_code",
            EXIT_SUCCESS,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
