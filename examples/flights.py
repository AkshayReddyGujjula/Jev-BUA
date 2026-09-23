"""Live Google Flights search. Calls TypeSafe; never selects or books a flight."""

import argparse
import base64
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from AgentJevUltraFast import Agent

URL = "https://www.google.com/travel/flights?hl=en"
# The date the published 7.073 s run used. It is now in the past, and Google's date
# picker will not offer it, so live re-runs must set JEV_FLIGHTS_DATE to a future date:
#   JEV_FLIGHTS_DATE=2026-10-20 uv run --env-file .env python examples/flights.py
DEFAULT_DATE = "2026-09-20"


def flight_date():
    try:
        return date.fromisoformat(os.environ.get("JEV_FLIGHTS_DATE", DEFAULT_DATE))
    except ValueError:
        return date.fromisoformat(DEFAULT_DATE)


def goal_for(day):
    return (
        f"Find one-way flights from Zurich to London on {day.strftime('%B')} {day.day}, "
        f"{day.year}, for one adult in economy. Stop when matching flight options are visible. "
        "Do not select or book a flight."
    )


GOALS = goal_for(flight_date())


def verify(page, day=None):
    """Independent checks on the resulting page, not the model's DONE answer."""
    day = day or flight_date()
    iso = day.isoformat()
    short = f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}"
    long_day = f"{day.strftime('%A, %B')} {day.day}"
    parsed = urlparse(page["url"])
    encoded = parse_qs(parsed.query).get("tfs", [""])[0]
    try:
        date_in_url = iso.encode() in base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        date_in_url = False
    actions = page["actions"]
    values = {a["label"].strip(): a.get("value") for a in actions}
    flights = [a["label"] for a in actions if "Select flight" in a["label"]]
    checks = {
        "search_page": parsed.hostname == "www.google.com" and parsed.path == "/travel/flights/search",
        "one_way": values.get("Change ticket type. One way") == "One way",
        "origin": values.get("Where from?") == "Zürich",
        "destination": values.get("Where to?") == "London",
        "date": values.get("Departure") == short,
        "year": date_in_url or f"departing {iso}" in page["text"],
        "results": bool(flights) and all(long_day in f for f in flights),
    }
    return {"passed": all(checks.values()), "checks": checks, "visible_flights": flights}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/flights/latest")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    agent = Agent(URL, GOALS)
    try:
        for state in agent.run():
            last = state["history"][-1] if state["history"] else {}
            print(state["elapsed_ms"], state["status"], last.get("action", ""), flush=True)
    finally:
        state = agent.snapshot()
        state["verification"] = verify(state["page"])
        (folder / "state.json").write_text(json.dumps(state, indent=2))
        (folder / "session.json").write_text(
            json.dumps({"target": agent.browser.target, "session": agent.browser.session})
        )
        if not args.keep_open:
            agent.close()
    print(json.dumps(state["verification"], indent=2))
    if not state["verification"]["passed"]:
        raise SystemExit("Final page did not satisfy the route/date checks")


if __name__ == "__main__":
    main()
