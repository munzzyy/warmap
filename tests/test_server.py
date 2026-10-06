"""The phone bridge: the LAN server and the single-file offline export.

The security properties get the most attention here, because this is the only
part of warmap that accepts a connection from another machine and the whole
design rests on four claims: the token gates everything, nothing outside the
app's own directories can be read, nothing can be written, and the server only
exists while something explicitly runs it.

Every test binds to loopback on an ephemeral port, so running the suite never
puts anything on the network.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

import pytest

from warmap import alpr, config, export, offline, server
from warmap.models import Sighting


def _sighting(**kw):
    base = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="TestNet", auth_mode="[WPA2]",
        enc_bucket="WPA2", first_seen="2026-08-30 10:00:00", channel=6,
        rssi=-55, lat=33.45, lon=-112.07, altitude=None, accuracy=None,
        type="WIFI",
    )
    base.update(kw)
    return Sighting(**base)


def _cameras(n=3):
    return [
        alpr.Camera(osm_id=f"node/{i}", lat=33.45 + i * 0.001, lon=-112.07,
                    directions=[90.0], manufacturer="Flock Safety")
        for i in range(n)
    ]


@pytest.fixture
def running_server():
    records = [_sighting()]
    cams = _cameras()
    data = server.DataSource(
        records=lambda: export.to_geojson(records),
        cameras=lambda: alpr.to_transfer(cams),
        tracks=lambda: None,
        session=lambda: {"host": "testpc", "counts": {"records": len(records)}},
    )
    srv = server.WarmapServer(data=data, host="127.0.0.1", port=0)
    srv.start()
    yield srv
    srv.stop()


def get(srv, path, headers=None, method="GET"):
    """(status, headers, body). HTTP errors come back as values, not raises,
    because the status is what most of these tests are asserting on."""
    url = f"http://127.0.0.1:{srv.port}{path}"
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def base(srv):
    return f"/s/{srv.token}/"


# --- the token gate -------------------------------------------------------

def test_no_token_is_not_found(running_server):
    assert get(running_server, "/")[0] == 404
    assert get(running_server, "/index.html")[0] == 404
    assert get(running_server, "/api/records")[0] == 404


def test_wrong_token_is_not_found(running_server):
    assert get(running_server, "/s/not-the-token/api/records")[0] == 404
    # A near-miss must be refused too: the compare is constant-time and exact,
    # not a prefix match.
    almost = running_server.token[:-1] + ("A" if running_server.token[-1] != "A" else "B")
    assert get(running_server, f"/s/{almost}/api/records")[0] == 404


def test_token_is_long_and_random():
    a = server.WarmapServer()
    b = server.WarmapServer()
    assert len(a.token) >= 30
    assert a.token != b.token


def test_a_wrong_token_looks_exactly_like_a_missing_file(running_server):
    """Same status and body either way, so the response can't be used to tell
    whether a guessed token was real."""
    wrong = get(running_server, "/s/deadbeefdeadbeefdeadbeef00/api/records")
    missing = get(running_server, base(running_server) + "no-such-file")
    assert wrong[0] == missing[0] == 404
    assert wrong[2] == missing[2]


# --- read-only ------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_writes_are_refused(running_server, method):
    status, _, _ = get(running_server, base(running_server) + "api/records",
                       method=method)
    assert status == 405


# --- no path traversal ----------------------------------------------------

@pytest.mark.parametrize("path", [
    "../../../etc/passwd",
    "vendor/../../../../etc/passwd",
    "....//....//etc/passwd",
    "vendor/leaflet/../../../../../../etc/passwd",
])
def test_traversal_is_refused(running_server, path):
    status, _, _ = get(running_server, base(running_server) + path)
    assert status == 404


def test_only_listed_files_are_served(running_server, tmp_path):
    # A real file inside the webapp dir that isn't in the allow-list stays
    # unreachable.
    sneaky = config.WEBAPP_DIR / "notes.txt"
    sneaky.write_text("private", encoding="utf-8")
    try:
        status, _, _ = get(running_server, base(running_server) + "notes.txt")
        assert status == 404
    finally:
        sneaky.unlink()


# --- the app shell --------------------------------------------------------

@pytest.mark.parametrize("name", [
    "", "index.html", "app.js", "app.css", "map.js", "map.css",
    "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png",
    "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css",
])
def test_shell_files_are_served(running_server, name):
    status, headers, body = get(running_server, base(running_server) + name)
    assert status == 200, name
    assert body, name


def test_manifest_is_valid_and_installable(running_server):
    _, _, body = get(running_server, base(running_server) + "manifest.webmanifest")
    manifest = json.loads(body)
    # The fields Chrome actually requires to offer an install.
    assert manifest["name"]
    assert manifest["start_url"]
    assert manifest["display"] in ("standalone", "fullscreen", "minimal-ui")
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "192x192" in sizes and "512x512" in sizes
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])


def test_map_js_is_the_same_file_the_desktop_uses(running_server):
    """One renderer, two hosts. If these ever diverge, the phone and the PC
    start drawing different maps from the same data."""
    _, _, served = get(running_server, base(running_server) + "map.js")
    assert served == (config.WEB_DIR / "map.js").read_bytes()


# --- the data API ---------------------------------------------------------

def test_api_records(running_server):
    status, _, body = get(running_server, base(running_server) + "api/records")
    assert status == 200
    payload = json.loads(body)
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1


def test_api_cameras_is_the_compact_form(running_server):
    _, _, body = get(running_server, base(running_server) + "api/cameras")
    payload = json.loads(body)
    assert payload["fields"][:2] == ["lon", "lat"]
    assert payload["count"] == 3
    assert len(payload["cameras"][0]) == len(payload["fields"])


def test_unknown_api_endpoint(running_server):
    assert get(running_server, base(running_server) + "api/nope")[0] == 404


def test_a_failing_provider_does_not_kill_the_server(running_server):
    def boom():
        raise RuntimeError("provider exploded")

    running_server.data.records = boom
    status, _, _ = get(running_server, base(running_server) + "api/records")
    assert status == 503
    # The server is still up for everything else.
    assert get(running_server, base(running_server) + "api/session")[0] == 200


def test_large_payloads_are_gzipped(running_server):
    cams = _cameras(400)
    running_server.data.cameras = lambda: alpr.to_transfer(cams)
    status, headers, body = get(running_server, base(running_server) + "api/cameras",
                                headers={"Accept-Encoding": "gzip"})
    assert status == 200
    assert headers.get("Content-Encoding") == "gzip"
    payload = json.loads(gzip.decompress(body))
    assert payload["count"] == 400


def test_no_gzip_when_the_client_does_not_ask(running_server):
    cams = _cameras(400)
    running_server.data.cameras = lambda: alpr.to_transfer(cams)
    _, headers, body = get(running_server, base(running_server) + "api/cameras",
                           headers={"Accept-Encoding": "identity"})
    assert headers.get("Content-Encoding") is None
    assert json.loads(body)["count"] == 400


def test_data_reflects_the_app_now_not_at_startup(running_server):
    """The providers are callables on purpose: import something on the PC and
    the phone's next sync sees it without restarting the server."""
    _, _, before = get(running_server, base(running_server) + "api/records")
    assert len(json.loads(before)["features"]) == 1

    more = [_sighting(), _sighting(bssid="AA:BB:CC:DD:EE:02", lat=33.46)]
    running_server.data.records = lambda: export.to_geojson(more)
    _, _, after = get(running_server, base(running_server) + "api/records")
    assert len(json.loads(after)["features"]) == 2


