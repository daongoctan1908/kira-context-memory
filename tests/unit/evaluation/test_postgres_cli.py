"""Read-only PostgreSQL behavior and safe command-line reporting."""

import json
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest

from evaluation.config import EvalConfig
from evaluation.errors import PreflightError
from evaluation.models import Probe, Reason
from evaluation.postgres import probe_database
from scripts.run_week5_benchmark import main


def install_connection(monkeypatch, *, ones=((1,), ("0.8.6",)), many=()):
    cursor = AsyncMock()
    cursor.fetchone.side_effect = ones
    cursor.fetchall.side_effect = many
    cursor_cm = MagicMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=False)
    connection = MagicMock()
    connection.cursor.return_value = cursor_cm
    connection_cm = MagicMock()
    connection_cm.__aenter__ = AsyncMock(return_value=connection)
    connection_cm.__aexit__ = AsyncMock(return_value=False)
    connect = AsyncMock(return_value=connection_cm)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    return connect, cursor, connection_cm


def db_config():
    return EvalConfig(
        database_url="postgresql+asyncpg://u:secret@local/eval",
        memory_database_url="postgresql://u:secret@local/eval",
        embedding={"model": "embed"},
    )


@pytest.mark.parametrize("probe", [Probe.PGVECTOR, Probe.MEMORY_SCHEMA, Probe.CONVERSATION_DB])
async def test_database_queries_read_only_and_connection_closed(monkeypatch, probe):
    rows = (
        [("20260908_0003",)]
        if probe == Probe.CONVERSATION_DB
        else [(2, "2.0.20+viettel.3", "embed", 3)]
    )
    connect, cursor, connection_cm = install_connection(monkeypatch, many=[rows])
    assert await probe_database(db_config(), probe, 3) == {}
    args, kwargs = connect.call_args
    assert "asyncpg" not in args[0]
    assert "default_transaction_read_only=on" in kwargs["options"]
    assert "statement_timeout=2000" in kwargs["options"]
    assert kwargs["autocommit"] is True
    connection_cm.__aexit__.assert_awaited_once()
    for call in cursor.execute.call_args_list:
        statement = call.args[0]
        statement = statement if isinstance(statement, str) else statement.as_string()
        assert statement.startswith("SELECT")


@pytest.mark.parametrize(
    "probe,ones,many,reason",
    [
        (Probe.PGVECTOR, [(1,), None], [], Reason.EXTENSION_MISSING),
        (Probe.PGVECTOR, [(2,)], [], Reason.DATABASE),
        (
            Probe.MEMORY_SCHEMA,
            [(1,), ("0.8.6",)],
            [[(1, "old", "embed", 3)]],
            Reason.SCHEMA_MISMATCH,
        ),
        (Probe.CONVERSATION_DB, [(1,)], [[("old",)]], Reason.SCHEMA_MISMATCH),
    ],
)
async def test_schema_mismatch_and_missing_extension(monkeypatch, probe, ones, many, reason):
    install_connection(monkeypatch, ones=ones, many=many)
    with pytest.raises(PreflightError) as captured:
        await probe_database(db_config(), probe, 3)
    assert captured.value.reason == reason


@pytest.mark.parametrize(
    "error,reason",
    [
        (psycopg.errors.QueryCanceled, Reason.TIMEOUT),
        (psycopg.errors.UndefinedTable, Reason.SCHEMA_MISMATCH),
        (psycopg.errors.UndefinedColumn, Reason.SCHEMA_MISMATCH),
        (psycopg.OperationalError, Reason.CONNECTION),
        (psycopg.ProgrammingError, Reason.DATABASE),
        (OSError, Reason.CONNECTION),
        (ValueError, Reason.DATABASE),
    ],
)
async def test_database_errors_have_no_dsn_or_raw_message(monkeypatch, error, reason):
    monkeypatch.setattr(
        psycopg.AsyncConnection,
        "connect",
        AsyncMock(side_effect=error("postgresql://u:secret@local/eval")),
    )
    with pytest.raises(PreflightError) as captured:
        await probe_database(db_config(), Probe.PGVECTOR)
    assert captured.value.reason == reason
    assert "secret" not in str(captured.value)
    assert captured.value.__suppress_context__


def test_cli_mock_creates_safe_artifact_and_wont_overwrite(tmp_path, capsys):
    output = tmp_path / "reports" / "preflight.json"
    args = ["preflight", "--profile", "mock", "--suite", "cross_session", "--output", str(output)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["simulated"] is True
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert main(args) == 2
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert "configuration_error" in capsys.readouterr().out


def test_cli_missing_config_and_bad_secret_are_safe(tmp_path, monkeypatch, capsys):
    for key in list(__import__("os").environ):
        if key.startswith("WEEK5_") or key == "OPENAI_API_KEY":
            monkeypatch.delenv(key)
    assert main(["preflight", "--profile", "internal_test", "--suite", "rewrite"]) == 1
    assert "NOT_RUN" in capsys.readouterr().out
    path = tmp_path / "bad.env"
    path.write_text("WEEK5_READ_TIMEOUT_SECONDS=sk-synthetic-key\n", encoding="utf-8")
    assert (
        main(
            [
                "preflight",
                "--profile",
                "external_synthetic",
                "--suite",
                "rewrite",
                "--env-file",
                str(path),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "sk-synthetic-key" not in captured.out + captured.err


def test_cli_unexpected_runtime_error_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(
        "scripts.run_week5_benchmark.run_preflight",
        AsyncMock(side_effect=RuntimeError("sk-synthetic-key")),
    )
    assert main(["preflight", "--profile", "mock", "--suite", "formation"]) == 2
    assert "sk-synthetic-key" not in capsys.readouterr().out


def test_file_only_ignores_inherited_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-unwanted-ambient")
    monkeypatch.setenv("WEEK5_OPENAI_BASE_URL", "https://unwanted.test/v1")
    monkeypatch.setenv("WEEK5_OPENAI_CHAT_MODEL", "unwanted-model")
    path = tmp_path / ".env.eval"
    path.write_text("# Intentionally no provider configured\n", encoding="utf-8")
    assert (
        main(
            [
                "preflight",
                "--profile",
                "external_synthetic",
                "--suite",
                "rewrite",
                "--env-file",
                str(path),
                "--env-file-only",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][0]["outcome"] == "NOT_RUN"
    assert report["config_source"] == "file_only"
    assert "unwanted" not in json.dumps(report)
    assert main(["preflight", "--suite", "rewrite", "--env-file-only"]) == 2
    assert "configuration_error" in capsys.readouterr().out
