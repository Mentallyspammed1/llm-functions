#!/usr/bin/env python3
# ==============================================================================
# str_replace_editor.py — Pyrmethus AIChat Tool v1.5.2-ASCENDED
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
# @flag   --backup                       Create timestamped .bak before modifying
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug logging
# @flag   --regex                        Treat search as regex pattern
# @flag   --dry-run                      Simulate replace (no file write)
# @flag   --ignore-case                  Case-insensitive search/replace
# @flag   --decode-escapes               Decode \n, \t, \r in search/replacement strings
# @flag   --dotall                       Treat . in regex as matching newlines (re.DOTALL)
# @flag   --encoding                      Specify file encoding (default: utf-8)
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import enum
import json
import os
import pathlib
import re
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

__version__ = "1.5.2-ASCENDED"
__all__ = ["run", "execute_tool", "__version__"]

# ==============================================================================
# SECTION 1: Exit Codes & Exception Models
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_INVALID_INPUT = 3
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
# SECTION 2: Color Palette & Formatting Helpers (Enhanced Mystical Glow)
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
ITALIC       = "\033[3m"

# Comprehensive ANSI escape sequence stripping regex
_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])|\033\[[0-9;]*[a-zA-Z]"
)

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
    """Convert literal escape sequences (e.g. \\n, \\t, \\r) into actual characters."""
    if not text:
        return text
    replacements = {
        "\\n": "\n",
        "\\r": "\r",
        "\\t": "\t",
        "\\\\": "\\",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

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
            if path_obj.is_file():
                content = path_obj.read_text(encoding="utf-8")
                data = json.loads(content)
                return data if isinstance(data, list) else [data]
        except (OSError, json.JSONDecodeError):
            pass
        try:
            data = json.loads(edits_str)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
    return []

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
    elif action in {"replace", "count"}:
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Search:{RESET}      '{data.get('search', '')}'", no_color=no_color)
        if action == "replace":
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Replacement:{RESET} '{data.get('replacement', '')}'", no_color=no_color)
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Changed:{RESET}     {NEON_GREEN}{data.get('replacements_made', 0)}{RESET} occurrence(s)", no_color=no_color)
            if data.get("dry_run"):
                _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}DRY-RUN:{RESET}     Simulation only — no changes written", no_color=no_color)
            if data.get("backup_created"):
                _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Backup:{RESET}      {NEON_GREEN}Created (.bak){RESET}", no_color=no_color)
    elif action == "batch":
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Edits:{RESET}       {data.get('total_edits', 0)} rule(s)", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Changed:{RESET}     {NEON_GREEN}{data.get('total_replacements_made', 0)}{RESET} total occurrence(s)", no_color=no_color)
        if data.get("dry_run"):
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}DRY-RUN:{RESET}     Simulation only — no changes written", no_color=no_color)
        if data.get("backup_created"):
            _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Backup:{RESET}      {NEON_GREEN}Created (.bak){RESET}", no_color=no_color)

        batch_results = data.get("batch_results", [])
        if batch_results:
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

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error:{RESET}       {data['error']}", no_color=no_color)

    if action == "view" and "content" in data:
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
    """
    Render an enhanced, mystical colorized box UI for terminal wizards on stderr.
    """
    if not _is_tty() or no_color:
        return

    _print_ui_header(data, no_color)
    _print_ui_body(data, no_color)
    _print_ui_footer(data, no_color)

# ==============================================================================
# SECTION 2: Core Logic Implementation (Fortified & Enchanted)
# ==============================================================================

class GracefulShutdown:
    """Signal handler for graceful cancellation of batch/file operations."""

    def __init__(self) -> None:
        self.interrupted = False
        try:
            self._old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
            self._old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
        except ValueError:
            self._old_sigint = signal.SIG_DFL
            self._old_sigterm = signal.SIG_DFL

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def restore(self) -> None:
        try:
            signal.signal(signal.SIGINT, self._old_sigint)
            signal.signal(signal.SIGTERM, self._old_sigterm)
        except ValueError:
            pass


