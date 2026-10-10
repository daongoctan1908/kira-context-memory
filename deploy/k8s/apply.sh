#!/bin/sh
# Run this only from the reviewed, digest-bound YAML directory on a cluster-access host.
set -eu
KIRA_MANIFEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KIRA_CONTEXT=${KIRA_KUBE_CONTEXT:-default}
KIRA_NAMESPACE=kira-context-memory
KIRA_ACTION=${1:-apply}
KIRA_PRIVATE_SECRETS=${2:-}

kube() { kubectl --context="$KIRA_CONTEXT" -n "$KIRA_NAMESPACE" "$@"; }
fail() { echo "$*" >&2; exit 1; }
for KIRA_FILE in postgres.yaml runtime.yaml jobs/migrate.yaml jobs/memory-init.yaml jobs/memory-validate.yaml; do
    if grep -q 'REPLACE_[A-Z]*_IMAGE' "$KIRA_MANIFEST_DIR/$KIRA_FILE"; then
        fail 'Bind the three registry.vlp.vn image digests before applying these templates.'
    fi
done

wait_job() {
    KIRA_JOB=$1
    KIRA_TRIES=0
    while [ "$KIRA_TRIES" -lt 180 ]; do
        KIRA_JOB_STATE=$(kube get job "$KIRA_JOB" -o jsonpath='{.status.succeeded}{"|"}{.status.failed}{"|"}{.status.conditions[?(@.type=="Failed")].status}')
        KIRA_SUCCEEDED=${KIRA_JOB_STATE%%|*}
        KIRA_REMAINDER=${KIRA_JOB_STATE#*|}
        KIRA_FAILED=${KIRA_REMAINDER%%|*}
        KIRA_FAILURE_CONDITION=${KIRA_REMAINDER#*|}
        [ "$KIRA_FAILURE_CONDITION" = True ] && fail "Job $KIRA_JOB failed; inspect it before continuing."
        [ "${KIRA_FAILED:-0}" -gt 0 ] && fail "Job $KIRA_JOB failed; inspect it before continuing."
        [ "${KIRA_SUCCEEDED:-0}" = 1 ] && return 0
        KIRA_TRIES=$((KIRA_TRIES + 1))
        sleep 5
    done
    fail "Timed out waiting for Job $KIRA_JOB; runtime workloads were not applied."
}

case "$KIRA_ACTION" in
    validate)
        for KIRA_FILE in namespace.yaml config.yaml postgres-pvc.yaml networkpolicy.yaml postgres.yaml jobs/migrate.yaml jobs/memory-init.yaml jobs/memory-validate.yaml runtime.yaml; do
            kube apply --dry-run=client -f "$KIRA_MANIFEST_DIR/$KIRA_FILE"
        done
        exit 0
        ;;
    status)
        kube get pods,services,jobs,pvc
        exit 0
        ;;
    apply) ;;
    *) fail 'Usage: sh apply.sh [validate|apply|status] [private-secrets.yaml]' ;;
esac

kubectl --context="$KIRA_CONTEXT" apply -f "$KIRA_MANIFEST_DIR/namespace.yaml"
if [ -n "$KIRA_PRIVATE_SECRETS" ]; then
    kube apply -f "$KIRA_PRIVATE_SECRETS"
fi
for KIRA_SECRET in postgres-bootstrap kira-database kira-database-admin kira-service registry-vlp; do
    kube get secret "$KIRA_SECRET" -o name >/dev/null
done
kube apply -f "$KIRA_MANIFEST_DIR/config.yaml"
kube apply -f "$KIRA_MANIFEST_DIR/networkpolicy.yaml"
kube apply -f "$KIRA_MANIFEST_DIR/postgres-pvc.yaml"
kube apply -f "$KIRA_MANIFEST_DIR/postgres.yaml"
kube rollout status statefulset/postgres --timeout=300s

for KIRA_JOB in migrate memory-init memory-validate; do
    kube apply -f "$KIRA_MANIFEST_DIR/jobs/$KIRA_JOB.yaml"
    wait_job "$KIRA_JOB"
done
kube apply -f "$KIRA_MANIFEST_DIR/runtime.yaml"
for KIRA_DEPLOYMENT in worker gateway frontend; do
    kube rollout status "deployment/$KIRA_DEPLOYMENT" --timeout=600s
done
kube get pods,services,jobs,pvc
echo 'Internal rollout completed. Create the first app account and run the documented smoke checks.'
