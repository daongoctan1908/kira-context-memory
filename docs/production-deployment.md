# Production deployment handoff

This runbook separates what is already testable in the repository from work that must use the
company platform. It is not a generic Helm chart and must not be treated as evidence that Phase 11
has passed.

## Current status

| Task | Laptop evidence | Still required in the company environment |
|---|---|---|
| T11.1 platform manifests | Backend/frontend amd64 builds and PostgreSQL/pgvector export passed; full raw YAML for PostgreSQL, ordered initialization Jobs and internal application rollout is prepared. | Bind published registry digests/private Secrets, prove install plus upgrade, then complete the deferred browser/HTTPS entry point. |
| T11.2 database/TLS/operations | Alembic uses a bounded PostgreSQL advisory lock; Gateway and Worker reject split application/memory databases; memory schema has a read-only validation command. | Bind managed PostgreSQL TLS and roles, perform a real backup/restore into another database and retain sanitized evidence. |
| T11.3 observability wiring | OTel-only Gateway/Worker metrics, context propagation, masking and fail-open behavior pass local checks; raw Collector/Langfuse and selectable metrics YAML is prepared. | Publish/bind observability images, apply ordered YAML, verify real telemetry and search the trace by the four application identifiers. |
| T11.4 company pilot | Product E2E exists with explicit mocks. | Run browser/KiRa/internal-provider, deletion-race, restart, restore and rollback acceptance on the exact release digest. |

Docker was unavailable at the earlier laptop checkpoint. The 2026-10-08 image build/export and
isolated container checks passed as recorded below; backup/restore and cluster acceptance remain
`NOT_RUN` until their procedures execute successfully.

### Target environment update received 2026-10-07

The user supplied a three-node RKE2 `v1.30.5+rke2r1` compute pool, required image platform
`linux/amd64`, internal registry `registry.vlp.vn`, pool labels/taint, and successful embedding
and Qwen inference probes from Pods on all three nodes. Record these supplied observations in
[kubernetes-target-environment.md](kubernetes-target-environment.md). They establish deployment
candidates and scheduling/build constraints; full provider preflight, image build/publish,
database acceptance and application rollout still need their own evidence. Node `.16` also has
an unresolved image garbage-collection warning. Pool capacity is not an application allocation.

The VDI transcription received 2026-10-08 adds context name `default`, namespace/project labels,
namespace quota and LimitRange inventory, and GitLab/Jenkins workload and routing observations.
These are preserved in the target-environment record with the user's `[?]` uncertainty markers.
The user then clarified that KiRa has no existing project/namespace and that they have
cluster-admin access. Use the new namespace `kira-context-memory` and the raw YAML bootstrap
in [deploy/k8s](../deploy/k8s/README.md); creation is an operator action rather than a requirement
to wait for allocation. A Rancher project association is optional. The laptop build route is
confirmed; registry repository access remains unresolved. The user deferred Ingress/HTTPS
and VDI browser connectivity to the user-access stage. See
[kubernetes-vdi-discovery.md](kubernetes-vdi-discovery.md) for small read-only follow-up commands
suitable for VDI transcription.

Further user SQL probes on 2026-10-08 confirmed PostgreSQL 16.4 in `vmlp-db` and 15.4 in
`vlp-database-core`; both returned no available `vector` extension. StorageClass inventory
confirmed default `longhorn-storage-2-retain`. The target-environment record now describes
a dedicated PostgreSQL/pgvector pilot option with a Longhorn PVC. Neither existing server is
bound as the KiRa database, and no database rollout/DDL or storage acceptance is claimed.

The subsequent Longhorn YAML confirms `numberOfReplicas=2` and `staleReplicaTimeout=2880`
for that StorageClass. A separate 20 GiB pilot PVC manifest is now prepared in
[deploy/k8s/postgres-pvc.yaml](../deploy/k8s/postgres-pvc.yaml). It has not been applied. The
user cautioned that long disk byte counts were difficult to read; retain the original
transcription and verify actual PVC/volume binding and health rather than certifying capacity
from those digits. Two requested storage replicas do not establish PostgreSQL HA.

