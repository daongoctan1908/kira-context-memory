"""One-shot backport of mem0 observation hooks into the historical control runtime.

Extracts packages/viettel-mem0/mem0/observability.py verbatim from 67b034a and
wraps the control AsyncMemory.add() extraction/parse blocks with observe() calls
using the exact 67b034a pattern. No business logic changes: prompts, provider
call count, dedup, persistence and returns are byte-identical.
"""

import subprocess

CONTROL = "75deb1d8e11b9c7ec3eb14ccb99e0860af3a1c00"
EXTRACT_SOURCE = "67b034a"

observability = subprocess.run(
    ["git", "show", f"{EXTRACT_SOURCE}:packages/viettel-mem0/mem0/observability.py"],
    capture_output=True,
    text=True,
    check=True,
).stdout
open("packages/viettel-mem0/mem0/observability.py", "w", encoding="utf-8", newline="\n").write(
    observability
)
print("observability.py extracted verbatim from", EXTRACT_SOURCE)

src = subprocess.run(
    ["git", "show", f"{CONTROL}:packages/viettel-mem0/mem0/memory/main.py"],
    capture_output=True,
    text=True,
    check=True,
).stdout

old_import = "from mem0.exceptions import ValidationError as Mem0ValidationError\n"
assert src.count(old_import) == 1
src = src.replace(old_import, old_import + "from mem0.observability import observe\n")

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
assert src.count(old_extract) == 1, src.count(old_extract)
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
src = src.replace(old_extract, new_extract)

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
assert src.count(old_parse) == 1, src.count(old_parse)
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
            parse_observation.set_attribute("kira.memory.fact_count", len(extracted_memories))
            parse_observation.set_output(extracted_memories)
"""
src = src.replace(old_parse, new_parse)

open("packages/viettel-mem0/mem0/memory/main.py", "w", encoding="utf-8", newline="\n").write(src)
print("main.py instrumented: import + mem0.extract + mem0.extract.parse")
