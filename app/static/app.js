/* InternStash frontend — vanilla JS, no build step. */

const $ = (id) => document.getElementById(id);

const state = {
  statuses: [],
  boardStatuses: [],
  archivedStatuses: [],
  // Board columns that carry a show/hide button in the topbar.
  collapsibleStatuses: [],
  // Which of those are currently folded away. Persisted across reloads.
  hiddenStatuses: new Set(),
  statusLabels: {},
  apps: [],
  tagFacets: [],
  seasonFacets: [],
  selectedTags: new Set(),
  // Holds season strings; the literal "none" means "term not stated".
  selectedSeasons: new Set(),
  showArchived: false,
  query: "",
  editingId: null,
  llm: { available: false },
  // Guards against double-submit; see submitForm().
  saving: false,
  idempotencyKey: null,
  // Serialized form contents as of the moment the modal opened; null when no
  // form is open. See isDirty().
  formSnapshot: null,
};

/* A random key identifying one filled-in form, sent with the create request.
 *
 * crypto.randomUUID() is deliberately not used unconditionally: it only exists
 * in secure contexts, and this app is served over plain HTTP on a tailnet IP,
 * where it is undefined. crypto.getRandomValues() has no such restriction, so
 * the fallback is what actually runs on anything but localhost.
 */
function newIdempotencyKey() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* Today's date, in the VIEWER's local timezone, computed fresh on every call.
 *
 * Two traps this avoids:
 *  1. Caching it at page load. This is a dashboard — the tab stays open for
 *     days, so a value captured during init() silently becomes "the date I
 *     opened the tab" and every "Today" click backdates the entry.
 *  2. toISOString(), which serializes in UTC. Anywhere west of Greenwich that
 *     rolls over to tomorrow's date during the local evening (in US Eastern,
 *     from 8pm onward), so "Today" would land on tomorrow.
 */
function todayISO() {
  const now = new Date();
  const localMs = now.getTime() - now.getTimezoneOffset() * 60000;
  return new Date(localMs).toISOString().slice(0, 10);
}

/* Which columns are folded away, remembered between visits.
 *
 * localStorage throws outright in some privacy configurations rather than
 * returning null, so every access is guarded: a board that renders is worth
 * more than a remembered toggle. */
const HIDDEN_KEY = "internstash.hiddenStatuses";

function loadHiddenStatuses() {
  try {
    const raw = JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((s) => typeof s === "string") : []);
  } catch {
    return new Set();
  }
}

function saveHiddenStatuses() {
  try {
    localStorage.setItem(HIDDEN_KEY, JSON.stringify([...state.hiddenStatuses]));
  } catch { /* storage unavailable — the toggle just won't survive a reload */ }
}

// Columns currently on the board: the board statuses minus any folded away,
// plus the archived ones only when you ask to see them.
function visibleStatuses() {
  const board = state.boardStatuses.filter((s) => !state.hiddenStatuses.has(s));
  return state.showArchived ? [...board, ...state.archivedStatuses] : board;
}

/* ------------------------------------------------------------------ */
/* API helpers                                                         */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* Small self-dismissing note in the bottom-right corner. Errors linger longer
 * than confirmations, and both can be dismissed early by clicking. */
function toast(msg, isError = false) {
  if (!msg) return;
  const el = document.createElement("div");
  el.className = `toast${isError ? " error" : ""}`;
  el.textContent = msg;

  const remove = () => {
    if (!el.isConnected) return;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 250);
  };

  el.addEventListener("click", remove);
  $("toasts").append(el);
  setTimeout(remove, isError ? 6000 : 2800);
}

/* ------------------------------------------------------------------ */
/* Board rendering                                                     */
/* ------------------------------------------------------------------ */

// A deadline is only urgent while you still have to act on it. Once an
// application is in, the date is reference material — not a red flag.
const DEADLINE_URGENT_STATUSES = new Set(["suggested"]);

