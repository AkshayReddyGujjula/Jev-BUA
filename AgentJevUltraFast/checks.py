"""Run browser checks in parallel across isolated Jev browser instances.

Each check runs in its own process with its own BU_NAME, so it gets its own
browser-harness daemon and its own tab: N instances never share a daemon or steal each
other's tab. Results are judged by structured assertions read off the real page, never by
the agent's own "done" - the agent drives, the assertions decide.

  python scripts/run_checks.py checks.json --workers 6

Check file:

  {
    "base_url": "http://localhost:3000",
    "defaults": {"max_seconds": 60, "tab": "background"},
    "checks": [
      {
        "name": "login rejects a bad password",
        "path": "/login",
        "goal": "Sign in with the email and a deliberately wrong password.",
        "values": {"Email address": "ada@example.com", "Password": "wrong-on-purpose"},
        "expect": {
          "url_contains": "/login",
          "text_contains": ["incorrect", "invalid"],
          "text_absent": ["Dashboard"]
        }
      }
    ]
  }

`expect` keys: url_contains, url_matches (regex), title_contains, text_contains (any of; a
bare string is one phrase), text_absent (none of), selector_count ({selector, at_least >= 1}),
value_contains ({selector, text}), harvest_at_least (>= 1). Anything omitted is not checked;
an unknown key or a malformed or vacuous value rejects the whole spec before any browser starts.
Each check's verdict is the agent's own final readback, resolved like browser_do.

Exit code 0 only when every check is verified.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

MAX_WORKERS = 10
# Actions one test step may take before it ends as budget_exhausted. A step is one goal; a page
# that keeps offering new work (a live feed, a chat that keeps asking) must not turn one
# step into a minute of decisions (measured: 117 calls, $0.03, in a 60 s budget).
STEP_ACTIONS = int(os.environ.get("JEV_STEP_ACTIONS", "25"))
# Added to a check's own max_seconds to bound its whole process: browser start-up, the final
# readback and teardown. A worker past it is killed and reported, never waited on - one
# instance parked behind Chrome's "Allow remote debugging?" prompt used to hang the run.
STARTUP_GRACE_S = float(os.environ.get("JEV_CHECK_STARTUP_S", "45"))
APPROVAL_HINT = (
    "Chrome is waiting for 'Allow remote debugging?' for this instance's new connection; "
    "approve it once in Chrome (it is per BU_NAME) and re-run"
)


def load_env():
    # The project's own .env first, then this checkout's, then the per-user one; a key set
    # earlier (or in the real environment) is never overwritten by a later file.
    for path in (Path.cwd() / ".env", ROOT / ".env", Path.home() / ".jev-ultrafast" / ".env"):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Judged by the package's assertion module so the library, the CLI and the MCP server all
# apply exactly the same rules. Re-exported here because callers import it from run_checks.
from AgentJevUltraFast.assertions import evaluate_expectations, normalize_expect  # noqa: E402,F401


def local_host(url):
    """True for an app on a local development host, the disposable environment a test suite
    owns. There, a recorded commit-like click (Save, Delete, Submit) replays like any other
    step, as a scripted test would; anywhere else it gets a fresh gated Jev decision."""
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".localhost", ".test"))


def check_steps(job):
    """A check's steps: an explicit `steps` list, or the check itself as its only step."""
    if job.get("steps"):
        return job["steps"]
    return [{"goal": job["goal"], "values": job.get("values"), "expect": job.get("expect")}]


def add_cost(total, part):
    """Sum two cost summaries (cost.summarize shape); None stays None only if both are None."""
    if not total:
        return dict(part)
    out = dict(total)
    out["calls"] = {k: total["calls"].get(k, 0) + part["calls"].get(k, 0)
                    for k in set(total["calls"]) | set(part["calls"])}
    for key in ("jev_input_tokens", "jev_input_usd_estimate", "text_input_tokens"):
        a, b = total.get(key), part.get(key)
        out[key] = None if a is None or b is None else round(a + b, 8)
    for key in ("side_usd_reported", "usd_estimate", "cache_hits", "cache_misses"):
        out[key] = round((total.get(key) or 0) + (part.get(key) or 0), 8)
    return out


