<img src="docs/banner.svg" alt="AgentJevUltraFast · Browser Use × TypeSafe" width="100%" />

# AgentJevUltraFast ⚡

> [!IMPORTANT]
> **The Browser Use Cloud waitlist is open.** Get early access to ultrafast browser agents in the cloud.
> **[Join the waitlist →](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Historical baseline:** Zürich → London on Google Flights in 7.1 seconds. That recording predates the delegation and profile changes; the delegated interface now has its own cold/warm measurement in [performance.md](docs/performance.md), and a fresh Flights benchmark is still pending.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](AgentJevUltraFast/agent.py)

[Architecture and current limits](docs/architecture.md) · [Delegation demo](docs/delegation-demo.md)

## Test your web app in plain English

Write tests the way you would brief a tester. Jev does each step on the real page; the checks under
it, read off the page, are the only verdict. A verified run is recorded, and every later run replays
it with **no model calls**, handing a step back to Jev only when the app changed under it.

```markdown
# Shop smoke
base_url: http://localhost:3000
isolate: storage

## Before each
path: /login
values:
  - Email: ada@example.com
  - Password: correct-horse
1. Sign in.
   - see: Dashboard

## Search finds a product
path: /
1. Search for laptops.
   - see: Results for "laptops"
   - not see: No results
   - no page errors
```

```bash
jev init --url http://localhost:3000     # writes jev-tests/smoke.md
jev draft http://localhost:3000 --about "a recipe planner"   # or: one page read + one cheap call
jev test jev-tests/smoke.md              # runs it; plans land in jev-tests/.jev/replay
jev test jev-tests/smoke.md --repeat 3   # pass rate per test (flaky / always failing) and cost per run
jev crawl http://localhost:3000          # every linked page: JS errors, blank or error pages; 0 model calls
```

`jev draft` marks every check it could not see on the start page as *predicted* and every check that
was already true before its step as *weak*; review those before trusting a pass. A step written
without checks still runs, reports `unknown` (never a pass), and prints suggested `see:` / `not see:`
lines taken from what it changed on screen; `jev test <spec> --write-suggestions` writes them
into `<spec>.suggested.md` as comments under each step, ready to review and uncomment.

`## Before each` runs at the start of every test. `## Before all` runs once per `jev test`, alone,
and the tests start from the state it left (signed in, onboarded): use it when signing in is slow or
rate-limited, and note that its tests then run one at a time. `check_wait: 20` (suite or test) lets
the checks keep re-reading for up to 20 s, for steps that start slow work such as a model call or
an upload (default 3 s).

