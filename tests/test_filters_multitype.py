"""The filters added alongside multi-technology support: record type, band,
Sub-GHz code type, trackers, randomized addresses, location, times-seen and
time range, plus the None-vs-empty-set convention they all share.
"""

from __future__ import annotations

from datetime import datetime

from warmap.filters import FilterState, apply_filters
from warmap.models import (
    GEO_NONE,
    TYPE_BLE,
    TYPE_NFC,
    TYPE_SUBGHZ,
    TYPE_WIFI,
    Sighting,
)
from warmap.radio import BAND_2G, BAND_5G, BAND_SUBGHZ, RISK_ROLLING, RISK_STATIC


def _s(**kw) -> Sighting:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="HomeNet", auth_mode="", enc_bucket="WPA2",
        first_seen="2026-06-14 09:00:00", channel=6, rssi=-60, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type=TYPE_WIFI, times_seen=1,
        frequency=2437.0, meta={},
    )
    defaults.update(kw)
    return Sighting(**defaults)


# --- record type ---------------------------------------------------------

def test_type_filter_across_all_types():
    records = [_s(type=TYPE_WIFI), _s(bssid="B", type=TYPE_SUBGHZ), _s(bssid="C", type=TYPE_NFC)]
    result = apply_filters(records, FilterState(types=frozenset({TYPE_SUBGHZ, TYPE_NFC})))
    assert {r.type for r in result} == {TYPE_SUBGHZ, TYPE_NFC}


def test_unticking_every_type_shows_nothing():
    """Empty set means the user turned everything off, which is different
    from "no opinion" and must not silently show everything."""
    assert apply_filters([_s()], FilterState(types=frozenset())) == []


# --- band ----------------------------------------------------------------

def test_band_filter_uses_frequency():
    records = [
        _s(frequency=2437.0, channel=6),
        _s(bssid="B", frequency=5180.0, channel=36),
        _s(bssid="C", type=TYPE_SUBGHZ, frequency=433.92, channel=None),
    ]
    result = apply_filters(records, FilterState(bands=frozenset({BAND_5G})))
    assert [r.bssid for r in result] == ["B"]


def test_band_filter_falls_back_to_channel():
    record = _s(frequency=None, channel=36)
    assert len(apply_filters([record], FilterState(bands=frozenset({BAND_5G})))) == 1


def test_band_filter_none_means_all():
    records = [_s(), _s(bssid="B", frequency=433.92, type=TYPE_SUBGHZ)]
    assert len(apply_filters(records, FilterState(bands=None))) == 2


def test_subghz_band():
    record = _s(type=TYPE_SUBGHZ, frequency=868.35, channel=None)
    assert len(apply_filters([record], FilterState(bands=frozenset({BAND_SUBGHZ})))) == 1
    assert apply_filters([record], FilterState(bands=frozenset({BAND_2G}))) == []


# --- Sub-GHz code type ---------------------------------------------------

def test_code_type_filter():
    records = [
        _s(bssid="static", type=TYPE_SUBGHZ, meta={"code_type": RISK_STATIC}),
        _s(bssid="rolling", type=TYPE_SUBGHZ, meta={"code_type": RISK_ROLLING}),
    ]
    result = apply_filters(records, FilterState(code_types=frozenset({RISK_STATIC})))
    assert [r.bssid for r in result] == ["static"]


def test_code_type_filter_leaves_other_types_alone():
    """A code-type filter is about Sub-GHz. It must not quietly hide every
    Wi-Fi record, which carries no code type at all."""
    records = [
        _s(bssid="wifi", type=TYPE_WIFI),
        _s(bssid="rolling", type=TYPE_SUBGHZ, meta={"code_type": RISK_ROLLING}),
    ]
    result = apply_filters(records, FilterState(code_types=frozenset({RISK_STATIC})))
    assert [r.bssid for r in result] == ["wifi"]


# --- trackers and addresses ----------------------------------------------

