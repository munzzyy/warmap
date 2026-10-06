"""Headless smoke tests for the dock widgets and the main window's
data-lifecycle wiring (load sample -> filter -> import -> re-filter). Not
pixel-level UI tests, just proof that constructing and driving these
widgets offscreen doesn't raise, and that filtering/importing actually
updates state end to end.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QDockWidget

from warmap import config
from warmap.filters import FilterState
from warmap.models import Sighting
from warmap.stats import Stats
from warmap.ui.filterpanel import FilterPanel
from warmap.ui.mainwindow import MainWindow
from warmap.ui.mapview import MapView
from warmap.ui.recordtable import RecordTable
from warmap.ui.statspanel import BarChart, StatsPanel


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class TestFilterPanel(unittest.TestCase):
    def setUp(self):
        _app()
        self.panel = FilterPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_default_state_matches_filters_default(self):
        state = self.panel.current_state()
        self.assertEqual(state.min_rssi, -100)
        self.assertFalse(state.open_only)
        self.assertIsNone(state.channels)

    def test_search_text_reflected_in_state(self):
        self.panel._search.setText("coffee")
        state = self.panel.current_state()
        self.assertEqual(state.text, "coffee")

    def test_unchecking_an_encryption_bucket_removes_it(self):
        self.panel._enc_checks["Open"].setChecked(False)
        state = self.panel.current_state()
        self.assertNotIn("Open", state.enc_buckets)

    def test_set_available_channels_then_uncheck_one(self):
        self.panel.set_available_channels([1, 6, 11])
        self.assertEqual(self.panel._channel_list.count(), 3)
        # unchecking channel 6 (index 1, since sorted) should narrow state.channels
        item = self.panel._channel_list.item(1)
        from PySide6.QtCore import Qt
        item.setCheckState(Qt.CheckState.Unchecked)
        state = self.panel.current_state()
        self.assertIsNotNone(state.channels)
        self.assertNotIn(6, state.channels)

    def test_open_only_toggle_reflected(self):
        self.panel._open_only.setChecked(True)
        state = self.panel.current_state()
        self.assertTrue(state.open_only)


class TestStatsPanel(unittest.TestCase):
    def setUp(self):
        _app()
        self.panel = StatsPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_set_stats_with_empty_data_does_not_crash(self):
        self.panel.set_stats(Stats())

    def test_set_stats_with_real_data_does_not_crash(self):
        stats = Stats(
            total=10, unique_ssids=5, open_count=2, wifi_count=8, ble_count=2,
            enc_breakdown={"Open": 2, "WPA2": 8}, channel_breakdown={"6": 5, "unknown": 5},
            first_seen_min="2024-01-01 00:00:00", first_seen_max="2024-01-01 01:00:00",
            bbox=(33.0, -112.0, 33.1, -111.9), area_km2=1.23,
        )
        self.panel.set_stats(stats)
        self.assertIn("10", self.panel._total.text())


class TestMainWindowDataFlow(unittest.TestCase):
    def setUp(self):
        _app()
        self.tmp_data_dir = _tmp_dir()
        os.environ["WARMAP_DATA_DIR"] = str(self.tmp_data_dir)
        os.environ["WARMAP_CONFIG_DIR"] = str(self.tmp_data_dir / "config")
        import importlib
        importlib.reload(config)
        self.window = MainWindow(tile_port=0)
        # QWidget.isVisible() reflects the whole ancestor chain, including
        # the top-level window. A banner's own setVisible(True) reads back
        # as False until the window itself has been shown, even offscreen.
        self.window.show()

    def tearDown(self):
        # deleteLater() only, never close(). See test_app_identity.py's
        # tearDown for why (closeEvent persists settings; this test isolates
        # both env vars but there's no reason to exercise the save path here).
        self.window.deleteLater()
        os.environ.pop("WARMAP_DATA_DIR", None)
        os.environ.pop("WARMAP_CONFIG_DIR", None)
        import importlib
        importlib.reload(config)

    def test_load_initial_data_falls_back_to_sample_when_store_empty(self):
        self.window.load_initial_data()
        self.assertTrue(self.window._using_sample)
        self.assertGreater(len(self.window._all_aps), 0)
        self.assertTrue(self.window._banner.isVisible())

    def test_import_paths_hides_sample_banner_and_persists(self):
        self.window.load_initial_data()
        csv_path = self.tmp_data_dir / "capture.csv"
        csv_path.write_text(
            "WigleWifi-1.4,appRelease=1.0\n"
            "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
            "AltitudeMeters,AccuracyMeters,Type\n"
            "AA:BB:CC:DD:EE:01,ImportedNet,[ESS],2024-01-01 00:00:00,6,-50,33.0,-112.0,340,5,WIFI\n"
        )
        count = self.window.import_paths([csv_path])
        self.assertEqual(count, 1)
        self.assertFalse(self.window._using_sample)
        self.assertFalse(self.window._banner.isVisible())
        self.assertTrue(any(ap.ssid == "ImportedNet" for ap in self.window._all_aps))

    def test_filters_changed_updates_stats_panel(self):
        self.window.load_initial_data()
        total_before = self.window.stats_panel._total.text()
        self.assertNotEqual(total_before, "-")

    def test_current_filtered_respects_filter_panel(self):
        self.window.load_initial_data()
        all_count = len(self.window.current_filtered())
        self.window.filter_panel._open_only.setChecked(True)
        open_count = len(self.window.current_filtered())
        self.assertLessEqual(open_count, all_count)


class TestBarChartLabels(unittest.TestCase):
    """The stats bars draw their own labels, so a label wider than the column
    is clipped mid-word with no ellipsis: "WPA2/3-mixed" rendered as
    "WPA2/3-mixe", which reads as a different encryption bucket."""

    def setUp(self):
        _app()
        self.chart = BarChart(label_width=90)
        self.chart.resize(320, 120)

    def tearDown(self):
        self.chart.deleteLater()

    def test_column_grows_to_fit_the_longest_label(self):
        self.chart.set_rows([("WPA2/3-mixed", 60, "#2f8f6f"), ("WPA2", 948, "#2f8f3f")])
        metrics = QFontMetrics(self.chart.font())
        needed = metrics.horizontalAdvance("WPA2/3-mixed")
        self.assertGreaterEqual(self.chart._column_width(metrics), needed)

    def test_short_labels_keep_the_column_narrow(self):
        self.chart.set_rows([("WPA2", 948, "#2f8f3f")])
        metrics = QFontMetrics(self.chart.font())
        self.assertEqual(self.chart._column_width(metrics), 90)

    def test_labels_never_take_more_than_half_the_width(self):
        self.chart.set_rows([("a" * 200, 1, "#2f8f3f")])
        metrics = QFontMetrics(self.chart.font())
        self.assertLessEqual(self.chart._column_width(metrics), self.chart.width() * 0.55)


class TestRecordTableOrder(unittest.TestCase):
    def setUp(self):
        _app()
        self.table = RecordTable()

    def tearDown(self):
        self.table.deleteLater()

    def test_opens_newest_first(self):
        """Without an explicit sort the view opened in reverse alphabetical
        order by name, which looks like a fault rather than a choice."""
        self.table.set_records([
            _sighting(bssid="AA:00", ssid="older", first_seen="2026-07-20 08:00:00"),
            _sighting(bssid="AA:01", ssid="zzz-newer", first_seen="2026-07-26 09:00:00"),
        ])
        first_row = self.table._proxy.index(0, 0).data()
        self.assertEqual(first_row, "zzz-newer")


class TestMapErrorRouting(unittest.TestCase):
    """A JavaScript failure in the map has to reach the window. A performance
    hint from the vendored heat layer must not, or the one line that matters
    gets lost in noise the user can do nothing about."""

    def setUp(self):
        _app()
        self.view = MapView(tile_port=0)
        self.seen: list = []
        self.view.mapError.connect(self.seen.append)

    def tearDown(self):
        self.view.deleteLater()

    def test_errors_are_surfaced(self):
        self.view._on_console_message("error", "coords is not defined")
        self.assertEqual(self.seen, ["coords is not defined"])

    def test_our_own_warnings_are_surfaced(self):
        self.view._on_console_message("warning", "warmap: 2 record(s) had no usable coordinate")
        self.assertEqual(len(self.seen), 1)

    def test_vendor_warnings_are_not_surfaced(self):
        self.view._on_console_message(
            "warning", "Canvas2D: Multiple readback operations using getImageData")
        self.assertEqual(self.seen, [])


class TestDockChrome(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow(tile_port=0)

    def tearDown(self):
        self.window.deleteLater()

    def test_docks_are_closable_but_not_floatable(self):
        """The float button draws as a bare diamond outline in every dock
        header and tears the panel into a window that's hard to get back."""
        for dock in (self.window.filter_dock, self.window.stats_dock,
                     self.window.records_dock):
            features = dock.features()
            self.assertTrue(features & QDockWidget.DockWidgetFeature.DockWidgetClosable)
            self.assertFalse(features & QDockWidget.DockWidgetFeature.DockWidgetFloatable)


