"""The ALPR/Flock camera overlay: parsing the snapshot, normalizing what the
fetcher pulls from DeFlock/Overpass, the stats rollups, and the "near your
captures" proximity search.

None of this touches the network. The fetch functions take an injected
transport, and the on-disk format is exercised against small fixtures. One
test does load the real bundled snapshot, as a sanity check that what ships
with the app is a plausible camera set and not an empty or truncated file.
"""

from __future__ import annotations

import gzip
import json

import pytest

from warmap import alpr, alpr_fetch, config


# --- direction parsing ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("90", [90.0]),
    ("90.0", [90.0]),
    (90, [90.0]),
    ("0", [0.0]),
    ("90;180;270", [90.0, 180.0, 270.0]),
    ("90,180", [90.0, 180.0]),          # comma treated like semicolon
    ("N", [0.0]),
    ("NE", [45.0]),
    ("south", [180.0]),
    ("W", [270.0]),
    ("", []),
    (None, []),
    ("garbage", []),
    ("360", [0.0]),                     # wraps into [0, 360)
    ("450", [90.0]),
    ("-90", [270.0]),                   # negative wraps
    ("200 deg", [200.0]),               # trailing text tolerated
    ("30; ; 170", [30.0, 170.0]),       # empty middle piece dropped
])
def test_parse_direction(raw, expected):
    assert alpr.parse_direction(raw) == expected


def test_parse_direction_never_raises_on_weird_input():
    for weird in ([1, 2], {"a": 1}, "nan", "inf", ";;;", "..."):
        assert isinstance(alpr.parse_direction(weird), list)


# --- Camera ----------------------------------------------------------------

def _rec(**kw):
    base = {"id": "node/1", "lat": 40.0, "lon": -80.0}
    base.update(kw)
    return base


def _cam(**kw) -> alpr.Camera:
    """A Camera from a valid record. Asserts it parsed, so tests that expect
    a camera don't silently work on None."""
    camera = alpr._camera_from_record(_rec(**kw))
    assert camera is not None
    return camera


def test_is_flock_matches_spelling_variants():
    assert _cam(mfg="Flock Safety").is_flock
    assert _cam(mfg="Flock").is_flock
    assert _cam(brand="flock_safety").is_flock
    assert not _cam(mfg="Motorola Solutions").is_flock
    assert not _cam().is_flock


def test_display_name_prefers_operator_then_manufacturer():
    assert _cam(op="Madison PD", mfg="Flock Safety").display_name == "Madison PD"
    assert _cam(mfg="Genetec").display_name == "Genetec"
    assert _cam().display_name == "ALPR camera"


def test_camera_from_record_rejects_bad_coords():
    assert alpr._camera_from_record(_rec(lat=None)) is None
    assert alpr._camera_from_record(_rec(lat=0.0, lon=0.0)) is None      # 0,0 sentinel
    assert alpr._camera_from_record(_rec(lat=200.0)) is None             # out of range
    assert alpr._camera_from_record(_rec(lat="nan")) is None
    assert alpr._camera_from_record("not a dict") is None


def test_to_feature_shape():
    c = _cam(dir="90;180", mfg="Flock Safety", op="PD", mount="pole")
    f = c.to_feature()
    assert f["geometry"]["coordinates"] == [-80.0, 40.0]
    p = f["properties"]
    assert p["dir"] == [90.0, 180.0]
    assert p["mfg"] == "Flock Safety"
    assert p["flock"] is True
    assert p["mount"] == "pole"


# --- snapshot loading -----------------------------------------------------

def _write_snapshot(path, cameras, **meta):
    payload = {"cameras": cameras}
    payload.update(meta)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_snapshot_wrapped(tmp_path):
    p = tmp_path / "s.json"
    _write_snapshot(p, [_rec(id="node/1"), _rec(id="node/2", lat=41.0)],
                    attribution="test attrib", source="src", generated="2026-08-30")
    snap = alpr.load_snapshot(p)
    assert len(snap) == 2
    assert snap.attribution == "test attrib"
    assert snap.generated == "2026-08-30"


def test_load_snapshot_bare_list(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps([_rec(id="node/1")]), encoding="utf-8")
    snap = alpr.load_snapshot(p)
    assert len(snap) == 1
    assert snap.attribution == alpr.DEFAULT_ATTRIBUTION


