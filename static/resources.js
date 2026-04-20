// static/resources.js — StudyFlow Resources (clean UI)
// Works with your backend:
//   GET  /api/notes
//   POST /api/notes   { title, body, tag }
//   GET  /api/notes/<id>
//   DELETE /api/notes/<id>

(function(){
  const mount = document.getElementById("resourcesApp");
  const listMount = document.getElementById("savedNotes");

  if (!mount || !listMount) {
    console.error("resources.js: Missing #resourcesApp or #savedNotes in template.");
    return;
  }

  // ---------- Helpers ----------
  const esc = (s) => String(s ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");

  function fmtDate(iso){
    if (!iso) return "";
    // iso from now_iso() looks like 2026-02-19T18:22:11+00:00
    try {
      return iso.slice(0, 10);
    } catch {
      return "";
    }
  }

  async function fetchJson(url, opts){
    const r = await fetch(url, opts);
    const ct = (r.headers.get("content-type") || "").toLowerCase();

    if (ct.includes("application/json")) {
      const j = await r.json();
      return { r, j, text: null };
    }
    const text = await r.text();
    return { r, j: null, text };
  }

  function showInlineMsg(el, text, isError){
    el.textContent = text || "";
    el.style.color = isError ? "rgba(255,180,180,.95)" : "rgba(210,225,255,.75)";
  }

  // ---------- UI ----------
  mount.innerHTML = `
    <div id="noteMsg" class="muted" style="margin-bottom:10px;"></div>

    <div class="form-row">
      <input class="input" id="noteTitle" placeholder="Title" maxlength="120" />
      <button class="btn btn-primary" id="saveNoteBtn" type="button">Save</button>
    </div>

    <div style="margin-top:10px;">
      <textarea class="input" id="noteBody" placeholder="Write your note..." rows="8"
        style="resize:vertical; line-height:1.35; padding:14px; border-radius:16px;"></textarea>
    </div>

    <div style="margin-top:10px;" class="form-row">
      <input class="input" id="noteTag" placeholder="Tag (optional)" maxlength="30" />
      <button class="btn btn-ghost" id="clearBtn" type="button">Clear</button>
    </div>
  `;

  const noteMsg = document.getElementById("noteMsg");
  const noteTitle = document.getElementById("noteTitle");
  const noteBody  = document.getElementById("noteBody");
  const noteTag   = document.getElementById("noteTag");
  const saveBtn   = document.getElementById("saveNoteBtn");
  const clearBtn  = document.getElementById("clearBtn");

  // Right side list + viewer (inside #savedNotes)
  function renderEmpty(){
    listMount.innerHTML = `<div class="empty">No notes yet</div>`;
  }

  function renderList(notes){
    if (!notes || !notes.length) return renderEmpty();

    listMount.innerHTML = "";
    for (const n of notes){
      const row = document.createElement("div");
      row.className = "row glass";
      row.innerHTML = `
        <div class="row-left">
          <div class="row-title">${esc(n.title || "Untitled")}</div>
          <div class="row-sub">${esc(n.tag || "general")} • ${fmtDate(n.updated_at || n.created_at) || "—"}</div>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
          <button class="btn btn-ghost" data-view="${n.id}">View</button>
          <button class="btn" data-del="${n.id}">Delete</button>
        </div>
      `;
      listMount.appendChild(row);
    }

    // Click handlers
    listMount.querySelectorAll("button[data-view]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-view");
        await openViewer(id);
      });
    });

    listMount.querySelectorAll("button[data-del]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-del");
        const ok = confirm("Delete this note?");
        if (!ok) return;
        await deleteNote(id);
      });
    });
  }

  async function loadNotes(){
    const { r, j, text } = await fetchJson("/api/notes");
    if (!j) {
      console.error("Notes non-JSON:", r.status, text);
      listMount.innerHTML = `<div class="empty">Notes failed to load</div>`;
      return;
    }
    if (!r.ok || !j.ok){
      console.error("Notes JSON error:", j);
      listMount.innerHTML = `<div class="empty">Notes failed to load</div>`;
      return;
    }
    renderList(j.notes || []);
  }

  async function saveNote(){
    showInlineMsg(noteMsg, "", false);

    const title = (noteTitle.value || "").trim() || "Untitled";
    const body  = (noteBody.value || "").trim();
    const tag   = (noteTag.value || "").trim() || "general";

    if (!body){
      showInlineMsg(noteMsg, "Write something first", true);
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";

    const { r, j, text } = await fetchJson("/api/notes", {
      method: "POST",
      headers: { "Content-Type":"application/json" },
      body: JSON.stringify({ title, body, tag })
    });

    saveBtn.disabled = false;
    saveBtn.textContent = "Save";

    if (!j){
      console.error("Save note non-JSON:", r.status, text);
      showInlineMsg(noteMsg, "Save failed (check console)", true);
      return;
    }
    if (!r.ok || !j.ok){
      console.error("Save note JSON error:", j);
      showInlineMsg(noteMsg, j.error || "Save failed", true);
      return;
    }

    // Clear fields (keeps title if you want? I’ll fully clear)
    noteTitle.value = "";
    noteBody.value = "";
    noteTag.value = "";

    showInlineMsg(noteMsg, "Saved", false);
    await loadNotes();
  }

  async function deleteNote(id){
    const { r, j, text } = await fetchJson(`/api/notes/${id}`, { method:"DELETE" });
    if (!j){
      console.error("Delete non-JSON:", r.status, text);
      alert("Delete failed (check console)");
      return;
    }
    if (!r.ok || !j.ok){
      console.error("Delete JSON error:", j);
      alert(j.error || "Delete failed");
      return;
    }
    await loadNotes();
  }

  async function openViewer(id){
    const { r, j, text } = await fetchJson(`/api/notes/${id}`);
    if (!j){
      console.error("View non-JSON:", r.status, text);
      alert("Open failed (check console)");
      return;
    }
    if (!r.ok || !j.ok){
      console.error("View JSON error:", j);
      alert(j.error || "Open failed");
      return;
    }

    const note = j.note || {};
    // Minimal modal overlay
    const overlay = document.createElement("div");
    overlay.style.position = "fixed";
    overlay.style.inset = "0";
    overlay.style.background = "rgba(0,0,0,.55)";
    overlay.style.display = "grid";
    overlay.style.placeItems = "center";
    overlay.style.padding = "22px";
    overlay.style.zIndex = "9999";

    const card = document.createElement("div");
    card.className = "glass";
    card.style.width = "min(900px, 96vw)";
    card.style.maxHeight = "86vh";
    card.style.overflow = "auto";
    card.style.padding = "18px";

    card.innerHTML = `
      <div class="card-head" style="align-items:center;">
        <div style="min-width:0;">
          <h2 style="margin:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(note.title || "Untitled")}</h2>
          <div class="muted">${esc(note.tag || "general")} • ${fmtDate(note.updated_at || note.created_at) || "—"}</div>
        </div>
        <div style="display:flex; gap:10px;">
          <button class="btn btn-ghost" id="copyBtn" type="button">Copy</button>
          <button class="btn" id="closeBtn" type="button">Close</button>
        </div>
      </div>

      <div class="divider"></div>

      <div style="white-space:pre-wrap; line-height:1.45; font-size:1rem; color:rgba(231,238,255,.92);">
        ${esc(note.body || "")}
      </div>
    `;

    overlay.appendChild(card);
    document.body.appendChild(overlay);

    card.querySelector("#closeBtn").onclick = () => overlay.remove();
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) overlay.remove();
    });

    card.querySelector("#copyBtn").onclick = async () => {
      try {
        await navigator.clipboard.writeText(String(note.body || ""));
        card.querySelector("#copyBtn").textContent = "Copied";
        setTimeout(() => (card.querySelector("#copyBtn").textContent = "Copy"), 900);
      } catch {
        alert("Copy failed");
      }
    };
  }

  clearBtn.onclick = () => {
    noteTitle.value = "";
    noteBody.value = "";
    noteTag.value = "";
    showInlineMsg(noteMsg, "", false);
  };

  saveBtn.onclick = saveNote;

  // Load on start
  loadNotes().catch(err => {
    console.error(err);
    renderEmpty();
  });
})();