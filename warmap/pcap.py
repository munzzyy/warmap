"""Reading capture files: classic pcap and pcapng, 802.11 and Bluetooth LE.

A wardrive CSV gives you a MAC, a name and a signal strength. A pcap gives you
the actual frames, which is a different quality of information: the real
encryption suites from the RSN element rather than a guess from a text field,
and for Bluetooth the entire advertisement (service UUIDs, manufacturer data,
TX power, appearance), which is what makes it possible to say "that's an
AirTag" instead of "that's a random MAC".

Scope is deliberately narrow. This decodes what warmap can put on a map:

* 802.11 beacons and probe responses (DLT 105 / 127 / 119 / 163)
* Bluetooth LE advertising PDUs (DLT 251 / 256)

Everything else in the file is skipped. An unrecognized link type is
**reported, not guessed at**: `read_pcap` returns the DLT it found so the
caller can say "this file is link type 195, warmap can't read that" rather
than returning an empty list that looks like an empty capture.

Nothing here trusts the file. Every length comes off disk and is bounds
checked before use; a truncated or hostile capture yields fewer records, never
an exception and never a read past the buffer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from warmap import ble, radio
from warmap.models import (
    ENC_OPEN,
    ENC_UNKNOWN,
    ENC_WEP,
    ENC_WPA,
    ENC_WPA2,
    ENC_WPA23_MIXED,
    ENC_WPA3,
    GEO_NONE,
    TYPE_BLE,
    TYPE_CLIENT,
    TYPE_WIFI,
    Sighting,
)

# Link types this module understands.
DLT_IEEE802_11 = 105
DLT_IEEE802_11_RADIOTAP = 127
DLT_IEEE802_11_PRISM = 119
DLT_IEEE802_11_AVS = 163
DLT_BLUETOOTH_LE_LL = 251
DLT_BLUETOOTH_LE_LL_WITH_PHDR = 256

WIFI_DLTS = frozenset({
    DLT_IEEE802_11, DLT_IEEE802_11_RADIOTAP, DLT_IEEE802_11_PRISM, DLT_IEEE802_11_AVS,
})
BLE_DLTS = frozenset({DLT_BLUETOOTH_LE_LL, DLT_BLUETOOTH_LE_LL_WITH_PHDR})

DLT_NAMES = {
    1: "Ethernet",
    105: "802.11 (no radio header)",
    119: "802.11 + Prism header",
    127: "802.11 + radiotap",
    163: "802.11 + AVS header",
    251: "Bluetooth LE link layer",
    256: "Bluetooth LE link layer with PHDR",
    272: "Nordic BLE sniffer",
}

# A guard against a corrupt length field turning into a multi-gigabyte read.
MAX_PACKETS = 500_000
MAX_PACKET_BYTES = 262_144

# The whole file is read into memory before anything is parsed, so the
# per-packet caps above do nothing about a 10 GB file. Checked against the size
# on disk first: an import comes off an SD card that may hold anything, and
# running the machine out of memory from a menu click is not a failure mode
# worth having.
MAX_FILE_BYTES = 256 * 1024 * 1024

# A packet stamped before this is not a capture from 1970, it's a board that
# wrote the file before its clock was set, so the "time" is really seconds of
# uptime. A Marauder does exactly this: a real ap_sta_0.pcap off the BFFB card
# carried a packet at epoch+18s, which reached the records table as
# "1969-12-31" and would have dragged the capture's whole time range back with
# it. An unknown time has to read as unknown, not as a date. 2000-01-01 UTC is
# comfortably before any capture this app can be shown and comfortably after
# any unset-clock value.
MIN_PLAUSIBLE_EPOCH = 946_684_800


@dataclass
class PcapResult:
    sightings: list = field(default_factory=list)
    link_type: Optional[int] = None
    packets_read: int = 0
    packets_decoded: int = 0
    error: Optional[str] = None
    note: Optional[str] = None
    # A pcapng can describe several interfaces, each with its own link type.
    # Both are tracked so a file mixing a readable radio with an unreadable
    # one reports the skip instead of quietly dropping half the capture.
    link_types: set = field(default_factory=set)
    skipped_link_types: set = field(default_factory=set)

    @property
    def link_type_name(self) -> str:
        if self.link_type is None:
            return "unknown"
        return DLT_NAMES.get(self.link_type, f"DLT {self.link_type}")

    @property
    def supported(self) -> bool:
        return bool(self.link_types & (WIFI_DLTS | BLE_DLTS))


# --- container parsing ---------------------------------------------------

def _packet_time(epoch: float) -> Optional[datetime]:
    """A packet's wall-clock time, or None when the capturing device clearly
    didn't have one. See MIN_PLAUSIBLE_EPOCH."""
    if epoch < MIN_PLAUSIBLE_EPOCH:
        return None
    return datetime.fromtimestamp(epoch)


