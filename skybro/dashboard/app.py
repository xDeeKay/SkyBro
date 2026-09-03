"""
SkyBro Dashboard - Flask app
Serves the live map UI, API endpoints, and the settings page.
Settings are persisted to /data/config.json (hot-reloaded by tracker).
"""

import os, json, re, secrets, sqlite3, time

APP_VERSION = "1.6.0"
from datetime import datetime
from pathlib import Path
import apprise
from flask import Flask, render_template, jsonify, request, abort

app = Flask(__name__)
DATA_DIR = Path("/data")
DB_PATH  = DATA_DIR / "skybro.db"
CFG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "tracker.log"

# Named, reusable title/body pairs. Targets reference one by id (linked
# reference: editing a template here updates every target using it) instead
# of each carrying its own copy. The two defaults are protected: always
# present, never deletable (see _clean_templates).
DEFAULT_TEMPLATES_LIST = [
    {
        "id": "default_aircraft", "name": "Default Aircraft", "kind": "aircraft",
        "title": "{emoji} {callsign} overhead",
        "body":  "{model} ({registration})\n{direction} at {distance_km} km • {altitude}\n{speed} • {vertical_speed}",
    },
    {
        "id": "default_satellite", "name": "Default Satellite", "kind": "satellite",
        "title": "🛰️ {sat_name} flyover coming up",
        "body":  "Visible pass in ~{minutes} min (at {time} local), lasting {duration}s\nRises {start_az} • peaks at {max_el} • sets {end_az}",
    },
    {
        "id": "default_digest", "name": "Default Daily Digest", "kind": "digest",
        "title": "🌌 SkyBro Daily Digest",
        "body":  "✈️ Yesterday: {aircraft_count} aircraft ({aircraft_closest})\n🛰️ Today: {satellite_count} pass(es), {satellite_list}\n⭐ Tonight: {astronomy_highlight}",
    },
]
_CATEGORY_DEFAULT_TEMPLATE_ID = {"aircraft": "default_aircraft", "satellites": "default_satellite", "digest": "default_digest"}

DEFAULTS = {
    "home_lat": 1.3521, "home_lon": 103.8198,
    "radius_km": 15.0, "alt_threshold_ft": 5000.0,
    "poll_interval": 15, "iss_check_hours": 2, "iss_warn_mins": 20,
    "opensky_client_id": "", "opensky_client_secret": "",
    "n2yo_api_key": "",
    "notifications": {
        "aircraft":   {"filters": "", "targets": [], "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"}},
        "satellites": {"filters": "", "targets": [], "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"}},
        "digest":     {"filters": "", "targets": []},
    },
    "templates": [dict(t) for t in DEFAULT_TEMPLATES_LIST],
    "use_location_time": False, "time_format": "12h",
    "units_speed": "aviation", "units_temp": "imperial",
    "digest_send_time": "08:00",
}

# Fields the UI is allowed to read/write
UI_FIELDS = list(DEFAULTS.keys())

APPRISE_URL_CAP = 20
FILTERS_CAP     = 5000
TEMPLATE_CAP    = 500
_TOKEN_RE       = re.compile(r"\{(\w+)\}")

def _render_template(tmpl, values):
    return _TOKEN_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), tmpl or "")

def _build_apprise_schemas():
    """Service picker for the target URL builder: one entry per Apprise
    plugin, derived from Apprise's own plugin metadata (scheme, display name,
    minimal URL template, and its placeholder fields) rather than hand-kept,
    so it stays correct across apprise version bumps. Uses each plugin's
    first/shortest template, since that's Apprise's own choice of the minimal
    valid form. Services outside that minimal form (e.g. Pushover device
    targeting) aren't covered; use "Custom URL" for those.
    """
    out = []
    try:
        schemas = apprise.Apprise().details().get("schemas", [])
    except Exception:
        return out
    for s in schemas:
        det = s.get("details") or {}
        templates = det.get("templates") or ()
        if not templates:
            continue
        tmpl = templates[0]
        scheme_opts = list(s.get("secure_protocols") or ()) + list(s.get("protocols") or ())
        if not scheme_opts:
            continue
        tokens = det.get("tokens") or {}
        fields = []
        for key in _TOKEN_RE.findall(tmpl):
            if key == "schema":
                continue
            meta = tokens.get(key) or {}
            fields.append({
                "key": key,
                "label": str(meta.get("name") or key.replace("_", " ").title()),
                "private": bool(meta.get("private")),
                "required": bool(meta.get("required")),
            })
        out.append({
            "scheme": scheme_opts[0],
            "name": str(s.get("service_name") or scheme_opts[0]),
            "template": tmpl,
            "fields": fields,
        })
    out.sort(key=lambda x: x["name"].lower())
    return out

