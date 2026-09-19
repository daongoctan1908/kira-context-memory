> Đây là contract kế hoạch canonical. Tiến độ/live evidence nằm trong
> `company-pc-ai-handoff.md` và `week5-internal-k8s-acceptance.md`; không suy ra task đã PASS chỉ
> từ việc task xuất hiện trong plan.

# Implementation plan v4: hoàn thiện benchmark, xây chatbot và đưa lên production tối thiểu

## 1. Mục tiêu và quyết định đã khóa

Đây là canonical master plan. Tài liệu runbook chi tiết có thể bổ sung cách chạy nhưng không được
thay đổi contract, gate hoặc thứ tự dependency trong file này.

- Laptop hoàn thiện harness, backend sản phẩm, frontend, tests và deployment artifacts.
- PC công ty dùng KiRa thật để materialize/freeze dataset, chạy OpenAI full-corpus acceptance và
  build exact images. Kết quả PC chỉ diagnostic/technical acceptance, không quyết định promotion.
- VDI chỉ chuyển/publish đúng image digest. Kubernetes dùng KiRa thật và internal
  extraction/embedding/rewrite/judge để chạy benchmark chính thức.
- Tiếp tục dùng FastAPI, PostgreSQL/pgvector, Mem0, Worker queue, SSE và OTel/Langfuse.
- Auth do ứng dụng quản lý, đăng nhập bằng username; frontend riêng dùng React + TypeScript + Vite.
- Kubernetes dùng manifest format mà platform công ty yêu cầu; chỉ dùng Helm nếu đó là contract
  deployment thực tế. Triển khai sau khi Docker Compose acceptance hoàn tất.
- Dataset chạy toàn bộ, không chia dev/holdout. Kết quả là acceptance/regression trên corpus đã biết.
- Performance chỉ kiểm tra sau khi candidate vượt semantic gates: 30 successful samples/variant, tối đa 40 measured attempts, chưa khóa ngưỡng latency cố định.
- Xóa conversation sẽ xóa các memory row có đúng `user_id + conversation_id`, cùng receipt liên quan. Không xây provenance graph, không rebuild memory từ conversation khác.

Ba chỗ triển khai chính:

- Hợp đồng benchmark: [week5-benchmark-contract.md](/C:/Code/kira-context-memory/docs/week5-benchmark-contract.md).
- Boundary giữa ứng dụng và Mem0: [mem0_adapter.py](/C:/Code/kira-context-memory/app/infrastructure/memory/mem0_adapter.py).
- Schema ứng dụng: [schema.py](/C:/Code/kira-context-memory/app/infrastructure/postgres/schema.py).

Effort mỗi task: **S** khoảng nửa ngày; **M** khoảng 1 ngày; **L** khoảng 2–3 ngày. Chưa tính chờ provider, human review hoặc sửa lỗi phát hiện trong acceptance.

### Thứ tự thực hiện

```text
Laptop:      T0 → T1 → T2 → T3; đồng thời có thể tiếp tục T6 → T7 → T8 → T9 → T10
PC công ty:  T4 — KiRa materialization, review/freeze, OpenAI acceptance, exact images
VDI:         handoff/publish đúng digest từ T4
Kubernetes:  T5 — internal discovery, confirmation, performance và promotion chính thức
Deployment:  T11 sau khi T5 và T10 đạt acceptance
```

Phần product **T6–T10 không phụ thuộc việc benchmark chính thức đã chạy**, nên có thể hoàn thiện trên
laptop trong lúc chưa truy cập được KiRa/internal providers.

---

## 2. Task triển khai

### Phase 0 — Khóa baseline và sửa contract

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T0.1 — Repository hygiene** | Thêm `.gitattributes`, chuẩn hóa LF trong commit riêng; ignore root Git bundles nhưng giữ file hiện có; sửa tài liệu phân biệt Mem0 hiện tại `.4` với historical control `.3`. | Ruff lint/format, dataset validator và `git diff --check` sạch; normalization không thay semantic code. | Không | S |
| **T0.2 — Benchmark contract v4** | Giữ historical control SHA `75deb1d…`; cập nhật full-corpus limitation, performance chạy muộn, attempt cap, review verdict và runtime provenance. Cập nhật contract ID trong types/tests. | Docs, models và CLI cùng dùng contract v4; source manifest lịch sử không bị sửa để giả thành runtime mới. | T0.1 | S |
| **T0.3 — Phân biệt historical control và release candidate** | Mỗi run ghi riêng runtime SHA, harness SHA, prompt/config hash và package versions. Candidate có thay đổi runtime ngoài prompt phải khai báo đầy đủ. | Report không quy mọi khác biệt chất lượng cho prompt nếu runtime cũng thay đổi. | T0.2 | S |

