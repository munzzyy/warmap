"""The export writers: CSV, GeoJSON, GPX, KML and Wigle round-trip.

The recurring question in all of these is what happens to a record with no
location. CSV is a table and can carry it; the geographic formats cannot
represent a point that has no point, so they drop it, and every writer
returns how many it actually wrote so the caller can say so out loud.
"""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET

from warmap import export, parse
from warmap.gps import TrackPoint
from warmap.models import GEO_NONE, GEO_TRACK, Sighting


def _record(**kw) -> Sighting:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="HomeNet", auth_mode="[WPA2-PSK-CCMP][ESS]",
        enc_bucket="WPA2", first_seen="2026-06-14 09:00:00", channel=6, rssi=-55,
        lat=33.0, lon=-112.0, altitude=340.0, accuracy=5.0, type="WIFI",
        times_seen=3, frequency=2437.0, vendor="Espressif Inc.",
        source="/tmp/a.csv", meta={"tracker": "Tile"},
    )
    defaults.update(kw)
    return Sighting(**defaults)


def _unlocated(**kw) -> Sighting:
    return _record(
        bssid="Princeton:123456", ssid="Garage", type="SUBGHZ", lat=None, lon=None,
        geo_source=GEO_NONE, enc_bucket="Unknown", channel=None, frequency=433.92,
        **kw
    )


# --- CSV -----------------------------------------------------------------

def test_csv_includes_the_new_columns(tmp_path):
    path = tmp_path / "out.csv"
    export.write_csv([_record()], path)
    row = list(csv.DictReader(path.open()))[0]
    assert row["vendor"] == "Espressif Inc."
    assert row["frequency"] == "2437.0"
    assert row["geo_source"] == "direct"
    assert row["type"] == "WIFI"


def test_csv_meta_is_json_so_it_round_trips(tmp_path):
    path = tmp_path / "out.csv"
    export.write_csv([_record()], path)
    row = list(csv.DictReader(path.open()))[0]
    assert json.loads(row["meta"])["tracker"] == "Tile"


def test_csv_keeps_records_with_no_location(tmp_path):
    """The only export that can, and the reason the records table exists."""
    path = tmp_path / "out.csv"
    written = export.write_csv([_record(), _unlocated()], path)
    assert written == 2
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 2
    assert rows[1]["lat"] == ""


def test_csv_empty_input_still_writes_a_header(tmp_path):
    path = tmp_path / "out.csv"
    assert export.write_csv([], path) == 0
    assert "bssid" in path.read_text()


# --- GeoJSON -------------------------------------------------------------

def test_geojson_shape_and_coordinate_order():
    payload = export.to_geojson([_record()])
    feature = payload["features"][0]
    assert payload["type"] == "FeatureCollection"
    assert feature["geometry"]["coordinates"] == [-112.0, 33.0]  # lon, lat
    assert feature["properties"]["ssid"] == "HomeNet"
    assert "lat" not in feature["properties"]


def test_geojson_carries_meta_for_the_popup():
    feature = export.to_geojson([_record()])["features"][0]
    assert feature["properties"]["meta"]["tracker"] == "Tile"


def test_geojson_drops_unlocated_records():
    payload = export.to_geojson([_record(), _unlocated()])
    assert len(payload["features"]) == 1


def test_write_geojson_reports_what_it_wrote(tmp_path):
    path = tmp_path / "out.geojson"
    assert export.write_geojson([_record(), _unlocated()], path) == 1
    assert json.loads(path.read_text())["type"] == "FeatureCollection"


# --- GPX -----------------------------------------------------------------

def test_gpx_is_valid_xml_with_waypoints(tmp_path):
    path = tmp_path / "out.gpx"
    export.write_gpx([_record()], path)
    root = ET.fromstring(path.read_text())
    waypoints = [e for e in root.iter() if e.tag.endswith("wpt")]
    assert len(waypoints) == 1
    assert waypoints[0].attrib["lat"] == "33.0000000"


def test_gpx_includes_the_track_when_given_one(tmp_path):
    from datetime import datetime
    points = [
        TrackPoint(lat=33.0, lon=-112.0, when=datetime(2026, 6, 14, 9, 0, 0)),
        TrackPoint(lat=33.01, lon=-112.01, when=datetime(2026, 6, 14, 9, 1, 0)),
    ]
    path = tmp_path / "out.gpx"
    export.write_gpx([_record()], path, track_points=points)
    root = ET.fromstring(path.read_text())
    trackpoints = [e for e in root.iter() if e.tag.endswith("trkpt")]
    assert len(trackpoints) == 2


def test_gpx_escapes_special_characters(tmp_path):
    """An SSID is arbitrary user-controlled text and will eventually contain
    an ampersand or an angle bracket."""
    path = tmp_path / "out.gpx"
    export.write_gpx([_record(ssid="Bob & <Alice>")], path)
    root = ET.fromstring(path.read_text())  # would raise if unescaped
    names = [e.text for e in root.iter() if e.tag.endswith("name")]
    assert "Bob & <Alice>" in names


def test_gpx_skips_unlocated(tmp_path):
    path = tmp_path / "out.gpx"
    assert export.write_gpx([_record(), _unlocated()], path) == 1


