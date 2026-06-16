"""
SkyBro Dashboard — Flask app
Serves the live map UI, API endpoints, and the settings page.
Settings are persisted to /data/config.json (hot-reloaded by tracker).
"""

import os, json, sqlite3, time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, abort

app = Flask(__name__)
DATA_DIR = Path("/data")
DB_PATH  = DATA_DIR / "skybro.db"
CFG_PATH = DATA_DIR / "config.json"

DEFAULTS = {
    "home_lat": 1.3521, "home_lon": 103.8198,
    "radius_km": 15.0, "alt_threshold_ft": 5000.0,
    "poll_interval": 15, "iss_check_hours": 2, "iss_warn_mins": 20,
    "pushover_token": "", "pushover_user": "",
    "discord_webhook": "", "opensky_client_id": "", "opensky_client_secret": "",
    "n2yo_api_key": "",
    "alerts_enabled": True, "iss_alerts_enabled": True,
    "use_location_time": False, "time_format": "12h",
    "units_speed": "imperial", "units_temp": "imperial",
}

# Fields the UI is allowed to read/write
UI_FIELDS = list(DEFAULTS.keys())

def read_config():
    if not CFG_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(CFG_PATH) as f:
            return {**DEFAULTS, **json.load(f)}
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
    return render_template("index.html", cfg=cfg)

@app.route("/settings")
def settings():
    return render_template("settings.html", cfg=read_config())

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
            "WHERE pass_time > ? ORDER BY pass_time ASC LIMIT 20",
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

@app.route("/api/history")
def api_history():
    try:
        rows = db().execute("""
            SELECT sa.icao24, sa.callsign, sa.model, sa.registration, sa.origin_country,
                   sa.min_alt_ft, sa.min_dist_km, sa.first_seen, sa.last_seen,
                   sa.lat, sa.lon, COALESCE(sa.heading, 0) AS heading,
                   COALESCE(sa.category, 0) AS category,
                   COALESCE(sa.favourited, 0) AS favourited,
                   COALESCE(pc.thumb_url, sa.photo_url) AS thumb_url,
                   pc.photo_url AS full_photo_url
            FROM seen_aircraft sa
            LEFT JOIN photo_cache pc ON pc.icao24 = sa.icao24
            WHERE sa.alerted=1
            ORDER BY sa.last_seen DESC LIMIT 100
        """).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

@app.route("/api/history/favourite", methods=["POST"])
def api_history_favourite():
    data = request.get_json(force=True)
    icao24 = (data or {}).get("icao24", "").strip().lower()
    if not icao24:
        abort(400)
    try:
        conn = db()
        row = conn.execute("SELECT COALESCE(favourited,0) FROM seen_aircraft WHERE icao24=?", (icao24,)).fetchone()
        if not row:
            abort(404)
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE seen_aircraft SET favourited=? WHERE icao24=?", (new_val, icao24))
        conn.commit()
        return jsonify({"ok": True, "favourited": new_val})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = read_config()
    # Mask secrets in GET response
    masked = dict(cfg)
    for k in ("pushover_token","pushover_user","discord_webhook","opensky_client_secret"):
        if masked.get(k):
            masked[k] = "••••••••"
    return jsonify(masked)

@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    if not data:
        abort(400)
    # Don't overwrite masked secrets with placeholder
    current = read_config()
    for k in ("pushover_token","pushover_user","discord_webhook","opensky_client_secret"):
        if data.get(k) == "••••••••":
            data[k] = current.get(k, "")
    saved = write_config(data)
    return jsonify({"ok": True})

@app.route("/api/test-alert", methods=["POST"])
def api_test_alert():
    """Send a test notification using current config."""
    import requests as req
    cfg = read_config()
    results = {}
    if cfg.get("pushover_token") and cfg.get("pushover_user"):
        try:
            r = req.post("https://api.pushover.net/1/messages.json", data={
                "token": cfg["pushover_token"], "user": cfg["pushover_user"],
                "title": "✅ SkyBro test", "message": "Alerts are working!",
            }, timeout=10)
            results["pushover"] = "ok" if r.ok else f"error {r.status_code}"
        except Exception as e:
            results["pushover"] = str(e)
    if cfg.get("discord_webhook"):
        try:
            r = req.post(cfg["discord_webhook"], json={"embeds":[{
                "title":"✅ SkyBro test","description":"Alerts are working!","color":0x2ecc71
            }]}, timeout=10)
            results["discord"] = "ok" if r.ok else f"error {r.status_code}"
        except Exception as e:
            results["discord"] = str(e)
    if not results:
        results["error"] = "No notification services configured"
    return jsonify(results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)


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

import json as _json  # ensure json available in appended scope
