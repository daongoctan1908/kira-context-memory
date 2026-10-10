# Triển khai KiRa Context Memory lên cụm RKE2

Bộ này dùng cho lần triển khai HTTP nội bộ đầu tiên trong namespace
`kira-context-memory`. Đăng nhập vẫn bật; `APP_ENVIRONMENT=test` và cookie HTTP
được dùng trong lúc chưa có domain/TLS. Các Service đều là `ClusterIP`.
Chưa có đường truy cập trình duyệt từ VDI trong cấu hình mặc định.

## Phạm vi hiện tại và observability đã chọn

Có YAML cho app và đầy đủ subsystem Langfuse tại
[`observability/README.md`](observability/README.md). Người dùng chọn giữ chức năng
riêng của Langfuse; phần dọn trùng là registry/dual-write metrics và `/metrics` cũ
của Gateway/Worker. Runtime chỉ ghi metrics qua OTel và không nhận Langfuse keys.

| Nhóm | YAML | Vai trò |
| --- | --- | --- |
| Collector | `observability/collector.yaml`, `config.yaml` | Một OTLP/HTTP endpoint; traces → Langfuse, metrics → Prometheus scrape |
| Langfuse Web/Worker | `observability/langfuse.yaml` | Timeline từng request/job, bước AI, model/token/IDs; masked content chỉ khi bật riêng |
| Langfuse data stores | `observability/datastores.yaml`, `s3-init.yaml` | PostgreSQL metadata, ClickHouse traces, Redis queue/cache, S3 event blobs |
| Prometheus/Grafana | `observability/metrics.yaml` | Metrics vận hành, rules và dashboard; bỏ file này khi dùng monitoring chung đã xác minh |
| Logs | stdout JSON | Loki chưa có pipeline trong repo, ngoài bộ lần này |

`standalone` có đủ chín service observability. `shared` giữ bảy service
Collector/Langfuse và dùng Prometheus/Grafana chung đã xác minh. Chỉ một backend
được lưu/evaluate metrics KiRa. Inventory `rancher-monitoring` là ứng viên cần kiểm
tra, chưa phải bằng chứng monitoring chung đang dùng được.

Bootstrap app giữ `OTEL_ENABLED=false`; helper observability chỉ bật sau khi data
stores, bucket S3, Langfuse và Collector Ready. Kiểm tra trace/metrics thật và
telemetry outage không ảnh hưởng chat/Worker vẫn là acceptance riêng. Langfuse
không thay memory pgvector; Redis Langfuse không thay queue PostgreSQL của Worker.

Đã kiểm tra trên laptop ngày 2026-10-08: 38 resource qua schema Kubernetes 1.30.5
bằng Kubeconform strict; 11 test của helpers/thứ tự Job pass. Phép thử container
local dùng đúng ba image đã build và config từ YAML, credentials giả, embedding
stub 1024 chiều: PostgreSQL chạy UID 999, tạo role không có quyền DDL; migrations,
memory-init/validate, readiness cả ba thành phần và tạo tài khoản app bằng role
runtime đều pass. Receipt không chứa credentials nằm tại
`docs/evidence/k8s-yaml-local-verification-2026-10-08.json` trong checkout.
Chưa chạy API admission, mount Longhorn, NetworkPolicy hoặc ứng dụng/provider thật
trên cụm; kết quả local không xác nhận các phần này.

Bổ sung ngày 2026-10-09: bản sau sizing có 85 resource YAML qua Kubeconform strict 1.30.5.
Các helper/guard sizing có 45 test pass; tổng tài nguyên, pool và hash YAML được
ghi tại [`kubernetes-generous-sizing-verification-2026-10-09.json`](../../docs/evidence/kubernetes-generous-sizing-verification-2026-10-09.json).
PostgreSQL core cũng đã start với đúng args/UID/shm mới, role runtime và pgvector
SQL pass; xem [`postgres-generous-config-2026-10-09.json`](../../docs/evidence/postgres-generous-config-2026-10-09.json).
Phép thử container observability dùng cấu hình từ YAML đã kiểm tra PG role không
superuser và migrations Langfuse, Redis AUTH/noeviction, ClickHouse, MinIO/bucket
Job, Langfuse Web/Worker readiness, trace thử đọc lại qua API và hai resource
streams metric tách biệt. Xem receipt
`docs/evidence/observability-generous-yaml-local-verification-2026-10-09.json`. Đây là dữ
liệu giả lập trên laptop, chưa phải cluster/provider acceptance.
Hồi quy toàn bộ và lượt chạy lại lỗi fixture đã kiểm tra 1.826 test qua, hai test
skip; coverage gộp 90,90%. Receipt chi tiết (gồm lỗi fixture cũ và test chạy lại):
`docs/evidence/observability-cleanup-verification-2026-10-09.json`.

