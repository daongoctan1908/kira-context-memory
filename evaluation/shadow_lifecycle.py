"""Shadow lifecycle for the D0-local O/O conflict-stage characterization.

Derives deterministic EARLY/LATE formation schedules from gold annotations, groups
same-boundary gold events into batches, and maintains pre-batch ACTIVE snapshots
with teacher-forced transitions applied atomically per batch. The dataset does not
encode exact formation boundaries: both schedules are sensitivity bounds, never
gold truth. No case IDs are hard-coded; every schedule is derived from
``primary_source_turn_ids`` and source/supporting evidence turns.
"""

from collections.abc import Iterator, Mapping
from enum import StrEnum
from itertools import groupby
from typing import Literal

from evaluation.dataset import DatasetMessage, LoadedBundle, iter_messages, namespace_id
from evaluation.models import D0CorpusStats, D0Schedule


class ShadowLifecycleError(ValueError):
    """Raised when gold events cannot form a consistent shadow timeline."""


class _TurnIndex:
    """Ordered user/assistant turn positions for one bundle."""

    __slots__ = ("order", "role_of", "companion_of", "messages")

    def __init__(self, bundle: LoadedBundle) -> None:
        messages: dict[str, DatasetMessage] = {}
        per_session: dict[str, list[DatasetMessage]] = {}
        for message in iter_messages(bundle):
            messages[message.local_message_id] = message
            per_session.setdefault(message.session_id or "", []).append(message)
        ordered = sorted(
            messages.values(), key=lambda m: (m.timestamp, m.local_message_id)
        )
        self.order: dict[str, int] = {
            m.local_message_id: index for index, m in enumerate(ordered)
        }
        self.role_of: dict[str, Literal["user", "assistant"]] = {
            m.local_message_id: m.role for m in ordered
        }
        self.companion_of: dict[str, str] = {}
        for session_messages in per_session.values():
            for index, message in enumerate(session_messages):
                if (
                    message.role == "user"
                    and index + 1 < len(session_messages)
                    and session_messages[index + 1].role == "assistant"
                ):
                    self.companion_of[message.local_message_id] = session_messages[
                        index + 1
                    ].local_message_id
        self.messages: dict[str, DatasetMessage] = messages

    def boundary_of(self, turns: tuple[str, ...]) -> str:
        """Completed production turn boundary: the assistant companion of the
        latest evidence user turn (an assistant turn is itself a boundary)."""
        latest = max(turns, key=lambda turn: self.order[turn])
        if self.role_of[latest] == "assistant":
            return latest
        companion = self.companion_of.get(latest)
        if companion is None:
            raise ShadowLifecycleError(
                f"evidence turn {latest} has no assistant companion; "
                "formation boundary cannot correspond to a completed turn"
            )
        return companion


class GoldEvent:
    """One persisted gold memory event with its evidence turns.

    Event != row: ``reinforce_existing`` events confirm existing durable rows and
    never materialize one (dataset ``event_vs_row_contract``); only ``add`` and
    ``update`` events become durable ACTIVE rows, and ``update`` additionally
    removes its superseded target row."""

    __slots__ = (
        "bundle_id",
        "event_id",
        "canonical_fact",
        "expected_operation",
        "supersedes_memory_id",
        "reinforce_target_ids",
        "primary_source_turn",
        "source_turns",
        "supporting_turns",
        "evidence_turns",
    )

    def __init__(self, bundle_id: str, row: Mapping[str, object]) -> None:
        event_id = str(row["memory_id"])
        if not row.get("should_store", True):
            raise ShadowLifecycleError(f"{bundle_id}:{event_id} is not a persisted gold event")
        self.bundle_id = bundle_id
        self.event_id = event_id
        self.canonical_fact = str(row["canonical_fact"])
        self.expected_operation = str(row["expected_operation"])
        supersedes = row.get("supersedes_memory_id")
        self.supersedes_memory_id = str(supersedes) if isinstance(supersedes, str) and supersedes else None
        reinforce_ids: list[str] = []
        single = row.get("reinforces_memory_id")
        if isinstance(single, str) and single:
            reinforce_ids.append(single)
        many = row.get("reinforces_memory_ids")
        if isinstance(many, list):
            reinforce_ids.extend(str(value) for value in many)
        self.reinforce_target_ids = tuple(dict.fromkeys(reinforce_ids))
        source_turns = tuple(str(value) for value in row["source_turn_ids"])  # type: ignore[arg-type]
        supporting = row.get("supporting_turn_ids")
        supporting = (
            tuple(str(value) for value in supporting) if isinstance(supporting, list) else ()
        )
        primary = row.get("primary_source_turn_ids")
        primary_turn = (
            str(primary[0]) if isinstance(primary, list) and primary else source_turns[0]
        )
        self.primary_source_turn = primary_turn
        self.source_turns = source_turns
        self.supporting_turns = supporting
        self.evidence_turns = tuple(dict.fromkeys((*source_turns, *supporting)))

    @property
    def gold_target_ids(self) -> tuple[str, ...]:
        targets: list[str] = []
        if self.supersedes_memory_id:
            targets.append(self.supersedes_memory_id)
        targets.extend(self.reinforce_target_ids)
        return tuple(dict.fromkeys(targets))