---

### Phase 1 — Harness nền tảng

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T1.1 — Dataset compiler** | Chuyển canonical dataset sang formation, retrieval, rewrite và cross-session cases; namespace IDs; ghi eligibility và lý do blocked. Gold không đi vào ingestion input. | Cùng dataset/seed tạo cùng output; mọi source row được đối chiếu bằng coverage report. | T0.3 | M |
| **T1.2 — Scorer** | Formation P/R/F1, unsupported facts, duplicate excess, attribution/formulas; retrieval Recall@1/3/5, Precision@K, MRR, no-hit FP; rewrite constraints. | Fixtures bao phủ pass/fail/empty/protocol error; zero denominator là `N/A`; không giấu lỗi khỏi coverage. | T1.1 | L |
| **T1.3 — Human audit** | LLM judge chấm semantic; human audit toàn bộ `UNCERTAIN`, mọi judge/deterministic conflict và một ngân sách toàn cục `ceil(10% × tổng semantic PASS/FAIL)`, stratified theo variant/suite/verdict/bundle. Review bind case/output hash/reviewer/revision; bất đồng mở rộng cùng reason family. | Không chấm tay toàn corpus; output đổi làm review cũ invalid; audit bắt buộc chưa xong hoặc gold lỗi thì evidence chưa đủ. | T1.2 | M |
| **T1.4 — Artifacts và resume** | Versioned run manifest, case results, review records, reports; ghi atomic và resume theo hashes. | Interrupted run không mất kết quả đã hoàn tất; config/dataset đổi thì từ chối resume. | T1.3 | M |
| **T1.5 — Runtime isolation** | Build control/candidate từ exact SHA; DB/schema/collection tách theo run; cleanup theo ownership manifest. | Không lẫn vectors, receipts, jobs hoặc runtime imports giữa hai variant. | T1.4 | L |
| **T1.6 — CLI** | Hoàn thiện `validate`, `preflight`, `compile`, `run`, `review export/import`, `compare`, `bundle`. | Exit codes và typed outcomes rõ; thiếu dependency không thành PASS; canonical dataset bị chặn khi dùng external provider. | T1.5 | M |

Run manifest bắt buộc chứa source/config/dataset/review hashes, model/deployment, embedding dimension, effective timeout/retry, seed, resource ownership và dirty flag.

Official run yêu cầu source sạch. Raw internal artifacts ở thư mục ignored và chỉ chuyển qua kênh nội bộ được phép; không commit credentials hoặc KiRa responses.

---

### Phase 2 — Formation benchmark

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T2.1 — Characterize pinned Mem0** | Kiểm tra A tạo fact → B lặp fact → B đưa fact mới; quan sát IDs, metadata, lifecycle và user-scoped context. Khóa behavior hiện tại: native formation tạo ADD hoặc bỏ duplicate. | Test thực thi native pipeline, chỉ thay provider/storage bằng doubles; không suy ra behavior từ fake lifecycle response. | T1.6 | M |
| **T2.2 — Write-free evaluator** | Đi qua extraction/parser native với storage không ghi dữ liệu; quan sát valid empty, malformed, provider/protocol errors. | Không sửa parser hoặc prompt baseline; malformed output không thành true-negative hợp lệ. | T2.1 | M |
| **T2.3 — Persistent evaluator** | Đi qua application/Mem0/pgvector; đối chiếu extracted facts, persisted rows và receipt. Xuất formed-corpus artifact có run provenance. | Fresh-state quality run; receipt replay đúng; artifact dùng được cho retrieval evaluator. | T2.2 | L |
| **T2.4 — Retry/dedup correctness** | Test same-event replay, fresh event, paraphrase, failure trước/sau commit, queue retry và lease reclaim. | Không duplicate durable write do replay; fresh events được chấm độc lập. | T2.3 | M |
| **T2.5 — Formation report/candidates** | Report theo family/bundle; đăng ký tối đa hai prompt/config candidates, có rendered hashes và declared differences. | Không sửa production prompt trong lúc xây harness; mọi score có denominator và review status. | T2.4 | M |

