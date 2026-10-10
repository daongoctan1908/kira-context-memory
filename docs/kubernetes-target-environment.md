# Kubernetes target environment

Recorded on 2026-10-07 from environment details and Pod probe results supplied by the user;
updated on 2026-10-08 with VDI terminal transcriptions and 2026-10-09 with numeric capacity output.
The AI probe execution timestamps and
raw outputs were not supplied. This records the supplied observations; the current laptop
session did not access the cluster or repeat the probes.
Application deployment and benchmark acceptance remain governed by
[production-deployment.md](production-deployment.md) and
[benchmark-k8s-acceptance.md](benchmark-k8s-acceptance.md).

## Worker pool

| Node | Internal IP | CPU capacity / allocatable | RAM capacity / allocatable | Ephemeral capacity | Pod capacity / allocatable |
|---|---|---|---|---|---|
| `hkh9104-10-221-248-16` | `10.221.248.16` | 96 / 96 | `259136916Ki`, approximately 247 GiB | `51136Mi`, approximately 50 GiB | 110 / 110 |
| `hkh9104-10-221-248-17` | `10.221.248.17` | 96 / 96 | `259136924Ki`, approximately 247 GiB | `51136Mi`, approximately 50 GiB | 110 / 110 |
| `hkh9104-10-221-248-18` | `10.221.248.18` | 96 / 96 | `259136924Ki`, approximately 247 GiB | `51136Mi`, approximately 50 GiB | 110 / 110 |

Each node has approximately 47.4 GiB allocatable ephemeral storage. The combined pool has
288 CPU, approximately 741 GiB RAM, approximately 150 GiB ephemeral capacity (approximately
142.2 GiB allocatable), and 330 allocatable Pods. These figures describe the pool, not free
resources or a reservation for this application. The allocation/usage snapshot below supports
the initial replica/resource settings in [CAPACITY.md](../deploy/k8s/CAPACITY.md). Namespace
admission and placement remain cluster checks.

### Allocation and usage supplied on 2026-10-09

All three target nodes report CPU requests `12120m`, CPU limits `2`, memory requests
`346Mi`, memory limits `1Gi` and zero requested/limited ephemeral storage. Usage is higher
than requested RAM, so reservations alone cannot describe actual resource use.

| Node suffix | CPU usage | Memory usage | Longhorn maximum | Longhorn available | Longhorn scheduled |
| --- | ---: | ---: | ---: | ---: | ---: |
| `.16` | `2131m` | `5847Mi` | 1602.1 GiB | 239.4 GiB | 900 GiB |
| `.17` | `1554m` | `6023Mi` | 1602.1 GiB | 148.6 GiB | 1000 GiB |
| `.18` | `1933m` | `5736Mi` | 1602.1 GiB | 793.0 GiB | 720 GiB |

The user supplied numeric output rather than unreadable image digits for this snapshot;
the execution timestamp was not supplied. All three Longhorn nodes allow scheduling.
Available and scheduled storage are different fields; this snapshot does not verify disk
conditions, Longhorn free-space policies, application PVC placement or image-filesystem health.
The sanitized record is [kubernetes-capacity-2026-10-09.json](evidence/kubernetes-capacity-2026-10-09.json).

All three nodes share:

```text
OS:                 Ubuntu 24.04.3 LTS
Architecture:       amd64 / x86_64
Kernel:             6.8.0-90-generic
Kubernetes/RKE2:    v1.30.5+rke2r1
Container runtime:  containerd://1.7.21-k3s2
Required image:     linux/amd64
Labels:             infra/zone=compute, project=lhvtt
Taint:              project=lhvtt:NoSchedule
```

The user's Kubernetes access is cluster-admin: `kubectl auth can-i '*' '*' --all-namespaces`
returned `yes`. This does not establish registry push permissions, an application ServiceAccount
or cluster credentials for the current laptop session.

## Scheduling and storage observations

