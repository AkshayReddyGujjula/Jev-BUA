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
import re

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


# Reversible actions are not gated by default (2026-09-23). Refusing one does not make a run
# safer - it can be undone - but it does cost a re-observe, a second decision and, on a
# confidently-split page, a loop: a plain Search click at 0.47 was refused twice and the
# trap form never submitted. The original loop always executed Jev's argmax; this restores
# that for everything that cannot commit. Set JEV_GATE_REVERSIBLE to re-enable a floor.
REVERSIBLE_MIN = _threshold("JEV_GATE_REVERSIBLE", 0.0)
IRREVERSIBLE_MIN = _threshold("JEV_GATE_IRREVERSIBLE", 0.9)
# The lowest a mode may set its committable-click floor (user config, never model-set).
MODE_FLOOR_MIN = 0.5
# A click whose own label names a commit keeps a floor even when Jev's page-level
# `irreversible` reading is low, stale or malformed (it rates "the most consequential offered
# action", not the chosen one). Code-owned: a "Delete" at 0.4 confidence is refused whatever
# the noul said. Plain clicks (Search, One way, a city) stay ungated.
LABELLED_MIN = _threshold("JEV_GATE_LABELLED", 0.5)
COMMIT_LABEL = re.compile(
    r"\b(publish|deploy|launch|go live|buy|purchase|pay|checkout|place order|"
    r"delete|remove|destroy|terminate|close account|deactivate|unsubscribe|submit|send|"
    r"transfer|save changes|apply changes|archive|unpublish|revoke|disconnect|refund|confirm|"
    r"post|comment|"
    r"activate|connect domain|cancel subscription|change password|"
    r"disable (?:2fa|mfa)|enable (?:2fa|mfa))\b",
    re.IGNORECASE,
)
SECURITY_LABEL = re.compile(r"\b(password|passkey|2fa|mfa|security|recovery code)\b", re.IGNORECASE)


TOGGLE_ROLES = {"checkbox", "radio", "switch"}


def commit_labelled(action):
    """True for a click whose own label names a commit or an account-security change.

    Toggles are exempt ("I confirm the information is accurate" is a checkbox, and ticking
    it commits nothing); links are not ("Unsubscribe" is usually a link).
    """
    label = str(action.get("label") or "")
    return (
        action.get("kind") in COMMITTABLE_KINDS
        and (action.get("role") or "").lower() not in TOGGLE_ROLES
        and bool(COMMIT_LABEL.search(label) or SECURITY_LABEL.search(label))
    )
IRREVERSIBLE_NOUL = _threshold("JEV_IRREVERSIBLE_THRESHOLD", 0.5)
# A submit button of a search or filter form navigates to results; it commits nothing.
SEARCH_LABEL = re.compile(r"\b(search|find|filter|filters|go|look up|lookup)\b", re.IGNORECASE)


def committal(action):
    """True when the chosen element itself looks like a commit, not merely the page.

    Jev's `irreversible` head rates the most consequential action the page OFFERS, so on a
    site that always shows Subscribe or Post (YouTube read 0.69-0.79 on every page) it put
    every button - Search, Comments - behind the 0.9 floor and the run abstained on step 2
    (measured 2026-09-24). The page reading now sets how high the floor is; this decides
    whether the chosen click is the kind of element that can commit at all.
    """
    if commit_labelled(action):
        return True
    label = str(action.get("label") or "")
    return bool(action.get("form_submit")) and not SEARCH_LABEL.search(label)


def check(decision, action, irreversible_floor=None):
    """None when the action may execute, else the gate verdict (a dict for evidence).

    `irreversible_floor` is a mode's own floor for committable actions. A mode may lower it
    for work the operator trusts (never below MODE_FLOOR_MIN); lowering it is a
    deliberate, user-authored choice recorded in the mode file, not a model decision.
    """
    if action.get("kind") == "key" and action.get("shortcut"):
        # A page-advertised shortcut does what its element does: "Press Control+Enter (page
        # shortcut: Send)" is gated exactly like clicking Send.
        action = {**action, "kind": "click"}
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
        and committal(action)
    )
    labelled = commit_labelled(action)
    floor = IRREVERSIBLE_MIN if high_stakes else REVERSIBLE_MIN
    if labelled and not high_stakes:
        floor = max(floor, LABELLED_MIN)
    if high_stakes and irreversible_floor is not None:
        try:
            floor = min(IRREVERSIBLE_MIN, max(MODE_FLOOR_MIN, float(irreversible_floor)))
        except (TypeError, ValueError):
            floor = IRREVERSIBLE_MIN
    if confidence >= floor:
        return None
    return {
        "reason": (
            "irreversible action below its confidence floor" if high_stakes
            else "commit-labelled action below its confidence floor" if labelled
            else "action below its confidence floor"
        ),
        "confidence": round(float(confidence), 3),
        "target_confidence": target_confidence,
        "floor": floor,
        "irreversible": round(irreversible, 3),
        "action": action.get("label"),
    }
