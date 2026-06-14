"""
SkyBro Tracker Service
Polls OpenSky Network for nearby aircraft and n2yo for ISS passes.
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

# ── Default config ────────────────────────────────────────────────────────────
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
    "opensky_client_id":     "",
    "opensky_client_secret": "",
    "n2yo_api_key":       "",
    "alerts_enabled":     True,
    "iss_alerts_enabled": True,
    "use_location_time":  False,
    "time_format":        "12h",
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
            dist_km REAL, updated INTEGER, photo_url TEXT
        );
        CREATE TABLE IF NOT EXISTS seen_aircraft (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao24 TEXT, callsign TEXT,
            first_seen INTEGER, last_seen INTEGER,
            min_alt_ft REAL, min_dist_km REAL,
            lat REAL, lon REAL,
            origin_country TEXT, model TEXT, registration TEXT,
            alerted INTEGER DEFAULT 0, photo_url TEXT
        );
        CREATE TABLE IF NOT EXISTS iss_alerts (
            pass_time INTEGER PRIMARY KEY,
            duration INTEGER,
            alerted INTEGER DEFAULT 0,
            start_az TEXT,
            max_el INTEGER,
            end_az TEXT
        );
        CREATE TABLE IF NOT EXISTS weather_current (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS weather_hourly  (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS weather_daily   (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS moon_phase      (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS source_status (
            source TEXT PRIMARY KEY,
            last_success INTEGER DEFAULT 0,
            last_error   INTEGER DEFAULT 0,
            status TEXT DEFAULT 'unknown',
            detail TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS photo_cache (
            icao24 TEXT PRIMARY KEY,
            photo_url TEXT,
            thumb_url TEXT,
            fetched INTEGER
        );
    """)
    conn.commit()
    conn.close()

def migrate_db():
    """Add columns introduced after initial release to existing tables."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for table, col in [("live_aircraft",  "photo_url TEXT"),
                       ("seen_aircraft",   "photo_url TEXT"),
                       ("seen_aircraft",   "lat REAL"),
                       ("seen_aircraft",   "lon REAL"),
                       ("iss_alerts",      "start_az TEXT"),
                       ("iss_alerts",      "max_el INTEGER"),
                       ("iss_alerts",      "end_az TEXT")]:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column already exists
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

# ── Source status ─────────────────────────────────────────────────────────────
def update_source_status(source, success, detail='', status_override=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = int(time.time())
        row = c.execute("SELECT last_success, last_error FROM source_status WHERE source=?",
                        (source,)).fetchone()
        prev_success = row[0] if row else 0
        prev_error   = row[1] if row else 0
        if success:
            c.execute("""INSERT OR REPLACE INTO source_status
                         (source, last_success, last_error, status, detail)
                         VALUES (?,?,?,?,?)""",
                      (source, now, prev_error, 'ok', ''))
        else:
            st = status_override or 'error'
            c.execute("""INSERT OR REPLACE INTO source_status
                         (source, last_success, last_error, status, detail)
                         VALUES (?,?,?,?,?)""",
                      (source, prev_success, now, st, detail))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"source_status update error: {e}")

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

def send_discord(title, description, color=0x4f7cff, fields=None, thumb_url=None):
    if not cfg.get("discord_webhook"):
        return
    embed = {
        "title": title, "description": description, "color": color,
        "fields": fields or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "SkyBro"},
    }
    if thumb_url:
        embed["thumbnail"] = {"url": thumb_url}
    try:
        requests.post(cfg["discord_webhook"], json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        log.error(f"Discord error: {e}")

def notify(title, body, fields=None, priority=0, color=0x4f7cff, thumb_url=None):
    if not cfg.get("alerts_enabled", True):
        return
    send_pushover(title, body, priority)
    send_discord(title, body, color, fields, thumb_url)
    log.info(f"Alert sent: {title}")

# ── Aircraft photos ───────────────────────────────────────────────────────────
def get_cached_photo(icao24):
    """Return cached thumb_url without making any HTTP request."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT thumb_url FROM photo_cache WHERE icao24=?",
                           (icao24,)).fetchone()
        conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""

