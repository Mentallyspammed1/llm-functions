#!/usr/bin/env python3
# ==============================================================================
# update_topic.py — AIChat Session Topic & Strategic Intent Tracker v2.3.0
# argc/aichat compatible · Human-Readable Colorized Outputs · Native Caching
#
# @describe Update session topic, current strategic intent, and milestone progress for agent tracking.
#
# @meta require-tools aichat
#
# @option --title! <TEXT>                Topic or milestone title (required)
# @option --summary! <TEXT>              Detailed summary of progress and context (required)
# @option --strategic-intent <TEXT>      Strategic direction or next step objective
# @option --session-file <PATH>          Path to session log/state file (optional)
# @option --mode <MODE>                  Execution mode: summary/detailed (default: summary)
# @option --tag <TAG>                    Category or phase tag (repeatable)
# @flag   --append                       Append to session history instead of overwriting
# @flag   --use-cache                    Enable result caching for session metadata
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug log output
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pickle
import platform
import re
import signal
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

__version__ = "2.3.0"
__all__ = [
    "run",
    "execute_tool",
    "ToolCache",
    "ToolError",
    "get_agent_var",
    "get_builtin_var",
    "get_execution_context",
    "__version__",
]

# ==============================================================================
# SECTION 1: Exit Codes & Exception Models
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_TIMEOUT = 124
EXIT_PERMISSION_DENIED = 126
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPTED = 130


class ExecutionMode(str, Enum):
    SUMMARY = "summary"
    DETAILED = "detailed"


class ToolError(Exception):
    """Structured exception model for tool operations."""

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.message,
            "exit_code": self.exit_code,
            **self.details,
        }


class ToolJSONEncoder(json.JSONEncoder):
    """Zero-crash custom JSON encoder supporting standard and dynamic Python objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        if isinstance(obj, ToolError):
            return obj.to_dict()
        if isinstance(obj, Exception):
            return str(obj)
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


# ==============================================================================
# SECTION 2: Terminal Color Palette & UI Helpers
# ==============================================================================

NEON_CYAN    = "\033[38;5;51m"
NEON_GREEN   = "\033[38;5;46m"
NEON_RED     = "\033[38;5;196m"
NEON_YELLOW  = "\033[38;5;226m"
NEON_PURPLE  = "\033[38;5;129m"
NEON_PINK    = "\033[38;5;198m"
RESET        = "\033[0m"
BOLD         = "\033[1m"
DIM          = "\033[2m"

_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\033\[[0-9;?]*[a-zA-Z]"
)


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    """Check interactive TTY state while honoring standard NO_COLOR/FORCE_COLOR overrides."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(text: str, file: Any = None, no_color: bool = False, end: str = "\n") -> None:
    """Print pre-formatted ANSI text, stripping colors if stream is not a TTY or --no-color is set."""
    target = file or sys.stderr
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    """Render a human-friendly, colorized box UI for terminal users to stderr."""
    if not _is_tty() or no_color:
        return

    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "UPDATED" if success else "FAILED"

    box_w = 68
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [TOPIC UPDATE v{__version__}]{RESET} {status_color}{BOLD}{status_symbol} {status_text}{RESET}")
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Title:{RESET}    {BOLD}{data.get('title', 'N/A')}{RESET}")

    if data.get("strategic_intent"):
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_YELLOW}Intent:{RESET}   {data.get('strategic_intent')}")

    tags = data.get("tags", [])
    if tags:
        tag_str = " ".join(f"{NEON_PINK}[{t}]{RESET}" for t in tags)
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Tags:{RESET}     {tag_str}")

    if data.get("persisted_to"):
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Journal:{RESET}  {DIM}{data.get('persisted_to')}{RESET}")

    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Duration:{RESET} {DIM}{data.get('duration_ms', 0)}ms{RESET}")

    summary = data.get("summary", "")
    if summary:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Summary:{RESET}")
        for line in summary.splitlines():
            _cprint(f"{NEON_PURPLE}│{RESET}   {line}")

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error:{RESET}    {data['error']}")

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")


# ==============================================================================
# SECTION 3: Agent & Environment Helpers
# ==============================================================================

def get_agent_var(name: str, default: str = "") -> str:
    """Access agent user-defined variables (LLM_AGENT_VAR_<NAME>)."""
    env_name = f"LLM_AGENT_VAR_{name.upper()}"
    return os.environ.get(env_name, default)


def get_builtin_var(name: str) -> Optional[str]:
    """Access agent built-in environment variables."""
    env_name = f"LLM_AGENT_VAR_{name}"
    return os.environ.get(env_name)


