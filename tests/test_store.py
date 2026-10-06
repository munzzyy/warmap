from __future__ import annotations

from warmap.models import AccessPoint
from warmap.store import clear, load, merge_and_save, save


def _ap(**kw) -> AccessPoint:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="Net", auth_mode="", enc_bucket="WPA2",
        first_seen="2024-01-01 00:00:00", channel=6, rssi=-60, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type="WIFI", times_seen=1,
    )
    defaults.update(kw)
    return AccessPoint(**defaults)


def test_load_missing_file_returns_empty(tmp_path):
    assert load(tmp_path / "nope.json") == []


def test_load_corrupt_json_returns_empty_not_raise(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{not valid json[[[", encoding="utf-8")
    assert load(path) == []


def test_load_valid_but_wrong_shape_returns_empty(tmp_path):
    path = tmp_path / "store.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")
    assert load(path) == []


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "store.json"
    aps = [_ap(bssid="A", rssi=-40), _ap(bssid="B", rssi=-90)]
    save(aps, path)
    loaded = load(path)
    assert {ap.bssid for ap in loaded} == {"A", "B"}
    assert next(ap.rssi for ap in loaded if ap.bssid == "A") == -40


def test_save_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "store.json"
    save([_ap()], path)
    assert path.exists()


def test_merge_and_save_first_import(tmp_path):
    path = tmp_path / "store.json"
    new = [_ap(bssid="A"), _ap(bssid="A"), _ap(bssid="B")]
    merged = merge_and_save(new, path)
    by_bssid = {ap.bssid: ap for ap in merged}
    assert by_bssid["A"].times_seen == 2
    assert by_bssid["B"].times_seen == 1
    # a second load from disk must see the same thing
    assert {ap.bssid for ap in load(path)} == {"A", "B"}


def test_merge_and_save_accumulates_across_calls(tmp_path):
    path = tmp_path / "store.json"
    merge_and_save([_ap(bssid="A", rssi=-80)], path)
    merged = merge_and_save([_ap(bssid="A", rssi=-40), _ap(bssid="C")], path)
    by_bssid = {ap.bssid: ap for ap in merged}
    assert by_bssid["A"].times_seen == 2  # 1 from first call + 1 from second
    assert by_bssid["A"].rssi == -40      # strongest across both calls
    assert "C" in by_bssid


def test_clear_removes_the_file(tmp_path):
    path = tmp_path / "store.json"
    save([_ap()], path)
    assert path.exists()
    clear(path)
    assert not path.exists()


def test_clear_missing_file_does_not_raise(tmp_path):
    clear(tmp_path / "nope.json")  # must not raise
