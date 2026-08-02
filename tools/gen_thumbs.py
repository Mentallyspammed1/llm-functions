#!/usr/bin/env python3
"""Generate video thumbnails (local paths or URLs) via ffmpeg — llm-functions tool."""

# @describe Generate video thumbnails (local paths or URLs) via ffmpeg.
#
# @option --input!                One or more video paths or URLs (separated by commas or newlines).
# @option --output-dir            Directory where thumbnail images are written (default: thumbnails).
# @option --interval              Seconds between each captured frame (default: 10).
# @option --width                 Thumbnail width in pixels; height scales automatically (default: 320).
# @option --format                Output image format (png, jpg, webp) (default: png).
# @option --start                 Start time as HH:MM:SS or seconds (default: 00:00:00).
# @option --end                   End time as HH:MM:SS or seconds (default: 00:00:00).
# @option --max-frames            Maximum number of thumbnails per input (default: 10).
# @option --timestamps            Comma-separated specific timestamps (HH:MM:SS or seconds) to capture.
# @option --percentages           Comma-separated percentages (0-100) of video duration to capture.
# @option --keyframes             Capture at keyframes only (I-frames) within time range.
# @option --scene-threshold       Scene change detection threshold (0.1-1.0, requires ffmpeg scene filter).
# @option --force-keyframes       Force keyframes at exact timestamps (re-encodes for precise seeking).
# @option --manifest              Output JSON manifest with capture details.
# @option --progress              Show progress bar for long operations.
# @option --montage-bg            Montage background color (default: white).
# @option --tile-spacing          Montage tile spacing in pixels (default: 2).
# @option --font                  Optional path to font file for timestamp overlay.
# @option --font-size             Font size for timestamp overlay (default: 14).
# @option --font-color            Font color for timestamp overlay (default: white).
# @option --box-color             Box background color for timestamp overlay (default: black).
# @option --box-opacity           Box background opacity for timestamp overlay (0.0 to 1.0) (default: 0.5).
# @option --position              Position of timestamp overlay (tl, tr, bl, br) (default: bl).
# @option --quality               JPEG/WebP quality (1-100) or PNG compression (1-9) (default: 80).
# @flag   --add-timestamps        If set, draw the capture time on each thumbnail.
# @flag   --strip-metadata        If set, strip EXIF metadata from output thumbnails.
# @flag   --only-montage          If set, clean up intermediate frames and only keep the montage grid.
# @flag   --verbose               Enable verbose logging to stderr.
#

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")

_verbose: bool = False


