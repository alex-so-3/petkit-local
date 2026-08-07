const BASE = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
const api = (p, o) => fetch(BASE + 'api/' + p, o).then(r => r.json());

// Markup escaping for the two entity-decoding contexts this file builds:
// TEXT nodes and DOUBLE-QUOTED attribute values. In both, an escaped string can
// no longer open a tag or close the attribute, so it lands as inert text.
// It is NOT enough for: unquoted attributes, URL schemes (a `javascript:` href
// survives escaping untouched), or anything the browser parses as CODE. That
// last case is why this file has no inline `on*=` handler attributes at all —
// see the delegation table below. Handler arguments travel in data-* attributes
// and are only ever read back as strings or JSON.parse'd, never evaluated.
const ESC_MAP = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
  '/': '&#x2F;',
};
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"'\/]/g, c => ESC_MAP[c]);
const fmtTs = t => (t ? new Date(t * 1000).toLocaleTimeString() : '—');
function relTime(t) {
  if (!t) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - t));
  if (s < 2) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function fmtBytes(n) {
  if (n == null) return '—';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}
let DEVICES = [];
let ACCESSORIES = [];
//: The last detail payload per device, so a panel can repaint from memory
//: instead of flashing empty while a refresh is in flight.
const DEV_DETAIL = new Map();
//: Open/closed state, keyed "<deviceId>:<section>". A MAP rather than a Set
//: because the two defaults differ — a device panel starts open, every
//: `<details class="adv">` inside it starts closed — and a set can only say
//: "closed unless listed". Keying by device id is what makes it survive N
//: panels; without it, expanding diagnostics on one device would expand it on
//: all of them.
const DEV_OPEN = new Map();
try {
  for (const [k, v] of JSON.parse(localStorage.getItem('devOpen') || '[]')) DEV_OPEN.set(k, !!v);
} catch (_) {}

function secOpen(id, sec, dflt) {
  const k = id + ':' + sec;
  return DEV_OPEN.has(k) ? DEV_OPEN.get(k) : dflt;
}
function setSecOpen(id, sec, open) {
  DEV_OPEN.set(id + ':' + sec, !!open);
  try {
    localStorage.setItem('devOpen', JSON.stringify([...DEV_OPEN]));
  } catch (_) {}
}
// `navigator.clipboard` exists only in a secure context, and this panel is
// normally plain HTTP on the LAN — the same constraint that keeps Web Bluetooth
// off the Provision tab. So execCommand, deprecated as it is, is the path that
// actually runs here; the modern one is the exception, not the fallback.
function copyText(text) {
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(
      () => toast('Copied'),
      () => toast('Could not copy — select it by hand'),
    );
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  document.body.removeChild(ta);
  toast(ok ? 'Copied' : 'Could not copy — select it by hand');
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), 2600);
}
// ---------------- Inline help ----------------
// A `?` next to a label, opening a small popover. Explanations that used to sit
// under a heading as a paragraph live in here instead, so the page reads as
// controls rather than as prose.
//
// The text travels in a data-* attribute rather than `title=`, for three
// reasons: the native tooltip never appears on touch, its delay and wrapping
// are the OS's to decide, and it cannot hold markup. It is escaped on the way
// in and assigned with textContent on the way out, so it is inert either way.
//
// The popover is a single element parented to <body>, NOT a sibling of the
// button: every container in this file is re-rendered wholesale through
// innerHTML, which would blow away an inline one mid-read. That does mean the
// anchor can vanish underneath it, hence the isConnected check while open.
const help = text =>
  `<button class="help" type="button" data-action="help" data-help="${esc(text)}" aria-label="What is this?">?</button>`;

let HELP_ANCHOR = null;
let HELP_WATCH = 0;

function closeHelp() {
  const pop = document.getElementById('helpPop');
  if (pop) pop.remove();
  if (HELP_ANCHOR) HELP_ANCHOR.setAttribute('aria-expanded', 'false');
  HELP_ANCHOR = null;
  clearInterval(HELP_WATCH);
  HELP_WATCH = 0;
}

// ---------------- Devices ----------------
function linkBadge(d) {
  // ONE badge, not three. This used to be a dot, an online/offline badge and a
  // separate MQTT badge, all reporting the same axis — and the middle one was
  // pure redundancy, since a device that is offline cannot be on MQTT and one
  // that is on MQTT is by definition online. What actually matters is which
  // transport a command will take, because MQTT is immediate and the heartbeat
  // queue is not.
  if (!d.online) return '<span class="badge off">offline</span>';
  return d.mqtt_connected
    ? '<span class="badge ok">MQTT</span>'
    : '<span class="badge">HTTP heartbeat</span>';
}

// ---------------- Event delegation ----------------
// Every container here is re-rendered wholesale through innerHTML, which would
// drop any listener bound to its children. So behaviour is bound ONCE at the
// document root and dispatched on the nearest [data-action]/[data-change]
// ancestor of the event target. The indirection is also the security property:
// a handler is picked from this table by name, so nothing device-supplied ever
// reaches a place the browser would parse as code.
const ACTIONS = {},
  CHANGES = {},
  INPUTS = {},
  TOGGLES = {};
const onAction = (name, fn) => {
  ACTIONS[name] = fn;
};
const onChange = (name, fn) => {
  CHANGES[name] = fn;
};
// A <details> fires `toggle`, which — like `error` below — does NOT bubble, so
// it is caught in the capture phase. `delegate` still works unchanged: on a
// non-bubbling capture-phase event `ev.target` IS the <details>, so a nested
// one is attributed to itself rather than to its parent. Reading `.open` from a
// click on the <summary> would instead need a next-tick hack, because the
// browser has not flipped the attribute yet at that point.
const onToggle = (name, fn) => {
  TOGGLES[name] = fn;
};
// `change` on a range input only fires when the pointer is RELEASED, so a
// slider wired through it appears frozen while you drag. `input` fires on every
// step — including arrow keys — which is what a live control needs.
const onInput = (name, fn) => {
  INPUTS[name] = fn;
};
function delegate(table, attr) {
  return ev => {
    const el = ev.target && ev.target.closest ? ev.target.closest('[data-' + attr + ']') : null;
    if (!el) return;
    const fn = table[el.dataset[attr]];
    if (fn) fn(el, ev);
  };
}
document.addEventListener('click', delegate(ACTIONS, 'action'));
document.addEventListener('change', delegate(CHANGES, 'change'));
document.addEventListener('input', delegate(INPUTS, 'input'));
document.addEventListener('toggle', delegate(TOGGLES, 'toggle'), true);
// 'error' does not bubble, so a broken <img> can only be caught on the way DOWN.
document.addEventListener(
  'error',
  ev => {
    const t = ev.target;
    if (t && t.classList && t.classList.contains('hide-on-error')) t.style.display = 'none';
  },
  true,
);

// Registered down here, not beside `help()` above: `onAction` is a `const` in
// the delegation table below, so calling it earlier hits the temporal dead zone
// and takes the whole script — every tab — down with it.
onAction('help', (el, ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  const same = HELP_ANCHOR === el;
  closeHelp();
  if (same) return; // second click on the same `?` closes it

  const pop = document.createElement('div');
  pop.id = 'helpPop';
  pop.setAttribute('role', 'tooltip');
  pop.textContent = el.dataset.help || '';
  document.body.appendChild(pop);

  // Placed after appending so the width is known. Clamped to the viewport so a
  // `?` near the right edge does not push the popover off-screen — the panel
  // has controls hard against that edge.
  const r = el.getBoundingClientRect();
  const w = pop.offsetWidth;
  const left = Math.max(8, Math.min(r.left + window.scrollX, window.innerWidth - w - 8));
  pop.style.left = left + 'px';
  pop.style.top = r.bottom + window.scrollY + 6 + 'px';

  el.setAttribute('aria-expanded', 'true');
  HELP_ANCHOR = el;
  HELP_WATCH = setInterval(() => {
    if (!el.isConnected) closeHelp();
  }, 500);
});

document.addEventListener('click', ev => {
  if (HELP_ANCHOR && !ev.target.closest('#helpPop') && !ev.target.closest('[data-action="help"]'))
    closeHelp();
});
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape') closeHelp();
});
window.addEventListener('scroll', closeHelp, true);
window.addEventListener('resize', closeHelp);

document.querySelectorAll('nav button').forEach(b =>
  b.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    ['devices', 'timeline', 'pets', 'provision', 'log', 'capture', 'patchers', 'setup'].forEach(t =>
      document.getElementById('tab-' + t).classList.toggle('hidden', t !== b.dataset.tab),
    );
    // Devices needs an entry now: refreshes are skipped while the tab is
    // hidden, so coming back to it has to repaint rather than wait for a tick.
    if (b.dataset.tab === 'devices') loadDevices();
    if (b.dataset.tab === 'log') loadDeviceLogs();
    if (b.dataset.tab === 'capture') loadCapture();
    if (b.dataset.tab === 'patchers') loadPatchers();
    if (b.dataset.tab === 'setup') loadSetup();
    if (b.dataset.tab === 'provision') loadProvision();
    if (b.dataset.tab === 'timeline') loadTimeline();
    if (b.dataset.tab === 'pets') loadPets();
  }),
);

// ---------------- Devices ----------------
// One <details> panel per device, all expanded by default. The container is
// rebuilt wholesale ONLY when the set of device ids changes; every other
// refresh writes a single panel's summary and body. That is what keeps a
// WebSocket update on one device from throwing away another device's scroll
// position, open sections and half-typed input.
async function loadDevices() {
  const [ds, bleResp] = await Promise.all([api('devices'), api('ble')]);
  DEVICES = ds;
  // An accessory is its own device here, not a row in its parent's card: in
  // Home Assistant it already is one, and a CTW3 carries 21 entities and 8
  // controls — more than a purifier. What it does NOT get is the parent's
  // machinery (counters, queue, patchers, proxy): it has no network of its
  // own, so every one of those would be a zero pretending to be a reading.
  ACCESSORIES = (bleResp && bleResp.accessories) || [];
  const box = document.getElementById('devPanels');
  if (!box) return;

  if (!ds.length) {
    box.innerHTML =
      '<div class="empty"><div class="big">🐾</div><b>No devices connected yet</b>' +
      '<p class="mut">Provision a PetKit device over Bluetooth, or point its <code>apiServers</code> URL at this app.<br>Open the <a data-action="goto-tab" data-tab="provision">Provision</a> tab to start.</p></div>';
    return;
  }

  // Forget open-state for devices that no longer exist, so the stored map does
  // not grow forever across re-registrations.
  const live = new Set([
    ...ds.map(d => String(d.id)),
    ...ACCESSORIES.map(a => String(a.petkit_id)),
  ]);
  let pruned = false;
  for (const k of [...DEV_OPEN.keys()]) {
    if (!live.has(k.split(':')[0])) {
      DEV_OPEN.delete(k);
      pruned = true;
    }
  }
  if (pruned) {
    try {
      localStorage.setItem('devOpen', JSON.stringify([...DEV_OPEN]));
    } catch (_) {}
  }

  // Each accessory follows the device that relays for it, so the relationship
  // reads out of the order rather than out of nesting.
  const order = [];
  for (const d of ds) {
    order.push({ kind: 'dev', d });
    for (const a of ACCESSORIES.filter(a => a.link_with === d.id)) order.push({ kind: 'ble', a });
  }
  for (const a of ACCESSORIES.filter(a => !ds.some(d => d.id === a.link_with)))
    order.push({ kind: 'ble', a });

  const rendered = [...box.querySelectorAll('[data-panel-key]')].map(p => p.dataset.panelKey);
  const wanted = order.map(o => (o.kind === 'dev' ? 'd' + o.d.id : 'a' + o.a.petkit_id));
  if (rendered.join(',') !== wanted.join(',')) {
    box.innerHTML = order.map(o => (o.kind === 'dev' ? panelShell(o.d) : accShell(o.a))).join('');
  }
  for (const d of ds) {
    const panel = devPanel(d.id);
    if (!panel) continue;
    panel.querySelector('summary').innerHTML = panelSummary(d);
    if (panel.open) {
      if (DEV_DETAIL.has(d.id)) paintPanelBody(d.id);
      else scheduleDetail(d.id);
    }
  }
  for (const a of ACCESSORIES) {
    const panel = document.querySelector(
      '.accpanel[data-id="' + CSS.escape(String(a.petkit_id)) + '"]',
    );
    if (!panel) continue;
    // No lazy detail fetch: /api/ble already carries the whole accessory,
    // entities and resolved values included.
    panel.querySelector('summary').innerHTML = accSummary(a);
    if (panel.open) panel.querySelector('.devbody').innerHTML = accBody(a);
  }
}
function gotoTab(t) {
  document.querySelector('nav button[data-tab="' + t + '"]').click();
}
onAction('goto-tab', el => gotoTab(el.dataset.tab));

const devPanel = id =>
  document.querySelector('.devpanel[data-id="' + CSS.escape(String(id)) + '"]');

function panelShell(d) {
  return `<details class="devpanel" data-panel-key="d${esc(d.id)}" data-toggle="dev-panel" data-id="${esc(d.id)}" data-sec="panel" ${
    secOpen(d.id, 'panel', true) ? 'open' : ''
  }><summary class="dh"></summary><div class="devbody"></div></details>`;
}

function accShell(a) {
  return `<details class="devpanel accpanel" data-panel-key="a${esc(a.petkit_id)}" data-toggle="acc-panel" data-id="${esc(a.petkit_id)}" data-sec="panel" ${
    secOpen(a.petkit_id, 'panel', true) ? 'open' : ''
  }><summary class="dh"></summary><div class="devbody"></div></details>`;
}
onAction('ble-read-now', async el => {
  const r = await api('ble/' + el.dataset.id + '/poll', { method: 'POST' });
  toast(r && r.ok ? 'Asked the relay for a reading' : 'Error: ' + ((r && r.error) || '?'));
  loadDevices();
});
onToggle('acc-panel', el => {
  const id = Number(el.dataset.id);
  setSecOpen(id, 'panel', el.open);
  if (el.open) {
    const a = ACCESSORIES.find(x => x.petkit_id === id);
    if (a) el.querySelector('.devbody').innerHTML = accBody(a);
  }
});

// `linkBadge`'s two answers — MQTT or HTTP heartbeat — are both untrue of an
// accessory: it has no session of its own and no heartbeat queue to fall back
// to. Everything reaches it over Bluetooth, through whoever relays for it.
// Offline still wins, because without that parent on the network the accessory
// is not merely idle, it is unaddressable.
function accSummary(a) {
  const seen = a.last_seen ? relTime(a.last_seen) : 'never reported';
  return `<span class="dot ${a.parent_online ? 'on' : 'off'}"></span>
    <b class="dp-name" title="${esc(a.name)}">${esc(a.name)}</b>
    <span class="chip">${esc(a.ble_type.toUpperCase())} · #${esc(a.petkit_id)}</span>
    ${a.parent_online ? '<span class="badge ble">BLE</span>' : '<span class="badge off">offline</span>'}
    ${a.parent_name ? `<span class="badge">relayed by ${esc(a.parent_name)}</span>` : '<span class="badge bad">no parent</span>'}
    <span class="grow"></span>
    <span class="mut dp-meta">${esc((a.entities || []).length)} entities · ${esc(seen)}</span>`;
}

function accBody(a) {
  const ents = a.entities || [];
  const sensors = ents.filter(e => e.component === 'sensor' || e.component === 'binary_sensor');
  const controls = ents.filter(e => ['switch', 'number', 'select'].includes(e.component));
  const actions = ents.filter(e => e.component === 'button');
  return `<div class="dh"></div>
  <div class="meta">
    <div><div class="m-k">Bluetooth address</div><div class="m-v"><code>${esc(a.mac)}</code></div></div>
    <div><div class="m-k">Relayed by</div><div class="m-v">${a.parent_name ? esc(a.parent_name) + ' #' + esc(a.link_with) : '—'}</div></div>
    <div><div class="m-k">Serial</div><div class="m-v">${esc(a.serial_number || '—')}</div></div>
    <div><div class="m-k">Last reading</div><div class="m-v">${a.last_seen ? esc(relTime(a.last_seen)) : 'never'}</div></div>
    <div><div class="m-k">Poll interval</div><div class="m-v">${esc(a.interval)}s</div></div>
    <div><div class="m-k">Scan type</div><div class="m-v">${esc(a.wire_entry ? a.wire_entry.type : '—')}</div></div>
  </div>
  ${
    a.scan_type_is_guessed
      ? `<div class="card notice"><b>The scan type for this model is a guess.</b>
    <p class="sub" style="margin:6px 0 0">Nobody has captured what a real ${esc(a.ble_type.toUpperCase())} pairing carries, so
    <code>${esc(a.wire_entry ? a.wire_entry.type : '?')}</code> is borrowed from its product line. If this accessory never reports,
    that number is the first thing to change — unpair and pair again with a different <b>Scan type</b> under Advanced,
    and please say which value worked.</p></div>`
      : ''
  }
  <div class="card"><h3>Actions${help(
    'Read now is about the relay rather than the accessory: it speaks only when its parent is told to open a Bluetooth session, and otherwise that happens on a timer up to the poll interval away. The rest are the accessory’s own — a filter reset carries no settings and is the one write that works before it has ever reported.',
  )}</h3>
    <div><button class="act" data-action="ble-read-now" data-id="${esc(a.petkit_id)}">Read now</button>${actions
      .map(
        e =>
          `<button class="act" data-action="press-entity" data-id="${esc(a.petkit_id)}" data-key="${esc(e.key)}" data-kind="ble" data-name="${esc(e.name)}">${esc(e.name)}</button>`,
      )
      .join('')}</div></div>
  ${
    sensors.length
      ? `<div class="card"><h3>State</h3>
    ${entityTable({ id: a.petkit_id, entities: ents }, sensors)}
    <details class="adv"><summary>Raw parsed state JSON</summary><pre>${esc(JSON.stringify(a.state, null, 2))}</pre></details></div>`
      : `<div class="card"><h3>State</h3><p class="mut" style="margin:0">Nothing reported yet. An accessory only speaks when its parent is asked to open a Bluetooth session to it.</p></div>`
  }
  ${
    controls.length
      ? `<div class="card"><h3>Controls</h3><div class="ctrls">${controls
          .map(e => controlRow(a.petkit_id, e, 'ble'))
          .join('')}</div>
    <p class="sub" style="margin:8px 0 0">Settings travel as one block, so a change needs a reading first — before the accessory has reported, a write is refused rather than guessed at.</p></div>`
      : ''
  }`;
}

// The header stays readable when collapsed, so it carries everything the old
// grid card showed — including the BLE accessory line, which lived ONLY on that
// card and would otherwise have been lost with it.
function panelSummary(d) {
  return `<span class="dot ${d.online ? 'on' : 'off'}"></span>
    <b class="dp-name" title="${esc(d.name)}">${esc(d.name)}</b>
    <span class="chip">${esc(d.type.toUpperCase())} · #${esc(d.id)}</span>
    ${linkBadge(d)}
    ${d.ble.length ? `<span class="badge">+${esc(d.ble.length)} BLE: ${d.ble.map(b => esc(b.type.toUpperCase())).join(', ')}</span>` : ''}
    <span class="grow"></span>
    <span class="mut dp-meta">${esc(d.entities)} entities · ${esc(d.queue)} queued · ${relTime(d.last_heartbeat || d.last_state_report || d.last_seen)}</span>`;
}

onToggle('dev-panel', el => {
  const id = Number(el.dataset.id);
  setSecOpen(id, 'panel', el.open);
  if (el.open) {
    if (DEV_DETAIL.has(id)) paintPanelBody(id);
    scheduleDetail(id);
  } else {
    // Drop the markup of a collapsed panel: it is not visible, it would keep
    // refreshing pointlessly, and with several devices it is a lot of DOM.
    const body = el.querySelector('.devbody');
    if (body) body.innerHTML = '';
  }
});
onToggle('dev-sec', el => setSecOpen(Number(el.dataset.id), el.dataset.sec, el.open));

// One request per device: the sidecars the detail view needs are folded into
// `api_device_detail` server-side. They used to be three extra round trips,
// which with a panel per device refreshing on every tick was four times the
// traffic for data that changes only when the user changes it.
async function fetchDeviceDetail(id) {
  return api('devices/' + id);
}

function paintPanelBody(id) {
  const panel = devPanel(id);
  if (!panel || !panel.open) return;
  const body = panel.querySelector('.devbody');
  const d = DEV_DETAIL.get(id);
  if (!body || !d) return;
  // Never redraw under the user's cursor: with N panels auto-refreshing every
  // ~10s, a repaint would silently wipe a half-typed raw command or schedule.
  if (body.contains(document.activeElement)) return;
  body.innerHTML = renderPanelBody(d);
}

// The device echoes back which mugshots it actually holds, as the
// `discernPic` array of face ids in its state report. That readback is the one
// proof a dev_discern_pic payload was accepted: ids it does not recognise mean
// it is still matching against photos cached from PetKit's cloud.
function facesOnDevice(d, ourFaceIds) {
  const cached = (d.state || {}).discernPic;
  if (!Array.isArray(cached)) return '';
  if (!cached.length)
    return `<p class="sub">No mugshots cached on the device yet. It re-reads them at boot and
      about once an hour; a reboot forces it.</p>`;
  // Comparing against our own ids is the whole value of this readback: matching
  // ids prove the device took OUR mugshots, foreign ones say it is still
  // matching against PetKit's — which is exactly what proxy mode serves it.
  const ours = ourFaceIds || [];
  const foreign = cached.filter(id => !ours.includes(id));
  let verdict = '';
  if (ours.length && !foreign.length) {
    verdict = ' — yours, so the device is matching against your mugshots.';
  } else if (ours.length) {
    verdict = ` — <b>not yours</b>. It is still using photos from PetKit's cloud, which is what
      proxy mode hands it. Turn proxy off and reboot the device to switch it to your
      ${ours.length} mugshot${ours.length === 1 ? '' : 's'}.`;
  }
  return `<p class="sub">Cached on device: ${cached.length} mugshot${
    cached.length === 1 ? '' : 's'
  } <code>${esc(cached.join(', '))}</code>${verdict}</p>`;
}

