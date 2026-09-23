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

`expect` keys: url_contains, url_matches (regex), title_contains, text_contains (any of),
text_absent (none of), selector_count ({selector, at_least}). Anything omitted is not checked.

Exit code 0 only when every check is verified.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAX_WORKERS = 10


def load_env():
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Judged by the package's assertion module so the library, the CLI and the MCP server all
# apply exactly the same rules. Re-exported here because callers import it from run_checks.
from AgentJevUltraFast.assertions import evaluate_expectations  # noqa: E402


def run_worker(job, out_path):
    """One check, in this process. Its BU_NAME was set by the parent before launch."""
    load_env()
    from AgentJevUltraFast import Agent

    os.environ["JEV_TAB_MODE"] = job.get("tab", "background")
    os.environ.pop("JEV_KEEP_TAB", None)
    # In parallel runs one instance must never adopt another instance's tab.
    os.environ.setdefault("JEV_FOLLOW_TABS", "0")
    # A check run is a measurement: no mode auto-creation unless the check names one.
    os.environ["JEV_MODES"] = "off"

    started = time.perf_counter()
    result = {
        "name": job["name"], "url": job["url"], "goal": job["goal"],
        "bu_name": os.environ.get("BU_NAME"), "pid": os.getpid(),
        "status": None, "verified": None, "checks": [], "wall_s": None,
        "actions": 0, "decisions": 0, "final_url": None, "title": None,
        "text_calls": [], "trail": [], "error": None, "stall": None,
    }
    try:
        with Agent(job["url"], job["goal"], values=job.get("values"),
                   mode=job.get("mode"), collect_steps=job.get("collect_steps")) as agent:
            deadline = time.monotonic() + job.get("max_seconds", 60)
            for _ in agent.run():
                if time.monotonic() > deadline:
                    result["error"] = f"exceeded the {job.get('max_seconds', 60)}s budget"
                    break
            state = agent.snapshot()
            page = state["page"]
            selectors = {}
            values = {}
            expect = job.get("expect") or {}
            for rule in ([expect["selector_count"]] if "selector_count" in expect else []):
                try:
                    selectors[rule["selector"]] = agent.browser.evaluate(
                        f"document.querySelectorAll({json.dumps(rule['selector'])}).length"
                    ) or 0
                except Exception:
                    selectors[rule["selector"]] = 0
            for rule in ([expect["value_contains"]] if "value_contains" in expect else []):
                try:
                    values[rule["selector"]] = agent.browser.evaluate(
                        f"(document.querySelector({json.dumps(rule['selector'])}) || {{}}).value ?? ''"
                    ) or ""
                except Exception:
                    values[rule["selector"]] = ""
            page_state = {
                "url": page.get("url"), "title": page.get("title"),
                "text": page.get("text"), "selector_counts": selectors, "values": values,
                "harvest": state.get("harvest") or {"items": 0},
            }
            # Independent of the agent's opinion: read the page again and assert on it.
            try:
                page_state["text"] = agent.browser.evaluate("document.body.innerText") or page.get("text")
            except Exception:
                pass
            result["status"] = state["status"]
            result["stall"] = state.get("stall")
            result["actions"] = sum(1 for h in state["history"] if not h.get("skipped"))
            result["skipped"] = sum(1 for h in state["history"] if h.get("skipped"))
            result["decisions"] = len(state["decisions"])
            result["text_calls"] = state["text_calls"]
            result["final_url"] = page_state["url"]
            result["title"] = page_state["title"]
            verified, detail = evaluate_expectations(job.get("expect"), page_state)
            result["verified"] = verified
            result["checks"] = detail
            result["trail"] = [
                {"action": h.get("action"), "outcome": h.get("outcome"), "error": h.get("error")}
                for h in state["history"][-12:]
            ]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = result["status"] or "error"
    result["wall_s"] = round(time.perf_counter() - started, 2)
    Path(out_path).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def run_parent(args):
    spec = json.loads(Path(args.checks).read_text(encoding="utf-8"))
    base = (os.environ.get("JEV_TRAP_BASE") or spec.get("base_url", "")).rstrip("/")
    defaults = spec.get("defaults", {})
    checks = spec["checks"]
    workers = max(1, min(args.workers, MAX_WORKERS, len(checks)))

    jobs = []
    for index, check in enumerate(checks):
        job = {**defaults, **check}
        path = job.get("path", "")
        if not path.startswith("http"):
            job["url"] = f"{base}{path}"
        else:
            job["url"] = path
        job["name"] = check.get("name") or f"check {index + 1}"
        jobs.append(job)

    outdir = Path(args.out).resolve().parent / (Path(args.out).stem + "_parts")
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} checks, {workers} parallel instances, base {base or '(absolute urls)'}")
    print(f"parts -> {outdir}\n")

    def launch(index_job):
        index, job = index_job
        part = outdir / f"{index:02d}-{re.sub(r'[^a-z0-9]+', '-', job['name'].lower())[:40]}.json"
        env = {
            **os.environ,
            "BU_NAME": f"jev-check-{index:02d}",
            "PYTHONIOENCODING": "utf-8",
        }
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(Path(__file__)), "--worker", "--job", json.dumps(job),
             "--out", str(part)],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return index, job, part, proc, round(time.perf_counter() - started, 2)

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
            })

    print(f"{'check':<44} {'agent':<9} {'assert':<7} {'wall':>7}  detail")
    print("-" * 118)
    for r in results:
        verdict = "PASS" if r.get("verified") else ("FAIL" if r.get("verified") is False else "n/a")
        notes = "; ".join(
            f"{c['check']}={'ok' if c['pass'] else 'NO'}" for c in (r.get("checks") or [])
        ) or (r.get("error") or "")[:40]
        print(f"{(r.get('name') or '')[:42]:<44} {str(r.get('status')):<9} {verdict:<7} "
              f"{r.get('wall_s', 0):>6}s  {notes[:52]}")

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
        print(f"  FAILED {r.get('name')}: {r.get('error') or 'assertions not satisfied'}")

    report = {
        "base_url": base, "workers": workers,
        "wall_s": max((w for _, _, _, _, w in launched), default=0),
        "serial_equivalent_s": round(total, 2),
        "passed": len(passed), "total": len(results), "stalled": len(stalls),
        "results": results,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"report: {args.out}")
    return 0 if not failed else 1


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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("checks", nargs="?", help="JSON check file")
    parser.add_argument("--workers", type=int, default=4,
                        help=f"parallel browser instances (1-{MAX_WORKERS}, default 4)")
    parser.add_argument("--out", default="artifacts/checks-report.json")
    parser.add_argument("--skip-probe", action="store_true",
                        help="do not pre-flight the base_url")
    parser.add_argument("--serve-traps", type=int, metavar="PORT", default=None,
                        help="start the local trap pages in-process and point checks at them "
                             "(self-test for this harness: proves isolatation and reporting "
                             "without needing a target app)")
    parser.add_argument("--worker", action="store_true", help="internal: run one job in this process")
    parser.add_argument("--job", help="internal: the job as JSON")
    if "--worker" in sys.argv:
        args = parser.parse_args()
        run_worker(json.loads(args.job), args.out)
        return 0
    args = parser.parse_args()
    if args.serve_traps:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
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
        spec = json.loads(Path(args.checks).read_text(encoding="utf-8"))
        if not probe_target((spec.get("base_url") or "").rstrip("/")):
            return 2
    return run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
