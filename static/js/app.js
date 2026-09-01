"use strict";

/* ==========================================================================
   Constants & state
   ========================================================================== */
const TYPES = {heading:"--t-heading", paragraph:"--t-paragraph", list:"--t-list",
  table:"--t-table", caption:"--t-caption", safety:"--t-safety"};
const CT_COLOR = {procedure:"--t-list", safety:"--t-safety", section:"--t-heading", page:"--t-other"};

const state = {
  doc: null,          // chunks JSON payload
  base: "",           // e.g. /files/<job_id>/<doc_stem>
  jobId: null,
  docStem: null,
  summary: null,      // { mode, can_enhance, engine, page_classification, ... }
  page: 1,
  sel: null,
  showBoxes: true,
  dim: false,
  zoom: 1,
  disp: null,
  boxMode: "chunk",
  enhancing: false,
};

const $ = id => document.getElementById(id);
const colorFor = t => `var(${TYPES[t] || "--t-other"})`;
const ctColor = t => `var(${CT_COLOR[t] || "--t-other"})`;
const esc = s => String(s ?? "").replace(/[&<>"]/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));

/* ==========================================================================
   Toasts
   ========================================================================== */
function toast(message, kind = "info", ms = 4200) {
  const host = $("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" ? "err" : kind === "success" ? "ok" : kind === "warn" ? "warn" : ""}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  }, ms);
}

/* ==========================================================================
   API helpers
   ========================================================================== */