def _replace_in_content(
    content: str,
    search: str,
    replacement: str,
    regex: bool = False,
    ignore_case: bool = False,
    dotall: bool = False
) -> tuple[str, int]:
    """
    Perform search and replace on content.
    Returns (new_content, count_of_replacements).
    """
    flags = re.IGNORECASE if ignore_case else 0
    if dotall:
        flags |= re.DOTALL

    if regex:
        try:
            pattern = re.compile(search, flags | re.MULTILINE)
            new_content = pattern.sub(replacement, content)
            count = len(pattern.findall(content))
        except re.error as e:
            raise ValueError(f"Regex error: {e}") from e
    else:
        if ignore_case:
            escaped_search = re.escape(search)
            count = len(re.findall(escaped_search, content, flags=re.IGNORECASE))
            new_content = re.sub(escaped_search, lambda _m, r=replacement: r, content, flags=re.IGNORECASE)
        else:
            count = content.count(search)
            new_content = content.replace(search, replacement)
    return new_content, count


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
        # For 'write' action, create file and parent directories if needed
        if action_lower == "write":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_path.exists():
                target_path.touch()
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

        # Determine encoding to use
        if encoding is None:
            encoding_used = "utf-8"
        else:
            encoding_used = encoding

        # Read content with graceful fallback using replacement handlers
        file_content = ""
        if target_path.exists() and target_path.is_file():
            try:
                file_content = target_path.read_text(encoding=encoding_used, errors="strict")
            except UnicodeDecodeError:
                if encoding is None:
                    # Fallback to latin-1 only if no encoding was specified
                    try:
                        file_content = target_path.read_text(encoding="latin-1", errors="replace")
                        encoding_used = "latin-1"
                    except Exception:
                        file_content = target_path.read_text(encoding="utf-8", errors="replace")
                        encoding_used = "utf-8"
                else:
                    # If encoding was specified and failed, we error
                    raise
            except Exception as e:
                file_content = target_path.read_text(encoding="utf-8", errors="replace")
                encoding_used = "utf-8"

        file_size = len(file_content.encode(encoding_used, errors="replace"))

        if shutdown.interrupted:
            return {
                "success": False,
                "error": "Operation interrupted by user signal.",
                "exit_code": EXIT_INTERRUPTED,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }

        mode = "regex" if regex else "literal"
        flags = re.IGNORECASE if ignore_case else 0

        # --- WRITE ---
        if action_lower == "write":
            if content is None:
                return {
                    "success": False,
                    "error": "--content required for write",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }
            write_encoding = encoding if encoding is not None else "utf-8"
            if backup and target_path.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = target_path.with_name(f"{target_path.name}.{ts}.bak")
                shutil.copy2(target_path, backup_path)

            if not dry_run:
                target_path.write_text(content, encoding=write_encoding, errors="strict")

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "write",
                "file_path": str(target_path),
                "written_bytes": len(content.encode(write_encoding)),
                "dry_run": dry_run,
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- VIEW ---
        if action_lower == "view":
            lines = file_content.splitlines(keepends=False)
            total_lines = len(lines)
            s_idx = max(0, (start_line or 1) - 1)
            e_idx = min(len(lines), end_line) if end_line else len(lines)
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
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- COUNT ---
        elif action_lower == "count":
            if search is None:
                return {
                    "success": False,
                    "error": "--search required for count",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

            if decode_escapes:
                search = _unescape_string(search)

            count_flags = flags
            if dotall:
                count_flags |= re.DOTALL

            if regex:
                try:
                    pattern = re.compile(search, count_flags | re.MULTILINE)
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
                    match_count = len(re.findall(re.escape(search), file_content, flags=re.IGNORECASE))
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
                if shutdown.interrupted:
                    break

                item_search = None
                for k in ("search", "old", "find", "target"):
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
                        dotall=item_dotall
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

            # Backup with timestamp for safety
            backup_created = False
            if backup and not dry_run and total_replacements > 0:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = target_path.with_name(f"{target_path.name}.{timestamp}.bak")
                shutil.copy2(target_path, backup_path)
                backup_created = True

            temp_path = target_path.with_name(f".{target_path.name}.tmp_{os.getpid()}")
            try:
                if not dry_run and total_replacements > 0:
                    temp_path.write_text(current_content, encoding=encoding_used, errors="strict")
                    temp_path.replace(target_path)
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "batch",
                "file_path": str(target_path),
                "total_edits": len(edit_list),
                "total_replacements_made": total_replacements,
                "batch_results": batch_results,
                "backup_created": backup_created,
                "dry_run": dry_run,
                "mode": mode,
                "file_size": file_size,
                "message": f"Batch operation complete: {total_replacements} replacement(s) across {len(edit_list)} edit rule(s).",
                "exit_code": EXIT_SUCCESS,
                "duration_ms": duration_ms,
            }

        # --- REPLACE ---
        elif action_lower == "replace":
            if search is None:
                return {
                    "success": False,
                    "error": "--search required for replace",
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }
            if replacement is None:
                return {
                    "success": False,
                    "error": "--replacement required for replace",
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
                    dotall=dotall
                )
            except ValueError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "exit_code": EXIT_INVALID_INPUT,
                    "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                }

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
                    "exit_code": EXIT_SUCCESS,
                    "duration_ms": duration_ms,
                }

            # Backup with timestamp for safety
            backup_created = False
            if backup and not dry_run:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = target_path.with_name(f"{target_path.name}.{timestamp}.bak")
                shutil.copy2(target_path, backup_path)
                backup_created = True

            # Atomic write unless dry-run
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
                    "message": f"Dry-run: would have performed {replacements_made} replacement(s).",
                    "mode": mode,
                    "file_size": file_size,
                    "exit_code": EXIT_SUCCESS,
                    "duration_ms": duration_ms,
                }

            temp_path = target_path.with_name(f".{target_path.name}.tmp_{os.getpid()}")
            try:
                temp_path.write_text(new_content, encoding=encoding_used, errors="strict")
                temp_path.replace(target_path)
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return {
                "success": True,
                "action": "replace",
                "file_path": str(target_path),
                "search": search,
                "replacement": replacement,
                "replacements_made": replacements_made,
                "backup_created": backup_created,
                "dry_run": False,
                "mode": mode,
                "file_size": file_size,
                "message": f"Successfully performed {replacements_made} replacement(s).",
                "exit_code": EXIT_SUCCESS,
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
# SECTION 3: Output Routing
# ==============================================================================

def write_llm_output(data: dict[str, Any]) -> None:
    """Write clean JSON for LLM consumption via LLM_OUTPUT."""
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = json.dumps(data, indent=2, ensure_ascii=False, cls=ToolJSONEncoder) + "\n"

    direct_targets = {"/dev/stdout", "/dev/fd/1", "-"}
    if out_path in direct_targets:
        sys.stdout.write(json_payload)
        sys.stdout.flush()
    else:
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "a", encoding="utf-8") as fp:
                fp.write(json_payload)
        except OSError as err:
            sys.stderr.write(f"Failed writing LLM_OUTPUT: {err}\n")
            sys.stdout.write(json_payload)
            sys.stdout.flush()

# ==============================================================================
# SECTION 4: AIChat Entrypoint
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
# SECTION 5: CLI Parser (Upgraded with new arcane flags)
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
