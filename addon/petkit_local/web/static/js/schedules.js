import { api, esc, toast } from './core.js';
import { onAction, onChange, onSection } from './delegate.js';
import { help } from './help.js';
import { DEV_DETAIL, devPanel, ENTITY_SECTION, scheduleDetail } from './devices.js';
import { setEntity } from './commands.js';
import { controlRow } from './entities.js';

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

// --- schedules --------------------------------------------------------------
// PetKit has one scheduling model and four shapes of it, and all but one count
// MINUTES SINCE LOCAL MIDNIGHT with Sunday as weekday 1. The exception is a
// feeder meal's `t`, which counts SECONDS since local midnight — the cloud's
// `n_46560` fires at 12:56:00 (D4SH capture, 2026-08-12). See events/codes.py,
// which is where those facts are written down.
//
// A range whose end is below its start crosses midnight and is correct; a
// one-minute range is something the app itself produced. So nothing here
// sorts, merges or "tidies" a schedule — it only refuses minutes outside a day.

// Working copies, so a background repaint (every ~10s) cannot throw away rows
// somebody just added. The panel-body guard only covers a focused element, and
// adding a row moves focus off it.
const SCHEDULE_EDITS = new Map();
const schedKey = (id, target) => id + ':' + target;

// 1440 is the end of the day, and no <input type="time"> can hold it — 24:00 is
// not a valid time. It only ever appears as `[0, 1440]`, which is PetKit's own
// "entire day", so a row is either ALL DAY or a pair of clocks. That is also the
// vocabulary the app uses, and it saves explaining why one end of a range has a
// checkbox nailed to it.
const DAY_END = 1440;
const isAllDay = pair => pair[0] === 0 && pair[1] === DAY_END;

// The same sentence for every empty schedule. "Not set" on its own reads as a
// gap somebody forgot to fill; the second half is the part that matters — this
// app sets nothing for you, and unset means unrestricted, not broken.
const SCHED_EMPTY =
  '<p class="sub">Not set — nothing here restricts the device. Add a period to change that.</p>';
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

const minToClock = m => {
  const n = Number(m);
  if (!Number.isFinite(n) || n < 0 || n > DAY_END) return '';
  return `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(Math.floor(n) % 60).padStart(2, '0')}`;
};
const clockToMin = s => {
  const parts = String(s).split(':');
  if (parts.length < 2) return null;
  const h = Number(parts[0]),
    m = Number(parts[1]);
  if (!Number.isInteger(h) || !Number.isInteger(m) || h < 0 || h > 23 || m < 0 || m > 59)
    return null;
  return h * 60 + m;
};
// Feeder meals only: `t` is seconds since midnight on the wire.
const secToClock = s => minToClock(Math.floor(Number(s) / 60));
const clockToSec = s => {
  const m = clockToMin(s);
  return m === null ? null : m * 60;
};

function schedValue(id, t) {
  const k = schedKey(id, t.target);
  if (SCHEDULE_EDITS.has(k)) return SCHEDULE_EDITS.get(k);
  return JSON.parse(JSON.stringify(t.value ?? (t.kind === 'feed' ? { schedule: [] } : [])));
}

function schedEdit(id, target, fn, repaint = true) {
  const d = DEV_DETAIL.get(Number(id));
  const t = (d.schedules || []).find(s => s.target === target);
  if (!t) return;
  const value = schedValue(Number(id), t);
  const next = fn(value);
  SCHEDULE_EDITS.set(schedKey(Number(id), target), next === undefined ? value : next);
  if (repaint) repaintSchedules(Number(id));
}

