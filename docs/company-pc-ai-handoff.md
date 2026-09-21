# Bàn giao cho AI trên PC công ty — KiRa LTM benchmark

Tài liệu này là nguồn hướng dẫn độc lập cho một AI khác tiếp quản repository trên PC công ty. Hãy
đọc hết tài liệu trước khi sửa code hoặc gọi API. Không được suy diễn một bước là `PASS` nếu chưa có
artifact chứng minh. Không được commit secret, raw KiRa checkpoint/response, prompt/response provider
hoặc database dump.

## 1. Mục tiêu và flow đã chốt

```text
Laptop cá nhân
  -> phát triển code, deterministic tests, synthetic/OpenAI experiments không canonical

PC công ty
  -> KiRa thật materialize canonical dataset
  -> human review + freeze dataset
  -> OpenAI + KiRa full technical acceptance (không promotion)
  -> build exact runtime/eval images
  -> đóng gói handoff

VDI
  -> verify/load hoặc publish đúng image digest

Kubernetes
  -> internal extraction/embedding/rewrite/judge + KiRa thật
  -> discovery/tuning loop
  -> freeze candidate
  -> official paired confirmation benchmark
```

Benchmark chất lượng chính thức **chỉ** chạy ở Kubernetes với internal models. Kết quả OpenAI trên PC
là diagnostic/technical acceptance, không dùng để chọn candidate thắng. Langfuse/OTel chỉ trace và
debug; tuyệt đối không dùng làm scorer.

## 2. Trạng thái repository khi bàn giao

Nhánh làm việc là `main`. Trước khi thao tác, tự xác minh thay vì tin SHA ghi trong tài liệu:

```powershell
git status --short
git branch --show-current
git log -12 --oneline
git remote -v
```

Các mốc Phase 4 đã có trong lịch sử gần nhất:

```text
3623407  PC benchmark profile và crash-safe runner foundation
b256a0f  KiRa materialization preflight
9e86cb6  hash-bound human review/freeze
924b8b9  exact dynamic image set
8f4a927  bind eval image vào đúng variant runtime
619d5db  PC provider/preflight freeze
a658a55  technical PC acceptance gate
2e41695  full-corpus mock runner + fail-closed live profile
b184601  T4.6 handoff evidence/bundle + tài liệu PC này
7911a8f  đồng bộ tài liệu handoff theo revision Phase 4 gần nhất
```

Nếu remote chưa có commit mới nhất, dừng và yêu cầu chuyển/push đúng commit. Không tự tái tạo thay đổi
từ tài liệu này trên một revision cũ.

### Dataset hiện tại trước khi lên PC

`dataset/kira_ltm_v1/manifest.json` trên laptop vẫn cố ý ở trạng thái:

```text
dataset_version = 1.0.0-draft.2
status = contract_frozen
materialization_status = pending
review.status = draft
external_provider_allowed = false
```

Workload dự kiến:

- 80 KiRa queries duy nhất;
- 140 assistant fills;
- 54 final-QA answers còn thiếu;
- 4 bundles `conv01`…`conv04`;
- một canonical full corpus, không chia dev/holdout.

Đó không phải lỗi. Các field chỉ được chuyển sang materialized/reviewed/benchmark-ready sau khi KiRa
thật và human review hoàn tất trên PC.

### Phần đã test được trên laptop

- validator/compiler và deterministic scorers;
- Formation Precision/Recall/F1, Retrieval Recall@3/MRR@10;
- rewrite constraint scorer + semantic judge contract;
- final-QA semantic/task-success contract;
- safety hard-fail semantics;
- crash-safe case ledger, resume, audit, timing;
- T4.1 materialization checkpoint/apply contract;
- T4.2 immutable review packet/freeze contract;
- T4.3 dynamic exact-image provenance logic;
- T4.4 sanitized provider/preflight freeze contract;
- T4.5 technical acceptance checker;
- T4.6 hash-bound handoff evidence/export/import logic;
- mock runner đủ bốn suites và không claim quality;
- native runner in-process cho formation/retrieval/rewrite/cross-session;
- run-owned PostgreSQL/Mem0 isolation, owner marker, exact cleanup và crash resume;
- unit tests cho provider binding, suite dispatch, ownership ledger và interrupted attempt.
- global human-audit budget `ceil(10% × semantic PASS/FAIL)` stratified theo
  variant/suite/verdict/bundle, cộng toàn bộ `UNCERTAIN` và deterministic conflicts;
- offline official-release tooling: internal run scorecard, three-pair confirmation gate,
  performance reviewer verdict và promotion/keep-control evidence;
- canonical production plan đã tách rõ PC technical acceptance khỏi K8s official benchmark.

Checkpoint này chỉ chạy validation offline, chưa build image thật và chưa tạo live KiRa/OpenAI PC
artifact nào trên laptop.

> Trước khi chuyển sang PC phải push và ghi lại exact handoff SHA. Clean checkout chỉ hợp lệ khi
> commit đó có đủ các file native runner/audit/release tooling liệt kê bên dưới; không dùng riêng
> revision cũ `7911a8f` để chạy.

### Validation snapshot trên laptop (2026-09-21)

Checkout bàn giao phải chứa đủ bốn file native mới sau; nếu thiếu thì revision đang dùng chưa phải
revision Phase 4 hoàn chỉnh:

```text
evaluation/native_executor.py
evaluation/native_runtime.py
tests/unit/evaluation/test_native_executor.py
tests/unit/evaluation/test_native_runtime.py
```

Kết quả kiểm tra gần nhất trên `main` (`3be5cb0c82b9305532948a85699be5acdb6d0213`):