Native `update()` tồn tại trong SDK không đồng nghĩa `add()` tự UPDATE. Nếu nâng Mem0 làm characterization tests thay đổi, phải review lại deletion contract trước khi chấp nhận phiên bản mới.

---

### Phase 3 — Retrieval, rewrite, cross-session và timing

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T3.1 — Gold seeding** | Seed bằng `infer=False`; mapping gold ID ↔ persisted ID; collection tách khỏi formed corpus. | Không gọi extraction model; không nhiễm user/run khác. | T1.6 | M |
| **T3.2 — Retrieval evaluator** | Hai mode rõ ràng: gold-seeded và formation-produced. Đo recall, precision, MRR, no-hit FP, cross-user leakage. | Gold mode dùng T3.1; formed mode dùng artifact từ T2.3. Không âm thầm thay formed corpus bằng gold. | T3.1; thêm T2.3 cho formed mode | M |
| **T3.3 — Score contract và diagnostic grid** | Khóa semantics: threshold áp lên semantic score trước hybrid ranking; returned score là hybrid. Chạy top-k `[1,3,5,10]`, threshold `[0,.1,.3,.5,.7]` với cùng embeddings. | Có contract tests cho direction/filtering; report không gọi hybrid score là cosine hoặc so threshold giữa hai embedding models. | T3.2 | M |
| **T3.4 — Rewrite evaluator** | Test Current > Recent > LTM, standalone/topic switch, ambiguity, KPI/date/location/formulas, injection và authorization. | Exact constraints được kiểm tra tự động; semantic verdict có review; rewrite không tự trả lời nghiệp vụ. | T3.2 | L |
| **T3.5 — Cross-session/ablation** | A formation → chờ đúng event → B retrieval/rewrite. Current-only, Recent-only, LTM-only, Recent+LTM là eval wiring. | Timeout readiness là dependency error; không sửa production flags để chạy ablation; query đang chấm không làm nhiễm corpus. | T2.3, T3.4 | L |
| **T3.6 — Timing instrumentation** | Ghi monotonic duration của formation, retrieval, rewrite, KiRa TTFT/completion, queue wait/readiness; tách SDK retries và Worker retries. | Mock kiểm tra timing/counts và percentile calculation. Chưa chạy workload performance thật hoặc chặn candidate. | T3.5 | S–M |
| **T3.7 — Offline handoff** | Eval images, PowerShell runbook, env templates và mock acceptance bundle; dependencies build trước trên laptop. | Validate và mock suites chạy được không tải dependency lúc chạy. | T3.6 | M |

Grid được dùng để chọn cấu hình trên **full corpus đã biết**. Candidate sau đó phải freeze trước confirmation runs. Đây là bằng chứng acceptance trên bộ hiện tại; muốn đo generalization phải có corpus độc lập sau này.

---

### Phase 4 — PC công ty: materialization, OpenAI acceptance và image handoff

PC là nơi duy nhất có KiRa thật trước K8s. OpenAI ở đây giúp phát hiện lỗi dataset/harness/provider
wiring nhưng **không** chọn candidate thắng thay cho internal models.

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T4.0 — PC bootstrap** | Clean checkout, `uv sync --frozen`, full tests, Docker/integration checks, env local ignored. | Source sạch; secret không vào Git; skipped dependency tests được chạy lại khi dependency có thật. | T3.7 | S |
| **T4.1 — KiRa materialization** | Plan → one-request preflight vào checkpoint chính thức → resume; preview apply rồi apply canonical. | Hoàn thành 80 unique queries, 140 fills và 54 pending answers của revision hiện tại; không gọi lại completed task, không commit raw checkpoint. | T4.0 | M |
| **T4.2 — Review/freeze dataset** | Human review KiRa fills/answers, gold/evidence, near-duplicate và blocking rows; freeze reviewer/revision/checksums. | Dataset `benchmark_ready`, mọi bundle materialized/frozen/reviewed, validator pass. | T4.1 | L |
| **T4.3 — Exact image set** | Build runtime + eval image cho historical control và từng declared candidate; ghi source/prompt/config/package/image digests. | Không hard-code bốn images: exact set có 2 images cho control và 2 cho mỗi candidate; mọi reference immutable. | T4.2 | M |
| **T4.4 — Provider/preflight freeze** | Freeze KiRa/OpenAI extraction/rewrite/embedding/judge endpoint/model/dimension/timeout/retry; probe đủ bốn suites. | Preflight sanitized PASS; không inherited provider fallback; không raw credential/payload trong artifact. | T4.3 | S–M |
| **T4.5 — Full-corpus PC acceptance** | Chạy control và declared candidates bằng KiRa thật + OpenAI, audit semantic output theo T1.3. | Full corpus kết thúc; không unresolved dependency/protocol error; zero safety hard fail; dataset/harness không có lỗi blocking. Metrics chỉ diagnostic, không promotion. | T4.4 | L |
| **T4.6 — VDI handoff bundle** | Bundle exact image set, registry/load manifest, dataset/review/provider/preflight/run/audit evidence và checksum. | Import verify offline đúng checksum/digest; raw secrets/responses không có trong bundle. | T4.5 | M |