def test_load_snapshot_skips_bad_records_keeps_good(tmp_path):
    p = tmp_path / "s.json"
    _write_snapshot(p, [
        _rec(id="node/1"),
        {"id": "node/2"},                 # no coords
        "not a dict",
        _rec(id="node/3", lat=0.0, lon=0.0),  # 0,0 sentinel
        _rec(id="node/4", lat=41.0),
    ])
    snap = alpr.load_snapshot(p)
    assert len(snap) == 2  # node/1 and node/4 only


def test_load_snapshot_fails_soft(tmp_path):
    assert len(alpr.load_snapshot(tmp_path / "missing.json")) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert len(alpr.load_snapshot(bad)) == 0
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"cameras": "not a list"}), encoding="utf-8")
    assert len(alpr.load_snapshot(wrong)) == 0


def test_load_snapshot_oversized_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "big.json"
    _write_snapshot(p, [_rec()])
    monkeypatch.setattr(alpr, "MAX_FILE_BYTES", 5)  # smaller than the file
    assert len(alpr.load_snapshot(p)) == 0


# --- stats + geojson ------------------------------------------------------

def test_camera_stats():
    cams = [
        _cam(id="n/1", mfg="Flock Safety", op="PD A", dir="90"),
        _cam(id="n/2", mfg="Flock Safety", op="PD A"),
        _cam(id="n/3", mfg="Genetec", dir="180"),
    ]
    st = alpr.camera_stats(cams, near=1)
    assert st.total == 3
    assert st.flock_count == 2
    assert st.with_direction == 2
    assert st.near_captures == 1
    assert st.top_manufacturers[0] == ("Flock Safety", 2)
    assert ("PD A", 2) in st.top_operators


def test_to_geojson_feature_collection():
    cams = [_cam()]
    gj = alpr.to_geojson(cams)
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) == 1


# --- proximity ------------------------------------------------------------

def test_cameras_near_grid_matches_brute_force():
    from warmap.gps import haversine_km
    # a small spread of cameras
    cams = [_cam(id=f"n/{i}", lat=40.0 + i * 0.001, lon=-80.0 + i * 0.001)
            for i in range(50)]
    points = [(40.0, -80.0), (40.02, -79.98)]

    def brute(radius_km):
        out = []
        for c in cams:
            if any(haversine_km(c.lat, c.lon, la, lo) <= radius_km for la, lo in points):
                out.append(c)
        return out

    for meters in (100, 300, 1000):
        grid = alpr.cameras_near(cams, points, meters=meters)
        bf = brute(meters / 1000.0)
        assert {c.osm_id for c in grid} == {c.osm_id for c in bf}, meters


def test_cameras_near_edges():
    cams = [_cam()]
    assert alpr.cameras_near(cams, []) == []
    assert alpr.cameras_near(cams, [(40.0, -80.0)], meters=0) == []
    assert len(alpr.cameras_near(cams, [(40.0, -80.0)], meters=100)) == 1


# --- fetch: DeFlock normalization -----------------------------------------

def _deflock_feature(**props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [props.pop("lon", -80.0), props.pop("lat", 40.0)]},
        "properties": props,
    }


def test_build_from_deflock_basic():
    gj = {"features": [
        _deflock_feature(osmId=1, osmType="node", brand="Flock Safety",
                         operator="PD", direction="200", surveillanceZone="traffic",
                         mountType="street_lamp"),
        _deflock_feature(osmId=2, osmType="node", brand="Genetec", directions=[0, 90]),
    ]}
    recs, dropped = alpr_fetch.build_records_from_deflock(gj)
    assert dropped == 0
    assert len(recs) == 2
    assert recs[0]["id"] == "node/1"
    assert recs[0]["dir"] == "200"
    assert recs[0]["mfg"] == "Flock Safety"
    assert recs[0]["mount"] == "street_lamp"
    assert recs[1]["dir"] == "0;90"  # directions array joined


def test_build_from_deflock_drops_highway_pollution():
    gj = {"features": [
        _deflock_feature(osmId=9, osmType="way", brand="Flock Safety",
                         ref="US 29;GA 8", startDate="1923"),
        _deflock_feature(osmId=10, osmType="node", brand="Flock Safety", direction="90"),
    ]}
    recs, dropped = alpr_fetch.build_records_from_deflock(gj)
    assert dropped == 1
    assert len(recs) == 1
    assert recs[0]["id"] == "node/10"