```text
uv run ruff check .                 PASS
frontend: pnpm test                 PASS (30 tests)
frontend: pnpm build                PASS
uv run pytest -q                    1207 passed, 99 skipped; exit 1
coverage                            88.47% (configured gate >= 90%)
git diff --check                    PASS
Docker live integration             NOT_RUN — không thuộc checkpoint offline này
KiRa/OpenAI/PostgreSQL live run      NOT_RUN — chỉ thực hiện trên PC công ty
```

`pytest` không có test functional fail, nhưng **coverage gate đang FAIL** do product code mới tăng
phạm vi đo. Đây là blocker chất lượng còn mở, không được viết thành PASS trên PC. Có thể dùng
`uv run pytest -q --no-cov` chỉ để tách lỗi functional khỏi lỗi coverage khi điều tra; lệnh đó không
thay thế gate 90% và không là acceptance evidence. Các test skip cũng không được tự diễn giải là PASS.
Trên PC, chạy lại full suite khi Docker đã bật để các integration test có dependency thật được thực
thi; ghi outcome thật trước materialization/preflight/live run.

### Khoảng trống còn lại là live evidence, không còn là wiring giả

Native runner đã được nối vào `pc_openai_acceptance` và `internal_test`. Nó bắt buộc đủ đúng bốn
suite, `formation_mode=persistent`, explicit providers, run-owned schema và Worker ngoài phải dừng.
Laptop chỉ xác minh được code/tests; chưa có KiRa thật, provider PC, image build hay full-corpus
artifact. Vì vậy mọi live checkpoint ở các mục sau vẫn là `NOT_RUN` cho tới khi thực hiện trên PC.

## 3. Kiến trúc cần giữ nguyên

- Gateway nhận `/chat`, tạo `correlation_id` riêng của app; OTel có `trace_id` riêng.
- `turn_id` định danh conversation turn; `event_id` định danh memory job.
- PostgreSQL giữ conversations/messages và durable `memory_jobs` queue.
- Worker claim/retry job và gọi vendored Mem0 formation.
- Mem0 dùng OpenAI-compatible extraction + embedding, pgvector persistence và formation receipt.
- Gateway retrieval -> rewrite -> KiRa SSE cho Session B.
- OTel/Langfuse fail-open và không được thay đổi business outcome.

Không nhập `trace_id` với `correlation_id`. Trace/log phải có thể chứa cùng lúc:

```text
trace_id, correlation_id, turn_id, event_id, origin_trace_id
```

Không sửa sâu vendored Mem0 nếu chưa có benchmark chứng minh. Không thêm expiration, validity,
supersession hoặc temporal reasoning engine vào scope này.

## 4. Bootstrap repository và chuẩn bị PC

### 4.1 Chọn đúng một nguồn source

Ưu tiên clone/pull từ `origin`. Nếu mạng công ty không truy cập được GitHub, dùng Git bundle đã được
tạo từ đúng handoff commit. Không copy lẻ source folder, `.venv`, `artifacts/`, Docker volume hoặc
`.env` giữa hai máy.

**Từ origin:**

```powershell
git clone https://github.com/daongoctan1908/kira-context-memory.git C:\Code\kira-context-memory
Set-Location C:\Code\kira-context-memory
git switch main
git pull --ff-only origin main
```

**Từ bundle:**

```powershell
git bundle verify C:\Transfer\kira-context-memory.bundle
git clone C:\Transfer\kira-context-memory.bundle C:\Code\kira-context-memory
Set-Location C:\Code\kira-context-memory
git switch main
git remote set-url origin https://github.com/daongoctan1908/kira-context-memory.git
```

Ở thời điểm bàn giao này, HEAD tối thiểu phải chứa commit
`3be5cb0c82b9305532948a85699be5acdb6d0213`. Nếu dùng revision khác, dừng và đối chiếu với chủ dự án
trước khi materialize; không ghép code bằng tay từ tài liệu.

```powershell
git rev-parse HEAD
git status --short
git branch --show-current
git remote -v
```

### 4.2 Cài dependency và kiểm tra baseline

```powershell
uv sync --frozen
uv run python -m scripts.benchmark.validate_dataset dataset/kira_ltm_v1
uv run pytest -q
```

Điều kiện:

- working tree sạch trước materialization commit và image build;
- Python 3.11/uv đúng lock file;
- Docker Desktop chạy;
- PC truy cập được KiRa Test và OpenAI theo policy công ty;
- đủ disk để giữ 4 hoặc 6 benchmark images + tar;
- không bật Langfuse/Prometheus/Grafana cho benchmark nếu không cần debug.

Node.js 22.12+, Corepack/pnpm và Playwright chỉ là prerequisite nếu nhận thêm product frontend
acceptance ở mục 19; benchmark Phase 4 thuần backend không cần chúng.

Tạo file local từ template, không commit:

```powershell
Copy-Item evaluation/benchmark.pc.env.example .env.benchmark.pc.local
```

Điền tất cả placeholder. Không dùng shared `OPENAI_*` fallback. Bốn provider phải explicit:

- extraction endpoint/model/key;
- rewrite endpoint/model/key;
- embedding endpoint/model/dimension/key;
- semantic judge endpoint/model/deployment/key.

KiRa cần `KIRA_BASE_URL`, username, domain và `KIRA_BASIC_AUTH`. Giá trị
`KIRA_BASIC_AUTH` không chứa literal `Basic ` vì adapter tự thêm prefix.

Kiểm tra không lộ secret:

```powershell
git status --short
git check-ignore .env.benchmark.pc.local
```

### 4.3 Checklist quyết định trước bất kỳ provider call nào

Ghi tên người xác nhận hoặc blocker cho từng mục; thiếu một mục thì chỉ chạy offline validation,
không gọi KiRa/OpenAI.

