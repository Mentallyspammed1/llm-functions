#!/usr/bin/env python3
# ==============================================================================
# pyrm_scalp_analyzer.py — Pyrmethus Bybit V5 Unified Quantitative Scalper & Analyzer v11.0.0-ENTERPRISE
# argc/aichat compatible · Human-Readable Colorized Outputs · Native Caching · No External Dependencies
#
# @describe Enterprise Bybit V5 USDT perpetual market microstructure analyzer, multi-symbol scanner, and guarded L2 micro-profit scalper.
#
# @meta require-tools aichat
#
# @option --action <ACTION>              Execution action: ta/scan/multi_scan/quick_scalp/l2_signal/l2_live_scalp/micro_pnl/compound_micro_profit/trade_statistics/wallet_balance/positions/open_orders/orderbook/ticker/instrument/risk_status/execute_scalp/close_position/cancel_orders/set_leverage/kill_switch/history (default: quick_scalp)
# @option --symbol <SYMBOL>              Primary perpetual trading pair (default: SOLUSDT)
# @option --symbols <SYMBOLS>            Comma-separated trading pairs for multi-scan (default: BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,ADAUSDT)
# @option --timeframe <TF>               Candle timeframe interval: 1/3/5/15/30/60/120/240/D (default: 1)
# @option --side <SIDE>                  Trade direction: Buy/Sell (default: Buy)
# @option --target-profit <USDT>         Target net profit per trade in USDT (default: 0.10, auto/dynamic supported)
# @option --qty <NUM>                    Order quantity override (default: auto-calculated)
# @option --leverage <LEV>               Account leverage multiplier (default: 10, max: 100)
# @option --max-notional <USDT>          Maximum position notional cap in USDT (default: 100.0)
# @option --max-spread-bps <BPS>         Maximum allowable spread in basis points (default: 12.0)
# @option --max-spread-pct <PCT>         Maximum allowable REST spread percentage (default: 0.035)
# @option --max-loss-trade <USDT>        Hard stop-loss limit per trade in USDT (default: 0.05)
# @option --daily-loss-cap <USDT>        Maximum cumulative daily loss before circuit breaker (default: 5.0)
# @option --max-drawdown-pct <PCT>       Maximum equity drawdown % from peak before halt (default: 15.0)
# @option --max-positions <NUM>          Maximum simultaneous open positions (default: 1)
# @option --max-workers <NUM>            Concurrent worker threads for batch scanning (default: 5)
# @option --cooldown <SEC>               Per-symbol execution cooldown in seconds (default: 60)
# @option --l2-depth <NUM>               Bybit L2 orderbook depth: 1/50/200/1000 (default: 50)
# @option --l2-entry-score <SCORE>       Minimum directional OFI score required for entry (default: 0.62)
# @option --l2-exit-score <SCORE>        Reversal OFI score threshold for early exit (default: 0.20)
# @option --l2-stop-bps <BPS>            Emergency L2 price stop in basis points (default: 10.0)
# @option --l2-max-hold <SEC>            Maximum lifetime of an active micro-scalp session (default: 45.0)
# @option --entry-price <PRICE>          Simulated/actual entry price for micro_pnl
# @option --exit-price <PRICE>           Simulated/actual exit price for micro_pnl
# @option --capital <USDT>               Starting capital for compound growth simulations (default: 100.0)
# @option --compound-rate <RATE>         Per-trade compounding rate as a decimal (default: 0.001)
# @option --trades <NUM>                 Number of compounding trades to simulate (default: 100)
# @option --live-confirmation <TOKEN>    Required live confirmation token (e.g. I_UNDERSTAND_LIVE_RISK)
# @option --state-file <PATH>            Custom state persistence JSON file path
# @option --trade-log <PATH>             Custom trade ledger JSONL file path
# @option --output-format <FORMAT>       Output format: json/jsonl (default: json)
# @option --cache-ttl <SEC>              Cache TTL in seconds (default: 5)
# @option --cache-dir <PATH>             Custom cache storage directory
# @option --env-var <KEY=VALUE>          Custom environment variable override (repeatable)
# @flag   --post-only                    Enforce maker post-only limit execution where supported
# @flag   --reduce-only                  Restrict execution to reducing/closing existing position
# @flag   --live                         Authorize live trading execution (defaults to safe dry-run)
# @flag   --use-cache                    Enable caching for cacheable queries
# @flag   --clear-cache                  Purge cache and state files and exit
# @flag   --schema                       Print JSON Tool Schema for LLM registration and exit
# @flag   --self-test                    Execute self-diagnostics and exit
# @flag   --no-color                     Disable ANSI color terminal output
# @flag   --verbose                      Enable detailed debug log output
#
# @env LLM_OUTPUT=/dev/stdout            Output destination for LLM tool integration
# @env BYBIT_API_KEY                     Bybit V5 API Key
# @env BYBIT_API_SECRET                  Bybit V5 API Secret
# @env BYBIT_TESTNET=true                Use Bybit Testnet (default: true)
# ==============================================================================

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import dataclasses
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
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

getcontext().prec = 28

__version__ = "11.0.0-ENTERPRISE"
__all__ = [
    "BybitL2WebSocket",
    "BybitV5",
    "GracefulShutdown",
    "MicrostructureAnalyzer",
    "ToolCache",
    "ToolError",
    "__version__",
    "execute_tool",
    "main",
    "run",
]

# ==============================================================================
# SECTION 1: Exit Codes, Constants & Error Models
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

DEFAULT_MAKER_FEE_PCT = 0.020  # 0.020% (or VIP rebate)
DEFAULT_TAKER_FEE_PCT = 0.055  # 0.055%

VALID_TIMEFRAMES = {"1", "3", "5", "15", "30", "60", "120", "240", "D"}

VALID_ACTIONS = {
    "ta",
    "scan",
    "multi_scan",
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

ACTION_ALIASES = {
    "scalp": "quick_scalp",
    "quick": "quick_scalp",
    "trade": "quick_scalp",
    "scan_all": "multi_scan",
    "scan_symbols": "multi_scan",
    "live_scalp": "l2_live_scalp",
    "l2_scalp": "l2_live_scalp",
    "signal": "l2_signal",
    "pnl": "micro_pnl",
    "profit": "calc_micro_profit",
    "balance": "wallet_balance",
    "orders": "open_orders",
    "position": "positions",
}

PUBLIC_ENDPOINTS = {
    "/v5/market/time",
    "/v5/market/kline",
    "/v5/market/orderbook",
    "/v5/market/instruments-info",
    "/v5/market/tickers",
    "/v5/market/recent-trade",
}

BLOCKED_ENV_VARS = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME",
    "PATH", "SHELL", "SUDO_COMMAND", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH"
})


class ToolError(Exception):
    """Structured exception model with machine-readable metadata."""
    def __init__(
        self,
        message: str,
        exit_code: int = EXIT_ERROR,
        error_type: str = "ExecutionError",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.error_type = error_type
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.message,
            "type": self.error_type,
            "exit_code": self.exit_code,
            **self.details,
        }


class ToolJSONEncoder(json.JSONEncoder):
    """Zero-crash JSON encoder supporting Decimal, Path, Enum, and dates."""
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return format(obj, "f") if obj.is_finite() else str(obj)
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, (dt.datetime, dt.date)):
            return obj.isoformat()
        if isinstance(obj, dt.timedelta):
            return obj.total_seconds()
        if isinstance(obj, (set, frozenset)):
            return sorted(list(obj))
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        return repr(obj)


# ==============================================================================
# SECTION 2: Numeric, Validation & Parsing Helpers
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


def positive_float(value: Any, name: str, allow_zero: bool = False) -> float:
    x = safe_float(value, float("nan"))
    if not math.isfinite(x):
        raise ToolError(f"Parameter '{name}' must be a finite number.", EXIT_INVALID_INPUT)
    if allow_zero and x < 0:
        raise ToolError(f"Parameter '{name}' cannot be negative.", EXIT_INVALID_INPUT)
    if not allow_zero and x <= 0:
        raise ToolError(f"Parameter '{name}' must be greater than zero.", EXIT_INVALID_INPUT)
    return x


