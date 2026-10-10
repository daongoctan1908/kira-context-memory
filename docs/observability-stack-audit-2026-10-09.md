# Rà soát mức cần thiết của stack observability

Ngày rà soát: 2026-10-09. Phạm vi: code Gateway/Worker, instrumentation OTel và
Prometheus cũ, Collector, các overlay Compose, dashboard/rules, kế hoạch production,
bộ YAML K8s và inventory VDI đã cung cấp. Đây là audit và đề xuất; không xóa
instrumentation/dịch vụ trong bước audit ban đầu. Phần quyết định bên dưới ghi lại
thay đổi source sau phản hồi của người dùng; chưa apply lên cụm.

## Quyết định sau audit

Người dùng chọn giữ Langfuse vì có chức năng riêng, tài nguyên không phải lý do
để cắt chức năng. Bộ YAML tại `deploy/k8s/observability/` triển khai Web/Worker và
đủ PostgreSQL, ClickHouse, Redis, S3 cho Langfuse. Collector có một đích trace là
Langfuse; metrics đi vào một Prometheus và được xem trên Grafana. Có hai mode
`standalone` và `shared` để chọn bản riêng hoặc monitoring chung đã xác minh,
không dựng hai hệ thống cùng lưu/evaluate signal KiRa.

Đã bỏ registry `prometheus_client`, dual-write và hai route `/metrics` cũ của
Gateway/Worker; giữ OTel instruments, trace, correlation và Worker queue sampler
phục vụ readiness. Tests đọc dữ liệu qua OTel InMemoryMetricReader. Mỗi Pod gửi
`service.instance.id` bằng Pod UID để tách counter/histogram giữa replica.
Loki chưa có pipeline trong dự án nên vẫn nằm ngoài bộ triển khai lần này.
Các bảng/phát hiện audit phía dưới mô tả trạng thái trước khi dọn code.

## Kết luận

Không cần bê nguyên chín dịch vụ observability local lên Kubernetes để chạy KiRa.
Phần tăng số dịch vụ lớn nhất là Langfuse tự host: Web + Worker + PostgreSQL +
ClickHouse + Redis + object storage. Bốn data store phục vụ các chức năng khác nhau;
không thể bỏ từng store rồi mong Langfuse vẫn hoạt động tương đương.

Đề xuất cho pilot vận hành: giữ instrumentation, dùng một Collector, ưu tiên
Prometheus/Grafana của cụm, dùng stdout JSON và hệ thống log platform nếu đã có.
Langfuse là lựa chọn khi cần tra timeline từng request/job và các bước AI; Loki
riêng hoãn ở release đầu. Chưa có trace backend thì không có lịch sử trace/AI UI
lâu dài. Đây là đánh đổi chức năng, không phải tối ưu mà giữ nguyên mọi khả năng.

Inventory có bằng chứng đáng kiểm tra trước khi dựng monitoring riêng:
`cattle-dashboards` mang nhãn release/chart `rancher-monitoring` và namespace
`cattle-monitoring-system`. Chưa có thông tin Pod/Service, scrape selector, quyền
Grafana hoặc backend log để xác nhận có thể tích hợp ngay.

## Vai trò và mức cần thiết của từng thành phần

