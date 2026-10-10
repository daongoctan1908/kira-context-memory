# kira-context-memory

Context Gateway và long-term memory service cho chatbot KiRa. Hệ thống gồm Gateway FastAPI,
PostgreSQL/pgvector, Worker xử lý memory bất đồng bộ, Mem0 tùy biến và frontend React.

## Trạng thái hiện tại

Luồng sản phẩm local đã có:

- auth nội bộ bằng username/password, session cookie và CSRF;
- conversation API, POST-SSE chat, idempotent retry và lịch sử phân trang;
- recent context, long-term retrieval, rewrite rồi chuyển tiếp KiRa SSE;
- memory job ghi atomically cùng turn và Worker formation bất đồng bộ;
- xóa conversation kèm jobs, vector memories và receipts thuộc conversation đó;
- OpenTelemetry/Langfuse tracing fail-open và metrics low-cardinality;
- frontend React cho login, conversation list, chat, retry và deletion-pending;
- Docker product E2E với các provider mock explicit.

Chất lượng chính thức chưa được công bố. Dataset hiện đã materialize/review và ở trạng thái
`benchmark_ready`; trên PC công ty cần validate bản freeze, chỉ materialize phần pending nếu có.
Control/candidate chính thức chạy với internal models trên K8s. Xem:

- [Master implementation plan](docs/production-readiness-plan.md)
- [Company PC AI handoff](docs/company-pc-ai-handoff.md)
- [Benchmark contract](docs/benchmark-contract.md)
- [Internal K8s acceptance](docs/benchmark-k8s-acceptance.md)
- [Production deployment handoff](docs/production-deployment.md)

D0 conflict experiments đã được bỏ; formation dùng luồng Mem0 ADD-only trước D1.
Dataset `kira_ltm_v1` và holdout `d0_holdout_v1` vẫn giữ nguyên bản freeze. Module
`evaluation.d0_holdout` chỉ còn kiểm tra tính toàn vẹn của holdout, không gọi model.

## Kiến trúc

```text
Frontend
   ↓ authenticated POST-SSE
Gateway
   ├─ PostgreSQL: users, sessions, conversations, turns, memory jobs
   ├─ Mem0/pgvector: LTM retrieval
   ├─ Rewriter: current + recent + retrieved memory
   └─ KiRa: answer SSE

PostgreSQL memory_jobs
   ↓ lease/claim/retry
Worker
   └─ Mem0: extract → embed → deduplicate → persist → receipt
```

