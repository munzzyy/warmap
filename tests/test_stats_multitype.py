"""The stats added alongside multi-technology support: per-type and per-band
breakdowns, Sub-GHz code types, trackers, vendors, and the mapped/unmapped
split.
"""

from __future__ import annotations

from warmap.models import (
    GEO_NONE,
    GEO_TRACK,
    TYPE_BLE,
    TYPE_NFC,
    TYPE_SUBGHZ,
    TYPE_WIFI,
    Sighting,
)
from warmap.radio import BAND_2G, BAND_5G, BAND_SUBGHZ, RISK_ROLLING, RISK_STATIC
from warmap.stats import compute_stats, parse_first_seen


def _s(**kw) -> Sighting:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="HomeNet", auth_mode="", enc_bucket="WPA2",
        first_seen="2026-06-14 09:00:00", channel=6, rssi=-60, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type=TYPE_WIFI, times_seen=1,
        frequency=2437.0, meta={},
    )
    defaults.update(kw)
    return Sighting(**defaults)


def test_type_breakdown():
    records = [_s(), _s(bssid="B", type=TYPE_BLE), _s(bssid="C", type=TYPE_BLE),
               _s(bssid="D", type=TYPE_NFC)]
    stats = compute_stats(records)
    assert stats.type_breakdown == {TYPE_WIFI: 1, TYPE_BLE: 2, TYPE_NFC: 1}


def test_band_breakdown():
    records = [
        _s(frequency=2437.0),
        _s(bssid="B", frequency=5180.0, channel=36),
        _s(bssid="C", type=TYPE_SUBGHZ, frequency=433.92, channel=None),
    ]
    stats = compute_stats(records)
    assert stats.band_breakdown[BAND_2G] == 1
    assert stats.band_breakdown[BAND_5G] == 1
    assert stats.band_breakdown[BAND_SUBGHZ] == 1


def test_code_type_breakdown_only_counts_records_that_have_one():
    records = [
        _s(type=TYPE_SUBGHZ, meta={"code_type": RISK_STATIC}),
        _s(bssid="B", type=TYPE_SUBGHZ, meta={"code_type": RISK_ROLLING}),
        _s(bssid="C", type=TYPE_WIFI),
    ]
    stats = compute_stats(records)
    assert stats.code_type_breakdown == {RISK_STATIC: 1, RISK_ROLLING: 1}


def test_subghz_frequency_tally():
    records = [
        _s(bssid="A", type=TYPE_SUBGHZ, frequency=433.92, channel=None),
        _s(bssid="B", type=TYPE_SUBGHZ, frequency=433.92, channel=None),
        _s(bssid="C", type=TYPE_SUBGHZ, frequency=315.0, channel=None),
    ]
    stats = compute_stats(records)
    assert stats.subghz_frequencies == {"433.92": 2, "315": 1}


def test_tracker_counts():
    records = [
        _s(bssid="A", type=TYPE_BLE, meta={"tracker": "Tile"}),
        _s(bssid="B", type=TYPE_BLE, meta={"tracker": "Tile"}),
        _s(bssid="C", type=TYPE_BLE, meta={"tracker": "Apple AirTag / Find My accessory"}),
        _s(bssid="D", type=TYPE_BLE),
    ]
    stats = compute_stats(records)
    assert stats.tracker_count == 3
    assert stats.tracker_breakdown["Tile"] == 2


def test_randomized_address_count():
    records = [
        _s(bssid="A", meta={"address_type": "random-resolvable"}),
        _s(bssid="B", meta={"address_type": "public"}),
    ]
    assert compute_stats(records).randomized_count == 1


def test_vendor_ranking_is_by_count_then_name():
    records = (
        [_s(bssid=f"A{i}", vendor="Espressif Inc.") for i in range(3)]
        + [_s(bssid=f"B{i}", vendor="Apple, Inc.") for i in range(5)]
        + [_s(bssid="C", vendor="")]
    )
    top = compute_stats(records).top_vendors
    assert top[0] == ("Apple, Inc.", 5)
    assert top[1] == ("Espressif Inc.", 3)
    assert all(name for name, _ in top)  # blank vendors never appear


def test_mapped_and_unmapped_split():
    records = [
        _s(bssid="A"),
        _s(bssid="B", geo_source=GEO_TRACK),
        _s(bssid="C", lat=None, lon=None, geo_source=GEO_NONE),
    ]
    stats = compute_stats(records)
    assert stats.located_count == 2
    assert stats.unlocated_count == 1
    assert stats.track_placed_count == 1


def test_bbox_ignores_unmapped_records():
    """A record with no coordinates must not drag the bounding box to a
    default value."""
    records = [_s(lat=33.0, lon=-112.0), _s(bssid="B", lat=None, lon=None, geo_source=GEO_NONE)]
    stats = compute_stats(records)
    assert stats.bbox == (33.0, -112.0, 33.0, -112.0)


def test_bbox_is_none_when_nothing_is_mapped():
    records = [_s(lat=None, lon=None, geo_source=GEO_NONE)]
    stats = compute_stats(records)
    assert stats.bbox is None
    assert stats.area_km2 is None
    assert stats.total == 1  # still counted


def test_hidden_ssid_count_is_wifi_only():
    records = [
        _s(bssid="A", ssid=""),
        _s(bssid="B", ssid="", type=TYPE_BLE),
        _s(bssid="C", ssid="Named"),
    ]
    stats = compute_stats(records)
    assert stats.hidden_ssid_count == 1
    assert stats.unique_ssids == 1


def test_empty_input_is_all_zeros():
    stats = compute_stats([])
    assert stats.total == 0
    assert stats.type_breakdown == {}
    assert stats.bbox is None


# --- timestamp parsing ---------------------------------------------------

def test_parse_first_seen_handles_iso_with_timezone():
    parsed = parse_first_seen("2026-06-14T09:00:00+02:00")
    assert parsed is not None
    assert parsed.tzinfo is None  # normalized to naive so comparisons work


def test_parse_first_seen_handles_fractional_seconds():
    assert parse_first_seen("2026-06-14T09:00:00.250") is not None


def test_parse_first_seen_rejects_junk():
    assert parse_first_seen("not a time") is None
    assert parse_first_seen("") is None
