# Sizing cho ba worker node

Cấu hình này tăng CPU/RAM thoáng hơn theo yêu cầu người dùng, dựa trên
output `kubectl describe/top` và Longhorn người dùng cung cấp ngày 2026-10-09.
Đây là lựa chọn triển khai; chưa phải kết quả benchmark hoặc cam kết số người
dùng đồng thời. Tổng CPU/RAM node không được dùng để tự suy ra công suất Qwen.

## Số liệu đã có

| Node | CPU đã request | CPU đang dùng | RAM đang dùng | Longhorn free |
| --- | ---: | ---: | ---: | ---: |
| `.16` | 12,12 CPU | 2,131 CPU | 5.847 MiB | 239,4 GiB |
| `.17` | 12,12 CPU | 1,554 CPU | 6.023 MiB | 148,6 GiB |
| `.18` | 12,12 CPU | 1,933 CPU | 5.736 MiB | 793,0 GiB |

Mỗi node có 96 CPU và khoảng 247 GiB RAM. Requests RAM hiện chỉ 346 MiB/node,
thấp hơn usage quan sát; scheduler reservations không đại diện toàn bộ mức sử
dụng thực. Output là một thời điểm. Receipt trong checkout:
[`kubernetes-capacity-2026-10-09.json`](../../docs/evidence/kubernetes-capacity-2026-10-09.json).

## Mức đã đưa vào YAML

Requests là phần scheduler đặt trước; limits là trần cho từng container. Bảng
dưới tính **mỗi Pod**, chưa nhân replica. CPU có thể bị throttle khi chạm limit;
RAM chạm limit có thể dẫn tới OOM, nên cần theo dõi cùng usage sau triển khai.

| Thành phần | Replica | CPU request / limit | RAM request / limit |
| --- | ---: | ---: | ---: |
| Gateway | 3 | 4 / 8 | 4 GiB / 8 GiB |
| Worker memory | 2 | 4 / 12 | 8 GiB / 24 GiB |
| Frontend | 3 | 0,5 / 2 | 512 MiB / 1 GiB |
| PostgreSQL app + pgvector | 1 | 8 / 24 | 32 GiB / 64 GiB |
| Langfuse PostgreSQL | 1 | 4 / 12 | 8 GiB / 16 GiB |
| Langfuse ClickHouse | 1 | 8 / 24 | 32 GiB / 64 GiB |
| Langfuse Redis | 1 | 2 / 4 | 4 GiB / 8 GiB |
| Langfuse MinIO | 1 | 2 / 8 | 4 GiB / 16 GiB |
| Langfuse Web | 2 | 2 / 6 | 4 GiB / 8 GiB |
| Langfuse Worker | 2 | 4 / 12 | 8 GiB / 16 GiB |
| OTel Collector | 1 | 2 / 8 | 4 GiB / 8 GiB |
| Prometheus, standalone | 1 | 4 / 12 | 8 GiB / 24 GiB |
| Grafana, standalone | 1 | 1 / 4 | 1 GiB / 4 GiB |

Standalone có 20 Pod chạy thường xuyên, request tổng **64,5 CPU / 146,5 GiB
RAM**, limits tổng **186 CPU / 327 GiB RAM**. Shared bỏ Prometheus/Grafana riêng:
18 Pod, request **59,5 CPU / 137,5 GiB**, limits **170 CPU / 299 GiB**.
Limits tổng là phép cộng trần container, không phải usage hoặc RAM được đặt trước.

Nếu cả năm Deployment rolling cùng lúc và mỗi Deployment có thêm một Pod surge,
standalone có 25 Pod, request **79 CPU / 171 GiB**, limits **226 CPU / 384 GiB**.
Các Job migrate/init/validate chạy tuần tự; mỗi Job request 2 CPU/2 GiB, limit
8 CPU/8 GiB. Operator và S3 Job là tác vụ tạm. Pod đang Terminating có thể còn
giữ tài nguyên ngoài con số surge này; admission/placement vẫn phải kiểm tra thật.

So với pool 288 CPU/~741 GiB, requests steady chiếm khoảng 22,4% CPU/19,8% RAM;
limits steady cho phép burst tới khoảng 64,6% CPU/44,1% RAM. Nếu cộng 36,36 CPU
requests đang có trong snapshot với requests rollout, tổng là 115,36 CPU trong
pool. Requests toàn stack vẫn nhỏ hơn capacity hai node, nhưng placement theo
node và tải thực tế cần xác nhận khi triển khai.

