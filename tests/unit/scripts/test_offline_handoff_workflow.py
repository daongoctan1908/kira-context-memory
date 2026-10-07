"""Exercise Build orchestration without Docker, Git mutations, or provider calls."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("compare", [False, True])
def test_build_defaults_to_one_current_runtime_and_comparison_is_opt_in(tmp_path: Path, compare):
    stub = tmp_path / "build.ps1"
    stub.write_text(
        r"""
$global:BuildImages = @{}
$global:ResolvedRevisions = @()
function global:git {
    $global:LASTEXITCODE = 0
    if ($args -contains "status") { return }
    if ($args -contains "rev-parse") {
        $revision = $args[-1] -replace '\^\{commit\}$', ''
        $global:ResolvedRevisions += $revision
        if ($revision -eq "HEAD") { return "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
        return $revision
    }
}
function global:docker {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq "build") {
        $values = @{}
        for ($i = 0; $i -lt $args.Count; $i++) {
            if ($args[$i] -eq "--build-arg") {
                $key, $value = $args[$i + 1] -split '=', 2
                $values[$key] = $value
            }
        }
        $reference = $args[[array]::IndexOf($args, "-t") + 1]
        if (-not $values.RUNTIME_REVISION) { $values.RUNTIME_REVISION = $values.SOURCE_REVISION }
        $global:BuildImages[$reference] = $values
        return
    }
    if ($args[0] -eq "run") {
        $values = $global:BuildImages[$args[6]]
        return (@{
            contract_id = "kira-week5-benchmark-v5"
            runtime_revision = $values.RUNTIME_REVISION
            harness_revision = $values.HARNESS_REVISION
            prompt_sha256 = @{ memory_extraction = ('1' * 64); rewrite_system = ('2' * 64) }
            package_versions = @{
                'kira-context-memory' = '0.4.1'; 'viettel-mem0' = '2.0.20+viettel.7'
            }
        } | ConvertTo-Json -Depth 5)
    }
    if ($args[0] -eq "image") {
        $values = $global:BuildImages[$args[-1]]
        $labels = @{}
        if ($values) {
            $labels = @{
                'org.opencontainers.image.revision' = $values.SOURCE_REVISION
                'io.kira.benchmark.contract' = 'kira-week5-benchmark-v5'
                'io.kira.benchmark.variant' = $values.BENCHMARK_VARIANT
                'io.kira.benchmark.runtime-revision' = $values.RUNTIME_REVISION
                'io.kira.benchmark.harness-revision' = $values.HARNESS_REVISION
                'io.kira.benchmark.role' = $values.BENCHMARK_ROLE
            }
        }
        return (ConvertTo-Json -InputObject @(@{
            Id = ('sha256:' + ('a' * 64)); RepoDigests = @(); Config = @{Labels = $labels}
        }) -Depth 7)
    }
    throw "Unexpected Docker invocation"
}
$target = $args[0]
$bundle = $args[1]
if ($args[2] -eq "compare") {
    & $target -Action Build -BundleDirectory $bundle -CompareHistorical
} else {
    & $target -Action Build -BundleDirectory $bundle
}
if (-not (Test-Path "$bundle/image-manifest.json")) { exit 1 }
ConvertTo-Json -InputObject $global:ResolvedRevisions |
    Set-Content -LiteralPath "$bundle/resolved-revisions.json" -Encoding UTF8
""",
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    completed = subprocess.run(
        [
            _POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(stub),
            str(_ROOT / "scripts/benchmark/offline_handoff.ps1"),
            str(bundle),
            "compare" if compare else "current",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest = json.loads((bundle / "image-manifest.json").read_text(encoding="utf-8"))
    revisions = json.loads((bundle / "resolved-revisions.json").read_text(encoding="utf-8-sig"))
    if compare:
        assert [item["variant_id"] for item in manifest["variants"]] == ["control", "candidate-a"]
        assert len(manifest["images"]) == 5
        assert "control_revision" in manifest
    else:
        assert [item["variant_id"] for item in manifest["variants"]] == ["current"]
        assert manifest["variants"][0]["benchmark_variant"] == "current_runtime"
        assert len(manifest["images"]) == 3
        assert revisions == ["HEAD", "HEAD"]
        assert "control_revision" not in manifest
        assert "candidates" not in manifest
        provenance = json.loads((bundle / "provenance/current.json").read_text(encoding="utf-8"))
        assert provenance["variant"] == "current_runtime"
        assert "candidate" not in provenance
