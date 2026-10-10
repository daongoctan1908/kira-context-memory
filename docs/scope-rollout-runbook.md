# Scope rollout runbook (CONVERSATION | GLOBAL)

Runbook triển khai feature memory scope (payload `memory_scope` + hai-branch
retrieval + backfill legacy rows) lên môi trường có dữ liệu thật. Runbook này
bổ sung cho `docs/production-deployment.md` — mọi quy tắc nền (immutable image
digests, secret handling, backup/restore acceptance) vẫn áp dụng nguyên văn.

**Artifact bắt buộc trước khi bắt đầu**: `tests/integration/postgres/test_scope_backfill_probe.py`
PASS trên cùng major version pgvector với production (kết luận Phase 3:
in-place `jsonb_set` an toàn — identity, vector, hash, provenance, receipt,
entity links đều giữ nguyên; rollback sạch; idempotent). Nếu artifact không khớp
version pgvector production, chạy lại probe trên disposable container với đúng
version trước khi đụng dữ liệu thật.

## Phase A — Freeze writes

1. Scale Worker xuống 0 (không job formation nào ghi mới) và chặn Gateway
   traffic theo cơ chế platform (Ingress maintenance page / scale Gateway = 0).
2. Xác nhận không còn job pending/processing:

   ```bash
   # psql với MEMORY_DATABASE_URL admin role
   SELECT status, count(*) FROM memory_jobs GROUP BY status;
   ```

   Chỉ chấp nhận `completed`/`dead` khác 0; `pending`/`processing` phải = 0.
   Đợi Worker hiện tại chạy nốt hoặc recycle pending jobs trước khi tiếp tục.
3. Ghi lại aggregate counts (KHÔNG log content) — baseline để đối chiếu sau:

   ```sql
   SELECT count(*) FROM <schema>.<collection>;                       -- tổng memory rows
   SELECT count(*) FROM <schema>.<collection>_formation_receipts;
   SELECT count(*) FROM <schema>.<collection>_entities;
   ```

## Phase B — Backup

Bắt buộc trước thay đổi payload đầu tiên. Làm theo "Backup and restore
acceptance" trong `docs/production-deployment.md` (pg_dump custom format →
restore vào database khác → đối chiếu aggregate counts). Backfill chỉ sửa
payload của memory rows nhưng vẫn phải có restore point.

## Phase C — Verify deploy artifact khớp artifact probe

1. Xác nhận image deploy là image chứa scope feature (commit có
   `worker.memory_admin` subcommand `scope-inventory`/`scope-backfill`):

   ```bash
   python -m worker.memory_admin validate
   ```

   Phải PASS với schema version + dims đúng.
2. So sánh version pgvector của môi trường thật với container probe:

   ```sql
   SELECT extversion FROM pg_extension WHERE extname = 'vector';
   ```

   Khác major version → chạy lại probe (`POSTGRES_TEST_URL` trỏ tới disposable
   container version đó) trước khi tiếp tục.

## Phase D — Inventory (read-only, không ghi)

```bash
python -m worker.memory_admin scope-inventory
```

Đọc JSON report. Per row chỉ có `memory_id`, `user_id`, action per-field —
không có content. Counter cần quan tâm:

- `run_id_missing`, `run_id_mismatch`, `conversation_id_missing`,
  `owner_or_provenance_missing`, `memory_scope_missing/invalid`.
- Nếu `total_memories` = 0 mutation candidate → vẫn chạy Phase E (idempotent,
  apply = 0) để xác nhận đường lối.

Lưu report vào release evidence.

## Phase E — Backfill dry-run

```bash
python -m worker.memory_admin scope-backfill
```

Default là dry-run: in report + `scope backfill dry-run: updated=N`, KHÔNG ghi.
Exit code 1 nếu có unresolved rows.

## Phase F — Backfill apply (gated)

1. Điều kiện apply: **unresolved = 0** (command tự raise
   `LongTermMemoryConfigurationError` khi còn unresolved — gate nằm trong
   `_scope_backfill_apply_sync` với `allow_unresolved=False`, không phải flag
   CLI). Nếu unresolved > 0: xử lý từng row thủ công theo report (sai provenance
   → sửa dữ liệu nguồn; conversation đã xóa → quyết định kinh doanh), rồi re-plan.
   KHÔNG có flag force — đây là chủ đích.
2. Apply:

   ```bash
   python -m worker.memory_admin scope-backfill --apply
   ```

   In-place, 1 transaction toàn cục: `SELECT ... FOR UPDATE` → `jsonb_set`
   per-field → commit. Rows unresolved không bị đụng.
3. Re-run để chứng minh idempotent:

   ```bash
   python -m worker.memory_admin scope-inventory
   ```

   `run_id_missing` + `memory_scope_missing` phải = 0. `scope-backfill --apply`
   lần 2 phải báo `updated=0`.
4. Đối chiếu aggregate counts của Phase A — không được thay đổi (backfill chỉ
   UPDATE payload, không insert/delete row).

