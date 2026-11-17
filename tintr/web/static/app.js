/* Tintr UI. State comes from Python (pywebview js_api); this file only renders it and sends intents. */
"use strict";

let S = null;                       // last state from Python
const ui = {
  uid: null, idx: null, mode: null, view: "main",
  log: [], logOpen: false, live: {},
  insertSel: null, save: null, port: null, connectError: "",
  zoom: { s: 1, x: 0, y: 0 },        // preview zoom (1 = fit) and pan in px; kept across keys/modes
  panned: false,                     // last mouse press was a pan drag, not a click
};

const api = () => window.pywebview.api;
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => "$" + (Number(v) || 0).toFixed(2);

const ICON = {
  settings: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>',
  folder: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>',
  alert: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="flex-shrink:0;margin-top:2px" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h0"/></svg>',
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

const STEP_OF_STAGE = { missing: 0, export: 0, ref: 1, anchor: 1, approve: 1, frames: 2, ready: 3, done: 4 };

// ------------------------------------------------------------------ render
function render() {
  const app = $("#app");
  if (!S) return;
  if (!S.connected) { app.innerHTML = header(false) + connectView() + footer(); bindConnect(); renderFooter(); return; }
  ensureSelection();
  const center = ui.view === "insert" ? insertView() : centerView();
  app.innerHTML = header(true) + `<div class="body">${treeView()}${center}${sideView()}</div>` + footer();
  bindMain();
  renderFooter();
}

function header(connected) {
  if (!connected) {
    return `<header><span class="brand">Tintr</span><span class="proj">не підключено</span><div class="grow"></div>
      <button class="icon-btn" data-act="settings" aria-label="Налаштування">${ICON.settings}</button></header>`;
  }
  const l = curLayer();
  const cur = l ? STEP_OF_STAGE[l.stage] : 0;
  const names = ["Ключі", "Якір", "Кадри", "Вставка"];
  const steps = names.map((n, i) => {
    const cls = cur > i ? "done" : cur === i || (i === 3 && cur === 4) ? "current" : "todo";
    const view = i === 3 ? "insert" : "main";
    const on = ui.view === view && (i === 3 || cls === "current");
    return `<button class="step ${cls}${on ? "" : ""}" data-act="view" data-view="${view}"><span class="dot"></span>${n}</button>` + (i < 3 ? '<span class="step-sep"></span>' : "");
  }).join("");
  return `<header><span class="brand">Tintr</span><span class="proj">${esc(S.project.name)}</span>
    <nav class="steps" aria-label="Кроки">${steps}</nav><div class="grow"></div>
    <span class="spend">Витрачено на проект ${money(S.cost.project)}</span>
    <button class="icon-btn" data-act="folder" aria-label="Відкрити папку з кадрами" title="Відкрити папку з кадрами">${ICON.folder}</button>
    <button class="icon-btn" data-act="settings" aria-label="Налаштування">${ICON.settings}</button></header>`;
}

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
    ${c.layers.map((l) => `<button class="layer ${l.uid === ui.uid ? "sel" : ""}" data-act="layer" data-uid="${esc(l.uid)}">
      <span class="swatch" style="${l.swatch ? `background:${l.swatch}` : ""}"></span>
      <span class="layer-text"><b>${esc(l.name)}</b><small class="tone-${l.tone}">${esc(layerState(l))}</small></span>
      <span class="count">${l.done}/${l.total}</span></button>`).join("")}</div>`).join("");
  const scanned = S.project.scanned;
  const empty = scanned ? "Шарів з тегом _cc- не знайдено." : "Проект ще не скановано.";
  return `<aside class="tree"><div class="tree-head"><span class="eyebrow">Кліпи</span>
    <button class="link" data-act="rescan" ${S.busy ? "disabled" : ""} title="Знайти шари з тегом _cc- у всіх кліпах">${scanned ? "Пересканувати" : "Сканувати"}</button></div>
    <div class="tree-clips">${clips || `<div style="padding:0 20px;color:var(--muted)">${empty}</div>`}</div>
    ${clips ? allClipsView() : ""}</aside>`;
}

function allClipsView() {
  // one parallel run over every clip; layers without a reference are skipped
  const a = S.allAnchors || 0, f = S.allFollows || 0, busy = !!S.busy;
  const wait = S.awaiting ? `<small class="tone-accent">${S.awaiting} якір(ів) чекають затвердження</small>` : "";
  return `<div class="tree-all"><span class="eyebrow">Усі кліпи · паралельно</span>
    <button class="btn sm" data-act="all-anchors" ${a && !busy ? "" : "disabled"} title="Якорі всіх шарів, у яких є референс і якір ще не розфарбовано">
      Розфарбувати всі якорі${a ? ` · ${a}` : ""}</button>
    <button class="btn sm" data-act="all-follows" ${f && !busy ? "" : "disabled"} title="Ключові кадри всіх шарів із затвердженим якорем">
      Розфарбувати всі кадри${f ? ` · ${f}` : ""}</button>${wait}</div>`;
}

function layerState(l) {
  const busyHere = l.keys.some((k) => ui.live[`${l.uid}:${k.idx}`]);
  if (busyHere) return "розфарбовується";
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
  const info = k ? `ключ #${k.idx} · кадр ${k.start} · ${k.length} кадр(ів)${k.dup != null ? ` · повтор #${k.dup}` : ""}` : "";
  let state = "";
  if (k && k.anchor) state = l.anchorApproved ? '<span class="tone-ok" style="font-size:12px">якір затверджено</span>' : k.colored ? '<span class="tone-accent" style="font-size:12px">якір чекає затвердження</span>' : "";
  else if (k && k.colored && m && m.followed) state = '<span class="tone-ok" style="font-size:12px">розфарбовано за якорем</span>';
  if (k && k.error) state = `<span class="tone-warn" style="font-size:12px" title="${esc(k.error)}">помилка</span>`;
  return `<main class="center">
    <div class="toolbar"><b>${esc(l.name)}</b><span class="info">${info}</span>${state}<div class="grow"></div>
      ${canAnchor ? `<button class="btn sm" data-act="set-anchor">Зробити якорем</button>` : ""}
      <button class="btn sm" data-act="export" ${S.busy ? "disabled" : ""}>Оновити ключі з TVPaint</button>
      <div class="modes" role="group" aria-label="Що показати">${modeBtns}</div></div>
    <div class="stage" id="stage">${stageView(l, k)}</div>
    ${groupsView(l)}
  </main>`;
}

