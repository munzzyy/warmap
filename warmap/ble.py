"""Turning a bare Bluetooth address and advertisement into something you can
read: who made it, what it claims to be, and whether it's a tracker.

Two data sources, deliberately separated:

* The tables in this file are a small, hand-checked subset, the assignments
  common enough to be worth having with zero setup and zero network. They
  are not the full registries and don't pretend to be.
* The full registries (Bluetooth SIG company IDs and 16-bit UUIDs, IEEE OUI)
  are far too large and too churn-prone to vendor into a git repo, and a
  stale copy silently mislabels devices. So `warmap fetch-ids` downloads them
  into the data dir and `load_external()` layers them on top of the built-ins
  at import time if they're present.

An unknown identifier is always rendered as the raw value ("0x0157"), never
guessed at and never silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

# --- Bluetooth SIG 16-bit company identifiers ----------------------------
# Subset. Full list: https://bitbucket.org/bluetooth-SIG/public (assigned_numbers).
COMPANY_IDS: dict[int, str] = {
    0x0000: "Ericsson Technology Licensing",
    0x0001: "Nokia Mobile Phones",
    0x0002: "Intel Corp.",
    0x0003: "IBM Corp.",
    0x0004: "Toshiba Corp.",
    0x0005: "3Com",
    0x0006: "Microsoft",
    0x0007: "Lucent",
    0x0008: "Motorola",
    0x0009: "Infineon Technologies AG",
    0x000A: "Cambridge Silicon Radio",
    0x000D: "Texas Instruments Inc.",
    0x000F: "Broadcom Corporation",
    0x0013: "Atmel Corporation",
    0x0014: "Mitsubishi Electric Corporation",
    0x001D: "Qualcomm",
    0x0025: "Philips Semiconductors",
    0x0030: "ST Microelectronics",
    0x003A: "Panasonic Corporation",
    0x003D: "Realtek Semiconductor Corporation",
    0x0046: "MediaTek, Inc.",
    0x004C: "Apple, Inc.",
    0x0057: "Harman International Industries",
    0x0059: "Nordic Semiconductor ASA",
    0x0065: "Hewlett-Packard Company",
    0x0075: "Samsung Electronics Co. Ltd.",
    0x0078: "Nike, Inc.",
    0x0087: "Garmin International, Inc.",
    0x009E: "Bose Corporation",
    0x00C4: "LG Electronics",
    0x00E0: "Google",
    0x0117: "Withings",
    0x0131: "Cypress Semiconductor",
    0x0157: "Anhui Huami (Amazfit)",
    0x0171: "Amazon.com Services LLC",
    0x02E5: "Espressif Systems",
    0x0499: "Ruuvi Innovations Ltd.",
}

# --- 16-bit UUIDs --------------------------------------------------------
# The 0x18xx block is standard GATT services. The 0xFDxx-0xFExx blocks are
# per-member allocations, which in practice are the ones that actually
# identify a device in the wild.
SERVICE_UUIDS_16: dict[int, str] = {
    0x1800: "Generic Access",
    0x1801: "Generic Attribute",
    0x1802: "Immediate Alert",
    0x1803: "Link Loss",
    0x1804: "Tx Power",
    0x1805: "Current Time",
    0x1808: "Glucose",
    0x1809: "Health Thermometer",
    0x180A: "Device Information",
    0x180D: "Heart Rate",
    0x180E: "Phone Alert Status",
    0x180F: "Battery",
    0x1810: "Blood Pressure",
    0x1811: "Alert Notification",
    0x1812: "Human Interface Device",
    0x1813: "Scan Parameters",
    0x1814: "Running Speed and Cadence",
    0x1816: "Cycling Speed and Cadence",
    0x1818: "Cycling Power",
    0x1819: "Location and Navigation",
    0x181A: "Environmental Sensing",
    0x181B: "Body Composition",
    0x181D: "Weight Scale",
    0x1822: "Pulse Oximeter",
    0x1826: "Fitness Machine",
    0x1827: "Mesh Provisioning",
    0x1828: "Mesh Proxy",
    0x184E: "Audio Stream Control",
    0x1853: "Common Audio",
    0x1854: "Hearing Access",
    # Member allocations.
    0xFCB2: "DULT tracker (cross-platform Find My)",
    0xFD5A: "Samsung Electronics",
    0xFD6F: "Exposure Notification (Google/Apple)",
    0xFE2C: "Google Fast Pair",
    0xFE59: "Nordic Secure DFU",
    0xFE95: "Xiaomi Inc.",
    0xFE9F: "Google",
    0xFEAA: "Google Eddystone",
    0xFEED: "Tile, Inc.",
}

# --- 128-bit service UUIDs ----------------------------------------------
# The few full-length UUIDs worth naming on sight. A tracker advertising the
# DULT Accessory Non-Owner Service exposes this as a 128-bit service class
# UUID, which the 0xFCB2 16-bit service-data check never sees, so a BLE
# sniff (Sniffle/nRF/phone) that captured the full advertisement is caught
# here even when the 16-bit path misses it.
DULT_ANOS_UUID = "15190001-12f4-c226-88ed-2ac5579f2a85"
SERVICE_UUIDS_128: dict[str, str] = {
    DULT_ANOS_UUID: "DULT Accessory Non-Owner Service",
}

# --- MAC OUI -> vendor ---------------------------------------------------
# Deliberately tiny. The real registry is ~35k entries and changes weekly;
# `warmap fetch-ids` pulls the current one. These are just enough that a
# fresh install with no network still labels the most common hardware.
OUI_PREFIXES: dict[str, str] = {
    "00:1A:11": "Google",
    "00:03:93": "Apple",
    "00:1B:63": "Apple",
    "00:23:12": "Apple",
    "3C:5A:B4": "Google",
    "44:07:0B": "Google",
    "F4:F5:D8": "Google",
    "18:B4:30": "Nest Labs",
    "00:17:88": "Philips Lighting (Hue)",
    "EC:FA:BC": "Espressif Inc.",
    "24:0A:C4": "Espressif Inc.",
    "30:AE:A4": "Espressif Inc.",
    "7C:9E:BD": "Espressif Inc.",
    "84:CC:A8": "Espressif Inc.",
    "A4:CF:12": "Espressif Inc.",
    "B4:E6:2D": "Espressif Inc.",
    "C4:4F:33": "Espressif Inc.",
    "D8:A0:1D": "Espressif Inc.",
    "B8:27:EB": "Raspberry Pi Foundation",
    "DC:A6:32": "Raspberry Pi Trading",
    "E4:5F:01": "Raspberry Pi Trading",
    "28:CD:C1": "Raspberry Pi Trading",
    "00:1D:0F": "TP-Link Technologies",
    "50:C7:BF": "TP-Link Technologies",
    "A4:2B:B0": "TP-Link Technologies",
    "00:14:6C": "Netgear",
    "20:4E:7F": "Netgear",
    "44:D9:E7": "Ubiquiti Networks",
    "24:5A:4C": "Ubiquiti Networks",
    "00:0C:29": "VMware",
    "00:50:56": "VMware",
    "52:54:00": "QEMU/KVM virtual NIC",
    "5C:AA:FD": "Sonos",
    "00:0E:58": "Sonos",
    "94:9F:3E": "Sonos",
    "F0:81:73": "Amazon Technologies",
    "68:54:FD": "Amazon Technologies",
    "44:65:0D": "Amazon Technologies",
    "AC:63:BE": "Amazon Technologies",
}

# --- GAP appearance ------------------------------------------------------
APPEARANCE_VALUES: dict[int, str] = {
    0x0000: "Unknown",
    0x0040: "Generic Phone",
    0x0080: "Generic Computer",
    0x00C0: "Generic Watch",
    0x00C1: "Sports Watch",
    0x0100: "Generic Clock",
    0x0140: "Generic Display",
    0x0180: "Generic Remote Control",
    0x0200: "Generic Eye-glasses",
    0x0240: "Generic Tag",
    0x0280: "Generic Keyring",
    0x02C0: "Generic Media Player",
    0x0300: "Generic Barcode Scanner",
    0x0340: "Generic Thermometer",
    0x0380: "Generic Heart Rate Sensor",
    0x03C0: "Generic Blood Pressure",
    0x03C1: "Keyboard",
    0x0341: "Mouse",
    0x0400: "Generic Human Interface Device",
    0x0440: "Generic Glucose Meter",
    0x0480: "Generic Running Walking Sensor",
    0x04C0: "Generic Cycling",
    0x0900: "Generic Outdoor Sports Activity",
    0x0941: "Heart Rate Sensor",
}

# --- consumer trackers ---------------------------------------------------
# Discriminators are the advertisement fields that actually identify these,
# not marketing names.
#
# Apple's Find My / Offline Finding advertisement is manufacturer-specific
# data under company 0x004C with a leading type byte: 0x12 is the Find My
# ("offline finding") payload an AirTag or a separated Find My accessory
# sends, and 0x07 is proximity pairing (AirPods and friends). The Continuity
# protocol's type bytes are documented in the reverse-engineering writeup at
# https://github.com/furiousMAC/continuity and in Apple's own Find My Network
# accessory specification.
#
# Tile and Samsung SmartTag are simpler: they advertise a member service UUID
# (0xFEED and 0xFD5A respectively).
TRACKERS: tuple = (
    {
        "name": "Apple AirTag / Find My accessory",
        "company_id": 0x004C,
        "mfg_data_prefix": "12",
        "notes": "Find My offline-finding payload. Rotates its address, so a "
                 "repeat sighting is not necessarily the same tag.",
    },
    {
        "name": "Apple proximity pairing (AirPods etc.)",
        "company_id": 0x004C,
        "mfg_data_prefix": "07",
        "notes": "Continuity proximity-pairing advertisement.",
    },
    {
        "name": "Apple Nearby",
        "company_id": 0x004C,
        "mfg_data_prefix": "10",
        "notes": "Continuity Nearby Info. Sent by iPhones/Macs, not a tracker "
                 "as such, but it is how most Apple devices show up.",
    },
    {
        "name": "Tile",
        "service_uuid": 0xFEED,
        "notes": "Tile trackers advertise the 0xFEED member service UUID.",
    },
    {
        "name": "Samsung SmartTag",
        "service_uuid": 0xFD5A,
        "notes": "Samsung's member UUID, used by SmartTag and Galaxy devices.",
    },
    {
        "name": "DULT tracker (Find My accessory)",
        "service_uuid": 0xFCB2,
        "notes": "Detected Location Tracking / cross-platform unwanted-tracking "
                 "format (Apple+Google IETF DULT draft). The service UUID a "
                 "separated third-party tracker advertises regardless of brand, "
                 "so it catches trackers that don't announce a member UUID or a "
                 "name, the current standard for tracker detection.",
    },
    {
        "name": "DULT tracker (Find My accessory)",
        "service_uuid_128": DULT_ANOS_UUID,
        "notes": "Same DULT tracker, matched on its 128-bit Accessory "
                 "Non-Owner Service UUID. A full BLE-advertisement capture "
                 "exposes this even when the 16-bit service-data field isn't "
                 "present.",
    },
    {
        "name": "Google Fast Pair",
        "service_uuid": 0xFE2C,
        "notes": "Fast Pair discoverable/unwanted-tracking advertisement.",
    },
    {
        "name": "Microsoft Swift Pair",
        "company_id": 0x0006,
        "mfg_data_prefix": "03",
        "notes": "Windows pairing beacon.",
    },
    # Name matches are weaker evidence than an advertisement structure: a
    # device can call itself anything. They're included because a wardrive
    # CSV carries a name and nothing else, and `basis` records which kind of
    # match it was so the UI can be honest about it.
    {
        "name": "Apple AirTag (by name)",
        "name_pattern": "airtag",
        "basis": "name",
        "notes": "Matched on the advertised device name, not the payload.",
    },
    {
        "name": "Tile (by name)",
        "name_pattern": "tile",
        "basis": "name",
        "notes": "Matched on the advertised device name, not the payload.",
    },
    {
        "name": "Samsung SmartTag (by name)",
        "name_pattern": "smarttag",
        "basis": "name",
        "notes": "Matched on the advertised device name, not the payload.",
    },
    {
        "name": "Chipolo",
        "name_pattern": "chipolo",
        "basis": "name",
        "notes": "Newer Chipolo models join Apple's Find My network and look "
                 "like a 0x12 payload instead.",
    },
    {
        "name": "Pebblebee",
        "name_pattern": "pebblebee",
        "basis": "name",
        "notes": "Also a Find My network participant on recent models.",
    },
)


# --- external registry loading -------------------------------------------

_external_loaded = False


def ensure_loaded() -> None:
    """Load the bundled registries once, on first lookup.

    Lazy rather than at import because the OUI table is ~1.6 MB of JSON and
    plenty of code paths (`warmap formats`, the Sub-GHz parsers) never look up
    a vendor at all. Idempotent, and a missing data directory is not an error,
    the built-in tables above are the fallback.
    """
    global _external_loaded
    if _external_loaded:
        return
    _external_loaded = True
    try:
        from warmap import config
        load_external(config.BUNDLED_DATA_DIR)
    except Exception:
        pass


def load_external(data_dir: Path) -> dict[str, int]:
    """Layer downloaded registries on top of the built-in tables.

    Returns a count per table of how many entries were added, so the caller
    can say something honest about what's loaded. A missing or malformed file
    is not an error, the built-ins just stay as they are.
    """
    added = {"companies": 0, "uuids": 0, "ouis": 0}
    for filename, table, key_is_int in (
        ("bluetooth-companies.json", COMPANY_IDS, True),
        ("bluetooth-uuids.json", SERVICE_UUIDS_16, True),
        ("oui.json", OUI_PREFIXES, False),
    ):
        path = Path(data_dir) / filename
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        bucket = {
            "bluetooth-companies.json": "companies",
            "bluetooth-uuids.json": "uuids",
            "oui.json": "ouis",
        }[filename]
        for k, v in raw.items():
            if not isinstance(v, str):
                continue
            if key_is_int:
                try:
                    key = int(str(k), 16) if str(k).lower().startswith("0x") else int(k)
                except (TypeError, ValueError):
                    continue
            else:
                key = str(k).upper()
            if key not in table:
                added[bucket] += 1
            table[key] = v
    return added


# --- lookups -------------------------------------------------------------

def company_name(cid: Optional[int]) -> Optional[str]:
    if cid is None:
        return None
    ensure_loaded()
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    return COMPANY_IDS.get(cid) or f"Unknown company 0x{cid:04X}"


def service_name(uuid16: Optional[int]) -> Optional[str]:
    if uuid16 is None:
        return None
    ensure_loaded()
    try:
        uuid16 = int(uuid16)
    except (TypeError, ValueError):
        return None
    return SERVICE_UUIDS_16.get(uuid16) or f"0x{uuid16:04X}"


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """"aa-bb-cc-dd-ee-ff" / "aabbccddeeff" / "AA:BB:CC:DD:EE:FF" all become
    the colon-separated uppercase form. None for anything that isn't 6 bytes."""
    if not mac:
        return None
    hexonly = "".join(c for c in str(mac) if c in "0123456789abcdefABCDEF")
    if len(hexonly) != 12:
        return None
    hexonly = hexonly.upper()
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))