# --- lifecycle ------------------------------------------------------------

def test_server_stops_cleanly():
    srv = server.WarmapServer(host="127.0.0.1", port=0)
    srv.start()
    assert srv.running
    port = srv.port
    assert get(srv, f"/s/{srv.token}/")[0] == 200
    srv.stop()
    assert not srv.running
    # The port is actually released, not just forgotten.
    with pytest.raises(Exception):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)


def test_info_url_contains_the_token():
    srv = server.WarmapServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        info = srv.info()
        assert srv.token in info.url
        assert info.url.startswith("http://")
        assert info.url.endswith("/")
    finally:
        srv.stop()


def test_hits_are_counted_so_the_dialog_can_confirm_a_connection(running_server):
    before = running_server.hits
    get(running_server, base(running_server) + "api/session")
    assert running_server.hits > before
    assert running_server.last_client == "127.0.0.1"


# --- the offline single-file export ---------------------------------------

def _bundle():
    records = export.to_geojson([_sighting()])
    cams = alpr.to_transfer(_cameras())
    return offline.build(records, cameras_transfer=cams,
                         session={"host": "testpc", "generated": "2026-08-30"})


def test_offline_bundle_is_self_contained():
    html = _bundle().html
    # Nothing left to fetch: no external script/style/image references.
    assert "<script src=" not in html
    assert "<link rel=\"stylesheet\"" not in html
    assert "images/marker-icon.png" not in html   # inlined as a data: URI
    assert "data:image/png;base64," in html


def test_offline_bundle_carries_the_app_and_the_data():
    bundle = _bundle()
    assert bundle.records == 1
    assert bundle.cameras == 3
    html = bundle.html
    assert "window.WARMAP_EMBEDDED" in html
    assert "warmapLoadData" in html          # the shared renderer is in there
    assert "TestNet" in html                 # and so is the actual record
    assert "leaflet" in html.lower()


def test_offline_bundle_embeds_valid_json():
    html = _bundle().html
    start = html.index("window.WARMAP_EMBEDDED = ") + len("window.WARMAP_EMBEDDED = ")
    end = html.index("\n</script>", start)
    payload = json.loads(html[start:end].rstrip().rstrip(";"))
    assert payload["records"]["features"][0]["properties"]["ssid"] == "TestNet"
    assert payload["cameras"]["count"] == 3


