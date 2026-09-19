# Week 5 offline benchmark handoff

Đọc [company-pc-ai-handoff.md](company-pc-ai-handoff.md) để có quy trình đầy đủ từ materialization
đến K8s và [week5-internal-k8s-acceptance.md](week5-internal-k8s-acceptance.md) cho toàn bộ Phase 5.
Tài liệu này chỉ mô tả boundary của bundle.

## Thứ tự đúng

```text
Laptop: code + deterministic tests
PC: KiRa materialization -> human review/freeze -> build exact images
    -> OpenAI/KiRa technical acceptance -> export
VDI: checksum/import/publish
K8s: internal-model official benchmark
```

Không build eval image trước dataset freeze vì eval image phải bake đúng dataset hash đã review.

## Exact image set

- Historical control runtime SHA:
  `75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00`.
- Một runtime + một eval image cho control.
- Một runtime + một eval image cho từng declared candidate, tối đa hai.
- `pgvector/pgvector:0.8.6-pg16-bookworm` là dependency image riêng.

Số variant images là `2 + 2 × candidate_count`; archive có thêm một PostgreSQL dependency image.
Eval image của mỗi variant dùng app/Worker/vendored Mem0/lock từ đúng runtime revision, còn common
harness + frozen dataset đến từ clean harness revision.

## Build và network-disabled acceptance trên PC

```powershell
git status --short
./scripts/week5_offline_handoff.ps1 -Action Build `
  -CandidateRevision @("<candidate-a-full-sha>") `
  -CandidateChangeScope @("prompt", "config") `
  -CandidateSummary "Mô tả ngắn candidate."
./scripts/week5_offline_handoff.ps1 -Action MockAcceptance
```

Hai candidates thì truyền hai SHA. Build fail nếu checkout dirty, image label sai, dataset chưa
`benchmark_ready`/external-approved, hoặc metadata runtime/harness không khớp. Mock acceptance chạy
với `--network none` trên mọi eval image và không claim model quality.

## Chạy stack tuần tự

```powershell
Copy-Item evaluation/week5.pc.env.example .env.week5.pc.local
./scripts/week5_offline_handoff.ps1 -Action Validate -EnvFile .env.week5.pc.local
./scripts/week5_offline_handoff.ps1 -Action StartControl -EnvFile .env.week5.pc.local
# preflight + full control run
./scripts/week5_offline_handoff.ps1 -Action Stop -EnvFile .env.week5.pc.local
./scripts/week5_offline_handoff.ps1 -Action StartCandidate `
  -VariantId candidate-a -EnvFile .env.week5.pc.local
# preflight + full candidate run
./scripts/week5_offline_handoff.ps1 -Action Stop -EnvFile .env.week5.pc.local
```

Mỗi full run dùng fresh database volume/database. `Stop` cố ý giữ volume; không chạy broad volume
prune.

## Export chỉ sau technical PC acceptance

```powershell
./scripts/week5_offline_handoff.ps1 -Action Export `
  -PcPreflightPath artifacts/week5/pc-preflight/freeze.json `
  -PcAcceptancePath artifacts/week5/pc-openai/<run-set-id>/pc-acceptance.json
```

Export fail-closed nếu acceptance chưa pass, dataset/preflight/image manifest thay đổi, dynamic image
count sai hoặc local image ID không còn khớp. `freeze_handoff` chạy trong control eval image với
network bị tắt. Không rebuild image sau PC acceptance.

Bundle có:

- exact runtime/eval/dependency image tar + SHA-256;
- image manifest và hash-bound handoff evidence;
- dataset/review, preflight và PC acceptance hashes;
- secret-free Compose/env templates;
- runbooks và `bundle-manifest.json` chứa checksum từng file.

Bundle không có `.env`, API key, KiRa credential, checkpoint/raw responses, embeddings, database dump,
auth headers hoặc Langfuse export.

## VDI import

Chuyển đúng các file được liệt kê trong `bundle-manifest.json`, rồi:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Import `
  -BundleDirectory <transferred-bundle-directory>
```

Import kiểm toàn bộ file hashes, tar checksum, load images và so image ID với
`image-manifest.json`. Nếu publish registry, dùng immutable digest; không dùng `latest` làm evidence.

Publish tạo create-only `registry-manifest.json` chứa digest bất biến cho từng runtime/eval image:

```powershell
./scripts/week5_offline_handoff.ps1 -Action Publish `
  -BundleDirectory <transferred-bundle-directory> `
  -Registry registry.internal.example/team
```

K8s chỉ deploy các `immutable_reference` trong manifest này, không deploy `pushed_tag`.

## Acceptance

- Dataset reviewed/frozen và hash khớp eval images.
- Mock acceptance PASS trên mọi eval image.
- Live T4.4 preflight PASS.
- Full T4.5 technical acceptance PASS và `official=false`.
- Image IDs/labels/manifest/archive hashes khớp.
- Không secret hoặc sensitive raw artifact trong bundle.
- OTel/Langfuse outage không thay đổi benchmark/business outcome.
