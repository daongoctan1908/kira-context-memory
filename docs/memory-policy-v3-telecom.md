# Memory policy v3 — telecom extraction

Checkpoint lịch sử: `kira-memory-policy-v3`. Corpus: `kira-memory-policy-eval-v3`.
Ngày thay đổi: 2026-09-13. Sáu taxonomy và format JSON Mem0 giữ nguyên.

Policy hiện hành là v5 và dùng `custom_instructions` cấu hình sẵn của Mem0, không có temporal
sidecar theo request. Các kết quả và giới hạn bên dưới mô tả v3 tại thời điểm kiểm tra, không phải
kết quả chạy lại v5.

**Trạng thái: bản cải tiến để thử nghiệm, chưa đạt toàn bộ live acceptance gate.**
Không xem việc unit test qua là bằng chứng model luôn làm đúng policy.

## Hành vi được bổ sung

- Nhớ quy tắc dùng lại, không biến yêu cầu "riêng lần này" thành sở thích lâu dài.
- Giữ công nghệ/dịch vụ, cell/site/khu vực, nhóm thuê bao, UL/DL, kỳ đo, múi giờ và các
  điều kiện áp dụng cùng với fact. Chỉ giữ thông tin thực sự được nói, không tự điền chỗ thiếu.
- Giữ nguyên mã KPI/counter, định nghĩa nội bộ, công thức, toán tử, tử số/mẫu số, cách cộng gộp,
  đơn vị, phủ định và ngoại lệ. Không dùng định nghĩa/threshold ngành để đoán cấu hình của user.
- Loại số đo, alarm, log, bảng kết quả nhất thời từ cả user lẫn assistant, trừ bối cảnh phân tích
  đã được xác nhận để tái sử dụng và có phạm vi/ngày rõ ràng.
- Đề xuất/kết luận của assistant cần được user xác nhận cho việc tái sử dụng; tiếp tục trò
  chuyện hay cảm ơn không đồng nghĩa với xác nhận. Không nâng giả thuyết thành nguyên nhân đã chốt.
- Viết fact bằng ngôn ngữ nguồn. Công thức tiếng Việt không được dịch sang tiếng Anh; tên kỹ thuật
  giữ nguyên. Mỗi quy tắc là một fact tự đủ nghĩa, gồm cả ngoại lệ của chính quy tắc đó.
- Ngày tương đối chỉ được giải nghĩa khi hội thoại có mốc lịch rõ ràng. Ngày worker chạy không
  chứng minh ngày hội thoại diễn ra. Bỏ fact chỉ có ý nghĩa tạm thời nếu chưa xác định được hạn.
  Nếu nguồn đã nêu ngày cụ thể, giữ nguyên ngày đó dù worker xử lý muộn; không ngụ ý trọng tâm
  đã hết hạn vẫn đang có hiệu lực.
- Khi user sửa quy tắc, fact mới giữ phạm vi và ngày hiệu lực đã nêu, diễn đạt quan hệ thay thế.
  Không dùng memory cũ làm nguồn fact mới, không gộp hai quy tắc khác phạm vi.

Các ví dụ/counter/threshold trong corpus đều là synthetic, không phải cấu hình vận hành Viettel.

## Đường đi của policy

`app/application/services/memory_policy.py` → `build_mem0_config()` trong
`app/infrastructure/memory/mem0_adapter.py` → `MemoryConfig.custom_instructions` →
`AsyncMemory.add(infer=True)` → prompt extraction gốc của Mem0.

Đây là thay đổi prompt/config, không sửa engine trong `packages/viettel-mem0` hay migration DB.
Không cần re-embed các memory cũ; policy mới chỉ áp dụng cho các lần extraction mới.
Process đang chạy phải được khởi động lại với source/image đã rebuild. Với local Week 5:

```powershell
uv run --frozen python scripts/week5_openai_stack.py up
```

## Kiểm chứng

Corpus giữ 18 ca v2 và thêm 16 ca telecom: phạm vi alarm, aggregation, ARPU nội bộ, mã KPI chưa
có định nghĩa đầy đủ, bảng số đo user dán vào, yêu cầu một lần, UL/DL/đơn vị/múi giờ, điểm phần
trăm, giả thuyết nguyên nhân, incident đã xác nhận, ngưỡng sửa đổi, quy tắc khác phạm vi, memory cũ
không phải chứng cứ mới, ngày tương đối có/không có mốc nguồn và hai quy tắc với ngoại lệ riêng.

Evaluator nhận `existing_memories` theo từng case với ID dạng chuỗi số như runtime. Scorer mới
kiểm tra các nhóm từ phải xuất hiện trong **cùng một fact**, tránh trường hợp toàn bộ từ khóa vẫn
đủ nhưng ngoại lệ bị gắn sang quy tắc khác. Scorer vẫn là kiểm tra từ khóa/công thức có giới hạn;
không phải semantic judge đầy đủ. Các ca correction chỉ kiểm tra output extraction, không chứng
minh memory cũ được xóa hay ngừng xuất hiện khi retrieval.

```powershell
uv run --frozen pytest tests/unit/application/test_memory_policy.py `
  tests/unit/scripts/test_check_live_memory_policy.py `
  tests/unit/infrastructure/memory/test_mem0_adapter.py `
  tests/vendor/test_mem0_pristine_contract.py --no-cov
```

Gate model thật dùng `MEMORY_LLM_BASE_URL`, `MEMORY_LLM_MODEL`, `MEMORY_LLM_API_KEY` và
`MEMORY_OPERATION_TIMEOUT_SECONDS`, như B2:

```powershell
uv run --frozen python -m scripts.check_live_memory_policy `
  --report artifacts/memory-policy-eval-v3.json
```

