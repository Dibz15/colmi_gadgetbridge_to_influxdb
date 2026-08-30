#!/usr/bin/env python3
#
#
# Fetch a Gadgetbridge database export from a WebDAV URL
# (in my case, Nextcloud) and then extract stats to write
# onwards into InfluxDB.
#
# This is a fork of bentasker/gadgetbridge_to_influxdb, adapted
# to read from the COLMI_* tables that Gadgetbridge populates for
# Colmi/Yawell smart rings (R02/R03/R06/R09/R10/R11/R12 family)
# instead of the HUAMI_* tables used for Amazfit/Mi Band devices.
#
# Original: https://github.com/bentasker/gadgetbridge_to_influxdb
# Copyright (c) 2023, B Tasker
# Colmi adaptation, 2026
# Released under BSD 3-clause
#
#
# pip install webdavclient3 influxdb-client

'''
Copyright 2023 B Tasker

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient, Point

# from influxdb_client.client.write_api import SYNCHRONOUS
from loguru import logger
from webdav3.client import Client

### Config section

# This expects hostname and scheme
#
# For nextcloud it'll be https://[nextcloud domain]/remote.php/dav/
WEBDAV_URL = os.getenv("WEBDAV_URL", False)

# Path to the export file
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")

# Creds
WEBDAV_USER = os.getenv("WEBDAV_USER", False)
WEBDAV_PASS = os.getenv("WEBDAV_PASS", False)

# What's the filename of the file on the webdav server?
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "Gadgetbridge.db")

# How far back in time should we query on the FIRST EVER run (before
# any checkpoint exists in InfluxDB)? After that, every subsequent run
# resumes from the last known checkpoint instead - see
# get_last_checkpoint_ns() - so this value stops mattering once a
# checkpoint exists, except as the fallback if checkpoint lookup fails.
QUERY_DURATION = int(os.getenv("QUERY_DURATION", "86400"))

# InfluxDB settings
INFLUXDB_URL = os.getenv("INFLUXDB_URL", False)
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "")
INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "gadgetbridge")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "testing_db")

# Which hours should be considered sleeping hours?
# (kept from upstream - used for the stress-excluding-sleep field)
SLEEP_HOURS = os.getenv("SLEEP_HOURS", "0,1,2,3,4,5,6").split(",")

# For testing/debugging only - if set to N, the copy of the sqlite
# db will be retained
REMOVE_TEMP_DB = os.getenv("REMOVE_TEMP_DB", "Y")

# Which human this data belongs to. Written as a tag on every point.
# Not used for anything else today (single-user setup, one parser
# instance) - this is a deliberate stop-over so that if this ever
# becomes a multi-user setup (e.g. a second ring/person added, or a
# second parser instance pointed at a different WebDAV export), each
# person's data is already disambiguated in InfluxDB by an explicit
# `user` tag rather than only by device serial - which is brittle
# (breaks if hardware is swapped/upgraded) and not self-documenting
# in dashboard/alert queries. Retrofitting this later would mean
# rewriting historical points' tags, which Influx doesn't support
# in-place - so it's cheap to add now, awkward to add after the fact.
GADGETBRIDGE_USER = os.getenv("GADGETBRIDGE_USER", "primary")

# CONFIRMED via real R09 hardware/Gadgetbridge export (Aug 2026, see
# repo history): COLMI_* tables store TIMESTAMP in MILLISECONDS, not
# seconds - the opposite of what was originally assumed by analogy to
# MI_BAND_ACTIVITY_SAMPLE. The evidence: InfluxDB rejected writes with
# "value out of range" on 22-digit timestamps, which is exactly what
# you get from multiplying an already-millisecond value by 1e9 (seconds
# -> nanos) instead of 1e6 (ms -> nanos). Y is now the verified default;
# only flip to N if your own export's data lands implausibly far in the
# future once graphed (a sign your device/build stores seconds instead).
COLMI_TIMESTAMPS_ARE_MS = os.getenv("COLMI_TIMESTAMPS_ARE_MS", "Y") == "Y"

# Best-effort mapping of COLMI_SLEEP_STAGE_SAMPLE.STAGE values.
# This has NOT been verified against Gadgetbridge source and may
# not match your build/firmware. Treat as a starting point - cross
# check a night of known sleep against these labels and adjust.
# Unknown values fall through as "stage_<n>" so nothing is silently
# dropped while you calibrate this.
SLEEP_STAGE_MAP = {
    2: "light",
    3: "deep",
    4: "rem",
    1: "awake",
}

# Safety cap on catch-up distance if the checkpoint turns out to be
# very old (e.g. the container was down for a while) - without this, a
# corrupted or ancient checkpoint could trigger an unbounded historical
# resync. Default 30 days; raise it if you need to backfill further
# after genuinely longer downtime.
MAX_CATCHUP_SECONDS = int(os.getenv("MAX_CATCHUP_SECONDS", str(30 * 86400)))

# Small overlap subtracted from the checkpoint before resuming, so a
# sample landing exactly at the boundary can't be missed due to a
# rounding/precision edge case. Re-querying a few already-processed
# rows is harmless - InfluxDB overwrites points with identical
# measurement+tags+timestamp rather than duplicating them.
CHECKPOINT_OVERLAP_SECONDS = int(os.getenv("CHECKPOINT_OVERLAP_SECONDS", "300"))

### Config ends


def fetch_database(webdav_client):
    ''' Connect to the WebDAV server and fetch the named database
    file, if it exists.
    '''
    file_list = webdav_client.list(WEBDAV_PATH)
    export_path = Path(WEBDAV_PATH)/EXPORT_FILE
    if EXPORT_FILE in file_list:
        _ = webdav_client.info(str(export_path))
    else:
        logger.error(f"Error: Export file {export_path} does not exist")
        sys.exit(1)

    # Create a temporary directory to operate from
    tempdir = Path(tempfile.mkdtemp())
    
    # Download the file
    webdav_client.download_sync(remote_path=str(export_path), local_path=str(tempdir / 'gadgetbridge.sqlite'))

    return tempdir


def open_database(tempdir):
    ''' Open a handle on the database
    '''
    conn = sqlite3.connect(f"{tempdir}/gadgetbridge.sqlite")
    cur = conn.cursor()
    return conn, cur


def to_nanos(ts):
    ''' Convert a COLMI_* TIMESTAMP value to nanoseconds for InfluxDB,
    honouring whichever unit COLMI_TIMESTAMPS_ARE_MS says the export uses.
    '''
    if COLMI_TIMESTAMPS_ARE_MS:
        return ts * 1000000
    return ts * 1000000000


def from_nanos(ns):
    ''' Inverse of to_nanos() - converts an InfluxDB-native nanosecond
    timestamp back down to whatever unit the local COLMI_* TIMESTAMP
    columns actually use (ms or s), honouring COLMI_TIMESTAMPS_ARE_MS
    the same way to_nanos does. Used to turn a checkpoint read back
    from InfluxDB (nanoseconds) into a bound usable in a SQLite query
    against the raw export (ms or s).
    '''
    if COLMI_TIMESTAMPS_ARE_MS:
        return ns // 1000000
    return ns // 1000000000


def raw_duration_to_seconds(raw_duration):
    ''' Converts a DIFFERENCE between two raw COLMI_* TIMESTAMP values
    (e.g. WAKEUP_TIME - TIMESTAMP) into actual seconds, honouring
    COLMI_TIMESTAMPS_ARE_MS. Distinct from to_nanos()/from_nanos()
    (which convert an absolute epoch timestamp) - a duration just needs
    dividing by the raw unit's scale, not a full nanosecond conversion.

    Returns an int, not a float - InfluxDB locks a field's type on
    first write (this field was originally written as an integer via
    plain int subtraction), and a later write of a float to the same
    field name is a hard type conflict InfluxDB rejects outright, not
    something it silently coerces. round() rather than // for slightly
    better accuracy if a duration isn't an exact multiple of 1000ms,
    while still guaranteeing an int return type either way.

    Bug this exists to fix: sleep_session_duration_s was previously
    computed as a raw millisecond difference (WAKEUP_TIME - TIMESTAMP,
    both ms) but stored/labelled as if it were already seconds -
    Grafana's "s" unit formatter then displayed an 8.5-hour sleep
    session as "50.8 weeks" (30,600,000 misread as 30,600,000 seconds
    instead of milliseconds).
    '''
    if COLMI_TIMESTAMPS_ARE_MS:
        return round(raw_duration / 1000)
    return raw_duration


def scaled_to_iso(ts_scaled) -> str:
    ''' Converts a "scaled" timestamp (whatever unit COLMI_TIMESTAMPS_ARE_MS
    says the local export/query bound uses - ms or s) into a readable
    UTC ISO 8601 string, purely for logging. Never used for the actual
    query itself, which stays in raw scaled units throughout.
    '''
    seconds = ts_scaled / 1000 if COLMI_TIMESTAMPS_ARE_MS else ts_scaled
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def get_last_checkpoint_ns(client) -> int | None:
    ''' Queries InfluxDB for the most recent `last_seen` value from our
    own sync_check points, so a run can resume from there instead of
    blindly re-querying "now - QUERY_DURATION" every single time.

    Searches a full year back so a checkpoint is found regardless of
    how long the container's been down - the actual catch-up distance
    is separately clamped by MAX_CATCHUP_SECONDS by the caller, once
    this returns, so searching widely here doesn't risk an unbounded
    resync on its own.

    Returns None if nothing is found (first run ever, or the query
    itself failed) - callers should fall back to the QUERY_DURATION-
    based bound in that case, exactly like before this feature existed.

    If multiple devices/series exist, returns the MINIMUM of their
    checkpoints - conservative, so we don't skip data for whichever
    device happens to be furthest behind. Re-querying an
    already-synced device's recent data is harmless (InfluxDB
    overwrites identical points rather than duplicating them).
    '''
    query_api = client.query_api()
    flux = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -365d)
      |> filter(fn: (r) => r._measurement == "{INFLUXDB_MEASUREMENT}")
      |> filter(fn: (r) => r.sample_type == "sync_check")
      |> filter(fn: (r) => r.user == "{GADGETBRIDGE_USER}")
      |> filter(fn: (r) => r._field == "last_seen")
      |> max()
    '''
    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Could not query last checkpoint (treating as first run): {e}")
        return None

    values = [int(record.get_value()) for table in tables for record in table.records]
    if not values:
        logger.debug("No prior checkpoint found - this looks like the first run")
        return None

    return min(values)


