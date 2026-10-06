from __future__ import annotations

from warmap.filters import FilterState, apply_filters
from warmap.models import AccessPoint


def _ap(**kw) -> AccessPoint:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="HomeNet", auth_mode="", enc_bucket="WPA2",
        first_seen="2024-01-01 00:00:00", channel=6, rssi=-60, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type="WIFI", times_seen=1,
    )
    defaults.update(kw)
    return AccessPoint(**defaults)


def test_default_state_keeps_everything():
    aps = [_ap(bssid="A"), _ap(bssid="B", enc_bucket="Open"), _ap(bssid="C", type="BLE")]
    assert len(apply_filters(aps, FilterState())) == 3


def test_text_matches_ssid_case_insensitive():
    aps = [_ap(ssid="CoffeeShopWifi"), _ap(bssid="X", ssid="Other")]
    result = apply_filters(aps, FilterState(text="coffee"))
    assert len(result) == 1
    assert result[0].ssid == "CoffeeShopWifi"


def test_text_matches_bssid():
    aps = [_ap(bssid="AA:BB:CC:DD:EE:99"), _ap(bssid="11:22:33:44:55:66")]
    result = apply_filters(aps, FilterState(text="ee:99"))
    assert len(result) == 1
    assert result[0].bssid == "AA:BB:CC:DD:EE:99"


def test_enc_bucket_filter():
    aps = [_ap(enc_bucket="Open"), _ap(bssid="X", enc_bucket="WPA2")]
    result = apply_filters(aps, FilterState(enc_buckets=frozenset({"Open"})))
    assert len(result) == 1
    assert result[0].enc_bucket == "Open"


def test_open_only_overrides_enc_buckets():
    aps = [_ap(enc_bucket="Open"), _ap(bssid="X", enc_bucket="WPA2")]
    # even with WPA2 explicitly in enc_buckets, open_only wins
    state = FilterState(enc_buckets=frozenset({"Open", "WPA2"}), open_only=True)
    result = apply_filters(aps, state)
    assert len(result) == 1
    assert result[0].enc_bucket == "Open"


def test_min_rssi_filters_weaker_signals():
    aps = [_ap(rssi=-90), _ap(bssid="X", rssi=-40)]
    result = apply_filters(aps, FilterState(min_rssi=-50))
    assert len(result) == 1
    assert result[0].rssi == -40


def test_channel_filter():
    aps = [_ap(channel=1), _ap(bssid="X", channel=11)]
    result = apply_filters(aps, FilterState(channels=frozenset({11})))
    assert len(result) == 1
    assert result[0].channel == 11


def test_channel_filter_none_means_all():
    aps = [_ap(channel=1), _ap(bssid="X", channel=11)]
    assert len(apply_filters(aps, FilterState(channels=None))) == 2


def test_type_filter():
    aps = [_ap(type="WIFI"), _ap(bssid="X", type="BLE")]
    result = apply_filters(aps, FilterState(types=frozenset({"BLE"})))
    assert len(result) == 1
    assert result[0].type == "BLE"


def test_ap_with_none_channel_excluded_by_specific_channel_filter():
    aps = [_ap(channel=None)]
    assert apply_filters(aps, FilterState(channels=frozenset({6}))) == []


def test_combined_filters():
    aps = [
        _ap(bssid="A", ssid="CafeWifi", enc_bucket="Open", rssi=-50, channel=6, type="WIFI"),
        _ap(bssid="B", ssid="CafeWifi", enc_bucket="WPA2", rssi=-50, channel=6, type="WIFI"),
        _ap(bssid="C", ssid="Other", enc_bucket="Open", rssi=-50, channel=6, type="WIFI"),
    ]
    state = FilterState(text="cafe", enc_buckets=frozenset({"Open"}))
    result = apply_filters(aps, state)
    assert [ap.bssid for ap in result] == ["A"]


def test_empty_input():
    assert apply_filters([], FilterState()) == []