Xem lại tổng trực tiếp từ YAML khi chỉnh sizing:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.capacity_report --observability standalone --format markdown
```

## Replica và phân bố

Gateway/Frontend có ba replica; Worker có hai replica với hai job/Pod: tổng
bốn formation job hoạt động, tối đa sáu trong lúc Worker surge. Queue claim,
lease token và receipt dùng PostgreSQL chung. Các job khác nhau trong cùng
conversation không được bảo đảm FIFO; giữ contract hiện có.

Gateway/Worker dùng một process Python/Pod, không nhân thêm `uvicorn --workers`.
Admission rate/concurrency của Gateway hiện **theo process**; ba replica không
biến giới hạn này thành giới hạn global cho một user. KiRa, Qwen rewrite/formation
và embedding đều chạy ngoài pool: tăng tài nguyên K8s không làm các server đó
tăng throughput. Tăng Worker tiếp chỉ sau khi đo queue wait, provider latency và
timeout ở tải thực.

Các Deployment nhiều replica có anti-affinity ưu tiên và topology spread
`maxSkew=1`, `ScheduleAnyway` theo hostname. Scheduler ưu tiên phân bố đều trong
ba node đã chọn; khi một node unavailable hoặc thiếu tài nguyên, vẫn cho phép
chạy trên node còn lại. Đây là ưu tiên, không bảo đảm luôn một Pod mỗi node.
Giữ ưu tiên `.17`/`.18` vì chưa có bằng chứng cảnh báo image GC `.16` đã hết.
Rollout `maxUnavailable=0`, `maxSurge=1`; PDB Gateway/Frontend giữ hai Pod,
Worker giữ một; Langfuse Web/Worker giữ một Pod mỗi dịch vụ khi drain tự nguyện.
PDB không bảo đảm availability khi node lỗi đột ngột.

Các database/object stores vẫn có một primary. Tăng `replicas` trực tiếp cho
PostgreSQL/ClickHouse/Redis/MinIO không tạo replication/failover. HA cho các store
là một thiết kế riêng, có replication và backup/restore cần kiểm tra.

Collector giữ một replica và dùng `Recreate` khi upgrade. Pipeline cumulative
metrics hiện cần một nơi giữ mỗi stream; thêm hai Collector sau một Service
có thể làm stream xuất hiện ở cả hai scrape target. Recreate có khoảng gián
đoạn telemetry; chat/Worker vẫn fail-open. Xem
[OTel single-writer principle](https://opentelemetry.io/docs/collector/deploy/gateway/#multiple-collectors-and-the-single-writer-principle).

## DB, cache và connection budget

PostgreSQL app dùng `max_connections=200`, `shared_buffers=8GB`,
`effective_cache_size=24GB`, `max_wal_size=2GB`. Shared buffers bằng 25% request
RAM; effective cache chỉ là ước lượng cho planner, không cấp thêm 24 GiB RAM.
Work memory giữ default để tránh nhân bộ nhớ theo query/connection. Hai server
PostgreSQL có memory-backed `/dev/shm` 256 MiB, tính vào memory container.
Xem [PostgreSQL 16 resource settings](https://www.postgresql.org/docs/16/runtime-config-resource.html)
và [effective cache size](https://www.postgresql.org/docs/16/runtime-config-query.html#GUC-EFFECTIVE-CACHE-SIZE).

Giữ async pool 5 + overflow 2 và memory pool max 3. Mem0 còn có entity store
khởi tạo lazy với pool tương tự: mỗi process có thể mở **7 + 3 + 3 = 13**
connections. Năm app Pod tối đa 65; Gateway/Worker cùng surge thành bảy Pod,
tối đa 91. Phần còn lại dành cho admin, Jobs và kết nối đang drain. Tăng replicas,
pools hoặc process/Pod thì phải tính lại, không chỉ tăng PostgreSQL limit.

Langfuse dùng PostgreSQL riêng `max_connections=200`, shared buffers 2 GiB,
effective cache 6 GiB.
URL Prisma 6 có `connection_limit=15&pool_timeout=10`: bốn Pod tối đa 60,
cả Web/Worker surge thành sáu Pod tối đa 90, còn headroom cho migration/admin.
Giới hạn explicit tránh pool default tăng theo CPU của node. Tham khảo
[Prisma pool parameters for v6](https://www.prisma.io/docs/orm/v7/prisma-client/setup-and-configuration/databases-connections/connection-pool).
File Secret đã tạo từ bản trước cần bổ sung hai URL parameters, giữ nguyên
password/salt/keys; không tạo lại credential trên PVC đang dùng.

Redis dùng `maxmemory 4gb`, `noeviction` với container limit 8 GiB để có khoảng
trống cho AOF/buffer. Node heaps Langfuse Web/Worker là 4096/8192 MiB dưới memory
limits 8/16 GiB. Collector memory limiter hard 6144 MiB, spike 1024 MiB và Go
memory limit 4915 MiB dưới Pod limit 8 GiB; queue vẫn bounded, không tăng vô hạn.
Go limit tương ứng khoảng 80% memory limiter hard limit theo
[hướng dẫn Collector 0.160.0](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.160.0/processor/memorylimiterprocessor/README.md).

## Storage giữ riêng với CPU/RAM

PVC giữ 83 GiB logical khi standalone: app PG20 + Langfuse PG10/CH20/Redis2/
MinIO20 + Prometheus10/Grafana1. Hai Longhorn replicas tương ứng khoảng 166 GiB
data copies, chưa tính metadata/snapshots và replica rebuild; đây không phải
phép cộng vào ephemeral storage của node. Shared có 72 GiB logical/144 GiB data.

`.17` còn 148,6 GiB theo output đã cung cấp. Longhorn `allowScheduling=true`
cấp node chưa xác nhận disk schedulable hoặc minimum-free policy. StorageClass
không khóa replicas vào ba compute node, nên dữ liệu có thể đặt trên storage
nodes khác trong cluster. Giữ PVC hiện tại cho bootstrap và xác minh volume
Bound/đủ replicas, disk conditions cùng dung lượng sau apply. Chưa tăng PVC
hàng trăm GiB chỉ vì CPU/RAM dư. ClickHouse/MinIO retention, backup/restore và
đo growth cần được chốt trước khi lưu trace production lâu dài.

Ephemeral request/limit mỗi container vẫn 128 MiB/1 GiB vì node chỉ có khoảng
47,4 GiB allocatable loại này và `.16` có warning image GC. Image storage và
container log rotation cần kiểm tra riêng; metrics/data store không ghi data
chính vào writable layer thay PVC.
