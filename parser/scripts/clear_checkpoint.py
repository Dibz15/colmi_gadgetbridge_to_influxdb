#!/usr/bin/env python3
"""
One-off script: deletes all sync_check checkpoint points for a user,
forcing the next parser sync to fall back to QUERY_DURATION instead of
resuming from wherever the checkpoint currently points.

This does NOT by itself trigger a full-history resync - QUERY_DURATION
is the "no checkpoint found" fallback bound, and it defaults to just
86400s (1 day), meant for bootstrapping a brand-new install, not for
"resync everything". To actually recover older data, set
QUERY_DURATION (in your .env, passed through to the parser container)
to something wide enough to cover however far back you need BEFORE
restarting the parser after running this script.

Note this affects every sensor type, not just sleep stages - the next
sync will re-extract and re-write HR, SpO2, stress, HRV, temperature,
activity, and sleep data across the whole widened window. This is
harmless (InfluxDB overwrites identical points rather than
duplicating), just means the next sync will take noticeably longer and
write far more points than a normal 15-30min incremental sync.

Usage:
    docker cp colmi_gadgetbridge_to_influxdb/scripts/clear_checkpoint.py biomarker-parser:/tmp/clear_checkpoint.py
    docker exec -it biomarker-parser python3 /tmp/clear_checkpoint.py
    # then, in .env: temporarily set QUERY_DURATION to cover the
    # history you need, e.g. 1209600 for 14 days, and restart the
    # parser container.
"""
import os

from influxdb_client import InfluxDBClient

INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "gadgetbridge")
GADGETBRIDGE_USER = os.getenv("GADGETBRIDGE_USER", "primary")

START = "2020-01-01T00:00:00Z"
STOP = "2100-01-01T00:00:00Z"


def main():
    if not all([INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET]):
        print("ERROR: INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, and INFLUXDB_BUCKET must all be set.")
        return

    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as client:
        delete_api = client.delete_api()
        # Key names quoted defensively (not just values) - InfluxDB's
        # delete predicate parser treats some bare words as reserved
        # (confirmed the hard way with "tag" earlier in this project).
        predicate = (
            f'_measurement="{INFLUXDB_MEASUREMENT}" AND '
            f'"sample_type"="sync_check" AND '
            f'"user"="{GADGETBRIDGE_USER}"'
        )
        print(f"Deleting all sync_check checkpoint points for user={GADGETBRIDGE_USER!r}...")
        delete_api.delete(START, STOP, predicate, bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG)
        print("Done. The next sync will find no checkpoint and fall back to QUERY_DURATION.")
        print()
        print("IMPORTANT: if you haven't already, set QUERY_DURATION in .env to cover")
        print("however far back you need to recover, then restart the parser container.")
        print("Otherwise the next sync will only catch up the default 1 day.")


if __name__ == "__main__":
    main()