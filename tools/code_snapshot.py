#!/usr/bin/env python3
# ==============================================================================
# code_snapshot.py — Neon Syntax Highlighting Code Snapshot Tool for AIChat
# Pyrmethus AIChat Tool Master Template v2.3.0-ASCENDED
#
# @describe Generates high-resolution code snapshot images with neon dark themes,
#           window frames, syntax highlighting, safe caching, robust text input,
#           and deterministic rendering.
#
# @meta require-tools aichat
#
# @option --target! <PATH_OR_CODE>        Path to code file OR inline code text string (required)
# @option --output <PATH>                 Destination image path (default: snapshot.png)
# @option --theme <THEME>                 Neon theme: cyberpunk/matrix/synthwave/tokyo-night (default: cyberpunk)
# @option --language <LANG>               Programming language (e.g., python, js, rust, auto) (default: auto)
# @option --title <TITLE>                 Custom title bar text (defaults to filename or language)
# @option --font-size <NUM>               Font size in pixels (default: 18)
# @flag   --line-numbers                  Display line numbers
# @flag   --window-frame                  Draw macOS-style window frame with control buttons
# @flag   --glow-effect                   Add outer neon glow border effect
# @flag   --use-cache                     Enable result caching for identical renders
# @flag   --no-color                      Disable ANSI color terminal output
# @flag   --verbose                       Enable detailed debug log output
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, List, Literal, Optional, Tuple

# ==============================================================================
# Dependency Verification & Imports
# ==============================================================================

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    sys.stderr.write("Missing dependency 'Pillow'. Install via: pip install Pillow\n")
    sys.exit(127)

try:
    import pygments
    from pygments.lexers import get_lexer_by_name, guess_lexer
    from pygments.token import Token

    HAS_PYGMENTS = True
except ImportError:
    HAS_PYGMENTS = False

__version__ = "2.3.0"

