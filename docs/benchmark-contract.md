# Benchmark contract v4

Status: **full-corpus acceptance contract**, 2026-09-18. Contract ID:
`kira-week5-benchmark-v4`. V4 giữ quyết định bỏ dev/holdout split, chuyển performance sang
guardrail chạy sau semantic confirmation, giới hạn workload đo bằng attempt cap, và bắt buộc
phân biệt historical control với release candidate bằng runtime provenance. V4 thu gọn scorecard,
thêm internal LLM judge có audit chọn mẫu và final-QA semantic/task-success. Corpus hiện tại là một
acceptance dataset chạy toàn bộ, không phải bằng chứng generalization trên unseen holdout.
Control source không đổi.
Đây là đặc tả cho implementation tiếp theo, chưa phải harness hoặc benchmark result.

Liên quan: [control manifest](benchmark-baseline.json),
[PC/VDI handoff](company-pc-ai-handoff.md),
[internal K8s acceptance](benchmark-k8s-acceptance.md).

## 1. Control và phạm vi so sánh

Control bất biến là source tại `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00`, không phải HEAD
của nhánh benchmark hiện tại và không phải một image tag mutable. Đây là **historical control**; việc package
hiện tại đã lên `.4` không được dùng để sửa manifest thành như thể control cũ từng chạy `.4`.
Manifest ghi cả Git tree và blob IDs. Các run sau phải ghi riêng runtime SHA và harness SHA;
image dùng digest/image ID thực tế. `kira-context:0.4.1` chỉ là source release reference, không
chứng minh cùng binary.

Baseline giữ nguyên app `0.4.1`, package `2.0.20+viettel.3`, memory schema `2`, native Mem0 V3,
policy `kira-memory-policy-v2` và rewrite prompt `2`. Trường cấu hình Mem0 `version="v1.1"`
không phải version của formation engine. Không ép lifecycle thành ADD-only; không thay engine.

Configuration trong manifest là **source defaults**, không phải env của một deployment đã kiểm
chứng. Source tắt `ltm_enabled` và `memory_formation_enabled`; suite cần các capability này phải
bật rõ trong eval wiring cho cả control/candidate. Ghi resolved config, không so bản tắt feature
với bản bật feature rồi gọi đó là hiệu quả tuning. Không dùng test Compose overrides như default
production. Baseline không pin một external provider/model chưa được chọn.

Candidate được phép thay extraction/rewrite prompt hoặc search/config knobs đã có. Mỗi candidate
phải khai báo change scope thuộc `prompt`, `config`, `runtime_code`, `dependencies`, `schema` hoặc
`lifecycle`, kèm mô tả. Candidate có bất kỳ scope nào ngoài prompt/config là một
`mixed_runtime_candidate`; report không được quy toàn bộ delta chất lượng cho prompt.
Giữ nguyên
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

K8s Test không Internet/GPU. User đã chỉ định binding rewrite production: Qwen3-14B base qua vLLM,
`http://10.254.135.40:8080/v1`, model ID `/models/Qwen3_14B`, không dùng `genai-lora`.
Binding này là cấu hình được cung cấp, chưa chứng minh internal preflight hoặc live readiness.
Extraction, embedding và judge nội bộ vẫn cần binding/capability thực tế. Không suy đoán model
revision, JSON support hay embedding dimension. Preflight kiểm tra khả năng thực tế, bao gồm
embedding batch/count/dimension và JSON extraction format.

Không cấu hình hoặc chưa lên lịch chạy: `NOT_RUN`. Đã thử nhưng dependency unavailable/timeout:
`DEPENDENCY_ERROR`. Response không tuân contract: `PROTOCOL_ERROR`. Không auto-fallback provider
khác trong cùng experiment hoặc biến provider error thành một case “không cần nhớ” đã pass.

## 3. Corpus, review và phạm vi chạy

Dataset nguồn hiện có 209 QA trên bốn storyline. T5.3/T5.4 vẫn phải ghi số case thực sự chuyển
được sang từng suite và lý do loại case; thay đổi phải được review trước chạy chính thức.

Mỗi case cần stable ID, suite, scenario-family ID, `evaluation_scope=full_corpus`, tags,
synthetic provenance, inputs,
gold IDs/constraints, evidence references, allowed attribution và review status. Formation gold
là atomic reusable claims; retrieval gold xác định relevant memory IDs; rewrite gold là các
slot/constraints cần giữ hoặc không được invent, không chỉ một câu exact-match.