// A value typed into a text or number field: store the edit and reveal Save
// WITHOUT repainting. Re-rendering the card would replace the very <input> being
// typed in, and an <input type="time"> commits `01:00` the instant its hour
// segment reads `1` (the minute is already `00`), firing `change` mid-entry --
// so a repaint there steals focus before the second digit and the meal lands an
// hour early. The dirty class and toolbar are added in place instead; the next
// full repaint renders the identical thing from SCHEDULE_EDITS.
function schedEditLive(el, fn) {
  schedEdit(el.dataset.id, el.dataset.target, fn, false);
  const sched = el.closest('.sched');
  if (!sched || sched.classList.contains('dirty')) return;
  sched.classList.add('dirty');
  const head = sched.querySelector('.sched-head');
  if (head && !head.querySelector('.sched-actions'))
    head.insertAdjacentHTML('beforeend', schedActions(el.dataset.id, el.dataset.target));
}

function repaintSchedules(id) {
  const panel = devPanel(id);
  const card = panel && panel.querySelector('.sched-card');
  const d = DEV_DETAIL.get(id);
  if (card && d) card.outerHTML = renderSchedules(d, scheduleEntities(d));
}

// Everything routed to this card: the raw-JSON `text` entities and now the
// `time` ones too. `renderSchedules` splits them by component.
const scheduleEntities = d =>
  (d.entities || []).filter(e => ENTITY_SECTION[e.component] === 'schedules');

// One [from, to] row: either the whole day, or two clocks. `All day` is a real
// state and not a formatting trick — `[0, 1440]` is the only place 1440 ever
// appears, in every payload PetKit sends.
function rangeRow(id, target, pair, idx, path) {
  const [from, to] = pair;
  const allDay = isAllDay(pair);
  const dat = ` data-id="${esc(id)}" data-target="${esc(target)}" data-idx="${esc(idx)}" data-path="${esc(path ?? '')}"`;
  const times = allDay
    ? '<span class="sched-allday">All day</span>'
    : `<input type="time" value="${esc(minToClock(from))}" data-change="sched-from"${dat}>
       <span class="sched-arrow">→</span>
       <input type="time" value="${esc(minToClock(to))}" data-change="sched-to"${dat}>
       ${to < from ? '<span class="mut sched-note">next day</span>' : ''}`;
  return `<div class="sched-row">
    <span class="sched-vals">${times}</span>
    <span class="sched-acts">
      <button class="link" data-action="sched-toggle-allday"${dat}>${allDay ? 'Pick times' : 'All day'}</button>
      <button class="link danger" data-action="sched-drop-range"${dat} title="Remove this period" aria-label="Remove this period">✕</button>
    </span>
  </div>`;
}

function weekdayBar(id, target, repeats, idx) {
  const on = new Set(
    String(repeats || '')
      .split(',')
      .map(Number),
  );
  const dat = ` data-id="${esc(id)}" data-target="${esc(target)}" data-idx="${esc(idx)}"`;
  return `<div class="sched-days">${WEEKDAYS.map(
    (name, i) =>
      `<label class="${on.has(i + 1) ? 'on' : ''}"><input type="checkbox" ${on.has(i + 1) ? 'checked' : ''} data-change="sched-day" data-day="${i + 1}"${dat}><span>${esc(name)}</span></label>`,
  ).join('')}</div>`;
}

function renderRangesEditor(id, t, value) {
  const rows = value.length
    ? value.map((pair, i) => rangeRow(id, t.target, pair, i)).join('')
    : SCHED_EMPTY;
  return `${rows}
    <button class="link" data-action="sched-add-range" data-id="${esc(id)}" data-target="${esc(t.target)}">+ Add period</button>`;
}

