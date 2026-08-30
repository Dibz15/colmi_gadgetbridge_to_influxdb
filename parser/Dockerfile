# Updated Dockerfile for colmi_gadgetbridge_to_influxdb
#
# Original Dockerfile ran the script once and exited (designed for
# ephemeral/cron-triggered invocation, e.g. `docker run --rm ...` on a
# schedule). This version adds a loop wrapper so the same image can run
# as a persistent service in a docker-compose stack, re-syncing every
# SYNC_INTERVAL_SECONDS.
#
# The one-shot behaviour is still available if you want it: override the
# entrypoint/command back to `/app/gadgetbridge_to_influxdb.py` and drive
# it from an external cron/scheduler instead.

FROM python:slim-bullseye

RUN pip install --upgrade pip \
    && pip install webdavclient3 influxdb-client loguru

COPY app /app
COPY scripts /scripts
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /app/gadgetbridge_to_influxdb.py /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]