# Evaluator scope semantics — spec cho benchmark formation (T0.5.2)

Spec này định nghĩa semantics mà evaluator Phase 5 (T5.2) phải implement cho
feature memory scope (CONVERSATION | GLOBAL | DROP). Viết trước dataset freeze
vì chỉ phụ thuộc semantics đã chốt trong plan (vòng 3) và code hiện tại — không
phụ thuộc nội dung dataset, không tune prompt. T5.2 consume spec này nguyên văn;
không định nghĩa lại khi implement.

Anchors code ghi tại thời điểm viết (commit sau `db451ba`); T5.2 đối chiếu lại
trước khi hiện thực.

## 1. Gold contract mà evaluator consume (đích annotation T0.5.1)

Sau khi T0.5.1 annotate xong, `memories.json` của mỗi bundle có:

- Persisted gold event (`expected_operation` ∈ `add | update | reinforce_existing`,
  `should_store: true`): bắt buộc có field `memory_scope` ∈ `{"CONVERSATION", "GLOBAL"}`.
- Negative gold event (`should_store: false`, `expected_operation: "do_not_persist"`):
  KHÔNG được có `memory_scope` — DROP proxy, không có gold scope để so sánh.
- Chuỗi update/reinforce (liên kết qua `supersedes_memory_id` /
  `reinforces_memory_ids`): cùng một `memory_scope` xuyên suốt chain — v1 không có
  scope promotion. Rule này do dataset validator kiểm tra (T0.5.1), evaluator
  KHÔNG re-check chain.

## 2. Nguồn predicted scope (2 đường evaluator)

**Write-free** (`WriteFreeFormationEvaluator`): `ExtractedFact` được parse từ raw
output của stage `mem0.extract.parse` (evaluation/formation.py `_facts`,
`_completed_extraction`) — tức TRƯỚC enforcement của fork. T5.2 thêm field
`scope: str | None` (raw, KHÔNG pattern-validate — giá trị invalid phải còn
nguyên để chấm, không được làm vỡ model). Phân loại mirror đúng
`_enforce_memory_scopes` (packages/viettel-mem0/mem0/memory/main.py):

- raw scope `None` hoặc blank sau strip → `MISSING` (fallback CONVERSATION);
- strip + uppercase ∈ {CONVERSATION, GLOBAL} → scope hợp lệ (so sánh case-insensitive);
- khác → `INVALID` (candidate bị drop trước hash/embed/write).

Trạng thái persist của một fact trong đường write-free: fact có mặt trong
`lifecycle_events` (chỉ chứa ADD) → đã persist; có trong `facts` nhưng không có
ADD tương ứng → KHÔNG được persist (drop vì scope invalid, hoặc dedup).

**Persistent** (`PersistentFormationEvaluator` + `PostgresFormationInspector`):
scope đọc từ payload pgvector đã normalize — enforcement đảm bảo chỉ có giá trị
hợp lệ được ghi. T5.2 thêm field `memory_scope: Literal["CONVERSATION", "GLOBAL"]`
vào `PersistedFormationMemory` (inspector `_parse_memory` đọc thêm
`payload->>'memory_scope'`). Payload thuộc đường này KHÔNG bao giờ missing/invalid
nên dùng Literal chặt.

## 3. Semantics scoring per formation case

Case formation = 1 gold memory row (compiler `_formation_case`): gold facts 0 hoặc
1 fact, evidence window chỉ gồm source turns + supporting turns của memory đó.

### 3.1 Text score — giữ nguyên

`score_formation` (evaluation/scoring.py) match text `canonical_fact` bằng
exact-normalized trước, judge semantic cho phần dư. Không đổi gì. Scope layer
NÂNG phía trên, chỉ so scope trên các cặp (gold, predicted) đã MATCH (cả
`exact_normalized` lẫn `internal_judge` — judge chỉ match text, scope vẫn so được).

### 3.2 Scope gates (mới, T5.2)

Trên từng cặp matched:

