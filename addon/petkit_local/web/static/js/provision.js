import { api, esc, toast } from './core.js';
import { onAction } from './delegate.js';

// ---------------- BLE provisioning (Web Bluetooth) ----------------
//
// TWO protocols, because PetKit uses two chips. Which one a device speaks is
// decided by asking it, not by a model table: connect, then look for whichever
// GATT service it exposes. A table would need updating for every codename and
// would be wrong the first time a model shipped a different board.
// 1. PetKit Ingenic devices (T5/T6/T7/D4H/D4SH/W7H): framed JSON written to
//    0xAAA2, answered on 0xAAA1.
// 2. PetKit ESP32 devices (T4, D4, D4S): PetKit JSON carried as custom
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
// Read, never sent. Provisioning is driven entirely by the PetKit keys inside
// custom data, so nothing here decides anything — but a device that fails at
// the BLUFI layer says so in one of these two, and without them that arrives
// as "ignored ESP32 packet subtype 0x12". Values from ESP-IDF's own
// `btc_blufi_prf.h`.
const BLUFI_DATA_WIFI_REP = 0x0f;
const BLUFI_DATA_ERROR_INFO = 0x12;
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
  // broken page. Same source as that card, so the two cannot disagree.
  btn.title = provisionWarning(hasBt, secure).tooltip;
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
  if (!secure)
    return {
      card: '⚠ Web Bluetooth only works on a <b>secure page</b>, and this one is plain HTTP. Serve Home Assistant over HTTPS with a trusted certificate, or access this panel through a localhost proxy on the computer with Bluetooth. You will need <b>Chrome or Edge</b> as well; a plain-HTTP page cannot tell whether you have one, because the browser hides Web Bluetooth entirely until the page is secure.',
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
      "⚠ The apiServers URL uses a <code>.local</code> mDNS host — most embedded PetKit devices can't resolve mDNS. Use your HA host's <b>IP</b> instead (e.g. <code>http://&lt;ha-host-ip&gt;/6/</code>).",
    );
  }
  try {
    const u = new URL(value);
    const port = u.port || (u.protocol === 'http:' ? '80' : u.protocol === 'https:' ? '443' : '');
    if (port && port !== '80') {
      out.push(
        '⚠ ESP32 devices such as T4, D4 and D4S require the API server on port <b>80</b>. Provisioning them will fail with this URL.',
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
  if (pktType === BLUFI_TYPE_DATA && subtype === BLUFI_DATA_WIFI_REP) {
    // opmode, sta_conn_state, softap_conn_num, then TLVs
    return 'wifi status: ' + (data[1] === 0 ? 'CONNECTED' : 'not connected (' + data[1] + ')');
  }
  if (pktType === BLUFI_TYPE_DATA && subtype === BLUFI_DATA_ERROR_INFO) {
    return 'BLUFI error report, code ' + data[0];
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
    plog(
      live
        ? 'timed out waiting for the device identity.'
        : 'the device disconnected before identifying itself.',
    );
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
    'the device did not report joining within ' +
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
    // This is the value the device starts life with. It is not the only way
    // to set one: a `property.set` carrying `timezone` as a STRING moves the
    // clock on a device that is already paired (Devices -> Timezone), which
    // is how a box provisioned before this field was sent gets its watermarks
    // corrected without being provisioned again.
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

export {
  loadProvision,
  provisionWarning,
  provisionUrlWarning,
  blufiExplain,
  pkCrc16,
  pkFrame,
  pkParse,
  pkBytes,
  pkJoined,
  pkJoinFailed,
  pkJoinWarn,
  pkJoinState,
  PK_MAGIC,
  PK_TAIL,
  PK_TYPE_OUT,
  PK_JOIN_STATES,
  PK_JOIN_DONE,
  PK_JOIN_FAILED,
  PK_JOIN_WARN,
  BLUFI_TYPE_DATA,
  BLUFI_DATA_CUSTOM,
  BLUFI_FC_FRAG,
  BLUFI_DATA_WIFI_REP,
  BLUFI_DATA_ERROR_INFO,
};
