"""Jev browser tool over MCP (stdio), with no MCP SDK dependency.

A long-lived server so the tool is registered once per agent session instead of paying a
process start, Python import and daemon handshake per call. Speaks newline-delimited
JSON-RPC 2.0 on stdin/stdout, which is what opencode, Command Code and Codex all launch.

Registered as `jev-browser`; tools appear as `jev-browser_<tool>` (opencode) or
`mcp__jev-browser__<tool>` (Command Code).

    opencode    "mcp": {"jev-browser": {"type": "local", "command": [...], "environment": {...}}}
    cmd         cmd mcp add --scope user jev-browser -- python jev_mcp_server.py
    codex       [mcp_servers.jev-browser]  command = "python"  args = ["jev_mcp_server.py"]

Anything written to stdout that is not JSON-RPC corrupts the protocol, so every tool call
runs with stdout redirected to stderr.

Limit worth knowing: the server keeps ONE warm tab per instance and navigates it to the
requested URL at the start of every call, so repeated calls skip daemon and tab setup
(pass `fresh: true` for a brand-new tab). browser_state / browser_act let a caller read
the indexed elements and execute one observed action without any model call.
"""

import contextlib
import functools
import io
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Own daemon and tab per server, so a second agent cannot silently drive this one's page.
# This must run before anything imports browser_harness: it reads BU_NAME at import time, and
# a top-level package import here (delegation -> browser) would freeze the shared "default"
# daemon for every server that starts with JEV_MCP_BU_NAME set.
os.environ.setdefault("BU_NAME", os.environ.get("JEV_MCP_BU_NAME", "jev-mcp"))

from AgentJevUltraFast.delegation import DelegatedTask, TaskRegistry  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "jev-browser"
SERVER_VERSION = "1.0.0"

# Where runs write their JSONL evidence: every offered candidate, answer, confidence and
# execution. `JEV_EVIDENCE_DIR=0` disables it; the default is repo-local and gitignored.
EVIDENCE_DIR = os.environ.get("JEV_EVIDENCE_DIR", str(ROOT / "artifacts" / "evidence"))
# Where replay plans live: identity-level step descriptors of a completed run, served only
# to the exact url+goal that recorded them. `JEV_REPLAY_DIR=0` disables replay.
REPLAY_DIR = os.environ.get("JEV_REPLAY_DIR", str(ROOT / "artifacts" / "replay"))
# A wedged page or daemon must not wedge the client: measured once as a 16-minute hang on a
# browser_state call against x.com. Every tool call runs under this wall-clock deadline;
# an abandoned call drops the warm session. <= 0 disables the guard.
CALL_TIMEOUT_S = float(os.environ.get("JEV_MCP_CALL_TIMEOUT_S", "120"))


def _evidence_dir():
    return None if EVIDENCE_DIR.strip().lower() in {"0", "off", "no", "false", ""} else EVIDENCE_DIR


def _replay_dir(wanted):
    if not wanted or REPLAY_DIR.strip().lower() in {"0", "off", "no", "false", ""}:
        return None
    return REPLAY_DIR


# --- one warm tab per server instance ---------------------------------------------------
# The caller picks the tab mode; a mode change rebuilds the session. Every call navigates
# to its own URL first, so a warm tab never leaks page state between verification runs.
_WARM = {"browser": None, "mode": None, "url": None}
_WARM_LOCK = threading.Lock()
_FENCED_WORKER = None
_TASKS = TaskRegistry()


def _warm_enabled():
    return os.environ.get("JEV_WARM", "1").strip().lower() not in {"0", "off", "no", "false"}


def _drop_warm():
    browser, _WARM["browser"] = _WARM["browser"], None
    _WARM["mode"] = _WARM["url"] = None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass


def _warm_browser(url, mode, fresh=False):
    """A live Browser for this instance, reused across calls unless `fresh`."""
    from AgentJevUltraFast.browser import Browser

    with _WARM_LOCK:
        browser = _WARM["browser"]
        if browser is not None and (mode != _WARM["mode"] or fresh):
            _drop_warm()
            browser = None
        if browser is not None:
            try:
                browser.evaluate("location.href")       # liveness probe
            except Exception:
                _drop_warm()
                browser = None
        if browser is None:
            browser = Browser(url)
            _WARM.update(browser=browser, mode=mode, url=url)
            return browser
        if url:
            try:
                browser.navigate(url)
            except Exception:
                _drop_warm()
                browser = Browser(url)
                _WARM.update(browser=browser, mode=mode, url=url)
            else:
                _WARM["url"] = url
        return browser


def _load_env():
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


