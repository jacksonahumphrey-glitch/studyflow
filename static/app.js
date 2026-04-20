async function apiGet(url){
  const r = await fetch(url, { credentials: "same-origin" });
  return await r.json();
}
async function apiPost(url, body){
  const r = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
  return await r.json();
}
async function apiPatch(url, body){
  const r = await fetch(url, {
    method: "PATCH",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
  return await r.json();
}
async function apiDelete(url){
  const r = await fetch(url, { method:"DELETE", credentials:"same-origin" });
  return await r.json();
}

function el(id){ return document.getElementById(id); }
function safe(s){ return (s ?? "").toString(); }

function fmtDue(d){
  if(!d) return "No due date";
  return d;
}

function page(){
  const p = location.pathname;
  if (p === "/today") return "today";
  if (p === "/inputs") return "inputs";
  if (p === "/resources") return "resources";
  if (p === "/active-recall") return "recall";
  if (p === "/settings") return "settings";
  return "other";
}

/* ---------------- Today ---------------- */
async function loadToday(){
  const me = await apiGet("/api/me");
  if(me.ok){
    el("welcomeLine").textContent = `Hi, ${me.user.name || "User"}`;
  }

  const settings = await apiGet("/api/settings");
  let avail = 120;
  if(settings.ok){
    avail = settings.settings.available_minutes;
  }

  // Load assignments for Tasks
  const a = await apiGet("/api/assignments");
  const taskList = el("taskList");
  if(taskList){
    taskList.innerHTML = "";
    if(a.ok && a.assignments.length){
      const open = a.assignments.filter(x => x.status === "open");
      el("taskMeta").textContent = `${open.length} open`;
      open.forEach(item => {
        const div = document.createElement("div");
        div.className = "item";
        div.innerHTML = `
          <div>
            <div class="title">${safe(item.title)}</div>
            <div class="meta">${safe(item.class_name || "")} • ${fmtDue(item.due_date)} • ${item.minutes}m</div>
          </div>
          <div class="actions">
            <button class="btn" data-done="${item.id}">Done</button>
          </div>
        `;
        taskList.appendChild(div);
      });

      taskList.querySelectorAll("button[data-done]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const id = btn.getAttribute("data-done");
          await apiPatch(`/api/assignments/${id}`, { status:"done" });
          loadToday();
        });
      });
    } else {
      el("taskMeta").textContent = "0 open";
      taskList.innerHTML = `<div class="muted">No open tasks yet. Add one in Inputs.</div>`;
    }
  }

  // Load cached plan
  const planRes = await apiGet("/api/plan/today");
  renderPlan(planRes.ok ? planRes.plan : null, avail);

  // Generate button
  const gen = el("btnGeneratePlan");
  if(gen){
    gen.addEventListener("click", async (e) => {
      e.preventDefault();

      const hoursEl = el("availHours");
      const minutesEl = el("availMinutes");

      const hours = parseInt(hoursEl?.value || "0", 10) || 0;
      const minutes = parseInt(minutesEl?.value || "0", 10) || 0;
      const totalMinutes = (hours * 60) + minutes;

      if (totalMinutes <= 0) {
        return;
      }

      gen.disabled = true;
      gen.textContent = "Generating…";

      const out = await apiPost("/api/plan/generate", { available_minutes: totalMinutes });

      gen.disabled = false;
      gen.textContent = "Generate Today’s Plan";

      if(out.ok){
        renderPlan(out.plan, totalMinutes);
      }
    });
  }
}

function renderPlan(plan, avail){
  const prioList = el("prioList");
  const blockList = el("blockList");
  const leftoverLine = el("leftoverLine");
  const focusTitle = el("focusTitle");
  const focusSub = el("focusSub");
  const focusMeta = el("focusMeta");

  if(!prioList || !blockList) return;

  prioList.innerHTML = "";
  blockList.innerHTML = "";

  if(!plan){
    focusTitle.textContent = "No plan yet";
    focusSub.textContent = "Press “Generate Today’s Plan”.";
    focusMeta.textContent = `${avail}m available`;
    leftoverLine.textContent = "";
    return;
  }

  focusMeta.textContent = `${plan.available_minutes}m available`;

  const next = plan.next;
  if(next){
    focusTitle.textContent = next.title || "Primary Focus";
    const cn = next.class_name ? `${next.class_name} • ` : "";
    focusSub.textContent = `${cn}${fmtDue(next.due_date)} • ${next.minutes}m`;
  } else {
    focusTitle.textContent = "All clear";
    focusSub.textContent = "No open assignments.";
  }

  (plan.top3 || []).forEach(t => {
    const li = document.createElement("li");
    li.className = "item";
    li.innerHTML = `
      <div>
        <div class="title">${safe(t.title)}</div>
        <div class="meta">${safe(t.class_name || "")} • ${fmtDue(t.due_date)} • ${t.minutes}m</div>
      </div>
    `;
    prioList.appendChild(li);
  });

  (plan.time_blocks || []).forEach(b => {
    const li = document.createElement("li");
    li.className = "item";
    li.innerHTML = `
      <div>
        <div class="title">${safe(b.title)}</div>
        <div class="meta">${safe(b.class || "")} • ${b.minutes}m</div>
      </div>
    `;
    blockList.appendChild(li);
  });

  const used = (plan.time_blocks || []).reduce((s,x)=>s+(x.minutes||0), 0);
  const left = Math.max(0, (plan.available_minutes||avail) - used);
  leftoverLine.textContent = left ? `${left} minutes left unassigned.` : `All available time assigned.`;
}

