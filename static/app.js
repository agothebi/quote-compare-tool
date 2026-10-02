/* Quote Compare: the broker's screen.
 *
 * Four pages: the board (#/), the archived clients (#/archived), a client's comparison
 * (#/c/<id>[/<line>]) and its proposal (#/c/<id>/proposal). One state object (S). Every change goes: user action -> API call -> the
 * server returns the updated comparison -> the page redraws from state. The server is the source of
 * truth, so a reload always shows the same page. All text from quotes goes through esc().
 *
 * The page's Content-Security-Policy forbids inline style attributes: widths (bars) are set from
 * data-w after each render (applyWidths), and colors come from classes (lc-<line>, st-<stage>).
 */
'use strict';
(() => {

// ---------------------------------------------------------------- basics

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ESC[m]);
const STEPS = ['Reading the document', 'Hiding client details', 'Finding coverages and prices', 'Double-checking every value'];
const STAGE_INDEX = { queued: -1, reading: 0, redacting: 1, extracting: 2, checking: 3 };
const ACTIVE = new Set(['queued', 'reading', 'redacting', 'extracting', 'checking']);
const BAD_FILE = new Set(['failed', 'interrupted', 'empty']);
const STAGES = [
  { key: 'working', label: 'Working on it' }, { key: 'ready', label: 'Ready to send' },
  { key: 'sent', label: 'Sent' }, { key: 'closed', label: 'Closed' },
];
const WARN_NOTICES = new Set(['uninsured_motorist_rejected', 'sum_mismatch']);
const BAD_NOTICES = new Set(['below_umbrella_requirement']);

const S = {
  route: { name: 'boot' },
  build: null,
  board: null,          // cards from GET /api/comparisons
  filter: '',
  archivedCount: 0,     // the board links to the archived clients
  shelf: null,          // archived cards from GET /api/comparisons?archived=1
  shelfFilter: '',
  comp: null,           // the open comparison (the server's view)
  lineBy: {},           // comparison id -> selected line
  rowMode: 'all',       // 'all' | 'diff'
  showOther: false,
  panel: null,          // {type: 'cell'|'quote', line, qid, key?, page?, mode?, newLine?}
  wide: false,
  modal: null,          // {kind, ...}
  batch: null,          // files being added: {cid, name, running, files: [{name, file, fid, status, summary, error, redactions}]}
  pollTimer: null, boardTimer: null, pvTimer: null,
  view: 'pdf',          // proposal preview: 'pdf' | 'mail'
  pv: { id: null, rev: null },
  dragging: null,
  pendingReview: null,
};
const N = { dirty: false, saving: false, again: false, base: '', timer: null };  // notes autosave

class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

async function api(method, path, body, form) {
  const opts = { method, headers: { 'X-Quote-Compare': '1' } };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { throw new ApiError("Can't reach the app. Check that it is still running.", 0); }
  const build = r.headers.get('X-App-Build');
  if (build && S.build && build !== S.build) updateNotice('reload');
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new ApiError((data && data.error) || 'Something went wrong. Try again.', r.status, data);
  return data;
}

// Writes to one comparison go one at a time, so their answers can never arrive out of order.
let writeChain = Promise.resolve();
function write(method, path, body, form) {
  const run = writeChain.then(() => api(method, path, body, form));
  writeChain = run.catch(() => {});
  return run;
}
const compPath = (id, rest = '') => `/api/comparisons/${encodeURIComponent(id)}${rest}`;
const patchComp = body => write('PATCH', compPath(S.comp.id), body);

function toast(text, opts = {}) {
  const el = document.createElement('div');
  el.className = 'toast' + (opts.bad ? ' bad' : '');
  const span = document.createElement('span');
  span.textContent = text;
  el.appendChild(span);
  if (opts.action) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = opts.action.label;
    b.addEventListener('click', () => { el.remove(); Promise.resolve().then(opts.action.fn).catch(fail); });
    el.appendChild(b);
  }
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), opts.action || opts.bad ? 6000 : 2600);
}

function fail(e) {
  toast(e instanceof ApiError ? e.message : 'Something went wrong. Try again.', { bad: true });
  if (!(e instanceof ApiError)) console.error(e);
}

const plural = (n, w, many) => `${n} ${n === 1 ? w : (many || w + 's')}`;
const lower = label => (/^[A-Z]{2,}$/.test(label) ? label : String(label).toLowerCase());  // 'RV' stays 'RV'
const joinWords = a => (a.length < 3 ? a.join(' and ') : a.slice(0, -1).join(', ') + ' and ' + a[a.length - 1]);
const stageLabel = k => (STAGES.find(s => s.key === k) || STAGES[0]).label;
const lineClass = line => `lc-${/^[a-z_]+$/.test(line) ? line : 'unknown'}`;
const norm = v => String(v || '').toLowerCase().replace(/(\d)\.00\b/g, '$1').replace(/[\s,.]/g, '');  // "$1,000.00" reads as "$1,000"

function fmtDate(iso, year = true) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('en-US', year ? { month: 'short', day: 'numeric', year: 'numeric' } : { month: 'short', day: 'numeric' });
}

function rel(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const min = Math.round((Date.now() - d) / 60000);
  if (min < 2) return 'just now';
  if (min < 60) return `${min}m ago`;
  if (min < 60 * 24) return `${Math.round(min / 60)}h ago`;
  if (min < 60 * 48) return 'yesterday';
  return fmtDate(iso, new Date().getFullYear() !== d.getFullYear());
}

// A printed expiry date (YYYY-MM-DD from the server): "Expires Oct 15", or "Expired Jan 3" once past.
function expiryText(ymd) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd || '');
  if (!m) return '';
  const date = new Date(+m[1], +m[2] - 1, +m[3]);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const opts = { month: 'short', day: 'numeric' };
  if (date.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
  const s = date.toLocaleDateString('en-US', opts);
  return date < today ? `Expired ${s}` : `Expires ${s}`;
}

// Bars: the width comes from data-w (0-100). Set through the DOM, which the CSP allows.
function applyWidths(root = document) {
  $$('[data-w]', root).forEach(el => { el.style.width = Math.max(0, Math.min(100, +el.dataset.w || 0)) + '%'; });
}