TOOLS = [
    {
        "name": "browser_do",
        "description": (
            "Drive the real browser with the Jev agent to accomplish one goal, then verify. "
            "Returns status (the agent's own state) and outcome (the code-owned verdict: "
            "verified / refuted / unknown / abstained / budget_exhausted). Only `expect` "
            "assertions read off the real page produce `verified`; without them the outcome "
            "is `unknown`, because a DONE choice is not proof. Supply `values` for any field "
            "whose literal you already know: it skips a 1-3s model call per field and cannot "
            "mis-type a guess. The tab is warm and navigated fresh at the start of every call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "One natural-language goal, verbatim."},
                "url": {"type": "string", "description": "Start URL."},
                "tab": {"type": "string", "enum": ["background", "foreground", "active"],
                        "default": "background"},
                "values": {"type": "object", "description": "Field label -> literal text to type.",
                           "additionalProperties": {"type": "string"}},
                "find": {"type": "string", "description":
                         "Literal to scroll a long list to find (a video title, a row name). "
                         "Turns scanning into deterministic work: the code scrolls and observes "
                         "until the target appears, instead of one model call per screen. Every "
                         "screen is read, so nothing is skipped. Scrolls it into view; the agent "
                         "then acts on it."},
                "expect": {"type": "object", "description":
                           "url_contains, url_matches, title_contains, text_contains, text_absent, "
                           "selector_count ({selector, at_least}). This is the verdict."},
                "max_steps": {"type": "integer", "description": "Cap on actions for this run."},
                "max_seconds": {"type": "number", "default": 60},
                "keep_open": {"type": "boolean", "default": False,
                              "description": "Leave the tab open when the run finishes."},
                "fresh": {"type": "boolean", "default": False,
                          "description": "Force a brand-new tab instead of the warm one."},
                "mode": {"type": "string", "default": "",
                         "description": "Task-class mode: a slug, or 'auto' to match or create "
                                        "one with the text model. A mode adds its own memory "
                                        "(personal facts, standing instructions), learned "
                                        "field values and specialist settings such as the "
                                        "collect strategy for feed scrolling."},
                "collect_steps": {"type": "integer", "minimum": 1, "maximum": 50,
                                  "description": "Override the scroll count for a collect-strategy mode."},
                "replay": {"type": "boolean", "default": False,
                           "description": "Use (and record) a step plan for this exact url+goal. "
                                          "A recorded run repeats without a model call per step, "
                                          "falling back to Jev on the first mismatched step; the "
                                          "outcome is still decided by `expect`."},
                "plan": {"type": "array", "items": {"type": "string"}, "maxItems": 30,
                         "description": "Optional orchestrator step list. Given to the on-demand "
                                        "supervisor if the run gets stuck; Jev still decides "
                                        "every step from the goal. Put steps in `goal` to steer "
                                        "Jev directly."},
                "screenshot": {"type": "boolean", "default": False,
                               "description": "Return the final page as an image (JPEG) with the "
                                              "result - for 'navigate there and show me'. Skipped "
                                              "when a private value is on the page."},
            },
            "required": ["goal", "url"],
        },
    },
    {
        "name": "browser_state",
        "description": (
            "Read the current page as the indexed element table the agent would see: one "
            "entry per observed element with the operations it supports, plus the raw observed "
            "actions. No model call and nothing is clicked. Use it to inspect a page, then pass "
            "an action id and the fingerprint to browser_act."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Navigate the warm tab here first."},
                "tab": {"type": "string", "enum": ["background", "foreground", "active"],
                        "default": "background"},
                "fresh": {"type": "boolean", "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "browser_act",
        "description": (
            "Execute ONE action from a browser_state table: pass the observed action id, the "
            "operation it supports, the fingerprint you read, and (for TYPE_TEXT/SEEK) the text. "
            "The id must exist on the live page and the fingerprint must still match, so a stale "
            "step is refused rather than dispatched. No Jev call: the caller chose the action, "
            "code enforces it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "element": {"type": "string", "description": "Action id from browser_state."},
                "operation": {"type": "string", "enum": ["CLICK", "TYPE_TEXT", "SELECT", "SEEK"]},
                "text": {"type": "string", "description": "Value for TYPE_TEXT or SEEK."},
                "fingerprint": {"type": "string", "description": "Fingerprint from browser_state."},
                "url": {"type": "string"},
                "tab": {"type": "string", "enum": ["background", "foreground", "active"],
                        "default": "background"},
            },
            "required": ["element", "operation", "fingerprint"],
        },
    },
    {
        "name": "browser_assert",
        "description": (
            "Read the current page and evaluate structured assertions against it. No model is "
            "called and nothing is clicked: this is the independent check that decides whether a "
            "flow actually worked. Opens the url, settles, and reports per-assertion pass/fail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "expect": {"type": "object", "description": "Same keys as browser_do."},
                "tab": {"type": "string", "enum": ["background", "foreground"], "default": "background"},
            },
            "required": ["url", "expect"],
        },
    },
    {
        "name": "browser_checks",
        "description": (
            "Run several browser checks in PARALLEL, each in its own browser instance and tab, "
            "and return a per-check pass/fail report. Use this for app testing instead of calling "
            "browser_do once per flow. Each check: {name, path, goal, values?, expect?} - the "
            "expect assertions are the verdict."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "description": "e.g. http://localhost:3000"},
                "checks": {"type": "array", "items": {"type": "object"},
                           "description": "Each: {name, path, goal, values?, expect?}."},
                "workers": {"type": "integer", "default": 4, "minimum": 1, "maximum": 10},
            },
            "required": ["base_url", "checks"],
        },
    },
]

