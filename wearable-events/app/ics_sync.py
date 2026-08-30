import json
import re
from datetime import datetime, timezone

import httpx
from icalendar import Calendar
from loguru import logger

from app import db
from app.influx import calendar_event_id, write_event_points


def _to_utc_datetime(dt_value) -> datetime:
    ''' icalendar gives back either a date (all-day events) or a
    datetime (possibly naive, possibly tz-aware) depending on the
    feed. Normalize everything to a tz-aware UTC datetime so downstream
    (event_id hashing, Influx timestamps) is consistent.
    '''
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)
    # date-only (all-day event) - treat as midnight UTC
    return datetime(dt_value.year, dt_value.month, dt_value.day, tzinfo=timezone.utc)


def classify_event(title: str, description: str, rules: list[dict], default_tag: str) -> list[str]:
    ''' Apply keyword_rules per spec §5, extended with a per-rule
    `exclusive` flag:
      - group enabled rules by category
      - within a category, EXCLUSIVE rules (the default, matching
        original behaviour) compete for one slot: first match wins by
        ascending priority, and any other exclusive rule in the same
        category is skipped once a winner is found
      - NON-exclusive rules (exclusive=0) don't compete for that slot -
        if they match, their tag is added unconditionally, alongside
        whatever else matched in their category or any other. This is
        the escape hatch for "I want both tags when both rules match",
        without changing the default behaviour for existing rules.
      - across categories, all surviving tags combine (unchanged)
      - if no EXCLUSIVE 'context' rule matches, fall back to default_tag
        (a non-exclusive context-category rule matching doesn't count
        as "context is covered" - the fallback still applies, since
        exclusive and non-exclusive rules are answering different
        questions: "what's the one context" vs "what extra tag applies")
      - other categories contribute nothing if nothing matches
    '''
    title = title or ""
    description = description or ""

    matched_by_category: dict[str, str] = {}
    extra_tags: list[str] = []

    for rule in rules:
        category = rule["category"]
        is_exclusive = bool(rule.get("exclusive", True))

        if is_exclusive and category in matched_by_category:
            # Already have an exclusive winner for this category (rules
            # are pre-sorted by priority ascending, so the first hit wins)
            continue

        haystack = title if rule["match_field"] == "title" else description

        if rule["is_regex"]:
            hit = re.search(rule["keyword"], haystack, re.IGNORECASE) is not None
        else:
            hit = rule["keyword"].lower() in haystack.lower()

        if not hit:
            continue

        if is_exclusive:
            matched_by_category[category] = rule["tag"]
        else:
            extra_tags.append(rule["tag"])

    if "context" not in matched_by_category:
        matched_by_category["context"] = default_tag

    # Dedupe while keeping a stable order: exclusive-category winners
    # first, then non-exclusive extras, dropping any extra tag that
    # already showed up as a category winner.
    result = list(matched_by_category.values())
    for tag in extra_tags:
        if tag not in result:
            result.append(tag)

    return result


def sync_calendar(calendar: dict, rules: list[dict], username: str):
    ''' Fetch + parse one calendar's ICS feed, classify each event, and
    write the resulting tag points to Influx under `username`. Updates
    last_synced/last_error on the calendars row per spec §7.
    '''
    try:
        resp = httpx.get(calendar["ics_url"], timeout=30, follow_redirects=True)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
    except Exception as e:
        logger.warning(f"Calendar '{calendar['name']}' (user={username}): fetch/parse failed - {e}")
        db.set_calendar_sync_result(calendar["id"], success=False, error=str(e))
        return

    event_count = 0
    for component in cal.walk("VEVENT"):
        try:
            title = str(component.get("SUMMARY", ""))
            description = str(component.get("DESCRIPTION", ""))

            dtstart_prop = component.get("DTSTART")
            if dtstart_prop is None:
                continue
            start_dt = _to_utc_datetime(dtstart_prop.dt)

            duration_min = None
            dtend_prop = component.get("DTEND")
            if dtend_prop is not None:
                end_dt = _to_utc_datetime(dtend_prop.dt)
                duration_min = int((end_dt - start_dt).total_seconds() // 60)

            tags = classify_event(title, description, rules, calendar["default_tag"])
            event_id = calendar_event_id(calendar["name"], start_dt.isoformat(), title)

            existing = db.get_cached_event(event_id, calendar["user_id"])
            if existing and existing["manually_tagged"]:
                # Respect a manual tag override from the Timeline UI -
                # use the cached tags instead of what the ruleset would
                # produce, so a scheduled sync doesn't silently revert
                # someone's manual fix. See the manually_tagged comment
                # in schema.sql.
                tags = json.loads(existing["applied_tags"] or "[]")

            write_event_points(
                user=username,
                tags=tags,
                source="calendar",
                timestamp=start_dt,
                event_id=event_id,
                calendar=calendar["name"],
                duration_min=duration_min,
            )
            db.upsert_cached_event(
                event_id=event_id,
                user_id=calendar["user_id"],
                calendar_id=calendar["id"],
                title=title,
                description=description,
                start_iso=start_dt.isoformat(),
                duration_min=duration_min,
                applied_tags=tags,
            )
            event_count += 1
        except Exception as e:
            # One malformed VEVENT shouldn't abort the whole calendar's sync
            logger.warning(f"Calendar '{calendar['name']}' (user={username}): skipped one event due to parse error - {e}")
            continue

    logger.info(f"Calendar '{calendar['name']}' (user={username}): synced {event_count} event(s)")
    db.set_calendar_sync_result(calendar["id"], success=True)


def sync_all_calendars():
    ''' Background job entrypoint - fetches every household member's
    enabled calendars in one pass. Per spec §7, failures are non-fatal
    per-calendar: one bad feed doesn't stop the others, and the next
    scheduled run is the retry (no tight backoff loop).

    This runs independently of any browser session, so it looks up
    rules per-owner (each calendar's rules are that owner's own
    keyword_rules, not a shared set) and tags written points with the
    calendar owner's username - not a session, since none exists here.
    '''
    calendars = db.list_enabled_calendars_all_users()
    if not calendars:
        logger.debug("No enabled calendars to sync")
        return

    logger.info(f"Starting calendar sync: {len(calendars)} enabled calendar(s) across all users")

    rules_cache: dict[int, list[dict]] = {}
    for calendar in calendars:
        user_id = calendar["user_id"]
        if user_id not in rules_cache:
            rules_cache[user_id] = db.list_keyword_rules(user_id, enabled_only=True)
        sync_calendar(calendar, rules_cache[user_id], calendar["owner_username"])