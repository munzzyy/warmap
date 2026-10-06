"""File > Send to phone: the dialog that hands this session to the mobile app.

Starts `warmap.server` while it's open, shows the URL as a QR code to point a
camera at, and stops the server again on close. The server is never left
running behind the user's back: closing the dialog is what stops it, and the
window title bar says so.

The security note in the dialog is deliberate rather than decorative. This is
the one feature in warmap that accepts a connection from another machine, and
the person using it should know what that means before they scan anything: the
link works for anything on the same network that has it, and it stops working
the moment the dialog closes.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

QR_PIXELS = 260


def qr_png(text: str) -> Optional[bytes]:
    """A QR code PNG for `text`, or None if qrencode isn't installed.

    Shelling out to qrencode rather than taking a Python QR dependency: it's
    already on this machine, warmap has no third-party runtime deps, and a
    missing tool degrades to "read the URL" rather than to an error.
    """
    exe = shutil.which("qrencode")
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "-t", "PNG", "-o", "-", "-s", "8", "-m", "2", "--", text],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout:
        return None
    return done.stdout


class PhoneDialog(QDialog):
    def __init__(self, server, parent=None):
        super().__init__(parent)
        self.server = server
        self.setWindowTitle("Send to phone")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        info = self.server.info()

        heading = QLabel("Open warmap on your phone")
        heading.setProperty("cssClass", "dialogHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Scan this with your phone's camera while it's on the same wifi. "
            "The phone gets everything this window is showing (your captures, "
            "the GPS track and the camera overlay) and keeps a copy so it "
            "still works when you close this."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        png = qr_png(info.url)
        if png:
            pix = QPixmap()
            # No format argument: Qt sniffs it from the data, and the overload
            # that takes one wants bytes rather than a str, which is an easy
            # way to turn a working dialog into a ValueError.
            pix.loadFromData(png)
            qr = QLabel()
            qr.setPixmap(pix.scaled(
                QR_PIXELS, QR_PIXELS,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # A QR is black-on-white and has to stay that way to scan, so it
            # gets its own white plate rather than inheriting the dark theme.
            qr.setStyleSheet("background: #ffffff; padding: 10px; border-radius: 8px;")
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(qr)
            row.addStretch()
            layout.addLayout(row)
        else:
            layout.addWidget(QLabel(
                "Install `qrencode` to get a scannable code here. "
                "The address below works either way."
            ))

        url_label = QLabel(info.url)
        url_label.setWordWrap(True)
        url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        url_label.setStyleSheet("font-family: monospace; font-size: 12px;")
        layout.addWidget(url_label)

        copy_btn = QPushButton("Copy link")
        copy_btn.clicked.connect(lambda: self._copy(info.url))
        layout.addWidget(copy_btn)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        self.status = QLabel("Waiting for the phone to connect...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        note = QLabel(
            "While this is open, anything on your network that has the full "
            "link can read this data. The link is random and stops working as "
            "soon as you close this window."
        )
        note.setWordWrap(True)
        note.setProperty("cssClass", "dialogNote")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        # Poll the server's own hit counter so the dialog can confirm the
        # phone actually reached it. The most common failure here is a
        # network that isolates devices from each other, and silence looks
        # identical to "it worked".
        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def _copy(self, url: str) -> None:
        clip = QGuiApplication.clipboard()
        if clip is not None:
            clip.setText(url)
            self.status.setText("Link copied.")

    def _refresh(self) -> None:
        hits = getattr(self.server, "hits", 0)
        if hits:
            who = getattr(self.server, "last_client", "") or "your phone"
            self.status.setText(f"Connected: {who} has loaded the app.")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)
