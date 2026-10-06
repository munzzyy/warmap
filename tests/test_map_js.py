"""Execute the real map.js and assert on what it drew.

Everything else in this suite tests Python. map.js is 600 lines that no test
ever ran, and the failure mode is brutal: one bad identifier throws inside the
loop that builds the markers, every remaining record is skipped, and the app
shows an empty map with no error anywhere. That shipped once: a follower
record hit a `coords` that didn't exist in that scope, and a capture with
1,885 located records drew nothing at all.

So these run map.js under node against a recording stub (tests/js/) and check
the markers, popups, legend and layers it actually produced. The stub is not
Leaflet and doesn't try to be; it verifies map.js's own logic, and the real
rendering is checked by eye with tools/shot.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from warmap import config

NODE = shutil.which("node")
RUNNER = config.REPO_ROOT / "tests" / "js" / "run_map.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_map(features, track=None, actions=None, tmp_path=None, viewport=None,
            alpr=None, view_bounds=None):
    """Run map.js over a FeatureCollection and return its report."""
    scenario = {
        "features": {"type": "FeatureCollection", "features": features},
        "actions": actions or [],
    }
    if track is not None:
        scenario["track"] = track
    if viewport is not None:
        scenario["viewport"] = list(viewport)
    if alpr is not None:
        scenario["alpr"] = {"type": "FeatureCollection", "features": alpr}
    if view_bounds is not None:
        scenario["viewBounds"] = list(view_bounds)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(RUNNER), str(path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"runner crashed: {proc.stderr}"
    return json.loads(proc.stdout)


def feature(bssid="AA:BB:CC:DD:EE:FF", type_="WIFI", lat=33.44, lon=-112.07, **props):
    p = {
        "bssid": bssid, "ssid": "", "type": type_, "auth_mode": "", "enc_bucket": "WPA2",
        "first_seen": "2026-07-26 01:14:04", "channel": 6, "rssi": -60,
        "altitude": None, "accuracy": None, "times_seen": 1, "frequency": None,
        "vendor": "", "source": "/media/cole/BFFB/wardrive_0.log",
        "geo_source": "direct", "meta": {},
    }
    p.update(props)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": p,
    }


def assert_clean(report):
    assert report["errors"] == [], f"map.js threw: {report['errors']}"
    assert report["ok"] is True


def test_map_js_loads_without_throwing(tmp_path):
    report = run_map([], tmp_path=tmp_path)
    assert_clean(report)
    assert sorted(report["apiPresent"]) == sorted([
        "warmapLoadData", "warmapSetTrack", "warmapSetHeatmapVisible",
        "warmapSetTypeVisible", "warmapFocus", "warmapFitToData",
    ])


def test_every_located_record_becomes_a_marker(tmp_path):
    features = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}", lat=33.44 + i / 1000)
                for i in range(25)]
    report = run_map(features, tmp_path=tmp_path)
    assert_clean(report)
    assert report["markerCount"] == 25
    assert sum(a["count"] for a in report["layerAdds"]) == 25


def test_a_follower_does_not_abort_the_whole_load(tmp_path):
    """The regression that shipped: a device seen across 150 m+ of the route
    made warmapLoadData throw part-way through, so none of the records that
    came after it, and none of the layer adds, which happen at the end, ever
    reached the map. Every marker must survive a follower in the feed."""
    follower = feature(
        bssid="11:22:33:44:55:66", type_="BLE",
        meta={"location_count": 3, "span_m": 331.9,
              "locations": [[33.4477, -112.0781], [33.4484, -112.0740], [33.4490, -112.0712]]},
    )
    features = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}") for i in range(5)]
    features.insert(2, follower)

    report = run_map(features, tmp_path=tmp_path)
    assert_clean(report)
    # 6 record markers plus the follower's ring. The trail is not drawn until
    # its popup is opened.
    assert report["markerCount"] == 6 + 1
    assert sum(a["count"] for a in report["layerAdds"]) == 6, "records never reached a layer"
    assert report["followerRingCount"] == 1
    assert report["followerTrailCount"] == 0
    assert "Possible follower" in "".join(p or "" for p in report["popups"])


def test_a_follower_trail_is_drawn_only_while_its_popup_is_open(tmp_path):
    """47 followers along one road meant 47 overlapping dashed lines drawn on
    top of each other. One trail at a time, belonging to whatever you clicked."""
    follower = feature(
        bssid="11:22:33:44:55:66", type_="BLE",
        meta={"location_count": 3, "span_m": 331.9,
              "locations": [[33.4477, -112.0781], [33.4484, -112.0740], [33.4490, -112.0712]]},
    )
    idle = run_map([follower], tmp_path=tmp_path)
    assert_clean(idle)
    assert idle["followerTrailCount"] == 0

    opened = run_map([follower],
                     actions=[["focus", "BLE", "11:22:33:44:55:66"]], tmp_path=tmp_path)
    assert_clean(opened)
    assert opened["followerTrailCount"] == 1
    # the line plus a dot at each spot it was seen
    assert opened["markerCount"] == 1 + 1 + 3
    assert opened["liveTrailShapeCount"] == 4

    closed = run_map([follower],
                     actions=[["focus", "BLE", "11:22:33:44:55:66"], ["closePopups"]],
                     tmp_path=tmp_path)
    assert_clean(closed)
    assert closed["liveTrailShapeCount"] == 0, "the trail outlived its popup"


def test_wifi_that_moved_is_not_called_a_follower(tmp_path):
    """A Wi-Fi AP that appears to move is a mobile hotspot, not a stalker.
    Only radios a person can carry get the warning ring."""
    features = [feature(type_="WIFI", meta={"location_count": 2, "span_m": 900.0,
                                            "locations": [[33.44, -112.07], [33.45, -112.08]]})]
    report = run_map(features, tmp_path=tmp_path)
    assert_clean(report)
    assert report["followerRingCount"] == 0


def test_popup_and_tooltip_escape_a_hostile_name(tmp_path):
    """An SSID is attacker-controlled text off the air. It lands in innerHTML
    via the popup, so it has to be escaped, not interpolated."""
    nasty = "<img src=x onerror=alert(1)>\"'&"
    report = run_map([feature(ssid=nasty)], tmp_path=tmp_path)
    assert_clean(report)
    popup = report["popups"][0]
    assert "<img src=x" not in popup
    assert "&lt;img src=x onerror=alert(1)&gt;" in popup
    assert "&quot;" in popup and "&#39;" in popup and "&amp;" in popup


def test_hostile_meta_key_is_escaped_too(tmp_path):
    """meta is parsed out of capture files, so its keys and values are just as
    untrusted as an SSID."""
    report = run_map(
        [feature(meta={"<b>k</b>": "<script>x</script>"})], tmp_path=tmp_path)
    assert_clean(report)
    popup = report["popups"][0]
    assert "<script>" not in popup
    assert "&lt;script&gt;" in popup


def test_glyph_types_render_as_glyph_markers(tmp_path):
    features = [feature(bssid="sub", type_="SUBGHZ", meta={"code_type": "static"}),
                feature(bssid="nfc", type_="NFC"),
                feature(bssid="ir", type_="IR")]
    report = run_map(features, tmp_path=tmp_path)
    assert_clean(report)
    assert report["markersByKind"].get("marker") == 3
    assert report["markersByKind"].get("circleMarker") is None


def test_inferred_position_is_drawn_dashed(tmp_path):
    """A position matched from the GPS track is an inference, and the marker
    has to say so without opening the popup."""
    report = run_map([feature(geo_source="track")], tmp_path=tmp_path)
    assert_clean(report)
    options = report["markerOptions"][0]
    assert options.get("dashArray") == "3,3"
    assert options.get("fillOpacity") == 0.45
    assert "Legend" in report["legendHtml"]
    assert "Inferred from GPS track" in report["legendHtml"]


def test_recorded_position_is_drawn_solid(tmp_path):
    report = run_map([feature(geo_source="direct")], tmp_path=tmp_path)
    assert_clean(report)
    assert report["markerOptions"][0].get("dashArray") is None
    assert "Inferred from GPS track" not in report["legendHtml"]


def test_signal_strength_sizes_the_dot(tmp_path):
    report = run_map([feature(bssid="a", rssi=-40), feature(bssid="b", rssi=-95),
                      feature(bssid="c", rssi=None)], tmp_path=tmp_path)
    assert_clean(report)
    strong, weak, unknown = [o["radius"] for o in report["markerOptions"]]
    assert strong > unknown > weak


def test_focus_opens_that_record_and_nothing_else(tmp_path):
    features = [feature(bssid="AA:BB:CC:00:00:01"), feature(bssid="AA:BB:CC:00:00:02")]
    report = run_map(features, actions=[["focus", "WIFI", "AA:BB:CC:00:00:02"]],
                     tmp_path=tmp_path)
    assert_clean(report)
    assert report["openedPopupCount"] == 1


def test_focus_on_a_record_that_is_not_there_is_survivable(tmp_path):
    report = run_map([feature()], actions=[["focus", "WIFI", "no:such:mac"]],
                     tmp_path=tmp_path)
    assert_clean(report)
    assert report["openedPopupCount"] == 0


def test_fit_to_data_covers_every_point(tmp_path):
    features = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}", lat=43.0 + i) for i in range(4)]
    report = run_map(features, actions=[["fit"]], tmp_path=tmp_path)
    assert_clean(report)
    assert report["fitBounds"] == 4


def test_fit_to_data_with_nothing_loaded_does_not_throw(tmp_path):
    report = run_map([], actions=[["fit"]], tmp_path=tmp_path)
    assert_clean(report)
    assert report["fitBounds"] is None


def test_track_is_drawn_with_start_and_end(tmp_path):
    track = {
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[-112.07, 33.44], [-112.08, 33.45], [-112.09, 33.46]]},
        "properties": {"start": "01:00", "end": "01:30", "distance_km": 2.1,
                       "derived": True},
    }
    report = run_map([feature()], track=track, tmp_path=tmp_path)
    assert_clean(report)
    assert report["trackPolylineCount"] == 1
    assert report["trackEndpointCount"] == 2  # where the drive started and ended


def test_clearing_the_track_does_not_throw(tmp_path):
    report = run_map([feature()], track=None, tmp_path=tmp_path)
    assert_clean(report)
    assert report["trackPolylineCount"] == 0


def test_a_track_with_one_point_is_skipped_not_crashed(tmp_path):
    """A session that only ever got one fix is a real thing off a cold board."""
    track = {"type": "Feature",
             "geometry": {"type": "LineString", "coordinates": [[-112.07, 33.44]]},
             "properties": {}}
    report = run_map([feature()], track=track, tmp_path=tmp_path)
    assert_clean(report)
    assert report["trackPolylineCount"] == 0


def test_heatmap_gets_a_point_per_record(tmp_path):
    features = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}") for i in range(7)]
    report = run_map(features, actions=[["heatmap", True]], tmp_path=tmp_path)
    assert_clean(report)
    assert report["heatPointCount"] == 7


def test_reloading_replaces_rather_than_stacks(tmp_path):
    """Import is additive, so warmapLoadData is called again with the full set
    every time. The second call must not leave the first call's markers
    behind."""
    first = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}") for i in range(3)]
    second = {"type": "FeatureCollection",
              "features": [feature(bssid="DD:EE:FF:00:00:01")]}
    report = run_map(first, actions=[["reload", second]], tmp_path=tmp_path)
    assert_clean(report)
    adds = [a["count"] for a in report["layerAdds"]]
    assert adds == [3, 1]
    assert report["heatPointCount"] == 1


def test_an_unknown_record_type_does_not_break_the_map(tmp_path):
    """A capture format warmap learns to read later, opened by an older build,
    must not blank the map."""
    report = run_map([feature(type_="QUANTUM"), feature(bssid="b")], tmp_path=tmp_path)
    assert_clean(report)
    assert report["markerCount"] == 2


def test_a_record_with_no_usable_coordinate_is_left_off_not_placed_at_null_island(tmp_path):
    """A missing or non-numeric coordinate must cost that one record, not the
    whole map, and it must not be drawn at [0, 0], since that's a real place."""
    broken = [
        {"type": "Feature", "properties": feature()["properties"]},
        feature(bssid="nan", lat=float("nan")),
        feature(bssid="oob", lat=91.5),
    ]
    report = run_map(broken + [feature(bssid="good")], tmp_path=tmp_path)
    assert_clean(report)
    assert report["markerCount"] == 1
    assert sum(a["count"] for a in report["layerAdds"]) == 1
    assert report["heatPointCount"] == 1