function stageView(l, k) {
  if (!k || !k.key) {
    return `<div class="empty"><b>Ключі ще не виведені</b>Tintr збере ключові кадри цього кліпу з TVPaint одним викликом і одразу знайде на них області для розфарбування. Це безкоштовно.
      <button class="btn primary" data-act="export" ${S.busy ? "disabled" : ""}>Вивести ключі</button></div>`;
  }
  const anchor = l.keys.find((x) => x.idx === l.anchorIdx);
  const sideBySide = ui.mode === "color" && anchor && anchor.colored && k.idx !== anchor.idx && master(l, k).idx !== anchor.idx;
  const big = frameHtml(k, ui.mode, true) + zoomBar();
  if (!sideBySide) {
    const hint = ui.mode === "regions" ? "Це бачить Gemini. Клік по області — змінити колір."
      : ui.mode === "color" ? "Клік по області — змінити колір." : "";
    return big + (hint && master(l, k).colored ? `<span class="stage-hint">${hint}</span>` : ui.mode === "regions" ? `<span class="stage-hint">Області, які бачить Gemini.</span>` : "");
  }
  return `<div class="figure" style="flex:0 0 28%">${frameHtml(anchor, "color", false, "anchor-ref")}<div class="cap">Якір #${anchor.idx}</div></div>
    ${ICON.arrow}<div class="figure" style="flex:1">${big}<div class="cap">Ключ #${k.idx} · клік по області — виправити колір</div></div>`;
}

