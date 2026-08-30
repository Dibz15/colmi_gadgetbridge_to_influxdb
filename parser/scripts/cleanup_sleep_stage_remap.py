#!/usr/bin/env python3
"""
One-off maintenance script: deletes stale sleep_stage points left behind
after changing SLEEP_STAGE_MAP in gadgetbridge_to_influxdb.py.

Why this is needed: `sleep_stage` is written as an InfluxDB TAG, not a
field. Changing which raw integer maps to which label and re-syncing
does NOT overwrite previously-written points - a different tag value
for the same underlying timestamp creates an entirely separate series
that coexists alongside the old one, rather than replacing it. Only
brand-new samples synced going forward get the corrected mapping;
historical points already tagged under the old mapping are stuck with
the old (now wrong) label until explicitly deleted.

This deletes the WHOLE POINT for each stale series (not just the
sleep_stage tag), which also cleans up the parallel
`{stage}_sleep_duration_s` field naming (e.g. "deep_sleep_duration_s")
that lives on the same point - no separate cleanup needed for that.

Usage:
    Edit OLD_STAGE_MAP / NEW_STAGE_MAP below to match your actual
    before/after change (defaults here match the shipped default vs a
    common first recalibration - check they're right for your case
    before running). Then, with the same env vars your parser container
    already uses available:

        docker exec -it biomarker-parser python3 /scripts/cleanup_sleep_stage_remap.py

    or copy this file into the running container and run it there, or
    run it locally with INFLUXDB_URL etc. pointed at your instance.

Safe to run multiple times - a second run finds nothing left to delete.
Never touches points that already match the CURRENT mapping.
"""
import os

from influxdb_client import InfluxDBClient

INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "gadgetbridge")
GADGETBRIDGE_USER = os.getenv("GADGETBRIDGE_USER", "primary")

# EDIT THESE to match your actual before/after SLEEP_STAGE_MAP change.
# Defaults below match: shipped default (OLD) -> a full remapping (NEW).
OLD_STAGE_MAP = {1: "light", 2: "deep", 3: "rem", 4: "awake"}
NEW_STAGE_MAP = {2: "light", 3: "deep", 4: "rem", 1: "awake"}

# Wide time range so this sweeps up all history regardless of how far
# back your synced data goes - costs nothing extra if your actual
# history is short.
START = "2020-01-01T00:00:00Z"
STOP = "2100-01-01T00:00:00Z"


def main():
    if not all([INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET]):
        print("ERROR: INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, and INFLUXDB_BUCKET must all be set.")
        return

    stale_pairs = [
        (raw, old_label)
        for raw, old_label in OLD_STAGE_MAP.items()
        if NEW_STAGE_MAP.get(raw) != old_label
    ]

    if not stale_pairs:
        print("No stale (raw, label) pairs between OLD_STAGE_MAP and NEW_STAGE_MAP - nothing to do.")
        return

    print(f"Found {len(stale_pairs)} stale pair(s) to clean up: {stale_pairs}")
    print(f"Target: bucket={INFLUXDB_BUCKET!r} measurement={INFLUXDB_MEASUREMENT!r} user={GADGETBRIDGE_USER!r}")
    print()

    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as client:
        delete_api = client.delete_api()
        for raw, old_label in stale_pairs:
            # Keys quoted defensively (not just values) - InfluxDB's
            # delete predicate parser treats some bare words as
            # reserved (confirmed the hard way with "tag" in this
            # project's wearable-events component - see its README).
            predicate = (
                f'_measurement="{INFLUXDB_MEASUREMENT}" AND '
                f'"sample_type"="sleep_stage" AND '
                f'"sleep_stage"="{old_label}" AND '
                f'"sleep_stage_raw"="{raw}" AND '
                f'"user"="{GADGETBRIDGE_USER}"'
            )
            print(f"Deleting stale points: sleep_stage={old_label!r}, sleep_stage_raw={raw} ...")
            try:
                delete_api.delete(START, STOP, predicate, bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG)
                print("  done.")
            except Exception as e:
                print(f"  FAILED: {e}")

    print()
    print("Cleanup complete. Refresh your Grafana panel - the stale legend entries should be gone.")


if __name__ == "__main__":
    main()