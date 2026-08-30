# Gadgetbridge to InfluxDB (Colmi fork)

Fetches a [Gadgetbridge](https://www.gadgetbridge.org/) database export from
a WebDAV server (e.g. Nextcloud) and writes biomarker data into
[InfluxDB](https://github.com/influxdata/influxdb) for dashboarding/alerting
in Grafana.

This repo is a fork of [Dibz15/colmi_gadgetbridge_to_influxdb](https://github.com/Dibz15/colmi_gadgetbridge_to_influxdb),
itself forked from [bentasker/gadgetbridge_to_influxdb](https://github.com/bentasker/gadgetbridge_to_influxdb).
The original targets Huami/Amazfit devices; the Dibz15 fork adapted the
queries for **Colmi/Yawell smart rings** (R02/R03/R06/R09/R10/R11/R12
family). This fork adds:

- A loop wrapper (`entrypoint.sh`) so the container runs as a persistent
  service on an interval, instead of one-shot-and-exit — suited to running
  in a long-lived `docker-compose` stack rather than being triggered by an
  external cron.
- This directory lives inside a monorepo alongside `wearable-events/` -
  see the top-level `README.md` for the CI setup, which builds and pushes
  both images from one workflow at `.github/workflows/docker-publish.yml`.

---

## How it fits together

```
Colmi ring (BLE)
   │
   ▼
Gadgetbridge (Android, periodic auto-export)
   │  WebDAV
   ▼
Nextcloud
   │  WebDAV (pulled by this container, on a loop)
   ▼
InfluxDB  ──▶  Grafana (dashboards, alert rules)
                  │
                  ▼
                ntfy (push notifications)
```

---

## Gadgetbridge configuration

Gadgetbridge needs to be set to periodically auto-export its database to a
WebDAV target. In Gadgetbridge: **Settings → Data auto-export → WebDAV**,
pointed at your Nextcloud instance's WebDAV endpoint
(`https://<nextcloud-domain>/remote.php/dav/`), into a dedicated folder
(e.g. `GadgetBridge/`). See the [Gadgetbridge wiki](https://codeberg.org/Freeyourgadget/Gadgetbridge/wiki/Data-Export-Import-Merging-Processing)
for the general auto-export mechanics.

Each export is a **full overwrite** of the database file, not an
incremental diff — this container re-reads the whole file each run and
relies on InfluxDB's identical-timestamp-and-tags dedup to avoid
duplicating points, so re-processing the same file repeatedly is harmless.

---

## Configuration (environment variables)

| Variable | Description | Default |
|---|---|---|
| `WEBDAV_URL` | WebDAV server URL. For Nextcloud: `https://<domain>/remote.php/dav/` | — (required) |
| `WEBDAV_USER` | WebDAV username | — (required) |
| `WEBDAV_PASS` | WebDAV password (use a Nextcloud **app password**, not your login password) | — (required) |
| `WEBDAV_PATH` | Path to the export directory on the WebDAV server, e.g. `files/<nextcloud_user>/GadgetBridge/` | — (required) |
| `EXPORT_FILENAME` | Filename of the export on the WebDAV server | `gadgetbridge` |
| `QUERY_DURATION` | How far back (seconds) to query on the **first run only** - see [Checkpointed sync](#checkpointed-sync) below | `86400` |
| `MAX_CATCHUP_SECONDS` | Safety cap on catch-up distance if the last checkpoint is very old | `2592000` (30 days) |
| `CHECKPOINT_OVERLAP_SECONDS` | Overlap subtracted from the checkpoint before resuming, to avoid missing a boundary sample | `300` (5 min) |
| `INFLUXDB_URL` | InfluxDB server URL | — (required) |
| `INFLUXDB_TOKEN` | InfluxDB API token (or `user:pass` on 1.x) | — (required) |
| `INFLUXDB_ORG` | InfluxDB org name/ID | — (required) |
| `INFLUXDB_BUCKET` | InfluxDB bucket to write into | — (required) |
| `INFLUXDB_MEASUREMENT` | InfluxDB measurement name | `gadgetbridge` |
| `SLEEP_HOURS` | Comma-separated hours (0–23) treated as sleeping hours, for stress-field averaging | `0,1,2,3,4,5,6` |
| `SYNC_INTERVAL_SECONDS` | **New in this fork.** Seconds between sync runs. Set to `0` to run once and exit (original upstream behaviour, for driving from an external cron instead) | `1800` |

> Field/table names above match upstream's documented set. Since Gadgetbridge's
> Colmi tables (`COLMI_HEART_RATE_SAMPLE`, `COLMI_HRV_VALUE_SAMPLE`,
> `COLMI_HRV_SUMMARY_SAMPLE`, `COLMI_SPO2_SAMPLE`, `COLMI_STRESS_SAMPLE`,
> `COLMI_TEMPERATURE_SAMPLE`, `COLMI_SLEEP_SESSION_SAMPLE`,
> `COLMI_SLEEP_STAGE_SAMPLE`, `COLMI_ACTIVITY_SAMPLE`) differ from the
> Huami ones this env-var list was originally written against, double check
> the actual query logic in `app/gadgetbridge_to_influxdb.py` in this repo
> against your own exported `.db` schema (`sqlite3 gadgetbridge.sqlite
> .schema`) if any expected metric is missing from InfluxDB after a sync —
> table/column names can drift slightly between Gadgetbridge versions.

---

## Checkpointed sync

Each run doesn't just blindly re-query the last `QUERY_DURATION` seconds
every time. Instead it checks InfluxDB for the most recent `last_seen`
value from its own `sync_check` points (written every run as a
sync-health marker) and resumes from there:

- **No checkpoint found** (first run ever) — falls back to
  `QUERY_DURATION`, same as the original fixed-window behaviour.
- **Checkpoint recent** (normal operation) — resumes from just past the
  checkpoint, which is usually *narrower* than `QUERY_DURATION` would
  be, cutting down on redundant re-writes of unchanged historical data.
- **Checkpoint old** (container was down for a while) — resumes from
  the checkpoint even if that's *further back* than `QUERY_DURATION`,
  so a gap from downtime actually gets backfilled instead of silently
  lost. Clamped to `MAX_CATCHUP_SECONDS` so a very old or corrupted
  checkpoint can't trigger an unbounded historical resync.

This relies on the `sync_check`/`last_seen` point already being written
every run — if you're running a much older build of this script that
predates it, the checkpoint lookup will find nothing and fall back to
`QUERY_DURATION` correctly, it just won't have gap-filling behaviour
until it's had at least one successful run to establish a checkpoint.

---

## Running

### Via Docker Hub image (recommended)

```bash
docker run -d --name colmi-parser \
  -e WEBDAV_URL=https://nextcloud.example.invalid/remote.php/dav/ \
  -e WEBDAV_USER=youruser \
  -e WEBDAV_PASS=yourapppassword \
  -e WEBDAV_PATH=files/youruser/GadgetBridge/ \
  -e INFLUXDB_URL=http://influxdb:8086 \
  -e INFLUXDB_TOKEN=yourtoken \
  -e INFLUXDB_ORG=home \
  -e INFLUXDB_BUCKET=health \
  -e SYNC_INTERVAL_SECONDS=1800 \
  yourdockerhubuser/colmi-gadgetbridge-to-influxdb:latest
```

Or as part of the full `docker-compose.yml` stack (InfluxDB + Grafana +
ntfy + this parser) — see that file for the complete setup.

### Running once, from an external cron

```bash
docker run --rm \
  -e SYNC_INTERVAL_SECONDS=0 \
  -e WEBDAV_URL=... \
  ... \
  yourdockerhubuser/colmi-gadgetbridge-to-influxdb:latest
```

### Running directly (no container)

```bash
pip install webdavclient3 influxdb-client
# export the env vars above
./app/gadgetbridge_to_influxdb.py
```

---

## Building and publishing your own image

This directory is one of two images built by the monorepo workflow at
`.github/workflows/docker-publish.yml` (repo root) — it builds and
pushes automatically on every push to `main` that touches this
directory (and on `v*.*.*` tags). To use it:

1. Create a Docker Hub repository, e.g. `colmi-gadgetbridge-to-influxdb`.
2. Generate a Docker Hub access token (Account Settings → Security).
3. In the repo's GitHub Settings → Secrets and variables → Actions, add:
   - `DOCKERHUB_USERNAME`
   - `DOCKERHUB_TOKEN`
4. Push to `main` — the workflow detects the change under
   `colmi_gadgetbridge_to_influxdb/`, builds `linux/amd64` and
   `linux/arm64` images, and pushes `:latest`, `:<git-sha>`, and (on
   tags) `:<semver>`. Pushing a `wearable-events`-only change does not
   trigger a rebuild of this image, and vice versa.

To build locally instead:

```bash
docker build -t colmi-gadgetbridge-to-influxdb .
```

---

## Verifying your data

1. **Timestamp units — CONFIRMED.** Real R09 hardware data (Aug 2026)
   proved `COLMI_*` tables store `TIMESTAMP` in **milliseconds**, not
   seconds — the opposite of the original guess. `COLMI_TIMESTAMPS_ARE_MS`
   now defaults to `Y`. Evidence: InfluxDB rejected writes with a 22-digit
   timestamp ("value out of range"), which is exactly what you get from
   multiplying an already-millisecond value by `1e9` instead of `1e6`.
   If your own device/build turns out to differ (dates land implausibly
   far in the future in Grafana), flip this back to `N`.
2. **Sleep stage codes** — `COLMI_SLEEP_STAGE_SAMPLE.STAGE` integer values
   still aren't independently confirmed; cross-check a night you remember
   clearly against what lands in InfluxDB to confirm which integer maps to
   light/deep/REM/awake.

---

## License

Copyright (c) 2023 B Tasker (original), with modifications by Dibz15 (Colmi
table adaptation) and this fork (loop wrapper, CI). Released under the
[BSD 3-Clause License](https://www.bentasker.co.uk/pages/licenses/bsd-3-clause.html).