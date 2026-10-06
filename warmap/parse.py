"""CSV ingestion: Marauder/Wigle wardrive exports in both the 1.4 and 1.6
layouts, plus a tolerant generic/Kismet-ish fallback, dedup by identity, and
AuthMode -> encryption-bucket classification.

Everything here is pure and I/O-light (one file read, no Qt, no network) so
it's cheap to unit test against small in-memory fixtures.

Wigle 1.6 added `Frequency`, `RCOIs` and `MfgrId` columns and widened the
`Type` column beyond WIFI/BLE to cover Bluetooth Classic and the cellular
radios. Rather than branch on the version line, the column-alias table just
knows about all of them and takes whichever are present, which also means a
hand-edited or third-party CSV with a subset of columns still loads.
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Iterable, Optional

from warmap import ble, radio
from warmap.models import (
    ALL_TYPES,
    ENC_OPEN,
    ENC_UNKNOWN,
    ENC_WEP,
    ENC_WPA,
    ENC_WPA2,
    ENC_WPA3,
    ENC_WPA23_MIXED,
    GEO_DIRECT,
    TYPE_BLE,
    TYPE_BT,
    TYPE_CELL,
    TYPE_WIFI,
    Sighting,
)

# Column name aliases, case-insensitive exact match, checked in order.
# Covers the Wigle 1.4 and 1.6 headers verbatim plus the handful of Kismet
# CSV exports and generic lat/lon dumps you're likely to point this at.
_COLUMN_ALIASES = {
    "bssid": ["mac", "bssid", "ap mac", "station mac", "address"],
    "ssid": ["ssid", "name", "essid", "device name"],
    "auth": ["authmode", "auth mode", "encryption", "crypto", "capabilities"],
    "first_seen": ["firstseen", "first seen", "time", "timestamp", "first_time", "datetime"],
    "channel": ["channel", "chan"],
    "frequency": ["frequency", "freq", "freq_mhz", "frequency_mhz"],
    "rssi": ["rssi", "signal", "signal_dbm", "signaldbm", "signal (dbm)", "level"],
    "lat": ["currentlatitude", "latitude", "lat", "gpslatitude"],
    "lon": ["currentlongitude", "longitude", "lon", "lng", "gpslongitude"],
    "altitude": ["altitudemeters", "altitude", "alt"],
    "accuracy": ["accuracymeters", "accuracy", "acc"],
    "type": ["type", "recordtype", "record type"],
    "rcois": ["rcois"],
    "mfgrid": ["mfgrid", "mfgid", "manufacturer", "manufacturerid"],
}

# Wigle's `Type` values -> warmap record types. The cellular technologies all
# collapse to one type; the specific radio is kept in the record's meta.
_TYPE_MAP = {
    "WIFI": TYPE_WIFI,
    "WLAN": TYPE_WIFI,
    "BLE": TYPE_BLE,
    "BT": TYPE_BT,
    "GSM": TYPE_CELL,
    "LTE": TYPE_CELL,
    "NR": TYPE_CELL,
    "CDMA": TYPE_CELL,
    "WCDMA": TYPE_CELL,
}


def sniff_format(path: Path) -> str:
    """"wigle" if the file's first line is the WigleWifi metadata line,
    otherwise "generic" (which also covers best-effort Kismet handling:
    there's no single stable Kismet CSV schema to branch on, so the same
    column-alias table in `_map_columns` does the work for both).

    `utf-8-sig` rather than `utf-8`: a CSV touched by a Windows-side tool or
    a text editor can pick up a leading UTF-8 BOM, which would otherwise
    survive as a stray character in front of "WigleWifi" and make an
    otherwise-normal file sniff as "generic". Transparent no-op when there's
    no BOM, so every other file is read exactly as before.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            first_line = f.readline()
    except OSError:
        return "generic"
    return "wigle" if first_line.startswith("WigleWifi") else "generic"


# A MAC address as its own field: six colon-separated hex pairs, nothing else.
_MAC17 = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
# Marauder's console counter prefix on Wi-Fi rows, e.g. "17 | AA:BB:...".
_WIFI_COUNTER = re.compile(r"^\s*\d+\s*\|\s*")

# A wardrive can legitimately grow far larger than any single Flipper file
# over a long collection career, but the whole thing is still read into
# memory as a list of rows before anything becomes a Sighting. Same 256 MB
# "capture file" ceiling as pcap.py: high enough that no real wardrive
# session hits it, low enough that a card pointed at by mistake doesn't
# freeze the GUI thread reading something that was never a wardrive at all.
MAX_FILE_BYTES = 256 * 1024 * 1024

