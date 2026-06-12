"""
SkyBro Tracker Service
Polls OpenSky Network for nearby aircraft and Open Notify for ISS passes.
Sends alerts via Pushover and/or Discord webhook.
Config is read from /data/config.json and hot-reloaded when it changes.
"""

import time, math, json, logging, sqlite3, requests, os, csv, io
from datetime import datetime, timezone
from pathlib import Path
from weather import fetch_weather, process_weather, process_moon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("skybro")

DATA_DIR  = Path("/data")
DB_PATH   = DATA_DIR / "skybro.db"
AC_DB     = DATA_DIR / "aircraft_db.json"
CFG_PATH  = DATA_DIR / "config.json"

WEATHER_INTERVAL = 900   # 15 minutes
_weather_last    = 0.0
MOON_INTERVAL    = 3600  # 1 hour
_moon_last       = 0.0

# ── Default config (overridden by config.json) ───────────────────────────────
DEFAULTS = {
    "home_lat":           1.3521,
    "home_lon":           103.8198,
    "radius_km":          15.0,
    "alt_threshold_ft":   5000.0,
    "poll_interval":      15,
    "iss_check_hours":    2,
    "iss_warn_mins":      20,
    "pushover_token":     "",
    "pushover_user":      "",
    "discord_webhook":    "",
    "opensky_user":       "",
    "opensky_pass":       "",
    "alerts_enabled":     True,
    "iss_alerts_enabled": True,
}

cfg = dict(DEFAULTS)
_cfg_mtime = 0.0

def load_config():
    global cfg, _cfg_mtime
    if not CFG_PATH.exists():
        save_config(DEFAULTS)
        return
    try:
        mtime = CFG_PATH.stat().st_mtime
        if mtime == _cfg_mtime:
            return
        with open(CFG_PATH) as f:
            loaded = json.load(f)
        cfg = {**DEFAULTS, **loaded}
        _cfg_mtime = mtime
        log.info("Config reloaded")
    except Exception as e:
        log.warning(f"Config load error: {e}")

