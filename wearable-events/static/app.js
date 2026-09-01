// --- tab switching ---
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

function escapeHtml(str) {
  // Any place user-typed or externally-sourced text (a regex keyword
  // rule pattern, a calendar name, a tag, a username, an ICS event
  // title from someone else's calendar invite) gets inserted into a
  // template literal that's later assigned to .innerHTML MUST go
  // through this first. Without it, the browser's HTML parser reads
  // characters like < > & as markup rather than literal text - a
  // regex pattern like <\d{3}> gets parsed as an (invalid) HTML tag
  // and silently vanishes from what's rendered, which is exactly the
  // "characters disappear" bug this fixes. Not primarily a security
  // fix (this is a single-user, self-hosted app) - it's a display-
  // correctness fix, since the disappearing content was never
  // executable, just mis-parsed as markup instead of text.
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  // /auth/login's own 401 means "wrong credentials", not "your session
  // expired" - you're never in a session yet at that point, so treating
  // it the same way masks the real reason (e.g. "invalid username or
  // password") behind a misleading message. Let it fall through to the
  // generic error handling below instead.
  if (resp.status === 401 && path !== "/auth/login") {
    // Session expired or was revoked mid-use - drop back to the login
    // screen rather than leaving the UI in a broken half-authenticated
    // state. showLogin is defined later in this file but hoisted since
    // it's a function declaration.
    showLogin();
    throw new Error("session expired - please log in again");
  }
  if (!resp.ok) {
    throw new Error(data.detail || `Request failed (${resp.status})`);
  }
  return data;
}

// --- Tags tab ---
// --- Duration-tag entry ---
// Simple, stateless: tap the button, pick or type a duration, log
// immediately with that duration_min. No running timer, nothing that
// can be lost by closing the tab mid-activity.
const DURATION_QUICKPICKS = [5, 10, 15, 30, 60, 90];

async function loadTagButtons() {
  const container = document.getElementById("tag-buttons");
  try {
    const defs = await api("/tag_definitions");
    if (!defs.length) {
      container.innerHTML = '<p class="muted">No tag buttons configured yet.</p>';
      return;
    }
    container.innerHTML = "";
    defs.forEach(def => {
      const btn = document.createElement("button");
      btn.className = "tag-btn";
      btn.textContent = def.label;
      if (def.is_duration) {
        btn.addEventListener("click", () => openDurationPicker(def.tag, def.label));
      } else {
        btn.addEventListener("click", () => logTag(def.tag, btn));
      }
      container.appendChild(btn);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

async function logTag(tag, btnEl) {
  const status = document.getElementById("tags-status");
  try {
    await api("/events", { method: "POST", body: JSON.stringify({ tags: [tag] }) });
    if (btnEl) {
      btnEl.classList.add("flash");
      setTimeout(() => btnEl.classList.remove("flash"), 300);
    }
    status.textContent = `Logged "${tag}"`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
}

function closeDurationPicker() {
  const picker = document.getElementById("duration-picker");
  picker.style.display = "none";
  picker.innerHTML = "";
}

function openDurationPicker(tag, label) {
  const picker = document.getElementById("duration-picker");
  picker.style.display = "block";
  picker.innerHTML = `
    <p class="section-label duration-picker-title">${label} - how long?</p>
    <div class="duration-quickpicks">
      ${DURATION_QUICKPICKS.map(m => `<button class="small-btn duration-quickpick" data-min="${m}">${m}m</button>`).join("")}
    </div>
    <div class="duration-custom-row">
      <input type="number" min="1" id="duration-custom-input" placeholder="Custom minutes">
      <button class="small-btn" id="duration-confirm-btn">Log</button>
      <button class="small-btn" id="duration-cancel-btn">Cancel</button>
    </div>
  `;

  picker.querySelectorAll(".duration-quickpick").forEach(qb => {
    qb.addEventListener("click", () => logDurationTag(tag, label, parseInt(qb.dataset.min, 10)));
  });

  const customInput = document.getElementById("duration-custom-input");
  document.getElementById("duration-confirm-btn").addEventListener("click", () => {
    const minutes = parseInt(customInput.value, 10);
    if (!minutes || minutes <= 0) {
      customInput.focus();
      return;
    }
    logDurationTag(tag, label, minutes);
  });
  customInput.addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("duration-confirm-btn").click();
  });

  document.getElementById("duration-cancel-btn").addEventListener("click", closeDurationPicker);

  customInput.focus();
}

async function logDurationTag(tag, label, minutes) {
  const status = document.getElementById("tags-status");
  status.textContent = `Logging "${label}"...`;
  try {
    await api("/events", { method: "POST", body: JSON.stringify({ tags: [tag], duration_min: minutes }) });
    status.textContent = `Logged "${label}" for ${minutes}min`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    closeDurationPicker();
  }
}

document.getElementById("custom-tag-submit").addEventListener("click", () => {
  const input = document.getElementById("custom-tag-input");
  const tag = input.value.trim();
  if (!tag) return;
  logTag(tag, null);
  input.value = "";
});

// --- Sleep tab ---
// All known qualifier chips - kept as one list so both fresh submission
// and editing can send every qualifier explicitly as true/false.
// InfluxDB only overwrites fields actually included in a write, so
// omitting a previously-true qualifier would silently leave it set
// instead of clearing it - explicit false is required to actually
// un-set one.
const KNOWN_SLEEP_QUALIFIERS = ["groggy", "woke_up_often", "vivid_dreams"];

function buildQualifiersPayload(selectedSet) {
  const qualifiers = {};
  KNOWN_SLEEP_QUALIFIERS.forEach(q => { qualifiers[q] = selectedSet.has(q); });
  return qualifiers;
}

function humanizeQualifierLabel(q) {
  const words = q.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

let selectedScore = null;
const selectedQualifiers = new Set();

document.querySelectorAll(".sleep-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".sleep-btn").forEach(b => b.classList.remove("selected"));
    btn.classList.add("selected");
    selectedScore = parseInt(btn.dataset.score, 10);
    document.getElementById("sleep-submit").disabled = false;
  });
});

function renderSleepQualifierChips() {
  // Rendered from KNOWN_SLEEP_QUALIFIERS rather than static HTML, so
  // adding/removing a qualifier there is genuinely sufficient - no
  // matching index.html edit needed. The edit-form chips (further down)
  // already worked this way; this brings the initial-submission chips
  // in line with that, closing the gap that made a JS-only addition
  // silently not show up here.
  const container = document.getElementById("sleep-qualifier-chips");
  container.innerHTML = "";
  KNOWN_SLEEP_QUALIFIERS.forEach(q => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.dataset.qualifier = q;
    chip.textContent = humanizeQualifierLabel(q);
    chip.addEventListener("click", () => {
      if (selectedQualifiers.has(q)) {
        selectedQualifiers.delete(q);
        chip.classList.remove("selected");
      } else {
        selectedQualifiers.add(q);
        chip.classList.add("selected");
      }
    });
    container.appendChild(chip);
  });
}
renderSleepQualifierChips();

