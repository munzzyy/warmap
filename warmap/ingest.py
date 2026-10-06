"""One entry point for "here are some paths, work out what they are".

Everything that imports data goes through `ingest_paths`: the toolbar, the
folder importer, the SD-card importer and the CLI. Given any mix of files and
directories it sorts out which parser each one needs, loads GPS tracks first
so captures without coordinates can be placed against them, and returns both
the records and a plain-language account of what happened.

That account matters more than it sounds. Most of the ways this goes wrong
are silent: a pcap in a link type warmap can't read, a folder of `.sub` files
whose timestamps were destroyed by a careless copy, a GPS track that doesn't
overlap the captures in time. All of those produce "0 records" and none of
them are the same problem. `IngestResult.notes` says which one it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from warmap import flipper, gps, parse, pcap
from warmap.gps import Track
from warmap.models import GEO_NONE

# A folder walk stops here rather than trying to read an entire filesystem
# someone pointed at it by accident.
MAX_FILES = 20_000

CSV_EXTENSIONS = {".csv"}
TRACK_EXTENSIONS = {".gpx", ".nmea"}
# `.log` and `.txt` are ambiguous and have to be sniffed rather than assumed.
# Both are the usual NMEA log extensions, and both are also what a Marauder
# wardrive lands as: the Flipper companion app saves to
# `/ext/apps_data/marauder/dumps/wardrive_0.txt` and a standalone Marauder
# writes `/wardrive_0.log` at the root of its own card. Neither is ever a
# `.csv`, so matching on extension alone would miss every capture taken that
# way, which is most of them.
AMBIGUOUS_EXTENSIONS = {".log", ".txt"}


@dataclass
class IngestResult:
    sightings: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    files_read: int = 0
    files_skipped: int = 0
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def located(self) -> int:
        return sum(1 for s in self.sightings if s.has_location)

    @property
    def unlocated(self) -> int:
        return sum(1 for s in self.sightings if not s.has_location)

    def summary(self) -> str:
        if not self.sightings and not self.tracks:
            return "Nothing usable found."
        bits = []
        if self.sightings:
            bits.append(f"{len(self.sightings)} record(s) from {self.files_read} file(s)")
        if self.tracks:
            points = sum(len(t) for t in self.tracks)
            bits.append(f"{len(self.tracks)} GPS track(s), {points} point(s)")
        if self.unlocated:
            bits.append(f"{self.unlocated} without a location")
        return "; ".join(bits)


def _unreadable(path: Path, exc: BaseException) -> str:
    return f"{path.name}: couldn't read it ({type(exc).__name__}: {exc})"


# A directory walk visits at most this many entries per argument, files or
# not, so a tree of a million empty folders cannot hold the GUI thread.
MAX_WALKED = 300_000


def expand_paths(paths: Iterable[Path]) -> list[Path]:
    """Flatten files and directories into a list of files, capped."""
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found: list[Path] = []
            walked = 0
            try:
                for child in path.rglob("*"):
                    walked += 1
                    if walked > MAX_WALKED:
                        break
                    if child.is_file():
                        found.append(child)
                    if len(out) + len(found) >= MAX_FILES:
                        break
            except OSError:
                pass
            out.extend(sorted(found))
            if len(out) >= MAX_FILES:
                return out[:MAX_FILES]
        elif path.is_file():
            out.append(path)
        if len(out) >= MAX_FILES:
            break
    return out


def _head(path: Path, size: int = 2048) -> str:
    """The first couple of KB, for content sniffing. Bounded so pointing the
    importer at a folder of large logs doesn't read them all end to end."""
    # utf-8-sig, not utf-8: a byte-order mark sits in front of the very text
    # these sniffers match on, so a BOM'd wardrive written by a Windows-side
    # tool would fail `startswith("WigleWifi")` and be skipped as an
    # unrecognized file. Harmless no-op when there's no BOM.
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read(size)
    except OSError:
        return ""


def _looks_like_nmea(head: str) -> bool:
    return any(talker in head for talker in ("$GP", "$GN", "$GL", "$GA", "$BD"))


def _looks_like_wardrive(head: str) -> bool:
    """A Wigle-format wardrive, whatever the file is called. Either the
    version line is there or the column header is."""
    if head.startswith("WigleWifi"):
        return True
    first_lines = head.split("\n", 3)[:3]
    for line in first_lines:
        lowered = line.lower()
        if "mac" in lowered and "currentlatitude" in lowered:
            return True
    return False


def classify(path: Path) -> str:
    """What kind of file this is: csv, flipper, pcap, track, or unknown.

    Extension first, then content for the ambiguous ones. A wardrive saved by
    the Flipper Marauder companion app is a `.txt`, and one saved by a
    standalone Marauder is a `.log`, both are Wigle CSV inside, so the
    content check is what actually finds them.
    """
    suffix = path.suffix.lower()
    if suffix in CSV_EXTENSIONS:
        return "csv"
    if flipper.is_flipper_file(path):
        return "flipper"
    if pcap.is_pcap_file(path):
        return "pcap"
    if suffix in TRACK_EXTENSIONS:
        return "track"
    if suffix in AMBIGUOUS_EXTENSIONS:
        head = _head(path)
        if _looks_like_wardrive(head):
            return "csv"
        if _looks_like_nmea(head):
            return "track"
    return "unknown"


