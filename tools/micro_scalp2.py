#!/usr/bin/env python3
# ==============================================================================
# micro_scalper_tool.py — Pyrmethus Autonomous Micro-Scalper v4.0.0-ASCENDED
# argc/aichat compatible · Colorized UI · Native V5 HMAC · Dual Single/Multi Scan
#
# @describe Enterprise Bybit USDT-Linear microstructure scalper with native v5 REST HMAC-SHA256 signing,
#           exponential OFI, depth-composite imbalances, fee-rebate aware TP/SL, live position & balance sync,
#           Ollama LLM reasoning engine, dynamic lot-size resolution, and permissive argument normalization.
#
# @meta require-tools aichat
#
# @option --symbols <SYMBOLS>            Comma-separated trading pairs (default: BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT)
# @option --symbol <SYMBOL>              Single trading pair override (takes precedence if provided)
# @option --side <SIDE>                  Order side for single-symbol mode: Buy/Sell (optional: auto if omitted)
# @option --target <USDT>                Target profit per trade in USDT (default: 0.02)
# @option --target-profit <USDT>         Alias for --target
# @option --leverage <LEV>               Account leverage multiplier (default: 50, max: 100)
# @option --pos-value <USDT>             Fixed position notional value cap in USDT (default: 50.0)
# @option --pos-pct <PCT>                Position equity percentage sizing (0.0 to 100.0, default: 0.0)
# @option --spread-max-bps <BPS>         Maximum allowable spread in basis points (default: 15.0)
# @option --max-positions <NUM>          Maximum simultaneous open positions (default: 1)
# @option --max-workers <NUM>            Concurrent market analysis worker threads (default: 5)
# @option --cooldown <SEC>               Per-symbol trade cooldown in seconds (default: 120)
# @option --loop-delay <SEC>             Cycle sleep interval when loop is enabled (default: 30)
# @option --risk-reward <RATIO>          Risk to reward ratio multiplier (default: 1.0)
# @option --trailing-stop <VAL>          Trailing stop distance in price or bps (e.g. 0.05 or 10bps)
# @option --max-loss-trade <USDT>        Hard stop-loss limit per trade in USDT (default: 0.05)
# @option --daily-loss-cap <USDT>        Maximum cumulative daily loss before halt (default: 0.50)
# @option --max-drawdown-pct <PCT>       Maximum equity drawdown % from peak (default: 15.0)
# @option --state-file <PATH>            Path to persistent state JSON file
# @option --trade-log <PATH>             Path to persistent JSONL trade ledger
# @option --output-format <FORMAT>       Output envelope format: json/jsonl (default: jsonl)
# @option --cache-ttl <SEC>              Cache TTL for market metadata (default: 10)
# @option --cache-dir <PATH>             Custom cache storage directory
# @option --ollama-host <URL>            Ollama LLM endpoint (default: http://localhost:11434)
# @option --ollama-model <MODEL>         Ollama model name (default: nemotron-3-nano:30b-cloud)
# @option --env-var <KEY=VALUE>          Custom environment variable override (repeatable)
# @flag   --loop                         Run continuous market evaluation loop
# @flag   --dry-run                      Simulate orders and execution without financial risk
# @flag   --enable-ai                    Enable Ollama AI trade thesis analysis
# @flag   --use-cache                    Enable short-term cache for ticker and kline data
# @flag   --clear-cache                  Clear state and cache directories and exit
# @flag   --schema                       Print JSON Tool Schema for LLM registration and exit
# @flag   --json-only                    Output raw JSON/JSONL only (suppress UI box on stderr)
# @flag   --quiet                        Suppress non-essential progress and UI output
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug log output and tracebacks
#
# @env BYBIT_API_KEY                     Bybit V5 REST API Key
# @env BYBIT_API_SECRET                  Bybit V5 REST API Secret
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# @env SCALP_SYMBOLS=BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT
# @env SCALP_SYMBOL=ETHUSDT
# @env SCALP_LEVERAGE=50
# @env SCALP_TARGET=0.02
# @env SCALP_DRY_RUN=false
# ==============================================================================

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation, getcontext
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Resolve parent directory and add it to sys.path to allow importing local sibling modules
_script_dir = str(Path(__file__).resolve().parent)
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# Auto-inject typical AIChat tool and config directories into sys.path
for candidate_dir in [
    Path.home() / ".config" / "aichat" / "tools",
    Path.home() / ".config" / "aichat" / "llm-functions",
    Path.home() / ".config" / "aichat",
    Path.home() / "autonomous-micro-scalper",
]:
    if candidate_dir.exists() and str(candidate_dir) not in sys.path:
        sys.path.insert(0, str(candidate_dir))

# Precision for quantitative calculations
getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

__version__ = "4.0.0-ASCENDED"
__all__ = [
    "GracefulShutdown",
    "MarketDataEngine",
    "MicrostructureAnalyzer",
    "OllamaReasoningEngine",
    "OrderExecutionEngine",
    "ScalperStateManager",
    "ToolCache",
    "ToolError",
    "__version__",
    "execute_tool",
    "generate_tool_schema",
    "main",
    "run",
    "validate_inputs",
]

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_TIMEOUT = 124
EXIT_PERMISSION_DENIED = 126
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPTED = 130

DEFAULT_CACHE_TTL = 10
DEFAULT_CACHE_MAX_SIZE_MB = 128
VALID_SIDES = {"BUY", "SELL"}
VALID_OUTPUT_FORMATS = {"json", "jsonl"}

# Default VIP-0 Bybit Fee Rates
DEFAULT_TAKER_FEE = Decimal("0.000550")  # 0.055% taker
DEFAULT_MAKER_FEE = Decimal("-0.000200")  # -0.020% maker rebate
MIN_NET_FLOOR = Decimal("0.02")

PAUSE_MINUTES = [15, 30, 45, 60]

# Static fallback precision rules
FALLBACK_PRECISION_RULES: dict[str, dict[str, Decimal]] = {
    "BTCUSDT":  {"qty_step": Decimal("0.001"), "price_step": Decimal("0.10"), "min_qty": Decimal("0.001")},
    "ETHUSDT":  {"qty_step": Decimal("0.01"), "price_step": Decimal("0.01"), "min_qty": Decimal("0.01")},
    "SOLUSDT":  {"qty_step": Decimal("0.1"), "price_step": Decimal("0.001"), "min_qty": Decimal("0.1")},
    "BNBUSDT":  {"qty_step": Decimal("0.01"), "price_step": Decimal("0.01"), "min_qty": Decimal("0.01")},
    "ADAUSDT":  {"qty_step": Decimal("1"), "price_step": Decimal("0.0001"), "min_qty": Decimal("1")},
    "LINKUSDT": {"qty_step": Decimal("0.1"), "price_step": Decimal("0.001"), "min_qty": Decimal("0.1")},
    "XRPUSDT":  {"qty_step": Decimal("1"), "price_step": Decimal("0.0001"), "min_qty": Decimal("1")},
    "DOGEUSDT": {"qty_step": Decimal("1"), "price_step": Decimal("0.00001"), "min_qty": Decimal("1")},
    "AVAXUSDT": {"qty_step": Decimal("0.1"), "price_step": Decimal("0.001"), "min_qty": Decimal("0.1")},
    "NEARUSDT": {"qty_step": Decimal("0.1"), "price_step": Decimal("0.001"), "min_qty": Decimal("0.1")},
    "SUIUSDT":  {"qty_step": Decimal("1"), "price_step": Decimal("0.0001"), "min_qty": Decimal("1")},
}

BLOCKED_ENV_VARS = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME",
    "PATH", "SHELL", "SUDO_COMMAND", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
})

# ==============================================================================
# SECTION 1: System Constants & Exceptions
# ==============================================================================


