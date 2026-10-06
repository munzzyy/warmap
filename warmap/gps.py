"""GPS tracks: reading them, drawing them, and using them to place captures
that have no coordinates of their own.

A Marauder wardrive row arrives with a fix already attached. Nothing a Flipper
saves does: a `.sub` or a `.nfc` knows what it caught but not where. The fix
for that is a track log running alongside the session: match each file's
timestamp to where the track says you were at that moment.

Two details make or break this:

**Timezones.** NMEA timestamps are UTC, and so is a Marauder wardrive row's
FirstSeen column, straight off the same GPS module's clock, unconverted
(confirmed against the ESP32Marauder firmware itself: `dt_string_from_gps()`
echoes the NMEA library's hour/minute/second fields verbatim). File mtimes
are local. Correlating any of these naively puts every capture hours away
from where it happened. So every track point, whether read from a real
track file or rebuilt from a wardrive's own fixes, is converted to *local*
time at parse time, using the system zone rules for that particular date
(which gets DST right, unlike a fixed offset). `clock_offset_seconds` on top
of that handles the other common case: a Flipper whose RTC was never set
correctly.

**Honesty about interpolation.** A location derived this way is an inference,
not a measurement. `geotag` records how many seconds away the nearest track
point was in `meta["geo_match_seconds"]`, refuses anything outside
`max_gap_seconds`, and marks the record `GEO_TRACK` so the map and the popup
can say where the coordinate came from.
"""

from __future__ import annotations

import bisect
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from warmap.models import GEO_DIRECT, GEO_NONE, GEO_TRACK, RADIO_TYPES, Sighting
from warmap.stats import parse_first_seen

TRACK_EXTENSIONS = {".nmea", ".gpx", ".log", ".txt"}

# How far from a track point a capture may sit and still be placed, by
# default. Five minutes of walking is a couple of hundred meters, beyond
# that the guess stops being worth making.
DEFAULT_MAX_GAP_SECONDS = 300

# A long NMEA log is a few tens of megabytes at worst. Capped for the same
# reason as flipper.MAX_FILE_BYTES: the file is read whole, the decode
# amplifies it several times over, and a symlink is enough to aim this at
# something enormous.
MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass
class TrackPoint:
    lat: float
    lon: float
    when: Optional[datetime] = None  # naive local time
    altitude: Optional[float] = None
    speed_kmh: Optional[float] = None
    satellites: Optional[int] = None
    hdop: Optional[float] = None


def _nmea_checksum_ok(sentence: str) -> bool:
    """Validate the `*XX` trailer. A GPS log written while the receiver was
    losing fix is full of truncated lines, and a corrupt one that happens to
    parse produces a wild coordinate that wrecks the map bounds."""
    if "*" not in sentence:
        return False
    body, _, checksum = sentence.rpartition("*")
    body = body.lstrip("$")
    checksum = checksum.strip()[:2]
    if len(checksum) != 2:
        return False
    try:
        expected = int(checksum, 16)
    except ValueError:
        return False
    actual = 0
    for char in body:
        actual ^= ord(char)
    return actual == expected