# A line longer than this is not a wardrive row; it is skipped unread.
MAX_LINE_BYTES = 64 * 1024

# Rows kept from one file. A Marauder writes about one a second, so this is
# months of continuous driving in a single file; past it the rest is dropped
# rather than turned into memory.
MAX_ROWS_PER_FILE = 500_000

# Integer cells (channel, RSSI) past this are garbage, and a Qt item or a
# JSON encoder would choke on them downstream.
MAX_INT_CELL = 2_147_483_647

# No wardrive format has anywhere near this many columns.
MAX_HEADER_COLUMNS = 256


def _clean_marauder_line(line: str) -> str:
    """Undo the two things a live ESP32Marauder wardrive does to a row on the
    serial wire that it doesn't do in the file its own companion app saves.

    Verified against a real capture off a BFFB v2 (ESP32-C5, Marauder v1.14.0):

    * Wi-Fi rows are printed with a running console counter in front:
      "17 | 74:37:5F:...,MapleHome,[WPA2_PSK],...". Strip it.
    * BLE rows have the device's advertised name (or, for a nameless device,
      its MAC a second time) jammed onto the front of the MAC with no comma,
      "ihoment_H6008_1B2Cd4:ad:fc:0a:1b:2c,,[BLE],..." or
      "5a:75:65:11:22:335a:75:65:11:22:33,,[BLE],...". Split the real MAC back
      out (it's the 17 characters right before ",,[BLE]") and, when the prefix
      is a real name rather than the repeated MAC, keep it as the SSID, which
      is the only way a BLE name survives from a wardrive at all. The companion
      app drops it entirely.

    A row that's already clean (the companion app's own files, warmap's own
    exports) is returned unchanged: this only rewrites what actually matches
    the mangled shapes, so it's safe to run over every line of every file.
    """
    s = line.rstrip("\r\n")
    # Already a clean row: it starts with a MAC and a comma. Return it untouched.
    # This makes the function idempotent and keeps it from re-splitting a row
    # whose SSID happens to contain a MAC-plus-",,[BLE]" sequence.
    if len(s) >= 18 and s[17] == "," and _MAC17.match(s[:17]):
        return s
    # The console counter is only ever on Wi-Fi rows. Stripping it off a BLE
    # line would eat a device whose name happens to start with "N | ".
    if s.endswith(",WIFI"):
        s = _WIFI_COUNTER.sub("", s, count=1)
    if s.endswith(",BLE") and ",,[BLE]" in s:
        # rpartition, not partition: the real MAC sits right before the LAST
        # ",,[BLE]", so a device that put ",,[BLE]" in its own name can't fool
        # the split.
        head, _, tail = s.rpartition(",,[BLE]")
        mac = head[-17:]
        if _MAC17.match(mac):
            name = head[:-17]
            if name == mac:  # nameless: the MAC got printed as the "name"
                name = ""
            elif "," in name or '"' in name:
                # A device is free to advertise a name with a comma or a quote
                # in it; the recovered name goes into the SSID field, so it has
                # to be CSV-quoted or it would split into extra fields and shift
                # the whole row.
                name = '"' + name.replace('"', '""') + '"'
            return mac + "," + name + ",[BLE]" + tail
    return s


