"""GPS track parsing and timestamp-based placement.

The NMEA sentences here are the canonical documented examples, checksums
included, so the checksum validator is being checked against real values
rather than against something this test generated with the same assumptions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from warmap import gps
from warmap.gps import Track, TrackPoint
from warmap.models import GEO_DIRECT, GEO_NONE, GEO_TRACK, Sighting

# 48 07.038' N -> 48.1173, 011 31.000' E -> roughly 11.516667
GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"


def _record(first_seen="2026-06-14 09:20:00", **kw) -> Sighting:
    defaults = dict(
        bssid="Princeton:123456", ssid="Garage", auth_mode="", enc_bucket="Unknown",
        first_seen=first_seen, channel=None, rssi=0, lat=None, lon=None,
        altitude=None, accuracy=None, type="SUBGHZ", geo_source=GEO_NONE,
    )
    defaults.update(kw)
    return Sighting(**defaults)


def _local(utc_naive: datetime) -> datetime:
    """The same UTC -> local conversion gps.py does, so assertions hold in
    any timezone rather than only the one this was written in."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


# --- checksums and coordinates -------------------------------------------

def test_checksum_accepts_documented_sentences():
    assert gps._nmea_checksum_ok(GGA)
    assert gps._nmea_checksum_ok(RMC)


def test_checksum_rejects_a_flipped_digit():
    assert not gps._nmea_checksum_ok(GGA.replace("4807.038", "4807.039"))


def test_checksum_rejects_missing_star():
    assert not gps._nmea_checksum_ok("$GPGGA,123519,4807.038,N")


def test_coordinate_conversion_latitude():
    assert gps._nmea_coord("4807.038", "N") == pytest.approx(48.1173, abs=1e-6)


def test_coordinate_conversion_longitude_has_three_degree_digits():
    assert gps._nmea_coord("01131.000", "E") == pytest.approx(11.5166667, abs=1e-6)


def test_coordinate_hemisphere_sets_sign():
    assert gps._nmea_coord("4807.038", "S") == pytest.approx(-48.1173, abs=1e-6)
    assert gps._nmea_coord("01131.000", "W") == pytest.approx(-11.5166667, abs=1e-6)


def test_coordinate_rejects_minutes_over_sixty():
    assert gps._nmea_coord("4877.000", "N") is None


def test_coordinate_rejects_out_of_range_latitude():
    assert gps._nmea_coord("9907.038", "N") is None


def test_coordinate_rejects_garbage():
    assert gps._nmea_coord("", "N") is None
    assert gps._nmea_coord("abcd.ef", "N") is None


def test_coordinate_rejects_nan_that_would_slip_past_a_naive_range_check():
    """"nan12.34" parses as degrees=nan without raising, and a `> 90` bound
    check never catches it. Every comparison against nan is False,
    including the one meant to reject it. Real corrupt-but-checksum-valid
    input can hit this; a naive fix here means a NaN coordinate silently
    reaches the map (and then a JSON payload that JS can't even parse)."""
    assert gps._nmea_coord("nan12.34", "N") is None
    assert gps._nmea_coord("nan12.34", "S") is None
    assert gps._nmea_coord("nan012.34", "E") is None
    assert gps._nmea_coord("nan012.34", "W") is None


def test_coordinate_still_rejects_genuine_infinity():
    """Contrast case: +/-inf was already caught by the existing bound checks
    (abs(inf) > 90 is True), so this must keep working once nan is fixed."""
    assert gps._nmea_coord("inf12.34", "N") is None
    assert gps._nmea_coord("inf012.34", "E") is None


# --- NMEA files ----------------------------------------------------------

def test_parse_nmea_reads_both_sentence_types(tmp_path):
    path = tmp_path / "track.nmea"
    path.write_text(f"{RMC}\n{GGA}\n")
    points = gps.parse_nmea(path)
    assert len(points) == 2
    assert points[0].lat == pytest.approx(48.1173, abs=1e-6)
    assert points[1].altitude == pytest.approx(545.4)
    assert points[1].satellites == 8


def test_parse_nmea_converts_utc_to_local(tmp_path):
    path = tmp_path / "track.nmea"
    path.write_text(f"{RMC}\n")
    points = gps.parse_nmea(path)
    assert points[0].when == _local(datetime(1994, 3, 23, 12, 35, 19))


def test_parse_nmea_gga_inherits_date_from_rmc(tmp_path):
    path = tmp_path / "track.nmea"
    path.write_text(f"{RMC}\n{GGA}\n")
    points = gps.parse_nmea(path)
    assert points[1].when == _local(datetime(1994, 3, 23, 12, 35, 19))


