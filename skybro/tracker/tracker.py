"""
SkyBro Tracker Service
Polls OpenSky Network for nearby aircraft and n2yo for ISS passes.
Sends alerts via Apprise (150+ notification services) per notification category.
Config is read from /data/config.json and hot-reloaded when it changes.
"""

import time, math, json, logging, sqlite3, requests, os, csv, io, re, uuid
from concurrent.futures import ThreadPoolExecutor
import apprise
import ephem
from datetime import datetime, timezone, timedelta
from pathlib import Path
from weather import fetch_weather, process_weather, process_moon
from astronomy import process_astronomy

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

WEATHER_INTERVAL   = 900    # 15 minutes
_weather_last      = 0.0
MOON_INTERVAL      = 3600   # 1 hour
_moon_last         = 0.0
ASTRONOMY_INTERVAL = 3600   # 1 hour
_astronomy_last    = 0.0
STARLINK_INTERVAL  = 6 * 3600  # 6 hours
_starlink_last     = 0.0
STARLINK_TLE_URL = "https://celestrak.org/NORAD/elements/supplemental/sup-gp.php?FILE=starlink&FORMAT=TLE"

# Satellites to track in addition to ISS (name, NORAD ID, send_alert)
SATELLITES = [
    ("ISS",      25544, True),
    ("Hubble",   20580, False),
    ("Tiangong", 48274, False),
]

# ── Default config ────────────────────────────────────────────────────────────
# Named, reusable title/body pairs. Targets reference one by id (linked
# reference — editing a template here updates every target using it) instead
# of each carrying its own copy. The two defaults are protected: always
# present, never deletable (see _clean_templates in app.py).
DEFAULT_TEMPLATES_LIST = [
    {
        "id": "default_aircraft", "name": "Default Aircraft", "kind": "aircraft",
        "title": "{emoji} {callsign} overhead",
        "body":  "{model} ({registration})\n{airframe} • {country}\n{speed} • {vertical_speed}",
    },
    {
        "id": "default_satellite", "name": "Default Satellite", "kind": "satellite",
        "title": "🛰️ {sat_name} flyover coming up",
        "body":  "Visible pass in ~{minutes} min (at {time} local)\nDuration: {duration}s. Look up!",
    },
]
_CATEGORY_DEFAULT_TEMPLATE_ID = {"aircraft": "default_aircraft", "satellites": "default_satellite"}

DEFAULTS = {
    "home_lat":           1.3521,
    "home_lon":           103.8198,
    "radius_km":          15.0,
    "alt_threshold_ft":   5000.0,
    "poll_interval":      15,
    "iss_check_hours":    2,
    "iss_warn_mins":      20,
    "opensky_client_id":     "",
    "opensky_client_secret": "",
    "n2yo_api_key":       "",
    "notifications": {
        "aircraft":   {"filters": "", "targets": []},
        "satellites": {"filters": "", "targets": []},
    },
    "templates": [dict(t) for t in DEFAULT_TEMPLATES_LIST],
    "use_location_time":  False,
    "time_format":        "12h",
}

cfg = dict(DEFAULTS)
_cfg_mtime = 0.0

def _parse_discord_webhook(url):
    """Convert a Discord webhook URL into an apprise discord://id/token target."""
    m = re.search(r'/webhooks/(\d+)/([^/?]+)', url or '')
    return f"discord://{m.group(1)}/{m.group(2)}" if m else None