Application Pods and migration/memory-init Jobs must select the compute pool using both supplied
labels and tolerate `project=lhvtt:NoSchedule` using key `project`, operator `Equal`, value
`lhvtt`, effect `NoSchedule`. If other nodes share these labels, required node affinity must
also restrict scheduling to the three named target nodes. A toleration alone does not select them.

Node `.16` was previously cordoned and is now reported as `Ready=True`, `Unschedulable=false`.
It repeatedly reports `FreeDiskSpaceFailed`: image garbage collection could not free the required
amount and found zero eligible bytes. `DiskPressure`, `MemoryPressure` and `PIDPressure` are all
`False`. The node is schedulable, but image-filesystem free space, inodes, garbage-collection
behavior and release image retention need an operator check before rollout. Do not treat its
capacity as evidence of currently free image storage.

Ephemeral storage is not a PostgreSQL persistence plan. PostgreSQL/pgvector is a separately
provisioned dependency of the application. Existing server probes and storage candidates are
recorded below; a database endpoint, roles and backup arrangements for KiRa are not yet bound.

## Image distribution and build boundary

`registry.vlp.vn` is usable for image pulls: `registry.vlp.vn/tools/busybox:1.32.0` ran
successfully in the cluster. A pull of `curlimages/curl:8.10.1` failed with `ImagePullBackOff`
because `registry-1.docker.io` could not be resolved/reached. Do not rely on public Docker Hub
access from application Pods.

- Build and publish backend/frontend images for `linux/amd64`, then deploy immutable digests
  from the approved internal repository. Benchmark execution additionally needs its eval image.
- Keep Gateway, Worker and migration/memory-init Jobs on the same backend digest.
- Application registry project/path, push credentials, pull credentials and digest references
  have not yet been supplied. Success pulling the busybox image does not establish those permissions.
- The user confirmed the laptop as the build machine; the PC and internal Drive transfer route
  into VDI is recorded below. Current Dockerfiles use public base images: `ghcr.io/astral-sh/uv:0.11.2`,
  `python:3.11-slim-bookworm`, `node:22-alpine` and `nginx:1.28-alpine`.
  The build machine needs access to these images and the Python/JavaScript package sources,
  or approved internal mirrors/caches. Cluster pull reachability does not establish laptop
  build access or a working local container engine. No mirror paths have been supplied.
- Node operating-system details are deployment context; endpoint addresses and credentials belong
  in deployment configuration/Secrets rather than being baked into the images.

No `nvidia.com/gpu` resource is advertised. Qwen and embedding inference run on external services;
the application Pods do not require a GPU or local model weights.

### Image build and transfer route confirmed 2026-10-08

The user confirmed this route for the deployment images:

```text
Laptop: build/export linux/amd64 images
  -> company PC: receive the same image archives
  -> internal Drive channel: transfer the files to VDI
  -> VDI: verify/import the archives and publish to registry.vlp.vn
  -> Kubernetes: pull the published immutable image references
```

Build application images once on the laptop; transfer/import/publish the same images rather
than rebuilding on the PC or VDI. If PC acceptance runs are required, use those same imported
images and retain the acceptance evidence before publishing. This route does not mark any
image build, acceptance, archive transfer or registry publish as completed.

The intended product handoff includes backend and frontend images, the pinned PostgreSQL/pgvector
dependency for the dedicated DB option, image identity/platform/source-revision metadata,
archive SHA256 checksums and deployment manifests. Include the eval image when benchmark
execution is in scope. Keep environment credentials separate from the transfer bundle.
Verify archive checksums after the PC/Drive/VDI transfer and image IDs on import; after publish,
record the registry digests used by the workloads.

The internal Drive channel is supplied by the user; no connector or external Drive service
was selected. Repository paths and push/pull authentication under `registry.vlp.vn` remain
unresolved. Container tooling on the laptop and the VDI import/publish host also needs execution
verification; Kubernetes node containerd and cluster-admin access do not identify a VDI CLI.

