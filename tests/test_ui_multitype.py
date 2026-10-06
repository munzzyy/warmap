"""Headless tests for the UI added alongside multi-technology support: the
records table, the new filter controls, and the main window's track/placement
wiring. Offscreen only, not pixel tests, just proof that driving these
widgets works end to end and that the data actually reaches them.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Each MainWindow owns a QWebEngineView, and every one of those spawns its own
# GPU/renderer processes. A test file that builds several will exhaust them and
# hang, so force the single-process, no-GPU path used for headless runs.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from warmap import config
from warmap.models import (
    ALL_TYPES,
    GEO_NONE,
    GEO_TRACK,
    TYPE_BLE,
    TYPE_SUBGHZ,
    TYPE_WIFI,
    Sighting,
)
from warmap.radio import RISK_STATIC
from warmap.stats import Stats
from warmap.ui.filterpanel import FilterPanel
from warmap.ui.mainwindow import MainWindow, PlacementDialog
from warmap.ui.recordtable import RecordTable
from warmap.ui.statspanel import StatsPanel

SUB_FILE = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 12 34 56
"""

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="33.0" lon="-112.0"><time>2026-06-14T09:00:00</time></trkpt>
    <trkpt lat="33.01" lon="-112.01"><time>2026-06-14T09:10:00</time></trkpt>
  </trkseg></trk>
</gpx>
"""


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


def _s(**kw) -> Sighting:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="HomeNet", auth_mode="", enc_bucket="WPA2",
        first_seen="2026-06-14 09:00:00", channel=6, rssi=-60, lat=33.0, lon=-112.0,
        altitude=None, accuracy=None, type=TYPE_WIFI, times_seen=1,
        frequency=2437.0, meta={},
    )
    defaults.update(kw)
    return Sighting(**defaults)


class TestRecordTable(unittest.TestCase):
    def setUp(self):
        _app()
        self.table = RecordTable()

    def tearDown(self):
        self.table.deleteLater()

    def test_rows_appear(self):
        self.table.set_records([_s(), _s(bssid="B", type=TYPE_BLE)])
        self.assertEqual(self.table.view.model().rowCount(), 2)

    def test_unlocated_records_are_listed(self):
        """The whole reason this dock exists: the map can't show these."""
        self.table.set_records([_s(lat=None, lon=None, geo_source=GEO_NONE)])
        self.assertEqual(self.table.view.model().rowCount(), 1)

    def test_position_column_says_where_the_coordinate_came_from(self):
        self.table.set_records([
            _s(bssid="A"),
            _s(bssid="B", geo_source=GEO_TRACK),
            _s(bssid="C", lat=None, lon=None, geo_source=GEO_NONE),
        ])
        model = self.table.view.model()
        values = [model.index(r, 10).data() for r in range(3)]
        self.assertEqual(set(values), {"GPS fix", "from track", "none"})

    def test_notes_column_surfaces_type_specific_detail(self):
        self.table.set_records([
            _s(type=TYPE_SUBGHZ, meta={"code_type_label": "Fixed code, the same value every press"}),
        ])
        note = self.table.view.model().index(0, 11).data()
        self.assertIn("Fixed code", note)

    def test_numeric_columns_sort_numerically(self):
        """Sorting -9 and -70 as text puts them in the wrong order, which is
        exactly backwards for signal strength."""
        self.table.set_records([_s(bssid="A", rssi=-9), _s(bssid="B", rssi=-70)])
        self.table.view.sortByColumn(4, Qt.SortOrder.AscendingOrder)
        model = self.table.view.model()
        first = model.index(0, 2).data()
        self.assertEqual(first, "B")  # -70 is weaker, so it sorts first

    def test_selecting_a_mapped_row_emits_the_signal(self):
        received = []
        self.table.recordSelected.connect(lambda t, b: received.append((t, b)))
        self.table.set_records([_s(bssid="AA:BB:CC:DD:EE:01")])
        self.table.view.selectRow(0)
        self.assertEqual(received, [(TYPE_WIFI, "AA:BB:CC:DD:EE:01")])

    def test_selecting_an_unmapped_row_emits_nothing(self):
        received = []
        self.table.recordSelected.connect(lambda t, b: received.append((t, b)))
        self.table.set_records([_s(lat=None, lon=None, geo_source=GEO_NONE)])
        self.table.view.selectRow(0)
        self.assertEqual(received, [])

    def test_empty_set_clears(self):
        self.table.set_records([_s()])
        self.table.set_records([])
        self.assertEqual(self.table.view.model().rowCount(), 0)