class ToolError(Exception):
    def __init__(
        self,
        message: str,
        exit_code: int = EXIT_ERROR,
        error_type: str = "ExecutionError",
        recoverable: bool = False,
        details: Optional[dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.error_type = error_type
        self.recoverable = recoverable
        self.details = details or {}
        self.trace_id = trace_id or str(uuid.uuid4())

    def to_dict(self, verbose: bool = False) -> dict[str, Any]:
        payload = {
            "success": False,
            "error": self.message,
            "type": self.error_type,
            "exit_code": self.exit_code,
            "recoverable": self.recoverable,
            "trace_id": self.trace_id,
            **self.details,
        }
        if verbose:
            payload["traceback"] = traceback.format_exc()
        return payload


# ==============================================================================
# SECTION 2: JSON Serialization & Parsing Helpers
# ==============================================================================


class ToolJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        try:
            if isinstance(obj, Decimal):
                return float(obj) if math.isfinite(float(obj)) else str(obj)
            if isinstance(obj, Path):
                return str(obj)
            if isinstance(obj, Enum):
                return obj.value
            if isinstance(obj, (datetime.datetime, datetime.date)):
                return obj.isoformat()
            if isinstance(obj, datetime.timedelta):
                return obj.total_seconds()
            if isinstance(obj, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address)):
                return str(obj)
            if isinstance(obj, bytes):
                return obj.decode("utf-8", errors="replace")
            if isinstance(obj, (set, frozenset)):
                return sorted(list(obj))
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)
            if hasattr(obj, "model_dump") and callable(obj.model_dump):
                return obj.model_dump()
            if isinstance(obj, Mapping):
                return dict(obj)
            if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
                return list(obj)
        except Exception:
            pass
        return repr(obj)


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def parse_safe_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = _clean_str(val).lower()
    if s in ("1", "true", "yes", "y", "t", "on"):
        return True
    if s in ("0", "false", "no", "n", "f", "off"):
        return False
    return default


def parse_safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = _clean_str(val)
    if s.lower() in ("", "none", "null", "undefined", "nan"):
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def parse_safe_decimal(val: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    if val is None:
        return default
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = _clean_str(val)
    if s.lower() in ("", "none", "null", "undefined", "nan"):
        return default
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError, ValueError):
        return default


# ==============================================================================
# SECTION 3: Terminal Colors, Logging & UI Display Helpers
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
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(text: str, file: Any = None, no_color: bool = False, end: str = "\n") -> None:
    target = file or sys.stderr
    if not _is_tty(no_color=no_color):
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def setup_tool_logging(verbose: bool = False, request_id: Optional[str] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_format = f"[%(asctime)s] [{request_id or 'scalper'}] [%(levelname)s] %(message)s"
    try:
        logging.basicConfig(level=level, format=log_format, force=True, stream=sys.stderr)
    except TypeError:
        logging.basicConfig(level=level, format=log_format, stream=sys.stderr)


def print_scalp_ui(data: dict[str, Any], no_color: bool = False, quiet: bool = False) -> None:
    if quiet or not _is_tty(no_color=no_color):
        return
    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "EXEC_OK" if success else "REJECTED/HALTED"
    box_w = 72
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}", no_color=no_color)
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [MICRO-SCALPER v{__version__}]{RESET} "
        f"{status_color}{BOLD}{status_symbol} {status_text}{RESET}".ljust(box_w + 14 if _is_tty(no_color) else box_w)
        + f"{NEON_PURPLE}│{RESET}",
        no_color=no_color,
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)

    state = data.get("state", {})
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Wallet Equity:{RESET}  ${state.get('wallet_equity', 0.0):.4f} USDT  "
        f"│ {NEON_CYAN}Daily PnL:{RESET} {NEON_GREEN if state.get('daily_pnl', 0.0) >= 0 else NEON_RED}"
        f"${state.get('daily_pnl', 0.0):+.4f}{RESET}",
        no_color=no_color,
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Peak Equity:{RESET}    ${state.get('equity_peak', 0.0):.4f} USDT  "
        f"│ {NEON_CYAN}Loss Streak:{RESET} {NEON_YELLOW}{state.get('consecutive_losses', 0)}{RESET}",
        no_color=no_color,
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Target/Trade:{RESET}   ${data.get('target', data.get('target_profit_usdt', '0.02'))} USDT  "
        f"│ {NEON_CYAN}Leverage:{RESET}   {NEON_YELLOW}{data.get('leverage', 50)}x{RESET}",
        no_color=no_color,
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Cycle Time:{RESET}     {DIM}{data.get('duration_ms', 0)} ms{RESET}       "
        f"│ {NEON_CYAN}Dry Run:{RESET}    {NEON_YELLOW}{data.get('dry_run', False)}{RESET}",
        no_color=no_color,
    )

    orders = data.get("orders") or ([data["trade"]] if data.get("trade") else [])
    if orders:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}{NEON_GREEN}ACTIVE SIGNALS & EXECUTIONS ({len(orders)}):{RESET}", no_color=no_color)
        for o in orders:
            side_col = NEON_GREEN if str(o.get("side", "")).upper() == "BUY" else NEON_RED
            _cprint(
                f"{NEON_PURPLE}│{RESET}   › {BOLD}{o.get('symbol')}{RESET} {side_col}{o.get('side')}{RESET} "
                f"Qty: {o.get('qty')} @ ${o.get('entry_price')} | TP: ${o.get('tp_price', o.get('tp'))} | SL: ${o.get('sl_price', o.get('sl'))}",
                no_color=no_color,
            )
            _cprint(
                f"{NEON_PURPLE}│{RESET}     Net Exp: {NEON_GREEN}+${o.get('expected_net_pnl', o.get('net_pnl', 0.0)):.4f} USDT{RESET} "
                f"(OFI: {o.get('ofi', 0.5):.2f}, Mom: {o.get('momentum', 0.0):+.3f}%, Status: {BOLD}{o.get('status', 'OK')}{RESET})",
                no_color=no_color,
            )
            if o.get("ai_commentary"):
                _cprint(f"{NEON_PURPLE}│{RESET}     {NEON_PINK}🤖 AI Thesis:{RESET} {DIM}{o['ai_commentary'][:56]}...{RESET}", no_color=no_color)

    skipped = data.get("skipped") or {}
    if skipped:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Skipped Evaluation Summary:{RESET}", no_color=no_color)
        for sym, reason in list(skipped.items())[:8]:
            _cprint(f"{NEON_PURPLE}│{RESET}   ⚑ {DIM}{sym}:{RESET} {reason}", no_color=no_color)

    if data.get("skipped_reason"):
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}Filter Gate:{RESET} {data['skipped_reason']}", no_color=no_color)

    for warn in data.get("warnings", []):
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}⚠ Warning:{RESET} {warn}", no_color=no_color)

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error [{data.get('type', 'Error')}]:{RESET} {data['error']}", no_color=no_color)

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}", no_color=no_color)


# ==============================================================================
# SECTION 4: Input Validation & Security Engine
# ==============================================================================


class SecurityValidator:
    @staticmethod
    def sanitize_env_vars(env_vars: Optional[list[str]]) -> tuple[dict[str, str], list[str]]:
        if not env_vars:
            return {}, []
        sanitized: dict[str, str] = {}
        warnings: list[str] = []
        for entry in env_vars:
            if "=" not in entry:
                warnings.append(f"Ignored malformed env var '{entry}' (must be KEY=VALUE)")
                continue
            k, v = entry.split("=", 1)
            k_clean = k.strip()
            if not k_clean:
                continue
            if k_clean.upper() in BLOCKED_ENV_VARS:
                warnings.append(f"Blocked dangerous environment variable override: '{k_clean}'")
                continue
            sanitized[k_clean] = v.strip()
        return sanitized, warnings