The user confirmed deployment image builds on the laptop, then transfer to the company PC
and through its internal Drive channel into VDI. Preserve the same image set and checksums
through that route; VDI imports/publishes to `registry.vlp.vn`, and Kubernetes pulls immutable
registry references. Do not require a company CI build merely because GitLab/Jenkins exist.
Image build/transfer/publish execution, the repository paths and registry authentication
remain to be verified. The separate [product image helper](../scripts/deploy/offline_images.py)
now includes backend, frontend and PostgreSQL/pgvector exports with image identity, source
snapshot and checksums. The benchmark bundle keeps its separate runtime/eval/PostgreSQL scope.
The real laptop build/export completed on 2026-10-08; its
[sanitized receipt](evidence/product-image-build-2026-10-08.json) records all three amd64 images,
archive/source identities and successful isolated container checks. Transfer, registry publish,
cluster deployment and full production application acceptance remain unexecuted.

The complete internal deployment sources are now available in
[deploy/k8s/DEPLOY.md](../deploy/k8s/DEPLOY.md): a dedicated PostgreSQL/pgvector StatefulSet,
20 GiB Longhorn PVC, three ordered Jobs, Gateway/Worker/Frontend Deployments and Services,
compute scheduling, probes, resource requests/limits and NetworkPolicies. The helpers bind
published internal image digests and prepare private Secrets from the existing KiRa settings.
They do not build, zip, publish or apply. These sources remain separate from the earlier frozen
image archive for review. Initial HTTP access uses the documented authenticated test mode;
production HTTPS, PostgreSQL TLS/HA, backup/restore and actual cluster acceptance remain pending.

## Deployment bindings and working choices

Record these answers in the pilot evidence. Do not infer them from a laptop Compose file.
Reuse the confirmed inputs in the target-environment record; obtain only the unresolved bindings
and policies below rather than requesting the supplied node/provider details again.

1. Initial bootstrap uses raw YAML and namespace `kira-context-memory`, created with the user's
   cluster-admin authority. Adapt the format later if the selected delivery pipeline requires it.
2. Registry path, immutable digest policy and image-pull secret mechanism.
3. Deferred to user access: permitted VDI network route/entry point, Ingress class if used,
   HTTPS origin, certificate owner and maximum streaming timeout.
4. Secret provider and rotation mechanism. Secret values must never enter Git, rendered artifacts or
   command output.
5. A pgvector-capable PostgreSQL host/database, TLS mode/CA, application role, memory runtime role
   and DDL/admin role. Bind a managed service or separately provision the dedicated pilot
   database described in the target record. All roles must reach the same physical database.
6. Reuse the existing KiRa endpoint/domain and service credentials from `.env.openai.local`.
   Prepare its Secret injection and authenticated application/SSE acceptance. Rewrite and
   formation use the recorded Qwen service; embedding uses the recorded 1024-dimensional
   service. The user confirmed calls without API keys. Complete application provider acceptance
   and egress policy using these bindings.
7. The user selected Langfuse and one OTel Collector at `http://otel-collector:4318`.
   Raw sources for the complete Langfuse subsystem and metrics are in
   [deploy/k8s/observability](../deploy/k8s/observability/README.md). Choose standalone
   Prometheus/Grafana or verified shared monitoring; bind published internal image digests,
   stable private Secrets, actual PVC capacity and browser origins. Legacy app metric registries
   are removed. Updated backend/frontend Dockerfiles were rebuilt and probed locally on
   2026-10-09; see [the rebuild receipt](evidence/product-dockerfile-rebuild-2026-10-09.json).
   These images have not been exported or published. Review the expanded replica/resource
   settings and pool/storage budgets in [CAPACITY.md](../deploy/k8s/CAPACITY.md).
8. Backup owner, retention, restore target and recovery objectives.

The namespace bootstrap can be prepared/applied now using the user's cluster-admin authority.
Complete registry, database and runtime bindings for initial internal workloads. The deferred
browser route does not block builds, database initialization or isolated internal smoke checks.
Full production/browser installation acceptance still requires the user-access bindings.
Do not restore historical Helm files merely to have a chart.

### Internal deployment before VDI browser access

The user chose to arrange VDI-to-node connectivity later. Start with internal `ClusterIP`
Services and inspect Pods, initialization Jobs, storage and real dependency calls from within
the cluster. A frontend NodePort or Ingress can be added when the actual access route is chosen.
The frontend already proxies `/api/` to Gateway; a user-facing route only needs to reach it.