function deadlineClass(deadline) {
  if (!deadline) return "";
  const today = todayISO();
  if (deadline < today) return "past";
  const days = (new Date(deadline) - new Date(today)) / 86400000;
  return days <= 7 ? "soon" : "";
}

function makeCard(app) {
  const card = document.createElement("div");
  card.className = "card";
  card.style.borderLeftColor = `var(--s-${app.status})`;
  card.tabIndex = 0;

  // textContent throughout — these are user/feed strings, never markup.
  const company = document.createElement("div");
  company.className = "company";
  company.textContent = app.company;

  const role = document.createElement("div");
  role.className = "role";
  role.textContent = app.role_title;

  card.append(company, role);

  const meta = document.createElement("div");
  meta.className = "card-meta";

  if (app.deadline) {
    const urgent = DEADLINE_URGENT_STATUSES.has(app.status);
    const d = document.createElement("span");
    d.className = `deadline ${urgent ? deadlineClass(app.deadline) : "muted"}`;
    // The hourglass only appears while the deadline still needs acting on.
    d.textContent = urgent ? `⏳ ${app.deadline}` : app.deadline;
    meta.append(d);
  }

  // Term reads as its own kind of fact, so it gets a distinct chip.
  if (app.season) {
    const s = document.createElement("span");
    s.className = "season-chip";
    s.textContent = app.season;
    meta.append(s);
  }

  (app.tags || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean)
    .slice(0, 3)
    .forEach((t) => {
      const el = document.createElement("span");
      el.className = "tag";
      el.textContent = t;
      meta.append(el);
    });

  if (meta.children.length) card.append(meta);

  const open = () => openModal(app);
  card.addEventListener("click", open);
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
  });

  return card;
}

function render() {
  const board = $("board");
  board.textContent = "";

  for (const status of visibleStatuses()) {
    const items = state.apps.filter((a) => a.status === status);

    const col = document.createElement("section");
    col.className = "column";
    // A column you can fold away is reference material, not a working queue —
    // it takes a narrower share of the board than the ones you act on.
    if (state.collapsibleStatuses.includes(status)) col.classList.add("secondary");

    const head = document.createElement("div");
    head.className = "col-head";

    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = `var(--s-${status})`;

    const title = document.createElement("h3");
    title.textContent = state.statusLabels[status] || status;

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = items.length;

    head.append(dot, title, count);

    const body = document.createElement("div");
    body.className = "col-body";

    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      const filtering =
        state.query || state.selectedTags.size || state.selectedSeasons.size;
      empty.textContent = filtering ? "no matches" : "—";
      body.append(empty);
    } else {
      items.forEach((a) => body.append(makeCard(a)));
    }

    col.append(head, body);
    board.append(col);
  }

  // Counts on the toggles come from the same filtered set as the columns.
  renderStatusToggles();
}

/* ------------------------------------------------------------------ */
/* Collapsible board columns                                           */
/*                                                                     */
/* Rows for these statuses are always in state.apps, so hiding one is   */
/* a re-render, not a refetch — and the button can keep showing a live  */
/* count of what it is hiding.                                          */
/* ------------------------------------------------------------------ */

function buildStatusToggles() {
  const box = $("status-toggles");
  box.textContent = "";

  for (const status of state.collapsibleStatuses) {
    const label = state.statusLabels[status] || status;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "status-toggle";
    btn.dataset.status = status;
    btn.style.setProperty("--s-color", `var(--s-${status})`);

    const dot = document.createElement("span");
    dot.className = "dot";

    const name = document.createElement("span");
    name.textContent = label;

    const n = document.createElement("span");
    n.className = "count";

    btn.append(dot, name, n);
    btn.addEventListener("click", () => {
      if (state.hiddenStatuses.has(status)) state.hiddenStatuses.delete(status);
      else state.hiddenStatuses.add(status);
      saveHiddenStatuses();
      render();
    });

    box.append(btn);
  }

  renderStatusToggles();
}