def _migrate_legacy_notifications(raw_cfg):
    """One-time, idempotent conversion of the old Pushover/Discord fields into
    the new Apprise-based notifications structure. Migrated targets just point
    at the new default templates (no attempt to reproduce old message wording —
    the original Discord embed's inline field grid has no Apprise equivalent
    anyway). The old category-level alerts_enabled/iss_alerts_enabled applied
    uniformly to every target, so it's carried over as each migrated target's
    own `enabled` flag."""
    if "notifications" in raw_cfg:
        return raw_cfg, False
    urls = []
    if raw_cfg.get("pushover_token") and raw_cfg.get("pushover_user"):
        urls.append({"id": uuid.uuid4().hex[:8],
                      "url": f"pover://{raw_cfg['pushover_user']}@{raw_cfg['pushover_token']}"})
    discord_url = _parse_discord_webhook(raw_cfg.get("discord_webhook"))
    if discord_url:
        urls.append({"id": uuid.uuid4().hex[:8], "url": discord_url})
    aircraft_enabled   = raw_cfg.get("alerts_enabled", True)
    satellites_enabled = raw_cfg.get("iss_alerts_enabled", True)
    raw_cfg["notifications"] = {
        "aircraft": {
            "filters": "",
            "targets": [{**u, "enabled": aircraft_enabled,
                         "template_id": "default_aircraft", "template": {"title": "", "body": ""},
                         "filters": ""} for u in urls],
        },
        "satellites": {
            "filters": "",
            "targets": [{**u, "enabled": satellites_enabled,
                         "template_id": "default_satellite", "template": {"title": "", "body": ""},
                         "filters": ""} for u in urls],
        },
    }
    if "templates" not in raw_cfg:
        raw_cfg["templates"] = [dict(t) for t in DEFAULT_TEMPLATES_LIST]
    for k in ("pushover_token", "pushover_user", "discord_webhook",
              "alerts_enabled", "iss_alerts_enabled"):
        raw_cfg.pop(k, None)
    if urls:
        log.info(f"Migrated legacy Pushover/Discord config to {len(urls)} Apprise target(s)")
    return raw_cfg, True

def _seed_templates(raw_cfg):
    """Separate from _migrate_legacy_notifications: an install can already have
    a `notifications` key (e.g. from testing a pre-templates build) without yet
    having `templates`, so this must not be gated by that migration's own guard."""
    if "templates" not in raw_cfg:
        raw_cfg["templates"] = [dict(t) for t in DEFAULT_TEMPLATES_LIST]
        return True
    return False

