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

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "jev-browser"
SERVER_VERSION = "1.0.0"
# Own daemon and tab per server, so a second agent cannot silently drive this one's page.
os.environ.setdefault("BU_NAME", os.environ.get("JEV_MCP_BU_NAME", "jev-mcp"))

# Where runs write their JSONL evidence: every offered candidate, answer, confidence and
# execution. `JEV_EVIDENCE_DIR=0` disables it; the default is repo-local and gitignored.
EVIDENCE_DIR = os.environ.get("JEV_EVIDENCE_DIR", str(ROOT / "artifacts" / "evidence"))
# Where replay plans live: identity-level step descriptors of a completed run, served only
# to the exact url+goal that recorded them. `JEV_REPLAY_DIR=0` disables replay.
REPLAY_DIR = os.environ.get("JEV_REPLAY_DIR", str(ROOT / "artifacts" / "replay"))


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
    from jev_ultrafast.browser import Browser

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
            "required": ["element", "operation"],
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
def tool_browser_do(args):
    _load_env()
    from jev_ultrafast import Agent
    from jev_ultrafast.assertions import (
        evaluate_expectations,
        page_facts,
        selectors_for,
        value_selectors_for,
    )
    from jev_ultrafast.outcomes import resolve

    mode = args.get("tab", "background")
    os.environ["JEV_TAB_MODE"] = mode
    expect = args.get("expect") or None
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
        }
        if browser is not None:
            kwargs["browser"] = browser
        with Agent(args["url"], args["goal"], **kwargs) as agent:
            deadline = float(args.get("max_seconds", 60))
            started = time.monotonic()
            for _ in agent.run():
                if time.monotonic() - started > deadline:
                    break
            state = agent.snapshot()
            # Independent of the agent's opinion: read the page again and assert on it.
            facts = page_facts(
                browser or agent.browser, selectors_for(expect), value_selectors_for(expect)
            )
    finally:
        if max_steps:
            if previous_steps is None:
                os.environ.pop("JEV_MAX_STEPS", None)
            else:
                os.environ["JEV_MAX_STEPS"] = previous_steps
        if browser is not None and not _warm_enabled() and not args.get("keep_open"):
            browser.close()

    verified, detail = evaluate_expectations(expect, facts)
    outcome = resolve(
        state["status"], checks=detail if expect else None,
        budget_exhausted=state["status"] == "stalled" or bool(state.get("stall")),
    )
    return {
        "status": state["status"],
        "outcome": outcome,
        "verified": verified,
        "checks": detail,
        "url": facts["url"],
        "title": facts["title"],
        "elapsed_ms": state.get("elapsed_ms"),
        "evidence": state.get("evidence"),
        "replay": state.get("replay"),
        "mode": state.get("mode"),
        "harvest": state.get("harvest"),
        # What each caller-directed scan did, so the caller can see how far it had to look.
        "scans": [h["scan"] for h in state["history"] if h.get("scan")],
        "stall": state.get("stall"),
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
    }


ACT_KINDS = {"CLICK": "click", "TYPE_TEXT": "fill", "SELECT": "select", "SEEK": "seek"}


@_quiet
def tool_browser_state(args):
    _load_env()
    from jev_ultrafast.model import action_space

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
def tool_browser_act(args):
    _load_env()
    mode = args.get("tab", "background")
    os.environ["JEV_TAB_MODE"] = mode
    operation = str(args.get("operation", "")).upper()
    kind = ACT_KINDS.get(operation)
    if kind is None:
        return {"executed": None, "error": f"operation must be one of {sorted(ACT_KINDS)}"}
    browser = _warm_browser(args.get("url") or "", mode)
    page = browser.observe(screenshot=False)
    if args.get("fingerprint") and args["fingerprint"] != page["fingerprint"]:
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
def tool_browser_assert(args):
    _load_env()
    from jev_ultrafast.assertions import evaluate_expectations, page_facts, selectors_for, value_selectors_for
    from jev_ultrafast.browser import Browser

    expect = args.get("expect") or {}
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

    out = Path(tempfile.gettempdir()) / "jev-mcp-checks.json"
    spec_path = Path(tempfile.gettempdir()) / "jev-mcp-checks-spec.json"
    spec_path.write_text(json.dumps({
        "base_url": args["base_url"],
        "defaults": {"max_seconds": 60, "tab": "background"},
        "checks": args["checks"],
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_checks.py"), str(spec_path),
         "--workers", str(min(int(args.get("workers", 4)), 10)), "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if not out.exists():
        return {"error": "checks did not run", "detail": (proc.stdout or proc.stderr)[-800:]}
    report = json.loads(out.read_text(encoding="utf-8"))
    return {
        "passed": report["passed"], "total": report["total"],
        "wall_s": report["wall_s"], "serial_equivalent_s": report["serial_equivalent_s"],
        "results": [
            {"name": r.get("name"), "status": r.get("status"), "verified": r.get("verified"),
             "wall_s": r.get("wall_s"), "checks": r.get("checks"), "error": r.get("error")}
            for r in report["results"]
        ],
    }


HANDLERS = {"browser_do": tool_browser_do, "browser_assert": tool_browser_assert,
            "browser_checks": tool_browser_checks, "browser_state": tool_browser_state,
            "browser_act": tool_browser_act}


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


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
        try:
            result = handler(params.get("arguments") or {})
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                "isError": True,
            }}
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            "isError": False,
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
