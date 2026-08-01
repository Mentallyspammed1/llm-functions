#!/usr/bin/env python3
# ==============================================================================
# str_replace_editor.py — Pyrmethus AIChat Tool v2.0.2-ASCENDED
# argc/aichat compatible · Enhanced Human-Readable Colorized Outputs & Robustness
#
# @describe A robust file editor tool supporting view, literal/regex search,
#           replace, count, write, and batch operations. Supports multiline
#           replacements, escape decoding, dotall regex, and batch edits.
#
# @option --action! <ACTION>             Operation: view, replace, count, write, batch (required)
# @option --file-path! <PATH>            Path to the target file (required)
# @option --search <TEXT>                Search string/pattern (required for replace/count)
# @option --replacement <TEXT>           Replacement string (required for replace)
# @option --content <TEXT>               New content for write action
# @option --edits <JSON_OR_FILE>         JSON array string or file path for batch replacements
# @option --start-line <NUM>             Starting line (1-based) for view
# @option --end-line <NUM>               Ending line for view
# @option --encoding <ENC>               Specify file encoding (default: utf-8)
# @flag   --backup                       Create timestamped .bak before modifying
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug logging
# @flag   --regex                        Treat search as regex pattern
# @flag   --dry-run                      Simulate replace (no file write)
# @flag   --ignore-case                  Case-insensitive search/replace
# @flag   --decode-escapes               Decode \n, \t, \r in search/replacement strings
# @flag   --dotall                       Treat . in regex as matching newlines (re.DOTALL)
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import difflib
import enum
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union, Pattern, Callable, cast

__version__ = "2.0.1-ASCENDED"
__all__ = ["run", "execute_tool", "__version__"]

# ==============================================================================
# SECTION 1: Exit Codes & Exception Models
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_INVALID_INPUT = 3
EXIT_PERMISSION_DENIED = 126
EXIT_INTERRUPTED = 130


class ToolJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder handling Path, Enum, datetime, timedelta, bytes, and sets safely."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, enum.Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            return obj.total_seconds()
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        return super().default(obj)


# ==============================================================================
# SECTION 2: Color Palette & Formatting Helpers
# ==============================================================================

NEON_CYAN    = "\033[38;5;51m"
NEON_GREEN   = "\033[38;5;46m"
NEON_RED     = "\033[38;5;196m"
NEON_YELLOW  = "\033[38;5;226m"
NEON_PURPLE  = "\033[38;5;129m"
NEON_PINK    = "\033[38;5;198m"
NEON_BLUE    = "\033[38;5;39m"
RESET        = "\033[0m"
BOLD         = "\033[1m"
DIM          = "\033[2m"

_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])|\033\[[0-9;]*[a-zA-Z]"
)

# IMPROVEMENT 1: Pre-compiled regex patterns for performance
_ESCAPE_SEQ_RE = re.compile(r'\\([nrt0\\\'"])')
_UNICODE_ESCAPE_RE = re.compile(r'\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})')
_HEX_ESCAPE_RE = re.compile(r'\\x([0-9a-fA-F]{2})')
_OCTAL_ESCAPE_RE = re.compile(r'\\([0-7]{1,3})')


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    """Return True if stderr is attached to an interactive terminal and TERM is active."""
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(text: str, file: Any = None, no_color: bool = False) -> None:
    """Print pre-formatted ANSI text to stderr by default, stripping colors when needed."""
    target = file or sys.stderr
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True)


def _unescape_string(text: str) -> str:
    """Convert literal escape sequences (e.g. \\n, \\t, \\r, \\uXXXX, \\UXXXXXXXX, \\xXX, \\ooo) into actual characters safely."""
    if not text:
        return text

    def _repl(m: re.Match[str]) -> str:
        c = m.group(1)
        if c == 'n': return '\n'
        if c == 'r': return '\r'
        if c == 't': return '\t'
        if c == '0': return '\0'
        if c == '\\': return '\\'
        if c == '"': return '"'
        if c == "'": return "'"
        return m.group(0)

    # Handle standard escapes
    text = _ESCAPE_SEQ_RE.sub(_repl, text)
    
    # IMPROVEMENT 2: Extended escape sequence support (unicode, hex, octal)
    def _unicode_repl(m: re.Match[str]) -> str:
        try:
            if m.group(1):  # \uXXXX
                return chr(int(m.group(1), 16))
            if m.group(2):  # \UXXXXXXXX
                return chr(int(m.group(2), 16))
        except (ValueError, OverflowError):
            pass
        return m.group(0)
    
    def _hex_repl(m: re.Match[str]) -> str:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return m.group(0)
    
    def _octal_repl(m: re.Match[str]) -> str:
        try:
            val = int(m.group(1), 8)
            if val <= 0o377:  # Valid byte value
                return chr(val)
        except (ValueError, OverflowError):
            pass
        return m.group(0)
    
    text = _UNICODE_ESCAPE_RE.sub(_unicode_repl, text)
    text = _HEX_ESCAPE_RE.sub(_hex_repl, text)
    text = _OCTAL_ESCAPE_RE.sub(_octal_repl, text)
    
    return text