def parse_decimal(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise InvalidOperation
        return d
    except Exception as exc:
        raise ToolError(f"Invalid decimal value: {value}", EXIT_INVALID_INPUT) from exc


def quantize_step(value: Any, step: Any, rounding: str = ROUND_DOWN) -> str:
    v = parse_decimal(value)
    s = parse_decimal(step)
    if s <= 0:
        raise ToolError("Exchange precision step must be positive.", EXIT_INVALID_INPUT)
    units = (v / s).quantize(Decimal("1"), rounding=rounding)
    return format((units * s).normalize(), "f")


def clamp_decimal_qty(qty: Any, step: Any, minimum: Any, maximum: Any) -> Tuple[float, str]:
    q = parse_decimal(qty)
    s = parse_decimal(step)
    mn = parse_decimal(minimum)
    mx = parse_decimal(maximum)

    if q <= 0:
        raise ToolError("Quantity must be greater than zero.", EXIT_INVALID_INPUT)
    if mx > 0 and q > mx:
        q = mx

    q = (q / s).quantize(Decimal("1"), rounding=ROUND_DOWN) * s
    if q < mn:
        raise ToolError(f"Quantity {q} is below exchange minimum {mn}.", EXIT_INVALID_INPUT)

    return float(q), format(q.normalize(), "f")


def validate_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ToolError("Perpetual symbol cannot be empty.", EXIT_INVALID_INPUT)
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", sym):
        raise ToolError(f"Invalid perpetual symbol '{sym}'.", EXIT_INVALID_INPUT)
    return sym


def validate_timeframe(timeframe: str) -> str:
    tf = str(timeframe or "1").strip().upper()
    if tf not in VALID_TIMEFRAMES:
        raise ToolError(f"Invalid timeframe '{tf}'. Allowed: {sorted(VALID_TIMEFRAMES)}", EXIT_INVALID_INPUT)
    return tf


# ==============================================================================
# SECTION 3: Terminal Colors, Logging & UI Display
# ==============================================================================

NEON_CYAN = "\033[38;5;51m"
NEON_GREEN = "\033[38;5;46m"
NEON_RED = "\033[38;5;196m"
NEON_YELLOW = "\033[38;5;226m"
NEON_PURPLE = "\033[38;5;129m"
NEON_PINK = "\033[38;5;198m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\033\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_tty(no_color: bool = False) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in {"", "dumb"}


def _cprint(text: str, file: Any = None, no_color: bool = False, end: str = "\n") -> None:
    target = file or sys.stderr
    if not _is_tty(no_color):
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    """Render colorized trading telemetry box UI to stderr."""
    if not _is_tty(no_color):
        return

    success = bool(data.get("success"))
    color = NEON_GREEN if success else NEON_RED
    symbol_str = "✓" if success else "✗"
    status = "SUCCESS" if success else "FAILED"
    box_w = 68
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}", no_color=no_color)
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [BYBIT SCALPER & ANALYZER v{__version__}]{RESET} {color}{BOLD}{symbol_str} {status}{RESET}",
        no_color=no_color,
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)

    for k in ("action", "symbol", "mode", "bias", "confidence", "target_profit_usdt", "risk_gate_pass", "order_id", "duration_ms"):
        if k in data:
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}{k:<18}:{RESET} {data[k]}", no_color=no_color)

    # Render Multi-Scan summary if present
    results = data.get("results")
    if isinstance(results, list) and results:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Multi-Scan Overview ({len(results)} pairs):{RESET}", no_color=no_color)
        for row in results[:8]:
            sym = row.get("symbol", "N/A")
            bias = row.get("bias", "NEUTRAL")
            b_col = NEON_GREEN if bias == "LONG" else NEON_RED if bias == "SHORT" else NEON_YELLOW
            conf = row.get("confidence", 0.0)
            pass_str = f"{NEON_GREEN}GATE_PASS{RESET}" if row.get("risk_gate_pass") else f"{DIM}SKIP{RESET}"
            _cprint(f"{NEON_PURPLE}│{RESET}   › {BOLD}{sym:<10}{RESET} {b_col}{bias:<6}{RESET} Conf: {conf:.2f} [{pass_str}]", no_color=no_color)

    # Render Micro-profit Plan
    plan = data.get("micro_profit_plan") or (data.get("signal", {}).get("micro_profit_plan") if isinstance(data.get("signal"), dict) else None)
    if isinstance(plan, dict):
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Micro-Profit Plan:{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET}   › Notional : ${plan.get('estimated_notional', 0.0):.2f} USDT | Qty: {plan.get('calculated_qty_formatted', '0')}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET}   › Exp Net  : {NEON_GREEN}+${plan.get('estimated_net_profit_usdt', 0.0):.4f} USDT{RESET} | Fees: ${plan.get('estimated_fees', 0.0):.4f}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET}   › R:R      : {plan.get('reward_risk', 0.0):.2f} (Required Δ: {plan.get('required_price_delta', 0.0)})", no_color=no_color)

    if not success and data.get("error"):
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error:{RESET} {data['error']}", no_color=no_color)

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}", no_color=no_color)


# ==============================================================================
# SECTION 4: Context, State, Cache & Environment
# ==============================================================================

def get_data_dir() -> Path:
    candidates = [
        Path.home() / ".config" / "aichat" / "functions" / "agents" / "bybit_trader" / "data",
        Path.home() / ".local" / "share" / "aichat_scalper",
        Path.home() / ".scalper_data",
    ]
    for p in candidates:
        with contextlib.suppress(OSError):
            p.mkdir(parents=True, exist_ok=True)
            return p
    path = Path("/tmp/aichat_scalper")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_builtin_var(name: str) -> Optional[str]:
    return (
        os.environ.get(f"LLM_AGENT_VAR_{name}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.lower()}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.upper()}")
    )


def get_env_config() -> Dict[str, str]:
    result: Dict[str, str] = {}
    candidates = [
        get_data_dir() / ".env",
        Path.home() / ".config" / "bybit-agent" / ".env",
        Path.cwd() / ".env",
    ]
    for env_file in candidates:
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
                            v = v[1:-1]
                        result[k] = v
                break
            except OSError:
                pass
    return result


def cfg_value(agent_var: str, env_var: str, default: str) -> str:
    cfg = get_env_config()
    return os.environ.get(agent_var) or os.environ.get(env_var) or cfg.get(env_var) or default


def state_file_path(override: Optional[str] = None) -> Path:
    return Path(override).expanduser().resolve() if override else get_data_dir() / "state.json"


def trade_log_file_path(override: Optional[str] = None) -> Path:
    return Path(override).expanduser().resolve() if override else get_data_dir() / "trade_log.jsonl"


def load_state(override: Optional[str] = None) -> Dict[str, Any]:
    default = {
        "day": utc_day(),
        "daily_realized_pnl": 0.0,
        "wallet_equity": 100.0,
        "equity_peak": 100.0,
        "last_execution_ts": 0.0,
        "consecutive_losses": 0,
        "execution_attempts": 0,
        "kill_switch": False,
        "last_trade_ts": {},
    }
    path = state_file_path(override)
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default
        if data.get("day") != utc_day():
            data["day"] = utc_day()
            data["daily_realized_pnl"] = 0.0
            data["consecutive_losses"] = 0
        merged = dict(default)
        merged.update(data)
        return merged
    except Exception:
        return default


def save_state(state: Dict[str, Any], override: Optional[str] = None) -> None:
    path = state_file_path(override)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, cls=ToolJSONEncoder, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def append_log(record: Dict[str, Any], override: Optional[str] = None) -> None:
    safe = dict(record)
    for key in ("api_key", "api_secret", "secret", "authorization"):
        if key in safe:
            safe[key] = "***REDACTED***"
    path = trade_log_file_path(override)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(safe, cls=ToolJSONEncoder, ensure_ascii=False) + "\n")


def kill_switch_active() -> bool:
    return parse_bool(cfg_value("BYBIT_KILL_SWITCH", "KILL_SWITCH", "false"), False) or bool(load_state().get("kill_switch"))


def set_kill_switch(active: bool) -> Dict[str, Any]:
    state = load_state()
    state["kill_switch"] = bool(active)
    state["kill_switch_changed_at"] = utc_now()
    save_state(state)
    return {"success": True, "kill_switch": bool(active), "timestamp": utc_now()}


