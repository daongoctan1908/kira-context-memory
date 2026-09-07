"""Verify the synthetic Docker stack end-to-end; output only case IDs and outcomes."""

import argparse
import asyncio
import json
import os
from hashlib import sha256
from uuid import uuid4

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import conversations
from scripts.smoke_gateway import iter_sse_events
from tests.support.week2_cases import CASES

SMOKE_USER_ID = "local-smoke-user"


async def run(gateway_url: str, mock_kira_url: str, database_url: str) -> None:
    engine = create_async_engine(database_url)
    store = PostgresConversationStoreAdapter(engine)
    created_sessions: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Explicit stub identity prevents using this harness against a real KiRa endpoint.
            identity = await client.get(mock_kira_url + "/_test/requests")
            assert identity.json()["stub"] == "kira-week2-local-only"
            for endpoint in ("/health", "/ready", "/metrics"):
                assert (await client.get(gateway_url + endpoint)).status_code == 200
            for case in CASES:
                session_id = "w2-smoke-" + uuid4().hex
                created_sessions.append(session_id)
                for query in (case.previous, case.current):
                    async with client.stream(
                        "POST",
                        gateway_url + "/chat",
                        json={
                            "session_id": session_id,
                            "message": query,
                        },
                    ) as response:
                        assert response.status_code == 200
                        assert response.headers["content-type"].startswith("text/event-stream")
                        lines = [line async for line in response.aiter_lines()]
                    events = list(iter_sse_events(lines))
                    assert len(events) == 3
                    assert all(event.name != "gateway_error" for event in events)
                    text = "".join(
                        item["content"]["text"]
                        for event in events
                        for item in json.loads(event.data)["data"]["response"]
                        if item["type"] == "text"
                    )
                    assert text == "Mock KiRa answer"
                recent = await store.read_recent(SMOKE_USER_ID, session_id, 10)
                assert [message.content for message in recent] == [
                    case.previous,
                    "Mock KiRa answer",
                    case.current,
                    "Mock KiRa answer",
                ]
                hashes = (await client.get(mock_kira_url + "/_test/requests")).json()[
                    "query_hashes"
                ]
                assert hashes[-2:] == [
                    sha256(query.encode()).hexdigest() for query in (case.previous, case.expected)
                ]
                print(f"PASS mock_e2e case={case.name} persisted_messages=4")
            metrics = (await client.get(gateway_url + "/metrics")).text
            assert 'kira_context_rewrite_total{outcome="success"}' in metrics
            assert 'kira_conversation_write_total{outcome="inserted"}' in metrics
            print("PASS health readiness metrics; real_model_gate=NOT_RUN")
    finally:
        try:
            if created_sessions:
                async with engine.begin() as connection:
                    await connection.execute(
                        delete(conversations).where(
                            conversations.c.session_id.in_(created_sessions),
                        )
                    )
        finally:
            await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--mock-kira-url", default="http://127.0.0.1:18122")
    args = parser.parse_args()
    database_url = os.environ.get("POSTGRES_TEST_URL")
    if not database_url:
        print("FAIL POSTGRES_TEST_URL must point to the isolated smoke database")
        return 1
    try:
        asyncio.run(run(args.gateway_url.rstrip("/"), args.mock_kira_url.rstrip("/"), database_url))
    except Exception as error:
        print(f"FAIL mock_e2e error_class={type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