Gateway production settings enforce enabled auth, an HTTPS origin and secure cookies. If
HTTP application smoke checks are needed while TLS is deferred, use an isolated test
configuration: `APP_ENVIRONMENT=test`, `AUTH_ENABLED=true`,
`DEV_STATIC_IDENTITY_ENABLED=false`, `AUTH_COOKIE_SECURE=false`, and the actual internal
`AUTH_ALLOWED_ORIGIN`. Use real providers and dedicated credentials/database; the disposable
Compose defaults and provider mocks are not this deployment. Keep the test configuration
internal. It verifies application behavior without claiming production browser acceptance.

Before user access, set `APP_ENVIRONMENT=production`, the actual HTTPS origin and secure
cookies, verify the entry point/network rule and repeat browser/session/SSE acceptance.
The repository's production validation is preserved.

## Workload contract

The minimum application release has the following objects. PostgreSQL and the telemetry backend are
external dependencies, not subcharts of the application.

| Object | Image/command | Ports and probes | Ordering |
|---|---|---|---|
| migration Job | backend image; `alembic upgrade head` | No probe. `MIGRATION_LOCK_TIMEOUT_SECONDS` bounds contention. | First; must complete successfully. |
| memory-init Job | backend image; `python -m worker.memory_admin init` | No probe; requires the pinned embedding endpoint/dimension. | After migration, before runtime pods. |
| Gateway | backend image default command | HTTP `8000`; liveness `/health`, readiness `/ready`. | After both Jobs. |
| Worker | backend image; `python -m worker.main` | HTTP `8001`; liveness `/health`, readiness `/ready`. | After both Jobs. |
| frontend | frontend image | HTTP `8080`; liveness/readiness `/healthz`. | After Gateway Service exists. |
| OTel Collector | company-owned image/config | Collector health endpoint chosen by platform. | Optional for application readiness; outage must remain fail-open. |

The backend image runs as UID/GID `10001`. The frontend image runs as the non-root Nginx user. Do not
override these with root. If the platform mandates a read-only root filesystem, first provide and
test writable `emptyDir` mounts for required `/tmp` and Nginx runtime/template paths; do not enable
the setting without a container smoke test.

Use immutable image digests. Keep Gateway, Worker and migration/memory-init Jobs on the same backend
digest. Record OCI revision/version labels and the frontend digest in release evidence.

## Configuration and secrets

Use ConfigMaps only for non-secret values. Use the company secret provider for all credentials.

The existing `.env.openai.local` supplies `KIRA_BASE_URL=http://10.255.62.64:8122`,
`KIRA_DOMAIN=VBI`, and configured `KIRA_USERNAME`/`KIRA_BASIC_AUTH` values. Reuse only those
four KiRa keys when preparing deployment configuration and Secrets. Do not copy the complete
local file or its OpenAI bindings into production. Credential values are intentionally absent
from the environment record and deployment manifests.

Secrets:

- `DATABASE_URL`, `MEMORY_DATABASE_URL`, `MEMORY_ADMIN_DATABASE_URL`;
- `KIRA_BASIC_AUTH` and any KiRa service credential;
- `VLLM_API_KEY`, `MEMORY_EMBEDDING_API_KEY`, `MEMORY_LLM_API_KEY` when enabled;
- Collector/backend authorization headers;
- initial admin password, supplied only to an interactive one-shot operator command.

The selected Qwen and embedding services are called without API keys, as confirmed by the
user's successful tests. Omit `VLLM_API_KEY`, `MEMORY_EMBEDDING_API_KEY` and `MEMORY_LLM_API_KEY`
from this deployment; their optional Secret entries above apply if authentication is introduced.

Non-secret configuration includes model names, dimensions, timeouts, feature flags, schema/collection
names, pool sizes, service URLs without credentials and OTel sampling/batching values. Start from
`.env.example`, but production must at least set:

```text
APP_ENVIRONMENT=production
AUTH_ENABLED=true
AUTH_ALLOWED_ORIGIN=https://<chat-origin>
AUTH_COOKIE_SECURE=true
DEV_STATIC_IDENTITY_ENABLED=false
LTM_ENABLED=true
MEMORY_FORMATION_ENABLED=true
OTEL_ENABLED=true
OTEL_CAPTURE_CONTENT_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-service>:4318
```

`DATABASE_URL` and `MEMORY_DATABASE_URL` may use different roles but must resolve to the same host,
port and database. The Worker now fails startup configuration if they differ. Configure TLS in the
DSNs according to the managed PostgreSQL contract; do not copy the plaintext local Compose URLs.

### Rewrite provider binding