def _sighting(**kw) -> Sighting:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="Net", auth_mode="", enc_bucket="WPA2",
        first_seen="2026-07-26 12:00:00", channel=6, rssi=-60, lat=43.0, lon=-90.0,
        altitude=None, accuracy=None, type="WIFI", times_seen=1,
    )
    defaults.update(kw)
    return Sighting(**defaults)


def _tmp_dir():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp(prefix="warmap-test-"))


class TestAlprOverlay(unittest.TestCase):
    """The DeFlock/Flock camera overlay is reference data held apart from the
    captures. These prove it loads, drives the filter/stats, and never
    leaks into the collected-captures store or export, which is the
    load-bearing one."""

    def setUp(self):
        import importlib
        import json

        _app()
        self.tmp_data_dir = _tmp_dir()
        os.environ["WARMAP_DATA_DIR"] = str(self.tmp_data_dir)
        os.environ["WARMAP_CONFIG_DIR"] = str(self.tmp_data_dir / "config")
        # A tiny snapshot with two cameras, one right on a capture point we'll
        # import, so the "near your captures" count has something to find.
        self.flock_path = self.tmp_data_dir / "flock.json"
        self.flock_path.write_text(json.dumps({
            "attribution": "Camera locations © OpenStreetMap contributors (ODbL).",
            "generated": "2026-08-30",
            "cameras": [
                {"id": "node/1", "lat": 33.0, "lon": -112.0, "dir": "90",
                 "mfg": "Flock Safety", "op": "Test PD"},
                {"id": "node/2", "lat": 40.0, "lon": -80.0, "mfg": "Genetec"},
            ],
        }), encoding="utf-8")
        os.environ["WARMAP_FLOCK_PATH"] = str(self.flock_path)
        importlib.reload(config)
        self.window = MainWindow(tile_port=0)
        self.window.show()

    def tearDown(self):
        import importlib
        self.window.deleteLater()
        for var in ("WARMAP_DATA_DIR", "WARMAP_CONFIG_DIR", "WARMAP_FLOCK_PATH"):
            os.environ.pop(var, None)
        # conftest set a default WARMAP_FLOCK_PATH; restore it so later tests
        # keep loading no cameras.
        os.environ["WARMAP_FLOCK_PATH"] = "/nonexistent/warmap-no-flock-snapshot.json"
        importlib.reload(config)

    def test_cameras_load_and_populate_the_filter_count(self):
        self.assertEqual(len(self.window._alpr_cameras), 2)
        self.assertIn("2", self.window.filter_panel._show_alpr.text())

    def test_toggling_the_overlay_off_does_not_crash(self):
        self.window.filter_panel._show_alpr.setChecked(False)
        self.window._refresh()
        self.assertFalse(self.window.filter_panel.current_state().show_alpr)

    def test_stats_panel_gets_a_camera_summary(self):
        # One camera is Flock, so the summary text says so.
        self.assertIn("Flock", self.window.stats_panel._alpr.text())

    def test_cameras_are_never_written_to_the_capture_store(self):
        from warmap import store
        csv_path = self.tmp_data_dir / "cap.csv"
        csv_path.write_text(
            "WigleWifi-1.4,appRelease=1.0\n"
            "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
            "AltitudeMeters,AccuracyMeters,Type\n"
            "AA:BB:CC:DD:EE:01,Net,[ESS],2024-01-01 00:00:00,6,-50,33.0,-112.0,340,5,WIFI\n",
            encoding="utf-8",
        )
        self.window.import_paths([csv_path])
        # Nothing of type ALPR in memory or on disk, and no camera osm id leaked.
        self.assertFalse(any(getattr(r, "type", "") == "ALPR" for r in self.window._all_aps))
        persisted = store.load(config.STORE_PATH)
        self.assertTrue(persisted)  # the capture did persist
        self.assertFalse(any(r.type == "ALPR" or r.bssid.startswith("node/")
                             for r in persisted))

    def test_near_captures_count_updates_after_an_import(self):
        csv_path = self.tmp_data_dir / "cap.csv"
        csv_path.write_text(
            "WigleWifi-1.4,appRelease=1.0\n"
            "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
            "AltitudeMeters,AccuracyMeters,Type\n"
            "AA:BB:CC:DD:EE:01,Net,[ESS],2024-01-01 00:00:00,6,-50,33.0,-112.0,340,5,WIFI\n",
            encoding="utf-8",
        )
        self.window.import_paths([csv_path])
        # node/1 sits on the imported capture, so it counts as "near".
        self.assertIn("within 500 m", self.window.stats_panel._alpr.text())


if __name__ == "__main__":
    unittest.main()
