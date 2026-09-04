# kira-context-memory

Context Gateway bổ sung ngữ cảnh hội thoại cho KiRa. Repository được tổ chức theo
Modular Service Architecture và Hexagonal Architecture để application/domain không phụ
thuộc trực tiếp vào FastAPI, HTTPX, PostgreSQL SDK hoặc vLLM.

## Trạng thái

Batch A-D của Tuần 1 cung cấp Gateway baseline hoàn chỉnh để live smoke với KiRa Test.
Batch A-D của Tuần 2 tích hợp short-term context qua PostgreSQL và vLLM:

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

`DATABASE_URL` phải khớp `POSTGRES_DB`, `POSTGRES_USER` và `POSTGRES_PASSWORD` trong `.env`.
Gateway không tự chạy migration. Cấu hình hoặc schema sai làm startup fail; connection timeout
tạm thời chỉ đặt PostgreSQL ở degraded state và `/ready` vẫn trả 200.

PostgreSQL là conversation store duy nhất. Recent window không xóa full history và không có
inactivity TTL; `MAX_RECENT_MESSAGES` và token budget chỉ giới hạn context gửi tới rewriter.

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

Gateway khởi tạo adapter vLLM ở startup; bắt buộc cấu hình base URL và model nhưng không gọi
model để probe. Empty/fully-trimmed recent bỏ qua rewriter. PostgreSQL recent-read hoặc rewriter
lỗi sẽ fallback current query nguyên bản. KiRa vẫn là dependency bắt buộc.

Test prompt bao phủ location/time/metric/reference/comparison, standalone, topic switch và
injection trong recent data. Đây là unit/HTTP contract tests với mock, **không chứng minh chất lượng
rewrite của Qwen thật**. Dev-case gate trên model nội bộ chỉ chạy khi endpoint khả dụng.

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
docker build --build-arg APP_VERSION=0.2.0 -t kira-context:0.2.0 .
docker run -d --name kira-context-v2 --env-file .env -p 8000:8000 kira-context:0.2.0
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
- `GET /metrics`: recent count/estimated tokens, rewrite latency/outcome, degradation và write outcome.

Turn ID do Gateway sinh; client không được gửi `turn_id`/`user_id`. Chỉ persist khi downstream
EOF bình thường và có assistant text; lưu original user query + exact concatenated assistant text.
Không ghi partial turn khi lỗi hoặc disconnect được phát hiện. Write lỗi chỉ log/metric, không thêm
`gateway_error` vào response đã trả text. Xem runbook về giới hạn durability/cancellation.

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
  `feat/short-term-context` trên baseline Week 1.
- Commit theo checkpoint có thể review; merge về `main` sau khi lint và test pass.