Gateway `Settings` reads `VLLM_BASE_URL`, `VLLM_MODEL` and optional `VLLM_API_KEY` from the
environment (`app/config/settings.py`). Gateway startup passes those settings to
`VllmQueryRewriterAdapter` (`app/presentation/api/main.py`); the adapter calls
`<base>/chat/completions` when the base already ends in `/v1`. No endpoint or model is selected
from the formation provider: `MEMORY_LLM_*` belongs to the Worker/Mem0 formation path.

Inject the non-secret values in [deploy/production-rewrite.env.example](../deploy/production-rewrite.env.example)
into the Gateway through the company ConfigMap/envFrom mechanism. The production binding is
`http://10.254.135.40:8080/v1` and the exact served base-model name `/models/Qwen3_14B`, not
`genai-lora`. Leave `VLLM_API_KEY` unset for the confirmed unauthenticated service. Local laptop/PC
runs retain their explicit `gpt-4o-mini` configuration. The complete `.env.openai.local` is not
production configuration; only its existing KiRa keys are reused as described above.
This fragment does not choose the company's manifest format, registry, namespace or secrets.
Connectivity is environment-specific: the laptop has OpenAI but cannot reach KiRa; the company
PC has OpenAI and real KiRa but cannot reach Qwen; K8s nodes can reach Qwen and real KiRa.
Use the Qwen fragment only in K8s. PC technical acceptance continues to use the explicit OpenAI
provider configuration from `evaluation/benchmark.pc.env.example`.

