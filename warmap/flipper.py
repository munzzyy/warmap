"""Reading the files a Flipper Zero saves to its SD card.

All of them share one container, which the firmware calls Flipper File Format:
UTF-8 text, one `Key: value` per line, `#` for comments. Files that hold more
than one thing (an `.ir` remote is many buttons) separate records with a `#`
line. `read_fff` handles that container; everything below it is per-type
interpretation.

On stock firmware none of these files contain a location or a timestamp: a
`.sub` knows its frequency and its key, not where or when you caught it. The
custom firmwares are the exception, and it's worth knowing which:

* **Momentum and RogueMaster** write `Lat:` and `Lon:` into every
  recognized-protocol `.sub`, from an attached GPS module. The keys are
  written whether or not a module is present, so `0.000000 / 0.000000` means
  "no fix" and must not be read as a point off the coast of Africa.
* **Xtreme** does the same but misspells the key as `Latitute:`.
* Weather-station and TPMS captures on those forks also carry `Ts:`, a real
  Unix timestamp from the Flipper's RTC, better evidence of when a capture
  happened than any file mtime.
* `Protocol: RAW` captures never get coordinates on any firmware, and
  `.nfc` / `.rfid` / `.ibtn` / `.ir` never do.

So warmap reads those when they're there, and falls back to placing the
capture by matching the file's timestamp against a GPS track (see gps.py)
when they're not. That fallback has one sharp edge worth knowing:

    Copying files off the SD card with plain `cp` sets the mtime to *now*.
    Use `cp -p` (or rsync -a) or the timestamps are gone and nothing can be
    placed.

warmap checks for that: if a batch of files all share a suspiciously recent
mtime, `mtime_looks_clobbered` says so and the UI warns instead of silently
dropping every Flipper capture onto the same spot.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from warmap import radio
from warmap.models import (
    ENC_UNKNOWN,
    GEO_DIRECT,
    GEO_NONE,
    TYPE_IBUTTON,
    TYPE_IR,
    TYPE_NFC,
    TYPE_RFID,
    TYPE_SUBGHZ,
    Sighting,
)

# Which file extension is which kind of capture. `.picopass` is the iCLASS
# app's own extension; it's close enough to NFC to share the type.
FLIPPER_EXTENSIONS = {
    ".sub": TYPE_SUBGHZ,
    ".nfc": TYPE_NFC,
    ".picopass": TYPE_NFC,
    ".rfid": TYPE_RFID,
    ".ibtn": TYPE_IBUTTON,
    ".ir": TYPE_IR,
}

# Where each type lives on a stock Flipper SD card. Used to give the folder
# importer a fast path and to recognize a card even when it's mounted under
# an unfamiliar name.
FLIPPER_SD_DIRS = {
    "subghz": TYPE_SUBGHZ,
    "nfc": TYPE_NFC,
    "lfrfid": TYPE_RFID,
    "ibutton": TYPE_IBUTTON,
    "infrared": TYPE_IR,
}

# Keys worth surfacing per type. Anything else in the file still gets counted
# but isn't splashed into the popup: a Mifare Classic dump has 64 block lines
# and none of them belong in a map tooltip.
_INTERESTING_KEYS = {
    # Temp/Hum/Batt/Ch/Id show up on weather-station and TPMS decodes, which
    # are genuinely interesting things to have mapped.
    TYPE_SUBGHZ: ("Frequency", "Preset", "Protocol", "Bit", "Key", "TE", "Te",
                  "Serial", "Btn", "Cnt", "Manufacture", "Id", "Data",
                  "Temp", "Hum", "Batt", "Ch"),
    TYPE_NFC: ("Device type", "UID", "ATQA", "SAK", "Data format version",
               "Mifare Classic type", "Key A", "Application type",
               "Manufacture id", "Blocks total"),
    TYPE_RFID: ("Key type", "Data"),
    # Current firmware writes `Protocol` + `Rom Data` (with a space); the
    # older format used `Key type` + `Data`.
    TYPE_IBUTTON: ("Protocol", "Rom Data", "Rom_data", "Key type", "Data"),
    TYPE_IR: ("name", "type", "protocol", "address", "command", "frequency",
              "duty_cycle"),
}

# A Flipper capture is kilobytes. Anything wildly bigger is not one, and
# reading it costs far more than its size on disk: decoding non-UTF-8 bytes
# with errors="replace" roughly doubles the memory, and splitlines() doubles it
# again. A symlink on an SD card pointing at some large file elsewhere is
# enough to trigger it, so the cap is on the size, not on trust.
MAX_FILE_BYTES = 16 * 1024 * 1024

_TIMESTAMP_PATTERNS = (
    # 20240115-143022 / 20240115_143022
    (re.compile(r"(\d{8})[-_](\d{6})"), "%Y%m%d%H%M%S"),
    # 2024-01-15T14-30-22 / 2024-01-15_14-30-22
    (re.compile(r"(\d{4}-\d{2}-\d{2})[T_](\d{2}-\d{2}-\d{2})"), "%Y-%m-%d%H-%M-%S"),
)


def read_fff(text: str) -> tuple[dict, list[dict]]:
    """Split Flipper File Format text into (header, records).

    The header is every key before the first `#` separator. Records are the
    key groups after it, empty for single-record files like `.sub`, one per
    button for `.ir`.

    Repeated keys accumulate into a single space-joined value, which is how
    the firmware itself writes long `RAW_Data` payloads across several lines.
    """
    header: dict = {}
    records: list[dict] = []
    current: Optional[dict] = None

    def finish(parts: dict) -> dict:
        return {key: " ".join(values).strip() for key, values in parts.items()}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # A separator starts a new record. Consecutive separators, and a
            # trailing one at end of file, must not produce empty records.
            if current:
                records.append(finish(current))
            current = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        target = current if current is not None else header
        target.setdefault(key.strip(), []).append(value.strip())

    if current:
        records.append(finish(current))
    return finish(header), records


def _get(d: dict, *names: str) -> Optional[str]:
    """First present, non-empty value among `names`. Flipper key names have
    drifted between firmware versions (iButton went from `Key type`/`Data` to
    `Protocol`/`Rom_data`), so every reader accepts both spellings."""
    for name in names:
        value = d.get(name)
        if value:
            return value
    for name in names:
        lowered = name.lower()
        for k, v in d.items():
            if k.lower() == lowered and v:
                return v
    return None


def timestamp_from_name(path: Path) -> Optional[datetime]:
    """A datetime embedded in the filename, if one is. Some firmwares and
    most manual naming conventions include one, and it survives a careless
    copy where mtime doesn't, so it wins over mtime when present."""
    stem = Path(path).stem
    for pattern, fmt in _TIMESTAMP_PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        try:
            return datetime.strptime("".join(m.groups()), fmt)
        except ValueError:
            continue
    return None


