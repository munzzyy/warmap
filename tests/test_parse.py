"""Parser robustness. Every edge case called out in the build spec gets
its own test so a future change can't silently regress one of them."""

from __future__ import annotations

from pathlib import Path

import pytest

from warmap.models import ENC_OPEN, ENC_UNKNOWN, ENC_WEP, ENC_WPA, ENC_WPA2, ENC_WPA3, ENC_WPA23_MIXED
from warmap.parse import classify_encryption, dedup, parse_file, sniff_format

WIGLE_HEADER = "WigleWifi-1.4,appRelease=1.0,model=ESP32 Marauder,release=1.0,device=ESP32,display=,board=,brand=Marauder\n"
COLUMN_HEADER = "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


def test_sniff_wigle_vs_generic(tmp_path):
    wigle = _write(tmp_path, "w.csv", WIGLE_HEADER + COLUMN_HEADER)
    generic = _write(tmp_path, "g.csv", "lat,lon,name\n1,2,three\n")
    assert sniff_format(wigle) == "wigle"
    assert sniff_format(generic) == "generic"


def test_basic_wigle_row_parses(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:FF,MyHomeWifi,[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,6,-55,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "one.csv", content)
    aps = parse_file(path)
    assert len(aps) == 1
    ap = aps[0]
    assert ap.bssid == "AA:BB:CC:DD:EE:FF"
    assert ap.ssid == "MyHomeWifi"
    assert ap.enc_bucket == ENC_WPA2
    assert ap.channel == 6
    assert ap.rssi == -55
    assert ap.lat == 33.4 and ap.lon == -112.0
    assert ap.type == "WIFI"
    assert ap.times_seen == 1


def test_device_name_line_names_the_next_ble_row(tmp_path):
    # A raw-serial-stream wardrive carries a "Device: <name>" line before each
    # BLE row. The companion app strips these; a full-stream capture keeps them,
    # and they're the only place a BLE name shows up in a wardrive.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "Device: Jo's AirTag\n"
        "11:22:33:44:55:66,,[BLE],2026-07-26 01:30:00,0,-40,33.4484,-112.0740,200,3,BLE\n"
    )
    path = _write(tmp_path, "aio.txt", content)
    rows = parse_file(path)
    assert len(rows) == 1
    assert rows[0].ssid == "Jo's AirTag"
    # A name-only tracker match fires off the recovered name.
    assert rows[0].meta.get("tracker") == "Apple AirTag (by name)"


def test_device_name_with_comma_is_rebuilt(tmp_path):
    # The name itself can contain a comma, which the CSV reader splits. It has
    # to be rejoined, not truncated at the first comma.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "Device: Tile, Mate\n"
        "D8:22:33:44:55:88,,[BLE],2026-07-26 01:30:02,0,-55,33.4490,-112.0742,200,3,BLE\n"
    )
    rows = parse_file(_write(tmp_path, "aio.txt", content))
    assert rows[0].ssid == "Tile, Mate"


def test_device_name_does_not_leak_to_a_later_row(tmp_path):
    # The name only belongs to the row that immediately follows it. A BLE row
    # with no "Device:" line of its own must stay nameless.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "Device: Named One\n"
        "11:22:33:44:55:66,,[BLE],2026-07-26 01:30:00,0,-40,33.4484,-112.0740,200,3,BLE\n"
        "77:66:55:44:33:22,,[BLE],2026-07-26 01:30:03,0,-60,33.4487,-112.0743,200,3,BLE\n"
    )
    rows = parse_file(_write(tmp_path, "aio.txt", content))
    named = {r.bssid: r.ssid for r in rows}
    assert named["11:22:33:44:55:66"] == "Named One"
    assert named["77:66:55:44:33:22"] == ""


# --- live-serial (raw ESP32Marauder wardrive) cleaning -------------------
# These strings are verbatim rows off a real BFFB v2 (ESP32-C5, Marauder
# v1.14.0) wardrive over the serial wire, a different, messier shape than the
# clean file the companion app saves.

from warmap.parse import _clean_marauder_line


