"""Run the historical formation method; observation must not change its I/O."""

import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from mem0.memory import main as native
from mem0.observability import bind_observer

from evaluation.formation import (
    FormationCaptureObserver,
    FormationExecutionStatus,
    _completed_extraction,
    _safe_lifecycle,
)
from evaluation.models import INSTRUMENTED_CONTROL_SHA
from scripts.backport_observability import CONTROL, backported_sources, main

ROOT = Path(__file__).resolve().parents[3]
MAIN_PATH = "packages/viettel-mem0/mem0/memory/main.py"
TEXT = "Use region North for reports"
FACT = {"text": TEXT, "attributed_to": "user", "scope": "CONVERSATION"}


def _source(revision, path=MAIN_PATH):
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{revision}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def _method(source):
    tree = ast.parse(source)
    namespace = dict(vars(native))
    # Reuse the original receipt/session helpers, not the modern receipt contract.
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {
            "_escape_scope_value",
            "_build_session_scope",
            "_formation_identity",
            "_formation_method",
        }
    ]
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AsyncMemory"
    )
    function = next(
        node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_add_to_vector_store"
    )
    module = ast.fix_missing_locations(ast.Module(body=[*helpers, function], type_ignores=[]))
    exec(compile(module, MAIN_PATH, "exec"), namespace)
    # The I/O and NLP dependencies are deterministic. The extraction, parse,
    # hash dedup and atomic receipt branches are the actual historical code.
    namespace["lemmatize_for_bm25"] = lambda text: text
    namespace["extract_entities_batch"] = lambda texts: [[] for _ in texts]
    namespace["capture_event"] = lambda *args, **kwargs: None
    return namespace["_add_to_vector_store"]


@pytest.fixture(scope="module")
def methods():
    return _method(_source(CONTROL)), _method(backported_sources(ROOT)[MAIN_PATH])


class Store:
    def __init__(self, existing=False, receipt=None):
        self.existing = existing
        self.receipt = receipt
        self.writes = []

    def get_formation_result(self, *args):
        return self.receipt

    def search(self, **kwargs):
        if not self.existing:
            return []
        return [
            SimpleNamespace(
                id=str(UUID(int=3)),
                payload={"data": TEXT, "hash": hashlib.md5(TEXT.encode()).hexdigest()},
            )
        ]

    def insert_with_formation_receipt(self, *args, **kwargs):
        self.writes.append(copy.deepcopy((args, kwargs)))
        self.receipt = copy.deepcopy(kwargs["result"])
        return True, self.receipt


async def _run(method, response, monkeypatch, *, existing=False, receipt=None, observing=True):
    provider_calls = []
    saved_messages = []

    def generate_response(**kwargs):
        provider_calls.append(copy.deepcopy(kwargs))
        return response

    memory = SimpleNamespace(
        llm=SimpleNamespace(generate_response=generate_response),
        embedding_model=SimpleNamespace(
            embed=lambda *args: [0.1, 0.2, 0.3],
            embed_batch=lambda texts, *args: [[0.1, 0.2, 0.3] for _ in texts],
        ),
        db=SimpleNamespace(
            get_last_messages=lambda *args: [],
            save_messages=lambda *args: saved_messages.append(copy.deepcopy(args)),
            batch_add_history=lambda *args: None,
        ),
        vector_store=Store(existing=existing, receipt=receipt),
        custom_instructions=None,
        api_version="v1.1",
    )
    monkeypatch.setattr(native.uuid, "uuid4", lambda: UUID(int=100))
    observer = FormationCaptureObserver()
    with bind_observer(observer if observing else None):
        result = await method(
            memory,
            [{"role": "user", "content": "Please remember my report region"}],
            {
                "formation_event_id": str(UUID(int=2)),
                "user_id": str(UUID(int=1)),
                "created_at": "2026-10-06T00:00:00Z",
            },
            {"user_id": str(UUID(int=1))},
            True,
        )
    business = (result, memory.vector_store.writes, saved_messages, provider_calls)
    return business, observer


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, 0])
@pytest.mark.parametrize("observing", [True, False])
async def test_nonlist_empty_values_preserve_original_control_behavior(
    methods, monkeypatch, value, observing
):
    response = json.dumps({"memory": value})
    original, _ = await _run(methods[0], response, monkeypatch, observing=observing)
    instrumented, observer = await _run(methods[1], response, monkeypatch, observing=observing)

    assert instrumented == original
    assert instrumented[0] == []
    assert len(instrumented[3]) == 1
    if observing:
        index = observer.stage_indexes("mem0.extract.parse")[0]
        assert observer.raw_output(index) is value
        assert observer.captures()[index].attributes["kira.memory.fact_count"] == 0


