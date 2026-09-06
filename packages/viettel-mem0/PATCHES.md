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
