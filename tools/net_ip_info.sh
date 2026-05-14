#!/usr/bin/env bash
set -e

# @describe Get network IP information including public IP and network interfaces
# @flag --public-ip    Include public IP address (default: true)
# @flag --interfaces   Include network interface information (default: true)

main() {
    local output="{}"
    local fetch_public_ip=0
    local fetch_interfaces=0

    if [[ "$argc_public_ip" == "1" ]]; then
        fetch_public_ip=1
    elif [[ -z "$argc_public_ip" && -z "$argc_interfaces" ]]; then
        fetch_public_ip=1
    fi

    if [[ "$argc_interfaces" == "1" ]]; then
        fetch_interfaces=1
    elif [[ -z "$argc_public_ip" && -z "$argc_interfaces" ]]; then
        fetch_interfaces=1
    fi
    
    if [[ "$fetch_public_ip" -eq 1 ]]; then
        local public_ip
        public_ip=$(curl -s https://api.ipify.org 2>/dev/null) || public_ip="N/A"
        if [[ -z "$public_ip" ]]; then
            public_ip="N/A"
        fi
        output=$(echo "$output" | jq -c ".public_ip = \"$public_ip\"")
    fi
    
    if [[ "$fetch_interfaces" -eq 1 ]]; then
        local interfaces_json
        interfaces_json=$(ip -j addr show 2>/dev/null | jq -e '[.[] | select(.operstate == "UP")]' 2>/dev/null) || interfaces_json="[]"
        
        if [[ "$interfaces_json" == "null" ]]; then
            interfaces_json="[]"
        fi
        
        output=$(echo "$output" | jq -c ".interfaces = $interfaces_json")
    fi
    
    echo "$output" >> "${LLM_OUTPUT:-/dev/stdout}"
}

eval "$(argc --argc-eval "$0" "$@")"
