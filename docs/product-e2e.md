# Product E2E acceptance

## Scope

This is the laptop gate for product behavior against the disposable product stack. It runs
the real frontend proxy, auth/session implementation, Gateway, PostgreSQL, Worker and vendored
Mem0 integration. Only KiRa, rewrite, embedding and memory-LLM providers are explicit deterministic
mocks.

This gate answers whether product contracts, transactions, retries and failure handling work. It
does **not** measure semantic quality and must not be used as benchmark evidence for real KiRa or
internal models.

## Run

From the repository root:

```powershell
docker compose -f compose.product.yaml config --quiet
docker compose -f compose.product.yaml up -d --build --wait
uv run python -m scripts.local.smoke_product_e2e
```

The E2E runner first normalizes Gateway, Worker and frontend to the base Compose runtime so a
previous overlay or rebuild cannot leave Nginx pointing at an old container address. It then
creates a disposable user and reuses the formation/cross-conversation LTM smoke under that
isolated identity before exercising the broader product lifecycle. It temporarily enables the
existing OTel Compose overlay for the Collector-outage gate. Prometheus and Grafana remain off.
Cleanup restores the base product containers and removes the Collector, including after a failed
assertion.

The expected sanitized evidence is:

```text
PASS product_auth_admin_and_user_isolation
PASS product_response_before_memory_release ...
PASS product_formation durable_memory_count=1
PASS product_cross_session_ltm recent_messages=0
PASS product_frontend_auth_and_history conversations=2
PASS product_concurrent_and_completed_retry messages=2 kira_calls=1
PASS product_disconnect_retry cancelled_then_completed attempts=2
PASS product_delete_blocks_late_memory_write
PASS product_restart_preserves_auth_and_history
PASS product_kira_outage_retry recovered=true
PASS product_postgres_outage_recovery sanitized=true
PASS product_telemetry_outage_fail_open
PASS product_logs_sanitized
```

## Gate coverage

| Gate | Evidence |
|---|---|
| Formation and retrieval | A response completes while formation is blocked; Worker later persists exactly one marked fact; another conversation retrieves it and the rewritten query reaches KiRa. |
| Auth lifecycle | Admin CLI create/reset/revoke, login/logout and invalidated sessions work; another user cannot list or read the owner's conversation. |
| Stream/history/idempotency | A live duplicate is rejected as `REQUEST_IN_PROGRESS`; a completed retry replays without a second KiRa call; exactly two turn messages are persisted. |
| Disconnect/cancel | Disconnect after the first delta produces a cancelled attempt with no partial messages; retrying the same client message completes as attempt 2. |
| Deletion race | Deleting while formation is blocked removes conversation, job, vector memory and receipt, and releasing the Worker cannot recreate them. |
| Restart | Gateway, Worker and frontend restart without losing the auth session or conversation history. |
| KiRa outage | The API returns a sanitized retryable `KIRA_CONNECTION_ERROR` or `KIRA_TIMEOUT`, depending on whether refusal or timeout wins the network race; the same client message succeeds after KiRa recovers without duplicate messages. |
| PostgreSQL outage | Worker readiness fails, product API returns a retryable sanitized 503, and the same session works after recovery. |
| Telemetry outage | Collector is stopped while a chat completes; Gateway and Worker readiness stay healthy. |
| Data handling | Gateway/Worker logs are checked both before and after telemetry container recreation for synthetic message markers/run ID, test passwords and provider/database credentials. |

The fault-control endpoints are test-only, bounded and content-free. KiRa evidence stores query
hashes and counters, never raw prompts.

## PostgreSQL transaction/race gate

The Docker E2E observes external behavior. Run the focused PostgreSQL suite as the companion gate
for database constraints, atomic completion rollback and row-lock/fencing semantics:

```powershell
$env:POSTGRES_TEST_URL = "postgresql+asyncpg://kira:local-product-only@127.0.0.1:15434/kira_product"
uv run pytest `
  tests/integration/postgres/test_auth_schema.py `
  tests/integration/postgres/test_auth_service.py `
  tests/integration/postgres/test_chat_request_reservation.py `
  tests/integration/postgres/test_conversation_deletion.py `
  tests/integration/postgres/test_memory_owner_fencing.py `
  --no-cov
Remove-Item Env:POSTGRES_TEST_URL
```

This suite proves concurrent reservation ownership, stale-attempt fencing, atomic turn + memory-job
completion, rollback when job scheduling fails, idempotent pending deletion and late memory-write
blocking. Together with the Docker flow, it is the product acceptance gate.

