#!/usr/bin/env python3
"""
One-off maintenance script: corrects sleep_session_duration_s values
already written to InfluxDB before the ms-to-seconds conversion fix in
gadgetbridge_to_influxdb.py (see raw_duration_to_seconds()).

Why this is needed: the buggy version stored a raw millisecond
difference (WAKEUP_TIME - TIMESTAMP) directly into a field labelled
"_s" (seconds). An 8.5-hour sleep session was stored as 30,600,000
instead of 30,600 - Grafana's "s" unit formatter then displayed it as
"50.8 weeks". The parser code is fixed going forward, but checkpoint-
based sync (see the README's "Checkpointed sync" section) won't
naturally revisit and correct sessions that were already written
before the fix - this script does that correction directly.

Unlike the sleep_stage cleanup script, this is a WRITE-based fix, not a
delete: sleep_session_duration_s is a FIELD (not a tag), so the
"point identity" (measurement+tags+timestamp) hasn't changed between
the wrong value and the corrected one - overwriting with the corrected
value at the same point is enough, no deletion needed.

IMPORTANT: the corrected value is written as an int, not a float.
InfluxDB locks a field's type on first write - this field was
originally written as an integer (plain int subtraction in the buggy
code), and a later write of a Python float to that same field name is
a hard type conflict InfluxDB rejects outright (422 Unprocessable
Entity), not something it silently coerces. Python's `/` always
returns float even for exact division, so this uses round() instead
to guarantee an int.

Usage:
    docker cp colmi_gadgetbridge_to_influxdb/scripts/fix_sleep_session_duration.py biomarker-parser:/tmp/fix.py
    docker exec -it biomarker-parser python3 /tmp/fix.py

Safe to run multiple times - a session whose value already looks
correct (under REASONABLE_MAX_SECONDS) is left untouched.
"""
import os

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "gadgetbridge")
GADGETBRIDGE_USER = os.getenv("GADGETBRIDGE_USER", "primary")

# Wide time range - sweeps up all history regardless of how far back
# your synced data goes.
START = "2020-01-01T00:00:00Z"
STOP = "2100-01-01T00:00:00Z"

# Any session reporting longer than this is almost certainly still
# affected by the bug (a real sleep session won't be 24h+) - used to
# decide which points need correcting vs are already fine.
REASONABLE_MAX_SECONDS = 24 * 3600


def main():
    if not all([INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET]):
        print("ERROR: INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, and INFLUXDB_BUCKET must all be set.")
        return

    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as client:
        query_api = client.query_api()

        flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {START}, stop: {STOP})
          |> filter(fn: (r) => r._measurement == "{INFLUXDB_MEASUREMENT}")
          |> filter(fn: (r) => r.sample_type == "sleep_session")
          |> filter(fn: (r) => r.user == "{GADGETBRIDGE_USER}")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''

        try:
            tables = query_api.query(flux)
        except Exception as e:
            print(f"ERROR querying existing sleep sessions: {e}")
            return

        rows = [record.values for table in tables for record in table.records]
        print(f"Found {len(rows)} sleep_session point(s) total.")

        affected = [r for r in rows if r.get("sleep_session_duration_s", 0) > REASONABLE_MAX_SECONDS]
        print(f"{len(affected)} appear affected by the bug (duration > {REASONABLE_MAX_SECONDS}s).")

        if not affected:
            print("Nothing to fix.")
            return

        write_api = client.write_api(write_options=SYNCHRONOUS)
        for r in affected:
            old_value = r["sleep_session_duration_s"]
            new_value = round(old_value / 1000)  # int, not float - see note below
            timestamp = r["_time"]

            print(f"  {timestamp}: {old_value:.0f} -> {new_value} ({new_value/3600:.2f}h)")

            p = Point(INFLUXDB_MEASUREMENT)
            for tag_key in ("device", "identifier", "alias", "user", "sample_type"):
                if tag_key in r:
                    p = p.tag(tag_key, r[tag_key])
            p = p.field("sleep_session_duration_s", new_value)
            p = p.time(timestamp)
            write_api.write(INFLUXDB_BUCKET, INFLUXDB_ORG, p)

        print()
        print(f"Corrected {len(affected)} point(s). Refresh Grafana - the duration should now show real hours.")


if __name__ == "__main__":
    main()