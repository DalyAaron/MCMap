'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const DEFAULTS = {
  edition: 'java',
  mode: 'geo-terrain',
  gamemode: 'creative',
  world_time: '6000',
  scale: 1,
  height_multiplier: 1,
  interior: false,
  caves: false,
  props_3d: true,
  overture: true,
  map_preview: true,
  world_type: 'void',
  signage: 'basic',
  install: true,
};
const SETTINGS_KEY = 'mcmap.settings.v1';
const VIEW_KEY = 'mcmap.view.v1';
const AREA_KEY = 'mcmap.area.v1';
const NOMINATIM = 'https://nominatim.openstreetmap.org';

const MODE_LABELS = { 'geo-terrain': 'Objects + terrain', 'geo-only': 'Objects only', 'terrain-only': 'Terrain only' };
const STATUS_LABELS = { queued: 'Queued', running: 'Building', done: 'Ready', failed: 'Failed', cancelled: 'Cancelled' };

const state = {
  bounds: null, // L.LatLngBounds of the selected area
  spawn: null, // L.LatLng
  mode: 'idle', // idle | draw | spawn
  presetSize: null,
  status: null, // /api/status
  offline: false,
  jobs: [],
  openLogs: new Set(),
  submitting: false,
};

const settings = loadJSON(SETTINGS_KEY, {});
for (const [k, v] of Object.entries(DEFAULTS)) if (!(k in settings)) settings[k] = v;

// ---------------------------------------------------------------- helpers

function loadJSON(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

function saveJSON(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} };
  if (method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body ?? {});
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function areaStats(b) {
  const mid = ((b.getSouth() + b.getNorth()) / 2) * (Math.PI / 180);
  const wKm = (b.getEast() - b.getWest()) * 111.32 * Math.cos(mid);
  const hKm = (b.getNorth() - b.getSouth()) * 110.574;
  return { wKm, hKm, km2: wKm * hKm };
}

function rectAround(center, wKm, hKm) {
  const dLat = hKm / 2 / 110.574;
  const dLng = wKm / 2 / (111.32 * Math.cos((center.lat * Math.PI) / 180));
  return L.latLngBounds([center.lat - dLat, center.lng - dLng], [center.lat + dLat, center.lng + dLng]);
}

function fmtDistance(km) {
  const m = Math.round(km * 1000);
  return m < 1000 ? `${m} m` : `${km.toFixed(km < 10 ? 2 : 1)} km`;
}

function fmtArea(km2) {
  return km2 < 0.1 ? `${Math.round(km2 * 1e6).toLocaleString()} m²` : `${km2.toFixed(km2 < 10 ? 2 : 1)} km²`;
}

function fmtBytes(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
}

// ---------------------------------------------------------------- map

const savedView = loadJSON(VIEW_KEY, null);
const map = L.map('map', { worldCopyJump: true }).setView(
  savedView?.center ?? [40.758, -73.9855],
  savedView?.zoom ?? 15,
);
map.on('moveend', () => saveJSON(VIEW_KEY, { center: map.getCenter(), zoom: map.getZoom() }));

const streets = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const satellite = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Imagery &copy; Esri' },
);
L.control.layers({ Map: streets, Satellite: satellite }, null, { position: 'bottomright' }).addTo(map);

const AREA_STYLE = { color: '#74d65d', weight: 2, fillColor: '#5fbf4a', fillOpacity: 0.12, interactive: false };

