[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Build", "Export", "Import", "MockAcceptance", "Validate", "StartCurrent", "StartControl", "StartCandidate", "Stop", "Publish")]
    [string]$Action,
    [string]$RuntimeRevision = "HEAD",
    [switch]$CompareHistorical,
    [ValidateCount(1, 2)]
    [string[]]$CandidateRevision = @("HEAD"),
    [ValidateSet("prompt", "config", "runtime_code", "dependencies", "schema", "lifecycle")]
    [string[]]$CandidateChangeScope = @("runtime_code"),
    [string]$CandidateSummary = "Declared benchmark candidate.",
    [ValidatePattern('^(current|candidate-[ab])$')]
    [string]$VariantId = "candidate-a",
    [string]$BundleDirectory = "artifacts/benchmark/offline-handoff",
    [string]$EnvFile = ".env.benchmark.internal.local",
    [string]$PcPreflightPath = "artifacts/benchmark/pc-preflight/freeze.json",
    [string]$PcAcceptancePath = "artifacts/benchmark/pc-openai/pc-acceptance.json",
    [string]$Registry = ""
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Resolve-InputPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepositoryRoot $Path))
}

$ControlRevision = "05a2d17ff9d10bb410a65eb0e662618d55930e2d"
$ContractId = "kira-week5-benchmark-v5"
$BundleRoot = Resolve-InputPath $BundleDirectory
$ManifestPath = Join-Path $BundleRoot "image-manifest.json"
$RegistryManifestPath = Join-Path $BundleRoot "registry-manifest.json"
$ComposeFile = Join-Path $RepositoryRoot "compose.benchmark.yaml"
$ResolvedEnvFile = Resolve-InputPath $EnvFile
$ResolvedPcPreflightPath = Resolve-InputPath $PcPreflightPath
$ResolvedPcAcceptancePath = Resolve-InputPath $PcAcceptancePath
$DatasetManifestPath = Join-Path $RepositoryRoot "dataset/kira_ltm_v1/manifest.json"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

function Resolve-GitRevision {
    param([string]$Revision)
    $value = (& git -C $RepositoryRoot rev-parse "$Revision^{commit}").Trim()
    if ($LASTEXITCODE -ne 0 -or $value -notmatch '^[a-f0-9]{40}$') {
        throw "Unable to resolve an exact Git revision"
    }
    return $value
}

function Ensure-Worktree {
    param([string]$Path, [string]$Revision)
    if (Test-Path -LiteralPath $Path) {
        $existing = (& git -C $Path rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $existing -ne $Revision) {
            throw "Existing handoff worktree does not match its declared revision"
        }
        return
    }
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Invoke-Checked git @("-C", $RepositoryRoot, "worktree", "add", "--detach", $Path, $Revision)
}

function Get-ImageRecord {
    param(
        [string]$Role,
        [string]$Reference,
        [string]$ExpectedSourceRevision,
        [string]$ExpectedRuntimeRevision,
        [string]$ExpectedHarnessRevision,
        [string]$ExpectedVariant
    )
    $inspect = (& docker image inspect $Reference | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0) {
        throw "Image is unavailable: $Role"
    }
    if ($ExpectedSourceRevision) {
        $labels = $inspect.Config.Labels
        if ($labels.'org.opencontainers.image.revision' -ne $ExpectedSourceRevision -or
            $labels.'io.kira.benchmark.contract' -ne $ContractId -or
            $labels.'io.kira.benchmark.variant' -ne $ExpectedVariant -or
            $labels.'io.kira.benchmark.runtime-revision' -ne $ExpectedRuntimeRevision -or
            $labels.'io.kira.benchmark.harness-revision' -ne $ExpectedHarnessRevision -or
            $labels.'io.kira.benchmark.role' -ne $Role) {
            throw "Image provenance labels do not match: $Role"
        }
    }
    return [ordered]@{
        role = $Role
        reference = $Reference
        image_id = $inspect.Id
        repo_digests = @($inspect.RepoDigests)
        source_revision = $ExpectedSourceRevision
        runtime_revision = $ExpectedRuntimeRevision
        harness_revision = $ExpectedHarnessRevision
        variant = $ExpectedVariant
    }
}

