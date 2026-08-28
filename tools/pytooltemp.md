Use this as a template for creating new python tools for aichat, make the tools complete with all functionality, thought out. 
#!/usr/bin/env python3
# ==============================================================================
# custom_tool.py — Pyrmethus AIChat / llm-functions Master Template v4.0.0
# AIChat/llm-functions compatible · Typed Python run() API · Colorized CLI · Safe Caching
#
# @describe Scan a file or directory safely and return structured JSON results. Designed for Sigoden llm-functions Python tool discovery with a typed run() function and Google-style Args documentation.
#
# @meta require-tools aichat
# @meta python-entrypoint run
# @meta read-only
#
# @option --target! <PATH>               Target file or directory path (required)
# @option --mode <MODE>                  Execution mode: summary/detailed (default: summary)
# @option --limit <NUM>                  Maximum items to process (default: 100, max: 100000)
# @option --timeout <SEC>                Maximum execution timeout in seconds (max: 86400)
# @option --file-pattern <PATTERN>       File glob pattern filter (e.g., *.py, *.txt)
# @option --max-size <BYTES>             Maximum file size in bytes to process
# @option --output-format <FORMAT>       Output envelope format: json/jsonl (default: jsonl)
# @option --cache-ttl <SEC>              Cache TTL in seconds (default: 3600)
# @option --cache-dir <PATH>             Custom cache storage directory
# @option --env-var <KEY=VALUE>          Custom environment variable override (repeatable)
# @flag   --recursive                    Process directories recursively
# @flag   --include-hidden               Include hidden files and directories
# @flag   --dry-run                      Simulate execution without modifying state or cache
# @flag   --use-cache                    Enable result caching for expensive operations
# @flag   --clear-cache                  Clear cache directory and exit
# @flag   --schema                       Print JSON Tool Schema for LLM registration and exit
# @flag   --json-only                    Output raw JSON/JSONL only (suppress UI box on stderr)
# @flag   --quiet                        Suppress non-essential progress and UI output
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug log output and tracebacks
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# @env LLM_TOOL_MODE=summary             Default execution mode override
# @env LLM_TOOL_LIMIT=100                Default processing limit override
# @env LLM_TOOL_CACHE_DIR                Default cache directory override
# ==============================================================================

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fnmatch
import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

__version__ = "4.0.0"
__all__ = [
    "run",
    "execute_tool",
    "main",
    "validate_inputs",
    "build_cache_key",
    "invalidate_cache",
    "generate_tool_schema",
    "ToolCache",
    "ToolError",
    "GracefulShutdown",
    "SecurityValidator",
    "get_agent_var",
    "get_builtin_var",
    "get_execution_context",
    "__version__",
]

# ==============================================================================
# SECTION 1: System Constants, Exit Codes & Exception Models
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_TIMEOUT = 124
EXIT_PERMISSION_DENIED = 126
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPTED = 130

MAX_LIMIT = 100_000
MAX_TIMEOUT = 86_400.0  # 24 hours
DEFAULT_CACHE_TTL = 3600
DEFAULT_CACHE_MAX_SIZE_MB = 512

VALID_MODES = {"summary", "detailed"}
VALID_OUTPUT_FORMATS = {"json", "jsonl"}

BLOCKED_ENV_VARS = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PATH",
        "SHELL",
        "SUDO_COMMAND",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
)


class ExecutionMode(str, Enum):
    SUMMARY = "summary"
    DETAILED = "detailed"


class OutputFormat(str, Enum):
    JSON = "json"
    JSONL = "jsonl"


class ToolError(Exception):
    """Structured exception model for predictable tool error propagation."""

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
        payload: dict[str, Any] = {
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
# SECTION 2: JSON Serialization & Sanitization Engine
# ==============================================================================


class ToolJSONEncoder(json.JSONEncoder):
    """Resilient JSON encoder handling rich stdlib and dynamic dataclass objects."""

    def default(self, obj: Any) -> Any:
        try:
            if isinstance(obj, Path):
                return str(obj)
            if isinstance(obj, Enum):
                return obj.value
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            if isinstance(obj, timedelta):
                return obj.total_seconds()
            if isinstance(obj, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address)):
                return str(obj)
            if isinstance(obj, bytes):
                return obj.decode("utf-8", errors="replace")
            if isinstance(obj, (set, frozenset)):
                return sorted(list(obj))
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)
            if hasattr(obj, "model_dump") and callable(obj.model_dump):  # Pydantic v2
                return obj.model_dump()
            if hasattr(obj, "dict") and callable(obj.dict):  # Pydantic v1
                return obj.dict()
            if isinstance(obj, Mapping):
                return dict(obj)
            if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
                return list(obj)
        except Exception:
            pass
        return repr(obj)


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
    """Remove ANSI escape codes."""
    return _ANSI_RE.sub("", text)


