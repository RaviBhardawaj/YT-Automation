"use strict";
let CSRF = document.querySelector('meta[name="csrf"]').content;

async function refreshCsrf() {
  const r = await fetch("/api/csrf", { credentials: "same-origin" });
  const j = await r.json();
  CSRF = j.token;
  document.querySelector('meta[name="csrf"]').content = CSRF;
}

async function api(path, data, method = "POST", retried = false) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
    body: method === "GET" ? undefined : JSON.stringify(data || {}),
  });
  if (r.status === 403 && !retried && method !== "GET") {
    const peek = await r.clone().text();
    if (peek.includes("CSRF")) {                  // token went stale (e.g. page open across a restart): renew once
      await refreshCsrf();
      return api(path, data, method, true);
    }
  }
  let j;
  const text = await r.text();
  try { j = JSON.parse(text); } catch { j = { ok: r.ok, error: text.slice(0, 300) }; }
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "err" ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 2800);
}

function setPath(obj, name, value) {
  const parts = name.split("::");
  let cur = obj;
  parts.slice(0, -1).forEach(p => { cur[p] = cur[p] || {}; cur = cur[p]; });
  cur[parts[parts.length - 1]] = value;
}

function collect(root) {
  const out = {};
  root.querySelectorAll("[name]").forEach(el => {
    if (el.disabled) return;
    let v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "radio") { if (!el.checked) return; v = el.value; }
    else v = el.value;
    const t = el.dataset.type;
    if (t === "number") v = v === "" ? null : parseFloat(v);
    if (t === "list") v = String(v).split(",").map(s => s.trim()).filter(Boolean);
    if (t === "json") { try { v = JSON.parse(v); } catch { throw new Error("Invalid JSON in " + el.name); } }
    setPath(out, el.name, v);
  });
  return out;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// [data-post] buttons -------------------------------------------------------
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-post]");
  if (!b) return;
  e.preventDefault();
  if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
  let payload = {};
  try {
    payload = b.dataset.body ? JSON.parse(b.dataset.body) : {};
    if (b.dataset.collect) Object.assign(payload, collect(document.querySelector(b.dataset.collect)));
  } catch (err) { toast(err.message, "err"); return; }
  b.disabled = true;
  try {
    const j = await api(b.dataset.post, payload);
    if (j.url && b.dataset.navigate !== undefined) { location.href = j.url; return; }
    toast(b.dataset.done || j.detail || "Done");
    if (j.show) window.prompt("Copy this now and give it to the user — it won't be shown again:", j.show);
    if (j.next) location.href = j.next;
    else if (b.dataset.reload !== "no") setTimeout(() => location.reload(), 700);
  } catch (err) { toast(err.message, "err"); }
  finally { b.disabled = false; }
});

// [data-form] forms ---------------------------------------------------------
document.addEventListener("submit", async e => {
  const f = e.target.closest("form[data-form]");
  if (!f) return;
  e.preventDefault();
  const btn = f.querySelector("button[type=submit]");
  if (btn) btn.disabled = true;
  try {
    const j = await api(f.dataset.form, collect(f));
    toast(f.dataset.done || j.detail || "Saved");
    if (j.next) setTimeout(() => { location.href = j.next; }, 500);
    else if (f.dataset.reload !== "no") setTimeout(() => location.reload(), 600);
  } catch (err) { toast(err.message, "err"); }
  finally { if (btn) btn.disabled = false; }
});

// secrets -------------------------------------------------------------------
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-save-secret]");
  if (!b) return;
  const name = b.dataset.saveSecret;
  const input = document.querySelector(`input[data-secret="${name}"]`);
  const clear = b.dataset.clear !== undefined;
  if (clear && !confirm("Remove this key from the Keychain?")) return;
  try {
    await api(`/api/secrets/${name}`, { value: clear ? "" : input.value });
    toast(clear ? "Removed from Keychain" : "Saved to macOS Keychain");
    setTimeout(() => location.reload(), 600);
  } catch (err) { toast(err.message, "err"); }
});

