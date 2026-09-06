# Mem0 v2.0.20 behavior baseline

This document records the behavior observed from the pristine upstream source
before any KiRa-specific memory policy is added.

## V3 formation

- `Memory.add(..., infer=True)` uses the single-pass V3 ADD-only pipeline.
- A distinct extracted text produces an `ADD` result and a vector-store insert.
- No automatic `UPDATE` or `DELETE` operation is issued by this pipeline.
- Exact duplicate detection uses the MD5 hash of extracted text.
- A duplicate is skipped only when the user-scoped top-10 retrieval returns an
  existing memory with the same hash. This is not global semantic deduplication.
- The same extracted text for a different `user_id` is a distinct memory because
  existing-memory retrieval is scoped by user.
- Explicit public `update()` and `delete()` APIs still exist upstream, but the
  KiRa Week 3 baseline does not call them.

`tests/vendor/test_mem0_pristine_contract.py` is the executable compatibility
fixture for these observations. A future upstream upgrade must run this fixture
and explicitly review any changed result shape, count, or scoping behavior. The
Gateway must not add a custom semantic-dedup layer merely to keep this fixture
green.
