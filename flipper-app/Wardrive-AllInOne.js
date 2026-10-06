// Wardrive-AllInOne.js
//
// One-button ESP32 Marauder (BFFB v2, ESP32-C5) wardrive capture for
// Momentum firmware. Talks to Marauder over the Flipper's GPIO USART,
// starts a wardrive, and writes the raw serial stream straight to the SD
// card in a shape the warmap tool auto-detects.
//
// Written against the module APIs actually demonstrated in Momentum's own
// on-card examples (apps/Scripts/Examples/*.js: uart_echo.js, bad_uart.js,
// storage.js, textbox.js, dialog.js, gui.js's stopwatch widget, spi.js,
// i2c.js, blebeacon.js) plus the Next-Flip/Momentum-Firmware
// documentation/js/*.md files. Nothing here calls an API that isn't shown
// working in one of those places. mJS (the JS engine) has no try/catch,
// no closures over re-entered callback state, no String.split/parseFloat/
// Number(), and only === / !==. None of that is used below.
//
// A companion write-up lists every assumption here that still needs a
// real board to confirm, ranked by risk. The Marauder commands are now
// confirmed against v1.14.0 source; the remaining unknowns are mJS runtime
// behavior (serial.readAny chunking, file.write failure return) that only a
// bench run settles.

let serial = require("serial");
let storage = require("storage");
let textbox = require("textbox");
let dialog = require("dialog");
let notify = require("notification");
let math = require("math");

// ---------------------------------------------------------------------
// Config. Confirmed against a real capture off the BFFB v2 (ESP32-C5,
// Marauder v1.14.0):
//   - one "wardrive" runs a dual-band (2.4+5GHz) WiFi scan AND a BLE scan
//     in the same pass, GPS-tagged. One command, both radios.
//   - "stopscan" ends it. Baud 115200. GPS is always-on; a wardrive just
//     won't write rows with real coordinates until the module has a fix.
//   - IMPORTANT: the serial stream does NOT include the WigleWifi header or
//     column header the file needs. The board only sends device rows (plus
//     a banner and status lines). So this script writes those two header
//     lines itself, then appends the raw stream. Marauder also mangles the
//     rows on the wire (a console counter in front of Wi-Fi rows, the device
//     name jammed onto the front of BLE rows); warmap's parser cleans that
//     and, as a bonus, recovers the BLE name the companion app throws away.
//     So the file this writes is intentionally the raw stream under a header,
//     not a pre-cleaned CSV; the cleaning lives in warmap where it's tested.
// ---------------------------------------------------------------------

let UART_PORT = "usart";   // Flipper GPIO pin 13 (TX) / 14 (RX) - same port
let UART_BAUD = 115200;    // esp32_wifi_marauder.fap companion app uses this

let CMD_PROBE = "help\n";
let CMD_WARDRIVE = "wardrive\n";     // dual-band WiFi + BLE + GPS
let CMD_BLE_SNIFF = "sniffbt\n";     // live BLE names/RSSI, no GPS, not mapped
let CMD_STOP = "stopscan\n";

// The header warmap needs and the board never sends. Row 0 must start with
// "WigleWifi" for warmap to recognize the file; row 1 is the column header.
let WIGLE_HEADER = "WigleWifi-1.4,appRelease=v1.14.0,model=ESP32 Marauder,release=v1.14.0,device=ESP32 Marauder,display=SPI TFT,board=ESP32 Marauder,brand=JustCallMeKoko\n";
let COLUMN_HEADER = "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n";

// Loose substring hints for "did the ESP answer the help probe at all" -
// we only need one hit, not an exact match.
let PROBE_HINTS = ["wardrive", "scanap", "stopscan", "sniffbt", "help", "Marauder"];

// Best-effort "no GPS fix" markers, for the on-screen status only.
let NOFIX_MARKERS = ["No Fix", "no fix", "NoFix"];

let DUMP_DIR = "/ext/apps_data/marauder/dumps";

