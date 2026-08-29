#!/bin/sh
# Renders grafana/provisioning-templates/*.template files with envsubst
# into /output (a Docker volume Grafana then mounts read-only at
# /etc/grafana/provisioning). Runs once, in the grafana-provisioning-init
# service, before Grafana itself starts.
#
# Exists because Grafana's alert-rule/contact-point provisioning does
# NOT support $ENV_VAR interpolation inside the `model`/`settings` blocks
# the way datasource provisioning does - confirmed by a real error
# ("could not find bucket $INFLUXDB_BUCKET") when that was assumed to
# work. This script is the fix: render everything ourselves before
# Grafana ever reads it.
#
# Generic over subdirectories under /templates - datasources/, alerting/,
# dashboards/ are all handled the same way, so adding a new category
# (another dashboard, say) needs no changes here, just a new subfolder.

set -eu

apk add --no-cache gettext >/dev/null

# Restricting envsubst to exactly this list (rather than substituting
# every variable in the environment) means nothing else in these files -
# like ntfy's own {{.title}}/{{.message}} template syntax, or Grafana's
# own v.timeRangeStart/v.windowPeriod Flux macros in dashboard JSON -
# can be accidentally mangled.
VARS='$INFLUX_URL $INFLUXDB_ORG $INFLUXDB_BUCKET $INFLUXDB_TOKEN $INFLUXDB_MEASUREMENT $ALERT_HRV_USER $NTFY_TOPIC $NTFY_USER $NTFY_PASSWORD $NTFY_PRIORITY'

for dir in /templates/*/; do
    category=$(basename "$dir")
    mkdir -p "/output/$category"
    for f in "$dir"*.template; do
        [ -e "$f" ] || continue  # no .template files in this category - skip rather than error on the literal glob
        out="/output/$category/$(basename "$f" .template)"
        envsubst "$VARS" < "$f" > "$out"
        echo "Rendered $out"
    done
done

echo "Grafana provisioning templates rendered."