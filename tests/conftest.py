"""Shared test setup, mostly about keeping Qt from taking the process down
with it.

Two things have to happen before PySide6 is imported anywhere: the platform
has to be `offscreen` (there's no display on a CI box) and QtWebEngine has to
be told not to reach for a GPU or a sandbox. Setting them in individual test
modules works only if that module happens to be imported first, so they live
here instead.

The teardown matters as much as the setup. A QWebEngineView owns child
Chromium processes, and if the interpreter tears down while those are still
live the process dies with a segfault *after* every test has already passed.
That reads as a failed run in CI and sends you looking for a test bug that
doesn't exist. Closing the widgets and pumping the deferred-delete queue while
the QApplication is still alive avoids it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

# Don't load the real 16 MB / 137k-camera ALPR snapshot in every test that
# builds a MainWindow, since it's slow and irrelevant to most of them. Point the
# overlay at a path that doesn't exist so it loads nothing by default; the
# ALPR tests set WARMAP_FLOCK_PATH to their own small fixture.
os.environ.setdefault("WARMAP_FLOCK_PATH", "/nonexistent/warmap-no-flock-snapshot.json")

import pytest


@pytest.fixture(scope="session", autouse=True)
def _qt_application():
    """One QApplication for the whole session, shut down in the right order.

    Held in a module-level name as well as yielded: a QApplication that gets
    garbage collected while Qt still holds references to it is its own source
    of interpreter-shutdown crashes.
    """
    import sys

    from PySide6.QtCore import QEvent, QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    globals()["_APP"] = app

    yield app

    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()

    # Deferred deletes are what actually tear down the web engine's child
    # processes, and they only run when the event loop gets a turn.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