// model lists ---------------------------------------------------------------
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-load-models]");
  if (!b) return;
  b.disabled = true;
  try {
    const j = await api(`/api/providers/${b.dataset.loadModels}/models`, null, "GET");
    const dl = document.getElementById(b.dataset.target);
    dl.innerHTML = j.models.map(m => `<option value="${escapeHtml(m)}"></option>`).join("");
    toast(`${j.models.length} models available — click the model field to choose`);
  } catch (err) { toast(err.message, "err"); }
  finally { b.disabled = false; }
});

// file → base64 / text uploads -----------------------------------------------
document.addEventListener("change", async e => {
  const inp = e.target.closest("input[type=file][data-upload]");
  if (!inp || !inp.files.length) return;
  const file = inp.files[0];
  try {
    if (inp.dataset.upload === "yt-client") {
      const text = await file.text();
      await api("/api/youtube/client", { json: text });
      toast("OAuth client saved (secret stored in Keychain)");
    } else {
      const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
      await api(`/api/upload/${inp.dataset.upload}`, { name: file.name, data });
      toast("Uploaded");
    }
    setTimeout(() => location.reload(), 700);
  } catch (err) { toast(err.message, "err"); }
});

// market-data import (file + provenance fields) ---------------------------------
document.addEventListener("submit", async e => {
  const f = e.target.closest("form[data-import]");
  if (!f) return;
  e.preventDefault();
  const fileInput = f.querySelector("input[type=file]");
  if (!fileInput.files.length) return toast("Choose a CSV file", "err");
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const file = fileInput.files[0];
    const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
    const fields = {};
    f.querySelectorAll("[name]").forEach(el => { if (el.type !== "file") fields[el.name] = el.value; });
    const j = await api("/api/datasets/import", { ...fields, name: file.name, data });
    toast(j.detail || "Imported");
    setTimeout(() => location.href = j.next || location.href, 900);
  } catch (err) { toast(err.message, "err"); }
  finally { btn.disabled = false; }
});

// voice sample ----------------------------------------------------------------
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-sample]");
  if (!b) return;
  b.disabled = true;
  try {
    const [engine, voice_id] = b.dataset.sample.split("|");
    const j = await api("/api/voice/sample", { engine, voice_id });
    const audio = new Audio(j.url);
    audio.play();
  } catch (err) { toast(err.message, "err"); }
  finally { b.disabled = false; }
});

// live dashboard status -----------------------------------------------------
async function pollStatus() {
  const box = document.getElementById("live");
  if (!box) return;
  try {
    const s = await api("/api/status", null, "GET");
    const running = s.running.length
      ? s.running.map(t => `<div><span class="chip info">${escapeHtml(t.type)}</span> ${t.production_id ? `<a href="/productions/${t.production_id}">${escapeHtml(t.title || "#" + t.production_id)}</a> · ${escapeHtml(t.pstatus || "")}` : ""}</div>`).join("")
      : `<div class="muted">Idle — nothing running right now.</div>`;
    const ev = s.events.map(e => `<div class="lvl-${escapeHtml(e.level)}"><span class="muted">${escapeHtml(e.when)}</span> · ${escapeHtml(e.message)}</div>`).join("");
    box.innerHTML = `<h3>Running now</h3>${running}<div class="muted small" style="margin-top:6px">${s.pending} task(s) queued</div><h3>Recent activity</h3><div class="events">${ev}</div>`;
  } catch { /* server restarting */ }
}
if (document.getElementById("live")) { pollStatus(); setInterval(pollStatus, 4000); }

