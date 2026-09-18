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
<commit chứa tài liệu này> T4.6 handoff evidence/bundle
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
- mock runner đủ bốn suites và không claim quality.

Docker engine trên laptop đã tắt lúc hoàn tất nên chưa build image thật. Không có live KiRa/OpenAI PC
artifact nào được tạo trên laptop.

### Khoảng trống phải nhìn thấy ngay

Lệnh `scripts.run_week5_benchmark run` hiện chỉ chạy profile `mock`. Với
`pc_openai_acceptance`/`internal_test`, nó cố ý trả:

```json
{"outcome":"NOT_RUN","reason":"native_benchmark_executor_not_configured"}
```

Đây là fail-closed, không phải PASS. Trước full PC run, phải hoàn thiện native executor wiring theo
mục 8. Không được xóa guard rồi sinh artifact giả. Các preflight, materialization, review, image,
acceptance và handoff checker đã sẵn; live full-corpus executor là phần implementation còn thiếu.

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

## 4. Chuẩn bị PC từ clean checkout

```powershell
git checkout main
git pull --ff-only origin main
git status --short
uv sync --frozen
uv run python -m scripts.validate_dataset dataset/kira_ltm_v1
uv run pytest -q
```

Điều kiện:

- working tree sạch trước materialization commit và image build;
- Python/uv đúng lock file;
- Docker Desktop chạy;
- PC truy cập được KiRa Test và OpenAI theo policy công ty;
- đủ disk để giữ 4 hoặc 6 benchmark images + tar;
- không bật Langfuse/Prometheus/Grafana cho benchmark nếu không cần debug.

Tạo file local từ template, không commit:

```powershell
Copy-Item evaluation/week5.pc.env.example .env.week5.pc.local
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
git check-ignore .env.week5.pc.local
```

## 5. T4.1 — materialize canonical dataset bằng KiRa thật

### 5.1 Lập plan, chưa network

```powershell
uv run python -m scripts.materialize_dataset plan `
  --root dataset/kira_ltm_v1
```

Xác nhận 80 unique requests, 140 fills, 54 answers. Nếu counts khác, dừng và audit dataset/revision.

### 5.2 Một request preflight vào checkpoint chính thức

```powershell
uv run python -m scripts.materialize_dataset preflight `
  --root dataset/kira_ltm_v1 `
  --env-file .env.week5.pc.local --env-file-only `
  --checkpoint artifacts/week5/kira-materialization.json
```

Preflight phải kiểm tra auth, SSE parsing, final text, timeout và ghi đúng một completed task vào
checkpoint chính thức. Không tạo checkpoint thử riêng rồi gọi lại cùng query.

### 5.3 Resume đủ 80/80

```powershell
uv run python -m scripts.materialize_dataset collect `
  --root dataset/kira_ltm_v1 `
  --env-file .env.week5.pc.local --env-file-only `
  --checkpoint artifacts/week5/kira-materialization.json `
  --resume
```

Nếu mất kết nối, chạy lại chính lệnh `--resume`; completed task không được gọi lại. Không sửa tay
checkpoint. Không commit checkpoint vì nó chứa query và KiRa response.

### 5.4 Preview rồi apply

```powershell
uv run python -m scripts.materialize_dataset apply `
  --root dataset/kira_ltm_v1 `
  --checkpoint artifacts/week5/kira-materialization.json `
  --dataset-version 1.0.0-materialized.1 `
  --output-root artifacts/week5/kira_ltm_v1_materialized

uv run python -m scripts.validate_dataset `
  artifacts/week5/kira_ltm_v1_materialized
```

Review diff preview. Nếu đúng, apply cùng checkpoint vào canonical:

```powershell
uv run python -m scripts.materialize_dataset apply `
  --root dataset/kira_ltm_v1 `
  --checkpoint artifacts/week5/kira-materialization.json `
  --dataset-version 1.0.0-materialized.1 `
  --in-place

uv run python -m scripts.validate_dataset dataset/kira_ltm_v1
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
uv run python -m scripts.review_dataset export `
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
uv run python -m scripts.review_dataset freeze `
  --root dataset/kira_ltm_v1 `
  --packet artifacts/week5/dataset-review-packet.json `
  --decisions artifacts/week5/dataset-review-decisions.json `
  --dataset-version 1.0.0 `
  --allow-pc-openai `
  --output-root artifacts/week5/kira_ltm_v1_reviewed

uv run python -m scripts.validate_dataset artifacts/week5/kira_ltm_v1_reviewed
```

Sau khi human chấp thuận preview:

```powershell
uv run python -m scripts.review_dataset freeze `
  --root dataset/kira_ltm_v1 `
  --packet artifacts/week5/dataset-review-packet.json `
  --decisions artifacts/week5/dataset-review-decisions.json `
  --dataset-version 1.0.0 `
  --allow-pc-openai `
  --in-place

uv run python -m scripts.validate_dataset dataset/kira_ltm_v1
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

Chọn một hoặc tối đa hai committed candidate revisions. Không dùng dirty tree. Candidate declaration
phải ghi scope thay đổi (prompt/config/runtime/dependency/schema/lifecycle) và summary. Runtime SHA và
harness SHA là hai khái niệm riêng.

Exact image set:

```text
control-runtime + control-eval
candidate-a-runtime + candidate-a-eval
[candidate-b-runtime + candidate-b-eval]
postgres-dependency
```

Không hard-code “4 images”: số benchmark variant images là `2 + 2 × candidate_count`; cộng một
PostgreSQL dependency image trong archive.

## 8. Việc code còn thiếu trước full PC run — native benchmark executor

Đầu tiên xác nhận khoảng trống vẫn tồn tại:

```powershell
uv run python -m scripts.run_week5_benchmark run `
  --profile pc_openai_acceptance `
  --suite rewrite `
  --artifact-root artifacts/week5/probe-do-not-use
```

