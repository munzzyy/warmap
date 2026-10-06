"""The all-in-one wardrive session, end to end.

This is the shape the one-button Flipper capture produces: a Marauder
wardrive `.txt` (dual-band Wi-Fi + BLE + GPS, but BLE rows are only MAC and
RSSI) plus a BLE sniff `.pcap` taken in the same session, where the real
advertisement detail lives: names, company IDs, service UUIDs, tracker type.
The pcap has no coordinates of its own.

What has to hold for warmap to "keep up" with that capture:

* Point it at the dumps folder and it reads both files without being told
  which is which.
* The wardrive rows double as a GPS track, so the BLE devices from the pcap,
  which carry no location, land on the map at the spot the clock says you
  were.
* Those placed devices keep their full detail, so an AirTag, a Tile and a
  cross-platform (DULT) tracker are identified on the map, not just plotted
  as anonymous dots.

If any of that breaks, the detailed capture silently degrades back to the
MAC-and-RSSI wardrive, which is the whole thing this session set out to fix.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from warmap import ingest
from warmap.parse import dedup
from warmap.models import GEO_TRACK


# --- minimal BLE advertisement pcap builder (DLT 251, raw LE link layer) ---

def _mac_bytes(mac: str) -> bytes:
    return bytes(int(b, 16) for b in mac.split(":"))


def _ad(ad_type: int, value: bytes) -> bytes:
    """One AD structure: length (counting the type byte), type, value."""
    return bytes([1 + len(value), ad_type]) + value


def _ble_adv(mac: str, ads: bytes, tx_random: bool = True) -> bytes:
    body = bytes(reversed(_mac_bytes(mac))) + ads
    header = bytes([0x00 | (0x40 if tx_random else 0), len(body)])
    return struct.pack("<I", 0x8E89BED6) + header + body


def _pcap_file(dlt: int, packets: list) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, dlt)
    for ts_sec, ts_frac, payload in packets:
        out += struct.pack("<IIII", ts_sec, ts_frac, len(payload), len(payload))
        out += payload
    return out


# A fixed 2026 instant. Both files are written from this same moment, exactly
# as one board's captures would be, but they record it in different time
# bases, and that difference is part of what this test exists to prove.
_BASE = 1784000000


def _wardrive_wall(sec: int) -> str:
    """A Marauder wardrive row's FirstSeen is UTC straight off the GPS
    module: `dt_string_from_gps()` in the ESP32Marauder firmware prints the
    NMEA hour/minute/second fields with no conversion. The pcap alongside it
    is stamped in epoch seconds by the capturing machine's own clock, which
    warmap reads back as local. Writing both in the same base would make this
    fixture agree with itself while disagreeing with every real capture, and
    would have hidden the five-hour placement error this session found.
    """
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _write_session(folder):
    """Write a wardrive .txt and a BLE sniff .pcap into `folder`, both from
    the same session. Returns nothing: the test reads the folder back."""
    # AirTag / Find My: company 0x004C, mfg-data type byte 0x12.
    airtag = _ble_adv("11:22:33:44:55:66", _ad(0xFF, bytes([0x4C, 0x00, 0x12]) + b"\x00" * 20))
    # Cross-platform tracker: service data under the DULT UUID.
    dult = _ble_adv("C7:22:33:44:55:77", _ad(0x16, bytes([0xB2, 0xFC]) + b"\x40\x01\x02\x03"))
    # A Tile: advertises the 0xFEED member UUID and names itself.
    tile = _ble_adv("D8:22:33:44:55:88", _ad(0x09, b"Tile") + _ad(0x03, bytes([0xED, 0xFE])))
    pcap_bytes = _pcap_file(251, [
        (_BASE, 0, airtag),
        (_BASE + 5, 0, dult),
        (_BASE + 10, 0, tile),
    ])
    (folder / "ble_sniff_0.pcap").write_bytes(pcap_bytes)

    header = (
        "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder,release=v1.14.0\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
        "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
    )
    rows = []
    lat, lon = 33.4484, -112.0740
    for i, offset in enumerate([-2, 0, 5, 10, 12]):
        la = lat + i * 0.0002
        lo = lon + i * 0.0002
        stamp = _wardrive_wall(_BASE + offset)
        rows.append(
            f"AA:BB:CC:DD:EE:{i:02X},Net{i},[WPA2_PSK],{stamp},6,-70,"
            f"{la:.7f},{lo:.7f},200.0,3.0,WIFI"
        )
        rows.append(
            f"5c:69:b7:aa:bb:{i:02X},,[BLE],{stamp},0,-62,"
            f"{la:.7f},{lo:.7f},200.0,3.0,BLE"
        )
    (folder / "wardrive_aio_0.txt").write_text(header + "\n".join(rows) + "\n")


def _pcap_ble(records):
    return [s for s in records if s.meta.get("from_pcap") and s.type in ("BLE", "BT")]


def test_folder_ingest_reads_both_files(tmp_path):
    _write_session(tmp_path)
    result = ingest.ingest_paths([tmp_path])
    # Two files that need two different parsers, discovered by content.
    assert result.files_read >= 2
    assert any("packets decoded" in n for n in result.notes)


def test_sniff_devices_land_on_the_wardrive_track(tmp_path):
    _write_session(tmp_path)
    result = ingest.ingest_paths([tmp_path])
    sniffed = _pcap_ble(result.sightings)
    # All three sniffed devices had no coordinates of their own and still got
    # placed, by matching their timestamps to the track rebuilt from the
    # wardrive rows.
    assert len(sniffed) == 3
    assert all(s.geo_source == GEO_TRACK for s in sniffed)
    assert all(s.has_location for s in sniffed)


def test_placed_devices_keep_their_tracker_detail(tmp_path):
    _write_session(tmp_path)
    result = ingest.ingest_paths([tmp_path])
    by_mac = {s.bssid: s for s in _pcap_ble(result.sightings)}

    airtag = by_mac["11:22:33:44:55:66"]
    assert "AirTag" in airtag.meta.get("tracker", "")
    assert airtag.meta.get("tracker_basis") == "advertisement"

    tile = by_mac["D8:22:33:44:55:88"]
    assert tile.meta.get("tracker") == "Tile"
    # Structural match on the service UUID, not just the self-reported name.
    assert tile.meta.get("tracker_basis") == "advertisement"

    # The cross-platform tracker advertises DULT service data (0x16 / 0xFCB2),
    # which the AD parser folds into service_uuids, so it's identified too.
    dult = by_mac["C7:22:33:44:55:77"]
    assert "DULT" in dult.meta.get("tracker", "")
    assert dult.meta.get("tracker_basis") == "advertisement"


def test_survives_dedup_into_one_record_each(tmp_path):
    _write_session(tmp_path)
    result = ingest.ingest_paths([tmp_path])
    deduped = dedup(result.sightings)
    sniffed = _pcap_ble(deduped)
    assert len(sniffed) == 3
    # Detail and location both survive the merge step the store runs.
    assert all(s.has_location and s.meta.get("tracker") for s in sniffed)
