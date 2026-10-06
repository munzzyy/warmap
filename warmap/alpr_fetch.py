"""Pull the current ALPR/Flock camera set that the DeFlock project maps.

This is the network side of the camera overlay. `warmap/alpr.py` reads the
snapshot this writes; the two share no code beyond the on-disk shape.

Two sources, tried in order:

1. **DeFlock's own bulk export** (`data.dontgetflocked.com`), the project's
   compiled US and Canada camera files, rebuilt daily, keyless, behind
   Cloudflare with no rate limit. This is the primary source: it's a single
   reliable request per country, it's literally the dataset deflock.org shows,
   and it already carries the fields the overlay wants (`direction`/
   `directions`, `brand`, `operator`, `mountType`, `surveillanceZone`).

2. **Overpass** (OpenStreetMap) as a fallback, using the exact tag pair DeFlock
   maps with (`man_made=surveillance` + `surveillance:type=ALPR`). This is the
   true worldwide source of record, but a single worldwide query is ~54 MB and
   times out against Overpass's proxy ceiling more often than not (measured: a
   bare worldwide `out count` timed out at 100 s), so it's only reached when
   the DeFlock CDN can't be.

Safety posture, matching tools/fetch_ids.py:

* Endpoints are fixed constants, never anything a caller supplies. No
  user-controlled URL, so no SSRF surface.
* Responses are size-capped as they stream in, so a misbehaving mirror can't
  exhaust memory.
* A result with implausibly few cameras is refused rather than allowed to
  overwrite a good snapshot with a broken one.
* The write is temp-file-then-rename, so a crash mid-write never leaves a
  half-snapshot behind.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional  # noqa: F401  (kept for annotations)

# DeFlock's compiled bulk exports. Despite the `.gz` name the body is usually
# plain JSON (their Cloudflare Worker doesn't actually gzip it), so the reader
# below sniffs for the gzip magic bytes rather than assuming either way.
DEFLOCK_BULK_URLS = (
    ("US", "https://data.dontgetflocked.com/cameras.geojson.gz"),
    ("CA", "https://data.dontgetflocked.com/cameras-ca.geojson.gz"),
)

# Overpass mirrors for the fallback path, tried in order.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# node+way+relation tagged as an ALPR camera; `out center tags` gives every
# element its tags and gives any way/relation a representative point.
OVERPASS_QUERY = (
    "[out:json][timeout:180];"
    'nwr["man_made"="surveillance"]["surveillance:type"="ALPR"];'
    "out center tags;"
)

USER_AGENT = "warmap-fetch-flock/1.0 (+https://github.com/munzzyy/warmap)"
TIMEOUT_SECONDS = 240

# The largest response body we'll read. DeFlock's US file is ~37 MB and a
# worldwide Overpass body is ~54 MB, so this is generous but still bounds a
# runaway stream.
MAX_RESPONSE_BYTES = 200 * 1024 * 1024

# Below this many cameras, assume the source is broken/partial rather than
# trust it. Loud failure beats overwriting a good snapshot with a bad one.
# The US set alone is ~137k, so a few hundred means something went wrong.
MIN_CAMERAS = 1000

ATTRIBUTION = (
    "Camera locations © OpenStreetMap contributors (ODbL), "
    "compiled by the DeFlock project (deflock.org)."
)

# A DeFlock bulk feature that carries a `startDate` (a 4-digit year) or a
# highway-shield `ref` is a road way that someone mis-tagged as surveillance,
# not a camera, a known ~0.01% pollution in the source file. Dropped, counted.
_YEAR_RE = re.compile(r"^\d{4}$")
_HIGHWAY_REF_RE = re.compile(r"\b(?:US|I|SR|GA|TX|CA|FL|[A-Z]{2})[\s-]?\d", re.IGNORECASE)


class FetchError(RuntimeError):
    """The camera set could not be retrieved or didn't parse as expected."""


@dataclass
class FetchResult:
    cameras: list = field(default_factory=list)
    source_url: str = ""
    source_label: str = ""
    count: int = 0
    dropped: int = 0
    # Per-source failures that didn't sink the whole fetch (e.g. the Canada
    # file was unreachable but the US one succeeded). Surfaced to the user so a
    # partial refresh never looks like a complete one.
    errors: list = field(default_factory=list)


# --- HTTP ------------------------------------------------------------------

