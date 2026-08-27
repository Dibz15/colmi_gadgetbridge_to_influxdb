#!/bin/sh
# Runs gadgetbridge_to_influxdb.py on a loop, sleeping SYNC_INTERVAL_SECONDS
# between runs. A *transient* failed run (e.g. Nextcloud briefly
# unreachable, or Gadgetbridge hasn't exported yet) just gets retried next
# interval rather than crashing the container.
#
# A *config* error (a required env var missing) is different - it will
# never succeed on retry, so we validate up front and exit hard rather
# than looping forever pretending to be healthy. `docker ps` will then
# correctly show this container as Exited/Restarting instead of a
# misleading "Up" while it silently fails every cycle.
#
# Set SYNC_INTERVAL_SECONDS=0 to run once and exit (original behaviour).

REQUIRED_VARS="WEBDAV_URL WEBDAV_USER WEBDAV_PASS WEBDAV_PATH INFLUXDB_URL INFLUXDB_TOKEN INFLUXDB_ORG INFLUXDB_BUCKET"

check_required_vars() {
    missing=""
    for var in $REQUIRED_VARS; do
        # Indirect variable read (POSIX sh compatible)
        eval "val=\$$var"
        if [ -z "$val" ]; then
            missing="$missing $var"
        fi
    done

    if [ -n "$missing" ]; then
        echo "FATAL: required environment variable(s) not set:$missing"
        echo "This is a configuration error, not a transient failure - exiting rather than retrying."
        exit 1
    fi
}

check_required_vars

INTERVAL="${SYNC_INTERVAL_SECONDS:-1800}"

if [ "$INTERVAL" = "0" ]; then
    exec /app/gadgetbridge_to_influxdb.py
fi

echo "Parser container starting. Sync interval: ${INTERVAL}s"

# How many consecutive transient failures to tolerate before giving up and
# exiting hard. Prevents an indefinitely crash-looping-but-"Up" container
# if something's wrong beyond just the initial env check (e.g. WebDAV
# creds are set but wrong, InfluxDB token revoked, etc).
MAX_CONSECUTIVE_FAILURES="${MAX_CONSECUTIVE_FAILURES:-10}"
consecutive_failures=0

while true; do
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running sync..."
    /app/gadgetbridge_to_influxdb.py
    STATUS=$?
    if [ $STATUS -ne 0 ]; then
        consecutive_failures=$((consecutive_failures + 1))
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Sync failed (exit $STATUS) - failure ${consecutive_failures}/${MAX_CONSECUTIVE_FAILURES}"
        if [ "$consecutive_failures" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
            echo "FATAL: ${MAX_CONSECUTIVE_FAILURES} consecutive sync failures - exiting rather than retrying indefinitely. Check credentials/connectivity."
            exit 1
        fi
    else
        consecutive_failures=0
        echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Sync complete"
    fi
    sleep "$INTERVAL"
done