def test_marauder_ble_name_jammed_onto_mac_is_split():
    row = "ihoment_H6008_1B2Cd4:ad:fc:0a:1b:2c,,[BLE],,0,-49,0.0,0.0,0.00,63.75,BLE"
    cleaned = _clean_marauder_line(row)
    # MAC split back out, name kept as the SSID (the only way a BLE name
    # survives a wardrive, since the companion app throws it away).
    assert cleaned == "d4:ad:fc:0a:1b:2c,ihoment_H6008_1B2C,[BLE],,0,-49,0.0,0.0,0.00,63.75,BLE"


def test_marauder_nameless_ble_prints_mac_twice():
    row = "5a:75:65:11:22:335a:75:65:11:22:33,,[BLE],,0,-90,0.0,0.0,0.00,63.75,BLE"
    cleaned = _clean_marauder_line(row)
    # The MAC printed as its own "name" is not a name, so drop it, leave SSID blank.
    assert cleaned == "5a:75:65:11:22:33,,[BLE],,0,-90,0.0,0.0,0.00,63.75,BLE"


def test_marauder_ble_name_with_comma_is_quoted(tmp_path):
    # A device can advertise a name with a comma; the recovered name goes into
    # the SSID field, so it must be CSV-quoted or the row gains a field.
    row = "Tile, Matec4:39:60:44:55:66,,[BLE],,0,-67,33.44,-112.07,0,63.75,BLE"
    cleaned = _clean_marauder_line(row)
    import csv, io
    fields = next(csv.reader(io.StringIO(cleaned)))
    assert len(fields) == 11
    assert fields[1] == "Tile, Mate"
    assert fields[-1] == "BLE"


def test_marauder_ble_name_with_quote_is_escaped():
    row = 'Say "Hi"d4:ad:fc:0a:1b:2c,,[BLE],,0,-49,33.44,-112.07,0,63.75,BLE'
    cleaned = _clean_marauder_line(row)
    import csv, io
    fields = next(csv.reader(io.StringIO(cleaned)))
    assert len(fields) == 11
    assert fields[1] == 'Say "Hi"'


def test_marauder_wifi_console_counter_prefix_stripped():
    row = "17 | 74:37:5F:AA:BB:CC,MapleHome,[WPA2_PSK],,6,-91,0.0,0.0,0.00,63.75,WIFI"
    assert _clean_marauder_line(row) == (
        "74:37:5F:AA:BB:CC,MapleHome,[WPA2_PSK],,6,-91,0.0,0.0,0.00,63.75,WIFI"
    )


def test_clean_companion_rows_pass_through_unchanged():
    # Idempotent: the companion app's own already-clean rows must not be touched.
    for row in [
        "56:01:f1:aa:bb:cc,,[BLE],2026-07-26 01:30:00,0,-58,33.44,-112.07,198,3.25,BLE",
        "EC:8E:B5:12:34:56,DIRECT-C1,[WPA2_PSK],2026-07-26 01:30:05,1,-91,33.44,-112.07,196,3.25,WIFI",
        "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder",
    ]:
        assert _clean_marauder_line(row) == row


def test_ble_name_that_looks_like_a_counter_is_kept():
    # The "N | " counter is only on Wi-Fi rows. A BLE device whose name starts
    # with that pattern must keep it, not have it stripped as a counter.
    row = "12 | TestDeviced4:ad:fc:0a:1b:2c,,[BLE],,0,-77,33.44,-112.07,0,3,BLE"
    cleaned = _clean_marauder_line(row)
    import csv, io
    fields = next(csv.reader(io.StringIO(cleaned)))
    assert fields[0] == "d4:ad:fc:0a:1b:2c"
    assert fields[1] == "12 | TestDevice"


def test_cleaner_is_idempotent():
    # Cleaning an already-clean row (or a second pass over a cleaned one) must
    # change nothing, so re-imports and warmap's own exports are safe.
    clean = "56:01:f1:aa:bb:cc,,[BLE],2026-07-26 01:30:00,0,-58,33.44,-112.07,198,3.25,BLE"
    assert _clean_marauder_line(clean) == clean
    once = _clean_marauder_line("ihoment_H6008c4:39:60:44:55:66,,[BLE],,0,-49,33.44,-112.07,0,3,BLE")
    assert _clean_marauder_line(once) == once


