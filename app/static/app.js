"use strict";

const $ = (id) => document.getElementById(id);
const ENTITY_LABEL = { issuer: "Titres", fund: "Fonds" };
const ENTITY_NOUN = { issuer: ["titre", "titres"], fund: ["fonds", "fonds"] };
const STORE_KEY = "msci-extraction.lists";
const MAX_DRAWN_ROWS = 1200;
const ROW_H = 24;
const COL_H = 34;

const STATE_TEXT = {
  complete: "Toutes les colonnes remplies",
  partial: "Partiellement rempli",
  reference_only: "Références seules, aucune valeur ESG",
  no_data_returned: "Aucune donnée retournée par MSCI",
  unresolved_identifier: "ISIN inconnu de MSCI",
  ambiguous_identifier: "Plusieurs fonds MSCI pour cet ISIN",
  invalid_isin: "ISIN invalide, non envoyé",
};
const STATE_FLAG = {
  reference_only: "#b87a0a", no_data_returned: "#c0442f", unresolved_identifier: "#c0442f",
  ambiguous_identifier: "#c0442f", invalid_isin: "#c0442f",
};

const css = getComputedStyle(document.documentElement);
const color = (name) => css.getPropertyValue(name).trim();
const CELL_COLORS = {
  "4": color("--src-v4"), "c": color("--src-v4"), "3": color("--src-v3"),
  "h": color("--src-history"), "i": color("--src-input"), ".": color("--cell-empty"),
};
const SOURCE_OF_CODE = { "4": "v4", "c": "coverage", "3": "v3", "h": "history", "i": "input" };
const SOURCE_TEXT = { v4: "API V4", coverage: "Référentiel MSCI", v3: "API V3", history: "Historique V4", input: "Votre saisie" };

const state = {
  entity: "issuer",
  texts: { issuer: "", fund: "" },
  columns: null,
  status: null,
  job: null,
  result: null,
  view: "issuer",
  cellCache: new Map(),
};

/* ------------------------------------------------------------------ ISIN */
function isinProblem(code) {
  if (code.length !== 12) return `${code.length} caractères au lieu de 12`;
  if (!/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(code)) return "Format : 2 lettres, 9 caractères, 1 chiffre";
  const digits = code.split("").map((c) => (/[A-Z]/.test(c) ? String(c.charCodeAt(0) - 55) : c)).join("");
  let total = 0;
  for (let i = 0; i < digits.length; i++) {
    let n = Number(digits[digits.length - 1 - i]) * (i % 2 ? 2 : 1);
    total += Math.floor(n / 10) + (n % 10);
  }
  return total % 10 === 0 ? null : "Clé de contrôle incorrecte";
}

function parseText(text) {
  const rows = [];
  text.split(/\r?\n/).forEach((line, index) => {
    const raw = line.replace(/\u00a0/g, " ");
    if (!raw.trim()) return;
    let parts = raw.includes("\t") ? raw.split("\t") : raw.split(/[;|]/);
    if (parts.length === 1) {
      // Plusieurs ISIN sur une ligne (copie d'une ligne Excel ou d'un e-mail)
      const tokens = raw.trim().split(/[\s,]+/).filter(Boolean);
      if (tokens.length > 1 && tokens.every((t) => t.length >= 10)) {
        tokens.forEach((t) => rows.push(makeRow(t, null, null, index + 1)));
        return;
      }
    }
    rows.push(makeRow(parts[0], parts[1], parts[2], index + 1));
  });
  return rows;
}

function makeRow(isin, clientId, status, line) {
  const code = String(isin || "").trim().toUpperCase().replace(/\s+/g, "");
  return {
    ISIN: code,
    Client_ID: clientId && clientId.trim() ? clientId.trim() : null,
    Security_Status: status && status.trim() ? status.trim() : null,
    line,
    problem: isinProblem(code),
  };
}

/* ------------------------------------------------------------------ saisie */
function saveLists() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state.texts)); } catch (e) { /* stockage indisponible */ }
}
function loadLists() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    state.texts.issuer = saved.issuer || "";
    state.texts.fund = saved.fund || "";
  } catch (e) { /* stockage indisponible */ }
}

function selectEntity(entity) {
  state.entity = entity;
  document.querySelectorAll(".input [role=tab]").forEach((tab) => {
    const on = tab.dataset.entity === entity;
    tab.setAttribute("aria-selected", on);
    tab.tabIndex = on ? 0 : -1;
  });
  $("panel-list").setAttribute("aria-labelledby", `tab-${entity}`);
  $("isins").value = state.texts[entity];
  refreshCheck();
}