## Các quyết định đã đưa vào YAML

| Thành phần | Số replica | Image | CPU request / limit mỗi Pod | RAM request / limit mỗi Pod |
| --- | ---: | --- | --- | --- |
| Gateway | 3 | Backend | 4 / 8 | 4Gi / 8Gi |
| Worker | 2 | Backend | 4 / 12 | 8Gi / 24Gi |
| Frontend | 3 | Frontend | 500m / 2 | 512Mi / 1Gi |
| PostgreSQL 16 + pgvector | 1 | PostgreSQL/pgvector | 8 / 24 | 32Gi / 64Gi |

Mức mới dựa trên allocation/usage và Longhorn người dùng cung cấp ngày 2026-10-09.
Xem [`CAPACITY.md`](CAPACITY.md) để review toàn bộ service, connection budget,
storage và replica. Standalone request tổng 64,5 CPU/146,5 GiB RAM, limits
186 CPU/327 GiB. Nhiều replica ưu tiên trải trên ba node; Worker xử lý bốn
job đồng thời, tối đa sáu khi surge. Đây chưa phải benchmark throughput provider.

Thay đổi CPU/RAM và memory tuning nằm trong manifest/config được inject, nên
không cần build lại hai image đã kiểm tra. Source/image snapshot ngày 2026-10-09
vẫn dùng cho runtime; bộ YAML review hiện tại có mức tài nguyên mới.

- Pod/Job chỉ được lên ba node `hkh9104-10-221-248-16`, `-17`, `-18`, có
  selector `infra/zone=compute`, `project=lhvtt` và toleration
  `project=lhvtt:NoSchedule`. Ưu tiên `.17`/`.18` vì `.16` có cảnh báo image GC.
- PVC `postgres-data`: `20Gi`, `ReadWriteOnce`, StorageClass
  `longhorn-storage-2-retain`. Đây là kích thước khởi đầu. Hai bản sao Longhorn
  là bản sao storage; PostgreSQL vẫn có một primary, chưa có HA hoặc backup tự động.
- Qwen rewrite và formation: `http://10.254.135.40:8080/v1`,
  `/models/Qwen3_14B`. Embedding: `http://10.254.135.40:8001/v1`,
  `Qwen3-Embedding-0.6B`, 1024 chiều. Không đặt API key theo kết quả kiểm tra đã có.
- KiRa: `http://10.255.62.64:8122`, domain `VBI`; credentials lấy từ cấu hình
  hiện có. OpenTelemetry tắt ở bootstrap và được helper observability bật sau
  khi Collector `http://otel-collector:4318` cùng backend Ready.
- Runtime dùng role `kira_app`/`kira_memory` không có quyền tạo schema. Hai Job
  migrate/init dùng role admin; Job validate dùng đúng credentials của runtime.
- NetworkPolicy giới hạn ingress/egress theo Service và các IP provider đã biết;
  cho phép DNS UDP/TCP 53. Hiệu lực thực tế cần kiểm tra trên CNI của cụm.

## 1. Chốt ba image đã publish vào registry nội bộ

Dockerfile backend/frontend đã được cập nhật và cả hai image đã build, kiểm tra
local ngày 2026-10-09 với tag `0.4.1-cbea254a95b2`. Context nguồn đã freeze tại
`artifacts/deployment/k8s-20261009-sized/build-context`; receipt nằm trong
`docs/evidence/product-dockerfile-rebuild-2026-10-09.json`. Chưa export `.tar`,
zip, push hoặc apply. Giữ bản `k8s-20261008` để đối chiếu; backend cũ còn legacy
metrics nên không dùng cho release OTel-only hiện tại.

Sau khi review, dùng helper `offline_images build --bundle` với bundle mới này
để tiếp tục build/export ba image theo hướng dẫn `scripts/deploy/OFFLINE-IMAGES.md`.
Nếu source runtime thay đổi tiếp, tạo bundle mới. Chuyển theo đường laptop → PC
công ty → Drive nội bộ → VDI; cả ba image phải publish dưới `registry.vlp.vn`.

