"""`jev`: your words go straight to Jev - no chat model in front of it.

    jev                          interactive Jev prompt; each line runs in your real Chrome
    jev "open youtube, search mkbhd and open his iPhone 17 review"
    jev --goal "..." --url URL [--tab foreground|background|active] [--expect JSON]

Without --url the start page comes from the goal itself (see start.py): a URL or domain it
names, else the site after "open / go to / visit". Nothing reads or rewrites the goal first.

Tab modes (env JEV_TAB_MODE, or --tab):
  foreground  new visible tab in the current Chrome window, brought to the front (default)
  background  new tab that never steals focus
  active      the tab the user already has open; it is navigated if --url is given and never closed
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from . import start
from .agent import Agent
from .assertions import normalize_expect

HOME = Path(os.environ.get("JEV_HOME") or Path.home() / ".jev-ultrafast")
REPO = Path(__file__).resolve().parent.parent
# A source checkout keeps run data in its ignored artifacts/; an installed `jev` in ~/.jev-ultrafast.
DATA = REPO / "artifacts" if (REPO / "pyproject.toml").is_file() else HOME

TAB_MODES = ("foreground", "background", "active")
HISTORY_FIELDS = ("step", "action", "kind", "text", "outcome", "page_changed", "confidence",
                  "recovery", "scan", "error")


def load_dotenv():
    """Load the repo .env when the caller did not.

    The skill runs this through `uv run --env-file .env`, but an agent (or a person) invoking
    `python run_task.py` plainly used to fail with a bare `KeyError: 'TYPESAFE_API_KEY'` and
    no hint about why. Reading the repo-root file makes the plain invocation work too, and
    an installed `jev` (no repo beside it) reads ~/.jev-ultrafast/.env. The first file that
    sets a key wins; anything already in the environment wins over all of them.
    """
    # Never the current directory's .env: `jev` runs from inside other projects.
    for path in (REPO / ".env", HOME / ".env"):
        if not path.is_file():
            continue
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
        "supervision": state.get("supervision") or [],
        "reviews": state.get("reviews") or [],
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


class Progress:
    """One line per executed step, printed as it happens (stderr, so stdout stays JSON)."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.seen = 0
        self.reviewed = 0
        self.started = time.monotonic()
        self.color = self.stream.isatty() and os.environ.get("NO_COLOR") is None

    def paint(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def __call__(self, state):
        history = state.get("history") or []
        for entry in history[self.seen:]:
            seconds = time.monotonic() - self.started
            label = " ".join(str(entry.get("action") or entry.get("kind") or "").split())[:60]
            if entry.get("text") and entry.get("kind") == "fill" and entry["text"] != "[redacted]":
                label += f' = "{str(entry["text"])[:40]}"'
            confidence = entry.get("confidence")
            kind = f"{entry.get('kind') or '':<6}"
            mark = self.paint("33", "skip  ") if entry.get("skipped") else self.paint("36", kind)
            conf = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "    "
            note = entry.get("recovery") or entry.get("error") or ""
            line = f"  {entry.get('step') or '':>3}  {mark} {label:<64} {conf}  {seconds:5.1f}s"
            if note:
                line += "  " + self.paint("2", str(note)[:80])
            print(line, file=self.stream, flush=True)
        self.seen = len(history)
        reviews = state.get("reviews") or []
        for review in reviews[self.reviewed:]:
            if review.get("on_track") is False:
                applied = "" if review.get("applied") else " (page moved on; not applied)"
                print("       " + self.paint("35", f"reviewer: {review.get('subgoal')}")
                      + self.paint("2", f"  [{review.get('latency_ms')} ms]{applied}"),
                      file=self.stream, flush=True)
        self.reviewed = len(reviews)

    def finish(self, result, error=None):
        status = result.get("status") if result else "error"
        outcome = result.get("outcome") if result else None
        ok = status == "done" and outcome != "refuted"
        head = self.paint("32", "done") if ok else self.paint("31", str(status))
        seconds = time.monotonic() - self.started
        actions = result.get("actions") if result else 0
        tail = f"  {error}" if error else ""
        print(f"  {head} ({outcome}) {seconds:.1f}s, {actions} actions{tail}", file=self.stream, flush=True)


def run_once(args, browser=None, on_state=None):
    trail = []
    started = time.monotonic()
    status, stall, outcome = None, None, None
    with Agent(args.url, args.goal, values=args.values, find=args.find, expect=args.expect,
               evidence_dir=args.evidence_dir, replay_dir=args.replay_dir,
               mode=args.mode or None, collect_steps=args.collect_steps,
               steps=args.plan, browser=browser) as agent:
        try:
            for state in agent.run():
                if on_state is not None:
                    on_state(state)
                trail.append(summarize(state))
                if len(trail) > args.max_steps:
                    trail.append(summarize(agent.stop_budget(f"{args.max_steps}-step caller budget")))
                    return trail, f"stopped after {args.max_steps} actions", 2
                if time.monotonic() - started > args.max_seconds:
                    trail.append(summarize(agent.stop_budget(f"{args.max_seconds}s caller budget")))
                    return trail, f"stopped after the {args.max_seconds}s budget", 2
        finally:
            if args.screenshot:
                _save_screenshot(agent, args.screenshot)
        status = agent.state.get("status")
        stall = agent.state.get("stall")
        outcome = agent.state.get("outcome")
    if status == "stalled":
        return trail, (
            f"stalled: {stall['reason']} ({stall['seconds_without_progress']}s browser-side, "
            f"{stall.get('wall_seconds_without_progress')}s wall, "
            f"{stall['repeated_rounds']} repeated rounds)"
        ), 2
    if status == "done" and outcome == "refuted":
        # The agent claimed completion and the readback disagreed. The readback wins.
        return trail, "done but the expected result was not on the page (refuted)", 2
    if status != "done":
        # Only "done" is the agent's success. Reporting 0 for a blocked or abandoned run
        # hid failures. The outcome says whether anything was actually verified.
        reason = agent.state.get("blocked_reason")
        return trail, f"ended as {status} ({outcome})" + (f": {reason}" if reason else ""), 2
    return trail, None, 0


def _save_screenshot(agent, path):
    """Write the final page to `path` as JPEG, unless a private value is on the page."""
    if getattr(agent, "_private_pixels", False):
        print("screenshot skipped: a private value is on the page", file=sys.stderr)
        return
    try:
        Path(path).write_bytes(base64.b64decode(agent.browser.screenshot(quality=70)))
    except Exception as error:
        print(f"screenshot unavailable: {type(error).__name__}", file=sys.stderr)


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


STARTER_SPEC = """\
# {title}
base_url: {url}
isolate: storage

## Home page loads
1. Read the page.
   - not blank
   - no page errors{title_check}

## Unknown page shows a not-found message
path: /this-page-does-not-exist
1. Read the page.
   - see any: not found | 404
"""


def test_command(argv):
    """`jev test SPEC`: run a Markdown or JSON test suite against a web app.

    Defaults are the ones an unattended run needs: a dedicated automation Chrome (no approval
    prompt, never the user's own tabs), and replay on, with plans kept next to the spec in
    `.jev/` so a team can commit them and every later run repeats verified flows for free.
    """
    from .checks import main as checks_main

    parser = argparse.ArgumentParser(prog="jev test", description=test_command.__doc__)
    parser.add_argument("spec", help="a Markdown spec (.md) or a JSON check file")
    parser.add_argument("--workers", type=int, default=4, help="parallel browsers (1-10)")
    parser.add_argument("--chrome", choices=("dedicated", "user"), default="dedicated")
    parser.add_argument("--no-replay", action="store_true", help="always decide with Jev; record nothing")
    parser.add_argument("--fresh", action="store_true", help="forget recorded plans first, then re-record")
    parser.add_argument("--skip-probe", action="store_true", help="do not check the app is up first")
    parser.add_argument("--write-suggestions", action="store_true",
                        help="after the run, write <spec>.suggested.md: the spec with each check-less step's "
                             "suggested checks added as comments to review")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run the suite N times and report each test's pass rate (flakiness) and "
                             "the cost of every run")
    args = parser.parse_args(argv)
    spec = Path(args.spec).resolve()
    if not spec.exists():
        parser.error(f"no such file: {spec}")
    state = spec.parent / ".jev"
    replay_dir = state / "replay" / spec.stem
    if args.fresh and replay_dir.exists():
        for plan in replay_dir.glob("*.json"):
            plan.unlink()
    forwarded = [str(spec), "--workers", str(args.workers), "--chrome", args.chrome,
                 "--state-dir", str(state), "--out", str(state / "reports" / f"{spec.stem}.json")]
    if not args.no_replay:
        forwarded += ["--replay", str(replay_dir)]
    if args.skip_probe:
        forwarded.append("--skip-probe")
    if args.repeat <= 1:
        code = checks_main(forwarded)
        if args.write_suggestions:
            write_suggestions(spec, state / "reports" / f"{spec.stem}.json")
        return code
    report_path = state / "reports" / f"{spec.stem}.json"
    runs, tests = [], {}
    for number in range(1, args.repeat + 1):
        print(f"\n--- run {number}/{args.repeat}")
        code = checks_main(forwarded)
        if code == 2 or not report_path.exists():
            return code
        report = json.loads(report_path.read_text(encoding="utf-8"))
        runs.append({"run": number, "passed": report.get("passed"), "total": report.get("total"),
                     "usd": report.get("usd_estimate"), "model_calls": report.get("model_calls"),
                     "wall_s": report.get("wall_s")})
        for result in report.get("results", []):
            entry = tests.setdefault(result.get("name"), {"passed": 0, "runs": 0})
            entry["runs"] += 1
            entry["passed"] += bool(result.get("verified"))
    flaky = {name: entry for name, entry in tests.items() if 0 < entry["passed"] < entry["runs"]}
    failing = {name: entry for name, entry in tests.items() if entry["passed"] == 0}
    summary = {"spec": str(spec), "runs": runs, "tests": tests,
               "flaky": sorted(flaky), "always_failing": sorted(failing)}
    (state / "reports" / f"{spec.stem}.repeat.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n{'run':>4} {'passed':>8} {'model calls':>12} {'usd':>10}")
    for run in runs:
        print(f"{run['run']:>4} {run['passed']:>4}/{run['total']:<3} {run['model_calls'] or 0:>12} "
              f"{run['usd'] or 0:>10.6f}")
    for name, entry in tests.items():
        mark = "FLAKY" if name in flaky else ("FAIL" if name in failing else "ok")
        print(f"  {mark:<6} {entry['passed']}/{entry['runs']}  {name}")
    return 0 if not flaky and not failing else 1