def load_config():
    global cfg, _cfg_mtime, _weather_last, _moon_last, _astronomy_last, _sat_last_check, _starlink_last
    if not CFG_PATH.exists():
        save_config(DEFAULTS)
        return
    try:
        mtime = CFG_PATH.stat().st_mtime
        if mtime == _cfg_mtime:
            return
        prev_lat, prev_lon = cfg.get("home_lat"), cfg.get("home_lon")
        with open(CFG_PATH) as f:
            loaded = json.load(f)
        loaded, migrated = _migrate_legacy_notifications(loaded)
        seeded = _seed_templates(loaded)
        if migrated or seeded:
            save_config({**DEFAULTS, **loaded})
        cfg = {**DEFAULTS, **loaded}
        _cfg_mtime = CFG_PATH.stat().st_mtime if (migrated or seeded) else mtime
        log.info("Config reloaded")
        if (cfg.get("home_lat"), cfg.get("home_lon")) != (prev_lat, prev_lon):
            _weather_last = _moon_last = _astronomy_last = _sat_last_check = _starlink_last = 0.0
            log.info("Home location changed, forcing immediate weather/moon/astronomy/satellite refresh")
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
            dist_km REAL, updated INTEGER, photo_url TEXT,
            category INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS flight_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visit_id INTEGER,
            icao24 TEXT,
            ts INTEGER,
            lat REAL, lon REAL,
            alt_ft REAL, speed_kts REAL, heading INTEGER, dist_km REAL
        );
        CREATE TABLE IF NOT EXISTS iss_alerts (
            pass_time INTEGER PRIMARY KEY,
            duration INTEGER,
            alerted INTEGER DEFAULT 0,
            start_az TEXT,
            max_el INTEGER,
            end_az TEXT
        );
        CREATE TABLE IF NOT EXISTS weather_current  (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS weather_hourly   (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS weather_daily    (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS moon_phase       (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS astronomy_data   (id INTEGER PRIMARY KEY, ts INTEGER, data TEXT);
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
                       ("iss_alerts",      "end_az TEXT"),
                       ("iss_alerts",      "sat_name TEXT DEFAULT 'ISS'"),
                       ("seen_aircraft",   "heading INTEGER DEFAULT 0"),
                       ("live_aircraft",   "category INTEGER DEFAULT 0"),
                       ("seen_aircraft",   "category INTEGER DEFAULT 0"),
                       ("seen_aircraft",   "favourited INTEGER DEFAULT 0"),
                       ("seen_aircraft",   "seen INTEGER DEFAULT 0"),
                       ("seen_aircraft",   "speed_kts REAL DEFAULT 0"),
                       ("seen_aircraft",   "vertical_rate REAL DEFAULT 0"),
                       ("seen_aircraft",   "geo_alt_ft REAL DEFAULT 0"),
                       ("seen_aircraft",   "squawk TEXT"),
                       ("seen_aircraft",   "spi INTEGER DEFAULT 0"),
                       ("seen_aircraft",   "position_source INTEGER DEFAULT 0")]:
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
_FILTER_FIELDS = {
    "aircraft":   {"callsign", "country", "model", "airframe"},
    "satellites": {"sat_name"},
}
_TOKEN_RE = re.compile(r"\{(\w+)\}")

def _compile_filters(rule_text, category):
    """Parse the filter DSL: one 'field=pattern' (include) or '-field=pattern'
    (exclude) rule per line, fields depending on category. See CLAUDE.md for
    full semantics."""
    fields = _FILTER_FIELDS.get(category, set())
    compiled = {}
    for line in (rule_text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        exclude = line.startswith("-")
        if exclude:
            line = line[1:]
        field, _, pattern = line.partition("=")
        field, pattern = field.strip().lower(), pattern.strip()
        if field not in fields or not pattern:
            continue
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            log.debug(f"Skipping invalid filter pattern for {field}")
            continue
        bucket = compiled.setdefault(field, {"include": [], "exclude": []})
        bucket["exclude" if exclude else "include"].append(rx)
    return compiled

def _passes_filters(compiled, values):
    for field, rules in compiled.items():
        value = values.get(field) or ""
        if any(rx.search(value) for rx in rules["exclude"]):
            return False
        if rules["include"] and not any(rx.search(value) for rx in rules["include"]):
            return False
    return True

def _render_template(tmpl, values):
    """Safe {token} substitution — never str.format, so a user-edited template
    can't reach object attributes/indices. Unknown tokens are left as-is."""
    return _TOKEN_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), tmpl or "")

def notify_category(key, values, photo_url=None, notify_type=None):
    notif = cfg.get("notifications", {}).get(key, {})
    if not _passes_filters(_compile_filters(notif.get("filters", ""), key), values):
        return
    templates_by_id = {t["id"]: t for t in cfg.get("templates", [])}
    default_tmpl = templates_by_id.get(_CATEGORY_DEFAULT_TEMPLATE_ID.get(key), {})
    for target in notif.get("targets", []):
        url = target.get("url")
        if not target.get("enabled", True) or not url:
            continue
        if not _passes_filters(_compile_filters(target.get("filters", ""), key), values):
            continue
        # Field-level fallback, not whole-object: an inline template with a
        # blank title but a real body (or vice versa) shouldn't lose the
        # other field to an empty string when it could fall back to default.
        tmpl = templates_by_id.get(target.get("template_id")) or target.get("template") or {}
        title = _render_template(tmpl.get("title") or default_tmpl.get("title", ""), values)
        body  = _render_template(tmpl.get("body")  or default_tmpl.get("body", ""),  values)
        a = apprise.Apprise()
        a.add(url)
        try:
            a.notify(title=title, body=body,
                      notify_type=notify_type or apprise.NotifyType.INFO,
                      attach=[photo_url] if photo_url else None)
            log.info(f"Alert sent ({key}): {title}")
        except Exception as e:
            # Don't log str(e): the target URL is a secret and may appear in it
            log.error(f"Notify error ({key}/{target.get('id','?')}): {type(e).__name__}")

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

def _wikipedia_type_photo(model):
    """Return (photo_url, thumb_url) from Wikipedia for this aircraft type name.
    photo_url is the Wikipedia article; thumb_url is a 300px image."""
    if not model or model.lower() == "unknown" or len(model) < 5:
        return "", ""
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": model,
                "gsrlimit": 1,
                "prop": "pageimages",
                "pithumbsize": 300,
                "format": "json",
            },
            headers={"User-Agent": "SkyBro/1.0 (https://github.com/xDeeKay/SkyBro)"},
            timeout=5,
        )
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = page.get("thumbnail", {}).get("source", "")
            if thumb:
                title = page.get("title", "")
                wiki_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
                return wiki_url, thumb
    except Exception as e:
        log.debug(f"Wikipedia type photo ({model}): {e}")
    return "", ""