function frameHtml(k, mode, main, extra = "") {
  const W = S.project.w, H = S.project.h;
  let imgs = "", paper = "";
  if (mode === "color") { imgs = `<img src="${k.color}" alt=""><img src="${k.key}" alt="">`; paper = "paper"; }
  else if (mode === "regions") imgs = `<img src="${k.regions}" alt="">`;
  else imgs = `<img src="${k.key}" alt="">`;
  const click = main && mode !== "line" ? "clickable" : "";
  return `<div class="frame ${paper} ${click} ${extra}" ${main ? 'id="frame"' : ""} data-w="${W}" data-h="${H}" style="aspect-ratio:${W}/${H}"><div class="zoom" ${main ? 'id="zoom"' : ""}>${imgs}</div></div>`;
}

function fitFrames() {
  // largest size that keeps the frame's aspect ratio inside the stage (or its figure column)
  const stage = $("#stage");
  if (!stage) return;
  const sr = stage.getBoundingClientRect();
  for (const f of stage.querySelectorAll(".frame")) {
    const inFig = f.parentElement !== stage;
    const W = +f.dataset.w, H = +f.dataset.h;
    const maxW = inFig ? f.parentElement.getBoundingClientRect().width : sr.width - 40;
    const maxH = sr.height - 40 - (inFig ? 28 : 0);
    const s = Math.min(maxW / W, maxH / H);
    f.style.width = `${Math.max(40, Math.floor(W * s))}px`;
  }
  applyZoom();
}

// ---------- zoom / pan of the main preview (line, regions, colour)
function zoomBar() {
  return `<div class="zoombar" role="group" aria-label="Масштаб">
    <button class="icon-btn" data-act="zoom" data-z="out" title="Зменшити (−)" aria-label="Зменшити">−</button>
    <span id="zoom-pct">${Math.round(ui.zoom.s * 100)}%</span>
    <button class="icon-btn" data-act="zoom" data-z="in" title="Збільшити (+)" aria-label="Збільшити">+</button>
    <button class="btn sm" data-act="zoom" data-z="px" title="Піксель у піксель">1:1</button>
    <button class="btn sm" data-act="zoom" data-z="fit" title="Вписати (0)">Вписати</button></div>`;
}

function applyZoom() {
  const fr = $("#frame"), z = $("#zoom");
  if (!fr || !z) return;
  const Z = ui.zoom, w = fr.clientWidth, h = fr.clientHeight;
  Z.s = Math.min(24, Math.max(1, Z.s));
  Z.x = Math.min(0, Math.max(w - w * Z.s, Z.x));
  Z.y = Math.min(0, Math.max(h - h * Z.s, Z.y));
  z.style.transform = `translate(${Z.x}px, ${Z.y}px) scale(${Z.s})`;
  fr.classList.toggle("zoomed", Z.s > 1);
  const pct = $("#zoom-pct");
  if (pct) pct.textContent = `${Math.round(Z.s * 100)}%`;
}

function zoomTo(s, cx, cy) {
  // keep the image point under (cx, cy) (client px; default: frame centre) in place
  const fr = $("#frame");
  if (!fr) return;
  const r = fr.getBoundingClientRect(), Z = ui.zoom;
  if (cx == null) { cx = r.left + r.width / 2; cy = r.top + r.height / 2; }
  s = Math.min(24, Math.max(1, s));
  const lx = (cx - r.left - Z.x) / Z.s, ly = (cy - r.top - Z.y) / Z.s;
  Z.x = cx - r.left - lx * s; Z.y = cy - r.top - ly * s; Z.s = s;
  applyZoom();
}

function zoomCmd(cmd) {
  const fr = $("#frame");
  if (!fr) return;
  closePopover();
  if (cmd === "in") zoomTo(ui.zoom.s * 1.5);
  else if (cmd === "out") zoomTo(ui.zoom.s / 1.5);
  else if (cmd === "px") zoomTo(S.project.w / fr.clientWidth);
  else { ui.zoom = { s: 1, x: 0, y: 0 }; applyZoom(); }
}