---

### Phase 5 — K8s internal benchmark chính thức

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T5.1 — VDI publish/deploy** | Verify bundle rồi load/push exact images, deploy isolated benchmark namespace/database theo platform contract. | Digest đang chạy khớp manifest T4.6; không rebuild trên VDI/K8s. | T4.6 | M |
| **T5.2 — Internal preflight** | Probe internal extraction/embedding/rewrite/judge, KiRa, managed PostgreSQL/pgvector và runtime contracts. | Model/deployment/dimension/timeout/retry được freeze; mọi suite PASS trước full corpus. | T5.1 | S–M |
| **T5.3 — Discovery run** | Fresh database riêng cho từng control/candidate; chạy full corpus, audit và diagnostic grid. | Có scorecard formation P/R/F1, retrieval Recall@3/MRR, rewrite constraint+semantic, final-QA semantic+task success và zero safety hard fail. | T5.2 | L |
| **T5.4 — Tuning loop** | Nếu internal discovery phát hiện candidate cần chỉnh: quay về PC sửa/declaration → build digest mới → PC acceptance → VDI handoff → K8s preflight/discovery lại. | Chỉ candidate thắng discovery được freeze; không tune sau khi vào confirmation. | T5.3 | Variable |
| **T5.5 — Three-pair confirmation** | Ba paired repetitions control/candidate, fresh database, paired seeds/order; scorecard và human audit hoàn tất. | Primary metric aggregate tăng; ít nhất 2/3 repetitions cùng hướng; guardrail không giảm; With-LTM > No-LTM; task success không giảm khi observable; zero safety fail; unresolved evidence → insufficient. | T5.4 | L |
| **T5.6 — Late performance review** | Chỉ sau T5.5 PASS; đo stage bị candidate tác động với 5 warmups, 30 successes, cap 40 measured attempts/variant. | Report p50/p95 từ successes và toàn bộ error/timeout/retry; reviewer verdict `acceptable`, `reject_regression` hoặc `needs_more_samples`. Không auto-reject chỉ vì p50/p95 tăng khi chưa có threshold. | T5.5 | M |
| **T5.7 — Promotion/keep-control evidence** | Bind confirmation, performance review, exact image digests, cleanup và reviewer decision. | Promote chỉ khi T5.5 PASS + T5.6 `acceptable` + cleanup hoàn tất; nếu không thì `keep_control` hoặc `insufficient_evidence`, không weighted score tổng. | T5.6 | S–M |

Human audit ở T5 dùng một budget toàn cục khoảng 10% semantic PASS/FAIL stratified; audit toàn bộ
`UNCERTAIN` và deterministic/judge conflicts. Deterministic scorer không cần audit ngẫu nhiên.

Performance là guardrail vận hành chạy muộn, không được cứu semantic failure. Percentile chỉ lấy 30
success đầu; mọi measured attempt vẫn xuất hiện trong error/timeout/retry report.

---

### Phase 6 — Auth backend

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T6.1 — Auth schema** | Users, password hash, account status/lockout; opaque sessions với token hash, idle/absolute expiry và revocation. | Alembic migration và schema compatibility tests pass. | T0; thực hiện sau T3 trên laptop | M |
| **T6.2 — Auth services** | Argon2id qua `pwdlib`; random session token 32 bytes; hash token trong DB; CSRF token bind session; reset password revoke sessions. | Password verify/rehash, expiry, revoke và CSRF tests pass; không custom password crypto. | T6.1 | M |
| **T6.3 — Auth routes/identity** | Login, me, logout, change-password; principal lấy từ request session. Static/null identity chỉ cho dev/test. | Production không đi vào chat bằng anonymous/static identity; Origin/CSRF checks đúng. | T6.2 | L |
| **T6.4 — Admin CLI** | Create/disable/enable/reset-password/revoke-sessions/list. Password qua hidden prompt hoặc stdin. | Disable/revoke có hiệu lực ở request tiếp theo; logs không có secret. | T6.3 | S |