def test_legend_lists_only_what_is_on_the_map(tmp_path):
    report = run_map([feature(enc_bucket="Open"), feature(bssid="b", enc_bucket="Open")],
                     tmp_path=tmp_path)
    assert_clean(report)
    legend = report["legendHtml"]
    assert "Open" in legend
    assert "WEP" not in legend
    assert "Sub-GHz" not in legend


def test_legend_starts_collapsed_when_the_map_is_too_small_to_hold_it(tmp_path):
    """In a 1000x640 window the map area is about 340x320, and the expanded
    legend covered most of it."""
    small = run_map([feature()], viewport=(340, 320), tmp_path=tmp_path)
    assert_clean(small)
    assert "Wi-Fi encryption" not in small["legendHtml"]
    assert "Legend" in small["legendHtml"]

    roomy = run_map([feature()], viewport=(1200, 900), tmp_path=tmp_path)
    assert_clean(roomy)
    assert "Wi-Fi encryption" in roomy["legendHtml"]


def test_layer_control_counts_match_the_records(tmp_path):
    features = ([feature(bssid=f"AA:{i:02X}") for i in range(3)]
                + [feature(bssid=f"BB:{i:02X}", type_="BLE") for i in range(2)])
    report = run_map(features, tmp_path=tmp_path)
    assert_clean(report)
    assert report["layerControlOverlays"] == ["Wi-Fi (3)", "Bluetooth LE (2)"]


