# kira-context-memory

Context Gateway bổ sung ngữ cảnh hội thoại cho KiRa. Repository được tổ chức theo
Modular Service Architecture và Hexagonal Architecture để application/domain không phụ
thuộc trực tiếp vào FastAPI, HTTPX, PostgreSQL SDK hoặc vLLM.

## Trạng thái

Batch A-D của Tuần 1 cung cấp Gateway baseline hoàn chỉnh để live smoke với KiRa Test.
Batch A-D của Tuần 2 tích hợp short-term context qua PostgreSQL và vLLM. Tuần 3
Batch A1 bổ sung identity/user scope và nền tảng Mem0/pgvector; Batch B1-B2 thêm taxonomy policy
versioned theo native Mem0 V3 dual-source cùng synthetic acceptance gate cho memory extraction.
Batch B3 bổ sung direct completed-turn formation use case; Batch B4 nghiệm thu formation trên
PostgreSQL/pgvector thật với provider doubles deterministic. Batch C1 mở rộng context và rewrite
prompt v2 để nhận ranked LTM an toàn; Batch C2 bổ sung orchestration search song song vào use case.
Batch C3 wire Mem0 retrieval có feature flag vào FastAPI lifecycle và observability:
Batch C4 thêm cross-session acceptance gate trên PostgreSQL/pgvector thật, gồm formation trực tiếp
ở Session A, recall ở Session B, user isolation và precedence Current > Recent > LTM:

- cấu trúc presentation, application, domain, infrastructure, config và worker;
- dependency/tooling bằng Python 3.11, `uv`, Ruff và pytest;
- settings đọc KiRa configuration từ environment;
- request/domain models tối thiểu;
- `KiraClientPort` cùng HTTPX adapter cho authenticate và chat streaming;
- token cache concurrency-safe và parser cho KiRa `data:` frames;
- `HandleChatUseCase`, FastAPI `POST /chat`, SSE proxy và health/readiness probes;
- Docker image non-root, automated test gate và manual smoke client.
- `ConversationStorePort` và message schema version 1 độc lập database SDK;
- PostgreSQL source of truth với migration, transactional pair append, deterministic ordering
  và indexed recent read, dedup theo `turn_id`;
- ContextBuilder, estimated token budget, QueryRewriterPort, prompt v1 và vLLM HTTP adapter;
- `/chat` đọc recent từ PostgreSQL → rewrite → KiRa SSE → lưu completed turn;
- fallback original query, structured logs an toàn và Prometheus `/metrics`.
- `IdentityPort` với static adapter chỉ dành cho dev/test; thiếu trusted identity sẽ không
  đọc/ghi contextual data nhưng KiRa current query vẫn hoạt động;
- conversation được scope bởi `(user_id, session_id)` và completed append trả exact
  `boundary_message_id` để worker tương lai đọc đúng snapshot từ PostgreSQL;
- `LongTermMemoryPort`, Mem0 adapter user-scoped và lifecycle-neutral; engine V3 hiện tại
  vẫn có hành vi additive nhưng adapter không ép kết quả thành ADD-only;
- `ProcessMemoryUseCase` đọc bounded snapshot kết thúc đúng PostgreSQL
  `boundary_message_id`, sau đó giao lifecycle formation cho native Mem0 V3;
- use case được gọi trực tiếp bởi test/dev harness từ reference đã persist; Gateway SSE không gọi
  formation và repository chưa có queue, worker loop hay delivery guarantee;
- pgvector `0.8.6` dev image, embedding dimension probe và admin-owned memory schema;
  Gateway/worker runtime cấu hình `auto_create=false` và không chạy DDL.
- B4 kiểm tra sáu taxonomy positive, sáu negative case, formula exact, duplicate boundary và
  cross-user isolation bằng conversation store + Mem0 adapter + pgvector thật.
- `ConversationContext` nhận tối đa 10 LTM theo đúng ranking từ retrieval; LTM không dùng chung
  recent token budget và current query vẫn luôn được truyền riêng, không trim.
- rewrite prompt v2 chỉ gửi text của LTM trong JSON untrusted data, không gửi memory ID, score hay
  metadata; precedence là current explicit > recent > LTM và memory không phải nguồn authorization.