## Cleanup

The runner removes its synthetic conversations and temporary user. To remove the whole disposable
environment and database:

```powershell
docker compose -f compose.product.yaml down -v
```

## Memory formation idempotency

`memory_jobs.event_id` là correctness key của một formation attempt xuyên suốt claim, lease
reclaim, memory persistence và queue completion. `turn_id`, memory text, semantic similarity và
retrieval rank không phải idempotency key. UUID này đi qua `ProcessMemoryJobUseCase`,
`ProcessMemoryUseCase`, `MemorySource` và `Mem0Adapter` dưới
`metadata.formation_event_id`.

| Queue state | Receipt state | Worker behavior |
| --- | --- | --- |
| `processing` | absent | Chạy native formation, commit atomically memories + receipt. |
| `processing` sau reclaim | present | Trả receipt, bỏ qua provider và complete queue. |
| `processing` sau reclaim | absent | Attempt cũ chưa commit; chạy formation lại. |
| `completed` | present | Terminal state; không claim lại. |

Receipt rỗng vẫn là một formation đã commit. Crash trước memory transaction commit sẽ rollback cả
memory lẫn receipt; reclaim chạy lại. Crash sau commit nhưng trước queue completion sẽ thấy receipt
và không gọi provider lần hai. Receipt primary key chọn một winner khi cùng event chạy concurrent.
Vector batch lỗi rollback toàn bộ; các event khác nhau tạo fact tương đương vẫn do native Mem0
policy xử lý, không phải queue idempotency.

Acceptance bao phủ propagation/preflight, concurrent paraphrase, pgvector transaction rollback,
schema upgrade không mất vector và SIGKILL trước/sau formation commit với lease reclaim.

## OpenAI runtime smoke

[compose.openai.yaml](../compose.openai.yaml) kiểm tra Gateway + Worker với OpenAI thật cho rewrite,
memory extraction và embedding/search. KiRa vẫn là local mock và dữ liệu phải hoàn toàn synthetic;
đây là dependency/runtime smoke, không phải semantic benchmark hoặc KiRa acceptance.

Tạo file local từ template rồi điền key/model được phép dùng:

```powershell
Copy-Item evaluation/openai.env.example .env.openai.local
notepad .env.openai.local
uv run --frozen python -m scripts.local.openai_stack config
uv run --frozen python -m scripts.local.openai_stack up
```

File cần tối thiểu `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL`; base URL mặc định
là `https://api.openai.com/v1` và embedding dimension mặc định là `1536`. File local bị Git và
Docker build context ignore. Không commit, log hoặc chụp key; người có Docker access trên máy vẫn
có thể đọc container environment nên chỉ dùng key dev có giới hạn phù hợp.

Stack có PostgreSQL, migration, memory init, KiRa mock, Gateway và Worker. Endpoint:

- Gateway `http://127.0.0.1:18000`;
- Worker `http://127.0.0.1:18001`;
- KiRa mock `http://127.0.0.1:18122`;
- PostgreSQL `127.0.0.1:15433`.

Dùng cùng một session để lượt đầu tạo completed turn/formation job, chờ Worker rồi gửi lượt sau để
kích hoạt retrieval và rewrite:

```powershell
$smokeSession = "openai-smoke-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
uv run --frozen python -m scripts.local.smoke_gateway `
  --gateway-url http://127.0.0.1:18000 `
  --session-id $smokeSession `
  --message "Trong dữ liệu synthetic, tôi muốn nhận báo cáo KPI vào sáng thứ Hai."
Start-Sleep -Seconds 8
uv run --frozen python -m scripts.local.smoke_gateway `
  --gateway-url http://127.0.0.1:18000 `
  --session-id $smokeSession `
  --message "Khi nào nên gửi báo cáo KPI cho tôi?"
```

Kiểm tra evidence không chứa prompt/output:

```powershell
docker exec kira-context-openai-worker-1 kira-memory-jobs stats
(Invoke-WebRequest http://127.0.0.1:18000/metrics).Content | `
  Select-String 'kira_context_rewrite_total|kira_memory_search_total|kira_memory_job_schedule_total'
```

Cần ít nhất một job `completed` và rewrite outcome `success`. Dừng stack bằng
`uv run --frozen python -m scripts.local.openai_stack down`; thêm `--volumes` khi muốn xóa database
synthetic.
