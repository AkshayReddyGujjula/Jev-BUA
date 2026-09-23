"""On-demand supervisor: a vision LLM consulted only when the Jev loop is stuck.

Jev decides every step in ~200 ms; a text/vision LLM takes ~1.5-2.5 s per call (measured
2026-09-23 through the OpenCode Go endpoint), so it must never sit on the fast path. Instead
code-owned telemetry decides when the loop needs help - the anti-loop deadline is about to
fire, three actions in a row changed nothing, or Jev itself chose BLOCKED - and names that
check in `trigger`. Naming the failing check, rather than asking an open question, is what
made LLM repair work in the literature (arXiv 2608.02464: 52% -> 73% task success).

The supervisor's authority is deliberately narrow:

- `hint`: a one-sentence sub-goal that is placed in Jev's state; Jev re-decides with it and
  every normal gate still applies.
- `act`: one element that is in the CURRENT observed table. Code validates the id, the kind
  and the text; committable clicks are downgraded to a hint so the confidence gate and the
  delegated approval boundary are never bypassed. No coordinates, selectors or scripts.
- `needs_human`, `done`, `impossible`: terminal or checkpoint signals. `done` is not proof;
  the outcome still comes from the independent `expect` readback.

The screenshot is taken once, at trigger time, and only when no private value has been typed
(a typed secret could be visible in pixels). Nothing here raises into the loop.
"""

import json
import os
import time

from .gates import IRREVERSIBLE_NOUL, REVERSIBLE_ROLES
from .model import TEXT_SESSION, post_json
from .questions import SUPERVISOR

VERDICTS = {"hint", "act", "needs_human", "done", "impossible"}
NEEDS = {"login", "captcha", "one_time_code", "missing_information", "decision"}
# Element kinds the supervisor may execute directly. A click is allowed only when it cannot
# commit anything (see committable()).
ACT_KINDS = {"click", "fill", "select", "scroll", "key"}
MAX_ELEMENTS = 150