def _debug(msg: str) -> None:
    if _verbose:
        print(f"[DEBUG] {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"[INFO] {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"[WARNING] {msg}", file=sys.stderr)


def _validate_sandbox(path: Path) -> bool:
    home = Path.home().resolve()
    tmp = Path("/tmp").resolve()
    try:
        resolved = path.resolve()
        s = str(resolved)
        return s.startswith(str(home)) or s.startswith(str(tmp))
    except OSError:
        return False


def _run(cmd: Sequence[str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    _debug(f"Running command: {' '.join(cmd)}")
    return subprocess.run(
        list(cmd),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _which_or_die(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    return path


def _parse_time_to_seconds(s: str, duration: float = 0.0) -> float:
    """Parse time string to seconds. Supports HH:MM:SS, seconds, percentages (50%), and relative times (+5s, -10s)."""
    s = (s or "").strip()
    if not s or s == "00:00:00":
        return 0.0

    # Percentage (e.g., "50%")
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
            if 0 <= pct <= 100 and duration > 0:
                return duration * pct / 100.0
        except ValueError:
            pass
        raise ValueError(f"invalid percentage: {s}")

    # Relative time (e.g., "+5s", "-10s", "+1:30", "+5", "-10")
    if s.startswith(("+", "-")):
        sign = 1 if s[0] == "+" else -1
        rel_str = s[1:]
        # Handle "5s" format (seconds with 's' suffix)
        if rel_str.endswith("s") and rel_str[:-1].replace(".", "").isdigit():
            rel = float(rel_str[:-1])
        else:
            rel = _parse_time_to_seconds(rel_str, duration)
        if sign > 0:
            # Positive relative: from start (0)
            return max(0.0, min(duration, rel))
        else:
            # Negative relative: from end (duration)
            if duration > 0:
                return max(0.0, min(duration, duration - rel))
            return max(0.0, -rel)

    # Plain seconds with optional 's' suffix (e.g., "5s", "10.5s")
    if s.endswith("s") and s[:-1].replace(".", "").isdigit():
        return float(s[:-1])

    # Plain seconds
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)

    # HH:MM:SS[.frac]
    m = TIME_RE.match(s)
    if not m:
        raise ValueError(f"invalid time: {s}")
    h, mi, sec, frac = m.groups()
    h = int(h or 0)
    mi, sec = int(mi), int(sec)
    f = float(f"0.{frac}") if frac else 0.0
    return h * 3600 + mi * 60 + sec + f


def _is_url(s: str) -> bool:
    p = urlparse(s.strip())
    return p.scheme in ("http", "https", "ftp") and bool(p.netloc)


def _sanitize_stem(name: str) -> str:
    if _is_url(name):
        stem = Path(urlparse(name).path).stem
    else:
        stem = Path(name).stem
    if not stem:
        stem = "video"
    return re.sub(r"[^\w.\-]+", "_", stem)[:120]


def _format_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _probe_duration(path: str, ffprobe: str) -> float:
    proc = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    return float(proc.stdout.strip())


def _parse_timestamp_list(s: str, duration: float = 0.0) -> List[float]:
    """Parse comma-separated timestamps (HH:MM:SS or seconds) into seconds."""
    if not s:
        return []
    result = []
    for part in s.split(","):
        part = part.strip()
        if part:
            result.append(_parse_time_to_seconds(part, duration))
    return result


def _parse_percentage_list(s: str) -> List[float]:
    """Parse comma-separated percentages (0-100) into fractions."""
    if not s:
        return []
    result = []
    for part in s.split(","):
        part = part.strip()
        if part:
            try:
                pct = float(part)
                if 0 <= pct <= 100:
                    result.append(pct / 100.0)
                else:
                    _warn(f"Percentage {pct} out of range 0-100, skipping")
            except ValueError:
                _warn(f"Invalid percentage: {part}, skipping")
    return result


def _compute_timestamps(
    duration: float,
    start: float,
    end: float,
    interval: float,
    max_frames: int,
    timestamps: str = "",
    percentages: str = "",
    keyframes: bool = False,
    scene_threshold: float = 0.0,
    force_keyframes: bool = False,
    manifest_data: Optional[List[dict]] = None,
    progress: bool = False,
    ffmpeg: str = "",
    src: str = "",
) -> List[float]:
    # If explicit timestamps provided, use those
    if timestamps:
        times = _parse_timestamp_list(timestamps, duration)
        # Filter to valid range
        times = [t for t in times if 0 <= t <= duration]
        if manifest_data is not None:
            for t in times[:max_frames]:
                manifest_data.append(
                    {
                        "timestamp": round(t, 3),
                        "timestamp_formatted": _format_timestamp(t),
                        "frame_type": "requested",
                        "scene_score": None,
                        "method": "timestamp",
                    }
                )
        return times[:max_frames]

    # If percentages provided, convert to absolute times
    if percentages:
        pcts = _parse_percentage_list(percentages)
        times = [p * duration for p in pcts]
        # Filter to valid range
        times = [t for t in times if 0 <= t <= duration]
        if manifest_data is not None:
            for t in times[:max_frames]:
                manifest_data.append(
                    {
                        "timestamp": round(t, 3),
                        "timestamp_formatted": _format_timestamp(t),
                        "frame_type": "requested",
                        "scene_score": None,
                        "method": "percentage",
                    }
                )
        return times[:max_frames]

    # If keyframes or scene detection requested, use ffmpeg to find them
    if keyframes or scene_threshold > 0:
        if not ffmpeg or not src:
            _warn(
                "Keyframe/scene detection requires ffmpeg and source path, falling back to interval"
            )
        else:
            times = _detect_keyframes_or_scenes(
                ffmpeg,
                src,
                duration,
                start,
                end,
                keyframes,
                scene_threshold,
                max_frames,
                force_keyframes,
                manifest_data,
                progress,
            )
            if times:
                return times

    # Default: interval-based capture
    if end > 0:
        if end > start:
            stop = min(duration, end)
        else:
            stop = min(duration, start + end)
    else:
        stop = duration
    start = max(0.0, min(start, duration))
    if stop <= start:
        return [start]

    times: List[float] = []
    t = start
    while t < stop - 1e-3 and len(times) < max_frames:
        times.append(round(t, 3))
        t += interval
    if not times:
        times.append(start)
    if manifest_data is not None:
        for t in times[:max_frames]:
            manifest_data.append(
                {
                    "timestamp": round(t, 3),
                    "timestamp_formatted": _format_timestamp(t),
                    "frame_type": "interval",
                    "scene_score": None,
                    "method": "interval",
                }
            )
    return times[:max_frames]


def _detect_keyframes_or_scenes(
    ffmpeg: str,
    src: str,
    duration: float,
    start: float,
    end: float,
    keyframes: bool,
    scene_threshold: float,
    max_frames: int,
    force_keyframes: bool = False,
    manifest_data: Optional[List[dict]] = None,
    progress: bool = False,
) -> List[float]:
    """Use ffmpeg to detect keyframes (I-frames) or scene changes."""
    try:
        # Build the filter graph
        filters = []
        if keyframes:
            filters.append("select='eq(pict_type,I)'")
        if scene_threshold > 0:
            filters.append(f"select='gt(scene,{scene_threshold})'")

        if not filters:
            return []

        filter_str = ",".join(filters) + ",showinfo"

        # Determine time range
        if end > 0:
            if end > start:
                stop = min(duration, end)
            else:
                stop = min(duration, start + end)
        else:
            stop = duration
        start = max(0.0, min(start, duration))

        # Run ffmpeg with showinfo to get timestamps
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            str(start),
            "-to",
            str(stop),
            "-i",
            src,
            "-vf",
            filter_str,
            "-f",
            "null",
            "-",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120.0)

        # Parse showinfo output for timestamps
        times = []
        for line in result.stderr.split("\n"):
            if "pts_time:" in line:
                try:
                    # Extract pts_time value
                    parts = line.split("pts_time:")
                    if len(parts) > 1:
                        t_str = parts[1].split()[0]
                        t = float(t_str)
                        if start <= t <= stop:
                            times.append(round(t, 3))
                except (ValueError, IndexError):
                    pass

        # Deduplicate and sort
        times = sorted(set(times))

        # Populate manifest data if provided
        if manifest_data is not None:
            for t in times:
                manifest_data.append(
                    {
                        "timestamp": t,
                        "formatted": _format_timestamp(t),
                        "frame_type": "I" if keyframes else "scene",
                        "scene_score": scene_threshold if scene_threshold > 0 else None,
                    }
                )

        return times[:max_frames]
    except Exception as e:
        _warn(f"Keyframe/scene detection failed: {e}")
        return []


def _extract_frame(
    ffmpeg: str,
    src: str,
    t: float,
    out_path: Path,
    width: int,
    fmt: str,
    add_timestamp: bool,
    font: Optional[str] = None,
    font_size: int = 14,
    font_color: str = "white",
    box_color: str = "black",
    box_opacity: float = 0.5,
    position: str = "bl",
    quality: int = 80,
    strip_metadata: bool = False,
) -> None:
    vf_parts = [f"scale={width}:-2"]
    if add_timestamp:
        ts = _format_timestamp(t).replace(":", r"\:")
        # Calculate overlay position parameters
        if position == "tl":
            x, y = "10", "10"
        elif position == "tr":
            x, y = "w-tw-10", "10"
        elif position == "br":
            x, y = "w-tw-10", "h-th-10"
        else:  # bl default
            x, y = "10", "h-th-10"

        drawtext = (
            f"drawtext=text='{ts}':fontsize={font_size}:fontcolor={font_color}:"
            f"box=1:boxcolor={box_color}@{box_opacity}:x={x}:y={y}"
        )
        if font:
            drawtext += f":fontfile='{font}'"
        vf_parts.append(drawtext)

    vf = ",".join(vf_parts)
    ext = fmt.lower()
    extra: List[str] = []
    if ext in ("jpg", "jpeg"):
        # JPEG quality scale from 1 (best) to 31 (worst). Maps 1-100 to 2-31 roughly.
        q_val = max(2, min(31, int(31 - (quality * 29 / 100))))
        extra.extend(["-q:v", str(q_val)])
    elif ext == "webp":
        extra.extend(["-quality", str(quality)])
    elif ext == "png":
        # PNG compression from 1 (fastest) to 9 (best)
        png_comp = max(1, min(9, int(quality / 10)))
        extra.extend(["-compression_level", str(png_comp)])

    if strip_metadata:
        extra.extend(["-map_metadata", "-1"])

    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(max(0.0, t)),
            "-i",
            src,
            "-frames:v",
            "1",
            "-vf",
            vf,
            *extra,
            "-y",
            str(out_path),
        ]
    )


def _extract_frame_precise(
    ffmpeg: str,
    src: str,
    t: float,
    out_path: Path,
    width: int,
    fmt: str,
    add_timestamp: bool,
    font: Optional[str] = None,
    font_size: int = 14,
    font_color: str = "white",
    box_color: str = "black",
    box_opacity: float = 0.5,
    position: str = "bl",
    quality: int = 80,
    strip_metadata: bool = False,
) -> None:
    """Extract frame with forced keyframe at exact timestamp by re-encoding a small segment."""
    vf_parts = [f"scale={width}:-2"]
    if add_timestamp:
        ts = _format_timestamp(t).replace(":", r"\:")
        if position == "tl":
            x, y = "10", "10"
        elif position == "tr":
            x, y = "w-tw-10", "10"
        elif position == "br":
            x, y = "w-tw-10", "h-th-10"
        else:
            x, y = "10", "h-th-10"

        drawtext = (
            f"drawtext=text='{ts}':fontsize={font_size}:fontcolor={font_color}:"
            f"box=1:boxcolor={box_color}@{box_opacity}:x={x}:y={y}"
        )
        if font:
            drawtext += f":fontfile='{font}'"
        vf_parts.append(drawtext)

    vf = ",".join(vf_parts)
    ext = fmt.lower()
    extra: List[str] = []
    if ext in ("jpg", "jpeg"):
        q_val = max(2, min(31, int(31 - (quality * 29 / 100))))
        extra.extend(["-q:v", str(q_val)])
    elif ext == "webp":
        extra.extend(["-quality", str(quality)])
    elif ext == "png":
        png_comp = max(1, min(9, int(quality / 10)))
        extra.extend(["-compression_level", str(png_comp)])

    if strip_metadata:
        extra.extend(["-map_metadata", "-1"])

    # Seek slightly before target, force keyframe at exact position using output option
    seek_before = max(0.0, t - 0.5)
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(seek_before),
            "-i",
            src,
            "-t",
            "1.0",  # 1 second segment
            "-vf",
            vf,
            "-force_key_frames",
            str(t - seek_before),
            "-frames:v",
            "1",
            *extra,
            "-y",
            str(out_path),
        ]
    )


