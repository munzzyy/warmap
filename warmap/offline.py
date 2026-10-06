"""Export the whole app and its data as one HTML file.

The LAN server is the good path while the PC is on and the phone is home. This
is the other half: a single file you save to the phone once and open from its
downloads forever after, with no server, no wifi and no warmap running
anywhere. Everything is inlined: Leaflet, the shared renderer, the mobile UI,
and the records themselves, so there is nothing left to fetch.

Two honest limits, both stated in the file's own banner rather than discovered
later:

* **The map background needs the internet.** Tiles are not embedded. Bundling a
  region's tiles would mean bulk-downloading them, which OpenStreetMap's tile
  policy specifically prohibits, so the file draws your records on a plain
  background when offline and pulls a real basemap in when there's a
  connection. Your data is what's embedded, and that part always works.
* **It's a snapshot.** Nothing in the file syncs. The date it was made is in
  the menu, and making a new one is a menu item away.

The camera overlay defaults to only what's near your own captures. All 137k
would make a 10 MB file mostly consisting of cameras in states you have never
driven through.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from warmap import config

# How far from your own records a camera has to be before it's left out of a
# default export. Wide enough to cover the drive home, small enough that the
# file stays a sensible size.
DEFAULT_CAMERA_RADIUS_M = 25_000

# Vendored libraries the app needs, in load order.
_VENDOR_JS = (
    "vendor/leaflet/leaflet.js",
    "vendor/leaflet.markercluster/leaflet.markercluster.js",
    "vendor/leaflet.heat/leaflet-heat.js",
)
_VENDOR_CSS = (
    "vendor/leaflet/leaflet.css",
    "vendor/leaflet.markercluster/MarkerCluster.css",
    "vendor/leaflet.markercluster/MarkerCluster.Default.css",
)


@dataclass
class OfflineBundle:
    html: str
    bytes_written: int = 0
    records: int = 0
    cameras: int = 0


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _inline_leaflet_images(css: str, base: Path) -> str:
    """Leaflet's CSS points at marker PNGs by relative URL. In a single file
    there is nowhere for those to live, so they become data: URIs."""
    for name in ("marker-icon.png", "marker-icon-2x.png", "marker-shadow.png",
                 "layers.png", "layers-2x.png"):
        image = base / "vendor" / "leaflet" / "images" / name
        if not image.exists():
            continue
        mime = mimetypes.guess_type(name)[0] or "image/png"
        data = base64.b64encode(image.read_bytes()).decode("ascii")
        css = css.replace(f"images/{name}", f"data:{mime};base64,{data}")
    return css


def build(
    records_geojson: dict,
    cameras_transfer: Optional[dict] = None,
    tracks_geojson: Optional[dict] = None,
    session: Optional[dict] = None,
    web_dir: Optional[Path] = None,
    webapp_dir: Optional[Path] = None,
) -> OfflineBundle:
    """Assemble the single-file app. Pure: no I/O beyond reading warmap's own
    assets, so a test can build one and inspect the string."""
    web = Path(web_dir) if web_dir else config.WEB_DIR
    app = Path(webapp_dir) if webapp_dir else config.WEBAPP_DIR

    css_parts = []
    for rel in _VENDOR_CSS:
        css_parts.append(_inline_leaflet_images(_read(web / rel), web))
    css_parts.append(_read(web / "map.css"))
    css_parts.append(_read(app / "app.css"))

    js_parts = [_read(web / rel) for rel in _VENDOR_JS]
    js_parts.append(_read(web / "map.js"))

    payload = {
        "records": records_geojson,
        "cameras": cameras_transfer or {"fields": [], "cameras": [], "count": 0},
        "tracks": tracks_geojson,
        "session": session or {},
    }

    # The shell is the mobile app's own markup with the network parts removed:
    # there is no server to sync from and no service worker to register.
    body = _read(app / "index.html")
    body = _extract_body(body)

    embedded = _embed_json(payload)
    app_js = _read(app / "app.js")

    html = _TEMPLATE.format(
        css="\n".join(css_parts),
        body=body,
        vendor_js="\n".join(js_parts),
        embedded=embedded,
        app_js=app_js,
    )
    return OfflineBundle(
        html=html,
        records=len((records_geojson or {}).get("features", [])),
        cameras=len((cameras_transfer or {}).get("cameras", [])),
    )


def _embed_json(payload: dict) -> str:
    """JSON safe to paste inside an inline <script> block.

    An SSID is text a stranger chose and broadcast, and it ends up in this
    file verbatim. `json.dumps` escapes quotes but not `<`, so a network named
    `</script><img src=x onerror=...>` closes the script tag early and the rest
    of the "name" becomes live markup in the exported page: a capture turning
    into script execution the next time the file is opened. Escaping the three
    characters that can start an HTML token closes that off, and since `\\uXXXX`
    is valid JSON the payload still parses identically.

    U+2028 and U+2029 go too: they're legal in JSON strings but are line
    terminators in older JavaScript parsers, which would break the assignment.
    """
    text = json.dumps(payload, separators=(",", ":"))
    return (text
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _extract_body(html: str) -> str:
    """Everything between <body> and </body>, minus the script tags: the
    single-file build supplies its own scripts, inlined and in order."""
    start = html.find("<body>")
    end = html.find("</body>")
    if start == -1 or end == -1:
        return html
    inner = html[start + len("<body>"):end]
    out_lines = []
    for line in inner.splitlines():
        if line.strip().startswith("<script"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def write(bundle: OfflineBundle, path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bundle.html.encode("utf-8")
    path.write_bytes(data)
    bundle.bytes_written = len(data)
    return len(data)


def nearby_cameras(cameras: Iterable, records_geojson: dict,
                   tracks_geojson: Optional[dict] = None,
                   meters: float = DEFAULT_CAMERA_RADIUS_M) -> list:
    """The cameras worth carrying: the ones near where you actually were.

    With nothing to measure against (an export whose records all failed to
    place), "near your captures" has no meaning, so this returns nothing
    rather than everything. Falling back to the whole set would turn the one
    case with no records to show into the single largest file the exporter can
    produce (~10 MB of cameras, zero of your own data), which is the opposite
    of what someone asking for a phone copy wants. `--all-cameras` is how you
    ask for all of them on purpose.
    """
    from warmap import alpr

    points: list[tuple[float, float]] = []
    for feature in (records_geojson or {}).get("features", []):
        coords = ((feature or {}).get("geometry") or {}).get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            points.append((coords[1], coords[0]))
    for feature in ((tracks_geojson or {}).get("features") or []):
        coords = ((feature or {}).get("geometry") or {}).get("coordinates") or []
        for point in coords:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append((point[1], point[0]))

    if not points:
        return []
    return alpr.cameras_near(list(cameras), points, meters=meters)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#0d0d0d">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="warmap">
<title>warmap</title>
<style>
{css}
</style>
</head>
<body>
{body}
<script>
{vendor_js}
</script>
<script>
// The whole dataset, inlined. app.js sees this and skips every network call.
window.WARMAP_EMBEDDED = {embedded};
</script>
<script>
{app_js}
</script>
</body>
</html>
"""