def _generate_diff(old_content: str, new_content: str, file_path: str, max_lines: int = 30) -> str:
    """Generate a unified diff summary between old and new content."""
    if old_content == new_content:
        return ""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=3,
        )
    )
    if not diff:
        return ""
    if len(diff) > max_lines:
        return "".join(diff[:max_lines]) + f"\n... ({len(diff) - max_lines} more diff lines truncated)\n"
    return "".join(diff)


def _is_binary(content: str) -> bool:
    """Determine if content appears to be binary by checking for null bytes and high control char ratio."""
    if '\x00' in content:
        return True
    # IMPROVEMENT 3: Better binary detection - check control character ratio
    if len(content) > 0:
        control_chars = sum(1 for c in content if ord(c) < 32 and c not in '\n\r\t\f\v')
        if control_chars / len(content) > 0.15:  # >15% control chars likely binary
            return True
    return False


def _parse_edits(edits_input: Any) -> list[dict[str, Any]]:
    """Parse batch edits input from JSON string, list of dicts, or file path safely."""
    if isinstance(edits_input, list):
        return edits_input
    if isinstance(edits_input, str):
        edits_str = edits_input.strip()
        if edits_str.startswith("[") or edits_str.startswith("{"):
            try:
                data = json.loads(edits_str)
                return data if isinstance(data, list) else [data]
            except json.JSONDecodeError:
                pass

        try:
            path_obj = Path(edits_str).expanduser().resolve()
            # IMPROVEMENT 4: Path traversal protection
            if not _is_safe_path(path_obj):
                raise ValueError(f"Path traversal attempt detected: {edits_str}")
            if path_obj.is_file():
                content = path_obj.read_text(encoding="utf-8")
                data = json.loads(content)
                return data if isinstance(data, list) else [data]
            elif not edits_str.startswith("[") and not edits_str.startswith("{"):
                raise ValueError(f"Batch edits file not found: {edits_str}")
        except OSError as oe:
            raise ValueError(f"Error accessing edits file '{edits_str}': {oe}") from oe

        try:
            data = json.loads(edits_str)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON string or edits file path: {e}") from e
    return []


def _is_safe_path(path: Path, base: Optional[Path] = None) -> bool:
    """IMPROVEMENT 4: Check if path is safe (no traversal outside base directory).

    When no base is provided, allows any path within the user's home directory
    or system temp directory, rather than restricting to cwd only.
    """
    try:
        resolved = path.resolve()
        if base is not None:
            # If a specific base is provided, enforce it strictly
            base_resolved = base.resolve()
            resolved.relative_to(base_resolved)
            return True
        # No base provided: allow paths under home or temp directories
        home_dir = Path.home().resolve()
        try:
            resolved.relative_to(home_dir)
            return True
        except ValueError:
            pass
        # Also allow temp directories
        import tempfile
        for temp_candidate in [Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()]:
            try:
                resolved.relative_to(temp_candidate)
                return True
            except (ValueError, OSError):
                pass
        return False
    except (ValueError, OSError):
        return False


# ==============================================================================
# SECTION 3: Agent & Environment Helpers
# ==============================================================================

def get_agent_var(name: str, default: str = "") -> str:
    """Access agent user-defined variables (LLM_AGENT_VAR_<NAME>)."""
    env_name = f"LLM_AGENT_VAR_{name.upper()}"
    return os.environ.get(env_name, default)


def get_builtin_var(name: str) -> Optional[str]:
    """Access agent built-in environment variables (e.g., __cwd__, __os__)."""
    env_name = f"LLM_AGENT_VAR_{name}"
    return os.environ.get(env_name)


def get_execution_context() -> dict[str, Any]:
    """Extract complete execution context from environment."""
    termux_prefix = os.environ.get("PREFIX", "")
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME", "str_replace_editor"),
        "output_path": os.environ.get("LLM_OUTPUT"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "is_termux": "com.termux" in termux_prefix or Path("/data/data/com.termux").exists(),
    }


def _print_ui_header(data: dict[str, Any], no_color: bool = False) -> None:
    """Print the header section of the UI."""
    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "SUCCESS" if success else "FAILED"

    box_w = 72
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [STR REPLACE EDITOR v{__version__}]{RESET} {status_color}{BOLD}{status_symbol} {status_text}{RESET}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)


