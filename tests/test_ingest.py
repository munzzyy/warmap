"""The ingest dispatcher: routing files to the right parser, loading tracks
before the things they place, and reporting what happened when nothing shows
up (which is most of the ways this goes wrong).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from warmap import gps, ingest
from warmap.models import GEO_DIRECT, GEO_NONE, GEO_TRACK, TYPE_SUBGHZ, TYPE_WIFI


def _local(utc_naive: datetime) -> datetime:
    """The same UTC -> local conversion a wardrive row's FirstSeen gets, so a
    fixture Flipper timestamp meant to line up with one can be computed
    rather than hard-coded. This holds in any timezone, not just the one it
    was written in. See tests/test_gps.py for the same helper."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


WIGLE_CSV = (
    "WigleWifi-1.6,appRelease=1.0\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,"
    "CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type\n"
    "AA:BB:CC:DD:EE:01,HomeNet,[WPA2-PSK-CCMP][ESS],2026-06-14 09:00:30,6,2437,"
    "-50,33.0,-112.0,340,5,,,WIFI\n"
)

SUB_FILE = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 12 34 56
"""

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="33.0" lon="-112.0"><time>2026-06-14T09:00:00</time></trkpt>
    <trkpt lat="33.01" lon="-112.01"><time>2026-06-14T09:10:00</time></trkpt>
  </trkseg></trk>
</gpx>
"""

NMEA = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\n"


# --- classification ------------------------------------------------------

def test_classify_each_kind(tmp_path):
    (tmp_path / "a.csv").write_text(WIGLE_CSV)
    (tmp_path / "b.sub").write_text(SUB_FILE)
    (tmp_path / "c.pcap").write_bytes(b"\x00" * 32)
    (tmp_path / "d.gpx").write_text(GPX)
    (tmp_path / "e.png").write_bytes(b"\x89PNG")

    assert ingest.classify(tmp_path / "a.csv") == "csv"
    assert ingest.classify(tmp_path / "b.sub") == "flipper"
    assert ingest.classify(tmp_path / "c.pcap") == "pcap"
    assert ingest.classify(tmp_path / "d.gpx") == "track"
    assert ingest.classify(tmp_path / "e.png") == "unknown"


def test_log_file_is_only_a_track_if_it_looks_like_nmea(tmp_path):
    """`.log` on a Flipper SD card is usually the firmware's debug log, so
    the extension alone can't decide."""
    gps_log = tmp_path / "gps.log"
    gps_log.write_text(NMEA)
    other_log = tmp_path / "debug.log"
    other_log.write_text("boot ok\nmounted sd\n")

    assert ingest.classify(gps_log) == "track"
    assert ingest.classify(other_log) == "unknown"


# --- path expansion ------------------------------------------------------

def test_expand_paths_walks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.csv").write_text(WIGLE_CSV)
    (tmp_path / "b.sub").write_text(SUB_FILE)
    found = ingest.expand_paths([tmp_path])
    assert {p.name for p in found} == {"a.csv", "b.sub"}