let READ_TIMEOUT_MS = 250;     // serial.readAny() poll interval / display tick
let PROBE_TIMEOUT_MS = 2000;
let DRAIN_MAX_READS = 20;      // bounded stale-buffer drain
let DRAIN_READ_TIMEOUT = 30;
let FLUSH_EVERY_TICKS = 20;    // close+reopen (commit to SD) roughly every 5s
let STOP_DRAIN_TICKS = 8;      // ~2s to catch trailing output after stopscan
let REPAINT_EVERY_TICKS = 4;   // redraw the status screen roughly every 1s

// ---------------------------------------------------------------------
// Small helpers, written against only what the on-card examples show
// working: .indexOf / .slice / .length / .push / .toString. mJS has no
// String.split, parseFloat or Number(), so none of those are used.
// ---------------------------------------------------------------------

function splitStr(str, delim) {
    let parts = [];
    let rest = str;
    while (true) {
        let idx = rest.indexOf(delim);
        if (idx === -1) {
            parts.push(rest);
            break;
        }
        parts.push(rest.slice(0, idx));
        rest = rest.slice(idx + delim.length);
    }
    return parts;
}

function containsAny(str, needles) {
    for (let i = 0; i < needles.length; i++) {
        if (str.indexOf(needles[i]) !== -1) {
            return true;
        }
    }
    return false;
}

// True if the string has any digit 1-9, i.e. not an all-zero coordinate.
// Marauder writes latitude "0.0000000" (seven zeros) when it has no GPS fix, so
// a plain !== "0.000000" check misses it; this catches every zero spelling.
// Uses indexOf, not c >= "1" && c <= "9": mJS's string relational comparison is
// broken ("4" >= "1" returns false), so range checks on characters silently
// never match on the Flipper.
function hasNonZeroDigit(str) {
    for (let i = 0; i < str.length; i++) {
        if ("123456789".indexOf(str.slice(i, i + 1)) !== -1) {
            return true;
        }
    }
    return false;
}

function pad2(n) {
    if (n < 10) {
        return "0" + n.toString();
    }
    return n.toString();
}

function mmss(totalSeconds) {
    let m = math.floor(totalSeconds / 60);
    let s = totalSeconds % 60;
    return pad2(m) + ":" + pad2(s);
}

// FatFs mkdir needs each level to exist before the next, there's no
// recursive "mkdir -p". storage.makeDirectory() on a directory that's
// already there is a harmless no-op (interactive.js on the card calls it
// unconditionally on every run for exactly this reason).
function ensureDir(fullPath) {
    let parts = splitStr(fullPath, "/");
    let cur = "";
    for (let i = 0; i < parts.length; i++) {
        if (parts[i].length === 0) {
            continue;
        }
        cur = cur + "/" + parts[i];
        storage.makeDirectory(cur);
    }
}

function nextFreeFilePath(dir, prefix) {
    let n = 1;
    while (n < 10000) {
        let path = dir + "/" + prefix + n.toString() + ".txt";
        if (storage.fileExists(path) === false) {
            return path;
        }
        n = n + 1;
    }
    return undefined;
}

// Keeps calling readAny() until it times out (buffer's genuinely empty)
// or maxReads is hit. Used to clear out stale bytes before we care about
// what's coming next - bounded so it can never hang.
function drainSerial(maxReads, perReadTimeoutMs) {
    let i = 0;
    while (i < maxReads) {
        let chunk = serial.readAny(perReadTimeoutMs);
        if (chunk === undefined) {
            break;
        }
        i = i + 1;
    }
}

// Fold every complete line in `buf` into the on-screen counters, and return
// whatever partial line is left over (no trailing newline yet) to prepend to
// the next chunk. `st` is a plain object mutated by reference, since mJS has no
// closures over re-entered locals, so the state travels in the object. This
// is the ONLY place lines get counted, so the same rows can't be counted
// twice no matter which path fed them in.
function countLines(st, buf) {
    while (true) {
        let nl = buf.indexOf("\n");
        if (nl === -1) {
            break;
        }
        let line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        st.rawLineCount = st.rawLineCount + 1;
        if (containsAny(line, NOFIX_MARKERS)) {
            st.gpsFix = false;
            continue;
        }
        if (line.indexOf("CurrentLatitude") !== -1) {
            continue; // a column-header line, not a data row
        }
        let fields = splitStr(line, ",");
        if (fields.length >= 8) {
            // WigleWifi-1.4: MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,
            // CurrentLatitude,CurrentLongitude, and more
            st.deviceCount = st.deviceCount + 1;
            let ssid = fields[1];
            st.lastSeen = (ssid !== undefined && ssid.length > 0) ? ssid : fields[0];
            let lat = fields[6];
            if (lat !== undefined && hasNonZeroDigit(lat)) {
                st.gpsFix = true;
            }
        }
    }
    // A real row is ~80 chars. If we're holding this much with no newline in
    // sight, the ESP is spewing garbage; drop it so a hung stream can't grow
    // the buffer without bound. (The file still gets every byte - this is only
    // the counter's line accumulator.)
    if (buf.length > 4096) {
        buf = "";
    }
    return buf;
}

