"""Places: phone location read from Timeline exports and logger files, shown in the lifelog as names only."""

import json
import os
import time
from datetime import datetime

import pytest

from limitful import brain, places
from limitful.store import Store

DAY = "2026-11-20"
LATER = int(datetime(2026, 11, 23, 9, 0).timestamp() * 1000)
HOME = (40.0, -75.0)
OFFICE = (40.05, -75.05)   # about 7 km away


def at(h, m=0):
    return int(datetime(2026, 11, 20, h, m).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="milliseconds")


def deg(lat, lon):
    return f"{lat}°, {lon}°"


TIMELINE = {
    "semanticSegments": [
        {"startTime": iso(at(6)), "endTime": iso(at(8)),
         "visit": {"topCandidate": {"placeId": "a", "semanticType": "HOME", "placeLocation": {"latLng": deg(*HOME)}}}},
        {"startTime": iso(at(8)), "endTime": iso(at(8, 30)),
         "activity": {"start": {"latLng": deg(*HOME)}, "end": {"latLng": deg(*OFFICE)},
                      "topCandidate": {"type": "IN_PASSENGER_VEHICLE"}}},
        {"startTime": iso(at(8, 30)), "endTime": iso(at(17)),
         "visit": {"topCandidate": {"placeId": "b", "semanticType": "INFERRED_WORK", "placeLocation": {"latLng": deg(*OFFICE)}}}},
        {"startTime": iso(at(18)), "endTime": iso(at(19)),
         "visit": {"topCandidate": {"placeId": "c", "semanticType": "UNKNOWN", "placeLocation": {"latLng": "41.0°, -76.0°"}}}},
        {"startTime": iso(at(20)), "endTime": iso(at(21)),
         "timelinePath": [{"point": deg(*HOME), "time": iso(at(20, 10))}]},
    ],
    "rawSignals": [{"position": {"LatLng": deg(*HOME), "accuracyMeters": 10, "timestamp": iso(at(22))}},
                   {"wifiScan": {"deliveryTime": iso(at(22))}}],
    "userLocationProfile": {},
}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


def _talk(store, cid, start, end, title):
    seg = f"audio/{DAY}/rec_{cid}.ogg"
    store.db.execute("INSERT INTO segments (path, start_ms, duration_ms, first_page, last_page) "
                     "VALUES (?, ?, 60000, -1, -1)", (seg, start))
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES (?, ?, ?, ?)",
                     (seg, start + 1000, end, "talking"))
    store.db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title, summary, tier, tags, sensitive) "
                     "VALUES (?, ?, ?, 'done', ?, '[]', 'text', '[]', 0)", (cid, start, end, title))
    store.db.commit()


def test_coordinates_are_read_from_what_google_maps_lets_you_copy():
    assert places.coords("35.2271, -80.8431") == (35.2271, -80.8431)
    assert places.coords("35.2271°, -80.8431°") == (35.2271, -80.8431)
    assert places.coords("https://www.google.com/maps/place/X/@35.2271,-80.8431,17z") == (35.2271, -80.8431)
    assert places.coords("https://maps.google.com/?q=35.2271,-80.8431") == (35.2271, -80.8431)
    for bad in ("my house", "95.0, 10.0"):
        with pytest.raises(places.PlacesError):
            places.coords(bad)


def test_a_timeline_export_names_each_moment(store):
    result = places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    assert result["stays"] == 3 and result["points"] == 4  # two ends of the drive, one path point, one raw
    places.add("Home", *HOME)
    places.add("Office", *OFFICE, radius_m=200)
    where = places.Where(store, at(0), at(23, 59))
    assert where.at(at(7)) == "Home"
    assert where.at(at(8, 15)) == places.COMMUTE
    assert where.at(at(12)) == "Office"
    assert where.at(at(18, 30)) == places.ELSEWHERE
    assert where.at(at(20, 10)) == "Home"           # a path point
    assert where.at(at(3)) is None                  # nothing known then
    assert where.during(at(7, 50), at(8, 40)) == "Home → Commute → Office"
    # Reading it again adds nothing.
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    assert store.db.execute("SELECT COUNT(*) FROM location_points").fetchone()[0] == 4


