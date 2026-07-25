#!/usr/bin/env python3
# ==============================================================================
# optimizer.py — Pyrmethus AIChat Tool Template v1.1.0
# argc/aichat compatible · Human-Readable Colorized Outputs
#
# @describe 18. Life Force Optimizer — Monitors battery, storage, CPU, RAM and suggests real-time optimizations, app-killing rituals, and performance enchantments.
#
# @option --target <PATH>                Target storage path to inspect (default: /)
# @option --interval <SECONDS>           Refresh interval in seconds when running in watch daemon mode (default: 5)
# @option --cpu-threshold <PERCENT>      CPU load percentage threshold to flag heavy processes (default: 80.0)
# @flag   --watch                        Run as a continuous monitoring daemon
# @flag   --kill-heavy                   Automatically identify high-resource process PIDs for termination rituals
# @flag   --no-color                     Disable ANSI color output
# @flag   --verbose                      Enable detailed debug log output
#
# @env LLM_OUTPUT=/dev/stdout            Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

__version__ = "1.1.0"

# ==============================================================================
# SECTION 1: Color Palette & Formatting Helpers
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

_ANSI_RE = re.compile(r"\033\[[0-9;]*[mGKHF]")


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    """Return True if stdout is attached to an interactive terminal."""
    return sys.stdout.isatty()


def _cprint(text: str, file: Any = None, no_color: bool = False) -> None:
    """Print pre-formatted ANSI text, stripping colors if stdout is not a TTY or --no-color is set."""
    target = file or sys.stdout
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True)


def print_human_readable_ui(data: dict[str, Any], no_color: bool = False) -> None:
    """
    Render a human-friendly, colorized box UI for terminal users.
    Only executes if running in an interactive TTY.
    """
    if not _is_tty() or no_color:
        return

    success = data.get("success", False)
    status_color = NEON_GREEN if success else NEON_RED
    status_symbol = "✓" if success else "✗"
    status_text = "HEALTHY" if success else "DEGRADED"

    box_w = 64
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_PINK}⚡ [18. LIFE FORCE OPTIMIZER]{RESET} {status_color}{BOLD}{status_symbol} {status_text}{RESET}")
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    
    cpu = data.get("cpu_load_percent", 0.0)
    ram = data.get("ram_usage_percent", 0.0)
    storage = data.get("storage_used_percent", 0.0)
    battery = data.get("battery_level", "N/A")

    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}CPU Load:{RESET}       {NEON_YELLOW}{cpu}%{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}RAM Usage:{RESET}      {NEON_YELLOW}{ram}%{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Storage Used:{RESET}   {NEON_YELLOW}{storage}%{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Battery Level:{RESET}  {NEON_GREEN}{battery}{RESET}")
    _cprint(f"{NEON_PURPLE}│{RESET} {NEON_CYAN}Duration:{RESET}       {DIM}{data.get('duration_ms', 0)}ms{RESET}")

    heavy_procs = data.get("heavy_processes", [])
    if heavy_procs:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}High-Resource Process Targets ({len(heavy_procs)}):{RESET}")
        for proc in heavy_procs[:5]:
            _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_RED}› PID {proc.get('pid')}{RESET} ({proc.get('name')}): {proc.get('cpu')}% CPU, {proc.get('mem')}% MEM")

    rituals = data.get("optimization_rituals", [])
    if rituals:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(f"{NEON_PURPLE}│{RESET} {BOLD}Performance Enchantments & Rituals:{RESET}")
        for ritual in rituals:
            _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_CYAN}🔮{RESET} {ritual}")

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")


# ==============================================================================
# SECTION 2: Core Logic Implementation
# ==============================================================================

