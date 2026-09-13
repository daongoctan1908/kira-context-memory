# Temporal grounding tối giản theo từng lần add

Policy: `kira-memory-policy-v5`. Corpus giữ nguyên `kira-memory-policy-eval-v4` (46 ca).
Mục tiêu là cung cấp thời gian nguồn cho memory formation và ghi mốc vào fact khi cần,
không xây temporal reasoning engine. Rewriter và KiRa chịu trách nhiệm suy luận downstream.
Không sửa package Mem0/schema DB, không thêm expiration, validity fields hay supersession.

## Đường dữ liệu

`conversation_messages.message_timestamp` → `ConversationMessage.timestamp` →
`Mem0Adapter.process_memory()` → `build_memory_extraction_prompt()` → `AsyncMemory.add(prompt=...)`.

Không dùng `conversation_messages.created_at`: đó là thời điểm persist, có thể muộn hơn lúc nhận
tin user. Gateway hiện ghi timestamp user ở đầu request và timestamp assistant khi hoàn thành;
đây là mốc hệ thống ghi nhận, không phải thời gian thiết bị của người dùng hay ngày sự kiện được kể.

Adapter vẫn gửi đúng `[{role, content}, ...]` và metadata nhận diện formation cũ. Mỗi lần gọi tạo
một chuỗi prompt mới gồm policy chọn memory, một đoạn temporal instruction ngắn và JSON source time.
Không thay đổi `client.custom_instructions` dùng chung và không thêm timestamp vào content.

Payload chỉ có `source_time`: một phần tử cho mỗi **New Message**, theo đúng thứ tự đầu vào,
gồm cả user và assistant. Vị trí mảng là cách ghép với message; không lặp lại role/index/count.

```json
{
  "source_time": [
    "2026-09-14T09:00:00+07:00",
    "2026-09-14T09:01:00+07:00"
  ]
}
```

Helper chỉ kiểm tra timestamp, quy đổi timezone và serialize. Không đọc/parse content, không tính
today/yesterday/week/month, không có `calendar_dates`, không có temporal examples hay business-specific
examples trong custom instruction của ứng dụng. Policy vẫn giữ taxonomy, attribution, bảo toàn
công thức/phạm vi và quy tắc không lưu secret hoặc dữ liệu không đủ điều kiện. Prompt gốc của
package Mem0 được giữ nguyên; các ví dụ native của package không bị sửa trong refactor này.

Trong bản Mem0 đang khóa, `prompt or self.custom_instructions` chọn hướng dẫn cho extraction.
Tham số `prompt=` thay thế policy mặc định, nên helper luôn ghép policy đầy đủ. Mảng này không đi
vào embedding dùng để tìm memory cũ và không đi vào history messages nội bộ của Mem0.

## Nội dung đoạn temporal instruction

- `source_time[i]` là timestamp tin cậy của New Message thứ `i`.
- Resolve relative time theo `source_time` của chính message, không theo processing time.
- `source_time` là metadata, không phải fact để lưu.
- Thiếu hoặc không ghép rõ được `source_time` thì không tự đặt ra ngày.

Đoạn temporal duy nhất nằm trong `app/application/services/memory_temporal.py`; policy nền không
nhắc lại quy tắc source time. Việc có mốc trong text không bảo đảm model luôn chọn đúng mốc, cũng
không tự ẩn fact cũ khi retrieval. Không thay đổi Rewriter/KiRa trong lần refactor này.

## Cấu hình

Gateway và Worker đọc cùng một biến, đã được nối vào base Compose mà Week 5 kế thừa:

```dotenv
MEMORY_SOURCE_TIMEZONE=Asia/Ho_Chi_Minh
```

Timezone phải là IANA zone hợp lệ; cấu hình sai làm bước tạo extraction prompt thất bại. Đây là
cấu hình mặc định của deployment, chưa phải timezone riêng cho từng user. `tzdata` là dependency
trực tiếp để việc quy đổi IANA/DST hoạt động cả trên Windows và image tối giản. Không còn cấu hình
week start; model tự hiểu biểu thức tuần từ timestamp và ngữ cảnh.