- KiRa Test endpoint, account/service credential và giới hạn lưu raw response đã được chủ hệ thống phê duyệt.
- Canonical dataset chỉ được gửi OpenAI sau T4.2, human review và `external_provider_allowed=true`.
- Người thực hiện human review và người có quyền chốt candidate đã được xác định.
- Cách chuyển bundle PC → VDI (file share/registry) và nơi lưu checksum đã được xác định.
- Dung lượng Docker đủ exact image set; không dùng shared volume/database làm benchmark target.
- Nếu có làm product UI real-KiRa ngoài benchmark, approved runtime overlay theo mục 19 đã tồn tại.

## 5. T4.1 — materialize canonical dataset bằng KiRa thật

### 5.1 Lập plan, chưa network

```powershell
uv run python -m scripts.benchmark.materialize_dataset plan `
  --root dataset/kira_ltm_v1
```

Xác nhận 80 unique requests, 140 fills, 54 answers. Nếu counts khác, dừng và audit dataset/revision.

### 5.2 Một request preflight vào checkpoint chính thức

```powershell
uv run python -m scripts.benchmark.materialize_dataset preflight `
  --root dataset/kira_ltm_v1 `
  --env-file .env.benchmark.pc.local --env-file-only `
  --checkpoint artifacts/week5/kira-materialization.json
```

Preflight phải kiểm tra auth, SSE parsing, final text, timeout và ghi đúng một completed task vào
checkpoint chính thức. Không tạo checkpoint thử riêng rồi gọi lại cùng query.

### 5.3 Resume đủ 80/80

```powershell
uv run python -m scripts.benchmark.materialize_dataset collect `
  --root dataset/kira_ltm_v1 `
  --env-file .env.benchmark.pc.local --env-file-only `
  --checkpoint artifacts/week5/kira-materialization.json `
  --resume
```

Nếu mất kết nối, chạy lại chính lệnh `--resume`; completed task không được gọi lại. Không sửa tay
checkpoint. Không commit checkpoint vì nó chứa query và KiRa response.

### 5.4 Preview rồi apply

```powershell
uv run python -m scripts.benchmark.materialize_dataset apply `
  --root dataset/kira_ltm_v1 `
  --checkpoint artifacts/week5/kira-materialization.json `
  --dataset-version 1.0.0-materialized.1 `
  --output-root artifacts/week5/kira_ltm_v1_materialized

uv run python -m scripts.benchmark.validate_dataset `
  artifacts/week5/kira_ltm_v1_materialized
```

Review diff preview. Nếu đúng, apply cùng checkpoint vào canonical:

```powershell
uv run python -m scripts.benchmark.materialize_dataset apply `
  --root dataset/kira_ltm_v1 `
  --checkpoint artifacts/week5/kira-materialization.json `
  --dataset-version 1.0.0-materialized.1 `
  --in-place

uv run python -m scripts.benchmark.validate_dataset dataset/kira_ltm_v1
```

Acceptance T4.1:

- checkpoint complete 80/80;
- không còn blank assistant turn;
- không còn `TBD_AFTER_KIRA_FILL`;
- 140 fills và 54 final answers đã materialize;
- validator PASS.

## 6. T4.2 — human review và dataset freeze

Xuất packet/decision template mới, create-only:

```powershell
uv run python -m scripts.benchmark.review_dataset export `
  --root dataset/kira_ltm_v1 `
  --packet artifacts/week5/dataset-review-packet.json `
  --decisions artifacts/week5/dataset-review-decisions.json
```

Human phải review:

- tối đa 80 unique KiRa output hashes;
- toàn bộ 54 final answers;
- intent, formula, threshold;
- temporal/negation;
- action/API fields;
- memory lifecycle add/update/reinforce/do-not-persist;
- safety và cross-user isolation;
- semantic near-duplicates;
- fills có giữ nguyên ý nghĩa Session A không.

Điền mọi placeholder trong decisions bằng cùng reviewer/revision. Freeze preview trước:

```powershell
uv run python -m scripts.benchmark.review_dataset freeze `
  --root dataset/kira_ltm_v1 `
  --packet artifacts/week5/dataset-review-packet.json `
  --decisions artifacts/week5/dataset-review-decisions.json `
  --dataset-version 1.0.0 `
  --allow-pc-openai `
  --output-root artifacts/week5/kira_ltm_v1_reviewed

uv run python -m scripts.benchmark.validate_dataset artifacts/week5/kira_ltm_v1_reviewed
```

Sau khi human chấp thuận preview:

```powershell
uv run python -m scripts.benchmark.review_dataset freeze `
  --root dataset/kira_ltm_v1 `
  --packet artifacts/week5/dataset-review-packet.json `
  --decisions artifacts/week5/dataset-review-decisions.json `
  --dataset-version 1.0.0 `
  --allow-pc-openai `
  --in-place

uv run python -m scripts.benchmark.validate_dataset dataset/kira_ltm_v1
git diff -- dataset/kira_ltm_v1
```

Chỉ commit canonical reviewed dataset, không add `artifacts/` hay `.env`:

```powershell
git add dataset/kira_ltm_v1
git commit -m "data: freeze reviewed KiRa LTM benchmark"
git status --short
```

Nếu sửa gold sau đó: bump dataset version, commit mới, rebuild images và rerun toàn bộ affected
variants. Không đổi dữ liệu dưới cùng version/hash.

## 7. Khai báo control/candidates và exact provenance

Control runtime cố định:

```text
75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00
```

### Quyết định còn cần chủ dự án chốt: candidate

Candidate benchmark **chưa được khai báo bằng SHA trong repository ở thời điểm handoff**. AI trên PC
được phép hoàn tất T4.1/T4.2, nhưng phải dừng trước T4.3 nếu bảng sau chưa được chủ dự án điền và
xác nhận. Không tự dùng `HEAD`, một dirty worktree, hay một commit có vẻ mới nhất làm candidate.

| Variant | Runtime revision | Trạng thái | Change scope và lý do |
| --- | --- | --- | --- |
| `control` | `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00` | fixed | historical control |
| `candidate-a` | `<owner-declared full SHA>` | required before T4.3 | `<prompt/config/runtime/dependency/schema/lifecycle + summary>` |
| `candidate-b` | `<owner-declared full SHA>` | optional | `<scope + summary>` |