def _is_tty(no_color: bool = False) -> bool:
    """Check if color display is supported and allowed."""
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(text: str, file: Any = None, no_color: bool = False, end: str = "\n") -> None:
    """Print text with conditional ANSI color stripping."""
    target = file or sys.stderr
    if not _is_tty(no_color=no_color):
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def setup_tool_logging(verbose: bool = False, request_id: Optional[str] = None) -> None:
    """Initialize structured stderr logging."""
    level = logging.DEBUG if verbose else logging.INFO
    log_format = f"[%(asctime)s] [{request_id or 'init'}] [%(levelname)s] %(message)s"
    try:
        logging.basicConfig(level=level, format=log_format, force=True, stream=sys.stderr)
    except TypeError:
        logging.basicConfig(level=level, format=log_format, stream=sys.stderr)


def print_progress(current: int, total: int, message: str = "", no_color: bool = False) -> None:
    """Render interactive CLI progress bar."""
    if not _is_tty(no_color=no_color):
        return
    percent = (current / total) * 100.0 if total > 0 else 100.0
    bar_width = 25
    filled = int(bar_width * percent / 100.0)
    bar = "█" * filled + "░" * (bar_width - filled)

    _cprint(
        f"\r{NEON_CYAN}Progress:{RESET} [{NEON_GREEN}{bar}{RESET}] {percent:5.1f}% {DIM}{message[:35]}{RESET}",
        end="",
        no_color=no_color,
    )
    if current >= total:
        _cprint("", no_color=no_color)


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False, quiet: bool = False) -> None:
    """Render structured visual execution summary to stderr."""
    if quiet or not _is_tty(no_color=no_color):
        return

    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "SUCCESS" if success else "FAILED"

    box_w = 68
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}", no_color=no_color)
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [TOOL EXECUTION v{__version__}]{RESET} {status_color}{BOLD}{status_symbol} {status_text}{RESET}".ljust(
            box_w + 14 if _is_tty(no_color) else box_w
        )
        + f"{NEON_PURPLE}│{RESET}",
        no_color=no_color,
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Request ID:{RESET} {data.get('request_id', 'N/A')}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Target:{RESET}     {data.get('target', 'N/A')}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Mode:{RESET}       {data.get('mode', 'N/A')}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Matches:{RESET}    {NEON_YELLOW}{data.get('count', 0)}{RESET}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Cached:{RESET}     {NEON_YELLOW}{data.get('cached', False)}{RESET}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Duration:{RESET}   {DIM}{data.get('duration_ms', 0)} ms{RESET}", no_color=no_color)

    warnings = data.get("warnings", [])
    if warnings:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        for warn in warnings:
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}⚠ Warning:{RESET} {warn}", no_color=no_color)

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error [{data.get('type', 'Error')}]:{RESET} {data['error']}", no_color=no_color)

    items = data.get("items", [])
    if items:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Processed Items Preview ({len(items)} shown):{RESET}", no_color=no_color)
        for item in items[:8]:
            _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}›{RESET} {item}", no_color=no_color)
        if len(items) > 8:
            _cprint(f"{NEON_PURPLE}│{RESET}   {DIM}... and {len(items) - 8} more items{RESET}", no_color=no_color)

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}", no_color=no_color)


# ==============================================================================
# SECTION 4: Security & Path Validation
# ==============================================================================