def _print_ui_body(data: dict[str, Any], no_color: bool = False) -> None:
    """Print the body section of the UI."""
    action = data.get("action")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}File:{RESET}        {data.get('file_path', 'N/A')}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Action:{RESET}      {NEON_YELLOW}{data.get('action', 'N/A')}{RESET}", no_color=no_color)
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Mode:{RESET}        {NEON_BLUE}{data.get('mode', 'literal')}{RESET}", no_color=no_color)

    if action == "view":
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Lines:{RESET}       {data.get('view_start_line')}–{data.get('view_end_line')} (Total: {data.get('total_lines')})", no_color=no_color)
    elif action == "write":
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Written:{RESET}     {NEON_GREEN}{data.get('written_bytes', 0)}{RESET} bytes", no_color=no_color)
        if data.get("dry_run"):
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}DRY-RUN:{RESET}     Simulation only — no changes written", no_color=no_color)
        if data.get("backup_created"):
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Backup:{RESET}      {NEON_GREEN}Created ({data.get('backup_path')}){RESET}", no_color=no_color)
    elif action in {"replace", "count"}:
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Search:{RESET}      '{data.get('search', '')}'", no_color=no_color)
        if action == "replace":
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Replacement:{RESET} '{data.get('replacement', '')}'", no_color=no_color)
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Changed:{RESET}     {NEON_GREEN}{data.get('replacements_made', 0)}{RESET} occurrence(s)", no_color=no_color)
            if data.get("dry_run"):
                _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}DRY-RUN:{RESET}     Simulation only — no changes written", no_color=no_color)
            if data.get("backup_created"):
                _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Backup:{RESET}      {NEON_GREEN}Created ({data.get('backup_path')}){RESET}", no_color=no_color)
    elif action == "batch":
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Edits:{RESET}       {data.get('total_edits', 0)} rule(s)", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Changed:{RESET}     {NEON_GREEN}{data.get('total_replacements_made', 0)}{RESET} total occurrence(s)", no_color=no_color)
        if data.get("dry_run"):
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}DRY-RUN:{RESET}     Simulation only — no changes written", no_color=no_color)
        if data.get("backup_created"):
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Backup:{RESET}      {NEON_GREEN}Created ({data.get('backup_path')}){RESET}", no_color=no_color)

        batch_results = data.get("batch_results", [])
        if batch_results:
            box_w = 72
            border = "─" * box_w
            _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
            _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Batch Results:{RESET}", no_color=no_color)
            for item in batch_results[:10]:
                idx = item.get("index")
                status = "✓" if item.get("success") else "✗"
                scolor = NEON_GREEN if item.get("success") else NEON_RED
                search_preview = repr(item.get("search", ""))[:30]
                made = item.get("replacements_made", 0)
                _cprint(f"{NEON_PURPLE}│{RESET}   {scolor}{status}{RESET} [{idx}] {search_preview} → {made} match(es)", no_color=no_color)
            if len(batch_results) > 10:
                _cprint(f"{NEON_PURPLE}│{RESET}   {DIM}... and {len(batch_results) - 10} more rules{RESET}", no_color=no_color)

    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Duration:{RESET}    {DIM}{data.get('duration_ms', 0)}ms{RESET}", no_color=no_color)

    if "file_size" in data:
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Size:{RESET}        {data.get('file_size')} bytes", no_color=no_color)

    if not data.get("success") and "error" in data:
        box_w = 72
        border = "─" * box_w
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error:{RESET}       {data['error']}", no_color=no_color)

    diff_text = data.get("diff", "")
    if diff_text:
        box_w = 72
        border = "─" * box_w
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Diff Preview:{RESET}", no_color=no_color)
        for diff_line in diff_text.splitlines()[:20]:
            if diff_line.startswith("+") and not diff_line.startswith("+++"):
                color_line = f"{NEON_GREEN}{diff_line}{RESET}"
            elif diff_line.startswith("-") and not diff_line.startswith("---"):
                color_line = f"{NEON_RED}{diff_line}{RESET}"
            elif diff_line.startswith("@"):
                color_line = f"{NEON_CYAN}{diff_line}{RESET}"
            else:
                color_line = diff_line
            _cprint(f"{NEON_PURPLE}│{RESET}   {color_line}", no_color=no_color)

    if action == "view" and "content" in data:
        box_w = 72
        border = "─" * box_w
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Content Preview:{RESET}", no_color=no_color)
        lines = data["content"].splitlines()
        start_ln = data.get("view_start_line", 1)
        preview_limit = 35
        for idx, line in enumerate(lines[:preview_limit]):
            display_line = line[:120] + ("..." if len(line) > 120 else "")
            _cprint(f"{NEON_PURPLE}│{RESET}   {DIM}{start_ln + idx:4d} │{RESET} {display_line}", no_color=no_color)
        if len(lines) > preview_limit:
            _cprint(f"{NEON_PURPLE}│{RESET}   {DIM}... and {len(lines) - preview_limit} more lines{RESET}", no_color=no_color)


