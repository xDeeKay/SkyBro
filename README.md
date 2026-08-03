# SkyBro for Umbrel

A real-time sky tracker for your Umbrel home server. Runs 24/7 in the background tracking aircraft, satellites, weather, and the night sky, with push alerts straight to your phone.

## Requirements

- Umbrel 1.x
- Raspberry Pi 4 or 5 (arm64)
- Free [OpenSky Network](https://opensky-network.org) account with OAuth2 client credentials
- Free [n2yo.com](https://www.n2yo.com) API key for satellite pass predictions
- One or more [Apprise](https://github.com/caronc/apprise) notification targets for alerts (optional) — supports Pushover, Discord, Telegram, Slack, email, and 150+ other services

## Install

1. Open your Umbrel dashboard
2. Go to **App Store → Community App Stores**
3. Click **Add Community App Store** and paste:
   ```
   https://github.com/xDeeKay/SkyBro
   ```
4. Find **SkyBro** and click **Install**
5. Open the app and go to **⚙ Settings** to set your home coordinates and configure alerts

## What it tracks

| Feature | Source | Refresh |
|---|---|---|
| ✈️ Live aircraft | OpenSky Network (OAuth2) | Every 30s |
| 🛰️ Satellite passes | n2yo.com API (ISS, Hubble, Tiangong) and CelesTrak (Starlink trains) | Every 2h |
| ☁️ Weather | Open-Meteo (no key needed) | Every 15 min |
| ✨ Astronomy | Computed locally via ephem | Every 1h |
| 🌙 Moon phase & position | Computed locally | Every 1h |

## Alerts

Alerts are sent via [Apprise](https://github.com/caronc/apprise) (150+ notification services) for two independent categories, each with its own targets:
- ✈️ **Aircraft**: a plane enters your configured radius **and** is below your altitude threshold. Optional include/exclude filters (callsign, country, model, airframe type) let you e.g. mute a specific operator or only alert for military traffic. Includes an aircraft photo pulled from Planespotters.net (attached where the target service supports it).
- 🛰️ **Satellites**: an ISS pass is approaching within your configured lead time

Each category's message title/body is an editable template with placeholders (e.g. `{callsign}`, `{model}`, `{sat_name}`). Existing Pushover/Discord config from older versions is converted to Apprise targets automatically on upgrade. All settings are managed through the in-app Settings page. Changes apply within 15 seconds without restarting.

## Dashboard tabs

### ✈ Aircraft
Live map with real-time plane positions. Five per-airframe icon silhouettes (jet, widebody, helicopter, light prop, military) coloured orange normally and blue when inside your alert zone. Clicking a plane shows its callsign, model, registration, altitude, speed, distance, and photo.

### 📋 History
Card-based log of every aircraft that triggered an alert. Each card shows a Planespotters photo, model, registration, country, altitude, closest approach distance, duration, and GPS coordinates. Clicking a card draws the recorded flight path on the map as ghost plane icons and a dashed polyline. Cards can be starred as favourites, marked as seen, or deleted individually. Filter by favourites, seen, or search by callsign, model, registration, or country.

### 📊 Statistics
Overview counts, all-time records, a 24h activity chart, and top countries and aircraft models seen.

### 🛰️ Satellites
Upcoming passes for ISS, Hubble Space Telescope, and Tiangong CSS with countdown timers and pass details, plus Starlink train sightings via CelesTrak. Sub-tabs show a pass count per satellite. ISS push alerts still fire as normal.

### ☁ Weather
Current conditions, 24h hourly strip (starting from the current local hour), 7-day forecast, and an astronomy visibility score for tonight.

### ✨ Astronomy
- **Tonight**: dark window, Bortle sky quality, moon interference, and observation highlights
- **Moon**: phase arc, illumination, rise/set, and next full/new moon dates
- **Planets**: altitude, azimuth, magnitude, rise/set/transit times for all 7 planets
- **DSOs**: all 110 Messier objects with equipment filter (naked eye / binoculars / telescope) and horizon filter
- **Meteors**: 11 annual showers with active state, ZHR, radiant, and parent body

## Privacy & data sharing

SkyBro runs entirely on your own hardware, but several features work by querying public third-party APIs, and some of those queries include your configured location. This is unavoidable for services like satellite pass prediction or weather that need to know where you are. Here's exactly what leaves your network, and when.

Sent from the tracker in the background (runs continuously while SkyBro is running, independent of which dashboard tab is open):

| Service | What's sent | How often |
|---|---|---|
| OpenSky Network | An approximate bounding box around your configured location | Every aircraft poll (default ~15-30s) |
| Planespotters.net, or Wikipedia as a fallback | An aircraft's ICAO24 code, registration, or model name (no location data) | Once per newly-sighted aircraft, to fetch a photo |
| n2yo.com | Your exact configured coordinates | Every satellite pass check (default every 2h), only if you've configured an API key |
| CelesTrak | Nothing beyond a plain HTTPS request; it's a public data download | Every 6h, for Starlink pass data |
| Open-Meteo | Your exact configured coordinates | Every weather poll (~15 min) |
| Clear Outside, or lightpollutionmap.info as a fallback | Your exact configured coordinates | Every astronomy poll (~1h) |
| Your configured Apprise target(s) | The alert content (aircraft or satellite pass details, plus a photo attachment for aircraft alerts where the target supports it) | Only when an alert actually fires, and only for the category/target(s) you've configured |

Sent directly from your browser (not the server), so these requests originate from whatever device you're viewing the dashboard on:

| Service | What's sent | When |
|---|---|---|
| Nominatim (OpenStreetMap Foundation) | Your exact configured coordinates | Every time you load the dashboard, to show a location name in the header |
| Photon (komoot) | Your search text, plus map coordinates for result ranking | Only when you use the city/address search in Settings |

Nothing else leaves your network. Astronomy calculations (planets, deep-sky objects, meteor showers, moon phase, twilight times) run entirely locally via the `ephem` library, and all history, settings, and cached data stay in the local SQLite database and `config.json`.

If you'd rather not share your configured location with n2yo, leave its API key blank; satellite tracking will simply be disabled and every other feature keeps working normally.

## Running outside Umbrel

SkyBro is a normal two-container Docker Compose app; Umbrel packaging (`umbrel-app.yml`, the `app_proxy` service) just adapts it to umbrelOS. To self-host it directly:

```yaml
services:
  tracker:
    image: xdeekay/skybro-tracker:1.5.6
    restart: unless-stopped
    volumes:
      - ./data:/data
      - /etc/localtime:/etc/localtime:ro

  web:
    image: xdeekay/skybro-dashboard:1.5.6
    restart: unless-stopped
    volumes:
      - ./data:/data
      - /etc/localtime:/etc/localtime:ro
    depends_on:
      - tracker
    ports:
      - "7437:5000"
```

Open `http://<host>:7437`, walk through the first-run flow, and configure everything else from **⚙ Settings**. No file edits or container restarts needed for normal use.

## Data & configuration

All state lives under the single mounted volume at `/data`:

| File | Contents |
|---|---|
| `skybro.db` | SQLite database: history, flight paths, satellite passes, weather, astronomy |
| `config.json` | All user settings (home location, alert radius/altitude, notification targets/filters/templates, API keys). Written by the dashboard, hot-reloaded by the tracker every poll cycle (≤30s) |
| `aircraft_db.json` | OpenSky aircraft model/registration lookup (~520k entries), auto-downloaded on first run if missing |

There are no required environment variables. All configuration is done through the Settings page in the browser and persisted to `config.json`. Optional env vars: `GIT_SHA` (dashboard only, build-time, only affects the dev version badge text) and `PORT` (dashboard only, default `5000`, the port gunicorn binds to inside the container).

**Ports**: the dashboard listens on `5000` inside the container by default (set `PORT` to change it); map it to whatever host port you want, e.g. `7437` above.

**Optional integrations** (all configured via Settings, app runs and degrades gracefully in-UI if any are missing):
- OpenSky Network OAuth2 client credentials: required for aircraft tracking
- n2yo.com API key: required for satellite pass predictions
- One or more Apprise target URLs: required for push alerts

## Maintenance

`refresh_photos.py` is a one-time, opt-in maintenance script that re-fetches Planespotters photos for every distinct aircraft in your history (useful after the photo-validation logic changed). It is **not** part of normal operation and does not run automatically:

```bash
docker exec -i skybro_tracker_1 python3 < refresh_photos.py
```
