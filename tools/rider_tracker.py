#!/usr/bin/env python3
# ==============================================================================
# bike_tracker.py — AIChat Pro Bike Trek Recorder & Telemetry Cockpit v3.5.0
# argc/aichat compatible · HUD Terminal UI · Power Model · GPX/TCX/GeoJSON Export
#
# @describe Track cycling activities, GPS pings, power watts, incline grade, heart rate zones, lap splits, and export to Strava/Garmin GPX/TCX.
#
# @meta require-tools aichat
#
# @option --action <ACTION>              Action: start, ping, pause, resume, stop, status, list, stats, export, delete (default: status)
# @option --title <TEXT>                 Ride session title / name (e.g. 'Morning Gravel Climb')
# @option --ride-id <ID>                 Specific ride ID (defaults to active session)
# @option --lat <FLOAT>                  Latitude coordinate (-90.0 to 90.0)
# @option --lon <FLOAT>                  Longitude coordinate (-180.0 to 180.0)
# @option --elevation <FLOAT>            Elevation in meters (or feet if imperial)
# @option --speed <FLOAT>                Instantaneous speed in km/h (or mph if imperial)
# @option --heart-rate <NUM>             Rider heart rate in BPM
# @option --cadence <NUM>                Pedaling cadence in RPM
# @option --battery <NUM>                Device/sensor battery percentage (0-100)
# @option --bike <TEXT>                  Bike profile (e.g. 'Road', 'Gravel', 'MTB', 'E-Bike')
# @option --notes <TEXT>                 Waypoint notes or trail conditions
# @option --format <FORMAT>              Export format: gpx, tcx, geojson, csv, json (default: gpx)
# @option --units <UNITS>                Unit system: metric or imperial (default: metric)
# @option --mode <MODE>                  Execution mode: summary or detailed (default: summary)
# @flag   --auto-gps                     Auto-acquire coordinates from hardware (Termux/Device GPS)
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
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

