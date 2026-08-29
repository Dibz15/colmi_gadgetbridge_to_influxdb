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
from app.ics_sync import sync_all_calendars
from app.influx import (
    find_last_completed_sleep_session,
    find_manual_events_in_range,
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


class SleepIn(BaseModel):
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
            "timestamp": ev["start_iso"],
            "title": ev["title"],
            "calendar": calendar_names.get(ev["calendar_id"], "(deleted calendar)"),
            "tags": json.loads(ev["applied_tags"] or "[]"),
            "duration_min": ev["duration_min"],
        })

    # Manual taps - from InfluxDB, reconstructed per event_id
    for ev in find_manual_events_in_range(current_user["username"], start_dt, end_dt):
        entries.append({
            "kind": "manual",
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
    write_sleep_point(
        user=current_user["username"],
        sleep_date=session["sleep_date"],
        score=payload.score,
        qualifiers=payload.qualifiers,
        submission_ts=submission_ts,
    )
    return {
        "sleep_date": session["sleep_date"],
        "score": payload.score,
        "qualifiers": payload.qualifiers,
        "resolved_session_duration_s": session["duration_s"],
    }


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
            priority=rule.priority, enabled=rule.enabled,
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