- `HandleChatUseCase` có thể search LTM theo trusted `user_id` và original current query song song
  với PostgreSQL recent-read; LTM-only context cũng được rewrite để hỗ trợ cross-session recall.
- LTM timeout/lỗi typed fallback về Recent + Current. PostgreSQL recent-read lỗi luôn current-only
  và bỏ kết quả LTM; cancellation/lỗi lập trình không bị nuốt hoặc để task dependency chạy rơi nền.
- `LTM_ENABLED=true` tạo một Gateway-owned `Mem0Adapter` và đóng nó khi shutdown; cấu hình/khởi tạo
  sai fail startup. Runtime search failure chỉ degrade contextual capability và không làm `/ready`
  fail hoặc thay KiRa response bằng synthetic answer.
- `/metrics` có LTM search outcome/latency/result count và degraded counter `mem0/memory_search`;
  không dùng user/session/memory ID, query, prompt hay error message làm label.
- C4 chạy native Mem0 V3 và pgvector thật với provider doubles deterministic, đi xuyên qua
  `ContextBuilder`, HTTP contract của vLLM adapter, Gateway SSE và KiRa double; cùng một
  `session_id` ở user khác không đọc được recent hoặc LTM của owner.
- C4 có thêm semantic gate opt-in dùng embedding, memory LLM và query-rewrite endpoint thật.
  Gate này không tự chạy trong CI/local mặc định và không được báo pass khi chưa có endpoint
  nội bộ được phê duyệt.

Phạm vi implementation local của Tuần 3 cho T3.1–T3.17 đã hoàn tất. Evidence dùng model/KiRa nội
bộ vẫn là release gate bên ngoài vì workspace hiện không có `.env` endpoint/credential. Xem bảng
đối chiếu và lệnh nghiệm thu tại [Week 3 acceptance](docs/week3-acceptance.md).

