"""The one record shape everything else in warmap works with.

A single `Sighting` does double duty: as parsed straight off a capture it's
one *observation* (`times_seen` is always 1); after `warmap.parse.dedup`
it's a *station/key* (one row per identity, `times_seen` is how many raw
observations collapsed into it, `rssi` is the strongest of them). Keeping
one shape for both means dedup and store-merge are the same function, see
parse.py.

The same shape also covers everything a Flipper Zero can save, not just
Wi-Fi: a Sub-GHz key, an NFC card, a 125 kHz fob, an iButton, an IR remote.
Those have no MAC and no coordinates of their own, so `bssid` holds
whatever identity the technology does have (a UID, a rolling-code key) and
the location is filled in later by correlating the file's timestamp against
a GPS track (see gps.py). `geo_source` records which of those two happened,
because "the GPS said so" and "I inferred it from a track" are not the same
claim and the UI should never present them as if they were.

`AccessPoint` stays as an alias: it was the original name, and it's still
the right word for the Wi-Fi case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --- encryption buckets (Wi-Fi) -----------------------------------------
# In the order the filter panel and legend show them.
ENC_OPEN = "Open"
ENC_WEP = "WEP"
ENC_WPA = "WPA"
ENC_WPA2 = "WPA2"
ENC_WPA3 = "WPA3"
ENC_WPA23_MIXED = "WPA2/3-mixed"
ENC_UNKNOWN = "Unknown"

ENC_BUCKETS = (ENC_OPEN, ENC_WEP, ENC_WPA, ENC_WPA2, ENC_WPA3, ENC_WPA23_MIXED, ENC_UNKNOWN)

# Marker/cluster/legend color per bucket, shared source of truth for the
# Qt filter panel's swatches and the JS in web/map.js
# (tests/test_web_assets.py cross-checks the two). Open = red (the thing you
# actually care about spotting), WEP = orange (weak), any WPA flavor =
# green (fine), Unknown = gray (couldn't tell).
ENC_COLORS = {
    ENC_OPEN: "#d03b3b",
    ENC_WEP: "#e07b1a",
    ENC_WPA: "#3f9142",
    ENC_WPA2: "#2f8f3f",
    ENC_WPA3: "#1f8f5a",
    ENC_WPA23_MIXED: "#2f8f6f",
    ENC_UNKNOWN: "#8a8a8a",
}

# --- record types --------------------------------------------------------
# The first four match Wigle's CSV `Type` column. The rest are warmap's own,
# one per thing a Flipper Zero saves to its SD card.
TYPE_WIFI = "WIFI"
# A station rather than an access point: something that was looking for a
# network, not offering one. Only a packet capture can see these.
TYPE_CLIENT = "CLIENT"
TYPE_BLE = "BLE"
TYPE_BT = "BT"
TYPE_CELL = "CELL"
TYPE_SUBGHZ = "SUBGHZ"
TYPE_NFC = "NFC"
TYPE_RFID = "RFID"
TYPE_IBUTTON = "IBUTTON"
TYPE_IR = "IR"

ALL_TYPES = (
    TYPE_WIFI,
    TYPE_CLIENT,
    TYPE_BLE,
    TYPE_BT,
    TYPE_CELL,
    TYPE_SUBGHZ,
    TYPE_NFC,
    TYPE_RFID,
    TYPE_IBUTTON,
    TYPE_IR,
)

# Types that come off a radio with a MAC-ish address, an RSSI and a channel,
# the things a wardrive CSV or a pcap produces. Everything else is a
# saved artifact with no signal strength of its own.
RADIO_TYPES = (TYPE_WIFI, TYPE_CLIENT, TYPE_BLE, TYPE_BT, TYPE_CELL)

# Types that come off the Flipper's own SD card as saved files.
FLIPPER_TYPES = (TYPE_SUBGHZ, TYPE_NFC, TYPE_RFID, TYPE_IBUTTON, TYPE_IR)

TYPE_LABELS = {
    TYPE_WIFI: "Wi-Fi",
    TYPE_CLIENT: "Wi-Fi client",
    TYPE_BLE: "Bluetooth LE",
    TYPE_BT: "Bluetooth Classic",
    TYPE_CELL: "Cell",
    TYPE_SUBGHZ: "Sub-GHz",
    TYPE_NFC: "NFC",
    TYPE_RFID: "RFID 125 kHz",
    TYPE_IBUTTON: "iButton",
    TYPE_IR: "Infrared",
}

# Wi-Fi markers are colored by encryption (ENC_COLORS) because that's the
# interesting axis there. Everything else has no encryption bucket worth
# showing, so it's colored by type instead.
TYPE_COLORS = {
    TYPE_WIFI: "#2f8f3f",
    TYPE_CLIENT: "#c9a227",
    TYPE_BLE: "#3b7fd0",
    TYPE_BT: "#5b5bd0",
    TYPE_CELL: "#9b5bd0",
    TYPE_SUBGHZ: "#d08b1a",
    TYPE_NFC: "#d03b8b",
    TYPE_RFID: "#c0506b",
    TYPE_IBUTTON: "#8b6b3b",
    TYPE_IR: "#d0503b",
}

# --- ALPR / surveillance overlay -----------------------------------------
# ALPR (Flock and other automatic license-plate readers) is a reference
# overlay from DeFlock/OpenStreetMap, not a record type warmap collects; see
# warmap/alpr.py for why it's kept out of the Sighting pipeline entirely. It's
# deliberately NOT in ALL_TYPES/TYPE_COLORS/TYPE_LABELS so nothing iterating
# the capture types ever picks it up. These two constants are the shared
# source of truth for its color and name; web/map.js mirrors ALPR_COLOR and
# tests/test_web_assets.py cross-checks the two the same way it does the rest.
ALPR_LABEL = "ALPR camera"
ALPR_COLOR = "#7b2fb5"  # a violet no capture type uses, so the overlay reads
                        # as a different kind of thing at a glance

# One character drawn inside the marker for the low-count Flipper types, so
# they're distinguishable at a glance without reading the popup. The radio
# types stay as plain dots: there are thousands of them and a glyph per
# marker is both unreadable and slow.
TYPE_GLYPHS = {
    TYPE_SUBGHZ: "≈",  # almost-equal-to, i.e. a radio wave
    TYPE_NFC: "N",
    TYPE_RFID: "R",
    TYPE_IBUTTON: "i",
    TYPE_IR: "IR",
}

# --- how a record got its coordinates ------------------------------------
# Wardrive rows carry their own GPS fix. Flipper files do not, so warmap
# infers a location by matching the file's timestamp to a loaded GPS track.
# That inference is weaker evidence and is labeled as such everywhere it's
# shown, never silently blended with a real fix.
GEO_DIRECT = "direct"
GEO_TRACK = "track"
GEO_NONE = "none"

GEO_LABELS = {
    GEO_DIRECT: "GPS fix in the capture",
    GEO_TRACK: "inferred from GPS track by timestamp",
    GEO_NONE: "no location",
}


def _opt_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _opt_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class Sighting:
    # Identity. For radio types this is the MAC/BSSID. For Flipper types
    # it's whatever that technology uses as a key (an NFC UID, a Sub-GHz
    # protocol+key pair, an IR protocol+address+command).
    bssid: str
    # Human name. SSID for Wi-Fi, advertised device name for BLE, the saved
    # file's name for Flipper types.
    ssid: str
    auth_mode: str
    enc_bucket: str
    first_seen: str
    channel: Optional[int]
    rssi: int
    lat: Optional[float]
    lon: Optional[float]
    altitude: Optional[float]
    accuracy: Optional[float]
    type: str
    times_seen: int = 1
    # Megahertz. Wigle 1.6 carries this for Wi-Fi/BLE; for Sub-GHz it's the
    # capture frequency, which is the single most useful field there.
    frequency: Optional[float] = None
    # Resolved from the OUI or the BLE company ID where one is known.
    vendor: str = ""
    # The file this came from, so a record can always be traced back.
    source: str = ""
    geo_source: str = GEO_DIRECT
    # Everything type-specific: BLE service UUIDs and manufacturer data,
    # Sub-GHz protocol/preset/key, NFC ATQA/SAK, IR address/command, and so
    # on. Kept as a free-form dict rather than 40 mostly-null columns.
    meta: dict = field(default_factory=dict)

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def ident(self) -> str:
        """Dedup key. Scoped by type so an NFC UID that happens to look like
        a MAC can never merge with a Wi-Fi AP."""
        return f"{self.type}|{self.bssid}"

    @property
    def display_name(self) -> str:
        if self.ssid:
            return self.ssid
        if self.type == TYPE_WIFI:
            return "(hidden)"
        return self.bssid or "(unnamed)"

    def to_dict(self) -> dict:
        return {
            "bssid": self.bssid,
            "ssid": self.ssid,
            "auth_mode": self.auth_mode,
            "enc_bucket": self.enc_bucket,
            "first_seen": self.first_seen,
            "channel": self.channel,
            "rssi": self.rssi,
            "lat": self.lat,
            "lon": self.lon,
            "altitude": self.altitude,
            "accuracy": self.accuracy,
            "type": self.type,
            "times_seen": self.times_seen,
            "frequency": self.frequency,
            "vendor": self.vendor,
            "source": self.source,
            "geo_source": self.geo_source,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Sighting":
        meta = d.get("meta")
        return cls(
            bssid=str(d.get("bssid", "")),
            ssid=str(d.get("ssid", "")),
            auth_mode=str(d.get("auth_mode", "")),
            enc_bucket=str(d.get("enc_bucket", ENC_UNKNOWN)),
            first_seen=str(d.get("first_seen", "")),
            channel=_opt_int(d.get("channel")),
            rssi=int(d.get("rssi", 0) or 0),
            lat=_opt_float(d.get("lat")),
            lon=_opt_float(d.get("lon")),
            altitude=_opt_float(d.get("altitude")),
            accuracy=_opt_float(d.get("accuracy")),
            type=str(d.get("type", TYPE_WIFI)),
            times_seen=int(d.get("times_seen", 1) or 1),
            frequency=_opt_float(d.get("frequency")),
            vendor=str(d.get("vendor", "") or ""),
            source=str(d.get("source", "") or ""),
            geo_source=str(d.get("geo_source", GEO_DIRECT) or GEO_DIRECT),
            meta=dict(meta) if isinstance(meta, dict) else {},
        )


# The original name. Still accurate for the Wi-Fi case, and every existing
# caller and test uses it.
AccessPoint = Sighting