def _parse_montage(montage: str) -> Optional[Tuple[int, int]]:
    m = (montage or "").strip().lower()
    if not m:
        return None
    if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", m):
        raise ValueError(f"--montage must be NxM (e.g. 2x3), got: {montage}")
    a, b = m.split("x")
    return int(a), int(b)


def _try_montage(
    paths: List[Path],
    grid: Tuple[int, int],
    out: Path,
    bg: str = "white",
    spacing: int = 2,
) -> bool:
    # Try ImageMagick montage first, fall back to GraphicsMagick gm montage
    montage_bin = shutil.which("montage")
    cmd = []
    if montage_bin:
        cmd = [montage_bin]
    else:
        gm_bin = shutil.which("gm")
        if gm_bin:
            cmd = [gm_bin, "montage"]

    if not cmd:
        return False

    cols, rows = grid
    _run(
        [
            *cmd,
            *[str(p) for p in paths],
            "-tile",
            f"{cols}x{rows}",
            "-geometry",
            f"+{spacing}+{spacing}",
            "-background",
            bg,
            str(out),
        ]
    )
    return True


def _split_inputs(raw: str) -> List[str]:
    parts: List[str] = []
    for chunk in re.split(r"[\n,]+", raw or ""):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _download_url(url: str, temp_dir: Path) -> Path:
    _info(f"Downloading remote URL: {url}...")
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    target = temp_dir / "downloaded_video"
    with urllib.request.urlopen(req, timeout=30.0) as response, open(
        target, "wb"
    ) as out_file:
        shutil.copyfileobj(response, out_file)
    return target


