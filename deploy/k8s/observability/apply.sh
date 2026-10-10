#!/bin/sh
# Run after the core application from the reviewed, digest-bound release directory.
set -eu
KIRA_OBS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KIRA_CONTEXT=${KIRA_KUBE_CONTEXT:-default}
KIRA_NAMESPACE=kira-context-memory
KIRA_ACTION=${1:-apply}
KIRA_PRIVATE_SECRETS=${2:-}
KIRA_MODE=${3:-}
if [ -z "$KIRA_MODE" ]; then
    if [ -f "$KIRA_OBS_DIR/mode" ]; then
        KIRA_MODE=$(cat "$KIRA_OBS_DIR/mode")
    else
        KIRA_MODE=standalone
    fi
fi
kube() { kubectl --context="$KIRA_CONTEXT" -n "$KIRA_NAMESPACE" "$@"; }
fail() { echo "$*" >&2; exit 1; }
case "$KIRA_MODE" in shared|standalone) ;; *) fail 'Mode must be shared or standalone.' ;; esac
KIRA_FILES='config.yaml networkpolicy.yaml datastores.yaml s3-init.yaml langfuse.yaml collector.yaml'
[ "$KIRA_MODE" = standalone ] && KIRA_FILES="$KIRA_FILES metrics.yaml"
for KIRA_FILE in $KIRA_FILES; do
    [ -f "$KIRA_OBS_DIR/$KIRA_FILE" ] || fail "Missing manifest: $KIRA_FILE"
    if grep -q 'REPLACE_[A-Z_]*_IMAGE' "$KIRA_OBS_DIR/$KIRA_FILE"; then
        fail 'Bind all selected observability images to published registry.vlp.vn digests first.'
    fi
done
case "$KIRA_ACTION" in
    validate)
        for KIRA_FILE in $KIRA_FILES; do
            kube apply --dry-run=client -f "$KIRA_OBS_DIR/$KIRA_FILE"
        done
        exit 0 ;;
    status) kube get pods,services,jobs,pvc; exit 0 ;;
    apply) ;;
    *) fail 'Usage: sh observability/apply.sh [apply|validate|status] [private-observability.yaml] [standalone|shared]' ;;
esac
# The core bootstrap creates this namespace, ServiceAccount and registry credential.
kube get serviceaccount kira-runtime -o name >/dev/null
kube get secret registry-vlp -o name >/dev/null
kube get deployment gateway worker -o name >/dev/null
[ -z "$KIRA_PRIVATE_SECRETS" ] || kube apply -f "$KIRA_PRIVATE_SECRETS"
kube get secret kira-observability -o name >/dev/null
kube apply -f "$KIRA_OBS_DIR/config.yaml"
kube apply -f "$KIRA_OBS_DIR/networkpolicy.yaml"
kube apply -f "$KIRA_OBS_DIR/datastores.yaml"
for KIRA_STORE in langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio; do
    kube rollout status "statefulset/$KIRA_STORE" --timeout=600s
done
kube apply -f "$KIRA_OBS_DIR/s3-init.yaml"
KIRA_TRIES=0
while [ "$KIRA_TRIES" -lt 180 ]; do
    KIRA_STATE=$(kube get job langfuse-s3-init -o jsonpath='{.status.succeeded}{"|"}{.status.failed}{"|"}{.status.conditions[?(@.type=="Failed")].status}')
    KIRA_SUCCEEDED=${KIRA_STATE%%|*}
    KIRA_REMAINDER=${KIRA_STATE#*|}
    KIRA_FAILED=${KIRA_REMAINDER%%|*}
    KIRA_FAILURE_CONDITION=${KIRA_REMAINDER#*|}
    [ "$KIRA_FAILURE_CONDITION" = True ] && fail 'Langfuse bucket initialization failed; telemetry stays at its previous setting.'
    [ "${KIRA_FAILED:-0}" -gt 0 ] && fail 'Langfuse bucket initialization failed; inspect Job logs.'
    [ "${KIRA_SUCCEEDED:-0}" = 1 ] && break
    KIRA_TRIES=$((KIRA_TRIES + 1))
    sleep 5
done
[ "$KIRA_TRIES" -lt 180 ] || fail 'Langfuse bucket initialization timed out.'
kube apply -f "$KIRA_OBS_DIR/langfuse.yaml"
for KIRA_DEPLOYMENT in langfuse-web langfuse-worker; do
    kube rollout status "deployment/$KIRA_DEPLOYMENT" --timeout=900s
done
kube apply -f "$KIRA_OBS_DIR/collector.yaml"
kube rollout status deployment/otel-collector --timeout=300s
if [ "$KIRA_MODE" = standalone ]; then
    kube apply -f "$KIRA_OBS_DIR/metrics.yaml"
    for KIRA_METRIC_STORE in prometheus grafana; do
        kube rollout status "statefulset/$KIRA_METRIC_STORE" --timeout=300s
    done
fi
# No Langfuse keys enter Gateway/Worker. Metrics and traces share this Collector endpoint.
kube patch configmap kira-runtime-config --type=merge -p '{"data":{"OTEL_ENABLED":"true","OTEL_EXPORTER_OTLP_ENDPOINT":"http://otel-collector:4318"}}'
for KIRA_DEPLOYMENT in gateway worker; do
    kube rollout restart "deployment/$KIRA_DEPLOYMENT"
    kube rollout status "deployment/$KIRA_DEPLOYMENT" --timeout=600s
done
kube get pods,services,jobs,pvc
echo 'Observability rollout completed. Verify real traces, metrics and fail-open behavior before acceptance.'