function Get-EvalMetadata {
    param(
        [string]$Reference,
        [string]$ExpectedRuntimeRevision,
        [string]$ExpectedHarnessRevision
    )
    $raw = & docker run --rm --network none --entrypoint python $Reference `
        -m scripts.benchmark.image_metadata
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect benchmark metadata from eval image"
    }
    $metadata = $raw | ConvertFrom-Json
    if ($metadata.contract_id -ne $ContractId -or
        $metadata.runtime_revision -ne $ExpectedRuntimeRevision -or
        $metadata.harness_revision -ne $ExpectedHarnessRevision) {
        throw "Eval image runtime/harness metadata does not match"
    }
    return $metadata
}

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Missing image manifest; run Build or Import first"
    }
    return Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Copy-NewFile {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required handoff evidence is missing"
    }
    if (Test-Path -LiteralPath $Destination) {
        throw "Handoff output already exists"
    }
    Copy-Item -LiteralPath $Source -Destination $Destination
}

function Assert-ManifestImagesUnchanged {
    param($Manifest)
    foreach ($image in $Manifest.images) {
        $inspect = (& docker image inspect $image.reference | ConvertFrom-Json)[0]
        if ($LASTEXITCODE -ne 0 -or $inspect.Id -ne $image.image_id) {
            throw "Image changed or is unavailable after manifest creation"
        }
    }
}

function Assert-BundleFileHashes {
    param($BundleManifest)
    foreach ($file in $BundleManifest.files) {
        $path = Join-Path $BundleRoot $file.name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-Sha256 $path) -ne $file.sha256) {
            throw "Offline bundle file checksum mismatch"
        }
    }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    if (-not (Test-Path -LiteralPath $ResolvedEnvFile -PathType Leaf)) {
        throw "Missing populated internal environment file"
    }
    $env:BENCHMARK_ENV_FILE = $ResolvedEnvFile
    $dockerArguments = @(
        "compose", "--env-file", $ResolvedEnvFile, "-f", $ComposeFile
    ) + $Arguments
    Invoke-Checked docker $dockerArguments
}

New-Item -ItemType Directory -Force -Path $BundleRoot | Out-Null

switch ($Action) {
    "Build" {
        $dirty = & git -C $RepositoryRoot status --porcelain=v1
        if ($dirty) {
            throw "Build requires a clean checkout so image provenance is exact"
        }
        if (Test-Path -LiteralPath $ManifestPath) {
            throw "Build manifest already exists; use a new bundle directory for a new image set"
        }
        $harnessSha = Resolve-GitRevision "HEAD"
        $declared = @()
        $controlSha = $null
        if ($CompareHistorical) {
            if (-not $CandidateSummary.Trim()) {
                throw "Candidate summary must not be blank"
            }
            $controlSha = Resolve-GitRevision $ControlRevision
            $declared += [ordered]@{
                variant_id = "control"
                benchmark_variant = "historical_control"
                revision = $controlSha
            }
            for ($index = 0; $index -lt $CandidateRevision.Count; $index++) {
                $declared += [ordered]@{
                    variant_id = "candidate-$([char]([int][char]'a' + $index))"
                    benchmark_variant = "release_candidate"
                    revision = Resolve-GitRevision $CandidateRevision[$index]
                }
            }
        } else {
            $declared += [ordered]@{
                variant_id = "current"
                benchmark_variant = "current_runtime"
                revision = Resolve-GitRevision $RuntimeRevision
            }
        }

        $dockerfile = Join-Path $RepositoryRoot "Dockerfile"
        $evalDockerfile = Join-Path $RepositoryRoot "Dockerfile.eval"
        $provenanceRoot = Join-Path $BundleRoot "provenance"
        New-Item -ItemType Directory -Force -Path $provenanceRoot | Out-Null
        $images = @()
        $variants = @()
        foreach ($runtime in $declared) {
            $runtimePath = Join-Path $BundleRoot "worktrees/$($runtime.variant_id)"
            Ensure-Worktree $runtimePath $runtime.revision
            $runtimeRole = "$($runtime.variant_id)-runtime"
            $evalRole = "$($runtime.variant_id)-eval"
            $runtimeTag = "kira-context-$($runtime.variant_id):$($runtime.revision.Substring(0, 12))"
            $evalTag = "kira-context-$($runtime.variant_id)-eval:$($harnessSha.Substring(0, 12))-$($runtime.revision.Substring(0, 12))"
            Invoke-Checked docker @("build", "--pull=false", "-f", $dockerfile,
                "--build-arg", "SOURCE_REVISION=$($runtime.revision)",
                "--build-arg", "HARNESS_REVISION=$harnessSha",
                "--build-arg", "BENCHMARK_VARIANT=$($runtime.benchmark_variant)",
                "--build-arg", "BENCHMARK_ROLE=$runtimeRole",
                "-t", $runtimeTag, $runtimePath)
            Invoke-Checked docker @("build", "--pull=false", "-f", $evalDockerfile,
                "--build-context", "variant_source=$runtimePath",
                "--build-arg", "SOURCE_REVISION=$harnessSha",
                "--build-arg", "RUNTIME_REVISION=$($runtime.revision)",
                "--build-arg", "HARNESS_REVISION=$harnessSha",
                "--build-arg", "BENCHMARK_VARIANT=$($runtime.benchmark_variant)",
                "--build-arg", "BENCHMARK_ROLE=$evalRole",
                "-t", $evalTag, $RepositoryRoot)
            $metadata = Get-EvalMetadata $evalTag $runtime.revision $harnessSha
            $provenance = [ordered]@{
                variant = $runtime.benchmark_variant
                runtime = [ordered]@{ sha = $runtime.revision; dirty = $false }
                harness = [ordered]@{ sha = $harnessSha; dirty = $false }
                prompt_sha256 = $metadata.prompt_sha256
                package_versions = $metadata.package_versions
            }
            if ($runtime.benchmark_variant -eq "release_candidate") {
                $provenance.candidate = [ordered]@{
                    candidate_id = $runtime.variant_id
                    control_runtime_sha = $controlSha
                    change_scopes = @($CandidateChangeScope)
                    summary = $CandidateSummary
                }
            }
            [System.IO.File]::WriteAllText(
                (Join-Path $provenanceRoot "$($runtime.variant_id).json"),
                ($provenance | ConvertTo-Json -Depth 6),
                [System.Text.UTF8Encoding]::new($false)
            )
            $images += Get-ImageRecord $runtimeRole $runtimeTag $runtime.revision $runtime.revision $harnessSha $runtime.benchmark_variant
            $images += Get-ImageRecord $evalRole $evalTag $harnessSha $runtime.revision $harnessSha $runtime.benchmark_variant
            $variants += [ordered]@{
                variant_id = $runtime.variant_id
                benchmark_variant = $runtime.benchmark_variant
                runtime_revision = $runtime.revision
                runtime_role = $runtimeRole
                eval_role = $evalRole
                provenance_file = "provenance/$($runtime.variant_id).json"
                metadata = $metadata
            }
        }
        $images += Get-ImageRecord "postgres-dependency" "pgvector/pgvector:0.8.6-pg16-bookworm" "" "" "" "dependency"
        $manifest = [ordered]@{
            schema_version = 2
            contract_id = $ContractId
            created_at = [DateTime]::UtcNow.ToString("o")
            harness_revision = $harnessSha
            variants = $variants
            pc_preflight_schema_version = 2
            pc_preflight_run_set_schema_version = 1
            benchmark_artifact_root = "artifacts/benchmark/benchmark"
            images = $images
        }
        if ($CompareHistorical) {
            $manifest.control_revision = $controlSha
            $manifest.candidates = @($declared | Where-Object { $_.benchmark_variant -eq "release_candidate" } | ForEach-Object {
                [ordered]@{ variant_id = $_.variant_id; runtime_revision = $_.revision }
            })
        }
        $manifestJson = $manifest | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText(
            $ManifestPath,
            $manifestJson,
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Output "PASS handoff images built and provenance manifest written"
    }
    "Export" {
        $manifest = Read-Manifest
        $currentHarnessSha = Resolve-GitRevision "HEAD"
        $dirty = & git -C $RepositoryRoot status --porcelain=v1
        if ($dirty -or $currentHarnessSha -ne $manifest.harness_revision) {
            throw "Export checkout differs from the exact accepted harness revision"
        }
        $archive = Join-Path $BundleRoot "kira-benchmark-images.tar"
        $bundleManifestPath = Join-Path $BundleRoot "bundle-manifest.json"
        $evidencePath = Join-Path $BundleRoot "handoff-evidence.json"
        $copiedPreflight = Join-Path $BundleRoot "pc-preflight.json"
        $copiedAcceptance = Join-Path $BundleRoot "pc-acceptance.json"
        $copiedDatasetManifest = Join-Path $BundleRoot "dataset-manifest.json"
        foreach ($outputPath in @(
            $archive, "$archive.sha256", $bundleManifestPath, $evidencePath,
            $copiedPreflight, $copiedAcceptance, $copiedDatasetManifest
        )) {
            if (Test-Path -LiteralPath $outputPath) {
                throw "Handoff export is create-only"
            }
        }
        Copy-NewFile $ResolvedPcPreflightPath $copiedPreflight
        Copy-NewFile $ResolvedPcAcceptancePath $copiedAcceptance
        Copy-NewFile $DatasetManifestPath $copiedDatasetManifest

        $evalRole = $manifest.variants[0].eval_role
        $evalImage = ($manifest.images | Where-Object { $_.role -eq $evalRole }).reference
        Invoke-Checked docker @(
            "run", "--rm", "--network", "none",
            "--mount", "type=bind,src=$BundleRoot,dst=/handoff",
            "--entrypoint", "python", $evalImage,
            "-m", "scripts.benchmark.freeze_handoff",
            "--dataset-root", "/app/dataset/kira_ltm_v1",
            "--image-manifest", "/handoff/image-manifest.json",
            "--pc-preflight", "/handoff/pc-preflight.json",
            "--pc-acceptance", "/handoff/pc-acceptance.json",
            "--output", "/handoff/handoff-evidence.json"
        )
        $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
        if ($evidence.dataset_manifest_sha256 -ne (Get-Sha256 $copiedDatasetManifest)) {
            throw "Host dataset manifest differs from the accepted eval image"
        }
        Assert-ManifestImagesUnchanged $manifest
        $references = @($manifest.images | ForEach-Object { $_.reference })
        $saveArguments = @("save", "--output", $archive) + $references
        Invoke-Checked docker $saveArguments
        $checksum = Get-Sha256 $archive
        Set-Content -LiteralPath "$archive.sha256" -Value "$checksum  kira-benchmark-images.tar" -Encoding ascii
        $copiedFiles = @(
            @{ source = $ComposeFile; name = "compose.benchmark.yaml" },
            @{ source = (Join-Path $RepositoryRoot "evaluation/benchmark.internal.env.example"); name = "benchmark.internal.env.example" },
            @{ source = (Join-Path $RepositoryRoot "docs/company-pc-ai-handoff.md"); name = "RUNBOOK.md" },
            @{ source = (Join-Path $RepositoryRoot "docs/benchmark-k8s-acceptance.md"); name = "K8S-RUNBOOK.md" }
        )
        foreach ($file in $copiedFiles) {
            Copy-NewFile $file.source (Join-Path $BundleRoot $file.name)
        }
        $bundleFileNames = @(
            "kira-benchmark-images.tar", "kira-benchmark-images.tar.sha256", "image-manifest.json",
            "handoff-evidence.json", "pc-preflight.json", "pc-acceptance.json",
            "dataset-manifest.json", "compose.benchmark.yaml", "benchmark.internal.env.example",
            "RUNBOOK.md", "K8S-RUNBOOK.md"
        )
        $bundleFileNames += @($manifest.variants | ForEach-Object { $_.provenance_file })
        $bundleFiles = $bundleFileNames | ForEach-Object {
            [ordered]@{ name = $_; sha256 = Get-Sha256 (Join-Path $BundleRoot $_) }
        }
        $bundleManifest = [ordered]@{
            schema_version = 1
            contract_id = $ContractId
            created_at = [DateTime]::UtcNow.ToString("o")
            official = $false
            image_count = @($manifest.images).Count
            variant_count = @($manifest.variants).Count
            files = @($bundleFiles)
        }
        [System.IO.File]::WriteAllText(
            $bundleManifestPath,
            ($bundleManifest | ConvertTo-Json -Depth 5),
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Output "PASS offline image archive exported"
    }
    "Import" {
        $archive = Join-Path $BundleRoot "kira-benchmark-images.tar"
        $checksumFile = "$archive.sha256"
        $bundleManifestPath = Join-Path $BundleRoot "bundle-manifest.json"
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf) -or
            -not (Test-Path -LiteralPath $checksumFile -PathType Leaf) -or
            -not (Test-Path -LiteralPath $bundleManifestPath -PathType Leaf)) {
            throw "Offline bundle is incomplete"
        }
        $bundleManifest = Get-Content -LiteralPath $bundleManifestPath -Raw | ConvertFrom-Json
        if ($bundleManifest.contract_id -ne $ContractId -or $bundleManifest.official -ne $false) {
            throw "Offline bundle identity is invalid"
        }
        Assert-BundleFileHashes $bundleManifest
        $expected = ((Get-Content -LiteralPath $checksumFile -Raw).Split()[0]).ToLowerInvariant()
        $actual = Get-Sha256 $archive
        if ($actual -ne $expected) {
            throw "Offline image archive checksum mismatch"
        }
        Invoke-Checked docker @("load", "--input", $archive)
        $manifest = Read-Manifest
        foreach ($image in $manifest.images) {
            $inspect = (& docker image inspect $image.reference | ConvertFrom-Json)[0]
            if ($LASTEXITCODE -ne 0 -or $inspect.Id -ne $image.image_id) {
                throw "Loaded image identity differs from manifest"
            }
        }
        Write-Output "PASS offline images loaded and verified"
    }
    "MockAcceptance" {
        $manifest = Read-Manifest
        $output = Join-Path $BundleRoot "mock-acceptance"
        New-Item -ItemType Directory -Force -Path $output | Out-Null
        foreach ($variant in $manifest.variants) {
            $evalImage = ($manifest.images | Where-Object { $_.role -eq $variant.eval_role }).reference
            $variantOutput = Join-Path $output $variant.variant_id
            if (Test-Path -LiteralPath $variantOutput) {
                throw "Mock acceptance output already exists for a declared variant"
            }
            New-Item -ItemType Directory -Force -Path $variantOutput | Out-Null
            Invoke-Checked docker @("run", "--rm", "--network", "none",
                "--mount", "type=bind,src=$variantOutput,dst=/artifacts",
                "--entrypoint", "python", $evalImage,
                "-m", "scripts.benchmark.mock_acceptance", "--output", "/artifacts")
        }
        Write-Output "PASS offline mock acceptance completed for every declared eval image"
    }
    "Validate" {
        Invoke-Compose @("config", "--quiet")
        Write-Output "PASS internal compose configuration is valid"
    }
    "StartCurrent" {
        $manifest = Read-Manifest
        $variant = @($manifest.variants | Where-Object { $_.variant_id -eq "current" })
        if ($variant.Count -ne 1 -or $variant[0].benchmark_variant -ne "current_runtime") {
            throw "Current runtime is not declared in the image manifest"
        }
        # Reuse the candidate Compose stack; no second DB/collection is needed for current-only runs.
        $env:BENCHMARK_CANDIDATE_IMAGE = ($manifest.images | Where-Object { $_.role -eq "current-runtime" }).reference
        $env:BENCHMARK_EVAL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "current-eval" }).reference
        Invoke-Compose @("--profile", "candidate", "up", "-d", "candidate-postgres")
        Invoke-Compose @("--profile", "candidate", "run", "--rm", "candidate-migrate")
        Invoke-Compose @("--profile", "candidate", "run", "--rm", "candidate-memory-init")
        Invoke-Compose @("--profile", "candidate", "up", "-d", "candidate-worker", "candidate-gateway")
        Write-Output "PASS current runtime stack started sequentially"
    }
    "StartControl" {
        $manifest = Read-Manifest
        $env:BENCHMARK_CONTROL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "control-runtime" }).reference
        $env:BENCHMARK_EVAL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "control-eval" }).reference
        Invoke-Compose @("--profile", "control", "up", "-d", "control-postgres")
        Invoke-Compose @("--profile", "control", "run", "--rm", "control-migrate")
        Invoke-Compose @("--profile", "control", "run", "--rm", "control-memory-init")
        Invoke-Compose @("--profile", "control", "up", "-d", "control-worker", "control-gateway")
        Write-Output "PASS control stack started sequentially"
    }
    "StartCandidate" {
        $manifest = Read-Manifest
        $variant = @($manifest.variants | Where-Object { $_.variant_id -eq $VariantId })
        if ($variant.Count -ne 1 -or $variant[0].benchmark_variant -ne "release_candidate") {
            throw "Requested candidate is not declared in the image manifest"
        }
        $env:BENCHMARK_CANDIDATE_IMAGE = ($manifest.images | Where-Object { $_.role -eq "$VariantId-runtime" }).reference
        $env:BENCHMARK_EVAL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "$VariantId-eval" }).reference
        Invoke-Compose @("--profile", "candidate", "up", "-d", "candidate-postgres")
        Invoke-Compose @("--profile", "candidate", "run", "--rm", "candidate-migrate")
        Invoke-Compose @("--profile", "candidate", "run", "--rm", "candidate-memory-init")
        Invoke-Compose @("--profile", "candidate", "up", "-d", "candidate-worker", "candidate-gateway")
        Write-Output "PASS $VariantId stack started sequentially"
    }
    "Stop" {
        Invoke-Compose @("down", "--remove-orphans")
        Write-Output "PASS benchmark containers stopped; volumes retained"
    }
    "Publish" {
        if (-not $Registry) {
            throw "Publish requires -Registry"
        }
        if ($Registry -match '^https?://' -or $Registry -match '[\s@]') {
            throw "Registry must be a bare host/path without scheme, digest or whitespace"
        }
        if (Test-Path -LiteralPath $RegistryManifestPath) {
            throw "Registry manifest already exists; publish evidence is create-only"
        }
        $manifest = Read-Manifest
        Assert-ManifestImagesUnchanged $manifest
        $published = @()
        foreach ($image in $manifest.images | Where-Object { $_.role -ne "postgres-dependency" }) {
            $sourceSuffix = $image.source_revision.Substring(0, 12)
            $runtimeSuffix = $image.runtime_revision.Substring(0, 12)
            $target = "$($Registry.TrimEnd('/'))/kira/$($image.role):$sourceSuffix-$runtimeSuffix"
            Invoke-Checked docker @("tag", $image.reference, $target)
            Invoke-Checked docker @("push", $target)
            $inspect = (& docker image inspect $target | ConvertFrom-Json)[0]
            if ($LASTEXITCODE -ne 0 -or $inspect.Id -ne $image.image_id) {
                throw "Published image identity differs from the accepted source image"
            }
            $tagSeparator = $target.LastIndexOf(':')
            $lastSlash = $target.LastIndexOf('/')
            if ($tagSeparator -le $lastSlash) {
                throw "Published image target has no immutable tag"
            }
            $repository = $target.Substring(0, $tagSeparator)
            $digests = @($inspect.RepoDigests | Where-Object { $_ -like "$repository@sha256:*" })
            if ($digests.Count -ne 1 -or $digests[0] -notmatch '@sha256:[a-f0-9]{64}$') {
                throw "Registry did not return one immutable digest for the published image"
            }
            $published += [ordered]@{
                role = $image.role
                variant = $image.variant
                source_reference = $image.reference
                source_image_id = $image.image_id
                pushed_tag = $target
                immutable_reference = $digests[0]
            }
        }
        $registryManifest = [ordered]@{
            schema_version = 1
            contract_id = $ContractId
            created_at = [DateTime]::UtcNow.ToString("o")
            source_image_manifest_sha256 = Get-Sha256 $ManifestPath
            image_count = $published.Count
            images = @($published)
        }
        [System.IO.File]::WriteAllText(
            $RegistryManifestPath,
            ($registryManifest | ConvertTo-Json -Depth 6),
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Output "PASS benchmark images published with immutable registry digest evidence"
    }
}
