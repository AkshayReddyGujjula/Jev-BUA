"""Code-owned run outcomes: what a run can evidence, not what the model concluded.

`status` stays the agent's own state (done/blocked/stalled/abstained). `outcome` is the
independent verdict, resolved from a deterministic readback. The distinction is the whole
point: a DONE choice is not proof of success, so a run with nothing to verify against is
`unknown` rather than `verified`.

Vocabulary, borrowed from the CUA bounded-chooser contract in the Jev knowledge base
(docs/40-ecosystem/browser-cua-repos.md):

    verified          a readback ran and matched
    refuted           a readback ran and did not match
    unknown           the run ended with nothing to verify against
    abstained         a risk gate refused to act
    budget_exhausted  the stall deadline or a step/time budget fired

Precedence matters: evidence outranks opinion, so a readback that passes is `verified`
even when the agent itself ended `blocked` or `stalled` - the page is what it is.
"""

OUTCOMES = ("verified", "refuted", "unknown", "abstained", "budget_exhausted")


def resolve(status, checks=None, budget_exhausted=False, abstained=False):
    """The single place the outcome vocabulary is derived.

    `checks` is the per-assertion detail list from an expectation readback, or None when
    the caller supplied nothing to verify against (an empty list is still "nothing").
    """
    if checks:
        return "verified" if all(item.get("pass") for item in checks) else "refuted"
    if abstained or status == "abstained":
        return "abstained"
    if budget_exhausted or status == "stalled":
        return "budget_exhausted"
    return "unknown"


def describe(outcome, checks=None):
    """One line an agent or person can read in a tool result."""
    checks = checks or []
    if outcome == "verified":
        return "verified: " + "; ".join(f"{c['check']} ok" for c in checks)
    if outcome == "refuted":
        failed = [c["check"] for c in checks if not c.get("pass")]
        return "refuted: " + ", ".join(failed)
    if outcome == "abstained":
        return "abstained: a risk gate refused to act"
    if outcome == "budget_exhausted":
        return "budget_exhausted: no further progress inside the run's bounds"
    return "unknown: the run has no independent readback for its result"
