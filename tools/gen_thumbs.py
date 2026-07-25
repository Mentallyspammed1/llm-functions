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
# @option --montage               Optional montage grid, e.g. 2x3 (requires ImageMagick or GraphicsMagick).
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
# @flag   --only-montage          If set, clean up individual frames and only keep the montage grid.
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

TIME_RE = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d+))?$"
)

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


def _parse_time_to_seconds(s: str) -> float:
    s = (s or "").strip()
    if not s or s == "00:00:00":
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
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


def _compute_timestamps(
    duration: float,
    start: float,
    end: float,
    interval: float,
    max_frames: int,
) -> List[float]:
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
    return times[:max_frames]


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
    with urllib.request.urlopen(req, timeout=30.0) as response, open(target, "wb") as out_file:
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
        times = _compute_timestamps(duration, start_s, end_s, interval, max_frames)

        paths: List[Path] = []
        lines: List[str] = []
        for i, t in enumerate(times):
            out_path = per_dir / f"{stem}_{i:04d}_{int(t)}s.{fmt}"
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

        grid = _parse_montage(montage)
        if grid:
            cap = grid[0] * grid[1]
            use = paths[:cap]
            montage_out = per_dir / f"{stem}_montage_{montage.strip().lower()}.{fmt}"
            if _try_montage(use, grid, montage_out, montage_bg, tile_spacing):
                lines.append(str(montage_out))
            else:
                lines.append("WARN: Neither ImageMagick nor GraphicsMagick montage was found; skipped montage")

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
    image_format: str = "png",
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

    fmt = image_format.lower().strip()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("png", "jpg", "webp"):
        return "ERROR: image_format must be png, jpg, or webp"

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
        image_format=args.format,
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
    )
    print(result)
    return 0 if not result.startswith("ERROR:") else 1


if __name__ == "__main__":
    sys.exit(_cli())