def write_suggestions(spec, report_path):
    """<spec>.suggested.md beside the spec, when the run suggested any checks. Never edits the spec."""
    from .spec import annotate

    if spec.suffix.lower() not in {".md", ".markdown"} or not report_path.exists():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    found = {}
    for result in report.get("results", []):
        for number, step in enumerate(result.get("steps") or [], 1):
            if step.get("suggested_checks"):
                found.setdefault(result.get("name"), {})[number] = step["suggested_checks"]
    if not found:
        print("no suggestions: every step that ran had checks, or changed nothing on screen")
        return None
    target = spec.with_name(spec.stem + ".suggested.md")
    target.write_text(annotate(spec.read_text(encoding="utf-8-sig"), found), encoding="utf-8")
    print(f"suggested checks for {sum(len(v) for v in found.values())} step(s): {target} (review, then copy)")
    return target


def page_title(url, timeout=5):
    """The start page's <title> from one plain GET (no browser, no model), or "" when unreachable."""
    import html
    import re
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status >= 400:
                return ""
            head = response.read(200_000).decode("utf-8", "replace")
    except Exception:
        return ""
    match = re.search(r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
    title = html.unescape(" ".join(match.group(1).split())) if match else ""
    return title[:80] if 3 <= len(title) else ""


def init_command(argv):
    """`jev init [--url URL] [--dir DIR]`: write a starter Markdown spec to edit."""
    parser = argparse.ArgumentParser(prog="jev init", description=init_command.__doc__)
    parser.add_argument("--url", default="http://localhost:3000", help="where the app runs locally")
    parser.add_argument("--dir", default="jev-tests", help="folder for specs (default jev-tests)")
    args = parser.parse_args(argv)
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "smoke.md"
    if target.exists():
        print(f"{target} already exists; leaving it alone.")
        return 1
    title = page_title(args.url)
    title_check = f"\n   - title contains: {title}" if title else ""
    target.write_text(STARTER_SPEC.format(title=Path.cwd().name + " smoke", url=args.url.rstrip("/"),
                                          title_check=title_check), encoding="utf-8")
    ignore = folder / ".jev" / ".gitignore"
    ignore.parent.mkdir(parents=True, exist_ok=True)
    # Plans are worth committing (free, deterministic reruns); raw evidence and reports are not.
    ignore.write_text("evidence/\nreports/\n", encoding="utf-8")
    print(f"wrote {target}\nrun it with:  jev test {target}")
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            # Windows consoles default to cp1252; one emoji in a page label would crash a print.
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) > 1 and sys.argv[1] == "draft":
        from .draft import main as draft_main

        sys.exit(draft_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "crawl":
        from .crawl import main as crawl_main

        sys.exit(crawl_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in {"test", "init"}:
        command = test_command if sys.argv[1] == "test" else init_command
        sys.exit(command(sys.argv[2:]))
    parser = argparse.ArgumentParser(
        prog="jev", description="Your words go straight to Jev, in your real Chrome. No arguments: interactive.")
    parser.add_argument("words", nargs="*", help="The goal, verbatim (same as --goal).")
    parser.add_argument("--goal", default="", help="One natural-language goal.")
    parser.add_argument("--url", default="",
                        help="Start URL. Optional: otherwise taken from the goal ('open youtube, ...').")
    parser.add_argument("--brief", action="store_true",
                        help="Print only the final result (no per-step trail) - cheaper for an agent to read.")
    parser.add_argument("--quiet", action="store_true", help="No live step lines on stderr.")
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
                             "selector_count ({selector, at_least}), value_contains "
                             "({selector, text}) and harvest_at_least (collected items; pair it "
                             "with a page check). Accepts @path/to/file.json. "
                             "Without it the run's outcome is unknown: a DONE is not proof.")
    parser.add_argument("--plan", default="",
                        help="Optional JSON list of steps from the orchestrator (or @file). Given "
                             "to the on-demand supervisor if the run gets stuck; Jev still "
                             "decides every step from --goal.")
    parser.add_argument("--screenshot", default="",
                        help="Write the final page to this path as JPEG (skipped when a private "
                             "value is on the page).")
    parser.add_argument("--evidence-dir", default=str(DATA / "evidence"),
                        help="Directory for this run's JSONL evidence: every offered candidate, "
                             "answer, confidence, answering model and execution.")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Do not write an evidence file for this run.")
    parser.add_argument("--replay", action="store_true",
                        help="Use a recorded step plan for this exact url+goal when one exists, "
                             "and record one when the run completes. Replay falls back to Jev on "
                             "the first step that does not match the live page.")
    parser.add_argument("--replay-dir", default=str(DATA / "replay"),
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
    if args.words and args.goal:
        parser.error("give the goal once: as words or as --goal")
    args.goal = (args.goal or " ".join(args.words)).strip()
    if not args.goal and not (args.remember or args.forget):
        if not sys.stdin.isatty():
            parser.error("no goal given (interactive mode needs a terminal)")
        load_dotenv()
        from .tui import interactive
        return interactive(tab=args.tab, mode=args.mode or None)

    args.values = _json_arg(parser, args.values, "--values")
    if not isinstance(args.values, dict):
        parser.error("--values must be a JSON object mapping label -> text")
    args.plan = _json_arg(parser, args.plan, "--plan") if args.plan else None
    if args.plan is not None and not isinstance(args.plan, list):
        parser.error("--plan must be a JSON list of step strings")
    args.expect = _json_arg(parser, args.expect, "--expect")
    if not isinstance(args.expect, dict):
        parser.error("--expect must be a JSON object of assertions")
    try:
        args.expect = normalize_expect(args.expect) or {}
    except ValueError as error:
        parser.error(f"--expect: {error}")
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
        from AgentJevUltraFast import modes

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
    progress = None if args.quiet else Progress()
    if not args.url and mode != "active":
        args.url, how = start.resolve(args.goal)
        if not args.url:
            parser.error("the goal names no site: say 'open <site>, ...', include a URL, or pass --url")
        if progress:
            print(f"  -> {args.url}  ({how})", file=sys.stderr, flush=True)

    try:
        trail, error, code = run_once(args, on_state=progress)
    except Exception as exc:
        # An exception after dispatch is ambiguous. Starting the whole goal again can
        # repeat a mutation; the caller must inspect the browser before a new run.
        if progress:
            progress.finish(None, f"{type(exc).__name__}: {exc}")
        return report(mode, [], f"{type(exc).__name__}: {exc}", 1)
    if progress:
        progress.finish(trail[-1] if trail else None, error)
    if args.brief:
        trail = trail[-1:]
    if error:
        return report(mode, trail, error, code)
    final = trail[-1] if trail else {"status": "blocked"}
    print(json.dumps({"tab_mode": mode, "result": final, "trail": trail}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
