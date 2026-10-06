"""Export the currently-filtered set: CSV, GeoJSON, GPX or KML. Pure
functions: the caller (mainwindow) decides the path and which set (filtered
vs. all) to hand in.

Records with no location are written to CSV, which is a table and can say
"no location" in a column, and left out of the three geographic formats,
which cannot represent a point that has no point. Each writer returns how
many records it actually wrote so the UI can tell you when those two numbers
differ.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from warmap.models import (
    GEO_TRACK,
    TYPE_BLE,
    TYPE_BT,
    TYPE_CELL,
    TYPE_WIFI,
    Sighting,
)

# The record types the Wigle CSV format can represent. Everything else is
# warmap's own and is dropped by the Wigle exporter rather than mislabelled.
WIGLE_TYPES = frozenset({TYPE_WIFI, TYPE_BLE, TYPE_BT, TYPE_CELL})

CSV_FIELDS = [
    "bssid", "ssid", "type", "auth_mode", "enc_bucket", "first_seen", "channel",
    "frequency", "rssi", "lat", "lon", "altitude", "accuracy", "times_seen",
    "vendor", "geo_source", "source", "meta",
]


def public_dict(sighting: Sighting) -> dict:
    """`to_dict()` for anything that leaves this machine: the source becomes
    the file's name alone, since the full path says who you are and how your
    disk is laid out, and neither belongs in a file you hand to someone."""
    d = sighting.to_dict()
    if d.get("source"):
        d["source"] = Path(str(d["source"])).name
    return d


def _row(sighting: Sighting) -> dict:
    d = public_dict(sighting)
    # A dict in a CSV cell is unreadable; JSON at least round-trips.
    d["meta"] = json.dumps(d["meta"], sort_keys=True) if d["meta"] else ""
    return d


def to_csv_rows(sightings: Iterable[Sighting]) -> list[dict]:
    return [_row(s) for s in sightings]


def write_csv(sightings: Iterable[Sighting], path: Path) -> int:
    sightings = list(sightings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for s in sightings:
            writer.writerow(_row(s))
    return len(sightings)


def to_geojson(sightings: Iterable[Sighting]) -> dict:
    """A FeatureCollection of the located records. This is also what gets
    handed to the map, so the properties here are exactly what the popup and
    the marker styling have to work with."""
    features = []
    for s in sightings:
        if not s.has_location:
            continue
        d = public_dict(s)
        lat, lon = d.pop("lat"), d.pop("lon")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": d,
        })
    return {"type": "FeatureCollection", "features": features}


def write_geojson(sightings: Iterable[Sighting], path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_geojson(sightings)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return len(payload["features"])


def to_gpx(sightings: Iterable[Sighting], track_points: Iterable = ()) -> str:
    """GPX with one waypoint per located record, plus the GPS track itself if
    one is supplied, which is what makes the export useful in any mapping
    tool rather than just a pile of points."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="warmap" xmlns="http://www.topografix.com/GPX/1/1">',
    ]
    written = 0
    for s in sightings:
        if not s.has_location:
            continue
        written += 1
        lines.append(f'  <wpt lat="{s.lat:.7f}" lon="{s.lon:.7f}">')
        if s.altitude is not None:
            lines.append(f"    <ele>{s.altitude:.2f}</ele>")
        lines.append(f"    <name>{escape(s.display_name)}</name>")
        detail = f"{s.type} {s.bssid}"
        if s.enc_bucket:
            detail += f" / {s.enc_bucket}"
        if s.rssi:
            detail += f" / {s.rssi} dBm"
        # GPX has no field for where a coordinate came from, and a waypoint
        # that was inferred from a track must not leave here looking like a
        # recorded fix. The description is the only place to say so.
        if s.geo_source == GEO_TRACK:
            detail += " / position inferred from GPS track, not a recorded fix"
        lines.append(f"    <desc>{escape(detail)}</desc>")
        lines.append("    <sym>Dot</sym>")
        lines.append("  </wpt>")

    points = list(track_points)
    if points:
        lines.append("  <trk><name>warmap track</name><trkseg>")
        for p in points:
            lines.append(f'    <trkpt lat="{p.lat:.7f}" lon="{p.lon:.7f}">')
            if p.altitude is not None:
                lines.append(f"      <ele>{p.altitude:.2f}</ele>")
            if p.when is not None:
                lines.append(f"      <time>{p.when.strftime('%Y-%m-%dT%H:%M:%SZ')}</time>")
            lines.append("    </trkpt>")
        lines.append("  </trkseg></trk>")

    lines.append("</gpx>")
    return "\n".join(lines)


