#!/bin/sh
# Runs gadgetbridge_to_influxdb.py on a loop, sleeping SYNC_INTERVAL_SECONDS
# between runs. A failed run (e.g. Nextcloud temporarily unreachable, or
# Gadgetbridge hasn't exported yet) just gets retried next interval rather
# than crashing the container.
#
# Set SYNC_INTERVAL_SECONDS=0 to run once and exit (original behaviour).

INTERVAL="${SYNC_INTERVAL_SECONDS:-1800}"

if [ "$INTERVAL" = "0" ]; then
    exec /app/gadgetbridge_to_influxdb.py
fi

echo "Parser container starting. Sync interval: ${INTERVAL}s"

while true; do
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running sync..."
    /app/gadgetbridge_to_influxdb.py
    STATUS=$?
    if [ $STATUS -ne 0 ]; then
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Sync failed (exit $STATUS) - will retry next interval"
    else
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Sync complete"
    fi
    sleep "$INTERVAL"
done