def _device_tags_factory(devices):
    ''' Returns a device_tags(device_id) closure that degrades gracefully
    (with a warning) instead of raising KeyError if a sample references a
    device_id that isn't in the DEVICE table - can happen with stale/
    orphaned rows after a device is unpaired/re-paired.
    '''
    warned_devices = set()

    def device_tags(device_id):
        key = f"dev-{device_id}"
        if key not in devices:
            if device_id not in warned_devices:
                logger.warning(
                    f"Sample references unknown DEVICE_ID={device_id} "
                    f"(not present in DEVICE table) - tagging as 'unknown'. "
                    f"This will only be logged once per device_id."
                )
                warned_devices.add(device_id)
            return {
                "device": "unknown",
                "identifier": "unknown",
                "alias": "unknown"
            }
        d = devices[key]
        return {
            "device": d['name'],
            "identifier": d['identifier'],
            "alias": d['alias']
        }

    return device_tags


def _run_query(cur, table_name, query) -> list|None:
    ''' Execute a query against one of the COLMI_* tables, tolerating the
    table not existing (older/newer Gadgetbridge versions or firmware
    revisions may not populate every table - e.g. a ring without a
    temperature sensor won't have meaningful COLMI_TEMPERATURE_SAMPLE
    rows, and some builds may not have the table at all).

    Returns a list of rows (possibly empty), or None if the query failed.
    '''
    try:
        res = cur.execute(query)
        rows = res.fetchall()
        logger.debug(f"{table_name}: query returned {len(rows)} row(s)")
        return rows
    except sqlite3.OperationalError as e:
        logger.warning(f"{table_name}: query failed ({e}) - skipping this table")
        return None