def _get_cpu_and_ram() -> tuple[float, float]:
    """Retrieve CPU load percentage and RAM usage percentage on Linux / Termux."""
    cpu_pct, ram_pct = 0.0, 0.0
    
    # Read CPU load average
    try:
        if Path("/proc/loadavg").exists():
            with open("/proc/loadavg", "r") as f:
                load1 = float(f.read().split()[0])
                cores = os.cpu_count() or 1
                cpu_pct = round(min(100.0, (load1 / cores) * 100), 1)
    except Exception:
        pass

    # Read RAM usage from /proc/meminfo
    try:
        if Path("/proc/meminfo").exists():
            mem_data = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = int(parts[1].split()[0])
                        mem_data[key] = val
            
            total = mem_data.get("MemTotal", 1)
            free = mem_data.get("MemAvailable", mem_data.get("MemFree", 0))
            ram_pct = round(((total - free) / total) * 100, 1)
    except Exception:
        pass

    return cpu_pct, ram_pct


def _get_battery_status() -> str:
    """Retrieve battery percentage for Termux or Linux environments."""
    # Check Termux API
    if shutil.which("termux-battery-status"):
        try:
            res = subprocess.check_output(["termux-battery-status"], timeout=2)
            bdata = json.loads(res.decode())
            percentage = bdata.get("percentage")
            status = "Charging" if bdata.get("plugged") != "UNPLUGGED" else "Discharging"
            if percentage is not None:
                return f"{percentage}% ({status})"
        except Exception:
            pass

    # Check Linux /sys/class/power_supply
    try:
        p_path = Path("/sys/class/power_supply")
        if p_path.exists():
            for supply in p_path.iterdir():
                cap_file = supply / "capacity"
                if cap_file.exists():
                    cap = cap_file.read_text().strip()
                    return f"{cap}%"
    except Exception:
        pass

    return "100% (AC)"


def _get_heavy_processes(cpu_threshold: float) -> list[dict[str, Any]]:
    """Scan process table for high CPU or RAM consuming processes."""
    heavy = []
    if shutil.which("ps"):
        try:
            out = subprocess.check_output(["ps", "-eo", "pid,%cpu,%mem,comm"], text=True)
            lines = out.strip().splitlines()[1:]
            for line in lines:
                parts = line.split(maxsplit=3)
                if len(parts) == 4:
                    pid_str, cpu_str, mem_str, comm = parts
                    try:
                        cpu_f = float(cpu_str)
                        mem_f = float(mem_str)
                        if cpu_f >= cpu_threshold or mem_f >= 15.0:
                            heavy.append({
                                "pid": int(pid_str),
                                "cpu": cpu_f,
                                "mem": mem_f,
                                "name": comm.strip()
                            })
                    except ValueError:
                        continue
        except Exception:
            pass

    return sorted(heavy, key=lambda x: x["cpu"], reverse=True)


