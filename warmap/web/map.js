(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  var port = params.get("port");

  // The host tells the page which theme it's living in, so the basemap can be
  // dark under dark chrome instead of a white rectangle in a dark window. The
  // mobile app sets this attribute itself; the desktop passes ?theme= because
  // the tiles start loading before any later call could re-tint them.
  var themeParam = params.get("theme");
  if (themeParam && !document.documentElement.dataset.theme) {
    document.documentElement.dataset.theme = themeParam;
  }

  // Marker/cluster color per encryption bucket. MUST match
  // warmap/models.py's ENC_COLORS. tests/test_web_assets.py cross-checks
  // this object against that dict so the two can't silently drift apart.
  var ENC_COLORS = {
    "Open": "#d03b3b",
    "WEP": "#e07b1a",
    "WPA": "#3f9142",
    "WPA2": "#2f8f3f",
    "WPA3": "#1f8f5a",
    "WPA2/3-mixed": "#2f8f6f",
    "Unknown": "#8a8a8a"
  };

  // Per record type, must match warmap/models.py's TYPE_COLORS.
  var TYPE_COLORS = {
    "WIFI": "#2f8f3f",
    "CLIENT": "#c9a227",
    "BLE": "#3b7fd0",
    "BT": "#5b5bd0",
    "CELL": "#9b5bd0",
    "SUBGHZ": "#d08b1a",
    "NFC": "#d03b8b",
    "RFID": "#c0506b",
    "IBUTTON": "#8b6b3b",
    "IR": "#d0503b"
  };

  // ALPR / Flock camera overlay color, MUST match warmap/models.py's
  // ALPR_COLOR. tests/test_web_assets.py cross-checks it. These cameras are a
  // reference overlay from DeFlock/OpenStreetMap, not a capture, and live in
  // their own layer outside everything above (see warmapSetAlpr below).
  var ALPR_COLOR = "#7b2fb5";

  // Sub-GHz code type, must match warmap/radio.py's RISK_COLORS.
  var RISK_COLORS = {
    "static": "#d03b3b",
    "rolling": "#3f9142",
    "raw": "#8a8a8a",
    "unknown": "#8a8a8a"
  };

  // Must match warmap/models.py's TYPE_LABELS and TYPE_GLYPHS.
  var TYPE_LABELS = {
    "WIFI": "Wi-Fi",
    "CLIENT": "Wi-Fi client",
    "BLE": "Bluetooth LE",
    "BT": "Bluetooth Classic",
    "CELL": "Cell",
    "SUBGHZ": "Sub-GHz",
    "NFC": "NFC",
    "RFID": "RFID 125 kHz",
    "IBUTTON": "iButton",
    "IR": "Infrared"
  };

  var TYPE_GLYPHS = {
    "SUBGHZ": "≈",
    "NFC": "N",
    "RFID": "R",
    "IBUTTON": "i",
    "IR": "IR"
  };

  // The radio types are high-count and get clustered circle markers. The
  // Flipper types are low-count and get a glyph marker, which is readable at
  // a glance and affordable when there are tens rather than thousands.
  var CLUSTERED_TYPES = ["WIFI", "CLIENT", "BLE", "BT", "CELL"];
  var GLYPH_TYPES = ["SUBGHZ", "NFC", "RFID", "IBUTTON", "IR"];
  var ALL_TYPES = CLUSTERED_TYPES.concat(GLYPH_TYPES);

  var map = L.map("map", { preferCanvas: true, zoomControl: false }).setView([20, 0], 2);

  // Zoom sits top-right with the layer control rather than top-left, because
  // the legend grows upward from the bottom-left corner and covers it.
  L.control.zoom({ position: "topright" }).addTo(map);

  // Where the basemap comes from, in order of preference:
  //   1. `window.warmapTileUrl`, the phone sets this to a RELATIVE path so
  //      tiles come from the PC through the same origin as the app. That
  //      matters twice over: the PC's cache serves them, and the service
  //      worker can only cache same-origin responses, so fetching OSM
  //      directly would quietly defeat the offline basemap.
  //   2. `?port=`, the desktop's loopback tile proxy.
  //   3. OpenStreetMap directly, which is what a single-file offline export
  //      falls back to when it has a connection and no server behind it.
  var tileUrl = window.warmapTileUrl
    || (port
      ? "http://127.0.0.1:" + port + "/tiles/{z}/{x}/{y}.png"
      : "https://tile.openstreetmap.org/{z}/{x}/{y}.png");

  L.tileLayer(tileUrl, {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  // One layer per record type so the overlay control can toggle them
  // independently.
  var layers = {};
  ALL_TYPES.forEach(function (t) {
    if (CLUSTERED_TYPES.indexOf(t) !== -1) {
      layers[t] = L.markerClusterGroup({
        iconCreateFunction: makeClusterIcon,
        chunkedLoading: true
      });
    } else {
      layers[t] = L.layerGroup();
    }
    map.addLayer(layers[t]);
  });

  var trackLayer = L.layerGroup().addTo(map);
  // Warning rings for devices that moved with you (see isFollower).
  var followerLayer = L.layerGroup().addTo(map);
  // Their trails live in their own layer and only one is drawn at a time.
  // Every follower's trail at once is unreadable: a drive down one road puts
  // 47 overlapping dashed lines along that road and buries the map under
  // them. The ring still marks every follower; the trail belongs to the one
  // whose popup is open, which is the only one being read.
  var trailLayer = L.layerGroup().addTo(map);
  var FOLLOW_SPAN_M = 150;   // seen across at least this far = travelled with you
  var heatLayer = null;
  var heatVisible = false;
  var currentFeatures = [];
  var markerIndex = {};
  var layerControl = null;
  var lastCounts = {};

  // The ALPR (Flock and other license-plate reader) camera overlay. Its own
  // clustered layer, kept apart from every capture layer above: these come
  // from DeFlock/OpenStreetMap, not from anything warmap heard on the ground,
  // so they never enter currentFeatures, fit-to-data or the heatmap. A capture
  // is something you found; a camera is context drawn around it.
  var alprLayer = L.markerClusterGroup({
    iconCreateFunction: makeAlprClusterIcon,
    chunkedLoading: true
  }).addTo(map);
  var alprIndex = {};
  var alprCount = 0;         // total cameras loaded
  var alprVisible = true;
  // The whole set (137k across the US) is held here as raw features and only
  // the ones in view are ever turned into markers. Handing markercluster all
  // 137k at once freezes the window for ~10s; a viewport's worth never does,
  // and the full set is still there the moment you pan to it.
  var alprFeatures = [];
  var ALPR_RENDER_CAP = 8000;  // most camera markers to build for one view
  var alprShown = 0;           // markers actually drawn right now
  var alprSampled = false;     // true when the view held more than the cap
  var alprMoveTimer = null;

  // Re-render the camera overlay for the new view after a pan/zoom settles.
  // Debounced so dragging the map doesn't rebuild markers on every frame.
  map.on("moveend", function () {
    if (!alprFeatures.length) { return; }
    if (alprMoveTimer) { clearTimeout(alprMoveTimer); }
    alprMoveTimer = setTimeout(function () {
      renderAlprViewport();
      rebuildLegend(lastTally);
    }, 150);
  });

  // Keep the intent flag in sync when the camera overlay is toggled from the
  // map's own layer control (as opposed to the filter panel). Without this,
  // unchecking it there is undone the next time renderAlprViewport runs on a
  // pan, because that function re-adds the layer whenever alprVisible is true.
  map.on("overlayremove", function (e) {
    if (e.layer === alprLayer) { alprVisible = false; alprShown = 0; rebuildLegend(lastTally); }
  });
  map.on("overlayadd", function (e) {
    if (e.layer === alprLayer) { alprVisible = true; renderAlprViewport(); rebuildLegend(lastTally); }
  });

  function colorFor(p) {
    if (!p) { return TYPE_COLORS.WIFI; }
    if (p.type === "WIFI") {
      return ENC_COLORS[p.enc_bucket] || ENC_COLORS.Unknown;
    }
    if (p.type === "SUBGHZ") {
      var code = (p.meta && p.meta.code_type) || "unknown";
      return RISK_COLORS[code] || RISK_COLORS.unknown;
    }
    return TYPE_COLORS[p.type] || TYPE_COLORS.WIFI;
  }

  function dominantColor(markers) {
    var counts = {};
    var best = null;
    var bestCount = -1;
    markers.forEach(function (m) {
      var c = (m.options && m.options.warmapColor) || "#8a8a8a";
      counts[c] = (counts[c] || 0) + 1;
      if (counts[c] > bestCount) {
        bestCount = counts[c];
        best = c;
      }
    });
    return best || "#8a8a8a";
  }

  function makeClusterIcon(cluster) {
    var markers = cluster.getAllChildMarkers();
    var color = dominantColor(markers);
    var count = cluster.getChildCount();
    var size = count < 10 ? 30 : count < 100 ? 36 : 44;
    return L.divIcon({
      html: '<div style="background:' + color + ";width:" + size + "px;height:" + size +
        "px;line-height:" + size + 'px;border-radius:50%;color:#fff;text-align:center;' +
        'font-weight:600;border:2px solid rgba(0,0,0,0.35);">' + count + "</div>",
      className: "warmap-cluster",
      iconSize: [size, size]
    });
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // --- host hooks ---------------------------------------------------------
  // This file is the renderer for both the desktop window and the phone app.
  // The desktop wants Leaflet's own popups; the phone wants the tap to open
  // its bottom sheet instead, because a popup on a 6-inch screen covers the
  // map it is describing. Rather than fork the renderer, a host can set
  // `window.warmapNoPopups` to skip binding popups and `window.warmapOnSelect`
  // to receive the tap. Both are read at marker-build time, so a host script
  // that loads after this one still gets to set them.

  function popupsSuppressed() {
    return !!window.warmapNoPopups;
  }

  // A tap on anything. `kind` is "record" or "alpr". Never allowed to throw
  // into Leaflet's event dispatch, a broken host must not take the map down.
  function notifySelect(kind, props, latlng) {
    if (typeof window.warmapOnSelect !== "function") { return; }
    try {
      window.warmapOnSelect(kind, props, latlng);
    } catch (err) {
      console.warn("warmap: select hook failed: " + (err && err.message));
    }
  }

  // --- ALPR / Flock camera overlay ----------------------------------------

  function makeAlprClusterIcon(cluster) {
    var count = cluster.getChildCount();
    var size = count < 10 ? 30 : count < 100 ? 36 : 44;
    return L.divIcon({
      html: '<div style="background:' + ALPR_COLOR + ";width:" + size + "px;height:" +
        size + "px;line-height:" + size + 'px;border-radius:50%;color:#fff;' +
        'text-align:center;font-weight:600;border:2px solid rgba(255,255,255,0.85);' +
        'box-shadow:0 0 0 2px rgba(0,0,0,0.3);">' + count + "</div>",
      className: "warmap-cluster warmap-alpr-cluster",
      iconSize: [size, size]
    });
  }

  // A camera icon: a dot at the pole, plus one wedge per known facing so the
  // way a camera is aimed is readable without opening the popup. OSM's
  // `direction` is degrees clockwise from north, which is exactly what CSS
  // rotate() wants (0 = up = north), so the value goes straight in. No
  // direction -> just the dot, never a wedge guessing at north.
  function alprIconHtml(dirs) {
    var wedges = "";
    (dirs || []).forEach(function (deg) {
      var d = Number(deg);
      if (!isFinite(d)) { return; }
      wedges += '<span class="alpr-wedge" style="transform:rotate(' + d + 'deg)"></span>';
    });
    return '<div class="alpr-icon">' + wedges + '<span class="alpr-dot"></span></div>';
  }

  function alprLabel(p) {
    return p.op || p.mfg || p.brand || "ALPR camera";
  }

  function alprFacing(dirs) {
    if (!dirs || !dirs.length) { return null; }
    return dirs.map(function (d) { return Math.round(Number(d)) + "°"; }).join(", ");
  }

  function alprPopupHtml(p) {
    var title = esc(p.op || p.mfg || p.brand || "ALPR camera");
    var html = "<div class='warmap-popup'>" +
      "<div class='wp-title'>" + title + "</div>" +
      "<div class='wp-type' style='color:" + ALPR_COLOR + "'>" +
      (p.flock ? "Flock Safety ALPR camera" : "ALPR camera") + "</div><table>";
    html += row("Manufacturer", p.mfg);
    html += row("Operator", p.op);
    html += row("Facing", alprFacing(p.dir));
    html += row("Mount", p.mount);
    html += row("Zone", p.zone);
    html += row("Ref", p.ref);
    html += "</table>";
    html += "<div class='wp-note'>Reference data from DeFlock / OpenStreetMap " +
      "(© OpenStreetMap contributors), not a warmap capture." +
      (p.id ? " " + esc(String(p.id)) : "") + "</div>";
    html += "</div>";
    return html;
  }

  function makeAlprMarker(feature) {
    var p = feature.properties || {};
    var c = feature.geometry && feature.geometry.coordinates;
    var marker = L.marker([c[1], c[0]], {
      alprId: p.id,
      warmapColor: ALPR_COLOR,
      icon: L.divIcon({
        className: "warmap-alpr" + (p.flock ? " warmap-alpr-flock" : ""),
        html: alprIconHtml(p.dir),
        iconSize: [26, 26],
        iconAnchor: [13, 13]
      })
    });
    // esc(): Leaflet sets a string tooltip via innerHTML, so an operator or
    // manufacturer name off OpenStreetMap is untrusted text and has to be
    // escaped here just like it is in the popup.
    marker.bindTooltip(esc(alprLabel(p)), { direction: "top", opacity: 0.9 });
    if (!popupsSuppressed()) {
      marker.bindPopup(alprPopupHtml(p), {
        maxHeight: 320, maxWidth: 360, autoPanPadding: [24, 24]
      });
    }
    marker.on("click", function () {
      notifySelect("alpr", p, [c[1], c[0]]);
    });
    return marker;
  }

  // Build camera markers for the current view only. Over a whole-country view
  // that would be all 137k, which markercluster can't take at once without
  // freezing the window, so a view holding more than the cap is evenly
  // sampled down, and the legend says so. Zoom in and the count drops below
  // the cap and every camera in view is drawn, wedges and all.
  function renderAlprViewport() {
    alprLayer.clearLayers();
    alprIndex = {};
    alprShown = 0;
    alprSampled = false;
    if (!alprVisible || !alprFeatures.length) {
      return;
    }
    var bounds = map.getBounds().pad(0.25);
    var visible = [];
    for (var i = 0; i < alprFeatures.length; i++) {
      var g = alprFeatures[i].geometry;
      var c = g && g.coordinates;
      if (!c || c.length < 2) { continue; }
      if (bounds.contains([c[1], c[0]])) { visible.push(alprFeatures[i]); }
    }
    var selected = visible;
    if (visible.length > ALPR_RENDER_CAP) {
      alprSampled = true;
      var stride = Math.ceil(visible.length / ALPR_RENDER_CAP);
      selected = [];
      for (var j = 0; j < visible.length; j += stride) {
        selected.push(visible[j]);
      }
    }
    var markers = [];
    selected.forEach(function (feature) {
      var coords = validCoords(feature);
      if (!coords) { return; }
      var marker = makeAlprMarker(feature);
      markers.push(marker);
      var id = feature.properties && feature.properties.id;
      if (id) { alprIndex[id] = marker; }
    });
    if (markers.length) { alprLayer.addLayers(markers); }
    alprShown = markers.length;
    if (!map.hasLayer(alprLayer)) { map.addLayer(alprLayer); }
  }

  // Show the fix as it was recorded. Rounding to a fixed 6 places both loses
  // the last digit the board actually reported and pads a coarse coordinate
  // with zeros it never had; 7 places is ~1 cm, finer than any consumer GPS,
  // so this only ever trims float noise.
  function trimCoord(v) {
    return String(Number(Number(v).toFixed(7)));
  }

  function row(label, value) {
    if (value === null || value === undefined || value === "") { return ""; }
    return "<tr><td>" + esc(label) + "</td><td>" + esc(value) + "</td></tr>";
  }

  // Meta keys that get a friendly label in the popup. Anything else in meta
  // is still shown, just under its raw key.
  var META_LABELS = {
    code_type_label: "Code type",
    band: "Band",
    band_note: "Band use",
    frequency_note: "Typically",
    preset_label: "Modulation",
    protocol: "Protocol",
    key: "Key",
    bit: "Bits",
    serial: "Serial",
    btn: "Button",
    cnt: "Counter",
    technology: "Technology",
    uid_bytes: "UID length (bytes)",
    atqa: "ATQA",
    sak: "SAK",
    tracker: "Tracker",
    tracker_basis: "Identified by",
    company: "Company",
    address_type: "Address type",
    address_type_source: "Address type from",
    address_note: "Note",
    appearance: "Appearance",
    tx_power: "TX power",
    pdu_type: "PDU type",
    probing_for: "Networks it asked for",
    frame_type: "Frame",
    mac_randomized: "Randomized MAC",
    services: "Services",
    mfg_data: "Manufacturer data",
    signal_type: "Signal type",
    address: "IR address",
    command: "IR command",
    geo_match_seconds: "Track match (seconds off)",
    location_source: "Location from",
    timestamp_source: "Timestamp from",
    cell_technology: "Cell radio",
    encryption_source: "Encryption read from"
  };

  // Raw keys that a friendlier row already covers, plus bookkeeping the
  // reader doesn't want. `frequency` and `preset` are the file's own values
  // in Hz and in FuriHal enum form; the record's frequency field and
  // preset_label say the same thing in a readable way.
  var META_HIDDEN = {
    extra_fields: true, from_pcap: true, frequency_mhz: true, code_type: true,
    vendor: true, name: true, frequency: true, preset: true, raw_data: true,
    ssid: true, address_type_actual: true,
    // Rendered explicitly (the "Seen at" row and the follower trail), not as
    // raw meta rows.
    location_count: true, span_m: true, locations: true
  };

  // `address_type_actual` is hidden because `address_type` already reflects it
  // when it's available, and `address_type_source` says which it was.

  function metaRows(meta) {
    if (!meta) { return ""; }
    var html = "";
    Object.keys(META_LABELS).forEach(function (key) {
      if (meta[key] === undefined || META_HIDDEN[key]) { return; }
      var value = meta[key];
      if (Array.isArray(value)) { value = value.join(", "); }
      html += row(META_LABELS[key], value);
    });
    Object.keys(meta).forEach(function (key) {
      if (META_LABELS[key] || META_HIDDEN[key]) { return; }
      var value = meta[key];
      if (value === null || value === undefined || value === "") { return; }
      if (typeof value === "object") { value = JSON.stringify(value); }
      html += row(key.replace(/_/g, " "), value);
    });
    return html;
  }

  var GEO_NOTES = {
    "track": "Position inferred by matching this capture's timestamp to the GPS track. Not a recorded fix.",
    "direct": null
  };

  function popupHtml(p, coords) {
    var title = p.ssid ? esc(p.ssid) : (p.type === "WIFI" ? "<em>(hidden)</em>" : esc(p.bssid));
    var html = "<div class='warmap-popup'>" +
      "<div class='wp-title'>" + title + "</div>" +
      "<div class='wp-type' style='color:" + colorFor(p) + "'>" +
      esc(TYPE_LABELS[p.type] || p.type) + "</div>" +
      "<table>";

    var addressy = ["WIFI", "CLIENT", "BLE", "BT"].indexOf(p.type) !== -1;
    html += row(addressy ? "Address" : "Identity", p.bssid);
    if (p.vendor) { html += row("Vendor", p.vendor); }
    if (p.type === "WIFI") { html += row("Encryption", p.enc_bucket); }
    if (p.auth_mode) { html += row("Auth mode", p.auth_mode); }
    html += row("Channel", p.channel);
    if (p.frequency !== null && p.frequency !== undefined) {
      html += row("Frequency", p.frequency + " MHz");
    }
    if (p.rssi) { html += row("Signal", p.rssi + " dBm"); }
    html += row("First seen", p.first_seen);
    html += row("Times seen", p.times_seen);
    if (p.meta && p.meta.location_count > 1) {
      html += row("Seen at", p.meta.location_count + " locations, spanning " +
        Math.round(p.meta.span_m) + " m");
    }
    html += metaRows(p.meta);
    html += row("Lat, Lon", trimCoord(coords[1]) + ", " + trimCoord(coords[0]));
    if (p.source) {
      html += row("From", String(p.source).split("/").pop());
    }
    html += "</table>";

    var note = GEO_NOTES[p.geo_source];
    if (note) {
      html += "<div class='wp-note'>" + esc(note) + "</div>";
    }
    if (isFollower(p)) {
      html += "<div class='wp-warn'>⚠ Possible follower: this device was seen at " +
        p.meta.location_count + " spots across " + Math.round(p.meta.span_m) +
        " m of your route.</div>";
    }
    html += "</div>";
    return html;
  }

  // The most informative short label for a device, for the hover tooltip and
  // for scanning the map without opening every popup. A BLE device off a
  // wardrive has no name, so fall back to what we can still say about it:
  // a detected tracker, the OUI vendor, or "randomized address" when the MAC
  // is a privacy address that reveals nothing.
  function bestLabel(p) {
    if (p.ssid) { return p.ssid; }
    var m = p.meta || {};
    if (m.tracker) { return m.tracker; }
    if (p.vendor) { return p.vendor; }
    if (m.address_type && String(m.address_type).indexOf("random") === 0) {
      return "randomized address";
    }
    return p.bssid || "(unnamed)";
  }

  // Dot radius carries signal strength: a strong signal (close/loud) reads as
  // a bigger dot, a weak one as a small dot. RSSI is negative dBm, roughly
  // -30 (very strong) to -100 (barely heard). No RSSI -> a neutral middle dot.
  function markerRadius(p) {
    var r = p.rssi;
    if (r === null || r === undefined || r === 0) { return 6; }
    var t = (r + 95) / 55;            // 0 at -95 dBm, 1 at -40 dBm
    if (t < 0) { t = 0; }
    if (t > 1) { t = 1; }
    return 4 + Math.round(t * 7);     // 4..11 px
  }

  // A "follower" is a device seen at several distinct spots that span real
  // distance, meaning it travelled with you. Bluetooth is the case that matters
  // for stalking (an AirTag/Tile in your bag); a Wi-Fi AP that "moves" is
  // usually a mobile hotspot, so only flag radios that can be carried.
  function isFollower(p) {
    if (!p || !p.meta) { return false; }
    var span = p.meta.span_m;
    if (!(span >= FOLLOW_SPAN_M)) { return false; }
    return p.type === "BLE" || p.type === "BT" || !!p.meta.tracker;
  }

  // A coordinate that isn't a real pair of finite numbers can't be drawn, and
  // must not be smuggled onto the map as [0, 0]: that's a point in the Gulf
  // of Guinea, not a missing position. A single NaN is worse than useless:
  // Leaflet propagates it into the layer bounds, and fitBounds then fails for
  // every other record in the capture.
  function validCoords(feature) {
    var c = feature && feature.geometry && feature.geometry.coordinates;
    if (!c || c.length < 2) { return null; }
    var lon = Number(c[0]);
    var lat = Number(c[1]);
    if (!isFinite(lat) || !isFinite(lon)) { return null; }
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) { return null; }
    return [lon, lat];
  }

  function makeMarker(feature, coords) {
    var p = feature.properties || {};
    var latlng = [coords[1], coords[0]];
    var color = colorFor(p);
    var inferred = p.geo_source === "track";
    var marker;

    if (GLYPH_TYPES.indexOf(p.type) !== -1) {
      var glyph = TYPE_GLYPHS[p.type] || "?";
      marker = L.marker(latlng, {
        warmapColor: color,
        icon: L.divIcon({
          className: "warmap-glyph" + (inferred ? " warmap-inferred" : ""),
          html: '<div class="wg-inner" style="background:' + color + '">' + esc(glyph) + "</div>",
          iconSize: [24, 24],
          iconAnchor: [12, 12]
        })
      });
    } else {
      marker = L.circleMarker(latlng, {
        radius: markerRadius(p),
        color: inferred ? color : "#1a1a1a",
        weight: inferred ? 2 : 1,
        dashArray: inferred ? "3,3" : null,
        fillColor: color,
        fillOpacity: inferred ? 0.45 : 0.9,
        warmapColor: color
      });
    }

    // A Sub-GHz or BLE record can carry a lot of decoded detail. Capping the
    // height keeps a tall popup inside the window with its own scrollbar
    // instead of running off the top of the map where the title can't be read.
    // esc(): a string tooltip is set via innerHTML, and bestLabel can be a raw
    // SSID or device name straight off the air, escape it like the popup does.
    marker.bindTooltip(esc(bestLabel(p)), { direction: "top", opacity: 0.9 });
    if (!popupsSuppressed()) {
      marker.bindPopup(popupHtml(p, coords), {
        maxHeight: 320, maxWidth: 380, autoPanPadding: [24, 24]
      });
    }
    marker.on("click", function () {
      notifySelect("record", p, [coords[1], coords[0]]);
    });
    return marker;
  }

  // Draw this follower's route while its popup is open, and take it away
  // again when the popup closes, so only one trail is ever on the map.
  function attachTrail(marker, pts) {
    marker.on("popupopen", function () {
      trailLayer.clearLayers();
      L.polyline(pts, {
        color: "#e02424", weight: 2, opacity: 0.85, dashArray: "4,4"
      }).addTo(trailLayer);
      pts.forEach(function (ll) {
        L.circleMarker(ll, {
          radius: 3, color: "#e02424", weight: 1, fillColor: "#e02424",
          fillOpacity: 0.8, interactive: false
        }).addTo(trailLayer);
      });
    });
    marker.on("popupclose", function () { trailLayer.clearLayers(); });
  }

  // On a small window the expanded legend covers most of the map it is there
  // to annotate, so it starts as a one-line header you can click open. On a
  // normal window it starts open, because that's where it costs nothing.
  var legendCollapsed = (window.innerHeight || 0) < 520
    || (window.innerWidth || 0) < 620;

  function legendSection(title, rows) {
    if (!rows.length) { return ""; }
    var html = "<div class='legend-title'>" + esc(title) + "</div>";
    rows.forEach(function (row) {
      html += "<div class='legend-row'><span class='legend-swatch'" +
        (row.dashed ? " data-dashed='1'" : " style='background:" + row.color + "'") +
        "></span>" + esc(row.label) +
        (row.count ? " <span class='legend-count'>" + row.count + "</span>" : "") +
        "</div>";
    });
    return html;
  }

  // The legend only lists what's actually on the map. A fixed legend showing
  // every category warmap knows about takes up half the window and spends
  // most of it telling you about things your capture doesn't contain.
  function rebuildLegend(tally) {
    var el = document.getElementById("legend");
    tally = tally || {};
    var types = tally.types || {};
    var enc = tally.enc || {};
    var codes = tally.codes || {};

    var html = "<div class='legend-head'>Legend<span class='legend-toggle'>" +
      (legendCollapsed ? "+" : "−") + "</span></div>";

    if (!legendCollapsed) {
      html += legendSection("Wi-Fi encryption", Object.keys(ENC_COLORS)
        .filter(function (b) { return enc[b]; })
        .map(function (b) {
          return { label: b, color: ENC_COLORS[b], count: enc[b] };
        }));

      html += legendSection("Sub-GHz code", ["static", "rolling", "raw", "unknown"]
        .filter(function (c) { return codes[c]; })
        .map(function (c) {
          var label = c === "static" ? "Fixed (replayable)"
            : c === "rolling" ? "Rolling"
            : c === "raw" ? "Raw capture" : "Unrecognized";
          return { label: label, color: RISK_COLORS[c], count: codes[c] };
        }));

      html += legendSection("Other types", ["CLIENT", "BLE", "BT", "CELL", "NFC", "RFID", "IBUTTON", "IR"]
        .filter(function (t) { return types[t]; })
        .map(function (t) {
          return { label: TYPE_LABELS[t], color: TYPE_COLORS[t], count: types[t] };
        }));

      if (tally.inferred) {
        html += legendSection("Position", [
          { label: "Inferred from GPS track", dashed: true, count: tally.inferred },
        ]);
      }

      if (tally.followers) {
        html += legendSection("Followed you", [
          { label: "Possible follower", color: "#e02424", count: tally.followers },
        ]);
        html += "<div class='legend-note legend-subnote'>Click a ringed dot to " +
          "trace where that device was seen.</div>";
      }

      if (alprCount && alprVisible) {
        html += legendSection("Surveillance", [
          { label: "ALPR camera (DeFlock/OSM)", color: ALPR_COLOR, count: alprCount },
        ]);
        var alprNote = "Wedge shows which way a camera faces. Reference data, "
          + "not a capture.";
        if (alprSampled) {
          alprNote = "Showing an even sample of " + alprShown + " here. Zoom in "
            + "to load every camera in view. " + alprNote;
        }
        html += "<div class='legend-note legend-subnote'>" + alprNote + "</div>";
      }

      html += "<div class='legend-note'>Dot size = signal strength " +
        "(bigger = stronger). Hover any dot for its label; click for full detail.</div>";
    }

    el.innerHTML = html;
    var head = el.querySelector(".legend-head");
    if (head) {
      head.onclick = function () {
        legendCollapsed = !legendCollapsed;
        rebuildLegend(lastTally);
      };
    }
  }

  var lastTally = {};
  rebuildLegend({});

  function rebuildLayerControl(counts) {
    lastCounts = counts || {};
    if (layerControl) {
      map.removeControl(layerControl);
      layerControl = null;
    }
    var overlays = {};
    ALL_TYPES.forEach(function (t) {
      var n = (counts && counts[t]) || 0;
      if (!n) { return; }
      overlays[TYPE_LABELS[t] + " (" + n + ")"] = layers[t];
    });
    if (trackLayer.getLayers().length) {
      overlays["GPS track"] = trackLayer;
    }
    if (followerLayer.getLayers().length) {
      overlays["⚠ Possible followers"] = followerLayer;
    }
    if (alprCount) {
      overlays["ALPR cameras (" + alprCount + ")"] = alprLayer;
    }
    if (Object.keys(overlays).length > 1) {
      layerControl = L.control.layers(null, overlays, {
        collapsed: true, position: "topright"
      }).addTo(map);
    }
  }

  // --- public API, called from Python via QWebEnginePage.runJavaScript ---

  window.warmapLoadData = function (geojson) {
    var fc = typeof geojson === "string" ? JSON.parse(geojson) : geojson;
    currentFeatures = fc.features || [];
    markerIndex = {};

    var counts = {};
    var tally = { types: counts, enc: {}, codes: {}, inferred: 0, followers: 0 };
    ALL_TYPES.forEach(function (t) { layers[t].clearLayers(); });
    followerLayer.clearLayers();
    trailLayer.clearLayers();

    var buckets = {};
    ALL_TYPES.forEach(function (t) { buckets[t] = []; });

    var skipped = 0;
    currentFeatures.forEach(function (feature) {
      var p = feature.properties || {};
      var coords = validCoords(feature);
      if (!coords) { skipped += 1; return; }
      var type = ALL_TYPES.indexOf(p.type) !== -1 ? p.type : "WIFI";
      var marker = makeMarker(feature, coords);
      buckets[type].push(marker);
      counts[type] = (counts[type] || 0) + 1;
      markerIndex[type + "|" + p.bssid] = marker;

      if (type === "WIFI" && p.enc_bucket) {
        tally.enc[p.enc_bucket] = (tally.enc[p.enc_bucket] || 0) + 1;
      }
      var code = p.meta && p.meta.code_type;
      if (code) { tally.codes[code] = (tally.codes[code] || 0) + 1; }
      if (p.geo_source === "track") { tally.inferred += 1; }

      // A follower gets a red warning ring at its strongest position, and its
      // trail through every distinct spot it was seen is armed to draw when
      // you open its popup.
      if (isFollower(p)) {
        tally.followers += 1;
        L.circleMarker([coords[1], coords[0]], {
          radius: 13, color: "#e02424", weight: 3, opacity: 0.9,
          fill: false, interactive: false
        }).addTo(followerLayer);
        var pts = (p.meta.locations || []).map(function (ll) { return [ll[0], ll[1]]; });
        if (pts.length > 1) { attachTrail(marker, pts); }
      }
    });

    ALL_TYPES.forEach(function (t) {
      if (!buckets[t].length) { return; }
      if (CLUSTERED_TYPES.indexOf(t) !== -1) {
        layers[t].addLayers(buckets[t]);
      } else {
        buckets[t].forEach(function (m) { layers[t].addLayer(m); });
      }
    });

    if (heatLayer) {
      map.removeLayer(heatLayer);
      heatLayer = null;
    }
    var heatPoints = [];
    currentFeatures.forEach(function (f) {
      var c = validCoords(f);
      if (c) { heatPoints.push([c[1], c[0], 0.5]); }
    });
    heatLayer = L.heatLayer(heatPoints, { radius: 20, blur: 15, maxZoom: 17 });
    if (heatVisible) {
      heatLayer.addTo(map);
    }

    lastTally = tally;
    rebuildLegend(tally);
    rebuildLayerControl(counts);
    if (skipped) {
      console.warn("warmap: " + skipped + " record(s) had no usable coordinate "
        + "and were left off the map");
    }
  };

  window.warmapSetTrack = function (feature) {
    trackLayer.clearLayers();
    if (!feature) { return; }
    var f = typeof feature === "string" ? JSON.parse(feature) : feature;
    var features = f.type === "FeatureCollection" ? f.features : [f];
    features.forEach(function (item) {
      var coords = (item.geometry && item.geometry.coordinates) || [];
      if (coords.length < 2) { return; }
      var latlngs = coords.map(function (c) { return [c[1], c[0]]; });
      var props = item.properties || {};
      // A track rebuilt from wardrive rows is drawn dashed: the points are
      // real fixes, but they only exist where the wardrive saw something, so
      // the line between them is a coarser guess than a logged track's.
      L.polyline(latlngs, {
        color: "#1f6fd0",
        weight: 3,
        opacity: props.derived ? 0.6 : 0.75,
        dashArray: props.derived ? "6,6" : null
      }).addTo(trackLayer);
      // Track endpoints get popups on the desktop like everything else, and
      // none on the phone. The mobile host suppresses popups so a tap opens
      // its own sheet, and these two were the one place that wasn't honoured.
      var startMarker = L.circleMarker(latlngs[0], {
        radius: 6, color: "#1f6fd0", fillColor: "#ffffff", fillOpacity: 1, weight: 2
      });
      var endMarker = L.circleMarker(latlngs[latlngs.length - 1], {
        radius: 6, color: "#1f6fd0", fillColor: "#1f6fd0", fillOpacity: 1, weight: 2
      });
      if (!popupsSuppressed()) {
        startMarker.bindPopup("Track start<br>" + esc(props.start || ""));
        endMarker.bindPopup("Track end<br>" + esc(props.end || "") +
          "<br>" + esc(props.distance_km || 0) + " km" +
          (props.derived ? "<br><em>Rebuilt from wardrive rows</em>" : ""));
      }
      startMarker.addTo(trackLayer);
      endMarker.addTo(trackLayer);
    });
  };

  window.warmapSetHeatmapVisible = function (visible) {
    heatVisible = !!visible;
    if (!heatLayer) {
      return;
    }
    if (heatVisible) {
      if (!map.hasLayer(heatLayer)) {
        heatLayer.addTo(map);
      }
    } else if (map.hasLayer(heatLayer)) {
      map.removeLayer(heatLayer);
    }
  };

  window.warmapSetTypeVisible = function (type, visible) {
    var layer = layers[type];
    if (!layer) { return; }
    if (visible && !map.hasLayer(layer)) {
      map.addLayer(layer);
    } else if (!visible && map.hasLayer(layer)) {
      map.removeLayer(layer);
    }
  };

  window.warmapFocus = function (type, bssid) {
    var marker = markerIndex[type + "|" + bssid];
    var group = layers[type];
    if (type === "ALPR") {
      marker = alprIndex[bssid];
      group = alprLayer;
    }
    if (!marker) { return false; }
    var latlng = marker.getLatLng();
    map.setView(latlng, Math.max(map.getZoom(), 17), { animate: true });
    // A marker inside a cluster group has to be spidered out before its
    // popup can open, which markercluster does via zoomToShowLayer.
    if (group && typeof group.zoomToShowLayer === "function") {
      group.zoomToShowLayer(marker, function () { marker.openPopup(); });
    } else {
      marker.openPopup();
    }
    return true;
  };

  // Populate (or replace) the ALPR camera overlay. Called once after the
  // snapshot loads and again on a refresh, never per filter change, because
  // the whole worldwide set is a lot to hand across the bridge and toggling
  // visibility (warmapSetAlprVisible) is what a filter actually needs.
  window.warmapSetAlpr = function (geojson) {
    var fc = typeof geojson === "string" ? JSON.parse(geojson) : geojson;
    alprFeatures = (fc && fc.features) || [];
    alprCount = alprFeatures.length;
    renderAlprViewport();
    rebuildLegend(lastTally);
    rebuildLayerControl(lastCounts);
  };

  window.warmapSetAlprVisible = function (visible) {
    visible = !!visible;
    // The main window calls this on every filter change (see mainwindow
    // _refresh), so an unchanged value must do nothing, otherwise every
    // keystroke in the search box would clear and rebuild the whole viewport.
    if (visible === alprVisible) { return; }
    alprVisible = visible;
    if (alprVisible) {
      renderAlprViewport();
    } else {
      alprLayer.clearLayers();
      alprIndex = {};
      alprShown = 0;
      if (map.hasLayer(alprLayer)) { map.removeLayer(alprLayer); }
    }
    rebuildLegend(lastTally);
  };

  // Center the map on a point at a given zoom. Used to jump to a place
  // (and by tests/screenshots to drive the view); the moveend handler then
  // renders the cameras that come into view.
  window.warmapSetView = function (lat, lon, zoom) {
    map.setView([lat, lon], zoom);
    return true;
  };

  // Where the person holding the phone is. The desktop never calls this (it
  // has no GPS); the mobile app calls it from watchPosition. Passing null
  // takes the marker away again. Kept here rather than handing the map object
  // out, so map.js stays the only thing that touches Leaflet.
  var meMarker = null;
  var meAccuracy = null;
  window.warmapSetMyLocation = function (lat, lon, accuracyMeters) {
    if (lat === null || lat === undefined) {
      if (meMarker) { map.removeLayer(meMarker); meMarker = null; }
      if (meAccuracy) { map.removeLayer(meAccuracy); meAccuracy = null; }
      return false;
    }
    var ll = [lat, lon];
    if (!meMarker) {
      meMarker = L.marker(ll, {
        icon: L.divIcon({
          className: "", html: '<div class="me-dot"></div>',
          iconSize: [16, 16], iconAnchor: [8, 8]
        }),
        interactive: false, zIndexOffset: 1000
      }).addTo(map);
    } else {
      meMarker.setLatLng(ll);
    }
    // The accuracy circle is the honest part: a phone fix is a claim with a
    // radius, and drawing only a dot overstates it.
    if (typeof accuracyMeters === "number" && isFinite(accuracyMeters)
        && accuracyMeters > 0) {
      if (!meAccuracy) {
        meAccuracy = L.circle(ll, {
          radius: accuracyMeters, color: "#2f8fe0", weight: 1,
          fillColor: "#2f8fe0", fillOpacity: 0.12, interactive: false
        }).addTo(map);
      } else {
        meAccuracy.setLatLng(ll);
        meAccuracy.setRadius(accuracyMeters);
      }
    }
    return true;
  };

  window.warmapFitToData = function () {
    var points = [];
    currentFeatures.forEach(function (f) {
      var c = validCoords(f);
      if (c) { points.push([c[1], c[0]]); }
    });
    trackLayer.eachLayer(function (layer) {
      if (typeof layer.getLatLngs === "function") {
        layer.getLatLngs().forEach(function (ll) { points.push([ll.lat, ll.lng]); });
      }
    });
    if (!points.length) {
      return;
    }
    map.fitBounds(L.latLngBounds(points), { padding: [30, 30], maxZoom: 17 });
  };

  window.warmapReady = true;
})();