def extract_data(cur, client):
    ''' Query the database for data
    '''
    results = []
    devices = {}
    devices_observed = {}

    now_seconds = int(time.time())
    fallback_bound_seconds = now_seconds - QUERY_DURATION

    checkpoint_ns = get_last_checkpoint_ns(client)

    if checkpoint_ns is not None:
        # Convert the checkpoint (nanoseconds, InfluxDB-native) down to
        # raw-sqlite-column units (ms or s) directly, then subtract the
        # overlap margin - both done in "scaled" units throughout so
        # there's no unit-mixing between this path and the fallback path.
        checkpoint_scaled = from_nanos(checkpoint_ns)
        unit_multiplier = 1000 if COLMI_TIMESTAMPS_ARE_MS else 1
        overlap_scaled = CHECKPOINT_OVERLAP_SECONDS * unit_multiplier
        resume_bound_scaled = checkpoint_scaled - overlap_scaled

        min_allowed_scaled = (now_seconds - MAX_CATCHUP_SECONDS) * unit_multiplier

        if resume_bound_scaled < min_allowed_scaled:
            logger.warning(
                f"Checkpoint ({scaled_to_iso(checkpoint_scaled)}) is older than "
                f"MAX_CATCHUP_SECONDS ({MAX_CATCHUP_SECONDS}s) - clamping catch-up "
                f"window to {scaled_to_iso(min_allowed_scaled)} rather than resyncing "
                f"the full gap. Increase MAX_CATCHUP_SECONDS if you need to backfill further."
            )
            query_start_bound_scaled = min_allowed_scaled
        else:
            query_start_bound_scaled = resume_bound_scaled

        logger.info(
            f"Resuming from checkpoint at {scaled_to_iso(checkpoint_scaled)} "
            f"(last synced sample) - querying from {scaled_to_iso(query_start_bound_scaled)} "
            f"after subtracting a {CHECKPOINT_OVERLAP_SECONDS}s overlap margin"
        )
    else:
        query_start_bound_scaled = fallback_bound_seconds * 1000 if COLMI_TIMESTAMPS_ARE_MS else fallback_bound_seconds
        logger.info(
            f"No checkpoint found - using QUERY_DURATION fallback ({QUERY_DURATION}s), "
            f"querying from {scaled_to_iso(query_start_bound_scaled)}"
        )

    logger.debug(
        f"Querying from {query_start_bound_scaled} "
        f"({'ms' if COLMI_TIMESTAMPS_ARE_MS else 's'} epoch, "
        f"{scaled_to_iso(query_start_bound_scaled)} UTC)"
    )

    # Pull out device names
    device_query = "select _id, NAME, IDENTIFIER, ALIAS from DEVICE"
    device_rows = _run_query(cur, "DEVICE", device_query)
    if device_rows is None:
        # DEVICE missing/unreadable is fatal - without it we can't tag
        # anything, and it usually means we've been handed an empty or
        # corrupt export.
        logger.error("Unable to fetch stats - DEVICE table missing or unreadable (empty/corrupt database export?)")
        return False

    if not device_rows:
        logger.warning("DEVICE table returned zero rows - export may be from before initial pairing completed")

    for r in device_rows:
        devices[f"dev-{r[0]}"] = {
            "name": r[1],
            "identifier": r[2],
            "alias": "Unset" if r[3] is None else r[3]
        }
    logger.info(f"Found {len(devices)} device(s) in export: "
                f"{[d['name'] for d in devices.values()]}")

    device_tags = _device_tags_factory(devices)

    def note_observed(device_id, row_ts):
        key = f"dev-{device_id}"
        if key not in devices_observed or devices_observed[key] < row_ts:
            devices_observed[key] = row_ts

    # Table -> (select columns, row-building function) so each section
    # gets consistent logging/error handling without repeating boilerplate.
    section_counts = {}

    # --- Heart rate (continuous samples) ---
    rows = _run_query(cur, "COLMI_HEART_RATE_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, HEART_RATE FROM COLMI_HEART_RATE_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"heart_rate": r[2]},
                "tags": {**device_tags(r[1]), "sample_type": "heart_rate"}
            })
            note_observed(r[1], row_ts)
        section_counts["heart_rate"] = len(rows)

    # --- SpO2 ---
    rows = _run_query(cur, "COLMI_SPO2_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, SPO2 FROM COLMI_SPO2_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"spo2": r[2]},
                "tags": device_tags(r[1])
            })
            note_observed(r[1], row_ts)
        section_counts["spo2"] = len(rows)

    # --- Stress ---
    rows = _run_query(cur, "COLMI_STRESS_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, STRESS FROM COLMI_STRESS_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        skipped_stress = 0
        for r in rows:
            row_ts = to_nanos(r[0])
            fields = {"stress": r[2]}

            # Mirror upstream's sleep-hour exclusion so alerting can
            # ignore/weight overnight stress readings differently
            sample_epoch_s = r[0] / 1000 if COLMI_TIMESTAMPS_ARE_MS else r[0]
            try:
                sample_hour = time.gmtime(sample_epoch_s).tm_hour
                if str(sample_hour) not in SLEEP_HOURS:
                    fields["stress_exc_sleep"] = r[2]
            except (OverflowError, OSError, ValueError) as e:
                # A corrupt/out-of-range timestamp shouldn't take down the
                # whole sync - drop the sleep-exclusion field for this row
                # and keep going.
                skipped_stress += 1
                logger.debug(f"COLMI_STRESS_SAMPLE: could not compute hour-of-day "
                             f"for timestamp {r[0]} ({e}) - stress_exc_sleep omitted for this row")

            results.append({
                "timestamp": row_ts,
                "fields": fields,
                "tags": device_tags(r[1])
            })
            note_observed(r[1], row_ts)
        section_counts["stress"] = len(rows)
        if skipped_stress:
            logger.warning(f"COLMI_STRESS_SAMPLE: {skipped_stress} row(s) had unparseable timestamps for sleep-hour exclusion")

    # --- HRV, per-reading values ---
    rows = _run_query(cur, "COLMI_HRV_VALUE_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, VALUE FROM COLMI_HRV_VALUE_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"hrv": r[2]},
                "tags": device_tags(r[1])
            })
            note_observed(r[1], row_ts)
        section_counts["hrv_value"] = len(rows)

    # --- HRV summary/baseline (closest thing to a "readiness"-style
    # computed score the ring/app produces) ---
    rows = _run_query(cur, "COLMI_HRV_SUMMARY_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, WEEKLY_AVERAGE, LAST_NIGHT_AVERAGE, "
        "LAST_NIGHT5_MIN_HIGH, BASELINE_LOW_UPPER, BASELINE_BALANCED_LOWER, "
        "BASELINE_BALANCED_UPPER, STATUS_NUM FROM COLMI_HRV_SUMMARY_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        empty_summaries = 0
        for r in rows:
            row_ts = to_nanos(r[0])
            fields = {}
            for name, val in (
                ("hrv_weekly_average", r[2]),
                ("hrv_last_night_average", r[3]),
                ("hrv_last_night_5min_high", r[4]),
                ("hrv_baseline_low_upper", r[5]),
                ("hrv_baseline_balanced_lower", r[6]),
                ("hrv_baseline_balanced_upper", r[7]),
                ("hrv_status_num", r[8]),
            ):
                if val is not None:
                    fields[name] = val

            if not fields:
                empty_summaries += 1
                continue

            results.append({
                "timestamp": row_ts,
                "fields": fields,
                "tags": {**device_tags(r[1]), "sample_type": "hrv_summary"}
            })
            note_observed(r[1], row_ts)
        section_counts["hrv_summary"] = len(rows) - empty_summaries
        if empty_summaries:
            logger.debug(f"COLMI_HRV_SUMMARY_SAMPLE: {empty_summaries} row(s) were entirely NULL - skipped")

    # --- Temperature ---
    rows = _run_query(cur, "COLMI_TEMPERATURE_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, TEMPERATURE, TEMPERATURE_TYPE, "
        "TEMPERATURE_LOCATION FROM COLMI_TEMPERATURE_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"temperature": r[2]},
                "tags": {
                    **device_tags(r[1]),
                    "temperature_type": r[3],
                    "temperature_location": r[4]
                }
            })
            note_observed(r[1], row_ts)
        section_counts["temperature"] = len(rows)

    # --- Activity (steps/distance/calories/HR, bucketed by RAW_KIND) ---
    rows = _run_query(cur, "COLMI_ACTIVITY_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, DISTANCE, "
        "CALORIES FROM COLMI_ACTIVITY_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {
                    "steps": r[3],
                    "heart_rate": r[4],
                    "distance": r[5],
                    "calories": r[6],
                },
                "tags": {
                    **device_tags(r[1]),
                    "activity_kind": r[2],
                    "sample_type": "activity"
                }
            })
            note_observed(r[1], row_ts)
        section_counts["activity"] = len(rows)

    # --- Sleep sessions + stages ---
    sleep_rows = get_sleep_data(cur, device_tags, query_start_bound_scaled)
    results += sleep_rows
    section_counts["sleep"] = len(sleep_rows)

    # Create a field to record when we last synced, based on the values in devices_observed
    now = time.time_ns()
    if not devices_observed:
        logger.warning("No samples observed for any device in this window - "
                        "check the ring has synced recently and QUERY_DURATION covers the gap")

    for device_key, row_ts in devices_observed.items():
        device_id = device_key.replace("dev-", "")
        row_age = now - row_ts
        row_age_hours = row_age / 1_000_000_000 / 3600
        if row_age_hours > 24:
            logger.warning(f"Device {devices.get(device_key, {}).get('name', device_key)}: "
                           f"last sample is {row_age_hours:.1f}h old")
        results.append({
            "timestamp": now,
            "fields": {
                "last_seen": row_ts,
                "last_seen_age": row_age
            },
            "tags": {
                **device_tags(device_id),
                "sample_type": "sync_check"
            }
        })

    logger.info(f"Extraction summary: {section_counts} | total points to write: {len(results)}")

    return results


