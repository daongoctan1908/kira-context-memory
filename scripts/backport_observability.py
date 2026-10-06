"""One-shot backport of mem0 observation hooks into the historical control runtime.

Extracts packages/viettel-mem0/mem0/observability.py verbatim from 67b034a and
wraps the control AsyncMemory.add() extraction/parse blocks with observe() calls
using the 67b034a pattern. The counter tolerates non-list JSON values without
changing parsing, prompts, provider calls, dedup, persistence or returns.
"""

import argparse
import subprocess
from pathlib import Path

CONTROL = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"
EXTRACT_SOURCE = "67b034a"

old_import = "from mem0.exceptions import ValidationError as Mem0ValidationError\n"

old_extract = """        try:
            response = await asyncio.to_thread(
                self.llm.generate_response,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            # Re-raise so callers can implement provider fallback / retry
            # (see sync counterpart for rationale).
            logger.error(f"LLM extraction failed (async): {e}")
            raise LLMError(f"LLM extraction failed: {e}") from e
"""
new_extract = """        extraction_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        with observe("mem0.extract", kind="client") as extract_observation:
            extract_observation.set_input(extraction_messages)
            try:
                response = await asyncio.to_thread(
                    self.llm.generate_response,
                    messages=extraction_messages,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                # Re-raise so callers can implement provider fallback / retry
                # (see sync counterpart for rationale).
                logger.error(f"LLM extraction failed (async): {e}")
                raise LLMError(f"LLM extraction failed: {e}") from e
            extract_observation.set_output(response)
            extract_observation.set_outcome("success")
"""

old_parse = """        # Parse response
        try:
            response = remove_code_blocks(response)
            if not response or not response.strip():
                extracted_memories = []
            else:
                try:
                    extracted_memories = json.loads(response, strict=False).get("memory", [])
                except json.JSONDecodeError:
                    extracted_json = extract_json(response)
                    extracted_memories = json.loads(extracted_json, strict=False).get("memory", [])
        except Exception as e:
            logger.error(f"Error parsing extraction response (async): {e}")
            extracted_memories = []
"""
new_parse = """        # Parse response
        with observe("mem0.extract.parse") as parse_observation:
            parse_observation.set_input(response)
            try:
                response = remove_code_blocks(response)
                if not response or not response.strip():
                    extracted_memories = []
                else:
                    try:
                        extracted_memories = json.loads(response, strict=False).get("memory", [])
                    except json.JSONDecodeError:
                        extracted_json = extract_json(response)
                        extracted_memories = json.loads(
                            extracted_json, strict=False
                        ).get("memory", [])
            except Exception as e:
                logger.error("Error parsing extraction response (async): %s", type(e).__name__)
                extracted_memories = []
                parse_observation.set_outcome("malformed")
            else:
                parse_observation.set_outcome("parsed" if extracted_memories else "empty_valid")
            parse_observation.set_attribute(
                "kira.memory.fact_count",
                len(extracted_memories) if isinstance(extracted_memories, list) else 0,
            )
            parse_observation.set_output(extracted_memories)
"""


def instrument_control(source: str) -> str:
    """Apply observation hooks to the pinned source without writing the active SDK."""
    for before, after in (
        (old_import, old_import + "from mem0.observability import observe\n"),
        (old_extract, new_extract),
        (old_parse, new_parse),
    ):
        if source.count(before) != 1:
            raise ValueError("backport requires the unchanged historical control source")
        source = source.replace(before, after)
    return source


def backported_sources(repository: Path) -> dict[str, str]:
    def read(revision: str, path: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), "show", f"{revision}:{path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout

    main_path = "packages/viettel-mem0/mem0/memory/main.py"
    observation_path = "packages/viettel-mem0/mem0/observability.py"
    return {
        main_path: instrument_control(read(CONTROL, main_path)),
        observation_path: read(EXTRACT_SOURCE, observation_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    output = args.output_root.resolve()
    main_path = "packages/viettel-mem0/mem0/memory/main.py"
    if (output / main_path).resolve() == (repository / main_path).resolve():
        parser.error("output-root must not overwrite the active SDK")
    for relative, source in backported_sources(repository).items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8", newline="\n")
    print("PASS observation backport written to explicit output directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
