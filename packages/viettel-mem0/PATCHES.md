# Viettel patch ledger

The pristine upstream baseline is commit `4c95a43`. This ledger describes every
intentional delta after that point.

## Packaging delta: `2.0.20+viettel.1`

- Rename the Python distribution from `mem0ai` to `viettel-mem0`.
- Keep the upstream Python import namespace `mem0` unchanged.
- Resolve `mem0.__version__` from the internal distribution name.
- Add a narrow `postgres` optional dependency containing psycopg3 and its pool.
- Register the distribution as a local path source in the parent uv project.

No memory extraction, search, deduplication, update, or deletion logic is
changed in this patch. In particular, the V3 formation pipeline remains
ADD-only. The parent application does not install the upstream broad
`vector-stores` extra, which contains Redis and unrelated providers.

The fork intentionally is not a uv workspace member. uv resolves every optional
dependency group of workspace members into the shared lock, which would put
Redis, GPU runtimes, and unrelated vector stores into the Gateway lock even
though they are not installed. The local path source keeps the same in-repo
development model while locking only the `postgres` extra selected by the
Gateway.

## PostgreSQL ownership boundary: `2.0.20+viettel.2`

- Add an explicit PostgreSQL `schema_name` to the pgvector provider.
- Add `auto_create=false` so Gateway/worker runtime processes fail closed when
  the memory schema has not been initialized and reject explicit create/delete/reset
  collection operations instead of issuing DDL.
- Qualify collection SQL with the configured schema and scope discovery to that
  schema.
- Expose deterministic pgvector pool cleanup so the KiRa adapter can close all
  runtime resources rather than relying on Python finalization.

Schema and extension creation remain available only when `auto_create=true` for
backward compatibility. KiRa configures `auto_create=false`; its separate admin
initializer owns extension, schema, table, and index DDL. Memory formation stays
the pristine upstream V3 ADD-only pipeline.
