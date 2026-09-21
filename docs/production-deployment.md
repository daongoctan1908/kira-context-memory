# Production deployment handoff

This runbook separates what is already testable in the repository from work that must use the
company platform. It is not a generic Helm chart and must not be treated as evidence that Phase 11
has passed.

## Current status

| Task | Laptop evidence | Still required in the company environment |
|---|---|---|
| T11.1 platform manifests | Backend/frontend production images, commands, ports, probes and environment contracts exist. | Select the platform-mandated manifest format, bind registry/namespace/Ingress/secrets, render it and prove install plus upgrade. |
| T11.2 database/TLS/operations | Alembic uses a bounded PostgreSQL advisory lock; Gateway and Worker reject split application/memory databases; memory schema has a read-only validation command. | Bind managed PostgreSQL TLS and roles, perform a real backup/restore into another database and retain sanitized evidence. |
| T11.3 observability wiring | Gateway/Worker OTLP export, context propagation, masking and fail-open behavior pass local acceptance. | Deploy/configure the company Collector and backend, then search the real trace by the four application identifiers. |
| T11.4 company pilot | Product E2E exists with explicit mocks. | Run browser/KiRa/internal-provider, deletion-race, restart, restore and rollback acceptance on the exact release digest. |

Docker was unavailable during this laptop checkpoint, so no backup/restore or cluster evidence is
claimed here. `NOT_RUN` is the correct status until the procedures below execute successfully.

## Decisions the PC AI must obtain before writing manifests

Record these answers in the pilot evidence. Do not infer them from a laptop Compose file.

1. Required format: raw YAML, Kustomize, an internal template, or Helm.
2. Registry path, immutable digest policy, namespace naming and image-pull secret mechanism.
3. Ingress class, public HTTPS origin, certificate owner and maximum streaming timeout.
4. Secret provider and rotation mechanism. Secret values must never enter Git, rendered artifacts or
   command output.
5. Managed PostgreSQL host/database, TLS mode/CA, application role, memory runtime role and DDL/admin
   role. All roles must reach the same physical database.
6. Internal KiRa, rewrite, embedding and memory-LLM service DNS names, authentication and egress
   policy.
7. OTel Collector ownership, OTLP/HTTP address and trace/metric backend. Langfuse is optional unless
   the company selects it as the AI trace backend.
8. Backup owner, retention, restore target and recovery objectives.

Only after these are known should T11.1 create platform manifests. Do not restore historical Helm
files merely to have a chart.

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

Secrets:

- `DATABASE_URL`, `MEMORY_DATABASE_URL`, `MEMORY_ADMIN_DATABASE_URL`;
- `KIRA_BASIC_AUTH` and any KiRa service credential;
- `VLLM_API_KEY`, `MEMORY_EMBEDDING_API_KEY`, `MEMORY_LLM_API_KEY` when enabled;
- Collector/backend authorization headers;
- initial admin password, supplied only to an interactive one-shot operator command.

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
OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-service>:4318
```

`DATABASE_URL` and `MEMORY_DATABASE_URL` may use different roles but must resolve to the same host,
port and database. The Worker now fails startup configuration if they differ. Configure TLS in the
DSNs according to the managed PostgreSQL contract; do not copy the plaintext local Compose URLs.

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
