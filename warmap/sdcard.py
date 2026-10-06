"""Find capture files on removable media: the "Import from SD card" action.
Pure path-scanning, no Qt; the caller (mainwindow) turns the result into a
confirm dialog and then an ingest call.

Two cards get plugged in here: the Marauder's, which holds wardrive CSVs and
pcaps, and the Flipper's, which holds `.sub`/`.nfc`/`.rfid`/`.ibtn`/`.ir`
under its per-app directories. Both are handled the same way: collect
anything with a recognized extension and let the parsers sort it out.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Where a Linux desktop mounts removable media. $USER is resolved at call
# time (not import time) so tests can override it cleanly via the `user`
# parameter instead of monkeypatching the environment. This is udisks2's
# own convention; GNOME, KDE and XFCE automount all use it.
_USER_MEDIA_ROOTS = ("/run/media/{user}", "/media/{user}")

# Conventional mount points that carry no per-user path component. A card
# mounted by hand (`sudo mount /dev/sdX1 /mnt/sdcard`, or `udisksctl mount`
# on a system with no per-user automount configured) lands directly under one
# of these rather than under a `$USER` subdirectory, and $USER not being set
# doesn't change where they are, so they're always worth checking, not just
# when a user could be resolved.
_SYSTEM_MEDIA_ROOTS = ("/media", "/mnt")

# GetDriveTypeW's answer for a removable drive (an SD reader, a USB stick).
_DRIVE_REMOVABLE = 2

# Everything warmap can read. Kept here rather than imported from ingest so
# this module stays free of parser dependencies.
CAPTURE_EXTENSIONS = frozenset({
    ".csv",                                        # wardrive exports
    ".sub", ".nfc", ".rfid", ".ibtn", ".ir", ".picopass",  # Flipper captures
    ".pcap", ".pcapng", ".cap",                    # packet captures
    ".gpx", ".nmea",                               # GPS tracks
})

# Scanned for, but only when the contents back it up. Both extensions carry
# real captures: the Flipper Marauder companion app saves wardrives to
# `apps_data/marauder/dumps/wardrive_0.txt` and a standalone Marauder writes
# `wardrive_0.log`. Both are also what unrelated junk on a card is called, and
# `.log` on a Flipper SD is often just the firmware's debug log.
AMBIGUOUS_EXTENSIONS = frozenset({".log", ".txt"})

# A card with more matching files than this is almost certainly not a
# capture card. Applied across the whole scan (every root combined), same as
# before.
_MAX_RESULTS = 5000

# A safety valve on top of _MAX_RESULTS, which only bounds *matches*: a card
# that's mostly a large personal photo/music library and holds nothing
# warmap recognizes would never trip it, and the walk would still have to
# visit every one of those files before giving up. This bounds the walk
# itself, per root, so a huge irrelevant tree can't take the GUI thread that
# calls this down for tens of seconds or more. Far above any real capture
# card's file count (a stock Flipper SD is a few hundred files at most).
_MAX_SCANNED_PER_ROOT = 300_000


class ScanResult(list):
    """The files `scan_removable_media` found: a plain list of paths in
    every way that matters to existing callers (`len()`, slicing, `for`,
    `if not found`), plus one extra bit: `.truncated` is True when
    `_MAX_SCANNED_PER_ROOT` or `_MAX_RESULTS` cut a walk short, so a caller
    that wants to tell the user "there may be more on this card than what's
    listed" has something to check for it.
    """

    def __init__(self, iterable=(), truncated: bool = False):
        super().__init__(iterable)
        self.truncated = truncated


def _windows_removable_drives(drive_type=None) -> list[Path]:
    """Drive letters Windows reports as removable (an SD reader, a USB stick).
    `drive_type` is injectable so the logic is testable off Windows."""
    if drive_type is None:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except (ImportError, AttributeError):
            return []

        def drive_type(root: str) -> int:
            return int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))

    roots = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            if drive_type(root) == _DRIVE_REMOVABLE:
                roots.append(Path(root))
        except (OSError, ValueError):
            continue
    return roots


def _macos_volumes(volumes: Path = Path("/Volumes")) -> list[Path]:
    """Every mounted volume except the boot disk, which shows up under
    /Volumes too and must not be walked."""
    roots = []
    try:
        entries = sorted(volumes.iterdir())
    except OSError:
        return roots
    for entry in entries:
        try:
            if entry.is_dir() and not os.path.samefile(entry, "/"):
                roots.append(entry)
        except OSError:
            continue
    return roots


def default_media_roots(user: Optional[str] = None, platform: str = sys.platform) -> list[Path]:
    """Where this platform mounts removable media. Linux and the BSDs get the
    udisks2 per-user paths plus the fixed ones a hand mount lands under;
    macOS gets everything under /Volumes except the boot disk; Windows gets
    the drive letters it reports as removable."""
    if platform == "win32":
        return _windows_removable_drives()
    if platform == "darwin":
        return _macos_volumes()
    user = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    roots: list[Path] = []
    if user:
        roots.extend(Path(root.format(user=user)) for root in _USER_MEDIA_ROOTS)
    roots.extend(Path(root) for root in _SYSTEM_MEDIA_ROOTS)
    return roots


def describe_roots(platform: str = sys.platform) -> str:
    """A short, human phrase for where the scan looked, for the "nothing
    found" message."""
    if platform == "win32":
        return "any removable drive"
    if platform == "darwin":
        return "/Volumes"
    return "/run/media, /media or /mnt"


