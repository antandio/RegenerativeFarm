/* Property overview map: local Leaflet only, OSM tiles.
 * Renders role-scoped properties from json_script data.
 * Popups use textContent (no innerHTML) so names are XSS-safe.
 */
window.PropertyOverview = (function () {
  "use strict";

  function popupEl(name, kind) {
    var div = document.createElement("div");
    var strong = document.createElement("strong");
    strong.textContent = name;
    div.appendChild(strong);
    if (kind) {
      var span = document.createElement("span");
      span.textContent = " — " + kind;
      div.appendChild(span);
    }
    return div;
  }

  function init(mapId, dataId) {
    var mapEl = document.getElementById(mapId || "property-overview-map");
    var dataEl = document.getElementById(dataId || "property-overview-data");
    if (!mapEl || !dataEl || typeof L === "undefined") { return; }
    var items = [];
    try {
      items = JSON.parse(dataEl.textContent) || [];
    } catch (e) { items = []; }

    // Empty-overview center comes from the farm location (data-center);
    // hardcoded coordinates are only the last-resort fallback.
    var defaultCenter = [46.25, 20.14];
    try {
      var dc = (mapEl.getAttribute("data-center") || "").split(",");
      var dlat = parseFloat(dc[0]);
      var dlon = parseFloat(dc[1]);
      if (isFinite(dlat) && isFinite(dlon)) { defaultCenter = [dlat, dlon]; }
    } catch (e) { /* keep fallback */ }
    var map = L.map(mapId || "property-overview-map").setView(defaultCenter, 8);
    var tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      // Origin only: OSM tile policy requires a Referer; never the full page URL.
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    });
    // Visible guidance on tile failure (e.g. OSM 403). The warning is sticky:
    // a later tileload must NOT silently clear it. The list below stays complete.
    tiles.on("tileerror", function () {
      var s = document.getElementById("property-overview-tiles-status");
      if (s) {
        var hu = ((document.documentElement && document.documentElement.lang) || "hu").toLowerCase().indexOf("hu") === 0;
        s.textContent = hu
          ? "A térkép csempéi nem tölthetők be (pl. OSM 403 tiltás). Az alatti lista teljes; a térkép csak segítség."
          : "Map tiles failed to load (e.g. OSM 403). The list below is complete; the map is only a helper.";
        s.hidden = false;
      }
    });
    tiles.addTo(map);

    var bounds = [];
    items.forEach(function (p) {
      var label = popupEl(String(p.name || ""), String(p.kind || ""));
      if (p.polygon && p.polygon.type === "Polygon") {
        try {
          var layer = L.geoJSON(p.polygon, {});
          layer.bindPopup(label);
          layer.addTo(map);
          try { bounds.push(layer.getBounds()); } catch (e) { /* skip */ }
        } catch (e) { /* skip invalid polygon */ }
      }
      if (p.lat !== null && p.lat !== undefined && p.lon !== null && p.lon !== undefined) {
        var lat = parseFloat(p.lat);
        var lon = parseFloat(p.lon);
        if (isFinite(lat) && isFinite(lon)) {
          var marker = L.marker([lat, lon]);
          marker.bindPopup(label);
          marker.addTo(map);
          bounds.push(L.latLng(lat, lon));
        }
      }
    });

    if (bounds.length) {
      try { map.fitBounds(L.latLngBounds(bounds).pad(0.2)); } catch (e) { /* keep default */ }
    }
  }

  return { init: init };
})();