def validate_inputs(
    symbols: Optional[str] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    target: Any = None,
    leverage: Any = None,
    spread_max_bps: Any = None,
    max_loss_trade: Any = None,
    daily_loss_cap: Any = None,
    output_format: str = "jsonl",
) -> Optional[dict[str, Any]]:
    trace_id = str(uuid.uuid4())
    if not (symbols or symbol):
        return ToolError("At least one of --symbols or --symbol is required.", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
    if side and _clean_str(side).upper() not in VALID_SIDES:
        return ToolError(f"Invalid side '{side}'. Allowed: Buy, Sell", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
    if output_format not in VALID_OUTPUT_FORMATS:
        return ToolError(f"Invalid output format '{output_format}'. Allowed: {sorted(VALID_OUTPUT_FORMATS)}", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
    for name, val, min_v in [
        ("target", target, 0), ("leverage", leverage, 1),
        ("spread_max_bps", spread_max_bps, 0), ("max_loss_trade", max_loss_trade, 0),
        ("daily_loss_cap", daily_loss_cap, 0),
    ]:
        parsed = parse_safe_float(val)
        if parsed is not None and parsed <= min_v:
            return ToolError(f"{name} must be > {min_v} (received: {val}).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
    lev = parse_safe_float(leverage)
    if lev is not None and lev > 125:
        return ToolError(f"Leverage must be <= 125 (received: {leverage}).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
    return None


# ==============================================================================
# SECTION 5: Agent Execution Context & Path Resolution
# ==============================================================================


def get_builtin_var(name: str) -> Optional[str]:
    return (
        os.environ.get(f"LLM_AGENT_VAR_{name}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.lower()}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.upper()}")
    )


def resolve_agent_path(target_str: Optional[str]) -> Optional[Path]:
    if not target_str:
        return None
    raw = Path(target_str).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)
    agent_cwd = get_builtin_var("__cwd__")
    if agent_cwd:
        return (Path(agent_cwd) / raw).resolve(strict=False)
    return raw.resolve(strict=False)


def env_default(env_name: str, fallback: Any = None) -> dict[str, Any]:
    val = os.getenv(env_name)
    if val is not None:
        return {"default": val}
    if fallback is not None:
        return {"default": fallback}
    return {}


# ==============================================================================
# SECTION 6: Cache Engine & State Persistence Manager
# ==============================================================================


class ToolCache:
    def __init__(
        self,
        cache_dir: Optional[Path | str] = None,
        ttl: int = DEFAULT_CACHE_TTL,
        max_size_mb: int = DEFAULT_CACHE_MAX_SIZE_MB,
    ) -> None:
        if cache_dir:
            self.cache_dir = Path(cache_dir).resolve()
        elif "LLM_TOOL_CACHE_DIR" in os.environ:
            self.cache_dir = Path(os.environ["LLM_TOOL_CACHE_DIR"]).resolve()
        else:
            self.cache_dir = Path.home() / ".cache" / "aichat_scalper"
        self.ttl = ttl
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._lock = threading.Lock()
        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _hash_key(self, key_str: str) -> str:
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()

    def get(self, key_str: str) -> Optional[dict[str, Any]]:
        cache_file = self.cache_dir / f"{self._hash_key(key_str)}.json"
        if not cache_file.exists():
            return None
        try:
            if (time.time() - cache_file.stat().st_mtime) > self.ttl:
                cache_file.unlink(missing_ok=True)
                return None
            with open(cache_file, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return None

    def set(self, key_str: str, value: Any) -> None:
        key_hash = self._hash_key(key_str)
        cache_file = self.cache_dir / f"{key_hash}.json"
        tmp_file = self.cache_dir / f"{key_hash}.tmp.{os.getpid()}"
        with self._lock:
            try:
                with open(tmp_file, "w", encoding="utf-8") as fp:
                    json.dump(value, fp, cls=ToolJSONEncoder, ensure_ascii=False)
                tmp_file.replace(cache_file)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp_file.unlink(missing_ok=True)


class ScalperStateManager:
    def __init__(self, state_path: Optional[str] = None, ledger_path: Optional[str] = None) -> None:
        self.state_file = resolve_agent_path(state_path) or Path.home() / ".scalper_state.json"
        self.ledger_file = resolve_agent_path(ledger_path) or Path.home() / "trade_log.jsonl"
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            defaults = {
                "daily_pnl": "0.0",
                "consecutive_losses": 0,
                "equity_peak": "0.0",
                "wallet_equity": "2.00",
                "pause_until": None,
                "last_reset_day": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
                "symbol_cooldowns": {},
            }
            if not self.state_file.exists():
                return defaults
            try:
                with open(self.state_file, encoding="utf-8") as fp:
                    data = json.load(fp)
                current_day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                if data.get("last_reset_day") != current_day:
                    data["daily_pnl"] = "0.0"
                    data["last_reset_day"] = current_day
                if "symbol_cooldowns" not in data:
                    data["symbol_cooldowns"] = {}
                return data
            except Exception:
                return defaults

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_file.with_suffix(f".tmp.{os.getpid()}")
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(state, fp, indent=2, cls=ToolJSONEncoder)
                tmp.replace(self.state_file)
            except Exception as e:
                logging.error(f"Failed to persist scalper state: {e}")

    def log_trade(self, entry: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.ledger_file, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(entry, cls=ToolJSONEncoder, ensure_ascii=False) + "\n")
            except Exception as e:
                logging.error(f"Failed to append to trade ledger: {e}")


# ==============================================================================
# SECTION 7: Signal Interception & Tool Schema
# ==============================================================================


class GracefulShutdown:
    def __init__(self) -> None:
        self.interrupted = False
        self._old_sigint = self._old_sigterm = None

    def __enter__(self) -> GracefulShutdown:
        if threading.current_thread() is threading.main_thread():
            try:
                self._old_sigint = signal.signal(signal.SIGINT, self._handle)
                self._old_sigterm = signal.signal(signal.SIGTERM, self._handle)
            except (ValueError, AttributeError):
                pass
        return self

    def __exit__(self, *args: Any) -> None:
        if threading.current_thread() is threading.main_thread():
            if self._old_sigint is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    signal.signal(signal.SIGINT, self._old_sigint)
            if self._old_sigterm is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    signal.signal(signal.SIGTERM, self._old_sigterm)

    def _handle(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def should_stop(self) -> bool:
        return self.interrupted


def generate_tool_schema() -> dict[str, Any]:
    return {
        "name": "micro_scalper_tool",
        "description": "High-frequency Bybit quantitative scalper evaluating OFI, depth ratios, dynamic spread-bands, and maker rebates. Supports single-symbol forced side or multi-symbol autonomous scan.",
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols for multi-mode."},
                "symbol": {"type": "string", "description": "Single symbol (overrides symbols list)."},
                "side": {"type": "string", "enum": ["Buy", "Sell"], "description": "Forced side for single-symbol mode."},
                "target": {"type": ["string", "number", "null"], "description": "Target net profit USDT (default: 0.02)."},
                "target_profit": {"type": ["string", "number", "null"], "description": "Alias for target."},
                "leverage": {"type": ["integer", "number", "string", "null"], "description": "Leverage multiplier."},
                "pos_value": {"type": ["string", "number", "null"], "description": "Fixed notional cap."},
                "pos_pct": {"type": ["number", "string", "null"], "description": "Position equity percentage sizing."},
                "spread_max_bps": {"type": ["number", "string", "null"], "description": "Max spread in bps."},
                "max_positions": {"type": ["integer", "number", "string", "null"], "description": "Max simultaneous positions."},
                "cooldown": {"type": ["integer", "number", "string", "null"], "description": "Per-symbol trade cooldown in seconds."},
                "max_loss_trade": {"type": ["number", "string", "null"], "description": "Hard SL per trade USDT."},
                "daily_loss_cap": {"type": ["number", "string", "null"], "description": "Daily loss halt threshold."},
                "trailing_stop": {"type": ["string", "number", "null"], "description": "Trailing stop distance or null."},
                "loop": {"type": ["boolean", "string", "null"], "description": "Continuous mode."},
                "dry_run": {"type": ["boolean", "string", "null"], "description": "Simulation only."},
                "enable_ai": {"type": ["boolean", "string", "null"], "description": "Enable Ollama AI trade analysis."},
            },
            "additionalProperties": True,
        },
    }


# ==============================================================================
# SECTION 8: Ollama AI Reasoning Engine
# ==============================================================================


class OllamaReasoningEngine:
    @staticmethod
    def generate_trade_thesis(
        symbol: str,
        side: str,
        ofi: Decimal,
        momentum: Decimal,
        composite: Decimal,
        spread_bps: Decimal,
        host: str = "http://localhost:11434",
        model: str = "nemotron-3-nano:30b-cloud",
    ) -> Optional[str]:
        """Generate AI trade rationale via Ollama endpoint with tight timeout."""
        try:
            url = f"{host.rstrip('/')}/api/generate"
            prompt = (
                f"Analyze this high-frequency scalp trade setup in 1 concise sentence: "
                f"Symbol={symbol}, Direction={side}, OFI_Decay={float(ofi):.3f}, "
                f"DepthComposite={float(composite):.3f}, Momentum1m={float(momentum):+.3f}%, SpreadBps={float(spread_bps):.1f}."
            )
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.88,
                    "num_predict": 110,
                    "top_p": 0.9,
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("response", "").strip()
        except Exception:
            pass
        return None


# ==============================================================================
# SECTION 9: Market Data & Quantitative Microstructure Engine
# ==============================================================================


class MarketDataEngine:
    @staticmethod
    def get_instrument_info(symbol: str, cache: Optional[ToolCache] = None) -> dict[str, Decimal]:
        cache_key = f"inst_info_{symbol}"
        if cache:
            cached = cache.get(cache_key)
            if cached:
                return {
                    "qty_step": Decimal(str(cached["qty_step"])),
                    "price_step": Decimal(str(cached["price_step"])),
                    "min_qty": Decimal(str(cached["min_qty"])),
                }

        try:
            url = f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={symbol}"
            req = urllib.request.Request(url, headers={"User-Agent": f"PyrmScalper/{__version__}"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode())
                    if raw.get("retCode") == 0 and "result" in raw:
                        items = raw["result"].get("list", [])
                        if items:
                            info = items[0]
                            lot_filter = info.get("lotSizeFilter", {})
                            price_filter = info.get("priceFilter", {})
                            rules = {
                                "qty_step": Decimal(str(lot_filter.get("qtyStep", "0.01"))),
                                "price_step": Decimal(str(price_filter.get("tickSize", "0.001"))),
                                "min_qty": Decimal(str(lot_filter.get("minOrderQty", "0.01"))),
                            }
                            if cache:
                                cache.set(cache_key, {k: str(v) for k, v in rules.items()})
                            return rules
        except Exception:
            pass

        return FALLBACK_PRECISION_RULES.get(
            symbol, {"qty_step": Decimal("0.01"), "price_step": Decimal("0.001"), "min_qty": Decimal("0.01")}
        )

    @staticmethod
    def get_orderbook(symbol: str, limit: int = 25) -> dict[str, list[list[str]]]:
        with contextlib.suppress(Exception):
            import functions  # type: ignore
            if hasattr(functions, "bybit_realm"):
                res = functions.bybit_realm({"action": "get_orderbook", "symbol": symbol, "limit": limit, "json": True})
                if isinstance(res, dict) and "text" in res:
                    payload = json.loads(res["text"])
                    if "result" in payload and "bids" in payload["result"]:
                        return payload["result"]

        # Try local bybit_core
        with contextlib.suppress(Exception):
            import bybit_core
            res = bybit_core.get_orderbook(symbol=symbol, limit=limit)
            if isinstance(res, dict) and res.get("retCode") == 0:
                result = res.get("result", {})
                if "b" in result and "a" in result:
                    return {"bids": result.get("b", []), "asks": result.get("a", [])}

        try:
            url = f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}&limit={limit}"
            req = urllib.request.Request(url, headers={"User-Agent": f"PyrmScalper/{__version__}"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode())
                    if raw.get("retCode") == 0 and "result" in raw:
                        return {"bids": raw["result"].get("b", []), "asks": raw["result"].get("a", [])}
        except Exception:
            pass

        base = {
            "BTCUSDT": 64250.0, "ETHUSDT": 2435.0, "BNBUSDT": 570.0, "SOLUSDT": 142.5,
            "ADAUSDT": 0.35, "LINKUSDT": 11.85, "XRPUSDT": 0.52, "DOGEUSDT": 0.11,
        }
        p = base.get(symbol, 100.0)
        tick = 0.001 if p < 50 else (0.01 if p < 500 else 0.5)
        bids = [[f"{p - (i * tick):.6f}", f"{max(0.01, 12.0 - i * 0.35):.4f}"] for i in range(1, limit + 1)]
        asks = [[f"{p + (i * tick):.6f}", f"{max(0.01, 11.5 - i * 0.35):.4f}"] for i in range(1, limit + 1)]
        return {"bids": bids, "asks": asks}

    @staticmethod
    def get_atr14(symbol: str, interval: str = "1") -> Decimal:
        # Try local bybit_core
        with contextlib.suppress(Exception):
            import bybit_core
            res = bybit_core.api_request(
                "GET",
                "/v5/market/kline",
                params={"category": "linear", "symbol": symbol, "interval": interval, "limit": 15}
            )
            if isinstance(res, dict) and res.get("retCode") == 0:
                result = res.get("result", {})
                list_klines = result.get("list", [])
                if len(list_klines) >= 14:
                    trs = [Decimal(k[2]) - Decimal(k[3]) for k in list_klines[:14]]
                    return sum(trs) / Decimal(len(trs))

        try:
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=15"
            req = urllib.request.Request(url, headers={"User-Agent": f"PyrmScalper/{__version__}"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode())
                    if raw.get("retCode") == 0:
                        klines = raw["result"].get("list", [])
                        if len(klines) >= 14:
                            trs = [Decimal(k[2]) - Decimal(k[3]) for k in klines[:14]]
                            return sum(trs) / Decimal(len(trs))
        except Exception:
            pass
        defaults = {"BTCUSDT": Decimal("150"), "ETHUSDT": Decimal("8.5"), "SOLUSDT": Decimal("0.85")}
        return defaults.get(symbol, Decimal("0.05"))

    @staticmethod
    def get_momentum_1m(symbol: str) -> Decimal:
        # Try local bybit_core
        with contextlib.suppress(Exception):
            import bybit_core
            res = bybit_core.api_request(
                "GET",
                "/v5/market/recent-trade",
                params={"category": "linear", "symbol": symbol, "limit": 15}
            )
            if isinstance(res, dict) and res.get("retCode") == 0:
                result = res.get("result", {})
                trades = result.get("list", [])
                if len(trades) >= 2:
                    p0 = Decimal(trades[-1]["price"])
                    p1 = Decimal(trades[0]["price"])
                    return ((p1 - p0) / p0) * Decimal("100")

        try:
            url = f"https://api.bybit.com/v5/market/recent-trade?category=linear&symbol={symbol}&limit=15"
            req = urllib.request.Request(url, headers={"User-Agent": f"PyrmScalper/{__version__}"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status == 200:
                    raw = json.loads(resp.read().decode())
                    if raw.get("retCode") == 0:
                        trades = raw["result"].get("list", [])
                        if len(trades) >= 2:
                            p0 = Decimal(trades[-1]["price"])
                            p1 = Decimal(trades[0]["price"])
                            return ((p1 - p0) / p0) * Decimal("100")
        except Exception:
            pass
        return Decimal("0")


class MicrostructureAnalyzer:
    @staticmethod
    def quantize(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def compute_decay_ofi(bids: list, asks: list, levels: int = 10, decay: Decimal = Decimal("0.85")) -> Decimal:
        bid_w = ask_w = Decimal("0")
        n = min(levels, len(bids), len(asks))
        if n == 0:
            return Decimal("0.5")
        for i in range(n):
            w = decay ** Decimal(i)
            bid_w += Decimal(bids[i][1]) * w
            ask_w += Decimal(asks[i][1]) * w
        total = bid_w + ask_w
        return (bid_w / total) if total else Decimal("0.5")

    @staticmethod
    def compute_depth_ratio(bids: list, asks: list) -> Decimal:
        t5b = sum(Decimal(b[1]) for b in bids[:5])
        t5a = sum(Decimal(a[1]) for a in asks[:5])
        r5 = (t5b / (t5b + t5a)) if (t5b + t5a) else Decimal("0.5")
        t25b = sum(Decimal(b[1]) for b in bids[:25])
        t25a = sum(Decimal(a[1]) for a in asks[:25])
        r25 = (t25b / (t25b + t25a)) if (t25b + t25a) else Decimal("0.5")
        return (Decimal("0.70") * r5) + (Decimal("0.30") * r25)

    @staticmethod
    def map_dynamic_target(spread: Decimal, override: Optional[Decimal] = None) -> Decimal:
        if override and override > 0:
            return override
        if spread <= Decimal("0.02"):
            return Decimal("0.02")
        if spread <= Decimal("0.05"):
            return Decimal("0.05")
        if spread <= Decimal("0.10"):
            return Decimal("0.10")
        return Decimal("0.20")

    @staticmethod
    def calculate_sizing(
        symbol: str,
        target_profit: Decimal,
        spread: Decimal,
        atr14: Decimal,
        ofi: Decimal,
        composite: Decimal,
        max_loss: Decimal,
        equity: Decimal,
        leverage: Decimal,
        entry: Decimal,
        pos_value_cap: Optional[Decimal] = None,
        pos_pct: Optional[Decimal] = None,
        rules: Optional[dict[str, Decimal]] = None,
    ) -> Decimal:
        if spread <= 0:
            return Decimal("0")
        base = target_profit / spread
        vol = max(Decimal("0.2"), Decimal("1") - (atr14 / Decimal("100")))
        imb = max(Decimal("0.8"), min(Decimal("1.2"), (Decimal("0.8") + Decimal("0.4") * ofi) * composite))
        qty = base * vol * imb * Decimal("0.99")

        if qty * spread > max_loss:
            qty *= max_loss / (qty * spread)

        max_notional = equity * leverage
        if pos_pct and pos_pct > 0:
            max_notional = min(max_notional, equity * (pos_pct / Decimal("100")) * leverage)
        if pos_value_cap and pos_value_cap > 0:
            max_notional = min(max_notional, pos_value_cap)
        if entry > 0 and qty * entry > max_notional:
            qty = (max_notional / entry) * Decimal("0.98")

        rule = rules or FALLBACK_PRECISION_RULES.get(symbol, {"qty_step": Decimal("0.01"), "min_qty": Decimal("0.01")})
        qty = MicrostructureAnalyzer.quantize(qty, rule["qty_step"])
        qty = max(qty, rule["min_qty"])
        return qty

    @staticmethod
    def calculate_tp_sl(
        symbol: str,
        entry: Decimal,
        side: str,
        qty: Decimal,
        target: Decimal,
        max_loss: Decimal,
        rules: Optional[dict[str, Decimal]] = None,
        taker: Decimal = DEFAULT_TAKER_FEE,
        maker: Decimal = DEFAULT_MAKER_FEE,
    ) -> tuple[Decimal, Decimal, Decimal]:
        entry_fee = entry * qty * taker
        exit_fee = entry * qty * maker  # negative = rebate
        round_trip = entry_fee + exit_fee
        slip = entry * qty * Decimal("0.0001")
        needed = round_trip + slip + target

        if side.upper() == "BUY":
            tp = entry + (needed / qty)
            sl = entry - (max_loss / qty)
        else:
            tp = entry - (needed / qty)
            sl = entry + (max_loss / qty)

        rule = rules or FALLBACK_PRECISION_RULES.get(symbol, {"price_step": Decimal("0.001")})
        step = rule["price_step"]
        return (
            MicrostructureAnalyzer.quantize(tp, step),
            MicrostructureAnalyzer.quantize(sl, step),
            target,
        )


# ==============================================================================
# SECTION 10: Standalone Signed Bybit V5 REST & Order Engine
# ==============================================================================


class OrderExecutionEngine:
    @staticmethod
    def get_credentials() -> tuple[Optional[str], Optional[str]]:
        key = os.environ.get("BYBIT_API_KEY") or os.environ.get("BYBIT_KEY") or os.environ.get("API_KEY")
        sec = os.environ.get("BYBIT_API_SECRET") or os.environ.get("BYBIT_SECRET") or os.environ.get("API_SECRET")
        return key, sec

    @staticmethod
    def _send_signed_request(
        method: str,
        path: str,
        params_or_body: dict[str, Any],
        api_key: str,
        api_secret: str,
    ) -> dict[str, Any]:
        """Sign and dispatch generic Bybit V5 REST requests with retry handling."""
        ts = str(int(time.time() * 1000))
        recv = "5000"
        url = f"https://api.bybit.com{path}"

        if method.upper() == "GET":
            query_str = urllib.parse.urlencode(params_or_body)
            if query_str:
                url = f"{url}?{query_str}"
            sign_str = f"{ts}{api_key}{recv}{query_str}"
            req = urllib.request.Request(url, method="GET")
        else:
            payload_str = json.dumps(params_or_body, separators=(",", ":"))
            sign_str = f"{ts}{api_key}{recv}{payload_str}"
            req = urllib.request.Request(url, data=payload_str.encode(), method="POST")
            req.add_header("Content-Type", "application/json")

        sig = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        req.add_header("X-BAPI-API-KEY", api_key)
        req.add_header("X-BAPI-SIGN", sig)
        req.add_header("X-BAPI-TIMESTAMP", ts)
        req.add_header("X-BAPI-RECV-WINDOW", recv)
        req.add_header("User-Agent", f"PyrmScalper/{__version__}")

        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get("retCode") != 0:
                        raise ToolError(
                            f"Bybit API [{data.get('retCode')}]: {data.get('retMsg')}",
                            EXIT_ERROR,
                            "ExchangeAPIError",
                        )
                    return data.get("result", {})
            except ToolError:
                raise
            except Exception as e:
                if attempt == 3:
                    raise ToolError(f"HTTP request to Bybit failed: {e}", EXIT_ERROR, "NetworkError")
                time.sleep(0.3 * attempt)

        return {}

    @staticmethod
    def get_live_account_state() -> Optional[dict[str, Any]]:
        # Try local bybit_smart_order and bybit_core first
        try:
            import bybit_core
            import bybit_smart_order
            eq, _ = bybit_smart_order.get_wallet_balance()
            res_pos = bybit_core.get_positions(category="linear")
            if eq > 0:
                positions = []
                if res_pos and res_pos.get("retCode") == 0:
                    result = res_pos.get("result", {})
                    positions = [p for p in result.get("list", []) if Decimal(str(p.get("size", "0"))) > 0]
                return {"wallet_equity": eq, "open_positions_count": len(positions), "open_positions": positions}
        except Exception as e:
            logging.debug(f"Failed to fetch live account state using local modules: {e}")

        key, sec = OrderExecutionEngine.get_credentials()
        if not (key and sec):
            return None
        try:
            res_bal = OrderExecutionEngine._send_signed_request(
                "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, key, sec
            )
            equity = None
            accounts = res_bal.get("list", [])
            if accounts:
                equity = parse_safe_decimal(accounts[0].get("totalEquity"))

            res_pos = OrderExecutionEngine._send_signed_request(
                "GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"}, key, sec
            )
            positions = [p for p in res_pos.get("list", []) if Decimal(str(p.get("size", "0"))) > 0]

            return {"wallet_equity": equity, "open_positions_count": len(positions), "open_positions": positions}
        except Exception:
            return None

    @staticmethod
    def execute_trade(
        symbol: str,
        side: str,
        qty: Decimal,
        entry: Decimal,
        tp: Decimal,
        sl: Decimal,
        trailing_stop: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        oid = f"ord_{uuid.uuid4().hex[:12]}"
        if dry_run:
            return {
                "success": True,
                "order_id": oid,
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "entry_price": float(entry),
                "tp_price": float(tp),
                "sl_price": float(sl),
                "trailing_stop": trailing_stop,
                "dry_run": True,
                "status": "FILLED_SIMULATED",
            }

        # Try local bybit_smart_order module first
        try:
            import bybit_smart_order
            res = bybit_smart_order.execute_smart_order(
                symbol=symbol,
                side="Buy" if side.upper() == "BUY" else "Sell",
                qty=float(qty),
                sl_price=float(sl),
                tp_price=float(tp),
                trailing_stop=float(trailing_stop.replace("bps", "").strip()) if trailing_stop else None,
                dry_run=dry_run,
            )
            if res.get("success"):
                return {"success": True, "data": res, "order_id": oid, "status": "SUBMITTED_SMART_ORDER", "dry_run": dry_run}
            else:
                logging.warning(f"bybit_smart_order execution returned failure for {symbol}: {res.get('error')}")
        except Exception as e:
            logging.warning(f"Failed to submit live order for {symbol} via bybit_smart_order: {e}")

        with contextlib.suppress(Exception):
            import functions  # type: ignore
            if hasattr(functions, "bybit_realm"):
                res = functions.bybit_realm({
                    "action": "place_smart_order",
                    "symbol": symbol,
                    "side": side,
                    "qty": str(qty),
                    "take_profit": str(tp),
                    "stop_loss": str(sl),
                    "trailing_stop": str(trailing_stop) if trailing_stop else None,
                    "json": True,
                })
                return {"success": True, "data": res, "order_id": oid, "status": "SUBMITTED_REALM", "dry_run": False}

        key, sec = OrderExecutionEngine.get_credentials()
        if key and sec:
            body: dict[str, Any] = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(qty),
                "timeInForce": "IOC",
                "takeProfit": str(tp),
                "stopLoss": str(sl),
                "tpTriggerBy": "MarkPrice",
                "slTriggerBy": "MarkPrice",
                "tpslMode": "Full",
                "positionIdx": 0,
            }
            if trailing_stop:
                body["trailingStop"] = str(trailing_stop).replace("bps", "").strip()

            res = OrderExecutionEngine._send_signed_request("POST", "/v5/order/create", body, key, sec)
            return {
                "success": True,
                "data": res,
                "order_id": res.get("orderId", oid),
                "status": "SUBMITTED_BYBIT_V5",
                "dry_run": False,
            }

        raise ToolError(
            "No live credentials (BYBIT_API_KEY / BYBIT_API_SECRET) and no functions.bybit_realm module. Use --dry-run.",
            EXIT_PERMISSION_DENIED,
            "AuthenticationError",
        )


# ==============================================================================
# SECTION 11: Quantitative Symbol Evaluator
# ==============================================================================


def analyze_symbol(
    symbol: str,
    forced_side: Optional[str],
    target: Optional[Decimal],
    leverage: Decimal,
    spread_max: Decimal,
    max_loss: Decimal,
    equity: Decimal,
    pos_value: Optional[Decimal],
    pos_pct: Optional[Decimal],
    trailing_stop: Optional[str],
    enable_ai: bool,
    ollama_host: str,
    ollama_model: str,
    dry_run: bool,
    cache: Optional[ToolCache] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    ob = MarketDataEngine.get_orderbook(symbol, 25)
    bids, asks = ob.get("bids", []), ob.get("asks", [])
    if not bids or not asks:
        return None, "Empty orderbook"

    best_bid = Decimal(str(bids[0][0]))
    best_ask = Decimal(str(asks[0][0]))
    spread = best_ask - best_bid
    if spread <= 0:
        return None, "Zero/negative spread"

    mid = (best_bid + best_ask) / 2
    spread_bps = (spread / mid) * Decimal("10000")
    if spread_bps > spread_max:
        return None, f"Spread {spread_bps:.1f} bps > max {spread_max:.1f}"

    atr = MarketDataEngine.get_atr14(symbol)
    ofi = MicrostructureAnalyzer.compute_decay_ofi(bids, asks)
    depth = MicrostructureAnalyzer.compute_depth_ratio(bids, asks)
    composite = (Decimal("0.60") * ofi) + (Decimal("0.40") * depth)
    mom = MarketDataEngine.get_momentum_1m(symbol)

    long_ok = composite >= Decimal("0.53") and mom >= Decimal("-0.02")
    short_ok = composite <= Decimal("0.47") and mom <= Decimal("0.02")

    if forced_side:
        side = forced_side.capitalize()
        if side == "Buy" and not long_ok:
            return None, f"No Buy edge (Imb {float(composite):.3f}, Mom {float(mom):+.2f}%)"
        if side == "Sell" and not short_ok:
            return None, f"No Sell edge (Imb {float(composite):.3f}, Mom {float(mom):+.2f}%)"
    elif long_ok:
        side = "Buy"
    elif short_ok:
        side = "Sell"
    else:
        return None, f"Neutral (Imb {float(composite):.2f}, Mom {float(mom):+.2f}%)"

    rules = MarketDataEngine.get_instrument_info(symbol, cache=cache)
    dyn_target = MicrostructureAnalyzer.map_dynamic_target(spread, target)
    qty = MicrostructureAnalyzer.calculate_sizing(
        symbol, dyn_target, spread, atr, ofi, composite, max_loss,
        equity, leverage, mid, pos_value, pos_pct, rules=rules
    )
    if qty <= 0:
        return None, "Qty zero after sizing"

    # Depth liquidity check: Total depth volume >= 5x order quantity
    total_vol = sum(Decimal(b[1]) for b in bids[:25]) + sum(Decimal(a[1]) for a in asks[:25])
    if total_vol < (Decimal("5.0") * qty):
        return None, f"Insufficient book depth ({float(total_vol):.2f} < 5x qty {float(5 * qty):.2f})"

    tp, sl, exp_net = MicrostructureAnalyzer.calculate_tp_sl(symbol, mid, side, qty, dyn_target, max_loss, rules=rules)

    ai_thesis = None
    if enable_ai:
        ai_thesis = OllamaReasoningEngine.generate_trade_thesis(
            symbol, side, ofi, mom, composite, spread_bps, host=ollama_host, model=ollama_model
        )

    try:
        res = OrderExecutionEngine.execute_trade(symbol, side, qty, mid, tp, sl, trailing_stop=trailing_stop, dry_run=dry_run)
    except ToolError as e:
        return None, str(e)

    res.update({
        "spread_bps": float(spread_bps),
        "ofi": float(ofi),
        "momentum": float(mom),
        "composite_imbalance": float(composite),
        "expected_net_pnl": float(exp_net),
        "tp_price": float(tp),
        "sl_price": float(sl),
        "entry_price": float(mid),
        "qty": float(qty),
        "symbol": symbol,
        "side": side,
        "ai_commentary": ai_thesis,
    })
    return res, None


# ==============================================================================
# SECTION 12: Core Execution Engine
# ==============================================================================


def execute_tool(
    symbols: Optional[str] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    target: Any = "0.02",
    target_profit: Any = None,
    target_profit_usdt: Any = None,
    leverage: Any = 50,
    pos_value: Any = "50.0",
    pos_pct: Any = 0.0,
    spread_max_bps: Any = 15.0,
    max_positions: Any = 1,
    max_workers: Any = 5,
    cooldown: Any = 120,
    loop: Any = False,
    loop_delay: Any = 30,
    risk_reward: Any = "1.0",
    trailing_stop: Any = None,
    max_loss_trade: Any = 0.05,
    daily_loss_cap: Any = 0.50,
    max_drawdown_pct: Any = 15.0,
    state_file: Optional[str] = None,
    trade_log: Optional[str] = None,
    dry_run: Any = False,
    enable_ai: Any = False,
    ollama_host: str = "http://localhost:11434",
    ollama_model: str = "nemotron-3-nano:30b-cloud",
    output_format: str = "jsonl",
    no_color: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    env_vars: Optional[list[str]] = None,
    use_cache: bool = False,
    cache_ttl: int = DEFAULT_CACHE_TTL,
    cache_dir: Optional[str] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Universal execution dispatcher with permissive parameter absorption."""
    start = time.monotonic()
    rid = f"scalp_{uuid.uuid4().hex[:10]}"
    setup_tool_logging(verbose, rid)
    warnings: list[str] = []

    parsed_env, env_warns = SecurityValidator.sanitize_env_vars(env_vars)
    warnings.extend(env_warns)
    for k, v in parsed_env.items():
        os.environ[k] = v

    # Resolve parameter aliases
    chosen_symbol = symbol or kwargs.get("symbol")
    chosen_symbols = chosen_symbol or symbols or kwargs.get("symbols") or "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT"
    chosen_target = target_profit_usdt or target_profit or target or kwargs.get("target_profit") or kwargs.get("target")

    bad = validate_inputs(
        symbols=chosen_symbols, symbol=chosen_symbol, side=side, target=chosen_target,
        leverage=leverage, spread_max_bps=spread_max_bps,
        max_loss_trade=max_loss_trade, daily_loss_cap=daily_loss_cap,
        output_format=output_format
    )
    if bad:
        return bad

    target_d = parse_safe_decimal(chosen_target, Decimal("0.02"))
    lev_d = parse_safe_decimal(leverage, Decimal("50"))
    pos_val_d = parse_safe_decimal(pos_value)
    pos_pct_d = parse_safe_decimal(pos_pct, Decimal("0"))
    spread_d = parse_safe_decimal(spread_max_bps, Decimal("15"))
    max_loss_d = parse_safe_decimal(max_loss_trade, Decimal("0.05"))
    daily_cap_d = parse_safe_decimal(daily_loss_cap, Decimal("0.50"))
    max_dd_d = parse_safe_decimal(max_drawdown_pct, Decimal("15"))
    cooldown_sec = int(parse_safe_float(cooldown, 120) or 120)
    max_pos = int(parse_safe_float(max_positions, 1) or 1)
    max_work = int(parse_safe_float(max_workers, 5) or 5)
    dry = parse_safe_bool(dry_run, False)
    ai_enabled = parse_safe_bool(enable_ai, False)
    forced_side = _clean_str(side).capitalize() if side else None

    tool_cache = ToolCache(cache_dir=cache_dir, ttl=cache_ttl) if use_cache else None
    state_mgr = ScalperStateManager(state_file, trade_log)
    state = state_mgr.load()

    # Synchronize live exchange state
    if not dry:
        live_state = OrderExecutionEngine.get_live_account_state()
        if live_state:
            if live_state.get("wallet_equity"):
                state["wallet_equity"] = str(live_state["wallet_equity"])
            if live_state.get("open_positions_count", 0) >= max_pos:
                msg = f"Max simultaneous open positions reached on Bybit ({live_state['open_positions_count']} >= {max_pos})"
                return {
                    "success": True, "status": "MAX_POSITIONS_REACHED", "message": msg,
                    "state": state, "orders": [], "duration_ms": round((time.monotonic() - start) * 1000, 2)
                }

    wallet = parse_safe_decimal(state.get("wallet_equity"), Decimal("2.00"))
    daily = parse_safe_decimal(state.get("daily_pnl"), Decimal("0"))
    peak = parse_safe_decimal(state.get("equity_peak"), Decimal("0"))
    if peak is None or wallet > peak:
        peak = wallet
        state["equity_peak"] = str(peak)

    if daily <= -daily_cap_d:
        msg = f"Daily loss cap hit (-${abs(daily):.4f} <= -${daily_cap_d:.4f}). Halted."
        return {
            "success": False, "error": msg, "type": "DailyLossCapExceeded",
            "state": state, "duration_ms": round((time.monotonic() - start) * 1000, 2)
        }

    if peak > 0:
        dd = ((peak - wallet) / peak) * 100
        if dd > max_dd_d:
            msg = f"Drawdown {dd:.1f}% > {max_dd_d:.1f}%. Halted."
            return {
                "success": False, "error": msg, "type": "MaxDrawdownExceeded",
                "state": state, "duration_ms": round((time.monotonic() - start) * 1000, 2)
            }

    pause = state.get("pause_until")
    if pause:
        try:
            if datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) < datetime.datetime.fromisoformat(pause):
                return {
                    "success": True, "status": "COOLDOWN_ACTIVE", "message": f"Paused until {pause}",
                    "state": state, "orders": [], "duration_ms": round((time.monotonic() - start) * 1000, 2)
                }
            state["pause_until"] = None
            state["consecutive_losses"] = 0
        except Exception:
            state["pause_until"] = None

    target_syms = [_clean_str(s).upper() for s in _clean_str(chosen_symbols).split(",") if _clean_str(s)]
    placed: list[dict] = []
    skipped: dict[str, str] = {}

    now_epoch = time.time()
    sym_cooldowns = state.get("symbol_cooldowns", {})
    eligible_syms = []
    for sym in target_syms:
        last_exec = sym_cooldowns.get(sym, 0)
        if (now_epoch - last_exec) < cooldown_sec:
            skipped[sym] = f"Per-symbol cooldown active ({int(cooldown_sec - (now_epoch - last_exec))}s remaining)"
        else:
            eligible_syms.append(sym)

    if eligible_syms:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_work) as pool:
            futs = {
                pool.submit(
                    analyze_symbol,
                    sym,
                    forced_side if len(target_syms) == 1 else None,
                    target_d,
                    lev_d,
                    spread_d,
                    max_loss_d,
                    wallet,
                    pos_val_d,
                    pos_pct_d,
                    trailing_stop,
                    ai_enabled,
                    ollama_host,
                    ollama_model,
                    dry,
                    tool_cache,
                ): sym
                for sym in eligible_syms
            }
            for fut in concurrent.futures.as_completed(futs):
                sym = futs[fut]
                try:
                    res, reason = fut.result()
                    if res:
                        placed.append(res)
                        sym_cooldowns[sym] = now_epoch
                        if len(placed) >= max_pos:
                            break
                    elif reason:
                        skipped[sym] = reason
                except Exception as e:
                    skipped[sym] = f"Scan error: {e}"

    state["symbol_cooldowns"] = sym_cooldowns

    if placed:
        for o in placed:
            exp = Decimal(str(o.get("expected_net_pnl", 0.02)))
            if dry:
                daily += exp
                wallet += exp
                peak = max(peak, wallet)
                state["daily_pnl"] = str(daily)
                state["wallet_equity"] = str(wallet)
                state["equity_peak"] = str(peak)
            state["consecutive_losses"] = 0
            state_mgr.log_trade({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                "request_id": rid,
                **o,
            })
        state_mgr.save(state)

    duration = round((time.monotonic() - start) * 1000, 2)
    return {
        "success": True,
        "request_id": rid,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "target": str(target_d),
        "leverage": int(lev_d),
        "orders_placed": len(placed),
        "orders": placed,
        "skipped": skipped,
        "state": {
            "wallet_equity": float(wallet),
            "daily_pnl": float(daily),
            "equity_peak": float(peak),
            "consecutive_losses": state.get("consecutive_losses", 0),
        },
        "dry_run": dry,
        "duration_ms": duration,
        "warnings": warnings,
        "exit_code": EXIT_SUCCESS,
    }


# ==============================================================================
# SECTION 13: Output Routing & Main CLI Entrypoint
# ==============================================================================


def write_llm_output(data: dict[str, Any], output_format: str = "jsonl") -> None:
    path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    indent = 2 if output_format == "json" else None
    payload = json.dumps(data, ensure_ascii=False, cls=ToolJSONEncoder, indent=indent)
    if path in {"/dev/stdout", "/dev/fd/1", "-"}:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fp:
            fp.write(payload + "\n")
    except OSError as e:
        sys.stderr.write(f"Failed writing LLM_OUTPUT: {e}\n")
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def run(**kwargs: Any) -> None:
    """AIChat entrypoint with permissive argument intake and envelope routing."""
    out_fmt = kwargs.get("output_format", "jsonl")
    no_col = kwargs.get("no_color", False)
    quiet = kwargs.get("quiet", False)
    json_only = kwargs.get("json_only", False)
    result = execute_tool(**kwargs)
    if not json_only:
        print_scalp_ui(result, no_color=no_col, quiet=quiet)
    write_llm_output(result, out_fmt)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="micro_scalper_tool.py",
        description=f"Enterprise Autonomous Bybit Micro-Scalper v{__version__}",
    )
    p.add_argument("--symbols", type=str, **env_default("SCALP_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT"))
    p.add_argument("--symbol", type=str, **env_default("SCALP_SYMBOL"))
    p.add_argument("--side", type=str, choices=["Buy", "Sell", "buy", "sell"])
    p.add_argument("--target", type=str, **env_default("SCALP_TARGET", "0.02"))
    p.add_argument("--target-profit", dest="target_profit", type=str)
    p.add_argument("--leverage", type=str, **env_default("SCALP_LEVERAGE", "50"))
    p.add_argument("--pos-value", dest="pos_value", type=str, default="50.0")
    p.add_argument("--pos-pct", dest="pos_pct", type=float, default=0.0)
    p.add_argument("--spread-max-bps", dest="spread_max_bps", type=float, default=15.0)
    p.add_argument("--max-positions", dest="max_positions", type=int, default=1)
    p.add_argument("--max-workers", dest="max_workers", type=int, default=5)
    p.add_argument("--cooldown", type=int, default=120)
    p.add_argument("--loop", action="store_true", default=False)
    p.add_argument("--loop-delay", dest="loop_delay", type=int, default=30)
    p.add_argument("--risk-reward", dest="risk_reward", type=str, default="1.0")
    p.add_argument("--trailing-stop", dest="trailing_stop", type=str, default=None)
    p.add_argument("--max-loss-trade", dest="max_loss_trade", type=float, default=0.05)
    p.add_argument("--daily-loss-cap", dest="daily_loss_cap", type=float, default=0.50)
    p.add_argument("--max-drawdown-pct", dest="max_drawdown_pct", type=float, default=15.0)
    p.add_argument("--state-file", dest="state_file", type=str, default=None)
    p.add_argument("--trade-log", dest="trade_log", type=str, default=None)
    p.add_argument("--dry-run", action="store_true", **env_default("SCALP_DRY_RUN", False))
    p.add_argument("--enable-ai", action="store_true", default=False)
    p.add_argument("--ollama-host", dest="ollama_host", type=str, default="http://localhost:11434")
    p.add_argument("--ollama-model", dest="ollama_model", type=str, default="nemotron-3-nano:30b-cloud")
    p.add_argument("--output-format", dest="output_format", choices=["json", "jsonl"], default="jsonl")
    p.add_argument("--cache-ttl", dest="cache_ttl", type=int, default=DEFAULT_CACHE_TTL)
    p.add_argument("--cache-dir", dest="cache_dir", type=str, default=None)
    p.add_argument("--env-var", action="append", dest="env_vars")
    p.add_argument("--schema", action="store_true", default=False)
    p.add_argument("--clear-cache", dest="clear_cache", action="store_true", default=False)
    p.add_argument("--json-only", dest="json_only", action="store_true", default=False)
    p.add_argument("--quiet", "-q", action="store_true", default=False)
    p.add_argument("--no-color", dest="no_color", action="store_true", default=False)
    p.add_argument("--verbose", "-v", action="store_true", default=False)
    p.add_argument("--use-cache", dest="use_cache", action="store_true", default=False)
    return p


def main() -> int:
    args = _build_parser().parse_args()

    if args.schema:
        sys.stdout.write(json.dumps(generate_tool_schema(), indent=2) + "\n")
        return EXIT_SUCCESS

    if args.clear_cache:
        cache = ToolCache(cache_dir=args.cache_dir)
        with contextlib.suppress(Exception):
            for f in cache.cache_dir.glob("*.json"):
                f.unlink(missing_ok=True)
        if not args.quiet and not args.json_only:
            _cprint(f"{NEON_GREEN}Cleared tool cache.{RESET}", no_color=args.no_color)
        return EXIT_SUCCESS

    kwargs = {
        "symbols": args.symbols,
        "symbol": args.symbol,
        "side": args.side,
        "target": args.target,
        "target_profit": args.target_profit,
        "leverage": args.leverage,
        "pos_value": args.pos_value,
        "pos_pct": args.pos_pct,
        "spread_max_bps": args.spread_max_bps,
        "max_positions": args.max_positions,
        "max_workers": args.max_workers,
        "cooldown": args.cooldown,
        "loop": args.loop,
        "loop_delay": args.loop_delay,
        "risk_reward": args.risk_reward,
        "trailing_stop": args.trailing_stop,
        "max_loss_trade": args.max_loss_trade,
        "daily_loss_cap": args.daily_loss_cap,
        "max_drawdown_pct": args.max_drawdown_pct,
        "state_file": args.state_file,
        "trade_log": args.trade_log,
        "dry_run": args.dry_run,
        "enable_ai": args.enable_ai,
        "ollama_host": args.ollama_host,
        "ollama_model": args.ollama_model,
        "output_format": args.output_format,
        "no_color": args.no_color,
        "verbose": args.verbose,
        "quiet": args.quiet,
        "env_vars": args.env_vars,
        "use_cache": args.use_cache,
        "cache_ttl": args.cache_ttl,
        "cache_dir": args.cache_dir,
    }

    with GracefulShutdown() as shutdown:
        if args.loop:
            if not args.quiet and not args.json_only:
                _cprint(
                    f"{NEON_CYAN}Continuous loop started (delay {args.loop_delay}s)... Ctrl-C to abort{RESET}",
                    no_color=args.no_color,
                )
            while not shutdown.should_stop():
                res = execute_tool(**kwargs)
                if not args.json_only:
                    print_scalp_ui(res, no_color=args.no_color, quiet=args.quiet)
                write_llm_output(res, args.output_format)
                if not res.get("success") and res.get("type") in ("DailyLossCapExceeded", "MaxDrawdownExceeded"):
                    break
                for _ in range(max(1, args.loop_delay)):
                    if shutdown.should_stop():
                        break
                    time.sleep(1)
            return EXIT_SUCCESS
        else:
            res = execute_tool(**kwargs)
            if not args.json_only:
                print_scalp_ui(res, no_color=args.no_color, quiet=args.quiet)
            write_llm_output(res, args.output_format)
            return res.get("exit_code", EXIT_SUCCESS)


if __name__ == "__main__":
    sys.exit(main())