def test_trackers_only():
    records = [_s(), _s(bssid="B", type=TYPE_BLE, meta={"tracker": "Tile"})]
    result = apply_filters(records, FilterState(trackers_only=True))
    assert [r.bssid for r in result] == ["B"]


def test_randomized_only():
    records = [
        _s(meta={"address_type": "public"}),
        _s(bssid="B", meta={"address_type": "random-resolvable"}),
        _s(bssid="C", meta={"address_type": "random-static"}),
    ]
    result = apply_filters(records, FilterState(randomized_only=True))
    assert {r.bssid for r in result} == {"B", "C"}


def test_randomized_only_excludes_records_with_no_address_info():
    assert apply_filters([_s(meta={})], FilterState(randomized_only=True)) == []


# --- location ------------------------------------------------------------

def test_located_only():
    records = [_s(), _s(bssid="B", lat=None, lon=None, geo_source=GEO_NONE)]
    assert len(apply_filters(records, FilterState(located_only=True))) == 1


def test_unlocated_records_are_visible_by_default():
    """They can't be mapped, but they're still real captures and hiding them
    by default loses them entirely."""
    records = [_s(bssid="B", lat=None, lon=None, geo_source=GEO_NONE)]
    assert len(apply_filters(records, FilterState())) == 1


# --- times seen ----------------------------------------------------------

def test_min_times_seen():
    records = [_s(times_seen=1), _s(bssid="B", times_seen=5)]
    result = apply_filters(records, FilterState(min_times_seen=3))
    assert [r.bssid for r in result] == ["B"]


# --- time range ----------------------------------------------------------

def test_time_range_filter():
    records = [
        _s(bssid="early", first_seen="2026-06-14 08:00:00"),
        _s(bssid="mid", first_seen="2026-06-14 09:00:00"),
        _s(bssid="late", first_seen="2026-06-14 10:00:00"),
    ]
    state = FilterState(
        time_from=datetime(2026, 6, 14, 8, 30), time_to=datetime(2026, 6, 14, 9, 30)
    )
    assert [r.bssid for r in apply_filters(records, state)] == ["mid"]


def test_time_range_excludes_records_with_unparseable_times():
    record = _s(first_seen="")
    state = FilterState(time_from=datetime(2026, 1, 1))
    assert apply_filters([record], state) == []


# --- search --------------------------------------------------------------

def test_search_matches_vendor():
    record = _s(vendor="Espressif Inc.")
    assert len(apply_filters([record], FilterState(text="espressif"))) == 1


def test_search_matches_meta_strings():
    """Searching "keeloq" or "airtag" should find things, and those live in
    meta rather than in a named column."""
    record = _s(type=TYPE_SUBGHZ, meta={"protocol": "KeeLoq"})
    assert len(apply_filters([record], FilterState(text="keeloq"))) == 1


def test_search_matches_inside_meta_lists():
    record = _s(type=TYPE_BLE, meta={"services": ["Battery", "Heart Rate"]})
    assert len(apply_filters([record], FilterState(text="heart"))) == 1


def test_search_ignores_non_string_meta_values():
    record = _s(meta={"count": 42, "nested": {"a": 1}})
    assert apply_filters([record], FilterState(text="42")) == []


# --- combinations --------------------------------------------------------

def test_filters_combine():
    records = [
        _s(bssid="A", type=TYPE_SUBGHZ, frequency=433.92, channel=None,
           meta={"code_type": RISK_STATIC}, times_seen=4),
        _s(bssid="B", type=TYPE_SUBGHZ, frequency=433.92, channel=None,
           meta={"code_type": RISK_ROLLING}, times_seen=4),
        _s(bssid="C", type=TYPE_SUBGHZ, frequency=433.92, channel=None,
           meta={"code_type": RISK_STATIC}, times_seen=1),
    ]
    state = FilterState(
        types=frozenset({TYPE_SUBGHZ}),
        bands=frozenset({BAND_SUBGHZ}),
        code_types=frozenset({RISK_STATIC}),
        min_times_seen=2,
    )
    assert [r.bssid for r in apply_filters(records, state)] == ["A"]