def oui_vendor(mac: Optional[str]) -> Optional[str]:
    norm = normalize_mac(mac)
    if norm is None:
        return None
    ensure_loaded()
    return OUI_PREFIXES.get(norm[:8])


def is_locally_administered(mac: Optional[str]) -> Optional[bool]:
    """The locally-administered bit (bit 1 of the first octet). Set means the
    address was not assigned from an IEEE OUI block: on Wi-Fi that's the
    randomized-MAC privacy feature every modern phone uses by default."""
    norm = normalize_mac(mac)
    if norm is None:
        return None
    return bool(int(norm[0:2], 16) & 0x02)


def address_type(mac: Optional[str]) -> str:
    """Best guess at a BLE address type from the address alone.

    This is a heuristic and the UI must say so. The real public/random
    distinction lives in the advertising PDU's TxAdd header bit, which a
    Wigle/Marauder CSV does not record, only a pcap does. So: if the OUI is
    one we recognize as IEEE-assigned, call it public; otherwise fall back to
    the random-address sub-type encoded in the top two bits of the most
    significant octet (0b11 static, 0b01 resolvable, 0b00 non-resolvable).
    """
    norm = normalize_mac(mac)
    if norm is None:
        return "unknown"
    ensure_loaded()
    if norm[:8] in OUI_PREFIXES:
        return "public"
    top_two = int(norm[0:2], 16) >> 6
    if top_two == 0b11:
        return "random-static"
    if top_two == 0b01:
        return "random-resolvable"
    if top_two == 0b00:
        return "random-non-resolvable"
    return "unknown"