def execute_tool(
    target: str = "/",
    interval: int = 5,
    cpu_threshold: float = 80.0,
    watch: bool = False,
    kill_heavy: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Core execution logic for system resource monitoring and performance optimization.
    """
    start_time = time.perf_counter()
    target_path = Path(target).expanduser().resolve()

    if not target_path.exists():
        target_path = Path("/")

    try:
        # Storage usage check
        disk_usage = shutil.disk_usage(target_path)
        storage_pct = round((disk_usage.used / disk_usage.total) * 100, 1)

        cpu_load, ram_usage = _check_cpu_and_ram = _get_cpu_and_ram()
        battery_status = _get_battery_status()
        heavy_procs = _get_heavy_processes(cpu_threshold)

        # Generate optimization rituals
        rituals = []
        if ram_usage > 75.0:
            rituals.append("Memory Reclaim Ritual: Purge cache using 'sync && sysctl -w vm.drop_caches=3' or restart heavy daemons.")
        if storage_pct > 85.0:
            rituals.append("Storage Liberation Ritual: Clean package cache ('apt clean' or 'pkg clean') & remove stale temp files.")
        if cpu_load > cpu_threshold:
            rituals.append(f"CPU Banishing Ritual: High CPU detected ({cpu_load}%). Consider terminating top PID targets.")
        if "Discharging" in battery_status:
            rituals.append("Life Force Preservation: Battery discharging. Lower background refresh intervals & turn off debug logs.")

        if not rituals:
            rituals.append("Harmonic Resonance Achieved: All system life metrics are operating at optimal levels.")

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return {
            "success": True,
            "target": str(target_path),
            "cpu_load_percent": cpu_load,
            "ram_usage_percent": ram_usage,
            "storage_used_percent": storage_pct,
            "battery_level": battery_status,
            "heavy_processes": heavy_procs,
            "optimization_rituals": rituals,
            "duration_ms": duration_ms,
            "exit_code": 0
        }

    except Exception as exc:
        return {
            "success": False,
            "error": f"Life Force Optimizer execution failed: {exc}",
            "exit_code": 1
        }


# ==============================================================================
# SECTION 3: Output Routing (LLM vs Human Terminal)
# ==============================================================================

def write_llm_output(data: dict[str, Any]) -> None:
    """Format and write clean JSON output to LLM_OUTPUT destination."""
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

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
# SECTION 4: Function Entry Point for AIChat
# ==============================================================================

def run(
    target: str = "/",
    interval: int = 5,
    cpu_threshold: float = 80.0,
    watch: bool = False,
    kill_heavy: bool = False,
    no_color: bool = False,
    verbose: bool = False,
) -> None:
    """
    AIChat Programmatic Entrypoint.
    Parameter names match option/flag slugs (with underscores).
    """
    if watch:
        try:
            while True:
                if _is_tty():
                    os.system("clear" if os.name == "posix" else "cls")
                result = execute_tool(
                    target=target,
                    interval=interval,
                    cpu_threshold=cpu_threshold,
                    watch=True,
                    kill_heavy=kill_heavy,
                    no_color=no_color,
                    verbose=verbose,
                )
                print_human_readable_ui(result, no_color=no_color)
                write_llm_output(result)
                time.sleep(interval)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        result = execute_tool(
            target=target,
            interval=interval,
            cpu_threshold=cpu_threshold,
            watch=False,
            kill_heavy=kill_heavy,
            no_color=no_color,
            verbose=verbose,
        )
        print_human_readable_ui(result, no_color=no_color)
        write_llm_output(result)


# ==============================================================================
# SECTION 5: CLI Argument Parser
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="optimizer.py",
        description=f"AIChat 18. Life Force Optimizer v{__version__}",
    )
    parser.add_argument(
        "--target", "-t",
        default="/",
        metavar="PATH",
        help="Target storage path to inspect (default: /)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=5,
        help="Refresh interval in seconds when running in watch daemon mode (default: 5)",
    )
    parser.add_argument(
        "--cpu-threshold",
        type=float,
        default=80.0,
        dest="cpu_threshold",
        help="CPU load percentage threshold to flag heavy processes (default: 80.0)",
    )
    parser.add_argument(
        "--watch", "-w",
        action="store_true",
        default=False,
        help="Run as a continuous monitoring daemon",
    )
    parser.add_argument(
        "--kill-heavy",
        action="store_true",
        default=False,
        dest="kill_heavy",
        help="Identify high-resource process PIDs for termination rituals",
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
    
    if args.watch:
        try:
            while True:
                if _is_tty():
                    os.system("clear" if os.name == "posix" else "cls")
                res = execute_tool(
                    target=args.target,
                    interval=args.interval,
                    cpu_threshold=args.cpu_threshold,
                    watch=True,
                    kill_heavy=args.kill_heavy,
                    no_color=args.no_color,
                    verbose=args.verbose,
                )
                print_human_readable_ui(res, no_color=args.no_color)
                write_llm_output(res)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        res = execute_tool(
            target=args.target,
            interval=args.interval,
            cpu_threshold=args.cpu_threshold,
            watch=False,
            kill_heavy=args.kill_heavy,
            no_color=args.no_color,
            verbose=args.verbose,
        )
        print_human_readable_ui(res, no_color=args.no_color)
        write_llm_output(res)
        sys.exit(res.get("exit_code", 0))