def _iter_pcap_packets(data: bytes):
    """(timestamp, payload) for each record in a classic pcap file.

    Yields ('LINKTYPE', dlt) first so the caller learns the link type before
    any packets.
    """
    if len(data) < 24:
        return
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian, nano = "<", False
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian, nano = ">", False
    elif magic == b"\x4d\x3c\xb2\xa1":
        endian, nano = "<", True
    elif magic == b"\xa1\xb2\x3c\x4d":
        endian, nano = ">", True
    else:
        return

    dlt = struct.unpack(endian + "I", data[20:24])[0]

    offset = 24
    count = 0
    size = len(data)
    while offset + 16 <= size and count < MAX_PACKETS:
        ts_sec, ts_frac, incl_len, _orig_len = struct.unpack(
            endian + "IIII", data[offset:offset + 16]
        )
        offset += 16
        if incl_len > MAX_PACKET_BYTES or offset + incl_len > size:
            return
        payload = data[offset:offset + incl_len]
        offset += incl_len
        count += 1
        try:
            when = _packet_time(ts_sec + (ts_frac / 1e9 if nano else ts_frac / 1e6))
        except (OverflowError, OSError, ValueError):
            when = None
        yield (when, payload, dlt)


def _iter_pcapng_packets(data: bytes):
    """Same contract as `_iter_pcap_packets`, for pcapng.

    Only the blocks that matter are decoded: the section header (for byte
    order), interface descriptions (for link type and timestamp resolution),
    and enhanced packet blocks.
    """
    size = len(data)
    if size < 12 or data[:4] != b"\x0a\x0d\x0d\x0a":
        return

    byte_order = struct.unpack("<I", data[8:12])[0]
    endian = "<" if byte_order == 0x1A2B3C4D else ">"
    if byte_order not in (0x1A2B3C4D, 0x4D3C2B1A):
        return

    offset = 0
    count = 0
    interfaces: list[tuple[int, int]] = []  # (linktype, tsresol)

    while offset + 12 <= size and count < MAX_PACKETS:
        block_type, block_len = struct.unpack(endian + "II", data[offset:offset + 8])
        if block_len < 12 or offset + block_len > size:
            return
        body = data[offset + 8:offset + block_len - 4]

        if block_type == 0x00000001 and len(body) >= 8:  # interface description
            linktype = struct.unpack(endian + "H", body[0:2])[0]
            tsresol = 6
            # Walk options for if_tsresol (code 9); default is microseconds.
            opt = 8
            while opt + 4 <= len(body):
                code, length = struct.unpack(endian + "HH", body[opt:opt + 4])
                opt += 4
                if code == 0:
                    break
                if code == 9 and length >= 1 and opt < len(body):
                    tsresol = body[opt]
                opt += length + ((4 - length % 4) % 4)
            interfaces.append((linktype, tsresol))

        elif block_type == 0x00000006 and len(body) >= 20:  # enhanced packet
            iface_id, ts_high, ts_low, cap_len = struct.unpack(
                endian + "IIII", body[0:16]
            )
            if cap_len > MAX_PACKET_BYTES or 20 + cap_len > len(body):
                offset += block_len
                continue
            payload = body[20:20 + cap_len]
            linktype, tsresol = interfaces[iface_id] if iface_id < len(interfaces) else (None, 6)
            raw_ts = (ts_high << 32) | ts_low
            when = None
            try:
                divisor = (2 ** (tsresol & 0x7F)) if tsresol & 0x80 else (10 ** tsresol)
                when = _packet_time(raw_ts / divisor)
            except (OverflowError, OSError, ValueError, ZeroDivisionError):
                when = None
            count += 1
            # The packet's OWN interface decides how to decode it. Freezing the
            # first interface's link type for the whole file silently drops
            # every packet from any other radio in a multi-source capture.
            yield (when, payload, linktype)

        offset += block_len


# --- radiotap ------------------------------------------------------------

