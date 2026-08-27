import json
import threading
from datetime import datetime, timedelta, timezone

from loguru import logger

from app import db
from app.config import INFLUX_BUCKET, INFLUX_ORG
from app.ics_sync import classify_event
from app.influx import get_client, write_event_points

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
    '''
    cached = db.list_cached_events(user_id)
    affected_titles = []
    for ev in cached:
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


def _delete_event_tag_point(*, tag: str, calendar: str, timestamp: datetime):
    ''' Delete the single Influx point for one (event, tag) pairing.

    event_id is stored as a FIELD, not a tag, on events points - and
    InfluxDB's delete API predicates can only match on tags/measurement,
    not field values. So we can't delete "everything with this event_id"
    directly. Instead we rely on tag + calendar + source + the event's
    exact timestamp being unique in practice for a personal calendar -
    a narrow (1 second) time window keeps this safe from touching
    neighbouring points. Known edge case: two events in the same
    calendar starting at the exact same second would collide here; for
    a personal calendar this is vanishingly unlikely and not worth the
    complexity of handling. Note this predicate doesn't filter on
    `user` (event_id/tag/calendar/source uniqueness already scopes it
    tightly enough in practice, and `user` isn't guaranteed stable if
    an account were ever renamed - a real but narrow edge case).
    '''
    client = get_client()
    delete_api = client.delete_api()
    start = timestamp
    stop = timestamp + timedelta(seconds=1)
    predicate = f'_measurement="events" AND tag="{tag}" AND calendar="{calendar}" AND source="calendar"'
    delete_api.delete(start, stop, predicate, bucket=INFLUX_BUCKET, org=INFLUX_ORG)


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
                        _delete_event_tag_point(tag=tag, calendar=calendar["name"], timestamp=start_dt)
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
                db.update_cached_event_tags(ev["event_id"], new_tags)
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
