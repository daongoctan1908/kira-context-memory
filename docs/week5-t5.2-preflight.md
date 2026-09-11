# Week 5 T5.2 — Eval contracts và provider preflight

T5.2 đã triển khai typed case/config/result và preflight theo suite. Ba probe OpenAI thật đã
pass với key trong `.env.week5.local`: extraction JSON, rewrite chat và embedding batch.
PostgreSQL live chưa chạy được; Docker Linux engine unavailable tại thời điểm kiểm tra và
file cấu hình Week 5 chưa có database URI. Những dependency này vẫn là `NOT_RUN`.

Preflight chỉ chứng minh protocol và dependency được cấu hình đúng. Chất lượng native extraction,
retrieval, rewrite và cross-session sẽ được chấm bằng corpus/review trong các task tiếp theo.

## Components

| Component | Trách nhiệm |
| --- | --- |
| `evaluation/models.py` | Case version 1 với discriminated inputs cho bốn suite; evidence/gold/attribution/review; result/outcome và preflight report |
| `evaluation/config.py` | Load file được chỉ định và process env; riêng provider extraction/rewrite/embedding; validation, secret exclusion và non-secret config hash |
| `evaluation/providers.py` | Bounded HTTPX chat/JSON/embedding/health probes, fixed synthetic inputs, typed errors |
| `evaluation/postgres.py` | Async PostgreSQL read-only: connection, pgvector, migration revision và memory metadata/collection/receipt tables |
| `evaluation/preflight.py` | Chọn/deduplicate probes theo suite, deadline từng probe, aggregate readiness |
| `evaluation/mock.py` | HTTP/DB doubles offline; report ghi rõ simulated |
| `scripts/run_week5_benchmark.py` | CLI `preflight`, safe JSON output và exit codes |
| `evaluation/week5.env.example` | Template không chứa secret thật |

Evaluation chạy từ source checkout; optional extra `evaluation` khai báo trực tiếp `python-dotenv`.
Application wheel vẫn chỉ đóng gói `app` và `worker`. Eval image/packaging riêng thuộc T5.19.
Gateway/Worker/domain không import evaluation; app version và Mem0 package vẫn giữ baseline.

## Dependency matrix

| Suite / mode | Probes |
| --- | --- |
| `formation`, mặc định `write_free` | `extraction_json` |
| `formation --formation-mode persistent` | extraction, embedding, pgvector, memory schema, conversation DB/queue tables |
| `retrieval` | embedding batch, pgvector, memory schema |
| `rewrite` | `rewrite_chat` |
| `cross_session` | persistent formation dependencies, rewrite, Gateway `/ready`, Worker `/ready`, KiRa mock identity |

Probe được deduplicate khi chọn nhiều suite. Embedding discovery chạy trước memory schema:
metadata phải khớp model và actual dimension. Thiếu prerequisite khiến schema probe `NOT_RUN`
với `prerequisite_failed`; các probe độc lập vẫn tiếp tục. Không có DB thì rewrite/extraction
write-free vẫn chạy được.

DB connection dùng `default_transaction_read_only=on`, `statement_timeout=2000ms`, tổng deadline
của probe mặc định 15 giây. Không chạy migrations, tạo extension/schema hay ghi conversation,
job, vector hoặc receipt. Metadata memory cần schema `2`, `viettel.3`; application migration cần
`20260908_0003`. Metadata/table presence checks không thay thế integration test về transaction,
constraints hoặc chứng minh DB này an toàn để cleanup.

`kira_mock_identity` kiểm tra đúng marker của mock KiRa có sẵn. Probe này luôn ghi
`simulated=true`, kể cả dùng cùng OpenAI thật. Chưa có KiRa real business-success gate ở T5.2;
Gateway/Worker readiness cũng không thay thế việc quan sát formation job hoàn tất ở T5.15.

## Cách chạy

Tại repository root, cài dependency từ lock:

```powershell
uv sync --frozen --extra evaluation
```

Offline smoke, không đọc key hoặc gọi mạng:

```powershell
uv run --frozen python -m scripts.run_week5_benchmark preflight --profile mock --suite cross_session
```

OpenAI probes bằng đúng file người dùng đã điền:

```powershell
uv run --frozen python -m scripts.run_week5_benchmark preflight --profile external_synthetic --suite formation --suite rewrite --suite retrieval --env-file .env.week5.local --env-file-only --output artifacts/week5/preflight-local.json
```

Mỗi run tạo output file mới; dùng tên khác nếu đã tồn tại. Mặc định không retry; một run gồm
ba suite trên tạo tối đa 3 provider requests: một extraction, một rewrite, một embedding batch
gồm 2 fixed synthetic strings. Chọn riêng `--suite rewrite` chỉ tạo một chat request.

Exit `0`: mọi dependency của các suite đã chọn pass; `1`: có `NOT_RUN`, dependency hoặc protocol
failure; `2`: config/output/CLI execution error. `PASS` trong preflight là dependency pass,
không phải semantic benchmark pass.

## Cấu hình và credential

Thông thường process environment ghi đè file, phù hợp CI. Với local secret file, dùng
`--env-file-only`: chỉ đọc file đó, tránh `OPENAI_API_KEY` ambient ghi đè. Report ghi
`config_source`; không sửa process env hay file người dùng. Không tự đọc `.env` ứng dụng,
không interpolate `${VARIABLE}` trong file.

Shared external config: `OPENAI_API_KEY`, `WEEK5_OPENAI_BASE_URL`, `WEEK5_OPENAI_CHAT_MODEL`,
`WEEK5_OPENAI_EMBEDDING_MODEL`. `internal_test` cần `WEEK5_EXTRACTION_*`, `WEEK5_REWRITE_*`,
`WEEK5_EMBEDDING_*`; không kế thừa external endpoint/key. Mỗi endpoint override cần credential
riêng nếu profile external yêu cầu auth. Model IDs do config quyết định.

