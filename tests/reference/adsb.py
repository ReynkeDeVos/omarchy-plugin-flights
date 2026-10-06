"""Small keyless ADS-B client used by the journey tracker.

Public flight numbers and radio callsigns are not always the same: airlines
like Emirates or Turkish may transmit alphanumeric callsigns (UAE57X, THY5HX).
Lookups try FlightStats' callsign, the last pinned match, then adsbdb's.
"""

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any

ADSB_URL = "https://api.adsb.lol/v2"
ROUTES_URL = "https://api.adsbdb.com/v0"
USER_AGENT = "omarchy-plugin-flights/1.0 (+https://github.com/ReynkeDeVos/omarchy-plugin-flights)"

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "omarchy-flights",
)
ROUTE_TTL = 7 * 24 * 3600
PIN_TTL = 3 * 3600


def cache_path(kind: str, key: str) -> str:
    safe = "".join(character if character.isalnum() or character in "-_" else "_" for character in key)
    return os.path.join(CACHE_DIR, f"{kind}-{safe}.json")


def read_json(path: str, ttl: float = math.inf) -> Any | None:
    try:
        if time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json(path: str, value: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        os.replace(temporary, path)
    except OSError:
        pass


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def haversine_nm(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 3440.065
    phi_a, phi_b = math.radians(a_lat), math.radians(b_lat)
    delta_phi = phi_b - phi_a
    delta_lambda = math.radians(b_lon - a_lon)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))


def _airport(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if not node:
        return None
    return {"lat": node.get("latitude"), "lon": node.get("longitude")}


def resolve_route(ident: str) -> dict[str, Any] | None:
    key = ident.strip().upper()
    cached = read_json(cache_path("route", key), ROUTE_TTL)
    if cached is not None:
        return cached or None
    try:
        data = _get_json(f"{ROUTES_URL}/callsign/{key}")
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None
    response = (data or {}).get("response", {})
    route_data = response.get("flightroute") if isinstance(response, dict) else None
    if not route_data:
        write_json(cache_path("route", key), {})
        return None
    route = {
        "callsignIcao": route_data.get("callsign_icao") or key,
        "origin": _airport(route_data.get("origin")),
        "destination": _airport(route_data.get("destination")),
    }
    write_json(cache_path("route", key), route)
    return route


def _live_by_callsign(callsign: str) -> dict[str, Any] | None:
    if not callsign:
        return None
    try:
        data = _get_json(f"{ADSB_URL}/callsign/{callsign.strip().upper()}")
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None
    for aircraft in (data or {}).get("ac") or []:
        if aircraft.get("lat") is not None and aircraft.get("lon") is not None:
            return aircraft
    return None


def find_aircraft(
    ident: str,
    preferred_callsign: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the live aircraft and resolved route for an IATA flight number."""
    key = ident.strip().upper()
    route = resolve_route(key)
    candidates = [preferred_callsign, str(read_json(cache_path("pin", key), PIN_TTL) or "")]
    if route:
        candidates.append(str(route.get("callsignIcao") or ""))

    checked: set[str] = set()
    aircraft = None
    for candidate in candidates:
        callsign = candidate.strip().upper()
        if not callsign or callsign in checked:
            continue
        checked.add(callsign)
        aircraft = _live_by_callsign(callsign)
        if aircraft:
            break
    if aircraft:
        live_callsign = str(aircraft.get("flight") or "").strip().upper()
        if live_callsign:
            write_json(cache_path("pin", key), live_callsign)
    return aircraft, route
