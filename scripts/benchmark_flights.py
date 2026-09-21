"""Alternating-arm flight benchmark, built on scripts/measure_flights.py.

Each run is a separate process against a frozen source tree, so a comparison never mixes
code revisions in one Python process. Arms alternate every pair, as the knowledge base's
measurement rules require, and the summary is computed from the runs' own state.json files
rather than from console output.

    python scripts/benchmark_flights.py --pairs 3 --candidate .
    python scripts/benchmark_flights.py --pairs 3 --baseline <frozen-dir> --candidate .

The baseline tree is a git worktree of the frozen commit, for example:

    git worktree add C:/tmp/jev-baseline 68c077bf79caca4e817b8e8a5854b2efa0c81ff6

Without --baseline this is a single-arm re-baseline of the current tree. Browser setup and
initial navigation are excluded by measure_flights.py in every arm; the clock is the run
itself. A run that fails independent verification is reported, not dropped.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def summarize(state):
    history = state.get("history") or []
    decisions = state.get("decisions") or []
    cdp = state.get("cdp") or {}
    return {
        "elapsed_ms": state.get("elapsed_ms"),
        "verified": bool((state.get("verification") or {}).get("passed")),
        "actions": len(history),
        "decisions": len(decisions),
        "cached_decisions": sum(1 for d in decisions if d.get("cached")),
        "outcome": state.get("outcome"),
        "cdp_calls": sum(v.get("count", 0) for v in cdp.values()),
        "text_calls": [
            {"model": c.get("model"), "latency_ms": c.get("latency_ms")}
            for c in (state.get("text_calls") or [])
        ],
        "error": state.get("error"),
    }


def run_arm(label, source, output, timeout):
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "measure_flights.py"),
         "--source", source, "--output", str(output)],
        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    wall = round(time.perf_counter() - started, 2)
    state_path = output / "state.json"
    if not state_path.exists():
        return {
            "arm": label, "source": source, "wall_s": wall, "verified": False,
            "error": (proc.stderr or proc.stdout or "no state.json")[-400:],
        }
    record = summarize(json.loads(state_path.read_text(encoding="utf-8")))
    record.update(arm=label, source=source, wall_s=wall, folder=str(output))
    return record


def aggregate(runs, label):
    arm = [r for r in runs if r["arm"] == label and r.get("elapsed_ms")]
    if not arm:
        return {"runs": 0}
    return {
        "runs": len(arm),
        "verified": sum(1 for r in arm if r.get("verified")),
        "median_elapsed_ms": round(statistics.median(r["elapsed_ms"] for r in arm)),
        "median_decisions": statistics.median(r.get("decisions", 0) for r in arm),
        "median_cdp_calls": statistics.median(r.get("cdp_calls", 0) for r in arm),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs", type=int, default=3, help="Alternating pairs to run.")
    parser.add_argument("--baseline", default=None,
                        help="Frozen source tree for the original arm (a git worktree).")
    parser.add_argument("--candidate", default=".", help="Source tree for the candidate arm.")
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "benchmark-flights.json"))
    parser.add_argument("--artifact-root", default=str(ROOT / "artifacts" / "benchmark-flights"),
                        help="Where per-run measurement folders are written.")
    parser.add_argument("--timeout", type=int, default=180, help="Per-run process timeout.")
    parser.add_argument("--pause", type=float, default=2.0, help="Seconds between runs.")
    args = parser.parse_args()

    load_env()
    # The measurement drives live model calls through the real loop; caching would replace
    # decisions and understate the request count. measure_flights enforces this for itself.
    os.environ.setdefault("JEV_CACHE", "0")

    arms = [("candidate", str(Path(args.candidate).resolve()))]
    if args.baseline:
        arms.insert(0, ("original", str(Path(args.baseline).resolve())))

    root = Path(args.artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    runs = []
    for pair in range(1, args.pairs + 1):
        for label, source in arms:
            folder = root / f"pair{pair}-{label}"
            if folder.exists():
                parser.error(f"{folder} already exists; move it aside first")
            print(f"[pair {pair}] {label}: {source}", flush=True)
            record = run_arm(label, source, folder, args.timeout)
            runs.append(record)
            elapsed = record.get("elapsed_ms")
            print(
                f"  -> {'VERIFIED' if record.get('verified') else 'NOT VERIFIED'} "
                f"{elapsed if elapsed is not None else 'n/a'} ms, "
                f"{record.get('decisions', '?')} decisions, "
                f"{record.get('cdp_calls', '?')} cdp calls",
                flush=True,
            )
            if args.pause:
                time.sleep(args.pause)

    summary = {label: aggregate(runs, label) for label, _ in arms}
    report = {
        "task": "Google Flights Zurich -> London, one-way, September 20, 2026",
        "pairs": args.pairs,
        "arms": {label: source for label, source in arms},
        "summary": summary,
        "runs": runs,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n== summary ==")
    for label, stats in summary.items():
        print(f"{label}: {stats.get('verified', 0)}/{stats.get('runs', 0)} verified, "
              f"median {stats.get('median_elapsed_ms', 'n/a')} ms, "
              f"median decisions {stats.get('median_decisions', 'n/a')}")
    if args.baseline and summary["original"].get("median_elapsed_ms") \
            and summary["candidate"].get("median_elapsed_ms"):
        delta = summary["original"]["median_elapsed_ms"] - summary["candidate"]["median_elapsed_ms"]
        pct = delta / summary["original"]["median_elapsed_ms"] * 100
        print(f"median change: {delta} ms ({pct:.1f}% {'faster' if delta > 0 else 'slower'})")
    print(f"report: {args.out}")
    return 0 if all(r.get("verified") for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
