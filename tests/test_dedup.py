"""dedup_by_bssid: strongest RSSI wins, times_seen accumulates. Also
doubles as the merge function for warmap.store, so these tests cover both
"dedup a batch of raw sightings" (each times_seen=1) and "merge new
sightings into an already-accumulated record" (times_seen > 1 going in).
"""

from __future__ import annotations

from dataclasses import replace

from warmap.models import AccessPoint
from warmap.parse import dedup_by_bssid


def _ap(bssid="AA:BB:CC:DD:EE:FF", rssi=-70, times_seen=1, **kw) -> AccessPoint:
    defaults = dict(
        bssid=bssid, ssid="Test", auth_mode="[WPA2-PSK-CCMP][ESS]", enc_bucket="WPA2",
        first_seen="2024-01-01 00:00:00", channel=6, rssi=rssi, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type="WIFI", times_seen=times_seen,
    )
    defaults.update(kw)
    return AccessPoint(**defaults)


def test_single_record_passthrough():
    result = dedup_by_bssid([_ap(rssi=-60)])
    assert len(result) == 1
    assert result[0].rssi == -60
    assert result[0].times_seen == 1


def test_strongest_rssi_wins():
    weak = _ap(rssi=-90, ssid="Weak")
    strong = _ap(rssi=-40, ssid="Strong")
    result = dedup_by_bssid([weak, strong])
    assert len(result) == 1
    assert result[0].rssi == -40
    assert result[0].ssid == "Strong"


def test_times_seen_counts_raw_sightings():
    aps = [_ap(rssi=-70), _ap(rssi=-60), _ap(rssi=-80)]
    result = dedup_by_bssid(aps)
    assert len(result) == 1
    assert result[0].times_seen == 3
    assert result[0].rssi == -60


def test_different_bssids_stay_separate():
    a = _ap(bssid="AA:AA:AA:AA:AA:AA")
    b = _ap(bssid="BB:BB:BB:BB:BB:BB")
    result = dedup_by_bssid([a, b])
    assert {ap.bssid for ap in result} == {"AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"}


def test_merge_semantics_preserve_prior_times_seen():
    """An already-deduped record (times_seen=5 from a prior session) merged
    with 2 fresh raw sightings should end up at times_seen=7, not 3.
    This is exactly what warmap.store.merge_and_save relies on."""
    existing = _ap(rssi=-70, times_seen=5)
    new_1 = _ap(rssi=-65, times_seen=1)
    new_2 = _ap(rssi=-80, times_seen=1)
    result = dedup_by_bssid([existing, new_1, new_2])
    assert len(result) == 1
    assert result[0].times_seen == 7
    assert result[0].rssi == -65  # strongest of the three


def test_empty_input():
    assert dedup_by_bssid([]) == []


def test_output_is_independent_copy_not_same_object():
    original = _ap(rssi=-70, times_seen=1)
    result = dedup_by_bssid([original])
    result[0].rssi = -1
    assert original.rssi == -70


def test_replace_helper_sanity():
    # Confirms AccessPoint is a plain dataclass replace() works against,
    # dedup_by_bssid relies on this shape staying true.
    ap = _ap()
    copy = replace(ap, times_seen=99)
    assert copy.times_seen == 99
    assert ap.times_seen == 1


def test_follow_detection_records_span_and_trail():
    # One BLE identity seen at three spots ~111 m apart: dedup should record how
    # many distinct places it was seen, the span between the farthest two, and a
    # trail of the points for the map to draw.
    pts = [(33.000, -112.0), (33.001, -112.0), (33.002, -112.0)]
    aps = [
        _ap(bssid="BB:BB:BB:BB:BB:BB", type="BLE", ssid="", lat=la, lon=lo)
        for la, lo in pts
    ]
    result = dedup_by_bssid(aps)
    assert len(result) == 1
    meta = result[0].meta
    assert meta["location_count"] == 3
    assert meta["span_m"] > 150  # ~222 m across the three points
    assert len(meta["locations"]) == 3


def test_no_follow_fields_for_a_stationary_device():
    # Two sightings at the same spot (GPS jitter inside one grid cell) is not
    # movement, so no follow fields should be attached.
    aps = [
        _ap(bssid="CC:CC:CC:CC:CC:CC", type="BLE", ssid="", rssi=-70),
        _ap(bssid="CC:CC:CC:CC:CC:CC", type="BLE", ssid="", rssi=-60),
    ]
    result = dedup_by_bssid(aps)
    assert len(result) == 1
    assert "location_count" not in result[0].meta
    assert "span_m" not in result[0].meta