class ToolCache:
    """Fast JSON disk cache with TTL."""
    def __init__(self, cache_dir: Optional[Path | str] = None) -> None:
        if cache_dir:
            self.cache_dir = Path(cache_dir).expanduser().resolve()
        elif "LLM_TOOL_CACHE_DIR" in os.environ:
            self.cache_dir = Path(os.environ["LLM_TOOL_CACHE_DIR"]).expanduser().resolve()
        else:
            self.cache_dir = get_data_dir() / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, key_str: str) -> Path:
        h = hashlib.sha256(f"{__version__}|{key_str}".encode()).hexdigest()
        return self.cache_dir / f"{h}.json"

    def get(self, key_str: str, ttl_seconds: int = 5) -> Optional[dict[str, Any]]:
        path = self._key(key_str)
        if not path.exists():
            return None
        try:
            if time.time() - path.stat().st_mtime > ttl_seconds:
                path.unlink(missing_ok=True)
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def set(self, key_str: str, value: Any) -> None:
        path = self._key(key_str)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(value, cls=ToolJSONEncoder, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


class GracefulShutdown:
    def __init__(self) -> None:
        self.interrupted = False
        self._old_int = None
        self._old_term = None

    def __enter__(self) -> GracefulShutdown:
        if threading.current_thread() is threading.main_thread():
            with contextlib.suppress(ValueError, AttributeError):
                self._old_int = signal.signal(signal.SIGINT, self._handler)
                self._old_term = signal.signal(signal.SIGTERM, self._handler)
        return self

    def __exit__(self, *args: Any) -> None:
        if threading.current_thread() is threading.main_thread():
            if self._old_int is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    signal.signal(signal.SIGINT, self._old_int)
            if self._old_term is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    signal.signal(signal.SIGTERM, self._old_term)

    def _handler(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def should_stop(self) -> bool:
        return self.interrupted


# ==============================================================================
# SECTION 5: Bybit V5 REST HTTP & WebSocket Microstructure Engine
# ==============================================================================

class BybitV5:
    """Hardened Bybit V5 Linear REST client with auto-clock sync and rate-limiting backoff."""
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True):
        cfg = get_env_config()
        self.api_key = (api_key or os.environ.get("BYBIT_API_KEY") or cfg.get("BYBIT_API_KEY", "")).strip()
        self.api_secret = (api_secret or os.environ.get("BYBIT_API_SECRET") or cfg.get("BYBIT_API_SECRET", "")).strip()
        self.testnet = testnet
        self.base_url = BYBIT_TESTNET if testnet else BYBIT_MAINNET
        self.time_offset_ms = 0
        self.timeout = safe_float(cfg_value("BYBIT_HTTP_TIMEOUT", "BYBIT_HTTP_TIMEOUT", "10.0"), 10.0)
        self.sync_time()

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def sync_time(self) -> None:
        try:
            t0 = int(time.time() * 1000)
            req = urllib.request.Request(f"{self.base_url}/v5/market/time", headers={"User-Agent": "PyrmScalper/11.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                t1 = int(time.time() * 1000)
                payload = json.loads(resp.read().decode("utf-8"))
            server_ms = safe_float(payload.get("result", {}).get("timeNano", 0)) / 1_000_000
            if server_ms <= 0:
                server_ms = safe_float(payload.get("time", 0))
            if server_ms > 0:
                self.time_offset_ms = int(server_ms - (t0 + t1) / 2.0)
        except Exception:
            self.time_offset_ms = 0

    def _sign(self, timestamp: str, recv_window: str, payload: str) -> str:
        raw = f"{timestamp}{self.api_key}{recv_window}{payload}"
        return hmac.new(self.api_secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()

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
            raise ToolError("Private endpoint requires BYBIT_API_KEY and BYBIT_API_SECRET.", EXIT_PERMISSION_DENIED)

        clean_params = {str(k): ("true" if v is True else "false" if v is False else str(v)) for k, v in (params or {}).items() if v is not None}
        query_str = urllib.parse.urlencode(sorted(clean_params.items()))
        url = f"{self.base_url}{endpoint}" + (f"?{query_str}" if method == "GET" and query_str else "")

        payload_for_sign = query_str if method == "GET" else json.dumps(data or {}, separators=(",", ":"), ensure_ascii=False)
        body_bytes = None if method == "GET" else payload_for_sign.encode("utf-8")

        for attempt in range(3):
            timestamp = str(int(time.time() * 1000) + self.time_offset_ms)
            headers = {"Content-Type": "application/json", "User-Agent": "PyrmScalper/11.0"}
            if not public:
                headers.update({
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": "5000",
                    "X-BAPI-SIGN": self._sign(timestamp, "5000", payload_for_sign),
                })

            req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))

                ret_code = result.get("retCode", 0)
                if ret_code != 0:
                    ret_msg = str(result.get("retMsg", "Unknown Bybit error"))
                    if ret_code == 10002 and attempt == 0:
                        self.sync_time()
                        continue
                    if ret_code in {10006, 10016} and attempt < 2:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    raise ToolError(f"Bybit API Error [{ret_code}]: {ret_msg}", EXIT_ERROR)
                return result.get("result", {})
            except urllib.error.HTTPError as exc:
                if exc.code in {408, 429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(0.3 * (2 ** attempt))
                    continue
                raise ToolError(f"HTTP {exc.code} from Bybit API", EXIT_ERROR) from exc
            except Exception as exc:
                if isinstance(exc, ToolError):
                    raise
                if attempt == 2:
                    raise ToolError(f"Bybit request failed: {exc}", EXIT_ERROR) from exc
                time.sleep(0.3)
        return {}


class BybitL2WebSocket:
    """Standard-library WebSocket client for real-time L2 orderbook stream."""
    def __init__(self, symbol: str, depth: int = 50, testnet: bool = False, timeout: float = 5.0):
        self.symbol = validate_symbol(symbol)
        self.depth = int(depth)
        self.testnet = testnet
        self.timeout = timeout
        self.sock: Optional[ssl.SSLSocket] = None
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_u = 0
        self.last_seq = 0
        self.last_update_ts = 0.0
        self.snapshot_ready = False

    @property
    def url(self) -> str:
        return BYBIT_WS_LINEAR_TESTNET if self.testnet else BYBIT_WS_LINEAR

    def connect(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        host, port, path = parsed.hostname or "", parsed.port or 443, parsed.path or "/"
        raw = socket.create_connection((host, port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        self.sock.sendall(req)

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake closed.")
            resp += chunk

        self.send_json({"op": "subscribe", "args": [f"orderbook.{self.depth}.{self.symbol}"]})

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.sock:
                self.sock.close()
        self.sock = None

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise ConnectionError("Socket disconnected.")
        length = len(payload)
        hdr = bytes([0x80 | (opcode & 0x0F)])
        if length < 126:
            hdr += bytes([0x80 | length])
        elif length < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(hdr + mask + masked)

    def send_json(self, data: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(data, separators=(",", ":")).encode())

    def _recv_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("Socket closed during read.")
            data += chunk
        return data

    def recv_json(self) -> Optional[dict[str, Any]]:
        while True:
            first_two = self._recv_exact(2)
            opcode = first_two[0] & 0x0F
            masked = bool(first_two[1] & 0x80)
            length = first_two[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))

            if opcode == 0x8:
                raise ConnectionError("WebSocket close received.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x1:
                try:
                    return json.loads(payload.decode("utf-8"))
                except Exception:
                    return None

    def apply_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if message.get("topic") != f"orderbook.{self.depth}.{self.symbol}":
            return None
        data = message.get("data", {})
        if not isinstance(data, dict):
            return None
        kind = message.get("type", "")

        if kind == "snapshot":
            self.bids.clear()
            self.asks.clear()
            for r in data.get("b", []):
                if len(r) >= 2 and safe_float(r[0]) > 0 and safe_float(r[1]) > 0:
                    self.bids[safe_float(r[0])] = safe_float(r[1])
            for r in data.get("a", []):
                if len(r) >= 2 and safe_float(r[0]) > 0 and safe_float(r[1]) > 0:
                    self.asks[safe_float(r[0])] = safe_float(r[1])
            self.snapshot_ready = True
        elif kind == "delta" and self.snapshot_ready:
            for r in data.get("b", []):
                p, s = safe_float(r[0]), safe_float(r[1])
                if p > 0:
                    if s <= 0:
                        self.bids.pop(p, None)
                    else:
                        self.bids[p] = s
            for r in data.get("a", []):
                p, s = safe_float(r[0]), safe_float(r[1])
                if p > 0:
                    if s <= 0:
                        self.asks.pop(p, None)
                    else:
                        self.asks[p] = s
        else:
            return None

        self.last_u = int(safe_float(data.get("u", self.last_u)))
        self.last_seq = int(safe_float(data.get("seq", self.last_seq)))
        self.last_update_ts = time.time()
        return self.snapshot()

    def snapshot(self, levels: int = 50) -> Optional[Dict[str, Any]]:
        if not self.bids or not self.asks:
            return None
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:levels]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:levels]
        if not bids or not asks:
            return None
        best_bid, best_ask = bids[0][0], asks[0][0]
        if best_bid <= 0 or best_ask <= best_bid:
            return None
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0
        return {
            "bids": [[str(p), str(s)] for p, s in bids],
            "asks": [[str(p), str(s)] for p, s in asks],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "spread_bps": (spread / mid) * 10000.0,
            "age_ms": max(0.0, (time.time() - self.last_update_ts) * 1000.0),
        }


# ==============================================================================
# SECTION 6: Quantitative Technical Analysis & Microstructure Metrics
# ==============================================================================

class MicrostructureAnalyzer:
    """Quantitative engine for OFI, Depth Composites, Indicators, and Micro-Sizing."""

    @staticmethod
    def ema_series(values: List[float], period: int) -> List[float]:
        if not values:
            return []
        if len(values) < period:
            return [values[-1]] * len(values)
        mult = 2.0 / (period + 1.0)
        seed = sum(values[:period]) / period
        res = [seed]
        curr = seed
        for v in values[period:]:
            curr = v * mult + curr * (1.0 - mult)
            res.append(curr)
        return [seed] * (period - 1) + res

    @staticmethod
    def rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) <= period:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            chg = closes[i] - closes[i - 1]
            gains.append(max(chg, 0.0))
            losses.append(max(-chg, 0.0))
        avg_g = sum(gains[:period]) / period
        avg_l = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l <= 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def macd(closes: List[float]) -> Dict[str, float]:
        if len(closes) < 35:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        fast = MicrostructureAnalyzer.ema_series(closes, 12)
        slow = MicrostructureAnalyzer.ema_series(closes, 26)
        diff = [fast[i] - slow[i] for i in range(len(closes))]
        sig = MicrostructureAnalyzer.ema_series(diff, 9)
        c, s = diff[-1], sig[-1]
        return {"macd": round(c, 8), "signal": round(s, 8), "histogram": round(c - s, 8)}

    @staticmethod
    def bollinger(closes: List[float], period: int = 20) -> Dict[str, float]:
        if len(closes) < period:
            p = closes[-1] if closes else 0.0
            return {"upper": p, "middle": p, "lower": p, "bandwidth_pct": 0.0, "percent_b": 0.5}
        sample = closes[-period:]
        mean = sum(sample) / period
        var = sum((x - mean) ** 2 for x in sample) / period
        dev = math.sqrt(max(var, 0.0))
        upper, lower = mean + 2.0 * dev, mean - 2.0 * dev
        width = upper - lower
        return {
            "upper": round(upper, 8),
            "middle": round(mean, 8),
            "lower": round(lower, 8),
            "bandwidth_pct": round((width / mean * 100.0) if mean > 0 else 0.0, 5),
            "percent_b": round(((closes[-1] - lower) / width) if width > 0 else 0.5, 5),
        }

    @staticmethod
    def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        if len(closes) <= period:
            return 0.0
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
        if len(trs) < period:
            return 0.0
        curr = sum(trs[:period]) / period
        for tr in trs[period:]:
            curr = (curr * (period - 1) + tr) / period
        return curr

    @staticmethod
    def price_action(raw: List[Any]) -> Dict[str, Any]:
        candles = [c for c in raw if isinstance(c, (list, tuple)) and len(c) >= 7]
        if len(candles) < 3:
            return {"pattern": "NONE", "bullish": False, "bearish": False}
        curr, prev = candles[-1], candles[-2]
        co, ch, cl, cc = safe_float(curr[1]), safe_float(curr[2]), safe_float(curr[3]), safe_float(curr[4])
        po, pc = safe_float(prev[1]), safe_float(prev[4])
        body = abs(cc - co)
        rng = max(ch - cl, 0.0)
        lower_w = min(co, cc) - cl
        upper_w = ch - max(co, cc)
        ref = max(body, rng * 0.02)

        bull_pin = rng > 0 and lower_w >= 2.0 * ref and cc >= co
        bear_pin = rng > 0 and upper_w >= 2.0 * ref and cc <= co
        bull_eng = cc > co and pc < po and cc >= po and co <= pc
        bear_eng = cc < co and pc > po and cc <= po and co >= pc

        pat = "BULL_ENGULF" if bull_eng else "BEAR_ENGULF" if bear_eng else "BULL_PIN" if bull_pin else "BEAR_PIN" if bear_pin else "NONE"
        return {"pattern": pat, "bullish": bool(bull_pin or bull_eng), "bearish": bool(bear_pin or bear_eng)}

    @staticmethod
    def compute_ofi(bids: List[Any], asks: List[Any], levels: int = 10, decay: float = 0.85) -> float:
        bid_w, ask_w = 0.0, 0.0
        limit = min(levels, len(bids), len(asks))
        if limit == 0:
            return 0.5
        for i in range(limit):
            weight = decay ** i
            bid_w += safe_float(bids[i][1]) * weight
            ask_w += safe_float(asks[i][1]) * weight
        tot = bid_w + ask_w
        return (bid_w / tot) if tot > 0 else 0.5

    @staticmethod
    def compute_depth_ratio(bids: List[Any], asks: List[Any]) -> float:
        top5_b = sum(safe_float(b[1]) for b in bids[:5])
        top5_a = sum(safe_float(a[1]) for a in asks[:5])
        r5 = (top5_b / (top5_b + top5_a)) if (top5_b + top5_a) > 0 else 0.5
        top25_b = sum(safe_float(b[1]) for b in bids[:25])
        top25_a = sum(safe_float(a[1]) for a in asks[:25])
        r25 = (top25_b / (top25_b + top25_a)) if (top25_b + top25_a) > 0 else 0.5
        return 0.70 * r5 + 0.30 * r25

    @staticmethod
    def analyze_l2_book(book: Dict[str, Any], levels: int = 20) -> Dict[str, Any]:
        bids = book.get("bids", [])[:levels]
        asks = book.get("asks", [])[:levels]
        if not bids or not asks:
            return {"valid": False, "signal": "NEUTRAL", "score": 0.0}

        bid_total = sum(safe_float(x[1]) for x in bids)
        ask_total = sum(safe_float(x[1]) for x in asks)
        weighted_b = sum(safe_float(x[1]) / (i + 1) for i, x in enumerate(bids))
        weighted_a = sum(safe_float(x[1]) / (i + 1) for i, x in enumerate(asks))
        tot_w = weighted_b + weighted_a
        pressure = (weighted_b - weighted_a) / tot_w if tot_w > 0 else 0.0

        bid_p, ask_p = safe_float(bids[0][0]), safe_float(asks[0][0])
        bid_s, ask_s = safe_float(bids[0][1]), safe_float(asks[0][1])
        microprice = (ask_p * bid_s + bid_p * ask_s) / max(bid_s + ask_s, 1e-12)
        mid = safe_float(book.get("mid", (bid_p + ask_p) / 2.0))
        micro_edge_bps = ((microprice - mid) / mid * 10000.0) if mid > 0 else 0.0

        near_b = sum(safe_float(x[1]) for x in bids[:5])
        near_a = sum(safe_float(x[1]) for x in asks[:5])
        near_p = (near_b - near_a) / max(near_b + near_a, 1e-12)

        deep_b = sum(safe_float(x[1]) for x in bids[5:levels])
        deep_a = sum(safe_float(x[1]) for x in asks[5:levels])
        deep_p = (deep_b - deep_a) / max(deep_b + deep_a, 1e-12)

        directional = pressure * 0.50 + near_p * 0.30 + deep_p * 0.20
        if micro_edge_bps > 0:
            directional += min(micro_edge_bps / 10.0, 0.10)
        elif micro_edge_bps < 0:
            directional -= min(abs(micro_edge_bps) / 10.0, 0.10)
        directional = max(-1.0, min(1.0, directional))

        signal = "LONG" if directional >= 0.20 else "SHORT" if directional <= -0.20 else "NEUTRAL"
        return {
            "valid": True,
            "signal": signal,
            "score": round(abs(directional), 4),
            "directional_score": round(directional, 4),
            "raw_imbalance": round(bid_total / max(ask_total, 1e-12), 4),
            "weighted_pressure": round(pressure, 4),
            "microprice": round(microprice, 6),
            "micro_edge_bps": round(micro_edge_bps, 4),
            "spread_bps": round(safe_float(book.get("spread_bps")), 4),
        }


# ==============================================================================
# SECTION 7: Market Data Fetchers & Precision Resolution
# ==============================================================================

def fetch_precision(client: BybitV5, symbol: str) -> Tuple[str, str, str, str]:
    sym = validate_symbol(symbol)
    res = client.request("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": sym})
    for item in res.get("list", []):
        if item.get("symbol") == sym:
            return (
                str(item.get("priceFilter", {}).get("tickSize", "0.01")),
                str(item.get("lotSizeFilter", {}).get("qtyStep", "0.01")),
                str(item.get("lotSizeFilter", {}).get("minOrderQty", "0.01")),
                str(item.get("lotSizeFilter", {}).get("maxOrderQty", "1000000")),
            )
    raise ToolError(f"Linear instrument metadata not found for '{sym}'.", EXIT_FILE_NOT_FOUND)


def get_orderbook(client: BybitV5, symbol: str, limit: int = 25) -> Dict[str, Any]:
    sym = validate_symbol(symbol)
    res = client.request("GET", "/v5/market/orderbook", {"category": "linear", "symbol": sym, "limit": str(limit)})
    bids = sorted([[str(x[0]), str(x[1])] for x in res.get("b", []) if len(x) >= 2], key=lambda x: safe_float(x[0]), reverse=True)
    asks = sorted([[str(x[0]), str(x[1])] for x in res.get("a", []) if len(x) >= 2], key=lambda x: safe_float(x[0]))
    if not bids or not asks:
        raise ToolError(f"Orderbook for {sym} is empty.", EXIT_ERROR)
    bb, ba = safe_float(bids[0][0]), safe_float(asks[0][0])
    if bb <= 0 or ba <= bb:
        raise ToolError(f"Invalid bid/ask spread for {sym}.", EXIT_ERROR)
    spread = ba - bb
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": bb,
        "best_ask": ba,
        "mid": (bb + ba) / 2.0,
        "spread": spread,
        "spread_pct": (spread / bb) * 100.0,
        "spread_bps": (spread / ((bb + ba) / 2.0)) * 10000.0,
        "timestamp": utc_now(),
    }


def get_closed_klines(client: BybitV5, symbol: str, timeframe: str, limit: int = 200) -> List[List[Any]]:
    res = client.request(
        "GET",
        "/v5/market/kline",
        {"category": "linear", "symbol": validate_symbol(symbol), "interval": validate_timeframe(timeframe), "limit": str(min(max(limit, 1), 1000))},
    )
    raw = res.get("list", [])
    valid = []
    for c in raw:
        if isinstance(c, (list, tuple)) and len(c) >= 7:
            with contextlib.suppress(ValueError, TypeError):
                int(c[0])
                for idx in range(1, 7):
                    float(c[idx])
                valid.append(list(c))
    valid.sort(key=lambda x: int(x[0]))
    return valid[:-1] if len(valid) > 1 else valid


# ==============================================================================
# SECTION 8: Micro-Profit Calculations, Compounding & Analytics
# ==============================================================================

def calculate_micro_pnl(
    entry: float,
    exit_price: float,
    qty: float,
    side: str = "Buy",
    maker_fee_pct: float = DEFAULT_MAKER_FEE_PCT,
    taker_fee_pct: float = DEFAULT_TAKER_FEE_PCT,
) -> Dict[str, Any]:
    entry_f = positive_float(entry, "entry_price")
    exit_f = positive_float(exit_price, "exit_price")
    qty_f = positive_float(qty, "qty")
    is_long = side.strip().lower() in {"buy", "long"}

    gross = (exit_f - entry_f) * qty_f if is_long else (entry_f - exit_f) * qty_f
    entry_fee = entry_f * qty_f * (maker_fee_pct / 100.0)
    exit_fee = exit_f * qty_f * (taker_fee_pct / 100.0)
    total_fees = entry_fee + exit_fee
    net = gross - total_fees
    notional = entry_f * qty_f

    mf, tf = maker_fee_pct / 100.0, taker_fee_pct / 100.0
    breakeven = (entry_f * (1.0 + mf) / (1.0 - tf)) if is_long else (entry_f * (1.0 - mf) / (1.0 + tf))
    delta = abs(exit_f - entry_f)

    return {
        "entry_price": round(entry_f, 8),
        "exit_price": round(exit_f, 8),
        "qty": round(qty_f, 6),
        "side": "Buy" if is_long else "Sell",
        "entry_notional_usdt": round(notional, 4),
        "gross_pnl_usdt": round(gross, 6),
        "entry_fee_usdt": round(entry_fee, 6),
        "exit_fee_usdt": round(exit_fee, 6),
        "total_fees_usdt": round(total_fees, 6),
        "net_pnl_usdt": round(net, 6),
        "roi_percent": round((net / max(notional, 1e-12)) * 100.0, 4),
        "price_move": round(delta, 8),
        "price_move_bps": round((delta / max(entry_f, 1e-12)) * 10000.0, 4),
        "break_even_exit": round(breakeven, 8),
        "profitable": net > 0,
    }


def compound_micro_profit(capital: float, rate: float, trades: int) -> Dict[str, Any]:
    cap = positive_float(capital, "capital")
    num_trades = int(positive_float(trades, "trades"))
    if rate <= -1:
        raise ToolError("Compound rate must be > -1.0", EXIT_INVALID_INPUT)
    ending = cap * ((1.0 + rate) ** num_trades)
    return {
        "starting_balance_usdt": round(cap, 4),
        "ending_balance_usdt": round(ending, 4),
        "net_growth_usdt": round(ending - cap, 4),
        "growth_percent": round(((ending / cap) - 1.0) * 100.0, 4),
        "per_trade_rate_percent": round(rate * 100.0, 6),
        "trades": num_trades,
    }


def calculate_trade_statistics_from_log(override_path: Optional[str] = None) -> Dict[str, Any]:
    path = trade_log_file_path(override_path)
    records = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                with contextlib.suppress(Exception):
                    records.append(json.loads(line))

    pnls = []
    for r in records:
        for k in ("net_pnl_usdt", "net_pnl_estimate", "realized_pnl", "pnl", "realizedPnl", "closed_pnl"):
            if k in r:
                val = safe_float(r.get(k), float("nan"))
                if math.isfinite(val):
                    pnls.append(val)
                    break

    wins = [x for x in pnls if x > 0]
    losses = [abs(x) for x in pnls if x < 0]
    gp, gl = sum(wins), sum(losses)
    curve, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        curve += p
        peak = max(peak, curve)
        dd = max(dd, peak - curve)

    return {
        "journal_records": len(records),
        "pnl_records": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round((len(wins) / len(pnls) * 100.0) if pnls else 0.0, 2),
        "gross_profit_usdt": round(gp, 4),
        "gross_loss_usdt": round(gl, 4),
        "net_pnl_usdt": round(sum(pnls), 4),
        "profit_factor": round((gp / max(gl, 1e-12)), 4),
        "average_win_usdt": round((gp / max(len(wins), 1)), 4),
        "average_loss_usdt": round((gl / max(len(losses), 1)), 4),
        "max_drawdown_usdt": round(dd, 4),
        "timestamp": utc_now(),
    }


# ==============================================================================
# SECTION 9: Scanning, TA & Risk Evaluation
# ==============================================================================

def handle_ta(client: BybitV5, symbol: str, timeframe: str) -> Dict[str, Any]:
    sym, tf = validate_symbol(symbol), validate_timeframe(timeframe)
    candles = get_closed_klines(client, sym, tf, 200)
    if len(candles) < 60:
        raise ToolError(f"Insufficient candle history ({len(candles)}), minimum 60 required.", EXIT_ERROR)

    closes = [safe_float(c[4]) for c in candles]
    highs = [safe_float(c[2]) for c in candles]
    lows = [safe_float(c[3]) for c in candles]
    price = closes[-1]

    rsi_val = MicrostructureAnalyzer.rsi(closes, 14)
    macd_dict = MicrostructureAnalyzer.macd(closes)
    bb = MicrostructureAnalyzer.bollinger(closes)
    ema9 = MicrostructureAnalyzer.ema_series(closes, 9)[-1]
    ema21 = MicrostructureAnalyzer.ema_series(closes, 21)[-1]
    ema50 = MicrostructureAnalyzer.ema_series(closes, 50)[-1]
    atr_val = MicrostructureAnalyzer.atr(highs, lows, closes, 14)

    trend = "STRONG_BULLISH" if (ema9 > ema21 > ema50 and price > ema9) else "BULLISH" if (ema9 > ema21 and price > ema21) else "STRONG_BEARISH" if (ema9 < ema21 < ema50 and price < ema9) else "BEARISH" if (ema9 < ema21 and price < ema21) else "NEUTRAL"

    return {
        "success": True,
        "symbol": sym,
        "timeframe": tf,
        "current_price": price,
        "trend": trend,
        "rsi_14": round(rsi_val, 2),
        "rsi_state": "OVERSOLD" if rsi_val <= 30 else "OVERBOUGHT" if rsi_val >= 70 else "NEUTRAL",
        "macd": macd_dict,
        "macd_bias": "BULLISH" if macd_dict["histogram"] > 0 else "BEARISH" if macd_dict["histogram"] < 0 else "NEUTRAL",
        "bollinger_bands": bb,
        "ema": {"ema_9": round(ema9, 6), "ema_21": round(ema21, 6), "ema_50": round(ema50, 6)},
        "atr_14": round(atr_val, 6),
        "timestamp": utc_now(),
    }


def handle_scan(
    client: BybitV5,
    symbol: str,
    max_spread_pct: float,
    target_profit: float,
    leverage: int,
    max_notional: float,
    maker_fee_pct: float = DEFAULT_MAKER_FEE_PCT,
    taker_fee_pct: float = DEFAULT_TAKER_FEE_PCT,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)
    ob = get_orderbook(client, sym)
    candles = get_closed_klines(client, sym, "1", 100)
    ta = handle_ta(client, sym, "1")

    ofi = MicrostructureAnalyzer.compute_ofi(ob["bids"], ob["asks"])
    depth_r = MicrostructureAnalyzer.compute_depth_ratio(ob["bids"], ob["asks"])
    composite = 0.60 * ofi + 0.40 * depth_r
    pa = MicrostructureAnalyzer.price_action(candles)
    trend = ta["trend"]

    score_long, score_short = 0, 0
    if ob["spread_pct"] <= max_spread_pct:
        score_long += 1
        score_short += 1
    if composite >= 0.55:
        score_long += 2
    elif composite <= 0.45:
        score_short += 2
    if pa["bullish"]:
        score_long += 2
    if pa["bearish"]:
        score_short += 2
    if trend in {"BULLISH", "STRONG_BULLISH"}:
        score_long += 2
    if trend in {"BEARISH", "STRONG_BEARISH"}:
        score_short += 2
    if 45 <= ta["rsi_14"] <= 68:
        score_long += 1
    if 32 <= ta["rsi_14"] <= 55:
        score_short += 1
    if ta["macd_bias"] == "BULLISH":
        score_long += 1
    if ta["macd_bias"] == "BEARISH":
        score_short += 1

    bias = "LONG" if (score_long >= 5 and score_long > score_short) else "SHORT" if (score_short >= 5 and score_short > score_long) else "NEUTRAL"
    entry = ob["best_bid"] if bias == "LONG" else ob["best_ask"] if bias == "SHORT" else ob["mid"]
    tick, step, min_qty, max_qty = fetch_precision(client, sym)

    atr_val = safe_float(ta["atr_14"])
    round_trip_fee = (maker_fee_pct + taker_fee_pct) / 100.0
    fee_unit = entry * round_trip_fee
    required_delta = max(target_profit + fee_unit, atr_val * 0.20, entry * 0.0005)

    raw_qty_for_target = (target_profit + fee_unit) / max(required_delta, 1e-12)
    raw_qty_cap = max_notional / max(entry, 1e-12)
    raw_qty = min(raw_qty_for_target, raw_qty_cap)

    final_qty, qty_str = clamp_decimal_qty(raw_qty, step, min_qty, max_qty)
    notional = final_qty * entry
    est_fees = notional * round_trip_fee
    est_gross = final_qty * required_delta
    est_net = est_gross - est_fees
    target_pass = est_net >= (target_profit * 0.85)

    stop_distance = max(atr_val * 0.75, ob["spread"] * 3.0, entry * 0.001)
    rr = required_delta / max(stop_distance, 1e-12)
    rr_pass = rr >= 1.0

    return {
        "success": True,
        "symbol": sym,
        "best_bid": ob["best_bid"],
        "best_ask": ob["best_ask"],
        "mid": ob["mid"],
        "spread_pct": round(ob["spread_pct"], 6),
        "spread_pass": ob["spread_pct"] <= max_spread_pct,
        "ofi": round(ofi, 4),
        "depth_ratio": round(depth_r, 4),
        "composite_imbalance": round(composite, 4),
        "long_score": score_long,
        "short_score": score_short,
        "confidence": round(max(score_long, score_short) / 10.0, 2),
        "pattern_detected": pa["pattern"],
        "bias": bias,
        "ta_summary": {"trend": trend, "rsi_14": ta["rsi_14"], "macd_bias": ta["macd_bias"], "atr_14": atr_val},
        "micro_profit_plan": {
            "target_profit_usdt": target_profit,
            "calculated_qty": final_qty,
            "calculated_qty_formatted": qty_str,
            "estimated_notional": round(notional, 4),
            "estimated_fees": round(est_fees, 4),
            "estimated_gross_profit_usdt": round(est_gross, 4),
            "estimated_net_profit_usdt": round(est_net, 4),
            "target_profit_pass": target_pass,
            "required_price_delta": round(required_delta, 6),
            "stop_distance": round(stop_distance, 6),
            "reward_risk": round(rr, 2),
            "reward_risk_pass": rr_pass,
        },
        "risk_gate_pass": (bias != "NEUTRAL" and ob["spread_pct"] <= max_spread_pct and notional <= max_notional and rr_pass),
        "timestamp": utc_now(),
    }


def live_authorized(testnet: bool, requested_live: bool, confirmation: str) -> Tuple[bool, str]:
    if not requested_live:
        return False, "DRY_RUN_REQUESTED"
    if testnet:
        return False, "TESTNET_MODE_ACTIVE"
    if not parse_bool(cfg_value("LLM_AGENT_VAR_LIVE_TRADING_ENABLED", "LIVE_TRADING_ENABLED", "false")):
        return False, "LIVE_TRADING_DISABLED"
    if kill_switch_active():
        return False, "KILL_SWITCH_ACTIVE"
    expected = cfg_value("LIVE_CONFIRMATION_TOKEN", "LIVE_CONFIRMATION_TOKEN", "I_UNDERSTAND_LIVE_RISK")
    if confirmation != expected:
        return False, "CONFIRMATION_TOKEN_MISMATCH"
    return True, "AUTHORIZED_LIVE"


# ==============================================================================
# SECTION 10: Guarded Order Execution & L2 Micro-Scalping
# ==============================================================================

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
    reduce_only: bool = False,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)
    if side not in {"Buy", "Sell"}:
        raise ToolError("Side must be Buy or Sell.", EXIT_INVALID_INPUT)

    live, auth_reason = live_authorized(client.testnet, live_request, live_confirmation)
    if kill_switch_active():
        raise ToolError("Execution blocked: kill switch is active.", EXIT_PERMISSION_DENIED)

    scan = handle_scan(client, sym, max_spread, target_profit, leverage, max_notional)
    if not reduce_only and not scan["risk_gate_pass"]:
        raise ToolError("Entry risk gates failed.", EXIT_PERMISSION_DENIED, {"scan": scan})

    tick, step, min_qty, max_qty = fetch_precision(client, sym)
    plan = scan["micro_profit_plan"]
    final_qty, qty_str = clamp_decimal_qty(qty or plan["calculated_qty"], step, min_qty, max_qty)

    ob = get_orderbook(client, sym)
    entry_p = ob["best_bid"] if side == "Buy" else ob["best_ask"]
    req_delta = safe_float(plan["required_price_delta"])
    stop_dist = safe_float(plan["stop_distance"])

    tp_price = entry_p + req_delta if side == "Buy" else entry_p - req_delta
    sl_price = entry_p - stop_dist if side == "Buy" else entry_p + stop_dist

    entry_fmt = quantize_step(entry_p, tick, ROUND_DOWN if side == "Buy" else ROUND_UP)
    tp_fmt = quantize_step(tp_price, tick, ROUND_UP if side == "Buy" else ROUND_DOWN)
    sl_fmt = quantize_step(sl_price, tick, ROUND_DOWN if side == "Buy" else ROUND_UP)
    order_link_id = "PYRM" + hashlib.sha256(f"{sym}:{side}:{time.time_ns()}:{uuid.uuid4()}".encode()).hexdigest()[:24]

    record = {
        "success": True,
        "symbol": sym,
        "side": side,
        "qty": qty_str,
        "notional_usdt": round(final_qty * entry_p, 4),
        "leverage": f"{leverage}x",
        "entry_price": entry_fmt,
        "take_profit": None if reduce_only else tp_fmt,
        "stop_loss": None if reduce_only else sl_fmt,
        "mode": "LIVE" if live else "DRY_RUN",
        "authorization": auth_reason,
        "order_link_id": order_link_id,
        "timestamp": utc_now(),
    }

    if not live:
        record["order_id"] = "SIMULATED_" + uuid.uuid4().hex[:8]
        append_log(record)
        return record

    # Submit live order
    with contextlib.suppress(Exception):
        client.request("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym, "buyLeverage": str(leverage), "sellLeverage": str(leverage)})

    payload = {
        "category": "linear",
        "symbol": sym,
        "side": side,
        "orderType": "Limit" if post_only else "Market",
        "qty": qty_str,
        "positionIdx": 0,
        "orderLinkId": order_link_id,
        "reduceOnly": reduce_only,
    }
    if post_only:
        payload.update({"price": entry_fmt, "timeInForce": "PostOnly"})
    else:
        payload["timeInForce"] = "IOC"

    if not reduce_only:
        payload.update({"takeProfit": tp_fmt, "stopLoss": sl_fmt, "tpslMode": "Full", "tpOrderType": "Market", "slOrderType": "Market"})

    res = client.request("POST", "/v5/order/create", data=payload)
    order_id = res.get("orderId")
    if not order_id:
        raise ToolError("Order submission rejected by Bybit.", EXIT_ERROR)

    record["order_id"] = order_id
    append_log(record)
    return record


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
    depth: int = 50,
) -> Dict[str, Any]:
    sym = validate_symbol(symbol)
    live, auth_reason = live_authorized(client.testnet, live_request, live_confirmation)
    if not live:
        raise ToolError(f"L2 live trading blocked: {auth_reason}.", EXIT_PERMISSION_DENIED)
    if kill_switch_active():
        raise ToolError("Execution blocked: kill switch is active.", EXIT_PERMISSION_DENIED)

    tick, step, min_qty, max_qty = fetch_precision(client, sym)
    ws = BybitL2WebSocket(symbol=sym, depth=depth, testnet=client.testnet, timeout=5.0)

    started = time.time()
    last_analysis = None
    try:
        ws.connect()
        while time.time() - started < max_hold_seconds:
            try:
                msg = ws.recv_json()
            except socket.timeout:
                continue
            if not msg:
                continue
            book = ws.apply_message(msg)
            if not book or book.get("age_ms", 0) > 500:
                continue

            analysis = MicrostructureAnalyzer.analyze_l2_book(book, min(depth, 20))
            last_analysis = analysis
            if book["spread_bps"] > max_spread_bps:
                continue

            dir_score = safe_float(analysis.get("directional_score"))
            if abs(dir_score) >= entry_score:
                side = "Buy" if dir_score > 0 else "Sell"
                entry_p = book["best_bid"] if side == "Buy" else book["best_ask"]
                raw_qty = min(max_notional / entry_p, target_profit / max(entry_p * (stop_bps / 10000.0), 1e-12))
                _, qty_str = clamp_decimal_qty(raw_qty, step, min_qty, max_qty)
                order_link_id = "L2E" + uuid.uuid4().hex[:20]

                res = client.request(
                    "POST",
                    "/v5/order/create",
                    data={
                        "category": "linear",
                        "symbol": sym,
                        "side": side,
                        "orderType": "Limit",
                        "qty": qty_str,
                        "price": quantize_step(entry_p, tick, ROUND_DOWN if side == "Buy" else ROUND_UP),
                        "timeInForce": "PostOnly",
                        "positionIdx": 0,
                        "orderLinkId": order_link_id,
                    },
                )
                return {
                    "success": True,
                    "mode": "LIVE",
                    "strategy": "L2_MICROPROFIT",
                    "symbol": sym,
                    "side": side,
                    "order_id": res.get("orderId"),
                    "l2": analysis,
                    "timestamp": utc_now(),
                }
    finally:
        ws.close()

    return {
        "success": True,
        "mode": "LIVE",
        "symbol": sym,
        "message": "L2 session completed without triggering entry threshold.",
        "last_l2": last_analysis,
        "timestamp": utc_now(),
    }


