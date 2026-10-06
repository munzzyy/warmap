"""Inputs that are legal text but hostile to the arithmetic behind them: an
"inf" where a number should be, a date the C runtime cannot convert, a track
that crosses the antimeridian, and a folder far too big to sort up front."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from warmap import gps, ingest
from warmap.parse import parse_file

HEADER = (
    "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder,release=v1.14.0\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,Type\n"
)


def _csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "wardrive.csv"
    path.write_text(HEADER + "\n".join(rows) + "\n")
    return path


def test_inf_in_a_numeric_column_costs_that_cell_not_the_file(tmp_path):
    rows = [
        "AA:BB:CC:DD:EE:01,A,[WPA2_PSK],2026-06-14 09:05:09,inf,-57,33.449359,-112.072225,341.3,10.4,WIFI",
        "AA:BB:CC:DD:EE:02,B,[WPA2_PSK],2026-06-14 09:05:10,6,-inf,33.449359,-112.072225,341.3,10.4,WIFI",
        "AA:BB:CC:DD:EE:03,C,[WPA2_PSK],2026-06-14 09:05:11,6,nan,33.449359,-112.072225,nan,10.4,WIFI",
        "AA:BB:CC:DD:EE:04,D,[WPA2_PSK],2026-06-14 09:05:12,6,-50,33.449359,-112.072225,341.3,10.4,WIFI",
    ]
    parsed = parse_file(_csv(tmp_path, rows))
    assert [r.ssid for r in parsed] == ["A", "B", "C", "D"]
    by_ssid = {r.ssid: r for r in parsed}
    assert by_ssid["A"].channel is None
    assert by_ssid["C"].altitude is None
    assert by_ssid["D"].rssi == -50


def test_an_infinite_coordinate_is_not_a_location(tmp_path):
    rows = ["AA:BB:CC:DD:EE:01,A,[WPA2_PSK],2026-06-14 09:05:09,6,-57,inf,-112.072225,341.3,10.4,WIFI"]
    assert parse_file(_csv(tmp_path, rows)) == []


def test_gpx_time_beyond_the_c_runtime_is_skipped_not_fatal():
    assert gps._parse_iso_utc("3000-01-01T00:00:00Z") in (None, datetime(2999, 12, 31, 18, 0), datetime(3000, 1, 1, 0, 0))
    assert gps._parse_iso_utc("not a date") is None


def test_haversine_survives_rounding_past_one():
    assert round(gps.haversine_km(0.0, 0.0, 0.0, 180.0)) == 20015
    assert gps.haversine_km(33.4, -112.0, 33.4, -112.0) == 0.0


def test_interpolation_crosses_the_antimeridian_the_short_way():
    start = datetime(2026, 6, 14, 9, 0, 0)
    track = gps.Track([
        gps.TrackPoint(lat=10.0, lon=179.9, when=start),
        gps.TrackPoint(lat=10.0, lon=-179.9, when=start + timedelta(seconds=60)),
    ])
    found = track.locate(start + timedelta(seconds=30), max_gap_seconds=300)
    assert found is not None
    assert abs(abs(found[1]) - 180.0) < 0.01


def test_expand_paths_stops_walking_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FILES", 5)
    for i in range(20):
        (tmp_path / f"{i:03d}.sub").write_text("Filetype: Flipper SubGhz Key File\n")
    walked: list[Path] = []
    real_rglob = Path.rglob

    def counting_rglob(self, pattern):
        for child in real_rglob(self, pattern):
            walked.append(child)
            yield child

    monkeypatch.setattr(Path, "rglob", counting_rglob)
    found = ingest.expand_paths([tmp_path])
    assert len(found) == 5
    assert found == sorted(found)
    assert len(walked) < 20
