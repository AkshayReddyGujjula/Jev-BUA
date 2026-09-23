"""Cold/warm measurement for the delegated task interface on a local fixture.

Each run is a real delegated task (`DelegatedTask`, the same code `browser_task`
drives) against `scripts/trap_server.py` (`/trap?mode=simple`, a deterministic
search with no form submit, so no approval checkpoint interrupts the run). The
only difference between the arms is the decision cache: the cold arm runs with
`JEV_CACHE=0`, the warm arm with the cache enabled after one warm-up run. Every
run must end `outcome=verified` or the measurement is not a pass.

Start the fixture first:

    python scripts/trap_server.py --port 8791

Then, with your own `.env` (this calls the paid Jev API):

    uv run --env-file .env python scripts/measure_delegation.py --pairs 3

Raw runs and medians are written to `artifacts/delegation-measurement/`.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GOAL = "Search the catalogue for laptops and stop when the results are visible."
VALUES = {"Search the catalogue": "laptops"}
EXPECT = {"url_contains": "simple-results", "text_contains": ["Results ready"]}


def run_once(url, out_dir, *, cache, arm):
    os.environ["JEV_CACHE"] = "1" if cache else "0"
    os.environ["JEV_MODES"] = "off"
    os.environ["JEV_TAB_MODE"] = "background"

    from AgentJevUltraFast import Agent
    from AgentJevUltraFast.browser import Browser
    from AgentJevUltraFast.delegation import DelegatedTask

    started = time.perf_counter()
    browser = Browser(url, follow_tabs=False)
    agent = None
    task = None
    try:
        agent = Agent(
            url, GOAL, browser=browser, mode="off", values=VALUES, expect=EXPECT,
            evidence_dir=Path(out_dir) / "evidence",
        )
        task = DelegatedTask(agent, browser)
        result = task.drive()
        guard = 0
        while result["phase"] in {"paused", "needs_human"} and guard < 20:
            guard += 1
            if result["phase"] == "needs_human":  # the fixture has no human steps
                checkpoint = {key: result.get(key) for key in ("phase", "reason", "proposed_action")}
                result = task.cancel()
                result["checkpoint"] = checkpoint
                break
            result = task.resume(result["checkpoint"])
        result["wall_s"] = round(time.perf_counter() - started, 3)
        result["arm"] = arm
        return result
    finally:
        if task is None or not task.closed:
            if agent is not None:
                agent.close()
            browser.close()


def summarize(arm, runs):
    def median(key):
        values = [run.get(key) for run in runs if isinstance(run.get(key), (int, float))]
        return round(statistics.median(values), 3) if values else None

    def usage(key, *, total=False):
        values = [((run.get("usage") or {}).get(key)) for run in runs]
        values = [value for value in values if isinstance(value, (int, float))]
        if not values:
            return None
        return round(sum(values), 8) if total else round(statistics.median(values), 3)

    return {
        "arm": arm,
        "runs": len(runs),
        "verified": sum(1 for run in runs if run.get("outcome") == "verified"),
        "median_wall_s": median("wall_s"),
        "median_active_s": median("active_seconds"),
        "median_decisions": median("decisions"),
        "median_cache_hits": usage("cache_hits"),
        "median_cache_misses": usage("cache_misses"),
        "jev_input_tokens": usage("jev_input_tokens", total=True),
        "jev_input_usd_estimate": usage("jev_input_usd_estimate", total=True),
        "text_input_tokens": usage("text_input_tokens", total=True),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8791/trap?mode=simple")
    parser.add_argument("--pairs", type=int, default=3,
                        help="cold/warm pairs; each pair runs one cold then one warm task")
    parser.add_argument("--out", default=str(Path("artifacts") / "delegation-measurement"))
    args = parser.parse_args()
    if not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("TYPESAFE_API_KEY is required; load your local .env before running")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("warm-up (not measured, populates the decision cache)...")
    warmup = run_once(args.url, out_dir, cache=True, arm="warmup")

    runs = {"cold": [], "warm": []}
    for index in range(max(1, args.pairs)):
        for arm, cache in (("cold", False), ("warm", True)):
            result = run_once(args.url, out_dir, cache=cache, arm=arm)
            runs[arm].append(result)
            print(f"{arm} run {index + 1}: phase={result['phase']} "
                  f"outcome={result.get('outcome')} wall={result['wall_s']}s "
                  f"decisions={result.get('decisions')}")

    summary = {arm: summarize(arm, items) for arm, items in runs.items()}
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "url": args.url,
        "goal": GOAL,
        "boundary": (
            "wall_s starts before Agent construction and includes browser attach and the "
            "first observation; active_seconds is the task's own active budget and excludes "
            "human waits. Verification runs after the timed task and is excluded."
        ),
        "arms": runs,
        "warmup": warmup,
        "summary": summary,
        "notes": [
            "One fixture, one goal, one browser profile; not a general benchmark.",
            "Only the decision cache differs between the arms; the field literal is supplied, "
            "so there are no text-model calls in either arm.",
            "The orchestrator's own browser tool was not part of this comparison.",
        ],
    }
    target = out_dir / "measurement.json"
    target.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    print("\narm   runs  verified  median wall s  median decisions  median cache hits  Jev tokens")
    for arm in ("cold", "warm"):
        row = summary[arm]
        print(f"{arm:<5} {row['runs']:<5} {row['verified']:<9} {row['median_wall_s']!s:<14} "
              f"{row['median_decisions']!s:<17} {row['median_cache_hits']!s:<18} "
              f"{row['jev_input_tokens']!s}")
    print(f"\nraw runs: {target}")
    return 0 if all(run.get("outcome") == "verified" for items in runs.values() for run in items) else 2


if __name__ == "__main__":
    raise SystemExit(main())