/* ---------------- Inputs ---------------- */
async function loadInputs(){
  const classes = await apiGet("/api/classes");
  const assignments = await apiGet("/api/assignments");

  const classList = el("classList");
  const assignClass = el("assignClass");
  const assignmentList = el("assignmentList");

  // Classes
  if(classList){
    classList.innerHTML = "";
    if(classes.ok && classes.classes.length){
      classes.classes.forEach(c => {
        const div = document.createElement("div");
        div.className = "item";
        div.innerHTML = `
          <div>
            <div class="title">${safe(c.name)}</div>
            <div class="meta">Class</div>
          </div>
          <div class="actions">
            <button class="btn" data-del-class="${c.id}">Delete</button>
          </div>
        `;
        classList.appendChild(div);
      });

      classList.querySelectorAll("button[data-del-class]").forEach(btn=>{
        btn.addEventListener("click", async ()=>{
          const id = btn.getAttribute("data-del-class");
          await apiDelete(`/api/classes/${id}`);
          loadInputs();
        });
      });
    } else {
      classList.innerHTML = `<div class="muted">No classes yet.</div>`;
    }
  }

  // Class select
  if(assignClass){
    assignClass.innerHTML = `<option value="">No class</option>`;
    if(classes.ok){
      classes.classes.forEach(c=>{
        const opt = document.createElement("option");
        opt.value = c.id;
        opt.textContent = c.name;
        assignClass.appendChild(opt);
      });
    }
  }

  // Assignments
  if(assignmentList){
    assignmentList.innerHTML = "";
    if(assignments.ok && assignments.assignments.length){
      const open = assignments.assignments.filter(x => x.status === "open");
      open.forEach(a=>{
        const div = document.createElement("div");
        div.className = "item";
        div.innerHTML = `
          <div>
            <div class="title">${safe(a.title)}</div>
            <div class="meta">${safe(a.class_name || "")} • ${fmtDue(a.due_date)} • ${a.minutes}m</div>
          </div>
          <div class="actions">
            <button class="btn" data-done="${a.id}">Done</button>
            <button class="btn" data-del="${a.id}">Delete</button>
          </div>
        `;
        assignmentList.appendChild(div);
      });

      assignmentList.querySelectorAll("button[data-done]").forEach(btn=>{
        btn.addEventListener("click", async ()=>{
          const id = btn.getAttribute("data-done");
          await apiPatch(`/api/assignments/${id}`, { status:"done" });
          loadInputs();
        });
      });
      assignmentList.querySelectorAll("button[data-del]").forEach(btn=>{
        btn.addEventListener("click", async ()=>{
          const id = btn.getAttribute("data-del");
          await apiDelete(`/api/assignments/${id}`);
          loadInputs();
        });
      });
    } else {
      assignmentList.innerHTML = `<div class="muted">No assignments yet.</div>`;
    }
  }

  // Actions
  const btnAddClass = el("btnAddClass");
  if(btnAddClass){
    btnAddClass.onclick = async ()=>{
      const name = safe(el("className").value).trim();
      if(!name) return;
      await apiPost("/api/classes", { name });
      el("className").value = "";
      loadInputs();
    };
  }

  const btnAddAssignment = el("btnAddAssignment");
  if(btnAddAssignment){
    btnAddAssignment.onclick = async ()=>{
      const title = safe(el("assignTitle").value).trim();
      const due_date = safe(el("assignDue").value).trim();
      const minutes = parseInt(el("assignMinutes").value || "30", 10);
      const class_id = el("assignClass").value || null;
      if(!title) return;
      await apiPost("/api/assignments", { title, due_date, minutes, class_id });
      el("assignTitle").value = "";
      loadInputs();
    };
  }
}