class SecurityValidator:
    """Security validation engine protecting against path traversal and injection."""

    @staticmethod
    def is_safe_glob(pattern: Optional[str]) -> bool:
        if not pattern:
            return True
        # Reject null bytes or dangerous patterns
        if "\x00" in pattern:
            return False
        try:
            re.compile(fnmatch.translate(pattern))
            return True
        except Exception:
            return False

    @staticmethod
    def sanitize_env_vars(env_vars: Optional[list[str]]) -> tuple[dict[str, str], list[str]]:
        """Filter out dangerous environment variables with warnings."""
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
    target: Optional[str],
    mode: str = "summary",
    limit: Optional[int] = None,
    timeout: Optional[float] = None,
    file_pattern: Optional[str] = None,
    max_size: Optional[int] = None,
    output_format: str = "jsonl",
) -> Optional[dict[str, Any]]:
    """Strict validation layer for tool arguments."""
    trace_id = str(uuid.uuid4())

    if not target or not str(target).strip():
        return ToolError("Target path is required.", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()

    if mode not in VALID_MODES:
        return ToolError(
            f"Invalid mode '{mode}'. Allowed: {sorted(list(VALID_MODES))}",
            EXIT_INVALID_INPUT,
            "ValidationError",
            trace_id=trace_id,
        ).to_dict()

    if output_format not in VALID_OUTPUT_FORMATS:
        return ToolError(
            f"Invalid output format '{output_format}'. Allowed: {sorted(list(VALID_OUTPUT_FORMATS))}",
            EXIT_INVALID_INPUT,
            "ValidationError",
            trace_id=trace_id,
        ).to_dict()

    if limit is not None:
        if limit < 0:
            return ToolError(f"Limit must be >= 0 (received: {limit}).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
        if limit > MAX_LIMIT:
            return ToolError(f"Limit exceeds maximum allowed ({MAX_LIMIT}).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()

    if timeout is not None:
        if timeout <= 0:
            return ToolError(f"Timeout must be > 0s (received: {timeout}).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()
        if timeout > MAX_TIMEOUT:
            return ToolError(f"Timeout exceeds maximum allowed ({MAX_TIMEOUT}s).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()

    if max_size is not None and max_size < 0:
        return ToolError(f"max_size must be >= 0 (received: {max_size}).", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()

    if file_pattern and not SecurityValidator.is_safe_glob(file_pattern):
        return ToolError(f"Invalid or unsafe glob pattern: '{file_pattern}'.", EXIT_INVALID_INPUT, "ValidationError", trace_id=trace_id).to_dict()

    return None


# ==============================================================================
# SECTION 5: Agent Execution Context & Path Resolution Helpers
# ==============================================================================


def get_agent_var(name: str, default: str = "") -> str:
    """Access agent user-defined variables (LLM_AGENT_VAR_<NAME>)."""
    return os.environ.get(f"LLM_AGENT_VAR_{name.upper()}", default)


def get_builtin_var(name: str) -> Optional[str]:
    """Access agent built-in environment variables (e.g., __cwd__, __os__)."""
    return (
        os.environ.get(f"LLM_AGENT_VAR_{name}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.lower()}")
        or os.environ.get(f"LLM_AGENT_VAR_{name.upper()}")
    )


def get_execution_context() -> dict[str, Any]:
    """Gather complete host runtime context."""
    termux_prefix = os.environ.get("PREFIX", "")
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME", "custom_tool"),
        "cache_dir": os.environ.get("LLM_TOOL_CACHE_DIR"),
        "root_dir": os.environ.get("LLM_ROOT_DIR"),
        "output_path": os.environ.get("LLM_OUTPUT", "/dev/stdout"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "host": {
            "os": platform.system(),
            "arch": platform.machine(),
            "python_version": platform.python_version(),
            "is_termux": "com.termux" in termux_prefix or Path("/data/data/com.termux").exists(),
        },
    }


def resolve_agent_path(target_str: str) -> Path:
    """Resolve target path relative to Agent CWD (__cwd__) safely."""
    raw_path = Path(target_str).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve(strict=False)

    agent_cwd = get_builtin_var("__cwd__")
    if agent_cwd:
        return (Path(agent_cwd) / raw_path).resolve(strict=False)

    return raw_path.resolve(strict=False)


def env_default(env_name: str, fallback: Any = None) -> dict[str, Any]:
    """Supply argparse defaults from environment variables without coercing types.

    Callers that need typed values should pass ``type=...`` to argparse; this
    helper only supplies the raw environment default and preserves the original
    template's compatibility.
    """
    val = os.getenv(env_name)
    if val is not None:
        return {"default": val}
    if fallback is not None:
        return {"default": fallback}
    return {}


# ==============================================================================
# SECTION 6: Cache Engine v3 (File-Locked, Size-Bounded, LRU-Evicting)
# ==============================================================================


def build_cache_key(
    target_path: Path,
    mtime: float,
    mode: str,
    limit_val: int,
    file_pattern: Optional[str],
    recursive: bool,
    max_size: Optional[int] = None,
    include_hidden: bool = False,
) -> str:
    """Construct deterministic cache key including path attributes and metadata."""
    is_dir = "dir" if target_path.is_dir() else "file"
    try:
        stat = target_path.stat()
        fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}:{getattr(stat, 'st_ino', 0)}"
    except OSError:
        fingerprint = str(mtime)
    return "|".join([
        __version__,
        str(target_path),
        is_dir,
        fingerprint,
        mode,
        str(limit_val),
        file_pattern or "",
        str(recursive),
        str(max_size if max_size is not None else ""),
        str(include_hidden),
    ])


class ToolCache:
    """Thread-safe and process-safe caching utility with automated LRU cleanup."""

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
            self.cache_dir = Path.home() / ".cache" / "aichat_tools"

        self.ttl = ttl
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._lock = threading.Lock()

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _hash_key(self, key_str: str) -> str:
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()

    @contextlib.contextmanager
    def _file_lock(self, lock_file: Path) -> Iterator[None]:
        """Simple cross-platform file locking."""
        acquired = False
        start = time.monotonic()
        while time.monotonic() - start < 3.0:
            try:
                # O_EXCL provides atomic creation semantics
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.close(fd)
                acquired = True
                break
            except (OSError, FileExistsError):
                time.sleep(0.05)
        if not acquired:
            raise TimeoutError(f"Timed out acquiring cache lock: {lock_file}")
        try:
            yield
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    lock_file.unlink(missing_ok=True)

    def get(self, key_str: str) -> Optional[dict[str, Any]]:
        """Retrieve unexpired payload from disk."""
        cache_file = self.cache_dir / f"{self._hash_key(key_str)}.json"
        if not cache_file.exists():
            return None

        try:
            stat = cache_file.stat()
            if (time.time() - stat.st_mtime) > self.ttl:
                cache_file.unlink(missing_ok=True)
                return None

            with open(cache_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, dict):
                    # Update access time for LRU tracking
                    with contextlib.suppress(OSError):
                        os.utime(cache_file, None)
                    return data
        except Exception:
            with contextlib.suppress(OSError):
                cache_file.unlink(missing_ok=True)
        return None

    def set(self, key_str: str, value: Any) -> None:
        """Atomically persist payload to cache and run LRU eviction if quota exceeded."""
        key_hash = self._hash_key(key_str)
        cache_file = self.cache_dir / f"{key_hash}.json"
        tmp_file = self.cache_dir / f"{key_hash}.tmp.{os.getpid()}"
        lock_file = self.cache_dir / f"{key_hash}.lock"

        with self._lock, self._file_lock(lock_file):
            try:
                payload = json.dumps(value, cls=ToolJSONEncoder, ensure_ascii=False)
                with open(tmp_file, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                tmp_file.replace(cache_file)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp_file.unlink(missing_ok=True)

        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """Evict oldest cache entries if total storage exceeds size quota."""
        try:
            files = list(self.cache_dir.glob("*.json"))
            total_size = sum(f.stat().st_size for f in files if f.is_file())
            if total_size <= self.max_size_bytes:
                return

            # Sort by mtime ascending (oldest first)
            files.sort(key=lambda f: f.stat().st_mtime)
            for f in files:
                if total_size <= self.max_size_bytes:
                    break
                try:
                    sz = f.stat().st_size
                    f.unlink(missing_ok=True)
                    total_size -= sz
                except OSError:
                    pass
        except Exception:
            pass


def invalidate_cache(cache: Optional[ToolCache] = None, prefix: str = "") -> int:
    """Evict all or prefix-matched cache records from disk."""
    cache_obj = cache or ToolCache()
    removed = 0
    if not cache_obj.cache_dir.exists():
        return removed
    for file in cache_obj.cache_dir.glob("*.json"):
        if not prefix or file.name.startswith(prefix):
            try:
                file.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


# ==============================================================================
# SECTION 7: Signal Interception & Tool Schema Specification
# ==============================================================================


class GracefulShutdown:
    """Context manager for intercepting termination signals cleanly."""

    def __init__(self) -> None:
        self.interrupted = False
        self._old_sigint = None
        self._old_sigterm = None

    def __enter__(self) -> GracefulShutdown:
        self.interrupted = False
        if threading.current_thread() is threading.main_thread():
            try:
                self._old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
                self._old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
            except (ValueError, AttributeError):
                pass
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.restore()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def restore(self) -> None:
        if threading.current_thread() is threading.main_thread():
            if self._old_sigint is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    signal.signal(signal.SIGINT, self._old_sigint)
            if self._old_sigterm is not None:
                with contextlib.suppress(ValueError, AttributeError):
                    signal.signal(signal.SIGTERM, self._old_sigterm)

    def should_stop(self) -> bool:
        return self.interrupted


def generate_tool_schema() -> dict[str, Any]:
    """Return the JSON declaration for the LLM-facing ``run`` function.

    The declaration mirrors the public Python ``run()`` signature used by
    Sigoden llm-functions. ``execute_tool()`` and the CLI remain richer
    implementation interfaces and are intentionally not exposed to the LLM.
    """
    return {
        "name": "run",
        "description": (
            "Safely scan a file or directory and return structured JSON "
            "metadata and matched paths. Read-only operation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "File or directory path to inspect.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["summary", "detailed"],
                    "description": "Return a short preview or all matched items.",
                    "default": "summary",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_LIMIT,
                    "description": "Maximum number of matched filesystem items.",
                    "default": 100,
                },
                "timeout": {
                    "type": ["number", "null"],
                    "exclusiveMinimum": 0,
                    "maximum": MAX_TIMEOUT,
                    "description": "Optional scan timeout in seconds.",
                },
                "file_pattern": {
                    "type": ["string", "null"],
                    "description": "Optional filename glob such as '*.py'.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively scan subdirectories.",
                    "default": False,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files and directories.",
                    "default": False,
                },
                "max_size": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "Optional maximum file size in bytes.",
                },
                "use_cache": {
                    "type": "boolean",
                    "description": "Reuse a matching cached result.",
                    "default": False,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Compatibility flag; the scanner is read-only.",
                    "default": False,
                },
            },
            "required": [
                "target",
                "mode",
                "limit",
                "timeout",
                "file_pattern",
                "recursive",
                "include_hidden",
                "max_size",
                "use_cache",
                "dry_run",
            ],
            "additionalProperties": False,
        },
    }


# ==============================================================================
# SECTION 8: Filesystem Scanner Engine
# ==============================================================================


class FileScanner:
    """Safe directory scanner guarding against infinite symlink cycles and file floods."""

    def __init__(
        self,
        base_path: Path,
        recursive: bool = False,
        pattern: Optional[str] = None,
        max_size: Optional[int] = None,
        include_hidden: bool = False,
        limit: int = 100,
        timeout: Optional[float] = None,
        start_time: float = 0.0,
        shutdown: Optional[GracefulShutdown] = None,
    ) -> None:
        self.base_path = base_path
        self.recursive = recursive
        self.pattern = pattern
        self.max_size = max_size
        self.include_hidden = include_hidden
        self.limit = limit
        self.timeout = timeout
        self.start_time = start_time
        self.shutdown = shutdown
        self.visited_real_paths: set[str] = set()

    def scan(self) -> list[str]:
        if not self.base_path.is_dir():
            if self._is_candidate(self.base_path):
                return [str(self.base_path)]
            return []

        results: list[str] = []
        self._walk_dir(self.base_path, results)
        return results

    def _is_candidate(self, path: Path) -> bool:
        if not self.include_hidden and path.name.startswith("."):
            return False
        if self.pattern and not fnmatch.fnmatch(path.name, self.pattern):
            return False
        if self.max_size is not None and path.is_file():
            try:
                if path.stat().st_size > self.max_size:
                    return False
            except OSError:
                return False
        return True

    def _walk_dir(self, current_dir: Path, accumulator: list[str]) -> None:
        if self.timeout and (time.monotonic() - self.start_time) > self.timeout:
            raise ToolError(f"Execution timed out after {self.timeout}s", EXIT_TIMEOUT, "TimeoutError", recoverable=True)

        if self.shutdown and self.shutdown.should_stop():
            raise ToolError("Execution interrupted by user signal.", EXIT_INTERRUPTED, "InterruptError")

        try:
            real_dir = str(current_dir.resolve(strict=False))
        except OSError:
            return

        # Symlink cycle / infinite traversal protection
        if real_dir in self.visited_real_paths:
            return
        self.visited_real_paths.add(real_dir)

        try:
            entries = sorted(os.scandir(current_dir), key=lambda e: e.name)
        except (PermissionError, OSError):
            return

        sub_dirs: list[Path] = []

        for entry in entries:
            if len(accumulator) >= self.limit:
                break
            if not self.include_hidden and entry.name.startswith("."):
                continue

            entry_path = Path(entry.path)

            try:
                if entry.is_dir(follow_symlinks=False):
                    if self.recursive:
                        sub_dirs.append(entry_path)
                elif entry.is_file(follow_symlinks=True):
                    if self._is_candidate(entry_path):
                        accumulator.append(str(entry_path))
            except OSError:
                continue

        if self.recursive:
            for sub_dir in sub_dirs:
                if len(accumulator) >= self.limit:
                    break
                self._walk_dir(sub_dir, accumulator)


# ==============================================================================
# SECTION 9: Core Execution Engine
# ==============================================================================


def execute_tool(
    target: str,
    mode: str = "summary",
    limit: Optional[int] = None,
    timeout: Optional[float] = None,
    file_pattern: Optional[str] = None,
    env_vars: Optional[list[str]] = None,
    recursive: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
    cache_ttl: int = DEFAULT_CACHE_TTL,
    cache_dir: Optional[str] = None,
    max_size: Optional[int] = None,
    output_format: str = "jsonl",
    include_hidden: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    """Execute main tool logic and return structured result payload."""
    start_time = time.monotonic()
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    setup_tool_logging(verbose=verbose, request_id=request_id)
    warnings: list[str] = []

    if cache_ttl < 0:
        return ToolError(
            f"cache_ttl must be >= 0 (received: {cache_ttl}).",
            EXIT_INVALID_INPUT,
            "ValidationError",
            trace_id=request_id,
        ).to_dict()
    if cache_ttl > 31_536_000:
        return ToolError(
            "cache_ttl exceeds the maximum supported value (31536000 seconds).",
            EXIT_INVALID_INPUT,
            "ValidationError",
            trace_id=request_id,
        ).to_dict()

    # Step 1: Input Validation
    bad_inputs = validate_inputs(
        target=target,
        mode=mode,
        limit=limit,
        timeout=timeout,
        file_pattern=file_pattern,
        max_size=max_size,
        output_format=output_format,
    )
    if bad_inputs:
        return bad_inputs

    # Step 2: Environment Variable Sanitization
    parsed_env, env_warnings = SecurityValidator.sanitize_env_vars(env_vars)
    warnings.extend(env_warnings)
    if parsed_env:
        for k, v in parsed_env.items():
            os.environ[k] = v

    limit_val = limit if limit is not None else 100

    # Step 3: Agent CWD Path Resolution
    target_path = resolve_agent_path(target)
    if not target_path.exists():
        return ToolError(
            f"Target path does not exist: '{target}' (Resolved: '{target_path}')",
            EXIT_FILE_NOT_FOUND,
            "FileNotFoundError",
            trace_id=request_id,
        ).to_dict(verbose=verbose)

    # Step 4: Cache Lookup
    try:
        mtime = target_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    cache = ToolCache(cache_dir=cache_dir, ttl=cache_ttl)
    cache_key = build_cache_key(
        target_path=target_path,
        mtime=mtime,
        mode=mode,
        limit_val=limit_val,
        file_pattern=file_pattern,
        recursive=recursive,
        max_size=max_size,
        include_hidden=include_hidden,
    )

    if use_cache and not dry_run:
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            cached_result["cached"] = True
            cached_result["request_id"] = request_id
            cached_result["duration_ms"] = round((time.monotonic() - start_time) * 1000, 2)
            return cached_result

    # Step 5: Scanner Execution
    try:
        with GracefulShutdown() as shutdown:
            scanner = FileScanner(
                base_path=target_path,
                recursive=recursive,
                pattern=file_pattern,
                max_size=max_size,
                include_hidden=include_hidden,
                limit=limit_val,
                timeout=timeout,
                start_time=start_time,
                shutdown=shutdown,
            )
            processed_items = scanner.scan()

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)

        result: dict[str, Any] = {
            "success": True,
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool_version": __version__,
            "target": str(target_path),
            "mode": mode,
            "count": len(processed_items),
            "items": processed_items if mode == "detailed" else processed_items[:5],
            "dry_run": dry_run,
            "cached": False,
            "warnings": warnings,
            "duration_ms": duration_ms,
            "execution_ms": duration_ms,
            "context": get_execution_context(),
            "exit_code": EXIT_SUCCESS,
        }

        if use_cache and not dry_run:
            cache.set(cache_key, result)

        return result

    except ToolError as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        err_dict = exc.to_dict(verbose=verbose)
        err_dict["duration_ms"] = duration_ms
        err_dict["warnings"] = warnings
        return err_dict
    except PermissionError as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        return ToolError(
            f"Permission denied accessing path: {exc}",
            EXIT_PERMISSION_DENIED,
            "PermissionError",
            trace_id=request_id,
            details={"duration_ms": duration_ms, "warnings": warnings},
        ).to_dict(verbose=verbose)
    except Exception as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        return ToolError(
            f"Tool execution unexpected error: {exc}",
            EXIT_ERROR,
            "UnhandledException",
            trace_id=request_id,
            details={"duration_ms": duration_ms, "warnings": warnings},
        ).to_dict(verbose=verbose)


# ==============================================================================
# SECTION 10: Output Routing Engine
# ==============================================================================


def write_llm_output(data: dict[str, Any], output_format: str = "jsonl") -> None:
    """Write single-line JSON or formatted JSON to stdout or LLM_OUTPUT destination."""
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    indent = 2 if output_format == "json" else None
    payload = json.dumps(data, ensure_ascii=False, cls=ToolJSONEncoder, indent=indent)

    if out_path in {"/dev/stdout", "/dev/fd/1", "-"}:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return

    try:
        target_file = Path(out_path)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        with open(target_file, "a", encoding="utf-8") as fp:
            fp.write(payload + "\n")
    except OSError as err:
        sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


# ==============================================================================
# SECTION 11: AIChat Python API Entrypoint
# ==============================================================================


def run(
    target: str,
    mode: Literal["summary", "detailed"] = "summary",
    limit: int = 100,
    timeout: Optional[float] = None,
    file_pattern: Optional[str] = None,
    recursive: bool = False,
    include_hidden: bool = False,
    max_size: Optional[int] = None,
    use_cache: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan a file or directory and return structured, LLM-friendly results.

    This is the canonical Python entrypoint for Sigoden's llm-functions.
    Keep the signature typed and the Google-style ``Args`` documentation
    explicit because the tool declaration is generated from the Python
    function metadata.

    Args:
        target: File or directory path to inspect. Relative paths are resolved
            from AIChat's agent working directory when ``__cwd__`` is available.
        mode: Result detail level. ``summary`` returns a short item preview;
            ``detailed`` returns all matched items up to ``limit``.
        limit: Maximum number of filesystem items to return. Must be between
            0 and 100000.
        timeout: Optional maximum scan duration in seconds. Use null when no
            timeout is required.
        file_pattern: Optional filename glob such as ``*.py`` or ``*.json``.
        recursive: Recursively inspect subdirectories when true.
        include_hidden: Include dot-files and dot-directories when true.
        max_size: Optional maximum file size in bytes. Files larger than this
            value are excluded.
        use_cache: Reuse a previously computed result when the target metadata
            and scan options match.
        dry_run: Compatibility flag for read-only tool workflows. This scanner
            never modifies the target filesystem.

    Returns:
        A JSON-serializable dictionary containing ``success``, request
        metadata, matched items, warnings, timing, and execution context.
    """
    return execute_tool(
        target=target,
        mode=mode,
        limit=limit,
        timeout=timeout,
        file_pattern=file_pattern,
        recursive=recursive,
        use_cache=use_cache,
        no_color=True,
        verbose=False,
        cache_ttl=DEFAULT_CACHE_TTL,
        cache_dir=None,
        max_size=max_size,
        output_format="jsonl",
        include_hidden=include_hidden,
        dry_run=dry_run,
        quiet=True,
    )


# ==============================================================================
# SECTION 12: CLI Parser & Main Entrypoint
# ==============================================================================


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="custom_tool.py",
        description=f"Enterprise AIChat Tool Template v{__version__}",
    )
    parser.add_argument(
        "--target",
        "-t",
        required=False,
        metavar="PATH",
        help="Target file or directory path",
    )
    parser.add_argument(
        "--mode",
        choices=["summary", "detailed"],
        **env_default("LLM_TOOL_MODE", "summary"),
        help="Execution mode (default: summary)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        **env_default("LLM_TOOL_LIMIT"),
        help="Maximum items to process (default: 100)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="Maximum execution timeout in seconds",
    )
    parser.add_argument(
        "--file-pattern",
        dest="file_pattern",
        metavar="PATTERN",
        help="File glob pattern filter (e.g. *.py)",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        dest="max_size",
        metavar="BYTES",
        help="Maximum file size in bytes to include",
    )
    parser.add_argument(
        "--output-format",
        choices=["json", "jsonl"],
        default="jsonl",
        help="Output format serialization (default: jsonl)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        metavar="SEC",
        help="Cache record TTL in seconds (default: 3600)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        **env_default("LLM_TOOL_CACHE_DIR"),
        metavar="PATH",
        help="Custom cache directory path",
    )
    parser.add_argument(
        "--env-var",
        action="append",
        dest="env_vars",
        metavar="KEY=VALUE",
        help="Custom runtime environment variable override (repeatable)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        default=False,
        help="Process directories recursively",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        default=False,
        dest="include_hidden",
        help="Include hidden dot-files during directory scans",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Simulate execution without cache writes or file modifications",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=False,
        dest="use_cache",
        help="Enable result caching",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        default=False,
        dest="clear_cache",
        help="Clear cache directory and exit",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        default=False,
        help="Print JSON Tool Schema for LLM registration and exit",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        default=False,
        dest="json_only",
        help="Suppress stderr human UI and only emit JSON/JSONL output",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Suppress non-essential progress and UI output",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        dest="no_color",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable detailed debug logging and exception traces",
    )
    return parser


def main() -> int:
    """CLI execution entrypoint."""
    args = _build_parser().parse_args()

    if args.schema:
        schema = generate_tool_schema()
        sys.stdout.write(json.dumps(schema, indent=2) + "\n")
        sys.stdout.flush()
        return EXIT_SUCCESS

    if args.clear_cache:
        cache = ToolCache(cache_dir=args.cache_dir)
        removed = invalidate_cache(cache)
        if not args.quiet and not args.json_only:
            _cprint(f"{NEON_GREEN}Cleared {removed} cache record(s).{RESET}", no_color=args.no_color)
        return EXIT_SUCCESS

    if not args.target:
        if not args.json_only:
            _cprint(f"{NEON_RED}Error: --target parameter is required for execution.{RESET}", no_color=args.no_color)
        else:
            err_payload = ToolError("Target parameter is required.", EXIT_INVALID_INPUT, "ValidationError").to_dict()
            write_llm_output(err_payload, output_format=args.output_format)
        return EXIT_INVALID_INPUT

    res = execute_tool(
        target=args.target,
        mode=args.mode,
        limit=args.limit,
        timeout=args.timeout,
        file_pattern=args.file_pattern,
        env_vars=args.env_vars,
        recursive=args.recursive,
        use_cache=args.use_cache,
        no_color=args.no_color,
        verbose=args.verbose,
        cache_ttl=args.cache_ttl,
        cache_dir=args.cache_dir,
        max_size=args.max_size,
        output_format=args.output_format,
        include_hidden=args.include_hidden,
        dry_run=args.dry_run,
        quiet=args.quiet,
    )

    if not args.json_only:
        print_human_readable_ui(res, no_color=args.no_color, quiet=args.quiet)
    write_llm_output(res, output_format=args.output_format)
    return res.get("exit_code", EXIT_SUCCESS)


if __name__ == "__main__":
    sys.exit(main())
