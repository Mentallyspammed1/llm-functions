#!/usr/bin/env python3
# ==============================================================================
# video_master_tool.py — Pyrmethus AIChat Master Video Intelligence Tool v4.3.0
# Native FFmpeg Thumbnail Extraction · Montage Grids · Scene Mapping (Zero-CV)
#
# @describe Master AI tool for native FFmpeg thumbnail extraction, montage grid building, scene detection, stream extraction, and subtitle-backed timestamp querying without OpenCV/PySceneDetect dependencies.
#
# @meta require-tools aichat
#
# @option --target! <PATH>               Target video file path or URL (required)
# @option --query <TEXT>                 Query text matched against embedded/sidecar subtitles
# @option --output-dir <PATH>            Directory where thumbnails and keyframes are written (default: thumbnails)
# @option --interval <NUM>               Seconds between each captured frame for thumbnail generation (default: 10.0)
# @option --width <NUM>                  Thumbnail width in pixels; height scales automatically (default: 320)
# @option --format <FMT>                 Output image format: png, jpg, webp (default: png)
# @option --start <TIME>                 Start time as HH:MM:SS, seconds, or NN% (default: 00:00:00)
# @option --end <TIME>                   End time as HH:MM:SS, seconds, or NN% (default: 00:00:00)
# @option --max-frames <NUM>             Maximum number of extracted thumbnail frames (default: 15)
# @option --timestamps <LIST>            Comma-separated timestamps (HH:MM:SS, seconds, or NN%) to capture
# @option --percentages <LIST>           Comma-separated percentages (0-100) of video duration to capture
# @option --montage <GRID>               Generate image grid montage, e.g. 2x3 or 3x3
# @option --montage-bg <COLOR>           Montage background/padding color (default: white)
# @option --tile-spacing <NUM>           Pixel padding between montage tiles (default: 2)
# @option --quality <NUM>                JPEG/WebP quality (1-100), mapped to PNG compression for png (default: 80)
# @option --font <PATH>                  TTF font file used for the timestamp overlay
# @option --font-size <NUM>              Timestamp overlay font size (default: 14)
# @option --font-color <COLOR>           Timestamp overlay font color (default: white)
# @option --box-color <COLOR>            Timestamp overlay box color (default: black)
# @option --box-opacity <NUM>            Timestamp overlay box opacity 0.0-1.0 (default: 0.5)
# @option --position <POS>               Overlay position: tl, tr, bl, br, center (default: bl)
# @option --scene-threshold <NUM>        Scene-change sensitivity 0.0-1.0 for --scenes (default: 0.4)
# @option --jobs <NUM>                   Parallel ffmpeg extraction workers (default: 4)
# @option --timeout <NUM>                Per-subprocess timeout in seconds (default: 180)
# @option --manifest <PATH>              Write a JSON manifest of all captures to this path
# @flag   --scenes                       Pick capture points from detected scene changes
# @flag   --query-capture                Capture frames at subtitle cues matching --query
# @flag   --keyframes                    Snap capture points to the nearest real keyframe (I-frame)
# @flag   --force-keyframes              Frame-accurate seeking (slower, decodes up to each timestamp)
# @flag   --add-timestamps               Draw the capture time overlay on each generated thumbnail
# @flag   --strip-metadata               Strip EXIF metadata from output thumbnails
# @flag   --only-montage                 Clean up intermediate frames and keep only the montage grid
# @flag   --dry-run                      Resolve and plan capture points without writing images
# @flag   --progress                     Show extraction progress on stderr
# @flag   --use-cache                    Enable result caching for expensive video processing operations
# @flag   --clear-cache                  Clear tool cache directory and exit
# @flag   --schema                       Print JSON Tool Schema for LLM registration and exit
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug log output
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# @env VIDEO_TOOL_SANDBOX                Extra colon-separated roots allowed for file I/O
# ==============================================================================

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

__version__ = "4.3.0"
__all__ = [
    "run",
    "execute_tool",
    "main",
    "validate_inputs",
    "generate_tool_schema",
    "ToolCache",
    "ToolError",
    "GracefulShutdown",
    "__version__",
]

# ==============================================================================
# SECTION 1: Exit Codes, Validation Rules & Exception Models
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_TIMEOUT = 124
EXIT_PERMISSION_DENIED = 126
EXIT_INVALID_INPUT = 127
EXIT_INTERRUPTED = 130

TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")
COMPACT_TIME_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$", re.I)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB safety ceiling
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ToolError(Exception):
    """Structured exception model for tool operations."""

    def __init__(
        self,
        message: str,
        exit_code: int = EXIT_ERROR,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": False,
            "error": self.message,
            "exit_code": self.exit_code,
            **self.details,
        }


def validate_inputs(
    target: Optional[str],
    width: int,
    interval: float,
    max_frames: int,
    quality: int = 80,
    jobs: int = 4,
) -> Optional[Dict[str, Any]]:
    """Validate core input parameters strictly before execution."""

    def bad(msg: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error": msg,
            "exit_code": EXIT_INVALID_INPUT,
            "duration_ms": 0.0,
            "generated_thumbnails": [],
        }

    if not target or not str(target).strip():
        return bad("Target video path or URL is required.")
    if width < 16 or width > 7680:
        return bad(f"Invalid width '{width}'. Width must be between 16 and 7680 pixels.")
    if interval <= 0:
        return bad(f"Invalid interval '{interval}'. Interval must be > 0 seconds.")
    if max_frames < 1 or max_frames > 500:
        return bad(f"Invalid max_frames '{max_frames}'. Must be between 1 and 500.")
    if not 1 <= quality <= 100:
        return bad(f"Invalid quality '{quality}'. Must be between 1 and 100.")
    if not 1 <= jobs <= 32:
        return bad(f"Invalid jobs '{jobs}'. Must be between 1 and 32.")
    return None


class ToolJSONEncoder(json.JSONEncoder):
    """Resilient JSON encoder handling Path, Enum, datetime, timedelta, and dataclasses."""

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
            if isinstance(obj, set):
                return sorted(obj, key=repr)
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)
        except Exception:
            pass
        return repr(obj)


# ==============================================================================
# SECTION 2: Terminal Colors & UI Display Helpers
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

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_VERBOSE = False


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


def _log(msg: str) -> None:
    if _VERBOSE:
        _cprint(f"{DIM}[debug] {msg}{RESET}")


