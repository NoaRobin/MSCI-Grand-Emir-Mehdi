"use strict";

/* =====================================================================
   Extraction MSCI ESG : interface.
   La feuille du template est l'élément central : elle se dessine dès la
   saisie, puis chaque cellule s'allume à l'arrivée de sa valeur, dans la
   couleur de sa source (V4, V3, historique). L'animation montre la cascade.
   ===================================================================== */

const $ = (id) => document.getElementById(id);
const STORE_KEY = "msci-extraction.lists";
const MAX_ROWS_DRAWN = 1500;
const ROW_H = 24;
const COL_H = 40;
const POLL_MS = 400;
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

const ENTITY = {
  issuer: { tab: "Titres", sheet: "Template Import Titres", noun: ["titre", "titres"] },
  fund: { tab: "Fonds", sheet: "Template Import Fonds", noun: ["fonds", "fonds"] },
};
const STATE_TEXT = {
  complete: "Toutes les colonnes remplies",
  partial: "Partiellement rempli",
  reference_only: "Références seules, aucune valeur ESG",
  no_data_returned: "Aucune donnée retournée par MSCI",
  unresolved_identifier: "ISIN inconnu de MSCI",
  ambiguous_identifier: "Plusieurs fonds MSCI pour cet ISIN",
  api_error: "Erreur MSCI sur ce lot",
  invalid_isin: "ISIN invalide, non envoyé",
};
const BAD_STATES = new Set(["no_data_returned", "unresolved_identifier", "ambiguous_identifier", "api_error", "invalid_isin"]);
const STEP_TEXT = { v4: "API V4", resolve: "ISIN → MDSUID", v3: "API V3", history: "Historique", excel: "Excel" };
const STEP_COLOR = { v4: "--src-v4", resolve: "--src-v4", v3: "--src-v3", history: "--src-history", excel: "--primary" };
const SOURCE_OF = { "4": "API V4", c: "Référentiel MSCI", "3": "API V3", h: "Historique V4", i: "Votre saisie" };

const tokens = getComputedStyle(document.documentElement);
const token = (name) => tokens.getPropertyValue(name).trim();
const COLORS = {
  "4": token("--src-v4"), c: token("--src-v4"), "3": token("--src-v3"), h: token("--src-history"),
  i: token("--src-input"), pending: token("--cell-pending"), empty: token("--cell-empty"),
  bar: token("--text-3"), barTrack: token("--line-soft"),
};

const state = {
  entity: "issuer",
  texts: { issuer: "", fund: "" },
  columns: null,
  status: null,
  job: null, // { id, sent: {issuer, fund}, status, result, texts }
  cellCache: new Map(),
};

/* ------------------------------------------------------------------ ISIN */
function isinProblem(code) {
  if (!code) return "Cellule vide";
  if (code.length !== 12) return `${code.length} caractères au lieu de 12`;
  if (!/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(code)) return "Format : 2 lettres, 9 caractères, 1 chiffre";
  const digits = code.split("").map((c) => (/[A-Z]/.test(c) ? String(c.charCodeAt(0) - 55) : c)).join("");
  let total = 0;
  for (let i = 0; i < digits.length; i++) {
    const n = Number(digits[digits.length - 1 - i]) * (i % 2 ? 2 : 1);
    total += Math.floor(n / 10) + (n % 10);
  }
  return total % 10 === 0 ? null : "Clé de contrôle incorrecte";
}

function makeRow(isin, clientId, status, line) {
  const code = String(isin || "").trim().toUpperCase().replace(/\s+/g, "");
  const clean = (v) => (v && v.trim() ? v.trim() : null);
  return { ISIN: code, Client_ID: clean(clientId), Security_Status: clean(status), line, problem: isinProblem(code) };
}

