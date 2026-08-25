# Gadgetbridge to InfluxDB (Colmi fork)

Fetches a [Gadgetbridge](https://www.gadgetbridge.org/) database export from
a WebDAV server (e.g. Nextcloud) and writes biomarker data into
[InfluxDB](https://github.com/influxdata/influxdb) for dashboarding/alerting
in Grafana.

This repo is a fork of [bentasker/gadgetbridge_to_influxdb](https://github.com/bentasker/gadgetbridge_to_influxdb).
The original targets Huami/Amazfit devices; this fork adapted the
queries for **Colmi/Yawell smart rings** (R02/R03/R06/R09/R10/R11/R12
family). This fork also adds:

- A loop wrapper (`entrypoint.sh`) so the container runs as a persistent
  service on an interval, instead of one-shot-and-exit — suited to running
  in a long-lived `docker-compose` stack rather than being triggered by an
  external cron.
- A GitHub Actions workflow that builds and pushes a multi-arch
  (amd64/arm64) image to Docker Hub automatically on every push to `main`.

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
WebDAV target. In Gadgetbridge: **Settings → Data auto-export → Database**,
pointed at a local android directory
(e.g. `GadgetBridge/`). Then, from the Nextcloud app, set it to sync and upload this directory.

See the [Gadgetbridge wiki](https://codeberg.org/Freeyourgadget/Gadgetbridge/wiki/Data-Export-Import-Merging-Processing) for the general auto-export mechanics.

Note that each GadgetBridge export is a **full overwrite** of the database file, not an
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
| `QUERY_DURATION` | How far back (seconds) to query on each run | `86400` |
| `INFLUXDB_URL` | InfluxDB server URL | — (required) |
| `INFLUXDB_TOKEN` | InfluxDB API token (or `user:pass` on 1.x) | — (required) |
| `INFLUXDB_ORG` | InfluxDB org name/ID | — (required) |
| `INFLUXDB_BUCKET` | InfluxDB bucket to write into | — (required) |
| `INFLUXDB_MEASUREMENT` | InfluxDB measurement name | `gadgetbridge` |
| `SLEEP_HOURS` | Comma-separated hours (0–23) treated as sleeping hours, for stress-field averaging | `0,1,2,3,4,5,6` |
| `SYNC_INTERVAL_SECONDS` | **New in this fork.** Seconds between sync runs. Set to `0` to run once and exit (original upstream behaviour, for driving from an external cron instead) | `1800` |

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

`.github/workflows/docker-publish.yml` builds and pushes automatically on
every push to `main` (and on `v*.*.*` tags). To use it:

1. Create a Docker Hub repository, e.g. `colmi-gadgetbridge-to-influxdb`.
2. Generate a Docker Hub access token (Account Settings → Security).
3. In this repo's GitHub Settings → Secrets and variables → Actions, add:
   - `DOCKERHUB_USERNAME`
   - `DOCKERHUB_TOKEN`
4. Push to `main` — the workflow builds `linux/amd64` and `linux/arm64`
   images and pushes `:latest`, `:<git-sha>`, and (on tags) `:<semver>`.

To build locally instead:

```bash
docker build -t colmi-gadgetbridge-to-influxdb .
```

---

## Verifying your data

Two things worth checking once you have a few days of real data flowing,
since they weren't confirmed against Gadgetbridge's source at the time of
this fork:

1. **Timestamp units** — if dates in Grafana look wrong (1970, or far in
   the future), the `COLMI_*` tables may use millisecond rather than
   second timestamps in your Gadgetbridge version; adjust the timestamp
   handling in `app/gadgetbridge_to_influxdb.py` accordingly.
2. **Sleep stage codes** — `COLMI_SLEEP_STAGE_SAMPLE.STAGE` integer values
   aren't documented publicly; cross-check a night you remember clearly
   against what lands in InfluxDB to confirm which integer maps to
   light/deep/REM/awake.

---

## License

Copyright (c) 2023 B Tasker (original), with modifications by Dibz15 (Colmi
table adaptation) and this fork (loop wrapper, CI). Released under the
[BSD 3-Clause License](https://www.bentasker.co.uk/pages/licenses/bsd-3-clause.html).