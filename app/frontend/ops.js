/* TOM Dispatch console — vanilla JS, no build step (mirrors the driver PWA).
   Live tracking + job dispatch + driver chat against /api/ops/v1. */
'use strict';

const API = '/api/ops/v1';
const LS = { tok: 'tom_ops_token', user: 'tom_ops_user' };
const $ = (s, r = document) => r.querySelector(s);
const app = () => document.getElementById('app');

const S = {
  token: localStorage.getItem(LS.tok) || null,
  user: null,
  view: 'dashboard',
  dash: null,
  jobsTab: 'active',
  // map
  markers: [], mapNow: null, selected: null, detail: null, trail: [],
  poll: null,
};

// ── utils ────────────────────────────────────────────────────────────
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, err) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.toggle('err', !!err); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3200);
}
function ago(sec) {
  if (sec == null) return '—';
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  return Math.floor(sec / 3600) + 'h ago';
}
function money(v) { return '£' + (v == null || v === '' ? '0.00' : v); }
function dutyDot(s) {
  const cls = s === 'available' ? 'on' : (s === 'going_home' ? 'home' : 'off');
  const lbl = s === 'available' ? 'Available' : (s === 'going_home' ? 'Going home' : 'Off shift');
  return `<span class="dot ${cls}"></span>${lbl}`;
}
const LIFE_STEPS = ['assigned', 'acknowledged', 'en_route_pickup', 'at_pickup', 'pob', 'en_route_drop', 'completed'];
function lifePill(s) {
  const map = {
    unassigned: ['grey', 'Unassigned'], assigned: ['blue', 'Assigned'],
    acknowledged: ['blue', 'Acknowledged'], en_route_pickup: ['amber', 'To pickup'],
    at_pickup: ['amber', 'At pickup'], pob: ['amber', 'On board'],
    en_route_drop: ['amber', 'Delivering'], completed: ['green', 'Completed'],
    cancelled: ['red', 'Cancelled'],
  };
  const [c, l] = map[s] || ['grey', s || '—'];
  return `<span class="pill ${c}">${esc(l)}</span>`;
}

// ── API ──────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const h = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (S.token) h['Authorization'] = 'Bearer ' + S.token;
  let r;
  try {
    r = await fetch(API + path, Object.assign({}, opts, { headers: h }));
  } catch (e) { throw { code: 'network', message: 'Network error', status: 0 }; }
  let body = null; try { body = await r.json(); } catch (e) {}
  if (r.status === 401) { logout(true); throw { code: 'unauthorized', message: 'Session expired', status: 401 }; }
  if (!r.ok) throw Object.assign({ status: r.status }, (body && body.error) || { message: 'Error' });
  return body;
}

// ── auth ─────────────────────────────────────────────────────────────
function renderLogin(errMsg) {
  app().innerHTML = `
    <div class="login-wrap"><form class="login-card" id="lf">
      <div class="brand"><div class="mark">TD</div>
        <div><h1>TOM Dispatch</h1><div class="sub">Xtra Mile Couriers · operations console</div></div></div>
      <label>Username</label><input id="u" autocomplete="username" autofocus />
      <label>Password</label><input id="p" type="password" autocomplete="current-password" />
      <div style="height:16px"></div>
      <button class="btn" style="width:100%" id="go">Sign in</button>
      <div class="err-txt" id="e">${errMsg ? esc(errMsg) : ''}</div>
    </form></div>`;
  $('#lf').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const go = $('#go'); go.disabled = true; go.textContent = 'Signing in…';
    try {
      const b = await apiFetch('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username: $('#u').value.trim(), password: $('#p').value }),
      });
      S.token = b.token; S.user = b.user;
      localStorage.setItem(LS.tok, b.token);
      localStorage.setItem(LS.user, JSON.stringify(b.user));
      boot();
    } catch (e) {
      $('#e').textContent = e.message || 'Sign-in failed';
      go.disabled = false; go.textContent = 'Sign in';
    }
  });
}
function logout(expired) {
  if (S.poll) { clearInterval(S.poll); S.poll = null; }
  S.token = null; S.user = null;
  localStorage.removeItem(LS.tok); localStorage.removeItem(LS.user);
  renderLogin(expired ? 'Your session expired — please sign in again.' : null);
}

