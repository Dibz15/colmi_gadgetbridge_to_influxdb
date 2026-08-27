# Biomarker stack

Self-hosted pipeline: Colmi ring → Gadgetbridge → Nextcloud (WebDAV) →
InfluxDB → Grafana, plus a calendar-tagging/subjective-sleep companion
service (`wearable-events`) with its own login.

## Structure

```
.
├── docker-compose.yml              InfluxDB + Grafana + ntfy + parser + wearable-events
├── .env.example                    copy to .env, fill in real values
├── colmi_gadgetbridge_to_influxdb/ fork this onto github.com/Dibz15/colmi_gadgetbridge_to_influxdb
│   ├── app/gadgetbridge_to_influxdb.py   the actual parser script
│   ├── Dockerfile                  loop wrapper + loguru added on top of upstream
│   ├── entrypoint.sh
│   ├── .github/workflows/          builds + pushes to Docker Hub on push to main
│   └── README.md                   full details on this component
└── wearable-events/                calendar tagging + subjective sleep score, built locally by compose
    ├── app/                        FastAPI backend (auth, calendars, keyword rules, reprocessing)
    ├── static/                     the web UI (login, tags, sleep, calendars, manage tabs)
    └── schema.sql
```

## Setup order

1. **Push the parser image.** Fork `Dibz15/colmi_gadgetbridge_to_influxdb`
   on GitHub, replace `Dockerfile`/`entrypoint.sh` with the ones in
   `colmi_gadgetbridge_to_influxdb/` here, push `app/gadgetbridge_to_influxdb.py`
   too. Add `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` as repo secrets, push to
   `main` - the Action builds and pushes `yourdockerhubuser/colmi-gadgetbridge-to-influxdb:latest`.
   Full details in `colmi_gadgetbridge_to_influxdb/README.md`.

2. **Fill in `.env`.** Copy `.env.example` → `.env` next to
   `docker-compose.yml`. At minimum set: `PARSER_IMAGE` (from step 1),
   `INFLUXDB_TOKEN`/`INFLUXDB_INIT_ADMIN_TOKEN` (same value, your choice),
   the `WEBDAV_*` vars (Nextcloud app password, not your login password),
   and `WEARABLE_EVENTS_ADMIN_USERNAME`/`PASSWORD` for your first login.

3. **`docker compose up -d --build`** — builds `wearable-events` locally,
   pulls everything else.

4. **Configure Gadgetbridge** on your phone to auto-export to the
   Nextcloud WebDAV path matching `WEBDAV_PATH` in `.env`.

5. **Grafana**: open `:3000`, add InfluxDB as a data source
   (`http://influxdb:8086`, org/bucket/token from `.env`), build dashboards.

6. **wearable-events**: open `:8081`, log in with the bootstrap account
   from step 2. Add calendars and keyword rules from there. If you later
   add a second household member, use the same username there as that
   person's `GADGETBRIDGE_USER` so their data correlates - the "Add
   household member" form will offer to pick from already-synced ring
   data automatically once it exists, rather than typing it blind.

## Notes

- `GADGETBRIDGE_USER` (parser) and the wearable-events login username
  must match for one person's calendar/sleep-score data and their
  HR/HRV/temperature data to share the same `user` tag in InfluxDB.
- Two things flagged as unverified against real hardware until you have
  a few days of data: timestamp units (`COLMI_TIMESTAMPS_ARE_MS`) and the
  sleep-stage integer mapping (`SLEEP_STAGE_MAP` in the parser script) -
  see the comments at each definition.