TOOLS.extend([
    {
        "name": "browser_task",
        "description": (
            "Start one delegated goal in its own visible Chrome tab. Runs a bounded slice, "
            "then returns a verified terminal result or a task checkpoint. A login, one-time "
            "code, CAPTCHA, or consequential action yields needs_human. Resume the same tab "
            "with browser_task_resume; do not start the goal again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "url": {"type": "string"},
                "expect": {"type": "object", "description": "Independent page assertions; required for verified."},
                "values": {"type": "object", "additionalProperties": {"type": "string"}},
                "profile": {"type": "boolean", "default": True,
                            "description": "Use approved local profile fields, if configured."},
                "mode": {"type": "string", "description": "Existing task-class slug or auto."},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 60},
                "max_active_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 120},
                "slice_seconds": {"type": "number", "minimum": 1, "maximum": 15, "default": 12},
                "keep_open": {"type": "boolean", "default": False},
            },
            "required": ["goal", "url"],
        },
    },
    {
        "name": "browser_task_resume",
        "description": (
            "Continue the same delegated task and Chrome tab from a checkpoint. Set "
            "approve_action=true only after the user approves the exact proposed action. "
            "A login checkpoint needs the user to finish in Chrome first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "checkpoint": {"type": "integer"},
                "approve_action": {"type": "boolean", "default": False},
                "slice_seconds": {"type": "number", "minimum": 1, "maximum": 15, "default": 12},
            },
            "required": ["task_id", "checkpoint"],
        },
    },
    {
        "name": "browser_task_cancel",
        "description": "Cancel a yielded delegated task. Already executed browser actions cannot be undone.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "checkpoint": {"type": "integer"}},
            "required": ["task_id", "checkpoint"],
        },
    },
    {
        "name": "browser_test",
        "description": (
            "Run a test suite against a web app (usually on localhost) and return a run_id at "
            "once; poll browser_test_status for the verdict. The suite is a Markdown spec: "
            "'## Test name' headings, 'path: /route', numbered steps that are plain-English goals, "
            "and check bullets under each step ('- see: X', '- not see: X', '- url contains: X', "
            "'- see any: A | B', '- no page errors'). '## Before each' steps (e.g. sign in) run "
            "first in every test. Checks read off the real page are the verdict. Verified flows are "
            "recorded and replayed with no model calls on later runs, so re-running is ~free. "
            "Pass spec_path (a .md/.json file in the project; plans persist next to it in .jev/) "
            "or spec_markdown (inline text, plus base_url if the text has none)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec_path": {"type": "string", "description": "Absolute path to a .md or .json suite."},
                "spec_markdown": {"type": "string", "description": "Inline Markdown suite."},
                "base_url": {"type": "string", "description": "Overrides the suite's base_url."},
                "workers": {"type": "integer", "default": 4, "minimum": 1, "maximum": 10},
                "fresh": {"type": "boolean", "default": False,
                          "description": "Forget recorded plans and decide every step again."},
            },
        },
    },
    {
        "name": "browser_test_status",
        "description": (
            "The state of a browser_test run: running (with how many tests have finished), or the "
            "final report - passed/total, cost, and per test the verdict, failed step, failing "
            "checks and an excerpt of what the page showed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "browser_crawl",
        "description": (
            "Find broken pages with no model calls: open every same-origin page the app links to "
            "(plain navigation, never a click, sign-out/delete links skipped), breadth first from "
            "url, and report each page's JavaScript errors, blank pages and error pages. Runs in "
            "the dedicated automation Chrome; a good first look at an app before writing tests."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The running app's start page."},
                "max_pages": {"type": "integer", "default": 15, "minimum": 1, "maximum": 25},
            },
            "required": ["url"],
        },
    },
])

# These are hints for MCP hosts, not an authorization mechanism. Even a state
# read can navigate a tab when a URL is supplied, so it is not advertised as
# read-only.
for _tool in TOOLS:
    _tool["annotations"] = {
        "readOnlyHint": False,
        "destructiveHint": _tool["name"] in {
            "browser_do", "browser_act", "browser_checks", "browser_task",
            "browser_task_resume",
        },
        "idempotentHint": False,
        "openWorldHint": True,
    }


