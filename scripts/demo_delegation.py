"""Manual real-Chrome demo: one goal, same-session login, publish approval, readback.

Start `python scripts/trap_server.py --port 8791` first. This script calls the
configured TypeSafe Jev API, so it is intentionally outside the offline tests.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from AgentJevUltraFast import Agent  # noqa: E402
from AgentJevUltraFast.browser import Browser  # noqa: E402
from AgentJevUltraFast.delegation import DelegatedTask  # noqa: E402
from scripts.demo_mode import DEMO_GOAL, DEMO_SLUG, ensure_demo_mode  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8791/delegate")
    parser.add_argument("--project", default="AgentJevUltraFast demo")
    args = parser.parse_args()
    if not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("TYPESAFE_API_KEY is required; load your local .env before running")

    os.environ["JEV_TAB_MODE"] = "foreground"
    ensure_demo_mode()
    browser = Browser(args.url, follow_tabs=False)
    agent = None
    task = None
    try:
        agent = Agent(
            args.url, DEMO_GOAL,
            browser=browser, mode=DEMO_SLUG, values={"Project name": args.project},
            expect={"text_contains": ["Published demo site"]},
            evidence_dir=Path("artifacts") / "delegation-demo",
        )
        task = DelegatedTask(agent, browser)
        result = task.drive()
        while result["phase"] in {"paused", "needs_human"}:
            print(json.dumps(result, indent=2))
            if result["phase"] == "paused":
                result = task.resume(result["checkpoint"])
                continue
            if result["reason"] in {"login", "one_time_code", "captcha"}:
                input("Finish the human step in the same Chrome tab, then press Enter here: ")
                result = task.resume(result["checkpoint"])
                continue
            proposed = result.get("proposed_action") or {}
            answer = input(
                f"Approve {proposed.get('label')!r} on {result.get('url')}? Type yes: "
            ).strip().lower()
            if answer != "yes":
                result = task.cancel()
                break
            result = task.resume(result["checkpoint"], approve_action=True)
        print(json.dumps(result, indent=2))
        return 0 if result.get("outcome") == "verified" else 2
    finally:
        if task is None or not task.closed:
            if agent is not None:
                agent.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
