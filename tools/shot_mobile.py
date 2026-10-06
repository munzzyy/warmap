"""Screenshot the mobile app the way a phone would see it.

Same reasoning as tools/shot.py: the Python tests and the node map.js tests
between them still never render the thing, and a bottom sheet covering the
map or a control under the notch passes every test and is obvious the instant
you look. This starts a real warmap server, points a phone-sized QWebEngineView
at it, drives the UI, and saves PNGs.

    python3 tools/shot_mobile.py
    python3 tools/shot_mobile.py --out-dir /tmp/shots --width 412 --height 915
    python3 tools/shot_mobile.py --steps map,sheet,detail,filters,stats,menu,light

Sizes default to a Pixel-class viewport (412x915 CSS px). The server it starts
binds to loopback only, so nothing is exposed to the network by taking a
screenshot.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def build_dataset(sample: bool):
    """The data to serve: the real collected store, or the bundled sample."""
    from warmap import alpr, config, export, ingest, parse, store

    if sample:
        result = ingest.ingest_paths([config.SAMPLE_DIR])
        records = parse.dedup(result.sightings)
        tracks = result.tracks
    else:
        records = store.load(config.STORE_PATH)
        tracks = []
    cameras = alpr.load_snapshot(config.flock_data_path())

    def tracks_payload():
        if not tracks:
            return None
        return {"type": "FeatureCollection",
                "features": [t.to_geojson() for t in tracks]}

    from warmap import server as server_mod
    return server_mod.DataSource(
        records=lambda: export.to_geojson(records),
        cameras=lambda: alpr.to_transfer(
            cameras.cameras, attribution=cameras.attribution,
            generated=cameras.generated),
        tracks=tracks_payload,
        session=lambda: {
            "host": "screenshot",
            "generated": cameras.generated,
            "counts": {"records": len(records), "cameras": len(cameras.cameras)},
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot the warmap mobile app.")
    parser.add_argument("--out-dir", default="/tmp/warmap-mobile")
    parser.add_argument("--width", type=int, default=412)
    parser.add_argument("--height", type=int, default=915)
    parser.add_argument("--wait", type=float, default=9.0,
                        help="seconds to let the map and data load before the first shot")
    parser.add_argument("--steps", default="map,sheet,detail,filters,stats,menu",
                        help="comma-separated screens to capture")
    parser.add_argument("--real-store", action="store_true",
                        help="serve the real collected store instead of the bundled sample")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    from warmap import server as server_mod
    from warmap.tileproxy import TileCacheProxy

    app = QApplication.instance() or QApplication(sys.argv[:1])

    proxy = TileCacheProxy()
    proxy.start()
    # Loopback only: a screenshot must not put anything on the network.
    srv = server_mod.WarmapServer(
        data=build_dataset(sample=not args.real_store),
        tile_proxy=proxy, host="127.0.0.1", port=0,
    )
    info = srv.start()
    print(f"serving {info.url}")

    view = QWebEngineView()
    view.resize(args.width, args.height)
    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
    view.load(QUrl(info.url))
    view.show()

    errors: list[str] = []
    page = view.page()

    def on_console(level, message, line, source):  # noqa: ARG001
        text = f"{message} ({source}:{line})"
        print(f"  [js] {text}")
        if "rror" in str(level) or "Error" in message:
            errors.append(text)

    page.javaScriptConsoleMessage = on_console  # type: ignore[assignment]

    def js(script: str) -> None:
        page.runJavaScript(script)

    def grab(name: str) -> None:
        path = out_dir / f"{name}.png"
        view.grab().save(str(path))
        print(f"wrote {path}")

    # A script per step, run in order with a gap between so the UI settles.
    # Each pane step opens the sheet as well as selecting the tab: selecting a
    # tab while the sheet is collapsed leaves the right pane active but
    # entirely below the fold, which photographs as "the click didn't work".
    def open_tab(tab: str) -> str:
        return (
            f"document.querySelector('[data-tab={tab}]').click();"
            "document.getElementById('sheet').dataset.state='half';"
            "document.getElementById('fabs').style.bottom='calc(47vh + 12px)';"
        )

    actions = {
        "map": ("", "01-map"),
        "sheet": ("document.getElementById('sheet').dataset.state='half';"
                  "document.getElementById('fabs').style.bottom='calc(47vh + 12px)';", "02-sheet"),
        "records": (open_tab("records"), "03-records"),
        "detail": (open_tab("records") +
                   "var r=document.querySelector('#record-list .row'); if(r) r.click();",
                   "04-detail"),
        "filters": (open_tab("filters"), "05-filters"),
        "stats": (open_tab("stats"), "06-stats"),
        "menu": ("document.getElementById('btn-menu').click();", "07-menu"),
        "light": ("document.getElementById('btn-theme').click();"
                  "document.getElementById('scrim').click();", "08-light"),
    }

    queue = []
    for step in steps:
        if step in actions:
            queue.append(actions[step])
        else:
            print(f"(unknown step {step!r}, skipping)", file=sys.stderr)

    delay = int(args.wait * 1000)
    for script, name in queue:
        if script:
            QTimer.singleShot(delay, lambda s=script: js(s))
            delay += 700
        QTimer.singleShot(delay, lambda n=name: grab(n))
        delay += 900

    def finish():
        if errors:
            print("\nJS errors:", file=sys.stderr)
            for e in errors:
                print("  " + e, file=sys.stderr)
        srv.stop()
        proxy.stop()
        app.quit()

    QTimer.singleShot(delay + 500, finish)
    app.exec()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
