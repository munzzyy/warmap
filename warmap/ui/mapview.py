"""QWebEngineView wrapper around web/map.html, the actual Leaflet map.
Python never touches the DOM; everything crosses through the small JS API
map.js exposes (warmapLoadData / warmapSetTrack / warmapSetHeatmapVisible /
warmapSetTypeVisible / warmapFocus / warmapFitToData), invoked here via
QWebEnginePage.runJavaScript.

Calls made before the page finishes loading are queued rather than dropped.
That's the normal case at startup, not an edge case: parsing and dedup finish
well before the web view is ready, so the first `load_data` almost always
arrives early.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from PySide6.QtCore import QUrl, QUrlQuery, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from warmap import config

_LEVEL_NAMES = {
    QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "info",
    QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "warning",
    QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "error",
}


class LoggingPage(QWebEnginePage):
    """A page that doesn't eat JavaScript errors.

    The default page discards console output, and an exception thrown inside
    a runJavaScript call is reported there and nowhere else. That combination
    hid a real bug for a whole release: a bad identifier threw part-way
    through building the markers, so a capture with 1,885 located records drew
    an empty map, with nothing on stderr and nothing in the window to say
    anything had gone wrong. Every message now goes to stderr, and errors are
    re-emitted so the window can tell the user out loud.
    """

    consoleMessage = Signal(str, str)  # level, text

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
        name = _LEVEL_NAMES.get(level, "info")
        where = source_id.rsplit("/", 1)[-1] if source_id else "map"
        print(f"warmap[js {name}] {where}:{line_number} {message}", file=sys.stderr)
        self.consoleMessage.emit(name, message)


class MapView(QWebEngineView):
    # Something in the map failed. The window puts this in the status bar,
    # because an empty map with no explanation is the worst possible outcome.
    mapError = Signal(str)

    def __init__(self, tile_port: int, parent=None):
        super().__init__(parent)
        self._ready = False
        self._pending: list[str] = []
        self._page = LoggingPage(self)
        self.setPage(self._page)
        self._page.consoleMessage.connect(self._on_console_message)
        self._configure_settings()
        self.loadFinished.connect(self._on_load_finished)
        self._load(tile_port)

    def _on_console_message(self, level: str, message: str) -> None:
        """Errors always reach the window. Warnings only do when they're ours:
        the vendored heat layer emits a Canvas2D performance hint on every
        draw, and putting that in the status bar trains you to ignore the one
        line that would have told you the map failed.
        """
        if level == "error" or message.startswith("warmap:"):
            self.mapError.emit(message)

    def _configure_settings(self) -> None:
        """map.html is loaded from disk over file://, and by default a
        file:// page is not allowed to request anything over the network,
        which silently includes the loopback tile proxy. Without this the map
        renders correctly in every respect except that it has no tiles under
        it, and nothing reports an error.
        """
        settings = self.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )

    def _load(self, tile_port: int) -> None:
        url = QUrl.fromLocalFile(str(config.MAP_HTML))
        query = QUrlQuery()
        query.addQueryItem("port", str(tile_port))
        # The page needs to know the app's theme so the basemap can be dark
        # under dark chrome. Passed in the URL rather than pushed later,
        # because the tiles start loading before any runJavaScript would land
        # and a bright flash then a re-tint looks broken.
        from warmap.ui import theme as theme_mod
        query.addQueryItem("theme", theme_mod.current().name)
        url.setQuery(query)
        self.load(url)

    def _on_load_finished(self, ok: bool) -> None:
        self._ready = bool(ok)
        if not ok:
            return
        queued, self._pending = self._pending, []
        for script in queued:
            self.page().runJavaScript(script)

    def _run(self, script: str) -> None:
        if not self._ready:
            self._pending.append(script)
            return
        self.page().runJavaScript(script)

    # --- public API -------------------------------------------------------

    def load_data(self, geojson: dict) -> None:
        """Push a GeoJSON FeatureCollection to the map."""
        self._run(f"window.warmapLoadData({json.dumps(geojson)});")

    def set_track(self, feature: Optional[dict]) -> None:
        """Draw a GPS track, or clear it when passed None."""
        if feature is None:
            self._run("window.warmapSetTrack(null);")
            return
        self._run(f"window.warmapSetTrack({json.dumps(feature)});")

    def set_heatmap_visible(self, visible: bool) -> None:
        self._run(f"window.warmapSetHeatmapVisible({str(bool(visible)).lower()});")

    def set_alpr(self, geojson: dict) -> None:
        """Push the ALPR/Flock camera overlay to the map. Sent once after the
        snapshot loads (and again on refresh), never per filter change: the
        whole set is large and visibility is toggled separately."""
        self._run(f"window.warmapSetAlpr({json.dumps(geojson)});")

    def set_alpr_visible(self, visible: bool) -> None:
        self._run(f"window.warmapSetAlprVisible({str(bool(visible)).lower()});")

    def set_type_visible(self, record_type: str, visible: bool) -> None:
        self._run(
            f"window.warmapSetTypeVisible({json.dumps(record_type)}, "
            f"{str(bool(visible)).lower()});"
        )

    def focus_record(self, record_type: str, bssid: str) -> None:
        """Center on one record and open its popup: how the records table
        drives the map when you click a row."""
        self._run(
            f"window.warmapFocus({json.dumps(record_type)}, {json.dumps(bssid)});"
        )

    def fit_to_data(self) -> None:
        self._run("window.warmapFitToData();")

    def set_view(self, lat: float, lon: float, zoom: int) -> None:
        """Center the map on a point: how a 'go to location' jump drives it."""
        self._run(f"window.warmapSetView({float(lat)}, {float(lon)}, {int(zoom)});")
