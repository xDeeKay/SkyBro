"""One-time photo refresh — run inside the skybro-tracker container.
Re-fetches Planespotters photos for every distinct aircraft in history,
with validation to prevent wrong-aircraft matches, then falls back to
Wikipedia type photos. Purely numeric registrations (e.g. military regs
like 9106) skip the reg lookup entirely.

Usage:
  sudo docker exec -i skybro-tracker python3 < refresh_photos.py
"""
import sqlite3, requests, time, re

DB  = '/data/skybro.db'
HDR = {"User-Agent": "SkyBro/1.0 (https://github.com/xDeeKay/SkyBro)"}

def _civil_reg(reg):
    """Return True if the registration contains at least one letter (civil regs always do)."""
    return bool(reg and re.search(r'[A-Za-z]', reg))

def _wikipedia_photo(model):
    if not model or model.lower() == "unknown" or len(model) < 5:
        return "", ""
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action":"query","generator":"search","gsrsearch":model,
                    "gsrlimit":1,"prop":"pageimages","pithumbsize":300,"format":"json"},
            headers=HDR, timeout=5)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = page.get("thumbnail", {}).get("source", "")
            if thumb:
                title = page.get("title", "")
                return f"https://en.wikipedia.org/wiki/{title.replace(' ','_')}", thumb
    except Exception:
        pass
    return "", ""

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT DISTINCT icao24, registration, model FROM seen_aircraft WHERE alerted=1"
).fetchall()
print(f"Processing {len(rows)} aircraft...\n")

found = 0
for icao24, reg, model in rows:
    photo_url = thumb_url = ""

    # 1. Planespotters hex lookup
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

    # 2. Planespotters reg lookup (civil regs only)
    if not thumb_url and _civil_reg(reg):
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

    # 3. Wikipedia type photo fallback
    if not thumb_url:
        photo_url, thumb_url = _wikipedia_photo(model)

    conn.execute(
        "INSERT OR REPLACE INTO photo_cache (icao24, photo_url, thumb_url, fetched) VALUES (?,?,?,?)",
        (icao24, photo_url, thumb_url, int(time.time())))
    conn.execute(
        "UPDATE seen_aircraft SET photo_url=? WHERE icao24=?",
        (thumb_url, icao24))

    if thumb_url:
        found += 1
    source = "planespotters" if "planespotters" not in photo_url and thumb_url else \
             "wikipedia" if "wikipedia" in photo_url else \
             "planespotters" if thumb_url else "none"
    status = f"✓ [{source}]" if thumb_url else "— no photo"
    print(f"  {icao24}  {(reg or '?'):>10}  {status}")

    time.sleep(0.3)

conn.commit()
conn.close()
print(f"\nDone — {found}/{len(rows)} photos found")