class TestFilterPanelNewControls(unittest.TestCase):
    def setUp(self):
        _app()
        self.panel = FilterPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_every_record_type_has_a_checkbox(self):
        self.assertEqual(set(self.panel._type_checks), set(ALL_TYPES))

    def test_default_state_selects_everything(self):
        state = self.panel.current_state()
        self.assertEqual(state.types, frozenset(ALL_TYPES))
        self.assertIsNone(state.bands)
        self.assertIsNone(state.code_types)
        self.assertFalse(state.trackers_only)
        self.assertEqual(state.min_times_seen, 1)

    def test_unticking_a_type_narrows_the_state(self):
        self.panel._type_checks[TYPE_SUBGHZ].setChecked(False)
        self.assertNotIn(TYPE_SUBGHZ, self.panel.current_state().types)

    def test_unticking_a_band_makes_the_band_set_explicit(self):
        band = next(iter(self.panel._band_checks))
        self.panel._band_checks[band].setChecked(False)
        state = self.panel.current_state()
        self.assertIsNotNone(state.bands)
        self.assertNotIn(band, state.bands)

    def test_unticking_a_code_type_makes_it_explicit(self):
        self.panel._code_checks[RISK_STATIC].setChecked(False)
        state = self.panel.current_state()
        self.assertIsNotNone(state.code_types)
        self.assertNotIn(RISK_STATIC, state.code_types)

    def test_quick_toggles_reach_the_state(self):
        self.panel._trackers_only.setChecked(True)
        self.panel._randomized_only.setChecked(True)
        self.panel._located_only.setChecked(True)
        state = self.panel.current_state()
        self.assertTrue(state.trackers_only)
        self.assertTrue(state.randomized_only)
        self.assertTrue(state.located_only)

    def test_type_counts_disable_absent_types(self):
        self.panel.set_type_counts({TYPE_WIFI: 12})
        self.assertTrue(self.panel._type_checks[TYPE_WIFI].isEnabled())
        self.assertFalse(self.panel._type_checks[TYPE_SUBGHZ].isEnabled())
        self.assertEqual(self.panel._type_counts[TYPE_WIFI].text(), "12")

    def test_reset_restores_defaults(self):
        self.panel._search.setText("something")
        self.panel._trackers_only.setChecked(True)
        self.panel._rssi_slider.setValue(-50)
        self.panel._min_seen.setValue(9)
        self.panel._type_checks[TYPE_SUBGHZ].setChecked(False)

        self.panel.reset()

        state = self.panel.current_state()
        self.assertEqual(state.text, "")
        self.assertFalse(state.trackers_only)
        self.assertEqual(state.min_rssi, -100)
        self.assertEqual(state.min_times_seen, 1)
        self.assertEqual(state.types, frozenset(ALL_TYPES))

    def test_open_only_disables_the_encryption_boxes(self):
        self.panel._open_only.setChecked(True)
        self.assertFalse(self.panel._enc_checks["WPA2"].isEnabled())
        self.panel._open_only.setChecked(False)
        self.assertTrue(self.panel._enc_checks["WPA2"].isEnabled())


