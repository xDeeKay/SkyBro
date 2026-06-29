"""One-time photo refresh — run inside the skybro-tracker container.
Clears and re-fetches Planespotters photos for every distinct aircraft in history,
with the same validation logic that prevents wrong-aircraft matches.

Usage:
  sudo docker exec -i skybro-tracker python3 < refresh_photos.py
"""
import sqlite3, requests, time

DB  = '/data/skybro.db'
HDR = {"User-Agent": "SkyBro/1.0 (https://github.com/xDeeKay/SkyBro)"}

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT DISTINCT icao24, registration FROM seen_aircraft WHERE alerted=1"
).fetchall()
print(f"Processing {len(rows)} aircraft...\n")

found = 0
for icao24, reg in rows:
    photo_url = thumb_url = ""

    try:
        r = requests.get(f"https://api.planespotters.net/pub/photos/hex/{icao24}",
                         headers=HDR, timeout=5)
        photos = r.json().get("photos", [])
        if photos:
            returned_hex = (photos[0].get("aircraft", {}).get("modes") or "").lower().strip()
            if not returned_hex or returned_hex == icao24.lower():
                photo_url = photos[0].get("link", "")
                tl = photos[0].get("thumbnail_large") or photos[0].get("thumbnail") or {}
                thumb_url = tl.get("src", "")
    except Exception:
        pass

    if not thumb_url and reg:
        try:
            r = requests.get(f"https://api.planespotters.net/pub/photos/reg/{reg}",
                             headers=HDR, timeout=5)
            photos = r.json().get("photos", [])
            if photos:
                returned_reg = (photos[0].get("aircraft", {}).get("reg") or "").upper().strip()
                if returned_reg == reg.upper().strip():
                    photo_url = photos[0].get("link", "")
                    tl = photos[0].get("thumbnail_large") or photos[0].get("thumbnail") or {}
                    thumb_url = tl.get("src", "")
        except Exception:
            pass

    conn.execute(
        "INSERT OR REPLACE INTO photo_cache (icao24, photo_url, thumb_url, fetched) VALUES (?,?,?,?)",
        (icao24, photo_url, thumb_url, int(time.time())))
    conn.execute(
        "UPDATE seen_aircraft SET photo_url=? WHERE icao24=?",
        (thumb_url, icao24))

    if thumb_url:
        found += 1
    status = f"✓ {thumb_url[:60]}" if thumb_url else "— no photo"
    print(f"  {icao24}  {(reg or '?'):>10}  {status}")
    time.sleep(0.3)

conn.commit()
conn.close()
print(f"\nDone — {found}/{len(rows)} photos found")
