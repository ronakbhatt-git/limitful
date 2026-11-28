"""Places: where you were during each conversation and note, as a name you chose.

Location comes from your phone in two ways. Google Maps Timeline keeps its history on the
phone only, and its "Export Timeline data" writes one JSON file: bring it in once for the past.
Going forward, a logger app such as GPSLogger for Android writes GPX or CSV files to a Google
Drive folder on a schedule, and Limitful reads that folder on this computer.

The coordinates stay in this computer's database. The lifelog only ever gets the names you
give places (Home, Office, ...), plus "Commute" while moving and "Elsewhere" anywhere unnamed,
since other programs and AI tools read the lifelog. Files that were read are removed from the
folder two days after they last changed, so coordinates do not pile up in Drive.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config
from .store import Store

COMMUTE = "Commute"
ELSEWHERE = "Elsewhere"
RADIUS_M = 150            # how close counts as being at a named place, unless the place says otherwise
NEAR_MS = 15 * 60_000     # a point this far from a moment, or further, says nothing about it
MOVING_M_S = 1.5          # faster than a slow walk between points: on the move
SCAN_EVERY_S = 10 * 60
TIDY_AFTER_S = 2 * 86_400
SUFFIXES = (".json", ".gpx", ".csv")
MAX_BYTES = 300_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS location_points (
    source     TEXT    NOT NULL,   -- 'timeline' or 'logger'
    t_ms       INTEGER NOT NULL,
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    accuracy_m REAL,
    PRIMARY KEY (source, t_ms)
);
CREATE INDEX IF NOT EXISTS location_points_t ON location_points (t_ms);
CREATE TABLE IF NOT EXISTS location_spans (
    source   TEXT    NOT NULL,
    kind     TEXT    NOT NULL,     -- 'visit' (staying somewhere) or 'moving'
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    lat      REAL,
    lon      REAL,
    hint     TEXT,                 -- Google's guess for a visit (HOME, INFERRED_WORK), or how one moved
    PRIMARY KEY (source, kind, start_ms)
);
CREATE INDEX IF NOT EXISTS location_spans_t ON location_spans (start_ms);
CREATE TABLE IF NOT EXISTS location_files (
    name     TEXT PRIMARY KEY,     -- path in the folder
    size     INTEGER,
    mtime_ms INTEGER,
    read_ms  INTEGER,
    points   INTEGER,
    error    TEXT
);
"""


class PlacesError(Exception):
    pass


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    radius_m: float = RADIUS_M


# --- settings -------------------------------------------------------------------------

