"use strict";
// Storage page: keep the hidden id list in sync with the checkboxes for "Delete selected".
(() => {
  const ids = document.getElementById("ids");
  if (!ids) return;
  const sync = () => { ids.value = [...document.querySelectorAll(".pickdel:checked")].map(x => x.value).join(","); };
  document.addEventListener("change", e => { if (e.target.classList.contains("pickdel")) sync(); });
  sync();
})();