function refreshCheck() {
  const counts = {};
  for (const entity of ["issuer", "fund"]) {
    const rows = parseText(state.texts[entity]);
    counts[entity] = rows;
    document.querySelector(`[data-count=${entity}]`).textContent = rows.length;
  }
  const rows = counts[state.entity];
  const bad = rows.filter((r) => r.problem);
  const unique = new Set(rows.map((r) => r.ISIN)).size;
  const box = $("check");
  box.innerHTML = "";
  if (rows.length) {
    const line = document.createElement("div");
    line.className = "ok";
    const dup = rows.length - unique;
    line.textContent = `${rows.length} ISIN` +
      (dup ? ` · ${dup} doublon${dup > 1 ? "s" : ""} (conservés dans l'export)` : "") +
      (bad.length ? ` · ${bad.length} à corriger` : " · tous valides");
    box.append(line);
  }
  if (bad.length) {
    const list = document.createElement("ul");
    bad.slice(0, 50).forEach((r) => {
      const li = document.createElement("li");
      li.innerHTML = `<span><b></b> · ligne ${r.line}</span><span></span>`;
      li.querySelector("b").textContent = r.ISIN || "(vide)";
      li.lastChild.textContent = r.problem;
      list.append(li);
    });
    box.append(list);
  }
  if (rows.length) {
    const actions = document.createElement("div");
    actions.className = "row-actions";
    if (bad.length) actions.append(linkButton("Retirer les ISIN invalides", () => {
      setText(rows.filter((r) => !r.problem).map(rowToLine).join("\n"));
    }));
    if (rows.length !== unique) actions.append(linkButton("Retirer les doublons", () => {
      const seen = new Set();
      setText(rows.filter((r) => !seen.has(r.ISIN) && seen.add(r.ISIN)).map(rowToLine).join("\n"));
    }));
    actions.append(linkButton(`Vider la liste ${ENTITY_LABEL[state.entity]}`, () => setText("")));
    box.append(actions);
  }
  const total = counts.issuer.length + counts.fund.length;
  const running = state.job && !state.result && !$("progress").hidden;
  $("run").disabled = total === 0 || running || !canRun();
  $("run-note").textContent = !canRun()
    ? "Connectez-vous à MSCI (en haut à droite) pour lancer l'extraction."
    : total ? `${counts.issuer.length} titres · ${counts.fund.length} fonds` : "";
}

function canRun() {
  return state.status && (state.status.demo || state.status.configured);
}

function rowToLine(r) {
  const extra = [r.Client_ID, r.Security_Status];
  while (extra.length && !extra[extra.length - 1]) extra.pop();
  return [r.ISIN, ...extra.map((v) => v || "")].join("\t");
}

function setText(text) {
  state.texts[state.entity] = text;
  $("isins").value = text;
  saveLists();
  refreshCheck();
}

function linkButton(label, onClick) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "link"; b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

async function uploadFile(file) {
  const drop = $("drop");
  drop.classList.remove("over");
  const note = $("check");
  try {
    const response = await fetch("/api/parse", {
      method: "POST", body: file,
      headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Lecture impossible");
    const added = { issuer: 0, fund: 0 };
    const target = { issuer: data.issuer, fund: data.fund };
    target[state.entity] = target[state.entity].concat(data.auto);
    for (const entity of ["issuer", "fund"]) {
      if (!target[entity].length) continue;
      const lines = target[entity].map((r) => rowToLine(r));
      const current = state.texts[entity].trim();
      state.texts[entity] = (current ? current + "\n" : "") + lines.join("\n");
      added[entity] = lines.length;
    }
    saveLists();
    if (!added[state.entity] && added[state.entity === "issuer" ? "fund" : "issuer"]) {
      selectEntity(state.entity === "issuer" ? "fund" : "issuer");
    } else {
      selectEntity(state.entity);
    }
    flash(`${file.name} : ${added.issuer} titres, ${added.fund} fonds ajoutés.`);
  } catch (error) {
    note.insertAdjacentHTML("afterbegin", '<ul><li></li></ul>');
    note.querySelector("li").textContent = `${file.name} : ${error.message}`;
  }
}

function flash(text) {
  const box = $("check");
  const p = document.createElement("div");
  p.textContent = text;
  box.prepend(p);
}

/* ------------------------------------------------------------------ connexion */
async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    state.status = await response.json();
  } catch (e) {
    state.status = { configured: false, demo: false, error: "Serveur local injoignable" };
  }
  renderStatus();
}