def _ellipsize(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def print_human_readable_ui(data: Dict[str, Any], no_color: bool = False) -> None:
    """Render human-friendly box UI to stderr."""
    if not _is_tty(no_color=no_color):
        return

    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "SUCCESS" if success else "FAILED"

    box_w = 68
    border = "─" * box_w
    bar = f"{NEON_PURPLE}│{RESET}"

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}", no_color=no_color)
    _cprint(
        f"{bar} {NEON_PINK}🎬 [PURE-FFMPEG VIDEO TOOL v{__version__}]{RESET} "
        f"{status_color}{BOLD}{status_symbol} {status_text}{RESET}",
        no_color=no_color,
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
    _cprint(f"{bar} {NEON_CYAN}Target Video:{RESET} {_ellipsize(data.get('target', 'N/A'), 50)}", no_color=no_color)
    if data.get("media_duration"):
        _cprint(f"{bar} {NEON_CYAN}Media Length:{RESET} {_format_timestamp(data['media_duration'])}", no_color=no_color)
    if data.get("query"):
        _cprint(
            f"{bar} {NEON_CYAN}Query Match:{RESET}  {NEON_YELLOW}\"{_ellipsize(data['query'], 45)}\"{RESET} "
            f"{DIM}({data.get('query_source', 'n/a')}){RESET}",
            no_color=no_color,
        )
    _cprint(
        f"{bar} {NEON_CYAN}Thumbnails:{RESET}   {NEON_GREEN}{len(data.get('generated_thumbnails', []))} files{RESET}",
        no_color=no_color,
    )
    if data.get("montage_path"):
        _cprint(f"{bar} {NEON_CYAN}Montage:{RESET}      {data['montage_path']}", no_color=no_color)
    _cprint(f"{bar} {NEON_CYAN}Cached:{RESET}       {NEON_YELLOW}{data.get('cached', False)}{RESET}", no_color=no_color)
    _cprint(f"{bar} {NEON_CYAN}Duration:{RESET}     {DIM}{data.get('duration_ms', 0)}ms{RESET}", no_color=no_color)

    if not success and "error" in data:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        for line in _wrap(str(data["error"]), box_w - 12):
            _cprint(f"{bar} {NEON_RED}Error:{RESET}    {line}", no_color=no_color)
        if data.get("hint"):
            for line in _wrap(str(data["hint"]), box_w - 12):
                _cprint(f"{bar} {NEON_YELLOW}Hint:{RESET}     {line}", no_color=no_color)

    matches = data.get("matching_segments", [])
    if matches:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{bar} {BOLD}Relevant Query Timestamps:{RESET}", no_color=no_color)
        for m in matches[:5]:
            _cprint(
                f"{bar}   {NEON_CYAN}⏱ [{m['start_time']} ➔ {m['end_time']}]{RESET} "
                f"Score: {NEON_GREEN}{m['relevance_score']}{RESET} {DIM}{_ellipsize(m.get('description', ''), 28)}{RESET}",
                no_color=no_color,
            )

    thumbs = data.get("generated_thumbnails", [])
    if thumbs:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}", no_color=no_color)
        _cprint(f"{bar} {BOLD}Generated Output Files (Sample):{RESET}", no_color=no_color)
        for th in thumbs[:5]:
            _cprint(f"{bar}   {NEON_CYAN}›{RESET} {_ellipsize(th, 60)}", no_color=no_color)
        if len(thumbs) > 5:
            _cprint(f"{bar}   {DIM}... and {len(thumbs) - 5} more files{RESET}", no_color=no_color)

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}", no_color=no_color)


# ==============================================================================
# SECTION 3: Cache Management & Schema Definition
# ==============================================================================

class ToolCache:
    """Safe, JSON file-backed caching utility with TTL support."""

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self.cache_dir = cache_dir or (Path.home() / ".cache" / "aichat_ffmpeg_video")
        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _hash_key(self, key_str: str) -> str:
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()

    def get(self, key_str: str, ttl_seconds: int = 86400) -> Optional[Dict[str, Any]]:
        cache_file = self.cache_dir / f"{self._hash_key(key_str)}.json"
        if not cache_file.exists():
            return None
        try:
            if time.time() - cache_file.stat().st_mtime > ttl_seconds:
                cache_file.unlink(missing_ok=True)
                return None
            with open(cache_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                return None
            files = data.get("generated_thumbnails", [])
            if files and not all(Path(f).exists() for f in files):
                _log("cache entry discarded: referenced files missing")
                cache_file.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            return None

    def set(self, key_str: str, value: Any) -> None:
        cache_file = self.cache_dir / f"{self._hash_key(key_str)}.json"
        tmp_file = cache_file.with_suffix(".tmp")
        try:
            payload = json.dumps(value, cls=ToolJSONEncoder, ensure_ascii=False)
            with open(tmp_file, "w", encoding="utf-8") as fp:
                fp.write(payload)
            tmp_file.replace(cache_file)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_file.unlink(missing_ok=True)


def invalidate_cache(cache: Optional[ToolCache] = None) -> int:
    cache_obj = cache or ToolCache()
    removed = 0
    if not cache_obj.cache_dir.exists():
        return removed
    for file in cache_obj.cache_dir.glob("*.json"):
        try:
            file.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


class GracefulShutdown:
    """Context manager for intercepting termination signals cleanly."""

    def __init__(self) -> None:
        self.interrupted = False
        self._old_sigint = None
        self._old_sigterm = None

    def __enter__(self) -> "GracefulShutdown":
        self.interrupted = False
        try:
            self._old_sigint = signal.signal(signal.SIGINT, self._handle_signal)
            self._old_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
        except (ValueError, AttributeError):
            pass
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            if self._old_sigint is not None:
                signal.signal(signal.SIGINT, self._old_sigint)
            if self._old_sigterm is not None:
                signal.signal(signal.SIGTERM, self._old_sigterm)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.interrupted = True
        _cprint(f"\n{NEON_YELLOW}Signal {signum} received — stopping operations cleanly…{RESET}")

    def should_stop(self) -> bool:
        return getattr(self, "interrupted", False)


def generate_tool_schema() -> Dict[str, Any]:
    return {
        "name": "video_master_tool",
        "description": (
            "Pure-FFmpeg video tool: extract thumbnails at intervals/percentages/explicit "
            "timestamps, snap to keyframes, detect scene changes, build montage grids, and "
            "locate time segments via embedded or sidecar subtitles."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Local video file path or direct/resolvable media URL (required)"},
                "query": {"type": "string", "description": "Text matched against embedded/sidecar subtitles"},
                "output_dir": {"type": "string", "description": "Directory for output thumbnails (default: thumbnails)"},
                "interval": {"type": "number", "description": "Seconds between captured frames (default: 10.0)"},
                "width": {"type": "integer", "description": "Thumbnail width in pixels (default: 320)"},
                "format": {"type": "string", "enum": ["png", "jpg", "webp"], "description": "Output image format"},
                "start": {"type": "string", "description": "Start time (HH:MM:SS, seconds, or NN%)"},
                "end": {"type": "string", "description": "End time (HH:MM:SS, seconds, or NN%)"},
                "max_frames": {"type": "integer", "description": "Maximum frames to extract (default: 15)"},
                "timestamps": {"type": "string", "description": "Comma-separated explicit timestamps"},
                "percentages": {"type": "string", "description": "Comma-separated percentages 0-100"},
                "montage": {"type": "string", "description": "Montage grid layout, e.g. 2x3 or 3x3"},
                "scenes": {"type": "boolean", "description": "Choose capture points from detected scene changes"},
                "scene_threshold": {"type": "number", "description": "Scene sensitivity 0.0-1.0 (default: 0.4)"},
                "keyframes": {"type": "boolean", "description": "Snap capture points to nearest keyframe"},
                "force_keyframes": {"type": "boolean", "description": "Frame-accurate (slower) seeking"},
                "add_timestamps": {"type": "boolean", "description": "Burn capture time onto each thumbnail"},
                "only_montage": {"type": "boolean", "description": "Delete tiles, keep only the montage"},
                "dry_run": {"type": "boolean", "description": "Plan capture points without writing files"},
            },
            "required": ["target"],
        },
    }


# ==============================================================================
# SECTION 4: Native FFmpeg Helpers & Stream Resolvers
# ==============================================================================

