#!/usr/bin/env python3
# ==============================================================================
# execute_command.py — Pyrmethus Command Executor v1.2.0
# argc/aichat compatible · Termux · Secure shell command execution
#
# @describe Execute arbitrary shell command and return full output.
#
# @option --command! <STRING>            Command to run (required)
# @option --timeout <DURATION>           Duration for the command (e.g. 60s, 1m, 2h)
# @option --connect-timeout <DURATION>   Connection timeout for curl commands (default: 10s)
# @option --max-time <DURATION>          Max transfer time for curl commands (default: matches --timeout)
# @option --working-dir <PATH>           Working directory for the command (default: current dir)
# @option --env <KEY=VALUE>              Extra environment variable (repeatable)
# @option --shell <SHELL>                Shell to use: bash/sh/zsh (default: bash)
# @flag   --no-color                     Disable ANSI colour output
# @flag   --strip-ansi                   Strip ANSI codes from command output before returning
# @flag   --verbose                      Show extra debug info (PATH, shell, env vars)
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

__version__ = "1.2.0"

# ==============================================================================
# SECTION 1: Color Palette
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

# Box drawing
BOX_TL = "╭"; BOX_TR = "╮"; BOX_BL = "╰"; BOX_BR = "╯"
BOX_V  = "│"; BOX_H  = "─"; BOX_LT = "├"; BOX_RT = "┤"

# Global flag — set to True via --no-color or when stdout is not a TTY
_NO_COLOR: bool = False

# ANSI escape stripper
_ANSI_RE = re.compile(r"\033\[[0-9;]*[mGKHF]")


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    """Return True if stdout is a real terminal."""
    return sys.stdout.isatty()


def _cprint(text: str, end: str = "\n", file: Any = None) -> None:
    """
    Print pre-formatted ANSI text.

    Strips colour codes when:
      - stdout is not a TTY, OR
      - --no-color was requested (_NO_COLOR is True)
    """
    target = file or sys.stdout
    if _NO_COLOR or not _is_tty():
        text = _strip_ansi(text)
    print(text, end=end, flush=True, file=target)


# ==============================================================================
# SECTION 2: Duration helpers
# ==============================================================================

_DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([sSmMhHdD]?)$")

_UNIT_MULTIPLIERS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def duration_to_seconds(raw: str) -> float:
    """
    Convert a human-readable duration string to seconds (float).

    Accepts: '30s', '1m', '2h', '1d', or a bare number (seconds).
    Returns 0.0 on parse failure.
    """
    raw = (raw or "0").strip()
    m = _DURATION_RE.match(raw)
    if not m:
        return 0.0
    n    = float(m.group(1))
    unit = m.group(2).lower() or "s"
    return n * _UNIT_MULTIPLIERS.get(unit, 1.0)


def seconds_to_human(sec: float) -> str:
    """Return a compact human-readable representation of a duration in seconds."""
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}m"
    return f"{sec / 3600:.2f}h"


# ==============================================================================
# SECTION 3: PATH sanitization
# ==============================================================================

def sanitize_path() -> None:
    """
    Remove llm-functions/bin entries from PATH to prevent recursive shadowing.

    AIChat tools in that directory expect JSON input and will fail when called
    as ordinary shell commands (e.g. curl, python, ls).
    """
    raw   = os.environ.get("PATH", "")
    parts = [
        p for p in raw.split(os.pathsep)
        if p and not p.endswith("/llm-functions/bin")
    ]
    os.environ["PATH"] = os.pathsep.join(parts)


# ==============================================================================
# SECTION 4: curl / wget timeout injection
# ==============================================================================

def _find_binary(name: str) -> str:
    """
    Locate the absolute path for a binary, checking Termux paths first.

    Falls back to shutil.which and finally the bare name.
    """
    candidates: list[str] = []
    if name == "curl":
        candidates = [
            "/usr/bin/curl",
            "/data/data/com.termux/files/usr/bin/curl",
        ]
    elif name == "wget":
        candidates = [
            "/usr/bin/wget",
            "/data/data/com.termux/files/usr/bin/wget",
        ]
    for c in candidates:
        if Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return shutil.which(name) or name


