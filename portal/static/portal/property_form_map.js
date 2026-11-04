/* Property form map: local Leaflet + Leaflet.draw, OSM tiles.
 * Draw/edit/delete one polygon (boundary) + one marker (point).
 * Syncs to the visible fallback fields; never touches FarmLocation center.
 */
(function () {
  "use strict";

  function parseInitial() {
    var el = document.getElementById("property-map-data");
    if (!el) { return { boundary: null, point: null }; }
    try {
      var raw = JSON.parse(el.textContent);
      return {
        boundary: raw && raw.boundary ? raw.boundary : null,
        point: raw && raw.point ? raw.point : null,
        center: raw && raw.center ? raw.center : null
      };
    } catch (e) { return { boundary: null, point: null, center: null }; }
  }

  function field(name) {
    return document.querySelector('[name="' + name + '"]');
  }

  function num(v) {
    if (v === null || v === undefined || v === "") { return null; }
    var n = parseFloat(String(v).replace(",", "."));
    return isFinite(n) ? n : null;
  }

  function syncFromLayers(drawn) {
    var bField = field("boundary_geojson");
    var latField = field("point_latitude");
    var lonField = field("point_longitude");
    var poly = null;
    var marker = null;
    drawn.eachLayer(function (layer) {
      if (!poly && layer.toGeoJSON && layer instanceof L.Polygon && !(layer instanceof L.Rectangle)) {
        poly = layer;
      } else if (!marker && layer instanceof L.Marker) {
        marker = layer;
      }
    });
    if (bField) {
      if (poly) {
        try {
          bField.value = JSON.stringify(poly.toGeoJSON().geometry);
        } catch (e) { /* keep old value */ }
      } else {
        bField.value = "";
      }
    }
    if (marker) {
      var ll = marker.getLatLng();
      if (latField) { latField.value = String(Math.round(ll.lat * 1e6) / 1e6); }
      if (lonField) { lonField.value = String(Math.round(ll.lng * 1e6) / 1e6); }
    } else {
      if (latField) { latField.value = ""; }
      if (lonField) { lonField.value = ""; }
    }
  }

  function init() {
    var mapEl = document.getElementById("property-map");
    if (!mapEl || typeof L === "undefined") { return; }
    var initial = parseInitial();

    var lat = (initial.point && num(initial.point.lat) !== null) ? num(initial.point.lat) : null;
    var lon = (initial.point && num(initial.point.lon) !== null) ? num(initial.point.lon) : null;
    // READONLY default center from the server (selected FarmLocation or farm
    // first location). Geometry (fitBounds below) overrides the view.
    var defLat = (initial.center && num(initial.center.lat) !== null) ? num(initial.center.lat) : 46.25;
    var defLon = (initial.center && num(initial.center.lon) !== null) ? num(initial.center.lon) : 20.14;
    var center = (lat !== null && lon !== null) ? [lat, lon] : [defLat, defLon];

    var map = L.map("property-map").setView(center, 13);
    var tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      // Origin only: OSM tile policy requires a Referer; never the full page URL.
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    });
    // Visible guidance on tile failure (e.g. OSM 403). The warning is sticky:
    // a later tileload must NOT silently clear it. Manual fields stay usable.
    tiles.on("tileerror", function () {
      var s = document.getElementById("property-map-tiles-status");
      if (s) {
        var hu = ((document.documentElement && document.documentElement.lang) || "hu").toLowerCase().indexOf("hu") === 0;
        s.textContent = hu
          ? "A térkép csempéi nem tölthetők be (pl. OSM 403 tiltás). A lenti mezők kézzel továbbra is kitölthetők; a mentett adat kizárólag a mezők tartalma."
          : "Map tiles failed to load (e.g. OSM 403). You can still fill in the fields below manually; only the field values are saved.";
        s.hidden = false;
      }
    });
    tiles.addTo(map);

    var drawn = new L.FeatureGroup();
    map.addLayer(drawn);

    var hasBounds = false;
    if (initial.boundary && initial.boundary.type === "Polygon") {
      try {
        L.geoJSON(initial.boundary, {}).eachLayer(function (layer) {
          drawn.addLayer(layer);
        });
        hasBounds = drawn.getLayers().length > 0;
      } catch (e) { /* invalid initial polygon: start empty */ }
    }
    if (lat !== null && lon !== null) {
      var m = L.marker([lat, lon], { draggable: true });
      m.on("dragend", function () { syncFromLayers(drawn); });
      drawn.addLayer(m);
      hasBounds = true;
    }
    if (hasBounds) {
      try { map.fitBounds(drawn.getBounds().pad(0.2)); } catch (e) { /* keep center */ }
    }


    var isHu = ((document.documentElement && document.documentElement.lang) || "hu").toLowerCase().indexOf("hu") === 0;
    if (isHu && L.drawLocal) {
      L.drawLocal.draw.toolbar.buttons.polygon = "Polygon rajzol\u00e1sa";
      L.drawLocal.draw.toolbar.buttons.marker = "Pont elhelyez\u00e9se";
      L.drawLocal.draw.toolbar.actions.text = "M\u00e9gse";
      L.drawLocal.draw.toolbar.actions.title = "Rajzol\u00e1s m\u00e9gse";
      L.drawLocal.draw.toolbar.finish.text = "Befejez\u00e9s";
      L.drawLocal.draw.toolbar.finish.title = "Rajzol\u00e1s befejez\u00e9se";
      L.drawLocal.draw.handlers.marker.tooltip.start = "Kattints a t\u00e9rk\u00e9pre a pont elhelyez\u00e9s\u00e9hez.";
      L.drawLocal.draw.handlers.polygon.tooltip.start = "Kattints a rajzol\u00e1s megkezd\u00e9s\u00e9hez.";
      L.drawLocal.draw.handlers.polygon.tooltip.cont = "Kattints a folytat\u00e1shoz.";
      L.drawLocal.draw.handlers.polygon.tooltip.end = "Kattints az els\u0151 pontra a befejez\u00e9shez.";
      L.drawLocal.edit.toolbar.buttons.edit = "Szerkeszt\u00e9s";
      L.drawLocal.edit.toolbar.buttons.editDisabled = "Nincs szerkeszthet\u0151 r\u00e9teg";
      L.drawLocal.edit.toolbar.buttons.remove = "T\u00f6rl\u00e9s";
      L.drawLocal.edit.toolbar.buttons.removeDisabled = "Nincs t\u00f6r\u00f6lhet\u0151 r\u00e9teg";
      L.drawLocal.edit.toolbar.actions.save.text = "Ment\u00e9s";
      L.drawLocal.edit.toolbar.actions.cancel.text = "M\u00e9gse";
      L.drawLocal.edit.toolbar.actions.clearAll.text = "\u00d6sszes t\u00f6rl\u00e9se";
    }
    // EN default: Leaflet.draw built-in English strings are used unchanged.

    if (L.Control && L.Control.Draw) {
      map.addControl(new L.Control.Draw({
        draw: {
          polygon: { allowIntersection: false, showArea: true },
          marker: true,
          polyline: false,
          rectangle: false,
          circle: false,
          circlemarker: false
        },
        edit: { featureGroup: drawn, remove: true }
      }));
    }

    map.on(L.Draw ? L.Draw.Event.CREATED : "draw:created", function (e) {
      var layer = e.layer || e.propagatedFrom;
      if (!layer) { return; }
      // Keep at most one polygon and one marker: replace same-kind layer.
      var replaceKind = (layer instanceof L.Marker) ? "marker" : "polygon";
      var drop = [];
      drawn.eachLayer(function (existing) {
        var kind = (existing instanceof L.Marker) ? "marker" : "polygon";
        if (kind === replaceKind) { drop.push(existing); }
      });
      drop.forEach(function (d) { drawn.removeLayer(d); });
      if (layer instanceof L.Marker && layer.dragging) { layer.dragging.enable(); }
      if (layer instanceof L.Marker) {
        layer.on("dragend", function () { syncFromLayers(drawn); });
      }
      drawn.addLayer(layer);
      syncFromLayers(drawn);
    });
    map.on("draw:edited", function () { syncFromLayers(drawn); });
    map.on("draw:deleted", function () { syncFromLayers(drawn); });
    // Manual edits of the fallback fields make the map stale: warn until the
    // geometry is redrawn on the map (which re-syncs and clears the warning).
    ["boundary_geojson", "point_latitude", "point_longitude"].forEach(function (name) {
      var f = field(name);
      if (f) {
        f.addEventListener("input", function () {
          var s = document.getElementById("property-map-stale");
          if (s) { s.hidden = false; }
        });
      }
    });
    map.on("draw:created draw:edited draw:deleted", function () {
      var s = document.getElementById("property-map-stale");
      if (s) { s.hidden = true; }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
