"""What a hostile file or a stranger on the network can cost this machine:
each case here was a measured problem before the guard it checks existed."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from warmap import alpr_fetch, export, flipper, ingest, offline, parse, pcap, stats
from warmap.gps import _utc_to_local_naive
from warmap.models import TYPE_WIFI, Sighting
from warmap.server import MAX_CONNECTIONS, WarmapServer
from datetime import datetime

HEADER = (
    "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder,release=v1.14.0\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,Type\n"
)


def _csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "wardrive.csv"
    path.write_text(HEADER + "\n".join(rows) + "\n")
    return path


# --- the CSV reader streams -------------------------------------------------

def test_a_csv_of_padding_costs_almost_no_memory(tmp_path):
    path = tmp_path / "padding.csv"
    path.write_bytes(b"\n" * (4 * 1024 * 1024))
    tracemalloc.start()
    try:
        assert parse.parse_file(path) == []
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


def test_streaming_reader_still_parses_a_normal_file(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text(HEADER + "\n\nAA:BB:CC:DD:EE:01,Net,[WPA2_PSK],2026-06-14 09:05:09,6,-57,33.449359,-112.072225,341.3,10.4,WIFI\n")
    rows = parse.parse_file(path)
    assert [r.ssid for r in rows] == ["Net"]
    assert rows[0].source == str(path)


def test_a_file_with_only_the_marker_line_is_empty_not_fatal(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text("WigleWifi-1.4,appRelease=v1.14.0\n")
    assert parse.parse_file(path) == []


# --- the pcap probe list stays linear --------------------------------------

def _probe_request(station: str, ssid: str) -> bytes:
    frame = struct.pack("<H", 4 << 4) + b"\x00\x00"
    frame += b"\xff" * 6 + bytes.fromhex(station.replace(":", "")) + b"\xff" * 6 + b"\x00\x00"
    body = ssid.encode()
    frame += bytes([0, len(body)]) + body
    return frame


def _pcap(dlt: int, frames: list[bytes]) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, dlt)
    for i, frame in enumerate(frames):
        out += struct.pack("<IIII", 1_700_000_000 + i, 0, len(frame), len(frame)) + frame
    return out


def test_one_station_probing_for_thousands_of_names_parses_in_bounded_time(tmp_path):
    frames = [_probe_request("02:00:00:00:00:01", f"n{i:07d}") for i in range(20_000)]
    path = tmp_path / "probes.pcap"
    path.write_bytes(_pcap(105, frames))
    started = time.perf_counter()
    outcome = pcap.read_pcap(path)
    assert time.perf_counter() - started < 5.0
    (record,) = outcome.sightings
    assert len(record.meta["probing_for"]) == pcap.MAX_PROBED_NAMES
    assert record.meta["probing_for_more"] == 20_000 - pcap.MAX_PROBED_NAMES
    assert record.times_seen == 20_000


# --- the Flipper reader joins repeated keys once -----------------------------

def test_a_sub_file_of_repeated_keys_parses_in_linear_time():
    text = "Filetype: Flipper SubGhz RAW File\nVersion: 1\nFrequency: 433920000\nProtocol: RAW\n"
    text += "RAW_Data: 1 -2 3\n" * 200_000
    started = time.perf_counter()
    header, records = flipper.read_fff(text)
    assert time.perf_counter() - started < 3.0
    assert header["Protocol"] == "RAW"
    assert header["RAW_Data"].startswith("1 -2 3 1 -2 3")
    assert records == []


def test_records_after_separators_still_group_and_join():
    header, records = flipper.read_fff("Filetype: IR\n#\nname: A\ndata: 1\ndata: 2\n#\n#\nname: B\n")
    assert header == {"Filetype": "IR"}
    assert records == [{"name": "A", "data": "1 2"}, {"name": "B"}]


# --- the phone bridge: token first, connections bounded ----------------------

def _recv_until_close(sock: socket.socket, limit: float = 3.0) -> bytes:
    sock.settimeout(limit)
    chunks = []
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except socket.timeout:
        pass
    return b"".join(chunks)


@pytest.fixture
def bridge():
    server = WarmapServer(host="127.0.0.1")
    info = server.start()
    yield server, info
    server.stop()


def test_a_wrong_token_is_refused_before_any_header_is_read(bridge):
    server, info = bridge
    with socket.create_connection(("127.0.0.1", info.port)) as sock:
        sock.sendall(b"GET /s/not-the-token/api/records HTTP/1.1\r\n")
        reply = _recv_until_close(sock)
    assert reply.startswith(b"HTTP/1.1 404")
    assert b"Connection: close" in reply
    assert server.hits == 0


def test_the_right_token_still_gets_the_app(bridge):
    server, info = bridge
    with socket.create_connection(("127.0.0.1", info.port)) as sock:
        sock.sendall(f"GET /s/{server.token}/api/session HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        reply = _recv_until_close(sock)
    assert reply.startswith(b"HTTP/1.1 200")


def test_connections_past_the_cap_are_closed_and_threads_stay_bounded(bridge):
    server, info = bridge
    before = threading.active_count()
    socks = []
    try:
        for _ in range(MAX_CONNECTIONS + 12):
            s = socket.create_connection(("127.0.0.1", info.port))
            socks.append(s)
        time.sleep(0.5)
        assert threading.active_count() - before <= MAX_CONNECTIONS + 2
        closed = 0
        for s in socks:
            s.settimeout(0.3)
            try:
                if s.recv(1) == b"":
                    closed += 1
            except socket.timeout:
                pass
            except OSError:
                closed += 1
        assert closed >= 8
    finally:
        for s in socks:
            s.close()
    time.sleep(0.3)
    with socket.create_connection(("127.0.0.1", info.port)) as sock:
        sock.sendall(f"GET /s/{server.token}/api/session HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        assert _recv_until_close(sock).startswith(b"HTTP/1.1 200")


# --- one bad file is one error line ------------------------------------------

def test_a_parser_exception_costs_that_file_not_the_batch(tmp_path, monkeypatch):
    good = tmp_path / "good.csv"
    good.write_text(HEADER + "AA:BB:CC:DD:EE:01,Net,[WPA2_PSK],2026-06-14 09:05:09,6,-57,33.449359,-112.072225,341.3,10.4,WIFI\n")
    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER + "AA:BB:CC:DD:EE:02,Boom,[WPA2_PSK],2026-06-14 09:05:09,6,-57,33.449359,-112.072225,341.3,10.4,WIFI\n")
    real = parse.parse_file

    def exploding(path):
        if Path(path).name == "bad.csv":
            raise RuntimeError("synthetic parser bug")
        return real(path)

    monkeypatch.setattr(parse, "parse_file", exploding)
    result = ingest.ingest_paths([tmp_path])
    assert [s.ssid for s in result.sightings] == ["Net"]
    assert any("bad.csv" in e and "RuntimeError" in e for e in result.errors)


# --- timestamps beyond what the C runtime converts ---------------------------

def test_timestamps_past_the_plausible_window_are_not_captures():
    assert stats.parse_first_seen("9999-12-31 23:59:59") is None
    assert stats.parse_first_seen("1999-12-31 23:59:59") is None
    assert stats.parse_first_seen("2026-06-14 09:05:09") == datetime(2026, 6, 14, 9, 5, 9)


def test_local_conversion_never_raises_at_the_edges():
    for value in (datetime(1, 1, 1, 0, 0), datetime(9999, 12, 31, 23, 59, 59)):
        assert isinstance(_utc_to_local_naive(value), datetime)


# --- what leaves the machine carries a file name, not a path -----------------

def _sighting(source: str) -> Sighting:
    return Sighting(bssid="AA:BB:CC:DD:EE:01", ssid="Net", auth_mode="[WPA2_PSK]", enc_bucket="WPA",
                    first_seen="2026-06-14 09:05:09", channel=6, rssi=-57, lat=33.44, lon=-112.07,
                    altitude=None, accuracy=None, type=TYPE_WIFI, source=source)


def test_exports_and_the_phone_payload_drop_the_directory(tmp_path):
    s = _sighting("/home/someone/secret-project/drive 3/wardrive_0.txt")
    geo = export.to_geojson([s])
    assert geo["features"][0]["properties"]["source"] == "wardrive_0.txt"
    out = tmp_path / "out.csv"
    export.write_csv([s], out)
    text = out.read_text()
    assert "wardrive_0.txt" in text and "secret-project" not in text
    bundle = offline.build(geo, cameras_transfer=None, tracks_geojson=None, session={"host": "", "generated": "2026-08-31"})
    assert "secret-project" not in bundle.html
    assert "wardrive_0.txt" in bundle.html
    assert s.to_dict()["source"].startswith("/home/")


# --- the overlay refresh survives a wrong-typed field -------------------------

def test_wrong_typed_geometry_skips_the_feature_not_the_refresh():
    payload = json.dumps({"features": [
        {"geometry": [1, 2], "properties": {}},
        {"geometry": "x", "properties": {}},
        {"geometry": {"type": "Point", "coordinates": [-112.07, 33.44]},
         "properties": {"manufacturer": "Flock Safety"}, "id": "node/1"},
    ]}).encode()
    records, dropped = alpr_fetch.build_records_from_deflock(json.loads(payload))
    assert len(records) == 1
    assert alpr_fetch.build_records_from_overpass({"elements": [{"type": "way", "id": 1, "center": "x", "tags": {}}]}) == []


# --- round two: what the skeptics got past ----------------------------------

def test_a_non_object_json_root_is_a_failed_source_not_a_traceback():
    for body in (b"[]", b"null", b'"features"', b"1", b'{"features":' + b"[" * 100_000 + b"]" * 100_000 + b"}"):
        with pytest.raises(alpr_fetch.FetchError):
            alpr_fetch.fetch_from_deflock(http_get=lambda url, body=body: body)


def test_an_oversized_integer_coordinate_is_skipped():
    huge = "1" + "0" * 400
    payload = json.loads('{"features": [{"geometry": {"type": "Point", "coordinates": [%s, 33.4]}, "properties": {}}]}' % huge)
    records, dropped = alpr_fetch.build_records_from_deflock(payload)
    assert records == []


def test_kml_and_the_track_payload_carry_file_names_only(tmp_path):
    from warmap import gps

    s = _sighting("/home/someone/secret-project/wardrive_0.txt")
    kml = export.to_kml([s])
    assert "wardrive_0.txt" in kml and "secret-project" not in kml
    track = gps.Track([gps.TrackPoint(lat=33.44, lon=-112.07, when=datetime(2026, 6, 14, 9, 0, 0)),
                       gps.TrackPoint(lat=33.45, lon=-112.08, when=datetime(2026, 6, 14, 9, 1, 0))],
                      source="/home/someone/secret-project/track.gpx")
    assert track.to_geojson()["properties"]["source"] == "track.gpx"


def test_a_flat_folder_of_many_files_stops_at_the_ceiling(tmp_path, monkeypatch):
    import os

    from warmap.walk import BoundedWalk

    for i in range(3_000):
        (tmp_path / f"{i:05d}.sub").write_bytes(b"")
    seen = {"entries": 0}
    real_scandir = os.scandir

    class Counting:
        def __init__(self, it):
            self.it = it

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.it.close()

        def __iter__(self):
            for entry in self.it:
                seen["entries"] += 1
                yield entry

    monkeypatch.setattr(os, "scandir", lambda p: Counting(real_scandir(p)))
    walk = BoundedWalk(tmp_path, 500)
    found = list(walk)
    assert len(found) == 500 and walk.truncated
    assert seen["entries"] <= 501


def test_empty_repeated_values_do_not_leave_double_spaces():
    header, _ = flipper.read_fff("Key: a\nKey:\nKey: b\n")
    assert header["Key"] == "a b"


def test_a_silent_connection_is_let_go_after_the_preauth_timeout(bridge, monkeypatch):
    from warmap import server as server_mod

    monkeypatch.setattr(server_mod._Handler, "preauth_timeout", 0.5)
    _, info = bridge
    started = time.perf_counter()
    with socket.create_connection(("127.0.0.1", info.port)) as sock:
        sock.settimeout(3)
        assert sock.recv(1) == b""
    assert time.perf_counter() - started < 2.5


def test_gpx_times_outside_the_plausible_window_are_dropped():
    from warmap.gps import _parse_iso_utc

    assert _parse_iso_utc("0001-01-01T00:00:00Z") is None
    assert _parse_iso_utc("9999-12-31T23:59:59Z") is None
    assert _parse_iso_utc("2026-06-14T14:00:00Z") is not None


# --- round three -------------------------------------------------------------

def test_csv_rows_past_the_ceiling_are_dropped_not_held(tmp_path, monkeypatch):
    monkeypatch.setattr(parse, "MAX_ROWS_PER_FILE", 50)
    rows = [f"AA:BB:CC:DD:{i >> 8:02X}:{i & 255:02X},n{i},[WPA2_PSK],2026-06-14 09:05:09,6,-57,33.449359,-112.072225,341.3,10.4,WIFI"
            for i in range(200)]
    parsed = parse.parse_file(_csv(tmp_path, rows))
    assert len(parsed) == 50


def test_a_giant_line_and_a_giant_header_are_skipped(tmp_path):
    path = tmp_path / "wide.csv"
    path.write_text("," * (2 * 1024 * 1024) + "\n")
    assert parse.parse_file(path) == []
    path.write_text(HEADER + "x" * (200 * 1024) + "\nAA:BB:CC:DD:EE:04,D,[WPA2_PSK],2026-06-14 09:05:12,6,-50,33.449359,-112.072225,341.3,10.4,WIFI\n")
    assert [r.ssid for r in parse.parse_file(path)] == ["D"]


def test_integer_cells_beyond_what_a_widget_takes_become_unknown(tmp_path):
    rows = ["AA:BB:CC:DD:EE:01,A,[WPA2_PSK],2026-06-14 09:05:09,9223372036854775808,-57,33.449359,-112.072225,341.3,10.4,WIFI",
            "AA:BB:CC:DD:EE:02,B,[WPA2_PSK],2026-06-14 09:05:09,6,1e19,33.449359,-112.072225,341.3,10.4,WIFI"]
    parsed = {r.ssid: r for r in parse.parse_file(_csv(tmp_path, rows))}
    assert parsed["A"].channel is None
    assert parsed["B"].rssi == 0 or parsed["B"].rssi is None or abs(parsed["B"].rssi) <= parse.MAX_INT_CELL


def test_a_huge_hex_mfgrid_does_not_poison_the_store(tmp_path):
    header = ("WigleWifi-1.6,appRelease=2.8,model=Pixel\n"
              "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,"
              "AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type\n")
    path = tmp_path / "w16.csv"
    path.write_text(header + "d4:ad:fc:0a:1b:2c,,[BLE],2026-06-14 09:05:09,0,2402,-60,33.449359,-112.072225,341.3,10.4,,0x"
                    + "f" * 5000 + ",BLE\n")
    (record,) = parse.parse_file(path)
    assert "company_id" not in record.meta
    json.dumps(record.to_dict())


def test_a_malformed_request_target_is_a_404_not_a_traceback(bridge):
    _, info = bridge
    with socket.create_connection(("127.0.0.1", info.port)) as sock:
        sock.sendall(b"GET //[::1 HTTP/1.1\r\nHost: x\r\n\r\n")
        reply = _recv_until_close(sock)
    assert reply.startswith(b"HTTP/1.1 404")


def test_an_early_404_to_head_carries_no_body(bridge):
    _, info = bridge
    with socket.create_connection(("127.0.0.1", info.port)) as sock:
        sock.sendall(b"HEAD /s/wrong/ HTTP/1.1\r\n")
        reply = _recv_until_close(sock)
    assert reply.startswith(b"HTTP/1.1 404") and reply.endswith(b"\r\n\r\n")


def test_a_cut_short_walk_is_reported_in_the_import_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FILES", 3)
    for i in range(10):
        (tmp_path / f"{i}.sub").write_text("Filetype: Flipper SubGhz Key File\nVersion: 1\nFrequency: 433920000\nProtocol: Princeton\nBit: 24\nKey: 00 00 00 00 00 12 34 56\n")
    found = ingest.expand_paths([tmp_path])
    assert len(found) == 3 and found.truncated
    result = ingest.ingest_paths([tmp_path])
    assert any("bigger than warmap will walk" in n for n in result.notes)


def test_a_non_finite_flipper_frequency_is_unknown_and_the_json_stays_valid(tmp_path):
    path = tmp_path / "odd.sub"
    path.write_text("Filetype: Flipper SubGhz Key File\nVersion: 1\nFrequency: inf\nProtocol: Princeton\nBit: 24\nKey: 00 00 00 00 00 12 34 56\n")
    (record,) = flipper.parse_flipper_file(path)
    assert record.frequency is None
    json.loads(json.dumps(record.to_dict(), allow_nan=False))


def test_the_map_engine_check_can_run_twice_in_one_process():
    from warmap import doctor

    ok1, _ = doctor.check_map(timeout_seconds=30)
    ok2, _ = doctor.check_map(timeout_seconds=30)
    assert ok1 and ok2