def inject_curl_timeouts(cmd: str, connect_timeout: float, max_time: float) -> str:
    """
    Prepend missing timeout / retry / silent flags to curl or wget invocations.

    Only modifies the command when curl/wget appears at the very start (after
    optional leading whitespace).  Uses the absolute binary path to prevent
    AIChat tool-symlink shadowing.

    Improvements v1.2.0:
      - Handles 'curl' with no trailing space (bare invocation)
      - wget: also inject --no-verbose only when not already present
      - Preserves original leading whitespace
    """
    stripped = cmd.lstrip()
    leading  = cmd[: len(cmd) - len(stripped)]

    # ── curl ──────────────────────────────────────────────────────────────────
    if re.match(r"^curl(\s|$)", stripped):
        flags: list[str] = []
        if "--connect-timeout" not in cmd:
            flags += ["--connect-timeout", str(int(connect_timeout))]
        if "--max-time" not in cmd:
            flags += ["--max-time", str(int(max_time))]
        if "--retry" not in cmd:
            flags += ["--retry", "3", "--retry-delay", "2"]
        # Add --silent only when no output-related flags are present
        silent_absent = (
            "--silent"   not in cmd
            and " -s "   not in cmd
            and " -sS "  not in cmd
            and not re.search(r"\s-[a-zA-Z]*s", cmd)
        )
        if silent_absent:
            flags.append("--silent")

        curl_bin = _find_binary("curl")
        rest     = stripped[len("curl"):].lstrip()
        return f"{leading}{curl_bin} {' '.join(flags)} {rest}".strip()

    # ── wget ──────────────────────────────────────────────────────────────────
    if re.match(r"^wget(\s|$)", stripped) and "--timeout" not in cmd:
        wget_bin = _find_binary("wget")
        rest     = stripped[len("wget"):].lstrip()
        extra    = f"--timeout={int(max_time)}"
        if "--no-verbose" not in cmd and "-nv" not in cmd:
            extra += " --no-verbose"
        return f"{leading}{wget_bin} {extra} {rest}".strip()

    return cmd


# ==============================================================================
# SECTION 5: Terminal / timing helpers
# ==============================================================================

