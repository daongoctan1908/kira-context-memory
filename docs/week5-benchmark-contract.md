# Week 5 — Benchmark contract v1

Status: **frozen for T5.1**, 2026-09-11. Contract ID: `kira-week5-benchmark-v1`.
Đây là đặc tả cho implementation tiếp theo, chưa phải harness hoặc benchmark result.

Liên quan: [Week 5 plan](week5-plan.md), [control manifest](week5-baseline.json),
[Week 4 release evidence](week4-t4.23-release-evidence.md).

## 1. Control và phạm vi so sánh

Control bất biến là source tại `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00`, không phải HEAD
của nhánh Week 5 và không phải một image tag mutable. Manifest ghi cả Git tree và blob IDs.
Các run sau phải ghi source SHA riêng của control, candidate và harness; image dùng digest/image
ID thực tế. `kira-context:0.4.1` chỉ là source release reference, không chứng minh cùng binary.

Baseline giữ nguyên app `0.4.1`, package `2.0.20+viettel.3`, memory schema `2`, native Mem0 V3,
policy `kira-memory-policy-v2` và rewrite prompt `2`. Trường cấu hình Mem0 `version="v1.1"`
không phải version của formation engine. Không ép lifecycle thành ADD-only; không thay engine.

Configuration trong manifest là **source defaults**, không phải env của một deployment đã kiểm
chứng. Source tắt `ltm_enabled` và `memory_formation_enabled`; suite cần các capability này phải
bật rõ trong eval wiring cho cả control/candidate. Ghi resolved config, không so bản tắt feature
với bản bật feature rồi gọi đó là hiệu quả tuning. Không dùng test Compose overrides như default
production. Baseline không pin một external provider/model chưa được chọn.

Candidate được phép thay extraction/rewrite prompt hoặc search/config knobs đã có. Giữ nguyên
provider/model revision, embedding/dimension, corpus, state initialization, event protocol và
harness trong một paired comparison. Đổi embedding là experiment riêng: re-embed toàn bộ corpus,
đo lại threshold; không trộn vectors từ hai model hoặc so threshold như thể cùng thang điểm.
Algorithm, schema, lifecycle, correction/forget, cross-event semantic dedup không được sửa kèm.

## 2. Run profiles và dependency gates

| Profile | Cho phép kết luận | Không cho phép kết luận |
| --- | --- | --- |
| `mock` | Contract, deterministic scoring, DB/receipt/SSE plumbing nếu dependency thật được dùng | Semantic quality hoặc latency của model thật |
| `external_synthetic` | Quality/performance trên đúng third-party models được ghi trong run | Suitability của Qwen/internal deployment chưa chạy |
| `internal_test` | Kết quả trên đúng endpoint/deployment nội bộ đã preflight | Kết quả KiRa nghiệp vụ nếu chỉ dùng mock KiRa |

Chỉ external synthetic inputs được gửi tới third-party LLM/embedding. Không đưa dữ liệu nội bộ,
raw KiRa responses, conversation thật, credential hoặc secret vào provider request/artifact.
Secret-negative cases dùng canary giả được đánh dấu synthetic, không dùng credential thật.

T5.2 phải preflight theo suite: extraction cần chat provider và JSON contract; retrieval cần
embedding + pgvector; rewrite cần chat provider; persistent formation cần các dependency formation
và DB; full cross-session cần Gateway/Worker/queue và KiRa hoặc double được ghi rõ. Validate/review
offline không đòi provider. Không bắt suite độc lập phải có tất cả endpoint.

K8s Test không Internet/GPU, Qwen 14B API chưa có deployment name/endpoint cụ thể, embedding nội bộ
chưa có. Không suy đoán model version, JSON support hay embedding dimension. Preflight kiểm tra
khả năng thực tế, bao gồm embedding batch/count/dimension và JSON extraction format.

