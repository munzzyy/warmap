/* warmap mobile, the app around the shared map renderer.
 *
 * map.js does all the drawing and is the exact same file the desktop window
 * loads. This file is the phone's half: it fetches the dataset from the PC,
 * keeps a copy in IndexedDB so the app still opens with the data when the PC
 * is off, and puts a touch UI over the top: a bottom sheet instead of map
 * popups, big targets, a filter pane, and the user's own position.
 *
 * Deliberate constraints, learned rather than assumed:
 *
 * - Served over plain HTTP on a LAN address, the page is NOT a secure context.
 *   Service workers, install prompts and geolocation are all unavailable there.
 *   IndexedDB is not, which is why the offline copy lives in IndexedDB and the
 *   rest degrades with an honest message instead of a broken button.
 * - The camera overlay can be ~137k points. They are handed to map.js, which
 *   already renders only what is in view; nothing here tries to hold that many
 *   DOM nodes.
 * - Every value that reaches the DOM goes through `text()` or is set with
 *   textContent. Names come off the air and out of OpenStreetMap and are not
 *   trusted.
 */
(function () {
  "use strict";

  // The bottom sheet replaces Leaflet's popups. Set before any data loads, so
  // map.js sees it when it builds markers.
  window.warmapNoPopups = true;

  var $ = function (id) { return document.getElementById(id); };
  var DB_NAME = "warmap";
  var DB_VERSION = 1;
  var STORE = "payloads";
  var LIST_PAGE = 60;

  // Everything is served under /s/<token>/, so relative URLs already carry the
  // token. Derived from the document rather than assumed, so the app works
  // unchanged from a saved offline copy at some other path.
  var BASE = location.pathname.replace(/[^/]*$/, "");

  var state = {
    records: { type: "FeatureCollection", features: [] },
    cameras: null,          // transfer payload as received
    cameraFeatures: [],     // rehydrated for map.js
    tracks: null,
    session: {},
    filtered: [],
    listShown: LIST_PAGE,
    selected: null,
    search: "",
    types: {},              // type -> boolean
    showAlpr: true,
    online: false,
    watchId: null,
    meMarker: null,
  };

  // --- tiny helpers -------------------------------------------------------

  function text(v) { return v === null || v === undefined ? "" : String(v); }

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (txt !== undefined) { n.textContent = text(txt); }
    return n;
  }

  function num(n) {
    return typeof n === "number" && isFinite(n) ? n.toLocaleString() : "0";
  }

  var toastTimer = null;
  function toast(message) {
    var t = $("toast");
    t.textContent = text(message);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 2600);
  }

  function setStatus(kind, label) {
    $("status-dot").className = "chip-dot " + (kind ? "is-" + kind : "");
    $("status-text").textContent = text(label);
  }

  // --- storage ------------------------------------------------------------
  // One object store keyed by payload name. The dataset is written as a single
  // record per payload rather than a row per camera: it is read back all at
  // once anyway, and one put beats 137,000 of them by orders of magnitude.

  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error("no IndexedDB")); return; }
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) { db.createObjectStore(STORE); }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  // All four payloads go in ONE transaction. Written separately, an interrupted
  // save (the app backgrounded, the tab killed, quota hit) could leave last
  // week's records sitting next to this week's cameras and a session line
  // describing neither, a mismatch nothing downstream could detect. One
  // transaction means the saved copy is either the old one or the new one.
  function dbPutAll(entries) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, "readwrite");
        var store = tx.objectStore(STORE);
        Object.keys(entries).forEach(function (key) {
          store.put(entries[key], key);
        });
        tx.oncomplete = function () { db.close(); resolve(true); };
        tx.onerror = function () { db.close(); reject(tx.error); };
        tx.onabort = function () { db.close(); reject(tx.error); };
      });
    });
  }

  function dbGet(key) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, "readonly");
        var req = tx.objectStore(STORE).get(key);
        req.onsuccess = function () { db.close(); resolve(req.result); };
        req.onerror = function () { db.close(); reject(req.error); };
      });
    });
  }

  // --- network ------------------------------------------------------------

  function api(name, timeoutMs) {
    var ctrl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) { ctrl.abort(); } },
                           timeoutMs || 20000);
    return fetch(BASE + "api/" + name, {
      signal: ctrl ? ctrl.signal : undefined,
      cache: "no-store",
    }).then(function (r) {
      clearTimeout(timer);
      if (!r.ok) { throw new Error(name + ": HTTP " + r.status); }
      return r.json();
    }, function (err) {
      clearTimeout(timer);
      throw err;
    });
  }

  // --- camera rehydration -------------------------------------------------
  // The transfer payload is positional rows plus a `fields` list naming the
  // columns. Reading by name means the app keeps working if the column order
  // ever changes, instead of silently mapping the wrong values.

  function camerasToFeatures(payload) {
    if (!payload || !payload.cameras) { return []; }
    var fields = payload.fields || ["lon", "lat", "dirs", "mfg", "op", "flock", "id"];
    var ix = {};
    fields.forEach(function (name, i) { ix[name] = i; });
    var out = [];
    payload.cameras.forEach(function (row) {
      var lon = row[ix.lon], lat = row[ix.lat];
      if (typeof lon !== "number" || typeof lat !== "number") { return; }
      out.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [lon, lat] },
        properties: {
          id: row[ix.id] || "",
          dir: row[ix.dirs] || [],
          mfg: row[ix.mfg] || "",
          op: row[ix.op] || "",
          mount: "", zone: "", brand: "", ref: "",
          flock: !!row[ix.flock],
        },
      });
    });
    return out;
  }

  // --- map plumbing -------------------------------------------------------

  function pushToMap() {
    if (!window.warmapLoadData) { return; }
    window.warmapLoadData({ type: "FeatureCollection", features: state.filtered });
    if (window.warmapSetTrack) { window.warmapSetTrack(state.tracks || null); }
  }

  function pushCameras() {
    if (!window.warmapSetAlpr) { return; }
    window.warmapSetAlpr({ type: "FeatureCollection", features: state.cameraFeatures });
    if (window.warmapSetAlprVisible) { window.warmapSetAlprVisible(state.showAlpr); }
  }

  // --- filtering ----------------------------------------------------------

  function applyFilters() {
    var q = state.search.trim().toLowerCase();
    state.filtered = (state.records.features || []).filter(function (f) {
      var p = f.properties || {};
      if (state.types[p.type] === false) { return false; }
      if (!q) { return true; }
      if (String(p.ssid || "").toLowerCase().indexOf(q) !== -1) { return true; }
      if (String(p.bssid || "").toLowerCase().indexOf(q) !== -1) { return true; }
      if (String(p.vendor || "").toLowerCase().indexOf(q) !== -1) { return true; }
      var meta = p.meta || {};
      for (var k in meta) {
        if (!Object.prototype.hasOwnProperty.call(meta, k)) { continue; }
        var v = meta[k];
        if (typeof v === "string" && v.toLowerCase().indexOf(q) !== -1) { return true; }
      }
      return false;
    });
    state.listShown = LIST_PAGE;
    pushToMap();
    renderPeek();
    renderList();
    renderStats();
  }

  // --- rendering: peek ----------------------------------------------------

  function renderPeek() {
    var total = (state.records.features || []).length;
    var shown = state.filtered.length;
    $("peek-count").textContent = num(shown);
    $("peek-label").textContent = shown === 1 ? "record" : "records";
    var bits = [];
    if (shown !== total) { bits.push("of " + num(total)); }
    if (state.cameraFeatures.length) {
      bits.push(num(state.cameraFeatures.length) + " cameras");
    }
    $("peek-sub").textContent = bits.join(" · ");
  }

  // --- rendering: records list -------------------------------------------

  var TYPE_LABELS = {
    WIFI: "Wi-Fi", CLIENT: "Wi-Fi client", BLE: "Bluetooth LE", BT: "Bluetooth",
    CELL: "Cell", SUBGHZ: "Sub-GHz", NFC: "NFC", RFID: "RFID", IBUTTON: "iButton",
    IR: "Infrared",
  };
  var TYPE_COLORS = {
    WIFI: "#2f8f3f", CLIENT: "#c9a227", BLE: "#3b7fd0", BT: "#5b5bd0",
    CELL: "#9b5bd0", SUBGHZ: "#d08b1a", NFC: "#d03b8b", RFID: "#c0506b",
    IBUTTON: "#8b6b3b", IR: "#d0503b",
  };
  var ENC_COLORS = {
    "Open": "#d03b3b", "WEP": "#e07b1a", "WPA": "#3f9142", "WPA2": "#2f8f3f",
    "WPA3": "#1f8f5a", "WPA2/3-mixed": "#2f8f6f", "Unknown": "#8a8a8a",
  };

  function colorFor(p) {
    if (p.type === "WIFI") { return ENC_COLORS[p.enc_bucket] || ENC_COLORS.Unknown; }
    return TYPE_COLORS[p.type] || "#8a8a8a";
  }

  function recordName(p) {
    if (p.ssid) { return p.ssid; }
    if (p.meta && p.meta.tracker) { return p.meta.tracker; }
    if (p.vendor) { return p.vendor; }
    return p.bssid || "(unnamed)";
  }

  function renderList() {
    var host = $("record-list");
    host.textContent = "";
    var items = state.filtered.slice(0, state.listShown);
    if (!items.length) {
      var empty = el("div", "empty");
      empty.appendChild(el("p", null,
        state.records.features && state.records.features.length
          ? "Nothing matches that filter."
          : "No records yet. Sync from your PC to pull them in."));
      host.appendChild(empty);
      $("btn-more").hidden = true;
      return;
    }
    items.forEach(function (f) {
      var p = f.properties || {};
      var row = el("div", "row");
      var dot = el("span", "row-dot");
      dot.style.background = colorFor(p);
      row.appendChild(dot);

      var main = el("div", "row-main");
      main.appendChild(el("div", "row-title", recordName(p)));
      var sub = [TYPE_LABELS[p.type] || p.type];
      if (p.type === "WIFI" && p.enc_bucket) { sub.push(p.enc_bucket); }
      if (p.vendor && p.vendor !== recordName(p)) { sub.push(p.vendor); }
      main.appendChild(el("div", "row-sub", sub.join(" · ")));
      row.appendChild(main);

      if (p.rssi) { row.appendChild(el("div", "row-meta", p.rssi + " dBm")); }

      row.addEventListener("click", function () {
        showDetail("record", p, [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
        if (window.warmapFocus) { window.warmapFocus(p.type, p.bssid); }
      });
      host.appendChild(row);
    });
    $("btn-more").hidden = state.filtered.length <= state.listShown;
  }

  // --- rendering: detail --------------------------------------------------

  var META_LABELS = {
    tracker: "Tracker", tracker_basis: "Identified by", company: "Company",
    address_type: "Address type", protocol: "Protocol", key: "Key",
    code_type_label: "Code type", band: "Band", frequency_note: "Typically",
    preset_label: "Modulation", technology: "Technology", uid_bytes: "UID bytes",
    services: "Services", probing_for: "Asked for networks",
    probing_for_more: "More networks asked for",
    geo_match_seconds: "Track match (s off)", mac_randomized: "Randomized MAC",
  };

  function kvList(pairs) {
    var dl = el("dl", "kv");
    pairs.forEach(function (pair) {
      if (pair[1] === undefined || pair[1] === null || pair[1] === "") { return; }
      dl.appendChild(el("dt", null, pair[0]));
      dl.appendChild(el("dd", null, pair[1]));
    });
    return dl;
  }

  function showDetail(kind, p, latlng) {
    state.selected = { kind: kind, props: p, latlng: latlng };
    var host = $("detail");
    host.textContent = "";
    $("detail-empty").hidden = true;

    var head = el("div", "detail-head");
    var dot = el("span", "row-dot");
    dot.style.background = kind === "alpr" ? "#7b2fb5" : colorFor(p);
    dot.style.marginTop = "6px";
    head.appendChild(dot);
    var titleWrap = el("div");

    if (kind === "alpr") {
      titleWrap.appendChild(el("div", "detail-title",
        p.op || p.mfg || p.brand || "ALPR camera"));
      var kindLine = el("div", "detail-kind",
        p.flock ? "Flock Safety ALPR camera" : "ALPR camera");
      kindLine.style.color = "#a970d8";
      titleWrap.appendChild(kindLine);
      head.appendChild(titleWrap);
      host.appendChild(head);

      var facing = (p.dir || []).map(function (d) { return Math.round(d) + "°"; }).join(", ");
      host.appendChild(kvList([
        ["Manufacturer", p.mfg], ["Operator", p.op], ["Facing", facing],
        ["Mount", p.mount], ["Zone", p.zone], ["Ref", p.ref], ["OSM id", p.id],
      ]));
      var note = el("div", "note",
        "Reference data from DeFlock / OpenStreetMap (© OpenStreetMap contributors), not one of your captures.");
      host.appendChild(note);
    } else {
      titleWrap.appendChild(el("div", "detail-title", recordName(p)));
      var kl = el("div", "detail-kind", TYPE_LABELS[p.type] || p.type);
      kl.style.color = colorFor(p);
      titleWrap.appendChild(kl);
      head.appendChild(titleWrap);
      host.appendChild(head);

      var pairs = [
        ["Address", p.bssid], ["Vendor", p.vendor],
        ["Encryption", p.type === "WIFI" ? p.enc_bucket : ""],
        ["Auth mode", p.auth_mode], ["Channel", p.channel],
        ["Frequency", p.frequency ? p.frequency + " MHz" : ""],
        ["Signal", p.rssi ? p.rssi + " dBm" : ""],
        ["First seen", p.first_seen], ["Times seen", p.times_seen],
      ];
      var meta = p.meta || {};
      Object.keys(META_LABELS).forEach(function (k) {
        var v = meta[k];
        if (v === undefined || v === null || v === "") { return; }
        if (Array.isArray(v)) { v = v.join(", "); }
        pairs.push([META_LABELS[k], v]);
      });
      if (p.source) { pairs.push(["From", String(p.source).split("/").pop()]); }
      host.appendChild(kvList(pairs));

      if (p.geo_source === "track") {
        host.appendChild(el("div", "note",
          "Position inferred by matching this capture's timestamp to the GPS track. Not a recorded fix."));
      }
      if (meta.span_m >= 150 && (p.type === "BLE" || p.type === "BT" || meta.tracker)) {
        host.appendChild(el("div", "note warn",
          "Possible follower: seen at " + text(meta.location_count) +
          " spots across " + Math.round(meta.span_m) + " m of your route."));
      }
    }

    if (latlng) {
      var actions = el("div", "detail-actions");
      var center = el("button", "btn primary", "Center on map");
      center.addEventListener("click", function () {
        if (window.warmapSetView) { window.warmapSetView(latlng[0], latlng[1], 18); }
        setSheet("peek");
      });
      actions.appendChild(center);
      host.appendChild(actions);
      host.appendChild(kvList([["Lat, Lon",
        latlng[0].toFixed(6) + ", " + latlng[1].toFixed(6)]]));
    }

    selectTab("detail");
    if ($("sheet").dataset.state === "peek") { setSheet("half"); }
  }

  // map.js calls this on every tap because the app set warmapNoPopups.
  window.warmapOnSelect = function (kind, props, latlng) {
    showDetail(kind, props || {}, latlng);
  };

  // --- rendering: stats ---------------------------------------------------

  function renderStats() {
    var host = $("stats");
    host.textContent = "";
    var feats = state.filtered;

    var byType = {}, byEnc = {}, located = 0, trackers = 0, open = 0;
    feats.forEach(function (f) {
      var p = f.properties || {};
      byType[p.type] = (byType[p.type] || 0) + 1;
      if (p.type === "WIFI") {
        byEnc[p.enc_bucket] = (byEnc[p.enc_bucket] || 0) + 1;
        if (p.enc_bucket === "Open") { open += 1; }
      }
      if (p.meta && p.meta.tracker) { trackers += 1; }
      located += 1;
    });

    var grid = el("div", "stat-grid");
    [["Records", num(feats.length)], ["Open Wi-Fi", num(open)],
     ["Trackers", num(trackers)], ["Cameras", num(state.cameraFeatures.length)]
    ].forEach(function (pair) {
      var card = el("div", "stat");
      card.appendChild(el("b", null, pair[1]));
      card.appendChild(el("span", null, pair[0]));
      grid.appendChild(card);
    });
    host.appendChild(grid);

    function bars(title, counts, colorOf) {
      var keys = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
      if (!keys.length) { return; }
      host.appendChild(el("div", "section-title", title));
      var max = counts[keys[0]] || 1;
      keys.forEach(function (k) {
        var row = el("div", "bar-row");
        row.appendChild(el("div", "bar-label", TYPE_LABELS[k] || k));
        var track = el("div", "bar-track");
        var fill = el("div", "bar-fill");
        fill.style.width = Math.max(3, (counts[k] / max) * 100) + "%";
        fill.style.background = colorOf(k);
        track.appendChild(fill);
        row.appendChild(track);
        row.appendChild(el("div", "bar-count", num(counts[k])));
        host.appendChild(row);
      });
    }

    bars("By type", byType, function (k) { return TYPE_COLORS[k] || "#8a8a8a"; });
    bars("Wi-Fi encryption", byEnc, function (k) { return ENC_COLORS[k] || "#8a8a8a"; });
  }

  // --- rendering: filters -------------------------------------------------

  function renderFilters() {
    var host = $("filters");
    host.textContent = "";
    var counts = {};
    (state.records.features || []).forEach(function (f) {
      var t = (f.properties || {}).type;
      counts[t] = (counts[t] || 0) + 1;
    });

    host.appendChild(el("div", "section-title", "Overlay"));
    host.appendChild(checkRow("ALPR cameras", state.cameraFeatures.length,
      state.showAlpr, function (on) {
        state.showAlpr = on;
        if (window.warmapSetAlprVisible) { window.warmapSetAlprVisible(on); }
      }));

    host.appendChild(el("div", "section-title", "Record types"));
    Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; })
      .forEach(function (t) {
        host.appendChild(checkRow(TYPE_LABELS[t] || t, counts[t],
          state.types[t] !== false, function (on) {
            state.types[t] = on;
            applyFilters();
          }, TYPE_COLORS[t]));
      });
  }

  function checkRow(label, count, checked, onChange, color) {
    var wrap = el("label", "check");
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!checked;
    wrap.appendChild(input);
    wrap.appendChild(el("span", "check-box"));
    if (color) {
      var dot = el("span", "row-dot");
      dot.style.background = color;
      wrap.appendChild(dot);
    }
    wrap.appendChild(el("span", "check-label", label));
    wrap.appendChild(el("span", "check-count", num(count)));
    input.addEventListener("change", function () { onChange(input.checked); });
    return wrap;
  }

  // --- bottom sheet -------------------------------------------------------

  var SNAP = ["peek", "half", "full"];

  function setSheet(stateName) {
    $("sheet").dataset.state = stateName;
    // Keep the floating buttons above the sheet's top edge.
    $("fabs").style.bottom = stateName === "full"
      ? "calc(86vh + 12px)"
      : stateName === "half"
        ? "calc(47vh + 12px)"
        : "";
  }

  function initSheet() {
    var sheet = $("sheet");
    var grip = $("sheet-grip");
    var startY = 0, startTranslate = 0, dragging = false, height = 0;

    function currentTranslate() {
      var m = new DOMMatrixReadOnly(getComputedStyle(sheet).transform);
      return m.m42 || 0;
    }

    grip.addEventListener("pointerdown", function (e) {
      dragging = true;
      height = sheet.offsetHeight;
      startY = e.clientY;
      startTranslate = currentTranslate();
      sheet.classList.add("is-dragging");
      grip.setPointerCapture(e.pointerId);
    });

    grip.addEventListener("pointermove", function (e) {
      if (!dragging) { return; }
      var dy = e.clientY - startY;
      var next = Math.max(0, startTranslate + dy);
      sheet.style.transform = "translateY(" + next + "px)";
    });

    function endDrag(e) {
      if (!dragging) { return; }
      dragging = false;
      sheet.classList.remove("is-dragging");
      var current = currentTranslate();
      sheet.style.transform = "";
      // Snap to whichever point is closest to where the finger let go.
      var peekY = height - (parseFloat(getComputedStyle(document.documentElement)
        .getPropertyValue("--sheet-peek")) || 92);
      var points = [{ name: "full", y: 0 },
                    { name: "half", y: height * 0.45 },
                    { name: "peek", y: peekY }];
      var best = points[0];
      points.forEach(function (pt) {
        if (Math.abs(pt.y - current) < Math.abs(best.y - current)) { best = pt; }
      });
      setSheet(best.name);
      if (e && e.pointerId !== undefined) {
        try { grip.releasePointerCapture(e.pointerId); } catch (err) { /* already gone */ }
      }
    }

    grip.addEventListener("pointerup", endDrag);
    grip.addEventListener("pointercancel", endDrag);

    // Tap the grip to cycle, and make it keyboard-reachable.
    grip.addEventListener("click", function () {
      var i = SNAP.indexOf(sheet.dataset.state);
      setSheet(SNAP[(i + 1) % SNAP.length]);
    });
    grip.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); grip.click(); }
    });
  }

  function selectTab(name) {
    document.querySelectorAll(".tab").forEach(function (t) {
      var on = t.dataset.tab === name;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".pane").forEach(function (p) {
      p.classList.toggle("is-active", p.dataset.pane === name);
    });
  }

  // --- location -----------------------------------------------------------

  function toggleLocate() {
    if (state.watchId !== null) {
      navigator.geolocation.clearWatch(state.watchId);
      state.watchId = null;
      $("btn-locate").classList.remove("is-on");
      if (window.warmapSetMyLocation) { window.warmapSetMyLocation(null); }
      return;
    }
    if (!navigator.geolocation) { toast("This browser has no location support."); return; }
    if (!window.isSecureContext) {
      // Worth saying plainly rather than letting the permission silently fail.
      toast("Location needs a secure (https) connection.");
      return;
    }
    $("btn-locate").classList.add("is-on");
    var centeredOnce = false;
    state.watchId = navigator.geolocation.watchPosition(function (pos) {
      var c = pos.coords;
      if (window.warmapSetMyLocation) {
        window.warmapSetMyLocation(c.latitude, c.longitude, c.accuracy);
      }
      // Recentre on the first fix only. Yanking the map back on every update
      // makes it impossible to look anywhere else while tracking.
      if (!centeredOnce && window.warmapSetView) {
        centeredOnce = true;
        window.warmapSetView(c.latitude, c.longitude, 17);
      }
    }, function (err) {
      // Tear the watch down rather than just forgetting its id. An error is
      // not the end of a watch: watchPosition keeps running and keeps firing,
      // so dropping the reference here would leave it updating the map with
      // the button showing off, and the next tap would start a second one on
      // top of it.
      if (state.watchId !== null) {
        navigator.geolocation.clearWatch(state.watchId);
        state.watchId = null;
      }
      $("btn-locate").classList.remove("is-on");
      if (window.warmapSetMyLocation) { window.warmapSetMyLocation(null); }
      toast(err && err.code === 1 ? "Location permission denied."
                                  : "Could not get a location fix.");
    }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 12000 });
  }

  // Stop the GPS when the app goes to the background: a watch left running is
  // the classic way a map app eats a battery.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden && state.watchId !== null) { toggleLocate(); }
  });

  // --- sync ---------------------------------------------------------------

  function hydrate(payloads) {
    if (payloads.records) { state.records = payloads.records; }
    if (payloads.tracks !== undefined) { state.tracks = payloads.tracks; }
    if (payloads.session) { state.session = payloads.session; }
    if (payloads.cameras) {
      state.cameras = payloads.cameras;
      state.cameraFeatures = camerasToFeatures(payloads.cameras);
    }
    applyFilters();
    renderFilters();
    pushCameras();
    if (window.warmapFitToData) { window.warmapFitToData(); }
    var when = state.session.generated || "";
    $("menu-sub").textContent = state.session.host
      ? "From " + state.session.host + (when ? " · " + when : "") : "";
    if (state.cameras && state.cameras.attribution) {
      $("attrib").textContent = state.cameras.attribution +
        " Map data © OpenStreetMap contributors.";
    }
  }

  function loadCached() {
    return Promise.all([
      dbGet("records"), dbGet("cameras"), dbGet("tracks"), dbGet("session"),
    ]).then(function (vals) {
      if (!vals[0] && !vals[1]) { return false; }
      hydrate({ records: vals[0], cameras: vals[1], tracks: vals[2], session: vals[3] });
      return true;
    }).catch(function () { return false; });
  }

  function sync(quiet) {
    setStatus("", "Syncing");
    return api("session").then(function (session) {
      state.online = true;
      return Promise.all([
        api("records", 60000),
        api("cameras", 120000),
        api("tracks"),
      ]).then(function (r) {
        hydrate({ session: session, records: r[0], cameras: r[1], tracks: r[2] });
        setStatus("live", "Live");
        // Persist after rendering, so the UI is usable while the write runs.
        return dbPutAll({
          session: session, records: r[0], cameras: r[1], tracks: r[2],
        }).then(function () {
          if (!quiet) { toast("Synced " + num((r[0].features || []).length) + " records"); }
        }).catch(function () {
          if (!quiet) { toast("Synced, but couldn't save an offline copy."); }
        });
      });
    }).catch(function () {
      state.online = false;
      var haveCopy = (state.records.features || []).length > 0;
      setStatus("offline", haveCopy ? "Offline copy" : "No PC");
      if (!quiet) {
        // Don't claim a saved copy when there isn't one. That reads as "your
        // data is here somewhere" when the real answer is "nothing loaded".
        toast(haveCopy
          ? "Can't reach the PC. Showing the saved copy."
          : "Can't reach the PC, and nothing is saved on this phone yet.");
      }
    });
  }

  // --- menu ---------------------------------------------------------------

  function openMenu(open) {
    $("menu").hidden = !open;
    $("scrim").hidden = !open;
    $("btn-menu").setAttribute("aria-expanded", open ? "true" : "false");
  }

  function applyTheme(name) {
    document.documentElement.dataset.theme = name;
    $("theme-sub").textContent = name === "light" ? "Light" : "Dark";
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) { meta.setAttribute("content", name === "light" ? "#f9f9f7" : "#0d0d0d"); }
    try { localStorage.setItem("warmap.theme", name); } catch (e) { /* private mode */ }
  }

  // --- boot ---------------------------------------------------------------

  function wire() {
    $("btn-menu").addEventListener("click", function () { openMenu(true); });
    $("scrim").addEventListener("click", function () { openMenu(false); });

    $("btn-sync").addEventListener("click", function () {
      openMenu(false);
      sync(false);
    });

    $("btn-offline").addEventListener("click", function () {
      openMenu(false);
      if (!navigator.storage || !navigator.storage.persist) {
        toast("This browser won't promise to keep the copy.");
        return;
      }
      navigator.storage.persist().then(function (granted) {
        toast(granted ? "Offline copy will be kept."
                      : "Saved, but the browser may clear it if space runs low.");
      });
    });

    $("btn-theme").addEventListener("click", function () {
      applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    });

    $("btn-fit").addEventListener("click", function () {
      if (window.warmapFitToData) { window.warmapFitToData(); }
    });
    $("btn-locate").addEventListener("click", toggleLocate);
    $("btn-layers").addEventListener("click", function () {
      selectTab("filters");
      setSheet("half");
    });

    var searchTimer = null;

    $("btn-search").addEventListener("click", function () {
      $("searchwrap").hidden = false;
      $("search").focus();
    });
    $("btn-search-close").addEventListener("click", function () {
      // Cancel any keystroke still waiting out its debounce. Without this the
      // last thing typed lands ~180 ms after the box is closed and cleared,
      // leaving the list filtered by a term nothing on screen still shows.
      clearTimeout(searchTimer);
      $("searchwrap").hidden = true;
      $("search").value = "";
      state.search = "";
      applyFilters();
    });

    $("search").addEventListener("input", function (e) {
      clearTimeout(searchTimer);
      var v = e.target.value;
      searchTimer = setTimeout(function () {
        state.search = v;
        applyFilters();
      }, 180);
    });

    $("btn-more").addEventListener("click", function () {
      state.listShown += LIST_PAGE;
      renderList();
    });

    document.querySelectorAll(".tab").forEach(function (t) {
      t.addEventListener("click", function () {
        selectTab(t.dataset.tab);
        if ($("sheet").dataset.state === "peek") { setSheet("half"); }
      });
    });
  }

  function boot() {
    try {
      applyTheme(localStorage.getItem("warmap.theme") || "dark");
    } catch (e) { applyTheme("dark"); }

    initSheet();
    wire();
    setStatus("", "Loading");

    // A service worker is what makes the app open with the PC off, but it
    // needs a secure context, which a plain-HTTP LAN address is not. Register
    // where it's possible and stay quiet where it isn't: the IndexedDB copy
    // still works either way.
    if (window.isSecureContext && "serviceWorker" in navigator) {
      navigator.serviceWorker.register(BASE + "sw.js", { scope: BASE })
        .catch(function () { /* not fatal, the app works without it */ });
    }

    // A single-file export carries its whole dataset inline and has no server
    // behind it, so it hydrates once and never syncs. Everything else about
    // the app (the map, the sheet, filters, search) is identical.
    if (window.WARMAP_EMBEDDED) {
      hydrate(window.WARMAP_EMBEDDED);
      var made = (window.WARMAP_EMBEDDED.session || {}).generated || "";
      setStatus("offline", "Saved copy");
      $("btn-sync").hidden = true;
      $("btn-offline").hidden = true;
      $("menu-version").textContent =
        "Offline copy" + (made ? ", saved " + made : "") +
        ". The map background needs internet; your records don't.";
      return;
    }

    $("menu-version").textContent = window.isSecureContext
      ? "" : "Served over plain HTTP: install and location are unavailable.";

    // Cached data first so the map is populated immediately, then refresh from
    // the PC in the background.
    loadCached().then(function (had) {
      if (had) { setStatus("offline", "Saved copy"); }
      return sync(had);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
