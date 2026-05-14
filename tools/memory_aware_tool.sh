file=tools/net_ip_info.js
/**
 * Get network IP information including public IP and network interfaces
 * @typedef {Object} Args
 * @property {boolean} public_ip - Include public IP address (default: true)
 * @property {boolean} interfaces - Include network interface information (default: true)
 * @param {Args} args
 */
const { execSync } = require("child_process");
const os = require("os");

function getPublicIP() {
  try {
    const ipify = execSync("curl -s https://api.ipify.org", { encoding: "utf8" }).trim();
    return ipify;
  } catch {
    try {
      const ipinfo = execSync("curl -s https://ipinfo.io/ip", { encoding: "utf8" }).trim();
      return ipinfo;
    } catch {
      return "N/A";
    }
  }
}

function getNetworkInterfaces() {
  const interfaces = os.networkInterfaces();
  const result = {};

  for (const [name, addrs] of Object.entries(interfaces)) {
    const validAddrs = addrs
      .filter((addr) => addr.family === "IPv4" && !addr.internal)
      .map((addr) => ({
        address: addr.address,
        netmask: addr.netmask,
        mac: addr.mac,
      }));

    if (validAddrs.length > 0) {
      result[name] = validAddrs;
    }
  }

  return result;
}

exports.run = function (args) {
  const { public_ip = true, interfaces = true } = args;
  const output = {};

  if (public_ip) {
    output.public_ip = getPublicIP();
  }

  if (interfaces) {
    output.interfaces = getNetworkInterfaces();
  }

  return output;
};
```

```py file=tools/net_ip_info.py
import json
import socket
import subprocess
import os


def get_public_ip() -> str:
    """Get public IP address"""
    try:
        result = subprocess.run(
            ["curl", "-s", "https://api.ipify.org"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["curl", "-s", "https://ipinfo.io/ip"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    return "N/A"


def get_network_interfaces() -> dict:
    """Get network interface information"""
    interfaces = {}
    hostname = socket.gethostname()

    try:
        # Try using ip command (Linux)
        result = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for iface in data:
                name = iface.get("ifname")
                if name and not iface.get("operstate") == "UNKNOWN":
                    addrs = iface.get("addr_info", [])
                    ipv4_addrs = [
                        {"address": a.get("local"), "netmask": a.get("prefixlen")}
                        for a in addrs
                        if a.get("family") == "inet" and not a.get("local", "").startswith("127.")
                    ]
                    if ipv4_addrs:
                        interfaces[name] = ipv4_addrs
    except Exception:
        pass

    # Fallback to socket method
    if not interfaces:
        try:
            ips = socket.gethostbyname_ex(hostname)[2]
            if not ips or ips == ["127.0.0.1"]:
                ips = []
            for ip in ips:
                if not ip.startswith("127."):
                    interfaces["default"] = [{"address": ip, "netmask": "N/A"}]
        except Exception:
            pass

    return interfaces


def run(public_ip: bool = True, interfaces: bool = True) -> dict:
    """Get network IP information including public IP and network interfaces
    Args:
        public_ip: Include public IP address (default: true)
        interfaces: Include network interface information (default: true)
    """
    output = {}

    if public_ip:
        output["public_ip"] = get_public_ip()

    if interfaces:
        output["interfaces"] = get_network_interfaces()

    return output
```

```sh file=tools/net_ip_info.sh
#!/usr/bin/env bash
set -e

# @describe Get network IP information including public IP and network interfaces
# @flag --public-ip    Include public IP address (default: true)
# @flag --interfaces   Include network interface information (default: true)

main() {
    local output="{}"
    
    if [[ "$ARG_PUBLIC_IP" == "1" ]] || [[ -z "$ARG_PUBLIC_IP" && -z "$ARG_INTERFACES" ]]; then
        local public_ip
        public_ip=$(curl -s https://api.ipify.org 2>/dev/null) || public_ip="N/A"
        output=$(echo "$output" | jq -c ".public_ip = \"$public_ip\"")
    fi
    
    if [[ "$ARG_INTERFACES" == "1" ]] || [[ -z "$ARG_PUBLIC_IP" && -z "$ARG_INTERFACES" ]]; then
        local interfaces_json
        interfaces_json=$(ip -j addr show 2>/dev/null | jq '[.[] | select(.operstate == "UP") | {name: .ifname, addresses: [.addr_info[] | select(.family == "inet") | {address: .local, prefix: .prefixlen}]}]' 2>/dev/null) || interfaces_json="[]"
        output=$(echo "$output" | jq -c ".interfaces = $interfaces_json")
    fi
    
    echo "$output" >> "$LLM_OUTPUT"
}

eval "$(argc --argc-eval "$0" "$@")"