APPRISE_SCHEMAS = _build_apprise_schemas()

def _parse_discord_webhook(url):
    """Convert a Discord webhook URL into an apprise discord://id/token target."""
    m = re.search(r'/webhooks/(\d+)/([^/?]+)', url or '')
    return f"discord://{m.group(1)}/{m.group(2)}" if m else None

def _migrate_legacy_notifications(raw_cfg):
    """One-time, idempotent conversion of the old Pushover/Discord fields into
    the new Apprise-based notifications structure. Migrated targets just point
    at the new default templates (no attempt to reproduce old message wording,
    the original Discord embed's inline field grid has no Apprise equivalent
    anyway). The old category-level alerts_enabled/iss_alerts_enabled applied
    uniformly to every target, so it's carried over as each migrated target's
    own `enabled` flag."""
    if "notifications" in raw_cfg:
        return raw_cfg, False
    urls = []
    if raw_cfg.get("pushover_token") and raw_cfg.get("pushover_user"):
        urls.append({"id": secrets.token_hex(4),
                      "url": f"pover://{raw_cfg['pushover_user']}@{raw_cfg['pushover_token']}"})
    discord_url = _parse_discord_webhook(raw_cfg.get("discord_webhook"))
    if discord_url:
        urls.append({"id": secrets.token_hex(4), "url": discord_url})
    aircraft_enabled   = raw_cfg.get("alerts_enabled", True)
    satellites_enabled = raw_cfg.get("iss_alerts_enabled", True)
    raw_cfg["notifications"] = {
        "aircraft": {
            "filters": "",
            "targets": [{**u, "enabled": aircraft_enabled,
                         "template_id": "default_aircraft", "filters": ""} for u in urls],
        },
        "satellites": {
            "filters": "",
            "targets": [{**u, "enabled": satellites_enabled,
                         "template_id": "default_satellite", "filters": ""} for u in urls],
        },
    }
    if "templates" not in raw_cfg:
        raw_cfg["templates"] = [dict(t) for t in DEFAULT_TEMPLATES_LIST]
    for k in ("pushover_token", "pushover_user", "discord_webhook",
              "alerts_enabled", "iss_alerts_enabled"):
        raw_cfg.pop(k, None)
    return raw_cfg, True

def _seed_templates(raw_cfg):
    """Separate from _migrate_legacy_notifications: an install can already have
    a `notifications` key (e.g. from testing a pre-templates build) without yet
    having `templates`, so this must not be gated by that migration's own guard.
    Also backfills any individual DEFAULT_TEMPLATES_LIST entry (e.g.
    default_digest, added after `templates` already existed on some installs)
    that isn't present yet (same shallow-merge gap as
    _seed_notification_categories, just for the templates list). _clean_templates
    already does this at save time; this covers the read/render path too, since
    settings.html's Daily Digest Templates tab would otherwise show nothing to
    edit until the next save."""
    if "templates" not in raw_cfg:
        raw_cfg["templates"] = [dict(t) for t in DEFAULT_TEMPLATES_LIST]
        return True
    existing_ids = {t.get("id") for t in raw_cfg["templates"] if isinstance(t, dict)}
    changed = False
    for d in DEFAULT_TEMPLATES_LIST:
        if d["id"] not in existing_ids:
            raw_cfg["templates"].append(dict(d))
            changed = True
    return changed

