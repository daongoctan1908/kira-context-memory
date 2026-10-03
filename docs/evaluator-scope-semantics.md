# Evaluator scope semantics — spec cho benchmark formation (T0.5.2)

Evaluator giữ hai scope `CONVERSATION | GLOBAL`; DROP là không persist assertion,
không phải scope thứ ba. GLOBAL là reusable interpretation evidence, không mặc nhiên
là current truth. Formation scorer có contract riêng cho required-subset gold của
canonical dataset; không dùng empty gold để cấm mọi assertion khác trong cùng event.

**Trạng thái**: canonical dataset đã reviewed/frozen và hiện chưa annotate scope.
Các gates scope dưới đây chỉ có hiệu lực khi gold mang scope; unit cases bổ sung
kiểm tra các guarantee đó. Không suy ra scope acceptance từ canonical gold thiếu nhãn.

## 1. Gold contract mà evaluator consume (đích annotation T0.5.1)

Gold model hỗ trợ các annotation sau:

- Persisted gold event (`expected_operation` ∈ `add | update | reinforce_existing`,
  `should_store: true`): field `memory_scope` tùy chọn ∈ `{"CONVERSATION", "GLOBAL"}`.
  Thiếu field này không tạo ra scope target; không mặc định gold là CONVERSATION.
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

Case formation = 1 gold memory event (compiler `_formation_case`): gold facts 0 hoặc
1 fact. Input giữ prefix của conversation nguồn đến boundary event, với
`source_message_ids` chỉ định cặp New Messages. Preceding messages là context;
supporting turn sau boundary không được trở thành source hoặc future context.

### 3.1 Required-subset formation score

`score_formation` match text `canonical_fact` exact-normalized trước, sau đó judge
semantic cho phần dư. Canonical cases khai báo `formation_contract=open_world`;
legacy/custom cases không khai báo vẫn dùng closed-world như trước.

- `MATCH`: một prediction tương đương toàn bộ một required gold; match một-một.
- `VALID_EXTRA`: assertion ngoài gold nhưng được source event hỗ trợ, attribution,
  scope và qualifiers đúng, được phép lưu. Không bù cho required gold bị thiếu.
- `NO_MATCH`: assertion sai, unsupported, sai attribution/applicability, hoặc thuộc
  target `forbidden_facts` của negative case.
- `UNCERTAIN`: thiếu evidence để chốt; case `REVIEW_REQUIRED`, không tự tính là đúng.

Open-world judge nhận New Messages, last-10 preceding context, metadata prediction,
gold attribution/scope và forbidden target. Mọi extra đều phải được judge, kể cả khi
gold đã match hết hoặc gold rỗng. Context chỉ resolve references trong source; không
được tạo assertion mới từ một phát biểu cũ không được source nhắc lại.

`true_positive` và recall chỉ đếm required gold. Precision dùng
`(true_positive + valid_extra) / (true_positive + valid_extra + false_positive)`.
Artifact lưu riêng counts/indexes của valid extras và
`scoring_contract=formation-open-world-v2`; điểm cũ dùng `formation-closed-world-v1`.
Judge prompt/schema hashes phân biệt rubric mới, không trộn với judgments cũ.
Chuỗi 98 → 99 → 98 được xét theo event: không loại 98 cũ chỉ vì không còn current,
nhưng cũng không coi 98 trong preceding context là assertion mới nếu source không nói lại.

Scope layer so scope trên các cặp gold/predicted đã match. Attribution được cấp cho
judge cùng source; không tạo hard gate từ generic `GoldFact.attributed_to=user` vì
canonical M02 cho phép assistant reinforcement của preference user đã phát biểu.

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

Canonical negative gold có `facts=()` và `forbidden_facts=(canonical_fact,)`.
Judge đánh giá target bị cấm cụ thể; assertion khác hợp lệ trong cùng source vẫn có
thể là VALID_EXTRA. Ví dụ source chứa mật khẩu và standing location preference:
preference được lưu không làm negative case fail, mật khẩu được extract vẫn fail.
Scope INVALID không miễn trừ assertion bị cấm; prediction GLOBAL không tự là
false promotion vì negative không có gold scope. Scope counters là diagnostics;
không diễn giải mọi GLOBAL trong negative case là invalid assertion.

Legacy closed-world negative cases giữ behavior cũ: mọi prediction là false positive.
TC-9..TC-12 dưới đây là regression tests cho contract legacy đó. Open-world tests
bổ sung xác minh valid extra, forbidden target và uncertainty riêng biệt.

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
| TC-7 | matched pair gold persisted, scope hợp lệ nhưng không ADD (dedup chặn write) | FAIL `formation_persistence_miss` |
| TC-8 | negative gold, 0 predicted | PASS |
| TC-9 | negative gold, predicted fact text khác canonical_fact | FAIL `formation_quality_mismatch` (false-ADD) |
| TC-10 | negative gold, predicted fact text trùng canonical_fact | FAIL false-ADD; KHÔNG tính promotion |
| TC-11 | negative gold, predicted GLOBAL | FAIL false-ADD + `negative_global_prediction_count` +1, không promotion |
| TC-12 | negative gold, predicted raw scope invalid | FAIL false-ADD (scope invalid không miễn trừ) + `scope_invalid_count` +1 |
| TC-13 | gold persisted match text, có ADD, persistent payload scope GLOBAL, gold CONVERSATION | FAIL `scope_false_global_promotion` (đường persistent đọc payload) |
| TC-14 | judge-matched pair (không exact), gold CONVERSATION, predicted GLOBAL | FAIL promotion (judge match text vẫn so scope) |

## 7. Ngoài phạm vi spec này

- Chain scope stability → dataset validator (T0.5.1), evaluator không re-check.
- Case authoring 14 loại retrieval/QA → T5.1, sau freeze.
- RRF fallback merge → chỉ kích hoạt nếu benchmark chứng minh raw-score merge sai
  lệch thực tế (quyết định đã chốt 2026-09-23).
- Việc migrate evaluation callers khỏi port `search` cũ → follow-up, không thuộc v1.