The existing `scripts/benchmark/offline_handoff.ps1` provides benchmark Build/Export/Import/Publish
operations for runtime/eval/PostgreSQL images. Its current bundle does not contain the frontend
image, so do not present it as a complete product-release bundle without that addition.

The separate product helper [scripts/deploy/offline_images.py](../scripts/deploy/offline_images.py)
now prepares frozen source inputs, builds/loads backend and frontend for `linux/amd64`, pulls
the pinned PostgreSQL/pgvector dependency, checks container/image contracts and exports all
three images with manifests and SHA256 checksums. It supports verification without Docker.
Build prerequisites are a running local Linux Docker engine and dependency access. Registry
repository/authentication, HTTPS and the VDI browser route are not prerequisites for this
local build/export. See [deploy/k8s/README.md](../deploy/k8s/README.md) for the commands.
Availability of the helper does not claim a successful image build or registry publication.

### Local product build completed 2026-10-08

The laptop's Linux Docker engine was started and the product helper completed a real
backend/frontend build plus PostgreSQL/pgvector pull/export. All three images report
`linux/amd64`. Exported archives total 288.46 MiB and are stored in
`artifacts/deployment/k8s-20261008/transfer`, with source/image manifests, non-secret bootstrap
fragments and SHA256 checksums. See the
[sanitized build receipt](evidence/product-image-build-2026-10-08.json) and
[product handoff](../deploy/k8s/README.md).

Container import/version checks, frontend Nginx/HTTP checks and a real pgvector extension/SQL
check passed in disposable local containers without production credentials or model access.
The dependency reports PostgreSQL 16.15 and pgvector 0.8.6. This establishes laptop Docker
and build-dependency access for this execution. It does not repeat the target-cluster probes
or mark PC/Drive/VDI transfer, registry push, database persistence or application acceptance
as completed. Node image-storage warnings and VDI import/publish tooling remain unresolved.

## Existing KiRa service configuration confirmed 2026-10-08

The user confirmed that the KiRa service binding and credentials are already available.
The local `.env.openai.local` configuration contains these deployment inputs:

| Key | Confirmed input |
|---|---|
| `KIRA_BASE_URL` | `http://10.255.62.64:8122` |
| `KIRA_DOMAIN` | `VBI` |
| `KIRA_USERNAME` | Configured; value is not reproduced here |
| `KIRA_BASIC_AUTH` | Configured; value is not reproduced here |

Reuse these four KiRa keys when preparing the Kubernetes configuration and Secret injection.
Do not request the endpoint or credentials again. Keep credential values out of Git, image
layers, manifest bundles and command output. The source file also contains local OpenAI
settings; do not copy the complete file into production configuration.

This confirms the available configuration source. Kubernetes Secret injection and authenticated
KiRa/SSE acceptance with the deployed application remain rollout work; the local presence
check did not test credential validity or network reachability.

## AI deployment candidates and supplied Pod evidence

| Service | Candidate model | Base URL | Additional supplied details |
|---|---|---|---|
| Embedding | `Qwen3-Embedding-0.6B` | `http://10.254.135.40:8001/v1` | 1024 dimensions; CPU inference; health at `/health` |
| Rewrite and memory formation | `/models/Qwen3_14B` | `http://10.254.135.40:8080/v1` | vLLM serving runtime; shared endpoint selected by the user |

The user reports all of the following checks passed from Pods on each of `.16`, `.17` and `.18`:

| Check | Embedding | Qwen |
|---|---|---|
| TCP connection | `:8001` PASS | `:8080` PASS |
| HTTP probe | `GET /health` HTTP 200 | `GET /v1/models` HTTP 200 |
| Actual inference | `POST /v1/embeddings` PASS | `POST /v1/chat/completions` PASS |

These are verified deployment candidates according to the supplied Pod observations. Preserve
this evidence separately from application-specific provider preflight and semantic acceptance.
The earlier laptop Qwen timeout does not contradict reachability from these Pods.