def file_timestamp(path: Path) -> Optional[datetime]:
    """When this capture happened, as best as can be told: a timestamp in the
    filename if there is one, else the file's mtime."""
    path = Path(path)
    from_name = timestamp_from_name(path)
    if from_name is not None:
        return from_name
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except (OSError, OverflowError, ValueError):
        return None


def mtime_looks_clobbered(paths, window_seconds: int = 120) -> bool:
    """True if these files' mtimes are all within a couple of minutes of each
    other AND of right now: the signature of a plain `cp` off the SD card,
    which rewrites every mtime to the moment of the copy.

    Needs at least three files to call it: two files genuinely captured
    seconds apart is ordinary, twenty of them is not.
    """
    stamps = []
    for p in paths:
        if timestamp_from_name(Path(p)) is not None:
            continue  # this one carries its own time, mtime doesn't matter
        try:
            stamps.append(datetime.fromtimestamp(Path(p).stat().st_mtime))
        except (OSError, OverflowError, ValueError):
            continue
    if len(stamps) < 3:
        return False
    spread = max(stamps) - min(stamps)
    if spread > timedelta(seconds=window_seconds):
        return False
    return (datetime.now() - max(stamps)) <= timedelta(seconds=window_seconds)


def _meta_for(kind: str, header: dict, record: Optional[dict] = None) -> dict:
    src = dict(header)
    if record:
        src.update(record)
    wanted = _INTERESTING_KEYS.get(kind, ())
    meta = {}
    for key in wanted:
        value = src.get(key)
        if value:
            meta[key.lower().replace(" ", "_")] = value
    extra = len(src) - len(meta)
    if extra > 0:
        meta["extra_fields"] = extra
    return meta


