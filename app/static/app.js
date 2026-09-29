"use strict";

const state = {
  sid: null,
  files: [],
  selected: null,
  processed: false,
};

const $ = (sel) => document.querySelector(sel);
const el = {
  drop: $("#drop"),
  files: $("#files"),
  fileCount: $("#fileCount"),
  inputFiles: $("#inputFiles"),
  inputFolder: $("#inputFolder"),
  previewEmpty: $("#previewEmpty"),
  previewBody: $("#previewBody"),
  previewMeta: $("#previewMeta"),
  imgBefore: $("#imgBefore"),
  imgAfter: $("#imgAfter"),
  results: $("#results"),
  noResults: $("#noResults"),
  tiles: $("#tiles"),
  resBody: $("#resBody"),
  btnDownload: $("#btnDownload"),
  btnReport: $("#btnReport"),
  toast: $("#toast"),
};

function toast(msg, isErr = false) {
  el.toast.textContent = msg;
  el.toast.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.toast.className = "toast"), 3200);
}
function kb(n) { return n ? (n / 1024).toFixed(0) + " КБ" : "—"; }
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = "Ошибка запроса";
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r;
}

function getSettings() {
  return {
    fill_method: $("#fill").value,
    blur_radius: +$("#blur").value,
    solid_color: $("#solid").value,
    gradient_from: $("#grad1").value,
    gradient_to: $("#grad2").value,
    reject_threshold: parseFloat($("#threshold").value) || 2.0,
    out_format: document.querySelector("#fmtSeg .on").dataset.fmt,
    quality: +$("#quality").value,
    keep_metadata: $("#keepMeta").checked,
  };
}

function syncFillFields() {
  const m = $("#fill").value;
  $("#wrapBlur").classList.toggle("hidden", m !== "blur");
  $("#wrapSolid").classList.toggle("hidden", m !== "solid");
  $("#wrapGrad1").classList.toggle("hidden", m !== "gradient");
  $("#wrapGrad2").classList.toggle("hidden", m !== "gradient");
}

function renderFiles() {
  el.fileCount.textContent = state.files.length;
  const threshold = parseFloat($("#threshold").value) || 2.0;
  el.files.innerHTML = "";
  state.files.forEach((f) => {
    const rejected = f.fit_scale >= threshold;
    const div = document.createElement("div");
    div.className = "file" + (state.selected === f.id ? " sel" : "");
    div.tabIndex = 0;
    div.setAttribute("role", "button");
    div.setAttribute("aria-label", `${f.name}, ${f.w} на ${f.h}`);
    div.innerHTML = `
      <span class="file-tag ${rejected ? "rej" : "ok"}">${rejected ? "отбрак." : "ок"}</span>
      <img class="thumb" loading="lazy" alt="" src="/api/thumb?sid=${state.sid}&id=${f.id}">
      <div class="file-meta">
        <div class="file-name" title="${f.name}">${f.name}</div>
        <div class="file-dims">${f.w}×${f.h} / ×${f.fit_scale.toFixed(2)}</div>
      </div>`;
    const sel = () => selectFile(f.id);
    div.addEventListener("click", sel);
    div.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sel(); } });
    el.files.appendChild(div);
  });
}

function selectFile(id) {
  state.selected = id;
  renderFiles();
  updatePreview();
}

const updatePreview = debounce(async () => {
  if (!state.selected) return;
  try {
    const r = await api("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: state.sid, id: state.selected, settings: getSettings() }),
    });
    const d = await r.json();
    el.previewEmpty.classList.add("hidden");
    el.previewBody.classList.remove("hidden");
    el.imgBefore.src = d.before;
    if (d.rejected) {
      el.imgAfter.closest(".frame").innerHTML =
        `<div class="rejected-box"><b>Отбраковано</b><span>${d.reason}</span></div>`;
      el.previewMeta.innerHTML = `<span class="flag rej">отбраковка / ×${d.scale.toFixed(2)}</span>`;
    } else {
      const frame = el.imgAfter.closest(".frame");
      if (!frame.querySelector("#imgAfter")) {
        frame.innerHTML = `<img id="imgAfter" alt="Результат">`;
        el.imgAfter = $("#imgAfter");
      }
      el.imgAfter.src = d.after;
      el.previewMeta.innerHTML =
        `<span class="flag ok">×${d.scale.toFixed(2)} / вписано ${d.placed.w}×${d.placed.h}</span>`;
    }
  } catch (e) { toast(e.message, true); }
}, 250);

async function uploadFiles(fileList) {
  const fd = new FormData();
  if (state.sid) fd.append("sid", state.sid);
  let count = 0;
  for (const f of fileList) {
    if (f.type.startsWith("image/") || /\.(jpe?g|png|webp|bmp|gif|tiff?)$/i.test(f.name)) {
      fd.append("files", f); count++;
    }
  }
  if (!count) { toast("Не найдено изображений в выбранном", true); return; }
  toast(`Загрузка ${count} файл(ов)…`);
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    const d = await r.json();
    state.sid = d.sid; state.files = d.files;
    renderFiles();
    if (!state.selected && state.files.length) selectFile(state.files[0].id);
    toast(`Загружено: ${d.added.length}. Всего: ${state.files.length}.`);
  } catch (e) { toast(e.message, true); }
}