## Phase G — Deploy Gateway/Worker mới

1. Migration + memory-init Jobs theo đúng trình tự
   `docs/production-deployment.md` ("Migration and rollout").
2. Rollout Gateway + Worker với image scope feature. Flags production:

   ```text
   LTM_ENABLED=true
   MEMORY_FORMATION_ENABLED=true
   ```

## Phase H — Smoke tests (theo thứ tự, mỗi bước PASS mới đi tiếp)

Trước các bước có metric evidence, bật OTel trong Gateway/Worker và cấu hình Collector/backend
theo `docs/production-deployment.md`. `OTEL_ENABLED=true` cần đi cùng endpoint và đường mạng
đã hoạt động. Dùng endpoint `/metrics` của Collector ở port `8889` hoặc Prometheus đã scrape
endpoint đó; Gateway/Worker không còn scrape endpoint riêng. Với stack local, bật overlay trước
smoke:

```powershell
docker compose -f compose.product.yaml -f compose.observability.yaml up -d --build --wait
(Invoke-WebRequest http://127.0.0.1:18889/metrics).Content | `
  Select-String 'kira_memory_branch_count_total|kira_memory_scope_count_total'
```

Prometheus/Grafana chỉ cần profile `metrics` khi muốn query lịch sử/dashboard. Xác nhận một
metric đã biết thay đổi sau lượt thử nghiệm để chứng minh export hoạt động; không coi mọi
metric vắng mặt là zero khi chưa kiểm tra đường telemetry.

Thực hiện bằng 2 user thử nghiệm (`user-a`, `user-b`), mỗi user 1 conversation:

1. **Current-branch recall**: user-a nói 1 fact conversation-local trong conv A
   → hỏi lại trong conv A → phải trả lời đúng. Kiểm tra span/metric:
   `kira_memory_branch_count_total{branch="conversation",outcome="success"}` tăng.
2. **Global cross-conversation**: user-a nói 1 preference bền vững (LLM phải
   classify GLOBAL — kiểm tra persisted payload có `memory_scope="GLOBAL"`) →
   conversation B mới của user-a hỏi cùng chủ đề → phải trả lời được từ
   `branch="global"`.
3. **Isolation**: user-b hỏi cùng câu trong conv của user-b → KHÔNG được thấy
   memory của user-a (cả 2 branch).
4. **Partial failure**: dừng embedding/LLM provider của Worker trong khi chạy
   formation → job phải retry, không mất message; Gateway search với 1 branch
   lỗi phải fail-open dùng branch còn lại
   (`kira_memory_branch_count_total{...,outcome="error"}` tăng nhưng chat vẫn đáp).
5. **Deletion**: user-a xóa conversation A → memory local của A biến mất khỏi
   cả 2 branch (global memory gắn conversation nguồn cũng bị xóa cùng —
   `_delete_owned_memory` theo `user_id + conversation_id`).
6. **Scope distribution**: đọc metrics Worker đã export qua Collector —
   `kira_memory_scope_count_total{scope="CONVERSATION",origin="fallback"}` tăng khi
   LLM bỏ qua scope, `origin="invalid"` tăng khi candidate bị drop; cả hai
   không được tăng trong smoke có prompt v6 hoạt động đúng. OTel counter chưa từng được ghi
   có thể chưa xuất hiện; chỉ coi là zero sau khi đã xác nhận export hoạt động như trên.

## Phase I — Mở traffic

Gỡ maintenance/chặn, scale Worker + Gateway về mức thường. Theo dõi 24h đầu:

- `kira_memory_branch_count_total{outcome="error"}` rate — phải ~0;
- `kira_memory_scope_count_total{origin="invalid"}` — tăng liên tục nghĩa là prompt
  scope đang sai enum, cần review extraction prompt;
- `kira_context_degraded_count_total{operation="memory_search"}` — tăng bất thường
  nghĩa là cả hai branch lỗi thường xuyên.

## Rollback

1. **Chưa apply backfill** (Phase ≤ E): rollback image về bản trước scope.
   Legacy rows không có `run_id`/`memory_scope` vẫn hoạt động với image cũ
   (filter `user_id`-only). An toàn.
2. **Đã apply backfill** (Phase ≥ F): rollback image về bản trước scope vẫn
   chạy được — rows đã backfill có thêm field mới trong payload nhưng image cũ
   không đọc chúng (filter `user_id`-only match cả 2). Không cần undo payload.
   Không chạy script "un-backfill" — `memory_scope`/`run_id` đã thêm là hợp lệ
   vĩnh viễn với cả image mới.
3. **Sau khi có memories GLOBAL mới**: rollback image cũ làm mất khả năng
   truy cập global memories (branch global không được query nữa) nhưng không
   mất dữ liệu. Restore-backup chỉ dùng khi payload corruption — xử lý như
   "Backup and restore acceptance", quyết định recovery riêng.

Không downgrade Alembic, không reset memory schema (quy tắc
`docs/production-deployment.md` giữ nguyên).
