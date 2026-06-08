"""
Road Bike Route Planner — FastAPI backend (BRouter edition).

Routing uses BRouter (https://brouter.de), which is free and needs NO API key.
For each request we generate a *custom BRouter profile* from a bundled
`fastbike` template, tuned to the user's settings, upload it, and route with it:

  * max_gradient      -> pins BRouter's uphill/downhill "cutoff" to the user's
                         limit and adds a strong cost above it, so the router
                         actively avoids exceeding that steepness.
  * road_calm (0-100) -> BRouter's `consider_traffic` (0.0-1.0): how hard to
                         avoid roads that usually carry traffic.
  * avoid_main_roads  -> bumps the cost of primary/secondary/tertiary roads so
                         highways and main roads are strongly avoided.
  * surface           -> asphalt/paved keep BRouter's strong unpaved avoidance;
                         "any" relaxes it.

BRouter returns 3D coordinates (lon, lat, elevation) plus per-segment WayTags,
so gradients, % paved and % on busy roads are computed directly — no separate
elevation API needed.
"""

import hashlib
import math
import os
import re
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

# BRouter needs no key. Override BROUTER_BASE_URL to point at a self-hosted
# instance or a mirror if the public server is busy.
BROUTER_BASE_URL = os.getenv("BROUTER_BASE_URL", "https://brouter.de").rstrip("/")
BROUTER_ROUTE_URL = f"{BROUTER_BASE_URL}/brouter"
BROUTER_PROFILE_URL = f"{BROUTER_BASE_URL}/brouter/profile"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "road-bike-route-planner/2.0 (private friends app)"

HERE = os.path.dirname(__file__)
TEMPLATE_PATH = os.path.join(HERE, "fastbike.brf")

MAX_TRACK_POINTS = 350
MIN_SAMPLE_SPACING_M = 90  # longer baseline -> less SRTM elevation noise
STEEP_REROUTE_THRESHOLD = 0.10
RIDE_SPEED_KMH = 20.0

# Profile-id cache so identical settings don't re-upload a profile every time.
_PROFILE_CACHE: Dict[str, str] = {}

with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
    FASTBIKE_TEMPLATE = fh.read()

app = FastAPI(title="Road Bike Route Planner (BRouter)")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# --------------------------------------------------------------------------- #
# Request model
# --------------------------------------------------------------------------- #
class PlanRequest(BaseModel):
    start: str = Field(..., description="Start place name")
    end: str = Field(..., description="End place name")
    max_gradient: float = Field(8.0, ge=1, le=15, description="Max gradient %")
    road_calm: int = Field(50, ge=0, le=100, description="Avoid busy roads, 0-100")
    avoid_main_roads: bool = Field(True, description="Strongly avoid highways/main roads")
    surface: str = Field("paved", description="asphalt|paved|any")


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def color_for_gradient(grad_pct: float) -> str:
    g = abs(grad_pct)
    if g <= 3:
        return "#2e9e5b"
    if g <= 7:
        return "#e8862e"
    return "#d83a34"


