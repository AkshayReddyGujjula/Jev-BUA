# Jev-BUA

This is Akshay Reddy Gujjula's independent personal project at
https://github.com/AkshayReddyGujjula/Jev-BUA. `origin` is the only project remote.
Keep the existing `AgentJevUltraFast` Python package and `jev` commands compatible.
Preserve dependency disclosures, historical evidence, and original license notices.

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The input is one natural-language goal. Do not add site-specific plans or hardcoded field values.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT values come from the text LLM. One batch call drafts every non-sensitive, non-supplied fill field
  while Jev decides (`JEV_TEXT_PREFETCH=0` disables it); a draft is used only under the identical goal and only
  for a field that draft saw. Otherwise one single-field call; cache a stale retry's value only while its entire
  helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- Screenshots are optional; Jev never consumes them. Only the on-demand supervisor may receive one, at
  trigger time, and never when a private value is on the page. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check AgentJevUltraFast/static/app.js, node --check AgentJevUltraFast/snapshot.js, uv build.

## Browser testing from any agent

This repo is the browser engine other agents drive. Three ways in, pick by what you need:

- **MCP (opencode, Command Code, Codex):** the `jev-browser` server is registered for each, with its
  own `JEV_MCP_BU_NAME` so instances never share a daemon or tab. Tools: `browser_do` (one goal +
  assertions), `browser_state` / `browser_act` (read the indexed elements, then execute one observed
  action with no model call), `browser_assert` (check a page, no model call), `browser_checks` (many
  checks in parallel, one browser instance each), and `browser_task` / `browser_task_resume` /
  `browser_task_cancel` (one task-owned visible tab with login and approval checkpoints).
  The original tools use a warm tab navigated fresh per call
  (`fresh: true` forces a new one); every run can write JSONL evidence under `artifacts/evidence/`
  (`JEV_EVIDENCE_DIR=0` disables it). Start the server directly with
  `.venv\Scripts\python.exe jev_mcp_server.py` if an agent needs it explained.
- **CLI, one task:** `jev --brief [--url <url>] [--tab background] [--values '<json>'] [--expect '<json>'] [--mode auto|<slug>] "<goal>"` (installed by `uv tool install --editable .`; `jev` alone is interactive; `run_task.py` remains as a shim). Without `--url` the start page comes from the goal (`AgentJevUltraFast/start.py`), never from a model.
- **App test suites (preferred for testing):** a Markdown spec (`AgentJevUltraFast/spec.py` documents
  the format; `skills/jev-test/SKILL.md` teaches it) run by `jev test <spec.md>` or the MCP tools
  `browser_test` / `browser_test_status` (background run, no 120 s cap). It uses a dedicated
  automation Chrome (no approval prompt), records verified flows and replays them with no model
  calls, heals one step at a time, and reports cost per test. Replay plans live in `<spec dir>/.jev/replay`
  and are meant to be committed; `.jev/evidence` and `.jev/reports` are ignored.
  `scripts/import_checklist.py` drafts specs from a JSON checklist (review the drafts).
  `jev crawl <url>` / MCP `browser_crawl` sweeps every same-origin page for JS errors and broken pages
  with no model calls (GET only; sign-out/delete links are never followed).
- **CLI, parallel app testing:** write a check file and run
  `uv run --env-file .env python scripts/run_checks.py checks.json --workers 6`
  (`scripts/run_checks.py` is a shim for `AgentJevUltraFast/checks.py`; pass `--chrome dedicated`
  to avoid the approval prompt entirely)
  (up to 10). Assertions in `expect` are read off the real page and are the verdict — not the agent's
  opinion. `checks.example.json` is a working template. Each instance (`jev-check-00`..`09`) is its
  own Chrome connection, so Chrome asks "Allow remote debugging?" once per instance the first time
  (its daemon then persists). A worker past `max_seconds + JEV_CHECK_STARTUP_S` (45 s) is killed and
  reported; an unapproved connection is reported as `blocked_reason: chrome-approval`, never a hang.

Every run reports `status` (what the agent did) and `outcome` (the code-owned verdict:
`verified` / `refuted` / `unknown` / `abstained` / `budget_exhausted`). Only an `expect` readback
produces `verified`; a `done` without one is `unknown` and must not be reported as success.

Exit codes: `0` only for `done`; `2` for `blocked`/`stalled`/budget; `1` for an error. A non-zero exit
is not a crash — read the JSON.

**The loop can never spin for more than ~5 s of browser time, or 20 s of wall time.** Once a page
stops yielding real progress (navigation, controls, control values — not ambient text such as a clock
or a carousel) the run terminates as `stalled`. Time spent waiting on a model is credited back to the
5 s clock (`JEV_STALL_SECONDS`) — a slow provider is not a stuck page — and is bounded instead by each
call's own timeout, the 8-round no-progress cap, and the uncredited wall-clock backstop
(`JEV_STALL_HARD_SECONDS`, default 20). `stall.reason` says which bound fired. Do not retry a stalled
run; the page did not respond to anything the agent tried.

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
  `AgentJevUltraFast/modes.py` ahead of anything new you learn.
- Learned values are typed only on an exact normalized label match, only when a fresh readback
  showed the field accepted the value, and never for sensitive labels, seek controls or replays.
- The confidence gate's `irreversible` reading is page-level, so floors apply to the chosen
  action's own risk class: only clicks can be committal; checkbox/radio/switch/link clicks and
  fills/selects/sliders are reversible. An *unambiguous* exact-label memory fill is
  code-validated for both element and value and skips the gate; two fields sharing a label
  fall back to the gate (the wrong-field risk).
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
- A mode may lower the irreversible floor (`specialist.irreversible_floor`, clamped at 0.5)
  for committable clicks on work its owner trusts; it is user config in `mode.json`, never
  model-set. The default 0.9 stays.
- A wedged page or daemon can hang an MCP tool call; every call is abandoned after
  `JEV_MCP_CALL_TIMEOUT_S` (default 120 s) with an `abandoned` error and the warm session is
  dropped. Treat an abandoned call as stalled and do not retry it.
