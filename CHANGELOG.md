# Changelog

## 1.0.0

First public release.

- Reads Marauder and WiGLE wardrive CSVs (1.4 and 1.6), Kismet and generic
  lat/lon exports, the raw Marauder serial stream, pcap and pcapng captures
  (802.11 beacons, probe responses, probe requests, BLE advertisements),
  every Flipper Zero file type, and GPX and NMEA tracks.
- Places Flipper captures by timestamp against a GPS track, or against the
  wardrive itself when there is no track, and never presents an inferred
  position as a measured one.
- Identifies Bluetooth trackers (AirTag and Find My accessories, Tile, Samsung
  SmartTag, DULT) from the advertisement itself, and flags devices that
  travelled with you.
- Overlays ALPR cameras compiled by DeFlock from OpenStreetMap, built per
  viewport so 137,000 cameras stay usable.
- A phone app served over your own network from a QR code, and a single-file
  offline copy for when the PC is off.
- Exports to CSV, GeoJSON, GPX, KML and WiGLE CSV.
- Installs with pip on Linux, macOS and Windows; data lives in each
  platform's app folder and the SD-card import knows about removable drive
  letters and /Volumes.
- Desktop bundles for Linux, Windows and macOS (Apple Silicon and Intel)
  that need no Python, built and started by the release workflow on each
  platform. Windows gets a
  second console binary for the terminal commands.
- `warmap doctor` reports the install, its paths and the camera snapshot,
  and `--map` starts the map engine to prove it works.
