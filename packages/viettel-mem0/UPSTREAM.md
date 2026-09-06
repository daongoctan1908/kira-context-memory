# Upstream provenance

`packages/viettel-mem0` is the in-repository fork of Mem0 OSS used by the KiRa
Context Gateway.

| Field | Value |
| --- | --- |
| Upstream repository | `https://github.com/mem0ai/mem0.git` |
| Upstream tag | `v2.0.20` |
| Upstream commit | `9a7924befd7026e41e445ba809370009e5e985a6` |
| Upstream tree | `71d41407cefaae26d8cdeb2f180f24bc4b0e90a7` |
| Pristine vendor commit | `4c95a43` |
| License | Apache-2.0 |

The tree at the pristine vendor commit is byte-for-byte and mode-for-mode equal
to the upstream Git tree. Verify it with:

```powershell
git rev-parse 4c95a43:packages/viettel-mem0
git rev-parse 9a7924befd7026e41e445ba809370009e5e985a6^{tree}
```

Both commands must print `71d41407cefaae26d8cdeb2f180f24bc4b0e90a7`.
All Viettel packaging or behavior changes must be made after `4c95a43` and
recorded in `PATCHES.md`. Upstream source must never be rewritten into the
pristine commit.
