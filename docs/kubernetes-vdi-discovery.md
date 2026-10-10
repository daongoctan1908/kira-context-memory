# Remaining Kubernetes discovery from VDI

Received environment evidence is recorded in
[kubernetes-target-environment.md](kubernetes-target-environment.md). The operator can run the
following read-only commands in the existing VDI Kubernetes terminal. They were not executed
by this laptop session. Run one command at a time to keep output small enough to transcribe.
Retain `[?]` for unreadable characters; do not use uncertain names in deployment commands.

## Cluster access

The current context name `default` is already known. This prints its cluster reference and
namespace field without credential data. An empty namespace field means the context does not
set a namespace; it does not select an application namespace for the deployment.

```sh
kubectl config view --minify -o jsonpath='cluster={.contexts[0].context.cluster}{"\n"}namespace={.contexts[0].context.namespace}{"\n"}'
```

The user deferred domain/Ingress/TLS and browser access while arranging VDI connectivity.
Proceed with internal deployment and checks first; no Ingress inventory is required for
that stage. `gitlab-ce-nginx` was observed on GitLab Ingresses but does not select a class
for KiRa. When user access is resumed, inspect `kubectl get ingressclass` only if Ingress
is the chosen entry point.

## Database findings and storage inspection

The user's SQL probes confirmed PostgreSQL 16.4 in `vmlp-db` and PostgreSQL 15.4 in
`vlp-database-core`. Both returned zero rows for `vector` in `pg_available_extensions`.
Do not repeat those probes unless the server image/extension installation changes. A newly
created KiRa database needs a server with pgvector installed, such as a dedicated instance
using the repository's pinned PostgreSQL/pgvector image mirrored into the internal registry.

The user supplied the full StorageClass YAML and Longhorn Node inventory. The class
`longhorn-storage-2-retain` is confirmed as default with `numberOfReplicas=2`,
`staleReplicaTimeout=2880`, `Retain`, `Immediate`, and expansion enabled. Do not request
that YAML again unless the class changes. All three target Longhorn nodes were listed with
node-level scheduling allowed; long disk byte counts were supplied with a moire warning.

PVC `postgres-data` is prepared in [deploy/k8s/postgres-pvc.yaml](../deploy/k8s/postgres-pvc.yaml)
with an initial 20 GiB pilot request. Once the operator has applied it, inspect the binding:

```sh
kubectl get pvc postgres-data -n kira-context-memory
```

To check the Longhorn components scheduled on a target node, use the following compact query
and repeat with the known `.17` and `.18` node names. Pod readiness is only one part of storage
acceptance; Longhorn disk/replica capacity and a bound test PVC still need verification.

```sh
kubectl get pods -n longhorn-system --field-selector spec.nodeName=hkh9104-10-221-248-16 -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready'
```

For any further capacity transcription, inspect only the three target Longhorn nodes and
their Ready/Schedulable conditions plus actual disk scheduling settings. Avoid another full
cluster table of long byte counts. Do not conclude PVC/replica readiness from node-level
`allowScheduling` alone. After the database mounts the PVC, verify the created Longhorn
volume's replica count, actual health and write/read persistence separately.

## Selected application namespace

The user has cluster-admin access and confirmed that no KiRa project/namespace exists. Use the
new namespace `kira-context-memory`; creation is prepared in
[deploy/k8s/README.md](../deploy/k8s/README.md). A Rancher project is optional and can be added
later. After namespace creation, inspect its actual policies and existing account references
with the commands below. Existing tenant quotas do not apply merely because workloads share
nodes. Node label `project=lhvtt` does not identify a Rancher project ID.

```sh
kubectl get namespace kira-context-memory --show-labels
kubectl describe resourcequota,limitrange -n kira-context-memory
kubectl get networkpolicy -n kira-context-memory
kubectl get serviceaccounts -n kira-context-memory -o custom-columns='NAME:.metadata.name,PULL_SECRETS:.imagePullSecrets[*].name'
```

This captures namespace quota headroom, container defaults/bounds, network policies and existing
ServiceAccount image-pull Secret references. No Secret values are printed.

## Confirmed image handoff

The user confirmed image builds on the laptop, then transfer to the company PC, followed by
the PC's internal Drive channel into VDI. The release images are exported with identity/platform
metadata and SHA256 checksums. VDI imports and publishes the same images to `registry.vlp.vn`;
the PC/VDI stages do not rebuild them. Kubernetes then pulls immutable references from the
internal registry. This is the confirmed route, not evidence of completed builds/transfers.

The product bundle needs backend/frontend and, for the dedicated DB option, PostgreSQL/pgvector.
The separate [product helper](../scripts/deploy/offline_images.py) bundles all three product
images. The benchmark handoff script also supports an eval image and keeps its separate scope.
The laptop build/export and dependency access passed on 2026-10-08; see the
[build receipt](evidence/product-image-build-2026-10-08.json). Verify the VDI container CLI
when importing/publishing; do not infer VDI tooling from the Kubernetes node runtime.

## Remaining build and runtime bindings

Namespace creation can proceed using the supplied cluster-admin authority. Resource inventory
does not supply image repository access, build-machine capabilities or external dependency
credentials. Resolve:

- application paths under `registry.vlp.vn`, registry push/pull Secret references and VDI
  container import/publish tooling; build location and transfer route are already confirmed;
- pgvector-capable KiRa database provisioning, endpoint, roles, TLS and backup contract; existing
  PostgreSQL versions and the Longhorn StorageClass inventory are already recorded;
- KiRa Secret injection and authenticated application/SSE acceptance using the existing
  `.env.openai.local` KiRa keys; its endpoint, domain, username and Basic Auth are already available;
- application-specific rewrite/formation acceptance with the confirmed unauthenticated
  embedding service and shared Qwen model; provider selection and API-key requirements are resolved;
- observability bindings.

The VDI-to-application network route, browser HTTPS origin, Ingress class if used, certificate
and streaming timeouts are deferred to user access. Initial Services remain internal.
See [deploy/k8s/README.md](../deploy/k8s/README.md) for the internal smoke/production
configuration boundary. No direct VDI-to-node connection or local kubectl forwarding has
been verified by this session.

GitLab/Jenkins workloads were visible in `vlp-cicd`, but the user selected the laptop build route.
The CI/CD workload inventory is not evidence of permission to publish to the intended repository.
The empty Fleet GitRepo listing applies to the inspected context and snapshot; it does not
choose a deployment workflow or manifest format.