Optional knobs xem [template](../evaluation/week5.env.example). `WEEK5_EMBEDDING_DIMENSIONS` để
trống thì discovery; điền số thì gửi `dimensions` và đối chiếu response. `json_object` là mặc định
cho extraction preflight; `prompt_only` kiểm tra endpoint không có JSON mode một cách explicit.
Không tự fallback mode khi provider báo unsupported parameter.

Report chỉ giữ counts, model identifiers, latency, token usage nếu provider trả, config đã loại
secret và typed reason. API key/DB URI bị loại khỏi repr/serialization/hash; không dump response
body, prompt, fact text, vectors hoặc raw exception. Endpoint URL không nhận userinfo/query/fragment.
HTTP redirect tắt; response giới hạn 1 MiB và deadline 15 giây. DB URI bị loại khỏi hash nên hash
này không phải bằng chứng hai deployment dùng cùng DB; resource provenance cho benchmark sẽ cần
run manifest đầy đủ ở các task sau.

Windows CLI dùng scoped selector event loop cho psycopg async. Không thay global event-loop
policy của Gateway/Worker.

## Protocol checks

Chat: một choice, `finish_reason=stop`, assistant content không rỗng/oversized, không tool call
hoặc refusal. JSON extraction probe yêu cầu envelope native V3 `memory`, sequential string IDs,
plain fact text và `attributed_to` user/assistant; optional linked IDs được kiểm tra kiểu.
Fixed positive input cần ít nhất một fact. Đây là probe envelope ngắn, chưa chạy full native
extraction prompt/policy để đánh giá chất lượng như T5.6.

Embedding: `encoding_format=float`, đủ hai indices duy nhất 0/1 (cho phép response đảo thứ tự),
dimension đồng nhất, finite numeric values, không bool/empty/zero vector. Trả dimension thực đo
và model được provider báo. Timeout/connection/non-2xx → `DEPENDENCY_ERROR`; malformed/truncated/
refusal/invalid vectors → `PROTOCOL_ERROR`; thiếu config → `NOT_RUN` trước khi gửi request.

HTTP contracts đã đối chiếu [Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
và [Embeddings](https://developers.openai.com/api/reference/python/resources/embeddings/methods/create)
trong OpenAI Docs. Giữ JSON-mode contract đang test; chưa migrate Mem0 sang một response schema khác.

## Evidence ngày 2026-09-11

Live run `4b100329-0293-4a60-a28b-f32cad29d6ae`, bắt đầu `2026-09-11T16:44:55.631385Z`:

| Probe | Outcome | Provider evidence |
| --- | --- | --- |
| Extraction JSON | PASS | `gpt-4o-mini` → `gpt-4o-mini-2024-07-18`; 1 fact; 125 total tokens |
| Rewrite chat | PASS | `gpt-4o-mini` → `gpt-4o-mini-2024-07-18`; 68 total tokens |
| Embedding batch | PASS | `text-embedding-3-small`; 2 vectors × 1536 dimensions; 12 total tokens |
| pgvector / memory schema | NOT_RUN | Chưa có Week 5 database URI; Docker Linux engine chưa khả dụng |

Ba request thành công báo tổng 205 tokens; không suy ra chi phí khi provider không trả cost.
Lượt thử trước đó dùng inherited key khác trả 401; không dùng lượt đó làm evidence chất lượng.
Vì phát hiện conflict, CLI bổ sung `--env-file-only` và có regression test cho credential precedence.
Live result được giữ tại local `artifacts/week5/t5.2-openai-file-preflight-20260911.json` (Git ignored),
không ghi key vào tài liệu. Artifact được tạo trước lần bổ sung metadata `config_source` và
per-probe `simulated`; bảng này mô tả đúng execution đó, không sửa artifact hoặc gọi lại có phí chỉ
để cập nhật serialization.

Validation cuối:

- Full suite: **735 passed, 70 skipped**, không failure/error; coverage **93,62%** tính cả
  app, Worker, evaluation và CLI. Trong đó có 106 unit test T5.2; evaluation + CLI coverage
  **99,51%** (615/618 statements).
- Unit/contract coverage gồm credential precedence/exclusion/reflection, suite dependency selection,
  JSON/embedding malformed payload, timeout/cancellation, no retries/redirects, read-only DB SQL,
  schema mismatch, report/output behavior và Windows-compatible CLI runner.
- Ruff lint toàn repository pass. Strict LF format check trên files mới pass. Root format check
  pass với `format.line-ending="auto"` vì baseline Windows checkout đang có CRLF; không rewrite
  hàng loạt baseline files chỉ để đổi newline.
- `uv lock --check`, wheel build và `git diff --check` pass. Dependency versions runtime không đổi;
  lock chỉ thêm metadata optional extra cho dependency `python-dotenv` đã được resolve.
- PostgreSQL opt-in test và các live integration gates chưa cấu hình được skip rõ. Không có
  claim PostgreSQL live hoặc Docker build mới đã pass ở T5.2.
- Real key không xuất hiện trong các file thay đổi; `.env.week5.local` và `artifacts/` tiếp tục
  được Git ignore. Evidence local: `artifacts/week5/t5.2-final-tests.xml` và
  `artifacts/week5/t5.2-final-coverage.json`.

## Phần tiếp theo

T5.3 tạo corpus synthetic có gold draft, T5.4 xử lý validator/split và T5.5 bổ sung scoring/review.
T5.2 không tạo corpus hoặc semantic score. PostgreSQL live test có opt-in bằng `POSTGRES_TEST_URL`
trỏ DB disposable khi hạ tầng khả dụng; test mặc định skip rõ nếu chưa cấu hình.