// ── shell ────────────────────────────────────────────────────────────
const NAV = [
  { id: 'dashboard', ico: '▚', lbl: 'Dashboard' },
  { id: 'map', ico: '◉', lbl: 'Live map' },
  { id: 'jobs', ico: '▤', lbl: 'Jobs' },
  { id: 'drivers', ico: '☰', lbl: 'Drivers' },
];
function renderShell() {
  const badge = (id) => {
    if (!S.dash) return '';
    if (id === 'jobs' && S.dash.unassigned_jobs) return `<span class="count">${S.dash.unassigned_jobs}</span>`;
    if (id === 'drivers' && S.dash.unread_driver_messages) return `<span class="count">${S.dash.unread_driver_messages}</span>`;
    return '';
  };
  app().innerHTML = `
    <div class="shell">
      <aside class="side">
        <div class="brand"><div class="mark">TD</div>
          <div><h1>TOM Dispatch</h1><div class="sub">Operations</div></div></div>
        <nav>${NAV.map(n => `<a data-v="${n.id}" class="${S.view === n.id ? 'active' : ''}">
          <span class="ico">${n.ico}</span><span class="lbl">${n.lbl}</span>${badge(n.id)}</a>`).join('')}</nav>
        <div class="who"><div><b>${esc(S.user ? S.user.name || S.user.username : '')}</b></div>
          <div class="muted tiny" style="text-transform:capitalize">${esc(S.user ? S.user.role : '')}</div>
          <div style="height:10px"></div>
          <button class="btn secondary sm" id="lo">Sign out</button></div>
      </aside>
      <main class="main">
        <div class="topbar"><h2 id="pt"></h2><div class="spacer"></div><div id="tbextra"></div></div>
        <div class="content" id="c"></div>
      </main>
    </div>
    <div class="drawer-bg" id="dbg"></div>
    <div class="drawer" id="drawer"><div class="dhead"><h3 id="dtitle"></h3>
      <button class="x" id="dx">×</button></div><div class="dbody" id="dbody"></div></div>`;
  app().querySelectorAll('.side nav a').forEach(a =>
    a.addEventListener('click', () => go(a.dataset.v)));
  $('#lo').addEventListener('click', () => logout(false));
  $('#dx').addEventListener('click', closeDrawer);
  $('#dbg').addEventListener('click', closeDrawer);
}
function go(view) {
  if (S.poll) { clearInterval(S.poll); S.poll = null; }
  S.view = view;
  if (('#' + view) !== location.hash) { try { location.hash = view; } catch (e) {} }
  renderShell();
  ({ dashboard: viewDashboard, map: viewMap, jobs: viewJobs, drivers: viewDrivers }[view] || viewDashboard)();
}

// ── drawer ───────────────────────────────────────────────────────────
function openDrawer(title, html) {
  $('#dtitle').innerHTML = title; $('#dbody').innerHTML = html;
  $('#drawer').classList.add('open'); $('#dbg').classList.add('open');
}
function closeDrawer() {
  $('#drawer').classList.remove('open'); $('#dbg').classList.remove('open');
}

// ── dashboard ────────────────────────────────────────────────────────
async function refreshDash() { try { S.dash = await apiFetch('/dashboard'); } catch (e) {} }
async function viewDashboard() {
  $('#pt').textContent = 'Dashboard';
  $('#c').innerHTML = '<div class="muted">Loading…</div>';
  await refreshDash();
  const d = S.dash || {};
  const tile = (n, l, cls, act) => `<div class="tile ${cls || ''} ${act ? 'click' : ''}" ${act ? `data-act="${act}"` : ''}>
    <div class="n">${n}</div><div class="l">${l}</div></div>`;
  $('#c').innerHTML = `
    <div class="tiles">
      ${tile(d.drivers_on_shift + '/' + d.drivers_total, 'Drivers on shift', 'good', 'drivers')}
      ${tile(d.active_jobs, 'Active jobs', '', 'jobs')}
      ${tile(d.unassigned_jobs, 'Unassigned', d.unassigned_jobs ? 'alert' : '', 'unassigned')}
      ${tile(d.jobs_with_failure, 'Jobs with a failed drop', d.jobs_with_failure ? 'bad' : '')}
      ${tile(d.unread_driver_messages, 'Unread driver messages', d.unread_driver_messages ? 'alert' : '', 'drivers')}
      ${tile(d.pending_profile_changes, 'Profile changes to review', d.pending_profile_changes ? 'alert' : '')}
      ${tile(d.open_data_requests, 'Open GDPR requests', d.open_data_requests ? 'alert' : '')}
    </div>
    <div class="section-title">Quick actions</div>
    <div class="btn-row">
      <button class="btn" id="newjob">+ New job</button>
      <button class="btn secondary" data-act="map">Open live map</button>
      <button class="btn secondary" data-act="unassigned">View unassigned</button>
    </div>`;
  $('#c').querySelectorAll('[data-act]').forEach(el => el.addEventListener('click', () => {
    const a = el.dataset.act;
    if (a === 'unassigned') { S.jobsTab = 'unassigned'; go('jobs'); }
    else go(a);
  }));
  $('#newjob').addEventListener('click', newJobForm);
}

