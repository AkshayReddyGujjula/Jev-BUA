"""Descriptor replay: repeat a recorded successful flow without a model call per step.

The knowledge base's caching pattern is explicit about the shape - record the steps of a
verified run, replay them against a fresh observation, and fall back to the normal Jev loop
the moment anything does not line up. Replay is not a shortcut around verification: the
run's outcome still comes from the same independent readback.

What is recorded is a step's *identity*, never a node id: operation kind, role, label and
(for text steps) the literal that was validated at record time. Elements are re-resolved
from the live DOM on every replayed step, so replay cannot click a stale node.

Safety rules:
- a step recorded as irreversible is never replayed; the plan stops there and Jev decides
  it with a fresh, gated request
- a step that no longer matches uniquely on the live page stops the replay
- a replayed step with no visible effect stops the replay (the page changed)
- a plan only serves the exact URL and goal it was recorded from (goal hash)
- nothing here retries a mutation
"""

import hashlib
import json
import os
import time
from pathlib import Path

VERSION = 1


def goal_hash(url, goal):
    payload = json.dumps([url, goal], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_path(directory, url, goal):
    return Path(directory) / f"{goal_hash(url, goal)[:16]}.json"


def load(directory, url, goal):
    """The recorded plan for this exact url+goal, or None when there is no usable one."""
    if not directory:
        return None
    try:
        plan = json.loads(plan_path(directory, url, goal).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or plan.get("version") != VERSION:
        return None
    if plan.get("goal_hash") != goal_hash(url, goal):
        return None
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    return plan


def record(directory, url, goal, steps, verified=False):
    """Write a plan atomically. Returns its path, or None when it could not be written."""
    if not directory or not steps:
        return None
    payload = {
        "version": VERSION,
        "url": url,
        "goal": goal,
        "goal_hash": goal_hash(url, goal),
        "recorded_at": time.time(),
        "verified": bool(verified),
        "steps": steps,
    }
    path = plan_path(directory, url, goal)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        return None
    return path


def normalize(label):
    return " ".join((label or "").split()).strip().lower()


def match_step(step, actions):
    """The live action a recorded step refers to, or None when the match is not unique.

    An ambiguous match is a miss on purpose: guessing between two same-labelled controls
    is exactly the kind of silent wrong answer replay must not produce.
    """
    wanted = normalize(step.get("label"))
    if not wanted or not step.get("kind"):
        return None
    same = [
        action for action in actions
        if action.get("kind") == step["kind"] and normalize(action.get("label")) == wanted
    ]
    if len(same) == 1:
        return same[0]
    if len(same) > 1 and step.get("role"):
        roled = [action for action in same if action.get("role") == step["role"]]
        if len(roled) == 1:
            return roled[0]
    return None


def descriptor(action, decision, outcome, text):
    """One recorded step from an executed action. Dead outcomes are never recorded."""
    return {
        "kind": action.get("kind"),
        "label": action.get("label"),
        "role": action.get("role"),
        "operation": decision.get("operation"),
        "text": text if action.get("kind") in {"fill", "seek"} else None,
        "outcome": outcome,
        "confidence": decision.get("confidence"),
        "irreversible": decision.get("irreversible"),
    }