def test_build_from_deflock_dedups_by_id():
    gj = {"features": [
        _deflock_feature(osmId=1, osmType="node", brand="Flock Safety"),
        _deflock_feature(osmId=1, osmType="node", brand="Flock Safety"),
    ]}
    recs, _ = alpr_fetch.build_records_from_deflock(gj)
    assert len(recs) == 1


def test_build_from_deflock_skips_missing_geometry():
    gj = {"features": [
        {"type": "Feature", "geometry": None, "properties": {"osmId": 1}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
         "properties": {"osmId": 2}},  # 0,0 sentinel
        _deflock_feature(osmId=3, brand="Flock Safety"),
    ]}
    recs, _ = alpr_fetch.build_records_from_deflock(gj)
    assert [r["id"] for r in recs] == ["node/3"]


# --- fetch: Overpass normalization ----------------------------------------

def test_build_from_overpass():
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": 40.0, "lon": -80.0,
         "tags": {"manufacturer": "Flock Safety", "direction": "90",
                  "surveillance:zone": "traffic", "camera:mount": "pole"}},
        {"type": "way", "id": 2, "center": {"lat": 41.0, "lon": -81.0},
         "tags": {"manufacturer": "Genetec"}},
    ]}
    recs = alpr_fetch.build_records_from_overpass(payload)
    assert len(recs) == 2
    assert recs[0]["id"] == "node/1"
    assert recs[0]["dir"] == "90"
    assert recs[0]["mount"] == "pole"
    assert recs[1]["id"] == "way/2"
    assert recs[1]["lat"] == 41.0


def test_build_from_overpass_requires_elements():
    with pytest.raises(alpr_fetch.FetchError):
        alpr_fetch.build_records_from_overpass({"nope": []})


# --- fetch: transport + fallback ------------------------------------------

def test_fetch_from_deflock_with_injected_transport():
    us = {"features": [_deflock_feature(osmId=i, osmType="node", brand="Flock Safety",
                                        lat=40.0 + i * 0.001) for i in range(1500)]}

    def fake_get(url):
        if "cameras-ca" in url:
            return b'{"features": []}'
        return json.dumps(us).encode("utf-8")

    result = alpr_fetch.fetch_from_deflock(http_get=fake_get)
    assert result.count == 1500
    assert "DeFlock" in result.source_label


def test_fetch_from_deflock_refuses_tiny_result():
    def fake_get(url):
        return b'{"features": []}'
    with pytest.raises(alpr_fetch.FetchError):
        alpr_fetch.fetch_from_deflock(http_get=fake_get)


def test_decode_body_handles_gzip_and_plain():
    plain = b'{"features": []}'
    assert alpr_fetch._decode_body(plain) == '{"features": []}'
    assert alpr_fetch._decode_body(gzip.compress(plain)) == '{"features": []}'


def test_fetch_cameras_falls_back_to_overpass(monkeypatch):
    """When DeFlock's export is unusable, fetch_cameras falls through to
    Overpass. Both sources are stubbed so no network is touched, and note
    the Overpass path is stubbed at the function level, not via _http_post,
    because fetch_from_overpass binds its transport as a default argument."""
    def boom():
        raise alpr_fetch.FetchError("deflock down")

    payload = {"elements": [
        {"type": "node", "id": i, "lat": 40.0 + i * 0.001, "lon": -80.0,
         "tags": {"manufacturer": "Flock Safety"}} for i in range(1500)]}

    def fake_overpass():
        cams = alpr_fetch.build_records_from_overpass(payload)
        return alpr_fetch.FetchResult(
            cameras=cams, source_url="stub",
            source_label="OpenStreetMap via Overpass (worldwide)", count=len(cams))

    monkeypatch.setattr(alpr_fetch, "fetch_from_deflock", boom)
    monkeypatch.setattr(alpr_fetch, "fetch_from_overpass", fake_overpass)
    result = alpr_fetch.fetch_cameras()
    assert result.count == 1500
    assert "Overpass" in result.source_label


# --- write + round-trip ---------------------------------------------------