Defaults:

- Username case-insensitive, 3–64 ký tự.
- Password 12–128 ký tự.
- Khóa 15 phút sau 5 lần đăng nhập sai.
- Session absolute TTL 8 giờ, idle TTL 2 giờ; touch tối đa mỗi 5 phút.
- Production cookie: `__Host-kira_session; Secure; HttpOnly; SameSite=Strict; Path=/`, **không có Domain**.
- Local HTTP dùng cookie dev riêng; production từ chối cấu hình cookie không an toàn.
- Không public signup, email reset, MFA hoặc SSO trong release đầu.

---

### Phase 7 — Conversation và durable chat API

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T7.1 — Conversation management** | Server sinh public `session_id`; thêm title, activity timestamp, `active/deletion_pending`; create/list/history methods. | Ownership bắt buộc theo user; cursor và sort dùng cùng `COALESCE(last_message_at, created_at) DESC, conversation_id DESC`. | T6.4 | M |
| **T7.2 — Conversation routes** | Create/list/history/delete entrypoint; deletion pending không nhận chat mới và không trả history. | Two-user isolation; pagination; empty/new conversation; deletion state tests. | T7.1 | M |
| **T7.3 — Idempotency reservation** | `chat_requests` có client ID, content hash, turn ID, status, attempt/lease token. Unique client ID trong conversation và partial unique processing request/conversation. | Concurrent submissions chỉ có một owner; expired attempt được reclaim an toàn; late completion từ owner cũ bị từ chối. | T7.1 | L |
| **T7.4 — Atomic completion/SSE** | Persist user/assistant pair, optional memory job, request completed và activity timestamp trong một transaction. Emit completed sau commit. | Crash/rollback không tạo completed request thiếu history; completed replay không gọi KiRa lại; cancellation không lưu partial assistant turn. | T7.3 | L |
| **T7.5 — Limits/errors/observability** | Message tối đa 8.000 ký tự; body/rate/concurrency limits; sanitized error codes; trace/log gắn các app IDs. | KiRa/DB outage, persistence failure và telemetry outage tests pass; IDs/content không thành metric labels. | T7.4 | M |

Rate/concurrency guard của MVP là bounded state theo từng Gateway process. Nếu pilot cần nhiều
Gateway replicas và global quota chính xác thì mới thay bằng coordinator dùng chung; không đưa Redis
vào single-replica MVP chỉ để rate limit.

Các interface mới:

```text
POST   /api/v1/auth/login
GET    /api/v1/auth/me
POST   /api/v1/auth/logout
POST   /api/v1/auth/change-password

POST   /api/v1/conversations
GET    /api/v1/conversations
GET    /api/v1/conversations/{session_id}/messages
POST   /api/v1/conversations/{session_id}/messages
DELETE /api/v1/conversations/{session_id}
```

Message request chứa `client_message_id` và `message`.

- Completed duplicate: replay persisted result.
- Processing duplicate: `409 REQUEST_IN_PROGRESS`.
- Cùng ID khác content: `409 IDEMPOTENCY_CONFLICT`.
- Failed/cancelled: explicit retry có thể reclaim atomically.
- Sau crash ở thời điểm chưa biết KiRa đã xử lý hay chưa, không tuyên bố exactly-once provider execution; bảo đảm không duplicate completed turn trong DB.

SSE contract:

```text
message.started
message.delta
message.completed
message.failed
```

Legacy `/chat` giữ cho compatibility tests, production tắt. API sản phẩm chỉ append vào conversation đã tồn tại và còn active, không tự upsert tạo lại conversation vừa bị xóa.

---

### Phase 8 — Xóa conversation và associated memories

#### T8.0 — Khóa deletion contract bằng characterization tests