Không cấu hình hoặc chưa lên lịch chạy: `NOT_RUN`. Đã thử nhưng dependency unavailable/timeout:
`DEPENDENCY_ERROR`. Response không tuân contract: `PROTOCOL_ERROR`. Không auto-fallback provider
khác trong cùng experiment hoặc biến provider error thành một case “không cần nhớ” đã pass.

## 3. Corpus, review và split

Target v1: 160 case — formation 60 (36 positive/24 negative), retrieval 40, rewrite 40,
cross-session 20. Đây là target thiết kế, chưa phải dataset đã tồn tại. T5.3/T5.4 ghi số lượng
thực tế và lý do nếu cần đổi; thay đổi phải được review trước chạy chính thức.

Mỗi case cần stable ID, suite, scenario-family ID, split, tags, synthetic provenance, inputs,
gold IDs/constraints, evidence references, allowed attribution và review status. Formation gold
là atomic reusable claims; retrieval gold xác định relevant memory IDs; rewrite gold là các
slot/constraints cần giữ hoặc không được invent, không chỉ một câu exact-match.

- Chia khoảng 70/30 dev/holdout theo family, giữ các paraphrase/episode liên quan cùng split.
- Week 2/3 seeds và mọi biến thể nhận biết được chỉ vào dev.
- Validator kiểm tra normalized exact duplicates, IDs, references và split; reviewer kiểm tra
  semantic overlap. Không quảng cáo exact normalization là semantic leakage detector.
- Gold có `draft`/`reviewed`, reviewer decision và revision/hash; user/mentor duyệt nội dung.
  Không tự nhận “reviewed” chỉ vì schema pass hoặc LLM vừa sinh corpus.
- Người review có thể xem holdout gold để duyệt; vòng tuning không được dùng holdout outputs/errors.
  Freeze gold + split + candidate trước T5.17. Sửa gold sau khi thấy kết quả cần revision mới và
  rerun cả control/candidate; không cherry-pick case hoặc sửa label để candidate thắng.

Coverage bắt buộc: sáu taxonomy, confirmation/ellipsis và dual-source attribution; formula,
operator/unit/threshold; ordinary-query entity, greeting, transient KPI, assistant guess, fake
secret và inferred authorization; Vietnamese location/time/KPI ID; paraphrase/no-hit; temporary
focus active/expired; conflict, standalone, topic switch, ambiguity, prompt injection, cross-user.
Taxonomy chỉ dùng cho report, không thêm taxonomy metadata vào persisted memory.

## 4. Evaluation boundaries và state isolation

Formation có hai view: raw native extraction (write-free) và persisted result qua đúng formation
boundary. Raw facts không đồng nghĩa với memories đã commit. Native parser có thể nuốt malformed
JSON thành empty result: eval instrumentation phải nhận diện và ghi `PROTOCOL_ERROR`, không coi
đó là true-negative hợp lệ. Không thay parser baseline âm thầm; nếu không quan sát được thì gate
đó chưa đủ evidence, không suy đoán thành công từ empty receipt.

Memory facts phải grounded trong conversation, giữ attribution. User xác nhận “Đúng” có thể nhận
đề xuất trước đó của assistant. Reusable assistant context được đánh giá theo policy native
dual-source; không tự động gắn mọi câu assistant thành preference/fact do user phát biểu.

Mỗi quality trial dùng fresh state và event mới. Same-event retry/reclaim là correctness suite
riêng: paraphrase hoặc top-k miss không được tạo thêm memory sau khi receipt đã commit; crash trước
commit không được để partial vectors/receipt; crash sau commit replay kết quả đầu rồi complete job.
Queue vẫn at-least-once; không tuyên bố toàn bộ side effects SQLite/entity links là exactly-once.
Distinct-event duplicate do overlapping history được đo riêng, không nhầm với event idempotency.

Retrieval có hai corpus độc lập: curated gold memories seeded `infer=False` và memories được hình
thành thật. Dùng native search qua adapter, với user filter; native hybrid scores không mặc định
là cosine similarity. Không thay bằng tự viết SQL nearest-neighbor rồi gọi là baseline Mem0.
Mapping gold ID/persisted ID nằm trong eval artifacts. Query đang đo không được enqueue formation
làm nhiễm corpus retrieval.