// Keeps keyboard focus on "the same" control across a redraw.
function focusKey() {
  const a = document.activeElement;
  if (!a || a === document.body || !$('#app').contains(a)) return null;
  const attrs = ['data-act', 'data-r', 'data-c', 'data-line', 'data-k', 'data-id', 'data-fact', 'data-own', 'data-rec-line', 'data-opt', 'data-v'];
  const sel = attrs.filter(n => a.hasAttribute(n)).map(n => `[${n}="${CSS.escape(a.getAttribute(n))}"]`).join('');
  if (a.id) return '#' + CSS.escape(a.id);
  if (sel) return a.tagName.toLowerCase() + sel;
  const form = a.closest('form[data-line]');
  return form ? `form[data-line="${CSS.escape(form.dataset.line)}"] input` : null;
}
function restoreFocus(key) {
  if (!key) return;
  const el = $(key, $('#app'));
  if (el) el.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- routing

function parseHash() {
  const h = location.hash.replace(/^#/, '') || '/';
  if (h === '/archived') return { name: 'archived' };
  const m = h.match(/^\/c\/([a-z0-9][a-z0-9_-]{0,80})(?:\/([a-z_]+))?$/);
  if (!m) return { name: 'board' };
  if (m[2] === 'proposal') return { name: 'proposal', id: m[1] };
  return { name: 'comp', id: m[1], line: m[2] || null };
}

let routeSeq = 0;
async function route() {
  const seq = ++routeSeq;
  flushNotes();
  closeMenu();
  closeModal(true);
  const r = parseHash();
  if (S.panel && (r.name !== 'comp' || !S.comp || S.comp.id !== r.id)) closePanel(true);
  stopPoll();
  clearTimeout(S.boardTimer);
  clearTimeout(S.pvTimer);
  S.route = r;
  document.body.classList.toggle('on-proposal', r.name === 'proposal');
  if (r.name === 'board') await showBoard(seq);
  else if (r.name === 'archived') await showShelf(seq);
  else await showComparison(r, seq);
}

function setHashLine(line) {
  const h = `#/c/${S.comp.id}/${line}`;
  if (location.hash !== h) history.replaceState(null, '', h);
}

// ---------------------------------------------------------------- board

async function showBoard(seq) {
  document.title = 'Clients · ANT Insurance';
  S.comp = null;
  if (!S.board) $('#app').innerHTML = '<div class="boot">Loading…</div>';
  try { setBoard(await api('GET', '/api/comparisons')); }
  catch (e) {
    if (seq !== routeSeq) return;
    $('#app').innerHTML = `<div class="empty"><h2>The clients could not be loaded</h2><p>${esc(e.message)}</p><button class="btn" data-act="reload">Try again</button></div>`;
    return;
  }
  if (seq !== routeSeq) return;
  renderBoard();
  window.scrollTo(0, 0);
  scheduleBoardRefresh();
  schedulePoll();  // files still being added keep being followed from the board
}

function scheduleBoardRefresh() {
  clearTimeout(S.boardTimer);
  if (S.route.name === 'board' && S.board && S.board.some(c => c.processing)) S.boardTimer = setTimeout(refreshBoard, 1500);
}

function setBoard(r) { S.board = r.comparisons; S.archivedCount = r.archived || 0; }

async function refreshBoard() {
  if (S.route.name === 'archived') { refreshShelf(); return; }
  if (S.route.name !== 'board') return;
  try {
    const r = await api('GET', '/api/comparisons');
    if (S.route.name !== 'board') return;
    setBoard(r);
    if (!S.dragging && !$('#menu')) renderBoard();
  } catch (e) { /* the next refresh tries again */ }
  scheduleBoardRefresh();
}

function batchFor(id) { return S.batch && S.batch.cid === id && batchRunning() ? S.batch : null; }

function cardSignal(c) {
  if (c.processing) {
    const b = batchFor(c.id);
    const text = b ? `Reading ${Math.min(batchDone(b) + 1, b.files.length)} of ${b.files.length} files` : `Reading ${plural(c.processing, 'file')}`;
    return `<span class="muted">${text}</span><span class="bar"><i data-w="${b ? batchPct(b) : c.progress || 0}"></i></span>`;
  }
  if (c.stage === 'closed') {  // once closed, the outcome is what matters, not what was left to check
    if (c.outcome === 'bound') return '<span class="pill good">Bound</span><button class="linkbtn sm" data-act="outcome" data-v="">Change</button>';
    if (c.outcome === 'lost') return '<span class="pill">Not bound</span><button class="linkbtn sm" data-act="outcome" data-v="">Change</button>';
    return '<span class="outc"><button class="btn sm" data-act="outcome" data-v="bound">Bound</button><button class="btn sm" data-act="outcome" data-v="lost">Not bound</button></span>';
  }
  if (c.failed) return `<span class="pill bad">${c.failed === 1 ? "1 file couldn't be read" : `${c.failed} files couldn't be read`}</span>`;
  if (c.review) return `<span class="pill check"><span class="dot"></span>${plural(c.review, 'value')} to check</span>`;
  const bits = [];
  if (!c.quotes) bits.push('<span class="faint">No quotes yet</span>');
  else if (c.picked) bits.push(`<span class="muted">${c.picked} of ${c.lines.length} picked${c.total ? ` · ${c.total_estimated ? '≈ ' : ''}${esc(c.total)}/yr` : ''}</span>`);
  else bits.push('<span class="muted">No picks yet</span>');
  const exp = expiryText(c.expires);
  if (exp && c.stage !== 'sent') bits.push(`<span class="${exp.startsWith('Expired') ? 'bad-t' : 'faint'}">${esc(exp)}</span>`);
  return bits.join('');
}

function cardHtml(c) {
  const chips = c.lines.map(l => `<span><i class="${lineClass(l.key)}"></i>${esc(l.label)} ${l.count}</span>`).join('');
  return `<article class="cc" draggable="true" data-id="${esc(c.id)}" tabindex="0" aria-label="${esc(c.client_name)}, ${esc(stageLabel(c.stage))}">
    <div class="top"><span class="nm">${esc(c.client_name)}</span><button class="icon" data-act="card-menu" data-id="${esc(c.id)}" aria-label="Options for ${esc(c.client_name)}">⋯</button></div>
    ${chips ? `<div class="lchips">${chips}</div>` : ''}
    <div class="sig">${cardSignal(c)}</div>
    <div class="foot"><span>Updated ${esc(rel(c.updated_at))}</span><span>Started ${esc(fmtDate(c.created_at, false))}</span></div>
  </article>`;
}

function renderBoard() {
  if (S.route.name !== 'board' || !S.board) return;
  const fk = focusKey();
  const sel = fk === '#search' ? [$('#search').selectionStart, $('#search').selectionEnd] : null;
  const q = S.filter.trim().toLowerCase();
  const match = c => !q || c.client_name.toLowerCase().includes(q) || (c.carriers || []).some(x => x.toLowerCase().includes(q));
  const open = S.board.filter(c => c.stage !== 'closed').length;
  const cols = STAGES.map(st => {
    const cs = S.board.filter(c => c.stage === st.key && match(c));
    const empty = q ? 'No matches' : st.key === 'working' && !S.board.length ? 'Start with New client' : 'Drag a client here';
    return `<section class="col" data-stage="${st.key}" aria-label="${st.label}">
      <h2><span class="sw st-${st.key}"></span>${st.label}<span class="n">${cs.length}</span></h2>
      <div class="cards">${cs.map(cardHtml).join('') || `<div class="empty-col">${empty}</div>`}</div></section>`;
  }).join('');
  $('#app').innerHTML = topLine(false) + `<header class="phead"><div>
      <h1>Clients</h1><div class="meta">${plural(open, 'open comparison')}${S.board.length ? ' · drag a card when its status changes' : ''}</div></div>
    <div class="acts">${S.archivedCount ? `<a class="linkbtn" href="#/archived">Archived (${S.archivedCount})</a>` : ''}<input class="inp search" id="search" type="search" placeholder="Find a client or carrier" aria-label="Find a client or carrier" value="${esc(S.filter)}">
      <button class="btn primary" data-act="new">New client</button></div></header>
    <div class="board">${cols}</div>`;
  applyWidths($('#app'));
  if (sel) { const box = $('#search'); box.focus(); box.setSelectionRange(sel[0], sel[1]); }  // a redraw never moves the caret
  else restoreFocus(fk);
}

// drag and drop between columns (native HTML5; the ⋯ menu's "Move to" does the same from the keyboard)
let dropLine = null;
function dropIndex(col, y) {
  const cards = $$('.cc:not(.dragging)', col);
  const i = cards.findIndex(el => { const r = el.getBoundingClientRect(); return y < r.top + r.height / 2; });
  return { cards, i: i < 0 ? cards.length : i };
}

async function moveCard(id, stage, index, undoable = true) {
  const card = S.board && S.board.find(c => c.id === id);
  if (!card) return;
  const was = card.stage;
  const wasIndex = S.board.filter(c => c.stage === was).findIndex(c => c.id === id);
  const rest = S.board.filter(c => c.id !== id);  // optimistic: the card lands at once, the server confirms
  const col = rest.filter(c => c.stage === stage);
  const at = Math.max(0, Math.min(index, col.length));
  card.stage = stage;
  if (stage !== 'closed') card.outcome = null;
  const pos = col[at] ? rest.indexOf(col[at]) : col.length ? rest.indexOf(col[col.length - 1]) + 1 : rest.length;
  rest.splice(pos, 0, card);
  S.board = rest;
  renderBoard();
  try { setBoard(await api('POST', '/api/board/move', { id, stage, index: at })); renderBoard(); }
  catch (e) { fail(e); refreshBoard(); return; }
  if (was !== stage && undoable) {
    toast(`${card.client_name} moved to ${stageLabel(stage)}`, { action: { label: 'Undo', fn: () => moveCard(id, was, wasIndex, false) } });
  }
}

// ---------------------------------------------------------------- archived clients (off the board, every file kept)

async function archiveCard(id) {
  const card = S.board && S.board.find(c => c.id === id);
  if (!card) return;
  const wasIndex = S.board.filter(c => c.stage === card.stage).findIndex(c => c.id === id);
  S.board = S.board.filter(c => c.id !== id);  // optimistic, like a move
  S.archivedCount += 1;
  renderBoard();
  try { await api('PATCH', compPath(id), { archived: true }); }
  catch (e) { fail(e); refreshBoard(); return; }
  refreshBoard();
  toast(`${card.client_name} archived`, { action: { label: 'Undo', fn: () => unarchive(id, wasIndex).then(refreshBoard) } });
}

// Back on the board at the top of its column, or at `index` (an undo puts it back where it was).
async function unarchive(id, index = 0) {
  const v = await api('PATCH', compPath(id), { archived: false });
  if (index > 0) await api('POST', '/api/board/move', { id, stage: v.stage, index });
  return v;
}

async function showShelf(seq) {
  document.title = 'Archived clients · ANT Insurance';
  S.comp = null;
  if (!S.shelf) $('#app').innerHTML = '<div class="boot">Loading…</div>';
  try { S.shelf = (await api('GET', '/api/comparisons?archived=1')).comparisons; }
  catch (e) {
    if (seq !== routeSeq) return;
    $('#app').innerHTML = `<div class="empty"><h2>The archived clients could not be loaded</h2><p>${esc(e.message)}</p><button class="btn" data-act="reload">Try again</button></div>`;
    return;
  }
  if (seq !== routeSeq) return;
  renderShelf();
  window.scrollTo(0, 0);
  schedulePoll();
}

async function refreshShelf() {
  try {
    const list = (await api('GET', '/api/comparisons?archived=1')).comparisons;
    if (S.route.name !== 'archived') return;
    S.shelf = list;
    renderShelf();
  } catch (e) { /* the list stays as it was */ }
}

function shelfRow(c) {
  const outcome = c.stage === 'closed' && c.outcome ? ` · ${c.outcome === 'bound' ? 'Bound' : 'Not bound'}` : '';
  return `<li class="shelf-row" data-id="${esc(c.id)}">
    <div class="who"><a class="nm" href="#/c/${esc(c.id)}">${esc(c.client_name)}</a>
      <div class="meta"><span class="sw st-${esc(c.stage)}"></span>${esc(stageLabel(c.stage))}${outcome} · ${plural(c.quotes, 'quote')} · archived ${esc(fmtDate(c.archived_at))}</div></div>
    <div class="acts"><button class="btn sm" data-act="unarchive" data-id="${esc(c.id)}">Unarchive</button><button class="btn sm quiet danger" data-act="delete" data-id="${esc(c.id)}">Delete…</button></div></li>`;
}

function renderShelf() {
  if (S.route.name !== 'archived' || !S.shelf) return;
  const fk = focusKey();
  const sel = fk === '#shelf-search' ? [$('#shelf-search').selectionStart, $('#shelf-search').selectionEnd] : null;
  const q = S.shelfFilter.trim().toLowerCase();
  const rows = S.shelf.filter(c => !q || c.client_name.toLowerCase().includes(q) || (c.carriers || []).some(x => x.toLowerCase().includes(q)));
  const body = !S.shelf.length
    ? '<div class="empty"><h2>No archived clients</h2><p>Archive a client from the ⋯ menu on its card. It leaves the board and keeps all its files.</p><a class="btn" href="#/">All clients</a></div>'
    : rows.length ? `<ul class="shelf">${rows.map(shelfRow).join('')}</ul>` : '<div class="empty-col">No matches</div>';
  $('#app').innerHTML = topLine(true) + `<header class="phead"><div>
      <h1>Archived clients</h1><div class="meta">${plural(S.shelf.length, 'client')} · off the board, with all their files kept</div></div>
    <div class="acts">${S.shelf.length ? `<input class="inp search" id="shelf-search" type="search" placeholder="Find a client or carrier" aria-label="Find an archived client or carrier" value="${esc(S.shelfFilter)}">` : ''}</div></header>${body}`;
  if (sel) { const box = $('#shelf-search'); box.focus(); box.setSelectionRange(sel[0], sel[1]); }
  else restoreFocus(fk);
}

document.addEventListener('dragstart', e => {
  const card = e.target.closest && e.target.closest('.cc');
  if (!card) return;
  closeMenu();
  S.dragging = card.dataset.id;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/x-card', card.dataset.id);
  requestAnimationFrame(() => card.classList.add('dragging'));
});
document.addEventListener('dragover', e => {
  const types = e.dataTransfer ? [...e.dataTransfer.types] : [];
  if (types.includes('Files')) {  // a missed file drop must never open the file in the browser
    e.preventDefault();
    const dz = e.target.closest && e.target.closest('.drop');
    if (dz) dz.classList.add('over');
    return;
  }
  const col = S.dragging && e.target.closest && e.target.closest('.col');
  if (!col) return;
  e.preventDefault();
  $$('.col.over').forEach(x => x !== col && x.classList.remove('over'));
  col.classList.add('over');
  const { cards, i } = dropIndex(col, e.clientY);
  dropLine = dropLine || Object.assign(document.createElement('div'), { className: 'drop-line' });
  const holder = $('.cards', col);
  const empty = $('.empty-col', holder);
  if (empty) empty.hidden = true;
  if (cards[i]) holder.insertBefore(dropLine, cards[i]); else holder.appendChild(dropLine);
});
document.addEventListener('dragleave', e => {
  const dz = e.target.closest && e.target.closest('.drop');
  if (dz && !dz.contains(e.relatedTarget)) dz.classList.remove('over');
});
document.addEventListener('drop', e => {
  e.preventDefault();
  const dz = e.target.closest && e.target.closest('.drop');
  if (dz && e.dataTransfer.files.length) { dz.classList.remove('over'); addFiles(e.dataTransfer.files); return; }
  const col = S.dragging && e.target.closest && e.target.closest('.col');
  if (!col) return;
  const { i } = dropIndex(col, e.clientY);
  const id = S.dragging;
  endDrag();
  moveCard(id, col.dataset.stage, i).catch(fail);
});
document.addEventListener('dragend', () => { if (S.dragging) { endDrag(); renderBoard(); } });
function endDrag() {
  S.dragging = null;
  if (dropLine) dropLine.remove();
  $$('.col.over').forEach(x => x.classList.remove('over'));
}

// ---------------------------------------------------------------- menu (one at a time, fixed position)

function openMenu(anchor, html) {
  closeMenu();
  const m = document.createElement('div');
  m.className = 'menu';
  m.id = 'menu';
  m.setAttribute('role', 'menu');
  m.innerHTML = html;
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.top = Math.max(8, Math.min(r.bottom + 6, innerHeight - m.offsetHeight - 8)) + 'px';
  m.style.left = Math.max(8, r.right - m.offsetWidth) + 'px';
  S.menuOpener = anchor;
  const f = m.querySelector('button:not([aria-current="true"])');
  if (f) f.focus();
}
function closeMenu(refocus) {
  const m = $('#menu');
  if (!m) return;
  m.remove();
  if (refocus && S.menuOpener && document.contains(S.menuOpener)) S.menuOpener.focus();
}
function cardMenu(anchor, id) {
  const c = S.board.find(x => x.id === id);
  if (!c) return;
  openMenu(anchor, `<button role="menuitem" data-act="open" data-id="${esc(id)}">Open</button><hr><div class="lbl">Move to</div>
    ${STAGES.map(s => `<button role="menuitem" data-act="move" data-id="${esc(id)}" data-stage="${s.key}" ${s.key === c.stage ? 'aria-current="true"' : ''}>${s.label}${s.key === c.stage ? '<span>current</span>' : ''}</button>`).join('')}
    <hr><button role="menuitem" data-act="archive" data-id="${esc(id)}">Archive</button><button role="menuitem" class="danger" data-act="delete" data-id="${esc(id)}">Delete…</button>`);
}

// ---------------------------------------------------------------- modal (one component for every dialog)

function openModal(m) {
  const opener = S.modal ? S.modal.opener : document.activeElement;
  S.modal = { ...m, opener };
  drawModal(true);
}

function drawModal(focus) {
  const m = S.modal;
  if (!m) return;
  const host = $('#host');
  const body = $('.mb', host);
  const scroll = body ? body.scrollTop : 0;
  const box = $('.modal', host);
  const html = modalHtml(m);
  if (box) box.innerHTML = html;  // same dialog, new content: no re-animation
  else host.innerHTML = `<div class="scrim" data-scrim><div class="modal${m.kind === 'progress' || m.kind === 'files' ? ' wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="mt">${html}</div></div>`;
  const nb = $('.mb', host);
  if (nb) nb.scrollTop = scroll;
  applyWidths(host);
  if (focus) {
    const f = $('[data-autofocus]', host) || $('input:not([type=hidden]):not([type=file]),textarea,select', host) || $('.btn.primary', host) || $('button', host);
    if (f) f.focus();
  }
}

function closeModal(force) {
  if (!S.modal || (S.modal.locked && !force)) return;
  const opener = S.modal.opener;
  S.modal = null;
  $('#host').innerHTML = '';
  if (opener && document.contains(opener)) opener.focus();
}

function trapFocus(e) {
  const modal = $('#host .modal');
  if (!modal) return;
  const f = $$('button,a[href],input:not([type=file]),textarea,select,summary,[tabindex="0"]', modal).filter(x => !x.disabled && x.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  else if (!modal.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
}

function stepsInd(n) {
  return `<div class="steps-ind">${[1, 2, 3].map(k => `<i class="${k <= n ? 'on' : ''}"></i>`).join('')}<span>${['Client', 'Quotes', 'Reading'][n - 1]}</span></div>`;
}

const X_BUTTON = '<button type="button" class="icon" aria-label="Close" data-act="close">×</button>';

function modalHtml(m) {
  switch (m.kind) {
    case 'new': return `<form id="new-form" novalidate><div class="mh"><div>${stepsInd(1)}<h2 id="mt">New client</h2><div class="s">Only the name is needed. Names here are hidden before a quote is read.</div></div>${X_BUTTON}</div>
      <div class="mb"><div class="fld"><label for="nc-name">Client name</label><input class="inp" id="nc-name" autocomplete="off" maxlength="200" required></div>
      <div class="fld"><label for="nc-addr">Address (optional)</label><input class="inp" id="nc-addr" autocomplete="off" maxlength="300"></div>
      <div class="fld"><label for="nc-others">Other people in the household (optional)</label><textarea class="inp" id="nc-others" rows="2" maxlength="2000" placeholder="One name per line"></textarea><span class="help">Used to hide personal details before a quote is read.</span></div>
      <p class="err" id="nc-err" hidden></p></div>
      <div class="mf"><button type="button" class="btn quiet" data-act="close">Cancel</button><button class="btn primary" type="submit">Continue</button></div></form>`;
    case 'drop': return `<div class="mh"><div>${m.fresh ? stepsInd(2) : ''}<h2 id="mt">${m.fresh ? `Quotes for ${esc(m.name)}` : 'Add quotes'}</h2><div class="s">PDFs or photos of quotes. A file with several quotes is split automatically.</div></div>${X_BUTTON}</div>
      <div class="mb"><label class="drop" id="drop" for="fi" tabindex="0" data-autofocus><b>Drop quote files here</b>or click to choose files</label>
        <input id="fi" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" hidden></div>
      ${m.fresh ? '<div class="mf"><button class="btn quiet" data-act="skip-drop">Skip for now</button></div>' : ''}`;
    case 'progress': return progressHtml(m);
    case 'files': return filesHtml();
    case 'delete': return `<div class="mh"><div><h2 id="mt">Delete ${esc(m.name)}?</h2><div class="s">This removes the quote files, the pages read from them, and your notes. It can't be undone.</div></div>${X_BUTTON}</div>
      <div class="mf"><button class="btn" data-act="close">Cancel</button><button class="btn danger-solid" data-act="delete-yes" data-id="${esc(m.id)}">Delete</button></div>`;
    case 'unchecked': return `<div class="mh"><div><h2 id="mt">${plural(m.n, 'value')} still ${m.n === 1 ? 'needs' : 'need'} a check</h2><div class="s">The PDF prints ${m.n === 1 ? 'it' : 'them'} like every other value, so the client can't tell ${m.n === 1 ? 'it was' : 'they were'} not checked.</div></div>${X_BUTTON}</div>
      <div class="mf"><button class="btn" data-act="download-anyway">Download anyway</button><button class="btn primary" data-act="check-first" data-autofocus>Check ${m.n === 1 ? 'it' : 'the first one'}</button></div>`;
    case 'client': return `<form id="client-form" novalidate><div class="mh"><div><h2 id="mt">Client on the proposal</h2><div class="s">Printed at the top of the PDF.</div></div>${X_BUTTON}</div>
      <div class="mb"><div class="fld"><label for="cl-name">Client name</label><input class="inp" id="cl-name" maxlength="200" value="${esc(S.comp.client.name)}"></div>
      <div class="fld"><label for="cl-addr">Address (optional)</label><input class="inp" id="cl-addr" maxlength="300" value="${esc(S.comp.client.address || '')}"></div>
      <p class="err" id="cl-err" hidden></p></div>
      <div class="mf"><button type="button" class="btn quiet" data-act="close">Cancel</button><button class="btn primary" type="submit">Save</button></div></form>`;
    case 'notice': return `<div class="mh"><div><h2 id="mt">Good to know</h2><div class="s">${esc(m.sub)}</div></div>${X_BUTTON}</div>
      <div class="mb"><p class="lead">${esc(m.text)}</p>${m.doc && m.doc !== m.text ? `<div><div class="eyebrow">As printed on the quote</div><div class="srcline">${esc(m.doc)}</div></div>` : ''}</div>
      <div class="mf"><button class="btn primary" data-act="close">Close</button></div>`;
    case 'notes-conflict': return `<div class="mh"><div><h2 id="mt">The note was changed in another window</h2></div></div>
      <div class="mb"><p>Keep the version you are typing here, or switch to the other one.</p><div class="srcline pre">${esc(m.current || '(empty)')}</div></div>
      <div class="mf"><button class="btn" data-act="notes-theirs">Use the other version</button><button class="btn primary" data-act="notes-mine">Keep mine</button></div>`;
    default: return '';
  }
}

// ---------------------------------------------------------------- new client + adding quotes

async function submitNew() {
  const name = $('#nc-name').value.trim();
  const err = $('#nc-err');
  if (!name) { err.textContent = "Enter the client's name."; err.hidden = false; $('#nc-name').focus(); return; }
  const others = $('#nc-others').value.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean).slice(0, 20);
  const btn = $('#new-form [type=submit]');
  btn.disabled = true;
  try {
    const v = await api('POST', '/api/comparisons', { client_name: name, address: $('#nc-addr').value.trim(), other_names: others });
    S.modal = { kind: 'drop', cid: v.id, name: v.client.name, fresh: true, opener: S.modal.opener };
    drawModal(true);
    if (S.route.name === 'board') refreshBoard();
  } catch (e) { err.textContent = e.message; err.hidden = false; btn.disabled = false; }
}

function openAdd() {
  if (batchRunning()) { openModal({ kind: 'progress', cid: S.batch.cid }); return; }
  openModal({ kind: 'drop', cid: S.comp.id, name: S.comp.client.name });
}

function addFiles(fileList) {
  const m = S.modal;
  if (!m || m.kind !== 'drop') return;
  startUpload(m.cid, m.name, fileList, m.fresh).catch(fail);
}

function batchRunning() { return !!S.batch && S.batch.files.some(b => b.status === 'uploading' || ACTIVE.has(b.status)); }
function batchDone(b) { return b.files.filter(f => !(f.status === 'uploading' || ACTIVE.has(f.status))).length; }
function batchPct(b) {
  const steps = f => f.status === 'uploading' ? 0 : ACTIVE.has(f.status) ? Math.max(0, STAGE_INDEX[f.status]) + 0.5 : STEPS.length;
  return b.files.length ? Math.round(b.files.reduce((a, f) => a + steps(f), 0) / (b.files.length * STEPS.length) * 100) : 0;
}

async function startUpload(cid, name, fileList, fresh) {
  const files = [...fileList];
  if (!files.length) return;
  if (batchRunning()) { toast('Wait for the files being read to finish, then add more.'); return; }
  S.batch = { cid, name, running: true, files: files.map(f => ({ name: f.name, file: f, status: 'uploading' })) };
  S.modal = { kind: 'progress', cid, fresh, opener: S.modal ? S.modal.opener : null };
  drawModal(true);
  if (S.route.name === 'comp' && S.route.id === cid) renderComp();  // "Reading" shows at once, not after the first poll
  for (const b of S.batch.files) {
    const fd = new FormData();
    fd.append('file', b.file, b.file.name);
    try {
      const r = await api('POST', compPath(cid, '/files'), undefined, fd);
      b.fid = r.file_id;
      b.status = 'queued';
      schedulePoll(true);
    } catch (e) { b.status = 'rejected'; b.error = e.message; }
    b.file = null;
    refreshProgressViews();
  }
  schedulePoll(true);
  refreshProgressViews();
  if (!batchRunning()) batchFinished();
}

function fileRow(b, j, kind) {
  const name = `<span>${esc(b.name)}</span>`;
  if (b.status === 'uploading') return `<li class="file"><div class="fn">${name}<span class="r">Uploading…</span></div></li>`;
  if (b.status === 'queued') return `<li class="file"><div class="fn">${name}<span class="r">Waiting</span></div></li>`;
  if (ACTIVE.has(b.status)) {
    const s = STAGE_INDEX[b.status];
    return `<li class="file"><div class="fn">${name}<span class="r"></span></div><ol>${STEPS.map((t, k) => `<li class="${k < s ? 'done' : k === s ? 'active' : ''}"><span class="ic"></span>${t}</li>`).join('')}</ol></li>`;
  }
  if (b.status === 'done') {
    return `<li class="file"><div class="fn">${name}<span class="r ok">${esc(b.summary || 'Added')}</span></div>${b.redactions ? `<span class="quiet-t">${plural(b.redactions, 'personal detail')} hidden before reading</span>` : ''}</li>`;
  }
  const label = b.status === 'rejected' ? 'Not added' : b.status === 'empty' ? 'No quote found' : "Couldn't read";
  const retry = ['failed', 'interrupted'].includes(b.status) && b.fid ? `<button class="btn sm" data-act="retry-file" data-fid="${esc(b.fid)}">Retry</button>` : '';
  const remove = b.fid ? `<button class="btn sm danger" data-act="remove-file" data-fid="${esc(b.fid)}" data-j="${j}" data-kind="${kind}">Remove</button>` : '';
  return `<li class="file"><div class="fn">${name}<span class="r bad">${label}</span></div><div class="fx"><span class="msg">${esc(b.error || '')}</span><span class="fxb">${retry}${remove}</span></div></li>`;
}

function progressHtml(m) {
  const b = S.batch;
  if (!b) return `<div class="mh"><div><h2 id="mt">Nothing is being added</h2></div>${X_BUTTON}</div>`;
  const running = batchRunning();
  const added = b.files.filter(f => f.status === 'done').length;
  const bad = b.files.filter(f => BAD_FILE.has(f.status) || f.status === 'rejected');
  let summary = '';
  if (!running) {
    const review = batchReviewCount();
    summary = `<div class="summary"><span><b>${plural(added, 'file')}</b> added.${bad.length ? ` ${plural(bad.length, 'file')} not added.` : ''}</span>${review ? `<span class="warn-t">${review === 1 ? '1 value needs' : `${review} values need`} a quick look.</span>` : ''}</div>`;
  }
  const title = running ? 'Reading quotes' : bad.length && !added ? 'No quotes added' : 'Quotes added';
  const sub = running ? `${batchDone(b)} of ${b.files.length} done. You can keep working while this runs.`
    : bad.length ? 'Some files need your attention.' : `Everything is in the comparison for ${esc(b.name)}.`;
  return `<div class="mh"><div>${m.fresh ? stepsInd(3) : ''}<h2 id="mt">${title}</h2><div class="s" aria-live="polite">${sub}</div></div>${running ? '' : X_BUTTON}</div>
    <div class="mb"><div class="pbig" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${batchPct(b)}"><i data-w="${batchPct(b)}"></i></div>
      <ul class="files">${b.files.map((f, j) => fileRow(f, j, 'batch')).join('')}</ul>${summary}</div>
    <div class="mf">${running ? '<button class="btn" data-act="keep-working" data-autofocus>Keep working</button>' : '<button class="btn primary" data-act="see-comp" data-autofocus>See comparison</button>'}</div>`;
}

function filesNeedingAttention() {
  return (S.comp ? S.comp.files.filter(f => BAD_FILE.has(f.status) || ACTIVE.has(f.status)) : [])
    .map(f => ({ name: f.name, fid: f.id, status: f.status, error: f.error, summary: f.summary, redactions: f.redactions }));
}

function filesHtml() {
  const fs = filesNeedingAttention();
  return `<div class="mh"><div><h2 id="mt">Files</h2><div class="s">${fs.length ? 'Retry a file, or remove it from this comparison.' : 'Every file has been read.'}</div></div>${X_BUTTON}</div>
    <div class="mb"><ul class="files">${fs.map((f, j) => fileRow(f, j, 'comp')).join('')}</ul></div>
    <div class="mf"><button class="btn primary" data-act="close">Close</button></div>`;
}

// Values from this batch's files that need a look. The batch's comparison may not be open (a new
// client is read from the board), so its view is fetched once the batch is done (S.batch.view).
function batchReviewCount() {
  const v = S.batch && (S.comp && S.comp.id === S.batch.cid ? S.comp : S.batch.view);
  if (!v) return 0;
  const ids = new Set();
  S.batch.files.forEach(b => { const f = v.files.find(x => x.id === b.fid); if (f) f.quote_ids.forEach(q => ids.add(q)); });
  let n = 0;
  v.tabs.forEach(t => t.rows.forEach(r => { if (r.section !== 'other') r.cells.forEach((c, i) => { if (c.state === 'review' && ids.has(t.columns[i].quote_id)) n++; }); }));
  return n;
}

let progressSig = '';
function refreshProgressViews() {
  const m = S.modal;
  if (m && (m.kind === 'progress' || m.kind === 'files')) {
    const sig = JSON.stringify(m.kind === 'progress' ? (S.batch ? S.batch.files.map(f => [f.status, f.summary, f.error]) : null)
      : filesNeedingAttention().map(f => [f.fid, f.status, f.error])) + (S.comp ? S.comp.revision : '');
    if (sig !== progressSig) {
      const wasRunning = m.kind === 'progress' && !$('#host [data-act="see-comp"]');
      progressSig = sig;
      drawModal(m.kind === 'progress' && wasRunning && !batchRunning());
    }
  }
  // the reading indicators outside the dialog
  const b = S.batch && batchRunning() ? S.batch : null;
  $$('.js-rtext').forEach(el => { if (b && el.dataset.cid === b.cid) el.textContent = `Reading ${Math.min(batchDone(b) + 1, b.files.length)} of ${b.files.length} files`; });
  $$('.js-rbar').forEach(el => { if (b && el.dataset.cid === b.cid) { el.dataset.w = batchPct(b); applyWidths(el.parentNode); } });
}

function batchFinished() {
  if (!S.batch || !S.batch.running) return;
  S.batch.running = false;
  if (S.route.name === 'comp' && S.route.id === S.batch.cid) renderComp();
  if (S.modal && S.modal.kind === 'progress') drawModal(true);
  else {
    const b = S.batch;
    const bad = b.files.filter(f => f.status !== 'done').length;
    toast(bad ? `${plural(bad, 'file')} for ${b.name} couldn't be added` : `Quotes added to ${b.name}`, {
      bad: !!bad,
      action: S.route.id === b.cid && S.route.name === 'comp' ? null : { label: 'Open', fn: () => { location.hash = `#/c/${b.cid}`; } },
    });
  }
  if (S.route.name === 'board') refreshBoard();
}

function goToComp(id) {
  if ((S.route.name === 'comp' || S.route.name === 'proposal') && S.route.id === id) {
    closeModal(true);
    if (S.route.name === 'proposal') location.hash = `#/c/${id}`;
    return;
  }
  location.hash = `#/c/${id}`;
}

// ---------------------------------------------------------------- polling (files being read)

function stopPoll() { clearTimeout(S.pollTimer); S.pollTimer = null; }

function pollTargets() {
  const ids = new Set();
  if (batchRunning() && S.batch.files.some(f => f.fid)) ids.add(S.batch.cid);
  if (S.comp && S.route.id === S.comp.id && S.comp.files.some(f => ACTIVE.has(f.status))) ids.add(S.comp.id);
  return ids;
}

function schedulePoll(soon) {
  if (!pollTargets().size) return;
  clearTimeout(S.pollTimer);
  S.pollTimer = setTimeout(() => { poll().catch(fail); }, soon ? 300 : 600);
}

async function poll() {
  S.pollTimer = null;
  for (const id of pollTargets()) {
    let p;
    try { p = await api('GET', compPath(id, '/progress')); }
    catch (e) {
      if (e.status === 404) {
        if (S.batch && S.batch.cid === id) S.batch = null;
        if (S.route.id === id) { toast('This client was deleted.', { bad: true }); location.hash = '#/'; return; }
      }
      continue;
    }
    if (S.batch && S.batch.cid === id) {
      const byId = Object.fromEntries(p.files.map(f => [f.id, f]));
      S.batch.files.forEach(b => {
        const f = b.fid && byId[b.fid];
        if (f) Object.assign(b, { status: f.status, summary: f.summary, error: f.error, redactions: f.redactions });
        else if (b.fid && b.status !== 'rejected') { b.status = 'rejected'; b.error = 'Removed.'; }
      });
    }
    if (S.batch && S.batch.cid === id && !batchRunning() && !(S.comp && S.comp.id === id) && S.batch.viewRev !== p.revision) {
      try { S.batch.view = await api('GET', compPath(id)); S.batch.viewRev = p.revision; } catch (e) { /* the summary waits */ }
    }
    if (S.comp && S.comp.id === id && S.route.id === id && p.revision !== S.comp.revision) {
      try { const v = await api('GET', compPath(id)); setComp(v); } catch (e) { /* the next poll tries again */ }
    }
  }
  refreshProgressViews();
  if (S.batch && S.batch.running && !batchRunning() && !S.batch.files.some(f => f.status === 'uploading')) batchFinished();
  schedulePoll();
}

// ---------------------------------------------------------------- the comparison (workspace)

async function showComparison(r, seq) {
  const app = $('#app');
  if (!S.comp || S.comp.id !== r.id) app.innerHTML = '<div class="boot">Loading…</div>';
  let v;
  try { v = await api('GET', compPath(r.id)); }
  catch (e) {
    if (seq !== routeSeq) return;
    app.innerHTML = e.status === 404
      ? '<div class="empty"><h2>This client no longer exists</h2><p>It may have been deleted.</p><a class="btn" href="#/">All clients</a></div>'
      : `<div class="empty"><h2>This client could not be loaded</h2><p>${esc(e.message)}</p><button class="btn" data-act="reload">Try again</button></div>`;
    return;
  }
  if (seq !== routeSeq) return;
  const fresh = !S.comp || S.comp.id !== v.id;
  S.comp = v;
  if (fresh) { S.showOther = false; N.dirty = false; N.base = v.notes || ''; }
  if (r.name === 'comp') {
    if (r.line && v.tabs.some(t => t.line === r.line)) S.lineBy[v.id] = r.line;
    renderComp();
    if (fresh) window.scrollTo(0, 0);
    if (S.pendingReview) { const pr = S.pendingReview; S.pendingReview = null; goReview(pr); }
  } else {
    S.pv = { id: null, rev: null };
    renderProposal();
    window.scrollTo(0, 0);
  }
  schedulePoll();
}

// Every update from the server lands here. Stale answers (lower revision) are ignored.
function setComp(v, opts = {}) {
  if (!v || !S.comp || v.id !== S.comp.id || v.revision < S.comp.revision) return;
  S.comp = v;
  if (S.route.id !== v.id) return;
  if (S.route.name === 'comp') {
    if (S.panel && !panelCtx()) closePanel(true);
    renderComp();
    if (S.panel && opts.panel) drawPanel(false);
  } else if (S.route.name === 'proposal') refreshProposal();
  refreshProgressViews();
  schedulePoll();
}

const curTab = () => {
  const c = S.comp;
  if (!c || !c.tabs.length) return null;
  if (!c.tabs.some(t => t.line === S.lineBy[c.id])) S.lineBy[c.id] = c.tabs[0].line;
  return c.tabs.find(t => t.line === S.lineBy[c.id]);
};

function reviews() {
  const out = [];
  (S.comp ? S.comp.tabs : []).forEach(t => t.rows.forEach(r => {
    if (!['price', 'core', 'also'].includes(r.section)) return;
    r.cells.forEach((c, i) => { if (c.state === 'review') out.push({ line: t.line, key: r.key, qid: t.columns[i].quote_id }); });
  }));
  return out;
}

// The line above every page: the logo on the left (it also goes to the board), "All clients" on the right.
function topLine(back) {
  const [href, label] = back === 'archived' ? ['#/archived', 'Archived clients'] : ['#/', 'All clients'];
  return `<div class="topline"><a class="brand" href="#/" aria-label="ANT Insurance, all clients"><img src="/static/logo.png" alt=""><span><b>ANT Insurance</b><span>Quote comparison</span></span></a>
    ${back ? `<a class="back" href="${href}"><span class="ar" aria-hidden="true">‹</span>${label}</a>` : ''}</div>`;
}

function clientHead(c, page) {
  const stageOpts = STAGES.map(s => `<option value="${s.key}" ${s.key === c.stage ? 'selected' : ''}>${s.label}</option>`).join('');
  const nq = Object.keys(c.quotes).length;
  const acts = page === 'comp'
    ? '<button class="btn primary" data-act="add">Add quotes</button>'
    : `<button class="btn" data-act="copy-mail">Copy email text</button><a class="btn primary" data-act="download" href="${esc(compPath(c.id, '/export.pdf'))}" download>Download PDF</a>`;
  return `<header class="phead"><div>
      <div class="titlerow"><h1>${esc(c.client.name)}</h1><select id="stage" aria-label="Status">${stageOpts}</select>${c.archived_at ? '<span class="pill">Archived</span><button class="linkbtn sm" data-act="unarchive-open">Unarchive</button>' : ''}</div><div class="meta">${c.client.address ? esc(c.client.address) + ' · ' : ''}${plural(nq, 'quote')} · started ${esc(fmtDate(c.created_at))}</div></div>
    <div class="acts"><nav class="seg" aria-label="Views"><a href="#/c/${esc(c.id)}"${page === 'comp' ? ' aria-current="page"' : ''}>Comparison</a><a href="#/c/${esc(c.id)}/proposal"${page === 'proposal' ? ' aria-current="page"' : ''}>Proposal</a></nav>${acts}</div></header>`;
}

function pickedTotal(c) {
  const picked = c.tabs.filter(t => t.recommended);
  if (c.recommended_total) return { text: `${c.recommended_total.estimated ? '≈ ' : ''}${c.recommended_total.amount}`, picked };
  if (picked.length === 1) {  // one pick: its own yearly price, when it is trusted
    const t = picked[0], i = t.columns.findIndex(x => x.quote_id === t.recommended), p = t.prices[i];
    const cell = t.rows.find(r => r.section === 'price').cells[i];
    if (p.unit === 'year' && cell.state !== 'review') return { text: `${p.estimated ? '≈ ' : ''}${p.amount}`, picked };
  }
  return { text: '—', picked };
}

function selectorHtml(c, line) {
  const rv = reviews();
  const items = c.tabs.map(t => {
    const n = rv.filter(r => r.line === t.line).length;
    const rec = t.recommended && t.columns.find(x => x.quote_id === t.recommended);
    return `<button class="lbtn" data-act="line" data-line="${esc(t.line)}" aria-current="${t.line === line}"><span class="b ${lineClass(t.line)}"></span>
      <span><span class="t">${esc(t.label)}</span><span class="s${n ? ' check' : ''}">${n ? `${plural(n, 'value')} to check` : rec ? `Pick: ${esc(rec.carrier)}` : 'No pick yet'}</span></span><span class="n">${t.columns.length}</span></button>`;
  }).join('');
  const tot = pickedTotal(c);
  const active = c.files.filter(f => ACTIVE.has(f.status));
  const failed = c.files.filter(f => BAD_FILE.has(f.status));
  const b = batchFor(c.id);
  const reading = active.length || b
    ? `<button class="reading" data-act="show-progress"><span class="js-rtext" data-cid="${esc(c.id)}">${b ? `Reading ${Math.min(batchDone(b) + 1, b.files.length)} of ${b.files.length} files` : `Reading ${plural(active.length, 'file')}`}</span><span class="bar"><i class="js-rbar" data-cid="${esc(c.id)}" data-w="${b ? batchPct(b) : 50}"></i></span></button>` : '';
  return `<nav class="selector" aria-label="Policies"><div class="eyebrow">Policies</div>${items || '<p class="faint small">Nothing read yet.</p>'}
    ${c.tabs.length ? `<div class="hh"><div class="eyebrow">Picks together</div><div class="v">${esc(tot.text)} ${tot.text !== '—' ? '<small>/ year</small>' : ''}</div><div class="s">${tot.picked.length} of ${plural(c.tabs.length, 'policy', 'policies')} picked${c.recommended_total && c.recommended_total.left_out.length ? `. Leaves out ${esc(joinWords(c.recommended_total.left_out.map(lower)))} (priced monthly)` : ''}</div></div>` : ''}
    ${rv.length ? `<button class="btn sm next-all" data-act="next-review">Check ${plural(rv.length, 'value')} <span aria-hidden="true">›</span></button>` : ''}
    ${reading}
    ${failed.length ? `<button class="filebad" data-act="show-files">${failed.length === 1 ? "1 file couldn't be read" : `${failed.length} files couldn't be read`}</button>` : ''}
  </nav>`;
}

function priceBits(t, i) {
  const p = t.prices[i], cell = t.rows.find(r => r.section === 'price').cells[i];
  const bits = [];
  if (p.vs_lowest) bits.push(esc(p.vs_lowest));
  if (p.six_month && p.printed) bits.push(`${esc(p.printed)} per 6 months`);
  else if (!p.unit && p.printed && t.line !== 'life') bits.push('term not clear on the quote');
  let sig = '';
  if (cell.state === 'review') sig = `<span class="pill check"><span class="dot"></span>${p.lowest_if ? esc(p.lowest_if) : 'Price needs a look'}</span>`;
  else if (p.below_umbrella) sig = '<span class="pill bad">Below umbrella requirement</span>';
  else if (p.lowest) sig = '<span class="pill good">Lowest price</span>';
  return { bits, sig };
}

function bigPrice(p, cell) {
  if (!p.amount) return '<div class="big none">—<small> no price found</small></div>';
  const unit = p.unit === 'year' ? ' / year' : p.unit === 'month' ? ' / month' : '';
  return `<div class="big${cell.state === 'review' ? ' rv' : ''}"><span class="amt">${p.estimated ? '≈ ' : ''}${esc(p.amount)}</span>${unit ? `<small>${unit}</small>` : ''}${cell.edited ? '<span class="edited">edited</span>' : ''}</div>`;
}

function tableHtml(t) {
  const cols = t.columns, rec = t.recommended, cur = t.current;
  const rc = i => (cols[i].quote_id === rec ? ' isrec' : '');
  const pctx = S.panel && S.panel.line === t.line ? S.panel : null;
  const isSel = (key, i) => pctx && pctx.type === 'cell' && pctx.key === key && pctx.qid === cols[i].quote_id;
  const ri = key => t.rows.findIndex(r => r.key === key);
  const priceRow = t.rows.find(r => r.section === 'price');
  const pr = ri(priceRow.key);
  const units = new Set(t.prices.map(p => p.unit).filter(Boolean));
  const priceLabel = t.line === 'life' ? 'Premium' : units.has('month') ? 'Price' : 'Price per year';

  const head = `<tr><th class="lab corner" scope="col"><span class="c1">${plural(cols.length, 'quote')}</span><span class="c2">${esc(t.label)}</span></th>${cols.map((c, i) => {
    const p = t.prices[i], on = c.quote_id === rec;
    const term = c.term_tag === '6-mo' ? '<span class="pill">6-month term</span>' : c.term_tag === 'term?' && t.line !== 'life' ? '<span class="pill check">Term unclear</span>' : '';
    const mini = p.amount ? `${p.estimated ? '≈ ' : ''}${p.amount}${p.unit === 'year' ? '/yr' : p.unit === 'month' ? '/mo' : ''}` : '';
    return `<th class="${rc(i).trim()}" scope="col"><div class="ctop${isSel('price', i) ? ' sel' : ''}"><div class="qh1"><span class="car" title="${esc(c.carrier || 'Unknown carrier')}">${esc(c.carrier || 'Unknown carrier')}</span><button class="icon" data-act="quote" data-c="${i}" aria-label="${esc(c.carrier || 'Unknown carrier')} quote details">⋯</button></div>
      <div class="qh2"><button class="recbtn" data-act="rec" data-c="${i}" aria-pressed="${on}">${on ? 'Recommended' : 'Recommend'}</button>${term}${c.quote_id === cur ? '<span class="pill cur">Current policy</span>' : ''}<span class="mp" aria-hidden="true">${esc(mini)}</span></div></div></th>`;
  }).join('')}</tr>`;

  const deck = `<tr class="deck"><th class="lab" scope="row">${priceLabel}</th>${cols.map((c, i) => {
    const p = t.prices[i], cell = priceRow.cells[i];
    const { bits, sig } = priceBits(t, i);
    return `<td class="${rc(i).trim()}"><div class="cbot${isSel('price', i) ? ' sel' : ''}${cell.state === 'review' ? ' review' : ''}" data-act="cell" data-r="${pr}" data-c="${i}" tabindex="0" role="button" aria-label="${esc(c.carrier)} price${cell.state === 'review' ? ', needs a check' : ''}">
      ${bigPrice(p, cell)}<div class="meta2">${bits.join(' · ')}</div>
      ${p.bar != null ? `<span class="pbar" aria-hidden="true"><i data-w="${p.bar}"></i></span>` : ''}
      ${sig ? `<div class="sigl">${sig}</div>` : ''}
      ${p.excludes.length ? `<div class="gx">Excludes ${esc(joinWords(p.excludes))}</div>` : ''}</div></td>`;
  }).join('')}</tr>`;

  const valueCell = (r, i) => {
    const c = r.cells[i], k = ri(r.key);
    const cls = `v${rc(i)}${isSel(r.key, i) ? ' sel' : ''}${c.best ? ' best' : ''}${c.state === 'review' ? ' review' : ''}`;
    const attrs = `data-act="cell" data-r="${k}" data-c="${i}" tabindex="0"`;
    if (c.state === 'not_listed') return `<td class="${cls}" ${attrs}><span class="nl">Not listed</span></td>`;
    let h = c.lines.map((l, j) => (c.excluded || [])[j] ? `<span class="val"><span class="ex">${esc(l)}</span></span>`
      : c.state === 'review' ? `<span class="val"><span class="ck">${esc(l)}</span></span>` : `<span class="val">${esc(l)}</span>`).join('');
    if (c.bar != null) h += `<span class="lbar" aria-hidden="true"><i data-w="${c.bar}"></i></span>`;
    if (c.note) h += `<span class="sub">${esc(c.note)}</span>`;
    if (c.vs_current) h += `<span class="vs ${c.vs_current.good === true ? 'up' : c.vs_current.good === false ? 'down' : 'faint'}">${c.vs_current.up === true ? '▲ ' : c.vs_current.up === false ? '▼ ' : ''}${esc(c.vs_current.text)}</span>`;
    h += c.edited ? '<span class="edited">edited</span>' : '<i class="mk" aria-hidden="true"></i>';
    return `<td class="${cls}" ${attrs}>${h}</td>`;
  };
  const same = r => r.cells.every(c => c.state !== 'not_listed') && new Set(r.cells.map(c => norm(c.lines.join('|')))).size === 1;
  const grp = title => `<tr class="grp"><td class="lab">${esc(title)}</td>${cols.map((c, i) => `<td class="${rc(i).trim()}"></td>`).join('')}</tr>`;
  const row = (r, extra) => `<tr><th class="lab" scope="row">${esc(r.label)}${extra || ''}</th>${cols.map((c, i) => valueCell(r, i)).join('')}</tr>`;

  let body = '';
  const veh = t.rows.find(r => r.section === 'vehicles');
  if (veh) body += `<tr><th class="lab" scope="row">${esc(veh.label)}</th>${veh.cells.map((c, i) => `<td class="${rc(i).trim()}">${c.lines.map(l => `<span class="val">${esc(l)}</span>`).join('') || '<span class="nl">—</span>'}</td>`).join('')}</tr>`;
  [['coverage', 'Coverage'], ['deductibles', 'Deductibles'], ['also', 'Also compare']].forEach(([g, title]) => {
    let rs = t.rows.filter(r => r.group === g);
    if (S.rowMode === 'diff') rs = rs.filter(r => !same(r));
    if (!rs.length) return;
    body += grp(title);
    rs.forEach(r => { body += row(r, r.kept ? `<button class="keep" data-act="unkeep" data-r="${ri(r.key)}">Stop keeping</button>` : ''); });
  });
  const other = t.rows.filter(r => r.group === 'other');
  if (other.length && S.showOther) {
    body += grp('Other coverages on these quotes');
    other.forEach(r => { body += row(r, `<button class="keep" data-act="keep" data-r="${ri(r.key)}">Keep in table</button>`); });
  }
  const gtk = t.rows.find(r => r.section === 'good_to_know');
  if (gtk && gtk.cells.some(c => c.notices.length)) {
    const g = ri(gtk.key);
    body += grp('Good to know');
    body += `<tr><th class="lab" scope="row"><span class="sr">Good to know</span></th>${gtk.cells.map((c, i) => `<td class="gtk${rc(i)}">${c.notices.map((n, k) =>
      `<button class="gtkn ${WARN_NOTICES.has(n.key) ? 'w' : BAD_NOTICES.has(n.key) ? 'b' : 'n'}" data-act="gtk" data-r="${g}" data-c="${i}" data-k="${k}">${esc(n.text)}</button>`).join('') || '<span class="nl">Nothing to note</span>'}</td>`).join('')}</tr>`;
  }
  if (!body && S.rowMode === 'diff') body = `<tr><td class="lab"></td><td class="nodiff" colspan="${cols.length}">Every row is the same on these quotes.</td></tr>`;
  let below = '';
  if (other.length) {
    const hidden = t.hidden_review && !S.showOther ? ` <span class="pill check"><span class="dot"></span>${plural(t.hidden_review, 'value')} to check</span>` : '';
    below = `<div class="below"><button class="linkbtn" data-act="toggle-other" aria-expanded="${S.showOther}">${S.showOther ? 'Hide other coverages' : `Show ${plural(other.length, 'other coverage')} on these quotes`}</button>${hidden}</div>`;
  }
  return `<section class="tcard"><div class="tscroll"><table class="cmp" data-cols="${cols.length}"><colgroup><col class="labc">${cols.map(() => '<col>').join('')}</colgroup>
    <thead>${head}</thead><tbody>${deck}${body}</tbody></table></div>${below}</section>`;
}

function renderComp() {
  const c = S.comp;
  if (!c || S.route.name !== 'comp') return;
  const t = curTab();
  const line = t ? t.line : null;
  if (line) setHashLine(line);
  document.title = `${c.client.name} · Quote comparison`;
  const fk = focusKey();
  const old = $('.tscroll');
  const keepScroll = old && S.scrollFor === `${c.id}:${line}`;
  const sl = keepScroll ? old.scrollLeft : 0;
  S.scrollFor = `${c.id}:${line}`;
  let main;
  if (!t) {
    const busy = c.files.some(f => ACTIVE.has(f.status)) || batchFor(c.id);
    main = busy
      ? '<div class="empty"><h2>Reading the quotes</h2><p>Each quote shows up here as soon as it has been read and checked.</p><button class="btn" data-act="show-progress">Show progress</button></div>'
      : `<div class="empty"><h2>No quotes yet</h2><p>Add the quote PDFs you received for ${esc(c.client.name)}. Each file is read, checked, and added under its policy.</p><button class="btn primary" data-act="add">Add quotes</button></div>`;
  } else {
    const curSel = t.columns.length > 1
      ? `<label for="cur">Compare with</label><select class="sel" id="cur"><option value="">Nothing</option>${t.columns.map(q => `<option value="${esc(q.quote_id)}" ${t.current === q.quote_id ? 'selected' : ''}>${esc(q.carrier || 'Unknown carrier')} (current policy)</option>`).join('')}</select>` : '';
    main = `<div class="lhead"><h2>${esc(t.label)}</h2>
      <div class="ctrl"><div class="seg" role="group" aria-label="Rows"><button data-act="rows" data-v="all" aria-pressed="${S.rowMode === 'all'}">All rows</button><button data-act="rows" data-v="diff" aria-pressed="${S.rowMode === 'diff'}">Only differences</button></div>${curSel}</div></div>
      ${tableHtml(t)}<p class="ws-foot">Click any value to see it on the quote page or change it.</p>`;
  }
  $('#app').innerHTML = topLine(c.archived_at ? 'archived' : true) + clientHead(c, 'comp') + `<div class="ws">${selectorHtml(c, line)}<div class="main">${main}</div></div>`;
  applyWidths($('#app'));
  const table = $('table.cmp');
  if (table) table.style.minWidth = (180 + (+table.dataset.cols) * 170) + 'px';
  fitTable();
  const ns = $('.tscroll');
  if (ns) ns.scrollLeft = sl;
  restoreFocus(fk);
}

// ---------------------------------------------------------------- side panel (a value, or a quote)

function panelCtx() {
  const p = S.panel, c = S.comp;
  if (!p || !c) return null;
  const t = c.tabs.find(x => x.line === p.line);
  if (!t) return null;
  const ci = t.columns.findIndex(x => x.quote_id === p.qid);
  if (ci < 0) return null;
  const q = c.quotes[p.qid];
  if (!q) return null;
  if (p.type === 'quote') return { t, ci, q, col: t.columns[ci] };
  const ri = t.rows.findIndex(r => r.key === p.key);
  if (ri < 0) return null;
  return { t, ci, ri, q, col: t.columns[ci], row: t.rows[ri], cell: t.rows[ri].cells[ci], isPrice: t.rows[ri].section === 'price' };
}

function openPanel(p) {
  S.panel = p;
  document.body.classList.add('panel-open');
  document.body.classList.toggle('panel-wide', S.wide);
  drawPanel(true);
  renderComp();
  const el = $('.cmp .sel');
  if (el) el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function closePanel(quiet) {
  if (!S.panel) return;
  const back = S.panel.opener;
  S.panel = null;
  document.body.classList.remove('panel-open', 'panel-wide');
  $('#panel').innerHTML = '';
  if (!quiet) renderComp();
  if (!quiet && back) { const el = $(back, $('#app')); if (el) el.focus({ preventScroll: true }); }
}

function drawPanel(focus) {
  const ctx = panelCtx();
  if (!ctx) { closePanel(true); return; }
  const pb = $('#panel .pb');
  const scroll = pb ? pb.scrollTop : 0;
  const typed = focus ? null : $$('#panel input.inp, #panel textarea.inp').map(el => [el.id, el.value]);
  $('#panel').innerHTML = S.panel.type === 'quote' ? quotePanelHtml(ctx) : cellPanelHtml(ctx);
  const nb = $('#panel .pb');
  if (nb) nb.scrollTop = scroll;
  if (typed) typed.forEach(([id, value]) => { const el = id && document.getElementById(id); if (el) el.value = value; });
  if (focus) {
    const f = $('#panel [data-autofocus]') || $('#panel input.inp') || $('#panel .pf .btn.primary');
    if (f) f.focus({ preventScroll: true });
  }
}

function highlight(source, value) {
  const s = source || '', v = String(value || '').split(': ').pop().trim();
  const i = v ? s.toLowerCase().indexOf(v.toLowerCase()) : -1;
  if (i < 0) return esc(s);
  return esc(s.slice(0, i)) + '<mark>' + esc(s.slice(i, i + v.length)) + '</mark>' + esc(s.slice(i + v.length));
}

function pageView(q, page) {
  const pages = (q.pages && q.pages.length ? q.pages : [q.first_page || 1]).slice();
  const cur = S.panel.page || page || pages[0];
  if (!pages.includes(cur)) { pages.push(cur); pages.sort((a, b) => a - b); }
  const src = compPath(S.comp.id, `/pages/${encodeURIComponent(q.file_id)}/${cur}`);
  const isPdf = (q.file_ext || '.pdf') === '.pdf';
  const orig = compPath(S.comp.id, `/files/${encodeURIComponent(q.file_id)}/original`) + (isPdf ? `#page=${cur}` : '');
  const tabs = pages.length > 1 ? pages.map(n => `<button type="button" data-act="page" data-p="${n}" aria-pressed="${n === cur}">Page ${n}</button>`).join('') : `<span class="pgl">Page ${cur}</span>`;
  return `<div class="pages"><div class="ptabs">${tabs}<button type="button" class="zoom" data-act="zoom" aria-pressed="${S.wide}">Larger</button><a href="${esc(orig)}" target="_blank" rel="noopener">Open the ${isPdf ? 'PDF' : 'file'}</a></div>
    <img class="pgimg" src="${esc(src)}" alt="Page ${cur} of the ${esc(q.carrier || '')} quote"></div>`;
}

function itemTitle(it, row) {
  if (it.vehicle) return it.vehicle;
  if (row.key === 'special_limits' || row.key.startsWith('other')) return it.label || row.label;
  return null;
}

function cellPanelHtml(ctx) {
  const { t, row, cell, q, col, isPrice } = ctx;
  const lineWord = t.line === 'unknown' ? '' : ` ${lower(t.label)}`;
  const head = `<div class="ph"><div><h3 id="panel-title">${esc(isPrice ? 'Price' : row.label)}</h3><div class="s">${esc(col.carrier || 'Unknown carrier')}${esc(lineWord)} quote</div></div><button class="icon" data-act="close-panel" aria-label="Close">×</button></div>`;
  const others = reviews().filter(r => !(r.qid === col.quote_id && r.key === row.key)).length;
  const next = others ? `<button class="next" data-act="next-review">Next to check (${others}) ›</button>` : '';
  if (!cell.items.length) {
    const removed = (q.removed_items || []).filter(r => r.key === row.key || ('other:' + (r.label || '').trim().toLowerCase()) === row.key);
    S.panel.removed = removed;
    return head + `<div class="pb">
      ${removed.map((r, k) => `<div class="note-box">You removed ${esc(r.value || 'a value')}${r.vehicle ? ` (${esc(r.vehicle)})` : ''} from this quote. <button class="linkbtn inline" data-act="undo-removed" data-k="${k}">Undo</button></div>`).join('')}
      <p class="muted np">Not listed on this quote. If it's there, type it as printed.</p>
      <div class="fld"><label for="pv-new">Value</label><input class="inp" id="pv-new" maxlength="500" placeholder="As printed on the quote"></div>
      ${row.help ? `<p class="helpline">${esc(row.help)}</p>` : ''}
      ${pageView(q, null)}</div>
      <div class="pf">${next}<span class="sp"></span><button class="btn lg" data-act="close-panel">Cancel</button><button class="btn primary lg" data-act="save-cell">Save</button></div>`;
  }
  const review = cell.state === 'review';
  const multi = cell.items.length > 1;
  let top = '';
  if (!multi && cell.why.length) top += `<div class="why" tabindex="-1" data-autofocus><b>Why check this:</b> ${cell.why.map(esc).join(' ')}</div>`;
  if (isPrice && review && cell.sum_check) {
    const sc = cell.sum_check;
    top += `<div class="picks"><button class="pick" data-act="pick" data-v="${esc(sc.printed)}"><b>${esc(sc.printed)}</b><small>Use the total as printed</small></button><button class="pick" data-act="pick" data-v="${esc(sc.parts)}"><b>${esc(sc.parts)}</b><small>Use the parts added up</small></button></div>`;
  }
  // a flagged value opens with focus on why, not in the field: Enter in the field means "Looks right",
  // so the broker has to reach the field (or the button) on purpose before approving anything
  const firstWhy = multi ? cell.items.findIndex(it => it.why.length) : -1;
  const items = cell.items.map((it, k) => {
    const title = multi ? itemTitle(it, row) || `Value ${k + 1}` : null;
    const why = multi && it.why.length ? `<div class="why"${k === firstWhy ? ' tabindex="-1" data-autofocus' : ''}>${it.why.map(esc).join(' ')}</div>` : '';
    const orig = it.edited ? `<div class="orig">${it.added ? 'Typed in by you.' : `Originally ${esc(it.original_value || '(empty)')}.`} <button class="linkbtn inline" data-act="undo-item" data-k="${k}">Undo my change</button></div>` : '';
    const extra = isPrice
      ? `<dl class="kv"><dt>Price used</dt><dd>${esc(q.price_basis || '—')}</dd>${q.price_basis_key === 'monthly' ? '<dt>Term</dt><dd>Monthly premium as printed. It is not turned into a yearly price.</dd>' : ''}${q.line === 'life' ? '' : termChoice(q)}</dl>`
      : it.premium ? `<dl class="kv"><dt>Premium</dt><dd>${esc(it.premium)}</dd></dl>` : '';
    const source = it.source ? `<div><div class="eyebrow">${it.on_page === false ? 'What the reader copied (not found on the quote)' : 'As printed on the quote'}${it.page ? ` · page ${it.page}` : ''}</div><div class="srcline">${highlight(it.source, it.value)}</div></div>` : '';
    const remove = !isPrice ? `<button class="linkbtn danger inline" data-act="remove-item" data-k="${k}">Remove this value</button>` : '';
    return `<div class="item">${title ? `<h4>${esc(title)}</h4>` : ''}${why}
      <div class="fld"><label for="pv-${k}">${multi ? 'Value' : isPrice ? 'Price as printed' : 'Value'}</label><input class="inp" id="pv-${k}" maxlength="500" value="${esc(it.value == null ? '' : it.value)}"></div>
      ${orig}${extra}${source}${remove ? `<div>${remove}</div>` : ''}</div>`;
  }).join('');
  const firstPage = (cell.items.find(it => it.page) || {}).page || null;
  return head + `<div class="pb">${top}${items}${row.help ? `<p class="helpline">${esc(row.help)}</p>` : ''}${pageView(q, firstPage)}</div>
    <div class="pf">${next}<span class="sp"></span>${review
      ? '<button class="btn lg" data-act="save-cell">Save</button><button class="btn primary lg" data-act="accept-cell">Looks right <kbd>↵</kbd></button>'
      : '<button class="btn lg" data-act="close-panel">Cancel</button><button class="btn primary lg" data-act="save-cell">Save</button>'}</div>`;
}

// the policy term decides the yearly figure (a 6-month price counts twice), so the broker can set it
function termChoice(q) {
  const b = n => `<button class="btn sm" data-act="set-term" data-v="${n}" aria-pressed="${q.term_months === n}">${n} months</button>`;
  const note = q.term_months === 6 ? 'The yearly figure is estimated as twice this price.' : q.term_months === 12 ? '' : 'Not clear on the quote. Pick the term printed on it.';
  return `<dt>Policy term</dt><dd><div class="termpick">${b(6)}${b(12)}</div>${note ? `<div class="faint">${note}</div>` : ''}${q.term_edited ? `<div class="orig">Set by you. <button class="linkbtn inline" data-act="undo-term">Undo</button></div>` : ''}</dd>`;
}

function quotePanelHtml(ctx) {
  const { t, q, col } = ctx;
  const p = S.panel;
  const f = S.comp.files.find(x => x.id === q.file_id) || {};
  const title = `${esc(q.carrier || 'Unknown carrier')} ${q.line === 'unknown' ? '' : esc(lower(q.line_label)) + ' '}quote`;
  const head = `<div class="ph"><div><h3 id="panel-title">${title}</h3><div class="s">${esc(q.file_name || f.name || '')}${q.pages_text ? `, ${esc(q.pages_text)}` : ''}</div></div><button class="icon" data-act="close-panel" aria-label="Close">×</button></div>`;
  if (p.mode === 'remove') {
    const others = (f.quote_ids || []).length - 1;
    return head + `<div class="pb"><p class="lead">Remove this quote from the comparison?</p><p class="muted np">${others > 0 ? `The other ${plural(others, 'quote')} from the same file stay.` : 'The file is removed too.'} This can't be undone.</p></div>
      <div class="pf"><span class="sp"></span><button class="btn lg" data-act="quote-back">Keep it</button><button class="btn danger-solid lg" data-act="quote-remove-yes" data-autofocus>Remove</button></div>`;
  }
  if (p.mode === 'move') {
    const label = (S.comp.lines.find(l => l.key === p.newLine) || {}).label || p.newLine;
    return head + `<div class="pb"><p class="lead">Move this quote to ${esc(label)}?</p><p class="muted np">The file is read again with this correction. It takes about ten seconds, and the rest of the comparison stays as it is.</p></div>
      <div class="pf"><span class="sp"></span><button class="btn lg" data-act="quote-back">Cancel</button><button class="btn primary lg" data-act="quote-move-yes" data-autofocus>Move to ${esc(label)}</button></div>`;
  }
  const opts = S.comp.lines.map(l => `<option value="${esc(l.key)}" ${l.key === q.line ? 'selected' : ''}>${esc(l.label)}</option>`).join('');
  const isCur = t.current === col.quote_id;
  const fine = q.other_notices.length ? `<details><summary>Other notes on the quote (${q.other_notices.length})</summary><ul class="fine">${q.other_notices.map(x => `<li>${esc(x)}</li>`).join('')}</ul></details>` : '';
  return head + `<div class="pb"><dl class="kv">
      <dt><label for="mv">This quote is for</label></dt><dd><select class="sel" id="mv">${opts}</select></dd>
      <dt>${q.line === 'life' ? 'Term length' : 'Policy term'}</dt><dd>${q.term_label ? esc(q.term_label) : q.line === 'life' ? 'Not stated' : 'Not clear on the quote'}${q.term_months === 6 ? ' <span class="faint">(the yearly price is estimated as twice this)</span>' : ''}</dd>
      <dt>Price used</dt><dd>${esc(q.price || '—')}${q.price_basis ? ` <span class="faint">· ${esc(q.price_basis.toLowerCase())}</span>` : ''}</dd>
      ${q.policy_period ? `<dt>Policy period</dt><dd>${esc(q.policy_period)}</dd>` : ''}
      ${q.expires ? `<dt>Quote expires</dt><dd>${esc(q.expires)}</dd>` : ''}
      ${q.discounts ? `<dt>Discounts</dt><dd>${esc(q.discounts)}</dd>` : ''}
      <dt>Read from it</dt><dd>${plural(q.values, 'value')}</dd>
      <dt>Added</dt><dd>${esc(fmtDate(q.added_at))}${q.redactions ? ` <span class="faint">· ${plural(q.redactions, 'personal detail')} hidden before reading</span>` : ''}</dd></dl>
      ${fine}
      <div><button class="btn sm" data-act="set-current" aria-pressed="${isCur}">${isCur ? 'Not the current policy' : 'This is the current policy'}</button></div>
      ${pageView(q, q.first_page)}</div>
    <div class="pf"><button class="btn danger-solid" data-act="quote-remove">Remove from comparison</button><span class="sp"></span><button class="btn primary lg" data-act="close-panel">Done</button></div>`;
}

function target(ctx, it) {
  if (ctx.isPrice) return { quote_id: ctx.col.quote_id, key: 'price', index: null, label: null, vehicle: null };
  return { quote_id: ctx.col.quote_id, key: it.key, index: it.index, label: it.label, vehicle: it.vehicle };
}
// the value the broker approved with "Looks right": the server ignores the approval if a re-read changes it
const seen = it => (it.value != null && String(it.value).length <= 500 ? String(it.value) : null);
const cellPatch = body => write('PATCH', compPath(S.comp.id, '/cells'), body);

function goReview(r) {
  if (!r || !S.comp) return;
  if (S.lineBy[S.comp.id] !== r.line) { S.lineBy[S.comp.id] = r.line; S.showOther = false; }
  const t = S.comp.tabs.find(x => x.line === r.line);
  const col = t ? t.columns.findIndex(x => x.quote_id === r.qid) : -1;
  const row = t ? t.rows.findIndex(x => x.key === r.key) : -1;
  openPanel({ type: 'cell', line: r.line, qid: r.qid, key: r.key, opener: `[data-act="cell"][data-r="${row}"][data-c="${col}"]` });
}

function afterCheck(msg) {
  const rv = reviews();
  if (rv.length) { goReview(rv[0]); toast(msg); }
  else { closePanel(); toast(msg === 'Saved' ? 'Saved' : 'Everything is checked'); }
}

async function saveCell(btn, forced) {
  const ctx = panelCtx();
  if (!ctx) return;
  const wasReview = ctx.cell.state === 'review';
  if (btn) btn.disabled = true;
  try {
    let v = null;
    if (!ctx.cell.items.length) {
      const value = $('#pv-new').value.trim();
      if (!value) { closePanel(); return; }
      v = await cellPatch({ quote_id: ctx.col.quote_id, key: ctx.row.key, index: null, label: ctx.row.label, vehicle: null, action: 'edit', value });
    } else {
      for (let k = 0; k < ctx.cell.items.length; k++) {
        const it = ctx.cell.items[k];
        const input = $(`#pv-${k}`);
        const value = forced != null && k === 0 ? forced : (input ? input.value.trim() : (it.value || ''));
        if (value === (it.value || '').trim()) {
          // unchanged: only "Looks right", or picking this exact total, approves a flagged value
          if (forced != null && k === 0 && it.state === 'review') v = await cellPatch({ ...target(ctx, it), action: 'accept', value: seen(it) });
          continue;
        }
        if (!value && !ctx.isPrice) v = await cellPatch({ ...target(ctx, it), action: 'clear' });
        else v = await cellPatch({ ...target(ctx, it), action: 'edit', value });
      }
    }
    if (!v) {  // nothing was changed
      if (btn && document.contains(btn)) btn.disabled = false;
      if (wasReview) toast('Nothing changed. If the value matches the page, press Looks right.');
      else closePanel();
      return;
    }
    setComp(v);
    if (wasReview) afterCheck('Saved');
    else { closePanel(); toast('Saved'); }
  } catch (e) { if (btn && document.contains(btn)) btn.disabled = false; fail(e); }
}

async function cellAction(action, k) {
  const ctx = panelCtx();
  if (!ctx) return;
  let v = null;
  if (action === 'accept') {
    for (const it of ctx.cell.items) if (it.state === 'review') v = await cellPatch({ ...target(ctx, it), action: 'accept', value: seen(it) });
    if (v) setComp(v);
    afterCheck('Marked as checked');
    return;
  }
  if (action === 'remove') v = await cellPatch({ ...target(ctx, ctx.cell.items[k]), action: 'clear' });
  else if (action === 'undo') v = await cellPatch({ ...target(ctx, ctx.cell.items[k]), action: 'undo' });
  else if (action === 'undo-removed') {
    const r = S.panel.removed[k];
    v = await cellPatch({ quote_id: ctx.col.quote_id, key: r.key, index: null, label: r.label, vehicle: r.vehicle, action: 'undo' });
  }
  if (v) setComp(v, { panel: true });
  toast({ remove: 'Value removed', undo: 'Change undone', 'undo-removed': 'Value restored' }[action]);
}

function openNotice(ri, ci, k) {
  const t = curTab(), col = t.columns[ci];
  const n = t.rows[ri].cells[ci].notices[k];
  openModal({ kind: 'notice', text: n.text, doc: n.document_text, sub: `${col.carrier || 'Unknown carrier'}${t.line === 'unknown' ? '' : ' ' + lower(t.label)} quote` });
}

// ---------------------------------------------------------------- table fit + sticky header

// Fits the width: the table scrolls with the page. Too wide (many quotes, or the side panel open):
// it gets its own sideways scroll box, and its header then no longer sticks.
function fitTable() {
  const sc = $('.tscroll');
  if (!sc) return;
  sc.classList.remove('scrollx');
  if (sc.scrollWidth > sc.clientWidth + 1) sc.classList.add('scrollx');
  updateStuck();
}
window.addEventListener('resize', () => { fitTable(); });
document.addEventListener('transitionend', e => { if (e.target.id === 'app') fitTable(); });

// The header row is sticky. Once the price cards scroll under it, the table gets .stuck and each
// card top closes into a chip with its price. One rect read per frame.
let stuckQueued = false;
function updateStuck() {
  stuckQueued = false;
  const t = $('table.cmp'), deck = t && $('tr.deck', t);
  if (!deck) return;
  const head = t.tHead.rows[0].cells[0];
  t.classList.toggle('stuck', deck.getBoundingClientRect().top < head.getBoundingClientRect().bottom - 2);
}
document.addEventListener('scroll', () => { if (!stuckQueued) { stuckQueued = true; requestAnimationFrame(updateStuck); } }, { capture: true, passive: true });

// ---------------------------------------------------------------- proposal

function renderProposal() {
  const c = S.comp;
  document.title = `Proposal · ${c.client.name}`;
  const recs = c.tabs.map(t => `<div class="box rec" id="rec-${esc(t.line)}">${recCard(t)}</div>`).join('');
  $('#app').innerHTML = topLine(c.archived_at ? 'archived' : true) + clientHead(c, 'proposal') + `<div class="prop"><section class="pset" aria-label="Proposal settings">
      <div><h3>Before it goes out</h3><div class="box pre" id="pre">${checklist()}</div></div>
      <div><h3>Recommendations</h3><div class="recs">${recs || '<p class="muted">Add quotes first.</p>'}</div></div>
      <div><h3>What to include</h3><div class="opts" id="opts">${optionsHtml()}</div></div>
      <div><h3><label for="notes">Note from us</label></h3><textarea class="inp" id="notes" rows="6" maxlength="20000" placeholder="Anything you want the client to read with the comparison. Printed at the end of the PDF."></textarea><span class="saved" id="notes-status" aria-live="polite"></span></div>
    </section>
    <section class="pv" aria-label="Preview"><div class="pvbar"><div class="seg" role="group" aria-label="Preview"><button data-act="view" data-v="pdf" aria-pressed="${S.view === 'pdf'}">PDF</button><button data-act="view" data-v="mail" aria-pressed="${S.view === 'mail'}">Email text</button></div><span class="faint small" id="pv-status" aria-live="polite"></span></div>
      <div class="pvscroll" id="pvscroll"><div id="pdfpages" class="pdfpages"${S.view === 'pdf' ? '' : ' hidden'}><div class="pvmsg">Making the preview…</div></div><div class="mail" id="mail"${S.view === 'mail' ? '' : ' hidden'}></div></div></section></div>`;
  S.recHtml = Object.fromEntries(c.tabs.map(t => [t.line, recCard(t)]));
  S.headHtml = clientHead(c, 'proposal');
  syncNotes(true);
  $('#mail').textContent = mailText();
  schedulePreview(0);
}

// After a change on the proposal page only the parts that changed are redrawn, so neither pane jumps.
function refreshProposal() {
  const c = S.comp;
  if (!$('#pre')) { renderProposal(); return; }
  const fk = focusKey();
  $('#pre').innerHTML = checklist();
  const lines = c.tabs.map(t => t.line);
  if (lines.join() !== Object.keys(S.recHtml || {}).join()) { renderProposal(); return; }
  c.tabs.forEach(t => {
    const html = recCard(t);
    if (S.recHtml[t.line] === html) return;
    S.recHtml[t.line] = html;
    const box = $(`#rec-${CSS.escape(t.line)}`);
    const typing = $('.addr input', box);  // a reason being typed survives the redraw
    const kept = typing && typing.value ? [typing.value, typing.selectionStart, typing.selectionEnd] : null;
    box.innerHTML = html;
    const again = kept && $('.addr input', box);
    if (again) { again.value = kept[0]; again.setSelectionRange(kept[1], kept[2]); }
  });
  const oh = optionsHtml();
  if ($('#opts').innerHTML !== oh) $('#opts').innerHTML = oh;
  const head = clientHead(c, 'proposal');
  if (S.headHtml !== head) { S.headHtml = head; $('.phead').outerHTML = head; }
  syncNotes(false);
  $('#mail').textContent = mailText();
  schedulePreview();
  restoreFocus(fk);
}

function optionsHtml() {
  const o = S.comp.export;
  return `<div><div class="seg" role="group" aria-label="Length"><button data-act="len" data-v="full" aria-pressed="${o.length === 'full'}">Full comparison</button><button data-act="len" data-v="short" aria-pressed="${o.length === 'short'}">Recommendation only</button></div></div>
    <label><input type="checkbox" data-opt="total" ${o.total ? 'checked' : ''}><span>Household total<small>The yearly total of everything recommended</small></span></label>
    <label><input type="checkbox" data-opt="gloss" ${o.gloss ? 'checked' : ''}><span>Explain coverages in plain words<small>A short line under each coverage name</small></span></label>
    <label><input type="checkbox" data-opt="gtk" ${o.gtk ? 'checked' : ''}><span>Good to know<small>Estimates, expiry dates, umbrella requirements</small></span></label>`;
}

function priceText(t, i, long) {
  const p = t.prices[i];
  if (!p.amount) return 'no price found';
  const per = p.unit === 'year' ? ' a year' : p.unit === 'month' ? ' a month' : '';
  return `${p.estimated ? (long ? 'about ' : '≈ ') : ''}${p.amount}${long ? per : p.unit === 'year' ? '/yr' : p.unit === 'month' ? '/mo' : ''}`;
}

function recCard(t) {
  const rec = t.recommended;
  const opts = t.columns.map((c, i) => `<option value="${esc(c.quote_id)}" ${c.quote_id === rec ? 'selected' : ''}>${esc(c.carrier || 'Unknown carrier')} · ${esc(priceText(t, i, false))}</option>`).join('');
  let h = `<div class="rh"><b><i class="${lineClass(t.line)}"></i>${esc(t.label)}</b><select class="sel" data-rec-line="${esc(t.line)}" aria-label="${esc(t.label)} recommendation"><option value="">No recommendation</option>${opts}</select></div>`;
  if (!rec) return h;
  const own = t.own_reasons;
  const have = new Set(own.map(r => r.text.toLowerCase()));
  const sugg = (S.comp.reason_suggestions || []).filter(s => !have.has(s.toLowerCase()));
  h += `<div class="bul">${t.facts.map(f => `<div class="row"><label><input type="checkbox" data-fact="${esc(t.line)}|${esc(f.id)}" ${f.on ? 'checked' : ''}><span>${esc(f.text)}</span></label></div><span class="src">from the ${esc(f.src)}</span>`).join('')}
    ${own.map((r, n) => `<div class="row"><label><input type="checkbox" data-own="${esc(t.line)}|${n}" ${r.on ? 'checked' : ''}><span>${esc(r.text)}</span></label><button type="button" class="rm" data-act="rm-reason" data-line="${esc(t.line)}" data-n="${n}" aria-label="Remove this reason">×</button></div><span class="src">your reason</span>`).join('')}
    ${!t.facts.length && !own.length ? '<p class="faint small np">Nothing to draft from the table yet. Add your own reason below.</p>' : ''}</div>
    <form class="addr" data-line="${esc(t.line)}"><input maxlength="200" placeholder="+ Add a reason" aria-label="Add a reason for ${esc(t.label)}"></form>
    ${sugg.length && own.length < 20 ? `<div class="sugg">${sugg.map(s => `<button type="button" data-act="add-reason" data-line="${esc(t.line)}" data-t="${esc(s)}">+ ${esc(s)}</button>`).join('')}</div>` : ''}`;
  return h;
}

function checklist() {
  const c = S.comp, rv = reviews();
  const missing = c.tabs.filter(t => !t.recommended);
  const picked = c.tabs.filter(t => t.recommended);
  const est = picked.filter(t => { const i = t.columns.findIndex(x => x.quote_id === t.recommended); return t.prices[i].estimated; });
  const busy = c.files.filter(f => ACTIVE.has(f.status)).length;
  const failed = c.files.filter(f => BAD_FILE.has(f.status)).length;
  const it = (ok, text, sub, act) => `<${act ? `button type="button" class="it ${ok ? 'ok' : 'w'}" data-act="${act}"` : `div class="it ${ok ? 'ok' : 'w'}"`}><span class="i" aria-hidden="true">${ok ? '<i class="okm"></i>' : '!'}</span><span>${text}${sub ? `<small>${sub}</small>` : ''}</span><span class="go">${act ? (ok ? 'Edit' : 'Go ›') : ''}</span></${act ? 'button' : 'div'}>`;
  const out = [];
  if (!c.tabs.length) out.push(it(false, 'No quotes yet', 'Add quotes on the comparison page first.'));
  out.push(it(!rv.length, rv.length ? `${plural(rv.length, 'value')} still ${rv.length === 1 ? 'needs' : 'need'} a look` : 'Every flagged value is settled', rv.length ? 'Opens the comparison at the first one' : '', rv.length ? 'check-first' : ''));
  if (c.tabs.length) out.push(it(!missing.length, missing.length ? `No pick yet for ${esc(joinWords(missing.map(t => t.label)))}` : 'A pick for every policy', missing.length ? 'Choose below. Policies without a pick are still compared.' : ''));
  if (est.length) out.push(it(false, `${plural(est.length, 'picked price')} ${est.length === 1 ? 'is an estimate' : 'are estimates'}`, 'A 6-month price counts twice for the year. Marked as estimated in the proposal.'));
  picked.forEach(t => {
    const q = c.quotes[t.recommended];
    if (q && q.expires) out.push(it(false, `${esc(q.carrier)} ${esc(lower(t.label))} quote: ${esc(q.expires)}`, c.export.gtk ? 'Printed under Good to know' : 'Good to know is off, so this is not printed'));
  });
  if (busy) out.push(it(false, `${plural(busy, 'file is', 'files are')} still being read`, "They join the proposal when they're done."));
  if (failed) out.push(it(false, `${failed === 1 ? "1 file couldn't" : `${failed} files couldn't`} be read`, 'Retry or remove it', 'show-files'));
  out.push(it(!!c.client.address, c.client.address ? 'Client name and address' : 'No address on file', c.client.address ? `${esc(c.client.name)} · ${esc(c.client.address)}` : 'Optional. It prints under the client name.', 'edit-client'));
  return out.join('');
}

function mailText() {
  const c = S.comp;
  const picked = c.tabs.filter(t => t.recommended);
  const parts = picked.map(t => {
    const i = t.columns.findIndex(x => x.quote_id === t.recommended);
    return `${t.label}: ${t.columns[i].carrier || 'Unknown carrier'}, ${priceText(t, i, true)}` + t.reasons.map(r => `\n  - ${r}`).join('');
  }).join('\n\n');
  const rt = c.recommended_total;
  const total = c.export.total && rt ? `\n\nAltogether that's ${rt.estimated ? 'about ' : ''}${rt.amount} a year (about ${rt.per_month} a month).` : '';
  const agency = c.agency || {};
  return `Hi,\n\nI compared the quotes we received for you. Here's what I recommend:\n\n${parts || '(Pick a quote for each policy on the left.)'}${total}${c.notes.trim() ? `\n\n${c.notes.trim()}` : ''}\n\nThe full side-by-side comparison is attached.\n\n${[agency.name, agency.phone].filter(Boolean).join(' · ')}`;
}

function setPvStatus(text) { const el = $('#pv-status'); if (el) el.textContent = text; }

function schedulePreview(delay = 500) {
  clearTimeout(S.pvTimer);
  if (!S.comp || S.pv.id !== S.comp.id || S.pv.rev !== S.comp.revision) setPvStatus('Updating…');
  S.pvTimer = setTimeout(() => { loadPreview().catch(fail); }, delay);
}

// The preview is the real PDF, page by page. New pages are loaded off screen and swapped in at once.
async function loadPreview() {
  if (S.route.name !== 'proposal' || !S.comp) return;
  const id = S.comp.id;
  if (S.pv.id === id && S.pv.rev === S.comp.revision) { setPvStatus('Up to date'); return; }
  let info;
  try { info = await api('GET', compPath(id, '/export-preview')); }
  catch (e) { if (S.route.name === 'proposal' && $('#pdfpages')) { $('#pdfpages').innerHTML = `<div class="pvmsg">The preview could not be made: ${esc(e.message)} The PDF may still download.</div>`; setPvStatus(''); } return; }
  if (S.route.name !== 'proposal' || !S.comp || S.comp.id !== id) return;
  if (info.revision < S.comp.revision) { schedulePreview(150); return; }
  const imgs = Array.from({ length: info.pages }, (_, k) => {
    const im = new Image();
    im.className = 'pvpage';
    im.alt = `Page ${k + 1} of ${info.pages} of the client PDF`;
    im.src = compPath(id, `/export-preview/${k + 1}.png?r=${info.revision}`);
    return im;
  });
  try { await Promise.all(imgs.map(im => im.decode())); }
  catch (e) { if (S.pv.rev == null) $('#pdfpages').innerHTML = '<div class="pvmsg">The preview could not be shown. The PDF may still download.</div>'; setPvStatus(''); return; }
  const box = $('#pdfpages');
  if (!box || S.route.name !== 'proposal' || S.comp.id !== id || (S.pv.id === id && S.pv.rev > info.revision)) return;
  const sc = $('#pvscroll'), top = sc.scrollTop;
  box.replaceChildren(...imgs);
  sc.scrollTop = top;
  S.pv = { id, rev: info.revision };
  setPvStatus(S.comp.revision === info.revision ? 'Up to date' : 'Updating…');
  if (S.comp.revision !== info.revision) schedulePreview(150);
}

async function proposalPatch(body, line) {
  try { setComp(await patchComp(body)); }
  catch (e) { fail(e); if (line && S.recHtml) { S.recHtml[line] = null; refreshProposal(); } }
}

function ownReasons(line) {
  const t = S.comp.tabs.find(x => x.line === line);
  return t ? t.own_reasons.map(r => ({ text: r.text, on: r.on })) : [];
}

function addReason(line, text) {
  text = String(text || '').trim();
  if (!text) return;
  const own = ownReasons(line);
  if (own.some(r => r.text.toLowerCase() === text.toLowerCase())) { toast('That reason is already there.'); return; }
  if (own.length >= 20) { toast('A recommendation can have up to 20 reasons of your own.'); return; }
  own.push({ text: text.slice(0, 200), on: true });
  return proposalPatch({ reasons_line: line, own_reasons: own }, line);
}

// ---------------------------------------------------------------- notes (autosave, on the proposal page)

function syncNotes(force) {
  const ta = $('#notes');
  if (!ta || !S.comp) return;
  if (force || (!N.dirty && !N.saving && document.activeElement !== ta)) {
    ta.value = S.comp.notes || '';
    N.base = S.comp.notes || '';
    N.dirty = false;
  }
}

function notesStatus(text, bad) {
  const el = $('#notes-status');
  if (el) { el.textContent = text; el.classList.toggle('bad', !!bad); }
}

function onNotesInput() {
  N.dirty = true;
  notesStatus('');
  clearTimeout(N.timer);
  N.timer = setTimeout(() => { saveNotes().catch(fail); }, 700);
}

async function saveNotes() {
  clearTimeout(N.timer);
  const ta = $('#notes');
  if (!ta || !N.dirty || !S.comp) return;
  if (N.saving) { N.again = true; return; }
  const value = ta.value, id = S.comp.id;
  N.saving = true;
  notesStatus('Saving…');
  try {
    const v = await write('PATCH', compPath(id), { notes: value, base_notes: N.base });
    N.base = value;
    N.dirty = ta.value !== value;
    setComp(v);
    notesStatus(N.dirty ? '' : 'Saved');
  } catch (e) {
    if (e.status === 409) { N.conflict = (e.data && e.data.current) || ''; openModal({ kind: 'notes-conflict', current: N.conflict, locked: true }); }
    else { notesStatus("Couldn't save the note. Trying again…", true); N.timer = setTimeout(() => { saveNotes().catch(fail); }, 3000); }
  } finally {
    N.saving = false;
    if (N.again) { N.again = false; saveNotes().catch(fail); }
  }
}

function flushNotes() { if (N.dirty && !N.saving) saveNotes().catch(fail); }

// ---------------------------------------------------------------- events

async function onAction(el, e) {
  const act = el.dataset.act;
  const c = S.comp;
  const t = c && curTab();
  switch (act) {
    case 'reload': route(); break;
    case 'reload-page': location.reload(); break;
    case 'close': closeModal(); break;

    // board
    case 'new': openModal({ kind: 'new' }); break;
    case 'card-menu': cardMenu(el, el.dataset.id); break;
    case 'open': closeMenu(); location.hash = `#/c/${el.dataset.id}`; break;
    case 'move': closeMenu(); await moveCard(el.dataset.id, el.dataset.stage, 0); break;
    case 'archive': closeMenu(); await archiveCard(el.dataset.id); break;
    case 'unarchive': {
      el.disabled = true;
      try { const v = await unarchive(el.dataset.id); toast(`${v.client.name} is back on the board in ${stageLabel(v.stage)}`); refreshShelf(); }
      catch (err) { el.disabled = false; fail(err); }
      break;
    }
    case 'unarchive-open': {
      const v = await patchComp({ archived: false });
      setComp(v);
      toast(`Back on the board in ${stageLabel(v.stage)}`);
      break;
    }
    case 'delete': {
      closeMenu();
      const card = [...(S.board || []), ...(S.shelf || [])].find(x => x.id === el.dataset.id);
      if (card) openModal({ kind: 'delete', id: card.id, name: card.client_name });
      break;
    }
    case 'delete-yes':
      el.disabled = true;
      try {
        await api('DELETE', compPath(el.dataset.id));
        if (S.batch && S.batch.cid === el.dataset.id) S.batch = null;
        closeModal(true);
        toast('Client deleted');
        refreshBoard();
      } catch (err) { el.disabled = false; fail(err); }
      break;
    case 'outcome': {
      const id = el.closest('.cc').dataset.id;
      try { await api('PATCH', compPath(id), { outcome: el.dataset.v || null }); refreshBoard(); }
      catch (err) { fail(err); }
      break;
    }

    // adding quotes
    case 'skip-drop': { const id = S.modal.cid; closeModal(true); location.hash = `#/c/${id}`; break; }
    case 'keep-working': { const m = S.modal; closeModal(true); if (m.fresh) goToComp(m.cid); break; }
    case 'see-comp': { const id = S.modal.cid; closeModal(true); goToComp(id); break; }
    case 'add': openAdd(); break;
    case 'show-progress':
      if (batchFor(c && c.id) || (S.batch && S.batch.cid === (c && c.id))) openModal({ kind: 'progress', cid: S.batch.cid });
      else openModal({ kind: 'files' });
      break;
    case 'show-files': openModal({ kind: 'files' }); break;
    case 'retry-file':
      try {
        const cid = el.dataset.kind === 'batch' && S.batch ? S.batch.cid : c.id;
        const v = await write('POST', compPath(cid, `/files/${el.dataset.fid}/retry`));
        const b = S.batch && S.batch.files.find(x => x.fid === el.dataset.fid);
        if (b) { b.status = 'queued'; b.error = null; S.batch.running = true; }
        setComp(v);
        refreshProgressViews();
        schedulePoll(true);
      } catch (err) { fail(err); }
      break;
    case 'remove-file':
      try {
        const cid = el.dataset.kind === 'batch' && S.batch ? S.batch.cid : c.id;
        const v = await write('DELETE', compPath(cid, `/files/${el.dataset.fid}`));
        if (S.batch) S.batch.files = S.batch.files.filter(x => x.fid !== el.dataset.fid);
        setComp(v);
        if (S.modal && S.modal.kind === 'progress' && !S.batch.files.length) closeModal(true);
        else drawModal(false);
        toast('File removed');
      } catch (err) { fail(err); }
      break;

    // workspace
    case 'line':
      S.lineBy[c.id] = el.dataset.line;
      S.showOther = false;
      closePanel(true);
      renderComp();
      break;
    case 'rows': S.rowMode = el.dataset.v; renderComp(); break;
    case 'toggle-other': S.showOther = !S.showOther; renderComp(); break;
    case 'keep': case 'unkeep': {
      const row = t.rows[+el.dataset.r];
      const keep = act === 'keep';
      try {
        setComp(await patchComp({ keep_line: t.line, keep_key: row.key, keep }));
        toast(keep ? `${row.label} moved into the table` : `${row.label} moved back to other coverages`, {
          action: { label: 'Undo', fn: async () => setComp(await patchComp({ keep_line: t.line, keep_key: row.key, keep: !keep })) },
        });
      } catch (err) { fail(err); }
      break;
    }
    case 'rec': {
      const col = t.columns[+el.dataset.c];
      const on = t.recommended === col.quote_id;
      try { setComp(await patchComp({ recommend_line: t.line, recommend_quote: on ? null : col.quote_id })); }
      catch (err) { fail(err); }
      break;
    }
    case 'cell': {
      const row = t.rows[+el.dataset.r], col = t.columns[+el.dataset.c];
      openPanel({ type: 'cell', line: t.line, qid: col.quote_id, key: row.key, opener: `[data-act="cell"][data-r="${el.dataset.r}"][data-c="${el.dataset.c}"]` });
      break;
    }
    case 'quote': {
      const col = t.columns[+el.dataset.c];
      openPanel({ type: 'quote', line: t.line, qid: col.quote_id, mode: 'view', opener: `[data-act="quote"][data-c="${el.dataset.c}"]` });
      break;
    }
    case 'gtk': openNotice(+el.dataset.r, +el.dataset.c, +el.dataset.k); break;
    case 'next-review': {
      const rv = reviews(), p = S.panel;
      if (!rv.length) break;
      const i = p && p.type === 'cell' ? rv.findIndex(r => r.qid === p.qid && r.key === p.key && r.line === p.line) : -1;
      goReview(rv[(i + 1) % rv.length]);
      break;
    }

    // panel
    case 'close-panel': closePanel(); break;
    case 'page': S.panel.page = +el.dataset.p; drawPanel(false); break;
    case 'zoom': S.wide = !S.wide; document.body.classList.toggle('panel-wide', S.wide); drawPanel(false); setTimeout(fitTable, 260); break;
    case 'save-cell': await saveCell(el); break;
    case 'accept-cell': el.disabled = true; try { await cellAction('accept'); } catch (err) { el.disabled = false; fail(err); } break;
    case 'pick': await saveCell(el, el.dataset.v); break;
    case 'remove-item': await cellAction('remove', +el.dataset.k).catch(fail); break;
    case 'undo-item': await cellAction('undo', +el.dataset.k).catch(fail); break;
    case 'undo-removed': await cellAction('undo-removed', +el.dataset.k).catch(fail); break;
    case 'set-term':
    case 'undo-term': {
      const ctx = panelCtx();
      if (!ctx) break;
      const body = { quote_id: ctx.col.quote_id, key: 'term', index: null, label: null, vehicle: null };
      try {
        setComp(await cellPatch(act === 'set-term' ? { ...body, action: 'edit', value: el.dataset.v } : { ...body, action: 'undo' }), { panel: true });
        toast(act === 'set-term' ? `Policy term set to ${el.dataset.v} months` : 'Change undone');
      } catch (err) { fail(err); }
      break;
    }
    case 'set-current': {
      const p = S.panel;
      const on = el.getAttribute('aria-pressed') === 'true';
      try { setComp(await patchComp({ current_line: p.line, current_quote: on ? null : p.qid }), { panel: true }); }
      catch (err) { fail(err); }
      break;
    }
    case 'quote-back': S.panel.mode = 'view'; drawPanel(true); break;
    case 'quote-remove': S.panel.mode = 'remove'; drawPanel(true); break;
    case 'quote-remove-yes': {
      el.disabled = true;
      const q = c.quotes[S.panel.qid];
      try {
        const v = await write('DELETE', compPath(c.id, `/quotes/${S.panel.qid}`));
        closePanel(true);
        setComp(v);
        toast(`${q ? q.carrier || 'The' : 'The'} quote was removed`);
      } catch (err) { el.disabled = false; fail(err); }
      break;
    }
    case 'quote-move-yes': {
      el.disabled = true;
      try {
        const v = await write('POST', compPath(c.id, `/quotes/${S.panel.qid}/move`), { line: S.panel.newLine });
        closePanel(true);
        setComp(v);
        schedulePoll(true);
        toast('Reading the file again');
      } catch (err) { el.disabled = false; fail(err); }
      break;
    }

    // proposal
    case 'view':
      S.view = el.dataset.v;
      $$('[data-act="view"]').forEach(b => b.setAttribute('aria-pressed', String(b === el)));
      $('#pdfpages').hidden = S.view !== 'pdf';
      $('#mail').hidden = S.view !== 'mail';
      $('#pvscroll').scrollTop = 0;
      break;
    case 'len': await proposalPatch({ export: { length: el.dataset.v } }); break;
    case 'rm-reason': {
      const own = ownReasons(el.dataset.line);
      own.splice(+el.dataset.n, 1);
      await proposalPatch({ reasons_line: el.dataset.line, own_reasons: own }, el.dataset.line);
      const input = $(`form[data-line="${CSS.escape(el.dataset.line)}"] input`);
      if (input) input.focus();
      break;
    }
    case 'add-reason': await addReason(el.dataset.line, el.dataset.t); break;
    case 'check-first': closeModal(true); S.pendingReview = reviews()[0] || null; location.hash = `#/c/${c.id}`; break;
    case 'edit-client': openModal({ kind: 'client' }); break;
    case 'copy-mail':
      try { await navigator.clipboard.writeText(mailText()); toast('Email text copied'); }
      catch (err) { toast("Couldn't copy. Open Email text and select it instead.", { bad: true }); }
      break;
    case 'download':
    case 'download-anyway': {
      const n = reviews().length;
      if (act === 'download' && n) { openModal({ kind: 'unchecked', n }); break; }
      if (N.dirty || N.saving) { await saveNotes(); await writeChain; }
      closeModal(true);
      location.href = compPath(c.id, '/export.pdf');
      break;
    }
    case 'notes-mine': N.base = N.conflict; N.dirty = true; closeModal(true); saveNotes().catch(fail); break;
    case 'notes-theirs': $('#notes').value = N.conflict; N.base = N.conflict; N.dirty = false; closeModal(true); notesStatus('Saved'); break;
  }
}

document.addEventListener('click', e => {
  if (e.target.matches && e.target.matches('[data-scrim]')) { if (!(S.modal && S.modal.locked)) closeModal(); return; }
  const menu = $('#menu');
  if (menu && !menu.contains(e.target) && !e.target.closest('[data-act="card-menu"]')) closeMenu();
  const el = e.target.closest('[data-act]');
  if (el) {
    if (el.tagName === 'LABEL') return;
    e.preventDefault();
    onAction(el, e).catch(fail);
    return;
  }
  const card = e.target.closest('.cc');
  if (card && !e.target.closest('a,button,input,select')) location.hash = `#/c/${card.dataset.id}`;
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if ($('#menu')) { closeMenu(true); return; }
    if (S.modal) { closeModal(); return; }
    if (S.panel && !e.target.closest('select')) { closePanel(); return; }
  }
  if (e.key === 'Tab' && S.modal) { trapFocus(e); return; }
  const el = e.target;
  if (!el.matches) return;
  if ((e.key === 'Enter' || e.key === ' ') && el.matches('[data-act="cell"]')) { e.preventDefault(); onAction(el, e).catch(fail); return; }
  if (e.key === 'Enter' && el.matches('.cc')) { location.hash = `#/c/${el.dataset.id}`; return; }
  if ((e.key === 'Enter' || e.key === ' ') && el.id === 'drop') { e.preventDefault(); $('#fi').click(); return; }
  if (e.key === 'Enter' && el.matches('#panel input.inp')) {
    e.preventDefault();
    const accept = $('#panel [data-act="accept-cell"]');
    const ctx = panelCtx();
    const unchanged = ctx && ctx.cell && ctx.cell.items.every((it, k) => { const inp = $(`#pv-${k}`); return !inp || inp.value.trim() === (it.value || '').trim(); });
    if (accept && unchanged) accept.click(); else { const save = $('#panel [data-act="save-cell"]'); if (save) save.click(); }
  }
  if (e.key === 'ArrowDown' && $('#menu') && $('#menu').contains(el)) { e.preventDefault(); const b = $$('#menu button'); b[(b.indexOf(el) + 1) % b.length].focus(); }
  if (e.key === 'ArrowUp' && $('#menu') && $('#menu').contains(el)) { e.preventDefault(); const b = $$('#menu button'); b[(b.indexOf(el) - 1 + b.length) % b.length].focus(); }
});

document.addEventListener('submit', e => {
  e.preventDefault();
  const f = e.target;
  if (f.id === 'new-form') submitNew().catch(fail);
  else if (f.id === 'client-form') {
    const name = $('#cl-name').value.trim(), addr = $('#cl-addr').value.trim();
    if (!name) { const err = $('#cl-err'); err.textContent = "Enter the client's name."; err.hidden = false; return; }
    patchComp({ client_name: name, address: addr }).then(v => { closeModal(true); setComp(v); toast('Saved'); }).catch(fail);
  } else if (f.classList.contains('addr')) {
    const input = f.querySelector('input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    Promise.resolve(addReason(f.dataset.line, text)).then(() => {
      const again = $(`form[data-line="${CSS.escape(f.dataset.line)}"] input`);
      if (again) again.focus();
    }).catch(fail);
  }
});

document.addEventListener('input', e => {
  if (e.target.id === 'notes') onNotesInput();
  if (e.target.id === 'search') { S.filter = e.target.value; renderBoard(); }
  if (e.target.id === 'shelf-search') { S.shelfFilter = e.target.value; renderShelf(); }
});

document.addEventListener('change', e => {
  const el = e.target, c = S.comp;
  if (el.id === 'fi') { addFiles(el.files); return; }
  if (el.id === 'stage' && c) {
    patchComp({ stage: el.value }).then(v => { setComp(v); toast(v.archived_at ? `Status set to ${stageLabel(el.value)}. Still archived.` : `Moved to ${stageLabel(el.value)}`); }).catch(err => { fail(err); el.value = c.stage; });
    return;
  }
  if (el.id === 'cur' && c) {
    patchComp({ current_line: curTab().line, current_quote: el.value || null }).then(setComp).catch(fail);
    return;
  }
  if (el.id === 'mv' && S.panel) {
    const q = c.quotes[S.panel.qid];
    if (el.value !== q.line) { S.panel.mode = 'move'; S.panel.newLine = el.value; drawPanel(true); }
    return;
  }
  if (el.dataset.recLine) { proposalPatch({ recommend_line: el.dataset.recLine, recommend_quote: el.value || null }, el.dataset.recLine); return; }
  if (el.dataset.fact) {
    const [line, ...rest] = el.dataset.fact.split('|');
    proposalPatch({ reasons_line: line, fact_id: rest.join('|'), fact_on: el.checked }, line);
    return;
  }
  if (el.dataset.own) {
    const [line, n] = el.dataset.own.split('|');
    const own = ownReasons(line);
    if (own[+n]) own[+n].on = el.checked;
    proposalPatch({ reasons_line: line, own_reasons: own }, line);
    return;
  }
  if (el.dataset.opt) proposalPatch({ export: { [el.dataset.opt]: el.checked } });
});

document.addEventListener('focusout', e => { if (e.target.id === 'notes') flushNotes(); });

document.addEventListener('error', e => {
  const el = e.target;
  if (el.tagName !== 'IMG') return;
  if (el.classList.contains('pgimg')) { const d = document.createElement('div'); d.className = 'nopg'; d.textContent = 'The page image is not available.'; el.replaceWith(d); }
}, true);

// Wheel over the side panel scrolls the panel only; reaching its end never scrolls the page behind.
$('#panel').addEventListener('wheel', e => {
  const pb = e.target.closest('.pb');
  if (!pb) { e.preventDefault(); return; }
  const up = e.deltaY < 0, top = pb.scrollTop <= 0, end = pb.scrollTop + pb.clientHeight >= pb.scrollHeight - 1;
  if ((up && top) || (!up && end)) e.preventDefault();
}, { passive: false });

window.addEventListener('beforeunload', e => {
  const uploading = S.batch && S.batch.files.some(f => f.status === 'uploading');
  if (N.dirty || N.saving || uploading) { flushNotes(); e.preventDefault(); e.returnValue = ''; }
});
window.addEventListener('hashchange', () => { route().catch(fail); });
window.addEventListener('unhandledrejection', e => { fail(e.reason); });

// ---------------------------------------------------------------- versions

async function checkVersion() {
  try {
    const v = await (await fetch('/api/version')).json();
    S.build = S.build || v.build;
    if (v.stale) updateNotice('restart');
  } catch (e) { /* the app is not answering: api() reports that on the next action */ }
}

function updateNotice(kind) {
  if ($('#update')) return;
  const el = document.createElement('div');
  el.id = 'update';
  el.className = 'update';
  el.setAttribute('role', 'alert');
  el.innerHTML = kind === 'reload'
    ? '<span>Quote Compare was updated. Reload this page to use the new version.</span><button class="btn sm" data-act="reload-page">Reload</button>'
    : '<span>Quote Compare was updated while it was running. Close its window and start it again to use the new version.</span>';
  document.body.prepend(el);
}

checkVersion().then(() => route()).catch(fail);
setInterval(checkVersion, 60000);
setInterval(() => { if (S.route.name === 'board' && !S.dragging && !$('#menu') && !S.modal) renderBoard(); }, 60000);  // "Updated 5m ago" stays true
})();