// The selected rectangle, with four corner handles and a centre handle for moving it.
const area = {
  rect: null,
  handles: [],
  mover: null,
  anchor: null,

  set(bounds, { fit = false } = {}) {
    state.bounds = L.latLngBounds(bounds.getSouthWest(), bounds.getNorthEast());
    if (!this.rect) {
      this.rect = L.rectangle(state.bounds, AREA_STYLE).addTo(map);
      this.createHandles();
    } else {
      this.rect.setBounds(state.bounds);
    }
    this.placeHandles();
    if (fit) map.fitBounds(state.bounds, { padding: [70, 70] });
    onAreaChanged();
  },

  createHandles() {
    const icon = L.divIcon({ className: 'handle', iconSize: [12, 12] });
    for (let i = 0; i < 4; i++) {
      const m = L.marker([0, 0], { icon, draggable: true, keyboard: false, zIndexOffset: 1000 }).addTo(map);
      // Corners are SW, NW, NE, SE: the opposite corner stays put while dragging.
      m.on('dragstart', () => (this.anchor = this.corners()[(i + 2) % 4]));
      m.on('drag', () => this.dragCorner(i));
      m.on('dragend', () => this.commit());
      this.handles.push(m);
    }
    this.mover = L.marker([0, 0], {
      icon: L.divIcon({ className: 'handle mover', iconSize: [14, 14] }),
      draggable: true,
      keyboard: false,
      zIndexOffset: 1000,
      title: 'Drag to move the area',
    }).addTo(map);
    this.mover.on('dragstart', () => {
      this.moveFrom = this.mover.getLatLng();
      this.boundsFrom = state.bounds;
    });
    this.mover.on('drag', () => {
      const now = this.mover.getLatLng();
      const dLat = now.lat - this.moveFrom.lat;
      const dLng = now.lng - this.moveFrom.lng;
      const sw = this.boundsFrom.getSouthWest();
      const ne = this.boundsFrom.getNorthEast();
      this.preview(L.latLngBounds([sw.lat + dLat, sw.lng + dLng], [ne.lat + dLat, ne.lng + dLng]), -1, true);
    });
    this.mover.on('dragend', () => this.commit());
  },

  corners() {
    const b = state.bounds;
    return [b.getSouthWest(), b.getNorthWest(), b.getNorthEast(), b.getSouthEast()];
  },

  dragCorner(i) {
    setActivePreset(null);
    this.preview(L.latLngBounds(this.anchor, this.handles[i].getLatLng()), i, false);
  },

  // Live update while dragging; skip the handle that is under the cursor.
  preview(bounds, skipCorner, skipMover) {
    state.bounds = bounds;
    this.rect.setBounds(bounds);
    this.corners().forEach((c, j) => j !== skipCorner && this.handles[j].setLatLng(c));
    if (!skipMover) this.mover.setLatLng(bounds.getCenter());
    updateAreaInfo();
  },

  placeHandles() {
    this.corners().forEach((c, i) => this.handles[i].setLatLng(c));
    this.mover.setLatLng(state.bounds.getCenter());
  },

  commit() {
    this.placeHandles();
    onAreaChanged();
  },
};

function squareAround(center, sizeKm) {
  return rectAround(center, sizeKm, sizeKm);
}

function applyPreset(sizeKm, center = map.getCenter()) {
  setMode('idle');
  setActivePreset(sizeKm);
  area.set(squareAround(center, sizeKm), { fit: true });
}

function setActivePreset(sizeKm) {
  state.presetSize = sizeKm;
  $$('#presets button[data-size]').forEach((b) => b.classList.toggle('active', Number(b.dataset.size) === sizeKm));
}

// ---------------------------------------------------------------- draw & spawn modes

function setMode(mode) {
  state.mode = mode;
  const el = map.getContainer();
  el.classList.toggle('drawing', mode === 'draw');
  el.classList.toggle('picking', mode === 'spawn');
  $('#draw-btn').classList.toggle('active', mode === 'draw');
  $('#spawn-btn').classList.toggle('active', mode === 'spawn');
  if (mode === 'draw') map.dragging.disable();
  else map.dragging.enable();
  showHint(
    mode === 'draw'
      ? 'Click and drag to draw your area · Esc to cancel'
      : mode === 'spawn'
        ? 'Click inside the area to place the spawn point · Esc to cancel'
        : null,
  );
}

let hintTimer;
function showHint(text, { flash = false } = {}) {
  const hint = $('#map-hint');
  clearTimeout(hintTimer);
  hint.hidden = !text;
  hint.textContent = text || '';
  if (flash) hintTimer = setTimeout(() => setMode(state.mode), 2200);
}

