# kira-context-memory

Context Gateway bổ sung ngữ cảnh hội thoại cho KiRa. Repository được tổ chức theo
Modular Service Architecture và Hexagonal Architecture để application/domain không phụ
thuộc trực tiếp vào FastAPI, HTTPX, Redis, Mem0 hoặc vLLM.

## Trạng thái

Batch A-D của Tuần 1 cung cấp Gateway baseline hoàn chỉnh để live smoke với KiRa Test.
Batch A-C của Tuần 2 bổ sung conversation infrastructure và các component rewrite độc lập:

- cấu trúc presentation, application, domain, infrastructure, config và worker;
- dependency/tooling bằng Python 3.11, `uv`, Ruff và pytest;
- settings đọc KiRa configuration từ environment;
- request/domain models tối thiểu;
- `KiraClientPort` cùng HTTPX adapter cho authenticate và chat streaming;
- token cache concurrency-safe và parser cho KiRa `data:` frames;
- `HandleChatUseCase`, FastAPI `POST /chat`, SSE proxy và health/readiness probes;
- Docker image non-root, automated test gate và manual smoke client.
- `ConversationStorePort` và message schema version 1 độc lập Redis SDK;
- Redis connection pool với timeout/health handling theo degraded policy;
- atomic append user/assistant, `turn_id` dedup, bounded 10-message window và sliding TTL.
- PostgreSQL source of truth với migration, transactional pair append, deterministic ordering
  và indexed recent read;
- Redis adapter được giữ làm optional cache candidate, không được Gateway chọn mặc định.
- ContextBuilder, estimated token budget, QueryRewriterPort, prompt v1 và vLLM HTTP adapter;
  chưa tích hợp các component này vào `/chat` (phần đó thuộc Batch D).

Automated test dùng mock transport vì laptop cá nhân không có route tới KiRa Test. T1.18 chỉ
được xác nhận sau khi chạy [live smoke runbook](docs/week1-smoke-test.md) trên PC công ty.

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

`DATABASE_URL` phải khớp `POSTGRES_DB`, `POSTGRES_USER` và `POSTGRES_PASSWORD` trong `.env`.
Gateway không tự chạy migration. Cấu hình hoặc schema sai làm startup fail; connection timeout
tạm thời chỉ đặt PostgreSQL ở degraded state và `/ready` vẫn trả 200.

Khởi động Redis 7.2 local và chạy integration test cho optional adapter:

```powershell
docker compose up -d redis
$env:REDIS_TEST_URL="redis://127.0.0.1:6379/15"
uv run pytest -m redis_integration --no-cov
Remove-Item Env:REDIS_TEST_URL
```

Test dùng key prefix ngẫu nhiên và chỉ dọn các key do chính test tạo; không `FLUSHDB`.

Redis recent store dùng các key cùng cluster hash slot:

```text
kira:session:{session_id}:messages
kira:session:{session_id}:seen_turns
```

`REDIS_SESSION_TTL_SECONDS`, `MAX_RECENT_MESSAGES` và pool/timeouts lấy từ environment.
Redis không được wire vào Gateway dù `REDIS_URL` có cấu hình. Batch B mới chỉ cung cấp
PostgreSQL infrastructure; `HandleChatUseCase` sẽ đọc/ghi conversation ở Batch D.

## Context Builder và Query Rewriter (Week 2 Batch C)

- `ContextBuilder` nhận history đã được store sắp xếp cũ → mới, không sort lại timestamp.
- Giữ tối đa `MAX_RECENT_MESSAGES` (mặc định 10) và `RECENT_CONTEXT_TOKEN_BUDGET` (3.000).
  Bỏ orphan assistant ở đầu window và loại turn cũ nhất theo nguyên nhóm; không truncate text.
- Estimator là `ceil(UTF-8 bytes / 4) + 8/message`, không phải số token Qwen chính xác.
  Current query và system prompt không tính vào recent budget; current query không bị sửa/trim.
- Prompt v1 tách system instructions khỏi JSON recent/current untrusted data. Chỉ rewrite;
  explicit current query thắng context, không invent KPI/date/location/service, giữ nguyên query
  standalone/topic switch và phần reference chưa resolve được.
- `VllmQueryRewriterAdapter` dùng HTTPX client do caller quản lý và không tự retry.
  Contract là [vLLM Chat Completions](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/):
  `POST /v1/chat/completions`, `temperature=0`, `stream=false`, `max_tokens=256`.
- Khi tạo adapter cần `VLLM_BASE_URL` và `VLLM_MODEL` đúng tên model được serve; không có model
  hardcode. Base URL chấp nhận origin hoặc kết thúc bằng `/v1`. `VLLM_API_KEY` tùy chọn.
  Connect/read timeout mặc định 2s/8s; giới hạn output `VLLM_MAX_OUTPUT_CHARS=2048`.
- Timeout/connection/non-2xx/malformed output được map thành typed errors. Empty, oversized,
  truncated (`finish_reason=length`) hoặc tool-call response bị từ chối. Adapter không log dữ liệu.

Batch C không khởi tạo vLLM ở startup, không đổi `/chat`, SSE hoặc `/ready`. Settings vLLM có thể
để trống đến khi wiring Batch D. QueryRewriterPort không tự thực hiện fallback; đó là trách nhiệm
orchestration ở Batch D.

Test prompt bao phủ location/time/metric/reference/comparison, standalone, topic switch và
injection trong recent data. Đây là unit/HTTP contract tests với mock, **không chứng minh chất lượng
rewrite của Qwen thật**. Dev-case gate trên model nội bộ được giữ cho Batch D khi endpoint khả dụng.

Chạy riêng checkpoint Batch C:

```powershell
uv run pytest tests/unit/application tests/contract/llm --no-cov
```

`--no-cov` chỉ dành cho focused test subset; full `uv run pytest` vẫn bắt buộc coverage ≥90%.

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
docker build --build-arg APP_VERSION=0.1.0 -t kira-context:0.1.0 .
docker run -d --name kira-context --env-file .env -p 8000:8000 kira-context:0.1.0
docker ps --filter "name=kira-context"
```

## Gateway API baseline

`POST /chat` nhận current query, chưa thêm recent context, memory hoặc query rewriting:

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
  outage tạm thời là degraded capability, còn configuration/schema mismatch làm startup fail.

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
  `feat/short-term-context` trên baseline Week 1.
- Commit theo checkpoint có thể review; merge về `main` sau khi lint và test pass.
