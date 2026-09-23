"""One-process, one-browser delegation sessions for the MCP boundary.

The Agent owns decisions and outcomes. This module owns pauses, human handoffs,
budgets and the lifetime of its browser. A checkpoint is not a successful run.
"""

import re
import secrets
import threading
import time

from .agent import TERMINAL_STATUS, progress_key
from .browser import StalePage
from .cost import summarize as cost_summary
from .modes import normalize_label, origin

TASK_TTL_S = 30 * 60
MAX_SLICE_S = 15.0
MAX_TICKS_PER_SLICE = 8
COMMIT_LABEL = re.compile(
    r"\b(publish|deploy|launch|go live|buy|purchase|pay|checkout|place order|"
    r"delete|remove|destroy|terminate|close account|submit|send|transfer|"
    r"save changes|apply changes|archive|unpublish|revoke|disconnect|refund|confirm|"
    r"activate|connect domain|cancel subscription|change password|"
    r"disable (?:2fa|mfa)|enable (?:2fa|mfa))\b",
    re.IGNORECASE,
)
SECURITY_LABEL = re.compile(r"\b(password|passkey|2fa|mfa|security|recovery code)\b", re.IGNORECASE)


def human_boundary(page):
    """Return a reason only for challenge signals observed by code in the DOM."""
    flags = page.get("human") or {}
    if flags.get("password"):
        return "login"
    if flags.get("one_time_code"):
        return "one_time_code"
    if flags.get("captcha"):
        return "captcha"
    return None


def approval_boundary(decision, action):
    """Conservative generic boundary for consequential browser input."""
    kind = action.get("kind")
    if kind == "key" and action.get("key") == "Enter":
        return "submit_key"
    if kind != "click":
        return None
    label = str(action.get("label") or "")
    if SECURITY_LABEL.search(label):
        return "account_security"
    if COMMIT_LABEL.search(label) or action.get("form_submit"):
        return "consequential_action"
    if (action.get("role") or "").lower() in {"checkbox", "radio", "switch"}:
        return None
    try:
        irreversible = float(decision.get("irreversible") or 0)
    except (TypeError, ValueError):
        irreversible = 0.0
    if irreversible >= 0.8:
        return "possibly_irreversible"
    return None


