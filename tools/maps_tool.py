#!/usr/bin/env python3
# ==============================================================================
# maps_tool.py — Interactive Mapper v2.4.0
# Structural alignment with edit.py + caching & validation
# ==============================================================================

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
import functools
import math
import ast
import subprocess
from typing import Any, Callable, Optional, List, Tuple
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

# Optional dependencies
try:
    import folium
    from folium.plugins import HeatMap, MarkerCluster
except ImportError:
    folium = None

# ==============================================================================
# SECTION 1: Logger & Constants
# ==============================================================================
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stderr))

MAPS_DIR = Path(os.path.expanduser("~")) / ".config" / "aichat" / "maps"
CACHE_FILE = MAPS_DIR / "geocode_cache.json"

STYLE_TILES = {
    "streets": "openstreetmap",
    "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/satellite/{z}/{x}/{y}",
    "dark": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}",
    "light": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}",
    "outdoors": "https://{s}.basemaps.cartocdn.com/outdoors_all/{z}/{x}/{y}",
}
CATEGORY_COLORS = {"home": "green", "work": "blue", "none": "red"}

class MapManager:
    """Secure map file manager with caching."""
    def __init__(self) -> None:
        self.maps_dir = MAPS_DIR.resolve()
        self.maps_dir.mkdir(parents=True, exist_ok=True)
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r") as f:
                    return json.load(f)
            except: pass
        return {}

    def save_cache(self):
        with open(CACHE_FILE, "w") as f:
            json.dump(self.cache, f)

    def validate_path(self, name: str) -> Path:
        if ".." in name or os.sep in name:
            raise ValueError("Invalid location name")
        return (self.maps_dir / f"{name}.json").resolve()

    def validate_coords(self, lat: float, lon: float):
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError(f"Invalid coordinates: ({lat}, {lon})")

_manager = MapManager()

# ==============================================================================
# SECTION 2: Decorators & Helpers
# ==============================================================================