Nếu muốn đổi timezone trong stack Week 5, thêm biến trên vào `.env.week5.local`. Source mới được
áp dụng sau khi rebuild/restart; lệnh local:

```powershell
uv run --frozen python scripts/week5_openai_stack.py up
```

Đợt triển khai này chưa chạy lệnh trên và không sửa DB. Các event đã có formation receipt vẫn
trả kết quả cũ khi retry; không tự re-extract các job đã hoàn thành chỉ vì đổi policy.

## Kiểm chứng và giới hạn

- Test tập trung vào đúng thứ tự/mốc nguồn, timezone/DST, null, marker giả, prompt ngắn không có
  trường lịch, cùng event retry, hai lần formation chạy đồng thời và cấu hình. Test tính sẵn lịch
  của v4 đã bỏ; corpus semantic không bị sửa để phù hợp với implementation mới.
- Contract test chạy qua **AsyncMemory thật**, thay provider/storage bằng doubles: xác nhận
  `prompt=` tới LLM, embedding và saved messages giữ nguyên text, policy dùng chung không đổi,
  replay receipt bỏ qua provider. Test này không đánh giá khả năng hiểu ngày của model.
- Corpus có 12 ca temporal và 34 ca trước đó. Evaluator và adapter dùng cùng helper, có thể đặt ngày
  Observation Date muộn hơn ngày nguồn để phát hiện model dùng nhầm đồng hồ worker.
- Chưa chạy live semantic gate cho đúng policy v5 này. Unit/contract test không phải bằng chứng
  độ chính xác temporal của model. Không rebuild Docker hoặc ghi DB khi refactor.

### Kết quả lịch sử, không phải điểm của v5

- V4 có precompute và ví dụ: lượt cuối trong `artifacts/temporal-grounding/final.json` đạt 13/15
  với `gpt-4o-mini-2024-07-18`; chưa đạt toàn bộ gate. Các lần thử prompt trước đó cũng nằm ở
  `artifacts/temporal-grounding/` (git-ignore).
- Review trước refactor chạy lại hai vòng cùng 12 ca temporal + 3 đối chứng, cùng model,
  temperature 0, max output 1000. Mỗi vòng v4 đạt 13/15; candidate tối giản đạt 7/15 theo bộ chấm,
  hoặc 8/15 khi đối chiếu ca ngày đúng nhưng viết bằng chữ. Candidate đó vẫn giữ các ví dụ policy
  nghiệp vụ nền, nên không đồng nhất với v5 đã bỏ các ví dụ này. Bằng chứng của review ở lịch sử
  task; không ghi đè report v4 hoặc lưu report mới vào repository.
- Bộ chấm ở một số ca chỉ nhận ngày số; ngày viết bằng chữ có thể bị đánh trượt dù cùng ý nghĩa.
  Vẫn giữ nguyên bộ chấm; không gộp ca đạt từ nhiều lần chạy hoặc báo điểm lịch sử thành điểm v5.

Đây là lựa chọn đơn giản hóa implementation, không phải tuyên bố cải thiện benchmark. Model vẫn
có thể bỏ qua nguồn hoặc ghép sai ngày; chưa có kiểm chứng held-out/production cho v5.

Chạy gate bằng `MEMORY_LLM_BASE_URL`, `MEMORY_LLM_MODEL`, `MEMORY_LLM_API_KEY` và timezone cấu hình
ở trên. Ca kiểm thử `tuần này` hiện vẫn kỳ vọng tuần lịch thứ Hai đến Chủ nhật; implementation
không tính hoặc truyền trước khoảng này:

```powershell
uv run --frozen python -m scripts.check_live_memory_policy `
  --case temporal_week_delayed_worker `
  --case temporal_multiple_messages_keep_own_dates `
  --report artifacts/temporal-grounding/check.json
```

CLI trả mã 1 khi có ca trượt hoặc dependency lỗi; mã 2 cho lỗi cấu hình hoặc ghi report.
Không đổi bộ chấm để che lỗi model.
