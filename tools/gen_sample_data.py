"""Generate warmap's bundled sample capture: a plausible session laid out
along a street grid, deterministic (a fixed random seed) so re-running this
doesn't churn the checked-in files.

This is what loads automatically on first run, before you've imported any real
capture (see warmap/ui/mainwindow.py's load_initial_data()). It deliberately
covers every input warmap handles, because the sample is also the fastest way
to see whether a feature still works:

    sample/sample_wardrive.csv     WigleWifi-1.4, the format Marauder emits
    sample/sample_track.gpx        the GPS track for the same session
    sample/flipper/                Sub-GHz, NFC, RFID, iButton and IR captures

The Flipper files carry no coordinates of their own, so they only appear on
the map once the track places them by timestamp. That makes the sample a
live demonstration of that mechanism rather than a claim about it.

One detail worth understanding: the sample track's GPX timestamps are written
WITHOUT a timezone suffix, so they parse as local time and line up with the
timestamps in the Flipper filenames on any machine. A real track is normally
UTC (ending in "Z") and gets converted; a sample that did that would drift by
whatever the reader's UTC offset happens to be and place nothing.

Usage:
    python3 tools/gen_sample_data.py
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "warmap" / "sample"
OUT_PATH = SAMPLE_DIR / "sample_wardrive.csv"
TRACK_PATH = SAMPLE_DIR / "sample_track.gpx"
FLIPPER_DIR = SAMPLE_DIR / "flipper"

SEED = 20260401

# A residential/commercial grid roughly the size of a few Phoenix, AZ
# blocks, chosen for the real strict north-south/east-west street grid,
# not because it's an exact real capture.
BASE_LAT = 33.4484
BASE_LON = -112.0740
BLOCK_COUNT = 13          # streets running each direction
BLOCK_SPACING_DEG = 0.00095   # ~105m between parallel streets
HOUSES_PER_BLOCK = 13

UNIQUE_AP_COUNT = 150
DUPLICATE_SIGHTING_COUNT = 32  # extra re-sightings of already-placed APs, same as a real drive-by re-pass

START_TIME = datetime(2026, 6, 14, 9, 5, 0)

SSID_PREFIXES = [
    "NETGEAR", "Linksys", "TP-Link_", "ATT-WiFi-", "xfinitywifi", "Spectrum-",
    "CenturyLink", "HOME-", "Family_Network_", "The_Nest", "Casa_", "FBI Surveillance Van",
    "Pretty Fly for a WiFi", "Corner_Cafe_Guest", "TacoShop_Guest", "DriveThru_WiFi",
    "Apt", "Suite", "Office_Net_", "Warehouse_", "Garage_", "Studio_",
]
SSID_SUFFIXES = ["", "_2G", "_5G", "-Guest", "24", "5", "", "", ""]

# The exact bracketed strings ESP32 Marauder writes into AuthMode. It never
# leaves the field blank: an unrecognized network gets [UNDEFINED].
AUTH_MODES = [
    ("[OPEN]", 0.10),
    ("[WEP]", 0.05),
    ("[WPA_PSK]", 0.08),
    ("[WPA2_PSK]", 0.45),
    ("[WPA_WPA2_PSK]", 0.10),
    ("[WPA2_ENTERPRISE]", 0.05),
    ("[WPA3_PSK]", 0.10),
    ("[WPA2_WPA3_PSK]", 0.05),
    ("[UNDEFINED]", 0.02),
]

CHANNELS_24 = [1, 1, 6, 6, 6, 11, 11, 3]
CHANNELS_5 = [36, 40, 44, 48, 149, 153]

# (name, manufacturer id). The Wigle 1.6 MfgrId column carries the Bluetooth
# SIG company identifier, which is how a CSV row alone can still be resolved
# to "Apple" and flagged as a tracker.
BLE_DEVICES = [
    ("", 0x004C), ("", 0x004C), ("", 0x0075), ("", None),
    ("JBL Flip 5", 0x0057), ("Fitbit Charge", 0x00E0), ("Tile", None),
    ("AirTag", 0x004C), ("SmartTag2", 0x0075), ("ESP32-BLE", 0x02E5),
    ("Ring Doorbell", 0x0171), ("Govee_H5075", None), ("MX Master 3", None),
]

# Marauder's Type column is only ever WIFI or BLE. It has no cellular radio
# and no classic-Bluetooth scan. warmap reads the wider WiGLE type set, but a
# sample claiming to be a Marauder capture should not contain rows Marauder
# cannot produce.


def _weighted_choice(rng: random.Random, options):
    values = [v for v, _ in options]
    weights = [w for _, w in options]
    return rng.choices(values, weights=weights, k=1)[0]


def _mac(rng: random.Random) -> str:
    return ":".join(f"{rng.randint(0, 255):02X}" for _ in range(6))


def _random_ble_mac(rng: random.Random) -> str:
    """A resolvable private address: top two bits 0b01, which is what most
    modern BLE devices actually advertise."""
    first = rng.randint(0x40, 0x7F)
    rest = ":".join(f"{rng.randint(0, 255):02X}" for _ in range(5))
    return f"{first:02X}:{rest}"


def _ssid(rng: random.Random) -> str:
    if rng.random() < 0.06:
        return ""  # hidden SSID
    prefix = rng.choice(SSID_PREFIXES)
    suffix = rng.choice(SSID_SUFFIXES)
    if prefix.endswith("_") or prefix.endswith("-"):
        return f"{prefix}{rng.randint(10, 9999)}{suffix}"
    return f"{prefix}{suffix}"


def _grid_point(rng: random.Random, row: int, col: int) -> tuple[float, float]:
    lat = BASE_LAT + row * BLOCK_SPACING_DEG + rng.uniform(-0.00006, 0.00006)
    lon = BASE_LON + col * BLOCK_SPACING_DEG + rng.uniform(-0.00006, 0.00006)
    return round(lat, 6), round(lon, 6)


def generate_rows(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)

    positions = []
    for row in range(BLOCK_COUNT):
        for house in range(HOUSES_PER_BLOCK):
            col = house + rng.uniform(-0.15, 0.15)
            positions.append((row, col))
    rng.shuffle(positions)
    positions = positions[:UNIQUE_AP_COUNT]

    rows: list[dict] = []
    placed = []
    t = START_TIME

    for row, col in positions:
        roll = rng.random()
        lat, lon = _grid_point(rng, row, col)

        if roll < 0.18:
            record_type = "BLE"
            name, _company = rng.choice(BLE_DEVICES)
            bssid = _random_ble_mac(rng) if rng.random() < 0.7 else _mac(rng)
            ssid = name
            auth = "[BLE]"
            channel = 0
        else:
            record_type = "WIFI"
            bssid = _mac(rng)
            ssid = _ssid(rng)
            auth = _weighted_choice(rng, AUTH_MODES)
            channel = rng.choice(CHANNELS_24) if rng.random() < 0.85 else rng.choice(CHANNELS_5)

        rssi = rng.randint(-92, -34)
        t = t + timedelta(seconds=rng.randint(3, 14))
        rows.append({
            "MAC": bssid, "SSID": ssid, "AuthMode": auth,
            "FirstSeen": t.strftime("%Y-%m-%d %H:%M:%S"),
            "Channel": channel, "RSSI": rssi,
            "CurrentLatitude": lat, "CurrentLongitude": lon,
            "AltitudeMeters": round(340 + rng.uniform(-3, 3), 1),
            "AccuracyMeters": round(rng.uniform(3, 12), 1),
            "Type": record_type,
        })
        placed.append((bssid, ssid, auth, channel, record_type, lat, lon))

    # Re-sightings: the same drive passes some APs more than once, usually at
    # a different (often weaker/further) signal a few minutes apart, exactly
    # what dedup is for.
    for _ in range(DUPLICATE_SIGHTING_COUNT):
        bssid, ssid, auth, channel, record_type, lat, lon = rng.choice(placed)
        rssi = rng.randint(-95, -40)
        t = t + timedelta(seconds=rng.randint(3, 14))
        rows.append({
            "MAC": bssid, "SSID": ssid, "AuthMode": auth,
            "FirstSeen": t.strftime("%Y-%m-%d %H:%M:%S"),
            "Channel": channel, "RSSI": rssi,
            "CurrentLatitude": round(lat + rng.uniform(-0.00004, 0.00004), 6),
            "CurrentLongitude": round(lon + rng.uniform(-0.00004, 0.00004), 6),
            "AltitudeMeters": round(340 + rng.uniform(-3, 3), 1),
            "AccuracyMeters": round(rng.uniform(3, 12), 1),
            "Type": record_type,
        })

    rows.sort(key=lambda r: r["FirstSeen"])
    return rows


HEADER_COLS = [
    "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
    "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters",
    "Type",
]


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder,release=v1.14.0,"
        "device=ESP32 Marauder,display=SPI TFT,board=ESP32 Marauder,"
        "brand=JustCallMeKoko",
        ",".join(HEADER_COLS),
    ]
    for row in rows:
        ssid = row["SSID"]
        if "," in ssid or '"' in ssid:
            ssid = '"' + ssid.replace('"', '""') + '"'
        lines.append(",".join(str(row[c]) if c != "SSID" else ssid for c in HEADER_COLS))
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def generate_track(rows: list[dict], seed: int = SEED) -> list[tuple]:
    """A serpentine drive through the grid, one point every 15 seconds,
    spanning the same window as the CSV rows."""
    rng = random.Random(seed + 1)
    points = []
    t = START_TIME
    step = timedelta(seconds=15)

    for row in range(BLOCK_COUNT):
        columns = range(HOUSES_PER_BLOCK) if row % 2 == 0 else reversed(range(HOUSES_PER_BLOCK))
        for col in columns:
            lat = BASE_LAT + row * BLOCK_SPACING_DEG + rng.uniform(-0.00002, 0.00002)
            lon = BASE_LON + col * BLOCK_SPACING_DEG + rng.uniform(-0.00002, 0.00002)
            points.append((round(lat, 6), round(lon, 6), t, round(340 + rng.uniform(-2, 2), 1)))
            t = t + step
    return points


def write_gpx(points: list[tuple], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="warmap sample generator" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        "  <trk><name>Sample wardrive</name><trkseg>",
    ]
    for lat, lon, when, ele in points:
        lines.append(f'    <trkpt lat="{lat}" lon="{lon}">')
        lines.append(f"      <ele>{ele}</ele>")
        # No timezone suffix on purpose, see the module docstring.
        lines.append(f"      <time>{when.strftime('%Y-%m-%dT%H:%M:%S')}</time>")
        lines.append("    </trkpt>")
    lines.append("  </trkseg></trk>")
    lines.append("</gpx>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Each entry is (subdirectory, filename stem, file body). The stems carry a
# timestamp because that is the only thing that can place these on a map, and
# a timestamp in the name survives being copied off an SD card carelessly
# where an mtime does not.
FLIPPER_FILES = [
    ("subghz", "GarageRemote_20260614-091530.sub", """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 315000000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 4B 8E 51