- Dùng kết quả T2.1 và bổ sung test qua application adapter.
- `conversation_id` là conversation của **formation operation tạo memory row**.
- B lặp fact của A không chuyển ownership sang B.
- Xóa A có thể làm mất fact mà B từng nhắc lại nhưng không tạo row mới; không rebuild.
- Không hứa truy ngược mọi ảnh hưởng gián tiếp đã được chép vào conversation/memory khác.
- Ghi ownership contract thành regression test khi nâng Mem0.

**Dependency:** T2.1, T7.2.  
**Acceptance:** hành vi ADD/dedup và ownership được xác nhận bằng native pipeline.  
**Effort:** S.

#### T8.1 — Quản lý dữ liệu phụ của Mem0

Chốt product runtime:

- PostgreSQL conversation store cung cấp bounded message window cho formation.
- Tắt auxiliary message/history cache của SDK trong product runtime bằng option rõ ràng, default native behavior của vendored package vẫn giữ cho control.
- Durable receipt tiếp tục phục vụ idempotency; không cần bản transcript/history thứ hai trong process Mem0.
- Thêm `conversation_id` vào formation receipt để xóa được cả empty receipts và receipts của jobs đã bị retention cleanup.
- Backfill receipt ownership từ job hoặc memory payload khi xác định được; dữ liệu không xác định không được âm thầm gán ownership.
- Không tự xóa/reset DB cũ. Nếu test DB có receipt không thể backfill, migration dừng và báo đúng record để xử lý có chủ đích.

Đây là **thay đổi runtime có thể ảnh hưởng prompt**, vì `Last k Messages` từ SDK không còn được bổ sung. Candidate/release manifest phải ghi nhận và các suite formation/retrieval/cross-session phải kiểm tra runtime này trên PC.

**Dependency:** T8.0.  
**Acceptance:** product formation chỉ dùng message window được cấp và active existing memories; không giữ auxiliary transcript cache xuyên jobs.  
**Effort:** M–L.

#### T8.2 — Chặn memory đang bị xóa và chặn late writes

Chốt cho MVP: conversation tables và memory schema nằm trong **cùng PostgreSQL database**, có thể dùng các DB roles riêng.

- Search cho chat và existing-memory search của formation chỉ nhận memory có conversation owner còn active.
- Kiểm tra authoritative state ở storage boundary; Gateway kiểm tra lại trước ContextBuilder.
- Không xác minh được ownership/status thì không dùng phần LTM đó.
- Transaction persist receipt/vector phải khóa chia sẻ row conversation, kiểm tra đúng user và trạng thái active, rồi mới ghi.
- Mark pending/final delete dùng row lock xung đột trên chính conversation đó.
- Lock chỉ giữ trong DB transaction; không giữ connection qua toàn bộ LLM/embedding call.
- Thread ghi DB chạy muộn sau timeout vẫn phải qua cùng kiểm tra transaction. Conversation pending hoặc đã mất thì không được tạo vector/receipt.
- Worker xử lý kết quả source đã bị xóa như cancelled/skipped, không đưa vào vòng retry provider vô ích.
- Entity-link writes phải bỏ các liên kết tới memory không còn tồn tại/không cùng user; không được tái tạo orphan links sau delete.

**Dependency:** T8.1.  
**Acceptance:** test provider blocked, persist thread blocked, timeout, concurrent delete và late completion đều không làm memory xuất hiện lại.  
**Effort:** L.

#### T8.3 — Idempotent deletion transaction

Flow endpoint:

1. Kiểm tra ownership và commit `deletion_pending`.
2. Từ thời điểm này, request mới không được dùng history/memory của conversation.
3. Trong một transaction, khóa conversation và:
   - xóa toàn bộ memory rows theo `user_id + conversation_id`;
   - xóa receipts theo cùng ownership;
   - bỏ entity links tương ứng, xóa entity rows không còn linked memories;
   - xóa conversation, cascade messages/jobs/chat requests.
4. Commit thành công trả `204`.

Quy tắc:

- Dùng relational filtering đầy đủ; không dùng semantic search/top-k để tìm records cần xóa.
- Xóa lại conversation đã không còn trả `204`, không tiết lộ dữ liệu user khác.
- DB lỗi hoặc lock timeout: giữ pending, trả `503 DELETION_RETRY_REQUIRED`.
- User retry cùng `DELETE`; admin có CLI `conversation purge-pending`.
- Không có erasure queue/service mới.
- Request đang stream trước khi delete có thể đã dùng context cũ; không tuyên bố thu hồi dữ liệu đã gửi. Nó không được persist lại vào conversation đã pending/deleted.

