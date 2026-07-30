#!/usr/bin/env python3
# ==============================================================================
# execute_command.py — Pyrmethus Command Executor v2.2.0-ASCENDED
# argc/aichat compatible · Termux · Secure shell command execution · Native Caching
#
# @describe Execute arbitrary shell command and return full output with complete runtime metadata.
#
# @meta require-tools aichat
#
# @option --command! <STRING>            Command to run (required)
# @option --timeout <DURATION>           Duration for the command (e.g. 60s, 1m, 2h)
# @option --connect-timeout <DURATION>   Connection timeout for curl commands (default: 10s)
# @option --max-time <DURATION>          Max transfer time for curl commands (default: matches --timeout)
# @option --working-dir <PATH>           Working directory for the command (default: current dir)
# @option --env <KEY=VALUE>              Extra environment variable (repeatable)
# @option --shell <SHELL>                Shell to use: bash/sh/zsh (default: bash)
# @flag   --use-cache                    Enable result caching for identical command operations
# @flag   --no-color                     Disable ANSI colour output
# @flag   --strip-ansi                   Strip ANSI codes from command output before returning
# @flag   --verbose                      Show extra debug info (PATH, shell, env vars)
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import logging
import os
import pickle
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union

__version__ = "2.2.0"
__all__ = [
    "run",
    "execute_tool",
    "run_command",
    "ToolCache",
    "ToolError",
    "get_agent_var",
    "get_builtin_var",
    "get_execution_context",
    "sanitize_path",
    "inject_curl_timeouts",
    "duration_to_seconds",
    "interpret_exit_code",
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

MAX_OUTPUT_BYTES = 20 * 1024 * 1024  # 20 MB memory safety cap


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
    """Custom JSON encoder handling Path, Enum, datetime, timedelta, bytes, sets, and objects safely."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            return obj.total_seconds()
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        if hasattr(obj, "to_dict") and callable(obj.to_dict):
            return obj.to_dict()
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


# ==============================================================================
# SECTION 2: Terminal Color Palette & UI Helpers
# ==============================================================================

NEON_PINK    = "\033[38;5;198m"
NEON_CYAN    = "\033[38;5;51m"
NEON_GREEN   = "\033[38;5;46m"
NEON_ORANGE  = "\033[38;5;202m"
NEON_PURPLE  = "\033[38;5;129m"
NEON_YELLOW  = "\033[38;5;226m"
NEON_RED     = "\033[38;5;196m"
NEON_BLUE    = "\033[38;5;33m"
NEON_MAGENTA = "\033[38;5;201m"
NEON_LIME    = "\033[38;5;82m"
RESET        = "\033[0m"
BOLD         = "\033[1m"
DIM          = "\033[2m"

GLOW_CYAN    = NEON_CYAN   + BOLD
GLOW_GREEN   = NEON_GREEN  + BOLD
GLOW_RED     = NEON_RED    + BOLD
GLOW_YELLOW  = NEON_YELLOW + BOLD
GLOW_PINK    = NEON_PINK   + BOLD

BOX_TL = "╭"; BOX_TR = "╮"; BOX_BL = "╰"; BOX_BR = "╯"
BOX_V  = "│"; BOX_H  = "─"; BOX_LT = "├"; BOX_RT = "┤"

_NO_COLOR: bool = False
_ANSI_RE = re.compile(
    r"(?:"
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences (ESC [ ...)
    r"|"
    r"\x1b[@-Z]"  # Single-char controls: ESC @ through ESC Z (NOT ESC [)
    r"|"
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r")"
)


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    """Return True if stderr is attached to an interactive, non-dumb terminal."""
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(text: str, end: str = "\n", file: Any = None) -> None:
    """Print pre-formatted ANSI text to stderr by default to keep stdout pure for LLM JSON."""
    target = file or sys.stderr
    if _NO_COLOR or not _is_tty():
        text = _strip_ansi(text)
    print(text, end=end, flush=True, file=target)


def get_width() -> int:
    """Return current terminal column count based on stderr, constrained to reasonable bounds."""
    try:
        cols = os.get_terminal_size(sys.stderr.fileno()).columns
        return max(40, min(cols, 120))
    except (OSError, AttributeError):
        return 80


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
    """Extract complete execution context from the llm-functions and Termux environment."""
    termux_prefix = os.environ.get("PREFIX", "")
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME"),
        "cache_dir": os.environ.get("LLM_TOOL_CACHE_DIR"),
        "root_dir": os.environ.get("LLM_ROOT_DIR"),
        "output_path": os.environ.get("LLM_OUTPUT"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "termux_prefix": termux_prefix,
        "is_termux": "com.termux" in termux_prefix or Path("/data/data/com.termux").exists(),
    }


def _parse_env_vars(env_vars: Optional[list[str]]) -> dict[str, str]:
    """Parse environment variables provided in KEY=VALUE format."""
    if not env_vars:
        return {}
    parsed: dict[str, str] = {}
    for item in env_vars:
        if "=" in item:
            key, val = item.split("=", 1)
            parsed[key.strip()] = val.strip()
    return parsed


def sanitize_path() -> None:
    """
    Remove llm-functions/bin entries from PATH to prevent recursive shadowing.
    """
    raw = os.environ.get("PATH", "")
    parts = []
    for p in raw.split(os.pathsep):
        if not p:
            continue
        norm = os.path.normpath(p)
        if norm.endswith(os.path.join("llm-functions", "bin")) or os.path.basename(norm) == "llm-functions-bin":
            continue
        parts.append(p)
    os.environ["PATH"] = os.pathsep.join(parts)


# ==============================================================================
# SECTION 4: Native Caching & Signal Handlers
# ==============================================================================

class ToolCache:
    """Caching utility with TTL support for expensive operations."""

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
            return None

    def set(self, key_data: str, value: Any) -> None:
        cache_file = self.cache_dir / f"{self._make_key(key_data)}.cache"
        tmp_file = cache_file.with_suffix(f".tmp.{os.getpid()}_{time.time_ns()}")
        try:
            with open(tmp_file, "wb") as fp:
                pickle.dump(value, fp)
            tmp_file.replace(cache_file)
        except Exception:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)


class GracefulShutdown:
    """Signal handler for graceful cancellation of process group operations."""

    def __init__(self) -> None:
        self.interrupted = False
        self._old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        self._old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def restore(self) -> None:
        """Restore previous signal handlers."""
        signal.signal(signal.SIGINT, self._old_sigint)
        signal.signal(signal.SIGTERM, self._old_sigterm)

    def should_stop(self) -> bool:
        return self.interrupted


# ==============================================================================
# SECTION 5: Duration Helpers & Parsing
# ==============================================================================

_DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)$")
_UNIT_MULTIPLIERS: dict[str, float] = {
    "": 1.0,
    "s": 1.0,
    "ms": 0.001,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def duration_to_seconds(raw: str) -> float:
    """Convert duration string (e.g. '30s', '100ms', '1m', '2h') to seconds float."""
    raw = (raw or "0").strip()
    m = _DURATION_RE.match(raw)
    if not m:
        try:
            return float(raw)
        except ValueError:
            return 0.0
    n = float(m.group(1))
    unit = m.group(2).lower()
    return n * _UNIT_MULTIPLIERS.get(unit, 1.0)


def seconds_to_human(sec: float) -> str:
    """Return compact human-readable duration representation."""
    if sec < 1.0:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}m"
    return f"{sec / 3600:.2f}h"


# ==============================================================================
# SECTION 6: Curl / Wget Timeout Injection & Command Icons
# ==============================================================================

def _find_binary(name: str) -> str:
    """Locate absolute binary path, dynamically checking Termux environment directories first."""
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    candidates = [
        os.path.join(prefix, "bin", name),
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
    ]
    for c in candidates:
        try:
            if Path(c).is_file() and os.access(c, os.X_OK):
                return c
        except Exception:
            pass
    return shutil.which(name) or name


def _inject_curl_timeouts_single(cmd_segment: str, connect_timeout: float, max_time: float) -> str:
    """Inject timeouts into a single command segment."""
    stripped = cmd_segment.lstrip()
    leading = cmd_segment[: len(cmd_segment) - len(stripped)]

    if re.match(r"^curl(\s|$)", stripped):
        flags: list[str] = []
        if "--connect-timeout" not in cmd_segment:
            flags += ["--connect-timeout", str(int(connect_timeout))]
        if "--max-time" not in cmd_segment:
            flags += ["--max-time", str(int(max_time))]
        if "--retry" not in cmd_segment:
            flags += ["--retry", "3", "--retry-delay", "2"]
        silent_absent = (
            "--silent" not in cmd_segment
            and " -s " not in cmd_segment
            and " -sS " not in cmd_segment
            and not re.search(r"\s-[a-zA-Z]*s", cmd_segment)
        )
        if silent_absent:
            flags.append("--silent")

        curl_bin = _find_binary("curl")
        rest = stripped[len("curl"):].lstrip()
        return f"{leading}{curl_bin} {' '.join(flags)} {rest}".strip()

    if re.match(r"^wget(\s|$)", stripped) and "--timeout" not in cmd_segment:
        wget_bin = _find_binary("wget")
        rest = stripped[len("wget"):].lstrip()
        extra = f"--timeout={int(max_time)}"
        if "--no-verbose" not in cmd_segment and "-nv" not in cmd_segment:
            extra += " --no-verbose"
        return f"{leading}{wget_bin} {extra} {rest}".strip()

    return cmd_segment


def inject_curl_timeouts(cmd: str, connect_timeout: float, max_time: float) -> str:
    """Prepend missing network timeouts and retry settings into curl or wget across shell pipelines."""
    # Split on shell separators safely while respecting pipeline segments
    segments = re.split(r"(&&|\|\||;|\|)", cmd)
    modified_segments = []
    for seg in segments:
        if seg.strip() in ("&&", "||", ";", "|"):
            modified_segments.append(seg)
        else:
            modified_segments.append(_inject_curl_timeouts_single(seg, connect_timeout, max_time))
    return "".join(modified_segments)


_ICON_PATTERNS: list[tuple[str, str]] = [
    (r"^(git|hg|svn)(\s|$)",                                           "📦"),
    (r"^(npm|yarn|pnpm|apt|apt-get|yum|dnf|pacman|brew|pip|uv|bun|deno)(\s|$)", "📦"),
    (r"^(curl|wget|http|aria2c)(\s|$)",                                "🌐"),
    (r"^(python[0-9.]*|node|ruby|perl|php|lua|rustc|zig|go)(\s|$)",   "🐍"),
    (r"^(docker|kubectl|helm|podman|k3s|terraform|ansible)(\s|$)",     "🐳"),
    (r"^(ls|ll|la|dir|pwd|mkdir|rm|cp|mv|touch|cat|grep|rg|fd|bat|eza|find|awk|sed|tr|sort|uniq|wc|jq|yq)(\s|$)", "📁"),
    (r"^(ffmpeg|ffprobe|convert|magick|sox)(\s|$)",                    "🎬"),
    (r"^(ffuf|gobuster|nmap|nikto|sqlmap|hydra)(\s|$)",                "🔍"),
    (r"^(ssh|scp|rsync|sftp|ftp)(\s|$)",                               "🔐"),
    (r"^(systemctl|service|journalctl|launchctl)(\s|$)",               "⚙️ "),
    (r"^(make|cmake|ninja|gcc|g\+\+|clang|cargo)(\s|$)",               "🔨"),
    (r"^(tar|zip|unzip|gzip|bzip2|xz|7z)(\s|$)",                      "🗜️ "),
    (r"^(mysql|psql|sqlite3|mongo|redis-cli)(\s|$)",                   "🗄️ "),
    (r"^(vi|vim|nvim|nano|emacs|code)(\s|$)",                          "✏️ "),
]


def get_cmd_icon(cmd: str) -> str:
    """Return an emoji icon representing the leading command."""
    stripped = cmd.lstrip()
    for pattern, icon in _ICON_PATTERNS:
        if re.match(pattern, stripped):
            return icon
    return "⚡"


# ==============================================================================
# SECTION 7: Exit Code Interpretation & Shadow Hints
# ==============================================================================

_EXIT_CODES: dict[int, str] = {
    0:   "Success",
    1:   "General error",
    2:   "Misuse of shell builtins or permission error",
    3:   "No such process",
    13:  "Permission denied",
    17:  "File exists",
    28:  "No space left on device",
    111: "Connection refused",
    124: "Command timed out",
    125: "timeout binary itself failed",
    126: "Permission denied (cannot execute)",
    127: "Command not found",
    128: "Invalid exit argument",
    129: "Received SIGHUP",
    130: "Terminated by Ctrl-C (SIGINT)",
    131: "Quit (SIGQUIT)",
    132: "Illegal instruction (SIGILL)",
    133: "Trace/breakpoint trap (SIGTRAP)",
    134: "Aborted (SIGABRT)",
    135: "Bus error (SIGBUS)",
    136: "Floating point exception (SIGFPE)",
    137: "Killed (SIGKILL)",
    138: "User defined signal 1 (SIGUSR1)",
    139: "Segmentation fault (SIGSEGV)",
    140: "User defined signal 2 (SIGUSR2)",
    141: "Broken pipe (SIGPIPE)",
    142: "Alarm clock (SIGALRM)",
    143: "Terminated (SIGTERM)",
    152: "CPU time limit exceeded (SIGXCPU)",
    153: "File size limit exceeded (SIGXFSZ)",
    255: "Exit status out of range / SSH error",
}


def interpret_exit_code(code: int) -> str:
    """Return a human-readable description of a process exit code."""
    if code in _EXIT_CODES:
        return _EXIT_CODES[code]
    if code > 128:
        return f"Killed by signal {code - 128}"
    return f"Unknown exit code {code}"


_SHADOW_HINTS: list[tuple[str, str]] = [
    (
        "invalid JSON data",
        "⚠️  HINT: Output contains 'invalid JSON data'. Command may be shadowed by an AIChat tool symlink. Use 'command <cmd>' or full path.",
    ),
    (
        "function not found",
        "⚠️  HINT: 'function not found' — command intercepted by function/alias. Use absolute path.",
    ),
    (
        "command not found: aichat",
        "⚠️  HINT: 'aichat' missing or shadowed. Verify installation or PATH environment variable.",
    ),
    (
        "permission denied: /llm-functions",
        "⚠️  HINT: Intercepted by wrapper script in /llm-functions. Use direct binary path.",
    ),
]


def detect_shadowing_hint(output: str) -> Optional[str]:
    """Detect tool shadowing based on standard output strings."""
    for trigger, hint in _SHADOW_HINTS:
        if trigger in output:
            return hint
    return None


# ==============================================================================
# SECTION 8: Terminal UI & Header/Footer Rendering
# ==============================================================================

def _border(width: int) -> str:
    return BOX_H * max(width, 10)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    """Render a human-friendly box UI to stderr for interactive user sessions."""
    if not _is_tty() or no_color:
        return

    success = data.get("success", False)
    exit_code = data.get("exit_code", EXIT_ERROR)
    cmd = data.get("command", "")
    duration_ms = data.get("duration_ms", 0.0)
    output = data.get("output", "")
    hint = data.get("hint")

    bw = max(get_width() - 4, 20)
    border_str = _border(bw)
    icon = get_cmd_icon(cmd)
    display_cmd = _truncate(cmd, bw - 12)

    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "SUCCESS" if success else "FAILED"

    _cprint(f"{NEON_PURPLE}{BOX_TL}{border_str}{BOX_TR}{RESET}")
    _cprint(
        f"{NEON_PINK} {icon} {GLOW_CYAN}[EXEC v{__version__}]{RESET} "
        f"{status_color}{BOLD}{status_symbol} {status_text}{RESET} "
        f"{NEON_YELLOW}›{RESET} {BOLD}{display_cmd}{RESET}"
    )
    _cprint(
        f"{NEON_PURPLE}{BOX_V}{RESET} "
        f"{NEON_CYAN}Duration:{RESET} {NEON_LIME}{duration_ms}ms{RESET}  "
        f"{NEON_CYAN}Exit:{RESET} {status_color}{exit_code}{RESET}  "
        f"{NEON_CYAN}Cached:{RESET} {NEON_YELLOW}{data.get('cached', False)}{RESET}"
    )
    _cprint(f"{NEON_PURPLE}{BOX_LT}{border_str}{BOX_RT}{RESET}")

    if hint:
        _cprint(f"{NEON_PURPLE}{BOX_V}{RESET} {NEON_RED}{hint}{RESET}")

    if output.strip():
        for line in output.splitlines():
            _cprint(f"{NEON_PURPLE}{BOX_V}{RESET} {line}")
    else:
        _cprint(f"{NEON_PURPLE}{BOX_V}{RESET} {DIM}(Command produced no stdout/stderr){RESET}")

    if exit_code != 0:
        _cprint(f"{NEON_PURPLE}{BOX_LT}{border_str}{BOX_RT}{RESET}")
        _cprint(f"{NEON_PURPLE}{BOX_V}{RESET} {NEON_RED}Error Info:{RESET} {interpret_exit_code(exit_code)}")

    _cprint(f"{NEON_PURPLE}{BOX_BL}{border_str}{BOX_BR}{RESET}")


# ==============================================================================
# SECTION 9: Shell Resolution & Core Execution Engine
# ==============================================================================

_SHELL_MAP: dict[str, list[str]] = {
    "bash": ["/bin/bash", "-c"],
    "sh":   ["/bin/sh",   "-c"],
    "zsh":  ["/bin/zsh",  "-c"],
}
_TERMUX_PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr") + "/bin"
for _sh in ("bash", "sh", "zsh"):
    _candidate = f"{_TERMUX_PREFIX}/{_sh}"
    if Path(_candidate).is_file():
        _SHELL_MAP[_sh][0] = _candidate


def _resolve_shell(shell: str) -> list[str]:
    """Return [executable, flag] for requested shell string."""
    key = shell.lower()
    if key in _SHELL_MAP:
        return _SHELL_MAP[key]
    if Path(shell).is_file():
        return [shell, "-c"]
    return ["/bin/sh", "-c"]


def _kill_process_group(process: subprocess.Popen) -> None:
    """Safely terminate or kill process group with fallback."""
    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:
            pass


def run_command(
    cmd: str,
    timeout_sec: Optional[float],
    shell: str = "bash",
    cwd: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    strip_ansi: bool = False,
) -> tuple[str, int]:
    """
    Execute cmd via requested shell returning (output, exit_code).
    Handles process groups cleanly and enforces memory safety caps.
    """
    shell_args = _resolve_shell(shell)
    env = {**os.environ, **(extra_env or {})}
    preexec = os.setsid if hasattr(os, "setsid") else None

    process = None
    try:
        process = subprocess.Popen(
            [*shell_args, cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=preexec,
            cwd=cwd or None,
            env=env,
        )
        try:
            raw_bytes, _ = process.communicate(timeout=timeout_sec)
            if len(raw_bytes) > MAX_OUTPUT_BYTES:
                raw_bytes = raw_bytes[:MAX_OUTPUT_BYTES] + b"\n... [Output truncated at 20MB limit]\n"
            raw = raw_bytes.decode("utf-8", errors="replace")
            output = raw.replace("\r\n", "\n").replace("\r", "\n")
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            try:
                raw_bytes, _ = process.communicate()
                partial = raw_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            except Exception:
                partial = ""
            output = partial + f"\n[Timed out after {timeout_sec:.1f}s]\n"
            exit_code = EXIT_TIMEOUT

    except FileNotFoundError:
        output = f"[Shell binary not found: {shell_args[0]}]\n"
        exit_code = EXIT_INVALID_INPUT

    except KeyboardInterrupt:
        if process is not None:
            _kill_process_group(process)
            try:
                process.wait(timeout=1.0)
            except Exception:
                pass
        output = "\n[Command execution interrupted by user]\n"
        exit_code = EXIT_INTERRUPTED

    except Exception as exc:
        if process is not None:
            _kill_process_group(process)
            try:
                process.wait()
            except Exception:
                pass
        output = f"[Executor failure: {exc}]\n"
        exit_code = EXIT_ERROR

    if strip_ansi:
        output = _strip_ansi(output)

    return output, exit_code


# ==============================================================================
# SECTION 10: Primary Master Tool Execution Logic
# ==============================================================================

def execute_tool(
    command: str,
    timeout: Optional[str] = None,
    connect_timeout: Optional[str] = None,
    max_time: Optional[str] = None,
    working_dir: Optional[str] = None,
    env: Optional[list[str]] = None,
    shell: str = "bash",
    use_cache: bool = False,
    no_color: bool = False,
    strip_ansi: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Core master tool execution context shared between API run() and CLI.
    """
    global _NO_COLOR
    _NO_COLOR = no_color or not _is_tty()
    start_time = time.monotonic()

    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="[DEBUG] %(message)s")
        logging.debug(f"Executing command: {command}")

    sanitize_path()

    ct_raw = connect_timeout or "10s"
    mt_raw = max_time or timeout or "30s"
    timeout_sec = duration_to_seconds(timeout) if timeout else None
    ct_sec = duration_to_seconds(ct_raw)
    mt_sec = duration_to_seconds(mt_raw)

    extra_env = _parse_env_vars(env)
    cmd = inject_curl_timeouts(command, ct_sec, mt_sec)

    cwd: Optional[str] = None
    if working_dir:
        base_dir = get_builtin_var("__cwd__") or os.getcwd()
        wd = (Path(base_dir) / working_dir).expanduser().resolve()
        if wd.is_dir():
            cwd = str(wd)
        else:
            if verbose:
                logging.debug(f"Working dir not found: {working_dir}, using default CWD.")

    cache = ToolCache()
    cache_key = f"{cmd}:{cwd}:{shell}:{timeout_sec}:{strip_ansi}:{extra_env}"
    if use_cache:
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            if verbose:
                logging.debug("Cache hit for command execution!")
            cached_result["cached"] = True
            return cached_result

    shutdown = GracefulShutdown()

    output, exit_code = run_command(
        cmd=cmd,
        timeout_sec=timeout_sec,
        shell=shell,
        cwd=cwd,
        extra_env=extra_env,
        strip_ansi=strip_ansi,
    )

    duration_ms = round((time.monotonic() - start_time) * 1000, 2)
    hint = detect_shadowing_hint(output)
    lines_count = len(output.splitlines()) if output else 0
    bytes_count = len(output.encode("utf-8"))

    result: dict[str, Any] = {
        "success": exit_code == EXIT_SUCCESS,
        "command": cmd,
        "raw_command": command,
        "output": output,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "lines_count": lines_count,
        "bytes_count": bytes_count,
        "hint": hint,
        "cached": False,
        "shell": shell,
        "cwd": cwd or os.getcwd(),
        "context": get_execution_context(),
    }

    if shutdown.should_stop():
        result["success"] = False
        result["error"] = "Execution interrupted by signal."
        result["exit_code"] = EXIT_INTERRUPTED

    if use_cache and result["success"]:
        cache.set(cache_key, result)

    shutdown.restore()
    return result


