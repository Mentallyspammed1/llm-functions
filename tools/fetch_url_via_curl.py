#!/usr/bin/env python3
# @describe Fetch content from a URL using curl with advanced options.
# @option --url! The URL to fetch.
# @option --timeout=30 <INT> Request timeout in seconds (used as --max-time when max-time is default).
# @option --max-time=60 <INT> Maximum transfer time in seconds.
# @option --connect-timeout=10 <INT> Connection timeout in seconds.
# @option --user-agent=curl_tool/1.0 Custom User-Agent header.
# @option --output= Output file path (default: LLM_OUTPUT or return body in tool result).
# @option --dump-header= Output file path for HTTP response headers.
# @option --cookie-file= Path to cookie file for session management.
# @option --method=GET HTTP method (GET, POST, PUT, DELETE, PATCH, HEAD).
# @option --data= Request body data for POST/PUT/PATCH.
# @option --headers= Comma-separated headers (e.g., "Key: Val,Key2: Val2").
# @flag --follow-redirects Follow HTTP redirects.
# @option --max-redirects=5 <INT> Maximum number of redirects.
# @flag --verify-ssl Verify SSL certificates (default: true).
# @flag --compressed Request compressed response.
# @flag --silent Silent mode (less curl stderr).
# @flag --verbose Verbose curl output to stderr.
# @flag --include Include HTTP response headers in the body output.
# @option --retry=3 <INT> Number of retry attempts.
# @option --retry-delay=1 <INT> Delay between retries in seconds.
# @option --limit-rate= Limit download rate (e.g., 100k, 1m).
# @option --proxy= Proxy server URL.
# @option --auth= Authentication credentials (user:password or bearer token).
# @option --auth-type=basic Authentication type (basic or bearer).
# @option --max-bytes=10485760 <INT> Abort if body exceeds this many bytes (default 10 MiB).
# @flag --json-on-success If set, return JSON with body, http_code, and final_url on success.
# @env LLM_OUTPUT=/dev/stdout The output path when --output is empty.

