/* Fire map: static site, no build step. Leaflet map of VIIRS hotspots and Himawari-9 crops per cluster. */
(function () {
  'use strict';

  var REGION = [[-6.5, 94.5], [7.5, 119.5]];   // [[south, west], [north, east]]
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var STORE_KEY = 'firemap.run';

  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var pad = function (n) { return String(n).padStart(2, '0'); };
  var store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } }
  };

  function fmt(d, offsetH) {
    var t = new Date(d.getTime() + (offsetH || 0) * 3600e3);
    return pad(t.getUTCDate()) + ' ' + MON[t.getUTCMonth()] + ' ' + pad(t.getUTCHours()) + ':' + pad(t.getUTCMinutes());
  }
  function ageText(min) {
    if (min == null) return 'n/a';
    min = Math.round(min);
    return min < 60 ? min + ' min' : Math.floor(min / 60) + ' h ' + pad(min % 60) + ' min';
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function getJSON(url) {
    return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + 't=' + Date.now(), { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(url + ': HTTP ' + r.status);
      return r.json();
    });
  }
  function bounds(b) { return [[b[1], b[0]], [b[3], b[2]]]; }   // [w,s,e,n] -> [[s,w],[n,e]]

  // ---------------------------------------------------------------- map
  var map = L.map('map', { zoomControl: true, worldCopyJump: false, preferCanvas: true });
  map.fitBounds(REGION);
  map.createPane('imgs').style.zIndex = 350;
  map.createPane('hot').style.zIndex = 450;
  var hotRenderer = L.canvas({ pane: 'hot', padding: 0.3 });

  var esri = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { attribution: 'Tiles &copy; Esri', maxZoom: 13 });
  var osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    { attribution: '&copy; OpenStreetMap contributors', maxZoom: 18 });
  esri.addTo(map);
  var bases = { 'Esri World Imagery': esri, 'OpenStreetMap': osm };

  var countries = null;   // lazy: world country borders, optional layer
  var countryLayer = L.layerGroup();
  countryLayer.on('add', function () {
    if (countries) return;
    countries = true;
    getJSON('https://raw.githubusercontent.com/python-visualization/folium/master/examples/data/world-countries.json')
      .then(function (g) {
        L.geoJSON(g, { pane: 'hot', interactive: false, style: { color: '#fff', weight: 1, fill: false, opacity: .7 } }).addTo(countryLayer);
      }).catch(function () { countries = null; });
  });

  // opacity slider for the selected overlay
  var selected = null;
  var opInput;
  var OpControl = L.Control.extend({
    options: { position: 'bottomleft' },
    onAdd: function () {
      var box = el('div', 'op');
      var lab = el('label', null, 'Overlay opacity');
      lab.id = 'opLabel';
      opInput = document.createElement('input');
      opInput.type = 'range'; opInput.min = 0; opInput.max = 1; opInput.step = 0.05; opInput.value = 1;
      opInput.setAttribute('aria-labelledby', 'opLabel');
      opInput.addEventListener('input', function () {
        if (selected && selected.setOpacity) selected.setOpacity(parseFloat(opInput.value));
      });
      box.appendChild(lab); box.appendChild(opInput);
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
      return box;
    }
  });
  new OpControl().addTo(map);

  function select(layer, id) {
    selected = layer;
    var lab = $('#opLabel');
    if (lab) lab.textContent = layer ? 'Overlay opacity: ' + id : 'Overlay opacity';
    if (layer && opInput) opInput.value = layer.options.opacity != null ? layer.options.opacity : 1;
    document.querySelectorAll('.card').forEach(function (c) { c.classList.toggle('sel', c.dataset.id === id); });
  }

  // ---------------------------------------------------------------- run state
  var runs = [];
  var current = { layers: [], ctl: null, imgs: {} };

  function clearRun() {
    current.layers.forEach(function (l) { map.removeLayer(l); });
    Object.keys(current.imgs).forEach(function (k) { map.removeLayer(current.imgs[k]); });
    if (current.ctl) current.ctl.remove();
    current = { layers: [], ctl: null, imgs: {} };
    select(null, null);
  }

  function frpStyle(frp) {
    if (frp >= 50) return { r: 5, c: '#ff4d3d' };
    if (frp >= 10) return { r: 3.5, c: '#ff9a2e' };
    return { r: 2.5, c: '#ffd84a' };
  }

  function hotspotLayer(gj) {
    return L.geoJSON(gj, {
      pointToLayer: function (f, ll) {
        var s = frpStyle(f.properties.frp);
        return L.circleMarker(ll, { renderer: hotRenderer, pane: 'hot', radius: s.r, color: '#000', weight: 1,
          fillColor: s.c, fillOpacity: 0.85 });
      },
      onEachFeature: function (f, layer) {
        var p = f.properties, t = new Date(p.time);
        var lines = ['<b>' + p.satellite + '</b>', 'FRP: <b>' + p.frp.toFixed(1) + ' MW</b>',
          fmt(t) + ' UTC / ' + fmt(t, 8) + ' SGT'];
        if (p.confidence) lines.push('Confidence: ' + p.confidence);
        if (p.cluster) lines.push('Cluster ' + p.cluster);
        layer.bindPopup(lines.join('<br>'));
      }
    });
  }

  function setStrip(meta, cj) {
    var strip = $('#strip');
    strip.textContent = '';
    function chip(k, v, cls) {
      var c = el('span', 'chip' + (cls ? ' ' + cls : ''));
      c.appendChild(el('b', null, k));
      c.appendChild(document.createTextNode(v));
      strip.appendChild(c);
    }
    if (meta.scan_iso) {
      var s = new Date(meta.scan_iso);
      chip('Scan', fmt(s) + ' UTC / ' + fmt(s, 8) + ' SGT');
    } else {
      chip('Run', fmt(new Date(meta.generated_utc)) + ' UTC', 'warn');
    }
    if (meta.hotspot_age_min != null) {
      chip('Newest hotspot', ageText(meta.hotspot_age_min) + ' old at run');
    } else if (meta.viirs_newest_scan_utc) {
      chip('Newest VIIRS data', fmt(new Date(meta.viirs_newest_scan_utc)) + ' UTC');
    }
    if (meta.schema === 2) {
      if (meta.n_hotspots) {
        chip('Flagged', meta.n_severe + ' severe \u00b7 ' + meta.n_watch + ' watch',
          meta.n_severe > 0 ? 'sev' : meta.n_watch > 0 ? 'watch' : '');
        if (meta.n_escalating > 0) chip('Getting worse', String(meta.n_escalating), 'sev');
        chip('Fires', meta.n_hotspots + ' \u00b7 ' + Math.round(meta.total_frp).toLocaleString('en-US') + ' MW');
      } else {
        chip('Fires', '0');
      }
    } else {
      chip('Clusters', String(meta.n_clusters) + (meta.n_hotspots ? ' (' + meta.n_hotspots + ' hotspots)' : ''));
    }
    if (meta.mode) chip('Mode', meta.mode === 'day' ? 'Day Fire' : meta.mode === 'night' ? 'Night 3.9 µm' : 'Day + night');
    var w = meta.winds || 'off';
    chip('Winds', w === 'skipped' ? 'skipped' : w === 'off' ? 'off' : w.replace(/^ECMWF IFS /, ''), w === 'skipped' ? 'warn' : '');

    var banner = $('#banner');
    var msg = null, info = false;
    if (meta.status === 'no_hotspots') {
      msg = 'No VIIRS hotspots in the region in the selected window. VIIRS passes over Indonesia mostly around 05–08 and 17–20 UTC, so this is normal between passes.'; info = true;
    } else if (meta.status === 'no_flagged') {
      msg = 'Fires were detected, but none is dense or intense enough to flag as severe or watch, so no images were made. The hotspots are still on the map.'; info = true;
    } else if (meta.status === 'no_imagery') {
      msg = 'Hotspots were found, but no complete Himawari-9 scan was available, so there are no satellite images in this run.';
    } else if (w === 'skipped') {
      msg = 'winds: skipped. The ECMWF wind data could not be used, so the images have no streamlines.'; info = true;
    }
    banner.hidden = !msg;
    banner.className = 'banner' + (info ? ' info' : '');
    banner.textContent = msg || '';
  }

  function badge(text, cls) { return el('span', 'tag' + (cls ? ' ' + cls : ''), text); }

  function setCards(base, meta, cj) {
    var wrap = $('#cards');
    wrap.textContent = '';
    cj.clusters.forEach(function (c) {
      var card = el('div', 'card');
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      card.dataset.id = c.id;
      card.setAttribute('aria-label', 'Zoom to ' + c.id);
      var th;
      if (c.thumb) {
        th = el('a', 'th');
        th.href = base + c.png; th.target = '_blank'; th.rel = 'noopener';
        th.title = 'Open full-size image';
        var img = document.createElement('img');
        img.src = base + c.thumb; img.alt = c.id + ' satellite crop'; img.loading = 'lazy';
        th.appendChild(img);
        th.addEventListener('click', function (e) { e.stopPropagation(); });
      } else {
        th = el('div', 'th none', c.image_error ? 'image failed' : 'no image');
      }
      var m = el('div', 'meta');
      var t = el('div', 't', c.id);
      if (c.tier) t.appendChild(badge(c.tier === 'severe' ? 'SEVERE' : 'WATCH', c.tier));
      if (c.trend && c.trend.state === 'escalating') t.appendChild(badge('\u25b2 getting worse', 'worse'));
      else if (c.trend && c.trend.state === 'new') t.appendChild(badge('newly flagged', 'worse'));
      else if (c.trend && c.trend.state === 'easing') t.appendChild(badge('easing'));
      if (c.mode) t.appendChild(badge(c.mode === 'day' ? 'Day Fire' : 'Night 3.9 µm'));
      if (c.obscured) t.appendChild(badge('obscured', 'warn'));
      m.appendChild(t);
      if (c.reasons && c.reasons.length) m.appendChild(el('div', 's why ' + c.tier, c.reasons.join(' \u00b7 ')));
      m.appendChild(el('div', 's', c.count + ' fires \u00b7 ' + Math.round(c.summed_frp).toLocaleString('en-US') + ' MW'));
      if (c.trend && c.trend.prev_count != null) {
        m.appendChild(el('div', 's', 'Was ' + c.trend.prev_count + ' fires \u00b7 ' + Math.round(c.trend.prev_frp) + ' MW' +
          (c.trend.prev_hotspot_utc ? ' at ' + fmt(new Date(c.trend.prev_hotspot_utc)) + ' UTC' : '')));
      }
      m.appendChild(el('div', 's', c.centroid.lat.toFixed(2) + ', ' + c.centroid.lon.toFixed(2) +
        (c.newest_utc ? ' · ' + fmt(new Date(c.newest_utc)) + ' UTC' : '')));
      if (c.confidence) {
        var tot = (c.confidence.low + c.confidence.nominal + c.confidence.high) || 1;
        var mix = el('div', 'mix');
        mix.title = 'Confidence: ' + c.confidence.low + ' low, ' + c.confidence.nominal + ' nominal, ' + c.confidence.high + ' high';
        ['low', 'nominal', 'high'].forEach(function (k) {
          var seg = el('i', k); seg.style.width = (100 * c.confidence[k] / tot) + '%'; mix.appendChild(seg);
        });
        m.appendChild(mix);
      }
      card.appendChild(th); card.appendChild(m);
      var go = function () { focusCluster(c); };
      card.addEventListener('click', go);
      card.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      wrap.appendChild(card);
    });
    if (cj.n_overflow > 0) {
      var o = el('div', 'over');
      var noun = cj.schema === 2 ? 'flagged area' : 'cluster';
      o.appendChild(document.createTextNode(cj.n_overflow + ' more ' + noun + (cj.n_overflow === 1 ? '' : 's') +
        ' not individually imaged' + (cj.overview ? '; see the overview layer.' : '.')));
      if (cj.overview) {
        var b = el('button', null, 'Show overview');
        b.type = 'button';
        b.addEventListener('click', function () { showOverview(); });
        o.appendChild(b);
      }
      if (cj.overflow && cj.overflow.length && cj.overflow[0].tier) {
        var det = document.createElement('details');
        det.appendChild(el('summary', null, 'List them'));
        cj.overflow.forEach(function (c) {
          var line = el('div', 'ovl ' + c.tier);
          line.textContent = c.id + ' \u00b7 ' + c.tier + ' \u00b7 ' + c.count + ' fires \u00b7 ' +
            Math.round(c.summed_frp) + ' MW \u00b7 ' + c.centroid.lat.toFixed(2) + ', ' + c.centroid.lon.toFixed(2) +
            (c.trend && c.trend.state === 'escalating' ? ' \u00b7 getting worse' : '');
          det.appendChild(line);
        });
        o.appendChild(det);
      }
      wrap.appendChild(o);
    }
    if (!cj.clusters.length && !cj.n_overflow) {
      wrap.appendChild(el('div', 'over', cj.schema === 2 ? 'Nothing flagged in this run.' : 'No clusters in this run.'));
    }
    var rules = $('#rules');
    if (cj.rules) {
      rules.textContent = 'Flagging rules: dense = ' + cj.rules.dense_min + '+ fires within ' + cj.rules.dense_km +
        ' km; intense = ' + cj.rules.intense_sum_mw + '+ MW in the cluster or a ' + cj.rules.intense_pixel_mw +
        '+ MW pixel. Severe = both, watch = one. Repeat sightings of one fire by other satellites or passes are counted once.' +
        (cj.n_ignored ? ' ' + cj.n_ignored + ' smaller clusters are not shown.' : '');
      rules.hidden = false;
    } else {
      rules.hidden = true;
    }
  }

  function focusCluster(c) {
    var layer = current.imgs[c.id];
    if (layer && !map.hasLayer(layer)) layer.addTo(map);
    map.fitBounds(bounds(c.bbox), { padding: [8, 8] });
    if (layer) select(layer, c.id);
    $('#map').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function showOverview() {
    var layer = current.imgs.overview;
    if (!layer) return;
    if (!map.hasLayer(layer)) layer.addTo(map);
    map.fitBounds(layer.getBounds());
    select(layer, 'overview');
    $('#map').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  var TIER_COLOUR = { severe: '#ff4d3d', watch: '#ffb02e' };
  function outline(c) {
    var cb = c.cluster_bbox, pad = 0.03;
    var rect = L.rectangle([[cb[1] - pad, cb[0] - pad], [cb[3] + pad, cb[2] + pad]],
      { pane: 'hot', renderer: hotRenderer, color: TIER_COLOUR[c.tier] || '#ff8a3d', weight: 1.5, fill: false, dashArray: '4 3' });
    rect.bindTooltip(c.id, { permanent: true, direction: 'top', className: 'cl-label', offset: [0, -2] });
    return rect;
  }

  function loadRun(id) {
    var meta = runs.filter(function (r) { return r.run_id === id; })[0];
    if (!meta) return Promise.resolve();
    var base = 'runs/' + id + '/';
    return Promise.all([getJSON(base + 'clusters.json'), getJSON(base + 'hotspots.geojson')]).then(function (res) {
      var cj = res[0], gj = res[1];
      clearRun();
      setStrip(meta, cj);
      setCards(base, meta, cj);

      var overlays = {};
      var outlines = L.layerGroup();
      var all = [];
      if (cj.overview) {
        var ov = L.imageOverlay(base + cj.overview.png, bounds(cj.overview.bbox), { pane: 'imgs', opacity: 1, interactive: false });
        current.imgs.overview = ov;
        overlays['Overview (all clusters)'] = ov;       // off by default
        ov.on('add', function () { select(ov, 'overview'); });
      }
      cj.clusters.forEach(function (c) {
        if (c.png) {
          var im = L.imageOverlay(base + c.png, bounds(c.bbox), { pane: 'imgs', opacity: 1, interactive: false });
          current.imgs[c.id] = im;
          overlays[c.id + ' (' + c.count + ' hs)'] = im;
          im.addTo(map); current.layers.push(im);
          im.on('add', function () { select(im, c.id); });
          all.push(im.getBounds());
        }
        var rect = outline(c);
        outlines.addLayer(rect);
        all.push(rect.getBounds());
      });
      (cj.overflow || []).forEach(function (c) {
        if (c.cluster_bbox) outlines.addLayer(outline(c));
      });
      overlays['Cluster outlines'] = outlines;
      var hot = hotspotLayer(gj);
      overlays['Hotspots (' + gj.features.length + ')'] = hot;
      overlays['Country borders'] = countryLayer;
      hot.addTo(map); outlines.addTo(map);
      current.layers.push(hot, outlines);
      current.ctl = L.control.layers(bases, overlays, { collapsed: window.innerWidth < 700 }).addTo(map);

      var first = cj.clusters.filter(function (c) { return c.png; })[0];
      if (first) select(current.imgs[first.id], first.id);
      if (all.length) {
        var b = all[0];
        all.forEach(function (x) { b = b.extend(x); });
        map.fitBounds(b, { padding: [16, 16], maxZoom: 9 });
      } else {
        map.fitBounds(REGION);
      }
    });
  }

  // ---------------------------------------------------------------- boot
  function runLabel(r) {
    var t = r.scan_iso ? new Date(r.scan_iso) : new Date(r.generated_utc);
    var tail = r.status === 'no_hotspots' ? 'no hotspots' : r.status === 'no_flagged' ? 'nothing flagged'
      : r.status === 'no_imagery' ? 'no imagery'
      : r.schema === 2 ? r.n_severe + ' severe \u00b7 ' + r.n_watch + ' watch'
      : r.n_imaged + '/' + r.n_clusters + ' imaged';
    return fmt(t, 8) + ' SGT · ' + tail;
  }

  function setHeaderHeight() {
    var h = $('#bar').getBoundingClientRect().height;
    document.documentElement.style.setProperty('--hdr', Math.ceil(h) + 'px');
    map.invalidateSize();
  }
  if (window.ResizeObserver) new ResizeObserver(setHeaderHeight).observe($('#bar'));
  window.addEventListener('resize', setHeaderHeight);

  getJSON('runs.json').then(function (list) {
    runs = list;
    var sel = $('#runSel');
    if (!runs.length) throw new Error('empty');
    runs.forEach(function (r) {
      var o = el('option', null, runLabel(r));
      o.value = r.run_id;
      sel.appendChild(o);
    });
    var saved = store.get(STORE_KEY);
    var start = runs.some(function (r) { return r.run_id === saved; }) ? saved : runs[0].run_id;
    sel.value = start;
    sel.addEventListener('change', function () {
      store.set(STORE_KEY, sel.value);
      loadRun(sel.value).catch(showError);
    });
    setHeaderHeight();
    return loadRun(start);
  }).catch(showError);

  function showError(e) {
    var b = $('#banner');
    b.hidden = false; b.className = 'banner';
    b.textContent = 'No run data yet (' + (e && e.message ? e.message : e) + '). Run the "Fire map" workflow once to publish the first run.';
    setHeaderHeight();
  }
})();