def save_config(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CFG_PATH, "w") as f:
        json.dump(data, f, indent=2)

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS live_aircraft (
            icao24 TEXT PRIMARY KEY,
            callsign TEXT, lat REAL, lon REAL,
            alt_ft REAL, speed_kts REAL, heading REAL,
            vertical_rate REAL, origin_country TEXT,
            model TEXT, registration TEXT,
            dist_km REAL, updated INTEGER
        );
        CREATE TABLE IF NOT EXISTS seen_aircraft (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao24 TEXT, callsign TEXT,
            first_seen INTEGER, last_seen INTEGER,
            min_alt_ft REAL, min_dist_km REAL,
            origin_country TEXT, model TEXT, registration TEXT,
            alerted INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS iss_alerts (
            pass_time INTEGER PRIMARY KEY,
            duration INTEGER,
            alerted INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS weather_current (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS weather_hourly  (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS weather_daily   (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS moon_phase      (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
    """)
    conn.commit()
    conn.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(lon2-lon1)/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def bearing_to_compass(deg):
    return ["N","NE","E","SE","S","SW","W","NW"][round((deg % 360) / 45) % 8]

def bearing_from_home(lat, lon):
    dlat = lat - cfg["home_lat"]
    dlon = lon - cfg["home_lon"]
    return math.degrees(math.atan2(dlon, dlat))

# ── Aircraft DB ───────────────────────────────────────────────────────────────
_ac_db = {}

def load_aircraft_db():
    global _ac_db
    if not AC_DB.exists():
        download_aircraft_db()
        return
    try:
        with open(AC_DB) as f:
            _ac_db = json.load(f)
        log.info(f"Aircraft DB loaded: {len(_ac_db):,} entries")
    except Exception as e:
        log.warning(f"Aircraft DB load error: {e}")

def download_aircraft_db():
    log.info("Downloading OpenSky aircraft database…")
    url = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
    try:
        r = requests.get(url, timeout=120, stream=True)
        r.raise_for_status()
        db = {}
        lines = (line.decode("utf-8", errors="replace") for line in r.iter_lines())
        for row in csv.DictReader(lines):
            icao = row.get("icao24","").strip().lower()
            if not icao: continue
            mfr   = (row.get("manufacturername","") or "").strip()
            model = (row.get("model","") or "").strip()
            reg   = (row.get("registration","") or "").strip()
            db[icao] = {
                "model": f"{mfr} {model}".strip() or "Unknown",
                "reg":   reg,
            }
        with open(AC_DB, "w") as f:
            json.dump(db, f)
        global _ac_db
        _ac_db = db
        log.info(f"Aircraft DB saved: {len(db):,} entries")
    except Exception as e:
        log.warning(f"Aircraft DB download failed: {e}")

def lookup(icao24):
    entry = _ac_db.get(icao24.lower(), {})
    return entry.get("model", "Unknown"), entry.get("reg", "")

# ── Notifications ─────────────────────────────────────────────────────────────
def send_pushover(title, message, priority=0):
    if not cfg.get("pushover_token") or not cfg.get("pushover_user"):
        return
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": cfg["pushover_token"], "user": cfg["pushover_user"],
            "title": title, "message": message, "priority": priority,
        }, timeout=10)
    except Exception as e:
        log.error(f"Pushover error: {e}")

def send_discord(title, description, color=0x4f7cff, fields=None):
    if not cfg.get("discord_webhook"):
        return
    try:
        requests.post(cfg["discord_webhook"], json={"embeds": [{
            "title": title, "description": description, "color": color,
            "fields": fields or [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "SkyBro • Singapore"},
        }]}, timeout=10)
    except Exception as e:
        log.error(f"Discord error: {e}")

def notify(title, body, fields=None, priority=0, color=0x4f7cff):
    if not cfg.get("alerts_enabled", True):
        return
    send_pushover(title, body, priority)
    send_discord(title, body, color, fields)
    log.info(f"Alert sent: {title}")

# ── OpenSky ───────────────────────────────────────────────────────────────────
_alerted = set()

def fetch_states():
    pad = cfg["radius_km"] / 111.0 * 1.3
    params = {
        "lamin": cfg["home_lat"] - pad, "lomin": cfg["home_lon"] - pad,
        "lamax": cfg["home_lat"] + pad, "lomax": cfg["home_lon"] + pad,
    }
    auth = (cfg["opensky_user"], cfg["opensky_pass"]) if cfg.get("opensky_user") else None
    try:
        r = requests.get("https://opensky-network.org/api/states/all",
                         params=params, auth=auth, timeout=20)
        r.raise_for_status()
        return r.json().get("states") or []
    except Exception as e:
        log.warning(f"OpenSky error: {e}")
        return []

def process_states(states):
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM live_aircraft")

    for s in states:
        icao24, callsign = s[0], (s[1] or "").strip() or s[0].upper()
        lat, lon = s[6], s[5]
        on_ground = s[8]
        if lat is None or lon is None or on_ground:
            continue

        alt_ft    = round((s[7] or 0) * 3.28084)
        speed_kts = round((s[9] or 0) * 1.944)
        heading   = round(s[10] or 0)
        vrate     = s[11] or 0
        country   = s[2] or ""
        dist_km   = round(haversine(cfg["home_lat"], cfg["home_lon"], lat, lon), 2)
        model, reg = lookup(icao24)

        c.execute("""INSERT OR REPLACE INTO live_aircraft VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (icao24, callsign, lat, lon, alt_ft, speed_kts, heading,
             vrate, country, model, reg, dist_km, now))

        if (dist_km <= cfg["radius_km"] and
                alt_ft <= cfg["alt_threshold_ft"] and
                icao24 not in _alerted):
            _alerted.add(icao24)
            direction = bearing_to_compass(bearing_from_home(lat, lon))
            vr_str = (f"↑ {abs(vrate*196.85):.0f} fpm" if vrate > 0.5
                      else f"↓ {abs(vrate*196.85):.0f} fpm" if vrate < -0.5
                      else "level")
            title = f"✈ {callsign} overhead"
            body  = f"{model} • {direction} at {dist_km} km\n{alt_ft:,} ft • {speed_kts} kts • {vr_str}"
            notify(title, body, color=0xE67E22, fields=[
                {"name": "Callsign",     "value": callsign,                   "inline": True},
                {"name": "Registration", "value": reg or "N/A",               "inline": True},
                {"name": "Model",        "value": model,                      "inline": True},
                {"name": "Distance",     "value": f"{dist_km} km {direction}", "inline": True},
                {"name": "Altitude",     "value": f"{alt_ft:,} ft",           "inline": True},
                {"name": "Speed",        "value": f"{speed_kts} kts",         "inline": True},
                {"name": "V/S",          "value": vr_str,                     "inline": True},
                {"name": "Country",      "value": country,                    "inline": True},
            ])
            c.execute("""INSERT INTO seen_aircraft
                (icao24,callsign,first_seen,last_seen,min_alt_ft,min_dist_km,
                 origin_country,model,registration,alerted)
                VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (icao24, callsign, now, now, alt_ft, dist_km, country, model, reg))

    c.execute("DELETE FROM live_aircraft WHERE updated < ?", (now - 90,))
    conn.commit()
    conn.close()

    live_icaos = {s[0] for s in states if s[6] and s[5]}
    for icao in list(_alerted):
        if icao not in live_icaos:
            _alerted.discard(icao)

# ── ISS ───────────────────────────────────────────────────────────────────────
_iss_last_check = 0.0

def check_iss():
    global _iss_last_check
    if not cfg.get("iss_alerts_enabled", True):
        return
    if time.time() - _iss_last_check < cfg["iss_check_hours"] * 3600:
        return
    _iss_last_check = time.time()
    try:
        r = requests.get(
            f"https://api.open-notify.org/iss-pass.json"
            f"?lat={cfg['home_lat']}&lon={cfg['home_lon']}&n=6&altitude=0",
            timeout=15)
        r.raise_for_status()
        passes = r.json().get("response", [])
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for p in passes:
            c.execute("INSERT OR IGNORE INTO iss_alerts (pass_time,duration,alerted) VALUES (?,?,0)",
                      (p["risetime"], p["duration"]))
        conn.commit(); conn.close()
        log.info(f"ISS: {len(passes)} passes fetched")
    except Exception as e:
        log.warning(f"ISS fetch error: {e}")

def dispatch_iss_alerts():
    if not cfg.get("iss_alerts_enabled", True):
        return
    now  = int(time.time())
    warn = cfg["iss_warn_mins"] * 60
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        "SELECT pass_time, duration FROM iss_alerts WHERE alerted=0 AND pass_time BETWEEN ? AND ?",
        (now, now + warn)).fetchall()
    for pass_time, duration in rows:
        mins = (pass_time - now) // 60
        dt   = datetime.fromtimestamp(pass_time).strftime("%H:%M")
        notify(
            "🛸 ISS flyover coming up",
            f"Visible pass in ~{mins} min (at {dt} local)\nDuration: {duration}s — look up!",
            priority=1, color=0x1abc9c,
            fields=[
                {"name": "Time",     "value": dt,             "inline": True},
                {"name": "In",       "value": f"{mins} min",  "inline": True},
                {"name": "Duration", "value": f"{duration}s", "inline": True},
            ])
        c.execute("UPDATE iss_alerts SET alerted=1 WHERE pass_time=?", (pass_time,))
    conn.commit(); conn.close()

# ── Weather ───────────────────────────────────────────────────────────────────
def maybe_update_weather():
    global _weather_last
    if time.time() - _weather_last < WEATHER_INTERVAL:
        return
    _weather_last = time.time()
    data = fetch_weather(cfg["home_lat"], cfg["home_lon"])
    conn = sqlite3.connect(DB_PATH)
    process_weather(data, conn)
    conn.close()

# ── Moon ──────────────────────────────────────────────────────────────────────
def maybe_update_moon():
    global _moon_last
    if time.time() - _moon_last < MOON_INTERVAL:
        return
    _moon_last = time.time()
    conn = sqlite3.connect(DB_PATH)
    process_moon(conn)
    conn.close()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_config()
    init_db()
    load_aircraft_db()
    log.info(f"SkyBro running | Home: {cfg['home_lat']}, {cfg['home_lon']} | "
             f"Radius: {cfg['radius_km']} km | Alt: {cfg['alt_threshold_ft']} ft")
    while True:
        load_config()
        try:
            states = fetch_states()
            process_states(states)
            check_iss()
            dispatch_iss_alerts()
            maybe_update_weather()
            maybe_update_moon()
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
        time.sleep(cfg["poll_interval"])

if __name__ == "__main__":
    main()