function renderWeeklyEditor(id, t, value) {
  const dat = ` data-id="${esc(id)}" data-target="${esc(t.target)}"`;
  const add = `<button class="link"${dat} data-action="sched-add-weekly">+ Add entry</button>`;
  if (!value.length) return `${SCHED_EMPTY}${add}`;
  return `${value
    .map(
      (entry, i) => `<div class="sched-entry">
      <div class="sched-row">
        <span class="sched-vals">
          <label class="sw-inline"><input type="checkbox" ${entry.enable ? 'checked' : ''} data-change="sched-enable"${dat} data-idx="${esc(i)}"><span>Enabled</span></label>
          ${weekdayBar(id, t.target, entry.rpt, i)}
        </span>
        <span class="sched-acts">
          <button class="link danger"${dat} data-idx="${esc(i)}" data-action="sched-drop-weekly" title="Remove this entry" aria-label="Remove this entry">✕</button>
        </span>
      </div>
      ${(entry.time || []).map((pair, j) => rangeRow(id, t.target, pair, j, String(i))).join('')}
      <button class="link"${dat} data-path="${esc(i)}" data-action="sched-add-range">+ Add period</button>
    </div>`,
    )
    .join('')}
    ${add}`;
}

// A feeder's meals. Groups of them share weekdays, which is what `re` is on the
// group and `it` is the meals inside it; each meal is `{id, t, a1, a2}`, read
// out of a D4SH `ctrl` (see events/codes.py::FEED_SCHEDULE_ITEM_KEYS). `a2` is
// the second hopper and only a Dual-Hopper has one.
function renderFeedEditor(id, t, value) {
  const groups = Array.isArray(value.schedule) ? value.schedule : [];
  const dat = ` data-id="${esc(id)}" data-target="${esc(t.target)}"`;
  const add = `<button class="link"${dat} data-action="sched-add-feed-group">+ Add days</button>`;
  if (!groups.length)
    return `<p class="sub">No meals set — the feeder dispenses only when you press Feed.</p>${add}`;

  return `${groups
    .map(
      (group, gi) => `<div class="sched-entry">
      <div class="sched-row">
        <span class="sched-vals">${weekdayBar(id, t.target, group.re, gi)}</span>
        <span class="sched-acts">
          <button class="link danger"${dat} data-idx="${esc(gi)}" data-action="sched-drop-feed-group" title="Remove these days" aria-label="Remove these days">✕</button>
        </span>
      </div>
      ${
        (group.it || []).length
          ? (group.it || [])
              .map(
                (meal, mi) => `<div class="sched-row">
        <span class="sched-vals">
          <input type="time" value="${esc(secToClock(meal.t))}" data-change="sched-meal-time"${dat} data-path="${esc(gi)}" data-idx="${esc(mi)}">
          <label class="sw-inline"><span>${t.dual ? 'Hopper 1' : 'Portions'}</span>
            <input type="number" class="sched-portion" min="0" max="255" step="1" value="${esc(meal.a1 ?? 0)}" data-change="sched-meal-a1"${dat} data-path="${esc(gi)}" data-idx="${esc(mi)}"></label>
          ${
            t.dual
              ? `<label class="sw-inline"><span>Hopper 2</span>
            <input type="number" class="sched-portion" min="0" max="255" step="1" value="${esc(meal.a2 ?? 0)}" data-change="sched-meal-a2"${dat} data-path="${esc(gi)}" data-idx="${esc(mi)}"></label>`
              : ''
          }
        </span>
        <span class="sched-acts">
          <button class="link danger"${dat} data-path="${esc(gi)}" data-idx="${esc(mi)}" data-action="sched-drop-meal" title="Remove this meal" aria-label="Remove this meal">✕</button>
        </span>
      </div>`,
              )
              .join('')
          : '<p class="sub">No meals on these days.</p>'
      }
      <button class="link"${dat} data-path="${esc(gi)}" data-action="sched-add-meal">+ Add meal</button>
    </div>`,
    )
    .join('')}
    ${add}`;
}

// `type` 0 is a cleaning and 1 a deodorizing, and BOTH live in one array — so
// each section edits its own entries and the save merges the whole array back.
// A section that rewrote only its own rows would delete the other job.
const POINT_SECTIONS = [
  { type: 0, name: 'Scheduled cleaning' },
  { type: 1, name: 'Periodic deodorizing' },
];