def _valid_fix(lat: float, lon: float) -> bool:
    """Reject anything that isn't a plausible point on Earth: non-finite
    (nan/inf that slipped past a `float()` call, since Python parses both from
    text happily, and a plain `> 90` range check never catches nan, since
    every comparison against nan is False, including the one meant to reject
    it), outside the physical coordinate range, or the 0,0 sentinel a GPS
    logger writes before it has an actual fix."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)


def _nmea_coord(value: str, hemisphere: str) -> Optional[float]:
    """NMEA packs coordinates as ddmm.mmmm (latitude) or dddmm.mmmm
    (longitude): degrees and minutes run together with no separator, so the
    split point depends on which one it is. Sign comes from the hemisphere
    letter, not from the number."""
    if not value or not hemisphere:
        return None
    try:
        dot = value.index(".")
    except ValueError:
        return None
    # Minutes are always the two digits before the decimal point.
    deg_digits = dot - 2
    if deg_digits < 1:
        return None
    try:
        degrees = float(value[:deg_digits])
        minutes = float(value[deg_digits:])
    except ValueError:
        return None
    # A field that happens to spell out "nan" or "inf" parses as a float
    # without raising, and it has to be caught here rather than by the range
    # checks below: every comparison against nan is False, so `abs(nan) > 90`
    # never rejects it the way a genuinely huge number would.
    if not (math.isfinite(degrees) and math.isfinite(minutes)):
        return None
    if minutes >= 60.0:
        return None
    result = degrees + minutes / 60.0
    if hemisphere.upper() in ("S", "W"):
        result = -result
    if hemisphere.upper() in ("N", "S") and abs(result) > 90.0:
        return None
    if abs(result) > 180.0:
        return None
    return result


def _utc_to_local_naive(dt_utc: datetime) -> datetime:
    """UTC -> the machine's local wall-clock time for that date, as a naive
    datetime. Uses the zone rules for the date in question so a track
    recorded on the other side of a DST change still lines up."""
    try:
        return dt_utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return dt_utc


def parse_nmea(path: Path) -> list[TrackPoint]:
    """Track points from a raw NMEA log, the format the Flipper's GPS app
    and almost every standalone receiver writes.

    RMC carries date and time; GGA carries time, altitude and fix quality but
    no date. So the date is carried forward from the most recent RMC, with a
    rollover bump when the clock wraps past midnight.
    """
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, MemoryError):
        return []

    points: list[TrackPoint] = []
    current_date = None
    last_tod = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("$") or not _nmea_checksum_ok(line):
            continue
        # rpartition, matching _nmea_checksum_ok's own split: a data field
        # never legitimately contains "*", but the two should agree on which
        # "*" ends the sentence rather than one taking the first and the
        # other the last.
        body = line.rpartition("*")[0]
        fields = body.split(",")
        if len(fields) < 2:
            continue
        # Talker ID varies (GP/GN/GL/GA/GB): only the last three characters
        # identify the sentence.
        kind = fields[0][-3:].upper()

        if kind == "RMC" and len(fields) >= 10:
            if fields[2].upper() != "A":
                continue  # V = navigation receiver warning, i.e. no fix
            lat = _nmea_coord(fields[3], fields[4])
            lon = _nmea_coord(fields[5], fields[6])
            if lat is None or lon is None or not _valid_fix(lat, lon):
                continue
            when = None
            if fields[1] and fields[9]:
                try:
                    stamp = datetime.strptime(
                        fields[9] + fields[1].split(".")[0], "%d%m%y%H%M%S"
                    )
                    current_date = stamp.date()
                    last_tod = stamp.time()
                    when = _utc_to_local_naive(stamp)
                except ValueError:
                    when = None
            speed = None
            try:
                if fields[7]:
                    speed = float(fields[7]) * 1.852  # knots -> km/h
            except ValueError:
                speed = None
            points.append(TrackPoint(lat=lat, lon=lon, when=when, speed_kmh=speed))

        elif kind == "GGA" and len(fields) >= 10:
            try:
                if int(fields[6] or 0) == 0:
                    continue  # fix quality 0 = no fix
            except ValueError:
                continue
            lat = _nmea_coord(fields[2], fields[3])
            lon = _nmea_coord(fields[4], fields[5])
            if lat is None or lon is None or not _valid_fix(lat, lon):
                continue
            when = None
            if fields[1] and current_date is not None:
                try:
                    tod = datetime.strptime(fields[1].split(".")[0], "%H%M%S").time()
                    if last_tod is not None and tod < last_tod and (
                        last_tod.hour - tod.hour
                    ) > 12:
                        current_date = current_date + timedelta(days=1)
                    last_tod = tod
                    when = _utc_to_local_naive(datetime.combine(current_date, tod))
                except ValueError:
                    when = None
            altitude = None
            sats = None
            hdop = None
            try:
                altitude = float(fields[9]) if fields[9] else None
            except ValueError:
                pass
            try:
                sats = int(fields[7]) if fields[7] else None
            except ValueError:
                pass
            try:
                hdop = float(fields[8]) if fields[8] else None
            except ValueError:
                pass
            points.append(TrackPoint(lat=lat, lon=lon, when=when, altitude=altitude,
                                     satellites=sats, hdop=hdop))

    return points


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_gpx(path: Path) -> list[TrackPoint]:
    """Track and waypoint entries from a GPX file. Namespace-agnostic: GPX
    1.0 and 1.1 use different namespace URIs and half the exporters in the
    world get them wrong anyway, so tags are matched on local name."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        tree = ET.parse(path)
    except (OSError, ET.ParseError, MemoryError):
        return []

    points: list[TrackPoint] = []
    for element in tree.getroot().iter():
        if _localname(element.tag) not in ("trkpt", "wpt", "rtept"):
            continue
        try:
            lat = float(element.attrib["lat"])
            lon = float(element.attrib["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        # Unlike the NMEA path, `float()` is the only parsing this value ever
        # gets, so nothing here would otherwise catch "nan"/"inf" (both parse
        # without raising), a lat/lon outside the physical range, or the 0,0
        # a logger app can write before it has its first real fix.
        if not _valid_fix(lat, lon):
            continue
        when = None
        altitude = None
        for child in element:
            name = _localname(child.tag)
            if name == "time" and child.text:
                when = _parse_iso_utc(child.text.strip())
            elif name == "ele" and child.text:
                try:
                    altitude = float(child.text.strip())
                except ValueError:
                    altitude = None
        points.append(TrackPoint(lat=lat, lon=lon, when=when, altitude=altitude))
    return points


def _parse_iso_utc(value: str) -> Optional[datetime]:
    """GPX times are ISO 8601 UTC ('2024-01-15T14:30:22Z'). Returns local
    naive time to match everything else in this module."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    try:
        return parsed.astimezone().replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def parse_track_file(path: Path) -> list[TrackPoint]:
    """Read a track from whichever of the supported formats this is."""
    path = Path(path)
    if path.suffix.lower() == ".gpx":
        return parse_gpx(path)
    return parse_nmea(path)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, max(0.0, a))))


class Track:
    """A time-ordered set of track points you can ask "where was I at X"."""

    def __init__(self, points: Iterable[TrackPoint], source: str = "", derived: bool = False):
        self.source = source
        # True when the track wasn't logged as a track at all, but rebuilt
        # from the GPS fixes already attached to a wardrive's rows. The points
        # are genuine measurements either way; what differs is spacing, since
        # a wardrive only fixes a position when it sees something.
        self.derived = derived
        self.points = list(points)
        # Only points with a time are usable for correlation; the rest still
        # count for drawing the line and measuring distance.
        self._timed = sorted(
            (p for p in self.points if p.when is not None), key=lambda p: p.when
        )
        self._times = [p.when for p in self._timed]

    def __len__(self) -> int:
        return len(self.points)

    @property
    def timed_count(self) -> int:
        return len(self._timed)

    @property
    def start(self) -> Optional[datetime]:
        return self._times[0] if self._times else None

    @property
    def end(self) -> Optional[datetime]:
        return self._times[-1] if self._times else None

    def duration(self) -> Optional[timedelta]:
        if len(self._times) < 2:
            return None
        return self._times[-1] - self._times[0]

    def distance_km(self) -> float:
        total = 0.0
        for a, b in zip(self.points, self.points[1:]):
            total += haversine_km(a.lat, a.lon, b.lat, b.lon)
        return total

    def bounds(self) -> Optional[tuple]:
        if not self.points:
            return None
        lats = [p.lat for p in self.points]
        lons = [p.lon for p in self.points]
        return (min(lats), min(lons), max(lats), max(lons))

    def locate(self, when: datetime, max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS):
        """Where the track says you were at `when`.

        Returns `(lat, lon, altitude, gap_seconds)` or None. Between two
        points the position is interpolated; outside the track's time range
        the nearest endpoint is used, but only within `max_gap_seconds`.
        """
        if not self._timed:
            return None

        index = bisect.bisect_left(self._times, when)

        if index == 0:
            first = self._timed[0]
            gap = abs((first.when - when).total_seconds())
            if gap > max_gap_seconds:
                return None
            return (first.lat, first.lon, first.altitude, gap)

        if index >= len(self._timed):
            last = self._timed[-1]
            gap = abs((when - last.when).total_seconds())
            if gap > max_gap_seconds:
                return None
            return (last.lat, last.lon, last.altitude, gap)

        before = self._timed[index - 1]
        after = self._timed[index]
        span = (after.when - before.when).total_seconds()
        gap = min(
            abs((when - before.when).total_seconds()),
            abs((after.when - when).total_seconds()),
        )
        if gap > max_gap_seconds:
            return None
        if span <= 0:
            return (before.lat, before.lon, before.altitude, gap)

        ratio = (when - before.when).total_seconds() / span
        lat = before.lat + (after.lat - before.lat) * ratio
        dlon = after.lon - before.lon
        if dlon > 180.0:
            dlon -= 360.0
        elif dlon < -180.0:
            dlon += 360.0
        lon = before.lon + dlon * ratio
        if lon > 180.0:
            lon -= 360.0
        elif lon < -180.0:
            lon += 360.0
        altitude = None
        if before.altitude is not None and after.altitude is not None:
            altitude = before.altitude + (after.altitude - before.altitude) * ratio
        return (lat, lon, altitude, gap)

    def to_geojson(self) -> dict:
        """A LineString for the map, plus the endpoints as properties so the
        UI can label where a session started and finished."""
        coords = [[p.lon, p.lat] for p in self.points]
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "source": self.source,
                "derived": self.derived,
                "points": len(self.points),
                "distance_km": round(self.distance_km(), 3),
                "start": self.start.strftime("%Y-%m-%d %H:%M:%S") if self.start else None,
                "end": self.end.strftime("%Y-%m-%d %H:%M:%S") if self.end else None,
            },
        }


def track_from_sightings(sightings: Iterable[Sighting], source: str = "") -> Optional[Track]:
    """Rebuild a GPS track out of records that already carry a fix.

    This is what makes placing Flipper captures practical. Nothing on a
    Flipper writes a GPS log: the `gps_nmea` app displays a position on
    screen and never saves it, so on paper you need a separate logger just
    to put an NFC read or a Sub-GHz capture on the map.

    But a Marauder wardrive is already a GPS log. Every row has a latitude, a
    longitude and a timestamp, because that's how the row got its coordinates
    in the first place. Run a wardrive while you're walking around with the
    Flipper and the CSV doubles as the track that places everything else.

    Only records with a genuine fix are used. Feeding track-placed records
    back in would be inferring positions from inferred positions.

    A wardrive row's FirstSeen is UTC off the GPS module, same as any NMEA
    sentence, so it needs the same UTC -> local conversion `parse_nmea` gives
    every other track point, otherwise this track sits hours away from a
    Flipper file's mtime (which is local) and nothing derived from it ever
    places. A Flipper record with its own embedded GPS fix (Momentum/
    RogueMaster/Xtreme's `Lat:`/`Lon:`) is not a wardrive row and is already
    local (from the file's mtime or its `Ts:` field), so it's left alone;
    converting it too would shift it the wrong way.
    """
    points = []
    seen_times = set()
    for s in sightings:
        if s.geo_source != GEO_DIRECT or not s.has_location:
            continue
        when = parse_first_seen(s.first_seen)
        if when is None:
            continue
        if s.type in RADIO_TYPES:
            when = _utc_to_local_naive(when)
        if when in seen_times:
            continue
        seen_times.add(when)
        points.append(TrackPoint(lat=s.lat, lon=s.lon, when=when, altitude=s.altitude))

    if len(points) < 2:
        return None
    points.sort(key=lambda p: p.when)
    return Track(points, source=source, derived=True)


def load_tracks(paths: Iterable[Path]) -> list[Track]:
    tracks = []
    for path in paths:
        points = parse_track_file(Path(path))
        if points:
            tracks.append(Track(points, source=str(path)))
    return tracks


def geotag(
    sightings: Iterable[Sighting],
    tracks: Iterable[Track],
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
    clock_offset_seconds: int = 0,
) -> int:
    """Place every un-located sighting onto the tracks by timestamp.

    Mutates in place and returns how many got a location. Records that
    already have one are left alone: a real GPS fix is never overwritten by
    an inference. When several tracks could match, the closest in time wins.
    """
    track_list = [t for t in tracks if t.timed_count]
    if not track_list:
        return 0

    offset = timedelta(seconds=clock_offset_seconds)
    placed = 0

    for sighting in sightings:
        if sighting.has_location or sighting.geo_source != GEO_NONE:
            continue
        when = parse_first_seen(sighting.first_seen)
        if when is None:
            continue
        when = when + offset

        best = None
        for track in track_list:
            found = track.locate(when, max_gap_seconds=max_gap_seconds)
            if found is None:
                continue
            if best is None or found[3] < best[3]:
                best = found

        if best is None:
            continue

        lat, lon, altitude, gap = best
        sighting.lat = lat
        sighting.lon = lon
        if altitude is not None:
            sighting.altitude = altitude
        sighting.geo_source = GEO_TRACK
        sighting.meta["geo_match_seconds"] = round(gap, 1)
        placed += 1

    return placed