1. **`scope_false_global_promotion` — hard gate = 0 toàn benchmark.** Gold
   `memory_scope == "CONVERSATION"` mà predicted GLOBAL. Đây là vi phạm mở rộng
   phạm vi exposure sai (risk rò rỉ conversation-local sang conversation khác).
   Case FAIL, reason code thêm vào danh sách hiện có.
2. **`missed_global` — metric, KHÔNG gate.** Gold `"GLOBAL"` mà predicted
   CONVERSATION (bao gồm fallback từ MISSING). Làm hẹp phạm vi — không leak, chỉ
   làm giảm Recall@k cross-conversation; đếm để báo cáo, không fail case.
3. **Persistence alignment — hard gate.** Với gold `should_store: true`: mọi gold
   fact được match text BẮT BUỘC có ADD tương ứng trong `lifecycle_events`.
   Match text nhưng không có ADD → reason `formation_persistence_miss`, FAIL.
   Gate này đóng lỗ hổng hiện tại: candidate bị drop do scope invalid vẫn match
   text và case vẫn PASS dù memory không hề được lưu.
4. **`scope_invalid` — metric + hệ quả qua gate 3.** Predicted raw scope INVALID
   (chỉ xảy ra đường write-free): đếm metric; candidate không được persist nên
   với gold persisted nó rơi vào `formation_persistence_miss` (gate 3); với gold
   negative nó là hành vi ĐÚNG (DROP đúng).

### 3.3 Negative case semantics (DROP)

Negative gold: `facts = ()`, `semantic_expectation` = "Do not persist the
unsupported candidate …" (compiler `_formation_case`).

- **False-ADD hard gate (đã có sẵn, giữ nguyên)**: gold rỗng → mọi predicted fact
  là false positive → `score_formation` trả `false_positive > 0` → executor FAIL
  với `formation_quality_mismatch` (evaluation/native_executor.py). Tức là chuẩn
  hiện tại đã khắt khe hơn assert tối thiểu: KHÔNG ĐƯỢC extract BẤT KỲ fact nào
  từ evidence window của turn negative, không chỉ fact trùng canonical_fact.
- **Assert lifecycle tối thiểu (canonical của plan)**: không lifecycle event nào
  có `memory` matching `canonical_fact` của negative gold (so bằng
  `normalize_exact`). Được bao bởi false-ADD gate ở trên — spec giữ cả hai, gate
  mạnh là nguồn FAIL.
- **Negative + predicted GLOBAL**: KHÔNG tính `scope_false_global_promotion` —
  negative không có gold scope để so. Violation duy nhất là False-ADD (gate trên).
  Ghi thêm metric chẩn đoán `negative_global_prediction_count` (LLM vừa ADD sai
  vừa định raise GLOBAL — tín hiệu prompt lệch nặng).
- **Negative + predicted INVALID scope**: DROP đúng — không FAIL, không metric
  nào ngoài `scope_invalid_count`.

### 3.4 Fallback MISSING

Predicted scope MISSING (LLM bỏ qua field) → mirror enforcement: ứng xử như
CONVERSATION. Không bao giờ là violation tự thân; đếm `scope_fallback_count`.
Với gold GLOBAL, MISSING rơi vào `missed_global` (metric).

## 4. Reason codes & metrics tổng hợp

| Tên | Loại | Định nghĩa | Gate |
|---|---|---|---|
| `scope_false_global_promotion` | reason code | matched pair gold CONVERSATION → predicted GLOBAL | hard = 0 toàn run |
| `formation_persistence_miss` | reason code | gold persisted match text nhưng không có ADD lifecycle | hard = 0 toàn run |
| `missed_global_count` | metric | matched pair gold GLOBAL → predicted CONVERSATION/MISSING | báo cáo |
| `scope_fallback_count` | metric | predicted raw scope missing | báo cáo |
| `scope_invalid_count` | metric | predicted raw scope invalid enum | báo cáo |
| `negative_global_prediction_count` | metric | negative turn có predicted GLOBAL | báo cáo |

