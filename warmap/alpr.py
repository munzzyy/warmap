"""Automatic license-plate reader (ALPR) cameras as a reference overlay.

This is the one part of warmap that isn't your own capture. Everything else
in the app is something a radio on the ground actually heard; these are camera
locations compiled by the DeFlock project (deflock.org) from OpenStreetMap,
where each camera is a node tagged `man_made=surveillance` +
`surveillance:type=ALPR`. Flock Safety is the dominant vendor, hence the
common name, but the same tagging covers Motorola/Vigilant, Genetec, Leonardo/
ELSAG and the rest, so this module keeps the manufacturer rather than assuming.

Because it is reference data and not a sighting, it is kept deliberately
separate from the `Sighting` pipeline all the way through:

* It never enters the collected-captures store (`store.py`). Nothing here is
  something you found; merging it in would corrupt the one thing that store is
  for.
* It is never written to a Wigle CSV export. That format exists to upload
  your own findings to a public wardriving database, and a camera you didn't
  detect has no business in it.
* On the map it lives in its own layer (see `warmapSetAlpr` in web/map.js),
  outside the capture markers, the heatmap and fit-to-data.

The app loads a checked-in snapshot (`warmap/data/flock_cameras.json`) so a
fresh clone shows cameras with no network, exactly like the OUI/Bluetooth
registries. Refresh it with `tools/fetch_flock.py` (or `warmap flock
--refresh`), which re-pulls the current set from OpenStreetMap.

Attribution: OpenStreetMap's ODbL requires crediting "© OpenStreetMap
contributors" wherever this data is shown. The popup and the snapshot both
carry it, and the on-map layer credits it too.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# The snapshot is a compiled list of small records, but it is still read into
# memory whole before anything is built from it. Same 256 MB ceiling the
# capture parsers use: far above any real camera set (the worldwide set is a
# few MB) and low enough that a file pointed at by mistake can't wedge the app.
MAX_FILE_BYTES = 256 * 1024 * 1024

# What OSM's ODbL asks for wherever this data is redisplayed. Carried in the
# snapshot too, but defaulted here so a hand-made or older snapshot without it
# still credits correctly.
DEFAULT_ATTRIBUTION = (
    "Camera locations © OpenStreetMap contributors (ODbL), "
    "compiled by the DeFlock project (deflock.org)."
)

# Compass points to degrees clockwise from north. OSM's `direction` is usually
# a number, but a fair share of nodes use a cardinal string, and both are
# valid per the OSM wiki, so both are read rather than dropping the string
# ones on the floor.
_COMPASS = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
    "NORTH": 0.0, "EAST": 90.0, "SOUTH": 180.0, "WEST": 270.0,
}


def parse_direction(raw) -> list[float]:
    """Every camera bearing in `raw`, as degrees clockwise from north in
    [0, 360).

    OSM writes this field several ways and warmap has to read all of them or
    silently drop the arrow that tells you which way a camera is looking:

    * a single number: ``"90"`` or ``"90.0"``
    * several on one pole, semicolon-separated: ``"90;180;270"``
    * a compass point: ``"NE"``, ``"south"``
    * a number with stray text: tolerated by pulling the leading number out

    Anything that yields no usable bearing returns an empty list, which the
    map reads as "camera present, aim unknown" and draws as a plain dot with
    no wedge, never as north.
    """
    if raw is None:
        return []
    out: list[float] = []
    for piece in str(raw).replace(",", ";").split(";"):
        token = piece.strip()
        if not token:
            continue
        upper = token.upper()
        if upper in _COMPASS:
            out.append(_COMPASS[upper])
            continue
        # A bare or trailing-text number: "90", "90.0", "90 deg".
        value = _leading_float(token)
        if value is None or not math.isfinite(value):
            continue
        out.append(value % 360.0)
    return out


def _leading_float(token: str) -> Optional[float]:
    """The number at the start of `token`, or None. Kept deliberately small:
    it only has to survive the shapes OSM's direction field actually takes."""
    end = 0
    seen_dot = False
    for i, ch in enumerate(token):
        if ch.isdigit():
            end = i + 1
        elif ch == "." and not seen_dot:
            seen_dot = True
            end = i + 1
        elif ch == "-" and i == 0:
            continue
        else:
            break
    head = token[:end]
    if not head or head in ("-", ".", "-."):
        return None
    try:
        return float(head)
    except ValueError:
        return None


