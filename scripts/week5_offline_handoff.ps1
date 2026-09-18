[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Build", "Export", "Import", "MockAcceptance", "Validate", "StartControl", "StartCandidate", "Stop", "Publish")]
    [string]$Action,
    [ValidateCount(1, 2)]
    [string[]]$CandidateRevision = @("HEAD"),
    [ValidateSet("prompt", "config", "runtime_code", "dependencies", "schema", "lifecycle")]
    [string[]]$CandidateChangeScope = @("runtime_code"),
    [string]$CandidateSummary = "Declared Week 5 benchmark candidate.",
    [ValidatePattern('^candidate-[ab]$')]
    [string]$VariantId = "candidate-a",
    [string]$BundleDirectory = "artifacts/week5/offline-handoff",
    [string]$EnvFile = ".env.week5.internal.local",
    [string]$PcPreflightPath = "artifacts/week5/pc-preflight/freeze.json",
    [string]$PcAcceptancePath = "artifacts/week5/pc-openai/pc-acceptance.json",
    [string]$Registry = ""
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot

function Resolve-InputPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepositoryRoot $Path))
}

$ControlRevision = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"
$ContractId = "kira-week5-benchmark-v4"
$BundleRoot = Resolve-InputPath $BundleDirectory
$ManifestPath = Join-Path $BundleRoot "image-manifest.json"
$ComposeFile = Join-Path $RepositoryRoot "compose.week5.benchmark.yaml"
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
        -m scripts.benchmark_image_metadata
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
    $env:WEEK5_ENV_FILE = $ResolvedEnvFile
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
        if (-not $CandidateSummary.Trim()) {
            throw "Candidate summary must not be blank"
        }
        $controlSha = Resolve-GitRevision $ControlRevision
        $harnessSha = Resolve-GitRevision "HEAD"
        $controlPath = Join-Path $BundleRoot "worktrees/control"
        Ensure-Worktree $controlPath $controlSha

        $candidates = @()
        for ($index = 0; $index -lt $CandidateRevision.Count; $index++) {
            $candidateId = "candidate-$([char]([int][char]'a' + $index))"
            $candidateSha = Resolve-GitRevision $CandidateRevision[$index]
            $candidatePath = Join-Path $BundleRoot "worktrees/$candidateId"
            Ensure-Worktree $candidatePath $candidateSha
            $candidates += [ordered]@{
                variant_id = $candidateId
                revision = $candidateSha
                path = $candidatePath
            }
        }

        $controlTag = "kira-context-control:$($controlSha.Substring(0, 12))"
        $controlEvalTag = "kira-context-control-eval:$($harnessSha.Substring(0, 12))-$($controlSha.Substring(0, 12))"
        $dockerfile = Join-Path $RepositoryRoot "Dockerfile"
        $evalDockerfile = Join-Path $RepositoryRoot "Dockerfile.eval"

        Invoke-Checked docker @("build", "--pull=false", "-f", $dockerfile,
            "--build-arg", "SOURCE_REVISION=$controlSha",
            "--build-arg", "HARNESS_REVISION=$harnessSha",
            "--build-arg", "BENCHMARK_VARIANT=historical_control",
            "--build-arg", "BENCHMARK_ROLE=control-runtime",
            "-t", $controlTag, $controlPath)
        Invoke-Checked docker @("build", "--pull=false", "-f", $evalDockerfile,
            "--build-context", "variant_source=$controlPath",
            "--build-arg", "SOURCE_REVISION=$harnessSha",
            "--build-arg", "RUNTIME_REVISION=$controlSha",
            "--build-arg", "HARNESS_REVISION=$harnessSha",
            "--build-arg", "BENCHMARK_VARIANT=historical_control",
            "--build-arg", "BENCHMARK_ROLE=control-eval",
            "-t", $controlEvalTag, $RepositoryRoot)

        $postgresRef = "pgvector/pgvector:0.8.6-pg16-bookworm"
        $controlMetadata = Get-EvalMetadata $controlEvalTag $controlSha $harnessSha
        $provenanceRoot = Join-Path $BundleRoot "provenance"
        New-Item -ItemType Directory -Force -Path $provenanceRoot | Out-Null
        $controlProvenance = [ordered]@{
            variant = "historical_control"
            runtime = [ordered]@{ sha = $controlSha; dirty = $false }
            harness = [ordered]@{ sha = $harnessSha; dirty = $false }
            prompt_sha256 = $controlMetadata.prompt_sha256
            package_versions = $controlMetadata.package_versions
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $provenanceRoot "control.json"),
            ($controlProvenance | ConvertTo-Json -Depth 6),
            [System.Text.UTF8Encoding]::new($false)
        )
        $images = @(
            Get-ImageRecord "control-runtime" $controlTag $controlSha $controlSha $harnessSha "historical_control"
            Get-ImageRecord "control-eval" $controlEvalTag $harnessSha $controlSha $harnessSha "historical_control"
        )
        $variants = @(
            [ordered]@{
                variant_id = "control"
                benchmark_variant = "historical_control"
                runtime_revision = $controlSha
                runtime_role = "control-runtime"
                eval_role = "control-eval"
                provenance_file = "provenance/control.json"
                metadata = $controlMetadata
            }
        )
        foreach ($candidate in $candidates) {
            $candidateTag = "kira-context-$($candidate.variant_id):$($candidate.revision.Substring(0, 12))"
            $candidateEvalTag = "kira-context-$($candidate.variant_id)-eval:$($harnessSha.Substring(0, 12))-$($candidate.revision.Substring(0, 12))"
            Invoke-Checked docker @("build", "--pull=false", "-f", $dockerfile,
                "--build-arg", "SOURCE_REVISION=$($candidate.revision)",
                "--build-arg", "HARNESS_REVISION=$harnessSha",
                "--build-arg", "BENCHMARK_VARIANT=release_candidate",
                "--build-arg", "BENCHMARK_ROLE=$($candidate.variant_id)-runtime",
                "-t", $candidateTag, $candidate.path)
            Invoke-Checked docker @("build", "--pull=false", "-f", $evalDockerfile,
                "--build-context", "variant_source=$($candidate.path)",
                "--build-arg", "SOURCE_REVISION=$harnessSha",
                "--build-arg", "RUNTIME_REVISION=$($candidate.revision)",
                "--build-arg", "HARNESS_REVISION=$harnessSha",
                "--build-arg", "BENCHMARK_VARIANT=release_candidate",
                "--build-arg", "BENCHMARK_ROLE=$($candidate.variant_id)-eval",
                "-t", $candidateEvalTag, $RepositoryRoot)
            $candidateMetadata = Get-EvalMetadata $candidateEvalTag $candidate.revision $harnessSha
            $candidateProvenance = [ordered]@{
                variant = "release_candidate"
                runtime = [ordered]@{ sha = $candidate.revision; dirty = $false }
                harness = [ordered]@{ sha = $harnessSha; dirty = $false }
                prompt_sha256 = $candidateMetadata.prompt_sha256
                package_versions = $candidateMetadata.package_versions
                candidate = [ordered]@{
                    candidate_id = $candidate.variant_id
                    control_runtime_sha = $controlSha
                    change_scopes = @($CandidateChangeScope)
                    summary = $CandidateSummary
                }
            }
            [System.IO.File]::WriteAllText(
                (Join-Path $provenanceRoot "$($candidate.variant_id).json"),
                ($candidateProvenance | ConvertTo-Json -Depth 6),
                [System.Text.UTF8Encoding]::new($false)
            )
            $images += Get-ImageRecord "$($candidate.variant_id)-runtime" $candidateTag $candidate.revision $candidate.revision $harnessSha "release_candidate"
            $images += Get-ImageRecord "$($candidate.variant_id)-eval" $candidateEvalTag $harnessSha $candidate.revision $harnessSha "release_candidate"
            $variants += [ordered]@{
                variant_id = $candidate.variant_id
                benchmark_variant = "release_candidate"
                runtime_revision = $candidate.revision
                runtime_role = "$($candidate.variant_id)-runtime"
                eval_role = "$($candidate.variant_id)-eval"
                provenance_file = "provenance/$($candidate.variant_id).json"
                metadata = $candidateMetadata
            }
        }
        $images += Get-ImageRecord "postgres-dependency" $postgresRef "" "" "" "dependency"
        $manifest = [ordered]@{
            schema_version = 2
            contract_id = $ContractId
            created_at = [DateTime]::UtcNow.ToString("o")
            control_revision = $controlSha
            harness_revision = $harnessSha
            candidates = @($candidates | ForEach-Object {
                [ordered]@{ variant_id = $_.variant_id; runtime_revision = $_.revision }
            })
            variants = $variants
            materialization_checkpoint = "artifacts/week5/kira-materialization.json"
            benchmark_artifact_root = "artifacts/week5/benchmark"
            images = $images
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
        $archive = Join-Path $BundleRoot "kira-week5-images.tar"
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

        $evalImage = ($manifest.images | Where-Object { $_.role -eq "control-eval" }).reference
        Invoke-Checked docker @(
            "run", "--rm", "--network", "none",
            "--mount", "type=bind,src=$BundleRoot,dst=/handoff",
            "--entrypoint", "python", $evalImage,
            "-m", "scripts.freeze_handoff",
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
        Set-Content -LiteralPath "$archive.sha256" -Value "$checksum  kira-week5-images.tar" -Encoding ascii
        $copiedFiles = @(
            @{ source = $ComposeFile; name = "compose.week5.benchmark.yaml" },
            @{ source = (Join-Path $RepositoryRoot "evaluation/week5.internal.env.example"); name = "week5.internal.env.example" },
            @{ source = (Join-Path $RepositoryRoot "docs/week5-offline-handoff.md"); name = "RUNBOOK.md" },
            @{ source = (Join-Path $RepositoryRoot "docs/company-pc-ai-handoff.md"); name = "COMPANY-PC-AI-HANDOFF.md" }
        )
        foreach ($file in $copiedFiles) {
            Copy-NewFile $file.source (Join-Path $BundleRoot $file.name)
        }
        $bundleFileNames = @(
            "kira-week5-images.tar", "kira-week5-images.tar.sha256", "image-manifest.json",
            "handoff-evidence.json", "pc-preflight.json", "pc-acceptance.json",
            "dataset-manifest.json", "compose.week5.benchmark.yaml", "week5.internal.env.example",
            "RUNBOOK.md", "COMPANY-PC-AI-HANDOFF.md"
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
        $archive = Join-Path $BundleRoot "kira-week5-images.tar"
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
                "-m", "scripts.week5_mock_acceptance", "--output", "/artifacts")
        }
        Write-Output "PASS offline mock acceptance completed for every declared eval image"
    }
    "Validate" {
        Invoke-Compose @("config", "--quiet")
        Write-Output "PASS internal compose configuration is valid"
    }
    "StartControl" {
        $manifest = Read-Manifest
        $env:WEEK5_CONTROL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "control-runtime" }).reference
        $env:WEEK5_EVAL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "control-eval" }).reference
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
        $env:WEEK5_CANDIDATE_IMAGE = ($manifest.images | Where-Object { $_.role -eq "$VariantId-runtime" }).reference
        $env:WEEK5_EVAL_IMAGE = ($manifest.images | Where-Object { $_.role -eq "$VariantId-eval" }).reference
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
        $manifest = Read-Manifest
        foreach ($image in $manifest.images | Where-Object { $_.role -ne "postgres-dependency" }) {
            $sourceSuffix = $image.source_revision.Substring(0, 12)
            $runtimeSuffix = $image.runtime_revision.Substring(0, 12)
            $target = "$($Registry.TrimEnd('/'))/kira/$($image.role):$sourceSuffix-$runtimeSuffix"
            Invoke-Checked docker @("tag", $image.reference, $target)
            Invoke-Checked docker @("push", $target)
        }
        Write-Output "PASS benchmark images published to the internal registry"
    }
}
