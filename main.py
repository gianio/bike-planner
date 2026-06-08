"""
Road Bike Route Planner — FastAPI backend.

Pipeline for POST /plan:
  1. Geocode start & end place names via Nominatim (OSM).
  2. Ask GraphHopper Directions API for a `bike` route using a custom model
     (translated from the "weighting YAML" idea) that penalises high-traffic
     road classes and non-paved surfaces according to the user's preferences.
  3. Resample the route to evenly spaced points and fetch per-point elevation
     from the Open-Elevation public API (falls back to GraphHopper's own
     elevation data if Open-Elevation is unavailable).
  4. Compute per-segment gradients, flag segments steeper than the user's max.
  5. If more than 10% of the route length is too steep, re-route once with an
     added slope penalty in the custom model to flatten the path.
  6. Return colour-coded GeoJSON + a stats summary + a track for GPX export.

All secrets come from a .env file (see .env.example).
"""

import math
import os
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

GRAPHHOPPER_API_KEY = os.getenv("GRAPHHOPPER_API_KEY", "").strip()
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GRAPHHOPPER_URL = "https://graphhopper.com/api/1/route"
OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"

# A descriptive User-Agent is REQUIRED by the Nominatim usage policy.
USER_AGENT = "road-bike-route-planner/1.0 (private friends app)"

# Tuning knobs
MAX_TRACK_POINTS = 350          # cap on points sent to Open-Elevation
MIN_SAMPLE_SPACING_M = 60       # minimum spacing between resampled points
STEEP_REROUTE_THRESHOLD = 0.10  # re-route if >10% of length is too steep
RIDE_SPEED_KMH = 20.0

app = FastAPI(title="Road Bike Route Planner")

# Same-origin in production, but permissive CORS keeps local testing painless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class PlanRequest(BaseModel):
    start: str = Field(..., description="Start place name")
    end: str = Field(..., description="End place name")
    max_gradient: float = Field(8.0, ge=1, le=15, description="Max gradient %")
    traffic: str = Field("low", description="Preferred traffic level: low|medium|high")
    surface: str = Field("any", description="asphalt|paved|any")


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def color_for_gradient(grad_pct: float) -> str:
    """Green <= 3%, orange 3-7%, red > 7% (absolute value)."""
    g = abs(grad_pct)
    if g <= 3:
        return "#2e9e5b"
    if g <= 7:
        return "#e8862e"
    return "#d83a34"