Chỉ dùng một hoặc tối đa hai committed candidate revisions. Runtime SHA và harness SHA là hai khái
niệm riêng. Candidate declaration phải ghi scope thay đổi (prompt/config/runtime/dependency/schema/
lifecycle) và summary; nếu không chắc scope, dùng `runtime_code` bảo thủ thay vì giả là prompt-only.

Exact image set:

```text
control-runtime + control-eval
candidate-a-runtime + candidate-a-eval
[candidate-b-runtime + candidate-b-eval]
postgres-dependency
```

Không hard-code “4 images”: số benchmark variant images là `2 + 2 × candidate_count`; cộng một
PostgreSQL dependency image trong archive.

## 8. Native benchmark executor đã có — audit trước khi chạy live

Không dùng lệnh probe một suite: native runner cố ý từ chối partial run. Audit các boundary sau trên
revision thực tế trước khi build image:

Tái sử dụng các module đã có:

- `evaluation/runner.py`: prepare, case ledger, resume;
- `evaluation/formation.py`: native Mem0/persistent formation + receipt reconciliation;
- `evaluation/retrieval.py`: gold and formation-produced fixtures, Recall@3/MRR@10;
- `evaluation/rewrite.py`: native rewriter + deterministic constraints + judge;
- `evaluation/cross_session.py`: paired No-LTM/With-LTM + final QA/task success;
- `evaluation/isolation.py`: run/case-owned resources và cleanup authorization;
- `evaluation/judge.py`: một OpenAI-compatible semantic call, zero automatic retry.

Native executor hiện phải giữ các invariants:

1. validate/compile trước network call;
2. dùng exact `RunProvenance` của image;
3. chạy tuần tự vì Gateway dùng static benchmark user;
4. tạo run-scoped user/session/conversation/event IDs;
5. assert DB/memory state rỗng trước từng case;
6. persistent formation đi qua exact Worker/runtime của variant và chờ durable receipt;
7. retrieval fixture chỉ xóa exact memory IDs do run sở hữu;
8. Session B No-LTM: retrieval off, formation off;
9. Session B With-LTM: retrieval on, formation off;
10. hai arm dùng cùng query/provider/KiRa contract;
11. Session B không schedule memory job mới;
12. cleanup chỉ trên resources đã đăng ký; cleanup fail dừng run;
13. cancellation không ghi PASS giả;
14. dependency/protocol error giữ đúng outcome, không đổi thành FAIL quality;
15. không serialize raw credentials, exception message, prompt/provider response;
16. resume không gọi lại quality-terminal case;
17. profile PC luôn `official=false`, internal mới có thể official.

Không thêm eval-only backdoor vào production `/chat` nếu có thể dùng injected application ports. Nếu
cần control No-LTM/With-LTM, ưu tiên hai isolated app instances/config rõ ràng; không mutate global
flag giữa concurrent requests.

### Test bắt buộc trước live run

- mock full runner đủ 4 suites;
- native factory không fallback provider;
- config/provenance mismatch fail trước provider call;
- resume/cancellation;
- No-LTM/With-LTM isolation;
- Session B không schedule job;
- contamination detection và exact cleanup;
- one provider call/zero retry judge;
- redaction/truncation và secret scan artifact;
- Docker integration với disposable PostgreSQL và deterministic KiRa/provider doubles.

Chạy:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Không chạy full corpus từ source tree dirty. Build eval image từ commit chứa executor rồi chạy
network-disabled mock acceptance trước live provider calls.

## 9. T4.3 — build exact images sau dataset freeze và executor commit

Đảm bảo Docker có base/dependency images cần thiết. Khi có mạng được phép:

```powershell
docker pull pgvector/pgvector:0.8.6-pg16-bookworm
git status --short
./scripts/benchmark/offline_handoff.ps1 -Action Build `
  -CandidateRevision @("<candidate-a-full-sha>") `
  -CandidateChangeScope @("prompt", "config") `
  -CandidateSummary "Mô tả ngắn thay đổi thực sự của candidate."
```

Hai candidates:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Build `
  -CandidateRevision @("<candidate-a-full-sha>", "<candidate-b-full-sha>")
```

`Build` bắt buộc clean checkout, dùng detached worktree, không pull trong build, kiểm OCI labels và
ghi `artifacts/week5/offline-handoff/image-manifest.json`. Eval image lấy harness/dataset đã freeze
từ current clean revision nhưng app/Worker/Mem0/lock từ exact variant runtime context.
Build đồng thời sinh strict provenance files tại
`artifacts/week5/offline-handoff/provenance/<variant>.json`. Khai báo change scope phải phản ánh thay
đổi thật; nếu không chắc, dùng scope bảo thủ `runtime_code`, không giả là prompt-only.

Chạy network-disabled acceptance trên **mọi** eval image:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action MockAcceptance
```

Không coi mock acceptance là quality result.

## 10. Start từng variant, không tăng peak RAM

Kiểm Compose mà không in resolved secret:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Validate `
  -EnvFile .env.benchmark.pc.local
```

Control:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action StartControl `
  -EnvFile .env.benchmark.pc.local
```

Sau khi xong control:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Stop `
  -EnvFile .env.benchmark.pc.local
./scripts/benchmark/offline_handoff.ps1 -Action StartCandidate `
  -VariantId candidate-a `
  -EnvFile .env.benchmark.pc.local
```

Lặp candidate-b nếu declared. `Stop` giữ volumes. Mỗi variant full acceptance cần fresh DB volume hoặc
database riêng. Không chạy broad `docker volume prune`; chỉ xóa đúng benchmark volume sau khi đã copy
evidence và được xác nhận.

## 11. T4.4 — OpenAI/KiRa/DB/Gateway/Worker preflight

