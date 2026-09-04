# Week 2 — PostgreSQL short-term context

## Phạm vi đã triển khai

`POST /chat → PostgreSQL recent → ContextBuilder → vLLM rewrite → KiRa SSE → PostgreSQL completed turn`

- Batch A Redis (`8d6497d`) giữ nguyên adapter/config/schema/Lua và regression tests;
  không được wire vào Gateway, không có auto-switch sang Redis khi PostgreSQL lỗi.
- Batch B PostgreSQL (`542d177`) giữ schema/migration, indexed recent read, transactional pair,
  dedup và full persistent history. Thêm wrapper quản lý runtime health/schema ở Batch D.
- Batch C (`7c9d64c`) giữ ContextBuilder, estimator, rewriter port/prompt/HTTP contract.
- Batch D nối orchestration, persistence-on-completion, degraded paths, metrics và E2E.
- Không thêm Mem0/LTM, exact tokenizer, full-history API, queue, Redis Stream hoặc worker logic.

## Invariants và giới hạn

- Recent mặc định 10 messages, 3.000 **estimated** tokens; current query không tính vào budget.
  PostgreSQL không trim/xóa history khi đọc. Redis TTL không áp dụng cho PostgreSQL.
- History rỗng hoặc bị loại hết bởi budget thì không gọi rewriter. Read/rewrite lỗi dùng current
  query nguyên bản. Không có answer fallback: KiRa lỗi vẫn trả typed JSON/SSE Week 1.
- Original query là message sau validation API sẵn có (strip outer whitespace); không phải rewrite.
  Assistant text được concatenate đúng thứ tự, không strip/normalize trước khi lưu.
- UUID turn ID sinh nội bộ mỗi request; callback chỉ một lần, sau downstream EOF bình thường,
  có text không-whitespace. Không ghi khi pre/midstream error, manual close hoặc cancellation.
  ASGI disconnect/send failure đóng iterator và HTTP connection; không shield persistence.
- Ghi transactional trong request, sau khi các chunk text đã được yield và trước SSE transport EOF;
  tối đa `CONVERSATION_OPERATION_TIMEOUT_SECONDS=5`. Write lỗi chỉ log/metric, không `gateway_error`.
  Không có durable retry/outbox: crash hoặc write failure có thể mất turn. Nếu DB đã commit đúng lúc
  client ngắt kết nối, không thể bảo đảm rollback hay chứng minh client đã nhận tất cả bytes.
- Dedup áp dụng cùng internal `turn_id`; retry một HTTP request mới sẽ sinh turn mới. Chưa có
  public idempotency key. Các request cùng session chạy đồng thời có thể đọc cùng snapshot;
  thứ tự lưu do completed append/DB lock quyết định, không sort wall-clock timestamps.
- Chưa thêm authentication/session ownership trong Week 2. Chỉ triển khai sau trusted gateway;
  cần enforce session ownership trước khi cung cấp nhiều người dùng/tenant ở production.

## Readiness và observability

| Tình huống | Hành vi |
| --- | --- |
| Thiếu/sai DATABASE_URL, sai schema revision lúc startup | Startup fail; không tự migrate |
| Thiếu VLLM_BASE_URL/VLLM_MODEL, invalid adapter config | Startup fail; model name không hardcode |
| PostgreSQL không kết nối được lúc startup | Degraded, `/ready` 200; xác minh schema trước khi dùng sau recovery |
| PostgreSQL read hoặc vLLM lỗi runtime | Original query xuống KiRa, `/ready` vẫn 200 |
| Schema/config lỗi được phát hiện runtime | Query degrade, `/ready` 503 đến khi xác minh schema thành công |
| Persistence lỗi | Giữ response KiRa, log + counter; không sinh câu trả lời giả |

`/health` chỉ liveness. `/ready` không gọi KiRa/vLLM/PG mỗi probe. Không background probe/queue.
Khi store degraded hoặc misconfigured, request kế tiếp sẽ thử validate schema lại có timeout;
schema error đã biết không bị che bởi connection outage tiếp theo.

`GET /metrics` (registry riêng mỗi app/process):

- `kira_context_recent_messages`: histogram số message **sau trimming**; 0 khi read failure.
- `kira_context_estimated_recent_tokens`: UTF-8 estimate, không phải tokenizer Qwen.
- `kira_context_rewrite_total{outcome=success|error|bypass}`.
- `kira_context_rewrite_duration_seconds{outcome=success|error}`; bypass không đo latency.
- `kira_context_degraded_total{dependency,operation}`: `postgres_read`, `postgres_write`, `rewriter`.
- `kira_conversation_write_total{outcome=inserted|duplicate|error}`; partial/empty turn không ghi.

