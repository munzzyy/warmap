"""Packet capture decoding: pcap and pcapng containers, radiotap, 802.11
beacons, RSN encryption classification, and BLE advertisements.

Fixtures are built byte by byte rather than checked in as binary blobs, so
what each test is actually feeding the parser is readable, and a change to
the frame layout shows up as a diff instead of an opaque file swap.
"""

from __future__ import annotations

import struct

from warmap import pcap
from warmap.models import (
    ENC_OPEN,
    ENC_WEP,
    ENC_WPA,
    ENC_WPA2,
    ENC_WPA23_MIXED,
    ENC_WPA3,
    GEO_NONE,
    TYPE_BLE,
    TYPE_WIFI,
)

# --- builders ------------------------------------------------------------


def _mac_bytes(mac: str) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


def _pcap_file(dlt: int, packets: list, nanosecond: bool = False) -> bytes:
    magic = 0xA1B23C4D if nanosecond else 0xA1B2C3D4
    out = struct.pack("<IHHiIII", magic, 2, 4, 0, 0, 65535, dlt)
    for ts_sec, ts_frac, payload in packets:
        out += struct.pack("<IIII", ts_sec, ts_frac, len(payload), len(payload))
        out += payload
    return out


def _pcapng_file(dlt: int, packets: list) -> bytes:
    # Section header: byte-order magic, version, section length (-1).
    shb_body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    out = struct.pack("<II", 0x0A0D0D0A, 12 + len(shb_body)) + shb_body
    out += struct.pack("<I", 12 + len(shb_body))

    idb_body = struct.pack("<HHI", dlt, 0, 65535)
    out += struct.pack("<II", 0x00000001, 12 + len(idb_body)) + idb_body
    out += struct.pack("<I", 12 + len(idb_body))

    for ts_sec, ts_usec, payload in packets:
        raw_ts = ts_sec * 1_000_000 + ts_usec
        padded = payload + b"\x00" * ((4 - len(payload) % 4) % 4)
        epb_body = struct.pack(
            "<IIIII", 0, raw_ts >> 32, raw_ts & 0xFFFFFFFF, len(payload), len(payload)
        ) + padded
        out += struct.pack("<II", 0x00000006, 12 + len(epb_body)) + epb_body
        out += struct.pack("<I", 12 + len(epb_body))
    return out


def _radiotap(freq: int = 2437, rssi: int = -42) -> bytes:
    present = (1 << 3) | (1 << 5)  # CHANNEL, DBM_ANTSIGNAL
    fields = struct.pack("<HH", freq, 0) + struct.pack("<b", rssi)
    return struct.pack("<BBHI", 0, 0, 8 + len(fields), present) + fields


def _rsn(akm_types: list) -> bytes:
    body = struct.pack("<H", 1)          # version
    body += b"\x00\x0f\xac\x04"          # group cipher: CCMP
    body += struct.pack("<H", 1)         # pairwise count
    body += b"\x00\x0f\xac\x04"          # pairwise: CCMP
    body += struct.pack("<H", len(akm_types))
    for akm in akm_types:
        body += b"\x00\x0f\xac" + bytes([akm])
    return body


def _beacon(bssid="AA:BB:CC:DD:EE:FF", ssid="TestNet", channel=6,
            rsn=None, wpa1=False, privacy=False, subtype=8) -> bytes:
    frame = struct.pack("<H", subtype << 4)      # frame control
    frame += struct.pack("<H", 0)                # duration
    frame += _mac_bytes("FF:FF:FF:FF:FF:FF")     # addr1
    frame += _mac_bytes(bssid)                   # addr2
    frame += _mac_bytes(bssid)                   # addr3 (BSSID)
    frame += struct.pack("<H", 0)                # sequence
    frame += b"\x00" * 8                         # timestamp
    frame += struct.pack("<H", 100)              # advertising interval
    frame += struct.pack("<H", 0x0010 if privacy else 0x0000)  # capability

    encoded = ssid.encode("utf-8")
    frame += bytes([0, len(encoded)]) + encoded
    if channel is not None:
        frame += bytes([3, 1, channel])
    if rsn is not None:
        frame += bytes([48, len(rsn)]) + rsn
    if wpa1:
        vendor = b"\x00\x50\xf2\x01" + b"\x01\x00" + b"\x00\x50\xf2\x02"
        frame += bytes([221, len(vendor)]) + vendor
    return frame


def _ad(ad_type: int, value: bytes) -> bytes:
    return bytes([1 + len(value), ad_type]) + value


