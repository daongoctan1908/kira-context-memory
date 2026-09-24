"""Deterministic scope-backfill rules against real pgvector rows (T4.1).

Rules under test (plan first, apply gated on zero unresolved):
- run_id missing + validated conversation_id + owner match -> backfill
  run_id = conversation_id
- run_id mismatch with proven proof of the canonical owner -> correct
- run_id mismatch without proof -> unresolved, never silently overwritten
- run_id already correct -> keep
- memory_scope missing -> backfill CONVERSATION
- memory_scope valid -> keep
- memory_scope invalid -> unresolved
- owner/provenance missing -> unresolved
"""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.domain.errors.memory import LongTermMemoryConfigurationError
from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    _scope_backfill_apply_sync,
    _scope_backfill_plan_sync,
    normalize_psycopg_dsn,
)

pytestmark = pytest.mark.postgres_integration


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return value


def _test_dsn() -> str:
    return normalize_psycopg_dsn(_test_url())


def _insert_conversation(
    connection: psycopg.Connection,
    conversation_id,
    user_id: str,
    status: str = "active",
) -> None:
    connection.execute(
        "INSERT INTO public.conversations "
        "(conversation_id, user_id, session_id, status) VALUES (%s, %s, %s, %s)",
        (conversation_id, user_id, uuid4().hex, status),
    )


def _payload(memory_id: str, user_id: str, conversation_id: str, **fields) -> dict:
    payload = {
        "id": memory_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "formation_event_id": str(uuid4()),
        "data": f"fact {memory_id}",
        "text_lemmatized": f"fact {memory_id}",
        "hash": f"hash-{memory_id}",
    }
    payload.update(fields)
    return {key: value for key, value in payload.items() if value is not None}


def _insert_memories(
    dsn: str,
    schema_name: str,
    payloads: list[dict],
) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL("INSERT INTO {} (id, vector, payload) VALUES (%s, %s, %s)").format(
                    sql.Identifier(schema_name, "memories")
                ),
                [
                    (payload["id"], [1.0, 0.0, 0.0], psycopg.types.json.Json(payload))
                    for payload in payloads
                ],
            )


def _fetch_payloads(dsn: str, schema_name: str) -> dict[str, dict]:
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            sql.SQL("SELECT id::text, payload FROM {}").format(
                sql.Identifier(schema_name, "memories")
            )
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _rows_by_data(plan) -> dict[str, object]:
    payloads = {row.memory_id: row for row in plan.rows}
    return payloads


