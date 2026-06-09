#!/usr/bin/env python3
# @describe Generate an interactive map from a set of coordinates.
# @option --coordinates! <TEXT> JSON, a Python literal list, or an address string.
# @option --style streets, satellite, dark, light, outdoors
# @option --markers circles, pins, heatmap, clusters
# @option --location-name <TEXT> Identifier used when saving a location.
# @option --retrieve-location <TEXT> Name of a previously saved location to overlay.
# @option --description <TEXT> Description for the saved location.
# @option --category <TEXT> Category for the saved location.
# @option --output <TEXT> Destination file path for the HTML map.
# @flag --use-termux-location Fetch the device's current GPS location.
# @flag --save-location Persist the generated map and its metadata.
# @flag --save-current-location Save the fetched GPS point for later retrieval.

import json
import os
import sys
import subprocess
import ast
import time
import math
from typing import Any, List, Literal, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.parse import quote

try:
    import folium
    from folium.plugins import HeatMap, MarkerCluster
except ImportError:
    folium = None

# ----------------------------------------------------------------------
# Tile map configurations
# ----------------------------------------------------------------------
STYLE_TILES = {
    "streets": "openstreetmap",
    "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/satellite/{z}/{x}/{y}",
    "dark": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}",
    "light": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}",
    "outdoors": "https://{s}.basemaps.cartocdn.com/outdoors_all/{z}/{x}/{y}",
}

CATEGORY_COLORS = {
    "home": "green",
    "work": "blue",
    "none": "red",
}

MAPS_DIR = os.path.join(os.path.expanduser("~"), ".config", "aichat", "maps")


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def list_saved_locations():
    save_dir = MAPS_DIR
    if not os.path.exists(save_dir):
        print("No saved locations found.")
        return

    print(f"{'Name':<20} | {'Description'}")
    print("-" * 40)
    found = False
    for filename in os.listdir(save_dir):
        if filename.endswith(".json"):
            name = filename[:-5]
            try:
                with open(os.path.join(save_dir, filename), "r") as f:
                    meta = json.load(f)
                    # We only want to list saved points
                    if validate_location_meta(meta):
                        found = True
                        print(f"{name:<20} | {meta.get('description', 'N/A')}")
            except Exception:
                continue
    if not found:
        print("No saved locations found.")

def delete_saved_location(name: str):
    save_dir = MAPS_DIR
    json_path = os.path.join(save_dir, name + ".json")
    html_path = os.path.join(save_dir, name + ".html")
    
    deleted = False
    if os.path.exists(json_path):
        os.remove(json_path)
        deleted = True
    if os.path.exists(html_path):
        os.remove(html_path)
        deleted = True
        
    if deleted:
        print(f"Location '{name}' deleted.")
    else:
        print(f"Location '{name}' not found.")

def update_location_description(name: str, new_description: str):
    save_dir = MAPS_DIR
    json_path = os.path.join(save_dir, name + ".json")
    
    if not os.path.exists(json_path):
        print(f"Location '{name}' not found.")
        return
        
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["description"] = new_description
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        print(f"Description for '{name}' updated.")
    except Exception as exc:
        print(f"Error updating description: {exc}")

def validate_location_meta(meta: dict) -> bool:
    required_keys = ["type", "lat", "lon"]
    return all(key in meta for key in required_keys)

def get_location_data(name: str) -> Optional[dict]:
    save_dir = MAPS_DIR
    json_path = os.path.join(save_dir, name + ".json")
    if not os.path.exists(json_path):
        return None
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_cache_path():
    return os.path.join(MAPS_DIR, "geocode_cache.json")

def load_geocode_cache():
    path = get_cache_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_geocode_cache(cache):
    path = get_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def get_termux_location() -> Optional[dict]:
    """
    Retrieve the current GPS coordinates using the Termux `termux-location` command.

    Returns
    -------
    dict : location data or None on failure
    """
    try:
        result = subprocess.check_output(["termux-location", "-p", "gps", "-r", "once"], text=True)
        return json.loads(result)
    except Exception as exc:  # pragma: no cover – defensive
        sys.stderr.write(f"Error getting termux location: {exc}\n")
        return None