def test_write_snapshot_round_trips(tmp_path):
    recs = [_rec(id="node/1", mfg="Flock Safety", dir="90")]
    result = alpr_fetch.FetchResult(cameras=recs, source_url="u",
                                    source_label="test", count=1)
    out = tmp_path / "snap.json"
    size = alpr_fetch.write_snapshot(result, out, generated="2026-08-30")
    assert size > 0
    snap = alpr.load_snapshot(out)
    assert len(snap) == 1
    assert snap.generated == "2026-08-30"
    assert snap.cameras[0].is_flock


# --- the real bundled snapshot --------------------------------------------

def test_bundled_snapshot_is_a_plausible_camera_set():
    """What ships with the app must be a real camera set, not an empty or
    truncated file. This deliberately loads the full bundled snapshot."""
    snap = alpr.load_snapshot(config.FLOCK_BUNDLED_PATH)
    assert len(snap) > 50_000, "bundled camera snapshot looks truncated/empty"
    st = alpr.camera_stats(snap.cameras)
    assert st.flock_count > 10_000
    # Almost every camera should carry a facing.
    assert st.with_direction > len(snap) * 0.5
    assert "OpenStreetMap" in snap.attribution


# --- review fixes ----------------------------------------------------------

def test_cameras_near_wraps_the_antimeridian():
    """A camera at lon +179.9995 and a point at lon -179.9995 are ~70 m apart,
    so the grid must treat those longitude cells as neighbours even though their
    raw indices are a hemisphere apart."""
    from warmap.gps import haversine_km
    cam = _cam(id="n/1", lat=52.0, lon=179.9995)
    point = (52.0, -179.9995)
    assert haversine_km(cam.lat, cam.lon, *point) < 0.5  # within 500 m
    near = alpr.cameras_near([cam], [point], meters=500)
    assert len(near) == 1


def test_looks_polluted_keeps_a_node_with_an_install_year():
    # A real camera node can carry an OSM start_date (install year). Only ways
    # are dropped, so this node survives.
    gj = {"features": [
        _deflock_feature(osmId=1, osmType="node", brand="Flock Safety",
                         direction="90", startDate="2023"),
    ]}
    recs, dropped = alpr_fetch.build_records_from_deflock(gj)
    assert dropped == 0
    assert len(recs) == 1


def test_decode_body_refuses_a_gzip_bomb(monkeypatch):
    # A tiny compressed payload that expands past the cap must be refused, not
    # decompressed into memory.
    monkeypatch.setattr(alpr_fetch, "MAX_RESPONSE_BYTES", 1024 * 1024)  # 1 MB
    bomb = gzip.compress(b"\x00" * (8 * 1024 * 1024))  # 8 MB of zeros
    assert len(bomb) < 100_000  # compresses tiny
    with pytest.raises(alpr_fetch.FetchError):
        alpr_fetch._decode_body(bomb)


def test_fetch_from_deflock_partial_success_is_honest():
    """If the Canada file fails but the US one loads, the result must say so:
    coverage names only US, and the CA error is carried, not swallowed."""
    us = {"features": [_deflock_feature(osmId=i, osmType="node", brand="Flock Safety",
                                        lat=40.0 + i * 0.001) for i in range(1500)]}

    def fake_get(url):
        if "cameras-ca" in url:
            raise OSError("503 from the CA file")
        return json.dumps(us).encode("utf-8")

    result = alpr_fetch.fetch_from_deflock(http_get=fake_get)
    assert result.count == 1500
    assert result.source_label.endswith("US")       # not "US + Canada"
    assert any("CA" in e for e in result.errors)


def test_write_snapshot_regression_guard(tmp_path):
    out = tmp_path / "snap.json"
    big = alpr_fetch.FetchResult(
        cameras=[_rec(id=f"node/{i}", lat=40.0 + i * 0.0001) for i in range(2000)],
        source_url="u", source_label="test", count=2000)
    alpr_fetch.write_snapshot(big, out)
    # A refresh that collapses to a fraction of the size is refused, unless forced.
    small = alpr_fetch.FetchResult(cameras=[_rec()], source_url="u", source_label="test", count=1)
    with pytest.raises(alpr_fetch.FetchError):
        alpr_fetch.write_snapshot(small, out)
    alpr_fetch.write_snapshot(small, out, force=True)
    assert len(alpr.load_snapshot(out)) == 1
