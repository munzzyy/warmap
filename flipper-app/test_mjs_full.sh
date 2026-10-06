#!/usr/bin/env bash
# Run the WHOLE app (not just its helpers) through the real mJS engine, with
# stub Flipper modules fed a real captured Marauder serial stream. This proves
# the entire runCapture control flow - the module-call sequence, the header
# write, the streaming loop, the flush, the clean stopscan+close - executes in
# mJS without error and produces a file that starts with the WigleWifi header
# and contains the device rows. The only thing it can't cover is the real
# Flipper module implementations, which are Momentum's own (its shipped
# scripts use the same calls).
#
# Needs gcc + git. Skips cleanly without them.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
app="$here/Wardrive-AllInOne.js"
work="${TMPDIR:-/tmp}/warmap-mjs-full"
mkdir -p "$work"

if [ ! -x "$work/mjs/mjs" ]; then
    if [ ! -f "$work/mjs/mjs.c" ]; then
        git clone --depth 1 https://github.com/cesanta/mjs "$work/mjs" >/dev/null 2>&1 \
            || { echo "SKIP: couldn't fetch mjs (no network?)"; exit 0; }
    fi
    gcc -DMJS_MAIN "$work/mjs/mjs.c" -o "$work/mjs/mjs" -lm -ldl >/dev/null 2>&1 \
        || { echo "SKIP: couldn't build mjs (need gcc)"; exit 0; }
fi
mjs="$work/mjs/mjs"

# A real captured wardrive burst to feed the stub serial port. Prefer a live
# capture if one's around, else the checked-in fixture.
cap="$here/../tests/fixtures/marauder_raw_serial.txt"
[ -n "${WARDRIVE_REAL_CAPTURE:-}" ] && [ -f "${WARDRIVE_REAL_CAPTURE}" ] && cap="${WARDRIVE_REAL_CAPTURE}"

# Build the runnable script: mJS stub modules + the app with its require() lines
# and its interactive entry point swapped out for a direct runCapture() call.
python3 - "$app" "$cap" > "$work/run.js" <<'PY'
import sys, json, re
app_path, cap_path = sys.argv[1], sys.argv[2]
src = open(app_path, encoding="utf-8").read()
raw = open(cap_path, encoding="utf-8").read()

# Vanilla Cesanta mJS lacks number.toString() (Momentum's fork adds it, which is
# why the real app is fine on-device). To exercise the whole control flow in the
# vanilla engine, swap X.toString() for a manual _ns(X) helper defined in the
# stubs. This is a test-harness shim, not a change to what ships.
src = re.sub(r"([A-Za-z_][\w.]*)\.toString\(\)", r"_ns(\1)", src)

# split the capture into irregular chunks, like readAny hands them over
chunks, i, sizes = [], 0, [7, 1, 64, 3, 200, 17, 2, 999, 40]
si = 0
while i < len(raw):
    n = sizes[si % len(sizes)]; si += 1
    chunks.append(raw[i:i+n]); i += n

lines = []
# strip the require() lines - the stubs below replace those modules
for ln in src.splitlines():
    s = ln.strip()
    if s.startswith("let ") and "= require(" in s:
        continue
    # stop at the interactive entry point; we call runCapture directly instead
    if s.startswith("let choice = dialog.custom("):
        break
    lines.append(ln)
app_body = "\n".join(lines)

stubs = """
function _ns(n) {
    if (n === 0) { return "0"; }
    let digits = "0123456789";
    let x = n;
    let sign = "";
    if (x < 0) { sign = "-"; x = 0 - x; }
    let rev = "";
    while (x > 0) {
        let d = x %% 10;
        rev = digits.slice(d, d + 1) + rev;
        x = (x - d) / 10;
    }
    return sign + rev;
}
let CHUNKS = %s;
let _reads = 0;
let serial = {
    setup: function (a, b) {},
    write: function (s) {},
    expect: function (a, b) { return 0; },
    readAny: function (t) {
        _reads = _reads + 1;
        if (_reads <= 2) { return undefined; }   // the two pre-command drains
        let idx = _reads - 3;
        if (idx < CHUNKS.length) { return CHUNKS[idx]; }
        return undefined;
    },
    end: function () {}
};
let FILEBUF = "";
let _fileObj = {
    write: function (s) { FILEBUF = FILEBUF + s; return s.length; },
    close: function () {}
};
let storage = {
    makeDirectory: function (p) {},
    fileExists: function (p) { return false; },
    openFile: function (p, m, o) { return _fileObj; }
};
let _open = 0;
let textbox = {
    setConfig: function (a, b) {},
    emptyText: function () {},
    addText: function (s) {},
    show: function () {},
    isOpen: function () { _open = _open + 1; return _open <= 80; },
    close: function () {}
};
let dialog = { custom: function (o) { return "Start"; }, message: function (a, b) {} };
let notify = { error: function () {} };
let math = { floor: function (x) { return x - (x %% 1); } };
""" % json.dumps(chunks)

driver = """
runCapture(CMD_WARDRIVE, "wardrive_aio_", true, "Test");
print("EXECUTED-OK");
print("file starts with WigleWifi:", FILEBUF.indexOf("WigleWifi") === 0);
print("file has a column header:", FILEBUF.indexOf("CurrentLatitude") !== -1);
print("file has device rows:", (FILEBUF.indexOf(",BLE") !== -1 || FILEBUF.indexOf(",WIFI") !== -1));
print("file length:", FILEBUF.length);
"""
print(stubs + "\n" + app_body + "\n" + driver)
PY

out="$("$mjs" "$work/run.js" 2>&1 | grep -vE '^undefined$')"
echo "$out"
echo "$out" | grep -q "EXECUTED-OK" || { echo "the app did not run to completion in mJS"; exit 1; }
echo "$out" | grep -q "file starts with WigleWifi: true" || { echo "header not written"; exit 1; }
echo "$out" | grep -q "file has device rows: true" || { echo "device rows not captured"; exit 1; }
echo "FULL-APP mJS RUN: PASS"