let drawStart = null;
let drawEnd = null;
let drawRect = null;

map.on('mousedown', (e) => {
  if (state.mode !== 'draw') return;
  drawStart = drawEnd = e.latlng;
  drawRect = L.rectangle(L.latLngBounds(e.latlng, e.latlng), { ...AREA_STYLE, dashArray: '6 4' }).addTo(map);
});

map.on('mousemove', (e) => {
  if (!drawStart) return;
  drawEnd = e.latlng;
  drawRect.setBounds(L.latLngBounds(drawStart, drawEnd));
});

// Listen on the document so releasing the mouse outside the map still finishes the box.
document.addEventListener('mouseup', () => {
  if (!drawStart) return;
  const bounds = L.latLngBounds(drawStart, drawEnd);
  drawRect.remove();
  drawRect = drawStart = drawEnd = null;
  const a = map.latLngToContainerPoint(bounds.getSouthWest());
  const b = map.latLngToContainerPoint(bounds.getNorthEast());
  if (Math.abs(a.x - b.x) < 8 || Math.abs(a.y - b.y) < 8) return; // a click, not a drag
  setMode('idle');
  setActivePreset(null);
  area.set(bounds);
});

map.on('click', (e) => {
  if (state.mode !== 'spawn') return;
  if (!state.bounds?.contains(e.latlng)) {
    showHint('The spawn point has to be inside the area', { flash: true });
    return;
  }
  setSpawn(e.latlng);
  setMode('idle');
});

const spawnIcon = L.divIcon({
  className: 'spawn-pin',
  html: '<svg viewBox="0 0 24 24"><path d="M12 1C7.6 1 4 4.6 4 9c0 5.6 8 14 8 14s8-8.4 8-14c0-4.4-3.6-8-8-8Z" fill="#f2c94c" stroke="#2b2306" stroke-width="1.5"/><circle cx="12" cy="9" r="3" fill="#2b2306"/></svg>',
  iconSize: [26, 26],
  iconAnchor: [13, 26],
});
let spawnMarker = null;

function setSpawn(latlng) {
  state.spawn = latlng;
  if (!latlng) {
    spawnMarker?.remove();
    spawnMarker = null;
  } else if (!spawnMarker) {
    spawnMarker = L.marker(latlng, { icon: spawnIcon, draggable: true, zIndexOffset: 2000, title: 'Spawn point (drag to move)' }).addTo(map);
    spawnMarker.on('dragend', () => {
      const p = spawnMarker.getLatLng();
      if (state.bounds?.contains(p)) state.spawn = p;
      else {
        spawnMarker.setLatLng(state.spawn);
        showHint('The spawn point has to be inside the area', { flash: true });
      }
      updateSpawnInfo();
      persistArea();
    });
  } else {
    spawnMarker.setLatLng(latlng);
  }
  updateSpawnInfo();
  persistArea();
}

function updateSpawnInfo() {
  const info = $('#spawn-info');
  $('#spawn-clear').hidden = !state.spawn;
  if (state.spawn) {
    info.innerHTML = `<strong>${state.spawn.lat.toFixed(5)}, ${state.spawn.lng.toFixed(5)}</strong> · drag the pin to adjust`;
  } else {
    info.textContent = state.bounds ? 'Not set: Arnis picks a spot in the area.' : 'Where players appear when they join the world.';
  }
}

// ---------------------------------------------------------------- area info, naming

function scaledArea() {
  return state.bounds ? areaStats(state.bounds).km2 * settings.scale ** 2 : 0;
}

function areaTooLarge() {
  const limit = state.status?.limits?.max_area_km2 ?? 400;
  return scaledArea() > limit;
}

function updateAreaInfo() {
  const info = $('#area-info');
  if (!state.bounds) {
    info.innerHTML = 'Pick a size to place a square at the map center, or draw a rectangle.';
    info.classList.add('muted');
    return;
  }
  const { wKm, hKm, km2 } = areaStats(state.bounds);
  const s = Number(settings.scale);
  const blocksW = Math.round(wKm * 1000 * s).toLocaleString();
  const blocksH = Math.round(hKm * 1000 * s).toLocaleString();
  const scaled = km2 * s * s;
  const limit = state.status?.limits?.max_area_km2 ?? 400;
  let note = '';
  if (scaled > limit) {
    note = `<span class="bad">Too large for scale ${s.toFixed(2)} (limit ${limit} km²). Shrink the area or lower the scale under Advanced.</span>`;
  } else if (scaled > 100) {
    note = '<span class="warn">Very large: expect a long generation, many GB of memory and a big world file.</span>';
  } else if (scaled > 16) {
    note = '<span class="warn">Large area: generation will take a while.</span>';
  }
  info.classList.remove('muted');
  info.innerHTML = `<strong>${fmtDistance(wKm)} × ${fmtDistance(hKm)}</strong> · ${fmtArea(km2)}<br>
    <span class="muted">≈ ${blocksW} × ${blocksH} blocks${s === 1 ? ' (real size)' : ''} · drag the handles to adjust</span>${note}`;
}

function onAreaChanged() {
  if (state.spawn && (!state.bounds || !state.bounds.contains(state.spawn))) setSpawn(null);
  $('#spawn-btn').disabled = !state.bounds;
  updateAreaInfo();
  updateSpawnInfo();
  updateGenerateButton();
  persistArea();
  if (state.bounds) suggestName();
}

function persistArea() {
  const b = state.bounds;
  saveJSON(AREA_KEY, {
    bounds: b ? [[b.getSouth(), b.getWest()], [b.getNorth(), b.getEast()]] : null,
    spawn: state.spawn ? [state.spawn.lat, state.spawn.lng] : null,
    presetSize: state.presetSize,
  });
}

// Suggest a world name from the area's location (used as the name field placeholder).
let nameRequest = 0;
const suggestName = debounce(async () => {
  if (!state.bounds) return;
  const id = ++nameRequest;
  const c = state.bounds.getCenter().wrap();
  const km2 = areaStats(state.bounds).km2;
  const zoom = km2 < 3 ? 16 : km2 < 40 ? 13 : 10;
  try {
    const url = `${NOMINATIM}/reverse?format=jsonv2&zoom=${zoom}&lat=${c.lat}&lon=${c.lng}&accept-language=${encodeURIComponent(navigator.language)}`;
    const data = await (await fetch(url)).json();
    if (id !== nameRequest) return;
    const a = data.address || {};
    // Suburb first: "neighbourhood" is often an admin unit like "Manhattan Community Board 5".
    const local = a.suburb || a.quarter || a.neighbourhood || a.borough || a.village || a.hamlet || a.city_district;
    const city = a.city || a.town || a.village || a.municipality || a.county || a.state;
    const landmark = zoom >= 16 && data.category !== 'highway' ? data.name : null;
    const parts = (zoom >= 13 ? [landmark || local, city] : [city, a.country]).filter((v, i, arr) => v && arr.indexOf(v) === i);
    const name = parts.join(', ') || data.name || (data.display_name || '').split(',')[0];
    if (name) $('#name').placeholder = name;
  } catch {
    /* naming is a nicety; ignore network errors */
  }
}, 700);

// ---------------------------------------------------------------- search

const searchInput = $('#search-input');
const searchResults = $('#search-results');

$('#search').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) return;
  searchResults.hidden = false;
  searchResults.innerHTML = '<li class="empty">Searching…</li>';
  try {
    const url = `${NOMINATIM}/search?format=jsonv2&limit=6&q=${encodeURIComponent(q)}&accept-language=${encodeURIComponent(navigator.language)}`;
    const items = await (await fetch(url)).json();
    searchResults.innerHTML = '';
    if (!items.length) searchResults.innerHTML = '<li class="empty">No places found</li>';
    for (const item of items) {
      const [title, ...rest] = item.display_name.split(', ');
      const li = document.createElement('li');
      li.innerHTML = `${escapeHtml(title)}<small>${escapeHtml(rest.join(', '))}</small>`;
      li.addEventListener('click', () => pickPlace(item));
      searchResults.appendChild(li);
    }
  } catch {
    searchResults.innerHTML = '<li class="empty">Search failed. Check your internet connection.</li>';
  }
});

function pickPlace(item) {
  searchResults.hidden = true;
  const center = L.latLng(Number(item.lat), Number(item.lon));
  setMode('idle');
  if (state.bounds) {
    // Keep the current area size and move it to the place.
    const { wKm, hKm } = areaStats(state.bounds);
    area.set(rectAround(center, wKm, hKm), { fit: true });
  } else {
    applyPreset(1, center);
  }
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('#search')) searchResults.hidden = true;
});

// ---------------------------------------------------------------- settings form

function initForm() {
  $$('.seg').forEach((seg) => {
    const name = seg.dataset.name;
    const sync = () => $$('button', seg).forEach((b) => b.classList.toggle('on', b.dataset.value === String(settings[name])));
    seg.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      settings[name] = b.dataset.value;
      sync();
      saveJSON(SETTINGS_KEY, settings);
      onSettingsChanged();
    });
    sync();
  });

  for (const id of ['interior', 'caves', 'props_3d', 'overture', 'map_preview', 'install']) {
    const el = $(`#${id}`);
    el.checked = Boolean(settings[id]);
    el.addEventListener('change', () => {
      settings[id] = el.checked;
      saveJSON(SETTINGS_KEY, settings);
    });
  }

  for (const id of ['world_type', 'signage']) {
    const el = $(`#${id}`);
    el.value = settings[id];
    el.addEventListener('change', () => {
      settings[id] = el.value;
      saveJSON(SETTINGS_KEY, settings);
    });
  }

  const sliders = [
    ['scale', '#scale-out', (v) => `${v.toFixed(2)}${v === 1 ? ' · real size' : ''}`],
    ['height_multiplier', '#height-out', (v) => `${v}×`],
  ];
  for (const [id, out, fmt] of sliders) {
    const el = $(`#${id}`);
    el.value = settings[id];
    const show = () => ($(out).textContent = fmt(Number(el.value)));
    el.addEventListener('input', () => {
      settings[id] = Number(el.value);
      show();
      saveJSON(SETTINGS_KEY, settings);
      updateAreaInfo();
      updateGenerateButton();
    });
    show();
  }

  $$('#presets button[data-size]').forEach((b) => b.addEventListener('click', () => applyPreset(Number(b.dataset.size))));
  $('#draw-btn').addEventListener('click', () => setMode(state.mode === 'draw' ? 'idle' : 'draw'));
  $('#spawn-btn').addEventListener('click', () => setMode(state.mode === 'spawn' ? 'idle' : 'spawn'));
  $('#spawn-clear').addEventListener('click', () => setSpawn(null));
  $('#generate').addEventListener('click', generate);
  onSettingsChanged();
}

function onSettingsChanged() {
  document.body.classList.toggle('bedrock', settings.edition === 'bedrock');
  updateDestination();
}

function updateGenerateButton() {
  $('#generate').disabled = !state.bounds || areaTooLarge() || state.submitting;
}

function updateDestination() {
  const el = $('#dest-info');
  const saves = state.status?.saves_dir;
  if (!el) return;
  if (settings.edition === 'bedrock') el.textContent = 'You get a .mcworld file: open it on a device with Minecraft Bedrock to import.';
  else if (saves && settings.install) el.textContent = 'Finished worlds appear in your Minecraft singleplayer list.';
  else el.textContent = `Finished worlds are saved in ${state.status?.worlds_dir ?? 'MCMap’s data folder'}.`;
}

function showFormError(message) {
  const el = $('#form-error');
  el.hidden = !message;
  el.textContent = message || '';
}

async function generate() {
  if (!state.bounds) return;
  const sw = state.bounds.getSouthWest().wrap();
  const ne = state.bounds.getNorthEast().wrap();
  if (sw.lng > ne.lng) {
    showFormError('Areas that cross the 180° meridian are not supported.');
    return;
  }
  const nameField = $('#name');
  const payload = {
    bbox: { south: sw.lat, west: sw.lng, north: ne.lat, east: ne.lng },
    spawn: state.spawn ? { lat: state.spawn.lat, lng: state.spawn.wrap().lng } : null,
    name: nameField.value.trim() || nameField.placeholder,
    edition: settings.edition,
    mode: settings.mode,
    gamemode: settings.gamemode,
    world_time: Number(settings.world_time),
    scale: Number(settings.scale),
    height_multiplier: Number(settings.height_multiplier),
    interior: settings.interior,
    caves: settings.caves,
    props_3d: settings.props_3d,
    overture: settings.overture,
    map_preview: settings.map_preview,
    world_type: settings.world_type,
    signage: settings.signage,
    install: Boolean(settings.install && state.status?.saves_dir),
  };
  const btn = $('#generate');
  state.submitting = true;
  updateGenerateButton();
  btn.textContent = 'Starting…';
  try {
    await api('/api/jobs', { method: 'POST', body: payload });
    showFormError(null);
    nameField.value = '';
    await refresh();
    $('.jobs').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    showFormError(err.message);
  } finally {
    state.submitting = false;
    btn.textContent = 'Generate world';
    updateGenerateButton();
  }
}

// ---------------------------------------------------------------- engine banner