- Mọi official run chạy toàn bộ case đủ điều kiện trong cả bốn bundle; không có dev/holdout split.
- `scenario_group` và memory family chỉ dùng để báo cáo/audit, không quyết định case có được chạy.
- Validator kiểm tra normalized exact duplicates, IDs, references và full-corpus scope; reviewer kiểm tra
  semantic overlap. Không quảng cáo exact normalization là semantic leakage detector.
- Gold có `draft`/`reviewed`, reviewer decision và revision/hash; user/mentor duyệt nội dung.
  Không tự nhận “reviewed” chỉ vì schema pass hoặc LLM vừa sinh corpus.
- Freeze gold + evaluation scope + candidate trước T5.17. Sửa gold sau khi thấy kết quả cần
  revision mới và rerun cả control/candidate; không cherry-pick case hoặc sửa label để candidate
  thắng. Vì toàn corpus có thể được xem trong lúc phát triển, report phải ghi rõ đây là
  acceptance/regression evidence, không phải unseen-holdout generalization evidence.

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
| `INSUFFICIENT_EVIDENCE` | Đã chạy nhưng không đủ sample/coverage đã khóa để kết luận |
| `DEPENDENCY_ERROR` | Provider/DB/transport không thực thi được case |
| `PROTOCOL_ERROR` | Payload/parse/result contract sai, kể cả lỗi bị native parser che |
| `NOT_RUN` | Chưa thực thi hoặc thiếu cấu hình đã biết trước |

Auto checks xử lý cấu trúc, exact-normalized match và ràng buộc deterministic. Chỉ internal
LLM-as-a-judge xử lý semantic equivalence; canonical dataset không được fallback sang external
provider. Judge chạy temperature 0, bị blind với variant và ghi model/deployment cùng hash của
prompt/schema. Human audit lấy toàn bộ `UNCERTAIN`, toàn bộ xung đột với deterministic checks và
một ngân sách toàn cục `ceil(10% × tổng semantic PASS/FAIL)`, stratified theo
variant/suite/verdict/bundle. Judgment/audit record phải gắn output hash và case ID; output đổi thì
record cũ hết hiệu lực. Một case có nhiều semantic output phải có `subject` riêng (ví dụ
`rewrite`, `final_no_ltm`, `final_with_ltm`); reviewer decision phải copy đúng cả `case_id`,
`subject` và `output_sha256` từ audit batch.
Langfuse/OTel chỉ trace/debug, không cung cấp verdict hoặc denominator.

Report luôn có total eligible, attempted, từng outcome count và số case thực sự được chấm.
Quality score có denominator chỉ rõ; report thêm coverage = scored/eligible. Provider/protocol
errors không trở thành zero-fact true negative, không âm thầm bị loại để score đẹp hơn. Score
trên corpus draft chỉ là provisional. Zero denominator hiển thị `N/A`, không tự cho 100%.

### Formation

Tách output thành atomic claims và match với gold IDs: normalized exact trước, semantic leftovers
qua internal judge. Một gold claim chỉ được một TP; unsupported hoặc lặp claims là FP, gold thiếu
là FN. Một output chứa hai gold claims
có thể credit hai claim, nhưng chỉ sau khi phân rã và kiểm tra evidence, không theo số string trả về.

- Headline chỉ gồm Precision = TP/(TP+FP), Recall = TP/(TP+FN), và F1. Per-family chỉ là failure
  drilldown, không phải promotion metric riêng.
- Negative case không có claim được xử lý trong cùng confusion counts; zero denominator là `N/A`.
- Formula preservation chấm expression có evidence; chỉ normalize whitespace đã được gold cho
  phép. Không normalize operators, variable names, units hoặc thresholds thành nghĩa khác.
- Attribution, unsupported fact và secret/authorization failures có case-level evidence riêng.

### Retrieval

Positive query với tập gold relevant R chỉ báo Recall@3 = số relevant IDs duy nhất trong top 3 / |R|
và MRR@10 = reciprocal rank của relevant hit đầu tiên trong top 10. Duplicate IDs không thêm credit.
No-hit query không vào hai denominator này. Cross-user hit vẫn là hard safety failure kể cả score
thấp hoặc không được ContextBuilder sử dụng. Gold-vs-formed reports tách nhau.

### Rewrite và cross-session

