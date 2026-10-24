const $ = s => document.querySelector(s);
const state = { doc: null, file: null, keep: new Set(), hl: new Set(), plan: null };
const MARKER = `<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9.5 2.5l4 4-6 6H4.5v-3z"/><path d="M2 14.5h12"/></svg>`;
const fmt = n => n.toLocaleString("en-MY", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- upload ----------
const drop = $("#drop");
$("#pickBtn").onclick = () => $("#file").click();
$("#file").onchange = e => e.target.files[0] && load(e.target.files[0]);
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => {
  e.preventDefault(); drop.classList.remove("over");
  const f = e.dataTransfer.files[0];
  if (f) load(f);
};
$("#pwBtn").onclick = () => state.file && load(state.file, $("#pw").value);
$("#pw").onkeydown = e => { if (e.key === "Enter") $("#pwBtn").click(); };
$("#newBtn").onclick = () => {
  if (state.doc) fetch("/api/close", { method: "POST", body: JSON.stringify({ id: state.doc.id }) });
  location.reload();
};

async function load(file, password = "") {
  state.file = file;
  $("#err").textContent = "Reading...";
  const res = await fetch("/api/open", {
    method: "POST",
    headers: { "X-Password": encodeURIComponent(password) },
    body: file,
  });
  const data = await res.json();
  if (res.status === 401) {
    $("#pwRow").classList.add("show");
    $("#err").textContent = data.error;
    $("#pw").focus();
    return;
  }
  if (!res.ok) { $("#err").textContent = data.error || "Could not open the file."; return; }
  state.doc = data;
  state.keep = new Set();
  state.hl = new Set();
  start();
}

// ---------- workspace ----------
function start() {
  const d = state.doc;
  drop.style.display = "none";
  $("#work").classList.add("show");
  $("#newBtn").style.display = "";
  const cardName = c => `${c.name.replace(/^MAYBANK\s*/, "")} ..${c.last4}`;
  $("#meta").textContent = `${state.file.name}  |  Statement ${toDmy(d.statement_date)}  |  ${d.transactions.length} transactions`;
  $("#card").innerHTML = `<option value="all">All cards</option>` +
    d.cards.map(c => `<option value="${c.id}">${esc(cardName(c))}</option>`).join("");
  resetFilters();
  // Parser cross-checks (row totals vs the statement, unrecognised dated lines).
  $("#notice").classList.toggle("show", d.warnings.length > 0);
  $("#notice").innerHTML = d.warnings.length
    ? `<b>Check the preview before sending this file.</b><ul>${d.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`
    : "";
  const n = d.pages.length;
  for (const id of ["#pgFrom", "#pgTo"]) $(id).max = n;
  $("#pgFrom").value = 1; $("#pgTo").value = n;

  $("#pages").innerHTML = d.pages.map((p, i) => `
    <div class="page" id="pg${i}" style="aspect-ratio:${p.w}/${p.h}">
      <span class="no">Page ${i + 1}</span>
      <img loading="lazy" src="/api/page?id=${d.id}&n=${i}" alt="Page ${i + 1}">
      <div class="layer"></div>
      <div class="drop-note"></div>
    </div>`).join("");
  renderList();
  refreshPlan();
}

function dateBounds() {
  const ds = state.doc.transactions.flatMap(t => [t.txn_date, t.post_date]).filter(Boolean).sort();
  return [ds[0], ds[ds.length - 1]];
}

// Dates are typed and shown as dd/mm/yyyy; filtering compares ISO yyyy-mm-dd strings.
const toDmy = iso => iso ? iso.split("-").reverse().join("/") : "";
function toIso(dmy) {
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(dmy.trim());
  if (!m) return null;
  const [d, mo, y] = [+m[1], +m[2], +m[3]];
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) return null;
  return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
// "" = no bound, null = invalid (input is flagged and the bound ignored).
function readDate(id) {
  const el = $(id), raw = el.value.trim();
  const iso = raw ? toIso(raw) : "";
  el.classList.toggle("bad", iso === null);
  return iso || "";
}

for (const id of ["from", "to"]) {
  const input = $("#" + id), native = input.parentElement.querySelector(".native");
  input.addEventListener("input", e => {
    // Auto-insert slashes while typing digits; leave deletions alone.
    if (e.inputType && e.inputType.startsWith("delete")) return renderList();
    const digits = input.value.replace(/\D/g, "").slice(0, 8);
    let out = digits.slice(0, 2);
    if (digits.length > 2) out += "/" + digits.slice(2, 4);
    if (digits.length > 4) out += "/" + digits.slice(4);
    if (digits.length === 2 || digits.length === 4) out += "/";
    input.value = out;
    renderList();
  });
  input.parentElement.querySelector(".cal").onclick = () => {
    native.value = toIso(input.value) || "";
    try { native.showPicker(); } catch { native.click(); }
  };
  native.onchange = () => { input.value = toDmy(native.value); renderList(); };
}

function resetFilters() {
  const [lo, hi] = dateBounds();
  document.querySelectorAll(".dt .native").forEach(n => { n.min = lo; n.max = hi; });
  $("#from").value = toDmy(lo); $("#to").value = toDmy(hi);
  $("#from").classList.remove("bad"); $("#to").classList.remove("bad");
  $("#q").value = ""; $("#card").value = "all"; $("#kind").value = "all";
}

function visible() {
  const from = readDate("#from"), to = readDate("#to"), basis = $("#basis").value;
  const q = $("#q").value.trim().toLowerCase();
  const card = $("#card").value, kind = $("#kind").value;
  return state.doc.transactions.filter(t => {
    const d = t[basis];
    if (from && d < from) return false;
    if (to && d > to) return false;
    if (card !== "all" && String(t.card) !== card) return false;
    if (kind === "debit" && (t.credit || t.amount == null)) return false;
    if (kind === "credit" && !t.credit) return false;
    if (q) {
      const hay = `${t.desc} ${t.location} ${t.extra} ${t.amount ?? ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderList() {
  const d = state.doc;
  const vis = visible();
  let lastCard = null, html = "";
  for (const t of vis) {
    if (t.card !== lastCard && d.cards.length > 1) {
      const c = d.cards[t.card];
      html += `<tr class="section"><td colspan="6">${esc(c ? `${c.name} ..${c.last4}` : "Other")}</td></tr>`;
      lastCard = t.card;
    }
    const kept = state.keep.has(t.id), marked = state.hl.has(t.id);
    const amt = t.amount == null ? "" : fmt(t.amount) + (t.credit ? " CR" : "");
    html += `<tr data-id="${t.id}" class="${kept ? "kept" : ""} ${marked ? "mk" : ""}">
      <td><input type="checkbox" ${kept ? "checked" : ""} tabindex="-1"></td>
      <td class="mkc"><button class="mk" title="Highlight this row in the PDF" tabindex="-1">${MARKER}</button></td>
      <td class="d">${t.txn_raw}</td>
      <td class="d">${t.post_raw}</td>
      <td>${esc(t.desc)} <span class="loc">${esc(t.location)}</span>${t.extra ? `<div class="loc">${esc(t.extra)}</div>` : ""}</td>
      <td class="amt ${t.credit ? "cr" : ""}">${amt}</td>
    </tr>`;
  }
  $("#rows").innerHTML = html || `<tr><td colspan="6" style="color:var(--muted);padding:16px">No transactions match these filters.</td></tr>`;
  const keptVisible = vis.filter(t => state.keep.has(t.id)).length;
  $("#shown").textContent = `${vis.length} shown, ${keptVisible} kept`;
  renderTotals();
}

function renderTotals() {
  let debit = 0, credit = 0;
  for (const t of state.doc.transactions) {
    if (!state.keep.has(t.id) || t.amount == null) continue;
    if (t.credit) credit += t.amount; else debit += t.amount;
  }
  $("#kCount").textContent = state.keep.size;
  $("#kDebit").textContent = fmt(debit);
  $("#kCredit").textContent = fmt(credit);
  $("#kNet").textContent = fmt(debit - credit);
  let hSum = 0;
  for (const t of state.doc.transactions) {
    if (state.hl.has(t.id) && t.amount != null) hSum += t.credit ? -t.amount : t.amount;
  }
  $("#hCount").textContent = state.hl.size;
  $("#hSum").textContent = fmt(hSum);
}

function toggle(id, on) {
  const want = on ?? !state.keep.has(id);
  if (want) state.keep.add(id);
  else { state.keep.delete(id); state.hl.delete(id); }
}

// Highlighting a row always keeps it; un-highlighting leaves it kept.
function toggleHl(id, on) {
  const want = on ?? !state.hl.has(id);
  if (want) { state.hl.add(id); state.keep.add(id); } else state.hl.delete(id);
}

$("#rows").onclick = e => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  if (e.target.closest("button.mk")) toggleHl(tr.dataset.id); else toggle(tr.dataset.id);
  renderList();
  refreshPlan();
  focusPage(tr.dataset.id);
};
$("#rows").onmouseover = e => {
  const tr = e.target.closest("tr[data-id]");
  highlight(tr ? tr.dataset.id : null);
};
$("#rows").onmouseleave = () => highlight(null);

for (const id of ["#basis", "#card", "#kind"]) $(id).onchange = renderList;
$("#q").oninput = renderList;
$("#resetF").onclick = () => { resetFilters(); renderList(); };
$("#tickAll").onclick = () => { visible().forEach(t => toggle(t.id, true)); renderList(); refreshPlan(); };
$("#untickAll").onclick = () => { visible().forEach(t => toggle(t.id, false)); renderList(); refreshPlan(); };
$("#hlAll").onclick = () => { visible().forEach(t => toggleHl(t.id, true)); renderList(); refreshPlan(); };
$("#clearHl").onclick = () => { state.hl.clear(); renderList(); refreshPlan(); };
document.querySelectorAll("[data-opt]").forEach(el => el.onchange = refreshPlan);
for (const id of ["#pgFrom", "#pgTo"]) $(id).oninput = refreshPlan;
$("#pgAll").onclick = () => { $("#pgFrom").value = 1; $("#pgTo").value = state.doc.pages.length; refreshPlan(); };

function options() {
  const o = {};
  document.querySelectorAll("[data-opt]").forEach(el => o[el.dataset.opt] = el.type === "checkbox" ? el.checked : el.value);
  return o;
}

// Highlight colour: one CSS variable drives the list, preview and legend; remembered per browser.
function setHlColor(hex, save = true) {
  $("#hlColor").value = hex;
  document.documentElement.style.setProperty("--hl", hex);
  document.querySelectorAll(".swatch").forEach(b => b.classList.toggle("on", b.dataset.color === hex));
  if (save) localStorage.setItem("hlColor", hex);
}
document.querySelectorAll(".swatch").forEach(b => b.onclick = () => { setHlColor(b.dataset.color); if (state.doc) refreshPlan(); });
$("#hlColor").oninput = () => setHlColor($("#hlColor").value);
setHlColor(localStorage.getItem("hlColor") || "#ffe859", false);

const payload = () => JSON.stringify({
  id: state.doc.id, keep: [...state.keep], highlight: [...state.hl], options: options(),
});

// ---------- preview ----------
let planTimer = null, planSeq = 0;
function refreshPlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(async () => {
    const seq = ++planSeq;
    const res = await fetch("/api/plan", {
      method: "POST",
      body: payload(),
    });
    if (seq !== planSeq) return;
    const plan = await res.json();
    const rep = $("#report");
    if (!res.ok) {
      // Keep the last good preview; just block export until the input is fixed.
      rep.className = "bad"; rep.textContent = plan.error || "Could not build the preview.";
      $("#dl").disabled = true;
      return;
    }
    $("#dl").disabled = false;
    state.plan = plan;
    drawPlan();
  }, 80);
  $("#report").textContent = ""; $("#report").className = "";
}

function pct(rect, p) {
  const [x0, y0, x1, y1] = rect;
  return `left:${x0 / p.w * 100}%;top:${y0 / p.h * 100}%;width:${(x1 - x0) / p.w * 100}%;height:${(y1 - y0) / p.h * 100}%`;
}

function drawPlan() {
  const d = state.doc, plan = state.plan;
  if (!plan || plan.error) return;
  const layers = d.pages.map(() => []);
  for (const h of plan.highlights) {
    layers[h.page].push(`<div class="box y" style="${pct(h.rect, d.pages[h.page])}"></div>`);
  }
  for (const b of plan.boxes) {
    layers[b.page].push(`<div class="box r" style="${pct(b.rect, d.pages[b.page])}"></div>`);
  }
  // Transparent full-row hit areas on top so every row stays clickable.
  for (const t of d.transactions) {
    const kept = state.keep.has(t.id);
    layers[t.page].push(`<div class="box hit ${kept ? "k" : ""}" data-id="${t.id}" title="${esc(t.desc)}" style="${pct(t.rect, d.pages[t.page])}"></div>`);
  }
  d.pages.forEach((p, i) => {
    const el = $("#pg" + i);
    el.querySelector(".layer").innerHTML = layers[i].join("");
    el.classList.toggle("dropped", plan.dropped.includes(i));
    el.querySelector(".drop-note").innerHTML = "<span>" +
      (plan.out_of_range.includes(i) ? "Outside export page range" : "This page will be dropped (nothing kept)") + "</span>";
  });
}

$("#pages").onclick = e => {
  const box = e.target.closest(".box.hit");
  if (!box) return;
  if (e.shiftKey) toggleHl(box.dataset.id); else toggle(box.dataset.id);
  renderList();
  refreshPlan();
};
$("#pages").onmouseover = e => {
  const box = e.target.closest(".box.hit");
  document.querySelectorAll("#rows tr.hl").forEach(r => r.classList.remove("hl"));
  if (box) $(`#rows tr[data-id="${box.dataset.id}"]`)?.classList.add("hl");
};

function highlight(id) {
  document.querySelectorAll(".box.hl").forEach(b => b.classList.remove("hl"));
  if (id) document.querySelector(`.box.hit[data-id="${id}"]`)?.classList.add("hl");
}

function focusPage(id) {
  const box = document.querySelector(`.box.hit[data-id="${id}"]`);
  const pane = $("#preview");
  if (!box) return;
  const r = box.getBoundingClientRect(), pr = pane.getBoundingClientRect();
  if (r.top < pr.top || r.bottom > pr.bottom) box.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ---------- export ----------
$("#dl").onclick = async () => {
  const btn = $("#dl"), rep = $("#report");
  btn.disabled = true; rep.className = ""; rep.textContent = "Redacting and checking...";
  try {
    const res = await fetch("/api/redact", {
      method: "POST",
      body: payload(),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    const report = JSON.parse(atob(res.headers.get("X-Redaction-Report") || "e30="));
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = state.file.name.replace(/\.pdf$/i, "") + "_redacted.pdf";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    const summary = `Done. ${report.kept} kept (${report.highlighted} highlighted), ${report.redacted_rows} rows redacted, ${report.pages_out} page(s). ` +
      (report.pages_dropped.length ? `Dropped pages: ${report.pages_dropped.join(", ")}. ` : "");
    if (report.warnings.length) {
      rep.className = "warn";
      rep.textContent = summary + report.warnings.join(" ");
    } else {
      rep.className = "ok";
      rep.textContent = summary + "Checked: no text left under the boxes, and everything outside them is intact.";
    }
  } catch (e) {
    rep.className = "bad";
    rep.textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
};
