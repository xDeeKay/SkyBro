# SkyBro for Umbrel

A real-time sky tracker for your Umbrel home server. Runs 24/7 in the background tracking aircraft, satellites, weather, and the night sky, with push alerts straight to your phone.

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
| 🛰️ Satellite passes | n2yo.com API (ISS, Hubble, Tiangong) | Every 2h |
| ☁️ Weather | Open-Meteo (no key needed) | Every 15 min |
| 🔭 Astronomy | Computed locally via ephem | Every 1h |
| 🌙 Moon phase & position | Computed locally | Every 1h |

## Alerts

Alerts fire via **Pushover** and/or **Discord webhook** when:
- ✈️ A plane enters your configured radius **and** is below your altitude threshold
- 🛰️ An ISS pass is approaching within your configured lead time

Discord alerts include an aircraft photo pulled from Planespotters.net. All settings are managed through the in-app Settings page. Changes apply within 15 seconds without restarting.

## Dashboard tabs

### ✈ Aircraft
Live map with real-time plane positions. Five per-airframe icon silhouettes (jet, widebody, helicopter, light prop, military) coloured orange normally and blue when inside your alert zone. Clicking a plane shows its callsign, model, registration, altitude, speed, distance, and photo.

### 📋 History
Card-based log of every aircraft that triggered an alert. Each card shows a Planespotters photo, model, registration, country, altitude, closest approach distance, duration, and GPS coordinates. Clicking a card draws the recorded flight path on the map as ghost plane icons and a dashed polyline. Cards can be starred as favourites, marked as seen, or deleted individually. Filter by favourites, seen, or search by callsign, model, registration, or country.

### 🛰️ Satellites
Upcoming passes for ISS, Hubble Space Telescope, and Tiangong CSS with countdown timers and pass details. ISS push alerts still fire as normal.

### ☁ Weather
Current conditions, 24h hourly strip (starting from the current local hour), 7-day forecast, and an astronomy visibility score for tonight.

### 🔭 Astronomy
- **Tonight** — dark window, Bortle sky quality, moon interference, and observation highlights
- **Planets** — altitude, azimuth, magnitude, rise/set/transit times for all 7 planets
- **DSOs** — all 110 Messier objects with equipment filter (naked eye / binoculars / telescope) and horizon filter
- **Meteors** — 11 annual showers with active state, ZHR, radiant, and parent body
- **Moon** — phase arc, illumination, rise/set, and next full/new moon dates

## Requirements

- Umbrel 1.x
- Raspberry Pi 4 or 5 (arm64)
- Free [OpenSky Network](https://opensky-network.org) account with OAuth2 client credentials
- Free [n2yo.com](https://www.n2yo.com) API key for satellite pass predictions
- [Pushover](https://pushover.net) and/or Discord webhook for notifications (optional)
