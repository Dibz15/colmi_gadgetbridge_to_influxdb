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

# How far back in time should we query when extracting stats?
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

# COLMI_* tables store TIMESTAMP as unix seconds (matching the
# generic AbstractActivitySample-derived tables in upstream
# Gadgetbridge, e.g. MI_BAND_ACTIVITY_SAMPLE), NOT milliseconds
# like some of the HUAMI_* tables. Verify this against your own
# export if values look wrong (e.g. dates in the far future) -
# flip to True if it turns out your build stores ms instead.
COLMI_TIMESTAMPS_ARE_MS = os.getenv("COLMI_TIMESTAMPS_ARE_MS", "N") == "Y"

# Best-effort mapping of COLMI_SLEEP_STAGE_SAMPLE.STAGE values.
# This has NOT been verified against Gadgetbridge source and may
# not match your build/firmware. Treat as a starting point - cross
# check a night of known sleep against these labels and adjust.
# Unknown values fall through as "stage_<n>" so nothing is silently
# dropped while you calibrate this.
SLEEP_STAGE_MAP = {
    1: "light",
    2: "deep",
    3: "rem",
    4: "awake",
}

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


def extract_data(cur):
    ''' Query the database for data
    '''
    results = []
    devices = {}
    devices_observed = {}

    query_start_bound = int(time.time()) - QUERY_DURATION
    query_start_bound_scaled = query_start_bound * 1000 if COLMI_TIMESTAMPS_ARE_MS else query_start_bound

    logger.debug(
        f"Querying from {query_start_bound_scaled} "
        f"({'ms' if COLMI_TIMESTAMPS_ARE_MS else 's'} epoch, "
        f"QUERY_DURATION={QUERY_DURATION}s)"
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
    # NOTE: get_sleep_data() isn't shown in the snippet you pasted, so it's
    # left as-is here. Apply the same pattern to it if it isn't already
    # resilient: wrap its own COLMI_SLEEP_SESSION_SAMPLE / COLMI_SLEEP_STAGE_SAMPLE
    # queries in try/except sqlite3.OperationalError (or route them through
    # _run_query above), and log a warning rather than letting a missing/
    # malformed sleep table take down the whole extraction.
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
                    fields["sleep_session_duration_s"] = r[2] - r[0]
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
                row = {
                    "timestamp": row_ts,
                    "fields" : {
                        "sleep_stage_duration_s" : r[2],
                        f"{stage_label}_sleep_duration_s" : r[2]
                    },
                    "tags" : {
                        **device_tags(r[1]),
                        "sample_type" : "sleep_stage",
                        "sleep_stage" : stage_label,
                        "sleep_stage_raw" : r[3]
                    }
                }
                results.append(row)
            except (IndexError, KeyError) as e:
                logger.warning(f'Row {r} parsing error: {e}')
                continue

    return results


def write_results(results):
    ''' Open a connection to InfluxDB and write the results in
    '''
    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as _client:  # noqa: SIM117
        with _client.write_api() as _write_client:
            # Iterate through the results generating and writing points
            for row in results:
                p = Point(INFLUXDB_MEASUREMENT)
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
                _write_client.write(INFLUXDB_BUCKET, INFLUXDB_ORG, p)


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

    # Extract data from the DB
    results = extract_data(cur)
    if not results:
        logger.error("Data extraction failed")
        sys.exit(1)

    # Write out to InfluxDB
    write_results(results)

    # Tidy up
    conn.close()
    if tempdir not in ["/", ""]:
        if REMOVE_TEMP_DB == "N":
            logger.debug(tempdir)
        else:
            shutil.rmtree(tempdir)