Không có session/turn/correlation ID làm metric labels. Deploy một Uvicorn process/container;
multiworker Prometheus aggregation không nằm trong scope hiện tại.
App JSON logs chỉ allowlist `correlation_id`, `operation`, `dependency`, `error_class`, `fallback_mode`;
không serialize exception trace, query, prompt, content, token hoặc credential.
Uvicorn access logs thuộc server logging riêng; không đặt dữ liệu nhạy cảm trong URL.

## Chạy acceptance local (không cần credential nội bộ)

Stack cô lập `kira-context-week2` dùng dữ liệu tổng hợp và credential local-only, không đọc `.env`
cho các giá trị service. Không thay/xóa container Week 1 hoặc dữ liệu project khác.

```powershell
uv sync --all-groups
docker compose -f compose.week2-smoke.yaml build gateway
docker compose -f compose.week2-smoke.yaml up -d --no-build
docker compose -f compose.week2-smoke.yaml ps

$env:POSTGRES_TEST_URL="postgresql+asyncpg://kira:local-smoke-only@127.0.0.1:15432/kira_smoke"
$env:REDIS_TEST_URL="redis://127.0.0.1:16379/15"
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python -m scripts.smoke_week2
Remove-Item Env:POSTGRES_TEST_URL
Remove-Item Env:REDIS_TEST_URL
```

Gateway: `http://127.0.0.1:18000`; `/health`, `/ready`, `/metrics` và `/docs` có thể mở trên máy.
Container `migrate` exit 0 là bình thường: đây là one-shot job, không phải service bị crash.
Mock servers chỉ mount tests read-only trong Compose; image runtime không chứa tests/.env/.git/cache.

Test DB URL phải trỏ DB dành riêng để kiểm thử. Tests apply migration rồi chỉ xóa những session/key
ngẫu nhiên của chính test; không `FLUSHDB` hoặc truncate conversation của người dùng.
Smoke client cũng chỉ dọn sessions do nó vừa tạo; evidence in ra case ID/outcome/count, không content.
Dừng stack mà không xóa dữ liệu/container:

```powershell
docker compose -f compose.week2-smoke.yaml stop
```

## Gate trên KiRa/Qwen nội bộ — cần thực hiện trước merge

Local mock **không chứng minh** model thật tuân thủ no-invention/no-answer/prompt-injection rules.
Không tự coi semantic test pass chỉ vì fake rewriter trả expected output.

1. Cấu hình `.env` trên máy có route nội bộ: KiRa credential, DATABASE_URL test đã migrate,
   VLLM_BASE_URL, đúng VLLM_MODEL, API key nếu cần. Không dùng credential local Compose.
2. Chạy `uv run python -m scripts.check_live_rewriter`. Script gửi 8 case tổng hợp trong
   `tests/support/week2_cases.py`, không gửi lịch sử thật. In case ID/outcome/prompt version,
   không in model output hoặc endpoint/secret. Exit khác 0 nếu dependency lỗi hoặc cần review.
3. Comparator chỉ normalize Unicode/case/whitespace/dấu kết câu. `review_required` có thể là
   paraphrase đúng; owner review output trong môi trường được phép trước khi cập nhật expected.
   Không relax test để che invent KPI/date/location hoặc model trả lời thay vì rewrite.
4. Chạy Gateway thật, gửi seed rồi follow-up bằng cùng session qua smoke client Week 1.
   Đối chiếu query xuống KiRa trong môi trường nội bộ, original/final text trong PG, metrics
   rewrite/degrade. Test topic switch/standalone/injection chỉ với dữ liệu được phê duyệt.
5. Evidence chỉ lưu timestamp, build/commit, model deployment alias được phép, prompt v1,
   case ID, pass/fail, latency, correlation ID cần thiết. Không commit raw prompts/responses/secrets.

Ví dụ CLI cho bước 4: dùng cùng `--session-id` cho hai lần chạy. Công cụ Week 1 in response ra
terminal; không chụp/copy transcript nếu dữ liệu nghiệp vụ nhạy cảm.

```powershell
uv run python scripts/smoke_gateway.py --session-id approved-dev-session --message "<approved seed>"
uv run python scripts/smoke_gateway.py --session-id approved-dev-session --message "<approved follow-up>"
```

## Evidence local — 2026-09-04

- Ruff lint/format: pass.
- PostgreSQL 16 + Redis 7.2 thật, full pytest: **280 pass, 0 skip; coverage 97,53%**.
- Docker image `kira-context:0.2.0`: build pass, non-root runtime.
- Socket E2E: 8/8 cases pass; mỗi case xác minh original query + exact assistant text trong PG,
  hash query rewrite thực sự nhận tại mock KiRa; health/readiness/metrics đều HTTP 200.
- API integration tạo lại Gateway giữa hai request vẫn lấy được context từ PG.
- Cancellation rollback trên PG thật, dedup/order/index/persistence và Redis regression pass.
- KiRa Test/Qwen nội bộ thật: **NOT RUN** — cần endpoint/model/credential và route nội bộ.
  Local checkpoint hoàn tất không đồng nghĩa gate nội bộ đã đạt.