PHOTO_FETCH_WORKERS = 8

def fetch_aircraft_photo(icao24):
    """Return (photo_url, thumb_url) from cache, Planespotters (hex+reg), or Wikipedia type photo."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT photo_url, thumb_url FROM photo_cache WHERE icao24=?",
                       (icao24,)).fetchone()
    conn.close()
    if row is not None:
        return row[0] or "", row[1] or ""

    headers = {"User-Agent": "SkyBro/1.0 (Raspberry Pi sky tracker; https://github.com/xDeeKay/SkyBro)"}
    photo_url, thumb_url = "", ""
    model_name, reg = lookup(icao24)

    try:
        r = requests.get(f"https://api.planespotters.net/pub/photos/hex/{icao24}",
                         headers=headers, timeout=5)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if photos:
            ac = photos[0].get("aircraft", {})
            returned_hex = (ac.get("modes") or "").lower().strip()
            if not returned_hex or returned_hex == icao24.lower():
                photo_url = photos[0].get("link", "")
                tl = photos[0].get("thumbnail_large") or photos[0].get("thumbnail") or {}
                thumb_url = tl.get("src", "")
            else:
                log.debug(f"Photo fetch {icao24}: hex result was for {returned_hex}, skipping")
    except Exception as e:
        log.debug(f"Photo fetch {icao24} (hex): {e}")

    if not thumb_url and reg and re.search(r'[A-Za-z]', reg):
        try:
            r = requests.get(f"https://api.planespotters.net/pub/photos/reg/{reg}",
                             headers=headers, timeout=5)
            r.raise_for_status()
            photos = r.json().get("photos", [])
            if photos:
                ac = photos[0].get("aircraft", {})
                returned_reg = (ac.get("reg") or "").upper().strip()
                if returned_reg == reg.upper().strip():
                    photo_url = photos[0].get("link", "")
                    tl = photos[0].get("thumbnail_large") or photos[0].get("thumbnail") or {}
                    thumb_url = tl.get("src", "")
                    log.debug(f"Photo fetch {icao24}: found via reg {reg}")
                else:
                    log.debug(f"Photo fetch {icao24}: reg result was for '{returned_reg}', expected '{reg}', skipping")
        except Exception as e:
            log.debug(f"Photo fetch {icao24} (reg {reg}): {e}")

    if not thumb_url:
        photo_url, thumb_url = _wikipedia_type_photo(model_name)
        if thumb_url:
            log.debug(f"Photo fetch {icao24}: Wikipedia type photo for {model_name}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO photo_cache (icao24,photo_url,thumb_url,fetched) VALUES (?,?,?,?)",
                 (icao24, photo_url, thumb_url, int(time.time())))
    conn.commit()
    conn.close()
    return photo_url, thumb_url

# ── OpenSky ───────────────────────────────────────────────────────────────────
_alerted = {}  # icao24 → seen_aircraft.id of current visit
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
    pad = min(cfg["radius_km"], 100.0) / 111.0 * 1.3
    params = {
        "lamin": max(-90.0, cfg["home_lat"] - pad), "lomin": max(-180.0, cfg["home_lon"] - pad),
        "lamax": min(90.0, cfg["home_lat"] + pad), "lomax": min(180.0, cfg["home_lon"] + pad),
    }

    def _request():
        token = get_opensky_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return requests.get("https://opensky-network.org/api/states/all",
                            params=params, headers=headers, timeout=20)

    try:
        r = _request()
        if r.status_code == 401:
            # Token expired mid-session: clear cache and retry once
            _opensky_token = None
            _opensky_token_expiry = 0.0
            log.info("OpenSky: 401 received, refreshing token and retrying")
            r = _request()
        if r.status_code == 429:
            wait_mins = _opensky_backoff_step // 60
            _opensky_backoff_until = time.time() + _opensky_backoff_step
            log.warning(f"OpenSky rate limited (429), backing off {wait_mins} min "
                        f"(next step: {min(_opensky_backoff_step*2, 3600)//60} min)")
            _opensky_backoff_step = min(_opensky_backoff_step * 2, 3600)
            update_source_status('opensky', False,
                                 f"Rate limited, retry in {wait_mins} min", 'backoff')
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
        geo_alt_ft      = round(s[13] * 3.28084) if len(s) > 13 and s[13] else 0
        squawk          = s[14] if len(s) > 14 and s[14] else None
        spi             = 1 if len(s) > 15 and s[15] else 0
        position_source = int(s[16]) if len(s) > 16 and s[16] is not None else 0
        category  = int(s[17]) if len(s) > 17 and s[17] is not None else 0
        dist_km   = round(haversine(cfg["home_lat"], cfg["home_lon"], lat, lon), 2)
        model, reg = lookup(icao24)
        thumb_url  = photo_cache_map.get(icao24, "")

        c.execute("""INSERT OR REPLACE INTO live_aircraft VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (icao24, callsign, lat, lon, alt_ft, speed_kts, heading,
             vrate, country, model, reg, dist_km, now, thumb_url, category))

        in_zone = (dist_km <= cfg["radius_km"] and alt_ft <= cfg["alt_threshold_ft"])

        if in_zone and icao24 not in _alerted:
            direction = bearing_to_compass(bearing_from_home(lat, lon))
            _metric = cfg.get("units_speed", "aviation") == "metric"
            vr_str = (f"↑ {abs(vrate*(60 if _metric else 196.85)):.0f} {'m/min' if _metric else 'fpm'}" if vrate > 0.5
                      else f"↓ {abs(vrate*(60 if _metric else 196.85)):.0f} {'m/min' if _metric else 'fpm'}" if vrate < -0.5
                      else "level")
            alert_queue.append({
                'icao24': icao24, 'callsign': callsign, 'lat': lat, 'lon': lon,
                'alt_ft': alt_ft, 'speed_kts': speed_kts, 'vrate': vrate,
                'vr_str': vr_str, 'dist_km': dist_km, 'country': country,
                'model': model, 'reg': reg, 'direction': direction,
                'heading': heading, 'category': category,
                'geo_alt_ft': geo_alt_ft, 'squawk': squawk,
                'spi': spi, 'position_source': position_source,
            })
            c.execute("""INSERT INTO seen_aircraft
                (icao24,callsign,first_seen,last_seen,min_alt_ft,min_dist_km,
                 lat,lon,origin_country,model,registration,alerted,photo_url,heading,category,
                 speed_kts,vertical_rate,geo_alt_ft,squawk,spi,position_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",
                (icao24, callsign, now, now, alt_ft, dist_km, lat, lon,
                 country, model, reg, thumb_url, heading, category,
                 speed_kts, vrate, geo_alt_ft, squawk, spi, position_source))
            _alerted[icao24] = c.lastrowid

        if in_zone and icao24 in _alerted:
            c.execute("""INSERT INTO flight_pings
                (visit_id,icao24,ts,lat,lon,alt_ft,speed_kts,heading,dist_km)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (_alerted[icao24], icao24, now, lat, lon, alt_ft, speed_kts, heading, dist_km))

    c.execute("DELETE FROM live_aircraft WHERE updated < ?", (now - 90,))
    conn.commit()
    conn.close()
    # ── No DB connections open beyond this point until Phase 4 ───────────────

    # ── Phase 3: fetch photos concurrently, same cycle for every aircraft ─────
    # needs_photo already covers alert_queue's icao24s too (alerted aircraft are
    # always a subset of visible aircraft), so one bounded-concurrency pass over
    # it fetches both the live-map thumbnail and any alert-notification photo.
    # Concurrency keeps a burst of new aircraft from stacking into a long,
    # sequential delay that would otherwise push out the next OpenSky poll.
    newly_fetched = {}
    if needs_photo:
        with ThreadPoolExecutor(max_workers=PHOTO_FETCH_WORKERS) as pool:
            for icao24, (_, thumb) in zip(needs_photo, pool.map(fetch_aircraft_photo, needs_photo)):
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
        airframe = _classify_airframe(alert.get('category', 0), alert['callsign'], alert['model'])
        ac_emoji = '🚁' if airframe == 'heli' else '✈️'
        _metric = cfg.get("units_speed", "aviation") == "metric"
        spd_str = f"{round(alert['speed_kts'] * 1.852)} km/h" if _metric else f"{alert['speed_kts']} kts"
        alt_str = f"{round(alert['alt_ft'] * 0.3048):,} m" if _metric else f"{alert['alt_ft']:,} ft"
        notify_category("aircraft", {
            'emoji': ac_emoji, 'callsign': alert['callsign'], 'model': alert['model'],
            'registration': alert['reg'] or "N/A", 'country': alert['country'],
            'direction': alert['direction'], 'speed': spd_str,
            'vertical_speed': alert['vr_str'], 'airframe': airframe,
            'altitude_ft': alt_str, 'distance_km': alert['dist_km'],
        }, photo_url=photo_url)

    live_icaos = {s[0] for s in states if s[6] and s[5]}
    for icao in list(_alerted):
        if icao not in live_icaos:
            _alerted.pop(icao, None)