def _tab_env(func):
    """Set JEV_TAB_MODE from the call's `tab` arg for the duration of the handler.

    The handlers used to mutate os.environ and never restore it, so one call's tab mode
    could define the next call's (and, with an interleaving client, a concurrent one).
    """
    @functools.wraps(func)
    def wrapper(args):
        previous = os.environ.get("JEV_TAB_MODE")
        os.environ["JEV_TAB_MODE"] = (args or {}).get("tab", "background")
        try:
            return func(args)
        finally:
            if previous is None:
                os.environ.pop("JEV_TAB_MODE", None)
            else:
                os.environ["JEV_TAB_MODE"] = previous
    return wrapper


def _quiet(func):
    """Run with stdout redirected so nothing but JSON-RPC reaches the client."""
    def wrapper(*args, **kwargs):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = func(*args, **kwargs)
        noise = buffer.getvalue().strip()
        if noise:
            print(f"[{func.__name__}] {noise[:2000]}", file=sys.stderr, flush=True)
        return result
    return wrapper


@_quiet
@_tab_env
def tool_browser_do(args):
    _load_env()
    from AgentJevUltraFast import Agent
    from AgentJevUltraFast.assertions import normalize_expect
    from AgentJevUltraFast.outcomes import resolve
    from AgentJevUltraFast.privacy import redact

    mode = args.get("tab", "background")
    os.environ["JEV_TAB_MODE"] = mode
    # Validate before touching the browser: a malformed expectation fails the call.
    expect = normalize_expect(args.get("expect"))
    max_steps = args.get("max_steps")
    previous_steps = os.environ.get("JEV_MAX_STEPS")
    if max_steps:
        os.environ["JEV_MAX_STEPS"] = str(int(max_steps))
    browser = None
    try:
        if _warm_enabled() or args.get("keep_open"):
            browser = _warm_browser(args["url"], mode, fresh=bool(args.get("fresh")))
        kwargs = {
            "values": args.get("values"),
            "find": args.get("find"),
            "expect": expect,
            "evidence_dir": _evidence_dir(),
            "replay_dir": _replay_dir(args.get("replay")),
            "mode": args.get("mode") or None,
            "collect_steps": args.get("collect_steps"),
            "steps": args.get("plan") or None,
        }
        if browser is not None:
            kwargs["browser"] = browser
        image = None
        with Agent(args["url"], args["goal"], **kwargs) as agent:
            deadline = float(args.get("max_seconds", 60))
            started = time.monotonic()
            for _ in agent.run():
                if time.monotonic() - started > deadline:
                    agent.stop_budget(f"{deadline:g}s MCP budget")
                    break
            state = agent.snapshot()
            # The verdict is the agent's own final readback (_finalize): one independent
            # read of the live page, the same one written to evidence. A second read here
            # could disagree with it if the page moved in between.
            location = state.get("readback")
            if location is None:
                try:
                    location = (browser or agent.browser).evaluate(
                        "({url: location.href, title: document.title})"
                    )
                except Exception:
                    location = None
            if args.get("screenshot"):
                image = _final_screenshot(agent, browser or agent.browser)
    finally:
        if max_steps:
            if previous_steps is None:
                os.environ.pop("JEV_MAX_STEPS", None)
            else:
                os.environ["JEV_MAX_STEPS"] = previous_steps
        if browser is not None and not _warm_enabled() and not args.get("keep_open"):
            browser.close()

    detail = state.get("checks") or []
    outcome = state.get("outcome") or resolve(
        state["status"], checks=None, abstained=state["status"] == "abstained",
        budget_exhausted=state["status"] == "stalled",
    )
    verified = {"verified": True, "refuted": False}.get(outcome) if expect else None
    result = redact({
        "status": state["status"],
        "outcome": outcome,
        "verified": verified,
        "checks": detail,
        "url": (location or {}).get("url"),
        "title": (location or {}).get("title"),
        "readback_error": state.get("readback_error"),
        "elapsed_ms": state.get("elapsed_ms"),
        "evidence": state.get("evidence"),
        "replay": state.get("replay"),
        "mode": state.get("mode"),
        "harvest": state.get("harvest"),
        # What each caller-directed scan did, so the caller can see how far it had to look.
        "scans": [h["scan"] for h in state["history"] if h.get("scan")],
        "stall": state.get("stall"),
        "blocked_reason": state.get("blocked_reason"),
        # Every on-demand supervisor consult: which check fired, what it answered, how long.
        "supervision": state.get("supervision") or [],
        "needs_human": state.get("needs_human"),
        "gate": state.get("gate"),
        "failure_class": state.get("failure_class"),
        "actions": sum(1 for h in state["history"] if not h.get("skipped")),
        "skipped": sum(1 for h in state["history"] if h.get("skipped")),
        "cache": {"hit": state.get("cache_hits", 0), "miss": state.get("cache_misses", 0)},
        "text_calls": state["text_calls"],
        "trail": [
            {"action": h.get("action"), "outcome": h.get("outcome"), "error": h.get("error")}
            for h in state["history"][-12:]
        ],
    }, getattr(agent, "private_values", ()))
    if args.get("screenshot"):
        result["screenshot"] = image["note"]
        if image.get("data"):
            result["_image"] = image["data"]
    return result


