# Week 1 KiRa Baseline - Live Smoke Test

Runbook này dùng trên PC công ty có route tới KiRa Test. Không chạy live smoke từ laptop cá
nhân vì mạng hiện tại không truy cập được `10.255.62.64:8122`.

## 1. Điều kiện trước khi chạy

- Python 3.11 và `uv`, hoặc Docker.
- Source code đúng revision cần kiểm thử, kèm `uv.lock`.
- KiRa username và Basic credential lấy từ nguồn secret được phê duyệt.
- Không chụp màn hình, copy log hoặc commit file chứa credential/token.

Xác nhận network route trên PC công ty:

```powershell
Test-NetConnection -ComputerName 10.255.62.64 -Port 8122
```

Chỉ tiếp tục khi `TcpTestSucceeded` là `True`.

## 2. Chuẩn bị cấu hình

```powershell
Copy-Item .env.example .env
notepad .env
```

Điền các giá trị thật vào `.env` local:

- `KIRA_BASE_URL=http://10.255.62.64:8122`
- `KIRA_USERNAME=<approved username>`
- `KIRA_DOMAIN=VBI`
- `KIRA_BASIC_AUTH=<credential sau prefix Basic>`

Không thêm literal `Basic ` vào `KIRA_BASIC_AUTH`; adapter tự tạo header. `.env` phải tiếp tục
được Git ignore.

## 3. Chạy native bằng uv

Nên dùng cách này cho live test đầu tiên để tách lỗi ứng dụng khỏi Docker Desktop networking.

Terminal 1:

```powershell
uv sync --frozen --all-groups
uv run uvicorn app.presentation.api.main:app --host 127.0.0.1 --port 8000
```

Terminal 2:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready

uv run python scripts/smoke_gateway.py `
  --gateway-url http://127.0.0.1:8000 `
  --session-id smoke-week1-001 `
  --message "Cho tôi số liệu thử nghiệm hợp lệ trên KiRa"
```

Thay message bằng câu hỏi nghiệp vụ đã được phép dùng trong môi trường Test.

## 4. Kết quả đạt

Smoke script phải:

- in status KiRa khi nhận được `status_response`;
- in text tăng dần khi từng chunk tới, không chờ stream kết thúc;
- kết thúc với dòng `PASS`;
- báo số event, số ký tự, Gateway correlation ID, KiRa requestId và messageId;
- exit code `0`.

T1.18 chỉ hoàn tất khi đã lưu evidence không nhạy cảm gồm:

- thời gian và revision/commit được test;
- lệnh chạy không chứa secret;
- kết quả `/health` và `/ready`;
- dòng `PASS` của smoke script;
- xác nhận text xuất hiện incremental.

Không lưu raw Basic credential, runtime token hoặc full response nghiệp vụ vào evidence.

## 5. Chạy bằng Docker

Build image versioned:

```powershell
docker build --build-arg APP_VERSION=0.1.0 -t kira-context:0.1.0 .
```

Chạy container:

```powershell
docker run -d --name kira-context `
  --env-file .env `
  -p 8000:8000 `
  kira-context:0.1.0
```

Container không dùng `--rm`, vì vậy vẫn xuất hiện trong tab **Containers** của Docker Desktop
sau khi dừng. Kiểm tra trạng thái và log bằng:

```powershell
docker ps --filter "name=kira-context"
docker logs kira-context
```

Chạy lại probes và smoke script ở Terminal 2. Nếu native chạy được nhưng container timeout,
kiểm tra route/VPN/firewall từ Docker Desktop VM tới mạng nội bộ trước khi sửa code Gateway.
Khi không còn cần container kiểm thử, xoá có chủ đích bằng `docker rm -f kira-context`.

## 6. Diễn giải lỗi

- HTTP `502` + `KIRA_AUTH_FAILED`: kiểm tra username/domain/Basic credential và quyền Test.
- HTTP `502` + `KIRA_HTTP_ERROR`: KiRa trả non-2xx; dùng correlation ID để đối chiếu log.
- HTTP `504` hoặc `KIRA_TIMEOUT`: kiểm tra route, firewall và timeout config.
- `event: gateway_error`: lỗi xảy ra sau khi SSE đã bắt đầu; stream sẽ đóng có kiểm soát.
- `stream completed without a text response`: KiRa đóng stream nhưng không có text chunk;
  giữ requestId/messageId để làm việc với owner KiRa.

`tokenExpirationTime` đang tạm được hiểu là TTL giây với skew 60 giây. Nếu live test cho thấy
token refresh không đúng, tắt test tiếp theo và xác nhận semantics với owner KiRa trước khi đổi.