def _process_one(
    inp: str,
    out_dir: Path,
    interval: float,
    width: int,
    fmt: str,
    start_s: float,
    end_s: float,
    max_frames: int,
    montage: str,
    montage_bg: str,
    tile_spacing: int,
    font: Optional[str],
    font_size: int,
    font_color: str,
    box_color: str,
    box_opacity: float,
    position: str,
    quality: int,
    add_timestamps: bool,
    strip_metadata: bool,
    only_montage: bool,
    ffmpeg: str,
    ffprobe: str,
    timestamps: str = "",
    percentages: str = "",
    keyframes: bool = False,
    scene_threshold: float = 0.0,
    force_keyframes: bool = False,
    manifest_data: Optional[List[dict]] = None,
    progress: bool = False,
) -> List[str]:
    stem = _sanitize_stem(inp)
    per_dir = out_dir / stem
    per_dir.mkdir(parents=True, exist_ok=True)

    local_file = inp
    temp_d = None
    try:
        if _is_url(inp):
            temp_d = tempfile.TemporaryDirectory()
            local_file = str(_download_url(inp, Path(temp_d.name)))
        else:
            local_file_path = Path(local_file).expanduser().resolve()
            if not _validate_sandbox(local_file_path):
                raise ValueError(f"Input path '{inp}' violates sandbox boundaries.")
            local_file = str(local_file_path)

        duration = _probe_duration(local_file, ffprobe)
        times = _compute_timestamps(
            duration,
            start_s,
            end_s,
            interval,
            max_frames,
            timestamps=timestamps,
            percentages=percentages,
            keyframes=keyframes,
            scene_threshold=scene_threshold,
            force_keyframes=force_keyframes,
            manifest_data=manifest_data,
            progress=progress,
            ffmpeg=ffmpeg,
            src=local_file,
        )

        paths: List[Path] = []
        lines: List[str] = []

        # Progress bar for frame extraction
        if progress and len(times) > 1:
            try:
                from tqdm import tqdm

                time_iter = tqdm(
                    enumerate(times),
                    total=len(times),
                    desc=f"Extracting {stem}",
                    unit="frame",
                )
            except ImportError:
                time_iter = enumerate(times)
                if progress:
                    _info(
                        "Progress bar requested but tqdm not installed. Install with: pip install tqdm"
                    )
        else:
            time_iter = enumerate(times)

        for i, t in time_iter:
            out_path = per_dir / f"{stem}_{i:04d}_{int(t)}s.{fmt}"

            # Use force_keyframes for precise seeking if requested
            if force_keyframes:
                _extract_frame_precise(
                    ffmpeg=ffmpeg,
                    src=local_file,
                    t=t,
                    out_path=out_path,
                    width=width,
                    fmt=fmt,
                    add_timestamp=add_timestamps,
                    font=font,
                    font_size=font_size,
                    font_color=font_color,
                    box_color=box_color,
                    box_opacity=box_opacity,
                    position=position,
                    quality=quality,
                    strip_metadata=strip_metadata,
                )
            else:
                _extract_frame(
                    ffmpeg=ffmpeg,
                    src=local_file,
                    t=t,
                    out_path=out_path,
                    width=width,
                    fmt=fmt,
                    add_timestamp=add_timestamps,
                    font=font,
                    font_size=font_size,
                    font_color=font_color,
                    box_color=box_color,
                    box_opacity=box_opacity,
                    position=position,
                    quality=quality,
                    strip_metadata=strip_metadata,
                )
            paths.append(out_path)
            if not only_montage:
                lines.append(str(out_path))

            # Populate manifest data for each extracted frame
            if manifest_data is not None:
                manifest_data.append(
                    {
                        "input": inp,
                        "output": str(out_path),
                        "timestamp": round(t, 3),
                        "timestamp_formatted": _format_timestamp(t),
                        "frame_index": i,
                        "width": width,
                        "format": fmt,
                    }
                )

        grid = _parse_montage(montage)
        if grid:
            cap = grid[0] * grid[1]
            use = paths[:cap]
            montage_out = per_dir / f"{stem}_montage_{montage.strip().lower()}.{fmt}"
            if _try_montage(use, grid, montage_out, montage_bg, tile_spacing):
                lines.append(str(montage_out))
            else:
                lines.append(
                    "WARN: Neither ImageMagick nor GraphicsMagick montage was found; skipped montage"
                )

        if only_montage and grid:
            # Clean up intermediate frames
            for p in paths:
                try:
                    p.unlink()
                except OSError:
                    pass

        return lines
    finally:
        if temp_d:
            temp_d.cleanup()