def match_tracker(
    company_id: Optional[int] = None,
    service_uuids: Optional[Iterable[int]] = None,
    mfg_data_hex: Optional[str] = None,
    name: Optional[str] = None,
    service_uuids_128: Optional[Iterable[str]] = None,
) -> Optional[dict]:
    """The first matching TRACKERS entry, or None.

    Entries are checked in order, so the ones keyed on advertisement structure
    win over the ones keyed on a device name: a real Find My payload is much
    better evidence than a device calling itself "AirTag". Never raises; it's
    called on whatever partial junk a scan produced.
    """
    try:
        uuids = set()
        for u in service_uuids or ():
            try:
                uuids.add(int(u))
            except (TypeError, ValueError):
                continue
        uuids128 = {str(u).lower() for u in (service_uuids_128 or ())}
        mfg = (mfg_data_hex or "").replace(" ", "").upper()
        lname = (name or "").lower()

        for entry in TRACKERS:
            if "service_uuid_128" in entry:
                if entry["service_uuid_128"].lower() in uuids128:
                    return entry
                continue
            if "service_uuid" in entry:
                if entry["service_uuid"] in uuids:
                    return entry
                continue
            if "company_id" in entry:
                if company_id is None or int(company_id) != entry["company_id"]:
                    continue
                prefix = entry.get("mfg_data_prefix")
                if prefix and not mfg.startswith(prefix.upper()):
                    continue
                return entry
            pattern = entry.get("name_pattern")
            if pattern and lname and pattern in lname:
                return entry
    except Exception:
        return None
    return None


