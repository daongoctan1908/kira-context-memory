# OpenAI-only prompt tuning — 2026-10-03

Đã test và chỉnh custom formation policy/rewrite prompt trong workspace dựa trên output thật của
`gpt-4o-mini`. **Chưa đạt semantic gate để rollout.** Đây là kết quả kiểm tra trước KiRa, không
phải kiểm chứng câu trả lời nghiệp vụ cuối cùng hoặc model Qwen.

## Phạm vi và tính tái hiện

- Không gọi KiRa, Qwen, live search, Gateway/Worker hoặc database trong vòng tuning này.
- Model/endpoint lấy từ `.env.openai.local`: `gpt-4o-mini` trên OpenAI. Không hard-code key.
- Dataset canonical được user cho phép gửi OpenAI; manifest canonical không bị thay đổi.
  Authorization và digest được đóng băng trong `artifacts/prompt-tuning-authorization.json`.
- Formation gọi native ADDITIVE prompt builder với New Messages là source pair và Last k là
  preceding context. Parse và scope enforcement dùng code native. Không embedding/persist/search;
  không thể dùng phép thử này để tuyên bố receipts/deletion/search đã chạy end-to-end.
- Rewrite gọi production adapter, temperature 0, max output 256 tokens. Input envelope, raw
  output, finish reason, usage, latency và prompt digest được lưu từng call. Không dùng LLM judge.
- 140 canonical rewrite inputs dùng **frozen causal native snapshots của formation policy v7**.
  Không ingest gold, không dùng future facts. Đây là kiểm tra interpretation khi đã có evidence,
  không phải đo recall của formation v10 hoặc search hiện tại.
- Recent window vẫn 10 messages/3,000 estimated tokens. Formation vẫn tối đa 10 messages gồm
  source pair, không giới hạn 300 ký tự. Không so sánh các window trong round này.

Tổng cộng có 865 output thật hoàn thành qua các vòng tuning: 731 rewrite và 134 formation,
bao gồm 4 call characterization riêng. Không coi số call thành công là số case đúng semantic.
Mỗi phiên bản prompt đều có snapshot/digest riêng; bản source cuối khớp digest của final run.

| Final candidate | Phép thử | Output hoàn thành |
|---|---|---:|
| Formation policy v10 | 18 source events canonical được chọn để phá source/fidelity/scope | 18 |
| Formation policy v10 | 34 synthetic policy cases + 4 assertion/change/reassertion/cancel cases | 38 |
| Rewrite v7 | Toàn bộ 140 canonical rewrite inputs + 35 synthetic inputs | 175 |
| Rewrite v7 | 20 canonical + 17 synthetic cases, lặp mỗi case 3 lần | 111 |

Final runs đều finish `stop`; không có rewrite execution error. Một diagnostic logger trước
khi sửa encoding stdout bị dừng sau khi đã checkpoint output tiếng Việt; resume bỏ qua các
case đã hoàn thành. Không ghi lại source event hoặc thay đổi persisted memories vì lỗi logger.

## Thay đổi được giữ trong source

`app/application/services/memory_policy.py`, policy **kira-memory-policy-v10**:

- Phân biệt source assertion/adoption với preceding context; chỉ rõ New assistant không thể
  đã được New user chấp nhận vì assistant trả lời sau user.
- Làm rõ override với native extraction checklist và semantic dedup guidance: một GLOBAL
  assertion mới là event mới kể cả khi nội dung giống existing memory.
- Named/conditional conventions, partial list amendments và cancellation được xét GLOBAL;
  task-only context vẫn CONVERSATION. Không yêu cầu keyword “ghi nhớ” hay “từ nay”.
- Giữ tên chỉ tiêu, công thức, qualifiers/negation và tách các quy ước độc lập; ví dụ output
  dùng JSON/scopes đúng native contract.

`app/application/services/rewrite_prompt.py`, rewrite **v7**:

- Dùng chỉ thị tiếng Việt, lấy current_query làm gốc; giữ metric/place/period/ranking rõ ràng.
- Bổ sung omitted fields từ substantive antecedent, không kéo metric cũ sang metric mới.
- Match trọn reference; task exceptions và same-context chronology được xét trước expansion.
- Không coi GLOBAL/scope/relevance là current truth; giữ unknown/tied conflicts chưa resolve.
- Truyền actual definition/criteria/formula/units vào query vì KiRa không nhận memory envelope.
- Không bỏ qualifiers hoặc thay một ordinal thiếu định nghĩa bằng item khác.

Thử nghiệm prompt dài 9,912 ký tự không được chọn: có thêm lỗi thay current_query bằng câu cũ
và thay explicit ranking bằng mặc định. Final rewrite có 5,822 ký tự nhưng token count không
giảm so với v4: trên cùng 175 inputs, tổng input tokens v4 là 330,200, v7 là 387,775 (+17.4%).
Tiếng Việt ít ký tự hơn không đồng nghĩa ít tokens hơn. Không tuyên bố cải thiện latency/SLO
hoặc tối ưu token; bản hiện tại là candidate để tiếp tục validation, chưa được promote.

## Bằng chứng cải thiện và giới hạn

Các ví dụ dưới đây là raw-output inspection, không phải aggregate accuracy hoặc held-out score.
Những case dùng để tune không còn là bộ kiểm tra độc lập.

| Case | Kết quả trước / final | Mức kết luận |
|---|---|---|
| Formation `conv04:D4:3` | v8 re-extract alias từ Last k; v9/v10 chỉ emit alias source “Dòng vào FTTH” | Cải thiện source separation trên input này |
| Formation `conv04:D5:1`, `D6:1` | v9 emit `[]`; v10 giữ top tốt cao nhất cho dòng vào / thấp nhất cho dòng ra | Cải thiện conditional convention, một lần mỗi case |
| Formation `conv03:D8:1` | v8 omission, v9 CONVERSATION; v10 giữ partial third-item amendment GLOBAL | Không cần invent hai mục đầu |
| Formation `conv03:D19:1` | v9 omission; v10 giữ cancellation/no-default + hỏi lại | Cải thiện cancellation, một lần |
| Rewrite `conv02:CONV02_FILL_018` | v4 đổi tháng 08 thành 10; v7 giữ đúng tháng 08/2026 ở 3 focused repetitions | Giữ explicit period trên input này |
| Rewrite `conv04:CONV04_FILL_015` | v4 thay Register đối chiếu bằng metric chính; v7 giữ Thuê bao Register 5G lũy kế đối chiếu | Kiểm tra đủ ba repetitions trong raw report |
| Rewrite `conv03:CONV03_FILL_026` | v4/v5 thay third item bằng IOT; v7 giữ third unresolved ở cả 3 repetitions | Safe fallback khi thiếu definition |
| Rewrite `reassert_98` | v4 để alias chưa expand; v7 chọn ngưỡng 98 sau 98 → 99 → 98 | Consumer test với controlled retrieved evidence, không chứng minh extractor giữ event |
| Rewrite `extra_explicit_rank_override` | v5 thay 7 cao nhất thành 5 thấp nhất; v7 giữ 7 cao nhất | Kiểm tra actual criteria, không chỉ tên alias |

**Các lỗi còn lại làm gate fail:**

- Formation `event_reassert_same_text`: nguồn mới quay lại 97 sau 97 → 98 → 97, nhưng v9/v10
  raw response là `{"memory": []}`. Lỗi xảy ra trước hash/text dedup/persistence. Receipts và
  việc giữ distinct memory IDs không khôi phục assertion không được emit.
- Formation `conv04:D1:7`: chỉ hỏi KPI nhưng v10 re-extract definition từ Last k;
  `conv02:D1:7` biến ordinary query thành GLOBAL. Không thể coi việc loại bỏ numeric answer
  là source/eligibility đã đúng hoàn toàn.
- Formation synthetic prompt injection vẫn được ghi thành doanh thu giả; one-off breakdown
  được ghi GLOBAL; dated incident thiếu ngày và bị widen GLOBAL. Đây là lỗi thật ngoài những
  literal-scoring disagreements của fixture.
- Rewrite `heldout_unknown_conflict`: v7 chọn `< 31 TB` từ dated memory dù còn conflicting
  undated `< 27 TB`; lặp 3 lần vẫn fail. Câu hỏi định nghĩa `unknown_conflict` giữ ambiguity
  đúng không có nghĩa mọi dạng ambiguous query đã đúng.