VISIBLE_TEXT = "document.body ? document.body.innerText : ''"
TRIVIAL_LINE = re.compile(r"^[\d\s:.,/%+-]*$")      # counters, clocks, dates: not a check


RUN_SLOTS = 6


def pid_alive(pid):
    """Whether a process exists. Never os.kill(pid, 0) on Windows: that terminates the process."""
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259                       # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim_slot(lock_dir=None):
    """(daemon-name prefix, lock path) that no other live check run on this machine holds.

    Worker daemons are named per index, so two runs at once (two coding agents testing in
    parallel) would otherwise share `jev-check-00`'s daemon and tab: silently wrong pages. Each
    run takes a slot; slot 0 keeps the historical names, so warm daemons are reused.
    """
    lock_dir = Path(lock_dir or os.environ.get("JEV_LOCK_DIR") or Path.home() / ".jev-ultrafast" / "locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    for slot in range(RUN_SLOTS):
        path = lock_dir / f"checks-{slot}.lock"
        for _ in range(2):
            try:
                handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    holder = int(path.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    holder = 0
                if holder and pid_alive(holder):
                    break                                     # taken: try the next slot
                if not holder and time.time() - path.stat().st_mtime < 5:
                    break                                     # just created; its pid is coming
                try:
                    path.unlink()                             # left by a run that died
                except OSError:
                    break
                continue
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            return ("jev-check" if slot == 0 else f"jev-check-s{slot}"), path
    raise SystemExit(f"! {RUN_SLOTS} check runs are already active on this machine; wait for one to finish")


def isolation(value):
    """A job's `isolate` as None, "storage" or "context". Specs write it many ways (true, none, off)."""
    if value is None or value is False or str(value).strip().lower() in {"", "none", "off", "no", "0", "false"}:
        return None
    return "storage" if str(value).strip().lower() == "storage" else "context"


SUGGEST_TEXT_CAP = 200_000


def suggest_checks(before, after, private=(), limit=5):
    """Checks a human can adopt for a step that had none, from what the step changed on screen.

    Zero model calls: the lines that appeared become `see:` candidates and the lines that went
    away `not see:` candidates, in page order. Field values never reach innerText, and any line
    holding a private value is dropped rather than redacted. They are suggestions only; a
    run without checks still reports `unknown`, never a pass.
    """
    def lines(text):
        # Dict keys: page order and constant-time membership (a feed can have 10k+ lines).
        seen = {}
        for line in (text or "")[:SUGGEST_TEXT_CAP].splitlines():
            line = " ".join(line.split())
            if 3 <= len(line) <= 60 and line not in seen and not TRIVIAL_LINE.match(line) \
                    and not any(value and value in line for value in private):
                seen[line] = None
        return seen

    old, new = lines(before), lines(after)
    appeared = [line for line in new if line not in old][:limit]
    gone = [line for line in old if line not in new][:max(1, limit // 2)]
    return [f"see: {line}" for line in appeared] + [f"not see: {line}" for line in gone]


def run_worker(job, out_path):
    """One check, in this process. Its BU_NAME was set by the parent before launch.

    A check is one or more steps run in order in ONE tab (and, with `isolate`, one fresh
    browser context), so a later step sees the state an earlier one left: sign in, then
    create, then delete. Each step is its own Agent run with its own goal and verdict; the
    check stops at the first step that fails, and passes only when every step with an
    expectation was verified.
    """
    load_env()
    from AgentJevUltraFast import Agent, Browser
    from AgentJevUltraFast.cost import summarize as cost_summary
    from AgentJevUltraFast.privacy import redact

    os.environ["JEV_TAB_MODE"] = job.get("tab", "background")
    os.environ.pop("JEV_KEEP_TAB", None)
    # In parallel runs one instance must never adopt another instance's tab. Forced, not
    # defaulted: an inherited JEV_FOLLOW_TABS=1 would re-enable adoption.
    os.environ["JEV_FOLLOW_TABS"] = "0"
    # A check run is a measurement: no mode auto-creation unless the check names one.
    os.environ["JEV_MODES"] = "off"
    os.environ["JEV_ISOLATE"] = isolation(job.get("isolate")) or "0"
    # Native dialogs freeze the page; a test answers them (see assertions.dialog_script).
    dialogs = job.get("dialogs")
    if dialogs is False or str(dialogs).lower() in {"off", "no", "none"}:
        dialogs = "off"             # the page's own dialogs stay native
    os.environ["JEV_DIALOGS"] = dialogs or ("accept" if local_host(job["url"]) else "dismiss")
    if job.get("viewport"):
        os.environ["JEV_VIEWPORT"] = str(job["viewport"])
    if job.get("review") is False:
        os.environ["JEV_REVIEW"] = "off"

    started = time.perf_counter()
    steps = check_steps(job)
    result = {
        "name": job["name"], "url": job["url"], "goal": steps[0]["goal"],
        "bu_name": os.environ.get("BU_NAME"), "pid": os.getpid(),
        "status": None, "outcome": None, "verified": None, "checks": [], "wall_s": None,
        "actions": 0, "decisions": 0, "final_url": None, "title": None,
        "text_calls": [], "trail": [], "error": None, "stall": None, "steps": [],
        "cost": None, "replay": None,
    }
    deadline = time.monotonic() + job.get("max_seconds", 60)
    browser = None
    try:
        browser = Browser(job["url"])
        for index, step in enumerate(steps):
            if step.get("path") and (index or step["path"] != job.get("path")):
                path = step["path"]
                browser.navigate(path if path.startswith("http") else job["base_url"] + path)
            url = job["url"] if index == 0 and not step.get("path") else browser.evaluate("location.href")
            values = {**(job.get("values") or {}), **(step.get("values") or {})} or None
            expect = step.get("expect")
            before_text = None
            if not expect:
                try:
                    before_text = browser.evaluate(VISIBLE_TEXT)
                except Exception:
                    before_text = None
            with Agent(url, step["goal"], values=values, expect=expect, browser=browser,
                       mode=job.get("mode"), collect_steps=job.get("collect_steps"),
                       replay_dir=job.get("replay_dir"),
                       evidence_dir=job.get("evidence_dir"),
                       commit_floor=job.get("commit_floor"),
                       replay_commits=job.get("replay_commits", local_host(job["url"])),
                       stop_on_expect=job.get("stop_on_expect", True),
                       check_wait=job.get("check_wait"),
                       max_steps=step.get("max_steps", job.get("max_steps", STEP_ACTIONS))) as agent:
                for _ in agent.run():
                    if time.monotonic() > deadline:
                        result["error"] = f"exceeded the {job.get('max_seconds', 60)}s budget"
                        # Finalizes the run, which performs the one independent readback.
                        agent.stop_budget(f"{job.get('max_seconds', 60)}s check budget")
                        break
                state = agent.snapshot()
            # The verdict is the agent's own final readback (one atomic read of the live
            # page, the same one written to evidence) resolved through outcomes.resolve, so a
            # check is judged exactly like browser_do and run_task.
            outcome = state.get("outcome")
            # With checks, anything short of a verified readback (refuted, unknown because the
            # readback failed, budget_exhausted) is a failed step, never a skipped one.
            verified = (outcome == "verified") if expect else None
            cost = cost_summary(state)
            replay_state = state.get("replay") or {}
            result["steps"].append({
                "goal": step["goal"], "status": state["status"], "outcome": outcome,
                "verified": verified, "checks": state.get("checks") or [],
                "decisions": len(state["decisions"]), "cost": cost,
                "replay": {k: replay_state.get(k) for k in
                           ("available", "used", "completed", "stopped", "heals",
                            "finished_by_readback", "recorded")},
            })
            if before_text is not None and state["status"] == "done":
                try:
                    suggested = suggest_checks(before_text, browser.evaluate(VISIBLE_TEXT),
                                               private=agent.private_values)
                except Exception:
                    suggested = []
                if suggested:
                    result["steps"][-1]["suggested_checks"] = suggested
            result["cost"] = add_cost(result["cost"], cost)
            result["outcome"] = outcome
            result["checks"] = state.get("checks") or []
            result["readback_error"] = state.get("readback_error")
            location = state.get("readback") or {}
            if not location:
                try:
                    location = browser.evaluate(
                        "({url: location.href, title: document.title})"
                    ) or {}
                except Exception:
                    location = {}
            result["status"] = state["status"]
            result["stall"] = state.get("stall")
            result["blocked_reason"] = state.get("blocked_reason")
            result["supervision"] = (result.get("supervision") or []) + (state.get("supervision") or [])
            result["actions"] += sum(1 for h in state["history"] if not h.get("skipped"))
            result["skipped"] = result.get("skipped", 0) + sum(1 for h in state["history"] if h.get("skipped"))
            result["decisions"] += len(state["decisions"])
            result["text_calls"] += state["text_calls"]
            result["final_url"] = location.get("url") or state["page"].get("url")
            result["title"] = location.get("title") or state["page"].get("title")
            result["trail"] = (result["trail"] + [
                {"action": h.get("action"), "outcome": h.get("outcome"), "error": h.get("error")}
                for h in state["history"]
            ])[-16:]
            if verified is False or (expect is None and state["status"] != "done") \
                    or result["error"]:
                result["failed_step"] = index + 1
                # What the page actually showed, so whoever reads the report (often a coding
                # agent) can tell an app bug from a wrong expectation without a re-run.
                try:
                    result["page_excerpt"] = redact(
                        browser.evaluate("document.body ? document.body.innerText.slice(0, 800) : ''"),
                        agent.private_values)
                except Exception:
                    pass
                break
        expected = [s for s in result["steps"] if s["verified"] is not None]
        # A step without checks that did not finish (stalled, blocked) fails the test wherever it
        # is: the steps after it never ran, and for the last step the test's flow never completed.
        result["verified"] = (
            all(s["verified"] for s in expected) and len(result["steps"]) == len(steps)
            and not result.get("failed_step")
        ) if expected else None
        result["replay"] = {
            "steps_replayed": sum((s["replay"].get("used") or 0) for s in result["steps"]),
            "heals": sum(len(s["replay"].get("heals") or []) for s in result["steps"]),
            "zero_model_steps": sum(1 for s in result["steps"] if s["decisions"] == 0),
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if "permission-blocked" in str(exc):
            # Not an app failure: the browser connection was never approved.
            result["status"] = "blocked"
            result["blocked_reason"] = "chrome-approval"
            result["error"] = f"{APPROVAL_HINT} ({exc})"
        elif "dedicated-chrome" in str(exc):
            result["status"] = "blocked"
            result["blocked_reason"] = "dedicated-chrome"
        else:
            frame = traceback.extract_tb(exc.__traceback__)[-1:]
            if frame:
                result["error_at"] = f"{Path(frame[0].filename).name}:{frame[0].lineno}"
        result["status"] = result["status"] or "error"
        result["verified"] = False if any(check_steps(job)[i].get("expect")
                                          for i in range(len(check_steps(job)))) else None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    result["wall_s"] = round(time.perf_counter() - started, 2)
    Path(out_path).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def run_job(job, part, bu_name):
    """Run one check in its own process under a hard bound; never wait on it forever.

    Returns a CompletedProcess. A worker past its bound is killed (its browser daemon is a
    separate process and is left alone: it holds Chrome's approval prompt, if any) and
    comes back with returncode None and the reason in stderr.
    """
    env = {**os.environ, "BU_NAME": bu_name, "PYTHONIOENCODING": "utf-8"}
    # The final readback may keep re-reading for check_wait after the step budget ends.
    bound = float(job.get("max_seconds", 60)) + STARTUP_GRACE_S + float(job.get("check_wait") or 0)
    command = [sys.executable, "-m", "AgentJevUltraFast.checks", "--worker", "--job", json.dumps(job),
               "--out", str(part)]
    try:
        return subprocess.run(command, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=bound)
    except subprocess.TimeoutExpired as expired:
        output = expired.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        reason = f"worker killed after {bound:.0f}s without a result"
        if "Allow remote debugging" in output or "handshake-wait" in output:
            reason += f": {APPROVAL_HINT}"
        return subprocess.CompletedProcess(command, None, "", reason)


def run_parent(args):
    prefix, lock = claim_slot()
    try:
        return _run_parent(args, prefix)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _run_parent(args, prefix="jev-check"):
    spec = load_suite(args.checks)
    base = (os.environ.get("JEV_TRAP_BASE") or spec.get("base_url", "")).rstrip("/")
    defaults = spec.get("defaults", {})
    checks = spec["checks"]
    workers = max(1, min(args.workers, MAX_WORKERS, len(checks)))

    state = Path(args.state_dir) if args.state_dir else ROOT / "artifacts"
    replay_dir = None
    if args.replay:
        replay_dir = str(Path(args.replay) if args.replay != "auto"
                         else state / "replay" / Path(args.checks).stem)
    evidence_dir = None if args.no_evidence else str(state / "evidence" / "checks")
    jobs = []
    before_all = spec.get("before_all")
    entries = ([before_all] if before_all else []) + list(checks)
    for index, check in enumerate(entries):
        job = {**defaults, **check}
        if defaults.get("values") or check.get("values"):
            # Suite-level values apply under a test's own, not instead of them.
            job["values"] = {**(defaults.get("values") or {}), **(check.get("values") or {})}
        path = job.get("path", "")
        if not path.startswith("http"):
            job["url"] = f"{base}{path}"
        else:
            job["url"] = path
        job["base_url"] = base
        job["replay_dir"] = replay_dir
        job["evidence_dir"] = evidence_dir
        job["name"] = check.get("name") or f"check {index + 1}"
        # Fail the whole spec before launching anything: a malformed expectation must
        # never surface later as a pass.
        try:
            if job.get("steps"):
                if not isinstance(job["steps"], list) or not all(
                        isinstance(s, dict) and isinstance(s.get("goal"), str) and s["goal"].strip()
                        for s in job["steps"]):
                    raise ValueError("steps must be a list of objects with a non-empty goal")
                job["steps"] = [{**s, "expect": normalize_expect(s.get("expect")) or None}
                                for s in job["steps"]]
                job["goal"] = job["steps"][0]["goal"]
            else:
                if not isinstance(job.get("goal"), str) or not job["goal"].strip():
                    raise ValueError("a check needs a goal (or steps)")
                job["expect"] = normalize_expect(job.get("expect")) or {}
        except ValueError as error:
            raise SystemExit(f"{job['name']}: {error}") from error
        jobs.append(job)
    setup_job = jobs.pop(0) if before_all else None
    if setup_job:
        setup_job["name"] = "Before all"
        for job in jobs:
            # Tests inherit the state Before all left (signed in, onboarded): wiping the
            # origin again would undo it. Isolation by context cannot share it at all.
            mode = isolation(job.get("isolate"))
            if mode == "context":
                raise SystemExit(f"{job['name']}: '## Before all' needs isolate: storage or none")
            job["isolate"] = None
            if mode == "storage":
                job["sequential"] = True
    from AgentJevUltraFast.agent import POSITIVE_CHECKS
    for job in jobs:
        for number, step in enumerate(check_steps(job), 1):
            expect = step.get("expect") or {}
            if expect and not any(key in POSITIVE_CHECKS for key in expect):
                # Not an error, but the costly case: only "not see" checks cannot tell the run
                # it has arrived, so it runs until Jev chooses DONE or the step budget ends it.
                print(f"note: {job['name']} step {number} has only negative checks; add a "
                      "'see:' check so it can stop the moment it succeeds")
    if workers > 1 and any(job.get("isolate") == "storage" or job.get("sequential") for job in jobs):
        # Storage isolation wipes the app origin's data in the shared browser context, which
        # would pull state out from under any check running at the same time.
        print("isolate: storage runs one check at a time (use isolate: context to parallelise)")
        workers = 1
    from AgentJevUltraFast import chrome
    if chrome.dedicated():
        # Once, before any worker: ten workers racing to launch the same profile is how a
        # second Chrome ends up without the debugging port.
        try:
            chrome.ensure_chrome()
        except RuntimeError as error:
            raise SystemExit(f"! {error}") from error

    outdir = Path(args.out).resolve().parent / (Path(args.out).stem + "_parts")
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} checks, {workers} parallel instances, base {base or '(absolute urls)'}")
    print(f"parts -> {outdir}\n")

    def launch(index_job):
        index, job = index_job
        part = outdir / f"{index:02d}-{re.sub(r'[^a-z0-9]+', '-', job['name'].lower())[:40]}.json"
        part.unlink(missing_ok=True)      # a stale part from an earlier run is not a result
        started = time.perf_counter()
        proc = run_job(job, part, f"{prefix}-{index:02d}")
        return index, job, part, proc, round(time.perf_counter() - started, 2)

    setup_result = None
    if setup_job:
        # Once per suite run, alone, before any test (Playwright's global setup).
        _, _, part, proc, wall = launch((99, setup_job))
        setup_result = (json.loads(part.read_text(encoding="utf-8")) if part.exists() else
                        {"name": "Before all", "status": "error", "verified": False, "wall_s": wall,
                         "error": (proc.stderr or proc.stdout or "no result file")[-400:]})
        print(f"Before all: {'verified' if setup_result.get('verified') else 'FAILED'} "
              f"({setup_result.get('wall_s')}s)")
    if setup_result is not None and not setup_result.get("verified"):
        # Every test depends on it: report them as blocked instead of running them blind.
        launched = [(index, job, outdir / "__none__", subprocess.CompletedProcess([], 1, "",
                     "Before all did not verify; test not run"), 0.0) for index, job in enumerate(jobs)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            launched = list(pool.map(launch, enumerate(jobs)))

    results = []
    for index, job, part, proc, wall in sorted(launched, key=lambda r: r[0]):
        if part.exists():
            results.append(json.loads(part.read_text(encoding="utf-8")))
        else:
            results.append({
                "name": job["name"], "url": job["url"], "status": "error",
                "verified": False, "error": (proc.stderr or proc.stdout or "no result file")[-400:],
                "checks": [], "wall_s": wall,
                **({"blocked_reason": "timeout"} if proc.returncode is None else {}),
            })

    print(f"{'check':<44} {'agent':<9} {'assert':<7} {'wall':>7} {'calls':>5} {'usd':>9}  detail")
    print("-" * 118)
    for r in results:
        verdict = "PASS" if r.get("verified") else ("FAIL" if r.get("verified") is False else "n/a")
        notes = "; ".join(
            f"{c['check']}={'ok' if c['pass'] else 'NO'}" for c in (r.get("checks") or [])
        ) or (r.get("error") or "")[:40]
        cost = r.get("cost") or {}
        calls = sum((cost.get("calls") or {}).get(k, 0) for k in ("jev", "text", "review", "supervisor"))
        print(f"{(r.get('name') or '')[:42]:<44} {str(r.get('status')):<9} {verdict:<7} "
              f"{r.get('wall_s', 0):>6}s {calls:>5} {cost.get('usd_estimate') or 0:>9.6f}  {notes[:40]}")

    passed = [r for r in results if r.get("verified")]
    failed = [r for r in results if not r.get("verified")]
    stalls = [r for r in results if r.get("status") == "stalled"]
    total = sum(r.get("wall_s", 0) for r in results)
    slowest = max((r.get("wall_s", 0) for r in results), default=0)

    print("\n" + "=" * 118)
    print(f"{len(passed)}/{len(results)} verified   "
          f"{len(stalls)} stalled   "
          f"serial-equivalent {total:.1f}s   wall {max((w for _, _, _, _, w in launched), default=0):.1f}s   "
          f"slowest check {slowest:.1f}s")
    for r in failed:
        step = f" (step {r['failed_step']})" if r.get("failed_step") else ""
        reason = r.get("error") or ("no checks, so nothing was verified" if r.get("verified") is None
                                    else f"step {r.get('failed_step')} did not finish ({r.get('status')})"
                                    if not (r.get("checks") or []) or all(c.get("pass") for c in r["checks"])
                                    else "assertions not satisfied")
        print(f"  FAILED {r.get('name')}{step}: {reason}")
    for r in results:
        for number, step in enumerate(r.get("steps") or [], 1):
            if step.get("suggested_checks"):
                # A step without checks proves nothing; these came from what it changed on screen.
                print(f"  no checks: {r.get('name')} step {number} - suggested (review first): "
                      + " | ".join(step["suggested_checks"]))
    # Before all is part of what the run cost: signing in is often the most expensive step.
    billed = results + ([setup_result] if setup_result else [])
    usd = round(sum((r.get("cost") or {}).get("usd_estimate") or 0 for r in billed), 6)
    model_calls = sum(sum(((r.get("cost") or {}).get("calls") or {}).get(k, 0)
                          for k in ("jev", "text", "review", "supervisor")) for r in billed)
    replayed = sum((r.get("replay") or {}).get("steps_replayed", 0) for r in billed)
    heals = sum((r.get("replay") or {}).get("heals", 0) for r in billed)
    print(f"cost ~${usd:.6f} for {model_calls} model calls"
          + (f"; {replayed} steps replayed with no model call, {heals} healed" if replay_dir else ""))

    report = {
        "base_url": base, "workers": workers,
        "wall_s": max((w for _, _, _, _, w in launched), default=0),
        "serial_equivalent_s": round(total, 2),
        "passed": len(passed), "total": len(results), "stalled": len(stalls),
        "usd_estimate": usd, "model_calls": model_calls,
        "replay_dir": replay_dir, "steps_replayed": replayed, "heals": heals,
        "before_all": setup_result,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"report: {args.out}")
    return 0 if not failed else 1


def load_suite(path):
    """A check file as a dict: JSON as written, or a Markdown spec compiled by spec.py."""
    if str(path).lower().endswith((".md", ".markdown")):
        from .spec import SpecError, load

        try:
            return load(path)
        except SpecError as error:
            raise SystemExit(f"! {error}") from error
    return json.loads(Path(path).read_text(encoding="utf-8"))


def probe_target(base_url, timeout=5):
    """Fail fast and loudly when the app under test is not actually up.

    Without this, a dev server that is not running turns every check into a
    chrome-error:// page. The agent correctly reports 'blocked' for that, so the run
    silently looks like an app failure instead of a setup failure.
    """
    if not base_url:
        return True
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(base_url, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500          # a 401/404 still proves something is listening
    except Exception as error:
        print(f"! {base_url} is not reachable: {type(error).__name__}: {error}")
        print("! Checks would all land on a browser error page. Start the app first.")
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("checks", nargs="?", help="check file: JSON, or a Markdown spec (.md)")
    parser.add_argument("--workers", type=int, default=4,
                        help=f"parallel browser instances (1-{MAX_WORKERS}, default 4)")
    parser.add_argument("--out", default="artifacts/checks-report.json")
    parser.add_argument("--skip-probe", action="store_true",
                        help="do not pre-flight the base_url")
    parser.add_argument("--serve-traps", type=int, metavar="PORT", default=None,
                        help="start the local trap pages in-process and point checks at them "
                             "(self-test for this harness: proves isolatation and reporting "
                             "without needing a target app)")
    parser.add_argument("--chrome", choices=("user", "dedicated"), default=None,
                        help="dedicated: a separate automation Chrome (no approval prompt, "
                             "never your own tabs); default from JEV_CHROME, else user")
    parser.add_argument("--replay", nargs="?", const="auto", default=None, metavar="DIR",
                        help="record verified runs and replay them with no model call "
                             "(default dir artifacts/replay/<check file name>)")
    parser.add_argument("--no-evidence", action="store_true",
                        help="do not write JSONL evidence under artifacts/evidence/checks")
    parser.add_argument("--state-dir", default=None,
                        help="where replay plans and evidence live (default artifacts/ in this checkout)")
    parser.add_argument("--worker", action="store_true", help="internal: run one job in this process")
    parser.add_argument("--job", help="internal: the job as JSON")
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--worker" in argv:
        args = parser.parse_args(argv)
        run_worker(json.loads(args.job), args.out)
        return 0
    args = parser.parse_args(argv)
    if args.chrome == "user":
        from . import chrome

        chrome.use_user_chrome()
    elif args.chrome:
        os.environ["JEV_CHROME"] = args.chrome
    if args.serve_traps:
        sys.path.insert(0, str(ROOT / "scripts"))
        from trap_server import serve as serve_traps

        serve_traps(args.serve_traps)
        print(f"trap pages served in-process on http://127.0.0.1:{args.serve_traps}")
        if not args.checks:
            args.checks = str(ROOT / "checks.example.json")
        os.environ["JEV_TRAP_BASE"] = f"http://127.0.0.1:{args.serve_traps}"
    if not args.checks:
        parser.error("a check file is required (or pass --worker)")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    if not args.skip_probe and not args.serve_traps:
        spec = load_suite(args.checks)
        if not probe_target((os.environ.get("JEV_TRAP_BASE") or spec.get("base_url") or "").rstrip("/")):
            return 2
    return run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