def resample_polyline(coords: List[List[float]], target_points: int) -> List[Dict[str, float]]:
    """Resample [[lon,lat,ele],...] into evenly spaced {lat,lon,ele} points."""
    if len(coords) < 2:
        c = coords[0]
        return [{"lat": c[1], "lon": c[0], "ele": c[2] if len(c) > 2 else 0.0}]

    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_m(coords[i - 1][1], coords[i - 1][0],
                                         coords[i][1], coords[i][0]))
    total = cum[-1]
    if total <= 0:
        c = coords[0]
        return [{"lat": c[1], "lon": c[0], "ele": c[2] if len(c) > 2 else 0.0}]

    spacing = max(MIN_SAMPLE_SPACING_M, total / max(1, target_points - 1))
    targets = [min(total, k * spacing) for k in range(int(total // spacing) + 1)]
    if not targets or targets[-1] < total:
        targets.append(total)

    out, seg = [], 1
    for t in targets:
        while seg < len(cum) - 1 and cum[seg] < t:
            seg += 1
        d0, d1 = cum[seg - 1], cum[seg]
        frac = 0.0 if d1 == d0 else (t - d0) / (d1 - d0)
        a, b = coords[seg - 1], coords[seg]
        ea = a[2] if len(a) > 2 else 0.0
        eb = b[2] if len(b) > 2 else 0.0
        out.append({
            "lat": a[1] + (b[1] - a[1]) * frac,
            "lon": a[0] + (b[0] - a[0]) * frac,
            "ele": ea + (eb - ea) * frac,
            "d": t,  # cumulative distance ALONG the path (m)
        })
    return out


# --------------------------------------------------------------------------- #
# Profile generation
# --------------------------------------------------------------------------- #
def _set_var(text: str, var: str, value) -> str:
    """Replace the default value of `assign <var> = ...` (keeps the %var% line)."""
    pattern = re.compile(rf"(assign\s+{re.escape(var)}\s*=\s*)([^#\n]+)")
    return pattern.sub(lambda m: f"{m.group(1)}{value}   ", text, count=1)


def _bump_highway_cost(text: str, key: str, value) -> str:
    """Raise the costfactor number after a `highway=<key> highway=<key>_link` switch."""
    pattern = re.compile(rf"(highway={key} highway={key}_link\s+)([\d.]+)")
    return pattern.sub(lambda m: f"{m.group(1)}{value}", text, count=1)


def build_profile(req: PlanRequest) -> str:
    """Create a tuned BRouter profile string from the fastbike template."""
    p = FASTBIKE_TEMPLATE

    # Busy-road avoidance: 0..100 slider -> 0.0..1.0 consider_traffic.
    consider = round(req.road_calm / 100.0, 2)
    p = _set_var(p, "consider_traffic", consider)

    # Gradient enforcement: count slope cost only above the user's max, and make
    # exceeding it expensive so the router seeks flatter roads.
    p = _set_var(p, "consider_elevation", "true")
    p = _set_var(p, "uphillcutoff", req.max_gradient)
    p = _set_var(p, "downhillcutoff", req.max_gradient)
    p = _set_var(p, "uphillcost", 220)
    p = _set_var(p, "downhillcost", 140)

    p = _set_var(p, "allow_motorways", "false")  # never route on motorways

    if req.avoid_main_roads:
        p = _bump_highway_cost(p, "primary", 8)
        p = _bump_highway_cost(p, "secondary", 3)
        p = _bump_highway_cost(p, "tertiary", 1.6)

    if req.surface == "any":
        # Relax BRouter's strong unpaved penalty (default keeps it for asphalt/paved).
        p = p.replace("isunpaved 10", "isunpaved 2.5")

    return p


async def get_profile_id(client: httpx.AsyncClient, profile_text: str) -> str:
    key = hashlib.md5(profile_text.encode("utf-8")).hexdigest()
    if key in _PROFILE_CACHE:
        return _PROFILE_CACHE[key]
    r = await client.post(BROUTER_PROFILE_URL, content=profile_text.encode("utf-8"), timeout=40)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"BRouter profile upload failed ({r.status_code}).")
    pid = r.json().get("profileid")
    if not pid:
        raise HTTPException(status_code=502, detail="BRouter did not return a profile id.")
    _PROFILE_CACHE[key] = pid
    return pid


# --------------------------------------------------------------------------- #
# External calls
# --------------------------------------------------------------------------- #
async def geocode(client: httpx.AsyncClient, place: str) -> Tuple[float, float, str]:
    r = await client.get(NOMINATIM_URL, params={"q": place, "format": "json", "limit": 1},
                         headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise HTTPException(status_code=404, detail=f"Could not geocode '{place}'.")
    return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name", place)


async def brouter_route(client: httpx.AsyncClient, points: List[Tuple[float, float]], profile_id: str) -> dict:
    """points is a list of (lat, lon). Returns the GeoJSON Feature."""
    lonlats = "|".join(f"{lon},{lat}" for (lat, lon) in points)
    r = await client.get(BROUTER_ROUTE_URL, params={
        "lonlats": lonlats, "profile": profile_id, "alternativeidx": 0, "format": "geojson",
    }, timeout=60)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"BRouter routing error ({r.status_code}): {r.text[:200]}")
    try:
        feat = r.json()["features"][0]
    except Exception:
        raise HTTPException(status_code=502, detail="BRouter returned no route.")
    return feat


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
PAVED_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "cycleway", "road",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}
BUSY_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
}


def _tag(waytags: str, key: str) -> Optional[str]:
    for tok in waytags.split():
        if tok.startswith(key + "="):
            return tok.split("=", 1)[1]
    return None


def surface_stats(feat: dict) -> Tuple[float, float]:
    """Return (% paved, % on busy roads) by distance, from BRouter messages."""
    msgs = feat.get("properties", {}).get("messages")
    if not msgs or len(msgs) < 2:
        return 0.0, 0.0
    header = msgs[0]
    try:
        di = header.index("Distance")
        wi = header.index("WayTags")
    except ValueError:
        return 0.0, 0.0

    total = paved = busy = 0.0
    for row in msgs[1:]:
        try:
            dist = float(row[di])
        except (ValueError, IndexError):
            continue
        tags = row[wi] if wi < len(row) else ""
        total += dist
        surface = _tag(tags, "surface")
        highway = _tag(tags, "highway")
        if surface:
            paved_like = surface in {
                "asphalt", "concrete", "paved", "paving_stones", "chipseal",
                "concrete:plates", "sett", "metal",
            }
        else:
            paved_like = (highway in PAVED_HIGHWAYS)
        if paved_like:
            paved += dist
        if highway in BUSY_HIGHWAYS:
            busy += dist
    if total <= 0:
        return 0.0, 0.0
    return round(100 * paved / total, 1), round(100 * busy / total, 1)