def now_ms() -> int:
    """Return current epoch time in milliseconds."""
    return int(time.monotonic_ns() // 1_000_000)


def now_wall_ms() -> int:
    """Return wall-clock time in milliseconds (for display timestamps)."""
    return int(time.time() * 1000)


def get_width() -> int:
    """Return the current terminal column count, falling back to 80."""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


# ==============================================================================
# SECTION 6: Command icon
# ==============================================================================

_ICON_PATTERNS: list[tuple[str, str]] = [
    (r"^(git|hg|svn)(\s|$)",                                           "📦"),
    (r"^(npm|yarn|pnpm|apt|apt-get|yum|dnf|pacman|brew|pip)(\s|$)",   "📦"),
    (r"^(curl|wget)(\s|$)",                                             "🌐"),
    (r"^(python[0-9.]*|node|ruby|perl|php|lua)(\s|$)",                 "🐍"),
    (r"^(docker|kubectl|helm|podman|k3s)(\s|$)",                       "🐳"),
    (r"^(ls|ll|la|dir|pwd|mkdir|rm|cp|mv|touch|cat|grep|find|awk|sed|tr|sort|uniq|wc)(\s|$)", "📁"),
    (r"^(ffmpeg|ffprobe|convert|magick|sox)(\s|$)",                    "🎬"),
    (r"^(ffuf|gobuster|nmap|nikto|sqlmap|hydra)(\s|$)",                "🔍"),
    (r"^(ssh|scp|rsync|sftp|ftp)(\s|$)",                               "🔐"),
    (r"^(systemctl|service|journalctl|launchctl)(\s|$)",               "⚙️ "),
    (r"^(make|cmake|ninja|gcc|g\+\+|clang|cargo|go)(\s|$)",           "🔨"),
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
# SECTION 7: Exit-code interpretation
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
    134: "Aborted (SIGABRT)",
    135: "Bus error (SIGBUS)",
    136: "Floating point exception (SIGFPE)",
    137: "Killed (SIGKILL)",
    139: "Segmentation fault (SIGSEGV)",
    141: "Broken pipe (SIGPIPE)",
    143: "Terminated (SIGTERM)",
    255: "Exit status out of range / SSH error",
}


def interpret_exit_code(code: int) -> str:
    """Return a human-readable description of a process exit code."""
    if code in _EXIT_CODES:
        return _EXIT_CODES[code]
    if code > 128:
        return f"Killed by signal {code - 128}"
    return f"Unknown exit code {code}"


# ==============================================================================
# SECTION 8: Box-drawing UI helpers
# ==============================================================================

def _border(width: int) -> str:
    return BOX_H * max(width, 10)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters, appending '…' if cut."""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def print_header(cmd: str, icon: str, timeout_str: str, verbose: bool = False) -> None:
    """Print the neon-styled execution header (TTY only)."""
    if not _is_tty() or _NO_COLOR:
        return
    bw     = max(get_width() - 4, 20)
    border = _border(bw)
    ts     = time.strftime("%H:%M:%S")

    # Truncate very long commands so the UI doesn't wrap badly
    display_cmd = _truncate(cmd, bw - 12)

    _cprint(f"{NEON_PURPLE}{BOX_TL}{border}{BOX_TR}{RESET}")
    _cprint(
        f"{NEON_PINK} {icon} {GLOW_CYAN}[EXEC]{RESET} "
        f"{NEON_YELLOW}›{RESET} {BOLD}{display_cmd}{RESET}"
    )
    _cprint(
        f"{NEON_PURPLE}{BOX_V}{RESET} "
        f"{NEON_CYAN}Time:{RESET} {NEON_YELLOW}{ts}{RESET}  "
        f"{NEON_CYAN}Timeout:{RESET} {NEON_ORANGE}{timeout_str}{RESET}  "
        f"{NEON_CYAN}PID:{RESET} {DIM}{os.getpid()}{RESET}"
    )
    if verbose:
        _cprint(
            f"{NEON_PURPLE}{BOX_V}{RESET} "
            f"{DIM}PATH={os.environ.get('PATH', '')[:80]}…{RESET}"
        )
    _cprint(f"{NEON_PURPLE}{BOX_LT}{border}{BOX_RT}{RESET}")


def print_output_lines(output: str) -> None:
    """Stream output lines to the TTY with the box-border prefix."""
    if not output.strip():
        return
    for line in output.splitlines():
        _cprint(f"{NEON_PURPLE}{BOX_V}{RESET} {line}")


def print_footer(exit_code: int, duration_ms: int) -> None:
    """Print the neon-styled footer (TTY only)."""
    if not _is_tty() or _NO_COLOR:
        return
    bw           = max(get_width() - 4, 20)
    border       = _border(bw)
    status_color = NEON_GREEN if exit_code == 0 else NEON_RED
    symbol       = "✓" if exit_code == 0 else "✗"
    status_text  = "SUCCESS" if exit_code == 0 else "FAILED"

    _cprint(f"{NEON_PURPLE}{BOX_LT}{border}{BOX_RT}{RESET}")
    line = (
        f"{NEON_PURPLE}{BOX_V}{RESET} "
        f"{status_color}{symbol} {status_text}{RESET}  "
        f"{NEON_CYAN}Duration:{RESET} {NEON_LIME}{duration_ms}ms{RESET}  "
        f"{NEON_CYAN}Exit:{RESET} {status_color}{exit_code}{RESET}"
    )
    if exit_code != 0:
        line += f"  {DIM}({interpret_exit_code(exit_code)}){RESET}"
    _cprint(line)
    _cprint(f"{NEON_PURPLE}{BOX_BL}{border}{BOX_BR}{RESET}")


# ==============================================================================
# SECTION 9: Command execution
# ==============================================================================

# Shells we support, with their flag to run a command string
_SHELL_MAP: dict[str, list[str]] = {
    "bash": ["/bin/bash",  "-c"],
    "sh":   ["/bin/sh",    "-c"],
    "zsh":  ["/bin/zsh",   "-c"],
}
# Termux overrides
_TERMUX_PREFIX = "/data/data/com.termux/files/usr/bin"
for _sh in ("bash", "sh", "zsh"):
    _candidate = f"{_TERMUX_PREFIX}/{_sh}"
    if Path(_candidate).is_file():
        _SHELL_MAP[_sh][0] = _candidate


def _resolve_shell(shell: str) -> list[str]:
    """Return the [executable, flag] pair for the requested shell name."""
    key = shell.lower()
    if key in _SHELL_MAP:
        return _SHELL_MAP[key]
    # Accept absolute paths directly
    if Path(shell).is_file():
        return [shell, "-c"]
    # Fallback: use /bin/sh
    return ["/bin/sh", "-c"]


def run_command(
    cmd:         str,
    timeout_sec: Optional[float],
    shell:       str  = "bash",
    cwd:         Optional[str]  = None,
    extra_env:   Optional[dict[str, str]] = None,
    strip_ansi:  bool = False,
) -> tuple[str, int]:
    """
    Execute cmd via the requested shell; return (output, exit_code).

    Improvements v1.2.1:
      - Accepts working-dir (cwd) and extra environment variables
      - Configurable shell (bash / sh / zsh or absolute path)
      - Optional ANSI stripping from command output
      - Kills entire process group on timeout or interrupt (no orphan processes)
      - Captures both stdout and stderr merged into one stream
      - Normalises Windows-style CRLF line endings in output
      - Gracefully handles KeyboardInterrupt
    """
    shell_args = _resolve_shell(shell)
    env        = {**os.environ, **(extra_env or {})}
    preexec    = os.setsid if hasattr(os, "setsid") else None

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
            raw       = raw_bytes.decode("utf-8", errors="replace")
            output    = raw.replace("\r\n", "\n").replace("\r", "\n")
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            # Kill entire process group so no children are left running
            if hasattr(os, "killpg") and preexec is not None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, AttributeError, OSError):
                    pass
            else:
                process.kill()
            try:
                raw_bytes, _ = process.communicate()
                partial = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                partial = ""
            output    = partial + f"\n[Timed out after {timeout_sec:.1f}s]\n"
            exit_code = 124

    except FileNotFoundError:
        output    = f"[Shell not found: {shell_args[0]}]\n"
        exit_code = 127

    except KeyboardInterrupt:
        if process is not None:
            if hasattr(os, "killpg") and preexec is not None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, AttributeError, OSError):
                    pass
            else:
                process.kill()
            try:
                process.wait(timeout=1.0)
            except Exception:
                pass
        output    = "\n[Command interrupted by user]\n"
        exit_code = 130

    except Exception as exc:  # pragma: no cover
        if process is not None:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass
        output    = f"[Executor error: {exc}]\n"
        exit_code = 1

    if strip_ansi:
        output = _strip_ansi(output)

    return output, exit_code


# ==============================================================================
# SECTION 10: Shadowing hint detection
# ==============================================================================

# Patterns that suggest the wrong binary was invoked
_SHADOW_HINTS: list[tuple[str, str]] = [
    (
        "invalid JSON data",
        "⚠️  HINT: Output contains 'invalid JSON data'. A system command may be\n"
        "    shadowed by an AIChat tool symlink. Try using 'command <cmd>' or\n"
        "    an absolute path (e.g. /usr/bin/curl) to bypass shadowing.",
    ),
    (
        "function not found",
        "⚠️  HINT: 'function not found' — a shell function or alias may be\n"
        "    intercepting this command. Try prefixing with 'env ' or using\n"
        "    the full absolute path.",
    ),
]


def detect_shadowing_hint(output: str) -> Optional[str]:
    """
    Return a formatted hint string when output suggests command shadowing,
    otherwise None.
    """
    for trigger, hint in _SHADOW_HINTS:
        if trigger in output:
            return hint
    return None


# ==============================================================================
# SECTION 11: Output routing
# ==============================================================================

_DIRECT_OUTPUTS: frozenset[str] = frozenset(
    {"/dev/stdout", "/dev/stderr", "/dev/fd/1", "&1", "&2", "-"}
)


def _build_llm_body(
    output:    str,
    exit_code: int,
    hint:      Optional[str],
    duration_ms: int,
    cmd:       str,
) -> str:
    """
    Compose the text body that will be written to LLM_OUTPUT.

    Includes: optional shadowing hint, command output, timing summary,
    and an error report section on non-zero exit.
    """
    parts: list[str] = []

    if hint:
        parts.append(hint + "\n\n")

    if output.strip():
        parts.append(output)
        if not output.endswith("\n"):
            parts.append("\n")
    else:
        parts.append(f"[Command produced no output. Exit code: {exit_code}]\n")

    # Compact footer for the LLM
    parts.append(
        f"\n[Duration: {duration_ms}ms | Exit: {exit_code}"
        f"{' | ' + interpret_exit_code(exit_code) if exit_code != 0 else ''}]\n"
    )

    if exit_code != 0:
        desc = interpret_exit_code(exit_code)
        parts.append(
            f"\n--- ERROR REPORT ---\n"
            f"COMMAND  : {cmd}\n"
            f"EXIT CODE: {exit_code} — {desc}\n"
        )

    return "".join(parts)


def write_to_llm_output(
    output:      str,
    exit_code:   int,
    hint:        Optional[str],
    duration_ms: int,
    cmd:         str,
    out_path:    str,
) -> None:
    """
    Write captured output (plus metadata) to LLM_OUTPUT.

    Direct paths (/dev/stdout etc.) → write to sys.stdout.
    File paths → open in append mode (safe for concurrent callers).
    """
    body = _build_llm_body(output, exit_code, hint, duration_ms, cmd)

    if out_path in _DIRECT_OUTPUTS:
        sys.stdout.write(body)
        sys.stdout.flush()
    else:
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "a", encoding="utf-8") as fp:
                fp.write(body)
        except OSError as exc:
            _cprint(
                f"{NEON_RED}[execute_command] Cannot write to '{out_path}': {exc}{RESET}",
                file=sys.stderr,
            )
            sys.stdout.write(body)
            sys.stdout.flush()


# ==============================================================================
# SECTION 12: Core logic (shared by run() and CLI __main__)
# ==============================================================================

def _execute(
    command:         str,
    timeout:         Optional[str]       = None,
    connect_timeout: Optional[str]       = None,
    max_time:        Optional[str]       = None,
    working_dir:     Optional[str]       = None,
    env:             Optional[list[str]] = None,
    shell:           str                 = "bash",
    no_color:        bool                = False,
    strip_ansi:      bool                = False,
    verbose:         bool                = False,
) -> dict[str, Any]:
    """
    Shared execution core — used by both run() and the CLI __main__ block.

    Returns a result dict:
      success     (bool)   — True when exit_code == 0
      output      (str)    — combined stdout + stderr
      exit_code   (int)
      duration_ms (int)
      command     (str)    — final command after timeout injection
    """
    global _NO_COLOR
    _NO_COLOR = no_color or not _is_tty()

    # Sanitize PATH first so all subsequent shutil.which / subprocess calls
    # use the clean PATH
    sanitize_path()

    # Parse durations
    ct_raw      = connect_timeout or "10s"
    mt_raw      = max_time or timeout or "30s"
    timeout_sec = duration_to_seconds(timeout) if timeout else None
    ct_sec      = duration_to_seconds(ct_raw)
    mt_sec      = duration_to_seconds(mt_raw)

    # Parse extra environment variables (KEY=VALUE pairs)
    extra_env: dict[str, str] = {}
    for item in (env or []):
        if "=" in item:
            k, _, v = item.partition("=")
            extra_env[k.strip()] = v
        else:
            _cprint(
                f"{NEON_YELLOW}⚠ Ignoring malformed --env value: {item!r}{RESET}",
                file=sys.stderr,
            )

    # Inject curl/wget timeouts
    cmd  = inject_curl_timeouts(command, ct_sec, mt_sec)
    icon = get_cmd_icon(cmd)

    # Validate working directory
    cwd: Optional[str] = None
    if working_dir:
        wd = Path(working_dir).expanduser().resolve()
        if wd.is_dir():
            cwd = str(wd)
        else:
            _cprint(
                f"{NEON_YELLOW}⚠ Working directory not found: {working_dir!r} "
                f"— using current directory{RESET}",
                file=sys.stderr,
            )

    if verbose:
        _cprint(
            f"{DIM}[debug] shell={shell}  cwd={cwd or os.getcwd()}  "
            f"timeout={timeout_sec}s  extra_env={extra_env}{RESET}",
            file=sys.stderr,
        )

    # Header UI
    start_ms = now_ms()
    print_header(cmd, icon, timeout or "none", verbose=verbose)

    # Execute
    output, exit_code = run_command(
        cmd,
        timeout_sec,
        shell=shell,
        cwd=cwd,
        extra_env=extra_env,
        strip_ansi=strip_ansi,
    )
    duration_ms = now_ms() - start_ms

    # Shadowing hint
    hint = detect_shadowing_hint(output)

    # ── TTY display ──────────────────────────────────────────────────────────
    if _is_tty():
        if hint:
            _cprint(f"{NEON_RED}{hint}{RESET}")
        if output.strip():
            print_output_lines(output)
        else:
            _cprint(
                f"{NEON_PURPLE}{BOX_V}{RESET} "
                f"{NEON_YELLOW}Command produced no output "
                f"(exit {exit_code}).{RESET}"
            )
        print_footer(exit_code, duration_ms)
    elif exit_code != 0:
        desc = interpret_exit_code(exit_code)
        _cprint(
            f"✗ FAILED: Exit {exit_code} ({desc}) "
            f"[{duration_ms}ms]",
            file=sys.stderr,
        )

    # ── Write to LLM_OUTPUT ──────────────────────────────────────────────────
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    write_to_llm_output(output, exit_code, hint, duration_ms, cmd, out_path)

    return {
        "success":     exit_code == 0,
        "output":      output,
        "exit_code":   exit_code,
        "duration_ms": duration_ms,
        "command":     cmd,
    }


# ==============================================================================
# SECTION 13: run() — required aichat tool entry point
# ==============================================================================

def run(
    command:         str,
    timeout:         Optional[str]       = None,
    connect_timeout: Optional[str]       = None,
    max_time:        Optional[str]       = None,
    working_dir:     Optional[str]       = None,
    env:             Optional[list[str]] = None,
    shell:           str                 = "bash",
    no_color:        bool                = False,
    strip_ansi:      bool                = False,
    verbose:         bool                = False,
) -> None:
    """
    Primary entry point called by the aichat tool infrastructure.

    Parameter names must exactly match the @option / @flag slugs in the
    module docstring (hyphens become underscores).
    The function writes all output to LLM_OUTPUT (or stdout) and intentionally
    returns None — aichat ignores tool return values.
    """
    _execute(
        command=command,
        timeout=timeout,
        connect_timeout=connect_timeout,
        max_time=max_time,
        working_dir=working_dir,
        env=env,
        shell=shell,
        no_color=no_color,
        strip_ansi=strip_ansi,
        verbose=verbose,
    )


# ==============================================================================
# SECTION 14: CLI argument parser
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execute_command.py",
        description=f"Pyrmethus Command Executor v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python execute_command.py --command "pwd"
  python execute_command.py --command "ls -la ~" --working-dir /tmp
  python execute_command.py --command "curl https://example.com" --timeout 30s
  python execute_command.py --command "sleep 10" --timeout 5s
  python execute_command.py --command "git status" --timeout 1m --shell zsh
  python execute_command.py --command "echo $FOO" --env FOO=bar --env BAZ=qux
  python execute_command.py --command "wget https://example.com/file" --max-time 120s
  python execute_command.py --command "make build" --working-dir ~/project --verbose
        """,
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
        help="Max transfer time injected into curl commands (default: matches --timeout)",
    )
    parser.add_argument(
        "--working-dir",
        metavar="PATH",
        default=None,
        dest="working_dir",
        help="Working directory for the command (default: current dir)",
    )
    parser.add_argument(
        "--env",
        metavar="KEY=VALUE",
        action="append",
        default=None,
        help="Extra environment variable (repeatable, e.g. --env FOO=bar)",
    )
    parser.add_argument(
        "--shell",
        metavar="SHELL",
        default="bash",
        choices=["bash", "sh", "zsh"],
        help="Shell to use: bash / sh / zsh (default: bash)",
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
        help="Strip ANSI codes from command output before returning",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Show extra debug info (PATH, shell, cwd, env)",
    )
    return parser


# ==============================================================================
# SECTION 15: Entry point
# ==============================================================================

if __name__ == "__main__":
    _args  = _build_parser().parse_args()
    result = _execute(
        command=_args.command,
        timeout=_args.timeout,
        connect_timeout=_args.connect_timeout,
        max_time=_args.max_time,
        working_dir=_args.working_dir,
        env=_args.env,
        shell=_args.shell,
        no_color=_args.no_color,
        strip_ansi=_args.strip_ansi,
        verbose=_args.verbose,
    )
    sys.exit(0 if result["success"] else result["exit_code"])