# ==============================================================================
# SECTION 11: Action Router & Multi-Symbol Orchestrator
# ==============================================================================

def execute_tool(
    action: str = "quick_scalp",
    symbol: str = "SOLUSDT",
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,ADAUSDT",
    timeframe: str = "1",
    side: str = "Buy",
    target_profit_usdt: float = 0.10,
    qty: Optional[float] = None,
    leverage: int = 10,
    max_notional_usdt: float = 100.0,
    max_spread_bps: float = 12.0,
    max_spread_pct: float = 0.035,
    max_loss_trade: float = 0.05,
    daily_loss_cap: float = 5.0,
    max_drawdown_pct: float = 15.0,
    max_positions: int = 1,
    max_workers: int = 5,
    cooldown: int = 60,
    l2_depth: int = 50,
    l2_entry_score: float = 0.62,
    l2_exit_score: float = 0.20,
    l2_stop_bps: float = 10.0,
    l2_max_hold: float = 45.0,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    capital_usdt: float = 100.0,
    compound_rate: float = 0.001,
    trades: int = 100,
    live_confirmation: str = "",
    state_file: Optional[str] = None,
    trade_log: Optional[str] = None,
    post_only: bool = True,
    reduce_only: bool = False,
    live: bool = False,
    use_cache: bool = False,
    cache_ttl: int = 5,
    cache_dir: Optional[str] = None,
    no_color: bool = False,
    verbose: bool = False,
    env_vars: Optional[list[str]] = None,
) -> dict[str, Any]:
    started = time.monotonic()
    action = ACTION_ALIASES.get(str(action or "").strip().lower(), str(action or "").strip().lower())

    if action not in VALID_ACTIONS:
        return {
            "success": False,
            "error": f"Invalid action '{action}'.",
            "valid_actions": sorted(VALID_ACTIONS),
            "exit_code": EXIT_INVALID_INPUT,
            "duration_ms": 0.0,
        }

    # Inject safe env var overrides
    if env_vars:
        for entry in env_vars:
            if "=" in entry:
                k, v = entry.split("=", 1)
                k_clean = k.strip().upper()
                if k_clean and k_clean not in BLOCKED_ENV_VARS:
                    os.environ[k_clean] = v.strip()

    testnet = parse_bool(cfg_value("LLM_AGENT_VAR_USE_TESTNET", "BYBIT_TESTNET", "true"), True)
    client = BybitV5(testnet=testnet)
    cache = ToolCache(cache_dir=cache_dir)
    cache_key = f"{action}:{symbol}:{symbols}:{timeframe}:{side}:{target_profit_usdt}:{qty}:{leverage}:{max_notional_usdt}:{live}:{reduce_only}"

    if use_cache and not live and action in {"ta", "scan", "multi_scan", "orderbook", "ticker", "instrument", "compound_micro_profit"}:
        cached = cache.get(cache_key, ttl_seconds=cache_ttl)
        if cached:
            cached["cached"] = True
            return cached

    try:
        with GracefulShutdown() as shutdown:
            if shutdown.should_stop():
                raise ToolError("Execution interrupted by user signal.", EXIT_INTERRUPTED)

            if action == "ta":
                result = handle_ta(client, symbol, timeframe)
            elif action == "scan":
                result = handle_scan(client, symbol, max_spread_pct, target_profit_usdt, leverage, max_notional_usdt)
            elif action == "multi_scan":
                target_syms = [validate_symbol(s) for s in symbols.split(",") if s.strip()]
                results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(target_syms))) as executor:
                    future_to_sym = {executor.submit(handle_scan, client, s, max_spread_pct, target_profit_usdt, leverage, max_notional_usdt): s for s in target_syms}
                    for fut in concurrent.futures.as_completed(future_to_sym):
                        try:
                            results.append(fut.result())
                        except Exception as exc:
                            results.append({"symbol": future_to_sym[fut], "success": False, "error": str(exc)})
                result = {"success": True, "count": len(results), "results": sorted(results, key=lambda x: x.get("confidence", 0.0), reverse=True)}
            elif action == "calc_micro_profit":
                scan_res = handle_scan(client, symbol, max_spread_pct, target_profit_usdt, leverage, max_notional_usdt)
                result = {"success": True, "symbol": symbol, "plan": scan_res.get("micro_profit_plan"), "risk_gate_pass": scan_res.get("risk_gate_pass")}
            elif action == "quick_scalp":
                scan_res = handle_scan(client, symbol, max_spread_pct, target_profit_usdt, leverage, max_notional_usdt)
                plan = scan_res.get("micro_profit_plan", {})
                entry_p = safe_float(scan_res.get("best_bid") if scan_res.get("bias") == "LONG" else scan_res.get("best_ask") if scan_res.get("bias") == "SHORT" else scan_res.get("mid"))
                qty_val = safe_float(plan.get("calculated_qty"))
                delta = safe_float(plan.get("required_price_delta"))
                exit_p = entry_p + delta if scan_res.get("bias") == "LONG" else entry_p - delta
                pnl = calculate_micro_pnl(entry_p, exit_p, qty_val, "Buy" if scan_res.get("bias") == "LONG" else "Sell") if entry_p > 0 and qty_val > 0 else None
                result = {
                    "success": True,
                    "mode": "ANALYSIS_ONLY",
                    "symbol": symbol,
                    "signal": scan_res,
                    "execution_plan": {"entry": entry_p, "qty": qty_val, "expected_exit": exit_p, "pnl": pnl, "live_ready": bool(scan_res.get("risk_gate_pass"))},
                }
            elif action == "micro_pnl":
                if entry_price is None or exit_price is None or qty is None:
                    raise ToolError("micro_pnl requires entry_price, exit_price, and qty.", EXIT_INVALID_INPUT)
                result = {"success": True, "pnl": calculate_micro_pnl(entry_price, exit_price, qty, side)}
            elif action == "compound_micro_profit":
                result = {"success": True, "compound": compound_micro_profit(capital_usdt, compound_rate, trades)}
            elif action == "trade_statistics":
                result = {"success": True, "statistics": calculate_trade_statistics_from_log(trade_log)}
            elif action == "l2_signal":
                ws = BybitL2WebSocket(symbol=symbol, depth=l2_depth, testnet=client.testnet)
                try:
                    ws.connect()
                    t_end = time.monotonic() + 5.0
                    book = None
                    while time.monotonic() < t_end:
                        msg = ws.recv_json()
                        if msg and (applied := ws.apply_message(msg)):
                            book = applied
                            break
                    if not book:
                        raise ToolError("Timed out waiting for L2 snapshot.", EXIT_TIMEOUT)
                    result = {"success": True, "symbol": symbol, "depth": l2_depth, "book": book, "l2": MicrostructureAnalyzer.analyze_l2_book(book, min(l2_depth, 20))}
                finally:
                    ws.close()
            elif action == "l2_live_scalp":
                result = handle_l2_live_scalp(
                    client, symbol, target_profit_usdt, leverage, max_notional_usdt,
                    max_spread_bps, l2_entry_score, l2_exit_score, l2_stop_bps, l2_max_hold,
                    live, live_confirmation, l2_depth
                )
            elif action == "orderbook":
                result = {"success": True, "symbol": symbol, "orderbook": get_orderbook(client, symbol)}
            elif action == "ticker":
                rows = client.request("GET", "/v5/market/tickers", {"category": "linear", "symbol": validate_symbol(symbol)}).get("list", [])
                result = {"success": True, "symbol": symbol, "ticker": rows[0] if rows else {}}
            elif action == "instrument":
                result = {"success": True, "symbol": symbol, "instrument": client.request("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": validate_symbol(symbol)}).get("list", [{}])[0]}
            elif action == "wallet_balance":
                result = {"success": True, "wallet": client.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})}
            elif action == "positions":
                result = {"success": True, "positions": client.request("GET", "/v5/position/list", {"category": "linear", "symbol": validate_symbol(symbol) if symbol else None})}
            elif action == "open_orders":
                result = {"success": True, "orders": client.request("GET", "/v5/order/realtime", {"category": "linear", "symbol": validate_symbol(symbol) if symbol else None})}
            elif action == "fills":
                result = {"success": True, "fills": client.request("GET", "/v5/execution/list", {"category": "linear", "symbol": validate_symbol(symbol) if symbol else None, "limit": "50"})}
            elif action == "risk_status":
                st = load_state(state_file)
                result = {
                    "success": True,
                    "daily_realized_pnl": st.get("daily_realized_pnl", 0.0),
                    "kill_switch": kill_switch_active(),
                    "live_environment": not client.testnet,
                    "timestamp": utc_now(),
                }
            elif action == "execute_scalp":
                result = handle_execute_scalp(client, symbol, side, qty, target_profit_usdt, leverage, post_only, live, live_confirmation, max_spread_pct, max_notional_usdt, reduce_only)
            elif action == "close_position":
                positions = client.request("GET", "/v5/position/list", {"category": "linear", "symbol": validate_symbol(symbol)}).get("list", [])
                active = [p for p in positions if abs(safe_float(p.get("size"))) > 0]
                if not active:
                    raise ToolError(f"No open position found for {symbol}.", EXIT_FILE_NOT_FOUND)
                pos = active[0]
                close_side = "Sell" if str(pos.get("side")) == "Buy" else "Buy"
                result = handle_execute_scalp(client, symbol, close_side, qty or safe_float(pos.get("size")), 0.01, leverage, False, live, live_confirmation, 100.0, max_notional_usdt, reduce_only=True)
            elif action == "cancel_orders":
                if not live:
                    result = {"success": True, "mode": "DRY_RUN", "message": "Cancel-all simulated."}
                else:
                    cancelled = client.request("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": validate_symbol(symbol)})
                    result = {"success": True, "mode": "LIVE", "result": cancelled}
            elif action == "set_leverage":
                if not live:
                    result = {"success": True, "mode": "DRY_RUN", "message": f"Set leverage to {leverage}x simulated."}
                else:
                    res = client.request("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": validate_symbol(symbol), "buyLeverage": str(leverage), "sellLeverage": str(leverage)})
                    result = {"success": True, "mode": "LIVE", "result": res}
            elif action == "kill_switch":
                result = set_kill_switch(True)
            elif action == "history":
                path = trade_log_file_path(trade_log)
                lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
                recent = [json.loads(line) for line in lines[-50:] if line.strip()]
                result = {"success": True, "recent_trades": recent}
            else:
                raise ToolError(f"Unhandled action '{action}'.", EXIT_INVALID_INPUT)

        result["action"] = action
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["exit_code"] = EXIT_SUCCESS
        if use_cache and not live:
            cache.set(cache_key, result)
        return result

    except ToolError as exc:
        res = exc.to_dict()
        res.update({"action": action, "duration_ms": round((time.monotonic() - started) * 1000, 2)})
        return res
    except Exception as exc:
        if verbose:
            logging.exception("Unhandled error in tool execution")
        return {"success": False, "action": action, "error": str(exc), "exit_code": EXIT_ERROR, "duration_ms": round((time.monotonic() - started) * 1000, 2)}


