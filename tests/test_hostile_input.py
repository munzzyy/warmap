"""What happens when a file is not what it claims to be.

Everything the importer reads comes off removable media, so "malformed" is the
normal case and "enormous" is one symlink away. These are the cases an
adversarial review pass found reachable, each one used to either take the
window down or quietly produce a wrong answer.
"""

from __future__ import annotations

import os
import struct

from warmap import flipper, gps, ingest, parse, pcap, sdcard


def _pcap_header(dlt: int) -> bytes:
    return struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, dlt)


# --- size caps -----------------------------------------------------------


def test_oversized_pcap_is_refused_before_being_read(tmp_path, monkeypatch):
    """The whole file is read into memory before anything is parsed, so the
    per-packet caps do nothing about a huge one. Refuse on the size on disk
    instead of running the machine out of memory from a menu click."""
    path = tmp_path / "huge.pcap"
    path.write_bytes(_pcap_header(127))
    monkeypatch.setattr(pcap, "MAX_FILE_BYTES", 8)

    read_calls = []
    original = type(path).read_bytes
    monkeypatch.setattr(
        type(path), "read_bytes",
        lambda self: (read_calls.append(self), original(self))[1],
    )

    result = pcap.read_pcap(path)
    assert result.error is not None
    assert "limit" in result.error
    assert read_calls == [], "the file was read despite being over the cap"


def test_oversized_flipper_file_is_skipped(tmp_path, monkeypatch):
    path = tmp_path / "big.sub"
    path.write_text("Filetype: Flipper SubGhz Key File\nProtocol: Princeton\n")
    monkeypatch.setattr(flipper, "MAX_FILE_BYTES", 4)
    assert flipper.parse_flipper_file(path) == []


def test_oversized_track_is_skipped(tmp_path, monkeypatch):
    path = tmp_path / "big.nmea"
    path.write_text("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\n")
    monkeypatch.setattr(gps, "MAX_FILE_BYTES", 4)
    assert gps.parse_nmea(path) == []


def test_oversized_gpx_is_skipped(tmp_path, monkeypatch):
    path = tmp_path / "big.gpx"
    path.write_text('<gpx><trkpt lat="1" lon="2"/></gpx>')
    monkeypatch.setattr(gps, "MAX_FILE_BYTES", 4)
    assert gps.parse_gpx(path) == []


def test_a_symlink_to_a_big_file_is_capped_too(tmp_path, monkeypatch):
    """`Path.is_file()` follows symlinks, so the folder walk picks up a link
    pointing anywhere on the filesystem. The cap has to be on the resolved
    size, which `stat()` gives us, not on the link itself."""
    target = tmp_path / "target.bin"
    target.write_bytes(b"x" * 4096)
    link = tmp_path / "lure.sub"
    os.symlink(target, link)

    assert link.is_file()
    monkeypatch.setattr(flipper, "MAX_FILE_BYTES", 512)
    assert flipper.parse_flipper_file(link) == []


def test_oversized_csv_is_skipped(tmp_path, monkeypatch):
    """The CSV wardrive reader was the one parser in this codebase with no
    size cap at all. The whole file is read into a list of rows before
    anything becomes a Sighting, same failure mode as the others above."""
    path = tmp_path / "big.csv"
    path.write_text(
        "WigleWifi-1.4,appRelease=1.0\nMAC,SSID,AuthMode,FirstSeen,Channel,RSSI,"
        "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
        "AA:BB:CC:DD:EE:FF,Net,[WPA2_PSK],2024-06-01 12:00:00,6,-55,33.4,-112.0,340,5,WIFI\n"
    )
    monkeypatch.setattr(parse, "MAX_FILE_BYTES", 8)
    assert parse.parse_file(path) == []


def test_a_symlink_to_a_big_file_masquerading_as_a_csv_is_capped(tmp_path, monkeypatch):
    """The same symlink lure as above, aimed at the CSV reader. A card can
    offer a `.csv`-suffixed link pointing anywhere on the filesystem just as
    easily as a `.sub` one."""
    target = tmp_path / "target.bin"
    target.write_bytes(b"x" * 4096)
    link = tmp_path / "lure.csv"
    os.symlink(target, link)

    assert link.is_file()
    monkeypatch.setattr(parse, "MAX_FILE_BYTES", 512)
    assert parse.parse_file(link) == []


def test_huge_irrelevant_card_does_not_walk_unbounded(tmp_path, monkeypatch):
    """A card that's mostly a large, unrelated file collection (dashcam
    footage, a photo library) has to stop the walk rather than visit every
    entry before any cap has a chance to apply. `sorted(rglob())` used to
    materialize the entire tree up front, defeating the cap that was
    supposed to bound it."""
    monkeypatch.setattr(sdcard, "_MAX_SCANNED_PER_ROOT", 100)
    card = tmp_path / "card"
    card.mkdir()
    for i in range(500):
        (card / f"IMG_{i:04d}.JPG").write_bytes(b"")
    (card / "a.csv").write_text("x")

    found = sdcard.scan_removable_media(roots=[card])
    assert found.truncated is True


# --- multi-interface pcapng ----------------------------------------------