def write_gpx(sightings: Iterable[Sighting], path: Path, track_points: Iterable = ()) -> int:
    sightings = list(sightings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_gpx(sightings, track_points), encoding="utf-8")
    return sum(1 for s in sightings if s.has_location)


def to_kml(sightings: Iterable[Sighting]) -> str:
    """KML for Google Earth. Records are grouped into one folder per record
    type so a capture with Wi-Fi, BLE and Sub-GHz in it stays legible."""
    grouped: dict[str, list] = {}
    for s in sightings:
        if not s.has_location:
            continue
        grouped.setdefault(s.type, []).append(s)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document><name>warmap export</name>",
    ]
    for record_type, group in sorted(grouped.items()):
        lines.append(f"<Folder><name>{escape(record_type)} ({len(group)})</name>")
        for s in group:
            description = "\n".join(
                f"{k}: {v}" for k, v in public_dict(s).items()
                if v not in (None, "", {}) and k not in ("lat", "lon")
            )
            lines.append("  <Placemark>")
            lines.append(f"    <name>{escape(s.display_name)}</name>")
            lines.append(f"    <description>{escape(description)}</description>")
            altitude = s.altitude if s.altitude is not None else 0
            lines.append(
                f"    <Point><coordinates>{s.lon:.7f},{s.lat:.7f},{altitude:.1f}"
                "</coordinates></Point>"
            )
            lines.append("  </Placemark>")
        lines.append("</Folder>")
    lines.append("</Document></kml>")
    return "\n".join(lines)


def write_kml(sightings: Iterable[Sighting], path: Path) -> int:
    sightings = list(sightings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_kml(sightings), encoding="utf-8")
    return sum(1 for s in sightings if s.has_location)


def write_wigle_csv(sightings: Iterable[Sighting], path: Path) -> int:
    """Export back out in Wigle 1.6 format, so a session collected across
    several sources can be uploaded or fed to another wardriving tool.

    Only the record types Wigle actually defines are written. A Sub-GHz key
    or an NFC card has no meaning in this format, and writing one with
    `Type: SUBGHZ` produces a file that other wardriving tools read back as a
    Wi-Fi access point.

    Records whose position was inferred from a GPS track are also left out.
    This format exists to be uploaded and traded between wardriving tools, it
    has no column for where a coordinate came from, and a guessed position
    submitted as a measured one is bad data in someone else's database as well
    as your own. Use the CSV or GeoJSON export to keep everything.
    """
    sightings = [
        s for s in sightings
        if s.has_location and s.type in WIGLE_TYPES and s.geo_source != GEO_TRACK
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "Frequency", "RSSI",
        "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters",
        "RCOIs", "MfgrId", "Type",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("WigleWifi-1.6,appRelease=warmap,model=warmap,release=,"
                "device=warmap,display=,board=,brand=warmap,star=Sol,body=3,subBody=0\n")
        writer = csv.writer(f)
        writer.writerow(header)
        for s in sightings:
            writer.writerow([
                s.bssid, s.ssid, s.auth_mode, s.first_seen,
                s.channel if s.channel is not None else "",
                f"{s.frequency:g}" if s.frequency is not None else "",
                s.rssi, f"{s.lat:.7f}", f"{s.lon:.7f}",
                f"{s.altitude:.2f}" if s.altitude is not None else "",
                f"{s.accuracy:.2f}" if s.accuracy is not None else "",
                s.meta.get("rcois", ""), s.meta.get("mfgr_id", ""), s.type,
            ])
    return len(sightings)