def _sandbox_roots() -> List[Path]:
    roots: List[Path] = []
    candidates = [
        Path.home(),
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path.cwd(),
    ]
    # Termux Android support
    termux_prefix = os.environ.get("PREFIX")
    if termux_prefix:
        p = Path(termux_prefix)
        candidates.extend([p, p.parent, p.parent / "tmp", p.parent / "home"])
    termux_data = Path("/data/data/com.termux/files")
    if termux_data.exists():
        candidates.append(termux_data)

    for cand in candidates:
        with contextlib.suppress(OSError):
            roots.append(cand.resolve())

    for extra in filter(None, os.environ.get("VIDEO_TOOL_SANDBOX", "").split(os.pathsep)):
        with contextlib.suppress(OSError):
            roots.append(Path(extra).expanduser().resolve())
    return roots


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        return child == parent or parent in child.parents
    except Exception:
        return False


def _validate_sandbox(path: Path) -> bool:
    """Component-aware sandbox boundary check."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(_is_relative_to(resolved, root) for root in _sandbox_roots())


def _run(
    cmd: Sequence[str],
    timeout: float = 180.0,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess, surfacing binary stderr details on ToolError."""
    name = Path(cmd[0]).name
    _log(f"exec: {name} {' '.join(str(c) for c in cmd[1:])[:400]}")
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"'{name}' timed out after {timeout:.0f}s.", EXIT_TIMEOUT)
    except FileNotFoundError:
        raise ToolError(f"Required system binary '{name}' not found on PATH.", EXIT_ERROR)
    except PermissionError:
        raise ToolError(f"Permission denied executing '{name}'.", EXIT_PERMISSION_DENIED)

    if check and proc.returncode != 0:
        tail = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()][-3:]
        detail = " | ".join(tail) if tail else "no stderr output"
        raise ToolError(f"{name} failed (exit {proc.returncode}): {detail}", EXIT_ERROR)
    return proc


def _which_or_die(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolError(
            f"Required system binary '{name}' not found on PATH. Install FFmpeg "
            f"(Debian/Ubuntu: `apt install ffmpeg`, Termux: `pkg install ffmpeg`).",
            EXIT_ERROR,
        )
    return path


def _parse_time_to_seconds(s: str, duration: float = 0.0) -> float:
    s = (s or "").strip()
    if not s or s == "00:00:00":
        return 0.0
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
        except ValueError:
            raise ValueError(f"invalid percentage: {s}")
        if not 0.0 <= pct <= 100.0:
            raise ValueError(f"percentage out of range 0-100: {s}")
        if duration <= 0:
            raise ValueError("percentage requires a known media duration")
        return duration * pct / 100.0
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    m = TIME_RE.match(s)
    if m:
        h, mi, sec, frac = m.groups()
        f = float(f"0.{frac}") if frac else 0.0
        return int(h or 0) * 3600 + int(mi) * 60 + int(sec) + f
    m = COMPACT_TIME_RE.match(s)
    if m and any(m.groups()):
        h, mi, sec = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)
    raise ValueError(f"invalid time format: {s!r} (use HH:MM:SS, seconds, 1h2m3s, or NN%)")


def _is_url(s: str) -> bool:
    p = urlparse(str(s).strip())
    return p.scheme in ("http", "https", "ftp", "rtsp", "rtmp") and bool(p.netloc)


def _sanitize_stem(name: str) -> str:
    stem = Path(urlparse(name).path).stem if _is_url(name) else Path(name).stem
    stem = re.sub(r"[^\w.\-]+", "_", stem or "video").strip("._-")
    return (stem or "video")[:120]


def _format_timestamp(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _looks_like_media(head: bytes) -> bool:
    """Sniff initial bytes to avoid feeding HTML web pages to FFmpeg/FFprobe."""
    if len(head) < 8:
        return False
    stripped = head.lstrip()[:512].lower()
    if stripped.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"{", b"<!--")):
        return False
    if head[4:8] == b"ftyp":                        # MP4 / MOV / M4V
        return True
    if head[:4] == b"\x1a\x45\xdf\xa3":             # Matroska / WebM
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True
    if head[:3] == b"FLV" or head[:4] == b"OggS":
        return True
    if head[:7] == b"#EXTM3U":                      # HLS playlist
        return True
    if head[0] == 0x47:                             # MPEG-TS sync byte
        return True
    if head[:3] in (b"\x00\x00\x01", b"ID3"):
        return True
    return True


def _probe_json(
    path: str,
    ffprobe: str,
    timeout: float,
    user_agent: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cmd = [ffprobe, "-v", "error"]
    if _is_url(path):
        cmd.extend(["-user_agent", user_agent or DEFAULT_UA])
        if headers:
            h_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd.extend(["-headers", h_lines])

    cmd.extend([
        "-show_entries", "format=duration,format_name,size",
        "-show_entries", "stream=index,codec_type,codec_name,width,height,duration,nb_frames",
        "-of", "json", path,
    ])
    proc = _run(cmd, timeout=timeout)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise ToolError("ffprobe returned unparseable JSON metadata.", EXIT_ERROR)


def _duration_from_probe(info: Dict[str, Any]) -> float:
    with contextlib.suppress(Exception):
        d = float(info.get("format", {}).get("duration"))
        if d > 0:
            return d
    for st in info.get("streams", []):
        with contextlib.suppress(Exception):
            d = float(st.get("duration"))
            if d > 0:
                return d
    return 0.0


def _has_video_stream(info: Dict[str, Any]) -> bool:
    return any(s.get("codec_type") == "video" for s in info.get("streams", []))


def _resolve_stream_target(
    target: str,
    timeout: float = 20.0,
) -> Tuple[str, Optional[float], Dict[str, str]]:
    """
    Resolve remote targets into playable direct media stream URLs.
    Supports yt-dlp resolution and native zero-dependency HTML5 regex scraping.
    """
    if not _is_url(target):
        return target, None, {}

    parsed = urlparse(target)
    path_lower = parsed.path.lower()
    # Direct media stream suffixes
    if path_lower.endswith((".mp4", ".m3u8", ".webm", ".mkv", ".mov", ".ts", ".flv", ".mp3", ".m4a")):
        return target, None, {"User-Agent": DEFAULT_UA, "Referer": target}

    # 1. Attempt extraction via yt-dlp if installed on the host/Termux
    yt_dlp = shutil.which("yt-dlp")
    if yt_dlp:
        try:
            res = _run(
                [
                    yt_dlp, "--no-warnings", "--no-playlist",
                    "--dump-single-json", "--skip-download", target,
                ],
                timeout=timeout,
            )
            data = json.loads(res.stdout)
            media_url = data.get("url")
            duration = data.get("duration")
            headers = data.get("http_headers") or {"User-Agent": DEFAULT_UA, "Referer": target}
            if media_url:
                _log(f"resolved target stream via yt-dlp: {media_url[:80]}...")
                return media_url, float(duration) if duration else None, headers
        except Exception as e:
            _log(f"yt-dlp stream resolution failed or skipped: {e}")

    # 2. Native HTML scraping fallback for HTML5 video platforms
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": target,
    }
    try:
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            if any(t in content_type for t in ["video/", "mpegurl", "octet-stream"]):
                return target, None, headers

            raw_bytes = resp.read(512 * 1024)  # First 512KB is typically enough for scripts/meta
            html = raw_bytes.decode("utf-8", errors="ignore")

        # Patterns for embedded video links
        patterns = [
            r"html5player\.setVideoUrlHigh\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            r"html5player\.setVideoHLS\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            r"html5player\.setVideoUrlLow\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            r"html5player\.setVideoUrl\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            r"<source[^>]+src\s*=\s*['\"]([^'\"]+\.(?:mp4|m3u8|webm)[^'\"]*)['\"]",
            r"<meta\s+property=['\"](?:og:video(?::url)?|twitter:player:stream)['\"]\s+content=['\"]([^'\"]+)['\"]",
        ]
        stream_url = None
        for pat in patterns:
            m = re.search(pat, html, re.I)
            if m and m.group(1):
                stream_url = m.group(1)
                break

        dur = None
        m_dur = re.search(r"setVideoDuration\s*\(\s*(\d+(?:\.\d+)?)\s*\)", html)
        if m_dur:
            dur = float(m_dur.group(1))
        if not dur:
            m_og_dur = re.search(r"<meta\s+property=['\"](?:video:duration|og:duration)['\"]\s+content=['\"](\d+(?:\.\d+)?)['\"]", html, re.I)
            if m_og_dur:
                dur = float(m_og_dur.group(1))

        if stream_url:
            _log(f"resolved target stream via native regex scraper: {stream_url[:80]}...")
            return stream_url, dur, headers

    except Exception as e:
        _log(f"native stream scraper encountered error: {e}")

    return target, None, headers