def _ble_adv(mac="AA:BB:CC:DD:EE:FF", ads=b"", pdu_type=0, tx_random=True) -> bytes:
    body = bytes(reversed(_mac_bytes(mac))) + ads
    header = bytes([pdu_type | (0x40 if tx_random else 0), len(body)])
    return struct.pack("<I", 0x8E89BED6) + header + body


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- containers ----------------------------------------------------------

def test_reads_classic_pcap(tmp_path):
    frame = _radiotap() + _beacon()
    path = _write(tmp_path, "a.pcap", _pcap_file(127, [(1700000000, 0, frame)]))
    result = pcap.read_pcap(path)
    assert result.error is None
    assert result.link_type == 127
    assert len(result.sightings) == 1


def test_reads_pcapng(tmp_path):
    frame = _radiotap() + _beacon(ssid="NgNet")
    path = _write(tmp_path, "a.pcapng", _pcapng_file(127, [(1700000000, 0, frame)]))
    result = pcap.read_pcap(path)
    assert result.error is None
    assert result.sightings[0].ssid == "NgNet"


def test_big_endian_pcap(tmp_path):
    frame = _radiotap() + _beacon()
    body = struct.pack(">IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127)
    body += struct.pack(">IIII", 1700000000, 0, len(frame), len(frame)) + frame
    result = pcap.read_pcap(_write(tmp_path, "be.pcap", body))
    assert result.link_type == 127
    assert len(result.sightings) == 1


def test_nanosecond_pcap_timestamps(tmp_path):
    frame = _radiotap() + _beacon()
    path = _write(
        tmp_path, "n.pcap", _pcap_file(127, [(1700000000, 500_000_000, frame)], nanosecond=True)
    )
    result = pcap.read_pcap(path)
    assert len(result.sightings) == 1
    assert result.sightings[0].first_seen  # a real timestamp, not empty


def test_unsupported_link_type_is_reported_not_silently_empty(tmp_path):
    """The failure mode that matters: an Ethernet capture must say so rather
    than look like a capture with nothing in it."""
    path = _write(tmp_path, "eth.pcap", _pcap_file(1, [(1700000000, 0, b"\x00" * 64)]))
    result = pcap.read_pcap(path)
    assert result.sightings == []
    assert result.error is not None
    assert "Ethernet" in result.error
    assert not result.supported


def test_not_a_capture_file_is_reported(tmp_path):
    path = _write(tmp_path, "x.pcap", b"this is not a pcap file at all, not even close")
    result = pcap.read_pcap(path)
    assert result.error is not None


def test_missing_file_is_reported(tmp_path):
    result = pcap.read_pcap(tmp_path / "nope.pcap")
    assert result.error is not None
    assert result.sightings == []


def test_truncated_packet_does_not_raise(tmp_path):
    frame = _radiotap() + _beacon()
    data = _pcap_file(127, [(1700000000, 0, frame)])
    result = pcap.read_pcap(_write(tmp_path, "t.pcap", data[:-10]))
    assert isinstance(result.sightings, list)


def test_absurd_packet_length_is_rejected(tmp_path):
    """A corrupt length field must not turn into a huge read."""
    data = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127)
    data += struct.pack("<IIII", 1700000000, 0, 0xFFFFFFF0, 0xFFFFFFF0)
    result = pcap.read_pcap(_write(tmp_path, "bad.pcap", data))
    assert result.sightings == []


# --- 802.11 --------------------------------------------------------------