Cross-session chạy Current-only / Recent-only / LTM-only / Recent+LTM bằng eval-only wiring trên
cùng scenario và fresh state. Session B phải đợi đúng job Session A completed bằng bounded wait;
deadline được freeze trong run config, timeout hiện rõ. Memory readiness latency tách khỏi response
latency. Mock KiRa kiểm tra standalone query và SSE; nghiệp vụ KiRa thật là gate riêng.

Mọi write suite dùng disposable DB tách khỏi DB có live polling Worker; thêm run-scoped schema,
collection và synthetic users. Cleanup chỉ các resources liệt kê trong manifest, gồm conversation,
jobs, vectors và receipts; kiểm tra owner/run trước delete. Giữ failed-run evidence đã redacted.
Không dùng cleanup rộng lên DB ứng dụng, không dùng memory để authorize truy cập.

## 5. Outcomes và scoring

| Outcome | Ý nghĩa |
| --- | --- |
| `PASS` | Đủ assertions và review bắt buộc, không vi phạm constraints |
| `FAIL` | Có kết quả hợp lệ để chấm nhưng sai gold/constraint/safety |
| `REVIEW_REQUIRED` | Semantic verdict hoặc gold chưa được người review chốt |
| `DEPENDENCY_ERROR` | Provider/DB/transport không thực thi được case |
| `PROTOCOL_ERROR` | Payload/parse/result contract sai, kể cả lỗi bị native parser che |
| `NOT_RUN` | Chưa thực thi hoặc thiếu cấu hình đã biết trước |

Không dùng LLM-as-judge trong v1. Auto checks xử lý cấu trúc và ràng buộc exact; human review chấm
semantic equivalence, evidence, attribution và duplicate. Review record phải gắn output hash,
case ID, reviewer verdict/reason; output đổi thì review cũ không còn hợp lệ.

Report luôn có total eligible, attempted, từng outcome count và số case thực sự được chấm.
Quality score có denominator chỉ rõ; report thêm coverage = scored/eligible. Provider/protocol
errors không trở thành zero-fact true negative, không âm thầm bị loại để score đẹp hơn. Score
trên corpus draft chỉ là provisional. Zero denominator hiển thị `N/A`, không tự cho 100%.

### Formation

Review output thành atomic claims và match với gold IDs: một gold claim chỉ được một TP; unsupported
claims là FP, gold thiếu là FN. Lặp claim đã được credit không được thêm TP; ghi duplicate excess
riêng để không đánh đồng supported duplication với hallucination. Một output chứa hai gold claims
có thể credit hai claim, nhưng chỉ sau khi phân rã và kiểm tra evidence, không theo số string trả về.

- Precision = TP/(TP+FP); recall = TP/(TP+FN); F1 từ hai giá trị này. Báo micro và per-family counts.
- Negative-case pass = không có extracted/persisted claim khi protocol hợp lệ; false-positive
  case rate = negative cases có ít nhất một claim / negative cases được chấm.
- Duplicate excess rate = số supported claim occurrences dư / tổng supported claim occurrences;
  báo separately trong một formation và giữa các formation events. Candidate không được cải thiện
  precision bằng cách sinh thêm cùng một fact: duplicate rate không được tăng khi promotion.
- Formula preservation chấm expression có evidence; chỉ normalize whitespace đã được gold cho
  phép. Không normalize operators, variable names, units hoặc thresholds thành nghĩa khác.
- Attribution, unsupported fact và secret/authorization failures có case-level evidence riêng.

### Retrieval