def _number(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def settings():
    """The endpoint the supervisor would use, or None when it is disabled or unconfigured."""
    switch = os.environ.get("JEV_SUPERVISOR", "auto").strip().lower()
    if switch in {"0", "off", "no", "false"}:
        return None
    key = os.environ.get("SUPERVISOR_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        return None
    base = (
        os.environ.get("SUPERVISOR_BASE_URL")
        or os.environ.get("TEXT_MODEL_BASE_URL")
        or "https://opencode.ai/zen/go/v1"
    ).rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    return {
        "key": key,
        "base": base,
        "model": os.environ.get("SUPERVISOR_MODEL") or os.environ.get("TEXT_MODEL")
        or "deepseek-v4.1-flash",
        "timeout": max(1.0, _number("JEV_SUPERVISOR_TIMEOUT", 8.0)),
        "limit": max(0, int(_number("JEV_SUPERVISOR_LIMIT", 2))),
        "reasoning": os.environ.get("JEV_SUPERVISOR_REASONING", "none").strip().lower(),
    }


def compact_elements(page):
    rows = []
    for action in (page.get("actions") or [])[:MAX_ELEMENTS]:
        row = {"id": action.get("id"), "kind": action.get("kind"),
               "label": str(action.get("label") or "")[:100]}
        if action.get("role"):
            row["role"] = action["role"]
        value = action.get("value") if action.get("kind") in {"fill", "select"} else None
        if value:
            row["value"] = str(value)[:60]
        rows.append(row)
    return rows


def compact_history(history):
    return [
        {k: h.get(k) for k in ("step", "action", "kind", "outcome", "recovery", "error")
         if h.get(k) is not None}
        for h in history[-10:]
    ]


def committable(action, irreversible=None):
    """Could clicking this element commit something? Then the supervisor may only hint it."""
    if action.get("kind") != "click":
        return False
    if (action.get("role") or "").lower() in REVERSIBLE_ROLES:
        return False
    if action.get("form_submit"):
        return True
    try:
        return float(irreversible or 0.0) >= IRREVERSIBLE_NOUL
    except (TypeError, ValueError):
        return True


def validate(raw, page, *, irreversible=None):
    """Turn a model reply into a code-checked verdict, or None when it is unusable.

    Every field is re-derived from the observed page: an id that is not in the current
    table, a fill without text, or text on a non-fill is rejected, never repaired.
    """
    if not isinstance(raw, dict):
        return None
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    reason = str(raw.get("reason") or "")[:160]
    if verdict == "hint":
        subgoal = raw.get("subgoal")
        if not isinstance(subgoal, str) or not subgoal.strip():
            return None
        return {"verdict": "hint", "subgoal": " ".join(subgoal.split())[:300], "reason": reason}
    if verdict == "act":
        element_id = str(raw.get("element_id") or "")
        action = next((a for a in page.get("actions") or [] if a.get("id") == element_id), None)
        if action is None or action.get("kind") not in ACT_KINDS:
            return None
        text = raw.get("text")
        if action["kind"] == "fill":
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                return None
        elif text not in (None, ""):
            return None
        else:
            text = None
        if committable(action, irreversible):
            # Never an automatic commit: hand it to Jev as the next sub-goal instead, so its
            # calibrated gate - and a delegated task's human approval - still decide.
            return {
                "verdict": "hint",
                "subgoal": f"Next, use the control labelled '{action.get('label')}'.",
                "reason": (reason + " [committable click downgraded to a hint]")[:200],
                "downgraded_from": element_id,
            }
        return {"verdict": "act", "element_id": element_id, "action": action,
                "text": text, "reason": reason}
    if verdict == "needs_human":
        need = str(raw.get("need") or "decision").strip().lower()
        return {"verdict": "needs_human", "need": need if need in NEEDS else "decision",
                "reason": reason}
    return {"verdict": verdict, "reason": reason}


def consult(page, goal, history, *, trigger, plan=None, guidance=None, screenshot=None,
            irreversible=None, config=None):
    """One bounded supervisor call. Returns (verdict or None, telemetry). Never raises.

    `irreversible` is Jev's latest page-level reading; it decides whether a proposed click
    is committable and must therefore be downgraded to a hint.
    """
    config = config or settings()
    telemetry = {"trigger": trigger, "latency_ms": 0, "model": None, "usage": {},
                 "screenshot": bool(screenshot), "error": None}
    if config is None:
        telemetry["error"] = "supervisor disabled or unconfigured"
        return None, telemetry
    telemetry["model"] = config["model"]
    context = {
        "trigger": trigger,
        "goal": goal,
        **({"plan": list(plan)} if plan else {}),
        **({"current_subgoal": guidance} if guidance else {}),
        "page": {"url": page.get("url"), "title": page.get("title"),
                 "text": (page.get("text") or "")[:2500]},
        "elements": compact_elements(page),
        "recent_actions": compact_history(history),
    }
    content = [{"type": "text", "text": json.dumps(context)}]
    if screenshot:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot}"}})
    body = {
        "model": config["model"],
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SUPERVISOR},
                     {"role": "user", "content": content}],
    }
    if config["reasoning"] in {"none", "off", "0"}:
        # {"reasoning": {"enabled": false}} is ignored by the OpenCode Go endpoint (measured).
        body["reasoning_effort"] = "none"
    elif config["reasoning"] in {"low", "medium", "high"}:
        body["reasoning_effort"] = config["reasoning"]
    started = time.perf_counter()
    try:
        result = post_json(
            config["base"] + "/chat/completions", config["key"], body,
            headers={"x-opencode-session": TEXT_SESSION, "User-Agent": "AgentJevUltraFast/0.1"},
            timeout=config["timeout"],
        )
        output = result["choices"][0]["message"]["content"]
        raw = json.loads(output) if isinstance(output, str) else output
        telemetry["usage"] = result.get("usage", {})
    except Exception as error:      # a failed consult is "no help", never a crash
        telemetry["latency_ms"] = round((time.perf_counter() - started) * 1000)
        telemetry["error"] = f"{type(error).__name__}: {error}"[:200]
        return None, telemetry
    telemetry["latency_ms"] = round((time.perf_counter() - started) * 1000)
    verdict = validate(raw, page, irreversible=irreversible)
    if verdict is None:
        telemetry["error"] = "reply failed validation"
        telemetry["raw"] = str(raw)[:300]
    return verdict, telemetry