# ==============================================================================
# SECTION 11: Output Routing (LLM vs Terminal)
# ==============================================================================

def write_llm_output(data: dict[str, Any]) -> None:
    """Format and write structured execution output to LLM_OUTPUT destination."""
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = json.dumps(data, indent=2, ensure_ascii=False, cls=ToolJSONEncoder) + "\n"

    direct_targets = {"/dev/stdout", "/dev/fd/1", "-", "/dev/stderr"}
    if out_path in direct_targets:
        sys.stdout.write(json_payload)
        sys.stdout.flush()
    else:
        try:
            p = Path(out_path).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fp:
                fp.write(json_payload)
        except OSError as err:
            sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
            sys.stdout.write(json_payload)
            sys.stdout.flush()


# ==============================================================================
# SECTION 12: Function Entry Point for AIChat
# ==============================================================================

def run(
    command: str,
    timeout: Optional[str] = None,
    connect_timeout: Optional[str] = None,
    max_time: Optional[str] = None,
    working_dir: Optional[str] = None,
    env: Optional[list[str]] = None,
    shell: str = "bash",
    use_cache: bool = False,
    no_color: bool = False,
    strip_ansi: bool = False,
    verbose: bool = False,
) -> None:
    """Execute the shell command with specified parameters.

    Args:
        command: Command string to run (required)
        timeout: Wall-clock timeout for command execution
        connect_timeout: Connection timeout injected into curl
        max_time: Max transfer time injected into curl commands
        working_dir: Working directory context
        env: Extra environment variables in KEY=VALUE format (repeatable)
        shell: Shell binary to execute command (bash/sh/zsh)
        use_cache: Enable result caching
        no_color: Disable ANSI color output
        strip_ansi: Strip ANSI sequences from process output
        verbose: Enable detailed debug logging
    """
    res = execute_tool(
        command=command,
        timeout=timeout,
        connect_timeout=connect_timeout,
        max_time=max_time,
        working_dir=working_dir,
        env=env,
        shell=shell,
        use_cache=use_cache,
        no_color=no_color,
        strip_ansi=strip_ansi,
        verbose=verbose,
    )

    print_human_readable_ui(res, no_color=no_color)
    write_llm_output(res)


