"""Rollup numbers for the stats dock, recomputed from whatever the filter
panel currently has visible, never the full unfiltered set. Pure Python, no
Qt, so the whole thing is testable without a display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from warmap import radio
from warmap.models import (
    ENC_OPEN,
    GEO_TRACK,
    TYPE_BLE,
    TYPE_SUBGHZ,
    TYPE_WIFI,
    Sighting,
)

# A carryable radio (Bluetooth, or a known tracker) seen across at least this
# many metres travelled with you: the follower/stalker signal. Kept in sync
# with FOLLOW_SPAN_M in warmap/web/map.js.
FOLLOW_SPAN_M = 150.0

# Anything older than this came from a clock that was never set, not from a
# capture. Same floor as warmap.pcap.MIN_PLAUSIBLE_EPOCH, which catches it one
# layer earlier for pcap files specifically; this one catches it for every
# source, including records already sitting in a store from before that fix.
MIN_PLAUSIBLE_CAPTURE = datetime(2000, 1, 1)
# And the other end: a stamp past this is a corrupt field, not a capture.
MAX_PLAUSIBLE_CAPTURE = datetime(2100, 1, 1)

# Wigle/Marauder's FirstSeen is "YYYY-MM-DD HH:MM:SS"; a couple of other
# shapes are tried too since generic/Kismet exports vary.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S",
)


def parse_first_seen(value: str) -> Optional[datetime]:
    """The moment a record was seen, or None if it doesn't have a usable one.

    "Usable" excludes a timestamp from a device whose clock was never set.
    A board that writes a capture before the GPS has given it the time stamps
    it a few seconds after the Unix epoch, and one such record (a real one,
    off the BFFB card) is enough to report a wardrive as a 57-year capture
    and, if it also had a fix, to put a 1970 point in a 2026 track.
    """
    if not value:
        return None
    text = str(value).strip()
    parsed = None
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        # Wigle sometimes writes an ISO stamp with a timezone or fractional
        # seconds, which none of the fixed formats above match.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo:
            parsed = parsed.replace(tzinfo=None)
    return parsed if MIN_PLAUSIBLE_CAPTURE <= parsed < MAX_PLAUSIBLE_CAPTURE else None


def bbox_area_km2(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> float:
    """Rough rectangular-bounding-box area in km^2 using a haversine-based
    width/height at the box's center latitude, good enough for "how much
    ground did this capture cover", not a real geodesic polygon area."""
    if min_lat == max_lat and min_lon == max_lon:
        return 0.0
    r = 6371.0088  # mean Earth radius, km
    mid_lat = math.radians((min_lat + max_lat) / 2.0)
    height_km = r * math.radians(max_lat - min_lat)
    width_km = r * math.cos(mid_lat) * math.radians(max_lon - min_lon)
    return abs(height_km) * abs(width_km)


@dataclass
class Stats:
    total: int = 0
    unique_ssids: int = 0
    open_count: int = 0
    wifi_count: int = 0
    ble_count: int = 0
    enc_breakdown: dict = field(default_factory=dict)
    channel_breakdown: dict = field(default_factory=dict)
    first_seen_min: Optional[str] = None
    first_seen_max: Optional[str] = None
    bbox: Optional[tuple] = None  # (min_lat, min_lon, max_lat, max_lon)
    area_km2: Optional[float] = None
    # Added alongside the multi-technology support.
    type_breakdown: dict = field(default_factory=dict)
    band_breakdown: dict = field(default_factory=dict)
    code_type_breakdown: dict = field(default_factory=dict)
    top_vendors: list = field(default_factory=list)  # [(vendor, count)]
    tracker_count: int = 0
    tracker_breakdown: dict = field(default_factory=dict)
    randomized_count: int = 0
    located_count: int = 0
    unlocated_count: int = 0
    track_placed_count: int = 0
    hidden_ssid_count: int = 0
    follower_count: int = 0
    subghz_frequencies: dict = field(default_factory=dict)


def compute_stats(sightings: Iterable[Sighting]) -> Stats:
    sightings = list(sightings)
    stats = Stats(total=len(sightings))
    if not sightings:
        return stats

    ssids = set()
    enc_breakdown: dict[str, int] = {}
    channel_breakdown: dict[str, int] = {}
    type_breakdown: dict[str, int] = {}
    band_breakdown: dict[str, int] = {}
    code_breakdown: dict[str, int] = {}
    vendor_counts: dict[str, int] = {}
    tracker_breakdown: dict[str, int] = {}
    freq_counts: dict[str, int] = {}
    lats, lons = [], []
    times: list[datetime] = []

    for s in sightings:
        if s.ssid:
            ssids.add(s.ssid)
        elif s.type == TYPE_WIFI:
            stats.hidden_ssid_count += 1

        if s.enc_bucket == ENC_OPEN:
            stats.open_count += 1
        if s.type == TYPE_WIFI:
            stats.wifi_count += 1
        elif s.type == TYPE_BLE:
            stats.ble_count += 1

        enc_breakdown[s.enc_bucket] = enc_breakdown.get(s.enc_bucket, 0) + 1
        type_breakdown[s.type] = type_breakdown.get(s.type, 0) + 1

        ch_key = str(s.channel) if s.channel is not None else "unknown"
        channel_breakdown[ch_key] = channel_breakdown.get(ch_key, 0) + 1

        band = radio.band_for_record(s.frequency, s.channel, s.type)
        band_breakdown[band] = band_breakdown.get(band, 0) + 1

        if s.vendor:
            vendor_counts[s.vendor] = vendor_counts.get(s.vendor, 0) + 1

        tracker = s.meta.get("tracker")
        if tracker:
            stats.tracker_count += 1
            tracker_breakdown[tracker] = tracker_breakdown.get(tracker, 0) + 1

        # A carryable radio seen across real distance travelled with you.
        span = s.meta.get("span_m")
        if (span is not None and span >= FOLLOW_SPAN_M
                and (s.type in (TYPE_BLE, "BT") or tracker)):
            stats.follower_count += 1

        if str(s.meta.get("address_type", "")).startswith("random"):
            stats.randomized_count += 1

        code = s.meta.get("code_type")
        if code:
            code_breakdown[code] = code_breakdown.get(code, 0) + 1

        if s.type == TYPE_SUBGHZ and s.frequency is not None:
            freq_counts[f"{s.frequency:g}"] = freq_counts.get(f"{s.frequency:g}", 0) + 1

        if s.has_location:
            stats.located_count += 1
            lats.append(s.lat)
            lons.append(s.lon)
        else:
            stats.unlocated_count += 1

        if s.geo_source == GEO_TRACK:
            stats.track_placed_count += 1

        parsed = parse_first_seen(s.first_seen)
        if parsed is not None:
            times.append(parsed)

    stats.unique_ssids = len(ssids)
    stats.enc_breakdown = enc_breakdown
    stats.channel_breakdown = channel_breakdown
    stats.type_breakdown = type_breakdown
    stats.band_breakdown = band_breakdown
    stats.code_type_breakdown = code_breakdown
    stats.tracker_breakdown = tracker_breakdown
    stats.subghz_frequencies = freq_counts
    stats.top_vendors = sorted(vendor_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]

    if lats and lons:
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        stats.bbox = (min_lat, min_lon, max_lat, max_lon)
        stats.area_km2 = bbox_area_km2(min_lat, min_lon, max_lat, max_lon)

    if times:
        stats.first_seen_min = min(times).strftime("%Y-%m-%d %H:%M:%S")
        stats.first_seen_max = max(times).strftime("%Y-%m-%d %H:%M:%S")

    return stats