def test_expand_paths_leaves_files_alone(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text(WIGLE_CSV)
    assert ingest.expand_paths([path]) == [path]


def test_expand_paths_on_a_missing_path(tmp_path):
    assert ingest.expand_paths([tmp_path / "gone"]) == []


# --- ingest --------------------------------------------------------------

def test_csv_only_import(tmp_path):
    (tmp_path / "a.csv").write_text(WIGLE_CSV)
    result = ingest.ingest_paths([tmp_path])
    assert len(result.sightings) == 1
    assert result.sightings[0].type == TYPE_WIFI
    assert result.sightings[0].geo_source == GEO_DIRECT
    assert result.files_read == 1


def test_flipper_without_a_track_is_reported_not_silently_dropped(tmp_path):
    (tmp_path / "Garage_20260614-090500.sub").write_text(SUB_FILE)
    result = ingest.ingest_paths([tmp_path])
    assert len(result.sightings) == 1
    assert result.sightings[0].geo_source == GEO_NONE
    assert result.unlocated == 1
    assert any("no location" in note for note in result.notes)
    # Says what to actually do about it, not just that it happened.
    assert any("wardrive" in note for note in result.notes)
    assert any(".nmea" in note or ".gpx" in note for note in result.notes)


def test_track_places_flipper_captures(tmp_path):
    (tmp_path / "Garage_20260614-090500.sub").write_text(SUB_FILE)
    (tmp_path / "track.gpx").write_text(GPX)
    result = ingest.ingest_paths([tmp_path])
    record = [s for s in result.sightings if s.type == TYPE_SUBGHZ][0]
    assert record.geo_source == GEO_TRACK
    assert record.has_location
    assert any("Placed 1" in note for note in result.notes)


def test_track_loaded_separately_still_places(tmp_path):
    """Importing a folder of captures after a track was already loaded has to
    work. That's the normal order of operations."""
    (tmp_path / "track.gpx").write_text(GPX)
    first = ingest.ingest_paths([tmp_path / "track.gpx"])

    (tmp_path / "Garage_20260614-090500.sub").write_text(SUB_FILE)
    second = ingest.ingest_paths(
        [tmp_path / "Garage_20260614-090500.sub"], extra_tracks=first.tracks
    )
    assert second.sightings[0].geo_source == GEO_TRACK


def test_capture_outside_the_track_window_is_reported(tmp_path):
    (tmp_path / "Garage_20261225-090500.sub").write_text(SUB_FILE)
    (tmp_path / "track.gpx").write_text(GPX)
    result = ingest.ingest_paths([tmp_path])
    record = [s for s in result.sightings if s.type == TYPE_SUBGHZ][0]
    assert record.geo_source == GEO_NONE
    assert any("no track point within" in note for note in result.notes)


def test_max_gap_is_respected(tmp_path):
    (tmp_path / "Garage_20260614-092000.sub").write_text(SUB_FILE)  # 10 min past the end
    (tmp_path / "track.gpx").write_text(GPX)

    tight = ingest.ingest_paths([tmp_path], max_gap_seconds=60)
    assert [s for s in tight.sightings if s.type == TYPE_SUBGHZ][0].geo_source == GEO_NONE

    loose = ingest.ingest_paths([tmp_path], max_gap_seconds=3600)
    assert [s for s in loose.sightings if s.type == TYPE_SUBGHZ][0].geo_source == GEO_TRACK


def test_clock_offset_is_respected(tmp_path):
    (tmp_path / "Garage_20260614-100500.sub").write_text(SUB_FILE)  # an hour fast
    (tmp_path / "track.gpx").write_text(GPX)
    result = ingest.ingest_paths([tmp_path], clock_offset_seconds=-3600)
    assert [s for s in result.sightings if s.type == TYPE_SUBGHZ][0].geo_source == GEO_TRACK


def test_unreadable_pcap_link_type_becomes_an_error_not_a_silent_zero(tmp_path):
    import struct
    ethernet = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    ethernet += struct.pack("<IIII", 1700000000, 0, 4, 4) + b"\x00\x00\x00\x00"
    (tmp_path / "a.pcap").write_bytes(ethernet)
    result = ingest.ingest_paths([tmp_path])
    assert result.sightings == []
    assert result.errors
    assert "Ethernet" in result.errors[0]


def test_unknown_files_are_counted_as_skipped(tmp_path):
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")
    (tmp_path / "a.csv").write_text(WIGLE_CSV)
    result = ingest.ingest_paths([tmp_path])
    assert result.files_skipped == 1
    assert result.files_read == 1


def test_empty_input(tmp_path):
    result = ingest.ingest_paths([tmp_path])
    assert result.sightings == []
    assert result.notes


def test_clobbered_mtime_warning_appears(tmp_path):
    """Files copied off an SD card without preserving timestamps can't be
    placed, and the reason has to be stated, otherwise it just looks like
    the track didn't work."""
    for i in range(4):
        (tmp_path / f"cap{i}.sub").write_text(SUB_FILE)
    result = ingest.ingest_paths([tmp_path])
    assert any("copied off the SD card" in note for note in result.notes)


def test_summary_is_readable(tmp_path):
    (tmp_path / "a.csv").write_text(WIGLE_CSV)
    (tmp_path / "track.gpx").write_text(GPX)
    summary = ingest.ingest_paths([tmp_path]).summary()
    assert "record(s)" in summary
    assert "GPS track" in summary


def test_mixed_import_reads_everything(tmp_path):
    (tmp_path / "a.csv").write_text(WIGLE_CSV)
    (tmp_path / "Garage_20260614-090500.sub").write_text(SUB_FILE)
    (tmp_path / "track.gpx").write_text(GPX)
    result = ingest.ingest_paths([tmp_path])
    types = {s.type for s in result.sightings}
    assert types == {TYPE_WIFI, TYPE_SUBGHZ}
    assert len(result.tracks) == 1
    assert result.located == 2


# --- placing captures with no track file at all --------------------------

WARDRIVE_WITH_FIXES = (
    "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,Type\n"
    "AA:BB:CC:DD:EE:01,NetA,[OPEN],2026-06-14 09:00:00,6,-50,33.000,-112.000,340,5,WIFI\n"
    "AA:BB:CC:DD:EE:02,NetB,[WPA2_PSK],2026-06-14 09:05:00,6,-55,33.010,-112.010,340,5,WIFI\n"
    "AA:BB:CC:DD:EE:03,NetC,[WPA2_PSK],2026-06-14 09:10:00,6,-60,33.020,-112.020,340,5,WIFI\n"
)


def test_a_wardrive_doubles_as_the_track_when_no_track_file_exists(tmp_path):
    """Nothing on a Flipper logs GPS, so most sessions have no track file.
    A wardrive row already carries the fix it was recorded at, which is all a
    track is, otherwise every NFC read and Sub-GHz capture stays unplaced.

    The wardrive row's FirstSeen ("2026-06-14 09:05:00") is UTC, straight off
    the GPS module; the Flipper's filename timestamp is local, straight off
    its own RTC, the same distinction as a real capture, so the fixture uses
    `_local()` to name the file at the true local clock reading for that same
    instant rather than reusing the CSV's own digits.
    """
    (tmp_path / "wardrive_0.txt").write_text(WARDRIVE_WITH_FIXES)
    stamp = _local(datetime(2026, 6, 14, 9, 5, 0)).strftime("%Y%m%d-%H%M%S")
    (tmp_path / f"Gate_{stamp}.sub").write_text(SUB_FILE)

    result = ingest.ingest_paths([tmp_path])
    capture = [s for s in result.sightings if s.type == TYPE_SUBGHZ][0]

    assert capture.geo_source == GEO_TRACK
    assert capture.has_location
    assert any("rebuilt from" in note for note in result.notes)


def test_the_rebuilt_track_is_marked_as_derived(tmp_path):
    (tmp_path / "wardrive_0.txt").write_text(WARDRIVE_WITH_FIXES)
    (tmp_path / "Gate_20260614-090500.sub").write_text(SUB_FILE)
    result = ingest.ingest_paths([tmp_path])
    assert result.tracks[0].derived is True
    assert result.tracks[0].to_geojson()["properties"]["derived"] is True


def test_a_real_track_file_gets_first_refusal(tmp_path):
    """The rebuild is a fallback. A logged track has denser points, so when
    one covers the capture it should be used and nothing rebuilt."""
    (tmp_path / "wardrive_0.txt").write_text(WARDRIVE_WITH_FIXES)
    (tmp_path / "track.gpx").write_text(GPX)
    (tmp_path / "Gate_20260614-090500.sub").write_text(SUB_FILE)

    result = ingest.ingest_paths([tmp_path])
    assert len(result.tracks) == 1
    assert result.tracks[0].derived is False


def test_an_unrelated_track_does_not_suppress_the_rebuild(tmp_path):
    """The bug this guards against: a track loaded earlier in the session
    that covers a different day made "a track exists" true, so the rebuild
    never ran and every capture stayed unplaced."""
    (tmp_path / "wardrive_0.txt").write_text(WARDRIVE_WITH_FIXES)
    stamp = _local(datetime(2026, 6, 14, 9, 5, 0)).strftime("%Y%m%d-%H%M%S")
    (tmp_path / f"Gate_{stamp}.sub").write_text(SUB_FILE)

    stale = gps.Track(
        [gps.TrackPoint(lat=50.0, lon=8.0, when=datetime(2020, 1, 1, 12, 0)),
         gps.TrackPoint(lat=50.1, lon=8.1, when=datetime(2020, 1, 1, 12, 10))],
        source="last year.gpx",
    )
    result = ingest.ingest_paths([tmp_path], extra_tracks=[stale])

    capture = [s for s in result.sightings if s.type == TYPE_SUBGHZ][0]
    assert capture.geo_source == GEO_TRACK
    assert capture.lat == pytest.approx(33.01, abs=0.02)  # placed locally, not in Germany


def test_rebuilding_never_feeds_inferred_positions_back_in(tmp_path):
    """A record placed from a track must not then become a track point.
    That would be inferring positions from inferred positions."""
    (tmp_path / "wardrive_0.txt").write_text(WARDRIVE_WITH_FIXES)
    (tmp_path / "Gate_20260614-090500.sub").write_text(SUB_FILE)
    result = ingest.ingest_paths([tmp_path])

    track = result.tracks[0]
    assert len(track) == 3  # the three wardrive rows, not the placed capture


def test_no_rebuild_when_there_is_nothing_to_rebuild_from(tmp_path):
    """Flipper files alone carry no fixes, so there's no track to recover and
    the user has to be told rather than left with an empty map."""
    (tmp_path / "Gate_20260614-090500.sub").write_text(SUB_FILE)
    result = ingest.ingest_paths([tmp_path])
    assert result.tracks == []
    assert result.sightings[0].geo_source == GEO_NONE
    assert any("no location" in note for note in result.notes)


def test_a_single_wardrive_row_is_not_a_track(tmp_path):
    """One point can't place anything between two moments in time."""
    one_row = "\n".join(WARDRIVE_WITH_FIXES.splitlines()[:3]) + "\n"
    (tmp_path / "wardrive_0.txt").write_text(one_row)
    (tmp_path / "Gate_20260614-090500.sub").write_text(SUB_FILE)
    result = ingest.ingest_paths([tmp_path])
    assert result.tracks == []


def test_content_sniffing_sees_past_a_byte_order_mark(tmp_path):
    """`_looks_like_wardrive` identifies a file by `startswith("WigleWifi")`,
    and a BOM sits in exactly that spot. A full Marauder file survives a BOM
    either way because the column-header fallback catches it, so this pins
    the fast path itself, the case where the version line is the only marker
    the sniffer gets.
    """
    path = tmp_path / "wardrive_0.txt"
    path.write_bytes(b"\xef\xbb\xbf" + b"WigleWifi-1.4,appRelease=v1.14.0\n")
    assert ingest._head(path).startswith("WigleWifi")
    assert ingest._looks_like_wardrive(ingest._head(path))


def test_a_wardrive_with_a_byte_order_mark_still_imports(tmp_path):
    path = tmp_path / "wardrive_0.csv"
    path.write_bytes(b"\xef\xbb\xbf" + WIGLE_CSV.encode("utf-8"))
    result = ingest.ingest_paths([tmp_path])
    assert len(result.sightings) == 1
    assert result.sightings[0].ssid == "HomeNet"