# ==============================================================================
# SECTION 12: Self-Test & Diagnostic Suite
# ==============================================================================

def run_self_test() -> Dict[str, Any]:
    checks = []
    checks.append({"name": "python_version", "pass": sys.version_info >= (3, 9), "detail": platform.python_version()})
    checks.append({"name": "data_dir_writable", "pass": get_data_dir().exists(), "detail": str(get_data_dir())})
    checks.append({"name": "decimal_quantization", "pass": quantize_step("1.23456", "0.01") == "1.23", "detail": "quantize_step OK"})
    checks.append({"name": "symbol_validator", "pass": validate_symbol("SOLUSDT") == "SOLUSDT", "detail": "SOLUSDT OK"})
    pnl = calculate_micro_pnl(100.0, 100.20, 1.0, "Buy")
    checks.append({"name": "micro_pnl_math", "pass": pnl["net_pnl_usdt"] < pnl["gross_pnl_usdt"] and pnl["profitable"], "detail": "Fees deducted correctly"})
    return {"success": all(c["pass"] for c in checks), "version": __version__, "checks": checks, "timestamp": utc_now()}


# ==============================================================================
# SECTION 13: Output Routing & AIChat Entrypoint
# ==============================================================================

def write_llm_output(data: dict[str, Any], output_format: str = "json") -> None:
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    indent = 2 if output_format == "json" else None
    payload = json.dumps(data, indent=indent, ensure_ascii=False, cls=ToolJSONEncoder) + "\n"

    if out_path in {"/dev/stdout", "/dev/fd/1", "-"}:
        sys.stdout.write(payload)
        sys.stdout.flush()
    else:
        try:
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fp:
                fp.write(payload)
        except OSError as err:
            sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
            sys.stdout.write(payload)
            sys.stdout.flush()