def test_type_visibility_toggle_does_not_throw(tmp_path):
    report = run_map([feature()],
                     actions=[["typeVisible", "WIFI", False], ["typeVisible", "WIFI", True],
                              ["typeVisible", "NOPE", False]],
                     tmp_path=tmp_path)
    assert_clean(report)


def test_popup_shows_coordinates_at_full_precision(tmp_path):
    """A wardrive fix is worth about a metre; truncating it in the popup makes
    warmap look less accurate than the capture it read."""
    report = run_map([feature(lat=33.4484123, lon=-112.0740456)], tmp_path=tmp_path)
    assert_clean(report)
    assert "33.4484123, -112.0740456" in report["popups"][0]


def test_a_hidden_network_is_labelled_not_blank(tmp_path):
    report = run_map([feature(ssid="")], tmp_path=tmp_path)
    assert_clean(report)
    assert "(hidden)" in report["popups"][0]
    assert report["tooltips"][0]


def test_realistic_mixed_capture_runs_clean(tmp_path):
    """One of everything, the shape a real card produces."""
    features = [
        feature(bssid="AA:00", ssid="home", enc_bucket="WPA2"),
        feature(bssid="AA:01", ssid="", enc_bucket="Open", rssi=-88),
        feature(bssid="BB:00", type_="BLE", meta={"tracker": "AirTag", "span_m": 400.0,
                                                  "location_count": 2,
                                                  "locations": [[33.44, -112.07], [33.45, -112.08]]}),
        feature(bssid="CC:00", type_="BT"),
        feature(bssid="DD:00", type_="CLIENT", meta={"probing_for": ["home", "cafe"]}),
        feature(bssid="EE:00", type_="CELL", meta={"cell_technology": "LTE"}),
        feature(bssid="sub-1", type_="SUBGHZ", geo_source="track",
                meta={"code_type": "static", "code_type_label": "Fixed"}),
        feature(bssid="nfc-1", type_="NFC", geo_source="track"),
        feature(bssid="rfid-1", type_="RFID"),
        feature(bssid="ib-1", type_="IBUTTON"),
        feature(bssid="ir-1", type_="IR"),
    ]
    report = run_map(features, actions=[["fit"], ["heatmap", True]], tmp_path=tmp_path)
    assert_clean(report)
    assert report["heatPointCount"] == len(features)
    assert report["followerRingCount"] == 1
    assert "Inferred from GPS track" in report["legendHtml"]
    # Every record marker is hoverable. The follower ring and its trail dots
    # are decoration and deliberately carry no tooltip.
    assert len([t for t in report["tooltips"] if t]) == len(features)