def _timed(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        result = fn(*args, **kwargs)
        if not isinstance(result, dict):
            result = {"success": False, "error": "Operation returned non-dict"}
        result["duration_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        return result
    return wrapper

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def nominatim_request(path: str, params: dict[str, str]) -> Any:
    key = json.dumps(params, sort_keys=True)
    if key in _manager.cache:
        return _manager.cache[key]
        
    query = "&".join([f"{k}={quote(v)}" for k, v in params.items()])
    url = f"https://nominatim.openstreetmap.org/{path}?{query}"
    req = Request(url, headers={"User-Agent": "maps_tool/2.4"})
    
    try:
        with urlopen(req, timeout=5) as r:
            if r.status == 429:
                raise Exception("Rate limit exceeded (429)")
            data = json.load(r)
            _manager.cache[key] = data
            _manager.save_cache()
            return data
    except Exception as e:
        logger.error(f"Nominatim request failed: {e}")
        raise

def osrm_request(coordinates: str) -> Any:
    url = f"http://router.project-osrm.org/route/v1/driving/{coordinates}?overview=full&steps=true"
    req = Request(url, headers={"User-Agent": "maps_tool/2.4"})
    with urlopen(req, timeout=5) as r:
        return json.load(r)

def get_termux_location() -> Optional[Tuple[float, float]]:
    try:
        loc = json.loads(subprocess.check_output(["termux-location"], text=True))
        return (float(loc["latitude"]), float(loc["longitude"]))
    except: return None

def load_coordinates(coords_input: str) -> List[Tuple[float, float]]:
    if "[" in coords_input:
        try: return [(float(item[1]), float(item[0])) for item in ast.literal_eval(coords_input)]
        except: pass
    res = nominatim_request("search", {"q": coords_input, "format": "json", "limit": "1"})
    return [(float(res[0]["lon"]), float(res[0]["lat"]))] if res else []

# ==============================================================================
# SECTION 3: Operations
# ==============================================================================

@_timed
def get_directions(points: List[str]) -> dict[str, Any]:
    try:
        coords = []
        for p in points:
            res = nominatim_request("search", {"q": p, "format": "json", "limit": "1"})
            if res: coords.append((float(res[0]["lon"]), float(res[0]["lat"])))
        if len(coords) < 2: return {"success": False, "error": "Need at least 2 points"}
        
        coord_str = ";".join([f"{lon},{lat}" for lon, lat in coords])
        data = osrm_request(coord_str)
        
        if "routes" not in data or not data["routes"]:
            return {"success": False, "error": "No route found"}
            
        # Construct readable instructions
        steps = []
        for leg in data["routes"][0]["legs"]:
            for step in leg["steps"]:
                m = step["maneuver"]
                t = m.get("type", "")
                mod = m.get("modifier", "")
                name = step.get("name", "")
                dist = int(step.get("distance", 0))
                
                road_info = f" onto {name}" if name else ""
                dist_info = f" for {dist} meters" if dist > 0 else ""
                
                if t == "depart": instruction = f"Depart {mod}{road_info}{dist_info}."
                elif t == "arrive": instruction = "Arrive at destination."
                elif t == "turn": instruction = f"Turn {mod}{road_info}{dist_info}."
                elif t == "exit roundabout": instruction = f"Exit roundabout {mod}{road_info}{dist_info}."
                else: instruction = f"{t.replace('_', ' ').capitalize()} {mod}{road_info}{dist_info}."
                
                steps.append(instruction)
        
        return {"success": True, "instructions": steps}
    except Exception as e:
        return {"success": False, "error": str(e)}

@_timed
def search_places(query: str, limit: int = 5, save_name: Optional[str] = None) -> dict[str, Any]:
    try:
        data = nominatim_request("search", {"q": query, "format": "json", "limit": str(limit)})
        result = {"success": True, "results": data}
        if save_name and data:
            # Save first result
            path = _manager.validate_path(save_name)
            with open(path, "w") as f:
                json.dump({"type": "point", "lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]), "description": data[0]["display_name"]}, f)
            result["saved"] = save_name
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

@_timed
def reverse_search(lat: float, lon: float) -> dict[str, Any]:
    try:
        _manager.validate_coords(lat, lon)
        data = nominatim_request("reverse", {"lat": str(lat), "lon": str(lon), "format": "json"})
        return {"success": True, "result": data}
    except Exception as e:
        return {"success": False, "error": str(e)}

@_timed
def list_locations() -> dict[str, Any]:
    items = []
    for f in _manager.maps_dir.glob("*.json"):
        if f == CACHE_FILE: continue
        try:
            with open(f, "r") as fh:
                meta = json.load(fh)
                items.append({"name": f.stem, "description": meta.get("description", "N/A")})
        except: continue
    return {"success": True, "items": items}

@_timed
def delete_location(name: str) -> dict[str, Any]:
    try:
        (_manager.maps_dir / f"{name}.json").unlink(missing_ok=True)
        (_manager.maps_dir / f"{name}.html").unlink(missing_ok=True)
        return {"success": True, "message": f"Deleted {name}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@_timed
def update_description(name: str, description: str) -> dict[str, Any]:
    path = _manager.validate_path(name)
    if not path.exists():
        return {"success": False, "error": "Location not found"}
    with open(path, "r+") as f:
        meta = json.load(f)
        meta["description"] = description
        f.seek(0)
        json.dump(meta, f, indent=2)
    return {"success": True, "message": "Updated"}

@_timed
def calculate_distance(loc1: str, loc2: str) -> dict[str, Any]:
    try:
        l1 = json.load(open(_manager.validate_path(loc1)))
        l2 = json.load(open(_manager.validate_path(loc2)))
        dist = haversine(l1['lat'], l1['lon'], l2['lat'], l2['lon'])
        return {"success": True, "distance_km": dist}
    except Exception as e:
        return {"success": False, "error": str(e)}

@_timed
def create_map(
    coordinates: Optional[str] = None,
    style: str = "streets",
    markers: str = "circles",
    location_name: str = "auto",
    description: Optional[str] = None,
    category: Optional[str] = None,
    use_termux_location: bool = False,
    save_location: bool = False,
    visualization: str = "marker"
) -> dict[str, Any]:
    if folium is None: return {"success": False, "error": "Folium not installed"}
    
    coords_list = []
    if use_termux_location:
        loc = get_termux_location()
        if loc: coords_list.append(loc)
    elif coordinates:
        coords_list = load_coordinates(coordinates)
    
    if not coords_list:
        return {"success": False, "error": "No coordinates found."}

    m = folium.Map(location=coords_list[0], zoom_start=12, tiles=STYLE_TILES.get(style, "openstreetmap"))
    color = CATEGORY_COLORS.get(category or "none", "red")
    
    if visualization == "heatmap":
        HeatMap(coords_list).add_to(m)
    elif visualization == "cluster":
        mc = MarkerCluster().add_to(m)
        for lat, lon in coords_list: folium.Marker([lat, lon]).add_to(mc)
    else:
        for lat, lon in coords_list:
            if markers == "circles": folium.CircleMarker([lat, lon], radius=5, color=color).add_to(m)
            else: folium.Marker([lat, lon], icon=folium.Icon(color=color)).add_to(m)
        
    if save_location:
        path = _manager.validate_path(location_name)
        m.save(str(path.with_suffix(".html")))
        with open(path, "w") as f:
            json.dump({"type": "point", "lat": coords_list[0][0], "lon": coords_list[0][1], "description": description}, f)
        return {"success": True, "message": f"Saved as {location_name}"}
    else:
        return {"success": True, "map_html": m.get_root().render()}

@_timed
def batch(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute multiple map operations in sequence."""
    results = []
    for op_data in operations:
        op_data_copy = op_data.copy()
        operation = op_data_copy.pop("operation", None)
        if not operation: continue
        res = run(operation, **op_data_copy)
        results.append({"operation": operation, "result": res})
        if not res.get("success"): break
    return {"success": True, "results": results}

# ==============================================================================
# SECTION 4: Dispatcher
# ==============================================================================

def run(operation: Optional[str] = None, **kwargs: Any) -> dict[str, Any]:
    """Dispatcher entry point."""
    if operation is None:
        operation = kwargs.get("operation", "create")
        
    dispatch: dict[str, Callable[..., dict[str, Any]]] = {
        "list": list_locations,
        "delete": lambda: delete_location(kwargs.get("name", "")),
        "distance": lambda: calculate_distance(kwargs.get("loc1", ""), kwargs.get("loc2", "")),
        "update": lambda: update_description(kwargs.get("name", ""), kwargs.get("description", "")),
        "create": lambda: create_map(
            kwargs.get("coordinates"), kwargs.get("style", "streets"),
            kwargs.get("markers", "circles"), kwargs.get("name", "auto"),
            kwargs.get("description"), kwargs.get("category"),
            kwargs.get("use_termux_location", False), kwargs.get("save", False),
            kwargs.get("visualization", "marker")
        ),
        "search": lambda: search_places(kwargs.get("query", ""), kwargs.get("limit", 5), kwargs.get("save_search")),
        "reverse": lambda: reverse_search(kwargs.get("lat", 0.0), kwargs.get("lon", 0.0)),
        "batch": lambda: batch(kwargs.get("operations", [])),
        "directions": lambda: get_directions(kwargs.get("points", [])),
    }
    
    if operation not in dispatch:
        return {"success": False, "error": f"Unknown operation: {operation}"}
    
    try:
        return dispatch[operation]()
    except Exception as e:
        logger.exception("Error in operation %s", operation)
        return {"success": False, "error": str(e)}

# ==============================================================================
# SECTION 5: CLI
# ==============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maps_tool.py", description="Interactive Mapper")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    
    subparsers.add_parser("list", help="List saved locations")
    
    delete = subparsers.add_parser("delete", help="Delete a location")
    delete.add_argument("name", help="Location name")
    
    dist = subparsers.add_parser("distance", help="Calculate distance")
    dist.add_argument("loc1", help="First location")
    dist.add_argument("loc2", help="Second location")
    
    upd = subparsers.add_parser("update", help="Update description")
    upd.add_argument("name", help="Location name")
    upd.add_argument("description", help="New description")
    
    create = subparsers.add_parser("create", help="Create a map")
    create.add_argument("--coordinates", help="Coords/address")
    create.add_argument("--style", default="streets")
    create.add_argument("--markers", default="circles")
    create.add_argument("--name", default="auto", help="Location name")
    create.add_argument("--description")
    create.add_argument("--category")
    create.add_argument("--save", action="store_true")
    create.add_argument("--visualization", choices=["marker", "heatmap", "cluster"], default="marker")
    
    search = subparsers.add_parser("search", help="Find places by query")
    search.add_argument("query", help="Place to search for")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--save-search", help="Save first result as location")
    
    rev = subparsers.add_parser("reverse", help="Get place from coordinates")
    rev.add_argument("lat", type=float)
    rev.add_argument("lon", type=float)
    
    batch = subparsers.add_parser("batch", help="Run multiple operations")
    batch.add_argument("--operations", type=json.loads, help="JSON string of operations")
    
    direc = subparsers.add_parser("directions", help="Get text directions")
    direc.add_argument("points", nargs="+", help="List of points/addresses")
    
    return parser

if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    
    result = run(**vars(args))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("success") else 1)
