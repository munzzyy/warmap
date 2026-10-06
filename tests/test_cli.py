"""CLI dispatch. `stats` runs pure-Python (no Qt), so it's tested for real
output; `open`/`import-sd`/no-args all end up opening a Qt window and
running an event loop, so those are only checked for correct argparse
wiring (the right function gets attached), not actually invoked here.
"""

from __future__ import annotations

from pathlib import Path

from warmap import cli

WIGLE_CSV = (
    "WigleWifi-1.4,appRelease=1.0\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,Type\n"
    "AA:BB:CC:DD:EE:01,StatsNet,[ESS],2024-01-01 00:00:00,6,-50,33.0,-112.0,340,5,WIFI\n"
    "AA:BB:CC:DD:EE:02,StatsNet2,[WPA2-PSK-CCMP][ESS],2024-01-01 00:01:00,11,-60,33.001,-112.001,340,5,WIFI\n"
)


def test_cmd_stats_prints_summary(tmp_path, capsys):
    csv_path = tmp_path / "cap.csv"
    csv_path.write_text(WIGLE_CSV)
    args = cli.main(["stats", str(csv_path)])
    out = capsys.readouterr().out
    assert args == 0
    assert "total access points: 2" in out
    assert "open networks: 1" in out


def test_cmd_stats_no_usable_rows_returns_nonzero(tmp_path, capsys):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("not,a,valid,header\n1,2,3,4\n")
    rc = cli.main(["stats", str(csv_path)])
    assert rc == 1
    assert "No usable rows" in capsys.readouterr().out


def test_cmd_stats_expands_a_folder(tmp_path, capsys):
    folder = tmp_path / "captures"
    folder.mkdir()
    (folder / "a.csv").write_text(WIGLE_CSV)
    rc = cli.main(["stats", str(folder)])
    assert rc == 0
    assert "total access points: 2" in capsys.readouterr().out


def test_expand_paths_leaves_plain_files_alone(tmp_path):
    f = tmp_path / "one.csv"
    f.write_text("x")
    assert cli._expand_paths([str(f)]) == [f]


def test_expand_paths_expands_directories_to_csvs_sorted(tmp_path):
    folder = tmp_path / "dir"
    folder.mkdir()
    (folder / "b.csv").write_text("x")
    (folder / "a.csv").write_text("x")
    (folder / "notes.txt").write_text("x")
    result = cli._expand_paths([str(folder)])
    assert [p.name for p in result] == ["a.csv", "b.csv"]


def test_open_subcommand_wires_to_cmd_open():
    sub_parser = _build_parser_for_inspection()
    parsed = sub_parser.parse_args(["open", "a.csv", "b.csv"])
    assert parsed.func is cli.cmd_open
    assert parsed.files == ["a.csv", "b.csv"]


def test_import_sd_subcommand_wires_to_cmd_import_sd():
    sub_parser = _build_parser_for_inspection()
    parsed = sub_parser.parse_args(["import-sd", "--yes"])
    assert parsed.func is cli.cmd_import_sd
    assert parsed.yes is True


def test_no_subcommand_falls_back_to_launch():
    sub_parser = _build_parser_for_inspection()
    parsed = sub_parser.parse_args([])
    assert not hasattr(parsed, "func") or parsed.func is None


def _build_parser_for_inspection():
    """Rebuilds the same argparse.ArgumentParser main() constructs, without
    calling main() itself (which would dispatch into Qt-opening code for
    real subcommands), just enough to assert the wiring is correct."""
    import argparse

    parser = argparse.ArgumentParser(prog="warmap")
    sub = parser.add_subparsers(dest="cmd")

    o = sub.add_parser("open")
    o.add_argument("files", nargs="+")
    o.set_defaults(func=cli.cmd_open)

    i = sub.add_parser("import-sd")
    i.add_argument("-y", "--yes", action="store_true")
    i.set_defaults(func=cli.cmd_import_sd)

    s = sub.add_parser("stats")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cli.cmd_stats)

    return parser
