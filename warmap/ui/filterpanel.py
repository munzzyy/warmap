"""Left dock: every filter control, all wired to emit one `filtersChanged`
signal carrying a `warmap.filters.FilterState`. The main window is the only
thing that knows what to do with that (recompute the visible set, push it to
the map, the stats panel and the records table). This widget has zero data
logic of its own.

It's a scroll area because with nine record types, seven encryption buckets,
four bands and the Sub-GHz code types there is more here than fits a laptop
side panel, and a filter you can't reach is a filter that doesn't exist.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from warmap.filters import FilterState
from warmap.models import (
    ALL_TYPES,
    ENC_BUCKETS,
    ENC_COLORS,
    TYPE_COLORS,
    TYPE_LABELS,
)
from warmap.radio import BAND_2G, BAND_5G, BAND_6G, BAND_SUBGHZ, BAND_UNKNOWN
from warmap.radio import RISK_LABELS, RISK_RAW, RISK_ROLLING, RISK_STATIC, RISK_UNKNOWN

_DEBOUNCE_MS = 200

BANDS = (BAND_2G, BAND_5G, BAND_6G, BAND_SUBGHZ, BAND_UNKNOWN)
CODE_TYPES = (RISK_STATIC, RISK_ROLLING, RISK_RAW, RISK_UNKNOWN)


def _swatch(color: str) -> QLabel:
    dot = QLabel()
    dot.setFixedSize(10, 10)
    dot.setStyleSheet(
        f"background:{color}; border-radius:5px; border:1px solid rgba(0,0,0,0.35);"
    )
    return dot


class FilterPanel(QScrollArea):
    filtersChanged = Signal(object)  # FilterState

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        # Match the stats dock: enough width that the dock title and every
        # filter label render in full instead of being clipped.
        self.setMinimumWidth(250)

        self._enc_checks: dict[str, QCheckBox] = {}
        self._type_checks: dict[str, QCheckBox] = {}
        self._type_counts: dict[str, QLabel] = {}
        self._band_checks: dict[str, QCheckBox] = {}
        self._code_checks: dict[str, QCheckBox] = {}

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search name, address, vendor, protocol...")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._schedule_emit)
        layout.addWidget(QLabel("Search"))
        layout.addWidget(self._search)

        quick = QGroupBox("Quick filters")
        quick_layout = QVBoxLayout(quick)
        self._open_only = QCheckBox("Open Wi-Fi only")
        self._open_only.stateChanged.connect(self._on_open_only_toggled)
        quick_layout.addWidget(self._open_only)

        self._trackers_only = QCheckBox("Bluetooth trackers only")
        self._trackers_only.setToolTip(
            "AirTags, Tiles, SmartTags and anything else identifiable as a "
            "consumer tracker from its advertisement."
        )
        self._trackers_only.stateChanged.connect(self._schedule_emit)
        quick_layout.addWidget(self._trackers_only)

        self._randomized_only = QCheckBox("Randomized addresses only")
        self._randomized_only.setToolTip(
            "Devices using a private/rotating address. These undercount badly "
            "on repeat passes because each pass looks like a new device."
        )
        self._randomized_only.stateChanged.connect(self._schedule_emit)
        quick_layout.addWidget(self._randomized_only)

        self._located_only = QCheckBox("Mapped records only")
        self._located_only.setToolTip(
            "Hide records with no location (Flipper captures that couldn't be "
            "matched to a GPS track)."
        )
        self._located_only.stateChanged.connect(self._schedule_emit)
        quick_layout.addWidget(self._located_only)

        self._show_alpr = QCheckBox("ALPR cameras (DeFlock)")
        self._show_alpr.setChecked(True)
        self._show_alpr.setToolTip(
            "Show the Flock / license-plate-reader camera overlay from DeFlock "
            "and OpenStreetMap. Reference data, not something warmap captured."
        )
        self._show_alpr.stateChanged.connect(self._schedule_emit)
        quick_layout.addWidget(self._show_alpr)
        layout.addWidget(quick)

        layout.addWidget(self._build_types())
        layout.addWidget(self._build_encryption())
        layout.addWidget(self._build_bands())
        layout.addWidget(self._build_code_types())

        rssi_box = QGroupBox("Minimum signal")
        rssi_layout = QVBoxLayout(rssi_box)
        self._rssi_label = QLabel("-100 dBm (no minimum)")
        self._rssi_slider = QSlider(Qt.Orientation.Horizontal)
        self._rssi_slider.setRange(-100, 0)
        self._rssi_slider.setValue(-100)
        self._rssi_slider.valueChanged.connect(self._on_rssi_changed)
        rssi_layout.addWidget(self._rssi_label)
        rssi_layout.addWidget(self._rssi_slider)
        layout.addWidget(rssi_box)

        seen_box = QGroupBox("Minimum times seen")
        seen_layout = QHBoxLayout(seen_box)
        self._min_seen = QSpinBox()
        self._min_seen.setRange(1, 999)
        self._min_seen.setValue(1)
        self._min_seen.setToolTip(
            "Filters out one-off sightings. Useful for separating things that "
            "live somewhere from things that were just passing through."
        )
        self._min_seen.valueChanged.connect(self._schedule_emit)
        seen_layout.addWidget(self._min_seen)
        seen_layout.addStretch()
        layout.addWidget(seen_box)

        chan_box = QGroupBox("Channels")
        chan_layout = QVBoxLayout(chan_box)
        self._channel_list = QListWidget()
        self._channel_list.setMaximumHeight(150)
        self._channel_list.itemChanged.connect(self._schedule_emit)
        chan_layout.addWidget(self._channel_list)
        layout.addWidget(chan_box)

        reset = QPushButton("Reset filters")
        reset.clicked.connect(self.reset)
        layout.addWidget(reset)

        layout.addStretch()
        self.setWidget(body)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit)

    # --- construction helpers ---------------------------------------------

    def _build_types(self) -> QGroupBox:
        box = QGroupBox("Record types")
        outer = QVBoxLayout(box)
        for record_type in ALL_TYPES:
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(_swatch(TYPE_COLORS[record_type]))
            cb = QCheckBox(TYPE_LABELS[record_type])
            cb.setChecked(True)
            cb.stateChanged.connect(self._schedule_emit)
            self._type_checks[record_type] = cb
            row.addWidget(cb, 1)
            count = QLabel("")
            count.setStyleSheet("color: palette(mid);")
            self._type_counts[record_type] = count
            row.addWidget(count)
            outer.addLayout(row)
        return box

    def _build_encryption(self) -> QGroupBox:
        box = QGroupBox("Wi-Fi encryption")
        outer = QVBoxLayout(box)
        for bucket in ENC_BUCKETS:
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(_swatch(ENC_COLORS[bucket]))
            cb = QCheckBox(bucket)
            cb.setChecked(True)
            cb.stateChanged.connect(self._schedule_emit)
            self._enc_checks[bucket] = cb
            row.addWidget(cb, 1)
            outer.addLayout(row)
        return box

    def _build_bands(self) -> QGroupBox:
        box = QGroupBox("Band")
        outer = QVBoxLayout(box)
        for band in BANDS:
            cb = QCheckBox(band)
            cb.setChecked(True)
            cb.stateChanged.connect(self._schedule_emit)
            self._band_checks[band] = cb
            outer.addWidget(cb)
        return box

    def _build_code_types(self) -> QGroupBox:
        box = QGroupBox("Sub-GHz code type")
        box.setToolTip(
            "A fixed code is the same on every press, so a capture of one "
            "replays. A rolling code changes each press."
        )
        outer = QVBoxLayout(box)
        for code in CODE_TYPES:
            cb = QCheckBox(RISK_LABELS[code])
            cb.setChecked(True)
            cb.stateChanged.connect(self._schedule_emit)
            self._code_checks[code] = cb
            outer.addWidget(cb)
        return box

    # --- internal handlers -------------------------------------------------

    def _on_open_only_toggled(self, _state: int) -> None:
        open_only = self._open_only.isChecked()
        for cb in self._enc_checks.values():
            cb.setEnabled(not open_only)
        self._schedule_emit()

    def _on_rssi_changed(self, value: int) -> None:
        if value <= -100:
            self._rssi_label.setText(f"{value} dBm (no minimum)")
        else:
            self._rssi_label.setText(f"{value} dBm or stronger")
        self._schedule_emit()

    def _schedule_emit(self, *_args) -> None:
        self._debounce.start()

    def _emit(self) -> None:
        self.filtersChanged.emit(self.current_state())

    # --- public API ----------------------------------------------------

    def set_available_channels(self, channels: list) -> None:
        """Rebuild the channel checklist from what's actually in the loaded
        data (called after every import). Everything starts checked, same as
        every other filter's default of "show all"."""
        self._channel_list.blockSignals(True)
        self._channel_list.clear()
        for ch in sorted(c for c in channels if c is not None):
            item = QListWidgetItem(str(ch))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, ch)
            self._channel_list.addItem(item)
        self._channel_list.blockSignals(False)

    def set_alpr_count(self, count: int) -> None:
        """Label the ALPR toggle with how many cameras are loaded, and disable
        it when there are none, so an empty overlay doesn't look like a filter
        that's hiding something."""
        if count:
            self._show_alpr.setText(f"ALPR cameras ({count})")
            self._show_alpr.setEnabled(True)
        else:
            self._show_alpr.setText("ALPR cameras (none loaded)")
            self._show_alpr.setEnabled(False)

    def set_type_counts(self, counts: dict) -> None:
        """Show how many records of each type are loaded, and gray out the
        types that aren't present at all, so a capture with no Sub-GHz in it
        doesn't look like a filter that's hiding something."""
        for record_type, label in self._type_counts.items():
            count = counts.get(record_type, 0)
            label.setText(str(count) if count else "")
            self._type_checks[record_type].setEnabled(bool(count))

    def reset(self) -> None:
        blocked = [
            self._search, self._open_only, self._trackers_only,
            self._randomized_only, self._located_only, self._show_alpr,
            self._rssi_slider, self._min_seen, self._channel_list,
        ]
        groups = (
            list(self._enc_checks.values()) + list(self._type_checks.values())
            + list(self._band_checks.values()) + list(self._code_checks.values())
        )
        for widget in blocked + groups:
            widget.blockSignals(True)

        self._search.clear()
        self._open_only.setChecked(False)
        self._trackers_only.setChecked(False)
        self._randomized_only.setChecked(False)
        self._located_only.setChecked(False)
        self._show_alpr.setChecked(True)  # the overlay defaults to on
        self._rssi_slider.setValue(-100)
        self._min_seen.setValue(1)
        for cb in groups:
            cb.setChecked(True)
        for i in range(self._channel_list.count()):
            self._channel_list.item(i).setCheckState(Qt.CheckState.Checked)

        for widget in blocked + groups:
            widget.blockSignals(False)

        for cb in self._enc_checks.values():
            cb.setEnabled(True)
        self._rssi_label.setText("-100 dBm (no minimum)")
        self._emit()

    def current_state(self) -> FilterState:
        enc_buckets = frozenset(b for b, cb in self._enc_checks.items() if cb.isChecked())
        types = frozenset(t for t, cb in self._type_checks.items() if cb.isChecked())

        channels: Optional[frozenset] = None
        total = self._channel_list.count()
        if total:
            checked = frozenset(
                self._channel_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(total)
                if self._channel_list.item(i).checkState() == Qt.CheckState.Checked
            )
            if len(checked) < total:
                channels = checked

        bands: Optional[frozenset] = None
        checked_bands = frozenset(b for b, cb in self._band_checks.items() if cb.isChecked())
        if len(checked_bands) < len(self._band_checks):
            bands = checked_bands

        code_types: Optional[frozenset] = None
        checked_codes = frozenset(c for c, cb in self._code_checks.items() if cb.isChecked())
        if len(checked_codes) < len(self._code_checks):
            code_types = checked_codes

        return FilterState(
            text=self._search.text(),
            enc_buckets=enc_buckets,
            min_rssi=self._rssi_slider.value(),
            channels=channels,
            types=types,
            open_only=self._open_only.isChecked(),
            bands=bands,
            trackers_only=self._trackers_only.isChecked(),
            randomized_only=self._randomized_only.isChecked(),
            located_only=self._located_only.isChecked(),
            code_types=code_types,
            min_times_seen=self._min_seen.value(),
            show_alpr=self._show_alpr.isChecked(),
        )
