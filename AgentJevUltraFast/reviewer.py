"""Parallel reviewer: an LLM that watches the run while Jev works, never in front of it.

Jev decides every step in ~0.3 s; a text LLM takes ~1.5-2.5 s per call. The on-demand
supervisor only helps once a code-owned stuck check fires, which is late: measured 2026-09-24,
Jev typed a reply text into YouTube's search box and looped for 16 s before the supervisor
was asked, then diagnosed it correctly in one call. The reviewer asks that same question
continuously, off the critical path:

- after each executed step the loop hands it a redacted snapshot and moves on at once;
- one review is in flight at a time, and a newer snapshot replaces a queued older one;
- at the start of each decision the loop polls without blocking; a finished review that says
  the run is off track becomes a sub-goal on the existing guidance channel for a few steps;
- it cannot click, type, or end a run, and it closes with the run, so a late answer is dropped.

Nothing here raises into the loop. It uses the supervisor's endpoint settings.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import supervisor
from .model import TEXT_SESSION, post_json
from .questions import REVIEWER


def enabled():
    switch = os.environ.get("JEV_REVIEW", "auto").strip().lower()
    return switch not in {"0", "off", "no", "false"}


def settings():
    """The supervisor's endpoint with the reviewer's own limits, or None when unavailable."""
    if not enabled():
        return None
    config = supervisor.settings()
    if config is None:
        return None
    try:
        limit = int(os.environ.get("JEV_REVIEW_LIMIT", "12"))
        timeout = float(os.environ.get("JEV_REVIEW_TIMEOUT", "6"))
    except ValueError:
        limit, timeout = 12, 6.0
    return {**config, "model": os.environ.get("REVIEW_MODEL") or config["model"],
            "limit": max(0, limit), "timeout": max(1.0, timeout)}


def review(payload, config):
    """One bounded call. Returns a dict (verdict fields plus telemetry). Never raises."""
    started = time.perf_counter()
    result = {"step": payload["step"], "url": payload["page"]["url"], "on_track": None,
              "subgoal": None, "reason": None, "model": config["model"], "error": None,
              "latency_ms": 0, "usage": {}}
    body = {
        "model": config["model"],
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": REVIEWER},
                     {"role": "user", "content": json.dumps(payload)}],
    }
    if config.get("reasoning") in {"none", "off", "0"}:
        body["reasoning_effort"] = "none"
    try:
        reply = post_json(
            config["base"] + "/chat/completions", config["key"], body,
            headers={"x-opencode-session": TEXT_SESSION, "User-Agent": "AgentJevUltraFast/0.1"},
            timeout=config["timeout"],
        )
        raw = reply["choices"][0]["message"]["content"]
        raw = json.loads(raw) if isinstance(raw, str) else raw
        result["usage"] = reply.get("usage", {})
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"[:200]
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return result
    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    if not isinstance(raw, dict) or not isinstance(raw.get("on_track"), bool):
        result["error"] = "reply failed validation"
        return result
    result["on_track"] = raw["on_track"]
    result["reason"] = " ".join(str(raw.get("reason") or "").split())[:200] or None
    subgoal = raw.get("subgoal")
    if not raw["on_track"]:
        if not isinstance(subgoal, str) or not subgoal.strip():
            result["error"] = "off track without a subgoal"
            result["on_track"] = None
            return result
        result["subgoal"] = " ".join(subgoal.split())[:300]
    return result


class Reviewer:
    """At most one review in flight; the newest pending snapshot waits behind it."""

    def __init__(self, config, call=review):
        self.config = config
        self.call = call
        self.sent = 0
        self.future = None
        self.pending = None
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-review")

    def submit(self, payload):
        if self.closed or self.sent >= self.config["limit"]:
            return
        if self.future is not None and not self.future.done():
            self.pending = payload          # replaces an older queued snapshot
            return
        self._start(payload)

    def _start(self, payload):
        self.sent += 1
        self.future = self.pool.submit(self.call, payload, self.config)

    def poll(self):
        """A finished review, or None. Never waits."""
        if self.future is None or not self.future.done():
            return None
        future, self.future = self.future, None
        try:
            result = future.result()
        except Exception as error:          # review() never raises; belt and braces
            result = {"on_track": None, "error": type(error).__name__}
        if self.pending is not None and not self.closed and self.sent < self.config["limit"]:
            pending, self.pending = self.pending, None
            self._start(pending)
        return result

    def wait_for(self, step, timeout):
        """Every review that finishes until one covers `step`, or the timeout passes."""
        end = time.monotonic() + timeout
        results = []
        while time.monotonic() < end and (self.future is not None or self.pending is not None):
            if self.future is None:
                break
            try:
                self.future.result(timeout=max(0.0, end - time.monotonic()))
            except Exception:
                break
            result = self.poll()
            if result:
                results.append(result)
                if result.get("step") == step:
                    break
        return results

    def close(self):
        self.closed = True
        self.pending = None
        self.pool.shutdown(wait=False, cancel_futures=True)


def payload(goal, page, history, step, guidance=None, done=False):
    """The redacted snapshot a review sees (the caller redacts private values first)."""
    return {
        "goal": goal,
        "step": step,
        # The agent has chosen to finish: is every requirement of the goal visibly met?
        **({"agent_chose_done": True} if done else {}),
        **({"current_subgoal": guidance} if guidance else {}),
        "page": {"url": page.get("url"), "title": page.get("title"),
                 "text": (page.get("text") or "")[:2000]},
        # Live video state (speed, fullscreen, captions): not visible in page text.
        **({"media": page["media"]} if page.get("media") else {}),
        "elements": supervisor.compact_elements(page)[:120],
        "recent_actions": supervisor.compact_history(history),
    }