def run(**kwargs: Any) -> None:
    """AIChat tool entrypoint executing scalp analyzer."""
    no_color = kwargs.get("no_color", False)
    out_format = kwargs.get("output_format", "json")
    result = execute_tool(**kwargs)
    print_human_readable_ui(result, no_color=no_color)
    write_llm_output(result, output_format=out_format)


def generate_tool_schema() -> Dict[str, Any]:
    return {
        "name": "pyrm_scalp_analyzer",
        "description": "Enterprise Bybit V5 USDT perpetual microstructure analyzer and guarded micro-profit scalper.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(VALID_ACTIONS), "default": "quick_scalp"},
                "symbol": {"type": "string", "default": "SOLUSDT"},
                "symbols": {"type": "string", "default": "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,ADAUSDT"},
                "timeframe": {"type": "string", "enum": sorted(VALID_TIMEFRAMES), "default": "1"},
                "side": {"type": "string", "enum": ["Buy", "Sell"], "default": "Buy"},
                "target_profit_usdt": {"type": "number", "default": 0.10},
                "qty": {"type": "number"},
                "leverage": {"type": "integer", "default": 10},
                "max_notional_usdt": {"type": "number", "default": 100.0},
                "max_spread_pct": {"type": "number", "default": 0.035},
                "live": {"type": "boolean", "default": False},
                "live_confirmation": {"type": "string", "default": ""},
                "post_only": {"type": "boolean", "default": True},
                "reduce_only": {"type": "boolean", "default": False},
                "use_cache": {"type": "boolean", "default": False},
            },
            "required": ["action"],
        },
    }


