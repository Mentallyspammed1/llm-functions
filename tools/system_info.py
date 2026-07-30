#!/usr/bin/env python3
# @describe Get expanded system + Termux-API + live memory & virtual-swap dynamics with luminous Termux-optimized clarity.
import platform
import os
import sys
import subprocess
import json
import shutil
import time
from colorama import init, Fore, Style

init(autoreset=True)

def _safe_termux(cmd: list[str], timeout: float = 4.0) -> str:
    """Invoke a Termux-API spell and return its essence, or a silent ward on failure."""
    if not shutil.which(cmd[0]):
        return f"{Fore.RED}termux-api not found – run: pkg install termux-api{Style.RESET_ALL}"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return f"{Fore.RED}unavailable{Style.RESET_ALL}"
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, Exception):
        return f"{Fore.RED}unavailable{Style.RESET_ALL}"

def _read_meminfo() -> dict[str, int]:
    """Read /proc/meminfo and return values in kilobytes."""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().split()[0]
                if value.isdigit():
                    info[key] = int(value)
    except (OSError, ValueError):
        pass
    return info

def _read_vmstat() -> dict[str, int]:
    """Read /proc/vmstat for page & swap activity counters."""
    info: dict[str, int] = {}
    try:
        with open("/proc/vmstat", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    info[parts[0]] = int(parts[1])
    except (OSError, ValueError):
        pass
    return info

def _fmt_bytes(kb: int) -> str:
    """Convert kilobytes into a human-readable luminous string."""
    if kb <= 0:
        return "Unknown"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"

def _live_samples(interval: float = 0.7) -> tuple[dict, dict, dict, dict, float]:
    """Dual-sample meminfo + vmstat to expose real-time virtual & swap dynamics."""
    m1 = _read_meminfo()
    v1 = _read_vmstat()
    time.sleep(interval)
    m2 = _read_meminfo()
    v2 = _read_vmstat()
    return m1, m2, v1, v2, interval

def run() -> str:
    """Channel host + Termux-API + live virtual-memory & swap dynamics into one luminous incantation."""
    # Core platform essence
    os_name = platform.system() or "Unknown"
    release = platform.release() or "Unknown"
    version = platform.version() or "Unknown"
    machine = platform.machine() or "Unknown"
    processor = platform.processor() or machine
    node = platform.node() or "Unknown"
    py_ver = platform.python_version()
    cpu_count = os.cpu_count() or "Unknown"
    cwd = os.getcwd()
    user = os.getenv("USER") or os.getenv("LOGNAME") or "Unknown"

    # Live dual samples
    mem1, mem2, vm1, vm2, interval = _live_samples()

    # RAM snapshot (second sample)
    total = mem2.get("MemTotal", 0)
    available = mem2.get("MemAvailable", 0)
    free = mem2.get("MemFree", 0)
    buffers = mem2.get("Buffers", 0)
    cached = mem2.get("Cached", 0)
    used = total - available if total and available else 0

    # Swap & virtual memory
    swap_total = mem2.get("SwapTotal", 0)
    swap_free = mem2.get("SwapFree", 0)
    swap_cached = mem2.get("SwapCached", 0)
    swap_used = swap_total - swap_free if swap_total else 0
    anon = mem2.get("AnonPages", 0)
    mapped = mem2.get("Mapped", 0)
    shmem = mem2.get("Shmem", 0)
    commit_limit = mem2.get("CommitLimit", 0)
    committed = mem2.get("Committed_AS", 0)
    vmalloc_total = mem2.get("VmallocTotal", 0)
    vmalloc_used = mem2.get("VmallocUsed", 0)

    # Real-time rates from vmstat (pages → kB, assuming 4 kB pages)
    PAGE_KB = 4
    pswpin1 = vm1.get("pswpin", 0)
    pswpin2 = vm2.get("pswpin", 0)
    pswpout1 = vm1.get("pswpout", 0)
    pswpout2 = vm2.get("pswpout", 0)
    pgfault1 = vm1.get("pgfault", 0)
    pgfault2 = vm2.get("pgfault", 0)
    pgmajfault1 = vm1.get("pgmajfault", 0)
    pgmajfault2 = vm2.get("pgmajfault", 0)

    swap_in_rate = ((pswpin2 - pswpin1) * PAGE_KB) / interval if interval else 0
    swap_out_rate = ((pswpout2 - pswpout1) * PAGE_KB) / interval if interval else 0
    fault_rate = (pgfault2 - pgfault1) / interval if interval else 0
    majfault_rate = (pgmajfault2 - pgmajfault1) / interval if interval else 0

    # RAM pressure trends
    avail1 = mem1.get("MemAvailable", 0)
    avail2 = mem2.get("MemAvailable", 0)
    free1 = mem1.get("MemFree", 0)
    free2 = mem2.get("MemFree", 0)
    delta_avail = (avail2 - avail1) / interval if interval else 0
    delta_free = (free2 - free1) / interval if interval else 0

    def _trend(delta: float) -> str:
        if delta > 50:
            return f"{Fore.GREEN}↑ releasing{Style.RESET_ALL}"
        if delta < -50:
            return f"{Fore.RED}↓ consuming{Style.RESET_ALL}"
        return f"{Fore.YELLOW}→ stable{Style.RESET_ALL}"

    def _swap_pressure() -> str:
        if swap_total == 0:
            return f"{Fore.BLUE}no swap configured{Style.RESET_ALL}"
        pct = (swap_used / swap_total) * 100 if swap_total else 0
        if pct > 80:
            return f"{Fore.RED}HIGH ({pct:.0f}%){Style.RESET_ALL}"
        if pct > 40:
            return f"{Fore.YELLOW}moderate ({pct:.0f}%){Style.RESET_ALL}"
        return f"{Fore.GREEN}low ({pct:.0f}%){Style.RESET_ALL}"

    def _overcommit() -> str:
        if commit_limit == 0:
            return "unknown"
        ratio = (committed / commit_limit) * 100 if commit_limit else 0
        if ratio > 100:
            return f"{Fore.RED}OVERCOMMITTED ({ratio:.0f}%){Style.RESET_ALL}"
        if ratio > 80:
            return f"{Fore.YELLOW}high ({ratio:.0f}%){Style.RESET_ALL}"
        return f"{Fore.GREEN}healthy ({ratio:.0f}%){Style.RESET_ALL}"

    # Termux-API channels
    battery_raw = _safe_termux(["termux-battery-status"])
    device_raw = _safe_termux(["termux-telephony-deviceinfo"])
    wifi_raw = _safe_termux(["termux-wifi-connectioninfo"])
    termux_info = _safe_termux(["termux-info"], timeout=6.0)

    # Parse battery if JSON
    battery_line = battery_raw
    if battery_raw.startswith("{"):
        try:
            b = json.loads(battery_raw)
            pct = b.get("percentage", "?")
            status = b.get("status", "?")
            health = b.get("health", "?")
            temp = b.get("temperature", "?")
            battery_line = f"{pct}% | {status} | health:{health} | {temp}°C"
        except json.JSONDecodeError:
            pass

    # Parse device if JSON
    device_line = device_raw
    if device_raw.startswith("{"):
        try:
            d = json.loads(device_raw)
            device_line = (
                f"{d.get('manufacturer', '?')} {d.get('model', '?')} "
                f"(API {d.get('sdk_version', '?')})"
            )
        except json.JSONDecodeError:
            pass

    # Parse wifi if JSON
    wifi_line = wifi_raw
    if wifi_raw.startswith("{"):
        try:
            w = json.loads(wifi_raw)
            ssid = w.get("ssid", "n/a")
            ip = w.get("ip", "n/a")
            wifi_line = f"SSID:{ssid}  IP:{ip}"
        except json.JSONDecodeError:
            pass

    return (
        f"{Fore.CYAN}═══ SYSTEM ═══{Style.RESET_ALL}\n"
        f"{Fore.CYAN}OS:{Style.RESET_ALL} {os_name} {release}\n"
        f"{Fore.CYAN}Kernel:{Style.RESET_ALL} {version}\n"
        f"{Fore.YELLOW}CPU:{Style.RESET_ALL} {processor} ({machine})\n"
        f"{Fore.YELLOW}Cores:{Style.RESET_ALL} {cpu_count}\n"
        f"{Fore.GREEN}Host:{Style.RESET_ALL} {node}\n"
        f"{Fore.GREEN}User:{Style.RESET_ALL} {user}\n"
        f"{Fore.MAGENTA}Python:{Style.RESET_ALL} {py_ver}\n"
        f"{Fore.BLUE}CWD:{Style.RESET_ALL} {cwd}\n\n"
        f"{Fore.CYAN}═══ LIVE RAM ═══{Style.RESET_ALL}\n"
        f"{Fore.YELLOW}RAM Total:{Style.RESET_ALL}     {_fmt_bytes(total)}\n"
        f"{Fore.YELLOW}RAM Used:{Style.RESET_ALL}      {_fmt_bytes(used)}\n"
        f"{Fore.YELLOW}RAM Available:{Style.RESET_ALL} {_fmt_bytes(available)}  {_trend(delta_avail)}\n"
        f"{Fore.YELLOW}RAM Free:{Style.RESET_ALL}      {_fmt_bytes(free)}  {_trend(delta_free)}\n"
        f"{Fore.YELLOW}Buffers:{Style.RESET_ALL}      {_fmt_bytes(buffers)}\n"
        f"{Fore.YELLOW}Cached:{Style.RESET_ALL}       {_fmt_bytes(cached)}\n\n"
        f"{Fore.CYAN}═══ VIRTUAL MEMORY & SWAP DYNAMICS ═══{Style.RESET_ALL}\n"
        f"{Fore.MAGENTA}Swap Total:{Style.RESET_ALL}    {_fmt_bytes(swap_total)}\n"
        f"{Fore.MAGENTA}Swap Used:{Style.RESET_ALL}     {_fmt_bytes(swap_used)}  {_swap_pressure()}\n"
        f"{Fore.MAGENTA}Swap Free:{Style.RESET_ALL}     {_fmt_bytes(swap_free)}\n"
        f"{Fore.MAGENTA}Swap Cached:{Style.RESET_ALL}   {_fmt_bytes(swap_cached)}\n"
        f"{Fore.YELLOW}AnonPages:{Style.RESET_ALL}     {_fmt_bytes(anon)}\n"
        f"{Fore.YELLOW}Mapped:{Style.RESET_ALL}        {_fmt_bytes(mapped)}\n"
        f"{Fore.YELLOW}Shmem:{Style.RESET_ALL}         {_fmt_bytes(shmem)}\n"
        f"{Fore.BLUE}CommitLimit:{Style.RESET_ALL}   {_fmt_bytes(commit_limit)}\n"
        f"{Fore.BLUE}Committed_AS:{Style.RESET_ALL}  {_fmt_bytes(committed)}  {_overcommit()}\n"
        f"{Fore.BLUE}Vmalloc Used:{Style.RESET_ALL}  {_fmt_bytes(vmalloc_used)} / {_fmt_bytes(vmalloc_total)}\n\n"
        f"{Fore.CYAN}═══ LIVE SWAP & FAULT RATES ═══{Style.RESET_ALL}\n"
        f"{Fore.RED}Swap-in:{Style.RESET_ALL}       {swap_in_rate:+.1f} kB/s\n"
        f"{Fore.RED}Swap-out:{Style.RESET_ALL}      {swap_out_rate:+.1f} kB/s\n"
        f"{Fore.YELLOW}Page faults:{Style.RESET_ALL}   {fault_rate:+.0f} /s\n"
        f"{Fore.YELLOW}Major faults:{Style.RESET_ALL}  {majfault_rate:+.0f} /s\n"
        f"{Fore.BLUE}Sample window:{Style.RESET_ALL} {interval:.1f}s\n\n"
        f"{Fore.CYAN}═══ TERMUX API ═══{Style.RESET_ALL}\n"
        f"{Fore.YELLOW}Battery:{Style.RESET_ALL} {battery_line}\n"
        f"{Fore.YELLOW}Device:{Style.RESET_ALL} {device_line}\n"
        f"{Fore.YELLOW}Wi-Fi:{Style.RESET_ALL} {wifi_line}\n\n"
        f"{Fore.CYAN}═══ TERMUX INFO ═══{Style.RESET_ALL}\n"
        f"{termux_info}"
    )

if __name__ == "__main__":
    print(run())
