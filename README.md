# SkyBro App Store for Umbrel

A personal Umbrel community app store containing **SkyBro** — a real-time sky
tracker for aircraft, ISS flyovers, weather, and moon phases.

## Install in Umbrel

1. Open your Umbrel dashboard
2. Go to **App Store → Community App Stores**
3. Click **Add Community App Store**
4. Paste your repo URL:
   ```
   https://github.com/YOUR_USERNAME/skybro-appstore
   ```
5. Find **SkyBro** in the app store and click **Install**
6. Once installed, open it and go to **⚙ Settings** to configure:
   - Your home coordinates (default is Singapore)
   - Alert radius and altitude threshold
   - Pushover and/or Discord notification credentials

## What SkyBro tracks

| Feature | Data source | Refresh rate |
|---|---|---|
| ✈️ Live aircraft | OpenSky Network (free, no key) | Every 15s |
| 🛸 ISS flyovers | Open Notify API (free, no key) | Every 2h |
| ☁️ Weather | Open-Meteo (free, no key) | Every 15 min |
| 🌙 Moon phase | Computed locally | Every 1h |

## Alerts

Alerts fire via **Pushover** and/or **Discord webhook** when:
- ✈️ A plane enters your radius **and** is below your altitude threshold
- 🛸 An ISS pass is approaching within your configured lead time

All thresholds and credentials are set in the in-app Settings page.

## After installing

Update the `HOME_LAT` and `HOME_LON` defaults in
`skybro-skybro/docker-compose.yml` to your actual coordinates before pushing,
or just set them via the Settings page after install — the tracker hot-reloads
config within 15 seconds.

## Gallery images

Umbrel expects `1.jpg`, `2.jpg`, `3.jpg` in the `skybro-skybro/` folder
(1440×900px screenshots of your running app). Add these after your first
install and push them to the repo.
