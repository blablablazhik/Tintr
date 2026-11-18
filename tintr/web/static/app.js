/* Tintr UI. State comes from Python (pywebview js_api); this file only renders it and sends intents.
   Browser mode (run.py --serve, window.TINTR_REMOTE): the same API over HTTP, events over /events (SSE). */
"use strict";

let S = null;                       // last state from Python
const ui = {
  uid: null, idx: null, mode: null, view: "main",
  log: [], logOpen: false, live: {},
  insertSel: null, save: null, port: null, connectError: "",
  zooms: {},                         // zoom (1 = fit) + pan per view: main, ref; kept across keys/modes
  panned: false,                     // last mouse press was a pan drag, not a click
  showAnchor: false,                 // the stage shows the anchor instead of the key (KeyA), same zoom
  showRef: false,                    // the stage shows the reference (KeyR), with its own zoom
  hideLines: false,                  // coloured frames without the line art on top (KeyL), kept across keys
  pipette: false,                    // the next click on the reference adds its colour to the palette
  palNote: "",                       // short note under the palette (pipette result)
  palHex: "#888888",                 // the colour typed / chosen in the "+" menu of the palette
  tool: "select", prevTool: "brush",  // drawing tools over the coloured frame: select brush eraser bucket picker
  size: 12, color: null,             // brush diameter in frame px; palette name ("C3") of brush and bucket
  toolPal: false, space: false,      // tool palette open; Space held (left drag pans)
  overRef: false,                    // the cursor is over the reference (I = pipette into the palette there)
};