class TestStatsPanelNewSections(unittest.TestCase):
    def setUp(self):
        _app()
        self.panel = StatsPanel()

    def tearDown(self):
        self.panel.deleteLater()

    def test_full_stats_render(self):
        stats = Stats(
            total=20, unique_ssids=10, open_count=2, wifi_count=15, ble_count=4,
            enc_breakdown={"Open": 2, "WPA2": 13},
            channel_breakdown={"6": 10, "unknown": 10},
            type_breakdown={TYPE_WIFI: 15, TYPE_BLE: 4, TYPE_SUBGHZ: 1},
            band_breakdown={"2.4 GHz": 19, "Sub-GHz": 1},
            code_type_breakdown={"static": 1},
            top_vendors=[("Apple, Inc.", 4)],
            tracker_count=2, tracker_breakdown={"Tile": 2},
            randomized_count=3, located_count=19, unlocated_count=1,
            track_placed_count=1, hidden_ssid_count=2,
            subghz_frequencies={"433.92": 1},
            first_seen_min="2026-06-14 09:00:00", first_seen_max="2026-06-14 09:45:00",
            bbox=(33.0, -112.0, 33.1, -111.9), area_km2=1.23,
        )
        self.panel.set_stats(stats)
        self.assertIn("20", self.panel._total.text())
        self.assertIn("19", self.panel._mapped.text())
        self.assertIn("Tile", self.panel._trackers.text())
        self.assertIn("Apple", self.panel._vendors.text())
        self.assertIn("433.92", self.panel._frequencies.text())

    def test_empty_stats_do_not_crash(self):
        self.panel.set_stats(Stats())
        self.assertEqual(self.panel._total.text(), "0")

    def test_track_summary_is_settable(self):
        self.panel.set_track_summary("2 track(s), 100 points, 5.00 km")
        self.assertIn("5.00 km", self.panel._track.text())

    def test_track_summary_says_none_when_blank(self):
        self.panel.set_track_summary("")
        self.assertIn("none", self.panel._track.text())