# (size, alignment) per radiotap present-bit, in bit order. Only the fields
# before the ones warmap reads need to be exact, walking stops at bit 14.
_RADIOTAP_FIELDS = [
    (8, 8),   # 0  TSFT
    (1, 1),   # 1  FLAGS
    (1, 1),   # 2  RATE
    (4, 2),   # 3  CHANNEL
    (2, 1),   # 4  FHSS
    (1, 1),   # 5  DBM_ANTSIGNAL
    (1, 1),   # 6  DBM_ANTNOISE
    (2, 2),   # 7  LOCK_QUALITY
    (2, 2),   # 8  TX_ATTENUATION
    (2, 2),   # 9  DB_TX_ATTENUATION
    (1, 1),   # 10 DBM_TX_POWER
    (1, 1),   # 11 ANTENNA
    (1, 1),   # 12 DB_ANTSIGNAL
    (1, 1),   # 13 DB_ANTNOISE
    (2, 2),   # 14 RX_FLAGS
]


def _parse_radiotap(data: bytes) -> tuple[int, Optional[float], Optional[int]]:
    """(header_length, frequency_mhz, rssi_dbm). Frequency and RSSI are None
    if the capture didn't include them."""
    if len(data) < 8:
        return (0, None, None)
    length = struct.unpack("<H", data[2:4])[0]
    if length < 8 or length > len(data):
        return (0, None, None)

    # The present bitmap repeats while bit 31 is set.
    present_words = []
    offset = 4
    while offset + 4 <= length:
        word = struct.unpack("<I", data[offset:offset + 4])[0]
        present_words.append(word)
        offset += 4
        if not word & (1 << 31):
            break

    if not present_words:
        return (length, None, None)

    present = present_words[0]
    pos = offset
    freq = None
    rssi = None

    for bit, (size, align) in enumerate(_RADIOTAP_FIELDS):
        if not present & (1 << bit):
            continue
        pos += (-pos) % align
        if pos + size > length:
            break
        if bit == 3:
            freq = float(struct.unpack("<H", data[pos:pos + 2])[0])
        elif bit == 5:
            rssi = struct.unpack("<b", data[pos:pos + 1])[0]
        pos += size
        if bit >= 5 and freq is not None and rssi is not None:
            break

    return (length, freq, rssi)


# --- 802.11 --------------------------------------------------------------

def _mac(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in data)


def _is_probe_request(frame: bytes) -> bool:
    if len(frame) < 24:
        return False
    fc = struct.unpack("<H", frame[0:2])[0]
    return ((fc >> 2) & 0x3) == 0 and ((fc >> 4) & 0xF) == 4


def _classify_rsn(rsn: bytes) -> Optional[str]:
    """Encryption bucket from an RSN information element body.

    The authentication suite list is what distinguishes the WPA generations:
    AKM type 2 is PSK (WPA2), 8 is SAE (WPA3), 18 is OWE (which is an open
    network with encryption, so it buckets as Open).
    """
    if len(rsn) < 8:
        return None
    pos = 2 + 4  # version, group cipher suite
    if len(rsn) < pos + 2:
        return None
    pair_count = struct.unpack("<H", rsn[pos:pos + 2])[0]
    pos += 2 + 4 * pair_count
    if len(rsn) < pos + 2:
        return None
    akm_count = struct.unpack("<H", rsn[pos:pos + 2])[0]
    pos += 2

    suites = set()
    for _ in range(akm_count):
        if pos + 4 > len(rsn):
            break
        suites.add(rsn[pos + 3])
        pos += 4

    has_sae = bool(suites & {8, 9})
    has_psk = bool(suites & {2, 6})
    if suites == {18}:
        return ENC_OPEN  # OWE / enhanced open
    if has_sae and has_psk:
        return ENC_WPA23_MIXED
    if has_sae:
        return ENC_WPA3
    if suites:
        return ENC_WPA2
    return None


def _parse_probe_request(frame: bytes) -> Optional[dict]:
    """The station address and the network name from a probe request.

    Different shape from beacons: a probe request has no fixed parameters, so
    the tagged elements start immediately after the 24-byte MAC header rather
    than 12 bytes further in. Reading it with the offsets beacons use yields
    plausible garbage, which is worse than failing.

    Address 2 is the transmitter, the client. An empty SSID is a wildcard
    probe ("anyone there?"); a named one means the device remembers that
    network and is actively looking for it.
    """
    if len(frame) < 24:
        return None
    station = _mac(frame[10:16])
    ssid = None

    pos = 24
    while pos + 2 <= len(frame):
        tag_id = frame[pos]
        tag_len = frame[pos + 1]
        pos += 2
        if pos + tag_len > len(frame):
            break
        if tag_id == 0:
            ssid = frame[pos:pos + tag_len].decode("utf-8", errors="replace").rstrip("\x00")
            break
        pos += tag_len

    return {"station": station, "ssid": ssid or ""}