const REMOTE = !!window.TINTR_REMOTE;
// browser mode: api().name(...args) → POST /api/name with the args as a JSON list
const httpApi = new Proxy({}, {
  get: (_t, name) => async (...args) => {
    const r = await fetch(`/api/${name}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args),
    });
    if (!r.ok) throw new Error(r.status === 403 ? "Немає доступу: відкрийте посилання з токеном з терміналу." : await r.text());
    return r.json();
  },
});
const api = () => (REMOTE ? httpApi : window.pywebview.api);
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => "$" + (Number(v) || 0).toFixed(2);

const ICON = {
  settings: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>',
  folder: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>',
  alert: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="flex-shrink:0;margin-top:2px" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h0"/></svg>',
  select: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M6 3l12 9-5.5 1.2L15 20l-2.6 1.1-2.6-6.6L6 18z"/></svg>',
  brush: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4 20l1.2-4.4L16.5 4.3a2.1 2.1 0 0 1 3 3L8.4 18.8z"/><path d="M14.5 6.3l3 3"/></svg>',
  eraser: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M8.5 20L3.8 15.3a1.5 1.5 0 0 1 0-2.1L13 4a1.5 1.5 0 0 1 2.1 0l4.9 4.9a1.5 1.5 0 0 1 0 2.1L11 20z"/><path d="M8.5 20H20M8 9.2l6.8 6.8"/></svg>',
  bucket: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M4.5 11.5L11 5l7 7-6.5 6.5a1.5 1.5 0 0 1-2.1 0l-4.9-4.9a1.5 1.5 0 0 1 0-2.1z"/><path d="M4.5 12h13M8 3.5L10.5 6"/><path d="M20 15.5s1.5 2 1.5 3a1.5 1.5 0 0 1-3 0c0-1 1.5-3 1.5-3z"/></svg>',
  picker: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M13.5 6.5l4 4M15.5 4.5a2.1 2.1 0 0 1 3 0l1 1a2.1 2.1 0 0 1 0 3L17.5 10.5l-4-4z"/><path d="M14.5 9.5L6 18l-2.5.5L4 16l8.5-8.5"/></svg>',
  eye: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  undo: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>',
  redo: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d="M15 14l5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/></svg>',
  more: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>',
  arrow: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#3E5266" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
};

// ------------------------------------------------------------------ events from Python
window.tintr = {
  on(evt) {
    const d = evt.data;
    switch (evt.type) {
      case "state": setState(d); break;
      case "log":
        ui.log.push(d); if (ui.log.length > 800) ui.log.shift();
        renderFooter(); appendLog(d); break;
      case "progress": if (S) { S.progress = d; renderFooter(); renderProgressBlock(); } break;
      case "key":
        if (d.live) ui.live[d.id] = d.live; else delete ui.live[d.id];
        renderLive(); break;
      case "done": onDone(d); break;
    }
  },
};

function setState(st) {
  const wasConnected = S && S.connected;
  S = st;
  if (st.log) ui.log = st.log;
  // live statuses come with the state too
  ui.live = {};
  for (const c of S.clips || []) for (const l of c.layers) for (const k of l.keys) if (k.live) ui.live[`${l.uid}:${k.idx}`] = k.live;
  if (S.connected && !wasConnected) ui.view = "main";
  render();
}

function onDone(d) {
  if (d.task === "Підключення до TVPaint" && !d.ok) {
    ui.connectError = ui.quietConnect ? "" : d.error;
    ui.quietConnect = false;
    render();
    return;
  }
  ui.quietConnect = false;
  if (!d.ok && d.error && d.error !== "Зупинено.") modal(errorDialog(d.task, d.error));
  if (d.ok && d.task === "Вставка в TVPaint") ui.insertSel = null;
}

async function call(name, ...args) {
  try {
    const r = await api()[name](...args);
    if (r && r.state) setState(r.state);
    if (r && r.ok === false && r.error) modal(errorDialog("Не вдалося", r.error));
    return r || {};
  } catch (e) {
    modal(errorDialog("Помилка", String(e)));
    return {};
  }
}

// ------------------------------------------------------------------ selection helpers
const layers = () => (S && S.clips ? S.clips.flatMap((c) => c.layers) : []);
const curLayer = () => layers().find((l) => l.uid === ui.uid) || null;
const curKey = () => { const l = curLayer(); return l ? l.keys.find((k) => k.idx === ui.idx) || null : null; };
const master = (l, k) => (k && k.dup != null ? l.keys.find((x) => x.idx === k.dup) : k);

function ensureSelection() {
  const ls = layers();
  if (!ls.length) { ui.uid = null; ui.idx = null; return; }
  if (!curLayer()) ui.uid = ls[0].uid;
  const l = curLayer();
  if (!curKey()) ui.idx = l.anchorIdx ?? (l.keys[0] ? l.keys[0].idx : null);
  const k = curKey();
  const avail = modesFor(k);
  if (!ui.mode || !avail.includes(ui.mode)) ui.mode = avail.includes("color") ? "color" : avail.includes("regions") ? "regions" : "line";
}

function modesFor(k) {
  const out = [];
  if (k && k.key) out.push("line");
  if (k && k.regions) out.push("regions");
  if (k && k.color) out.push("color");
  return out;
}

// the anchor and the frames after it are one step: the same stage, strip and tools
const STEP_OF_STAGE = { missing: 0, export: 0, ref: 1, anchor: 1, approve: 1, frames: 1, ready: 2, done: 3 };

// ------------------------------------------------------------------ render
function render() {
  const app = $("#app");
  if (!S) return;
  if (!S.connected) { app.innerHTML = header(false) + connectView() + footer(); bindConnect(); renderFooter(); return; }
  ensureSelection();
  // a re-render that the user didn't ask for (autosaved strokes, a state push) keeps open menus open;
  // clicking an action closes them before its own render
  const openMenus = [...document.querySelectorAll(".menu:not([hidden])")].map((m) => m.id);
  const center = ui.view === "insert" ? insertView() : centerView();
  app.innerHTML = header(true) + `<div class="body">${treeView()}${center}${sideView()}</div>` + footer();
  bindMain();
  for (const id of openMenus) {
    const m = document.getElementById(id);
    if (m) { m.hidden = false; m.previousElementSibling.setAttribute("aria-expanded", "true"); }
  }
  renderFooter();
}

function header(connected) {
  if (!connected) {
    return `<header><span class="brand">Tintr</span><span class="proj">не підключено</span><div class="grow"></div>
      <button class="icon-btn" data-act="settings" aria-label="Налаштування">${ICON.settings}</button></header>`;
  }
  const l = curLayer();
  const cur = l ? STEP_OF_STAGE[l.stage] : 0;
  const names = ["Ключі", "Розфарбування", "Вставка"], last = names.length - 1;
  const steps = names.map((n, i) => {
    const cls = cur > i ? "done" : cur === i || (i === last && cur > last) ? "current" : "todo";
    const view = i === last ? "insert" : "main";
    return `<button class="step ${cls}" data-act="view" data-view="${view}"><span class="dot"></span>${n}</button>` + (i < last ? '<span class="step-sep"></span>' : "");
  }).join("");
  const busy = !!S.busy;
  const proj = menu("m-proj", ICON.more, "Проект", [
    { act: "folder", label: "Відкрити папку з кадрами" },
    { act: "rescan", label: "Пересканувати проект", hint: "знайти нові шари з тегом _cc-", dis: busy },
    { act: "settings", label: "Налаштування" },
  ], "icon-btn");
  return `<header><span class="brand">Tintr</span><span class="proj">${esc(S.project.name)}</span>
    <nav class="steps" aria-label="Кроки">${steps}</nav><div class="grow"></div>
    ${layers().length ? allClipsMenu() : ""}
    <span class="spend">Витрачено на проект ${money(S.cost.project)}</span>${proj}</header>`;
}

// ---------- dropdown menus: a trigger with data-act="menu" and a hidden list of actions under it
function menu(id, trigger, label, items, cls = "btn sm", menuCls = "") {
  const rows = items.map((i) => `<button role="menuitem" data-act="${i.act}" ${i.dis ? "disabled" : ""} ${i.attrs || ""}>
    <span>${i.label}</span>${i.hint ? `<small>${esc(i.hint)}</small>` : ""}</button>`).join("");
  return `<div class="menu-wrap"><button class="${cls}" data-act="menu" data-menu="${id}" aria-haspopup="menu" aria-expanded="false" aria-label="${esc(label)}" title="${esc(label)}">${trigger}</button>
    <div class="menu ${menuCls}" id="${id}" role="menu" hidden>${rows}</div></div>`;
}

function toggleMenu(id) {
  const m = document.getElementById(id), open = m && m.hidden;
  closeMenus();
  if (!open) return;
  m.hidden = false;
  m.previousElementSibling.setAttribute("aria-expanded", "true");
}

function closeMenus() {
  for (const m of document.querySelectorAll(".menu:not([hidden])")) {
    m.hidden = true;
    m.previousElementSibling.setAttribute("aria-expanded", "false");
  }
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".menu-wrap")) closeMenus(); });

function footer() {
  return `<footer><span class="last" id="f-last"></span><div class="fbar" id="f-bar" hidden><div class="bar"><div id="f-bar-in" style="width:0"></div></div></div>
    <span id="f-prog"></span><button class="link" data-act="log">Журнал</button></footer>`;
}

function renderFooter() {
  const last = $("#f-last");
  if (!last || !S) return;
  const line = ui.log.length ? ui.log[ui.log.length - 1].replace(/^\d\d:\d\d:\d\d\s+/, "") : "";
  last.textContent = line;
  const p = S.progress || {};
  const busy = !!S.busy;
  $("#f-bar").hidden = !(busy && p.total);
  if (busy && p.total) $("#f-bar-in").style.width = `${Math.round((100 * p.n) / p.total)}%`;
  $("#f-prog").textContent = busy ? (p.msg || S.busy) : "";
}

function appendLog(line) {
  const d = $("#log-drawer");
  if (d.hidden) return;
  d.textContent += (d.textContent ? "\n" : "") + line;
  d.scrollTop = d.scrollHeight;
}

// ---------- connect
function connectView() {
  const port = ui.port ?? S.port;
  const busy = !!S.busy;
  const err = ui.connectError
    ? `<div class="alert">${ICON.alert}<span>TVPaint не відповідає на порту ${esc(port)}. Перевірте, що TVPaint запущено і плагін tvpaint-rpc активний.<br><small>${esc(ui.connectError)}</small></span></div>` : "";
  const recent = (S.recent || []).map((r) => `<div class="recent-row"><b>${esc(r.name)}</b>
      <span>${r.layers} шар(ів) у ${r.clips} кліп(ах) · ${money(r.usd)}</span><small>${esc(r.when)}</small></div>`).join("");
  return `<main class="connect"><div class="connect-box">
    <div style="display:flex;flex-direction:column;gap:10px"><h1>Підключіться до TVPaint</h1>
    <p>Відкрийте проект у TVPaint з увімкненим плагіном tvpaint-rpc. Tintr знайде шари з тегом <span style="color:var(--fg)">_cc-</span> і запам'ятає їх для наступних запусків.</p></div>
    <div class="connect-row"><div class="field"><label for="port">Порт</label><input id="port" type="number" value="${esc(port)}"></div>
      <button class="btn primary" data-act="connect" ${busy ? "disabled" : ""}>${busy ? "Підключення…" : "Підключитися"}</button></div>
    ${err}
    ${recent ? `<div style="display:flex;flex-direction:column"><span class="eyebrow" style="padding-bottom:6px">Нещодавні проекти</span>${recent}
      <span style="color:var(--faint);font-size:12px;padding-top:6px">Проект відкривається в TVPaint. Tintr підхопить той, що відкритий зараз.</span></div>` : ""}
  </div></main>`;
}

function bindConnect() {
  bindCommon();
  const inp = $("#port");
  if (inp) {
    inp.addEventListener("input", () => { ui.port = inp.value; });
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") doConnect(); });
  }
}

function doConnect() {
  ui.connectError = "";
  const port = parseInt(ui.port ?? S.port, 10) || 3000;
  call("connect", port, false);
}

// ---------- tree
function treeView() {
  const clips = (S.clips || []).map((c) => `<div class="clip"><div class="clip-title"><b>${esc(c.name)}</b><span>кліп ${c.pos}</span></div>
    ${c.layers.map((l) => `<button class="layer ${l.uid === ui.uid ? "sel" : ""}" data-act="layer" data-uid="${esc(l.uid)}" title="${esc(layerState(l))}">
      <span class="swatch" style="${l.swatch ? `background:${l.swatch}` : ""}"></span>
      <span class="layer-text"><b>${esc(l.name)}</b></span>
      <span class="tdot tone-${l.tone} ${layerBusy(l) ? "live" : ""}" aria-label="${esc(layerState(l))}"></span>
      <span class="count">${l.done}/${l.total}</span></button>`).join("")}</div>`).join("");
  const scanned = S.project.scanned;
  const empty = scanned ? "Шарів з тегом _cc- не знайдено." : "Проект ще не скановано.";
  return `<aside class="tree"><div class="tree-head"><span class="eyebrow">Кліпи</span></div>
    <div class="tree-clips">${clips || `<div style="padding:0 20px;color:var(--muted)">${empty}</div>`}</div>
    </aside>`;
}

function allClipsMenu() {
  // one parallel run over every clip; layers without a reference are skipped
  const a = S.allAnchors || 0, f = S.allFollows || 0, busy = !!S.busy;
  const items = [
    { act: "all-anchors", label: `Усі якорі${a ? ` · ${a}` : ""}`, hint: "шари з референсом, де якір ще не розфарбовано", dis: !a || busy },
    { act: "all-follows", label: `Усі кадри за якорями${f ? ` · ${f}` : ""}`, hint: "шари із затвердженим якорем", dis: !f || busy },
  ];
  if (S.awaiting) items.push({ act: "noop", label: `${S.awaiting} якір(ів) чекають затвердження`, hint: "затвердьте їх, щоб розфарбувати кадри", dis: true });
  const n = a + f;
  return menu("m-all", `Розфарбувати все${n ? ` · ${n}` : ""} ▾`, "Розфарбувати всі кліпи паралельно", items, `btn sm${n && !busy ? " all-on" : ""}`);
}

const layerBusy = (l) => l.keys.some((k) => ui.live[`${l.uid}:${k.idx}`]);

function layerState(l) {
  if (layerBusy(l)) return "розфарбовується";
  if (l.stage === "export" && l.keys.some((k) => k.key)) return "ключі застаріли";
  if (l.stage === "anchor" || l.stage === "export") return l.keys.some((k) => k.key) ? `${l.total} ключів · ${l.unique} унікальних` : l.stateText;
  if (l.stage === "frames") return `${l.followsN} кадр(ів) до розфарбування`;
  return l.stateText;
}

// ---------- center
function centerView() {
  const l = curLayer();
  if (!l) {
    const busy = S.busy ? "disabled" : "";
    const body = S.busy === "Сканування проекту"
      ? `<b>Сканування…</b>Tintr проходить усі кліпи проекту і шукає шари з тегом _cc-. На великому проекті це може тривати.`
      : S.project.scanned
        ? `<b>Шарів з тегом не знайдено</b>Додайте тег _cc- до назви шару персонажа в TVPaint і проскануйте проект знову.
           <button class="btn primary" data-act="rescan" ${busy}>Пересканувати</button>`
        : `<b>Проект ще не скановано</b>Tintr пройде всі кліпи і знайде шари з тегом _cc-. Результат запам'ятовується, тож наступні підключення будуть миттєві.
           <button class="btn primary" data-act="rescan" ${busy}>Сканувати проект</button>`;
    return `<main class="center"><div class="stage"><div class="empty">${body}</div></div></main>`;
  }
  const k = curKey();
  const m = master(l, k);
  const modes = modesFor(k);
  const modeBtns = [["line", "Лайн"], ["regions", "Області"], ["color", "Колір"]].map(([id, n]) =>
    `<button class="mode ${ui.mode === id ? "on" : ""}" data-act="mode" data-mode="${id}" ${modes.includes(id) ? "" : "disabled"}>${n}</button>`).join("");
  const canAnchor = m && l.anchorIdx != null && m.idx !== l.anchorIdx && !S.busy;
  const info = k ? `#${k.idx} · кадр ${k.start} · ${k.length} кадр(ів)${k.dup != null ? ` · повтор #${k.dup}` : ""}` : "";
  const state = keyChip(l, k, m);
  const more = menu("m-layer", ICON.more, "Дії з шаром", [
    { act: "set-anchor", label: `Зробити ключ #${m ? m.idx : "–"} якорем`, hint: canAnchor ? "якір задає кольори решти кадрів" : "цей ключ уже якір", dis: !canAnchor },
    { act: "export", label: "Оновити ключі з TVPaint", hint: "якщо кадри змінили в TVPaint", dis: !!S.busy },
    { act: "ref", label: l.refUrl ? "Змінити референс" : "Обрати референс", dis: !!S.busy },
  ], "icon-btn");
  return `<main class="center">
    <div class="toolbar"><b>${esc(l.name)}</b><span class="info">${info}</span>${state}<div class="grow"></div>
      <div class="modes" role="group" aria-label="Що показати">${modeBtns}</div>${more}</div>
    <div class="stage" id="stage">${stageView(l, k)}</div>
    ${groupsView(l)}
  </main>`;
}

function keyChip(l, k, m) {
  // the one place that says what state this key is in
  if (!k || !k.key) return "";
  const chip = (tone, text, title = "") => `<span class="chip tone-${tone}" ${title ? `title="${esc(title)}"` : ""}>${text}</span>`;
  if (k.error) return chip("warn", "помилка", k.error);
  if (ui.live[`${l.uid}:${k.idx}`] || (m && ui.live[`${l.uid}:${m.idx}`])) return chip("accent", "розфарбовується");
  if (m && m.idx === l.anchorIdx) {
    if (!m.colored) return chip("muted", "якір · ще не розфарбовано");
    return l.anchorApproved ? chip("ok", "якір · затверджено") : chip("accent", "якір · перевірте");
  }
  if (!m || !m.colored) return chip("muted", "не розфарбовано");
  if (!m.followed) return chip("accent", "за старим якорем", "Розфарбуйте кадри знову за затвердженим якорем");
  return chip("ok", "розфарбовано за якорем");
}

function stageView(l, k) {
  if (!k || !k.key) {
    return `<div class="empty"><b>Ключі ще не виведені</b>Tintr збере ключові кадри цього кліпу з TVPaint одним викликом і одразу знайде на них області для розфарбування. Це безкоштовно.
      <button class="btn primary" data-act="export" ${S.busy ? "disabled" : ""}>Вивести ключі</button></div>`;
  }
  if (ui.showRef && l.refUrl) {  // the reference at full size; the pipette works here too
    return `<div class="ref-stage zoomable ref-pick ${ui.pipette ? "picking" : ""}" data-zoom="refmain"><div class="zoom">
      <img src="${l.refUrl.replace(/&w=\d+/, "")}" alt="Референс ${esc(l.name)}"></div></div>` + dockView("ref", null) + `<span class="anchor-tag">Референс · ${esc(l.ref)}</span>`;
  }
  const a = ui.showAnchor ? anchorOf(l) : null;
  if (a) {  // quick check against the anchor: same view, same zoom; regions are not editable here
    const mode = modesFor(a).includes(ui.mode) ? ui.mode : "color";
    return frameHtml(a, mode, true, "", false) + dockView(mode, null) + `<span class="anchor-tag">Якір · ключ #${a.idx}</span>`;
  }
  return frameHtml(k, ui.mode, true) + dockView(ui.mode, ui.mode === "color" && master(l, k).colored ? l : null);
}

function frameHtml(k, mode, main, extra = "", editable = true) {
  const W = S.project.w, H = S.project.h;
  let imgs = "", paper = "";
  if (mode === "color") {
    const ed = main && editable ? editorFor(curLayer(), k) : null;  // drawing tools draw into a canvas here
    const base = ed ? `<div class="edit-slot" data-ed="${ed.id}"><img src="${k.color}" alt=""></div>` : `<img src="${k.color}" alt="">`;
    imgs = `${base}<img class="lines" src="${k.key}" alt="">`; paper = "paper" + (ui.hideLines ? " no-lines" : "");
  }
  else if (mode === "regions") imgs = `<img src="${k.regions}" alt="">`;
  else imgs = `<img src="${k.key}" alt="">`;
  const click = main && editable && mode !== "line" ? `clickable tool-${mode === "color" ? ui.tool : "select"}` : "";
  return `<div class="frame ${paper} ${click} ${extra}" ${main ? 'id="frame" data-zoom="main"' : ""} data-w="${W}" data-h="${H}" style="aspect-ratio:${W}/${H}"><div class="zoom" ${main ? 'id="zoom"' : ""}>${imgs}</div></div>`;
}

function fitFrames() {
  // largest size that keeps the frame's aspect ratio inside the stage (or its figure column)
  const stage = $("#stage");
  if (!stage) return;
  const cs = getComputedStyle(stage);
  const innerW = stage.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const innerH = stage.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
  for (const f of stage.querySelectorAll(".frame")) {
    const inFig = f.parentElement !== stage;
    const W = +f.dataset.w, H = +f.dataset.h;
    const maxW = inFig ? f.parentElement.getBoundingClientRect().width : innerW;
    const maxH = innerH - (inFig ? 28 : 0);
    const s = Math.min(maxW / W, maxH / H);
    f.style.width = `${Math.max(40, Math.floor(W * s))}px`;
  }
  applyAllZooms();
}

// ---------- the dock: one floating panel under the frame (drawing tools, lines, zoom, shortcuts)
function dockView(mode, editable) {
  // editable: the layer whose coloured key the tools draw on (colour mode), or null
  const parts = [];
  if (editable) parts.push(toolsView(editable));
  if (mode === "color") parts.push(`<button class="dbtn ${ui.hideLines ? "" : "on"}" data-act="lines" id="lines-btn" aria-pressed="${!ui.hideLines}"
    title="Лайн поверх кольору (L)" aria-label="Лайн поверх кольору">${ICON.eye}</button>`);
  const pct = `<span id="zoom-pct">${Math.round(zstate(stageZoom()).s * 100)}%</span>`;
  const zoomItems = [{ act: "zoom", label: "Вписати", hint: "F або 0", attrs: 'data-z="fit"' }];
  if (mode !== "ref") zoomItems.push({ act: "zoom", label: "1:1", hint: "піксель кадру = піксель екрана", attrs: 'data-z="px"' });
  parts.push(`<div class="dgroup"><button class="dbtn" data-act="zoom" data-z="out" title="Зменшити (−)" aria-label="Зменшити">−</button>
    ${menu("m-zoom", pct, "Масштаб", zoomItems, "dpct", "up")}
    <button class="dbtn" data-act="zoom" data-z="in" title="Збільшити (+)" aria-label="Збільшити">+</button></div>`);
  parts.push(`<button class="dbtn help-btn ${ui.help ? "on" : ""}" data-act="help" title="Гарячі клавіші (H)" aria-label="Гарячі клавіші" aria-expanded="${ui.help}">?</button>`);
  return `<div class="dock" role="toolbar" aria-label="Інструменти і масштаб">${parts.join('<span class="dsep"></span>')}</div>${helpView()}`;
}

// ---------- zoom / pan of the main preview (line, regions, colour)

// every zoomable view: an element with data-zoom="<name>" holding a .zoom layer that is scaled and moved
const zoomEl = (name) => document.querySelector(`[data-zoom="${name}"]`);
const zstate = (name) => ui.zooms[name] || (ui.zooms[name] = { s: 1, x: 0, y: 0 });

function applyZoom(name = "main") {
  const el = zoomEl(name), z = el && el.querySelector(".zoom");
  if (!z) return;
  const Z = zstate(name), w = el.clientWidth, h = el.clientHeight;
  Z.s = Math.min(24, Math.max(1, Z.s));
  Z.x = Math.min(0, Math.max(w - w * Z.s, Z.x));
  Z.y = Math.min(0, Math.max(h - h * Z.s, Z.y));
  z.style.transform = `translate(${Z.x}px, ${Z.y}px) scale(${Z.s})`;
  el.classList.toggle("zoomed", Z.s > 1);
  if (name === stageZoom()) {
    const pct = $("#zoom-pct");
    if (pct) pct.textContent = `${Math.round(Z.s * 100)}%`;
  }
}

function applyAllZooms() { for (const el of document.querySelectorAll("[data-zoom]")) applyZoom(el.dataset.zoom); }

function zoomTo(name, s, cx, cy) {
  // keep the image point under (cx, cy) (client px; default: the view's centre) in place
  const el = zoomEl(name);
  if (!el) return;
  const r = el.getBoundingClientRect(), Z = zstate(name);
  if (cx == null) { cx = r.left + r.width / 2; cy = r.top + r.height / 2; }
  s = Math.min(24, Math.max(1, s));
  const lx = (cx - r.left - Z.x) / Z.s, ly = (cy - r.top - Z.y) / Z.s;
  Z.x = cx - r.left - lx * s; Z.y = cy - r.top - ly * s; Z.s = s;
  applyZoom(name);
}

const stageZoom = () => (ui.showRef ? "refmain" : "main");  // the zoom that the bar, the keys and F drive

function zoomCmd(cmd) {
  const name = stageZoom();
  const el = zoomEl(name);
  if (!el) return;
  closePopover();
  if (cmd === "in") zoomTo(name, zstate(name).s * 1.5);
  else if (cmd === "out") zoomTo(name, zstate(name).s / 1.5);
  else if (cmd === "px") zoomTo(name, S.project.w / el.clientWidth);
  else { ui.zooms[name] = { s: 1, x: 0, y: 0 }; applyZoom(name); }
}

function bindZoomable(name) {
  // wheel zooms at the cursor; drag pans. On the main frame a left drag pans only when zoomed in
  // (a click without moving still picks a region); elsewhere any left drag pans; middle drag always.
  const el = zoomEl(name);
  if (!el || el.dataset.zbound) return;
  el.dataset.zbound = "1";
  el.addEventListener("wheel", (e) => {
    e.preventDefault();
    closePopover();
    zoomTo(name, zstate(name).s * Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
  }, { passive: false });
  el.addEventListener("mousedown", (e) => {
    ui.panned = false;
    const Z = zstate(name);
    const leftPans = name !== "main" || ui.space || (Z.s > 1 && !drawing());
    if (!(e.button === 1 || (e.button === 0 && leftPans))) return;
    e.preventDefault();
    const x0 = e.clientX, y0 = e.clientY, zx = Z.x, zy = Z.y;
    const move = (ev) => {
      if (!ui.panned && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 4) return;
      ui.panned = true;
      el.classList.add("panning");
      Z.x = zx + (ev.clientX - x0); Z.y = zy + (ev.clientY - y0);
      applyZoom(name);
    };
    const up = () => { el.classList.remove("panning"); window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
  el.addEventListener("auxclick", (e) => { if (e.button === 1) e.preventDefault(); });
  el.addEventListener("dblclick", () => { if (name !== "main") { ui.zooms[name] = { s: 1, x: 0, y: 0 }; applyZoom(name); } });
}

// ---------- the anchor in the stage (KeyA) and the reference in the stage (KeyR)
function anchorOf(l) { const a = l && l.keys.find((x) => x.idx === l.anchorIdx); return a && a.colored ? a : null; }

function toggleLines() {
  // a class switch instead of a re-render: the zoom and pan stay exactly where they are
  ui.hideLines = !ui.hideLines;
  for (const el of document.querySelectorAll(".frame.paper")) el.classList.toggle("no-lines", ui.hideLines);
  const b = $("#lines-btn");
  if (b) { b.classList.toggle("on", !ui.hideLines); b.setAttribute("aria-pressed", String(!ui.hideLines)); }
}

function toggleAnchor(on = !ui.showAnchor) {
  if (on && !anchorOf(curLayer())) on = false;
  if (on === ui.showAnchor && !(on && ui.showRef)) return;
  ui.showAnchor = on;
  if (on) ui.showRef = false;
  closePopover();
  render();
}

function toggleRef(on = !ui.showRef) {
  // like A for the anchor: the reference in the stage; R again (or Esc) goes back to the key
  const l = curLayer();
  if (on && !(l && l.refUrl)) on = false;
  if (on === ui.showRef && !(on && ui.showAnchor)) return;
  ui.showRef = on;
  if (on) ui.showAnchor = false;
  closePopover();
  render();
}

function groupsView(l) {
  // one row: the anchor first (marked on the thumbnail), the unique keys, then the repeats, folded by default
  // (they take the colour of their original for free); a selected repeat unfolds them
  if (!l.keys.some((k) => k.key)) return "";
  const main = [...l.keys.filter((k) => k.idx === l.anchorIdx), ...l.keys.filter((k) => k.dup == null && k.idx !== l.anchorIdx)];
  const dups = l.keys.filter((k) => k.dup != null);
  const open = ui.showDups || dups.some((k) => k.idx === ui.idx);
  const more = dups.length ? `<button class="kf-more" data-act="dups" aria-expanded="${open}"
    title="${open ? "Згорнути повтори" : "Повтори беруть колір оригіналу, безкоштовно"}"><b>${open ? "−" : "+"}${dups.length}</b><span>${open ? "згорнути" : "повтори"}</span></button>` : "";
  return `<section class="strip" id="strip" aria-label="Ключові кадри">${main.map((k) => kfHtml(l, k)).join("")}${more}${open ? dups.map((k) => kfHtml(l, k)).join("") : ""}</section>`;
}

function keyMenu(e) {
  // right click on a key in the strip: make it the anchor
  const kf = e.target.closest(".kf"), l = curLayer();
  if (!kf || !l) return;
  e.preventDefault();
  const k = l.keys.find((x) => x.idx === +kf.dataset.idx), m = master(l, k);
  const isAnchor = m.idx === l.anchorIdx;
  const pop = $("#popover");
  pop.innerHTML = `<span class="t">Ключ #${k.idx}${k.dup != null ? ` · повтор #${k.dup}` : ""}</span>
    <button class="btn sm" data-k="anchor" ${isAnchor || S.busy ? "disabled" : ""}>${isAnchor ? "Це вже якір" : `Зробити ${k.dup != null ? `оригінал #${m.idx}` : "його"} якорем`}</button>`;
  pop.hidden = false;
  pop.style.left = `${Math.min(window.innerWidth - 250, e.clientX + 4)}px`;
  pop.style.top = `${Math.min(window.innerHeight - pop.offsetHeight - 12, e.clientY + 4)}px`;
  pop.onclick = (ev) => {
    if (!ev.target.closest('[data-k="anchor"]')) return;
    closePopover();
    call("set_anchor", l.uid, m.idx);
  };
}

function kfHtml(l, k) {
  const live = ui.live[`${l.uid}:${k.idx}`];
  const thumb = k.colorThumb ? `<img src="${k.colorThumb}" alt=""><img src="${k.keyThumb}" alt="">` : `<img src="${k.keyThumb}" alt="">`;
  const isAnchor = k.idx === l.anchorIdx;
  const cls = ["kf", k.idx === ui.idx ? "sel" : "", isAnchor ? "anchor" : "", k.dup != null ? "dup" : ""].join(" ");
  return `<button class="${cls}" data-act="key" data-idx="${k.idx}" title="${k.error ? esc(k.error) : ""}">
    <span class="thumb ${k.colorThumb ? "paper" : ""}">${thumb}
      <span class="live" data-live="${esc(l.uid)}:${k.idx}" ${live ? "" : "hidden"}><span>${live ? esc(liveText(live)) : ""}</span></span>
      ${k.error ? '<span class="err"></span>' : ""}${isAnchor ? '<span class="kf-tag">якір</span>' : ""}</span>
    <span class="meta"><b>#${k.idx}</b><span>${k.dup != null ? `повтор #${k.dup}` : `кадр ${k.start} · ${k.length}`}</span></span></button>`;
}

function liveText(live) {
  const s = Math.round(Date.now() / 1000 - live.t0);
  return live.text === "генерується" ? `генерується ${s} с` : live.text;
}

function renderLive() {
  for (const el of document.querySelectorAll("[data-live]")) {
    const live = ui.live[el.dataset.live];
    el.hidden = !live;
    if (live) el.firstElementChild.textContent = liveText(live);
  }
  // the tree row state ("розфарбовується") depends on live statuses too
  for (const l of layers()) {
    const row = document.querySelector(`.layer[data-uid="${CSS.escape(l.uid)}"]`);
    if (!row) continue;
    row.title = layerState(l);
    row.querySelector(".tdot").classList.toggle("live", layerBusy(l));
  }
}
setInterval(() => { if (Object.keys(ui.live).length) renderLive(); }, 1000);

// ---------- pipette: a colour from the reference into the layer palette
// the side panel may show the labelled reference and the stage (R) the plain one: each image is sampled itself
const refPixels = {};  // image url → { ctx, w, h }

function refSampler(url) {
  // the image drawn once into an offscreen canvas (same origin, so its pixels are readable)
  if (!url || refPixels[url]) return;
  refPixels[url] = { ctx: null, w: 0, h: 0 };
  const img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas");
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    refPixels[url] = { ctx, w: c.width, h: c.height };
  };
  img.onerror = () => { delete refPixels[url]; };
  img.src = url;
}

function samplePickers() {
  for (const img of document.querySelectorAll(".ref-pick img")) refSampler(img.getAttribute("src"));
}

function refColorAt(view, cx, cy) {
  // client point → pixel of the shown image (object-fit: contain inside the zoomed box) → "#RRGGBB"
  const img = view.querySelector("img"), px = img && refPixels[img.getAttribute("src")];
  if (!px || !px.ctx) return null;
  const r = img.getBoundingClientRect(), s = Math.min(r.width / px.w, r.height / px.h);
  const x = Math.floor((cx - r.left - (r.width - px.w * s) / 2) / s);
  const y = Math.floor((cy - r.top - (r.height - px.h * s) / 2) / s);
  if (x < 0 || y < 0 || x >= px.w || y >= px.h) return null;
  const d = px.ctx.getImageData(x, y, 1, 1).data;
  if (d[3] < 128) return null;  // transparent background of the reference
  return "#" + [d[0], d[1], d[2]].map((v) => v.toString(16).padStart(2, "0")).join("").toUpperCase();
}

function togglePipette(on = !ui.pipette) {
  const l = curLayer();
  if (on && !(l && l.refUrl)) on = false;
  ui.pipette = on;
  for (const el of document.querySelectorAll(".ref-pick")) el.classList.toggle("picking", on);
  $(".pal-add")?.classList.toggle("on", on);
  const prev = $("#pip-preview");
  if (prev && !on) prev.hidden = true;
  if (on) samplePickers();
}

function palNote(text) {
  ui.palNote = text;
  const n = $("#pal-note");
  if (n) n.textContent = text;
  clearTimeout(palNote.t);
  palNote.t = setTimeout(() => { ui.palNote = ""; const m = $("#pal-note"); if (m) m.textContent = ""; }, 3500);
}

function bindPipette() {
  const l = curLayer();
  if (!l) return;
  if (ui.pipette) samplePickers();
  for (const ref of document.querySelectorAll(".ref-pick")) bindRefPick(ref, l);
  bindPalAdd("pal");
  bindPalAdd("tpal");
  const pal = $("#palette");
  if (pal) pal.addEventListener("contextmenu", async (e) => {
    const sw = e.target.closest(".extra");
    if (!sw) return;
    e.preventDefault();
    if (!S.busy) await call("remove_palette_color", l.uid, sw.dataset.name);
  });
}

function bindRefPick(ref, l) {
  // the side reference and the reference in the stage (R): pipette into the palette
  const prev = $("#pip-preview");
  ref.addEventListener("mousemove", (e) => {
    if (!ui.pipette) return;
    const c = refColorAt(ref, e.clientX, e.clientY);
    prev.hidden = !c;
    if (c) { prev.style.background = c; prev.style.left = `${e.clientX + 14}px`; prev.style.top = `${e.clientY + 14}px`; }
  });
  ref.addEventListener("mouseleave", () => { prev.hidden = true; ui.overRef = false; });
  ref.addEventListener("mouseenter", () => { ui.overRef = true; });
  ref.addEventListener("click", async (e) => {
    if (!ui.pipette || ui.panned || S.busy) return;
    const c = refColorAt(ref, e.clientX, e.clientY);
    if (!c) return;
    const keep = e.shiftKey;
    if (!keep) togglePipette(false);
    const r = await call("add_palette_color", l.uid, c);
    if (r.ok) palNote(r.exists ? `Такий колір уже є: ${r.name}` : `Додано ${r.name}`);
  });
}

// ---------- right panel
function sideView() {
  const l = curLayer();
  if (!l) return `<aside class="side"></aside>`;
  const ref = l.refUrl
    ? `<div class="ref ref-pick zoomable ${ui.pipette ? "picking" : ""}" data-zoom="ref" id="ref"><div class="zoom"><img src="${l.refLabeledUrl || l.refUrl}"
         alt="Референс ${esc(l.name)}${l.refLabeledUrl ? " з підписами частин" : ""}"></div></div>
       <span class="ref-name">${esc(l.ref)} · <kbd>R</kbd> ${l.refLabeledUrl ? "без підписів " : ""}на весь кадр</span>
       ${refLabelsView(l)}
       <div class="pal-head"><span class="eyebrow">Палітра · ${l.palette.length}</span><span class="pal-note" id="pal-note">${esc(ui.palNote)}</span></div>
       <div class="palette" id="palette">${l.palette.map((p) => `<span class="${p.extra ? "extra" : ""}" data-name="${p.name}" style="background:${p.hex}"
         title="${p.name} ${p.hex}${p.extra ? " · додано вручну · права кнопка — видалити" : ""}"></span>`).join("")}${palAddView()}</div>`
    : `<div class="ref none"><span>Референс не вибрано</span><button class="btn primary" data-act="ref">Оберіть референс</button></div>`;
  return `<aside class="side"><div class="action" id="action">${actionView(l)}</div>
    <div class="side-head"><span class="eyebrow">Референс · ${esc(l.name)}</span>
    ${l.refUrl ? `<button class="link" data-act="ref" ${S.busy ? "disabled" : ""}>Змінити</button>` : ""}</div>
    ${ref}</aside>`;
}

function palAddView() {
  // "+": the pipette over the reference, or any colour by its code / from the system colour picker
  return `<div class="menu-wrap pal-add-wrap"><button class="pal-add ${ui.pipette ? "on" : ""}" data-act="menu" data-menu="pal-menu" aria-haspopup="menu"
      aria-expanded="false" title="Додати колір у палітру" aria-label="Додати колір у палітру" ${S.busy ? "disabled" : ""}>+</button>
    <div class="menu pal-menu" id="pal-menu" role="menu" hidden>
      <button role="menuitem" data-act="pipette"><span>Піпетка з референса</span><small>I · клік по референсу, Shift+клік — кілька</small></button>
      ${palCustomView("pal")}
    </div></div>`;
}

function palCustomView(at) {
  // any colour by its code or from the system colour picker; at: "pal" (palette "+" menu) or "tpal" (the dock's tool colours)
  return `<div class="pal-custom"><input type="color" id="${at}-color" value="${normHex(ui.palHex) || "#888888"}" title="Вибрати колір" aria-label="Вибрати колір">
    <input type="text" id="${at}-hex" value="${esc(ui.palHex)}" maxlength="7" spellcheck="false" autocomplete="off" placeholder="#RRGGBB" aria-label="Код кольору">
    <button class="btn sm primary" data-act="pal-add-hex" data-at="${at}" title="Додати колір у палітру">Додати</button></div>`;
}

function normHex(v) {
  // "#abc", "aabbcc", "#AABBCC" → "#AABBCC"; anything else → ""
  let h = String(v || "").trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(h)) h = h.split("").map((c) => c + c).join("");
  return /^[0-9a-f]{6}$/i.test(h) ? "#" + h.toUpperCase() : "";
}

async function addHexColor(at = "pal") {
  const l = curLayer(), inp = $(`#${at}-hex`), hex = normHex(inp ? inp.value : ui.palHex);
  if (!hex) { inp?.classList.add("bad"); inp?.focus(); return; }
  closeMenus();
  if (at === "tpal") { ui.toolPal = false; const p = $(".tpal"); if (p) p.hidden = true; }  // the new colour becomes the brush colour
  const r = await call("add_palette_color", l.uid, hex);
  if (!r.ok) return;
  palNote(r.exists ? `Такий колір уже є: ${r.name}` : `Додано ${r.name} ${hex}`);
  if (at === "tpal") {
    setColor(r.name);
    if (ui.tool === "select" || ui.tool === "picker") setTool("brush");
  }
}

function bindPalAdd(at = "pal") {
  const pick = $(`#${at}-color`), inp = $(`#${at}-hex`);
  if (!pick || !inp) return;
  pick.addEventListener("input", () => { ui.palHex = inp.value = pick.value.toUpperCase(); inp.classList.remove("bad"); });
  inp.addEventListener("input", () => {
    ui.palHex = inp.value;
    const h = normHex(inp.value);
    inp.classList.toggle("bad", !!inp.value.trim() && !h && inp.value.replace(/^#/, "").length >= 6);
    if (h) pick.value = h.toLowerCase();
  });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addHexColor(at); } });
}

function refLabelsView(l) {
  // the parts of the reference labelled by the anchor model: the follow keys are coloured by these labels
  const dis = S.busy ? "disabled" : "";
  if (S.busy === "Підписування референсу") return `<div class="ref-labels"><span class="spin" aria-hidden="true"></span><span>Модель підписує частини…</span></div>`;
  if (l.refLabeledUrl) return `<div class="ref-labels"><span>Підписано: ${l.refLabelsN}</span><span class="grow"></span>
    <button class="link" data-act="label-ref" ${dis} title="Підписати частини заново (LLM)">Заново</button>
    <button class="link" data-act="unlabel-ref" ${dis} title="Прибрати підписи: кадри за якорем знову підуть із зображеннями якоря">Прибрати</button></div>`;
  return `<div class="ref-labels"><button class="btn sm" data-act="label-ref" ${dis}
    title="Модель якоря назве частини персонажа на референсі. Кадри за якорем розфарбовуються за цими підписами — точніше й дешевше">Підписати частини (LLM)</button></div>`;
}

function actionView(l) {
  const busy = S.busy;
  if (busy && /Розфарбовування/.test(busy)) return progressBlock(l);
  const dis = busy ? "disabled" : "";
  const a = l.keys.find((k) => k.idx === l.anchorIdx);
  switch (l.stage) {
    case "missing":
      return `<b>Шар не знайдено в TVPaint</b><p>Його перейменували або видалили. Перескануйте проект.</p>
        <button class="btn" data-act="rescan" ${dis}>Пересканувати</button>`;
    case "export":
      return `<b>Ключі не виведено</b><p>Виведіть ключові кадри з TVPaint, щоб почати.</p>
        <button class="btn primary" data-act="export" ${dis}>Вивести ключі</button>`;
    case "ref":
      return `<b>Потрібен референс</b><p>Оберіть зображення персонажа в кольорі. З нього береться палітра.</p>
        <button class="btn primary" data-act="ref" ${dis}>Оберіть референс</button>`;
    case "anchor":
      return `<b>Якір: ключ #${a ? a.idx : "–"}</b><p>Якір розфарбовується першим і задає кольори для решти кадрів. Найкраще підходить кадр, де персонаж видно повністю. Щоб змінити якір, виберіть інший кадр.</p>
        <button class="btn primary" data-act="color-anchor" ${dis}>Розфарбувати якір · ~${money(S.est)}</button>`;
    case "approve":
      return `<b>Перевірте якір · ключ #${a.idx}</b><p>Клік по області — змінити колір. Затверджений якір задає кольори для решти кадрів.</p>
        <button class="btn primary" data-act="approve" ${dis}>Затвердити</button>${recolorLink(dis)}`;
    case "frames":
      return `<b>Кадри за якорем</b><p>Якір #${a.idx} затверджено. ${l.followsN} унікальних кадр(ів) розфарбуються за ним, повтори отримають колір автоматично.</p>
        <button class="btn primary" data-act="follows" ${dis}>Розфарбувати ${l.followsN} кадр(ів) · ~${money(l.followsN * S.estFollow)}</button>${recolorLink(dis)}`;
    case "ready":
      return `<b>Усі кадри готові</b><p>${l.total} кадр(ів) розфарбовано. Можна вставляти в TVPaint.</p>
        <button class="btn primary" data-act="view" data-view="insert">До вставки</button>${refollowLink(l, dis)}${recolorLink(dis)}`;
    case "done":
      return `<b>Вставлено в TVPaint</b><p>Шар ${esc(l.name)}_color створено під лайном.</p>
        <button class="btn primary" data-act="view" data-view="insert">Вставити знову</button>${refollowLink(l, dis)}${recolorLink(dis)}`;
  }
  return "";
}

function refollowLink(l, dis) {
  // all keys again with the approved anchor (e.g. another model in Settings)
  return l.keysN ? `<button class="link" data-act="refollow" ${dis}>Перефарбувати всі кадри · ${l.keysN}</button>` : "";
}

function recolorLink(dis) {
  // the anchor can be recoloured at any stage, also after approval
  return `<button class="link" data-act="recolor-anchor" ${dis} title="Розфарбувати якір заново: затвердження буде знято, решту кадрів потім розфарбуєте за новим якорем">Перефарбувати якір</button>`;
}

function progressBlock(l) {
  const p = S.progress || {};
  const pct = p.total ? Math.round((100 * p.n) / p.total) : 0;
  return `<div style="display:flex;justify-content:space-between;align-items:baseline"><b>${esc(S.busy)}</b><span style="color:var(--fg2)">${p.total ? `${p.n} з ${p.total}` : ""}</span></div>
    <div class="bar"><div id="pb-in" style="width:${pct}%"></div></div>
    <div class="kv"><span>Сесія ${money(S.cost.session)}</span><span id="pb-msg"></span></div>
    <button class="btn" data-act="stop">Зупинити</button>`;
}

function renderProgressBlock() {
  const el = $("#pb-in");
  if (!el) return;
  const p = S.progress || {};
  el.style.width = `${p.total ? Math.round((100 * p.n) / p.total) : 0}%`;
}

// ---------- insert view
function insertView() {
  const ls = layers();
  if (!ui.insertSel) ui.insertSel = new Set(ls.filter((l) => l.stage === "ready").map((l) => l.uid));
  if (!ui.save) ui.save = S.saveNew ? "new" : "overwrite";
  const ok = (l) => l.stage === "ready" || l.stage === "done";
  const rows = (S.clips || []).map((c) => `<div style="display:flex;align-items:baseline;gap:8px;padding:14px 0 6px"><b style="font-weight:600">${esc(c.name)}</b><span style="color:var(--muted);font-size:12px">кліп ${c.pos}</span></div>
    ${c.layers.map((l) => `<label class="irow ${ok(l) ? "" : "off"}"><input type="checkbox" data-act="isel" data-uid="${esc(l.uid)}" ${ui.insertSel.has(l.uid) ? "checked" : ""} ${ok(l) ? "" : "disabled"}>
      <span class="swatch" style="${l.swatch ? `background:${l.swatch}` : ""}"></span><b>${esc(l.name)}</b>
      <small class="${l.stage === "ready" ? "tone-ok" : ""}">${l.stage === "ready" ? `усі ${l.total} кадр(ів)` : l.stage === "done" ? "уже вставлено · вставити знову" : `розфарбовано ${l.done} з ${l.total}`}</small></label>`).join("")}`).join("");
  const n = ui.insertSel.size;
  const radio = (v, text) => `<label class="check"><input type="radio" name="save" value="${v}" data-act="save" ${ui.save === v ? "checked" : ""}><span>${text}</span></label>`;
  const btn = ui.save === "none" ? `Вставити ${n} шар(и)` : `Вставити ${n} шар(и) і зберегти`;
  return `<main class="insert"><div class="insert-box">
    <div style="display:flex;flex-direction:column;gap:8px"><h1>Вставка в TVPaint</h1>
    <p>Для кожного шару буде створено шар <span style="color:var(--fg)">&lt;персонаж&gt;_color</span> одразу під лайном. Таймінги беруться з оригіналу. Попередній _color-шар буде замінено.</p></div>
    <div>${rows}</div>
    <fieldset><legend class="eyebrow">Після вставки</legend>
      ${radio("new", `Зберегти як нову версію <span style="color:var(--fg2)">${esc(S.project.nextVersion)}</span>`)}
      ${radio("overwrite", `Перезаписати ${esc(S.project.name)}`)}
      ${radio("none", `Не зберігати <span style="color:var(--muted)">(перегляну і збережу в TVPaint сам)</span>`)}
    </fieldset>
    <div style="display:flex;align-items:center;gap:16px">
      <button class="btn primary" data-act="inject" ${n && !S.busy ? "" : "disabled"} style="height:42px;padding:0 22px">${S.busy === "Вставка в TVPaint" ? "Вставка…" : btn}</button>
      <span style="color:var(--muted);font-size:12px">Вставка займає ~1 с. Збереження великого проекту може тривати хвилину.</span></div>
  </div></main>`;
}

// ------------------------------------------------------------------ interactions
function bindCommon() {
  $("#app").onclick = onClick;
}

function bindMain() {
  bindCommon();
  const fr = $("#frame");
  if (fr && fr.classList.contains("clickable")) {
    fr.addEventListener("click", onFrameClick);
    fr.addEventListener("mousedown", onDrawStart);  // bound before the pan handler: brush/eraser drags draw
  }
  mountEditor();
  const ts = $("#t-size");
  if (ts) ts.addEventListener("input", () => setSize(+ts.value));
  for (const img of document.querySelectorAll("#stage img")) img.addEventListener("load", fitFrames, { once: true });
  for (const el of document.querySelectorAll("#app [data-zoom]")) bindZoomable(el.dataset.zoom);
  bindPipette();
  fitFrames();
  applyAllZooms();
  const strip = $("#strip");
  if (strip) strip.addEventListener("contextmenu", keyMenu);
  const sel = document.querySelector(".kf.sel");
  if (sel) sel.scrollIntoView({ block: "nearest", inline: "nearest" });
  for (const inp of document.querySelectorAll('[data-act="isel"]')) inp.addEventListener("change", () => {
    if (inp.checked) ui.insertSel.add(inp.dataset.uid); else ui.insertSel.delete(inp.dataset.uid);
    render();
  });
  for (const r of document.querySelectorAll('[data-act="save"]')) r.addEventListener("change", () => { ui.save = r.value; render(); });
}
window.addEventListener("resize", fitFrames);

async function onClick(e) {
  const b = e.target.closest("[data-act]");
  if (!b || b.disabled) return;
  const act = b.dataset.act;
  const l = curLayer();
  if (act !== "menu" && act !== "pal-add-hex") closeMenus();  // a wrong colour code keeps its menu open
  switch (act) {
    case "menu": toggleMenu(b.dataset.menu); break;
    case "connect": doConnect(); break;
    case "settings": openSettings(); break;
    case "folder": call("open_folder"); break;
    case "log": toggleLog(); break;
    case "view": ui.view = b.dataset.view; closePopover(); render(); break;
    case "layer": ui.uid = b.dataset.uid; ui.idx = null; ui.view = "main"; ui.zooms = {}; ui.showAnchor = false; ui.showRef = false; ui.pipette = false; closePopover(); render(); break;
    case "zoom": zoomCmd(b.dataset.z); break;
    case "lines": toggleLines(); break;
    case "pipette": togglePipette(); break;
    case "pal-add-hex": addHexColor(b.dataset.at); break;
    case "tool": setTool(b.dataset.tool); break;
    case "help": toggleHelp(); break;
    case "dups": {  // from what is shown (a selected repeat unfolds them); folding moves the selection to its original
      ui.showDups = b.getAttribute("aria-expanded") !== "true";
      store.set("tintr.showDups", ui.showDups ? "1" : "0");
      const k = curKey();
      if (!ui.showDups && k && k.dup != null) ui.idx = k.dup;
      render();
      break;
    }
    case "tools-min": ui.toolsMin = !ui.toolsMin; ui.toolPal = false; store.set("tintr.toolsMin", ui.toolsMin ? "1" : "0"); render(); break;
    case "tpal": ui.toolPal = !ui.toolPal; $(".tpal").hidden = !ui.toolPal; break;
    case "tcolor":
      setColor(b.dataset.c); ui.toolPal = false; $(".tpal").hidden = true;
      if (ui.tool === "select" || ui.tool === "picker") setTool("brush");
      break;
    case "undo": undoRedo(false); break;
    case "redo": undoRedo(true); break;
    case "key": ui.idx = +b.dataset.idx; ui.showAnchor = false; ui.showRef = false; closePopover(); render(); break;
    case "mode": ui.mode = b.dataset.mode; closePopover(); render(); break;
    case "rescan": call("connect", null, true); break;
    case "export": call("export"); break;
    case "ref": REMOTE ? uploadRef(l.uid) : call("pick_ref", l.uid); break;
    case "label-ref":
      if (await confirmCost(`Модель якоря підпише частини на референсі шару ${l.name} (один запит).`
        + (l.refLabeledUrl ? " Поточні підписи буде замінено." : ""), S.estLabel)) call("label_ref", l.uid);
      break;
    case "unlabel-ref": call("clear_ref_labels", l.uid); break;
    case "set-anchor": { const m = master(l, curKey()); call("set_anchor", l.uid, m.idx); break; }
    case "color-anchor":
      if (await confirmCost(`Gemini розфарбує якір шару ${l.name}.`, S.est)) call("colorize_anchor", l.uid, false);
      break;
    case "recolor-anchor":
      if (await confirmCost(`Gemini розфарбує якір шару ${l.name} заново. Поточні кольори якоря буде замінено`
        + (l.anchorApproved ? ", затвердження знято. Після нового затвердження решту кадрів треба буде розфарбувати знову." : "."), S.est)) call("colorize_anchor", l.uid, true);
      break;
    case "approve": call("approve_anchor", l.uid); break;
    case "follows":
      if (await confirmCost(`Буде розфарбовано ${l.followsN} кадр(ів) шару ${l.name} за затвердженим якорем.`, l.followsN * S.estFollow)) call("colorize_follows", l.uid);
      break;
    case "refollow":
      if (await confirmCost(`Буде заново розфарбовано всі ${l.keysN} кадр(ів) шару ${l.name} за затвердженим якорем. Поточні кольори кадрів буде замінено.`, l.keysN * S.estFollow)) call("colorize_follows", l.uid, true);
      break;
    case "all-anchors":
      if (await confirmCost(`Gemini паралельно розфарбує якорі ${S.allAnchors} шар(ів) у всіх кліпах. Шари без референсу буде пропущено.`, S.allAnchors * S.est)) call("colorize_all_anchors");
      break;
    case "all-follows":
      if (await confirmCost(`Паралельно буде розфарбовано ${S.allFollows} кадр(ів) усіх шарів із затвердженими якорями.`, S.allFollows * S.estFollow)) call("colorize_follows", null);
      break;
    case "stop": call("stop"); break;
    case "inject": call("inject", [...ui.insertSel], ui.save); break;
  }
}

// ---------- drawing tools over the coloured frame: brush, eraser, bucket, picker
// The canvas shows fill → erase → paint at the frame's full resolution inside the zoom layer, so zoom and pan
// work as for the images. Strokes are hard-edged (flat colours for TVPaint) and saved as whole layers.
const TOOLS = [["select", "Вибір", "V", "клік по області — колір з палітри"], ["brush", "Пензель", "B", "Alt+клік — взяти колір з кадру"],
  ["eraser", "Гумка", "E", "стирає до прозорого"], ["bucket", "Заливка", "G", "залити область поточним кольором"],
  ["picker", "Піпетка", "I", "взяти колір з кадру"]];
const editors = {};  // "uid:masterIdx" → editor (kept across renders: the canvas node is moved into the new DOM)
// edit history per key, kept apart from the editor: a region colour change reloads the editor, the history stays.
// Entries: strokes {box, bp, be, ap, ae} (paint/erase before/after in a box) and region colours
// {region, from, to, bucket, strokes} (the bucket also cleared strokes in that region: a box of them before/after).
const history = {};
const hist = (id) => history[id] || (history[id] = { undo: [], redo: [] });
const keyId = (l, k) => `${l.uid}:${master(l, k).idx}`;

function pushHistory(id, step) {
  const h = hist(id);
  h.undo.push(step);
  if (h.undo.length > 50) h.undo.shift();
  h.redo = [];
}
const drawing = () => ui.mode === "color" && (ui.tool === "brush" || ui.tool === "eraser") && !ui.showAnchor && !ui.showRef;

function toolColor(l) {
  const p = l.palette.find((c) => c.name === ui.color) || l.palette[0];
  if (p) ui.color = p.name;
  return p || { name: "", hex: "#000000" };
}

function toolsView(l) {
  // the dock's drawing group; minimized it keeps only the active tool and the colour (shortcuts still work)
  const c = toolColor(l);
  const dis = S.busy ? "disabled" : "";
  const tool = ([id, n, key, tip]) => `<button class="dbtn ${ui.tool === id ? "on" : ""}" data-act="tool" data-tool="${id}" ${dis}
    title="${n} (${key}): ${tip}" aria-label="${n}" aria-pressed="${ui.tool === id}">${ICON[id]}</button>`;
  const pal = l.palette.map((p) => `<button style="background:${p.hex}" class="${p.name === c.name ? "cur" : ""}" data-act="tcolor" data-c="${p.name}" title="${p.name} ${p.hex}"></button>`).join("");
  const color = `<div class="dcolor"><button class="tcolor" data-act="tpal" style="background:${c.hex}" title="Колір пензля і заливки: ${c.name}" aria-label="Колір ${c.name}" ${dis}></button>
    <div class="tpal" ${ui.toolPal ? "" : "hidden"}><div class="pal-grid">${pal}</div>${palCustomView("tpal")}</div></div>`;
  if (ui.toolsMin) {
    const t = TOOLS.find(([id]) => id === ui.tool) || TOOLS[0];
    return `<div class="dgroup" id="tmin-tool">${tool(t)}</div>${color}
      <button class="dbtn dmin" data-act="tools-min" title="Розгорнути інструменти" aria-label="Розгорнути інструменти" aria-expanded="false">›</button>`;
  }
  return `<div class="dgroup">${TOOLS.map(tool).join("")}</div>${color}
    <label class="dsize" title="Розмір пензля в пікселях кадру ([ і ])"><input type="range" id="t-size" min="1" max="200" value="${ui.size}" ${dis} aria-label="Розмір пензля"><span id="t-size-v">${ui.size}</span></label>
    <div class="dgroup"><button class="dbtn" data-act="undo" title="Скасувати (Ctrl+Z)" aria-label="Скасувати" ${dis}>${ICON.undo}</button>
      <button class="dbtn" data-act="redo" title="Повторити (Ctrl+Shift+Z)" aria-label="Повторити" ${dis}>${ICON.redo}</button></div>
    <button class="dbtn dmin" data-act="tools-min" title="Згорнути інструменти" aria-label="Згорнути інструменти" aria-expanded="true">‹</button>`;
}

function setTool(t) {
  if (t === ui.tool) return;
  if (t === "picker") ui.prevTool = ui.tool === "select" ? "brush" : ui.tool;
  ui.tool = t;
  closePopover();
  for (const b of document.querySelectorAll('.dock [data-act="tool"]')) {
    b.classList.toggle("on", b.dataset.tool === t); b.setAttribute("aria-pressed", String(b.dataset.tool === t));
  }
  const fr = $("#frame");
  if (fr && fr.classList.contains("clickable") && ui.mode === "color") fr.className = fr.className.replace(/tool-\w+/, `tool-${t}`);
  const mt = $("#tmin-tool"), tt = TOOLS.find(([id]) => id === t);
  if (mt && tt) {  // minimized dock: show the newly active tool
    const b = mt.querySelector("button");
    b.dataset.tool = t; b.innerHTML = ICON[t]; b.title = `${tt[1]} (${tt[2]}): ${tt[3]}`; b.setAttribute("aria-label", tt[1]);
  }
  updateCursor();
}

function setSize(v) {
  ui.size = Math.max(1, Math.min(200, Math.round(v)));
  const inp = $("#t-size"), lbl = $("#t-size-v");
  if (inp) inp.value = ui.size;
  if (lbl) lbl.textContent = ui.size;
  updateCursor();
}

function setColor(name) {
  ui.color = name;
  const l = curLayer();
  if (!l) return;
  const c = toolColor(l), b = $(".dock .tcolor");
  if (b) { b.style.background = c.hex; b.title = `Колір пензля і заливки: ${c.name}`; }
  for (const x of document.querySelectorAll('.tpal [data-act="tcolor"]')) x.classList.toggle("cur", x.dataset.c === c.name);
}

function framePoint(cx, cy) {
  const r = $("#zoom").getBoundingClientRect();  // zoomed/panned image box
  return { x: ((cx - r.left) / r.width) * S.project.w, y: ((cy - r.top) / r.height) * S.project.h };
}

function editorFor(l, k) {
  const m = l && k ? master(l, k) : null;
  if (!m || !m.colored) return null;
  const id = `${l.uid}:${m.idx}`, urls = [m.fill, m.paint, m.erase].join("|");
  let ed = editors[id];
  // changed elsewhere (region colour, bucket, refill): reload, unless strokes are still being sent
  if (ed && ed.urls !== urls && !ed.dirty && !ed.saving) { delete editors[id]; ed = null; }
  if (!ed) {
    for (const x of Object.keys(editors)) if (!editors[x].dirty && !editors[x].saving) delete editors[x];  // keep memory small
    ed = editors[id] = newEditor(id, l.uid, m, urls);
  }
  return ed;
}

function loadPixels(url, W, H) {
  if (!url) return Promise.resolve(null);
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas"); c.width = W; c.height = H;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, W, H);
      resolve(ctx.getImageData(0, 0, W, H).data);
    };
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

function newEditor(id, uid, m, urls) {
  const W = S.project.w, H = S.project.h;
  const cv = document.createElement("canvas");
  cv.width = W; cv.height = H; cv.className = "edit-canvas";
  const ed = { id, uid, idx: m.idx, W, H, cv, ctx: cv.getContext("2d"), urls, ready: false, dirty: false, saving: null };
  ed.loaded = Promise.all([m.fill, m.paint, m.erase].map((u) => loadPixels(u, W, H))).then(([f, p, e]) => {
    if (!f) return;
    ed.fill = f;
    ed.paint = p || new Uint8ClampedArray(W * H * 4);
    ed.erase = e || new Uint8ClampedArray(W * H * 4);
    ed.comp = ed.ctx.createImageData(W, H);
    composite(ed, 0, 0, W, H);
    ed.ready = true;
    mountEditor();
  });
  return ed;
}

function mountEditor() {
  const slot = document.querySelector(".edit-slot");
  const ed = slot && editors[slot.dataset.ed];
  if (ed && ed.ready && slot.firstElementChild !== ed.cv) slot.replaceChildren(ed.cv);
}

function composite(ed, x, y, w, h) {
  // paint over everything, erased pixels transparent, the flat fill elsewhere
  const { W, fill, paint, erase } = ed, out = ed.comp.data;
  for (let j = y; j < y + h; j++) {
    for (let i = (j * W + x) * 4, end = (j * W + x + w) * 4; i < end; i += 4) {
      if (paint[i + 3] >= 128) { out[i] = paint[i]; out[i + 1] = paint[i + 1]; out[i + 2] = paint[i + 2]; out[i + 3] = 255; }
      else if (erase[i + 3] >= 128) { out[i] = out[i + 1] = out[i + 2] = out[i + 3] = 0; }
      else { out[i] = fill[i]; out[i + 1] = fill[i + 1]; out[i + 2] = fill[i + 2]; out[i + 3] = fill[i + 3]; }
    }
  }
  ed.ctx.putImageData(ed.comp, 0, 0, x, y, w, h);
}

function stamp(ed, cx, cy, st) {
  // a hard circle of diameter ui.size (no anti-aliasing) into the paint / erase layers
  const r = ui.size / 2, W = ed.W, H = ed.H;
  const x0 = Math.max(0, Math.floor(cx - r)), x1 = Math.min(W - 1, Math.ceil(cx + r));
  const y0 = Math.max(0, Math.floor(cy - r)), y1 = Math.min(H - 1, Math.ceil(cy + r));
  if (x0 > x1 || y0 > y1) return;
  const r2 = ui.size <= 1 ? 0.5 : r * r, [R, G, B] = st.rgb, er = st.tool === "eraser";
  for (let y = y0; y <= y1; y++) {
    for (let x = x0; x <= x1; x++) {
      const dx = x + 0.5 - cx, dy = y + 0.5 - cy;
      if (dx * dx + dy * dy > r2) continue;
      const i = (y * W + x) * 4;
      if (er) { ed.paint[i + 3] = 0; ed.erase[i + 3] = 255; }
      else { ed.paint[i] = R; ed.paint[i + 1] = G; ed.paint[i + 2] = B; ed.paint[i + 3] = 255; ed.erase[i + 3] = 0; }
    }
  }
  st.box = st.box ? [Math.min(st.box[0], x0), Math.min(st.box[1], y0), Math.max(st.box[2], x1), Math.max(st.box[3], y1)] : [x0, y0, x1, y1];
  composite(ed, x0, y0, x1 - x0 + 1, y1 - y0 + 1);
}

function stampLine(ed, a, b, st) {
  const d = Math.hypot(b.x - a.x, b.y - a.y), step = Math.max(0.5, ui.size / 4), n = Math.max(1, Math.ceil(d / step));
  for (let t = 1; t <= n; t++) stamp(ed, a.x + ((b.x - a.x) * t) / n, a.y + ((b.y - a.y) * t) / n, st);
}

function rect(arr, W, [x0, y0, x1, y1]) {
  const w = x1 - x0 + 1, out = new Uint8ClampedArray(w * (y1 - y0 + 1) * 4);
  for (let y = y0; y <= y1; y++) out.set(arr.subarray((y * W + x0) * 4, (y * W + x1 + 1) * 4), (y - y0) * w * 4);
  return out;
}

function putRect(arr, W, [x0, y0, x1, y1], data) {
  const w = x1 - x0 + 1;
  for (let y = y0; y <= y1; y++) arr.set(data.subarray((y - y0) * w * 4, (y - y0 + 1) * w * 4), (y * W + x0) * 4);
}

function onDrawStart(e) {
  if (e.button !== 0 || ui.space || !drawing() || S.busy) return;
  const l = curLayer(), ed = editorFor(l, curKey());
  if (!ed || !ed.ready) return;
  e.preventDefault();
  e.stopImmediatePropagation();  // not a pan
  closePopover();
  if (e.altKey) { pickColorAt(e.clientX, e.clientY, true); return; }
  const c = toolColor(l);
  const st = { tool: ui.tool, rgb: [1, 3, 5].map((i) => parseInt(c.hex.slice(i, i + 2), 16)), box: null,
    p0: ed.paint.slice(), e0: ed.erase.slice() };  // before the stroke, for undo (only the touched box is kept)
  let last = framePoint(e.clientX, e.clientY);
  stamp(ed, last.x, last.y, st);
  const move = (ev) => {
    const p = framePoint(ev.clientX, ev.clientY);
    stampLine(ed, last, p, st);
    last = p;
  };
  const up = () => {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    if (!st.box) return;
    pushHistory(ed.id, { box: st.box, bp: rect(st.p0, ed.W, st.box), be: rect(st.e0, ed.W, st.box), ap: rect(ed.paint, ed.W, st.box), ae: rect(ed.erase, ed.W, st.box) });
    scheduleSave(ed);
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

function applyStrokes(ed, s, after) {
  putRect(ed.paint, ed.W, s.box, after ? s.ap : s.bp);
  putRect(ed.erase, ed.W, s.box, after ? s.ae : s.be);
  const [x0, y0, x1, y1] = s.box;
  composite(ed, x0, y0, x1 - x0 + 1, y1 - y0 + 1);
  scheduleSave(ed);
}

async function undoRedo(redo) {
  const l = curLayer(), k = curKey();
  if (!l || !k || ui.view !== "main" || ui.showAnchor || ui.showRef || S.busy || ui.undoing) return;
  const id = keyId(l, k), h = hist(id);
  const step = (redo ? h.redo : h.undo)[(redo ? h.redo : h.undo).length - 1];
  if (!step) return;
  ui.undoing = true;
  try {
    if (step.region == null) {  // strokes: only where the colour layer is shown
      const ed = ui.mode === "color" ? editors[id] : null;
      if (!ed || !ed.ready) return;
      applyStrokes(ed, step, redo);
    } else {  // a region colour (V or the bucket): back on the server, the bucket's strokes back here
      closePopover();
      await flushPaint();
      const r = redo && step.bucket ? await call("fill_region", l.uid, step.idx, step.region, step.to)
        : await call("set_region_color", l.uid, step.idx, step.region, redo ? step.to : step.from);
      if (r.ok === false || !r.state) return;
      if (step.bucket && step.strokes && !redo) {
        const ed = editors[id];
        if (ed) { await ed.loaded; if (ed.ready) applyStrokes(ed, step.strokes, false); }
      }
    }
    (redo ? h.redo : h.undo).pop();
    (redo ? h.undo : h.redo).push(step);
  } finally {
    ui.undoing = false;
  }
}

function changedBox(a, b, W) {
  // the box where two RGBA layers differ (alpha decides), or null
  let x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
  for (let i = 3; i < a.length; i += 4) {
    if ((a[i] >= 128) === (b[i] >= 128) && (a[i] < 128 || (a[i - 3] === b[i - 3] && a[i - 2] === b[i - 2] && a[i - 1] === b[i - 1]))) continue;
    const p = (i - 3) / 4, x = p % W, y = (p - x) / W;
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  return x1 < 0 ? null : [x0, y0, x1, y1];
}

async function bucketFill(l, k, res, color) {
  // the server clears strokes inside the region: remember them (a box) so Ctrl+Z brings them back
  const id = keyId(l, k), before = editors[id] && editors[id].ready ? editors[id] : null;
  const r = await call("fill_region", l.uid, k.idx, res.region, color);
  if (r.ok === false || !r.state) return;
  const step = { region: res.region, idx: k.idx, from: res.color || "BG", to: color, bucket: true, strokes: null };
  const after = editors[id];
  if (before && after && after !== before) {
    await after.loaded;
    if (after.ready) {
      const bp = changedBox(before.paint, after.paint, before.W), be = changedBox(before.erase, after.erase, before.W);
      const box = bp && be ? [Math.min(bp[0], be[0]), Math.min(bp[1], be[1]), Math.max(bp[2], be[2]), Math.max(bp[3], be[3])] : bp || be;
      if (box) step.strokes = { box, bp: rect(before.paint, before.W, box), be: rect(before.erase, before.W, box), ap: rect(after.paint, after.W, box), ae: rect(after.erase, after.W, box) };
    }
  }
  pushHistory(id, step);
}

function scheduleSave(ed) {
  ed.dirty = true;
  clearTimeout(ed.timer);
  ed.timer = setTimeout(() => savePaint(ed), 400);
}

function layerPng(ed, arr) {
  let any = false;
  for (let i = 3; i < arr.length; i += 4) if (arr[i] >= 128) { any = true; break; }
  if (!any) return "";
  const c = document.createElement("canvas"); c.width = ed.W; c.height = ed.H;
  const ctx = c.getContext("2d");
  const d = ctx.createImageData(ed.W, ed.H); d.data.set(arr);
  ctx.putImageData(d, 0, 0);
  return c.toDataURL("image/png").split(",")[1];
}

async function savePaint(ed) {
  // one request at a time; strokes made meanwhile go in the next one
  clearTimeout(ed.timer);
  while (ed.saving) await ed.saving;
  if (!ed.dirty) return;
  ed.dirty = false;
  ed.saving = (async () => {
    let r = {};
    try { r = await api().save_paint(ed.uid, ed.idx, layerPng(ed, ed.paint), layerPng(ed, ed.erase)); }
    catch (e) { r = { ok: false, error: String(e) }; }
    if (r.ok === false) { ed.dirty = true; modal(errorDialog("Мазки не збережено", r.error || "")); }
    if (r.state) {  // our own save: the new file URLs show what this editor already has
      const l = (r.state.clips || []).flatMap((c) => c.layers).find((x) => x.uid === ed.uid);
      const m = l && l.keys.find((x) => x.idx === ed.idx);
      if (m) ed.urls = [m.fill, m.paint, m.erase].join("|");
    }
    ed.saving = null;
    if (r.state) setState(r.state);
  })();
  return ed.saving;
}

async function flushPaint() {
  for (const ed of Object.values(editors)) {
    if (ed.dirty) await savePaint(ed);
    while (ed.saving) await ed.saving;
  }
}

function pickColorAt(cx, cy, keepTool = false) {
  // colour under the cursor → the nearest palette colour (brush and bucket paint with palette colours only)
  const l = curLayer(), ed = editorFor(l, curKey());
  if (!ed || !ed.ready) return;
  const { x, y } = framePoint(cx, cy);
  const xi = Math.floor(x), yi = Math.floor(y);
  if (xi < 0 || yi < 0 || xi >= ed.W || yi >= ed.H) return;
  const d = ed.comp.data, i = (yi * ed.W + xi) * 4;
  if (d[i + 3] < 128) return;  // transparent
  let best = null, bd = Infinity;
  for (const p of l.palette) {
    const dist = [1, 3, 5].reduce((acc, j, n) => acc + (parseInt(p.hex.slice(j, j + 2), 16) - d[i + n]) ** 2, 0);
    if (dist < bd) { bd = dist; best = p; }
  }
  if (best) setColor(best.name);
  if (!keepTool && ui.tool === "picker") setTool(ui.prevTool || "brush");
}

// brush outline under the cursor, sized by the zoom
let lastMouse = null;
function updateCursor() {
  const cur = $("#brush-cursor"), fr = $("#frame");
  if (!cur) return;
  const over = lastMouse && fr && fr.contains(document.elementFromPoint(lastMouse.x, lastMouse.y));
  const show = !!(drawing() && over && fr.classList.contains("clickable") && !ui.space);
  cur.hidden = !show;
  if (!show) return;
  const px = Math.max(3, (ui.size * fr.clientWidth * zstate("main").s) / S.project.w);
  cur.style.width = cur.style.height = `${px}px`;
  cur.style.left = `${lastMouse.x}px`; cur.style.top = `${lastMouse.y}px`;
  cur.classList.toggle("erase", ui.tool === "eraser");
}
document.addEventListener("mousemove", (e) => { lastMouse = { x: e.clientX, y: e.clientY }; updateCursor(); });
document.addEventListener("wheel", () => requestAnimationFrame(updateCursor), { passive: true });

// ---------- shortcut hint: a "?" in the corner of the stage, hidden by default (the choice is remembered)
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode etc. */ } },
};
ui.help = store.get("tintr.help") === "1";
ui.toolsMin = store.get("tintr.toolsMin") === "1";
ui.showDups = store.get("tintr.showDups") === "1";  // repeats in the strip: folded unless asked

const SHORTCUTS = [
  ["← →", "попередній / наступний ключ"], ["A", "якір замість ключа"], ["R", "референс замість ключа"], ["L", "лайн увімк. / вимк."],
  ["F або 0", "вписати (100%)"], ["+ −", "масштаб; колесо — до курсора"], ["Пробіл+тягнути", "рухати кадр"],
  ["V B E G I", "вибір · пензель · гумка · заливка · піпетка"], ["Alt+клік", "колір з кадру (пензель)"],
  ["[ ]", "розмір пензля"], ["Ctrl+Z", "скасувати; Ctrl+Shift+Z — повторити"],
  ["I над референсом", "піпетка в палітру"], ["Esc", "закрити / вийти"], ["H або ?", "ця підказка"],
];

function helpView() {
  const rows = SHORTCUTS.map(([k, t]) => `<kbd>${esc(k)}</kbd><span>${esc(t)}</span>`).join("");
  return `<div class="help" id="help" ${ui.help ? "" : "hidden"}>${rows}</div>`;
}

function toggleHelp(on = !ui.help) {
  ui.help = on;
  store.set("tintr.help", on ? "1" : "0");
  const h = $("#help"), b = $(".help-btn");
  if (h) h.hidden = !on;
  if (b) { b.classList.toggle("on", on); b.setAttribute("aria-expanded", String(on)); }
}

// ---------- region popover
async function onFrameClick(e) {
  if (ui.panned) { ui.panned = false; return; }  // the press was a pan drag
  if (S.busy) return;
  const l = curLayer(), k = curKey();
  const tool = ui.mode === "color" ? ui.tool : "select";
  if (tool === "brush" || tool === "eraser") return;  // strokes are handled on mousedown
  if (tool === "picker") { pickColorAt(e.clientX, e.clientY); return; }
  const { x, y } = framePoint(e.clientX, e.clientY);
  await flushPaint();  // region edits redraw the fill: send pending strokes first
  if (tool === "bucket") {
    const res = await api().region_at(l.uid, k.idx, x, y);
    if (res && res.ok && res.editable) await bucketFill(l, k, res, toolColor(l).name);
    return;
  }
  const res = await api().region_at(l.uid, k.idx, x, y);
  if (!res || !res.ok) { closePopover(); return; }
  const pop = $("#popover");
  const cur = String(res.color || "").toUpperCase();
  const sw = l.palette.map((p) => `<button style="background:${p.hex}" class="${cur === p.name ? "cur" : ""}" title="${p.name}" aria-label="${p.name}" data-c="${p.name}"></button>`).join("");
  pop.innerHTML = `<span class="t">Область ${res.region}${res.part ? ` · <b>${esc(res.part)}</b>` : ""}</span>
    ${res.editable ? `<div class="pal-grid">${sw}</div><button class="pal-bg ${cur === "BG" ? "cur" : ""}" data-c="BG">BG — прозорий фон</button>`
      : '<span class="t">Спершу розфарбуйте цей кадр.</span>'}`;
  pop.hidden = false;
  const px = Math.min(window.innerWidth - 250, e.clientX + 12), py = Math.min(window.innerHeight - pop.offsetHeight - 12, e.clientY + 12);
  pop.style.left = `${px}px`; pop.style.top = `${py}px`;
  pop.onclick = async (ev) => {
    const c = ev.target.closest("[data-c]");
    if (!c) return;
    closePopover();
    const to = c.dataset.c, from = cur || "BG";
    if (to === from) return;
    const r = await call("set_region_color", l.uid, k.idx, res.region, to);
    if (r.ok !== false && r.state) pushHistory(keyId(l, k), { region: res.region, idx: k.idx, from, to, bucket: false });
  };
}
function closePopover() { $("#popover").hidden = true; }
document.addEventListener("mousedown", (e) => {
  if (!$("#popover").hidden && !e.target.closest("#popover") && !e.target.closest("#frame") && e.button !== 2) closePopover();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMenus(); closePopover(); closeModal(); toggleAnchor(false); toggleRef(false); togglePipette(false); if (ui.help) toggleHelp(false); return; }
  if ((e.target.closest && e.target.closest("input, textarea, select")) || !$("#modal").hidden) return;
  if ((e.ctrlKey || e.metaKey) && e.code === "KeyZ") { e.preventDefault(); undoRedo(e.shiftKey); return; }  // strokes and region colours
  if ((e.ctrlKey || e.metaKey) && e.code === "KeyY") { e.preventDefault(); undoRedo(true); return; }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  if (e.code === "Space") { e.preventDefault(); if (!ui.space) { ui.space = true; $("#frame")?.classList.add("space-pan"); updateCursor(); } return; }
  if (e.key === "+" || e.key === "=") zoomCmd("in");
  else if (e.key === "-") zoomCmd("out");
  else if (e.key === "0" || e.code === "KeyF") zoomCmd("fit");  // F: any keyboard layout
  else if (e.code === "KeyA") toggleAnchor();  // any keyboard layout
  else if (e.code === "KeyR") toggleRef();
  else if (e.code === "KeyL") toggleLines();
  else if (e.code === "KeyH" || e.key === "?") toggleHelp();
  else if (e.code === "KeyI") { if (ui.overRef || ui.showRef) togglePipette(); else setTool("picker"); }  // over the reference: into the palette
  else if (e.code === "KeyV") setTool("select");
  else if (e.code === "KeyB") setTool("brush");
  else if (e.code === "KeyE") setTool("eraser");
  else if (e.code === "KeyG") setTool("bucket");
  else if (e.code === "BracketLeft") setSize(ui.size > 10 ? ui.size / 1.25 : ui.size - 1);
  else if (e.code === "BracketRight") setSize(ui.size >= 10 ? ui.size * 1.25 : ui.size + 1);
  else if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); stepKey(e.key === "ArrowRight" ? 1 : -1); }
});

document.addEventListener("keyup", (e) => {
  if (e.code === "Space") { ui.space = false; $("#frame")?.classList.remove("space-pan"); updateCursor(); }
});

function stepKey(dir) {
  // previous / next key in the order of the bottom strip (anchor, unique keys, repeats)
  const l = curLayer();
  if (!l || ui.view !== "main") return;
  const order = [...l.keys.filter((k) => k.idx === l.anchorIdx), ...l.keys.filter((k) => k.dup == null && k.idx !== l.anchorIdx),
    ...l.keys.filter((k) => k.dup != null)];
  const i = order.findIndex((k) => k.idx === ui.idx);
  const next = order[i + dir];
  if (!next) return;
  ui.idx = next.idx;
  ui.showAnchor = false;
  ui.showRef = false;
  closePopover();
  render();
}

// ---------- log drawer
function toggleLog() {
  const d = $("#log-drawer");
  d.hidden = !d.hidden;
  if (!d.hidden) { d.textContent = ui.log.join("\n"); d.scrollTop = d.scrollHeight; }
}

// ---------- modals
function modal(html) { const m = $("#modal"); m.innerHTML = html; m.hidden = false; const f = m.querySelector("[autofocus], .btn.primary"); if (f) f.focus(); }
function closeModal() { const m = $("#modal"); m.hidden = true; m.innerHTML = ""; }

function errorDialog(title, msg) {
  return `<div class="dialog" role="alertdialog" aria-label="${esc(title)}"><h2>${esc(title)}</h2><p>${esc(msg)}</p>
    <div class="actions"><button class="btn primary" onclick="closeModal()">Зрозуміло</button></div></div>`;
}

function confirmCost(what, usd) {
  return new Promise((resolve) => {
    modal(`<div class="dialog" role="dialog" aria-label="Підтвердження витрат"><h2>Підтвердження витрат</h2>
      <p>${esc(what)}</p><p>Орієнтовна вартість: <b style="color:var(--fg)">~${money(usd)}</b></p>
      <div class="actions"><button class="btn" id="m-no">Скасувати</button><button class="btn primary" id="m-yes">Продовжити</button></div></div>`);
    $("#m-yes").onclick = () => { closeModal(); resolve(true); };
    $("#m-no").onclick = () => { closeModal(); resolve(false); };
  });
}

// model choice: a list of tested-for-availability models + "other" (any OpenRouter vision model id)
function modelPicker(id, label, list, cur) {
  const known = list.some((m) => m.id === cur);
  const opts = list.map((m) => `<option value="${esc(m.id)}" ${m.id === cur ? "selected" : ""}>${esc(m.id)} · ~$${m.est < 0.01 ? m.est.toFixed(4) : m.est.toFixed(2)} · ${esc(m.note)}</option>`).join("");
  return `<div class="field"><label for="${id}">${label}</label>
    <select id="${id}">${opts}<option value="__other" ${known ? "" : "selected"}>Інша модель (id OpenRouter)…</option></select>
    <input id="${id}-c" type="text" placeholder="напр. google/gemini-3.8-flash" value="${known ? "" : esc(cur)}" ${known ? "hidden" : ""}></div>`;
}

function bindModelPicker(id, list, estId) {
  const sel = $(`#${id}`), other = $(`#${id}-c`);
  sel.addEventListener("change", () => {
    other.hidden = sel.value !== "__other";
    const m = list.find((x) => x.id === sel.value);
    if (m) $(`#${estId}`).value = m.est;  // rough $ per request of the chosen model
    if (!other.hidden) other.focus();
  });
}

function pickedModel(id) {
  const sel = $(`#${id}`);
  return sel.value === "__other" ? $(`#${id}-c`).value.trim() : sel.value;
}

async function openSettings() {
  const s = await api().get_settings();
  modal(`<div class="dialog" role="dialog" aria-label="Налаштування"><h2>Налаштування</h2>
    <div class="field"><label for="s-key">fal.ai API key</label><div class="inline">
      <input id="s-key" type="password" value="${esc(s.apiKey)}" placeholder="зберігається у Windows Credential Manager">
      <button class="btn" id="s-show" type="button">Показати</button><button class="btn" id="s-check" type="button">Перевірити</button></div>
      <span id="s-note"></span></div>
    <div class="cols2">
      ${modelPicker("s-model", "Модель для якоря", s.models.anchor, s.model)}
      <div class="field"><label for="s-est">Оцінка $ за якір</label><input id="s-est" type="number" step="0.01" value="${s.est}"></div>
    </div>
    <label class="check"><input id="s-reason" type="checkbox" ${s.reasoning ? "checked" : ""}><span>Reasoning для якоря: модель сама розбирає неоднозначні місця. Довше, але точніше.</span></label>
    <div class="cols2">
      ${modelPicker("s-model-f", "Модель для кадрів за якорем", s.models.follow, s.modelFollow)}
      <div class="field"><label for="s-est-f">Оцінка $ за кадр</label><input id="s-est-f" type="number" step="0.001" value="${s.estFollow}"></div>
    </div>
    <span style="color:var(--muted);font-size:12px;line-height:1.5">Моделі fal openrouter/router/vision. Кадри за якорем ідуть без reasoning: зразок уже є, тому швидка модель працює так само точно.</span>
    <div class="cols2">
      <div class="field"><label for="s-conc">Паралельних запитів</label><input id="s-conc" type="number" min="1" max="16" value="${s.concurrency}"></div>
      <div class="field"><label for="s-port">Порт TVPaint</label><input id="s-port" type="number" value="${s.port}"></div>
    </div>
    <div class="actions"><button class="btn" id="s-cancel">Скасувати</button><button class="btn primary" id="s-save">Зберегти</button></div></div>`);
  $("#s-show").onclick = () => { const i = $("#s-key"); i.type = i.type === "password" ? "text" : "password"; };
  bindModelPicker("s-model", s.models.anchor, "s-est");
  bindModelPicker("s-model-f", s.models.follow, "s-est-f");
  $("#s-check").onclick = async () => {
    const n = $("#s-note"); n.className = ""; n.textContent = "Перевірка…";
    const r = await api().check_key($("#s-key").value, pickedModel("s-model"));
    n.className = r.ok ? "note-ok" : "note-warn"; n.textContent = r.ok ? "Ключ працює." : `Ключ не прийнято: ${r.error}`;
  };
  $("#s-cancel").onclick = closeModal;
  $("#s-save").onclick = async () => {
    await call("save_settings", {
      apiKey: $("#s-key").value, model: pickedModel("s-model"), reasoning: $("#s-reason").checked,
      concurrency: +$("#s-conc").value, est: +$("#s-est").value, port: +$("#s-port").value,
      modelFollow: pickedModel("s-model-f"), estFollow: +$("#s-est-f").value,
    });
    closeModal();
  };
}

// browser mode: the reference image comes from this device and is stored next to the project
function uploadRef(uid) {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = ".png,.jpg,.jpeg,.webp";
  inp.onchange = () => {
    const f = inp.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => call("upload_ref", uid, f.name, String(rd.result).split(",")[1]);
    rd.readAsDataURL(f);
  };
  inp.click();
}

// ------------------------------------------------------------------ boot
async function boot() {
  setState(await api().get_state());
  if (!S.connected && !S.busy) { ui.quietConnect = true; api().connect(null, false); }  // quiet first attempt
}

if (REMOTE) {
  let opened = false;
  const es = new EventSource("/events");
  es.onmessage = (e) => window.tintr.on(JSON.parse(e.data));
  // first open boots the UI; a reopen after a dropped connection resyncs (events in between are lost)
  es.onopen = () => { if (!opened) { opened = true; boot(); } else api().get_state().then(setState); };
} else {
  window.addEventListener("pywebviewready", boot);
}