def ingest_paths(
    paths: Iterable[Path],
    extra_tracks: Iterable[Track] = (),
    max_gap_seconds: int = gps.DEFAULT_MAX_GAP_SECONDS,
    clock_offset_seconds: int = 0,
) -> IngestResult:
    """Read everything at `paths` and return the records plus what happened.

    `extra_tracks` lets a caller supply tracks loaded in a previous import:
    the app keeps them around so a Flipper folder imported after a GPS log
    still gets placed.
    """
    result = IngestResult()
    files = expand_paths(paths)
    if not files:
        result.notes.append("No files found at those paths.")
        return result

    buckets: dict[str, list[Path]] = {"csv": [], "flipper": [], "pcap": [], "track": []}
    for path in files:
        kind = classify(path)
        if kind == "unknown":
            result.files_skipped += 1
            continue
        buckets[kind].append(path)

    # Tracks first: they're what gives everything else a location.
    tracks: list[Track] = list(extra_tracks)
    for path in buckets["track"]:
        try:
            points = gps.parse_track_file(path)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(_unreadable(path, exc))
            result.files_skipped += 1
            continue
        if points:
            track = Track(points, source=str(path))
            tracks.append(track)
            result.tracks.append(track)
            result.files_read += 1
        else:
            result.notes.append(f"{path.name}: no usable GPS fixes in it.")

    for path in buckets["csv"]:
        try:
            found = parse.parse_file(path)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(_unreadable(path, exc))
            result.files_skipped += 1
            continue
        if found:
            result.sightings.extend(found)
            result.files_read += 1
        else:
            result.files_skipped += 1

    for path in buckets["flipper"]:
        try:
            found = flipper.parse_flipper_file(path)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(_unreadable(path, exc))
            result.files_skipped += 1
            continue
        if found:
            result.sightings.extend(found)
            result.files_read += 1
        else:
            result.files_skipped += 1

    for path in buckets["pcap"]:
        try:
            outcome = pcap.read_pcap(path)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(_unreadable(path, exc))
            result.files_skipped += 1
            continue
        if outcome.error:
            result.errors.append(outcome.error)
            continue
        if outcome.note:
            result.notes.append(outcome.note)
        if outcome.sightings:
            result.sightings.extend(outcome.sightings)
            result.files_read += 1
            result.notes.append(
                f"{path.name}: {outcome.packets_decoded} of {outcome.packets_read} "
                f"packets decoded ({outcome.link_type_name})."
            )
        else:
            result.notes.append(
                f"{path.name}: readable ({outcome.link_type_name}) but held no "
                "beacons or advertisements."
            )

    # Flipper files carry no coordinates. Place them against the tracks.
    unplaced = [s for s in result.sightings if s.geo_source == GEO_NONE]

    if unplaced:
        placed = 0
        if tracks:
            placed = gps.geotag(
                unplaced, tracks,
                max_gap_seconds=max_gap_seconds,
                clock_offset_seconds=clock_offset_seconds,
            )
            if placed:
                result.notes.append(
                    f"Placed {placed} record(s) on the map by matching their "
                    "timestamps against the GPS track."
                )

        # Whatever's left had no track covering its moment in time. Nothing on
        # a Flipper logs GPS, so for most sessions that's every capture, but a
        # wardrive CSV is already a GPS log, since every row carries the fix it
        # was recorded at. Rebuild a track from those rather than asking for
        # equipment the user doesn't need.
        #
        # Second, not first: a real track file has denser points and places
        # more accurately, so it gets first refusal. This also has to key off
        # what's still unplaced rather than off whether any track exists at
        # all; an unrelated track loaded earlier in the session would
        # otherwise suppress the rebuild and leave everything on the floor.
        still_unplaced = [s for s in unplaced if s.geo_source == GEO_NONE]
        if still_unplaced:
            derived = gps.track_from_sightings(
                result.sightings, source="wardrive rows"
            )
            if derived is not None:
                tracks.append(derived)
                result.tracks.append(derived)
                recovered = gps.geotag(
                    still_unplaced, [derived],
                    max_gap_seconds=max_gap_seconds,
                    clock_offset_seconds=clock_offset_seconds,
                )
                placed += recovered
                if recovered:
                    result.notes.append(
                        f"No GPS track covered {len(still_unplaced)} of these, so a "
                        f"track was rebuilt from the {len(derived)} wardrive rows "
                        f"that carry their own fix, which placed {recovered} more."
                    )

        still_missing = sum(1 for s in unplaced if s.geo_source == GEO_NONE)
        if still_missing and tracks:
            # There was something to match against, it just didn't line up in
            # time. The fix is the tolerance or the clock offset, not loading
            # a track they've already got.
            result.notes.append(
                f"{still_missing} record(s) had no track point within "
                f"{max_gap_seconds}s of their timestamp, so they have no "
                "location. They're still listed, just not mapped. Widen the "
                "tolerance under Tools > GPS placement settings, or set a "
                "clock offset there if the Flipper's clock was wrong."
            )
        elif still_missing:
            result.notes.append(
                f"{still_missing} record(s) have no location. Flipper files "
                "don't store coordinates, and there was no GPS track and no "
                "wardrive fix to rebuild one from. Run a Marauder wardrive "
                "alongside the Flipper next time: its rows double as the "
                "track, or load a .nmea/.gpx recorded during the session."
            )

        flipper_paths = buckets["flipper"]
        if flipper_paths and flipper.mtime_looks_clobbered(flipper_paths):
            result.notes.append(
                "Warning: these Flipper files all have near-identical, very "
                "recent modification times. That usually means they were "
                "copied off the SD card without preserving timestamps, which "
                "destroys the only clue to when each capture happened. "
                "Re-copy with `cp -p` or `rsync -a` to fix it."
            )

    return result