# ── Satellites ────────────────────────────────────────────────────────────────
_sat_last_check = 0.0

_HELI_CALLSIGN_RE = re.compile(r'^(RSCU|TIGR|HEMS|LIFEF|HELIAIR|POLAIR|NGAIR|CHC|PHG)')
_HELI_MODEL_RE     = re.compile(r'helicopt|eurocopter|sikorsky|robinson|airbus h|leonardo|bell \d|ec\d{3}|h130|h145|h160|aw\d{3}|r22|r44|r66|bo.?105|bk.?117|ka-\d|mi-\d')
_WIDEBODY_MODEL_RE = re.compile(r'b74\d|b76\d|b77\d|b78\d|747|767|777|787|a33\d|a34\d|a35\d|a380|dc-10|md-11|dreamliner')
_MILITARY_MODEL_RE = re.compile(r'\bf-\d|f/a-|eurofighter|typhoon|gripen|rafale|hornet|b-52|c-17|c-130|hercules|harrier|su-\d|mig-\d|pc.?21|p-8a|poseidon|globemaster|wedgetail')
_LIGHT_MODEL_RE     = re.compile(r'cessna|piper|beechcraft|cirrus sr|diamond da|tecnam|c172|c152|c182|pa-\d|da2\d|da4\d|sr2\d|tbm |pc-12|pc-6|robin')