def resolve_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Resolve a free‑form address to latitude/longitude using the OpenStreetMap
    Nominatim API, with caching.
    """
    cache = load_geocode_cache()
    if address in cache:
        return cache[address]["lat"], cache[address]["lon"]

    url = f"https://nominatim.openstreetmap.org/search?q={quote(address)}&format=json&limit=1"
    headers = {"User-Agent": "maps_tool/1.0 (Termux; aichat)"}
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=5) as response:
            data = json.load(response)
            if data:
                lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                cache[address] = {"lat": lat, "lon": lon}
                save_geocode_cache(cache)
                return lat, lon
    except Exception as exc:  # pragma: no cover – defensive
        sys.stderr.write(f"Error resolving address {address}: {exc}\n")
    return None



def load_coordinates(coords_input: Any) -> Optional[List[Tuple[float, float]]]:
    """
    Parse the ``coordinates`` argument which can be:

    * a JSON object (e.g. ``{\\\"features\\\": […]}``)
    * a Python literal list like ``[[lat, lon], …]``
    * a free‑form address string that can be geocoded

    Returns a list of ``(lat, lon)`` tuples ready for mapping.
    """
    if not isinstance(coords_input, str):
        return None

    # Try JSON first
    if coords_input.strip().startswith("{"):
        try:
            return json.loads(coords_input)
        except Exception:  # pragma: no cover – defensive
            pass

    # Try Python literal evaluation (list of [lat, lon] pairs)
    if "[" in coords_input and "]" in coords_input:
        try:
            parsed = ast.literal_eval(coords_input)
            if isinstance(parsed, list):
                return [(float(item[1]), float(item[0])) for item in parsed]  # type: ignore[arg-type]
        except Exception:  # pragma: no cover – defensive
            pass

    # Fallback to address geocoding
    coords = resolve_address(coords_input)
    return [(coords[0], coords[1])] if coords else None


# ----------------------------------------------------------------------
# Core mapping routine
# ----------------------------------------------------------------------
def run(
    coordinates: str,
    style: Literal["streets", "satellite", "dark", "light", "outdoors"] = "streets",
    markers: Literal["circles", "pins", "heatmap", "clusters"] = "circles",
    use_termux_location: bool = False,
    save_location: bool = False,
    save_current_location: bool = False,
    location_name: str = "auto",
    retrieve_location: Optional[str] = None,
    description: Optional[str] = None,
    category: Optional[str] = None,
    output: str = "stdout",
) -> None:

    """
    Generate an interactive map from a set of coordinates.

    Parameters
    ----------
    coordinates : str
        JSON, a Python literal list, or an address string.
    style : {"streets","satellite","dark","light","outdoors"}, default "streets"
        Map tile set to use.
    markers : {"circles","pins","heatmap","clusters"}, default "circles"
        Visual representation of the points.
    use_termux_location : bool, default False
        If True, fetch the device's current GPS location and map it.
    save_location : bool, default False
        Persist the generated map and its metadata to ``~/.config/aichat/maps``.
    save_current_location : bool, default False
        When ``use_termux_location`` is True, also save the fetched GPS point
        with ``location_name`` for later retrieval.
    location_name : str, default \"auto\"
        Identifier used when saving a location for later retrieval.
    retrieve_location : str or None, default None
        Name of a previously saved location to overlay on the new map.
    output : str, default "stdout"
        Destination file path for the HTML map, or ``stdout`` to print it.
    """


    if folium is None:  # pragma: no cover – defensive
        sys.stderr.write("folium library is not installed.\n")
        return

    # ------------------------------------------------------------------
    # Load a previously saved location (if requested) and overlay it
    # ------------------------------------------------------------------
    if retrieve_location is not None and retrieve_location != "none":
        try:
            meta_path = os.path.join(
                MAPS_DIR,
                retrieve_location + ".json",
            )


            os.makedirs(os.path.dirname(meta_path) or ".", exist_ok=True)
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if validate_location_meta(meta):
                description = meta.get("description", "")
                popup_text = f"Name: {retrieve_location}"

                if description:
                    popup_text += f"<br>Description: {description}"
                folium.Marker(
                    location=[meta["lat"], meta["lon"]],
                    icon=folium.Icon(color="green", icon="star"),
                    popup=folium.Popup(popup_text, parse_html=True),
                ).add_to(m)

        except FileNotFoundError:
            sys.stderr.write(f"Warning: location file not found: {meta_path}\n")
        except Exception as exc:  # pragma: no cover – defensive
            sys.stderr.write(f"Error loading retrieved location: {exc}\n")

    # ------------------------------------------------------------------
    # Build the coordinate list
    # ------------------------------------------------------------------
    if use_termux_location:
        loc = get_termux_location()
        if not loc:
            return
        lat, lon = float(loc["latitude"]), float(loc["longitude"])
        # Save the current location if requested
        if save_current_location:
            save_dir = MAPS_DIR
            os.makedirs(save_dir, exist_ok=True)
            meta_path = os.path.join(save_dir, location_name + ".json")
            meta_data = {
                "type": "point",
                "lat": lat,
                "lon": lon,
                "style": style,
                "markers": markers,
                "timestamp": time.time(),
                "full_data": loc,
                "description": description or "",
                "category": category or "none",
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, indent=2)
        coords_list: List[Tuple[float, float]] = [(lat, lon)]

    else:
        raw = load_coordinates(coordinates)
        if not raw:
            return
        if isinstance(raw, dict) and "features" in raw:
            coords_list = [
                (float(feat["geometry"]["coordinates"][1]), float(feat["geometry"]["coordinates"][0]))
                for feat in raw.get("features", [])
                if feat.get("geometry", {}).get("type") == "Point"
            ]
            # Handle MultiPoint as well
            for feat in raw.get("features", []):
                if feat.get("geometry", {}).get("type") == "MultiPoint":
                    for pt in feat["geometry"]["coordinates"]:
                        coords_list.append((float(pt[1]), float(pt[0])))
        elif isinstance(raw, list):
            coords_list = [(float(item[1]), float(item[0])) for item in raw]  # type: ignore[arg-type]
        else:
            sys.stderr.write("Unsupported coordinates format.\n")
            return

    if not coords_list:
        sys.stderr.write("No coordinates found.\n")
        return

    # ------------------------------------------------------------------
    # Determine map centre and create the Folium map object
    # ------------------------------------------------------------------
    center_lat = sum(c[0] for c in coords_list) / len(coords_list)
    center_lon = sum(c[1] for c in coords_list) / len(coords_list)
    tiles = STYLE_TILES.get(style, STYLE_TILES["streets"])
    m = folium.Map(location=[center_lat, center_lon], zoom_start=12, tiles=tiles)

    # ------------------------------------------------------------------
    # Add the selected marker type
    # ------------------------------------------------------------------
    color = CATEGORY_COLORS.get(category or "none", "red")
    if markers == "circles":
        for lat, lon in coords_list:
            folium.CircleMarker(location=[lat, lon], radius=5, color=color, fill=True).add_to(m)
    elif markers == "pins":
        for lat, lon in coords_list:
            folium.Marker(
                location=[lat, lon],
                icon=folium.Icon(color=color, icon="map-marker")
            ).add_to(m)
    elif markers == "heatmap":
        HeatMap(coords_list).add_to(m)
    elif markers == "clusters":
        MarkerCluster(coords_list).add_to(m)


    # ------------------------------------------------------------------
    # Persist or output the map
    # ------------------------------------------------------------------
    if save_location:
        save_dir = os.path.join(os.path.expanduser("~"), ".config", "aichat", "maps")
        os.makedirs(save_dir, exist_ok=True)
        map_path = os.path.join(save_dir, location_name + ".html")
        m.save(map_path)
        meta_data = {
            "name": location_name,
            "lat": center_lat,
            "lon": center_lon,
            "style": style,
            "markers": markers,
            "timestamp": time.time(),
        }
        meta_path = os.path.join(save_dir, location_name + ".json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, indent=2)
    else:
        if output and output != "stdout":
            m.save(output)
        else:
            print(m.get_root().render())


# ----------------------------------------------------------------------
# Command‑line interface (optional)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys
    import os

    # Check for argc environment variables first
    if "argc_coordinates" in os.environ or "argc_use_termux_location" in os.environ:
        kwargs = {}
        for k, v in os.environ.items():
            if k.startswith("argc_"):
                key = k[5:]
                # Cast common types
                if v.lower() == "true": val = True
                elif v.lower() == "false": val = False
                else:
                    try: val = float(v) if "." in v else int(v)
                    except: val = v
                kwargs[key] = val
        
        run(**kwargs)
        sys.exit(0)

    parser = argparse.ArgumentParser(description="Generate interactive maps from coordinates.")
    parser.add_argument("coordinates", nargs='?', help="Coordinates or address to map")
    parser.add_argument("--list", action="store_true", help="List all saved locations")
    parser.add_argument("--delete", help="Name of a saved location to delete")
    parser.add_argument("--update-description", help="New description for an existing location")
    parser.add_argument("--distance", help="Calculate distance between two locations (comma-separated, e.g., loc1,loc2)")
    parser.add_argument("--category", help="Category for the saved location (e.g., home, work)")
    parser.add_argument("--style", default="streets", help="Map style (streets, satellite, dark, light, outdoors)")

    parser.add_argument("--markers", default="circles", help="Marker type (circles, pins, heatmap, clusters)")
    parser.add_argument("--use-termux-location", action="store_true", help="Use termux-location for current GPS")
    parser.add_argument("--save-location", action="store_true", help="Save map with metadata")
    parser.add_argument("--save-current-location", action="store_true", help="Save the fetched GPS point for later retrieval")
    parser.add_argument("--location-name", default="auto", help="Name to save the location under")
    parser.add_argument("--retrieve-location", help="Name of a saved location to load")
    parser.add_argument("--description", help="Description for the saved location")
    parser.add_argument("--output", default="stdout", help="Output file path (default: stdout)")

    args = parser.parse_args()

    if args.list:
        list_saved_locations()
        sys.exit(0)

    if args.delete:
        delete_saved_location(args.delete)
        sys.exit(0)

    if args.update_description:
        if not args.location_name or args.location_name == "auto":
            parser.error("--update-description requires --location-name to specify the location")
        update_location_description(args.location_name, args.update_description)
        sys.exit(0)

    if args.distance:
        parts = args.distance.split(',')
        if len(parts) != 2:
            parser.error("--distance requires two location names separated by a comma")
        loc1 = get_location_data(parts[0])
        loc2 = get_location_data(parts[1])
        if not loc1 or not loc2:
            print("One or both locations not found.")
        else:
            dist = haversine(loc1["lat"], loc1["lon"], loc2["lat"], loc2["lon"])
            print(f"Distance between {parts[0]} and {parts[1]}: {dist:.2f} km")
        sys.exit(0)

    if not args.coordinates:
        parser.error("the following arguments are required: coordinates")

    run(
        coordinates=args.coordinates,
        style=args.style,
        markers=args.markers,
        use_termux_location=args.use_termux_location,
        save_location=args.save_location,
        save_current_location=args.save_current_location,
        location_name=args.location_name,
        retrieve_location=args.retrieve_location,
        description=args.description,
        category=args.category,
        output=args.output,
    )

