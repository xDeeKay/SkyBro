"""
SkyBro — Astronomy module
Calculates planet positions, DSO visibility, twilight times, and meteor showers
using the ephem library. All heavy lifting is local — no API keys needed except
for the optional Bortle/SQM lookup from lightpollutionmap.info.
"""

import math, json, time, logging, requests
from datetime import datetime, timezone, timedelta
import ephem
from weather import moon_phase as _calc_moon_phase

log = logging.getLogger("skybro.astronomy")

# ── Static catalogues ─────────────────────────────────────────────────────────

PLANETS = [
    ("Mercury", ephem.Mercury),
    ("Venus",   ephem.Venus),
    ("Mars",    ephem.Mars),
    ("Jupiter", ephem.Jupiter),
    ("Saturn",  ephem.Saturn),
    ("Uranus",  ephem.Uranus),
    ("Neptune", ephem.Neptune),
]

_PLANET_WIKI = {
    "Mercury": "Mercury (planet)",
    "Venus":   "Venus",
    "Mars":    "Mars",
    "Jupiter": "Jupiter",
    "Saturn":  "Saturn",
    "Uranus":  "Uranus",
    "Neptune": "Neptune",
}

# (number, common_name, type, ra_hours, dec_degrees, magnitude, constellation, size_arcmin, equip)
# equip: 0=naked eye  1=binoculars  2=telescope
MESSIER = [
    (1,   "Crab Nebula",           "Supernova Remnant",    5.5753,  22.0145,  8.4, "Tau", 7,   2),
    (2,   "",                      "Globular Cluster",    21.5578,  -0.8233,  6.5, "Aqr", 16,  1),
    (3,   "",                      "Globular Cluster",    13.7033,  28.3767,  6.2, "CVn", 18,  1),
    (4,   "",                      "Globular Cluster",    16.3933, -26.5317,  5.6, "Sco", 36,  1),
    (5,   "",                      "Globular Cluster",    15.3097,   2.0817,  5.6, "Ser", 23,  1),
    (6,   "Butterfly Cluster",     "Open Cluster",        17.6683, -32.2133,  4.2, "Sco", 15,  1),
    (7,   "Ptolemy's Cluster",     "Open Cluster",        17.8983, -34.8267,  3.3, "Sco", 80,  0),
    (8,   "Lagoon Nebula",         "Emission Nebula",     18.0617, -24.3833,  5.8, "Sgr", 90,  1),
    (9,   "",                      "Globular Cluster",    17.3200, -18.5150,  7.7, "Oph", 12,  2),
    (10,  "",                      "Globular Cluster",    16.9500,  -4.1000,  6.6, "Oph", 15,  1),
    (11,  "Wild Duck Cluster",     "Open Cluster",        18.8517,  -6.2700,  5.8, "Sct", 14,  1),
    (12,  "",                      "Globular Cluster",    16.7867,  -1.9483,  6.7, "Oph", 16,  1),
    (13,  "Great Hercules Cluster","Globular Cluster",    16.6950,  36.4617,  5.8, "Her", 20,  1),
    (14,  "",                      "Globular Cluster",    17.6267,  -3.2467,  7.6, "Oph", 12,  2),
    (15,  "Great Pegasus Cluster", "Globular Cluster",    21.4997,  12.1667,  6.2, "Peg", 18,  1),
    (16,  "Eagle Nebula",          "Open Cluster+Nebula", 18.3133, -13.7933,  6.0, "Ser", 7,   1),
    (17,  "Omega Nebula",          "Emission Nebula",     18.3467, -16.1767,  6.0, "Sgr", 46,  1),
    (18,  "",                      "Open Cluster",        18.3317, -17.1433,  6.9, "Sgr", 9,   1),
    (19,  "",                      "Globular Cluster",    17.0433, -26.2683,  6.8, "Oph", 17,  1),
    (20,  "Trifid Nebula",         "Emission Nebula",     18.0433, -23.0267,  6.3, "Sgr", 28,  1),
    (21,  "",                      "Open Cluster",        18.0767, -22.5000,  5.9, "Sgr", 13,  1),
    (22,  "",                      "Globular Cluster",    18.6083, -23.9050,  5.1, "Sgr", 32,  1),
    (23,  "",                      "Open Cluster",        17.9467, -18.9850,  5.5, "Sgr", 27,  1),
    (24,  "Sagittarius Star Cloud","Star Cloud",          18.2833, -18.5500,  4.6, "Sgr", 90,  0),
    (25,  "",                      "Open Cluster",        18.5267, -19.2333,  4.6, "Sgr", 32,  0),
    (26,  "",                      "Open Cluster",        18.7550,  -9.3817,  8.0, "Sct", 15,  2),
    (27,  "Dumbbell Nebula",       "Planetary Nebula",    19.9933,  22.7217,  7.4, "Vul", 8,   1),
    (28,  "",                      "Globular Cluster",    18.4083, -24.8700,  6.8, "Sgr", 11,  2),
    (29,  "",                      "Open Cluster",        20.3967,  38.5117,  6.6, "Cyg", 7,   1),
    (30,  "",                      "Globular Cluster",    21.6717, -23.1800,  7.2, "Cap", 11,  2),
    (31,  "Andromeda Galaxy",      "Galaxy",               0.7122,  41.2689,  3.4, "And", 190, 0),
    (32,  "",                      "Galaxy",               0.7117,  40.8650,  8.7, "And", 8,   2),
    (33,  "Triangulum Galaxy",     "Galaxy",               1.5644,  30.6600,  5.7, "Tri", 73,  1),
    (34,  "",                      "Open Cluster",         2.7017,  42.7450,  5.2, "Per", 35,  1),
    (35,  "",                      "Open Cluster",         6.1483,  24.3383,  5.1, "Gem", 28,  1),
    (36,  "",                      "Open Cluster",         5.6033,  34.1350,  6.0, "Aur", 12,  1),
    (37,  "",                      "Open Cluster",         5.8717,  32.5517,  5.6, "Aur", 24,  1),
    (38,  "",                      "Open Cluster",         5.4783,  35.8517,  6.4, "Aur", 21,  1),
    (39,  "",                      "Open Cluster",        21.5317,  48.4267,  4.6, "Cyg", 32,  0),
    (40,  "Winnecke 4",            "Double Star",         12.3700,  58.0850,  8.0, "UMa", 1,   2),
    (41,  "",                      "Open Cluster",         6.7667, -20.7417,  4.5, "CMa", 38,  0),
    (42,  "Orion Nebula",          "Emission Nebula",      5.5883,  -5.3900,  4.0, "Ori", 65,  0),
    (43,  "De Mairan's Nebula",    "Emission Nebula",      5.5933,  -5.2717,  9.0, "Ori", 20,  2),
    (44,  "Beehive Cluster",       "Open Cluster",         8.6700,  19.9833,  3.1, "Cnc", 95,  0),
    (45,  "Pleiades",              "Open Cluster",         3.7900,  24.1167,  1.6, "Tau", 110, 0),
    (46,  "",                      "Open Cluster",         7.6967, -14.8150,  6.1, "Pup", 27,  1),
    (47,  "",                      "Open Cluster",         7.6117, -14.4900,  4.4, "Pup", 30,  0),
    (48,  "",                      "Open Cluster",         8.2300,  -5.8000,  5.8, "Hya", 54,  1),
    (49,  "",                      "Galaxy",              12.4967,   8.0000,  8.4, "Vir", 9,   2),
    (50,  "",                      "Open Cluster",         7.0433,  -8.3333,  5.9, "Mon", 16,  1),
    (51,  "Whirlpool Galaxy",      "Galaxy",              13.4958,  47.1950,  8.4, "CVn", 11,  2),
    (52,  "",                      "Open Cluster",        23.4050,  61.5933,  6.9, "Cas", 13,  1),
    (53,  "",                      "Globular Cluster",    13.2150,  18.1683,  7.6, "Com", 13,  2),
    (54,  "",                      "Globular Cluster",    18.9183, -30.4800,  7.6, "Sgr", 12,  2),
    (55,  "",                      "Globular Cluster",    19.6667, -30.9617,  6.3, "Sgr", 19,  1),
    (56,  "",                      "Globular Cluster",    19.2767,  30.1850,  8.3, "Lyr", 7,   2),
    (57,  "Ring Nebula",           "Planetary Nebula",    18.8933,  33.0283,  8.8, "Lyr", 1,   2),
    (58,  "",                      "Galaxy",              12.6283,  11.8183,  9.7, "Vir", 6,   2),
    (59,  "",                      "Galaxy",              12.7000,  11.6467,  9.6, "Vir", 5,   2),
    (60,  "",                      "Galaxy",              12.7267,  11.5533,  8.8, "Vir", 7,   2),
    (61,  "",                      "Galaxy",              12.3650,   4.4733,  9.7, "Vir", 6,   2),
    (62,  "",                      "Globular Cluster",    17.0200, -30.1133,  6.5, "Oph", 15,  1),
    (63,  "Sunflower Galaxy",      "Galaxy",              13.2642,  42.0294,  8.6, "CVn", 13,  2),
    (64,  "Black Eye Galaxy",      "Galaxy",              12.9458,  21.6825,  8.5, "Com", 10,  2),
    (65,  "",                      "Galaxy",              11.3150,  13.0917,  9.3, "Leo", 8,   2),
    (66,  "",                      "Galaxy",              11.3367,  12.9917,  8.9, "Leo", 8,   2),
    (67,  "",                      "Open Cluster",         8.8567,  11.8000,  6.9, "Cnc", 30,  1),
    (68,  "",                      "Globular Cluster",    12.6567, -26.7450,  7.6, "Hya", 12,  2),
    (69,  "",                      "Globular Cluster",    18.5233, -32.3483,  7.6, "Sgr", 10,  2),
    (70,  "",                      "Globular Cluster",    18.7233, -32.2983,  7.8, "Sgr", 8,   2),
    (71,  "",                      "Globular Cluster",    19.8967,  18.7783,  6.1, "Sge", 7,   1),
    (72,  "",                      "Globular Cluster",    20.8917, -12.5367,  9.3, "Aqr", 6,   2),
    (73,  "",                      "Asterism",            20.9867, -12.6333,  9.0, "Aqr", 3,   2),
    (74,  "",                      "Galaxy",               1.6117,  15.7833,  9.4, "Psc", 10,  2),
    (75,  "",                      "Globular Cluster",    20.1017, -21.9217,  8.5, "Sgr", 6,   2),
    (76,  "Little Dumbbell",       "Planetary Nebula",     1.7033,  51.5750, 10.1, "Per", 3,   2),
    (77,  "",                      "Galaxy",               2.7117,  -0.0133,  8.9, "Cet", 7,   2),
    (78,  "",                      "Reflection Nebula",    5.7800,   0.0833,  8.3, "Ori", 8,   2),
    (79,  "",                      "Globular Cluster",     5.4033, -24.5233,  7.7, "Lep", 10,  2),
    (80,  "",                      "Globular Cluster",    16.2833, -22.9750,  7.3, "Sco", 10,  2),
    (81,  "Bode's Galaxy",         "Galaxy",               9.9258,  69.0653,  6.9, "UMa", 21,  1),
    (82,  "Cigar Galaxy",          "Galaxy",               9.9292,  69.6797,  8.4, "UMa", 11,  2),
    (83,  "Southern Pinwheel",     "Galaxy",              13.6167, -29.8650,  7.5, "Hya", 13,  1),
    (84,  "",                      "Galaxy",              12.4200,  12.8867,  9.1, "Vir", 5,   2),
    (85,  "",                      "Galaxy",              12.4233,  18.1917,  9.1, "Com", 7,   2),
    (86,  "",                      "Galaxy",              12.4367,  12.9467,  8.9, "Vir", 7,   2),
    (87,  "Virgo A",               "Galaxy",              12.5136,  12.3911,  8.6, "Vir", 7,   2),
    (88,  "",                      "Galaxy",              12.5317,  14.4217,  9.6, "Com", 7,   2),
    (89,  "",                      "Galaxy",              12.5950,  12.5567,  9.7, "Vir", 4,   2),
    (90,  "",                      "Galaxy",              12.6133,  13.1633,  9.5, "Vir", 7,   2),
    (91,  "",                      "Galaxy",              12.5917,  14.4967, 10.2, "Com", 5,   2),
    (92,  "",                      "Globular Cluster",    17.2850,  43.1367,  6.4, "Her", 11,  1),
    (93,  "",                      "Open Cluster",         7.7433, -23.8617,  6.0, "Pup", 22,  1),
    (94,  "",                      "Galaxy",              12.8508,  41.1203,  8.2, "CVn", 11,  2),
    (95,  "",                      "Galaxy",              10.7317,  11.7033,  9.7, "Leo", 7,   2),
    (96,  "",                      "Galaxy",              10.7767,  11.8200,  9.2, "Leo", 7,   2),
    (97,  "Owl Nebula",            "Planetary Nebula",    11.2467,  55.0183,  9.9, "UMa", 3,   2),
    (98,  "",                      "Galaxy",              12.2317,  14.9000, 10.1, "Com", 10,  2),
    (99,  "",                      "Galaxy",              12.3133,  14.4167,  9.9, "Com", 9,   2),
    (100, "",                      "Galaxy",              12.3817,  15.8217,  9.3, "Com", 7,   2),
    (101, "Pinwheel Galaxy",       "Galaxy",              14.0533,  54.3489,  7.9, "UMa", 29,  2),
    (102, "Spindle Galaxy",        "Galaxy",              15.1050,  55.7650,  9.9, "Dra", 5,   2),
    (103, "",                      "Open Cluster",         1.5567,  60.6567,  7.4, "Cas", 6,   1),
    (104, "Sombrero Galaxy",       "Galaxy",              12.6669, -11.6231,  8.0, "Vir", 9,   1),
    (105, "",                      "Galaxy",              10.7983,  12.5817,  9.3, "Leo", 4,   2),
    (106, "",                      "Galaxy",              12.3161,  47.3036,  8.4, "CVn", 19,  2),
    (107, "",                      "Globular Cluster",    16.5417, -13.0533,  7.8, "Oph", 13,  2),
    (108, "",                      "Galaxy",              11.1900,  55.6733, 10.0, "UMa", 8,   2),
    (109, "",                      "Galaxy",              11.9600,  53.3750,  9.8, "UMa", 7,   2),
    (110, "",                      "Galaxy",               0.6733,  41.6850,  8.1, "And", 17,  2),
]

