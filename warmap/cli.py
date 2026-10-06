"""warmap CLI entry point (launch / open / import-sd / stats / inspect /
export / formats).

Thin dispatch so `warmap ...` works from anywhere once installed (see
install.sh); all the actual logic lives in warmap/app.py, warmap/ingest.py,
warmap/sdcard.py, warmap/stats.py and warmap/export.py.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Iterable

from warmap import __version__


def _expand_paths(paths: Iterable[str]) -> list[Path]:
    """A path argument may be a plain file or a folder. Folders expand to
    every capture file under them, same as the GUI's Open Folder action."""
    from warmap import ingest

    expanded: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found = ingest.expand_paths([path])
            if found.truncated:
                print(f"{path}: bigger than warmap will walk; only the first part was read.")
            expanded.extend(p for p in found if ingest.classify(p) != "unknown")
        else:
            expanded.append(path)
    return expanded


def cmd_launch(args) -> int:
    from warmap.app import main as app_main
    return app_main()


def cmd_open(args) -> int:
    from warmap.app import build_app
    app, window = build_app()
    window.load_initial_data()
    window.import_paths(_expand_paths(args.files))
    window.show()
    return app.exec()


def cmd_import_sd(args) -> int:
    from warmap import sdcard

    found = sdcard.scan_removable_media()
    if not found:
        print(f"No removable media with capture files found under {sdcard.describe_roots()}.")
        return 1

    print(f"Found {len(found)} capture file(s):")
    for p in found:
        print(f"  {p}")

    if not args.yes:
        reply = input("Import all of them into the app? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Cancelled.")
            return 0

    from warmap.app import build_app
    app, window = build_app()
    window.load_initial_data()
    window.import_paths(found)
    window.show()
    return app.exec()


def _load(files, max_gap: int = 300, clock_offset: int = 0):
    from warmap import ingest, parse

    result = ingest.ingest_paths(
        _expand_paths(files), max_gap_seconds=max_gap, clock_offset_seconds=clock_offset
    )
    return result, parse.dedup(result.sightings)


def cmd_stats(args) -> int:
    from warmap import stats as stats_mod
    from warmap.models import TYPE_LABELS

    result, deduped = _load(args.files, args.max_gap, args.clock_offset)
    if not deduped:
        print("No usable rows found.")
        for note in result.notes + result.errors:
            print(f"  {note}")
        return 1

    s = stats_mod.compute_stats(deduped)
    print(f"total records: {s.total}")
    print(f"unique SSIDs: {s.unique_ssids}")
    print(f"open networks: {s.open_count}")
    print(f"wifi / ble: {s.wifi_count} / {s.ble_count}")
    print(f"mapped / unmapped: {s.located_count} / {s.unlocated_count}")
    if s.track_placed_count:
        print(f"placed from GPS track: {s.track_placed_count}")

    if len(s.type_breakdown) > 1:
        print("by type:")
        for record_type, n in sorted(s.type_breakdown.items(), key=lambda kv: -kv[1]):
            print(f"  {TYPE_LABELS.get(record_type, record_type)}: {n}")

    print(f"encryption breakdown: {s.enc_breakdown}")
    print(f"channel distribution: {s.channel_breakdown}")
    if s.band_breakdown:
        print(f"band distribution: {s.band_breakdown}")
    if s.code_type_breakdown:
        print(f"sub-GHz code types: {s.code_type_breakdown}")
    if s.subghz_frequencies:
        print(f"sub-GHz frequencies: {s.subghz_frequencies}")
    if s.tracker_breakdown:
        print(f"bluetooth trackers: {s.tracker_breakdown}")
    if s.randomized_count:
        print(f"randomized addresses: {s.randomized_count}")
    if s.top_vendors:
        top = ", ".join(f"{name} ({n})" for name, n in s.top_vendors[:5])
        print(f"top vendors: {top}")
    if s.first_seen_min:
        print(f"capture time range: {s.first_seen_min} -> {s.first_seen_max}")
    if s.bbox:
        print(f"bounding box: {s.bbox}")
        print(f"approx area covered: {s.area_km2:.3f} km^2")

    for track in result.tracks:
        print(
            f"gps track {Path(track.source).name}: {len(track)} points, "
            f"{track.distance_km():.2f} km"
        )
    for note in result.notes + result.errors:
        print(f"note: {note}")
    return 0


