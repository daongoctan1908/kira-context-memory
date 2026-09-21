"""CLI and KiRa stream collection tests for dataset materialization."""

from datetime import UTC, datetime

import pytest

from app.domain.models.kira import KiraEventKind, KiraStreamEvent
from evaluation.dataset import default_dataset_root
from evaluation.materialization import completed_task, replace_checkpoint_task
from scripts.benchmark import materialize_dataset
from scripts.benchmark.materialize_dataset import collect_kira_text, main


class FakeKiraAdapter:
    def __init__(self, events: tuple[KiraStreamEvent, ...]) -> None:
        self.events = events

    async def chat_stream(self, message: str):
        async def stream():
            for event in self.events:
                yield event

        return stream()


def _event(text: str | None, request_id: str | None = None) -> KiraStreamEvent:
    return KiraStreamEvent(
        kind=KiraEventKind.TEXT,
        raw_data="{}",
        payload={},
        text_fragment=text,
        request_id=request_id,
        message_id="message-1",
    )


async def test_collect_kira_text_joins_fragments_and_ids():
    response = await collect_kira_text(
        FakeKiraAdapter((_event("  Xin ", "request-1"), _event("chào  ", "request-1"))),  # type: ignore[arg-type]
        "query",
    )

    assert response.text == "Xin chào"
    assert response.request_ids == ("request-1",)
    assert response.message_ids == ("message-1",)


async def test_collect_kira_text_rejects_empty_stream():
    with pytest.raises(ValueError, match="without text"):
        await collect_kira_text(FakeKiraAdapter((_event(None),)), "query")  # type: ignore[arg-type]


def test_plan_cli_reports_exact_workload(capsys):
    assert main(["plan", "--root", str(default_dataset_root())]) == 0
    output = capsys.readouterr().out
    assert "unique_queries=80" in output
    assert "conversation_fills=140" in output
    assert "qa_answers=54" in output


def test_collect_cli_rejects_bad_limits_without_creating_checkpoint(tmp_path, capsys):
    checkpoint = tmp_path / "checkpoint.json"

    assert (
        main(
            [
                "collect",
                "--root",
                str(default_dataset_root()),
                "--checkpoint",
                str(checkpoint),
                "--max-requests",
                "0",
            ]
        )
        == 2
    )

    assert not checkpoint.exists()
    assert "error_class=ValueError" in capsys.readouterr().err


def test_apply_cli_rejects_missing_checkpoint_without_details(tmp_path, capsys):
    missing = tmp_path / "missing.json"

    assert (
        main(
            [
                "apply",
                "--root",
                str(default_dataset_root()),
                "--checkpoint",
                str(missing),
                "--dataset-version",
                "1.0.0-materialized.1",
                "--in-place",
            ]
        )
        == 2
    )

    assert "error_class=FileNotFoundError" in capsys.readouterr().err


def test_preflight_persists_one_request_then_reuses_it(tmp_path, capsys, monkeypatch):
    checkpoint_path = tmp_path / "checkpoint.json"
    calls = 0

    def fake_settings(*args, **kwargs):
        del args, kwargs
        return object()

    async def fake_collect(checkpoint, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["max_requests"] == 1
        assert kwargs["request_delay_seconds"] == 0
        assert kwargs["continue_on_error"] is False
        updated = replace_checkpoint_task(
            checkpoint,
            0,
            completed_task(
                checkpoint.tasks[0],
                "KiRa preflight response",
                now=datetime(2026, 9, 18, tzinfo=UTC),
            ),
            now=datetime(2026, 9, 18, tzinfo=UTC),
        )
        materialize_dataset.write_materialization_checkpoint(kwargs["checkpoint_path"], updated)
        return updated, False

    monkeypatch.setattr(materialize_dataset, "_load_settings", fake_settings)
    monkeypatch.setattr(materialize_dataset, "collect_checkpoint", fake_collect)
    command = [
        "preflight",
        "--root",
        str(default_dataset_root()),
        "--checkpoint",
        str(checkpoint_path),
        "--env-file-only",
    ]

    assert main(command) == 0
    assert "completed=1/80 reused=false" in capsys.readouterr().out
    assert main(command) == 0
    assert "completed=1/80 reused=true" in capsys.readouterr().out
    assert calls == 1