def _pcapng(interfaces: list, packets: list) -> bytes:
    """`interfaces` is a list of link types; `packets` is (iface_id, payload)."""
    shb_body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    out = struct.pack("<II", 0x0A0D0D0A, 12 + len(shb_body)) + shb_body
    out += struct.pack("<I", 12 + len(shb_body))

    for dlt in interfaces:
        idb = struct.pack("<HHI", dlt, 0, 65535)
        out += struct.pack("<II", 0x00000001, 12 + len(idb)) + idb
        out += struct.pack("<I", 12 + len(idb))

    for iface_id, payload in packets:
        padded = payload + b"\x00" * ((4 - len(payload) % 4) % 4)
        body = struct.pack("<IIIII", iface_id, 0, 1700000000 * 1_000_000 & 0xFFFFFFFF,
                           len(payload), len(payload)) + padded
        out += struct.pack("<II", 0x00000006, 12 + len(body)) + body
        out += struct.pack("<I", 12 + len(body))
    return out


def _ble_adv(mac: str) -> bytes:
    addr = bytes(reversed(bytes(int(p, 16) for p in mac.split(":"))))
    body = addr + bytes([2, 0x01, 0x06])
    return struct.pack("<I", 0x8E89BED6) + bytes([0x40, len(body)]) + body


def test_pcapng_decodes_each_interface_with_its_own_link_type(tmp_path):
    """A pcapng can describe several interfaces. Freezing the first one's link
    type for the whole file silently drops every packet from the others, and
    reports the file as simply empty, which is the one thing this module's
    design says it must never do."""
    path = tmp_path / "multi.pcapng"
    path.write_bytes(_pcapng(
        interfaces=[127, 251],                       # radiotap, then BLE
        packets=[(1, _ble_adv("C0:FF:EE:00:11:22"))],  # the packet is on BLE
    ))

    result = pcap.read_pcap(path)
    assert result.error is None
    assert len(result.sightings) == 1, "the BLE advertisement was dropped"
    assert result.sightings[0].bssid == "C0:FF:EE:00:11:22"
    # Only types that actually carried packets are recorded; interface 0 was
    # declared but empty, so it never shows up.
    assert result.link_types == {251}


def test_pcapng_reports_the_interface_it_could_not_read(tmp_path):
    path = tmp_path / "mixed.pcapng"
    path.write_bytes(_pcapng(
        interfaces=[251, 1],                          # BLE, then Ethernet
        packets=[(0, _ble_adv("C0:FF:EE:00:11:22")),
                 (1, b"\x00" * 64)],
    ))
    result = pcap.read_pcap(path)
    assert len(result.sightings) == 1
    assert result.error is None
    assert result.note is not None
    assert "Ethernet" in result.note


def test_a_wholly_undecodable_pcapng_is_still_an_error(tmp_path):
    path = tmp_path / "eth.pcapng"
    path.write_bytes(_pcapng(interfaces=[1], packets=[(0, b"\x00" * 64)]))
    result = pcap.read_pcap(path)
    assert result.sightings == []
    assert result.error is not None
    assert "Ethernet" in result.error


def test_the_pcap_note_reaches_the_ingest_report(tmp_path):
    path = tmp_path / "mixed.pcapng"
    path.write_bytes(_pcapng(
        interfaces=[251, 1],
        packets=[(0, _ble_adv("C0:FF:EE:00:11:22")), (1, b"\x00" * 64)],
    ))
    result = ingest.ingest_paths([path])
    assert any("cannot" in note for note in result.notes)


# --- malformed but plausible ---------------------------------------------


def test_pcapng_with_a_packet_on_an_interface_that_does_not_exist(tmp_path):
    path = tmp_path / "bad.pcapng"
    path.write_bytes(_pcapng(interfaces=[251], packets=[(7, _ble_adv("AA:BB:CC:DD:EE:FF"))]))
    result = pcap.read_pcap(path)
    assert isinstance(result.sightings, list)


def test_flipper_file_of_random_bytes(tmp_path):
    path = tmp_path / "junk.sub"
    path.write_bytes(bytes(range(256)) * 8)
    assert isinstance(flipper.parse_flipper_file(path), list)


def test_nmea_file_of_random_bytes(tmp_path):
    path = tmp_path / "junk.nmea"
    path.write_bytes(bytes(range(256)) * 8)
    assert gps.parse_nmea(path) == []


def test_csv_file_of_random_bytes(tmp_path):
    path = tmp_path / "junk.csv"
    path.write_bytes(bytes(range(256)) * 8)
    assert isinstance(parse.parse_file(path), list)


# --- dedup on hostile/corrupted data ---------------------------------------
# This is the "quietly produce a wrong answer" half of this module's brief:
# nothing here crashes, but a bad reading was winning a merge it shouldn't.


def test_a_row_with_unparseable_rssi_cannot_outrank_a_real_reading(tmp_path):
    """Sighting.rssi has to be some int even when the CSV cell was garbage,
    and 0 was the fallback, but 0 reads as a *stronger* signal than any real
    (negative) dBm reading, so a single corrupted row could silently replace
    a whole station's real signal strength on merge."""
    content = (
        "WigleWifi-1.4,appRelease=1.0\nMAC,SSID,AuthMode,FirstSeen,Channel,RSSI,"
        "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
        "AA:BB:CC:DD:EE:FF,Net,[WPA2_PSK],2024-06-01 12:00:00,6,-88,33.4,-112.0,340,5,WIFI\n"
        "AA:BB:CC:DD:EE:FF,Net,[WPA2_PSK],2024-06-01 12:00:05,6,not-a-number,33.4,-112.0,340,5,WIFI\n"
    )
    path = tmp_path / "corrupt_rssi.csv"
    path.write_text(content)
    merged = parse.dedup(parse.parse_file(path))
    assert len(merged) == 1
    assert merged[0].rssi == -88, "a fabricated 0 dBm sentinel beat a real -88 dBm reading"