Tạo provenance JSON từ exact image metadata; không tự gõ SHA/package versions bằng trí nhớ. Chọn đúng
4 target selectors trong `.env.benchmark.pc.local` cho stack đang chạy.

Control selectors:

```ini
WEEK5_BENCHMARK_GATEWAY_HOST=control-gateway
WEEK5_BENCHMARK_WORKER_HOST=control-worker
WEEK5_BENCHMARK_POSTGRES_HOST=control-postgres
WEEK5_BENCHMARK_DATABASE=kira_control
```

Candidate selectors dùng `candidate-*`/`kira_candidate`.

Preflight từng exact variant:

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile tools run --rm eval-controller preflight `
  --profile pc_openai_acceptance `
  --suite formation --suite retrieval --suite rewrite --suite cross_session `
  --formation-mode persistent `
  --provenance-file /materialization/offline-handoff/provenance/<variant>.json `
  --output /artifacts/pc-preflight/<variant>-provider-preflight.json
```

Required probes: extraction JSON, embedding dimension, rewrite, judge schema, pgvector/memory schema,
conversation DB, Gateway, Worker. KiRa real evidence lấy từ complete materialization checkpoint, không
dùng mock `/_test/requests`.

Sau khi preflight của variant đó PASS, **dừng đúng Worker của variant trước native benchmark**. Runner
tự claim/process job cross-session trong eval-controller để dùng đúng run-owned Mem0 schema; để
Worker thường chạy song song sẽ tạo race và làm artifact vô hiệu.

Control:

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile control stop control-worker
```

Candidate:

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile candidate stop candidate-worker
```

Xác minh container Worker đã stopped. Không dừng PostgreSQL. Gateway có thể giữ chạy nhưng native
runner không phụ thuộc Gateway cho case execution.

Freeze preflight dùng một run-set provider artifact đã xác minh đồng nhất (hoặc mở rộng freeze để
hash tất cả variant preflights nếu native executor implementation yêu cầu):

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.benchmark.freeze_pc_preflight `
  --provider-preflight /artifacts/pc-preflight/candidate-a-provider-preflight.json `
  --materialization-checkpoint /materialization/kira-materialization.json `
  --dataset-root /app/dataset/kira_ltm_v1 `
  --output /artifacts/pc-preflight/freeze.json
```

Không rerun paid preflight vào cùng output; output create-only. Freeze chỉ chứa hashes/identity đã
sanitize.

## 12. T4.5 — full PC OpenAI acceptance

Chạy control rồi candidates tuần tự, cùng dataset/config/seed. Chạy **bên trong exact eval image** để
hostname PostgreSQL nội bộ và harness provenance đều đúng. Không truyền `--env-file-only`: Compose đã
nạp file local và inject `WEEK5_DATABASE_URL`/`WEEK5_MEMORY_DATABASE_URL` đúng variant.

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile tools run --rm eval-controller run `
  --profile pc_openai_acceptance `
  --suite formation --suite retrieval --suite rewrite --suite cross_session `
  --formation-mode persistent `
  --provenance-file /materialization/offline-handoff/provenance/<variant>.json `
  --dataset-root /app/dataset/kira_ltm_v1 `
  --artifact-root /artifacts/pc-openai/<run-set-id>/<variant> `
  --seed 742
```

`<variant>` là `control`, `candidate-a` hoặc `candidate-b` đúng image đang chạy. Resume dùng nguyên
command/artifact root; manifest + isolation plan giữ run/owner ID, attempt dang dở được đóng bằng
`benchmark_attempt_interrupted`, rồi attempt mới dùng state riêng. Không overwrite run khác.

Metrics cần report nhưng chỉ diagnostic:

- Formation Precision/Recall/F1;
- Retrieval Recall@3/MRR@10;
- rewrite constraints + semantic verdict;
- final QA semantic verdict + task success;
- No-LTM/With-LTM;
- timing/retry và control/candidate deltas.

Hard gates duy nhất:

- full eligible corpus có quality-terminal artifact;
- không unresolved dependency/protocol/missing case;
- zero safety hard fail;
- không blocking dataset/harness/provenance error.

Kiểm gate:

```powershell
uv run python -m scripts.benchmark.check_pc_acceptance `
  --run control=artifacts/week5/pc-openai/<run-set-id>/control `
  --run candidate-a=artifacts/week5/pc-openai/<run-set-id>/candidate-a `
  --dataset-root dataset/kira_ltm_v1 `
  --image-manifest artifacts/week5/offline-handoff/image-manifest.json `
  --pc-preflight artifacts/week5/pc-preflight/freeze.json `
  --output artifacts/week5/pc-openai/<run-set-id>/pc-acceptance.json
```

Thêm candidate-b nếu declared. Sau đó copy acceptance artifact về path export mặc định hoặc truyền
path rõ ràng cho `-PcAcceptancePath`.

Không viết “candidate promoted/rejected” từ PC metrics. `UNCERTAIN`/quality thấp không tự chặn nếu
không phải safety/blocking error.

## 13. Failure routing trên PC

- KiRa auth/SSE/timeout: sửa local env/network, resume checkpoint; không sửa gold để né lỗi.
- OpenAI auth/rate/JSON schema: dừng run, giữ dependency/protocol outcome, sửa config/runtime rồi rerun.
- Gold/dataset error: quay lại T4.2, bump version, commit, rebuild images, T4.4 và T4.5 lại.
- Harness/provenance mismatch: không sửa manifest tay; rebuild từ clean exact revisions.
- Safety hard fail: sửa trước handoff.
- Quality yếu nhưng technical sạch: report diagnostic; có thể tạo candidate revision mới, không
  promotion trên PC.
- Cleanup/contamination fail: dừng run, không chạy case tiếp theo.

## 14. T4.6 — đóng gói handoff sau technical PASS

Export phải trỏ đúng final acceptance artifact:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Export `
  -PcPreflightPath artifacts/week5/pc-preflight/freeze.json `
  -PcAcceptancePath artifacts/week5/pc-openai/<run-set-id>/pc-acceptance.json
```