The paired production rewrite benchmark must independently declare
`BENCHMARK_REWRITE_BASE_URL=http://10.254.135.40:8080/v1` and
`BENCHMARK_REWRITE_MODEL=/models/Qwen3_14B`, plus its approved internal judge settings.
Freeze the served model, request settings and server/chat-template configuration with the report.
The current adapter expects final plain text, `finish_reason=stop`, and at most 256 generated
tokens; verify that the served Qwen configuration satisfies that contract before rollout.
Qwen3 supports a non-thinking chat-template mode, while its default template may generate
thinking content ([Qwen deployment contract](https://qwen.readthedocs.io/en/stable/deployment/vllm.html#thinking-non-thinking-modes)).
Require non-thinking output for this rewrite service through the actual server/template
configuration and record it in preflight evidence. This application does not inject
`chat_template_kwargs` or strip `<think>` blocks; such a block in `message.content` would be
forwarded verbatim. A separate `reasoning_content` field is ignored, and truncated/empty final
content fails the adapter contract and falls back to the original query. These are compatibility
checks, not observed failures on the inaccessible server. Do not change a shared server's
configuration as part of this application rollout without its operator's deployment contract.
The earlier laptop connection probe timed out. The user's subsequent Pod probes on all three
target nodes passed Qwen chat and embedding inference; see the target-environment record.
Application-specific provider and semantic acceptance remain unproven by those connectivity checks.
Follow the [GLOBAL evidence rollout gate](global-evidence-rollout.md); external-model results
and unit tests do not approve the production semantic behavior.

### Memory provider binding

The user selected the same Qwen service for memory formation:
`MEMORY_LLM_BASE_URL=http://10.254.135.40:8080/v1` and
`MEMORY_LLM_MODEL=/models/Qwen3_14B`. Use
[deploy/production-memory.env.example](../deploy/production-memory.env.example) for the
Gateway/Worker/memory-init provider configuration. It also binds
`Qwen3-Embedding-0.6B` at `http://10.254.135.40:8001/v1` with 1024 dimensions.
Both API-key settings remain unset. The adapter already handles absent keys for these
OpenAI-compatible services, so this binding needs no application code change.

Verify formation extraction JSON and durable memory persistence through the deployed application
during rollout. The supplied inference probes and model selection do not mark that application
acceptance as executed. Local/PC acceptance keeps its explicitly selected providers.

Do not run automatic user creation on every rollout. Create the first account with
`kira-auth-admin create --password-stdin` from a controlled one-shot admin pod, then remove that pod
and its input secret.

## Network policy boundary

Default-deny is the target. Permit only the flows required below, adjusted for platform DNS and
Ingress implementation:

- Ingress → frontend `8080`;
- frontend → Gateway `8000`;
- Gateway → PostgreSQL, KiRa, rewrite provider, embedding provider and Collector;
- Worker → PostgreSQL, embedding provider, memory LLM and Collector;
- migration Job → PostgreSQL;
- memory-init Job → PostgreSQL and embedding provider;
- DNS and platform health/control-plane traffic required by the cluster.

Do not expose Worker, migration, memory-init, PostgreSQL, provider mocks or Collector OTLP ports
through public Ingress. Provider mocks from `compose.product.yaml` are forbidden in the pilot.

## Migration and rollout

1. Confirm backup and restore ownership before changing schema.
2. Run exactly one migration Job per rollout. A second accidental Job waits on the advisory lock and
   fails after `MIGRATION_LOCK_TIMEOUT_SECONDS` instead of racing indefinitely.
3. Run memory-init using the exact release image. It probes the pinned embedding dimension and
   performs only the supported in-place memory schema transition.
4. Run `python -m worker.memory_admin validate`; this is read-only and must report the expected
   memory schema version and vector dimension.
5. Start Worker and Gateway, wait for readiness, then start frontend.
6. Run product acceptance before directing pilot users to the release.

An application rollback is allowed only when the older image declares the current schema compatible.
Never run Alembic downgrade or reset the memory schema as an automatic rollback. If compatibility is
unknown, restore into a separate database and make an explicit recovery decision.

## Backup and restore acceptance

Use company-approved tooling if managed PostgreSQL supplies snapshots. Otherwise the equivalent
logical procedure is:

```bash
pg_dump --format=custom --no-owner --no-acl --file=kira.dump "$SOURCE_ADMIN_DSN"
createdb "$RESTORE_DATABASE_NAME"
pg_restore --exit-on-error --no-owner --no-acl --dbname="$RESTORE_ADMIN_DSN" kira.dump
```

Keep DSNs in the secret mechanism or `PGPASSFILE`, not shell history or evidence. Quiesce Gateway and
Worker, or use a platform-consistent snapshot, before recording source counts. Restore to a different
database; never validate by overwriting the source.

Before backup and after restore, record only aggregate counts—never message/memory/auth content—for:

```sql
SELECT version_num FROM alembic_version;
SELECT count(*) FROM auth_users;
SELECT count(*) FROM auth_sessions;
SELECT count(*) FROM conversations;
SELECT count(*) FROM conversation_messages;
SELECT count(*) FROM chat_requests;
SELECT count(*) FROM memory_jobs;
SELECT count(*) FROM memory.memories;
SELECT count(*) FROM memory.memories_entities;
SELECT count(*) FROM memory.memories_formation_receipts;
SELECT schema_version, embedding_model, embedding_dims, mem0_version, pgvector_version
FROM memory.kira_memory_schema WHERE singleton;
```

The schema and collection identifiers above must be changed if the frozen release uses non-default
names. Acceptance requires exact source/restore aggregate equality, current Alembic revision,
`python -m worker.memory_admin validate` PASS, login/history/retrieval PASS and a new post-restore
chat producing a durable memory receipt. Store checksums and sanitized command outcomes, not the dump,
in the repository.

## Observability production acceptance

Set Gateway and Worker OTLP endpoints to the Collector Service. Do not put backend credentials in the
application pods. The Collector owns export authentication, buffering and backend-specific mapping.

For one real chat, retain the IDs from the API and prove that the backend can find:

```text
correlation_id → turn_id → event_id → Worker attempt
                              └──────→ origin_trace_id
```

Inspect retrieval/context/rewrite/KiRa and Mem0 extract/parse/embed/dedup/persist/receipt spans.
Confirm inputs/outputs are masked and truncated. Then stop or block the Collector and repeat a chat
plus formation: Gateway/Worker readiness and business completion must remain healthy. Raw prompt,
response, credential and exception messages must not appear in logs or exported attributes.

## Pilot and rollback evidence

T11.4 remains `NOT_RUN` until all of these use the exact running image digests:

- frontend login/change-password/logout and two-user isolation;
- real KiRa SSE, rewrite, retrieval and durable formation;
- completed replay, concurrent duplicate, disconnect/cancel and retry;
- deletion pending, provider/persist late-write fencing and idempotent retry;
- Gateway/Worker restart and PostgreSQL/provider/Collector outage behavior;
- backup/restore acceptance above;
- application image rollback compatibility check;
- observed CPU/RAM and a reviewed initial requests/limits choice;
- operator exercises for dead memory jobs and pending conversation purge.

Do not rerun the official semantic benchmark merely because deployment YAML changed. Rerun it only
when formation, retrieval, rewrite, provider/model or persisted memory semantics changed.