# --- ALPR / Flock camera overlay -----------------------------------------
# The overlay is a separate layer fed by warmapSetAlpr. These run it under the
# same recording stub and check what it drew: a marker per in-view camera, a
# direction wedge per known facing, viewport culling, the visibility toggle,
# focus, the legend and the layer control.

def alpr_feature(id_="node/1", lat=33.5, lon=-112.0, dirs=None, mfg="Flock Safety",
                 op="", flock=True, **props):
    p = {"id": id_, "dir": dirs if dirs is not None else [], "mfg": mfg,
         "op": op, "mount": "", "zone": "traffic", "brand": "", "ref": "",
         "flock": flock}
    p.update(props)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": p}


def test_alpr_cameras_render_with_wedges(tmp_path):
    cams = [
        alpr_feature("node/1", dirs=[200]),           # one facing -> one wedge
        alpr_feature("node/2", dirs=[0, 90], mfg="Genetec", flock=False),  # two wedges
        alpr_feature("node/3", dirs=[]),              # no facing -> no wedge
    ]
    report = run_map([], alpr=cams, tmp_path=tmp_path)
    assert_clean(report)
    assert report["alprMarkerCount"] == 3
    assert sorted(report["alprWedgeCounts"]) == [0, 1, 2]


def test_alpr_popup_carries_attribution_and_details(tmp_path):
    cams = [alpr_feature("node/9", dirs=[200], mfg="Flock Safety", op="Austin PD")]
    report = run_map([], alpr=cams, tmp_path=tmp_path)
    assert_clean(report)
    html = report["alprPopups"][0]
    assert "Austin PD" in html
    assert "Flock Safety" in html
    assert "OpenStreetMap" in html          # ODbL attribution is mandatory
    assert "200" in html                    # facing shown