class TestMainWindowMultiFormat(unittest.TestCase):
    """One window shared across the class. Building a MainWindow per test
    means a QWebEngineView per test, and enough of those in one process will
    exhaust the renderer pool and hang. Each test resets the dataset instead.
    """

    @classmethod
    def setUpClass(cls):
        _app()
        cls.tmp = Path(tempfile.mkdtemp(prefix="warmap-mt-"))
        os.environ["WARMAP_DATA_DIR"] = str(cls.tmp / "data")
        os.environ["WARMAP_CONFIG_DIR"] = str(cls.tmp / "config")
        import importlib
        importlib.reload(config)
        cls.window = MainWindow(tile_port=0)
        cls.window.show()

    @classmethod
    def tearDownClass(cls):
        cls.window.deleteLater()
        os.environ.pop("WARMAP_DATA_DIR", None)
        os.environ.pop("WARMAP_CONFIG_DIR", None)
        import importlib
        importlib.reload(config)

    def setUp(self):
        from warmap import store
        store.clear(config.STORE_PATH)
        self.window._tracks = []
        self.window.filter_panel.reset()
        self.window.load_initial_data()

    def test_sample_load_includes_flipper_records_and_a_track(self):
        """First run should demonstrate placement, not just describe it."""
        types = {r.type for r in self.window._all_aps}
        self.assertIn(TYPE_WIFI, types)
        self.assertIn(TYPE_SUBGHZ, types)
        self.assertTrue(self.window.active_tracks())
        placed = [r for r in self.window._all_aps if r.geo_source == GEO_TRACK]
        self.assertTrue(placed, "sample should show track-based placement working")

    def test_sample_is_never_written_to_the_store(self):
        from warmap import store
        self.assertTrue(self.window._using_sample)
        self.assertEqual(store.load(config.STORE_PATH), [])

    def test_importing_a_flipper_file_then_a_track_places_it(self):
        """The normal order of operations: captures first, track afterwards.
        Uses a date outside the bundled sample's track so the first import is
        genuinely unplaceable."""
        self.window._tracks = []
        sub = self.tmp / "Garage_20270301-090500.sub"
        sub.write_text(SUB_FILE)
        self.window.import_paths([sub])

        def garage():
            return [r for r in self.window._all_aps
                    if r.type == TYPE_SUBGHZ and r.ssid.startswith("Garage")][0]

        self.assertEqual(garage().geo_source, GEO_NONE)

        track = self.tmp / "track.gpx"
        track.write_text(GPX.replace("2026-06-14", "2027-03-01"))
        self.window.import_paths([track])

        self.assertEqual(garage().geo_source, GEO_TRACK)
        self.assertTrue(garage().has_location)

    def test_sample_track_actually_reaches_the_map(self):
        """The sample's whole point is showing timestamp placement working, and
        the track is what makes that legible. Holding the track in a list the
        map is never told about looks identical to having no track."""
        sent = []
        original = self.window.map_view.set_track
        self.window.map_view.set_track = lambda f: sent.append(f)
        try:
            self.window.load_initial_data()
        finally:
            self.window.map_view.set_track = original

        payloads = [f for f in sent if f]
        self.assertTrue(payloads, "no track was ever pushed to the map")
        self.assertTrue(payloads[-1]["features"])
        coords = payloads[-1]["features"][0]["geometry"]["coordinates"]
        self.assertGreater(len(coords), 2)

    def test_sample_track_is_dropped_once_real_data_arrives(self):
        """The worst possible failure this app could have: a real capture
        placed on the sample's route. The sample track covers 2026-06-14
        09:05-09:47, so a real Flipper file stamped inside that window would
        land in Phoenix if the sample track were still in play."""
        self.assertTrue(self.window._sample_tracks)
        self.assertTrue(self.window.active_tracks())

        sub = self.tmp / "Real_20260614-091600.sub"   # inside the sample window
        sub.write_text(SUB_FILE)
        self.window.import_paths([sub])

        self.assertFalse(self.window._using_sample)
        self.assertEqual(self.window._sample_tracks, [])
        self.assertEqual(self.window.active_tracks(), [])

        record = [r for r in self.window._all_aps
                  if r.type == TYPE_SUBGHZ and r.ssid.startswith("Real")][0]
        self.assertEqual(record.geo_source, GEO_NONE)
        self.assertFalse(record.has_location)

    def test_a_real_track_survives_the_sample_being_retired(self):
        track = self.tmp / "mine.gpx"
        track.write_text(GPX.replace("2026-06-14", "2027-03-01"))
        self.window.import_paths([track])
        self.assertEqual(len(self.window._tracks), 1)

        csv_path = self.tmp / "real.csv"
        csv_path.write_text(
            "WigleWifi-1.4,appRelease=1.0\n"
            "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
            "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
            "AA:BB:CC:DD:EE:01,Net,[OPEN],2027-03-01 09:00:00,6,-50,33.0,-112.0,1,1,WIFI\n"
        )
        self.window.import_paths([csv_path])

        self.assertFalse(self.window._using_sample)
        self.assertEqual(len(self.window.active_tracks()), 1)  # mine, not the sample's

    def test_records_dock_gets_the_filtered_set(self):
        shown = self.window.record_table.view.model().rowCount()
        self.assertEqual(shown, len(self.window.current_filtered()))

    def test_filter_change_updates_the_records_dock(self):
        before = self.window.record_table.view.model().rowCount()
        self.window.filter_panel._type_checks[TYPE_WIFI].setChecked(False)
        self.window._refresh()
        after = self.window.record_table.view.model().rowCount()
        self.assertLess(after, before)

    def test_type_counts_reach_the_filter_panel(self):
        self.assertNotEqual(self.window.filter_panel._type_counts[TYPE_WIFI].text(), "")

    def test_placement_settings_survive_a_save_and_restore(self):
        self.window._max_gap_seconds = 900
        self.window._clock_offset_seconds = -3600
        self.window._save_settings()

        self.window._max_gap_seconds = 1
        self.window._clock_offset_seconds = 1
        self.window._restore_settings()

        self.assertEqual(self.window._max_gap_seconds, 900)
        self.assertEqual(self.window._clock_offset_seconds, -3600)

    def test_status_bar_mentions_unmapped_records(self):
        sub = self.tmp / "Nowhere_20991231-235959.sub"
        sub.write_text(SUB_FILE)
        self.window.import_paths([sub])
        self.assertIn("no location", self.window.statusBar().currentMessage())

    def test_forget_all_clears_everything(self):
        from warmap import store
        csv_path = self.tmp / "cap.csv"
        csv_path.write_text(
            "WigleWifi-1.4,appRelease=1.0\n"
            "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
            "AltitudeMeters,AccuracyMeters,Type\n"
            "AA:BB:CC:DD:EE:01,Net,[ESS],2026-01-01 00:00:00,6,-50,33.0,-112.0,340,5,WIFI\n"
        )
        self.window.import_paths([csv_path])
        self.assertTrue(self.window._all_aps)

        store.clear(config.STORE_PATH)
        self.window._tracks = []
        self.window._set_dataset([], using_sample=False)
        self.assertEqual(self.window._all_aps, [])


class TestPlacementDialog(unittest.TestCase):
    def test_dialog_shows_the_current_values(self):
        _app()
        dialog = PlacementDialog(max_gap=900, clock_offset=-3600)
        try:
            self.assertEqual(dialog.gap.value(), 900)
            self.assertEqual(dialog.offset.value(), -3600)
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
