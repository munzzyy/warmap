from __future__ import annotations

import csv
import json

from warmap.export import to_geojson, write_csv, write_geojson
from warmap.models import AccessPoint


def _ap(**kw) -> AccessPoint:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="Café Net", auth_mode="[WPA2-PSK-CCMP][ESS]",
        enc_bucket="WPA2", first_seen="2024-01-01 12:00:00", channel=6, rssi=-60,
        lat=33.123456, lon=-112.654321, altitude=340.0, accuracy=5.0, type="WIFI", times_seen=3,
    )
    defaults.update(kw)
    return AccessPoint(**defaults)


def test_write_csv_round_trip(tmp_path):
    aps = [_ap(bssid="A"), _ap(bssid="B", ssid="Other, With Comma")]
    path = tmp_path / "out.csv"
    count = write_csv(aps, path)
    assert count == 2
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["bssid"] == "A"
    assert rows[1]["ssid"] == "Other, With Comma"


def test_write_csv_empty_list(tmp_path):
    path = tmp_path / "empty.csv"
    count = write_csv([], path)
    assert count == 0
    assert path.exists()


def test_to_geojson_shape():
    aps = [_ap()]
    fc = to_geojson(aps)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    feature = fc["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Point"
    # GeoJSON coordinate order is [lon, lat], not [lat, lon]
    assert feature["geometry"]["coordinates"] == [-112.654321, 33.123456]
    assert feature["properties"]["bssid"] == "AA:BB:CC:DD:EE:01"
    assert "lat" not in feature["properties"]
    assert "lon" not in feature["properties"]


def test_write_geojson_is_valid_json_on_disk(tmp_path):
    aps = [_ap(bssid="A"), _ap(bssid="B")]
    path = tmp_path / "out.geojson"
    count = write_geojson(aps, path)
    assert count == 2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["features"]) == 2


def test_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "out.csv"
    write_csv([_ap()], path)
    assert path.exists()