class DelegatedTask:
    def __init__(self, agent, browser, *, max_active_seconds=120, keep_open=False, profile=None):
        self.id = secrets.token_urlsafe(24)
        self.agent = agent
        self.browser = browser
        self.profile = profile
        self.max_active_seconds = float(max_active_seconds)
        self.keep_open = bool(keep_open)
        self.created_at = time.time()
        self.expires_at = time.monotonic() + TASK_TTL_S
        self.active_seconds = 0.0
        self.segment_started = None
        self.phase = "ready"
        self.reason = None
        self.pending_action = None
        self.pending_profile_value = None
        self.checkpoint = 0
        self.wait_started = None
        self.cancel_requested = threading.Event()
        self.closed = False

        # A replay step acts inside Agent.command("tick") before this layer can
        # present an approval boundary. Delegated tasks retain decision caching
        # and learned values, but disable descriptor replay until it has a
        # per-step preflight hook.
        self.agent.replay_plan = None
        self.agent._delegation_no_vision = True
        if isinstance(self.agent.state.get("replay"), dict):
            self.agent.state["replay"]["available"] = False

    def _close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.agent.close()
        finally:
            if not self.keep_open:
                self.browser.close()

    def _summary(self):
        state = self.agent.state
        active_seconds = self.active_seconds
        if self.segment_started is not None:
            active_seconds += time.perf_counter() - self.segment_started
        return {
            "task_id": self.id,
            "checkpoint": self.checkpoint,
            "phase": self.phase,
            "reason": self.reason,
            "url": (state.get("page") or {}).get("url"),
            "status": state.get("status") if self.phase == "complete" else None,
            "outcome": state.get("outcome") if self.phase == "complete" else None,
            "actions": sum(1 for item in state.get("history", []) if not item.get("skipped")),
            "decisions": len(state.get("decisions", [])),
            "last_actions": [
                {"kind": item.get("kind"), "outcome": item.get("outcome")}
                for item in state.get("history", [])[-5:]
            ],
            "checks": [
                {"check": item.get("check"), "pass": item.get("pass")}
                for item in (state.get("checks") or [])
            ] if self.phase == "complete" else [],
            "readback_error": state.get("readback_error") if self.phase == "complete" else None,
            "gate": state.get("gate") if self.phase == "complete" else None,
            "stall": state.get("stall") if self.phase == "complete" else None,
            "failure_class": state.get("failure_class") if self.phase == "complete" else None,
            # Gate refusals executed nothing; surface them so a caller can promote a
            # standing instruction with `run_task.py --mode <slug> --remember`.
            "refusals": [
                {
                    "kind": item.get("kind"),
                    "label": item.get("action"),
                    "reason": str(item.get("recovery") or "").removeprefix("skipped: ") or None,
                }
                for item in state.get("history", [])
                if item.get("skipped")
            ][-5:],
            "active_seconds": round(active_seconds, 3),
            "wall_seconds": round(time.time() - self.created_at, 3),
            "evidence": state.get("evidence"),
            "usage": cost_summary(state),
        }

    def _pause(self, phase, reason, action=None):
        self.phase = phase
        self.reason = reason
        self.pending_action = action
        self.checkpoint += 1
        self.expires_at = time.monotonic() + TASK_TTL_S
        self.wait_started = time.perf_counter()
        result = self._summary()
        if action is not None:
            result["proposed_action"] = {
                "kind": action.get("kind"),
                "label": action.get("label"),
                "role": action.get("role"),
                "source": "local_profile" if reason == "confirm_fill" else "browser",
            }
        return result

    def _finish(self):
        self.phase = "complete"
        self.reason = None
        self.pending_action = None
        result = self._summary()
        self._close()
        return result

    def _refresh_after_human(self, *, after_wait=True):
        """Refresh this tab; only a real human wait rebases the stall clock."""
        state = self.agent.state
        if after_wait and self.wait_started is not None and state.get("started_at") is not None:
            state["started_at"] += time.perf_counter() - self.wait_started
        if after_wait:
            self.wait_started = None
            self.agent.pending_text = None
        page = self.browser.observe(screenshot=False)
        state["page"] = page
        state["decision"] = None
        state["status"] = "ready"
        if after_wait:
            state["progress_at"] = time.monotonic()
            state["stall_rounds"] = 0
            state["recent_fingerprints"] = [progress_key(page)]
        else:
            self.agent.note_progress(page)
        return page

    def _selected_action(self):
        state = self.agent.state
        decision = state.get("decision") or {}
        selected = decision.get("choice")
        if selected == "BACK":
            return {"kind": "back", "label": "Back"}
        return next(
            (action for action in state["page"]["actions"] if action["id"] == selected),
            None,
        )

    def _profile_value(self, action, page):
        if self.profile is None or action.get("kind") != "fill":
            return None
        same_label = [
            item for item in page.get("actions", [])
            if item.get("kind") == "fill"
            and normalize_label(item.get("label")) == normalize_label(action.get("label"))
        ]
        if len(same_label) != 1 or self.agent.supplied_text(action) is not None:
            return None
        return self.profile.value_for(page.get("url"), action.get("label"))

    def _use_profile_value(self, action, value):
        self.agent.profile_fill = (
            origin(self.agent.state["page"].get("url")), normalize_label(action["label"]), value
        )
        self.agent.private_values.add(value)
        self.browser.private_values = sorted(self.agent.private_values, key=len, reverse=True)

    def drive(self, *, slice_seconds=12):
        """Run at most one bounded active segment and return a checkpoint or result."""
        slice_seconds = max(1.0, min(MAX_SLICE_S, float(slice_seconds)))
        segment_started = time.perf_counter()
        self.segment_started = segment_started
        self.phase = "running"
        ticks = 0
        try:
            while True:
                if self.cancel_requested.is_set():
                    self.phase = "abandoned"
                    self.reason = "timed_out"
                    result = self._summary()
                    self._close()
                    return result
                state = self.agent.state
                if state["status"] in TERMINAL_STATUS:
                    return self._finish()
                if self.active_seconds + time.perf_counter() - segment_started >= self.max_active_seconds:
                    self.agent.stop_budget(f"{self.max_active_seconds:g}s active task budget")
                    return self._finish()
                if ticks and (ticks >= MAX_TICKS_PER_SLICE or
                              time.perf_counter() - segment_started >= slice_seconds):
                    return self._pause("paused", "slice_limit")
                challenge = human_boundary(state["page"])
                if challenge:
                    return self._pause("needs_human", challenge)
                if getattr(self.agent, "_strategy", "decide") == "collect":
                    self.agent.command("tick")
                    ticks += 1
                    continue
                if state["status"] != "predicted":
                    try:
                        self.agent.command("predict")
                    except StalePage:
                        self._refresh_after_human(after_wait=False)
                        continue
                if state["status"] in TERMINAL_STATUS:
                    return self._finish()
                if state["status"] != "predicted":
                    ticks += 1
                    continue
                if (state["decision"] or {}).get("choice") in {
                    "DONE", "BLOCKED", "ABSTAIN", "REOBSERVE"
                }:
                    try:
                        self.agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
                    except StalePage:
                        self._refresh_after_human(after_wait=False)
                    ticks += 1
                    continue
                action = self._selected_action()
                if action is None:
                    self._refresh_after_human(after_wait=False)
                    ticks += 1
                    continue
                profile_value = self._profile_value(action, state["page"])
                if profile_value is not None:
                    if not self.profile.is_approved(state["page"].get("url")):
                        self.pending_profile_value = profile_value
                        return self._pause("needs_human", "confirm_fill", action)
                    self._use_profile_value(action, profile_value)
                risk = approval_boundary(state["decision"], action)
                if risk:
                    return self._pause("needs_human", risk, action)
                if self.cancel_requested.is_set():
                    continue
                try:
                    self.agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
                except StalePage:
                    self._refresh_after_human(after_wait=False)
                finally:
                    self.agent.profile_fill = None
                ticks += 1
        finally:
            self.active_seconds += time.perf_counter() - segment_started
            self.segment_started = None

    def resume(self, checkpoint, *, approve_action=False, slice_seconds=12):
        if self.cancel_requested.is_set():
            raise ValueError("Task was abandoned")
        if self.phase not in {"paused", "needs_human"}:
            raise ValueError("Task is not waiting at a checkpoint")
        if int(checkpoint) != self.checkpoint:
            raise ValueError("Stale task checkpoint")
        if time.monotonic() > self.expires_at:
            self.cancel()
            raise ValueError("Task checkpoint expired")
        if self.pending_action is not None:
            if not approve_action:
                return self._summary() | {"proposed_action": {
                    "kind": self.pending_action.get("kind"),
                    "label": self.pending_action.get("label"),
                    "role": self.pending_action.get("role"),
                }}
            state = self.agent.state
            if self.wait_started is not None and state.get("started_at") is not None:
                state["started_at"] += time.perf_counter() - self.wait_started
            self.wait_started = None
            # Approval applies to one observed action on one page. The human
            # may have navigated or changed the page while reading the prompt.
            # Re-observe before dispatch, and discard that approval if anything
            # differs. A later prediction must request a new checkpoint.
            previous = state["page"]
            fresh = self.browser.observe(screenshot=False)
            if fresh.get("fingerprint") != previous.get("fingerprint"):
                state["page"] = fresh
                state["decision"] = None
                state["status"] = "ready"
                self.agent.note_progress(fresh)
                self.pending_action = None
                self.pending_profile_value = None
                return self.drive(slice_seconds=slice_seconds)
            if self.active_seconds >= self.max_active_seconds:
                self.agent.stop_budget(f"{self.max_active_seconds:g}s active task budget")
                return self._finish()
            if self.cancel_requested.is_set():
                raise ValueError("Task was abandoned before approved input")
            if self.pending_profile_value is not None:
                self.profile.approve(previous.get("url"))
                self._use_profile_value(self.pending_action, self.pending_profile_value)
            action_started = time.perf_counter()
            try:
                self.agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                self._refresh_after_human(after_wait=False)
            finally:
                self.agent.profile_fill = None
                self.active_seconds += time.perf_counter() - action_started
            # A successful approved action is consumed exactly once. A later
            # checkpoint has a new generation and requires a new approval.
            self.pending_action = None
            self.pending_profile_value = None
        else:
            self._refresh_after_human()
        return self.drive(slice_seconds=slice_seconds)

    def cancel(self):
        if self.phase not in {"paused", "needs_human", "ready"}:
            raise ValueError("Only a yielded task can be cancelled")
        self.cancel_requested.set()
        self.phase = "cancelled"
        self.reason = "cancelled_by_caller"
        result = self._summary()
        self._close()
        return result

    def abandon(self):
        """A timed-out worker may still be in a browser call: never resume it."""
        self.cancel_requested.set()