function renderStatus() {
  const s = state.status;
  const conn = $("conn");
  if (s.demo) { conn.dataset.state = "demo"; $("conn-label").textContent = "Démonstration"; }
  else if (s.configured) { conn.dataset.state = "ok"; $("conn-label").textContent = s.connected ? "Connecté à MSCI" : "Identifiants MSCI chargés"; }
  else { conn.dataset.state = "off"; $("conn-label").textContent = "Se connecter à MSCI"; }
  $("demo-banner").hidden = !s.demo;
  refreshCheck();
}

async function postJSON(url, body) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Erreur ${response.status}`);
  return data;
}

/* ------------------------------------------------------------------ extraction */
async function run() {
  const rows = { issuer: parseText(state.texts.issuer), fund: parseText(state.texts.fund) };
  const payload = {
    issuer: rows.issuer.map(({ ISIN, Client_ID, Security_Status }) => ({ ISIN, Client_ID, Security_Status })),
    fund: rows.fund.map(({ ISIN, Client_ID, Security_Status }) => ({ ISIN, Client_ID, Security_Status })),
    options: { use_v3: $("opt-v3").checked, use_history: $("opt-history").checked, inherit: $("opt-inherit").checked },
  };
  state.result = null;
  state.cellCache.clear();
  show("progress");
  $("stage").textContent = "Connexion à MSCI";
  setProgress(0.02);
  $("run").disabled = true;
  if (window.matchMedia("(max-width: 900px)").matches) $("progress").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const { id } = await postJSON("/api/run", payload);
    state.job = id;
    poll();
  } catch (error) {
    fail(error.message);
  }
}

async function poll() {
  try {
    const response = await fetch(`/api/jobs/${state.job}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    if (data.state === "running") {
      $("stage").textContent = data.stage;
      setProgress(data.fraction);
      setTimeout(poll, 500);
      return;
    }
    if (data.state === "failed") return fail(data.error);
    state.result = data.result;
    renderResult();
  } catch (error) {
    fail(error.message);
  }
}

function setProgress(fraction) {
  $("fill").style.width = `${Math.round(fraction * 100)}%`;
  $("track").setAttribute("aria-valuenow", Math.round(fraction * 100));
}

function show(which) {
  for (const id of ["empty", "progress", "result"]) $(id).hidden = id !== which;
  $("failure").hidden = true;
}

function fail(message) {
  show("empty");
  const box = $("failure");
  box.hidden = false;
  box.innerHTML = "<strong>L'extraction n'a pas abouti.</strong><span></span>";
  box.querySelector("span").textContent = message;
  refreshCheck();
}

/* ------------------------------------------------------------------ résultat */
function renderResult() {
  const result = state.result;
  show("result");
  const entities = Object.keys(result.entities);
  state.view = entities.includes(state.view) ? state.view : entities[0];
  const parts = entities.map((e) => {
    const n = result.entities[e].rows;
    return `${n} ${ENTITY_NOUN[e][n > 1 ? 1 : 0]}`;
  });
  $("result-title").textContent = result.demo ? "Fichier de démonstration prêt" : "Fichier prêt";
  $("result-sub").textContent = `${parts.join(" et ")} · ${String(result.duration).replace(".", ",")} s`;
  $("download").href = `/api/jobs/${state.job}/excel`;
  $("report").href = `/api/jobs/${state.job}/report`;

  const tabs = $("result-tabs");
  tabs.innerHTML = "";
  tabs.hidden = entities.length < 2;
  entities.forEach((e) => {
    const b = document.createElement("button");
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", e === state.view);
    b.textContent = `Template ${ENTITY_LABEL[e]}`;
    b.addEventListener("click", () => { state.view = e; renderResult(); });
    tabs.append(b);
  });
  renderView();
  refreshCheck();
}