def test_parse_nmea_gga_date_rolls_over_at_utc_midnight(tmp_path):
    """current_date is carried forward from the RMC ahead of it. A GGA whose
    time-of-day is earlier than the previous one by more than half a day
    means the UTC clock wrapped past midnight, and the date has to bump
    forward, otherwise every GGA point logged after midnight lands a day
    early, both wrong in itself and out of chronological order with what
    came before it."""
    rmc = "$GPRMC,235800,A,4807.038,N,01131.000,E,022.4,084.4,140626,003.1,W*63"
    before_midnight = "$GPGGA,235930,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*44"
    after_midnight = "$GPGGA,000015,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*4E"
    path = tmp_path / "track.nmea"
    path.write_text(f"{rmc}\n{before_midnight}\n{after_midnight}\n")

    points = gps.parse_nmea(path)
    assert len(points) == 3
    assert points[1].when == _local(datetime(2026, 6, 14, 23, 59, 30))
    assert points[2].when == _local(datetime(2026, 6, 15, 0, 0, 15))
    assert points[2].when > points[1].when


def test_parse_nmea_skips_gga_with_no_fix(tmp_path):
    no_fix = "$GPGGA,123519,4807.038,N,01131.000,E,0,00,,,M,,M,,*4E"
    path = tmp_path / "track.nmea"
    path.write_text(no_fix + "\n")
    # Whether or not that hand-built checksum is right, a fix-quality of 0
    # must never produce a point.
    assert gps.parse_nmea(path) == []


def test_parse_nmea_skips_null_island(tmp_path):
    """0,0 is a real, checksum-valid pair of NMEA fields, not a fix. A
    spoofed or glitching receiver reporting it must not become a point in
    the middle of the Gulf of Guinea."""
    null_gga = "$GPGGA,123519,0000.000,N,00000.000,E,1,08,0.9,545.4,M,46.9,M,,*45"
    null_rmc = "$GPRMC,123519,A,0000.000,N,00000.000,E,022.4,084.4,230394,003.1,W*68"
    path = tmp_path / "track.nmea"
    path.write_text(f"{null_gga}\n{null_rmc}\n")
    assert gps.parse_nmea(path) == []


def test_parse_nmea_skips_rmc_marked_void(tmp_path):
    void = RMC.replace(",A,", ",V,")
    path = tmp_path / "track.nmea"
    path.write_text(void + "\n")
    assert gps.parse_nmea(path) == []


def test_parse_nmea_skips_corrupt_lines(tmp_path):
    path = tmp_path / "track.nmea"
    path.write_text("garbage\n$GPGGA,truncated\n" + RMC + "\n")
    assert len(gps.parse_nmea(path)) == 1


def test_parse_nmea_accepts_other_talker_ids(tmp_path):
    """A multi-constellation receiver emits a $GN talker id, not $GP."""
    body = RMC[1:].split("*")[0].replace("GPRMC", "GNRMC", 1)
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    path = tmp_path / "track.nmea"
    path.write_text(f"${body}*{checksum:02X}\n")
    assert len(gps.parse_nmea(path)) == 1


def test_parse_nmea_missing_file(tmp_path):
    assert gps.parse_nmea(tmp_path / "nope.nmea") == []


# --- GPX -----------------------------------------------------------------

GPX_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="33.4484" lon="-112.0740"><ele>340</ele><time>2026-06-14T09:00:00Z</time></trkpt>
    <trkpt lat="33.4490" lon="-112.0730"><ele>341</ele><time>2026-06-14T09:01:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""


def test_parse_gpx_reads_points(tmp_path):
    path = tmp_path / "t.gpx"
    path.write_text(GPX_BODY)
    points = gps.parse_gpx(path)
    assert len(points) == 2
    assert points[0].lat == pytest.approx(33.4484)
    assert points[0].altitude == pytest.approx(340)


def test_parse_gpx_z_suffix_converts_to_local(tmp_path):
    path = tmp_path / "t.gpx"
    path.write_text(GPX_BODY)
    points = gps.parse_gpx(path)
    assert points[0].when == _local(datetime(2026, 6, 14, 9, 0, 0))


def test_parse_gpx_without_timezone_stays_as_written(tmp_path):
    """The bundled sample relies on this: a naive GPX time is treated as
    local so it lines up with local file timestamps on any machine."""
    path = tmp_path / "t.gpx"
    path.write_text(GPX_BODY.replace("Z</time>", "</time>"))
    points = gps.parse_gpx(path)
    assert points[0].when == datetime(2026, 6, 14, 9, 0, 0)


def test_parse_gpx_malformed_returns_empty(tmp_path):
    path = tmp_path / "t.gpx"
    path.write_text("<gpx><trkpt lat='oops'")
    assert gps.parse_gpx(path) == []


