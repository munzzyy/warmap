// Runs the real warmap/web/map.js against the recording stub and prints a JSON
// report of what it drew, so a Python test can assert on it.
//
//     node tests/js/run_map.js scenario.json
//
// A scenario is {"features": <FeatureCollection>, "track": <feature|null>,
// "actions": a list of steps like ["focus","WIFI","AA:BB:CC:DD:EE:FF"] or ["heatmap", true]}.
// Errors are reported per step rather than thrown: a step that blows up must
// show in the report as a failure, never as an empty-but-successful map.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { makeStub } = require("./leaflet_stub");

// Evaluated as a JavaScript expression, not JSON.parse'd, because that is how
// the data really arrives: mapview.py interpolates json.dumps output straight
// into a runJavaScript call. Python writes a bare NaN for a float('nan'),
// which JSON.parse rejects but a browser happily evaluates as the NaN global,
// so parsing this file strictly would test a path the app never takes.
const scenarioPath = process.argv[2];
const scenario = scenarioPath
  ? vm.runInNewContext("(" + fs.readFileSync(scenarioPath, "utf8") + ")")
  : {};

const mapJs = path.join(__dirname, "..", "..", "warmap", "web", "map.js");
const source = fs.readFileSync(mapJs, "utf8");

const stub = makeStub();
if (scenario.viewport) {
  stub.window.innerWidth = scenario.viewport[0];
  stub.window.innerHeight = scenario.viewport[1];
}
const consoleErrors = [];
const errors = [];

const sandbox = {
  window: stub.window,
  document: stub.document,
  L: stub.L,
  URLSearchParams,
  // The overlay debounces its viewport re-render with setTimeout. Run it
  // synchronously so a test that fires "moveend" sees the result immediately.
  setTimeout: (fn) => { if (typeof fn === "function") { fn(); } return 0; },
  clearTimeout: () => {},
  console: {
    log() {},
    warn(...a) { consoleErrors.push("warn: " + a.join(" ")); },
    error(...a) { consoleErrors.push("error: " + a.join(" ")); },
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function step(name, fn) {
  try {
    return fn();
  } catch (err) {
    errors.push({ step: name, message: String(err && err.message || err),
                  stack: String(err && err.stack || "") });
    return null;
  }
}

step("load", () => vm.runInContext(source, sandbox, { filename: "map.js" }));

const api = stub.window;

if (scenario.viewBounds) {
  stub.state.viewBounds = scenario.viewBounds;
}
if (scenario.features) {
  step("warmapLoadData", () => api.warmapLoadData(scenario.features));
}
if (scenario.track !== undefined) {
  step("warmapSetTrack", () => api.warmapSetTrack(scenario.track));
}
if (scenario.alpr !== undefined) {
  step("warmapSetAlpr", () => api.warmapSetAlpr(scenario.alpr));
}
for (const action of scenario.actions || []) {
  const [name, ...args] = action;
  step(name, () => {
    if (name === "focus") { return api.warmapFocus(args[0], args[1]); }
    if (name === "heatmap") { return api.warmapSetHeatmapVisible(args[0]); }
    if (name === "typeVisible") { return api.warmapSetTypeVisible(args[0], args[1]); }
    if (name === "fit") { return api.warmapFitToData(); }
    if (name === "reload") { return api.warmapLoadData(args[0]); }
    if (name === "setAlpr") { return api.warmapSetAlpr(args[0]); }
    if (name === "setAlprVisible") { return api.warmapSetAlprVisible(args[0]); }
    if (name === "setViewBounds") { stub.state.viewBounds = args[0]; return true; }
    if (name === "moveend") { return stub.map.fire("moveend"); }
    if (name === "closePopups") {
      stub.state.openedPopups.slice().forEach((m) => m.closePopup());
      return true;
    }
    throw new Error("unknown action " + name);
  });
}

const state = stub.state;
const markersByKind = {};
state.markers.forEach((m) => {
  markersByKind[m._kind] = (markersByKind[m._kind] || 0) + 1;
});

process.stdout.write(JSON.stringify({
  ok: errors.length === 0,
  errors,
  consoleErrors,
  apiPresent: [
    "warmapLoadData", "warmapSetTrack", "warmapSetHeatmapVisible",
    "warmapSetTypeVisible", "warmapFocus", "warmapFitToData",
  ].filter((n) => typeof api[n] === "function"),
  markerCount: state.markers.length,
  markersByKind,
  // The follower warning ring and its trail are identified by the color and
  // shape map.js gives them, not by which group they landed in, so the
  // assertion stays true if the layer wiring is refactored.
  followerRingCount: state.markers.filter(
    (m) => m.options && m.options.color === "#e02424" && m.options.radius === 13).length,
  followerTrailCount: state.polylines.filter(
    (p) => p.options && p.options.color === "#e02424").length,
  trackPolylineCount: state.polylines.filter(
    (p) => p.options && p.options.color === "#1f6fd0").length,
  trackEndpointCount: state.markers.filter(
    (m) => m.options && m.options.color === "#1f6fd0" && m.options.radius === 6).length,
  polylineCount: state.polylines.length,
  // Still on the map right now, as opposed to ever drawn. That's the difference
  // between a trail that was cleaned up and one that was left behind.
  liveTrailShapeCount: state.groups.reduce((n, g) => n + g._layers.filter(
    (l) => l.options && l.options.color === "#e02424"
      && (l._kind === "polyline" || l.options.radius === 3)).length, 0),
  layerAdds: state.layerAdds,
  heatPointCount: state.heatLayers.length
    ? state.heatLayers[state.heatLayers.length - 1]._points.length
    : 0,
  legendHtml: state.legendHtml,
  popups: state.markers.map((m) => (m._popup ? m._popup.html : null)),
  tooltips: state.markers.map((m) => (m._tooltip ? m._tooltip.content : null)),
  markerOptions: state.markers.map((m) => m.options || {}),
  fitBounds: state.fitBounds ? state.fitBounds.bounds._points.length : null,
  openedPopupCount: state.openedPopups.length,
  controls: state.controls.map((c) => c.kind),
  layerControlOverlays: (() => {
    const last = state.controls.filter((c) => c.kind === "layers").pop();
    return last ? Object.keys(last.args[1] || {}) : [];
  })(),
  // ALPR camera markers, identified by the class map.js gives their icon.
  // Counted from what's LIVE on the map right now (the group's current
  // layers), not cumulatively. clearLayers empties a group without forgetting
  // the shapes it once held, so the viewport-cull and visibility-toggle tests
  // need the live set, the same way the follower-trail assertions do.
  ...(() => {
    const isAlpr = (m) => m && m.options && m.options.icon && m.options.icon.className
      && m.options.icon.className.indexOf("warmap-alpr") !== -1;
    const live = [];
    state.groups.forEach((g) => g._layers.forEach((l) => { if (isAlpr(l)) { live.push(l); } }));
    return {
      alprMarkerCount: live.length,
      alprPopups: live.map((m) => (m._popup ? m._popup.html : null)),
      alprTooltips: live.map((m) => (m._tooltip ? m._tooltip.content : null)),
      alprWedgeCounts: live.map(
        (m) => ((m.options.icon.html || "").match(/alpr-wedge/g) || []).length),
    };
  })(),
}, null, 1));
