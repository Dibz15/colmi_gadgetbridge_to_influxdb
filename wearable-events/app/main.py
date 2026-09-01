import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from app import auth, db, reprocess
from app.auth import SESSION_COOKIE_NAME, get_current_user
from app.config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    PORT,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_DAYS,
    SYNC_INTERVAL_MINUTES,
)
from app.ics_sync import classify_event, sync_all_calendars
from app.influx import (
    delete_event_tag_point,
    delete_sleep_entry,
    find_last_completed_sleep_session,
    find_manual_event_by_id,
    find_manual_events_in_range,
    find_sleep_entries_in_range,
    find_sleep_entry_by_id,
    list_distinct_sensor_users,
    manual_event_id,
    write_event_points,
    write_sleep_point,
)
from app.reprocess import compute_reclassification_diff

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    auth.bootstrap_admin_if_configured(ADMIN_USERNAME, ADMIN_PASSWORD)
    scheduler.add_job(
        sync_all_calendars,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        id="calendar_sync",
        next_run_time=datetime.now(),  # run once immediately on startup
    )
    scheduler.start()
    logger.info(f"Calendar sync scheduled every {SYNC_INTERVAL_MINUTES} minutes")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Wearable Events", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    ''' Forces the browser to always revalidate static assets (HTML/JS/
    CSS) rather than heuristically caching them. FastAPI's StaticFiles
    mount sends Last-Modified/ETag but no explicit Cache-Control, and
    browsers can decide to skip revalidation entirely for a while -
    meaning a shipped frontend fix can silently not take effect in an
    already-open browser tab, with no visible sign anything is wrong.
    This app is small and self-hosted, so trading away browser caching
    for "you always get what's actually on disk" is the right default.
    '''
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store"
    return response


# --- request/response models ---

class ManualEventIn(BaseModel):
    tags: list[str]
    duration_min: int | None = None


class ManualEventUpdateIn(BaseModel):
    ''' Partial update - only fields actually provided are changed.
    tags, if given, is a full replacement set (not a diff).
    '''
    tags: list[str] | None = None
    timestamp: str | None = None  # ISO 8601
    duration_min: int | None = None


class CalendarEventTagsIn(BaseModel):
    tags: list[str]  # full replacement set


class SleepIn(BaseModel):
    score: int  # 1-5
    qualifiers: dict[str, bool] = {}


class SleepUpdateIn(BaseModel):
    score: int  # 1-5
    qualifiers: dict[str, bool] = {}


class CalendarIn(BaseModel):
    name: str
    ics_url: str
    default_tag: str


class CalendarUpdateIn(BaseModel):
    name: str | None = None
    ics_url: str | None = None
    default_tag: str | None = None
    enabled: bool | None = None


class KeywordRuleIn(BaseModel):
    keyword: str
    tag: str
    category: str  # 'context' | 'meta' | 'substance' | 'restful'
    is_regex: bool = False
    match_field: str = "title"  # 'title' | 'description'
    priority: int = 0
    enabled: bool = True
    exclusive: bool = True  # False = "stack" this tag rather than compete for its category's one slot


class TagDefinitionIn(BaseModel):
    tag: str
    label: str
    category: str  # 'context' | 'substance' | 'restful' | 'meta'
    is_duration: bool = False
    sort_order: int = 0


class KeywordRuleBatchIn(BaseModel):
    ''' Staged batch of rule changes from the Manage tab - nothing hits
    the database until this is posted.
    '''
    added: list[KeywordRuleIn] = []
    deleted_ids: list[int] = []


class LoginIn(BaseModel):
    username: str
    password: str


class CreateUserIn(BaseModel):
    username: str
    password: str


# --- auth ---

@app.post("/auth/login")
def login(payload: LoginIn, response: Response):
    user = db.get_user_by_username(payload.username)
    if user is None or not auth.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(401, "invalid username or password")

    token = auth.create_session(user["id"])
    response.set_cookie(
        SESSION_COOKIE_NAME, token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_MAX_AGE_DAYS * 86400,
    )
    return {"id": user["id"], "username": user["username"]}


@app.post("/auth/logout")
def logout(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth.delete_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@app.get("/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return {"id": current_user["id"], "username": current_user["username"]}


@app.get("/users")
def get_users(current_user: dict = Depends(get_current_user)):
    ''' Any logged-in household member can see who else has an account
    (usernames only) - no roles/permissions distinction, this is a
    personal/family app, not a multi-tenant SaaS product.
    '''
    return db.list_users()


@app.get("/unclaimed_ring_users")
def get_unclaimed_ring_users(current_user: dict = Depends(get_current_user)):
    ''' Distinct `user` tag values seen in the ring parser's sensor data
    that don't already belong to an account here. Used by the "Add
    household member" form to offer picking an existing ring identity
    instead of free-typing a username that has to be manually kept in
    sync with GADGETBRIDGE_USER.

    An empty list is a normal, expected response (e.g. before any ring
    has synced yet) - the frontend falls back to manual entry, it's not
    treated as an error.
    '''
    sensor_users = set(list_distinct_sensor_users())
    claimed = {u["username"] for u in db.list_users()}
    return sorted(sensor_users - claimed)


@app.post("/users")
def post_user(payload: CreateUserIn, current_user: dict = Depends(get_current_user)):
    ''' Adds another household member. Requires being logged in as
    someone already, since there's no public signup page - this is the
    intended way to add a second/third person after the initial
    ADMIN_USERNAME/ADMIN_PASSWORD bootstrap account exists.

    Returns whether the chosen username matched existing ring sensor
    data at creation time, so the UI can confirm the link worked (or
    warn that it didn't, if someone typed a username manually instead
    of picking from the unclaimed list).
    '''
    if db.get_user_by_username(payload.username) is not None:
        raise HTTPException(400, "username already exists")
    user_id = auth.create_user(payload.username, payload.password)
    linked = payload.username in set(list_distinct_sensor_users())
    return {"id": user_id, "username": payload.username, "linked_to_ring_data": linked}


# --- events ---

@app.post("/events")
def post_event(payload: ManualEventIn, current_user: dict = Depends(get_current_user)):
    if not payload.tags:
        raise HTTPException(400, "at least one tag is required")

    event_id = manual_event_id()
    write_event_points(
        user=current_user["username"],
        tags=payload.tags,
        source="manual",
        timestamp=datetime.now(timezone.utc),
        event_id=event_id,
        duration_min=payload.duration_min,
    )
    return {"event_id": event_id, "tags": payload.tags}


@app.patch("/events/{event_id}")
def patch_event(event_id: str, payload: ManualEventUpdateIn, current_user: dict = Depends(get_current_user)):
    ''' Edit a manual event's tags, timestamp, and/or duration. At least
    one field must be provided. Unset fields keep their current value.

    duration_min is how the Tags tab's start/stop timer for
    is_duration-flagged buttons gets its final value: the tap that
    starts the timer POSTs the event with no duration, and the tap that
    stops it PATCHes duration_min in here once elapsed time is known.
    It's also editable directly, as a manual correction/safety net for
    a timer that got orphaned (e.g. the tab was closed mid-timer).
    '''
    if payload.tags is None and payload.timestamp is None and payload.duration_min is None:
        raise HTTPException(400, "provide at least one of: tags, timestamp, duration_min")

    username = current_user["username"]
    existing = find_manual_event_by_id(username, event_id)
    if existing is None:
        raise HTTPException(404, "manual event not found")

    old_tags = set(existing["tags"])
    old_timestamp = existing["timestamp"]

    new_tags = set(payload.tags) if payload.tags is not None else old_tags
    new_duration_min = payload.duration_min if payload.duration_min is not None else existing["duration_min"]

    if payload.timestamp is not None:
        try:
            new_timestamp = datetime.fromisoformat(payload.timestamp)
            if new_timestamp.tzinfo is None:
                new_timestamp = new_timestamp.replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise HTTPException(400, f"invalid timestamp: {e}") from e
    else:
        new_timestamp = old_timestamp

    if not new_tags:
        raise HTTPException(400, "at least one tag is required")

    timestamp_changed = new_timestamp != old_timestamp

    # If the timestamp is moving, every old point (all old tags) needs
    # deleting from the old timestamp - a partial tag diff doesn't make
    # sense once the point in time itself has changed. If the timestamp
    # is unchanged, only delete the tags actually being removed. Note a
    # duration_min-only change (same tags, same timestamp) still needs
    # a full rewrite below since duration_min lives on every point for
    # this event_id - but nothing needs deleting first in that case.
    tags_to_delete = old_tags if timestamp_changed else (old_tags - new_tags)
    for tag in tags_to_delete:
        try:
            delete_event_tag_point(tag=tag, source="manual", timestamp=old_timestamp)
        except Exception as e:
            logger.warning(f"Failed to delete stale point for manual event {event_id} (tag={tag}): {e}")

    write_event_points(
        user=username,
        tags=sorted(new_tags),
        source="manual",
        timestamp=new_timestamp,
        event_id=event_id,
        duration_min=new_duration_min,
    )

    return {
        "event_id": event_id,
        "tags": sorted(new_tags),
        "timestamp": new_timestamp.isoformat(),
        "duration_min": new_duration_min,
    }


@app.delete("/events/{event_id}")
def delete_event(event_id: str, current_user: dict = Depends(get_current_user)):
    ''' Delete a manual event entirely (all its tags). '''
    username = current_user["username"]
    existing = find_manual_event_by_id(username, event_id)
    if existing is None:
        raise HTTPException(404, "manual event not found")

    for tag in existing["tags"]:
        try:
            delete_event_tag_point(tag=tag, source="manual", timestamp=existing["timestamp"])
        except Exception as e:
            logger.warning(f"Failed to delete point for manual event {event_id} (tag={tag}): {e}")

    return {"ok": True}


# --- timeline (read-only merged view) ---

@app.get("/timeline")
def get_timeline(start: str | None = None, end: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Read-only merged view of calendar-derived events (from the local
    cache, so no ICS re-fetch needed) and manual tag logs (from
    InfluxDB), sorted chronologically. Powers the Timeline tab.

    `start`/`end` are ISO date strings (YYYY-MM-DD). Defaults to the
    last 7 days through tomorrow if omitted.
    '''
    now = datetime.now(timezone.utc)
    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else now - timedelta(days=7)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else now + timedelta(days=1)
    except ValueError as e:
        raise HTTPException(400, f"invalid start/end date: {e}") from e

    user_id = current_user["id"]
    calendar_names = {c["id"]: c["name"] for c in db.list_calendars(user_id)}

    entries = []

    # Calendar-derived - read straight from the local cache, no ICS
    # fetch, so this is always fast and doesn't touch external feeds.
    for ev in db.list_cached_events(user_id):
        try:
            ev_start = datetime.fromisoformat(ev["start_iso"])
        except ValueError:
            continue
        if not (start_dt <= ev_start < end_dt):
            continue
        entries.append({
            "kind": "calendar",
            "event_id": ev["event_id"],
            "timestamp": ev["start_iso"],
            "title": ev["title"],
            "calendar": calendar_names.get(ev["calendar_id"], "(deleted calendar)"),
            "tags": json.loads(ev["applied_tags"] or "[]"),
            "manually_tagged": bool(ev["manually_tagged"]),
            "duration_min": ev["duration_min"],
        })

    # Manual taps - from InfluxDB, reconstructed per event_id
    for ev in find_manual_events_in_range(current_user["username"], start_dt, end_dt):
        entries.append({
            "kind": "manual",
            "event_id": ev["event_id"],
            "timestamp": ev["timestamp"],
            "title": None,
            "calendar": None,
            "tags": ev["tags"],
            "duration_min": ev["duration_min"],
        })

    entries.sort(key=lambda e: e["timestamp"])
    return entries


