"""Reading Flipper Zero capture files: the Flipper File Format container,
each per-type reader, and the timestamp handling the whole geotagging story
depends on.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from warmap import flipper
from warmap.models import (
    GEO_NONE,
    TYPE_IBUTTON,
    TYPE_IR,
    TYPE_NFC,
    TYPE_RFID,
    TYPE_SUBGHZ,
)

SUB_PARSED = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 12 34 56
TE: 403
"""

SUB_ROLLING = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: KeeLoq
Bit: 64
Key: 3F 21 9C 4A 7E 05 B8 D2
Serial: 00 1D 4A 7E
Btn: 2
Cnt: 004E
"""

SUB_RAW = """Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 315000000
Preset: FuriHalSubGhzPresetOok270Async
Protocol: RAW
RAW_Data: -25049 471 -238 476
RAW_Data: -235 950 -710 475
"""

NFC_FILE = """Filetype: Flipper NFC device
Version: 4
Device type: Mifare Classic
UID: 04 A3 91 2B 6C 5D 80
ATQA: 00 44
SAK: 08
"""

RFID_FILE = """Filetype: Flipper RFID key
Version: 1
Key type: EM4100
Data: 1A 2B 3C 4D 5E
"""

IBTN_FILE = """Filetype: Flipper iButton key
Version: 1
Protocol: DS1990
Rom_data: 01 A4 7B 22 0C 00 00 3D
"""

IBTN_OLD_FILE = """Filetype: Flipper iButton key
Version: 1
Key type: Dallas
Data: 01 A4 7B 22 0C 00 00 3D
"""

IR_FILE = """Filetype: IR signals file
Version: 1
#
name: Power
type: parsed
protocol: NEC
address: 04 00 00 00
command: 08 00 00 00
#
name: Vol_up
type: raw
frequency: 38000
duty_cycle: 0.330000
data: 8964 4432 559 1671
"""


def _write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# --- the container -------------------------------------------------------

def test_read_fff_splits_header_and_records():
    header, records = flipper.read_fff(IR_FILE)
    assert header["Filetype"] == "IR signals file"
    assert len(records) == 2
    assert records[0]["name"] == "Power"
    assert records[1]["type"] == "raw"


def test_read_fff_joins_repeated_keys():
    header, _ = flipper.read_fff(SUB_RAW)
    assert header["RAW_Data"] == "-25049 471 -238 476 -235 950 -710 475"


def test_read_fff_ignores_lines_without_a_colon():
    header, records = flipper.read_fff("Filetype: x\ngarbage line\nVersion: 1\n")
    assert header == {"Filetype": "x", "Version": "1"}
    assert records == []


def test_read_fff_trailing_separator_makes_no_empty_record():
    _, records = flipper.read_fff("Filetype: x\n#\nname: a\n#\n")
    assert len(records) == 1


def test_read_fff_value_containing_a_colon_survives():
    header, _ = flipper.read_fff("Note: see 12:30 for detail\n")
    assert header["Note"] == "see 12:30 for detail"


# --- sub-GHz -------------------------------------------------------------

def test_subghz_parsed_capture(tmp_path):
    path = _write(tmp_path, "Garage.sub", SUB_PARSED)
    records = flipper.parse_flipper_file(path)
    assert len(records) == 1
    record = records[0]
    assert record.type == TYPE_SUBGHZ
    assert record.frequency == 433.92
    assert record.meta["protocol"] == "Princeton"
    assert record.meta["code_type"] == "static"
    assert record.ssid == "Garage"


def test_subghz_rolling_code_classified(tmp_path):
    path = _write(tmp_path, "Car.sub", SUB_ROLLING)
    record = flipper.parse_flipper_file(path)[0]
    assert record.meta["code_type"] == "rolling"
    assert record.meta["cnt"] == "004E"


def test_subghz_raw_capture_is_unique_per_file(tmp_path):
    a = flipper.parse_flipper_file(_write(tmp_path, "RawA.sub", SUB_RAW))[0]
    b = flipper.parse_flipper_file(_write(tmp_path, "RawB.sub", SUB_RAW))[0]
    assert a.meta["code_type"] == "raw"
    # Two raw captures of the same thing are still two captures, so they must
    # not collapse into one record on dedup.
    assert a.ident != b.ident


def test_subghz_same_key_in_two_files_shares_an_identity(tmp_path):
    a = flipper.parse_flipper_file(_write(tmp_path, "One.sub", SUB_PARSED))[0]
    b = flipper.parse_flipper_file(_write(tmp_path, "Two.sub", SUB_PARSED))[0]
    assert a.ident == b.ident


def test_subghz_band_and_preset_decoded(tmp_path):
    record = flipper.parse_flipper_file(_write(tmp_path, "G.sub", SUB_PARSED))[0]
    assert record.meta["band"] == "387-464 MHz"
    assert "433.92" in record.meta["frequency_note"] or record.meta["frequency_note"]
    assert "OOK" in record.meta["preset_label"]


# --- other types ---------------------------------------------------------

def test_nfc_uses_uid_as_identity(tmp_path):
    record = flipper.parse_flipper_file(_write(tmp_path, "Badge.nfc", NFC_FILE))[0]
    assert record.type == TYPE_NFC
    assert record.bssid == "04:A3:91:2B:6C:5D:80"
    assert record.meta["technology"] == "Mifare Classic"
    assert record.meta["uid_bytes"] == 7


def test_rfid_carries_protocol_and_frequency(tmp_path):
    record = flipper.parse_flipper_file(_write(tmp_path, "Fob.rfid", RFID_FILE))[0]
    assert record.type == TYPE_RFID
    assert record.meta["protocol"] == "EM4100"
    assert record.frequency == 0.125


def test_ibutton_current_key_names(tmp_path):
    record = flipper.parse_flipper_file(_write(tmp_path, "Key.ibtn", IBTN_FILE))[0]
    assert record.type == TYPE_IBUTTON
    assert record.meta["protocol"] == "DS1990"


def test_ibutton_older_key_names_still_read(tmp_path):
    """Firmware renamed these keys; a card written by an older build must
    still load."""
    record = flipper.parse_flipper_file(_write(tmp_path, "Old.ibtn", IBTN_OLD_FILE))[0]
    assert record.type == TYPE_IBUTTON
    assert record.meta["protocol"] == "Dallas"
    assert record.bssid.endswith("01A47B220C00003D")


def test_ir_file_yields_one_record_per_button(tmp_path):
    records = flipper.parse_flipper_file(_write(tmp_path, "TV.ir", IR_FILE))
    assert len(records) == 2
    assert all(r.type == TYPE_IR for r in records)
    assert records[0].meta["signal_type"] == "parsed"
    assert records[1].meta["signal_type"] == "raw"
    assert records[1].frequency == 0.038


def test_ir_parsed_identity_is_protocol_address_command(tmp_path):
    record = flipper.parse_flipper_file(_write(tmp_path, "TV.ir", IR_FILE))[0]
    assert record.bssid == "NEC:04000000:08000000"


# --- robustness ----------------------------------------------------------

def test_unknown_extension_returns_empty(tmp_path):
    assert flipper.parse_flipper_file(_write(tmp_path, "x.png", "junk")) == []


def test_empty_file_returns_empty(tmp_path):
    assert flipper.parse_flipper_file(_write(tmp_path, "x.sub", "")) == []


def test_missing_file_returns_empty(tmp_path):
    assert flipper.parse_flipper_file(tmp_path / "nope.sub") == []


def test_garbage_body_does_not_raise(tmp_path):
    records = flipper.parse_flipper_file(_write(tmp_path, "x.sub", "\x00\xff not a file"))
    assert isinstance(records, list)


def test_bom_prefixed_file_parses_the_same_as_a_clean_one(tmp_path):
    # A BOM would land on the "Filetype" key's name, not any value this
    # module actually branches on (kind is decided by extension, and nothing
    # here reads the Filetype value), confirmed rather than assumed, since
    # the CSV reader in parse.py has the same shape of bug for its own
    # metadata-line check.
    path = tmp_path / "bom.sub"
    path.write_bytes(("﻿" + SUB_PARSED).encode("utf-8"))
    record = flipper.parse_flipper_file(path)[0]
    assert record.meta["protocol"] == "Princeton"
    assert record.frequency == 433.92


# --- card yanked mid-write -------------------------------------------------
# Extremely common in the field: the SD card comes out while the firmware is
# still writing the file. Every one of these has to degrade gracefully:
# whatever was fully written before the cut still becomes a record, rather
# than silently discard the capture entirely.

def test_truncated_momentum_sub_with_no_lon_at_all_has_no_location(tmp_path):
    # Cut off right after the Lat: line. Lon: never got written at all.
    body = (
        "Filetype: Flipper SubGhz Key File\n"
        "Version: 1\n"
        "Frequency: 433920000\n"
        "Preset: FuriHalSubGhzPresetOok270Async\n"
        "Lat: 37.774900\n"
    )
    record = flipper.parse_flipper_file(_write(tmp_path, "weather_trunc.sub", body))[0]
    assert record.lat is None
    assert not record.has_location  # a half-written fix must not become a guessed point


def test_truncated_nfc_uid_still_yields_a_record(tmp_path):
    # Cut off partway through the UID line, only 3 of 7 bytes present, no
    # trailing newline. The capture still gets a record with what's there,
    # not silently dropped for being incomplete.
    body = (
        "Filetype: Flipper NFC device\n"
        "Version: 4\n"
        "Device type: Mifare Classic\n"
        "UID: 04 A3 91"
    )
    records = flipper.parse_flipper_file(_write(tmp_path, "trunc.nfc", body))
    assert len(records) == 1
    assert records[0].bssid == "04:A3:91"
    assert records[0].meta["uid_bytes"] == 3


def test_truncated_ir_file_keeps_the_complete_button_before_the_cut(tmp_path):
    # Cut off right after the second "#" separator, before any keys for that
    # button were written. read_fff already drops a trailing empty record,
    # so the one complete button survives on its own.
    body = (
        "Filetype: IR signals file\n"
        "Version: 1\n"
        "#\n"
        "name: Power\n"
        "type: parsed\n"
        "protocol: NEC\n"
        "address: 04 00 00 00\n"
        "command: 08 00 00 00\n"
        "#\n"
    )
    records = flipper.parse_flipper_file(_write(tmp_path, "trunc.ir", body))
    assert len(records) == 1
    assert records[0].bssid == "NEC:04000000:08000000"


def test_truncated_sub_mid_key_value_still_yields_a_record(tmp_path):
    # Cut off mid hex-byte in the Key line, no trailing newline.
    body = (
        "Filetype: Flipper SubGhz Key File\n"
        "Version: 1\n"
        "Frequency: 433920000\n"
        "Protocol: Princeton\n"
        "Key: 00 00 00 00 00 12 34 5"
    )
    records = flipper.parse_flipper_file(_write(tmp_path, "trunc2.sub", body))
    assert len(records) == 1
    assert records[0].meta["key"] == "00 00 00 00 00 12 34 5"


def test_records_start_with_no_location(tmp_path):
    """Nothing a Flipper writes contains coordinates. If this ever changes,
    the geotagging design needs revisiting, so it's asserted rather than
    assumed."""
    record = flipper.parse_flipper_file(_write(tmp_path, "G.sub", SUB_PARSED))[0]
    assert record.lat is None and record.lon is None
    assert record.geo_source == GEO_NONE
    assert not record.has_location


# --- timestamps ----------------------------------------------------------

def test_timestamp_from_name_compact_form(tmp_path):
    path = tmp_path / "Garage_20260614-091530.sub"
    assert flipper.timestamp_from_name(path) == datetime(2026, 6, 14, 9, 15, 30)


def test_timestamp_from_name_iso_form(tmp_path):
    path = tmp_path / "cap_2026-06-14T09-15-30.sub"
    assert flipper.timestamp_from_name(path) == datetime(2026, 6, 14, 9, 15, 30)


def test_timestamp_from_name_none_when_absent(tmp_path):
    assert flipper.timestamp_from_name(tmp_path / "Garage.sub") is None


def test_filename_timestamp_beats_mtime(tmp_path):
    """A name-embedded timestamp survives a careless copy where mtime does
    not, so it has to win."""
    path = _write(tmp_path, "Garage_20260614-091530.sub", SUB_PARSED)
    os.utime(path, (0, 0))  # mtime = 1970
    record = flipper.parse_flipper_file(path)[0]
    assert record.first_seen == "2026-06-14 09:15:30"


def test_mtime_used_when_name_has_no_timestamp(tmp_path):
    path = _write(tmp_path, "Garage.sub", SUB_PARSED)
    stamp = datetime(2025, 3, 1, 12, 0, 0)
    os.utime(path, (stamp.timestamp(), stamp.timestamp()))
    record = flipper.parse_flipper_file(path)[0]
    assert record.first_seen == "2025-03-01 12:00:00"


def test_mtime_clobber_detected(tmp_path):
    """Files copied with plain `cp` all land within seconds of now. Three or
    more of those is the signature."""
    paths = []
    for i in range(4):
        paths.append(_write(tmp_path, f"cap{i}.sub", SUB_PARSED))
    assert flipper.mtime_looks_clobbered(paths) is True


def test_mtime_clobber_not_flagged_for_old_files(tmp_path):
    paths = []
    old = time.time() - 86400
    for i in range(4):
        path = _write(tmp_path, f"cap{i}.sub", SUB_PARSED)
        os.utime(path, (old, old))
        paths.append(path)
    assert flipper.mtime_looks_clobbered(paths) is False


def test_mtime_clobber_needs_more_than_two_files(tmp_path):
    paths = [_write(tmp_path, f"cap{i}.sub", SUB_PARSED) for i in range(2)]
    assert flipper.mtime_looks_clobbered(paths) is False


def test_mtime_clobber_ignores_files_that_carry_their_own_time(tmp_path):
    paths = [
        _write(tmp_path, f"cap_2026061{i}-091530.sub", SUB_PARSED) for i in range(4)
    ]
    assert flipper.mtime_looks_clobbered(paths) is False


def test_mtime_clobber_not_flagged_when_spread_out(tmp_path):
    paths = []
    now = time.time()
    for i in range(4):
        path = _write(tmp_path, f"cap{i}.sub", SUB_PARSED)
        stamp = now - i * 600  # ten minutes apart
        os.utime(path, (stamp, stamp))
        paths.append(path)
    assert flipper.mtime_looks_clobbered(paths) is False


# --- SD card layout ------------------------------------------------------

def test_looks_like_flipper_sd(tmp_path):
    for name in ("subghz", "nfc", "infrared"):
        (tmp_path / name).mkdir()
    assert flipper.looks_like_flipper_sd(tmp_path) is True


def test_looks_like_flipper_sd_false_for_one_dir(tmp_path):
    (tmp_path / "subghz").mkdir()
    assert flipper.looks_like_flipper_sd(tmp_path) is False


def test_looks_like_flipper_sd_false_for_missing_root(tmp_path):
    assert flipper.looks_like_flipper_sd(tmp_path / "gone") is False
