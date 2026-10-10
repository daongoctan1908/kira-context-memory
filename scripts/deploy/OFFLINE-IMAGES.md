# KiRa product image handoff

This directory is the product bundle for laptop -> company PC -> internal Drive -> VDI.
It contains backend, frontend and PostgreSQL/pgvector Docker image archives for `linux/amd64`.
Credentials are supplied separately at deployment time. The same backend image serves Gateway,
Worker, migrations and memory initialization. No local Qwen/embedding weights are included.

## Before import

Transfer every file in this directory. Verify `SHA256SUMS.txt` before importing. On Linux,
run `sha256sum -c SHA256SUMS.txt` from this directory. On a PC with this repository and Python
3.11+, use `python -m scripts.deploy.offline_images verify --bundle <transfer-directory>`.
Do not proceed if a checksum fails.

## Docker import and internal publish

These commands require a working Docker CLI/engine on the import/publish host. If VDI uses
different tooling, confirm its archive import and registry publish commands first.

```sh
docker load --input backend.tar
docker load --input frontend.tar
docker load --input postgres.tar
```

Read each image's `reference`, `image_id`, `config_digest` and `platform` from
`image-manifest.json`. Check `docker image inspect <reference>` reports `linux/amd64` after
import. Docker's containerd image store and classic image store can report different image
IDs: the classic store uses the config digest, while the source store may report the image
index digest. Check its ID against the recorded source `image_id` or `config_digest`, according
to the store in use. The verifier checks each archive's configuration blob against the recorded
config digest. After import, `docker save` to a temporary archive followed by the same config
blob check is another way to verify identity across stores. Registry digests are recorded
separately after publish; do not substitute the local config digest for them.

Once the actual repositories and publish account under `registry.vlp.vn` are available,
authenticate interactively or via `--password-stdin`, tag the imported images to those
repositories, and push them. Record each registry's immutable `repository@sha256:...` reference
after publish. Gateway, Worker and initialization Jobs must use the same backend digest.
No registry login, repository selection or push is performed by this build helper.

## Deployment boundary

The included namespace/PVC and provider fragments are bootstrap inputs. Full application
workloads and database credentials still need preparation. Domain/Ingress/TLS and VDI browser
access are deferred. Container import/config checks are not real application, storage,
KiRa/SSE, memory quality or production acceptance evidence. An eval image is not included;
the existing benchmark handoff remains separate when benchmark execution is requested.

`source-manifest.json` records a frozen working-tree source inventory and SHA256, along with
the base Git revision. The snapshot may include uncommitted source changes; do not identify
the binary by Git revision alone. Use image IDs and, after publish, registry digests.