# (name, start mm-dd, end mm-dd, peak mm-dd, zhr, radiant constellation, parent body, note)
METEOR_SHOWERS = [
    ("Quadrantids",       "01-01", "01-05", "01-04", 120, "Boo", "2003 EH1",          "Best from Northern Hemisphere; poor southern viewing"),
    ("Lyrids",            "04-14", "04-30", "04-22",  18, "Lyr", "Comet Thatcher",     ""),
    ("Eta Aquariids",     "04-19", "05-28", "05-06",  50, "Aqr", "Halley's Comet",     "Excellent from Southern Hemisphere — one of the year's best"),
    ("Delta Aquariids",   "07-12", "08-23", "07-29",  25, "Aqr", "Comet Marsden",      "Best from Southern Hemisphere"),
    ("Alpha Capricornids","07-03", "08-15", "08-01",   5, "Cap", "Comet 169P/NEAT",    "Slow bright fireballs"),
    ("Perseids",          "07-14", "09-01", "08-12", 100, "Per", "Comet Swift-Tuttle", "Radiant stays low from Southern Hemisphere"),
    ("Orionids",          "10-02", "11-07", "10-21",  20, "Ori", "Halley's Comet",     ""),
    ("Taurids",           "10-20", "12-10", "11-08",  10, "Tau", "Comet Encke",        "Slow with bright fireballs"),
    ("Leonids",           "11-05", "11-30", "11-18",  15, "Leo", "Comet Tempel-Tuttle",""),
    ("Geminids",          "12-04", "12-20", "12-14", 120, "Gem", "3200 Phaethon",      "Good from both hemispheres; one of the year's best"),
    ("Ursids",            "12-17", "12-26", "12-23",  10, "UMi", "Comet Tuttle",       "Northern Hemisphere only"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _az_compass(az_deg):
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(az_deg / 22.5) % 16]

def _make_obs(lat, lon, ts=None):
    obs = ephem.Observer()
    obs.lat       = str(lat)
    obs.lon       = str(lon)
    obs.elevation = 0
    obs.pressure  = 0  # disable atmospheric refraction
    obs.date = ephem.Date(datetime.utcfromtimestamp(ts or time.time())
                          .strftime("%Y/%m/%d %H:%M:%S"))
    return obs

def _to_ts(ephem_date):
    try:
        return int(ephem.Date(ephem_date).datetime()
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None

# ── Calculations ──────────────────────────────────────────────────────────────

def _twilight_times(lat, lon, ts):
    results = {}
    sun = ephem.Sun()
    for label, horizon in [("civil", "-6"), ("nautical", "-12"), ("astronomical", "-18")]:
        obs = _make_obs(lat, lon, ts)
        obs.horizon = horizon
        for event, fn in [("dusk", "next_setting"), ("dawn", "next_rising")]:
            try:
                results[f"{label}_{event}"] = _to_ts(getattr(obs, fn)(sun, use_center=True))
            except Exception:
                results[f"{label}_{event}"] = None
    obs = _make_obs(lat, lon, ts)
    obs.horizon = "0"
    for event, fn in [("sunset", "next_setting"), ("sunrise", "next_rising")]:
        try:
            results[event] = _to_ts(getattr(obs, fn)(sun, use_center=True))
        except Exception:
            results[event] = None
    return results


def _planet_info(lat, lon, ts, name, planet_cls):
    obs  = _make_obs(lat, lon, ts)
    body = planet_cls()
    body.compute(obs)

    alt = math.degrees(float(body.alt))
    az  = math.degrees(float(body.az))

    info = {
        "name":         name,
        "alt_deg":      round(alt, 1),
        "az_deg":       round(az, 1),
        "az_compass":   _az_compass(az),
        "magnitude":    round(float(body.mag), 1),
        "is_up":        alt > 10,
        "distance_au":  round(float(body.earth_distance), 3),
    }
    try:
        info["constellation"] = ephem.constellation(body)[1]
    except Exception:
        info["constellation"] = ""

    for event, fn, hz in [("rise_ts", "next_rising", "0"),
                           ("set_ts",  "next_setting", "0"),
                           ("transit_ts", "next_transit", "0")]:
        obs2 = _make_obs(lat, lon, ts)
        obs2.horizon = hz
        try:
            info[event] = _to_ts(getattr(obs2, fn)(body))
        except Exception:
            info[event] = None

    return info


def _dso_info(lat, lon, ts, row, moon_body):
    m_num, name, obj_type, ra_h, dec_deg, mag, const, size, equip = row

    body      = ephem.FixedBody()
    body._ra  = ra_h * math.pi / 12
    body._dec = dec_deg * math.pi / 180
    obs = _make_obs(lat, lon, ts)
    body.compute(obs)

    alt = math.degrees(float(body.alt))
    az  = math.degrees(float(body.az))

    try:
        moon_sep = math.degrees(ephem.separation(body, moon_body))
    except Exception:
        moon_sep = 180.0

    info = {
        "id":           m_num,
        "label":        f"M{m_num}" + (f" — {name}" if name else ""),
        "name":         name or f"M{m_num}",
        "type":         obj_type,
        "constellation": const,
        "magnitude":    mag,
        "size_arcmin":  size,
        "alt_deg":      round(alt, 1),
        "az_deg":       round(az, 1),
        "az_compass":   _az_compass(az),
        "is_up":        alt > 10,
        "equipment":    ("naked_eye", "binoculars", "telescope")[equip],
        "moon_sep_deg": round(moon_sep, 1),
        "moon_warning": moon_sep < 30,
    }

    obs2 = _make_obs(lat, lon, ts)
    obs2.horizon = "0"
    try:
        info["transit_ts"] = _to_ts(obs2.next_transit(body))
    except Exception:
        info["transit_ts"] = None

    return info


def _meteor_showers():
    now  = datetime.now(timezone.utc)
    year = now.year
    mmdd = now.strftime("%m-%d")
    result = []
    for name, start, end, peak, zhr, radiant, parent, note in METEOR_SHOWERS:
        active = start <= mmdd <= end

        peak_dt = datetime.strptime(f"{year}-{peak}", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_to = (peak_dt - now).days
        if days_to < -15:
            peak_dt = peak_dt.replace(year=year + 1)
            days_to = (peak_dt - now).days

        result.append({
            "name":         name,
            "active":       active,
            "active_start": start,
            "active_end":   end,
            "peak_date":    peak_dt.strftime("%Y-%m-%d"),
            "zhr":          zhr,
            "radiant":      radiant,
            "parent":       parent,
            "note":         note,
            "days_to_peak": days_to,
        })

    result.sort(key=lambda x: (not x["active"],
                                x["days_to_peak"] if x["days_to_peak"] >= 0 else 366))
    return result


def _fetch_wiki_thumbs(queries):
    """Batch-fetch Wikipedia page thumbnails (50 per request). Returns {query: thumb_url}."""
    result = {q: "" for q in queries}
    headers = {"User-Agent": "SkyBro/1.0 (https://github.com/xDeeKay/SkyBro)"}
    for i in range(0, len(queries), 50):
        batch = queries[i:i+50]
        try:
            r = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action":      "query",
                    "prop":        "pageimages",
                    "titles":      "|".join(batch),
                    "pithumbsize": 200,
                    "redirects":   "1",
                    "format":      "json",
                },
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("query", {})
            chain = {q: q for q in batch}
            for n in data.get("normalized", []):
                chain[n["from"]] = n["to"]
            for rd in data.get("redirects", []):
                for k in list(chain):
                    if chain[k] == rd["from"]:
                        chain[k] = rd["to"]
            inv = {v: k for k, v in chain.items()}
            for page in data.get("pages", {}).values():
                title = page.get("title", "")
                thumb = (page.get("thumbnail") or {}).get("source", "")
                if thumb and title in inv:
                    result[inv[title]] = thumb
        except Exception as e:
            log.warning(f"Wikipedia thumbs batch ({len(batch)} items): {e}")
    return result


def _bortle(lat, lon):
    try:
        import json as _j
        url = ("https://www.lightpollutionmap.info/QueryRaster/"
               f"?ql=wa_2015&qt=point&qd={_j.dumps({'lonlat':[lon, lat]})}")
        r = requests.get(url, timeout=10, headers={"User-Agent": "SkyBro/1.2"})
        r.raise_for_status()
        sqm = float(r.text.strip())
        thresholds = [(21.99,1),(21.89,2),(21.69,3),(20.49,4),
                      (19.25,5),(18.38,6),(17.50,7),(16.50,8)]
        cls = 9
        for thresh, b in thresholds:
            if sqm >= thresh:
                cls = b
                break
        descs = {1:"Darkest skies",2:"Truly dark site",3:"Rural sky",
                 4:"Rural/suburban transition",5:"Suburban sky",
                 6:"Bright suburban sky",7:"Suburban/urban transition",
                 8:"City sky",9:"Inner city sky"}
        return {"sqm": round(sqm, 2), "class": cls, "description": descs[cls]}
    except Exception as e:
        log.warning(f"Bortle fetch: {e}")
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

def process_astronomy(lat, lon, conn):
    ts = int(time.time())

    twilight = _twilight_times(lat, lon, ts)

    # Moon — computed once, reused for DSO separation checks
    moon_obs  = _make_obs(lat, lon, ts)
    moon_body = ephem.Moon()
    moon_body.compute(moon_obs)
    moon_alt = math.degrees(float(moon_body.alt))
    moon_az  = math.degrees(float(moon_body.az))

    mp = _calc_moon_phase()
    moon_info = {
        "alt_deg":      round(moon_alt, 1),
        "az_deg":       round(moon_az, 1),
        "az_compass":   _az_compass(moon_az),
        "phase_pct":    round(float(moon_body.phase), 1),
        "is_up":        moon_alt > 0,
        "phase_name":   mp["phase_name"],
        "phase_emoji":  mp["phase_emoji"],
        "illumination": mp["illumination"],
        "age_days":     mp["age_days"],
        "fraction":     mp["fraction"],
        "next_full_moon": mp["next_full_moon"],
        "next_new_moon":  mp["next_new_moon"],
    }
    for event, fn in [("rise_ts", "next_rising"), ("set_ts", "next_setting")]:
        obs2 = _make_obs(lat, lon, ts)
        obs2.horizon = "0"
        try:
            moon_info[event] = _to_ts(getattr(obs2, fn)(moon_body))
        except Exception:
            moon_info[event] = None

    # Planets
    planets = []
    for name, cls in PLANETS:
        try:
            planets.append(_planet_info(lat, lon, ts, name, cls))
        except Exception as e:
            log.warning(f"Planet error ({name}): {e}")

    # DSOs
    dso = []
    for row in MESSIER:
        try:
            dso.append(_dso_info(lat, lon, ts, row, moon_body))
        except Exception as e:
            log.warning(f"DSO error (M{row[0]}): {e}")

    bortle  = _bortle(lat, lon)
    meteors = _meteor_showers()

    # Wikipedia thumbnails — batched, one call per 50 items
    try:
        p_titles = [_PLANET_WIKI.get(n, n) for n, _ in PLANETS]
        p_thumbs = _fetch_wiki_thumbs(p_titles)
        for p in planets:
            p["wiki_thumb"] = p_thumbs.get(_PLANET_WIKI.get(p["name"], p["name"]), "")
    except Exception as e:
        log.warning(f"Planet wiki thumbs: {e}")

    try:
        d_titles = [f"Messier {row[0]}" for row in MESSIER]
        d_thumbs = _fetch_wiki_thumbs(d_titles)
        for d in dso:
            d["wiki_thumb"] = d_thumbs.get(f"Messier {d['id']}", "")
    except Exception as e:
        log.warning(f"DSO wiki thumbs: {e}")

    try:
        s_titles = [s[0] for s in METEOR_SHOWERS]
        s_thumbs = _fetch_wiki_thumbs(s_titles)
        for m in meteors:
            m["wiki_thumb"] = s_thumbs.get(m["name"], "")
    except Exception as e:
        log.warning(f"Shower wiki thumbs: {e}")

    up_planets = sum(1 for p in planets if p["is_up"])
    up_dso     = sum(1 for d in dso     if d["is_up"])

    data = {
        "ts":             ts,
        "twilight":       twilight,
        "moon":           moon_info,
        "planets":        planets,
        "dso":            dso,
        "meteor_showers": meteors,
        "bortle":         bortle,
    }

    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO astronomy_data (id, ts, data) VALUES (1, ?, ?)",
              (ts, json.dumps(data)))
    conn.commit()
    log.info(f"Astronomy updated | {up_planets} planets up | {up_dso} DSOs up")
    return data