def _final_screenshot(agent, browser):
    """The final page as base64 JPEG, or a note saying why not. Never raises."""
    if getattr(agent, "_private_pixels", False):
        return {"note": "skipped: a private value is on the page"}
    try:
        return {"note": "attached", "data": browser.screenshot(quality=70)}
    except Exception as error:
        return {"note": f"unavailable: {type(error).__name__}"}


ACT_KINDS = {"CLICK": "click", "TYPE_TEXT": "fill", "SELECT": "select", "SEEK": "seek"}


@_quiet
@_tab_env
def tool_browser_state(args):
    _load_env()
    from AgentJevUltraFast.model import action_space

    mode = args.get("tab", "background")
    os.environ["JEV_TAB_MODE"] = mode
    browser = _warm_browser(args.get("url") or "", mode, fresh=bool(args.get("fresh")))
    page = browser.observe(screenshot=False)
    elements, _targets, controls = action_space(page["actions"])
    return {
        "url": page["url"],
        "title": page["title"],
        "fingerprint": page["fingerprint"],
        "elements": elements,
        "controls": sorted(controls),
        "actions": [
            {k: a.get(k) for k in ("id", "kind", "label", "role", "value", "checked", "selected")}
            for a in page["actions"]
        ],
        "text": (page.get("text") or "")[:2000],
    }


@_quiet
@_tab_env
def tool_browser_act(args):
    _load_env()
    mode = args.get("tab", "background")
    os.environ["JEV_TAB_MODE"] = mode
    operation = str(args.get("operation", "")).upper()
    kind = ACT_KINDS.get(operation)
    if kind is None:
        return {"executed": None, "error": f"operation must be one of {sorted(ACT_KINDS)}"}
    # Action ids are positional in the observed table. Without the fingerprint of the table
    # they were read from, a re-render could make the same id name a different element.
    if not args.get("fingerprint"):
        return {"executed": None,
                "error": "fingerprint is required: pass the one browser_state returned"}
    browser = _warm_browser(args.get("url") or "", mode)
    page = browser.observe(screenshot=False)
    if args["fingerprint"] != page["fingerprint"]:
        return {"executed": None, "stale": True, "fingerprint": page["fingerprint"],
                "error": "page changed since browser_state; read state again"}
    element = str(args.get("element", ""))
    action = next((a for a in page["actions"] if a["id"] == element and a["kind"] == kind), None)
    if action is None:
        return {"executed": None, "fingerprint": page["fingerprint"],
                "error": f"{element!r} is not an observed {operation} target"}
    text = args.get("text")
    if kind in {"fill", "seek"}:
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return {"executed": None, "error": f"{operation} needs text (1-2000 characters)"}
    else:
        text = None
    try:
        browser.act(action, page, text=text)
    except Exception as error:
        return {"executed": None, "fingerprint": page["fingerprint"],
                "error": f"{type(error).__name__}: {error}"}
    after = browser.observe(screenshot=False)
    return {
        "executed": element,
        "operation": operation,
        "page_changed": after["fingerprint"] != page["fingerprint"],
        "url": after["url"],
        "title": after["title"],
        "fingerprint": after["fingerprint"],
    }


@_quiet
@_tab_env
def tool_browser_assert(args):
    _load_env()
    from AgentJevUltraFast.assertions import (
        evaluate_expectations,
        normalize_expect,
        page_facts,
        selectors_for,
        value_selectors_for,
    )
    from AgentJevUltraFast.browser import Browser

    expect = normalize_expect(args.get("expect"))
    if not expect:
        raise ValueError("browser_assert needs at least one expect key")
    os.environ["JEV_TAB_MODE"] = args.get("tab", "background")
    os.environ.pop("JEV_KEEP_TAB", None)
    # Its own tab on purpose: an assertion must not disturb the warm agent session.
    browser = Browser(args["url"])
    try:
        facts = page_facts(browser, selectors_for(expect), value_selectors_for(expect))
    finally:
        browser.close()
    verified, detail = evaluate_expectations(expect, facts)
    return {"verified": verified, "checks": detail, "url": facts["url"], "title": facts["title"]}