const CAP_LABELS = {
  fullVideo: 'Recordings',
  eventImage: 'Snapshots',
  highLight: 'Highlights',
  dynamicVideo: 'Motion Clips',
};
function capRow(id, ct, on) {
  return `<label class="ctrl"><span>${esc(CAP_LABELS[ct] || ct)}</span>
    <span class="sw"><input type="checkbox" ${on ? 'checked' : ''} data-change="set-capability" data-id="${esc(id)}" data-cap="${esc(ct)}"><span class="sl"></span></span></label>`;
}
onChange('set-capability', el => setCapability(Number(el.dataset.id), el.dataset.cap, el.checked));
async function setCapability(id, ct, on) {
  const r = await api('devices/' + id + '/capabilities', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [ct]: on }),
  });
  toast(r.ok ? 'Saved' : 'Error: ' + (r.error || 'failed'));
}
onAction('ble-pair', async el => {
  const box = el.closest('details');
  const val = c => (box.querySelector('.' + c) || {}).value || '';
  const r = await api('ble', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ble_type: val('ble-type'),
      petkit_id: Number(val('ble-id')) || 0,
      mac: val('ble-mac'),
      secret: val('ble-secret'),
      interval: Number(val('ble-interval')) || 240,
      serial_number: val('ble-sn'),
      scan_type: Number(val('ble-scan-type')) || 0,
      link_with: Number(el.dataset.id),
    }),
  });
  if (r.error) return toast('Error: ' + r.error);
  toast('Paired — the device picks it up on its next poll');
  loadDevices();
});

onAction('ble-unpair', async el => {
  if (
    !confirm(
      `Unpair the ${el.dataset.name}?\n\nIts Home Assistant entities stay until you delete the device there.`,
    )
  )
    return;
  const r = await api('ble/' + encodeURIComponent(el.dataset.id), { method: 'DELETE' });
  toast(r.ok ? 'Unpaired' : 'Error: ' + (r.error || 'failed'));
  loadDevices();
});

onChange('set-log-upload', el => setLogUpload(Number(el.dataset.id), el.checked));
async function setLogUpload(id, on) {
  const r = await api('devices/' + id + '/logs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log_upload_enabled: on }),
  });
  toast(r.ok ? 'Saved' : 'Error: ' + (r.error || 'failed'));
}
onChange('set-ai', el => setAiEnabled(Number(el.dataset.id), el.checked));
async function setAiEnabled(id, on) {
  const r = await api('devices/' + id + '/ai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ai_enabled: on }),
  });
  toast(r.ok ? 'Saved' : 'Error: ' + (r.error || 'failed'));
  // This toggle decides whether the pets section exists at all, so the view has
  // to be rebuilt — otherwise it saves and nothing visibly happens, which reads
  // as the switch not working.
  loadPets();
}

// Which card each HA component lands in. Declared rather than implied by a
// chain of filters, so a component that gains no home shows up as a visible
// warning instead of silently vanishing from the panel while still being
// published to Home Assistant — which is exactly what happened to `text`,
// `event`, `camera` and `image`.
const ENTITY_SECTION = {
  sensor: 'state',
  binary_sensor: 'state',
  switch: 'controls',
  number: 'controls',
  select: 'controls',
  button: 'actions',
  text: 'schedules',
  // No card of its own. An event entity is momentary — it fires and keeps no
  // value — so the card that used to list them could only ever print the
  // event_types they declare, never anything that happened. What happened is
  // the Timeline's job. They are still reachable, under "Show every entity".
  event: 'entity-list-only',
  camera: 'camera',
  image: 'camera',
};

function renderPanelBody(d) {
  const ents = d.entities || [];
  const section = e => ENTITY_SECTION[e.component];
  // Capability toggles get their own card below, and the AI detections live on
  // the AI / Pets tab next to the pets they are about — same underlying
  // entities/API, friendlier grouping. Both excluded here or they appear twice.
  //
  // The AI half is conditional on this device HAVING that card: a feeder shares
  // `pet_detection` with the fountain but has no on-device AI, so relocating it
  // there would delete the control rather than move it.
  const relocated = e =>
    e.value_path.startsWith('capabilities.') || (d.supports_ai && AI_ENTITY_KEYS.includes(e.key));
  // `config`/`diagnostic` sort last within their card, the way HA groups them.
  // Grouping only — filtering any of it out would re-create the divergence in
  // the other direction.
  const byCategory = (a, b) => (a.entity_category ? 1 : 0) - (b.entity_category ? 1 : 0);

  const sensors = ents.filter(e => section(e) === 'state').sort(byCategory);
  const controls = ents.filter(e => section(e) === 'controls' && !relocated(e)).sort(byCategory);
  const schedules = ents.filter(e => section(e) === 'schedules');
  const media = ents.filter(e => section(e) === 'camera');
  const unrouted = ents.filter(e => !section(e));
  const hbAgo = d.last_heartbeat ? relTime(d.last_heartbeat) : 'never';
  return `
  <div class="card">
    <div class="meta">
      <div><div class="m-k">Serial</div><div class="m-v">${esc(d.sn) || '—'}</div></div>
      <div><div class="m-k">MAC</div><div class="m-v">${esc(d.mac) || '—'}</div></div>
      <div><div class="m-k">Firmware</div><div class="m-v">${esc(d.firmware) || '—'}</div></div>
      <div><div class="m-k">Last heartbeat</div><div class="m-v">${hbAgo}</div></div>
      <div><div class="m-k">Last state</div><div class="m-v">${d.last_state_report ? relTime(d.last_state_report) : 'never'}</div></div>
      <div><div class="m-k">Queued cmds</div><div class="m-v">${esc(d.queue)}</div></div>
      <div><div class="m-k">HTTP / MQTT</div><div class="m-v">${esc(d.http_count)} / ${esc(d.mqtt_count)}</div></div>
      <div><div class="m-k">ProductKey</div><div class="m-v"><code>${esc(d.pk)}</code></div></div>
      <div><div class="m-k">DeviceName</div><div class="m-v"><code>${esc(d.dn)}</code></div></div>
    </div>
  </div>

  ${
    !d.actions.length && !controls.length && !sensors.length
      ? `<div class="card">
    <p class="sub" style="margin:0">This device type (<code>${esc(d.type.toUpperCase())}</code>) isn\'t recognized, so no Home Assistant entities are defined for it. It still connects, appears here, and its traffic is logged — you can send raw commands below.</p></div>`
      : ''
  }

  ${
    d.actions.length
      ? `<div class="card"><h3>Actions${help(
          d.mqtt_connected
            ? 'This device has a live MQTT session, so a command reaches it immediately.'
            : "This device is not on MQTT, so a command waits in a queue until its next HTTP heartbeat — about 10 seconds. Nothing is lost, it just isn't instant.",
        )}</h3>
    <div>${d.actions.map(a => `<button class="act${a.destructive ? ' danger' : ''}" data-action="send-action" data-id="${esc(d.id)}" data-key="${esc(a.key)}" data-destructive="${a.destructive ? 1 : 0}" data-name="${esc(a.name)}">${esc(a.name)}</button>`).join('')}</div>
    <span class="cmd-out mut" style="display:inline-block;margin-top:6px"></span></div>`
      : ''
  }

  ${
    controls.length
      ? `<div class="card"><h3>Controls</h3>
    <div class="ctrls">${controls.map(e => controlRow(d.id, e)).join('')}</div></div>`
      : ''
  }

  ${
    sensors.length
      ? `<div class="card"><h3>State${help(
          'What this device is reporting right now. "Show every entity" adds the controls, buttons and event entities too — the full list Home Assistant has, so you can check the two agree.',
        )}</h3>
    ${entityTable(d, sensors)}
    <label class="showall"><input type="checkbox" data-change="show-all-entities" data-id="${esc(d.id)}" ${SHOW_ALL.has(d.id) ? 'checked' : ''}> <span class="mut">Show every entity (${esc(ents.length)})</span></label>
    <details class="adv" data-toggle="dev-sec" data-id="${esc(d.id)}" data-sec="rawstate" ${secOpen(d.id, 'rawstate', false) ? 'open' : ''}><summary>Raw parsed state JSON</summary><pre>${esc(JSON.stringify(d.state, null, 2))}</pre></details></div>`
      : ''
  }

  ${
    d.is_camera && d.capInfo
      ? `<div class="card"><h3>Media Capabilities${help(
          "Turns the upload off at the source: the device stops sending that media type at all, so there is nothing to prune and no bandwidth spent. Takes effect on the device's next STS poll.",
        )}</h3>
    <div class="ctrls">${Object.entries(d.capInfo.capabilities)
      .map(([ct, on]) => capRow(d.id, ct, on))
      .join('')}</div></div>`
      : ''
  }

  ${
    d.logInfo
      ? `<div class="card"><h3>Debug log collection${help(
          'Lets the device upload its own devRun.log here instead of to PetKit. Needs the cacert patcher, since the upload is HTTPS to our self-signed bucket. The device decides when to send one — a reboot is the usual trigger.',
        )}</h3>
    <p class="sub">The firmware's own view of what it was doing, in the <a data-action="goto-tab" data-tab="log">Log</a> tab.${
      d.logInfo.reason
        ? ` <b>Cannot collect right now:</b> ${esc(DEVLOG_REASONS[d.logInfo.reason] || d.logInfo.reason)}.`
        : ''
    }</p>
    <div class="ctrls"><label class="ctrl"><span>Collect debug logs</span>
      <span class="sw"><input type="checkbox" ${d.logInfo.log_upload_enabled ? 'checked' : ''} data-change="set-log-upload" data-id="${esc(d.id)}"><span class="sl"></span></span></label></div></div>`
      : ''
  }


  <div class="card"><h3>BLE accessories${help(
    "The device never goes looking for accessories: it asks us for a list of Bluetooth addresses and scans for exactly those, and no firmware can report a new one back. Pairing normally happens in PetKit's app, so with the app gone it has to be entered here.",
  )}</h3>
    <p class="sub">A Pura Air spray or an EverSweet fountain has no network of its own — this device relays for it over Bluetooth.</p>
    ${
      (d.ble || []).length
        ? `<table class="stbl"><tbody>${d.ble
            .map(
              b => `<tr><td><b>${esc(b.type.toUpperCase())}</b> <span class="mut">#${esc(b.id)}</span></td>
        <td><code>${esc(b.mac)}</code></td>
        <td><a class="danger-link" data-action="ble-unpair" data-id="${esc(b.id)}" data-name="${esc(b.type.toUpperCase())}">unpair</a></td></tr>`,
            )
            .join('')}</tbody></table>`
        : '<p class="mut" style="margin:0 0 10px">Nothing paired.</p>'
    }
    <details class="adv" data-toggle="dev-sec" data-id="${esc(d.id)}" data-sec="blepair" ${secOpen(d.id, 'blepair', false) ? 'open' : ''}>
      <summary>Pair an accessory</summary>
      <p class="sub">You need two things off the accessory: its <b>Bluetooth address</b> and its <b>pairing secret</b>. Both come from the accessory itself or from PetKit's app — there is no way to work them out here, and the accessory will refuse the connection without the right secret.</p>
      <div class="row">
        <div class="col"><label class="mut">Accessory</label>
          <select class="ble-type"><option value="w5">EverSweet 3 Pro / Solo (W5)</option><option value="w4">EverSweet (W4)</option><option value="ctw2">EverSweet Solo 2 (CTW2)</option><option value="ctw3">EverSweet Max Cordless (CTW3)</option><option value="k3">Pura Air spray (K3)</option></select></div>
        <div class="col"><label class="mut">Bluetooth address (MAC)</label><input class="ble-mac" placeholder="AA:BB:CC:DD:EE:FF"></div>
      </div>
      <div class="row" style="margin-top:8px">
        <div class="col"><label class="mut">Pairing secret${help(
          'Eight bytes, written into the accessory once and kept there until a factory reset. PetKit’s cloud generates it the first time the app sets the fountain up, and the accessory then refuses any session that presents a different one — so a fountain that has already been through PetKit’s app needs THAT secret, not a new one. A factory-fresh accessory has none and accepts eight zero bytes, which is what a reset gets you back to.',
        )}</label><input class="ble-secret" placeholder="from the accessory"></div>
        <div class="col"><label class="mut">Name it (optional)</label><input class="ble-sn" placeholder="serial or a label"></div>
      </div>
      <details class="adv" style="margin-top:10px"><summary>Advanced</summary>
        <div class="row" style="margin-top:8px">
          <div class="col"><label class="mut">Identifier${help(
            "A number this app uses to refer to the accessory; it becomes its Home Assistant device id. The relay only ever reports back by Bluetooth address, so this one is ours to pick. Leave it blank unless you want to reuse the id PetKit's app gave it.",
          )}</label><input class="ble-id" placeholder="chosen for you"></div>
          <div class="col"><label class="mut">Poll interval (s)${help(
            'How often the relay opens a Bluetooth session to read the accessory.',
          )}</label><input class="ble-interval" value="240"></div>
          <div class="col"><label class="mut">Scan type${help(
            'The number the relay is told to scan for. 14 is the only value anybody has read off a real pairing, and that was a W5 — the other fountains reuse it because they are the same Bluetooth family, which is a guess. If one of them never reports, this is the field to try another number in. Leave blank to use the default, and please say which value worked.',
          )}</label><input class="ble-scan-type" placeholder="default"></div>
        </div>
      </details>
      <div style="margin-top:10px"><button class="act" data-action="ble-pair" data-id="${esc(d.id)}">Pair accessory</button></div>
    </details>
  </div>

  ${
    schedules.length
      ? `<div class="card"><h3>Schedules${help(
          'The raw JSON the device fetches on its own clock. Home Assistant exposes these as text entities but caps them at 255 characters, so a longer schedule can only be edited here.',
        )}</h3>
    ${schedules
      .map(
        e => `<div class="sched"><label class="mut">${esc(e.name)}</label>
      <textarea class="sched-json" rows="4" data-id="${esc(d.id)}" data-key="${esc(e.key)}">${esc(typeof e.value === 'string' ? e.value : JSON.stringify(e.value ?? '', null, 2))}</textarea>
      <button class="ghost act" data-action="save-schedule" data-id="${esc(d.id)}" data-key="${esc(e.key)}">Save ${esc(e.name)}</button></div>`,
      )
      .join('')}</div>`
      : ''
  }

  ${
    media.length
      ? `<div class="card"><h3>Camera &amp; media${help(
          'Live view comes from this app’s own go2rtc, which reads the device and republishes it as RTSP. It only pulls from the device while somebody is watching. Recordings arrive whether or not any of this is set up.',
        )}</h3>
    <p class="sub">Recorded clips and snapshots are in the <a data-action="goto-tab" data-tab="timeline">Timeline</a> and in Home Assistant's media browser.</p>
    ${
      // The stream addresses live HERE and not on the Patchers tab: that card is
      // about applying and undoing a change to the firmware, and once it is
      // applied the address is just another fact about the device.
      d.streams && d.streams.rtsp
        ? `<div style="margin:6px 0 10px"><label class="mut">Live view — give this one to Home Assistant${help(
            'Add it as a Generic Camera. Do NOT use the device’s own address below for that: Home Assistant opens a stream with PyAV, and the device’s FLV segfaults libav and restarts the whole of HA. go2rtc stands between the two.',
          )}</label>
      <div style="display:flex;gap:6px;margin-top:4px;align-items:center"><code style="flex:1;overflow-x:auto">${esc(d.streams.rtsp)}</code><button class="mini" data-action="copy-url" data-url="${esc(d.streams.rtsp)}">Copy</button></div>
      <details class="adv" style="margin-top:6px"><summary>Straight from the device (VLC, ffmpeg — not Home Assistant)</summary>
        ${Object.entries(d.streams)
          .filter(([k]) => k !== 'rtsp')
          .map(
            ([k, url]) =>
              `<div style="display:flex;gap:6px;margin-top:4px;align-items:center"><span class="mut" style="min-width:56px">${esc(k)}</span><code style="flex:1;overflow-x:auto">${esc(url)}</code><button class="mini" data-action="copy-url" data-url="${esc(url)}">Copy</button></div>`,
          )
          .join('')}</details></div>`
        : `<p class="sub mut">No live stream. The device is only checked for one every few minutes, and it answers with a stream once <b>Local Camera Streaming</b> is applied on the <a data-action="goto-tab" data-tab="patchers">Patchers</a> tab${d.state && d.state.ip ? '' : ' — and it has not reported an IP yet either'}.</p>`
    }
    <table class="stbl"><tbody>${
      // The camera/image entities themselves are deliberately NOT listed with a
      // value: they carry no `value_path` (their bytes go to their own topic),
      // so every row read "—" and the table said nothing. What is worth showing
      // is where the media actually is.
      d.state && d.state.lastClipPath
        ? `<tr><td>Last clip</td><td colspan="2"><code>${esc(d.state.lastClipPath)}</code></td></tr>`
        : ''
    }<tr><td>Entities</td><td colspan="2" class="mut">${media.map(e => `<code>${esc(e.key)}</code>`).join(' ')}</td></tr></tbody></table></div>`
      : ''
  }

  ${
    unrouted.length
      ? `<div class="card" style="border-color:var(--warn)"><h3>Not shown above</h3>
    <p class="sub">These entities are published to Home Assistant but this panel has no card for them, which means <code>ENTITY_SECTION</code> in <code>app.js</code> is missing an entry. Please report it.</p>
    <table class="stbl"><tbody>${unrouted
      .map(
        e => `<tr><td>${esc(e.name)}</td><td>${esc(e.component)}</td><td>${esc(e.key)}</td></tr>`,
      )
      .join('')}</tbody></table></div>`
      : ''
  }

  <div class="card"><details class="adv" data-toggle="dev-sec" data-id="${esc(d.id)}" data-sec="diag" ${secOpen(d.id, 'diag', false) ? 'open' : ''}><summary>Diagnostics &amp; raw payloads</summary>
    <div class="row" style="margin-top:8px">
      <div class="col"><b>Last HTTP state_report</b><pre>${esc(JSON.stringify(d.diag.last_state_report || { note: 'none yet' }, null, 2))}</pre></div>
      <div class="col"><b>Last MQTT property.post</b><pre>${esc(JSON.stringify(d.diag.last_property || { note: 'none yet' }, null, 2))}</pre></div>
    </div>
    <b>MQTT connection</b><pre>${esc(JSON.stringify(d.diag.last_connect || { note: 'no MQTT connect seen — device is on HTTP heartbeat' }, null, 2))}</pre>
  </details></div>

  <div class="card"><details class="adv" data-toggle="dev-sec" data-id="${esc(d.id)}" data-sec="raw" ${secOpen(d.id, 'raw', false) ? 'open' : ''}><summary>Advanced: send raw command</summary>
    <p class="sub">Publishes VERBATIM — nothing is added. The firmware dispatches on <code>method</code>, so an envelope without one does nothing at all. Sent over MQTT if the device has a session, else queued for its heartbeat.</p>
    <textarea class="cmd-raw" rows="3" spellcheck="false"></textarea>
    <div style="margin-top:8px"><button class="act" data-action="send-raw" data-id="${esc(d.id)}">Send raw</button></div>
    <p class="sub" style="margin-top:10px">Examples — click one to load it:</p>
    <div>${RAW_EXAMPLES.map(
      x =>
        `<button class="act mini-ex" data-action="load-raw-example" data-body="${esc(JSON.stringify(x.body))}">${esc(x.label)}</button>`,
    ).join('')}</div>
  </details></div>`;
}

//: Worked examples for the raw-command box. They were a `placeholder` before,
//: which vanishes the moment you type and cannot be got back — so the one
//: reference for the envelope shape disappeared exactly when it was needed.
//: Every one of these is a shape this add-on really sends (see ha/commands.py).
const RAW_EXAMPLES = [
  {
    label: 'Change a setting',
    body: {
      suffix: 'property/set',
      payload: {
        method: 'thing.service.property.set',
        id: '1',
        version: '1.0.0',
        params: { autoWork: 1 },
      },
    },
  },
  {
    label: 'Start an action',
    body: {
      suffix: 'start',
      payload: {
        method: 'thing.service.start',
        id: '1',
        version: '1.0.0',
        params: { start_action: 0 },
      },
    },
  },
  {
    label: 'Reset the N60 deodorant',
    body: {
      suffix: 'start',
      payload: {
        method: 'thing.service.start',
        id: '1',
        version: '1.0.0',
        params: { start_action: 10 },
      },
    },
  },
];

onAction('load-raw-example', el => {
  const box = el.closest('details');
  const ta = box && box.querySelector('.cmd-raw');
  if (!ta) return;
  try {
    ta.value = JSON.stringify(JSON.parse(el.dataset.body), null, 2);
  } catch (_) {
    return;
  }
  ta.focus();
});

onAction('save-schedule', el => {
  const box = el.closest('.sched');
  const ta = box && box.querySelector('.sched-json');
  if (!ta) return;
  try {
    JSON.parse(ta.value);
  } catch (e) {
    return toast('Invalid JSON — not sent');
  }
  setEntity(Number(el.dataset.id), el.dataset.key, ta.value);
});
function fmtVal(v) {
  if (v === null || v === undefined) return '<span class="mut">—</span>';
  if (v === true) return 'on';
  if (v === false) return 'off';
  if (typeof v === 'object') return '<code>' + esc(JSON.stringify(v)) + '</code>';
  return esc(v);
}

// A second count as something a human reads. Uptime arrives as `142750 s`,
// which is a number you have to do arithmetic on before it means anything.
// Home Assistant renders `device_class: duration` for you; the panel has to do
// it itself.
function fmtDuration(sec) {
  const n = Number(sec);
  if (!isFinite(n) || n < 0) return null;
  const d = Math.floor(n / 86400),
    h = Math.floor((n % 86400) / 3600),
    m = Math.floor((n % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${Math.floor(n)}s`;
}