Dependency direction của backend là `presentation → application → domain`; infrastructure chỉ
implement các port của domain. Gateway và Worker dùng cùng database để completion/deletion giữ
được transaction và ownership fencing. Contract idempotency và crash recovery nằm trong
[product E2E](docs/product-e2e.md#memory-formation-idempotency).

## Yêu cầu

- Python 3.11
- [uv](https://docs.astral.sh/uv/)
- Node.js 22.12+
- pnpm 10 qua Corepack
- Docker Desktop cho PostgreSQL và E2E

## Thiết lập local

```powershell
Copy-Item .env.example .env
uv sync --all-groups
```

Không commit `.env`, API key, KiRa credential hoặc nội dung nội bộ. Chạy backend gates:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Pytest mặc định yêu cầu coverage backend/evaluation tối thiểu 90%.

Frontend:

```powershell
Set-Location frontend
corepack pnpm install --frozen-lockfile
corepack pnpm lint
corepack pnpm typecheck
corepack pnpm test
corepack pnpm build
corepack pnpm exec playwright install chromium
corepack pnpm test:e2e
Set-Location ..
```

## Local product acceptance

[compose.product.yaml](compose.product.yaml) chạy auth, PostgreSQL, migration/init, Gateway,
Worker, frontend production image và bốn provider mock deterministic. Nó không gọi KiRa hoặc model
thật.

```powershell
docker compose -f compose.product.yaml config --quiet
docker compose -f compose.product.yaml up -d --build --wait
uv run python -m scripts.local.smoke_product_stack
uv run python -m scripts.local.smoke_product_e2e
```

Mở `http://127.0.0.1:18080` với tài khoản disposable:

```text
username: local-admin
password: local-product-only
```

Xóa toàn bộ state synthetic:

```powershell
docker compose -f compose.product.yaml down -v
```

Chi tiết: [product E2E](docs/product-e2e.md). Pull request gate được định nghĩa trực tiếp trong
[CI workflow](.github/workflows/ci.yml). Các cổng local có thể đổi bằng nhóm biến
`PRODUCT_*_PORT`; khi đổi frontend port, Compose đồng thời tạo đúng allowed origin cho auth.

## PostgreSQL local

[compose.yaml](compose.yaml) chỉ dựng pgvector/PostgreSQL tối thiểu cho migration và integration
test:

```powershell
docker compose up -d postgres
$env:DATABASE_URL="postgresql+asyncpg://kira:replace_me@127.0.0.1:5432/kira_context"
uv run alembic upgrade head
$env:POSTGRES_TEST_URL=$env:DATABASE_URL
uv run pytest -m postgres_integration --no-cov
Remove-Item Env:POSTGRES_TEST_URL
```

Khởi tạo/validate schema Mem0 sau khi đã cấu hình embedding:

```powershell
uv run python -m worker.memory_admin init
```

## Auth và operator CLI

Production không có public signup. Quản lý tài khoản bằng CLI:

```powershell
uv run kira-auth-admin create --username alice
$env:NEW_KIRA_PASSWORD | uv run kira-auth-admin reset-password --username alice --password-stdin
uv run kira-auth-admin disable --username alice
uv run kira-auth-admin enable --username alice
uv run kira-auth-admin revoke-sessions --username alice
uv run kira-auth-admin list --limit 100
```

Không truyền password trong command-line argument. Production phải dùng `AUTH_ENABLED=true`, HTTPS,
`AUTH_COOKIE_SECURE=true` và origin frontend chính xác.

Memory-job operations:

```powershell
uv run kira-memory-jobs stats
uv run kira-memory-jobs list-dead --limit 50
uv run kira-memory-jobs requeue --event-id 00000000-0000-0000-0000-000000000000
uv run kira-conversations purge-pending --limit 100
```

## OpenAI runtime smoke

[compose.openai.yaml](compose.openai.yaml) dùng KiRa mock nhưng gọi OpenAI thật cho rewrite,
formation và embedding. Đây chỉ là dependency/runtime smoke trên dữ liệu synthetic, không phải
canonical benchmark.

```powershell
Copy-Item evaluation/openai.env.example .env.openai.local
notepad .env.openai.local
uv run --frozen python -m scripts.local.openai_stack up
```

Runbook nằm trong [local product acceptance](docs/product-e2e.md#openai-runtime-smoke).

## Local observability và Langfuse

K8s: [bộ YAML và thứ tự triển khai](deploy/k8s/DEPLOY.md),
[Langfuse và metrics](deploy/k8s/observability/README.md). Gateway/Worker chỉ ghi
metrics qua OTel; `/metrics` cũ đã bỏ. Metrics vận hành dùng một Prometheus/Grafana
đã chọn; Langfuse giữ chức năng trace các bước AI. Dockerfile backend/frontend
đã cập nhật và cả hai image `0.4.1-cbea254a95b2` đã build, probe local ngày
2026-10-09; xem [receipt](docs/evidence/product-dockerfile-rebuild-2026-10-09.json).
Chưa export/publish. Bộ image ngày 2026-10-08 còn code metrics cũ.
Review replica, CPU/RAM, connection budget và Longhorn tại
[CAPACITY.md](deploy/k8s/CAPACITY.md).

Telemetry là tùy chọn: `OTEL_ENABLED=false` mặc định, chat và Worker vẫn chạy bình thường.
Khi bật OTel, `OTEL_CAPTURE_CONTENT_ENABLED=false` mặc định chỉ ghi timing, outcome, model,
usage và IDs; đặt `true` khi cần debug prompt/output đã mask. Langfuse không phải dependency
readiness hay điều kiện để chạy benchmark.

OTel Collector là overlay fail-open của product stack. Prometheus/Grafana chỉ chạy khi bật profile
`metrics`:

```powershell
docker compose -f compose.product.yaml -f compose.observability.yaml up -d --build --wait
```

Langfuse local acceptance:

Overlay này pin Langfuse `3.225.7` bằng image digest và bật capture cho dữ liệu giả lập.
Nếu môi trường đã đặt `OTEL_CAPTURE_CONTENT_ENABLED=false`, đặt lại `true` khi chạy bài smoke
có kiểm tra input/output:

```powershell
$env:OTEL_CAPTURE_CONTENT_ENABLED = "true"
$compose = @(
  "-f", "compose.product.yaml",
  "-f", "compose.observability.yaml",
  "-f", "compose.langfuse.yaml"
)

docker compose @compose config --quiet
docker compose @compose up -d --build --wait
uv run python -m scripts.local.smoke_langfuse
```

Để kiểm tra chế độ chỉ ghi timing/metadata, khởi động lại Gateway và Worker với
`OTEL_CAPTURE_CONTENT_ENABLED=false`, rồi chạy smoke với `--no-capture-content`.
Bài kiểm tra này vẫn yêu cầu model/token usage và xác nhận mọi input/output đều vắng mặt.

Mở `http://127.0.0.1:13001`. Credential mặc định chỉ dành cho local acceptance:
`local@example.invalid` / `local-acceptance-only`. Trace tìm được bằng `correlation_id`, `turn_id`,
`event_id` và `origin_trace_id`; telemetry outage không được làm hỏng chat hoặc Worker.

Kiến trúc, data policy và metric semantics được gom trong
[observability contract](docs/observability-architecture.md).

## Chạy trực tiếp

Sau khi cấu hình `.env` và apply migration:

```powershell
uv run uvicorn app.presentation.api.main:app --host 0.0.0.0 --port 8000
uv run python -m worker.main
```

Frontend dev chạy trong `frontend/` bằng `corepack pnpm dev`; Vite proxy `/api` về Gateway. Build
production frontend bằng [frontend/Dockerfile](frontend/Dockerfile).

## KiRa contract assumptions

- `tokenExpirationTime` đang được hiểu là TTL giây và refresh sớm theo
  `KIRA_TOKEN_EXPIRY_SKEW_SECONDS`.
- HTTP 401/403 trước frame đầu invalidate token và retry đúng một lần.
- Stream kết thúc khi downstream đóng; không tự suy diễn `stream.stop`.
- Frame JSON hợp lệ nhưng chưa biết type được proxy nguyên dạng.

Các assumption này phải được xác nhận lại với KiRa thật trên PC công ty.

## Cấu trúc repository

```text
app/             backend presentation/application/domain/infrastructure
worker/          memory worker và operator entrypoints
frontend/        React/Vite chatbot UI
migrations/      Alembic application schema
evaluation/      benchmark compiler, runner, scoring và evidence
dataset/         canonical KiRa LTM dataset
deploy/          local observability assets
scripts/
  benchmark/     dataset, benchmark và handoff entrypoints
  local/         local stacks, smoke tests và diagnostics
tests/           unit, integration, contract và vendor regressions
packages/        vendored viettel-mem0
docs/            active runbooks, contracts và plans
```