Tuần 4 bắt đầu bằng ADR T4.1: PostgreSQL `memory_jobs` sẽ là async memory queue duy nhất. Gateway
sau này ghi completed turn và job atomically; Worker claim trực tiếp bằng
`FOR UPDATE SKIP LOCKED`. Redis/Redis Stream không được đưa trở lại baseline. Xem
[Week 4 PostgreSQL memory job queue ADR](docs/week4-t4.1-postgresql-memory-job-queue.md).
T4.2 bổ sung versioned domain models, sanitized queue errors và `MemoryJobQueuePort`; core chưa
biết SQLAlchemy/PostgreSQL. T4.3 thêm migration/schema reference-only với lifecycle constraints và
partial operational indexes. T4.4 thêm feature flag độc lập và transaction ghi completed turn +
optional memory job atomically. T4.5 hiện thực queue adapter PostgreSQL với claim/reclaim
`FOR UPDATE SKIP LOCKED`, lease-token guarded transitions và các thao tác stats/dead/requeue/purge;
T4.6 khóa concurrency, deterministic ordering, final-attempt expiry và user/session boundary
isolation bằng PostgreSQL thật. Batch A đã hoàn tất. T4.7 khóa feature flag formation mặc định tắt,
độc lập với online retrieval `LTM_ENABLED`; formation-only mode không khởi tạo Mem0 trong Gateway.
T4.8 nối flag vào completion callback đúng một lần: chỉ clean KiRa EOF có trusted identity và text
mới persist turn kèm yêu cầu tạo reference-only job; Gateway không gọi Mem0 formation. Worker loop
T4.9 thêm counter scheduling low-cardinality với bốn outcome `scheduled`, `disabled`, `duplicate`,
`error`; không dùng identity hay conversation/job reference làm label. Worker loop và Mem0
processing vẫn được giữ cho các task kế tiếp. T4.10 khóa regression matrix trên PostgreSQL thật và
hoàn tất Batch B: enabled/disabled, stream failure, missing identity và atomic rollback đều giữ
nguyên SSE contract. Xem
[Week 4 T4.5 queue adapter](docs/week4-t4.5-postgresql-memory-job-queue-adapter.md),
[Week 4 Batch A acceptance](docs/week4-batch-a-acceptance.md) và
[Week 4 T4.7 formation flag](docs/week4-t4.7-memory-formation-feature-flag.md),
[Week 4 T4.8 completion scheduling](docs/week4-t4.8-gateway-completion-scheduling.md) và
[Week 4 T4.9 scheduling observability](docs/week4-t4.9-gateway-scheduling-observability.md),
[Week 4 Batch B acceptance](docs/week4-batch-b-gateway-scheduling-acceptance.md).
T4.11 mở Batch C bằng `WorkerSettings` độc lập Gateway và một dependency lifespan fail-fast:
Worker chỉ nhận PostgreSQL, Mem0 và queue runtime settings, validate cả application migration lẫn
pgvector memory schema trước khi sẵn sàng, rồi đóng đúng các resource do Worker sở hữu. Poller,
job execution và HTTP runtime vẫn thuộc T4.12-T4.14. Xem
[Week 4 T4.11 Worker settings and lifecycle](docs/week4-t4.11-worker-settings-lifecycle.md).
T4.12 thêm `ProcessMemoryJobUseCase`: mỗi leased job đọc exact PostgreSQL boundary qua
`ProcessMemoryUseCase`, giữ nguyên native Mem0 lifecycle, rồi thực hiện đúng một transition
complete/retry/dead. Retryable dependency errors dùng backoff theo attempt; lỗi boundary,
configuration/schema và protocol đi thẳng dead; cancellation không bị chuyển thành failure. Xem
[Week 4 T4.12 memory job processing](docs/week4-t4.12-memory-job-processing.md).
T4.13 bổ sung `MemoryJobRunner` one-shot: claim không vượt số slot trống hoặc batch cap, hỗ trợ
nhiều replica qua lease/reclaim của PostgreSQL, backoff exponential tối đa 30 giây khi poll DB lỗi
và ngừng claim ngay khi shutdown. In-flight job được drain trong grace period; job quá hạn bị cancel
mà không tạo transition giả để replica khác reclaim. Runner snapshot chỉ giữ trạng thái
low-cardinality phục vụ readiness/metrics ở T4.14. Xem
[Week 4 T4.13 concurrent runner](docs/week4-t4.13-concurrent-runner.md).
T4.14 đưa runner vào một FastAPI process nội bộ riêng: `python -m worker.main` phục vụ đúng ba
endpoint read-only `/health`, `/ready`, `/metrics`. Readiness yêu cầu runner active, queue claim DB
khả dụng và queue-stats snapshot còn fresh; metrics HTTP chỉ đọc cache, không query PostgreSQL.
Prometheus labels chỉ dùng status/outcome bounded, không chứa identity, event hay error class. Xem
[Week 4 T4.14 Worker FastAPI app](docs/week4-t4.14-worker-fastapi-app.md).
T4.15 thêm retention runner trong cùng Worker lifecycle: chạy ngay một bounded cleanup batch rồi
lặp theo interval, giữ completed mặc định 7 ngày và dead 30 ngày; pending/processing không bao giờ
bị purge. Cleanup timeout/lỗi DB chỉ retry ở chu kỳ kế tiếp và không dừng processing runner.
PostgreSQL acceptance cũng khóa đủ 5 provider attempts cùng retry schedule `1/5/30/120`. Xem
[Week 4 T4.15 retry and retention](docs/week4-t4.15-retry-cleanup-retention.md).
T4.16 thêm operator CLI machine-readable cho queue stats, bounded dead listing và explicit
single-event requeue. CLI dùng dependency lifecycle PostgreSQL riêng, không tải Mem0/provider và
không mở HTTP mutation endpoint. Xem
[Week 4 T4.16 operator CLI](docs/week4-t4.16-memory-job-operator-cli.md).
T4.17 hoàn tất Batch C bằng acceptance matrix cho retry, dead, lost lease, cancellation, cleanup,
readiness và CLI. Cross-component test mới chạy Worker FastAPI runtime cùng queue/conversation
adapter PostgreSQL thật: retryable timeout được retry rồi complete, còn job bị cancel sau shutdown
grace vẫn giữ lease để replica khác reclaim; stale lease token bị từ chối. Xem
[Week 4 T4.17 Worker test acceptance](docs/week4-t4.17-worker-test-acceptance.md).
T4.18 mở Batch D bằng stack `compose.week4.yaml` độc lập và không Redis: PostgreSQL/pgvector,
migration, memory init, Gateway, Worker cùng bốn deterministic provider mocks chạy thành các
service riêng. Gateway/Worker dùng readiness healthcheck; hai DDL job phải exit 0 trước khi runtime
khởi động. Xem [Week 4 T4.18 Compose stack](docs/week4-t4.18-compose-stack.md).
T4.19 khóa happy path bất đồng bộ trên chính stack này: memory-LLM bị chặn trong lúc Session A đã
nhận xong SSE, Worker sau đó complete durable job, và Session B ở session mới recall LTM qua
Rewriter trước khi query đã rewrite tới KiRa. Evidence chỉ dùng hash/count và dữ liệu synthetic.
Xem [Week 4 T4.19 async happy-path E2E](docs/week4-t4.19-async-happy-path-e2e.md).
T4.20 khóa recovery path trên stack thật: lỗi transient complete ở attempt 2; lỗi còn tồn tại quá
retry horizon vào `dead` đúng attempt 5; packaged operator CLI list projection đã sanitize rồi
requeue chính xác event để Worker xử lý thành công. Compose chỉ tăng tốc retry cho synthetic gate,
không đổi default production. Xem
[Week 4 T4.20 retry/dead/requeue E2E](docs/week4-t4.20-retry-dead-requeue-e2e.md).
T4.21 khóa crash boundary sau durable Mem0 write nhưng trước queue complete: PostgreSQL row lock giữ
transition, Worker bị `SIGKILL`, replica mới reclaim lease hết hạn ở attempt 2; native exact-hash
dedup giữ một memory và zero-event retry vẫn complete. Override lease chỉ dùng cho synthetic gate và
base Worker luôn được khôi phục. Xem
[Week 4 T4.21 crash/lease recovery](docs/week4-t4.21-crash-lease-recovery.md).