function renderPointsEditor(id, t, value) {
  const known = new Set(POINT_SECTIONS.map(s => s.type));
  const sections = POINT_SECTIONS.concat(
    value.some(e => !known.has(e.type)) ? [{ type: null, name: 'Other' }] : [],
  );
  return sections
    .map(section => {
      const rows = value
        .map((entry, i) => ({ entry, i }))
        .filter(({ entry }) =>
          section.type === null ? !known.has(entry.type) : entry.type === section.type,
        );
      const dat = ` data-id="${esc(id)}" data-target="${esc(t.target)}"`;
      return `<div class="sched-entry"><div class="sched-sub">${esc(section.name)}</div>
      ${
        rows.length
          ? rows
              .map(
                ({ entry, i }) => `<div class="sched-row">
        <span class="sched-vals">
          <input type="time" value="${esc(minToClock(entry.time))}" data-change="sched-point-time"${dat} data-idx="${esc(i)}">
          ${weekdayBar(id, t.target, entry.repeats, i)}
          ${section.type === null ? `<span class="mut sched-note">type ${esc(entry.type)}</span>` : ''}
        </span>
        <span class="sched-acts">
          <button class="link danger" data-action="sched-drop-point"${dat} data-idx="${esc(i)}" title="Remove this time" aria-label="Remove this time">✕</button>
        </span>
      </div>`,
              )
              .join('')
          : '<p class="sub">No times set.</p>'
      }
      ${section.type === null ? '' : `<button class="link" data-action="sched-add-point"${dat} data-type="${esc(section.type)}">+ Add time</button>`}
    </div>`;
    })
    .join('');
}

// A dirty schedule's Save/Discard pair. Also injected in place by schedEditLive
// when a text field is edited, so it is a function rather than inline markup.
function schedActions(id, target) {
  const dat = ` data-id="${esc(id)}" data-target="${esc(target)}"`;
  return `<span class="sched-actions"><button class="act"${dat} data-action="sched-save">Save</button>
    <button class="link"${dat} data-action="sched-revert">Discard</button></span>`;
}

function renderSchedules(d, sectionEntities) {
  const targets = d.schedules || [];
  const all = sectionEntities || [];
  // Two different shapes share this card. A `time` entity is one moment; a
  // schedule target is a list of ranges. They are the same question to the
  // person reading it, and different enough underneath that the units are
  // spelled out rather than left to be inferred from the controls.
  const times = all.filter(e => e.component === 'time');
  const texts = all.filter(e => e.component !== 'time');
  if (!targets.length && !all.length) return '';
  const editors = targets
    .map(t => {
      const value = schedValue(d.id, t);
      const dirty = SCHEDULE_EDITS.has(schedKey(d.id, t.target));
      let body;
      if (t.kind === 'ranges') body = renderRangesEditor(d.id, t, value);
      else if (t.kind === 'weekly') body = renderWeeklyEditor(d.id, t, value);
      else if (t.kind === 'points') body = renderPointsEditor(d.id, t, value);
      else body = renderFeedEditor(d.id, t, value);
      // Save appears only once there is something to save. Five permanent Save
      // buttons in one card read as five things demanding attention; a button
      // that shows up when you have changed something reads as one.
      return `<div class="sched${dirty ? ' dirty' : ''}">
      <div class="sched-head"><span class="sched-name">${esc(t.name)}</span>
        ${dirty ? schedActions(d.id, t.target) : ''}</div>
      ${body}</div>`;
    })
    .join('');

  return `<div class="card sched-card"><h3>Schedules${help(
    'The device runs these on its own clock — this app answers when it asks. Times are local, and nothing is set for you: a schedule you have not filled in is empty, and the device is simply not restricted. A period that ends before it starts runs through midnight, which is normal and is how PetKit’s own app writes quiet hours.',
  )}</h3>
    ${
      times.length
        ? `<div class="ctrls">${times.map(e => controlRow(d.id, e)).join('')}</div>
    <p class="sub">Those two are a single time of day. The periods below are RANGES — a start and an end — which is why they look different.</p>`
        : ''
    }
    <div class="sched-grid">${editors}</div>
    ${
      texts.length
        ? `<details class="sched-raw"><summary>Raw JSON</summary>
      <p class="sub">What the device is actually served. Also the only way to edit a shape the
        editor above does not know — nothing here rewrites a field it does not understand.</p>
      ${texts
        .map(
          e => `<div class="sched"><div class="sched-head"><span class="sched-name">${esc(e.name)}</span></div>
        <textarea class="sched-json" rows="4" data-id="${esc(d.id)}" data-key="${esc(e.key)}">${esc(typeof e.value === 'string' ? e.value : JSON.stringify(e.value ?? '', null, 2))}</textarea>
        <button class="ghost act" data-action="save-schedule" data-id="${esc(d.id)}" data-key="${esc(e.key)}">Save ${esc(e.name)}</button></div>`,
        )
        .join('')}</details>`
        : ''
    }</div>`;
}