@pytest.mark.asyncio
async def test_real_control_capture_retains_facts_dropped_by_batch_dedup(methods, monkeypatch):
    response = json.dumps({"memory": [FACT, FACT]})
    original, _ = await _run(methods[0], response, monkeypatch)
    instrumented, observer = await _run(methods[1], response, monkeypatch)
    extraction = _completed_extraction(
        "conv01:formation:M01", observer, _safe_lifecycle({"results": instrumented[0]})
    )

    assert instrumented == original
    assert len(instrumented[0]) == 1
    assert extraction.status is FormationExecutionStatus.VALID_FACTS
    assert [fact.text for fact in extraction.facts] == [TEXT, TEXT]
    assert len(extraction.lifecycle_events) == 1
    assert extraction.provider_calls == 1


@pytest.mark.asyncio
async def test_real_control_distinguishes_empty_parse_from_all_facts_deduplicated(
    methods, monkeypatch
):
    empty, empty_observer = await _run(methods[1], '{"memory":[]}', monkeypatch)
    dropped, dropped_observer = await _run(
        methods[1], json.dumps({"memory": [FACT]}), monkeypatch, existing=True
    )
    empty_extraction = _completed_extraction(
        "conv01:formation:M01", empty_observer, _safe_lifecycle({"results": empty[0]})
    )
    dropped_extraction = _completed_extraction(
        "conv01:formation:M01", dropped_observer, _safe_lifecycle({"results": dropped[0]})
    )

    assert empty[0] == dropped[0] == []
    assert empty_extraction.status is FormationExecutionStatus.VALID_EMPTY
    assert empty_extraction.facts == ()
    assert dropped_extraction.status is FormationExecutionStatus.VALID_FACTS
    assert [fact.text for fact in dropped_extraction.facts] == [TEXT]


@pytest.mark.asyncio
async def test_real_control_receipt_retry_still_skips_provider_and_parse(methods, monkeypatch):
    response = json.dumps({"memory": [FACT]})
    original, _ = await _run(methods[0], response, monkeypatch, receipt=[])
    instrumented, observer = await _run(methods[1], response, monkeypatch, receipt=[])

    assert instrumented == original
    assert instrumented[0] == []
    assert instrumented[3] == []
    assert observer.captures() == ()


def test_pinned_control_source_matches_generator_and_changes_only_observation_files():
    sources = backported_sources(ROOT)
    for path, source in sources.items():
        assert _source(INSTRUMENTED_CONTROL_SHA, path) == source
    changed = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", CONTROL, INSTRUMENTED_CONTROL_SHA],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert set(changed) == set(sources)


def test_backport_cli_requires_explicit_output_and_refuses_active_sdk():
    with pytest.raises(SystemExit) as missing:
        main([])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as active:
        main(["--output-root", str(ROOT)])
    assert active.value.code == 2


def test_backport_cli_writes_only_to_explicit_directory(tmp_path):
    output = tmp_path / "control"
    assert main(["--output-root", str(output)]) == 0
    for path, source in backported_sources(ROOT).items():
        assert (output / path).read_text(encoding="utf-8") == source