def test_clean_row_with_ble_marker_inside_ssid_is_left_alone():
    # A pathological but already-clean row whose quoted SSID contains a MAC and
    # the ",,[BLE]" marker must not be re-split.
    row = '00:11:22:33:44:55,"MyName11:22:33:44:55:66,,[BLE]",[BLE],,0,-50,33.44,-112.07,0,3,BLE'
    assert _clean_marauder_line(row) == row


def test_ssid_starting_with_a_quote_only_breaks_its_own_row(tmp_path):
    # Marauder writes SSIDs unquoted, so a network whose name starts with a
    # double quote used to open a CSV quoted field that swallowed every row
    # after it. Per-line parsing must keep the damage to the one bad row.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        'AA:BB:CC:DD:EE:01,"quotestart,[WPA2_PSK],,6,-70,33.44,-112.07,0,3,WIFI\n'
        "BB:CC:DD:EE:FF:02,Normal,[WPA2_PSK],,6,-71,33.45,-112.08,0,3,WIFI\n"
        "CC:DD:EE:FF:00:03,Also,[OPEN],,1,-72,33.46,-112.09,0,3,WIFI\n"
    )
    rows = parse_file(_write(tmp_path, "q.txt", content))
    macs = {r.bssid for r in rows}
    assert "BB:CC:DD:EE:FF:02" in macs and "CC:DD:EE:FF:00:03" in macs


def test_ble_name_containing_the_ble_marker_is_split_correctly(tmp_path):
    # A device that jams ",,[BLE]" into its own name must not fool the split;
    # the real MAC is before the LAST occurrence.
    row = "My,,[BLE]DeviceAA:BB:CC:DD:EE:FF,,[BLE],,0,-49,33.44,-112.07,0,3,BLE"
    cleaned = _clean_marauder_line(row)
    import csv, io
    fields = next(csv.reader(io.StringIO(cleaned)))
    assert fields[0] == "AA:BB:CC:DD:EE:FF"
    assert fields[1] == "My,,[BLE]Device"
    assert fields[-1] == "BLE"


def test_recovered_ble_name_drives_tracker_detection(tmp_path):
    # A tracker that advertises a recognizable name jams it onto the serial row.
    # Recovering that name means the wardrive flags the tracker on its own, with
    # no pcap - which is the only BLE tracker signal this hardware can give.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "Tile Prod4:ad:fc:0a:1b:2c,,[BLE],,0,-49,33.44,-112.07,0,3,BLE\n"
        "My AirTagc4:39:60:44:55:67,,[BLE],,0,-55,33.44,-112.07,0,3,BLE\n"
    )
    rows = parse_file(_write(tmp_path, "aio.txt", content))
    by_mac = {r.bssid: r for r in rows}
    assert "Tile" in by_mac["D4:AD:FC:0A:1B:2C"].meta.get("tracker", "")
    assert "AirTag" in by_mac["C4:39:60:44:55:67"].meta.get("tracker", "")


def test_real_marauder_serial_capture_parses(tmp_path):
    # A header (which the app writes, since it never comes over serial) plus
    # real mangled rows off the wire. With a GPS fix they all parse, MACs come
    # out valid, and the two named BLE devices keep their names.
    header = (
        WIGLE_HEADER + COLUMN_HEADER
    )
    raw = (
        "StartingWardrive. Stop with stopscan\n"
        "/wardrive_5.log\n"
        "> AP config set error, Maurauder SSID might visible : err=0x3005\n"
        "ihoment_H6008_1B2Cd4:ad:fc:0a:1b:2c,,[BLE],,0,-49,33.44,-112.07,0,63.75,BLE\n"
        "5a:75:65:11:22:335a:75:65:11:22:33,,[BLE],,0,-90,33.44,-112.07,0,63.75,BLE\n"
        "netc4:39:60:44:55:66,,[BLE],,0,-67,33.44,-112.07,0,63.75,BLE\n"
        "7 | 8E:49:62:77:88:99,,[WPA2_PSK],,36,-68,33.44,-112.07,0,63.75,WIFI\n"
        "17 | 74:37:5F:AA:BB:CC,MapleHome,[WPA2_PSK],,6,-91,33.44,-112.07,0,63.75,WIFI\n"
    )
    rows = parse_file(_write(tmp_path, "aio.txt", header + raw))
    assert len(rows) == 5  # 3 BLE + 2 WiFi; the status/banner lines are skipped
    by_mac = {r.bssid: r for r in rows}
    assert by_mac["D4:AD:FC:0A:1B:2C"].ssid == "ihoment_H6008_1B2C"
    assert by_mac["C4:39:60:44:55:66"].ssid == "net"
    assert by_mac["5A:75:65:11:22:33"].ssid == ""  # nameless stays blank
    assert by_mac["74:37:5F:AA:BB:CC"].ssid == "MapleHome"


