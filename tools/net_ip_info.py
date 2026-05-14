import json
import socket
import subprocess
import os
import logging
from typing import Dict, List, Any

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_public_ip() -> str:
    """Get public IP address"""
    for url in ["https://api.ipify.org", "https://ipinfo.io/ip"]:
        try:
            result = subprocess.run(
                ["curl", "-s", url],
                capture_output=True,
                text=True,
                timeout=10,
                check=True
            )
            ip = result.stdout.strip()
            if ip:
                return ip
        except FileNotFoundError:
            logging.error("curl command not found. Please install curl.")
            return "N/A"
        except subprocess.CalledProcessError as e:
            logging.warning(f"curl failed for {url}: {e.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logging.warning(f"curl timed out for {url}.")
        except Exception as e:
            logging.warning(f"Unexpected error for {url}: {e}")
    return "N/A"

def get_network_interfaces() -> Dict[str, List[Dict[str, Any]]]:
    """Get network interface information"""
    interfaces: Dict[str, List[Dict[str, Any]]] = {}
    hostname = socket.gethostname()

    try:
        result = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True
        )
        data = json.loads(result.stdout)
        for iface_info in data:
            name = iface_info.get("ifname")
            if name and iface_info.get("operstate") == "UP":
                addrs = iface_info.get("addr_info", [])
                ipv4_addrs = []
                for a in addrs:
                    local_addr = a.get("local")
                    if a.get("family") == "inet" and local_addr and not local_addr.startswith("127."):
                        ipv4_addrs.append({
                            "address": local_addr,
                            "netmask": a.get("prefixlen", "N/A") 
                        })
                if ipv4_addrs:
                    interfaces[name] = ipv4_addrs
    except FileNotFoundError:
        logging.warning("`ip` command not found. Falling back to socket method.")
    except subprocess.CalledProcessError as e:
        logging.warning(f"`ip` command failed: {e.stderr.strip()}. Falling back.")
    except json.JSONDecodeError:
        logging.warning("Failed to parse `ip` output. Falling back.")
    except subprocess.TimeoutExpired:
        logging.warning("`ip` command timed out. Falling back.")
    except Exception as e:
        logging.warning(f"Unexpected error with `ip`: {e}. Falling back.")
    
    if not interfaces:
        try:
            ips = socket.gethostbyname_ex(hostname)[2]
            ips = [ip for ip in ips if not ip.startswith("127.")]
            if ips:
                interfaces["default"] = [{"address": ip, "netmask": "N/A"} for ip in ips]
        except Exception as e:
            logging.warning(f"Error during socket fallback: {e}")

    return interfaces

def run(public_ip: bool = True, interfaces: bool = True) -> Dict[str, Any]:
    """Get network IP information including public IP and network interfaces
    Args:
        public_ip: Include public IP address (default: true)
        interfaces: Include network interface information (default: true)
    """
    output: Dict[str, Any] = {}
    if public_ip:
        output["public_ip"] = get_public_ip()
    if interfaces:
        output["interfaces"] = get_network_interfaces()
    return output