def _print_ui_footer(data: dict[str, Any], no_color: bool = False) -> None:
    """Print the footer section of the UI."""
    box_w = 72
    border = "─" * box_w
    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}", no_color=no_color)


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    """Render an enhanced colorized box UI for terminal users on stderr."""
    if not _is_tty() or no_color:
        return

    _print_ui_header(data, no_color)
    _print_ui_body(data, no_color)
    _print_ui_footer(data, no_color)


# ==============================================================================
# SECTION 4: Core Logic Implementation
# ==============================================================================

class GracefulShutdown:
    """IMPROVEMENT 5: Thread-safe signal handler for graceful cancellation."""
    
    def __init__(self) -> None:
        self.interrupted = False
        self._lock = threading.Lock()
        self._original_handlers: Dict[int, Any] = {}
        self._signal_supported = True
        
        try:
            # Only set handlers in main thread
            if threading.current_thread() is threading.main_thread():
                self._original_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, self._handle_signal)
                self._original_handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, self._handle_signal)
            else:
                self._signal_supported = False
        except (ValueError, OSError, AttributeError):
            self._signal_supported = False
            self._original_handlers = {}

    def _handle_signal(self, signum: int, frame: Any) -> None:
        with self._lock:
            self.interrupted = True

    def check_interrupted(self) -> bool:
        """Thread-safe check for interruption."""
        with self._lock:
            return self.interrupted

    def restore(self) -> None:
        if self._signal_supported and threading.current_thread() is threading.main_thread():
            try:
                for sig, handler in self._original_handlers.items():
                    signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# IMPROVEMENT 6: Regex compilation cache for batch operations
class RegexCache:
    """Thread-safe regex pattern cache with LRU eviction."""
    
    def __init__(self, max_size: int = 128):
        self._cache: Dict[Tuple[str, int], Pattern[str]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()
    
    def get(self, pattern: str, flags: int) -> Pattern[str]:
        key = (pattern, flags)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            
            # Simple LRU: remove oldest if at capacity
            if len(self._cache) >= self._max_size:
                # Remove first item (approximate LRU)
                first_key = next(iter(self._cache))
                del self._cache[first_key]
            
            compiled = re.compile(pattern, flags)
            self._cache[key] = compiled
            return compiled
    
    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# Global regex cache instance
_regex_cache = RegexCache()


@contextmanager
def _safe_temp_file(target_path: Path, encoding: str) -> Any:
    """IMPROVEMENT 7: Context manager for safe atomic writes with guaranteed cleanup."""
    # Use tempfile.mkstemp for secure temp file creation
    fd, temp_path_str = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=f".tmp.{os.getpid()}",
        dir=target_path.parent
    )
    temp_path = Path(temp_path_str)
    os.close(fd)  # We'll open it again with proper encoding
    
    try:
        yield temp_path
        # Atomic replace on success
        if target_path.exists():
            try:
                shutil.copymode(target_path, temp_path)
            except OSError:
                pass
        temp_path.replace(target_path)
    except Exception:
        # Cleanup on failure
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _safe_write_file(target_path: Path, new_content: str, encoding: str) -> None:
    """
    Write text to target file atomically preserving original file permissions.
    IMPROVEMENT 7: Uses secure tempfile and context manager.
    """
    with _safe_temp_file(target_path, encoding) as temp_path:
        temp_path.write_text(new_content, encoding=encoding, errors="strict")


def _replace_in_content(
    content: str,
    search: str,
    replacement: str,
    regex: bool = False,
    ignore_case: bool = False,
    dotall: bool = False,
) -> tuple[str, int]:
    """
    Perform search and replace on content cleanly in a single pass.
    Returns (new_content, count_of_replacements).
    IMPROVEMENT 6: Uses regex cache for performance.
    """
    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE
    if dotall:
        flags |= re.DOTALL

    if regex:
        try:
            # IMPROVEMENT 6: Use cached regex compilation
            pattern = _regex_cache.get(search, flags)
            new_content, count = pattern.subn(replacement, content)
        except re.error as e:
            raise ValueError(f"Regex error: {e}") from e
    else:
        if ignore_case:
            # IMPROVEMENT 8: Cache literal patterns too for case-insensitive
            escaped_search = re.escape(search)
            pattern = _regex_cache.get(escaped_search, re.IGNORECASE | re.MULTILINE)
            new_content, count = pattern.subn(lambda _m: replacement, content)
        else:
            count = content.count(search)
            new_content = content.replace(search, replacement)
    return new_content, count


