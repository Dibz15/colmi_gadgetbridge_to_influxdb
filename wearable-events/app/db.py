import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from app.config import SQLITE_PATH

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


def _ensure_column(conn, table: str, column: str, coltype_and_default: str):
    ''' Adds a column to an existing table if it's missing, via
    ALTER TABLE. Exists because `CREATE TABLE IF NOT EXISTS` in
    schema.sql is a no-op against a database that already has the table
    from before that column was added - it does NOT retroactively add
    new columns to existing installs. This is the lightweight migration
    mechanism for this app (deliberately not a full framework like
    Alembic - overkill for a handful of personal-scale tables). Add a
    new _ensure_column() call here, alongside the schema.sql change,
    any time a column is added to an existing table in the future.
    '''
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype_and_default}")
        logger.info(f"Migrated existing database: added column {table}.{column}")


def init_db():
    ''' Create config.db (and its parent dir) if it doesn't exist yet,
    apply schema.sql, and run any pending lightweight migrations for
    existing installs. Safe to call on every startup - schema.sql uses
    IF NOT EXISTS / INSERT OR IGNORE, and _ensure_column calls are
    similarly idempotent (no-op once the column already exists).
    '''
    db_path = Path(SQLITE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        conn.executescript(_SCHEMA_PATH.read_text())
        _ensure_column(conn, "calendar_events_cache", "manually_tagged", "INTEGER NOT NULL DEFAULT 0")
    logger.info(f"Initialized config db at {SQLITE_PATH}")


@contextmanager
def get_conn():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- users ---

def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def create_user(username: str, password_hash: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash)
        )
        return cur.lastrowid


def list_users() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()]


# --- sessions ---

def create_session(token: str, user_id: int):
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))


def get_session_with_user(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT users.id, users.username, users.created_at
               FROM sessions JOIN users ON sessions.user_id = users.id
               WHERE sessions.token = ?""",
            (token,)
        ).fetchone()
        return dict(row) if row else None


def touch_session(token: str):
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = datetime('now') WHERE token = ?", (token,))


def delete_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --- calendars (all scoped by user_id) ---

def get_calendar(calendar_id: int, user_id: int | None = None):
    ''' user_id filter is optional here because the background sync job
    (sync_all_calendars) needs to look up a calendar's owner without
    already knowing it - everywhere else, pass user_id to enforce that
    people can't reference each other's calendar rows by guessing ids.
    '''
    query = "SELECT * FROM calendars WHERE id = ?"
    params = [calendar_id]
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    with get_conn() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None


def list_calendars(user_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM calendars WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()]


def list_enabled_calendars_all_users():
    ''' Used by the background sync job, which runs independently of any
    session and must sync every household member's enabled calendars in
    one pass. Joins in username so the sync job knows which Influx
    `user` tag to write each calendar's events under.
    '''
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT calendars.*, users.username AS owner_username
               FROM calendars JOIN users ON calendars.user_id = users.id
               WHERE calendars.enabled = 1"""
        ).fetchall()]


def add_calendar(user_id: int, name: str, ics_url: str, default_tag: str):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO calendars (user_id, name, ics_url, default_tag) VALUES (?, ?, ?, ?)",
            (user_id, name, ics_url, default_tag)
        )
        return cur.lastrowid


def update_calendar(calendar_id: int, user_id: int, **fields):
    if not fields:
        return
    allowed = {"name", "ics_url", "default_tag", "enabled"}
    set_fields = {k: v for k, v in fields.items() if k in allowed}
    if not set_fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in set_fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE calendars SET {set_clause} WHERE id = ? AND user_id = ?",
            (*set_fields.values(), calendar_id, user_id)
        )


def set_calendar_sync_result(calendar_id: int, *, success: bool, error: str | None = None):
    with get_conn() as conn:
        if success:
            conn.execute(
                "UPDATE calendars SET last_synced = datetime('now'), last_error = NULL WHERE id = ?",
                (calendar_id,)
            )
        else:
            conn.execute(
                "UPDATE calendars SET last_error = ? WHERE id = ?",
                (error, calendar_id)
            )