// ── live map ─────────────────────────────────────────────────────────
async function viewMap() {
  $('#pt').textContent = 'Live map';
  $('#tbextra').innerHTML = '<span class="pill grey" id="mstamp">—</span>';
  $('#c').innerHTML = `
    <div class="map-shell">
      <div class="map-box">
        <canvas id="map"></canvas>
        <div class="map-legend">
          <div><span class="dot on"></span>Available</div>
          <div><span class="dot home"></span>Going home</div>
          <div><span class="dot off"></span>Off / idle</div>
          <div class="muted tiny" style="margin-top:2px">Click a driver to track</div>
        </div>
      </div>
      <div class="map-side" id="mside"><div class="card"><div class="empty">
        <div class="big">◉</div>Select a driver on the map to see their route and current job.</div></div></div>
    </div>`;
  const cv = $('#map');
  cv.addEventListener('click', onMapClick);
  if (!drawMap._resizeBound) { window.addEventListener('resize', () => drawMap()); drawMap._resizeBound = true; }
  await refreshTracking();
  S.poll = setInterval(refreshTracking, 5000);
}
async function refreshTracking() {
  try {
    const b = await apiFetch('/tracking');
    S.markers = b.drivers; S.mapNow = b.now;
    const st = $('#mstamp'); if (st) st.textContent = S.markers.length + ' driver' + (S.markers.length === 1 ? '' : 's') + ' tracked';
    if (S.selected) {
      try { const t = await apiFetch('/tracking/' + encodeURIComponent(S.selected) + '/trail?n=80'); S.trail = t.trail; } catch (e) {}
    }
    drawMap();
  } catch (e) {}
}
// equirectangular projection with aspect correction, auto-fit to content
function _project(cv) {
  const pts = S.markers.map(m => [m.lat, m.lng]);
  (S.trail || []).forEach(p => pts.push([p.lat, p.lng]));
  if (S.detail && S.detail.jobs) S.detail.jobs.forEach(j => {
    if (j.pickup && j.pickup.lat != null) pts.push([j.pickup.lat, j.pickup.lng]);
    (j.drops || []).forEach(d => { if (d.lat != null) pts.push([d.lat, d.lng]); });
  });
  if (!pts.length) return null;
  let minLa = 90, maxLa = -90, minLo = 180, maxLo = -180;
  pts.forEach(([la, lo]) => { minLa = Math.min(minLa, la); maxLa = Math.max(maxLa, la); minLo = Math.min(minLo, lo); maxLo = Math.max(maxLo, lo); });
  const midLa = (minLa + maxLa) / 2, k = Math.cos(midLa * Math.PI / 180) || 1;
  let padLa = (maxLa - minLa) * 0.18 || 0.01, padLo = (maxLo - minLo) * 0.18 || 0.01;
  minLa -= padLa; maxLa += padLa; minLo -= padLo; maxLo += padLo;
  const W = cv.width, H = cv.height, M = 26;
  const spanLo = (maxLo - minLo) * k, spanLa = (maxLa - minLa);
  const sc = Math.min((W - 2 * M) / spanLo, (H - 2 * M) / spanLa);
  const offX = (W - spanLo * sc) / 2, offY = (H - spanLa * sc) / 2;
  return {
    x: (lo) => offX + (lo - minLo) * k * sc,
    y: (la) => H - (offY + (la - minLa) * sc),
  };
}
function drawMap() {
  const cv = $('#map'); if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = cv.getBoundingClientRect();
  cv.width = Math.max(300, rect.width * dpr); cv.height = Math.max(300, rect.height * dpr);
  const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#0b1420'; ctx.fillRect(0, 0, cv.width, cv.height);
  // graticule
  ctx.strokeStyle = 'rgba(70,100,140,.14)'; ctx.lineWidth = 1;
  for (let i = 1; i < 10; i++) {
    const x = cv.width * i / 10, y = cv.height * i / 10;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, cv.height); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cv.width, y); ctx.stroke();
  }
  const P = _project(cv);
  cv._hit = [];
  if (!P) {
    ctx.fillStyle = '#93a6c2'; ctx.font = `${16 * dpr}px system-ui`; ctx.textAlign = 'center';
    ctx.fillText('No live driver positions yet.', cv.width / 2, cv.height / 2);
    return;
  }
  const scale = dpr;
  // selected driver's job pins + trail
  if (S.detail && S.detail.jobs) {
    S.detail.jobs.forEach(j => {
      if (j.pickup && j.pickup.lat != null) pin(ctx, P.x(j.pickup.lng), P.y(j.pickup.lat), '#4D9EF5', 'P', scale);
      (j.drops || []).forEach((d, i) => { if (d.lat != null) pin(ctx, P.x(d.lng), P.y(d.lat), '#E0A200', String(i + 1), scale); });
    });
  }
  if (S.trail && S.trail.length > 1) {
    ctx.strokeStyle = 'rgba(77,158,245,.75)'; ctx.lineWidth = 2.5 * scale; ctx.beginPath();
    S.trail.forEach((p, i) => { const x = P.x(p.lng), y = P.y(p.lat); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }
  // driver markers
  S.markers.forEach(m => {
    const x = P.x(m.lng), y = P.y(m.lat);
    const col = m.duty_status === 'available' ? '#2ED47A' : (m.duty_status === 'going_home' ? '#E0A200' : '#93a6c2');
    const isSel = m.driver_id === S.selected;
    const stale = m.age_s != null && m.age_s > 300;
    ctx.globalAlpha = stale ? 0.5 : 1;
    if (isSel) { ctx.beginPath(); ctx.arc(x, y, 15 * scale, 0, 7); ctx.fillStyle = 'rgba(77,158,245,.25)'; ctx.fill(); }
    ctx.beginPath(); ctx.arc(x, y, 8 * scale, 0, 7); ctx.fillStyle = col; ctx.fill();
    ctx.lineWidth = 2 * scale; ctx.strokeStyle = '#0b1420'; ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = '#EAF1FB'; ctx.font = `${12 * scale}px system-ui`; ctx.textAlign = 'left';
    ctx.fillText(m.callsign || m.driver_id, x + 12 * scale, y + 4 * scale);
    cv._hit.push({ x, y, r: 16 * scale, id: m.driver_id });
  });
}
function pin(ctx, x, y, col, label, s) {
  ctx.beginPath(); ctx.arc(x, y, 7 * s, 0, 7); ctx.fillStyle = col; ctx.fill();
  ctx.lineWidth = 1.5 * s; ctx.strokeStyle = '#0b1420'; ctx.stroke();
  ctx.fillStyle = '#04203f'; ctx.font = `bold ${10 * s}px system-ui`; ctx.textAlign = 'center';
  ctx.fillText(label, x, y + 3.5 * s);
}
async function onMapClick(ev) {
  const cv = $('#map'); const rect = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cx = (ev.clientX - rect.left) * dpr, cy = (ev.clientY - rect.top) * dpr;
  let best = null, bd = 1e9;
  (cv._hit || []).forEach(h => { const d = Math.hypot(h.x - cx, h.y - cy); if (d < h.r && d < bd) { bd = d; best = h.id; } });
  if (!best) return;
  S.selected = best;
  try {
    S.detail = await apiFetch('/drivers/' + encodeURIComponent(best));
    const t = await apiFetch('/tracking/' + encodeURIComponent(best) + '/trail?n=80');
    S.trail = t.trail;
  } catch (e) {}
  renderMapSide(); drawMap();
}
function renderMapSide() {
  const d = S.detail; if (!d) return;
  const dv = d.driver;
  const jobsHtml = d.jobs.length ? d.jobs.map(j => `
    <div class="kv"><span class="k mono">${esc(j.docket_number)}</span>${lifePill(j.lifecycle_status)}</div>
    <div class="tiny muted" style="margin:-2px 0 8px">${esc(j.account || '')} · ${esc(j.pickup.postcode || '')} → ${esc((j.drops[0] || {}).postcode || '')}</div>`).join('')
    : '<div class="muted small">No active jobs.</div>';
  const fix = d.last_fix;
  $('#mside').innerHTML = `<div class="card">
    <div class="row" style="display:flex;justify-content:space-between;align-items:center">
      <h3 style="margin:0">${esc(dv.name)}</h3><span class="pill grey mono">${esc(dv.callsign)}</span></div>
    <div class="small" style="margin:6px 0 10px">${dutyDot(dv.duty_status)} · ${esc(dv.vehicle || '')}</div>
    <div class="kv"><span class="k">Last fix</span><span>${fix ? ago(S.markers.find(m => m.driver_id === S.selected)?.age_s) : '—'}</span></div>
    <div class="kv"><span class="k">Phone</span><span>${esc(dv.phone || '—')}</span></div>
    <div class="section-title" style="margin-top:14px">Active jobs</div>${jobsHtml}
    <div style="height:12px"></div>
    <div class="btn-row"><button class="btn sm" id="mchat">Message</button>
      <button class="btn secondary sm" id="mfull">Driver detail</button></div>
  </div>`;
  $('#mchat').addEventListener('click', () => openChat(S.selected, dv.name));
  $('#mfull').addEventListener('click', () => openDriver(S.selected));
}

// ── jobs ─────────────────────────────────────────────────────────────
const JOB_TABS = [['active', 'Active'], ['unassigned', 'Unassigned'], ['completed', 'Completed'], ['all', 'All']];
async function viewJobs() {
  $('#pt').textContent = 'Jobs';
  $('#tbextra').innerHTML = '<button class="btn sm" id="njb">+ New job</button>';
  $('#njb').addEventListener('click', newJobForm);
  $('#c').innerHTML = `<div class="tabs">${JOB_TABS.map(([k, l]) =>
    `<button data-t="${k}" class="${S.jobsTab === k ? 'active' : ''}">${l}</button>`).join('')}</div>
    <div id="joblist" class="muted">Loading…</div>`;
  $('#c').querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => { S.jobsTab = b.dataset.t; viewJobs(); }));
  await loadJobs();
}
async function loadJobs() {
  let jobs = [];
  try { jobs = (await apiFetch('/jobs?status=' + S.jobsTab)).jobs; } catch (e) { $('#joblist').innerHTML = '<div class="err-txt">' + esc(e.message) + '</div>'; return; }
  if (!jobs.length) { $('#joblist').innerHTML = '<div class="empty"><div class="big">▤</div>No jobs here.</div>'; return; }
  $('#joblist').innerHTML = `<div class="card tbl-wrap"><table>
    <thead><tr><th>Docket</th><th>Account</th><th>Driver</th><th>Deadline</th><th>Status</th><th>Progress</th><th>Pay</th></tr></thead>
    <tbody>${jobs.map(j => {
      const pr = j.progress;
      const prog = `${pr.drops_delivered}/${pr.drops_total} drops${pr.has_failure ? ' <span class="pill red">fail</span>' : ''}`;
      return `<tr class="click" data-d="${esc(j.docket_number)}">
        <td class="mono">${esc(j.docket_number)}</td><td>${esc(j.account || '—')}</td>
        <td>${j.driver_name ? esc(j.driver_name) : '<span class="pill grey">unassigned</span>'}</td>
        <td>${esc(j.deadline || '—')}</td><td>${lifePill(j.lifecycle_status)}</td>
        <td class="small">${prog}</td><td class="mono">${money(j.driver_pay_final)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  $('#joblist').querySelectorAll('tr.click').forEach(tr => tr.addEventListener('click', () => openJob(tr.dataset.d)));
}
async function openJob(docket) {
  let job, drivers = [];
  try { job = (await apiFetch('/jobs/' + encodeURIComponent(docket))).job; }
  catch (e) { toast(e.message, true); return; }
  try { drivers = (await apiFetch('/drivers')).drivers; } catch (e) {}
  const closed = job.status === 'COMPLETED' || job.status === 'CANCELLED';
  const dropRows = job.drops.map(d => `<div class="kv"><span class="k">Drop ${d.seq} · ${esc(d.postcode || '')}</span>
    <span class="pill ${d.status === 'delivered' ? 'green' : d.status === 'failed' ? 'red' : d.status === 'arrived' ? 'amber' : 'grey'}">${esc(d.status)}</span></div>
    <div class="tiny muted" style="margin:-2px 0 8px">${esc(d.address || '')}${d.contact ? ' · ' + esc(d.contact) : ''}${d.instructions ? ' · ' + esc(d.instructions) : ''}</div>`).join('');
  const opts = drivers.map(d => `<option value="${esc(d.driver_id)}" ${d.driver_id === job.driver_id ? 'selected' : ''}>${esc(d.name)} (${esc(d.callsign)})${d.active ? '' : ' — inactive'}</option>`).join('');
  openDrawer(`<span class="mono">${esc(docket)}</span> ${lifePill(job.lifecycle_status)}`, `
    <div class="kv"><span class="k">Account</span><span>${esc(job.account || '—')}</span></div>
    <div class="kv"><span class="k">Vehicle</span><span>${esc(job.vehicle || '—')}</span></div>
    <div class="kv"><span class="k">Deadline</span><span>${esc(job.deadline || '—')}</span></div>
    <div class="kv"><span class="k">Scanning</span><span>${job.requires_scan ? 'Required' : 'Not required'}</span></div>
    <div class="kv"><span class="k">Driver pay</span><span class="mono">${money(job.driver_pay_final)}</span></div>
    <div class="section-title">Pickup</div>
    <div class="small">${esc(job.pickup.address || '')} <b>${esc(job.pickup.postcode || '')}</b></div>
    ${job.pickup.contact ? `<div class="tiny muted">${esc(job.pickup.contact)}</div>` : ''}
    ${job.special_instructions ? `<div class="tiny" style="color:var(--warn)">⚠ ${esc(job.special_instructions)}</div>` : ''}
    <div class="section-title">Drops (${job.drops.length})</div>${dropRows}
    ${closed ? '' : `<div class="section-title">Assign driver</div>
      <select id="asgn"><option value="">— Unassigned —</option>${opts}</select>
      <div style="height:12px"></div>
      <div class="btn-row">
        <button class="btn" id="doasgn" data-d="${esc(docket)}">Update assignment</button>
        <button class="btn danger" id="docancel" data-d="${esc(docket)}">Cancel job</button>
      </div>`}
    ${job.driver_id ? `<div style="height:12px"></div><button class="btn secondary" id="jmsg">Message driver</button>` : ''}
  `);
  if (!closed) {
    $('#doasgn').addEventListener('click', async () => {
      try { await apiFetch('/jobs/' + encodeURIComponent(docket) + '/assign', { method: 'POST', body: JSON.stringify({ driver_id: $('#asgn').value }) });
        toast('Assignment updated'); closeDrawer(); await refreshDash(); go('jobs'); }
      catch (e) { toast(e.message, true); }
    });
    $('#docancel').addEventListener('click', async () => {
      if (!confirm('Cancel job ' + docket + '? The assigned driver is notified.')) return;
      try { await apiFetch('/jobs/' + encodeURIComponent(docket) + '/cancel', { method: 'POST' });
        toast('Job cancelled'); closeDrawer(); await refreshDash(); go('jobs'); }
      catch (e) { toast(e.message, true); }
    });
  }
  if (job.driver_id) $('#jmsg') && $('#jmsg').addEventListener('click', () => openChat(job.driver_id, null, docket));
}

async function newJobForm() {
  let drivers = [];
  try { drivers = (await apiFetch('/drivers')).drivers; } catch (e) {}
  const opts = drivers.map(d => `<option value="${esc(d.driver_id)}">${esc(d.name)} (${esc(d.callsign)})</option>`).join('');
  openDrawer('New job', `
    <label>Account / customer</label><input id="f_account" placeholder="e.g. ACME01" />
    <div class="grid2">
      <div><label>Vehicle</label><input id="f_vehicle" placeholder="Small Van" /></div>
      <div><label>Deadline (HH:MM)</label><input id="f_deadline" placeholder="16:30" /></div>
    </div>
    <label>Assign to driver (optional)</label>
    <select id="f_driver"><option value="">— Leave unassigned —</option>${opts}</select>
    <div class="section-title">Pickup</div>
    <input id="f_pu_addr" placeholder="Pickup address" />
    <div class="grid2"><div><label>Postcode</label><input id="f_pu_pc" /></div>
      <div><label>Contact</label><input id="f_pu_ct" /></div></div>
    <div class="grid2"><div><label>Lat (optional)</label><input id="f_pu_lat" /></div>
      <div><label>Lng (optional)</label><input id="f_pu_lng" /></div></div>
    <label>Special instructions</label><input id="f_special" />
    <label><input type="checkbox" id="f_scan" checked style="width:auto;margin-right:8px" />Barcode scanning required</label>
    <div class="section-title">First drop</div>
    <input id="f_dr_addr" placeholder="Drop address" />
    <div class="grid2"><div><label>Postcode</label><input id="f_dr_pc" /></div>
      <div><label>Contact</label><input id="f_dr_ct" /></div></div>
    <div class="grid2"><div><label>Lat (optional)</label><input id="f_dr_lat" /></div>
      <div><label>Lng (optional)</label><input id="f_dr_lng" /></div></div>
    <label>Parcel barcode(s), comma-separated (optional)</label><input id="f_barcodes" placeholder="XM001, XM002" />
    <div class="section-title">Driver pay</div>
    <div class="grid2"><div><label>Base £</label><input id="f_base" placeholder="0.00" /></div>
      <div><label>Extras £</label><input id="f_extras" placeholder="0.00" /></div></div>
    <div style="height:16px"></div>
    <button class="btn" id="createjob" style="width:100%">Create job</button>
    <div class="err-txt" id="cjerr"></div>`);
  $('#createjob').addEventListener('click', async () => {
    const num = (id) => { const v = $(id).value.trim(); return v === '' ? null : parseFloat(v); };
    const barcodes = $('#f_barcodes').value.split(',').map(s => s.trim()).filter(Boolean)
      .map(b => ({ barcode: b }));
    const drop = {
      address: $('#f_dr_addr').value.trim(), postcode: $('#f_dr_pc').value.trim(),
      contact: $('#f_dr_ct').value.trim(), lat: num('#f_dr_lat'), lng: num('#f_dr_lng'),
      parcels: barcodes,
    };
    const payload = {
      account: $('#f_account').value.trim(), vehicle: $('#f_vehicle').value.trim(),
      deadline: $('#f_deadline').value.trim(), driver_id: $('#f_driver').value,
      special_instructions: $('#f_special').value.trim(), requires_scan: $('#f_scan').checked,
      pickup: {
        address: $('#f_pu_addr').value.trim(), postcode: $('#f_pu_pc').value.trim(),
        contact: $('#f_pu_ct').value.trim(), lat: num('#f_pu_lat'), lng: num('#f_pu_lng'),
      },
      pay: { base: $('#f_base').value.trim() || '0', extras: $('#f_extras').value.trim() || '0' },
      drops: [drop],
    };
    const btn = $('#createjob'); btn.disabled = true; btn.textContent = 'Creating…';
    try {
      const r = await apiFetch('/jobs', { method: 'POST', body: JSON.stringify(payload) });
      toast('Job ' + r.docket + ' created'); closeDrawer(); await refreshDash();
      S.jobsTab = payload.driver_id ? 'active' : 'unassigned'; go('jobs');
    } catch (e) {
      $('#cjerr').textContent = e.message || 'Could not create job';
      btn.disabled = false; btn.textContent = 'Create job';
    }
  });
}

// ── drivers ──────────────────────────────────────────────────────────
async function viewDrivers() {
  $('#pt').textContent = 'Drivers';
  $('#tbextra').innerHTML = '';
  $('#c').innerHTML = '<div class="muted">Loading…</div>';
  let drivers = [];
  try { drivers = (await apiFetch('/drivers')).drivers; } catch (e) { $('#c').innerHTML = '<div class="err-txt">' + esc(e.message) + '</div>'; return; }
  $('#c').innerHTML = `<div class="card tbl-wrap"><table>
    <thead><tr><th>Driver</th><th>Callsign</th><th>Status</th><th>Vehicle</th><th>Active jobs</th><th>Last fix</th><th>Messages</th></tr></thead>
    <tbody>${drivers.map(d => `<tr class="click" data-id="${esc(d.driver_id)}">
      <td>${esc(d.name)}${d.is_subcontracted ? ' <span class="pill blue">sub</span>' : ''}${d.active ? '' : ' <span class="pill red">inactive</span>'}</td>
      <td class="mono">${esc(d.callsign)}</td><td class="small">${dutyDot(d.duty_status)}</td>
      <td>${esc(d.vehicle || '—')}</td><td>${d.active_jobs}</td>
      <td class="small">${ago(d.last_fix_age_s)}</td>
      <td>${d.unread_from_driver ? `<span class="pill amber">${d.unread_from_driver} new</span>` : '<span class="muted">—</span>'}</td>
    </tr>`).join('')}</tbody></table></div>`;
  $('#c').querySelectorAll('tr.click').forEach(tr => tr.addEventListener('click', () => openDriver(tr.dataset.id)));
}
async function openDriver(driverId) {
  let d;
  try { d = await apiFetch('/drivers/' + encodeURIComponent(driverId)); } catch (e) { toast(e.message, true); return; }
  const dv = d.driver;
  const jobs = d.jobs.length ? d.jobs.map(j => `<div class="kv"><span class="k mono">${esc(j.docket_number)}</span>${lifePill(j.lifecycle_status)}</div>`).join('') : '<div class="muted small">No active jobs.</div>';
  openDrawer(`${esc(dv.name)} <span class="pill grey mono">${esc(dv.callsign)}</span>`, `
    <div class="small" style="margin-bottom:8px">${dutyDot(dv.duty_status)}${dv.active ? '' : ' · <span class="pill red">inactive</span>'}</div>
    <div class="kv"><span class="k">Vehicle</span><span>${esc(dv.vehicle || '—')}${dv.vehicle_reg ? ' · ' + esc(dv.vehicle_reg) : ''}</span></div>
    <div class="kv"><span class="k">Phone</span><span>${esc(dv.phone || '—')}</span></div>
    <div class="kv"><span class="k">Home</span><span>${esc(dv.home_postcode || '—')}</span></div>
    <div class="kv"><span class="k">Rating</span><span>${esc(dv.rating != null ? dv.rating : '—')} ★ · on-time ${esc(dv.on_time_pct)}%</span></div>
    <div class="section-title">Active jobs</div>${jobs}
    <div style="height:14px"></div>
    <div class="btn-row"><button class="btn" id="dcchat">Message driver</button>
      <button class="btn secondary" id="dctrack">Track on map</button></div>`);
  $('#dcchat').addEventListener('click', () => openChat(driverId, dv.name));
  $('#dctrack').addEventListener('click', () => { S.selected = driverId; closeDrawer(); go('map'); });
}

// ── chat ─────────────────────────────────────────────────────────────
async function openChat(driverId, name, docket) {
  openDrawer('Chat' + (name ? ' · ' + esc(name) : ''), `
    <div class="chat">
      <div class="chat-log" id="clog"><div class="muted">Loading…</div></div>
      ${docket ? `<div class="tiny muted" style="margin-top:8px">Tagged to job <b class="mono">${esc(docket)}</b></div>` : ''}
      <div class="chat-in"><input id="cmsg" placeholder="Type a message…" autofocus />
        <button class="btn" id="csend">Send</button></div>
    </div>`);
  async function load() {
    try {
      const b = await apiFetch('/messages/' + encodeURIComponent(driverId) + (docket ? '?docket=' + encodeURIComponent(docket) : ''));
      await apiFetch('/messages/' + encodeURIComponent(driverId) + '/read', { method: 'POST' }).catch(() => {});
      const log = $('#clog'); if (!log) return;
      log.innerHTML = b.messages.length ? b.messages.map(m => `
        <div class="msg ${m.direction === 'ops' ? 'ops' : 'driver'}">
          ${m.docket_number ? `<div class="msg tag">↳ ${esc(m.docket_number)}</div>` : ''}
          ${esc(m.text)}<div class="meta">${m.direction === 'ops' ? 'You' : 'Driver'} · ${esc((m.ts || '').replace('T', ' ').replace('Z', ''))}</div>
        </div>`).join('') : '<div class="muted">No messages yet.</div>';
      log.scrollTop = log.scrollHeight;
    } catch (e) { const l = $('#clog'); if (l) l.innerHTML = '<div class="err-txt">' + esc(e.message) + '</div>'; }
  }
  await load();
  async function send() {
    const inp = $('#cmsg'); const text = inp.value.trim(); if (!text) return;
    inp.value = '';
    try { await apiFetch('/messages/' + encodeURIComponent(driverId), { method: 'POST', body: JSON.stringify({ text, docket: docket || null }) }); await load(); await refreshDash(); }
    catch (e) { toast(e.message, true); inp.value = text; }
  }
  $('#csend').addEventListener('click', send);
  $('#cmsg').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); send(); } });
}

// ── boot ─────────────────────────────────────────────────────────────
async function boot() {
  try { S.user = JSON.parse(localStorage.getItem(LS.user) || 'null'); } catch (e) {}
  try {
    const me = await apiFetch('/me'); S.user = me.user;
    await refreshDash();
    const start = (location.hash || '').replace('#', '');
    go(NAV.some(n => n.id === start) ? start : 'dashboard');
  } catch (e) {
    if (e.status === 401 || e.code === 'unauthorized') return; // renderLogin already shown
    renderLogin();
  }
}
if (S.token) boot(); else renderLogin();