// One entity's value, rendered the way Home Assistant renders it. The panel used
// to print the raw number while HA applied the discovery value_template, so the
// same entity read `-1` here and `idle` there — and the panel is where anyone
// checks whether a sensor is working.
function fmtEntityValue(e) {
  const v = e.value;
  const unit = e.unit ? ` <span class="mut">${esc(e.unit)}</span>` : '';
  if (v === null || v === undefined) return '<span class="mut">—</span>';
  if (e.options && e.options.length && e.component !== 'event') {
    const vals = e.option_values && e.option_values.length ? e.option_values : e.options;
    const i = vals.findIndex(x => String(x) === String(v));
    // An unmapped value stays visible as its raw self, matching
    // `_enum_sensor_value_template`: blanking it would hide a real state.
    if (i >= 0) return esc(e.options[i]);
  }
  if (e.component === 'binary_sensor') return v === 0 || v === '0' || v === false ? 'off' : 'on';
  // These two carry their own units ("1d 15h", a formatted date), so the raw
  // unit must NOT be appended or they read "1d 15h s".
  if (e.device_class === 'duration' && e.unit === 's') {
    const pretty = fmtDuration(v);
    if (pretty) return esc(pretty);
  }
  if (e.device_class === 'timestamp') return esc(fmtDateTime(v));
  return fmtVal(v) + unit;
}

// A timestamp sensor holds an ISO string; printing it raw next to "2 hours ago"
// values is the kind of thing that makes a table hard to scan.
function fmtDateTime(v) {
  const t = typeof v === 'number' ? v * 1000 : Date.parse(v);
  if (!isFinite(t)) return String(v);
  return new Date(t).toLocaleString();
}

//: Device ids whose State card is showing every entity rather than just the
//: sensors. Not persisted: it is a "let me check something" mode, and coming
//: back later to a wall of 67 rows is not what anyone meant by it.
const SHOW_ALL = new Set();

onChange('show-all-entities', el => {
  const id = Number(el.dataset.id);
  if (el.checked) SHOW_ALL.add(id);
  else SHOW_ALL.delete(id);
  paintPanelBody(id, true);
});

// The State table, and — behind the toggle — the whole entity list. These were
// two tables: "State" and a collapsed "All Home Assistant entities" that was a
// strict superset of it, re-listing every sensor plus every control, button and
// event. One table with a switch says the same thing without the duplication.
function entityTable(d, sensors) {
  const all = SHOW_ALL.has(d.id);
  const rows = all ? d.entities || [] : sensors;
  return `<table class="stbl"><tbody>${rows
    .map(
      e => `<tr><td>${esc(e.name)}</td>
      <td>${fmtEntityValue(e)}</td>
      <td class="mut">${all ? `${esc(e.component)}<br>` : ''}<code>${esc(e.value_path || e.key)}</code></td></tr>`,
    )
    .join('')}</tbody></table>`;
}

// Settings the AI card owns. They are ordinary entities — the card just gives
// them a home next to the recognition toggle they belong with, the way
// capability switches get their own card.
//
// The three detections are what the NPU is asked to look for, not settings of
// the machine around it: each one decides whether a class of event is raised at
// all. `pet_detection` gates `pet_detect` and the `pet_time` stamp,
// `drink_detection` gates `drink_start` and `drink_time`, and
// `vomit_detection` fills the `vomit_info` array a `pet_discern` result
// carries. Sitting in Controls they read like Auto Flush and Heater, which are
// plumbing.
//
// `relocate` GUARDS ON supports_ai, and must. `pet_detection` also belongs to
// the feeders (D4H/D4SH), which have a camera but no on-device AI and so get no
// AI card — moving it unconditionally took the switch out of Controls and gave
// it nowhere to go, removing it from the panel entirely.
const AI_ENTITY_KEYS = [
  'yowling_detection',
  'ph_detection',
  'pet_detection',
  'drink_detection',
  'vomit_detection',
];

// Both are Beta in PetKit's own app, and pH has a hardware prerequisite that is
// invisible from here — without the right litter the toggle is on and nothing
// is ever measured, which looks like a bug rather than a missing consumable.
//: Per-control explanations, keyed by entity key, rendered as a `?` beside the
//: label by `controlRow`. These used to be one paragraph sitting UNDER both
//: switches it described, with its bold words ("Yowling", "Urine pH") not even
//: matching the labels above it ("Yowling Detection", "Urine pH Detection") —
//: so the reader had to work out which sentence went with which toggle.
//:
//: Only add an entry where the label genuinely is not enough. A `?` on every
//: row is the same wall of text with extra clicks.
const ENTITY_HELP = {
  yowling_detection:
    "Listens for meowing while a cat is in the box and records when it heard some. PetKit frames repeated yowling as a possible sign of discomfort — it is a health signal, not a novelty. Beta in PetKit's app.",
  ph_detection:
    "Reads the colour of PetKit's own pH-indicator litter from the camera. With ordinary litter it measures nothing at all. Beta in PetKit's app.",
  vomit_detection:
    'Watches for vomiting. A detection result carries a `vomit_info` list, which stays empty while this is off.',
};

//: Values typed into a number control but not yet sent, keyed "<id>:<key>".
//:
//: The panel repaints every few seconds from the device's own state, replacing
//: the whole body. `paintPanelBody` refuses to do that while something inside
//: has focus, which covers typing, but not the moment you look away: the input
//: is then rebuilt from the device's value and whatever you typed is gone. Held
//: here instead, so a repaint re-renders the edit rather than discarding it.
//:
//: Cleared once the value is accepted, or when it matches the device again.
const PENDING_EDITS = new Map();
const editKey = (id, key) => id + ':' + key;

onInput('entity-number', el => {
  const k = editKey(el.dataset.id, el.dataset.key);
  if (el.value === '' || el.value === String(el.dataset.deviceValue)) PENDING_EDITS.delete(k);
  else PENDING_EDITS.set(k, el.value);
  // Mark it visibly unsaved: a number that survives a repaint but has not been
  // sent would otherwise be indistinguishable from the device's own state.
  el.classList.toggle('dirty', PENDING_EDITS.has(k));
});

function controlRow(id, e, kind) {
  const k = kind ? ` data-kind="${esc(kind)}"` : '';
  const nm = esc(e.name) + (ENTITY_HELP[e.key] ? help(ENTITY_HELP[e.key]) : '');
  if (e.component === 'switch') {
    const on = e.value === 1 || e.value === true || e.value === '1';
    return `<label class="ctrl"><span>${nm}</span>
      <span class="sw"><input type="checkbox" ${on ? 'checked' : ''} data-change="set-entity-switch" data-id="${esc(id)}" data-key="${esc(e.key)}"${k}><span class="sl"></span></span></label>`;
  }
  if (e.component === 'number') {
    // The Set button finds its input by walking the DOM rather than by an id
    // built from the entity key — one less device-supplied string in markup.
    // The range is SHOWN, not just enforced by the input: a bare spinner gives
    // no clue what the maximum is, and typing past it silently does nothing.
    const range =
      e.min != null && e.max != null
        ? ` <span class="mut">(${esc(e.min)}–${esc(e.max)}${e.unit ? ' ' + esc(e.unit) : ''})</span>`
        : e.unit
          ? ` <span class="mut">(${esc(e.unit)})</span>`
          : '';
    const pending = PENDING_EDITS.get(editKey(id, e.key));
    const shown = pending !== undefined ? pending : (e.value ?? '');
    return `<label class="ctrl"><span>${nm}${range}</span>
      <span class="cn"><input type="number" class="${pending !== undefined ? 'dirty' : ''}" value="${esc(shown)}" min="${esc(e.min)}" max="${esc(e.max)}" step="${esc(e.step)}"
        data-input="entity-number" data-id="${esc(id)}" data-key="${esc(e.key)}" data-device-value="${esc(e.value ?? '')}">
      <button class="mini" data-action="set-entity-number" data-id="${esc(id)}" data-key="${esc(e.key)}"${k}>Set</button></span></label>`;
  }
  // select — device value maps to an option via option_values (else by index/label)
  let sel = -1;
  if (e.option_values && e.option_values.length)
    sel = e.option_values.findIndex(v => String(v) === String(e.value));
  const opts = (e.options || [])
    .map(
      (o, idx) =>
        `<option ${idx === sel || (sel < 0 && String(e.value) === String(o)) ? 'selected' : ''}>${esc(o)}</option>`,
    )
    .join('');
  return `<label class="ctrl"><span>${nm}</span>
    <span class="cn"><select data-change="set-entity-select" data-id="${esc(id)}" data-key="${esc(e.key)}"${k}>${opts}</select></span></label>`;
}
onChange('set-entity-switch', el =>
  setEntity(
    Number(el.dataset.id),
    el.dataset.key,
    el.checked ? 'ON' : 'OFF',
    null,
    el.dataset.kind,
  ),
);
onChange('set-entity-select', el =>
  setEntity(Number(el.dataset.id), el.dataset.key, el.value, null, el.dataset.kind),
);
onAction('set-entity-number', el => {
  const input = el.closest('.cn').querySelector('input');
  const v = Number(input.value);
  const min = Number(input.min),
    max = Number(input.max);
  // The min/max attributes only bound the spinner and the browser's own
  // validity flag; nothing stops a typed value going straight through. Out of
  // range is refused here rather than clamped, because a silently different
  // number is worse than being told no -- and the server refuses it too, since
  // the panel is not the only way in.
  if (input.value === '' || Number.isNaN(v)) return toast('Enter a number');
  if (v < min || v > max) return toast(`Must be between ${min} and ${max}`);
  setEntity(
    Number(el.dataset.id),
    el.dataset.key,
    input.value,
    () => {
      PENDING_EDITS.delete(editKey(el.dataset.id, el.dataset.key));
      input.classList.remove('dirty');
    },
    el.dataset.kind,
  );
});

onAction('send-action', el =>
  sendAction(
    el,
    Number(el.dataset.id),
    el.dataset.key,
    el.dataset.destructive === '1',
    el.dataset.name,
  ),
);
async function sendAction(el, id, action, destructive, name) {
  if (destructive && !confirm('Send "' + name + '" to the device?')) return;
  post(id, { action }, r => {
    const msg = r.ok
      ? r.delivered === 'mqtt'
        ? 'Sent over MQTT'
        : 'Queued — delivers on next heartbeat (~10s)'
      : 'Error: ' + r.error;
    // Found by walking up from the button that was pressed, not by a global id:
    // with a panel per device those ids would collide, and every device's
    // result would land in the first panel's slot.
    const card = el.closest('.card');
    const o = card && card.querySelector('.cmd-out');
    if (o) o.textContent = msg;
    toast(msg);
  });
}
async function setEntity(id, key, value, onAccepted, kind) {
  post(
    id,
    { entity: key, value },
    r => {
      if (r.ok && onAccepted) onAccepted();
      toast(
        r.ok
          ? r.delivered === 'mqtt'
            ? 'Updated (MQTT)'
            : r.delivered === 'local'
              ? 'Saved'
              : r.delivered === 'ble'
                ? 'Sent over Bluetooth'
                : 'Queued for heartbeat'
          : 'Error: ' + r.error,
      );
    },
    kind,
  );
}
// A `button` entity, as opposed to a device ACTION (`send-action`). The two
// look identical on screen and are different things underneath: an action is
// looked up in `ALL_ACTIONS` and posted as `{action}`, while a button entity is
// one of the device's own entities and is posted as `{entity}` like every other
// control. A BLE accessory only has the second kind.
onAction('press-entity', el =>
  setEntity(Number(el.dataset.id), el.dataset.key, '', null, el.dataset.kind),
);
onAction('send-raw', el => sendRaw(el, Number(el.dataset.id)));
async function sendRaw(el, id) {
  const box = el.closest('details');
  const ta = box && box.querySelector('.cmd-raw');
  let b;
  try {
    b = JSON.parse(ta.value);
  } catch (e) {
    return toast('Invalid JSON');
  }
  post(id, b, r => toast(r.ok ? 'Sent via ' + r.delivered : 'Error: ' + r.error));
}
async function post(id, body, cb, kind) {
  // A BLE accessory has its own route: it is reachable only while its parent
  // is on MQTT, so there is no heartbeat queue to fall back to and the server
  // answers 409 rather than pretending to have queued something.
  const path = kind === 'ble' ? 'ble/' + id + '/command' : 'devices/' + id + '/command';
  const r = await api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (cb) cb(r);
  if (kind === 'ble') {
    if (r && r.error) toast('Error: ' + r.error);
    loadDevices();
    return;
  }
  // reflect optimistic changes + queue count
  scheduleRefresh();
  scheduleDetail(id);
}

// ---------------- Live log ----------------
const LOG = { rows: [] };
function logDetailText(dt) {
  // Not every log entry is an HTTP exchange — a redaction detail has no method
  // or status, and rendering "undefined undefined → undefined" for it is worse
  // than showing the object.
  if (dt && dt.topic) return mqttDetailText(dt);
  if (!dt || !dt.method) return JSON.stringify(dt, null, 2);
  const L = [];
  L.push(dt.method + ' ' + dt.path + ' → ' + dt.status);
  if (dt.headers && Object.keys(dt.headers).length) {
    L.push('');
    L.push('── Request headers ──');
    for (const k in dt.headers) L.push(k + ': ' + dt.headers[k]);
  }
  if (dt.query && Object.keys(dt.query).length) {
    L.push('');
    L.push('── Query ──');
    for (const k in dt.query) L.push(k + ' = ' + dt.query[k]);
  }
  if (dt.req_body) {
    L.push('');
    L.push('── Request body ──');
    L.push(pretty(dt.req_body));
  }
  if (dt.proxy) {
    const p = dt.proxy;
    L.push('');
    L.push('── Proxied to the real cloud ──');
    L.push(p.upstream);
    L.push(
      'status ' +
        p.status +
        (p.error ? '  ·  error ' + p.error.code + ' ' + (p.error.msg || '') : ''),
    );
    L.push(
      'served to the device: ' +
        (p.served === 'upstream' ? 'the cloud’s reply' : 'OUR reply (upstream unusable)'),
    );
    if (p.redactions && p.redactions.length) L.push('redacted: ' + p.redactions.join(', '));
    if (p.upstream_body) {
      L.push('');
      L.push('── What the cloud sent ──');
      L.push(pretty(p.upstream_body));
    }
  }
  if (dt.resp_body) {
    L.push('');
    L.push('── Sent to the device ──');
    L.push(pretty(dt.resp_body));
  }
  return L.join('\n');
}
// An MQTT frame: who it was between, which way it went, and what was in it.
// The summary line only has room for the tail of the topic, so the full one is
// repeated here.
function mqttDetailText(dt) {
  const L = [dt.direction || ''];
  if (dt.client) L.push('client: ' + dt.client);
  if (dt.origin) L.push('origin: ' + dt.origin);
  L.push(dt.topic);
  if (dt.payload) {
    L.push('');
    L.push('── Payload ──');
    L.push(pretty(dt.payload));
  }
  return L.join('\n');
}
function pretty(s) {
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch (e) {
    return s;
  }
}
// A heartbeat is not its own event kind — it is an `http` row whose summary
// names the endpoint. Both the row renderer and the download filter have to
// agree on that, so the test lives here and they share it.
const isHeartbeat = e => e.kind === 'http' && /heartbeat/.test(e.summary || '');

function makeLogNode(e) {
  const hb = isHeartbeat(e);
  const dt = e.detail ? logDetailText(e.detail) : '';
  return (
    `<div class="l ${dt ? 'exp' : ''}" data-hb="${hb ? 1 : 0}"${dt ? ' data-action="toggle-log"' : ''}>` +
    `<div class="lh"><span class="mut">${fmtTs(e.ts)}</span><span class="k ${esc(e.kind)}">${esc(e.kind)}</span>` +
    `${e.device_id != null ? `<span class="mut">#${esc(e.device_id)}</span>` : ''}<span class="lsum">${esc(e.summary)}</span>` +
    // Inside the row, which also carries data-action="toggle-log". `delegate`
    // resolves the innermost [data-action], so this swallows the click instead
    // of expanding the row on the way past — no stopPropagation needed.
    `${dt ? '<button class="mini logcopy" data-action="copy-log" title="Copy this entry">Copy</button>' : ''}` +
    `${dt ? '<span class="mut chev">▸</span>' : ''}</div>` +
    `${dt ? `<pre class="ld">${esc(dt)}</pre>` : ''}</div>`
  );
}
onAction('toggle-log', el => el.classList.toggle('open'));
// The whole entry, header included: a pasted detail with no timestamp or
// endpoint on it is hard to place once it is out of the list.
onAction('copy-log', el => {
  const row = el.closest('.l');
  const head = row && row.querySelector('.lh');
  const body = row && row.querySelector('pre.ld');
  const headText = head
    ? Array.from(head.children)
        .filter(c => c !== el && !c.classList.contains('chev'))
        .map(c => c.textContent.trim())
        .filter(Boolean)
        .join(' ')
    : '';
  copyText([headText, body ? body.textContent : ''].filter(Boolean).join('\n'));
});
//: How many rows this browser keeps. The cap is the reason the count is worth
//: showing at all: "Device logs" reports its line count off the server's whole
//: file, while this list silently drops its oldest row past the limit — so the
//: one that needed saying was the one that never said it.
const LOG_CAP = 600;

function showLogCount() {
  const el = document.getElementById('logCount');
  if (!el) return;
  el.textContent = !LOG.rows.length
    ? ''
    : LOG.rows.length >= LOG_CAP
      ? `last ${LOG_CAP} requests`
      : `${LOG.rows.length} requests`;
}

function pushLog(e) {
  if (e.kind === 'ping') return;
  LOG.rows.push(e);
  if (LOG.rows.length > LOG_CAP) LOG.rows.shift();
  const v = document.getElementById('logView');
  if (!v) return;
  const nearBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 60;
  v.insertAdjacentHTML('beforeend', makeLogNode(e));
  while (v.children.length > LOG_CAP) v.removeChild(v.firstChild);
  if (document.getElementById('autoscroll').checked && nearBottom) v.scrollTop = v.scrollHeight;
  showLogCount();
}
function clearLog() {
  LOG.rows = [];
  document.getElementById('logView').innerHTML = '';
  showLogCount();
}
onAction('clear-log', () => clearLog());

// Save what is on screen, in the same shape the expanded rows show — the point
// of the download is attaching it to a bug report, and a second format nobody
// has read would defeat that. Honours "hide heartbeats" so the file matches
// what the user was actually looking at when they hit download; that used to
// test `e.kind === 'heartbeat'`, which no event ever is, so the checkbox was
// silently ignored.
function downloadLog() {
  const hideHb = document.getElementById('hideHb').checked;
  const rows = LOG.rows.filter(e => !(hideHb && isHeartbeat(e)));
  if (!rows.length) return toast('Nothing to download yet');
  const text = rows
    .map(e => {
      const head = `[${fmtTs(e.ts)}] ${e.kind}${e.device_id != null ? ' #' + e.device_id : ''} — ${e.summary || ''}`;
      const body = e.detail ? logDetailText(e.detail) : '';
      return body ? head + '\n' + body : head;
    })
    .join('\n\n' + '─'.repeat(70) + '\n\n');
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `petkit-local-traffic-${stamp}.log`;
  a.click();
  // Revoking immediately can cancel the download in some browsers; one tick is
  // enough for the navigation to have been started.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Saved ${rows.length} rows`);
}
onAction('download-log', () => downloadLog());
document
  .getElementById('hideHb')
  .addEventListener('change', e => document.body.classList.toggle('hidehb', e.target.checked));
document.body.classList.toggle('hidehb', document.getElementById('hideHb').checked);

// ---------------- Device logs (uploaded by the device itself) ----------------
// Filtering is server-side: a devRun.log runs to a few thousand lines, and
// shipping the whole file on every keystroke costs more than shipping the
// matches. The input is debounced so a fast typist queues one request, not ten.
const DEVLOG = { files: [], sel: '', q: '', reason: '' };
let _devlogT = null;

const DEVLOG_REASONS = {
  no_log_root: 'no log directory is configured',
  no_bucket_endpoint: 'this app has no bucket address, so there is nowhere to send the device',
  authority_not_splittable:
    'the bucket address cannot be split into a host the device can build — it needs a dotted name or IP',
};

async function loadDeviceLogs() {
  const r = await api('devicelogs');
  DEVLOG.files = r.files || [];
  DEVLOG.reason = r.reason || '';
  const pick = document.getElementById('devLogPick');
  if (!pick) return;
  if (!DEVLOG.files.length) {
    pick.innerHTML = '<option value="">no logs yet</option>';
    const why = DEVLOG.reason
      ? `Cannot collect: ${DEVLOG_REASONS[DEVLOG.reason] || esc(DEVLOG.reason)}.`
      : (r.enabled_devices || []).length
        ? 'Collection is on. The device uploads on its own schedule — a reboot is the usual trigger.'
        : 'No device has debug log collection turned on.';
    document.getElementById('devLogView').innerHTML =
      `<p class="mut" style="padding:14px">${esc(why)}</p>`;
    document.getElementById('devLogDl').style.display = 'none';
    document.getElementById('devLogCount').textContent = '';
    return;
  }
  if (!DEVLOG.files.some(f => f.rel === DEVLOG.sel)) DEVLOG.sel = DEVLOG.files[0].rel;
  pick.innerHTML = DEVLOG.files
    .map(
      f =>
        `<option value="${esc(f.rel)}" ${f.rel === DEVLOG.sel ? 'selected' : ''}>${esc(fmtTs(f.mtime))} · #${esc(f.device ?? '?')} · ${esc(fmtBytes(f.size))}</option>`,
    )
    .join('');
  readDeviceLog();
}

function devlogUrl(rel, extra) {
  return 'devicelogs/' + rel.split('/').map(encodeURIComponent).join('/') + (extra || '');
}