Nếu trả `native_benchmark_executor_not_configured`, phải implement. Nếu revision mới đã có executor,
audit test và semantics trước khi dùng.

### Yêu cầu implementation, không được giảm chuẩn

Tái sử dụng các module đã có:

- `evaluation/runner.py`: prepare, case ledger, resume;
- `evaluation/formation.py`: native Mem0/persistent formation + receipt reconciliation;
- `evaluation/retrieval.py`: gold and formation-produced fixtures, Recall@3/MRR@10;
- `evaluation/rewrite.py`: native rewriter + deterministic constraints + judge;
- `evaluation/cross_session.py`: paired No-LTM/With-LTM + final QA/task success;
- `evaluation/isolation.py`: run/case-owned resources và cleanup authorization;
- `evaluation/judge.py`: một OpenAI-compatible semantic call, zero automatic retry.

Native executor phải:

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

Commit executor riêng trước build image. Không chạy full corpus nếu chỉ có mock executor.

## 9. T4.3 — build exact images sau dataset freeze và executor commit

Đảm bảo Docker có base/dependency images cần thiết. Khi có mạng được phép:

```powershell
docker pull pgvector/pgvector:0.8.6-pg16-bookworm
git status --short
./scripts/week5_offline_handoff.ps1 -Action Build `
  -CandidateRevision @("<candidate-a-full-sha>") `
  -CandidateChangeScope @("prompt", "config") `
  -CandidateSummary "Mô tả ngắn thay đổi thực sự của candidate."
```

Hai candidates:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Build `
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
./scripts/week5_offline_handoff.ps1 -Action MockAcceptance
```

Không coi mock acceptance là quality result.

## 10. Start từng variant, không tăng peak RAM

Kiểm Compose mà không in resolved secret:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Validate `
  -EnvFile .env.week5.pc.local
```

Control:

```powershell
./scripts/week5_offline_handoff.ps1 -Action StartControl `
  -EnvFile .env.week5.pc.local
```

Sau khi xong control:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Stop `
  -EnvFile .env.week5.pc.local
./scripts/week5_offline_handoff.ps1 -Action StartCandidate `
  -VariantId candidate-a `
  -EnvFile .env.week5.pc.local
```

Lặp candidate-b nếu declared. `Stop` giữ volumes. Mỗi variant full acceptance cần fresh DB volume hoặc
database riêng. Không chạy broad `docker volume prune`; chỉ xóa đúng benchmark volume sau khi đã copy
evidence và được xác nhận.

## 11. T4.4 — OpenAI/KiRa/DB/Gateway/Worker preflight

Tạo provenance JSON từ exact image metadata; không tự gõ SHA/package versions bằng trí nhớ. Chọn đúng
4 target selectors trong `.env.week5.pc.local` cho stack đang chạy.

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
docker compose --env-file .env.week5.pc.local -f compose.week5.benchmark.yaml `
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

Freeze preflight dùng một run-set provider artifact đã xác minh đồng nhất (hoặc mở rộng freeze để
hash tất cả variant preflights nếu native executor implementation yêu cầu):

```powershell
docker compose --env-file .env.week5.pc.local -f compose.week5.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.freeze_pc_preflight `
  --provider-preflight /artifacts/pc-preflight/candidate-a-provider-preflight.json `
  --materialization-checkpoint /materialization/kira-materialization.json `
  --dataset-root /app/dataset/kira_ltm_v1 `
  --output /artifacts/pc-preflight/freeze.json
```

Không rerun paid preflight vào cùng output; output create-only. Freeze chỉ chứa hashes/identity đã
sanitize.

## 12. T4.5 — full PC OpenAI acceptance

Sau khi native executor hoàn chỉnh, chạy control rồi candidates tuần tự, cùng dataset/config/seed:

```powershell
python -m scripts.run_week5_benchmark run `
  --profile pc_openai_acceptance `
  --suite formation --suite retrieval --suite rewrite --suite cross_session `
  --formation-mode persistent `
  --env-file .env.week5.pc.local --env-file-only `
  --provenance-file artifacts/week5/offline-handoff/provenance/<variant>.json `
  --dataset-root dataset/kira_ltm_v1 `
  --artifact-root artifacts/week5/pc-openai/<run-set-id>/<variant> `
  --seed 742
```

Resume dùng đúng command/artifact root; manifest giữ run ID và case ledger. Không overwrite run khác.

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
uv run python -m scripts.check_pc_acceptance `
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
./scripts/week5_offline_handoff.ps1 -Action Export `
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
./scripts/week5_offline_handoff.ps1 -Action Import `
  -BundleDirectory <path-to-transferred-bundle>
```

Import kiểm checksum từng file, tar, load images và so image ID với manifest. Nếu dùng internal
registry, publish immutable digest; không retag `latest` làm evidence.

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
- audit khoảng 10% semantic PASS/FAIL, stratified theo verdict;
- deterministic scorer không cần random manual audit;
- safety là hard fail;
- không gộp mọi metric thành một score tổng.

Sau confirmation mới chạy late performance guardrail và promotion/keep-control decision.

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
    kira-week5-images.tar
    kira-week5-images.tar.sha256
    compose.week5.benchmark.yaml
    week5.internal.env.example
    RUNBOOK.md
    COMPANY-PC-AI-HANDOFF.md
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