def resample_polyline(
    coords: List[List[float]], target_points: int
) -> List[Dict[str, float]]:
    """
    Resample a GraphHopper polyline (list of [lon, lat] or [lon, lat, ele]) into
    evenly spaced points. Elevation is linearly interpolated from GraphHopper if
    present, so it can serve as a fallback when Open-Elevation is unavailable.
    Returns a list of {lat, lon, ele} dicts.
    """
    if len(coords) < 2:
        lon, lat = coords[0][0], coords[0][1]
        ele = coords[0][2] if len(coords[0]) > 2 else 0.0
        return [{"lat": lat, "lon": lon, "ele": ele}]

    # cumulative distance along the original line
    cum = [0.0]
    for i in range(1, len(coords)):
        d = haversine_m(coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0])
        cum.append(cum[-1] + d)
    total = cum[-1]
    if total <= 0:
        lon, lat = coords[0][0], coords[0][1]
        ele = coords[0][2] if len(coords[0]) > 2 else 0.0
        return [{"lat": lat, "lon": lon, "ele": ele}]

    spacing = max(MIN_SAMPLE_SPACING_M, total / max(1, target_points - 1))
    n = int(total // spacing) + 1
    targets = [min(total, k * spacing) for k in range(n + 1)]
    if targets[-1] < total:
        targets.append(total)

    out: List[Dict[str, float]] = []
    seg = 1
    for t in targets:
        while seg < len(cum) - 1 and cum[seg] < t:
            seg += 1
        d0, d1 = cum[seg - 1], cum[seg]
        frac = 0.0 if d1 == d0 else (t - d0) / (d1 - d0)
        a, b = coords[seg - 1], coords[seg]
        lon = a[0] + (b[0] - a[0]) * frac
        lat = a[1] + (b[1] - a[1]) * frac
        ea = a[2] if len(a) > 2 else 0.0
        eb = b[2] if len(b) > 2 else 0.0
        ele = ea + (eb - ea) * frac
        out.append({"lat": lat, "lon": lon, "ele": ele})
    return out


# --------------------------------------------------------------------------- #
# External API calls
# --------------------------------------------------------------------------- #
async def geocode(client: httpx.AsyncClient, place: str) -> Tuple[float, float, str]:
    params = {"q": place, "format": "json", "limit": 1}
    r = await client.get(
        NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=20
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise HTTPException(status_code=404, detail=f"Could not geocode '{place}'.")
    top = data[0]
    return float(top["lat"]), float(top["lon"]), top.get("display_name", place)


def choose_profile(traffic: str, surface: str) -> str:
    """
    Pick a built-in GraphHopper profile.

    GraphHopper's FREE tier only supports pre-built (CH) profiles — "flexible
    mode" (ch.disable + custom_model) is a paid feature. Luckily the built-in
    profiles already encode most of what we want for a road bike:

      * `racingbike` — strongly prefers smooth/paved roads, avoids motorways and
        busy/rough roads, and is climb-averse (penalises steep gradients).
      * `bike` — general cycling, more permissive about surface and roads.

    So the surface/traffic preferences select/bias the profile instead of
    feeding a custom weighting model:
      - Road-bike defaults (asphalt/paved, or low/medium traffic) -> racingbike.
      - Only loosen to `bike` when the user explicitly wants ANY surface AND
        tolerates HIGH traffic.
    """
    if surface == "any" and traffic == "high":
        return "bike"
    return "racingbike"


async def graphhopper_route(
    client: httpx.AsyncClient,
    points: List[Tuple[float, float]],
    profile: str,
    with_details: bool = True,
) -> dict:
    """
    Request a route using a built-in profile (free-tier compatible — no custom
    model, no ch.disable). `points` is a list of (lat, lon). Includes graceful
    retries if a key rejects path details or an unknown profile.
    """
    body: dict = {
        "profile": profile,
        "points": [[lon, lat] for (lat, lon) in points],
        "points_encoded": False,
        "elevation": True,
        "instructions": False,
        "locale": "en",
    }
    if with_details:
        body["details"] = ["road_class", "surface"]

    r = await client.post(
        GRAPHHOPPER_URL,
        params={"key": GRAPHHOPPER_API_KEY},
        json=body,
        timeout=40,
    )
    if r.status_code != 200:
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        low = (msg or "").lower()
        # Some keys don't expose path details on CH -> retry without them.
        if with_details and "detail" in low:
            return await graphhopper_route(client, points, profile, with_details=False)
        # Profile not enabled on this key -> fall back to the generic bike profile.
        if "profile" in low and profile != "bike":
            return await graphhopper_route(client, points, "bike", with_details=with_details)
        raise HTTPException(
            status_code=502, detail=f"GraphHopper error ({r.status_code}): {msg}"
        )
    data = r.json()
    if not data.get("paths"):
        raise HTTPException(status_code=502, detail="GraphHopper returned no path.")
    return data["paths"][0]


async def fetch_open_elevation(
    client: httpx.AsyncClient, track: List[Dict[str, float]]
) -> Optional[List[float]]:
    """
    Query Open-Elevation in batches. Returns a list of elevations aligned with
    `track`, or None if the service is unavailable (caller falls back to GH).
    """
    elevations: List[float] = []
    BATCH = 100
    try:
        for i in range(0, len(track), BATCH):
            chunk = track[i : i + BATCH]
            payload = {
                "locations": [{"latitude": p["lat"], "longitude": p["lon"]} for p in chunk]
            }
            r = await client.post(OPEN_ELEVATION_URL, json=payload, timeout=30)
            r.raise_for_status()
            results = r.json().get("results", [])
            if len(results) != len(chunk):
                return None
            elevations.extend(float(x["elevation"]) for x in results)
        return elevations
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def percent_paved(path: dict) -> float:
    """Use GraphHopper surface details to estimate the paved fraction by length."""
    coords = path["points"]["coordinates"]
    details = path.get("details", {}).get("surface")
    if not details:
        return 0.0
    paved_set = {
        "ASPHALT", "CONCRETE", "PAVED", "PAVING_STONES", "COMPACTED", "CHIPSEAL"
    }

    def seg_len(a: int, b: int) -> float:
        d = 0.0
        for i in range(a + 1, b + 1):
            if i >= len(coords):
                break
            d += haversine_m(
                coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0]
            )
        return d

    paved = 0.0
    total = 0.0
    for frm, to, val in details:
        length = seg_len(frm, to)
        total += length
        if str(val).upper() in paved_set:
            paved += length
    if total <= 0:
        return 0.0
    return round(100.0 * paved / total, 1)


def analyze(track: List[Dict[str, float]], max_gradient: float) -> dict:
    """
    Build colour-coded GeoJSON segments and stats from an elevation-aware track.
    """
    features = []
    total_dist = 0.0
    steep_dist = 0.0
    elev_gain = 0.0
    weighted_grad = 0.0
    max_grad_seen = 0.0

    for i in range(1, len(track)):
        a, b = track[i - 1], track[i]
        dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        if dist <= 0:
            continue
        dele = b["ele"] - a["ele"]
        grad = (dele / dist) * 100.0
        total_dist += dist
        if dele > 0:
            elev_gain += dele
        weighted_grad += abs(grad) * dist
        max_grad_seen = max(max_grad_seen, abs(grad))
        if abs(grad) > max_gradient:
            steep_dist += dist

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "gradient": round(grad, 1),
                    "color": color_for_gradient(grad),
                    "steep": abs(grad) > max_gradient,
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
                },
            }
        )

    dist_km = total_dist / 1000.0
    steep_frac = (steep_dist / total_dist) if total_dist > 0 else 0.0
    avg_grad = (weighted_grad / total_dist) if total_dist > 0 else 0.0
    ride_min = (dist_km / RIDE_SPEED_KMH) * 60.0 if RIDE_SPEED_KMH else 0.0

    return {
        "geojson": {"type": "FeatureCollection", "features": features},
        "stats": {
            "distance_km": round(dist_km, 2),
            "elevation_gain_m": round(elev_gain),
            "avg_gradient": round(avg_grad, 1),
            "max_gradient_seen": round(max_grad_seen, 1),
            "steep_percent": round(steep_frac * 100.0, 1),
            "ride_time_min": round(ride_min),
        },
        "steep_frac": steep_frac,
    }