def cmd_inspect(args) -> int:
    """Dump exactly what one file parses to. This is the tool for working out
    why a capture isn't showing up: it prints every field warmap extracted,
    without the map in the way."""
    from warmap import ingest

    path = Path(args.file)
    kind = ingest.classify(path)
    print(f"file: {path}")
    print(f"detected as: {kind}")

    if kind == "unknown":
        print("warmap does not recognize this file type.")
        print("Readable: .csv, .sub, .nfc, .rfid, .ibtn, .ir, .picopass, "
              ".pcap, .pcapng, .gpx, .nmea")
        return 1

    if kind == "pcap":
        from warmap import pcap
        outcome = pcap.read_pcap(path)
        print(f"link type: {outcome.link_type_name}")
        print(f"packets read: {outcome.packets_read}")
        print(f"packets decoded: {outcome.packets_decoded}")
        if outcome.error:
            print(f"couldn't read it: {outcome.error}")
            return 1
        records = outcome.sightings
    elif kind == "track":
        from warmap import gps
        points = gps.parse_track_file(path)
        track = gps.Track(points, source=str(path))
        print(f"points: {len(track)} ({track.timed_count} with timestamps)")
        print(f"distance: {track.distance_km():.3f} km")
        if track.start:
            print(f"time range: {track.start} -> {track.end}")
        if track.bounds():
            print(f"bounds: {track.bounds()}")
        return 0
    else:
        result = ingest.ingest_paths([path])
        records = result.sightings
        for note in result.notes + result.errors:
            print(f"note: {note}")

    print(f"records: {len(records)}")
    for record in records[:args.limit]:
        print()
        for key, value in record.to_dict().items():
            if value in (None, "", {}):
                continue
            print(f"  {key}: {value}")
    if len(records) > args.limit:
        print(f"\n...and {len(records) - args.limit} more (raise --limit to see them)")
    return 0


def cmd_export(args) -> int:
    from warmap import export

    result, deduped = _load(args.files, args.max_gap, args.clock_offset)
    if not deduped:
        print("No usable rows found.")
        return 1

    writers = {
        "csv": export.write_csv,
        "geojson": export.write_geojson,
        "gpx": export.write_gpx,
        "kml": export.write_kml,
        "wigle": export.write_wigle_csv,
    }
    out_path = Path(args.output)
    if args.format == "gpx":
        points = [p for track in result.tracks for p in track.points]
        written = export.write_gpx(deduped, out_path, track_points=points)
    else:
        written = writers[args.format](deduped, out_path)

    skipped = len(deduped) - written
    print(f"wrote {written} record(s) to {out_path}")
    if skipped > 0:
        if args.format == "wigle":
            print(f"{skipped} record(s) left out: WiGLE CSV only takes measured fixes, "
                  "and these have no location or one inferred from a track")
        else:
            print(f"{skipped} record(s) skipped: no location, and {args.format} "
                  "cannot represent a point without one")
    return 0


