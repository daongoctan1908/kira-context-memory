# Internal K8s benchmark discovery, confirmation và promotion

Runbook này bắt đầu **sau** khi Phase 4 trên PC công ty đã tạo một offline bundle hợp lệ. Đây là
nguồn thao tác cho AI/operator trên PC, VDI và Kubernetes. Không được dùng kết quả OpenAI ở Phase 4
để thay cho kết quả internal model ở đây.

## 1. Trạng thái phần đã làm được trên laptop

Đã có và đã được unit/contract test offline:

- native runner cho đủ bốn suite với profile `internal_test`;
- explicit extraction/embedding/rewrite/judge providers, không fallback external;
- persistent formation, dual-mode retrieval, paired No-LTM/With-LTM và KiRa SSE thật;
- run-owned schema/collection/user/session, crash resume và exact cleanup;
- profile `internal_test` tự ghi `official=true`; profile PC không thể claim official;
- offline bundle import kiểm checksum, tar và local image ID;
- publish lên registry tạo `registry-manifest.json` create-only, bind source image manifest và ghi
  immutable `repository@sha256:...` cho từng runtime/eval image;
- deterministic scorers, semantic-judge contract, tự sinh content-free audit candidates từ run
  artifact, audit sampling/reconciliation và timing models.

Validation snapshot laptop ngày 2026-09-19:

```text
ruff check / format check        PASS
pytest                           1084 passed, 78 skipped
coverage                         90.00%
Docker/K8s/internal providers    NOT_RUN
```

Những điều **chưa** được chứng minh trên laptop:

- bundle Phase 4 thật chưa được chứng minh; dataset hiện đã materialize/review và
  `benchmark_ready`, cần validate bản freeze trước khi chạy Phase 4;
- image chưa build/import/push;
- chưa biết registry, namespace, ServiceAccount, Secret mechanism hoặc managed PostgreSQL thực tế;
- binding/capability của extraction, embedding và judge nội bộ chưa được cấp đầy đủ;
- chưa có K8s preflight, discovery, confirmation hoặc performance artifact.

Mọi mục trên giữ `NOT_RUN`; không thay bằng mock hoặc OpenAI.

Binding rewrite production được user cung cấp ngày 2026-10-02: Qwen3-14B base qua vLLM tại
`http://10.254.135.40:8080/v1`, model ID `/models/Qwen3_14B`, không dùng `genai-lora`.
`evaluation/benchmark.internal.env.example` đã ghi binding này cho cả evaluator và Gateway.
Đây là cập nhật cấu hình; internal preflight và live readiness vẫn chưa được chứng minh.

## 2. Quy tắc chung

```text
T5.1  VDI import + immutable publish
  -> T5.2 K8s deployment/preflight
  -> T5.3 internal technical smoke
  -> T5.4 full-corpus discovery/tuning loop
  -> freeze candidate
  -> T5.5 three paired confirmation repetitions
  -> T5.6 late performance + promotion/keep-control
```

- Dataset là canonical full corpus; không dev/holdout split.
- Control và candidate trong một pair dùng cùng dataset/config/order/seed/provider deployments.
- Mỗi variant/repetition dùng database riêng trên managed PostgreSQL. Không dùng chung `public`
  state giữa hai arm hoặc hai repetition.
- Normal Worker chỉ chạy để preflight readiness. Trước native run phải dừng nó; eval-controller tự
  claim/process exact memory job trong run-owned schema.
- Không chạy external provider trong profile `internal_test`.
- OTel/Langfuse chỉ debug; outage không được đổi business/benchmark outcome.
- Dependency/protocol failure không được đổi thành quality `FAIL`.
- Không tune gold, scorer hoặc acceptance rule sau khi xem candidate output.

## 3. T5.1 — VDI import và immutable registry publish

Chuyển đúng các file trong `bundle-manifest.json` sang VDI. Không chuyển `.env`, KiRa checkpoint,
provider response, database dump hoặc Langfuse export.

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Import `
  -BundleDirectory <transferred-bundle>
```

Import phải PASS checksum mọi file, image tar và exact local image IDs. Sau đó publish:

```powershell
./scripts/benchmark/offline_handoff.ps1 -Action Publish `
  -BundleDirectory <transferred-bundle> `
  -Registry registry.internal.example/team
```

Acceptance T5.1:

- `registry-manifest.json` được tạo đúng một lần;
- `source_image_manifest_sha256` khớp `image-manifest.json` trong bundle;
- mỗi control/candidate runtime và eval image có đúng một `immutable_reference` dạng
  `repository@sha256:<64 hex>`;
- `source_image_id` vẫn khớp image đã được Phase 4 acceptance;
- deployment sau đây chỉ dùng immutable reference, không dùng tag hoặc `latest`.

Nếu VDI không được push registry, giữ import local làm evidence nhưng T5.2 vẫn `NOT_RUN` cho tới khi
cluster có cơ chế load đúng digest/image ID được platform phê duyệt.

## 4. T5.2 — K8s deployment và internal preflight

Không hard-code YAML cluster cụ thể trong repo trước khi biết platform. AI/operator phải ghi lại:

- cluster/context và namespace;
- immutable runtime/eval image references từ `registry-manifest.json`;
- ServiceAccount, imagePullSecret và NetworkPolicy cần thiết;
- KiRa endpoint/service identity;
- bốn internal provider endpoint/model/deployment, embedding dimension và JSON mode;
- managed PostgreSQL host/database mapping;
- artifact PVC/object-store path;
- resource requests/limits do platform owner duyệt.

Tạo secret bằng secret manager/K8s Secret ngoài Git. Không render secret vào artifact hoặc terminal
log. `evaluation/benchmark.internal.env.example` chỉ là danh sách field, không phải Secret manifest.

Mỗi preflight dùng exact eval digest và args:

```text
python -m scripts.benchmark.run preflight
  --profile internal_test
  --suite formation --suite retrieval --suite rewrite --suite cross_session
  --formation-mode persistent
  --provenance-file <exact variant provenance JSON>
  --output <artifact path>/internal-preflight/<variant>.json
```

Environment phải map explicit:

```text
BENCHMARK_EXTRACTION_*
BENCHMARK_EMBEDDING_*
BENCHMARK_REWRITE_*
BENCHMARK_JUDGE_*
BENCHMARK_DATABASE_URL
BENCHMARK_MEMORY_DATABASE_URL
BENCHMARK_GATEWAY_URL
BENCHMARK_WORKER_URL
KIRA_*
BENCHMARK_KIRA_CONTEXT_ISOLATION=unique_username
```

Acceptance T5.2:

- exact image digest/provenance/dataset/package/prompt hashes khớp bundle;
- extraction envelope và judge schema PASS;
- embedding dimension/model khớp memory metadata;
- pgvector, migrations, Gateway, Worker và KiRa route PASS;
- username mới có quyền tương đương và history độc lập; case/arm/attempt không reuse KiRa context;
- không endpoint nào trỏ external Internet;
- missing/unsupported dependency giữ `NOT_RUN`/typed error;
- telemetry backend tắt hoặc unreachable vẫn không làm readiness/business fail.

Sau preflight, dừng normal Worker của đúng variant trước khi chạy native benchmark.

## 5. T5.3 — internal technical smoke

Native runner cố ý không cho chạy partial canonical suite. Technical smoke ở đây gồm preflight PASS,
network/policy check và một disposable deployment rehearsal bằng mock/network-disabled image nếu
platform yêu cầu; **không** lấy subset canonical để claim quality.

Trước full corpus, xác minh:

- database của run mới/rỗng;
- eval-controller ghi được artifact PVC;
- cancellation để lại manifest/ledger resumable;
- job restart đọc lại đúng run directory;
- cleanup chỉ xóa run-owned users/schema;
- một namespace khác hoặc user khác không bị chạm;
- raw prompt/response/credential không xuất hiện trong log/artifact.

Nếu không thể rehearsal mà không gọi model thật, bỏ rehearsal và chuyển thẳng sang một T5.4 run có
budget được duyệt; không tạo hidden probe trên canonical data.

## 6. T5.4 — full-corpus discovery/tuning loop

Chạy control trước, sau đó từng declared candidate, tuần tự. Mỗi run dùng fresh database và artifact
directory riêng:

```text
python -m scripts.benchmark.run run
  --profile internal_test
  --suite formation --suite retrieval --suite rewrite --suite cross_session
  --formation-mode persistent
  --provenance-file <variant provenance JSON>
  --dataset-root /app/dataset/kira_ltm_v1
  --artifact-root <artifact root>/internal-discovery/<iteration>/<variant>
  --seed 742
```

