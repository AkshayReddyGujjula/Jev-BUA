"""Live self-check for the delegated task interface (real Chrome + Jev API).

Start the fixture first:

    python scripts/trap_server.py --port 8791

This script plays the human with CDP: it fills the fixture's password field and
clicks Sign in at the login checkpoint, then approves the publish action. That
verifies the checkpoint mechanics end to end without a person at the keyboard --
it does not verify the human experience, and it calls the paid Jev API.

    uv run --env-file .env python scripts/verify_delegation.py [--keep-open]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FILL_LOGIN = """
(() => {
  const field = document.getElementById('password');
  if (!field) return 'no-password-field';
  field.value = 'verify-delegation';
  field.dispatchEvent(new Event('input', { bubbles: true }));
  document.getElementById('sign-in').click();
  return 'signed-in';
})()
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8791/delegate")
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the Chrome tab open after the run")
    args = parser.parse_args()
    if not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("TYPESAFE_API_KEY is required; load your local .env before running")

    from AgentJevUltraFast import Agent
    from AgentJevUltraFast.browser import Browser
    from AgentJevUltraFast.delegation import DelegatedTask
    from scripts.demo_mode import DEMO_GOAL, DEMO_SLUG, ensure_demo_mode

    os.environ["JEV_TAB_MODE"] = "foreground"
    ensure_demo_mode()
    browser = Browser(args.url, follow_tabs=False)
    agent = None
    task = None
    steps = []
    try:
        agent = Agent(
            args.url, DEMO_GOAL,
            browser=browser, mode=DEMO_SLUG,
            values={"Project name": "AgentJevUltraFast verification"},
            expect={"text_contains": ["Published demo site"]},
            evidence_dir=Path("artifacts") / "verify-delegation",
        )
        task = DelegatedTask(agent, browser, keep_open=args.keep_open)

        result = task.drive()
        steps.append({"phase": result["phase"], "reason": result.get("reason")})
        if (result["phase"], result.get("reason")) != ("needs_human", "login"):
            print(json.dumps({"steps": steps, "unexpected": result}, indent=2, default=str))
            return 2

        played = browser.call("Runtime.evaluate", expression=FILL_LOGIN, returnByValue=True)
        steps.append({"played_login": (played.get("result") or {}).get("value")})
        result = task.resume(result["checkpoint"])
        steps.append({"phase": result["phase"], "reason": result.get("reason")})
        if result["phase"] != "needs_human" or not result.get("proposed_action"):
            print(json.dumps({"steps": steps, "unexpected": result}, indent=2, default=str))
            return 2

        result = task.resume(result["checkpoint"], approve_action=True)
        steps.append({"phase": result["phase"], "outcome": result.get("outcome")})
        print(json.dumps({"steps": steps, "result": result}, indent=2, default=str))
        return 0 if result.get("outcome") == "verified" else 2
    finally:
        if task is None or not task.closed:
            if agent is not None:
                agent.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