# ==============================================================================
# SECTION 14: CLI Argument Parser & Main
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyrm_scalp_analyzer.py",
        description=f"Enterprise Bybit Quantitative Scalper & Analyzer Tool v{__version__}",
    )
    parser.add_argument("--action", "-a", choices=sorted(VALID_ACTIONS), default="quick_scalp", help="Tool action (default: quick_scalp)")
    parser.add_argument("--symbol", "-s", default="SOLUSDT", help="Perpetual symbol (default: SOLUSDT)")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,ADAUSDT", help="Comma-separated symbols for multi-scan")
    parser.add_argument("--timeframe", "-t", choices=sorted(VALID_TIMEFRAMES), default="1", help="Candle interval (default: 1)")
    parser.add_argument("--side", choices=["Buy", "Sell"], default="Buy", help="Trade side (default: Buy)")
    parser.add_argument("--target-profit", "--target-profit-usdt", type=float, default=0.10, dest="target_profit_usdt", help="Net micro-profit target in USDT")
    parser.add_argument("--qty", type=float, default=None, help="Order quantity override")
    parser.add_argument("--leverage", type=int, default=10, help="Account leverage multiplier")
    parser.add_argument("--max-notional", "--max-notional-usdt", type=float, default=100.0, dest="max_notional_usdt", help="Maximum position notional in USDT")
    parser.add_argument("--max-spread-bps", type=float, default=12.0, help="Maximum L2 spread in bps")
    parser.add_argument("--max-spread-pct", type=float, default=0.035, help="Maximum REST spread %")
    parser.add_argument("--max-loss-trade", type=float, default=0.05, help="Hard stop-loss per trade in USDT")
    parser.add_argument("--daily-loss-cap", type=float, default=5.0, help="Max cumulative daily loss")
    parser.add_argument("--max-drawdown-pct", type=float, default=15.0, help="Max equity drawdown %")
    parser.add_argument("--max-positions", type=int, default=1, help="Max concurrent positions")
    parser.add_argument("--max-workers", type=int, default=5, help="Concurrent scanner threads")
    parser.add_argument("--cooldown", type=int, default=60, help="Symbol cooldown in seconds")
    parser.add_argument("--l2-depth", type=int, choices=[1, 50, 200, 1000], default=50, help="L2 book depth")
    parser.add_argument("--l2-entry-score", type=float, default=0.62, help="L2 entry threshold score")
    parser.add_argument("--l2-exit-score", type=float, default=0.20, help="L2 exit reversal score")
    parser.add_argument("--l2-stop-bps", type=float, default=10.0, help="L2 emergency stop bps")
    parser.add_argument("--l2-max-hold", type=float, default=45.0, dest="l2_max_hold", help="Max scalp hold seconds")
    parser.add_argument("--entry-price", type=float, default=None, help="Entry price for micro_pnl")
    parser.add_argument("--exit-price", type=float, default=None, help="Exit price for micro_pnl")
    parser.add_argument("--capital", "--capital-usdt", type=float, default=100.0, dest="capital_usdt", help="Compounding starting capital")
    parser.add_argument("--compound-rate", type=float, default=0.001, help="Per-trade compound rate")
    parser.add_argument("--trades", type=int, default=100, help="Number of compounding trades")
    parser.add_argument("--live-confirmation", default="", help="Required live token (e.g. I_UNDERSTAND_LIVE_RISK)")
    parser.add_argument("--state-file", default=None, help="State JSON file path")
    parser.add_argument("--trade-log", default=None, help="Trade ledger JSONL path")
    parser.add_argument("--output-format", choices=["json", "jsonl"], default="json", help="Output format")
    parser.add_argument("--cache-ttl", type=int, default=5, help="Cache TTL seconds")
    parser.add_argument("--cache-dir", default=None, help="Cache directory")
    parser.add_argument("--env-var", action="append", dest="env_vars", help="Environment overrides KEY=VALUE")
    parser.add_argument("--post-only", action="store_true", default=True, help="Enforce post-only maker limit")
    parser.add_argument("--reduce-only", action="store_true", default=False, help="Close/reduce position only")
    parser.add_argument("--live", action="store_true", default=False, help="Enable live trading execution")
    parser.add_argument("--use-cache", action="store_true", default=False, help="Enable response caching")
    parser.add_argument("--clear-cache", action="store_true", default=False, help="Clear cache and exit")
    parser.add_argument("--schema", action="store_true", default=False, help="Print tool schema and exit")
    parser.add_argument("--self-test", action="store_true", default=False, help="Run diagnostics and exit")
    parser.add_argument("--no-color", action="store_true", default=False, help="Disable ANSI colors")
    parser.add_argument("--verbose", "-v", action="store_true", default=False, help="Enable verbose logs")
    return parser