async function readDeviceLog() {
  if (!DEVLOG.sel) return;
  const q = DEVLOG.q;
  const r = await api(devlogUrl(DEVLOG.sel, '?q=' + encodeURIComponent(q)));
  if (q !== DEVLOG.q) return; // a newer keystroke already won
  const view = document.getElementById('devLogView');
  const dl = document.getElementById('devLogDl');
  dl.style.display = '';
  dl.href = BASE + 'api/' + devlogUrl(DEVLOG.sel, '?download=1');
  document.getElementById('devLogCount').textContent = q
    ? `${r.matched} of ${r.total} lines`
    : `${r.total} lines`;
  if (!r.lines.length) {
    view.innerHTML = `<p class="mut" style="padding:14px">No line matches.</p>`;
    return;
  }
  view.innerHTML = r.lines
    .map(
      ([n, text]) =>
        `<div class="dl"><span class="ln">${n}</span><span>${hl(text, q)}</span></div>`,
    )
    .join('');
}

// Highlight without ever emitting unescaped input: the raw line is split around
// each match and EVERY fragment is escaped, so `<mark>` is the only markup that
// reaches the DOM. Single-term queries only — with several terms there is no
// one span to mark.
function hl(text, q) {
  const terms = q.split(/\s+/).filter(Boolean);
  if (terms.length !== 1) return esc(text);
  const needle = terms[0].toLowerCase();
  const low = text.toLowerCase();
  let out = '',
    i = 0,
    j;
  while ((j = low.indexOf(needle, i)) !== -1) {
    out += esc(text.slice(i, j)) + '<mark>' + esc(text.slice(j, j + needle.length)) + '</mark>';
    i = j + needle.length;
  }
  return out + esc(text.slice(i));
}

onChange('devlog-pick', el => {
  DEVLOG.sel = el.value;
  readDeviceLog();
});
onInput('devlog-grep', el => {
  DEVLOG.q = el.value;
  clearTimeout(_devlogT);
  _devlogT = setTimeout(readDeviceLog, 200);
});

// ---------------- WebSocket ----------------
let _refreshT = null,
  _detailT = null,
  _timelineT = null;
//: Device ids with a refresh pending. One shared timer coalesces a burst of
//: WebSocket messages into a single round of fetches, and only expanded panels
//: on the visible tab actually cost anything.
const DIRTY = new Set();
function scheduleRefresh() {
  clearTimeout(_refreshT);
  _refreshT = setTimeout(loadDevices, 300);
}
function scheduleDetail(id) {
  if (id == null) DEVICES.forEach(d => DIRTY.add(d.id));
  else DIRTY.add(id);
  clearTimeout(_detailT);
  _detailT = setTimeout(flushDetail, 350);
}
async function flushDetail() {
  const tab = document.getElementById('tab-devices');
  if (!tab || tab.classList.contains('hidden')) {
    DIRTY.clear(); // nobody is looking; loadDevices() repaints on the way back
    return;
  }
  const ids = [...DIRTY];
  DIRTY.clear();
  await Promise.all(ids.map(refreshDevice));
}
async function refreshDevice(id) {
  const panel = devPanel(id);
  if (!panel || !panel.open) return; // a collapsed panel costs nothing
  const d = await fetchDeviceDetail(id);
  if (d.error) return;
  DEV_DETAIL.set(id, d);
  paintPanelBody(id);
}
function scheduleTimeline() {
  const tab = document.getElementById('tab-timeline');
  if (!tab || tab.classList.contains('hidden')) return; // not looking at it — nothing to refresh
  clearTimeout(_timelineT);
  _timelineT = setTimeout(loadTimeline, 400);
}
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + BASE + 'api/ws');
  const st = document.getElementById('wsState');
  ws.onopen = () => {
    st.textContent = 'live';
    st.className = 'pill ok';
  };
  ws.onclose = () => {
    st.textContent = 'reconnecting';
    st.className = 'pill bad';
    setTimeout(connectWS, 2000);
  };
  ws.onmessage = ev => {
    const e = JSON.parse(ev.data);
    if (e.kind === 'ping') {
      loadDevices();
      scheduleDetail(null);
      return;
    }
    pushLog(e);
    if (e.kind === 'patcher') {
      const m = (e.summary || '').match(/^\[(\w+)\]/);
      if (m) {
        const lg = document.getElementById('plog_' + e.device_id + '_' + m[1]);
        if (lg) {
          lg.style.display = 'block';
          lg.textContent += e.summary + '\n';
          lg.scrollTop = lg.scrollHeight;
          if (/done|FAILED/.test(e.summary)) setTimeout(loadPatchers, 2000);
        }
      }
    }
    scheduleRefresh();
    if (e.device_id != null) scheduleDetail(e.device_id);
    else if (e.kind === 'connect') scheduleDetail(null);
    // media becomes "ready" (and new events land) asynchronously, well after
    // the request that triggered them returns — without this the Timeline
    // can render before the file exists, its <img> 404s, and nothing ever
    // retries (see the hide-on-error handling in mediaThumbs() below).
    if (e.kind === 'media' || e.kind === 'event') scheduleTimeline();
  };
}

// ---------------- Capture ----------------
async function loadCapture() {
  const c = await api('capture');
  const v = document.getElementById('capView');
  const what =
    '<p class="sub">Capture records raw device traffic to <code>.jsonl</code> files (one JSON object per line) for protocol analysis — every HTTP request/response and every MQTT frame. Use it to reverse-engineer new payloads or debug a device that won\'t connect.</p>' +
    '<div class="card" style="border-color:var(--warn);margin:0 0 14px"><p style="margin:0"><b>⚠ Every capture is sensitive.</b> These files are a verbatim recording of everything your device said and was told — nothing in them is filtered, because a capture is only useful if it is exact. Expect your <b>Wi-Fi SSID</b>, LAN addresses, the device serial and its signing secret in any of them, and in the <b>proxy</b> ones the full exchanges with PetKit <b>including your account credentials</b>. Read one before you send it anywhere.</p></div>';
  if (!c.enabled) {
    v.innerHTML =
      '<h3>Capture is off</h3>' +
      what +
      '<p class="sub">Turn <b>Traffic capture</b> on in <a data-action="goto-tab" data-tab="setup">Setup → Settings</a> to start recording to <code>' +
      esc(c.dir || '/data/capture') +
      '</code>. It applies immediately — there is no configuration option and no restart.</p>';
    return;
  }
  if (!c.files.length) {
    v.innerHTML =
      '<h3>Capture is on</h3>' +
      what +
      '<p class="mut">No files yet at <code>' +
      esc(c.dir) +
      '</code> — traffic will appear once a device talks to this app.</p>';
    return;
  }
  v.innerHTML =
    '<h3>Capture files</h3>' +
    what +
    '<table><thead><tr><th>File</th><th>Lines</th><th>Size</th><th></th></tr></thead><tbody>' +
    c.files
      .map(
        f => `<tr><td><a data-action="view-capture" data-name="${esc(f.name)}">${esc(f.name)}</a></td><td>${esc(f.lines)}</td><td>${esc(fmtBytes(f.size))}</td>
      <td class="cap-acts"><a href="${esc(BASE + 'api/capture/' + encodeURIComponent(f.name) + '/download')}">download</a>
      <a data-action="delete-capture" data-name="${esc(f.name)}" data-size="${esc(f.size)}" class="danger-link">delete</a></td></tr>`,
      )
      .join('') +
    '</tbody></table>' +
    `<p class="sub" style="margin-top:10px">Nothing prunes these — a capture is deliberate, so it is kept in full until you delete it. Total on disk: <b>${esc(fmtBytes(c.files.reduce((n, f) => n + f.size, 0)))}</b> in <code>${esc(c.dir)}</code>.</p>` +
    '<div id="capBody"></div>';
}
onAction('delete-capture', el => deleteCapture(el.dataset.name, Number(el.dataset.size)));
async function deleteCapture(name, size) {
  // Irreversible and there is no second copy — a capture is usually the only
  // record of whatever it was recorded to investigate.
  if (!confirm(`Delete ${name} (${fmtBytes(size)})?\n\nThis cannot be undone.`)) return;
  const r = await api('capture/' + encodeURIComponent(name), { method: 'DELETE' });
  toast(r.ok ? 'Deleted ' + name : 'Error: ' + (r.error || 'failed'));
  // Clear the viewer if it was showing the file that just went away.
  const body = document.getElementById('capBody');
  if (body && body.textContent.includes(name)) body.innerHTML = '';
  loadCapture();
}
onAction('view-capture', el => viewCapture(el.dataset.name));
async function viewCapture(name) {
  // The name is a directory entry, so it can hold anything a filesystem allows —
  // percent-encode it rather than pasting it straight into the request path.
  const r = await api('capture/' + encodeURIComponent(name) + '?limit=60');
  document.getElementById('capBody').innerHTML =
    '<b>' +
    esc(name) +
    '</b> <span class="mut">(last ' +
    r.records.length +
    ' of ' +
    r.total +
    ')</span><pre>' +
    esc(r.records.map(x => JSON.stringify(x, null, 1)).join('\n')) +
    '</pre>';
}

// ---------------- Setup ----------------
// Categories with BOTH a size and an age cap, in display order. This is the
// single source for the retention table and for saveRetention — they were two
// hardcoded lists, so a category added to one rendered an input that silently
// never saved.
//
// `rawUpload` ("Staged uploads") is deliberately NOT here. Staged files are
// deleted twice over without anyone asking: inline by the media pipeline the
// moment an upload is filed, and by the sweeper for orphans whose metadata
// never arrived — with a code-level one-hour floor a panel edit cannot lower.
// It was a setting for a failure mode nobody can observe, over a dot-directory
// no screen in this app can browse. The server-side default stays.
//
// `wasteCheck` and `healthPic` ARE here now: both are photo galleries the
// Timeline renders, so they are the categories most likely to be the ones
// filling a disk, and they had no control at all while two internal caches did.
const RETENTION_LABELS = {
  fullVideo: 'Recordings',
  eventImage: 'Snapshots',
  highLight: 'Highlights',
  dynamicVideo: 'Motion Clips',
  cloudDouble: 'Timelapses',
  wasteCheck: 'Waste photos',
  healthPic: 'Health photos',
  deviceLog: 'Device logs',
  thumbnail: 'Thumbnail cache',
};
function numOrNull(id) {
  const v = document.getElementById(id).value;
  return v === '' ? null : Number(v);
}
onAction('save-retention', () => saveRetention());
async function saveRetention() {
  const body = {};
  for (const k of Object.keys(RETENTION_LABELS))
    body[k] = { max_mb: numOrNull('ret_' + k + '_mb'), max_days: numOrNull('ret_' + k + '_days') };
  // Age-only categories: rows of their own rather than RETENTION_LABELS
  // entries, which carry a size cap these two do not have.
  body.events = { max_days: numOrNull('ret_events_days') };
  body.blocked = { max_days: numOrNull('ret_blocked_days') };
  const r = await api('retention', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  toast(r.ok ? 'Retention saved' : 'Error: ' + (r.error || 'failed'));
}

// The proxy guards stay VISIBLE but disabled while proxy mode is off: hiding
// "RCE guard is on" would be worse than showing it inactive. The upstream
// picker and the blocked-attempts table are hidden outright — they say nothing
// at all when nothing is being forwarded.
const CUSTOM_UPSTREAM = '__custom__';

async function loadSetup() {
  const i = await api('info');
  // No `api('devices')` here: this tab used to repeat the Devices tab's own
  // table verbatim from the same array. `api('info')` already carries the
  // device count, which is the only thing Setup actually needs to say.
  const ret = await api('retention');
  const v = document.getElementById('setupView');
  const s = i.settings || {};
  const w = i.settings_writable;
  const rd = ret.retention || {};
  const on = s.proxy_mode;
  const sw = (key, val, enabled) =>
    `<span class="sw"><input type="checkbox" ${val ? 'checked' : ''} ${w && enabled !== false ? '' : 'disabled'} data-change="save-setting" data-key="${esc(key)}"><span class="sl"></span></span>`;
  // A guard row that greys out with proxy mode, rather than disappearing.
  const guard = (key, val, title, hint) =>
    `<label class="ctrl${on ? '' : ' disabled'}"><span>${title}<br><span class="mut" style="font-size:11px">${hint}</span></span>${sw(key, val, on)}</label>`;

  // An empty setting means the server's default, which it flags for us — the
  // picker must not have to guess which key that is.
  const presets = i.upstreams || [];
  const fallback = (presets.find(u => u.default) || presets[0] || {}).key || '';
  const current = s.proxy_upstream || fallback;
  const sel = presets.some(u => u.key === current) ? current : CUSTOM_UPSTREAM;

  v.innerHTML = `
  <div class="card"><h3>Settings${help(
    'Proxy mode and capture live here and nowhere else — they are not configuration options. Both apply immediately and survive a restart.',
  )}${w ? '' : ' <span class="badge warn">read-only — running outside Home Assistant</span>'}</h3>
    <div class="ctrls">
      <label class="ctrl"><span>Proxy mode<br><span class="mut" style="font-size:11px">forward every device request to the real PetKit cloud and answer with its reply</span></span>${sw('proxy_mode', s.proxy_mode)}</label>
      <label class="ctrl"><span>Traffic capture<br><span class="mut" style="font-size:11px">record HTTP + MQTT to JSONL (see Capture tab)</span></span>${sw('capture', s.capture)}</label>
    </div>
    ${
      on
        ? `<div style="margin-top:14px"><label class="mut">Upstream server</label>
      <span class="cn" style="display:flex;gap:6px;margin-top:4px;flex-wrap:wrap">
        <select id="proxyUpSel" ${w ? '' : 'disabled'} data-change="save-upstream">
          ${presets.map(u => `<option value="${esc(u.key)}" ${sel === u.key ? 'selected' : ''}>${esc(u.key)} — ${esc(u.url)}</option>`).join('')}
          <option value="${CUSTOM_UPSTREAM}" ${sel === CUSTOM_UPSTREAM ? 'selected' : ''}>custom…</option>
        </select>
      </span>
      ${
        sel === CUSTOM_UPSTREAM
          ? `<span class="cn" style="display:flex;gap:6px;margin-top:6px">
        <input id="proxyUp" value="${esc(s.proxy_upstream)}" ${w ? '' : 'disabled'} placeholder="https://api-eu.petkt.com/6/">
        <button class="mini" ${w ? '' : 'disabled'} data-action="save-proxy-upstream">Set</button></span>`
          : ''
      }
      <label class="mut" style="display:block;margin-top:10px">DNS for upstream lookups${help(
        'Only used to find the server above. Leave empty unless your router or Pi-hole points PetKit’s names at this app — then it would point them here for us too, and proxy mode would reach itself instead of the cloud.',
      )}</label>
      <span class="cn" style="display:flex;gap:6px;margin-top:4px">
        <input id="proxyDns" value="${esc(s.proxy_dns || '')}" ${w ? '' : 'disabled'} placeholder="empty = system resolver">
        <button class="mini" ${w ? '' : 'disabled'} data-action="save-proxy-dns">Set</button></span>
    </div>`
        : ''
    }
    <p class="sub" style="margin-top:14px">${on ? 'Removed from the cloud’s replies:' : 'Guards for proxy mode — inactive until it is on.'}</p>
    <div class="ctrls">
      ${guard('proxy_block_run_cmd', 'proxy_block_run_cmd' in s ? s.proxy_block_run_cmd : true, 'Block run_cmd <span class="badge warn">RCE guard</span>', 'strip shell commands the cloud tries to run on the device')}
      ${guard('proxy_block_ota', 'proxy_block_ota' in s ? s.proxy_block_ota : true, 'Block OTA push <span class="badge warn">firmware guard</span>', 'answer the OTA endpoints locally; drop firmware images found elsewhere')}
      ${guard('proxy_block_log_upload', 'proxy_block_log_upload' in s ? s.proxy_block_log_upload : true, 'Block log upload <span class="badge warn">privacy</span>', 'withhold the token the device needs to send its debug log to PetKit')}
      ${guard('proxy_mqtt_bridge', 'proxy_mqtt_bridge' in s ? s.proxy_mqtt_bridge : true, 'Upstream MQTT bridge', 'also bridge the device’s MQTT session to the real Aliyun broker')}
      ${guard('proxy_media_real_oss', !!s.proxy_media_real_oss, 'Media → real OSS', 'let the device upload recordings to PetKit instead of us — nothing lands locally')}
    </div>
    ${on ? upstreamStatus(i.upstream || {}) : ''}
    ${on ? `<div id="blockedView" style="margin-top:14px"></div>` : ''}
  </div>
  <div class="card"><h3>Connection <span class="badge">restart to change</span></h3>
    <p class="sub">Bound at startup — set these in the <b>Configuration</b> tab, then restart.</p>
    <table><tbody>
      <tr><td>Version</td><td><code>${esc(i.version || '?')}</code></td><td class="mut">The code actually running. If an update did not take effect this still shows the old number, which is the quickest way to tell a stale build from a real bug.</td></tr>
      <tr><td>API URL (apiServers)</td><td><code>${esc(i.api_url)}</code></td><td class="mut">The URL a device calls. Most models take it over Bluetooth from the Provision tab; the oldest, which have no Bluetooth setup, need a DNS redirect.</td></tr>
      <tr><td>MQTT host</td><td><span class="badge ok">automatic</span></td><td class="mut">No global setting — every device is handed our own broker (derived from the API URL host). A patched <code>ctrl</code> connects over MQTT; an unpatched one simply heartbeats over HTTP (it does not crash). Commands fall back to the HTTP heartbeat when there\'s no live MQTT session.</td></tr>
      <tr><td>MQTT broker port</td><td><code>${esc(i.mqtt_port)}</code>${i.mqtt_tls ? ` · TLS <code>${esc(i.mqtt_tls_port)}</code>` : ''}</td><td class="mut">TLS cert ${i.cert_exists ? '<span class="badge ok">present</span>' : '<span class="badge bad">missing</span>'}${help('The self-signed certificate the broker presents. "missing" means the file named by cert_path is not there, and a device that expects TLS will not connect.')}</td></tr>
      <tr><td>MQTT auth</td><td>${i.strict_auth ? '<span class="badge warn">strict HMAC</span>' : '<span class="badge">accept-all</span>'}</td><td class="mut">Accept-all is fine for a trusted LAN; strict enforces the Aliyun HMAC-SHA256 sign.</td></tr>
      <tr><td>Bridge → HA</td><td>${
        !i.ha_enabled
          ? '<span class="badge">disabled</span>'
          : i.ha_publishing
            ? '<span class="badge ok">publishing</span>'
            : '<span class="badge bad">down</span>'
      }</td><td class="mut">Whether entities are reaching Home Assistant right now.${help('This is the connection to HOME ASSISTANT\'s broker, not the one your devices talk to — a device can be perfectly healthy while this is down, and vice versa. "disabled" means publishing was switched off (no ha_mqtt_host, or --no-ha), which is a valid way to run.')}</td></tr>
    </tbody></table>
  </div>
  <div class="card"><h3>Media Retention${help(
    'Over the size cap, the oldest files go first; past the age cap they go regardless of size. The sweep runs about every 10 minutes. Leave a field blank for no cap — 0 also means no cap, not "delete everything".',
  )}</h3>
    <table><thead><tr><th></th><th>Max size (MB)</th><th>Max age (days)</th></tr></thead><tbody>
      ${Object.keys(RETENTION_LABELS)
        .map(k => {
          const c = rd[k] || {};
          return `<tr><td>${RETENTION_LABELS[k]}</td>
        <td><input type="number" id="ret_${k}_mb" value="${esc(c.max_mb ?? '')}" min="0" style="width:100px"></td>
        <td><input type="number" id="ret_${k}_days" value="${esc(c.max_days ?? '')}" min="0" style="width:100px"></td></tr>`;
        })
        .join('')}
      <tr><td>Events (history)</td><td class="mut">—</td><td><input type="number" id="ret_events_days" value="${esc((rd.events || {}).max_days ?? '')}" min="0" style="width:100px"></td></tr>
      <tr><td>Blocked attempts${help('How long to keep the record of instructions the real cloud sent that we refused. Rows are tiny and rare, so a long window costs almost nothing and is worth having if you ever need to show what happened.')}</td><td class="mut">—</td><td><input type="number" id="ret_blocked_days" value="${esc((rd.blocked || {}).max_days ?? '')}" min="0" style="width:100px"></td></tr>
    </tbody></table>
    <div style="margin-top:10px"><button class="act" data-action="save-retention">Save retention settings</button></div>
  </div>`;
  if (on) loadBlocked(i.redactions || {});
}
onChange('save-setting', el => saveSetting(el.dataset.key, el.checked));
// "custom…" only reveals the text box; nothing is saved until a real value is
// picked, so switching to it does not blank a working upstream.
onChange('save-upstream', el =>
  el.value === CUSTOM_UPSTREAM ? renderCustomUpstream() : saveSetting('proxy_upstream', el.value),
);
onAction('save-proxy-dns', () =>
  saveSetting('proxy_dns', document.getElementById('proxyDns').value),
);
onAction('save-proxy-upstream', () =>
  saveSetting('proxy_upstream', document.getElementById('proxyUp').value),
);
function renderCustomUpstream() {
  const sel = document.getElementById('proxyUpSel');
  if (sel && !document.getElementById('proxyUp')) {
    sel.parentElement.insertAdjacentHTML(
      'afterend',
      '<span class="cn" style="display:flex;gap:6px;margin-top:6px"><input id="proxyUp" placeholder="https://api.eu-pet.com/6/">' +
        '<button class="mini" data-action="save-proxy-upstream">Set</button></span>',
    );
    document.getElementById('proxyUp').focus();
  }
}
async function saveSetting(key, value) {
  const r = await api('settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [key]: value }),
  });
  toast(r.ok ? 'Saved · ' + key : 'Error: ' + (r.error || 'failed'));
  loadSetup(); // re-render (guards ungrey, the upstream picker appears, …)
}