function parseText(text) {
  const rows = [];
  text.split(/\r?\n/).forEach((line, index) => {
    const raw = line.replace(/ /g, " ");
    if (!raw.trim()) return;
    const parts = raw.includes("\t") ? raw.split("\t") : raw.split(/[;|]/);
    if (parts.length === 1) {
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

const rowToLine = (r) => {
  const extra = [r.Client_ID, r.Security_Status];
  while (extra.length && !extra[extra.length - 1]) extra.pop();
  return [r.ISIN, ...extra.map((v) => v || "")].join("\t");
};

/* ------------------------------------------------------------------ utilitaires DOM */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}
const fmt = (n) => Number(n).toLocaleString("fr-FR");
const lowerFirst = (t) => (/^[A-Z]{2}/.test(t) ? t : t.charAt(0).toLowerCase() + t.slice(1));
const plural = (n, [one, many]) => `${fmt(n)} ${n > 1 ? many : one}`;

async function api(url, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* réponse vide */ }
  if (!response.ok) throw new Error(data.error || `Erreur ${response.status}`);
  return data;
}

/* =====================================================================
   Feuille (canvas) : cellules + barres de remplissage par colonne
   ===================================================================== */
const sheet = {
  rows: [], cells: [], previous: [], anims: new Map(), mode: "preview",
  bars: [], barTargets: [], cw: 6, raf: 0,

  setColumns(columns) { this.columns = columns; },

  /** rows: [{isin, name, flag}], cells: [string], mode: preview | live | result */
  render(rows, cells, mode) {
    const sameRows = rows.length === this.rows.length && rows.every((r, i) => r.isin === this.rows[i].isin);
    if (!sameRows || mode === "preview") { this.anims.clear(); this.previous = []; }
    this.rows = rows.slice(0, MAX_ROWS_DRAWN);
    this.mode = mode;
    const n = this.columns.length;
    const now = performance.now();
    this.cells = this.rows.map((_, y) => cells[y] || ".".repeat(n));
    // Nouvelles valeurs : animation décalée de gauche à droite puis de haut en bas.
    if (mode !== "preview" && !REDUCED.matches) {
      this.cells.forEach((line, y) => {
        const before = this.previous[y] || "";
        for (let x = 0; x < n; x++) {
          if (line[x] !== "." && line[x] !== before[x]) {
            this.anims.set(y * n + x, now + x * 4 + Math.min(y, 60) * 10);
          }
        }
      });
    }
    this.previous = this.cells.slice();
    this.barTargets = this.columns.map((_, x) => {
      const filled = this.cells.reduce((sum, line) => sum + (line[x] !== "." ? 1 : 0), 0);
      return this.cells.length ? filled / this.cells.length : 0;
    });
    if (!sameRows || this.bars.length !== n) this.bars = this.barTargets.map((t) => (mode === "preview" ? t : 0));
    this.renderLabels(!sameRows);
    this.layout();
    this.kick();
  },

  renderLabels(rebuild) {
    const box = $("sheet-labels");
    if (rebuild) {
      box.textContent = "";
      box.append(el("div", { class: "head" }, "ISIN · émetteur MSCI"));
      const frag = document.createDocumentFragment();
      this.rows.forEach(() => frag.append(el("div", { class: "row" },
        el("span", { class: "flag" }), el("span", { class: "code" }), el("span", { class: "name" }))));
      box.append(frag);
    }
    const nodes = box.querySelectorAll(".row");
    this.rows.forEach((r, i) => {
      const node = nodes[i];
      node.className = `row ${r.flag || ""}`;
      node.title = r.title || "";
      node.querySelector(".code").textContent = r.isin || "(vide)";
      node.querySelector(".name").textContent = r.name || "";
    });
  },

  layout() {
    const holder = $("sheet-cells");
    const n = this.columns.length;
    const available = Math.max(240, holder.clientWidth - 20);
    this.cw = Math.max(4, Math.min(18, available / n));
    const width = Math.ceil(this.cw * n);
    const dpr = window.devicePixelRatio || 1;
    for (const [canvas, h] of [[$("col-canvas"), COL_H], [$("cell-canvas"), this.rows.length * ROW_H]]) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.max(1, Math.round(h * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${h}px`;
      canvas.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    }
  },

  kick() { if (!this.raf) this.raf = requestAnimationFrame((t) => this.frame(t)); },

  frame(now) {
    this.raf = 0;
    const busy = this.drawCells(now) | this.drawBars();
    if (busy) this.kick();
  },

  drawCells(now) {
    const ctx = $("cell-canvas").getContext("2d");
    const n = this.columns.length;
    const cw = this.cw;
    const gap = cw > 6 ? 2 : 1.5;
    const idle = this.mode === "result" ? COLORS.empty : COLORS.pending;
    let animating = false;
    ctx.clearRect(0, 0, cw * n + 2, this.rows.length * ROW_H);
    this.cells.forEach((line, y) => {
      const top = y * ROW_H + 5;
      const h = ROW_H - 10;
      for (let x = 0; x < n; x++) {
        const code = line[x];
        ctx.fillStyle = idle;
        ctx.fillRect(x * cw, top, cw - gap, h);
        if (code === "." || !code) continue;
        const start = this.anims.get(y * n + x);
        let p = 1;
        if (start !== undefined) {
          p = Math.min(1, Math.max(0, (now - start) / 300));
          if (p >= 1) this.anims.delete(y * n + x); else animating = true;
        }
        const e = 1 - Math.pow(1 - p, 3);
        if (e <= 0) continue;
        const hh = h * (0.35 + 0.65 * e);
        ctx.globalAlpha = e;
        ctx.fillStyle = COLORS[code] || COLORS.i;
        ctx.fillRect(x * cw, top + (h - hh) / 2, cw - gap, hh);
        ctx.globalAlpha = 1;
      }
    });
    return animating;
  },

  drawBars() {
    const ctx = $("col-canvas").getContext("2d");
    const n = this.columns.length;
    const cw = this.cw;
    const gap = cw > 6 ? 2 : 1.5;
    const track = COL_H - 14;
    let moving = false;
    ctx.clearRect(0, 0, cw * n + 2, COL_H);
    for (let x = 0; x < n; x++) {
      const target = this.barTargets[x] || 0;
      let v = this.bars[x] || 0;
      if (REDUCED.matches) v = target;
      else if (Math.abs(target - v) > 0.002) { v += (target - v) * 0.18; moving = true; } else v = target;
      this.bars[x] = v;
      ctx.fillStyle = COLORS.barTrack;
      ctx.fillRect(x * cw, 6, cw - gap, track);
      if (v > 0) {
        ctx.fillStyle = COLORS.bar;
        const h = Math.max(1, track * v);
        ctx.fillRect(x * cw, 6 + track - h, cw - gap, h);
      }
    }
    return moving;
  },

  hit(event, canvas) {
    const x = Math.floor(event.offsetX / this.cw);
    const y = canvas === "cells" ? Math.floor(event.offsetY / ROW_H) : 0;
    if (x < 0 || x >= this.columns.length || y < 0 || (canvas === "cells" && y >= this.rows.length)) return null;
    return { x, y };
  },
};

/* =====================================================================
   Saisie
   ===================================================================== */
function saveLists() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state.texts)); } catch (e) { /* stockage indisponible */ }
}
function loadLists() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    state.texts.issuer = typeof saved.issuer === "string" ? saved.issuer : "";
    state.texts.fund = typeof saved.fund === "string" ? saved.fund : "";
  } catch (e) { /* stockage indisponible */ }
}

function selectEntity(entity) {
  state.entity = entity;
  document.querySelectorAll(".seg [role=tab]").forEach((tab) => {
    const on = tab.dataset.entity === entity;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  });
  document.querySelector(".seg").dataset.active = entity;
  $("isins").value = state.texts[entity];
  state.cellCache.clear();
  refresh();
}

function setText(text) {
  state.texts[state.entity] = text;
  $("isins").value = text;
  saveLists();
  refresh();
}

function renderCheck(rows) {
  const box = $("check");
  box.textContent = "";
  if (!rows.length) return;
  const bad = rows.filter((r) => r.problem);
  const unique = new Set(rows.map((r) => r.ISIN)).size;
  const dup = rows.length - unique;
  const summary = el("div", { class: "summary" },
    el("span", { class: bad.length ? "" : "ok" }, `${plural(rows.length, ["ISIN", "ISIN"])}${bad.length ? "" : " valides"}`));
  if (dup) summary.append(el("span", {}, `${plural(dup, ["doublon", "doublons"])} conservés`));
  if (bad.length) summary.append(el("span", {}, `${fmt(bad.length)} à corriger`));
  box.append(summary);
  if (bad.length) {
    const list = el("ul");
    bad.slice(0, 50).forEach((r) => list.append(el("li", {},
      el("span", {}, el("b", {}, r.ISIN || "(vide)"), ` · ligne ${r.line}`), el("span", {}, r.problem))));
    box.append(list);
  }
  const actions = el("div", { class: "row-actions" });
  if (bad.length) actions.append(el("button", { type: "button", class: "link", onclick: () =>
    setText(rows.filter((r) => !r.problem).map(rowToLine).join("\n")) }, "Retirer les invalides"));
  if (dup) actions.append(el("button", { type: "button", class: "link", onclick: () => {
    const seen = new Set();
    setText(rows.filter((r) => !seen.has(r.ISIN) && seen.add(r.ISIN)).map(rowToLine).join("\n"));
  } }, "Retirer les doublons"));
  actions.append(el("button", { type: "button", class: "link", onclick: () => setText("") },
    `Vider la liste ${ENTITY[state.entity].tab}`));
  box.append(actions);
}

async function uploadFile(file) {
  $("drop").classList.remove("over");
  try {
    const response = await fetch("/api/parse", {
      method: "POST", body: file,
      headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Lecture impossible");
    const target = { issuer: data.issuer, fund: data.fund };
    target[state.entity] = target[state.entity].concat(data.auto);
    const added = { issuer: 0, fund: 0 };
    for (const entity of ["issuer", "fund"]) {
      if (!target[entity].length) continue;
      const current = state.texts[entity].trim();
      state.texts[entity] = (current ? `${current}\n` : "") + target[entity].map(rowToLine).join("\n");
      added[entity] = target[entity].length;
    }
    saveLists();
    const other = state.entity === "issuer" ? "fund" : "issuer";
    selectEntity(!added[state.entity] && added[other] ? other : state.entity);
    $("check").prepend(el("div", { class: "flash" },
      `${file.name} : ${plural(added.issuer, ENTITY.issuer.noun)} et ${plural(added.fund, ENTITY.fund.noun)} ajoutés.`));
  } catch (error) {
    $("check").prepend(el("ul", {}, el("li", {}, `${file.name} : ${error.message}`)));
  }
}

/* =====================================================================
   Rendu global
   ===================================================================== */
function running() { return state.job && state.job.status && state.job.status.state === "running"; }
function stale() {
  return state.job && state.job.result && (state.job.texts.issuer !== state.texts.issuer || state.job.texts.fund !== state.texts.fund);
}

function refresh() {
  const lists = { issuer: parseText(state.texts.issuer), fund: parseText(state.texts.fund) };
  for (const entity of ["issuer", "fund"]) document.querySelector(`[data-count=${entity}]`).textContent = lists[entity].length;
  renderCheck(lists[state.entity]);
  renderRunButton(lists);
  renderSheet(lists);
}

function canRun() { return state.status && (state.status.demo || state.status.configured); }

function renderRunButton(lists) {
  const button = $("run");
  const total = lists.issuer.length + lists.fund.length;
  if (running()) {
    button.disabled = false;
    button.className = "btn ghost-btn";
    button.textContent = "Annuler l'extraction";
    $("run-note").textContent = "Les requêtes en cours se terminent, puis tout s'arrête.";
    return;
  }
  const done = state.job && state.job.result && !stale();
  button.className = done ? "btn ghost-btn" : "btn primary";
  button.textContent = done ? "Relancer l'extraction" : "Extraire les données";
  button.disabled = total === 0 || !canRun();
  $("run-note").textContent = !canRun()
    ? "Connectez-vous à MSCI (en haut à droite) pour lancer l'extraction."
    : total ? `${plural(lists.issuer.length, ENTITY.issuer.noun)} · ${plural(lists.fund.length, ENTITY.fund.noun)}` : "";
}

function renderSheet(lists) {
  if (!state.columns) return;
  const entity = state.entity;
  const columns = state.columns[entity];
  sheet.setColumns(columns);
  $("sheet-title").textContent = ENTITY[entity].sheet;
  const job = state.job;
  const status = job && job.status;
  let rows, cells, mode;

  if (job && !stale() && (running() || job.result) && job.sent[entity] && job.sent[entity].length) {
    if (job.result && job.result.entities[entity]) {
      const table = job.result.entities[entity].table;
      rows = table.map((r) => ({
        isin: r.isin, name: r.name || (BAD_STATES.has(r.state) ? STATE_TEXT[r.state] : ""),
        flag: r.state === "invalid_isin" ? "bad invalid" : BAD_STATES.has(r.state) ? "bad" : r.state === "reference_only" ? "warn" : "",
        title: STATE_TEXT[r.state] + (r.problem ? ` : ${r.problem}` : ""),
      }));
      cells = table.map((r) => r.cells);
      mode = "result";
    } else {
      rows = job.sent[entity].map((r) => ({ isin: r.ISIN, flag: r.problem ? "bad invalid" : "", title: r.problem || "" }));
      cells = (status.live && status.live[entity]) || [];
      mode = "live";
    }
  } else {
    rows = lists[entity].map((r) => ({ isin: r.ISIN, flag: r.problem ? "bad invalid" : "", title: r.problem || "", name: r.problem ? r.problem : "" }));
    cells = [];
    mode = "preview";
  }

  const has = rows.length > 0;
  $("sheet-empty").hidden = has;
  $("sheet").hidden = !has;
  $("sheet-foot").hidden = !has || mode === "preview";
  if (has) sheet.render(rows, cells, mode);

  // Sous-titre
  const n = columns.length;
  if (mode === "preview") {
    $("sheet-sub").textContent = has
      ? `${plural(rows.length, ["ligne", "lignes"])} × ${n} colonnes : prêtes à être remplies.`
      : "La feuille se dessine dès qu'un ISIN est saisi.";
  } else if (mode === "live") {
    $("sheet-sub").textContent = status.stage;
  } else {
    const data = job.result.entities[entity];
    $("sheet-sub").textContent = `${plural(data.rows, ["ligne", "lignes"])} × ${n} colonnes · ${String(job.result.duration).replace(".", ",")} s`;
  }
  if (mode !== "preview") renderFoot(cells, columns, mode);
  renderSteps(mode);
  renderDeliver();
  renderNotes(mode === "result" ? job.result.entities[entity] : null);
}

function renderFoot(cells, columns, mode) {
  const counts = { "4": 0, "3": 0, h: 0, i: 0 };
  const msciIdx = columns.map((c, i) => (c.kind === "msci" ? i : -1)).filter((i) => i >= 0);
  let msciFilled = 0;
  cells.forEach((line) => {
    for (let x = 0; x < line.length; x++) {
      const code = line[x] === "c" ? "4" : line[x];
      if (code in counts) counts[code]++;
    }
    msciIdx.forEach((i) => { if (line[i] && line[i] !== "." && line[i] !== "i") msciFilled++; });
  });
  const total = msciIdx.length * cells.length;
  const legend = $("legend");
  legend.textContent = "";
  [["4", "API V4"], ["3", "API V3"], ["h", "Historique"], ["i", "Saisie"]].forEach(([code, label]) => {
    legend.append(el("li", {}, el("i", { style: `background:${COLORS[code]}` }), label, el("span", { class: "n" }, fmt(counts[code]))));
  });
  legend.append(el("li", {}, el("i", { style: `background:${mode === "result" ? COLORS.empty : COLORS.pending}` }),
    mode === "result" ? "Vide" : "En attente"));
  const rate = total ? Math.round((100 * msciFilled) / total) : 0;
  const line = $("fill-line");
  line.textContent = "";
  line.append(el("strong", {}, `${rate}\u202f%`), ` des cellules MSCI remplies (${fmt(msciFilled)} / ${fmt(total)})`);
}

function renderSteps(mode) {
  const list = $("steps");
  const job = state.job;
  if (mode === "preview" || !job || !job.status || !job.status.plan[state.entity]) { list.hidden = true; return; }
  const status = job.status;
  const plan = status.plan[state.entity];
  const done = new Set(status.done[state.entity] || []);
  const finished = status.state === "done";
  list.hidden = false;
  list.textContent = "";
  const sources = (job.result && job.result.sources[state.entity]) || {};
  plan.forEach((step) => {
    let s = "pending";
    if (finished || done.has(step)) s = "done";
    else if (status.entity === state.entity && status.step === step) s = "active";
    if (finished && (step === "v3" || step === "history") && !sources[step]) s = "skipped";
    if (finished && step === "resolve" && !sources.v4) s = "skipped";
    if (sources[step] && sources[step].state === "refused") s = "refused";
    const item = el("li", { "data-state": s, style: `--step-color: var(${STEP_COLOR[step]})` },
      el("span", { class: "pip" }), STEP_TEXT[step]);
    if (s === "active") item.setAttribute("aria-current", "step");
    if (s === "skipped") item.title = "Rien à compléter pour cette source";
    if (s === "refused") item.title = "Accès refusé par MSCI pour cette source";
    list.append(item);
  });
}

function renderDeliver() {
  const job = state.job;
  const box = $("deliver");
  if (!job || !job.result) { box.hidden = true; return; }
  const parts = Object.entries(job.result.entities).map(([e, d]) => plural(d.rows, ENTITY[e].noun));
  $("deliver-title").textContent = stale()
    ? "La liste a changé depuis cette extraction"
    : job.result.demo ? "Fichier de démonstration prêt" : "Fichier prêt";
  $("deliver-sub").textContent = stale()
    ? "Le fichier ci-contre correspond à l'ancienne liste. Relancez pour le mettre à jour."
    : `${parts.join(" et ")} · feuilles « Template Import Titres » et « Template Import Fonds »`;
  $("download").href = `/api/jobs/${job.id}/excel`;
  $("report").href = `/api/jobs/${job.id}/report`;
  $("download").className = stale() ? "btn ghost-btn" : "btn primary";
  box.hidden = false;
}

function renderNotes(data) {
  const notes = $("notes");
  notes.textContent = "";
  if (!data) return;
  const note = (kind, title, text) => {
    const n = el("div", { class: `note ${kind}` }, el("h3", {}, title), el("p", {}, text));
    notes.append(n);
    return n;
  };
  const details = (summary, items) => {
    const list = el("ul", { class: "cols" });
    items.forEach(([code, text]) => list.append(el("li", {}, el("code", {}, code), el("span", {}, ` · ${text}`))));
    return el("details", {}, el("summary", {}, summary), list);
  };
  const sources = state.job.result.sources[state.entity] || {};
  const nameOf = { v4: "L'API V4", v3: "L'API V3", history: "L'historique V4" };
  for (const [source, info] of Object.entries(sources)) {
    if (info.state === "refused") {
      note("warn", `${nameOf[source]} a refusé l'accès`, `${String(info.detail || "Accès refusé").replace(/\.?$/, ".")} Les autres sources ont été utilisées.`);
    } else if (info.state === "partial") {
      note("bad", `${nameOf[source]} : ${plural(info.errors, ["lot", "lots"])} en erreur`, `${String(info.detail).replace(/\.?$/, ".")} Les lignes concernées sont signalées ; relancez pour réessayer (les lots réussis viennent du cache).`);
    }
  }
  const attention = Object.entries(data.states).filter(([s]) => BAD_STATES.has(s) || s === "reference_only");
  if (attention.length) {
    const text = attention.map(([s, n]) => `${fmt(n)} × ${lowerFirst(STATE_TEXT[s])}`).join(" ; ");
    const rows = data.table.filter((r) => BAD_STATES.has(r.state) || r.state === "reference_only").slice(0, 300);
    note("warn", "Lignes à vérifier", `${text}. Elles restent dans l'Excel, à leur place.`)
      .append(details("Voir les ISIN", rows.map((r) => [r.isin || "(vide)", lowerFirst(STATE_TEXT[r.state]) + (r.problem ? ` (${r.problem})` : "")])));
  }
  if (data.empty_columns.length) {
    const refused = data.empty_columns.filter((c) => c.reason.startsWith("Non autorisé")).length;
    note("", `${plural(data.empty_columns.length, ["colonne MSCI vide", "colonnes MSCI vides"])} sur toutes les lignes`,
      refused ? `${fmt(refused)} relèvent de produits non couverts par l'abonnement ; les autres n'ont pas de valeur publiée pour ces ISIN.`
        : "MSCI n'a publié aucune valeur pour ces ISIN sur ces colonnes.")
      .append(details("Voir les colonnes", data.empty_columns.map((c) => [c.column, lowerFirst(c.reason)])));
  }
  (state.job.result.warnings || []).forEach((w) => note("warn", "Avertissement", w));
}

/* =====================================================================
   Infobulles
   ===================================================================== */
function showTip(event, build) {
  const tip = $("tip");
  tip.textContent = "";
  build(tip);
  tip.hidden = false;
  const box = $("sheet").getBoundingClientRect();
  let x = event.clientX - box.left + 14;
  let y = event.clientY - box.top + 16;
  if (x + tip.offsetWidth > box.width - 6) x = event.clientX - box.left - tip.offsetWidth - 14;
  if (y + tip.offsetHeight > box.height - 6) y = event.clientY - box.top - tip.offsetHeight - 12;
  tip.style.left = `${Math.max(4, x)}px`;
  tip.style.top = `${Math.max(4, y)}px`;
}
const hideTip = () => { $("tip").hidden = true; };

function describeColumn(col) {
  if (col.kind === "input") return "Recopiée depuis votre fichier";
  if (col.kind === "input_isin") return "ISIN saisi";
  if (col.kind === "blocked") return "Non alimentée (correspondance à valider)";
  return col.label !== col.column ? col.label : `Codes : ${col.candidates.join(", ")}`;
}

function onColumnHover(event) {
  const hit = sheet.hit(event, "cols");
  if (!hit) return hideTip();
  const col = sheet.columns[hit.x];
  const filled = sheet.cells.reduce((s, line) => s + (line[hit.x] !== "." ? 1 : 0), 0);
  showTip(event, (tip) => tip.append(el("b", {}, col.column), el("div", { class: "muted" }, describeColumn(col)),
    el("div", {}, `Remplie pour ${fmt(filled)} / ${plural(sheet.cells.length, ["ligne", "lignes"])}`)));
}

let hoverToken = 0;
function onCellHover(event) {
  const hit = sheet.hit(event, "cells");
  if (!hit) return hideTip();
  const col = sheet.columns[hit.x];
  const row = sheet.rows[hit.y];
  const code = sheet.cells[hit.y][hit.x];
  const token = ++hoverToken;
  const draw = (cell) => {
    if (token !== hoverToken) return;
    showTip(event, (tip) => {
      tip.append(el("b", {}, col.column), el("div", { class: "muted" }, `${row.isin}${row.name ? " · " + row.name : ""}`));
      if (code === ".") {
        tip.append(el("div", { class: "val" }, sheet.mode === "result" ? "Vide" : "En attente"),
          el("div", { class: "muted" }, describeColumn(col)));
        return;
      }
      tip.append(el("div", { class: "val" }, cell ? formatValue(cell.value) : sheet.mode === "result" ? "…" : "Reçue"),
        el("div", { class: "src" }, el("i", { style: `background:${COLORS[code]}` }),
          `${SOURCE_OF[code] || ""}${cell && cell.as_of ? ` · au ${cell.as_of}` : ""}`));
    });
  };
  if (code === "." || sheet.mode !== "result") return draw(null);
  const key = `${state.entity}|${hit.y}|${col.column}`;
  if (state.cellCache.has(key)) return draw(state.cellCache.get(key));
  draw(null);
  clearTimeout(onCellHover.timer);
  onCellHover.timer = setTimeout(async () => {
    try {
      const cell = await api(`/api/jobs/${state.job.id}/cell/${state.entity}/${hit.y}/${encodeURIComponent(col.column)}`);
      state.cellCache.set(key, cell);
      draw(cell);
    } catch (e) { /* infobulle sans valeur */ }
  }, 50);
}

function formatValue(value) {
  if (value === null || value === undefined) return "Vide";
  if (typeof value === "boolean") return value ? "Vrai" : "Faux";
  if (typeof value === "number") return value.toLocaleString("fr-FR", { maximumFractionDigits: 6 });
  return String(value);
}

/* =====================================================================
   Extraction
   ===================================================================== */
async function onRunClick() {
  if (running()) {
    $("run").disabled = true;
    try { await api(`/api/jobs/${state.job.id}/cancel`, {}); } catch (e) { /* l'état suivant le dira */ }
    return;
  }
  // Les lignes sans ISIN ne sont pas envoyées (le serveur les ignorerait) : l'aperçu reste aligné.
  const keep = (rows) => rows.filter((r) => r.ISIN);
  const sent = { issuer: keep(parseText(state.texts.issuer)), fund: keep(parseText(state.texts.fund)) };
  const strip = (rows) => rows.map(({ ISIN, Client_ID, Security_Status }) => ({ ISIN, Client_ID, Security_Status }));
  const payload = {
    issuer: strip(sent.issuer), fund: strip(sent.fund),
    options: {
      use_v3: $("opt-v3").checked, use_history: $("opt-history").checked,
      inherit: $("opt-inherit").checked, use_cache: $("opt-cache").checked,
    },
  };
  $("failure").hidden = true;
  state.cellCache.clear();
  try {
    const { id } = await api("/api/run", payload);
    state.job = { id, sent, texts: { ...state.texts }, result: null,
      status: { state: "running", stage: "Connexion à MSCI", plan: {}, done: {}, live: {} } };
    if (!sent[state.entity].length) selectEntity(sent.issuer.length ? "issuer" : "fund"); else refresh();
    if (window.matchMedia("(max-width: 960px)").matches) $("sheet-panel").scrollIntoView({ behavior: REDUCED.matches ? "auto" : "smooth" });
    poll(id);
  } catch (error) {
    showFailure(error.message);
  }
}

async function poll(id) {
  if (!state.job || state.job.id !== id) return;
  try {
    const status = await api(`/api/jobs/${id}`);
    if (!state.job || state.job.id !== id) return;
    state.job.status = status;
    if (status.state === "running") {
      refresh();
      setTimeout(() => poll(id), POLL_MS);
      return;
    }
    if (status.state === "done") {
      state.job.result = status.result;
    } else {
      const job = state.job;
      state.job = null;
      if (status.state === "failed") showFailure(status.error);
      else if (job) showFailure("Extraction annulée. Rien n'a été écrit ; relancez quand vous voulez.", "Annulée");
    }
    refresh();
  } catch (error) {
    state.job = null;
    showFailure(error.message);
    refresh();
  }
}

function showFailure(message, title = "L'extraction n'a pas abouti.") {
  const box = $("failure");
  box.classList.toggle("info", title === "Annulée");
  box.textContent = "";
  box.append(el("strong", {}, title), message);
  box.hidden = false;
}

/* =====================================================================
   Connexion
   ===================================================================== */
async function loadStatus() {
  try { state.status = await api("/api/status"); } catch (e) {
    state.status = { configured: false, demo: false, error: "Serveur local injoignable" };
  }
  renderStatus();
}

function renderStatus() {
  const s = state.status;
  const conn = $("conn");
  if (s.demo) { conn.dataset.state = "demo"; $("conn-label").textContent = "Démonstration"; }
  else if (s.configured) { conn.dataset.state = "ok"; $("conn-label").textContent = s.connected ? "Connecté à MSCI" : "Identifiants chargés"; }
  else { conn.dataset.state = "off"; $("conn-label").textContent = "Se connecter à MSCI"; }
  $("demo-banner").hidden = !s.demo;
  renderAccess();
  refresh();
}

function renderAccess() {
  const box = $("access");
  const access = state.status && state.status.access;
  box.textContent = "";
  if (!access) { box.hidden = true; return; }
  const line = (label, ok, detail) => el("div", {}, el("span", {}, label),
    el("strong", { class: ok === true ? "yes" : ok === false ? "no" : "" }, detail));
  const products = (list) => (list.length ? plural(list.length, ["produit", "produits"]) : "aucun produit");
  box.append(line("Titres (V4)", access.issuer.length > 0, products(access.issuer)),
    line("Fonds (V4)", access.fund.length > 0, products(access.fund)),
    line("API V3", access.v3, access.v3 === true ? "accessible" : access.v3 === false ? "refusée" : "non vérifiée"));
  const all = [...access.issuer, ...access.fund];
  if (all.length) box.title = all.join("\n");
  box.hidden = false;
}

/* =====================================================================
   Initialisation
   ===================================================================== */
function bind() {
  document.querySelectorAll(".seg [role=tab]").forEach((tab) => {
    tab.addEventListener("click", () => selectEntity(tab.dataset.entity));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const next = state.entity === "issuer" ? "fund" : "issuer";
      selectEntity(next);
      $(`tab-${next}`).focus();
    });
  });
  $("isins").addEventListener("input", (event) => {
    state.texts[state.entity] = event.target.value;
    saveLists();
    clearTimeout(bind.timer);
    bind.timer = setTimeout(refresh, 160);
  });
  $("file").addEventListener("change", (event) => {
    if (event.target.files[0]) uploadFile(event.target.files[0]);
    event.target.value = "";
  });
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  $("run").addEventListener("click", onRunClick);

  $("conn").addEventListener("click", () => {
    $("cred-error").textContent = "";
    renderAccess();
    $("dialog").showModal();
    $("cid").focus();
  });
  $("cred-cancel").addEventListener("click", () => $("dialog").close());
  $("cred-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("cred-submit");
    button.disabled = true;
    button.textContent = "Connexion…";
    try {
      state.status = await api("/api/credentials", { client_id: $("cid").value, client_secret: $("secret").value });
      $("secret").value = "";
      renderStatus();
    } catch (error) {
      $("cred-error").textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = "Se connecter";
    }
  });
  $("demo-on").addEventListener("click", async () => {
    state.status = await api("/api/demo", { enabled: true });
    $("dialog").close();
    renderStatus();
  });
  $("demo-off").addEventListener("click", async () => {
    state.status = await api("/api/demo", { enabled: false });
    renderStatus();
  });

  $("col-canvas").addEventListener("mousemove", onColumnHover);
  $("cell-canvas").addEventListener("mousemove", onCellHover);
  $("col-canvas").addEventListener("mouseleave", hideTip);
  $("cell-canvas").addEventListener("mouseleave", hideTip);
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (!$("sheet").hidden) { sheet.layout(); sheet.kick(); } }, 120);
  });
}

async function init() {
  loadLists();
  bind();
  document.querySelector(".seg").dataset.active = state.entity;
  $("isins").value = state.texts[state.entity];
  try { state.columns = await api("/api/columns"); } catch (e) {
    showFailure("Impossible de charger les colonnes du template : redémarrez l'application.");
  }
  await loadStatus();
}

init();