function bindZoom() {
  const fr = $("#frame");
  if (!fr) return;
  fr.addEventListener("wheel", (e) => {
    e.preventDefault();
    closePopover();
    zoomTo(ui.zoom.s * Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
  }, { passive: false });
  fr.addEventListener("mousedown", (e) => {
    // left drag pans when zoomed in (a click without moving still picks a region); middle drag always
    ui.panned = false;
    if (!(e.button === 1 || (e.button === 0 && ui.zoom.s > 1))) return;
    e.preventDefault();
    const x0 = e.clientX, y0 = e.clientY, zx = ui.zoom.x, zy = ui.zoom.y;
    const move = (ev) => {
      if (!ui.panned && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 4) return;
      ui.panned = true;
      fr.classList.add("panning");
      ui.zoom.x = zx + ev.clientX - x0; ui.zoom.y = zy + ev.clientY - y0;
      applyZoom();
    };
    const up = () => { fr.classList.remove("panning"); window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
  fr.addEventListener("auxclick", (e) => { if (e.button === 1) e.preventDefault(); });
}

function groupsView(l) {
  if (!l.keys.some((k) => k.key)) return "";
  const anchor = l.keys.filter((k) => k.idx === l.anchorIdx);
  const uniq = l.keys.filter((k) => k.dup == null && k.idx !== l.anchorIdx);
  const dups = l.keys.filter((k) => k.dup != null);
  const a = anchor[0];
  const aHint = !a ? "" : l.anchorApproved ? "затверджено" : a.colored ? "чекає затвердження" : "ще не розфарбовано";
  const uDone = uniq.filter((k) => k.colored && k.followed).length;
  const uHint = !uniq.length ? "" : !l.anchorApproved ? `${uniq.length} · після затвердження якоря ≈ ${money(uniq.length * S.estFollow)}`
    : uDone === uniq.length ? "усі готові" : `${uDone} з ${uniq.length} готово`;
  const group = (title, hint, keys) => keys.length ? `<div class="group"><div class="group-head"><span class="eyebrow">${title}</span><span class="hint">${esc(hint)}</span></div>
    <div class="group-keys">${keys.map((k) => kfHtml(l, k)).join("")}</div></div>` : "";
  return `<section class="groups" aria-label="Ключові кадри">${group("Якір", aHint, anchor)}${group("Унікальні", uHint, uniq)}${group("Повтори", "колір оригіналу, безкоштовно", dups)}</section>`;
}

function kfHtml(l, k) {
  const live = ui.live[`${l.uid}:${k.idx}`];
  const thumb = k.colorThumb ? `<img src="${k.colorThumb}" alt=""><img src="${k.keyThumb}" alt="">` : `<img src="${k.keyThumb}" alt="">`;
  const cls = ["kf", k.idx === ui.idx ? "sel" : "", k.idx === l.anchorIdx ? "anchor" : "", k.dup != null ? "dup" : ""].join(" ");
  return `<button class="${cls}" data-act="key" data-idx="${k.idx}" title="${k.error ? esc(k.error) : ""}">
    <span class="thumb ${k.colorThumb ? "paper" : ""}">${thumb}
      <span class="live" data-live="${esc(l.uid)}:${k.idx}" ${live ? "" : "hidden"}><span>${live ? esc(liveText(live)) : ""}</span></span>
      ${k.error ? '<span class="err"></span>' : ""}</span>
    <span class="meta"><b>#${k.idx}</b><span>кадр ${k.start} · ${k.length}</span></span></button>`;
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
  // the tree row text ("розфарбовується") depends on live statuses too
  for (const l of layers()) {
    const row = document.querySelector(`.layer[data-uid="${CSS.escape(l.uid)}"] small`);
    if (row) row.textContent = layerState(l);
  }
}
setInterval(() => { if (Object.keys(ui.live).length) renderLive(); }, 1000);

// ---------- right panel
function sideView() {
  const l = curLayer();
  if (!l) return `<aside class="side"></aside>`;
  const ref = l.refUrl
    ? `<div class="ref"><img src="${l.refUrl}" alt="Референс ${esc(l.name)}"></div><span class="ref-name">${esc(l.ref)} · ${l.palette.length} кольорів у палітрі</span>
       <div class="palette">${l.palette.map((p) => `<span title="${p.name}" style="background:${p.hex}"></span>`).join("")}</div>`
    : `<div class="ref none"><span>Референс не вибрано</span><button class="btn primary" data-act="ref">Оберіть референс</button></div>`;
  return `<aside class="side"><div class="side-head"><span class="eyebrow">Референс · ${esc(l.name)}</span>
    ${l.refUrl ? `<button class="link" data-act="ref" ${S.busy ? "disabled" : ""}>Змінити</button>` : ""}</div>
    ${ref}<div class="grow"></div><div class="action" id="action">${actionView(l)}</div></aside>`;
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
      return `<b>Якір: ключ #${a.idx}</b><p>Перевірте кольори. Клік по області — змінити колір. Затверджений якір задає кольори для решти кадрів.</p>
        <div class="row"><button class="btn" data-act="recolor-anchor" ${dis}>Перефарбувати</button>
        <button class="btn primary" data-act="approve" ${dis}>Затвердити</button></div>`;
    case "frames":
      return `<b>Кадри за якорем</b><p>Якір #${a.idx} затверджено. ${l.followsN} унікальних кадр(ів) розфарбуються за ним, повтори отримають колір автоматично.</p>
        <button class="btn primary" data-act="follows" ${dis}>Розфарбувати ${l.followsN} кадр(ів) · ~${money(l.followsN * S.estFollow)}</button>${recolorLink(dis)}`;
    case "ready":
      return `<b>Усі кадри готові</b><p>${l.total} кадр(ів) розфарбовано. Можна вставляти в TVPaint.</p>
        <button class="btn primary" data-act="view" data-view="insert">До вставки</button>${recolorLink(dis)}`;
    case "done":
      return `<b>Вставлено в TVPaint</b><p>Шар ${esc(l.name)}_color створено під лайном.</p>
        <button class="btn" data-act="view" data-view="insert">Вставити знову</button>${recolorLink(dis)}`;
  }
  return "";
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
  if (fr && fr.classList.contains("clickable")) fr.addEventListener("click", onFrameClick);
  for (const img of document.querySelectorAll("#stage img")) img.addEventListener("load", fitFrames, { once: true });
  bindZoom();
  fitFrames();
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
  switch (act) {
    case "connect": doConnect(); break;
    case "settings": openSettings(); break;
    case "folder": call("open_folder"); break;
    case "log": toggleLog(); break;
    case "view": ui.view = b.dataset.view; closePopover(); render(); break;
    case "layer": ui.uid = b.dataset.uid; ui.idx = null; ui.view = "main"; ui.zoom = { s: 1, x: 0, y: 0 }; closePopover(); render(); break;
    case "zoom": zoomCmd(b.dataset.z); break;
    case "key": ui.idx = +b.dataset.idx; closePopover(); render(); break;
    case "mode": ui.mode = b.dataset.mode; closePopover(); render(); break;
    case "rescan": call("connect", null, true); break;
    case "export": call("export"); break;
    case "ref": call("pick_ref", l.uid); break;
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

// ---------- region popover
async function onFrameClick(e) {
  if (ui.panned) { ui.panned = false; return; }  // the press was a pan drag
  if (S.busy) return;
  const l = curLayer(), k = curKey();
  const r = $("#zoom").getBoundingClientRect();  // zoomed/panned image box
  const x = ((e.clientX - r.left) / r.width) * S.project.w;
  const y = ((e.clientY - r.top) / r.height) * S.project.h;
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
    await call("set_region_color", l.uid, k.idx, res.region, c.dataset.c);
  };
}
function closePopover() { $("#popover").hidden = true; }
document.addEventListener("mousedown", (e) => {
  if (!$("#popover").hidden && !e.target.closest("#popover") && !e.target.closest("#frame")) closePopover();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closePopover(); closeModal(); return; }
  if (e.target.closest("input, textarea") || !$("#modal").hidden) return;
  if (e.key === "+" || e.key === "=") zoomCmd("in");
  else if (e.key === "-") zoomCmd("out");
  else if (e.key === "0") zoomCmd("fit");
});

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

async function openSettings() {
  const s = await api().get_settings();
  modal(`<div class="dialog" role="dialog" aria-label="Налаштування"><h2>Налаштування</h2>
    <div class="field"><label for="s-key">fal.ai API key</label><div class="inline">
      <input id="s-key" type="password" value="${esc(s.apiKey)}" placeholder="зберігається у Windows Credential Manager">
      <button class="btn" id="s-show" type="button">Показати</button><button class="btn" id="s-check" type="button">Перевірити</button></div>
      <span id="s-note"></span></div>
    <div class="cols2">
      <div class="field"><label for="s-model">Модель для якоря</label><input id="s-model" type="text" value="${esc(s.model)}"></div>
      <div class="field"><label for="s-est">Оцінка $ за якір</label><input id="s-est" type="number" step="0.01" value="${s.est}"></div>
    </div>
    <label class="check"><input id="s-reason" type="checkbox" ${s.reasoning ? "checked" : ""}><span>Reasoning для якоря: модель сама розбирає неоднозначні місця. Довше, але точніше.</span></label>
    <div class="cols2">
      <div class="field"><label for="s-model-f">Модель для кадрів за якорем</label><input id="s-model-f" type="text" value="${esc(s.modelFollow)}"></div>
      <div class="field"><label for="s-est-f">Оцінка $ за кадр</label><input id="s-est-f" type="number" step="0.001" value="${s.estFollow}"></div>
    </div>
    <span style="color:var(--muted);font-size:12px;line-height:1.5">Моделі fal openrouter/router/vision. Кадри за якорем ідуть без reasoning: зразок уже є, тому швидка модель працює так само точно.</span>
    <div class="cols2">
      <div class="field"><label for="s-conc">Паралельних запитів</label><input id="s-conc" type="number" min="1" max="16" value="${s.concurrency}"></div>
      <div class="field"><label for="s-port">Порт TVPaint</label><input id="s-port" type="number" value="${s.port}"></div>
    </div>
    <div class="actions"><button class="btn" id="s-cancel">Скасувати</button><button class="btn primary" id="s-save">Зберегти</button></div></div>`);
  $("#s-show").onclick = () => { const i = $("#s-key"); i.type = i.type === "password" ? "text" : "password"; };
  $("#s-check").onclick = async () => {
    const n = $("#s-note"); n.className = ""; n.textContent = "Перевірка…";
    const r = await api().check_key($("#s-key").value, $("#s-model").value);
    n.className = r.ok ? "note-ok" : "note-warn"; n.textContent = r.ok ? "Ключ працює." : `Ключ не прийнято: ${r.error}`;
  };
  $("#s-cancel").onclick = closeModal;
  $("#s-save").onclick = async () => {
    await call("save_settings", {
      apiKey: $("#s-key").value, model: $("#s-model").value, reasoning: $("#s-reason").checked,
      concurrency: +$("#s-conc").value, est: +$("#s-est").value, port: +$("#s-port").value,
      modelFollow: $("#s-model-f").value, estFollow: +$("#s-est-f").value,
    });
    closeModal();
  };
}

// ------------------------------------------------------------------ boot
window.addEventListener("pywebviewready", async () => {
  setState(await api().get_state());
  if (!S.connected && !S.busy) { ui.quietConnect = true; api().connect(null, false); }  // quiet first attempt
});
