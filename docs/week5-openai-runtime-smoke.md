# Week 5 — OpenAI runtime smoke

Runbook này kiểm tra runtime Gateway + Worker với OpenAI thật cho ba capability:

- query rewrite qua Chat Completions;
- memory extraction qua Chat Completions;
- embedding/search qua Embeddings API.

KiRa vẫn là local mock và toàn bộ prompt là synthetic. Đây là dependency/runtime smoke, không phải
semantic benchmark hoặc nghiệm thu KiRa thật.

## Cấu hình

```powershell
Copy-Item evaluation/week5.env.example .env.week5.local
notepad .env.week5.local
```

Điền tối thiểu:

```dotenv
OPENAI_API_KEY=<local secret>
WEEK5_OPENAI_BASE_URL=https://api.openai.com/v1
WEEK5_OPENAI_CHAT_MODEL=gpt-4o-mini
WEEK5_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
WEEK5_EMBEDDING_DIMENSIONS=1536
```

`.env.week5.local` đã bị Git và Docker build context bỏ qua. Không commit, log hoặc chụp giá trị
`OPENAI_API_KEY`. Lưu ý local Docker environment vẫn có thể được người có quyền Docker trên máy
đọc; chỉ dùng key dev có giới hạn ngân sách/quyền phù hợp.

## Preflight trực tiếp

Chạy ba request nhỏ, write-free trước khi dựng stack:

```powershell
uv run --frozen python -m scripts.run_week5_benchmark preflight `
  --profile external_synthetic `
  --suite formation `
  --suite rewrite `
  --suite retrieval `
  --env-file .env.week5.local `
  --env-file-only `
  --output artifacts/week5/openai-runtime-preflight.json
```

`extraction_json`, `rewrite_chat` và `embedding_batch` phải `PASS`. Suite retrieval có thể
`NOT_RUN` ở bước này nếu chưa cấp PostgreSQL URL; stack bên dưới cung cấp PostgreSQL riêng.

## Chạy stack

```powershell
uv run --frozen python scripts/week5_openai_stack.py up
```

Launcher cố ý cho giá trị trong `.env.week5.local` thắng biến môi trường cùng tên của máy. Việc
này tránh Docker Compose vô tình nhận một `OPENAI_API_KEY` khác từ shell/IDE.

Stack chỉ tạo sáu service: PostgreSQL, migration, memory init, KiRa mock, Gateway và Worker. Ba
provider mock của Week 4 không được chạy. Endpoint local:

- Gateway: `http://127.0.0.1:18000`
- Worker: `http://127.0.0.1:18001`
- KiRa mock: `http://127.0.0.1:18122`
- PostgreSQL: `127.0.0.1:15433`

Kiểm tra trạng thái:

```powershell
Invoke-RestMethod http://127.0.0.1:18000/ready
Invoke-RestMethod http://127.0.0.1:18001/ready
```

## Kích hoạt đủ ba capability

Dùng cùng một session. Lượt đầu tạo completed turn và job extraction; chờ Worker xử lý rồi lượt
hai buộc Gateway dùng recent/LTM context và gọi rewriter.

```powershell
$smokeSession = "week5-openai-$([guid]::NewGuid().ToString('N').Substring(0, 8))"

uv run --frozen python scripts/smoke_gateway.py `
  --gateway-url http://127.0.0.1:18000 `
  --session-id $smokeSession `
  --message "Trong dữ liệu synthetic, tôi muốn nhận báo cáo KPI vào sáng thứ Hai."

Start-Sleep -Seconds 8

uv run --frozen python scripts/smoke_gateway.py `
  --gateway-url http://127.0.0.1:18000 `
  --session-id $smokeSession `
  --message "Khi nào nên gửi báo cáo KPI cho tôi?"
```

Xem evidence không chứa prompt/output:

```powershell
docker exec kira-context-week5-openai-worker-1 kira-memory-jobs stats

(Invoke-WebRequest http://127.0.0.1:18000/metrics).Content | `
  Select-String 'kira_context_rewrite_total|kira_memory_search_total|kira_memory_job_schedule_total'
```

Cần thấy ít nhất một job `completed` và rewrite outcome `success`. Nếu job còn `pending` hoặc
`processing`, chờ vài giây rồi đọc stats lại. Không dùng nội dung thật hoặc dữ liệu nội bộ với
profile OpenAI bên ngoài.

## Dừng và dọn

Giữ database để kiểm tra lại:

```powershell
uv run --frozen python scripts/week5_openai_stack.py down
```

Xóa cả database synthetic Week 5:

```powershell
uv run --frozen python scripts/week5_openai_stack.py down --volumes
```