// Once a taken-over device settles down it polls only the heartbeat, PetKit
// refuses that, and we answer locally — so nothing in the UI would otherwise
// differ from proxy being off. This line is what says it is running.
function upstreamStatus(counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total)
    return '<p class="sub" style="margin-top:14px"><span class="mut">No calls forwarded yet — the device polls every ~10s.</span></p>';
  const ok = counts.ok || 0;
  const refused = Object.entries(counts).filter(([k]) => k.startsWith('error_'));
  const failed = Object.entries(counts).filter(([k]) => k.startsWith('http_'));
  let html =
    `<p class="sub" style="margin-top:14px">Forwarded <b>${total}</b> calls · ` +
    `<span class="badge ${ok ? 'ok' : ''}">${ok} answered</span> ` +
    refused
      .map(([k, n]) => `<span class="badge warn">${n} refused (${esc(k.slice(6))})</span>`)
      .join(' ') +
    failed
      .map(([k, n]) => `<span class="badge bad">${n} × HTTP ${esc(k.slice(5))}</span>`)
      .join(' ') +
    '</p>';
  if (refused.length && !ok)
    html +=
      '<p class="sub mut">The cloud is refusing every session-bearing call — expected once this app ' +
      'has taken the device over, since the session it presents is one we issued. The device is served our own answers throughout.</p>';
  return html;
}

// Blocked attempts are the persisted ones only. The routine rewrites — every
// dev_serverinfo poll substitutes our address — are counters, not rows.
const REDACT_LABELS = {
  rce: 'shell command',
  ota: 'firmware push',
  secret: 'credential swap',
  log_upload: 'log upload',
  server: 'server address',
  mqtt: 'broker address',
  oss_sts: 'media upload',
  locale: 'timezone',
};
async function loadBlocked(counts) {
  const v = document.getElementById('blockedView');
  if (!v) return;
  let r;
  try {
    r = await api('blocked?limit=25');
  } catch (e) {
    v.innerHTML = '';
    return;
  }
  if (r.error) {
    v.innerHTML = '';
    return;
  }
  // Only rendered when there is something to render. The counter chips that
  // used to head this section were dominated by routine rewrites — the server
  // address, the MQTT host, STS, the timezone — which fire on every poll, so
  // they scrolled a permanent "nothing is wrong" banner past the one thing
  // worth seeing. A blocked attempt is a cloud instruction we REFUSED (a shell
  // command, a firmware push, a credential swap); on a healthy session there
  // are none and this whole card should be absent, not saying so at length.
  const rows = r.records || [];
  if (!rows.length) {
    v.innerHTML = '';
    return;
  }
  v.innerHTML =
    `<h3 style="margin-top:0">Blocked attempts <span class="badge bad">${esc(rows.length)}</span>${help(
      'Instructions the real PetKit cloud sent that this app refused to pass on to your device. Kept in the database, so this list outlives a restart.',
    )}</h3>
    <table><thead><tr><th>When</th><th>What</th><th>Endpoint</th><th>Payload</th></tr></thead><tbody>` +
    rows
      .map(
        b => `<tr><td class="mut">${esc(new Date((b.created_at || 0) * 1000).toLocaleString())}</td>
        <td><span class="badge bad">${esc(REDACT_LABELS[b.kind] || b.kind)}</span></td>
        <td><code>${esc(b.endpoint || '')}</code></td>
        <td class="mut">${esc(b.payload_json || '')}</td></tr>`,
      )
      .join('') +
    '</tbody></table>';
}

// ---------------- Patchers ----------------
async function loadPatchers() {
  const ds = await api('devices');
  const v = document.getElementById('patchView');
  if (!ds.length) {
    v.innerHTML =
      '<div class="empty"><b>No devices connected</b><p class="mut">Connect a device first, then come back to apply patches.</p></div>';
    return;
  }
  let html = `<div class="card" style="border-color:var(--warn)">
    <p style="margin:0"><b>Warning: firmware modification</b> — These patchers modify files on the device filesystem. This is done at your own risk. The authors are not responsible for any bricking, damage, loss of warranty, or other issues. Read each description carefully before applying.</p>
    <p style="margin:4px 0 0;font-size:0.85em;opacity:0.8">All patches are stored on the writable /system partition (jffs2). The read-only /app partition (squashfs) is never modified. Removing a patch just deletes the files from /system and reboots — the device returns to stock firmware.</p></div>`;
  for (const dev of ds) {
    const p = await api('devices/' + dev.id + '/patcher');
    if (!p.supported) {
      html += `<div class="card"><h3>${esc(dev.name)} <span class="chip">${esc(dev.type.toUpperCase())} · #${esc(dev.id)}</span></h3><p class="mut">Patchers are only available for the Linux models (T5, T6, T7, D4H, D4SH, W7H). This device uses a different platform.</p></div>`;
      continue;
    }
    const ip = p.device_ip || '?';
    html += `<div class="card"><h3>${esc(dev.name)} <span class="chip">${esc(dev.type.toUpperCase())} · #${esc(dev.id)}</span></h3>`;
    if (!ip || ip === '?')
      html +=
        '<p class="badge warn">Device IP unknown — wait for a state_report before patching.</p>';
    for (const [pid, pt] of Object.entries(p.patchers || {})) {
      // Real newlines in the description are rendered by `white-space:pre-line`;
      // only the literal two-character sequence "\n" becomes a <br>. Splitting
      // first and escaping each piece keeps that without ever un-escaping.
      const desc = String(pt.description ?? '')
        .split('\\n')
        .map(esc)
        .join('<br>');
      html += `<div class="card" style="margin:8px 0;background:var(--card2)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
          <b>${esc(pt.name)}</b>
          ${pt.applied ? '<span class="badge ok">applied</span>' : '<span class="badge">not applied</span>'}
          ${pt.unavailable ? `<span class="badge warn">${esc(pt.unavailable)}</span>` : pt.greyed ? '<span class="badge ok">MQTT connected</span>' : ''}
          <span class="grow"></span>
          <span class="badge" title="Checked on the device before anything is written. The exact requirement is measured at apply time and is usually smaller.">needs ~${esc(fmtBytes(pt.needs_bytes))} free on /system</span>
        </div>
        <p class="sub" style="white-space:pre-line">${desc}</p>
        ${pt.needs_pubkey ? `<div style="margin-bottom:8px"><label class="mut">Public key</label><input type="text" class="ssh-pubkey" data-dev="${esc(dev.id)}" data-patcher="${esc(pid)}" placeholder="ssh-rsa AAAA... or ecdsa-sha2-..." value="${esc(pt.ssh_pubkey || '')}" aria-label="Public key"></div>` : ''}
        <div>
          <button class="act${pt.greyed ? ' ghost' : ''}" ${pt.greyed || !ip || ip === '?' ? 'disabled' : ''} data-action="apply-patch" data-id="${esc(dev.id)}" data-patcher="${esc(pid)}" data-name="${esc(pt.name)}">Apply</button>
          <button class="ghost act" data-action="remove-patch" data-id="${esc(dev.id)}" data-patcher="${esc(pid)}" data-name="${esc(pt.name)}">Remove</button>
        </div>
        <pre id="plog_${esc(dev.id)}_${esc(pid)}" style="display:none;margin-top:8px;font-size:11px;max-height:200px;overflow:auto"></pre>
      </div>`;
    }
    html += '</div>';
  }
  v.innerHTML = html;
}
onAction('apply-patch', el =>
  applyPatch(Number(el.dataset.id), el.dataset.patcher, el.dataset.name),
);
onAction('remove-patch', el =>
  removePatch(Number(el.dataset.id), el.dataset.patcher, el.dataset.name),
);
async function applyPatch(id, patcher, name) {
  if (
    !confirm(
      'Apply "' +
        name +
        '" to this device?\n\nThis modifies the device firmware. Make sure you understand what it does. Proceed?',
    )
  )
    return;
  const lg = document.getElementById('plog_' + id + '_' + patcher);
  if (lg) {
    lg.style.display = 'block';
    lg.textContent = 'Starting...\n';
  }
  toast('Applying ' + name + '...');
  const r = await api('devices/' + id + '/patcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      patcher,
      action: 'apply',
      pubkey:
        (document.querySelector(`.ssh-pubkey[data-patcher="${patcher}"][data-dev="${id}"]`) || {})
          .value || '',
    }),
  });
  if (!r.ok) {
    toast('Error: ' + (r.error || 'failed'));
    if (lg) lg.textContent += 'ERROR: ' + (r.error || 'failed') + '\n';
  }
}
async function removePatch(id, patcher, name) {
  if (!confirm('Remove "' + name + '" from this device?\n\nThe device will reboot.')) return;
  const lg = document.getElementById('plog_' + id + '_' + patcher);
  if (lg) {
    lg.style.display = 'block';
    lg.textContent = 'Removing...\n';
  }
  const r = await api('devices/' + id + '/patcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ patcher, action: 'remove' }),
  });
  if (!r.ok) {
    toast('Error: ' + (r.error || 'failed'));
  }
}

// ---------------- BLE provisioning (Web Bluetooth) ----------------
//
// TWO protocols, because PetKit uses two chips. Which one a device speaks is
// decided by asking it, not by a model table: connect, then look for whichever
// GATT service it exposes. A table would need updating for every codename and
// would be wrong the first time a model shipped a different board.
// 1. PetKit Ingenic devices (T5/T6/T7/D4H/D4SH/W7H): framed JSON written to
//    0xAAA2, answered on 0xAAA1.
// 2. PetKit ESP32 devices (T4, D4): PetKit JSON carried as custom
//    data on service 0xFFFF, write 0xFF01, notify 0xFF02.
//
const BLE_SERVICE = '0000aaa0-0000-1000-8000-00805f9b34fb';
const BLE_TX = '0000aaa1-0000-1000-8000-00805f9b34fb';
const BLE_RX = '0000aaa2-0000-1000-8000-00805f9b34fb';

const BLUFI_SERVICE = '0000ffff-0000-1000-8000-00805f9b34fb';
const BLUFI_P2E = '0000ff01-0000-1000-8000-00805f9b34fb'; // app -> device, write
const BLUFI_E2P = '0000ff02-0000-1000-8000-00805f9b34fb'; // device -> app, notify
const BLUFI_TYPE_DATA = 0x01;
const BLUFI_DATA_CUSTOM = 0x13;
const BLUFI_FC_FRAG = 0x10;
// PetKit's ESP32 firmware receives custom data in 12-byte JSON chunks.
const BLUFI_FRAG_LEN = 12;
// Timeouts from real captures. Observed worst cases:
// 110 reply 161 ms, 151 ack 270 ms, credentials-ack to state 7 ~11 s
// (to state 10, which we do not wait for, 114 s).
const T_IDENT = 5000;
const T_ACK = 10000;
const T_JOIN = 120000;

//: Advertised-name prefixes that can never be provisioned: BLE-only
//: accessories, which have no WiFi to configure. They show up in the chooser
//: because the filter is a name prefix, and selecting one used to end in a raw
//: NotFoundError.
const BLE_ACCESSORY_PREFIXES = [
  'Petkit_W5',
  'Petkit_W4X',
  'Petkit_CTW2',
  'Petkit_CTW3',
  'Petkit_K2',
  'Petkit_K3',
];
let PROV_INFO = null;

// Offsets are what the device actually stores: `ctrl` parses
// "…locale:%s timezone:%f" into a single float of hours east of UTC and reports
// it back as "&timezone=%.1f". A city list would imply a DST awareness it does
// not have, so the picker shows the thing being stored. Quarter-hour steps
// because Nepal (+5:45), Chatham (+12:45) and India (+5:30) exist.
function tzOptions(selected) {
  let html = '';
  for (let q = -48; q <= 56; q++) {
    const hours = q / 4;
    const sign = hours < 0 ? '-' : '+';
    const abs = Math.abs(hours);
    const label =
      'UTC' +
      sign +
      String(Math.floor(abs)).padStart(2, '0') +
      ':' +
      String(Math.round((abs % 1) * 60)).padStart(2, '0');
    const isSel = Math.abs(hours - selected) < 1e-9;
    html +=
      '<option value="' +
      esc(hours) +
      '"' +
      (isSel ? ' selected' : '') +
      '>' +
      esc(label) +
      (isSel ? ' — detected' : '') +
      '</option>';
  }
  return html;
}

async function loadProvision() {
  PROV_INFO = await api('info');
  const srv = document.getElementById('p-server');
  if (!srv.value) srv.value = PROV_INFO.api_url || '';
  const tz = document.getElementById('p-tz');
  // Only fill it once, so re-opening the tab does not discard a chosen value.
  if (tz && !tz.options.length) tz.innerHTML = tzOptions(-new Date().getTimezoneOffset() / 60);
  const w = document.getElementById('provWarn');
  const btn = document.getElementById('p-btn');
  const hasBt = !!(navigator.bluetooth && navigator.bluetooth.requestDevice);
  const secure = window.isSecureContext;
  // A secure context is the ONLY gate. Being inside the Home Assistant Ingress
  // iframe is not a problem — confirmed on hardware 2026-07-29: provisioning
  // works through Ingress when HA itself is served over a valid certificate.
  //
  // The panel used to serve itself a second time over HTTPS with a self-signed
  // certificate purely to obtain that secure context. That published the whole
  // unauthenticated API to the LAN, so it is gone: the secure context now has
  // to come from a certificate the operator actually controls.
  if (srv && !srv._provWarnBound) {
    srv._provWarnBound = true;
    srv.addEventListener('input', () => paintProvisionWarnings(hasBt, secure));
  }
  paintProvisionWarnings(hasBt, secure);

  // Switch the form off, rather than leaving it fully lit and letting the
  // failure arrive on submit. Real `disabled` attributes, so the state reaches
  // keyboard and screen-reader users too and is not only a CSS dimming.
  const blocked = !hasBt || !secure;
  const form = document.getElementById('provForm');
  if (form) form.classList.toggle('blocked', blocked);
  for (const id of ['p-ssid', 'p-pass', 'p-server', 'p-tz']) {
    const field = document.getElementById(id);
    if (field) field.disabled = blocked;
  }
  btn.disabled = blocked;
  // The reason lives in a warning card that can be scrolled off, so put it on
  // the control itself too — a disabled button with no explanation reads as a
  // broken page.
  btn.title = tooltip;
}

// Why this is a separate, pure function: the branch order below is the whole
// bug it exists to pin, and it is not observable from the DOM stub the panel's
// script test runs against.
//
// THE INSECURE CHECK MUST COME FIRST. Web Bluetooth is a secure-context-only
// API, so on plain HTTP the browser does not expose `navigator.bluetooth` AT
// ALL — `hasBt` is false in Chrome exactly as it is in Firefox. Testing it
// first therefore told two users their browser was unsupported while they were
// on Chrome and the only thing wrong was the page being HTTP. The browser
// message is only meaningful once the context is secure, because that is the
// only situation in which the absence of the API means what it says.
function provisionWarning(hasBt, secure) {
  const HOSTED = 'https://petkit.2442.pl';
  const hosted =
    ' Or use the hosted provisioning page at <a href="' +
    HOSTED +
    '" target="_blank"><code>' +
    HOSTED +
    '</code></a>, which runs entirely in your browser and talks to the device over Bluetooth — it never sees your Wi-Fi password.';
  if (!secure)
    return {
      card:
        '⚠ Web Bluetooth only works on a <b>secure page</b>, and this one is plain HTTP. Serve Home Assistant over HTTPS with your own certificate — provisioning then works from this tab, Ingress included. You will need <b>Chrome or Edge</b> as well; a plain-HTTP page cannot tell whether you have one, because the browser hides Web Bluetooth entirely until the page is secure.' +
        hosted,
      tooltip: 'Web Bluetooth needs a secure page, and this one is plain HTTP. See the note above.',
    };
  if (!hasBt)
    return {
      card: "⚠ Web Bluetooth is only in <b>Chrome/Edge</b> (desktop or Android). Firefox, Safari and iOS can't provision.",
      tooltip: 'This browser has no Web Bluetooth. Use Chrome or Edge, on desktop or Android.',
    };
  return { card: '', tooltip: '' };
}
function plog(line) {
  const el = document.getElementById('p-log');
  el.style.display = 'block';
  el.textContent += line + '\n';
  el.scrollTop = el.scrollHeight;
}
onAction('provision', () => doProvision());
function blufiFrame(seq, data, frag, totalLen) {
  const head = [
    BLUFI_TYPE_DATA | (BLUFI_DATA_CUSTOM << 2),
    frag ? BLUFI_FC_FRAG : 0x00,
    seq & 0xff,
    frag ? data.length + 2 : data.length,
  ];
  const body = frag ? [totalLen & 0xff, (totalLen >> 8) & 0xff, ...data] : [...data];
  return new Uint8Array([...head, ...body]);
}

async function blufiSend(write, ctr, data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (bytes.length <= BLUFI_FRAG_LEN) {
    await write(blufiFrame(ctr.seq++, bytes, false, 0));
    return;
  }
  for (let off = 0; off < bytes.length; off += BLUFI_FRAG_LEN) {
    const chunk = bytes.slice(off, off + BLUFI_FRAG_LEN);
    // `total_len` is what REMAINS including this chunk, which is what the
    // device reassembles against — not the length of the whole message.
    const remaining = bytes.length - off;
    const last = off + BLUFI_FRAG_LEN >= bytes.length;
    await write(blufiFrame(ctr.seq++, chunk, !last, remaining));
  }
}

function paintProvisionWarnings(hasBt, secure) {
  const srv = document.getElementById('p-server');
  const w = document.getElementById('provWarn');
  if (!srv || !w) return;
  const { card } = provisionWarning(hasBt, secure);
  const full = [card, ...provisionUrlWarning(srv.value || '')].filter(Boolean).join('<br>');
  w.innerHTML = full;
  w.style.display = full ? 'block' : 'none';
  w.classList.toggle('stop', !!card);
}

function provisionUrlWarning(value) {
  const out = [];
  // These hints are ADVISORY: the page works, the URL is just one some PetKit
  // hardware cannot use. They must never switch the form off, and they must not
  // borrow the blocking colour — a user who can provision perfectly well should
  // not be shown a stopped form.
  if (/\.local(?=[:/]|$)/i.test(value || '')) {
    out.push(
      "⚠ The apiServers URL uses a <code>.local</code> mDNS host — most embedded PetKit devices can't resolve mDNS. Use your HA host's <b>IP</b> instead (e.g. <code>http://&lt;ha-host-ip&gt;:8080/6/</code>).",
    );
  }
  try {
    const u = new URL(value);
    const port = u.port || (u.protocol === 'http:' ? '80' : u.protocol === 'https:' ? '443' : '');
    if (port && port !== '80') {
      out.push(
        '⚠ ESP32 devices such as T4 and D4 require the API server on port <b>80</b>. Provisioning them will fail with this URL.',
      );
    }
  } catch (e) {
    /* the field may still be half-typed */
  }
  return out;
}

// Turn one notification from 0xFF02 into something worth putting in the log,
// recording any PetKit document it carries into `ctx.replies`.
function blufiExplain(view, ctx) {
  const b = pkBytes(view);
  if (b.length < 4) return 'short frame (' + b.length + 'B)';
  const pktType = b[0] & 0x03;
  const subtype = b[0] >> 2;
  const fc = b[1];
  let data = b.slice(4, 4 + b[3]);

  if (pktType === BLUFI_TYPE_DATA && subtype === BLUFI_DATA_CUSTOM) {
    if (fc & BLUFI_FC_FRAG) {
      ctx.frag.push(data.slice(2));
      return 'custom data, partial (' + (data.length - 2) + 'B)';
    }
    ctx.frag.push(data);
    const whole = ctx.frag.reduce((all, part) => all.concat([...part]), []);
    ctx.frag.length = 0;
    data = new Uint8Array(whole);
    const msg = pkParse(data);
    if (!msg || msg.key === undefined) {
      return 'custom data, not a PetKit document: ' + new TextDecoder().decode(data);
    }
    ctx.replies[msg.key] = msg.payload || {};
    return 'key ' + msg.key + ' ' + JSON.stringify(msg.payload || {});
  }

  return 'ignored ESP32 packet type ' + pktType + ' subtype 0x' + subtype.toString(16);
}