Rewrite báo deterministic constraint pass/fail và internal-judge semantic pass, không gộp thành
một score. Semantic pass cần giữ intent và đúng required slots/constraints, không invent KPI/date/location,
không trả lời nghiệp vụ. Exact string match chỉ là diagnostic. Query standalone/topic-switch không
bị ép dùng history; không đủ evidence thì giữ ambiguity. Explicit current-query information là
hard precedence. Control rewrite v2 dùng Recent > LTM; candidate từ v3 (hiện v7) xét chronology và applicability
của user evidence trong cả hai nguồn, không tự ưu tiên recent context hoặc một scope. Một GLOBAL
declaration mới hơn có thể thay thế convention cũ trong recent messages nếu cùng ngữ cảnh áp dụng;
conflict có timestamp không rõ/bằng nhau giữ ambiguity. Gold/review phải chốt đúng prompt contract
của candidate, không áp precedence của control lên candidate một cách ngầm định.

Cross-session/final QA báo internal-judge semantic pass và deterministic task success riêng;
task success là `N/A` khi không có structured action/API evidence. Bounded wait hết hạn không được
đổi thành semantic miss. Safety regression luôn là hard fail, không nằm trong score tổng hợp.

## 6. Candidate selection và late performance guardrail

T5.3 chạy control trước, thử tối đa hai declared candidate trên cùng full corpus; diagnostic grid
chỉ hỗ trợ discovery. Tổ hợp cuối được chọn bằng metric/guardrail đã khóa trước, sau đó freeze thành
một candidate cho ba paired repetitions T5.5. Không mở thêm candidate bằng cách cherry-pick case,
đổi gold hoặc đổi luật sau khi xem kết quả.

| Thành phần được tune | Primary metric | Guardrails |
| --- | --- | --- |
| Formation | F1 tăng | Precision và Recall không giảm |
| Retrieval | Recall@3 tăng | MRR@10 không giảm |
| Rewrite | Semantic judge pass rate tăng | Constraint pass rate không giảm; không thêm safety regression |
| Final QA | Semantic judge pass rate tăng | Task success không giảm khi observable; không thêm safety regression |

Chọn candidate trên paired, reviewed case set cùng profile; không promote từ mock. Candidate cuối
phải cải thiện primary aggregate và ít nhất 2/3 paired repetitions cùng hướng; component không đổi
vẫn phải không regress. Không dùng weighted aggregate để che cross-user leak hoặc formula failure.
Tie giữ control; không đủ evidence cũng giữ control. Nếu hai candidate cùng tốt, ưu tiên ít thay đổi
hơn; nếu vẫn hòa thì giữ control. Performance chạy sau semantic confirmation và không được dùng để
cứu một candidate không có semantic gain.

Semantic confirmation chạy ba independent repetitions cho control và candidate với fresh state,
case-order seed cố định theo từng paired repetition; không cherry-pick lần tốt nhất. Báo từng run +
tổng hợp và failure counts. Promotion yêu cầu primary aggregate tăng, ít nhất 2/3 repetitions cùng
hướng, guardrail không giảm, With-LTM tốt hơn No-LTM, task success không giảm khi observable và zero
safety hard fail. Cả hai phía phải đủ evidence; dependency/protocol/audit gaps là
`INSUFFICIENT_EVIDENCE`, không promote trên subset bị thiếu dữ liệu. Human chỉ audit theo policy,
không chấm tay toàn bộ output.

Hard gates trên corpus được chạy: zero observed cross-user leak, secret memory, inferred
authorization hoặc instruction-following từ injected context; formula/precedence assertions pass
100%; event replay/rollback correctness pass. Đây là yêu cầu trong tested corpus, **không phải**
cam kết zero-risk production. Baseline cũng có thể fail; không hạ gate để promote candidate.

Timing instrumentation được xây và kiểm thử trước, nhưng workload performance thật **chỉ chạy sau**
khi candidate đã vượt semantic confirmation. Chỉ đo các stage bị candidate tác động. Stage
wall-clock dùng monotonic clock: extraction/formation, retrieval, rewrite, Gateway
time-to-first-text/stream completion, queue wait và formation readiness riêng. Provider SDK retry
và Worker retry khác nhau, phải ghi cả hai; không mặc định timeout probe là timeout SDK runtime.

Performance workload v4:

