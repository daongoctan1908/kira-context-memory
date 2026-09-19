from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_keeps_backend_coverage_postgres_and_offline_harness_gates() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "uv run ruff check ." in workflow
    assert "uv run ruff format --check ." in workflow
    assert "uv run pytest" in workflow
    assert "--cov-fail-under=90" in project
    assert "POSTGRES_TEST_URL:" in workflow
    assert "pgvector/pgvector:0.8.6-pg16-bookworm" in workflow
    assert "scripts.validate_dataset dataset/kira_ltm_v1 --json" in workflow
    assert "scripts.week5_mock_acceptance" in workflow


def test_ci_never_enables_live_or_external_quality_evaluation() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'RUN_MEMORY_POLICY_EVAL: "0"' in workflow
    assert 'RUN_CROSS_SESSION_EVAL: "0"' in workflow
    for forbidden in (
        "OPENAI_API_KEY",
        "KIRA_BASE_URL",
        "VLLM_BASE_URL",
        "MEMORY_LLM_BASE_URL",
        "run_week5_benchmark",
    ):
        assert forbidden not in workflow


def test_ci_checks_frontend_browser_product_images_and_e2e() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for command in (
        "pnpm install --frozen-lockfile",
        "pnpm lint",
        "pnpm typecheck",
        "pnpm test",
        "pnpm build",
        "playwright install --with-deps chromium",
        "pnpm test:e2e",
        "docker compose -f compose.product.yaml up -d --build --wait",
        "uv run python -m scripts.smoke_product_e2e",
        "down --volumes --remove-orphans",
    ):
        assert command in workflow

    assert "needs: [backend, frontend]" in workflow
    assert "permissions:\n  contents: read" in workflow


def test_ci_pins_language_and_package_manager_contracts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("actions/checkout@v7") == 3
    assert workflow.count("actions/setup-python@v7") == 2
    assert "actions/setup-node@v7" in workflow
    assert workflow.count("python -m pip install uv==0.11.2") == 2
    assert "corepack prepare pnpm@10.17.1 --activate" in workflow
