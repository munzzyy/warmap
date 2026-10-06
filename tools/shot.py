"""Screenshot the real running app.

Tests exercise the Python side and cross-check the constants baked into
map.js, but nothing in the suite ever executes map.js itself. A typo in the
JS, a broken layer, a legend that renders on top of the controls: all of that
passes every test and is obvious the moment you look at the window. So this
launches the app for real, loads whatever capture you point it at, waits for
the web view to actually paint, and saves a PNG.

    python3 tools/shot.py                       # sample data
    python3 tools/shot.py --out /tmp/a.png ~/captures
    python3 tools/shot.py --records             # with the records dock open

Runs on the real display by default because an offscreen QWebEngineView grabs
as a blank rectangle: the page renders in a separate process that never
composites into the widget. Pass --offscreen if you only need to prove the Qt
chrome builds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot the warmap window.")
    parser.add_argument("paths", nargs="*", help="capture file(s)/folder(s) to load")
    parser.add_argument("--out", default="/tmp/warmap-shot.png", help="where to write the PNG")
    parser.add_argument("--wait", type=float, default=6.0,
                        help="seconds to let the map render before grabbing")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--records", action="store_true", help="open the records dock")
    parser.add_argument("--heatmap", action="store_true", help="turn the heatmap on")
    parser.add_argument("--offscreen", action="store_true",
                        help="run headless (the map area will be blank)")
    parser.add_argument("--real-store", action="store_true",
                        help="use the real ~/.local/share/warmap store instead "
                             "of a throwaway one. Off by default, because "
                             "importing a capture persists it: a screenshot "
                             "must never merge test data into a real session.")
    parser.add_argument("--focus", metavar="TYPE",
                        help="open the popup of the first record of this type "
                             "(WIFI, BLE, SUBGHZ, NFC, etc.), the only way to "
                             "see the popup rendering without clicking")
    args = parser.parse_args()

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    # Isolate the data/config dirs before warmap.config is imported (it reads
    # these env vars at import time). A screenshot run then loads and writes an
    # empty throwaway store, so it can never pollute the real collected.json.
    if not args.real_store:
        import tempfile
        sandbox = tempfile.mkdtemp(prefix="warmap-shot-")
        os.environ["WARMAP_DATA_DIR"] = sandbox
        os.environ["WARMAP_CONFIG_DIR"] = sandbox

    from PySide6.QtCore import QTimer
    from warmap.app import build_app

    app, window = build_app()
    window.resize(args.width, args.height)

    # With paths given, show exactly those: don't fold in the bundled sample
    # (or, with --real-store, the live session) so the shot is only the capture
    # under test.
    if args.paths:
        window.import_paths([Path(p) for p in args.paths])
    else:
        window.load_initial_data()
    if args.records:
        window.records_dock.setVisible(True)
    if args.heatmap:
        window.heatmap_act.setChecked(True)

    window.show()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def capture():
        window.grab().save(str(out))
        counts: dict = {}
        for record in window._all_aps:
            counts[record.type] = counts.get(record.type, 0) + 1
        print(f"wrote {out}")
        print(f"records: {sum(counts.values())} {counts}")
        print(f"tracks: {len(window._tracks)}")
        app.quit()

    if args.focus:
        wanted = args.focus.upper()
        match = next((r for r in window._all_aps
                      if r.type == wanted and r.has_location), None)
        if match is None:
            print(f"no located {wanted} record to focus", file=sys.stderr)
        else:
            print(f"focusing {match.type} {match.bssid} ({match.ssid})")
            QTimer.singleShot(
                int(args.wait * 1000) - 1500,
                lambda: window.map_view.focus_record(match.type, match.bssid),
            )

    # A single delay rather than polling: the map has to finish loading
    # Leaflet, run warmapLoadData, and paint tiles, and none of that reports
    # completion back to Python.
    QTimer.singleShot(int(args.wait * 1000), capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
