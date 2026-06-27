#!/usr/bin/env python3
# @describe: Perform an internet speed test to measure download and upload bandwidth.
# @param output_format: (Optional) Output format, either "json" (default) or "plain".
# @param verbose: (Optional) If true, include detailed diagnostic information.

import speedtest
import json
import sys
from datetime import datetime, timezone

def run(output_format: str = "json", verbose: bool = False):
    """
    Execute a speed test and output the result.

    Args:
        output_format: "json" for structured JSON output, "plain" for human readable.
        verbose: If true, include raw bytes and server details in output.
    """
    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        download_bps = st.download()
        upload_bps = st.upload()
        ping = st.results.ping

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "ping": ping,
            "download": round(download_bps / 1_000_000, 2),
            "upload": round(upload_bps / 1_000_000, 2),
            "bytes_downloaded": download_bps,
            "bytes_uploaded": upload_bps,
        }

        if verbose:
            server = st.get_best_server()
            print(f"[VERBOSE] Server: {server.hostname} ({server.country})")
            print(f"[VERBOSE] Ping: {ping:.2f} ms")
            print(f"[VERBOSE] Download: {download_bps} bits/sec ({download_bps/1_000_000:.2f} Mbps)")
            print(f"[VERBOSE] Upload: {upload_bps} bits/sec ({upload_bps/1_000_000:.2f} Mbps)")

        if output_format == "plain":
            print(f"Ping: {ping:.2f} ms")
            print(f"Download: {result['download']:.2f} Mbps")
            print(f"Upload: {result['upload']:.2f} Mbps")
        else:
            print(json.dumps(result, indent=2))

        return result if output_format == "json" else None

    except Exception as e:
        error = {"error": str(e)}
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

def main():
    """Entry point for direct CLI usage."""
    # 1. Parse JSON input directly if passed by aichat's tool dispatcher
    if len(sys.argv) > 1 and (sys.argv[1].startswith("{") or sys.argv[1].startswith("[")):
        try:
            kwargs = json.loads(sys.argv[1])
            if isinstance(kwargs, dict):
                output_format = kwargs.get("output_format", "json")
                verbose = kwargs.get("verbose", False)
                run(output_format, verbose)
            else:
                run(*kwargs)
            sys.exit(0)
        except Exception as err:
            print(json.dumps({"success": False, "error": f"JSON argument parse error: {err}"}))
            sys.exit(1)

    # 2. Fallback to standard CLI fallback
    fmt = "json"
    verbose = False
    if len(sys.argv) >= 2:
        fmt = sys.argv[1]
    if len(sys.argv) >= 3 and sys.argv[2].lower() == "verbose":
        verbose = True
    run(fmt, verbose)

if __name__ == "__main__":
    main()
