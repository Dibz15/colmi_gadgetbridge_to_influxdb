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

from webdav3.client import Client
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

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
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "gadgetbridge")

# How far back in time should we query when extracting stats?
QUERY_DURATION = int(os.getenv("QUERY_DURATION", 86400))

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
    if EXPORT_FILE in file_list:
        info = webdav_client.info(f'{WEBDAV_PATH}/{EXPORT_FILE}')
    else:
        print("Error: Export file does not exist")
        sys.exit(1)

    # Create a temporary directory to operate from
    tempdir = tempfile.mkdtemp()

    # Download the file
    webdav_client.download_sync(remote_path=f'{WEBDAV_PATH}/{EXPORT_FILE}', local_path=f'{tempdir}/gadgetbridge.sqlite')

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


def extract_data(cur):
    ''' Query the database for data
    '''
    results = []
    devices = {}
    devices_observed = {}

    query_start_bound = int(time.time()) - QUERY_DURATION
    query_start_bound_scaled = query_start_bound * 1000 if COLMI_TIMESTAMPS_ARE_MS else query_start_bound

    # Pull out device names
    device_query = "select _id, NAME, IDENTIFIER, ALIAS from DEVICE"
    try:
        res = cur.execute(device_query)
    except sqlite3.OperationalError as e:
        # We received an empty db
        print("Unable to fetch stats - received an empty database")
        return False

    for r in res.fetchall():
        devices[f"dev-{r[0]}"] = {
            "name" : r[1],
            "identifier" : r[2],
            "alias" : "Unset" if r[3] is None else r[3]
        }

    def device_tags(device_id):
        d = devices[f"dev-{device_id}"]
        return {
            "device" : d['name'],
            "identifier" : d['identifier'],
            "alias" : d['alias']
        }

    def note_observed(device_id, row_ts):
        key = f"dev-{device_id}"
        if key not in devices_observed or devices_observed[key] < row_ts:
            devices_observed[key] = row_ts

    # --- Heart rate (continuous samples) ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, HEART_RATE FROM COLMI_HEART_RATE_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        row = {
            "timestamp": row_ts,
            "fields" : {
                "heart_rate" : r[2]
            },
            "tags" : {
                **device_tags(r[1]),
                "sample_type" : "heart_rate"
            }
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- SpO2 ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, SPO2 FROM COLMI_SPO2_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        row = {
            "timestamp": row_ts,
            "fields" : {
                "spo2" : r[2]
            },
            "tags" : device_tags(r[1])
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- Stress ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, STRESS FROM COLMI_STRESS_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        fields = {"stress" : r[2]}

        # Mirror upstream's sleep-hour exclusion so alerting can
        # ignore/weight overnight stress readings differently
        sample_epoch_s = r[0] / 1000 if COLMI_TIMESTAMPS_ARE_MS else r[0]
        if str(time.gmtime(sample_epoch_s).tm_hour) not in SLEEP_HOURS:
            fields["stress_exc_sleep"] = r[2]

        row = {
            "timestamp": row_ts,
            "fields" : fields,
            "tags" : device_tags(r[1])
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- HRV, per-reading values ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, VALUE FROM COLMI_HRV_VALUE_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        row = {
            "timestamp": row_ts,
            "fields" : {
                "hrv" : r[2]
            },
            "tags" : device_tags(r[1])
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- HRV summary/baseline (this is the closest thing to a
    # "readiness"-style computed score the ring/app produces) ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, WEEKLY_AVERAGE, LAST_NIGHT_AVERAGE, "
                  "LAST_NIGHT5_MIN_HIGH, BASELINE_LOW_UPPER, BASELINE_BALANCED_LOWER, "
                  "BASELINE_BALANCED_UPPER, STATUS_NUM FROM COLMI_HRV_SUMMARY_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        fields = {}
        # Only set fields that aren't NULL
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
            continue

        row = {
            "timestamp": row_ts,
            "fields" : fields,
            "tags" : {**device_tags(r[1]), "sample_type" : "hrv_summary"}
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- Temperature ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, TEMPERATURE, TEMPERATURE_TYPE, "
                  "TEMPERATURE_LOCATION FROM COLMI_TEMPERATURE_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        row = {
            "timestamp": row_ts,
            "fields" : {
                "temperature" : r[2]
            },
            "tags" : {
                **device_tags(r[1]),
                # Raw type/location codes - meaning not confirmed against
                # Gadgetbridge source, kept as tags so you can filter/
                # compare once you've worked out what each value means
                "temperature_type" : r[3],
                "temperature_location" : r[4]
            }
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- Activity (steps/distance/calories/HR, bucketed by RAW_KIND) ---
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, DISTANCE, "
                  "CALORIES FROM COLMI_ACTIVITY_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
        row_ts = to_nanos(r[0])
        row = {
            "timestamp": row_ts,
            "fields" : {
                "steps" : r[3],
                "heart_rate" : r[4],
                "distance" : r[5],
                "calories" : r[6],
            },
            "tags" : {
                **device_tags(r[1]),
                "activity_kind" : r[2],
                "sample_type" : "activity"
            }
        }
        results.append(row)
        note_observed(r[1], row_ts)

    # --- Sleep sessions + stages ---
    results += get_sleep_data(cur, device_tags, query_start_bound_scaled)

    # Create a field to record when we last synced, based on the values in devices_observed
    now = time.time_ns()
    for device_key, row_ts in devices_observed.items():
        device_id = device_key.replace("dev-", "")
        row_age = now - row_ts
        row = {
            "timestamp": now,
            "fields" : {
                "last_seen" : row_ts,
                "last_seen_age" : row_age
            },
            "tags" : {
                **device_tags(device_id),
                "sample_type" : "sync_check"
            }
        }
        results.append(row)

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
    res = cur.execute(data_query)
    for r in res.fetchall():
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

    # Stages
    data_query = ("SELECT TIMESTAMP, DEVICE_ID, DURATION, STAGE FROM COLMI_SLEEP_STAGE_SAMPLE "
                  f"WHERE TIMESTAMP >= {query_start_bound_scaled} "
                  "ORDER BY TIMESTAMP ASC")
    res = cur.execute(data_query)
    for r in res.fetchall():
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

    return results


def write_results(results):
    ''' Open a connection to InfluxDB and write the results in
    '''
    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as _client:
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
        print("Error: WEBDAV_URL not set in environment")
        sys.exit(1)

    if not INFLUXDB_URL:
        print("Error: INFLUXDB_URL not set in environment")
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
        print("Data extraction failed")
        sys.exit(1)

    # Write out to InfluxDB
    write_results(results)

    # Tidy up
    conn.close()
    if tempdir not in ["/", ""]:
        if REMOVE_TEMP_DB == "N":
            print(tempdir)
        else:
            shutil.rmtree(tempdir)