/* ---------------- Resources ---------------- */
async function loadResources(){
  const notes = await apiGet("/api/notes");
  const noteList = el("noteList");
  const noteMeta = el("noteMeta");

  function render(list){
    if(!noteList) return;
    noteList.innerHTML = "";
    if(!list.length){
      noteMeta.textContent = "0 notes";
      noteList.innerHTML = `<div class="muted">No notes yet.</div>`;
      return;
    }
    noteMeta.textContent = `${list.length} notes`;

    list.forEach(n=>{
      const div = document.createElement("div");
      div.className = "item";
      div.innerHTML = `
        <div>
          <div class="title">${safe(n.title)}</div>
          <div class="meta">${safe(n.tag)} • updated ${safe(n.updated_at || n.created_at || "")}</div>
        </div>
        <div class="actions">
          <button class="btn" data-open="${n.id}">Open</button>
          <button class="btn" data-del="${n.id}">Delete</button>
        </div>
      `;
      noteList.appendChild(div);
    });

    noteList.querySelectorAll("button[data-open]").forEach(btn=>{
      btn.addEventListener("click", async ()=>{
        const id = btn.getAttribute("data-open");
        const full = await apiGet(`/api/notes/${id}`);
        if(full.ok){
          alert(`${full.note.title}\n\n${full.note.body}`);
        }
      });
    });

    noteList.querySelectorAll("button[data-del]").forEach(btn=>{
      btn.addEventListener("click", async ()=>{
        const id = btn.getAttribute("data-del");
        await apiDelete(`/api/notes/${id}`);
        loadResources();
      });
    });
  }

  const all = (notes.ok ? notes.notes : []);
  render(all);

  const btnSave = el("btnSaveNote");
  if(btnSave){
    btnSave.onclick = async ()=>{
      const title = safe(el("noteTitle").value).trim() || "Untitled";
      const body = safe(el("noteBody").value).trim();
      const tag = safe(el("noteTag").value).trim() || "general";
      if(!body) return;
      await apiPost("/api/notes", { title, body, tag });
      el("noteTitle").value = "";
      el("noteBody").value = "";
      loadResources();
    };
  }

  const noteSearch = el("noteSearch");
  const noteFilter = el("noteFilter");
  function apply(){
    const q = safe(noteSearch?.value).toLowerCase().trim();
    const f = safe(noteFilter?.value).trim();
    let filtered = all.slice();
    if(f) filtered = filtered.filter(n => n.tag === f);
    if(q) filtered = filtered.filter(n => safe(n.title).toLowerCase().includes(q) || safe(n.tag).toLowerCase().includes(q));
    render(filtered);
  }
  if(noteSearch) noteSearch.addEventListener("input", apply);
  if(noteFilter) noteFilter.addEventListener("change", apply);
}

/* ---------------- Active Recall ---------------- */
/* NOTE: Your backend endpoints for flashcards/quizzes may not exist yet.
   This page stays clean UI-first. If endpoints exist later, wire them in. */
async function loadRecall(){
  // Placeholder UX (doesn't break anything)
  const fcList = el("fcList");
  const quizList = el("quizList");
  if(fcList) fcList.innerHTML = `<div class="muted">Flashcard review wiring can be added next.</div>`;
  if(quizList) quizList.innerHTML = `<div class="muted">Quiz question UI can be added next.</div>`;

  const fcMeta = el("fcMeta");
  const quizMeta = el("quizMeta");
  if(fcMeta) fcMeta.textContent = "beta";
  if(quizMeta) quizMeta.textContent = "beta";
}

/* ---------------- Settings ---------------- */
async function loadSettings(){
  const s = await apiGet("/api/settings");
  const me = await apiGet("/api/me");

  if(s.ok){
    el("modeSelect").value = s.settings.mode || "school";
    el("availMinutes").value = s.settings.available_minutes || 120;
    el("settingsMeta").textContent = `${s.settings.mode} • ${s.settings.available_minutes}m`;
    el("profileStreak").textContent = `${s.settings.streak || 0}`;
  }
  if(me.ok){
    el("profileName").textContent = me.user.name || "User";
    el("profileEmail").textContent = me.user.email || "";
  }

  const btnSave = el("btnSaveSettings");
  if(btnSave){
    btnSave.onclick = async ()=>{
      const mode = el("modeSelect").value;
      const available_minutes = parseInt(el("availMinutes").value || "120", 10);
      const out = await apiPost("/api/settings", { mode, available_minutes });
      if(out.ok) loadSettings();
    };
  }
}

/* Boot */
document.addEventListener("DOMContentLoaded", ()=>{
  const p = page();
  if(p === "today") loadToday();
  if(p === "inputs") loadInputs();
  if(p === "resources") loadResources();
  if(p === "recall") loadRecall();
  if(p === "settings") loadSettings();
});