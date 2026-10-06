"""warmap's tiny persisted-preferences file, window geometry today. A
preference, not state warmap depends on to run: a missing or corrupt
settings file must never be fatal, it just means "use the defaults," same
fail-soft posture as the collected store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from warmap import config


def load(path: Optional[Path] = None) -> dict:
    path = Path(path) if path is not None else config.SETTINGS_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict, path: Optional[Path] = None) -> None:
    path = Path(path) if path is not None else config.SETTINGS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