def places() -> list[Place]:
    out = []
    for p in config.load().get("places") or []:
        try:
            out.append(Place(str(p["name"]), float(p["lat"]), float(p["lon"]), float(p.get("radius_m") or RADIUS_M)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def folder() -> Path | None:
    saved = config.load().get("location_folder")
    return Path(saved) if saved else None


_COORDS = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)")


def coords(text: str) -> tuple[float, float]:
    """Latitude and longitude from what Google Maps lets you copy: "35.22, -80.84", a link with
    @35.22,-80.84 or ?q=35.22,-80.84 in it, or Timeline's "35.22°, -80.84°"."""
    text = text.strip()
    found = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", text) or re.search(r"[?&](?:q|query|ll)=(-?\d+\.\d+),\s*(-?\d+\.\d+)", text) \
        or _COORDS.search(text.removeprefix("geo:"))
    if not found:
        raise PlacesError("paste the place's coordinates, such as 35.2271, -80.8431. In Google Maps, "
                          "press and hold the spot and copy the numbers that appear")
    lat, lon = float(found[1]), float(found[2])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise PlacesError("those numbers are not a place on Earth")
    return lat, lon


def add(name: str, lat: float, lon: float, radius_m: float | None = None) -> None:
    name = " ".join(str(name).split())[:60]
    if not name:
        raise PlacesError("give the place a name, such as Home or Office")
    if name.lower() in (COMMUTE.lower(), ELSEWHERE.lower()):
        raise PlacesError(f"{name} is used for times between places; pick another name")
    radius = float(radius_m or RADIUS_M)
    if not 25 <= radius <= 5000:
        raise PlacesError("the size should be between 25 and 5000 metres")
    with config._updating:
        settings = config.load()
        kept = [p for p in settings.get("places") or [] if p.get("name") != name]
        settings["places"] = [*kept, {"name": name, "lat": round(lat, 6), "lon": round(lon, 6), "radius_m": radius}]
        config.save(settings)


def remove(name: str) -> bool:
    with config._updating:
        settings = config.load()
        before = settings.get("places") or []
        settings["places"] = [p for p in before if p.get("name") != name]
        config.save(settings)
    return len(settings["places"]) < len(before)


def set_folder(path: str | None) -> None:
    path = (path or "").strip().strip('"')
    if path and not Path(path).is_dir():
        raise PlacesError(f"there is no folder at {path}. On Windows, Google Drive's folder is usually "
                          r"G:\My Drive\<folder name>")
    with config._updating:
        settings = config.load()
        if path:
            settings["location_folder"] = path
        else:
            settings.pop("location_folder", None)
        config.save(settings)


# --- reading files --------------------------------------------------------------------

def _ms(text: str) -> int:
    if not isinstance(text, str):
        raise ValueError("no time")
    when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.astimezone()
    return int(when.timestamp() * 1000)


def _point(text) -> tuple[float, float] | None:
    if isinstance(text, dict):  # {"latLng": "..."} in some files
        text = text.get("latLng") or text.get("LatLng") or ""
    found = _COORDS.search(str(text).removeprefix("geo:"))
    return (float(found[1]), float(found[2])) if found else None


def parse_timeline(data: bytes) -> tuple[list[tuple], list[tuple]]:
    """Points (t_ms, lat, lon, accuracy) and spans (kind, start, end, lat, lon, hint) from a
    Google Timeline export: the phone's own (Android or iPhone), or an old Takeout Records.json."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise PlacesError("that file is not a Timeline export (it is not JSON)") from None
    points: list[tuple] = []
    spans: list[tuple] = []
    if isinstance(doc, dict) and isinstance(doc.get("locations"), list):  # Takeout Records.json
        for loc in doc["locations"]:
            try:
                when = _ms(loc["timestamp"]) if "timestamp" in loc else int(loc["timestampMs"])
                points.append((when, loc["latitudeE7"] / 1e7, loc["longitudeE7"] / 1e7, loc.get("accuracy")))
            except (KeyError, TypeError, ValueError):
                continue
        return points, spans
    segments = doc.get("semanticSegments") if isinstance(doc, dict) else doc
    if not isinstance(segments, list):
        raise PlacesError("that file is not a Timeline export")
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            start, end = _ms(seg["startTime"]), _ms(seg["endTime"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(seg.get("visit"), dict):
            top = seg["visit"].get("topCandidate") or {}
            where = _point(top.get("placeLocation"))
            if where:
                spans.append(("visit", start, end, *where, top.get("semanticType")))
        elif isinstance(seg.get("activity"), dict):
            act = seg["activity"]
            hint = (act.get("topCandidate") or {}).get("type")
            spans.append(("moving", start, end, None, None, hint))
            for key, at in (("start", start), ("end", end)):
                where = _point(act.get(key))
                if where:
                    points.append((at, *where, None))
        elif isinstance(seg.get("timelinePath"), list):
            for step in seg["timelinePath"]:
                where = _point(step.get("point"))
                if not where:
                    continue
                try:
                    at = _ms(step["time"]) if "time" in step else start + int(float(step["durationMinutesOffsetFromStartTime"]) * 60_000)
                except (KeyError, TypeError, ValueError):
                    continue
                points.append((at, *where, None))
    for raw in (doc.get("rawSignals") or []) if isinstance(doc, dict) else []:
        pos = raw.get("position") if isinstance(raw, dict) else None
        if isinstance(pos, dict):
            where = _point(pos.get("LatLng") or pos.get("latLng"))
            try:
                if where:
                    points.append((_ms(pos["timestamp"]), *where, pos.get("accuracyMeters")))
            except (KeyError, TypeError, ValueError):
                continue
    if not points and not spans:
        raise PlacesError("that file has no places in it. Is Timeline turned on?")
    return points, spans


def parse_gpx(data: bytes) -> list[tuple]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise PlacesError("that GPX file could not be read") from None
    points = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in ("trkpt", "wpt", "rtept"):
            continue
        when = next((child.text for child in node if child.tag.rsplit("}", 1)[-1] == "time"), None)
        accuracy = next((child.text for child in node if child.tag.rsplit("}", 1)[-1] in ("hdop", "accuracy")), None)
        try:
            points.append((_ms(when), float(node.get("lat")), float(node.get("lon")),
                           float(accuracy) if accuracy else None))
        except (TypeError, ValueError):
            continue
    return points


def parse_csv(data: bytes) -> list[tuple]:
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    pick = lambda row, *names: next((row[k] for k in row if k and k.strip().lower() in names and row[k]), None)
    points = []
    for row in rows:
        try:
            when = pick(row, "time", "timestamp", "date_time", "datetime")
            lat, lon = pick(row, "lat", "latitude"), pick(row, "lon", "lng", "longitude")
            accuracy = pick(row, "accuracy", "accuracy(m)")
            points.append((_ms(when), float(lat), float(lon), float(accuracy) if accuracy else None))
        except (TypeError, ValueError):
            continue
    return points


def parse(name: str, data: bytes) -> tuple[str, list[tuple], list[tuple]]:
    """(source, points, spans) for a file, by its kind."""
    suffix = Path(name).suffix.lower()
    if suffix == ".json":
        return ("timeline", *parse_timeline(data))
    if suffix == ".gpx":
        return "logger", parse_gpx(data), []
    if suffix == ".csv":
        return "logger", parse_csv(data), []
    raise PlacesError("choose a Timeline export (.json), or a .gpx or .csv file from a logger app")


def _ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def save(store: Store, source: str, points: list[tuple], spans: list[tuple]) -> int:
    """Keep what a file holds. Reading the same file again adds nothing new."""
    _ensure(store)
    with store.db:
        before = store.db.total_changes
        store.db.executemany("INSERT OR IGNORE INTO location_points VALUES (?, ?, ?, ?, ?)",
                             [(source, *p) for p in points])
        store.db.executemany("INSERT OR REPLACE INTO location_spans VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [(source, *s) for s in spans])
        return store.db.total_changes - before


def import_bytes(store: Store, name: str, data: bytes) -> dict:
    source, points, spans = parse(name, data)
    if not points and not spans:
        raise PlacesError("that file has no locations in it")
    save(store, source, points, spans)
    times = [p[0] for p in points] + [s[1] for s in spans]
    return {"points": len(points), "stays": sum(1 for s in spans if s[0] == "visit"),
            "first_ms": min(times), "last_ms": max(times)}


def scan(store: Store, now: float | None = None) -> dict[str, int | str]:
    """Read new or changed files in the location folder, then tidy old ones away.
    Only files that held locations are ever removed."""
    where = folder()
    if where is None or not where.is_dir():
        return {}
    _ensure(store)
    now = time.time() if now is None else now
    out: dict[str, int | str] = {}
    files = [f for f in [*where.iterdir(), *(g for d in where.iterdir() if d.is_dir() for g in d.iterdir())]
             if f.is_file() and f.suffix.lower() in SUFFIXES]
    for file in files:
        name = str(file.relative_to(where))
        stat = file.stat()
        size, mtime = stat.st_size, int(stat.st_mtime * 1000)
        seen = store.db.execute("SELECT size, mtime_ms, points, error FROM location_files WHERE name = ?", (name,)).fetchone()
        if not seen or (seen[0], seen[1]) != (size, mtime):
            try:
                if size > MAX_BYTES:
                    raise PlacesError("the file is too large")
                result = import_bytes(store, file.name, file.read_bytes())
                count, error = result["points"] + result["stays"], None
            except (PlacesError, OSError) as err:
                count, error = 0, str(err)
            store.db.execute("INSERT OR REPLACE INTO location_files VALUES (?, ?, ?, ?, ?, ?)",
                             (name, size, mtime, int(now * 1000), count, error))
            store.db.commit()
            out[name] = error or count
            seen = (size, mtime, count, error)
        if seen[2] and not seen[3] and now - stat.st_mtime > TIDY_AFTER_S:
            try:
                file.unlink()
                store.db.execute("DELETE FROM location_files WHERE name = ?", (name,))
                store.db.commit()
            except OSError:
                pass  # tried again next time
    return out


_last_scan: list[float] = []


def due() -> bool:
    return folder() is not None and (not _last_scan or time.monotonic() - _last_scan[-1] >= SCAN_EVERY_S)


def scanned() -> None:
    _last_scan[:] = [time.monotonic()]


def forget(store: Store) -> None:
    _ensure(store)
    with store.db:
        for table in ("location_points", "location_spans", "location_files"):
            store.db.execute(f"DELETE FROM {table}")


# --- where you were ---------------------------------------------------------------------

def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(min(1.0, a)))


def _named(lat: float, lon: float, named: list[Place], slack: float = 0) -> str | None:
    best = min(((distance_m(lat, lon, p.lat, p.lon), p) for p in named), default=None, key=lambda x: x[0])
    return best[1].name if best and best[0] <= best[1].radius_m + slack else None


class Where:
    """Where you were over one stretch of time, loaded once so a whole day is quick."""

    def __init__(self, store: Store, start_ms: int, end_ms: int, named: list[Place] | None = None):
        _ensure(store)
        self.named = places() if named is None else named
        lo, hi = start_ms - NEAR_MS, end_ms + NEAR_MS
        self.spans = store.db.execute(
            "SELECT kind, start_ms, end_ms, lat, lon FROM location_spans WHERE start_ms <= ? AND end_ms >= ? "
            "ORDER BY kind DESC, start_ms", (hi, lo)).fetchall()  # visits before moving
        self.points = store.db.execute(
            "SELECT t_ms, lat, lon FROM location_points WHERE t_ms BETWEEN ? AND ? ORDER BY t_ms", (lo, hi)).fetchall()

    def __bool__(self) -> bool:
        return bool(self.spans or self.points)

    def at(self, ms: int) -> str | None:
        for kind, start, end, lat, lon in self.spans:
            if start <= ms <= end:
                if kind == "visit":
                    return _named(lat, lon, self.named, slack=50) or ELSEWHERE
                return COMMUTE
        near = [p for p in self.points if abs(p[0] - ms) < NEAR_MS]
        if not near:
            return None
        t, lat, lon = min(near, key=lambda p: abs(p[0] - ms))
        place = _named(lat, lon, self.named)
        if place:
            return place
        close = [p for p in near if abs(p[0] - ms) <= 5 * 60_000]
        if len(close) >= 2:
            seconds = max(1.0, (close[-1][0] - close[0][0]) / 1000)
            if distance_m(close[0][1], close[0][2], close[-1][1], close[-1][2]) / seconds > MOVING_M_S:
                return COMMUTE
        return ELSEWHERE

    def during(self, start_ms: int, end_ms: int) -> str | None:
        """Where a conversation happened: one place, or the places in order ("Home → Commute")."""
        moments = [start_ms, *range(start_ms + 5 * 60_000, end_ms, 5 * 60_000), end_ms]
        seen: list[str] = []
        for ms in moments:
            place = self.at(ms)
            if place and (not seen or seen[-1] != place):
                seen.append(place)
        if len(seen) > 4:
            seen = [seen[0], "…", seen[-1]]
        return " → ".join(seen) or None


# --- what the Settings page shows -------------------------------------------------------

def suggestions(store: Store, named: list[Place] | None = None, limit: int = 6) -> list[dict]:
    """Spots where you spend the most time and that have no name yet, to name with one click."""
    _ensure(store)
    named = places() if named is None else named
    cells: dict[tuple[int, int], dict] = {}

    def count(lat: float, lon: float, ms: int, last: int, hint: str | None) -> None:
        key = (round(lat / 0.002), round(lon / 0.002))  # about 200 m
        cell = cells.setdefault(key, {"lat": 0.0, "lon": 0.0, "ms": 0, "last_ms": 0, "hints": {}})
        cell["lat"] += lat * ms
        cell["lon"] += lon * ms
        cell["ms"] += ms
        cell["last_ms"] = max(cell["last_ms"], last)
        if hint:
            cell["hints"][hint] = cell["hints"].get(hint, 0) + ms

    visits = store.db.execute("SELECT start_ms, end_ms, lat, lon, hint FROM location_spans WHERE kind = 'visit'").fetchall()
    for start, end, lat, lon, hint in visits:
        count(lat, lon, max(1, end - start), end, hint)
    if not visits:  # only a logger's points: time spent is the gap to the next point while staying put
        prev = None
        for t, lat, lon in store.db.execute("SELECT t_ms, lat, lon FROM location_points ORDER BY t_ms"):
            if prev and t - prev[0] < NEAR_MS and distance_m(prev[1], prev[2], lat, lon) < 100:
                count(prev[1], prev[2], t - prev[0], t, None)
            prev = (t, lat, lon)
    out = []
    for cell in sorted(cells.values(), key=lambda c: -c["ms"]):
        lat, lon = cell["lat"] / cell["ms"], cell["lon"] / cell["ms"]
        if cell["ms"] < 3_600_000 or _named(lat, lon, named, slack=100):
            continue
        hint = max(cell["hints"], key=cell["hints"].get) if cell["hints"] else None
        out.append({"lat": round(lat, 5), "lon": round(lon, 5), "hours": round(cell["ms"] / 3_600_000, 1),
                    "last_ms": cell["last_ms"],
                    "guess": {"HOME": "Home", "INFERRED_HOME": "Home", "WORK": "Work", "INFERRED_WORK": "Work"}.get(hint or "")})
        if len(out) >= limit:
            break
    return out


def overview(store: Store) -> dict:
    _ensure(store)
    named = places()
    points, first, last = store.db.execute("SELECT COUNT(*), MIN(t_ms), MAX(t_ms) FROM location_points").fetchone()
    stays, sfirst, slast = store.db.execute(
        "SELECT COUNT(*) FILTER (WHERE kind = 'visit'), MIN(start_ms), MAX(end_ms) FROM location_spans").fetchone()
    firsts = [x for x in (first, sfirst) if x]
    lasts = [x for x in (last, slast) if x]
    files = [{"name": n, "read_ms": r, "points": p, "error": e} for n, r, p, e in store.db.execute(
        "SELECT name, read_ms, points, error FROM location_files ORDER BY read_ms DESC LIMIT 5")]
    logger_last = store.db.execute("SELECT MAX(t_ms) FROM location_points WHERE source = 'logger'").fetchone()[0]
    return {"places": [{"name": p.name, "lat": p.lat, "lon": p.lon, "radius_m": p.radius_m} for p in named],
            "folder": str(folder()) if folder() else None,
            "folder_missing": bool(folder() and not folder().is_dir()),
            "points": points, "stays": stays,
            "first_ms": min(firsts) if firsts else None, "last_ms": max(lasts) if lasts else None,
            "logger_last_ms": logger_last, "files": files,
            "suggestions": suggestions(store, named)}
