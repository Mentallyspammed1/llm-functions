#!/usr/bin/env python3
# @describe Get location, address, map link, weather, and altitude.
import subprocess
import json
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime

def get_location(provider="gps", timeout=30) -> dict:
    """Helper to fetch location from a specific provider."""
    result = subprocess.check_output(
        ["termux-location", "-p", provider, "-r", "once"], 
        text=True, 
        timeout=timeout
    )
    return json.loads(result)

def run() -> str:
    """Get location, reverse geocode, generate map link, and fetch weather."""
    data = None
    provider_used = "gps"
    
    # 1. FETCH LOCATION (Fallback logic)
    try:
        data = get_location(provider="gps", timeout=15)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError):
        try:
            provider_used = "network"
            data = get_location(provider="network", timeout=10)
        except Exception as e:
            return f"Error: Could not get location via GPS or Network. Details: {e}"

    if not data:
        return "Error: Location data is empty."

    lat = data.get("latitude")
    lon = data.get("longitude")
    
    if lat is None or lon is None:
        return f"Error: Could not obtain valid coordinates (Provider: {provider_used})."

    # NEW: EXTRACT METADATA (Accuracy, Altitude, Speed)
    accuracy = data.get("accuracy", "Unknown")
    altitude = data.get("altitude", 0.0)
    speed = data.get("speed", 0.0)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Start building the output string
    output = f"🕒 Time: {timestamp}\n"
    output += f"📍 Coordinates: {lat}, {lon}\n"
    output += f"📡 Provider: {provider_used.upper()} (Accuracy: ~{accuracy}m)\n"
    
    # Only show altitude/speed if they are reasonably valid
    if altitude != 0.0:
        output += f"⛰️  Altitude: {altitude} meters\n"
    if speed > 0.5:
        output += f"🏃 Speed: {speed} m/s\n"

    # NEW: GOOGLE MAPS LINK
    output += f"🗺️  Google Maps: https://www.google.com/maps?q={lat},{lon}\n"
    
    # 2. REVERSE GEOCODING
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
        req = urllib.request.Request(url, headers={'User-Agent': 'TermuxLocationScript/4.0'})
        
        with urllib.request.urlopen(req, timeout=10) as response:
            geo_data = json.loads(response.read().decode('utf-8'))
            addr = geo_data.get("address", {})
            
            if addr:
                house_number = addr.get("house_number", "")
                road = addr.get("road", addr.get("pedestrian", ""))
                city = addr.get("city", addr.get("town", addr.get("village", "")))
                state = addr.get("state", "")
                
                if road:
                    street = f"{house_number} {road}".strip()
                    clean_address = f"{street}, {city}, {state}".strip(", ")
                    output += f"🏠 Address: {clean_address}"
                else:
                    output += f"🏠 Address: {geo_data.get('display_name', 'Unknown')}"
            else:
                output += "🏠 Address: Could not resolve to a known street address."
                
    except Exception as e:
        output += f"🏠 Address lookup failed: {e}"

    # NEW: FETCH LOCAL WEATHER (Using free Open-Meteo API, no key required)
    try:
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&temperature_unit=fahrenheit"
        weather_req = urllib.request.Request(weather_url, headers={'User-Agent': 'TermuxLocationScript/4.0'})
        
        with urllib.request.urlopen(weather_req, timeout=10) as weather_response:
            weather_data = json.loads(weather_response.read().decode('utf-8'))
            current = weather_data.get("current_weather", {})
            
            if current:
                temp = current.get("temperature")
                wind = current.get("windspeed")
                output += f"\n☁️  Weather: {temp}°F (Wind: {wind} mph)"
                
    except Exception as e:
        output += f"\n☁️  Weather lookup failed: {e}"

    return output

if __name__ == "__main__":
    print(run())