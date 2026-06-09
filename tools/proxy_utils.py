import os

def get_proxies():
    """Returns the SOCKS5 proxy configuration to be used with the requests library."""
    # Defaulting to standard local Tor SOCKS5 proxy
    proxy_url = os.getenv("BYBIT_PROXY_URL", "socks5h://127.0.0.1:9050")
    return {
        "http": proxy_url,
        "https": proxy_url
    }