def _download_url(url: str, temp_dir: Path, headers: Dict[str, str], timeout: float = 30.0) -> Path:
    """Download a direct media URL safely with size ceiling and container sniff."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": DEFAULT_UA})
    target = temp_dir / "downloaded_video"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype.startswith(("text/", "application/xhtml")) or ctype in ("application/json", "application/xml"):
                raise ToolError(
                    f"Target URL returned '{ctype}' — web markup/JSON was returned instead of a media stream.",
                    EXIT_INVALID_INPUT,
                    {"hint": "Ensure the target points to a direct video URL (.mp4/.mkv/.m3u8) or install yt-dlp."},
                )
            clen = response.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > MAX_DOWNLOAD_BYTES:
                raise ToolError(
                    f"Remote media file is {int(clen) / 1e9:.1f} GB, exceeding safety cap of {MAX_DOWNLOAD_BYTES / 1e9:.1f} GB.",
                    EXIT_INVALID_INPUT,
                )

            first = response.read(65536)
            if not first:
                raise ToolError("Remote URL returned an empty response body.", EXIT_INVALID_INPUT)
            if not _looks_like_media(first):
                raise ToolError(
                    "Downloaded stream header is HTML/markup, not a valid media container.",
                    EXIT_INVALID_INPUT,
                    {"hint": "Web page URL provided. Install yt-dlp on your system for automated web stream extraction."},
                )

            written = len(first)
            with open(target, "wb") as out_file:
                out_file.write(first)
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise ToolError("Download exceeded the maximum configured file cap.", EXIT_INVALID_INPUT)
                    out_file.write(chunk)
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} fetching target URL: {e.reason}", EXIT_ERROR)
    except urllib.error.URLError as e:
        raise ToolError(f"Network error fetching target URL: {e.reason}", EXIT_ERROR)
    except TimeoutError:
        raise ToolError(f"Timed out after {timeout:.0f}s fetching target URL.", EXIT_TIMEOUT)
    return target


def _parse_montage(montage: str) -> Optional[Tuple[int, int]]:
    m = (montage or "").strip().lower()
    if not m:
        return None
    if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", m):
        raise ValueError(f"--montage must be NxM (e.g. 2x3), got: {montage!r}")
    cols, rows = (int(v) for v in m.split("x"))
    if cols * rows > 100:
        raise ValueError("--montage grid cannot exceed 100 tiles.")
    return cols, rows


def _esc_drawtext(text: str) -> str:
    """Escape text for the FFmpeg drawtext video filter."""
    out = text.replace("\\", r"\\\\")
    out = out.replace(":", r"\:").replace("'", r"\'")
    out = out.replace("%", r"\%").replace(",", r"\,")
    out = out.replace("[", r"\[").replace("]", r"\]")
    return out


_POSITIONS = {
    "tl": "x={m}:y={m}",
    "tr": "x=w-tw-{m}:y={m}",
    "bl": "x={m}:y=h-th-{m}",
    "br": "x=w-tw-{m}:y=h-th-{m}",
    "center": "x=(w-tw)/2:y=(h-th)/2",
}


def _drawtext_filter(
    label: str,
    position: str,
    font: Optional[str],
    font_size: int,
    font_color: str,
    box_color: str,
    box_opacity: float,
) -> str:
    margin = max(2, font_size // 2)
    coords = _POSITIONS.get((position or "bl").lower(), _POSITIONS["bl"]).format(m=margin)
    opacity = min(1.0, max(0.0, float(box_opacity)))
    parts = [f"text='{_esc_drawtext(label)}'"]
    if font:
        parts.append(f"fontfile='{_esc_drawtext(str(font))}'")
    parts += [
        f"fontsize={max(6, int(font_size))}",
        f"fontcolor={font_color}",
        "box=1",
        f"boxcolor={box_color}@{opacity:.2f}",
        "boxborderw=4",
        coords,
    ]
    return "drawtext=" + ":".join(parts)


def _encoder_args(fmt: str, quality: int, strip_metadata: bool) -> List[str]:
    args: List[str] = []
    if fmt == "jpg":
        args += ["-q:v", str(max(2, min(31, round(31 - (quality * 29 / 100)))))]
    elif fmt == "webp":
        args += ["-quality", str(max(1, min(100, quality)))]
    elif fmt == "png":
        args += ["-compression_level", str(max(0, min(9, round((100 - quality) * 9 / 100))))]
    if strip_metadata:
        args += ["-map_metadata", "-1", "-fflags", "+bitexact"]
    return args


def _try_montage_imagemagick(
    paths: Sequence[Path],
    grid: Tuple[int, int],
    out: Path,
    bg: str,
    spacing: int,
    timeout: float,
) -> bool:
    montage_bin = shutil.which("montage") or shutil.which("gm")
    if not montage_bin:
        return False
    base = [montage_bin] if not montage_bin.endswith("gm") else [montage_bin, "montage"]
    cols, rows = grid
    _run(
        [
            *base,
            *[str(p) for p in paths],
            "-tile", f"{cols}x{rows}",
            "-geometry", f"+{spacing}+{spacing}",
            "-background", bg,
            str(out),
        ],
        timeout=timeout,
    )
    return out.exists()


def _try_montage_ffmpeg(
    paths: Sequence[Path],
    grid: Tuple[int, int],
    out: Path,
    bg: str,
    spacing: int,
    ffmpeg: str,
    quality: int,
    timeout: float,
) -> bool:
    """ImageMagick-free pure FFmpeg montage builder using the tile filter."""
    if not paths:
        return False
    cols, rows = grid
    ext = paths[0].suffix.lstrip(".") or "png"
    with tempfile.TemporaryDirectory() as tdir:
        tmp = Path(tdir)
        for i, p in enumerate(paths):
            shutil.copyfile(p, tmp / f"tile_{i:04d}.{ext}")
        vf = f"tile={cols}x{rows}:padding={max(0, spacing)}:margin={max(0, spacing)}:color={bg}"
        _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-framerate", "1",
                "-start_number", "0",
                "-i", str(tmp / f"tile_%04d.{ext}"),
                "-vf", vf,
                "-frames:v", "1",
                *_encoder_args(out.suffix.lstrip(".").lower(), quality, False),
                "-y", str(out),
            ],
            timeout=timeout,
        )
    return out.exists()


def _list_keyframes(
    path: str,
    ffprobe: str,
    lo: float,
    hi: float,
    timeout: float,
    user_agent: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> List[float]:
    """Return I-frame presentation timestamps in range [lo, hi] using ffprobe."""
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "csv=p=0",
    ]
    if _is_url(path):
        cmd.extend(["-user_agent", user_agent or DEFAULT_UA])
        if headers:
            h_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd.extend(["-headers", h_lines])

    if hi > lo:
        cmd += ["-read_intervals", f"{max(0.0, lo):.3f}%{hi:.3f}"]
    cmd.append(path)
    try:
        proc = _run(cmd, timeout=timeout)
    except ToolError as e:
        _log(f"keyframe probe failed, proceeding without keyframe snap: {e.message}")
        return []
    out: List[float] = []
    for line in proc.stdout.splitlines():
        tok = line.strip().rstrip(",")
        if tok and tok.lower() not in ("n/a", ""):
            with contextlib.suppress(ValueError):
                out.append(float(tok))
    return sorted(out)


def _snap_to_keyframes(times: Iterable[float], keys: Sequence[float]) -> List[float]:
    if not keys:
        return list(times)
    import bisect
    snapped: List[float] = []
    for t in times:
        i = bisect.bisect_left(keys, t)
        cands = [keys[j] for j in (i - 1, i) if 0 <= j < len(keys)]
        snapped.append(min(cands, key=lambda k: abs(k - t)) if cands else t)
    return snapped


def _detect_scenes(
    path: str,
    ffmpeg: str,
    threshold: float,
    lo: float,
    hi: float,
    limit: int,
    timeout: float,
    user_agent: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> List[Tuple[float, float]]:
    """Native FFmpeg scene detection via select filter (no OpenCV/PySceneDetect)."""
    thr = min(0.99, max(0.01, float(threshold)))
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error"]
    if _is_url(path):
        cmd.extend(["-user_agent", user_agent or DEFAULT_UA])
        if headers:
            h_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd.extend(["-headers", h_lines])

    if lo > 0:
        cmd += ["-ss", f"{lo:.3f}"]
    if hi > lo:
        cmd += ["-to", f"{hi:.3f}"]
    cmd += [
        "-i", path, "-an", "-sn",
        "-filter:v", f"select='gt(scene,{thr})',metadata=print:file=-",
        "-f", "null", "-",
    ]
    try:
        proc = _run(cmd, timeout=timeout)
    except ToolError as e:
        _log(f"scene detection failed: {e.message}")
        return []

    scenes: List[Tuple[float, float]] = []
    pending: Optional[float] = None
    for line in proc.stdout.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            pending = float(m.group(1)) + (lo if lo > 0 else 0.0)
            continue
        m = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if m and pending is not None:
            scenes.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:
        scenes.append((pending, thr))
    scenes.sort(key=lambda x: x[0])
    return scenes[:limit] if limit > 0 else scenes


# ------------------------------------------------------------------------------
# Subtitle Extraction & Semantic Grounding
# ------------------------------------------------------------------------------

_SRT_BLOCK = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*\n(.*?)(?=\n\s*\n|\Z)",
    re.S,
)


def _parse_srt(text: str) -> List[Tuple[float, float, str]]:
    cues: List[Tuple[float, float, str]] = []
    for a, b, body in _SRT_BLOCK.findall(text):
        with contextlib.suppress(ValueError):
            s = _parse_time_to_seconds(a.replace(",", "."))
            e = _parse_time_to_seconds(b.replace(",", "."))
            clean = re.sub(r"<[^>]+>", " ", body)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                cues.append((s, e, clean))
    return cues


def _load_subtitles(
    path: str,
    ffmpeg: str,
    timeout: float,
    user_agent: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[List[Tuple[float, float, str]], str]:
    """Check sidecar subtitle files first, then embedded subtitle tracks."""
    if not _is_url(path):
        base = Path(path)
        for ext in (".srt", ".vtt", ".sbv"):
            side = base.with_suffix(ext)
            if side.exists():
                with contextlib.suppress(OSError, UnicodeDecodeError):
                    cues = _parse_srt(side.read_text(encoding="utf-8", errors="replace"))
                    if cues:
                        return cues, f"sidecar:{side.name}"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if _is_url(path):
        cmd.extend(["-user_agent", user_agent or DEFAULT_UA])
        if headers:
            h_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd.extend(["-headers", h_lines])

    cmd.extend(["-i", path, "-map", "0:s:0", "-f", "srt", "-"])
    with contextlib.suppress(ToolError):
        proc = _run(cmd, timeout=timeout)
        cues = _parse_srt(proc.stdout)
        if cues:
            return cues, "embedded-subtitles"
    return [], "none"


def _tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1]


def _rank_cues(
    cues: Sequence[Tuple[float, float, str]],
    query: str,
    limit: int,
) -> List[Dict[str, Any]]:
    q = set(_tokenize(query))
    if not q:
        return []
    scored: List[Tuple[float, Tuple[float, float, str]]] = []
    for cue in cues:
        toks = set(_tokenize(cue[2]))
        if not toks:
            continue
        overlap = len(q & toks)
        if overlap:
            scored.append((overlap / len(q), cue))
    scored.sort(key=lambda x: (-x[0], x[1][0]))
    return [
        {
            "start_time": _format_timestamp(c[0]),
            "end_time": _format_timestamp(c[1]),
            "start_seconds": round(c[0], 3),
            "end_seconds": round(c[1], 3),
            "relevance_score": round(score, 3),
            "description": _ellipsize(c[2], 160),
        }
        for score, c in scored[:limit]
    ]


# ==============================================================================
# SECTION 5: Core Execution Engine
# ==============================================================================

def _fail(message: str, code: int, started: float, **extra: Any) -> Dict[str, Any]:
    return {
        "success": False,
        "error": message,
        "exit_code": code,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "generated_thumbnails": [],
        "matching_segments": [],
        "cached": False,
        **extra,
    }


def execute_tool(
    target: str,
    query: Optional[str] = None,
    output_dir: str = "thumbnails",
    interval: float = 10.0,
    width: int = 320,
    format: str = "png",
    start: str = "00:00:00",
    end: str = "00:00:00",
    max_frames: int = 15,
    montage: str = "",
    montage_bg: str = "white",
    tile_spacing: int = 2,
    font: Optional[str] = None,
    font_size: int = 14,
    font_color: str = "white",
    box_color: str = "black",
    box_opacity: float = 0.5,
    position: str = "bl",
    quality: int = 80,
    add_timestamps: bool = False,
    strip_metadata: bool = False,
    only_montage: bool = False,
    timestamps: str = "",
    percentages: str = "",
    keyframes: bool = False,
    force_keyframes: bool = False,
    scenes: bool = False,
    scene_threshold: float = 0.4,
    query_capture: bool = False,
    jobs: int = 4,
    timeout: float = 180.0,
    manifest: str = "",
    progress: bool = False,
    dry_run: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    global _VERBOSE
    _VERBOSE = bool(verbose)
    started = time.monotonic()

    bad_inputs = validate_inputs(target, width, interval, max_frames, quality, jobs)
    if bad_inputs:
        return bad_inputs

    fmt = (format or "png").lower().strip()
    fmt = "jpg" if fmt == "jpeg" else fmt
    if fmt not in ("png", "jpg", "webp"):
        return _fail(f"Unsupported format '{format}'. Use png, jpg, or webp.", EXIT_INVALID_INPUT, started)

    try:
        grid = _parse_montage(montage)
    except ValueError as e:
        return _fail(str(e), EXIT_INVALID_INPUT, started)

    cache = ToolCache()
    cache_key = json.dumps(
        {
            "v": __version__, "t": target, "q": query, "o": output_dir, "i": interval,
            "w": width, "f": fmt, "s": start, "e": end, "mf": max_frames, "m": montage,
            "ts": timestamps, "pc": percentages, "kf": keyframes, "fkf": force_keyframes,
            "sc": scenes, "sct": scene_threshold, "qc": query_capture, "ql": quality,
            "at": add_timestamps, "pos": position, "fs": font_size, "om": only_montage,
        },
        sort_keys=True,
    )

    if use_cache and not dry_run:
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            cached_result["cached"] = True
            cached_result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
            return cached_result

    out_dir = Path(output_dir).expanduser().resolve()
    if not _validate_sandbox(out_dir):
        return _fail(
            f"output_dir '{output_dir}' is outside the allowed sandbox boundaries.",
            EXIT_PERMISSION_DENIED, started,
            hint="Set VIDEO_TOOL_SANDBOX=/custom/path to expand permitted paths.",
        )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail(f"Cannot create output_dir: {e}", EXIT_PERMISSION_DENIED, started)

    try:
        ffmpeg = _which_or_die("ffmpeg")
        ffprobe = _which_or_die("ffprobe")
    except ToolError as e:
        return _fail(e.message, e.exit_code, started)

    generated_thumbnails: List[str] = []
    matching_segments: List[Dict[str, Any]] = []
    manifest_data: List[Dict[str, Any]] = []
    montage_path: Optional[str] = None
    query_source = "none"
    temp_d: Optional[tempfile.TemporaryDirectory] = None
    net_headers: Dict[str, str] = {}
    net_ua = DEFAULT_UA

    try:
        # ── INPUT RESOLUTION & STREAM SCRAPING ────────────────────────────────
        if _is_url(target):
            resolved_url, dur_hint, headers = _resolve_stream_target(target, timeout=min(timeout, 25.0))
            net_headers = headers
            net_ua = headers.get("User-Agent", DEFAULT_UA)
            media_input = resolved_url

            # Probe remote stream directly via HTTP range requests
            try:
                info = _probe_json(media_input, ffprobe, timeout, user_agent=net_ua, headers=net_headers)
                if not _has_video_stream(info):
                    raise ToolError("Resolved stream contains no decodable video stream.", EXIT_INVALID_INPUT)
            except ToolError as probe_err:
                _log(f"direct remote stream probe failed: {probe_err.message}; falling back to safe download")
                temp_d = tempfile.TemporaryDirectory()
                media_input = str(_download_url(resolved_url, Path(temp_d.name), headers=net_headers, timeout=min(timeout, 60.0)))
                info = _probe_json(media_input, ffprobe, timeout)
                if not _has_video_stream(info):
                    raise ToolError(
                        "Target payload contains no valid video stream.",
                        EXIT_INVALID_INPUT,
                        {"hint": "The URL is an HTML page without extractable video containers."},
                    )
        else:
            local_path = Path(target).expanduser()
            if not local_path.exists():
                return _fail(f"Target video file not found: {target}", EXIT_FILE_NOT_FOUND, started)
            local_path = local_path.resolve()
            if not _validate_sandbox(local_path):
                return _fail(f"Target path '{target}' violates sandbox boundaries.", EXIT_PERMISSION_DENIED, started)
            if not os.access(local_path, os.R_OK):
                return _fail(f"Target file is not readable: {target}", EXIT_PERMISSION_DENIED, started)
            media_input = str(local_path)
            info = _probe_json(media_input, ffprobe, timeout)
            if not _has_video_stream(info):
                raise ToolError("Input contains no decodable video stream.", EXIT_INVALID_INPUT)

        duration = _duration_from_probe(info)
        if duration <= 0 and _is_url(target) and 'dur_hint' in locals() and dur_hint:
            duration = dur_hint

        try:
            start_s = _parse_time_to_seconds(start, duration)
            end_s = _parse_time_to_seconds(end, duration)
        except ValueError as e:
            return _fail(str(e), EXIT_INVALID_INPUT, started)

        if duration > 0:
            start_s = max(0.0, min(start_s, duration))
            end_s = min(end_s, duration)
        if end_s and end_s <= start_s:
            return _fail(f"--end ({end_s:.2f}s) must be greater than --start ({start_s:.2f}s).", EXIT_INVALID_INPUT, started)
        hi = end_s if end_s > 0 else duration

        stem = _sanitize_stem(target)
        per_dir = out_dir / stem
        per_dir.mkdir(parents=True, exist_ok=True)

        # ── QUERY / SUBTITLE MATCHING ─────────────────────────────────────────
        query_times: List[float] = []
        if query:
            cues, query_source = _load_subtitles(media_input, ffmpeg, timeout, user_agent=net_ua, headers=net_headers)
            if cues:
                matching_segments = _rank_cues(cues, query, max(5, max_frames))
                query_times = [m["start_seconds"] for m in matching_segments]
            else:
                query_source = "unavailable"
                matching_segments = [{
                    "start_time": _format_timestamp(start_s),
                    "end_time": _format_timestamp(hi or start_s),
                    "relevance_score": 0.0,
                    "description": "No subtitles or transcript tracks available for matching.",
                }]

        # ── CAPTURE POINTS PLANNING ───────────────────────────────────────────
        times: List[float] = []
        plan_source = "interval"
        if timestamps.strip():
            plan_source = "explicit"
            for part in timestamps.split(","):
                if part.strip():
                    times.append(_parse_time_to_seconds(part.strip(), duration))
        elif percentages.strip():
            plan_source = "percentages"
            if duration <= 0:
                return _fail("--percentages requires a probeable media duration.", EXIT_INVALID_INPUT, started)
            for part in percentages.split(","):
                part = part.strip().rstrip("%")
                if not part:
                    continue
                try:
                    pct = float(part)
                except ValueError:
                    return _fail(f"Invalid percentage value: {part!r}", EXIT_INVALID_INPUT, started)
                if not 0.0 <= pct <= 100.0:
                    return _fail(f"Percentage out of range 0-100: {pct}", EXIT_INVALID_INPUT, started)
                times.append(duration * pct / 100.0)
        elif scenes:
            plan_source = "scenes"
            detected = _detect_scenes(media_input, ffmpeg, scene_threshold, start_s, hi, max_frames, timeout * 2, user_agent=net_ua, headers=net_headers)
            times = [t for t, _ in detected]
            if not matching_segments:
                matching_segments = [
                    {
                        "start_time": _format_timestamp(t),
                        "end_time": _format_timestamp(t + 2),
                        "relevance_score": round(min(1.0, score), 3),
                        "description": f"Scene change (score {score:.2f})",
                    }
                    for t, score in detected[:10]
                ]
            if not times:
                plan_source = "interval(scene-fallback)"
        elif query_capture and query_times:
            plan_source = "query-cues"
            times = query_times[:max_frames]

        if not times:
            stop = hi if hi > 0 else (start_s + interval * max_frames)
            t = start_s
            while (duration <= 0 or t < stop - 1e-3) and len(times) < max_frames:
                times.append(t)
                t += interval
            if not times:
                times.append(start_s)

        # Clamping, deduplication, sorting
        clean: List[float] = []
        seen: set = set()
        for t in times:
            t = max(0.0, t if duration <= 0 else min(t, max(0.0, duration - 0.05)))
            key = round(t, 2)
            if key not in seen:
                seen.add(key)
                clean.append(round(t, 3))
        times = sorted(clean)[:max_frames]

        if keyframes:
            kf = _list_keyframes(media_input, ffprobe, start_s, hi, timeout, user_agent=net_ua, headers=net_headers)
            if kf:
                times = sorted({round(x, 3) for x in _snap_to_keyframes(times, kf)})[:max_frames]

        if dry_run:
            return {
                "success": True,
                "target": target,
                "query": query,
                "query_source": query_source,
                "dry_run": True,
                "media_duration": round(duration, 3),
                "plan_source": plan_source,
                "planned_timestamps": [
                    {"index": i, "seconds": t, "formatted": _format_timestamp(t)}
                    for i, t in enumerate(times)
                ],
                "generated_thumbnails": [],
                "matching_segments": matching_segments,
                "cached": False,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # ── FRAME EXTRACTION (PARALLEL & ORDER-PRESERVED) ─────────────────────
        enc_args = _encoder_args(fmt, quality, strip_metadata)
        total = len(times)
        done = 0
        results: List[Optional[Dict[str, Any]]] = [None] * total

        def extract(idx_t: Tuple[int, float]) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
            idx, t = idx_t
            out_thumb_path = per_dir / f"{stem}_{idx:04d}_{int(t)}s.{fmt}"
            vf_parts = [f"scale={width}:-2:flags=bicubic"]
            if add_timestamps:
                vf_parts.append(
                    _drawtext_filter(
                        _format_timestamp(t), position, font, font_size,
                        font_color, box_color, box_opacity
                    )
                )
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
            if _is_url(media_input):
                cmd.extend(["-user_agent", net_ua])
                if net_headers:
                    h_lines = "".join(f"{k}: {v}\r\n" for k, v in net_headers.items())
                    cmd.extend(["-headers", h_lines])

            if force_keyframes:
                # Two-phase seek: fast coarse input seek + accurate fine output seek
                coarse = max(0.0, t - 5.0)
                cmd += ["-ss", f"{coarse:.3f}", "-i", media_input, "-ss", f"{t - coarse:.3f}"]
            else:
                # Fast input seek enables instant seeking across remote network streams
                cmd += ["-ss", f"{t:.3f}", "-i", media_input]

            cmd += [
                "-an", "-sn", "-frames:v", "1",
                "-vf", ",".join(vf_parts),
                *enc_args,
                "-y", str(out_thumb_path),
            ]
            try:
                _run(cmd, timeout=timeout)
            except ToolError as e:
                return idx, None, f"t={_format_timestamp(t)}: {e.message}"
            if not out_thumb_path.exists() or out_thumb_path.stat().st_size == 0:
                with contextlib.suppress(OSError):
                    out_thumb_path.unlink(missing_ok=True)
                return idx, None, f"t={_format_timestamp(t)}: ffmpeg emitted zero-byte frame"
            return idx, {
                "index": idx,
                "output": str(out_thumb_path),
                "timestamp": round(t, 3),
                "formatted": _format_timestamp(t),
                "bytes": out_thumb_path.stat().st_size,
            }, None

        failures: List[str] = []
        with GracefulShutdown() as shutdown:
            worker_count = max(1, min(int(jobs), total))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                for idx, payload, err in pool.map(extract, list(enumerate(times))):
                    if shutdown.should_stop():
                        raise ToolError("Thumbnail generation interrupted by signal.", EXIT_INTERRUPTED)
                    results[idx] = payload
                    if err:
                        failures.append(err)
                    done += 1
                    if progress and _is_tty(no_color):
                        pct = int(done * 100 / max(1, total))
                        _cprint(f"\r{NEON_CYAN}  extracting frames: {done}/{total} ({pct}%){RESET}", no_color=no_color, end="")
        if progress and _is_tty(no_color):
            _cprint("", no_color=no_color)

        for payload in results:
            if payload:
                generated_thumbnails.append(payload["output"])
                manifest_data.append(payload)

        if not generated_thumbnails:
            raise ToolError(
                "No frames could be extracted. " + (failures[0] if failures else "Decode failure."),
                EXIT_ERROR,
            )

        # ── MONTAGE GRID GENERATION ───────────────────────────────────────────
        if grid:
            cap_tiles = grid[0] * grid[1]
            tile_paths = [Path(p) for p in generated_thumbnails[:cap_tiles]]
            montage_out = per_dir / f"{stem}_montage_{grid[0]}x{grid[1]}.{fmt}"
            ok = False
            with contextlib.suppress(ToolError):
                ok = _try_montage_imagemagick(tile_paths, grid, montage_out, montage_bg, tile_spacing, timeout)
            if not ok:
                ok = _try_montage_ffmpeg(tile_paths, grid, montage_out, montage_bg, tile_spacing, ffmpeg, quality, timeout)
            if ok:
                montage_path = str(montage_out)
                generated_thumbnails.append(montage_path)
                if only_montage:
                    for tp in tile_paths:
                        with contextlib.suppress(OSError):
                            tp.unlink()
                    generated_thumbnails = [montage_path]
            else:
                failures.append("Montage generation failed.")

        # ── MANIFEST WRITER ───────────────────────────────────────────────────
        if manifest:
            manifest_path = Path(manifest).expanduser()
            if manifest_path.is_dir() or str(manifest).endswith(os.sep):
                manifest_path = manifest_path / "manifest.json"
            manifest_path = manifest_path.resolve()
            if not _validate_sandbox(manifest_path):
                failures.append("Manifest destination is outside sandbox boundary.")
            else:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(
                        {
                            "tool_version": __version__,
                            "target": target,
                            "media_duration": round(duration, 3),
                            "plan_source": plan_source,
                            "format": fmt,
                            "width": width,
                            "montage": montage_path,
                            "frames": manifest_data,
                            "generated_at": datetime.now().isoformat(timespec="seconds"),
                        },
                        mf, indent=2, cls=ToolJSONEncoder,
                    )

    except ToolError as te:
        return _fail(te.message, te.exit_code, started, target=target, **te.details)
    except KeyboardInterrupt:
        return _fail("Execution interrupted by user.", EXIT_INTERRUPTED, started, target=target)
    except Exception as exc:
        return _fail(f"Execution error: {type(exc).__name__}: {exc}", EXIT_ERROR, started, target=target)
    finally:
        if temp_d:
            with contextlib.suppress(Exception):
                temp_d.cleanup()

    result: Dict[str, Any] = {
        "success": True,
        "target": target,
        "query": query,
        "query_source": query_source,
        "media_duration": round(duration, 3),
        "plan_source": plan_source,
        "output_dir": str(per_dir),
        "generated_thumbnails": generated_thumbnails,
        "montage_path": montage_path,
        "frames": manifest_data,
        "matching_segments": matching_segments,
        "warnings": failures,
        "cached": False,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "exit_code": EXIT_SUCCESS,
    }

    if use_cache:
        cache.set(cache_key, result)
    return result


# ==============================================================================
# SECTION 6: Output Routing & AIChat Entrypoints
# ==============================================================================

def write_llm_output(data: Dict[str, Any]) -> None:
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    payload = json.dumps(data, ensure_ascii=False, cls=ToolJSONEncoder)

    if out_path in {"/dev/stdout", "/dev/fd/1", "-", ""}:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return

    try:
        target_file = Path(out_path)
        if target_file.parent and not target_file.parent.exists():
            target_file.parent.mkdir(parents=True, exist_ok=True)
        with open(target_file, "a", encoding="utf-8") as fp:
            fp.write(payload + "\n")
    except OSError as err:
        sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def run(
    target: str,
    query: Optional[str] = None,
    output_dir: str = "thumbnails",
    interval: float = 10.0,
    width: int = 320,
    format: str = "png",
    start: str = "00:00:00",
    end: str = "00:00:00",
    max_frames: int = 15,
    montage: str = "",
    montage_bg: str = "white",
    tile_spacing: int = 2,
    font: Optional[str] = None,
    font_size: int = 14,
    font_color: str = "white",
    box_color: str = "black",
    box_opacity: float = 0.5,
    position: str = "bl",
    quality: int = 80,
    add_timestamps: bool = False,
    strip_metadata: bool = False,
    only_montage: bool = False,
    timestamps: str = "",
    percentages: str = "",
    keyframes: bool = False,
    force_keyframes: bool = False,
    scenes: bool = False,
    scene_threshold: float = 0.4,
    query_capture: bool = False,
    jobs: int = 4,
    timeout: float = 180.0,
    manifest: str = "",
    progress: bool = False,
    dry_run: bool = False,
    use_cache: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> None:
    result = execute_tool(
        target=target, query=query, output_dir=output_dir, interval=interval, width=width,
        format=format, start=start, end=end, max_frames=max_frames, montage=montage,
        montage_bg=montage_bg, tile_spacing=tile_spacing, font=font, font_size=font_size,
        font_color=font_color, box_color=box_color, box_opacity=box_opacity, position=position,
        quality=quality, add_timestamps=add_timestamps, strip_metadata=strip_metadata,
        only_montage=only_montage, timestamps=timestamps, percentages=percentages,
        keyframes=keyframes, force_keyframes=force_keyframes, scenes=scenes,
        scene_threshold=scene_threshold, query_capture=query_capture, jobs=jobs,
        timeout=timeout, manifest=manifest, progress=progress, dry_run=dry_run,
        use_cache=use_cache, no_color=no_color, verbose=verbose,
    )
    print_human_readable_ui(result, no_color=no_color)
    write_llm_output(result)


# ==============================================================================
# SECTION 7: CLI Parser & Main Entrypoint
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video_master_tool.py",
        description=f"Native FFmpeg Video Intelligence Tool v{__version__}",
    )
    p.add_argument("--target", "-t", help="Target video file or media URL")
    p.add_argument("--query", "-q", default=None, help="Query text matched against subtitles")
    p.add_argument("--output-dir", default="thumbnails", help="Output directory")
    p.add_argument("--interval", type=float, default=10.0, help="Seconds between frames")
    p.add_argument("--width", type=int, default=320, help="Thumbnail width")
    p.add_argument("--format", choices=["png", "jpg", "jpeg", "webp"], default="png", help="Image format")
    p.add_argument("--start", default="00:00:00", help="Start time (HH:MM:SS | secs | NN%%)")
    p.add_argument("--end", default="00:00:00", help="End time (HH:MM:SS | secs | NN%%)")
    p.add_argument("--max-frames", type=int, default=15, help="Max frames limit")
    p.add_argument("--montage", default="", help="Grid layout (e.g. 2x3)")
    p.add_argument("--montage-bg", default="white", help="Montage background color")
    p.add_argument("--tile-spacing", type=int, default=2, help="Montage tile padding (px)")
    p.add_argument("--timestamps", default="", help="Comma-separated explicit timestamps")
    p.add_argument("--percentages", default="", help="Comma-separated percentages (0-100)")
    p.add_argument("--quality", type=int, default=80, help="Quality (1-100)")
    p.add_argument("--font", default=None, help="TTF font file for timestamp overlay")
    p.add_argument("--font-size", type=int, default=14, help="Overlay font size")
    p.add_argument("--font-color", default="white", help="Overlay font color")
    p.add_argument("--box-color", default="black", help="Overlay box color")
    p.add_argument("--box-opacity", type=float, default=0.5, help="Overlay box opacity (0-1)")
    p.add_argument("--position", choices=["tl", "tr", "bl", "br", "center"], default="bl", help="Overlay position")
    p.add_argument("--scene-threshold", type=float, default=0.4, help="Scene sensitivity 0-1")
    p.add_argument("--jobs", "-j", type=int, default=4, help="Parallel extraction workers")
    p.add_argument("--timeout", type=float, default=180.0, help="Per-subprocess timeout (s)")
    p.add_argument("--manifest", default="", help="Write JSON manifest to this path")
    p.add_argument("--scenes", action="store_true", default=False, help="Capture at scene changes")
    p.add_argument("--query-capture", action="store_true", default=False, help="Capture frames at subtitle cues matching --query")
    p.add_argument("--keyframes", action="store_true", default=False, help="Snap to nearest keyframe")
    p.add_argument("--force-keyframes", action="store_true", default=False, help="Frame-accurate seeking")
    p.add_argument("--add-timestamps", action="store_true", default=False, help="Burn timestamp overlay")
    p.add_argument("--strip-metadata", action="store_true", default=False, help="Strip EXIF metadata")
    p.add_argument("--only-montage", action="store_true", default=False, help="Keep only montage grid")
    p.add_argument("--dry-run", action="store_true", default=False, help="Plan without writing files")
    p.add_argument("--progress", action="store_true", default=False, help="Show progress on stderr")
    p.add_argument("--use-cache", action="store_true", default=False, help="Enable result caching")
    p.add_argument("--clear-cache", action="store_true", default=False, help="Clear cache and exit")
    p.add_argument("--schema", action="store_true", default=False, help="Print JSON Schema and exit")
    p.add_argument("--no-color", action="store_true", default=False, help="Disable colors")
    p.add_argument("--verbose", "-v", action="store_true", default=False, help="Verbose logging")
    p.add_argument("--version", action="version", version=f"video_master_tool {__version__}")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    if args.schema:
        sys.stdout.write(json.dumps(generate_tool_schema(), indent=2) + "\n")
        sys.stdout.flush()
        return EXIT_SUCCESS

    if args.clear_cache:
        removed = invalidate_cache()
        _cprint(f"{NEON_GREEN}Cleared {removed} video tool cache file(s).{RESET}", no_color=args.no_color)
        return EXIT_SUCCESS

    if not args.target:
        _cprint(f"{NEON_RED}Error: --target parameter is required for execution.{RESET}", no_color=args.no_color)
        return EXIT_INVALID_INPUT

    res = execute_tool(
        target=args.target, query=args.query, output_dir=args.output_dir, interval=args.interval,
        width=args.width, format=args.format, start=args.start, end=args.end,
        max_frames=args.max_frames, montage=args.montage, montage_bg=args.montage_bg,
        tile_spacing=args.tile_spacing, font=args.font, font_size=args.font_size,
        font_color=args.font_color, box_color=args.box_color, box_opacity=args.box_opacity,
        position=args.position, quality=args.quality, timestamps=args.timestamps,
        percentages=args.percentages, keyframes=args.keyframes,
        force_keyframes=args.force_keyframes, scenes=args.scenes,
        scene_threshold=args.scene_threshold, query_capture=args.query_capture,
        jobs=args.jobs, timeout=args.timeout, add_timestamps=args.add_timestamps,
        strip_metadata=args.strip_metadata, only_montage=args.only_montage,
        manifest=args.manifest, progress=args.progress, dry_run=args.dry_run,
        use_cache=args.use_cache, no_color=args.no_color, verbose=args.verbose,
    )

    print_human_readable_ui(res, no_color=args.no_color)
    write_llm_output(res)
    return int(res.get("exit_code", EXIT_SUCCESS))


if __name__ == "__main__":
    sys.exit(main())