Copy `images.example.json` thành file riêng, điền ba reference **do registry trả
về sau push**, theo dạng `registry.vlp.vn/<repository>@sha256:<64 ký tự hex>`.
Image ID/config digest trên laptop không thay cho manifest digest tại registry.
Không dùng tag hoặc Docker Hub trong YAML chạy trên cụm.

Trên laptop, từ thư mục gốc repo:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.render_k8s --images artifacts/deployment/images.json --output artifacts/deployment/k8s-ready
```

Lệnh tạo một thư mục file nguồn mới để review, không zip, không build lại image,
không push/apply. Nó từ chối ghi đè thư mục đã tồn tại và từ chối reference thiếu
digest hoặc nằm ngoài registry nội bộ. Nếu chưa push thì review trực tiếp các
template `deploy/k8s/*.yaml`; các marker image chưa phải reference để apply.

Để có cả observability, export thêm image amd64 từ laptop có Docker Linux:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.observability_images export --mode standalone --output artifacts/deployment/observability-transfer
.\.venv\Scripts\python.exe -m scripts.deploy.observability_images verify --output artifacts/deployment/observability-transfer
```

Output là chín `.tar` riêng, JSON manifest và SHA256SUMS; không zip. Khi dùng
monitoring chung, export `--mode langfuse` chỉ có bảy image. Chuyển bằng cùng kênh
nội bộ, kiểm tra checksum trước import/publish bằng công cụ registry được công ty
cho phép. Manifest lưu local tag cho lệnh import/tag và upstream image identities;
upstream digest cũng không thay digest được registry nội bộ trả về sau push.

Copy `images.observability.example.json` thành file riêng; điền ba image app và
chín image observability đã publish. Với shared, bỏ hai key `prometheus`/`grafana`.

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.render_k8s --images artifacts/deployment/images-all.json --observability standalone --output artifacts/deployment/k8s-all-ready
```

Thay `standalone` bằng `shared` khi monitoring chung đã được xác minh. Renderer
chỉ chép các file được chọn, không chép file Secret riêng; shared không có
`metrics.yaml`. Tất cả image trong workload phải là internal immutable digest.

## 2. Chuẩn bị Secret riêng

Trên laptop, tái sử dụng `.env.openai.local` đã có KiRa credentials:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.prepare_k8s_secrets --kira-env .env.openai.local --output artifacts/deployment/private/kira-secrets.yaml --registry-user <pull-user>
```

Registry password/token được hỏi bằng prompt ẩn. Chỉ bỏ `--registry-user` khi đã
xác nhận repository cho phép pull ẩn danh hoặc node đã có credential tương ứng.
Helper chỉ lấy KiRa username/Basic Auth; không lấy OpenAI credentials trong file
local. Nó tạo ba password DB riêng, hai DSN runtime và hai DSN admin cùng trỏ vào
database `kira_context` tại Service `postgres:5432`.

Giữ file này trong vị trí riêng có quyền truy cập hạn chế, chuyển bằng kênh nội bộ
được phép cho credentials. Kubernetes Secret không tự mã hóa các giá trị khi lưu
trong file. `secrets.example.yaml` chỉ để review cấu trúc; không apply nguyên mẫu.

**Giữ nguyên password DB khi tái sử dụng PVC.** Script PostgreSQL tạo role chỉ chạy
khi data directory rỗng. Tạo Secret mới trên PVC đã có dữ liệu không đổi password
role trong DB và sẽ làm app không đăng nhập được. Helper từ chối ghi đè file cũ.
Không đưa Secret admin hoặc Secret PostgreSQL vào Deployment Gateway/Worker.

Tạo thêm Secret riêng cho observability bằng email admin Langfuse thực tế:

```powershell
.\.venv\Scripts\python.exe -m scripts.deploy.prepare_observability_secrets --admin-email <admin-email> --output artifacts/deployment/private/kira-observability.yaml
```

Helper tạo password mạnh, DSN riêng, project keys và OTLP Basic Auth nhất quán,
salt/encryption key/NextAuth secret. Giá trị không được in ra terminal. Đọc
email/password trong file riêng khi đăng nhập Langfuse; Grafana dùng user `admin`
và `GRAFANA_ADMIN_PASSWORD`. Giữ nguyên file với PVC, không tạo lại encryption
key/salt/database credentials để thử lại deployment.

## 3. Triển khai lần đầu từ host có kubectl

Chuyển nguyên thư mục `k8s-ready` và file Secret riêng tới host/terminal có quyền
kubectl của cụm. Context đã biết là `default`. Nếu chạy trong terminal Rancher,
các file phải tồn tại tại filesystem của terminal đó; chuyển lên VDI chưa đồng
nghĩa terminal Rancher đọc được đường dẫn trên VDI.

Trong thư mục `k8s-ready`, dùng shell có `sh`:

```sh
export KIRA_KUBE_CONTEXT=default
sh apply.sh validate
sh apply.sh apply /path/to/private/kira-secrets.yaml
sh apply.sh status
```

`validate` kiểm tra schema bằng kubectl; vẫn cần kết nối API cho discovery.
`apply` tạo namespace, nạp/check Secret, ConfigMap, NetworkPolicy, PVC và
PostgreSQL; chờ DB sẵn sàng rồi chạy **migrate → memory-init → memory-validate**.
Chỉ sau khi cả ba Job thành công mới apply Gateway/Worker/Frontend. Job lỗi hoặc
timeout sẽ dừng script và giữ Pod/log để xem. Script không in giá trị Secret.

Nếu terminal chỉ thuận tiện chạy từng lệnh kubectl, thực hiện cùng thứ tự sau.
Dừng ngay khi một bước lỗi; không apply runtime khi Job chưa Complete:

```sh
kubectl --context=default apply -f namespace.yaml
kubectl --context=default -n kira-context-memory apply -f /path/to/private/kira-secrets.yaml
kubectl --context=default -n kira-context-memory apply -f config.yaml
kubectl --context=default -n kira-context-memory apply -f networkpolicy.yaml
kubectl --context=default -n kira-context-memory apply -f postgres-pvc.yaml
kubectl --context=default -n kira-context-memory apply -f postgres.yaml
kubectl --context=default -n kira-context-memory rollout status statefulset/postgres --timeout=300s
kubectl --context=default -n kira-context-memory apply -f jobs/migrate.yaml
kubectl --context=default -n kira-context-memory wait --for=condition=Complete job/migrate --timeout=900s
kubectl --context=default -n kira-context-memory apply -f jobs/memory-init.yaml
kubectl --context=default -n kira-context-memory wait --for=condition=Complete job/memory-init --timeout=900s
kubectl --context=default -n kira-context-memory apply -f jobs/memory-validate.yaml
kubectl --context=default -n kira-context-memory wait --for=condition=Complete job/memory-validate --timeout=900s
kubectl --context=default -n kira-context-memory apply -f runtime.yaml
kubectl --context=default -n kira-context-memory rollout status deployment/worker --timeout=600s
kubectl --context=default -n kira-context-memory rollout status deployment/gateway --timeout=600s
kubectl --context=default -n kira-context-memory rollout status deployment/frontend --timeout=600s
```

Không chạy `kubectl apply -f . -R`: thư mục có mẫu Secret, Job cần đúng thứ tự và
resource optional. Không tạo ResourceQuota/LimitRange hoặc gắn Rancher project
khi chưa chọn quota cụ thể.

Sau khi core app Ready, từ cùng thư mục `k8s-all-ready` chạy:

```sh
sh observability/apply.sh validate
sh observability/apply.sh apply /path/to/private/kira-observability.yaml
sh observability/apply.sh status
```

Mode được đọc từ release do renderer tạo. Script chờ data stores, Job bucket S3,
Langfuse Web/Worker, Collector và metrics nếu standalone; rồi patch ConfigMap
OTLP và restart Gateway/Worker. Mỗi Pod gửi Pod UID để phân biệt metric giữa
replica. Quy trình chỉ đảm bảo thứ tự/Ready, chưa chứng minh telemetry đã lưu
hay alert đã gửi: bốn rule hiện chưa có Alertmanager/contact route.

## 4. Kiểm tra và tạo tài khoản đăng nhập app

```sh
kubectl --context=default -n kira-context-memory get pods -o wide
kubectl --context=default -n kira-context-memory get pvc,services,jobs
kubectl --context=default -n kira-context-memory logs job/migrate
kubectl --context=default -n kira-context-memory logs job/memory-init
kubectl --context=default -n kira-context-memory logs job/memory-validate
kubectl --context=default -n kira-context-memory get events --sort-by=.lastTimestamp
```

Xác nhận PVC Bound, Longhorn volume khỏe đủ hai replica, Pod chạy đúng ba node,
ba Job Complete và readiness của các Deployment. `memory-init` kiểm tra embedding
thực tế có 1024 chiều trước khi ghi contract; `memory-validate` kiểm tra schema
và contract với runtime credentials. Các bước này cần chạy thật trên cụm.

Operator Pod chỉ sống tối đa một giờ, dùng role DB runtime và không có KiRa/admin
credentials. Dùng nó để tạo tài khoản app đầu tiên (khác tài khoản dịch vụ KiRa):

```sh
kubectl --context=default -n kira-context-memory apply -f optional/operator.yaml
kubectl --context=default -n kira-context-memory wait --for=condition=Ready pod/kira-operator --timeout=120s
kubectl --context=default -n kira-context-memory exec -it kira-operator -- kira-auth-admin create --username admin
kubectl --context=default -n kira-context-memory exec kira-operator -- python -c 'import urllib.request; urls=["http://frontend:8080/healthz","http://gateway:8000/ready","http://worker:8001/ready"]; [(print(u, urllib.request.urlopen(u, timeout=10).status)) for u in urls]'
kubectl --context=default -n kira-context-memory delete pod kira-operator
```

Lệnh tạo user hỏi password bằng prompt ẩn. Sau khi có đường truy cập trình duyệt,
kiểm tra login, chat/SSE tới KiRa, rewrite, formation và retrieval bằng Qwen đã
chọn; kiểm tra cách ly dữ liệu hai tài khoản và dữ liệu còn sau restart. Pod Ready
không thay cho các acceptance này. KiRa endpoint cần được kiểm tra từ Pod nếu
chưa có phép thử authenticated trong cụm.

Nếu PostgreSQL lỗi permission khi mount PVC, xem Pod events và quyền volume/fsGroup
trên Longhorn; không đổi container sang root để bỏ qua lỗi. Nếu pull lỗi, kiểm tra
reference đã publish và quyền của `registry-vlp`. Nếu Job lỗi, sửa nguyên nhân
rồi xóa đúng Job lỗi và apply lại; không xóa namespace/PVC để thử lại.

## 5. Khi có đường VDI → node và domain/TLS

`optional/frontend-nodeport.yaml` là mẫu Service bổ sung. Kubernetes tự chọn
NodePort trong range của cụm; không đoán range từ default vì cụm có port riêng.
Chỉ apply khi đã chọn đường truy cập, mở firewall và bổ sung NetworkPolicy cho
nguồn truy cập thực tế. Policy hiện tại chỉ cho frontend nhận từ Pod cùng namespace;
chỉ tạo NodePort chưa làm trình duyệt VDI truy cập được.

Sau khi biết URL HTTP nội bộ thực tế, cập nhật `AUTH_ALLOWED_ORIGIN` đúng scheme,
host và port, rồi restart Gateway. Nếu dùng port-forward, origin cũng phải là URL
trình duyệt thực tế. Không tắt auth để xử lý sai origin.

Trước khi dùng như production có người dùng, chọn Ingress class/domain/certificate,
cấu hình HTTPS rồi đổi `APP_ENVIRONMENT=production`, `AUTH_COOKIE_SECURE=true`,
`AUTH_ALLOWED_ORIGIN=https://<domain>`; auth và static-identity giữ nguyên. Xác minh
proxy SSE, session/cookie, backup/restore và yêu cầu HA. Bộ hiện tại chưa cấu hình
TLS cho PostgreSQL hoặc cơ chế failover DB.

## 6. Lần nâng cấp sau

Đây là quy trình triển khai đầu tiên. Job template là immutable; không dùng kết
quả Job Complete cũ để chứng minh image mới đã migrate/validate. Khi nâng cấp:
backup DB và xác nhận restore, giữ PVC/credentials, dừng traffic và quiesce
Gateway/Worker, review migration và khả năng rollback, xóa đúng ba Job cũ, rồi
chạy lại các Job với image mới trước khi rollout runtime. Không downgrade schema
tự động và không delete PVC. Thay ConfigMap không tự restart Pod; thực hiện
rollout restart các Deployment liên quan khi chỉ thay cấu hình.
Mỗi lần dùng lại `apply.sh` cho core, ConfigMap sẽ trở về `OTEL_ENABLED=false`;
chạy tiếp `observability/apply.sh apply` để bật lại luồng đã chọn. Upgrade data
stores/Langfuse cần review migrations, backup và khả năng rollback riêng; không
đổi Secret trên PVC đã có hoặc dùng Job Complete cũ cho template/image mới.

Tham khảo kỹ thuật: [Node affinity](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/),
[NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/),
[StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/),
[psql 16](https://www.postgresql.org/docs/16/app-psql.html).
