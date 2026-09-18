# Week 5 — Domain Adaptation và Benchmark KiRa

## Trạng thái và baseline

T5.1–T5.2 hoàn tất về implementation: plan/control contract, eval types và provider preflight.
[T5.2 evidence](week5-t5.2-preflight.md): ba OpenAI probes và PostgreSQL/pgvector live pass.
T5.3–T5.4 đang triển khai: corpus nguồn có bốn storyline, manifest/checksum, full-corpus scope, schema, typed
loader và validator deterministic. Corpus chưa materialize KiRa, chưa có reviewer revision và chưa
được chuyển thành bốn suite `EvalCase`; semantic benchmark chưa chạy.

- Control: `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00`, kết thúc Week 4.
- Application `0.4.1`; `viettel-mem0==2.0.20+viettel.3`; memory schema version `2`.
- Extraction policy `kira-memory-policy-v2`; rewrite prompt `2`.
- [Control manifest](week5-baseline.json) khóa provenance và các default liên quan.
- [Benchmark contract v3](week5-benchmark-contract.md) quy định profile, scoring, runtime
  provenance và late performance guardrail.
- [Week 4 release evidence](week4-t4.23-release-evidence.md) là evidence kế thừa, không phải
  kết quả kiểm thử mới của Week 5.

Mục tiêu: đo formation → retrieval → context/rewrite → cross-session trên corpus tiếng Việt
synthetic có gold labels được review; chỉ thay prompt/config khi có bằng chứng tốt hơn control.
Không đặt SLO chất lượng 90%/85% tùy ý và không dùng mock để công bố chất lượng model thật.

## Quyết định giữ nguyên

- PostgreSQL là source of truth cho conversation và queue; bounded recent context, current query riêng.
- Worker at-least-once; formation idempotent theo `event_id` nhờ atomic vector/receipt commit.
  Queue completion là transition riêng. Không quay về exact-hash/top-k để bảo đảm retry.
- Native Mem0 V3; adapter tôn trọng lifecycle trả về, không ép ADD-only và không port engine cũ.
- Native dual-source extraction: user/assistant đều có thể cung cấp evidence; giữ attribution,
  hỗ trợ user xác nhận đề xuất assistant. Không đổi sang user-only extraction.
- Current > Recent > LTM; memory không phải nguồn identity, quyền hạn hoặc authorization.
- Redis, Mem0 REST service, LTM summary, exact tokenizer, fine-tuning, reranker, automatic
  correction/forget và thuật toán semantic dedup mới đều ngoài scope.

## Môi trường và dữ liệu

| Profile | Mục đích | Điều kiện và giới hạn |
| --- | --- | --- |
| `mock` | Contract, scorer và integration deterministic | Không chứng minh chất lượng extraction/retrieval ngữ nghĩa của model thật |
| `external_synthetic` | Đo thực tế trên laptop/PC có Internet | LLM + embedding bên thứ ba; chỉ dữ liệu synthetic, model/provider phải được ghi trong run manifest |
| `internal_test` | Nghiệm thu môi trường nội bộ | Qwen 14B qua API khi được cấp; chưa có endpoint/model deployment chính xác hoặc embedding nội bộ |

K8s Test không có Internet/GPU. Không giả định Qwen có JSON mode, batch embedding hoặc một model
name cụ thể trước preflight. Profile thiếu dependency ghi `NOT_RUN`/`DEPENDENCY_ERROR`, không giả
là đã pass. KiRa thật là gate task-success riêng; mock KiRa chỉ chứng minh query/SSE contract.
Không chuyển KiRa transcript, kết quả nội bộ hay dữ liệu người dùng thật sang provider bên ngoài.

Corpus hiện có 209 QA trên bốn storyline, dùng như một bộ acceptance `full_corpus`: mọi official
run chạy toàn bộ case đủ điều kiện. `scenario_group` và memory family chỉ là chiều báo cáo/audit,
không chia dev/holdout. Vì người phát triển có thể đã xem toàn bộ corpus, kết quả không được trình
bày như bằng chứng generalization trên unseen holdout. Taxonomy là nhãn đánh giá, không phải schema
lưu memory. User/mentor review gold labels trước khi chấm semantic pass chính thức.

