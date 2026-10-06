"""Taskbar pinnability: the main window must carry a real app_id ("warmap",
matching the installed warmap.desktop) instead of the default "python3".

Offscreen-safe: QT_QPA_PLATFORM is forced to "offscreen" before PySide6 is
imported so this runs the same on a machine with no display attached.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from warmap.app import ICON_PATH, configure_app_identity
from warmap.ui.mainwindow import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class TestConfigureAppIdentity(unittest.TestCase):
    def test_desktop_file_name_matches_installed_desktop_entry(self):
        app = _app()
        configure_app_identity(app)
        self.assertEqual(app.desktopFileName(), "warmap")

    def test_application_name_is_set(self):
        app = _app()
        configure_app_identity(app)
        self.assertEqual(app.applicationName(), "warmap")

    def test_window_icon_is_set_when_icon_file_exists(self):
        app = _app()
        configure_app_identity(app)
        if ICON_PATH.exists():
            self.assertFalse(app.windowIcon().isNull())

    def test_survives_a_missing_icon_file_without_raising(self):
        app = _app()
        import warmap.app as app_mod
        from unittest import mock
        with mock.patch.object(app_mod, "ICON_PATH", Path("/nonexistent/warmap.png")):
            configure_app_identity(app)  # must not raise


class TestMainWindowConstructsOffscreen(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow(tile_port=0)

    def tearDown(self):
        # deleteLater() only, NOT close(): closeEvent() persists window
        # geometry to the real ~/.config/warmap/settings.json, which a test
        # must never touch (see test_ui_smoke.py's env-isolated variant for
        # the one place that IS exercising save/restore on purpose).
        self.window.deleteLater()

    def test_window_title(self):
        self.assertEqual(self.window.windowTitle(), "warmap")

    def test_docks_present(self):
        self.assertIsNotNone(self.window.filter_dock)
        self.assertIsNotNone(self.window.stats_dock)

    def test_map_view_is_central_widget_descendant(self):
        self.assertIsNotNone(self.window.map_view)


if __name__ == "__main__":
    unittest.main()