def _load_gold_events(bundle: LoadedBundle) -> tuple[GoldEvent, ...]:
    rows = bundle.memories.get("memory_gold")
    if not isinstance(rows, list):
        raise ShadowLifecycleError("memories document must contain a memory_gold array")
    return tuple(
        GoldEvent(bundle.manifest.bundle_id, row)
        for row in rows
        if row.get("should_store", True)
    )


class ScheduleBundle:
    """All gold events of one bundle resolved under one schedule."""

    __slots__ = ("bundle_id", "schedule", "events", "boundaries", "turn_index")

    def __init__(
        self,
        bundle: LoadedBundle,
        schedule: D0Schedule,
        turn_index: _TurnIndex | None = None,
    ) -> None:
        self.bundle_id = bundle.manifest.bundle_id
        self.schedule = schedule
        self.turn_index = turn_index or _TurnIndex(bundle)
        self.events = _load_gold_events(bundle)
        self.boundaries: dict[str, str] = {}
        for event in self.events:
            if schedule is D0Schedule.EARLY:
                turns: tuple[str, ...] = (event.primary_source_turn,)
            else:
                turns = event.evidence_turns
            self.boundaries[event.event_id] = self.turn_index.boundary_of(turns)

    def batch_key(self, event: GoldEvent) -> int:
        return self.turn_index.order[self.boundaries[event.event_id]]

    def batches(self) -> list[tuple[int, tuple[GoldEvent, ...]]]:
        """Deterministic batch ordering; array order never decides lifecycle."""
        ordered = sorted(self.events, key=lambda e: (self.batch_key(e), e.event_id))
        return [
            (key, tuple(batch))
            for key, batch in groupby(ordered, key=self.batch_key)
        ]

    @staticmethod
    def _apply_batch(active: set[str], batch: tuple[GoldEvent, ...]) -> None:
        """Apply one batch of gold events as durable row transitions, atomically.

        Row semantics follow the dataset ``event_vs_row_contract``: ``add``
        materializes a durable row; ``update`` removes the superseded row and
        materializes its own; ``reinforce_existing`` is a no-op on the row set
        (a confirmation never becomes a distinct row). Atomicity: callers pass a
        pre-batch snapshot, and every event in the batch is resolved against that
        same snapshot state — an update and its superseded target never split
        across read/write phases within the batch."""
        for event in batch:
            if event.expected_operation == "add":
                active.add(event.event_id)
            elif event.expected_operation == "update":
                if event.supersedes_memory_id is not None:
                    active.discard(event.supersedes_memory_id)
                active.add(event.event_id)
            # reinforce_existing: no-op on the durable row set.

    def pre_batch_active_ids(self, event: GoldEvent) -> tuple[str, ...]:
        """Durable ACTIVE rows the candidate sees: rows materialized by batches
        strictly before this event's batch, under row (not event) semantics."""
        key = self.batch_key(event)
        active: set[str] = set()
        for batch_key, batch in self.batches():
            if batch_key >= key:
                break
            self._apply_batch(active, batch)
        return tuple(sorted(active))

    def post_batch_active_ids(self, batch_key: int) -> tuple[str, ...]:
        """Durable ACTIVE rows after applying every gold transition of the batch."""
        active: set[str] = set()
        for key, batch in self.batches():
            self._apply_batch(active, batch)
            if key == batch_key:
                break
        return tuple(sorted(active))

    def iter_batches(self) -> Iterator[tuple[int, tuple[GoldEvent, ...], tuple[str, ...]]]:
        """Yield (batch_key, events, pre-batch ACTIVE snapshot) in timeline order."""
        active: set[str] = set()
        for key, batch in self.batches():
            yield key, batch, tuple(sorted(active))
            self._apply_batch(active, batch)


