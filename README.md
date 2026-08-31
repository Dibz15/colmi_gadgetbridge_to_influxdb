# Biomarker Stack

A self-hosted alternative to subscription-gated wearable health apps.

Wear a cheap smart ring, own your own data. This stack pulls biomarker
data (heart rate, HRV, SpO2, temperature, sleep) off a Colmi/Yawell smart
ring via [Gadgetbridge](https://gadgetbridge.org/), stores it in your own
InfluxDB instance, and visualizes it in Grafana — no vendor cloud, no
per-user fees, no app you don't control. A companion service adds manual
context tagging (caffeine, alcohol, meetings pulled from your calendar)
and a subjective sleep-quality score, so the raw sensor trends can
eventually be correlated against what was actually happening in your day.

Everything runs in Docker and is designed to be pasted straight into a
home server / NAS setup (tested against [CasaOS](https://casaos.io/), but
plain `docker compose` works anywhere).

## What this is (and isn't)

- **Is:** a data-ownership layer. You get your raw sensor data, in your
  own database, with your own dashboards and alerting rules.
- **Isn't:** a polished consumer app. There's no fancy "readiness score" —
  the ring itself computes a basic HRV baseline/status, and this stack
  otherwise gives you the raw trends. Building smarter analysis on top is
  on you (or a future contribution).

## Architecture

```
Colmi ring (BLE)
   │
   ▼
Gadgetbridge (Android app, periodic auto-export)
   │  WebDAV
   ▼
Nextcloud (or any WebDAV server)
   │  WebDAV (pulled on a loop by the parser container)
   ▼
InfluxDB  ──────────────────────────────▶  Grafana (dashboards, alerts)
   ▲                                            │
   │ writes                                     ▼
wearable-events (calendar tags,             ntfy (push notifications)
subjective sleep score - has its
own login, one account per person)
   ▲
   │ ICS feed
Your calendar (Google Calendar, etc.)
```

Two custom images are built from this repo (see
[Building the images](#building-the-images) below); everything else
(InfluxDB, Grafana, ntfy) is an off-the-shelf image.

## Prerequisites

- A Linux host that can run Docker + Docker Compose (a NAS, a Raspberry
  Pi, a home server — anything).
- A Colmi/Yawell smart ring. `R09` is a good starting point: it has the
  fullest sensor set that's well-supported by Gadgetbridge (HR, HRV,
  SpO2, temperature) at low cost. Other models in the family
  (R02/R03/R06/R10/R11/R12) work too — see
  [`colmi_gadgetbridge_to_influxdb/README.md`](./colmi_gadgetbridge_to_influxdb/README.md)
  for per-model notes.
- [Gadgetbridge](https://gadgetbridge.org/) installed on an Android phone
  (from F-Droid, not Google Play).
- A WebDAV server reachable from both your phone and your Docker host —
  Nextcloud is what this was built and tested against, but any WebDAV
  target Gadgetbridge can export to should work.
- A Docker Hub account (free tier is fine) if you want to build and host
  your own images via the included GitHub Actions workflow. You can also
  build the images locally instead and skip Docker Hub entirely — see
  the note at the end of [Setup](#setup).

## Repo structure

```
.
├── .github/workflows/docker-publish.yml   builds + pushes BOTH container images
├── docker-compose.yml              the full stack: InfluxDB, Grafana, ntfy, parser, wearable-events
├── .env.example                    copy to .env, fill in your own values
├── colmi_gadgetbridge_to_influxdb/ the ring parser
│   ├── app/gadgetbridge_to_influxdb.py    reads Gadgetbridge's exported DB, writes to InfluxDB
│   ├── Dockerfile
│   └── entrypoint.sh
├── grafana/                         Grafana datasource + alerting config-as-code (see step 7)
│   ├── provisioning-templates/      templates - NOT read directly by Grafana, see render-provisioning.sh
│   └── render-provisioning.sh       renders templates into a Docker volume before Grafana starts
└── wearable-events/                calendar tagging + subjective sleep score, has its own web UI
    ├── app/                        FastAPI backend (login, calendars, keyword rules, reprocessing)
    ├── static/                     the web UI itself
    └── schema.sql
```

## Setup

### 1. Get the container images

Two ways to do this — pick whichever suits you:

**Use prebuilt images.** Point `.env` at wherever this repo's images are
published (see [Configure the stack](#3-configure-the-stack) below for
the exact variables) and skip straight to step 2.

**Build them yourself.** No Docker Hub account or CI setup required —
swap the relevant `image:` line in `docker-compose.yml` for a `build:`
block, e.g.:

```yaml
wearable-events:
  build:
    context: ./wearable-events
```

then run `docker compose up -d --build` later in step 4. Do the same for
the `parser` service (`context: ./colmi_gadgetbridge_to_influxdb`) if you
want that one built locally too.

(If you're maintaining your own fork and want it to build and publish
images automatically on every push, the repo includes a GitHub Actions
workflow for that — see
[`.github/workflows/docker-publish.yml`](./.github/workflows/docker-publish.yml).
Not needed for normal use.)

### 2. Set up Gadgetbridge

Pair your ring in Gadgetbridge, then go to **Settings → Data auto-export**
and point it at your WebDAV server (e.g. a Nextcloud folder like
`GadgetBridge/`). Gadgetbridge will periodically write its full database
there — that's what the parser container reads.

### 3. Configure the stack

```bash
git clone https://github.com/dibz15/colmi_gadgetbridge_to_influxdb
cd colmi_gadgetbridge_to_influxdb
cp .env.example .env
```

> Before generating any secrets: Compose applies its own `${VAR}` interpolation syntax to .env's own values, not just to docker-compose.yml. A raw `$` inside a generated password gets misread as the start of a variable reference and silently truncates everything after it — no error, it just quietly produces a shorter, wrong value. Generating secrets with openssl rand -hex N (as used throughout this guide) sidesteps this entirely, since hex output can never contain $. See the comment at the top of .env.example for the full explanation.

Edit `.env` and fill in, at minimum:

- `PARSER_IMAGE` / `WEARABLE_EVENTS_IMAGE` — set these to wherever the
  images live (prebuilt or your own, per step 1)
- `INFLUXDB_TOKEN` and `INFLUXDB_INIT_ADMIN_TOKEN` — set both to the same value: a randomly generated token, not something you type yourself (this is a real credential with full API access). Generate one with:
```bash
  openssl rand -hex 32
```
- `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS`, `WEBDAV_PATH` — match
  whatever you set up in step 2 (use a dedicated WebDAV app password,
  not your main account password, if your server supports one)
- `WEARABLE_EVENTS_ADMIN_USERNAME` / `WEARABLE_EVENTS_ADMIN_PASSWORD` —
  your first login for the wearable-events web UI

Every variable in `.env.example` has an inline comment explaining what
it does and a safe default where one exists.

### 4. Start everything

```bash
docker compose up -d
```

If you're building one or both images locally (step 1), add `--build`.

This starts InfluxDB, Grafana, ntfy, the ring parser, and wearable-events.

### 5. Set up InfluxDB

InfluxDB initializes itself on first boot, using the INFLUXDB_ORG, INFLUXDB_BUCKET, INFLUXDB_INIT_USERNAME, INFLUXDB_INIT_PASSWORD, and INFLUXDB_TOKEN values from your .env — no manual setup wizard needed.

Open http://<your-host>:8086 and log in with INFLUXDB_INIT_USERNAME/ INFLUXDB_INIT_PASSWORD to confirm the org and bucket you set exist.

This auto-init only runs once, against an empty data volume. If you change INFLUXDB_ORG/INFLUXDB_BUCKET/etc. in .env after the first boot, it won't retroactively apply — either create the new org/bucket/ token manually from this UI, or wipe the InfluxDB volume (you'll lose any data already collected) to trigger a fresh init.

### 6. Set up Grafana

Open `http://<your-host>:3000` (default login `admin`/`admin`, you'll be
asked to change it). Add InfluxDB as a data source: URL
`http://influxdb:8086`, using the org/bucket/token you set in `.env`.
Build dashboards on whatever fields matter to you — heart rate, HRV,
temperature, and sleep stages are all written by the parser (see the
[parser README](./parser/README.md) for the full
field list).

### 7. Set up ntfy (for alerting)

ntfy is included in the stack for push notifications, but ships with auth locked down (NTFY_AUTH_DEFAULT_ACCESS: deny-all, signup disabled) rather than open by default. You need two ntfy accounts before alerting works end to end:

```bash
# Your own account, to subscribe and receive notifications
docker exec -it biomarker-ntfy ntfy user add youruser
docker exec -it biomarker-ntfy ntfy access youruser "$NTFY_TOPIC" read

# A dedicated account for Grafana to publish as (matches
# NTFY_GRAFANA_USER / NTFY_GRAFANA_PASSWORD in your .env)
docker exec -it biomarker-ntfy ntfy user add grafana
docker exec -it biomarker-ntfy ntfy access grafana "$NTFY_TOPIC" write
```

Then subscribe to $NTFY_TOPIC (default biomarker-alerts) from the ntfy app on your phone, pointed at http://<your-host>:8090.

#### Grafana alerting (provisioned)

Unlike the rest of this stack, the alerting pipeline is genuinely wired up already — provisioned from templates under grafana/provisioning-templates/, not something you configure by hand:

- Datasource (`datasources/influxdb.yaml.template`) — the InfluxDB connection from step 6, using your .env org/bucket/token.
- Alert rule (`alerting/rules.yaml.template`) — "HRV dropped below baseline": compares your last 24h average HRV against your prior 14-day average, and fires if it's dropped more than 20%. This is a worked example, not the only possible rule — the file has comments on how to copy the pattern for resting heart rate or temperature.
- Contact point (`alerting/contact-points.yaml.template`) — routes to ntfy using the grafana account above.
- Notification policy (`alerting/notification-policies.yaml.template`) — routes all alerts in this Grafana instance to that contact point.

These are templates, not the final config Grafana reads. A small init container (`grafana-provisioning-init`) renders them with envsubst into a Docker volume before Grafana starts, substituting the .env values in. This exists because Grafana's alert-rule provisioning does not support `$ENV_VAR` interpolation inside the query/settings blocks — this was discovered the hard way (a literal `$INFLUXDB_BUCKET` string reached InfluxDB and failed with "could not find bucket") and is now handled by the render step instead. 

This mechanism has been tested directly (the render script's output was verified to substitute correctly and leave ntfy's own `{{.title}}/{{.message}}` template syntax untouched), but the resulting config has not been tested against a live Grafana instance. After your stack is up, check Alerting → Alert rules to confirm the rule loaded, and use the Test button on the ntfy contact point (Alerting → Contact points) to confirm a notification actually arrives before relying on it. Until you have at least ~2 weeks of HRV history synced, expect this rule to sit in "No data" state — that's the intended, non-alerting behaviour, not a bug.

### 8. Set up wearable-events (optional)

Open `http://<your-host>:8081` and log in with the account from step 3. From there you can add calendar feeds (ICS URLs), keyword rules that tag calendar events, manual one-tap context tags (caffeine, alcohol, etc.), and a nightly subjective sleep score. Adding a second household member is available from the Manage tab — use the same username there as that person's GADGETBRIDGE_USER (see below) so their data lines up.

Developing or modifying this component? See `wearable-events/README.md` for the API reference, data model, auth internals, and how to run it locally without Docker.

## Multi-user notes

Each person gets their own ring and their own parser container instance
(set `GADGETBRIDGE_USER` differently per instance — see
[`.env.example`](./.env.example)). The wearable-events web app supports
multiple logins itself: use the *same* username for a person's
wearable-events account as their `GADGETBRIDGE_USER` value, so their
calendar/sleep-score data and their sensor data share the same `user` tag
in InfluxDB and can be correlated later. When adding a household member
from the Manage tab, the app will offer to pick their username from
already-synced ring data automatically (once any exists), rather than
risking a typo'd manual entry.

## Known limitations

- **Timestamp units and sleep-stage codes are best-effort.** The Colmi/
  Gadgetbridge integration isn't officially documented; two things in
  particular (`COLMI_TIMESTAMPS_ARE_MS` and the `SLEEP_STAGE_MAP` in the
  parser script) are reasonable defaults that should be verified against
  a few real nights of your own data. See the comments at each
  definition in
  [`colmi_gadgetbridge_to_influxdb/app/gadgetbridge_to_influxdb.py`](./colmi_gadgetbridge_to_influxdb/app/gadgetbridge_to_influxdb.py).
- **No password reset flow** in wearable-events. Losing a password means
  editing the SQLite file directly or recreating the account.
- **Reprocessing calendar events** after a keyword-rule change is bounded
  by what's been cached locally since that event was last synced — it
  can't resurrect classification for events that rolled off your
  calendar's ICS feed window before ever being seen by this stack.
- **Grafana alerting provisioning is unverified against a live instance**. The YAML files under grafana/provisioning/alerting/ were written against Grafana's documented schema but couldn't be tested against a running Grafana in the environment they were built in. Verify the rule loaded (Alerting → Alert rules) and the contact point actually delivers (Alerting → Contact points → Test) before relying on it — see step 7.
- **The HRV alert rule is single-user**. It watches one ALERT_HRV_USER value; a household with multiple people needs a copy of the rule block in rules.yaml per person, each with a different uid and user filter.

## License

The ring parser is a derivative of
[bentasker/gadgetbridge_to_influxdb](https://github.com/bentasker/gadgetbridge_to_influxdb)
(BSD 3-Clause).
See [`parser/README.md`](./colmi_gadgetbridge_to_influxdb/README.md)
for full attribution.