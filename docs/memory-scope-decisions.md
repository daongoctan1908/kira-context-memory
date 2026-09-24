# Memory scope decisions — Phase 0 baseline & comparability

**Ngày**: 2026-09-23. Plan: `C:\Users\tandn16\.claude\plans\gentle-mixing-robin.md` (đã duyệt, vòng 3).
Scope feature: CONVERSATION | GLOBAL | DROP — memory xuyên conversation, không project.

## T0.1 — Formation baseline (đã đo, code @ 8f372ec)

Chạy green trước khi đụng fork:

- `tests/vendor/test_mem0_pristine_contract.py` + `tests/vendor/test_mem0_extraction_pipeline.py` — **31 passed**.
- `tests/unit/infrastructure/memory/test_mem0_adapter.py` — **21 passed** (trong file này) + full file list.
- `tests/unit/application/test_process_memory.py` — **21 passed**; `test_process_memory_job.py` — **28 passed**.

Baseline provider-call count / formation job (từ test_mem0_extraction_pipeline.py):

- LLM extraction: **đúng 1 call** (`llm.generate_response.assert_called_once`, test_mem0_extraction_pipeline.py:84).
- Embedding: **1 embed(query, "search")** cho existing-memory lookup (:68, `parse_messages(raw)`) + **1 embed_batch(texts, "add")** cho candidates khi có facts.
- Writes: vector_store insert qua `insert_with_formation_receipt` — **1 receipt row / event**; replay bypass providers (llm + embed không gọi lại, :84-85).
- Vector store: 3 inserts khi 3 facts (test_mem0_pristine_contract.py:164 — `insert.call_count == 3`).

## T0.2 — Raw-score comparability giữa 2 branch (đã đo, và ghi nhận rủi ro)

Decoded từ `score_and_rank` (mem0/utils/scoring.py:60-139):

- `combined = (semantic + bm25 + entity_boost) / max_possible` (scoring.py:118-119).
- Divisor `max_possible`: 1.0 / 1.5 / 2.0 / 2.5 tùy `has_bm25`/`has_entity` (scoring.py:94-101) — phụ thuộc candidate population của branch.
- **Kết luận đo được**: cùng query, branch A có BM25 hit (divisor 2.0) và branch B không (divisor 1.0) → cùng semantic score s → combined A = s/2.0 < s/1.0 = combined B. Raw-score merge giữa 2 branch **có thể thiên lệch** về branch không có BM25/entity hit.
- **Quyết định giữ raw-score merge** (user 2026-09-23: "merge = raw-score"): chấp nhận thiên lệch divisor; RRF là fallback nếu benchmark Phase 5 chứng minh sai lệch thực tế ảnh hưởng Recall@3/MRR.
- Fix C (T2.2b) loại 1 phần rủi ro: divisor entity chỉ tăng khi boost id thực sự trong candidate set (giao keys ∩ candidates), không còn tăng ảo từ entity ngoài branch.
- Characterization tests: `tests/vendor/test_score_divisor_comparability.py` — 4 case khóa behavior pre-fix-C: (1) không tín hiệu phụ → score = semantic nguyên; (2) BM25 hit score 0.0 vẫn kích hoạt divisor 2.0 (dict truthiness, không phải score > 0); (3) boost id NGOÀI candidate set vẫn tăng divisor 1.5 (đây là bằng chứng pre-fix C của ISSUE ranking influence — mục 9 plan); (4) boost id trong candidate → (sem + boost)/1.5 đúng.

## T0.3 — Telemetry / receipt replay / deletion baseline (ghi nhận, không sửa)

- Receipt replay: ON CONFLICT (event_id) DO NOTHING (pgvector.py:559-566); replay trả committed result, bypass providers. Test hiện có: tests/vendor/test_mem0_extraction_pipeline.py:81-85.
- Deletion auto-covers global: `_delete_owned_memory` xóa theo `payload->>'user_id' AND payload->>'conversation_id'` (app/infrastructure/postgres/conversation_store.py:502-504); late-write blocking `_require_active_owner` + `InactiveMemoryOwnerError`. Test hiện có: tests/integration/postgres/test_memory_owner_fencing.py.
- Telemetry ON/OFF: `MEM0_TELEMETRY=False` patch trong tests; production telemetry qua observer spans (mem0.persist / mem0.extract.parse / mem0.memory.embed — main.py:2796-2817, 2773, 2821-2841).
- **Không sửa gì trong T0.3.**

## Fix C record (T2.2b, áp dụng khi Phase 2)

`score_and_rank` (mem0/utils/scoring.py:94-101): `has_entity` sẽ tính theo giao `entity_boosts.keys()` ∩ candidate ids thay vì `bool(entity_boosts)` — loại ranking influence qua divisor từ entity ngoài candidate set. 1 điểm sửa phủ sync (main.py:1772) + async (~3585) vì chung hàm.

## T0.5 — Scope Gold Preparation (BLOCKED chờ materialized.2 freeze)

Điều kiện: T4.2 (user điền artifacts/benchmark/dataset-review-decisions.json → review packet → freeze) hoàn tất. Trước khi bắt đầu T1.1.