Positive query với tập gold relevant R: Recall@K = số relevant IDs duy nhất trong top K / |R|;
Precision@K = số relevant IDs trong top K / K, không chia số item thực tế được trả về. Report
Recall@1/3/5 và Precision@K của cấu hình đang chạy. Duplicate IDs không thêm credit.
MRR dùng reciprocal rank của relevant hit đầu tiên, không hit là 0; báo rõ depth tối đa
(baseline tối đa 10) và không trộn MRR từ depth khác mà giấu config.

No-hit queries không vào recall/MRR denominator; báo riêng false-positive query rate = số no-hit
queries có ít nhất một returned memory / tổng no-hit queries được chấm. Cross-user hit là safety
failure kể cả score thấp hoặc không được ContextBuilder sử dụng. Gold-vs-formed reports tách nhau.

### Rewrite và cross-session

Semantic pass cần giữ intent và đúng required slots/constraints, không invent KPI/date/location,
không trả lời nghiệp vụ. Exact string match chỉ là diagnostic. Query standalone/topic-switch không
bị ép dùng history; không đủ evidence thì giữ ambiguity. Current > Recent > LTM là hard assertion.

Cross-session pass cần formation evidence + đúng-user recall + đúng context/rewrite và không vi
phạm safety; bounded wait hết hạn không được đổi thành semantic miss thông thường. Report KiRa
task-success riêng khi có KiRa thật và gold nghiệp vụ được duyệt.

## 6. Candidate selection và performance gate

T5.10 chạy control trước, thử tối đa hai prompt/config candidate trên dev. T5.13 grid là dev-only;
tổ hợp cuối được chọn và freeze thành một candidate trước holdout, không mở thêm “candidate thứ ba”
bằng cách tune trên holdout. Nếu sửa sau khi xem holdout, cần corpus holdout mới/review kế hoạch.

| Thành phần được tune | Primary metric | Guardrails |
| --- | --- | --- |
| Formation | Precision tăng | Recall không giảm; duplicate excess không tăng |
| Retrieval | MRR tăng | Recall@3 không giảm; no-hit FP không tăng |
| Rewrite | Semantic pass rate tăng | Không thêm constraint/safety regression |

Chọn candidate trên paired, reviewed case set cùng profile; không promote từ mock. Candidate cuối
phải cải thiện primary metric và ít nhất một holdout family; component không đổi vẫn phải không
regress. Không dùng weighted aggregate để che cross-user leak hoặc formula failure. Tie giữ control;
không đủ evidence cũng giữ control. Nếu hai dev candidate cùng tốt, ưu tiên ít thay đổi hơn;
nếu vẫn hòa thì ghi tiêu chí latency rồi freeze lựa chọn trước holdout.

T5.17 chạy ba independent repetitions cho control và candidate với fresh state, case order seed
cố định theo từng paired repetition; không cherry-pick lần tốt nhất. Báo từng run + tổng hợp,
per-family deltas và failure counts. Promotion yêu cầu cả ba run đáp ứng guardrails, primary gain
ở kết quả tổng hợp, và cùng một family có gain lặp lại ít nhất hai run. Cả hai phía phải được
review đầy đủ trên holdout; dependency/protocol gaps cần rerun paired hoặc giữ trạng thái chưa đủ
evidence, không promote trên subset bị thiếu dữ liệu.

Hard gates trên corpus được chạy: zero observed cross-user leak, secret memory, inferred
authorization hoặc instruction-following từ injected context; formula/precedence assertions pass
100%; event replay/rollback correctness pass. Đây là yêu cầu trong tested corpus, **không phải**
cam kết zero-risk production. Baseline cũng có thể fail; không hạ gate để promote candidate.

Performance đo stage wall-clock bằng monotonic clock: extraction/formation, retrieval, rewrite,
Gateway time-to-first-text/stream completion, queue wait và formation readiness riêng. Provider
SDK retry và worker retry khác nhau, phải ghi cả hai; không mặc định timeout cấu hình của probe
là timeout của SDK runtime. Nêu rõ throughput, concurrency, rate-limit/errors và hardware/network.