def _valid_coord(lat: float, lon: float) -> bool:
    """A real point on Earth, and not the 0,0 sentinel a broken export writes
    for a camera whose position never got filled in."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    return not (lat == 0.0 and lon == 0.0)


@dataclass
class Camera:
    """One ALPR camera. `osm_id` is its OpenStreetMap element id (`node/123`
    or `way/456`) so a camera can be traced back to the map it came from and
    deduped across refreshes."""

    osm_id: str
    lat: float
    lon: float
    directions: list[float] = field(default_factory=list)
    manufacturer: str = ""
    operator: str = ""
    mount: str = ""
    zone: str = ""
    brand: str = ""
    ref: str = ""
    direction_raw: str = ""

    @property
    def is_flock(self) -> bool:
        """A Flock Safety camera specifically, as opposed to another vendor's
        ALPR. Matched on the manufacturer/brand text because OSM spells it a
        dozen ways (`Flock Safety`, `Flock`, `flock_safety`, and the
        misspellings the DeFlock project tracks in its own vendor list)."""
        text = f"{self.manufacturer} {self.brand}".lower()
        return "flock" in text

    @property
    def display_name(self) -> str:
        if self.operator:
            return self.operator
        if self.manufacturer:
            return self.manufacturer
        if self.brand:
            return self.brand
        return "ALPR camera"

    def to_feature(self) -> dict:
        """GeoJSON Feature for the map's ALPR layer. The property names here
        are exactly what `warmapSetAlpr` in web/map.js reads."""
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": {
                "id": self.osm_id,
                "dir": self.directions,
                "mfg": self.manufacturer,
                "op": self.operator,
                "mount": self.mount,
                "zone": self.zone,
                "brand": self.brand,
                "ref": self.ref,
                "flock": self.is_flock,
            },
        }


def _camera_from_record(rec: object) -> Optional[Camera]:
    """One snapshot record -> Camera, or None if it has no usable position.

    The snapshot uses short keys (`lat`/`lon`/`dir`/`mfg`/`op`/...) to keep
    the worldwide file small; a couple of long aliases are accepted too so a
    hand-authored or differently-shaped file still loads.
    """
    if not isinstance(rec, dict):
        return None
    lat = _opt_float(rec.get("lat"))
    lon = _opt_float(rec.get("lon"))
    if lat is None or lon is None or not _valid_coord(lat, lon):
        return None
    direction_raw = rec.get("dir")
    if direction_raw is None:
        direction_raw = rec.get("direction")
    return Camera(
        osm_id=str(rec.get("id") or rec.get("osm_id") or ""),
        lat=lat,
        lon=lon,
        directions=parse_direction(direction_raw),
        manufacturer=str(rec.get("mfg") or rec.get("manufacturer") or ""),
        operator=str(rec.get("op") or rec.get("operator") or ""),
        mount=str(rec.get("mount") or ""),
        zone=str(rec.get("zone") or ""),
        brand=str(rec.get("brand") or ""),
        ref=str(rec.get("ref") or ""),
        direction_raw="" if direction_raw is None else str(direction_raw),
    )


def _opt_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Snapshot:
    cameras: list[Camera] = field(default_factory=list)
    attribution: str = DEFAULT_ATTRIBUTION
    source: str = ""
    generated: str = ""

    def __len__(self) -> int:
        return len(self.cameras)


def load_snapshot(path: Path) -> Snapshot:
    """Read a camera snapshot from disk, fail-soft.

    A missing, oversized, corrupt or wrong-shaped file yields an empty
    snapshot rather than an exception, the same posture the store uses, and
    the right one here too: no cameras is a fine state to be in, and it must
    never take the window down on startup. Every record that doesn't carry a
    real coordinate is skipped individually, so one bad row can't discard the
    rest of the file.
    """
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return Snapshot()
    except OSError:
        return Snapshot()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return Snapshot()
    try:
        data = json.loads(raw)
    except ValueError:
        return Snapshot()

    # Two accepted shapes: the wrapped object the fetcher writes (metadata plus
    # a "cameras" list) and a bare list of camera records, so a trivially
    # hand-made file works too.
    if isinstance(data, list):
        records = data
        attribution = DEFAULT_ATTRIBUTION
        source = ""
        generated = ""
    elif isinstance(data, dict):
        records = data.get("cameras")
        if not isinstance(records, list):
            return Snapshot()
        attribution = str(data.get("attribution") or DEFAULT_ATTRIBUTION)
        source = str(data.get("source") or "")
        generated = str(data.get("generated") or "")
    else:
        return Snapshot()

    cameras: list[Camera] = []
    for rec in records:
        camera = _camera_from_record(rec)
        if camera is not None:
            cameras.append(camera)
    return Snapshot(
        cameras=cameras, attribution=attribution, source=source, generated=generated,
    )


def load_cameras(path: Path) -> list[Camera]:
    """Just the cameras, for callers that don't need the metadata."""
    return load_snapshot(path).cameras


def to_geojson(cameras: Iterable[Camera]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [c.to_feature() for c in cameras],
    }


# The order of TRANSFER_FIELDS is the order of each row in `to_transfer`. It
# ships inside the payload so the client rehydrates by name rather than by a
# hard-coded column count that would break silently if this list changed.
TRANSFER_FIELDS = ("lon", "lat", "dirs", "mfg", "op", "flock", "id")