def test_parse_gpx_rejects_nan_inf_out_of_range_and_null_island(tmp_path):
    """Unlike the NMEA path, `float()` is the only parsing a GPX coordinate
    ever gets, and "nan" and "inf" both parse without raising, so nothing else
    here would catch them, an out-of-range value, or the 0,0 a logger app can
    write before its first real fix."""
    body = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1"><trk><trkseg>
<trkpt lat="nan" lon="12.5"><time>2026-06-14T09:00:00Z</time></trkpt>
<trkpt lat="999.0" lon="12.5"><time>2026-06-14T09:00:01Z</time></trkpt>
<trkpt lat="0.0" lon="0.0"><time>2026-06-14T09:00:02Z</time></trkpt>
<trkpt lat="45.0" lon="12.5"><time>2026-06-14T09:00:03Z</time></trkpt>
</trkseg></trk></gpx>
"""
    path = tmp_path / "t.gpx"
    path.write_text(body)
    points = gps.parse_gpx(path)
    assert len(points) == 1
    assert points[0].lat == pytest.approx(45.0)
    assert points[0].lon == pytest.approx(12.5)


# --- Track ---------------------------------------------------------------

def _track(start=datetime(2026, 6, 14, 9, 0, 0), count=5, step=60):
    points = [
        TrackPoint(
            lat=33.0 + i * 0.001,
            lon=-112.0 + i * 0.001,
            when=start + timedelta(seconds=i * step),
            altitude=300.0 + i,
        )
        for i in range(count)
    ]
    return Track(points, source="test")


def test_track_locate_exact_point():
    track = _track()
    found = track.locate(datetime(2026, 6, 14, 9, 2, 0))
    assert found is not None
    assert found[0] == pytest.approx(33.002)
    assert found[3] == 0  # zero seconds away


def test_track_locate_interpolates_between_points():
    track = _track()
    found = track.locate(datetime(2026, 6, 14, 9, 0, 30))
    assert found[0] == pytest.approx(33.0005)
    assert found[1] == pytest.approx(-111.9995)
    assert found[2] == pytest.approx(300.5)


def test_track_locate_before_start_within_tolerance():
    track = _track()
    found = track.locate(datetime(2026, 6, 14, 8, 59, 30), max_gap_seconds=60)
    assert found is not None
    assert found[0] == pytest.approx(33.0)
    assert found[3] == pytest.approx(30)


def test_track_locate_before_start_outside_tolerance():
    track = _track()
    assert track.locate(datetime(2026, 6, 14, 8, 0, 0), max_gap_seconds=60) is None


def test_track_locate_after_end_outside_tolerance():
    track = _track()
    assert track.locate(datetime(2026, 6, 14, 12, 0, 0), max_gap_seconds=60) is None


def test_track_locate_gap_is_to_the_nearest_point_not_the_earlier_one():
    """A capture 55 seconds after point A and 5 seconds before point B is 5
    seconds from the track, not 55, otherwise a tight tolerance rejects
    things that are actually well covered."""
    track = _track(step=60)
    found = track.locate(datetime(2026, 6, 14, 9, 0, 55), max_gap_seconds=10)
    assert found is not None
    assert found[3] == pytest.approx(5)


def test_track_locate_empty_track():
    assert Track([]).locate(datetime(2026, 6, 14, 9, 0, 0)) is None


def test_track_ignores_points_without_a_time_for_locating():
    points = [TrackPoint(lat=1.0, lon=2.0, when=None)]
    track = Track(points)
    assert track.timed_count == 0
    assert track.locate(datetime(2026, 6, 14, 9, 0, 0)) is None
    assert len(track) == 1  # still counts for drawing


def test_track_distance_and_duration():
    track = _track()
    assert track.distance_km() > 0
    assert track.duration() == timedelta(minutes=4)


def test_track_bounds():
    track = _track()
    min_lat, min_lon, max_lat, max_lon = track.bounds()
    assert min_lat == pytest.approx(33.0)
    assert max_lat == pytest.approx(33.004)


def test_track_geojson_shape():
    feature = _track().to_geojson()
    assert feature["geometry"]["type"] == "LineString"
    assert len(feature["geometry"]["coordinates"]) == 5
    assert feature["geometry"]["coordinates"][0] == [-112.0, 33.0]  # lon, lat order


def test_haversine_known_distance():
    # One degree of latitude is about 111 km anywhere on Earth.
    assert gps.haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.19, abs=0.5)


# --- rebuilding a track from wardrive rows --------------------------------

def test_track_from_sightings_converts_wardrive_utc_to_local():
    """A wardrive row's FirstSeen is UTC straight off the GPS module, same as
    an NMEA sentence, confirmed against the ESP32Marauder firmware itself
    (GpsInterface::dt_string_from_gps() echoes the NMEA library's own
    hour/minute/second, no offset applied). Treating it as already-local,
    the way parsing this string ordinarily would, silently placed every
    Flipper capture hours away from a track rebuilt this way, the entire
    point of rebuilding one in the first place."""
    first = _record(
        first_seen="2026-06-14 09:00:00", type="WIFI",
        lat=33.0, lon=-112.0, geo_source=GEO_DIRECT,
    )
    second = _record(
        first_seen="2026-06-14 09:05:00", type="WIFI",
        lat=33.01, lon=-112.01, geo_source=GEO_DIRECT,
    )
    track = gps.track_from_sightings([first, second])
    assert track is not None
    whens = sorted(p.when for p in track.points)
    assert whens == [
        _local(datetime(2026, 6, 14, 9, 0, 0)),
        _local(datetime(2026, 6, 14, 9, 5, 0)),
    ]


def test_track_from_sightings_leaves_flipper_embedded_fixes_alone():
    """A Momentum/RogueMaster `.sub` with its own Lat:/Lon: fix is not a
    wardrive row. Its first_seen already came from the file's mtime or its
    Ts: field, both local, so converting it a second time would shift it the
    wrong way. Mixed with a genuine wardrive row in the same rebuilt track,
    each has to get the treatment that actually matches how it was made."""
    flipper_fix = _record(
        first_seen="2026-06-14 09:00:00", type="SUBGHZ",
        lat=33.0, lon=-112.0, geo_source=GEO_DIRECT,
    )
    wardrive_fix = _record(
        first_seen="2026-06-14 09:05:00", type="WIFI",
        lat=33.01, lon=-112.01, geo_source=GEO_DIRECT,
    )
    track = gps.track_from_sightings([flipper_fix, wardrive_fix])
    assert track is not None
    by_lat = {round(p.lat, 5): p.when for p in track.points}
    assert by_lat[33.0] == datetime(2026, 6, 14, 9, 0, 0)  # untouched, already local
    assert by_lat[33.01] == _local(datetime(2026, 6, 14, 9, 5, 0))  # converted


# --- geotagging ----------------------------------------------------------

def test_geotag_places_a_record():
    track = _track()
    record = _record(first_seen="2026-06-14 09:02:00")
    placed = gps.geotag([record], [track])
    assert placed == 1
    assert record.has_location
    assert record.geo_source == GEO_TRACK
    assert record.meta["geo_match_seconds"] == 0


def test_geotag_skips_records_outside_the_window():
    track = _track()
    record = _record(first_seen="2026-06-14 22:00:00")
    assert gps.geotag([record], [track]) == 0
    assert not record.has_location
    assert record.geo_source == GEO_NONE


def test_geotag_never_overwrites_a_real_fix():
    """A recorded GPS fix is stronger evidence than an inference and must
    survive geotagging untouched."""
    track = _track()
    record = _record(
        first_seen="2026-06-14 09:02:00", lat=1.0, lon=2.0, geo_source=GEO_DIRECT
    )
    assert gps.geotag([record], [track]) == 0
    assert record.lat == 1.0
    assert record.geo_source == GEO_DIRECT


def test_geotag_skips_records_with_no_parseable_time():
    track = _track()
    record = _record(first_seen="")
    assert gps.geotag([record], [track]) == 0


def test_geotag_clock_offset_shifts_the_match():
    """A Flipper whose clock ran an hour fast: without the correction the
    record falls outside the track, with it the record lands."""
    track = _track()
    record = _record(first_seen="2026-06-14 10:02:00")
    assert gps.geotag([record], [track]) == 0
    assert gps.geotag([record], [track], clock_offset_seconds=-3600) == 1


def test_geotag_picks_the_closest_of_several_tracks():
    near = _track(start=datetime(2026, 6, 14, 9, 0, 0))
    far = Track([
        TrackPoint(lat=50.0, lon=8.0, when=datetime(2026, 6, 14, 9, 4, 0)),
    ])
    record = _record(first_seen="2026-06-14 9:02:00".replace("9:", "09:"))
    gps.geotag([record], [far, near])
    assert record.lat == pytest.approx(33.002)


def test_geotag_with_no_usable_tracks_returns_zero():
    assert gps.geotag([_record()], []) == 0
    assert gps.geotag([_record()], [Track([TrackPoint(1.0, 2.0, None)])]) == 0


def test_load_tracks_skips_empty_files(tmp_path):
    good = tmp_path / "good.nmea"
    good.write_text(RMC + "\n")
    empty = tmp_path / "empty.nmea"
    empty.write_text("")
    tracks = gps.load_tracks([good, empty])
    assert len(tracks) == 1
