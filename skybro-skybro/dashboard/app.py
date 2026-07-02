"""
SkyBro Dashboard — Flask app
Serves the live map UI, API endpoints, and the settings page.
Settings are persisted to /data/config.json (hot-reloaded by tracker).
"""

import os, json, sqlite3, time

APP_VERSION = "1.5.0"
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
    "units_speed": "aviation", "units_temp": "imperial",
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
    first_run = (cfg.get("home_lat") == DEFAULTS["home_lat"] and
                 cfg.get("home_lon") == DEFAULTS["home_lon"])
    return render_template("index.html", cfg=cfg,
                           app_version=APP_VERSION,
                           git_sha=os.environ.get("GIT_SHA", "").strip(),
                           first_run=first_run)

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
    # Only return known fields — never echo stray/legacy keys that may
    # still be sitting in config.json (e.g. old opensky_user/opensky_pass).
    masked = {k: cfg.get(k, DEFAULTS[k]) for k in UI_FIELDS}
    for k in ("pushover_token","pushover_user","discord_webhook","opensky_client_secret","n2yo_api_key"):
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
    for k in ("pushover_token","pushover_user","discord_webhook","opensky_client_secret","n2yo_api_key"):
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