// production page: reload when status changes --------------------------------
const prodEl = document.querySelector("[data-watch-production]");
if (prodEl) {
  const id = prodEl.dataset.watchProduction;
  let last = prodEl.dataset.status;
  setInterval(async () => {
    try {
      const s = await api(`/api/productions/${id}/status`, null, "GET");
      if (s.status !== last || s.stage !== prodEl.dataset.stage) location.reload();
    } catch { }
  }, 4000);
}

// copy a field's text (Shorts captions, hashtags …) ----------------------------
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  const el = document.querySelector(b.dataset.copy);
  if (!el) return;
  const text = el.value !== undefined ? el.value : el.textContent;
  try { await navigator.clipboard.writeText(text); }
  catch { el.select && el.select(); document.execCommand("copy"); }
  toast("Copied");
});

// generic auto-refresh ----------------------------------------------------------
const auto = document.querySelector("[data-autorefresh]");
if (auto) setTimeout(() => location.reload(), parseInt(auto.dataset.autorefresh, 10) * 1000);

// Piper catalogue browser ---------------------------------------------------
const piperBtn = document.getElementById("piper-search");
if (piperBtn) piperBtn.addEventListener("click", async () => {
  const lang = document.getElementById("piper-lang").value.trim();
  const out = document.getElementById("piper-results");
  out.innerHTML = '<div class="muted">Loading catalogue…</div>';
  try {
    const j = await api(`/api/voice/piper?lang=${encodeURIComponent(lang)}`, null, "GET");
    out.innerHTML = `<table><tr><th>Voice</th><th>Language</th><th>Quality</th><th>Size</th><th></th></tr>${j.voices.map(v => `
      <tr><td class="mono">${escapeHtml(v.key)}</td><td>${escapeHtml(v.language)}</td><td>${escapeHtml(v.quality)}</td><td>${v.size_mb} MB</td>
      <td class="row"><button class="sm" data-card="${escapeHtml(v.key)}">Licence</button>
      <button class="sm" data-post="/api/voice/piper/${encodeURIComponent(v.key)}/download" data-done="Download queued — see Queue" data-reload="no">Download</button></td></tr>
      <tr><td colspan="5" id="card-${escapeHtml(v.key)}" class="small"></td></tr>`).join("")}</table>`;
  } catch (err) { out.innerHTML = ""; toast(err.message, "err"); }
});
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-card]");
  if (!b) return;
  const cell = document.getElementById("card-" + b.dataset.card);
  try {
    const j = await api(`/api/voice/piper/${encodeURIComponent(b.dataset.card)}/card`, null, "GET");
    const verdict = j.commercial === true ? '<span class="chip ok">commercial use OK</span>'
      : j.commercial === false ? '<span class="chip err">NON-COMMERCIAL training data</span>' : '<span class="chip warn">licence unclear — review</span>';
    cell.innerHTML = `${verdict} <span class="muted">Dataset licence: ${escapeHtml(j.license)}</span>`;
  } catch (err) { toast(err.message, "err"); }
});

// Ollama model check ----------------------------------------------------------
const olCheck = document.getElementById("ollama-check");
if (olCheck) olCheck.addEventListener("click", async () => {
  const model = document.getElementById("ollama-model").value.trim();
  const out = document.getElementById("ollama-result");
  try {
    const j = await api("/api/ollama/check", { model });
    const f = j.fit;
    out.innerHTML = `<div class="note ${f.ram_ok && f.disk_ok ? "ok" : "warn"}"><b>${escapeHtml(j.model)}</b>: download ${j.size_gb} GB.
      Needs ~${f.needed_ram_gb} GB RAM (you have ${f.ram_gb} GB) — ${f.ram_ok ? "fits" : "too large for comfortable use"}.
      Disk free ${f.disk_free_gb} GB — ${f.disk_ok ? "OK" : "not enough headroom"}.
      ${f.ram_ok && f.disk_ok ? `<div style="margin-top:8px"><button class="sm primary" data-post="/api/ollama/pull" data-body='${JSON.stringify({ model: j.model }).replace(/'/g, "&#39;")}' data-confirm="Download ${escapeHtml(j.model)} (${j.size_gb} GB) with Ollama?" data-done="Pull started — see Queue" data-reload="no">Pull ${escapeHtml(j.model)}</button></div>` : ""}</div>`;
  } catch (err) { out.innerHTML = ""; toast(err.message, "err"); }
});