function renderView() {
  const result = state.result;
  const data = result.entities[state.view];
  const columns = state.columns[state.view];
  const by = data.cells_by_source;
  const msciFilled = (by["4"] || 0) + (by["c"] || 0) + (by["3"] || 0) + (by["h"] || 0);
  const rate = data.msci_cells ? Math.round((100 * msciFilled) / data.msci_cells) : 0;
  const fmt = (n) => n.toLocaleString("fr-FR");
  const pieces = [`${fmt((by["4"] || 0) + (by["c"] || 0))} par l'API V4`];
  if (by["3"]) pieces.push(`${fmt(by["3"])} par la V3`);
  if (by["h"]) pieces.push(`${fmt(by["h"])} par l'historique`);
  $("fill-line").innerHTML = `<strong>${rate} %</strong> des cellules MSCI sont remplies (${fmt(msciFilled)} sur ${fmt(data.msci_cells)}) : <span></span>.`;
  $("fill-line").querySelector("span").textContent = pieces.join(", ");

  const legend = [["4", "API V4"], ["3", "API V3"], ["h", "Historique"], ["i", "Saisie"], [".", "Vide"]];
  $("legend").innerHTML = "";
  legend.forEach(([code, label]) => {
    const li = document.createElement("li");
    li.innerHTML = `<i style="background:${CELL_COLORS[code]}"></i>`;
    li.append(label);
    $("legend").append(li);
  });

  drawSheet(data, columns);
  renderNotes(data);
}

function drawSheet(data, columns) {
  const rows = data.table.slice(0, MAX_DRAWN_ROWS);
  const labels = $("sheet-labels");
  labels.innerHTML = '<div class="head">ISIN · nom MSCI</div>';
  const frag = document.createDocumentFragment();
  rows.forEach((r) => {
    const div = document.createElement("div");
    div.className = "row";
    div.title = STATE_TEXT[r.state] + (r.problem ? ` (${r.problem})` : "");
    const flag = STATE_FLAG[r.state];
    div.innerHTML = `<span class="flag" style="background:${flag || "transparent"}"></span><span class="code"></span><span class="name"></span>`;
    div.querySelector(".code").textContent = r.isin || "(vide)";
    div.querySelector(".name").textContent = r.name || STATE_TEXT[r.state];
    frag.append(div);
  });
  labels.append(frag);
  $("sheet-note").textContent = data.table.length > MAX_DRAWN_ROWS
    ? `Aperçu limité aux ${MAX_DRAWN_ROWS} premières lignes ; l'Excel contient les ${data.table.length}.` : "";

  const holder = document.querySelector(".sheet-cells");
  const available = Math.max(200, holder.clientWidth - 16);
  const n = columns.length;
  const cw = Math.max(4, Math.min(18, available / n));
  const width = Math.ceil(cw * n);
  const dpr = window.devicePixelRatio || 1;
  const geometry = { cw, n, rows, columns, data };
  state.geometry = geometry;

  const colCanvas = $("col-canvas");
  sizeCanvas(colCanvas, width, COL_H, dpr);
  const cctx = colCanvas.getContext("2d");
  cctx.scale(dpr, dpr);
  const total = Math.max(1, data.rows);
  data.column_fill.forEach((filled, i) => {
    const h = Math.round((filled / total) * (COL_H - 10));
    cctx.fillStyle = color("--cell-empty");
    roundRect(cctx, i * cw, 4, cw - 1.5, COL_H - 10, 1);
    if (h > 0) {
      cctx.fillStyle = color("--ink-3");
      roundRect(cctx, i * cw, COL_H - 6 - h, cw - 1.5, h, 1);
    }
  });

  const cellCanvas = $("cell-canvas");
  sizeCanvas(cellCanvas, width, rows.length * ROW_H, dpr);
  const ctx = cellCanvas.getContext("2d");
  ctx.scale(dpr, dpr);
  rows.forEach((r, y) => {
    for (let x = 0; x < n; x++) {
      ctx.fillStyle = CELL_COLORS[r.cells[x]] || CELL_COLORS["."];
      ctx.fillRect(x * cw, y * ROW_H + 5, cw - 1.5, ROW_H - 10);
    }
  });
}

function sizeCanvas(canvas, w, h, dpr) {
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h);
  ctx.fill();
}

function showTip(html, event, setText) {
  const tip = $("tip");
  tip.innerHTML = html;
  setText(tip);
  tip.hidden = false;
  const box = $("sheet").getBoundingClientRect();
  let x = event.clientX - box.left + 14;
  let y = event.clientY - box.top + 14;
  if (x + tip.offsetWidth > box.width - 8) x = event.clientX - box.left - tip.offsetWidth - 14;
  if (y + tip.offsetHeight > box.height - 8) y = event.clientY - box.top - tip.offsetHeight - 14;
  tip.style.left = `${Math.max(4, x)}px`;
  tip.style.top = `${Math.max(4, y)}px`;
}

