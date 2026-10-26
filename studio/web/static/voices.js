// My voice page: record in the browser (or pick a file), then save it with consent.
(() => {
  const box = document.getElementById("newvoice");
  if (!box) return;
  let blob = null, fname = "recording.webm", rec = null, timer = null;
  const recBtn = document.getElementById("rec"), state = document.getElementById("recstate");
  const play = document.getElementById("playback"), file = document.getElementById("file");
  function show(b) { play.src = URL.createObjectURL(b); play.style.display = ""; }
  recBtn.addEventListener("click", async () => {
    if (rec && rec.state === "recording") { rec.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch (e) { toast("Microphone not available — allow it in the browser, or upload a file instead.", "err"); return; }
    const chunks = [];
    rec = new MediaRecorder(stream);
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = () => {
      clearInterval(timer); stream.getTracks().forEach(t => t.stop());
      blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      fname = (rec.mimeType || "").includes("mp4") ? "recording.m4a" : "recording.webm";
      recBtn.textContent = "🎙️ Record again"; state.textContent = "Recorded — listen below, then save.";
      file.value = ""; show(blob);
    };
    rec.start(); let s = 0;
    recBtn.textContent = "⏹ Stop"; state.textContent = "Recording… 0 s";
    timer = setInterval(() => { s++; state.textContent = `Recording… ${s} s` + (s < 10 ? " (keep going)" : ""); if (s >= 30) rec.stop(); }, 1000);
  });
  file.addEventListener("change", () => { if (file.files[0]) { blob = file.files[0]; fname = blob.name; show(blob); state.textContent = ""; } });
  document.getElementById("save").addEventListener("click", async e => {
    if (!blob) { toast("Record or choose a file first.", "err"); return; }
    if (blob.size > 15 * 1024 * 1024) { toast("That file is over 15 MB — 10 to 30 seconds is enough.", "err"); return; }
    const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(blob); });
    e.target.disabled = true;
    try {
      await api("/api/voices", { name: box.querySelector("[name=name]").value, language: box.querySelector("[name=language]").value,
                                 consent: document.getElementById("consent").checked, filename: fname, data });
      toast("Voice saved"); location.reload();
    } catch (err) { toast(err.message, "err"); }
    finally { e.target.disabled = false; }
  });
})();