def _iter_rows(path: Path):
    """Yield each non-blank line of `path` as one parsed CSV record.

    Each line is parsed on its own rather than handing the whole file to one
    csv.reader. Marauder writes SSIDs unquoted, so a network whose name
    starts with a double quote would otherwise open a quoted field that
    swallows every row after it. Per-line parsing keeps any such damage to
    the single row it is on; multi-line quoted fields do not occur in these
    files. Streaming, so a file that is mostly padding costs nothing to hold.

    `utf-8-sig` strips a leading BOM (some export tools and any Windows-side
    text editor add one) so it never ends up glued onto the WigleWifi marker
    or the header's first column name.
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for line in f:
            if len(line) > MAX_LINE_BYTES or not line.strip():
                continue
            # A NUL has no business in a text capture, and Python 3.10's csv
            # module refuses the whole line over one; later versions keep it
            # inside the field. Dropping it reads the same row everywhere.
            cleaned = _clean_marauder_line(line.replace("\x00", ""))
            try:
                yield next(csv.reader([cleaned]), [])
            except csv.Error:
                yield [cleaned]


def _read_all_rows(path: Path) -> list[list[str]]:
    return list(_iter_rows(path))


def _map_columns(header: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in header]
    colmap: dict[str, int] = {}
    for logical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                colmap[logical] = lowered.index(alias)
                break
    # Fallback for lat/lon on truly generic files: any header containing
    # "lat" / "lon" / "lng" as a substring, if the exact-alias pass missed.
    if "lat" not in colmap:
        for i, h in enumerate(lowered):
            if "lat" in h:
                colmap["lat"] = i
                break
    if "lon" not in colmap:
        for i, h in enumerate(lowered):
            if "lon" in h or "lng" in h:
                colmap["lon"] = i
                break
    return colmap


def _cell(row: list[str], colmap: dict[str, int], key: str) -> Optional[str]:
    idx = colmap.get(key)
    if idx is None or idx >= len(row):
        return None
    value = row[idx].strip()
    return value if value != "" else None


def classify_encryption(auth_mode: Optional[str], record_type: str = TYPE_WIFI) -> str:
    """Bucket a raw AuthMode string (Marauder/Wigle brackets like
    "[WPA2-PSK-CCMP][ESS]", or a plainer "WPA2", or blank for an open AP)
    into one of the fixed ENC_BUCKETS.

    Bluetooth rows generally don't carry a meaningful AuthMode at all. An
    empty field there means "we don't know", not "open", so only Wi-Fi falls
    through to Open when the field is blank.
    """
    s = (auth_mode or "").upper()
    has_wpa3 = "WPA3" in s or "SAE" in s
    has_wpa2 = "WPA2" in s
    has_wep = "WEP" in s
    if has_wpa3 and has_wpa2:
        return ENC_WPA23_MIXED
    if has_wpa3:
        return ENC_WPA3
    if has_wpa2:
        return ENC_WPA2
    if has_wep:
        return ENC_WEP
    if "WPA" in s:
        return ENC_WPA
    if "OWE" in s:
        return ENC_OPEN
    if "OPEN" in s:
        return ENC_OPEN
    if not s or s == "[ESS]" or s == "ESS":
        return ENC_OPEN if record_type == TYPE_WIFI else ENC_UNKNOWN
    return ENC_UNKNOWN


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(round(float(value)))
    except (ValueError, OverflowError):
        return None
    return parsed if abs(parsed) <= MAX_INT_CELL else None


def _row_to_sighting(row: list[str], colmap: dict[str, int]) -> Optional[Sighting]:
    bssid = _cell(row, colmap, "bssid")
    if not bssid:
        return None

    lat = _parse_float(_cell(row, colmap, "lat"))
    lon = _parse_float(_cell(row, colmap, "lon"))
    if lat is None or lon is None:
        return None  # missing/blank fix, skip, don't crash
    if lat == 0.0 and lon == 0.0:
        return None  # 0/0 is Wigle/Marauder's "no GPS fix", not a real location
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None  # a corrupt row, not a place on Earth

    raw_type = (_cell(row, colmap, "type") or TYPE_WIFI).strip().upper()
    if raw_type in _TYPE_MAP:
        record_type = _TYPE_MAP[raw_type]
    elif raw_type in ALL_TYPES:
        # warmap's own CSV export writes its full type set, so reading one
        # back has to preserve it. Without this a Sub-GHz record exported to
        # CSV and re-imported comes back as a Wi-Fi access point.
        record_type = raw_type
    else:
        record_type = TYPE_WIFI

    auth_mode = _cell(row, colmap, "auth") or ""
    rssi = _parse_int(_cell(row, colmap, "rssi"))
    channel = _parse_int(_cell(row, colmap, "channel"))
    frequency = _parse_float(_cell(row, colmap, "frequency"))

    # Marauder writes Channel=0 on BLE rows, where a Wi-Fi channel number
    # means nothing. Zero is not a channel, so it becomes "no channel" rather
    # than a bogus entry in the channel filter and the channel histogram.
    if channel == 0:
        channel = None

    # Wigle 1.4 has no Frequency column, so derive one from the channel; the
    # band filter and the stats panel both key off frequency.
    if frequency is None:
        frequency = radio.wifi_channel_to_freq(channel)
    elif channel is None:
        channel = radio.wifi_freq_to_channel(frequency)

    normalized = ble.normalize_mac(bssid) or bssid.strip().upper()

    meta: dict = {}
    if rssi is None:
        # Sighting.rssi is a plain int, not Optional, so a missing/garbage
        # RSSI cell still has to become *some* number: 0 is what real
        # Wi-Fi/BT dBm readings never are, so it reads as "no signal" rather
        # than a plausible measurement. But `dedup`'s "strongest RSSI wins"
        # compares that fabricated 0 against real (negative) readings of the
        # same identity, and 0 > -70 would let a row with no real signal
        # information "win" over one that actually measured something. This
        # flag is how dedup tells the two apart; it's stripped back out
        # before the merged record is built; see dedup() below.
        meta["rssi_unknown"] = True
    if raw_type in ("GSM", "LTE", "NR", "CDMA", "WCDMA"):
        meta["cell_technology"] = raw_type
    rcois = _cell(row, colmap, "rcois")
    if rcois:
        meta["rcois"] = rcois
    mfgrid = _cell(row, colmap, "mfgrid")
    if mfgrid:
        meta["mfgr_id"] = mfgrid
        try:
            # Wigle writes this as a decimal Bluetooth SIG company ID, but
            # some exporters use "0x004C"; int(x, 0) takes either.
            company_id = int(mfgrid, 0) if len(mfgrid) <= 8 else None
        except (TypeError, ValueError):
            company_id = None
        if company_id is not None:
            meta["company_id"] = company_id
            meta["company"] = ble.company_name(company_id)

    sighting = Sighting(
        bssid=normalized,
        ssid=_cell(row, colmap, "ssid") or "",
        auth_mode=auth_mode,
        enc_bucket=classify_encryption(auth_mode, record_type),
        first_seen=_cell(row, colmap, "first_seen") or "",
        channel=channel,
        rssi=rssi if rssi is not None else 0,
        lat=lat,
        lon=lon,
        altitude=_parse_float(_cell(row, colmap, "altitude")),
        accuracy=_parse_float(_cell(row, colmap, "accuracy")),
        type=record_type,
        times_seen=1,
        frequency=frequency,
        vendor=ble.oui_vendor(normalized) or "",
        geo_source=GEO_DIRECT,
        meta=meta,
    )

    if record_type in (TYPE_BLE, TYPE_BT):
        derived = ble.describe(normalized, {**meta, "name": sighting.ssid})
        sighting.meta.update(derived)
        if not sighting.vendor and derived.get("vendor"):
            sighting.vendor = derived["vendor"]

    return sighting


# Kept as the old name because it's the documented one; `_row_to_ap` was
# only ever called from this module.
_row_to_ap = _row_to_sighting


def parse_file(path: Path) -> list[Sighting]:
    """Every valid sighting in `path`, one Sighting per row (times_seen=1
    each; call dedup to collapse repeats). Never raises on a malformed
    file: empty file, header-only file, and rows that don't parse all just
    contribute nothing."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
    except OSError:
        return []
    try:
        rows = _iter_rows(path)
        first = next(rows, None)
        if first is None:
            return []
        if first and first[0].startswith("WigleWifi"):
            header = next(rows, None)
            if header is None:
                return []  # metadata line only, no header/data
        else:
            header = first
        if len(header) > MAX_HEADER_COLUMNS:
            return []
        colmap = _map_columns(header)
        if "bssid" not in colmap or "lat" not in colmap or "lon" not in colmap:
            return []  # not a CSV shape we understand at all
        return _rows_to_sightings(rows, colmap, str(path))
    except OSError:
        return []