class TaskRegistry:
    """A single live task per MCP server prevents cross-tab ownership races."""

    def __init__(self):
        self.current = None
        self.reservation = None
        self.lock = threading.RLock()

    def reserve(self):
        with self.lock:
            if self.current is not None and time.monotonic() > self.current.expires_at \
                    and self.current.phase in {"paused", "needs_human"}:
                self.current.cancel()
                self.current = None
            if self.current is not None or self.reservation is not None:
                raise ValueError("A browser task is already active in this MCP server")
            self.reservation = threading.Event()
            return self.reservation

    def release_reservation(self, reservation):
        with self.lock:
            if self.reservation is reservation:
                self.reservation = None

    def add(self, task, *, reservation=None):
        with self.lock:
            if reservation is not None:
                if self.reservation is not reservation or reservation.is_set():
                    raise ValueError("Browser task setup was abandoned")
                self.reservation = None
            if self.current is not None:
                if time.monotonic() > self.current.expires_at and self.current.phase in {
                    "paused", "needs_human"
                }:
                    self.current.cancel()
                    self.current = None
                else:
                    raise ValueError("A browser task is already active in this MCP server")
            self.current = task

    def get(self, task_id):
        with self.lock:
            task = self.current
            if task is None or task.id != task_id:
                raise ValueError("Unknown or expired task id; a server restart invalidates task ids")
            if task.cancel_requested.is_set():
                raise ValueError("Task is abandoned and cannot be resumed")
            return task

    def release_if_terminal(self, task):
        with self.lock:
            if task.phase in {"complete", "cancelled", "abandoned"} and self.current is task:
                self.current = None

    def abandon_active(self):
        with self.lock:
            if self.reservation is not None:
                self.reservation.set()
            if self.current is not None:
                self.current.abandon()
