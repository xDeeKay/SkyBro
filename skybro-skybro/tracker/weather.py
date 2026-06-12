"""
SkyBro — Weather & Moon module
Fetches weather from Open-Meteo (no API key needed).
Computes moon phase locally.
"""

import math, time, json, logging, requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("skybro.weather")

# ── Open-Meteo ────────────────────────────────────────────────────────────────
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

WMO_CODES = {
    0:"Clear sky", 1:"Mainly clear", 2:"Partly cloudy", 3:"Overcast",
    45:"Fog", 48:"Icy fog",
    51:"Light drizzle", 53:"Drizzle", 55:"Heavy drizzle",
    61:"Slight rain", 63:"Rain", 65:"Heavy rain",
    71:"Slight snow", 73:"Snow", 75:"Heavy snow",
    80:"Slight showers", 81:"Showers", 82:"Heavy showers",
    95:"Thunderstorm", 96:"Thunderstorm w/ hail", 99:"Thunderstorm w/ heavy hail",
}

def wmo_desc(code):
    return WMO_CODES.get(int(code) if code is not None else 0, "Unknown")

def fetch_weather(lat, lon):
    params = {
        "latitude":  lat,
        "longitude": lon,
        "current": ",".join([
            "temperature_2m","relative_humidity_2m","apparent_temperature",
            "weather_code","wind_speed_10m","wind_direction_10m",
            "wind_gusts_10m","uv_index","precipitation","cloud_cover",
            "pressure_msl","visibility",
        ]),
        "hourly": ",".join([
            "temperature_2m","precipitation_probability","precipitation",
            "weather_code","wind_speed_10m","cloud_cover","uv_index",
            "visibility",
        ]),
        "daily": ",".join([
            "weather_code","temperature_2m_max","temperature_2m_min",
            "precipitation_sum","precipitation_probability_max",
            "wind_speed_10m_max","uv_index_max","sunrise","sunset",
        ]),
        "forecast_days": 7,
        "timezone": "Asia/Singapore",
        "wind_speed_unit": "kn",
    }
    try:
        r = requests.get(OPEN_METEO, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Open-Meteo fetch error: {e}")
        return None

def astronomy_score(cloud_cover, visibility_m, precip_mm, wind_kts):
    """0-100 score for sky-watching suitability."""
    score = 100
    score -= (cloud_cover or 0) * 0.7
    vis_km = (visibility_m or 10000) / 1000
    if vis_km < 10: score -= (10 - vis_km) * 3
    if precip_mm and precip_mm > 0: score -= min(precip_mm * 20, 40)
    if wind_kts and wind_kts > 15: score -= (wind_kts - 15) * 1.5
    return max(0, min(100, round(score)))

def score_label(score):
    if score >= 80: return "Excellent", "#2ecc71"
    if score >= 60: return "Good",      "#a8e063"
    if score >= 40: return "Fair",      "#f39c12"
    if score >= 20: return "Poor",      "#e67e22"
    return "Bad", "#e74c3c"

def process_weather(data, conn):
    if not data:
        return
    now_ts = int(time.time())
    cur    = data.get("current", {})
    hourly = data.get("hourly", {})
    daily  = data.get("daily", {})

    cloud  = cur.get("cloud_cover", 0) or 0
    vis    = cur.get("visibility", 10000) or 10000
    precip = cur.get("precipitation", 0) or 0
    wind   = cur.get("wind_speed_10m", 0) or 0
    astro  = astronomy_score(cloud, vis, precip, wind)
    astro_label, astro_color = score_label(astro)

    current_doc = {
        "ts":               now_ts,
        "temp":             cur.get("temperature_2m"),
        "feels_like":       cur.get("apparent_temperature"),
        "humidity":         cur.get("relative_humidity_2m"),
        "weather_code":     cur.get("weather_code"),
        "description":      wmo_desc(cur.get("weather_code")),
        "wind_speed_kts":   round(wind, 1),
        "wind_direction":   cur.get("wind_direction_10m"),
        "wind_gusts_kts":   cur.get("wind_gusts_10m"),
        "uv_index":         cur.get("uv_index"),
        "precipitation_mm": precip,
        "cloud_cover_pct":  cloud,
        "pressure_hpa":     cur.get("pressure_msl"),
        "visibility_m":     vis,
        "astronomy_score":  astro,
        "astronomy_label":  astro_label,
        "astronomy_color":  astro_color,
    }

    # Hourly next 24h
    times    = hourly.get("time", [])
    now_hour = datetime.now().strftime("%Y-%m-%dT%H:00")
    try:
        start_i = times.index(now_hour)
    except ValueError:
        start_i = 0

    hourly_list = []
    for i in range(start_i, min(start_i + 25, len(times))):
        def hget(key, idx):
            arr = hourly.get(key) or []
            return arr[idx] if idx < len(arr) else None
        h_astro = astronomy_score(
            hget("cloud_cover", i) or 0,
            hget("visibility", i) or 10000,
            hget("precipitation", i) or 0,
            hget("wind_speed_10m", i) or 0)
        hourly_list.append({
            "time":         times[i],
            "temp":         hget("temperature_2m", i),
            "precip_prob":  hget("precipitation_probability", i),
            "precipitation":hget("precipitation", i),
            "weather_code": hget("weather_code", i),
            "description":  wmo_desc(hget("weather_code", i)),
            "wind_kts":     round(hget("wind_speed_10m", i) or 0, 1),
            "cloud_cover":  hget("cloud_cover", i),
            "uv_index":     hget("uv_index", i),
            "astronomy_score": h_astro,
        })

    # Daily 7 days
    dtimes = daily.get("time", [])
    daily_list = []
    for i in range(len(dtimes)):
        def dget(key, idx):
            arr = daily.get(key) or []
            return arr[idx] if idx < len(arr) else None
        d_code   = dget("weather_code", i)
        d_precip = dget("precipitation_sum", i) or 0
        d_wind   = dget("wind_speed_10m_max", i) or 0
        d_astro  = astronomy_score(
            50 if d_code and d_code >= 2 else 10,
            10000, d_precip, d_wind)
        daily_list.append({
            "date":         dtimes[i],
            "weather_code": d_code,
            "description":  wmo_desc(d_code),
            "temp_max":     dget("temperature_2m_max", i),
            "temp_min":     dget("temperature_2m_min", i),
            "precip_sum":   d_precip,
            "precip_prob":  dget("precipitation_probability_max", i),
            "wind_max_kts": round(d_wind, 1),
            "uv_max":       dget("uv_index_max", i),
            "sunrise":      dget("sunrise", i),
            "sunset":       dget("sunset", i),
            "astronomy_score": d_astro,
        })

    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO weather_current (id, ts, data) VALUES (1, ?, ?)",
              (now_ts, json.dumps(current_doc)))
    c.execute("INSERT OR REPLACE INTO weather_hourly (id, ts, data) VALUES (1, ?, ?)",
              (now_ts, json.dumps(hourly_list)))
    c.execute("INSERT OR REPLACE INTO weather_daily (id, ts, data) VALUES (1, ?, ?)",
              (now_ts, json.dumps(daily_list)))
    conn.commit()
    log.info(f"Weather updated | {current_doc['description']} {current_doc['temp']}°C | "
             f"Astro: {astro_label} ({astro})")