def test_empty_file(tmp_path):
    path = _write(tmp_path, "empty.csv", "")
    assert parse_file(path) == []


def test_header_only_file(tmp_path):
    path = _write(tmp_path, "header_only.csv", WIGLE_HEADER + COLUMN_HEADER)
    assert parse_file(path) == []


def test_metadata_line_only_no_header(tmp_path):
    path = _write(tmp_path, "meta_only.csv", WIGLE_HEADER)
    assert parse_file(path) == []


def test_missing_lat_lon_rows_skipped_not_crashed(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:01,NoLat,[ESS],2024-06-01 12:00:00,1,-50,,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:02,NoLon,[ESS],2024-06-01 12:00:01,1,-50,33.4,,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:03,Good,[ESS],2024-06-01 12:00:02,1,-50,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "missing.csv", content)
    aps = parse_file(path)
    assert [ap.bssid for ap in aps] == ["AA:BB:CC:DD:EE:03"]


def test_zero_zero_treated_as_no_fix(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:01,NoFix,[ESS],2024-06-01 12:00:00,1,-50,0,0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:02,RealFix,[ESS],2024-06-01 12:00:00,1,-50,0.0001,0.0001,340,5,WIFI\n"
    )
    path = _write(tmp_path, "zerozero.csv", content)
    aps = parse_file(path)
    assert [ap.bssid for ap in aps] == ["AA:BB:CC:DD:EE:02"]


def test_malformed_rows_skipped_not_crashed(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "totally,not,enough,columns\n"
        "AA:BB:CC:DD:EE:03,Good,[ESS],2024-06-01 12:00:02,1,-50,33.4,-112.0,340,5,WIFI\n"
        "\n"
        ",,,,,,,,,,\n"
    )
    path = _write(tmp_path, "malformed.csv", content)
    aps = parse_file(path)
    assert [ap.bssid for ap in aps] == ["AA:BB:CC:DD:EE:03"]


