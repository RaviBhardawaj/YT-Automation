"use strict";
// Create / Upload & edit page: streamed uploads with progress and retry, then one /api/create call.
(() => {
  const root = document.getElementById("creator");
  if (!root) return;
  const drop = document.getElementById("drop");
  const input = document.getElementById("files");
  const list = document.getElementById("queue");
  const uploaded = new Map();          // row element -> media id
  let busy = 0;

  function human(n) { return n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(1) + " MB"; }

  function upload(file, row, retried = false) {
    const bar = row.querySelector(".bar > i");
    const status = row.querySelector(".st");
    const btn = row.querySelector("button");
    btn.hidden = true;
    busy++;
    status.textContent = "uploading…";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/media/upload");
    xhr.setRequestHeader("X-CSRF-Token", CSRF);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    const ch = document.getElementById("channel").value;
    if (ch && ch !== "none") xhr.setRequestHeader("X-Channel", ch);
    xhr.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = (e.loaded / e.total * 100).toFixed(0) + "%"; };
    xhr.onload = async () => {
      busy--;
      if (xhr.status === 403 && !retried && xhr.responseText.includes("CSRF")) {   // stale token: renew once
        try { await refreshCsrf(); return upload(file, row, true); } catch { /* fall through to the error */ }
      }
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch { j = { ok: false, error: xhr.responseText.slice(0, 200) }; }
      if (xhr.status === 200 && j.ok) {
        uploaded.set(row, j.media.id);
        bar.style.width = "100%";
        const d = j.media.duration ? ` · ${Math.floor(j.media.duration / 60)}:${String(Math.round(j.media.duration % 60)).padStart(2, "0")}` : "";
        const res = j.media.width ? ` · ${j.media.width}×${j.media.height}` : "";
        status.textContent = (j.duplicate ? "already in your library" : "uploaded") + d + res;
      } else {
        status.textContent = "failed: " + (j.error || "HTTP " + xhr.status);
        row.querySelector(".bar").classList.add("hot");
        btn.hidden = false;
      }
    };
    xhr.onerror = () => { busy--; status.textContent = "connection lost"; btn.hidden = false; };
    xhr.send(file);
  }

  function add(files) {
    for (const file of files) {
      const row = document.createElement("div");
      row.className = "filerow";
      row.innerHTML = `<div><b>${escapeHtml(file.name)}</b> <span class="muted">${human(file.size)}</span> · <span class="st"></span>
        <div class="bar"><i style="width:0%"></i></div></div><button class="sm" type="button" hidden>Retry</button>`;
      row.querySelector("button").addEventListener("click", () => { row.querySelector(".bar").classList.remove("hot"); upload(file, row); });
      list.appendChild(row);
      upload(file, row);
    }
  }

  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { add(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => add(e.dataTransfer.files));
  document.querySelectorAll("[data-example]").forEach(b => b.addEventListener("click", () => {
    document.getElementById("req").value = b.dataset.example;
  }));

  document.getElementById("go").addEventListener("click", async e => {
    const state = document.getElementById("gostate");
    if (busy) { state.textContent = "Wait for the uploads to finish."; return; }
    const chSel = document.getElementById("channel");
    if (!chSel.value) { toast("Choose which channel this video is for (or “No channel”)", "err"); chSel.focus(); return; }
    const ids = [...uploaded.values(), ...[...document.querySelectorAll(".prev:checked")].map(x => +x.value)];
    if (root.dataset.mode === "edit" && !ids.length) { toast("Add at least one file to edit", "err"); return; }
    const ai = document.getElementById("aivideo");
    const wantAi = !!(ai && ai.checked);
    if (wantAi) {
      try {
        const est = await api("/api/videogen/estimate", null, "GET");
        if (!confirm(`AI video clips will cost about $${est.usd.toFixed(2)} (${est.basis}).\n` +
                     `Used this month: $${est.spent_this_month.toFixed(2)} of $${est.cap.toFixed(2)}.\n\nStart the job?`)) return;
      } catch (err) { toast(err.message, "err"); return; }
    }
    e.target.disabled = true;
    state.textContent = "Starting…";
    try {
      const j = await api("/api/create", {
        request: document.getElementById("req").value, media_ids: ids,
        channel_id: chSel.value === "none" ? null : chSel.value, no_upload: chSel.value === "none", ai_video: wantAi,
        format: document.getElementById("format").value || null,
        voice_id: (document.getElementById("voice") || {}).value || null,
      });
      toast(j.detail || "Started");
      location.href = j.next;
    } catch (err) { toast(err.message, "err"); state.textContent = ""; }
    finally { e.target.disabled = false; }
  });
})();