Export chạy `freeze_handoff` trong control eval image với `--network none`, kiểm:

- reviewed dataset/hash;
- image manifest không đổi sau PC acceptance;
- preflight không đổi;
- exact variants và dynamic image count;
- current Docker image IDs còn khớp manifest;
- PC technical acceptance PASS.

Bundle gồm image tar, SHA-256, image manifest, dataset/review hash, preflight/acceptance hash,
secret-free env/Compose templates và runbooks. Không bundle:

- `.env`;
- API key/KiRa credential/auth header;
- materialization checkpoint/raw response;
- embedding vector/database dump;
- Langfuse export.

Chỉ chuyển các file được liệt kê trong `bundle-manifest.json`; không cần chuyển thư mục build
worktrees. Lưu thêm SHA-256 của toàn bundle theo cơ chế chuyển file nội bộ nếu có.

## 15. VDI và Kubernetes sau Phase 4

### VDI

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Import `
  -BundleDirectory <path-to-transferred-bundle>
```

Import kiểm checksum từng file, tar, load images và so image ID với manifest. Nếu dùng internal
registry, chạy `-Action Publish`; script tạo create-only `registry-manifest.json` có
`repository@sha256:...` cho từng runtime/eval image. K8s chỉ dùng immutable reference, không dùng
tag hoặc `latest` làm evidence. Quy trình Phase 5 đầy đủ nằm trong
`K8S-RUNBOOK.md` của bundle và `docs/benchmark-k8s-acceptance.md` trong repo.

### K8s preflight

- deploy exact runtime/eval digests;
- database riêng cho mỗi variant/repetition trên managed PostgreSQL;
- internal extraction/embedding/rewrite/judge explicit, không fallback external;
- KiRa thật;
- OTel/Langfuse outage không ảnh hưởng business/readiness;
- preflight embedding dimension/schema/Gateway/Worker/KiRa trước full corpus.

### T5.4 discovery/tuning loop

T5.4 là discovery, được phép nhìn failure và cải tiến candidate. Nếu sửa candidate:

```text
quay lại PC -> commit SHA mới -> build digest mới -> PC technical acceptance
-> handoff VDI -> K8s preflight/discovery lại
```

Tối đa hai declared candidate revisions. Chỉ freeze best candidate trước T5.5.

### T5.5 confirmation

- 3 paired repetitions control/candidate;
- cùng exact dataset/config/order/seed policy;
- không tune sau khi bắt đầu confirmation;
- audit toàn bộ semantic `UNCERTAIN`/bất đồng;
- audit một global budget `ceil(10% × tổng semantic PASS/FAIL)`, stratified theo
  variant/suite/verdict/bundle;
- deterministic scorer không cần random manual audit;
- safety là hard fail;
- không gộp mọi metric thành một score tổng.

Khi điền audit decisions, copy nguyên `case_id`, `subject` và `output_sha256` từ batch. Không bỏ
`subject`: một cross-session case có thể có semantic output riêng cho No-LTM/With-LTM và
rewrite/final answer.

Sau confirmation mới chạy late performance guardrail và promotion/keep-control decision.

Các command `release scorecard|confirm|performance|promote` đã được triển khai và unit-test trên
laptop. Chúng chỉ tổng hợp/validate artifact, không gọi provider. `release scorecard` cố ý chỉ nhận
`internal_test`, nên PC/OpenAI không được dùng command này để tạo official evidence. PC phải hoàn
thành materialization, review/freeze, technical acceptance và exact-image handoff; K8s mới chạy
scorecard/confirmation chính thức theo `docs/benchmark-k8s-acceptance.md`.

Nếu reviewer đổi verdict của một formation match, không sửa tay scorecard: phải re-score/rerun để
TP/FP/FN và audit artifact cùng nhất quán. Tool sẽ fail-closed với
`formation_audit_override_requires_rescore`.

## 16. Security và các điều tuyệt đối không làm

- Không commit `.env*`, API key, KiRa credentials, DB password.
- Không in `docker compose config` đã resolve secret; chỉ dùng `config --quiet`.
- Không ghi raw provider prompt/response hoặc sensitive exception message vào log/artifact.
- Masking fail thì bỏ content field, không fallback raw.
- Không gửi canonical dataset cho external provider trước human approval +
  `external_provider_allowed=true`.
- Không chạy external provider trong official K8s benchmark.
- Không dùng trace availability để đổi quality outcome.
- Không xóa/rebuild historical control.
- Không broad-delete shared database/volume.
- Không gọi lại 80 KiRa tasks nếu checkpoint đã complete.
- Không claim official từ `pc_openai_acceptance`.

## 17. Artifact checklist cuối Phase 4

```text
artifacts/week5/
  kira-materialization.json                 # local sensitive, không handoff/commit
  dataset-review-packet.json                # local review evidence
  dataset-review-decisions.json             # local review evidence
  pc-preflight/
    <variant>-provider-preflight.json
    freeze.json
  pc-openai/<run-set-id>/
    control/
    candidate-a/
    [candidate-b/]
    pc-acceptance.json
  offline-handoff/
    image-manifest.json
    mock-acceptance/<variant>/...
    handoff-evidence.json
    pc-preflight.json
    pc-acceptance.json
    dataset-manifest.json
    kira-benchmark-images.tar
    kira-benchmark-images.tar.sha256
    compose.benchmark.yaml
    benchmark.internal.env.example
    RUNBOOK.md                  # tài liệu này
    K8S-RUNBOOK.md              # benchmark-k8s-acceptance.md
    bundle-manifest.json
```

## 18. Definition of done cho AI trên PC

