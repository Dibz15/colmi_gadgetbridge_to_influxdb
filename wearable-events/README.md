# wearable-events

Calendar tagging + subjective sleep score, as a companion to the
[Colmi ring parser](../parser/README.md). Adds a
"context" layer on top of raw sensor data — manual one-tap tags
(caffeine, alcohol, social), calendar-derived tags (meeting, deep-work,
commute, resolved from ICS feeds via keyword rules), and a nightly
1–5 subjective sleep score — all written into the same InfluxDB bucket
so they can eventually be correlated against HRV/HR/temperature trends.

Has its own login (multiple household members, one account each) and a
small web UI served directly from the FastAPI backend — no separate
frontend build step.

## Architecture

```
FastAPI app (app/main.py)
  ├── SQLite (schema.sql) — users, sessions, calendars, keyword_rules,
  │                          tag_definitions, calendar_events_cache
  │                          all scoped per-user
  ├── InfluxDB — writes: events, subjective_sleep measurements
  │              reads: the ring parser's sensor measurement (read-only),
  │                     to resolve "last night's sleep session" and to
  │                     find unclaimed ring identities during signup
  └── APScheduler — background job, polls every user's enabled ICS
                     calendar feeds on an interval, classifies events via
                     keyword rules, writes tag points
```

Static UI (`static/`) is plain HTML/CSS/JS, no build tooling — served by
FastAPI's `StaticFiles` mount at `/`.

## Directory structure

```
wearable-events/
├── Dockerfile
├── requirements.txt
├── schema.sql                 SQLite schema (applied on every startup, idempotent)
├── app/
│   ├── main.py                FastAPI routes, APScheduler wiring, lifespan/startup
│   ├── auth.py                password hashing, session tokens, get_current_user dependency
│   ├── config.py               env var definitions
│   ├── db.py                   all SQLite access (every query lives here)
│   ├── influx.py               InfluxDB reads/writes
│   ├── ics_sync.py             ICS fetch/parse, keyword-rule classification
│   └── reprocess.py            background reclassification job + progress tracking
└── static/
    ├── index.html
    ├── app.js
    └── style.css
```

## Running it for development

You don't need Docker to iterate on this — it's a plain FastAPI app.

```bash
cd wearable-events
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export SQLITE_PATH=./dev-config.db
export INFLUX_URL=http://localhost:8086      # or wherever your dev InfluxDB is
export INFLUX_TOKEN=devtoken
export INFLUX_ORG=home
export INFLUX_BUCKET=health
export ADMIN_USERNAME=dev
export ADMIN_PASSWORD=devpassword

uvicorn app.main:app --reload --port 8080
```

Open `http://localhost:8080` — you'll get the login screen, log in with
the `ADMIN_USERNAME`/`ADMIN_PASSWORD` you set (bootstrap only fires once,
on an empty `users` table, so delete `dev-config.db` to reset it).

Auto-reload (`--reload`) picks up changes to `app/*.py` immediately. The
static files under `static/` are served fresh on every request (no
caching), so editing HTML/CSS/JS just needs a browser refresh.

