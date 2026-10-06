#!/usr/bin/env bash
# Run the wardrive app's pure logic through the REAL mJS engine (Cesanta mJS,
# what the Flipper's JavaScript is built on), not just node. node is far more
# permissive than mJS, so this catches engine-level issues node never would:
# mJS forbids `==`, forbids implicit number->string concatenation, and vanilla
# mJS lacks number.toString() (Momentum's fork adds it, which is why the app is
# fine on-device but this script sticks to mJS-legal test code).
#
# Needs gcc and git. Builds mjs into a scratch dir, extracts the app's pure
# functions verbatim, and exercises them. Exits non-zero on any failure.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
app="$here/Wardrive-AllInOne.js"
work="${TMPDIR:-/tmp}/warmap-mjs-test"
mkdir -p "$work"

# Build the engine once (cached).
if [ ! -x "$work/mjs/mjs" ]; then
    if [ ! -f "$work/mjs/mjs.c" ]; then
        git clone --depth 1 https://github.com/cesanta/mjs "$work/mjs" >/dev/null 2>&1 \
            || { echo "SKIP: couldn't fetch the mjs engine source (no network?)"; exit 0; }
    fi
    gcc -DMJS_MAIN "$work/mjs/mjs.c" -o "$work/mjs/mjs" -lm -ldl >/dev/null 2>&1 \
        || { echo "SKIP: couldn't build the mjs engine (need gcc)"; exit 0; }
fi
mjs="$work/mjs/mjs"

# Pull the pure functions + constants straight out of the app.
{
    grep -E '^let NOFIX_MARKERS =' "$app"
    sed -n '/^function splitStr/,/^}/p' "$app"
    sed -n '/^function containsAny/,/^}/p' "$app"
    sed -n '/^function hasNonZeroDigit/,/^}/p' "$app"
    sed -n '/^function countLines/,/^}/p' "$app"
} > "$work/funcs.js"

# The test body is written in strict mJS: only ===, no number->string concat,
# print() takes separate args rather than concatenating them.
cat "$work/funcs.js" > "$work/run.js"
cat >> "$work/run.js" <<'MJS'
let fails = 0;
function ok(name, cond) { if (cond === true) { print("  ok  ", name); } else { fails = fails + 1; print("  FAIL", name); } }

ok("splitStr len", splitStr("a,b,c", ",").length === 3);
ok("splitStr[1]", splitStr("a,b,c", ",")[1] === "b");
ok("splitStr comma-in-name rejoin", splitStr("Tile, Mate", ",").length === 2);
ok("containsAny hit", containsAny("x No Fix y", NOFIX_MARKERS) === true);
ok("containsAny miss", containsAny("clean", NOFIX_MARKERS) === false);

let WROW = "AA:BB:CC:DD:EE:01,Net,[WPA2_PSK],2026-07-26 01:30:00,6,-70,33.4484,-112.0740,200,3,WIFI\n";
let BROW = "5c:69:b7:aa:bb:e2,,[BLE],2026-07-26 01:30:00,0,-62,33.4484,-112.0740,200,3,BLE\n";
let HDR = "WigleWifi-1.4\nMAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n";

// One header + one WiFi + one BLE, all in one chunk.
let st = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
let rest = countLines(st, HDR + WROW + BROW + "partial");
ok("countLines deviceCount 2", st.deviceCount === 2);
ok("countLines rawLineCount 4", st.rawLineCount === 4);
ok("countLines gpsFix true", st.gpsFix === true);
ok("countLines partial retained", rest === "partial");

// A realistic 300-row capture fed through in irregular serial-sized chunks.
let cap = HDR;
let i = 0, expected = 0;
while (i < 300) {
    if ((i % 3) === 0) { cap = cap + BROW; } else { cap = cap + WROW; }
    expected = expected + 1;
    i = i + 1;
}
let st2 = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
let buf = "", pos = 0, si = 0;
let sizes = [7, 1, 64, 3, 200, 17, 2, 999, 40];
while (pos < cap.length) {
    let n = sizes[si % 9]; si = si + 1;
    buf = countLines(st2, buf + cap.slice(pos, pos + n));
    pos = pos + n;
}
ok("300-row workload counts all", st2.deviceCount === expected);

if (fails === 0) { print("MJS-ENGINE: ALL PASS"); } else { print("MJS-ENGINE: FAILURES"); }
MJS

out="$("$mjs" "$work/run.js" 2>&1 | grep -vE '^undefined$')"
echo "$out"
echo "$out" | grep -q "ALL PASS" || { echo "mJS-engine test failed"; exit 1; }