def delete_calendar(calendar_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM calendars WHERE id = ? AND user_id = ?", (calendar_id, user_id))


# --- keyword_rules (all scoped by user_id) ---

def list_keyword_rules(user_id: int, enabled_only: bool = False):
    query = "SELECT * FROM keyword_rules WHERE user_id = ?"
    params = [user_id]
    if enabled_only:
        query += " AND enabled = 1"
    query += " ORDER BY category, priority"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def add_keyword_rule(user_id: int, keyword: str, tag: str, category: str, *,
                      is_regex: bool = False, match_field: str = "title",
                      priority: int = 0, enabled: bool = True):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO keyword_rules
               (user_id, keyword, is_regex, match_field, tag, category, priority, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, keyword, int(is_regex), match_field, tag, category, priority, int(enabled))
        )
        return cur.lastrowid


def delete_keyword_rule(rule_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM keyword_rules WHERE id = ? AND user_id = ?", (rule_id, user_id))


# --- tag_definitions (all scoped by user_id) ---

def add_tag_definition(user_id: int, tag: str, label: str, category: str, *,
                        is_duration: bool = False, sort_order: int = 0):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tag_definitions (user_id, tag, label, category, is_duration, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, tag, label, category, int(is_duration), sort_order)
        )
        return cur.lastrowid


def delete_tag_definition(tag_def_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM tag_definitions WHERE id = ? AND user_id = ?", (tag_def_id, user_id))


def list_tag_definitions(user_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tag_definitions WHERE user_id = ? ORDER BY sort_order", (user_id,)
        ).fetchall()]


# --- calendar_events_cache (all scoped by user_id) ---

def upsert_cached_event(event_id: str, user_id: int, calendar_id: int, title: str, description: str,
                         start_iso: str, duration_min: int | None, applied_tags: list[str]):
    ''' Called on every calendar sync. Deliberately preserves applied_tags
    (and the manually_tagged flag) for events a person has manually
    retagged via the Timeline UI - everything else (title, description,
    timing) still updates normally, only the tags are protected. Without
    this, a manual fix would get silently reverted on the very next
    15-minute sync.
    '''
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO calendar_events_cache
               (event_id, user_id, calendar_id, title, description, start_iso, duration_min, applied_tags, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(event_id) DO UPDATE SET
                 user_id = excluded.user_id,
                 calendar_id = excluded.calendar_id,
                 title = excluded.title,
                 description = excluded.description,
                 start_iso = excluded.start_iso,
                 duration_min = excluded.duration_min,
                 applied_tags = CASE
                   WHEN calendar_events_cache.manually_tagged = 1 THEN calendar_events_cache.applied_tags
                   ELSE excluded.applied_tags
                 END,
                 updated_at = datetime('now')""",
            (event_id, user_id, calendar_id, title, description, start_iso, duration_min, json.dumps(applied_tags))
        )


def get_cached_event(event_id: str, user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM calendar_events_cache WHERE event_id = ? AND user_id = ?",
            (event_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def list_cached_events(user_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM calendar_events_cache WHERE user_id = ?", (user_id,)
        ).fetchall()]


def set_cached_event_tags(event_id: str, tags: list[str], *, manually_tagged: bool):
    ''' Used both by the manual tag-override endpoint (manually_tagged=True)
    and by the background sync/reprocess jobs writing auto-classified
    tags for a not-yet-overridden event (manually_tagged=False).
    '''
    with get_conn() as conn:
        conn.execute(
            "UPDATE calendar_events_cache SET applied_tags = ?, manually_tagged = ?, updated_at = datetime('now') WHERE event_id = ?",
            (json.dumps(tags), int(manually_tagged), event_id)
        )