def get_execution_context() -> dict[str, Any]:
    """Extract complete execution context from the tool and runtime environment."""
    termux_prefix = os.environ.get("PREFIX", "")
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME", "update_topic"),
        "cache_dir": os.environ.get("LLM_TOOL_CACHE_DIR"),
        "root_dir": os.environ.get("LLM_ROOT_DIR"),
        "output_path": os.environ.get("LLM_OUTPUT"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "python_version": sys.version.split()[0],
        "platform": platform.system().lower(),
        "termux_prefix": termux_prefix,
        "is_termux": "com.termux" in termux_prefix or Path("/data/data/com.termux").exists(),
    }


# ==============================================================================
# SECTION 4: Native Caching & Signal Handlers
# ==============================================================================

class ToolCache:
    """Resilient caching utility with TTL support."""

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        if cache_dir:
            self.cache_dir = cache_dir
        elif "LLM_TOOL_CACHE_DIR" in os.environ:
            self.cache_dir = Path(os.environ["LLM_TOOL_CACHE_DIR"])
        else:
            self.cache_dir = Path.home() / ".cache" / "aichat_tools"

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _make_key(self, key_data: str) -> str:
        return hashlib.sha256(key_data.encode("utf-8")).hexdigest()

    def get(self, key_data: str, ttl_seconds: int = 3600) -> Optional[Any]:
        cache_file = self.cache_dir / f"{self._make_key(key_data)}.cache"
        if not cache_file.exists():
            return None
        try:
            mtime = cache_file.stat().st_mtime
            if time.time() - mtime > ttl_seconds:
                cache_file.unlink(missing_ok=True)
                return None
            with open(cache_file, "rb") as fp:
                return pickle.load(fp)
        except Exception:
            cache_file.unlink(missing_ok=True)
            return None

    def set(self, key_data: str, value: Any) -> None:
        cache_file = self.cache_dir / f"{self._make_key(key_data)}.cache"
        tmp_file = cache_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "wb") as fp:
                pickle.dump(value, fp)
            tmp_file.replace(cache_file)
        except Exception:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)


class GracefulShutdown:
    """Signal handler for safe cancellation."""

    def __init__(self) -> None:
        self.interrupted = False
        self._old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        self._old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def restore(self) -> None:
        signal.signal(signal.SIGINT, self._old_sigint)
        signal.signal(signal.SIGTERM, self._old_sigterm)

    def should_stop(self) -> bool:
        return self.interrupted


# ==============================================================================
# SECTION 5: Core Logic Implementation
# ==============================================================================

