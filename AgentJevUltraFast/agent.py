"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cache, modes, replay, supervisor
from .assertions import (
    evaluate_expectations,
    normalize_expect,
    page_facts,
    selectors_for,
    value_selectors_for,
)
from .browser import SCAN_MAX_S, Browser, StalePage, target_visible
from .evidence import Evidence
from .gates import IRREVERSIBLE_NOUL
from .gates import check as gate_check
from .model import (
    DECIDE_TIMEOUT,
    TEXT_TIMEOUT,
    action_space,
    choose,
    classify_failure,
    field_context,
    field_text,
    field_values,
    harvest_items,
)
from .outcomes import resolve
from .privacy import redact
from .questions import MAX_STEPS

TERMINAL = {"DONE", "BLOCKED", "ABSTAIN"}
# "stalled" and "abstained" are terminal too: the anti-loop bound fired, or a risk gate
# refused to act. Both end the run honestly instead of taking another lap.
TERMINAL_STATUS = {"done", "blocked", "stalled", "abstained", "budget_exhausted"}
RECOVERY_LIMIT = 6
# Text prefetch: one helper call drafts every fillable field while Jev decides, so TYPE_TEXT
# rarely waits on the helper (measured 2026-09-23: two waits of ~1.35 s on the Flights path).
PREFETCH_ENABLED = os.environ.get("JEV_TEXT_PREFETCH", "1") not in {"0", "off", "false"}
PREFETCH_FIELDS = 12
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jev-prefetch")
# A supervisor sub-goal steers this many actions, then Jev is back on the caller's goal alone.
GUIDANCE_STEPS = 6
DEAD_OUTCOMES = {"no visible change", "action failed", "loop detected"}

# How many times one run may choose REOBSERVE before the option is withdrawn. Re-observing
# is a read, but an unbounded re-observe is a loop with extra steps.
REOBSERVE_LIMIT = int(os.environ.get("JEV_REOBSERVE_LIMIT", "5"))
# Bounded memory of gate attempts per page fingerprint: one re-observe, then abstain.
GATE_MEMORY = 50

# Hard anti-loop bound. After this many seconds of BROWSER-SIDE time with no NEW page state
# the agent stops deciding on its own and terminates as "stalled". It is a deadline rather
# than a step budget precisely so that no further lap of the loop can extend it, and it is
# read here rather than being asked of the model, because a loop is exactly the situation in
# which the model's own "am I stuck?" answer cannot be trusted.
#
# Time spent waiting on a model is credited back to this clock (see _credit_wait): a slow
# provider is not the page failing to respond, and counting it turned provider latency
# spikes (Jev's median moved 0.3 s -> 1.8 s for an hour on 2026-09-23) into false stalls.
# Model waits are bounded instead by each call's own timeout, by STALL_MAX_ROUNDS, and by
# STALL_HARD_S below.
STALL_LIMIT_S = float(os.environ.get("JEV_STALL_SECONDS", "5"))
# Wall-clock backstop that nothing is credited against: no new page state for this long ends
# the run whatever the time was spent on.
STALL_HARD_S = float(os.environ.get("JEV_STALL_HARD_SECONDS", "20"))
# Never start a model call with less than this much of the hard window left: a decision
# that cannot finish inside the backstop must not be started at all.
MIN_DECIDE_S = float(os.environ.get("JEV_MIN_DECIDE_SECONDS", "0.6"))
# Backstop for the deadline: consecutive decisions with no progress at all. The clock can
# be held open by a page that keeps changing; a decision count cannot.
STALL_MAX_ROUNDS = int(os.environ.get("JEV_STALL_MAX_ROUNDS", "8"))
# How many recent page states count as "seen"; a repeat of any of them is not progress.
STALL_MEMORY = 12


# Scrolling is legitimate work. A human scrolls a long list for ten minutes to find one
# item and the agent should be allowed to do the same, only faster. What is NOT work is a
# scroll that reveals nothing new - that is the loop. So the signal is content YIELD, not
# scroll count: scroll as far as the list keeps producing unseen items, and cut it off only
# when it stops producing them. An earlier version capped the streak at 10, which in a long
# list would have forced a click on whatever happened to be next - a fast wrong answer.
BARREN_SCROLL_LIMIT = int(os.environ.get("JEV_BARREN_SCROLL_LIMIT", "3"))
# How many times a collector waits for a feed that has not rendered yet before concluding
# there is nothing to scroll. Each attempt is a re-observation, so a render counts.
COLLECT_SETTLE_ATTEMPTS = int(os.environ.get("JEV_COLLECT_SETTLE_ATTEMPTS", "4"))
# A feed whose scroller is an inner element (x.com) offers no document scroll action, so the
# collector falls back to a code-owned wheel-equivalent scroll of the element under the
# viewport centre, bounded per run.
COLLECT_WHEEL_LIMIT = int(os.environ.get("JEV_COLLECT_WHEEL_LIMIT", "10"))
COLLECT_WHEEL_DELTA = int(os.environ.get("JEV_COLLECT_WHEEL_DELTA", "600"))
# Distinct element labels to remember when judging whether a step revealed anything new.
SEEN_LABEL_MEMORY = 800


