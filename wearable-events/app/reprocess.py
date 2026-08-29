import json
import threading
from datetime import datetime, timezone

from loguru import logger

from app import db
from app.ics_sync import classify_event
from app.influx import delete_event_tag_point, write_event_points

_lock = threading.Lock()

# Per-user state, since each household member can independently trigger
# and monitor their own reprocess run. Keyed by user_id.
_states: dict[int, dict] = {}


def _default_state() -> dict:
    return {
        "status": "idle",  # idle | running | done | error
        "total": 0,
        "processed": 0,
        "changed": 0,
        "started_at": None,
        "finished_at": None,
        "error": None,
    }


def get_status(user_id: int) -> dict:
    with _lock:
        return dict(_states.get(user_id, _default_state()))


def compute_reclassification_diff(user_id: int, old_rules: list[dict], new_rules: list[dict]) -> dict:
    ''' Dry run only - writes nothing. For every cached (previously
    synced) calendar event belonging to user_id, compares classification
    under old_rules vs new_rules. This is what powers the "N events
    would change" figure shown before a reprocess is actually kicked
    off, so the person is confirming a real number, not a guess.

    Manually-tagged events are excluded - reprocessing skips them (see
    _run_reprocess), so including them in this count would overstate how
    many events will actually change.
    '''
    cached = db.list_cached_events(user_id)
    affected_titles = []
    for ev in cached:
        if ev["manually_tagged"]:
            continue
        calendar = db.get_calendar(ev["calendar_id"])
        if calendar is None:
            # Calendar was deleted since this event was cached - nothing
            # sensible to reclassify against, skip it.
            continue
        old_tags = set(classify_event(ev["title"], ev["description"], old_rules, calendar["default_tag"]))
        new_tags = set(classify_event(ev["title"], ev["description"], new_rules, calendar["default_tag"]))
        if old_tags != new_tags:
            affected_titles.append(ev["title"] or "(untitled event)")

    return {"count": len(affected_titles), "sample_titles": affected_titles[:5]}


def _run_reprocess(user_id: int, username: str):
    with _lock:
        _states[user_id] = _default_state()
        _states[user_id]["status"] = "running"
        _states[user_id]["started_at"] = datetime.now(timezone.utc).isoformat()

    try:
        rules = db.list_keyword_rules(user_id, enabled_only=True)
        cached = db.list_cached_events(user_id)
        with _lock:
            _states[user_id]["total"] = len(cached)

        for ev in cached:
            if ev["manually_tagged"]:
                # Respect the manual override - see the manually_tagged
                # comment in schema.sql. Still counts as "processed" for
                # progress tracking, just never "changed".
                with _lock:
                    _states[user_id]["processed"] += 1
                continue

            calendar = db.get_calendar(ev["calendar_id"])
            if calendar is None:
                with _lock:
                    _states[user_id]["processed"] += 1
                continue

            new_tags = classify_event(ev["title"], ev["description"], rules, calendar["default_tag"])
            old_tags = json.loads(ev["applied_tags"] or "[]")

            if set(new_tags) != set(old_tags):
                start_dt = datetime.fromisoformat(ev["start_iso"])
                removed = set(old_tags) - set(new_tags)
                for tag in removed:
                    try:
                        delete_event_tag_point(tag=tag, source="calendar", timestamp=start_dt, calendar=calendar["name"])
                    except Exception as e:
                        logger.warning(
                            f"Reprocess (user={username}): failed to delete stale point "
                            f"(event_id={ev['event_id']}, tag={tag}): {e}"
                        )

                # Writing the full new tag set is safe even for tags that
                # didn't change - identical tag-set + timestamp overwrites
                # in InfluxDB rather than duplicating.
                write_event_points(
                    user=username,
                    tags=new_tags,
                    source="calendar",
                    timestamp=start_dt,
                    event_id=ev["event_id"],
                    calendar=calendar["name"],
                    duration_min=ev["duration_min"],
                )
                db.set_cached_event_tags(ev["event_id"], new_tags, manually_tagged=False)
                with _lock:
                    _states[user_id]["changed"] += 1

            with _lock:
                _states[user_id]["processed"] += 1

        with _lock:
            _states[user_id]["status"] = "done"
            _states[user_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Reprocess complete for user={username}: "
            f"{_states[user_id]['changed']}/{_states[user_id]['total']} event(s) reclassified"
        )

    except Exception as e:
        logger.error(f"Reprocess failed for user={username}: {e}")
        with _lock:
            _states[user_id]["status"] = "error"
            _states[user_id]["error"] = str(e)
            _states[user_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_reprocess(user_id: int, username: str) -> bool:
    ''' Kicks off a background reprocess thread for one user. Returns
    False (without starting a new one) if a reprocess is already running
    for that user - the UI should surface the existing run's status
    instead. Different users can run reprocesses concurrently.
    '''
    with _lock:
        existing = _states.get(user_id)
        if existing and existing["status"] == "running":
            return False
    thread = threading.Thread(target=_run_reprocess, args=(user_id, username), daemon=True)
    thread.start()
    return True