def _read_capped(resp) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = resp.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise FetchError(
                f"response exceeded {MAX_RESPONSE_BYTES} bytes, refusing to read further"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return _read_capped(resp)


def _http_post(url: str, query: str) -> bytes:
    data = ("data=" + urllib.parse.quote(query)).encode("ascii")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return _read_capped(resp)


def _decode_body(raw: bytes) -> str:
    """DeFlock's `.gz` files are usually plain JSON but occasionally actually
    gzipped; sniff the two-byte gzip magic rather than trusting the name.

    Decompression is streamed with the same size ceiling the wire read uses:
    a bare `gzip.decompress` would happily expand a small DEFLATE bomb into
    gigabytes, which is exactly the memory-exhaustion the wire cap exists to
    prevent. The cap has to apply to the decompressed size too, not just the
    bytes on the wire.
    """
    if raw[:2] == b"\x1f\x8b":
        out = bytearray()
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                while True:
                    chunk = gz.read(1024 * 1024)
                    if not chunk:
                        break
                    out += chunk
                    if len(out) > MAX_RESPONSE_BYTES:
                        raise FetchError(
                            "gzip body expanded past the size cap, refusing "
                            "(possible decompression bomb)"
                        )
        except (OSError, EOFError) as exc:
            raise FetchError(f"could not decompress gzip body: {exc}")
        raw = bytes(out)
    return raw.decode("utf-8")


# --- normalization ---------------------------------------------------------

def _coord(lat, lon) -> Optional[tuple[float, float]]:
    """The (lat, lon) as finite, in-range floats, or None. Also rejects the
    0,0 sentinel a broken export writes for a camera with no real position."""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def _looks_polluted(props: dict) -> bool:
    """A road way mis-tagged as surveillance, not a camera.

    The pollution in DeFlock's bulk file is highways carrying surveillance
    tags, and every one of them is an OSM `way`. Real cameras are nodes. Both
    the `startDate`-year tell and the highway-`ref` tell can legitimately
    appear on a real camera (a mapper recording an install year, or a `ref`),
    so neither is trusted on its own. They only condemn a feature that is
    already a `way`. A node is always kept.
    """
    if props.get("osmType") != "way":
        return False
    start = props.get("startDate")
    if isinstance(start, (str, int)) and _YEAR_RE.match(str(start)):
        return True
    ref = props.get("ref")
    if isinstance(ref, str) and _HIGHWAY_REF_RE.search(ref):
        return True
    return False


def _direction_string(props: dict) -> str:
    """DeFlock carries a single `direction` and/or a `directions` array; both
    become a single `;`-joined string that warmap.alpr.parse_direction reads."""
    parts: list[str] = []
    directions = props.get("directions")
    if isinstance(directions, list):
        parts.extend(str(d) for d in directions if d is not None and d != "")
    single = props.get("direction")
    if single is not None and single != "":
        parts.append(str(single))
    # De-dup while preserving order. A camera often has both fields agreeing.
    seen = set()
    uniq = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return ";".join(uniq)


def build_records_from_deflock(geojson: dict) -> tuple[list[dict], int]:
    """DeFlock bulk GeoJSON -> snapshot records, plus the count dropped as
    pollution. Pure, so it can be tested against a canned file."""
    features = geojson.get("features")
    if not isinstance(features, list):
        raise FetchError("DeFlock export had no 'features' array")
    records: list[dict] = []
    dropped = 0
    seen: set[str] = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geom = feature.get("geometry")
        if not isinstance(geom, dict):
            continue
        coords = geom.get("coordinates")
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        point = _coord(coords[1], coords[0])
        if point is None:
            continue
        lat, lon = point
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        if _looks_polluted(props):
            dropped += 1
            continue
        osm_id = f"{props.get('osmType', 'node')}/{props.get('osmId', '')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)
        rec = {"id": osm_id, "lat": round(lat, 6), "lon": round(lon, 6)}
        direction = _direction_string(props)
        if direction:
            rec["dir"] = direction
        brand = props.get("brand") or props.get("manufacturer")
        if brand:
            rec["mfg"] = str(brand)
        operator = props.get("operator")
        if operator:
            rec["op"] = str(operator)
        mount = props.get("mountType") or props.get("camera:mount")
        if mount:
            rec["mount"] = str(mount)
        zone = props.get("surveillanceZone") or props.get("surveillance:zone")
        if zone:
            rec["zone"] = str(zone)
        ref = props.get("ref")
        if ref:
            rec["ref"] = str(ref)
        records.append(rec)
    return records, dropped


def build_records_from_overpass(overpass_json: dict) -> list[dict]:
    """Overpass `{"elements": [...]}` -> snapshot records (fallback path)."""
    elements = overpass_json.get("elements")
    if not isinstance(elements, list):
        raise FetchError("Overpass response had no 'elements' array")
    records: list[dict] = []
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        if "lat" in element and "lon" in element:
            raw_lat, raw_lon = element.get("lat"), element.get("lon")
        else:
            center = element.get("center")
            if not isinstance(center, dict):
                continue
            raw_lat, raw_lon = center.get("lat"), center.get("lon")
        point = _coord(raw_lat, raw_lon)
        if point is None:
            continue
        lat, lon = point
        tags = element.get("tags")
        if not isinstance(tags, dict):
            tags = {}
        osm_id = f"{element.get('type', 'node')}/{element.get('id', '')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)
        rec = {"id": osm_id, "lat": round(lat, 6), "lon": round(lon, 6)}
        direction = tags.get("direction") or tags.get("camera:direction")
        if direction:
            rec["dir"] = str(direction)
        manufacturer = tags.get("manufacturer") or tags.get("brand")
        if manufacturer:
            rec["mfg"] = str(manufacturer)
        operator = tags.get("operator")
        if operator:
            rec["op"] = str(operator)
        mount = tags.get("camera:mount")
        if mount:
            rec["mount"] = str(mount)
        zone = tags.get("surveillance:zone")
        if zone:
            rec["zone"] = str(zone)
        ref = tags.get("ref")
        if ref:
            rec["ref"] = str(ref)
        records.append(rec)
    return records


# --- top-level fetch -------------------------------------------------------

def fetch_from_deflock(http_get: Callable[[str], bytes] = _http_get) -> FetchResult:
    cameras: list[dict] = []
    dropped = 0
    used_urls = []
    used_labels = []
    seen: set[str] = set()
    errors = []
    for label, url in DEFLOCK_BULK_URLS:
        try:
            raw = http_get(url)
            geojson = json.loads(_decode_body(raw))
            recs, dr = build_records_from_deflock(geojson)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError,
                FetchError) as exc:
            errors.append(f"{label} ({url}): {exc}")
            continue
        # US and Canada can in principle both list a border camera; dedup by id
        # across the two files.
        for rec in recs:
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            cameras.append(rec)
        dropped += dr
        used_urls.append(url)
        used_labels.append(label)
    if len(cameras) < MIN_CAMERAS:
        raise FetchError(
            "DeFlock bulk export unusable ("
            + (f"{len(cameras)} cameras; " if cameras else "")
            + "; ".join(errors) + ")"
        )
    # Name only the countries that actually loaded, so a partial refresh (one
    # country's file down) never claims coverage it doesn't have.
    coverage = " + ".join(used_labels) if used_labels else "unknown"
    return FetchResult(
        cameras=cameras, source_url=", ".join(used_urls),
        source_label=f"DeFlock bulk export (data.dontgetflocked.com), {coverage}",
        count=len(cameras), dropped=dropped, errors=errors,
    )