function renderStatusToggles() {
  for (const btn of $("status-toggles").children) {
    const status = btn.dataset.status;
    const shown = !state.hiddenStatuses.has(status);
    const n = state.apps.filter((a) => a.status === status).length;
    const label = state.statusLabels[status] || status;

    btn.classList.toggle("on", shown);
    btn.setAttribute("aria-pressed", String(shown));
    btn.title = `${shown ? "Hide" : "Show"} the ${label} column`;
    btn.querySelector(".count").textContent = n;
  }
}

async function load() {
  const params = new URLSearchParams();
  if (state.query.trim()) params.set("q", state.query.trim());
  if (state.selectedTags.size) params.set("tags", [...state.selectedTags].join(","));
  if (state.selectedSeasons.size) params.set("season", [...state.selectedSeasons].join(","));
  if (state.showArchived) params.set("include_archived", "true");
  try {
    state.apps = await api(`/api/applications?${params}`);
    render();
    updateFilterChrome();
  } catch (err) {
    toast(`Could not load applications: ${err.message}`, true);
  }
}

/* ------------------------------------------------------------------ */
/* Tag filter                                                          */
/*                                                                     */
/* Facets come from the server already canonicalized, so near-synonyms  */
/* like software-engineering and software-development collapse into a   */
/* single "swe" checkbox instead of competing for the same jobs.        */
/* ------------------------------------------------------------------ */

async function loadTags() {
  const params = new URLSearchParams();
  if (state.showArchived) params.set("include_archived", "true");
  try {
    const [tagFacets, seasonFacets] = await Promise.all([
      api(`/api/tags?${params}`),
      api(`/api/seasons?${params}`),
    ]);
    state.tagFacets = tagFacets;
    state.seasonFacets = seasonFacets;
    renderTagFilter();
    renderSeasonFilter();
    fillSeasonDatalist();
  } catch {
    state.tagFacets = [];
    state.seasonFacets = [];
  }
}

/* Term is its own filter axis, separate from tags — a term is a scale, not a
 * topic, so "Summer 2027" should never sit in the same list as "pytorch". */
function renderSeasonFilter() {
  const box = $("season-list");
  box.textContent = "";

  if (!state.seasonFacets.length) {
    const none = document.createElement("span");
    none.className = "hint";
    none.textContent = "No terms yet.";
    box.append(none);
    return;
  }

  for (const facet of state.seasonFacets) {
    // null season is selectable as the literal "none".
    const value = facet.season === null ? "none" : facet.season;
    const shown = facet.season === null ? (facet.label || "not stated") : facet.season;

    const label = document.createElement("label");
    label.className = `tag-check season${
      state.selectedSeasons.has(value) ? " on" : ""
    }${facet.season === null ? " unstated" : ""}`;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selectedSeasons.has(value);
    cb.addEventListener("change", () => {
      if (cb.checked) state.selectedSeasons.add(value);
      else state.selectedSeasons.delete(value);
      label.classList.toggle("on", cb.checked);
      load();
    });

    const name = document.createElement("span");
    name.textContent = shown;

    const n = document.createElement("span");
    n.className = "n";
    n.textContent = facet.count;

    label.append(cb, name, n);
    box.append(label);
  }
}

// Feed the form's term autocomplete from terms actually in the database.
function fillSeasonDatalist() {
  const dl = $("season-options");
  dl.textContent = "";
  for (const f of state.seasonFacets) {
    if (!f.season) continue;
    const opt = document.createElement("option");
    opt.value = f.season;
    dl.append(opt);
  }
}