# ==============================================================================
# SECTION 13: CLI Argument Parser & Runner
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execute_command.py",
        description=f"Pyrmethus Command Executor v{__version__}-ASCENDED",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--command", "-c",
        required=True,
        metavar="STRING",
        help="Command to run (required)",
    )
    parser.add_argument(
        "--timeout",
        metavar="DURATION",
        default=None,
        help="Wall-clock timeout for the command (e.g. 60s, 1m, 2h)",
    )
    parser.add_argument(
        "--connect-timeout",
        metavar="DURATION",
        default=None,
        dest="connect_timeout",
        help="Connection timeout injected into curl commands (default: 10s)",
    )
    parser.add_argument(
        "--max-time",
        metavar="DURATION",
        default=None,
        dest="max_time",
        help="Max transfer time injected into curl commands",
    )
    parser.add_argument(
        "--working-dir",
        metavar="PATH",
        default=None,
        dest="working_dir",
        help="Working directory for command execution",
    )
    parser.add_argument(
        "--env",
        metavar="KEY=VALUE",
        action="append",
        default=None,
        help="Extra environment variable (repeatable)",
    )
    parser.add_argument(
        "--shell",
        metavar="SHELL",
        default="bash",
        help="Shell interpreter to use (default: bash)",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=False,
        dest="use_cache",
        help="Enable caching for repeated commands",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        dest="no_color",
        help="Disable ANSI colour output",
    )
    parser.add_argument(
        "--strip-ansi",
        action="store_true",
        default=False,
        dest="strip_ansi",
        help="Strip ANSI codes from output before returning",
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
        command=args.command,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        max_time=args.max_time,
        working_dir=args.working_dir,
        env=args.env,
        shell=args.shell,
        use_cache=args.use_cache,
        no_color=args.no_color,
        strip_ansi=args.strip_ansi,
        verbose=args.verbose,
    )

    print_human_readable_ui(res, no_color=args.no_color)
    write_llm_output(res)
    sys.exit(res.get("exit_code", EXIT_SUCCESS))