def run(
    input: str,
    output_dir: str = "thumbnails",
    interval: float = 10,
    width: int = 320,
    format: str = "png",
    start: str = "00:00:00",
    end: str = "00:00:00",
    max_frames: int = 10,
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
    scene_threshold: float = 0.0,
    force_keyframes: bool = False,
    manifest: str = "",
    progress: bool = False,
) -> str:
    """Generate thumbnails from video files or URLs using ffmpeg."""
    inputs = _split_inputs(input)
    if not inputs:
        return "ERROR: input is empty"

    if interval <= 0:
        return "ERROR: interval must be positive"
    if max_frames < 1:
        return "ERROR: max_frames must be >= 1"
    if width < 16:
        return "ERROR: width too small"

    # Validate scene_threshold range
    if scene_threshold and not (0.1 <= scene_threshold <= 1.0):
        return "ERROR: scene_threshold must be between 0.1 and 1.0"

    fmt = format.lower().strip()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("png", "jpg", "webp"):
        return "ERROR: format must be png, jpg, or webp"

    try:
        ffmpeg = _which_or_die("ffmpeg")
        ffprobe = _which_or_die("ffprobe")
        start_s = _parse_time_to_seconds(start)
        end_s = _parse_time_to_seconds(end)
        _parse_montage(montage)  # validate early
    except (RuntimeError, ValueError) as e:
        return f"ERROR: {e}"

    out_dir = Path(output_dir).expanduser().resolve()
    if not _validate_sandbox(out_dir):
        return "ERROR: output_dir is outside the allowed sandbox"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_lines: List[str] = []
    manifest_data = [] if manifest else None

    # Parallel processing of multiple inputs
    with ThreadPoolExecutor(max_workers=min(4, len(inputs))) as executor:
        futures = [
            executor.submit(
                _process_one,
                one,
                out_dir,
                float(interval),
                int(width),
                fmt,
                start_s,
                end_s,
                int(max_frames),
                montage,
                montage_bg,
                int(tile_spacing),
                font,
                int(font_size),
                font_color,
                box_color,
                float(box_opacity),
                position,
                int(quality),
                bool(add_timestamps),
                bool(strip_metadata),
                bool(only_montage),
                ffmpeg,
                ffprobe,
                timestamps,
                percentages,
                keyframes,
                scene_threshold,
                force_keyframes,
                manifest_data,
                progress,
            )
            for one in inputs
        ]

        for future in futures:
            try:
                all_lines.extend(future.result())
            except subprocess.CalledProcessError as e:
                err = (e.stderr or e.stdout or str(e)).strip()
                return f"ERROR: ffmpeg failed: {err}"
            except Exception as e:
                return f"ERROR: {e}"

    # Write manifest if requested
    if manifest and manifest_data:
        import json

        manifest_path = out_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        all_lines.append(f"Manifest written to: {manifest_path}")

    return "\n".join(all_lines) if all_lines else "ERROR: no thumbnails generated"