- Rewrite `conv04:CONV04_FILL_021`: “top cảnh báo” thành “top tốt”, cả 3 focused repetitions.
- Rewrite `explicit_override`: một trong 3 repetitions đổi Đà Nẵng thành Hà Nội. Temperature 0
  không đủ chứng minh mọi call sẽ giữ explicit fields. Full run của local task cũng có lỗi,
  trong khi focused repetitions đúng; cần ghi nhận variance, không chọn riêng output đẹp.
- Rewrite còn mất “tăng giảm”, bỏ label “nền FTTH” chưa được định nghĩa, thiếu đơn vị byte/s
  trong HDD, bỏ definition của một số aliases và chưa làm query hoàn toàn standalone.
- Rewrite `conv01:CONV01V2_FILL_033` vẫn đổi comparison wording; không được suy ra report year
  hoặc coi output không thêm năm là toàn bộ case đã đúng.

Characterization riêng đặt nguyên custom policy vào cuối native system prompt, giữ những input
khác như cũ, 2 cases × 2 repetitions. Nó vẫn emit ordinary-query facts và vẫn bỏ same-text
reassertion. **Không có bằng chứng để adopt cách wiring đó; runtime/vendor không bị sửa.**

Một số scoring expectations cần phân biệt với product contract: meeting-only explicit override
được phép là CONVERSATION dù historical canonical label coi nó negative. Synthetic case tên
`telecom_relative_focus_without_source_date` thực tế có Observation Date; không tune để làm
mất local focus chỉ để đạt negative label đó. Không sửa canonical dataset để nâng điểm.

## Validation, acceptance và rollout

Deterministic validation cuối: **875 unit/contract tests + 32 vendor tests = 907 passed**.
Ruff check/format và diff whitespace check chạy trên các file của thay đổi này. Prompt literal
tests/JSON example tests chỉ xác nhận contract/format, không thay thế live semantic validation.

Acceptance của provider phải kiểm tra giữ explicit fields, đúng qualifiers/criteria/units,
same-context changes, cancellation/reassertion, unknown/tied ambiguity, source eligibility và
scope. Một safety failure không được bù bằng tổng số positive cases hay token tiết kiệm.
Các final raw failures trên khiến verdict là **HOLD_PROVIDER_SEMANTIC_GATE**.

Giữ native Mem0 formation/search, schema, source metadata, receipts, deletion/fencing và window.
Round này không sửa vendor hoặc bump `.7`, không get_all, migration, backfill hoặc subsystem mới.
Không deploy/rollout. Không kết luận rằng có thể sửa toàn bộ lỗi bằng cách thêm prompt rules.
PC vẫn test OpenAI + KiRa thật; K8s test Qwen3-14B base + KiRa thật. Các gate đó chưa chạy ở đây.

Raw artifacts (ignored, không commit dữ liệu dataset):

- `artifacts/prompt-tuning-summary.json`: digest/count/usage của tất cả runs.
- `artifacts/formation-prompt-v10-canonical.json`, `formation-prompt-v10-synthetic.json`.
- `artifacts/prompt-v7-all-live.json`, `prompt-v7-focused-live.json`.
- `artifacts/formation-policy-priority-characterization.json`.
- `artifacts/*-system-frozen.json`: exact prompt snapshots.

Các runners và frozen inputs dưới `artifacts/` chỉ phục vụ diagnostic lịch sử trên laptop,
không được chuyển theo Git checkout và không phải entrypoints benchmark trên PC. Không cần
copy artifacts hoặc authorization laptop để thực hiện benchmark PC mới.

Trên PC dùng các entrypoints tracked trong `scripts/benchmark/` theo
[company PC handoff](company-pc-ai-handoff.md). Diagnostic recent-window chạy trên source checkout
bằng `scripts.benchmark.compare_rewrite_context` với env/provider/profile explicit; fixture mặc
định là `tests/support/context_window_cases.json`. Nó không tái tạo 140 frozen-input observations
trong report này và không thay cho canonical evaluator preflight hoặc real-KiRa acceptance.