def test_non_ascii_ssid(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:04,Café_Wüfi_日本語,[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,1,-50,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "unicode.csv", content)
    aps = parse_file(path)
    assert aps[0].ssid == "Café_Wüfi_日本語"


def test_quoted_ssid_with_embedded_comma(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        'AA:BB:CC:DD:EE:05,"Smith, Family Wifi",[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,1,-50,33.4,-112.0,340,5,WIFI\n'
    )
    path = _write(tmp_path, "quoted.csv", content)
    aps = parse_file(path)
    assert aps[0].ssid == "Smith, Family Wifi"


def test_crlf_line_endings(tmp_path):
    content = (
        WIGLE_HEADER.rstrip("\n") + "\r\n" +
        COLUMN_HEADER.rstrip("\n") + "\r\n" +
        "AA:BB:CC:DD:EE:06,CRLFNet,[ESS],2024-06-01 12:00:00,1,-50,33.4,-112.0,340,5,WIFI\r\n"
    )
    path = _write(tmp_path, "crlf.csv", content)
    aps = parse_file(path)
    assert len(aps) == 1
    assert aps[0].ssid == "CRLFNet"


@pytest.mark.parametrize("rssi_field,expected", [("-70", -70), ("70", 70), ("-1", -1), ("0", 0)])
def test_rssi_positive_and_negative(tmp_path, rssi_field, expected):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        f"AA:BB:CC:DD:EE:07,RssiTest,[ESS],2024-06-01 12:00:00,1,{rssi_field},33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "rssi.csv", content)
    aps = parse_file(path)
    assert aps[0].rssi == expected


def test_duplicate_bssids_all_kept_as_raw_sightings(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:08,Dup,[ESS],2024-06-01 12:00:00,1,-70,33.4,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:08,Dup,[ESS],2024-06-01 12:05:00,1,-40,33.4001,-112.0001,340,5,WIFI\n"
    )
    path = _write(tmp_path, "dup.csv", content)
    aps = parse_file(path)
    assert len(aps) == 2  # raw sightings are NOT deduped by parse_file itself
    assert all(ap.bssid == "AA:BB:CC:DD:EE:08" for ap in aps)


def test_generic_csv_column_autodetect(tmp_path):
    content = "Latitude,Longitude,BSSID,Encryption\n33.5,-112.1,11:22:33:44:55:66,WPA2\n"
    path = _write(tmp_path, "generic.csv", content)
    aps = parse_file(path)
    assert len(aps) == 1
    assert aps[0].bssid == "11:22:33:44:55:66"
    assert aps[0].enc_bucket == ENC_WPA2


def test_generic_csv_with_no_recognizable_columns_yields_nothing(tmp_path):
    content = "foo,bar,baz\n1,2,3\n"
    path = _write(tmp_path, "nogood.csv", content)
    assert parse_file(path) == []


def test_nonexistent_file_returns_empty_not_raise(tmp_path):
    assert parse_file(tmp_path / "does_not_exist.csv") == []


class TestClassifyEncryption:
    def test_open_bracket_ess_only(self):
        assert classify_encryption("[ESS]") == ENC_OPEN

    def test_blank_wifi_is_open(self):
        assert classify_encryption("", "WIFI") == ENC_OPEN

    def test_blank_ble_is_unknown(self):
        assert classify_encryption("", "BLE") == ENC_UNKNOWN

    def test_wep(self):
        assert classify_encryption("[WEP][ESS]") == ENC_WEP

    def test_plain_wpa(self):
        assert classify_encryption("[WPA-PSK-TKIP][ESS]") == ENC_WPA

    def test_wpa2(self):
        assert classify_encryption("[WPA2-PSK-CCMP][ESS]") == ENC_WPA2

    def test_wpa3(self):
        assert classify_encryption("[WPA3-SAE-CCMP][ESS]") == ENC_WPA3

    def test_wpa2_wpa3_mixed(self):
        assert classify_encryption("[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]") == ENC_WPA23_MIXED

    def test_unrecognized_string_is_unknown(self):
        assert classify_encryption("some-garbage-value") == ENC_UNKNOWN

    def test_none_is_treated_as_open_for_wifi(self):
        assert classify_encryption(None, "WIFI") == ENC_OPEN


# --- BOM ------------------------------------------------------------------
# A card touched by a Windows-side tool, or a hand-edit in a text editor that
# defaults to writing one, can pick up a UTF-8 BOM. It's invisible in most
# editors, so this is exactly the kind of thing that reaches a real card.

def test_bom_prefixed_wardrive_still_parses(tmp_path):
    content = "﻿" + WIGLE_HEADER + COLUMN_HEADER + (
        "AA:BB:CC:DD:EE:FF,MyHomeWifi,[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,"
        "6,-55,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "bom.csv", content)
    aps = parse_file(path)
    assert len(aps) == 1, "the BOM made the metadata line look like data, dropping the whole file"
    assert aps[0].ssid == "MyHomeWifi"


def test_bom_prefixed_file_still_sniffs_as_wigle(tmp_path):
    content = "﻿" + WIGLE_HEADER + COLUMN_HEADER
    path = _write(tmp_path, "bom.csv", content)
    assert sniff_format(path) == "wigle"


# --- size cap ---------------------------------------------------------------
# Every other parser in this codebase (flipper.py, gps.py, pcap.py) refuses a
# file over some ceiling rather than reading an arbitrarily large one into
# memory a line at a time on the GUI thread. This one had no cap at all.

def test_oversized_csv_is_refused(tmp_path, monkeypatch):
    import warmap.parse as parse_module

    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:FF,MyHomeWifi,[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,"
        "6,-55,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "big.csv", content)
    monkeypatch.setattr(parse_module, "MAX_FILE_BYTES", 8)
    assert parse_file(path) == []


def test_a_normal_sized_csv_is_unaffected_by_the_cap(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:FF,MyHomeWifi,[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,"
        "6,-55,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "normal.csv", content)
    assert len(parse_file(path)) == 1


# --- dedup: a fabricated RSSI must never outrank a real one ----------------
# Sighting.rssi is a plain int, not Optional, so an unparseable or missing
# RSSI cell has to become *some* number, and 0 is what's used (see
# _row_to_sighting). But dedup's "strongest signal wins" then has to compare
# that fabricated 0 against a real, negative dBm reading, and 0 > -88 would
# let a corrupted reading "win" a real one just because of the sentinel.

def test_unparseable_rssi_does_not_beat_a_real_reading_in_dedup(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:AA,RealSignal,[WPA2_PSK],2024-06-01 12:00:00,6,-88,"
        "33.4,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:AA,RealSignal,[WPA2_PSK],2024-06-01 12:00:05,6,garbage,"
        "33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "rssi.csv", content)
    raw = parse_file(path)
    assert [r.rssi for r in raw] == [-88, 0]  # the garbage cell degrades, doesn't drop the row

    merged = dedup(raw)
    assert len(merged) == 1
    assert merged[0].rssi == -88, "a fabricated 0 outranked a real -88 dBm reading"
    assert merged[0].times_seen == 2


def test_missing_rssi_column_does_not_beat_a_real_reading_in_dedup(tmp_path):
    # A third-party/generic export with no RSSI column at all defaults every
    # row to 0 just the same as an unparseable one, same bug, different cause.
    generic_no_rssi = (
        "Latitude,Longitude,BSSID,Encryption\n"
        "33.4,-112.0,AA:BB:CC:DD:EE:BB,WPA2\n"
    )
    path = _write(tmp_path, "generic.csv", generic_no_rssi)
    no_rssi_row = parse_file(path)
    assert no_rssi_row[0].rssi == 0

    real_row_content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:BB,RealSignal,[WPA2_PSK],2024-06-01 12:00:00,6,-70,"
        "33.4,-112.0,340,5,WIFI\n"
    )
    real_row = parse_file(_write(tmp_path, "real.csv", real_row_content))

    merged = dedup(real_row + no_rssi_row)
    assert len(merged) == 1
    assert merged[0].rssi == -70


def test_rssi_unknown_marker_does_not_leak_into_merged_meta(tmp_path):
    # rssi_unknown is an internal signal for the comparison above, not a fact
    # about the merged identity. It must never show up in what gets
    # persisted to the store or exported.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:CC,X,[WPA2_PSK],2024-06-01 12:00:00,6,-70,33.4,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:CC,X,[WPA2_PSK],2024-06-01 12:00:05,6,garbage,33.4,-112.0,340,5,WIFI\n"
    )
    raw = parse_file(_write(tmp_path, "leak.csv", content))
    merged = dedup(raw)
    assert "rssi_unknown" not in merged[0].meta


def test_two_unknown_rssi_readings_still_dedup_without_crashing(tmp_path):
    # Both candidates unknown: neither should be preferred over the other on
    # RSSI grounds, and the merge must still produce exactly one record.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:DD,X,[WPA2_PSK],2024-06-01 12:00:00,6,garbage1,33.4,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:DD,X,[WPA2_PSK],2024-06-01 12:00:05,6,garbage2,33.4,-112.0,340,5,WIFI\n"
    )
    raw = parse_file(_write(tmp_path, "bothunknown.csv", content))
    merged = dedup(raw)
    assert len(merged) == 1
    assert merged[0].times_seen == 2


# --- truncated final row: the card was pulled mid-write --------------------
# Extremely common in the field. Every complete row before the cut must
# still come through.

def test_truncated_final_row_keeps_the_complete_rows_before_it(tmp_path):
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:10,First,[WPA2_PSK],2024-06-01 12:00:00,6,-55,33.4,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:11,Second,[WPA2_PSK],2024-06-01 12:00:01,6,-60,33.5,-112.1,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:12,Thi"  # cut off mid-row, no newline, no further fields
    )
    path = _write(tmp_path, "trunc.csv", content)
    aps = parse_file(path)
    assert [ap.bssid for ap in aps] == ["AA:BB:CC:DD:EE:10", "AA:BB:CC:DD:EE:11"]


def test_truncated_mid_quoted_field_keeps_the_complete_rows_before_it(tmp_path):
    # Cut off inside an opened-but-never-closed quoted field, the nastier
    # version of a card pulled mid-write, since the unterminated quote could
    # in principle confuse a whole-file CSV parse.
    content = (
        WIGLE_HEADER + COLUMN_HEADER +
        "AA:BB:CC:DD:EE:20,First,[WPA2_PSK],2024-06-01 12:00:00,6,-55,33.4,-112.0,340,5,WIFI\n"
        'AA:BB:CC:DD:EE:21,"Unterminated quote SSID here'
    )
    path = _write(tmp_path, "trunc2.csv", content)
    aps = parse_file(path)
    assert [ap.bssid for ap in aps] == ["AA:BB:CC:DD:EE:20"]


# --- encoding robustness ----------------------------------------------------

def test_nul_byte_in_a_field_does_not_crash_or_drop_other_rows(tmp_path):
    path = tmp_path / "nul.csv"
    with open(path, "wb") as f:
        f.write(WIGLE_HEADER.encode())
        f.write(COLUMN_HEADER.encode())
        f.write(b"AA:BB:CC:DD:EE:01,Nul\x00Net,[WPA2_PSK],2024-06-01 12:00:00,6,-55,"
                b"33.4,-112.0,340,5,WIFI\n")
        f.write(b"AA:BB:CC:DD:EE:02,Good,[WPA2_PSK],2024-06-01 12:00:01,6,-55,"
                b"33.4,-112.0,340,5,WIFI\n")
    aps = parse_file(path)
    assert [ap.bssid for ap in aps] == ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]


def test_invalid_utf8_byte_is_localized_to_its_own_field(tmp_path):
    # A raw latin-1 byte (invalid standalone UTF-8) must only garble the text
    # field it's actually in. The row's structured fields (rssi, lat, lon,
    # channel) must come through exactly, not get shifted or dropped.
    path = tmp_path / "latin1.csv"
    with open(path, "wb") as f:
        f.write(WIGLE_HEADER.encode())
        f.write(COLUMN_HEADER.encode())
        f.write(
            "AA:BB:CC:DD:EE:03,Caf".encode() + bytes([0xE9]) +
            ",[WPA2_PSK],2024-06-01 12:00:00,6,-55,33.4,-112.0,340,5,WIFI\n".encode()
        )
    aps = parse_file(path)
    assert len(aps) == 1
    ap = aps[0]
    assert ap.rssi == -55 and ap.lat == 33.4 and ap.lon == -112.0 and ap.channel == 6


# --- header/version shape variance -----------------------------------------

def test_mixed_version_marker_and_column_set_still_parses(tmp_path):
    # The metadata line's version number is just a marker to skip; the actual
    # header row drives column mapping regardless of what version it claims.
    content = (
        "WigleWifi-1.6,appRelease=2.0,model=WiGLE Android\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
        "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"  # 1.4-shaped columns
        "AA:BB:CC:DD:EE:30,MixedNet,[WPA2-PSK-CCMP][ESS],2024-06-01 12:00:00,"
        "6,-55,33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "mixed.csv", content)
    aps = parse_file(path)
    assert len(aps) == 1
    assert aps[0].ssid == "MixedNet"


def test_rows_without_any_header_yield_nothing_not_a_wrong_guess(tmp_path):
    # No header line at all (accidentally deleted from a hand-edited file):
    # there's no way to know which column is which, so returning nothing is
    # the correct behavior, not a bug, so this locks that choice in.
    content = (
        "AA:BB:CC:DD:EE:32,NoHeaderNet,[WPA2_PSK],2024-06-01 12:00:00,6,-55,"
        "33.4,-112.0,340,5,WIFI\n"
    )
    path = _write(tmp_path, "nohead.csv", content)
    assert parse_file(path) == []


def test_extra_trailing_column_beyond_the_known_set_is_ignored(tmp_path):
    content = (
        "WigleWifi-1.4,appRelease=1.0\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
        "AltitudeMeters,AccuracyMeters,Type,VendorExtraCol\n"
        "AA:BB:CC:DD:EE:34,ExtraColNet,[WPA2_PSK],2024-06-01 12:00:00,6,-55,"
        "33.4,-112.0,340,5,WIFI,whatever\n"
    )
    path = _write(tmp_path, "extracol.csv", content)
    aps = parse_file(path)
    assert len(aps) == 1
    assert aps[0].ssid == "ExtraColNet"