async function apiFetch(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (netErr) {
    throw new Error("Network error — is the server running?");
  }
  if (!res.ok) {
    let body = {};
    try { body = await res.json(); } catch { /* non-JSON error body */ }
    const err = new Error(body.detail || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = body.error;
    throw err;
  }
  return res.json();
}

function jobIdFromBaseUrl(baseUrl) {
  // base_url looks like "/files/<job_id>/<doc_stem>"
  const parts = baseUrl.split("/").filter(Boolean);
  return parts[1] || null;
}

/* ==========================================================================
   Upload flow
   ========================================================================== */
const drop = $("drop"), fileInput = $("file");
$("pick").onclick = () => fileInput.click();
fileInput.onchange = () => { if (fileInput.files[0]) upload(fileInput.files[0]); };
["dragover", "dragenter"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hot"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); e.stopPropagation(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", e => { const f = e.dataTransfer?.files?.[0]; if (f) upload(f); });
drop.addEventListener("click", e => {
  if (!e.target.closest("button")) fileInput.click();
});

// Browsers only deliver file drops when dragover is cancelled. Handle this at
// document level too, so nested elements and the edges of the upload panel do
// not accidentally navigate the browser to the dropped file.
document.addEventListener("dragover", e => {
  if ($("upload").style.display !== "none" && e.dataTransfer?.types?.includes("Files")) {
    e.preventDefault();
    drop.classList.add("hot");
  }
});
document.addEventListener("dragleave", e => {
  if (!e.relatedTarget) drop.classList.remove("hot");
});
document.addEventListener("drop", e => {
  if ($("upload").style.display === "none") return;
  e.preventDefault();
  drop.classList.remove("hot");
  const f = e.dataTransfer?.files?.[0];
  if (f) upload(f);
});

function renderUploadSteps(activeStep = null, failed = false) {
  document.querySelectorAll(".up-step[data-step]").forEach(el => {
    const step = Number(el.dataset.step);
    el.classList.toggle("done", activeStep !== null && step < activeStep);
    el.classList.toggle("active", !failed && activeStep !== null && step === activeStep && step < 4);
    el.classList.toggle("failed", failed && activeStep !== null && step === Math.min(activeStep, 3));
  });
}

function newClientJobId() {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

async function upload(file) {
  $("status").innerHTML = "";
  $("bar").classList.add("on");
  $("status").textContent = `Processing “${file.name}” — converting & parsing…`;
  renderUploadSteps(0);
  const clientJobId = newClientJobId();
  const fd = new FormData();
  fd.append("file", file);
  fd.append("client_job_id", clientJobId);
  let stopped = false;
  const pollProgress = async () => {
    while (!stopped) {
      try {
        const progress = await apiFetch(`/api/ingest-status/${clientJobId}`);
        renderUploadSteps(progress.status === "complete" ? 4 : progress.active_step);
      } catch { /* ingestion response remains the source of errors */ }
      await new Promise(resolve => setTimeout(resolve, 400));
    }
  };
  const polling = pollProgress();
  try {
    const out = await apiFetch("/api/ingest", { method: "POST", body: fd });
    applyDocument(out);
    renderUploadSteps(4);
    $("bar").classList.remove("on");
    $("status").textContent = "";
    $("upload").style.display = "none";
    $("app").classList.add("on");
    renderAll();
  } catch (err) {
    const active = [...document.querySelectorAll(".up-step.active")][0];
    renderUploadSteps(active ? Number(active.dataset.step) : 0, true);
    $("bar").classList.remove("on");
    $("status").innerHTML = `<span class="err">Could not process file: ${esc(err.message)}</span>`;
    toast(err.message, "error");
  } finally {
    stopped = true;
    await polling;
  }
}

function applyDocument(payload) {
  state.doc = payload.data;
  state.base = payload.base_url;
  state.summary = payload.summary;
  state.jobId = payload.summary?.job_id || jobIdFromBaseUrl(payload.base_url);
  state.docStem = payload.summary?.doc_stem || null;
  state.page = (payload.data.pages?.[0]?.page) || 1;
  state.sel = null;
}

$("newdoc").onclick = () => {
  $("app").classList.remove("on");
  $("upload").style.display = "";
  fileInput.value = "";
  state.doc = null;
  state.summary = null;
  state.jobId = null;
  renderUploadSteps(null);
};

/* ==========================================================================
   Enhanced Mode button
   ========================================================================== */
const enhanceBtn = $("enhanceBtn");
const enhanceSpinner = $("enhanceSpinner");
const enhanceLabel = enhanceBtn.querySelector(".lbl");

enhanceBtn.onclick = () => enhancePages(null); // null -> server default: all pending pages

async function enhancePages(pages) {
  if (state.enhancing || !state.jobId) return;
  const s = state.summary || {};
  if (!s.can_enhance) return; // nothing pending, button should be disabled anyway

  state.enhancing = true;
  enhanceBtn.disabled = true;
  enhanceSpinner.hidden = false;
  enhanceLabel.textContent = pages ? `Enhancing p.${pages.join(",")}…` : "Enhancing…";
  $("stageLoading").hidden = false;

  try {
    const body = pages ? { pages } : {};
    const out = await apiFetch(`/api/enhance/${state.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    applyDocument(out);
    renderAll();
    toast(pages
      ? `Re-OCR'd page(s) ${pages.join(", ")} — now in Enhanced Mode.`
      : "Re-OCR'd the remaining scanned page(s) — now in Enhanced Mode.", "success");
  } catch (err) {
    if (err.status === 409) {
      toast("Already fully processed in Enhanced Mode.", "warn");
    } else if (err.status === 503) {
      toast(err.message, "error", 7000);
    } else {
      toast(`Enhanced Mode failed: ${err.message}`, "error", 6000);
    }
  } finally {
    state.enhancing = false;
    enhanceSpinner.hidden = true;
    $("stageLoading").hidden = true;
    renderEnhanceButton();
  }
}

function renderEnhanceButton() {
  const s = state.summary || {};
  enhanceSpinner.hidden = true;
  const scannedCount = (s.scanned_page_numbers || []).length;
  const pendingCount = (s.pending_enhance_pages || []).length;
  if (s.can_enhance) {
    enhanceBtn.disabled = false;
    enhanceBtn.classList.remove("done");
    enhanceLabel.textContent = "Enhanced Mode";
    enhanceBtn.title = scannedCount > pendingCount
      ? `Re-run the ${pendingCount} remaining scanned page(s) in Enhanced Mode`
      : "Not happy with the OCR result? Re-run this document's scanned pages in Enhanced Mode";
  } else if (scannedCount > 0) {
    // every scanned page already went through Enhanced Mode -- nothing left to do
    enhanceBtn.disabled = true;
    enhanceBtn.classList.add("done");
    enhanceLabel.textContent = "Enhanced Mode";
    enhanceBtn.title = "Already processed in Enhanced Mode";
  } else {
    // no scanned pages at all -- this document never needed OCR
    enhanceBtn.disabled = true;
    enhanceBtn.classList.remove("done");
    enhanceLabel.textContent = "Enhanced Mode";
    enhanceBtn.title = "Nothing to enhance — this document had no OCR'd (scanned) pages";
  }
}

/* ==========================================================================
   Download button
   ========================================================================== */
$("downloadBtn").onclick = () => {
  if (!state.jobId) return;
  const a = document.createElement("a");
  a.href = `/api/export/${state.jobId}`;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast("Preparing download…", "info", 2000);
};

/* ==========================================================================
   Inspector controls
   ========================================================================== */
$("tgboxes").onclick = e => { state.showBoxes = !state.showBoxes; e.target.classList.toggle("on", state.showBoxes); drawPage(); };
$("tgdetail").onclick = e => {
  state.boxMode = state.boxMode === "chunk" ? "region" : "chunk";
  e.target.classList.toggle("on", state.boxMode === "region");
  renderLegend(); drawPage();
};
$("tgdim").onclick = e => { state.dim = !state.dim; e.target.classList.toggle("on", state.dim); drawPage(); };
function setZoom(z) {
  state.zoom = Math.min(3.5, Math.max(.4, z));
  $("zlabel").textContent = Math.abs(state.zoom - 1) < .01 ? "Fit" : Math.round(state.zoom * 100) + "%";
  drawPage();
}
$("zin").onclick = () => setZoom(state.zoom + .2);
$("zout").onclick = () => setZoom(state.zoom - .2);
$("zfit").onclick = () => setZoom(1);
$("chunksearch").addEventListener("input", () => { if (state.doc) renderList(); });

/* ==========================================================================
   Rendering
   ========================================================================== */
function renderAll() {
  renderDocMeta(); renderFileInfo(); renderEnhanceButton(); renderLegend(); renderRail(); renderList(); drawPage();
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function renderFileInfo() {
  const d = state.doc || {}, s = state.summary || {}, p = d.parse || {};
  const words = (d.chunks || []).reduce((total, chunk) => {
    const matches = String(chunk.text || "").trim().match(/\S+/g);
    return total + (matches ? matches.length : 0);
  }, 0);
  const pages = d.total_pages || (d.pages || []).length || 0;
  const engines = Object.values(s.page_engines || p.page_engines || {});
  const effectiveMode = engines.includes("lighton") ? "enhanced"
    : engines.length ? "normal" : (s.mode || p.mode || "normal");
  const mode = effectiveMode === "enhanced" ? "Enhanced · LightOn OCR" : "Normal · Tesseract";
  const items = [
    ["File", d.source_file || s.source_file || "—"],
    ["Size", formatBytes(s.file_size_bytes)],
    ["Pages", pages.toLocaleString()],
    ["Words", words.toLocaleString()],
    ["Chunks", (d.num_chunks ?? s.num_chunks ?? 0).toLocaleString()],
    ["OCR", mode],
  ];
  $("fileinfo").innerHTML = items.map(([label, value]) =>
    `<div class="fileinfo-item"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`
  ).join("");
}

function renderDocMeta() {
  const d = state.doc, p = d.parse || {}, s = state.summary || {};
  const cls = p.page_classification || "—";
  const engines = Object.values(s.page_engines || p.page_engines || {});
  const mode = engines.includes("lighton") ? "enhanced"
    : engines.length ? "normal" : (s.mode || p.mode || "normal");
  const modeLabel = mode === "enhanced" ? "Enhanced Mode" : "Normal Mode";
  // Some source formats hand back a title like "Microsoft Word - <name>.docx" --
  // that's an internal artifact of how the file was read, not something the
  // person should see in the UI, so strip it down to just the document name.
  const rawTitle = d.doc_title || d.doc_id || "document";
  const title = String(rawTitle).replace(/^Microsoft\s+(Word|PowerPoint|Excel|Office)\s*-\s*/i, "");
  $("docmeta").innerHTML =
    `<span class="title">${esc(title || "document")}</span>` +
    `<span class="tag">${esc(d.source_file || s.source_file || "")}</span>` +
    `<span class="tag">${d.num_chunks ?? s.num_chunks ?? 0} chunks</span>` +
    `<span class="tag ${cls !== "digital" ? "on" : ""}">${esc(cls)}</span>` +
    `<span class="tag mode-${mode}">${esc(modeLabel)}</span>`;
}

const pageMeta = n => (state.doc.pages || []).find(p => p.page === n) || null;
function pagesPresent() {
  const s = new Set();
  (state.doc.pages || []).forEach(p => s.add(p.page));
  (state.doc.chunks || []).forEach(c => (c.regions || []).forEach(r => s.add(r.page)));
  return [...s].sort((a, b) => a - b);
}
const scannedPages = () => new Set((state.doc.parse || {}).scanned_page_numbers || []);

function renderRail() {
  const el = $("rail"); el.innerHTML = "";
  const scans = scannedPages();
  const pending = new Set((state.summary || {}).pending_enhance_pages || []);
  pagesPresent().forEach(n => {
    const b = document.createElement("button");
    b.textContent = n;
    b.className = (n === state.page ? "sel" : "");
    if (scans.has(n)) b.insertAdjacentHTML("beforeend", `<span class="dot scan" title="scanned / OCR'd"></span>`);
    b.onclick = () => { state.page = n; renderRail(); drawPage(); $("stage").scrollTo({ top: 0, left: 0 }); };
    // Only pages that are scanned AND not yet on LightOnOCR get a per-page
    // enhance affordance -- clicking it enhances just this one page instead
    // of every pending scanned page in the document.
    if (scans.has(n) && pending.has(n)) {
      const tag = document.createElement("span");
      tag.className = "dot enhance-page";
      tag.title = `Enhance page ${n} only`;
      tag.onclick = (ev) => { ev.stopPropagation(); enhancePages([n]); };
      b.appendChild(tag);
    }
    el.appendChild(b);
  });
}

function renderList() {
  const el = $("list"); el.innerHTML = "";
  const q = (($("chunksearch") || {}).value || "").trim().toLowerCase();
  let shown = 0;
  (state.doc.chunks || []).forEach(c => {
    if (q) {
      const hay = ((c.section_title || "") + " " + (c.section_number || "") + " " + (c.text || "")).toLowerCase();
      if (!hay.includes(q)) return;
    }
    shown++;
    const color = ctColor(c.metadata?.content_type);
    const div = document.createElement("div");
    div.className = "chunk" + (state.sel === c.chunk_id ? " sel" : "");
    div.style.borderLeftColor = color;
    const m = c.metadata || {};
    const pages = m.page_start === m.page_end ? `p${m.page_start}` : `p${m.page_start}–${m.page_end}`;
    const body = (c.text || "").split("\n").slice(1).join("\n") || c.text || "";
    div.innerHTML =
      `<div class="row"><span class="sec">${esc(c.section_number || "")}</span>` +
      `<span class="ttl">${esc(c.section_title || "")}</span>` +
      `<span class="ct" style="background:${color}">${esc(m.content_type || "")}</span></div>` +
      `<div class="snip">${esc(body)}</div>` +
      `<div class="meta"><span>${pages}</span><span>${(c.regions || []).length} boxes</span>` +
      `<span>${(c.images || []).length} img</span><span>~${m.token_estimate || 0} tok</span></div>`;
    div.onclick = () => selectChunk(c.chunk_id);
    el.appendChild(div);
  });
  if (q && shown === 0) {
    const none = document.createElement("div");
    none.style.cssText = "color:var(--mute);font-size:12px;padding:10px";
    none.textContent = `No chunks match “${q}”.`;
    el.appendChild(none);
  }
}

function selectChunk(id) {
  state.sel = (state.sel === id ? null : id);
  const c = (state.doc.chunks || []).find(x => x.chunk_id === id);
  if (c && state.sel) {
    const first = (c.regions || [])[0];
    if (first && first.page !== state.page) { state.page = first.page; renderRail(); }
  }
  renderList(); drawPage();
  if (c && state.sel) {
    const card = $("list").querySelector(".chunk.sel");
    if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const box = state.boxMode === "chunk" ? chunkUnion(c, state.page) : ((c.regions || [])[0]?.bbox);
    if (box) requestAnimationFrame(() => focusBox(box));
  }
}

function focusBox(bbox) {
  const d = state.disp; if (!d) return;
  const stage = $("stage"), wrap = $("pagewrap");
  const [x0, , x1] = bbox;
  const topPx = (d.Hp - bbox[3]) / d.Hp * d.dispH, hPx = (bbox[3] - bbox[1]) / d.Hp * d.dispH;
  const leftPx = x0 / d.Wp * d.dispW, wPx = (x1 - x0) / d.Wp * d.dispW;
  const wRect = wrap.getBoundingClientRect(), sRect = stage.getBoundingClientRect();
  const boxTop = (wRect.top - sRect.top) + stage.scrollTop + topPx;
  const boxLeft = (wRect.left - sRect.left) + stage.scrollLeft + leftPx;
  stage.scrollTo({
    top: boxTop - stage.clientHeight / 2 + hPx / 2,
    left: Math.max(0, boxLeft - stage.clientWidth / 2 + wPx / 2), behavior: "smooth"
  });
  const rect = $("overlay").querySelector("rect.sel");
  if (rect) { rect.classList.add("flash"); setTimeout(() => rect.classList.remove("flash"), 1100); }
}

function collectRegions(page) {
  const out = [];
  (state.doc.chunks || []).forEach(c => (c.regions || []).forEach(r => { if (r.page === page) out.push({ ...r, chunk: c.chunk_id }); }));
  return out;
}

function renderLegend() {
  const el = $("legend");
  if (state.boxMode === "chunk") {
    const seen = new Set();
    (state.doc.chunks || []).forEach(c => seen.add(c.metadata?.content_type || "section"));
    el.innerHTML = [...seen].sort().map(t => `<span><i style="border-color:${ctColor(t)}"></i>${t}</span>`).join("");
  } else {
    const seen = new Set();
    (state.doc.chunks || []).forEach(c => (c.regions || []).forEach(r => seen.add(r.type || "other")));
    el.innerHTML = [...seen].sort().map(t => `<span><i style="border-color:${colorFor(t)}"></i>${t}</span>`).join("");
  }
}

function chunkUnion(c, page) {
  const rs = (c.regions || []).filter(r => r.page === page);
  if (!rs.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  rs.forEach(r => { x0 = Math.min(x0, r.bbox[0]); y0 = Math.min(y0, r.bbox[1]); x1 = Math.max(x1, r.bbox[2]); y1 = Math.max(y1, r.bbox[3]); });
  return [x0, y0, x1, y1];
}

function drawPage() {
  const meta = pageMeta(state.page), wrap = $("pagewrap"), img = $("pageimg"), svg = $("overlay");
  const regions = collectRegions(state.page);
  let Wp = meta?.width_pt, Hp = meta?.height_pt;
  if (!Wp || !Hp) { let mx = 612, my = 792; regions.forEach(r => { mx = Math.max(mx, r.bbox[2]); my = Math.max(my, r.bbox[3]); }); Wp = mx; Hp = my; }
  const avail = Math.max(320, $("stage").clientWidth - 56);
  const dispW = avail * state.zoom, dispH = dispW * (Hp / Wp);
  wrap.style.width = dispW + "px"; wrap.style.height = dispH + "px";
  state.disp = { Wp, Hp, dispW, dispH };
  if (meta?.image) { img.src = state.base + "/" + meta.image; img.style.display = ""; }
  else { img.removeAttribute("src"); img.style.display = "none"; }
  svg.setAttribute("viewBox", `0 0 ${Wp} ${Hp}`); svg.innerHTML = "";
  if (!state.showBoxes) return;
  const NS = "http://www.w3.org/2000/svg";
  const put = (bbox, stroke, { chunk, text, label, selectable = true } = {}) => {
    const [x0, y0, x1, y1] = bbox;
    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("x", x0); rect.setAttribute("y", Hp - y1);
    rect.setAttribute("width", Math.max(0, x1 - x0)); rect.setAttribute("height", Math.max(0, y1 - y0));
    rect.setAttribute("rx", "2"); rect.setAttribute("stroke", stroke);
    if (state.sel && chunk) {
      const cls = chunk === state.sel ? "sel" : (state.dim ? "dim" : "");
      if (cls) rect.classList.add(cls);
    }
    if (selectable) {
      rect.style.cursor = "pointer";
      rect.addEventListener("mousemove", ev => showHud(ev, { type: label ? "chunk" : "region", page: state.page, bbox, text }));
      rect.addEventListener("mouseleave", hideHud);
      rect.addEventListener("click", () => selectChunk(chunk));
    }
    svg.appendChild(rect);
    if (label) {
      const fs = Math.max(9, Math.min(13, (x1 - x0) / 8));
      const pad = fs * 0.35, tw = label.length * fs * 0.62 + pad * 2;
      const bg = document.createElementNS(NS, "rect");
      bg.setAttribute("x", x0); bg.setAttribute("y", Hp - y1); bg.setAttribute("width", tw);
      bg.setAttribute("height", fs + pad * 2); bg.setAttribute("fill", stroke); bg.setAttribute("rx", "1.5");
      bg.style.pointerEvents = "none"; svg.appendChild(bg);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", x0 + pad); t.setAttribute("y", Hp - y1 + fs + pad * 0.6);
      t.setAttribute("font-size", fs); t.setAttribute("font-family", "ui-monospace,monospace");
      t.setAttribute("font-weight", "700"); t.setAttribute("fill", "#04121f");
      t.style.pointerEvents = "none"; t.textContent = label; svg.appendChild(t);
    }
  };
  if (state.boxMode === "chunk") {
    (state.doc.chunks || []).forEach(c => {
      const u = chunkUnion(c, state.page); if (!u) return;
      const snip = (c.text || "").replace(/\n/g, " ").slice(0, 200);
      put(u, ctColor(c.metadata?.content_type || "section"), { chunk: c.chunk_id, text: snip, label: c.section_number || "" });
    });
  } else {
    regions.forEach(r => put(r.bbox, colorFor(r.type), { chunk: r.chunk, text: r.text }));
  }
}

const hud = $("hud");
function showHud(ev, r) {
  const [x0, y0, x1, y1] = r.bbox;
  hud.innerHTML = `<span class="k">${r.type}</span> · p${r.page}  <span class="k">bbox</span> [${x0}, ${y0}, ${x1}, ${y1}]` +
    `<span class="tx">${esc((r.text || "").slice(0, 200))}</span>`;
  hud.classList.add("show");
}
const hideHud = () => hud.classList.remove("show");
window.addEventListener("resize", () => { if (state.doc) drawPage(); });