Gate chỉ gửi hội thoại synthetic tới model và chấm JSON trong RAM, không gọi `Memory.add()`
hay ghi database. Report chỉ chứa case IDs, counts, reason codes, version và latency.
Control Week 5 trong `week5-baseline.json` vẫn khóa policy v2; không sửa evidence lịch sử để
đổi thành v3. Gate này không thay thế semantic benchmark Week 5 hay nghiệm thu production.

### Kết quả thực chạy ngày 2026-09-13

- Ruff lint/format: đạt. Toàn bộ `tests/unit tests/vendor --no-cov`: **659 passed**.
- API `api.openai.com`, model `gpt-4o-mini`, temperature `0`, giới hạn output `1000` tokens;
  dùng cùng 34 ca và cùng bộ chấm cho cả hai policy.
- Policy v2 ở `HEAD` trước chỉnh sửa: **8/34 ca đạt**, 25 ca trượt kiểm tra nội dung và
  1 ca lỗi envelope JSON (`PolicyEvalProtocolError`).
- Policy v3 cuối cùng: **26/34 ca đạt**, 8 ca trượt kiểm tra. Một request gặp HTTP 429 đã
  được chạy lại trước khi hoàn thành; lần lỗi được giữ riêng trong `dependency_attempts`.
- Evidence local, được git-ignore: `artifacts/policy-v3/kira-memory-policy-v2.json` và
  `artifacts/policy-v3/kira-memory-policy-v3.json`. Report v3 có hash prompt để đối chiếu.

Đây là **kết quả bộ chấm trên corpus phát triển**, không phải độ chính xác production.
Prompt đã được chỉnh dựa trên chính các ca này, chưa có tập held-out hay nhiều lượt chạy để
đánh giá độ ổn định. Không gộp kết quả tốt nhất của các lần chỉnh prompt thành một lượt đạt.

Các ca chưa qua ở lượt cuối:

| Ca | Vấn đề bộ chấm phát hiện |
| --- | --- |
| `assistant_reference_explicitly_confirmed` | Thiếu từ khóa chỉ việc dùng lại; kiểm tra output riêng cho thấy “các báo cáo sau” là diễn đạt hợp lệ nhưng bộ chấm chỉ nhận “lần sau”/“mặc định” — một false negative của scorer. |
| `assistant_threshold_explicitly_confirmed` | Tóm tắt thành ngưỡng 10%, không giữ nguyên assignment `threshold = 10%`. |
| `telecom_alarm_scope_and_window` | Bỏ phạm vi Hà Nội khỏi quy tắc LTE. |
| `telecom_operator_arpu_definition` | Bỏ nhóm thuê bao trả trước khỏi định nghĩa ARPU. |
| `telecom_confirmed_dated_incident` | Tách kết luận và xác nhận thành hai fact. |
| `telecom_relative_focus_without_source_date` | Vẫn giữ trọng tâm “tuần này” dù chưa có ngày nguồn để xác định hạn. |
| `telecom_relative_focus_with_source_date` | Bỏ trọng tâm có ngày nguồn rõ ràng khi ngày xử lý đã muộn hơn. |
| `telecom_separate_rules_keep_exclusions_attached` | Gộp hai quy tắc độc lập thành một fact. |

Giữ nguyên bộ chấm trong phép so sánh này; không hạ yêu cầu để đổi các ca trượt thành đạt.
Các lỗi còn lại cho thấy custom instructions chưa bảo đảm được việc giữ phạm vi, chia fact
và xử lý thời gian. Cần kiểm chứng tiếp trước khi dùng làm policy production.

## Giới hạn cần xử lý riêng

- Adapter hiện chỉ chuyển role/content, chưa chuyển timestamp từng message. Quy tắc thận trọng
  về ngày tương đối giảm nguy cơ đoán sai nhưng có thể bỏ sót temporary focus chưa đủ ngày.
- Native V3 `add(infer=True)` thêm fact; instructions không tự xóa/sửa bản ghi cũ. Việc vô hiệu hóa
  ngưỡng cũ cần cơ chế lifecycle/retrieval riêng.
- Ngày hết hạn viết trong text không tự tạo `expiration_date` cho vector store. Chưa có bảo đảm
  tự động loại memory hết hạn chỉ nhờ thay đổi prompt này.

## Tài liệu domain tham khảo

- [3GPP TS 28.554, mục 5](https://www.etsi.org/deliver/etsi_ts/128500_128599/128554/18.07.00_60/ts_128554v180700p.pdf):
  định nghĩa KPI gồm công thức, đơn vị, kiểu thống kê và phạm vi đối tượng/cách cộng gộp.
- [3GPP TS 32.401](https://www.etsi.org/deliver/etsi_ts/132400_132499/132401/19.00.00_60/ts_132401v190000p.pdf):
  phạm vi tài nguyên, kỳ đo và quản lý kết quả đo hiệu năng.
- [Vodafone Annual Report 2026 — definitions](https://reports.investors.vodafone.com/view/500231330/252/):
  ví dụ định nghĩa chỉ tiêu kinh doanh của một nhà mạng, không áp làm định nghĩa cho Viettel.

Các quy tắc extraction ở trên là lựa chọn thiết kế cho KiRa dựa trên nguồn tham khảo, không phải
yêu cầu của tiêu chuẩn về cách triển khai LLM memory.