def fetch_from_overpass(
    endpoints: tuple = OVERPASS_ENDPOINTS,
    http_post: Callable[[str, str], bytes] = _http_post,
) -> FetchResult:
    errors = []
    for url in endpoints:
        try:
            raw = http_post(url, OVERPASS_QUERY)
            payload = json.loads(raw.decode("utf-8"))
            cameras = build_records_from_overpass(payload)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError,
                FetchError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        if len(cameras) < MIN_CAMERAS:
            errors.append(f"{url}: only {len(cameras)} cameras, partial/timed-out")
            continue
        return FetchResult(
            cameras=cameras, source_url=url,
            source_label="OpenStreetMap via Overpass (worldwide)",
            count=len(cameras),
        )
    raise FetchError("no Overpass endpoint returned a usable set: " + "; ".join(errors))


def fetch_cameras(prefer: str = "deflock") -> FetchResult:
    """Fetch and normalize the current ALPR set. Tries DeFlock's bulk export
    first (reliable, US+CA, the deflock.org dataset), then Overpass worldwide.
    `prefer="overpass"` flips the order."""
    order = [fetch_from_deflock, fetch_from_overpass]
    if prefer == "overpass":
        order.reverse()
    errors = []
    for fn in order:
        try:
            return fn()
        except FetchError as exc:
            errors.append(str(exc))
    raise FetchError("both sources failed: " + " | ".join(errors))


_COUNT_RE = re.compile(rb'"count"\s*:\s*(\d+)')


def _existing_count(path: Path) -> Optional[int]:
    """The camera count of a snapshot already on disk, read cheaply from the
    file's head (the count field is written before the cameras array). None if
    there's no readable count."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    m = _COUNT_RE.search(head)
    return int(m.group(1)) if m else None


def write_snapshot(result: FetchResult, path: Path, generated: str = "",
                   force: bool = False) -> int:
    """Write the snapshot atomically. Returns the byte size written.

    Refuses to replace a healthy snapshot with a much smaller one unless
    `force` is set. A fetch that comes back with less than half the cameras
    the current file has is far more likely a partial/degraded pull than a real
    collapse in the dataset, and silently overwriting would throw away good
    data. The caller turns this into a `--force` prompt.
    """
    path = Path(path)
    if path.exists() and not force:
        prev = _existing_count(path)
        if prev is not None and prev >= MIN_CAMERAS and result.count < prev * 0.5:
            raise FetchError(
                f"refusing to overwrite {path.name}: the new set has {result.count} "
                f"cameras but the current one has {prev}. That large a drop is more "
                "likely a partial fetch than a real change. Re-run with force to "
                "overwrite anyway."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": result.source_label,
        "source_url": result.source_url,
        "attribution": ATTRIBUTION,
        "generated": generated,
        "count": result.count,
        "cameras": result.cameras,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return len(body.encode("utf-8"))