def test_alpr_focus_opens_the_camera_popup(tmp_path):
    cams = [alpr_feature("node/1"), alpr_feature("node/2", lat=33.6)]
    report = run_map([], alpr=cams, tmp_path=tmp_path,
                     actions=[["focus", "ALPR", "node/2"]])
    assert_clean(report)
    assert report["openedPopupCount"] == 1


def test_alpr_visibility_toggle_clears_and_restores(tmp_path):
    cams = [alpr_feature("node/1"), alpr_feature("node/2", lat=33.6)]
    off = run_map([], alpr=cams, tmp_path=tmp_path,
                  actions=[["setAlprVisible", False]])
    assert_clean(off)
    # After hiding, no camera markers survive on the layer.
    assert off["alprMarkerCount"] == 0

    back = run_map([], alpr=cams, tmp_path=tmp_path,
                   actions=[["setAlprVisible", False], ["setAlprVisible", True]])
    assert_clean(back)
    assert back["alprMarkerCount"] == 2


def test_alpr_viewport_culls_offscreen_cameras(tmp_path):
    # Two cameras far apart; only the one inside the view box is drawn.
    cams = [alpr_feature("node/near", lat=33.50, lon=-112.00),
            alpr_feature("node/far", lat=40.00, lon=-80.00)]
    report = run_map([], alpr=cams, tmp_path=tmp_path,
                     view_bounds=[33.4, -112.1, 33.6, -111.9])
    assert_clean(report)
    assert report["alprMarkerCount"] == 1