def cmd_flock(args) -> int:
    """The ALPR/Flock camera overlay: show what's in the current snapshot, or
    refresh it from OpenStreetMap."""
    from warmap import alpr, config

    if args.refresh:
        from datetime import datetime, timezone

        from warmap import alpr_fetch

        out_path = Path(args.output) if args.output else config.FLOCK_USER_PATH
        print("Fetching current ALPR camera set (DeFlock, then OpenStreetMap)...")
        try:
            result = alpr_fetch.fetch_cameras()
            generated = datetime.now(timezone.utc).date().isoformat()
            size = alpr_fetch.write_snapshot(
                result, out_path, generated=generated, force=args.force)
        except alpr_fetch.FetchError as exc:
            print(f"flock: {exc}")
            print("flock: leaving the existing snapshot untouched.")
            return 1
        print(f"wrote {result.count} cameras to {out_path} ({size} bytes, "
              f"source: {result.source_label})")
        for warning in result.errors:
            print(f"  warning: {warning}")

    snapshot = alpr.load_snapshot(Path(args.output) if args.output else config.flock_data_path())
    stats = alpr.camera_stats(snapshot.cameras)
    if not stats.total:
        print("No camera snapshot loaded. Run `warmap flock --refresh` to pull "
              "the current set from OpenStreetMap.")
        return 1
    print(f"ALPR cameras: {stats.total}")
    print(f"Flock Safety: {stats.flock_count}")
    print(f"with a known facing: {stats.with_direction}")
    if snapshot.generated:
        print(f"snapshot date: {snapshot.generated}")
    if stats.top_manufacturers:
        print("top manufacturers:")
        for name, n in stats.top_manufacturers:
            print(f"  {name}: {n}")
    if stats.top_operators:
        print("top operators:")
        for name, n in stats.top_operators[:5]:
            print(f"  {name}: {n}")
    print(snapshot.attribution)
    return 0


def cmd_serve(args) -> int:
    """Serve the mobile app to a phone on your network, from the terminal.

    The GUI has the same thing behind File > Send to phone, with a QR code.
    This is the headless equivalent: it reads the same collected store and the
    same camera snapshot the app would, and serves them read-only until you
    press Ctrl-C.
    """
    import signal

    from warmap import alpr, config, export, server, store
    from warmap.tileproxy import TileCacheProxy

    records = store.load(config.STORE_PATH)
    cameras = alpr.load_snapshot(config.flock_data_path())
    if not records and not args.allow_empty:
        print("Nothing collected yet, so there'd be nothing to look at on the "
              "phone.\nImport a capture first, or pass --allow-empty to serve "
              "the camera overlay on its own.")
        return 1

    proxy = TileCacheProxy()
    proxy.start()

    data = server.DataSource(
        records=lambda: export.to_geojson(records),
        cameras=lambda: alpr.to_transfer(
            cameras.cameras, attribution=cameras.attribution,
            generated=cameras.generated),
        tracks=lambda: None,
        session=lambda: {
            "host": args.name or _hostname(),
            "generated": cameras.generated,
            "counts": {"records": len(records), "cameras": len(cameras.cameras)},
        },
    )

    srv = server.WarmapServer(data=data, tile_proxy=proxy, port=args.port)
    info = srv.start()

    print()
    print("  warmap is serving to your network.")
    print()
    print(f"    {info.url}")
    print()
    if not args.no_qr:
        _print_qr(info.url)
    print(f"  {len(records)} record(s), {len(cameras.cameras)} camera(s).")
    print("  Anyone on this network who has the full link above can view this "
          "data.\n  Press Ctrl-C to stop.")
    print()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        srv.stop()
        proxy.stop()
        print("\nStopped serving.")
    return 0


def offline_default_radius() -> int:
    """Imported lazily so `warmap --help` doesn't pull in the offline module."""
    from warmap.offline import DEFAULT_CAMERA_RADIUS_M
    return DEFAULT_CAMERA_RADIUS_M


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname()
    except OSError:
        return "this PC"