The known non-secret runtime bindings are:

```text
VLLM_BASE_URL=http://10.254.135.40:8080/v1
VLLM_MODEL=/models/Qwen3_14B
MEMORY_EMBEDDING_BASE_URL=http://10.254.135.40:8001/v1
MEMORY_EMBEDDING_MODEL=Qwen3-Embedding-0.6B
MEMORY_EMBEDDING_DIMS=1024
MEMORY_LLM_BASE_URL=http://10.254.135.40:8080/v1
MEMORY_LLM_MODEL=/models/Qwen3_14B
```

The user clarified on 2026-10-08 that the tested Qwen/embedding calls work without an API key
and selected the same Qwen endpoint/model for memory formation. Leave `VLLM_API_KEY`,
`MEMORY_EMBEDDING_API_KEY` and `MEMORY_LLM_API_KEY` unset; no AI credential needs to be supplied
for this binding. Use the supplied embedding model identifier above and verify its request
compatibility during application preflight. The corresponding benchmark embedding binding
uses the same candidate URL and dimension.

The prepared non-secret fragments are
[production-rewrite.env.example](../deploy/production-rewrite.env.example) and
[production-memory.env.example](../deploy/production-memory.env.example). These provider
fragments do not replace the complete runtime configuration or database Secrets.

Qwen's chat inference success does not yet establish the rewrite adapter's final plain-text,
non-thinking, token/finish-reason contract or the formation extraction JSON contract. Provider
selection and authentication are resolved; run the application-specific rewrite/formation
checks during rollout and retain their results. Do not require another connectivity test to
answer these configuration questions. The benchmark judge binding remains open only when
benchmark execution is in scope.

## User access deferred 2026-10-08

The user chose to defer the browser-access route, HTTPS domain, Ingress class and TLS
certificate/Issuer. They still need to request network access from VDI to a selected target
node if using a node-based entry point. No VDI-to-node connection is assumed to be open.

| Stage | Work | Access requirement |
|---|---|---|
| Internal deployment and smoke checks | Build/transfer images, provision the KiRa database, run initialization, prepare internal services and check application/provider behavior from within the cluster | Existing cluster administration and required Pod dependency access; no browser domain/Ingress required |
| VDI browser access and production acceptance | Confirm the permitted entry point, source/destination/port, HTTPS origin, TLS termination and streaming behavior | Deferred until the network route and browser configuration are ready |

Use `ClusterIP` for the initial application Services. A later frontend NodePort is an option
if the platform approves VDI-to-node access; select and verify its actual node IP and port
before requesting that connection. The frontend proxies `/api/` to Gateway, so browser access
needs the frontend entry point rather than separate Gateway/Worker/database ports.

The application enforces HTTPS origin, secure cookies and enabled auth for
`APP_ENVIRONMENT=production`. If authenticated HTTP smoke checks are needed before TLS is
available, use an isolated `APP_ENVIRONMENT=test` configuration with `AUTH_ENABLED=true`,
`DEV_STATIC_IDENTITY_ENABLED=false`, the actual internal test origin, and
`AUTH_COOKIE_SECURE=false`. Keep that deployment internal and restore production settings
for user access. Do not bypass the production validator or count HTTP smoke results as
completed production browser acceptance.