def test_unnamed_spots_are_offered_with_googles_guess(store):
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    offered = places.suggestions(store, named=[])
    assert [(o["guess"], o["hours"]) for o in offered] == [("Work", 8.5), ("Home", 2.0), (None, 1.0)]
    places.add("Office", *OFFICE)
    assert [o["guess"] for o in places.suggestions(store)] == ["Home", None]


def test_logger_files_in_the_folder_are_read_and_tidied_away(store, tmp_path):
    folder = tmp_path / "drive" / "Lifelog Location"
    (folder / "GPSLogger").mkdir(parents=True)
    gpx = ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
           + "".join(f'<trkpt lat="{OFFICE[0]}" lon="{OFFICE[1]}"><time>{iso(at(10, m))}</time></trkpt>' for m in (0, 5, 10))
           + "</trkseg></trk></gpx>")
    (folder / "20261120.gpx").write_text(gpx, encoding="utf-8")
    moving = "time,lat,lon,accuracy\n" + "".join(
        f"{iso(at(13, m))},{40.0 + m * 0.001},-75.0,8\n" for m in (0, 2, 4))  # ~110 m a minute
    (folder / "GPSLogger" / "20261120.csv").write_text(moving, encoding="utf-8")
    (folder / "shopping.csv").write_text("item,qty\nmilk,1\n", encoding="utf-8")
    places.set_folder(str(folder))
    places.add("Office", *OFFICE)

    read = places.scan(store)
    assert read["20261120.gpx"] == 3 and read[os.path.join("GPSLogger", "20261120.csv")] == 3
    assert read["shopping.csv"] == "that file has no locations in it"
    assert places.scan(store) == {}  # nothing changed, nothing read again
    where = places.Where(store, at(0), at(23))
    assert where.at(at(10, 5)) == "Office"
    assert where.at(at(13, 2)) == places.COMMUTE

    old = time.time() + places.TIDY_AFTER_S + 60
    places.scan(store, now=old)
    assert not (folder / "20261120.gpx").exists() and not (folder / "GPSLogger" / "20261120.csv").exists()
    assert (folder / "shopping.csv").exists()  # never held locations, so never removed
    assert store.db.execute("SELECT COUNT(*) FROM location_points").fetchone()[0] == 6  # kept here


