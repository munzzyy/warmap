"""warmap's paths, all overridable by env var so tests never touch real files.
Nothing in this module does I/O; it only decides where things live.

Per-user state follows each platform's convention: XDG on Linux and the
BSDs, Application Support on macOS, LOCALAPPDATA and APPDATA on Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from warmap import __version__


def _home() -> Path:
    return Path.home()


def default_data_dir(platform: str = sys.platform, env: "os._Environ[str] | dict" = os.environ) -> Path:
    """Where the collected store and the tile cache go when WARMAP_DATA_DIR
    is not set."""
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
    elif platform == "darwin":
        base = str(_home() / "Library" / "Application Support")
    else:
        base = env.get("XDG_DATA_HOME") or str(_home() / ".local" / "share")
    return Path(base) / "warmap"


def default_config_dir(platform: str = sys.platform, env: "os._Environ[str] | dict" = os.environ) -> Path:
    """Where the small settings file goes when WARMAP_CONFIG_DIR is not set."""
    if platform == "win32":
        base = env.get("APPDATA") or str(_home() / "AppData" / "Roaming")
    elif platform == "darwin":
        base = str(_home() / "Library" / "Application Support")
    else:
        base = env.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "warmap"


# Where warmap keeps its own state: the persistent collected store and the
# on-disk tile cache. Tests set WARMAP_DATA_DIR to a tmp dir so nothing
# pollutes the real machine.
DATA_DIR = Path(os.environ.get("WARMAP_DATA_DIR") or default_data_dir())
TILE_CACHE_DIR = DATA_DIR / "tiles"
STORE_PATH = DATA_DIR / "collected.json"

# Small persisted preferences (window geometry, dock state).
CONFIG_DIR = Path(os.environ.get("WARMAP_CONFIG_DIR") or default_config_dir())
SETTINGS_PATH = CONFIG_DIR / "settings.json"

# The installed package. Everything bundled with the app lives under it, so
# a pip install carries the sample, the map, the phone app and the
# registries along with the code.
PACKAGE_DIR = Path(__file__).resolve().parent

# The checkout, when running from one. Only the tools and the tests need it.
REPO_ROOT = PACKAGE_DIR.parent

# The bundled demo session: a wardrive CSV, the GPS track recorded alongside
# it, and a folder of Flipper captures. Loaded on first run when the store is
# still empty. SAMPLE_CSV stays a separate name because plenty of code (and
# the tests) only wants the CSV.
SAMPLE_DIR = PACKAGE_DIR / "sample"
SAMPLE_CSV = SAMPLE_DIR / "sample_wardrive.csv"

WEB_DIR = PACKAGE_DIR / "web"
MAP_HTML = WEB_DIR / "map.html"

# The mobile app (a PWA) that warmap.server hands to a phone on your network.
# It shares web/map.js and web/vendor with the desktop map rather than
# duplicating them, so the two can never drift apart.
WEBAPP_DIR = PACKAGE_DIR / "webapp"

# The identifier registries (Bluetooth SIG company IDs and UUIDs, IEEE OUI)
# that warmap.ble layers on top of its built-in tables. Checked in so a fresh
# clone resolves vendors with no network; refresh with `tools/fetch_ids.py`.
BUNDLED_DATA_DIR = PACKAGE_DIR / "data"

# OSM tile policy requires a real, identifying User-Agent on every request.
TILE_USER_AGENT = f"warmap/{__version__} (+https://github.com/munzzyy/warmap)"
TILE_UPSTREAM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# The DeFlock/OSM ALPR-camera snapshot (see warmap/alpr.py). The bundled copy
# ships with the app so a fresh install shows cameras offline; a refresh
# writes a user copy under DATA_DIR so `warmap flock --refresh` never needs
# write access to the package. flock_data_path() prefers the user copy when
# it exists.
FLOCK_BUNDLED_PATH = BUNDLED_DATA_DIR / "flock_cameras.json"
FLOCK_USER_PATH = DATA_DIR / "flock_cameras.json"


def flock_data_path() -> Path:
    """Where to read the camera snapshot from: an explicit WARMAP_FLOCK_PATH
    override first (tests point this at a tiny fixture, or at a nonexistent
    path to load no cameras and stay fast), then the user-refreshed copy, then
    the version shipped with the app. Read at call time, not import, so a
    test can set it per-case."""
    override = os.environ.get("WARMAP_FLOCK_PATH")
    if override:
        return Path(override)
    return FLOCK_USER_PATH if FLOCK_USER_PATH.exists() else FLOCK_BUNDLED_PATH


def ensure_data_dir() -> Path:
    """Create DATA_DIR (and the tile cache dir under it) if missing. Owner-only
    where the filesystem has a notion of that."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(DATA_DIR, 0o700)
        except OSError:
            pass
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def ensure_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR
