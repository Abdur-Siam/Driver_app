/* TOM Driver — PWA client. Framework-free for portability.
   Talks to /api/driver/v1. Offline-first: mutations queue to an outbox
   and replay with idempotency keys. */
'use strict';

// Relative on web/PWA (same origin as the server). In the Capacitor native
// build the frontend loads from capacitor://localhost, so set window.TOM_API_BASE
// (see config.js — protocol-guarded) to the deployed TOM backend's absolute origin.
const API = (window.TOM_API_BASE || '') + '/api/driver/v1';
const LS = {
  token: 'tom_driver_token',
  driver: 'tom_driver_profile',
  tracking: 'tom_driver_tracking',
  outbox: 'tom_driver_outbox',
  run: 'tom_driver_run',           // last-known run, for offline display
  pingOutbox: 'tom_driver_pings',  // durable GPS-ping queue (survives offline / app kill)
};

const S = {
  token: localStorage.getItem(LS.token) || null,
  driver: JSON.parse(localStorage.getItem(LS.driver) || 'null'),
  run: [], job: null, messages: [], unread: 0,
  mapsKey: null, tracking: localStorage.getItem(LS.tracking) === '1',
  geoWatch: null, pings: [], scan: null, lastPos: null,
  routeSource: 'tom',              // 'tom' = authoritative dispatch order, 'local' = on-device offline fallback
  home: null, earnings: null, statements: [], statement: null,
  profile: null, pending: [], expenses: [], tax: null,
  shift: null, dutyStatus: 'off', history: [], appVersion: '1.0.0',
  demo: false,                     // server says demo creds exist (login hint)
};

const LS_THEME = 'tom_driver_theme';
const LS_SOUND = 'tom_driver_sound_conn';

/* ── utils ── */
const $ = (sel, root = document) => root.querySelector(sel);
const el = (html) => {
  const t = document.createElement('template'); t.innerHTML = html.trim();
  const nodes = [...t.content.childNodes].filter(n => !(n.nodeType === 3 && !n.textContent.trim()));
  return nodes.length === 1 ? nodes[0] : t.content;   // single element, else fragment
};
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'id-' + Date.now() + '-' + Math.random().toString(16).slice(2));
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 2600);
}
function go(hash) { location.hash = hash; }
const gbp = (x) => '£' + (Math.round((Number(x) || 0) * 100) / 100).toFixed(2);
// Signed media refs come back as relative /media/... paths; in the native app
// they must resolve against the backend origin, not capacitor://localhost.
const mediaUrl = (u) => (u && u.startsWith('/') ? (window.TOM_API_BASE || '') + u : u);

/* ── api ── */
async function api(path, { method = 'GET', body, idemKey } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (S.token) headers['Authorization'] = 'Bearer ' + S.token;
  if (idemKey) headers['X-Idempotency-Key'] = idemKey;
  const res = await fetch(API + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let data = null; try { data = await res.json(); } catch (e) {}
  if (res.status === 401 && S.token) { logoutLocal(); go('#/login'); throw new Error('unauthorized'); }
  return { ok: res.ok, status: res.status, data };
}

/* mutating call with offline outbox fallback */
async function mutate(path, body, { idemKey } = {}) {
  const key = idemKey || uuid();
  try {
    const r = await api(path, { method: 'POST', body, idemKey: key });
    return r;
  } catch (e) {
    const queued = queueOutbox({ path, body, key }); syncChip();
    return { ok: false, queued, status: 0, data: null };
  }
}

function queueOutbox(item) {
  const q = JSON.parse(localStorage.getItem(LS.outbox) || '[]');
  q.push({ ...item, at: Date.now() });
  try { localStorage.setItem(LS.outbox, JSON.stringify(q)); return true; }
  catch (e) {
    // Storage quota exceeded — a queued POD/status action would be silently
    // lost. Make the failure VISIBLE so the driver keeps the evidence.
    toast('Phone storage is full — this could not be saved for later. Free up space, or retry when you have signal.', true);
    return false;
  }
}
async function drainOutbox() {
  let q = JSON.parse(localStorage.getItem(LS.outbox) || '[]');
  if (!q.length || !navigator.onLine || !S.token) return;
  const keep = []; let rejected = 0;
  for (const it of q) {
    try {
      const r = await api(it.path, { method: 'POST', body: it.body, idemKey: it.key });
      // api() RESOLVES (never rejects) on an HTTP response and REJECTS only on a
      // true network failure. So the old `status === 0` test never fired and a
      // transient 5xx on reconnect (cold start / PgBouncer) silently DROPPED a
      // queued POD/status action. Re-queue on a retryable server error (5xx) or
      // throttling (429) — the idempotency key makes the re-send safe. Only
      // DISCARD on success or a definitive client rejection (4xx), which a
      // replay can't fix, and surface that so it is never silent.
      if (!r.ok && (r.status >= 500 || r.status === 429)) keep.push(it);
      else if (!r.ok) rejected += 1;
    } catch (e) { keep.push(it); } // network failure / abort — keep for next drain
  }
  localStorage.setItem(LS.outbox, JSON.stringify(keep)); syncChip();
  const succeeded = q.length - keep.length - rejected;
  if (rejected) toast(`${rejected} queued action(s) were rejected by the server — please review`, true);
  if (succeeded > 0 && !keep.length) {
    if (!rejected) toast('Synced');
    if (location.hash.startsWith('#/run')) { await loadRun(); render(); }
  }
}
function outboxCount() { return JSON.parse(localStorage.getItem(LS.outbox) || '[]').length; }

/* ── auth ── */
function logoutLocal() {
  S.token = null; S.driver = null; localStorage.removeItem(LS.token); localStorage.removeItem(LS.driver);
  stopTracking();
}
async function doLogin(identifier, password) {
  // A dead network or unreachable server must fail fast and visibly —
  // never leave the driver stuck on "Signing in…".
  let res;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 15000);
    res = await fetch(API + '/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier, password, device: { id: deviceId(), platform: navigator.platform, model: 'PWA' } }),
      signal: ctl.signal,
    });
    clearTimeout(timer);
  } catch (e) {
    toast('Can’t reach the server — check your connection', true);
    return false;
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) { toast((data && data.error && data.error.code === 'rate_limited') ? 'Too many attempts' : 'Invalid login', true); return false; }
  S.token = data.token; S.driver = data.driver;
  localStorage.setItem(LS.token, S.token); localStorage.setItem(LS.driver, JSON.stringify(S.driver));
  return true;
}
function deviceId() {
  let d = localStorage.getItem('tom_driver_device'); if (!d) { d = uuid(); localStorage.setItem('tom_driver_device', d); } return d;
}

/* ── data loads ── */
async function loadConfig() { try { const r = await api('/config'); if (r.ok) { S.mapsKey = r.data.maps_browser_key; S.appVersion = r.data.app_version || S.appVersion; S.demo = !!r.data.demo; } } catch (e) {} }
async function loadRun() {
  try {
    const r = await api('/run');
    if (r.ok) {
      // TOM is authoritative — adopt its order and remember it for offline use.
      S.run = r.data.jobs; S.routeSource = 'tom';
      localStorage.setItem(LS.run, JSON.stringify(S.run));
    }
  } catch (e) {
    // Offline: show the last-known run rather than an empty screen.
    const cached = JSON.parse(localStorage.getItem(LS.run) || 'null');
    if (cached && !S.run.length) S.run = cached;
  }
}
async function loadJob(docket) { try { const r = await api('/jobs/' + docket); S.job = r.ok ? r.data.job : null; } catch (e) { S.job = null; } }
async function loadMessages() { try { const r = await api('/messages'); if (r.ok) { S.messages = r.data.messages; S.unread = r.data.messages.filter(m => m.direction === 'ops' && !m.read).length; } } catch (e) {} }
// Opening the inbox marks ops messages read (clears the badge server-side too).
async function markMessagesRead() {
  if (!S.unread) return;
  try {
    const r = await api('/messages/read', { method: 'POST' });
    if (r.ok) { S.unread = 0; S.messages = S.messages.map(m => (m.direction === 'ops' ? { ...m, read: 1 } : m)); }
  } catch (e) {} // offline — badge clears on the next online open
}
async function loadHome() { try { const r = await api('/home'); if (r.ok) { S.home = r.data; S.unread = r.data.unread_messages || 0; S.shift = r.data.shift; S.dutyStatus = r.data.duty_status; } } catch (e) {} }
async function loadEarnings() { try { const r = await api('/earnings'); if (r.ok) S.earnings = r.data; } catch (e) {} }
async function loadStatements() { try { const r = await api('/statements'); if (r.ok) S.statements = r.data.statements; } catch (e) {} }
async function loadStatement(sid) { try { const r = await api('/statements/' + sid); S.statement = r.ok ? r.data.statement : null; } catch (e) { S.statement = null; } }
async function loadProfile() { try { const r = await api('/profile'); if (r.ok) { S.profile = r.data.profile; S.pending = r.data.pending;
  if (r.data.profile.theme) applyTheme(r.data.profile.theme);
  localStorage.setItem(LS_SOUND, r.data.profile.sound_alert_conn ? '1' : '0'); } } catch (e) {} }
async function loadExpenses() { try { const r = await api('/expenses'); if (r.ok) S.expenses = r.data.expenses; } catch (e) {} }
async function loadTax() { try { const r = await api('/tax'); if (r.ok) S.tax = r.data; } catch (e) {} }
async function loadShift() { try { const r = await api('/shift'); if (r.ok) { S.shift = r.data.shift; S.dutyStatus = r.data.duty_status; } } catch (e) {} }
async function loadHistory() { try { const r = await api('/history'); if (r.ok) S.history = r.data.jobs; } catch (e) {} }

/* ── shift / duty helpers ── */
function shiftRemaining(plannedEnd) {
  if (!plannedEnd) return null;
  const [h, m] = plannedEnd.split(':').map(Number);
  const now = new Date(); const end = new Date(now); end.setHours(h, m, 0, 0);
  let mins = Math.round((end - now) / 60000); if (mins < 0) mins = 0;
  return { mins, label: `${Math.floor(mins / 60)}h ${mins % 60}m`, end: plannedEnd };
}
function dutyPill() {
  if (S.dutyStatus === 'available') return '<span class="pill green">● Available</span>';
  if (S.dutyStatus === 'going_home') return '<span class="pill amber">⌂ Going home</span>';
  return '<span class="pill grey">○ Off shift</span>';
}

/* ── theme ── */
function applyTheme(theme) {
  theme = theme || localStorage.getItem(LS_THEME) || 'night';
  let resolved = theme;
  if (theme === 'auto') resolved = window.matchMedia('(prefers-color-scheme: light)').matches ? 'day' : 'night';
  document.documentElement.dataset.theme = resolved;
  localStorage.setItem(LS_THEME, theme);
}

/* ── connection-lost alert ── */
let _audioCtx = null;
function beep() {
  if (localStorage.getItem(LS_SOUND) !== '1') return;
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = _audioCtx.createOscillator(), g = _audioCtx.createGain();
    o.type = 'sine'; o.frequency.value = 880; o.connect(g); g.connect(_audioCtx.destination);
    g.gain.setValueAtTime(0.001, _audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.3, _audioCtx.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.4);
    o.start(); o.stop(_audioCtx.currentTime + 0.42);
  } catch (e) {}
}
function showOfflineBanner(on) {
  let b = $('#offbar');
  if (on) {
    if (!b) { b = el('<div id="offbar" style="position:sticky;top:0;z-index:30;background:var(--danger);color:#fff;text-align:center;padding:8px;font-size:14px;font-weight:600">⚠ Offline — you may miss jobs. Reconnecting…</div>'); document.getElementById('app').prepend(b); }
  } else if (b) b.remove();
}

/* ── header / chips ── */
function syncChip() { const c = $('#syncchip'); if (!c) return; const n = outboxCount();
  c.className = 'chip ' + (n ? 'warn' : 'ok'); c.innerHTML = n ? `<span class="dot warn"></span>${n} queued` : `<span class="dot"></span>Synced`; }

// The five bottom-tab roots + login show no back button; every sub-page does.
function isRootHash() {
  const r = (location.hash || '#/home').slice(2).split('/')[0] || 'home';
  return ['home', 'run', 'earnings', 'messages', 'me', 'login'].includes(r);
}
function header(title, sub) {
  const rem = S.shift ? shiftRemaining(S.shift.planned_end) : null;
  const back = isRootHash() ? '' : `<button class="hdr-back" onclick="history.back()" aria-label="Back">‹</button>`;
  return `<div class="hdr"><div class="hdr-row">
    <div style="display:flex;align-items:center;gap:8px;min-width:0">${back}
    <div style="min-width:0"><h1>${esc(title)}</h1>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div></div>
    <div class="stack" style="align-items:flex-end;gap:6px">
      <span id="syncchip" class="chip ok"><span class="dot"></span>Synced</span>
      ${dutyPill()}
      ${rem ? `<span class="tiny" style="color:#cfe2fb">⏱ <span id="shiftclock">${rem.label}</span> left</span>` : ''}
    </div></div></div>`;
}
function nav(active) {
  const item = (h, ico, label, key, badge) =>
    `<a href="${h}" class="${active === key ? 'active' : ''}"><span class="ico">${ico}</span>${label}${badge ? `<span class="badge">${badge}</span>` : ''}</a>`;
  return `<div class="nav">
    ${item('#/home', '🏠', 'Home', 'home')}
    ${item('#/run', '🗺️', 'Run', 'run')}
    ${item('#/earnings', '💷', 'Pay', 'earnings')}
    ${item('#/messages', '💬', 'Inbox', 'messages', S.unread || '')}
    ${item('#/me', '👤', 'Me', 'me')}
  </div>`;
}

/* ── deadline pill ── */
function deadlinePill(hhmm) {
  if (!hhmm) return '';
  const [h, m] = hhmm.split(':').map(Number); const now = new Date();
  const dl = new Date(now); dl.setHours(h, m, 0, 0);
  const mins = (dl - now) / 60000;
  const cls = mins < 0 ? 'red' : mins < 45 ? 'amber' : 'green';
  return `<span class="pill ${cls}">${esc(hhmm)}</span>`;
}
const STATUS_LABEL = {
  assigned: 'New', acknowledged: 'Accepted', en_route_pickup: 'To pickup',
  at_pickup: 'At pickup', pob: 'On board', en_route_drop: 'Delivering', completed: 'Done',
};

/* ── screens ── */
function loginScreen() {
  const v = el(`<div class="login">
    <div class="logo"><div class="mark">XM</div><div><div style="font-weight:700;font-size:20px">TOM Driver</div>
      <div class="muted small">Xtra Mile Couriers</div></div></div>
    <label for="id">Driver ID or callsign</label>
    <input id="id" type="text" autocomplete="username" autocapitalize="characters" placeholder="DRV001" />
    <label for="pw">Password</label>
    <input id="pw" type="password" autocomplete="current-password" placeholder="••••••••" />
    <div class="spacer"></div>
    <button class="btn" id="loginbtn">Sign in</button>
    ${S.demo ? '<p class="muted tiny" style="text-align:center;margin-top:18px">Demo: DRV001 / test1234</p>' : ''}
  </div>`);
  v.querySelector('#loginbtn').onclick = async (e) => {
    const id = v.querySelector('#id').value.trim(), pw = v.querySelector('#pw').value;
    if (!id || !pw) return toast('Enter your details', true);
    e.target.disabled = true; e.target.textContent = 'Signing in…';
    let ok = false;
    try { ok = await doLogin(id, pw); }
    finally { e.target.disabled = false; e.target.textContent = 'Sign in'; }
    if (ok) { await Promise.all([loadConfig(), loadMessages()]); go('#/home'); }
  };
  v.querySelector('#pw').addEventListener('keydown', e => { if (e.key === 'Enter') v.querySelector('#loginbtn').click(); });
  return v;
}

function runScreen() {
  const wrap = el('<div></div>');
  wrap.appendChild(el(header('Today', new Date().toDateString())));
  const main = el('<main></main>');
  const total = S.run.length;
  const local = S.routeSource === 'local';
  const srcLine = local
    ? '<span class="pill amber">⚠ On-device order (offline)</span> — re-syncs to dispatch when back online'
    : 'Optimised by dispatch · tap a job to start';
  main.appendChild(el(`<div class="card"><div class="row">
      <div class="stack"><div style="font-weight:700">${total} stop${total !== 1 ? 's' : ''} today</div>
      <div class="muted small">${srcLine}</div></div>
      <div class="btn-row" style="width:auto"><button class="btn ghost sm" id="histbtn">History</button>
      <button class="btn ghost sm" id="optbtn">↻ Optimise</button></div></div></div>`));
  main.appendChild(el(mapPanel()));
  if (!total) {
    main.appendChild(el(`<div class="empty"><div class="big">✅</div><div>No active jobs.<br>You're all caught up.</div></div>`));
  } else {
    main.appendChild(el('<div class="section-title">Your run</div>'));
    const etas = runEtas(S.run);
    S.run.forEach((j, i) => main.appendChild(jobCard(j, i + 1, etas[j.docket_number])));
  }
  wrap.appendChild(main);
  wrap.appendChild(el(nav('run')));
  setTimeout(() => { const o = $('#optbtn'); if (o) o.onclick = optimise; const hb = $('#histbtn'); if (hb) hb.onclick = () => go('#/history'); mountMap(); syncChip(); }, 0);
  return wrap;
}

function jobCard(j, n, eta) {
  const drops = j.drops.length;
  const etaTag = eta ? `<span class="pill grey">🕑 ETA ${hhmm(eta)}</span>` : '';
  const c = el(`<div class="card tap"><div class="row">
      <div style="display:flex;gap:12px;align-items:center">
        <span class="seqnum">${n}</span>
        <div class="stack"><div style="font-weight:700">${esc(j.account || 'Job')} · ${esc(j.vehicle || '')}</div>
          <div class="muted small">${esc(j.pickup.postcode || j.pickup.address || '')} → ${drops} drop${drops !== 1 ? 's' : ''} · ${j.parcel_count} parcel${j.parcel_count !== 1 ? 's' : ''}</div></div>
      </div>${deadlinePill(j.deadline)}</div>
      <div class="row" style="margin-top:10px"><div style="display:flex;gap:6px;align-items:center">
        <span class="pill blue">${STATUS_LABEL[j.lifecycle_status] || j.lifecycle_status}</span>
        ${j.requires_scan ? '<span class="pill amber">📷 Scan</span>' : ''}</div>
      <div style="display:flex;gap:6px;align-items:center">${etaTag}<span class="muted small mono">${esc(j.docket_number)}</span></div></div></div>`);
  c.onclick = () => go('#/job/' + j.docket_number);
  return c;
}

function mapPanel() {
  if (S.mapsKey) return `<div class="mapbox" id="map"></div>`;
  return `<div class="mapbox"><div class="map-fallback"><div style="font-size:30px">🗺️</div>
    <div>Live Google Map appears here</div>
    <div class="tiny">Add your Google Maps key to enable turn-by-turn and the live route view.</div></div></div>`;
}
function mountMap() {
  if (!S.mapsKey || !$('#map')) return;
  // When a key is supplied, load Google Maps JS and draw the run + driver pin.
  if (!window.google) {
    const s = document.createElement('script');
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(S.mapsKey)}`;
    s.onload = drawMap; document.head.appendChild(s);
  } else drawMap();
}
function drawMap() {
  if (!window.google || !$('#map')) return;
  const pts = S.run.filter(j => j.pickup.lat).map(j => ({ lat: j.pickup.lat, lng: j.pickup.lng }));
  const center = pts[0] || { lat: 51.5237, lng: -0.1075 };
  const map = new google.maps.Map($('#map'), { center, zoom: 12, disableDefaultUI: true });
  pts.forEach((p, i) => new google.maps.Marker({ position: p, map, label: String(i + 1) }));
}

/* ── on-device route fallback + ETAs (consumer / edge model) ──
   The device holds NO Google server key and never calls Google directly: when
   TOM is reachable it owns the optimised order (consumed via /run). Offline, the
   device re-sequences its own stops with a nearest-neighbour haversine so the
   driver keeps moving; on reconnect we discard this and re-sync to TOM. */
const AVG_SPEED_KMH = 30;   // urban driving assumption, ETA display only
const SERVICE_MIN = 4;      // handling minutes per stop, ETA display only
const DEFAULT_ORIGIN = { lat: 51.5237, lng: -0.1075 };
const _hasCoords = (j) => j && j.pickup && j.pickup.lat != null && j.pickup.lng != null;

function haversineKm(a, b) {
  const R = 6371, toR = (d) => d * Math.PI / 180;
  const dLat = toR(b.lat - a.lat), dLng = toR(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toR(a.lat)) * Math.cos(toR(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
// Nearest-neighbour over pickup coords; coordless jobs keep booked order at the end.
function localOptimise(origin, jobs) {
  const remaining = jobs.filter(_hasCoords).slice();
  const without = jobs.filter((j) => !_hasCoords(j));
  const ordered = [];
  let cur = origin || DEFAULT_ORIGIN, totalKm = 0;
  while (remaining.length) {
    let bi = 0, bd = Infinity;
    remaining.forEach((j, i) => { const d = haversineKm(cur, j.pickup); if (d < bd) { bd = d; bi = i; } });
    const nxt = remaining.splice(bi, 1)[0];
    totalKm += bd; ordered.push(nxt); cur = { lat: nxt.pickup.lat, lng: nxt.pickup.lng };
  }
  return { jobs: ordered.concat(without), totalKm };
}
// Cumulative arrival ETAs for display, seeded from the driver's last known position.
function runEtas(jobs, origin) {
  const out = {}; let t = Date.now(); let cur = origin || S.lastPos || null;
  for (const j of jobs) {
    if (_hasCoords(j) && cur) t += (haversineKm(cur, j.pickup) / AVG_SPEED_KMH) * 3600e3;
    out[j.docket_number] = new Date(t);
    t += SERVICE_MIN * 60e3;
    if (_hasCoords(j)) cur = { lat: j.pickup.lat, lng: j.pickup.lng };
  }
  return out;
}
const hhmm = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

async function optimise() {
  const pos = await currentPos().catch(() => null);
  if (pos) S.lastPos = pos;
  if (navigator.onLine) {
    toast('Optimising route…');
    const r = await mutate('/route/optimise', { from: pos ? { lat: pos.lat, lng: pos.lng } : {} });
    if (r.ok) { toast(`Route set (${r.data.engine === 'google_routes' ? 'Google' : 'offline'}) · ${r.data.total_distance_km} km`); await loadRun(); render(); return; }
    // call failed/queued → drop to on-device order so the driver isn't stuck
  }
  const { jobs, totalKm } = localOptimise(pos, S.run);
  S.run = jobs; S.routeSource = 'local';
  localStorage.setItem(LS.run, JSON.stringify(S.run));
  toast(`On-device order · ~${totalKm.toFixed(1)} km (offline)`);
  render();
}

/* ── job detail ── */
function jobScreen() {
  const j = S.job;
  const wrap = el('<div></div>');
  wrap.appendChild(el(header(j ? (j.account || 'Job') : 'Job', j ? j.docket_number : '')));
  const main = el('<main></main>');
  if (!j) { main.appendChild(el(`<div class="empty"><div class="big">⚠️</div><div>Job not found.</div></div>`));
    main.appendChild(backBtn()); wrap.appendChild(main); return wrap; }

  main.appendChild(el(timeline(j)));
  // Scanning is per-job: only surface (and enforce) it where the job demands it.
  if (j.requires_scan) {
    main.appendChild(el(`<div class="card" style="border-color:var(--accent)"><span class="pill blue">📷 Scan required</span>
      <span class="small" style="margin-left:6px">Every parcel must be barcode-scanned at pickup and at each drop.</span></div>`));
  }
  // pickup (wrapped in a single container — el() returns one root)
  main.appendChild(el(`<div>
    <div class="section-title">Pickup</div>
    <div class="card">
      <div class="stack">
        <div style="font-weight:700">${esc(j.pickup.address || j.pickup.postcode || '')}</div>
        <div class="muted small">${esc(j.pickup.postcode || '')}${j.pickup.contact ? ' · ' + esc(j.pickup.contact) : ''}</div>
        ${j.pickup.notes ? `<div class="pill amber" style="margin-top:6px;width:fit-content">${esc(j.pickup.notes)}</div>` : ''}
      </div>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn ghost sm" onclick="navTo('${enc(j.pickup)}')">🧭 Navigate</button>
        <button class="btn secondary sm" onclick="location.href='tel:'">📞 Call</button>
      </div>
    </div>
    ${j.special_instructions ? `<div class="card"><span class="pill red">!</span> ${esc(j.special_instructions)}</div>` : ''}
  </div>`));

  // drops
  main.appendChild(el(`<div class="section-title">Drops (${j.drops.length})</div>`));
  j.drops.forEach(d => main.appendChild(dropCard(j, d)));

  wrap.appendChild(main);

  // primary action bar
  const pa = primaryAction(j);
  if (pa) {
    const bar = el(`<div class="fab-bar"><button class="btn" id="primary">${pa.label}</button></div>`);
    bar.querySelector('#primary').onclick = () => pa.run();
    wrap.appendChild(bar);
  } else {
    wrap.appendChild(el(nav('run')));
  }
  return wrap;
}

function timeline(j) {
  const order = ['assigned', 'acknowledged', 'en_route_pickup', 'at_pickup', 'pob', 'en_route_drop', 'completed'];
  const cur = order.indexOf(j.lifecycle_status);
  return '<div class="timeline">' + order.slice(1).map((s, i) =>
    `<div class="seg ${i < cur ? 'done' : i === cur - 1 ? 'cur' : ''}"></div>`).join('') + '</div>';
}

function dropCard(j, d) {
  const can = ['pob', 'en_route_drop'].includes(j.lifecycle_status);
  const statusPill = d.status === 'delivered' ? '<span class="pill green">Delivered</span>'
    : d.status === 'failed' ? '<span class="pill red">Failed</span>' : '<span class="pill grey">Pending</span>';
  const c = el(`<div class="card"><div class="row"><div class="stack">
      <div style="font-weight:700">${d.seq}. ${esc(d.address || d.postcode || '')}</div>
      <div class="muted small">${esc(d.postcode || '')}${d.contact ? ' · ' + esc(d.contact) : ''}</div>
      ${d.instructions ? `<div class="tiny muted">${esc(d.instructions)}</div>` : ''}
      <div class="tiny muted">${d.parcels.length} parcel${d.parcels.length !== 1 ? 's' : ''}</div>
    </div>${statusPill}</div></div>`);
  if (can && d.status === 'pending') {
    const allScanned = d.parcels.every(p => p.state === 'delivered');
    const row = el(`<div class="btn-row" style="margin-top:12px"></div>`);
    const navb = el(`<button class="btn ghost sm">🧭 Navigate</button>`); navb.onclick = () => navTo(enc(d));
    row.appendChild(navb);
    // Scan gate applies only to scan-required jobs; others go straight to POD.
    if (j.requires_scan && !allScanned) {
      const sc = el(`<button class="btn sm">📷 Scan & deliver</button>`);
      sc.onclick = () => go(`#/scan/${j.docket_number}/deliver/${d.seq}`); row.appendChild(sc);
    } else {
      const pod = el(`<button class="btn ok sm">✍️ Proof of delivery</button>`);
      pod.onclick = () => go(`#/pod/${j.docket_number}/${d.seq}`); row.appendChild(pod);
    }
    const fail = el(`<button class="btn danger sm">Can't</button>`);
    fail.onclick = () => go(`#/fail/${j.docket_number}/${d.seq}`); row.appendChild(fail);
    c.appendChild(row);
  }
  // View-back: show captured proof on a delivered drop.
  if (d.status === 'delivered') {
    const rec = (j.pod || []).find(p => p.seq === d.seq);
    if (rec) {
      const imgs = [rec.signature, ...(rec.photos || [])].filter(Boolean);
      if (imgs.length) c.appendChild(el(`<div class="pod-thumbs" style="margin-top:10px">${imgs.map(u => `<div class="pod-thumb"><img src="${esc(mediaUrl(u))}" alt="Proof of delivery"/></div>`).join('')}</div>`));
      if (rec.recipient) c.appendChild(el(`<div class="tiny muted" style="margin-top:6px">Received by ${esc(rec.recipient)}${rec.at ? ' · ' + new Date(rec.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}</div>`));
    }
  }
  return c;
}

function primaryAction(j) {
  const ls = j.lifecycle_status;
  const step = (label, action) => ({ label, run: async () => {
    const r = await mutate(`/jobs/${j.docket_number}/status`, { action }, { idemKey: uuid() });
    if (r.ok) { await loadJob(j.docket_number); render(); toast('Updated'); }
    else if (r.queued) { toast('Queued — will sync'); }
    else { const code = r.data && r.data.error && r.data.error.code; toast(code === 'parcels_outstanding' ? 'Scan all parcels first' : 'Not allowed now', true); }
  }});
  if (ls === 'assigned') return step('Accept job', 'acknowledge');
  if (ls === 'acknowledged') return step('On the way to pickup', 'en_route_pickup');
  if (ls === 'en_route_pickup') return step('Arrived at pickup', 'arrive_pickup');
  // At pickup: scan-required jobs must go through the scanner; the rest
  // confirm loading directly (the server enforces the same rule).
  if (ls === 'at_pickup') {
    if (j.requires_scan) return { label: '📷 Scan parcels', run: () => go(`#/scan/${j.docket_number}/collect`) };
    return step('Confirm parcels on board', 'collected');
  }
  if (ls === 'pob') return step('Start delivery', 'en_route_drop');
  return null; // en_route_drop → per-drop actions; completed → none
}

/* ── scan screen ── */
function scanScreen(docket, phase, dropSeq) {
  S.scan = { docket, phase, dropSeq: dropSeq != null ? Number(dropSeq) : null, detector: null, stream: null };
  const j = S.job;
  const expected = j ? collectExpected(j, phase, S.scan.dropSeq) : [];
  const wrap = el('<div></div>');
  wrap.appendChild(el(header(phase === 'collect' ? 'Scan parcels' : 'Scan parcel', j ? j.docket_number : '')));
  const main = el('<main></main>');
  main.appendChild(el(`<div class="scan-wrap"><video id="cam" playsinline muted></video><div class="reticle"></div></div>`));
  const nativeScan = !!(window.TOMNative && window.TOMNative.isNative && window.TOMNative.scanBarcode);
  main.appendChild(el(`<div class="btn-row" style="margin:12px 0">
    ${nativeScan ? '<button class="btn sm" id="hwscan">📷 Scan</button>' : ''}
    <button class="btn secondary sm" id="manual">⌨️ Enter manually</button>
    <button class="btn ghost sm" id="torch">🔦 Torch</button></div>`));
  const list = el(`<div class="card checklist" id="checklist"></div>`);
  main.appendChild(list);
  wrap.appendChild(main);

  const done = collectDone(expected);
  const bar = el(`<div class="fab-bar"></div>`);
  if (phase === 'collect') {
    const b = el(`<button class="btn ${done ? 'ok' : ''}" id="confirm" ${done ? '' : 'disabled'}>Confirm on board</button>`);
    b.onclick = confirmCollect; bar.appendChild(b);
  } else {
    const b = el(`<button class="btn ok" id="confirm" ${done ? '' : 'disabled'}>Continue to proof</button>`);
    b.onclick = () => go(`#/pod/${docket}/${S.scan.dropSeq}`); bar.appendChild(b);
  }
  wrap.appendChild(bar);
  setTimeout(() => {
    renderChecklist();
    // Native build: use the OS/ML-Kit hardware scanner; browser PWA uses BarcodeDetector.
    if (nativeScan) { const hb = $('#hwscan'); if (hb) hb.onclick = hardwareScan; } else { startCamera(); }
    $('#manual').onclick = manualEntry;
  }, 0);
  return wrap;
}
function collectExpected(j, phase, dropSeq) {
  let parcels = [];
  j.drops.forEach(d => { if (phase === 'deliver' && dropSeq != null && d.seq !== dropSeq) return; d.parcels.forEach(p => parcels.push({ ...p, drop: d.seq })); });
  return parcels;
}
function collectDone(expected) {
  if (S.scan.phase === 'collect') return expected.every(p => p.state === 'on_board' || p.state === 'delivered');
  return expected.every(p => p.state === 'delivered');
}
function renderChecklist() {
  const j = S.job; if (!j) return;
  const expected = collectExpected(j, S.scan.phase, S.scan.dropSeq);
  const scanned = expected.filter(p => (S.scan.phase === 'collect') ? (p.state !== 'expected') : (p.state === 'delivered')).length;
  const cl = $('#checklist'); if (!cl) return;
  cl.innerHTML = `<div class="row" style="margin-bottom:8px"><b>${scanned} / ${expected.length} scanned</b></div>` +
    expected.map(p => {
      const ok = (S.scan.phase === 'collect') ? p.state !== 'expected' : p.state === 'delivered';
      return `<div class="ck"><span class="${ok ? 'tick' : 'pending'}">${ok ? '✔︎' : '○'}</span>
        <span class="mono">${esc(p.barcode)}</span><span class="muted small" style="margin-left:auto">${esc(p.description || '')}</span></div>`;
    }).join('');
  const c = $('#confirm'); if (c) { const done = collectDone(expected); c.disabled = !done; if (done) c.classList.add('ok'); }
}

async function startCamera() {
  const video = $('#cam'); if (!video) return;
  if (!('BarcodeDetector' in window) || !navigator.mediaDevices) {
    const why = platform() === 'ios'
      ? 'Safari can’t decode barcodes in the browser — the installed native app scans with the camera.'
      : 'Camera scanning needs Chrome on Android (or the native app).';
    video.parentElement.querySelector('.reticle').insertAdjacentHTML('afterend',
      `<div style="position:absolute;color:#9fb6d6;text-align:center;padding:16px;font-size:14px">${why}<br>Use “Enter manually” below.</div>`);
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    S.scan.stream = stream; video.srcObject = stream; await video.play();
    const det = new BarcodeDetector({ formats: ['code_128', 'qr_code', 'ean_13', 'code_39', 'data_matrix'] });
    const tick = async () => {
      if (!S.scan || location.hash.indexOf('/scan/') < 0) return stopCamera();
      try { const codes = await det.detect(video); if (codes && codes.length) await onScan(codes[0].rawValue, 'scan'); } catch (e) {}
      S.scan._raf = requestAnimationFrame(tick);
    };
    tick();
  } catch (e) { toast('Camera unavailable — enter manually', true); }
}
function stopCamera() {
  if (S.scan && S.scan._raf) cancelAnimationFrame(S.scan._raf);
  if (S.scan && S.scan.stream) S.scan.stream.getTracks().forEach(t => t.stop());
}
function manualEntry() {
  const code = prompt('Enter the barcode number:'); if (code) onScan(code.trim(), 'manual');
}
// Native hardware scanner (Capacitor/ML-Kit). Falls back to manual on failure.
async function hardwareScan() {
  if (!(window.TOMNative && window.TOMNative.scanBarcode)) return manualEntry();
  try { const code = await window.TOMNative.scanBarcode(); if (code) onScan(String(code).trim(), 'scan'); else toast('No barcode detected'); }
  catch (e) { toast('Scanner unavailable — enter manually', true); }
}
let _scanLock = false;
async function onScan(barcode, entry) {
  if (_scanLock) return; _scanLock = true; setTimeout(() => _scanLock = false, 900);
  const { docket, phase, dropSeq } = S.scan;
  const pos = await currentPos().catch(() => null);
  const r = await mutate(`/jobs/${docket}/scan`, { phase, drop_seq: dropSeq, barcode, entry, lat: pos && pos.lat, lng: pos && pos.lng });
  if (r.queued) { toast('Scan queued'); return; }
  if (!r.ok) { toast('Scan failed', true); return; }
  const m = r.data.match;
  if (m === 'expected') { navigator.vibrate && navigator.vibrate(40); toast('✔ Parcel matched'); }
  else if (m === 'duplicate') toast('Already scanned');
  else if (m === 'wrong_drop') warnSheet('Wrong drop', `That parcel belongs to a different drop. Don't deliver it here.`);
  else if (m === 'unexpected') warnSheet('Extra parcel', `This barcode isn't on the expected list for this job.`);
  await loadJob(docket); renderChecklist();
}
async function confirmCollect() {
  const r = await mutate(`/jobs/${S.scan.docket}/status`, { action: 'collected' }, { idemKey: uuid() });
  stopCamera();
  if (r.ok || r.queued) { toast('Parcels on board'); await loadJob(S.scan.docket); go('#/job/' + S.scan.docket); }
  else toast('Scan all parcels first', true);
}

/* ── POD ── */
function podScreen(docket, dropSeq) {
  let sigData = null, drawing = false; const photoData = [];
  const wrap = el('<div></div>');
  wrap.appendChild(el(header('Proof of delivery', `Drop ${dropSeq}`)));
  const main = el(`<main>
    <label>Recipient name</label><input id="rcpt" type="text" placeholder="Who received it?" />
    <label>Signature</label><canvas class="sig" id="sig"></canvas>
    <button class="btn ghost sm" id="clr" style="margin-top:8px">Clear signature</button>
    <label>Photos (optional)</label>
    <input id="photo" type="file" accept="image/*" multiple />
    <div id="thumbs" class="pod-thumbs"></div>
    <label>Note (optional)</label><textarea id="note" placeholder="Anything to record?"></textarea>
  </main>`);
  wrap.appendChild(main);
  const bar = el(`<div class="fab-bar"><button class="btn ok" id="confirm">Confirm delivery</button></div>`);
  wrap.appendChild(bar);
  setTimeout(() => {
    const cv = $('#sig'); const ctx = cv.getContext('2d');
    const fit = () => { cv.width = cv.offsetWidth; cv.height = cv.offsetHeight; ctx.lineWidth = 2.5; ctx.lineCap = 'round'; ctx.strokeStyle = '#0E1623'; };
    fit();
    const pt = (e) => { const r = cv.getBoundingClientRect(); const t = e.touches ? e.touches[0] : e; return { x: t.clientX - r.left, y: t.clientY - r.top }; };
    const down = (e) => { drawing = true; const p = pt(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); e.preventDefault(); };
    const move = (e) => { if (!drawing) return; const p = pt(e); ctx.lineTo(p.x, p.y); ctx.stroke(); e.preventDefault(); };
    const up = () => { drawing = false; sigData = cv.toDataURL('image/png'); };
    cv.addEventListener('mousedown', down); cv.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
    cv.addEventListener('touchstart', down); cv.addEventListener('touchmove', move); cv.addEventListener('touchend', up);
    $('#clr').onclick = () => { ctx.clearRect(0, 0, cv.width, cv.height); sigData = null; };
    const renderThumbs = () => {
      const t = $('#thumbs'); if (!t) return;
      t.innerHTML = photoData.map((src, i) => `<div class="pod-thumb"><img src="${src}" alt=""/><button class="rm" data-i="${i}" aria-label="Remove">✕</button></div>`).join('');
      t.querySelectorAll('.rm').forEach(b => b.onclick = () => { photoData.splice(Number(b.dataset.i), 1); renderThumbs(); });
    };
    $('#photo').onchange = (e) => {
      [...e.target.files].forEach(f => { const fr = new FileReader(); fr.onload = () => { photoData.push(fr.result); renderThumbs(); }; fr.readAsDataURL(f); });
      e.target.value = '';   // allow re-picking the same file / adding more
    };
    $('#confirm').onclick = async (e) => {
      const rcpt = $('#rcpt').value.trim();
      if (!rcpt) return toast('Enter recipient name', true);
      if (!sigData) return toast('Capture a signature', true);
      e.target.disabled = true;
      const pos = await currentPos().catch(() => null);
      const r = await mutate(`/jobs/${docket}/pod`, {
        drop_seq: Number(dropSeq), recipient_name: rcpt, signature: sigData, photos: photoData.slice(),
        note: $('#note').value.trim(), lat: pos && pos.lat, lng: pos && pos.lng,
      }, { idemKey: uuid() });
      if (r.ok || r.queued) { toast(r.queued ? 'POD queued' : 'Delivered ✔'); await loadJob(docket);
        if (r.data && r.data.job_completed) { toast('Job complete 🎉'); go('#/run'); loadRun(); } else go('#/job/' + docket); }
      else {
        e.target.disabled = false;
        const code = r.data && r.data.error && r.data.error.code;
        toast(code === 'parcels_outstanding' ? "Scan this drop's parcels first" : 'Could not save POD', true);
      }
    };
  }, 0);
  return wrap;
}

/* ── failure ── */
function failScreen(docket, dropSeq) {
  const reasons = [['no_access', 'No access'], ['refused', 'Refused'], ['damaged', 'Damaged'], ['wrong_address', 'Wrong address'], ['other', 'Other']];
  let reason = null, photoData = null;
  const wrap = el('<div></div>');
  wrap.appendChild(el(header("Can't complete", `Drop ${dropSeq}`)));
  const main = el(`<main><label>Reason</label><div id="reasons"></div>
    <label>Photo (optional)</label><input id="photo" type="file" accept="image/*" capture="environment" />
    <img id="prev" class="photo-prev" hidden />
    <label>Note</label><textarea id="note" placeholder="What happened?"></textarea></main>`);
  wrap.appendChild(main);
  const rb = main.querySelector('#reasons');
  reasons.forEach(([k, lbl]) => { const b = el(`<button class="btn secondary sm" style="margin:4px 6px 4px 0;display:inline-flex;width:auto">${lbl}</button>`);
    b.onclick = () => { reason = k; [...rb.children].forEach(x => x.classList.add('secondary')); b.classList.remove('secondary'); }; rb.appendChild(b); });
  const bar = el(`<div class="fab-bar"><button class="btn danger" id="confirm">Report failure</button></div>`);
  wrap.appendChild(bar);
  setTimeout(() => {
    $('#photo').onchange = (e) => { const f = e.target.files[0]; if (!f) return; const fr = new FileReader();
      fr.onload = () => { photoData = fr.result; const p = $('#prev'); p.src = photoData; p.hidden = false; }; fr.readAsDataURL(f); };
    $('#confirm').onclick = async () => {
      if (!reason) return toast('Pick a reason', true);
      const r = await mutate(`/jobs/${docket}/fail`, { drop_seq: Number(dropSeq), reason_code: reason, note: $('#note').value.trim(), photo: photoData }, { idemKey: uuid() });
      if (r.ok || r.queued) { toast('Reported to ops'); await loadJob(docket); go('#/job/' + docket); }
      else toast('Could not report', true);
    };
  }, 0);
  return wrap;
}

/* ── messages ── */
/* Quick canned messages (driving-safe, one tap) — from the legacy app. */
const QUICK_MSGS = [
  { t: 'Heavy traffic', c: 'traffic' },
  { t: 'Urgent — please call me', c: 'urgent', confirm: false },
  { t: 'Where should I plot?', c: 'plot' },
  { t: 'EMERGENCY', c: 'emergency', danger: true, confirm: true },
  { t: 'Accident / vehicle breakdown', c: 'breakdown', danger: true, confirm: true },
  { t: 'I would like to change my start time — please call me', c: 'start_time' },
];

async function sendMessage(text, category, confirmFirst, danger) {
  const doSend = async () => {
    const r = await mutate('/messages', { text, category });
    if (r.ok || r.queued) { toast(category === 'emergency' ? 'Emergency sent to ops' : 'Sent to ops'); await loadMessages(); if (location.hash.startsWith('#/messages')) render(); }
    else toast('Could not send', true);
  };
  if (confirmFirst) {
    const bg = el(`<div class="sheet-bg"><div class="sheet">
      <div class="icon" style="font-size:30px">${danger ? '🚨' : '⚠️'}</div><h3>Send to ops?</h3>
      <p class="muted">"${esc(text)}"</p>
      <div class="btn-row" style="margin-top:14px"><button class="btn secondary" id="no">Cancel</button>
      <button class="btn ${danger ? 'danger' : ''}" id="yes">Send now</button></div></div></div>`);
    bg.querySelector('#no').onclick = () => bg.remove();
    bg.addEventListener('click', e => { if (e.target === bg) bg.remove(); });
    bg.querySelector('#yes').onclick = () => { bg.remove(); doSend(); };
    document.body.appendChild(bg);
  } else doSend();
}

function messagesScreen() {
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Inbox', 'Quick message ops')));
  const main = el('<main></main>');
  main.appendChild(el(`<div class="section-title" style="margin-top:0">Send a quick message</div>`));
  const grid = el(`<div class="stack" style="gap:10px"></div>`);
  QUICK_MSGS.forEach(q => {
    const b = el(`<button class="btn ${q.danger ? 'danger' : 'secondary'}" style="justify-content:flex-start;text-align:left">${q.danger ? '🚨 ' : ''}${esc(q.t)}</button>`);
    b.onclick = () => sendMessage(q.t, q.c, q.confirm, q.danger);
    grid.appendChild(b);
  });
  main.appendChild(grid);
  main.appendChild(el(`<div class="section-title">Other</div>
    <div class="btn-row"><input id="msg" type="text" placeholder="Type a message…" />
    <button class="btn sm" id="send">Send</button></div>`));
  if (S.messages.length) {
    main.appendChild(el(`<div class="section-title">Conversation</div>`));
    S.messages.slice().reverse().forEach(m => main.appendChild(el(
      `<div class="card" style="background:${m.direction === 'driver' ? 'var(--surface2)' : 'var(--surface)'};${m.category === 'emergency' ? 'border-color:var(--danger)' : ''}">
        <div class="tiny muted">${m.direction === 'driver' ? 'You' : 'Ops'} · ${esc((m.ts || '').slice(11, 16))}</div>
        <div>${esc(m.text)}</div></div>`)));
  }
  wrap.appendChild(main); wrap.appendChild(el(nav('messages')));
  setTimeout(() => { $('#send').onclick = async () => { const t = $('#msg').value.trim(); if (!t) return; await sendMessage(t, 'other'); $('#msg').value = ''; }; }, 0);
  return wrap;
}

/* ── job history ── */
function historyScreen() {
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Job history', 'Completed work')));
  const main = el('<main></main>');
  if (!S.history.length) main.appendChild(el(`<div class="empty"><div class="big">📋</div><div>No completed jobs yet.</div></div>`));
  S.history.forEach(j => main.appendChild(el(`<div class="card"><div class="row"><div class="stack">
    <div style="font-weight:700">${esc(j.account || 'Job')} <span class="mono tiny muted">${esc(j.docket_number)}</span></div>
    <div class="tiny muted">${esc(j.operational_date || '')} · ${esc(j.vehicle || '')}</div></div>
    <div class="stack" style="align-items:flex-end"><b>${gbp(j.driver_pay_final)}</b>
    <span class="pill ${j.status === 'COMPLETED' ? 'green' : 'red'}">${j.status === 'COMPLETED' ? 'Delivered' : esc(j.status)}</span></div></div></div>`)));
  wrap.appendChild(main); wrap.appendChild(el(nav('run')));
  return wrap;
}

/* ── shift control ── */
async function setDuty(status) {
  const r = await mutate('/status', { status });
  if (r.ok || r.queued) { S.dutyStatus = status; toast(status === 'going_home' ? 'Going home — winding-down jobs only' : 'Available'); render(); }
}
async function endShift() {
  const r = await mutate('/shift/end', {});
  if (r.ok || r.queued) {
    S.shift = null; S.dutyStatus = 'off';
    // Shift over → stop sharing location (keeps the consent promise: "only
    // while you work"), then flush any pings recorded during the shift —
    // the server still accepts those (their timestamps predate shift end).
    stopTracking();
    flushPings();
    toast('Shift ended'); go('#/home');
  }
}
function shiftBlock() {
  if (!S.shift) {
    const c = el(`<div class="card" style="border-color:var(--accent)"><div class="row"><div class="stack">
      <b>You're off shift</b><span class="muted small">Start a shift so dispatch can pre-allocate jobs to you.</span></div>
      <button class="btn sm" id="startsh">Start shift</button></div></div>`);
    c.querySelector('#startsh').onclick = () => go('#/shift-start');
    return c;
  }
  const rem = shiftRemaining(S.shift.planned_end);
  const c = el(`<div class="card"><div class="row"><div class="stack">
      <b>On shift${rem ? ` · ${rem.label} left` : ''}</b>
      <span class="muted small">Ends ${esc(S.shift.planned_end)}</span></div>
      <button class="btn ghost sm" id="endsh">End</button></div>
    <div class="btn-row" style="margin-top:12px">
      <button class="btn ${S.dutyStatus === 'available' ? 'ok' : 'secondary'} sm" id="av">● Available</button>
      <button class="btn ${S.dutyStatus === 'going_home' ? '' : 'secondary'} sm" id="gh">⌂ Going home</button>
    </div></div>`);
  c.querySelector('#endsh').onclick = endShift;
  c.querySelector('#av').onclick = () => setDuty('available');
  c.querySelector('#gh').onclick = () => setDuty('going_home');
  return c;
}

function shiftStartScreen() {
  const now = new Date(); const def = new Date(now.getTime() + 8 * 3600000);
  const hh = String(def.getHours()).padStart(2, '0'), mm = String(def.getMinutes()).padStart(2, '0');
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Start shift', 'End time')));
  const main = el(`<main>
    <div class="card"><p class="muted small" style="margin-top:0">To allocate pre-bookings to you, set the time your shift ends before you start.</p>
    <label>Shift end time</label>
    <input id="endtime" type="time" value="${hh}:${mm}" style="font-size:22px;text-align:center" />
    <div id="dur" class="muted small" style="text-align:center;margin-top:10px"></div>
    <div class="btn-row" style="margin-top:8px">
      ${[4, 6, 8, 10].map(h => `<button class="btn ghost sm preset" data-h="${h}">+${h}h</button>`).join('')}
    </div></div></main>`);
  wrap.appendChild(main);
  const bar = el(`<div class="fab-bar"><button class="btn" id="startbtn">Start shift</button></div>`);
  wrap.appendChild(bar);
  setTimeout(() => {
    const inp = $('#endtime');
    const dur = () => {
      const [h, m] = inp.value.split(':').map(Number); const e = new Date(); e.setHours(h, m, 0, 0);
      let mins = Math.round((e - new Date()) / 60000); if (mins < 0) mins += 1440;
      $('#dur').textContent = `Duration: ${Math.floor(mins / 60)} hr ${mins % 60} min`;
    };
    inp.oninput = dur; dur();
    document.querySelectorAll('.preset').forEach(b => b.onclick = () => {
      const t = new Date(new Date().getTime() + Number(b.dataset.h) * 3600000);
      inp.value = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0'); dur();
    });
    $('#startbtn').onclick = async () => {
      const r = await mutate('/shift/start', { planned_end: inp.value });
      if (r.ok || r.queued) { toast('Shift started'); await loadShift(); go('#/home'); } else toast('Could not start shift', true);
    };
  }, 0);
  return wrap;
}

/* ── home dashboard ── */
function homeScreen() {
  const hd = S.home || {}; const d = hd.driver || S.driver || {};
  const t = hd.today || { earned: '0', on_run: '0', jobs_done: 0, jobs_total: 0 };
  const perf = hd.performance || {};
  const hr = new Date().getHours();
  const greet = hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
  const wrap = el('<div></div>');
  wrap.appendChild(el(header(`${greet}, ${(d.name || '').split(' ')[0]}`, new Date().toDateString())));
  const main = el('<main></main>');
  main.appendChild(shiftBlock());
  main.appendChild(el(`<div class="card" style="background:var(--accent-d)">
    <div class="muted small" style="color:#cfe2fb">Earned today</div>
    <div style="font-size:36px;font-weight:800;color:#fff">${gbp(t.earned)}</div>
    <div class="row" style="margin-top:4px"><span class="small" style="color:#cfe2fb">${gbp(t.on_run)} on the run · ${t.jobs_done}/${t.jobs_total} jobs done</span></div>
  </div>`));
  if (hd.next_stop) {
    const ns = hd.next_stop;
    const c = el(`<div class="card tap"><div class="row"><div class="stack">
      <div class="muted small">Next stop</div><div style="font-weight:700">${esc(ns.account || '')} · ${esc(ns.where || '')}</div></div>
      ${deadlinePill(ns.deadline)}</div>
      <button class="btn" style="margin-top:12px">Continue run →</button></div>`);
    c.onclick = () => go('#/run'); main.appendChild(c);
  } else {
    main.appendChild(el(`<div class="card"><div class="muted">No active jobs right now.</div></div>`));
  }
  // quick tiles
  main.appendChild(el(`<div class="section-title">Quick access</div>`));
  const tiles = [['💷', 'Earnings', '#/earnings'], ['🧾', 'Statements', '#/statements'],
                 ['💳', 'Expenses', '#/expenses'], ['📊', 'Tax centre', '#/tax']];
  const grid = el(`<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"></div>`);
  tiles.forEach(([ic, lbl, h]) => { const c = el(`<div class="card tap" style="margin:0"><div style="font-size:22px">${ic}</div><div style="font-weight:600;margin-top:4px">${lbl}</div></div>`); c.onclick = () => go(h); grid.appendChild(c); });
  main.appendChild(grid);
  // performance
  main.appendChild(el(`<div class="section-title">Your performance</div>
    <div class="card"><div class="row">
      <div class="stack"><div style="font-size:24px;font-weight:800">${(perf.rating || 5).toFixed(1)} ★</div><div class="tiny muted">Rating</div></div>
      <div class="stack"><div style="font-size:24px;font-weight:800">${perf.on_time_pct || 100}%</div><div class="tiny muted">On time</div></div>
      <div class="stack"><div style="font-size:24px;font-weight:800">${perf.completion_pct || 100}%</div><div class="tiny muted">Completion</div></div>
      <div class="stack"><div style="font-size:24px;font-weight:800">${perf.jobs_completed || 0}</div><div class="tiny muted">Jobs</div></div>
    </div></div>`));
  if (S.messages && S.messages.find(m => m.direction === 'ops')) {
    const m = S.messages.find(x => x.direction === 'ops');
    main.appendChild(el(`<div class="card" style="border-color:var(--accent)"><div class="tiny muted">📣 From ops</div><div>${esc(m.text)}</div></div>`));
  }
  wrap.appendChild(main); wrap.appendChild(el(nav('home')));
  return wrap;
}

/* ── earnings ── */
function earningsScreen() {
  const e = S.earnings;
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Earnings', 'Pay & balance')));
  const main = el('<main></main>');
  if (!e) { main.appendChild(el('<div class="empty">Loading…</div>')); wrap.appendChild(main); wrap.appendChild(el(nav('earnings'))); return wrap; }
  const p = e.period;
  if (p) {
    const stCls = p.status === 'paid' ? 'green' : 'amber';
    main.appendChild(el(`<div class="card">
      <div class="row"><div class="muted small">${esc(p.label)} · ${esc(p.reference)}</div><span class="pill ${stCls}">${p.status}</span></div>
      <div style="font-size:34px;font-weight:800;margin-top:4px">${gbp(p.net)}</div>
      <div class="muted small">Net this period · gross ${gbp(p.gross)} · expected ${esc(p.projected_pay_date || 'TBC')}</div>
    </div>`));
  }
  // instant pay
  main.appendChild(el(`<div class="card" style="border-color:var(--ok)"><div class="row">
    <div class="stack"><b>${gbp(e.totals.available_balance)} available now</b>
      <span class="muted small">Get paid before payday (1.5% fee)</span></div>
    <button class="btn ok sm" id="payout">Cash out</button></div></div>`));
  // week chart
  const maxv = Math.max(1, ...e.week_chart.map(d => Number(d.amount)));
  main.appendChild(el(`<div class="section-title">This week</div>
    <div class="card"><div style="display:flex;align-items:flex-end;gap:8px;height:120px">
      ${e.week_chart.map(d => `<div class="stack" style="flex:1;align-items:center;justify-content:flex-end;gap:4px">
        <div class="tiny muted">${Number(d.amount) ? '£' + Math.round(Number(d.amount)) : ''}</div>
        <div style="width:100%;background:${Number(d.amount) ? 'var(--accent)' : 'var(--surface2)'};height:${Math.max(4, Number(d.amount) / maxv * 90)}px;border-radius:6px"></div>
        <div class="tiny muted">${d.dow}</div></div>`).join('')}
    </div></div>`));
  // breakdown
  const b = e.breakdown;
  main.appendChild(el(`<div class="card">
    <div class="kv"><span class="k">Jobs</span><span>${gbp(b.jobs)}</span></div>
    <div class="kv"><span class="k">Bonuses</span><span>${gbp(b.bonus)}</span></div>
    <div class="kv"><span class="k">Expense reimbursements</span><span>${gbp(b.expenses)}</span></div>
    <div class="kv"><span class="k">Deductions</span><span>-${gbp(b.deductions)}</span></div>
  </div>`));
  // totals
  main.appendChild(el(`<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
    <div class="card" style="margin:0"><div class="tiny muted">Paid this year</div><div style="font-size:20px;font-weight:700">${gbp(e.totals.paid_ytd)}</div></div>
    <div class="card" style="margin:0"><div class="tiny muted">Gross YTD</div><div style="font-size:20px;font-weight:700">${gbp(e.totals.ytd_gross)}</div></div>
  </div>`));
  // today's jobs
  if (e.today_jobs && e.today_jobs.length) {
    main.appendChild(el(`<div class="section-title">Today's jobs</div>`));
    e.today_jobs.forEach(j => main.appendChild(el(`<div class="card"><div class="row">
      <div class="stack"><div style="font-weight:700">${esc(j.account)} <span class="mono tiny muted">${esc(j.docket_number)}</span></div>
      <div class="tiny muted">base ${gbp(j.components.base)} · waiting ${gbp(j.components.waiting)} · extras ${gbp(j.components.extras)}</div></div>
      <div class="stack" style="align-items:flex-end"><b>${gbp(j.total)}</b><span class="pill ${j.status === 'COMPLETED' ? 'green' : 'grey'}">${j.status === 'COMPLETED' ? 'Done' : 'On run'}</span></div></div></div>`)));
  }
  const sb = el(`<div class="btn-row" style="margin-top:8px">
    <button class="btn secondary" id="stmts">🧾 Statements</button>
    <button class="btn secondary" id="tax">📊 Tax centre</button></div>`);
  main.appendChild(sb);
  wrap.appendChild(main); wrap.appendChild(el(nav('earnings')));
  setTimeout(() => {
    $('#payout').onclick = payoutSheet;
    $('#stmts').onclick = () => go('#/statements');
    $('#tax').onclick = () => go('#/tax');
  }, 0);
  return wrap;
}

function payoutSheet() {
  const avail = S.earnings ? S.earnings.totals.available_balance : '0';
  const bg = el(`<div class="sheet-bg"><div class="sheet">
    <h3>Cash out early</h3>
    <p class="muted small">${gbp(avail)} available. A 1.5% fee applies; money lands within ~30 minutes.</p>
    <label>Amount (£)</label><input id="amt" type="text" inputmode="decimal" placeholder="0.00" value="${avail}" />
    <div class="btn-row" style="margin-top:14px"><button class="btn secondary" id="cancel">Cancel</button>
      <button class="btn ok" id="go">Request payout</button></div></div></div>`);
  bg.querySelector('#cancel').onclick = () => bg.remove();
  bg.addEventListener('click', e => { if (e.target === bg) bg.remove(); });
  bg.querySelector('#go').onclick = async () => {
    const amount = bg.querySelector('#amt').value.trim();
    const r = await mutate('/payout', { amount });
    if (r.ok) { bg.remove(); toast(`Payout requested — ${gbp(r.data.net)} after fee`); loadEarnings(); }
    else { const code = r.data && r.data.error && r.data.error.code; toast(code === 'exceeds_available' ? 'Amount exceeds balance' : 'Could not request', true); }
  };
  document.body.appendChild(bg);
}

/* ── statements ── */
function stmtPill(s) { return s === 'paid' ? '<span class="pill green">Paid</span>' : s === 'processing' ? '<span class="pill amber">Processing</span>' : `<span class="pill grey">${s}</span>`; }

function statementsScreen() {
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Statements', 'Pay history')));
  const main = el('<main></main>');
  if (!S.statements.length) main.appendChild(el(`<div class="empty"><div class="big">🧾</div><div>No statements yet.</div></div>`));
  S.statements.forEach(s => {
    const c = el(`<div class="card tap"><div class="row"><div class="stack">
      <div style="font-weight:700">${esc(s.period_label)}</div>
      <div class="muted small mono">${esc(s.reference)}${s.paid_at ? ' · paid ' + s.paid_at.slice(0, 10) : ''}</div></div>
      <div class="stack" style="align-items:flex-end"><b>${gbp(s.net)}</b>${stmtPill(s.status)}</div></div></div>`);
    c.onclick = () => go('#/statement/' + s.statement_id); main.appendChild(c);
  });
  wrap.appendChild(main); wrap.appendChild(el(nav('earnings')));
  return wrap;
}

function statementScreen(sid) {
  const s = S.statement;
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Statement', s ? s.period_label : '')));
  const main = el('<main></main>');
  if (!s) { main.appendChild(el(`<div class="empty"><div class="big">⚠️</div><div>Statement not found.</div></div>`)); main.appendChild(backBtn()); wrap.appendChild(main); return wrap; }
  main.appendChild(el(`<div class="card"><div class="row"><div class="muted small mono">${esc(s.reference)}</div>${stmtPill(s.status)}</div>
    <div style="font-size:30px;font-weight:800;margin-top:6px">${gbp(s.net)}</div>
    <div class="muted small">${esc(s.period_start)} → ${esc(s.period_end)} (${esc(s.frequency)})${s.paid_at ? ' · paid ' + s.paid_at.slice(0, 10) : ''}</div></div>`));
  main.appendChild(el(`<div class="section-title">Lines</div>`));
  const list = el('<div class="card"></div>');
  s.lines.forEach(l => list.appendChild(el(`<div class="kv" style="border-bottom:1px solid var(--line)">
    <span class="k">${(l.line_date || '').slice(0, 10)} · ${esc(l.description || l.type)}</span>
    <span style="${Number(l.amount) < 0 ? 'color:var(--danger)' : ''}">${gbp(l.amount)}</span></div>`)));
  main.appendChild(list);
  main.appendChild(el(`<div class="card">
    <div class="kv"><span class="k">Gross</span><span>${gbp(s.gross)}</span></div>
    <div class="kv"><span class="k">Deductions</span><span>-${gbp(s.deductions)}</span></div>
    ${Number(s.vat) ? `<div class="kv"><span class="k">VAT</span><span>${gbp(s.vat)}</span></div>` : ''}
    <div class="kv" style="border-top:1px solid var(--line);padding-top:10px"><span class="k" style="font-weight:700;color:var(--text)">Net pay</span><span style="font-weight:800;font-size:18px">${gbp(s.net)}</span></div>
  </div>`));
  wrap.appendChild(main);
  const bar = el(`<div class="fab-bar"><button class="btn" id="dl">⬇︎ Download PDF</button></div>`);
  bar.querySelector('#dl').onclick = () => downloadStatement(sid, s.reference);
  wrap.appendChild(bar);
  return wrap;
}

async function downloadStatement(sid, ref) {
  toast('Preparing PDF…');
  try {
    const res = await fetch(API + '/statements/' + sid + '/pdf', { headers: { 'Authorization': 'Bearer ' + S.token } });
    if (!res.ok) throw new Error('bad');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `XtraMile_Statement_${(ref || sid).replace(/\s+/g, '_')}.pdf`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('Downloaded');
  } catch (e) { toast('Could not download', true); }
}

/* ── tax centre ── */
function taxScreen() {
  const t = S.tax;
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Tax centre', 'Year to date')));
  const main = el('<main></main>');
  if (!t) { main.appendChild(el('<div class="empty">Loading…</div>')); }
  else {
    main.appendChild(el(`<div class="card"><div class="kv"><span class="k">Gross earnings (YTD)</span><span><b>${gbp(t.ytd_gross)}</b></span></div>
      <div class="kv"><span class="k">Business mileage</span><span>${t.ytd_miles.toLocaleString()} mi</span></div>
      <div class="kv"><span class="k">Mileage allowance @ ${t.mileage_rate}/mi</span><span>${gbp(t.mileage_allowance)}</span></div>
      <div class="kv"><span class="k">Allowable expenses</span><span>${gbp(t.allowable_expenses)}</span></div>
      <div class="kv" style="border-top:1px solid var(--line);padding-top:10px"><span class="k">Estimated taxable</span><span><b>${gbp(t.estimated_taxable)}</b></span></div></div>`));
    main.appendChild(el(`<div class="card" style="border-color:var(--warn)">
      <div class="stack"><b>Set aside ${gbp(t.suggested_set_aside)}</b>
      <span class="muted small">~${t.suggested_set_aside_pct}% of estimated taxable income for your tax bill.${t.vat_registered ? ' You are VAT registered.' : ''}</span></div></div>`));
    main.appendChild(el(`<p class="muted tiny" style="text-align:center">${esc(t.note)}</p>`));
  }
  wrap.appendChild(main); wrap.appendChild(el(nav('earnings')));
  return wrap;
}

/* ── expenses ── */
function expensesScreen() {
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Expenses', 'Log & track')));
  const main = el('<main></main>');
  let receiptData = null;
  main.appendChild(el(`<div class="card"><div class="section-title" style="margin-top:0">Add expense</div>
    <label>Type</label>
    <select id="etype" style="width:100%;min-height:var(--tap);background:var(--surface2);border:1px solid var(--line);border-radius:var(--radius);color:var(--text);padding:0 12px">
      <option value="fuel">Fuel</option><option value="parking">Parking</option><option value="toll">Toll / congestion</option><option value="other">Other</option></select>
    <label>Amount (£)</label><input id="eamt" type="text" inputmode="decimal" placeholder="0.00" />
    <label>Note</label><input id="enote" type="text" placeholder="Where / what" />
    <label>Receipt (optional)</label><input id="ephoto" type="file" accept="image/*" capture="environment" />
    <button class="btn" id="eadd" style="margin-top:14px">Submit expense</button></div>`));
  main.appendChild(el(`<div class="section-title">Recent</div>`));
  if (!S.expenses.length) main.appendChild(el(`<div class="muted small" style="padding:8px">No expenses yet.</div>`));
  S.expenses.forEach(x => main.appendChild(el(`<div class="card"><div class="row">
    <div class="stack"><div style="font-weight:600;text-transform:capitalize">${esc(x.type)}</div>
    <div class="tiny muted">${(x.exp_date || '').slice(0, 10)}${x.note ? ' · ' + esc(x.note) : ''}</div></div>
    <div class="stack" style="align-items:flex-end"><b>${gbp(x.amount)}</b>
    <span class="pill ${x.status === 'reimbursed' ? 'green' : x.status === 'approved' ? 'blue' : 'grey'}">${esc(x.status)}</span></div></div></div>`)));
  wrap.appendChild(main); wrap.appendChild(el(nav('earnings')));
  setTimeout(() => {
    $('#ephoto').onchange = (e) => { const f = e.target.files[0]; if (!f) return; const fr = new FileReader(); fr.onload = () => receiptData = fr.result; fr.readAsDataURL(f); };
    $('#eadd').onclick = async () => {
      const amount = $('#eamt').value.trim(); if (!amount) return toast('Enter an amount', true);
      const r = await mutate('/expenses', { type: $('#etype').value, amount, note: $('#enote').value.trim(), receipt: receiptData });
      if (r.ok) { toast('Expense submitted'); await loadExpenses(); render(); } else toast('Could not submit', true);
    };
  }, 0);
  return wrap;
}

/* ── settings hub (Me) ── */
function meScreen() {
  const d = S.driver || {};
  const wrap = el('<div></div>'); wrap.appendChild(el(header('You', d.name || '')));
  const main = el('<main></main>');
  const avatarUrl = (S.profile && S.profile.avatar_url) || d.avatar_url;
  const avatar = avatarUrl
    ? `<div class="seqnum" style="width:46px;height:46px;overflow:hidden;padding:0"><img src="${esc(mediaUrl(avatarUrl))}" alt="" style="width:100%;height:100%;object-fit:cover"/></div>`
    : `<div class="seqnum" style="width:46px;height:46px;font-size:18px">${esc((d.name || '?').split(' ').map(x => x[0]).join('').slice(0, 2))}</div>`;
  main.appendChild(el(`<div class="card"><div class="row"><div style="display:flex;gap:12px;align-items:center">
    ${avatar}
    <div class="stack"><div style="font-weight:700;font-size:17px">${esc(d.name || '')}</div>
    <div class="muted small mono">${esc(d.callsign || '')} · ${d.is_subcontracted ? 'Subcontractor' : 'Owned fleet'}</div></div></div></div></div>`));
  // tracking toggle
  main.appendChild(el(`<div class="card"><div class="row"><div class="stack"><b>Location sharing</b>
    <span class="muted small">On while working — live ETAs for ops.</span></div>
    <button class="btn ${S.tracking ? 'ok' : 'secondary'} sm" id="trk">${S.tracking ? 'On' : 'Off'}</button></div></div>`));
  const menu = [
    ['👤', 'Personal details', '#/profile'],
    ['💳', 'Payment & tax details', '#/payment'],
    ['🧾', 'Statements', '#/statements'],
    ['💷', 'Earnings', '#/earnings'],
    ['📊', 'Tax centre', '#/tax'],
    ['💳', 'Expenses', '#/expenses'],
    ['📋', 'Job history', '#/history'],
    ['🎨', 'App & display', '#/notifications'],
    ['🔒', 'Security', '#/security'],
    ['🔏', 'Privacy & data', '#/privacy'],
    ['📄', 'Documents', '#/documents'],
    ['❓', 'Help & support', '#/help'],
    ['ℹ️', 'About', '#/about'],
  ];
  const list = el('<div class="card" style="padding:4px 14px"></div>');
  menu.forEach(([ic, lbl, h]) => { const row = el(`<div class="kv tap" style="border-bottom:1px solid var(--line);cursor:pointer"><span class="k">${ic} ${lbl}</span><span class="muted">›</span></div>`); row.onclick = () => go(h); list.appendChild(row); });
  main.appendChild(list);
  main.appendChild(el(`<button class="btn danger" id="logout" style="margin-top:8px">Sign out</button>
    <p class="muted tiny" style="text-align:center;margin-top:16px">TOM Driver · v${esc(S.appVersion || '1.0.0')}</p>`));
  wrap.appendChild(main); wrap.appendChild(el(nav('me')));
  setTimeout(() => {
    $('#trk').onclick = () => { S.tracking ? disableTracking() : enableTracking(); };
    $('#logout').onclick = async () => { try { await api('/auth/logout', { method: 'POST' }); } catch (e) {} logoutLocal(); go('#/login'); };
  }, 0);
  return wrap;
}

/* ── profile / vehicle photo upload ── */
function uploadProfilePhoto(kind, file) {
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) return toast('Image too large (max 8MB)', true);
  const fr = new FileReader();
  fr.onload = async () => {
    toast('Uploading…');
    const r = await mutate('/profile/photo', { kind, image: fr.result });
    if (r.ok) { toast(kind === 'avatar' ? 'Profile photo updated' : 'Vehicle photo updated'); await loadProfile(); render(); }
    else toast('Upload failed', true);
  };
  fr.readAsDataURL(file);
}

/* ── personal details (editable) ── */
function profileScreen() {
  const p = S.profile || {};
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Personal details', 'Tap to edit')));
  const main = el('<main></main>');
  const initials = esc((p.name || '?').split(' ').map(x => x[0]).join('').slice(0, 2));
  const photos = el(`<div class="card"><div class="section-title" style="margin-top:0">Photos</div>
    <div class="photo-grid">
      <div class="photo-pick">
        <div class="avatar-lg">${p.avatar_url ? `<img src="${esc(mediaUrl(p.avatar_url))}" alt="Profile photo"/>` : initials}</div>
        <label class="btn ghost sm">${p.avatar_url ? 'Change' : 'Add'} profile<input id="av_in" type="file" accept="image/*" hidden /></label>
      </div>
      <div class="photo-pick">
        <div class="veh-lg">${p.vehicle_photo_url ? `<img src="${esc(mediaUrl(p.vehicle_photo_url))}" alt="Vehicle photo"/>` : '🚐'}</div>
        <label class="btn ghost sm">${p.vehicle_photo_url ? 'Change' : 'Add'} vehicle<input id="vh_in" type="file" accept="image/*" hidden /></label>
      </div>
    </div></div>`);
  photos.querySelector('#av_in').onchange = (e) => uploadProfilePhoto('avatar', e.target.files[0]);
  photos.querySelector('#vh_in').onchange = (e) => uploadProfilePhoto('vehicle', e.target.files[0]);
  main.appendChild(photos);
  if (S.pending && S.pending.length) main.appendChild(el(`<div class="card" style="border-color:var(--warn)"><div class="tiny">⏳ ${S.pending.length} change(s) awaiting ops review</div></div>`));
  const field = (id, label, val, ro) => `<label>${label}</label><input id="${id}" type="text" value="${esc(val || '')}" ${ro ? 'readonly style="opacity:.6"' : ''} />`;
  main.appendChild(el(`<div class="card">
    ${field('f_name', 'Name', p.name, true)}
    ${field('f_phone', 'Phone', p.phone)}
    ${field('f_email', 'Email', p.email)}
    ${field('f_address', 'Address', p.address)}
    ${field('f_dob', 'Date of birth', p.dob, true)}
  </div>
  <div class="card"><div class="section-title" style="margin-top:0">Emergency contact</div>
    ${field('f_en', 'Name', p.emergency_name)}
    ${field('f_ep', 'Phone', p.emergency_phone)}
  </div>
  <div class="card"><div class="section-title" style="margin-top:0">Vehicle</div>
    ${field('f_vr', 'Registration', p.vehicle_reg)}
    ${field('f_vmk', 'Make', p.vehicle_make)}
    ${field('f_vmd', 'Model', p.vehicle_model)}
  </div>`));
  wrap.appendChild(main);
  const bar = el(`<div class="fab-bar"><button class="btn" id="save">Save changes</button></div>`);
  bar.querySelector('#save').onclick = async () => {
    const fields = {
      phone: $('#f_phone').value.trim(), email: $('#f_email').value.trim(), address: $('#f_address').value.trim(),
      emergency_name: $('#f_en').value.trim(), emergency_phone: $('#f_ep').value.trim(),
      vehicle_reg: $('#f_vr').value.trim(), vehicle_make: $('#f_vmk').value.trim(), vehicle_model: $('#f_vmd').value.trim(),
    };
    const r = await mutate('/profile', { fields });
    if (r.ok) { toast('Details saved'); await loadProfile(); render(); } else toast('Could not save', true);
  };
  wrap.appendChild(bar);
  return wrap;
}

/* ── payment & tax (sensitive → review) ── */
function paymentScreen() {
  const p = S.profile || {};
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Payment & tax', 'Reviewed by ops')));
  const main = el('<main></main>');
  main.appendChild(el(`<div class="card" style="border-color:var(--accent)"><div class="tiny muted">🔒 Bank and tax changes are checked by the accounts team before they go live.</div></div>`));
  if (S.pending && S.pending.length) main.appendChild(el(`<div class="card" style="border-color:var(--warn)">${S.pending.map(x => `<div class="tiny">⏳ ${esc(x.field)} → ${esc(x.new_value)} (pending)</div>`).join('')}</div>`));
  const field = (id, label, val) => `<label>${label}</label><input id="${id}" type="text" value="${esc(val || '')}" />`;
  main.appendChild(el(`<div class="card"><div class="section-title" style="margin-top:0">Bank</div>
    <div class="kv"><span class="k">Status</span><span class="pill ${p.bank_status === 'verified' ? 'green' : 'amber'}">${esc(p.bank_status || 'unverified')}</span></div>
    ${field('b_name', 'Account name', p.bank_account_name)}
    ${field('b_sort', 'Sort code', p.bank_sort_code)}
    ${field('b_acc', 'Account number', p.bank_account_number)}
  </div>
  <div class="card"><div class="section-title" style="margin-top:0">Tax</div>
    <div class="kv"><span class="k">Pay cycle</span><span>${esc(p.pay_cycle || 'weekly')}</span></div>
    ${field('t_utr', 'UTR / company ref', p.utr_or_company_ref)}
    <label>VAT registered</label>
    <select id="t_vatreg" style="width:100%;min-height:var(--tap);background:var(--surface2);border:1px solid var(--line);border-radius:var(--radius);color:var(--text);padding:0 12px">
      <option value="0" ${!p.vat_registered ? 'selected' : ''}>No</option><option value="1" ${p.vat_registered ? 'selected' : ''}>Yes</option></select>
    ${field('t_vatno', 'VAT number', p.vat_number)}
  </div>`));
  wrap.appendChild(main);
  const bar = el(`<div class="fab-bar"><button class="btn" id="save">Submit for review</button></div>`);
  bar.querySelector('#save').onclick = async () => {
    const fields = {
      bank_account_name: $('#b_name').value.trim(), bank_sort_code: $('#b_sort').value.trim(), bank_account_number: $('#b_acc').value.trim(),
      utr_or_company_ref: $('#t_utr').value.trim(), vat_registered: Number($('#t_vatreg').value), vat_number: $('#t_vatno').value.trim(),
    };
    const r = await mutate('/profile', { fields });
    if (r.ok) { toast(r.data.pending && r.data.pending.length ? 'Submitted for review' : 'Saved'); await loadProfile(); render(); } else toast('Could not submit', true);
  };
  wrap.appendChild(bar);
  return wrap;
}

/* ── app & display settings ── */
function notificationsScreen() {
  const p = S.profile || {};
  const sel = `width:100%;min-height:var(--tap);background:var(--surface2);border:1px solid var(--line);border-radius:var(--radius);color:var(--text);padding:0 12px`;
  const wrap = el('<div></div>'); wrap.appendChild(el(header('App & display', 'Preferences')));
  const main = el('<main></main>');
  // Theme
  main.appendChild(el(`<div class="section-title" style="margin-top:0">Theme</div>`));
  const themeRow = el(`<div class="btn-row"></div>`);
  ['day', 'night', 'auto'].forEach(tm => {
    const cur = (p.theme || localStorage.getItem(LS_THEME) || 'night') === tm;
    const b = el(`<button class="btn ${cur ? '' : 'secondary'} sm" style="text-transform:capitalize">${tm}</button>`);
    b.onclick = async () => { applyTheme(tm); await mutate('/profile', { fields: { theme: tm } }); await loadProfile(); render(); };
    themeRow.appendChild(b);
  });
  main.appendChild(themeRow);
  // Map app
  main.appendChild(el(`<div class="section-title">Navigation app</div>
    <select id="navapp" style="${sel}">
      <option value="google" ${p.nav_app === 'google' ? 'selected' : ''}>Google Maps</option>
      <option value="android" ${p.nav_app === 'android' ? 'selected' : ''}>Android native</option>
      <option value="waze" ${p.nav_app === 'waze' ? 'selected' : ''}>Waze</option>
      <option value="herewego" ${p.nav_app === 'herewego' ? 'selected' : ''}>HERE WeGo</option></select>`));
  // Notifications + sound
  main.appendChild(el(`<div class="section-title">Notifications</div>`));
  const toggle = (key, label, desc, defOn) => {
    const on = p[key] == null ? defOn : !!p[key];
    const c = el(`<div class="card"><div class="row"><div class="stack"><b>${label}</b><span class="muted small">${desc}</span></div>
      <button class="btn ${on ? 'ok' : 'secondary'} sm">${on ? 'On' : 'Off'}</button></div></div>`);
    c.querySelector('button').onclick = async (e) => {
      const nv = e.target.textContent === 'On' ? 0 : 1; const f = {}; f[key] = nv;
      if (key === 'sound_alert_conn') localStorage.setItem(LS_SOUND, nv ? '1' : '0');
      const r = await mutate('/profile', { fields: f }); if (r.ok || r.queued) { await loadProfile(); render(); }
    };
    return c;
  };
  main.appendChild(toggle('notify_jobs', 'New jobs & route changes', 'When ops sends or reorders work.', true));
  main.appendChild(toggle('notify_pay', 'Payments & statements', 'When a statement is paid or issued.', true));
  main.appendChild(toggle('notify_msgs', 'Messages from ops', 'Two-way operational messages.', true));
  main.appendChild(toggle('sound_alert_conn', 'Sound alert for connection lost', 'Beep if you drop offline mid-shift.', false));
  main.appendChild(el(`<div class="card"><div class="row"><div class="stack"><b>Test notification</b>
    <span class="muted small">Check this device receives push alerts.</span></div>
    <button class="btn secondary sm" id="pushtest">Send</button></div></div>`));
  wrap.appendChild(main); wrap.appendChild(el(nav('me')));
  setTimeout(() => {
    const s = $('#navapp'); if (s) s.onchange = async () => { await mutate('/profile', { fields: { nav_app: s.value } }); await loadProfile(); toast('Saved'); };
    const pt = $('#pushtest'); if (pt) pt.onclick = async () => {
      const r = await mutate('/push/test', {});
      if (!(r.ok || r.queued)) return toast('Could not send', true);
      const d = r.data || {};
      if (!d.tokens) toast('No device registered — install the native app', true);
      else if (d.configured) toast('Test notification sent');
      else toast('Queued — push service not configured yet');
    };
  }, 0);
  return wrap;
}

/* ── security ── */
function securityScreen() {
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Security', 'Password')));
  const p = S.profile || {};
  const bio = !!p.biometric;
  const main = el(`<main><div class="card">
    <label>Current password</label><input id="cur" type="password" autocomplete="current-password" />
    <label>New password</label><input id="new" type="password" autocomplete="new-password" placeholder="at least 6 characters" />
    <label>Confirm new password</label><input id="cnf" type="password" autocomplete="new-password" />
    <button class="btn" id="chg" style="margin-top:14px">Change password</button>
  </div>
  <div class="card"><div class="row"><div class="stack"><b>Fingerprint login</b>
    <span class="muted small">Unlock with your fingerprint (native app).</span></div>
    <button class="btn ${bio ? 'ok' : 'secondary'} sm" id="bio">${bio ? 'On' : 'Off'}</button></div></div>
  <div class="card"><div class="stack"><b>Device</b><span class="muted small">One active device per driver. Signing in elsewhere signs this out.</span></div></div></main>`);
  wrap.appendChild(main); wrap.appendChild(el(nav('me')));
  setTimeout(() => {
    $('#bio').onclick = async (e) => { const nv = e.target.textContent === 'On' ? 0 : 1; await mutate('/profile', { fields: { biometric: nv } }); await loadProfile(); render(); toast(nv ? 'Fingerprint login on' : 'Off'); };
    $('#chg').onclick = async () => {
      const cur = $('#cur').value, nw = $('#new').value, cnf = $('#cnf').value;
      if (nw.length < 6) return toast('New password too short', true);
      if (nw !== cnf) return toast('Passwords do not match', true);
      const r = await api('/profile/password', { method: 'POST', body: { current: cur, new: nw } });
      if (r.ok) toast('Password changed'); else toast(r.data && r.data.error ? r.data.error.message : 'Failed', true);
      $('#cur').value = $('#new').value = $('#cnf').value = '';
    };
  }, 0);
  return wrap;
}

/* ── privacy & data (GDPR self-service) ── */
function privacyScreen() {
  const p = S.profile || {};
  const consent = p.location_consent_at;
  const wrap = el('<div></div>'); wrap.appendChild(el(header('Privacy & data', 'Your data rights')));
  const main = el('<main></main>');
  main.appendChild(el(`<div class="card"><div class="stack">
    <b>What we collect</b>
    <div class="muted small">Your account and vehicle details, the jobs you carry, proof of delivery (signature/photo), and — only while you're on shift and have agreed — your location.</div>
    <div class="muted small">Location is used to route jobs and give customers ETAs. Proof-of-delivery images are access-controlled and shared only with the relevant customer and ops.</div>
  </div></div>`));
  main.appendChild(el(`<div class="card"><div class="row"><div class="stack"><b>Location sharing</b>
    <span class="muted small">${consent ? 'Consented ' + new Date(consent).toLocaleDateString() : 'Not currently shared'}</span></div>
    <span class="pill ${consent ? 'green' : 'grey'}">${consent ? 'On' : 'Off'}</span></div></div>`));
  main.appendChild(el('<div class="section-title">Your rights</div>'));
  const card = el(`<div class="card"><div class="stack">
    <button class="btn secondary" id="dl">⬇️ Request a copy of my data</button>
    <button class="btn ghost" id="er" style="color:var(--danger);border-color:var(--danger)">🗑️ Request account erasure</button>
    <span class="muted tiny">Requests go to the operations team, who will action them in line with UK GDPR.</span>
  </div></div>`);
  main.appendChild(card);
  main.appendChild(backBtn());
  wrap.appendChild(main); wrap.appendChild(el(nav('me')));
  setTimeout(() => {
    $('#dl').onclick = async () => { const r = await mutate('/account/data-request', { kind: 'access' }); toast(r.ok || r.queued ? 'Data request sent to ops' : 'Could not send', !(r.ok || r.queued)); };
    $('#er').onclick = async () => {
      if (!confirm('Request erasure of your account and personal data? Ops will contact you to confirm.')) return;
      const r = await mutate('/account/data-request', { kind: 'erasure' }); toast(r.ok || r.queued ? 'Erasure request sent to ops' : 'Could not send', !(r.ok || r.queued));
    };
  }, 0);
  return wrap;
}

/* ── about / version / legal ── */
function aboutScreen() {
  const wrap = el('<div></div>'); wrap.appendChild(el(header('About', 'TOM Driver')));
  const main = el('<main></main>');
  main.appendChild(el(`<div class="card"><div class="stack" style="align-items:center;text-align:center;gap:8px;padding:8px">
    <img src="/icons/icon-192.png" alt="" style="width:64px;height:64px;border-radius:14px" />
    <div style="font-weight:800;font-size:18px">TOM Driver</div>
    <div class="muted small">Version ${esc(S.appVersion || '1.0.0')}${isStandalone() ? ' · installed' : ''}</div>
    <div class="muted tiny">Xtra Mile Couriers</div>
  </div></div>`));
  const links = el('<div class="card" style="padding:4px 14px"></div>');
  [['📄 Terms of service', '#/terms'], ['🔐 Privacy policy', '#/privacy-policy'], ['🆘 Help & support', '#/help'], ['🔏 Privacy & data', '#/privacy']]
    .forEach(([lbl, h]) => { const row = el(`<div class="kv tap" style="border-bottom:1px solid var(--line);cursor:pointer"><span class="k">${lbl}</span><span class="muted">›</span></div>`); row.onclick = () => go(h); links.appendChild(row); });
  main.appendChild(links);
  main.appendChild(backBtn());
  wrap.appendChild(main); wrap.appendChild(el(nav('me')));
  return wrap;
}

/* ── simple info screens ── */
function infoScreen(title, lines) {
  const wrap = el('<div></div>'); wrap.appendChild(el(header(title, '')));
  const main = el('<main></main>');
  main.appendChild(el(`<div class="card"><div class="stack">${lines.map(l => `<div>${l}</div>`).join('')}</div></div>`));
  main.appendChild(backBtn());
  wrap.appendChild(main); wrap.appendChild(el(nav('me')));
  return wrap;
}

function backBtn() { const b = el('<button class="btn secondary" style="margin-top:12px">← Back</button>'); b.onclick = () => history.back(); return b; }
function enc(o) { return encodeURIComponent(JSON.stringify({ lat: o.lat, lng: o.lng, q: o.postcode || o.address || '' })); }
window.navTo = (encoded) => {
  const o = JSON.parse(decodeURIComponent(encoded));
  const hasLL = o.lat != null && o.lng != null;
  const ll = hasLL ? `${o.lat},${o.lng}` : '';
  const q = encodeURIComponent(o.q || '');
  const app = (S.profile && S.profile.nav_app) || 'google';
  let url;
  if (app === 'waze') url = hasLL ? `https://waze.com/ul?ll=${ll}&navigate=yes` : `https://waze.com/ul?q=${q}`;
  else if (app === 'herewego') url = hasLL ? `https://share.here.com/r/${ll}` : `https://share.here.com/r/${q}`;
  else if (app === 'android') url = hasLL ? `geo:${ll}?q=${ll}` : `geo:0,0?q=${q}`;
  else url = `https://www.google.com/maps/dir/?api=1&destination=${hasLL ? ll : q}`;
  window.open(url, '_blank');
};

/* ── warn sheet ── */
function warnSheet(title, body) {
  const bg = el(`<div class="sheet-bg"><div class="sheet warn-sheet">
    <div class="icon">🛑</div><h3>${esc(title)}</h3><p class="muted">${esc(body)}</p>
    <button class="btn">Got it</button></div></div>`);
  bg.querySelector('button').onclick = () => bg.remove();
  bg.addEventListener('click', e => { if (e.target === bg) bg.remove(); });
  document.body.appendChild(bg);
}

/* ── geolocation tracking ── */
function currentPos() {
  return new Promise((res, rej) => { if (!navigator.geolocation) return rej();
    navigator.geolocation.getCurrentPosition(p => { const v = { lat: p.coords.latitude, lng: p.coords.longitude, acc: p.coords.accuracy }; S.lastPos = { lat: v.lat, lng: v.lng }; res(v); }, rej, { enableHighAccuracy: true, timeout: 6000 }); });
}
function startTracking() {
  S.tracking = true; localStorage.setItem(LS.tracking, '1');
  // Native build (Capacitor): use OS background geolocation so pings continue
  // when the screen is off / the app is backgrounded. Web PWA tracks foreground only.
  if (window.TOMNative && window.TOMNative.isNative && window.TOMNative.startBackgroundTracking) {
    window.TOMNative.startBackgroundTracking((p) => {
      S.lastPos = { lat: p.lat, lng: p.lng };
      enqueuePing({ id: uuid(), t: new Date().toISOString(), lat: p.lat, lng: p.lng, spd: p.spd, hdg: p.hdg, acc: p.acc });
      if (pingQueueLen() >= 5) flushPings();
    });
    if (!S._flushTimer) S._flushTimer = setInterval(flushPings, 15000);
    return;
  }
  if (!navigator.geolocation) { toast('No geolocation on this device', true); return; }
  S.geoWatch = navigator.geolocation.watchPosition(p => {
    S.lastPos = { lat: p.coords.latitude, lng: p.coords.longitude };
    enqueuePing({ id: uuid(), t: new Date().toISOString(), lat: p.coords.latitude, lng: p.coords.longitude, spd: p.coords.speed, hdg: p.coords.heading, acc: p.coords.accuracy });
    if (pingQueueLen() >= 5) flushPings();
  }, () => {}, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
  if (!S._flushTimer) S._flushTimer = setInterval(flushPings, 15000);
}
function stopTracking() {
  S.tracking = false; localStorage.setItem(LS.tracking, '0');
  if (window.TOMNative && window.TOMNative.stopBackgroundTracking) window.TOMNative.stopBackgroundTracking();
  if (S.geoWatch != null) { navigator.geolocation.clearWatch(S.geoWatch); S.geoWatch = null; }
}
/* Durable GPS-ping queue (localStorage) — survives offline stretches and app
 * kills. Each ping carries a client id; the server dedups on it, so re-sending
 * after a flaky reconnect never duplicates a point. Bounded so a very long
 * no-signal stretch can't overflow storage (oldest points evicted last resort). */
const PING_QUEUE_MAX = 5000;
function pingQueueGet() {
  try { return JSON.parse(localStorage.getItem(LS.pingOutbox) || '[]'); } catch (e) { return []; }
}
function pingQueueSet(q) {
  if (q.length > PING_QUEUE_MAX) q = q.slice(q.length - PING_QUEUE_MAX); // keep newest
  try { localStorage.setItem(LS.pingOutbox, JSON.stringify(q)); }
  catch (e) { // storage quota — shed the oldest half rather than lose the lot
    try { localStorage.setItem(LS.pingOutbox, JSON.stringify(q.slice(Math.floor(q.length / 2)))); } catch (_) {}
  }
}
function pingQueueLen() { return pingQueueGet().length; }
function enqueuePing(p) { const q = pingQueueGet(); q.push(p); pingQueueSet(q); }

async function flushPings() {
  if (!S.token) return;
  const q = pingQueueGet();
  if (!q.length) return;
  const batch = q.slice(0, 100);                 // bounded chunk per request
  const ids = new Set(batch.map(p => p.id));
  try {
    const r = await api('/location/batch', { method: 'POST', body: { pings: batch } });
    // 2xx (incl. an idempotent replay that stored 0 new) → the batch is safely
    // on the server; drop exactly those ids. A 5xx/403 leaves them queued to
    // retry. api() THROWS on a true network failure → caught below, kept.
    if (r.ok || (r.status >= 200 && r.status < 300)) {
      pingQueueSet(pingQueueGet().filter(p => !ids.has(p.id)));
      if (pingQueueLen()) setTimeout(flushPings, 500); // drain a long backlog
    } else if (r.status === 409) {
      // Definitive: not on shift — the server will never take these points,
      // so drop them rather than wedge the queue in a retry loop.
      pingQueueSet(pingQueueGet().filter(p => !ids.has(p.id)));
    }
  } catch (e) { /* offline — keep buffered, retry on interval / reconnect */ }
}

/* ── location consent (GDPR) ── */
function hasLocationConsent() { return !!(S.profile && S.profile.location_consent_at); }
function locationConsentSheet(onGrant) {
  const sheet = el(`<div class="sheet-bg"><div class="sheet">
    <h3>Share your location?</h3>
    <p class="muted small">While you're on shift, TOM shares your live location with dispatch so jobs are routed to you and customers get accurate ETAs. We keep it only while you work, and you can switch it off any time in Settings. See <b>Privacy &amp; data</b> for how it's handled.</p>
    <button class="btn" id="cy">Allow location sharing</button>
    <button class="btn secondary" id="cn" style="margin-top:8px">Not now</button>
  </div></div>`);
  document.body.appendChild(sheet);
  const close = () => sheet.remove();
  sheet.onclick = (e) => { if (e.target === sheet) close(); };
  $('#cy').onclick = async () => { close(); await mutate('/consent/location', { granted: true }); await loadProfile(); toast('Location sharing on'); onGrant && onGrant(); };
  $('#cn').onclick = close;
}
function enableTracking() {
  if (!hasLocationConsent()) { locationConsentSheet(() => { startTracking(); render(); }); return; }
  startTracking(); render();
}
async function disableTracking() {
  stopTracking();
  await mutate('/consent/location', { granted: false });   // withdrawing sharing withdraws consent
  await loadProfile(); render();
}

/* ── install / platform (Android + iOS) ── */
const LS_INSTALL_DISMISS = 'tom_driver_install_dismissed';
function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true
    || (window.TOMNative && window.TOMNative.isNative);
}
function platform() {
  const ua = navigator.userAgent || '';
  if (/iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) return 'ios';
  if (/Android/.test(ua)) return 'android';
  return 'other';
}
let _deferredInstall = null;
function initInstall() {
  // Android/Chrome: capture the native mini-infobar so we can trigger it from our own button.
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); _deferredInstall = e; maybeShowInstallBar(); });
  window.addEventListener('appinstalled', () => { _deferredInstall = null; removeInstallBar(); toast('Installed — open TOM Driver from your home screen'); });
  maybeShowInstallBar();
}
function removeInstallBar() { const b = $('#installbar'); if (b) b.remove(); }
function maybeShowInstallBar() {
  removeInstallBar();
  if (isStandalone() || localStorage.getItem(LS_INSTALL_DISMISS) === '1') return;
  const plat = platform();
  // Android shows once we have a deferred prompt; iOS has no prompt API → guide.
  if (plat === 'android' && !_deferredInstall) return;
  if (plat === 'other') return;
  const bar = el(`<div id="installbar" class="install-bar">
    <img src="/icons/icon-192.png" alt="" />
    <div class="txt"><div style="font-weight:700">Install TOM Driver</div>
      <div class="muted small">${plat === 'ios' ? 'Add to your home screen for full-screen use' : 'Add to your phone for full-screen, offline use'}</div></div>
    <button class="btn sm" id="installgo">${plat === 'ios' ? 'How' : 'Install'}</button>
    <button class="btn sm secondary" id="installx" aria-label="Dismiss">✕</button>
  </div>`);
  document.body.appendChild(bar);
  $('#installx').onclick = () => { localStorage.setItem(LS_INSTALL_DISMISS, '1'); removeInstallBar(); };
  $('#installgo').onclick = () => (plat === 'ios' ? showIosInstallSheet() : triggerAndroidInstall());
}
async function triggerAndroidInstall() {
  if (!_deferredInstall) { toast('Use your browser menu → “Install app”'); return; }
  _deferredInstall.prompt();
  try { await _deferredInstall.userChoice; } catch (e) {}
  _deferredInstall = null; removeInstallBar();
}
function showIosInstallSheet() {
  const sheet = el(`<div class="sheet-bg"><div class="sheet">
    <h3>Add TOM Driver to your home screen</h3>
    <div class="install-steps">
      <div class="step"><span class="n">1</span><div>Tap the <span class="ios-share">Share ⬆️</span> button in Safari's toolbar</div></div>
      <div class="step"><span class="n">2</span><div>Scroll down and tap <b>Add to Home Screen</b></div></div>
      <div class="step"><span class="n">3</span><div>Tap <b>Add</b> — then open <b>TOM Driver</b> from your home screen</div></div>
    </div>
    <p class="muted small">This gives you full-screen mode, an app icon, and offline access.</p>
    <button class="btn" id="iosdone">Got it</button>
  </div></div>`);
  document.body.appendChild(sheet);
  const close = () => sheet.remove();
  sheet.onclick = (e) => { if (e.target === sheet) close(); };
  $('#iosdone').onclick = () => { localStorage.setItem(LS_INSTALL_DISMISS, '1'); close(); removeInstallBar(); };
}

/* ── router ── */
async function render() {
  const root = $('#app'); root.innerHTML = '';
  const parts = (location.hash || '#/run').slice(2).split('/');
  const route = parts[0] || 'run';
  if (!S.token && route !== 'login') return go('#/login');
  let view;
  if (route === 'login') view = loginScreen();
  else if (route === 'home') { await Promise.all([loadHome(), loadMessages()]); view = homeScreen(); }
  else if (route === 'shift-start') view = shiftStartScreen();
  else if (route === 'history') { await loadHistory(); view = historyScreen(); }
  else if (route === 'run') { await loadRun(); view = runScreen(); }
  else if (route === 'job') { await loadJob(parts[1]); view = jobScreen(); }
  else if (route === 'scan') { if (!S.job || S.job.docket_number !== parts[1]) await loadJob(parts[1]); view = scanScreen(parts[1], parts[2], parts[3]); }
  else if (route === 'pod') { if (!S.job || S.job.docket_number !== parts[1]) await loadJob(parts[1]); view = podScreen(parts[1], parts[2]); }
  else if (route === 'fail') { if (!S.job || S.job.docket_number !== parts[1]) await loadJob(parts[1]); view = failScreen(parts[1], parts[2]); }
  else if (route === 'earnings') { await loadEarnings(); view = earningsScreen(); }
  else if (route === 'statements') { await loadStatements(); view = statementsScreen(); }
  else if (route === 'statement') { await loadStatement(parts[1]); view = statementScreen(parts[1]); }
  else if (route === 'tax') { await loadTax(); view = taxScreen(); }
  else if (route === 'expenses') { await loadExpenses(); view = expensesScreen(); }
  else if (route === 'profile') { await loadProfile(); view = profileScreen(); }
  else if (route === 'payment') { await loadProfile(); view = paymentScreen(); }
  else if (route === 'notifications') { await loadProfile(); view = notificationsScreen(); }
  else if (route === 'security') view = securityScreen();
  else if (route === 'documents') view = infoScreen('Documents', ['Insurance, MOT and licence are managed by ops.', '<span class="pill green">All valid</span>', '<span class="muted small">Document upload arrives in the native build.</span>']);
  else if (route === 'help') view = infoScreen('Help & support', ['Call ops: <a class="link" href="tel:">0800 000 000</a>', 'In an emergency, contact ops immediately.', '<span class="muted small">In-app SOS arrives in the native build.</span>']);
  else if (route === 'privacy') { await loadProfile(); view = privacyScreen(); }
  else if (route === 'about') view = aboutScreen();
  else if (route === 'terms') view = infoScreen('Terms of service', ['By using TOM Driver you agree to carry out assigned jobs in line with the Xtra Mile Couriers driver agreement.', 'These in-app terms summarise the signed contract held by ops, which prevails.', '<span class="muted small">Full terms are provided by the operations team.</span>']);
  else if (route === 'privacy-policy') view = infoScreen('Privacy policy', ['We process your data to operate courier services: account, jobs, proof of delivery, and — with consent, while on shift — location.', 'Data is retained as required for tax and operational records and shared only with the relevant customer and ops.', 'Manage your rights under <a class="link" href="#/privacy">Privacy &amp; data</a>.', '<span class="muted small">The full policy is provided by the operations team.</span>']);
  else if (route === 'messages') { await loadMessages(); await markMessagesRead(); view = messagesScreen(); }
  else if (route === 'me') view = meScreen();
  else { await Promise.all([loadHome(), loadMessages()]); view = homeScreen(); }
  root.appendChild(view);
  syncChip();
}

window.addEventListener('hashchange', () => { if (!location.hash.includes('/scan/')) stopCamera(); render(); });
window.addEventListener('online', async () => {
  showOfflineBanner(false); syncChip();
  await drainOutbox();
  flushPings();  // reconnect → upload GPS points buffered while offline (in order)
  // TOM is authoritative: drop any on-device fallback order and re-sync to dispatch.
  if (S.routeSource === 'local') {
    await loadRun();
    toast('Back online — synced to dispatch route');
    if (location.hash.startsWith('#/run') || location.hash.startsWith('#/home')) render();
  }
});
window.addEventListener('offline', () => { showOfflineBanner(true); beep(); syncChip(); });

// Live shift countdown ticker — updates the header clock once a second.
setInterval(() => {
  const el2 = document.getElementById('shiftclock');
  if (el2 && S.shift) { const r = shiftRemaining(S.shift.planned_end); if (r) el2.textContent = r.label; }
}, 1000);

/* Native biometric app-lock: when enabled and available, require Face/Touch ID
   before the app is shown. No-op on the web PWA (which has no biometric API). */
async function biometricGate() {
  const p = S.profile || {};
  if (!p.biometric) return true;
  if (!(window.TOMNative && window.TOMNative.isNative && window.TOMNative.biometricAuth)) return true;
  const root = $('#app');
  root.innerHTML = `<div class="login"><div class="logo"><div class="mark">T</div><div><div style="font-weight:800;font-size:20px">TOM Driver</div><div class="muted small">Locked</div></div></div>
    <button class="btn" id="unlock">🔓 Unlock</button></div>`;
  const tryUnlock = async () => (await window.TOMNative.biometricAuth('Unlock TOM Driver')) === true;
  if (await tryUnlock()) return true;
  return new Promise((res) => { $('#unlock').onclick = async () => { if (await tryUnlock()) res(true); else toast('Could not verify', true); }; });
}

// Register this device's push token with the backend so ops can target it.
async function registerPush() {
  if (!(window.TOMNative && window.TOMNative.isNative && window.TOMNative.pushToken)) return;
  const tok = window.TOMNative.pushToken();
  if (tok) { try { await mutate('/push/register', { token: tok, platform: window.TOMNative.platform }); } catch (e) {} }
}

/* ── boot ── */
(async function boot() {
  applyTheme();   // instant theme from localStorage before anything renders
  document.documentElement.dataset.platform = platform();
  if (isStandalone()) document.documentElement.dataset.standalone = '1';
  if (window.TOMNative && window.TOMNative.init) { try { await window.TOMNative.init({ onPush: () => { loadMessages(); if (location.hash.startsWith('#/messages') || location.hash.startsWith('#/home')) render(); } }); } catch (e) {} }
  if ('serviceWorker' in navigator) { try { await navigator.serviceWorker.register('/sw.js'); } catch (e) {} }
  await loadConfig();
  if (S.token) {
    await Promise.all([loadMessages(), loadShift(), loadProfile()]);
    if (!(await biometricGate())) return;                       // stay locked until verified
    if (S.tracking && hasLocationConsent()) startTracking();    // GDPR: never auto-track without consent
    registerPush();
    drainOutbox();
  }
  if (!location.hash) go(S.token ? '#/home' : '#/login');
  if (!navigator.onLine) showOfflineBanner(true);
  render();
  initInstall();
  setInterval(drainOutbox, 20000);
})();