def get_sleep_data(cur, device_tags, query_start_bound_scaled):
    ''' Fetch sleep session + stage data from the COLMI sleep tables.

    COLMI_SLEEP_SESSION_SAMPLE gives one row per night (TIMESTAMP = sleep
    onset, WAKEUP_TIME = when they woke). COLMI_SLEEP_STAGE_SAMPLE gives
    per-stage segments within that (TIMESTAMP, DURATION, STAGE).
    '''
    results = []

    # Sessions
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, WAKEUP_TIME FROM COLMI_SLEEP_SESSION_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    
    rows = _run_query(cur, "COLMI_SLEEP_SESSION_SAMPLE", data_query)
    
    if rows:
        for r in rows:
            try:
                row_ts = to_nanos(r[0])
                fields = {"sleep_session_start" : r[0]}
                if r[2] is not None:
                    fields["sleep_session_wakeup"] = r[2]
                    fields["sleep_session_duration_s"] = raw_duration_to_seconds(r[2] - r[0])
                row = {
                    "timestamp": row_ts,
                    "fields" : fields,
                    "tags" : {**device_tags(r[1]), "sample_type" : "sleep_session"}
                }
                results.append(row)
            except (IndexError, KeyError) as e:
                logger.warning(f'Row {r} parsing error: {e}')
                continue

    # Stages
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, DURATION, STAGE FROM COLMI_SLEEP_STAGE_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    rows = _run_query(cur, "COLMI_SLEEP_STAGE_SAMPLE", data_query)
    if rows:
        for r in rows:
            try:
                row_ts = to_nanos(r[0])
                stage_label = SLEEP_STAGE_MAP.get(r[3], f"stage_{r[3]}")
                common_tags = {
                    **device_tags(r[1]),
                    "sample_type": "sleep_stage",
                    "sleep_stage": stage_label,
                    "sleep_stage_raw": r[3]
                }

                # Start marker - existing duration fields, plus a
                # sleep_stage_active state field (1=active) for the
                # Grafana Sleep Stage Timeline panel. Without an
                # explicit "this segment ends here" signal, Grafana's
                # State Timeline panel just connects consecutive points
                # in the same (stage) series - meaning a stage that
                # recurs a few times a night rendered as one solid
                # block from its first to its last occurrence, silently
                # swallowing every other stage's blocks in between.
                results.append({
                    "timestamp": row_ts,
                    "fields": {
                        "sleep_stage_duration_s": r[2],
                        f"{stage_label}_sleep_duration_s": r[2],
                        "sleep_stage_active": 1,
                    },
                    "tags": common_tags,
                })

                # End marker - same series (identical tags) so it's
                # interpreted as "this series' value changed" by
                # State Timeline, ending the block at the right place
                # instead of extending it to this stage's next
                # occurrence. Reuses to_nanos() to convert the raw
                # DURATION value the same way TIMESTAMP is converted -
                # this assumes DURATION uses the same raw unit
                # convention as TIMESTAMP (confirmed ms), which hasn't
                # been independently verified the way TIMESTAMP's unit
                # was. If blocks in the rendered panel look implausible
                # (real sleep stage segments typically run 10-90+ min),
                # that's the signal this assumption is wrong.
                end_ts = row_ts + to_nanos(r[2])
                results.append({
                    "timestamp": end_ts,
                    "fields": {
                        "sleep_stage_active": 0,
                    },
                    "tags": common_tags,
                })
            except (IndexError, KeyError) as e:
                logger.warning(f'Row {r} parsing error: {e}')
                continue

    return results