// The Schedules card is part of a device panel, and `devices.js` draws it —
// but this module reads that module's cached detail, so the render is offered
// by name instead of imported back (see `onSection` in delegate.js).
onSection('schedules', renderSchedules);

// A range lives either at the top level (`ranges`) or inside one weekly entry
// (`weekly`); `data-path` says which, and is the entry index when it is set.
const rangeListOf = (value, path) => (path === '' ? value : value[Number(path)].time);

onChange('sched-from', el =>
  schedEditLive(el, v => {
    const m = clockToMin(el.value);
    if (m !== null) rangeListOf(v, el.dataset.path)[Number(el.dataset.idx)][0] = m;
  }),
);
onChange('sched-to', el =>
  schedEditLive(el, v => {
    const m = clockToMin(el.value);
    if (m !== null) rangeListOf(v, el.dataset.path)[Number(el.dataset.idx)][1] = m;
  }),
);
onAction('sched-toggle-allday', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    const list = rangeListOf(v, el.dataset.path);
    const idx = Number(el.dataset.idx);
    // Leaving "all day" collapses to a zero-length window at midnight rather
    // than inventing an hour — the two clocks appear and you pick them. Going
    // the other way is the one value that means the whole day.
    list[idx] = isAllDay(list[idx]) ? [0, 0] : [0, DAY_END];
  }),
);
onAction('sched-drop-range', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    rangeListOf(v, el.dataset.path).splice(Number(el.dataset.idx), 1);
  }),
);
onAction('sched-add-range', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    // [0, 1440] — the whole day, which is what PetKit's own "entire day"
    // payload is, and the one starting point that changes nothing.
    rangeListOf(v, el.dataset.path ?? '').push([0, DAY_END]);
  }),
);
// A feeder's schedule is an object with the groups inside it; every other kind
// is the list itself.
const entryListOf = v => (Array.isArray(v) ? v : v.schedule);