## Batch A — Hợp đồng, corpus và harness nền tảng

| Task | Nội dung / Definition of Done | Phụ thuộc | Trạng thái |
| --- | --- | --- | --- |
| T5.1 | Lưu plan; khóa control manifest; chốt scope tuning, profiles, metrics, candidate selection và safety gates | Week 4 | DONE |
| T5.2 | Eval types + provider preflight: typed case/config/result; probe chat/JSON extraction, embedding batch/dimension và DB theo dependency từng suite; lỗi typed, output không lộ secret | T5.1 | DONE |
| T5.3 | Synthetic dataset v1: đủ bốn suite, positive/negative và domain slices; gold IDs, evidence, constraints và allowed attribution; trạng thái draft/reviewed rõ | T5.2 | IN_PROGRESS — 4 storyline/209 QA đã đóng gói; còn materialization, reviewer revision và chuyển sang 4 suite |
| T5.4 | Validator + scope: validate schema, unique IDs, gold references, normalized exact duplicates, full-corpus scope, seed reproducibility và checksums; semantic near-duplicate do reviewer kiểm tra | T5.3 | IN_PROGRESS — manifest/checksum, full-corpus scope và validator deterministic đã có; còn semantic near-duplicate review |
| T5.5 | Scorer/human review/report: deterministic checks + review semantic thủ công; CLI dự kiến `validate`, `preflight`, `run`, `review`, `compare`; kết quả có denominator, coverage, error breakdown, runtime/harness SHA, prompt/config hashes và package versions | T5.4 | NOT_STARTED |

Checkpoint A: harness chạy offline được với mocks; corpus có checksum và review status;
chỉ corpus đã review mới tạo semantic verdict. T5.1 **không** bao gồm code harness, provider
probe, dataset hay một lần gọi model có phí.

## Batch B — Đánh giá formation và thử prompt/config

| Task | Nội dung / Definition of Done | Phụ thuộc |
| --- | --- | --- |
| T5.6 | Write-free evaluator đi qua native V3 extraction; quan sát raw output/parse outcome, tách empty hợp lệ khỏi malformed JSON; không sửa parser baseline âm thầm | T5.5 |
| T5.7 | Persistent evaluator đi qua application/Mem0/pgvector boundary thật; đối chiếu raw facts với memories/receipt thực sự commit | T5.6 |
| T5.8 | Multi-turn và dedup/retry cases: paraphrase, top-k miss, before/after commit; fresh event cho quality, cùng event cho replay; đo duplicate giữa các event độc lập | T5.7 |
| T5.9 | Formation report: precision/recall/F1, unsupported facts, formula/attribution, negatives và duplicate rate; phân tích theo taxonomy/case family | T5.8 |
| T5.10 | Chạy historical control trước; tối đa hai declared candidates trên cùng full corpus; ghi prompt/config hashes và mọi thay đổi runtime/dependency/schema/lifecycle; chọn theo metric/guardrail đã khóa, không cherry-pick case | T5.9 |

Checkpoint B: có baseline formation report và candidate có evidence trên full corpus; chấp nhận kết
luận “giữ baseline”. Không tự động làm correction/forget hoặc sửa cross-event dedup.

## Batch C — Retrieval, rewrite và cross-session

| Task | Nội dung / Definition of Done | Phụ thuộc |
| --- | --- | --- |
| T5.11 | Gold-memory seeding **evaluation-only**, `infer=False`, mapping gold ID ↔ persisted ID; tách corpus gold và corpus formation thật | T5.5 |
| T5.12 | Retrieval evaluator gọi pipeline native với user scope; Recall@1/3/5, MRR, Precision@K và no-hit FP; zero cross-user leakage | T5.11 |
| T5.13 | Diagnostic grid top-k `[1,3,5,10]`, threshold `[0,0.1,0.3,0.5,0.7]` trên full corpus; cùng embedding/corpus; không coi native hybrid score là cosine similarity | T5.12 |
| T5.14 | Context/rewrite evaluator: constraints + semantic equivalence, exact formulas, Current > Recent > LTM, standalone/topic-switch/ambiguity/injection | T5.5, T5.12 |
| T5.15 | Cross-session + ablation Current-only / Recent-only / LTM-only / Recent+LTM bằng eval wiring; Session B đợi đúng job A complete có deadline; đo memory readiness riêng | T5.7, T5.14 |