async function loadSamples() {
  const fd = new FormData();
  if (state.sid) fd.append("sid", state.sid);
  toast("Загрузка тестового датасета…");
  try {
    const r = await api("/api/samples", { method: "POST", body: fd });
    const d = await r.json();
    state.sid = d.sid; state.files = d.files;
    renderFiles();
    if (state.files.length) selectFile(state.files[0].id);
    toast(`Датасет загружен: ${d.added.length} изображений`);
  } catch (e) { toast(e.message, true); }
}

async function resetAll() {
  if (state.sid) {
    const fd = new FormData(); fd.append("sid", state.sid);
    try { await api("/api/reset", { method: "POST", body: fd }); } catch (e) {}
  }
  state.sid = null; state.files = []; state.selected = null; state.processed = false;
  renderFiles();
  el.previewBody.classList.add("hidden");
  el.previewEmpty.classList.remove("hidden");
  el.previewMeta.innerHTML = "";
  el.results.classList.add("hidden");
  el.noResults.classList.remove("hidden");
  el.btnDownload.classList.add("hidden");
  el.btnReport.classList.add("hidden");
  toast("Очищено.");
}

async function processAll() {
  if (!state.files.length) { toast("Сначала загрузите изображения", true); return; }
  const btn = $("#btnProcess");
  btn.disabled = true;
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span> Обработка…';
  try {
    const r = await api("/api/process", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: state.sid, settings: getSettings() }),
    });
    const d = await r.json();
    renderResults(d);
    state.processed = true;
    el.btnDownload.classList.remove("hidden");
    el.btnReport.classList.remove("hidden");
    toast(`Готово: ${d.summary.converted} преобразовано, ${d.summary.rejected} отбраковано`);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.innerHTML = old; }
}

function renderResults(d) {
  const s = d.summary;
  el.results.classList.remove("hidden");
  el.noResults.classList.add("hidden");
  el.tiles.innerHTML = `
    <div class="stat"><b>${s.total}</b><span>всего файлов</span></div>
    <div class="stat"><b style="color:var(--olive)">${s.converted}</b><span>преобразовано</span></div>
    <div class="stat"><b style="color:var(--rust)">${s.rejected}</b><span>отбраковано</span></div>
    <div class="stat"><b>${s.errors}</b><span>ошибок</span></div>
    <div class="stat accent"><b>${s.saved_percent_total}%</b><span>экономия размера</span></div>`;
  el.resBody.innerHTML = d.files.map((r) => {
    const st = { converted: "Преобразован", rejected: "Отбракован", error: "Ошибка" }[r.status] || r.status;
    return `<tr>
      <td>${r.name}</td>
      <td><span class="st ${r.status}">${st}</span></td>
      <td class="num">${r.src_w ? r.src_w + "×" + r.src_h : "—"}</td>
      <td class="num">${r.scale ? "×" + r.scale.toFixed(2) : "—"}</td>
      <td>${r.out_name || "—"}</td>
      <td class="num">${kb(r.src_bytes)}</td>
      <td class="num">${kb(r.out_bytes)}</td>
      <td class="num">${r.status === "converted" ? r.saved_percent.toFixed(1) + "%" : "—"}</td>
      <td class="muted" style="white-space:normal;max-width:260px">${r.reason || ""}</td>
    </tr>`;
  }).join("");
}

function bind() {
  el.inputFiles.addEventListener("change", (e) => uploadFiles(e.target.files));
  el.inputFolder.addEventListener("change", (e) => uploadFiles(e.target.files));
  $("#btnFiles").addEventListener("click", () => el.inputFiles.click());
  $("#btnFolder").addEventListener("click", () => el.inputFolder.click());
  $("#btnSamples").addEventListener("click", loadSamples);
  $("#btnReset").addEventListener("click", resetAll);
  $("#btnProcess").addEventListener("click", processAll);

  el.btnDownload.addEventListener("click", () => {
    if (state.sid) window.location = `/api/download?sid=${state.sid}`;
  });
  el.btnReport.addEventListener("click", () => {
    if (state.sid) window.open(`/api/report?sid=${state.sid}`, "_blank");
  });

  ["dragenter", "dragover"].forEach((ev) =>
    el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.add("hover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.remove("hover"); }));
  el.drop.addEventListener("drop", (e) => {
    const items = e.dataTransfer.files; if (items && items.length) uploadFiles(items);
  });
  el.drop.addEventListener("click", () => el.inputFiles.click());
  el.drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.inputFiles.click(); } });

  $("#fill").addEventListener("change", () => { syncFillFields(); updatePreview(); });
  ["solid", "grad1", "grad2", "blur", "quality"].forEach((id) =>
    $("#" + id).addEventListener("input", updatePreview));
  $("#blur").addEventListener("input", () => ($("#blurOut").textContent = $("#blur").value));
  $("#quality").addEventListener("input", () => ($("#qualityOut").textContent = $("#quality").value));
  $("#keepMeta").addEventListener("change", updatePreview);
  $("#threshold").addEventListener("input", () => { renderFiles(); updatePreview(); });

  document.querySelectorAll("#fmtSeg button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#fmtSeg button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on"); updatePreview();
    }));

  syncFillFields();
}

bind();
