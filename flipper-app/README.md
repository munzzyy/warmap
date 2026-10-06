# Wardrive-AllInOne

One-button wardrive script for a Flipper Zero running Momentum firmware,
talking to an attached ESP32 Marauder (BFFB v2 / ESP32-C5) over the GPIO
UART. Press one button, drive around, press back, get a file warmap can
open.

## Install

Copy `Wardrive-AllInOne.js` onto the Flipper's SD card at:

```
apps/Scripts/Wardrive-AllInOne.js
```

Wire the Marauder the same way you would for the official
`esp32_wifi_marauder.fap` companion app: TX/RX on the Flipper's GPIO
pins 13/14, GND, and power. If the companion app already talks to your
board, this script uses the exact same port and baud (115200 8N1), so
the wiring doesn't change.

## Run it

Apps > Scripts > Wardrive-AllInOne. You get one screen:

- **Start** - the all-in-one wardrive. One `wardrive` command runs a
  dual-band (2.4+5GHz) WiFi scan and a BLE scan together, GPS-tagged, into
  one file. This is the button you want.
- **BLE scan** - runs a live BLE sniff so you can see tracker/device names
  and signal on the screen as you move. Marauder can't GPS-tag or pcap a
  BLE sniff on this firmware, so this one's a field reference, not something
  that lands on the map.
- **Cancel** / back - exits, nothing was touched yet.

Once it's running you get a live screen: elapsed time, lines and devices
written, GPS fix state, the last SSID or MAC seen, and whether the ESP
answered at all. Press back to stop. It tells the Marauder to stop
scanning, catches whatever's still coming over the wire, and closes the
file cleanly before it exits. You'll get a summary dialog when it's done.

## What it writes

`apps_data/marauder/dumps/wardrive_aio_1.txt`, `_2.txt`, and so on. It never
overwrites an existing dump, including the ones the official companion app
already wrote there. The file is the two CSV header lines the board doesn't
send, followed by a straight copy of the serial stream. That raw stream is
messier than the companion app's cleaned-up file, and keeping it is the point:
Marauder puts the BLE device name on the wire (jammed onto the row), and
warmap pulls it back out into the map. The companion app drops it, so these
captures carry a bit more than the stock ones do.

The BLE-scan path writes to `ble_scan_1.txt` etc. in the same folder.
That's a plain text log of the live sniff for reading back later: no GPS,
not a mappable capture, and warmap will just skip it when you import the
folder.

## How far this is verified

This was checked against a real BFFB v2 (ESP32-C5, Marauder v1.14.0) on USB,
not just in theory. Driving a live `wardrive` over serial is what showed the
board never sends the CSV header and mangles its rows, and caught a GPS-fix
check that would never have fired on the Flipper. Those are fixed. The capture
the board produced runs through warmap end to end: every device parses, the
MACs come out clean, and the BLE names come through.

The pure logic is also tested two ways off-device:

- `node flipper-app/test_logic.js` runs the real functions against the messy
  serial stream (banner, status lines, the Wi-Fi counter prefix, no-fix
  coordinates) and, when it's present, against a real captured burst.
- `bash flipper-app/test_mjs.sh` runs the same functions through the actual mJS
  engine (Cesanta mJS, what the Flipper's JavaScript is built on) rather than
  node. mJS is far stricter, and running it there is what caught the string
  comparison bug node happily accepted. (Needs gcc + git; skips without them.)
- `bash flipper-app/test_mjs_full.sh` goes further and runs the *whole* app in
  that engine, with stub Flipper modules fed a real captured serial burst. It
  proves the entire flow - probe, header write, streaming loop, flush, stop and
  close - executes without a mJS error and writes a file that opens with the
  WigleWifi header and holds the device rows.

The one thing left is running the app on the Flipper with the board on its GPIO
header, since that's the only place the real Flipper modules exist. Every layer
underneath that is now checked against the real hardware.

Every Flipper module call was checked against Momentum's own on-card examples,
and the Marauder commands against v1.14.0 source.

What that can't cover is the device itself, since mJS only runs on the Flipper. So
the first real run is the bench check: plug the board in, start a wardrive, and
watch that the screen shows a GPS FIX and a climbing device count, then confirm
the saved file opens in warmap. If the count moves and the file imports, it
works.

## Getting it into warmap

Pull the SD card, plug it in, File > Import from SD Card. warmap finds
the dump on its own, it checks file contents for a WigleWifi header, not
just the file extension, same as it does for the companion app's own
files.

Worth knowing: a Marauder wardrive file doubles as a GPS track. Nothing
else on a Flipper logs a position, so if you're also carrying `.sub` or
`.nfc` captures from the same walk, importing this file alongside them is
what lets warmap place those on the map. Every row in the wardrive
carries a timestamp and a fix, and warmap matches your other captures
against it. Run the wardrive whenever you're also planning to grab
anything else.

If you copy the file off the card by hand instead of using Import from SD
Card, use `cp -p` or `rsync -a`. A plain `cp` resets the timestamp to the
copy time, and warmap needs the real one to place anything by track.

## If it doesn't show up in warmap

The live screen says "Waiting for CSV header" if the script hasn't
seen a line starting with `WigleWifi` yet. It gives that about 15 seconds
before it gives up and just starts writing raw output instead. That's
better than capturing nothing, but a file like that won't auto-detect.
If you end up with one: open it in a text editor, find the
`WigleWifi-1.4` line, and delete everything above it. The most likely
reason it stalls is the GPS not having a fix yet. The wardrive holds off
writing rows until the module locks on, so give it a minute of clear sky
first and watch the GPS indicator flip to FIX.