onChange('sched-day', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    const entry = entryListOf(v)[Number(el.dataset.idx)];
    // The day set has three spellings and this bar renders all of them: a feeder
    // group's `re`, a weekly entry's `rpt`, a cleaning point's `repeats`. Write
    // back the one THIS entry actually carries — resolving `re` last put every
    // feeder click on a stray `repeats` the device never reads, so the weekdays
    // repainted straight back from the untouched `re`.
    const field = 're' in entry ? 're' : 'rpt' in entry ? 'rpt' : 'repeats';
    const days = new Set(
      String(entry[field] || '')
        .split(',')
        .map(Number)
        .filter(Boolean),
    );
    const day = Number(el.dataset.day);
    if (el.checked) days.add(day);
    else days.delete(day);
    entry[field] = [...days].sort((a, b) => a - b).join(',');
  }),
);
onChange('sched-enable', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    v[Number(el.dataset.idx)].enable = el.checked ? 1 : 0;
  }),
);
onAction('sched-add-weekly', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    v.push({ enable: 1, rpt: '1,2,3,4,5,6,7', time: [[0, DAY_END]] });
  }),
);
onAction('sched-drop-weekly', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    v.splice(Number(el.dataset.idx), 1);
  }),
);
onChange('sched-point-time', el =>
  schedEditLive(el, v => {
    const m = clockToMin(el.value);
    if (m !== null) v[Number(el.dataset.idx)].time = m;
  }),
);
onAction('sched-drop-point', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    v.splice(Number(el.dataset.idx), 1);
  }),
);
onAction('sched-add-point', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    // The cloud hands these ids out of one pool shared by both job types — the
    // T5 capture went 110969 then 110970 across a cleaning and a deodorizing
    // entry. With no cloud to ask, one above the highest we hold keeps that
    // property and cannot collide with an entry already on the device.
    const next = v.reduce((max, e) => Math.max(max, Number(e.id) || 0), 0) + 1;
    v.push({
      id: next,
      repeats: '1,2,3,4,5,6,7',
      time: 0,
      type: Number(el.dataset.type),
    });
  }),
);
// --- feeder meals -----------------------------------------------------------
const feedGroup = (v, path) => v.schedule[Number(path)];

onAction('sched-add-feed-group', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    if (!Array.isArray(v.schedule)) v.schedule = [];
    v.schedule.push({ re: '1,2,3,4,5,6,7', it: [], itemJsonString: '[]' });
  }),
);
onAction('sched-drop-feed-group', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    v.schedule.splice(Number(el.dataset.idx), 1);
  }),
);
onAction('sched-add-meal', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    const group = feedGroup(v, el.dataset.path);
    if (!Array.isArray(group.it)) group.it = [];
    // Ids only have to be distinct within the schedule the device is handed;
    // one above the highest we hold keeps that true without a cloud to ask.
    const next =
      v.schedule.reduce(
        (max, g) => (g.it || []).reduce((m, meal) => Math.max(m, Number(meal.id) || 0), max),
        0,
      ) + 1;
    group.it.push({ id: next, t: 0, a1: 1, a2: 0 });
  }),
);
onAction('sched-drop-meal', el =>
  schedEdit(el.dataset.id, el.dataset.target, v => {
    feedGroup(v, el.dataset.path).it.splice(Number(el.dataset.idx), 1);
  }),
);
onChange('sched-meal-time', el =>
  schedEditLive(el, v => {
    const s = clockToSec(el.value);
    if (s !== null) feedGroup(v, el.dataset.path).it[Number(el.dataset.idx)].t = s;
  }),
);
const mealAmount = (el, field) =>
  schedEditLive(el, v => {
    const n = Number(el.value);
    if (Number.isInteger(n) && n >= 0 && n <= 255)
      feedGroup(v, el.dataset.path).it[Number(el.dataset.idx)][field] = n;
  });
onChange('sched-meal-a1', el => mealAmount(el, 'a1'));
onChange('sched-meal-a2', el => mealAmount(el, 'a2'));

onAction('sched-revert', el => {
  SCHEDULE_EDITS.delete(schedKey(Number(el.dataset.id), el.dataset.target));
  repaintSchedules(Number(el.dataset.id));
});
onAction('sched-save', async el => {
  const id = Number(el.dataset.id),
    target = el.dataset.target;
  const d = DEV_DETAIL.get(id);
  const t = (d.schedules || []).find(s => s.target === target);
  if (!t) return;
  try {
    const res = await api(`devices/${id}/schedule`, {
      method: 'POST',
      body: JSON.stringify({ target, value: schedValue(id, t) }),
    });
    if (res && res.error) return toast(res.error);
    SCHEDULE_EDITS.delete(schedKey(id, target));
    toast(
      `Saved — ${res && res.delivered === 'mqtt' ? 'sent to the device' : 'the device picks it up on its next poll'}`,
    );
    scheduleDetail(id);
  } catch (e) {
    toast('Save failed: ' + e.message);
  }
});