Discovery được phép xem lỗi và cải tiến candidate. Report riêng, không gộp score tổng:

- Formation: Precision/Recall/F1;
- Retrieval formation-produced: Recall@3/MRR@10; gold-fixture là diagnostic pipeline;
- Rewrite: deterministic constraints + semantic judge;
- Final QA: semantic judge + task success;
- Safety: hard fail;
- dependency/protocol/cleanup/contamination: blocking technical error.

Nếu candidate cần sửa:

```text
K8s discovery
  -> quay về PC công ty
  -> sửa + commit candidate SHA mới
  -> build exact image set mới
  -> PC technical acceptance lại
  -> export/import/publish bundle mới
  -> K8s T5.2/T5.4 lại
```

Không patch container đang chạy và không tái dùng digest cũ. Tối đa hai declared candidates trong
một vòng. Chỉ freeze một candidate sau khi discovery đủ evidence; OpenAI PC result không quyết định.

## 7. Human audit trước confirmation

Từ semantic outputs của control/candidate, tạo audit candidates đã bind `case_id + output_sha256`.
Policy bắt buộc:

- audit toàn bộ `UNCERTAIN`;
- audit toàn bộ deterministic/judge disagreement;
- audit một global budget `ceil(10% × tổng semantic PASS/FAIL)`, stratified theo
  variant/suite/verdict/bundle;
- khi sample phát hiện disagreement, mở rộng đúng stratum theo contract;
- `GOLD_ERROR` quay lại sửa/version/freeze dataset và chạy lại; không override tại chỗ;
- human verdict không thể xóa safety failure.

Mỗi decision phải giữ nguyên `case_id`, `subject` và `output_sha256` từ batch. `subject` tách các
semantic output trong cùng case: rewrite của With-LTM và final answer của cả No-LTM/With-LTM.
No-LTM chuyển nguyên query nên không chạy rewrite judge; rewrite constraints chỉ là diagnostic
và không quyết định outcome của baseline.

Sinh candidates trực tiếp từ từng completed native run; command kiểm profile, dataset hash, thứ tự
case và bind từng record vào hash của exact attempt output:

```text
python -m scripts.benchmark.run audit candidates
  --run-root <artifact root>/internal-discovery/<iteration>/<variant>
  --dataset-root /app/dataset/kira_ltm_v1
  --output <audit root>/<variant>-candidates.jsonl
```

Sau đó dùng policy cố định để export batch và import quyết định của reviewer:

```text
python -m scripts.benchmark.run audit export
  --candidates <audit root>/<variant>-candidates.jsonl
  --seed 742
  --sample-rate 0.10
  --output <audit root>/<variant>-batch.json

python -m scripts.benchmark.run audit import
  --candidates <audit root>/<variant>-candidates.jsonl
  --batch <audit root>/<variant>-batch.json
  --decisions <audit root>/<variant>-decisions.jsonl
  --output <audit root>/<variant>-reconciliation.json
```

Không tự viết candidates bằng tay và không bắt human chấm toàn bộ deterministic output.

## 8. T5.5 — three paired confirmation repetitions

Sau khi freeze candidate, không tune nữa. Chạy ba pair độc lập:

```text
repetition 1: control -> candidate, seed cố định của pair
repetition 2: control -> candidate, seed cố định của pair
repetition 3: control -> candidate, seed cố định của pair
```

Mỗi run dùng run ID, isolation plan, database và artifact root mới. Seed có thể khác giữa repetition
nhưng phải khai báo trước và giống nhau trong cùng pair. Không chọn “best of three”.

Confirmation chỉ đủ evidence khi:

- cả sáu run đủ toàn bộ eligible cases và bốn suites;
- không dependency/protocol/unexpected `NOT_RUN`;
- audit bắt buộc hoàn tất và hash-bound;
- zero safety hard fail;
- primary aggregate của component được tune tăng;
- ít nhất 2/3 paired repetitions có primary metric cùng hướng tăng;
- guardrail aggregate không giảm;
- With-LTM final QA tốt hơn No-LTM;
- task success không giảm khi observable.

Metric/guardrail theo component:

| Component | Primary | Guardrail |
| --- | --- | --- |
| Formation | F1 tăng | Precision và Recall không giảm |
| Retrieval | Recall@3 tăng | MRR@10 không giảm |
| Rewrite | semantic pass rate tăng | constraint pass rate không giảm |
| Final QA | semantic pass tăng | task success không giảm khi observable |

