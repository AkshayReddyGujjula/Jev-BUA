"""Measure what one browser round trip costs, and what a screenshot costs, on this machine.

Hypothesis H3 (2026-09-23): each Browser.call goes through browser_harness's daemon over a
fresh IPC connection, so a trivial Runtime.evaluate costs ~30 ms. That bounds how cheap an
observation can be, and it decides whether merging calls or a persistent transport is worth
the work. Also measures Page.captureScreenshot, which the on-demand supervisor pays once per
intervention, and whether a concurrent screenshot slows an evaluate on the same tab.

No model calls. Opens one background tab on a local data: page and closes it.

    uv run python scripts/probe_cdp_latency.py [--n 40]
"""

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("BU_NAME", "jev-probe-cdp")
os.environ.setdefault("JEV_TAB_MODE", "background")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from AgentJevUltraFast.browser import Browser  # noqa: E402

HTML = "<!doctype html><title>probe</title>" + "".join(
    f"<p>Row {i} <button>Action {i}</button> <input aria-label='Field {i}'></p>" for i in range(60)
)


def timed(fn, n):
    samples = []
    for _ in range(n):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {"n": n, "median_ms": round(statistics.median(samples), 2),
            "p90_ms": round(sorted(samples)[int(n * 0.9) - 1], 2),
            "min_ms": round(min(samples), 2)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40)
    args = parser.parse_args()
    browser = Browser("data:text/html," + quote(HTML))
    try:
        time.sleep(0.5)
        results = {
            "evaluate_trivial": timed(lambda: browser.evaluate("1"), args.n),
            "observe_full": timed(lambda: browser.observe(screenshot=False), max(5, args.n // 4)),
            "screenshot_jpeg60": timed(lambda: browser.screenshot(quality=60), max(5, args.n // 4)),
        }
        # Contention: evaluate latency while another thread takes screenshots on the same tab.
        stop = threading.Event()

        def shoot():
            while not stop.is_set():
                try:
                    browser.screenshot(quality=60)
                except Exception:
                    pass

        shooter = threading.Thread(target=shoot, daemon=True)
        shooter.start()
        try:
            results["evaluate_during_screenshots"] = timed(lambda: browser.evaluate("1"), args.n)
        finally:
            stop.set()
            shooter.join(5)
        print(json.dumps(results, indent=2))
    finally:
        browser.close()


if __name__ == "__main__":
    main()
