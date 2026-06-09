#!/usr/bin/env bash
set -e

# @describe Download files or websites using GNU Wget2
# @option --url! The URL to download
# @option --output-file -O File to save the output
# @flag --quiet -q Quiet mode
# @flag --verbose -v Verbose mode
# @flag --mirror -m Mirror a website (recursive)
# @option --limit-rate Limit bandwidth usage (e.g., 100k, 1M)
# @option --tries -t Number of retries (default: 20)
# @flag --no-check-certificate Don't validate the server's certificate

main() {
    local wget2_args=()
    
    # Termux CA Certs path
    local termux_certs="/data/data/com.termux/files/usr/etc/tls/cert.pem"
    if [[ -f "$termux_certs" ]]; then
        wget2_args+=("--ca-certificate" "$termux_certs")
    fi

    if [[ "$argc_no_check_certificate" == "true" ]]; then
        wget2_args+=("--no-check-certificate")
    fi
    if [[ -n "$argc_output_file" ]]; then
        wget2_args+=("-O" "$argc_output_file")
    fi
    
    if [[ "$argc_quiet" == "true" ]]; then
        wget2_args+=("-q")
    fi
    
    if [[ "$argc_verbose" == "true" ]]; then
        wget2_args+=("-v")
    fi
    
    if [[ "$argc_mirror" == "true" ]]; then
        wget2_args+=("-m")
    fi
    
    if [[ -n "$argc_limit_rate" ]]; then
        wget2_args+=("--limit-rate" "$argc_limit_rate")
    fi
    
    if [[ -n "$argc_tries" ]]; then
        wget2_args+=("-t" "$argc_tries")
    fi

    # Execute wget2
    wget2 "${wget2_args[@]}" "$argc_url" 2>/dev/null
}

eval "$(argc --argc-eval "$0" "$@")"