# --- sleep ---

@app.post("/sleep")
def post_sleep(payload: SleepIn, current_user: dict = Depends(get_current_user)):
    if not (1 <= payload.score <= 5):
        raise HTTPException(400, "score must be between 1 and 5")

    session = find_last_completed_sleep_session(current_user["username"])
    if session is None:
        raise HTTPException(
            409,
            "No recent completed sleep session found - try again after your ring syncs "
            "(a session needs a recorded wake-up time and be long enough to not look like a nap). "
            "If this persists, check that your account username matches the GADGETBRIDGE_USER "
            "value configured for your ring parser instance."
        )

    submission_ts = datetime.now(timezone.utc)
    entry_id = write_sleep_point(
        user=current_user["username"],
        session_start=session["start_time"],
        sleep_date=session["sleep_date"],
        score=payload.score,
        qualifiers=payload.qualifiers,
        submission_ts=submission_ts,
    )
    return {
        "entry_id": entry_id,
        "sleep_date": session["sleep_date"],
        "score": payload.score,
        "qualifiers": payload.qualifiers,
        "resolved_session_duration_s": session["duration_s"],
    }


@app.get("/sleep")
def get_sleep_history(start: str | None = None, end: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Read-only history of subjective sleep entries. Powers the
    "Recent nights" list on the Sleep tab. start/end are ISO date
    strings; defaults to the last 30 days through tomorrow.

    Sorted by start_time (the session's own real timestamp), not
    sleep_date - multiple entries can share a sleep_date (see
    write_sleep_point's docstring for why), so sorting by date alone
    wouldn't give a stable or meaningful order between same-day entries.
    '''
    now = datetime.now(timezone.utc)
    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else now - timedelta(days=30)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else now + timedelta(days=1)
    except ValueError as e:
        raise HTTPException(400, f"invalid start/end date: {e}") from e

    entries = find_sleep_entries_in_range(current_user["username"], start_dt, end_dt)
    entries.sort(key=lambda e: e["start_time"] or "", reverse=True)
    return entries


@app.patch("/sleep/{entry_id}")
def patch_sleep(entry_id: str, payload: SleepUpdateIn, current_user: dict = Depends(get_current_user)):
    ''' Edit an existing sleep entry's score/qualifiers, addressed by
    its stable entry_id (not sleep_date - multiple entries can share a
    date, see write_sleep_point's docstring). Relies on
    write_sleep_point's fixed-per-session timestamp to overwrite
    cleanly - no delete-and-rewrite needed, unlike event tag edits.
    The caller (frontend) is expected to send every known qualifier
    explicitly as true/false, not just the ones that are true -
    InfluxDB only overwrites fields actually included in a write, so
    an omitted qualifier that was previously true would otherwise
    silently persist instead of being cleared.
    '''
    if not (1 <= payload.score <= 5):
        raise HTTPException(400, "score must be between 1 and 5")

    username = current_user["username"]
    existing = find_sleep_entry_by_id(username, entry_id)
    if existing is None:
        raise HTTPException(404, "no sleep entry found for this id")

    new_entry_id = write_sleep_point(
        user=username,
        session_start=existing["start_time"],
        sleep_date=existing["sleep_date"],
        score=payload.score,
        qualifiers=payload.qualifiers,
        submission_ts=datetime.now(timezone.utc),
    )
    return {"entry_id": new_entry_id, "sleep_date": existing["sleep_date"], "score": payload.score, "qualifiers": payload.qualifiers}


@app.delete("/sleep/{entry_id}")
def delete_sleep(entry_id: str, current_user: dict = Depends(get_current_user)):
    ''' Delete a sleep entry entirely, addressed by its stable entry_id. '''
    username = current_user["username"]
    existing = find_sleep_entry_by_id(username, entry_id)
    if existing is None:
        raise HTTPException(404, "no sleep entry found for this id")

    delete_sleep_entry(username, entry_id)
    return {"ok": True}


# --- calendars ---

@app.get("/calendars")
def get_calendars(current_user: dict = Depends(get_current_user)):
    return db.list_calendars(current_user["id"])


@app.post("/calendars")
def post_calendar(payload: CalendarIn, current_user: dict = Depends(get_current_user)):
    try:
        calendar_id = db.add_calendar(current_user["id"], payload.name, payload.ics_url, payload.default_tag)
    except Exception as e:
        raise HTTPException(400, f"could not add calendar: {e}") from e
    return {"id": calendar_id}


@app.patch("/calendars/{calendar_id}")
def patch_calendar(calendar_id: int, payload: CalendarUpdateIn, current_user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    db.update_calendar(calendar_id, current_user["id"], **fields)
    return {"ok": True}


@app.delete("/calendars/{calendar_id}")
def delete_calendar(calendar_id: int, current_user: dict = Depends(get_current_user)):
    db.delete_calendar(calendar_id, current_user["id"])
    return {"ok": True}


@app.post("/calendars/{calendar_id}/sync")
def trigger_calendar_sync(calendar_id: int, current_user: dict = Depends(get_current_user)):
    ''' Manual "sync now" for one of the current user's calendars.
    '''
    calendar = db.get_calendar(calendar_id, current_user["id"])
    if calendar is None:
        raise HTTPException(404, "calendar not found")

    from app.ics_sync import sync_calendar
    rules = db.list_keyword_rules(current_user["id"], enabled_only=True)
    sync_calendar(calendar, rules, current_user["username"])
    return {"ok": True}


@app.patch("/calendar_events/{event_id}/tags")
def patch_calendar_event_tags(event_id: str, payload: CalendarEventTagsIn, current_user: dict = Depends(get_current_user)):
    ''' Manually override a calendar-derived event's tags (add/edit/
    remove), independent of keyword-rule classification. Marks the
    event manually_tagged=1, so future scheduled syncs and reprocess
    runs leave it alone from now on - see the schema.sql comment on
    that column for why this is necessary (without it, the very next
    15-minute sync would silently revert the edit back to whatever the
    ruleset says).
    '''
    if not payload.tags:
        raise HTTPException(400, "at least one tag is required")

    user_id = current_user["id"]
    cached = db.get_cached_event(event_id, user_id)
    if cached is None:
        raise HTTPException(404, "calendar event not found")

    calendar = db.get_calendar(cached["calendar_id"], user_id)
    if calendar is None:
        raise HTTPException(409, "this event's calendar no longer exists")

    old_tags = set(json.loads(cached["applied_tags"] or "[]"))
    new_tags = set(payload.tags)
    start_dt = datetime.fromisoformat(cached["start_iso"])

    removed = old_tags - new_tags
    for tag in removed:
        try:
            delete_event_tag_point(tag=tag, source="calendar", timestamp=start_dt, calendar=calendar["name"])
        except Exception as e:
            logger.warning(f"Failed to delete stale point for calendar event {event_id} (tag={tag}): {e}")

    write_event_points(
        user=current_user["username"],
        tags=sorted(new_tags),
        source="calendar",
        timestamp=start_dt,
        event_id=event_id,
        calendar=calendar["name"],
        duration_min=cached["duration_min"],
    )
    db.set_cached_event_tags(event_id, sorted(new_tags), manually_tagged=True)

    return {"event_id": event_id, "tags": sorted(new_tags)}


@app.post("/calendar_events/{event_id}/reset_tags")
def reset_calendar_event_tags(event_id: str, current_user: dict = Depends(get_current_user)):
    ''' Undoes a manual tag override: re-runs keyword classification
    against the *current* ruleset (not whatever was cached before the
    override), writes the result, and clears manually_tagged so future
    syncs/reprocess apply normally to this event again.
    '''
    user_id = current_user["id"]
    cached = db.get_cached_event(event_id, user_id)
    if cached is None:
        raise HTTPException(404, "calendar event not found")
    if not cached["manually_tagged"]:
        raise HTTPException(400, "this event isn't manually tagged - nothing to reset")

    calendar = db.get_calendar(cached["calendar_id"], user_id)
    if calendar is None:
        raise HTTPException(409, "this event's calendar no longer exists")

    rules = db.list_keyword_rules(user_id, enabled_only=True)
    new_tags = classify_event(cached["title"], cached["description"], rules, calendar["default_tag"])
    old_tags = set(json.loads(cached["applied_tags"] or "[]"))
    start_dt = datetime.fromisoformat(cached["start_iso"])

    removed = old_tags - set(new_tags)
    for tag in removed:
        try:
            delete_event_tag_point(tag=tag, source="calendar", timestamp=start_dt, calendar=calendar["name"])
        except Exception as e:
            logger.warning(f"Failed to delete stale point while resetting calendar event {event_id} (tag={tag}): {e}")

    write_event_points(
        user=current_user["username"],
        tags=new_tags,
        source="calendar",
        timestamp=start_dt,
        event_id=event_id,
        calendar=calendar["name"],
        duration_min=cached["duration_min"],
    )
    db.set_cached_event_tags(event_id, new_tags, manually_tagged=False)

    return {"event_id": event_id, "tags": new_tags}


# --- keyword rules ---

@app.get("/keyword_rules")
def get_keyword_rules(current_user: dict = Depends(get_current_user)):
    return db.list_keyword_rules(current_user["id"])


@app.post("/keyword_rules/save_batch")
def save_keyword_rules_batch(payload: KeywordRuleBatchIn, current_user: dict = Depends(get_current_user)):
    ''' Commits a staged batch of add/delete changes to this user's
    ruleset in one go, then returns a precise count (and sample titles)
    of how many already-synced cached events would be reclassified
    differently under the new ruleset.

    This endpoint only commits the rule changes and reports the diff -
    it does NOT reprocess/rewrite any Influx data itself. That's a
    separate, explicit step via POST /reprocess.
    '''
    user_id = current_user["id"]
    old_rules = db.list_keyword_rules(user_id, enabled_only=True)

    for rule_id in payload.deleted_ids:
        db.delete_keyword_rule(rule_id, user_id)

    for rule in payload.added:
        if rule.category not in {"context", "meta", "substance", "restful"}:
            raise HTTPException(400, f"invalid category: {rule.category}")
        db.add_keyword_rule(
            user_id, rule.keyword, rule.tag, rule.category,
            is_regex=rule.is_regex, match_field=rule.match_field,
            priority=rule.priority, enabled=rule.enabled, exclusive=rule.exclusive,
        )

    new_rules = db.list_keyword_rules(user_id, enabled_only=True)
    diff = compute_reclassification_diff(user_id, old_rules, new_rules)

    return {
        "saved": True,
        "affected_events": diff["count"],
        "sample_titles": diff["sample_titles"],
    }


@app.post("/reprocess")
def post_reprocess(current_user: dict = Depends(get_current_user)):
    ''' Kicks off a background reprocess of the current user's cached
    calendar events against their current (just-saved) ruleset. Runs in
    a thread rather than blocking this request or the rest of the UI -
    poll GET /reprocess/status for progress.
    '''
    started = reprocess.start_reprocess(current_user["id"], current_user["username"])
    if not started:
        return {"started": False, "reason": "already running", **reprocess.get_status(current_user["id"])}
    return {"started": True, **reprocess.get_status(current_user["id"])}


@app.get("/reprocess/status")
def get_reprocess_status(current_user: dict = Depends(get_current_user)):
    return reprocess.get_status(current_user["id"])


# --- tag definitions ---

@app.get("/tag_definitions")
def get_tag_definitions(current_user: dict = Depends(get_current_user)):
    return db.list_tag_definitions(current_user["id"])


@app.post("/tag_definitions")
def post_tag_definition(payload: TagDefinitionIn, current_user: dict = Depends(get_current_user)):
    try:
        tag_def_id = db.add_tag_definition(
            current_user["id"], payload.tag, payload.label, payload.category,
            is_duration=payload.is_duration, sort_order=payload.sort_order,
        )
    except Exception as e:
        raise HTTPException(400, f"could not add tag: {e}") from e
    return {"id": tag_def_id}


@app.delete("/tag_definitions/{tag_def_id}")
def delete_tag_definition(tag_def_id: int, current_user: dict = Depends(get_current_user)):
    db.delete_tag_definition(tag_def_id, current_user["id"])
    return {"ok": True}


# --- health check (unauthenticated - used by docker healthcheck /
# restart policies) ---

@app.get("/health")
def health():
    return {"status": "ok"}


# --- static UI ---
_static_dir = Path(__file__).parent.parent / "static"
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")