"""
curl_fetch.py — HTTP fetch via curl (llm-functions / Termux / webx).

Success: response body (UTF-8 with replacement), or JSON if --json-on-success.
HTTP / curl failures: single-line JSON {"status":"error",...} via json.dumps.
Pre-flight failures: ERROR: ... (missing url, curl, invalid method).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ALLOWED_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

# Common curl exit code mappings for improved error transparency
CURL_EXIT_CODES = {
    1: "Unsupported protocol",
    2: "Failed to initialize",
    3: "URL malformed",
    4: "Feature not supported",
    5: "Could not resolve proxy",
    6: "Could not resolve host",
    7: "Failed to connect to host",
    28: "Operation timeout / connection timed out",
    35: "SSL connect error",
    52: "Server returned nothing",
    60: "SSL peer certificate or SSH remote key was not OK",
}

# =========================================================================
# JSON helpers (never build error JSON with f-strings)
# =========================================================================


def _sanitize_json_text(s: str, max_len: int = 2000) -> str:
    if not s:
        return ""
    # Prevent CPU exhaustion on huge strings by truncating before character analysis
    s = s[: max_len + 100]
    s = s.replace("\x00", "")
    out: List[str] = []
    for ch in s:
        if ch in "\n\r\t":
            out.append(ch)
        elif ord(ch) < 32:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)[:max_len]


def _error_json(
    http_code: int,
    msg: str,
    stderr: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "status": "error",
        "http_code": int(http_code),
        "msg": _sanitize_json_text(msg, 2000),
    }
    if stderr:
        payload["stderr"] = _sanitize_json_text(stderr, 500)
    if extra:
        for k, v in extra.items():
            if isinstance(v, str):
                payload[k] = _sanitize_json_text(v, 1000)
            else:
                payload[k] = v
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _success_json(
    http_code: int,
    body: str,
    final_url: str,
    bytes_read: int,
    truncated: bool,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "status": "ok",
        "http_code": int(http_code),
        "final_url": final_url,
        "bytes": bytes_read,
        "truncated": truncated,
        "body": body,
    }
    if headers:
        payload["headers"] = headers
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# =========================================================================
# Parsing / paths
# =========================================================================


def _parse_headers_csv(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    # Support JSON array of headers if provided
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if item]
        except Exception:
            pass
    # Fallback to splitting on commas not enclosed in quotes
    parts = re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw)
    return [p.strip().strip('"') for p in parts if p.strip()]


def _resolve_output(output: str) -> Optional[str]:
    """None means return body in tool result (stdout semantics)."""
    if output and output.strip():
        p = output.strip()
        if p in ("/dev/stdout", "-"):
            return None
        return p
    env = os.environ.get("LLM_OUTPUT", "").strip()
    if not env or env in ("/dev/stdout", "-"):
        return None
    return env


def _effective_max_time(timeout: int, max_time: int) -> int:
    t, m = int(timeout), int(max_time)
    # If max_time is default (60) and timeout was explicitly customized (not 30)
    if m == 60 and t != 30:
        return max(t, 1)
    # Otherwise, prefer the specified max_time as the explicit ceiling
    return max(m, 1)


def _parse_header_file(path: str) -> Dict[str, str]:
    """Parse HTTP headers from file, returning keys in lowercase."""
    if not path or not os.path.exists(path):
        return {}
    headers: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # Split headers into logical blocks (handling multiple hops/redirects)
        blocks: List[List[str]] = []
        current_block: List[str] = []
        for line in lines:
            line_s = line.strip()
            if not line_s:
                if current_block:
                    blocks.append(current_block)
                    current_block = []
            else:
                current_block.append(line_s)
        if current_block:
            blocks.append(current_block)

        if not blocks:
            return {}

        # Use the last block representing the final destination headers
        for line in blocks[-1]:
            if line.startswith("HTTP/"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return headers


# =========================================================================
# curl command builder
# =========================================================================


def _build_curl_command(
    url: str,
    curl_bin: str,
    max_time: int,
    connect_timeout: int,
    user_agent: str,
    retry: int,
    retry_delay: int,
    follow_redirects: bool,
    max_redirects: int,
    verify_ssl: bool,
    cookie_file: str,
    compressed: bool,
    verbose: bool,
    include_headers: bool,
    limit_rate: str,
    method: str,
    data: str,
    headers: str,
    proxy: str,
    auth: str,
    auth_type: str,
) -> List[str]:
    cmd: List[str] = [
        curl_bin,
        "--max-time",
        str(max_time),
        "--connect-timeout",
        str(connect_timeout),
        "--user-agent",
        user_agent,
        "--retry",
        str(max(0, retry)),
        "--retry-delay",
        str(max(0, retry_delay)),
    ]

    if follow_redirects:
        cmd.extend(["--location", "--max-redirs", str(max(0, max_redirects))])
    else:
        cmd.append("--no-location")

    if not verify_ssl:
        cmd.append("--insecure")

    if cookie_file:
        cmd.extend(["--cookie-jar", cookie_file, "--cookie", cookie_file])

    if compressed:
        cmd.append("--compressed")
    if verbose:
        cmd.append("--verbose")
    if include_headers:
        cmd.append("--include")

    if limit_rate:
        cmd.extend(["--limit-rate", limit_rate])

    meth = (method or "GET").upper()
    if meth != "GET":
        cmd.extend(["--request", meth])

    if data:
        cmd.extend(["--data", data])

    for h in _parse_headers_csv(headers):
        if ":" in h:
            cmd.extend(["--header", h])

    if proxy:
        cmd.extend(["--proxy", proxy])

    if auth:
        at = (auth_type or "basic").lower()
        if at == "bearer":
            cmd.extend(["--header", f"Authorization: Bearer {auth}"])
        else:
            cmd.extend(["--user", auth])

    cmd.append(url)
    return cmd


def _read_body_capped(path: str, max_bytes: int) -> Tuple[bytes, bool]:
    truncated = False
    if not os.path.exists(path):
        return b"", False

    file_size = os.path.getsize(path)
    if file_size > max_bytes:
        truncated = True
        read_limit = max_bytes
    else:
        read_limit = file_size

    chunks: List[bytes] = []
    total = 0
    try:
        with open(path, "rb") as f:
            while total < read_limit:
                to_read = min(65536, read_limit - total)
                block = f.read(to_read)
                if not block:
                    break
                chunks.append(block)
                total += len(block)
    except OSError:
        pass
    return b"".join(chunks), truncated


# =========================================================================
# Core fetch
# =========================================================================


def fetch_url(
    url: str,
    timeout: int = 30,
    max_time: int = 60,
    connect_timeout: int = 10,
    user_agent: str = "curl_tool/1.0",
    output: str = "",
    dump_header: str = "",
    cookie_file: str = "",
    method: str = "GET",
    data: str = "",
    headers: str = "",
    follow_redirects: bool = False,
    max_redirects: int = 5,
    verify_ssl: bool = True,
    compressed: bool = False,
    silent: bool = False,
    verbose: bool = False,
    include: bool = False,
    retry: int = 3,
    retry_delay: int = 1,
    limit_rate: str = "",
    proxy: str = "",
    auth: str = "",
    auth_type: str = "basic",
    max_bytes: int = DEFAULT_MAX_BYTES,
    json_on_success: bool = False,
) -> str:
    target_url = (url or "").strip()
    if not target_url:
        return "ERROR: url is empty"

    if not re.match(r"^https?://", target_url, re.I):
        return "ERROR: url must start with http:// or https://"

    curl_bin = shutil.which("curl")
    if not curl_bin:
        return "ERROR: curl not found on PATH (pkg install curl)"

    meth = (method or "GET").upper()
    if meth not in ALLOWED_METHODS:
        return f"ERROR: unsupported method {method}"

    eff_max_time = _effective_max_time(timeout, max_time)
    max_bytes = max(1024, min(int(max_bytes), 100 * 1024 * 1024))
    out_path = _resolve_output(output)

    base_cmd = _build_curl_command(
        url=target_url,
        curl_bin=curl_bin,
        max_time=eff_max_time,
        connect_timeout=int(connect_timeout),
        user_agent=user_agent or "curl_tool/1.0",
        retry=int(retry),
        retry_delay=int(retry_delay),
        follow_redirects=bool(follow_redirects),
        max_redirects=int(max_redirects),
        verify_ssl=bool(verify_ssl),
        cookie_file=(cookie_file or "").strip(),
        compressed=bool(compressed),
        verbose=bool(verbose),
        include_headers=bool(include),
        limit_rate=(limit_rate or "").strip(),
        method=meth,
        data=(data or ""),
        headers=(headers or ""),
        proxy=(proxy or "").strip(),
        auth=(auth or "").strip(),
        auth_type=(auth_type or "basic"),
    )

    meta_path: Optional[str] = None
    body_path: Optional[str] = None

    try:
        with tempfile.NamedTemporaryFile(delete=False) as body_tmp:
            body_path = body_tmp.name
        with tempfile.NamedTemporaryFile(
            delete=False, mode="w", encoding="utf-8"
        ) as meta_tmp:
            meta_path = meta_tmp.name

        exec_cmd = list(base_cmd)
        if not silent:
            if "--silent" not in exec_cmd:
                exec_cmd.append("--silent")
            if "--show-error" not in exec_cmd:
                exec_cmd.append("--show-error")
        elif "--silent" not in exec_cmd:
            exec_cmd.append("--silent")

        exec_cmd.extend(
            [
                "-w",
                "%{http_code}\n%{url_effective}",
                "-o",
                body_path,
                "-D",
                meta_path,
            ]
        )

        proc = subprocess.run(
            exec_cmd,
            capture_output=True,
            text=True,
            timeout=eff_max_time + 45,
        )

        trailer = (proc.stdout or "").strip().splitlines()
        http_code = 0
        final_url = target_url
        if trailer:
            if trailer[0].isdigit() and len(trailer[0]) == 3:
                http_code = int(trailer[0])
            if len(trailer) >= 2:
                final_url = trailer[-1].strip() or final_url
            elif len(trailer) == 1 and not trailer[0].isdigit():
                final_url = trailer[0]

        # Read response headers if dumped to our temp file
        parsed_headers = _parse_header_file(meta_path) if meta_path else {}

        # Copy headers to requested path if --dump-header was requested
        dest_header = (dump_header or "").strip()
        if dest_header and meta_path and os.path.exists(meta_path):
            try:
                shutil.copy2(meta_path, dest_header)
            except Exception:
                pass

        if proc.returncode != 0 and http_code == 0:
            err_msg = CURL_EXIT_CODES.get(
                proc.returncode, f"curl exited with code {proc.returncode}"
            )
            return _error_json(
                0,
                err_msg,
                proc.stderr or "",
                {"url": target_url, "exit_code": proc.returncode},
            )

        raw_body, truncated = _read_body_capped(body_path, max_bytes)
        if truncated:
            if not (200 <= http_code < 300):
                return _error_json(
                    http_code,
                    f"Response exceeded max_bytes ({max_bytes})",
                    proc.stderr or "",
                    {"url": target_url, "final_url": final_url},
                )

        try:
            body_text = raw_body.decode("utf-8", errors="replace")
        except Exception:
            body_text = ""

        if not (200 <= http_code < 300):
            preview = _sanitize_json_text(body_text, 800)
            return _error_json(
                http_code,
                f"Request failed with status {http_code}",
                proc.stderr or "",
                {
                    "url": target_url,
                    "final_url": final_url,
                    "body_preview": preview,
                    "truncated": truncated,
                    "headers": parsed_headers,
                },
            )

        if truncated:
            return _error_json(
                http_code,
                f"Success but body truncated at max_bytes ({max_bytes})",
                "",
                {"url": target_url, "final_url": final_url},
            )

        if json_on_success:
            result = _success_json(
                http_code, body_text, final_url, len(raw_body), False, parsed_headers
            )
        else:
            result = body_text

        if out_path:
            dest = Path(out_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(result, encoding="utf-8")
            return (
                f"OK: http_code={http_code} bytes={len(raw_body)} "
                f"final_url={final_url} path={out_path}"
            )

        return result

    except subprocess.TimeoutExpired:
        return _error_json(0, "Request timed out", extra={"url": target_url})
    except OSError as e:
        return f"ERROR: {e}"
    finally:
        for p in (body_path, meta_path):
            if p and os.path.isfile(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


# =========================================================================
# llm-functions entry
# =========================================================================


def run(
    url: str,
    timeout: int = 30,
    max_time: int = 60,
    connect_timeout: int = 10,
    user_agent: str = "curl_tool/1.0",
    output: str = "",
    dump_header: str = "",
    cookie_file: str = "",
    method: str = "GET",
    data: str = "",
    headers: str = "",
    follow_redirects: bool = False,
    max_redirects: int = 5,
    verify_ssl: bool = True,
    compressed: bool = False,
    silent: bool = False,
    verbose: bool = False,
    include: bool = False,
    retry: int = 3,
    retry_delay: int = 1,
    limit_rate: str = "",
    proxy: str = "",
    auth: str = "",
    auth_type: str = "basic",
    max_bytes: int = DEFAULT_MAX_BYTES,
    json_on_success: bool = False,
) -> str:
    """Fetch a URL with curl and return the body, OK summary, or JSON error.

    Args:
        url: HTTP or HTTPS URL to fetch (required).
        timeout: Seconds; maps to curl --max-time when max_time is still the default 60.
        max_time: curl --max-time in seconds.
        connect_timeout: curl --connect-timeout in seconds.
        user_agent: User-Agent header string.
        output: Write response to this path; empty uses LLM_OUTPUT env or returns body in result.
        dump_header: Save response headers to this file (--dump-header).
        cookie_file: Cookie jar path for load/save.
        method: GET, POST, PUT, DELETE, PATCH, HEAD, or OPTIONS.
        data: Request body for POST/PUT/PATCH.
        headers: Comma-separated or JSON-formatted extra headers.
        follow_redirects: Follow redirects with --location.
        max_redirects: Maximum redirect hops.
        verify_ssl: If false, use curl --insecure.
        compressed: Request gzip/deflate (--compressed).
        silent: curl --silent.
        verbose: curl --verbose (stderr).
        include: Include response headers in the downloaded body (--include).
        retry: curl --retry count.
        retry_delay: Seconds between retries.
        limit_rate: Cap speed, e.g. 500k or 2m.
        proxy: HTTP/SOCKS proxy URL.
        auth: Basic user:password or bearer token string.
        auth_type: basic or bearer.
        max_bytes: Stop and error if response body exceeds this size.
        json_on_success: Return JSON with status, http_code, final_url, and body on success.
    """
    return fetch_url(
        url=url,
        timeout=timeout,
        max_time=max_time,
        connect_timeout=connect_timeout,
        user_agent=user_agent,
        output=output,
        dump_header=dump_header,
        cookie_file=cookie_file,
        method=method,
        data=data,
        headers=headers,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        verify_ssl=verify_ssl,
        compressed=compressed,
        silent=silent,
        verbose=verbose,
        include=include,
        retry=retry,
        retry_delay=retry_delay,
        limit_rate=limit_rate,
        proxy=proxy,
        auth=auth,
        auth_type=auth_type,
        max_bytes=max_bytes,
        json_on_success=json_on_success,
    )


# =========================================================================
# CLI
# =========================================================================


def _add_cli_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", required=True)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--max-time", type=int, default=60)
    p.add_argument("--connect-timeout", type=int, default=10)
    p.add_argument("--user-agent", default="curl_tool/1.0")
    p.add_argument("--output", default="")
    p.add_argument("--dump-header", default="")
    p.add_argument("--cookie-file", default="")
    p.add_argument("--method", default="GET")
    p.add_argument("--data", default="")
    p.add_argument("--headers", default="")
    p.add_argument("--follow-redirects", action="store_true")
    p.add_argument("--max-redirects", type=int, default=5)
    p.add_argument("--verify-ssl", action="store_true", default=True)
    p.add_argument("--no-verify-ssl", action="store_false", dest="verify_ssl")
    p.add_argument("--compressed", action="store_true")
    p.add_argument("--silent", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--include", action="store_true")
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--retry-delay", type=int, default=1)
    p.add_argument("--limit-rate", default="")
    p.add_argument("--proxy", default="")
    p.add_argument("--auth", default="")
    p.add_argument("--auth-type", default="basic")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p.add_argument("--json-on-success", action="store_true")


def _exit_code(result: str) -> int:
    if result.startswith("ERROR:"):
        return 1
    if result.startswith("{") and '"status":"error"' in result.replace(" ", ""):
        return 1
    if '"status": "error"' in result[:120]:
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch URL with curl")
    _add_cli_args(p)
    args = p.parse_args()

    out = run(
        url=args.url,
        timeout=args.timeout,
        max_time=args.max_time,
        connect_timeout=args.connect_timeout,
        user_agent=args.user_agent,
        output=args.output,
        dump_header=args.dump_header,
        cookie_file=args.cookie_file,
        method=args.method,
        data=args.data,
        headers=args.headers,
        follow_redirects=args.follow_redirects,
        max_redirects=args.max_redirects,
        verify_ssl=args.verify_ssl,
        compressed=args.compressed,
        silent=args.silent,
        verbose=args.verbose,
        include=args.include,
        retry=args.retry,
        retry_delay=args.retry_delay,
        limit_rate=args.limit_rate,
        proxy=args.proxy,
        auth=args.auth,
        auth_type=args.auth_type,
        max_bytes=args.max_bytes,
        json_on_success=args.json_on_success,
    )

    print(out)
    sys.exit(_exit_code(out))


if __name__ == "__main__":
    main()