def _rows_to_sightings(rows, colmap: dict[str, int], source: str) -> list[Sighting]:
    sightings = []
    pending_name: Optional[str] = None
    for row in rows:
        if not row or all(not c.strip() for c in row):
            continue
        # Marauder prints "Device: <name>" on its own line right before each
        # BLE wardrive row. The Flipper companion app drops those lines, so a
        # companion-app capture never has them, but a capture that saved the
        # raw serial stream does, and it's the only place a BLE name appears in
        # a wardrive at all (the CSV row itself is MAC and RSSI, nothing else).
        # The line isn't CSV; the reader hands it back as one cell, so a real
        # data row (which starts with a MAC) can't be mistaken for it. Rejoin
        # in case the name itself had a comma.
        if row[0].strip().startswith("Device:"):
            name = ",".join(row).split("Device:", 1)[1].strip()
            pending_name = name or None
            continue
        try:
            sighting = _row_to_sighting(row, colmap)
        except (ValueError, IndexError):
            pending_name = None
            continue
        if sighting is not None:
            if (pending_name
                    and sighting.type in (TYPE_BLE, TYPE_BT)
                    and not sighting.ssid):
                sighting.ssid = pending_name
                # Re-derive with the name so a name-only tracker match (a device
                # that calls itself "AirTag"/"Tile") is caught too.
                derived = ble.describe(sighting.bssid, {**sighting.meta, "name": pending_name})
                sighting.meta.update(derived)
            sighting.source = source
            sightings.append(sighting)
            if len(sightings) >= MAX_ROWS_PER_FILE:
                break
        pending_name = None
    return sightings