You don't strictly need a real ring or InfluxDB data to develop most of
this app — calendars, keyword rules, tag buttons, and manual event
logging all work against an empty InfluxDB. The one thing that needs
real sensor data to test end-to-end is `/sleep`, since it queries for an
actual completed sleep session (see [Known limitations](#known-limitations)).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SQLITE_PATH` | `/data/config.db` | Where the config database lives |
| `INFLUX_URL` | `http://influxdb:8086` | InfluxDB server |
| `INFLUX_TOKEN` | — | InfluxDB API token |
| `INFLUX_ORG` | `home` | InfluxDB org |
| `INFLUX_BUCKET` | `health` | InfluxDB bucket (shared with the ring parser) |
| `EVENTS_MEASUREMENT` | `events` | Measurement this service writes tag events into |
| `SLEEP_MEASUREMENT` | `subjective_sleep` | Measurement for nightly sleep scores |
| `SENSOR_MEASUREMENT` | `colmi` | Measurement the ring parser writes into — must match `INFLUXDB_MEASUREMENT` on that service |
| `SYNC_INTERVAL_MINUTES` | `15` | How often the background job polls ICS calendar feeds |
| `MIN_SLEEP_SESSION_SECONDS` | `10800` (3h) | Minimum session length to count as "last night's sleep" rather than a nap |
| `PORT` | `8080` | Server port |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | First-run bootstrap account, only created if the `users` table is empty |
| `SESSION_COOKIE_SECURE` | `N` | Set `Y` if served behind TLS, to add the `Secure` cookie flag |
| `SESSION_MAX_AGE_DAYS` | `30` | Session cookie lifetime |

## Auth model

- Passwords are bcrypt-hashed. Sessions are opaque random tokens in an
  `HttpOnly` cookie, looked up against a `sessions` table (not a
  signed-cookie scheme) — so logout/revocation is a plain row delete.
- No public signup route. The `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars
  create exactly one account, and only if the `users` table is currently
  empty. After that, any logged-in user can add another from the
  Manage tab (`POST /users`).
- **Convention, not enforcement:** a person's wearable-events username
  should match their `GADGETBRIDGE_USER` value on the ring parser, so
  their calendar/sleep data and their sensor data share the same `user`
  tag in InfluxDB. The "Add household member" form queries
  `GET /unclaimed_ring_users` (distinct `user` tag values already in the
  sensor measurement, minus ones already claimed) and offers them as a
  picker instead of free text, to reduce typo risk — but nothing stops
  someone from typing a mismatched username manually, and there's no
  server-side check tying the two together after the fact.

## Data model

All config tables (`calendars`, `keyword_rules`, `tag_definitions`,
`calendar_events_cache`) are scoped by `user_id` with `ON DELETE CASCADE`
— see `schema.sql` for the full definitions and inline comments. Every
function in `db.py` takes and enforces `user_id`, so there's no path to
reading or modifying another user's rows short of a bug in a route
handler forgetting to pass it.

`calendar_events_cache` deserves a callout: it's not part of the original
spec, added specifically to support reprocessing (see below) — it caches
each synced event's raw title/description/timing locally, keyed by the
same `event_id` written to InfluxDB, so keyword rule changes can be
reclassified against everything ever synced without depending on the ICS
feed still exposing old events (most feeds are a rolling window, not a
full archive).

## API reference

All routes except `/auth/login` and `/health` require a valid session
cookie (`Depends(get_current_user)` → `401` if missing/invalid).

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | Log in, sets session cookie |
| POST | `/auth/logout` | Log out, deletes the session row |
| GET | `/auth/me` | Current logged-in user |
| GET | `/users` | List all accounts (usernames only) |
| POST | `/users` | Add a household member |
| GET | `/unclaimed_ring_users` | Sensor `user` tag values not yet claimed by an account |
| POST | `/events` | Log a manual tag |
| POST | `/sleep` | Submit a 1–5 sleep score, resolved against the most recent completed sleep session |
| GET / POST | `/calendars` | List / add an ICS calendar feed |
| PATCH / DELETE | `/calendars/{id}` | Update / remove a calendar |
| POST | `/calendars/{id}/sync` | Manually trigger a sync for one calendar |
| GET | `/keyword_rules` | List this user's keyword rules |
| POST | `/keyword_rules/save_batch` | Commit a staged batch of rule adds/deletes; returns a precise reclassification-diff count |
| POST | `/reprocess` | Kick off a background reclassification of cached events against the current ruleset |
| GET | `/reprocess/status` | Poll progress of the above |
| GET / POST | `/tag_definitions` | List / add a manual tag button |
| DELETE | `/tag_definitions/{id}` | Remove a tag button |
| GET | `/health` | Unauthenticated, for Docker healthchecks |

## Keyword rule classification

Applied per calendar event (`app/ics_sync.py::classify_event`):

1. Rules are grouped by `category` (`context` | `meta` | `substance` |
   `restful`).
2. Within a category, the first match wins, ordered by ascending
   `priority` — mutually exclusive, only one tag per category survives.
3. Across categories, all surviving tags combine onto the same event.
4. If no `context` rule matches, the calendar's `default_tag` is used as
   a fallback — every event ends up with at least one context tag.
5. Other categories contribute nothing if nothing matches (no fallback).

## Reprocessing

Rule changes in the Manage tab are staged client-side and only committed
on "Save changes" (`POST /keyword_rules/save_batch`), which also runs a
dry-run reclassification against every cached event and returns a
precise affected-event count. If that count is non-zero, the UI asks
before triggering an actual reprocess (`POST /reprocess`), which runs in
a background thread — polls to `/reprocess/status` drive a progress
banner without blocking the rest of the UI.

One real constraint worth knowing if you're touching this code:
`event_id` is stored as an InfluxDB **field**, not a tag, and Influx's
delete API can only filter on tags. So exact per-event deletion isn't
directly possible — `reprocess.py` instead deletes by
`tag + calendar + source` within a tight time window around the event's
exact timestamp, which is safe in practice for a personal calendar but
has a narrow theoretical edge case (two events in the same calendar
starting the same second). See the comment in
`_delete_event_tag_point` for the full reasoning.

## Known limitations

- **No password reset flow.** Fixing a forgotten password means editing
  `users.password_hash` directly in the SQLite file, or dropping and
  recreating the account.
- **`/sleep` depends on the ring parser's exact field names.** It queries
  the sensor measurement for a `sleep_session_duration_s` field on a
  `sample_type="sleep_session"` point — if the parser's schema changes,
  this breaks silently (returns "no session found" rather than an error).
- **Reprocessing is bounded by the local cache**, not the ICS feed itself
  — it can't reclassify an event that was never synced while this
  service's cache existed, even if the feed history is theoretically
  fetchable.
- **Multi-tenant isolation relies entirely on correct `user_id`
  threading through every query** — there's no database-level row
  security net (e.g. SQLite doesn't have RLS); a bug in a new route that
  forgets to pass `user_id` would be a real cross-user data leak. Worth
  extra care/review on any new endpoint that touches `calendars`,
  `keyword_rules`, `tag_definitions`, or `calendar_events_cache`.