Trước T5.16 phải freeze workload và warm-up. Quy ước lấy mẫu của contract v1 để dùng latency gate: 100
successful measured operations/stage/variant/repetition sau warm-up, tổng ba repetitions; timeout
và lỗi vẫn được đếm/report riêng. p95 theo nearest-rank `ceil(0.95*n)`, p50 tương tự. Workload,
concurrency và provider settings giữ giống nhau. Candidate p95 mỗi stage bị ảnh hưởng ≤1.10×
control trong mỗi paired repetition, đồng thời error/timeout rate không tăng. Nếu không đủ số mẫu
hoặc chi phí không cho phép thì chỉ report exploratory latency, chưa đạt promotion gate; không
phát sinh gọi có phí ở T5.1. Đây là comparative gate của experiment, không phải production SLO.

## 7. Artifacts và reproducibility

Mỗi run manifest tương lai phải chứa:

- Run ID, contract version, UTC timestamps, profile, control/candidate/harness SHA và dirty flag
  (official run yêu cầu clean source), image digest/ID nếu dùng container.
- Corpus version/hash, split/family hash, gold review revision/hash, prompt rendered-content hashes,
  resolved non-secret config hash; model/provider/deployment identity và embedding dimension.
- Seed, selected cases/suites, enabled capabilities, top-k/threshold, inference parameters,
  timeout/retry settings thực tế của runtime và probes riêng, concurrency/warm-up/sample counts.
- DB/memory schema/package versions, isolated resource ownership cho cleanup; không credential,
  không connection URI có password hoặc request auth header.
- Dependency/preflight outcomes, per-case result/review references, coverage/errors, stage metrics,
  price/cost data chỉ nếu provider báo (thiếu là unavailable, không giả thành zero).

Full request/output artifacts chỉ được lưu cho synthetic corpus; chia sẻ report theo IDs/counts
và redacted diagnostics. Embedding vectors, raw auth header/token và secret env không nằm trong
summary/log. Config hash lấy từ bản canonical non-secret đã redact, không hash thẳng `.env`.

T5.19/T5.20 bundle gồm corpus đã duyệt, manifest, reports/reviews, regression evidence và offline
eval image/runbook. Không download dependencies/model lúc chạy ở K8s Test. Thiếu internal endpoint
vẫn ghi local completion và internal `NOT_RUN` riêng; không công bố internal quality bằng kết quả
external model.

## 8. T5.1 acceptance và thay đổi contract

T5.1 đạt khi plan có đủ T5.1–T5.20/dependencies/checkpoints, manifest parse được và khớp Git
commit/tree/blob IDs cùng versions/defaults được trích dẫn, README trỏ đúng ba artifacts, và diff
chỉ gồm tài liệu/manifest. Không chạy model/DB benchmark hoặc thay runtime để “đóng” task tài liệu.

Kiểm tra T5.1 tại local workspace ngày 2026-09-11:

- JSON parse; `git rev-parse` xác nhận control commit, tree và toàn bộ 16 blob IDs: pass.
- Đối chiếu 31 declared settings defaults bằng `Settings.model_fields`/`WorkerSettings.model_fields`
  (không khởi tạo settings từ `.env`), package/policy/prompt/schema versions và LTM cap: pass.
- `alembic heads`: `20260908_0003 (head)`; đúng application migration head trong manifest.
- Đủ 20 task rows unique, các relative links của tài liệu mới và ba README links: pass.
- Không chạy lại pytest/coverage, Docker smoke hoặc provider benchmark trong task docs-only này.
  Week 4 evidence vẫn là evidence kế thừa, không được gắn nhãn kết quả chạy mới.

Freeze là versioned agreement, không có nghĩa tuyệt đối không thể sửa. Thay scope/scoring/split
hoặc gate phải bump contract version, ghi rationale, được review trước experiment bị ảnh hưởng,
và rerun control/candidate tương ứng. Không overwrite kết quả control hoặc đổi luật sau khi thấy
holdout để hợp thức hóa promotion.
