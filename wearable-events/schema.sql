-- config.db schema
-- Relational config data only (users, calendars, rules, tag button
-- definitions, sessions). Time-series sensor/event/sleep data lives in
-- InfluxDB, not here.
--
-- Multi-user: calendars, keyword_rules, tag_definitions, and
-- calendar_events_cache are all scoped by user_id. Each household
-- member gets their own calendars/rules/tags after logging in - nothing
-- is shared across users by default.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Should match the GADGETBRIDGE_USER value used for this person's
    -- ring-parser instance, so their calendar/sleep-score data and their
    -- HRV/HR/temperature data share the same `user` tag in InfluxDB and
    -- can be correlated later. Not enforced (this service has no
    -- knowledge of the parser's config) - just a convention worth
    -- following when creating accounts.
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token         TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS calendars (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    ics_url       TEXT NOT NULL,
    default_tag   TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_synced   TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, ics_url)
);

CREATE TABLE IF NOT EXISTS keyword_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL,
    is_regex    INTEGER NOT NULL DEFAULT 0,
    match_field TEXT NOT NULL DEFAULT 'title',
    tag         TEXT NOT NULL,
    category    TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tag_definitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    label       TEXT NOT NULL,
    category    TEXT NOT NULL,
    is_duration INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_keyword_rules_user_category ON keyword_rules(user_id, category, enabled);
CREATE INDEX IF NOT EXISTS idx_calendars_user_enabled ON calendars(user_id, enabled);
CREATE INDEX IF NOT EXISTS idx_tag_definitions_user ON tag_definitions(user_id);

-- Local cache of raw calendar event data (title/description/start/etc)
-- as last seen during a sync, keyed by the same event_id written to
-- Influx. This exists so keyword rule changes can be reprocessed against
-- everything we've ever synced, without depending on the ICS feed still
-- exposing those events (most feeds are a rolling window, not a full
-- archive). applied_tags tracks what's currently written to Influx for
-- this event, so a reprocess pass knows exactly which old tag-points
-- need deleting vs which are unchanged.
CREATE TABLE IF NOT EXISTS calendar_events_cache (
    event_id      TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    calendar_id   INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    start_iso     TEXT NOT NULL,
    duration_min  INTEGER,
    applied_tags  TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cache_user_calendar ON calendar_events_cache(user_id, calendar_id);