PostgreSQL integration tests và Docker E2E chạy được local; KiRa/Qwen dùng mock.
Nghiệm thu với endpoint nội bộ thật vẫn là gate riêng, xem
[Week 2 runbook và evidence](docs/week2-acceptance.md).

## KiRa contract assumptions

- `tokenExpirationTime` tạm được hiểu là TTL tính bằng giây và refresh sớm theo
  `KIRA_TOKEN_EXPIRY_SKEW_SECONDS`. Cần xác nhận lại với owner KiRa.
- HTTP 401/403 trước frame đầu sẽ invalidate token và retry đúng một lần.
- Stream kết thúc khi downstream connection đóng; không suy diễn `stream.stop`.
- Frame JSON hợp lệ nhưng chưa biết type vẫn được giữ nguyên để proxy ở Batch C.

## Yêu cầu

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## Thiết lập local

```powershell
Copy-Item .env.example .env
uv sync --all-groups
```

Điền credential thật vào `.env` local. File này đã bị Git ignore; không đưa Basic
credential hoặc token KiRa vào source code, commit, test fixture hay log.

Các lệnh kiểm tra:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Khởi động PostgreSQL local, apply migration và chạy integration test thật:

```powershell
docker compose up -d postgres
$env:DATABASE_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run alembic upgrade head
$env:POSTGRES_TEST_URL=$env:DATABASE_URL
uv run pytest -m postgres_integration --no-cov
Remove-Item Env:POSTGRES_TEST_URL
```

Khởi tạo pgvector memory schema sau khi cấu hình embedding endpoint/model/dimension:

```powershell
uv run python -m worker.memory_admin init
```

Lệnh này cần `MEMORY_ADMIN_DATABASE_URL` (hoặc fallback `MEMORY_DATABASE_URL`) có quyền
`CREATE EXTENSION`/schema. Nó probe `/v1/embeddings`, kiểm tra dimension thật rồi tạo/validate
hai collection `memory.memories` và `memory.memories_entities` cùng metadata/index. Chạy lại
idempotent; model, dimension, Mem0 version hoặc pgvector version lệch metadata sẽ fail closed.
Runtime service account chỉ cần DML và không được cấp quyền DDL.