def identify_tracker(
    company_id: Optional[int] = None,
    service_uuids: Optional[Iterable[int]] = None,
    mfg_data_hex: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[str]:
    """Name of the first matching TRACKERS entry, or None."""
    entry = match_tracker(company_id, service_uuids, mfg_data_hex, name)
    return entry["name"] if entry else None


def describe_wifi_station(mac: Optional[str]) -> dict:
    """Flat fields for a Wi-Fi client seen probing.

    Deliberately NOT `describe()`. The public/random-static/resolvable
    taxonomy is Bluetooth's and means nothing for Wi-Fi. The equivalent
    question here is whether the MAC is locally administered, which is how
    every modern phone hides its real address while scanning, so a probing
    device is usually a different "device" on every pass.
    """
    out: dict = {}
    vendor = oui_vendor(mac)
    if vendor:
        out["vendor"] = vendor

    randomized = is_locally_administered(mac)
    if randomized is None:
        return out
    out["mac_randomized"] = randomized
    if randomized:
        out["address_note"] = (
            "Randomized MAC: phones use a throwaway address when scanning, so "
            "this will look like a different device next time."
        )
    elif vendor:
        out["address_note"] = "Real hardware MAC, not a randomized one."
    return out


def describe(mac: Optional[str], meta: Optional[dict] = None) -> dict:
    """Everything derivable about one BLE sighting, as flat fields ready to
    merge into a Sighting's `meta`. Safe on partial input."""
    meta = meta or {}
    out: dict = {}

    vendor = oui_vendor(mac)
    if vendor:
        out["vendor"] = vendor

    # A pcap carries the real answer in the advertising PDU's TxAdd bit, which
    # pcap.py records as `address_type_actual`. When that's present it wins
    # outright: guessing from the address bits over the top of it would state
    # something false about a device we have ground truth for.
    actual = meta.get("address_type_actual")
    if actual in ("public", "random"):
        guess = address_type(mac)
        if actual == "random":
            # Keep the sub-type from the address bits, which the TxAdd bit
            # doesn't tell us, but only when it agrees this is random.
            out["address_type"] = guess if guess.startswith("random") else "random"
        else:
            out["address_type"] = "public"
        out["address_type_source"] = "advertising PDU header"
    else:
        out["address_type"] = address_type(mac)
        out["address_type_source"] = "guessed from the address bits"

    if out["address_type"].startswith("random"):
        out["address_note"] = (
            "Randomized address: this device will look like a different one "
            "on the next pass, so repeat sightings undercount it."
        )

    cid = meta.get("company_id")
    if cid is not None:
        out["company"] = company_name(cid)

    uuids = meta.get("service_uuids") or []
    uuids128 = meta.get("service_uuids_128") or []
    services = [service_name(u) for u in uuids]
    services += [SERVICE_UUIDS_128.get(str(u).lower(), str(u)) for u in uuids128]
    if services:
        out["services"] = services

    appearance = meta.get("appearance")
    if appearance is not None:
        try:
            out["appearance"] = APPEARANCE_VALUES.get(int(appearance), f"0x{int(appearance):04X}")
        except (TypeError, ValueError):
            pass

    entry = match_tracker(
        company_id=cid,
        service_uuids=uuids,
        mfg_data_hex=meta.get("mfg_data"),
        name=meta.get("name") or meta.get("ssid"),
        service_uuids_128=uuids128,
    )
    if entry:
        out["tracker"] = entry["name"]
        # "advertisement" means the payload structure identified it;
        # "name" means only the device's own claimed name did.
        out["tracker_basis"] = entry.get("basis", "advertisement")

    return out
