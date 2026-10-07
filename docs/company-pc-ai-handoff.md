# Bàn giao benchmark KiRa trên PC

## 1. Phạm vi hiện tại

Đánh giá **bản code hiện tại**, một runtime committed và clean. Hai nhánh No-LTM/With-LTM chạy trên
cùng runtime. Không tự build/chạy historical control hoặc thêm candidate để nhân đôi workload.
PC dùng OpenAI + KiRa thật; kết quả là technical acceptance/diagnostic (`official=false`).
Qwen3-14B base chỉ reachable từ K8s; không đặt Qwen thành gate trên PC. Xem
[benchmark-k8s-acceptance.md](benchmark-k8s-acceptance.md) khi chuyển môi trường.

Benchmark QA mặc định chọn `--suite cross_session`: 253 source events trên bốn users, sau đó
209 QA với No-LTM/With-LTM. Component formation (62 cases) và rewrite (54 cases) chạy riêng để
kiểm tra một cấu hình, rồi chạy lại khi thành phần liên quan thay đổi; không replay chúng trong
mỗi lượt QA nếu không đổi. Full bốn suites là diagnostic tùy chọn.

Contract harness là `kira-week5-benchmark-v5`. Build mặc định khai báo variant `current` với provenance
`current_runtime`; exact runtime/harness SHAs, prompt hashes, package versions và image IDs được ghi
trong manifest. Artifacts v4 là evidence lịch sử, không dùng làm readiness cho lượt v5.

Dataset payload/gold/tiêu chí chấm giữ frozen. Reuse policy OpenAI, proxy/CA/env và checks đã PASS.
Không collect KiRa lại, đổi gold/scorer hay tune prompt trong lượt đo.
Nếu dataset thực sự còn pending hoặc thay policy, dùng `scripts.benchmark.review_dataset` và
`scripts.benchmark.materialize_dataset`; đây là tooling riêng, không tự chạy trong benchmark frozen.

## 2. Replay nguồn và QA

- `conv01`…`conv04` là **bốn conversations nguồn, bốn users**. Sessions bên trong mỗi conv chỉ là
  các đoạn làm việc theo thời gian. Replay nguyên chronology, không tạo 86 source conversations.
- Bốn users dùng chung run-owned DB/Mem0 collection; giữ owner và source metadata riêng. Memory
  user này không được lọt sang user khác.
- Replay source từng conv một lần, chờ formation hoàn tất, chạy QA của conv đó rồi đi conv kế.
  Hiện dataset có **253 source pairs duy nhất** cho cross-session; không replay 13.301 pairs mỗi QA.
  Đây là số source deliveries, không cam kết số LLM calls: eligibility, retries và native dedup
  quyết định calls thực tế. Formation tests độc lập chỉ dựng state riêng khi chọn suite đó.
- Mỗi QA mở conversation mới cùng user. Flow thật: query → scoped Mem0 search → context/rewrite →
  KiRa → chấm. GLOBAL được xét; CONVERSATION nguồn vẫn isolate. Gold chỉ dùng chấm sau retrieval.
- QA/arm/attempt dùng conversation/recent context và KiRa username riêng. Tắt formation cho QA:
  bộ câu hỏi kiểm thử không thay đổi kho memory nguồn. Đổi token/client không reset history KiRa.
- No-LTM chuyển nguyên query sang KiRa, chỉ judge câu trả lời cuối. Rewrite judge chỉ chạy cho
  With-LTM; rewrite constraints của No-LTM giữ làm diagnostic, không quyết định outcome.
- Resume reuse source corpus đã được chứng minh hoàn tất; source replay dang dở phải được reconcile
  qua receipts/job state, không coi cache trong RAM là evidence. Cleanup chỉ xóa run-owned state.

## 3. Chuẩn bị tối thiểu

Kiểm tra `git status --short`, revision committed và Docker. Chỉ chạy lại checks bị thay đổi hoặc lỗi;
không mặc định full pytest/audit dự án. Env local git-ignored có provider models/endpoints, KiRa auth,
proxy/CA, `BENCHMARK_KIRA_CONTEXT_ISOLATION=unique_username` và password DB. Không in/commit secrets.

```powershell
uv run python -m scripts.benchmark.validate_dataset dataset/kira_ltm_v1
uv run python -m scripts.benchmark.run compile `
  --root dataset/kira_ltm_v1 --seed 742 --output artifacts/benchmark/compilation-current.json