**Dependency:** T8.2.  
**Acceptance:** delete trên số lượng memory lớn hơn list limit vẫn xóa hết; rollback/retry đúng; không ảnh hưởng user hoặc conversation khác.  
**Effort:** M–L.

---

### Phase 9 — Frontend riêng

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T9.1 — Scaffold** | React/TypeScript/Vite/pnpm, React Router, strict types, Vitest/RTL/Playwright; routes login/new-chat/conversation. | Lint, typecheck, unit tests và build pass. | Contract T6–T7 | M |
| **T9.2 — Auth UI** | Login/logout/change-password, `/api/v1/auth/me` rehydration, session cookie và CSRF handling. | Không lưu auth token trong localStorage; unsafe methods gửi CSRF token; 401/revoke/expiry có UX đúng. | T9.1, T6.3 | M |
| **T9.3 — Conversation UI** | Sidebar, create/list/open, cursor history, delete confirmation, `deletion_pending` và retry UX. | Keyboard/accessibility cơ bản; isolation, pagination và pending delete E2E pass. | T9.2, T7.2, T8.3 | L |
| **T9.4 — Streaming UI** | Native fetch POST-SSE parser, AbortController stop/cancel, stable client message ID, completed replay và unsaved-answer state. | Chunk splitting, stop, retry, disconnect và persistence failure không tạo duplicate bubbles/turns. | T9.3, T7.4 | L |
| **T9.5 — Production image** | Multi-stage build, Nginx, same-origin API, security headers, cache hashed assets; Markdown không render raw HTML. | Container build và browser smoke pass; không hardcode backend host. | T9.4 | M |

Chưa thêm attachments, voice, admin web console, shared conversations hoặc collaborative editing.

---

### Phase 10 — Local product acceptance và CI

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T10.1 — Compose product stack** | PostgreSQL, migration, memory-init, Gateway, Worker, frontend và explicit KiRa/rewrite/embedding/memory-LLM mocks. Observability profiles riêng. | Laptop chạy đủ formation/cross-session flows; không cần external provider. | T8.3, T9.5 | M |
| **T10.2 — Product E2E** | Auth, history, stream, retry, cancel, memory formation/retrieval, delete, restart và dependency outages. | Toàn bộ MVP gate bên dưới pass với explicit mocks. | T10.1 | L |
| **T10.3 — CI** | Backend lint/format/tests/coverage, dataset validation, harness mocks, Postgres integration, frontend checks, Playwright smoke và image build. | PR regress contract/schema/UI bị chặn; current repository coverage gate `>=90%` được giữ; CI không gọi KiRa/model nội bộ hoặc benchmark chất lượng. | T10.2 | M |

Startup tuần tự:

```text
PostgreSQL → migrations/memory-init → provider mocks
→ Gateway/Worker → frontend → observability profile nếu cần
```

Prometheus/Grafana và Langfuse chạy độc lập theo nhu cầu test; không phải dependency readiness của ứng dụng.

---

### Phase 11 — Deployment production tối thiểu

| Task | Thay đổi chính | Acceptance | Phụ thuộc | Effort |
|---|---|---|---|---|
| **T11.1 — Platform manifests** | Dùng manifest format platform công ty bắt buộc cho Gateway/Worker/frontend, Services, migration/init Jobs, Ingress, config/secrets references, probes, resources và NetworkPolicy. Chỉ dùng/khôi phục Helm nếu platform thực sự yêu cầu. | Render/validation pass; cài mới và upgrade namespace pilot thành công. | T10.3 | L |
| **T11.2 — Database/TLS/operations** | Cùng PostgreSQL DB cho app+memory, pgvector, persistent storage, TLS và secrets; migration lock; backup/restore runbook. | Restore thật sang DB khác và kiểm tra conversation/jobs/memories/auth schema. | T11.1 | L |
| **T11.3 — Observability wiring** | Message request v1 → event ID → Worker attempt → Mem0; Collector export fail-open, structured stdout, masking. | Search được correlation/turn/event/origin-trace IDs; backend telemetry outage không làm business/readiness fail. | T11.1 | M |
| **T11.4 — Company pilot** | Real KiRa/model/embedding, browser acceptance, user isolation, deletion races, restart, backup/restore, image rollback và resource observation. | Production gate bên dưới đạt; runtime SHA/config khớp evidence. Benchmark chỉ rerun khi product change ảnh hưởng formation/retrieval/rewrite/memory semantics. | T5.7, T10.3, T11.2–T11.3 | L |