def test_moveend_rerenders_for_the_new_view(tmp_path):
    cams = [alpr_feature("node/a", lat=33.50, lon=-112.00),
            alpr_feature("node/b", lat=40.00, lon=-80.00)]
    # Start looking at camera a, then pan to camera b and fire moveend.
    report = run_map([], alpr=cams, tmp_path=tmp_path,
                     view_bounds=[33.4, -112.1, 33.6, -111.9],
                     actions=[["setViewBounds", [39.9, -80.1, 40.1, -79.9]], ["moveend"]])
    assert_clean(report)
    # After the pan, only camera b is in view.
    assert report["alprMarkerCount"] == 1
    assert "node/b" in (report["alprPopups"][0] or "")


def test_alpr_layer_control_lists_the_cameras(tmp_path):
    captures = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}") for i in range(3)]
    cams = [alpr_feature("node/1"), alpr_feature("node/2", lat=33.6)]
    report = run_map(captures, alpr=cams, tmp_path=tmp_path)
    assert_clean(report)
    assert any(k.startswith("ALPR cameras (2)") for k in report["layerControlOverlays"])


def test_alpr_cameras_are_not_included_in_fit_to_data(tmp_path):
    # fit-to-data must frame the captures, never the whole camera database.
    captures = [feature(bssid=f"AA:BB:CC:00:00:{i:02X}", lat=33.44 + i / 1000)
                for i in range(4)]
    cams = [alpr_feature("node/1", lat=33.5), alpr_feature("node/2", lat=40.0)]
    report = run_map(captures, alpr=cams, tmp_path=tmp_path, actions=[["fit"]])
    assert_clean(report)
    assert report["fitBounds"] == 4  # the 4 captures, not the cameras


def test_a_bad_camera_coordinate_does_not_abort_the_overlay(tmp_path):
    cams = [
        alpr_feature("node/1"),
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [float("nan"), 33.5]},
         "properties": {"id": "node/bad", "dir": [], "flock": True}},
        alpr_feature("node/3", lat=33.55),
    ]
    report = run_map([], alpr=cams, tmp_path=tmp_path)
    assert_clean(report)
    assert report["alprMarkerCount"] == 2  # the good two, bad one skipped


def test_alpr_tooltip_escapes_a_hostile_operator(tmp_path):
    """Leaflet sets a string tooltip via innerHTML, so an operator name off
    OpenStreetMap is an XSS sink unless escaped - the same guard the popup has."""
    nasty = "<img src=x onerror=alert(1)>"
    cams = [alpr_feature("node/1", op=nasty, mfg="")]
    report = run_map([], alpr=cams, tmp_path=tmp_path)
    assert_clean(report)
    tip = report["alprTooltips"][0]
    assert "<img src=x" not in tip
    assert "&lt;img src=x" in tip


def test_capture_tooltip_escapes_a_hostile_name(tmp_path):
    """Same innerHTML sink for a capture's own tooltip label (an SSID off the air)."""
    report = run_map([feature(ssid="<img src=x onerror=alert(1)>")], tmp_path=tmp_path)
    assert_clean(report)
    tip = next(t for t in report["tooltips"] if t)
    assert "<img src=x" not in tip
    assert "&lt;img" in tip