@_quiet
def tool_browser_checks(args):
    _load_env()
    import subprocess
    import tempfile

    # Each invocation owns both files. A failed preflight must never return a prior
    # invocation's report, and concurrent MCP server processes must not race.
    with tempfile.TemporaryDirectory(prefix="jev-mcp-checks-") as temp_dir:
        out = Path(temp_dir) / "report.json"
        spec_path = Path(temp_dir) / "spec.json"
        spec_path.write_text(json.dumps({
            "base_url": args["base_url"],
            "defaults": {"max_seconds": 60, "tab": "background"},
            "checks": args["checks"],
        }), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "AgentJevUltraFast.checks", str(spec_path),
             "--workers", str(min(int(args.get("workers", 4)), 10)), "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if not out.exists():
            return {"error": "checks did not run", "exit_code": proc.returncode,
                    "detail": (proc.stdout or proc.stderr)[-800:]}
        report = json.loads(out.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not all(k in report for k in ("passed", "total", "results")):
        return {"error": "checks returned an invalid report", "exit_code": proc.returncode}
    return {
        "passed": report["passed"], "total": report["total"],
        "wall_s": report["wall_s"], "serial_equivalent_s": report["serial_equivalent_s"],
        "results": [
            {"name": r.get("name"), "status": r.get("status"), "verified": r.get("verified"),
             "wall_s": r.get("wall_s"), "checks": r.get("checks"), "error": r.get("error")}
            for r in report["results"]
        ],
    }


@_quiet
def tool_browser_task(args):
    _load_env()
    from AgentJevUltraFast import Agent
    from AgentJevUltraFast.browser import Browser
    from AgentJevUltraFast.profile import ProfileStore

    if not args.get("goal") or not args.get("url"):
        raise ValueError("browser_task needs a goal and start URL")
    try:
        float(args.get("slice_seconds", 12))
    except (TypeError, ValueError):
        raise ValueError("slice_seconds must be a number") from None
    reservation = _TASKS.reserve()
    previous_tab = os.environ.get("JEV_TAB_MODE")
    os.environ["JEV_TAB_MODE"] = "foreground"
    browser = None
    agent = None
    try:
        profile = ProfileStore() if args.get("profile", True) else None
        with _WARM_LOCK:
            _drop_warm()
        browser = Browser(args["url"], follow_tabs=False)
        if profile is not None:
            browser.private_values = profile.protected_values()
        if reservation.is_set():
            raise ValueError("Browser task setup was abandoned")
        agent = Agent(
            args["url"], args["goal"], browser=browser,
            values=args.get("values"), expect=args.get("expect"),
            evidence_dir=_evidence_dir(), mode=args.get("mode") or None,
        )
        if args.get("max_steps") is not None:
            agent._max_steps = max(1, min(60, int(args["max_steps"])))
        task = DelegatedTask(
            agent, browser,
            max_active_seconds=max(1, min(600, float(args.get("max_active_seconds", 120)))),
            keep_open=bool(args.get("keep_open")),
            profile=profile,
        )
        _TASKS.add(task, reservation=reservation)
    except Exception:
        if agent is not None:
            agent.close()
        if browser is not None:
            browser.close()
        raise
    finally:
        _TASKS.release_reservation(reservation)
        if previous_tab is None:
            os.environ.pop("JEV_TAB_MODE", None)
        else:
            os.environ["JEV_TAB_MODE"] = previous_tab
    try:
        result = task.drive(slice_seconds=args.get("slice_seconds", 12))
    except Exception:
        _fail_task(task)
        raise
    _TASKS.release_if_terminal(task)
    return result


def _fail_task(task):
    """An exception escaped a task's active slice: end it so the registry frees the slot.

    Without a terminal phase release_if_terminal() is a no-op, the registry keeps a task
    whose cancel flag makes it unresumable, and every later tool call is refused until the
    server restarts.
    """
    task.abandon()
    task._close()
    task.phase = "abandoned"
    task.reason = task.reason or "error"
    _TASKS.release_if_terminal(task)


@_quiet
def tool_browser_task_resume(args):
    task = _TASKS.get(args["task_id"])
    try:
        result = task.resume(
            args["checkpoint"], approve_action=bool(args.get("approve_action")),
            slice_seconds=args.get("slice_seconds", 12),
        )
    except Exception:
        if task.phase not in {"paused", "needs_human"}:
            _fail_task(task)
        raise
    _TASKS.release_if_terminal(task)
    return result


@_quiet
def tool_browser_task_cancel(args):
    task = _TASKS.get(args["task_id"])
    if int(args["checkpoint"]) != task.checkpoint:
        raise ValueError("Stale task checkpoint")
    result = task.cancel()
    _TASKS.release_if_terminal(task)
    return result


_TEST_RUNS = {}
TEST_RUNS_DIR = Path.home() / ".jev-ultrafast" / "mcp-tests"


def tool_browser_test(args):
    """Start a suite as a detached child process; never holds the MCP call open."""
    import subprocess
    import uuid

    run_id = uuid.uuid4().hex[:12]
    run_dir = TEST_RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.get("spec_path"):
        spec = Path(args["spec_path"]).expanduser().resolve()
        if not spec.exists():
            return {"error": f"no such spec: {spec}"}
    elif args.get("spec_markdown"):
        spec = run_dir / "suite.md"
        spec.write_text(args["spec_markdown"], encoding="utf-8")
    else:
        return {"error": "pass spec_path or spec_markdown"}
    try:
        from AgentJevUltraFast.checks import load_suite

        suite = load_suite(spec)
    except SystemExit as error:          # a malformed spec is the caller's to fix, before any browser
        return {"error": str(error)}
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}
    report = spec.parent / ".jev" / "reports" / f"{spec.stem}.json"
    report.unlink(missing_ok=True)
    command = [sys.executable, "-m", "AgentJevUltraFast.cli", "test", str(spec),
               "--workers", str(min(int(args.get("workers", 4)), 10))]
    if args.get("fresh"):
        command.append("--fresh")
    log = open(run_dir / "run.log", "w", encoding="utf-8")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    env = dict(os.environ)
    if args.get("base_url"):
        # Overrides the suite's base_url for every format (the runner reads it first).
        env["JEV_TRAP_BASE"] = str(args["base_url"]).rstrip("/")
    elif not (suite.get("base_url") or "").strip():
        log.close()
        return {"error": "the suite has no base_url; pass base_url"}
    proc = subprocess.Popen(command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, **flags)
    _TEST_RUNS[run_id] = {"proc": proc, "report": report, "log": run_dir / "run.log", "log_file": log,
                          "started": time.monotonic(), "tests": len(suite.get("checks") or []),
                          "parts": report.parent / f"{spec.stem}_parts"}
    return {"run_id": run_id, "tests": len(suite.get("checks") or []), "status": "running",
            "hint": "poll browser_test_status every 15-30 s"}


CRAWL_TIMEOUT_S = 100       # under the MCP call cap; 25 pages take ~20 s on a local dev server


def tool_browser_crawl(args):
    import subprocess
    import uuid

    url = str(args.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be an http(s) URL"}
    pages = max(1, min(25, int(args.get("max_pages") or 15)))
    TEST_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = TEST_RUNS_DIR / f"crawl-{uuid.uuid4().hex[:8]}.json"
    command = [sys.executable, "-m", "AgentJevUltraFast.cli", "crawl", url, "--max-pages", str(pages),
               "--out", str(out)]
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=CRAWL_TIMEOUT_S, stdin=subprocess.DEVNULL, **flags)
    except subprocess.TimeoutExpired:
        return {"error": f"crawl did not finish in {CRAWL_TIMEOUT_S}s"}
    if not out.exists():
        return {"error": "crawl produced no report (is the app running at that url?)"}
    report = json.loads(out.read_text(encoding="utf-8"))
    out.unlink(missing_ok=True)
    return {"pages": len(report["pages"]), "problems": report["problems"], "model_calls": 0,
            "unvisited": len(report.get("unvisited") or []),
            "problem_pages": [{k: page.get(k) for k in ("path", "status", "note", "found_on")}
                              for page in report["pages"] if page["status"] not in {"ok", "left-origin"}]}


def tool_browser_test_status(args):
    run = _TEST_RUNS.get(args.get("run_id"))
    if run is None:
        return {"error": "unknown run_id (runs live as long as this MCP server process)"}
    code = run["proc"].poll()
    elapsed = round(time.monotonic() - run["started"], 1)
    if code is None:
        finished = (sum(1 for part in run["parts"].glob("*.json") if not part.name.startswith("99-"))
                    if run["parts"].exists() else 0)            # 99-* is Before all, not a test
        return {"status": "running", "elapsed_s": elapsed, "finished": finished, "tests": run["tests"]}
    if run.get("log_file") and not run["log_file"].closed:
        run["log_file"].close()
    if not run["report"].exists():
        tail = run["log"].read_text(encoding="utf-8", errors="replace")[-1200:] if run["log"].exists() else ""
        return {"status": "error", "exit_code": code, "detail": tail}
    report = json.loads(run["report"].read_text(encoding="utf-8"))
    setup = report.get("before_all")
    return {
        "status": "finished", "elapsed_s": elapsed, "passed": report.get("passed"),
        # When setup fails every test reads "not run": say why, once, with what the page showed.
        **({"before_all": {"verified": setup.get("verified"), "status": setup.get("status"),
                           "failed_step": setup.get("failed_step"), "error": setup.get("error"),
                           "failing_checks": [c for c in (setup.get("checks") or []) if not c.get("pass")],
                           "page_excerpt": (setup.get("page_excerpt") or "")[:400]
                           if not setup.get("verified") else None}} if setup else {}),
        "total": report.get("total"), "usd_estimate": report.get("usd_estimate"),
        "model_calls": report.get("model_calls"), "steps_replayed": report.get("steps_replayed"),
        "report_path": str(run["report"]),
        "results": [
            {"name": r.get("name"), "verified": r.get("verified"), "status": r.get("status"),
             "failed_step": r.get("failed_step"),
             "failing_checks": [c for c in (r.get("checks") or []) if not c.get("pass")],
             "page_excerpt": (r.get("page_excerpt") or "")[:400] if not r.get("verified") else None,
             "error": r.get("error"), "usd": (r.get("cost") or {}).get("usd_estimate")}
            for r in report.get("results", [])
        ],
    }


HANDLERS = {"browser_do": tool_browser_do, "browser_assert": tool_browser_assert,
            "browser_test": tool_browser_test, "browser_test_status": tool_browser_test_status,
            "browser_crawl": tool_browser_crawl,
            "browser_checks": tool_browser_checks, "browser_state": tool_browser_state,
            "browser_act": tool_browser_act, "browser_task": tool_browser_task,
            "browser_task_resume": tool_browser_task_resume,
            "browser_task_cancel": tool_browser_task_cancel}


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _bounded(handler, arguments, timeout=None):
    """Run a tool handler under a wall-clock deadline.

    An abandoned worker may still be executing. Fence the entire server until it exits;
    closing its browser while it is running would let it reattach to another task's tab.
    """
    global _FENCED_WORKER
    if _FENCED_WORKER is not None:
        if _FENCED_WORKER.is_alive():
            return {"error": "a previous tool call is still running after timeout; browser access is fenced",
                    "abandoned": True}
        _FENCED_WORKER = None
        _drop_warm()
        task = _TASKS.current
        if task is not None and task.cancel_requested.is_set():
            task._close()
            task.phase = "abandoned"
            _TASKS.release_if_terminal(task)
    limit = CALL_TIMEOUT_S if timeout is None else float(timeout)
    if limit <= 0:
        return handler(arguments)
    outcome = {}

    def run():
        try:
            outcome["value"] = handler(arguments)
        except Exception as error:      # re-raised on the caller's thread below
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True, name="jev-mcp-tool")
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        _FENCED_WORKER = worker
        _TASKS.abandon_active()
        return {
            "error": (
                f"tool call exceeded {limit:g}s and was abandoned; the page never answered. "
                "Treat the run as stalled and do not retry it."
            ),
            "abandoned": True,
        }
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def handle(request):
    method = request.get("method")
    rid = request.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": request.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Drive the user's real Chrome with the Jev browser agent. browser_do runs one "
                "natural-language goal and returns a code-owned outcome: `verified` only when "
                "`expect` assertions were read off the page, `unknown` otherwise. browser_checks "
                "runs many flows in parallel. browser_state lists the indexed elements; "
                "browser_act executes one observed action from that table. browser_assert checks "
                "a page with no model call."
            ),
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602,
                                                           "message": f"Unknown tool: {name}"}}
        if _FENCED_WORKER is None or not _FENCED_WORKER.is_alive():
            # Never while a timed-out worker may still be using the task's browser.
            _TASKS.expire_stale()
        current = _TASKS.current
        if current is not None and name not in {"browser_task_resume", "browser_task_cancel"}:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": (
                    "A delegated browser task owns this Chrome session. Resume or cancel it "
                    "before using another browser tool: "
                    f"task_id={current.id} checkpoint={current.checkpoint} phase={current.phase}."
                )}],
                "isError": True,
            }}
        try:
            result = _bounded(handler, params.get("arguments") or {})
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                "isError": True,
            }}
        image = result.pop("_image", None) if isinstance(result, dict) else None
        content = [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
        if image:
            content.append({"type": "image", "data": image, "mimeType": "image/jpeg"})
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": content,
            "isError": isinstance(result, dict) and bool(result.get("error")),
        }}
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601,
                                                   "message": f"Method not found: {method}"}}


def main():
    print(f"{SERVER_NAME} listening on stdio (BU_NAME={os.environ['BU_NAME']})",
          file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(f"ignoring non-JSON input: {line[:200]}", file=sys.stderr, flush=True)
            continue
        response = handle(request)
        if response is not None:
            send(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