Checkpoint C: so sánh gold vs formed corpus và bốn ablation trên scenario tương đương, fresh state;
retrieval query đang chấm không tự tạo thêm memories làm nhiễm corpus. Bao phủ taxonomy, KPI ID,
địa danh Việt Nam, paraphrase, no-hit, focus còn/hết hạn, conflict và user isolation. Expired focus
hoặc explicit correction có lỗi phải hiện trong report, không được tuyên bố đã có automatic TTL/update.

## Batch D — Repeated semantic confirmation, late performance và handoff

| Task | Nội dung / Definition of Done | Phụ thuộc |
| --- | --- | --- |
| T5.16 | Timing instrumentation theo stage: formation, retrieval, rewrite, KiRa TTFT/completion, queue wait và memory readiness; tách SDK/Worker retries; mock kiểm tra counts và percentile | T5.10, T5.15 |
| T5.17 | Freeze candidate; ba paired semantic run độc lập control/candidate trên toàn bộ corpus cùng profile; kiểm tra primary metric, case-family gain và safety; chưa dùng performance để cứu candidate semantic fail | T5.16 |
| T5.18 | Late performance chỉ cho candidate đã vượt T5.17: 5 warm-up, mục tiêu 30 success, cap 40 attempts/variant; verdict `acceptable`, `reject_regression` hoặc `needs_more_samples` | T5.17 |
| T5.19 | Promote candidate đủ evidence hoặc giữ control; chạy Week 1–4 regression, coverage ≥90%, scorer/validator tests, PostgreSQL/memory compatibility và T4.19–T4.22 smoke; build eval image offline riêng | T5.18 hoặc quyết định giữ control ở T5.17 |
| T5.20 | Acceptance bundle: manifests có runtime/harness provenance, corpus/review checksum, reports, limitations và quyết định; runbook save/load/internal registry cho Week 6 | T5.19 |

Checkpoint D: kết luận riêng local benchmark và internal acceptance. Eval image được build trên
máy có Internet, kèm dependencies/corpus synthetic đã duyệt; runtime không tải model/pip, không
cần GPU. Chưa triển khai Helm/Rancher/IAM trong Week 5. Endpoint chưa cấp thì internal gate vẫn
`NOT_RUN`, không đánh dấu toàn bộ internal acceptance hoàn thành.

T5.6–T5.20 hiện đều `NOT_STARTED`. Thực hiện tuần tự theo task/checkpoint khi được yêu cầu;
không dùng việc có dependency graph để trộn thay đổi của các task chưa được giao.

## Components và ranh giới sửa đổi

- T5.1 chỉ thêm tài liệu/manifest và README links; giữ nguyên `app/`, `worker/`, migrations,
  custom Mem0, Docker/Compose, dependency versions và lock.
- T5.2 đã thêm `evaluation/`, `scripts/run_week5_benchmark.py preflight` và tests riêng.
  Các lệnh validator/scorer cùng eval-image definition sẽ có ở task sau; chưa chạy benchmark.
- Prompt/config runtime chỉ được thay có chủ đích tại T5.19 sau evidence. Candidate experiments
  nằm trong evaluation trước đó; algorithm/lifecycle/schema change cần kế hoạch review riêng.
- Mỗi write suite dùng disposable DB tách khỏi DB có live Worker, schema/collection/user theo run;
  cleanup chính xác theo manifest, không thao tác broad delete trên dữ liệu ứng dụng.

Effort dự kiến 45–60 giờ kỹ thuật, chưa tính gold review và chờ provider. Thiếu endpoint không
cản Batch A và mock integration; không được thay benchmark thật bằng số giả để đóng gate.