def test_day_pages_get_place_names_and_never_coordinates(store, tmp_path):
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    places.add("Home", *HOME)
    places.add("Office", *OFFICE)
    _talk(store, 1, at(7), at(7, 10), "Breakfast")
    _talk(store, 2, at(12), at(12, 30), "Budget")
    _talk(store, 3, at(4), at(4, 5), "Night")
    public, _ = brain.new_key()
    settings = brain.Settings(repo=tmp_path / "repo", audio=tmp_path / "audio", recipient=public)
    (settings.repo / "notes/daily").mkdir(parents=True)
    (settings.repo / "notes/daily" / f"{DAY}.md").write_text("### 08:15 Traffic\nslow today\n", encoding="utf-8")

    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    assert 'places: ["Home", "Office"]' in md
    assert "_conversation 1 · text · 📍 Home_" in md
    assert "_conversation 2 · text · 📍 Office_" in md
    assert "_conversation 3 · text_" in md  # nothing known at 4 am
    assert f"> [!note] My note · 📍 Commute · [edit](../../../notes/daily/{DAY}.md)" in md
    jsonl = (settings.repo / "data/2026/2026-11" / f"{DAY}.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["place"] for line in jsonl.splitlines()] == [None, "Home", "Office"]
    for text in (md, jsonl):
        assert "40.0" not in text and "-75" not in text


def test_without_location_data_day_pages_are_unchanged(store, tmp_path):
    _talk(store, 1, at(7), at(7, 10), "Breakfast")
    public, _ = brain.new_key()
    settings = brain.Settings(repo=tmp_path / "repo", audio=tmp_path / "audio", recipient=public)
    brain.export(store, settings, now_ms=LATER, git=False)
    md = (settings.repo / "days/2026/2026-11" / f"{DAY}.md").read_text(encoding="utf-8")
    assert "places:" not in md and "📍" not in md


def test_forgetting_deletes_every_point_but_keeps_the_names(store):
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    places.add("Home", *HOME)
    places.forget(store)
    assert places.overview(store)["points"] == 0 and places.overview(store)["stays"] == 0
    assert [p.name for p in places.places()] == ["Home"]


def test_names_used_for_the_gaps_are_refused():
    with pytest.raises(places.PlacesError):
        places.add("commute", *HOME)
    with pytest.raises(places.PlacesError):
        places.set_folder("/no/such/folder")


# --- names for spots the owner has not named --------------------------------------------

SHOP = (41.0, -76.0)  # the UNKNOWN visit in TIMELINE, 18:00-19:00


def _answers(seen):
    def lookup(lat, lon):
        seen.append((lat, lon))
        if abs(lat - SHOP[0]) < 0.01:
            return {"name": "Corner Market", "type": "supermarket",
                    "address": {"house_number": "12", "road": "Elm Street", "town": "Springfield"}}
        return {"category": "building", "type": "yes", "address": {"house_number": "9", "road": "Oak Lane", "city": "Shelbyville"}}
    return lookup


def test_unnamed_spots_are_looked_up_and_named_by_triage(store, tmp_path):
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    places.add("Home", *HOME)
    seen = []
    result = places.refresh(store, lookup=_answers(seen), pause=lambda s: None)
    assert result == {"spots": 2, "looked_up": 2, "failed": 0}  # Office and the shop; never Home
    assert all(places.distance_m(lat, lon, *HOME) > 1000 for lat, lon in seen)
    assert places.refresh(store, lookup=_answers(seen), pause=lambda s: None)["looked_up"] == 0  # once each

    where = places.Where(store, at(0), at(23, 59))
    assert where.at(at(18, 30)) == "Corner Market"
    assert where.at(at(12)) == "Oak Lane, Shelbyville"  # no house number, ever

    _talk(store, 9, at(18, 10), at(18, 40), "Buying groceries for the party")
    waiting = places.pending_names(store)
    shop = next(w for w in waiting if w["osm_name"] == "Corner Market")
    assert shop["conversations"] == ["Buying groceries for the party"] and shop["osm_area"] == "Elm Street, Springfield"
    saved = places.save_names(store, [{"id": shop["id"], "name": " The   market "}, {"id": "nope", "name": "x"},
                                      {"id": shop["id"], "name": "Elsewhere"}])
    assert saved == {"saved": 1, "refused": ["nope", shop["id"]], "still_waiting": 1}
    assert places.Where(store, at(0), at(23, 59)).at(at(18, 30)) == "The market"
    # A name the owner gives wins over every guess.
    places.add("Grocer", *SHOP)
    assert places.Where(store, at(0), at(23, 59)).at(at(18, 30)) == "Grocer"
    assert all(f["name"] != "The market" for f in places.overview(store)["found"])

    places.forget(store)
    assert store.db.execute("SELECT COUNT(*) FROM place_guesses").fetchone()[0] == 0


def test_lookup_can_be_turned_off_and_failures_wait(store):
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    places.set_lookup(False)
    calls = []
    assert places.refresh(store, lookup=_answers(calls))["looked_up"] == 0 and not calls
    assert len(places.pending_names(store)) == 3  # triage still sees them, with no OpenStreetMap help
    places.set_lookup(True)

    def offline(lat, lon):
        raise OSError("no network")
    assert places.refresh(store, lookup=offline, pause=lambda s: None)["failed"] == 1
    assert places.pending_names(store) == []  # not offered until looked up


def test_two_people_in_one_timeline_are_noticed(store):
    places.import_bytes(store, "Timeline.json", json.dumps(TIMELINE).encode())
    assert places.clashes(store) == []
    other = {"semanticSegments": [{"startTime": iso(at(12)), "endTime": iso(at(13)),
                                   "visit": {"topCandidate": {"placeLocation": {"latLng": deg(*SHOP)}}}}]}
    places.import_bytes(store, "Other.json", json.dumps(other).encode())
    assert [c["start_ms"] for c in places.overview(store)["clashes"]] == [at(12)]