function hideTip() { $("tip").hidden = true; }

function onColumnHover(event) {
  const g = state.geometry;
  if (!g) return;
  const i = Math.floor(event.offsetX / g.cw);
  if (i < 0 || i >= g.n) return hideTip();
  const col = g.columns[i];
  const filled = g.data.column_fill[i];
  showTip('<b></b><div class="muted"></div><div></div>', event, (tip) => {
    tip.querySelector("b").textContent = col.column;
    tip.querySelector(".muted").textContent = col.label !== col.column ? col.label : sourceKind(col);
    tip.lastChild.textContent = `Remplie pour ${filled} ligne${filled > 1 ? "s" : ""} sur ${g.data.rows}`;
  });
}

function sourceKind(col) {
  return { input: "Saisie (colonne de votre fichier)", input_isin: "ISIN saisi", blocked: "Non alimentée (source à valider)" }[col.kind]
    || col.candidates.join(" puis ");
}

let hoverToken = 0;
function onCellHover(event) {
  const g = state.geometry;
  if (!g) return;
  const x = Math.floor(event.offsetX / g.cw);
  const y = Math.floor(event.offsetY / ROW_H);
  if (x < 0 || x >= g.n || y < 0 || y >= g.rows.length) return hideTip();
  const col = g.columns[x];
  const row = g.rows[y];
  const code = row.cells[x];
  const token = ++hoverToken;
  const render = (cell) => {
    if (token !== hoverToken) return;
    showTip('<b></b><div class="muted"></div><div class="val"></div><div class="muted src"></div>', event, (tip) => {
      tip.querySelector("b").textContent = col.column;
      tip.querySelector(".muted").textContent = `${row.isin}${row.name ? " · " + row.name : ""}`;
      const val = tip.querySelector(".val");
      const src = tip.querySelector(".src");
      if (code === ".") {
        val.textContent = "Vide";
        src.textContent = col.kind === "msci" ? `Codes demandés : ${col.candidates.join(", ")}` : sourceKind(col);
      } else {
        val.textContent = cell ? formatValue(cell.value) : "…";
        const source = SOURCE_TEXT[SOURCE_OF_CODE[code]] || "";
        src.textContent = cell && cell.as_of ? `${source} · au ${cell.as_of}` : source;
      }
    });
  };
  if (code === ".") return render(null);
  const key = `${state.view}|${y}|${col.column}`;
  if (state.cellCache.has(key)) return render(state.cellCache.get(key));
  render(null);
  clearTimeout(onCellHover.timer);
  onCellHover.timer = setTimeout(async () => {
    try {
      const response = await fetch(`/api/jobs/${state.job}/cell/${state.view}/${y}/${encodeURIComponent(col.column)}`);
      const cell = await response.json();
      state.cellCache.set(key, cell);
      render(cell);
    } catch (e) { /* infobulle sans valeur */ }
  }, 60);
}

function formatValue(value) {
  if (value === null || value === undefined) return "Vide";
  if (typeof value === "boolean") return value ? "Vrai" : "Faux";
  if (typeof value === "number") return value.toLocaleString("fr-FR", { maximumFractionDigits: 6 });
  return String(value);
}

