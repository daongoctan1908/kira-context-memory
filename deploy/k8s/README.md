# Kubernetes deployment sources

The reviewable core-application YAML for the first internal rollout is in this directory.
Start with [DEPLOY.md](DEPLOY.md) for image/Secret binding, ordered deployment, smoke checks
and later HTTPS access. `apply.sh` waits for database migration, memory initialization and
runtime validation before applying the application. Do not recursively apply this directory:
it includes placeholders, Secret examples and optional resources.

The [observability sources](observability/README.md) include Collector, Langfuse web/worker and
its four data stores. Langfuse is selected for AI traces. Metrics use one Prometheus/Grafana pair:
choose the standalone YAML or verified platform monitoring, with no second ingestion path.
Gateway/Worker now record OTel metrics only; their legacy `/metrics` routes are removed.
Centralized logs/Loki remain deferred. Core bootstrap starts with `OTEL_ENABLED=false`; the
ordered observability helper enables export after the backends are ready.
See [DEPLOY.md](DEPLOY.md) and the updated [stack audit](../../docs/observability-stack-audit-2026-10-09.md).

The image transfer produced earlier is a frozen artifact. These new deployment sources
remain separate files for review; they have not been added to that archive or applied to K8s.
Rebuild the backend from the current source before deploying the OTel-only change; the earlier
2026-10-08 backend image still contains the removed legacy registry.

The user confirmed on 2026-10-08 that no KiRa project or namespace exists and that they have
cluster-admin access. Use the new namespace `kira-context-memory` for this deployment.
The initial bootstrap uses raw Kubernetes YAML. A Rancher project association can be added
later for group permissions/quotas; it is not a prerequisite for namespace creation.

## Offline image and manifest handoff

The product image helper is now available at
[scripts/deploy/offline_images.py](../../scripts/deploy/offline_images.py). From the repository
root on the laptop, with the project Python environment and a running Linux Docker engine:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.offline_images build --bundle artifacts/deployment/k8s-next-release
```

The helper freezes the current backend/frontend build inputs, builds both application images
with `--platform linux/amd64`, pulls the pinned PostgreSQL/pgvector image for that platform,
checks image architecture/non-root application users, runs isolated container import/config
checks, and exports all three images. It records the base Git revision and an inventory/hash
of the exact source snapshot, including uncommitted source changes. No commit or registry
publish is required to build this working image set.

The output directory's `transfer` subdirectory contains the three image archives,
`image-manifest.json`, `source-manifest.json`, `SHA256SUMS.txt`, non-secret namespace/PVC/provider
fragments and [handoff instructions](../../scripts/deploy/OFFLINE-IMAGES.md). Transfer that
entire subdirectory. Build contexts remain in the parent directory on the laptop. Verify
on the PC/VDI before import:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.offline_images verify --bundle artifacts/deployment/k8s-20261008/transfer
```

Verification needs Python 3.11+ and no Docker engine. The handoff also documents Linux
`sha256sum -c SHA256SUMS.txt` and Docker import/publish commands when those tools are present
on VDI. The helper does not publish to the registry or execute application/benchmark acceptance.
An eval image remains part of the separate benchmark workflow when requested.

### Build completed on 2026-10-08

The laptop built and exported the product images to
`artifacts/deployment/k8s-20261008/transfer`: backend 114.30 MiB, frontend 25.18 MiB and
PostgreSQL/pgvector 148.98 MiB, total 288.46 MiB. All three report `linux/amd64`; application
images use their non-root users. Backend imports/version, frontend Nginx configuration and
HTTP health/HTML, and actual PostgreSQL `CREATE EXTENSION vector`/vector SQL checks passed.
The dependency reports PostgreSQL 16.15 and pgvector 0.8.6. SHA256 verification passed.

[The sanitized build receipt](../../docs/evidence/product-image-build-2026-10-08.json) records
image IDs, portable config digests, archive hashes/sizes and the frozen source hash. The
source snapshot includes the frontend label change and is based on Git revision
`6eb9adc0ff54219e22bf003b588b6a578a26699f`; it is not a clean-commit build. The helper's four
integrity/architecture tests and Ruff checks passed. Image transfer, registry publish,
cluster deployment and real application/provider acceptance remain unexecuted.
Use a fresh bundle directory, such as the example above, for another build.

The confirmed route is laptop -> company PC -> the PC's internal Drive channel -> VDI.
Build `linux/amd64` application images once on the laptop. Export backend/frontend and the
PostgreSQL/pgvector dependency for the dedicated DB option with image identity metadata and
SHA256 checksums; include the eval image when needed. Transfer the deployment manifests
through the same route and keep credentials in the deployment Secret mechanism.

VDI verifies/imports the transferred images and publishes them to the actual repository paths
under `registry.vlp.vn`. The workloads use the resulting immutable digests. Repository paths,
registry push/pull authentication and the VDI container CLI remain unresolved. The helper's
availability does not establish a completed build/export; inspect its actual output and status.
The existing benchmark handoff tooling still does not include the frontend in its bundle;
the product helper above provides that separate complete product image set.

## Existing KiRa configuration