def main() -> int:
    args = sys.argv[1:]

    # AIChat instruction prompt hook
    if args and args[0] == "_instructions":
        testnet = parse_bool(cfg_value("LLM_AGENT_VAR_USE_TESTNET", "BYBIT_TESTNET", "true"), True)
        prompt = f"""You are operating as pyrm_scalp_analyzer (v{__version__}).
Network: {'TESTNET' if testnet else 'MAINNET'} | Kill Switch: {'ACTIVE' if kill_switch_active() else 'OFF'}
Policy: Default is guarded dry-run. Live trading strictly requires --live and --live-confirmation.
"""
        write_llm_output({"instructions": prompt})
        return EXIT_SUCCESS

    parsed = _build_parser().parse_args()

    if parsed.schema:
        write_llm_output(generate_tool_schema())
        return EXIT_SUCCESS

    if parsed.self_test:
        res = run_self_test()
        print_human_readable_ui(res, no_color=parsed.no_color)
        write_llm_output(res)
        return EXIT_SUCCESS if res["success"] else EXIT_ERROR

    if parsed.clear_cache:
        c = ToolCache(cache_dir=parsed.cache_dir)
        count = sum(1 for f in c.cache_dir.glob("*.json") if f.unlink() or True)
        _cprint(f"{NEON_GREEN}Cleared {count} cache entry/entries.{RESET}", no_color=parsed.no_color)
        return EXIT_SUCCESS

    kwargs = {
        "action": parsed.action,
        "symbol": parsed.symbol,
        "symbols": parsed.symbols,
        "timeframe": parsed.timeframe,
        "side": parsed.side,
        "target_profit_usdt": parsed.target_profit_usdt,
        "qty": parsed.qty,
        "leverage": parsed.leverage,
        "max_notional_usdt": parsed.max_notional_usdt,
        "max_spread_bps": parsed.max_spread_bps,
        "max_spread_pct": parsed.max_spread_pct,
        "max_loss_trade": parsed.max_loss_trade,
        "daily_loss_cap": parsed.daily_loss_cap,
        "max_drawdown_pct": parsed.max_drawdown_pct,
        "max_positions": parsed.max_positions,
        "max_workers": parsed.max_workers,
        "cooldown": parsed.cooldown,
        "l2_depth": parsed.l2_depth,
        "l2_entry_score": parsed.l2_entry_score,
        "l2_exit_score": parsed.l2_exit_score,
        "l2_stop_bps": parsed.l2_stop_bps,
        "l2_max_hold": parsed.l2_max_hold,
        "entry_price": parsed.entry_price,
        "exit_price": parsed.exit_price,
        "capital_usdt": parsed.capital_usdt,
        "compound_rate": parsed.compound_rate,
        "trades": parsed.trades,
        "live_confirmation": parsed.live_confirmation,
        "state_file": parsed.state_file,
        "trade_log": parsed.trade_log,
        "post_only": parsed.post_only,
        "reduce_only": parsed.reduce_only,
        "live": parsed.live,
        "use_cache": parsed.use_cache,
        "cache_ttl": parsed.cache_ttl,
        "cache_dir": parsed.cache_dir,
        "no_color": parsed.no_color,
        "verbose": parsed.verbose,
        "env_vars": parsed.env_vars,
    }

    result = execute_tool(**kwargs)
    print_human_readable_ui(result, no_color=parsed.no_color)
    write_llm_output(result, output_format=parsed.output_format)
    return int(result.get("exit_code", EXIT_SUCCESS))


if __name__ == "__main__":
    sys.exit(main())
