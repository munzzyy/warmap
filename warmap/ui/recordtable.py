"""Bottom dock: every visible record as a sortable table.

The map answers "what's around here". This answers "what did I actually
collect", and it's the only place records with no location show up at all,
which matters because a Flipper capture that couldn't be matched to a GPS
track is still a real capture and shouldn't vanish just because it can't be
drawn.

Backed by a QAbstractTableModel rather than QTableWidget: a decent wardrive is
tens of thousands of rows and populating that many widget items takes seconds
and a lot of memory, while a model view stays instant.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QVBoxLayout, QWidget

from warmap.models import (
    ENC_COLORS,
    GEO_DIRECT,
    GEO_NONE,
    GEO_TRACK,
    TYPE_COLORS,
    TYPE_LABELS,
    Sighting,
)

_COLUMNS = (
    ("Name", 180),
    ("Type", 110),
    ("Identity", 160),
    ("Vendor", 130),
    ("Signal", 60),
    ("Ch", 45),
    ("Freq (MHz)", 85),
    ("Encryption", 95),
    ("Seen", 50),
    ("First seen", 145),
    ("Position", 95),
    ("Notes", 260),
)

_POSITION_LABELS = {
    GEO_DIRECT: "GPS fix",
    GEO_TRACK: "from track",
    GEO_NONE: "none",
}

# Sorting uses this role so numeric columns sort numerically instead of
# lexically ("-9" before "-70").
SORT_ROLE = Qt.ItemDataRole.UserRole + 1

FIRST_SEEN_COLUMN = next(
    i for i, (name, _) in enumerate(_COLUMNS) if name == "First seen"
)


def _notes(sighting: Sighting) -> str:
    """The one line of type-specific detail worth showing in a table row."""
    meta = sighting.meta
    bits = []
    if meta.get("tracker"):
        bits.append(meta["tracker"])
    if meta.get("code_type_label"):
        bits.append(meta["code_type_label"])
    if meta.get("protocol") and not meta.get("code_type_label"):
        bits.append(str(meta["protocol"]))
    if meta.get("technology"):
        bits.append(str(meta["technology"]))
    if meta.get("company") and not bits:
        bits.append(str(meta["company"]))
    if meta.get("address_type", "").startswith("random"):
        bits.append("randomized address")
    if meta.get("frequency_note"):
        bits.append(str(meta["frequency_note"]))
    if meta.get("geo_match_seconds") is not None:
        bits.append(f"track match {meta['geo_match_seconds']:g}s off")
    return " · ".join(bits)


class RecordModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[Sighting] = []

    def set_records(self, records: list) -> None:
        self.beginResetModel()
        self._rows = list(records)
        self.endResetModel()

    def record_at(self, row: int) -> Optional[Sighting]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(_COLUMNS):
            return _COLUMNS[section][0]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        sighting = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.ForegroundRole:
            if column == 1:
                return QColor(TYPE_COLORS.get(sighting.type, "#8a8a8a"))
            if column == 7 and sighting.type == "WIFI":
                return QColor(ENC_COLORS.get(sighting.enc_bucket, "#8a8a8a"))
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{sighting.display_name}\n{sighting.bssid}\n{sighting.source}"

        if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            return None

        sorting = role == SORT_ROLE

        if column == 0:
            return sighting.display_name
        if column == 1:
            return TYPE_LABELS.get(sighting.type, sighting.type)
        if column == 2:
            return sighting.bssid
        if column == 3:
            return sighting.vendor
        if column == 4:
            if sighting.rssi == 0:
                return -999 if sorting else ""
            return sighting.rssi if sorting else str(sighting.rssi)
        if column == 5:
            if sighting.channel is None:
                return -1 if sorting else ""
            return sighting.channel if sorting else str(sighting.channel)
        if column == 6:
            if sighting.frequency is None:
                return -1.0 if sorting else ""
            return sighting.frequency if sorting else f"{sighting.frequency:g}"
        if column == 7:
            return sighting.enc_bucket if sighting.type == "WIFI" else ""
        if column == 8:
            return sighting.times_seen if sorting else str(sighting.times_seen)
        if column == 9:
            return sighting.first_seen
        if column == 10:
            return _POSITION_LABELS.get(sighting.geo_source, sighting.geo_source)
        if column == 11:
            return _notes(sighting)
        return None


class RecordTable(QWidget):
    recordSelected = Signal(str, str)  # (type, identity)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._model = RecordModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(SORT_ROLE)

        self.view = QTableView()
        self.view.setModel(self._proxy)
        self.view.setSortingEnabled(True)
        # Say what the opening order is. Left to itself the view sorted on
        # column 0, which put the table in reverse alphabetical order by
        # name, an order nobody chose and that reads as a fault. Newest
        # sighting first is what you want to see after a drive.
        self.view.sortByColumn(FIRST_SEEN_COLUMN, Qt.SortOrder.DescendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(22)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for i, (_, width) in enumerate(_COLUMNS):
            self.view.setColumnWidth(i, width)

        self.view.doubleClicked.connect(self._on_activated)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)

        layout.addWidget(self.view)

    def set_records(self, records: list) -> None:
        self._model.set_records(records)

    def _record_for_proxy_index(self, index) -> Optional[Sighting]:
        if not index.isValid():
            return None
        return self._model.record_at(self._proxy.mapToSource(index).row())

    def _on_activated(self, index) -> None:
        record = self._record_for_proxy_index(index)
        if record is not None and record.has_location:
            self.recordSelected.emit(record.type, record.bssid)

    def _on_selection_changed(self, selected, _deselected) -> None:
        indexes = selected.indexes()
        if not indexes:
            return
        record = self._record_for_proxy_index(indexes[0])
        if record is not None and record.has_location:
            self.recordSelected.emit(record.type, record.bssid)
