"""Entry contracts shared by the approved range and IV/breach engines."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EntryInputs:
    rps: float
    upper_breach_probability: float
    lower_breach_probability: float
    iv_condition: str
    range_confidence: float
    data_quality_ok: bool
    # Configurable so callers can honor the same operator-facing thresholds
    # (rps_threshold, rps_max_upper/lower_breach_probability) used elsewhere
    # in the entry pipeline instead of silently re-applying different,
    # hardcoded ones here. Defaults preserve the original fixed behaviour.
    min_rps: float = 60.0
    max_upper_breach_probability: float = 0.50
    max_lower_breach_probability: float = 0.50
    # The probability of settling outside the range on *either* side, and its
    # ceiling. Supplied only for a structure that actually sold both sides.
    #
    # The per-side pair above cannot express this. At expiry the two sides are
    # mutually exclusive -- the underlying cannot finish both above the short
    # call and below the short put -- so a structure's real risk is their sum,
    # and checking each against its own ceiling silently grants one full budget
    # per side: two ceilings of 0.50 admit a combined 1.00. Measured on a
    # 6-month NIFTY replay, an iron butterfly at 0.4934 above and 0.4795 below
    # (a 0.9729 chance of finishing outside its wings, POP 17%) passed here,
    # while a call credit spread at 0.5984 on its only side was refused.
    #
    # `None` leaves the original per-side-only behaviour untouched, which is
    # also the correct rule for a single-sided spread: it has no second side to
    # combine, and must never be judged against the opposite side's ceiling.
    either_breach_probability: float | None = None
    max_either_breach_probability: float | None = None


def range_confidence(rps, upper_breach_probability, lower_breach_probability, rps_weight=.5):
    rps_component = max(0.0, min(1.0, float(rps) / 100.0))
    breach_component = 1.0 - (float(upper_breach_probability) + float(lower_breach_probability)) / 2.0
    return rps_weight * rps_component + (1.0 - rps_weight) * max(0.0, min(1.0, breach_component))


def entry_allowed(inputs: EntryInputs) -> bool:
    """RPS is an input, never the final trigger; all upstream evidence is mandatory."""
    either_ok = (
        inputs.either_breach_probability is None
        or inputs.max_either_breach_probability is None
        or inputs.either_breach_probability <= inputs.max_either_breach_probability
    )
    return bool(
        inputs.data_quality_ok
        and inputs.rps >= inputs.min_rps
        and inputs.iv_condition == "FAVOURABLE"
        and inputs.upper_breach_probability <= inputs.max_upper_breach_probability
        and inputs.lower_breach_probability <= inputs.max_lower_breach_probability
        and either_ok
        and inputs.range_confidence > 0.0
    )

