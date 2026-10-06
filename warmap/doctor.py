"""`warmap doctor`: what this install is, where its data lives, and whether
the map engine starts. The first thing to run when something is off, and
what the release build runs on each platform to prove the bundle works.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

from warmap import __version__, config


def _size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "missing"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _snapshot_summary(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return "missing"
    generated = count = None
    for key in ("generated", "count"):
        marker = f'"{key}":'
        at = head.find(marker)
        if at < 0:
            continue
        raw = head[at + len(marker):].lstrip()
        token = raw.split(",", 1)[0].strip().strip('"')
        if key == "generated":
            generated = token
        else:
            count = token
    if count is None:
        return f"{_size(path)}, header unreadable"
    return f"{count} cameras, generated {generated}, {_size(path)}"


def check_map(timeout_seconds: float = 30.0) -> tuple[bool, str]:
    """Start Qt offscreen if nothing else asked for a platform, load the real
    map page in a QWebEngineView and wait for it to finish. This is the step
    that fails when a bundle is missing WebEngine's helper process or its
    resources, which no import check can see."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
    try:
        from PySide6.QtCore import QCoreApplication, QEvent, QTimer, QUrl
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        return False, f"PySide6 WebEngine import failed: {exc}"

    app = QApplication.instance() or QApplication(sys.argv[:1])
    view = QWebEngineView()
    outcome: dict = {}

    def finished(ok: bool) -> None:
        outcome["ok"] = ok
        app.quit()

    def timed_out() -> None:
        outcome["ok"] = False
        outcome["why"] = f"no loadFinished within {timeout_seconds:.0f} s"
        app.quit()

    view.loadFinished.connect(finished)
    QTimer.singleShot(int(timeout_seconds * 1000), timed_out)
    view.load(QUrl.fromLocalFile(str(config.MAP_HTML)))
    view.resize(800, 600)
    view.show()
    app.exec()
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    if outcome.get("ok"):
        return True, "map page loaded in QWebEngineView"
    return False, outcome.get("why", "map page failed to load")


def run(check_map_engine: bool = False, as_json: bool = False) -> int:
    report: dict = {
        "warmap": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "frozen": bool(getattr(sys, "frozen", False)),
        "package_dir": str(config.PACKAGE_DIR),
        "data_dir": str(config.DATA_DIR),
        "config_dir": str(config.CONFIG_DIR),
        "store": _size(config.STORE_PATH),
        "tile_cache": str(config.TILE_CACHE_DIR),
        "camera_snapshot": f"{config.flock_data_path()}: {_snapshot_summary(config.flock_data_path())}",
        "sample": "present" if config.SAMPLE_CSV.exists() else "missing",
        "map_page": "present" if config.MAP_HTML.exists() else "missing",
        "phone_app": "present" if (config.WEBAPP_DIR / "index.html").exists() else "missing",
        "node": shutil.which("node") or "not found (only the JS tests need it)",
    }
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        report["pyside6"] = PySide6.__version__
        report["qt"] = qVersion()
    except ImportError as exc:
        report["pyside6"] = f"import failed: {exc}"
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401

        report["webengine"] = "importable"
    except ImportError as exc:
        report["webengine"] = f"import failed: {exc}"

    ok = True
    for key in ("sample", "map_page", "phone_app"):
        ok = ok and report[key] == "present"
    ok = ok and report.get("webengine") == "importable"

    if check_map_engine:
        map_ok, why = check_map()
        report["map_engine"] = why
        ok = ok and map_ok

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        width = max(len(k) for k in report)
        for key, value in report.items():
            print(f"{key.ljust(width)}  {value}")
        print()
        print("everything looks fine" if ok else "something is missing; see above")
    return 0 if ok else 1
