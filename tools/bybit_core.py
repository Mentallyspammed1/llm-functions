import os
import requests
import json
import time
import hmac
import hashlib
import urllib.parse

def get_config():
    return {
        "api_key": os.environ.get("BYBIT_API_KEY", ""),
        "api_secret": os.environ.get("BYBIT_API_SECRET", ""),
        "base_url": "https://api.bybit.com",
        "use_tor": os.environ.get("BYBIT_USE_TOR", "true").lower() == "true",
        "proxies": {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
    }

def api_request(method, endpoint, params=None, signed=False):
    cfg = get_config()
    url = f"{cfg["base_url"]}{endpoint}"
    headers = {"Content-Type": "application/json"}
    proxies = cfg["proxies"] if cfg["use_tor"] else None
    
    if signed:
        ts = str(int(time.time() * 1000))
        rw = "20000"
        # For POST, sign the JSON body. For GET, sign the query string.
        if method == "POST":
            payload = json.dumps(params or {}, separators=(',', ':'))
        else:
            payload = urllib.parse.urlencode(params or {})
        
        sig = hmac.new(cfg["api_secret"].encode("utf-8"), (ts + cfg["api_key"] + rw + payload).encode("utf-8"), hashlib.sha256).hexdigest()
        headers.update({"X-BAPI-API-KEY": cfg["api_key"], "X-BAPI-SIGN": sig, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw})
    
    try:
        if method == "POST":
            resp = requests.post(url, data=json.dumps(params or {}, separators=(',', ':')), headers=headers, proxies=proxies, timeout=30)
        else:
            resp = requests.get(url, params=params, headers=headers, proxies=proxies, timeout=30)
        try: return resp.json()
        except: print(resp.text); return {"retCode": -1, "retMsg": "Not JSON: " + resp.text}
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}
