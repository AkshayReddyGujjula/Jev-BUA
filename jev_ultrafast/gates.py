"""Per-risk confidence gates: one number cannot cover both a scroll and a payment.

Jev returns calibrated confidence with every choice, and the calibration is measurably
real (>= 0.9 was right 91.6% / 82.0% on public sets; < 0.5 was 63.6% / 29.0%), so it is a
forecast, not a formality. The threshold table this follows is the one in the knowledge
base's implementation guide: automate above ~0.95, allow 0.70-0.95 only when the action is
reversible, and never let a low-confidence reading execute on its own authority.

The interesting part is `irreversible`: a Noul answer asked in the SAME fan-out call as the
operation (so it costs no extra round trip) that says whether the chosen element commits
something the page cannot undo - pay, delete, submit, send. An irreversible action needs
the high floor; ordering a list needs the low one. The gate never retries a mutation: on a
low reading the loop re-observes once, and if the second reading is still low the run ends
`abstained`, which is a normal outcome rather than a failure.
"""

import os

# Kinds that change the page. WAIT, SCROLL, BACK and key presses are reversible by nature.
GATED_KINDS = {"click", "fill", "select", "seek"}
# Only a click can commit, pay, send or delete. A fill, a select or a slider is reversible
# by nature, so the page-level `irreversible` reading - which rates the most consequential
# OFFERED action, e.g. a Submit button that merely happens to be visible - must not raise
# their floor. (Measured live: filling "Full name" on a page offering Submit was refused
# under the 0.9 irreversible floor before this split.)
COMMITTABLE_KINDS = {"click"}
# A click on one of these roles is a reversible control, not a commit, even when the page
# also offers a Submit button: a consent checkbox or a link must not inherit its risk.
REVERSIBLE_ROLES = {"checkbox", "radio", "switch", "link"}


def _threshold(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


REVERSIBLE_MIN = _threshold("JEV_GATE_REVERSIBLE", 0.5)
IRREVERSIBLE_MIN = _threshold("JEV_GATE_IRREVERSIBLE", 0.9)
IRREVERSIBLE_NOUL = _threshold("JEV_IRREVERSIBLE_THRESHOLD", 0.5)


def check(decision, action):
    """None when the action may execute, else the gate verdict (a dict for evidence)."""
    if action.get("kind") not in GATED_KINDS:
        return None
    confidence = decision.get("confidence")
    target_confidence = decision.get("target_confidence")
    if isinstance(target_confidence, (int, float)) and isinstance(confidence, (int, float)):
        # The risky part of a mutation is the element, so gate on the weaker of the two
        # calibrated numbers the answer carried.
        confidence = min(confidence, target_confidence)
    elif isinstance(target_confidence, (int, float)):
        confidence = target_confidence
    if not isinstance(confidence, (int, float)):
        # No calibrated number to gate on: leave the decision to the freshness guards.
        return None
    try:
        irreversible = float(decision.get("irreversible") or 0.0)
    except (TypeError, ValueError):
        irreversible = 0.0
    high_stakes = (
        irreversible >= IRREVERSIBLE_NOUL
        and action.get("kind") in COMMITTABLE_KINDS
        and (action.get("role") or "").lower() not in REVERSIBLE_ROLES
    )
    floor = IRREVERSIBLE_MIN if high_stakes else REVERSIBLE_MIN
    if confidence >= floor:
        return None
    return {
        "reason": (
            "irreversible action below its confidence floor"
            if high_stakes else "action below its confidence floor"
        ),
        "confidence": round(float(confidence), 3),
        "target_confidence": target_confidence,
        "floor": floor,
        "irreversible": round(irreversible, 3),
        "action": action.get("label"),
    }