# ── Moon phase ────────────────────────────────────────────────────────────────
def moon_phase(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)

    REF_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    LUNAR_CYCLE  = 29.53058867

    delta = dt - REF_NEW_MOON
    days  = delta.total_seconds() / 86400
    age   = days % LUNAR_CYCLE
    frac  = age / LUNAR_CYCLE

    illum = round((1 - math.cos(2 * math.pi * frac)) / 2 * 100)

    if   frac < 0.0338: name, emoji = "New Moon",        "🌑"
    elif frac < 0.2162: name, emoji = "Waxing Crescent", "🌒"
    elif frac < 0.2838: name, emoji = "First Quarter",   "🌓"
    elif frac < 0.4662: name, emoji = "Waxing Gibbous",  "🌔"
    elif frac < 0.5338: name, emoji = "Full Moon",       "🌕"
    elif frac < 0.7162: name, emoji = "Waning Gibbous",  "🌖"
    elif frac < 0.7838: name, emoji = "Last Quarter",    "🌗"
    elif frac < 0.9662: name, emoji = "Waning Crescent", "🌘"
    else:               name, emoji = "New Moon",        "🌑"

    def next_phase_ts(target_frac):
        remaining = (target_frac - frac) % 1.0
        if remaining < 0.01:
            remaining += 1.0
        next_dt = dt + timedelta(days=remaining * LUNAR_CYCLE)
        return int(next_dt.timestamp())

    return {
        "age_days":       round(age, 1),
        "illumination":   illum,
        "phase_name":     name,
        "phase_emoji":    emoji,
        "fraction":       round(frac, 4),
        "next_full_moon": next_phase_ts(0.5),
        "next_new_moon":  next_phase_ts(0.0),
        "is_full":        0.4662 <= frac <= 0.5338,
        "is_new":         frac < 0.0338 or frac > 0.9662,
    }

def process_moon(conn):
    data = moon_phase()
    data["ts"] = int(time.time())
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO moon_phase (id, ts, data) VALUES (1, ?, ?)",
              (data["ts"], json.dumps(data)))
    conn.commit()
    log.info(f"Moon: {data['phase_emoji']} {data['phase_name']} {data['illumination']}%")
