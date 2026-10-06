"""Frequency, channel and band arithmetic, plus the Sub-GHz fixed-vs-rolling
code classification.
"""

from __future__ import annotations

import pytest

from warmap import radio
from warmap.models import TYPE_BLE, TYPE_SUBGHZ, TYPE_WIFI


# --- Wi-Fi channels ------------------------------------------------------

@pytest.mark.parametrize("channel,freq", [
    (1, 2412.0), (6, 2437.0), (11, 2462.0), (13, 2472.0), (14, 2484.0),
    (36, 5180.0), (149, 5745.0),
])
def test_channel_to_frequency(channel, freq):
    assert radio.wifi_channel_to_freq(channel) == freq


@pytest.mark.parametrize("freq,channel", [
    (2412, 1), (2437, 6), (2462, 11), (2484, 14), (5180, 36), (5745, 149),
])
def test_frequency_to_channel(freq, channel):
    assert radio.wifi_freq_to_channel(freq) == channel


def test_channel_14_is_special_cased():
    """Channel 14 breaks the 5 MHz spacing pattern. It's at 2484, not 2472."""
    assert radio.wifi_channel_to_freq(14) == 2484.0
    assert radio.wifi_freq_to_channel(2484) == 14


def test_six_gigahertz_frequencies_map_back():
    assert radio.wifi_freq_to_channel(5955) == 1
    assert radio.wifi_freq_to_channel(6175) == 45


def test_channel_conversion_on_junk():
    assert radio.wifi_channel_to_freq(None) is None
    assert radio.wifi_channel_to_freq("abc") is None
    assert radio.wifi_channel_to_freq(9999) is None
    assert radio.wifi_freq_to_channel(None) is None
    assert radio.wifi_freq_to_channel(1) is None


# --- bands ---------------------------------------------------------------

def test_band_for_frequency():
    assert radio.band_for_freq(2437) == radio.BAND_2G
    assert radio.band_for_freq(5180) == radio.BAND_5G
    assert radio.band_for_freq(6175) == radio.BAND_6G
    assert radio.band_for_freq(433.92) == radio.BAND_SUBGHZ
    assert radio.band_for_freq(None) == radio.BAND_UNKNOWN


def test_band_for_record_prefers_frequency():
    assert radio.band_for_record(5180, 36, TYPE_WIFI) == radio.BAND_5G


def test_band_for_record_falls_back_to_channel():
    assert radio.band_for_record(None, 6, TYPE_WIFI) == radio.BAND_2G


def test_band_for_record_assumes_2g_for_bluetooth():
    """Bluetooth is always 2.4 GHz, and a CSV row for it carries neither a
    channel nor a frequency."""
    assert radio.band_for_record(None, None, TYPE_BLE) == radio.BAND_2G


def test_band_for_record_unknown_when_nothing_to_go_on():
    assert radio.band_for_record(None, None, TYPE_SUBGHZ) == radio.BAND_UNKNOWN


# --- Sub-GHz code type ---------------------------------------------------

# These are the strings the firmware actually writes, which differ from the
# names the community uses for several of them: "Cham_Code" rather than
# "Chamberlain", for one. tests/test_format_fidelity.py covers the full set
# against the firmware's protocol registry.
@pytest.mark.parametrize("protocol", [
    "Princeton", "CAME", "Nice FLO", "Holtek_HT12X", "Linear", "SMC5326",
    "Cham_Code", "Ansonic", "Magellan",
])
def test_fixed_code_protocols(protocol):
    assert radio.code_type(protocol) == radio.RISK_STATIC


@pytest.mark.parametrize("protocol", [
    "KeeLoq", "Security+ 1.0", "Security+ 2.0", "Faac SLH", "Alutech AT-4N",
    "CAME Atomo", "Somfy Telis", "Star Line", "Hormann BiSecur",
])
def test_rolling_code_protocols(protocol):
    assert radio.code_type(protocol) == radio.RISK_ROLLING


def test_protocol_matching_is_case_insensitive():
    assert radio.code_type("princeton") == radio.RISK_STATIC
    assert radio.code_type("KEELOQ") == radio.RISK_ROLLING


def test_keeloq_manufacturer_variants_are_still_rolling():
    """The firmware writes manufacturer-specific KeeLoq names; guessing
    "unknown" for those would understate them."""
    assert radio.code_type("KeeLoq Beninca") == radio.RISK_ROLLING
    assert radio.code_type("Centurion KeeLoq") == radio.RISK_ROLLING


def test_raw_is_its_own_category():
    assert radio.code_type("RAW") == radio.RISK_RAW


def test_unrecognized_protocol_is_unknown_not_assumed_safe():
    assert radio.code_type("Some New Protocol") == radio.RISK_UNKNOWN
    assert radio.code_type(None) == radio.RISK_UNKNOWN
    assert radio.code_type("") == radio.RISK_UNKNOWN


def test_no_protocol_is_in_both_lists():
    overlap = radio.STATIC_PROTOCOLS & radio.ROLLING_PROTOCOLS
    assert overlap == frozenset()


# --- Sub-GHz bands and frequencies ---------------------------------------

def test_subghz_band_lookup():
    assert radio.subghz_band(433.92)[0] == "387-464 MHz"
    assert radio.subghz_band(315.0)[0] == "300-348 MHz"
    assert radio.subghz_band(868.35)[0] == "779-928 MHz"


def test_subghz_band_outside_any_range():
    assert radio.subghz_band(500.0) is None
    assert radio.subghz_band(None) is None


def test_notable_frequency_lookup():
    assert "ISM" in radio.notable_frequency(433.92)
    assert radio.notable_frequency(433.9201) is not None  # within tolerance
    assert radio.notable_frequency(500.0) is None


def test_describe_subghz_bundles_everything():
    out = radio.describe_subghz(433.92, "Princeton")
    assert out["code_type"] == radio.RISK_STATIC
    assert "Fixed code" in out["code_type_label"]
    assert out["band"] == "387-464 MHz"
    assert out["frequency_note"]


def test_describe_subghz_with_nothing_known():
    out = radio.describe_subghz(None, None)
    assert out["code_type"] == radio.RISK_UNKNOWN
    assert "band" not in out


# --- presets -------------------------------------------------------------

def test_preset_label_decodes_known_presets():
    assert "OOK" in radio.preset_label("FuriHalSubGhzPresetOok650Async")
    assert "2-FSK" in radio.preset_label("FuriHalSubGhzPreset2FSKDev476Async")


def test_preset_label_passes_unknown_through():
    assert radio.preset_label("SomethingNew") == "SomethingNew"
    assert radio.preset_label(None) is None