def _parse_beacon(frame: bytes) -> Optional[dict]:
    """SSID, BSSID, channel and encryption from beacons and probe responses."""
    if len(frame) < 24:
        return None
    fc = struct.unpack("<H", frame[0:2])[0]
    ftype = (fc >> 2) & 0x3
    subtype = (fc >> 4) & 0xF
    if ftype != 0 or subtype not in (5, 8):  # management frames: probe response, beacons
        return None
    if len(frame) < 36:
        return None

    bssid = _mac(frame[16:22])
    capability = struct.unpack("<H", frame[34:36])[0]
    privacy = bool(capability & 0x0010)

    ssid = ""
    channel = None
    bucket = None
    has_wpa1 = False

    pos = 36
    while pos + 2 <= len(frame):
        tag_id = frame[pos]
        tag_len = frame[pos + 1]
        pos += 2
        if pos + tag_len > len(frame):
            break
        body = frame[pos:pos + tag_len]
        pos += tag_len

        if tag_id == 0:
            ssid = body.decode("utf-8", errors="replace").rstrip("\x00")
        elif tag_id == 3 and tag_len >= 1:
            channel = body[0]
        elif tag_id == 48:
            bucket = _classify_rsn(body) or bucket
        elif tag_id == 221 and tag_len >= 4 and body[:4] == b"\x00\x50\xf2\x01":
            has_wpa1 = True

    if bucket is None:
        if has_wpa1:
            bucket = ENC_WPA
        elif privacy:
            bucket = ENC_WEP
        else:
            bucket = ENC_OPEN

    return {"bssid": bssid, "ssid": ssid, "channel": channel, "enc": bucket}


# --- Bluetooth LE --------------------------------------------------------

_ADV_PDU_TYPES = {
    0x0: "ADV_IND",
    0x1: "ADV_DIRECT_IND",
    0x2: "ADV_NONCONN_IND",
    0x4: "SCAN_RSP",
    0x6: "ADV_SCAN_IND",
}


def _uuid128_le(raw: bytes) -> str:
    """A 128-bit UUID from its 16 advertised bytes (little-endian on the wire)
    into the canonical 8-4-4-4-12 string."""
    h = bytes(reversed(raw)).hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _parse_ad_structures(data: bytes) -> dict:
    """Decode the AD structures in an advertisement payload.

    Format is a chain of `length, type, value` where length counts the type
    byte. A zero length terminates the chain (it's the padding at the end).
    """
    out: dict = {}
    uuids: list[int] = []
    uuids128: list[str] = []
    pos = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            break
        pos += 1
        if pos + length > len(data):
            break
        ad_type = data[pos]
        value = data[pos + 1:pos + length]
        pos += length

        if ad_type == 0x01 and value:
            out["flags"] = value[0]
        elif ad_type in (0x02, 0x03) and len(value) >= 2:
            for i in range(0, len(value) - 1, 2):
                uuids.append(struct.unpack("<H", value[i:i + 2])[0])
        elif ad_type in (0x06, 0x07) and len(value) >= 16:
            # Incomplete/complete list of 128-bit service class UUIDs. A DULT
            # tracker's Accessory Non-Owner Service shows up here, and a
            # 16-bit-only parser was blind to it.
            for i in range(0, len(value) - 15, 16):
                uuids128.append(_uuid128_le(value[i:i + 16]))
        elif ad_type in (0x08, 0x09) and value:
            out["name"] = value.decode("utf-8", errors="replace").rstrip("\x00")
        elif ad_type == 0x0A and value:
            out["tx_power"] = struct.unpack("<b", value[0:1])[0]
        elif ad_type == 0x16 and len(value) >= 2:
            uuid16 = struct.unpack("<H", value[0:2])[0]
            uuids.append(uuid16)
            out.setdefault("service_data", {})[f"0x{uuid16:04X}"] = value[2:].hex().upper()
        elif ad_type == 0x19 and len(value) >= 2:
            out["appearance"] = struct.unpack("<H", value[0:2])[0]
        elif ad_type == 0xFF and len(value) >= 2:
            out["company_id"] = struct.unpack("<H", value[0:2])[0]
            out["mfg_data"] = value[2:].hex().upper()

    if uuids:
        out["service_uuids"] = sorted(set(uuids))
    if uuids128:
        out["service_uuids_128"] = sorted(set(uuids128))
    return out


