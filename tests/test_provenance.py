"""The load-bearing promise: a position that was inferred is never presented
as a position that was measured, and a guess is never stated over the top of
ground truth.

Every case here is a defect that was actually shipped and caught by an
independent review pass, which is why they're grouped together rather than
filed under the module each one touches.
"""

from __future__ import annotations

import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from warmap import ble, config, export, pcap, store
from warmap.models import GEO_DIRECT, GEO_NONE, GEO_TRACK, Sighting


def _record(**kw) -> Sighting:
    defaults = dict(
        bssid="AA:BB:CC:DD:EE:01", ssid="Net", auth_mode="", enc_bucket="WPA2",
        first_seen="2026-06-14 09:00:00", channel=6, rssi=-55, lat=33.0, lon=-112.0,
        altitude=340.0, accuracy=5.0, type="WIFI", frequency=2437.0, meta={},
    )
    defaults.update(kw)
    return Sighting(**defaults)


# --- exports must not erase where a coordinate came from -----------------


def test_gpx_says_when_a_position_was_inferred(tmp_path):
    """GPX has no provenance field, so it has to go in the description. Two
    records that differ only in geo_source must not export identically."""
    measured = tmp_path / "measured.gpx"
    inferred = tmp_path / "inferred.gpx"
    export.write_gpx([_record(geo_source=GEO_DIRECT)], measured)
    export.write_gpx([_record(geo_source=GEO_TRACK)], inferred)

    assert measured.read_text() != inferred.read_text()
    assert "inferred" in inferred.read_text().lower()
    assert "inferred" not in measured.read_text().lower()


def test_wigle_export_leaves_out_inferred_positions(tmp_path):
    """Wigle CSV is the upload/interop format and has no provenance column.
    A guessed coordinate submitted as a measured one is bad data in someone
    else's database as well as this one."""
    path = tmp_path / "wigle.csv"
    written = export.write_wigle_csv(
        [_record(bssid="AA:BB:CC:DD:EE:01", geo_source=GEO_DIRECT),
         _record(bssid="AA:BB:CC:DD:EE:02", geo_source=GEO_TRACK)],
        path,
    )
    assert written == 1
    assert "AA:BB:CC:DD:EE:02" not in path.read_text()


def test_csv_and_geojson_still_keep_everything(tmp_path):
    """The two lossless formats must not start dropping things. That's what
    you use when you want the whole picture."""
    records = [_record(bssid="A", geo_source=GEO_DIRECT),
               _record(bssid="B", geo_source=GEO_TRACK)]
    csv_path = tmp_path / "out.csv"
    assert export.write_csv(records, csv_path) == 2
    assert "track" in csv_path.read_text()
    assert len(export.to_geojson(records)["features"]) == 2


def test_kml_carries_provenance(tmp_path):
    path = tmp_path / "out.kml"
    export.write_kml([_record(geo_source=GEO_TRACK)], path)
    assert "track" in path.read_text()


# --- ground truth beats a guess ------------------------------------------


def _mac_bytes(mac: str) -> bytes:
    return bytes(int(p, 16) for p in mac.split(":"))


def _ble_pcap(mac: str, tx_random: bool) -> bytes:
    body = bytes(reversed(_mac_bytes(mac))) + bytes([2, 0x01, 0x06])
    packet = struct.pack("<I", 0x8E89BED6)
    packet += bytes([0x00 | (0x40 if tx_random else 0), len(body)]) + body
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 251)
    out += struct.pack("<IIII", 1700000000, 0, len(packet), len(packet)) + packet
    return out


def test_pcap_txadd_bit_overrides_the_address_bit_guess(tmp_path):
    """A MAC starting 3E has top bits 0b00 and isn't a registered OUI, so the heuristic
    calls it a non-resolvable private address. The advertisement says it's
    public. The advertisement is right and must win. The old behaviour
    stated the guess as fact and hid the real answer."""
    path = tmp_path / "b.pcap"
    path.write_bytes(_ble_pcap("3E:11:22:33:44:55", tx_random=False))
    meta = pcap.read_pcap(path).sightings[0].meta

    assert meta["address_type_actual"] == "public"
    assert meta["address_type"] == "public"
    assert meta["address_type_source"] == "advertising PDU header"
    assert "address_note" not in meta


def test_txadd_random_keeps_the_subtype_from_the_address_bits(tmp_path):
    """TxAdd says random but not which kind; the address bits say resolvable.
    Both pieces of information should survive."""
    path = tmp_path / "b.pcap"
    path.write_bytes(_ble_pcap("4C:11:22:33:44:55", tx_random=True))
    meta = pcap.read_pcap(path).sightings[0].meta

    assert meta["address_type"] == "random-resolvable"
    assert meta["address_type_source"] == "advertising PDU header"
    assert "address_note" in meta


def test_without_ground_truth_the_guess_is_labelled_as_one():
    """A CSV row carries no TxAdd bit, so the guess is all there is, but it
    has to say so."""
    out = ble.describe("3E:11:22:33:44:55", {})
    assert out["address_type"] == "random-non-resolvable"
    assert out["address_type_source"] == "guessed from the address bits"


def test_map_js_shows_the_address_type_source():
    """The popup has to render the distinction, not just carry it."""
    content = (config.WEB_DIR / "map.js").read_text(encoding="utf-8")
    assert "address_type_source" in content


# --- the sample never reaches the real store -----------------------------


class TestSampleNeverPersisted(unittest.TestCase):
    """`_replace_unmapped` saved the whole dataset without checking whether
    that dataset was the bundled sample, so re-placing while the sample was
    loaded wrote 162 fake records into the collected store."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])
        cls.tmp = Path(tempfile.mkdtemp(prefix="warmap-prov-"))
        os.environ["WARMAP_DATA_DIR"] = str(cls.tmp / "data")
        os.environ["WARMAP_CONFIG_DIR"] = str(cls.tmp / "config")
        import importlib
        importlib.reload(config)
        from warmap.ui.mainwindow import MainWindow
        cls.window = MainWindow(tile_port=0)

    @classmethod
    def tearDownClass(cls):
        cls.window.deleteLater()
        os.environ.pop("WARMAP_DATA_DIR", None)
        os.environ.pop("WARMAP_CONFIG_DIR", None)
        import importlib
        importlib.reload(config)

    def test_replacing_unmapped_on_the_sample_writes_nothing(self):
        self.window.load_initial_data()
        self.assertTrue(self.window._using_sample)

        # Force the situation: an unplaced sample record with a track loaded.
        target = self.window._all_aps[0]
        target.lat = None
        target.lon = None
        target.geo_source = GEO_NONE

        self.window._replace_unmapped(quiet=True)

        self.assertTrue(self.window._using_sample)
        self.assertEqual(store.load(config.STORE_PATH), [])


# --- the README's own numbers --------------------------------------------


def test_readme_test_count_matches_reality():
    """A README that states a test count goes stale silently. This is the
    cheapest way to keep it honest."""
    readme = (config.REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^(\d[\d,]*) tests, all offline", readme, re.MULTILINE)
    assert match, "README no longer states a test count in the expected form"
    claimed = int(match.group(1).replace(",", ""))

    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(config.REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    collected = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    assert collected, f"could not read the collected count:\n{result.stdout[-800:]}"
    assert claimed == int(collected.group(1)), (
        f"README says {claimed} tests, pytest collects {collected.group(1)}"
    )