def _classify_airframe(category, callsign, model):
    """Mirrors index.html's aircraftType() JS so filter/template values match
    what the dashboard shows (silhouette selection stays client-side)."""
    if category == 8: return 'heli'
    if category == 6: return 'widebody'
    cs = (callsign or '').upper()
    if not model or model == 'Unknown':
        return 'heli' if _HELI_CALLSIGN_RE.match(cs) else 'jet'
    m = model.lower()
    if _HELI_MODEL_RE.search(m): return 'heli'
    if _WIDEBODY_MODEL_RE.search(m): return 'widebody'
    if _MILITARY_MODEL_RE.search(m): return 'military'
    if _LIGHT_MODEL_RE.search(m): return 'light'
    return 'jet'

def _az_compass(deg):
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[int(round(deg / 22.5)) % 16]

def _ephem_to_unix(d):
    return int(ephem.Date(d).datetime().replace(tzinfo=timezone.utc).timestamp())

def check_starlink_passes():
    global _starlink_last
    if time.time() - _starlink_last < STARLINK_INTERVAL:
        return
    _starlink_last = time.time()
    lat, lon = cfg["home_lat"], cfg["home_lon"]

    try:
        r = requests.get(STARLINK_TLE_URL, timeout=30)
        r.raise_for_status()
        lines = [l.strip() for l in r.text.splitlines() if l.strip()]
    except Exception as e:
        log.warning(f"Starlink TLE fetch error: {e}")
        return

    sats = []
    for i in range(0, len(lines) - 2, 3):
        if lines[i+1].startswith('1 ') and lines[i+2].startswith('2 '):
            sats.append((lines[i], lines[i+1], lines[i+2]))
    if not sats:
        log.warning("Starlink: no TLEs parsed")
        return

    # Sample ~30 across the full list for good orbital plane coverage
    n = 30
    step = max(1, len(sats) // n)
    sampled = sats[::step][:n]

    obs = ephem.Observer()
    obs.lat = str(lat)
    obs.lon = str(lon)
    obs.elevation = 0
    obs.horizon = '10'

    now_e = ephem.now()
    end_e = ephem.Date(now_e + 7)
    passes = []
    for name, line1, line2 in sampled:
        try:
            sat = ephem.readtle(name, line1, line2)
            obs.date = now_e
            for _ in range(25):
                if obs.date >= end_e:
                    break
                try:
                    rise_t, rise_az, _, max_el, set_t, set_az = obs.next_pass(sat)
                    if rise_t and rise_t >= now_e:
                        max_el_deg = int(math.degrees(max_el))
                        if max_el_deg >= 10:
                            passes.append({
                                'pass_time': _ephem_to_unix(rise_t),
                                'duration':  max(0, int((set_t - rise_t) * 86400)),
                                'max_el':    max_el_deg,
                                'start_az':  _az_compass(math.degrees(rise_az)),
                                'end_az':    _az_compass(math.degrees(set_az)),
                            })
                        obs.date = ephem.Date((set_t or obs.date) + 1 * ephem.minute)
                    else:
                        obs.date = ephem.Date(obs.date + 90 * ephem.minute)
                except (ephem.NeverUpError, ephem.AlwaysUpError):
                    break
                except Exception:
                    obs.date = ephem.Date(obs.date + 90 * ephem.minute)
        except Exception as e:
            log.debug(f"Starlink pass error ({name}): {e}")

    passes.sort(key=lambda p: p['pass_time'])
    passes = passes[:50]  # cap at 50 upcoming passes

    now_unix = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM iss_alerts WHERE sat_name='Starlink' AND alerted=0 AND pass_time > ?", (now_unix,))
    for p in passes:
        c.execute(
            "INSERT OR IGNORE INTO iss_alerts (pass_time, duration, alerted, start_az, max_el, end_az, sat_name) "
            "VALUES (?, ?, 0, ?, ?, ?, 'Starlink')",
            (p['pass_time'], p['duration'], p['start_az'], p['max_el'], p['end_az'])
        )
    conn.commit(); conn.close()
    log.info(f"Starlink: {len(passes)} passes computed from {len(sampled)} satellites")

def check_satellites():
    global _sat_last_check
    if time.time() - _sat_last_check < cfg["iss_check_hours"] * 3600:
        return
    _sat_last_check = time.time()
    api_key = cfg.get("n2yo_api_key", "")
    if not api_key:
        log.info("Satellites: n2yo API key not configured, skipping")
        update_source_status('iss', False, "No API key configured", 'no_key')
        return
    lat, lon = cfg["home_lat"], cfg["home_lon"]
    warn = cfg["iss_warn_mins"] * 60
    total = 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for sat_name, norad_id, _ in SATELLITES:
        try:
            url = (f"https://api.n2yo.com/rest/v1/satellite/visualpasses/"
                   f"{norad_id}/{lat}/{lon}/0/10/60/")
            r = requests.get(url, params={"apiKey": api_key}, timeout=15)
            r.raise_for_status()
            passes = r.json().get("passes") or []
            c.execute("DELETE FROM iss_alerts WHERE alerted=0 AND sat_name=? AND pass_time > ?",
                      (sat_name, int(time.time()) + warn))
            for p in passes:
                c.execute(
                    "INSERT OR IGNORE INTO iss_alerts "
                    "(pass_time,duration,alerted,start_az,max_el,end_az,sat_name) "
                    "VALUES (?,?,0,?,?,?,?)",
                    (p["startUTC"], p["duration"],
                     p.get("startAzCompass"), p.get("maxEl"), p.get("endAzCompass"),
                     sat_name)
                )
            total += len(passes)
            log.info(f"Satellites: {sat_name} — {len(passes)} passes fetched")
        except Exception as e:
            msg = str(e).replace(api_key, "***") if api_key else str(e)
            log.warning(f"Satellites fetch error ({sat_name}): {msg}")
    conn.commit(); conn.close()
    update_source_status('iss', True)
    log.info(f"Satellites: {total} total passes across {len(SATELLITES)} objects")

def dispatch_satellite_alerts():
    # No category-level enable/disable anymore — each target has its own
    # `enabled` flag, checked inside notify_category(). All satellite types
    # notify through the shared "satellites" category; sat_name is passed as a
    # value so filters/templates can still differentiate between them.
    now  = int(time.time())
    warn = cfg["iss_warn_mins"] * 60
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    wr = conn.execute("SELECT data FROM weather_current WHERE id=1").fetchone()
    utc_off = json.loads(wr[0]).get("utc_offset_seconds", 0) if wr else 0
    local_tz = timezone(timedelta(seconds=utc_off))
    rows = c.execute(
        "SELECT pass_time, duration, sat_name FROM iss_alerts "
        "WHERE alerted=0 AND pass_time BETWEEN ? AND ?",
        (now, now + warn)).fetchall()
    for pass_time, duration, sat_name in rows:
        mins = (pass_time - now) // 60
        dt   = datetime.fromtimestamp(pass_time, tz=timezone.utc).astimezone(local_tz).strftime("%H:%M")
        notify_category("satellites", {
            'sat_name': sat_name, 'time': dt, 'minutes': str(mins), 'duration': str(duration),
        }, notify_type=apprise.NotifyType.WARNING)
        # Scope by sat_name too: pass_time alone can collide across satellite
        # types sharing the same second-resolution timestamp.
        c.execute("UPDATE iss_alerts SET alerted=1 WHERE pass_time=? AND sat_name=?",
                  (pass_time, sat_name))
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

# ── Astronomy ─────────────────────────────────────────────────────────────────
def maybe_update_astronomy():
    global _astronomy_last
    if time.time() - _astronomy_last < ASTRONOMY_INTERVAL:
        return
    _astronomy_last = time.time()
    conn = sqlite3.connect(DB_PATH)
    try:
        process_astronomy(cfg["home_lat"], cfg["home_lon"], conn)
        update_source_status('astronomy', True)
    except Exception as e:
        log.error(f"Astronomy error: {e}", exc_info=True)
        update_source_status('astronomy', False, str(e))
    finally:
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
        except Exception as e:
            log.error(f"Aircraft error: {e}", exc_info=True)
            update_source_status('opensky', False, str(e))
        try:
            check_satellites()
            dispatch_satellite_alerts()
        except Exception as e:
            log.error(f"Satellites error: {e}", exc_info=True)
            update_source_status('iss', False, str(e))
        try:
            check_starlink_passes()
        except Exception as e:
            log.error(f"Starlink error: {e}", exc_info=True)
        try:
            maybe_update_weather()
        except Exception as e:
            log.error(f"Weather error: {e}", exc_info=True)
            update_source_status('weather', False, str(e))
        try:
            maybe_update_moon()
        except Exception as e:
            log.error(f"Moon error: {e}", exc_info=True)
        try:
            maybe_update_astronomy()
        except Exception as e:
            log.error(f"Astronomy error: {e}", exc_info=True)
        Path("/tmp/heartbeat").write_text(str(time.time()))
        time.sleep(max(1, cfg["poll_interval"]))

if __name__ == "__main__":
    main()
