"""What a frequency or a protocol name actually means.

Two jobs:

* Wi-Fi channel <-> frequency, so a capture that recorded only one of the two
  can be filtered and grouped by band either way.
* Sub-GHz: which ISM band a capture sits in, what's normally found there, and,
  the useful one, whether the protocol is a fixed code or a rolling code.

That last distinction is the whole point of mapping Sub-GHz captures. A fixed
code is the same every press: capturing it is equivalent to holding the
remote. A rolling code changes per press and a recorded one is generally
spent. Showing those two the same shade of "captured a signal" would be
misleading, so warmap labels them differently and the map colors them
differently.
"""

from __future__ import annotations

from typing import Optional

# --- Wi-Fi ---------------------------------------------------------------

BAND_2G = "2.4 GHz"
BAND_5G = "5 GHz"
BAND_6G = "6 GHz"
BAND_SUBGHZ = "Sub-GHz"
BAND_UNKNOWN = "unknown"


def wifi_channel_to_freq(channel: Optional[int]) -> Optional[float]:
    """Center frequency in MHz for a Wi-Fi channel number.

    Ambiguous by nature: channel 1-14 exist in 2.4 GHz and channels 1-233
    also exist in 6 GHz. Numbers that only make sense in one band resolve
    there; the overlapping low numbers resolve to 2.4 GHz, which is what a
    Marauder capture means by them.
    """
    if channel is None:
        return None
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    if ch == 14:
        return 2484.0
    if 1 <= ch <= 13:
        return 2407.0 + ch * 5.0
    if 36 <= ch <= 177:
        return 5000.0 + ch * 5.0
    return None


def wifi_freq_to_channel(freq_mhz: Optional[float]) -> Optional[int]:
    if freq_mhz is None:
        return None
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return None
    if f == 2484:
        return 14
    if 2412 <= f <= 2472:
        return int(round((f - 2407) / 5))
    if 5160 <= f <= 5885:
        return int(round((f - 5000) / 5))
    if 5955 <= f <= 7115:
        return int(round((f - 5950) / 5))
    return None


def band_for_freq(freq_mhz: Optional[float]) -> str:
    if freq_mhz is None:
        return BAND_UNKNOWN
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return BAND_UNKNOWN
    if f < 1000:
        return BAND_SUBGHZ
    if 2400 <= f <= 2500:
        return BAND_2G
    if 5100 <= f <= 5900:
        return BAND_5G
    if 5925 <= f <= 7125:
        return BAND_6G
    return BAND_UNKNOWN


def band_for_record(freq_mhz: Optional[float], channel: Optional[int], record_type: str) -> str:
    """Band label for a record, preferring an explicit frequency and falling
    back to the channel number."""
    if freq_mhz is not None:
        return band_for_freq(freq_mhz)
    derived = wifi_channel_to_freq(channel)
    if derived is not None:
        return band_for_freq(derived)
    if record_type in ("BLE", "BT"):
        return BAND_2G
    return BAND_UNKNOWN


# --- Sub-GHz bands -------------------------------------------------------
# The three ranges the Flipper's CC1101 is allowed to tune, and what tends to
# live in each.
SUBGHZ_BANDS = (
    (300.0, 348.0, "300-348 MHz", "US garage/gate remotes, alarm sensors, TPMS"),
    (387.0, 464.0, "387-464 MHz", "433.92 MHz ISM: the most common remote band worldwide"),
    (779.0, 928.0, "779-928 MHz", "868 MHz (EU) and 915 MHz (US) ISM"),
)

# Frequencies common enough to be worth naming when one shows up.
NOTABLE_FREQUENCIES = {
    300.00: "US garage door remotes",
    303.87: "US garage/gate remotes (older Chamberlain, Linear)",
    304.25: "US gate remotes",
    310.00: "US garage door remotes",
    315.00: "US remotes, TPMS, alarm sensors",
    318.00: "US garage remotes",
    345.00: "Honeywell/2GIG alarm sensors (US)",
    390.00: "Chamberlain/LiftMaster garage (US)",
    418.00: "UK/EU remotes",
    433.07: "EU ISM edge",
    433.42: "Some car remotes and gates",
    433.92: "The default ISM remote frequency worldwide",
    434.42: "EU remotes",
    434.78: "EU remotes",
    438.90: "EU remotes",
    464.00: "UK/EU",
    779.00: "China ISM",
    868.35: "EU ISM (SRD860): alarms, meters, LoRa",
    915.00: "US ISM (902-928): meters, LoRa, industrial",
    925.00: "Asia-Pacific ISM",
}