Chỉ báo Phase 4 complete khi có bằng chứng thật:

- materialization 80/80 và canonical validator PASS;
- named human review, version/hash frozen, external approval explicit;
- native full runner (không mock) chạy đủ bốn suites;
- exact image pair cho control và mọi candidate;
- network-disabled mock acceptance trên mọi eval image;
- live OpenAI/KiRa/DB/Gateway/Worker preflight PASS;
- full control/candidate corpora complete;
- technical PC acceptance PASS, `official=false`, không promotion decision;
- handoff evidence và bundle manifest/hash PASS;
- working tree sạch, mỗi task/fix có commit riêng;
- không có secret/sensitive checkpoint trong Git hoặc bundle.

Nếu một mục chưa có artifact, ghi `NOT_RUN` hoặc blocker cụ thể. Không hạ acceptance criteria để kết
thúc nhanh.

## 19. Product chat acceptance với KiRa thật

Product MVP đã được implement và kiểm thử trên laptop bằng KiRa mock + PostgreSQL thật. Đây là kiểm
tra product API/UI, không thay thế benchmark Phase 4/5.

### Trạng thái launcher hiện tại — không được suy diễn là real-KiRa E2E

`compose.product.yaml` và `compose.openai.yaml` đều cố ý chạy `mock-kira`; chúng hữu ích cho local
product acceptance/OpenAI runtime smoke nhưng **không có Compose overlay đã được phê duyệt để chạy
frontend + Gateway + Worker với KiRa thật trên PC**. Vì vậy không được chỉ thay một biến môi trường
rồi tuyên bố real-KiRa product acceptance PASS.

Real-KiRa product acceptance là việc riêng sau T4 benchmark hoặc trong Phase 11 pilot. Trước khi chạy,
AI trên PC phải có một cấu hình do chủ dự án/platform phê duyệt, nêu rõ:

1. image digest Gateway/Worker/frontend và database disposable hoặc database pilot;
2. secret injection cho KiRa, extraction/rewrite/embedding mà không viết secret vào file tracked;
3. `AUTH_ENABLED=true`, origin frontend thật, HTTPS/cookie contract phù hợp môi trường;
4. endpoint frontend/Gateway, cách apply migration và khởi tạo memory schema;
5. cách dừng/cleanup chính xác resources của run, không đụng DB/volume dùng chung.

Nếu chưa có cấu hình này, ghi product real-KiRa acceptance là `NOT_RUN — approved company runtime
overlay missing`; vẫn có thể hoàn thành T4 benchmark độc lập.

Khi launcher đã tồn tại và được phê duyệt, chạy acceptance dưới đây:

1. Chạy `alembic upgrade head`, xác nhận schema có `chat_requests` và revision đúng runtime.
2. Cấu hình `AUTH_*`, `DATABASE_URL`, KiRa thật và provider retrieval/rewrite cần thiết; không ghi
   credential vào artifact hay commit.
3. Tạo user thử nghiệm bằng `uv run kira-auth-admin create ...`, đăng nhập qua
   `/api/v1/auth/login`, giữ cookie + CSRF token.
4. Tạo conversation bằng `POST /api/v1/conversations`, rồi gửi message qua
   `POST /api/v1/conversations/{session_id}/messages` với một UUID `client_message_id` ổn định.
5. Xác nhận SSE lần lượt có `message.started`, các `message.delta`, rồi `message.completed`; chỉ sau
   completed mới được thấy đủ user/assistant pair, request `completed` và optional memory job trong DB.
6. Gửi lại đúng client ID + content: phải replay từ DB và không gọi KiRa lần hai. Gửi cùng client ID
   nhưng content khác: phải nhận `409 IDEMPOTENCY_CONFLICT`.
7. Ngắt stream giữa chừng và thử KiRa/DB outage: không có partial turn; response chỉ dùng sanitized
   error code/correlation ID, không chứa message, credential hay raw exception.
8. Kiểm tra trace thật có `correlation_id`, `turn_id`, optional `event_id`, `origin_trace_id`; tắt
   Collector/Langfuse rồi lặp lại để xác nhận chat và Worker vẫn chạy.

Giới hạn MVP khi acceptance: message 8.000 ký tự, body 131.072 bytes, rate 30 request/phút và tối đa 2
stream đồng thời mỗi user trên **mỗi Gateway process**. Chỉ thêm shared coordinator khi pilot chạy
nhiều replicas và thực sự cần global quota chính xác.

## 20. Phase 8 memory schema 3 acceptance trên PC

T8.1 nâng package candidate lên `viettel-mem0==2.0.20+viettel.5` và memory schema lên version 3.
Historical control vẫn giữ `.3`; không sửa hoặc migrate database của control bằng runtime candidate.

Trước khi chạy candidate:

1. Backup database/schema candidate. Không reset hoặc xóa memory để né migration.
2. Chạy memory admin initializer bằng đúng image candidate. Receipt v2 phải được backfill
   `conversation_id` từ vector payload hoặc durable `memory_jobs`.
3. Nếu initializer báo `cannot backfill ... events [...]`, dừng lại và review đúng các event ID đó;
   không tự gán owner, không xóa receipt im lặng.
4. Xác nhận `kira_memory_schema.schema_version = 3`, `mem0_version = 2.0.20+viettel.5`, receipt có
   `conversation_id NOT NULL` và owner index.
5. Chạy formation, retrieval và cross-session suite của candidate với KiRa/OpenAI theo PC acceptance.
   So sánh prompt/quality vì product runtime đã tắt auxiliary SQLite history; message window từ
   PostgreSQL là nguồn transcript duy nhất.
6. Xác nhận empty/deduplicated formation cũng tạo receipt có đúng `conversation_id`, replay không gọi
   provider lần hai, và package/runtime provenance trong artifact ghi `.5`.