def test_beacon_fields_extracted(tmp_path):
    frame = _radiotap(freq=2437, rssi=-55) + _beacon(
        bssid="11:22:33:44:55:66", ssid="CoffeeShop", channel=6
    )
    result = pcap.read_pcap(_write(tmp_path, "a.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    record = result.sightings[0]
    assert record.type == TYPE_WIFI
    assert record.bssid == "11:22:33:44:55:66"
    assert record.ssid == "CoffeeShop"
    assert record.channel == 6
    assert record.rssi == -55
    assert record.frequency == 2437


def test_probe_response_also_decoded(tmp_path):
    frame = _radiotap() + _beacon(subtype=5, ssid="ProbeNet")
    result = pcap.read_pcap(_write(tmp_path, "a.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    assert result.sightings[0].ssid == "ProbeNet"


def test_data_frames_are_ignored(tmp_path):
    data_frame = struct.pack("<H", (0 << 4) | (2 << 2)) + b"\x00" * 40
    frame = _radiotap() + data_frame
    result = pcap.read_pcap(_write(tmp_path, "a.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    assert result.sightings == []


def test_hidden_ssid_yields_empty_name(tmp_path):
    frame = _radiotap() + _beacon(ssid="")
    result = pcap.read_pcap(_write(tmp_path, "a.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    assert result.sightings[0].ssid == ""


def test_repeat_beacons_collapse_and_count(tmp_path):
    packets = [
        (1700000000, 0, _radiotap(rssi=-80) + _beacon()),
        (1700000001, 0, _radiotap(rssi=-40) + _beacon()),
        (1700000002, 0, _radiotap(rssi=-70) + _beacon()),
    ]
    result = pcap.read_pcap(_write(tmp_path, "a.pcap", _pcap_file(127, packets)))
    assert len(result.sightings) == 1
    assert result.sightings[0].times_seen == 3
    assert result.sightings[0].rssi == -40  # strongest wins


def test_no_radiotap_link_type_still_decodes(tmp_path):
    result = pcap.read_pcap(
        _write(tmp_path, "a.pcap", _pcap_file(105, [(1700000000, 0, _beacon(ssid="Bare"))]))
    )
    assert result.sightings[0].ssid == "Bare"


def test_pcap_records_have_no_location(tmp_path):
    """A pcap has timestamps but no GPS, so records come out unplaced and get
    positioned later against a track."""
    frame = _radiotap() + _beacon()
    result = pcap.read_pcap(_write(tmp_path, "a.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    assert result.sightings[0].geo_source == GEO_NONE
    assert not result.sightings[0].has_location


# --- encryption from the RSN element -------------------------------------

def _enc_for(tmp_path, **beacon_kwargs) -> str:
    frame = _radiotap() + _beacon(**beacon_kwargs)
    result = pcap.read_pcap(
        _write(tmp_path, "e.pcap", _pcap_file(127, [(1700000000, 0, frame)]))
    )
    return result.sightings[0].enc_bucket


def test_open_network(tmp_path):
    assert _enc_for(tmp_path, privacy=False) == ENC_OPEN


def test_wep_is_privacy_bit_with_no_rsn(tmp_path):
    assert _enc_for(tmp_path, privacy=True) == ENC_WEP


def test_wpa1_vendor_element(tmp_path):
    assert _enc_for(tmp_path, privacy=True, wpa1=True) == ENC_WPA


def test_wpa2_psk(tmp_path):
    assert _enc_for(tmp_path, privacy=True, rsn=_rsn([2])) == ENC_WPA2


def test_wpa3_sae(tmp_path):
    assert _enc_for(tmp_path, privacy=True, rsn=_rsn([8])) == ENC_WPA3


def test_wpa2_wpa3_transition_mode(tmp_path):
    assert _enc_for(tmp_path, privacy=True, rsn=_rsn([2, 8])) == ENC_WPA23_MIXED


def test_owe_buckets_as_open(tmp_path):
    """Enhanced Open is encrypted but unauthenticated. From a wardriving
    point of view it's an open network you can join."""
    assert _enc_for(tmp_path, privacy=True, rsn=_rsn([18])) == ENC_OPEN


def test_truncated_rsn_element_falls_back(tmp_path):
    assert _enc_for(tmp_path, privacy=True, rsn=b"\x01\x00") == ENC_WEP


# --- Bluetooth LE --------------------------------------------------------

def test_ble_advertisement_decoded(tmp_path):
    ads = _ad(0x01, b"\x06") + _ad(0x09, b"MySpeaker") + _ad(0x0A, struct.pack("<b", -8))
    packet = _ble_adv(mac="C0:FF:EE:00:11:22", ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    record = result.sightings[0]
    assert record.type == TYPE_BLE
    assert record.bssid == "C0:FF:EE:00:11:22"
    assert record.ssid == "MySpeaker"
    assert record.meta["tx_power"] == -8
    assert record.meta["pdu_type"] == "ADV_IND"


def test_ble_service_uuids_decoded(tmp_path):
    ads = _ad(0x03, struct.pack("<H", 0x180F) + struct.pack("<H", 0x180D))
    packet = _ble_adv(ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    meta = result.sightings[0].meta
    assert 0x180F in meta["service_uuids"]
    assert "Battery" in " ".join(meta["services"])


def test_ble_manufacturer_data_identifies_apple(tmp_path):
    ads = _ad(0xFF, struct.pack("<H", 0x004C) + b"\x12\x19\x00")
    packet = _ble_adv(ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    meta = result.sightings[0].meta
    assert meta["company_id"] == 0x004C
    assert "Apple" in meta["company"]
    assert "Find My" in meta["tracker"]
    assert meta["tracker_basis"] == "advertisement"


def test_ble_tile_identified_by_service_uuid(tmp_path):
    ads = _ad(0x03, struct.pack("<H", 0xFEED))
    packet = _ble_adv(ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    assert result.sightings[0].meta["tracker"] == "Tile"


def test_ble_128bit_dult_uuid_decoded_and_flagged(tmp_path):
    # A DULT tracker's Accessory Non-Owner Service is a 128-bit UUID, sent
    # little-endian on the wire in a 0x07 (complete 128-bit UUID list) AD.
    anos_be = bytes.fromhex("1519000112f4c22688ed2ac5579f2a85")
    ads = _ad(0x07, bytes(reversed(anos_be)))
    packet = _ble_adv(mac="C7:11:22:33:44:55", ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    meta = result.sightings[0].meta
    assert "15190001-12f4-c226-88ed-2ac5579f2a85" in meta["service_uuids_128"]
    assert "DULT" in meta["tracker"]


def test_ble_txadd_bit_gives_the_real_address_type(tmp_path):
    """A pcap actually carries the public/random flag, unlike a CSV, so the
    record should say so rather than fall back to the address-bit guess."""
    public = _ble_adv(mac="B8:27:EB:11:22:33", tx_random=False)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, public)])))
    assert result.sightings[0].meta["address_type_actual"] == "public"

    random_addr = _ble_adv(mac="4C:11:22:33:44:55", tx_random=True)
    result = pcap.read_pcap(_write(tmp_path, "c.pcap", _pcap_file(251, [(1700000000, 0, random_addr)])))
    assert result.sightings[0].meta["address_type_actual"] == "random"


def test_ble_with_phdr_link_type(tmp_path):
    phdr = bytes([37]) + struct.pack("<b", -60) + b"\x00" * 8
    packet = phdr + _ble_adv(ads=_ad(0x09, b"Phdr"))
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(256, [(1700000000, 0, packet)])))
    record = result.sightings[0]
    assert record.ssid == "Phdr"
    assert record.rssi == -60
    assert record.channel == 37


def test_ble_non_advertising_pdu_ignored(tmp_path):
    packet = _ble_adv(pdu_type=0x3)  # SCAN_REQ, not an advertisement
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    assert result.sightings == []


def test_ble_truncated_ad_structure_does_not_raise(tmp_path):
    ads = bytes([20, 0x09]) + b"short"  # claims 20 bytes, supplies 5
    packet = _ble_adv(ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    assert isinstance(result.sightings, list)


def test_ble_zero_length_ad_terminates_cleanly(tmp_path):
    ads = _ad(0x09, b"Name") + b"\x00\x00\x00"
    packet = _ble_adv(ads=ads)
    result = pcap.read_pcap(_write(tmp_path, "b.pcap", _pcap_file(251, [(1700000000, 0, packet)])))
    assert result.sightings[0].ssid == "Name"


def test_is_pcap_file():
    from pathlib import Path
    assert pcap.is_pcap_file(Path("x.pcap"))
    assert pcap.is_pcap_file(Path("x.PCAPNG"))
    assert not pcap.is_pcap_file(Path("x.csv"))


# --- probe requests ------------------------------------------------------

def _probe_request(station="AA:BB:CC:DD:EE:01", ssid="HomeNet") -> bytes:
    """A probe request has NO fixed parameters, so tagged elements start right
    after the 24-byte MAC header, 12 bytes earlier than in an AP's advertisement frame."""
    frame = struct.pack("<H", 4 << 4)            # management, subtype 4
    frame += struct.pack("<H", 0)                # duration
    frame += _mac_bytes("FF:FF:FF:FF:FF:FF")     # addr1: broadcast
    frame += _mac_bytes(station)                 # addr2: the client
    frame += _mac_bytes("FF:FF:FF:FF:FF:FF")     # addr3
    frame += struct.pack("<H", 0)                # sequence
    encoded = ssid.encode("utf-8")
    frame += bytes([0, len(encoded)]) + encoded
    return frame


def test_probe_request_becomes_a_client_record(tmp_path):
    frame = _radiotap(rssi=-61) + _probe_request(station="11:22:33:44:55:66", ssid="HomeNet")
    result = pcap.read_pcap(_write(tmp_path, "p.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    record = result.sightings[0]
    assert record.type == "CLIENT"
    assert record.bssid == "11:22:33:44:55:66"
    assert record.ssid == "HomeNet"
    assert record.rssi == -61
    assert record.meta["probing_for"] == ["HomeNet"]


def test_one_client_probing_for_several_networks_is_one_record(tmp_path):
    """A phone asks for every network it remembers. The useful unit is the
    station and its list, not one record per frame."""
    packets = [
        (1700000000 + i, 0, _radiotap() + _probe_request(station="11:22:33:44:55:66", ssid=name))
        for i, name in enumerate(["HomeNet", "CoffeeShop", "Airport_WiFi"])
    ]
    result = pcap.read_pcap(_write(tmp_path, "p.pcap", _pcap_file(127, packets)))
    assert len(result.sightings) == 1
    record = result.sightings[0]
    assert record.times_seen == 3
    assert record.meta["probing_for"] == ["HomeNet", "CoffeeShop", "Airport_WiFi"]


def test_wildcard_probe_is_labelled_not_left_blank(tmp_path):
    frame = _radiotap() + _probe_request(ssid="")
    result = pcap.read_pcap(_write(tmp_path, "p.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    record = result.sightings[0]
    assert record.ssid == ""
    assert "Wildcard" in record.meta["note"]
    assert "probing_for" not in record.meta


def test_a_client_is_not_confused_with_an_access_point(tmp_path):
    """Both are Wi-Fi and both have a MAC, but one was asking for a network
    and the other was offering one. Filing them under the same type would be
    the same class of error as calling an inference a measurement."""
    packets = [
        (1700000000, 0, _radiotap() + _beacon(bssid="AA:BB:CC:DD:EE:FF", ssid="HomeNet")),
        (1700000001, 0, _radiotap() + _probe_request(station="11:22:33:44:55:66", ssid="HomeNet")),
    ]
    result = pcap.read_pcap(_write(tmp_path, "p.pcap", _pcap_file(127, packets)))
    assert {r.type for r in result.sightings} == {"WIFI", "CLIENT"}


def test_probe_request_resolves_the_client_vendor(tmp_path):
    frame = _radiotap() + _probe_request(station="B8:27:EB:11:22:33")
    result = pcap.read_pcap(_write(tmp_path, "p.pcap", _pcap_file(127, [(1700000000, 0, frame)])))
    assert "Raspberry Pi" in result.sightings[0].vendor


def test_probing_client_mac_randomization_is_reported_as_wifi_not_bluetooth(tmp_path):
    """The public/random-resolvable taxonomy is Bluetooth's and means nothing
    for Wi-Fi. The equivalent question for a probing station is whether the
    MAC is locally administered, which is how phones hide while scanning."""
    randomized = _radiotap() + _probe_request(station="DA:11:22:33:44:55")
    result = pcap.read_pcap(_write(tmp_path, "r.pcap", _pcap_file(127, [(1700000000, 0, randomized)])))
    meta = result.sightings[0].meta
    assert meta["mac_randomized"] is True
    assert "Randomized MAC" in meta["address_note"]
    assert "address_type" not in meta

    real = _radiotap() + _probe_request(station="B8:27:EB:11:22:33")
    result = pcap.read_pcap(_write(tmp_path, "h.pcap", _pcap_file(127, [(1700000000, 0, real)])))
    meta = result.sightings[0].meta
    assert meta["mac_randomized"] is False
    assert "Real hardware MAC" in meta["address_note"]


def test_unset_board_clock_reads_as_no_time_not_1969(tmp_path):
    """A Marauder writes its pcap before the GPS has given it a clock, so the
    packet is stamped a few seconds after the Unix epoch. That reached the
    records table as "1969-12-31" and pulled the capture's whole time range
    back to 1969 with it. An unknown time has to be blank."""
    frame = _radiotap() + _beacon(ssid="EarlyNet")
    path = _write(tmp_path, "ap_sta_0.pcap", _pcap_file(127, [(18, 0, frame)]))
    result = pcap.read_pcap(path)
    assert len(result.sightings) == 1
    assert result.sightings[0].first_seen == ""


def test_a_real_timestamp_still_survives(tmp_path):
    frame = _radiotap() + _beacon(ssid="LaterNet")
    path = _write(tmp_path, "b.pcap", _pcap_file(127, [(1784000000, 0, frame)]))
    result = pcap.read_pcap(path)
    assert result.sightings[0].first_seen.startswith("2026-")


def test_unset_clock_in_pcapng_reads_as_no_time_too(tmp_path):
    frame = _radiotap() + _beacon(ssid="NgEarly")
    path = _write(tmp_path, "c.pcapng", _pcapng_file(127, [(18, 0, frame)]))
    result = pcap.read_pcap(path)
    assert len(result.sightings) == 1
    assert result.sightings[0].first_seen == ""