def embedded_location(header: dict) -> Optional[tuple]:
    """(lat, lon) if a custom firmware tagged this capture with a GPS fix.

    Momentum and RogueMaster write `Lat:`/`Lon:`; Xtreme ships the same
    feature with `Latitute:` misspelled. All three write the keys even with no
    GPS module attached, in which case both read 0.000000, which is "no fix",
    not a real coordinate, exactly as in a Wigle CSV.
    """
    lat_raw = _get(header, "Lat", "Latitude", "Latitute")
    lon_raw = _get(header, "Lon", "Longitude")
    if lat_raw is None or lon_raw is None:
        return None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


def embedded_timestamp(header: dict) -> Optional[datetime]:
    """A `Ts:` Unix timestamp, written by the weather-station and TPMS
    decoders on the custom firmwares. Real capture time from the Flipper's
    RTC, so it beats both the filename and the mtime."""
    raw = _get(header, "Ts")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _base(path: Path, kind: str, ident: str, label: str, when: Optional[datetime]) -> Sighting:
    return Sighting(
        bssid=ident,
        ssid=label,
        auth_mode="",
        enc_bucket=ENC_UNKNOWN,
        first_seen=when.strftime("%Y-%m-%d %H:%M:%S") if when else "",
        channel=None,
        rssi=0,
        lat=None,
        lon=None,
        altitude=None,
        accuracy=None,
        type=kind,
        times_seen=1,
        source=str(path),
        geo_source=GEO_NONE,
    )


# --- per-type readers ----------------------------------------------------

def _parse_subghz(path: Path, header: dict, when) -> list[Sighting]:
    protocol = _get(header, "Protocol") or "RAW"
    key = _get(header, "Key")
    freq_hz = _get(header, "Frequency")
    freq_mhz = None
    if freq_hz:
        try:
            freq_mhz = round(float(freq_hz) / 1_000_000.0, 5)
        except ValueError:
            freq_mhz = None

    # A decoded capture is identified by its protocol and key, so the same
    # remote pressed twice in two files collapses to one record. A RAW
    # capture has no key and is unique per file.
    if key:
        ident = f"{protocol}:{key.replace(' ', '')}"
    else:
        ident = f"RAW:{path.stem}"

    # A `Ts:` field is the Flipper's own RTC reading at decode time, which is
    # better evidence than anything derived from the file itself.
    stamped = embedded_timestamp(header)
    if stamped is not None:
        when = stamped

    rec = _base(path, TYPE_SUBGHZ, ident, path.stem, when)
    rec.frequency = freq_mhz
    rec.meta = _meta_for(TYPE_SUBGHZ, header)
    rec.meta.update(radio.describe_subghz(freq_mhz, protocol))
    preset = radio.preset_label(_get(header, "Preset"))
    if preset:
        rec.meta["preset_label"] = preset
    if freq_mhz is not None:
        rec.meta["frequency_mhz"] = freq_mhz
    if stamped is not None:
        rec.meta["timestamp_source"] = "Ts field written by the firmware"

    # Momentum/RogueMaster/Xtreme tag saves with a GPS fix. That's a real
    # measurement, so it takes precedence over anything a track could infer.
    fix = embedded_location(header)
    if fix is not None:
        rec.lat, rec.lon = fix
        rec.geo_source = GEO_DIRECT
        rec.meta["location_source"] = "GPS fix saved by the Flipper firmware"

    return [rec]


def _parse_nfc(path: Path, header: dict, when) -> list[Sighting]:
    uid = _get(header, "UID")
    device_type = _get(header, "Device type") or "unknown"
    ident = uid.replace(" ", ":").upper() if uid else f"NFC:{path.stem}"
    rec = _base(path, TYPE_NFC, ident, path.stem, when)
    rec.meta = _meta_for(TYPE_NFC, header)
    rec.meta["technology"] = device_type
    if uid:
        rec.meta["uid_bytes"] = len(uid.split())
    return [rec]