def _seed_notification_categories(raw_cfg):
    """A `notifications` dict from before a new category/field existed survives
    the top-level DEFAULTS merge as-is, since that merge is shallow and
    `notifications` already exists. Without this, settings.html's unconditional
    `cfg.notifications.digest.filters` etc. raises a Jinja UndefinedError (500)
    on any install saved before that category was added. Backfills (1) an
    entirely missing category (e.g. `digest`) and (2) a missing per-category
    `quiet_hours` block for aircraft/satellites (mirrors tracker.py's copy of
    this function). Also drops the short-lived top-level `quiet_hours` +
    per-category `quiet_hours_exempt` shape from an earlier build of the
    quiet-hours feature, since each category now owns its own independent
    window instead of sharing one with an exemption flag."""
    notif = raw_cfg.get("notifications")
    if not isinstance(notif, dict):
        return False
    changed = False
    for key, default_val in DEFAULTS["notifications"].items():
        if key not in notif:
            notif[key] = dict(default_val)
            changed = True
        elif "quiet_hours" in default_val and "quiet_hours" not in notif[key]:
            notif[key]["quiet_hours"] = dict(default_val["quiet_hours"])
            changed = True
        if isinstance(notif.get(key), dict) and "quiet_hours_exempt" in notif[key]:
            del notif[key]["quiet_hours_exempt"]
            changed = True
    if "quiet_hours" in raw_cfg:
        del raw_cfg["quiet_hours"]
        changed = True
    return changed

# Server-side bounds mirroring the settings page's HTML min/max, since those
# are only client-side hints and can be bypassed via a direct API call.
NUMERIC_BOUNDS = {
    "home_lat":         (-90.0, 90.0),
    "home_lon":         (-180.0, 180.0),
    "radius_km":        (1.0, 100.0),
    "alt_threshold_ft": (500.0, 50000.0),
    "poll_interval":    (10, 120),
    "iss_check_hours":  (1, 12),
    "iss_warn_mins":    (5, 60),
}
ENUM_FIELDS = {
    "units_speed": {"aviation", "metric"},
    "units_temp":  {"imperial", "metric"},
    "time_format": {"12h", "24h"},
}

def read_config():
    if not CFG_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(CFG_PATH) as f:
            loaded = json.load(f)
        loaded, migrated = _migrate_legacy_notifications(loaded)
        seeded = _seed_templates(loaded)
        seeded_cats = _seed_notification_categories(loaded)
        merged = {**DEFAULTS, **loaded}
        if migrated or seeded or seeded_cats:
            # Write directly, not via write_config() (which itself calls
            # read_config() to merge, that would recurse).
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(CFG_PATH, "w") as f:
                json.dump(merged, f, indent=2)
        return merged
    except Exception:
        return dict(DEFAULTS)