def _cli() -> int:
    global _verbose
    p = argparse.ArgumentParser(description="Generate video thumbnails.")
    p.add_argument("--input", action="append", required=True, dest="inputs")
    p.add_argument("--output_dir", default="thumbnails")
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--width", type=int, default=320)
    p.add_argument("--format", choices=("png", "jpg", "jpeg", "webp"), default="png")
    p.add_argument("--start", default="00:00:00")
    p.add_argument("--end", default="00:00:00")
    p.add_argument("--max_frames", type=int, default=10)
    p.add_argument(
        "--timestamps",
        default="",
        help="Comma-separated specific timestamps (HH:MM:SS or seconds) to capture",
    )
    p.add_argument(
        "--percentages",
        default="",
        help="Comma-separated percentages (0-100) of video duration to capture",
    )
    p.add_argument(
        "--keyframes",
        action="store_true",
        dest="keyframes",
        help="Capture at keyframes only (I-frames) within time range",
    )
    p.add_argument(
        "--scene-threshold",
        type=float,
        default=0.0,
        dest="scene_threshold",
        help="Scene change detection threshold (0.1-1.0)",
    )
    p.add_argument(
        "--force-keyframes",
        action="store_true",
        dest="force_keyframes",
        help="Force keyframes at exact timestamps (re-encodes for precise seeking)",
    )
    p.add_argument(
        "--manifest", default="", help="Output JSON manifest with capture details"
    )
    p.add_argument(
        "--progress", action="store_true", help="Show progress bar for long operations"
    )
    p.add_argument("--montage", default="")
    p.add_argument("--montage-bg", default="white", dest="montage_bg")
    p.add_argument("--tile-spacing", type=int, default=2, dest="tile_spacing")
    p.add_argument("--font", default=None)
    p.add_argument("--font-size", type=int, default=14, dest="font_size")
    p.add_argument("--font-color", default="white", dest="font_color")
    p.add_argument("--box-color", default="black", dest="box_color")
    p.add_argument("--box-opacity", type=float, default=0.5, dest="box_opacity")
    p.add_argument("--position", choices=("tl", "tr", "bl", "br"), default="bl")
    p.add_argument("--quality", type=int, default=80)
    p.add_argument("--add-timestamps", action="store_true", dest="add_timestamps")
    p.add_argument("--strip-metadata", action="store_true", dest="strip_metadata")
    p.add_argument("--only-montage", action="store_true", dest="only_montage")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    _verbose = args.verbose
    merged = ",".join(args.inputs)
    result = run(
        input=merged,
        output_dir=args.output_dir,
        interval=args.interval,
        width=args.width,
        format=args.format,
        start=args.start,
        end=args.end,
        max_frames=args.max_frames,
        montage=args.montage,
        montage_bg=args.montage_bg,
        tile_spacing=args.tile_spacing,
        font=args.font,
        font_size=args.font_size,
        font_color=args.font_color,
        box_color=args.box_color,
        box_opacity=args.box_opacity,
        position=args.position,
        quality=args.quality,
        add_timestamps=args.add_timestamps,
        strip_metadata=args.strip_metadata,
        only_montage=args.only_montage,
        timestamps=args.timestamps,
        percentages=args.percentages,
        keyframes=args.keyframes,
        scene_threshold=args.scene_threshold,
        force_keyframes=args.force_keyframes,
        manifest=args.manifest,
        progress=args.progress,
    )
    print(result)
    return 0 if not result.startswith("ERROR:") else 1


if __name__ == "__main__":
    sys.exit(_cli())
