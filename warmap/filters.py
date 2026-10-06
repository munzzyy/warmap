"""Filter state and application, pure Python, no Qt. The filter panel builds a
FilterState from whatever the user's got set and hands it to apply_filters;
the main window recomputes the visible set and pushes it to the map, the
stats panel and the records table. Nothing here knows about widgets.

Every `Optional[frozenset]` field follows the same convention: None means "no
opinion, show everything", an empty set means "the user unticked all of them,
show nothing". Those are genuinely different and collapsing them makes the
panel feel broken when you untick the last box and everything reappears.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from warmap import radio
from warmap.models import ALL_TYPES, ENC_BUCKETS, ENC_OPEN, Sighting
from warmap.stats import parse_first_seen


@dataclass
class FilterState:
    text: str = ""
    enc_buckets: frozenset = field(default_factory=lambda: frozenset(ENC_BUCKETS))
    min_rssi: int = -100
    channels: Optional[frozenset] = None  # None = every channel
    types: frozenset = field(default_factory=lambda: frozenset(ALL_TYPES))
    open_only: bool = False
    bands: Optional[frozenset] = None  # None = every band
    trackers_only: bool = False
    randomized_only: bool = False
    located_only: bool = False
    code_types: Optional[frozenset] = None  # Sub-GHz: static / rolling / raw
    min_times_seen: int = 1
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None
    # The ALPR/Flock camera overlay is reference data, not a Sighting, so it
    # doesn't pass through apply_filters at all. This flag just carries the
    # panel's show/hide choice back to the main window, which toggles the map
    # layer's visibility. Kept here so the filter panel has one state object.
    show_alpr: bool = True


def _matches_text(sighting: Sighting, text: str) -> bool:
    """Search covers the name, the address and the vendor, plus the free-form
    meta values, so "airtag", "keeloq", "433.92" and "Espressif" all find
    what you'd expect without needing a field-specific search box."""
    if text in sighting.ssid.lower():
        return True
    if text in sighting.bssid.lower():
        return True
    if sighting.vendor and text in sighting.vendor.lower():
        return True
    for value in sighting.meta.values():
        if isinstance(value, str):
            if text in value.lower():
                return True
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and text in item.lower():
                    return True
    return False


def apply_filters(sightings: Iterable[Sighting], state: FilterState) -> list[Sighting]:
    text = state.text.strip().lower()
    out = []

    for s in sightings:
        if state.open_only:
            if s.enc_bucket != ENC_OPEN:
                continue
        elif s.enc_bucket not in state.enc_buckets:
            continue

        if s.type not in state.types:
            continue
        if s.rssi < state.min_rssi:
            continue
        if state.channels is not None and s.channel not in state.channels:
            continue
        if state.min_times_seen > 1 and s.times_seen < state.min_times_seen:
            continue
        if state.located_only and not s.has_location:
            continue

        if state.bands is not None:
            band = radio.band_for_record(s.frequency, s.channel, s.type)
            if band not in state.bands:
                continue

        if state.code_types is not None:
            code = s.meta.get("code_type")
            # Only Sub-GHz records carry a code type; a code-type filter is
            # about Sub-GHz and shouldn't silently hide every Wi-Fi row.
            if code is not None and code not in state.code_types:
                continue

        if state.trackers_only and not s.meta.get("tracker"):
            continue

        if state.randomized_only:
            addr = s.meta.get("address_type", "")
            if not str(addr).startswith("random"):
                continue

        if state.time_from is not None or state.time_to is not None:
            when = parse_first_seen(s.first_seen)
            if when is None:
                continue
            if state.time_from is not None and when < state.time_from:
                continue
            if state.time_to is not None and when > state.time_to:
                continue

        if text and not _matches_text(s, text):
            continue

        out.append(s)

    return out
