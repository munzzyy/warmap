// Logic tests for Wardrive-AllInOne.js, runnable with plain node:
//
//     node flipper-app/test_logic.js
//
// The Flipper's mJS engine isn't available off-device, and the module glue
// (serial/storage/textbox/dialog) only runs on the Flipper. But the line
// counting is pure JavaScript, and it now runs over the real, messy serial
// stream a live Marauder wardrive produces (a console banner, status lines, a
// per-row counter on Wi-Fi rows, the device name jammed onto BLE rows). This
// reads the real functions out of the app and exercises them against that.
//
// It reads the real source rather than a copy, so it can't drift out of sync.

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "Wardrive-AllInOne.js"), "utf8");

function grab(re, label) {
    const m = src.match(re);
    if (!m) { throw new Error("could not extract " + label + " from the app source"); }
    return m[0];
}

const extracted = [
    grab(/let NOFIX_MARKERS = [^\n]*/, "NOFIX_MARKERS"),
    grab(/function splitStr[\s\S]*?\n}/, "splitStr"),
    grab(/function containsAny[\s\S]*?\n}/, "containsAny"),
    grab(/function hasNonZeroDigit[\s\S]*?\n}/, "hasNonZeroDigit"),
    grab(/function countLines[\s\S]*?\n}/, "countLines"),
].join("\n");

const harness = `
var pass = 0, fail = 0;
function ok(name, cond) { if (cond === true) { pass++; } else { fail++; console.log("FAIL " + name); } }

// A real, messy wardrive serial burst: banner/status lines, a nameless BLE row
// (MAC printed twice), a named BLE row (name jammed on), Wi-Fi rows with the
// "N | " console counter. All coordinates 0 because there's no fix yet.
var NOFIX = "StartingWardrive. Stop with stopscan\\n" +
    "/wardrive_5.log\\n" +
    "> AP config set error\\n" +
    "5a:75:65:11:22:335a:75:65:11:22:33,,[BLE],,0,-90,0.0000000,0.0000000,0.00,63.75,BLE\\n" +
    "ihoment_H6008_1B2Cd4:ad:fc:0a:1b:2c,,[BLE],,0,-49,0.0000000,0.0000000,0.00,63.75,BLE\\n" +
    "7 | 8E:49:62:77:88:99,,[WPA2_PSK],,36,-68,0.0000000,0.0000000,0.00,63.75,WIFI\\n";

var st = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
var rest = countLines(st, NOFIX + "partial");
ok("counts the 3 device rows, skips banner/status", st.deviceCount === 3);
ok("no-fix coords (0.0000000) do NOT show a fix", st.gpsFix === false);
ok("partial line retained", rest === "partial");

// Now a row with a real GPS fix flips the indicator on.
var st2 = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
countLines(st2, "AA:BB:CC:DD:EE:01,Net,[WPA2_PSK],,6,-70,33.4484123,-112.0740456,200,3,WIFI\\n");
ok("a real coordinate shows a fix", st2.gpsFix === true);
ok("named row's SSID becomes lastSeen", st2.lastSeen === "Net");

// hasNonZeroDigit directly: the bug this fixed was 0.0000000 (7 zeros) reading
// as a fix because it wasn't literally "0.000000" (6 zeros).
ok("0.0000000 has no non-zero digit", hasNonZeroDigit("0.0000000") === false);
ok("0.00 has no non-zero digit", hasNonZeroDigit("0.00") === false);
ok("33.4484123 has a non-zero digit", hasNonZeroDigit("33.4484123") === true);

// A garbage stream with no newline must not grow the line buffer without bound.
var st3 = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
var junk = "";
var k = 0;
while (k < 6000) { junk = junk + "x"; k = k + 1; }
var left = countLines(st3, junk);
ok("no-newline junk buffer is capped", left.length <= 4096);

// splitStr / helpers.
ok("splitStr len", splitStr("a,b,c", ",").length === 3);
ok("splitStr comma rejoin", splitStr("Tile, Mate", ",").length === 2);
ok("containsAny hit", containsAny("x No Fix y", NOFIX_MARKERS) === true);

console.log("\\n" + pass + " passed, " + fail + " failed");
if (fail > 0) { throw new Error(fail + " logic test(s) failed"); }
`;

eval(extracted + "\n" + harness);

// Opt-in: feed a real Marauder serial capture through countLines in irregular
// chunks and confirm every device row is counted. Skips cleanly if absent.
(function checkRealCapture() {
    const candidates = [];
    if (process.env.WARDRIVE_REAL_CAPTURE) { candidates.push(process.env.WARDRIVE_REAL_CAPTURE); }
    candidates.push(path.join(__dirname, "..", "tests", "fixtures", "marauder_raw_serial.txt"));
    const found = candidates.find((p) => { try { return fs.statSync(p).isFile(); } catch (e) { return false; } });
    if (!found) {
        console.log("real-capture check: skipped (no capture found)");
        return;
    }
    eval(extracted);
    const real = fs.readFileSync(found, "utf8");
    // Ground truth: lines that end in ,WIFI or ,BLE are the device rows.
    const rows = real.split("\n").filter((l) => /,(WIFI|BLE)$/.test(l.replace(/\r$/, ""))).length;
    let st = { rawLineCount: 0, deviceCount: 0, gpsFix: false, lastSeen: "" };
    let buf = "", pos = 0, si = 0;
    const sizes = [7, 1, 64, 3, 200, 17, 2, 999, 40];
    while (pos < real.length) {
        const n = sizes[si++ % sizes.length];
        buf = countLines(st, buf + real.slice(pos, pos + n));
        pos += n;
    }
    const ok = st.deviceCount === rows;
    console.log("real-capture check: " + (ok ? "PASS" : "FAIL") +
        " (" + path.basename(found) + ": counted " + st.deviceCount + " / " + rows + " device rows)");
    if (!ok) { throw new Error("real-capture device count mismatch"); }
})();