def test_offline_write_round_trips(tmp_path):
    out = tmp_path / "warmap-offline.html"
    bundle = _bundle()
    written = offline.write(bundle, out)
    assert written > 100_000          # Leaflet alone is bigger than this
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_nearby_cameras_keeps_the_close_ones():
    records = export.to_geojson([_sighting(lat=33.45, lon=-112.07)])
    cams = _cameras(3) + [
        alpr.Camera(osm_id="node/far", lat=40.0, lon=-80.0),
    ]
    near = offline.nearby_cameras(cams, records, meters=5000)
    ids = {c.osm_id for c in near}
    assert "node/far" not in ids
    assert "node/0" in ids




def test_offline_embedded_json_cannot_break_out_of_the_script_tag():
    """An SSID is a string a stranger chose and broadcast. json.dumps escapes
    quotes but not '<', so a network named '</script><whatever follows>' would
    close the inline script early and turn the rest of its own name into
    live markup in the exported file, turning a capture into script
    execution when the file is opened. The payload must carry it as \\u003c
    and still parse back exactly.
    """
    hostile = "</script><img src=x onerror=alert(1)>"
    records = export.to_geojson([_sighting(ssid=hostile)])
    html = offline.build(records).html

    assert "</script><img" not in html
    assert "\\u003c/script" in html

    start = html.index("window.WARMAP_EMBEDDED = ") + len("window.WARMAP_EMBEDDED = ")
    end = html.index("\n</script>", start)
    payload = json.loads(html[start:end].rstrip().rstrip(";"))
    assert payload["records"]["features"][0]["properties"]["ssid"] == hostile


def test_offline_embedded_json_escapes_js_line_terminators():
    """U+2028/U+2029 are legal in a JSON string but terminate a line in older
    JavaScript parsers, which would break the assignment mid-payload."""
    records = export.to_geojson([_sighting(ssid="a\u2028b\u2029c")])
    html = offline.build(records).html
    assert "\u2028" not in html
    assert "\\u2028" in html


# --- review fixes ---------------------------------------------------------

def test_non_ascii_token_is_a_clean_404_not_a_crash(running_server):
    """hmac.compare_digest refuses to compare non-ASCII strings and raises
    TypeError. The request line arrives latin-1 decoded, so one high byte in
    the URL used to blow up on the single pre-auth path every request crosses,
    dropping the connection with no response at all instead of a 404."""
    import socket
    raw = (b"GET /s/\xc3\xa9token/app.js HTTP/1.1\r\n"
           b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
    s = socket.create_connection(("127.0.0.1", running_server.port), timeout=10)
    try:
        s.sendall(raw)
        response = s.recv(200)
    finally:
        s.close()
    assert response, "server answered nothing at all"
    assert b"404" in response.split(b"\r\n", 1)[0]


def test_token_ok_never_raises_on_odd_input(running_server):
    for weird in ["é", "\udcff", "", "x" * 5000, "токен"]:
        assert running_server.token_ok(weird) is False


@pytest.mark.parametrize("path", [
    "tiles/25/1/1.png",        # past OSM's deepest zoom
    "tiles/3/99/1.png",        # x beyond 2^z
    "tiles/3/1/99.png",        # y beyond 2^z
])
def test_out_of_range_tiles_are_refused(path):
    """A tile request is the one thing a phone can ask for that makes the PC
    fetch from OpenStreetMap and write a file. Coordinates that name no real
    tile must never reach the network or the disk."""
    class _Proxy:
        def __init__(self):
            self.calls = 0

        def fetch_tile(self, z, x, y):
            self.calls += 1
            return b"tile"

    proxy = _Proxy()
    srv = server.WarmapServer(tile_proxy=proxy, host="127.0.0.1", port=0)
    srv.start()
    try:
        status, _, _ = get(srv, f"/s/{srv.token}/{path}")
        assert status == 404
        assert proxy.calls == 0, "an out-of-range tile still reached the proxy"
        # A real one still works.
        ok, _, body = get(srv, f"/s/{srv.token}/tiles/12/100/200.png")
        assert ok == 200 and body == b"tile"
        assert proxy.calls == 1
    finally:
        srv.stop()


def test_nearby_cameras_with_no_reference_points_embeds_none():
    """'Near your captures' has no meaning with nothing to measure against.
    Returning everything would make the one export with no records to show
    into the largest file the exporter can produce."""
    empty = {"type": "FeatureCollection", "features": []}
    assert offline.nearby_cameras(_cameras(3), empty) == []


def test_mobile_shell_points_tiles_at_this_origin():
    """The phone must pull tiles through the PC, not straight from OSM: the
    PC's cache answers them and a service worker can only cache same-origin
    responses, so going direct would mean no offline basemap at all."""
    html = (config.WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    assert 'window.warmapTileUrl = "tiles/{z}/{x}/{y}.png"' in html
    # In <head>, so it runs before map.js reads it and so the offline export
    # (which copies only <body>) correctly falls back to OpenStreetMap.
    assert html.index("warmapTileUrl") < html.index("</head>")
    map_js = (config.WEB_DIR / "map.js").read_text(encoding="utf-8")
    assert "window.warmapTileUrl" in map_js