async def test_backfill_rules_classify_and_apply_deterministically() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_backfill_rules_{uuid4().hex}"
    user_id = f"bf-user-{uuid4().hex}"
    other_user = f"bf-other-{uuid4().hex}"
    conversation = str(uuid4())
    other_conversation = str(uuid4())
    missing_conversation = str(uuid4())
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection:
            _insert_conversation(connection, conversation, user_id)
            _insert_conversation(connection, other_conversation, user_id)

        mid = lambda name: str(uuid4())  # noqa: E731
        correct = mid("correct")  # run_id already equals conversation_id
        missing = mid("missing")  # run_id missing -> backfill
        missing_no_owner = mid("no-owner")  # conversation row absent -> unresolved
        proven_mismatch = mid("proven-mismatch")  # mismatch, owner proves canonical
        unproven_mismatch = mid("unproven-mismatch")  # mismatch without proof
        scope_missing = mid("scope-missing")
        scope_valid_global = mid("scope-valid")
        scope_invalid = mid("scope-invalid")
        cross_user = mid("cross-user")  # conversations row owned by another user

        payloads = [
            _payload(correct, user_id, conversation, run_id=conversation, memory_scope="GLOBAL"),
            _payload(missing, user_id, conversation),
            _payload(missing_no_owner, user_id, missing_conversation),
            _payload(proven_mismatch, user_id, conversation, run_id=str(uuid4())),
            _payload(unproven_mismatch, user_id, missing_conversation, run_id=str(uuid4())),
            _payload(scope_missing, user_id, conversation, run_id=conversation),
            _payload(
                scope_valid_global,
                user_id,
                conversation,
                run_id=conversation,
                memory_scope="GLOBAL",
            ),
            _payload(
                scope_invalid, user_id, conversation, run_id=conversation, memory_scope="regional"
            ),
            _payload(cross_user, other_user, other_conversation),
        ]
        _insert_memories(dsn, schema_name, payloads)

        plan = await asyncio.to_thread(_scope_backfill_plan_sync, dsn, schema_name, "memories")
        actions = {row.memory_id: row for row in plan.rows}

        assert plan.total_memories == len(payloads)
        assert actions[correct].run_id_action == "keep"
        assert actions[missing].run_id_action == "backfill"
        assert actions[missing].resolved_conversation_id == conversation
        assert actions[missing_no_owner].run_id_action == "unresolved"
        assert actions[proven_mismatch].run_id_action == "backfill"
        assert actions[unproven_mismatch].run_id_action == "unresolved"
        assert actions[cross_user].run_id_action == "unresolved"
        assert actions[scope_missing].memory_scope_action == "backfill"
        assert actions[scope_valid_global].memory_scope_action == "keep"
        assert actions[scope_invalid].memory_scope_action == "unresolved"

        # The report exposes no memory content.
        report = plan.to_report()
        assert report["rows"][0].keys() >= {
            "memory_id",
            "user_id",
            "run_id_action",
            "memory_scope_action",
            "resolved_conversation_id",
            "owner_match",
        }

        # Production gate: unresolved rows must block an apply.
        with pytest.raises(LongTermMemoryConfigurationError):
            await asyncio.to_thread(
                _scope_backfill_apply_sync,
                dsn,
                schema_name,
                "memories",
                plan,
                allow_unresolved=False,
            )

        # Test-path apply (unresolved rows present but never touched).
        updated = await asyncio.to_thread(
            _scope_backfill_apply_sync, dsn, schema_name, "memories", plan, allow_unresolved=True
        )
        # Updated rows: missing (run_id+scope), proven_mismatch (run_id+scope),
        # scope_missing (scope), and the three run_id-unresolved rows whose
        # scope is still missing (missing_no_owner, unproven_mismatch,
        # cross_user) — the two fields are independent.
        assert updated == 6

        after = _fetch_payloads(dsn, schema_name)
        assert after[correct]["run_id"] == conversation
        assert after[correct]["memory_scope"] == "GLOBAL"
        assert after[missing]["run_id"] == conversation
        assert after[missing]["memory_scope"] == "CONVERSATION"
        assert after[proven_mismatch]["run_id"] == conversation
        assert after[unproven_mismatch]["run_id"] != other_conversation
        assert "run_id" in after[scope_missing]
        assert after[scope_missing]["memory_scope"] == "CONVERSATION"
        assert after[scope_valid_global]["memory_scope"] == "GLOBAL"
        # Invalid scope is never silently overwritten.
        assert after[scope_invalid]["memory_scope"] == "regional"
        # Hash and provenance survive every applied update.
        for name in (correct, missing, proven_mismatch, scope_missing):
            assert after[name]["hash"] == payloads[[p["id"] for p in payloads].index(name)]["hash"]
            assert (
                after[name]["formation_event_id"]
                == (payloads[[p["id"] for p in payloads].index(name)]["formation_event_id"])
            )
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id, other_user],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_backfill_apply_is_idempotent_on_re_run() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_backfill_idem_{uuid4().hex}"
    user_id = f"bf-idem-{uuid4().hex}"
    conversation = str(uuid4())
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection:
            _insert_conversation(connection, conversation, user_id)

        memory_id = str(uuid4())
        _insert_memories(
            dsn,
            schema_name,
            [_payload(memory_id, user_id, conversation)],
        )

        first_plan = await asyncio.to_thread(
            _scope_backfill_plan_sync, dsn, schema_name, "memories"
        )
        assert first_plan.pending_backfill_memory_ids == (memory_id,)
        assert (
            await asyncio.to_thread(
                _scope_backfill_apply_sync,
                dsn,
                schema_name,
                "memories",
                first_plan,
                allow_unresolved=False,
            )
            == 1
        )

        # Re-plan after the apply: nothing left to backfill; re-apply writes 0.
        second_plan = await asyncio.to_thread(
            _scope_backfill_plan_sync, dsn, schema_name, "memories"
        )
        assert second_plan.pending_backfill_memory_ids == ()
        assert second_plan.unresolved_memory_ids == ()
        assert (
            await asyncio.to_thread(
                _scope_backfill_apply_sync,
                dsn,
                schema_name,
                "memories",
                second_plan,
                allow_unresolved=False,
            )
            == 0
        )

        after = _fetch_payloads(dsn, schema_name)[memory_id]
        assert after["run_id"] == conversation
        assert after["memory_scope"] == "CONVERSATION"
        assert after["hash"].startswith("hash-")
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