TE: 403
"""),
    ("subghz", "CarKeyfob_20260614-092245.sub", """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: KeeLoq
Bit: 64
Key: 3F 21 9C 4A 7E 05 B8 D2
Manufacture: Unknown
Serial: 00 1D 4A 7E
Btn: 2
Cnt: 004E
"""),
    ("subghz", "Unknown_868_20260614-093015.sub", """Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 868350000
Preset: FuriHalSubGhzPreset2FSKDev476Async
Protocol: RAW
RAW_Data: -25049 471 -238 476 -235 950 -710 475 -239 476 -708 951 -235 476
RAW_Data: -239 950 -711 474 -240 475 -239 951 -710 475 -239 476 -708 476
"""),
    ("subghz", "GateOpener_20260614-094120.sub", """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: CAME
Bit: 12
Key: 00 00 00 00 00 00 0E 5A
TE: 320
"""),
    ("nfc", "OfficeBadge_20260614-091850.nfc", """Filetype: Flipper NFC device
Version: 4
Device type: Mifare Classic
UID: 04 A3 91 2B 6C 5D 80
ATQA: 00 44
SAK: 08
Mifare Classic type: 1K
Data format version: 2
"""),
    ("nfc", "TransitCard_20260614-093740.nfc", """Filetype: Flipper NFC device