def _smooth_elevations(track: List[Dict[str, float]], window: int = 5) -> List[float]:
    """Moving-average the elevation profile to suppress SRTM spikes before
    computing gradients (raw elevation is kept on the track for GPX export)."""
    eles = [p["ele"] for p in track]
    n = len(eles)
    if n < 3:
        return eles
    half = window // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(eles[lo:hi]) / (hi - lo))
    return out


def analyze(track: List[Dict[str, float]], max_gradient: float) -> dict:
    features = []
    total_dist = steep_dist = weighted_grad = max_grad_seen = 0.0
    sm = _smooth_elevations(track)
    for i in range(1, len(track)):
        a, b = track[i - 1], track[i]
        seg_dist = b.get("d", 0) - a.get("d", 0)  # distance along the path
        draw_dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        if seg_dist <= 0 or draw_dist <= 0:
            continue
        grad = ((sm[i] - sm[i - 1]) / seg_dist) * 100.0
        total_dist += seg_dist
        weighted_grad += abs(grad) * seg_dist
        max_grad_seen = max(max_grad_seen, abs(grad))
        if abs(grad) > max_gradient:
            steep_dist += seg_dist
        features.append({
            "type": "Feature",
            "properties": {
                "gradient": round(grad, 1),
                "color": color_for_gradient(grad),
                "steep": abs(grad) > max_gradient,
            },
            "geometry": {"type": "LineString",
                         "coordinates": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]]},
        })
    steep_frac = (steep_dist / total_dist) if total_dist else 0.0
    avg_grad = (weighted_grad / total_dist) if total_dist else 0.0
    return {
        "geojson": {"type": "FeatureCollection", "features": features},
        "avg_gradient": round(avg_grad, 1),
        "max_gradient_seen": round(max_grad_seen, 1),
        "steep_percent": round(steep_frac * 100, 1),
        "steep_frac": steep_frac,
    }


async def route_and_analyze(client, pts, req, profile_text) -> Tuple[dict, List[Dict[str, float]], dict]:
    pid = await get_profile_id(client, profile_text)
    feat = await brouter_route(client, pts, pid)
    coords = feat["geometry"]["coordinates"]
    track = resample_polyline(coords, MAX_TRACK_POINTS)
    analysis = analyze(track, req.max_gradient)
    return feat, track, analysis


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok", "router": "brouter", "base_url": BROUTER_BASE_URL}


@app.post("/plan")
async def plan(req: PlanRequest):
    async with httpx.AsyncClient() as client:
        s_lat, s_lon, s_name = await geocode(client, req.start)
        e_lat, e_lon, e_name = await geocode(client, req.end)
        pts = [(s_lat, s_lon), (e_lat, e_lon)]

        profile_text = build_profile(req)
        feat, track, analysis = await route_and_analyze(client, pts, req, profile_text)
        rerouted = False

        # Enforce the gradient harder if too much is still too steep: regenerate
        # the profile with a stronger penalty above the limit and keep the
        # flatter result. (Some terrain genuinely has no flatter option.)
        if analysis["steep_frac"] > STEEP_REROUTE_THRESHOLD:
            try:
                strict = _set_var(profile_text, "uphillcost", 600)
                strict = _set_var(strict, "downhillcost", 320)
                feat2, track2, analysis2 = await route_and_analyze(client, pts, req, strict)
                if analysis2["steep_frac"] < analysis["steep_frac"]:
                    feat, track, analysis = feat2, track2, analysis2
                    rerouted = True
            except HTTPException:
                pass

        paved_pct, busy_pct = surface_stats(feat)
        props = feat.get("properties", {})

    try:
        dist_km = float(props.get("track-length", 0)) / 1000.0
    except (TypeError, ValueError):
        dist_km = 0.0
    try:
        elev_gain = round(float(props.get("filtered ascend", 0)))
    except (TypeError, ValueError):
        elev_gain = 0

    stats = {
        "distance_km": round(dist_km, 2),
        "elevation_gain_m": elev_gain,
        "percent_paved": paved_pct,
        "busy_road_percent": busy_pct,
        "avg_gradient": analysis["avg_gradient"],
        "max_gradient_seen": analysis["max_gradient_seen"],
        "steep_percent": analysis["steep_percent"],
        "ride_time_min": round((dist_km / RIDE_SPEED_KMH) * 60) if dist_km else 0,
        "rerouted": rerouted,
    }

    return {
        "geojson": analysis["geojson"],
        "track": track,
        "stats": stats,
        "start": {"lat": s_lat, "lon": s_lon, "name": s_name},
        "end": {"lat": e_lat, "lon": e_lon, "name": e_name},
    }
