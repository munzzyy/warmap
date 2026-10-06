"""Checks against what the real firmware actually writes.

Every case here comes from reading the ESP32Marauder and Flipper firmware
source rather than from documentation or convention, and each one is a place
where the obvious assumption is wrong. They're grouped in one file because
what they have in common is provenance, not subject.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from warmap import flipper, ingest, parse, radio, sdcard
from warmap.models import GEO_DIRECT, GEO_NONE, TYPE_BLE, TYPE_WIFI
from warmap.radio import RISK_ROLLING, RISK_STATIC

# The real Marauder header and two real rows. Note the bracketed AuthMode on
# both, Channel=0 on the BLE row, and that the file only ever says WIFI/BLE.
MARAUDER_CSV = (
    "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder,release=v1.14.0,"
    "device=ESP32 Marauder,display=SPI TFT,board=ESP32 Marauder,brand=JustCallMeKoko\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,Type\n"
    "AA:BB:CC:DD:EE:FF,MyRouter,[WPA2_PSK],2026-07-25 14:32:10,6,-58,"
    "37.774900,-122.419400,15.20,5.00,WIFI\n"
    "11:22:33:44:55:66,,[BLE],2026-07-25 14:32:11,0,-72,"
    "37.774900,-122.419400,15.20,5.00,BLE\n"
)


# --- Marauder AuthMode strings -------------------------------------------

@pytest.mark.parametrize("auth,expected", [
    ("[OPEN]", "Open"),
    ("[WEP]", "WEP"),
    ("[WPA_PSK]", "WPA"),
    ("[WPA2_PSK]", "WPA2"),
    ("[WPA_WPA2_PSK]", "WPA2"),
    ("[WPA2_ENTERPRISE]", "WPA2"),
    ("[WPA3_PSK]", "WPA3"),
    ("[WPA2_WPA3_PSK]", "WPA2/3-mixed"),
    ("[UNDEFINED]", "Unknown"),
])
def test_marauder_auth_strings_classify(auth, expected):
    assert parse.classify_encryption(auth) == expected


def test_ble_rows_carry_a_literal_ble_authmode_not_a_blank():
    """Marauder writes the string "[BLE]". It must not read as an open
    network, which is what a blank Wi-Fi AuthMode means."""
    assert parse.classify_encryption("[BLE]", TYPE_BLE) == "Unknown"
    assert parse.classify_encryption("[BLE]", TYPE_WIFI) == "Unknown"


def test_real_marauder_csv_parses(tmp_path):
    path = tmp_path / "wardrive_0.csv"
    path.write_text(MARAUDER_CSV)
    records = parse.parse_file(path)
    assert len(records) == 2

    wifi = [r for r in records if r.type == TYPE_WIFI][0]
    assert wifi.ssid == "MyRouter"
    assert wifi.enc_bucket == "WPA2"
    assert wifi.channel == 6

    ble = [r for r in records if r.type == TYPE_BLE][0]
    assert ble.ssid == ""
    assert ble.enc_bucket == "Unknown"


def test_channel_zero_on_ble_is_not_treated_as_a_channel(tmp_path):
    """Marauder writes Channel=0 for BLE. Zero isn't a channel, and letting
    it through puts a meaningless "ch 0" in the filter and the histogram."""
    path = tmp_path / "wardrive_0.csv"
    path.write_text(MARAUDER_CSV)
    ble = [r for r in parse.parse_file(path) if r.type == TYPE_BLE][0]
    assert ble.channel is None


# --- wardrive files that aren't .csv -------------------------------------

def test_wardrive_saved_as_txt_is_recognized(tmp_path):
    """The Flipper Marauder companion app saves to
    `apps_data/marauder/dumps/wardrive_0.txt`. Filtering on the .csv
    extension would miss every capture taken that way."""
    path = tmp_path / "wardrive_0.txt"
    path.write_text(MARAUDER_CSV)
    assert ingest.classify(path) == "csv"
    assert len(ingest.ingest_paths([path]).sightings) == 2


def test_wardrive_saved_as_log_is_recognized(tmp_path):
    """A standalone Marauder writes `wardrive_0.log` to its own SD root."""
    path = tmp_path / "wardrive_0.log"
    path.write_text(MARAUDER_CSV)
    assert ingest.classify(path) == "csv"
    assert len(ingest.ingest_paths([path]).sightings) == 2


def test_a_wardrive_without_its_version_line_is_still_recognized(tmp_path):
    path = tmp_path / "export.txt"
    path.write_text("\n".join(MARAUDER_CSV.splitlines()[1:]))
    assert ingest.classify(path) == "csv"


def test_an_ordinary_log_file_is_still_ignored(tmp_path):
    path = tmp_path / "debug.log"
    path.write_text("boot ok\nsd mounted\nradio init\n")
    assert ingest.classify(path) == "unknown"


def test_sd_scan_finds_txt_and_log_wardrives(tmp_path):
    (tmp_path / "apps_data" / "marauder" / "dumps").mkdir(parents=True)
    (tmp_path / "apps_data" / "marauder" / "dumps" / "wardrive_0.txt").write_text(MARAUDER_CSV)
    (tmp_path / "wardrive_0.log").write_text(MARAUDER_CSV)
    (tmp_path / "debug.log").write_text("nothing useful here")
    (tmp_path / "notes.txt").write_text("shopping list")

    found = {p.name for p in sdcard.scan_removable_media(roots=[tmp_path])}
    assert found == {"wardrive_0.txt", "wardrive_0.log"}


# --- Flipper file key names ----------------------------------------------

def test_ibutton_rom_data_with_a_space(tmp_path):
    """Current firmware writes "Rom Data" with a space, not an underscore."""
    path = tmp_path / "key.ibtn"
    path.write_text(
        "Filetype: Flipper iButton key\n"
        "Version: 2\n"
        "Protocol: DS1990\n"
        "Rom Data: 01 A2 B3 C4 D5 E6 F7 88\n"
    )
    record = flipper.parse_flipper_file(path)[0]
    assert record.meta["protocol"] == "DS1990"
    assert record.bssid == "DS1990:01A2B3C4D5E6F788"


def test_ir_library_file_type_also_reads(tmp_path):
    """Bundled remotes say "IR library file" where saved ones say "IR signals
    file". The Filetype line isn't what identifies the format."""
    path = tmp_path / "tv.ir"
    path.write_text(
        "Filetype: IR library file\n"
        "Version: 1\n"
        "#\n"
        "name: Power\n"
        "type: parsed\n"
        "protocol: SIRC\n"
        "address: 01 00 00 00\n"
        "command: 15 00 00 00\n"
    )
    records = flipper.parse_flipper_file(path)
    assert len(records) == 1
    assert records[0].bssid == "SIRC:01000000:15000000"