KiRa's endpoint `http://10.255.62.64:8122`, domain `VBI`, username and Basic Auth are already
available in the local `.env.openai.local`. Reuse only `KIRA_BASE_URL`, `KIRA_DOMAIN`,
`KIRA_USERNAME` and `KIRA_BASIC_AUTH` for the deployment; the rest of that file contains local
provider configuration. Inject credentials through the deployment Secret mechanism and keep
their values out of the image/manifest transfer bundle. Secret injection and authenticated
KiRa/SSE acceptance are still to be executed in K8s.

## Confirmed AI provider bindings

Use [production-rewrite.env.example](../production-rewrite.env.example) for Gateway rewrite
and [production-memory.env.example](../production-memory.env.example) for memory providers.
The user selected `/models/Qwen3_14B` at `http://10.254.135.40:8080/v1` for both rewrite and
formation. Embedding uses `Qwen3-Embedding-0.6B` at `http://10.254.135.40:8001/v1`, dimension
1024. The user confirmed successful inference without API keys; omit all three optional
AI API-key variables. Merge these fragments with the complete runtime configuration and
database Secrets, then verify application rewrite/formation during rollout.

## Internal deployment before VDI browser access

The user deferred domain/Ingress/TLS while arranging the connection from VDI to a selected
target node. Build/transfer images and prepare the database, initialization Jobs and internal
application Services first. Use `ClusterIP` Services for this stage. Check readiness,
initialization and actual provider/application behavior from within the cluster; VDI browser
access is a later acceptance step.

Production Gateway settings require an HTTPS origin and secure cookies. For authenticated
HTTP smoke checks before TLS is available, use an isolated internal test configuration with
`APP_ENVIRONMENT=test`, `AUTH_ENABLED=true`, `DEV_STATIC_IDENTITY_ENABLED=false`,
`AUTH_COOKIE_SECURE=false`, and the actual internal `AUTH_ALLOWED_ORIGIN`. This is a test
deployment in the target cluster. Before user access, switch to production settings and
validate HTTPS browser/session/SSE behavior. No application validation has been weakened.

When the VDI route is ready, select the frontend entry point and its exact destination/port.
A frontend NodePort is an option if approved; the frontend proxies API calls to Gateway.
Local [kubectl port-forward](https://kubernetes.io/docs/tasks/access-application-cluster/port-forward-access-application-cluster/)
is an optional debugging route if the VDI itself has working kubectl/API access and forwarding
support. The Rancher web terminal alone does not establish those local capabilities.
No entry point, NodePort, TLS resource or network connection has been created by this session.

## Namespace bootstrap

From a workstation that has this checkout and the correct cluster access:

```sh
kubectl --context=default apply -f deploy/k8s/namespace.yaml
```

If using the terminal before the manifest bundle arrives in VDI, these two short commands create the same namespace
and add the label from the manifest:

```sh
kubectl --context=default create namespace kira-context-memory
kubectl --context=default label namespace kira-context-memory app.kubernetes.io/part-of=kira-context-memory --overwrite
```

If the namespace was already created, use the manifest apply path or verify it and run the
label command; do not delete/recreate the namespace. The current laptop session prepared
these files without creating a namespace in the cluster.

The namespace manifest creates only the namespace. The adjacent Deployments, Services,
Jobs and Secret templates provide the full rollout described in [DEPLOY.md](DEPLOY.md), using
the database, provider and registry bindings described in
[the environment record](../../docs/kubernetes-target-environment.md). Browser entry-point
and HTTPS bindings are deferred as described above. Every application
Pod/Job must carry the compute selectors and `project=lhvtt:NoSchedule` toleration from that
record. No ResourceQuota or LimitRange is introduced by this bootstrap.

The user's SQL probes found no available `vector` extension on either existing PostgreSQL
server (16.4 and 15.4). The environment record proposes a dedicated PostgreSQL 16/pgvector
pilot with a PVC using the observed `longhorn-storage-2-retain` class. This needs its own
internal-registry image digest, roles/TLS and storage/backup verification. No database
workload is created by applying the namespace alone; use `postgres.yaml` for the dedicated DB.

## PostgreSQL pilot PVC

The supplied StorageClass YAML confirms `numberOfReplicas: "2"` and
`staleReplicaTimeout: "2880"` for `longhorn-storage-2-retain`. The existing class is referenced
without changing it. [postgres-pvc.yaml](postgres-pvc.yaml) prepares `postgres-data` in
`kira-context-memory`, with `ReadWriteOnce`, `Filesystem`, and an initial `20Gi` pilot request.
This size is a working default and should be sized against the measured workload for production.

Apply the namespace first, then the PVC from a workstation with this checkout and cluster access:

```sh
kubectl --context=default apply -f deploy/k8s/postgres-pvc.yaml
kubectl --context=default get pvc postgres-data -n kira-context-memory
```

`Immediate` provisioning means applying this PVC can allocate a Longhorn volume before the
database Pod is started. The laptop session only prepared the manifest; it has not provisioned
storage. Verify `Bound` and, after the PostgreSQL Pod mounts it, verify volume health, actual
replicas and durable database writes. Node-level `allowScheduling=true` and the transcribed
disk byte counts do not replace that verification. `Retain` does not replace backups or a
PostgreSQL HA configuration. The requested two Longhorn copies are storage replicas, not
two database servers. A single PostgreSQL primary is the intended initial pilot consumer.