function renderTagFilter() {
  const box = $("tag-list");
  box.textContent = "";

  if (!state.tagFacets.length) {
    const none = document.createElement("span");
    none.className = "hint";
    none.textContent = "No tags yet.";
    box.append(none);
    return;
  }

  let skillsStarted = false;
  for (const facet of state.tagFacets) {
    // Server returns areas first; insert one divider where skills begin.
    if (!facet.is_area && !skillsStarted) {
      skillsStarted = true;
      const sep = document.createElement("span");
      sep.className = "tag-sep";
      sep.textContent = "skills";
      box.append(sep);
    }

    const label = document.createElement("label");
    label.className = `tag-check${facet.is_area ? " area" : ""}${
      state.selectedTags.has(facet.tag) ? " on" : ""
    }`;
    // Explain the merge, so "where did software-engineering go?" has an answer.
    label.title = facet.variants.length
      ? `also matches: ${facet.variants.join(", ")}`
      : facet.tag;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selectedTags.has(facet.tag);
    cb.addEventListener("change", () => {
      if (cb.checked) state.selectedTags.add(facet.tag);
      else state.selectedTags.delete(facet.tag);
      label.classList.toggle("on", cb.checked);
      load();
    });

    const name = document.createElement("span");
    name.textContent = facet.tag;

    const n = document.createElement("span");
    n.className = "n";
    n.textContent = facet.count;

    label.append(cb, name, n);
    box.append(label);
  }
}

/* The drawer is closed by default, so the trigger has to carry the signal that
 * filters are active — otherwise a filtered board looks like a broken one. */
function updateFilterChrome() {
  const activeCount =
    state.selectedTags.size + state.selectedSeasons.size + (state.showArchived ? 1 : 0);
  $("filter-clear").hidden = activeCount === 0;

  const badge = $("filter-count");
  badge.textContent = activeCount;
  badge.hidden = activeCount === 0;
  $("btn-filters").classList.toggle("has-filters", activeCount > 0);
}

function panelOpen() {
  return $("filter-panel").classList.contains("open");
}

function setPanel(open) {
  $("filter-panel").classList.toggle("open", open);
  $("panel-backdrop").classList.toggle("open", open);
  $("filter-panel").setAttribute("aria-hidden", String(!open));
  $("btn-filters").setAttribute("aria-expanded", String(open));
  if (open) $("panel-close").focus();
}

function wireFilters() {
  $("btn-filters").addEventListener("click", () => setPanel(!panelOpen()));
  $("panel-close").addEventListener("click", () => setPanel(false));
  $("panel-backdrop").addEventListener("click", () => setPanel(false));

  $("show-archived").addEventListener("change", async (e) => {
    state.showArchived = e.target.checked;
    await Promise.all([load(), loadTags()]);
  });

  $("filter-clear").addEventListener("click", async () => {
    state.selectedTags.clear();
    state.selectedSeasons.clear();
    state.showArchived = false;
    $("show-archived").checked = false;
    await Promise.all([load(), loadTags()]);
  });
}

/* ------------------------------------------------------------------ */
/* Modal                                                               */
/* ------------------------------------------------------------------ */

function setFormStatus(value) {
  const group = $("f-status");
  group.dataset.value = value;
  for (const btn of group.children) {
    btn.classList.toggle("active", btn.dataset.value === value);
  }
}

function getFormStatus() {
  return $("f-status").dataset.value ?? "suggested";
}

function openModal(app = null) {
  state.editingId = app ? app.id : null;
  // One key per opened form. Every retry of THIS form carries the same key, so
  // the server collapses them into a single row.
  state.idempotencyKey = app ? null : newIdempotencyKey();
  setSaving(false);
  $("modal-title").textContent = app ? "Edit application" : "New application";
  $("btn-delete").hidden = !app;

  $("f-id").value = app?.id ?? "";
  $("f-company").value = app?.company ?? "";
  $("f-role").value = app?.role_title ?? "";
  setFormStatus(app?.status ?? "suggested");
  $("f-url").value = app?.source_url ?? "";
  $("f-found").value = app?.date_found ?? "";
  $("f-applied").value = app?.date_applied ?? "";
  $("f-deadline").value = app?.deadline ?? "";
  $("f-tags").value = app?.tags ?? "";
  $("f-season").value = app?.season ?? "";
  $("f-notes").value = app?.notes ?? "";

  // jd_html was sanitized server-side on the way in, so it is safe to render.
  $("f-jd").innerHTML = app?.jd_html ?? "";

  // A brand-new entry was, by definition, found today.
  if (!app) $("f-found").value = todayISO();

  setStatus("");
  refreshTodayButtons();
  refreshOpenButton();

  state.formSnapshot = null;
  $("modal").hidden = false;

  // New entry starts in the paste box — that is step one of the flow.
  if (app) $("f-company").focus();
  else $("f-jd").focus();

  // Baseline for the unsaved-changes check, taken only once the dialog is on
  // screen and focused. Both of those steps can mutate the contenteditable by
  // themselves (browsers inject a <br> into an empty editable on focus), and a
  // baseline captured before them would never match the first comparison.
  requestAnimationFrame(() => {
    state.formSnapshot = formFingerprint();
  });
}