// Interview wizard ------------------------------------------------------------
const wiz = document.getElementById("wizard");
if (wiz) {
  const Q = JSON.parse(document.getElementById("questions").textContent);
  const A = JSON.parse(document.getElementById("answers").textContent) || {};
  let i = Q.findIndex(q => !(q.key in A));
  if (i < 0) i = Q.length;

  function opts(q) {
    const cur = A[q.key];
    if (q.type === "choice") return q.options.map(([v, l]) =>
      `<label class="opt"><input type="radio" name="v" value="${escapeHtml(v)}" ${cur === v ? "checked" : ""}>${escapeHtml(l)}</label>`).join("");
    if (q.type === "multi") return q.options.map(([v, l]) =>
      `<label class="opt"><input type="checkbox" name="m" value="${escapeHtml(v)}" ${(cur || []).includes(v) ? "checked" : ""}>${escapeHtml(l)}</label>`).join("");
    if (q.type === "audience") return `
      <label class="f"><span>Describe them</span><input id="aud" value="${escapeHtml(A.audience || "")}" placeholder="e.g. Hindi-speaking students preparing for competitive exams"></label>
      <label class="f"><span>Age group</span><select id="age">${[["all", "General audience"], ["kids", "Children under 13 (videos marked Made for Kids)"], ["teens", "Teenagers"], ["adults", "Adults"]]
        .map(([v, l]) => `<option value="${v}" ${A.audience_age === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
    if (q.type === "cadence") {
      const f = A.formats || "shorts";
      const c = A.cadence || {};
      const row = (k, label, d) => `<div class="row" style="margin-bottom:10px"><b style="width:110px">${label}</b>
        <input type="number" min="0" max="20" id="c-${k}" value="${(c[k] || {}).count ?? d[0]}" style="width:90px">
        <select id="p-${k}" style="width:140px"><option value="day" ${((c[k] || {}).period || d[1]) === "day" ? "selected" : ""}>per day</option>
        <option value="week" ${((c[k] || {}).period || d[1]) === "week" ? "selected" : ""}>per week</option></select></div>`;
      return (f !== "long" ? row("shorts", "Shorts", [1, "day"]) : "") + (f !== "shorts" ? row("long", "Long-form", [1, "week"]) : "")
        + `<p class="muted small">Start small: every video still needs rendering time and, if you chose approval, your review.</p>`;
    }
    return "";
  }

  function followup(q) {
    if (!q.followup) return "";
    const f = q.followup;
    return `<div id="fu" style="display:none"><label class="f"><span>${escapeHtml(f.label)}</span>
      <input id="fu-v" value="${escapeHtml(A[f.key] || "")}" placeholder="${escapeHtml(f.placeholder || "")}"></label></div>`;
  }

  function render() {
    const prog = Q.map((_, k) => `<i class="${k < i ? "on" : ""}"></i>`).join("");
    if (i >= Q.length) {
      wiz.innerHTML = `<div class="progress">${prog}</div><div class="q">All set — here is your channel profile</div>
        <div class="kv">${Q.map(q => `<div>${escapeHtml(q.q.replace("?", ""))}</div><div>${escapeHtml(JSON.stringify(A[q.key]))}${q.followup && A[q.followup.key] ? " — " + escapeHtml(A[q.followup.key]) : ""}</div>`).join("")}</div>
        <div class="row" style="margin-top:18px"><button id="back">Back</button><button class="primary" id="finish">Save profile & continue</button></div>`;
      wiz.querySelector("#back").onclick = () => { i--; render(); };
      wiz.querySelector("#finish").onclick = async () => {
        try { const j = await api("/api/interview/complete", {}); location.href = j.next; } catch (err) { toast(err.message, "err"); }
      };
      return;
    }
    const q = Q[i];
    wiz.innerHTML = `<div class="progress">${prog}</div><div class="muted small">Question ${i + 1} of ${Q.length}</div>
      <div class="q">${escapeHtml(q.q)}</div>${opts(q)}${followup(q)}
      ${q.key === "publish_mode" ? '<div class="note warn small">Even with automatic publishing, YouTube keeps uploads from an unaudited Google Cloud project private. The app only publishes publicly after you confirm the audit passed in Settings.</div>' : ""}
      <div class="row" style="margin-top:16px">${i > 0 ? '<button id="back">Back</button>' : ""}<button class="primary" id="next">Next</button></div>`;
    const fu = wiz.querySelector("#fu");
    const syncFu = () => {
      if (!fu) return;
      const sel = [...wiz.querySelectorAll("input[name=v]:checked,input[name=m]:checked")].map(x => x.value);
      fu.style.display = sel.includes(q.followup.when) ? "block" : "none";
    };
    wiz.querySelectorAll("input[name=v],input[name=m]").forEach(x => x.addEventListener("change", syncFu));
    syncFu();
    if (wiz.querySelector("#back")) wiz.querySelector("#back").onclick = () => { i--; render(); };
    wiz.querySelector("#next").onclick = async () => {
      const out = {};
      if (q.type === "choice") {
        const v = wiz.querySelector("input[name=v]:checked");
        if (!v) return toast("Please choose an option", "err");
        out[q.key] = v.value;
      } else if (q.type === "multi") {
        const v = [...wiz.querySelectorAll("input[name=m]:checked")].map(x => x.value);
        if (!v.length) return toast("Pick at least one", "err");
        out[q.key] = v;
      } else if (q.type === "audience") {
        const v = wiz.querySelector("#aud").value.trim();
        if (!v) return toast("Describe your audience", "err");
        out.audience = v; out.audience_age = wiz.querySelector("#age").value;
      } else if (q.type === "cadence") {
        const c = {};
        ["shorts", "long"].forEach(k => { const n = wiz.querySelector("#c-" + k); if (n) c[k] = { count: parseInt(n.value || "0", 10), period: wiz.querySelector("#p-" + k).value }; });
        out.cadence = c;
      }
      if (q.followup && fu && fu.style.display !== "none") {
        const v = wiz.querySelector("#fu-v").value.trim();
        if (!v && !q.followup.optional) return toast("Please fill in the detail", "err");
        out[q.followup.key] = v;
      }
      try { const j = await api("/api/interview/answer", { answers: out }); Object.assign(A, j.answers); i++; render(); }
      catch (err) { toast(err.message, "err"); }
    };
  }
  render();
}

// GET filter forms submit as soon as a select changes -----------------------------
document.addEventListener("change", e => {
  const el = e.target.closest("[data-autosubmit]");
  if (el && el.form) el.form.submit();
});

// page tips: hidden once dismissed (per page, this browser) -------------------
document.querySelectorAll("[data-help]").forEach(box => {
  const key = "help-hidden:" + box.dataset.help;
  try { if (localStorage.getItem(key)) box.remove(); } catch { /* storage blocked: keep showing */ }
  box.querySelector(".x")?.addEventListener("click", () => {
    box.remove();
    try { localStorage.setItem(key, "1"); } catch { /* ignore */ }
  });
});

// phone layout: the sidebar collapses behind a Menu button ---------------------
document.querySelector(".navtoggle")?.addEventListener("click", e => {
  const side = e.currentTarget.closest(".side");
  const open = side.classList.toggle("open");
  e.currentTarget.setAttribute("aria-expanded", String(open));
  const label = e.currentTarget.querySelector("span");
  if (label) label.textContent = open ? "Close" : "Menu";
});