async function provisionBlufi(service, cfg) {
  const p2e = await service.getCharacteristic(BLUFI_P2E);
  const e2p = await service.getCharacteristic(BLUFI_E2P);

  const ctx = { frag: [], replies: {} };
  await e2p.startNotifications();
  e2p.addEventListener('characteristicvaluechanged', ev => {
    const text = blufiExplain(ev.target.value, ctx);
    plog('device: ' + text);
  });

  const write = p2e.writeValueWithResponse
    ? p2e.writeValueWithResponse.bind(p2e)
    : p2e.writeValue.bind(p2e);
  const ctr = { seq: 0 };
  const enc = new TextEncoder();

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const send = async obj => {
    const custom = JSON.stringify(obj);
    plog('ESP32: key ' + obj.key + ' custom data (' + enc.encode(custom).length + ' bytes)');
    await blufiSend(write, ctr, enc.encode(custom));
  };
  let live = true;
  if (service.device) {
    service.device.addEventListener('gattserverdisconnected', () => {
      live = false;
    });
  }
  const until = ms => {
    const end = Date.now() + ms;
    return () => live && Date.now() < end;
  };

  plog('asking the device who it is (key 110)…');
  await send({ key: 110 });
  await sleep(2000);
  if (live && !ctx.replies[110]) {
    plog('no identity reply yet — retrying key 110 once…');
    await send({ key: 110 });
  }
  const identing = until(T_IDENT);
  while (identing() && !ctx.replies[110]) await sleep(250);
  if (!ctx.replies[110]) {
    plog(live ? 'timed out waiting for the device identity.' : 'the device disconnected before identifying itself.');
    return false;
  }

  plog('sending Wi-Fi credentials and server address (key 151)…');
  await send({ key: 151, payload: cfg.payload });
  const acking = until(T_ACK);
  while (acking() && !(ctx.replies[151] && ctx.replies[151].state === 1)) {
    if (pkJoinFailed(ctx.replies[151])) {
      plog('the device refused the credentials: ' + pkJoinState(ctx.replies[151]));
      return false;
    }
    await sleep(250);
  }
  if (!(ctx.replies[151] && ctx.replies[151].state === 1)) {
    plog(
      live
        ? 'timed out waiting for the device to accept the credentials.'
        : 'the device disconnected before accepting the credentials.',
    );
    return false;
  }

  plog('credentials accepted — waiting for the device to join the network…');
  await send({ key: 112 });
  let shown = null;
  let askedWifiDetails = false;
  let sentLanguage = false;
  const joining = until(T_JOIN);
  while (joining()) {
    await sleep(1000);
    const state = (ctx.replies[112] || {}).state;
    if (state !== shown) {
      shown = state;
      plog('device: ' + pkJoinState(ctx.replies[112]));
      if (pkJoinWarn(ctx.replies[112])) {
        plog(
          'warning: the device reported a wrong Wi-Fi password. Seen on ESP32 to ' +
            'recover and join with unchanged credentials, so this is not treated as ' +
            'fatal — still waiting.',
        );
      }
    }
    if (pkJoinFailed(ctx.replies[112])) {
      plog('the device gave up: ' + pkJoinState(ctx.replies[112]));
      return false;
    }
    if (pkJoined(ctx.replies[112])) {
      if (!sentLanguage) {
        sentLanguage = true;
        await send({ key: 114, payload: { language: cfg.language } });
      }
      plog('device joined — sending completion (key 101)…');
      await send({ key: 101 });
      return true;
    }
    if (state === 6 && !askedWifiDetails) {
      askedWifiDetails = true;
      await send({ key: 111 });
    } else {
      await send({ key: 112 });
    }
  }
  plog(
    live
      ? 'timed out waiting for the device to join (last status: ' +
          pkJoinState(ctx.replies[112]) +
          ').'
      : 'the device disconnected before joining (last status: ' +
          pkJoinState(ctx.replies[112]) +
          ').',
  );
  return false;
}

// PetKit Ingenic provisioning (0xAAA0): both directions use the FA FC FD 46
// envelope, type 0x18 for app writes and type 0x13 on notifications. The
// length field includes the JSON plus the two CRC bytes.
const PK_MAGIC = [0xfa, 0xfc, 0xfd, 0x46];
const PK_TAIL = 0xfb;
const PK_TYPE_OUT = 0x18;

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, xorout 0.
function pkCrc16(bytes) {
  let crc = 0xffff;
  for (const b of bytes) {
    crc ^= b << 8;
    for (let i = 0; i < 8; i++) {
      crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
    }
  }
  return crc & 0xffff;
}

// Wrap one JSON object in the framed envelope.
function pkFrame(seq, obj) {
  const json = new TextEncoder().encode(JSON.stringify(obj));
  const crc = pkCrc16(json);
  const len = json.length + 2;
  return new Uint8Array([
    ...PK_MAGIC,
    PK_TYPE_OUT,
    seq & 0xff,
    len & 0xff,
    (len >> 8) & 0xff,
    ...json,
    crc & 0xff,
    (crc >> 8) & 0xff,
    PK_TAIL,
  ]);
}

// Key 112 is the join report. The whole table is PetKit's app's own, read off
// the log lines it prints per state (`BleDeviceBindProgressPresenter`, 13.8.1)
// — including the four failures, which used to render as a bare number here.
// Telling somebody their Wi-Fi password is wrong beats telling them "state 3".
const PK_JOIN_STATES = {
  0: 'starting',
  1: 'looking for the network',
  2: 'connecting to the network',
  3: 'the Wi-Fi password is wrong',
  4: 'that Wi-Fi network was not found',
  5: 'could not connect to the Wi-Fi',
  6: 'on Wi-Fi, connecting to the server',
  7: 'connected to the server',
  8: 'could not connect to the server',
  9: 'connecting to MQTT',
  10: 'online',
};
// The states that mean stop waiting and say why.
const PK_JOIN_FAILED = [4, 5, 8];
const PK_JOIN_WARN = [3];
const PK_JOIN_DONE = [7, 10];

function pkJoined(payload) {
  return !!payload && PK_JOIN_DONE.includes(payload.state);
}

function pkJoinFailed(payload) {
  return !!payload && PK_JOIN_FAILED.includes(payload.state);
}

function pkJoinWarn(payload) {
  return !!payload && PK_JOIN_WARN.includes(payload.state);
}

function pkJoinState(payload) {
  if (!payload || payload.state === undefined) return 'never reported';
  const base = PK_JOIN_STATES[payload.state] || 'state ' + payload.state;
  return payload.code !== undefined ? base + ' (code ' + payload.code + ')' : base;
}

// A DataView's bytes, and only its own: `view.buffer` is the whole underlying
// ArrayBuffer, which may be longer than the view and start before it. Chrome
// hands Web Bluetooth notifications out on their own buffers today, so reading
// it whole happens to work — which is exactly the kind of thing that stops
// working on one browser, on one platform, with no way to see why.
function pkBytes(view) {
  return view.byteLength === undefined
    ? new Uint8Array(view)
    : new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
}

// Parse one inbound PetKit reply: the framed envelope, or bare JSON. Null when
// it is neither. Accepts raw bytes as well as a DataView, because ESP32 custom
// data carries the same PetKit document.
function pkParse(view) {
  const u = view instanceof Uint8Array ? view : pkBytes(view);
  if (u.length >= 11 && u[0] === 0xfa && u[1] === 0xfc && u[2] === 0xfd && u[3] === 0x46) {
    const len = u[6] | (u[7] << 8);
    // len includes the trailing CRC; try that first, then fall back to
    // stripping the 8-byte header and the crc16 + tail (3 bytes).
    for (const end of [8 + len - 2, u.length - 3]) {
      try {
        return JSON.parse(new TextDecoder().decode(u.slice(8, end)));
      } catch (e) {
        /* try next */
      }
    }
    return null;
  }
  try {
    return JSON.parse(new TextDecoder().decode(u));
  } catch (e) {
    return null;
  }
}

async function provisionPetkit(service, cfg) {
  const chars = await service.getCharacteristics();
  const byUuid = uuid => chars.find(c => c.uuid === uuid);
  const tx =
    byUuid(BLE_TX) ||
    chars.find(c => c.properties && (c.properties.notify || c.properties.indicate)); // 0xAAA1 notify
  const rx =
    byUuid(BLE_RX) ||
    chars.find(c => c.properties && (c.properties.writeWithoutResponse || c.properties.write)); // 0xAAA2 write
  if (!tx || !rx) {
    plog(
      'could not find PetKit write/notify characteristics. Seen: ' +
        chars
          .map(c => {
            const p = c.properties || {};
            return (
              c.uuid +
              ' [' +
              ['notify', 'indicate', 'writeWithoutResponse', 'write'].filter(k => p[k]).join(',') +
              ']'
            );
          })
          .join(', '),
    );
    return false;
  }
  plog('PetKit notify: ' + tx.uuid);
  plog('PetKit write: ' + rx.uuid);

  const replies = {}; // key -> payload, as frames land
  tx.addEventListener('characteristicvaluechanged', ev => {
    const msg = pkParse(ev.target.value);
    if (!msg) return;
    replies[msg.key] = msg.payload || {};
    plog('device: key ' + msg.key + ' ' + JSON.stringify(msg.payload || {}));
  });
  await tx.startNotifications();

  const write = async frame => {
    if (rx.writeValueWithResponse) {
      await rx.writeValueWithResponse(frame);
    } else {
      await rx.writeValue(frame);
    }
  };
  let seq = 0;
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const send = async obj => {
    await write(pkFrame(seq++, obj));
  };

  plog('asking the device who it is (key 110)…');
  await send({ key: 110 });
  for (let i = 0; i < 20 && !replies[110]; i++) await sleep(250);
  if (!replies[110]) {
    plog('no identity reply yet — retrying key 110 once…');
    await send({ key: 110 });
    for (let i = 0; i < 20 && !replies[110]; i++) await sleep(250);
  }
  if (!replies[110]) {
    plog('the device never answered — make sure it is still in pairing mode.');
    return false;
  }
  // Inter-step delays transcribed from captures of the official app, each
  // confirmed across two sessions:
  //   1045  key 110 reply -> key 151 write        (1.054 s, 1.048 s)
  //   3340  key 151 ack   -> first key 112 poll   (3.344 s, 3.342 s)
  //   5780  first state 10 -> confirming 112 poll (5.879 s, 5.792 s)
  //   1030  confirmed 10  -> key 101              (1.101 s, 1.038 s)
  // Do not round these.
  await sleep(1045);

  // 151: Wi-Fi + where to phone home. Ack is { state: 1 }.
  plog('sending Wi-Fi credentials and server address (key 151)…');
  await send({ key: 151, payload: cfg.payload });
  for (let i = 0; i < 20 && !(replies[151] && replies[151].state === 1); i++) await sleep(250);
  if (!(replies[151] && replies[151].state === 1)) {
    plog('the device did not accept the credentials (no state:1).');
    return false;
  }

  plog('credentials accepted — waiting for the device to join the network…');
  let shown = null;
  for (let i = 0; i < 25; i++) {
    await sleep(i === 0 ? 3340 : 3000);
    await send({ key: 112 });
    const state = (replies[112] || {}).state;
    // Only on change: polling once a second would otherwise print the same
    // line twenty-five times and bury the one that matters.
    if (state !== shown) {
      shown = state;
      plog('device: ' + pkJoinState(replies[112]));
      if (pkJoinWarn(replies[112])) {
        plog(
          'warning: the device reported a wrong Wi-Fi password. Seen on ESP32 to ' +
            'recover and join with unchanged credentials, so this is not treated as ' +
            'fatal — still waiting.',
        );
      }
    }
    if (pkJoined(replies[112])) {
      await sleep(5780);
      await send({ key: 112 });
      await sleep(1030);
      if (pkJoined(replies[112])) {
        plog('device joined — sending completion (key 101)…');
        await send({ key: 101 });
        return true;
      }
    }
    if (pkJoinFailed(replies[112])) {
      plog('the device gave up: ' + pkJoinState(replies[112]));
      return false;
    }
  }
  // Not "joined" and not a failure either: the device took the credentials and
  // said so. Returning true without a word here reported a device that never
  // got onto Wi-Fi as provisioned, which is the one thing 1.5.0 set out to stop
  // doing.
  plog(
    'the credentials were accepted, but the device did not report joining within ' +
      '25s (last status: ' +
      pkJoinState(replies[112]) +
      '). It may still get there on its own — watch the device list.',
  );
  return true;
}

async function doProvision() {
  const ssid = document.getElementById('p-ssid').value.trim();
  const pwd = document.getElementById('p-pass').value;
  const server = document.getElementById('p-server').value.trim();
  const st = document.getElementById('p-status');
  if (!ssid || !pwd) {
    toast('WiFi SSID and password are required.');
    return;
  }
  document.getElementById('p-log').textContent = '';
  let gatt = null;
  try {
    st.textContent = ' requesting device…';
    const device = await navigator.bluetooth.requestDevice({
      filters: [{ namePrefix: 'Petkit' }],
      optionalServices: [BLE_SERVICE, BLUFI_SERVICE],
    });
    const name = device.name || device.id || '';
    plog('selected: ' + name);
    if (BLE_ACCESSORY_PREFIXES.some(p => name.startsWith(p))) {
      st.textContent = ' this model has no WiFi.';
      plog(
        name +
          ' is a Bluetooth-only accessory — there is no WiFi on it to configure. ' +
          'Pair it instead from the Devices tab, on the panel of the litter box ' +
          'or feeder that will relay for it.',
      );
      return;
    }
    st.textContent = ' connecting…';
    gatt = await device.gatt.connect();
    // The device has no timezone of its own: `TZ` is unset and `date` reports
    // UTC, so it burns UTC into video watermarks. Its `ctrl` binary parses
    // "ssid:%s pwd:%s hide:%d locale:%s timezone:%f" at provisioning time and
    // stores the result in g_config_timezone — this payload is the only path
    // that sets it. Hours east of UTC, the same unit the device reports back as
    // "&locale=%s&timezone=%.1f". The picker above chooses it; the browser's
    // own offset is only the fallback, since the phone provisioning a device is
    // not always in the same timezone as the device.
    //
    // Only takes effect at provisioning: an already-paired device keeps
    // whatever it was given (or wasn't) until it is provisioned again.
    const tzEl = document.getElementById('p-tz');
    const timezone =
      tzEl && tzEl.value !== '' ? Number(tzEl.value) : -new Date().getTimezoneOffset() / 60;
    // `locale` is a TIME ZONE NAME, not a language. PetKit's own app fills it
    // with `TimeZone.getDefault().getID()` — "Europe/Amsterdam", "America/
    // New_York" — right beside the numeric offset, and the captured signup body
    // of a real D4 echoes exactly that back: `timezone=2.0&locale=Europe/
    // Amsterdam`. This used to send `navigator.language`, so a device was told
    // its zone was named "en-US".
    //
    // From the browser, which is the same source the phone uses. The picker
    // above cannot supply it: it offers UTC offsets, and an offset names no
    // single zone — half of UTC+01:00 is Berlin and half is Lagos. The two
    // halves can therefore disagree when somebody overrides the offset by hand,
    // and that is the right way round: the number is what the device runs its
    // clock on, the name is a label it stores and echoes back.
    const zoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    const language = (navigator.language || 'en_US').replace('-', '_');
    // Everything below is the app's key-151 payload, field for field
    // (`BleDeviceBindProgressPresenter.proceedNextStep`, PetKit 13.8.1):
    // `hide` is the constant 1 rather than anything about the network, and the
    // offset goes out as a string of hours.
    const cfg = {
      ssid,
      pwd,
      language,
      payload: {
        ssid,
        pwd,
        hide: 1,
        locale: zoneName,
        timezone: timezone.toFixed(1),
        apiServers: [server],
        ipServers: [server],
      },
    };

    // Ask the device which protocol it speaks rather than assuming — but ask
    // for each service BY NAME, never with `getPrimaryServices()`.
    //
    // The enumeration lists what the browser has already discovered and cached
    // for this device, which is not the same set as what it will hand over when
    // asked directly. A D4SH that provisions fine through `getPrimaryService(
    // BLE_SERVICE)` can be missing from it — reported by an owner whose feeder
    // paired from the hosted page (still on the 1.4.0 code, which asked by
    // name) and refused to pair from the add-on the moment 1.5.0 switched to
    // enumerating. So the Ingenic path makes the exact call it made when it
    // worked, ESP32 is a fallback, and the enumeration is demoted to writing
    // the error message, where an incomplete answer costs nothing.
    const open = async uuid => {
      try {
        return await gatt.getPrimaryService(uuid);
      } catch (e) {
        return null;
      }
    };
    st.textContent = ' sending…';
    let heard;
    const petkit = await open(BLE_SERVICE);
    const blufi = petkit ? null : await open(BLUFI_SERVICE);
    if (petkit) {
      plog('protocol: PetKit (0xAAA0)');
      heard = await provisionPetkit(petkit, cfg);
    } else if (blufi) {
      plog('protocol: BLUFI (0xFFFF)');
      heard = await provisionBlufi(blufi, cfg);
    } else {
      let seen = [];
      try {
        seen = (await gatt.getPrimaryServices()).map(x => x.uuid);
      } catch (e) {
        seen = ['(could not be listed: ' + e.name + ')'];
      }
      st.textContent = ' unknown device.';
      plog(
        "This device answered to neither PetKit's provisioning service (0xAAA0) " +
          'nor PetKit ESP32 provisioning (0xFFFF), so there is nothing here that can configure it. ' +
          'Older models such as the Feeder Mini have no Bluetooth setup at all — ' +
          'those are pointed here with a DNS redirect instead. Services seen: ' +
          seen.join(', '),
      );
      return;
    }

    // "Provisioned" now means the device answered, not that a write returned.
    // The old wording was printed either way, so a payload the firmware never
    // understood read exactly like a success.
    if (heard) {
      st.textContent = ' provisioned — device will restart and join WiFi.';
      plog('done. watching for the device to connect…');
    } else {
      st.textContent = ' sent, but the device never answered.';
      plog(
        'The payload was written and acknowledged at the Bluetooth level, but the ' +
          'device said nothing back. It may still join — watching — but if it does ' +
          'not, this log is what to report.',
      );
    }
    watchForDevice();
  } catch (err) {
    const map = {
      NotFoundError: "No device selected — make sure it's in pairing mode.",
      SecurityError: 'Bluetooth blocked — the page must be HTTPS.',
      NotAllowedError: 'Permission denied — allow Bluetooth for the browser in your OS settings.',
    };
    st.textContent = ' error: ' + (map[err.name] || err.name + ': ' + err.message);
    plog('ERROR ' + err.name + ': ' + err.message);
  } finally {
    try {
      if (gatt) gatt.disconnect();
    } catch (e) {}
  }
}
async function watchForDevice() {
  const before = (await api('devices')).length;
  let tries = 0;
  const iv = setInterval(async () => {
    tries++;
    const ds = await api('devices');
    if (ds.length > before) {
      clearInterval(iv);
      document.getElementById('p-status').textContent = ' ✓ device connected! see the Devices tab.';
      plog('device connected: ' + ds[ds.length - 1].name + ' #' + ds[ds.length - 1].id);
    } else if (tries > 60) {
      clearInterval(iv);
      plog('(still waiting — check the Log tab for its first HTTP call)');
    }
  }, 5000);
}

// ---------------- Timeline ----------------
let TL_DATE = null,
  TL_FILTER = 'all',
  TL_DEVICE = '',
  TL_PET = '',
  TL_MULTIDEV = false;