// ---------------------------------------------------------------------
// runCapture - shared by the wardrive path and the BLE-scan path.
//
//   cmd          command line sent to the ESP once we've probed it
//   filePrefix   dump filename prefix under DUMP_DIR
//   writeHeader  true for the wardrive: write the WigleWifi + column header
//                the board never sends, then stream the raw device rows
//                under it (warmap cleans the row mangling on import). false
//                for the BLE scan, whose output isn't Wigle CSV.
//   title        short label shown on screen and in the summary dialog
// ---------------------------------------------------------------------

function runCapture(cmd, filePrefix, writeHeader, title) {
    serial.setup(UART_PORT, UART_BAUD);
    drainSerial(DRAIN_MAX_READS, DRAIN_READ_TIMEOUT);

    serial.write(CMD_PROBE);
    let probeIdx = serial.expect(PROBE_HINTS, PROBE_TIMEOUT_MS);
    let espAlive = (probeIdx !== undefined);
    drainSerial(DRAIN_MAX_READS, DRAIN_READ_TIMEOUT);

    if (!espAlive) {
        let proceed = dialog.custom({
            header: "No ESP response",
            text: "Marauder didn't answer\n'help'. Check TX/RX wiring\nand power.\n\nTry anyway?",
            button_left: "Cancel",
            button_center: "Try anyway",
        });
        if (proceed !== "Try anyway") {
            serial.end();
            return;
        }
    }

    ensureDir(DUMP_DIR);
    let path = nextFreeFilePath(DUMP_DIR, filePrefix);
    if (path === undefined) {
        dialog.message(title, "Could not pick a filename -\ntoo many old dumps in\n" + DUMP_DIR + "?");
        serial.end();
        return;
    }

    // Open the file and, for a wardrive, write the two header lines the board
    // itself never sends. Everything after that is a straight copy of the
    // serial stream.
    let file = storage.openFile(path, "w", "create_always");
    if (file === undefined) {
        dialog.message(title, "SD write failed.\nCheck card / free space.");
        serial.end();
        return;
    }
    let writeError = false;
    if (writeHeader) {
        let head = WIGLE_HEADER + COLUMN_HEADER;
        let wrote = file.write(head);
        if (wrote === undefined || wrote < head.length) {
            writeError = true;
        }
    }

    // Only now tell the ESP to start - if anything above failed we never
    // left it scanning with nothing listening.
    serial.write(cmd);

    textbox.setConfig("end", "text");
    textbox.emptyText();
    textbox.addText(title + "\nstarting...\n");
    textbox.show();

    // lineBuf is only for the on-screen counters and is separate from the
    // write path: countLines never writes and the disk writes never count, so
    // a row can't be double-written or double-counted.
    let lineBuf = "";
    let st = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
    let ticks = 0;
    let sinceFlush = 0;

    while (textbox.isOpen()) {
        let chunk = serial.readAny(READ_TIMEOUT_MS);

        if (chunk !== undefined && chunk.length > 0 && file !== undefined && !writeError) {
            let wrote = file.write(chunk);
            if (wrote === undefined || wrote < chunk.length) {
                // Partial or failed write. Count only the bytes that actually
                // reached disk (a partial write still saved some), so the tally
                // matches the file, then stop.
                if (wrote !== undefined && wrote > 0) {
                    lineBuf = countLines(st, lineBuf + chunk.slice(0, wrote));
                }
                writeError = true;
            } else {
                lineBuf = countLines(st, lineBuf + chunk);
            }
        }

        if (writeError) {
            notify.error();
            textbox.emptyText();
            textbox.addText(title + "\n!! SD write error - stopped !!\n");
            textbox.addText("Lines written: " + st.rawLineCount.toString() + "\n");
            textbox.addText("[Back to exit]\n");
            break;
        }

        ticks = ticks + 1;
        sinceFlush = sinceFlush + 1;
        if (file !== undefined && !writeError && sinceFlush >= FLUSH_EVERY_TICKS) {
            // No flush()/sync() call exists in the storage API - closing
            // and reopening in append mode is the only documented way to
            // force bytes to actually hit the SD before the next chunk.
            file.close();
            file = storage.openFile(path, "w", "open_append");
            if (file === undefined) {
                writeError = true;
            }
            sinceFlush = 0;
        }

        if (ticks % REPAINT_EVERY_TICKS === 0) {
            textbox.emptyText();
            textbox.addText(title + "\n");
            textbox.addText("Time: " + mmss(math.floor(ticks * READ_TIMEOUT_MS / 1000)) + "\n");
            textbox.addText("Lines: " + st.rawLineCount.toString() + "  Devices: " + st.deviceCount.toString() + "\n");
            textbox.addText("GPS: " + (st.gpsFix ? "FIX" : "NO FIX") + "\n");
            textbox.addText("Last: " + (st.lastSeen.length > 0 ? st.lastSeen : "-") + "\n");
            textbox.addText("ESP: " + (espAlive ? "OK" : "no response") + "\n");
            textbox.addText("File: " + filePrefix + "*.txt\n");
            textbox.addText("[Back to stop]\n");
        }
    }

    // Either the user pressed Back, or we broke out above on a write
    // error. Either way: tell the ESP to stop, catch whatever trailing
    // output is already in flight, then close everything down cleanly.
    serial.write(CMD_STOP);
    let drained = 0;
    while (drained < STOP_DRAIN_TICKS) {
        // Don't bail on the first empty read: the ESP's last rows can arrive
        // after a gap, so keep reading for the whole window (each readAny already
        // waits READ_TIMEOUT_MS) and just skip the empties.
        let chunk = serial.readAny(READ_TIMEOUT_MS);
        if (chunk !== undefined && chunk.length > 0 && file !== undefined && !writeError) {
            let wrote = file.write(chunk);
            if (wrote === undefined || wrote < chunk.length) {
                if (wrote !== undefined && wrote > 0) {
                    lineBuf = countLines(st, lineBuf + chunk.slice(0, wrote));
                }
                writeError = true;
            } else {
                lineBuf = countLines(st, lineBuf + chunk);
            }
        }
        drained = drained + 1;
    }

    if (file !== undefined) {
        file.close();
    }
    serial.end();

    if (textbox.isOpen()) {
        textbox.close();
    }

    // A write error has to be shown with a blocking dialog: the in-loop textbox
    // message would otherwise be wiped by the textbox.close() just above before
    // it could be read.
    if (writeError) {
        dialog.message(title, "SD write error.\nStopped after "
            + st.rawLineCount.toString() + " lines.\nCheck the card and free space.");
    } else {
        dialog.message(title, "Stopped.\n" + st.rawLineCount.toString() + " lines, "
            + st.deviceCount.toString() + " devices.\nSaved to:\n" + path);
    }
}

// ---------------------------------------------------------------------
// Entry point - one confirm screen, then straight into the capture.
// ---------------------------------------------------------------------

let choice = dialog.custom({
    header: "Wardrive All-in-One",
    text: "Start: WiFi+BLE+GPS wardrive,\none file for warmap.\n\nBLE scan: live tracker names\n(screen only, not mapped).",
    button_left: "Cancel",
    button_center: "Start",
    button_right: "BLE scan",
});

if (choice === "Start") {
    runCapture(CMD_WARDRIVE, "wardrive_aio_", true, "Wardrive AIO");
} else if (choice === "BLE scan") {
    runCapture(CMD_BLE_SNIFF, "ble_scan_", false, "BLE names (live)");
}
// choice === "" (back) or "Cancel": nothing was opened yet, just exit.