Acceptance Phase 5 (plan Verification.5) ánh xạ: "False-GLOBAL-promotion = 0" =
không case nào FAIL với `scope_false_global_promotion`; "negative-ADD = 0" =
không negative case nào FAIL với `formation_quality_mismatch`; leakage = 0 ở
retrieval (mục 5).

## 5. Retrieval-side assertions (tóm tắt — chi tiết ở T5.1/T5.2)

Retrieval suite dùng lại `score_retrieval_groups` (Recall@3, MRR@10) trên output
của `search_scoped` 2 branch. Assert bổ sung theo scope:

- **Global recall xuyên conversation**: QA ở conversation B về gold GLOBAL hình
  thành ở conversation A → gold id phải xuất hiện trong top-3 của merged result;
  metadata row trả về phải `memory_scope == "GLOBAL"`.
- **Local non-leak**: QA conversation-local chỉ match gold CONVERSATION; không
  gold GLOBAL nào trở thành relevant.
- **Cross-user isolation**: query user-b phải 0 hit memory của user-a (cả 2
  branch) — hard fail nếu có.
- **Deletion removes global**: sau khi xóa conversation nguồn, memory GLOBAL gắn
  conversation đó không còn xuất hiện ở global branch.

Case authoring (14 case types theo plan T5.1) làm SAU freeze, consume gold đã
annotate — phần này chỉ khóa semantics.

## 6. Danh sách test case T5.2 phải hiện thực (unit, fabrication trực tiếp)

Mỗi dòng = 1 unit test trên evaluator layer (không cần provider):

| # | Setup | Expected |
|---|---|---|
| TC-1 | matched pair gold CONVERSATION, predicted CONVERSATION | PASS, không violation |
| TC-2 | matched pair gold GLOBAL, predicted GLOBAL | PASS |
| TC-3 | matched pair gold CONVERSATION, predicted GLOBAL | FAIL `scope_false_global_promotion` |
| TC-4 | matched pair gold GLOBAL, predicted CONVERSATION | PASS case, `missed_global_count` +1 |
| TC-5 | matched pair gold GLOBAL, predicted raw scope missing | như TC-4 (fallback) + `scope_fallback_count` +1 |
| TC-6 | matched pair gold persisted, predicted raw scope invalid, không ADD | FAIL `formation_persistence_miss` + `scope_invalid_count` +1 |
| TC-7 | matched pair gold persisted, có ADD, raw scope invalid (dịu: dedup chặn write) | FAIL `formation_persistence_miss` |
| TC-8 | negative gold, 0 predicted | PASS |
| TC-9 | negative gold, predicted fact text khác canonical_fact | FAIL `formation_quality_mismatch` (false-ADD) |
| TC-10 | negative gold, predicted fact text trùng canonical_fact | FAIL false-ADD; KHÔNG tính promotion |
| TC-11 | negative gold, predicted GLOBAL | FAIL false-ADD + `negative_global_prediction_count` +1, không promotion |
| TC-12 | negative gold, predicted raw scope invalid | PASS (DROP đúng) + `scope_invalid_count` +1 |
| TC-13 | gold persisted match text, có ADD, persistent payload scope GLOBAL, gold CONVERSATION | FAIL `scope_false_global_promotion` (đường persistent đọc payload) |
| TC-14 | judge-matched pair (không exact), gold CONVERSATION, predicted GLOBAL | FAIL promotion (judge match text vẫn so scope) |

## 7. Ngoài phạm vi spec này

- Chain scope stability → dataset validator (T0.5.1), evaluator không re-check.
- Case authoring 14 loại retrieval/QA → T5.1, sau freeze.
- RRF fallback merge → chỉ kích hoạt nếu benchmark chứng minh raw-score merge sai
  lệch thực tế (quyết định đã chốt 2026-09-23).
- Việc migrate evaluation callers khỏi port `search` cũ → follow-up, không thuộc v1.