| Thành phần | Chức năng thực tế trong dự án | Đề xuất |
| --- | --- | --- |
| OTel SDK trong Gateway/Worker | Timing/outcome, model/token metadata, trace request → job → formation, optional masked I/O; export fail-open | Giữ. Đây là thư viện trong app, không phải một service K8s riêng |
| OTel Collector | Nhận OTLP, batch/giới hạn bộ nhớ, lọc trace rỗng, route traces và expose metrics | Giữ một instance cho luồng hiện tại; có thể dùng Collector platform khi cấu hình và quyền cho phép |
| Prometheus | Lưu operational metrics và evaluate bốn alert rule | Cần chức năng này để xem xu hướng/cảnh báo; ưu tiên instance platform thay vì bản riêng cho KiRa |
| Grafana | Dashboard tám panel metrics, chỉ có datasource Prometheus | Không cần thêm instance nếu có Grafana dùng chung; import dashboard của repo |
| Langfuse Web/Worker | Trace timeline, model/token usage, liên kết request/job; optional AI I/O đã mask | Hữu ích khi điều tra chất lượng retrieval/rewrite/formation, nhưng không cần để app/benchmark chạy |
| PostgreSQL của Langfuse | Metadata/user/project/config transactional của Langfuse | Cần database/role riêng nếu giữ Langfuse; không nhất thiết cần một server PG riêng |
| ClickHouse của Langfuse | Lưu/query observations và trace analytics | Cần nếu giữ Langfuse; không thay bằng PostgreSQL/pgvector của KiRa |
| Redis của Langfuse | Queue/cache phục vụ ingestion của Langfuse | Cần nếu giữ Langfuse; không phải queue memory của KiRa. Không thêm Redis cho app, không mặc định dùng Redis của CI |
| MinIO/S3 | Blob/raw-event storage của Langfuse | Cần chức năng object storage; Pod MinIO mới có thể bỏ nếu có endpoint S3 nội bộ và bucket/credential riêng |
| Loki và log agent | Centralized log history/search, mới ở phần thiết kế | Hoãn hệ thống riêng ở release đầu; kế hoạch production hiện cũng loại Loki khỏi release này |
| Tempo/Jaeger | Một trace store/UI khác | Không có trong repo và không cần thêm khi đã chọn Langfuse làm trace store |

Trong repo chưa thấy app/evaluation dùng Langfuse prompt management, datasets,
scoring hoặc online evaluators. Giá trị đang dùng là trace/debug. Điều đó không
làm Langfuse vô dụng, nhưng chưa đủ lý do triển khai cả hệ thống nếu pilot chỉ
cần theo dõi lỗi, latency và queue. Benchmark vẫn có verdict/denominator và audit
riêng; không bỏ evaluation vì Langfuse cũng có tính năng evaluation.

## Chỗ trùng thực sự và chỗ nhìn giống nhau nhưng khác chức năng

1. **Metrics cũ và OTel ghi song song trong app.** `ContextTelemetry` và
   `MemoryJobTelemetry` có cả registry `prometheus_client` lẫn OTel facade. Các
   test parity chứng minh một callback cập nhật cả hai. Đây là lớp chuyển đổi có
   chủ đích, nhưng không nên giữ hai bộ lâu dài nếu consumers đã chuyển hết.
   Hiện Prometheus chỉ scrape Collector `8889` và self-metrics `8888`, không
   scrape Gateway/Worker `/metrics`; chưa có double-ingest trong cấu hình này.
   Hai port Collector là application metrics và sức khỏe Collector, không phải
   hai bản lưu của cùng signal.
2. **OTel/Collector và Langfuse không thay nhau.** SDK tạo telemetry, Collector
   vận chuyển/lọc/route, Langfuse lưu và hiển thị traces. App không dùng thêm
   Langfuse SDK/direct exporter thứ hai. Không có hai trace backend đang chạy.
3. **Grafana và Langfuse có mục tiêu khác.** Dashboard Grafana hiện theo dõi
   latency/errors/queue/Collector; Langfuse xem từng flow AI. Có chồng lấn biểu đồ
   latency/token ở mức sản phẩm, nhưng bỏ một UI là bỏ một cách điều tra, không
   chứng minh hai backend hoàn toàn trùng nhau.
4. **Hai PostgreSQL là cơ hội gộp instance, không phải gộp dữ liệu.** Một server
   phù hợp có thể có database/role `langfuse` riêng. Không dùng database/schema
   KiRa cho Langfuse. Hai PostgreSQL có sẵn 15.4/16.4 là candidate để kiểm tra
   version, quyền migration, connection/storage headroom và backup; Langfuse
   metadata không cần extension vector. Nếu cùng instance với PG của app, sự cố
   disk/pool/server vẫn ảnh hưởng chat dù app export telemetry fail-open. Ưu tiên
   dịch vụ platform phù hợp hoặc instance riêng khi cần isolation.
5. **Redis/ClickHouse/S3 không phải bản sao queue/vector DB của KiRa.** Queue
   memory của app vẫn là PostgreSQL; ClickHouse không phải nơi lưu embedding
   memory. Tắt media/batch export không bỏ được event object storage của Langfuse.