```

Outputs create-only; chọn path mới nếu artifact cũ tồn tại. Không re-authorize policy đã được duyệt
nếu policy/đích provider không thay đổi. Laptop gọi OpenAI nhưng không gọi KiRa; PC gọi OpenAI+KiRa.
Rewrite PC là gpt-4o-mini; config lấy từ env, không hard-code model/endpoint nội bộ vào code.

## 4. Build một runtime/eval pair

Dùng bundle directory mới cho exact revision mới. Docker cache được reuse; không xóa images/volumes
cũ để né build. Build bắt buộc clean checkout, default `RuntimeRevision=HEAD`:

```powershell
$bundle = "artifacts/benchmark/offline-handoff-current"
./scripts/benchmark/offline_handoff.ps1 -Action Build -BundleDirectory $bundle
./scripts/benchmark/offline_handoff.ps1 -Action MockAcceptance -BundleDirectory $bundle
./scripts/benchmark/offline_handoff.ps1 -Action Validate `
  -BundleDirectory $bundle -EnvFile .env.benchmark.pc.local
./scripts/benchmark/offline_handoff.ps1 -Action StartCurrent `
  -BundleDirectory $bundle -EnvFile .env.benchmark.pc.local
```

Manifest chứa `current-runtime`, `current-eval` và pinned PostgreSQL dependency (3 images).
`StartCurrent` reuse Compose services `candidate-*` và DB `kira_candidate`; tên stack là wiring
hiện có, **không** có nghĩa đang chạy thêm candidate. Tất cả bốn users cùng DB/collection trong run.

Copy references từ manifest vào env/local session đúng image. PowerShell session mới cần set lại
`BENCHMARK_EVAL_IMAGE` từ role `current-eval` trước Docker Compose; env-file giá trị cũ không tự đổi.

```powershell
$manifest = Get-Content "$bundle/image-manifest.json" -Raw | ConvertFrom-Json
$env:BENCHMARK_EVAL_IMAGE = ($manifest.images | Where-Object role -eq "current-eval").reference
$env:BENCHMARK_GATEWAY_HOST = "candidate-gateway"
$env:BENCHMARK_WORKER_HOST = "candidate-worker"
$env:BENCHMARK_POSTGRES_HOST = "candidate-postgres"
$env:BENCHMARK_DATABASE = "kira_candidate"
$env:BENCHMARK_ENV_FILE = ".env.benchmark.pc.local"
```

Reuse CA mount và Docker proxy route đã verify. Không thay proxy/CA chỉ vì revision harness đổi.

## 5. Live preflight và freeze

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile tools run --rm eval-controller preflight `
  --profile pc_openai_acceptance `
  --suite cross_session `
  --formation-mode persistent `
  --provenance-file /materialization/offline-handoff-current/provenance/current.json `
  --output /artifacts/pc-preflight-current/provider-preflight.json
```

Required probes kiểm OpenAI extraction/embedding/rewrite/judge, PostgreSQL/schema, Gateway/Worker
và KiRa thật auth+SSE trên username riêng. Probe PASS là readiness, không kết luận quality tốt.
QA vẫn cần toàn bộ các dependency này dù không chạy component suites. Preflight/freeze phải khớp
selected suites, config và provenance của lượt run. Full-suite preflight PASS có thể dùng cho QA
khi runtime/harness, prompts, dataset và mọi config ngoài selected suites giữ nguyên. Nếu freeze
cũ chỉ khóa full-suite config hash, chạy lại freeze command bên dưới từ provider-preflight JSON
đã giữ, với output path mới; helper tạo binding cho QA subset offline, không gọi provider lại.
Sau PASS, **stop Worker trước native runner** để chỉ runner claim/process formation jobs:

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile candidate stop candidate-worker

docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile tools run --rm --entrypoint python eval-controller `
  -m scripts.benchmark.freeze_pc_preflight `
  --provider-preflight current=/artifacts/pc-preflight-current/provider-preflight.json `
  --dataset-root /app/dataset/kira_ltm_v1 `
  --output /artifacts/pc-preflight-current/freeze.json
```

Giữ artifacts cũ ở path bền vững trong `artifacts`, không archive vào `/tmp` rồi bỏ. Khi người dùng
chỉ giao chuẩn bị, báo READY_FOR_FULL_BENCHMARK/BLOCKED và dừng; không tự chạy corpus.

## 6. Benchmark QA khi đã được yêu cầu