# --- KML -----------------------------------------------------------------

def test_kml_is_valid_xml_grouped_by_type(tmp_path):
    path = tmp_path / "out.kml"
    export.write_kml([_record(), _record(bssid="X", type="BLE", ssid="Tag")], path)
    root = ET.fromstring(path.read_text())
    folders = [e for e in root.iter() if e.tag.endswith("Folder")]
    assert len(folders) == 2


def test_kml_coordinate_order_is_lon_lat_alt(tmp_path):
    path = tmp_path / "out.kml"
    export.write_kml([_record()], path)
    root = ET.fromstring(path.read_text())
    coords = [e.text for e in root.iter() if e.tag.endswith("coordinates")][0]
    assert coords.startswith("-112.0000000,33.0000000,")


def test_kml_escapes_special_characters(tmp_path):
    path = tmp_path / "out.kml"
    export.write_kml([_record(ssid="A & B")], path)
    ET.fromstring(path.read_text())


def test_kml_skips_unlocated(tmp_path):
    path = tmp_path / "out.kml"
    assert export.write_kml([_record(), _unlocated()], path) == 1


# --- Wigle round trip ----------------------------------------------------

def test_wigle_export_reimports_cleanly(tmp_path):
    """The strongest check on both the writer and the reader: write a record
    out in Wigle format, read it back, and expect the same thing."""
    path = tmp_path / "wigle.csv"
    original = _record()
    export.write_wigle_csv([original], path)

    reloaded = parse.parse_file(path)
    assert len(reloaded) == 1
    back = reloaded[0]
    assert back.bssid == original.bssid
    assert back.ssid == original.ssid
    assert back.enc_bucket == original.enc_bucket
    assert back.channel == original.channel
    assert back.rssi == original.rssi
    assert back.lat == original.lat
    assert back.lon == original.lon
    assert back.type == original.type


def test_wigle_export_writes_the_version_line(tmp_path):
    path = tmp_path / "wigle.csv"
    export.write_wigle_csv([_record()], path)
    assert path.read_text().startswith("WigleWifi-1.6,")


def test_wigle_export_preserves_ble_type(tmp_path):
    path = tmp_path / "wigle.csv"
    export.write_wigle_csv([_record(type="BLE", enc_bucket="Unknown", auth_mode="")], path)
    assert parse.parse_file(path)[0].type == "BLE"


def test_wigle_export_skips_unlocated(tmp_path):
    path = tmp_path / "wigle.csv"
    assert export.write_wigle_csv([_record(), _unlocated()], path) == 1


def test_wigle_export_handles_a_comma_in_the_ssid(tmp_path):
    path = tmp_path / "wigle.csv"
    export.write_wigle_csv([_record(ssid="Bob, Alice and Co")], path)
    assert parse.parse_file(path)[0].ssid == "Bob, Alice and Co"


# --- geo_source survives a round trip through the store ------------------

def test_track_placed_records_keep_their_provenance():
    """A position inferred from a track must never come back looking like a
    recorded GPS fix."""
    record = _record(geo_source=GEO_TRACK)
    restored = Sighting.from_dict(record.to_dict())
    assert restored.geo_source == GEO_TRACK


# --- type fidelity across a round trip -----------------------------------

def test_csv_round_trip_preserves_non_wifi_types(tmp_path):
    """warmap's own CSV export carries its full type set. Reading one back
    must not quietly turn a Sub-GHz key into a Wi-Fi access point."""
    path = tmp_path / "out.csv"
    original = _record(bssid="Princeton:123456", type="SUBGHZ", frequency=433.92,
                       channel=None, enc_bucket="Unknown")
    export.write_csv([original], path)
    back = parse.parse_file(path)
    assert len(back) == 1
    assert back[0].type == "SUBGHZ"


def test_wigle_export_drops_types_wigle_cannot_represent(tmp_path):
    """A Sub-GHz key written into a Wigle CSV is read back by every other
    wardriving tool as a Wi-Fi access point. Dropping it is the honest
    behaviour; the CSV and GeoJSON exports keep everything."""
    path = tmp_path / "wigle.csv"
    records = [
        _record(bssid="AA:BB:CC:DD:EE:01", type="WIFI"),
        _record(bssid="AA:BB:CC:DD:EE:02", type="BLE", enc_bucket="Unknown"),
        _record(bssid="Princeton:123456", type="SUBGHZ", enc_bucket="Unknown"),
        _record(bssid="04:A3:91:2B", type="NFC", enc_bucket="Unknown"),
    ]
    written = export.write_wigle_csv(records, path)
    assert written == 2
    assert {r.type for r in parse.parse_file(path)} == {"WIFI", "BLE"}


def test_unknown_type_string_still_falls_back_to_wifi(tmp_path):
    """A third-party CSV with a type warmap has never heard of shouldn't be
    dropped. Wi-Fi is the right guess for a wardrive row."""
    path = tmp_path / "odd.csv"
    path.write_text(
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
        "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
        "AA:BB:CC:DD:EE:01,Net,[OPEN],2026-01-01 00:00:00,6,-50,33.0,-112.0,1,1,ZIGBEE\n"
    )
    assert parse.parse_file(path)[0].type == "WIFI"
