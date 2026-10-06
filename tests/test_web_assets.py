"""Vendored Leaflet assets must be the real libraries, not empty files or a
404 page saved by mistake, and the encryption colors baked into map.js
must exactly match warmap.models.ENC_COLORS, the Python-side source of
truth, or the legend/markers and the filter panel's swatches would drift
apart silently.
"""

from __future__ import annotations

import re

from warmap import config
from warmap.models import ALPR_COLOR, ENC_COLORS, TYPE_COLORS, TYPE_GLYPHS, TYPE_LABELS
from warmap.radio import RISK_COLORS

VENDOR = config.WEB_DIR / "vendor"


def _js_object(name: str) -> dict:
    """Pull a flat `var NAME = { "k": "v", ... };` literal out of map.js."""
    content = (config.WEB_DIR / "map.js").read_text(encoding="utf-8")
    match = re.search(r"var " + name + r" = \{(.*?)\};", content, re.DOTALL)
    assert match, f"could not find {name} object literal in map.js"
    return dict(re.findall(r'"([^"]+)":\s*"([^"]*)"', match.group(1)))


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def test_leaflet_js_present_and_real():
    path = VENDOR / "leaflet" / "leaflet.js"
    assert path.exists()
    content = _read(path)
    assert len(content) > 50_000  # the real bundle is ~140KB minified
    assert "Leaflet" in content
    assert "1.9.4" in content
    # Minified UMD bundle. Public "L.xxx" call sites don't survive
    # minification (internal refs use a short local var, not "L"), but
    # these core class names are string literals Leaflet emits as-is
    # (CSS class names, error messages), so they're a reliable real-content check.
    assert "LatLng" in content
    assert "TileLayer" in content
    assert "leaflet-container" in content


def test_leaflet_css_present_and_real():
    path = VENDOR / "leaflet" / "leaflet.css"
    assert path.exists()
    content = _read(path)
    assert len(content) > 5_000
    assert ".leaflet-container" in content


def test_leaflet_marker_images_present_and_nonempty():
    images_dir = VENDOR / "leaflet" / "images"
    for name in ("marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"):
        path = images_dir / name
        assert path.exists(), f"missing {name}"
        data = path.read_bytes()
        assert len(data) > 100
        assert data[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG signature, not an HTML 404 page


def test_markercluster_js_present_and_real():
    path = VENDOR / "leaflet.markercluster" / "leaflet.markercluster.js"
    assert path.exists()
    content = _read(path)
    assert len(content) > 10_000
    assert "MarkerClusterGroup" in content


def test_markercluster_css_present():
    for name in ("MarkerCluster.css", "MarkerCluster.Default.css"):
        path = VENDOR / "leaflet.markercluster" / name
        assert path.exists()
        assert len(_read(path)) > 100


def test_leaflet_heat_js_present_and_real():
    path = VENDOR / "leaflet.heat" / "leaflet-heat.js"
    assert path.exists()
    content = _read(path)
    assert len(content) > 1_000
    assert "heatLayer" in content or "HeatLayer" in content


def test_map_html_references_vendored_assets_not_a_cdn():
    content = _read(config.MAP_HTML)
    assert "cdn" not in content.lower()
    assert "http://" not in content and "https://" not in content
    assert "vendor/leaflet/leaflet.js" in content
    assert "vendor/leaflet.markercluster/leaflet.markercluster.js" in content
    assert "vendor/leaflet.heat/leaflet-heat.js" in content


def test_map_js_enc_colors_match_python_source_of_truth():
    assert _js_object("ENC_COLORS") == ENC_COLORS


def test_map_js_type_colors_match_python_source_of_truth():
    assert _js_object("TYPE_COLORS") == TYPE_COLORS


def test_map_js_type_labels_match_python_source_of_truth():
    assert _js_object("TYPE_LABELS") == TYPE_LABELS


def test_map_js_type_glyphs_match_python_source_of_truth():
    assert _js_object("TYPE_GLYPHS") == TYPE_GLYPHS


def test_map_js_risk_colors_match_radio_source_of_truth():
    """The Sub-GHz legend and the stats panel bars have to agree on what
    "fixed code" looks like, or the two panels contradict each other."""
    assert _js_object("RISK_COLORS") == RISK_COLORS


def test_map_js_alpr_color_matches_python_source_of_truth():
    """The camera overlay's color is defined once in models.py and mirrored in
    map.js; the dot, the wedge, the cluster and the legend swatch all read from
    it, so a drift would recolor the overlay inconsistently."""
    content = _read(config.WEB_DIR / "map.js")
    match = re.search(r'var ALPR_COLOR = "(#[0-9a-fA-F]{6})";', content)
    assert match, "ALPR_COLOR not found in map.js"
    assert match.group(1) == ALPR_COLOR


def test_map_js_exposes_the_full_python_facing_api():
    """mapview.py calls these by name through runJavaScript, where a typo is
    a silent no-op rather than an error."""
    content = _read(config.WEB_DIR / "map.js")
    for function in (
        "warmapLoadData", "warmapSetTrack", "warmapSetHeatmapVisible",
        "warmapSetTypeVisible", "warmapFocus", "warmapFitToData",
        "warmapSetAlpr", "warmapSetAlprVisible", "warmapSetView",
    ):
        assert f"window.{function} =" in content, f"map.js is missing {function}"


def test_mapview_only_calls_functions_map_js_defines():
    mapview = _read(config.REPO_ROOT / "warmap" / "ui" / "mapview.py")
    map_js = _read(config.WEB_DIR / "map.js")
    called = set(re.findall(r"window\.(warmap\w+)\(", mapview))
    assert called, "no map API calls found in mapview.py"
    for name in called:
        assert f"window.{name} =" in map_js, f"mapview.py calls undefined {name}"
