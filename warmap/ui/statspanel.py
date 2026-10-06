"""Right dock: the numbers, recomputed by the main window every time the
filter set changes. `BarChart` is a small custom QPainter bar chart (no
charting library) that reads `theme.current()` fresh in paintEvent.

Bars are colored by what they represent rather than by a theme accent: the
encryption bucket's color, the record type's color, the Sub-GHz code type's
color. In each of those the color *is* the data, and it matches the map
legend, so the two panels read as one thing.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from warmap.models import ENC_BUCKETS, ENC_COLORS, TYPE_COLORS, TYPE_LABELS
from warmap.radio import RISK_COLORS
from warmap.stats import Stats
from warmap.ui import theme

_ROW_H = 20
_GAP = 2


class BarChart(QWidget):
    """One horizontal bar per row, length proportional to count. Rows are
    (label, count, color) and are drawn in the order given."""

    def __init__(self, label_width: int = 90, parent=None):
        super().__init__(parent)
        self._rows: list[tuple] = []
        # A floor, not a fixed size. The column grows to fit the longest label
        # it actually has to draw: at a fixed 90px "WPA2/3-mixed" rendered as
        # "WPA2/3-mixe", which reads as a different encryption bucket rather
        # than as clipped text.
        self._label_width = label_width
        self.setMinimumHeight(_ROW_H + 8)

    def _column_width(self, metrics: QFontMetrics) -> int:
        needed = max((metrics.horizontalAdvance(label) for label, _, _ in self._rows),
                     default=0) + 12
        # Never let the labels eat the whole widget. Past half the width the
        # bars stop being comparable, which is the point of the chart.
        return int(min(max(self._label_width, needed), self.width() * 0.55))

    def set_rows(self, rows: list) -> None:
        self._rows = list(rows)
        self.setMinimumHeight(max(1, len(self._rows)) * (_ROW_H + _GAP) + 8)
        self.updateGeometry()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        t = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not self._rows or all(count == 0 for _, count, _ in self._rows):
            painter.setPen(QColor(t.text_muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No data")
            painter.end()
            return

        metrics = painter.fontMetrics()
        label_w = self._column_width(metrics)
        # Reserve room for the longest count so a full-length bar can't push
        # its own number off the edge of the panel.
        count_w = max(metrics.horizontalAdvance(str(count)) for _, count, _ in self._rows) + 10
        max_count = max(count for _, count, _ in self._rows) or 1
        bar_area_w = max(1, self.width() - label_w - count_w - 8)
        y = 4

        for label, count, color in self._rows:
            painter.setPen(QColor(t.text_secondary))
            painter.drawText(
                QRectF(4, y, label_w - 8, _ROW_H),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(label, Qt.TextElideMode.ElideRight, label_w - 8),
            )
            bar_w = (count / max_count) * bar_area_w
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(
                QRectF(label_w, y + 2, bar_w, _ROW_H - 4), 3, 3
            )
            painter.setPen(QColor(t.text_primary))
            painter.drawText(
                QRectF(label_w + bar_w + 4, y, count_w, _ROW_H),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                str(count),
            )
            y += _ROW_H + _GAP

        painter.end()


class StatsPanel(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        # Without a floor the dock opens narrow enough to clip its own title
        # (down to just "St") and every section header. Give it room.
        self.setMinimumWidth(250)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(10)

        self._total = QLabel("-")
        self._mapped = QLabel("-")
        self._unique_ssids = QLabel("-")
        self._open_count = QLabel("-")
        self._wifi_ble = QLabel("-")
        self._trackers = QLabel("-")
        self._followers = QLabel("-")
        self._randomized = QLabel("-")
        self._vendors = QLabel("-")
        self._time_range = QLabel("-")
        self._bbox = QLabel("-")
        self._area = QLabel("-")
        self._channels = QLabel("-")
        self._track = QLabel("-")
        self._frequencies = QLabel("-")
        self._alpr = QLabel("-")
        self._alpr_breakdown = QLabel("-")
        for lbl in (self._mapped, self._time_range, self._bbox, self._area,
                    self._channels, self._vendors, self._track, self._trackers,
                    self._frequencies, self._randomized, self._followers,
                    self._alpr, self._alpr_breakdown):
            lbl.setWordWrap(True)

        layout.addWidget(self._section("Records shown"))
        layout.addWidget(self._total)
        layout.addWidget(self._mapped)

        layout.addWidget(self._section("By type"))
        self._type_bars = BarChart(label_width=110)
        layout.addWidget(self._type_bars)

        layout.addWidget(self._section("Unique names"))
        layout.addWidget(self._unique_ssids)
        layout.addWidget(self._section("Open networks"))
        layout.addWidget(self._open_count)
        layout.addWidget(self._section("Wi-Fi / BLE"))
        layout.addWidget(self._wifi_ble)

        layout.addWidget(self._section("Wi-Fi encryption"))
        self._enc_bars = BarChart(label_width=90)
        layout.addWidget(self._enc_bars)

        layout.addWidget(self._section("Band"))
        self._band_bars = BarChart(label_width=80)
        layout.addWidget(self._band_bars)

        layout.addWidget(self._section("Sub-GHz code type"))
        self._code_bars = BarChart(label_width=70)
        layout.addWidget(self._code_bars)
        layout.addWidget(self._frequencies)

        layout.addWidget(self._section("Bluetooth trackers"))
        layout.addWidget(self._trackers)
        layout.addWidget(self._section("Possible followers"))
        layout.addWidget(self._followers)
        layout.addWidget(self._section("Randomized addresses"))
        layout.addWidget(self._randomized)

        layout.addWidget(self._section("Surveillance (ALPR)"))
        layout.addWidget(self._alpr)
        layout.addWidget(self._alpr_breakdown)

        layout.addWidget(self._section("Top vendors"))
        layout.addWidget(self._vendors)

        layout.addWidget(self._section("Channel distribution"))
        layout.addWidget(self._channels)

        layout.addWidget(self._section("Capture time range"))
        layout.addWidget(self._time_range)

        layout.addWidget(self._section("GPS track"))
        layout.addWidget(self._track)

        layout.addWidget(self._section("Bounding box"))
        layout.addWidget(self._bbox)

        layout.addWidget(self._section("Approx. area covered"))
        layout.addWidget(self._area)

        layout.addStretch()
        self.setWidget(body)

    @staticmethod
    def _section(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("cssClass", "sectionHeader")
        lbl.setWordWrap(True)
        return lbl

    def set_track_summary(self, text: str) -> None:
        self._track.setText(text or "none loaded")

    def set_alpr_stats(self, stats) -> None:
        """The DeFlock/OSM camera overlay's rollup. Separate from set_stats
        because cameras are reference data, not captures. They never dilute
        the capture numbers above, they get their own section."""
        if not stats.total:
            self._alpr.setText("none loaded")
            self._alpr_breakdown.setText(
                "Refresh from the terminal: warmap flock --refresh"
            )
            return

        summary = f"{stats.total} cameras"
        if stats.flock_count:
            summary += f" ({stats.flock_count} Flock Safety)"
        if stats.with_direction:
            summary += f"\n{stats.with_direction} with a known facing"
        if stats.near_captures:
            summary += (
                f"\n⚠ {stats.near_captures} within 500 m of your captures/track"
            )
        self._alpr.setText(summary)

        lines = []
        if stats.top_manufacturers:
            lines.append("By manufacturer:")
            lines.extend(f"  {name}: {n}" for name, n in stats.top_manufacturers)
        if stats.top_operators:
            lines.append("By operator:")
            lines.extend(f"  {name}: {n}" for name, n in stats.top_operators)
        self._alpr_breakdown.setText("\n".join(lines) if lines else "")

    def set_stats(self, stats: Stats) -> None:
        self._total.setText(str(stats.total))

        if stats.unlocated_count:
            mapped = f"{stats.located_count} mapped, {stats.unlocated_count} without a location"
        else:
            mapped = f"{stats.located_count} mapped"
        if stats.track_placed_count:
            mapped += f"\n{stats.track_placed_count} placed from the GPS track"
        self._mapped.setText(mapped)

        unique = str(stats.unique_ssids)
        if stats.hidden_ssid_count:
            unique += f" ({stats.hidden_ssid_count} hidden Wi-Fi)"
        self._unique_ssids.setText(unique)

        self._open_count.setText(str(stats.open_count))
        self._wifi_ble.setText(f"{stats.wifi_count} / {stats.ble_count}")

        self._type_bars.set_rows([
            (TYPE_LABELS.get(t, t), n, TYPE_COLORS.get(t, "#8a8a8a"))
            for t, n in sorted(stats.type_breakdown.items(), key=lambda kv: -kv[1])
        ])

        self._enc_bars.set_rows([
            (bucket, stats.enc_breakdown.get(bucket, 0), ENC_COLORS[bucket])
            for bucket in ENC_BUCKETS
            if stats.enc_breakdown.get(bucket, 0)
        ])

        self._band_bars.set_rows([
            (band, n, "#4a7fb5")
            for band, n in sorted(stats.band_breakdown.items(), key=lambda kv: -kv[1])
        ])

        self._code_bars.set_rows([
            (code.title(), n, RISK_COLORS.get(code, "#8a8a8a"))
            for code, n in sorted(stats.code_type_breakdown.items(), key=lambda kv: -kv[1])
        ])

        if stats.subghz_frequencies:
            parts = [
                f"{freq} MHz: {n}" for freq, n in
                sorted(stats.subghz_frequencies.items(), key=lambda kv: -kv[1])
            ]
            self._frequencies.setText(", ".join(parts))
        else:
            self._frequencies.setText("")

        if stats.tracker_breakdown:
            parts = [
                f"{name}: {n}" for name, n in
                sorted(stats.tracker_breakdown.items(), key=lambda kv: -kv[1])
            ]
            self._trackers.setText(f"{stats.tracker_count} total\n" + "\n".join(parts))
        else:
            self._trackers.setText("none identified")

        if stats.follower_count:
            self._followers.setText(
                f"⚠ {stats.follower_count} Bluetooth device(s) seen across "
                "150 m+ of your route (shown ringed in red on the map)"
            )
        else:
            self._followers.setText("none detected")

        if stats.randomized_count:
            self._randomized.setText(
                f"{stats.randomized_count}: these rotate their address, so "
                "repeat passes count them as new devices"
            )
        else:
            self._randomized.setText("none detected")

        if stats.top_vendors:
            self._vendors.setText(
                "\n".join(f"{name}: {n}" for name, n in stats.top_vendors)
            )
        else:
            self._vendors.setText("-")

        if stats.channel_breakdown:
            def sort_key(item):
                ch, _ = item
                return (ch == "unknown", int(ch) if ch != "unknown" else 0)

            parts = [f"ch {ch}: {n}" for ch, n in sorted(stats.channel_breakdown.items(), key=sort_key)]
            self._channels.setText(", ".join(parts))
        else:
            self._channels.setText("-")

        if stats.first_seen_min and stats.first_seen_max:
            self._time_range.setText(f"{stats.first_seen_min} -> {stats.first_seen_max}")
        else:
            self._time_range.setText("unknown")

        if stats.bbox:
            min_lat, min_lon, max_lat, max_lon = stats.bbox
            self._bbox.setText(
                f"lat {min_lat:.5f} to {max_lat:.5f}\nlon {min_lon:.5f} to {max_lon:.5f}"
            )
        else:
            self._bbox.setText("-")

        if stats.area_km2 is not None:
            self._area.setText(f"{stats.area_km2:.3f} km^2")
        else:
            self._area.setText("-")