def parse_files(paths: Iterable[Path]) -> list[Sighting]:
    """Raw sightings across every file in `paths`, concatenated (not
    deduped): one bad file among several never drops the good ones."""
    out: list[Sighting] = []
    for p in paths:
        out.extend(parse_file(p))
    return out


def dedup(records: Iterable[Sighting]) -> list[Sighting]:
    """Collapse repeat sightings of the same thing into one record: the
    strongest RSSI wins for the descriptive fields, `times_seen` is the sum
    of every input record's own `times_seen` (so this doubles as both "dedup
    a batch of raw sightings" and "merge new sightings into an
    already-deduped store", see store.py).

    Identity is scoped by record type, so an NFC UID can never collide with a
    Wi-Fi BSSID that happens to have the same digits.

    A record that has a location always beats one that doesn't, regardless of
    signal, otherwise merging a placed sighting with an unplaced one could
    throw the location away.
    """
    counts: dict[str, int] = {}
    best: dict[str, Sighting] = {}
    order: list[str] = []
    merged_meta: dict[str, dict] = {}
    # Every distinct place a given identity was seen, snapped to a ~11 m grid so
    # GPS jitter at one spot doesn't read as movement. A device (especially a
    # Bluetooth one) seen across many places that span real distance is the
    # signature of something travelling with you, a tracker/follower.
    locs: dict[str, set] = {}

    for r in records:
        key = r.ident
        counts[key] = counts.get(key, 0) + max(r.times_seen, 1)
        if key not in best:
            order.append(key)
            merged_meta[key] = {}
        # rssi_unknown (see _row_to_sighting) describes one raw observation's
        # RSSI, not the merged identity. It exists only to steer the
        # comparison below, so it must not leak into the persisted/exported
        # meta of whichever record ends up winning.
        merged_meta[key].update({k: v for k, v in r.meta.items() if k != "rssi_unknown"})
        if r.has_location:
            locs.setdefault(key, set()).add((round(r.lat, 4), round(r.lon, 4)))

        current = best.get(key)
        if current is None:
            best[key] = r
            continue
        if r.has_location and not current.has_location:
            best[key] = r
            continue
        if r.has_location != current.has_location:
            continue  # current already has the location and r doesn't
        # Same location status: a fabricated 0 dBm (see _row_to_sighting)
        # must never outrank a real reading just because 0 > -70, so a known
        # RSSI always beats an unknown one before the numbers are compared.
        r_known = not r.meta.get("rssi_unknown")
        current_known = not current.meta.get("rssi_unknown")
        if r_known and not current_known:
            best[key] = r
        elif r_known == current_known and r.rssi > current.rssi:
            best[key] = r

    from warmap.gps import haversine_km

    result = []
    for key in order:
        winner = best[key]
        cells = locs.get(key)
        if cells and len(cells) > 1:
            lats = [c[0] for c in cells]
            lons = [c[1] for c in cells]
            span_m = haversine_km(min(lats), min(lons), max(lats), max(lons)) * 1000.0
            merged_meta[key]["location_count"] = len(cells)
            merged_meta[key]["span_m"] = round(span_m, 1)
            # A capped trail of the distinct spots, for the map to draw. Sorted
            # so the output is deterministic; thinned to 40 points at most.
            pts = sorted(cells)
            if len(pts) > 40:
                step = len(pts) / 40.0
                pts = [pts[int(i * step)] for i in range(40)]
            merged_meta[key]["locations"] = [[la, lo] for la, lo in pts]
        result.append(
            Sighting(
                bssid=winner.bssid,
                ssid=winner.ssid,
                auth_mode=winner.auth_mode,
                enc_bucket=winner.enc_bucket,
                first_seen=winner.first_seen,
                channel=winner.channel,
                rssi=winner.rssi,
                lat=winner.lat,
                lon=winner.lon,
                altitude=winner.altitude,
                accuracy=winner.accuracy,
                type=winner.type,
                times_seen=counts[key],
                frequency=winner.frequency,
                vendor=winner.vendor,
                source=winner.source,
                geo_source=winner.geo_source,
                meta=merged_meta[key],
            )
        )
    return result


# The original name, from when Wi-Fi was the only thing this handled.
dedup_by_bssid = dedup
