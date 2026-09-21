# Jev Ultrafast

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The input is one natural-language goal. Do not add site-specific plans or hardcoded field values.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, node --check jev_ultrafast/snapshot.js, uv build.

## Browser testing from any agent

This repo is the browser engine other agents drive. Three ways in, pick by what you need:

- **MCP (opencode, Command Code, Codex):** the `jev-browser` server is registered for each, with its
  own `JEV_MCP_BU_NAME` so instances never share a daemon or tab. Tools: `browser_do` (one goal +
  assertions), `browser_state` / `browser_act` (read the indexed elements, then execute one observed
  action with no model call), `browser_assert` (check a page, no model call), `browser_checks` (many
  checks in parallel, one browser instance each). The tab is warm and navigated fresh per call
  (`fresh: true` forces a new one); every run can write JSONL evidence under `artifacts/evidence/`
  (`JEV_EVIDENCE_DIR=0` disables it). Start the server directly with
  `.venv\Scripts\python.exe jev_mcp_server.py` if an agent needs it explained.
- **CLI, one task:** `uv run --env-file .env python run_task.py --goal "<goal>" --url <url> --tab background [--values '<json>'] [--expect '<json>']`
- **CLI, parallel app testing:** write a check file and run
  `uv run --env-file .env python scripts/run_checks.py checks.json --workers 6`
  (up to 10). Assertions in `expect` are read off the real page and are the verdict — not the agent's
  opinion. `checks.example.json` is a working template.

Every run reports `status` (what the agent did) and `outcome` (the code-owned verdict:
`verified` / `refuted` / `unknown` / `abstained` / `budget_exhausted`). Only an `expect` readback
produces `verified`; a `done` without one is `unknown` and must not be reported as success.

Exit codes: `0` only for `done`; `2` for `blocked`/`stalled`/budget; `1` for an error. A non-zero exit
is not a crash — read the JSON.

**The loop can never spin for more than ~5 s.** Once a page stops yielding real progress (navigation,
controls, control values — not ambient text such as a clock or a carousel) the run terminates as
`stalled`. Do not retry a stalled run; the page did not respond to anything the agent tried.

Verification tooling: `python scripts/verify_stall.py` runs the loop against local trap pages and
asserts the deadline; `python scripts/run_checks.py --serve-traps 8803` self-tests the parallel harness
without needing a target app.

One browser per instance matters: give every agent or worker its own `BU_NAME`, or they share a daemon
and a tab and the failure mode is *silently wrong pages*.