__all__ = [
    "ToolCache",
    "ToolError",
    "__version__",
    "execute_tool",
    "get_agent_var",
    "get_builtin_var",
    "get_execution_context",
    "render_code_snapshot",
    "run",
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

DEFAULT_THEME = "cyberpunk"
DEFAULT_LANGUAGE = "auto"
DEFAULT_OUTPUT = "snapshot.png"
DEFAULT_FONT_SIZE = 18
DEFAULT_CACHE_TTL = 3600

# Guardrails prevent accidental multi-gigabyte images from huge input.
MIN_FONT_SIZE = 9
MAX_FONT_SIZE = 96
MAX_CODE_CHARS = 2_000_000
MAX_CODE_LINES = 50_000
MAX_IMAGE_WIDTH = 20_000
MAX_IMAGE_HEIGHT = 100_000

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
    """JSON encoder for common tool/runtime objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            return obj.total_seconds()
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if isinstance(obj, (set, frozenset)):
            return sorted(obj, key=str)
        return super().default(obj)


# ==============================================================================
# SECTION 2: Terminal Color Palette & UI Helpers
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

_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\033\[[0-9;?]*[a-zA-Z]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(
    text: str, file: Any = None, no_color: bool = False, end: str = "\n"
) -> None:
    target = file or sys.stderr
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    if not _is_tty() or no_color:
        return

    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "SUCCESS" if success else "FAILED"

    box_w = 64
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [CODE SNAPSHOT RENDERER v{__version__}]{RESET} "
        f"{status_color}{BOLD}{status_symbol} {status_text}{RESET}"
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Output Path:{RESET} "
        f"{NEON_GREEN}{data.get('output_path', 'N/A')}{RESET}"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Theme:{RESET}       "
        f"{NEON_PINK}{data.get('theme', 'N/A')}{RESET}"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Language:{RESET}    "
        f"{NEON_YELLOW}{data.get('language', 'N/A')}{RESET}"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Lines:{RESET}       "
        f"{data.get('line_count', 0)}"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Dimensions:{RESET}  "
        f"{data.get('width', 0)}x{data.get('height', 0)} px"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Cached:{RESET}      "
        f"{data.get('cached', False)}"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Duration:{RESET}    "
        f"{DIM}{data.get('duration_ms', 0)}ms{RESET}"
    )

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_RED}Error:{RESET}       {data['error']}")

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")


# ==============================================================================
# SECTION 3: Agent & Environment Helpers
# ==============================================================================

def get_agent_var(name: str, default: str = "") -> str:
    env_name = f"LLM_AGENT_VAR_{name.upper()}"
    return os.environ.get(env_name, default)


def get_builtin_var(name: str) -> Optional[str]:
    env_name = f"LLM_AGENT_VAR_{name}"
    return os.environ.get(env_name)


def get_execution_context() -> dict[str, Any]:
    termux_prefix = os.environ.get("PREFIX", "")
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME"),
        "cache_dir": os.environ.get("LLM_TOOL_CACHE_DIR"),
        "root_dir": os.environ.get("LLM_ROOT_DIR"),
        "output_path": os.environ.get("LLM_OUTPUT"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "termux_prefix": termux_prefix,
        "is_termux": "com.termux" in termux_prefix
        or Path("/data/data/com.termux").exists(),
    }


# ==============================================================================
# SECTION 4: Native Caching & Signal Handlers
# ==============================================================================

class ToolCache:
    """
    Small JSON cache compatible with the previous ToolCache API.

    The old implementation used pickle, which can execute arbitrary code when
    unpickling a tampered cache file. JSON keeps cache reads data-only.
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        elif os.environ.get("LLM_TOOL_CACHE_DIR"):
            self.cache_dir = Path(os.environ["LLM_TOOL_CACHE_DIR"])
        else:
            self.cache_dir = Path.home() / ".cache" / "aichat_tools"

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _make_key(self, key_data: str) -> str:
        return hashlib.sha256(
            f"code_snapshot:{__version__}:{key_data}".encode("utf-8")
        ).hexdigest()

    def get(self, key_data: str, ttl_seconds: int = DEFAULT_CACHE_TTL) -> Optional[Any]:
        cache_file = self.cache_dir / f"{self._make_key(key_data)}.json"
        try:
            stat = cache_file.stat()
            if time.time() - stat.st_mtime > max(0, int(ttl_seconds)):
                cache_file.unlink(missing_ok=True)
                return None

            with cache_file.open("r", encoding="utf-8") as fp:
                value = json.load(fp)

            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def set(self, key_data: str, value: Any) -> None:
        cache_file = self.cache_dir / f"{self._make_key(key_data)}.json"
        tmp_file: Optional[Path] = None

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{cache_file.stem}.",
                suffix=".tmp",
                dir=str(self.cache_dir),
            )
            tmp_file = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(
                    value,
                    fp,
                    ensure_ascii=False,
                    sort_keys=True,
                    cls=ToolJSONEncoder,
                )
                fp.flush()
                os.fsync(fp.fileno())

            tmp_file.replace(cache_file)
        except (OSError, TypeError, ValueError):
            if tmp_file:
                try:
                    tmp_file.unlink(missing_ok=True)
                except OSError:
                    pass


class GracefulShutdown:
    """Signal state tracker; does not raise from inside signal handlers."""

    def __init__(self) -> None:
        self.interrupted = False
        self._old_sigint = None
        self._old_sigterm = None

        try:
            self._old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
            self._old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
        except (ValueError, OSError):
            # Signal registration can fail outside the main thread.
            pass

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def restore(self) -> None:
        try:
            if self._old_sigint is not None:
                signal.signal(signal.SIGINT, self._old_sigint)
            if self._old_sigterm is not None:
                signal.signal(signal.SIGTERM, self._old_sigterm)
        except (ValueError, OSError):
            pass

    def should_stop(self) -> bool:
        return self.interrupted


# ==============================================================================
# SECTION 5: Neon Themes & Syntax Tokenizer
# ==============================================================================