Không weighted aggregate. Tie, thiếu evidence hoặc component không đổi bị regression thì giữ control.

Sau khi import audit của từng run, tạo sáu scorecard rồi aggregate. Ví dụ cho một run:

Nếu human override một quyết định formation, scorecard cố ý trả evidence chưa đủ thay vì tự sửa
TP/FP/FN từ một verdict rời rạc. Phải sửa gold nếu cần hoặc re-score/rerun formation để tạo artifact
nhất quán, sau đó export/import audit lại.

```text
python -m scripts.benchmark.run release scorecard
  --run-root <run root>
  --dataset-root /app/dataset/kira_ltm_v1
  --audit-reconciliation <audit root>/<run>-reconciliation.json
  --output <release root>/<run>-scorecard.json
```

Sau đó truyền đúng ba scorecard mỗi phía, theo cùng thứ tự repetition:

```text
python -m scripts.benchmark.run release confirm
  --component <formation|retrieval|rewrite|final_qa>
  --control <control-r1.json> --control <control-r2.json> --control <control-r3.json>
  --candidate <candidate-r1.json> --candidate <candidate-r2.json> --candidate <candidate-r3.json>
  --output <release root>/confirmation.json
```

Command fail-closed nếu run không phải `internal_test`, corpus/seed không paired, runtime/config bị
drift, audit stale/chưa hoàn tất hoặc evidence thiếu.

## 9. T5.6 — late performance và promotion

Chỉ chạy nếu candidate vượt semantic confirmation. Đo đúng stage bị candidate tác động:

- 5 warm-up mỗi variant;
- mục tiêu 30 successful measured operations;
- cap 40 attempts mỗi variant;
- percentile nearest-rank từ 30 success đầu tiên;
- báo error/timeout, SDK retry và Worker retry;
- thiếu 30 success là `INSUFFICIENT_EVIDENCE`.

Reviewer ghi đúng một verdict:

```text
acceptable
reject_regression
needs_more_samples
```

Chỉ `acceptable` mới cho promotion. Performance không cứu semantic failure. Sau đó:

- promotion candidate hoặc ghi quyết định giữ control;
- chạy full regression hiện tại, coverage ≥90%, migration/schema compatibility và async/retry/crash/outage
  smoke trên exact promoted image;
- lưu report, audit, preflight, run manifests, image/registry manifests và limitation;
- không đưa secret, vector dump hoặc raw internal transcript vào release evidence.

Typed reviewer/promotion commands (file performance evidence do live K8s workload collector tạo):

```text
python -m scripts.benchmark.run release performance
  --confirmation <release root>/confirmation.json
  --evidence <release root>/performance-evidence.json
  --verdict <acceptable|reject_regression|needs_more_samples>
  --reviewer <reviewer-id>
  --rationale <review-rationale>
  --output <release root>/performance-review.json

python -m scripts.benchmark.run release promote
  --confirmation <release root>/confirmation.json
  --performance <release root>/performance-review.json
  --images <release root>/exact-images.json
  --cleanup <release root>/cleanup-evidence.json
  --decision <promote_candidate|keep_control|insufficient_evidence>
  --reviewer <reviewer-id>
  --rationale <decision-rationale>
  --output <release root>/promotion.json
```

Không sửa tay các derived report. `promotion` chỉ cho phép promote khi confirmation PASS,
performance `acceptable` đủ 30 successes/variant và cleanup hoàn tất.

## 10. Việc AI trên PC phải hoàn tất trước khi chuyển bundle

PC AI không chạy Phase 5 model benchmark, nhưng phải chuẩn bị để VDI/K8s không phải quay lại sửa
harness:

1. hoàn tất toàn bộ Definition of Done Phase 4 trong `RUNBOOK.md`;
2. bảo đảm bundle chứa `K8S-RUNBOOK.md` và `benchmark.internal.env.example`;
3. chạy mock acceptance trên mọi exact eval image;
4. xác minh eval image có native `internal_test` runner và bốn suite;
5. ghi candidate declarations/provenance rõ ràng;
6. không thêm internal endpoint giả vào bundle;
7. chuyển bundle cùng checksum qua kênh nội bộ được duyệt.

Phase 5 chỉ được đánh dấu complete sau live T5.1–T5.7 evidence. Laptop completion của tooling không
phải K8s acceptance.
