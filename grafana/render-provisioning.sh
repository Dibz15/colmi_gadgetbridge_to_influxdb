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

set -eu

apk add --no-cache gettext >/dev/null

mkdir -p /output/datasources /output/alerting

# Restricting envsubst to exactly this list (rather than substituting
# every variable in the environment) means nothing else in these files -
# like ntfy's own {{.title}}/{{.message}} template syntax - can be
# accidentally mangled.
VARS='$INFLUX_URL $INFLUXDB_ORG $INFLUXDB_BUCKET $INFLUXDB_TOKEN $INFLUXDB_MEASUREMENT $ALERT_HRV_USER $NTFY_TOPIC $NTFY_USER $NTFY_PASSWORD $NTFY_PRIORITY'

for f in /templates/datasources/*.yaml.template; do
    out="/output/datasources/$(basename "$f" .template)"
    envsubst "$VARS" < "$f" > "$out"
    echo "Rendered $out"
done

for f in /templates/alerting/*.yaml.template; do
    out="/output/alerting/$(basename "$f" .template)"
    envsubst "$VARS" < "$f" > "$out"
    echo "Rendered $out"
done

echo "Grafana provisioning templates rendered."