# --- Sub-GHz protocol security ------------------------------------------
# Fixed code: transmits the same value every press, so a capture replays.
# Rolling code: the value changes per press from a shared counter, so a
# capture is normally spent once the real remote is used again.
#
# Names are the exact strings the Flipper writes into a .sub file's
# `Protocol:` key, taken from the protocol registry in the firmware source
# (lib/subghz/protocols/) rather than from how the community writes them:
# several differ. Matching is case-insensitive, so only the spelling matters.
#
# Note the names people expect that do NOT exist as Flipper protocols:
# EV1527, PT2262 and RcSwitch all decode as `Princeton`, and Nice Smilo
# decodes as `KeeLoq`. Listing them would be harmless but misleading.
STATIC_PROTOCOLS = frozenset({
    "princeton", "nice flo", "nice flor s", "came", "came twee",
    "holtek", "holtek_ht12x", "cham_code", "linear", "lineardelta3",
    "smc5326", "ansonic", "gate-tx", "gatetx", "doitrand", "nero radio",
    "nero sketch", "bett", "clemsa", "magellan", "mastercode", "phoenix_v2",
    "honeywell", "intertechno_v3", "marantec", "power smart", "hormann hsm",
    "dooya", "airforce", "megacode", "legrand",
})

# KingGates Stylo4k is registered as a dynamic (rolling) protocol in the
# firmware, despite frequently being described as a fixed code.
ROLLING_PROTOCOLS = frozenset({
    "keeloq", "security+ 1.0", "security+ 2.0", "faac slh", "alutech at-4n",
    "came atomo", "somfy telis", "somfy keytis", "star line", "starline",
    "scher-khan", "hormann bisecur", "kinggates stylo4k", "nice one",
})

RISK_STATIC = "static"
RISK_ROLLING = "rolling"
RISK_RAW = "raw"
RISK_UNKNOWN = "unknown"

RISK_LABELS = {
    RISK_STATIC: "Fixed code: the same value every press",
    RISK_ROLLING: "Rolling code: changes per press",
    RISK_RAW: "Raw capture: no protocol decoded",
    RISK_UNKNOWN: "Unrecognized protocol",
}

# Map marker colors for Sub-GHz, by code type rather than by band: the code
# type is the thing worth seeing at a glance.
RISK_COLORS = {
    RISK_STATIC: "#d03b3b",
    RISK_ROLLING: "#3f9142",
    RISK_RAW: "#8a8a8a",
    RISK_UNKNOWN: "#8a8a8a",
}


def code_type(protocol: Optional[str]) -> str:
    if not protocol:
        return RISK_UNKNOWN
    p = str(protocol).strip().lower()
    if p == "raw":
        return RISK_RAW
    if p in STATIC_PROTOCOLS:
        return RISK_STATIC
    if p in ROLLING_PROTOCOLS:
        return RISK_ROLLING
    # KeeLoq shows up under a lot of manufacturer-specific names.
    if "keeloq" in p or "security+" in p or "bisecur" in p:
        return RISK_ROLLING
    return RISK_UNKNOWN


def subghz_band(freq_mhz: Optional[float]) -> Optional[tuple]:
    """(label, description) for the Sub-GHz band a frequency falls in."""
    if freq_mhz is None:
        return None
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return None
    for low, high, label, desc in SUBGHZ_BANDS:
        if low <= f <= high:
            return (label, desc)
    return None


def notable_frequency(freq_mhz: Optional[float], tolerance: float = 0.02) -> Optional[str]:
    """What a frequency is commonly used for, if it's close to a known one."""
    if freq_mhz is None:
        return None
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return None
    for known, desc in NOTABLE_FREQUENCIES.items():
        if abs(f - known) <= tolerance:
            return desc
    return None


def describe_subghz(freq_mhz: Optional[float], protocol: Optional[str]) -> dict:
    """Flat fields ready to merge into a Sighting's `meta`."""
    out: dict = {}
    risk = code_type(protocol)
    out["code_type"] = risk
    out["code_type_label"] = RISK_LABELS[risk]

    band = subghz_band(freq_mhz)
    if band:
        out["band"] = band[0]
        out["band_note"] = band[1]

    notable = notable_frequency(freq_mhz)
    if notable:
        out["frequency_note"] = notable

    return out


# --- preset decoding -----------------------------------------------------
# The Flipper writes a `Preset:` key naming the CC1101 radio configuration it
# used. Translating it tells you the modulation, which matters if you ever
# want to reproduce the capture with other hardware.
PRESETS = {
    "FuriHalSubGhzPresetOok270Async": "OOK, 270 kHz bandwidth",
    "FuriHalSubGhzPresetOok650Async": "OOK, 650 kHz bandwidth",
    "FuriHalSubGhzPreset2FSKDev238Async": "2-FSK, 2.38 kHz deviation",
    "FuriHalSubGhzPreset2FSKDev476Async": "2-FSK, 47.6 kHz deviation",
    "FuriHalSubGhzPresetMSK99_97KbAsync": "MSK, 99.97 kbps",
    "FuriHalSubGhzPresetGFSK9_99KbAsync": "GFSK, 9.99 kbps",
    "FuriHalSubGhzPresetCustom": "Custom register set stored in the file",
}


def preset_label(preset: Optional[str]) -> Optional[str]:
    if not preset:
        return None
    return PRESETS.get(str(preset).strip(), str(preset).strip())