Optional local browser debugging can use
[kubectl port-forward](https://kubernetes.io/docs/tasks/access-application-cluster/port-forward-access-application-cluster/)
if kubectl, cluster API credentials and the forwarding connection work on the VDI itself.
A port-forward running in a remote Rancher terminal binds on that terminal's host; it does
not establish a listener on the VDI browser's localhost. This option is not yet verified.

## VDI inventory received 2026-10-08

The user transcribed visible terminal output from VDI screenshots because raw logs could not
be transferred from VDI. Unreadable characters are explicitly marked `[?]`. Preserve those
markers; do not reconstruct hashes or uncertain resource names.

- [Namespace/quota/LimitRange transcription](evidence/kubernetes-vdi-inventory-2026-10-08.txt)
  is a byte-identical copy of the supplied attachment, preserving its order, spacing and markers.
  SHA256: `07d3df76b702b9ad6be52603ab93d784d9b221394388d736e44cbd260a9e4a05`.
- [CI/CD transcription](evidence/kubernetes-vdi-cicd-2026-10-08.txt) records the three subsequent
  command/output blocks supplied in the message, with presentation spacing normalized.
  No execution chronology between the two source blocks is inferred.

### Context, namespaces and Rancher associations

`kubectl config current-context` returned `default`. This is the context's name; the selected
namespace and cluster reference in kubeconfig were not printed. Do not equate it with selection
of namespace `default` for the application.

The following namespace/project labels were readable in the supplied inventory:

| Namespaces | Reported `field.cattle.io/projectId` |
|---|---|
| `default` | `p-mpnmw` |
| `vlp-cicd`, `vlp-core-app`, `vlp-database-core`, `vlp-dev` | `p-st877` |
| `keycloak`, `minio-operator`, `mlflow`, `superset`, `tenant-ns`, `vmlp-db`, `vmlp-operator` | `p-7dvxk` |
| `fleet-default`, `fleet-local`, `cattle-fleet-clusters-system` | `p-5g94w` |

Other readable namespace names included `autolake-ai-test`, `test-154`, `vtt-test-156`,
`vtt-core-2-157`, `vtt-core-3-158`, `vtt-core-4-159`, `vtt-gboc-162`, `local-storage` and
`longhorn-system`. Their existence does not choose a namespace or storage dependency for KiRa.
Node selector label `project=lhvtt` and a Rancher namespace project ID are different bindings;
the inventory did not supply a mapping between them.

### Quotas and LimitRanges

`kubectl get resourcequota,limitrange -A` showed namespace ResourceQuotas primarily under
`vlp-tenantfmxjcjj-ws1j8l6to-*`, listing `limits.cpu` and `limits.memory` as used/hard values.
These are aggregate declared resource limits charged to each quota, not measurements of
current CPU/memory use or available resources in the three-node compute pool. An example
`10/10` CPU limits entry has no remaining CPU-limit quota in that snapshot. Quota limits
exceeding the target pool's capacity do not establish additional schedulable compute for KiRa.

Two LimitRange objects were listed:

| Namespace | Name | Reported creation time |
|---|---|---|
| `cert-manager` | `default-tmsft` | `2025-03-24T04:24:58Z` |
| `dev-data-transfer` | `default-4rpks` | `2026-05-08T03:09:38Z` |

The output did not show their default requests/limits, minimum/maximum values or ratios.
LimitRanges apply within their own namespaces. The listing does not establish policy for a
future KiRa namespace. Its ResourceQuota/LimitRange and any Rancher project-level policy
must be checked after the target namespace/project are identified.

### User clarification and namespace bootstrap

Full internal deployment sources were subsequently prepared in
[deploy/k8s/DEPLOY.md](../deploy/k8s/DEPLOY.md), using these confirmed node/provider/storage
bindings. The dedicated PostgreSQL pilot, ordered initialization Jobs and application
workloads are now represented in reviewable YAML. Published image digests/private Secrets
must be bound before apply; no namespace, volume or workload was created from this laptop.

The user clarified on 2026-10-08 that KiRa has no existing Rancher project or Kubernetes
namespace and that they have cluster-admin access. Namespace/project allocation is therefore
an action the operator can perform, not an external approval dependency.

Use `kira-context-memory` as the working namespace name and raw Kubernetes YAML for the initial
bootstrap. [deploy/k8s/namespace.yaml](../deploy/k8s/namespace.yaml) is prepared in the repository;
it has not been applied by this laptop session. An optional Rancher project can be created or
associated later when project-level grouping, team permissions or quotas are wanted. No
existing `p-*` project is selected or inferred from the compute node label.

The namespace bootstrap does not add quotas or container defaults. Choose explicit workload
resources when the initial application deployment is prepared and check any admission policies
that actually apply to the newly created namespace. Cluster-admin authorization does not supply
database credentials, registry publish credentials, service model bindings or HTTPS settings.

### CI/CD, Fleet and GitLab routing

`kubectl get gitrepos.fleet.cattle.io -A` returned `No resources found` in the inspected context.
This is an empty resource listing at the supplied snapshot; it does not rule out another
management-cluster GitOps workflow or select Helm, Kustomize or manual deployment for KiRa.

In `vlp-cicd`, GitLab shell, KAS, registry and webservice Deployments reported `2/2` ready,
and Sidekiq `1/1`. Gitaly and Jenkins StatefulSets reported `1/1`; Redis master and replicas
reported `1/1` and `3/3` respectively. These workloads demonstrate existing CI/CD infrastructure
in the snapshot. No build job, runner/agent configuration or registry publish permission was shown.

The GitLab registry Service was reported as `10.43.71.172:5000`. GitLab, KAS and registry
Ingresses showed class `gitlab-ce-nginx` and hosts `gitlab.example.com`, `kas.example.com` and
`registry.example.com`. Preserve these as transcribed values; DNS/TLS/reachability and their
relationship to the separately confirmed `registry.vlp.vn` have not been established.
The application's Ingress class, origin and certificate are deferred to the user-access stage
as recorded above; they are not prerequisites for the initial internal deployment.

The next small read-only commands and outstanding platform decisions are collected in
[kubernetes-vdi-discovery.md](kubernetes-vdi-discovery.md).

### PostgreSQL and storage probes received 2026-10-08

The user supplied the additional
[database/storage transcription](evidence/kubernetes-vdi-database-storage-2026-10-08.txt).
The first `vmlp-db` socket probe failed because no password was supplied. The user then
successfully authenticated using the existing container environment without printing its value.
The subsequent PostgreSQL probes establish the following:

| Namespace / Pod | Reported container image | SQL server version | `pg_available_extensions WHERE name='vector'` |
|---|---|---|---|
| `vmlp-db` / `vmlp-db-postgresql-0` | `registry.vlp.vn/vtt-common/bitnamilegacy/postgresql:16.4.0-debian-12-r7` | `16.4` | 0 rows |
| `vlp-database-core` / `postgresdb-0` | `registry.vlp.vn/vlp/postgres:15.4` | `15.4 (Debian 15.4-1.pgdg120+1)` | 0 rows |

The inventory also included an Oracle workload; it is not a PostgreSQL/pgvector candidate.
The PostgreSQL extension view lists extensions available for installation. Neither probed
server currently advertises `vector` as installable. This is different from a listed extension
whose `installed_version` is null. Creating another database or issuing `CREATE EXTENSION`
does not install missing server-side extension files. Existing installations in other databases
were not queried via `pg_extension` and are not inferred from this result.

KiRa's memory initializer executes `CREATE EXTENSION IF NOT EXISTS vector` and creates
`vector(1024)` columns/HNSW indexes with the supplied embedding dimension. A fresh KiRa
database therefore needs a pgvector-capable PostgreSQL server. Kubernetes cluster-admin
access does not supply that extension through a SQL permission change or an application image.

The `vmlp-db` metadata probe revealed Secret references `vmlp-db-postgresql/password` and
`vmlp-db-postgresql/postgres-password`; no values were supplied. Empty reference output on the
other Pod does not establish its network authentication or TLS policy. The successful socket
probes also do not prove application-Pod TCP connectivity or grant KiRa database roles.

The following storage observations are confirmed by the supplied listing:

| StorageClass | Provisioner | Reclaim policy | Binding | Expansion |
|---|---|---|---|---|
| `longhorn-storage-2-retain` (default) | `driver.longhorn.io` | Retain | Immediate | true |
| `longhorn-storage-retain` | `driver.longhorn.io` | Retain | Immediate | true |
| `longhorn`, `longhorn-static`, `longhorn-storage-2-delete`, `longhorn-storage-delete` | `driver.longhorn.io` | Delete | Immediate | true |
| clickhouse/kafka/keeper local classes listed in the source | `kubernetes.io/no-provisioner` | Retain | WaitForFirstConsumer | false |

For a dedicated KiRa database, use `longhorn-storage-2-retain` as the explicit PVC storage class
in the prepared pilot manifest. The subsequent supplied YAML confirms its replica parameters
in the next section. Free schedulable storage, actual replica health and volume attachment on
the target nodes still need verification. `Retain` is a PV reclaim policy, not backup/restore
evidence or PostgreSQL replication/failover.

The recommended pilot preparation is a separately provisioned PostgreSQL 16 instance with
pgvector for KiRa, a persistent Longhorn PVC, and a single physical database for app and memory
using dedicated roles. The repository currently pins `pgvector/pgvector:0.8.6-pg16-bookworm`
for local/CI database runs. Mirror that exact `linux/amd64` dependency to an application-accessible
repository under `registry.vlp.vn` and record its immutable digest before using it in K8s.
The target registry path/digest has not been supplied or published in this session.

If this dedicated database option is adopted, the minimum product image set is backend,
frontend and PostgreSQL/pgvector; benchmark execution adds the eval image. A single-primary
StatefulSet is a pilot starting point, not proven production HA. Longhorn storage replication
does not replace a PostgreSQL HA design. Production rollout still needs database TLS/roles,
backup/restore, capacity and failure recovery acceptance. The existing shared DB workloads
remain inventory candidates; this session did not modify them or run database DDL.

### Longhorn parameters and node inventory received 2026-10-08

The additional [Longhorn transcription](evidence/kubernetes-vdi-longhorn-2026-10-08.txt) is a
byte-identical copy of the supplied attachment, preserving all lines and their order.
SHA256: `8c96a47a8c750afc6800facfed69e0e04d8e77c69b9eeef4db87019f2f8f187f`.
The user explicitly cautioned that long numeric columns were difficult to read because of
moire. The attachment contains digit strings without individual uncertainty markers; retain
them as supplied, but do not promote them to verified capacity calculations or repair them by
guessing. Resource version/UID strings are also unnecessary for deployment authoring.

The readable StorageClass YAML confirms:

| Field | Supplied value |
|---|---|
| Name | `longhorn-storage-2-retain` |
| Provisioner | `driver.longhorn.io` |
| Default class annotations | both supplied annotations are `"true"` |
| `parameters.numberOfReplicas` | `"2"` |
| `parameters.staleReplicaTimeout` | `"2880"` |
| `reclaimPolicy` | `Retain` |
| `volumeBindingMode` | `Immediate` |
| `allowVolumeExpansion` | `true` |

`numberOfReplicas` is the desired number of Longhorn data copies for newly provisioned volumes,
not the number of PostgreSQL Pods and not evidence that two healthy replicas already exist.
`staleReplicaTimeout` is measured in minutes; 2880 is 48 hours before a replica marked unhealthy
is considered stale for rebuild purposes and deleted. It is not a PostgreSQL request timeout.
The supplied StorageClass did not contain a Longhorn `nodeSelector`, `diskSelector` or
Kubernetes `allowedTopologies`. Do not infer that its volume replicas are restricted to the
three compute nodes just because application Pods use compute selectors.

The Longhorn Node inventory included all three named target nodes with
`.spec.allowScheduling=true`. This permits node-level scheduling, but does not prove disk-level
allowScheduling, Ready/Schedulable conditions or sufficient capacity under the actual Longhorn
reservation/minimum-free/overprovisioning settings. The earlier moiré-affected values were
superseded for sizing by the numeric 2026-10-09 snapshot above; disk policies remain unverified.
They are Longhorn disk fields, not Kubernetes node ephemeral/image-filesystem measurements;
this listing does not resolve node `.16`'s earlier `FreeDiskSpaceFailed` event.

[deploy/k8s/postgres-pvc.yaml](../deploy/k8s/postgres-pvc.yaml) prepares PVC `postgres-data`
in namespace `kira-context-memory`, explicitly using this StorageClass, `ReadWriteOnce`,
`Filesystem` and an initial 20 GiB pilot request. The size is a working default, not a measured
production capacity requirement. No PVC was applied and no volume was provisioned in this
laptop session. After applying it, verify PVC binding; once PostgreSQL mounts it, verify
Longhorn volume health, desired/actual replicas and database durability/recovery behavior.
The existing StorageClass is referenced as-is rather than recreated or changed.

Technical interpretation follows the primary documentation for
[kubeconfig contexts](https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/),
[ResourceQuotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/),
[LimitRanges](https://kubernetes.io/docs/concepts/policy/limit-range/) and
[Rancher projects/namespaces](https://ranchermanager.docs.rancher.com/how-to-guides/new-user-guides/manage-clusters/projects-and-namespaces), and
[Rancher project quotas](https://ranchermanager.docs.rancher.com/how-to-guides/advanced-user-guides/manage-projects/manage-project-resource-quotas/about-project-resource-quotas).
Database/storage interpretation follows
[PostgreSQL's extension view](https://www.postgresql.org/docs/16/view-pg-available-extensions.html),
[pgvector installation](https://github.com/pgvector/pgvector#installation) and
[Kubernetes StorageClasses](https://kubernetes.io/docs/concepts/storage/storage-classes/).
Longhorn interpretation follows its
[StorageClass parameters](https://longhorn.io/docs/latest/references/storage-class-parameters/) and
[replica scheduling](https://longhorn.io/docs/latest/nodes-and-volumes/nodes/scheduling/) documentation.

## Remaining deployment inputs

1. Cluster reference (context name is confirmed as `default`) and application rollout workflow.
   Namespace creation uses `kira-context-memory` with the supplied cluster-admin authority and
   the prepared raw YAML bootstrap; a Rancher project is optional. Existing GitLab/Jenkins
   inventory is known. Image build/transfer is confirmed as laptop -> company PC -> internal
   Drive -> VDI; raw YAML bootstrap is prepared, while rollout execution remains unverified.
2. Application registry repository paths, push/pull authentication and VDI container import/publish
   tooling. The laptop build/export and dependency access passed for the recorded product image
   set; image-pull Secret binding and registry publication remain unresolved.
3. Deferred to user access: VDI network route/entry point, HTTPS origin, Ingress class if used,
   certificate ownership and streaming timeout policy. These do not block image builds,
   database initialization or isolated internal application smoke checks.
4. Provision/bind a pgvector-capable KiRa database with endpoint, database, TLS/CA and
   runtime/admin roles. Existing PostgreSQL versions are confirmed as 16.4 and 15.4; both
   report no available `vector` extension. Longhorn StorageClass parameters are now confirmed
   and a 20 GiB pilot PVC is prepared; actual capacity/health, binding/mount and database backup
   arrangements still need verification.
5. Inject the existing KiRa configuration/credentials through the deployment configuration and
   Secret mechanism, then run authenticated KiRa/SSE acceptance. Endpoint, domain, username
   and Basic Auth are already available from `.env.openai.local`; no resupply is needed.
6. Execute application-specific provider acceptance using the recorded embedding model and
   shared Qwen rewrite/formation binding, with API keys unset. Select a benchmark judge only
   when running benchmark acceptance; it is not an application deployment input.
7. Secret provider, application ServiceAccount, NetworkPolicy/egress requirements, resource
   settings/policy for the new namespace, and expected application
   concurrency/availability. Supplied tenant quotas are not an application resource budget.
8. OTel Collector/log/metric destinations, backup/restore ownership and recovery targets.

Collect names and references for Secrets; keep credential values out of this document.
