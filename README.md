# SkyBro for Umbrel

A real-time sky tracker for your Umbrel home server. Runs 24/7 in the background tracking aircraft, ISS flyovers, weather forecasts, and moon phases, with push alerts straight to your phone.

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
| ✈️ Live aircraft | OpenSky Network (free account recommended) | Every 30s |
| 🛰️ ISS flyovers | n2yo.com API | Every 2h |
| ☁️ Weather | Open-Meteo (no key needed) | Every 15 min |
| 🌙 Moon phase | Computed locally | Every 1h |

## Alerts

Alerts fire via **Pushover** and/or **Discord webhook** when:
- ✈️ A plane enters your configured radius **and** is below your altitude threshold
- 🛰️ An ISS pass is approaching within your configured lead time

All settings are managed through the in-app Settings page. No config file editing required. Changes apply within 15 seconds without restarting.

## Dashboard tabs

- **✈ Aircraft:** Live map with plane cards showing model, registration, altitude, speed, and vertical rate
- **🛰️ ISS:** Upcoming flyover passes with countdown timers
- **☁ Weather:** Current conditions, 24h hourly strip, 7-day forecast, and astronomy visibility score
- **🌙 Moon:** Phase, illumination percentage, and next full/new moon dates
- **📋 History:** Log of every plane that triggered an alert

## Requirements

- Umbrel 1.x
- Raspberry Pi 4 or 5 (arm64)
- Free [OpenSky Network](https://opensky-network.org) account (recommended to avoid rate limits)
- Free [n2yo.com](https://www.n2yo.com) API key for ISS pass predictions
- [Pushover](https://pushover.net) and/or Discord webhook for notifications (optional)