Kiểm tra queue và requeue có chủ đích một dead job bằng operator CLI PostgreSQL-only:

```powershell
uv run kira-memory-jobs stats
uv run kira-memory-jobs list-dead --limit 50
uv run kira-memory-jobs requeue --event-id 00000000-0000-0000-0000-000000000000
```

CLI chỉ cần `DATABASE_URL` cùng cấu hình pool/timeout PostgreSQL; nó không khởi tạo Mem0 và không
cần KiRa, embedding hay memory-LLM. `list-dead` chỉ trả projection vận hành đã sanitize và bị giới
hạn tối đa 1.000 row. `requeue` chỉ tác động đúng một UUID đang ở trạng thái `dead`; event không tồn
tại hoặc không còn dead trả exit code 4 và không thay đổi dữ liệu.

Chạy riêng gate hoàn tất Worker Batch C trên PostgreSQL disposable:

```powershell
$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_memory_job_processing.py `
  tests/integration/postgres/test_memory_job_runner.py `
  tests/integration/postgres/test_memory_job_retention.py `
  tests/integration/postgres/test_memory_job_admin.py `
  tests/integration/postgres/test_memory_worker_acceptance.py --no-cov
Remove-Item Env:POSTGRES_TEST_URL
```

Khởi động stack synthetic Week 4 đầy đủ để chạy các gate Batch D:

```powershell
docker compose -f compose.week4.yaml build gateway
docker compose -f compose.week4.yaml up -d --no-build --wait
docker compose -f compose.week4.yaml ps -a
uv run python -m scripts.smoke_week4_async
uv run python -m scripts.smoke_week4_retry
uv run python -m scripts.smoke_week4_crash
```

Gateway ở `http://127.0.0.1:18000`, Worker ở `http://127.0.0.1:18001`; PostgreSQL và bốn mock
provider chỉ publish trên loopback. Stack dùng project `kira-context-week4`, credential synthetic
cố định và volume riêng; không phụ thuộc giá trị `.env`, không gọi endpoint thật và không chứa
Redis.

`DATABASE_URL` phải khớp `POSTGRES_DB`, `POSTGRES_USER` và `POSTGRES_PASSWORD` trong `.env`.
Gateway không tự chạy migration. Cấu hình hoặc schema sai làm startup fail; connection timeout
tạm thời chỉ đặt PostgreSQL ở degraded state và `/ready` vẫn trả 200.

PostgreSQL là conversation store duy nhất. Recent window không xóa full history và không có
inactivity TTL; `MAX_RECENT_MESSAGES` và token budget chỉ giới hạn context gửi tới rewriter.

## Context Builder và Query Rewriter (Week 3 Batch C1)

- `ContextBuilder` nhận history đã được store sắp xếp cũ → mới, không sort lại timestamp.
- Giữ tối đa `MAX_RECENT_MESSAGES` (mặc định 10) và `RECENT_CONTEXT_TOKEN_BUDGET` (3.000).
  Bỏ orphan assistant ở đầu window và loại turn cũ nhất theo nguyên nhóm; không truncate text.
- Estimator là `ceil(UTF-8 bytes / 4) + 8/message`, không phải số token Qwen chính xác.
  Current query và system prompt không tính vào recent budget; current query không bị sửa/trim.
- Ranked LTM giữ nguyên thứ tự từ provider, bị cap bởi `MEMORY_SEARCH_TOP_K` trong khoảng 1–10,
  không sort/dedup lại và chưa có token budget riêng trong baseline Week 3.
- Prompt v2 tách system instructions khỏi JSON LTM/recent/current untrusted data. Chỉ gửi nội dung
  memory, không gửi ID/score/metadata. Explicit current query thắng recent và LTM; recent thắng LTM
  khi xung đột; LTM chỉ được dùng khi liên quan và không phải instruction/authorization source.
  Rewriter vẫn không invent KPI/date/location/service, giữ nguyên query standalone/topic switch
  và phần reference chưa resolve được.