PostgreSQL và Langfuse được vận hành như dependency riêng, không nhét toàn bộ vào application chart.

Rollback app image chỉ được thực hiện khi schema tương thích ngược. Không tự downgrade production schema. Giữ Prometheus compatibility trong giai đoạn chuyển tiếp; chỉ bỏ khi đã xác nhận OTel metric parity và không còn consumer phụ thuộc.

---

## 3. Test gates và định nghĩa hoàn thành

### Harness-ready trên laptop

- Compiler/scorer/review/resume deterministic.
- Run isolation và cleanup ownership đúng.
- Native extraction characterization đã có.
- Mock profiles không tạo semantic quality claims.
- Đủ CLI, images và runbook để PC chạy không cần viết thêm harness.

### MVP-ready trên laptop

- Admin tạo tài khoản; login/logout/reset/revoke hoạt động.
- Tạo/mở/list conversation, load history và SSE.
- Concurrent/completed retry không tạo duplicate completed turn.
- Stop/disconnect không persist partial response.
- Transaction completion rollback toàn bộ khi có lỗi.
- Formation và cross-session retrieval chạy bằng đầy đủ provider mocks.
- Delete xóa associated memories/receipts, chặn pending retrieval và late writes.
- User isolation, restart và telemetry outage tests pass.
- CI xanh, coverage backend giữ ≥90%.

### Production-ready tối thiểu

- Có internal benchmark report và quyết định được review.
- Kiểm tra đúng runtime phát hành, gồm cấu hình cache/ownership mới.
- Real KiRa acceptance từ frontend pass.
- TLS/secrets, migrations, backup/restore và rollback được kiểm chứng.
- Trace nối message request với Worker/Mem0.
- Có runbook vận hành, xử lý dead jobs và retry pending deletions.
- Các hạn chế full-corpus, latency sample size và creation-owner deletion được ghi rõ.

---

## 4. Branch, dependencies và handoff

- Tiếp tục harness trên `feat/benchmark`, cập nhật từ `main` mà không rewrite history.
- Tạo `feat/product-mvp` từ baseline đã có harness khi bắt đầu T6; không cần đợi PC benchmark.
- Historical control luôn chạy từ SHA đã pin; product work không thay đổi source của control.
- Trước official run, freeze product runtime và candidate manifests. Mọi thay đổi T8 ảnh hưởng formation phải nằm trong runtime được kiểm tra.
- Một task hoặc một nhóm thay đổi nhỏ có cùng mục tiêu là một commit; schema, runtime behavior và mechanical formatting tách commit.
- T11 dùng nhánh deployment riêng nếu platform yêu cầu; không mặc định phải là Helm.
- “Giữ control” là quyết định benchmark/prompt-config; không phải reset toàn bộ repo về code cũ và mất auth/deletion fixes.

---

## 5. Giới hạn và effort

Không thêm provenance graph, memory reconstruction, erasure queue, automatic expiration/supersession, SSO, Redis/message broker, WebSocket, Loki, HPA hoặc HA ở release đầu.

T8 được giới hạn ở ownership của memory row. SQL transaction checks, receipt ownership và cache handling là phần cần thiết để đảm bảo xóa không bị Worker ghi ngược lại; không mở rộng thành hệ thống theo dõi mọi nguồn thông tin.

Ước lượng một người:

| Nhóm công việc | Effort dự kiến |
|---|---:|
| T0–T3: harness và laptop handoff | 8–12 ngày |
| T4: PC materialization/OpenAI acceptance/handoff | 3–6 ngày thao tác |
| T5: K8s internal discovery/confirmation/promotion | 3–7 ngày + tuning loop |
| T6–T8: auth, durable chat, deletion | 7–11 ngày |
| T9–T10: frontend, Compose, CI | 6–9 ngày |
| T11: deployment và pilot | 4–7 ngày |

Đây là ước lượng kỹ thuật, chưa gồm thời gian chờ endpoint/quyền truy cập và human review.

Việc một phần tooling đã được triển khai không tự đánh dấu live task là PASS; trạng thái phải dựa
trên artifact/acceptance tương ứng.