// Debug state lives at module scope on purpose. #timelineView is re-rendered
// wholesale by loadTimeline(), including 400ms after any websocket media/event
// message, so anything held on an element is lost — but a global survives, and
// sessionCard() re-renders open panels straight from the cache. An event's
// content never changes after ingest, so caching it for the session is safe
// and means a live refresh costs no requests and shows no flicker.
let TL_DEBUG = false;
const TL_DBG_OPEN = new Set();
const TL_DBG_CACHE = new Map();
try {
  TL_DEBUG = localStorage.getItem('tlDebug') === '1';
} catch (_) {
  /* private mode */
}
// The server cuts a timeline day at LOCAL midnight, so "today" here must mean
// the same thing. toISOString() is UTC, so east of Greenwich it named
// tomorrow's date late in the evening and yesterday's just after midnight —
// the date picker and the data it fetched disagreed about which day it was.
function todayLocal() {
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
// Encode each path SEGMENT: encodeURI() leaves '#' (and '?') alone, and the
// device folder name contains one ("Purobot Max Pro 2 (T5 #10000001)") — the
// browser would treat everything from '#' on as a fragment and request a
// truncated path, so every thumbnail 404'd and silently hid itself.
function mediaPath(rel) {
  return rel.split('/').map(encodeURIComponent).join('/');
}
function mediaUrl(rel) {
  return rel ? BASE + 'api/media/' + mediaPath(rel) : '';
}
function thumbUrl(rel) {
  return rel ? BASE + 'api/media/thumb/' + mediaPath(rel) : '';
}

// A pet filter chip, carrying the same mugshot the cards below it show — the
// first registered face, which is what `_pet_badge` uses server-side. A photo
// that fails to load hides itself rather than leaving a broken-image glyph in
// the middle of the chip.
function petChip(p) {
  const face = (p.faces || [])[0];
  return `<button class="chip-btn${String(p.id) === TL_PET ? ' on' : ''}" data-action="tl-pet" data-k="${esc(p.id)}">${
    face
      ? `<img class="chip-av hide-on-error" src="${BASE}${esc(face.url)}" alt="">`
      : '<span class="chip-av chip-av-none">🐾</span>'
  }${esc(p.name)}</button>`;
}

async function loadTimeline() {
  if (!TL_DATE) TL_DATE = todayLocal();
  const [ds, pd] = await Promise.all([api('devices'), api('pets').catch(() => ({}))]);
  const pets = pd.pets || [];
  // Only worth repeating the device name per-card when there's more than one.
  TL_MULTIDEV = ds.length > 1;
  // A pet that has been deleted must not keep filtering the view to nothing.
  if (TL_PET && !pets.some(p => String(p.id) === TL_PET)) TL_PET = '';
  const q = new URLSearchParams({ filter: TL_FILTER, date: TL_DATE });
  if (TL_DEVICE) q.set('device', TL_DEVICE);
  if (TL_PET) q.set('pet', TL_PET);
  const data = await api('timeline?' + q.toString());
  const v = document.getElementById('timelineView');
  const filters = [
    ['all', 'All'],
    ['pet', 'Pet'],
    ['toileting', 'Toileting'],
    ['health_alert', 'Health Alert'],
    ['fault', 'Faults'],
  ];
  v.innerHTML = `<div class="card">
    <div class="row" style="align-items:center;margin-bottom:10px">
      <button class="ghost act" data-action="tl-shift" data-n="-1">◂</button>
      <input type="date" id="tlDate" value="${esc(TL_DATE)}" data-change="tl-date" style="width:auto">
      <button class="ghost act" data-action="tl-shift" data-n="1" ${TL_DATE >= todayLocal() ? 'disabled' : ''}>▸</button>
      <select id="tlDevice" data-change="tl-device" style="width:auto">
        <option value="">All devices</option>
        ${ds.map(d => `<option value="${esc(d.id)}" ${String(d.id) === TL_DEVICE ? 'selected' : ''}>${esc(d.name)} #${esc(d.id)}</option>`).join('')}
      </select>
      <span class="grow"></span>
    </div>
    ${
      pets.length
        ? `<div class="row tl-filterbar" style="align-items:center">
      <div class="chips" role="group" aria-label="Filter by pet">
        <button class="chip-btn${TL_PET === '' ? ' on' : ''}" data-action="tl-pet" data-k="">All pets</button>
        ${pets.map(petChip).join('')}
      </div>
      <span class="grow"></span>
    </div>`
        : ''
    }
    <div class="row tl-filterbar" style="align-items:center">
      <div class="chips">
        ${filters.map(([k, label]) => `<button class="chip-btn${TL_FILTER === k ? ' on' : ''}" data-action="tl-filter" data-k="${k}">${esc(label)}<span class="chip-n">${esc(data.counts[k] ?? 0)}</span></button>`).join('')}
      </div>
      <span class="grow"></span>
      <label class="mut tl-dbgtog"><input type="checkbox" ${TL_DEBUG ? 'checked' : ''} data-change="tl-debug"> Debug info</label>
    </div>
  </div>
  ${data.sessions.length ? data.sessions.map(sessionCard).join('') : '<div class="card"><p class="mut">No activity on this day.</p></div>'}`;
  v.classList.toggle('dbg', TL_DEBUG);
  observeLazyVideos();
}
function tlShiftDay(n) {
  const d = new Date(TL_DATE + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  TL_DATE = d.toISOString().slice(0, 10);
  loadTimeline();
}
function tlSetDate(v) {
  TL_DATE = v;
  loadTimeline();
}
function tlSetFilter(k) {
  TL_FILTER = k;
  loadTimeline();
}
function tlSetDevice(v) {
  TL_DEVICE = v;
  loadTimeline();
}
onAction('tl-shift', el => tlShiftDay(Number(el.dataset.n)));
onAction('tl-filter', el => tlSetFilter(el.dataset.k));
onChange('tl-date', el => tlSetDate(el.value));
onChange('tl-device', el => tlSetDevice(el.value));
onAction('tl-pet', el => {
  TL_PET = el.dataset.k;
  loadTimeline();
});
onChange('tl-debug', el => {
  TL_DEBUG = el.checked;
  if (!TL_DEBUG) TL_DBG_OPEN.clear();
  try {
    localStorage.setItem('tlDebug', TL_DEBUG ? '1' : '0');
  } catch (_) {}
  loadTimeline();
});
onAction('tl-dbg', el => toggleDebug(Number(el.dataset.id)));

// Keyed by event_kind (NOT s.kind) — the chip is the thing the card is about.
const KIND_TAG = {
  toilet_visit: 'Toilet',
  pet: 'Appeared',
  motion: 'Motion',
  cleaning: 'Cleaning',
  error: 'Alert',
  feeding: 'Feeding',
  // A fountain's kinds. Without these the chip rendered the raw lowercase
  // string, so every W7H row was the one card on the page not in title case.
  drinking: 'Drinking',
  system: 'System',
};
const KIND_CLASS = { toilet_visit: 'visit', pet: 'pet', motion: 'pet', error: 'error' };
function hhmmss(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : '';
}

// The timeline's time column is a FIXED grid track, and `toLocaleTimeString()`
// follows the viewer's locale — so "13:23:45" and "1:23:45 PM" have to share
// one width. They don't: a 12-hour time is 3-4 characters longer, and on the
// bold 13px anchor rows it ran past a 70px track by more than the 12px gutter,
// landing on top of the kind pill. `overflow-x: hidden` on <body> hid it.
//
// Widening unconditionally would waste a fifth of a 600px column for everyone
// on a 24-hour locale, and forcing `hour12: false` would override a preference
// that is the viewer's to make. So the track is sized once, from what the
// locale actually resolves to.
(function sizeTimeColumn() {
  let twelve = false;
  try {
    twelve = !!new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' }).resolvedOptions().hour12;
  } catch (_) {
    // Older engine with no resolved `hour12`: measure the rendered string
    // instead. 13:00 is unambiguous — a 12-hour locale renders it with a
    // suffix, a 24-hour one does not.
    twelve = /[AaPp]\.?[Mm]/.test(new Date(2020, 0, 1, 13, 0, 0).toLocaleTimeString());
  }
  if (twelve) document.documentElement.style.setProperty('--tl-time-w', '94px');
})();

// One row = a time in the left gutter + a title (+ optional media below it).
// In debug mode it also carries an "i" toggle, and the panel it opens is
// emitted as a SIBLING of the row: the row is a two-column grid, so a panel
// nested inside it would be trapped in the narrow content column.
function tlRow(ts, titleHtml, media, anchor, id) {
  const dbg = TL_DEBUG && id != null;
  return `<div class="tl-row${anchor ? ' anchor' : ''}">
    <div class="tl-t">${hhmmss(ts)}</div>
    <div>${titleHtml}${dbg ? ` <button class="tl-dbgbtn" data-action="tl-dbg" data-id="${esc(id)}" title="Show what the device actually reported">i</button>` : ''}${media && _has(media) ? `<div class="tl-media">${mediaThumbs(media)}</div>` : ''}</div>
  </div>${dbg ? `<div class="tl-dbg" data-dbg="${esc(id)}">${TL_DBG_OPEN.has(id) ? dbgBody(TL_DBG_CACHE.get(id)) : ''}</div>` : ''}`;
}

async function toggleDebug(id) {
  const box = document.querySelector(`[data-dbg="${CSS.escape(String(id))}"]`);
  if (!box) return;
  if (TL_DBG_OPEN.has(id)) {
    TL_DBG_OPEN.delete(id);
    box.innerHTML = '';
    return;
  }
  TL_DBG_OPEN.add(id);
  if (TL_DBG_CACHE.has(id)) {
    box.innerHTML = dbgBody(TL_DBG_CACHE.get(id));
    return;
  }
  box.innerHTML = '<p class="mut">loading…</p>';
  const d = await api('timeline/' + id);
  TL_DBG_CACHE.set(id, d);
  // The whole view may have been re-rendered while the request was in flight,
  // so re-find the container instead of writing into a detached node — and
  // check the panel is still meant to be open, in case it was toggled shut.
  const live = document.querySelector(`[data-dbg="${CSS.escape(String(id))}"]`);
  if (live && TL_DBG_OPEN.has(id)) live.innerHTML = dbgBody(d);
}

// A decoded field's grade is the honest part of this panel: it says whether we
// are reading a value we have confirmed, one we inferred from captures, one
// the firmware names but we have never seen, or one we are passing through
// without understanding it at all.
function dbgGrade(g) {
  return `<span class="tl-grade ${esc(g || 'unknown')}">${esc(g || 'unknown')}</span>`;
}

function dbgBody(d) {
  if (!d) return '<p class="mut">loading…</p>';
  if (d.error) return `<p class="mut">${esc(d.error)}</p>`;
  const e = d.event || {},
    c = d.code;

  const decoded = (d.decoded || [])
    .map(
      f =>
        `<tr>
      <td>${esc(f.label)}</td><td><b>${esc(f.text)}</b></td>
      <td class="mut"><code>${esc(JSON.stringify(f.raw))}</code></td>
      <td>${dbgGrade(f.grade)}</td></tr>` +
        (f.note
          ? `<tr class="tl-note"><td></td><td colspan="3" class="mut">${esc(f.note)}</td></tr>`
          : ''),
    )
    .join('');

  const meta = [
    ['Event id', e.id],
    ['Code', e.event_type],
    ['Kind', e.event_kind],
    ['Transport', e.source],
    ['Device', [e.device_id, e.device_type].filter(Boolean).join(' · ')],
    ['Received', e.ts ? new Date(e.ts * 1000).toLocaleString() : ''],
    ['Episode', e.related_event],
    ['Parent episode', e.parent_event],
    ['Dedup key', e.event_uid],
    // Reported vs resolved. A ref with no pet means the box is still matching
    // against faces cached from PetKit's cloud — bind it in the Pets tab.
    ['Reported pet id', e.pet_ref],
    ['Pet', e.pet_name || e.pet_id],
    ['Match score', e.score],
  ]
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td colspan="3"><code>${esc(v)}</code></td></tr>`)
    .join('');

  const codeBlock = c
    ? `<table class="tl-dbgt">
      <tr><td>Means</td><td colspan="2"><b>${esc(c.label)}</b></td><td>${dbgGrade(c.grade)}</td></tr>
      ${c.firmware ? `<tr><td>Firmware</td><td colspan="3"><code>${esc(c.firmware)}</code></td></tr>` : ''}
      ${c.note ? `<tr class="tl-note"><td></td><td colspan="3" class="mut">${esc(c.note)}</td></tr>` : ''}
    </table>`
    : '<p class="mut">This code is in no table yet — it will read as a bare "Event N".</p>';

  return `<div class="tl-dbgin">
    <h4>What this code means</h4>${codeBlock}
    <h4>Decoded payload</h4>${
      decoded
        ? `<table class="tl-dbgt">${decoded}</table>`
        : '<p class="mut">The device sent no content.</p>'
    }
    <h4>Event record</h4><table class="tl-dbgt">${meta}</table>
    <details class="adv"><summary>Raw content JSON</summary><pre>${esc(JSON.stringify(d.content || {}, null, 2))}</pre></details>
    <details class="adv"><summary>Raw state snapshot</summary><pre>${esc(JSON.stringify(d.state || {}, null, 2))}</pre></details>
  </div>`;
}
function _has(m) {
  return (
    m &&
    (m.playback_url ||
      m.highlight_url ||
      m.preview_url ||
      (m.waste && m.waste.length) ||
      (m.health && m.health.length) ||
      m.snapshot_url ||
      m.video_pending)
  );
}

function sessionCard(s) {
  const tag = KIND_TAG[s.event_kind] || (s.event_kind || 'event').replace(/_/g, ' ');
  const cls = KIND_CLASS[s.event_kind] || '';
  // Every fact about the episode reads the same way: "label <b>value</b>",
  // separated by · and kept unbreakable — the line used to wrap between "2.07"
  // and "kg", which reads as two different numbers at a glance.
  //
  // The server-decoded facts (waste weight, litter level, yowling) used to be
  // pills instead, which made them look like a different KIND of thing from
  // the duration and weight beside them. They are not: they are all just what
  // this visit was. Anything less certain than these stays behind Debug.
  const facts = [];
  if (s.kind === 'visit') {
    if (s.duration_sec != null) facts.push(`spent <b>${Math.round(s.duration_sec)}s</b>`);
    if (s.weight != null) facts.push(`weighed <b>${(s.weight / 1000).toFixed(2)} kg</b>`);
  }
  for (const b of s.bits || []) {
    // "waste 42 g" / "litter 40%" / "yowled 12s" — bold from the first digit,
    // matching the pattern above. A bit with no number ("waste", "yowled")
    // has nothing to bold and is printed as-is.
    const m = String(b).match(/^(\D+?)\s+(\d.*)$/);
    facts.push(m ? `${esc(m[1])} <b>${esc(m[2])}</b>` : esc(b));
  }
  const factsHtml = facts.map(f => `<span class="tl-nb">${f}</span>`).join(' · ');

  let title;
  if (s.kind === 'visit') {
    title = factsHtml || '<span class="mut">toilet visit</span>';
  } else {
    title =
      `<span class="mut">${esc(s.label || tag)}</span>` + (factsHtml ? ' · ' + factsHtml : '');
  }
  // Who the device's NPU recognised. Absent when it recognised nobody, which
  // is normal for a passing glance — most "appeared" episodes have no match.
  const pet = s.pet_name
    ? `<span class="tl-pet">${s.pet_photo_url ? `<img src="${BASE}${esc(s.pet_photo_url)}" alt="">` : ''}${esc(s.pet_name)}</span>`
    : '';
  const head = `<span class="tl-kind ${cls}">${esc(tag)}</span>${pet}${title}`;
  const subs = s.sub_events || [];
  const primary = subs.filter(e => !e.detail),
    detail = subs.filter(e => e.detail);

  return `<div class="card tl-card">
    ${TL_MULTIDEV ? `<span class="tl-dev">${esc(s.device_name || '')}</span>` : ''}
    ${tlRow(s.display_ts ?? s.ts, head, s.media, true, s.id)}
    ${primary.map(e => tlRow(e.ts, esc(e.label || e.event_type || ''), e.media, false, e.id)).join('')}
    ${detail.length ? `<details class="tl-more"><summary>show ${detail.length} more step${detail.length > 1 ? 's' : ''}</summary>${detail.map(e => tlRow(e.ts, esc(e.label || e.event_type || ''), e.media, false, e.id)).join('')}</details>` : ''}
  </div>`;
}

// Thumbnails for one media slot-set. Aspect ratio is preserved (object-fit:
// contain in CSS) — the device's fisheye is square and must not be cropped.
// The primary tile autoplays a muted looping <video>, GIF-like and with NO
// re-encoding (the browser just plays the existing mp4), lazily (only while
// on screen; see observeLazyVideos). Source preference:
//   1. the CLIP (highlight/dynamicVideo) — a complete single file, so it's a
//      real preview, never a fragment;
//   2. else the timelapse (cloudDouble) ONCE it's stitched;
//   3. else a still;
//   4. else, if a recording is still assembling, a "processing" placeholder
//      so the user knows the video is on its way.
// Every path here is DEVICE-DERIVED (folder names come from the device's own
// reported name), so the lists ride along as JSON in a data- attribute: the
// browser hands that back as an inert string and JSON.parse can only ever
// produce data. They are never interpolated into anything executable.
function mediaThumbs(m) {
  if (!m) return '';
  let html = '';
  const autoplay = m.highlight_url || m.preview_url; // clip preferred over timelapse
  // The PLAYER offers Timelapse ⇄ Playback (the short clip is already the
  // autoplaying thumbnail, so it'd be redundant there). The clip is only a
  // last-resort fallback when neither chunked video exists.
  const opts = [];
  if (m.preview_url) opts.push(['Timelapse', m.preview_url]);
  if (m.playback_url) opts.push(['Playback', m.playback_url]);
  if (!opts.length && m.highlight_url) opts.push(['Clip', m.highlight_url]);
  const poster = thumbUrl(m.snapshot_url || m.highlight_url || m.playback_url);
  if (autoplay) {
    const open = opts.length
      ? ` data-action="open-video" data-media="${esc(JSON.stringify(opts))}" data-pending="${m.video_pending ? 1 : 0}"`
      : '';
    html += `<div class="tl-thumb"${open}>
      <video class="lazyvid" muted loop playsinline preload="none" data-src="${esc(mediaUrl(autoplay))}" ${poster ? `poster="${esc(poster)}"` : ''}></video>
      <span class="tl-play">▶</span>${m.video_pending ? '<span class="tl-badge proc">assembling…</span>' : ''}</div>`;
  } else if (m.video_pending) {
    // A recording is expected but its chunks haven't been joined yet. Show the
    // still if we have one (badged), else a clear placeholder — never a raw
    // fragment. The card auto-refreshes when the stitcher finishes (WS 'media').
    html += m.snapshot_url
      ? `<div class="tl-thumb" data-action="open-gallery" data-media="${esc(JSON.stringify([m.snapshot_url]))}" data-title="Snapshot">
           <img class="hide-on-error" src="${esc(thumbUrl(m.snapshot_url))}">
           <span class="tl-cap">▶ video processing…</span></div>`
      : `<div class="tl-thumb tl-ph"><div class="tl-ph-in"><span class="tl-spin"></span>Video processing…</div></div>`;
  } else if (m.snapshot_url) {
    html += `<div class="tl-thumb" data-action="open-gallery" data-media="${esc(JSON.stringify([m.snapshot_url]))}" data-title="Snapshot">
      <img class="hide-on-error" src="${esc(thumbUrl(m.snapshot_url))}"></div>`;
  }
  if (m.waste && m.waste.length) {
    html += `<div class="tl-thumb small" data-action="open-gallery" data-media="${esc(JSON.stringify(m.waste))}" data-title="Check waste">
      <img class="hide-on-error" src="${esc(thumbUrl(m.waste[0]))}"><span class="tl-badge">${esc(m.waste.length)}</span>
      <span class="tl-cap">Check waste</span></div>`;
  }
  if (m.health && m.health.length) {
    html += `<div class="tl-thumb small" data-action="open-gallery" data-media="${esc(JSON.stringify(m.health))}" data-title="Health">
      <img class="hide-on-error" src="${esc(thumbUrl(m.health[0]))}"><span class="tl-badge">${esc(m.health.length)}</span>
      <span class="tl-cap">Health</span></div>`;
  }
  return html;
}
onAction('open-video', el => openVideo(JSON.parse(el.dataset.media), el.dataset.pending === '1'));
onAction('open-gallery', el => openGallery(JSON.parse(el.dataset.media), el.dataset.title));

// Lazily autoplay the timelapse-preview <video> thumbnails: load + play only
// while on screen, pause + unload when scrolled away, so a long day of events
// doesn't fetch every clip at once.
let _vidObserver = null;
function observeLazyVideos() {
  if (!('IntersectionObserver' in window)) {
    // Fallback: just load + play them all.
    document.querySelectorAll('video.lazyvid').forEach(v => {
      if (!v.src) {
        v.src = v.dataset.src;
      }
      v.play().catch(() => {});
    });
    return;
  }
  if (!_vidObserver) {
    _vidObserver = new IntersectionObserver(
      entries => {
        for (const en of entries) {
          const v = en.target;
          if (en.isIntersecting) {
            if (!v.src) {
              v.src = v.dataset.src;
            }
            v.play().catch(() => {});
          } else {
            v.pause();
          }
        }
      },
      { rootMargin: '100px' },
    );
  }
  document.querySelectorAll('video.lazyvid').forEach(v => _vidObserver.observe(v));
}

// State for whatever the modal currently shows. The source-switch and gallery
// buttons carry only an INDEX into this, so no path is ever put in markup.
let _VID = null; // {opts: [[label, relPath], …], pending: bool}
let _GAL = null; // {list: [relPath, …], title, idx}

function closeModal() {
  document.getElementById('mediaModal').classList.add('hidden');
  document.getElementById('modalBody').innerHTML = '';
  _VID = null;
  _GAL = null;
}
onAction('close-modal', () => closeModal());
onAction('modal-backdrop', (el, ev) => {
  if (ev.target === el) closeModal();
});

// opts: array of [label, relPath] — usually [['Timelapse', …], ['Playback', …]].
function openVideo(opts, playbackPending) {
  if (!opts || !opts.length) return;
  _VID = { opts, pending: !!playbackPending };
  renderVideo(opts[0][1]);
  document.getElementById('mediaModal').classList.remove('hidden');
}
function renderVideo(rel) {
  const multi = _VID.opts.length > 1;
  document.getElementById('modalBody').innerHTML =
    (multi
      ? `<div class="row" style="margin-bottom:8px">${_VID.opts
          .map(
            ([label], i) =>
              `<button class="ghost act" data-action="vid-source" data-i="${i}">${esc(label)}</button>`,
          )
          .join('')}</div>`
      : // Playback is chunked and still being assembled — say so rather than
        // implying the shown clip is the whole recording.
        _VID.pending
        ? `<div class="mut" style="margin-bottom:8px;font-size:12px">▶ Full playback still processing…</div>`
        : '') +
    `<video controls autoplay style="width:100%;max-height:70vh" src="${esc(mediaUrl(rel))}"></video>`;
}
onAction('vid-source', el => {
  if (_VID) renderVideo(_VID.opts[Number(el.dataset.i)][1]);
});

function openGallery(list, title) {
  if (!list || !list.length) return;
  _GAL = { list, title: title || '', idx: 0 };
  renderGallery();
  document.getElementById('mediaModal').classList.remove('hidden');
}
// The TRUE "n of m" is shown here, computed from the actual group — the
// filename deliberately carries only a bare index (the total isn't known
// at upload time). See media/layout.build_media_path.
function renderGallery() {
  const g = _GAL;
  document.getElementById('modalBody').innerHTML =
    `<div class="row" style="margin-bottom:8px;align-items:center;gap:10px">
      <b>${esc(g.title)}</b><span class="grow"></span>
      <button class="ghost act" data-action="gal-step" data-n="-1" ${g.list.length < 2 ? 'disabled' : ''}>◂</button>
      <span class="mut" style="font-variant-numeric:tabular-nums">${g.idx + 1} of ${g.list.length}</span>
      <button class="ghost act" data-action="gal-step" data-n="1" ${g.list.length < 2 ? 'disabled' : ''}>▸</button></div>
      <img src="${esc(mediaUrl(g.list[g.idx]))}" style="width:100%;max-height:70vh;object-fit:contain;background:#000;border-radius:10px">`;
}
onAction('gal-step', el => {
  if (!_GAL) return;
  _GAL.idx = (_GAL.idx + Number(el.dataset.n) + _GAL.list.length) % _GAL.list.length;
  renderGallery();
});
// ---------------- AI / Pets ----------------
// The device holds at most this many mugshots per pet. Mirrors
// events/store.py::MAX_FACES_PER_PET, and a test asserts the two agree.
const MAX_FACES = 6;

// The size the NPU is fed. Every mugshot PetKit's own cloud served was
// exactly this, and media/transcode.py::normalize_face_photo returns an upload
// byte-identical when it already is — so cropping to it here means the server
// never re-encodes and the crop the user made is the one the device gets.
const FACE_SIZE = 224;

// What PetKit's own app tells its users. These are the conditions the matcher
// was tuned for; a photo that breaks one costs accuracy on every future visit.
// Every rule here is real and comes from what the NPU actually needs — except
// the last one, which is a passport-photo joke. Nothing detects eyewear; it is
// there because the list had already drifted into sounding like a government
// form by the time it reached "One cat per photo". Leave it, or don't, but
// please do not implement it.
const FACE_RULES = [
  ['ok', 'Front view, both eyes visible, looking at the camera'],
  ['ok', 'The whole head in frame, evenly lit'],
  ['bad', 'No side or three-quarter views'],
  ['bad', 'No night-vision / infrared shots'],
  ['bad', 'No backlighting — the face must not be in shadow'],
  ['bad', 'Nothing covering the face'],
  ['bad', 'One cat per photo'],
  ['bad', 'No glasses, and all facial jewellery must be removed'],
];