Nếu dọn metrics cũ, giữ queue sampler/snapshot: `worker/runtime.py` dùng độ mới
của queue observation trong readiness. Tên task `memory-job-metrics` không có
nghĩa task đó chỉ phục vụ dashboard. Dọn registry phải kèm cập nhật endpoint/test
consumer và kiểm chứng readiness; không xóa sampler cùng instrumentation.

## Hạ tầng đã có và điều chưa xác minh

Nguồn là `docs/evidence/kubernetes-vdi-inventory-2026-10-08.txt`, không phải kết
quả truy vấn trực tiếp từ laptop:

- Dòng 12/22: `rancher-monitoring` và `cattle-monitoring-system` → candidate tốt
  để dùng chung Prometheus/Grafana/alert routing; chưa xác nhận trạng thái hoạt động.
- Dòng 27–29: các namespace ClickHouse/operator/test → chưa đủ để kết luận có
  một endpoint ClickHouse cho production hoặc được phép thêm database Langfuse.
- Dòng 48: `minio-operator` → chưa chứng minh có S3 tenant/endpoint/bucket usable.
- Redis trong `vlp-cicd` thuộc CI; inventory không chứng minh nó đáp ứng isolation,
  `noeviction`, version, capacity và chủ sở hữu vận hành cho Langfuse.

Một inventory ngắn, chỉ đọc, từ terminal có quyền cụm:

```sh
kubectl --context=default -n cattle-monitoring-system get deploy,sts,svc
kubectl --context=default -n cattle-monitoring-system get prometheus.monitoring.coreos.com -o custom-columns='NAME:.metadata.name,MONITOR_NAMESPACES:.spec.serviceMonitorNamespaceSelector,MONITOR_LABELS:.spec.serviceMonitorSelector'
kubectl --context=default get servicemonitors.monitoring.coreos.com -A
kubectl --context=default -n cattle-monitoring-system get configmaps -l grafana_dashboard=1
```

Các tên Service/selector/auth lấy từ kết quả thật; không đoán release name thành
hostname. KiRa namespace có default-deny NetworkPolicy, nên tích hợp monitoring
phải thêm đường OTLP/scrape cụ thể. Không cần cài lại node-exporter,
kube-state-metrics, operator hoặc cả kube-prometheus stack chỉ cho KiRa.

## Các phương án và số service mới

Các số dưới là service logic, không phải số Pod/replica; không tính app core.

| Phương án | Thành phần mới | Số service mới | Khả năng/đánh đổi |
| --- | --- | ---: | --- |
| Pilot dùng monitoring platform | Một Collector; metrics/dashboard/logging dùng platform sau khi xác minh | 1; có thể 0 nếu dùng được Collector platform | Operational monitoring; chưa có durable AI trace UI nếu không gắn trace backend |
| Pilot vận hành độc lập | Collector + Prometheus + Grafana | 3 | Có metrics/dashboard riêng; chưa có durable AI trace store, logs vẫn stdout/platform |
| Pilot cần Langfuse và tận dụng PG/S3/metrics platform | Collector + Langfuse Web/Worker + ClickHouse + Redis | 5 | Có trace/AI UI; cần database/role, bucket/credentials riêng và bốn chức năng data store dù một phần dùng chung |
| Toàn bộ overlay tự host | Collector + sáu service Langfuse + Prometheus + Grafana | 9 | Tự vận hành toàn bộ; PVC/backup/retention/upgrade lớn hơn. Loki/log agent chưa tính |

Nếu muốn pipeline OTel chỉ giữ operational metrics, có thể giữ nguyên meter,
đặt trace sampling về 0 và capture content off trong một cấu hình đã kiểm tra.
Không lưu traces ở debug exporter rồi coi đó là trace history có thể tra cứu.
Nếu cần full trace, bảo toàn filter empty `memory_job.claim` và policy masking.

Không khuyến nghị bỏ Collector chỉ để giảm một Pod ở cấu hình hiện tại. App có
một `OTEL_ENABLED` và một OTLP base URL cho cả `/v1/traces` lẫn `/v1/metrics`;
hai provider được khởi tạo cùng runtime. Prometheus hỗ trợ OTLP trực tiếp, nhưng
route metric/traces sang hai backend và auth Langfuse cần thay đổi rõ ràng trong
code/config. Scrape `/metrics` cũ trực tiếp cũng không giữ đủ các metric mới
request/stage/first-event/queue-wait và tên mà dashboard/rules hiện đang dùng.