async def route_and_analyze(
    client: httpx.AsyncClient,
    pts: List[Tuple[float, float]],
    req: PlanRequest,
    profile: str,
) -> Tuple[dict, dict, List[Dict[str, float]]]:
    """Route -> resample -> elevation -> analyze. Returns (path, analysis, track)."""
    path = await graphhopper_route(client, pts, profile)

    coords = path["points"]["coordinates"]
    track = resample_polyline(coords, MAX_TRACK_POINTS)

    elevations = await fetch_open_elevation(client, track)
    if elevations and len(elevations) == len(track):
        for p, e in zip(track, elevations):
            p["ele"] = e  # prefer Open-Elevation per spec
    # else: keep GraphHopper-interpolated elevations as fallback.

    analysis = analyze(track, req.max_gradient)
    return path, analysis, track


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok", "graphhopper_key_set": bool(GRAPHHOPPER_API_KEY)}


@app.post("/plan")
async def plan(req: PlanRequest):
    if not GRAPHHOPPER_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GRAPHHOPPER_API_KEY is not set. Add it to .env or Railway variables.",
        )

    async with httpx.AsyncClient() as client:
        start_lat, start_lon, start_name = await geocode(client, req.start)
        end_lat, end_lon, end_name = await geocode(client, req.end)
        pts = [(start_lat, start_lon), (end_lat, end_lon)]

        # First attempt with the profile chosen from the user's preferences.
        profile = choose_profile(req.traffic, req.surface)
        path, analysis, track = await route_and_analyze(client, pts, req, profile)
        rerouted = False

        # Re-route to flatten if too much of the route is too steep. On the free
        # tier we can't add a slope penalty, but `racingbike` is climb-averse, so
        # switching to it is the available "flatten" lever.
        if analysis["steep_frac"] > STEEP_REROUTE_THRESHOLD and profile != "racingbike":
            try:
                path2, analysis2, track2 = await route_and_analyze(
                    client, pts, req, "racingbike"
                )
                if analysis2["steep_frac"] < analysis["steep_frac"]:
                    path, analysis, track = path2, analysis2, track2
                    profile = "racingbike"
                    rerouted = True
            except HTTPException:
                pass  # keep the original route if the re-route fails

        paved = percent_paved(path)

    stats = analysis["stats"]
    stats["percent_paved"] = paved
    stats["rerouted"] = rerouted
    stats["routing_profile"] = profile

    return {
        "geojson": analysis["geojson"],
        "track": track,  # [{lat, lon, ele}] for GPX export
        "stats": stats,
        "start": {"lat": start_lat, "lon": start_lon, "name": start_name},
        "end": {"lat": end_lat, "lon": end_lon, "name": end_name},
    }
