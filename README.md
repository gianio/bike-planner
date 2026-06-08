# Ridgeline — Road Bike Route Planner

A small FastAPI + Leaflet web app that plans **quiet, paved, climb-aware** road
bike routes. Enter a start and destination, set your maximum gradient, traffic
tolerance and surface preference, and get an interactive map colour-coded by
gradient, a stats summary, and a one-click **GPX export** for your bike computer.

- **Backend:** FastAPI, single `POST /plan` endpoint returning GeoJSON + stats.
- **Frontend:** plain `index.html` (no framework) with Leaflet.js.
- **Routing:** GraphHopper Directions API using built-in **bike profiles**.
  `racingbike` prefers paved/asphalt roads, avoids motorways and busy roads, and
  is climb-averse — so it natively does most of what we want for a road bike.
- **Elevation:** Open-Elevation public API (with GraphHopper elevation as a
  fallback) to compute per-segment gradients.
- **Re-routing:** if more than 10% of the route is steeper than your limit, the
  app switches to the climb-averse `racingbike` profile to flatten the path.

---

## How it works

1. Start/end place names are geocoded with **Nominatim** (OpenStreetMap).
2. **GraphHopper** returns a route using a built-in bike profile. Your surface
   and traffic choices select the profile: road-bike preferences (asphalt/paved,
   or low/medium traffic) use **`racingbike`** (paved, quiet, climb-averse);
   only "any surface + high traffic" loosens to the general **`bike`** profile.
3. The route is resampled into evenly spaced points and elevations are fetched
   from **Open-Elevation**. Per-segment gradients are computed and each segment
   is coloured: green ≤ 3%, orange 3–7%, red > 7%. Surface details from
   GraphHopper give the % paved figure.
4. If too much of the route is too steep, the app re-routes with `racingbike`,
   which avoids steep climbs, and keeps whichever route is flatter.

> **Why profiles instead of a custom weighting model?** GraphHopper's **free
> tier only supports pre-built profiles** — "flexible mode" (custom models with
> `ch.disable`) is a paid feature and returns *"Free packages cannot use flexible
> mode."* The good news is the built-in `racingbike` profile is already tuned to
> prefer smooth paved roads, avoid motorways/busy roads, and penalise steep
> gradients, so it covers the traffic/surface/climb goals without a custom model.
> If you upgrade to a paid key, you can reintroduce a custom model for finer
> control over individual road classes and a slope penalty.

---

## 1. Get a free GraphHopper API key

1. Go to **https://www.graphhopper.com/** and click **Sign up** (free).
2. Open the **Dashboard → API Keys** and create a new key.
3. Copy the key — you'll paste it as `GRAPHHOPPER_API_KEY`.

The free tier includes a daily credit allowance that's plenty for a few friends.
Nominatim and the public Open-Elevation API need no key (just be polite with
request volume — both are free community services).

---

## 2. Run locally

```bash
git clone https://github.com/<you>/bike-route-planner.git
cd bike-route-planner

pip install -r requirements.txt

cp .env.example .env          # then edit .env and paste your key
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000** in your browser.

Your `.env` should contain:

```
GRAPHHOPPER_API_KEY=your_real_key_here
```

A friend who clones the repo only needs `pip install -r requirements.txt`, their
own free GraphHopper key in `.env`, and `uvicorn main:app --reload`.

---

## 3. Deploy to Railway in 3 steps

No Docker required — Railway's Nixpacks builder reads `requirements.txt`,
`Procfile` and `railway.toml` automatically.

**Step 1 — Push to GitHub**

```bash
git init
git add .
git commit -m "Ridgeline bike route planner"
git branch -M main
git remote add origin https://github.com/<you>/bike-route-planner.git
git push -u origin main
```

(`.env` is git-ignored, so your key never leaves your machine.)

**Step 2 — Connect Railway**

1. Sign in at **https://railway.app** with GitHub.
2. **New Project → Deploy from GitHub repo** and pick this repository.
3. Railway detects Python, installs dependencies and starts the app.

**Step 3 — Set the environment variable and share**

1. In the project, open **Variables** and add:
   - **Key:** `GRAPHHOPPER_API_KEY`
   - **Value:** your GraphHopper key
2. Railway redeploys automatically. Under **Settings → Networking → Generate
   Domain**, create a public URL.
3. Send that single URL to your friends — there's no login, so anyone with the
   link can use it. Keep the URL private.

`$PORT` is provided by Railway and used by both the `Procfile` and
`railway.toml` start commands, so nothing else needs changing.

---

## API reference

`POST /plan`

```json
{
  "start": "Chur, Switzerland",
  "end": "Lenzerheide",
  "max_gradient": 8,
  "traffic": "low",
  "surface": "paved"
}
```

Returns GeoJSON route segments (each with `gradient` and `color`), a `track`
array (`lat`/`lon`/`ele`, used for GPX export), a `stats` object (distance,
elevation gain, % paved, average gradient, steepest gradient, ride time at
20 km/h, `rerouted` flag) and the geocoded `start`/`end` points.

`GET /health` reports whether the API key is configured.

---

## Project layout

```
.
├── main.py            # FastAPI backend (/plan, /health, serves index.html)
├── index.html         # Leaflet frontend + GPX export
├── requirements.txt
├── Procfile           # web: uvicorn main:app --host 0.0.0.0 --port $PORT
├── railway.toml       # Nixpacks build + start config
├── .env.example       # copy to .env and add your key
└── .gitignore
```

## Troubleshooting

- **"GRAPHHOPPER_API_KEY is not set"** — add it to `.env` (local) or Railway
  Variables (deployed).
- **"Free packages cannot use flexible mode"** — this app uses only built-in
  profiles, so a stock free key works. If you see it, you're on an older build
  that sent a custom model; pull the latest `main.py`.
- **Flat / missing elevation** — the public Open-Elevation API is occasionally
  down or rate-limited; the app falls back to GraphHopper's elevation data.
- **Geocoding fails** — make the place name more specific (add the country).