def write_results(results, client):
    ''' Write results to InfluxDB using the given (already-open) client -
    shared with the checkpoint lookup in extract_data() rather than
    each opening its own connection.
    '''
    logger.debug(f"Writing {len(results)} point(s) tagged user={GADGETBRIDGE_USER!r}")
    write_failures = 0
    with client.write_api() as _write_client:
        # Iterate through the results generating and writing points
        for row in results:
            p = Point(INFLUXDB_MEASUREMENT)

            # Applied here, once, rather than threaded through every
            # section above - guarantees every point gets a `user` tag
            # with no risk of a section being missed. See the
            # GADGETBRIDGE_USER comment in the config section for why
            # this exists even in a single-user setup.
            p = p.tag("user", GADGETBRIDGE_USER)

            for tag in row['tags']:
                p = p.tag(tag, row['tags'][tag])

            for field in row['fields']:
                if row['fields'][field] == -1:
                    continue

                # Skip any special heart_rate values (upstream noted
                # these show up as sentinel/error values on Huami gear;
                # kept as a safety net here too)
                if field == "heart_rate" and row['fields'][field] is not None and row['fields'][field] > 253:
                    continue

                if row['fields'][field] is None:
                    continue

                p = p.field(field, row['fields'][field])

            p = p.time(row['timestamp'])

            # A single point's write can fail for reasons unrelated to
            # every other point - most notably an InfluxDB field-type
            # conflict (a field's type is locked on first write; a
            # later point sending a different Python type for the same
            # field name, e.g. float vs the field's established int, is
            # rejected outright, not coerced). Without this try/except,
            # one such conflict crashes the whole sync run and no
            # further points get written at all - logging and
            # continuing means the rest of this run's data still lands.
            try:
                _write_client.write(INFLUXDB_BUCKET, INFLUXDB_ORG, p)
            except Exception as e:
                write_failures += 1
                logger.warning(f"Failed to write point (tags={row['tags']}, timestamp={row['timestamp']}): {e}")

    if write_failures:
        logger.warning(f"{write_failures} of {len(results)} point(s) failed to write this run - see warnings above for details")


if __name__ == "__main__":
    if not WEBDAV_URL:
        logger.error("WEBDAV_URL not set in environment")
        sys.exit(1)

    if not INFLUXDB_URL:
        logger.error("INFLUXDB_URL not set in environment")
        sys.exit(1)

    webdav_options = {
        "webdav_hostname" : WEBDAV_URL,
        "webdav_login" : WEBDAV_USER,
        "webdav_password" : WEBDAV_PASS
    }

    webdav_client = Client(webdav_options)
    tempdir = fetch_database(webdav_client)
    conn, cur = open_database(tempdir)

    # One InfluxDB client, shared between the checkpoint lookup
    # (extract_data) and the write (write_results) - opened once here
    # rather than each opening its own connection.
    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as influx_client:
        results = extract_data(cur, influx_client)
        if not results:
            logger.error("Data extraction failed")
            sys.exit(1)

        write_results(results, influx_client)

    # Tidy up
    conn.close()
    if tempdir not in ["/", ""]:
        if REMOVE_TEMP_DB == "N":
            logger.debug(tempdir)
        else:
            shutil.rmtree(tempdir)