Keyboard shortcuts: a step that names a key ("Press \"?\" to open the shortcuts", "Open the command
bar with Ctrl+K", "move down with ArrowDown", "the Tab key") gets that key as an action, parsed from
the step's own words; Jev never invents a key. Several `jev test` runs (or coding agents) can run at
once on one machine: each takes its own set of browser connections. A JSON test checklist
can be drafted the same way with `scripts/import_checklist.py`.

Measured on a real app ([StudyCanvas](examples/studycanvas/core.md), Vite + React on localhost; the
suite first passes an access gate, a five-question onboarding and a tutorial, once per run):

| Run | Verified | Model calls | Model cost |
|---|---|---|---|
| first (nothing recorded) | 5/5 | 55 (39 of them signing in) | $0.0119 |
| third (replay) | 5/5 | 0 | $0.0000 |

A second, unrelated app ([Decision Lab](examples/decision-lab/smoke.md)) passes 3/3 with the same runner
($0.0069 first run, $0.0021 on replay). A third (a browser game's age gate) passes 3/3 for $0.002, then 0 model calls on replay.
Full results in [REPORT.md](REPORT.md).

- **Checks:** `see:`, `see any: A | B`, `not see:`, `url contains:`, `url matches:`, `title contains:`,
  `count: SELECTOR >= N`, `value: SELECTOR contains X`, `no page errors` (uncaught exceptions,
  unhandled rejections and `console.error`). They re-read the page for up to 3 s, like
  auto-waiting test assertions. A step with a positive check ends the moment it holds.
- **Replay:** steps are recorded by meaning (kind, accessible name, role, plus test id, name,
  placeholder, link path and enclosing dialog/section) and must match exactly one element. A step that
  no longer matches is decided by Jev (a heal), replay resumes on the next matching step, and the healed
  plan is re-recorded. Supplied values are stored by reference, never by value. Commit-like clicks
  (Save, Delete, Submit) replay only on local dev hosts; elsewhere they get a fresh gated decision.
  Commit `.jev/replay/` and CI reruns cost nothing.
- **Unattended by default:** `jev test` uses its own automation Chrome (`JEV_CHROME=dedicated`, a
  separate profile on port 9333), so there is never an "Allow remote debugging?" prompt and your own
  tabs are never touched. `JEV_CHROME_HEADLESS=1` for CI. `isolate: context` gives each test a fresh
  browser context (parallel); `isolate: storage` wipes the app origin's storage instead and keeps the
  HTTP cache warm (sequential; much faster on dev servers).
- **Bounded:** each test has `max_seconds`, each step at most 25 actions (`max_steps`), and a step with
  only `not see:` checks gets a warning, because absence cannot tell a run it has arrived.
- **From a coding agent:** the MCP tool `browser_test` takes a spec path or inline Markdown and returns
  a run id at once; `browser_test_status` returns pass/fail, cost, the failing checks and an excerpt
  of what the page showed.

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `REOBSERVE`, `ABSTAIN`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered, and `REOBSERVE` is withdrawn after its per-run budget.

Element state rides along: `checked`, `selected`, `expanded` and `pressed` from ARIA, field values, and, when the page has a real video player, one `[video: speed 1.5x, fullscreen, playing, sound on]` line from the standard media API. Keyboard operations are `Escape`, `Enter`, and only the shortcuts the page itself declares with `aria-keyshortcuts`, labelled with the control they stand for (`Press f (page shortcut: Full screen (f))`). Nothing site-specific is hardcoded; a shortcut is gated exactly like a click on its control, so `Control+Enter (page shortcut: Send)` keeps the commit floor.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

When the goal itself contains the text to type ("type in MKBHD", `saying "banger"`), code extracts those literals and the same request carries a `type_value` choice head: Jev picks which literal belongs in the field, or `COMPOSE`, and only then does the small LLM write the value. Measured on YouTube: decision-to-typed dropped from 2.6 s to 0.6 s.

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone <this repo> jev-ultrafast && cd jev-ultrafast
uv tool install --editable .          # puts `jev` on your PATH
cp .env.example .env                  # add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY
jev "open youtube, type in MKBHD and open his iPhone 17 review, then turn on captions"
```

Your words go **straight to Jev**: no chat model reads, plans or rewrites them first. The start
page comes from the goal itself (a URL or domain it names, else the site after "open / go to /
visit", resolved by one HTTP lookup), each step streams to the terminal as it executes, and the
final result is JSON on stdout. Run `jev` with no arguments for an interactive prompt that keeps
one Chrome tab warm; a line that names no site continues on the current page:

```text
$ jev "Open YouTube, type in MKBHD, go to click on its first iPhone 17 video, click on captions, turn on captions"
  -> https://youtube.com/  (first result for 'YouTube')
    1  fill   Search                                               0.85    8.8s
    2  click  mkbhd iphone 17                                      0.88   11.1s
    3  click  iPhone 17 Review: No Asterisks! 11 minutes, 20 seconds  0.93   17.2s
    4  key    Toggle captions (C)                                  0.49   23.3s
  done (unknown) 23.7s, 4 actions
```

(Measured 2026-09-24, foreground tab, cold process; times are from the command's start. Every
Jev decision took 250-580 ms: the rest is YouTube loading its pages. `unknown` because no
`--expect` readback was given.)

Coding agents call the same command. The `jev-browser` skill tells Claude Code, Codex, opencode
and Command Code to run `jev --brief "<the user's words verbatim>"` instead of planning, and
`!jev "..."` in those tools skips the model turn entirely. The MCP server (`jev-mcp`) remains for
structured testing: parallel checks, assertions and delegated tasks.

The web inspector is `jev-ui`: open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is the key for the text helper. The current configuration runs `deepseek-v4.1-flash` through the OpenAI-compatible OpenCode Go endpoint with reasoning disabled (`TEXT_MODEL`, `TEXT_MODEL_BASE_URL`, `TEXT_MODEL_REASONING` in `.env`). Gemini, GLM, DeepSeek and other OpenAI-compatible providers work too; configure the model, endpoint, and reasoning setting together. The 7.073 s recording used `inception/mercury-2.5`, so its helper latencies are historical rather than a property of the current configuration.

**Optional Cloud Chrome pilot:** [docs/cloud-browser.md](docs/cloud-browser.md) explains the labeled API-key settings, free-credit budget, `jev-cloud` command, and browser stop/recovery procedure. Cloud supplies Chrome over CDP; Jev still makes the decisions and verifies outcomes.

## Use the library

```python
from AgentJevUltraFast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation alone is not enough: measured on Chrome 153, a background tab of a real site ran 0–2 animation frames per 500 ms, so JavaScript menus never finished opening and their options stayed invisible to the snapshot. A tiny, never-acknowledged screencast makes Chrome render the tab like a visible one (30–32 frames, menus open within 50 ms, screenshots in ~100 ms) without switching Chrome's visible tab. `JEV_RENDER_KEEPALIVE=0` turns it off.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Draft field values while Jev decides.** One text-helper call drafts every fillable, non-sensitive field on the page in parallel with the decision, so TYPE_TEXT does not wait on it (Flights: two ~1.35 s waits became 0 ms). A draft is used only under the identical goal and only for a field it saw; a null draft falls back to a single-field call. `JEV_TEXT_PREFETCH=0` turns it off.
- **Click what is visible, the way a person does.** The pointer moves onto the target before the press, so hover-revealed controls (a video player's faded bar, a row's action buttons) respond. If a popover or sticky bar covers the centre of a control, the click lands on an uncovered point of the same element; only a fully covered control counts as covered, which presses Escape once per URL and then routes to the next-best choice.
- **Re-read a page mid-navigation.** An observation retries a document that is being replaced for up to `JEV_NAV_READ_TIMEOUT` (3 s): 0.2 s ended a heavy results page (eBay) as unreadable.
- **Settle ignores a playing video.** The player's own text (clock, captions) is left out of the settle signature: observing a watch page dropped from 2.3 s to about 0.1 s.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Verified outcomes, not opinions

`status` is what the agent did: `done`, `blocked`, `stalled`, or `abstained` (a collect-strategy run passes through `collecting`). `outcome` is the code-owned verdict, resolved from an independent readback:

| outcome | meaning |
| --- | --- |
| `verified` | an `expect` assertion ran against the real page and matched |
| `refuted` | a readback ran and did not match |
| `unknown` | nothing to verify against — a `DONE` choice alone is not proof |
| `abstained` | a risk gate refused to act |
| `budget_exhausted` | the anti-loop deadline or a step/time budget fired |

Pass `expect` (`url_contains`, `url_matches`, `title_contains`, `text_contains`, `text_absent`, `selector_count`, `value_contains` for an input's value, `harvest_at_least` for collected items) to `browser_do`, `run_task.py --expect` or `Agent(expect=...)` to get a verdict. Without one, a completed run reports `unknown`, never `verified`. `text_contains` / `text_absent` take a phrase or a list of phrases (a bare string is one phrase). An unknown key, a malformed value, or a vacuous one (`at_least: 0`, an empty `value_contains` text) is rejected before the browser starts, because a check that cannot fail must not produce `verified`.

Three v2 properties go with it: a low-confidence *commit* (a click Jev marks irreversible — pay, send, submit an application) is re-observed once and then refused rather than executed, with the `irreversible` question riding the same request; repeated decisions are served from a disk cache keyed on the entire request; and a run can write append-only JSONL evidence — offered candidates, raw answers, confidences, the answering model, executions, outcome. Reversible actions (search, open, fill, scroll) execute Jev's top choice as the original loop did; `JEV_GATE_REVERSIBLE=0.5` re-enables a floor for them.

## When Jev gets stuck: the on-demand supervisor

Jev decides every step. A second, slower model (DeepSeek v4.1 Flash by default, through the text-model endpoint) is consulted **only** when code-owned telemetry says the loop is stuck, and the result names the check that fired: `no_progress_deadline`, `repeated_no_progress_rounds`, `dead_streak` (three actions in a row changed nothing), or `model_blocked` (Jev chose BLOCKED). It sees the goal, the recent trail, the numbered elements, the page text and — unless a private value is on the page — one screenshot, and it may answer:

- `hint`: a one-sentence sub-goal that rides Jev's next requests (operation and target heads only, never `goal_done`) for up to six actions;
- `act`: one element id from the current table, executed through the normal `act()` path with its freshness check and delegated approval. A click that could commit is downgraded to a hint;
- `done`, `impossible`, `needs_human`: end the run. `done` is no more proof than Jev's own DONE — the outcome still comes from the `expect` readback.

No coordinates, selectors or scripts are ever accepted. At most `JEV_SUPERVISOR_LIMIT` (2) consults per run, each bounded by `JEV_SUPERVISOR_TIMEOUT` (8 s); every consult is returned in `supervision` and written to evidence. `JEV_SUPERVISOR=off` disables it.

## While Jev works: the parallel reviewer

The supervisor is late by design: it waits for a stuck check. The reviewer asks the same model the same question continuously, off the critical path. After each executed step the loop hands it a redacted snapshot (goal, recent actions and outcomes, page text, elements, video state) and moves on at once. One review is in flight at a time; a newer snapshot replaces a queued one. Each decision polls without blocking, and a finished review that says the run is off track becomes a sub-goal on the supervisor's guidance channel for six steps, only if the page it judged is still the current one. It cannot click, type or end a run.

Before a `DONE` is accepted, the loop waits for one review of the final page (bounded by `JEV_REVIEW_TIMEOUT`, credited to the stall clock) that compares every requested value exactly against the page; an off-track verdict turns the DONE into a sub-goal, at most `JEV_REVIEW_DONE_LIMIT` (2) times per run. `JEV_REVIEW=off` disables it; `JEV_REVIEW_LIMIT` (12) caps reviews per run. Verdicts are in the result's `reviews` and in evidence.

## Navigate there and show me

A coding agent testing its own app can hand over the navigation and get facts plus a picture back in one call:

```json
{"name": "browser_do", "arguments": {
  "goal": "Open Settings, then Billing, and show the current plan",
  "url": "http://localhost:3000",
  "plan": ["Open Settings", "Open Billing"],
  "expect": {"text_contains": "Current plan"},
  "screenshot": true}}
```

The result carries `status`, the code-owned `outcome`, the check details, the final URL and title, and — with `screenshot: true` — the final page as a JPEG image content block (rendered even from a background tab; skipped when a private value is on the page). `plan` is optional orchestrator context for the supervisor if the run gets stuck; Jev still decides every step from the goal. `run_task.py --plan '[...]' --screenshot final.jpg` does the same from the CLI. Measured on the local fixture through the real MCP server: verified in 2.3 s of agent time, 5.0 s wall including browser setup.

## Delegate a browser goal from another agent

The MCP server offers `browser_task(goal, url, expect?, mode?)`. It returns a task ID
and a checkpoint when the browser needs a human login, one-time code, CAPTCHA,
or approval for a consequential click. After the human finishes in the same
Chrome tab, call `browser_task_resume(task_id, checkpoint)`. If the result
proposes a publish, purchase, deletion, account-security, or other submit
action, show that exact proposed action to the human; only call resume with
`approve_action=true` after they approve it. Use `browser_task_cancel` to close
a yielded task. Every checkpoint is single-use: a changed page requires a new
prediction and another approval. A task ID expires after 30 minutes of waiting
or when the MCP server restarts.

This is a single-process, single-task delegation interface. Active work runs
in bounded slices and the result distinguishes `paused`, `needs_human`,
`complete`, `cancelled`, and `abandoned`. A pause is not success. Only the
independent `expect` readback can produce `verified`; an uncertain dispatched
action stops the task with `unknown` rather than repeating it. Login and
approval signals are based on visible DOM controls and labels, so callers must
still review the proposed action. Descriptor replay is disabled for delegated
tasks while it lacks per-step approval preflight; a supervisor action goes
through the same approval boundary as any Jev decision. New-tab journeys are not yet supported by this task interface.

Delegated tasks can fill exact, unique field labels from the local profile at
`~/.jev-ultrafast/profile/`. The first profile fill on a web origin yields a
`confirm_fill` checkpoint; subsequent fills on that origin use the recorded
approval. [Profile setup and privacy limits](docs/profile.md) explain the JSON
format. The profile stores no passwords or payment details; type those into
Chrome yourself. A Windows DPAPI vault is a separate future feature.

## Modes: memory and specialists per task class

A mode is a folder for one repeated kind of work — internship applications, scrolling a feed — with its own memory, learned field values and settings. The agent gets that context before it decides anything:

```bash
# Match an existing mode by keyword, or let the text model create one for a durable task.
jev --mode auto --url https://x.com/home "..."

# Manage the memory of a mode you own.
jev --mode internship-application --remember "Name: Ada Lovelace"
jev --mode internship-application --forget "Email"
```

- Folders live in `JEV_MODES_DIR` (default `~/.jev-ultrafast/modes`, outside git). Each holds `mode.json`, a human-editable `AGENTS.md` (about me / standing instructions / interests), `learned/` field values, `harvest/` collected items and `replay/` plans.
- `--mode auto` (or `JEV_MODES=auto`) matches existing modes by keyword; only a durable task class gets a new folder, and creation is printed, recorded in evidence and named in the mode's own file. One-off errands stay mode-less.
- A fill with an exact label and web-origin match in learned memory is typed with no model call. Caller-supplied `values` outrank memory. Learned literals stay out of model prompts.
- Sensitive labels (password, passcode, OTP, card, CVV, SSN, passport, IBAN, bank, national insurance, routing/sort code, PIN) are never learned, never injected into a prompt, and never persisted in a replay plan.
- A collect-strategy mode scrolls and harvests only — it never clicks anything — and stores an item only if its title appears verbatim in the page text. Live results for the internship, local-feed and real-x.com scenarios are in [docs/modes-scenarios.json](docs/modes-scenarios.json).

[Task-class routing](docs/routing.md) records Jev predictions in shadow mode
after deterministic misses. It can only control routing after six active
modes and an independently labelled accuracy gate pass.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](AgentJevUltraFast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](AgentJevUltraFast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](AgentJevUltraFast/browser.py) | Browser connection, current geometry, execution |
| [model.py](AgentJevUltraFast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](AgentJevUltraFast/questions.py) | Model instructions |
| [outcomes.py](AgentJevUltraFast/outcomes.py) | Code-owned outcome vocabulary |
| [assertions.py](AgentJevUltraFast/assertions.py) | Readback assertions and page facts |
| [gates.py](AgentJevUltraFast/gates.py) | Per-risk confidence gates |
| [cache.py](AgentJevUltraFast/cache.py) | Decision cache keyed on the full request |
| [evidence.py](AgentJevUltraFast/evidence.py) | Append-only run evidence |
| [demo.py](AgentJevUltraFast/demo.py) | Local inspector |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark. Both figures predate the v2 outcome/gate layer, and the recorded task date (Sunday, September 20, 2026) has since passed, so the matched comparison cannot be re-run as published; [performance.md](docs/performance.md) documents what was re-measured instead.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling, and keyboard widgets without declared shortcuts remain outside this MVP. Owned tabs share the existing Chrome profile.

## Development

```bash
uv run ruff check .
uv run pytest
node --check AgentJevUltraFast/static/app.js
node --check AgentJevUltraFast/snapshot.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
