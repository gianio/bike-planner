# Ridgeline — Road Bike Route Planner

A small FastAPI + Leaflet web app that plans **quiet, paved, climb-aware** road
bike routes. Set a start and destination, your maximum gradient, how hard to
avoid busy roads, whether to shun main roads, and a surface preference — and get
an interactive map colour-coded by gradient, a stats summary, and a one-click
**GPX export** for your bike computer.

- **Backend:** FastAPI, single `POST /plan` endpoint returning GeoJSON + stats.
- **Frontend:** plain `index.html` (no framework) with Leaflet.js, mobile-friendly.
- **Routing:** **BRouter** — free, **no API key required**. For each request the
  app generates a *custom BRouter profile* tuned to your settings and routes with it.
- **Elevation:** comes straight from BRouter (SRTM-based 3D coordinates), so there's
  no separate elevation API to depend on.

---

## What the controls do

| Control | Effect on routing |
|---|---|
| **Max gradient (enforced)** | Pins BRouter's uphill/downhill slope cutoff to your limit and adds a strong cost above it, so the router actively seeks roads that stay under that steepness. (Genuinely steep terrain may have no flatter option — anything that can't be avoided is flagged red and counted.) |
| **Avoid busy roads** (0–100 slider) | Maps to BRouter's `consider_traffic`. Higher = avoid roads that usually carry traffic more aggressively. Replaces the old low/medium/high dropdown with a continuous, meaningful control. |
| **Avoid main roads** (toggle) | Forbids motorways and strongly raises the cost of primary/secondary/tertiary roads. |
| **Surface** | `Asphalt`/`Paved` keep BRouter's strong unpaved avoidance; `Any` relaxes it. |

The summary panel reports total distance, elevation gain, % paved, **% on busy
roads**, average gradient, the steepest gradient seen, and estimated ride time at
20 km/h. If more than 10% of the route exceeds your gradient, the app re-routes
once with a stronger slope penalty and keeps the flatter result.

> **Why BRouter (and a custom profile)?** BRouter is free with no key and lets us
> upload a tuned profile per request, so the gradient/traffic/highway preferences
> actually change the route. (The previous GraphHopper free tier rejected custom
> weighting — *"Free packages cannot use flexible mode."*) The profile is built
> from the bundled `fastbike.brf` template; tweak it if you want different defaults.

---

## Run locally

```bash
git clone https://github.com/<you>/bike-route-planner.git
cd bike-route-planner

pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000**. That's it — **no API key, no `.env` needed.**
A friend who clones the repo only needs `pip install -r requirements.txt` and
`uvicorn main:app --reload`.

(Optional: set `BROUTER_BASE_URL` in a `.env` to point at a self-hosted BRouter
instance instead of the public server. See `.env.example`.)

---

## Deploy to Railway in 3 steps

No Docker required — Railway's Nixpacks builder reads `requirements.txt`,
`Procfile` and `railway.toml` automatically. **No environment variables are
required** because BRouter needs no key.

**Step 1 — Push to GitHub**

```bash
git init
git add .
git commit -m "Ridgeline bike route planner"
git branch -M main
git remote add origin https://github.com/<you>/bike-route-planner.git
git push -u origin main
```

**Step 2 — Connect Railway**

1. Sign in at **https://railway.app** with GitHub.
2. **New Project → Deploy from GitHub repo** and pick this repository.
3. Railway installs dependencies and starts the app automatically.

**Step 3 — Generate a URL and share**

Under **Settings → Networking → Generate Domain**, create a public URL and send
that single link to your friends. There's no login, so keep the URL private.

`$PORT` is provided by Railway and used by both the `Procfile` and `railway.toml`
start commands.

---

## API reference

`POST /plan`

```json
{
  "start": "Chur, Switzerland",
  "end": "Lenzerheide",
  "max_gradient": 7,
  "road_calm": 80,
  "avoid_main_roads": true,
  "surface": "paved"
}
```

Returns GeoJSON route segments (each with `gradient` and `color`), a `track`
array (`lat`/`lon`/`ele`, used for GPX export), a `stats` object (distance,
elevation gain, % paved, % busy roads, average gradient, steepest gradient, ride
time, `rerouted` flag) and the geocoded `start`/`end` points.

`GET /health` reports the active BRouter base URL.

---

## Project layout

```
.
├── main.py            # FastAPI backend (/plan, /health, serves index.html)
├── index.html         # Leaflet frontend + GPX export (mobile bottom-sheet UI)
├── fastbike.brf       # BRouter profile template, tuned per request
├── requirements.txt
├── Procfile           # web: uvicorn main:app --host 0.0.0.0 --port $PORT
├── railway.toml       # Nixpacks build + start config
├── .env.example       # optional BROUTER_BASE_URL override
└── .gitignore
```

## Notes & troubleshooting

- **Geocoding** uses Nominatim (OpenStreetMap); if it fails, make the place name
  more specific (add the country).
- **Public BRouter limits** — `brouter.de` is a free community service; it's fine
  for a few friends. If it's busy or rate-limits you, set `BROUTER_BASE_URL` to a
  self-hosted instance.
- **Gradients are SRTM-derived** and lightly smoothed, so the steepest figure is a
  good estimate rather than survey-grade. "Max gradient" is a strong preference,
  not a hard guarantee — terrain with only steep roads will still be routed (and
  the steep parts shown in red).
- **Coverage** — BRouter's public server covers most of the world; very remote
  areas may lack routable data.