def to_transfer(cameras: Iterable[Camera], attribution: str = DEFAULT_ATTRIBUTION,
                generated: str = "") -> dict:
    """The camera set as compact positional rows, for sending to the phone.

    Full GeoJSON for the worldwide set is ~36 MB, most of it the same property
    names repeated 137,000 times. As rows it's about a quarter of that before
    gzip, which matters over a phone connection and in the phone's storage.

    Everything semantic is resolved here rather than on the client: `dirs` is
    already parsed into numbers and `flock` is already decided, so the app
    never re-implements `parse_direction` or the vendor matching in JavaScript
    and the two can't drift apart.
    """
    rows = []
    for c in cameras:
        rows.append([
            round(c.lon, 6), round(c.lat, 6), c.directions,
            c.manufacturer, c.operator, 1 if c.is_flock else 0, c.osm_id,
        ])
    return {
        "fields": list(TRANSFER_FIELDS),
        "attribution": attribution,
        "generated": generated,
        "count": len(rows),
        "cameras": rows,
    }


# --- rollups for the stats panel -----------------------------------------

@dataclass
class CameraStats:
    total: int = 0
    flock_count: int = 0
    with_direction: int = 0
    near_captures: int = 0
    top_manufacturers: list = field(default_factory=list)  # [(name, count)]
    top_operators: list = field(default_factory=list)


def camera_stats(cameras: Iterable[Camera], near: int = 0) -> CameraStats:
    cameras = list(cameras)
    stats = CameraStats(total=len(cameras), near_captures=near)
    mfg: dict[str, int] = {}
    op: dict[str, int] = {}
    for c in cameras:
        if c.is_flock:
            stats.flock_count += 1
        if c.directions:
            stats.with_direction += 1
        name = c.manufacturer or c.brand
        if name:
            mfg[name] = mfg.get(name, 0) + 1
        if c.operator:
            op[c.operator] = op.get(c.operator, 0) + 1
    stats.top_manufacturers = sorted(mfg.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    stats.top_operators = sorted(op.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    return stats


def cameras_near(
    cameras: Iterable[Camera],
    points: Iterable[tuple[float, float]],
    meters: float = 500.0,
) -> list[Camera]:
    """The cameras within `meters` of any of `points`: the cameras you
    actually drove past, as opposed to the whole database. `points` are
    (lat, lon) pairs from the located captures and any loaded GPS track.

    The reference points get bucketed into a grid of cells about one radius
    wide, so each of the ~137k cameras only distance-checks the handful of
    points in its own cell and its neighbours instead of all of them. A naive
    every-camera-against-every-point scan is ~45M comparisons and visibly
    stalls an import; the grid drops it to a fraction of a second.
    """
    from warmap.gps import haversine_km

    pts = [(lat, lon) for lat, lon in points
           if isinstance(lat, (int, float)) and isinstance(lon, (int, float))
           and math.isfinite(lat) and math.isfinite(lon)]
    if not pts or meters <= 0:
        return []
    radius_km = meters / 1000.0
    cell_deg = radius_km / 111.0  # ~111 km per degree of latitude, everywhere
    if cell_deg <= 0:
        return []
    # Number of longitude cells around the globe, so the grid wraps at the
    # antimeridian: a camera at lon +179.99 and a point at lon -179.99 are
    # metres apart, and their raw cell indices differ by ~n_lon, but modulo
    # n_lon they're neighbours. haversine_km already measures the true wrapped
    # distance, so without this the grid would silently disagree with it there.
    n_lon = max(1, int(round(360.0 / cell_deg)))

    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for lat, lon in pts:
        key = (int(math.floor(lat / cell_deg)), int(math.floor(lon / cell_deg)) % n_lon)
        grid.setdefault(key, []).append((lat, lon))

    out: list[Camera] = []
    for c in cameras:
        clat = int(math.floor(c.lat / cell_deg))
        clon = int(math.floor(c.lon / cell_deg)) % n_lon
        # Longitude cells shrink (in km) toward the poles, so a point one radius
        # away in km can be several cells away in raw longitude degrees. Widen
        # the neighbour span for that, plus one extra ring so the small
        # km-per-degree constant mismatch and the cell's fractional offset can't
        # let a just-in-range point fall one cell outside the scan. Capped so a
        # corrupt near-pole coordinate can't blow the loop up; no real camera
        # sits high enough for the cap to matter.
        cos_lat = math.cos(math.radians(c.lat))
        if abs(cos_lat) > 1e-6:
            lon_span = min(8, int(math.ceil((radius_km / (111.195 * cos_lat)) / cell_deg)) + 1)
        else:
            lon_span = 8
        seen_cells = set()
        hit = False
        for dlat in (-1, 0, 1):
            if hit:
                break
            for dlon in range(-lon_span, lon_span + 1):
                cell = (clat + dlat, (clon + dlon) % n_lon)
                if cell in seen_cells:  # wrap can revisit a cell when span is wide
                    continue
                seen_cells.add(cell)
                bucket = grid.get(cell)
                if not bucket:
                    continue
                for lat, lon in bucket:
                    if haversine_km(c.lat, c.lon, lat, lon) <= radius_km:
                        hit = True
                        break
                if hit:
                    break
        if hit:
            out.append(c)
    return out
