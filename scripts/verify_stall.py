"""Prove the anti-loop deadline against real browser behaviour.

Runs the real agent loop against the local trap pages in scripts/trap_server.py and
asserts that a run which cannot make progress TERMINATES instead of spinning, within a
bounded multiple of JEV_STALL_SECONDS.

Also asserts the happy path still works and that caller-supplied values skip the text
model entirely.

Usage:
  python scripts/verify_stall.py                 # 3 runs per trap
  python scripts/verify_stall.py --runs 5
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Page text from real sites can contain characters the Windows console codec cannot
# encode; printing must never abort the verifier.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# The verifier measures loop behaviour under real decision latency; a cached answer would
# change the request path and the timing the deadline assertions are about.
os.environ.setdefault("JEV_CACHE", "0")
os.environ.setdefault("JEV_MODES", "off")

from jev_ultrafast import Agent  # noqa: E402
from jev_ultrafast.agent import STALL_LIMIT_S, TERMINAL_STATUS  # noqa: E402
from jev_ultrafast.browser import NAV_TIMEOUT  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from trap_server import serve  # noqa: E402

# The PRIMARY contract is that the loop stops once the page stops yielding new state.
# The stop is check-based: it lands at the first check after the deadline, so the observed
# window includes the tail of whatever single operation was in flight (a click plus its
# capped read). The allowance is one such operation, and the earlier 0.3s was tuned to a
# loop whose tails were shorter.
STALL_OVERSHOOT_S = 1.0

# Total wall is reported, and separately capped only to prove nothing hangs: the parts are
# browser setup (NAV_TIMEOUT), whatever time the run legitimately spent making progress,
# the deadline, and one in-flight iteration. On these traps the page stops changing
# immediately, so this cap is loose on purpose.
HARD_WALL_CAP_S = 20.0

TRAPS = [
    {
        "name": "dead control (repeat-forever)",
        "url": "/trap?mode=dead",
        "goal": "Click Confirm changes and reach the confirmation screen.",
        "expect": "never_done",
    },
    {
        "name": "A/B oscillation (two steps linking to each other)",
        "url": "/trap?mode=ab",
        "goal": "Complete both steps and finish the review.",
        "expect": "never_done",
    },
    {
        "name": "never finishes loading (hung subresource)",
        "url": "/trap?mode=loading",
        "goal": "Read the dashboard total and report it.",
        "expect": "never_done",
    },
]


def run_one(port, url, goal, values=None, max_seconds=45):
    started = time.perf_counter()
    os.environ["JEV_TAB_MODE"] = "background"
    os.environ.pop("JEV_KEEP_TAB", None)
    status, stall, decisions, text_calls, final_text, error, outcome = None, None, 0, [], "", None, None
    try:
        with Agent(f"http://127.0.0.1:{port}{url}", goal, values=values) as agent:
            for _ in agent.run():
                if time.perf_counter() - started > max_seconds:
                    break
            status = agent.state.get("status")
            outcome = agent.state.get("outcome")
            stall = agent.state.get("stall")
            decisions = len(agent.state["decisions"])
            text_calls = agent.state["text_calls"]
            try:
                final_text = (agent.browser.evaluate("document.body.innerText") or "")[:200]
            except Exception:
                pass
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = status or "error"
    return {
        "status": status,
        "outcome": outcome,
        "wall_s": round(time.perf_counter() - started, 2),
        "stall": stall,
        "decisions": decisions,
        "text_calls": text_calls,
        "final_text": final_text,
        "error": error,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()

    serve(args.port)
    limit = STALL_LIMIT_S
    print(
        f"stall limit {limit}s (+{STALL_OVERSHOOT_S}s allowance)   "
        f"wall cap {HARD_WALL_CAP_S}s (hang check only)   runs per trap {args.runs}\n"
    )

    failures = []
    report = {
        "stall_limit_s": limit, "overshoot_allowance_s": STALL_OVERSHOOT_S,
        "wall_cap_s": HARD_WALL_CAP_S, "nav_timeout_s": NAV_TIMEOUT,
        "traps": [], "happy_path": None,
    }

    for trap in TRAPS:
        walls, verdicts = [], []
        for i in range(args.runs):
            out = run_one(args.port, trap["url"], trap["goal"])
            walls.append(out["wall_s"])
            verdicts.append(out)
            window = out["stall"]["seconds_without_progress"] if out["stall"] else None
            stalled_in_time = window is None or window <= limit + STALL_OVERSHOOT_S
            terminated = out["status"] in TERMINAL_STATUS and out["status"] != "done"
            no_hang = out["wall_s"] <= HARD_WALL_CAP_S
            ok = terminated and stalled_in_time and no_hang
            shown = f"{window}s" if window is not None else f"n/a (ended {out['status']})"
            print(
                f"{trap['name'][:32]:<34} run{i+1}  {out['status']:<8} "
                f"stall_window={shown:<20} wall={out['wall_s']:>6.2f}s  "
                f"decisions={out['decisions']:<3} {'OK' if ok else 'FAIL'}"
            )
            if not ok:
                failures.append(
                    f"{trap['name']} run{i+1}: status={out['status']} window={window} "
                    f"wall={out['wall_s']}s error={out['error']}"
                )
        report["traps"].append({
            "name": trap["name"], "walls_s": walls,
            "median_s": round(statistics.median(walls), 2),
            "max_s": max(walls),
            "statuses": [v["status"] for v in verdicts],
            "outcomes": [v["outcome"] for v in verdicts],
            "stall_windows_s": [v["stall"]["seconds_without_progress"] if v["stall"] else None
                                for v in verdicts],
            "max_decisions": max(v["decisions"] for v in verdicts),
        })
        print(f"{'':<34} -> worst stall window "
              f"{max((v['stall']['seconds_without_progress'] for v in verdicts if v['stall']), default=0)}s"
              f"  worst wall {max(walls):.2f}s\n")

    # Happy path: a supplied value must skip the text model and reach the result page.
    print("happy path (working form + caller-supplied value)")
    happy = run_one(
        args.port, "/trap?mode=form",
        "Search the catalogue for laptops and open the results.",
        values={"Search the catalogue": "laptops"},
    )
    used_supplied = [c for c in happy["text_calls"] if c.get("model") == "supplied"]
    model_calls = [c for c in happy["text_calls"] if c.get("model") != "supplied"]
    happy_ok = (
        happy["status"] == "done"
        and "laptops" in (happy["final_text"] or "").lower()
        and used_supplied
        and not model_calls
    )
    print(f"  status={happy['status']}  {happy['wall_s']}s  text_calls={happy['text_calls']}")
    print(f"  final text: {happy['final_text'][:90]!r}")
    print(f"  {'OK' if happy_ok else 'FAIL'}\n")
    report["happy_path"] = {**happy, "ok": happy_ok}
    if not happy_ok:
        failures.append(f"happy path failed: status={happy['status']} error={happy['error']}")

    print("=" * 78)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
    else:
        print(
            f"ALL PASS - every trap terminated, no stall window exceeded "
            f"{limit + STALL_OVERSHOOT_S}s, nothing ran past {HARD_WALL_CAP_S}s."
        )
    out = ROOT / "artifacts" / "verify_stall.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"evidence: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