def execute_tool(
    title: str,
    summary: str,
    strategic_intent: Optional[str] = None,
    session_file: Optional[str] = None,
    tags: Optional[list[str]] = None,
    mode: str = "summary",
    append: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Execute topic and milestone update logic."""
    start_time = time.monotonic()

    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="[DEBUG] %(message)s")
        logging.debug(f"Updating topic: {title}")

    if not title or not title.strip():
        return {
            "success": False,
            "error": "Topic 'title' cannot be empty.",
            "exit_code": EXIT_INVALID_INPUT,
            "duration_ms": 0.0,
        }

    if not summary or not summary.strip():
        return {
            "success": False,
            "error": "Topic 'summary' cannot be empty.",
            "exit_code": EXIT_INVALID_INPUT,
            "duration_ms": 0.0,
        }

    title_clean = title.strip()
    summary_clean = summary.strip()
    intent_clean = strategic_intent.strip() if strategic_intent else None
    tag_list = [t.strip() for t in (tags or []) if t.strip()]

    cache = ToolCache()
    cache_key = f"topic:{title_clean}:{intent_clean}:{hashlib.sha256(summary_clean.encode()).hexdigest()}"
    if use_cache:
        cached = cache.get(cache_key)
        if cached:
            if verbose:
                logging.debug("Cache hit for topic update.")
            cached["cached"] = True
            return cached

    timestamp_iso = datetime.now(timezone.utc).isoformat()
    persisted_path: Optional[str] = None

    # Handle state file persistence if requested or defined in agent vars
    target_session_file = session_file or get_agent_var("SESSION_FILE")
    if target_session_file:
        try:
            path_obj = Path(target_session_file).expanduser().resolve()
            path_obj.parent.mkdir(parents=True, exist_ok=True)

            log_entry = {
                "timestamp": timestamp_iso,
                "title": title_clean,
                "strategic_intent": intent_clean,
                "summary": summary_clean,
                "tags": tag_list,
            }

            if path_obj.suffix.lower() == ".json":
                existing_entries = []
                if append and path_obj.exists():
                    try:
                        with open(path_obj, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            existing_entries = data if isinstance(data, list) else [data]
                    except Exception:
                        existing_entries = []
                existing_entries.append(log_entry)
                with open(path_obj, "w", encoding="utf-8") as f:
                    json.dump(existing_entries, f, indent=2, ensure_ascii=False)
            else:
                # Markdown / Plain text journal entry
                write_mode = "a" if append else "w"
                with open(path_obj, write_mode, encoding="utf-8") as f:
                    if append and path_obj.stat().st_size > 0:
                        f.write("\n---\n\n")
                    f.write(f"## [{timestamp_iso}] {title_clean}\n\n")
                    if intent_clean:
                        f.write(f"**Strategic Intent:** {intent_clean}\n\n")
                    if tag_list:
                        f.write(f"**Tags:** {', '.join(tag_list)}\n\n")
                    f.write(f"{summary_clean}\n")

            persisted_path = str(path_obj)
        except PermissionError as exc:
            return {
                "success": False,
                "error": f"Permission denied writing session file: {exc}",
                "exit_code": EXIT_PERMISSION_DENIED,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed writing session file: {exc}",
                "exit_code": EXIT_ERROR,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            }

    duration_ms = round((time.monotonic() - start_time) * 1000, 2)
    result: dict[str, Any] = {
        "success": True,
        "title": title_clean,
        "strategic_intent": intent_clean,
        "summary": summary_clean,
        "tags": tag_list,
        "timestamp": timestamp_iso,
        "persisted_to": persisted_path,
        "mode": mode,
        "context": get_execution_context(),
        "cached": False,
        "duration_ms": duration_ms,
        "exit_code": EXIT_SUCCESS,
    }

    if use_cache:
        cache.set(cache_key, result)

    return result


# ==============================================================================
# SECTION 6: Output Routing (LLM vs Human Terminal)
# ==============================================================================

def write_llm_output(data: dict[str, Any]) -> None:
    """Format and write clean JSON output to LLM_OUTPUT destination safely."""
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
            sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
            sys.stdout.write(json_payload)
            sys.stdout.flush()


# ==============================================================================
# SECTION 7: Function Entry Point for AIChat
# ==============================================================================

def run(
    title: str,
    summary: str,
    strategic_intent: Optional[str] = None,
    session_file: Optional[str] = None,
    mode: Literal["summary", "detailed"] = "summary",
    tag: Optional[list[str]] = None,
    append: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> None:
    """Execute the topic update with specified parameters.

    Args:
        title: Topic or milestone title (required)
        summary: Detailed summary of progress and context (required)
        strategic_intent: Strategic direction or next step objective
        session_file: Path to session log/state file
        mode: Execution mode: summary or detailed (default: summary)
        tag: Category or phase tag (repeatable)
        append: Append to session history instead of overwriting
        use_cache: Enable result caching
        no_color: Disable ANSI color output
        verbose: Enable detailed debug log output
    """
    result = execute_tool(
        title=title,
        summary=summary,
        strategic_intent=strategic_intent,
        session_file=session_file,
        tags=tag,
        mode=mode,
        append=append,
        use_cache=use_cache,
        no_color=no_color,
        verbose=verbose,
    )

    print_human_readable_ui(result, no_color=no_color)
    write_llm_output(result)


# ==============================================================================
# SECTION 8: CLI Argument Parser
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update_topic.py",
        description=f"AIChat Session Topic & Intent Tracker v{__version__}",
    )
    parser.add_argument(
        "--title", "-t",
        required=True,
        metavar="TEXT",
        help="Topic or milestone title (required)",
    )
    parser.add_argument(
        "--summary", "-s",
        required=True,
        metavar="TEXT",
        help="Detailed summary of progress and context (required)",
    )
    parser.add_argument(
        "--strategic-intent",
        dest="strategic_intent",
        metavar="TEXT",
        help="Strategic direction or next step objective",
    )
    parser.add_argument(
        "--session-file",
        dest="session_file",
        metavar="PATH",
        help="Path to session log/state file",
    )
    parser.add_argument(
        "--mode",
        choices=["summary", "detailed"],
        default="summary",
        help="Execution mode (default: summary)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        dest="tag",
        metavar="TAG",
        help="Category or phase tag (repeatable)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        default=False,
        help="Append to session history instead of overwriting",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=False,
        dest="use_cache",
        help="Enable result caching",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        dest="no_color",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable detailed debug logging",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    res = execute_tool(
        title=args.title,
        summary=args.summary,
        strategic_intent=args.strategic_intent,
        session_file=args.session_file,
        tags=args.tag,
        mode=args.mode,
        append=args.append,
        use_cache=args.use_cache,
        no_color=args.no_color,
        verbose=args.verbose,
    )

    print_human_readable_ui(res, no_color=args.no_color)
    write_llm_output(res)
    sys.exit(res.get("exit_code", EXIT_SUCCESS))