THEMES = {
    "cyberpunk": {
        "bg": (13, 15, 24),
        "window_bg": (22, 25, 43),
        "border": (255, 0, 127),
        "glow": (255, 0, 127, 100),
        "title": (0, 246, 255),
        "line_num": (80, 90, 120),
        "default": (248, 248, 242),
        "keyword": (255, 0, 127),
        "string": (0, 246, 255),
        "comment": (98, 114, 164),
        "function": (0, 255, 102),
        "number": (255, 190, 0),
        "operator": (255, 128, 0),
    },
    "matrix": {
        "bg": (3, 10, 5),
        "window_bg": (8, 20, 11),
        "border": (0, 255, 65),
        "glow": (0, 255, 65, 120),
        "title": (0, 255, 65),
        "line_num": (25, 75, 40),
        "default": (170, 255, 204),
        "keyword": (0, 255, 65),
        "string": (51, 204, 102),
        "comment": (0, 85, 25),
        "function": (102, 255, 170),
        "number": (204, 255, 0),
        "operator": (0, 200, 80),
    },
    "synthwave": {
        "bg": (18, 13, 28),
        "window_bg": (26, 19, 41),
        "border": (255, 126, 219),
        "glow": (120, 40, 200, 150),
        "title": (54, 249, 246),
        "line_num": (108, 103, 131),
        "default": (240, 239, 241),
        "keyword": (254, 68, 80),
        "string": (255, 126, 219),
        "comment": (108, 103, 131),
        "function": (54, 249, 246),
        "number": (254, 222, 93),
        "operator": (254, 150, 50),
    },
    "tokyo-night": {
        "bg": (22, 22, 30),
        "window_bg": (26, 27, 38),
        "border": (122, 162, 247),
        "glow": (122, 162, 247, 100),
        "title": (187, 154, 247),
        "line_num": (86, 95, 137),
        "default": (192, 202, 245),
        "keyword": (187, 154, 247),
        "string": (158, 206, 106),
        "comment": (86, 95, 137),
        "function": (122, 162, 247),
        "number": (255, 158, 100),
        "operator": (137, 221, 254),
    },
}

_LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "sh": "bash",
    "shell": "bash",
    "yml": "yaml",
    "md": "markdown",
    "c++": "cpp",
    "rs": "rust",
}


def _normalize_language(language: str) -> str:
    """Normalize common language aliases without changing caller-facing values."""
    value = str(language or DEFAULT_LANGUAGE).strip().lower()
    return _LANGUAGE_ALIASES.get(value, value)


def _tokenize_code(code: str, language: str) -> List[List[Tuple[str, str]]]:
    """Tokenize code into [(text, category)] lines with a regex fallback."""
    lines_tokens: List[List[Tuple[str, str]]] = []
    normalized_language = _normalize_language(language)

    if HAS_PYGMENTS:
        try:
            if normalized_language and normalized_language != "auto":
                lexer = get_lexer_by_name(normalized_language)
            else:
                lexer = guess_lexer(code)

            raw_tokens = pygments.lex(code, lexer)
            current_line: List[Tuple[str, str]] = []

            for tok_type, value in raw_tokens:
                category = "default"
                if tok_type in Token.Keyword or tok_type in Token.Keyword.Reserved:
                    category = "keyword"
                elif tok_type in Token.String:
                    category = "string"
                elif tok_type in Token.Comment:
                    category = "comment"
                elif tok_type in Token.Name.Function or tok_type in Token.Name.Class:
                    category = "function"
                elif tok_type in Token.Number:
                    category = "number"
                elif tok_type in Token.Operator:
                    category = "operator"

                parts = value.split("\n")
                for i, part in enumerate(parts):
                    if part:
                        current_line.append((part, category))
                    if i < len(parts) - 1:
                        lines_tokens.append(current_line)
                        current_line = []

            if current_line or not lines_tokens:
                lines_tokens.append(current_line)

            return lines_tokens
        except Exception as exc:
            logging.debug("Pygments tokenization failed; using fallback: %s", exc)

    kw_regex = re.compile(
        r"\b(def|class|return|if|else|elif|import|from|for|while|in|as|"
        r"try|except|finally|with|const|let|var|function|async|await|"
        r"fn|pub|struct|impl|match|enum|package|interface|extends|new)\b"
    )
    str_regex = re.compile(r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)")
    num_regex = re.compile(r"\b(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+(?:\.\d+)?)\b")
    comment_regex = re.compile(r"(#.*|//.*|--.*)$")

    for line in code.splitlines():
        tokens: List[Tuple[str, str]] = []
        pos = 0

        while pos < len(line):
            m_comm = comment_regex.match(line, pos)
            if m_comm:
                tokens.append((m_comm.group(0), "comment"))
                pos = len(line)
                break

            m_str = str_regex.match(line, pos)
            if m_str:
                tokens.append((m_str.group(0), "string"))
                pos = m_str.end()
                continue

            m_kw = kw_regex.match(line, pos)
            if m_kw:
                tokens.append((m_kw.group(0), "keyword"))
                pos = m_kw.end()
                continue

            m_num = num_regex.match(line, pos)
            if m_num:
                tokens.append((m_num.group(0), "number"))
                pos = m_num.end()
                continue

            tokens.append((line[pos], "default"))
            pos += 1

        lines_tokens.append(tokens)

    if not lines_tokens:
        lines_tokens.append([])

    return lines_tokens