def _parse_ble_ll(payload: bytes, dlt: int) -> Optional[dict]:
    """Advertising address and decoded advertisement from a BLE link-layer
    packet."""
    rssi = None
    channel = None

    if dlt == DLT_BLUETOOTH_LE_LL_WITH_PHDR:
        if len(payload) < 10:
            return None
        channel = payload[0]
        rssi = struct.unpack("<b", payload[1:2])[0]
        payload = payload[10:]

    if len(payload) < 8:
        return None
    header = payload[4]
    pdu_type = header & 0x0F
    tx_add_random = bool(header & 0x40)
    body_len = payload[5]
    if pdu_type not in _ADV_PDU_TYPES:
        return None
    body = payload[6:6 + body_len]
    if len(body) < 6:
        return None

    # The address is transmitted least-significant byte first.
    address = _mac(body[5::-1])
    decoded = _parse_ad_structures(body[6:])
    decoded["pdu_type"] = _ADV_PDU_TYPES[pdu_type]
    decoded["address_type_actual"] = "random" if tx_add_random else "public"
    if rssi is not None:
        decoded["rssi"] = rssi
    if channel is not None:
        decoded["ble_channel"] = channel
    decoded["address"] = address
    return decoded


# --- top level -----------------------------------------------------------

# A station that asks for more networks than this is a fuzzer or a hostile
# file, not a phone. The first names are kept and the rest are counted, which
# also keeps the membership check above linear.
MAX_PROBED_NAMES = 64


def _record_probe(best: dict, probe: dict, when, path: Path, rssi) -> None:
    """Fold one probe request into the per-station record.

    One phone probes for every network it remembers, so the useful unit is the
    station with the list of names it asked for, not one record per frame.
    """
    station = probe["station"]
    key = f"CLIENT|{station}"
    signal = rssi if rssi is not None else 0
    existing = best.get(key)

    if existing is not None:
        existing.times_seen += 1
        if signal > existing.rssi:
            existing.rssi = signal
        if probe["ssid"]:
            asked = existing.meta.setdefault("probing_for", [])
            if probe["ssid"] not in asked:
                if len(asked) < MAX_PROBED_NAMES:
                    asked.append(probe["ssid"])
                else:
                    existing.meta["probing_for_more"] = existing.meta.get("probing_for_more", 0) + 1
            if not existing.ssid:
                existing.ssid = probe["ssid"]
        return

    meta = {"from_pcap": True, "frame_type": "probe request"}
    if probe["ssid"]:
        meta["probing_for"] = [probe["ssid"]]
    else:
        meta["note"] = "Wildcard probe: asked for any network, not a named one."

    best[key] = Sighting(
        bssid=station,
        ssid=probe["ssid"],
        auth_mode="",
        enc_bucket=ENC_UNKNOWN,
        first_seen=when.strftime("%Y-%m-%d %H:%M:%S") if when else "",
        channel=None,
        rssi=signal,
        lat=None,
        lon=None,
        altitude=None,
        accuracy=None,
        type=TYPE_CLIENT,
        vendor=ble.oui_vendor(station) or "",
        source=str(path),
        geo_source=GEO_NONE,
        meta=meta,
    )
    best[key].meta.update(ble.describe_wifi_station(station))