def _detect_encoding(file_path: Path, declared_encoding: Optional[str] = None) -> Tuple[str, str]:
    """
    IMPROVEMENT 9: Robust encoding detection with fallback chain.
    Returns (content, encoding_used).
    """
    encodings_to_try = []
    
    if declared_encoding:
        encodings_to_try.append(declared_encoding)
    
    # Standard fallback chain
    encodings_to_try.extend(["utf-8", "utf-8-sig", "latin-1", "cp1252", "iso-8859-1"])
    
    last_error = None
    for enc in encodings_to_try:
        try:
            content = file_path.read_text(encoding=enc, errors="strict")
            return content, enc
        except UnicodeDecodeError as e:
            last_error = e
            continue
        except OSError as e:
            last_error = e
            continue
    
    # Final fallback with replacement
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return content, "utf-8"
    except OSError as e:
        raise OSError(f"Cannot read file {file_path}: {e}") from e


def execute_tool(
    action: str,
    file_path: str,
    search: Optional[str] = None,
    replacement: Optional[str] = None,
    content: Optional[str] = None,
    edits: Optional[Union[str, List[Dict[str, Any]]]] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    backup: bool = False,
    no_color: bool = False,
    verbose: bool = False,
    regex: bool = False,
    dry_run: bool = False,
    ignore_case: bool = False,
    decode_escapes: bool = False,
    dotall: bool = False,
    encoding: Optional[str] = None,
) -> dict[str, Any]:
    """
    Core execution logic supporting view, replace, count, write, and batch actions.
    Supports multiline replacements, escape decoding, dotall regex, and batch edits.
    """
    start_time = time.monotonic()
    target_path = Path(file_path).expanduser().resolve()

    # IMPROVEMENT 10: Structured logging setup
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG, 
            format="[%(levelname)s] %(message)s",
            force=True  # Override any existing config
        )
        logging.debug(f"Action: {action} on Path: {target_path}")

    action_lower = action.lower().strip()
    allowed_actions = {"view", "replace", "count", "write", "batch"}
    if action_lower not in allowed_actions:
        return {
            "success": False,
            "error": f"Unknown action '{action}'. Allowed: {', '.join(allowed_actions)}.",
            "exit_code": EXIT_INVALID_INPUT,
            "duration_ms": 0.0,
        }

    shutdown = GracefulShutdown()

    try:
        # IMPROVEMENT 11: Path validation and security checks
        if action_lower == "write":
            # Ensure parent directory exists
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {
                    "success": False,
                    "error": f"Cannot create directory {target_path.parent}: {e}",
                    "exit_code": EXIT_PERMISSION_DENIED,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }
        elif not target_path.exists():
            return {
                "success": False,
                "error": f"File does not exist: {file_path}",
                "exit_code": EXIT_FILE_NOT_FOUND,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }

        if not target_path.is_file() and action_lower != "write":
            return {
                "success": False,
                "error": f"Not a regular file: {file_path}",
                "exit_code": EXIT_FILE_NOT_FOUND,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }

        # IMPROVEMENT 9: Use robust encoding detection
        # For write action on new files, skip reading (file doesn't exist yet)
        if action_lower == "write" and not target_path.exists():
            file_content = ""
            encoding_used = encoding or "utf-8"
        else:
            file_content, encoding_used = _detect_encoding(target_path, encoding)

        file_size = len(file_content.encode(encoding_used, errors="replace")) if file_content else 0

        # Safety Check: Binary File Detection
        if _is_binary(file_content) and action_lower in {"replace", "batch"}:
            return {
                "success": False,
                "error": f"Target file '{file_path}' appears to be a binary file. Refusing to perform replace operations.",
                "exit_code": EXIT_INVALID_INPUT,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }

        if shutdown.check_interrupted():
            return {
                "success": False,
                "error": "Operation interrupted by user signal.",
                "exit_code": EXIT_INTERRUPTED,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }

        mode = "regex" if regex else "literal"
        context_data = get_execution_context()

        # --- WRITE ---
        if action_lower == "write":
            if content is None:
                return {
                    "success": False,
                    "error": "--content required for write action",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            if decode_escapes:
                content = _unescape_string(content)

            write_encoding = encoding_used
            backup_created = False
            backup_path_str = None
            if backup and target_path.exists() and not dry_run:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                b_path = target_path.with_name(f"{target_path.name}.{ts}.bak")
                try:
                    shutil.copy2(target_path, b_path)
                    backup_created = True
                    backup_path_str = str(b_path)
                except OSError as e:
                    logging.warning(f"Failed to create backup: {e}")

            written_bytes = len(content.encode(write_encoding))
            diff_text = _generate_diff(file_content, content, str(target_path)) if target_path.exists() else ""

            if not dry_run:
                _safe_write_file(target_path, content, write_encoding)

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "write",
                "file_path": str(target_path),
                "written_bytes": written_bytes,
                "backup_created": backup_created,
                "backup_path": backup_path_str,
                "dry_run": dry_run,
                "diff": diff_text,
                "mode": mode,
                "file_size": written_bytes if not dry_run else file_size,
                "context": context_data,
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- VIEW ---
        if action_lower == "view":
            lines = file_content.splitlines(keepends=False)
            total_lines = len(lines)
            s_idx = max(0, (start_line or 1) - 1)
            e_idx = min(total_lines, end_line) if end_line else total_lines
            if end_line and end_line < (start_line or 1):
                e_idx = s_idx

            sliced = lines[s_idx:e_idx]
            view_content = "\n".join(sliced)

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "view",
                "file_path": str(target_path),
                "total_lines": total_lines,
                "view_start_line": s_idx + 1 if total_lines > 0 else 0,
                "view_end_line": min(e_idx, total_lines),
                "content": view_content,
                "file_size": file_size,
                "mode": mode,
                "context": context_data,
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- COUNT ---
        elif action_lower == "count":
            if search is None:
                return {
                    "success": False,
                    "error": "--search required for count action",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            if decode_escapes:
                search = _unescape_string(search)

            count_flags = 0
            if ignore_case:
                count_flags |= re.IGNORECASE
            if dotall:
                count_flags |= re.DOTALL
            count_flags |= re.MULTILINE

            if regex:
                try:
                    pattern = _regex_cache.get(search, count_flags)
                    match_count = len(pattern.findall(file_content))
                except re.error as e:
                    return {
                        "success": False,
                        "error": f"Regex error: {e}",
                        "exit_code": EXIT_INVALID_INPUT,
                        "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                    }
            else:
                if ignore_case:
                    escaped_search = re.escape(search)
                    pattern = _regex_cache.get(escaped_search, count_flags)
                    match_count = len(pattern.findall(file_content))
                else:
                    match_count = file_content.count(search)

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "count",
                "file_path": str(target_path),
                "search": search,
                "match_count": match_count,
                "mode": mode,
                "file_size": file_size,
                "context": context_data,
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- BATCH ---
        elif action_lower == "batch" or (action_lower == "replace" and edits is not None):
            if edits is None:
                return {
                    "success": False,
                    "error": "--edits (JSON string or file path) required for batch action",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            try:
                edit_list = _parse_edits(edits)
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to parse batch edits: {e}",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            if not edit_list:
                return {
                    "success": False,
                    "error": "No valid edits found in batch payload",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            current_content = file_content
            batch_results = []
            total_replacements = 0

            for idx, item in enumerate(edit_list, 1):
                if shutdown.check_interrupted():
                    batch_results.append({
                        "index": idx,
                        "success": False,
                        "error": "Operation interrupted",
                        "replacements_made": 0,
                    })
                    break

                item_search = None
                for k in ("search", "old", "find", "target", "pattern"):
                    if k in item and item[k] is not None:
                        item_search = item[k]
                        break

                item_replacement = None
                for k in ("replacement", "new", "replace", "with"):
                    if k in item and item[k] is not None:
                        item_replacement = item[k]
                        break

                if item_search is None or item_replacement is None:
                    batch_results.append({
                        "index": idx,
                        "success": False,
                        "error": "Missing search or replacement field",
                        "replacements_made": 0,
                    })
                    continue

                item_regex = item.get("regex", regex)
                item_ignore_case = item.get("ignore_case", ignore_case)
                item_decode = item.get("decode_escapes", decode_escapes)
                item_dotall = item.get("dotall", dotall)

                if item_decode:
                    item_search = _unescape_string(item_search)
                    item_replacement = _unescape_string(item_replacement)

                try:
                    new_content, count_made = _replace_in_content(
                        current_content,
                        item_search,
                        item_replacement,
                        regex=item_regex,
                        ignore_case=item_ignore_case,
                        dotall=item_dotall,
                    )
                    current_content = new_content
                    total_replacements += count_made
                    batch_results.append({
                        "index": idx,
                        "success": True,
                        "search": item_search,
                        "replacement": item_replacement,
                        "replacements_made": count_made,
                    })
                except Exception as ex:
                    batch_results.append({
                        "index": idx,
                        "success": False,
                        "search": item_search,
                        "error": str(ex),
                        "replacements_made": 0,
                    })

            backup_created = False
            backup_path_str = None
            if backup and not dry_run and total_replacements > 0:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                b_path = target_path.with_name(f"{target_path.name}.{ts}.bak")
                try:
                    shutil.copy2(target_path, b_path)
                    backup_created = True
                    backup_path_str = str(b_path)
                except OSError as e:
                    logging.warning(f"Failed to create backup: {e}")

            diff_text = _generate_diff(file_content, current_content, str(target_path))

            if not dry_run and total_replacements > 0:
                _safe_write_file(target_path, current_content, encoding_used)

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "batch",
                "file_path": str(target_path),
                "total_edits": len(edit_list),
                "total_replacements_made": total_replacements,
                "batch_results": batch_results,
                "backup_created": backup_created,
                "backup_path": backup_path_str,
                "dry_run": dry_run,
                "diff": diff_text,
                "mode": mode,
                "file_size": file_size,
                "context": context_data,
                "message": f"Batch operation complete: {total_replacements} replacement(s) across {len(edit_list)} edit rule(s).",
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- REPLACE ---
        elif action_lower == "replace":
            if search is None:
                return {
                    "success": False,
                    "error": "--search required for replace action",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }
            if replacement is None:
                return {
                    "success": False,
                    "error": "--replacement required for replace action",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            if decode_escapes:
                search = _unescape_string(search)
                replacement = _unescape_string(replacement)

            try:
                new_content, replacements_made = _replace_in_content(
                    file_content,
                    search,
                    replacement,
                    regex=regex,
                    ignore_case=ignore_case,
                    dotall=dotall,
                )
            except ValueError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            diff_text = _generate_diff(file_content, new_content, str(target_path))

            if replacements_made == 0 and not dry_run:
                duration_ms = round((time.monotonic() - start_time) * 1000, 2)
                return {
                    "success": True,
                    "action": "replace",
                    "file_path": str(target_path),
                    "search": search,
                    "replacement": replacement,
                    "replacements_made": 0,
                    "message": "No matches found.",
                    "mode": mode,
                    "dry_run": dry_run,
                    "file_size": file_size,
                    "context": context_data,
                    "exit_code": EXIT_SUCCESS,
                    "duration_ms": duration_ms,
                }

            backup_created = False
            backup_path_str = None
            if backup and not dry_run and replacements_made > 0:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                b_path = target_path.with_name(f"{target_path.name}.{ts}.bak")
                try:
                    shutil.copy2(target_path, b_path)
                    backup_created = True
                    backup_path_str = str(b_path)
                except OSError as e:
                    logging.warning(f"Failed to create backup: {e}")

            if dry_run:
                duration_ms = round((time.monotonic() - start_time) * 1000, 2)
                return {
                    "success": True,
                    "action": "replace",
                    "file_path": str(target_path),
                    "search": search,
                    "replacement": replacement,
                    "replacements_made": replacements_made,
                    "dry_run": True,
                    "backup_created": False,
                    "diff": diff_text,
                    "message": f"Dry-run: would have performed {replacements_made} replacement(s).",
                    "mode": mode,
                    "file_size": file_size,
                    "context": context_data,
                    "exit_code": EXIT_SUCCESS,
                    "duration_ms": duration_ms,
                }

            _safe_write_file(target_path, new_content, encoding_used)

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "replace",
                "file_path": str(target_path),
                "search": search,
                "replacement": replacement,
                "replacements_made": replacements_made,
                "backup_created": backup_created,
                "backup_path": backup_path_str,
                "dry_run": False,
                "diff": diff_text,
                "mode": mode,
                "file_size": file_size,
                "context": context_data,
                "message": f"Successfully performed {replacements_made} replacement(s).",
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

    except PermissionError as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        return {
            "success": False,
            "error": f"Permission denied accessing file: {exc}",
            "exit_code": EXIT_PERMISSION_DENIED,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        if verbose:
            import traceback
            error_detail = f"{exc}\n{traceback.format_exc()}"
        else:
            error_detail = str(exc)
        return {
            "success": False,
            "error": f"Operation failed: {error_detail}",
            "exit_code": EXIT_ERROR,
            "duration_ms": duration_ms,
        }
    finally:
        shutdown.restore()


# ==============================================================================
# SECTION 5: Output Routing
# ==============================================================================

def write_llm_output(data: dict[str, Any]) -> None:
    """Write clean JSON for LLM consumption via LLM_OUTPUT safely."""
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = json.dumps(data, indent=2, ensure_ascii=False, cls=ToolJSONEncoder) + "\n"

    direct_targets = {"/dev/stdout", "/dev/fd/1", "-"}
    if out_path in direct_targets:
        try:
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(json_payload.encode("utf-8"))
                sys.stdout.buffer.flush()
            else:
                sys.stdout.write(json_payload)
                sys.stdout.flush()
        except UnicodeEncodeError:
            sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=True, cls=ToolJSONEncoder) + "\n")
            sys.stdout.flush()
    else:
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            # IMPROVEMENT 12: Atomic write for LLM output too
            with _safe_temp_file(Path(out_path), "utf-8") as temp_path:
                temp_path.write_text(json_payload, encoding="utf-8")
        except OSError as err:
            sys.stderr.write(f"Failed writing LLM_OUTPUT: {err}\n")
            sys.stdout.write(json_payload)
            sys.stdout.flush()


# ==============================================================================
# SECTION 6: AIChat Entrypoint
# ==============================================================================

def run(
    action: str,
    file_path: str,
    search: Optional[str] = None,
    replacement: Optional[str] = None,
    content: Optional[str] = None,
    edits: Optional[Union[str, List[Dict[str, Any]]]] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    backup: bool = False,
    no_color: bool = False,
    verbose: bool = False,
    regex: bool = False,
    dry_run: bool = False,
    ignore_case: bool = False,
    decode_escapes: bool = False,
    dotall: bool = False,
    encoding: Optional[str] = None,
    pattern: Optional[str] = None,
    use_regex: Optional[Union[bool, str]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    AIChat Programmatic Entrypoint with alias support.

    Args:
        action: Operation to perform (view, replace, count, write, batch)
        file_path: Path to target file
        search: Search string or pattern
        replacement: Replacement text
        content: New content for write action
        edits: JSON array or file path for batch replacements
        start_line: Starting line (1-based) for view
        end_line: Ending line for view
        backup: Create timestamped .bak backup file before modifying
        no_color: Disable ANSI terminal colors
        verbose: Enable debug logging
        regex: Treat search as regex pattern
        dry_run: Simulate replacements without writing to file
        ignore_case: Case-insensitive search/replace
        decode_escapes: Decode escape sequences (\n, \t, \r)
        dotall: Enable dotall in regex (. matches newline)
        encoding: Custom file encoding (default: utf-8)
    """
    effective_search = search if search is not None else pattern
    effective_regex = regex
    if use_regex is not None:
        if isinstance(use_regex, str):
            effective_regex = use_regex.lower() in ("true", "1", "yes")
        else:
            effective_regex = bool(use_regex)

    result = execute_tool(
        action=action,
        file_path=file_path,
        search=effective_search,
        replacement=replacement,
        content=content,
        edits=edits,
        start_line=start_line,
        end_line=end_line,
        backup=backup,
        no_color=no_color,
        verbose=verbose,
        regex=effective_regex,
        dry_run=dry_run,
        ignore_case=ignore_case,
        decode_escapes=decode_escapes,
        dotall=dotall,
        encoding=encoding,
    )
    print_human_readable_ui(result, no_color=no_color)
    write_llm_output(result)
    return result


# ==============================================================================
# SECTION 7: CLI Parser
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="str_replace_editor.py",
        description=f"Pyrmethus Enhanced String Replace Editor v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--action", "-a", required=True, choices=["view", "replace", "count", "write", "batch"],
                        help="Operation to perform")
    parser.add_argument("--file-path", "-f", required=True, dest="file_path", metavar="PATH",
                        help="Target file path")
    parser.add_argument("--search", "-s", metavar="TEXT", help="Search string/pattern")
    parser.add_argument("--replacement", "-r", metavar="TEXT", help="Replacement text")
    parser.add_argument("--content", "-c", metavar="TEXT", help="New content for write action")
    parser.add_argument("--edits", "-e", metavar="JSON_OR_PATH", help="JSON array or file path for batch edits")
    parser.add_argument("--start-line", type=int, dest="start_line", metavar="NUM",
                        help="View start line (1-based)")
    parser.add_argument("--end-line", type=int, dest="end_line", metavar="NUM",
                        help="View end line")
    parser.add_argument("--backup", action="store_true", default=False,
                        help="Create timestamped backup")
    parser.add_argument("--no-color", action="store_true", default=False, dest="no_color",
                        help="Disable colors")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Verbose output")
    parser.add_argument("--regex", action="store_true", default=False,
                        help="Treat search as regex pattern")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Simulate replace without writing")
    parser.add_argument("--ignore-case", "-i", action="store_true", default=False,
                        help="Case-insensitive operation")
    parser.add_argument("--decode-escapes", action="store_true", default=False, dest="decode_escapes",
                        help="Decode escape sequences (\\n, \\t, \\r)")
    parser.add_argument("--dotall", action="store_true", default=False,
                        help="Dotall mode in regex (. matches newline)")
    parser.add_argument("--encoding", metavar="ENC", help="Specify file encoding (default: utf-8)")
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    res = run(
        action=args.action,
        file_path=args.file_path,
        search=args.search,
        replacement=args.replacement,
        content=args.content,
        edits=args.edits,
        start_line=args.start_line,
        end_line=args.end_line,
        backup=args.backup,
        no_color=args.no_color,
        verbose=args.verbose,
        regex=args.regex,
        dry_run=args.dry_run,
        ignore_case=args.ignore_case,
        decode_escapes=args.decode_escapes,
        dotall=args.dotall,
        encoding=args.encoding,
    )
    sys.exit(res.get("exit_code", EXIT_SUCCESS))
