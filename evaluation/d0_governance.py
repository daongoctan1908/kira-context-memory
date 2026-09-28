"""Run-level governance for the D0-local characterization.

Reuses the canonical policy in evaluation/runner.py (validate_canonical_policy) —
no duplicated rules. The runner must declare ONE run-level provider exposure
(``internal`` or ``external``) that applies to every provider in the run; the repo
has no trusted hostname classification, so exposure is an explicit caller
declaration and the profile must match it. Conservative mixed-provider rule: any
external provider makes the whole run external; per-provider exposure mixing is
therefore not expressible rather than silently accepted.

--real-run fails closed before any transport is constructed. --dry-run performs no
network and may report blocking reasons instead of raising, but its artifacts can
never claim official or accepted evidence.
"""

from dataclasses import dataclass
from typing import Literal

from evaluation.dataset import DatasetManifest
from evaluation.models import Profile
from evaluation.runner import validate_canonical_policy

D0Exposure = Literal["internal", "external"]

_PROFILE_EXPOSURE: dict[Profile, D0Exposure | None] = {
    Profile.INTERNAL_TEST: "internal",
    Profile.PC_OPENAI_ACCEPTANCE: "external",
    Profile.EXTERNAL_SYNTHETIC: None,  # canonical dataset rejection lives in policy
    Profile.MOCK: None,  # not a real-provider profile
}


@dataclass(frozen=True, slots=True)
class D0GovernanceDecision:
    """Outcome of the pre-flight policy evaluation; no provider was contacted."""

    eligible: bool
    blocking_reasons: tuple[str, ...]
    governance_status: Literal["eligible", "blocked"]


def exposure_of(profile: Profile) -> D0Exposure | None:
    """Run-level exposure implied by a profile; None when the profile cannot run D0."""
    return _PROFILE_EXPOSURE[profile]


def evaluate_d0_policy(
    manifest: DatasetManifest,
    profile: Profile,
    *,
    declared_exposure: D0Exposure,
    real_run: bool,
) -> D0GovernanceDecision:
    """One shared policy gate for the D0 runner.

    ``declared_exposure`` is the caller's explicit statement that every provider in
    this run is internal or external. It must equal the exposure implied by the
    profile; a mismatch fails closed (conservative mixed-provider rule: the run is
    only as internal as its most-exposed provider, and mixed declarations cannot be
    mechanically verified, so they are rejected instead of guessed).

    Dry-run never blocks on dataset maturity: it reports reasons. Real-run blocks
    (the caller must raise before constructing transports) whenever reasons exist.
    """
    reasons: list[str] = []
    implied = exposure_of(profile)
    if implied is None:
        reasons.append(
            f"profile {profile.value} cannot run D0; use internal_test or "
            "pc_openai_acceptance"
        )
    elif implied != declared_exposure:
        reasons.append(
            f"declared exposure {declared_exposure!r} does not match profile-implied "
            f"exposure {implied!r}; every provider shares one run-level exposure"
        )
    try:
        validate_canonical_policy(manifest, profile)
    except ValueError as error:
        reasons.append(f"canonical policy: {error}")
    eligible = not reasons
    if real_run and not eligible:
        return D0GovernanceDecision(
            eligible=False,
            blocking_reasons=tuple(reasons),
            governance_status="blocked",
        )
    return D0GovernanceDecision(
        eligible=eligible,
        blocking_reasons=tuple(reasons),
        governance_status="eligible" if eligible else "blocked",
    )