# ==============================================================================
# SECTION 6: Snapshot Rendering Logic
# ==============================================================================

def _load_font(font_size: int) -> ImageFont.ImageFont:
    """Load a broadly available monospace font with graceful fallbacks."""
    if not MIN_FONT_SIZE <= int(font_size) <= MAX_FONT_SIZE:
        raise ToolError(
            f"font_size must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE}.",
            EXIT_INVALID_INPUT,
        )
    candidates = [
        os.environ.get("LLM_CODE_SNAPSHOT_FONT", ""),
        "DejaVuSansMono.ttf",
        "LiberationMono-Regular.ttf",
        "Courier New.ttf",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, font_size)
        except OSError:
            continue

    return ImageFont.load_default()


def _safe_text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    if not text:
        return 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return max(0, bbox[2] - bbox[0])


def _save_png_atomic(image: Image.Image, output_path: Path) -> None:
    """Write PNG through a temporary file and atomically replace the destination."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Keep the temporary file beside the destination so replace() remains atomic
    # on normal local filesystems.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}.",
        suffix=".png.tmp",
        dir=str(output_path.parent),
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as fp:
            image.save(fp, "PNG", optimize=True)
            fp.flush()
            os.fsync(fp.fileno())
        tmp_path.replace(output_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def render_code_snapshot(
    code: str,
    output_path: Path,
    theme_name: str = "cyberpunk",
    language: str = "auto",
    title: str = "",
    font_size: int = 18,
    show_line_numbers: bool = True,
    show_window_frame: bool = True,
    glow_effect: bool = True,
) -> Tuple[int, int]:
    """Render code into a neon snapshot image using Pillow."""
    theme_key = (theme_name or DEFAULT_THEME).lower()
    if theme_key not in THEMES:
        raise ToolError(
            f"Unknown theme '{theme_name}'.",
            EXIT_INVALID_INPUT,
            {"available_themes": sorted(THEMES)},
        )

    if not MIN_FONT_SIZE <= font_size <= MAX_FONT_SIZE:
        raise ToolError(
            f"font_size must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE}.",
            EXIT_INVALID_INPUT,
        )

    if len(code) > MAX_CODE_CHARS:
        raise ToolError(
            f"Code input exceeds the {MAX_CODE_CHARS:,} character limit.",
            EXIT_INVALID_INPUT,
        )

    palette = THEMES[theme_key]
    tokens_by_line = _tokenize_code(code, language)

    if len(tokens_by_line) > MAX_CODE_LINES:
        raise ToolError(
            f"Code input exceeds the {MAX_CODE_LINES:,} line limit.",
            EXIT_INVALID_INPUT,
        )

    font = _load_font(font_size)

    dummy_img = Image.new("RGB", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)
    char_w = max(_safe_text_width(dummy_draw, "M", font), 1)
    bbox = dummy_draw.textbbox((0, 0), "M", font=font)
    line_h = max(bbox[3] - bbox[1] + 8, font_size + 6)

    margin = 50 if glow_effect else 30
    title_h = 44 if show_window_frame else 16
    padding = 24

    raw_lines = code.splitlines() or [""]
    expanded_lines = [line.expandtabs(4) for line in raw_lines]
    max_cols = max((len(line) for line in expanded_lines), default=0)

    line_num_digits = len(str(max(len(tokens_by_line), 1)))
    line_num_w = (
        (line_num_digits + 2) * char_w if show_line_numbers else 0
    )

    # Adaptive layout:
    # The previous sizing model used one vertical column regardless of how many
    # source lines existed. Large files therefore became extremely tall and were
    # visually compressed by viewers. Keep the same content/order, but split long
    # files into sequential visual columns so the snapshot remains readable.
    measured_max_width = max(
        (_safe_text_width(dummy_draw, line, font) for line in expanded_lines),
        default=0,
    )
    natural_code_width = max(
        max_cols * char_w + line_num_w,
        measured_max_width + line_num_w,
        400,
    )

    line_count = max(len(tokens_by_line), 1)
    max_column_lines = 220
    max_columns = 6

    column_count = 1
    if line_count > max_column_lines:
        column_count = min(
            max_columns,
            max(1, (line_count + max_column_lines - 1) // max_column_lines),
        )

    # Recompute the number of columns if the first pass would still create an
    # excessively tall image. This is a sizing-only decision; no source lines
    # are removed or reordered.
    target_max_height = 5200
    available_code_height = max(
        target_max_height - title_h - padding * 2 - margin * 2,
        line_h,
    )
    lines_per_column_for_height = max(
        1,
        available_code_height // line_h,
    )
    required_columns = max(
        1,
        (line_count + lines_per_column_for_height - 1)
        // lines_per_column_for_height,
    )
    column_count = min(max_columns, max(column_count, required_columns))

    # If the file still cannot fit the preferred height with the column limit,
    # let the existing absolute MAX_IMAGE_HEIGHT guard handle the rare case.
    lines_per_column = (line_count + column_count - 1) // column_count

    # Estimate each column independently so unusually long lines in one section
    # do not force every other column to use unnecessary width.
    column_widths: List[int] = []
    for col_idx in range(column_count):
        start = col_idx * lines_per_column
        end = min(start + lines_per_column, line_count)
        col_lines = expanded_lines[start:end] or [""]
        col_max_cols = max((len(line) for line in col_lines), default=0)
        col_measured_width = max(
            (_safe_text_width(dummy_draw, line, font) for line in col_lines),
            default=0,
        )
        col_width = max(
            col_max_cols * char_w + line_num_w,
            col_measured_width + line_num_w,
            400,
        )
        column_widths.append(col_width)

    column_gap = max(28, padding)
    code_width = sum(column_widths) + column_gap * max(column_count - 1, 0)
    code_height = max(
        min(lines_per_column, line_count) * line_h,
        line_h,
    )

    win_w = code_width + padding * 2
    win_h = code_height + title_h + padding * 2
    total_w = win_w + margin * 2
    total_h = win_h + margin * 2

    if total_w > MAX_IMAGE_WIDTH or total_h > MAX_IMAGE_HEIGHT:
        raise ToolError(
            f"Calculated image dimensions {total_w}x{total_h} exceed the "
            f"safe limit of {MAX_IMAGE_WIDTH}x{MAX_IMAGE_HEIGHT}.",
            EXIT_INVALID_INPUT,
        )

    canvas = Image.new("RGBA", (total_w, total_h), palette["bg"] + (255,))

    if glow_effect:
        glow_canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow_canvas)
        glow_box = [
            margin - 4,
            margin - 4,
            margin + win_w + 4,
            margin + win_h + 4,
        ]
        glow_draw.rounded_rectangle(
            glow_box,
            radius=16,
            fill=palette["glow"],
        )
        glow_canvas = glow_canvas.filter(ImageFilter.GaussianBlur(radius=20))
        canvas.alpha_composite(glow_canvas)

    win_box = [margin, margin, margin + win_w, margin + win_h]
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle(
        win_box,
        radius=12,
        fill=palette["window_bg"] + (255,),
        outline=palette["border"],
        width=2,
    )

    if show_window_frame:
        btn_y = margin + 22
        colors = [(255, 95, 86), (255, 189, 46), (39, 201, 63)]
        for i, col in enumerate(colors):
            btn_x = margin + 24 + i * 20
            draw.ellipse(
                [btn_x - 6, btn_y - 6, btn_x + 6, btn_y + 6],
                fill=col,
            )

        if title:
            title_bbox = draw.textbbox((0, 0), title, font=font)
            title_w = title_bbox[2] - title_bbox[0]
            # Keep a reasonable gap from the control buttons and clip long titles.
            max_title_w = max(win_w - 120, 100)
            title_text = title
            while title_text and title_w > max_title_w:
                title_text = title_text[:-1]
                title_bbox = draw.textbbox((0, 0), title_text + "…", font=font)
                title_w = title_bbox[2] - title_bbox[0]
            if title_text != title:
                title_text += "…"
                title_w = _safe_text_width(draw, title_text, font)

            title_x = margin + max(60, (win_w - title_w) // 2)
            draw.text(
                (title_x, margin + 14),
                title_text,
                fill=palette["title"],
                font=font,
            )

    start_y = margin + title_h + padding

    for col_idx in range(column_count):
        col_start = col_idx * lines_per_column
        col_end = min(col_start + lines_per_column, line_count)
        col_x = (
            margin
            + padding
            + sum(column_widths[:col_idx])
            + column_gap * col_idx
        )
        start_x = col_x + line_num_w

        for local_idx, line_tokens in enumerate(tokens_by_line[col_start:col_end]):
            # Abort promptly if the caller sends SIGINT/SIGTERM during a large render.
            y = start_y + local_idx * line_h

            if show_line_numbers:
                source_idx = col_start + local_idx
                num_str = str(source_idx + 1).rjust(line_num_digits)
                num_x = col_x
                draw.text(
                    (num_x, y),
                    num_str,
                    fill=palette["line_num"],
                    font=font,
                )

            x = start_x
            for text, cat in line_tokens:
                color = palette.get(cat, palette["default"])
                clean_text = text.replace("\t", "    ")
                if clean_text:
                    draw.text((x, y), clean_text, fill=color, font=font)
                    # The bundled monospace font is normally fixed-width, but
                    # measured width prevents layout drift with fallback fonts.
                    x += _safe_text_width(draw, clean_text, font)

    _save_png_atomic(canvas, output_path)
    return total_w, total_h


# ==============================================================================
# SECTION 7: Core Tool Execution
# ==============================================================================

def _read_target(target: str) -> Tuple[str, str, Optional[Path]]:
    """
    Resolve target as a file when it clearly identifies an existing file;
    otherwise treat it as inline code.

    This preserves the original path-or-code behavior while avoiding a
    potentially expensive Path.resolve() on very large inline strings.
    """
    if not isinstance(target, str):
        raise ToolError("Target must be a string.", EXIT_INVALID_INPUT)

    if not target:
        raise ToolError("Target cannot be empty.", EXIT_INVALID_INPUT)
    if "\x00" in target:
        raise ToolError("Target contains an invalid NUL character.", EXIT_INVALID_INPUT)

    looks_like_inline = "\n" in target or "\r" in target or len(target) >= 500

    if not looks_like_inline:
        try:
            candidate = Path(target).expanduser()
            if candidate.exists():
                if not candidate.is_file():
                    raise ToolError(
                        f"Target path is not a file: {candidate}",
                        EXIT_INVALID_INPUT,
                    )
                try:
                    code_text = candidate.read_text(
                        encoding="utf-8",
                        errors="strict",
                    )
                except UnicodeDecodeError as exc:
                    raise ToolError(
                        f"Target file is not valid UTF-8: {exc}",
                        EXIT_INVALID_INPUT,
                    ) from exc
                except PermissionError as exc:
                    raise ToolError(
                        f"Permission denied reading target: {candidate}",
                        EXIT_PERMISSION_DENIED,
                    ) from exc
                except OSError as exc:
                    raise ToolError(
                        f"Failed reading file: {exc}",
                        EXIT_FILE_NOT_FOUND,
                    ) from exc

                return code_text, candidate.name, candidate.resolve()

            # If the argument has an obvious path shape but does not exist,
            # report it instead of silently rendering the path as code.
            if (
                os.path.sep in target
                or target.endswith((".py", ".js", ".ts", ".rs", ".go", ".java"))
            ):
                raise ToolError(
                    f"Target file not found: {target}",
                    EXIT_FILE_NOT_FOUND,
                )
        except ToolError:
            raise
        except OSError:
            pass

    return target, "snapshot.code", None


def _validate_output_path(output: str) -> Path:
    """Validate and normalize the destination while preserving CLI semantics."""
    if not isinstance(output, str):
        raise ToolError("Output path must be a string.", EXIT_INVALID_INPUT)
    if not output.strip():
        raise ToolError("Output path cannot be empty.", EXIT_INVALID_INPUT)

    path = Path(output).expanduser()
    if path.exists() and path.is_dir():
        raise ToolError(
            f"Output path is a directory, not a file: {path}",
            EXIT_INVALID_INPUT,
        )
    if path.suffix and path.suffix.lower() != ".png":
        logging.debug("Output extension '%s' is non-PNG; Pillow will still write PNG.", path.suffix)

    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def execute_tool(
    target: str,
    output: Optional[str] = None,
    theme: str = "cyberpunk",
    language: str = "auto",
    title: Optional[str] = None,
    font_size: Optional[int] = None,
    line_numbers: bool = False,
    window_frame: bool = False,
    glow_effect: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    start_time = time.monotonic()
    shutdown = GracefulShutdown()

    try:
        if verbose:
            logging.basicConfig(
                level=logging.DEBUG,
                format="[DEBUG] %(message)s",
            )
            logging.debug("Target argument length: %d", len(target or ""))

        if shutdown.should_stop():
            raise ToolError("Operation interrupted.", EXIT_INTERRUPTED)

        code_text, default_title, target_path = _read_target(target)

        if not code_text.strip():
            raise ToolError(
                "Provided code target is empty.",
                EXIT_INVALID_INPUT,
            )

        # Normalize platform line endings without changing code content otherwise.
        code_text = code_text.replace("\r\n", "\n").replace("\r", "\n")

        if len(code_text) > MAX_CODE_CHARS:
            raise ToolError(
                f"Code input exceeds the {MAX_CODE_CHARS:,} character limit.",
                EXIT_INVALID_INPUT,
            )

        output_path = _validate_output_path(output or DEFAULT_OUTPUT)

        theme_name = (theme or DEFAULT_THEME).lower().strip()
        if theme_name not in THEMES:
            raise ToolError(
                f"Unknown theme '{theme}'.",
                EXIT_INVALID_INPUT,
                {"available_themes": sorted(THEMES)},
            )

        language_name = _normalize_language(language)
        if len(language_name) > 64:
            raise ToolError(
                "language name is too long.",
                EXIT_INVALID_INPUT,
            )
        try:
            f_size = int(font_size or DEFAULT_FONT_SIZE)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                "font_size must be an integer.",
                EXIT_INVALID_INPUT,
            ) from exc

        if not MIN_FONT_SIZE <= f_size <= MAX_FONT_SIZE:
            raise ToolError(
                f"font_size must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE}.",
                EXIT_INVALID_INPUT,
            )

        display_title = str(
            title or default_title or language_name or "snapshot.code"
        ).strip()
        if len(display_title) > 512:
            display_title = display_title[:512]
        if not display_title:
            display_title = "snapshot.code"

        cache = ToolCache()
        # Output path is intentionally excluded: identical renders can be
        # recognized even when the caller chooses a different destination.
        cache_key = json.dumps(
            {
                "code_sha256": hashlib.sha256(
                    code_text.encode("utf-8")
                ).hexdigest(),
                "theme": theme_name,
                "language": language_name,
                "title": display_title,
                "font_size": f_size,
                "line_numbers": bool(line_numbers),
                "window_frame": bool(window_frame),
                "glow_effect": bool(glow_effect),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        if use_cache:
            cached_result = cache.get(cache_key)
            if isinstance(cached_result, dict) and cached_result.get("success") is True:
                cached_result = dict(cached_result)
                cached_result["output_path"] = str(output_path)

                # Cache is metadata, not the image itself. If the requested
                # output does not exist, rerender it rather than claiming hit.
                cached_output = Path(output_path)
                if cached_output.is_file() and cached_output.stat().st_size > 0:
                    cached_result["cached"] = True
                    cached_result["duration_ms"] = round(
                        (time.monotonic() - start_time) * 1000,
                        2,
                    )
                    cached_result["cache_hit"] = True
                    return cached_result

        if shutdown.should_stop():
            raise ToolError("Operation interrupted.", EXIT_INTERRUPTED)

        width, height = render_code_snapshot(
            code=code_text,
            output_path=output_path,
            theme_name=theme_name,
            language=language_name,
            title=display_title,
            font_size=f_size,
            show_line_numbers=bool(line_numbers),
            show_window_frame=bool(window_frame),
            glow_effect=bool(glow_effect),
        )

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        result = {
            "success": True,
            "output_path": str(output_path),
            "renderer_version": __version__,
            "theme": theme_name,
            "language": language_name,
            "line_count": len(code_text.splitlines()),
            "width": width,
            "height": height,
            "layout_columns": max(1, min(6, (len(code_text.splitlines()) + 219) // 220)),
            "context": get_execution_context(),
            "cached": False,
            "cache_hit": False,
            "duration_ms": duration_ms,
            "exit_code": EXIT_SUCCESS,
        }

        if target_path is not None:
            result["target_path"] = str(target_path)
        if HAS_PYGMENTS:
            result["pygments"] = True
        else:
            result["pygments"] = False

        if use_cache:
            try:
                cache.set(cache_key, result)
            except Exception as cache_exc:
                logging.debug("Cache write skipped: %s", cache_exc)

        return result

    except ToolError as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        result = exc.to_dict()
        result["duration_ms"] = duration_ms
        return result
    except PermissionError as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        return {
            "success": False,
            "error": f"Permission denied: {exc}",
            "exit_code": EXIT_PERMISSION_DENIED,
            "duration_ms": duration_ms,
        }
    except KeyboardInterrupt:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        return {
            "success": False,
            "error": "Operation interrupted.",
            "exit_code": EXIT_INTERRUPTED,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        logging.exception("Snapshot rendering error")
        return {
            "success": False,
            "error": f"Snapshot rendering error: {exc}",
            "exit_code": EXIT_ERROR,
            "duration_ms": duration_ms,
        }
    finally:
        shutdown.restore()


# ==============================================================================
# SECTION 8: Output Routing & Main Entrypoint
# ==============================================================================

def write_llm_output(data: dict[str, Any]) -> None:
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = (
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            cls=ToolJSONEncoder,
        )
        + "\n"
    )

    if out_path in {"/dev/stdout", "/dev/fd/1", "-"}:
        sys.stdout.write(json_payload)
        sys.stdout.flush()
        return

    try:
        destination = Path(out_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Preserve the original append behavior for compatibility with
        # LLM_OUTPUT files that collect multiple tool invocations.
        with destination.open("a", encoding="utf-8") as fp:
            fp.write(json_payload)
            fp.flush()
    except OSError as err:
        sys.stderr.write(
            f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n"
        )
        sys.stdout.write(json_payload)
        sys.stdout.flush()


def run(
    target: str,
    output: Optional[str] = "snapshot.png",
    theme: Literal["cyberpunk", "matrix", "synthwave", "tokyo-night"] = "cyberpunk",
    language: str = "auto",
    title: Optional[str] = None,
    font_size: Optional[int] = 18,
    line_numbers: bool = False,
    window_frame: bool = False,
    glow_effect: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> None:
    """Execute code snapshot creation with specified parameters."""
    result = execute_tool(
        target=target,
        output=output,
        theme=theme,
        language=language,
        title=title,
        font_size=font_size,
        line_numbers=line_numbers,
        window_frame=window_frame,
        glow_effect=glow_effect,
        use_cache=use_cache,
        no_color=no_color,
        verbose=verbose,
    )

    print_human_readable_ui(result, no_color=no_color)
    write_llm_output(result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_snapshot.py",
        description=f"AIChat Neon Code Snapshot Tool v{__version__}",
    )

    parser.add_argument(
        "--target",
        "-t",
        required=True,
        metavar="PATH_OR_CODE",
        help="Target file path or inline code text",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUTPUT,
        metavar="PATH",
        help=f"Destination PNG output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--theme",
        choices=sorted(THEMES),
        default=DEFAULT_THEME,
        help=f"Neon theme selection (default: {DEFAULT_THEME})",
    )
    parser.add_argument(
        "--language",
        "-l",
        default=DEFAULT_LANGUAGE,
        help="Programming language (default: auto)",
    )
    parser.add_argument(
        "--title",
        help="Custom title bar text",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=DEFAULT_FONT_SIZE,
        help=f"Font size in pixels (default: {DEFAULT_FONT_SIZE})",
    )
    parser.add_argument(
        "--line-numbers",
        action="store_true",
        default=False,
        help="Enable line numbers",
    )
    parser.add_argument(
        "--window-frame",
        action="store_true",
        default=False,
        help="Enable window header frame with control dots",
    )
    parser.add_argument(
        "--glow-effect",
        action="store_true",
        default=False,
        help="Add outer neon glow border effect",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=False,
        help="Enable result caching",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable ANSI colors",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable debug output",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    res = execute_tool(
        target=args.target,
        output=args.output,
        theme=args.theme,
        language=args.language,
        title=args.title,
        font_size=args.font_size,
        line_numbers=args.line_numbers,
        window_frame=args.window_frame,
        glow_effect=args.glow_effect,
        use_cache=args.use_cache,
        no_color=args.no_color,
        verbose=args.verbose,
    )

    print_human_readable_ui(res, no_color=args.no_color)
    write_llm_output(res)
    sys.exit(res.get("exit_code", EXIT_SUCCESS))
