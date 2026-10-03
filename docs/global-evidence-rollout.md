# GLOBAL interpretation evidence — contract and rollout

This change follows HEAD `34fae5b`. GLOBAL means user-owned evidence that may be considered
when interpreting a query in another conversation. It does not establish current truth or
applicability. CONVERSATION remains local/task context. Native Mem0 search stays best effort;
the consumer makes decisions about returned records, not a supposedly complete chronology.

## Formation and provenance

Each formation job supplies its verified source USER/ASSISTANT pair as New Messages. Completed
preceding turns from the owned PostgreSQL boundary window go through the optional per-call
`last_k_messages` hook. Context supplies referents and user-adopted proposals; it is not an
independent source of assertions for the new event. Context pairs completed after the source
logical request are excluded. The window remains at most ten messages including the source
pair. Last k messages retain their full content: the former 300-character limit is removed.

Mem0 history remains disabled. Jobs need not serialize by conversation; retries and reordered
worker completion use their own boundary context. The hook does not cache another transcript.
For other native callers, `last_k_messages=None` keeps native history behavior, while `[]`
supplies authoritative empty context.

USER source time is the first `chat_requests.created_at`, including reclaimed requests. The
store writes this into the existing message timestamp and projects it on reads of older turns.
Legacy `/chat` turns without a matching request use their stored USER timestamp. Metadata
retains owner, source conversation/event/turn/boundary and adds aware ISO `source_timestamp`.
This timestamp also anchors native Observation Date; Current Date remains processing time.
Explicit source dates/timezones take precedence over an implicit observation anchor.

Event-scoped GLOBAL candidates are not discarded just because an older event has the same
text hash. Within-batch duplicate suppression remains, and receipt replay remains the
correctness key for the same event. Native callers without a formation event and CONVERSATION
hash dedup retain their existing behavior.

## Consumer and compatibility

The retriever merges by memory ID, retains native scoring/caps and partial-failure fallback,
and does not collapse independent records by normalized text. Rewrite v7 receives only memory
text/scope/source time and recent message role/content/source user time. It sees no raw UUIDs
or retrieval scores. Assistant source time is null because completion time is not a new user
assertion. Invalid or missing memory source time is null; provider creation time is not a fallback.

Rewrite v7 instructs the model to prefer explicit current information, match qualifiers,
resolve dated supersession only within the same applicable convention, preserve unknown/tied
conflicts, limit cancellation to its named convention and require user adoption of historical
assistant suggestions. These are model behavior requirements, not code-enforced guarantees.
Code preserves independent records by ID and supplies source chronology; it cannot recover
assertions omitted by native best-effort search or guarantee the model's interpretation.

The current prompt revision uses formation policy `kira-memory-policy-v10` and rewrite v7.
Formation applies source/eligibility before fidelity, evidence-event handling and scope. Rewrite
preserves explicit query fields, matches complete references and applicable task exceptions,
then resolves same-context chronology before expanding each supported field. Metric/service
switches, ordered items, ranking direction/count and primary/reference distinctions are explicit.
The architecture, native Mem0 prompts/search, source metadata, windows and schema are unchanged.
The local policy diagnostic now separates its verified source pair from preceding context and
accepts native scope; protocol probes require explicit valid scope. These are local contract
checks, not evidence of model quality. The initial dataset review was followed by explicitly
authorized OpenAI-only tuning. The final candidate received 56 write-free formation calls and
286 rewrite calls: 140 canonical inputs, 35 synthetic inputs, and 37 focused cases repeated three
times. The canonical rewrite evidence uses frozen causal native memories from formation policy
v7, not fresh v10 search results. See [the current tuning report](openai-prompt-tuning-2026-10-03.md).
It documents concrete improvements and remaining failures; no semantic acceptance is claimed.
The older paired window results below belong to formation v7/rewrite v3, not the current prompts.

There is no schema migration, metadata backfill, receipt replay/re-formation or GLOBAL get_all.
Committed legacy records remain usable with unknown chronology. Existing conversation deletion
removes its source memories/receipts; owner fencing continues to block late writes. A separate
assertion owned by another source conversation survives deletion of the first conversation.

## Validation and window selection

Implementation is complete; model and product acceptance are separate release evidence.
The final deterministic suite passed with disposable PostgreSQL/pgvector: 1,503 tests, two skips,
90.73% coverage. The checkout byte contract is pinned by eight dataset attributes; all sixteen
declared bundle hashes match without temporary rewriting or changing JSON/manifests.
Use the following environment boundary, confirmed by the user:

| Environment | Rewrite provider | KiRa | Checks that run here |
|---|---|---|---|
| Laptop | OpenAI `gpt-4o-mini` | Unreachable | Unit/contract/vendor tests, disposable PostgreSQL, write-free synthetic formation and rewrite/window diagnostics |
| Company PC | OpenAI `gpt-4o-mini` | Real KiRa reachable | Native Gateway/Worker/queue product flow, reviewed dataset materialization and OpenAI + KiRa technical acceptance |
| K8s | Internal Qwen3-14B base | Real KiRa reachable | Internal-provider preflight, semantic confirmation and production rollout gate |

The PC cannot reach Qwen. Do not require KiRa on the laptop, probe Qwen on the PC, or treat
external-model diagnostics as internal-provider acceptance. Unavailable future-environment
checks are `NOT_RUN`; they do not mean the memory implementation remains unfinished.