Version: 4
Device type: ISO14443-4A
UID: 08 1F 3C 92
ATQA: 03 44
SAK: 20
"""),
    ("lfrfid", "GateFob_20260614-092610.rfid", """Filetype: Flipper RFID key
Version: 1
Key type: EM4100
Data: 1A 2B 3C 4D 5E
"""),
    ("lfrfid", "OfficeProx_20260614-094500.rfid", """Filetype: Flipper RFID key
Version: 1
Key type: H10301
Data: 2C 91 4E
"""),
    ("ibutton", "IntercomKey_20260614-093325.ibtn", """Filetype: Flipper iButton key
Version: 1
Protocol: DS1990
Rom_data: 01 A4 7B 22 0C 00 00 3D
"""),
    ("infrared", "LivingRoomTV_20260614-094015.ir", """Filetype: IR signals file
Version: 1
#
name: Power
type: parsed
protocol: NEC
address: 04 00 00 00
command: 08 00 00 00
#
name: Vol_up
type: parsed
protocol: NEC
address: 04 00 00 00
command: 10 00 00 00
#
name: Source
type: raw
frequency: 38000
duty_cycle: 0.330000
data: 8964 4432 559 1671 558 559 558 1671 559 558
"""),
]


def write_flipper_files(root: Path) -> int:
    for subdir, name, body in FLIPPER_FILES:
        target = root / subdir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return len(FLIPPER_FILES)


def main() -> int:
    rows = generate_rows()
    write_csv(rows, OUT_PATH)
    print(f"wrote {OUT_PATH} ({len(rows)} rows, {UNIQUE_AP_COUNT} unique addresses)")

    points = generate_track(rows)
    write_gpx(points, TRACK_PATH)
    print(f"wrote {TRACK_PATH} ({len(points)} track points)")

    count = write_flipper_files(FLIPPER_DIR)
    print(f"wrote {count} Flipper capture files under {FLIPPER_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
