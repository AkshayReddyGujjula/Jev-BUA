"""Descriptor replay: repeat a recorded successful flow without a model call per step.

The knowledge base's caching pattern is explicit about the shape - record the steps of a
verified run, replay them against a fresh observation, and fall back to the normal Jev loop
the moment anything does not line up. Replay is not a shortcut around verification: the
run's outcome still comes from the same independent readback.

What is recorded is a step's *identity*, never a node id: operation kind, role, label and
(for text steps) the literal that was validated at record time. Elements are re-resolved
from the live DOM on every replayed step, so replay cannot click a stale node.

Safety rules:
- a step whose live element is commit-like (gates.committal) is never replayed blind; Jev
  decides it with a fresh, gated request, and replay resumes after it
- a step that no longer matches uniquely on the live page hands that step to Jev (a heal);
  replay resumes once a later recorded step matches again, at most MAX_HEALS times a run
- a replayed step with no visible effect stops the replay (the page changed)
- a plan only serves the route (path + query, not host or port) and goal it was recorded from
- a supplied value is recorded by reference (`value_ref`), never by value, and is resolved from
  the current run's values; without one the step goes back to Jev
- nothing here retries a mutation
"""

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 2
# Heals one run may make before the plan is dropped for good: a page that needs more is a
# different flow, and Jev should own it end to end.
MAX_HEALS = int(os.environ.get("JEV_REPLAY_MAX_HEALS", "3"))
# How far past the step that missed a resync may land. Skipping recorded steps is how a heal
# absorbs a step the app no longer needs (a dismissed banner); a long skip is a different flow.
RESYNC_WINDOW = 3


def route(url):
    """The part of a URL a plan is keyed on: the same app on another port is the same flow."""
    parts = urlsplit(url or "")
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def goal_hash(url, goal):
    payload = json.dumps([route(url), goal], ensure_ascii=False, sort_keys=True)
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


def record(directory, url, goal, steps, verified=False, heals=None):
    """Write a plan atomically. Returns its path, or None when it could not be written.

    `heals` is the log of steps Jev re-decided while replaying an older plan, kept so a
    reviewer can see what changed in the app, never consulted by replay itself.
    """
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
    if heals:
        payload["heals"] = heals
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


def descriptor(action, decision, outcome, text, value_ref=None):
    """One recorded step from an executed action. Dead outcomes are never recorded."""
    fills = action.get("kind") in {"fill", "seek"}
    step = {
        "kind": action.get("kind"),
        "label": action.get("label"),
        "role": action.get("role"),
        "operation": decision.get("operation"),
        "text": text if fills and value_ref is None else None,
        "outcome": outcome,
        "confidence": decision.get("confidence"),
        "irreversible": decision.get("irreversible"),
    }
    if fills and value_ref is not None:
        step["value_ref"] = value_ref
    return step


def resync(steps, start, actions, limit=RESYNC_WINDOW):
    """The first recorded step at or after `start` that matches this page uniquely, or None."""
    for index in range(start, min(len(steps), start + limit)):
        if match_step(steps[index], actions) is not None:
            return index
    return None