Run unit/contract/vendor tests and PostgreSQL receipt, reservation, deletion, fencing and
formation gates against the installed `.7` distribution. Source-context tests cover long
proposals, cancellation/reinstatement, retries, reordered completion and separate calls.
Live formation validation must distinguish extraction omission from persistence hash drops.
Mocked extraction proves deterministic preservation; it does not prove model recall.

The paired window comparison uses a frozen v2/ten-message control and v3 windows of four,
six and ten messages. It uses fixed reviewed synthetic retrieval snapshots, not refetched
results or a complete GLOBAL collection. They isolate consumer behavior; native search recall
remains covered by the existing retrieval gates. Run with explicit provider configuration:

```powershell
python -m scripts.benchmark.compare_rewrite_context --env-file <explicit-env-file> --profile internal_test --output artifacts/context-window-tuning-v1.json
```

For the already authorized external acceptance configuration, use `--profile pc_openai_acceptance`.
On the laptop this command uses only the declared OpenAI rewrite and judge providers; it does not
call KiRa, Gateway, Worker, PostgreSQL or Mem0 search. On the PC run it with an explicit ignored
env file copied from `evaluation/benchmark.pc.env.example`; `BENCHMARK_REWRITE_*` must agree with
Gateway `VLLM_*` for the product run. Follow [the existing PC handoff](company-pc-ai-handoff.md)
for the separate real-KiRa acceptance. On K8s use the internal profile and exact deployed eval
image, with rewrite and approved internal judge bindings explicitly supplied.
The report freezes fixture/config/prompt hashes, rotates arm order over three paired repetitions,
uses native rewrite request settings and an arm-blind semantic judge, and reports provider input
tokens when available, full prompt bytes otherwise, P50/P95 latency, failures and uncertain cases.
The byte measure is a size proxy, not a tokenizer. Judge disagreements need adjudication; they
cannot promote a candidate automatically. No per-case quality loss can be offset by aggregate
gains. Equal quality plus a smaller prompt is sufficient; semantic improvement is not required.
The runner does not change configuration. Keep ten messages until quality and latency review
justify a shorter window. The existing 3,000-token recent budget still trims whole turns.

## Rollout and rollback

**HOLD:** deterministic tests pass, but the current local `gpt-4o-mini` semantic gate has genuine
formation and rewrite failures. The latest focused checks still select a dated threshold despite
an undated conflicting assertion, drop a formula's separately supplied unit, and occasionally
overwrite an explicit location. Native extraction can omit a new same-text GLOBAL assertion
before persistence/deduplication, or turn an ordinary reporting request into a memory. Existing
receipts protect retries; they cannot recover facts the extractor did not emit. These findings
come from raw provider outputs without an LLM judge. Historical judge disagreements also exist.
This sample is diagnostic, not a production error-rate estimate. Keep the deployed behavior and
ten-message setting until the configured production model passes the quality gate.

The earlier local paired run used rewrite v3 and a frozen 25-case fixture over three repetitions:
300 trials, no dependency errors, raw v2 control 43/75 and v3/ten-message 52/75. No shorter window
passed the gate. Recorded output inspection identifies six v3/ten-message judge false failures;
genuine failures remain after those disagreements, so aggregate improvement does not approve rollout.
The synthetic probes have informed tuning and do not constitute independent held-out evidence.

Production rewrite uses Qwen3-14B base at `http://10.254.135.40:8080/v1`, served model
`/models/Qwen3_14B`; it must not select `genai-lora`. Local/PC acceptance uses `gpt-4o-mini`
and does not establish Qwen acceptance. See [production configuration](production-deployment.md)
and the non-secret [rewrite env fragment](../deploy/production-rewrite.env.example). The laptop
connection probe timed out; production acceptance must run where that endpoint is reachable.
Only K8s nodes can reach this Qwen endpoint. The PC runs OpenAI + real-KiRa technical acceptance;
the K8s deployment runs Qwen + real-KiRa production acceptance.

1. Freeze the production endpoint, exact served model and request/server configuration, then run
   the paired gate and adjudicate raw outputs. Require applicable chronology, cancellation,
   explicit override, ambiguity and complete formula/unit cases to pass before rollout.
2. Ship app and vendor `.7` together after that gate passes. The distribution check expects `.7`; persisted storage
   remains schema version 3 with the compatible `.6` schema contract. No administrator migration
   or old scope-backfill runbook is needed for this change.
3. Drain in-flight workers during replacement. Preserve pending jobs and all committed receipts.
4. Validate readiness and the source/retrieval/deletion smoke gates; reuse `LTM_ENABLED` and
   `MEMORY_FORMATION_ENABLED` for staged enablement. Keep the recent window at ten initially.
5. Review native branch outcomes, formation scope/latency, rewrite errors/latency and the explicit
   paired report. No new subsystem, background job or feature flag is introduced.
6. Roll back by disabling existing flags if needed and reverting app/vendor artifacts together.
   Preserve rows, receipts and added metadata. Old code can ignore source_timestamp; reverting
   code restores the old interpretation, text-dedup and source-envelope behavior.

Remaining limits are best-effort search omissions, model interpretation errors, legacy unknown
chronology, tied source times and finite source/context windows. A source timestamp is not a
per-user business timezone or a total causal order; no such machinery is introduced here.