function renderNotes(data) {
  const notes = $("notes");
  notes.innerHTML = "";
  const sources = (state.result.sources || {})[state.view] || {};
  const nameOf = { v3: "L'API V3", history: "L'historique V4", v4: "L'API V4" };
  for (const [source, info] of Object.entries(sources)) {
    if (info.state === "ok") continue;
    addNote(notes, "warn", `${nameOf[source] || source} n'a pas été utilisable`,
      `${(info.detail || "Accès refusé").replace(/\.?$/, ".")} Les autres sources ont été utilisées normalement.`);
  }

  const attention = Object.entries(data.states).filter(([s]) => STATE_FLAG[s]);
  if (attention.length) {
    const text = attention.map(([s, n]) => `${n} × ${lowerFirst(STATE_TEXT[s])}`).join(" ; ");
    const rows = data.table.filter((r) => STATE_FLAG[r.state]).slice(0, 200);
    const note = addNote(notes, "warn", "Lignes à vérifier", `${text}. Ces lignes restent dans l'Excel, à leur place.`);
    const det = document.createElement("details");
    det.innerHTML = "<summary>Voir les ISIN concernés</summary><ul class=\"cols\"></ul>";
    rows.forEach((r) => {
      const li = document.createElement("li");
      li.textContent = r.isin || "(vide)";
      const span = document.createElement("span");
      span.textContent = ` · ${lowerFirst(STATE_TEXT[r.state])}${r.problem ? " (" + lowerFirst(r.problem) + ")" : ""}`;
      li.append(span);
      det.querySelector("ul").append(li);
    });
    note.append(det);
  }

  if (data.empty_columns.length) {
    const refused = data.empty_columns.filter((c) => c.reason.startsWith("Non autorisé")).length;
    const body = refused
      ? `${refused} relèvent de produits MSCI non couverts par l'abonnement de ce compte ; les autres n'ont simplement pas de valeur publiée pour ces ISIN.`
      : "MSCI n'a publié aucune valeur pour ces ISIN sur ces colonnes.";
    const note = addNote(notes, "", `${data.empty_columns.length} colonne${data.empty_columns.length > 1 ? "s" : ""} MSCI vide${data.empty_columns.length > 1 ? "s" : ""} sur toutes les lignes`, body);
    const det = document.createElement("details");
    det.innerHTML = "<summary>Voir les colonnes</summary><ul class=\"cols\"></ul>";
    data.empty_columns.forEach((c) => {
      const li = document.createElement("li");
      li.textContent = c.column;
      const span = document.createElement("span");
      span.textContent = ` · ${lowerFirst(c.reason)}`;
      li.append(span);
      det.querySelector("ul").append(li);
    });
    note.append(det);
  }
  (state.result.warnings || []).forEach((w) => addNote(notes, "warn", "Avertissement", w));
}

function lowerFirst(text) {
  // Garde les sigles intacts (ISIN, MSCI…)
  return /^[A-Z]{2}/.test(text) ? text : text.charAt(0).toLowerCase() + text.slice(1);
}

function addNote(parent, kind, title, text) {
  const div = document.createElement("div");
  div.className = `note ${kind}`;
  div.innerHTML = "<h3></h3><p></p>";
  div.querySelector("h3").textContent = title;
  div.querySelector("p").textContent = text;
  parent.append(div);
  return div;
}

/* ------------------------------------------------------------------ init */
function bind() {
  document.querySelectorAll(".input [role=tab]").forEach((tab) => {
    tab.addEventListener("click", () => selectEntity(tab.dataset.entity));
    tab.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const next = state.entity === "issuer" ? "fund" : "issuer";
        selectEntity(next);
        $(`tab-${next}`).focus();
      }
    });
  });
  $("isins").addEventListener("input", (e) => {
    state.texts[state.entity] = e.target.value;
    saveLists();
    clearTimeout(bind.timer);
    bind.timer = setTimeout(refreshCheck, 150);
  });
  $("file").addEventListener("change", (e) => {
    if (e.target.files[0]) uploadFile(e.target.files[0]);
    e.target.value = "";
  });
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
  });
  // Un dépôt hors de la zone ne doit pas ouvrir le fichier dans le navigateur.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  $("run").addEventListener("click", run);

  $("conn").addEventListener("click", () => { $("cred-error").textContent = ""; $("dialog").showModal(); $("cid").focus(); });
  $("cred-cancel").addEventListener("click", () => $("dialog").close());
  $("cred-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = $("cred-submit");
    button.disabled = true;
    button.textContent = "Connexion…";
    try {
      state.status = await postJSON("/api/credentials", { client_id: $("cid").value, client_secret: $("secret").value });
      $("secret").value = "";
      $("dialog").close();
      renderStatus();
    } catch (error) {
      $("cred-error").textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = "Se connecter";
    }
  });
  $("demo-on").addEventListener("click", async () => {
    state.status = await postJSON("/api/demo", { enabled: true });
    $("dialog").close();
    renderStatus();
  });
  $("demo-off").addEventListener("click", async () => {
    state.status = await postJSON("/api/demo", { enabled: false });
    renderStatus();
  });

  $("col-canvas").addEventListener("mousemove", onColumnHover);
  $("cell-canvas").addEventListener("mousemove", onCellHover);
  $("col-canvas").addEventListener("mouseleave", hideTip);
  $("cell-canvas").addEventListener("mouseleave", hideTip);
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.result && !$("result").hidden) renderView(); }, 150);
  });
}

async function init() {
  loadLists();
  bind();
  selectEntity(state.entity);
  try {
    state.columns = await (await fetch("/api/columns")).json();
  } catch (e) { /* colonnes rechargées à l'affichage */ }
  await loadStatus();
}

init();