def read_pcap(path: Path) -> PcapResult:
    """Every mappable record in a capture file.

    The records come back with no location: a pcap has timestamps but no
    GPS. Feed the result through `warmap.gps.geotag` with a track to place
    them.
    """
    path = Path(path)
    result = PcapResult()

    try:
        size = path.stat().st_size
    except OSError as exc:
        result.error = f"could not read {path.name}: {exc}"
        return result

    if size > MAX_FILE_BYTES:
        result.error = (
            f"{path.name} is {size / 1e6:.0f} MB, over warmap's "
            f"{MAX_FILE_BYTES / 1e6:.0f} MB limit for a single capture. "
            "Split it with `editcap -c` and import the pieces."
        )
        return result

    try:
        data = path.read_bytes()
    except (OSError, MemoryError) as exc:
        result.error = f"could not read {path.name}: {exc}"
        return result

    if len(data) < 24:
        result.error = f"{path.name} is too small to be a capture file"
        return result

    if data[:4] == b"\x0a\x0d\x0d\x0a":
        stream = _iter_pcapng_packets(data)
    else:
        stream = _iter_pcap_packets(data)

    best: dict[str, Sighting] = {}

    for when, payload, dlt in stream:
        if result.link_type is None:
            result.link_type = dlt
        result.link_types.add(dlt)

        if dlt not in WIFI_DLTS and dlt not in BLE_DLTS:
            result.skipped_link_types.add(dlt)
            continue

        result.packets_read += 1

        if dlt in WIFI_DLTS:
            frame = payload
            freq = None
            rssi = None
            if dlt == DLT_IEEE802_11_RADIOTAP:
                header_len, freq, rssi = _parse_radiotap(payload)
                if header_len <= 0 or header_len >= len(payload):
                    continue
                frame = payload[header_len:]
            elif dlt == DLT_IEEE802_11_PRISM:
                if len(payload) < 144:
                    continue
                frame = payload[144:]
            elif dlt == DLT_IEEE802_11_AVS:
                if len(payload) < 64:
                    continue
                frame = payload[64:]

            if _is_probe_request(frame):
                probe = _parse_probe_request(frame)
                if probe is None:
                    continue
                result.packets_decoded += 1
                _record_probe(best, probe, when, path, rssi)
                continue

            info = _parse_beacon(frame)
            if info is None:
                continue
            result.packets_decoded += 1
            key = info["bssid"]
            existing = best.get(key)
            signal = rssi if rssi is not None else 0
            if existing is not None:
                existing.times_seen += 1
                if signal > existing.rssi:
                    existing.rssi = signal
                continue

            channel = info["channel"]
            if channel is None and freq is not None:
                channel = radio.wifi_freq_to_channel(freq)
            sighting = Sighting(
                bssid=key,
                ssid=info["ssid"],
                auth_mode="",
                enc_bucket=info["enc"],
                first_seen=when.strftime("%Y-%m-%d %H:%M:%S") if when else "",
                channel=channel,
                rssi=signal,
                lat=None,
                lon=None,
                altitude=None,
                accuracy=None,
                type=TYPE_WIFI,
                frequency=freq,
                vendor=ble.oui_vendor(key) or "",
                source=str(path),
                geo_source=GEO_NONE,
                meta={"from_pcap": True, "encryption_source": "RSN element"},
            )
            best[key] = sighting

        elif dlt in BLE_DLTS:
            info = _parse_ble_ll(payload, dlt)
            if info is None:
                continue
            result.packets_decoded += 1
            address = info.pop("address")
            key = f"BLE|{address}"
            existing = best.get(key)
            signal = info.get("rssi", 0)
            if existing is not None:
                existing.times_seen += 1
                if signal > existing.rssi:
                    existing.rssi = signal
                if info.get("name") and not existing.ssid:
                    existing.ssid = info["name"]
                existing.meta.update(info)
                continue

            sighting = Sighting(
                bssid=address,
                ssid=info.get("name", ""),
                auth_mode="",
                enc_bucket=ENC_UNKNOWN,
                first_seen=when.strftime("%Y-%m-%d %H:%M:%S") if when else "",
                channel=info.get("ble_channel"),
                rssi=signal,
                lat=None,
                lon=None,
                altitude=None,
                accuracy=None,
                type=TYPE_BLE,
                vendor=ble.oui_vendor(address) or "",
                source=str(path),
                geo_source=GEO_NONE,
                meta={"from_pcap": True, **info},
            )
            sighting.meta.update(ble.describe(address, sighting.meta))
            if sighting.meta.get("vendor") and not sighting.vendor:
                sighting.vendor = sighting.meta["vendor"]
            best[key] = sighting

    result.sightings = list(best.values())

    if not result.link_types:
        result.error = f"{path.name} is not a pcap or pcapng file"
    elif result.skipped_link_types and not result.packets_read:
        names = ", ".join(
            DLT_NAMES.get(d, f"DLT {d}") for d in sorted(result.skipped_link_types)
        )
        result.error = (
            f"{path.name} is link type {names}, which warmap does not decode. "
            "Convert it with `editcap -T ieee-802-11-radiotap`."
        )
    elif result.skipped_link_types:
        names = ", ".join(
            DLT_NAMES.get(d, f"DLT {d}") for d in sorted(result.skipped_link_types)
        )
        result.note = (
            f"{path.name} also holds packets from an interface warmap cannot "
            f"decode ({names}); those were skipped."
        )
    return result


def is_pcap_file(path: Path) -> bool:
    return Path(path).suffix.lower() in (".pcap", ".pcapng", ".cap")