__version__ = "3.5.0"
__all__ = [
    "run",
    "execute_tool",
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


class ActionType(str, Enum):
    START = "start"
    PING = "ping"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    STATUS = "status"
    LIST = "list"
    STATS = "stats"
    EXPORT = "export"
    DELETE = "delete"


class ExportFormat(str, Enum):
    GPX = "gpx"
    TCX = "tcx"
    GEOJSON = "geojson"
    CSV = "csv"
    JSON = "json"


class ToolError(Exception):
    """Structured exception model for bike tracker operations."""

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
    """Zero-crash JSON encoder supporting Path, Enum, datetime, and collections."""

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
# SECTION 2: Terminal Color Palette & Visual HUD Cockpit
# ==============================================================================

NEON_CYAN    = "\033[38;5;51m"
NEON_GREEN   = "\033[38;5;46m"
NEON_RED     = "\033[38;5;196m"
NEON_YELLOW  = "\033[38;5;226m"
NEON_PURPLE  = "\033[38;5;129m"
NEON_PINK    = "\033[38;5;198m"
NEON_ORANGE  = "\033[38;5;208m"
NEON_BLUE    = "\033[38;5;39m"
RESET        = "\033[0m"
BOLD         = "\033[1m"
DIM          = "\033[2m"

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\033\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stderr.isatty() and os.environ.get("TERM", "").lower() not in ("dumb", "")


def _cprint(text: str, file: Any = None, no_color: bool = False, end: str = "\n") -> None:
    target = file or sys.stderr
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def _format_seconds(seconds: float) -> str:
    """Convert seconds to standard HH:MM:SS format."""
    total = int(max(0, seconds))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def _render_ascii_sparkline(values: list[float], width: int = 36) -> str:
    """Generate a clean Unicode sparkline profile ( ▂▃▄▅▆▇█)."""
    if not values or len(values) < 2:
        return f"{DIM}insufficient data{RESET}"

    bars = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    
    # Resample values down to requested visual width
    if len(values) > width:
        step = len(values) / float(width)
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values

    min_val, max_val = min(sampled), max(sampled)
    span = max_val - min_val
    if span <= 0.001:
        return f"{NEON_CYAN}{bars[0] * len(sampled)}{RESET}"

    rendered = ""
    for v in sampled:
        idx = int(((v - min_val) / span) * (len(bars) - 1))
        idx = max(0, min(len(bars) - 1, idx))
        rendered += bars[idx]

    return f"{NEON_CYAN}{rendered}{RESET}"


def _render_hr_zone_bar(zone: Optional[int]) -> str:
    """Format colorized Heart Rate training zone badge."""
    if not zone:
        return f"{DIM}Z--{RESET}"
    colors = {
        1: (NEON_BLUE, "Z1 Recovery"),
        2: (NEON_GREEN, "Z2 Endurance"),
        3: (NEON_YELLOW, "Z3 Tempo"),
        4: (NEON_ORANGE, "Z4 Threshold"),
        5: (NEON_RED, "Z5 Anaerobic"),
    }
    col, label = colors.get(zone, (RESET, f"Z{zone}"))
    return f"{col}{BOLD}{label}{RESET}"


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    """Render telemetry cockpit HUD interface to stderr."""
    if not _is_tty() or no_color:
        return

    box_w = 72
    border = "─" * box_w
    action = data.get("action", "unknown")
    success = data.get("success", False)

    if not success:
        _cprint(f"{NEON_RED}╭{border}╮{RESET}")
        _cprint(f"{NEON_RED}│{RESET} {NEON_RED}{BOLD}✗ COCKPIT OPERATION FAILED{RESET}")
        _cprint(f"{NEON_RED}├{border}┤{RESET}")
        _cprint(f"{NEON_RED}│{RESET} {NEON_RED}Error:{RESET} {data.get('error', 'Unknown error')}")
        _cprint(f"{NEON_RED}╰{border}╯{RESET}")
        return

    # LIFETIME STATS UI
    if action == "stats":
        st = data.get("stats", {})
        u = data.get("units", {})
        _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}🏆 [LIFETIME CYCLING CAREER TOTALS]{RESET}")
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Total Activities:{RESET}  {BOLD}{st.get('total_rides', 0)}{RESET} rides")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Total Distance:{RESET}    {NEON_YELLOW}{BOLD}{st.get('total_distance', 0):.2f} {u.get('distance')}{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Total Moving Time:{RESET} {BOLD}{_format_seconds(st.get('total_moving_time_sec', 0))}{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Total Elevation:{RESET}   {NEON_GREEN}+{st.get('total_elevation_gain', 0):.0f} {u.get('elevation')}{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Total Energy:{RESET}      {NEON_ORANGE}{st.get('total_calories', 0):,} kcal{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Longest Single:{RESET}    {st.get('longest_ride_dist', 0):.2f} {u.get('distance')}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}All-Time Max Spd:{RESET}  {st.get('all_time_max_speed', 0):.1f} {u.get('speed')}")
        _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")
        return

    # LIST ACTION UI
    if action == "list":
        rides = data.get("rides", [])
        _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}🚴 [RECORDED RIDE HISTORY]{RESET} ({len(rides)} Activities)")
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        if not rides:
            _cprint(f"{NEON_PURPLE}│{RESET} {DIM}No activities recorded yet. Run --action start to begin.{RESET}")
        else:
            for r in rides:
                st = r.get("status", "COMPLETED").upper()
                st_color = NEON_GREEN if st == "RECORDING" else (NEON_YELLOW if st == "PAUSED" else DIM)
                dist = f"{r.get('total_distance', 0):.2f} {data.get('units', {}).get('distance', 'km')}"
                dur = _format_seconds(r.get("moving_time_seconds", 0))
                title = r.get("title", "Untitled")[:24]
                rid = r.get("ride_id", "")
                _cprint(f"{NEON_PURPLE}│{RESET} {st_color}●{RESET} {BOLD}{title:<24}{RESET} [{dist:>9} | {dur}] ID: {DIM}{rid}{RESET}")
        _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")
        return

    # EXPORT ACTION UI
    if action == "export":
        _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}💾 [ACTIVITY EXPORTED SUCCESSFULLY]{RESET}")
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Format:{RESET}       {BOLD}{data.get('format', '').upper()}{RESET} (Compatible with Strava/Garmin)")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Export File:{RESET}  {NEON_GREEN}{data.get('export_file', 'N/A')}{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Waypoints:{RESET}    {data.get('point_count', 0)} GPS coordinates")
        _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")
        return

    # RIDE COCKPIT HUD (START, PING, STATUS, STOP, PAUSE, RESUME)
    ride = data.get("ride", {})
    stats = ride.get("stats", {})
    status = ride.get("status", "STOPPED").upper()
    status_color = NEON_GREEN if status == "RECORDING" else (NEON_YELLOW if status == "PAUSED" else NEON_RED)

    units = data.get("units", {})
    u_dist = units.get("distance", "km")
    u_speed = units.get("speed", "km/h")
    u_elev = units.get("elevation", "m")
    u_pace = units.get("pace", "min/km")

    dist_val = stats.get("distance", 0.0)
    avg_speed = stats.get("avg_speed", 0.0)
    max_speed = stats.get("max_speed", 0.0)
    curr_speed = stats.get("current_speed", 0.0)
    curr_grade = stats.get("current_grade_pct", 0.0)
    max_grade = stats.get("max_grade_pct", 0.0)
    est_watts = stats.get("current_watts", 0)
    avg_watts = stats.get("avg_watts", 0)
    elev_gain = stats.get("elevation_gain", 0.0)
    calories = stats.get("calories", 0)

    elapsed_str = _format_seconds(stats.get("elapsed_time_seconds", 0))
    moving_str = _format_seconds(stats.get("moving_time_seconds", 0))
    pace_str = stats.get("current_pace_str", "--:--")

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}🚴 [PRO CYCLING COCKPIT v{__version__}]{RESET} Status: {status_color}{BOLD}● {status}{RESET}")
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Activity:{RESET}       {BOLD}{ride.get('title', 'Ride Session')}{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Bike Profile:{RESET}   {ride.get('bike_profile', 'Standard Bike')}  ·  ID: {DIM}{ride.get('ride_id')}{RESET}")
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}DYNAMICS & SPEED:{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Distance:{RESET}     {NEON_YELLOW}{BOLD}{dist_val:.2f} {u_dist}{RESET}       {NEON_CYAN}› Current Speed:{RESET}  {NEON_GREEN}{BOLD}{curr_speed:.1f} {u_speed}{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Moving Time:{RESET}  {moving_str}            {NEON_CYAN}› Avg / Max Spd:{RESET}  {avg_speed:.1f} / {max_speed:.1f} {u_speed}")
    _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Total Elapsed:{RESET}{elapsed_str}            {NEON_CYAN}› Current Pace:{RESET}   {pace_str} {u_pace}")
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}CLIMBING & POWER (ESTIMATED):{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Road Gradient:{RESET}{NEON_ORANGE}{curr_grade:+.1f}%{RESET} (Max: {max_grade:+.1f}%) {NEON_CYAN}› Elev Gain/Loss:{RESET}+{elev_gain:.0f}{u_elev} / -{stats.get('elevation_loss', 0):.0f}{u_elev}")
    _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Power Output:{RESET} {NEON_YELLOW}{est_watts} W{RESET} (Avg: {avg_watts} W)   {NEON_CYAN}› Energy Burned:{RESET}  {NEON_ORANGE}{calories:,} kcal{RESET}")

    # Biometric / Cadence Sensors
    hr = stats.get("heart_rate")
    cad = stats.get("cadence")
    zone_badge = _render_hr_zone_bar(stats.get("hr_zone"))
    if hr or cad:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}BIOMETRIC TELEMETRY:{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Heart Rate:{RESET}   {NEON_RED}{hr or '--'} BPM{RESET} ({zone_badge})   {NEON_CYAN}› Cadence:{RESET}        {NEON_BLUE}{cad or '--'} RPM{RESET}")

    # ASCII Elevation Sparkline
    elev_history = [p.get("elevation_m", 0.0) for p in ride.get("pings", []) if p.get("elevation_m") is not None]
    if len(elev_history) >= 3:
        sparkline = _render_ascii_sparkline(elev_history, width=38)
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Elevation Profile:{RESET} [{sparkline}]")

    # Lap Splits Preview (if detailed mode or multiple splits)
    splits = ride.get("splits", [])
    if splits and (data.get("mode") == "detailed" or len(splits) <= 4):
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Auto-Lap Splits (1 {u_dist}):{RESET}")
        for s in splits[-3:]:
            s_num = s.get("split_number")
            s_dur = _format_seconds(s.get("duration_seconds", 0))
            s_spd = s.get("avg_speed", 0.0)
            s_climb = s.get("elev_gain", 0.0)
            _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}› Split {s_num}:{RESET} {s_dur} ({s_spd:.1f} {u_speed}) · Climb: +{s_climb:.0f}{u_elev}")

    # Latest GPS Waypoint
    latest_ping = ride.get("latest_ping")
    if latest_ping:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        notes = f" · Note: \"{latest_ping.get('notes')}\"" if latest_ping.get("notes") else ""
        batt = f" · Batt: {latest_ping.get('battery')}%" if latest_ping.get("battery") is not None else ""
        _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Latest GPS:{RESET} {latest_ping.get('lat'):.5f}, {latest_ping.get('lon'):.5f} @ {latest_ping.get('time', '')[-8:]}{batt}{notes}")

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")


# ==============================================================================
# SECTION 3: Agent & Environment Helpers
# ==============================================================================

def get_agent_var(name: str, default: str = "") -> str:
    """Access agent user-defined variables (LLM_AGENT_VAR_<NAME>)."""
    return os.environ.get(f"LLM_AGENT_VAR_{name.upper()}", default)


def get_builtin_var(name: str) -> Optional[str]:
    """Access agent built-in environment variables."""
    return os.environ.get(f"LLM_AGENT_VAR_{name}")


def get_execution_context() -> dict[str, Any]:
    """Extract runtime execution context."""
    termux_prefix = os.environ.get("PREFIX", "")
    return {
        "tool_name": os.environ.get("LLM_TOOL_NAME", "bike_tracker"),
        "cache_dir": os.environ.get("LLM_TOOL_CACHE_DIR"),
        "root_dir": os.environ.get("LLM_ROOT_DIR"),
        "output_path": os.environ.get("LLM_OUTPUT"),
        "cwd": get_builtin_var("__cwd__") or os.getcwd(),
        "python_version": sys.version.split()[0],
        "platform": platform.system().lower(),
        "is_termux": "com.termux" in termux_prefix or Path("/data/data/com.termux").exists(),
    }


# ==============================================================================
# SECTION 4: Storage Manager & Data Persistence
# ==============================================================================

class BikeStorage:
    """Persistent activity engine for bike telemetry sessions."""

    def __init__(self) -> None:
        if "LLM_TOOL_CACHE_DIR" in os.environ:
            base_dir = Path(os.environ["LLM_TOOL_CACHE_DIR"]) / "bike_tracker"
        else:
            base_dir = Path.home() / ".local" / "share" / "aichat_bike_tracker"

        self.storage_dir = base_dir / "rides"
        self.state_file = base_dir / "active_ride.json"

        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.storage_dir = Path("/tmp/aichat_bike_tracker/rides")
            self.state_file = Path("/tmp/aichat_bike_tracker/active_ride.json")
            self.storage_dir.mkdir(parents=True, exist_ok=True)

    def get_active_ride_id(self) -> Optional[str]:
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return data.get("active_ride_id")
        except Exception:
            return None

    def set_active_ride_id(self, ride_id: Optional[str]) -> None:
        try:
            if ride_id is None:
                if self.state_file.exists():
                    self.state_file.unlink(missing_ok=True)
            else:
                with open(self.state_file, "w", encoding="utf-8") as fp:
                    json.dump({"active_ride_id": ride_id, "updated_at": datetime.now(timezone.utc).isoformat()}, fp)
        except Exception as err:
            logging.debug(f"Failed to update active state: {err}")

    def load_ride(self, ride_id: str) -> dict[str, Any]:
        ride_path = self.storage_dir / f"{ride_id}.json"
        if not ride_path.exists():
            raise ToolError(f"Ride activity '{ride_id}' not found.", exit_code=EXIT_FILE_NOT_FOUND)
        try:
            with open(ride_path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception as err:
            raise ToolError(f"Failed to read ride data for '{ride_id}': {err}", exit_code=EXIT_ERROR)

    def save_ride(self, ride_data: dict[str, Any]) -> None:
        ride_id = ride_data.get("ride_id")
        if not ride_id:
            raise ToolError("Cannot save ride without a valid ride_id.", exit_code=EXIT_INVALID_INPUT)
        ride_path = self.storage_dir / f"{ride_id}.json"
        tmp_path = ride_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fp:
                json.dump(ride_data, fp, indent=2, cls=ToolJSONEncoder)
            tmp_path.replace(ride_path)
        except Exception as err:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise ToolError(f"Failed saving ride '{ride_id}': {err}", exit_code=EXIT_ERROR)

    def list_all_rides(self, is_imperial: bool = False) -> list[dict[str, Any]]:
        rides = []
        for file in sorted(self.storage_dir.glob("*.json"), reverse=True):
            try:
                with open(file, "r", encoding="utf-8") as fp:
                    r = json.load(fp)
                    stats = r.get("stats", {})
                    dist_km = stats.get("distance", 0.0)
                    dist_disp = dist_km * 0.621371 if is_imperial else dist_km
                    rides.append({
                        "ride_id": r.get("ride_id"),
                        "title": r.get("title", "Untitled"),
                        "status": r.get("status", "STOPPED"),
                        "start_time": r.get("start_time"),
                        "total_distance": round(dist_disp, 2),
                        "moving_time_seconds": stats.get("moving_time_seconds", 0.0),
                        "pings_count": len(r.get("pings", [])),
                    })
            except Exception:
                continue
        return rides

    def delete_ride(self, ride_id: str) -> bool:
        ride_path = self.storage_dir / f"{ride_id}.json"
        if ride_path.exists():
            ride_path.unlink(missing_ok=True)
            if self.get_active_ride_id() == ride_id:
                self.set_active_ride_id(None)
            return True
        return False


# ==============================================================================
# SECTION 5: Math, Telemetry, Power Physics & Exporters
# ==============================================================================

def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between coordinates in kilometers."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def estimate_cycling_power_watts(
    speed_kmh: float,
    grade_pct: float,
    total_mass_kg: float = 85.0,
    c_rr: float = 0.0045,
    c_d_a: float = 0.388,
) -> int:
    """Physics-based mechanical power model for cycling (gravity + rolling + aero resistance)."""
    if speed_kmh <= 1.0:
        return 0

    v = speed_kmh / 3.6  # convert km/h to m/s
    g = 9.80665
    rho = 1.225  # standard air density kg/m^3

    theta = math.atan(grade_pct / 100.0)
    p_gravity = total_mass_kg * g * v * math.sin(theta)
    p_rolling = c_rr * total_mass_kg * g * v * math.cos(theta)
    p_aero = 0.5 * c_d_a * rho * (v ** 3)
    p_drivetrain_eff = 0.95

    raw_watts = (p_gravity + p_rolling + p_aero) / p_drivetrain_eff
    return int(max(0, min(2000, raw_watts)))


def compute_hr_zone(hr: Optional[int], max_hr: int = 190) -> Optional[int]:
    """Calculate HR Zone 1 to 5 based on percentage of max HR."""
    if not hr or hr <= 30:
        return None
    pct = (hr / max_hr) * 100.0
    if pct < 60:
        return 1
    elif pct < 70:
        return 2
    elif pct < 80:
        return 3
    elif pct < 90:
        return 4
    return 5


def acquire_hardware_gps() -> Optional[dict[str, Any]]:
    """Poll hardware GPS on Android/Termux using termux-location if available."""
    termux_loc_bin = shutil.which("termux-location")
    if not termux_loc_bin:
        return None

    try:
        proc = subprocess.run(
            [termux_loc_bin, "-p", "gps", "-r", "last"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = json.loads(proc.stdout)
            lat = raw.get("latitude")
            lon = raw.get("longitude")
            if lat is not None and lon is not None:
                speed_ms = raw.get("speed", 0.0) or 0.0
                return {
                    "lat": float(lat),
                    "lon": float(lon),
                    "elevation_m": float(raw.get("altitude", 0.0)) if raw.get("altitude") is not None else None,
                    "speed_kmh": round(speed_ms * 3.6, 2),
                    "bearing": raw.get("bearing"),
                }
    except Exception as err:
        logging.debug(f"Hardware GPS acquisition failed: {err}")
    return None


def calculate_ride_telemetry(ride: dict[str, Any], is_imperial: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute comprehensive dynamics, road gradients, wattage, HR zones, and 1km/1mi lap splits."""
    pings = ride.get("pings", [])
    start_iso = ride.get("start_time")
    now_iso = datetime.now(timezone.utc).isoformat()
    end_iso = ride.get("end_time") or now_iso

    try:
        t_start = datetime.fromisoformat(start_iso).timestamp()
        t_end = datetime.fromisoformat(end_iso).timestamp()
        total_elapsed = max(0.0, t_end - t_start)
    except Exception:
        total_elapsed = 0.0

    pause_periods = ride.get("pause_intervals", [])
    total_paused = 0.0
    for p in pause_periods:
        p_start = datetime.fromisoformat(p["start"]).timestamp()
        p_end = datetime.fromisoformat(p.get("end") or now_iso).timestamp()
        total_paused += max(0.0, p_end - p_start)

    moving_time = max(0.0, total_elapsed - total_paused)

    rider_w = float(get_agent_var("RIDER_WEIGHT_KG", "75.0"))
    bike_w = float(get_agent_var("BIKE_WEIGHT_KG", "10.0"))
    max_hr = int(get_agent_var("MAX_HR", "190"))
    total_mass = rider_w + bike_w

    total_dist_km = 0.0
    elev_gain_m = 0.0
    elev_loss_m = 0.0
    max_speed_kmh = 0.0
    curr_speed_kmh = 0.0
    curr_grade_pct = 0.0
    max_grade_pct = 0.0
    watts_list: list[int] = []
    curr_watts = 0

    latest_hr = None
    latest_cadence = None

    # Splits tracking (1.0 unit buckets: 1 km or 1 mi)
    split_dist_target = 1.60934 if is_imperial else 1.0  # in km
    splits: list[dict[str, Any]] = []
    cur_split_start_idx = 0
    cur_split_start_dist = 0.0
    cur_split_start_time = t_start

    for i in range(len(pings)):
        curr = pings[i]

        if curr.get("speed_kmh") is not None:
            spd = float(curr["speed_kmh"])
            max_speed_kmh = max(max_speed_kmh, spd)
            curr_speed_kmh = spd

        if curr.get("heart_rate") is not None:
            latest_hr = curr["heart_rate"]
        if curr.get("cadence") is not None:
            latest_cadence = curr["cadence"]

        if i > 0:
            prev = pings[i - 1]
            d_km = haversine_distance_km(prev["lat"], prev["lon"], curr["lat"], curr["lon"])
            total_dist_km += d_km

            # Grade & Elevation delta
            if prev.get("elevation_m") is not None and curr.get("elevation_m") is not None:
                d_ele = float(curr["elevation_m"]) - float(prev["elevation_m"])
                if d_ele > 1.2:
                    elev_gain_m += d_ele
                elif d_ele < -1.2:
                    elev_loss_m += abs(d_ele)

                if d_km > 0.005:  # Calculate grade over minimum 5 meters
                    grade = (d_ele / (d_km * 1000.0)) * 100.0
                    curr_grade_pct = max(-25.0, min(30.0, grade))
                    max_grade_pct = max(max_grade_pct, curr_grade_pct)

            # Inferred speed if GPS speed tag was omitted
            if curr.get("speed_kmh") is None:
                try:
                    dt = datetime.fromisoformat(curr["time"]).timestamp() - datetime.fromisoformat(prev["time"]).timestamp()
                    if dt > 0:
                        inf_spd = d_km / (dt / 3600.0)
                        if inf_spd < 120.0:
                            max_speed_kmh = max(max_speed_kmh, inf_spd)
                            curr_speed_kmh = inf_spd
                except Exception:
                    pass

            # Calculate Instantaneous Wattage
            w = estimate_cycling_power_watts(curr_speed_kmh, curr_grade_pct, total_mass_kg=total_mass)
            watts_list.append(w)
            curr_watts = w

            # Split evaluation
            if (total_dist_km - cur_split_start_dist) >= split_dist_target:
                split_end_time = datetime.fromisoformat(curr["time"]).timestamp()
                split_duration = max(1.0, split_end_time - cur_split_start_time)
                split_dist_actual = total_dist_km - cur_split_start_dist
                split_dist_disp = split_dist_actual * 0.621371 if is_imperial else split_dist_actual
                split_avg_spd = (split_dist_disp / (split_duration / 3600.0))

                splits.append({
                    "split_number": len(splits) + 1,
                    "distance": round(split_dist_disp, 2),
                    "duration_seconds": round(split_duration, 1),
                    "avg_speed": round(split_avg_spd, 2),
                    "elev_gain": round(elev_gain_m if not splits else (elev_gain_m - sum(s.get("elev_gain_raw", 0) for s in splits)), 1),
                    "elev_gain_raw": elev_gain_m,
                })
                cur_split_start_idx = i
                cur_split_start_dist = total_dist_km
                cur_split_start_time = split_end_time

    avg_speed_kmh = (total_dist_km / (moving_time / 3600.0)) if moving_time > 10 else 0.0
    avg_watts = int(sum(watts_list) / len(watts_list)) if watts_list else 0

    # Metabolic Energy Expenditure (Calories)
    if avg_watts > 0:
        # 1 Joule = 1 Watt-second. Human gross mechanical efficiency ~22%
        total_joules = avg_watts * moving_time
        est_calories = int(total_joules / (4184 * 0.22))
    else:
        # Fallback to speed-based MET formula
        met = 4.0 if avg_speed_kmh < 15 else (7.0 if avg_speed_kmh < 20 else (10.0 if avg_speed_kmh < 26 else 12.5))
        est_calories = int(met * rider_w * (moving_time / 3600.0))

    # Pace calculations (min/km or min/mi)
    pace_seconds = (moving_time / (total_dist_km * 0.621371 if is_imperial else total_dist_km)) if total_dist_km > 0.05 else 0.0
    pace_str = f"{int(pace_seconds // 60):02d}:{int(pace_seconds % 60):02d}" if pace_seconds > 0 else "--:--"

    # Unit Conversions
    dist_final = total_dist_km * 0.621371 if is_imperial else total_dist_km
    avg_spd_final = avg_speed_kmh * 0.621371 if is_imperial else avg_speed_kmh
    max_spd_final = max_speed_kmh * 0.621371 if is_imperial else max_speed_kmh
    curr_spd_final = curr_speed_kmh * 0.621371 if is_imperial else curr_speed_kmh
    elev_gain_final = elev_gain_m * 3.28084 if is_imperial else elev_gain_m
    elev_loss_final = elev_loss_m * 3.28084 if is_imperial else elev_loss_m

    stats_dict = {
        "distance": round(dist_final, 3),
        "moving_time_seconds": round(moving_time, 1),
        "elapsed_time_seconds": round(total_elapsed, 1),
        "avg_speed": round(avg_spd_final, 2),
        "max_speed": round(max_spd_final, 2),
        "current_speed": round(curr_spd_final, 2),
        "current_pace_str": pace_str,
        "current_grade_pct": round(curr_grade_pct, 1),
        "max_grade_pct": round(max_grade_pct, 1),
        "current_watts": curr_watts,
        "avg_watts": avg_watts,
        "elevation_gain": round(elev_gain_final, 1),
        "elevation_loss": round(elev_loss_final, 1),
        "calories": est_calories,
        "heart_rate": latest_hr,
        "hr_zone": compute_hr_zone(latest_hr, max_hr=max_hr),
        "cadence": latest_cadence,
    }
    return stats_dict, splits


# ------------------------------------------------------------------------------
# Exporters (GPX 1.1 + TrackPointExtensions, TCX, GeoJSON, CSV)
# ------------------------------------------------------------------------------

def generate_gpx(ride: dict[str, Any]) -> str:
    """Generate Strava/Garmin-compatible GPX 1.1 with TrackPointExtension (HR, Cadence, Speed) [INDEX: 1, 2]."""
    title = ride.get("title", "Bike Ride")
    start = ride.get("start_time", "")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="AIChat Pro Bike Tracker" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        '  <metadata>',
        f'    <name>{title}</name>',
        f'    <time>{start}</time>',
        '  </metadata>',
        '  <trk>',
        f'    <name>{title}</name>',
        '    <type>cycling</type>',
        '    <trkseg>',
    ]
    for p in ride.get("pings", []):
        lat, lon = p.get("lat"), p.get("lon")
        ele = p.get("elevation_m")
        t = p.get("time")
        ele_tag = f"<ele>{ele:.1f}</ele>" if ele is not None else ""

        ext_inner = []
        if p.get("heart_rate") is not None:
            ext_inner.append(f"<gpxtpx:hr>{p['heart_rate']}</gpxtpx:hr>")
        if p.get("cadence") is not None:
            ext_inner.append(f"<gpxtpx:cad>{p['cadence']}</gpxtpx:cad>")
        if p.get("speed_kmh") is not None:
            ext_inner.append(f"<gpxtpx:speed>{p['speed_kmh'] / 3.6:.2f}</gpxtpx:speed>")

        ext_tag = f"<extensions><gpxtpx:TrackPointExtension>{''.join(ext_inner)}</gpxtpx:TrackPointExtension></extensions>" if ext_inner else ""
        lines.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">{ele_tag}<time>{t}</time>{ext_tag}</trkpt>')

    lines.extend(['    </trkseg>', '  </trk>', '</gpx>'])
    return "\n".join(lines)


def generate_tcx(ride: dict[str, Any]) -> str:
    """Generate Training Center XML (TCX) for Garmin and training platforms."""
    title = ride.get("title", "Bike Ride")
    start = ride.get("start_time", "")
    stats = ride.get("stats", {})
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">',
        '  <Activities>',
        '    <Activity Sport="Biking">',
        f'      <Id>{start}</Id>',
        f'      <Lap StartTime="{start}">',
        f'        <TotalTimeSeconds>{stats.get("moving_time_seconds", 0.0)}</TotalTimeSeconds>',
        f'        <DistanceMeters>{stats.get("distance", 0.0) * 1000.0}</DistanceMeters>',
        f'        <Calories>{stats.get("calories", 0)}</Calories>',
        '        <Intensity>Active</Intensity>',
        '        <TriggerMethod>Manual</TriggerMethod>',
        '        <Track>',
    ]
    for p in ride.get("pings", []):
        lines.append('          <Trackpoint>')
        lines.append(f'            <Time>{p.get("time")}</Time>')
        lines.append('            <Position>')
        lines.append(f'              <LatitudeDegrees>{p.get("lat"):.6f}</LatitudeDegrees>')
        lines.append(f'              <LongitudeDegrees>{p.get("lon"):.6f}</LongitudeDegrees>')
        lines.append('            </Position>')
        if p.get("elevation_m") is not None:
            lines.append(f'            <AltitudeMeters>{p.get("elevation_m"):.1f}</AltitudeMeters>')
        if p.get("heart_rate") is not None:
            lines.append(f'            <HeartRateBpm><Value>{p["heart_rate"]}</Value></HeartRateBpm>')
        if p.get("cadence") is not None:
            lines.append(f'            <Cadence>{p["cadence"]}</Cadence>')
        lines.append('          </Trackpoint>')
    lines.extend(['        </Track>', '      </Lap>', '    </Activity>', '  </Activities>', '</TrainingCenterDatabase>'])
    return "\n".join(lines)


def generate_geojson(ride: dict[str, Any]) -> dict[str, Any]:
    """Generate GeoJSON FeatureCollection."""
    coords = []
    for p in ride.get("pings", []):
        ele = p.get("elevation_m", 0.0) or 0.0
        coords.append([p["lon"], p["lat"], ele])

    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "title": ride.get("title"),
                "ride_id": ride.get("ride_id"),
                "bike": ride.get("bike_profile"),
                "stats": ride.get("stats"),
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        }],
    }


def generate_csv(ride: dict[str, Any]) -> str:
    """Generate tabular CSV format."""
    lines = ["time,latitude,longitude,elevation_m,speed_kmh,heart_rate,cadence,notes"]
    for p in ride.get("pings", []):
        note = (p.get("notes") or "").replace(",", ";")
        lines.append(f"{p.get('time')},{p.get('lat')},{p.get('lon')},{p.get('elevation_m', '')},{p.get('speed_kmh', '')},{p.get('heart_rate', '')},{p.get('cadence', '')},{note}")
    return "\n".join(lines)


# ==============================================================================
# SECTION 6: Action Execution Dispatcher
# ==============================================================================

def execute_tool(
    action: str = "status",
    title: Optional[str] = None,
    ride_id: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    elevation: Optional[float] = None,
    speed: Optional[float] = None,
    heart_rate: Optional[int] = None,
    cadence: Optional[int] = None,
    battery: Optional[int] = None,
    bike: Optional[str] = None,
    notes: Optional[str] = None,
    format: str = "gpx",
    units: str = "metric",
    mode: str = "summary",
    auto_gps: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Core dispatcher and telemetry processing engine."""
    start_time = time.monotonic()
    storage = BikeStorage()
    is_imperial = units.lower() in ("imperial", "us", "mph", "miles")
    action_norm = action.strip().lower()

    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="[DEBUG] %(message)s")
        logging.debug(f"Action={action_norm}, Title={title}, RideID={ride_id}, AutoGPS={auto_gps}")

    units_spec = {
        "distance": "mi" if is_imperial else "km",
        "speed": "mph" if is_imperial else "km/h",
        "elevation": "ft" if is_imperial else "m",
        "pace": "min/mi" if is_imperial else "min/km",
    }

    try:
        # ----------------------------------------------------------------------
        # ACTION: STATS (LIFETIME CAREER TOTALS)
        # ----------------------------------------------------------------------
        if action_norm == ActionType.STATS.value:
            all_rides = storage.list_all_rides(is_imperial=is_imperial)
            tot_dist = 0.0
            tot_moving_sec = 0.0
            tot_gain = 0.0
            tot_cal = 0
            longest_dist = 0.0
            all_time_max_spd = 0.0

            for r_meta in all_rides:
                try:
                    full_r = storage.load_ride(r_meta["ride_id"])
                    st = full_r.get("stats", {})
                    d = st.get("distance", 0.0)
                    tot_dist += d
                    longest_dist = max(longest_dist, d)
                    tot_moving_sec += st.get("moving_time_seconds", 0.0)
                    tot_gain += st.get("elevation_gain", 0.0)
                    tot_cal += st.get("calories", 0)
                    all_time_max_spd = max(all_time_max_spd, st.get("max_speed", 0.0))
                except Exception:
                    continue

            return {
                "success": True,
                "action": "stats",
                "units": units_spec,
                "stats": {
                    "total_rides": len(all_rides),
                    "total_distance": round(tot_dist, 2),
                    "total_moving_time_sec": round(tot_moving_sec, 1),
                    "total_elevation_gain": round(tot_gain, 1),
                    "total_calories": tot_cal,
                    "longest_ride_dist": round(longest_dist, 2),
                    "all_time_max_speed": round(all_time_max_spd, 2),
                },
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: LIST
        # ----------------------------------------------------------------------
        if action_norm == ActionType.LIST.value:
            rides = storage.list_all_rides(is_imperial=is_imperial)
            return {
                "success": True,
                "action": "list",
                "count": len(rides),
                "active_ride_id": storage.get_active_ride_id(),
                "units": units_spec,
                "rides": rides,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: START
        # ----------------------------------------------------------------------
        if action_norm == ActionType.START.value:
            active_id = storage.get_active_ride_id()
            if active_id:
                return {
                    "success": False,
                    "error": f"Session '{active_id}' is already recording. Stop it with --action stop first.",
                    "exit_code": EXIT_INVALID_INPUT,
                    "active_ride_id": active_id,
                }

            # Hardware GPS auto-polling
            if auto_gps and (lat is None or lon is None):
                hw = acquire_hardware_gps()
                if hw:
                    lat, lon = hw["lat"], hw["lon"]
                    if elevation is None and hw.get("elevation_m") is not None:
                        elevation = hw["elevation_m"]
                    if speed is None and hw.get("speed_kmh") is not None:
                        speed = hw["speed_kmh"]

            now_dt = datetime.now(timezone.utc)
            new_id = f"ride_{now_dt.strftime('%Y%m%d_%H%M%S')}"
            bike_profile = bike or get_agent_var("BIKE_TYPE", "Road Bike")
            session_title = title or f"Ride {now_dt.strftime('%b %d, %Y %H:%M')}"

            pings = []
            if lat is not None and lon is not None:
                elev_m = (elevation / 3.28084) if (elevation is not None and is_imperial) else elevation
                spd_kmh = (speed / 0.621371) if (speed is not None and is_imperial) else speed
                pings.append({
                    "time": now_dt.isoformat(),
                    "lat": float(lat),
                    "lon": float(lon),
                    "elevation_m": elev_m,
                    "speed_kmh": spd_kmh,
                    "heart_rate": heart_rate,
                    "cadence": cadence,
                    "battery": battery,
                    "notes": notes,
                })

            ride_data = {
                "ride_id": new_id,
                "title": session_title,
                "bike_profile": bike_profile,
                "status": "RECORDING",
                "start_time": now_dt.isoformat(),
                "end_time": None,
                "pause_intervals": [],
                "pings": pings,
            }

            stats_res, splits_res = calculate_ride_telemetry(ride_data, is_imperial=is_imperial)
            ride_data["stats"] = stats_res
            ride_data["splits"] = splits_res
            storage.save_ride(ride_data)
            storage.set_active_ride_id(new_id)

            return {
                "success": True,
                "action": "start",
                "message": f"Ride session '{session_title}' started.",
                "ride_id": new_id,
                "units": units_spec,
                "ride": {**ride_data, "latest_ping": pings[-1] if pings else None},
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # Resolve active ride ID for subsequent operations
        target_id = ride_id or storage.get_active_ride_id()
        if not target_id:
            raise ToolError(
                "No active ride session found. Start one using `--action start` or specify `--ride-id <ID>`.",
                exit_code=EXIT_INVALID_INPUT,
            )

        # ----------------------------------------------------------------------
        # ACTION: DELETE
        # ----------------------------------------------------------------------
        if action_norm == ActionType.DELETE.value:
            if not storage.delete_ride(target_id):
                raise ToolError(f"Ride '{target_id}' does not exist.", exit_code=EXIT_FILE_NOT_FOUND)
            return {
                "success": True,
                "action": "delete",
                "message": f"Ride session '{target_id}' removed.",
                "ride_id": target_id,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        ride_data = storage.load_ride(target_id)

        # ----------------------------------------------------------------------
        # ACTION: PING (LOG WAYPOINT / GPS / TELEMETRY)
        # ----------------------------------------------------------------------
        if action_norm == ActionType.PING.value:
            if ride_data.get("status") == "PAUSED":
                return {
                    "success": False,
                    "error": f"Ride '{target_id}' is currently PAUSED. Resume with --action resume before pinging.",
                    "exit_code": EXIT_INVALID_INPUT,
                }

            if auto_gps and (lat is None or lon is None):
                hw = acquire_hardware_gps()
                if hw:
                    lat, lon = hw["lat"], hw["lon"]
                    if elevation is None and hw.get("elevation_m") is not None:
                        elevation = hw["elevation_m"]
                    if speed is None and hw.get("speed_kmh") is not None:
                        speed = hw["speed_kmh"]

            if lat is None or lon is None:
                raise ToolError("Latitude (--lat) and Longitude (--lon) are required for GPS pings.", exit_code=EXIT_INVALID_INPUT)

            elev_m = (elevation / 3.28084) if (elevation is not None and is_imperial) else elevation
            spd_kmh = (speed / 0.621371) if (speed is not None and is_imperial) else speed

            new_ping = {
                "time": datetime.now(timezone.utc).isoformat(),
                "lat": float(lat),
                "lon": float(lon),
                "elevation_m": elev_m,
                "speed_kmh": spd_kmh,
                "heart_rate": heart_rate,
                "cadence": cadence,
                "battery": battery,
                "notes": notes,
            }
            ride_data.setdefault("pings", []).append(new_ping)
            stats_res, splits_res = calculate_ride_telemetry(ride_data, is_imperial=is_imperial)
            ride_data["stats"] = stats_res
            ride_data["splits"] = splits_res
            storage.save_ride(ride_data)

            return {
                "success": True,
                "action": "ping",
                "message": f"Waypoint ping recorded at ({lat:.5f}, {lon:.5f})",
                "units": units_spec,
                "ride": {**ride_data, "latest_ping": new_ping},
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: PAUSE
        # ----------------------------------------------------------------------
        if action_norm == ActionType.PAUSE.value:
            if ride_data.get("status") == "PAUSED":
                return {"success": True, "message": "Activity is already paused.", "ride": ride_data}
            ride_data["status"] = "PAUSED"
            ride_data.setdefault("pause_intervals", []).append({"start": datetime.now(timezone.utc).isoformat(), "end": None})
            stats_res, splits_res = calculate_ride_telemetry(ride_data, is_imperial=is_imperial)
            ride_data["stats"] = stats_res
            ride_data["splits"] = splits_res
            storage.save_ride(ride_data)
            return {
                "success": True,
                "action": "pause",
                "message": "Activity paused.",
                "units": units_spec,
                "ride": ride_data,
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: RESUME
        # ----------------------------------------------------------------------
        if action_norm == ActionType.RESUME.value:
            if ride_data.get("status") == "RECORDING":
                return {"success": True, "message": "Activity is already recording.", "ride": ride_data}
            ride_data["status"] = "RECORDING"
            intervals = ride_data.get("pause_intervals", [])
            if intervals and intervals[-1].get("end") is None:
                intervals[-1]["end"] = datetime.now(timezone.utc).isoformat()
            stats_res, splits_res = calculate_ride_telemetry(ride_data, is_imperial=is_imperial)
            ride_data["stats"] = stats_res
            ride_data["splits"] = splits_res
            storage.save_ride(ride_data)
            return {
                "success": True,
                "action": "resume",
                "message": "Activity recording resumed.",
                "units": units_spec,
                "ride": ride_data,
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: STOP
        # ----------------------------------------------------------------------
        if action_norm == ActionType.STOP.value:
            now_iso = datetime.now(timezone.utc).isoformat()
            ride_data["status"] = "COMPLETED"
            ride_data["end_time"] = now_iso
            intervals = ride_data.get("pause_intervals", [])
            if intervals and intervals[-1].get("end") is None:
                intervals[-1]["end"] = now_iso

            stats_res, splits_res = calculate_ride_telemetry(ride_data, is_imperial=is_imperial)
            ride_data["stats"] = stats_res
            ride_data["splits"] = splits_res
            storage.save_ride(ride_data)

            if storage.get_active_ride_id() == target_id:
                storage.set_active_ride_id(None)

            return {
                "success": True,
                "action": "stop",
                "message": f"Ride session '{ride_data.get('title')}' completed and saved.",
                "units": units_spec,
                "ride": ride_data,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: EXPORT
        # ----------------------------------------------------------------------
        if action_norm == ActionType.EXPORT.value:
            fmt = format.lower()
            export_path = storage.storage_dir / f"{target_id}.{fmt}"
            pings_count = len(ride_data.get("pings", []))

            if fmt == ExportFormat.GPX.value:
                payload_str = generate_gpx(ride_data)
                with open(export_path, "w", encoding="utf-8") as fp:
                    fp.write(payload_str)
                payload: Any = payload_str if mode == "detailed" else f"GPX XML Track ({len(payload_str)} bytes)"
            elif fmt == ExportFormat.TCX.value:
                payload_str = generate_tcx(ride_data)
                with open(export_path, "w", encoding="utf-8") as fp:
                    fp.write(payload_str)
                payload = payload_str if mode == "detailed" else f"TCX XML Activity ({len(payload_str)} bytes)"
            elif fmt == ExportFormat.GEOJSON.value:
                geojson_content = generate_geojson(ride_data)
                with open(export_path, "w", encoding="utf-8") as fp:
                    json.dump(geojson_content, fp, indent=2)
                payload = geojson_content
            elif fmt == ExportFormat.CSV.value:
                csv_content = generate_csv(ride_data)
                with open(export_path, "w", encoding="utf-8") as fp:
                    fp.write(csv_content)
                payload = csv_content if mode == "detailed" else f"CSV Data ({len(csv_content)} bytes)"
            else:
                with open(export_path, "w", encoding="utf-8") as fp:
                    json.dump(ride_data, fp, indent=2, cls=ToolJSONEncoder)
                payload = ride_data

            return {
                "success": True,
                "action": "export",
                "ride_id": target_id,
                "format": fmt,
                "export_file": str(export_path),
                "point_count": pings_count,
                "data": payload,
                "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
                "exit_code": EXIT_SUCCESS,
            }

        # ----------------------------------------------------------------------
        # ACTION: STATUS (DEFAULT)
        # ----------------------------------------------------------------------
        stats_res, splits_res = calculate_ride_telemetry(ride_data, is_imperial=is_imperial)
        ride_data["stats"] = stats_res
        ride_data["splits"] = splits_res
        pings = ride_data.get("pings", [])

        return {
            "success": True,
            "action": "status",
            "units": units_spec,
            "ride": {**ride_data, "latest_ping": pings[-1] if pings else None},
            "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
            "exit_code": EXIT_SUCCESS,
        }

    except ToolError as exc:
        res = exc.to_dict()
        res["duration_ms"] = round((time.monotonic() - start_time) * 1000, 2)
        return res
    except Exception as exc:
        return {
            "success": False,
            "error": f"Bike tracker execution error: {exc}",
            "exit_code": EXIT_ERROR,
            "duration_ms": round((time.monotonic() - start_time) * 1000, 2),
        }


# ==============================================================================
# SECTION 7: Output Routing & AIChat Interface
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


def run(
    action: Literal["start", "ping", "pause", "resume", "stop", "status", "list", "stats", "export", "delete"] = "status",
    title: Optional[str] = None,
    ride_id: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    elevation: Optional[float] = None,
    speed: Optional[float] = None,
    heart_rate: Optional[int] = None,
    cadence: Optional[int] = None,
    battery: Optional[int] = None,
    bike: Optional[str] = None,
    notes: Optional[str] = None,
    format: Literal["gpx", "tcx", "geojson", "csv", "json"] = "gpx",
    units: Literal["metric", "imperial"] = "metric",
    mode: Literal["summary", "detailed"] = "summary",
    auto_gps: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> None:
    """Execute the bike tracker tool with specified parameters.

    Args:
        action: Tracker action: start, ping, pause, resume, stop, status, list, stats, export, delete
        title: Activity name or session title
        ride_id: Specific session ID (defaults to active session)
        lat: Latitude for GPS ping
        lon: Longitude for GPS ping
        elevation: Elevation in meters (or feet)
        speed: Speed in km/h (or mph)
        heart_rate: Rider heart rate in BPM
        cadence: Pedaling cadence in RPM
        battery: Sensor battery percentage
        bike: Bike profile name
        notes: Waypoint note or comment
        format: Export format: gpx, tcx, geojson, csv, json
        units: Unit system: metric or imperial
        mode: Execution mode: summary or detailed
        auto_gps: Auto-acquire coordinates from hardware GPS
        no_color: Disable ANSI color output
        verbose: Enable detailed debug logging
    """
    result = execute_tool(
        action=action,
        title=title,
        ride_id=ride_id,
        lat=lat,
        lon=lon,
        elevation=elevation,
        speed=speed,
        heart_rate=heart_rate,
        cadence=cadence,
        battery=battery,
        bike=bike,
        notes=notes,
        format=format,
        units=units,
        mode=mode,
        auto_gps=auto_gps,
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
        prog="bike_tracker.py",
        description=f"AIChat Pro Bike Trek Recorder & Telemetry Cockpit v{__version__}",
    )
    parser.add_argument(
        "--action", "-a",
        choices=["start", "ping", "pause", "resume", "stop", "status", "list", "stats", "export", "delete"],
        default="status",
        help="Tracker action (default: status)",
    )
    parser.add_argument("--title", "-t", metavar="TEXT", help="Ride session name or title")
    parser.add_argument("--ride-id", dest="ride_id", metavar="ID", help="Specific ride ID (defaults to active)")
    parser.add_argument("--lat", type=float, metavar="FLOAT", help="Latitude GPS coordinate")
    parser.add_argument("--lon", type=float, metavar="FLOAT", help="Longitude GPS coordinate")
    parser.add_argument("--elevation", "-e", type=float, metavar="FLOAT", help="Elevation in meters (or ft)")
    parser.add_argument("--speed", "-s", type=float, metavar="FLOAT", help="Instantaneous speed in km/h (or mph)")
    parser.add_argument("--heart-rate", "--hr", dest="heart_rate", type=int, metavar="BPM", help="Heart rate in BPM")
    parser.add_argument("--cadence", "-c", type=int, metavar="RPM", help="Cadence in RPM")
    parser.add_argument("--battery", type=int, metavar="PCT", help="Sensor/device battery %")
    parser.add_argument("--bike", "-b", metavar="TEXT", help="Bike profile or model")
    parser.add_argument("--notes", "-n", metavar="TEXT", help="Waypoint notes or trail observations")
    parser.add_argument(
        "--format", "-f",
        choices=["gpx", "tcx", "geojson", "csv", "json"],
        default="gpx",
        help="Export format (default: gpx)",
    )
    parser.add_argument(
        "--units", "-u",
        choices=["metric", "imperial"],
        default="metric",
        help="Unit system (default: metric)",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["summary", "detailed"],
        default="summary",
        help="Detail level (default: summary)",
    )
    parser.add_argument(
        "--auto-gps",
        action="store_true",
        default=False,
        dest="auto_gps",
        help="Auto-acquire coordinates from hardware GPS",
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
        action=args.action,
        title=args.title,
        ride_id=args.ride_id,
        lat=args.lat,
        lon=args.lon,
        elevation=args.elevation,
        speed=args.speed,
        heart_rate=args.heart_rate,
        cadence=args.cadence,
        battery=args.battery,
        bike=args.bike,
        notes=args.notes,
        format=args.format,
        units=args.units,
        mode=args.mode,
        auto_gps=args.auto_gps,
        no_color=args.no_color,
        verbose=args.verbose,
    )

    print_human_readable_ui(res, no_color=args.no_color)
    write_llm_output(res)
    sys.exit(res.get("exit_code", EXIT_SUCCESS))