- freeze workload sequence, concurrency, hardware/network, provider, timeout và retry policy;
- chạy 5 warm-up operations mỗi variant; lỗi warm-up vẫn được báo;
- mục tiêu 30 successful measured operations mỗi variant;
- tối đa 40 measured attempts mỗi variant, rồi dừng thay vì gọi vô hạn để đủ success;
- percentile lấy từ 30 success đầu tiên; p95 dùng nearest-rank `ceil(0.95*n)`, p50 tương tự;
- report toàn bộ measured attempts, `successful/attempted`, error, timeout và SDK retry;
- thiếu 30 success trong attempt cap là `INSUFFICIENT_EVIDENCE`, không phải semantic fail;
- không khóa ngưỡng `1.10x` khi chưa có baseline vận hành đủ tin cậy;
- reviewer ghi đúng một verdict: `acceptable`, `reject_regression` hoặc `needs_more_samples`;
  promotion cần `acceptable`.

Nếu error/timeout regression được xác nhận do candidate thì verdict là `reject_regression`.
Môi trường nhiễu hoặc thiếu mẫu dùng `needs_more_samples` và chạy lại cặp control/candidate;
không kết luận từ subset thuận lợi. Đây là comparative guardrail, không phải production SLO.

## 7. Artifacts và reproducibility

Mỗi run manifest tương lai phải chứa:

- Run ID, contract version, UTC timestamps và profile.
- Runtime Git SHA/dirty flag và harness Git SHA/dirty flag là hai trường độc lập; official run
  yêu cầu cả hai source sạch. Ghi image digest/ID nếu dùng container.
- Variant `historical_control`, `release_candidate` hoặc `working_tree`. Historical control phải
  dùng đúng SHA `75deb1d...`; release candidate phải có candidate ID, control SHA, declared change
  scopes và mô tả.
- Corpus version/hash, evaluation-scope/family hash, gold review revision/hash, prompt rendered-content hashes,
  resolved non-secret config hash; model/provider/deployment identity và embedding dimension.
- Package versions thực thi, tối thiểu application và `viettel-mem0`; không suy package version
  hiện tại từ version lịch sử hoặc ngược lại.
- Seed, selected cases/suites, enabled capabilities, top-k/threshold, inference parameters,
  timeout/retry settings thực tế của runtime và probes riêng, concurrency/warm-up/sample counts.
- DB/memory schema/package versions, isolated resource ownership cho cleanup; không credential,
  không connection URI có password hoặc request auth header.
- Dependency/preflight outcomes, per-case result/review references, coverage/errors, stage metrics,
  price/cost data chỉ nếu provider báo (thiếu là unavailable, không giả thành zero).

CLI preflight mặc định ghi variant `working_tree` từ checkout đang chạy. Khi harness probe một
runtime historical/candidate được build riêng, phải truyền `--provenance-file`; schema từ chối
historical control sai SHA/package version, release candidate thiếu declaration, field thừa hoặc
hash sai định dạng.

Full request/output artifacts chỉ được lưu cho synthetic corpus; chia sẻ report theo IDs/counts
và redacted diagnostics. Embedding vectors, raw auth header/token và secret env không nằm trong
summary/log. Config hash lấy từ bản canonical non-secret đã redact, không hash thẳng `.env`.

T5.19/T5.20 bundle gồm corpus đã duyệt, manifest, reports/reviews, regression evidence và offline
eval image/runbook. Không download dependencies/model lúc chạy ở K8s Test. Thiếu internal endpoint
vẫn ghi local completion và internal `NOT_RUN` riêng; không công bố internal quality bằng kết quả
external model.

## 8. Baseline acceptance và thay đổi contract

V4 chỉ thay evaluation contract/harness metadata; không sửa historical control source, prompt,
package hoặc schema. `docs/benchmark-baseline.json` tiếp tục khóa app `0.4.1`, Mem0 `.3`, policy v2 và
SHA `75deb1d...`, đồng thời trỏ sang contract v4 để run mới tuân scorecard/judge/audit rules mới.

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
  Evidence trước baseline vẫn là evidence kế thừa, không được gắn nhãn kết quả chạy mới.

Freeze là versioned agreement, không có nghĩa tuyệt đối không thể sửa. Thay scope/scoring
hoặc gate phải bump contract version, ghi rationale, được review trước experiment bị ảnh hưởng,
và rerun control/candidate tương ứng. Không overwrite kết quả control hoặc đổi luật sau khi thấy
kết quả để hợp thức hóa promotion. Nếu sau này cần đo generalization, phải thu một corpus độc lập;
không tái gắn nhãn một phần corpus hiện tại thành holdout sau khi nó đã được xem.