def _is_capture_content(path: Path) -> bool:
    """Whether an ambiguously-named file actually holds a wardrive or a GPS
    track. Reads only the head of the file.

    `utf-8-sig`: a leading BOM (some export tools and any Windows-side text
    editor can add one) would otherwise sit in front of "WigleWifi" and make
    the first check below miss a real wardrive. Transparent no-op when
    there's no BOM.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            head = f.read(2048)
    except OSError:
        return False
    if head.startswith("WigleWifi"):
        return True
    for line in head.split("\n", 3)[:3]:
        lowered = line.lower()
        if "mac" in lowered and "currentlatitude" in lowered:
            return True
    return any(talker in head for talker in ("$GP", "$GN", "$GL", "$GA", "$BD"))


def scan_removable_media(
    roots: Optional[list[Path]] = None,
    user: Optional[str] = None,
) -> ScanResult:
    """Every capture file found under any mounted-media root (case-insensitive
    extension).

    Doesn't require a Wigle header or a Flipper filetype line: the parsers
    already degrade to an empty result for anything they can't read, so a
    stray unrelated file on the card just yields zero records rather than
    crashing the scan.

    Missing/unmounted roots are silently skipped, not an error: no card
    inserted is the normal case, not a fault condition.
    """
    if roots is None:
        roots = default_media_roots(user)

    found: list[Path] = []
    seen: set = set()
    truncated = False

    for root in roots:
        if not root.is_dir():
            continue
        if len(found) >= _MAX_RESULTS:
            truncated = True
            break
        visited = 0
        try:
            # Not `sorted(root.rglob("*"))`: sorted() has to exhaust the
            # generator before yielding anything, which means the caps below
            # can never actually cut a huge walk short: every entry gets
            # visited regardless before either cap gets a chance to apply.
            # Sorting only `found` at the end gives the same deterministic,
            # alphabetical result for everything that matters (the case where
            # a cap truncates a single root, so which specific files survive
            # is already the "this probably isn't a capture card" case, where
            # exactly which extras got dropped isn't a promise worth keeping).
            for path in root.rglob("*"):
                visited += 1
                if visited > _MAX_SCANNED_PER_ROOT:
                    truncated = True
                    break  # this root only; a huge unrelated mount elsewhere
                            # shouldn't stop a different, smaller one from
                            # being scanned fully
                if len(found) >= _MAX_RESULTS:
                    truncated = True
                    break
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in CAPTURE_EXTENSIONS:
                    pass
                elif suffix in AMBIGUOUS_EXTENSIONS and _is_capture_content(path):
                    pass
                else:
                    continue
                # The same device can be visible under two roots; don't offer
                # to import it twice.
                try:
                    key = path.resolve()
                except OSError:
                    key = path
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
        except OSError:
            continue

    return ScanResult(sorted(found), truncated=truncated)