def step_budget():
    """Action budget for this run, re-read per call.

    A cap, not a target. The previous hard-coded 60 was reached by a real task, and because
    this internal limit was separate from run_task's --max-steps it surfaced as a crash
    (exit 1) rather than a budget stop (exit 2).
    """
    raw = os.environ.get("JEV_MAX_STEPS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return MAX_STEPS


def _number(value, default, ceiling=None):
    """A positive float specialist setting, clamped, or the default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not (number > 0):
        return default
    return min(number, ceiling) if ceiling is not None else number


def _budget(value, default):
    """A positive int specialist setting, or the default (None stays None)."""
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def progress_key(page):
    """What counts as real progress. Deliberately excludes free page text.

    Text is the one part of a page that changes when nothing is happening: clocks,
    counters, carousels, live ads and nonces rewrite it on a timer. When the full
    fingerprint (url + text + controls + scroll) was the progress signal, such a page
    reset the stall clock on every read - a measured trap ran 61 decisions with zero
    progress and the deadline never fired once. Navigation, the set of available controls,
    and control values are the signals that actually move when work is being done; ambient
    text is not. Control values are included on purpose, so typing into a field counts.
    """
    controls = tuple(sorted(
        (
            a.get("kind"), a.get("label"), a.get("role"),
            str(a.get("value", "")), a.get("checked"), a.get("selected"),
        )
        for a in (page.get("actions") or [])
    ))
    return (page.get("url"), page.get("title"), controls)


def action_outcome(before, after, error=None):
    """Deterministic classification of what an executed action did."""
    if error:
        return "action failed"
    if after.get("url") != before.get("url"):
        return "navigated"
    if after.get("title") != before.get("title"):
        return "page changed"
    if abs(len(after.get("text") or "") - len(before.get("text") or "")) > 50:
        return "page content changed"
    if abs((after.get("scroll") or {}).get("y", 0) - (before.get("scroll") or {}).get("y", 0)) > 40:
        return "scrolled"
    if after.get("fingerprint") != before.get("fingerprint"):
        return "page changed"
    return "no visible change"


def next_best(decision, exclude):
    """Next-best operation/target from the same Jev distributions, skipping `exclude` choices."""
    operations = decision.get("operation_probabilities") or {}
    candidate_map = decision.get("candidate_map") or {}
    for operation in sorted(operations, key=operations.get, reverse=True):
        if operation in TERMINAL:
            continue
        if operation == "BACK":
            if "BACK" not in exclude:
                return {"choice": "BACK", "operation": "BACK", "target": None}
            continue
        head = (decision.get("raw_answers") or {}).get(operation.lower() + "_target") or {}
        mapping = candidate_map.get(operation) or {}
        probabilities = head.get("probabilities") or {}
        if probabilities and mapping:
            for index in sorted(probabilities, key=probabilities.get, reverse=True):
                choice = mapping.get(index)
                if choice and choice not in exclude:
                    return {"choice": choice, "operation": operation, "target": index}
        else:
            for choice in mapping.values():
                if choice not in exclude:
                    return {"choice": choice, "operation": operation, "target": None}
    return None


def choice_probability(decision, alt):
    """Probability of a recovered choice, read from the head it came from."""
    head = (decision.get("raw_answers") or {}).get(alt["operation"].lower() + "_target") or {}
    if head and alt.get("target") is not None:
        return (head.get("probabilities") or {}).get(alt["target"], 0.0)
    return (decision.get("operation_probabilities") or {}).get(alt["operation"], 0.0)


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, values=None, find=None,
                 expect=None, browser=None, evidence_dir=None, replay_dir=None, mode=None,
                 collect_steps=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.start_url = url
        # Task-class mode: per-mode memory, learned field values and specialist settings.
        # Resolution may create the mode (mode="auto" / JEV_MODES=auto) and never guesses
        # one; an explicitly named missing mode is the caller's error and raises.
        self.mode, self._mode_info = modes.resolve("\n".join(plan), url, requested=mode)
        spec = self.mode.specialist if self.mode else {}
        self._strategy = self.mode.strategy if self.mode else "decide"
        self._operation_model = spec.get("operation_model") or None
        self._text_model = spec.get("text_model") or None
        self._text_base_url = spec.get("text_base_url") or None
        self.decide_timeout = _number(spec.get("decide_timeout_s"), DECIDE_TIMEOUT, ceiling=DECIDE_TIMEOUT)
        # A mode may lower the irreversible floor for work its owner trusts. The gate
        # clamps it to the reversible floor; raising it is not supported (the global
        # default already is the conservative end of the KB's table).
        self._irreversible_floor = (
            _number(spec.get("irreversible_floor"), None)
            if spec.get("irreversible_floor") is not None else None
        )
        self._max_steps = _budget(spec.get("max_steps"), None)
        self.use_cache = spec.get("cache") is not False
        collect = spec.get("collect") if isinstance(spec.get("collect"), dict) else {}
        self._harvest_enabled = bool(collect.get("harvest")) and self._strategy == "collect"
        self._harvest_instructions = collect.get("instructions") or None
        self._collect_limit = _budget(
            collect_steps if collect_steps is not None else collect.get("steps"), 8
        )
        self._collect_settle_attempts = COLLECT_SETTLE_ATTEMPTS
        self._collect_wheel_limit = COLLECT_WHEEL_LIMIT
        # A recorded descriptor plan for this exact url+goal, when replay was requested.
        # The collect strategy never replays: its contract is that it only scrolls.
        replay_allowed = spec.get("replay") is not False and self._strategy != "collect"
        if self.mode and replay_allowed:
            # A mode's own plans win; an explicit replay_dir applies to mode-less runs so
            # one mode's recorded values can never replay into another.
            replay_dir = self.mode.replay_dir()
        self.replay_dir = Path(replay_dir) if replay_dir else None
        self.replay_plan = (
            replay.load(self.replay_dir, url, "\n".join(plan)) if self.replay_dir else None
        )
        # Caller-supplied field values. When a value is known ahead of time there is no
        # reason to pay a 1.2-3.3 s text-model round trip, and a test author's literal is
        # more correct than a generated one.
        self.values = {str(k): v for k, v in (values or {}).items()}
        # A caller-supplied scan target (a video title, a row name). With it, scrolling is
        # deterministic work: the code scans until the target appears rather than paying a
        # model call per screen. Without it, scrolling stays model-driven as before.
        self.find = (find or "").strip()
        # An injected browser is owned by the caller (the MCP server keeps one warm), so the
        # agent must never close it out from under the next call.
        self.browser = browser or Browser(url)
        self._owns_browser = browser is None
        # A supplied value that appears verbatim in the goal is not private: the goal already
        # carries it to every model in plain text, and masking it on the page only hid the
        # evidence of success (a "Results for laptops" page read "Results for [private]", so
        # Jev could not see its search had worked and chose BACK). Sensitive labels stay
        # private whatever the goal says.
        goal_text = "\n".join(plan)
        self.private_values = {
            str(value) for label, value in self.values.items()
            if isinstance(value, (str, int, float)) and str(value).strip()
            and (modes.is_sensitive(label) or str(value) not in goal_text)
        }
        self.private_values.update(getattr(self.browser, "private_values", ()))
        if self.mode is not None:
            self.private_values.update(value for _, value in self.mode.learned_entries(url=url))
        self.browser.private_values = sorted(self.private_values, key=len, reverse=True)
        # The independent readback, when the caller supplied one. finalize() evaluates it
        # against the live page; without it the run's outcome is "unknown", never "verified".
        # Validated here, before any browser work: a malformed expectation must fail the
        # call, never silently weaken or pass the verdict.
        self.expect = normalize_expect(expect)
        self.evidence = Evidence(
            evidence_dir,
            time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}",
            goal=redact("\n".join(plan), self.private_values),
            url=url,
            tab_mode=os.environ.get("JEV_TAB_MODE", "foreground"),
            model=os.environ.get("TYPESAFE_MODEL", "jev-latest"),
            mode=self.mode.slug if self.mode else None,
        ) if evidence_dir else None
        if self.evidence is not None and self._mode_info.get("enabled"):
            self.evidence.record("mode_resolved", **self._mode_info)
            if self.mode is not None and self._mode_info.get("matched") == "created":
                self.evidence.record(
                    "mode_created", slug=self.mode.slug, name=self.mode.name,
                    strategy=self.mode.strategy, root=self._mode_info.get("root"),
                )
        if self._mode_info.get("matched") == "created" and self._mode_info.get("root_in_repo"):
            print(
                f"warning: mode root {self._mode_info.get('root')} is inside a git repository; "
                "mode memory is personal data and should not be committed",
                file=sys.stderr,
            )
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir) or bool(spec.get("screenshots"))
        if self.private_values:
            self.screenshots = False
            self.record_dir = None
            self._private_pixels = True
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            if self._owns_browser:
                self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            progress_at=None,
            progress_wall_at=None,
            model_wait_s=0.0,
            stall_rounds=0,
            recent_fingerprints=[],
            stall=None,
            # v2: the code-owned verdict and its inputs.
            outcome=None,
            checks=None,
            readback=None,
            gate=None,
            reobserves=0,
            failure_class=None,
            cache_hits=0,
            cache_misses=0,
            evidence=str(self.evidence.path) if self.evidence and self.evidence.path else None,
            # v2 replay: identity-level descriptors of executed steps, and the plan state.
            descriptors=[],
            replay={
                "available": bool(self.replay_plan),
                "steps": len(self.replay_plan["steps"]) if self.replay_plan else 0,
                "used": 0,
                "completed": False,
                "stopped": None,
                "recorded": None,
            },
            # Task-class mode: name/strategy for callers, and this run's collector state.
            mode=self.mode.summary() if self.mode else None,
            mode_matched=self._mode_info.get("matched"),
            router=self._mode_info.get("router"),
            harvest={"items": 0, "calls": 0, "dropped": 0, "steps": 0, "barren": 0,
                     "file": None, "stopped": None, "last": []},
        )
        self.note_progress(page)
        # Seed the content-yield memory from the first view, so the first scroll is judged
        # against what was already on screen instead of getting one free "productive" step.
        self.state["seen_labels"] = [a.get("label") for a in page["actions"] if a.get("label")]
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def note_progress(self, page):
        """Record whether this observation is new ground.

        Progress means a progress_key we have not already seen in this stall window. The key
        excludes free page text, so a ticking clock or an animated counter cannot hold the
        clock open; and a repeat of an earlier key (an A/B oscillation, a re-click that
        renders the same thing) is not progress either.
        """
        state = self.state
        key = progress_key(page)
        seen = state["recent_fingerprints"]
        fresh = key not in seen
        seen.append(key)
        del seen[:-STALL_MEMORY]
        if fresh:
            self._mark_progress()
            state["stall_rounds"] = 0
        else:
            state["stall_rounds"] += 1
        return fresh

    def _mark_progress(self):
        """Restart both stall clocks: new page state, a new run, or a human hand-back."""
        now = time.monotonic()
        self.state["progress_at"] = now
        self.state["progress_wall_at"] = now

    def _credit_wait(self, started):
        """Exclude a model wait that began at `started` from the browser-side clock.

        Only the 5 s clock is credited; the hard wall clock is not, so a provider that keeps
        answering slowly still ends the run at STALL_HARD_S.
        """
        state = self.state
        if state.get("progress_at") is None:
            return
        now = time.monotonic()
        spent = max(0.0, now - started)
        state["progress_at"] = min(now, state["progress_at"] + spent)
        state["model_wait_s"] = round(state.get("model_wait_s", 0.0) + spent, 3)

    def _model_room(self):
        """Seconds a model call may still take before the hard backstop."""
        wall = self.state.get("progress_wall_at")
        if wall is None:
            return STALL_HARD_S
        return STALL_HARD_S - (time.monotonic() - wall)

    def stalled(self):
        """True once the loop stops making real progress.

        Three independent bounds, because each alone can be evaded: a browser-side deadline
        (a step count cannot extend it, but a page that keeps changing can hold it open), a
        consecutive no-progress round count (a decision count that text churn cannot
        reset), and a wall-clock backstop that model waits cannot extend.
        """
        state = self.state
        if state.get("stall_rounds", 0) >= STALL_MAX_ROUNDS:
            return True
        started = state.get("progress_at")
        if started is None:
            return False
        if self._model_room() < 0:
            return True
        return (time.monotonic() - started) > STALL_LIMIT_S

    def _stop_stalled(self):
        """Terminate deterministically instead of taking another lap.

        The supervisor gets one look first (bounded by JEV_SUPERVISOR_LIMIT per run); a
        usable verdict continues the run with a fresh window instead of ending it.
        """
        state = self.state
        trigger = ("repeated_no_progress_rounds" if state["stall_rounds"] >= STALL_MAX_ROUNDS
                   else "no_progress_deadline")
        if self._supervise(trigger):
            return self.snapshot()
        now = time.monotonic()
        waited = round(now - state["progress_at"], 1)
        wall = state.get("progress_wall_at") or state["progress_at"]
        last = state["history"][-1] if state["history"] else None
        if state["stall_rounds"] >= STALL_MAX_ROUNDS:
            reason = "repeated decisions with no progress"
        elif now - state["progress_at"] > STALL_LIMIT_S:
            # Browser-side time alone, model waits excluded, ran out: the page stopped
            # responding to anything the agent tried.
            reason = "no progress inside the deadline"
        else:
            # Only the wall clock ran out: the time went on model waits.
            reason = "no progress inside the hard backstop (model waits included)"
        state["stall"] = {
            "seconds_without_progress": waited,
            "limit_s": STALL_LIMIT_S,
            "wall_seconds_without_progress": round(now - wall, 1),
            "hard_limit_s": STALL_HARD_S,
            "model_wait_s": state.get("model_wait_s", 0.0),
            "repeated_rounds": state["stall_rounds"],
            "round_limit": STALL_MAX_ROUNDS,
            "reason": reason,
            "last_action": last.get("action") if last else None,
            "last_outcome": last.get("outcome") if last else None,
        }
        state["decision"] = None
        state["status"] = "stalled"
        if state["started_at"] is not None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        self._finalize()
        return self.snapshot()

    def stop_budget(self, reason):
        """End a run at an explicit action, decision, or caller time budget."""
        state = self.state
        if state["status"] in TERMINAL_STATUS:
            return self.snapshot()
        state["decision"] = None
        state["status"] = "budget_exhausted"
        state["budget_reason"] = str(reason)
        if state["started_at"] is not None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        self._finalize()
        return self.snapshot()

    def _prefetch_fields(self, page):
        """Fill labels a batch draft may cover: not sensitive, not supplied, not remembered."""
        labels = []
        for action in page.get("actions") or []:
            label = action.get("label")
            if action.get("kind") != "fill" or not label or label in labels:
                continue
            if modes.is_sensitive(label) or self.supplied_text(action) is not None:
                continue
            mode = getattr(self, "mode", None)
            if mode is not None and mode.value_for(label, page.get("url")) is not None:
                continue
            labels.append(label)
        return labels[:PREFETCH_FIELDS]

    def _maybe_prefetch(self, page):
        """Start one batch draft for this goal and field set, unless one already covers it.

        A draft covers a page when the goal is identical and every fill label on the page
        was in that draft (labels, not values). A draft made on the first view of a form is
        therefore reused as fields disappear or get filled - Google Flights drops "Return"
        after One way - and a page with a field no draft has seen gets a new draft.
        """
        if not PREFETCH_ENABLED or not os.environ.get("TEXT_MODEL_API_KEY"):
            return
        labels = self._prefetch_fields(page)
        if not labels:
            return
        goal = self.state["goal"]
        batches = [b for b in getattr(self, "_prefetches", []) if b["goal"] == goal]
        if any(set(labels) <= b["labels"] for b in batches):
            return
        mode = getattr(self, "mode", None)
        future = _PREFETCH_POOL.submit(
            field_values, goal, labels, page,
            mode_state=mode.text_state(page.get("url")) if mode else None,
            model=getattr(self, "_text_model", None),
            base_url=getattr(self, "_text_base_url", None),
        )
        self._prefetches = [*batches, {"goal": goal, "labels": set(labels), "future": future}][-6:]
        self.state["prefetches"] = self.state.get("prefetches", 0) + 1

    def _take_prefetch(self, action, page):
        """(text, helper meta) drafted for this field under this goal, or None to ask singly.

        A finished draft that saw this field is used at once; otherwise the newest pending
        draft that includes it is awaited (never longer than the text-helper timeout).
        """
        label = action.get("label")
        goal = self.state["goal"]
        candidates = [b for b in reversed(getattr(self, "_prefetches", []))
                      if b["goal"] == goal and label in b["labels"]]
        if not candidates:
            return None
        misses = self.state.setdefault("prefetch_misses", [])
        for batch in candidates:
            future = batch["future"]
            if future.done() and future.exception() is None:
                values, meta = future.result()
                if values.get(label) is not None:
                    return values[label], {**meta, "prefetched": True, "waited_ms": 0}
        pending = next((b for b in candidates if not b["future"].done()), None)
        if pending is None:
            misses.append({"field": label, "reason": "no usable draft"})
            return None
        asked = time.monotonic()
        try:
            values, meta = pending["future"].result(
                timeout=max(0.0, min(TEXT_TIMEOUT, self._model_room()))
            )
        except Exception as error:
            # Slow, failed or invalid: fall back to the single-field call, never guess.
            misses.append({"field": label, "reason": f"{type(error).__name__}: {error}"[:160]})
            return None
        finally:
            self._credit_wait(asked)
        if values.get(label) is None:
            misses.append({"field": label, "reason": "drafted as null"})
            return None
        return values[label], {**meta, "prefetched": True,
                               "waited_ms": round((time.monotonic() - asked) * 1000)}

    def _guidance(self):
        """The supervisor's current sub-goal, or None once it has steered GUIDANCE_STEPS actions."""
        guidance = self.state.get("guidance")
        if not guidance:
            return None
        if len(self.state["history"]) >= guidance["until_step"]:
            self.state["guidance"] = None
            return None
        return guidance["subgoal"]

    def _supervise(self, trigger):
        """Consult the on-demand supervisor once. True when it handled the situation.

        Called only when code-owned telemetry says the loop is stuck (`trigger` names the
        check), never on the fast path. At most JEV_SUPERVISOR_LIMIT consults per run, each
        bounded by its own timeout; a usable hint or action opens one fresh stall window,
        so the worst case a run can be extended by is limit x (timeout + one window).
        Returns False - leaving the caller's own ending in place - whenever the supervisor
        is off, spent, failed, or answered something code could not validate.
        """
        state = self.state
        if getattr(self, "_strategy", "decide") == "collect" or state.get("supervising"):
            return False
        config = supervisor.settings()
        if config is None or state.get("supervisions", 0) >= config["limit"]:
            return False
        state["supervisions"] = state.get("supervisions", 0) + 1
        state["supervising"] = True
        page = state["page"]
        private = getattr(self, "private_values", ())
        shot = None
        browser = state["browser"]
        # Pixels can show a typed secret that text redaction cannot reach.
        if not getattr(self, "_private_pixels", False) and getattr(browser, "can_screenshot", False):
            try:
                shot = browser.screenshot(quality=60)
            except Exception:
                shot = None
        last = state["decisions"][-1] if state["decisions"] else {}
        asked = time.monotonic()
        try:
            verdict, telemetry = supervisor.consult(
                redact(page, private), redact(state["goal"], private),
                redact(state["history"], private), trigger=trigger,
                plan=getattr(self, "steps", None), guidance=self._guidance(),
                screenshot=shot, irreversible=last.get("irreversible"), config=config,
            )
        finally:
            state["supervising"] = False
            self._credit_wait(asked)
        record = {
            "trigger": trigger, "verdict": verdict["verdict"] if verdict else None,
            "step": len(state["history"]), "latency_ms": telemetry["latency_ms"],
            "model": telemetry["model"], "screenshot": telemetry["screenshot"],
            "error": telemetry["error"], "reason": (verdict or {}).get("reason"),
        }
        state.setdefault("supervision", []).append(record)
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            evidence.record("supervision", **record, usage=telemetry.get("usage"),
                            subgoal=(verdict or {}).get("subgoal"),
                            element_id=(verdict or {}).get("element_id"))
        if verdict is None:
            return False
        kind = verdict["verdict"]
        if state.get("started_at") is not None:
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        if kind in {"hint", "act"}:
            # New advice is a new active segment; the consult limit bounds how often.
            self._mark_progress()
            state["stall_rounds"] = 0
            state["status"] = "ready"
        if kind == "hint":
            state["guidance"] = {"subgoal": verdict["subgoal"],
                                 "until_step": len(state["history"]) + GUIDANCE_STEPS}
            return True
        if kind == "act":
            action = next(a for a in page["actions"] if a["id"] == verdict["element_id"])
            operation = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}.get(
                action["kind"], action["id"].upper())
            state["supervisor_decision"] = {
                "choice": action["id"], "operation": operation, "target": None,
                "confidence": 1.0, "probability": 1.0, "probabilities": {action["id"]: 1.0},
                "irreversible": float(last.get("irreversible") or 0.0),
                "goal_done": 0.0, "stuck": 0.0, "cached": False, "supervisor": True,
                "supervisor_text": verdict["text"], "model": telemetry["model"],
                "latency_ms": telemetry["latency_ms"], "usage": telemetry.get("usage", {}),
                "recovery": f"supervisor ({trigger}): {verdict['reason']}"[:200],
                "fingerprint": page["fingerprint"],
            }
            return True
        # done / impossible / needs_human end the run; the outcome still comes from the
        # independent readback, so a supervisor "done" is no more proof than Jev's.
        state["decision"] = None
        if kind == "done":
            state["status"] = "done"
        else:
            state["status"] = "blocked"
            state["blocked_reason"] = f"supervisor: {kind}: {verdict.get('reason')}"[:300]
            if kind == "needs_human":
                state["needs_human"] = {"need": verdict["need"], "reason": verdict.get("reason")}
        self._finalize()
        return True

    def _finalize(self):
        """Resolve the run's outcome from an independent readback, exactly once.

        Evidence outranks the agent's own opinion: when the caller's expectation matches
        the real page, the outcome is `verified` even if the loop itself ended blocked or
        stalled - the page is what it is. Without an expectation there is nothing to verify
        against, so the outcome stays `unknown`: a DONE choice is not proof of success.
        """
        state = self.state
        if state.get("outcome"):
            return
        checks = None
        expect = getattr(self, "expect", None)
        if expect:
            try:
                facts = page_facts(
                    state["browser"], selectors_for(expect), value_selectors_for(expect)
                )
            except Exception as error:
                # The last observation is not an independent verification. In
                # particular, an absent-text assertion could otherwise pass when
                # the browser stopped answering.
                state["readback_error"] = type(error).__name__
            else:
                facts["harvest"] = {
                    "items": (state.get("harvest") or {}).get("items", 0),
                    "file": (state.get("harvest") or {}).get("file"),
                }
                _, checks = evaluate_expectations(expect, facts)
                checks = redact(checks, getattr(self, "private_values", ()))
                state["checks"] = checks
                state["readback"] = {
                    "url": redact(facts.get("url"), getattr(self, "private_values", ())),
                    "title": redact(facts.get("title"), getattr(self, "private_values", ())),
                    "text_chars": len(facts.get("text") or ""),
                }
        state["outcome"] = resolve(
            state.get("status"),
            checks=checks,
            budget_exhausted=state.get("status") == "stalled",
            abstained=state.get("status") == "abstained",
        )
        self._record_replay()
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            evidence.record(
                "run_end",
                status=state.get("status"),
                outcome=state.get("outcome"),
                checks=checks,
                actions=len(state["history"]),
                decisions=len(state["decisions"]),
                elapsed_ms=state.get("elapsed_ms"),
                stall=state.get("stall"),
                gate=state.get("gate"),
                failure_class=state.get("failure_class"),
                blocked_reason=state.get("blocked_reason"),
                budget_reason=state.get("budget_reason"),
                cache_hits=state.get("cache_hits", 0),
                cache_misses=state.get("cache_misses", 0),
            )

    def _record_replay(self):
        """Record the executed steps of a completed run, unless this run was itself a replay.

        Only `done` runs record: a blocked or stalled run has no successful flow to repeat.
        A replay run never re-records, so the plan keeps the confidences and irreversibility
        readings of the real run that produced it.
        """
        state = self.state
        replay_dir = getattr(self, "replay_dir", None)
        replay_state = state.get("replay")
        if not replay_dir or replay_state is None:
            return
        if state.get("status") != "done" or state.get("outcome") != "verified" or replay_state.get("used"):
            return
        if replay_state.get("recorded") or not state.get("descriptors"):
            return
        path = replay.record(
            replay_dir,
            getattr(self, "start_url", state["page"].get("url")),
            state["goal"],
            state["descriptors"],
            verified=state.get("outcome") == "verified",
        )
        if path:
            replay_state["recorded"] = str(path)
            evidence = getattr(self, "evidence", None)
            if evidence is not None:
                evidence.record(
                    "replay_recorded", path=str(path), steps=len(state["descriptors"]),
                    verified=state.get("outcome") == "verified",
                )

    def _replay_step(self):
        """Execute the next recorded step when it is unambiguously safe.

        True means a step was attempted (the caller snapshots); False means there is no
        usable plan or the replay must hand back to the normal Jev loop. Replay never
        retries: a mismatch or a no-effect step stops it and Jev decides from there.
        """
        plan = getattr(self, "replay_plan", None)
        state = self.state
        replay_state = state.get("replay")
        if not plan or replay_state is None or state["status"] in TERMINAL_STATUS:
            return False
        index = replay_state["used"]
        steps = plan["steps"]
        if index >= len(steps):
            replay_state["completed"] = True
            self.replay_plan = None
            return False
        step = steps[index]
        if float(step.get("irreversible") or 0.0) >= IRREVERSIBLE_NOUL:
            # The gate's trust does not survive into replay: commit-like steps get a fresh
            # gated decision even when the recorded one executed once.
            self._stop_replay(index, "irreversible step needs a fresh gated decision")
            return False
        if state["started_at"] is None:
            state["started_at"] = time.perf_counter()
            self._mark_progress()
        try:
            page = state["page"]
            if not state["browser"].fresh(page):
                page = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                state["page"] = page
                self.note_progress(page)
        except Exception:
            self._stop_replay(index, "page unreadable before the replayed step")
            return False
        action = replay.match_step(step, page["actions"])
        if action is None:
            self._stop_replay(index, "recorded step does not match this page")
            return False
        text = step.get("text")
        if action["kind"] in {"fill", "seek"} and (
            not isinstance(text, str) or not text.strip() or len(text) > 2000
        ):
            self._stop_replay(index, "recorded text is not reusable")
            return False
        decision = {
            "choice": action["id"],
            "operation": step.get("operation") or action["kind"].upper(),
            "target": None,
            "confidence": None,
            "probability": None,
            "latency_ms": 0,
            "usage": {},
            "replay": True,
            "replay_text": text,
        }
        state["decision"] = decision
        state["status"] = "predicted"
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            evidence.record(
                "replay_step", index=index, action_kind=action["kind"],
                label=action.get("label"), operation=decision["operation"],
            )
        try:
            self.command("act", {"fingerprint": page["fingerprint"]})
        except StalePage:
            state["decision"] = None
            state["status"] = "ready"
            state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
            self.note_progress(state["page"])
            self._stop_replay(index, "page changed during the replayed step")
            return True
        last = state["history"][-1] if state["history"] else {}
        if last.get("outcome") in DEAD_OUTCOMES and step.get("kind") != "scroll":
            self._stop_replay(index, "replayed step had no visible effect")
        else:
            # A scroll that changes nothing is not a mismatch: the page may already be
            # where it needs to be, and the next step's match is the real check.
            replay_state["used"] = index + 1
        return True

    def _stop_replay(self, index, reason):
        self.replay_plan = None
        state = self.state
        replay_state = state.get("replay")
        if replay_state is not None:
            replay_state["stopped"] = reason
        evidence = getattr(self, "evidence", None)
        if evidence is not None and replay_state is not None:
            evidence.record(
                "replay_stopped", at_step=index, done=replay_state.get("used"), reason=reason,
            )

    def _reveal_below(self, page):
        """One code-owned downward scroll after the gate refused the model's choices here.

        Scrolling is reversible and it is the only action that turns content below the fold
        into visible candidates. Tried once per page state, and only after a refusal, so it
        cannot loop on its own; the stall deadline still bounds everything.
        """
        state = self.state
        if self.stalled():
            # A reveal that starts after the deadline is exactly the behaviour the
            # deadline exists to stop.
            return False
        scroll = next(
            (a for a in page["actions"] if a["kind"] == "scroll" and (a.get("delta") or 0) > 0),
            None,
        )
        if scroll is None:
            return False
        tried = state.setdefault("reveal_attempted", {})
        if tried.get(page["fingerprint"]):
            return False
        tried[page["fingerprint"]] = True
        if len(tried) > GATE_MEMORY:
            for stale in list(tried)[:-GATE_MEMORY]:
                tried.pop(stale, None)
        error = None
        try:
            state["browser"].act(scroll, page)
        except StalePage:
            # Raised before any input is dispatched: nothing was scrolled.
            return False
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        # Record the dispatched scroll before observing its result, like every other
        # execution: a failed observation must not erase the record of the input.
        state["history"].append({
            "step": len(state["history"]) + 1,
            "action": "Scroll down (reveal)",
            "kind": "scroll",
            "choice": scroll["id"],
            "probability": 0.0,
            "confidence": None,
            "irreversible": 0.0,
            "cached": False,
            "model": None,
            "latency_ms": 0,
            "text": None,
            "text_helper": None,
            "text_latency_ms": 0,
            "operation": "SCROLL_DOWN",
            "target": None,
            "recovery": "visible options were refused; scrolled to reveal more",
            "scan": None,
            "error": error,
            "outcome": None,
            "page_changed": None,
            "url": page.get("url"),
            "usage": {},
            "executed_ms": state["elapsed_ms"],
            "elapsed_ms": state["elapsed_ms"],
        })
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            evidence.record(
                "execution", step=state["history"][-1]["step"], action="Scroll down (reveal)",
                action_kind="scroll", choice=scroll["id"], operation="SCROLL_DOWN",
                recovery=state["history"][-1]["recovery"], error=error, url=page.get("url"),
            )
        state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
        self.note_progress(state["page"])
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        outcome = action_outcome(page, state["page"], error)
        state["history"][-1].update(
            outcome=outcome,
            page_changed=outcome != "no visible change",
            fingerprint=state["page"]["fingerprint"],
            url=state["page"].get("url"),
            elapsed_ms=state["elapsed_ms"],
        )
        if state.get("descriptors") is not None and outcome not in DEAD_OUTCOMES:
            state["descriptors"].append(
                replay.descriptor(scroll, {"operation": "SCROLL_DOWN"}, outcome, None)
            )
        return True

    def _value_present(self, action, page, text):
        """Did the field actually accept the typed value? Learning must not poison later runs."""
        wanted = " ".join(str(text).split()).lower()
        label = (action.get("label") or "").strip().lower()
        for candidate in page.get("actions") or []:
            if candidate.get("kind") != "fill":
                continue
            if (candidate.get("label") or "").strip().lower() != label:
                continue
            direct = getattr(self.browser, "value_present", None)
            if direct is not None:
                try:
                    return bool(direct(candidate.get("node"), text))
                except Exception:
                    return False
            value = " ".join(str(candidate.get("value") or "").split()).lower()
            if value and (wanted in value or value in wanted):
                return True
        return False

    def _collect_tick(self):
        """One code-owned collection step: harvest the page, then scroll it.

        The collector exists for feed-style work where one model decision per scroll is
        pure overhead. It never clicks anything - no like, repost or follow can happen
        here - and that is a property of this code path, not a rule the model could break.
        """
        state = self.state
        if state["started_at"] is None:
            state["started_at"] = time.perf_counter()
            self._mark_progress()
        if self.stalled():
            return self._stop_stalled()
        harvest = state.setdefault(
            "harvest",
            {"items": 0, "calls": 0, "dropped": 0, "steps": 0, "barren": 0,
             "file": None, "stopped": None, "last": []},
        )
        page = state["page"]
        # Content yield, not control yield, is what a collector is doing. Scrolling a feed
        # reveals items without changing the control set, so the decision loop's progress
        # key (url/title/controls) would never move and the anti-loop clock would end the
        # run mid-collection. New visible text is the collector's progress.
        visible = (page.get("text") or "")
        digest = hashlib.sha256(visible.encode("utf-8", "replace")).hexdigest()
        progressed = digest != state.get("collect_text_hash")
        if progressed:
            state["collect_text_hash"] = digest
            self._mark_progress()
        if getattr(self, "_harvest_enabled", False) and getattr(self, "mode", None) is not None:
            # One harvest per visited page state, keyed to the collector's own step counter.
            # Triggering on text change alone burned 19 calls in 70 s on x.com, because a
            # live page rewrites counters and timestamps without yielding new items.
            if state.get("harvest_last_step") != harvest.get("steps", 0):
                state["harvest_last_step"] = harvest.get("steps", 0)
                self._harvest_page(page)
        if state["status"] in TERMINAL_STATUS:
            return self.snapshot()
        if harvest.get("steps", 0) >= getattr(self, "_collect_limit", 8):
            harvest["stopped"] = "steps"
            return self._collect_done()
        if harvest.get("barren", 0) >= BARREN_SCROLL_LIMIT:
            harvest["stopped"] = "feed exhausted"
            return self._collect_done()
        scroll = next(
            (a for a in page["actions"]
             if a["kind"] == "scroll" and (a.get("delta") or 0) > 0),
            None,
        )
        wheel = False
        if scroll is None:
            # A feed that has not rendered yet offers no scroll action. Wait, re-observe, and
            # only conclude "nothing to scroll" after a bounded number of attempts; the
            # anti-loop room keeps this from spinning, and a render that adds controls counts
            # as progress.
            attempts = state.get("collect_settle", 0)
            room = STALL_LIMIT_S - (
                time.monotonic() - (state.get("progress_at") or time.monotonic())
            )
            if (
                attempts < getattr(self, "_collect_settle_attempts", COLLECT_SETTLE_ATTEMPTS)
                and room > 1.0
            ):
                state["collect_settle"] = attempts + 1
                time.sleep(0.5)
                state["page"] = state["browser"].observe(
                    screenshot=self.screenshots, deadline=self.read_deadline()
                )
                if self.note_progress(state["page"]):
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    state["status"] = "collecting"
                    return self.snapshot()
        if scroll is None and state.get("collect_wheel", 0) < getattr(
            self, "_collect_wheel_limit", COLLECT_WHEEL_LIMIT
        ):
            # An inner-scroller feed (x.com) offers no document scroll action. Scroll the
            # element under the viewport centre instead; content yield is still what counts.
            state["collect_wheel"] = state.get("collect_wheel", 0) + 1
            scroll = {"id": "collect_wheel", "kind": "wheel", "label": "Scroll content (collect)"}
            wheel = True
        if scroll is None:
            harvest["stopped"] = "no scroll offered"
            return self._collect_done()
        if len(state["history"]) >= self.step_budget():
            # The collector is bounded by the same action budget as the decision loop.
            harvest["stopped"] = "step budget"
            return self._collect_done()
        error = None
        try:
            if wheel:
                state["browser"].scroll_content(COLLECT_WHEEL_DELTA)
            else:
                state["browser"].act(scroll, page)
        except StalePage:
            state["page"] = state["browser"].observe(
                screenshot=self.screenshots, deadline=self.read_deadline()
            )
            self.note_progress(state["page"])
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["status"] = "collecting"
            return self.snapshot()
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        state["history"].append({
            "step": len(state["history"]) + 1,
            "action": "Scroll content (collect)" if wheel else "Scroll down (collect)",
            "kind": "wheel" if wheel else "scroll",
            "choice": scroll["id"],
            "probability": 0.0,
            "confidence": None,
            "irreversible": 0.0,
            "cached": False,
            "model": None,
            "latency_ms": 0,
            "text": None,
            "text_helper": None,
            "text_latency_ms": 0,
            "operation": "SCROLL_DOWN",
            "target": None,
            "recovery": "collector scroll",
            "scan": None,
            "error": error,
            "outcome": None,
            "page_changed": None,
            "url": page.get("url"),
            "usage": {},
            "executed_ms": state["elapsed_ms"],
            "elapsed_ms": state["elapsed_ms"],
        })
        writer = getattr(self, "evidence", None)
        if writer is not None:
            # Recorded before observing its result, exactly as the decision loop does.
            writer.record(
                "execution", step=state["history"][-1]["step"],
                action="Scroll content (collect)" if wheel else "Scroll down (collect)",
                action_kind="wheel" if wheel else "scroll", choice=scroll["id"],
                operation="SCROLL_DOWN",
                confidence=None, collector=True, error=error, url=page.get("url"),
            )
        state["page"] = state["browser"].observe(
            screenshot=self.screenshots, deadline=self.read_deadline()
        )
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        outcome = action_outcome(page, state["page"], error)
        self.note_progress(state["page"])           # keeps the decision-loop clock honest
        after = (state["page"].get("text") or "")
        after_digest = hashlib.sha256(after.encode("utf-8", "replace")).hexdigest()
        content_yield = after_digest != digest
        if content_yield:
            state["collect_text_hash"] = after_digest
            self._mark_progress()
        harvest["steps"] = harvest.get("steps", 0) + 1
        harvest["barren"] = 0 if content_yield else harvest.get("barren", 0) + 1
        state["history"][-1].update(
            outcome=outcome,
            page_changed=outcome != "no visible change",
            fingerprint=state["page"]["fingerprint"],
            url=state["page"].get("url"),
            elapsed_ms=state["elapsed_ms"],
        )
        if harvest["barren"] >= BARREN_SCROLL_LIMIT:
            # Stop on the barren scroll itself instead of paying one more scroll+read to
            # discover the same thing at the top of the next tick.
            harvest["stopped"] = "feed exhausted"
            return self._collect_done()
        state["status"] = "collecting"
        return self.snapshot()

    def _collect_done(self):
        state = self.state
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["status"] = "done"
        self._finalize()
        return self.snapshot()

    def _harvest_page(self, page):
        """One harvest call for one page state, bounded by the anti-loop room left."""
        state = self.state
        mode = getattr(self, "mode", None)
        harvest = state["harvest"]
        text = (page.get("text") or "")[:3000]
        if mode is None or len(text.strip()) < 80:
            return
        left = self._model_room()
        if left < 0.5:
            return
        harvest["calls"] = harvest.get("calls", 0) + 1
        writer = getattr(self, "evidence", None)
        asked = time.monotonic()
        try:
            items = harvest_items(
                text, mode.memory_text(include_learned=False), getattr(self, "_harvest_instructions", None),
                timeout=min(TEXT_TIMEOUT, max(0.5, left)),
                model=getattr(self, "_text_model", None),
                base_url=getattr(self, "_text_base_url", None),
            )
        except Exception as exc:
            if writer is not None:
                writer.record("harvest", ok=False, error=str(exc)[:200], url=page.get("url"))
            return
        finally:
            self._credit_wait(asked)
        offered = len(items or [])
        matched = modes.source_match(items, text)
        stored = mode.save_items(page.get("url"), matched)
        harvest["dropped"] = harvest.get("dropped", 0) + (offered - len(stored))
        if stored:
            harvest["items"] = harvest.get("items", 0) + len(stored)
            harvest["file"] = str(mode.harvest_dir())
            harvest["last"] = [entry["title"][:80] for entry in stored[-5:]]
        if writer is not None:
            writer.record(
                "harvest", ok=bool(stored), offered=offered, saved=len(stored),
                dropped=offered - len(stored), url=page.get("url"),
                titles=[entry["title"][:80] for entry in stored[:5]],
            )

    def _record_skipped(self, decision, action, verdict):
        """A gate refusal that is not terminal: nothing executed, and the loop sees why.

        The history entry has outcome "action failed", so the existing repeat-recovery
        switches the next decision away from this choice instead of re-proposing it.
        """
        state = self.state
        state["history"].append({
            "step": len(state["history"]) + 1,
            "action": action.get("label"),
            "kind": action.get("kind"),
            "choice": decision.get("choice"),
            "probability": decision.get("probability", 0.0),
            "confidence": decision.get("confidence"),
            "irreversible": decision.get("irreversible"),
            "cached": decision.get("cached"),
            "model": decision.get("model"),
            "latency_ms": decision.get("latency_ms", 0),
            "text": None,
            "text_helper": None,
            "text_latency_ms": 0,
            "operation": decision.get("operation"),
            "target": decision.get("target"),
            "recovery": "skipped: " + str(verdict.get("reason")),
            "scan": None,
            "error": "skipped below the confidence floor; nothing was executed",
            "skipped": True,
            "outcome": "action failed",
            "page_changed": False,
            "url": state["page"].get("url"),
            "usage": {},
            "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
            "elapsed_ms": state["elapsed_ms"],
        })
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            record = state["history"][-1]
            evidence.record(
                "execution", step=record["step"], action=record["action"],
                action_kind=record["kind"],
                choice=record["choice"], operation=record["operation"],
                confidence=record["confidence"], skipped=True, recovery=record["recovery"],
                error=record["error"], url=record["url"],
            )

    def supplied_text(self, action):
        """A caller-provided value for this field, or None if the model must supply it.

        Validated exactly like the model path (non-empty string, <=2000 chars, numeric for
        a seek control) so a bad literal fails loudly here rather than as an opaque
        "Seek target is not actionable" at execution time.
        """
        picked = self._matching_value(action.get("label")) if self.values else None
        if picked is None:
            profile_fill = getattr(self, "profile_fill", None)
            if (
                profile_fill is not None
                and profile_fill[0] == modes.origin(self.state["page"].get("url"))
                and profile_fill[1] == modes.normalize_label(action.get("label"))
            ):
                picked = profile_fill[2]
        if picked is None:
            return None
        return self._validated_supplied(picked, action)

    def _matching_value(self, label):
        """The supplied value for a field label, or None.

        Exact match first, then substring with the LONGEST key winning. Longest-wins is not
        cosmetic: with plain insertion-order matching the same JSON could pick different
        literals depending on key order and capitalisation (measured: `{"Search":..., "Search
        the catalogue":...}` typed the wrong one, and reversing the keys fixed it). An
        outright tie is refused rather than guessed - typing a password or an address into
        the wrong field is far worse than falling back to the model.
        """
        label = (label or "").strip().lower()
        if label in self.values:
            return self.values[label]
        hits = sorted(
            (k for k in self.values if k.strip().lower() and k.strip().lower() in label),
            key=lambda k: (-len(k.strip()), k),
        )
        if not hits:
            return None
        top = [k for k in hits if len(k.strip()) == len(hits[0].strip())]
        if len(top) > 1:
            raise ValueError(
                f"Supplied values are ambiguous for {label!r}: {sorted(top)}. "
                "Give exactly one key that matches the field label."
            )
        return self.values[top[0]]

    @staticmethod
    def _validated_supplied(picked, action):
        """A supplied literal, validated exactly like the model path."""
        if isinstance(picked, (dict, list, tuple, set)) or picked is None:
            raise ValueError(
                f"Supplied value for {action.get('label')!r} must be a string, "
                f"got {type(picked).__name__}"
            )
        text = picked if isinstance(picked, str) else str(picked)
        if not text.strip() or len(text) > 2000:
            raise ValueError(f"Supplied value for {action.get('label')!r} must be 1-2000 characters")
        if action["kind"] == "seek":
            try:
                number = float(text.strip())
            except ValueError:
                raise ValueError(f"Supplied seek value for {action.get('label')!r} must be a number") from None
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError(f"Supplied seek value for {action.get('label')!r} must be finite")
        return text

    def snapshot(self):
        return redact({
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }, getattr(self, "private_values", ()))

    def step_budget(self):
        """Action budget for this run. A mode may lower the run's cap, never raise it."""
        base = step_budget()
        if getattr(self, "_max_steps", None):
            return min(base, self._max_steps)
        return base

    def read_deadline(self):
        """Absolute monotonic time an observation must return by, or None before the run.

        Observations cap their settle wait at this, so one started just before the anti-loop
        deadline cannot push the recorded stop past the guarantee by a full settle window.
        """
        state = getattr(self, "state", None)
        if not state or state.get("progress_at") is None:
            return None
        deadline = state["progress_at"] + STALL_LIMIT_S
        if state.get("progress_wall_at") is not None:
            deadline = min(deadline, state["progress_wall_at"] + STALL_HARD_S)
        return deadline

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            if getattr(self, "_strategy", "decide") == "collect":
                return self._collect_tick()
            if self._replay_step():
                return self.snapshot()
            try:
                self.command("predict", {})
                if state["status"] != "predicted":
                    # A watcher or the anti-loop deadline stopped the run during prediction,
                    # or the supervisor handed back a sub-goal: nothing to execute this tick.
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                self.note_progress(state["page"])
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
                # The stall clock starts with the run, not with browser setup.
                self._mark_progress()
            if self.stalled():
                return self._stop_stalled()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                self.note_progress(state["page"])
            state["decision"] = None
            if state["status"] in TERMINAL_STATUS:
                raise ValueError("This run has stopped. Start a fresh demo.")
            pending = state.pop("supervisor_decision", None)
            if pending and pending["fingerprint"] == state["page"]["fingerprint"]:
                # The supervisor already chose an observed, reversible element on THIS page;
                # it goes through act() - freshness, gates, approval - like any decision. A
                # page that moved on since the consult drops it: Jev decides afresh.
                state["decisions"].append(
                    {**pending, "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000)}
                )
                state["decision"] = pending
                state["status"] = "predicted"
                return self.snapshot()
            if len(state["decisions"]) >= self.step_budget() * 2:
                return self.stop_budget("model-call budget")
            # The decision's wait is credited back to the browser-side clock, so it is bounded
            # by its own timeout and by the hard backstop instead; one that cannot finish
            # inside the backstop is not started.
            left = self._model_room()
            if left < MIN_DECIDE_S:
                return self._stop_stalled()
            self._maybe_prefetch(state["page"])
            skip_cache = state.pop("skip_cache_once", False)
            decision = None
            for attempt in range(2):
                if attempt:
                    # The first attempt spent part of the backstop: size the retry by what
                    # is left, and do not start one that cannot finish inside it.
                    left = self._model_room()
                    if left < MIN_DECIDE_S:
                        return self._stop_stalled()
                asked = time.monotonic()
                try:
                    decision = choose(
                        state["page"], state["goal"], state["history"],
                        timeout=min(getattr(self, "decide_timeout", DECIDE_TIMEOUT), max(MIN_DECIDE_S, left)),
                        # Re-observing the same unchanged page once is enough; offering it again
                        # would only replay the same non-answer. Refusals below still get their
                        # own fresh look through the gate.
                        allow_reobserve=(
                            state.get("reobserves", 0) < REOBSERVE_LIMIT
                            and state["page"]["fingerprint"] not in (state.get("reobserved") or {})
                        ),
                        # A gate retry must be a genuine second reading, not a replayed answer.
                        use_cache=getattr(self, "use_cache", True) and not skip_cache,
                        model_id=getattr(self, "_operation_model", None),
                        mode_state=(
                            self.mode.decision_state() if getattr(self, "mode", None) else None
                        ),
                        guidance=self._guidance(),
                    )
                    break
                except ValueError as error:
                    # A malformed TypeSafe answer is not a page state, and the client-side
                    # validation is never relaxed. Take one live re-reading, then end the run
                    # honestly instead of escaping the loop with no status and no outcome.
                    state["invalid_answers"] = state.get("invalid_answers", 0) + 1
                    if attempt:
                        state["status"] = "blocked"
                        state["blocked_reason"] = f"invalid model answer: {error}"[:300]
                        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                        self._finalize()
                        return self.snapshot()
                    skip_cache = True
                except RuntimeError as error:
                    # The decision provider timed out or failed. A decision is not a
                    # mutation, so one retry inside the deadline is safe; a second failure
                    # ends the run with a reason instead of escaping with no status.
                    state["provider_errors"] = state.get("provider_errors", 0) + 1
                    if attempt:
                        state["status"] = "blocked"
                        state["blocked_reason"] = f"decision provider unavailable: {error}"[:300]
                        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                        self._finalize()
                        return self.snapshot()
                finally:
                    self._credit_wait(asked)
            # If the decision ran into the hard backstop, stop without acting. Without this
            # one more action plus its observation could run while the deadline had already
            # passed (measured: a 6.4 s window against a 5 s limit), and that action is also
            # the one least likely to be useful - it was chosen by a model that had run out
            # of time.
            if self.stalled():
                return self._stop_stalled()
            if state.pop("loop_detected", False) and state.get("recoveries", 0) < RECOVERY_LIMIT:
                alternate = next_best(decision, {h.get("choice") for h in state["history"][-2:]})
                if alternate:
                    state["recoveries"] = state.get("recoveries", 0) + 1
                    decision = {
                        **decision,
                        **alternate,
                        "probability": choice_probability(decision, alternate),
                        "recovery": "recent states are repeating; switched to a different action",
                    }
            last = state["history"][-1] if state["history"] else None
            if (
                last
                and last.get("outcome") in {"no visible change", "action failed"}
                and last.get("choice") == decision["choice"]
            ):
                alternate = (next_best(decision, {last["choice"]})
                             if state.get("recoveries", 0) < RECOVERY_LIMIT else None)
                if alternate:
                    state["recoveries"] = state.get("recoveries", 0) + 1
                    decision = {
                        **decision,
                        **alternate,
                        "probability": choice_probability(decision, alternate),
                        "recovery": "repeated a no-effect action; switched to the next-best option",
                    }
                else:
                    # The last mutation may have committed server-side even if
                    # Chrome showed no immediate change. Never dispatch it twice.
                    state["status"] = "blocked"
                    state["uncertain_execution"] = True
                    self._finalize()
                    return self.snapshot()
            if (
                decision.get("stuck", 0.0) > 0.85
                and not decision.get("recovery")
                and len(state["history"]) >= 2
                and state.get("recoveries", 0) < RECOVERY_LIMIT
            ):
                alternate = next_best(decision, {state["history"][-1].get("choice")})
                if alternate:
                    state["recoveries"] = state.get("recoveries", 0) + 1
                    decision = {
                        **decision,
                        **alternate,
                        "probability": choice_probability(decision, alternate),
                        "recovery": "stuck watcher fired; switched to a different action",
                    }
            # Choices this page already had refused below their confidence floor must not
            # come back: route to the next-best option that has not been skipped yet.
            skipped = (state.get("skipped") or {}).get(state["page"]["fingerprint"]) or []
            if (
                skipped
                and decision["choice"] in skipped
                and state.get("recoveries", 0) < RECOVERY_LIMIT
            ):
                alternate = next_best(decision, set(skipped))
                if alternate:
                    state["recoveries"] = state.get("recoveries", 0) + 1
                    decision = {
                        **decision,
                        **alternate,
                        "probability": choice_probability(decision, alternate),
                        "recovery": (
                            "low-confidence choices were skipped; switched to the "
                            "next-best option"
                        ),
                    }
            # Only a scroll that stopped producing new items is the loop. A productive scan
            # is left alone however long it takes - that is the task.
            if (
                state.get("barren_scrolls", 0) >= BARREN_SCROLL_LIMIT
                and state.get("scroll_recoveries", 0) < RECOVERY_LIMIT
            ):
                chosen = next(
                    (a for a in state["page"]["actions"] if a["id"] == decision["choice"]), None
                )
                if chosen is not None and chosen["kind"] == "scroll":
                    alternate = next_best(
                        decision,
                        {a["id"] for a in state["page"]["actions"] if a["kind"] == "scroll"},
                    )
                    if alternate:
                        state["scroll_recoveries"] = state.get("scroll_recoveries", 0) + 1
                        state["barren_scrolls"] = 0
                        decision = {
                            **decision,
                            **alternate,
                            "probability": choice_probability(decision, alternate),
                            "recovery": (
                                "scrolling stopped showing anything new, so the list is probably "
                                "exhausted; do not keep scrolling - use the page's own search, "
                                "filter, or another route to the target"
                            ),
                        }
            state["decisions"].append(
                {
                    **decision,
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            if decision.get("cached"):
                state["cache_hits"] = state.get("cache_hits", 0) + 1
            else:
                state["cache_misses"] = state.get("cache_misses", 0) + 1
            evidence = getattr(self, "evidence", None)
            if evidence is not None:
                request = decision.get("request") or {}
                evidence.record(
                    "decision",
                    step=len(state["decisions"]),
                    fingerprint=state["page"]["fingerprint"],
                    choice=decision.get("choice"),
                    operation=decision.get("operation"),
                    target=decision.get("target"),
                    confidence=decision.get("confidence"),
                    probability=decision.get("probability"),
                    irreversible=decision.get("irreversible"),
                    goal_done=decision.get("goal_done"),
                    stuck=decision.get("stuck"),
                    cached=decision.get("cached"),
                    model=decision.get("model"),
                    latency_ms=decision.get("latency_ms"),
                    usage=decision.get("usage"),
                    recovery=decision.get("recovery"),
                    offered={
                        name: sorted((question.get("criteria") or {}).keys())
                        for name, question in (request.get("questions") or {}).items()
                    },
                    request=request,
                )
            if decision.get("goal_done", 0.0) > 0.85 and state["history"]:
                # The completion cross-check fired: stop without executing the proposed action.
                state["decision"] = None
                state["status"] = "done"
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self._finalize()
                return self.snapshot()
            state["decision"] = decision
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            # Last gate before a mutation: the anti-loop deadline outranks the decision.
            if self.stalled():
                return self._stop_stalled()
            selected = decision["choice"]
            evidence = getattr(self, "evidence", None)
            if selected in {"DONE", "BLOCKED"}:
                if (
                    selected == "BLOCKED"
                    and not state.get("loading_wait")
                    and state["history"]
                    and state["history"][-1].get("page_changed") is True
                    and len(state["history"]) <= 5
                ):
                    # A page that just navigated and still looks empty is loading, not blocked.
                    # Wait once, then let the model decide again on the settled page.
                    if any(a["id"].upper() == "WAIT" for a in page["actions"]):
                        state["loading_wait"] = True
                        time.sleep(1.0)
                        state["page"] = state["browser"].observe(
                            screenshot=self.screenshots, deadline=self.read_deadline()
                        )
                        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                        state["status"] = "ready"
                        return self.snapshot()
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if selected == "BLOCKED" and self._supervise("model_blocked"):
                    return self.snapshot()
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self._finalize()
                return self.snapshot()
            if selected == "ABSTAIN":
                # A model-side refusal. Terminal and honest: no mutation, no guessing.
                state["status"] = "abstained"
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self._finalize()
                return self.snapshot()
            if selected == "REOBSERVE":
                if self.stalled():
                    return self._stop_stalled()
                state["reobserves"] = state.get("reobserves", 0) + 1
                marks = state.setdefault("reobserved", {})
                marks[page["fingerprint"]] = True
                if len(marks) > GATE_MEMORY:
                    for stale in list(marks)[:-GATE_MEMORY]:
                        marks.pop(stale, None)
                state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                self.note_progress(state["page"])
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                state["status"] = "ready"
                return self.snapshot()
            if selected == "BACK":
                action = {"id": "BACK", "kind": "back", "label": "Back"}
            else:
                action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= self.step_budget():
                return self.stop_budget(f"{self.step_budget()}-action budget")
            # An exact-label memory fill is code-validated: the value is user data, and the
            # element is only trusted when exactly one fill action on the page carries that
            # label. A second field with the same label is the wrong-field risk, so
            # ambiguity falls back to the confidence gate like any other decision.
            memory_fill = False
            if action["kind"] == "fill" and getattr(self, "mode", None) is not None:
                wanted = modes.normalize_label(action.get("label"))
                same_label = [
                    a for a in page["actions"]
                    if a.get("kind") == "fill"
                    and modes.normalize_label(a.get("label")) == wanted
                ] if wanted else []
                memory_fill = (
                    len(same_label) == 1
                    and self.mode.value_for(action.get("label"), page.get("url")) is not None
                )
            verdict = None if memory_fill else gate_check(
                decision, action,
                irreversible_floor=getattr(self, "_irreversible_floor", None),
            )
            if verdict:
                # The gate never retries a mutation. First low reading: one fresh
                # observation with the decision cache bypassed. Second low reading on the
                # same page: refuse, and end the run as abstained rather than mutate.
                attempts = state.setdefault("gate_attempts", {})
                fingerprint = page["fingerprint"]
                count = attempts.get(fingerprint, 0)
                attempts[fingerprint] = count + 1
                if len(attempts) > GATE_MEMORY:
                    for stale in list(attempts)[:-GATE_MEMORY]:
                        attempts.pop(stale, None)
                if count == 0:
                    verdict["resolution"] = "reobserve"
                    state["gate"] = verdict
                    if evidence is not None:
                        evidence.record("gate", **verdict)
                    if self.stalled():
                        return self._stop_stalled()
                    state["skip_cache_once"] = True
                    state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                    self.note_progress(state["page"])
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    state["status"] = "ready"
                    return self.snapshot()
                verdict["resolution"] = (
                    "abstain" if verdict["irreversible"] >= IRREVERSIBLE_NOUL else "skipped"
                )
                state["gate"] = verdict
                if evidence is not None:
                    evidence.record("gate", **verdict)
                if verdict["resolution"] == "abstain":
                    state["status"] = "abstained"
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    self._finalize()
                    return self.snapshot()
                # A reversible action below its floor is never executed, but it does not
                # have to end the run: record it as a failed attempt so the loop's own
                # recovery routes to the next-best option, and remember the choice for this
                # page so a later decision cannot bounce back to it.
                skipped = state.setdefault("skipped", {})
                choices = skipped.setdefault(page["fingerprint"], [])
                if selected not in choices:
                    choices.append(selected)
                if len(skipped) > GATE_MEMORY:
                    for stale in list(skipped)[:-GATE_MEMORY]:
                        skipped.pop(stale, None)
                self._record_skipped(decision, action, verdict)
                self._reveal_below(page)
                state["status"] = "ready"
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            text, helper, text_source = None, None, None
            if action["kind"] in {"fill", "seek"}:
                supplied = self.supplied_text(action) if not decision.get("replay") else None
                remembered = None
                if (
                    supplied is None and not decision.get("replay")
                    and action["kind"] == "fill" and getattr(self, "mode", None)
                ):
                    # Exact normalized-label hit only: an approximate match could type one
                    # person's value into another field, so anything fuzzier goes to the
                    # text model with the memory in context instead.
                    remembered = self.mode.value_for(action["label"], page.get("url"))
                if decision.get("replay"):
                    text = decision.get("replay_text")
                    text_source = "replay"
                    state["text_calls"].append({
                        "model": "replay", "latency_ms": 0, "usage": {},
                        "field": action["label"], "value": "[redacted]",
                    })
                elif supplied is not None:
                    # No model call and nothing to cache: a supplied literal cannot go
                    # stale, and Browser.act re-checks freshness immediately before input.
                    text = supplied
                    text_source = "profile" if getattr(self, "profile_fill", None) else "supplied"
                    state["text_calls"].append(
                        {
                            "model": "supplied",
                            "latency_ms": 0,
                            "usage": {},
                            "field": action["label"],
                            "value": "[redacted]",
                        }
                    )
                elif remembered is not None:
                    # Learned memory, not a model call. The gate, step budget and freshness
                    # checks around this branch are the same as for any other execution.
                    text = remembered
                    text_source = "mode-memory"
                    state["text_calls"].append(
                        {
                            "model": "mode-memory",
                            "latency_ms": 0,
                            "usage": {},
                            "field": action["label"],
                            "value": "[redacted]",
                        }
                    )
                elif decision.get("supervisor_text"):
                    # The supervisor supplied the value with the element; user data from the
                    # caller and learned memory above still outrank it.
                    text = decision["supervisor_text"]
                    text_source = "supervisor"
                    state["text_calls"].append({
                        "model": "supervisor", "latency_ms": 0, "usage": {},
                        "field": action["label"], "value": "[redacted]",
                    })
                else:
                    text_source = "model"
                    if not state["browser"].fresh(page):
                        raise StalePage("Page changed before text generation. Choose again.")
                    context = field_context(
                        state["goal"], action, page, state["history"],
                        mode_state=self.mode.text_state(page.get("url")) if getattr(self, "mode", None) else None,
                    )
                    prefetched = self._take_prefetch(action, page)
                    if prefetched is not None:
                        text, helper = prefetched
                        state["text_calls"].append(
                            {**helper, "field": action["label"], "value": "[redacted]"}
                        )
                    elif self.pending_text and self.pending_text[0] == context:
                        _, text, helper = self.pending_text
                    else:
                        # Bound the model call by the hard backstop, so a slow text call
                        # cannot overrun the guarantee on its own; its wait is credited back
                        # to the browser-side clock. A call that cannot fit is allowed to
                        # fail: no action is taken and the backstop then ends the run.
                        left = self._model_room()
                        asked = time.monotonic()
                        try:
                            text, helper = field_text(
                                context,
                                timeout=min(TEXT_TIMEOUT, max(0.5, left)),
                                model=getattr(self, "_text_model", None),
                                base_url=getattr(self, "_text_base_url", None),
                            )
                        except Exception as exc:
                            # A slow or failed text call must be a failed ACTION, not a dead
                            # run. The helper's measured tail reaches 22 s; letting that
                            # abort the task turned a survivable slow call into exit 1.
                            text, helper = None, None
                            state["text_calls"].append(
                                {
                                    "model": "error",
                                    "latency_ms": 0,
                                    "usage": {},
                                    "field": action["label"],
                                    "value": None,
                                    "error": str(exc)[:200],
                                }
                            )
                        else:
                            self.pending_text = (context, text, helper)
                            state["text_calls"].append(
                                {**(helper or {}), "field": action["label"], "value": "[redacted]"}
                            )
                        finally:
                            self._credit_wait(asked)
            # Browser.act checks freshness immediately before input, including after text generation.
            error = None
            dispatch_error = False
            scan = None
            if action["kind"] == "scroll" and self.find and not target_visible(page, self.find):
                # One decision buys the whole scan. Every screen is still observed inside
                # Browser.scan, so the target cannot be scrolled past unseen.
                try:
                    scan_end = time.monotonic() + SCAN_MAX_S
                    scanned_page, screens, found = state["browser"].scan(
                        action, self.find, page,
                        deadline=lambda: min(scan_end, self.read_deadline() or scan_end),
                        on_observe=self.note_progress,
                    )
                    scan = {"target": self.find, "screens": screens, "found": found}
                    state["page"] = scanned_page
                except StalePage:
                    raise
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    dispatch_error = True
            elif action["kind"] in {"fill", "seek"} and text is None:
                # Nothing to type: record a failed action rather than dispatching an empty
                # insert or aborting the run.
                error = "no text was produced for this field"
            else:
                try:
                    state["browser"].act(action, page, text=text)
                except StalePage:
                    raise
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    dispatch_error = True
                finally:
                    # Mask only what came from a private source. A value generated from the
                    # goal, or a goal literal, must stay visible: masking every typed value
                    # turned "Zurich" into "[private]" in the field and in every autocomplete
                    # option, so Jev chose blind (introduced in 7fa3ced, fixed 2026-09-23).
                    private_fill = bool(text) and (
                        text_source in {"profile", "mode-memory"}
                        or modes.is_sensitive(action.get("label"))
                        or text in getattr(self, "private_values", ())
                    )
                    if action["kind"] in {"fill", "seek"} and private_fill:
                        self.private_values = getattr(self, "private_values", set())
                        self.private_values.add(text)
                        state["browser"].private_values = sorted(
                            self.private_values, key=len, reverse=True
                        )
                        self.screenshots = False
                        self.record_dir = None
                        state["record"] = False
                        self._private_pixels = True
            state["scan"] = scan
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision.get("probability", 0.0),
                    "confidence": decision["confidence"],
                    "irreversible": decision.get("irreversible"),
                    "cached": decision.get("cached"),
                    "model": decision.get("model"),
                    "latency_ms": decision["latency_ms"],
                    "text": "[redacted]" if text is not None else None,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "recovery": decision.get("recovery"),
                    "scan": scan,
                    "error": error,
                    "outcome": None,
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            if evidence is not None:
                record = state["history"][-1]
                evidence.record(
                    "execution",
                    step=record["step"],
                    action=record["action"],
                    action_kind=record["kind"],
                    choice=record["choice"],
                    operation=record["operation"],
                    target=record["target"],
                    confidence=record["confidence"],
                    probability=record["probability"],
                    irreversible=record.get("irreversible"),
                    cached=record.get("cached"),
                    model=record.get("model"),
                    recovery=record.get("recovery"),
                    error=record.get("error"),
                    url=record.get("url"),
                )
            if dispatch_error:
                # The browser may have performed the mutation before its reply was
                # lost. Re-observing is useful, but another model decision could
                # dispatch the same mutation again. End this run as uncertain.
                state["status"] = "blocked"
                state["uncertain_execution"] = True
                self._finalize()
                return self.snapshot()
            try:
                state["page"] = state["browser"].observe(
                    screenshot=self.screenshots, deadline=self.read_deadline()
                )
            except Exception as exc:
                # The input was already dispatched. An unreadable result gives no
                # evidence that repeating the input is safe.
                state["history"][-1]["outcome"] = "readback failed"
                state["history"][-1]["error"] = str(exc)[:200]
                state["status"] = "blocked"
                state["uncertain_execution"] = True
                self._finalize()
                return self.snapshot()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            outcome = action_outcome(page, state["page"], error)
            if outcome == "no visible change" and state["browser"].follow_new_tab():
                state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                outcome = "opened new tab"
            self.note_progress(state["page"])
            # Content yield: did this step show us anything we had not already seen? For a
            # scroll that is the whole question - a long list yields new items on every
            # scroll and the agent keeps going; a scroll that yields nothing new is the
            # loop, and that is what gets cut off.
            labels = {a.get("label") for a in state["page"]["actions"] if a.get("label")}
            seen = set(state.get("seen_labels", ()))
            revealed = labels - seen
            state["seen_labels"] = list(seen | labels)[-SEEN_LABEL_MEMORY:]
            state["last_revealed"] = len(revealed)
            if action["kind"] == "scroll" and not revealed:
                state["barren_scrolls"] = state.get("barren_scrolls", 0) + 1
            else:
                state["barren_scrolls"] = 0
            recent_fingerprints = {h.get("fingerprint") for h in state["history"][-9:-1] if h.get("fingerprint")}
            if outcome != "action failed" and state["page"]["fingerprint"] in recent_fingerprints:
                # The same state keeps coming back: an A/B loop, even when every action "changed" something.
                outcome = "loop detected"
                state["loop_detected"] = True
            state["history"][-1].update(
                outcome=outcome,
                page_changed=outcome != "no visible change",
                fingerprint=state["page"]["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if outcome in DEAD_OUTCOMES and decision.get("cache_key"):
                # A decision that led nowhere must not be served again for the same request:
                # measured, a 7-day cache replayed a no-effect click at 5 ms per lap, in this
                # run and the next. Evicting costs at most one paid decision later.
                cache.delete(decision["cache_key"])
                state["cache_evictions"] = state.get("cache_evictions", 0) + 1
                if evidence is not None:
                    evidence.record("cache_evicted", step=state["history"][-1]["step"],
                                    outcome=outcome, cached=bool(decision.get("cached")))
            if (
                state.get("descriptors") is not None
                and not decision.get("replay")
                and outcome not in DEAD_OUTCOMES
            ):
                # Descriptor of a successfully executed step, for a possible replay.
                # Credentials are never persisted: a redacted fill just falls back to Jev.
                state["descriptors"].append(
                    replay.descriptor(
                        action, decision, outcome,
                        modes.redact_for_replay(action.get("label"), text)
                        if text_source == "model" else None,
                    )
                )
            if (
                getattr(self, "mode", None) is not None
                and action["kind"] == "fill"
                and text
                and text_source == "model"
                and not decision.get("replay")
                and error is None
                and outcome not in DEAD_OUTCOMES
                and self._value_present(action, state["page"], text)
            ):
                # Learn only what the field actually accepted: a value the page rejected
                # would otherwise be retyped into every later run of this mode.
                if self.mode.learn(action["label"], text, url=state["page"].get("url")):
                    writer = getattr(self, "evidence", None)
                    if writer is not None:
                        writer.record(
                            "mode_learned", label=action["label"],
                            url=state["page"].get("url"),
                        )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            dead = len(repeated) == 3 and all(
                h.get("outcome") in DEAD_OUTCOMES and h["kind"] != "wait" for h in repeated
            )
            if dead and self.stalled():
                # Out of time: the anti-loop deadline outranks one more repair attempt.
                return self._stop_stalled()
            # Repair calls are model waits: bounded by the hard backstop, credited back to
            # the browser-side clock.
            room = self._model_room()
            failure = None
            if dead and room > MIN_DECIDE_S:
                # Classify before paying for the vision planner: a hard wall or a page that
                # is still loading needs no planner, and this is one cheap typed choice
                # instead of a screenshot round trip. None on any failure to answer.
                asked = time.monotonic()
                try:
                    failure = classify_failure(
                        state["page"], state["goal"], state["history"],
                        timeout=min(DECIDE_TIMEOUT, max(MIN_DECIDE_S, room - 0.2)),
                    )
                finally:
                    self._credit_wait(asked)
                if failure:
                    state["failure_class"] = failure
                    if evidence is not None:
                        evidence.record("failure_class", **failure)
            if failure and failure["class"] == "hard_wall":
                # The page itself is the wall; the planner would only burn the deadline.
                state["status"] = "blocked"
            elif failure and failure["class"] == "still_loading":
                if self.stalled():
                    return self._stop_stalled()
                if any(a["id"].upper() == "WAIT" for a in state["page"]["actions"]):
                    time.sleep(0.5)
                state["page"] = state["browser"].observe(screenshot=self.screenshots, deadline=self.read_deadline())
                self.note_progress(state["page"])
                state["status"] = "ready"
            elif dead and self._supervise("dead_streak"):
                # The supervisor set the next step (or ended the run); act() is done.
                return self.snapshot()
            else:
                state["status"] = "blocked" if dead else "ready"
            if state["status"] == "blocked":
                self._finalize()
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in TERMINAL_STATUS:
            try:
                yield self.command("tick")
            except (TimeoutError, ConnectionError) as error:
                # Every CDP call has its own IPC timeout. When one expires the browser has
                # effectively stopped answering, and that used to escape the generator as an
                # exception with status=None - so the run neither finished nor reported a
                # stall. A browser that stops responding is a stall: end it honestly.
                state = self.state
                state["stall"] = {
                    "seconds_without_progress": round(
                        time.monotonic() - (state.get("progress_at") or time.monotonic()), 1
                    ),
                    "limit_s": STALL_LIMIT_S,
                    "repeated_rounds": state.get("stall_rounds", 0),
                    "round_limit": STALL_MAX_ROUNDS,
                    "reason": f"browser stopped responding ({type(error).__name__})",
                    "last_action": state["history"][-1].get("action") if state["history"] else None,
                    "last_outcome": state["history"][-1].get("outcome") if state["history"] else None,
                }
                state["decision"] = None
                state["status"] = "stalled"
                if state.get("started_at") is not None:
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self._finalize()
                yield self.snapshot()

    def close(self):
        if getattr(self, "_owns_browser", True):
            # An injected browser belongs to the caller (the MCP warm session) and stays open.
            self.browser.close()
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            evidence.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