def fetch_aircraft_photo(icao24):
    """Return (photo_url, thumb_url) from cache or Planespotters.net."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT photo_url, thumb_url FROM photo_cache WHERE icao24=?",
                       (icao24,)).fetchone()
    conn.close()
    if row is not None:
        return row[0] or "", row[1] or ""
    try:
        r = requests.get(f"https://api.planespotters.net/pub/photos/hex/{icao24}",
                         headers={"User-Agent": "SkyBro/1.0 (Raspberry Pi sky tracker; https://github.com/xDeeKay/SkyBro)"},
                         timeout=5)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if photos:
            photo_url = photos[0].get("link", "")
            tl = photos[0].get("thumbnail_large") or photos[0].get("thumbnail") or {}
            thumb_url = tl.get("src", "")
        else:
            photo_url, thumb_url = "", ""
    except Exception as e:
        log.debug(f"Photo fetch {icao24}: {e}")
        photo_url, thumb_url = "", ""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO photo_cache (icao24,photo_url,thumb_url,fetched) VALUES (?,?,?,?)",
                 (icao24, photo_url, thumb_url, int(time.time())))
    conn.commit()
    conn.close()
    return photo_url, thumb_url

# ── OpenSky ───────────────────────────────────────────────────────────────────
_alerted = set()
_opensky_backoff_until = 0.0
_opensky_backoff_step  = 300  # 5 min starting step, doubles on each 429 up to 1 hr
_opensky_token         = None
_opensky_token_expiry  = 0.0

_OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                      "opensky-network/protocol/openid-connect/token")

def get_opensky_token():
    global _opensky_token, _opensky_token_expiry
    client_id     = cfg.get("opensky_client_id", "")
    client_secret = cfg.get("opensky_client_secret", "")
    if not client_id or not client_secret:
        return None
    if _opensky_token and time.time() < _opensky_token_expiry - 60:
        return _opensky_token
    try:
        r = requests.post(_OPENSKY_TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        _opensky_token        = data["access_token"]
        _opensky_token_expiry = time.time() + data.get("expires_in", 300)
        log.info("OpenSky: OAuth2 token acquired")
        return _opensky_token
    except Exception as e:
        log.warning(f"OpenSky token fetch error: {e}")
        _opensky_token = None
        _opensky_token_expiry = 0.0
        return None

def fetch_states():
    global _opensky_backoff_until, _opensky_backoff_step, _opensky_token, _opensky_token_expiry
    if time.time() < _opensky_backoff_until:
        log.debug(f"OpenSky backoff: {int(_opensky_backoff_until - time.time())}s remaining")
        return []
    pad = cfg["radius_km"] / 111.0 * 1.3
    params = {
        "lamin": cfg["home_lat"] - pad, "lomin": cfg["home_lon"] - pad,
        "lamax": cfg["home_lat"] + pad, "lomax": cfg["home_lon"] + pad,
    }

    def _request():
        token = get_opensky_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return requests.get("https://opensky-network.org/api/states/all",
                            params=params, headers=headers, timeout=20)

    try:
        r = _request()
        if r.status_code == 401:
            # Token expired mid-session — clear cache and retry once
            _opensky_token = None
            _opensky_token_expiry = 0.0
            log.info("OpenSky: 401 received, refreshing token and retrying")
            r = _request()
        if r.status_code == 429:
            wait_mins = _opensky_backoff_step // 60
            _opensky_backoff_until = time.time() + _opensky_backoff_step
            log.warning(f"OpenSky rate limited (429) — backing off {wait_mins} min "
                        f"(next step: {min(_opensky_backoff_step*2, 3600)//60} min)")
            _opensky_backoff_step = min(_opensky_backoff_step * 2, 3600)
            update_source_status('opensky', False,
                                 f"Rate limited — retry in {wait_mins} min", 'backoff')
            return []
        r.raise_for_status()
        _opensky_backoff_step  = 300
        _opensky_backoff_until = 0.0
        states = r.json().get("states") or []
        update_source_status('opensky', True)
        return states
    except Exception as e:
        log.warning(f"OpenSky error: {e}")
        update_source_status('opensky', False, str(e))
        return []

def process_states(states):
    now = int(time.time())

    # ── Phase 1: read photo cache (read-only, no write lock) ─────────────────
    visible = [s[0] for s in states if s[6] is not None and s[5] is not None and not s[8]]
    photo_cache_map = {}
    if visible:
        conn_r = sqlite3.connect(DB_PATH)
        ph = ','.join('?' * len(visible))
        for row in conn_r.execute(
                f"SELECT icao24, thumb_url FROM photo_cache WHERE icao24 IN ({ph})", visible):
            photo_cache_map[row[0]] = row[1] or ""
        conn_r.close()
    needs_photo = [icao for icao in visible if icao not in photo_cache_map]

    # ── Phase 2: write transaction — no HTTP, no secondary connections ────────
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM live_aircraft")
    alert_queue = []

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
        thumb_url  = photo_cache_map.get(icao24, "")

        c.execute("""INSERT OR REPLACE INTO live_aircraft VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (icao24, callsign, lat, lon, alt_ft, speed_kts, heading,
             vrate, country, model, reg, dist_km, now, thumb_url))

        if (dist_km <= cfg["radius_km"] and
                alt_ft <= cfg["alt_threshold_ft"] and
                icao24 not in _alerted):
            _alerted.add(icao24)
            direction = bearing_to_compass(bearing_from_home(lat, lon))
            vr_str = (f"↑ {abs(vrate*196.85):.0f} fpm" if vrate > 0.5
                      else f"↓ {abs(vrate*196.85):.0f} fpm" if vrate < -0.5
                      else "level")
            alert_queue.append({
                'icao24': icao24, 'callsign': callsign, 'lat': lat, 'lon': lon,
                'alt_ft': alt_ft, 'speed_kts': speed_kts, 'vrate': vrate,
                'vr_str': vr_str, 'dist_km': dist_km, 'country': country,
                'model': model, 'reg': reg, 'direction': direction,
            })
            c.execute("""INSERT OR IGNORE INTO seen_aircraft
                (icao24,callsign,first_seen,last_seen,min_alt_ft,min_dist_km,
                 lat,lon,origin_country,model,registration,alerted,photo_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (icao24, callsign, now, now, alt_ft, dist_km, lat, lon,
                 country, model, reg, thumb_url))

    c.execute("DELETE FROM live_aircraft WHERE updated < ?", (now - 90,))
    conn.commit()
    conn.close()
    # ── No DB connections open beyond this point until Phase 4 ───────────────

    # ── Phase 3: fetch photos — each call opens/closes its own connection ─────
    newly_fetched = {}
    for icao24 in needs_photo:
        _, thumb = fetch_aircraft_photo(icao24)
        if thumb:
            newly_fetched[icao24] = thumb
    for alert in alert_queue:
        icao24 = alert['icao24']
        if icao24 not in photo_cache_map and icao24 not in newly_fetched:
            _, thumb = fetch_aircraft_photo(icao24)
            if thumb:
                newly_fetched[icao24] = thumb

    # ── Phase 4: single write to update photo_urls ────────────────────────────
    if newly_fetched:
        conn_upd = sqlite3.connect(DB_PATH)
        for icao24, thumb in newly_fetched.items():
            conn_upd.execute(
                "UPDATE live_aircraft SET photo_url=? WHERE icao24=?", (thumb, icao24))
            conn_upd.execute(
                "UPDATE seen_aircraft SET photo_url=? WHERE icao24=? AND photo_url=''",
                (thumb, icao24))
        conn_upd.commit()
        conn_upd.close()

    # ── Phase 5: send notifications (no DB operations) ────────────────────────
    for alert in alert_queue:
        icao24    = alert['icao24']
        photo_url = newly_fetched.get(icao24) or photo_cache_map.get(icao24, "") or None
        title = f"✈ {alert['callsign']} overhead"
        body  = (f"{alert['model']} • {alert['direction']} at {alert['dist_km']} km\n"
                 f"{alert['alt_ft']:,} ft • {alert['speed_kts']} kts • {alert['vr_str']}")
        notify(title, body, color=0xE67E22, thumb_url=photo_url, fields=[
            {"name": "Callsign",     "value": alert['callsign'],                            "inline": True},
            {"name": "Registration", "value": alert['reg'] or "N/A",                        "inline": True},
            {"name": "Model",        "value": alert['model'],                               "inline": True},
            {"name": "Distance",     "value": f"{alert['dist_km']} km {alert['direction']}", "inline": True},
            {"name": "Altitude",     "value": f"{alert['alt_ft']:,} ft",                    "inline": True},
            {"name": "Speed",        "value": f"{alert['speed_kts']} kts",                  "inline": True},
            {"name": "V/S",          "value": alert['vr_str'],                              "inline": True},
            {"name": "Country",      "value": alert['country'],                             "inline": True},
        ])

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
    api_key = cfg.get("n2yo_api_key", "")
    if not api_key:
        log.info("ISS: n2yo API key not configured — skipping")
        update_source_status('iss', False, "No API key configured", 'no_key')
        return
    try:
        lat, lon = cfg["home_lat"], cfg["home_lon"]
        url = (f"https://api.n2yo.com/rest/v1/satellite/visualpasses/"
               f"25544/{lat}/{lon}/0/10/60/&apiKey={api_key}")
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        passes = r.json().get("passes") or []
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for p in passes:
            c.execute(
                "INSERT OR IGNORE INTO iss_alerts "
                "(pass_time,duration,alerted,start_az,max_el,end_az) VALUES (?,?,0,?,?,?)",
                (p["startUTC"], p["duration"],
                 p.get("startAzCompass"), p.get("maxEl"), p.get("endAzCompass"))
            )
        conn.commit(); conn.close()
        update_source_status('iss', True)
        log.info(f"ISS: {len(passes)} passes fetched from n2yo")
    except Exception as e:
        log.warning(f"ISS fetch error: {e}")
        update_source_status('iss', False, str(e))

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
            "🛰️ ISS flyover coming up",
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
    if data:
        update_source_status('weather', True)
    else:
        update_source_status('weather', False, 'Open-Meteo fetch failed')

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
    migrate_db()
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
