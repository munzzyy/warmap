from __future__ import annotations

import math

from warmap.models import AccessPoint
from warmap.stats import bbox_area_km2, compute_stats, parse_first_seen


def _ap(**kw) -> AccessPoint:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="Net", auth_mode="", enc_bucket="WPA2",
        first_seen="2024-01-01 12:00:00", channel=6, rssi=-60, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type="WIFI", times_seen=1,
    )
    defaults.update(kw)
    return AccessPoint(**defaults)


def test_empty_input_is_all_zero_not_crash():
    stats = compute_stats([])
    assert stats.total == 0
    assert stats.bbox is None
    assert stats.area_km2 is None
    assert stats.first_seen_min is None


def test_total_and_unique_ssid_counts():
    aps = [_ap(bssid="A", ssid="Home"), _ap(bssid="B", ssid="Home"), _ap(bssid="C", ssid="Office")]
    stats = compute_stats(aps)
    assert stats.total == 3
    assert stats.unique_ssids == 2


def test_blank_ssid_not_counted_as_a_unique_ssid():
    aps = [_ap(bssid="A", ssid=""), _ap(bssid="B", ssid="")]
    stats = compute_stats(aps)
    assert stats.unique_ssids == 0


def test_open_count():
    aps = [_ap(bssid="A", enc_bucket="Open"), _ap(bssid="B", enc_bucket="WPA2")]
    stats = compute_stats(aps)
    assert stats.open_count == 1


def test_wifi_ble_counts():
    aps = [_ap(bssid="A", type="WIFI"), _ap(bssid="B", type="BLE"), _ap(bssid="C", type="BLE")]
    stats = compute_stats(aps)
    assert stats.wifi_count == 1
    assert stats.ble_count == 2


def test_enc_breakdown():
    aps = [_ap(bssid="A", enc_bucket="Open"), _ap(bssid="B", enc_bucket="Open"), _ap(bssid="C", enc_bucket="WPA2")]
    stats = compute_stats(aps)
    assert stats.enc_breakdown == {"Open": 2, "WPA2": 1}


def test_channel_breakdown_handles_missing_channel():
    aps = [_ap(bssid="A", channel=6), _ap(bssid="B", channel=None)]
    stats = compute_stats(aps)
    assert stats.channel_breakdown == {"6": 1, "unknown": 1}


def test_bbox_and_area():
    aps = [_ap(bssid="A", lat=33.0, lon=-112.0), _ap(bssid="B", lat=33.01, lon=-112.01)]
    stats = compute_stats(aps)
    assert stats.bbox == (33.0, -112.01, 33.01, -112.0)
    assert stats.area_km2 > 0


def test_time_range():
    aps = [
        _ap(bssid="A", first_seen="2024-01-01 12:00:00"),
        _ap(bssid="B", first_seen="2024-01-01 15:30:00"),
    ]
    stats = compute_stats(aps)
    assert stats.first_seen_min == "2024-01-01 12:00:00"
    assert stats.first_seen_max == "2024-01-01 15:30:00"


def test_unparsable_timestamps_are_excluded_not_crashed():
    aps = [_ap(bssid="A", first_seen="not-a-date"), _ap(bssid="B", first_seen="")]
    stats = compute_stats(aps)
    assert stats.first_seen_min is None
    assert stats.first_seen_max is None


def test_parse_first_seen_formats():
    assert parse_first_seen("2024-06-01 12:34:56") is not None
    assert parse_first_seen("") is None
    assert parse_first_seen("garbage") is None


def test_bbox_area_zero_for_a_single_point():
    assert bbox_area_km2(33.0, -112.0, 33.0, -112.0) == 0.0


def test_bbox_area_reasonable_for_known_span():
    # Roughly one degree of latitude is ~111km; a 0.01 x 0.01 degree box
    # near the equator should be on the order of ~1 km^2, not wildly off.
    area = bbox_area_km2(0.0, 0.0, 0.01, 0.01)
    assert 0.5 < area < 2.0
    assert math.isfinite(area)


def test_a_never_set_clock_is_not_a_capture_time():
    """A board that writes before the GPS gives it the time stamps the record
    a few seconds after the epoch. One such record in a real BFFB capture
    reported the whole wardrive as running from 1969."""
    assert parse_first_seen("1969-12-31 18:00:18") is None
    assert parse_first_seen("1970-01-01 00:00:18") is None


def test_a_real_capture_time_still_parses():
    assert parse_first_seen("2026-07-26 01:46:39") is not None


def test_the_time_range_ignores_an_unset_clock():
    stats = compute_stats([
        _ap(first_seen="1969-12-31 18:00:18"),
        _ap(first_seen="2026-07-26 01:14:04"),
        _ap(first_seen="2026-07-26 01:46:39"),
    ])
    assert stats.first_seen_min == "2026-07-26 01:14:04"
    assert stats.first_seen_max == "2026-07-26 01:46:39"