Đây là technical acceptance cho thay đổi runtime/persisted contract, chưa phải official benchmark và
không quyết định promotion.

## 21. Phase 8 active-owner fencing acceptance trên PC

T8.2 nâng package candidate lên `viettel-mem0==2.0.20+viettel.6`. Memory schema vẫn là version 3;
thay đổi `.5 -> .6` chỉ cập nhật package contract marker, không đổi bảng, vector, embedding hay dữ
liệu memory. Historical control vẫn giữ nguyên package/database của nó.

Trước khi chạy candidate:

1. Chạy memory initializer bằng đúng image candidate và xác nhận metadata là
   `schema_version = 3`, `mem0_version = 2.0.20+viettel.6`. Việc nâng marker `.5 -> .6` phải hoàn
   tất tại chỗ, không reset schema và không chạy lại embedding.
2. Tạo ba conversation cùng database: một `active`, một `deletion_pending`, một thuộc user khác.
   Search của product runtime chỉ được trả memory có owner active và đúng user; owner thiếu, pending
   hoặc cross-user đều phải bị loại.
3. Chặn provider formation giữa chừng, chuyển conversation sang `deletion_pending`, rồi cho provider
   trả kết quả. Job phải kết thúc `skipped` (không retry/dead-letter), không có vector, receipt hay
   entity link mới.
4. Giữ row lock xóa trong lúc thread persist đang chờ và mô phỏng timeout phía caller. Sau khi commit
   pending/delete, thread chạy muộn phải thất bại bằng source-unavailable; kiểm tra lại trực tiếp DB
   để xác nhận vector và receipt vẫn bằng 0.
5. Xác nhận job còn `pending` của conversation đã pending không được Worker claim; job đã chạy mà
   đọc boundary sau thời điểm pending cũng phải được acknowledge dạng `skipped`.
6. Chạy lại formation/retrieval/cross-session acceptance và kiểm tra trace/log chỉ chứa outcome cùng
   identifier đã quy định, không chứa raw prompt, response hoặc credential.

Đây là technical/concurrency acceptance cho candidate. Không dùng kết quả này thay official
benchmark trên K8s.

## 22. Phase 8 idempotent conversation erasure acceptance trên PC

T8.3 đổi `DELETE /api/v1/conversations/{session_id}` thành flow đồng bộ, idempotent và trả `204`.
`DATABASE_URL` và `MEMORY_DATABASE_URL` phải là cùng PostgreSQL database; hai URL được phép dùng role
khác nhau. Không chạy acceptance nếu hai cấu hình trỏ sang database khác.

1. Tạo một conversation có nhiều hơn 10 memory, ít nhất một empty/deduplicated receipt, memory job và
   entity link; tạo thêm conversation của cùng user và user khác làm control.
2. Gọi DELETE với cookie + CSRF hợp lệ. Xác nhận response `204`, conversation/messages/jobs/chat
   requests biến mất; toàn bộ vector và receipt theo `user_id + conversation_id` bằng 0; entity link
   tương ứng bị bỏ và entity orphan bị xóa. Control rows phải còn nguyên.
3. Gọi lại cùng DELETE và gọi bằng user khác. Cả hai đều trả `204`, không tiết lộ conversation từng
   tồn tại hay thuộc ai.
4. Giữ lock hoặc gây DB failure sau lúc mark pending nhưng trước commit purge. Endpoint phải trả
   `503 DELETION_RETRY_REQUIRED`, history/chat mới bị chặn và row vẫn `deletion_pending`. Bỏ lỗi rồi
   retry cùng DELETE; lần sau phải hoàn tất.
5. Trong lúc DELETE, cho một formation/provider cũ trả về muộn. Sau khi purge hoàn tất không được có
   vector, receipt hay entity link tái xuất hiện; Worker ghi outcome `skipped`, không retry.
6. Tạo một pending row thử nghiệm rồi chạy
   `uv run kira-conversations purge-pending --limit 100`. Xác nhận JSON chỉ có `limit`/`purged`, batch
   xóa đúng pending rows và chạy lại cho `purged=0`.
7. Kiểm tra log/error không chứa message, memory content, raw exception hoặc credential. Đây là
   technical acceptance; không thay official benchmark.

## 23. Phase 11 production deployment handoff

Sau Phase 4/5 và product acceptance, đọc toàn bộ
[`production-deployment.md`](production-deployment.md). Laptop đã chuẩn bị phần không
phụ thuộc platform; không được diễn giải thành Phase 11 PASS.

AI trên PC phải:

1. Thu thập tám quyết định platform ở đầu runbook trước khi tạo manifest. Không tự chọn Helm/Kustomize,
   Ingress, secret provider hoặc PostgreSQL topology khi công ty đã có contract khác.
2. Dùng exact runtime/frontend digests đã qua acceptance. Gateway, Worker, migration và memory-init
   phải cùng backend digest.
3. Render/validate manifest, chạy install mới và upgrade trong namespace pilot; lưu sanitized output.
4. Cấu hình managed PostgreSQL TLS với application/memory/admin roles cùng một database; chạy
   migration lock, memory init rồi `python -m worker.memory_admin validate`.
5. Backup database đã quiesce, restore sang database khác và đối chiếu revision, memory metadata cùng
   aggregate counts theo runbook. Không commit dump hoặc DSN.
6. Nối Gateway/Worker tới company Collector, tìm real trace bằng `correlation_id`, `turn_id`,
   `event_id`, `origin_trace_id`, rồi chứng minh Collector/backend outage vẫn fail-open.
7. Chạy toàn bộ pilot checklist bằng frontend, KiRa và internal providers thật, gồm deletion race,
   restart, restore và image rollback.

Mọi bước chưa có artifact thật phải ghi `NOT_RUN` kèm blocker. Manifest chỉ được commit sau khi format
platform, registry, namespace, Ingress/TLS, secrets, database và Collector contracts đã được xác nhận.