class ShadowTimeline:
    """Paired EARLY/LATE schedules over all bundles with sensitivity metadata."""

    def __init__(self, bundles: tuple[LoadedBundle, ...]) -> None:
        self._turn_indexes: dict[str, _TurnIndex] = {}
        self._bundles: dict[str, LoadedBundle] = {}
        self._schedules: dict[D0Schedule, dict[str, ScheduleBundle]] = {s: {} for s in D0Schedule}
        for bundle in bundles:
            bundle_id = bundle.manifest.bundle_id
            index = _TurnIndex(bundle)
            self._turn_indexes[bundle_id] = index
            self._bundles[bundle_id] = bundle
            for schedule in D0Schedule:
                self._schedules[schedule][bundle_id] = ScheduleBundle(bundle, schedule, index)

    def bundle(self, schedule: D0Schedule, bundle_id: str) -> ScheduleBundle:
        return self._schedules[schedule][bundle_id]

    def bundle_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._schedules[D0Schedule.EARLY]))

    def events(self, schedule: D0Schedule) -> Iterator[tuple[str, GoldEvent]]:
        for bundle_id in sorted(self._schedules[schedule]):
            for event in self._schedules[schedule][bundle_id].events:
                yield bundle_id, event

    def ambiguous_aggregate_events(self) -> frozenset[str]:
        """Events whose supporting evidence extends past the companion of their
        primary source turn. Computed from annotations; never hard-coded."""
        ambiguous: set[str] = set()
        for schedule in D0Schedule:
            for bundle_id, event in self.events(schedule):
                early_bundle = self._schedules[schedule][bundle_id]
                early = early_bundle.turn_index.boundary_of((event.primary_source_turn,))
                late = early_bundle.turn_index.boundary_of(event.evidence_turns)
                if early != late:
                    ambiguous.add(namespace_id(bundle_id, event.event_id))
        return frozenset(ambiguous)

    def boundary_pairs(self) -> tuple[tuple[str, GoldEvent, str, str], ...]:
        """(bundle_id, event, early_boundary, late_boundary) for every gold event."""
        pairs: list[tuple[str, GoldEvent, str, str]] = []
        for bundle_id in sorted(self._schedules[D0Schedule.EARLY]):
            early_bundle = self._schedules[D0Schedule.EARLY][bundle_id]
            late_bundle = self._schedules[D0Schedule.LATE][bundle_id]
            for event in early_bundle.events:
                pairs.append(
                    (
                        bundle_id,
                        event,
                        early_bundle.boundaries[event.event_id],
                        late_bundle.boundaries[event.event_id],
                    )
                )
        return tuple(pairs)

    def corpus_stats(self) -> D0CorpusStats:
        """Raw gold rows vs evaluated conflict points vs excluded negatives."""
        total = 0
        for bundle in self._bundles.values():
            rows = bundle.memories.get("memory_gold")
            if isinstance(rows, list):
                total += len(rows)
        evaluable = sum(1 for _ in self.events(D0Schedule.EARLY))
        return D0CorpusStats(
            total_gold_events=total,
            conflict_evaluable_events=evaluable,
            do_not_persist_excluded=total - evaluable,
        )