/* Unconditional close. Only call this once the work is safe -- i.e. saved, or
 * the user has explicitly agreed to discard. Accidental closes go through
 * attemptClose() instead. */
function closeModal() {
  $("modal").hidden = true;
  state.editingId = null;
  state.formSnapshot = null;
}

/* True when the form differs from how it was opened. Compares the same payload
 * that would be submitted, so it tracks real content rather than focus or
 * keystroke events -- typing a character and deleting it again is not dirty. */
/* What counts as "the user changed something".
 *
 * Deliberately NOT formPayload() verbatim: jd_text_plain comes from the
 * editor's innerText, which is layout-dependent. It returns textContent (no
 * line breaks between blocks) while the dialog is display:none and real
 * rendered text once it is visible, so including it reported every freshly
 * opened entry as modified. jd_html already captures any genuine edit to the
 * description, and the server re-derives the plain text on save anyway.
 */
function formFingerprint() {
  const fields = formPayload();
  delete fields.jd_text_plain;
  return JSON.stringify(fields);
}

function isDirty() {
  if (state.formSnapshot === null) return false;
  return formFingerprint() !== state.formSnapshot;
}

/* Name the field that changed. A false positive here is invisible from the UI —
 * the dialog just refuses to close — so make it one console line to diagnose. */
function dirtyFields() {
  if (state.formSnapshot === null) return [];
  const now = formPayload();
  delete now.jd_text_plain;
  let before;
  try { before = JSON.parse(state.formSnapshot); } catch { return ["<unparseable>"]; }
  return Object.keys(now).filter((k) => JSON.stringify(now[k]) !== JSON.stringify(before[k]));
}

/* Draw the eye back to the dialog when a close is refused. */
function nudgeModal() {
  const el = document.querySelector(".modal");
  el.classList.remove("nudge");
  void el.offsetWidth; // restart the animation
  el.classList.add("nudge");
}

/* Every close path that could be accidental routes through here.
 *
 * A stray backdrop click is the one that actually costs work, so it is simply
 * refused while there are unsaved changes -- no dialog to dismiss, nothing
 * lost. Esc and Cancel are deliberate acts, so they ask instead of refusing,
 * which keeps a real "throw this away" possible. */
function attemptClose(source) {
  if (state.saving) return;          // a save is in flight; let it finish
  if (!isDirty()) { closeModal(); return; }

  if (source === "backdrop") {
    console.debug("InternStash: close refused, changed fields:", dirtyFields());
    nudgeModal();
    toast("Unsaved changes — use Save, or Cancel to discard.", true);
    return;
  }

  if (confirm("Discard your unsaved changes to this entry?")) closeModal();
}

function formPayload() {
  const editor = $("f-jd");
  const url = $("f-url").value.trim();
  return {
    company: $("f-company").value.trim(),
    role_title: $("f-role").value.trim(),
    status: getFormStatus(),
    source_url: url || null,
    date_found: $("f-found").value || null,
    date_applied: $("f-applied").value || null,
    deadline: $("f-deadline").value || null,
    tags: $("f-tags").value.trim(),
    season: $("f-season").value.trim() || null,
    notes: $("f-notes").value,
    // Both clipboard flavours, derived from the one visible editor so manual
    // edits after a paste are preserved. Server sanitizes jd_html again.
    jd_html: editor.innerHTML.trim(),
    jd_text_plain: editor.innerText.trim(),
  };
}

