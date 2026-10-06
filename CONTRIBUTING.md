# Contributing

Bug reports, fixes and new formats are welcome.

## Layout

Python owns the data. `warmap/parse.py`, `flipper.py`, `pcap.py`, `gps.py`,
`ble.py`, `filters.py` and `stats.py` are plain functions with no Qt in them,
and `warmap/ingest.py` is the one door every file comes through. `warmap/ui/`
is the PySide6 window around a Leaflet map, and `warmap/web/map.js` is the
map itself, shared with the phone app in `warmap/webapp/`. `warmap/server.py`
is the phone bridge and `warmap/offline.py` writes the single-file copy.

## Before you open a pull request

```sh
pip install -e ".[dev]"
python3 -m pytest -q
node flipper-app/test_logic.js
```

The suite runs offline and needs no hardware. Node is only there for the
tests that execute `map.js`. CI runs the same thing on Linux, macOS and
Windows.

A change to a parser comes with a test that fails without it, built from a
small input written in the test rather than a checked-in capture. Break your
fix on purpose once and watch the test fail.

## Fixtures

Nothing real goes in the repository. Invent MACs (keep a real vendor prefix
if the test needs the vendor), invent names, and put coordinates on the same
Phoenix grid the sample uses. A capture from an actual drive carries other
people's network names and your own route, and git history is forever.

## A new format

Add the reader next to its relatives, register it in `warmap/ingest.py`, list
it in `warmap formats`, and add a row to the README's table. If the format
can carry a position, decide whether it is a measured fix or an inference and
set the record's provenance accordingly; the map, the table and the exports
all read it.

## Words

Plain and short. Messages that a user sees say what happened and what to do
next, in one sentence each.
