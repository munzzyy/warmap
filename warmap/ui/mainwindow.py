"""The main window: map center, filter dock left, stats dock right, records
table along the bottom, a toolbar and menu for import/export, and a banner
across the top while whatever's loaded is still the bundled sample.

Owns the one in-memory dataset (`self._all_aps`) and the loaded GPS tracks,
and is the only thing that talks to the map view, the stats panel, the records
table, the persistent store and the filesystem. The dock widgets themselves
hold no data logic (see filterpanel.py / statspanel.py / recordtable.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from warmap import (
    alpr,
    config,
    export,
    filters,
    gps,
    ingest,
    parse,
    sdcard,
    settings,
    stats,
    store,
)
from warmap.models import GEO_NONE
from warmap.ui.filterpanel import FilterPanel
from warmap.ui.mapview import MapView
from warmap.ui.recordtable import RecordTable
from warmap.ui.statspanel import StatsPanel

_MAX_LISTED_SD_FILES = 20

CAPTURE_FILE_FILTER = (
    "All capture files (*.csv *.sub *.nfc *.rfid *.ibtn *.ir *.picopass *.pcap *.pcapng *.gpx *.nmea);;"
    "Wardrive CSV (*.csv);;"
    "Flipper captures (*.sub *.nfc *.rfid *.ibtn *.ir *.picopass);;"
    "Packet captures (*.pcap *.pcapng *.cap);;"
    "GPS tracks (*.gpx *.nmea *.log);;"
    "All files (*)"
)


class PlacementDialog(QDialog):
    """How far in time a capture may sit from a GPS track point and still be
    placed, plus a correction for a Flipper whose clock is off."""

    def __init__(self, max_gap: int, clock_offset: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GPS placement settings")
        layout = QVBoxLayout(self)

        explain = QLabel(
            "Flipper captures have no coordinates. warmap places them by "
            "matching each file's timestamp against a loaded GPS track.\n\n"
            "Widen the tolerance if captures are being left unplaced. Set a "
            "clock offset if the Flipper's own clock was wrong: a positive "
            "value means its clock reads later than real time."
        )
        explain.setWordWrap(True)
        layout.addWidget(explain)

        form = QFormLayout()
        self.gap = QSpinBox()
        self.gap.setRange(5, 86400)
        self.gap.setSingleStep(30)
        self.gap.setSuffix(" s")
        self.gap.setValue(max_gap)
        form.addRow("Match tolerance", self.gap)

        self.offset = QSpinBox()
        self.offset.setRange(-86400, 86400)
        self.offset.setSingleStep(60)
        self.offset.setSuffix(" s")
        self.offset.setValue(clock_offset)
        form.addRow("Flipper clock offset", self.offset)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, tile_port: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("warmap")

        self._all_aps: list = []
        self._tracks: list = []
        # ALPR/Flock cameras are held entirely apart from the captures above:
        # they're reference data from DeFlock/OSM, never persisted to the
        # store, never exported as findings, never in fit-to-data. See
        # warmap/alpr.py.
        self._alpr_cameras: list = []
        # Only non-None while the Send-to-phone dialog is open (see
        # _on_send_to_phone). warmap never listens on the network otherwise.
        self._phone_server = None
        # The sample session's track is kept apart from real ones. It is used
        # and drawn only while the sample is what's loaded, otherwise a real
        # Flipper capture whose timestamp happened to fall inside the sample's
        # window would be placed on the sample's route, hundreds of miles from
        # where it was actually taken.
        self._sample_tracks: list = []
        self.last_ingest = None
        self._using_sample = False
        self._max_gap_seconds = gps.DEFAULT_MAX_GAP_SECONDS
        self._clock_offset_seconds = 0

        self.map_view = MapView(tile_port)
        self.map_view.mapError.connect(self._on_map_error)

        self._banner = QLabel(
            "Showing sample data, not a real capture. "
            "Import your own from the File menu, or drop a folder on the window."
        )
        self._banner.setObjectName("sampleBanner")
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._banner.setVisible(False)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        # Explicit stretch factors: the banner is one line of text and the
        # map takes everything else. Left to the default the label claims a
        # share of the spare vertical space and renders as a fat coloured
        # block across the top of the window.
        central_layout.addWidget(self._banner, 0)
        central_layout.addWidget(self.map_view, 1)
        self.setCentralWidget(central)

        self.filter_panel = FilterPanel()
        self.filter_dock = QDockWidget("Filters", self)
        self.filter_dock.setObjectName("filterDock")
        self.filter_dock.setWidget(self.filter_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.filter_dock)

        self.stats_panel = StatsPanel()
        self.stats_dock = QDockWidget("Stats", self)
        self.stats_dock.setObjectName("statsDock")
        self.stats_dock.setWidget(self.stats_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.stats_dock)

        self.record_table = RecordTable()
        self.records_dock = QDockWidget("Records", self)
        self.records_dock.setObjectName("recordsDock")
        self.records_dock.setWidget(self.record_table)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.records_dock)
        self.records_dock.setVisible(False)

        # Movable and closable, but not floatable. The float button renders as
        # a bare diamond outline in every dock header (the platform style has
        # no icon for it at that size), and a panel torn off into its own
        # window is a state you can only get out of by finding that window
        # again. Closing one is the useful gesture, and View reopens it.
        for dock in (self.filter_dock, self.stats_dock, self.records_dock):
            dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetClosable
            )

        self.filter_panel.filtersChanged.connect(self._on_filters_changed)
        self.record_table.recordSelected.connect(self.map_view.focus_record)

        self.setAcceptDrops(True)
        self._build_toolbar()
        self._build_menu()
        self._restore_settings()
        # The camera overlay loads once, here, so it's present no matter which
        # populate path runs (initial data, a direct import, the CLI, a
        # screenshot). The map view queues the push until the page is ready.
        self._load_alpr()
        self.statusBar().showMessage("Ready")

    # --- toolbar and menu ---------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("mainToolbar")
        self.addToolBar(toolbar)

        for text, slot in (
            ("Open File(s)", self._on_open_files),
            ("Open Folder", self._on_open_folder),
            ("Import from SD Card", self._on_import_sd),
            ("Load GPS Track", self._on_load_track),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            toolbar.addAction(action)

        toolbar.addSeparator()

        self.heatmap_act = QAction("Heatmap", self)
        self.heatmap_act.setCheckable(True)
        self.heatmap_act.toggled.connect(self.map_view.set_heatmap_visible)
        toolbar.addAction(self.heatmap_act)

        fit_act = QAction("Fit to Data", self)
        fit_act.triggered.connect(self.map_view.fit_to_data)
        toolbar.addAction(fit_act)

        self.records_act = QAction("Records", self)
        self.records_act.setCheckable(True)
        self.records_act.setChecked(False)
        self.records_act.toggled.connect(self.records_dock.setVisible)
        self.records_dock.visibilityChanged.connect(self.records_act.setChecked)
        toolbar.addAction(self.records_act)

        self.toolbar = toolbar

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        for text, slot in (
            ("Open File(s)...", self._on_open_files),
            ("Open Folder...", self._on_open_folder),
            ("Import from SD Card...", self._on_import_sd),
            ("Load GPS Track...", self._on_load_track),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            file_menu.addAction(action)

        file_menu.addSeparator()
        phone_act = QAction("Send to phone...", self)
        phone_act.setToolTip(
            "Show a QR code your phone can scan to open this session in the "
            "mobile app, over your own network."
        )
        phone_act.triggered.connect(self._on_send_to_phone)
        file_menu.addAction(phone_act)

        offline_act = QAction("Save offline app for phone...", self)
        offline_act.setToolTip(
            "Write one HTML file holding the app and this data. Copy it to a "
            "phone and it opens with no PC and no network."
        )
        offline_act.triggered.connect(self._on_save_offline)
        file_menu.addAction(offline_act)

        file_menu.addSeparator()
        export_menu = file_menu.addMenu("Export")
        for text, slot in (
            ("CSV...", self._on_export_csv),
            ("GeoJSON...", self._on_export_geojson),
            ("GPX...", self._on_export_gpx),
            ("KML (Google Earth)...", self._on_export_kml),
            ("Wigle CSV...", self._on_export_wigle),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            export_menu.addAction(action)

        file_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        view_menu = menu.addMenu("&View")
        view_menu.addAction(self.filter_dock.toggleViewAction())
        view_menu.addAction(self.stats_dock.toggleViewAction())
        view_menu.addAction(self.records_dock.toggleViewAction())

        tools_menu = menu.addMenu("&Tools")
        placement_act = QAction("GPS placement settings...", self)
        placement_act.triggered.connect(self._on_placement_settings)
        tools_menu.addAction(placement_act)

        replace_act = QAction("Re-place unmapped records", self)
        replace_act.triggered.connect(self._on_replace_unmapped)
        tools_menu.addAction(replace_act)

        reload_alpr_act = QAction("Reload DeFlock ALPR cameras", self)
        reload_alpr_act.setToolTip(
            "Re-read the camera snapshot from disk after refreshing it with "
            "`warmap flock --refresh`."
        )
        reload_alpr_act.triggered.connect(self._reload_alpr)
        tools_menu.addAction(reload_alpr_act)

        tools_menu.addSeparator()
        forget_act = QAction("Forget all collected data...", self)
        forget_act.triggered.connect(self._on_forget_all)
        tools_menu.addAction(forget_act)

    # --- dataset lifecycle ------------------------------------------------

    def load_initial_data(self) -> None:
        """Called once after construction: the persisted store if there's
        anything real in it, else the bundled sample with a banner.

        The sample is loaded through the same ingest path as a real import,
        CSV, GPS track and Flipper captures together, so first run shows
        timestamp-based placement working rather than just describing it. It
        is never written to the store; nothing about the sample should
        survive into real collected data.
        """
        persisted = store.load(config.STORE_PATH)
        if persisted:
            self._set_dataset(persisted, using_sample=False)
            self.map_view.fit_to_data()
            return

        result = ingest.ingest_paths([config.SAMPLE_DIR])
        self._sample_tracks = list(result.tracks)
        # _set_dataset first: `active_tracks()` only counts the sample's track
        # while `_using_sample` is true, so drawing before that flag is set
        # draws nothing.
        self._set_dataset(parse.dedup(result.sightings), using_sample=True)
        self._show_tracks()
        self.map_view.fit_to_data()

    # --- the phone bridge --------------------------------------------------

    def _phone_data_source(self):
        """What the mobile app is allowed to read.

        Every field is a callable rather than a snapshot, so the phone always
        gets what this window is showing right now: import something on the
        PC, hit sync on the phone, and it's there.
        """
        import socket

        from warmap import alpr as alpr_mod
        from warmap import server as server_mod

        def records():
            return export.to_geojson(self.current_filtered())

        def cameras():
            snapshot = alpr_mod.load_snapshot(config.flock_data_path())
            return alpr_mod.to_transfer(
                self._alpr_cameras,
                attribution=snapshot.attribution,
                generated=snapshot.generated,
            )

        def tracks():
            active = self.active_tracks()
            if not active:
                return None
            return {"type": "FeatureCollection",
                    "features": [t.to_geojson() for t in active]}

        def session():
            try:
                host = socket.gethostname()
            except OSError:
                host = "this PC"
            return {
                "host": host,
                "generated": alpr_mod.load_snapshot(config.flock_data_path()).generated,
                "counts": {
                    "records": len(self._all_aps),
                    "cameras": len(self._alpr_cameras),
                },
            }

        return server_mod.DataSource(
            records=records, cameras=cameras, tracks=tracks, session=session,
        )

    def _on_send_to_phone(self) -> None:
        """Start the phone server for as long as the dialog is open."""
        from warmap import server as server_mod
        from warmap.ui.phonedialog import PhoneDialog

        if not self._all_aps:
            QMessageBox.information(
                self, "warmap",
                "There's nothing collected yet, so the phone would open an "
                "empty map. Import a capture first."
            )
            return

        srv = server_mod.WarmapServer(
            data=self._phone_data_source(),
            tile_proxy=getattr(self, "tile_proxy", None),
        )
        try:
            srv.start()
        except OSError as exc:
            QMessageBox.warning(
                self, "warmap",
                f"Couldn't start the phone server:\n{exc}"
            )
            return

        self._phone_server = srv
        try:
            PhoneDialog(srv, self).exec()
        finally:
            # Whatever happens to the dialog, the server does not outlive it.
            srv.stop()
            self._phone_server = None
            self.statusBar().showMessage("Stopped serving to phone", 5000)

    def _on_save_offline(self) -> None:
        """Write the single-file app, data and all."""
        from warmap import alpr as alpr_mod
        from warmap import offline

        records = self.current_filtered()
        if not records:
            QMessageBox.information(
                self, "warmap", "Nothing to save: no records are visible."
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save offline app", str(Path.home() / "warmap-offline.html"),
            "Web page (*.html)",
        )
        if not path:
            return

        records_geojson = export.to_geojson(records)
        tracks_geojson = None
        if self.active_tracks():
            tracks_geojson = {
                "type": "FeatureCollection",
                "features": [t.to_geojson() for t in self.active_tracks()],
            }

        snapshot = alpr_mod.load_snapshot(config.flock_data_path())
        near = offline.nearby_cameras(
            self._alpr_cameras, records_geojson, tracks_geojson)
        cameras = alpr_mod.to_transfer(
            near, attribution=snapshot.attribution, generated=snapshot.generated)

        # No hostname in a file that may be handed around: the phone menu
        # just shows the date.
        try:
            bundle = offline.build(
                records_geojson, cameras_transfer=cameras,
                tracks_geojson=tracks_geojson,
                session={"host": "", "generated": snapshot.generated,
                         "counts": {"records": len(records), "cameras": len(near)}},
            )
            written = offline.write(bundle, Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "warmap", f"Could not write that file:\n{exc}")
            return

        QMessageBox.information(
            self, "warmap",
            f"Saved {Path(path).name}, {written / 1_000_000:.1f} MB.\n\n"
            f"{bundle.records} record(s) and {bundle.cameras} nearby camera(s) "
            "are inside the file.\n\nCopy it to your phone and open it from "
            "the downloads folder. It works with no PC and no network; only "
            "the map background needs a connection."
        )

    def _load_alpr(self) -> None:
        """Load the DeFlock/OSM camera snapshot and push it to the map as a
        toggleable overlay. Fail-soft: a missing or unreadable snapshot just
        means no cameras, never a startup failure."""
        snapshot = alpr.load_snapshot(config.flock_data_path())
        self._alpr_cameras = snapshot.cameras
        self.map_view.set_alpr(alpr.to_geojson(self._alpr_cameras))
        self.map_view.set_alpr_visible(self.filter_panel.current_state().show_alpr)
        self.filter_panel.set_alpr_count(len(self._alpr_cameras))
        self._update_alpr_stats()

    def _reload_alpr(self) -> None:
        """Re-read the snapshot from disk, after a `warmap flock --refresh`
        pulled a fresh set while the window was open, and redraw the overlay."""
        before = len(self._alpr_cameras)
        self._load_alpr()
        after = len(self._alpr_cameras)
        if after:
            self.statusBar().showMessage(
                f"Loaded {after} ALPR camera(s) from DeFlock/OSM"
                + (f" (was {before})" if before != after else "")
            )
        else:
            QMessageBox.information(
                self, "warmap",
                "No ALPR camera snapshot found. Refresh it from the terminal "
                "with:\n\n    warmap flock --refresh\n\nwhich pulls the current "
                "set from OpenStreetMap.",
            )

    def _alpr_reference_points(self) -> list:
        """Every point the cameras can be measured 'near': the located
        captures plus every loaded GPS track point. This is what turns the
        global camera set into 'the cameras you actually drove past'."""
        points = [(r.lat, r.lon) for r in self._all_aps if r.has_location]
        for track in self.active_tracks():
            points.extend((p.lat, p.lon) for p in track.points)
        return points

    def _update_alpr_stats(self) -> None:
        near = 0
        points = self._alpr_reference_points()
        if self._alpr_cameras and points:
            near = len(alpr.cameras_near(self._alpr_cameras, points))
        self.stats_panel.set_alpr_stats(alpr.camera_stats(self._alpr_cameras, near=near))

    def _set_dataset(self, records: list, using_sample: bool) -> None:
        was_sample = self._using_sample
        self._all_aps = records
        self._using_sample = using_sample
        self._banner.setVisible(using_sample)
        if was_sample and not using_sample:
            # The sample has been retired. Its track goes with it.
            self._sample_tracks = []
            self._show_tracks()

        channels = sorted({r.channel for r in records if r.channel is not None})
        self.filter_panel.set_available_channels(channels)

        counts: dict = {}
        for record in records:
            counts[record.type] = counts.get(record.type, 0) + 1
        self.filter_panel.set_type_counts(counts)

        self._refresh()
        # The "cameras near your captures" count depends on what's loaded, so
        # recompute it whenever the dataset changes.
        self._update_alpr_stats()

    def _refresh(self) -> None:
        state = self.filter_panel.current_state()
        filtered = filters.apply_filters(self._all_aps, state)
        self.stats_panel.set_stats(stats.compute_stats(filtered))
        self.stats_panel.set_track_summary(self._track_summary())
        self.record_table.set_records(filtered)
        self.map_view.load_data(export.to_geojson(filtered))
        # Cheap: the overlay is already on the map, this just shows/hides it.
        # The heavy push happens once in _load_alpr, never per filter change.
        self.map_view.set_alpr_visible(state.show_alpr)
        self._update_status(filtered)

    def _update_status(self, filtered: list) -> None:
        total = len(self._all_aps)
        shown = len(filtered)
        unmapped = sum(1 for r in filtered if not r.has_location)
        message = f"{shown} of {total} record(s) shown"
        if unmapped:
            message += f", {unmapped} with no location (see the Records dock)"
        self.statusBar().showMessage(message)

    def _on_map_error(self, message: str) -> None:
        """A map that fails has to say so. Silence here reads as "your capture
        had nothing in it", which is the wrong conclusion and sends you back
        to the card looking for a fault that isn't there."""
        self.statusBar().showMessage(f"Map: {message}", 15000)

    def active_tracks(self) -> list:
        """The tracks that count right now: the real ones always, plus the
        sample's only while the sample is still what's loaded."""
        if self._using_sample:
            return self._tracks + self._sample_tracks
        return self._tracks

    def _track_summary(self) -> str:
        tracks = self.active_tracks()
        if not tracks:
            return "none loaded"
        points = sum(len(t) for t in tracks)
        distance = sum(t.distance_km() for t in tracks)
        lines = [f"{len(tracks)} track(s), {points} points, {distance:.2f} km"]
        if any(t.derived for t in tracks):
            lines.append("rebuilt from wardrive rows (no track file loaded)")
        starts = [t.start for t in tracks if t.start]
        ends = [t.end for t in tracks if t.end]
        if starts and ends:
            lines.append(
                f"{min(starts).strftime('%Y-%m-%d %H:%M')} to "
                f"{max(ends).strftime('%Y-%m-%d %H:%M')}"
            )
        return "\n".join(lines)

    def _on_filters_changed(self, _state) -> None:
        self._refresh()

    def current_filtered(self) -> list:
        return filters.apply_filters(self._all_aps, self.filter_panel.current_state())

    # --- import ----------------------------------------------------------

    def import_paths(self, paths: list) -> int:
        """Every import funnels through here: File>Open, folder import,
        SD-card import, drag and drop, and the CLI.

        Additive: merges into whatever's already loaded, dedups across
        old+new, and persists to the real store (which also permanently
        retires the sample banner, since anything imported is real). Returns
        the number of raw records parsed (0 means nothing usable was found).

        Deliberately shows no dialogs. This is the programmatic entry point,
        called from the CLI and from tests where a modal would block forever;
        the interactive callers use `import_and_report` to say what happened.
        The full account is left on `self.last_ingest` either way.
        """
        # Import runs on the GUI thread, so anything that escapes here takes
        # the window down. The parsers are careful, but they read whatever is
        # on a card that could hold anything, and a failed import should be a
        # message, never a crash.
        try:
            result = ingest.ingest_paths(
                paths,
                extra_tracks=self.active_tracks(),
                max_gap_seconds=self._max_gap_seconds,
                clock_offset_seconds=self._clock_offset_seconds,
            )
        except (MemoryError, RecursionError) as exc:
            result = ingest.IngestResult()
            result.errors.append(
                f"Ran out of room reading those files ({type(exc).__name__}). "
                "One of them is probably far larger than a capture should be."
            )
        except Exception as exc:  # noqa: BLE001 - last line of defence
            result = ingest.IngestResult()
            result.errors.append(
                f"Import failed: {type(exc).__name__}: {exc}\n\n"
                "This is a bug in warmap. `warmap inspect <file>` on the file "
                "that broke it will print what it managed to parse."
            )
        self.last_ingest = result

        if result.tracks:
            self._tracks.extend(result.tracks)
            self._show_tracks()

        if not result.sightings:
            if result.tracks:
                # A track on its own is a legitimate import: it may place
                # records that are already loaded.
                self._replace_unmapped(quiet=True)
                self._refresh()
                # New track points (and any records they placed) change the
                # "cameras near your captures" set, so recompute it.
                self._update_alpr_stats()
                self.map_view.fit_to_data()
            return 0

        merged = store.merge_and_save(result.sightings, config.STORE_PATH)
        self._set_dataset(merged, using_sample=False)
        self.map_view.fit_to_data()
        return len(result.sightings)

    def import_and_report(self, paths: list) -> int:
        """Import, then tell the user what happened. The interactive path."""
        imported = self.import_paths(paths)
        self._report_ingest(self.last_ingest, imported)
        return imported

    def _report_ingest(self, result, imported: int) -> None:
        """Say what actually happened. Most import failures are silent and
        each has a different fix, so the notes matter more than the count."""
        if result is None:
            return

        if not result.notes and not result.errors:
            if imported:
                self.statusBar().showMessage(f"Imported {imported} record(s)")
            else:
                QMessageBox.information(
                    self, "warmap", "No usable records found in the selected file(s)."
                )
            return

        lines = []
        if imported:
            lines.append(f"Imported {imported} record(s) from {result.files_read} file(s).")
        else:
            lines.append("No records were imported.")
        if result.files_skipped:
            lines.append(f"{result.files_skipped} file(s) skipped (not a format warmap reads).")
        lines.extend(result.notes)
        lines.extend(result.errors)

        dialog = QDialog(self)
        dialog.setWindowTitle("Import results")
        dialog.resize(560, 320)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join(lines))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_tracks(self) -> None:
        tracks = self.active_tracks()
        if not tracks:
            self.map_view.set_track(None)
            return
        self.map_view.set_track({
            "type": "FeatureCollection",
            "features": [t.to_geojson() for t in tracks],
        })

    def _on_open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Capture File(s)", str(Path.home()), CAPTURE_FILE_FILTER
        )
        if paths:
            self.import_and_report([Path(p) for p in paths])

    def _on_open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Open Folder of Captures", str(Path.home())
        )
        if folder:
            self.import_and_report([Path(folder)])

    def _on_load_track(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Load GPS Track", str(Path.home()),
            "GPS tracks (*.gpx *.nmea *.log *.txt);;All files (*)"
        )
        if paths:
            self.import_and_report([Path(p) for p in paths])

    def _on_import_sd(self) -> None:
        found = sdcard.scan_removable_media()
        if not found:
            QMessageBox.information(
                self, "warmap",
                f"No removable media with capture files found under "
                f"{sdcard.describe_roots()}.\n\nwarmap looks for wardrive CSVs, Flipper captures "
                "(.sub/.nfc/.rfid/.ibtn/.ir), packet captures and GPS tracks."
            )
            return
        shown = "\n".join(str(p) for p in found[:_MAX_LISTED_SD_FILES])
        remainder = len(found) - _MAX_LISTED_SD_FILES
        more = f"\n...and {remainder} more" if remainder > 0 else ""
        # The scan stops walking a card that's mostly other things, so on a
        # big card this list can be short of what's really there. Say so:
        # importing "everything" and quietly getting some of it is the kind
        # of thing you only notice weeks later when a drive is missing.
        if getattr(found, "truncated", False):
            more += ("\n\nThis card is large enough that the scan stopped early, "
                     "so there may be more on it than this. Use Open Folder on a "
                     "specific folder to be sure of getting all of it.")
        reply = QMessageBox.question(
            self,
            "Import from SD card",
            f"Found {len(found)} capture file(s):\n{shown}{more}\n\nImport all of them?",
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.import_and_report(found)

    # --- drag and drop -----------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.import_and_report(paths)
            event.acceptProposedAction()

    # --- tools -------------------------------------------------------------

    def _on_placement_settings(self) -> None:
        dialog = PlacementDialog(self._max_gap_seconds, self._clock_offset_seconds, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._max_gap_seconds = dialog.gap.value()
        self._clock_offset_seconds = dialog.offset.value()
        self._on_replace_unmapped()

    def _replace_unmapped(self, quiet: bool = False) -> int:
        unmapped = [r for r in self._all_aps if r.geo_source == GEO_NONE]
        if not unmapped:
            if not quiet:
                QMessageBox.information(
                    self, "warmap", "Every loaded record already has a location."
                )
            return 0
        if not self.active_tracks():
            if not quiet:
                QMessageBox.information(
                    self, "warmap",
                    f"{len(unmapped)} record(s) have no location, but no GPS track "
                    "is loaded. Load a .nmea or .gpx recorded during the same "
                    "session and they can be placed."
                )
            return 0
        placed = gps.geotag(
            unmapped, self.active_tracks(),
            max_gap_seconds=self._max_gap_seconds,
            clock_offset_seconds=self._clock_offset_seconds,
        )
        # Only ever persist real data. Re-placing while the bundled sample is
        # loaded must not write the sample into the collected store, that's
        # the one thing the sample is promised never to do.
        if placed and not self._using_sample:
            store.save(self._all_aps, config.STORE_PATH)
        return placed

    def _on_replace_unmapped(self) -> None:
        placed = self._replace_unmapped()
        self._refresh()
        if placed:
            # Newly placed records are new reference points for the camera
            # proximity count.
            self._update_alpr_stats()
            self.map_view.fit_to_data()
            self.statusBar().showMessage(f"Placed {placed} record(s) from the GPS track")

    def _on_forget_all(self) -> None:
        reply = QMessageBox.question(
            self, "Forget all collected data",
            "This permanently deletes every record warmap has collected across "
            "all sessions. There is no undo.\n\nThe original capture files on "
            "disk are not touched.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        store.clear(config.STORE_PATH)
        self._tracks = []
        self._sample_tracks = []
        self.map_view.set_track(None)
        self._set_dataset([], using_sample=False)
        self.statusBar().showMessage("Collected data cleared")

    # --- export ----------------------------------------------------------

    def _export(self, title: str, default_name: str, file_filter: str, writer) -> None:
        records = self.current_filtered()
        if not records:
            QMessageBox.information(self, "warmap", "Nothing to export: no records are visible.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, title, str(Path.home() / default_name), file_filter
        )
        if not path:
            return
        try:
            written = writer(records, Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "warmap", f"Could not write that file:\n{exc}")
            return
        skipped = len(records) - written
        message = f"Exported {written} record(s) to {Path(path).name}"
        if skipped > 0:
            message += f", {skipped} skipped, they have no location"
        self.statusBar().showMessage(message)

    def _on_export_csv(self) -> None:
        self._export("Export CSV", "warmap-export.csv", "CSV Files (*.csv)", export.write_csv)

    def _on_export_geojson(self) -> None:
        self._export(
            "Export GeoJSON", "warmap-export.geojson",
            "GeoJSON Files (*.geojson)", export.write_geojson,
        )

    def _on_export_gpx(self) -> None:
        points = [p for track in self.active_tracks() for p in track.points]

        def writer(records, path):
            return export.write_gpx(records, path, track_points=points)

        self._export("Export GPX", "warmap-export.gpx", "GPX Files (*.gpx)", writer)

    def _on_export_kml(self) -> None:
        self._export("Export KML", "warmap-export.kml", "KML Files (*.kml)", export.write_kml)

    def _on_export_wigle(self) -> None:
        self._export(
            "Export Wigle CSV", "warmap-wigle.csv",
            "CSV Files (*.csv)", export.write_wigle_csv,
        )

    # --- window settings ---------------------------------------------------

    def _restore_settings(self) -> None:
        data = settings.load()
        geom_hex: Optional[str] = data.get("geometry")
        if geom_hex:
            try:
                self.restoreGeometry(bytes.fromhex(geom_hex))
            except (ValueError, TypeError):
                self.resize(1400, 900)
        else:
            self.resize(1400, 900)

        state_hex: Optional[str] = data.get("window_state")
        if state_hex:
            try:
                self.restoreState(bytes.fromhex(state_hex))
            except (ValueError, TypeError):
                pass

        try:
            self._max_gap_seconds = int(data.get("max_gap_seconds", gps.DEFAULT_MAX_GAP_SECONDS))
            self._clock_offset_seconds = int(data.get("clock_offset_seconds", 0))
        except (TypeError, ValueError):
            self._max_gap_seconds = gps.DEFAULT_MAX_GAP_SECONDS
            self._clock_offset_seconds = 0

    def _save_settings(self) -> None:
        data = settings.load()
        data["geometry"] = bytes(self.saveGeometry()).hex()
        data["window_state"] = bytes(self.saveState()).hex()
        data["max_gap_seconds"] = self._max_gap_seconds
        data["clock_offset_seconds"] = self._clock_offset_seconds
        settings.save(data)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        super().closeEvent(event)