/* Visually and functionally lock the form while a save is in flight. */
function setSaving(on) {
  state.saving = on;
  const btn = $("btn-save");
  btn.disabled = on;
  btn.textContent = on ? "Saving…" : "Save";
  $("btn-delete").disabled = on;
}

async function submitForm(e) {
  e.preventDefault();

  // First line of defence: ignore submits while one is already running. The
  // six-duplicate incident happened inside a single second, i.e. well within
  // one round trip, so this is what stops the burst at source. The server's
  // idempotency key is the actual guarantee if this ever fails.
  if (state.saving) return;

  const payload = formPayload();
  if (!payload.company || !payload.role_title) {
    toast("Company and role title are required.", true);
    return;
  }

  setSaving(true);
  try {
    if (state.editingId) {
      await api(`/api/applications/${state.editingId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/applications", {
        method: "POST",
        body: JSON.stringify({ ...payload, idempotency_key: state.idempotencyKey }),
      });
    }
    closeModal();
    await Promise.all([load(), loadTags()]);
    toast("Saved.");
  } catch (err) {
    toast(`Save failed: ${err.message}`, true);
  } finally {
    setSaving(false);
  }
}

async function deleteEntry() {
  if (!state.editingId) return;
  if (!confirm("Delete this application? This cannot be undone.")) return;
  try {
    await api(`/api/applications/${state.editingId}`, { method: "DELETE" });
    closeModal();
    await Promise.all([load(), loadTags()]);
    toast("Deleted.");
  } catch (err) {
    toast(`Delete failed: ${err.message}`, true);
  }
}

/* ------------------------------------------------------------------ */
/* Rich paste                                                          */
/*                                                                     */
/* Capture both text/html and text/plain from the clipboard event. The  */
/* HTML flavour goes to the server to be sanitized BEFORE it touches    */
/* the live DOM — inline handlers such as onerror still fire when raw   */
/* markup is inserted via innerHTML, so pasting unsanitized would be an */
/* XSS sink even though <script> stays inert.                           */
/* ------------------------------------------------------------------ */

async function handlePaste(e) {
  const cd = e.clipboardData;
  if (!cd) return;

  const html = cd.getData("text/html");
  const text = cd.getData("text/plain");

  e.preventDefault();

  if (!html) {
    // Plain-text paste: insert as text, no sanitizing needed.
    document.execCommand("insertText", false, text);
    return;
  }

  const editor = $("f-jd");
  editor.classList.add("busy");
  try {
    const clean = await api("/api/sanitize", {
      method: "POST",
      body: JSON.stringify({ html }),
    });
    // Sanitized server-side; safe to insert.
    document.execCommand("insertHTML", false, clean.html);
  } catch (err) {
    // Fall back to the plain-text flavour rather than risking raw HTML.
    document.execCommand("insertText", false, text);
    toast(`Kept plain text only — sanitizer unreachable (${err.message}).`, true);
  } finally {
    editor.classList.remove("busy");
  }

  // Paste is step one; extraction follows automatically.
  autofill({ auto: true });
}

/* ------------------------------------------------------------------ */
/* LLM autofill                                                        */
/*                                                                     */
/* Runs against a local Ollama model — nothing leaves the machine.      */
/* Only ever fills fields that are currently EMPTY, so it cannot        */
/* overwrite something you typed. Notes are never touched.              */
/* ------------------------------------------------------------------ */

function setStatus(msg, cls = "") {
  const el = $("autofill-status");
  el.textContent = msg;
  el.className = `autofill-status ${cls}`;
  el.hidden = !msg;
}

function fillIfEmpty(id, value) {
  if (!value) return false;
  const el = $(id);
  if (el.value.trim()) return false; // never clobber your input
  el.value = value;
  el.classList.remove("autofilled");
  void el.offsetWidth; // restart the flash animation
  el.classList.add("autofilled");
  return true;
}

async function autofill({ auto = false } = {}) {
  if (!state.llm.available) return;

  const text = $("f-jd").innerText.trim();
  if (text.length < 40) {
    if (!auto) setStatus("Paste a job description first.", "failed");
    return;
  }

  const btn = $("btn-autofill");
  btn.disabled = true;
  setStatus("✨ Reading the posting…", "working");

  // After a reboot the model has to be loaded from disk, which can take ~10s or
  // more. Without this the UI looks identical whether it will take 2s or 90s,
  // and a slow first run reads as a hang.
  const slowNotice = setTimeout(() => {
    setStatus("✨ Loading the model — the first run after a reboot is slower…", "working");
  }, 5000);

  try {
    const r = await api("/api/extract", {
      method: "POST",
      body: JSON.stringify({ text }),
    });

    if (!r.ok) {
      setStatus(`Autofill unavailable — ${r.error}. Fill the fields by hand.`, "failed");
      return;
    }

    const filled = [];
    if (fillIfEmpty("f-company", r.company)) filled.push("company");
    if (fillIfEmpty("f-role", r.role_title)) filled.push("role");
    if (fillIfEmpty("f-deadline", r.deadline)) filled.push("deadline");
    if (fillIfEmpty("f-url", r.source_url)) filled.push("source URL");
    if (fillIfEmpty("f-tags", r.tags)) filled.push("tags");
    if (fillIfEmpty("f-season", r.season)) filled.push("term");

    refreshTodayButtons();
    refreshOpenButton();

    setStatus(
      filled.length
        ? `Filled ${filled.join(", ")} — check before saving.`
        : "Nothing new to fill (existing values kept)."
    );
  } catch (err) {
    setStatus(`Autofill failed — ${err.message}. Fill the fields by hand.`, "failed");
  } finally {
    clearTimeout(slowNotice);
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Today buttons                                                       */
/* ------------------------------------------------------------------ */

function refreshTodayButtons() {
  document.querySelectorAll(".today-btn[data-target]").forEach((b) => {
    b.classList.toggle("is-today", $(b.dataset.target).value === todayISO());
  });
}

// Filling in an application date is a strong enough signal of intent that we
// bump the status for you -- but only forward, from the triage column. If
// status was already moved on (or moved back) by hand, leave it alone.
function maybeAutoApplyStatus() {
  if ($("f-applied").value && getFormStatus() === "suggested") {
    setFormStatus("applied");
  }
}

/* ------------------------------------------------------------------ */
/* Open source URL                                                     */
/* ------------------------------------------------------------------ */

function isOpenableUrl(v) {
  return /^https?:\/\/\S+$/i.test((v || "").trim());
}

function refreshOpenButton() {
  $("btn-open-url").disabled = !isOpenableUrl($("f-url").value);
}

function wireOpenUrl() {
  const input = $("f-url");
  $("btn-open-url").addEventListener("click", () => {
    const url = input.value.trim();
    // Guard again at click time: only ever hand http(s) to window.open, so a
    // javascript:/data: value typed into the field can't be executed.
    if (!isOpenableUrl(url)) return;
    window.open(url, "_blank", "noopener,noreferrer");
  });
  input.addEventListener("input", refreshOpenButton);
}

function wireTodayButtons() {
  document.querySelectorAll(".today-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = $(btn.dataset.target);
      // Second click clears, so it doubles as an undo.
      const today = todayISO();
      input.value = input.value === today ? "" : today;
      refreshTodayButtons();
      if (btn.dataset.target === "f-applied") maybeAutoApplyStatus();
    });
  });
  ["f-found", "f-applied", "f-deadline"].forEach((id) =>
    $(id).addEventListener("change", refreshTodayButtons)
  );
  $("f-applied").addEventListener("change", maybeAutoApplyStatus);
}

/* ------------------------------------------------------------------ */
/* Feed fetch                                                          */
/* ------------------------------------------------------------------ */

async function runFetch() {
  const btn = $("btn-fetch");
  btn.disabled = true;
  btn.textContent = "Fetching…";
  try {
    const r = await api("/api/fetch/run", { method: "POST" });
    if (r.ok) {
      toast(`Feed: ${r.fetched} postings, ${r.matched} matched keywords, ${r.inserted} new.`);
      await Promise.all([load(), loadTags()]);
    } else {
      toast(`Fetch failed: ${r.error}`, true);
    }
  } catch (err) {
    toast(`Fetch failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch feed";
  }
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function init() {
  const meta = await api("/api/meta");
  state.statuses = meta.statuses;
  state.boardStatuses = meta.board_statuses || meta.statuses;
  state.archivedStatuses = meta.archived_statuses || [];
  state.collapsibleStatuses = (meta.collapsible_statuses || []).filter((s) =>
    state.boardStatuses.includes(s)
  );
  state.hiddenStatuses = new Set(
    [...loadHiddenStatuses()].filter((s) => state.collapsibleStatuses.includes(s))
  );
  state.statusLabels = meta.status_labels || {};
  state.llm = meta.llm || { available: false };
  // Print which frontend build is actually loaded, so a stale cached bundle is
  // identifiable instead of looking like a logic bug.
  console.info(`InternStash frontend build ${meta.build || "?"}`);

  // Only offer autofill if a local model is actually reachable — no dead button
  // for anyone running this without Ollama.
  $("btn-autofill").hidden = !state.llm.available;
  if (state.llm.available) {
    $("btn-autofill").title = `Extract fields with ${state.llm.model} (local)`;
  }

  const statusGroup = $("f-status");
  for (const s of state.statuses) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "status-btn";
    btn.dataset.value = s;
    btn.style.setProperty("--s-color", `var(--s-${s})`);
    btn.textContent = state.statusLabels[s] || s;
    btn.addEventListener("click", () => setFormStatus(s));
    statusGroup.append(btn);
  }

  buildStatusToggles();

  $("btn-new").addEventListener("click", () => openModal());
  $("btn-fetch").addEventListener("click", runFetch);
  $("modal-close").addEventListener("click", () => attemptClose("cancel"));
  $("btn-cancel").addEventListener("click", () => attemptClose("cancel"));
  $("btn-delete").addEventListener("click", deleteEntry);
  $("entry-form").addEventListener("submit", submitForm);
  $("f-jd").addEventListener("paste", handlePaste);
  $("btn-autofill").addEventListener("click", () => autofill());
  wireTodayButtons();
  wireOpenUrl();
  wireFilters();

  const search = $("search");
  search.addEventListener("input", debounce(() => {
    state.query = search.value;
    $("search-clear").hidden = !search.value;
    load();
  }, 200));

  $("search-clear").addEventListener("click", () => {
    search.value = "";
    state.query = "";
    $("search-clear").hidden = true;
    load();
  });

  // Click the backdrop (not the dialog) to dismiss.
  $("modal").addEventListener("click", (e) => {
    if (e.target === $("modal")) attemptClose("backdrop");
  });

  // The browser's own guard, for closing the tab or hitting Back. Only fires
  // when there is genuinely something to lose.
  window.addEventListener("beforeunload", (e) => {
    if (!$("modal").hidden && isDirty()) {
      e.preventDefault();
      e.returnValue = "";   // required by older browsers to trigger the prompt
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Modal sits above the drawer, so it closes first.
      if (!$("modal").hidden) attemptClose("escape");
      else if (panelOpen()) setPanel(false);
    }
    if (e.key === "/" && document.activeElement === document.body) {
      e.preventDefault();
      search.focus();
    }
  });

  await Promise.all([load(), loadTags()]);
}

init().catch((err) => toast(`Startup failed: ${err.message}`, true));