def _print_qr(url: str) -> None:
    """A QR in the terminal, if qrencode is around. Purely a convenience:
    the URL above it is the real answer, so a missing tool is not an error."""
    import shutil
    import subprocess

    exe = shutil.which("qrencode")
    if not exe:
        return
    try:
        out = subprocess.run(
            [exe, "-t", "ANSIUTF8", "-m", "2", url],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if out.returncode == 0 and out.stdout:
        print(out.stdout)


def cmd_offline(args) -> int:
    """Write the single-file mobile app, data included."""
    from warmap import alpr, config, export, offline, store

    if args.files:
        result, records = _load(args.files, args.max_gap, args.clock_offset)
        tracks = result.tracks
    else:
        records = store.load(config.STORE_PATH)
        tracks = []
    if not records:
        print("Nothing to save yet: nothing has been collected on this machine.")
        print("Point me at your captures:  warmap offline <file or folder> -o out.html")
        print("or import some first with:  warmap open <file or folder>  /  warmap import-sd")
        return 1

    records_geojson = export.to_geojson(records)
    tracks_geojson = None
    if tracks:
        tracks_geojson = {"type": "FeatureCollection",
                          "features": [t.to_geojson() for t in tracks]}

    snapshot = alpr.load_snapshot(config.flock_data_path())
    if args.all_cameras:
        cameras = snapshot.cameras
    else:
        cameras = offline.nearby_cameras(
            snapshot.cameras, records_geojson, tracks_geojson,
            meters=args.camera_radius if args.camera_radius is not None else offline_default_radius())

    bundle = offline.build(
        records_geojson,
        cameras_transfer=alpr.to_transfer(
            cameras, attribution=snapshot.attribution,
            generated=snapshot.generated),
        tracks_geojson=tracks_geojson,
        session={"host": args.name or "", "generated": snapshot.generated},
    )
    written = offline.write(bundle, Path(args.output))
    print(f"wrote {args.output} ({written / 1_000_000:.2f} MB)")
    print(f"  {bundle.records} record(s), {bundle.cameras} camera(s) embedded")
    print("  Copy it to a phone and open it from the downloads folder. No PC "
          "and no network needed; only the map background wants a connection.")
    return 0


def cmd_doctor(args) -> int:
    from warmap import doctor

    return doctor.run(check_map_engine=args.map, as_json=args.json)


def cmd_formats(args) -> int:
    """What warmap can read. Printed rather than buried in the README because
    "will it read X" is the first question every time."""
    from warmap.flipper import FLIPPER_EXTENSIONS
    from warmap.models import TYPE_LABELS
    from warmap.pcap import DLT_NAMES, BLE_DLTS, WIFI_DLTS

    print("Wardrive CSV")
    print("  WigleWifi-1.4 and 1.6, plus Kismet and generic lat/lon exports.")
    print("  ESP32 Marauder writes 1.4 with WIFI and BLE rows; the WiGLE app")
    print("  writes 1.6, which adds Frequency/RCOIs/MfgrId and the BT and")
    print("  cellular row types. Both load.")
    print("  Not always a .csv: the Flipper Marauder companion app saves to")
    print("  apps_data/marauder/dumps/wardrive_0.txt and a standalone Marauder")
    print("  writes wardrive_0.log. Both are found by content, not extension.")
    print()
    print("Flipper Zero captures")
    for extension, record_type in sorted(FLIPPER_EXTENSIONS.items()):
        print(f"  {extension:<11} {TYPE_LABELS.get(record_type, record_type)}")
    print("  These carry no coordinates. Load a GPS track from the same")
    print("  session and warmap places them by timestamp.")
    print()
    print("Packet captures (.pcap, .pcapng)")
    for dlt in sorted(WIFI_DLTS | BLE_DLTS):
        print(f"  DLT {dlt:<4} {DLT_NAMES.get(dlt, 'unknown')}")
    print()
    print("GPS tracks")
    print("  .nmea, .gpx, and .log/.txt files containing NMEA sentences")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="warmap",
        description="A native viewer for wardriving and Flipper Zero captures.",
        epilog="With no command, opens the map window.",
    )
    parser.add_argument("--version", action="version", version=f"warmap {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    def add_placement_args(p):
        p.add_argument(
            "--max-gap", type=int, default=300, metavar="SECONDS",
            help="how far in time a capture may sit from a GPS track point and "
                 "still be placed (default: 300)",
        )
        p.add_argument(
            "--clock-offset", type=int, default=0, metavar="SECONDS",
            help="correction for a Flipper whose clock was wrong; positive "
                 "means its clock read later than real time",
        )

    o = sub.add_parser("open", help="launch and load specific capture file(s)/folder(s)")
    o.add_argument("files", nargs="+", help="file(s) or folder(s) to load")
    o.set_defaults(func=cmd_open)

    i = sub.add_parser("import-sd", help="scan removable media for captures and import them")
    i.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    i.set_defaults(func=cmd_import_sd)

    s = sub.add_parser("stats", help="print summary stats for file(s)/folder(s), no window")
    s.add_argument("files", nargs="+", help="file(s) or folder(s) to summarize")
    add_placement_args(s)
    s.set_defaults(func=cmd_stats)

    n = sub.add_parser("inspect", help="dump everything warmap parsed out of one file")
    n.add_argument("file", help="the file to inspect")
    n.add_argument("--limit", type=int, default=10, help="how many records to print")
    n.set_defaults(func=cmd_inspect)

    e = sub.add_parser("export", help="convert capture(s) to another format, no window")
    e.add_argument("files", nargs="+", help="file(s) or folder(s) to read")
    e.add_argument("-o", "--output", required=True, help="path to write")
    e.add_argument(
        "-f", "--format", default="geojson",
        choices=("csv", "geojson", "gpx", "kml", "wigle"),
        help="output format (default: geojson)",
    )
    add_placement_args(e)
    e.set_defaults(func=cmd_export)

    f = sub.add_parser("formats", help="list every file format warmap can read")
    f.set_defaults(func=cmd_formats)

    fl = sub.add_parser("flock", help="show or refresh the DeFlock/Flock ALPR camera overlay")
    fl.add_argument("--refresh", action="store_true",
                    help="pull the current camera set from DeFlock (falls back to OpenStreetMap)")
    fl.add_argument("-o", "--output", metavar="PATH",
                    help="snapshot path to write/read (default: the user data dir)")
    fl.add_argument("--force", action="store_true",
                    help="overwrite even if the new set is much smaller than the current one")
    fl.set_defaults(func=cmd_flock)

    dr = sub.add_parser("doctor", help="print what this install is and where its data lives")
    dr.add_argument("--map", action="store_true",
                    help="also start the map engine offscreen to prove it works")
    dr.add_argument("--json", action="store_true", help="machine-readable output")
    dr.set_defaults(func=cmd_doctor)

    sv = sub.add_parser("serve", help="serve the mobile app to a phone on your network")
    sv.add_argument("--port", type=int, default=0,
                    help="port to listen on (default: pick a free one)")
    sv.add_argument("--name", help="what to call this PC in the phone's menu")
    sv.add_argument("--no-qr", action="store_true", help="don't print a QR code")
    sv.add_argument("--allow-empty", action="store_true",
                    help="serve even with no captures collected yet")
    sv.set_defaults(func=cmd_serve)

    off = sub.add_parser(
        "offline", help="write a single-file copy of the app for a phone")
    off.add_argument("files", nargs="*",
                     help="capture file(s)/folder(s); defaults to the collected store")
    off.add_argument("-o", "--output", default="warmap-offline.html",
                     help="where to write (default: warmap-offline.html)")
    off.add_argument("--name", default="",
                     help="a name for this PC to show in the phone's menu (default: none, "
                          "since the file may be shared)")
    off.add_argument("--all-cameras", action="store_true",
                     help="embed every ALPR camera, not just those near your data")
    off.add_argument("--camera-radius", type=int, default=None,
                     help="how far from your records to include cameras, in metres")
    add_placement_args(off)
    off.set_defaults(func=cmd_offline)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_launch(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
