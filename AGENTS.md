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
- **CLI, one task:** `uv run --env-file .env python run_task.py --goal "<goal>" --url <url> --tab background [--values '<json>'] [--expect '<json>'] [--mode auto|<slug>]`
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

## Modes: lessons for future agents

- Modes are task-class folders (`JEV_MODES_DIR`, default `~/.jev-ultrafast/modes`): `mode.json`,
  human-editable `AGENTS.md` memory, `learned/` shards, `harvest/`, `replay/`. Auto-creation is
  opt-in (`--mode auto`, `JEV_MODES=auto`, MCP `mode: "auto"`) and only for a durable task class;
  benchmark scripts force `JEV_MODES=off`.
- Never put goal text into a mode's `AGENTS.md`; `mode.json` keeps `created_from` only. Memory is
  sent to model providers — never store credentials, and keep the sensitive-label denylist in
  `jev_ultrafast/modes.py` ahead of anything new you learn.
- Learned values are typed only on an exact normalized label match, only when a fresh readback
  showed the field accepted the value, and never for sensitive labels, seek controls or replays.
- The confidence gate's `irreversible` reading is page-level, so floors apply to the chosen
  action's own risk class: only clicks can be committal; checkbox/radio/switch/link clicks and
  fills/selects/sliders are reversible. A fill backed by exact memory skips the gate — it is user
  data, not a model guess.
- The collector is scroll-only by construction. Harvest once per visited page state, never on
  ambient text churn; wait for unrendered feeds; fall back to scrolling the element under the
  viewport centre for inner scrollers. Keep `harvest_at_least` paired with a page check.
- Off-loop model calls need their own timeout ceiling (`JEV_CLASSIFY_TIMEOUT`); the in-loop helper
  budget (4 s) silently disabled mode creation on long goals.
- Live scenarios: `uv run --env-file .env python scripts/modes_live.py --case internship|feed|x`.
  Fixtures `/apply` and `/feed` live in `scripts/trap_server.py`; evidence in the gitignored
  `artifacts/modes-live/`; the committed summary is `docs/modes-scenarios.json`.
- `opencode-instructions.md` is an external reviewer's channel: never edit or commit it (it is
  gitignored), and address findings in code and docs.