- `VllmQueryRewriterAdapter` dùng HTTPX client do caller quản lý và không tự retry.
  Contract là [vLLM Chat Completions](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/):
  `POST /v1/chat/completions`, `temperature=0`, `stream=false`, `max_tokens=256`.
- Khi tạo adapter cần `VLLM_BASE_URL` và `VLLM_MODEL` đúng tên model được serve; không có model
  hardcode. Base URL chấp nhận origin hoặc kết thúc bằng `/v1`. `VLLM_API_KEY` tùy chọn.
  Connect/read timeout mặc định 2s/8s; giới hạn output `VLLM_MAX_OUTPUT_CHARS=2048`.
- Timeout/connection/non-2xx/malformed output được map thành typed errors. Empty, oversized,
  truncated (`finish_reason=length`) hoặc tool-call response bị từ chối. Adapter không log dữ liệu.

Gateway khởi tạo adapter vLLM ở startup; bắt buộc cấu hình base URL và model nhưng không gọi
model để probe. Khi `LTM_ENABLED=false`, memory search được ghi nhận là bypass và flow giữ nguyên
Week 2. Khi bật, Gateway khởi tạo Mem0 từ cấu hình Week 3 và inject vào use case; rewriter chỉ bypass
nếu cả recent và LTM đều rỗng. PostgreSQL recent-read hoặc rewriter lỗi fallback current query
nguyên bản; lỗi riêng LTM vẫn cho phép Recent + Current tiếp tục. KiRa vẫn là dependency bắt buộc.

Test prompt bao phủ location/time/metric/reference/comparison, standalone, topic switch và
injection trong recent data. Đây là unit/HTTP contract tests với mock, **không chứng minh chất lượng
rewrite của Qwen thật**. Dev-case gate trên model nội bộ chỉ chạy khi endpoint khả dụng.

Chạy riêng checkpoint Batch C:

```powershell
uv run pytest tests/unit/application tests/contract/llm --no-cov
```

`--no-cov` chỉ dành cho focused test subset; full `uv run pytest` vẫn bắt buộc coverage ≥90%.

Chạy Week 3 B4 formation gate trên PostgreSQL/pgvector disposable:

```powershell
$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_memory_formation.py --no-cov
```

Gate này dùng deterministic in-process doubles cho embedding và memory LLM để kiểm tra pipeline,
DB persistence, metadata, dedup và isolation ổn định. Nó không thay thế semantic gate B2 trên model
nội bộ thật; policy quality vẫn là `NOT_RUN` nếu chưa cấu hình endpoint được phê duyệt.

Chạy Week 3 C4 cross-session gate deterministic:

```powershell
$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run pytest tests/integration/postgres/test_cross_session_recall.py --no-cov
```

Test đầu dùng PostgreSQL/pgvector và native Mem0 V3 thật, nhưng provider embedding/memory LLM
deterministic và vLLM HTTP mock để kết quả ổn định. Test semantic thứ hai mặc định skip. Chỉ chạy
với disposable database và các endpoint model nội bộ đã được phê duyệt:

```powershell
$env:RUN_CROSS_SESSION_EVAL="1"
uv run pytest tests/integration/postgres/test_cross_session_recall.py `
  -m "postgres_integration and memory_llm_integration" --no-cov
```

Xem contract, ma trận nghiệm thu và giới hạn evidence tại
[Week 3 C4 cross-session acceptance](docs/week3-c4-cross-session-acceptance.md).

### Chạy Gateway

Chạy Gateway sau khi đã cấu hình `.env`:

```powershell
uv run uvicorn app.presentation.api.main:app --host 0.0.0.0 --port 8000
```

Chạy smoke client từ terminal khác:

```powershell
uv run python scripts/smoke_gateway.py `
  --gateway-url http://127.0.0.1:8000 `
  --message "<approved KiRa Test question>"