// ---- the cropper -----------------------------------------------------------
// A fixed square frame with the photo panning and zooming underneath it, the
// way every avatar cropper works. The alternative — a draggable, resizable box
// over a static photo — needs corner handles, aspect locking and edge clamping,
// and is miserable on a touchscreen.
//
// State lives here rather than in the DOM because the modal is static markup
// (see index.html) while everything that opens it is re-rendered.
//
// The zoom ceiling is a multiple of "cover", not an absolute scale, so it means
// the same thing for a 12MP phone photo and a small crop someone already made.
// 8x lets you pull a face out of a wide shot where the cat is a fifth of the
// frame — the case the old 4x could not reach.
const MAX_ZOOM = 8;
let _CROP = null; // {petId, bitmap, scale, minScale, x, y, dragging, px, py}

function cropEls() {
  return {
    modal: document.getElementById('cropModal'),
    pick: document.getElementById('cropPick'),
    stage: document.getElementById('cropStage'),
    file: document.getElementById('cropFile'),
    canvas: document.getElementById('cropCanvas'),
    preview: document.getElementById('cropPreview'),
    zoom: document.getElementById('cropZoom'),
    rules: document.getElementById('cropRules'),
    title: document.getElementById('cropTitle'),
  };
}

function openCropper(petId, petName) {
  const el = cropEls();
  _CROP = { petId: petId, bitmap: null, scale: 1, minScale: 1, x: 0, y: 0, dragging: false };
  el.title.textContent = petName ? `Add a mugshot of ${petName}` : 'Add a mugshot';
  el.rules.innerHTML = FACE_RULES.map(r => `<li class="${r[0]}">${esc(r[1])}</li>`).join('');
  el.file.value = '';
  el.pick.classList.remove('hidden', 'dragover');
  el.stage.classList.add('hidden');
  el.modal.classList.remove('hidden');
}

function closeCropper() {
  const el = cropEls();
  el.modal.classList.add('hidden');
  // Free the decoded image rather than waiting for GC: a phone photo is tens
  // of megabytes once decoded, and the modal may be reopened many times.
  if (_CROP && _CROP.bitmap && _CROP.bitmap.close) _CROP.bitmap.close();
  _CROP = null;
}

// EXIF orientation is honoured by createImageBitmap, which matters because a
// phone portrait shot is stored landscape plus a rotation tag — decoded naively
// the cat comes out sideways. The <img> fallback covers browsers without the
// option; there the tag is applied by the image decoder anyway.
async function decodeImage(file) {
  if (window.createImageBitmap) {
    try {
      return await createImageBitmap(file, { imageOrientation: 'from-image' });
    } catch (e) {
      /* fall through to the <img> path */
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    await new Promise((res, rej) => {
      img.addEventListener('load', res);
      img.addEventListener('error', () => rej(new Error('could not read that image')));
      img.src = url;
    });
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function cropLoad(file) {
  if (!file) return;
  const el = cropEls();
  let bitmap;
  try {
    bitmap = await decodeImage(file);
  } catch (e) {
    return toast('Could not read that image');
  }
  const size = el.canvas.width;
  const w = bitmap.width || bitmap.naturalWidth;
  const h = bitmap.height || bitmap.naturalHeight;
  // "Cover": the smallest scale at which the photo still fills the frame, so
  // the crop can never contain empty space.
  _CROP.bitmap = bitmap;
  _CROP.minScale = Math.max(size / w, size / h);
  _CROP.scale = _CROP.minScale;
  _CROP.x = (size - w * _CROP.scale) / 2;
  _CROP.y = (size - h * _CROP.scale) / 2;
  el.zoom.value = '100';
  el.pick.classList.add('hidden');
  el.stage.classList.remove('hidden');
  cropDraw();
}

// Keep the photo covering the frame on every pan and zoom. Without this a drag
// exposes the canvas background and the exported JPEG has a blank edge.
function cropClamp() {
  const size = cropEls().canvas.width;
  const w = (_CROP.bitmap.width || _CROP.bitmap.naturalWidth) * _CROP.scale;
  const h = (_CROP.bitmap.height || _CROP.bitmap.naturalHeight) * _CROP.scale;
  _CROP.x = Math.min(0, Math.max(size - w, _CROP.x));
  _CROP.y = Math.min(0, Math.max(size - h, _CROP.y));
}

function cropDraw() {
  if (!_CROP || !_CROP.bitmap) return;
  const el = cropEls();
  cropClamp();
  const w = (_CROP.bitmap.width || _CROP.bitmap.naturalWidth) * _CROP.scale;
  const h = (_CROP.bitmap.height || _CROP.bitmap.naturalHeight) * _CROP.scale;

  const ctx = el.canvas.getContext('2d');
  ctx.clearRect(0, 0, el.canvas.width, el.canvas.height);
  ctx.drawImage(_CROP.bitmap, _CROP.x, _CROP.y, w, h);

  // The preview is the same transform at the size the device will receive, so
  // what it shows is literally the bytes that get uploaded.
  const k = FACE_SIZE / el.canvas.width;
  const pctx = el.preview.getContext('2d');
  pctx.clearRect(0, 0, FACE_SIZE, FACE_SIZE);
  pctx.drawImage(_CROP.bitmap, _CROP.x * k, _CROP.y * k, w * k, h * k);
}

// Zoom about a point (the cursor for a wheel, the centre for the slider) so the
// thing being looked at stays put instead of sliding away.
function cropZoomTo(scale, ax, ay) {
  const el = cropEls();
  const next = Math.max(_CROP.minScale, Math.min(_CROP.minScale * MAX_ZOOM, scale));
  const ratio = next / _CROP.scale;
  _CROP.x = ax - (ax - _CROP.x) * ratio;
  _CROP.y = ay - (ay - _CROP.y) * ratio;
  _CROP.scale = next;
  el.zoom.value = String(Math.round((next / _CROP.minScale) * 100));
  cropDraw();
}

async function cropSave() {
  if (!_CROP || !_CROP.bitmap) return;
  const petId = _CROP.petId;
  const blob = await new Promise(res => cropEls().preview.toBlob(res, 'image/jpeg', 0.92));
  if (!blob) return toast('Could not encode the crop');
  closeCropper();

  let r;
  try {
    const res = await fetch(BASE + 'api/pets/' + encodeURIComponent(petId) + '/faces', {
      method: 'POST',
      body: blob,
    });
    // A 413 answers text/plain, so .json() rejects — surface it rather than
    // letting the promise die silently, which is what once made this button
    // appear to do nothing at all.
    r = await res
      .json()
      .catch(() => ({ error: res.status === 413 ? 'photo too large' : 'HTTP ' + res.status }));
  } catch (e) {
    r = { error: String((e && e.message) || e) };
  }
  toast(r.face ? 'Photo added' : 'Error: ' + (r.error || 'failed'));
  loadPets();
}

onAction('crop-close', () => closeCropper());
onAction('crop-backdrop', (el, ev) => {
  if (ev.target === el) closeCropper();
});
onAction('crop-browse', () => cropEls().file.click());
onAction('crop-back', () => {
  cropEls().stage.classList.add('hidden');
  cropEls().pick.classList.remove('hidden');
});
onAction('crop-reset', () => {
  const size = cropEls().canvas.width;
  cropZoomTo(_CROP.minScale, size / 2, size / 2);
});
onAction('crop-save', () => cropSave());
onChange('crop-file', el => cropLoad(el.files && el.files[0]));
onInput('crop-zoom', el => {
  const size = cropEls().canvas.width;
  cropZoomTo(_CROP.minScale * (Number(el.value) / 100), size / 2, size / 2);
});

// Drag and wheel are bound once, on the document and the canvas respectively —
// not per render. The modal is static markup, so these survive every re-render
// of the tab behind it, and a pointer that leaves the canvas mid-drag is still
// tracked (which is why move/up live on the document).
(function bindCropGestures() {
  const canvas = document.getElementById('cropCanvas');
  if (!canvas) return;
  canvas.addEventListener('pointerdown', ev => {
    if (!_CROP || !_CROP.bitmap) return;
    _CROP.dragging = true;
    _CROP.px = ev.clientX;
    _CROP.py = ev.clientY;
    canvas.setPointerCapture(ev.pointerId);
  });
  document.addEventListener('pointermove', ev => {
    if (!_CROP || !_CROP.dragging) return;
    // The canvas is drawn at its backing-store size but laid out by CSS, so a
    // pointer pixel is not a canvas pixel unless we scale by the ratio.
    const rect = canvas.getBoundingClientRect();
    const k = canvas.width / rect.width;
    _CROP.x += (ev.clientX - _CROP.px) * k;
    _CROP.y += (ev.clientY - _CROP.py) * k;
    _CROP.px = ev.clientX;
    _CROP.py = ev.clientY;
    cropDraw();
  });
  document.addEventListener('pointerup', () => {
    if (_CROP) _CROP.dragging = false;
  });
  canvas.addEventListener(
    'wheel',
    ev => {
      if (!_CROP || !_CROP.bitmap) return;
      ev.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const k = canvas.width / rect.width;
      cropZoomTo(
        _CROP.scale * (ev.deltaY < 0 ? 1.12 : 1 / 1.12),
        (ev.clientX - rect.left) * k,
        (ev.clientY - rect.top) * k,
      );
    },
    { passive: false },
  );
  // Dropping a file straight onto the picker, rather than only via the dialog.
  const pick = document.getElementById('cropPick');
  ['dragenter', 'dragover'].forEach(t =>
    pick.addEventListener(t, ev => {
      ev.preventDefault();
      pick.classList.add('dragover');
    }),
  );
  ['dragleave', 'drop'].forEach(t =>
    pick.addEventListener(t, () => pick.classList.remove('dragover')),
  );
  pick.addEventListener('drop', ev => {
    ev.preventDefault();
    cropLoad(ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]);
  });
})();

// The file's only keyboard handler. Both dialogs get it, rather than leaving
// one closable with Escape and the other not.
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  if (!document.getElementById('cropModal').classList.contains('hidden')) return closeCropper();
  if (!document.getElementById('mediaModal').classList.contains('hidden')) closeModal();
});

// ---- the tab ---------------------------------------------------------------
async function loadPets() {
  const v = document.getElementById('petsView');
  const [ds, pd, ub, info] = await Promise.all([
    api('devices'),
    api('pets'),
    api('pets/unbound').catch(() => ({})),
    api('info').catch(() => ({})),
  ]);
  const pets = pd.pets || [];
  const unbound = ub.unbound || [];
  const aiDevices = ds.filter(d => d.supports_ai);
  // Full detail per AI device: the summary in /api/devices carries no entities
  // and no ai_enabled. A household has one or two of these, so fetching them
  // together costs nothing.
  const aiDetails = (await Promise.all(aiDevices.map(d => fetchDeviceDetail(d.id)))).filter(
    d => d && !d.error,
  );
  const ourFaceIds = pets.flatMap(p => (p.faces || []).map(f => f.id));

  // Everything below the settings depends on at least one device actually
  // recognising: mugshots feed a matcher, so with every matcher off they are
  // configuration for nothing. `ai_enabled` defaults to on when unset, which is
  // what a device that has never been touched reports.
  // Per device, always: one box can be recognising while another is not, and a
  // pet assigned only to the switched-off one is not being matched by anything.
  // `ai_enabled` defaults to on when unset, which is what a device that has
  // never been touched reports.
  const recognisingIds = aiDetails
    .filter(d => !d.aiInfo || d.aiInfo.ai_enabled !== false)
    .map(d => d.id);
  const recognising = recognisingIds.length > 0;

  v.innerHTML = `
    ${aiDetails.map(d => deviceAiCard(d, ourFaceIds)).join('')}
    ${aiDevices.length ? '' : noAiDeviceCard(info.ai_device_names || [])}
    ${aiDevices.length && !recognising ? recognitionOffCard() : ''}
    ${recognising ? petsSection(pets, ds, aiDevices, recognisingIds) : ''}
    ${recognising && unbound.length ? unboundCard(unbound, pets) : ''}`;
}

// The pets themselves — header, add form, and one card each. Rendered only when
// something is there to recognise them; see `loadPets`.
function petsSection(pets, ds, aiDevices, recognisingIds) {
  return `
    <div class="card">
      <div class="row" style="align-items:baseline">
        <h3 style="flex:1;margin:0">Pets</h3>
        <span class="mut">${pets.length} pet${pets.length === 1 ? '' : 's'}</span>
      </div>
      <p class="sub" style="margin-top:8px">
        Give each cat up to ${MAX_FACES} mugshots. The device downloads them and matches
        against them itself — more angles, better recognition.
      </p>
      <div class="newpet">
        <input id="newPetName" placeholder="Name" aria-label="Pet name">
        <select id="newPetDevice" aria-label="Assign to device">${aiDevices
          .map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
          .join('')}</select>
        <button class="act" data-action="add-pet">Add pet</button>
      </div>
    </div>
    ${pets.length ? pets.map(p => petCard(p, ds, recognisingIds)).join('') : emptyPets()}`;
}

// Recognition is off everywhere. The pets are still stored — this only hides a
// section that would be configuring a matcher nobody is running, and the toggle
// that changes it is the card directly above.
// No registered device can recognise anything, so there is nothing to
// configure. The product list comes from the server rather than being spelled
// out here, which is what let the old copy go stale when a fountain joined it.
function noAiDeviceCard(names) {
  return `<div class="card"><h3>No AI-capable device</h3>
    <p class="sub" style="margin:0">None of your devices does on-device recognition${
      names.length ? ` — that is ${names.map(esc).join(', ')}` : ''
    }. A device not on that list earns it by asking: the first time one requests face
    data we remember it, which is how the newer YumShare models are recognised despite
    sharing a codename with older ones that have no camera AI.</p></div>`;
}

function recognitionOffCard() {
  return `<div class="card empty">
    <div class="big">\u{1F634}</div>
    <b>Pet recognition is off</b>
    <p class="sub" style="margin:6px 0 0">Your mugshots are kept, but nothing is matching
      against them. Turn <b>Recognise pets</b> back on above to manage them.</p>
  </div>`;
}

// The per-device AI settings. They sit here rather than on the device page
// because they are about the pets: recognition, whether the box listens for
// yowling, whether it reads urine pH. The device page keeps a pointer.
function deviceAiCard(d, ourFaceIds) {
  const ents = (d.entities || []).filter(e => AI_ENTITY_KEYS.includes(e.key));
  const enabled = d.aiInfo ? d.aiInfo.ai_enabled : true;
  return `<div class="card">
    <div class="row" style="align-items:baseline">
      <h3 style="flex:1;margin:0">On-device AI</h3>
      <span class="mut">${esc(d.name)}</span>
    </div>
    <p class="sub" style="margin-top:8px">What this device's camera watches and listens for.</p>
    <div class="ctrls">
      <label class="ctrl"><span>Recognise pets</span><span class="sw"><input type="checkbox" ${
        enabled ? 'checked' : ''
      } data-change="set-ai" data-id="${esc(d.id)}"><span class="sl"></span></span></label>
      ${ents.map(e => controlRow(d.id, e)).join('')}
    </div>
    ${facesOnDevice(d, ourFaceIds)}
  </div>`;
}

function emptyPets() {
  return `<div class="card empty">
    <div class="big">🐈</div>
    <b>No pets yet</b>
    <p class="sub" style="margin:6px 0 0">Add one above, then give it a few mugshots.
      Until then the box records visits without saying who made them.</p>
  </div>`;
}

function petCard(p, ds, recognisingIds) {
  let devIds = [];
  try {
    devIds = JSON.parse(p.device_ids_json || '[]');
  } catch (e) {
    /* a pet linked to nothing reads better than a broken card */
  }
  // A chip per device this pet is assigned to, dimmed when that particular box
  // is not recognising — the assignment is still real, it is just not doing
  // anything there. Without this a pet on two devices looks equally active on
  // both when only one is matching.
  const off = id => Array.isArray(recognisingIds) && !recognisingIds.includes(id);
  const chips = devIds
    .map(id => {
      const d = ds.find(x => x.id === id);
      const name = esc(d ? d.name : '#' + id);
      return off(id)
        ? `<span class="badge off" title="Recognition is off on this device">${name} · off</span>`
        : `<span class="badge">${name}</span>`;
    })
    .join(' ');
  const faces = p.faces || [];
  const full = faces.length >= MAX_FACES;

  return `<div class="card petcard">
    <div class="pet-head">
      <b class="pet-name">${esc(p.name)}</b>
      <span class="pet-devs">${chips || '<span class="mut">no devices assigned</span>'}</span>
      <button class="ghost act" data-action="delete-pet" data-id="${esc(p.id)}">Delete</button>
    </div>
    <div class="tiles">
      ${faces
        .map(
          f => `<div class="tile">
        <img src="${BASE}${esc(f.url)}" alt="Mugshot ${esc(f.id)}">
        <button class="tile-x" title="Remove this photo"
                data-action="delete-face" data-id="${esc(p.id)}" data-face="${esc(f.id)}">✕</button>
      </div>`,
        )
        .join('')}
      ${
        full
          ? ''
          : `<button class="tile tile-add" data-action="crop-open"
                     data-id="${esc(p.id)}" data-name="${esc(p.name)}">
               <span class="plus">+</span><span>Add mugshot</span>
             </button>`
      }
    </div>
    <div class="pet-foot mut">
      ${faces.length} of ${MAX_FACES} mugshots${full ? ' · full' : ''}
    </div>
  </div>`;
}

// Identities the device reports that match no pet. Almost always ids a box
// cached from PetKit's cloud before the takeover — we show them and let the
// user say who they are, rather than guessing (with one pet in the table a
// guess would even look right, and be wrong the day a second animal appears).
function unboundCard(unbound, pets) {
  if (!pets.length) return '';
  return `<div class="card">
    <h3>Unrecognized identities</h3>
    <p class="sub">Your box reported these pet ids, but they are not ours — it is still matching
      against mugshots cached from PetKit's cloud. Bind one to name its past events. Once the
      device picks up your own photos it reports our ids instead and this list empties by itself.</p>
    ${unbound
      .map(
        u => `<div class="unbound">
      <code>#${esc(u.pet_ref)}</code>
      <span class="mut">${esc(u.count)} event${u.count === 1 ? '' : 's'}${
        u.last_ts ? ', last ' + esc(new Date(u.last_ts * 1000).toLocaleString()) : ''
      }</span>
      <span class="grow"></span>
      <select data-role="bind-pet">${pets
        .map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
        .join('')}</select>
      <button class="mini" data-action="bind-pet-ref" data-ref="${esc(u.pet_ref)}">Bind</button>
    </div>`,
      )
      .join('')}
  </div>`;
}

onAction('add-pet', () => addPet());
onAction('delete-pet', el => deletePet(el.dataset.id));
onAction('delete-face', el => deleteFace(el.dataset.id, el.dataset.face));
onAction('crop-open', el => openCropper(el.dataset.id, el.dataset.name));
onAction('bind-pet-ref', el =>
  bindPetRef(el.dataset.ref, el.closest('.unbound').querySelector('[data-role=bind-pet]').value),
);

async function addPet() {
  const name = document.getElementById('newPetName').value.trim();
  if (!name) return toast('Name required');
  const devSel = document.getElementById('newPetDevice');
  const device_ids = devSel.value ? [Number(devSel.value)] : [];
  const r = await api('pets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name, device_ids: device_ids }),
  });
  toast(r.pet ? 'Pet added' : 'Error: ' + (r.error || 'failed'));
  loadPets();
}

async function deletePet(id) {
  if (!confirm('Delete this pet, and every mugshot it has?')) return;
  await api('pets/' + encodeURIComponent(id), { method: 'DELETE' });
  loadPets();
}

async function deleteFace(petId, faceId) {
  // Confirmed like delete-pet: this unlinks a file, and the device keeps
  // matching against it until its next poll, so it is not trivially undone.
  if (!confirm('Remove this mugshot?')) return;
  await api('pets/' + encodeURIComponent(petId) + '/faces/' + encodeURIComponent(faceId), {
    method: 'DELETE',
  });
  loadPets();
}

async function bindPetRef(ref, petId) {
  // Additive: read the pet's current aliases first, or binding a second
  // identity would silently drop the first.
  const cur = (await api('pets/' + encodeURIComponent(petId))).pet || {};
  let ids = [];
  try {
    ids = JSON.parse(cur.device_pet_ids_json || '[]');
  } catch (e) {
    /* an unreadable alias list is replaced, not extended */
  }
  if (!ids.includes(Number(ref))) ids.push(Number(ref));
  const r = await api('pets/' + encodeURIComponent(petId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_pet_ids: ids }),
  });
  toast(
    r.pet
      ? `Bound — ${r.bound_events} past event${r.bound_events === 1 ? '' : 's'} attributed`
      : 'Error: ' + (r.error || 'failed'),
  );
  loadPets();
}

// Is this page the one the server would serve now?
//
// The Setup tab already shows the running version, and it answers a different
// question: it comes from `/api/info`, so it reports the SERVER's build. A
// fresh server behind a stale page reports the new number while running the old
// code, which is exactly the case that keeps costing an afternoon — a fix is
// deployed, served, and the panel quietly goes on doing the old thing with
// nothing anywhere to say so.
//
// `PANEL_ASSET_V` came with the document and may have come out of a cache;
// `asset_version` from `/api/info` is answered live. Only a stale document can
// make them differ, so the check has no other way to fire.
async function checkPageIsCurrent() {
  const mine = window.PANEL_ASSET_V;
  if (!mine) return; // an older index, or one served without the stamp
  const info = await api('info').catch(() => null);
  if (!info || !info.asset_version || info.asset_version === mine) return;
  const bar = document.createElement('div');
  bar.className = 'stale-banner';
  bar.innerHTML =
    '<b>This page is out of date.</b> The app has been updated since it was ' +
    'loaded, and your browser is still running the older panel. ' +
    '<button class="mini" data-action="reload-panel">Reload</button>';
  document.body.prepend(bar);
}
onAction('reload-panel', () => location.reload());

loadDevices();
connectWS();
checkPageIsCurrent();
