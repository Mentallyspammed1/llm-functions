#!/usr/bin/env python3
# @describe Download files or websites using GNU Wget2
# @option --url! The URL to download
# @option --output-file -O File to save the output
# @option --user-agent Custom User-Agent header
# @option --limit-rate Limit bandwidth usage (e.g., 100k, 1M)
# @option --tries -t Number of retries (default: 20)
# @option --waitretry Wait specified seconds between retries
# @flag   --quiet -q Quiet mode
# @flag   --verbose -v Verbose mode
# @flag   --mirror -m Mirror a website (recursive)
# @flag   --no-check-certificate Don't validate the server's certificate
# @env    LLM_OUTPUT=/dev/stdout The output path.
"""
wget2.py - Python tool wrapper around GNU Wget2
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
from typing import Optional

def run(
    url: str,
    output_file: Optional[str] = None,
    user_agent: Optional[str] = None,
    limit_rate: Optional[str] = None,
    tries: Optional[str] = None,
    waitretry: Optional[str] = None,
    quiet: bool = False,
    verbose: bool = False,
    mirror: bool = False,
    no_check_certificate: bool = False,
) -> None:
    output_target = os.environ.get("LLM_OUTPUT", "/dev/stdout")

    wget2_args = ["wget2"]
    
    # Termux CA Certs path
    termux_certs = Path("/data/data/com.termux/files/usr/etc/tls/cert.pem")
    if termux_certs.is_file():
        wget2_args.extend(["--ca-certificate", str(termux_certs)])

    wget2_args.append("--no-cookies")

    if no_check_certificate:
        wget2_args.append("--no-check-certificate")
    if output_file:
        wget2_args.extend(["-O", output_file])
    if user_agent:
        wget2_args.extend(["--user-agent", user_agent])
    if quiet:
        wget2_args.append("-q")
    if verbose:
        wget2_args.append("-v")
    if mirror:
        wget2_args.append("-m")
    if limit_rate:
        wget2_args.extend(["--limit-rate", limit_rate])
    if tries:
        wget2_args.extend(["-t", tries])
    if waitretry:
        wget2_args.extend(["--waitretry", waitretry])

    wget2_args.append(url)

    try:
        # Run wget2, inheriting stdout/stderr so output goes to user terminal/stream as normal
        res = subprocess.run(wget2_args)
        if res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, wget2_args)
    except Exception as exc:
        err_msg = json.dumps({"status": "error", "msg": f"wget2 download failed for {url}: {exc}"})
        try:
            with open(output_target, "w") as f:
                f.write(err_msg + "\n")
        except Exception:
            print(err_msg, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    # If JSON is passed as single argument by aichat's tool dispatcher:
    if len(sys.argv) == 2 and (sys.argv[1].startswith("{") or sys.argv[1].startswith("[")):
        try:
            kwargs = json.loads(sys.argv[1])
            # Normalize key names from hyphens to underscores if any
            normalized_kwargs = {}
            for k, v in kwargs.items():
                normalized_kwargs[k.replace("-", "_")] = v
                
            run(
                url=normalized_kwargs.get("url"),
                output_file=normalized_kwargs.get("output_file"),
                user_agent=normalized_kwargs.get("user_agent"),
                limit_rate=normalized_kwargs.get("limit_rate"),
                tries=normalized_kwargs.get("tries"),
                waitretry=normalized_kwargs.get("waitretry"),
                quiet=normalized_kwargs.get("quiet", False),
                verbose=normalized_kwargs.get("verbose", False),
                mirror=normalized_kwargs.get("mirror", False),
                no_check_certificate=normalized_kwargs.get("no_check_certificate", False),
            )
            sys.exit(0)
        except Exception as err:
            err_msg = json.dumps({"status": "error", "msg": f"JSON argument parse error: {err}"})
            out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
            try:
                with open(out_path, "w") as f:
                    f.write(err_msg + "\n")
            except Exception:
                print(err_msg, file=sys.stderr)
            sys.exit(1)

    # Standard CLI Parser
    parser = argparse.ArgumentParser(description="Download files or websites using GNU Wget2")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-file", "-O")
    parser.add_argument("--user-agent")
    parser.add_argument("--limit-rate")
    parser.add_argument("--tries", "-t")
    parser.add_argument("--waitretry")
    parser.add_argument("--quiet", "-q", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--mirror", "-m", action="store_true")
    parser.add_argument("--no-check-certificate", action="store_true")

    args = parser.parse_args()
    run(
        url=args.url,
        output_file=args.output_file,
        user_agent=args.user_agent,
        limit_rate=args.limit_rate,
        tries=args.tries,
        waitretry=args.waitretry,
        quiet=args.quiet,
        verbose=args.verbose,
        mirror=args.mirror,
        no_check_certificate=args.no_check_certificate,
    )