def _parse_rfid(path: Path, header: dict, when) -> list[Sighting]:
    key_type = _get(header, "Key type") or "unknown"
    data = _get(header, "Data") or ""
    ident = f"{key_type}:{data.replace(' ', '')}" if data else f"RFID:{path.stem}"
    rec = _base(path, TYPE_RFID, ident, path.stem, when)
    rec.frequency = 0.125  # 125 kHz, expressed in MHz like every other record
    rec.meta = _meta_for(TYPE_RFID, header)
    rec.meta["protocol"] = key_type
    return [rec]


def _parse_ibutton(path: Path, header: dict, when) -> list[Sighting]:
    protocol = _get(header, "Protocol", "Key type") or "unknown"
    # Current firmware writes "Rom Data" with a space; older builds wrote
    # "Data" under a "Key type" header.
    data = _get(header, "Rom Data", "Rom_data", "Data") or ""
    ident = f"{protocol}:{data.replace(' ', '')}" if data else f"IBTN:{path.stem}"
    rec = _base(path, TYPE_IBUTTON, ident, path.stem, when)
    rec.meta = _meta_for(TYPE_IBUTTON, header)
    rec.meta["protocol"] = protocol
    return [rec]


def _parse_ir(path: Path, header: dict, records: list[dict], when) -> list[Sighting]:
    """One Sighting per button. An `.ir` file is a whole remote, and the
    interesting unit is the individual signal, not the file."""
    out = []
    for index, record in enumerate(records):
        name = _get(record, "name") or f"signal {index + 1}"
        sig_type = (_get(record, "type") or "parsed").lower()
        if sig_type == "raw":
            ident = f"IR-RAW:{path.stem}:{name}"
        else:
            protocol = _get(record, "protocol") or "unknown"
            address = (_get(record, "address") or "").replace(" ", "")
            command = (_get(record, "command") or "").replace(" ", "")
            ident = f"{protocol}:{address}:{command}"

        rec = _base(path, TYPE_IR, ident, f"{path.stem} / {name}", when)
        rec.meta = _meta_for(TYPE_IR, header, record)
        rec.meta["signal_type"] = sig_type
        freq = _get(record, "frequency")
        if freq:
            try:
                rec.frequency = round(float(freq) / 1_000_000.0, 6)
            except ValueError:
                pass
        out.append(rec)

    if not out:
        # A remote file with no parseable buttons still deserves a record so
        # it isn't silently swallowed.
        out.append(_base(path, TYPE_IR, f"IR:{path.stem}", path.stem, when))
    return out


def parse_flipper_file(path: Path) -> list[Sighting]:
    """Every capture in one Flipper file. Empty list if it isn't one, is
    unreadable, or holds nothing recognizable. Never raises."""
    path = Path(path)
    kind = FLIPPER_EXTENSIONS.get(path.suffix.lower())
    if kind is None:
        return []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, MemoryError):
        return []
    if not text.strip():
        return []

    header, records = read_fff(text)
    if not header and not records:
        return []

    when = file_timestamp(path)
    try:
        if kind == TYPE_SUBGHZ:
            return _parse_subghz(path, header, when)
        if kind == TYPE_NFC:
            return _parse_nfc(path, header, when)
        if kind == TYPE_RFID:
            return _parse_rfid(path, header, when)
        if kind == TYPE_IBUTTON:
            return _parse_ibutton(path, header, when)
        if kind == TYPE_IR:
            return _parse_ir(path, header, records, when)
    except (ValueError, TypeError, KeyError):
        return []
    return []


def is_flipper_file(path: Path) -> bool:
    return Path(path).suffix.lower() in FLIPPER_EXTENSIONS


def looks_like_flipper_sd(root: Path) -> bool:
    """True if `root` looks like a mounted Flipper SD card: at least two of
    the standard app directories present."""
    root = Path(root)
    try:
        hits = sum(1 for name in FLIPPER_SD_DIRS if (root / name).is_dir())
    except OSError:
        return False
    return hits >= 2