Chạy **một current runtime**, source corpus chung trong mỗi bundle và No-LTM/With-LTM cho từng QA:
Native `run` mặc định chọn `cross_session` nếu bỏ `--suite`; command dưới đây ghi rõ scope.

```powershell
docker compose --env-file .env.benchmark.pc.local -f compose.benchmark.yaml `
  --profile tools run --rm eval-controller run `
  --profile pc_openai_acceptance `
  --suite cross_session `
  --formation-mode persistent `
  --provenance-file /materialization/offline-handoff-current/provenance/current.json `
  --dataset-root /app/dataset/kira_ltm_v1 `
  --artifact-root /artifacts/pc-openai/current-run/current `
  --seed 742
```

Resume dùng đúng command/artifact root; manifest/isolation ledger khóa identities. Không chỉnh
runtime/prompt/config/dataset giữa lượt; report ghi rõ QA-only và đủ 209 QA đã chọn. Nếu Compose phiên bản
PC gặp lỗi args-form đã verify trước, dùng `--entrypoint sh -c 'exec python -m ...'` tương đương,
giữ nguyên arguments/provenance; không sửa logic benchmark vì wrapper lỗi.

Để kiểm component, dùng cùng command với artifact root mới và preflight/freeze khớp scope:

| Mục đích | Suite flags |
| --- | --- |
| Formation component, 62 cases | `--suite formation` |
| Rewrite component, 54 cases | `--suite rewrite` |
| Retrieval component trên gold corpus và formed corpus | `--suite formation --suite retrieval` |
| Diagnostic đầy đủ cả bốn suites | `--suite formation --suite retrieval --suite rewrite --suite cross_session` |

Retrieval component cần formation suite cùng run để có formed corpus. Product QA vẫn dùng scoped
Mem0 retrieval và rewrite khi chỉ chọn `cross_session`; bỏ component suites không tắt product flow.
Giữ evidence component cũ với cấu hình/revision thực đã đo, không gắn nó thành kết quả chạy mới.

## 7. Báo cáo và acceptance

Report tự động của run ghi selected suites và các metrics tương ứng, same-runtime No-LTM/With-LTM
uplift/regression, latency và calls/token usage. Component không chọn không gây thiếu completeness
của QA-only và không được ghi PASS mới. Báo usage thiếu là unknown thay vì 0; unique source formation chỉ tính
một lần mỗi bundle, QA không tính lại source cost. Semantic metrics chưa human-audit là diagnostic.
Scorecard/audit chính thức là tooling riêng, không tạo thêm variants chỉ để dùng tool đó.

```powershell
uv run python -m scripts.benchmark.check_pc_acceptance `
  --run current=artifacts/benchmark/pc-openai/current-run/current `
  --dataset-root dataset/kira_ltm_v1 `
  --image-manifest artifacts/benchmark/offline-handoff-current/image-manifest.json `
  --pc-preflight artifacts/benchmark/pc-preflight-current/freeze.json `
  --output artifacts/benchmark/pc-openai/current-run/pc-acceptance.json
```

Acceptance kiểm completeness của selected suites, lỗi dependency/protocol, safety và exact provenance. Nó không tự
kết luận chất lượng tốt. Report cần phân biệt runtime/transport lỗi với semantic FAIL và uncertainty;
không dùng Langfuse/OTel thay scorer. PC `official=false`; Qwen/K8s phải đo riêng theo model thật.

## 8. So sánh historical chỉ khi được yêu cầu riêng

Không phải default của PC run. `Build -CompareHistorical -CandidateRevision @("<full-sha>")` giữ
workflow comparison cũ; khai báo scope theo diff thật. Control observation-only là
`05a2d17ff9d10bb410a65eb0e662618d55930e2d`, ref `codex/benchmark-control-observation-guard`, descendant
của `75deb1d8`; package .3/schema2. Counter list guard giữ native null/false/0 behavior. Không sửa
historical runtime hoặc migrate DB của nó bằng current runtime. Backport helper bắt buộc output
root riêng, không ghi đè SDK đang checkout.

Export/Import/Publish exact image bundles tiếp tục dùng manifest, checksums và acceptance freeze.
Không cần chạy chúng để hoàn tất PC benchmark hiện tại. Product memory architecture vẫn native
Mem0 formation/search, CONVERSATION/GLOBAL, receipts và deletion/fencing. Không get_all, không schema
migration hoặc subsystem memory mới. Xem [global-evidence-rollout.md](global-evidence-rollout.md)
cho production rollout; không dùng báo cáo PC để claim Qwen rollout PASS.