document.getElementById("sleep-submit").addEventListener("click", async () => {
  const status = document.getElementById("sleep-status");
  if (selectedScore === null) return;

  const qualifiers = buildQualifiersPayload(selectedQualifiers);

  status.textContent = "Submitting...";
  try {
    const result = await api("/sleep", {
      method: "POST",
      body: JSON.stringify({ score: selectedScore, qualifiers }),
    });
    status.textContent = `Logged for ${result.sleep_date}`;
    loadSleepHistory();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Sleep history ---
const sleepEditDrafts = new Map(); // entry_id -> { score, qualifiers: Set }

function localTimeString(isoTimestamp) {
  const d = new Date(isoTimestamp);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderSleepEntryReadonly(entry) {
  const qualifierChips = KNOWN_SLEEP_QUALIFIERS
    .filter(q => entry.qualifiers && entry.qualifiers[q])
    .map(q => `<span class="chip-small">${humanizeQualifierLabel(q)}</span>`)
    .join("");
  // HH:MM alongside the date - multiple entries can now share a date
  // (e.g. one session starting just after local midnight, another
  // just before the next one; naps too), so the time is what actually
  // distinguishes them at a glance.
  const timeLabel = entry.start_time ? ` ${localTimeString(entry.start_time)}` : "";
  return `
    <div class="sleep-entry-main">
      <span class="sleep-entry-date">${escapeHtml(entry.sleep_date)}${timeLabel}</span>
      <span class="sleep-entry-score">${"●".repeat(entry.score)}${"○".repeat(5 - entry.score)}</span>
      <button class="small-btn sleep-edit-btn">Edit</button>
    </div>
    ${qualifierChips ? `<div class="timeline-tags">${qualifierChips}</div>` : ""}
  `;
}

function renderSleepEntryEditForm(entry) {
  const draft = sleepEditDrafts.get(entry.entry_id);
  const scoreButtons = [1, 2, 3, 4, 5].map(n => `
    <button class="sleep-btn ${draft.score === n ? "selected" : ""}" data-score="${n}">${n}</button>
  `).join("");
  const chips = KNOWN_SLEEP_QUALIFIERS.map(q => `
    <button class="chip ${draft.qualifiers.has(q) ? "selected" : ""}" data-qualifier="${q}">${humanizeQualifierLabel(q)}</button>
  `).join("");

  return `
    <div class="sleep-edit-form" data-entry-id="${escapeHtml(entry.entry_id)}">
      <div class="sleep-scale sleep-edit-scale">${scoreButtons}</div>
      <div class="qualifier-chips">${chips}</div>
      <div class="timeline-edit-actions">
        <button class="small-btn sleep-save-btn">Save</button>
        <button class="small-btn sleep-cancel-btn">Cancel</button>
        <button class="small-btn danger sleep-delete-btn">Delete</button>
      </div>
      <p class="sleep-edit-status status"></p>
    </div>
  `;
}

function rerenderSleepEntry(row, entry) {
  if (sleepEditDrafts.has(entry.entry_id)) {
    row.innerHTML = renderSleepEntryEditForm(entry);
    wireSleepEditForm(row, entry);
  } else {
    row.innerHTML = renderSleepEntryReadonly(entry);
    row.querySelector(".sleep-edit-btn").addEventListener("click", () => {
      sleepEditDrafts.set(entry.entry_id, {
        score: entry.score,
        qualifiers: new Set(KNOWN_SLEEP_QUALIFIERS.filter(q => entry.qualifiers && entry.qualifiers[q])),
      });
      rerenderSleepEntry(row, entry);
    });
  }
}

function wireSleepEditForm(row, entry) {
  const form = row.querySelector(".sleep-edit-form");
  const status = form.querySelector(".sleep-edit-status");
  const draft = sleepEditDrafts.get(entry.entry_id);

  form.querySelectorAll(".sleep-edit-scale .sleep-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      draft.score = parseInt(btn.dataset.score, 10);
      rerenderSleepEntry(row, entry);
    });
  });

  form.querySelectorAll(".chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const q = chip.dataset.qualifier;
      if (draft.qualifiers.has(q)) draft.qualifiers.delete(q);
      else draft.qualifiers.add(q);
      rerenderSleepEntry(row, entry);
    });
  });

  form.querySelector(".sleep-cancel-btn").addEventListener("click", () => {
    sleepEditDrafts.delete(entry.entry_id);
    rerenderSleepEntry(row, entry);
  });

  form.querySelector(".sleep-save-btn").addEventListener("click", async () => {
    status.textContent = "Saving...";
    try {
      await api(`/sleep/${entry.entry_id}`, {
        method: "PATCH",
        body: JSON.stringify({ score: draft.score, qualifiers: buildQualifiersPayload(draft.qualifiers) }),
      });
      sleepEditDrafts.delete(entry.entry_id);
      loadSleepHistory();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  form.querySelector(".sleep-delete-btn").addEventListener("click", async () => {
    const timeLabel = entry.start_time ? ` ${localTimeString(entry.start_time)}` : "";
    if (!confirm(`Delete the sleep entry for ${entry.sleep_date}${timeLabel}? This can't be undone.`)) return;
    status.textContent = "Deleting...";
    try {
      await api(`/sleep/${entry.entry_id}`, { method: "DELETE" });
      sleepEditDrafts.delete(entry.entry_id);
      loadSleepHistory();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });
}

async function loadSleepHistory() {
  const container = document.getElementById("sleep-history");
  container.innerHTML = '<p class="muted">Loading...</p>';
  try {
    const entries = await api("/sleep");
    if (!entries.length) {
      container.innerHTML = '<p class="muted">No sleep entries yet.</p>';
      return;
    }
    container.innerHTML = "";
    entries.forEach(entry => {
      const row = document.createElement("div");
      row.className = "list-row sleep-entry-row";
      rerenderSleepEntry(row, entry);
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

// --- Calendars tab ---
async function loadCalendars() {
  const container = document.getElementById("calendar-list");
  try {
    const cals = await api("/calendars");
    if (!cals.length) {
      container.innerHTML = '<p class="muted">No calendars added yet.</p>';
      return;
    }
    container.innerHTML = "";
    cals.forEach(cal => {
      const row = document.createElement("div");
      row.className = "calendar-row";
      const lastSynced = cal.last_synced ? `Last synced: ${cal.last_synced}` : "Never synced";
      const errorLine = cal.last_error ? `<div class="cal-error">${cal.last_error}</div>` : "";
      row.innerHTML = `
        <div class="cal-name">${escapeHtml(cal.name)} ${cal.enabled ? "" : "(disabled)"}</div>
        <div class="cal-meta">${lastSynced} · default: ${cal.default_tag}</div>
        ${errorLine}
      `;
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("cal-add-submit").addEventListener("click", async () => {
  const status = document.getElementById("cal-status");
  const name = document.getElementById("cal-name").value.trim();
  const ics_url = document.getElementById("cal-url").value.trim();
  const default_tag = document.getElementById("cal-default-tag").value.trim();

  if (!name || !ics_url || !default_tag) {
    status.textContent = "All fields required";
    return;
  }

  try {
    await api("/calendars", { method: "POST", body: JSON.stringify({ name, ics_url, default_tag }) });
    status.textContent = "Added";
    document.getElementById("cal-name").value = "";
    document.getElementById("cal-url").value = "";
    document.getElementById("cal-default-tag").value = "";
    loadCalendars();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Manage tab: keyword rules (staged draft, no hot-apply) ---
let committedRules = [];      // last-known-saved rules from the server, each may be marked _markedForDeletion
let pendingNewRules = [];     // rules added to the draft but not yet saved

function draftIsDirty() {
  return pendingNewRules.length > 0 || committedRules.some(r => r._markedForDeletion);
}

function renderRuleList() {
  const container = document.getElementById("rule-list");
  const rows = [];

  committedRules.forEach(rule => {
    const fieldNote = rule.is_regex ? `regex on ${rule.match_field}` : `contains, on ${rule.match_field}`;
    const exclusiveNote = rule.exclusive === undefined ? " · exclusivity unknown (old rule row?)" : (!rule.exclusive ? " · stacks (non-exclusive)" : "");
    const marked = rule._markedForDeletion;
    const row = document.createElement("div");
    row.className = "list-row" + (marked ? " marked-deleted" : "");
    row.innerHTML = `
      <div>
        <div class="row-title">"${escapeHtml(rule.keyword)}" → <strong>${escapeHtml(rule.tag)}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${exclusiveNote}${marked ? " · marked for deletion" : ""}</div>
      </div>
      <button class="small-btn ${marked ? "" : "danger"}">${marked ? "Undo" : "Delete"}</button>
    `;
    row.querySelector(".small-btn").addEventListener("click", () => {
      rule._markedForDeletion = !rule._markedForDeletion;
      renderRuleList();
    });
    rows.push(row);
  });

  pendingNewRules.forEach((rule, idx) => {
    const fieldNote = rule.is_regex ? `regex on ${rule.match_field}` : `contains, on ${rule.match_field}`;
    const exclusiveNote = rule.exclusive === undefined ? " · exclusivity unknown (old rule row?)" : (!rule.exclusive ? " · stacks (non-exclusive)" : "");
    const row = document.createElement("div");
    row.className = "list-row pending-new";
    row.innerHTML = `
      <div>
        <div class="row-title">"${escapeHtml(rule.keyword)}" → <strong>${escapeHtml(rule.tag)}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${exclusiveNote} · pending</div>
      </div>
      <button class="small-btn danger">Remove</button>
    `;
    row.querySelector(".small-btn").addEventListener("click", () => {
      pendingNewRules.splice(idx, 1);
      renderRuleList();
    });
    rows.push(row);
  });

  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<p class="muted">No rules yet.</p>';
  } else {
    rows.forEach(r => container.appendChild(r));
  }

  document.getElementById("rule-save-batch").disabled = !draftIsDirty();
}

async function loadKeywordRules() {
  try {
    committedRules = await api("/keyword_rules");
    committedRules.forEach(r => { r._markedForDeletion = false; });
    pendingNewRules = [];
    renderRuleList();
  } catch (e) {
    document.getElementById("rule-list").innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("rule-add-draft").addEventListener("click", () => {
  const keyword = document.getElementById("rule-keyword").value.trim();
  const tag = document.getElementById("rule-tag").value.trim();
  const category = document.getElementById("rule-category").value;
  const match_field = document.getElementById("rule-match-field").value;
  const is_regex = document.getElementById("rule-is-regex").checked;
  const priority = parseInt(document.getElementById("rule-priority").value, 10) || 0;
  const exclusive = document.getElementById("rule-exclusive").checked;
  const status = document.getElementById("rule-status");

  if (!keyword || !tag) {
    status.textContent = "Keyword and tag are required";
    return;
  }

  pendingNewRules.push({ keyword, tag, category, match_field, is_regex, priority, exclusive, enabled: true });
  document.getElementById("rule-keyword").value = "";
  document.getElementById("rule-tag").value = "";
  document.getElementById("rule-priority").value = "0";
  document.getElementById("rule-is-regex").checked = false;
  // Deliberately NOT resetting rule-exclusive here - it used to silently
  // flip back to checked after every add, which could leave a rule
  // exclusive when the person believed they'd already unchecked it for
  // this session. Leaving it as the person last set it is more
  // predictable when adding several related rules in a row.
  status.textContent = "Added to draft - not saved yet";
  renderRuleList();
});

document.getElementById("rule-save-batch").addEventListener("click", async () => {
  const status = document.getElementById("rule-status");
  const deleted_ids = committedRules.filter(r => r._markedForDeletion).map(r => r.id);

  status.textContent = "Saving...";
  try {
    const result = await api("/keyword_rules/save_batch", {
      method: "POST",
      body: JSON.stringify({ added: pendingNewRules, deleted_ids }),
    });

    status.textContent = "Rules saved";
    await loadKeywordRules(); // refresh committed state, clears draft

    if (result.affected_events > 0) {
      const sample = result.sample_titles.join(", ");
      const confirmed = confirm(
        `This rule change would reclassify ${result.affected_events} previously-synced event(s), ` +
        `e.g.: ${sample}${result.affected_events > result.sample_titles.length ? ", ..." : ""}.\n\n` +
        `Reprocess them now? This runs in the background - you can keep using the app while it works.`
      );
      if (confirmed) {
        startReprocess();
      }
    }
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Reprocess: background trigger + polling banner ---
let reprocessPollHandle = null;

async function startReprocess() {
  const banner = document.getElementById("reprocess-banner");
  banner.style.display = "block";
  banner.className = "reprocess-banner running";
  banner.textContent = "Starting reprocess...";

  try {
    await api("/reprocess", { method: "POST" });
  } catch (e) {
    banner.className = "reprocess-banner error";
    banner.textContent = `Error starting reprocess: ${e.message}`;
    return;
  }

  if (reprocessPollHandle) clearInterval(reprocessPollHandle);
  reprocessPollHandle = setInterval(pollReprocessStatus, 1500);
  pollReprocessStatus();
}

async function pollReprocessStatus() {
  const banner = document.getElementById("reprocess-banner");
  try {
    const s = await api("/reprocess/status");

    if (s.status === "running") {
      banner.style.display = "block";
      banner.className = "reprocess-banner running";
      banner.textContent = `Reprocessing... ${s.processed}/${s.total} checked, ${s.changed} changed`;
    } else if (s.status === "done") {
      banner.className = "reprocess-banner done";
      banner.textContent = `Reprocess complete - ${s.changed}/${s.total} event(s) updated`;
      clearInterval(reprocessPollHandle);
      reprocessPollHandle = null;
      setTimeout(() => { banner.style.display = "none"; }, 6000);
    } else if (s.status === "error") {
      banner.className = "reprocess-banner error";
      banner.textContent = `Reprocess failed: ${s.error}`;
      clearInterval(reprocessPollHandle);
      reprocessPollHandle = null;
    }
  } catch (e) {
    // Transient poll failure - don't kill the banner, just try again next tick
  }
}

// On load, if a reprocess was already running from a previous session
// (e.g. page refreshed mid-run), pick up polling rather than losing track of it.
async function checkReprocessOnLoad() {
  try {
    const s = await api("/reprocess/status");
    if (s.status === "running") {
      reprocessPollHandle = setInterval(pollReprocessStatus, 1500);
      pollReprocessStatus();
    }
  } catch (e) {
    // ignore
  }
}

// --- Manage tab: tag button definitions ---
async function loadTagDefManage() {
  const container = document.getElementById("tagdef-list");
  try {
    const defs = await api("/tag_definitions");
    if (!defs.length) {
      container.innerHTML = '<p class="muted">No tag buttons yet.</p>';
      return;
    }
    container.innerHTML = "";
    defs.forEach(def => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `
        <div>
          <div class="row-title">${escapeHtml(def.label)} <span class="muted">(${escapeHtml(def.tag)})</span></div>
          <div class="row-meta">${def.category}${def.is_duration ? " · duration" : ""} · order ${def.sort_order}</div>
        </div>
        <button class="small-btn danger" data-id="${def.id}">Delete</button>
      `;
      row.querySelector(".small-btn").addEventListener("click", async () => {
        await api(`/tag_definitions/${def.id}`, { method: "DELETE" });
        loadTagDefManage();
        loadTagButtons(); // keep the Tags tab in sync
      });
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("tagdef-add-submit").addEventListener("click", async () => {
  const status = document.getElementById("tagdef-status");
  const tag = document.getElementById("tagdef-tag").value.trim();
  const label = document.getElementById("tagdef-label").value.trim();
  const category = document.getElementById("tagdef-category").value;
  const is_duration = document.getElementById("tagdef-is-duration").checked;
  const sort_order = parseInt(document.getElementById("tagdef-sort-order").value, 10) || 0;

  if (!tag || !label) {
    status.textContent = "Tag and label are required";
    return;
  }

  try {
    await api("/tag_definitions", {
      method: "POST",
      body: JSON.stringify({ tag, label, category, is_duration, sort_order }),
    });
    status.textContent = "Tag button added";
    document.getElementById("tagdef-tag").value = "";
    document.getElementById("tagdef-label").value = "";
    document.getElementById("tagdef-sort-order").value = "0";
    document.getElementById("tagdef-is-duration").checked = false;
    loadTagDefManage();
    loadTagButtons(); // keep the Tags tab in sync
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// --- Timeline tab ---
function formatDateInput(d) {
  return d.toISOString().slice(0, 10); // YYYY-MM-DD
}

function defaultTimelineRange() {
  const now = new Date();
  const start = new Date(now);
  start.setDate(start.getDate() - 7);
  const end = new Date(now);
  end.setDate(end.getDate() + 1);
  return { start: formatDateInput(start), end: formatDateInput(end) };
}

function localDateString(isoTimestamp) {
  // Local calendar date, not UTC's - a timestamp shortly after local
  // midnight (any timezone ahead of UTC) can otherwise group under
  // the previous day's heading, since UTC's date for that instant is
  // still yesterday even though it's already tomorrow locally. Uses
  // the browser's own local timezone automatically (no TZ config
  // needed here, unlike the backend - the browser already knows).
  const d = new Date(isoTimestamp);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function groupByDay(entries) {
  const groups = {};
  entries.forEach(entry => {
    const day = localDateString(entry.timestamp);
    if (!groups[day]) groups[day] = [];
    groups[day].push(entry);
  });
  return groups;
}

function renderTagChips(tags) {
  if (!tags || !tags.length) return "";
  return `<div class="timeline-tags">${tags.map(t => `<span class="chip-small">${escapeHtml(t)}</span>`).join("")}</div>`;
}

// --- Timeline edit state ---
// event_id -> Set of tags currently being edited in that entry's open editor.
// Entries not in this map are shown read-only (the default).
const timelineEditDrafts = new Map();

function isoToDatetimeLocalValue(iso) {
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function datetimeLocalValueToIso(value) {
  const [datePart, timePart] = value.split("T");
  const [y, m, d] = datePart.split("-").map(Number);
  const [hh, mm] = timePart.split(":").map(Number);
  return new Date(y, m - 1, d, hh, mm).toISOString();
}

function renderTimelineEditForm(entry) {
  const draft = timelineEditDrafts.get(entry.event_id);
  const tags = Array.from(draft);
  const chipsHtml = tags.map(t => `
    <span class="chip-small editable-chip">
      ${t} <button class="chip-remove" data-remove-tag="${t}">&times;</button>
    </span>
  `).join("");

  const timeFieldHtml = entry.kind === "manual"
    ? `<label class="timeline-edit-label">Time
         <input type="datetime-local" class="timeline-edit-datetime" value="${isoToDatetimeLocalValue(entry.timestamp)}">
       </label>
       <label class="timeline-edit-label">Duration (minutes)
         <input type="number" min="0" class="timeline-edit-duration" value="${entry.duration_min ?? ""}" placeholder="none">
       </label>`
    : `<p class="muted small-note">Time and title come from the calendar feed and can't be edited here - only tags.</p>`;

  const deleteBtnHtml = entry.kind === "manual"
    ? `<button class="small-btn danger timeline-delete-btn">Delete event</button>`
    : "";

  const resetBtnHtml = (entry.kind === "calendar" && entry.manually_tagged)
    ? `<button class="small-btn timeline-reset-btn">Reset to auto</button>`
    : "";

  return `
    <div class="timeline-edit-form" data-event-id="${entry.event_id}" data-kind="${entry.kind}">
      <div class="timeline-edit-tags">${chipsHtml || '<span class="muted">No tags - add one below</span>'}</div>
      <div class="timeline-edit-add-tag">
        <input type="text" class="timeline-new-tag-input" placeholder="Add tag...">
        <button class="small-btn timeline-add-tag-btn">Add</button>
      </div>
      ${timeFieldHtml}
      <div class="timeline-edit-actions">
        <button class="small-btn timeline-save-btn">Save</button>
        <button class="small-btn timeline-cancel-btn">Cancel</button>
        ${deleteBtnHtml}
        ${resetBtnHtml}
      </div>
      <p class="timeline-edit-status status"></p>
    </div>
  `;
}

function wireTimelineEditForm(row, entry) {
  const form = row.querySelector(".timeline-edit-form");
  const status = form.querySelector(".timeline-edit-status");

  form.querySelectorAll("[data-remove-tag]").forEach(btn => {
    btn.addEventListener("click", () => {
      timelineEditDrafts.get(entry.event_id).delete(btn.dataset.removeTag);
      rerenderTimelineEntry(row, entry);
    });
  });

  form.querySelector(".timeline-add-tag-btn").addEventListener("click", () => {
    const input = form.querySelector(".timeline-new-tag-input");
    const tag = input.value.trim();
    if (!tag) return;
    timelineEditDrafts.get(entry.event_id).add(tag);
    rerenderTimelineEntry(row, entry);
  });

  form.querySelector(".timeline-cancel-btn").addEventListener("click", () => {
    timelineEditDrafts.delete(entry.event_id);
    rerenderTimelineEntry(row, entry);
  });

  form.querySelector(".timeline-save-btn").addEventListener("click", async () => {
    const draftTags = Array.from(timelineEditDrafts.get(entry.event_id));
    if (!draftTags.length) {
      status.textContent = "At least one tag is required";
      return;
    }
    status.textContent = "Saving...";
    try {
      if (entry.kind === "manual") {
        const timeInput = form.querySelector(".timeline-edit-datetime");
        const durationInput = form.querySelector(".timeline-edit-duration");
        const body = { tags: draftTags };
        const newIso = datetimeLocalValueToIso(timeInput.value);
        if (newIso !== entry.timestamp) body.timestamp = newIso;
        const newDuration = durationInput.value === "" ? null : parseInt(durationInput.value, 10);
        if (newDuration !== null && newDuration !== entry.duration_min) body.duration_min = newDuration;
        await api(`/events/${entry.event_id}`, { method: "PATCH", body: JSON.stringify(body) });
      } else {
        await api(`/calendar_events/${entry.event_id}/tags`, {
          method: "PATCH",
          body: JSON.stringify({ tags: draftTags }),
        });
      }
      timelineEditDrafts.delete(entry.event_id);
      loadTimeline(); // full reload - simplest way to reflect the save, including any day-grouping change from a time edit
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  const deleteBtn = form.querySelector(".timeline-delete-btn");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!confirm("Delete this logged event? This can't be undone.")) return;
      status.textContent = "Deleting...";
      try {
        await api(`/events/${entry.event_id}`, { method: "DELETE" });
        timelineEditDrafts.delete(entry.event_id);
        loadTimeline();
      } catch (e) {
        status.textContent = `Error: ${e.message}`;
      }
    });
  }

  const resetBtn = form.querySelector(".timeline-reset-btn");
  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      if (!confirm("Reset this event's tags to automatic keyword-rule classification? Your manual edit will be discarded.")) return;
      status.textContent = "Resetting...";
      try {
        await api(`/calendar_events/${entry.event_id}/reset_tags`, { method: "POST" });
        timelineEditDrafts.delete(entry.event_id);
        loadTimeline();
      } catch (e) {
        status.textContent = `Error: ${e.message}`;
      }
    });
  }
}

function renderTimelineEntryReadonly(entry) {
  const time = entry.timestamp.slice(11, 16);
  const durationNote = entry.duration_min ? ` · ${entry.duration_min}min` : "";
  const overrideBadge = entry.manually_tagged ? ' <span class="chip-small">manually tagged</span>' : "";

  if (entry.kind === "calendar") {
    return `
      <div class="timeline-entry-main">
        <span class="timeline-time">${time}</span>
        <span class="timeline-kind-badge cal">Calendar</span>
        <span class="timeline-title">${escapeHtml(entry.title) || "(untitled event)"}</span>
        <span class="muted timeline-cal-name">${escapeHtml(entry.calendar) || ""}${durationNote}</span>
        <button class="small-btn timeline-edit-btn">Edit tags</button>
      </div>
      ${renderTagChips(entry.tags)}${overrideBadge}
    `;
  }
  return `
    <div class="timeline-entry-main">
      <span class="timeline-time">${time}</span>
      <span class="timeline-kind-badge manual">Logged</span>
      <span class="muted">${durationNote}</span>
      <button class="small-btn timeline-edit-btn">Edit</button>
    </div>
    ${renderTagChips(entry.tags)}
  `;
}

function rerenderTimelineEntry(row, entry) {
  if (timelineEditDrafts.has(entry.event_id)) {
    row.innerHTML = renderTimelineEditForm(entry);
    wireTimelineEditForm(row, entry);
  } else {
    row.innerHTML = renderTimelineEntryReadonly(entry);
    row.querySelector(".timeline-edit-btn").addEventListener("click", () => {
      timelineEditDrafts.set(entry.event_id, new Set(entry.tags));
      rerenderTimelineEntry(row, entry);
    });
  }
}

async function loadTimeline() {
  const container = document.getElementById("timeline-list");
  const start = document.getElementById("timeline-start").value;
  const end = document.getElementById("timeline-end").value;

  container.innerHTML = '<p class="muted">Loading...</p>';
  try {
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    const entries = await api(`/timeline?${params.toString()}`);

    if (!entries.length) {
      container.innerHTML = '<p class="muted">Nothing in this range yet.</p>';
      return;
    }

    const groups = groupByDay(entries);
    const days = Object.keys(groups).sort().reverse(); // most recent day first

    container.innerHTML = "";
    days.forEach(day => {
      const heading = document.createElement("div");
      heading.className = "timeline-day-heading";
      heading.textContent = day;
      container.appendChild(heading);

      groups[day].forEach(entry => {
        const row = document.createElement("div");
        row.className = `timeline-entry timeline-entry-${entry.kind}`;
        rerenderTimelineEntry(row, entry);
        container.appendChild(row);
      });
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

function initTimelineControls() {
  const defaults = defaultTimelineRange();
  document.getElementById("timeline-start").value = defaults.start;
  document.getElementById("timeline-end").value = defaults.end;
  document.getElementById("timeline-refresh").addEventListener("click", loadTimeline);
}

// --- init ---
async function initApp() {
  try {
    const me = await api("/auth/me");
    showApp(me);
  } catch (e) {
    showLogin();
  }
}

function showLogin() {
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("app-shell").style.display = "none";
}

function showApp(me) {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app-shell").style.display = "block";
  document.getElementById("current-username").textContent = me.username;

  loadTagButtons();
  loadCalendars();
  loadKeywordRules();
  loadTagDefManage();
  loadUserList();
  loadClaimPicker();
  checkReprocessOnLoad();
  initTimelineControls();
  loadTimeline();
  loadSleepHistory();
}

document.getElementById("login-submit").addEventListener("click", async () => {
  const status = document.getElementById("login-status");
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;

  if (!username || !password) {
    status.textContent = "Username and password required";
    return;
  }

  status.textContent = "Logging in...";
  try {
    const me = await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    document.getElementById("login-password").value = "";
    showApp(me);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// Allow pressing Enter in either login field to submit
["login-username", "login-password"].forEach(id => {
  document.getElementById(id).addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("login-submit").click();
  });
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch (e) {
    // even if the request fails, drop back to the login screen -
    // an invalid/expired session should look the same either way
  }
  showLogin();
});

// --- Household members ---
async function loadUserList() {
  const container = document.getElementById("user-list");
  try {
    const users = await api("/users");
    if (!users.length) {
      container.innerHTML = '<p class="muted">No accounts yet.</p>';
      return;
    }
    container.innerHTML = "";
    users.forEach(u => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `<div class="row-title">${escapeHtml(u.username)}</div>`;
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

const MANUAL_OPTION_VALUE = "__manual__";

async function loadClaimPicker() {
  const hint = document.getElementById("claim-hint");
  const select = document.getElementById("new-user-username-select");
  const manualInput = document.getElementById("new-user-username-manual");

  try {
    const unclaimed = await api("/unclaimed_ring_users");

    if (unclaimed.length === 0) {
      // No ring data to claim yet - manual entry is the only path, with
      // a clear warning rather than silently accepting a mismatch.
      hint.textContent = "No unclaimed ring data found yet - you can still add this person, " +
        "but double-check the username matches their ring parser's GADGETBRIDGE_USER once it's set up.";
      hint.className = "muted small-note warning-note";
      select.style.display = "none";
      manualInput.style.display = "block";
      manualInput.placeholder = "Username (must match their GADGETBRIDGE_USER)";
      return;
    }

    hint.textContent = "Pick from ring data that's already synced, to avoid typos that would break correlation.";
    hint.className = "muted small-note";

    select.innerHTML = "";
    unclaimed.forEach(username => {
      const opt = document.createElement("option");
      opt.value = username;
      opt.textContent = username;
      select.appendChild(opt);
    });
    const manualOpt = document.createElement("option");
    manualOpt.value = MANUAL_OPTION_VALUE;
    manualOpt.textContent = "— enter manually instead —";
    select.appendChild(manualOpt);

    select.style.display = "block";
    manualInput.style.display = "none";
    manualInput.value = "";

    select.onchange = () => {
      if (select.value === MANUAL_OPTION_VALUE) {
        manualInput.style.display = "block";
        manualInput.placeholder = "Username (must match their GADGETBRIDGE_USER)";
      } else {
        manualInput.style.display = "none";
      }
    };
  } catch (e) {
    // Influx unreachable or some other failure - don't block account
    // creation over it, just fall back to manual entry.
    hint.textContent = "Couldn't check ring data - you can still add this person manually.";
    hint.className = "muted small-note warning-note";
    select.style.display = "none";
    manualInput.style.display = "block";
  }
}

document.getElementById("new-user-submit").addEventListener("click", async () => {
  const status = document.getElementById("user-status");
  const select = document.getElementById("new-user-username-select");
  const manualInput = document.getElementById("new-user-username-manual");
  const password = document.getElementById("new-user-password").value;

  let username;
  if (select.style.display !== "none" && select.value !== MANUAL_OPTION_VALUE) {
    username = select.value;
  } else {
    username = manualInput.value.trim();
  }

  if (!username || !password) {
    status.textContent = "Username and password required";
    return;
  }

  try {
    const result = await api("/users", { method: "POST", body: JSON.stringify({ username, password }) });
    status.textContent = result.linked_to_ring_data
      ? `Added ${username} - linked to existing ring data`
      : `Added ${username} - no matching ring data found yet, double-check the username later`;
    manualInput.value = "";
    document.getElementById("new-user-password").value = "";
    loadUserList();
    loadClaimPicker(); // refresh so the just-claimed username drops off the list
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

initApp();