```

Build và chạy Docker image versioned:

```powershell
docker build --build-arg APP_VERSION=0.3.0 -t kira-context:0.3.0 .
docker run -d --name kira-context-v3 --env-file .env -p 8000:8000 kira-context:0.3.0
docker ps --filter "name=kira-context"
```

## Gateway API baseline

`POST /chat` giữ request contract cũ, nội bộ bổ sung bounded recent context và query rewriting:

```json
{
  "session_id": "sess_xxx",
  "message": "Hưng Yên thì sao?"
}
```

Response là `text/event-stream`. KiRa frames được proxy nguyên payload dưới `data:`. Nếu lỗi
xảy ra sau khi response đã bắt đầu, Gateway phát `event: gateway_error` chứa `code`,
`message`, `correlation_id`, `retryable` rồi đóng stream. Lỗi KiRa trước stream trả JSON cùng
schema với HTTP 502; timeout trả HTTP 504.

- `GET /health`: liveness của process.
- `GET /ready`: dependency graph local đã khởi tạo; không probe KiRa. PostgreSQL connection
  outage tạm thời là degraded capability. Configuration/schema mismatch làm startup fail;
  nếu phát hiện mismatch lúc runtime, trả 503 đến khi schema được xác minh lại thành công.
- `GET /metrics`: recent count/estimated tokens, LTM search count/latency/result count, rewrite
  latency/outcome, degradation và write outcome.

Turn ID do Gateway sinh; client không được gửi `turn_id`/`user_id`. `KiRa /authenticate` chỉ
xác thực service account với KiRa, không được dùng làm danh tính end-user. Khi chưa có real auth,
local/test có thể bật `DEV_STATIC_IDENTITY_ENABLED`; cấu hình này bị từ chối ở production.
Chỉ persist khi downstream
EOF bình thường và có assistant text; lưu original user query + exact concatenated assistant text.
Không ghi partial turn khi lỗi hoặc disconnect được phát hiện. Write lỗi chỉ log/metric, không thêm
`gateway_error` vào response đã trả text. Xem runbook về giới hạn durability/cancellation.

`MEMORY_FORMATION_MESSAGE_LIMIT` là số message chẵn (mặc định 10), nhờ đó direct formation snapshot
chỉ chứa nguyên pair user/assistant và current completed turn luôn ở cuối. Harness gửi cả user lẫn
assistant message cho native Mem0 V3 để giữ đúng ngữ cảnh xác nhận/reference; không gọi
`update()`/`delete()` có chủ đích và không ép action provider thành ADD. Gateway chưa tự gọi
formation; retrieval runtime đã được wire ở C3, còn online formation/durable delivery nằm ngoài
scope Tuần 3.

`CONVERSATION_OPERATION_TIMEOUT_SECONDS=5` giới hạn tổng thời gian mỗi read/write (kể cả chờ pool).
Để chạy ngay stack cô lập với mock KiRa/vLLM và DB thật:

```powershell
docker compose -f compose.week2-smoke.yaml build gateway
docker compose -f compose.week2-smoke.yaml up -d --no-build
```

Docker Desktop hiển thị project `kira-context-week2`; Gateway ở `http://127.0.0.1:18000`.
Các credential cố định của stack này chỉ dành cho dữ liệu tổng hợp local, không dùng production.
Không copy tests/mock vào runtime image; Compose mount tests read-only cho hai mock services.

## Cấu trúc chính

```text
app/
├── presentation/    # HTTP API và transport schemas
├── application/     # Use cases và orchestration
├── domain/          # Models và ports độc lập framework
├── infrastructure/  # Adapter cho các external systems
└── config/           # Environment-backed settings
worker/               # Entry point cho memory worker ở các tuần sau
tests/                # Unit, integration và contract tests
```

Dependency direction: `presentation -> application -> domain`, còn infrastructure
implement các port của domain. Domain/application không import framework hoặc SDK hạ tầng.

## Branch strategy

- `main` luôn là baseline đã qua kiểm tra.
- Mỗi feature dùng branch ngắn hạn `feat/<feature>`; Week 2 dùng
  `feat/short-term-context`, Week 3 dùng `feat/long-term-memory` và Week 4 dùng
  `feat/async-memory-worker` trên checkpoint Week 3.
- Commit theo checkpoint có thể review; merge về `main` sau khi lint và test pass.
