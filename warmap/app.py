"""warmap desktop app bootstrap: PySide6 native chrome around an embedded
Leaflet map (QWebEngineView). No daemon, no socket, no autostart: `warmap`
just opens a window.

    python3 -m warmap.app                  # launch
    QT_QPA_PLATFORM=offscreen python3 -c "from warmap.app import build_app; build_app()"
"""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from warmap import config
from warmap.tileproxy import TileCacheProxy
from warmap.ui import theme
from warmap.ui.mainwindow import MainWindow

ICON_PATH = config.WEBAPP_DIR / "icon-512.png"


def configure_app_identity(app: QApplication) -> None:
    """Give this process's windows a real Wayland/X11 app_id instead of the
    default "python3". Must exactly match the installed warmap.desktop's
    basename (warmap.desktop -> "warmap") or KDE's task manager can't
    associate the window with a taskbar pin.
    """
    app.setApplicationName("warmap")
    app.setApplicationDisplayName("warmap")
    app.setDesktopFileName("warmap")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))


def build_app():
    """Construct the QApplication, tile-cache proxy, and main window
    without entering the event loop or loading data. Shared by main() and
    the offscreen smoke test/tests. Callers that want the window populated
    call `window.load_initial_data()` (or `window.import_paths(...)`)
    themselves; build_app() alone is safe to call repeatedly in tests
    because QApplication.instance() is reused.
    """
    config.ensure_data_dir()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    configure_app_identity(app)
    app.setStyleSheet(theme.stylesheet())

    proxy = TileCacheProxy()
    proxy.start()

    window = MainWindow(tile_port=proxy.port)
    window.tile_proxy = proxy  # keep a live reference for the window's lifetime
    app.aboutToQuit.connect(proxy.stop)
    return app, window


def main() -> int:
    app, window = build_app()
    window.load_initial_data()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