## Những điểm cần sửa trước khi gọi là monitoring production

- **Phân biệt replica:** `Resource.create()` hiện đặt service name/version/env,
  chưa tự tạo `service.instance.id`. Collector local không enrich Pod identity.
  Khi bật OTel cho hai Gateway, cần cung cấp ID riêng bằng Pod UID/resource attrs
  và kiểm tra counter/reset/aggregate hai replica; nếu thiếu, các stream có thể
  indistinguishable/đụng nhau. Đây là rủi ro cấu hình, chưa phải lỗi đã reproduce
  trên cụm; không cần thêm service để giải quyết.
- **Notification:** bốn Prometheus rules có evaluate, nhưng repo chưa có route
  tới Alertmanager/contact point. `manageAlerts: true` không tự gửi thông báo.
  Ưu tiên wiring alerting platform; không mặc định dựng thêm Alertmanager riêng.
- **Retention/persistence:** Compose local Prometheus đặt 7 ngày và không khai
  báo volume TSDB; contract pilot đề xuất 30 ngày. Local acceptance không phải
  deployment production durable. Langfuse/PG/ClickHouse/S3 cũng cần retention và
  backup thực tế khi được chọn.
- **Sampling:** default Worker poll mỗi giây. Pipeline Langfuse đã drop empty
  claim spans; giữ filter để tránh xấp xỉ 86.400 spans/ngày/Worker nhàn rỗi nếu
  export 100% và không lọc. Không nhầm việc giảm trace với dừng queue polling.

## Footprint và kiểm chứng

Sáu service Langfuse + Collector trong overlay local có tổng `mem_limit`
4.875 GiB (4992 MiB). Đây là tổng trần Docker local, không phải RAM đo được và
không phải khuyến nghị sizing production. Prometheus/Grafana không có mem_limit
trong overlay, nên không suy ra trần cả stack từ số này. Gánh nặng chính cần
cân nhắc là số dependency stateful, storage/backup/upgrade và chuyển thêm image
qua kênh offline, không chỉ CPU/RAM của ba worker node.

43 test hiện có về assets, metrics, telemetry Gateway/Worker đã pass trong lần
audit, gồm parity legacy/OTel và Collector-only scrape. Không benchmark overhead
production, không verify shared platform hay metric collision nhiều Pod trong
lần này. Runtime, YAML resources và bộ image đã build không bị thay đổi.

## Nguồn kiểm tra

- Repo: `compose.observability.yaml`, `compose.langfuse.yaml`,
  `deploy/observability/`, `app/infrastructure/observability/`,
  `worker/telemetry.py`, `worker/runtime.py`.
- `docs/production-readiness-plan.md`: release đầu không thêm Loki; observability
  độc lập với readiness. `docs/benchmark-contract.md`: Langfuse/OTel chỉ trace/debug.
- [Upstream Compose đúng Langfuse v3.225.7](https://raw.githubusercontent.com/langfuse/langfuse/v3.225.7/docker-compose.yml)
  xác nhận Web/Worker và bốn dependency của bản đang acceptance trong repo.
- [Langfuse Kubernetes](https://langfuse.com/self-hosting/deployment/kubernetes-helm)
  hỗ trợ backend ngoài. Current chart/v4 không đồng nhất với baseline local v3;
  không dùng default latest rồi coi là stack đã kiểm tra.
- [Langfuse PostgreSQL](https://langfuse.com/self-hosting/deployment/infrastructure/postgres),
  [cache](https://langfuse.com/self-hosting/deployment/infrastructure/cache),
  [blob storage](https://langfuse.com/self-hosting/deployment/infrastructure/blobstorage).
- [Prometheus exporter đúng Collector 0.160.0](https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/v0.160.0/exporter/prometheusexporter/README.md)
  mô tả mapping `service.instance.id` sang `instance`.
- [Prometheus OTLP](https://prometheus.io/docs/guides/opentelemetry/) và
  [alert routing](https://prometheus.io/docs/alerting/latest/overview/).
