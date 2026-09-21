"""Run one Jev browser task against the user's real Chrome.

uv run --env-file .env python run_task.py --goal "..." [--url URL] [--tab foreground|background|active]

Tab modes (env JEV_TAB_MODE, or --tab):
  foreground  new visible tab in the current Chrome window, brought to the front (default)
  background  new tab that never steals focus
  active      the tab the user already has open; it is navigated if --url is given and never closed
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from jev_ultrafast import Agent

TAB_MODES = ("foreground", "background", "active")
HISTORY_FIELDS = ("step", "action", "kind", "text", "outcome", "page_changed", "confidence",
                  "recovery", "scan", "error")
SESSION_ERROR = "session with given id"


def load_dotenv():
    """Load the repo .env when the caller did not.

    The skill runs this through `uv run --env-file .env`, but an agent (or a person) invoking
    `python run_task.py` plainly used to fail with a bare `KeyError: 'TYPESAFE_API_KEY'` and
    no hint about why. Reading the repo-root file makes the plain invocation work too;
    anything already in the environment still wins.
    """
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def summarize(state):
    page = state.get("page") or {}
    history = state.get("history") or []
    return {
        "status": state.get("status"),
        # Code-owned verdict: verified / refuted / unknown / abstained / budget_exhausted.
        # Without an --expect readback the outcome is unknown even when status is done.
        "outcome": state.get("outcome"),
        "checks": state.get("checks"),
        "evidence": state.get("evidence"),
        "replay": state.get("replay"),
        "mode": state.get("mode"),
        "harvest": state.get("harvest"),
        "elapsed_ms": state.get("elapsed_ms"),
        "goal": state.get("goal"),
        "url": page.get("url"),
        "title": page.get("title"),
        # Skipped attempts (gate refusals) live in history for recovery but are not actions.
        "actions": sum(1 for h in history if not h.get("skipped")),
        "skipped": sum(1 for h in history if h.get("skipped")),
        "text_calls": state.get("text_calls") or [],
        "history": [
            {k: h.get(k) for k in HISTORY_FIELDS}
            for h in history
        ],
    }


def report(mode, trail, error, code):
    print(json.dumps(
        {"tab_mode": mode, "error": error, "result": trail[-1] if trail else None, "trail": trail},
        indent=2, default=str,
    ))
    return code


def run_once(args):
    trail = []
    started = time.monotonic()
    status, stall, outcome = None, None, None
    with Agent(args.url, args.goal, values=args.values, find=args.find, expect=args.expect,
               evidence_dir=args.evidence_dir, replay_dir=args.replay_dir,
               mode=args.mode or None, collect_steps=args.collect_steps) as agent:
        for state in agent.run():
            trail.append(summarize(state))
            if len(trail) > args.max_steps:
                return trail, f"stopped after {args.max_steps} actions", 2
            if time.monotonic() - started > args.max_seconds:
                return trail, f"stopped after the {args.max_seconds}s budget", 2
        status = agent.state.get("status")
        stall = agent.state.get("stall")
        outcome = agent.state.get("outcome")
    if status == "stalled":
        return trail, (
            f"stalled: no new page state for {stall['seconds_without_progress']}s "
            f"(limit {stall['limit_s']}s) after {stall['repeated_rounds']} repeated rounds"
        ), 2
    if status == "done" and outcome == "refuted":
        # The agent claimed completion and the readback disagreed. The readback wins.
        return trail, "done but the expected result was not on the page (refuted)", 2
    if status != "done":
        # Only "done" is the agent's success. Reporting 0 for a blocked or abandoned run
        # hid failures. The outcome says whether anything was actually verified.
        return trail, f"ended as {status} ({outcome})", 2
    return trail, None, 0


def restart_daemon():
    """Best effort: drop a daemon whose browser connection is wedged, so the retry gets a fresh one."""
    try:
        from browser_harness.admin import restart_daemon as restart

        restart()
    except Exception:
        pass


def _json_arg(parser, raw, flag):
    """A JSON argument, inline or via @path. Empty means an empty object."""
    if raw.startswith("@"):
        try:
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as error:
            parser.error(f"{flag} file unreadable: {error}")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        parser.error(f"{flag} must be a JSON object: {error}")


def main():
    parser = argparse.ArgumentParser(description="Drive the user's real Chrome with a Jev-powered browser agent.")
    parser.add_argument("--goal", required=True, help="One natural-language goal.")
    parser.add_argument("--url", default="", help="Start URL. Optional in active mode (uses the current page).")
    parser.add_argument("--tab", choices=TAB_MODES, default=None, help="Tab mode; overrides JEV_TAB_MODE.")
    parser.add_argument("--max-steps", type=int, default=500,
                        help="Cap on actions. A cap, not a target - the run ends when the goal is "
                             "met, so a generous value costs a short task nothing.")
    parser.add_argument("--max-seconds", type=int, default=900,
                        help="Wall-clock budget, the real bound for a long task.")
    parser.add_argument(
        "--values",
        default="{}",
        help='JSON object mapping a field label to the literal text to type, e.g. '
             '\'{"Search": "Python"}\'. Skips the text model entirely (1-3 s per field). '
             "Accepts @path/to/file.json as well as inline JSON.",
    )
    parser.add_argument("--find", default="",
                        help="Literal to scroll a long list to find (a video title, a row name). "
                             "Turns scanning into deterministic work: the code scrolls and observes "
                             "until the target appears, instead of one model call per screen. "
                             "Nothing is skipped - every screen is read.")
    parser.add_argument("--expect", default="",
                        help="Assertions read off the real page after the run: url_contains, "
                             "url_matches, title_contains, text_contains, text_absent, "
                             "selector_count ({selector, at_least}). Accepts @path/to/file.json. "
                             "Without it the run's outcome is unknown: a DONE is not proof.")
    parser.add_argument("--evidence-dir", default=str(Path("artifacts") / "evidence"),
                        help="Directory for this run's JSONL evidence: every offered candidate, "
                             "answer, confidence, answering model and execution.")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Do not write an evidence file for this run.")
    parser.add_argument("--replay", action="store_true",
                        help="Use a recorded step plan for this exact url+goal when one exists, "
                             "and record one when the run completes. Replay falls back to Jev on "
                             "the first step that does not match the live page.")
    parser.add_argument("--replay-dir", default=str(Path("artifacts") / "replay"),
                        help="Where replay plans live. Only used with --replay.")
    parser.add_argument("--keep-open", action="store_true",
                        help="Leave the tab open when the run finishes (foreground/background modes).")
    parser.add_argument("--mode", default="",
                        help="Task-class mode: a slug ('internship-applications'), 'auto' to "
                             "match or create one with the text model, or empty to use JEV_MODES. "
                             "A mode carries its own memory file, learned field values and "
                             "specialist settings (models, timeouts, budgets, collect strategy).")
    parser.add_argument("--modes-dir", default="",
                        help="Mode root. Defaults to JEV_MODES_DIR or ~/.jev-ultrafast/modes. "
                             "Keep personal mode memory outside the repository.")
    parser.add_argument("--collect-steps", type=int, default=None,
                        help="Override the scroll count for a collect-strategy mode.")
    parser.add_argument("--remember", default="",
                        help="Add a fact to a mode's memory and exit. Needs --mode <slug>.")
    parser.add_argument("--forget", default="",
                        help="Forget a learned field value by label and exit. Needs --mode <slug>.")
    args = parser.parse_args()

    args.values = _json_arg(parser, args.values, "--values")
    if not isinstance(args.values, dict):
        parser.error("--values must be a JSON object mapping label -> text")
    args.expect = _json_arg(parser, args.expect, "--expect")
    if not isinstance(args.expect, dict):
        parser.error("--expect must be a JSON object of assertions")
    if args.no_evidence:
        args.evidence_dir = None
    if not args.replay:
        args.replay_dir = None

    # The agent reads its own limit from here, so one flag controls both enforcement points
    # instead of the internal hard-coded one cutting the run short at 60.
    load_dotenv()
    os.environ["JEV_MAX_STEPS"] = str(args.max_steps)
    if args.modes_dir:
        os.environ["JEV_MODES_DIR"] = args.modes_dir
    if args.remember or args.forget:
        from jev_ultrafast import modes

        setting = (args.mode or os.environ.get("JEV_MODES") or "").strip()
        if not setting or setting.lower() == "auto":
            parser.error("--remember/--forget need an explicit --mode <slug> (not auto)")
        try:
            if args.remember:
                changed = modes.remember(setting, args.remember)
                print(json.dumps({"mode": setting, "remembered": bool(changed),
                                  "fact": args.remember}, indent=2))
            else:
                removed = modes.forget(setting, args.forget)
                print(json.dumps({"mode": setting, "forgot": removed, "label": args.forget},
                                 indent=2))
        except ValueError as error:
            parser.error(str(error))
        return 0
    if args.tab:
        os.environ["JEV_TAB_MODE"] = args.tab
    if args.keep_open:
        os.environ["JEV_KEEP_TAB"] = "1"
    mode = os.environ.get("JEV_TAB_MODE", "foreground").strip().lower()
    if not args.url and mode != "active":
        parser.error("--url is required unless --tab active")

    for attempt in (1, 2):
        try:
            trail, error, code = run_once(args)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if attempt == 1 and SESSION_ERROR in message.lower():
                restart_daemon()
                continue
            # Running out of budget is a budget stop, not a crash, and the exit code says
            # which one happened.
            return report(mode, [], message, 2 if "budget" in message.lower() else 1)
        if error:
            return report(mode, trail, error, code)
        final = trail[-1] if trail else {"status": "blocked"}
        print(json.dumps({"tab_mode": mode, "result": final, "trail": trail}, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