def test_ir_separator_with_a_trailing_space(tmp_path):
    """The runtime writer emits "# " while the bundled assets use a bare "#".
    Both have to work."""
    path = tmp_path / "tv.ir"
    path.write_text(
        "Filetype: IR signals file\n"
        "Version: 1\n"
        "# \n"
        "name: Power\n"
        "type: parsed\n"
        "protocol: NEC\n"
        "address: 04 00 00 00\n"
        "command: 08 00 00 00\n"
        "# \n"
        "name: Mute\n"
        "type: parsed\n"
        "protocol: NEC\n"
        "address: 04 00 00 00\n"
        "command: 09 00 00 00\n"
    )
    assert len(flipper.parse_flipper_file(path)) == 2


# --- custom-firmware GPS and timestamp tagging ---------------------------

WEATHER_SUB = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok270Async
Lat: 37.774900
Lon: -122.419400
Protocol: Vauno-EN8822C
Id: 64
Bit: 42
Data: 00 00 01 00 02 16 88 1D
Batt: 0
Hum: 81
Ts: 1728836876
Ch: 0
Btn: 255
Temp: 13.300000
"""


def test_momentum_lat_lon_is_read_as_a_real_fix(tmp_path):
    """Momentum and RogueMaster tag saves with GPS. That's a measurement, so
    it must be used directly rather than inferred from a track."""
    path = tmp_path / "weather.sub"
    path.write_text(WEATHER_SUB)
    record = flipper.parse_flipper_file(path)[0]
    assert record.lat == pytest.approx(37.7749)
    assert record.lon == pytest.approx(-122.4194)
    assert record.geo_source == GEO_DIRECT
    assert record.has_location


def test_zero_lat_lon_means_no_gps_module_not_the_gulf_of_guinea(tmp_path):
    """The keys are written whether or not a GPS module is attached."""
    path = tmp_path / "nofix.sub"
    path.write_text(WEATHER_SUB.replace("37.774900", "0.000000")
                    .replace("-122.419400", "0.000000"))
    record = flipper.parse_flipper_file(path)[0]
    assert not record.has_location
    assert record.geo_source == GEO_NONE


def test_xtreme_misspelled_latitude_key_still_reads(tmp_path):
    """Xtreme ships the same feature with `Latitute:` (missing the d)."""
    path = tmp_path / "xtreme.sub"
    path.write_text(
        WEATHER_SUB.replace("Lat: ", "Latitute: ").replace("Lon: ", "Longitude: ")
    )
    record = flipper.parse_flipper_file(path)[0]
    assert record.lat == pytest.approx(37.7749)
    assert record.geo_source == GEO_DIRECT


def test_ts_field_is_used_as_the_capture_time(tmp_path):
    """A `Ts:` value is the Flipper's own RTC reading, which beats both the
    filename and the mtime."""
    path = tmp_path / "weather.sub"
    path.write_text(WEATHER_SUB)
    record = flipper.parse_flipper_file(path)[0]
    expected = datetime.fromtimestamp(1728836876).strftime("%Y-%m-%d %H:%M:%S")
    assert record.first_seen == expected
    assert "Ts field" in record.meta["timestamp_source"]


def test_weather_station_readings_are_kept(tmp_path):
    path = tmp_path / "weather.sub"
    path.write_text(WEATHER_SUB)
    meta = flipper.parse_flipper_file(path)[0].meta
    assert meta["temp"] == "13.300000"
    assert meta["hum"] == "81"


def test_raw_captures_never_claim_a_location(tmp_path):
    """Raw saves go through a different serializer that writes no Lat/Lon."""
    path = tmp_path / "raw.sub"
    path.write_text(
        "Filetype: Flipper SubGhz RAW File\n"
        "Version: 1\n"
        "Frequency: 433920000\n"
        "Preset: FuriHalSubGhzPresetOok650Async\n"
        "Protocol: RAW\n"
        "RAW_Data: 1711 -32700 621 -1600\n"
    )
    record = flipper.parse_flipper_file(path)[0]
    assert record.geo_source == GEO_NONE
    assert record.meta["code_type"] == "raw"


def test_an_embedded_fix_is_not_overwritten_by_a_track(tmp_path):
    from warmap.gps import Track, TrackPoint

    path = tmp_path / "weather.sub"
    path.write_text(WEATHER_SUB)
    records = flipper.parse_flipper_file(path)
    track = Track([
        TrackPoint(lat=10.0, lon=10.0, when=datetime.fromtimestamp(1728836876)),
    ])
    from warmap import gps
    gps.geotag(records, [track])
    assert records[0].lat == pytest.approx(37.7749)


# --- Sub-GHz protocol strings --------------------------------------------

@pytest.mark.parametrize("protocol", [
    "Princeton", "Nice FLO", "CAME", "CAME TWEE", "Holtek_HT12X", "Cham_Code",
    "Linear", "LinearDelta3", "SMC5326", "Ansonic", "Magellan", "Phoenix_V2",
    "Honeywell", "MegaCode", "Marantec", "Hormann HSM",
])
def test_real_fixed_code_protocol_strings(protocol):
    assert radio.code_type(protocol) == RISK_STATIC


@pytest.mark.parametrize("protocol", [
    "KeeLoq", "Security+ 1.0", "Security+ 2.0", "Faac SLH", "Alutech AT-4N",
    "CAME Atomo", "Somfy Telis", "Somfy Keytis", "Star Line", "Scher-Khan",
    "Hormann BiSecur", "Nice One",
])
def test_real_rolling_code_protocol_strings(protocol):
    assert radio.code_type(protocol) == RISK_ROLLING


def test_kinggates_stylo4k_is_rolling_not_fixed():
    """Commonly described as a fixed code, but the firmware registers it as a
    dynamic protocol. Calling it replayable would be a wrong security claim."""
    assert radio.code_type("KingGates Stylo4k") == RISK_ROLLING


@pytest.mark.parametrize("name", ["EV1527", "PT2262", "RcSwitch"])
def test_princeton_aliases_are_not_listed_as_protocols(name):
    """These decode as Princeton. The firmware never writes these strings.
    Listing them would imply warmap recognizes something it won't ever see."""
    assert name.lower() not in radio.STATIC_PROTOCOLS
