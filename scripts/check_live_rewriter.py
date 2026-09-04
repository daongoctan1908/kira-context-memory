"""Opt-in synthetic dev gate against the configured vLLM; never print prompts/outputs."""

import asyncio
import json
import unicodedata

import httpx

from app.application.services.context_builder import ContextBuilder
from app.application.services.rewrite_prompt import REWRITE_PROMPT_VERSION
from app.config.settings import Settings
from app.domain.errors.query_rewriter import QueryRewriterError
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter
from tests.support.context_fakes import pair
from tests.support.week2_cases import CASES


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split()).rstrip("?.!")


async def run(settings: Settings) -> int:
    failures = 0
    async with httpx.AsyncClient() as client:
        rewriter = VllmQueryRewriterAdapter(client, settings)
        for case in CASES:
            context = ContextBuilder().build(
                pair(user=case.previous, assistant="Dữ liệu kiểm thử tổng hợp."),
                case.current,
            )
            try:
                output = await rewriter.rewrite(context)
            except QueryRewriterError as error:
                outcome = "dependency_error"
                error_class = type(error).__name__
            else:
                outcome = (
                    "pass" if normalized(output) == normalized(case.expected) else "review_required"
                )
                error_class = None
            failures += outcome != "pass"
            print(
                json.dumps(
                    {
                        "case": case.name,
                        "outcome": outcome,
                        "error_class": error_class,
                        "prompt_version": REWRITE_PROMPT_VERSION,
                    }
                )
            )
    return int(failures > 0)


def main() -> int:
    try:
        return asyncio.run(run(Settings()))
    except Exception as error:
        print(json.dumps({"outcome": "configuration_error", "error_class": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
