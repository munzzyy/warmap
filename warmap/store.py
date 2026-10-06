"""The persistent "everything I've ever collected" store, a plain JSON
file of already-deduped AccessPoints. This is what makes captures additive
across app restarts: every import merges into it, `times_seen` accumulates
across sessions, and the bundled sample data never touches it (see
warmap/app.py's startup logic).

A preference/cache, not a database warmap depends on to boot: a missing or
corrupt store file just means "nothing collected yet" rather than a crash,
the same fail-soft posture as the settings file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from warmap.models import AccessPoint
from warmap.parse import dedup_by_bssid


def load(path: Path) -> list[AccessPoint]:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict):
            try:
                out.append(AccessPoint.from_dict(item))
            except (TypeError, ValueError):
                continue
    return out


def save(aps: list[AccessPoint], path: Path) -> None:
    """Atomic write (tmp file + rename) so a crash mid-write never leaves a
    half-written store behind for the next launch to choke on."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([ap.to_dict() for ap in aps], indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".warmap-store-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def merge_and_save(new_sightings: list[AccessPoint], path: Path) -> list[AccessPoint]:
    """Fold `new_sightings` (raw, times_seen=1 each) into whatever's already
    on disk at `path`, save the result, and return the merged deduped set."""
    existing = load(path)
    merged = dedup_by_bssid(existing + new_sightings)
    save(merged, path)
    return merged


def clear(path: Optional[Path] = None) -> None:
    """Wipe the persisted store (used by tests; not exposed in the UI:
    there's no undo for "forget everything I've collected")."""
    if path is None:
        return
    path = Path(path)
    try:
        path.unlink()
    except OSError:
        pass