def write_config(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = read_config()
    current.update({k: v for k, v in data.items() if k in UI_FIELDS})
    with open(CFG_PATH, "w") as f:
        json.dump(current, f, indent=2)
    return current

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    cfg = read_config()
    first_run = (cfg.get("home_lat") == DEFAULTS["home_lat"] and
                 cfg.get("home_lon") == DEFAULTS["home_lon"])
    return render_template("index.html", cfg=cfg,
                           app_version=APP_VERSION,
                           git_sha=os.environ.get("GIT_SHA", "").strip(),
                           first_run=first_run)

SETTINGS_TABS = {"location", "display", "polling", "notifications", "integrations", "danger"}
NOTIF_SUBTABS = {"aircraft", "satellites", "digest"}
NOTIF_INNER_TABS = {"targets", "templates"}

@app.route("/settings")
@app.route("/settings/<tab>")
@app.route("/settings/<tab>/<subtab>")
@app.route("/settings/<tab>/<subtab>/<inner>")
def settings(tab=None, subtab=None, inner=None):
    if tab is not None and tab not in SETTINGS_TABS:
        abort(404)
    if subtab is not None and (tab != "notifications" or subtab not in NOTIF_SUBTABS):
        abort(404)
    if inner is not None and (tab != "notifications" or inner not in NOTIF_INNER_TABS):
        abort(404)
    return render_template("settings.html", cfg=read_config(), apprise_schemas=APPRISE_SCHEMAS,
                           initial_tab=tab or "location",
                           initial_subtab=subtab or "aircraft",
                           initial_inner=inner or "targets")

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("404.html"), 404

# ── REST API ──────────────────────────────────────────────────────────────────
@app.route("/api/live")
def api_live():
    try:
        rows = db().execute(
            "SELECT * FROM live_aircraft WHERE updated > ? ORDER BY dist_km ASC",
            (int(time.time()) - 90,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

@app.route("/api/satellites")
@app.route("/api/iss")  # keep as alias
def api_satellites():
    try:
        now  = int(time.time())
        rows = db().execute(
            "SELECT *, (pass_time - ?) as mins_away FROM iss_alerts "
            "WHERE pass_time > ? ORDER BY pass_time ASC LIMIT 200",
            (now, now)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["mins_away"] = d["mins_away"] // 60
            d["sat_name"]  = d.get("sat_name") or "ISS"
            result.append(d)
        return jsonify(result)
    except Exception:
        return jsonify([])

HISTORY_PAGE = 25

@app.route("/api/history")
def api_history():
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit  = max(HISTORY_PAGE, min(500, int(request.args.get('limit', HISTORY_PAGE))))
        only_favs = request.args.get('favourites') == '1'
        only_seen = request.args.get('seen') == '1'
        search    = (request.args.get('search') or '').strip()
        conditions = ["sa.alerted=1"]
        params = []
        if only_favs:
            conditions.append("COALESCE(sa.favourited,0)=1")
        if only_seen:
            conditions.append("COALESCE(sa.seen,0)=1")
        if search:
            like = f"%{search}%"
            conditions.append("(sa.callsign LIKE ? OR sa.model LIKE ? OR sa.registration LIKE ? OR sa.origin_country LIKE ?)")
            params.extend([like, like, like, like])
        where = "WHERE " + " AND ".join(conditions)
        params.extend([limit + 1, offset])
        rows = db().execute(f"""
            SELECT sa.id, sa.icao24, sa.callsign, sa.model, sa.registration, sa.origin_country,
                   sa.min_alt_ft, sa.min_dist_km, sa.first_seen, sa.last_seen,
                   sa.lat, sa.lon, COALESCE(sa.heading, 0) AS heading,
                   COALESCE(sa.category, 0) AS category,
                   COALESCE(sa.favourited, 0) AS favourited,
                   COALESCE(sa.seen, 0) AS seen,
                   COALESCE(sa.speed_kts, 0) AS speed_kts,
                   COALESCE(sa.vertical_rate, 0) AS vertical_rate,
                   COALESCE(sa.geo_alt_ft, 0) AS geo_alt_ft,
                   sa.squawk,
                   COALESCE(sa.spi, 0) AS spi,
                   COALESCE(sa.position_source, 0) AS position_source,
                   COALESCE(pc.thumb_url, sa.photo_url) AS thumb_url,
                   pc.photo_url AS full_photo_url
            FROM seen_aircraft sa
            LEFT JOIN photo_cache pc ON pc.icao24 = sa.icao24
            {where}
            ORDER BY sa.last_seen DESC LIMIT ? OFFSET ?
        """, params).fetchall()
        rows = [dict(r) for r in rows]
        has_more = len(rows) > limit
        return jsonify({"rows": rows[:limit], "has_more": has_more})
    except Exception:
        return jsonify({"rows": [], "has_more": False})

@app.route("/api/history/favourite", methods=["POST"])
def api_history_favourite():
    data = request.get_json(force=True)
    row_id = (data or {}).get("id")
    if not row_id: abort(400)
    try:
        conn = db()
        row = conn.execute("SELECT COALESCE(favourited,0) FROM seen_aircraft WHERE id=?", (row_id,)).fetchone()
        if not row: abort(404)
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE seen_aircraft SET favourited=? WHERE id=?", (new_val, row_id))
        conn.commit()
        return jsonify({"ok": True, "favourited": new_val})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/history/seen", methods=["POST"])
def api_history_seen():
    data = request.get_json(force=True)
    row_id = (data or {}).get("id")
    if not row_id: abort(400)
    try:
        conn = db()
        row = conn.execute("SELECT COALESCE(seen,0) FROM seen_aircraft WHERE id=?", (row_id,)).fetchone()
        if not row: abort(404)
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE seen_aircraft SET seen=? WHERE id=?", (new_val, row_id))
        conn.commit()
        return jsonify({"ok": True, "seen": new_val})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/history/delete", methods=["POST"])
def api_history_delete():
    data = request.get_json(force=True)
    row_id = (data or {}).get("id")
    if not row_id: abort(400)
    try:
        conn = db()
        conn.execute("DELETE FROM flight_pings WHERE visit_id=?", (row_id,))
        conn.execute("DELETE FROM seen_aircraft WHERE id=?", (row_id,))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/history/<int:row_id>/pings")
def api_history_pings(row_id):
    try:
        rows = db().execute(
            "SELECT ts, lat, lon, alt_ft, speed_kts, heading, dist_km FROM flight_pings "
            "WHERE visit_id=? ORDER BY ts ASC", (row_id,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

@app.route("/api/aircraft/stats")
def api_aircraft_stats():
    try:
        conn = db()
        try:
            row = conn.execute("SELECT data FROM weather_current WHERE id=1").fetchone()
            utc_offset = json.loads(row["data"]).get("utc_offset_seconds", 0) if row else 0
        except Exception:
            utc_offset = 0

        total            = conn.execute("SELECT COUNT(*) FROM seen_aircraft WHERE alerted=1").fetchone()[0]
        unique_aircraft  = conn.execute("SELECT COUNT(DISTINCT icao24) FROM seen_aircraft WHERE alerted=1").fetchone()[0]
        unique_countries = conn.execute("SELECT COUNT(DISTINCT origin_country) FROM seen_aircraft WHERE alerted=1 AND origin_country != ''").fetchone()[0]
        unique_models    = conn.execute("SELECT COUNT(DISTINCT model) FROM seen_aircraft WHERE alerted=1 AND model NOT IN ('','Unknown')").fetchone()[0]

        closest     = conn.execute("SELECT icao24, callsign, model, min_dist_km FROM seen_aircraft WHERE alerted=1 ORDER BY min_dist_km ASC LIMIT 1").fetchone()
        lowest      = conn.execute("SELECT icao24, callsign, model, min_alt_ft FROM seen_aircraft WHERE alerted=1 ORDER BY min_alt_ft ASC LIMIT 1").fetchone()
        most_visited = conn.execute("SELECT icao24, callsign, model, COUNT(*) AS visits FROM seen_aircraft WHERE alerted=1 GROUP BY icao24 ORDER BY visits DESC LIMIT 1").fetchone()

        top_countries = conn.execute(
            "SELECT origin_country, COUNT(*) AS n FROM seen_aircraft WHERE alerted=1 AND origin_country != '' "
            "GROUP BY origin_country ORDER BY n DESC LIMIT 8").fetchall()
        top_models = conn.execute(
            "SELECT model, COUNT(*) AS n FROM seen_aircraft WHERE alerted=1 AND model NOT IN ('','Unknown') "
            "GROUP BY model ORDER BY n DESC LIMIT 8").fetchall()
        hours = conn.execute(
            "SELECT CAST((first_seen + ?) / 3600 AS INTEGER) % 24 AS hr, COUNT(*) AS n "
            "FROM seen_aircraft WHERE alerted=1 GROUP BY hr ORDER BY hr", (utc_offset,)).fetchall()

        return jsonify({
            "total": total,
            "unique_aircraft": unique_aircraft,
            "unique_countries": unique_countries,
            "unique_models": unique_models,
            "closest":      dict(closest)      if closest      else None,
            "lowest":       dict(lowest)       if lowest       else None,
            "most_visited": dict(most_visited) if most_visited else None,
            "top_countries": [dict(r) for r in top_countries],
            "top_models":    [dict(r) for r in top_models],
            "hours":         [dict(r) for r in hours],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/history/reset", methods=["POST"])
def api_history_reset():
    try:
        conn = db()
        conn.execute("DELETE FROM seen_aircraft")
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/logs")
def api_logs():
    try:
        n = min(max(int(request.args.get("lines", 300)), 1), 1000)
    except (TypeError, ValueError):
        n = 300
    if not LOG_PATH.exists():
        return jsonify({"lines": []})
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [l.rstrip("\n") for l in lines[-n:]]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats")
def api_stats():
    try:
        conn = db()
        now  = int(time.time())
        try:
            row = conn.execute("SELECT data FROM weather_current WHERE id=1").fetchone()
            utc_offset = json.loads(row["data"]).get("utc_offset_seconds", 0) if row else 0
        except Exception:
            utc_offset = 0
        local_now   = now + utc_offset
        today_start = (local_now - local_now % 86400) - utc_offset
        live    = conn.execute("SELECT COUNT(*) FROM live_aircraft").fetchone()[0]
        today   = conn.execute("SELECT COUNT(*) FROM seen_aircraft WHERE alerted=1 AND last_seen >= ?", (today_start,)).fetchone()[0]
        total   = conn.execute("SELECT COUNT(*) FROM seen_aircraft WHERE alerted=1").fetchone()[0]
        countries = conn.execute("SELECT COUNT(DISTINCT origin_country) FROM seen_aircraft WHERE alerted=1").fetchone()[0]
        cfg = read_config()
        return jsonify({
            "live": live, "today": today, "total": total, "countries": countries,
            "use_location_time": cfg.get("use_location_time", False),
            "time_format": cfg.get("time_format", "12h"),
        })
    except Exception:
        return jsonify({"live":0,"today":0,"total":0,"countries":0})

def _mask_notifications(notifications):
    masked = json.loads(json.dumps(notifications))
    for cat in masked.values():
        for target in cat.get("targets", []):
            if target.get("url"):
                target["url"] = "••••••••"
    return masked

_FILTER_LINE_RE = re.compile(r'^(-?\w+=.+|\w+\s*(?:>=|<=|>|<)\s*-?\d+(?:\.\d+)?)$')

def _clean_filter_text(text):
    lines = [l.strip() for l in str(text or "")[:FILTERS_CAP].splitlines()
             if _FILTER_LINE_RE.match(l.strip())]
    return "\n".join(lines)

_HHMM_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')

def _clean_quiet_hours(submitted, current):
    submitted = submitted if isinstance(submitted, dict) else {}
    current = current if isinstance(current, dict) else {}
    start = str(submitted.get("start", ""))
    end   = str(submitted.get("end", ""))
    if not _HHMM_RE.match(start): start = current.get("start", "22:00")
    if not _HHMM_RE.match(end):   end   = current.get("end", "08:00")
    return {"enabled": bool(submitted.get("enabled", False)), "start": start, "end": end}

def _clean_notif_category(key, submitted, current, valid_template_ids):
    """Validate/sanitize one category's submitted notifications block,
    restoring a masked target URL (by target id) from the currently-saved
    value so an unedited row doesn't get overwritten with the literal mask."""
    submitted = submitted if isinstance(submitted, dict) else {}
    current = current if isinstance(current, dict) else {}
    cur_by_id = {t.get("id"): t for t in (current.get("targets") or []) if t.get("id")}
    out_targets = []
    for row in (submitted.get("targets") or [])[:APPRISE_URL_CAP]:
        if not isinstance(row, dict):
            continue
        url, row_id = (row.get("url") or "").strip(), row.get("id")
        cur = cur_by_id.get(row_id)
        if url == "••••••••" and cur:
            url = cur.get("url", "")
        elif not row_id or row_id not in cur_by_id:
            row_id = secrets.token_hex(4)
        if not url:
            continue
        template_id = row.get("template_id")
        if template_id not in valid_template_ids:
            template_id = _CATEGORY_DEFAULT_TEMPLATE_ID.get(key)
        out_targets.append({
            "id": row_id,
            "url": url,
            "enabled": bool(row.get("enabled", True)),
            "template_id": template_id,
            "filters": _clean_filter_text(row.get("filters", "")),
        })
    result = {"targets": out_targets, "filters": _clean_filter_text(submitted.get("filters", ""))}
    if "quiet_hours" in DEFAULTS["notifications"][key]:
        result["quiet_hours"] = _clean_quiet_hours(
            submitted.get("quiet_hours"), current.get("quiet_hours", DEFAULTS["notifications"][key]["quiet_hours"]))
    return result

NOTIF_CATEGORIES = ["aircraft", "satellites", "digest"]
TEMPLATES_CAP = 20
TEMPLATE_NAME_CAP = 60

_PROTECTED_TEMPLATE_IDS = {t["id"] for t in DEFAULT_TEMPLATES_LIST}

def _clean_templates(submitted, current):
    """Caps count/lengths, keeps `kind` immutable for an existing id (aircraft
    vs satellite token sets differ), and always guarantees the two protected
    default templates survive with their canonical content. The UI disables
    their fields, but this is the defense-in-depth enforcement: a submitted
    row for a protected id is ignored in favor of the hardcoded default,
    regardless of what it contains."""
    submitted = submitted if isinstance(submitted, list) else []
    cur_by_id = {t.get("id"): t for t in (current or []) if isinstance(t, dict) and t.get("id")}
    default_by_id = {t["id"]: t for t in DEFAULT_TEMPLATES_LIST}
    out, seen_ids = [], set()
    for row in submitted[:TEMPLATES_CAP]:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if rid in _PROTECTED_TEMPLATE_IDS:
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            out.append(dict(default_by_id[rid]))
            continue
        cur = cur_by_id.get(rid)
        kind = cur.get("kind") if cur else row.get("kind")
        if kind not in ("aircraft", "satellite", "digest"):
            continue
        # A brand-new template's client-generated id must be preserved (not
        # replaced) so a target's template_id in the same save payload, set
        # client-side before the row existed on the server, still resolves.
        if not cur and (not isinstance(rid, str) or not rid):
            rid = secrets.token_hex(4)
        if rid in seen_ids:
            continue
        name = str(row.get("name", "")).strip()[:TEMPLATE_NAME_CAP]
        title = str(row.get("title", ""))[:TEMPLATE_CAP]
        body = str(row.get("body", ""))[:TEMPLATE_CAP]
        if not name or not title.strip() or not body.strip():
            continue
        seen_ids.add(rid)
        out.append({"id": rid, "name": name, "kind": kind, "title": title, "body": body})
    for d in DEFAULT_TEMPLATES_LIST:
        if d["id"] not in seen_ids:
            out.append(dict(d))
    return out

def _clean_notifications(submitted, current, valid_template_ids):
    submitted = submitted if isinstance(submitted, dict) else {}
    current = current if isinstance(current, dict) else {}
    return {key: _clean_notif_category(key, submitted.get(key) or {}, current.get(key) or {}, valid_template_ids)
            for key in NOTIF_CATEGORIES}

@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = read_config()
    # Only return known fields. Never echo stray/legacy keys that may
    # still be sitting in config.json (e.g. old opensky_user/opensky_pass).
    masked = {k: cfg.get(k, DEFAULTS[k]) for k in UI_FIELDS}
    for k in ("opensky_client_secret","n2yo_api_key"):
        if masked.get(k):
            masked[k] = "••••••••"
    masked["notifications"] = _mask_notifications(cfg.get("notifications", DEFAULTS["notifications"]))
    return jsonify(masked)

@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    if not data:
        abort(400)
    # Don't overwrite masked secrets with placeholder
    current = read_config()
    for k in ("opensky_client_secret","n2yo_api_key"):
        if data.get(k) == "••••••••":
            data[k] = current.get(k, "")
    if "templates" in data:
        data["templates"] = _clean_templates(data["templates"], current.get("templates", []))
    if "notifications" in data:
        valid_template_ids = {t["id"] for t in data.get("templates", current.get("templates", []))}
        data["notifications"] = _clean_notifications(data["notifications"], current.get("notifications", {}), valid_template_ids)
    if "digest_send_time" in data and not _HHMM_RE.match(str(data["digest_send_time"])):
        data.pop("digest_send_time", None)
    for k, (lo, hi) in NUMERIC_BOUNDS.items():
        if k in data:
            try:
                clamped = max(lo, min(hi, float(data[k])))
                data[k] = type(DEFAULTS[k])(clamped)
            except (TypeError, ValueError):
                data.pop(k, None)
    for k, allowed in ENUM_FIELDS.items():
        if k in data and data[k] not in allowed:
            data.pop(k, None)
    saved = write_config(data)
    return jsonify({"ok": True})

def _sample_placeholders(category, cfg):
    """Fake but realistic values for the "Send test alert" button, unit-aware
    per the Display tab's units_speed setting (same conversions as
    process_states in tracker.py) and time-format-aware per time_format, so a
    test alert previews the units/format a real alert would actually use."""
    if category == "digest":
        return {"aircraft_count": "3", "aircraft_closest": "closest was QFA123 (BOEING 737-8AS) at 8.4 km",
                "satellite_count": "2", "satellite_list": "ISS at 8:15 PM, Starlink at 9:40 PM",
                "astronomy_highlight": "Jupiter visible; Perseids meteor shower active; Bortle: Suburban sky"}
    if category != "aircraft":
        sample_time = "8:15 PM" if cfg.get("time_format", "12h") == "12h" else "20:15"
        return {"sat_name": "ISS", "time": sample_time, "minutes": "12", "duration": "420",
                "start_az": "NW", "max_el": "45°", "end_az": "NE"}
    metric = cfg.get("units_speed", "aviation") == "metric"
    speed          = f"{round(450 * 1.852)} km/h" if metric else "450 kts"
    vertical_speed = f"↑ {round(800 * 0.3048)} m/min" if metric else "↑ 800 fpm"
    altitude       = f"{round(5200 * 0.3048):,} m" if metric else "5,200 ft"
    return {
        "emoji": "✈️", "callsign": "QFA123", "model": "BOEING 737-8AS",
        "registration": "VH-ABC", "country": "Australia", "direction": "NW",
        "speed": speed, "vertical_speed": vertical_speed, "airframe": "jet",
        "altitude": altitude, "distance_km": "8.4",
        "icao24": "7c1234", "lat": "-33.8688", "long": "151.2093",
        "heading": "270", "squawk": "7000",
    }

@app.route("/api/test-alert/<category>", methods=["POST"])
def api_test_alert(category):
    """Send a test notification for one target using whatever url/template is
    currently in its settings-page card, not necessarily saved yet, so a
    target can be test-fired before it's ever written to config.json."""
    if category not in NOTIF_CATEGORIES:
        abort(404)
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url or url == "••••••••":
        return jsonify({"error": "Enter a target URL first"})
    cfg = read_config()
    values = _sample_placeholders(category, cfg)
    templates_by_id = {t["id"]: t for t in cfg.get("templates", [])}
    defaults = templates_by_id.get(_CATEGORY_DEFAULT_TEMPLATE_ID[category], {})
    tmpl = templates_by_id.get(data.get("template_id")) or defaults
    title = "✅ " + _render_template(tmpl.get("title", ""), values)
    body  = _render_template(tmpl.get("body", ""), values)
    a = apprise.Apprise()
    if not a.add(url):
        return jsonify({"error": "Invalid Apprise URL"})
    return jsonify({"result": "ok" if a.notify(title=title, body=body) else "failed"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)


# ── Weather & Moon API endpoints (appended) ───────────────────────────────────
@app.route("/api/weather/current")
def api_weather_current():
    try:
        row = db().execute("SELECT data FROM weather_current WHERE id=1").fetchone()
        return jsonify(json.loads(row["data"]) if row else {})
    except Exception:
        return jsonify({})

@app.route("/api/weather/hourly")
def api_weather_hourly():
    try:
        row = db().execute("SELECT data FROM weather_hourly WHERE id=1").fetchone()
        return jsonify(json.loads(row["data"]) if row else [])
    except Exception:
        return jsonify([])

@app.route("/api/weather/daily")
def api_weather_daily():
    try:
        row = db().execute("SELECT data FROM weather_daily WHERE id=1").fetchone()
        return jsonify(json.loads(row["data"]) if row else [])
    except Exception:
        return jsonify([])

@app.route("/api/moon")
def api_moon():
    try:
        row = db().execute("SELECT data FROM moon_phase WHERE id=1").fetchone()
        return jsonify(json.loads(row["data"]) if row else {})
    except Exception:
        return jsonify({})

@app.route("/api/astronomy")
def api_astronomy():
    try:
        row = db().execute("SELECT data FROM astronomy_data WHERE id=1").fetchone()
        return jsonify(json.loads(row["data"]) if row else {})
    except Exception:
        return jsonify({})

@app.route("/api/status")
def api_status():
    try:
        rows = db().execute("SELECT * FROM source_status").fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

@app.route("/api/health")
def api_health():
    db_ok = True
    sources = {}
    tracker_alive = False
    now = int(time.time())
    try:
        db().execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False
    try:
        rows = db().execute(
            "SELECT source, status, last_success, last_error FROM source_status").fetchall()
        for r in rows:
            sources[r["source"]] = {
                "status": r["status"], "last_success": r["last_success"], "last_error": r["last_error"]}
            if r["last_success"] and now - r["last_success"] < 300:
                tracker_alive = True
    except Exception:
        db_ok = False
    any_error = any(s["status"] == "error" for s in sources.values())
    status = "ok" if (db_ok and tracker_alive and not any_error) else "degraded"
    return jsonify({"status": status, "db": db_ok, "tracker_alive": tracker_alive, "sources": sources})

import json as _json  # ensure json available in appended scope
