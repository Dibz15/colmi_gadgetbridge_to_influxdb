// --- tab switching ---
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

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
      btn.addEventListener("click", () => logTag(def.tag, btn));
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

document.getElementById("custom-tag-submit").addEventListener("click", () => {
  const input = document.getElementById("custom-tag-input");
  const tag = input.value.trim();
  if (!tag) return;
  logTag(tag, null);
  input.value = "";
});

// --- Sleep tab ---
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

document.querySelectorAll(".chip").forEach(chip => {
  chip.addEventListener("click", () => {
    const q = chip.dataset.qualifier;
    if (selectedQualifiers.has(q)) {
      selectedQualifiers.delete(q);
      chip.classList.remove("selected");
    } else {
      selectedQualifiers.add(q);
      chip.classList.add("selected");
    }
  });
});

document.getElementById("sleep-submit").addEventListener("click", async () => {
  const status = document.getElementById("sleep-status");
  if (selectedScore === null) return;

  const qualifiers = {};
  selectedQualifiers.forEach(q => { qualifiers[q] = true; });

  status.textContent = "Submitting...";
  try {
    const result = await api("/sleep", {
      method: "POST",
      body: JSON.stringify({ score: selectedScore, qualifiers }),
    });
    status.textContent = `Logged for ${result.sleep_date}`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

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
        <div class="cal-name">${cal.name} ${cal.enabled ? "" : "(disabled)"}</div>
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
    const marked = rule._markedForDeletion;
    const row = document.createElement("div");
    row.className = "list-row" + (marked ? " marked-deleted" : "");
    row.innerHTML = `
      <div>
        <div class="row-title">"${rule.keyword}" → <strong>${rule.tag}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote}${marked ? " · marked for deletion" : ""}</div>
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
    const row = document.createElement("div");
    row.className = "list-row pending-new";
    row.innerHTML = `
      <div>
        <div class="row-title">"${rule.keyword}" → <strong>${rule.tag}</strong></div>
        <div class="row-meta">${rule.category} · priority ${rule.priority} · ${fieldNote} · pending</div>
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
  const status = document.getElementById("rule-status");

  if (!keyword || !tag) {
    status.textContent = "Keyword and tag are required";
    return;
  }

  pendingNewRules.push({ keyword, tag, category, match_field, is_regex, priority, enabled: true });
  document.getElementById("rule-keyword").value = "";
  document.getElementById("rule-tag").value = "";
  document.getElementById("rule-priority").value = "0";
  document.getElementById("rule-is-regex").checked = false;
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
          <div class="row-title">${def.label} <span class="muted">(${def.tag})</span></div>
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

function groupByDay(entries) {
  const groups = {};
  entries.forEach(entry => {
    const day = entry.timestamp.slice(0, 10); // YYYY-MM-DD from ISO timestamp
    if (!groups[day]) groups[day] = [];
    groups[day].push(entry);
  });
  return groups;
}

function renderTagChips(tags) {
  if (!tags || !tags.length) return "";
  return `<div class="timeline-tags">${tags.map(t => `<span class="chip-small">${t}</span>`).join("")}</div>`;
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
        const time = entry.timestamp.slice(11, 16); // HH:MM

        const durationNote = entry.duration_min ? ` · ${entry.duration_min}min` : "";

        if (entry.kind === "calendar") {
          row.innerHTML = `
            <div class="timeline-entry-main">
              <span class="timeline-time">${time}</span>
              <span class="timeline-kind-badge cal">Calendar</span>
              <span class="timeline-title">${entry.title || "(untitled event)"}</span>
              <span class="muted timeline-cal-name">${entry.calendar || ""}${durationNote}</span>
            </div>
            ${renderTagChips(entry.tags)}
          `;
        } else {
          row.innerHTML = `
            <div class="timeline-entry-main">
              <span class="timeline-time">${time}</span>
              <span class="timeline-kind-badge manual">Logged</span>
              <span class="muted">${durationNote}</span>
            </div>
            ${renderTagChips(entry.tags)}
          `;
        }
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
      row.innerHTML = `<div class="row-title">${u.username}</div>`;
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