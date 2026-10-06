// A recording stand-in for Leaflet, markercluster and leaflet.heat, enough of
// each for map.js to run start to finish under node.
//
// The point is not to test Leaflet. It's that map.js is 600 lines of code no
// Python test has ever executed: a stray identifier, a typo in a function
// name, a wrong argument count, and all of it passes the asset and constant
// checks in test_web_assets.py and then throws at runtime, where the only
// symptom is a map that silently renders nothing. Running the real file
// against this stub catches that class of bug in milliseconds.
//
// Every call is recorded on `state` so a test can assert what map.js drew
// rather than only that it didn't crash.

"use strict";

function makeStub() {
  // Shapes are identified in the report by the color and size map.js gives
  // them rather than by which group they landed in, so the assertions survive
  // a refactor of the layer wiring.
  const state = {
    markers: [],        // every marker map.js constructed
    polylines: [],      // every polyline (follower trails and GPS tracks)
    groups: [],         // every layer group, so the report can see what is
                        // still on the map rather than only what was drawn
    layerAdds: [],      // {layer, count} per addLayers/addLayer into a group
    heatLayers: [],
    legendHtml: "",
    fitBounds: null,
    setViews: [],
    controls: [],
    openedPopups: [],
    mapLayers: new Set(),
  };

  function group(name) {
    const layers = [];
    const g = {
      _name: name,
      _layers: layers,
      clearLayers() { layers.length = 0; return g; },
      addLayer(l) { layers.push(l); return g; },
      addLayers(arr) {
        arr.forEach((l) => layers.push(l));
        state.layerAdds.push({ layer: name, count: arr.length });
        return g;
      },
      getLayers() { return layers.slice(); },
      eachLayer(fn) { layers.forEach(fn); return g; },
      addTo(map) { map.addLayer(g); return g; },
      zoomToShowLayer(marker, cb) { if (cb) { cb(); } },
    };
    state.groups.push(g);
    return g;
  }

  function shape(kind, latlng, options) {
    const handlers = {};
    const s = {
      _kind: kind,
      _latlng: latlng,
      options: options || {},
      _tooltip: null,
      _popup: null,
      _handlers: handlers,
      on(event, fn) {
        (handlers[event] = handlers[event] || []).push(fn);
        return s;
      },
      fire(event) {
        (handlers[event] || []).forEach((fn) => fn());
        return s;
      },
      bindTooltip(content, opts) { s._tooltip = { content, opts }; return s; },
      bindPopup(html, opts) { s._popup = { html, opts }; return s; },
      openPopup() {
        state.openedPopups.push(s);
        s.fire("popupopen");
        return s;
      },
      closePopup() { s.fire("popupclose"); return s; },
      getLatLng() { return { lat: latlng[0], lng: latlng[1] }; },
      addTo(layer) { layer.addLayer(s); return s; },
    };
    state.markers.push(s);
    return s;
  }

  // A view box the map reports through getBounds(). Null means "the whole
  // world is in view", so every ALPR feature counts as visible, which is what
  // most tests want. A test that exercises viewport culling sets it to
  // [south, west, north, east].
  state.viewBounds = null;
  state.mapHandlers = {};

  function boundsObject() {
    return {
      pad() { return this; },
      contains(latlng) {
        if (!state.viewBounds) { return true; }
        const lat = latlng[0];
        const lon = latlng[1];
        const [s, w, n, e] = state.viewBounds;
        return lat >= s && lat <= n && lon >= w && lon <= e;
      },
    };
  }

  const map = {
    addLayer(l) { state.mapLayers.add(l); return map; },
    removeLayer(l) { state.mapLayers.delete(l); return map; },
    hasLayer(l) { return state.mapLayers.has(l); },
    setView(ll, z) { state.setViews.push({ ll, z }); return map; },
    getZoom() { return 13; },
    getBounds() { return boundsObject(); },
    on(event, fn) {
      (state.mapHandlers[event] = state.mapHandlers[event] || []).push(fn);
      return map;
    },
    fire(event) {
      (state.mapHandlers[event] || []).forEach((fn) => fn());
      return map;
    },
    fitBounds(bounds, opts) { state.fitBounds = { bounds, opts }; return map; },
    removeControl(c) {
      const i = state.controls.indexOf(c);
      if (i !== -1) { state.controls.splice(i, 1); }
      return map;
    },
  };

  const control = (kind, args) => ({
    _kind: kind,
    _args: args,
    addTo() { state.controls.push({ kind, args }); return control; },
  });

  const L = {
    map() { return map; },
    tileLayer(url, opts) {
      return { _url: url, _opts: opts, addTo(m) { m.addLayer(this); return this; } };
    },
    control: Object.assign(
      (opts) => control("control", [opts]),
      {
        zoom: (opts) => control("zoom", [opts]),
        layers: (base, overlays, opts) => control("layers", [base, overlays, opts]),
      }
    ),
    markerClusterGroup(opts) {
      const g = group("cluster");
      g._clusterOpts = opts;
      return g;
    },
    layerGroup() { return group("layerGroup"); },
    marker(latlng, opts) { return shape("marker", latlng, opts); },
    circleMarker(latlng, opts) { return shape("circleMarker", latlng, opts); },
    divIcon(opts) { return { _divIcon: true, ...opts }; },
    polyline(latlngs, opts) {
      const p = {
        _kind: "polyline",
        _latlngs: latlngs,
        options: opts || {},
        getLatLngs() { return latlngs.map((c) => ({ lat: c[0], lng: c[1] })); },
        addTo(layer) { layer.addLayer(p); return p; },
        bindPopup() { return p; },
      };
      state.polylines.push(p);
      return p;
    },
    latLngBounds(points) { return { _points: points }; },
    heatLayer(points, opts) {
      const h = {
        _kind: "heat",
        _points: points,
        _opts: opts,
        addTo(m) { m.addLayer(h); return h; },
      };
      state.heatLayers.push(h);
      return h;
    },
  };

  // map.js only ever touches #legend, and only its innerHTML plus the click
  // handler on the header it just wrote.
  const legendEl = {
    set innerHTML(html) { state.legendHtml = html; this._html = html; },
    get innerHTML() { return this._html || ""; },
    querySelector() { return { onclick: null }; },
  };

  const documentStub = {
    getElementById(id) { return id === "legend" ? legendEl : null; },
  };

  // Defaults to a roomy window; a scenario can shrink it to check what the
  // map does when there isn't space for the legend.
  const windowStub = {
    location: { search: "?port=8123" },
    innerWidth: 1280,
    innerHeight: 900,
  };

  return { state, L, map, window: windowStub, document: documentStub };
}

module.exports = { makeStub };