function renderEngine() {
  const el = $('#engine-banner');
  if (state.offline) {
    el.hidden = false;
    el.className = 'banner error';
    el.innerHTML = '<div><strong>Can’t reach the MCMap server.</strong> Is it still running? Start it with <code>python -m mcmap</code>.</div>';
    return;
  }
  const a = state.status?.arnis;
  if (!a || a.status === 'installed') {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.className = `banner${a.status === 'error' ? ' error' : ''}`;
  if (a.status === 'downloading') {
    const pct = Math.round(a.progress * 100);
    el.innerHTML = `<div>Downloading the Arnis ${escapeHtml(a.pinned_version)} engine… ${pct}%</div><div class="bar"><div style="width:${pct}%"></div></div>`;
  } else if (a.status === 'error') {
    el.innerHTML = `<div><strong>Couldn’t download Arnis:</strong> ${escapeHtml(a.error)}</div>
      <div class="row"><span></span><button type="button" class="btn small" data-engine-install>Try again</button></div>`;
  } else {
    el.innerHTML = `<div>MCMap builds worlds with the open-source <a href="https://github.com/louis-e/arnis" target="_blank" rel="noopener">Arnis</a> ${escapeHtml(a.pinned_version)} engine.
      It downloads automatically (about 60 MB) the first time you generate.</div>
      <div class="row"><span></span><button type="button" class="btn small" data-engine-install>Download now</button></div>`;
  }
}

$('#engine-banner').addEventListener('click', async (e) => {
  if (!e.target.closest('[data-engine-install]')) return;
  try {
    await api('/api/arnis/install', { method: 'POST' });
  } catch (err) {
    alert(err.message);
  }
  refresh();
});

// ---------------------------------------------------------------- jobs

function renderJobs() {
  const ul = $('#jobs');
  $('#jobs-empty').hidden = state.jobs.length > 0;
  const seen = new Set();
  state.jobs.forEach((job, index) => {
    seen.add(job.id);
    let li = ul.querySelector(`li[data-id="${job.id}"]`);
    if (!li) li = createJobElement(job.id);
    if (ul.children[index] !== li) ul.insertBefore(li, ul.children[index] || null);
    updateJobElement(li, job);
  });
  [...ul.children].forEach((li) => seen.has(li.dataset.id) || li.remove());
}

function createJobElement(id) {
  const li = document.createElement('li');
  li.dataset.id = id;
  li.innerHTML = `
    <div class="job-head"><span class="job-name"></span><span class="badge"></span></div>
    <div class="job-meta"></div>
    <div class="bar"><div></div></div>
    <div class="job-step"></div>
    <div class="job-error"></div>
    <img class="job-preview" hidden alt="Map preview of the world">
    <div class="job-actions"></div>
    <pre class="job-log" hidden></pre>`;
  $('.job-preview', li).addEventListener('click', (e) => openLightbox(e.target.src));
  $('.job-actions', li).addEventListener('click', (e) => {
    const b = e.target.closest('button[data-action]');
    if (b) jobAction(id, b.dataset.action, b);
  });
  return li;
}

function jobStepText(job) {
  switch (job.status) {
    case 'queued':
      return 'Waiting for the current world to finish';
    case 'running':
      return `${job.step} · ${Math.round(job.progress * 100)}%`;
    case 'done':
      if (!job.world_exists) return 'The world files are no longer there';
      if (job.installed) return 'In your Minecraft singleplayer list';
      return job.edition === 'bedrock' ? 'Ready to download' : 'Saved in MCMap’s worlds folder';
    case 'cancelled':
      return 'Cancelled';
    default:
      return '';
  }
}

function jobActions(job) {
  const actions = [];
  const active = job.status === 'queued' || job.status === 'running';
  if (active) actions.push(['cancel', 'Cancel', 'danger']);
  if (job.status === 'done' && job.world_exists) {
    if (job.edition === 'java' && !job.installed && state.status?.saves_dir) actions.push(['install', 'Add to Minecraft', 'primary']);
    actions.push(['download', job.edition === 'bedrock' ? 'Download .mcworld' : 'Download .zip']);
    actions.push(['reveal', 'Show in folder']);
  }
  if (job.status === 'failed' || job.status === 'cancelled') actions.push(['retry', 'Retry']);
  actions.push(['reuse', 'Reuse area']);
  actions.push(['log', state.openLogs.has(job.id) ? 'Hide log' : 'Log']);
  if (!active) actions.push(['remove', 'Remove', 'danger']);
  return actions;
}

function updateJobElement(li, job) {
  li.className = `job status-${job.status}`;
  const name = $('.job-name', li);
  name.textContent = job.name;
  name.title = job.name;
  $('.badge', li).textContent = STATUS_LABELS[job.status] || job.status;

  const b = job.request.bbox;
  const { km2 } = areaStats(L.latLngBounds([b.south, b.west], [b.north, b.east]));
  const end = job.finished ?? Date.now() / 1000;
  const meta = [job.edition === 'bedrock' ? 'Bedrock' : 'Java', fmtArea(km2), MODE_LABELS[job.request.mode]];
  if (job.started) meta.push(fmtDuration(end - job.started));
  if (job.world_size) meta.push(fmtBytes(job.world_size));
  $('.job-meta', li).textContent = meta.join(' · ');

  $('.bar > div', li).style.width = `${Math.round(job.progress * 100)}%`;
  $('.job-step', li).textContent = jobStepText(job);
  $('.job-error', li).textContent = job.error || '';

  const img = $('.job-preview', li);
  img.hidden = !job.has_preview;
  const src = `/api/jobs/${job.id}/preview.png?v=${job.finished}`;
  if (job.has_preview && img.getAttribute('src') !== src) img.src = src;

  // Rebuild buttons only when they change, so a poll never swaps one out mid-click.
  const actions = jobActions(job);
  const signature = JSON.stringify(actions);
  const box = $('.job-actions', li);
  if (box.dataset.signature !== signature) {
    box.dataset.signature = signature;
    box.innerHTML = actions
      .map(([action, label, kind]) => `<button type="button" class="btn small ${kind || ''}" data-action="${action}">${escapeHtml(label)}</button>`)
      .join('');
  }
  $('.job-log', li).hidden = !state.openLogs.has(job.id);
}

async function jobAction(id, action, button) {
  const job = state.jobs.find((j) => j.id === id);
  if (!job) return;
  try {
    switch (action) {
      case 'cancel':
        await api(`/api/jobs/${id}/cancel`, { method: 'POST' });
        break;
      case 'retry':
        await api(`/api/jobs/${id}/retry`, { method: 'POST' });
        break;
      case 'install':
        button.disabled = true;
        button.textContent = 'Adding…';
        await api(`/api/jobs/${id}/install`, { method: 'POST' });
        break;
      case 'reveal':
        await api(`/api/jobs/${id}/reveal`, { method: 'POST' });
        return;
      case 'download': {
        button.textContent = 'Preparing…';
        setTimeout(() => (button.textContent = job.edition === 'bedrock' ? 'Download .mcworld' : 'Download .zip'), 4000);
        const a = document.createElement('a');
        a.href = `/api/jobs/${id}/download`;
        a.download = '';
        a.click();
        return;
      }
      case 'reuse':
        reuseArea(job);
        return;
      case 'log':
        if (state.openLogs.has(id)) state.openLogs.delete(id);
        else state.openLogs.add(id);
        renderJobs();
        refreshLog(id);
        return;
      case 'remove': {
        if (!confirm(`Remove “${job.name}” from the list?`)) return;
        const hasFiles = !job.installed && (job.world_exists || job.status === 'failed');
        const deleteFiles = hasFiles && confirm('Also delete the generated world files?\n\nOK deletes them. Cancel keeps them on disk.');
        await api(`/api/jobs/${id}${deleteFiles ? '?files=1' : ''}`, { method: 'DELETE' });
        state.openLogs.delete(id);
        break;
      }
    }
  } catch (err) {
    alert(err.message);
  }
  refresh();
}

function reuseArea(job) {
  const b = job.request.bbox;
  setMode('idle');
  setActivePreset(null);
  area.set(L.latLngBounds([b.south, b.west], [b.north, b.east]), { fit: true });
  const sp = job.request.spawn;
  setSpawn(sp ? L.latLng(sp.lat, sp.lng) : null);
}

async function refreshLog(id) {
  if (!state.openLogs.has(id)) return;
  const pre = $(`li[data-id="${id}"] .job-log`);
  if (!pre) return;
  try {
    const job = await api(`/api/jobs/${id}`);
    const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = job.log.join('\n') || '(no output yet)';
    if (nearBottom) pre.scrollTop = pre.scrollHeight;
  } catch {
    /* job removed */
  }
}

// ---------------------------------------------------------------- polling

async function refresh() {
  try {
    const [status, { jobs }] = await Promise.all([api('/api/status'), api('/api/jobs')]);
    const firstStatus = !state.status;
    state.status = status;
    state.jobs = jobs;
    state.offline = false;
    if (firstStatus) {
      $('#install-row').hidden = !status.saves_dir;
      updateAreaInfo();
    }
  } catch {
    state.offline = true;
  }
  renderEngine();
  renderJobs();
  updateDestination();
  updateGenerateButton();
  for (const id of state.openLogs) refreshLog(id);
  schedulePoll();
}

// Every refresh reschedules the next one, so starting a job switches to fast polling at once.
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = state.jobs.some((j) => j.status === 'queued' || j.status === 'running') || state.status?.arnis?.status === 'downloading';
  pollTimer = setTimeout(refresh, busy ? 1000 : 4000);
}

// ---------------------------------------------------------------- lightbox, keys

function openLightbox(src) {
  const box = $('#lightbox');
  $('img', box).src = src;
  box.hidden = false;
}
$('#lightbox').addEventListener('click', () => ($('#lightbox').hidden = true));

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (drawStart) {
    drawRect.remove();
    drawRect = drawStart = drawEnd = null;
  }
  setMode('idle');
  searchResults.hidden = true;
  $('#lightbox').hidden = true;
});

// ---------------------------------------------------------------- boot

initForm();
const saved = loadJSON(AREA_KEY, null);
if (saved?.bounds) {
  setActivePreset(saved.presetSize ?? null);
  area.set(L.latLngBounds(saved.bounds));
  if (saved.spawn && state.bounds.contains(saved.spawn)) setSpawn(L.latLng(saved.spawn));
}
updateSpawnInfo();
refresh();
