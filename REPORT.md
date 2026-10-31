# Overnight report — 30/31 Oct 2026

Branch `overnight/jev-qa` (local, not pushed). Everything below was run on this machine against
real apps on localhost; numbers come from the run reports in each example's `.jev/reports/`.

**In one paragraph:** Jev Ultrafast is now a plain-English web-app test runner your coding agent can
drive (CLI, MCP, skill). On StudyCanvas the whole suite (sign-in, onboarding, tutorial, 5 tests)
costs about a cent the first time and **$0 with 0 model calls on every rerun**. It passes on two other
unrelated apps unchanged, never touches your own Chrome, and never lets a model decide pass/fail.
Your pre-production checklist drafts automatically into 23 runnable tests for under 10 cents, but
only 5 pass unedited: the drafts need a human (or agent) pass, and that is the weakest part. Three
code reviews' findings are fixed; 488 tests, lint, node checks and `uv build` pass.

## What you have now

Jev Ultrafast is now a **plain-English test runner for web apps** that any coding agent can drive,
built on the existing Jev loop (one cheap decision per step, no screenshots).

| Piece | What it does | Where |
|---|---|---|
| Markdown specs | `## Test`, `path:`, numbered steps (goals), check bullets (`see:`, `not see:`, `see any:`, `url contains:`, `count:`, `value:`, `no page errors`), `## Before each`, `## Before all` (once per run, like Playwright's global setup), `- open: /path`, `values:`, `check_wait:` (how long checks keep re-reading, for slow async work) | `AgentJevUltraFast/spec.py` |
| `jev crawl` | opens every same-origin page an app links to (GET only, risky links skipped) and reports JS errors, blank pages and error pages; 0 model calls (StudyCanvas: 7 pages in 5 s) | `crawl.py` |
| Check suggestions | a step written without checks reports `see:` / `not see:` candidates from what it changed on screen (no model call); `--write-suggestions` writes `<spec>.suggested.md` with them as comments (see `examples/studycanvas/checklist/A1-S10.suggested.md`) | `checks.py`, `spec.py` |
| `jev test` | runs a spec or JSON check file; dedicated Chrome, replay on, cost per test; `--repeat N` for flake rate; `--fresh` to re-record | `AgentJevUltraFast/cli.py`, `checks.py` |
| `jev init` / `jev draft` | starter spec; or the start page plus up to 4 linked pages (GET only) + one cheap DeepSeek call → a draft suite with weak/predicted checks marked | `cli.py`, `draft.py` |
| Zero-model replay | verified runs recorded by meaning (kind, label, role + test id, name, placeholder, href, container); unique-match cascade (same-labelled twins by recorded position, only when nothing contradicts it); record ids in URLs treated as one page (`/canvas/:id`); 2 s auto-wait; one-step heals with resync; healed plans re-recorded; supplied values by reference only | `replay.py`, `agent.py` |
| Stop rules | a step ends the moment a positive check holds (twice, page non-empty); a DONE the checks contradict is turned into "keep going" (×2); 25 actions per step | `agent.py`, `checks.py` |
| Never-hang browser | `JEV_CHROME=dedicated`: own profile on :9333, no "Allow remote debugging?" prompt, user tabs untouched; `jev test`/`draft` pick it at import; `JEV_DAEMON_WAIT_S`; headless for CI | `chrome.py`, `browser.py` |
| Isolation | `isolate: context` (fresh browser context per test, parallel) or `storage` (wipe the origin, warm cache, sequential) | `browser.py` |
| Robustness | content wait on navigation (SPA dev servers render after ~2-16 s); alert/confirm/prompt answered in tests (accept on local hosts); "Leave site?" accepted on the test's own navigation; page-error recorder; placeholder/value-aware checks; one retry of a page read that misses its deadline (never of a click) | `browser.py`, `assertions.py`, `agent.py` |
| Keyboard | a key the step names ("press ?", Ctrl+K, ArrowDown, "the Tab key") becomes an action, parsed by code; words like Home/End/Delete alone are not keys | `keys.py` |
| Many runs at once | concurrent `jev test` runs (several coding agents) each claim a slot, so they never share a browser connection or tab | `checks.py` |
| MCP | `browser_test` (background run, returns run id) + `browser_test_status` (verdict, cost, failing checks, page excerpt, Before all status); checked end to end over stdio | `jev_mcp_server.py` |
| Agent skill | teaches any coding agent the spec format and how to read results | `skills/jev-test/SKILL.md` |
| Checklist importer | drafts specs from a JSON checklist (your StudyCanvas pre-production checklist) | `scripts/import_checklist.py` |
| Go-to-market | competitors, positioning, pricing, naming ("Rehearse, powered by Jev"), launch plan; landing page draft | `docs/go-to-market.md`, `docs/site/index.html` |

## Results

### StudyCanvas, hand-written suite (`examples/studycanvas/core.md`)
5 tests behind the access gate, the 5-question onboarding and (new tonight) a tutorial that a
Gemini call writes. Final code, `## Before all` (sign in once per run). Costs include Before all.

| Run (final code, 23:41-23:45) | Verified | Model calls | Model cost |
|---|---|---|---|
| fresh (nothing recorded) | 5/5 | 55 (39 of them signing in) | $0.0119 |
| `--repeat 3`, run 1 | 5/5 | 19 | $0.0046 |
| run 2 | 5/5 | 9 | $0.0017 |
| run 3 | 5/5 | **0** | **$0.0000** |
| after two replay fixes (00:15), `--repeat 3` | 5/5, 5/5, 5/5 | **0, 0, 0** | **$0** |

The two replay fixes came from reading why runs 1-2 still needed calls: plans were keyed by the URL,
so a page reached as `/canvas/<new uuid>` never found its plan (ids now become `:id`); and a toolbar
and an empty-state "New canvas" button were ambiguous, so that step healed every run (twins are now
matched by recorded position when their count is unchanged).

Earlier tonight, with `## Before each` (sign in before every test) and before the review fixes:

| Run | Verified | Model calls | Model cost | Steps replayed |
|---|---|---|---|---|
| fresh (nothing recorded) | 5/5 | 61 | $0.0085 | 32 |
| 2nd | 4/5 (one flaky step) | 10 | $0.0011 | 51 |
| 3rd | 5/5 | 3 | $0.00028 | 52 |
| headless Chrome (replay) | 5/5 | 15 | $0.0014 | 40 |

### Decision Lab (second, unrelated app; `examples/decision-lab/smoke.md`)
3/3 verified on every run. A live, self-updating page exposed a cost hole: the "answer the
question" test had only a negative check, so nothing told it that it had arrived.

| Run | Verified | Model calls | Model cost |
|---|---|---|---|
| first measurement (no step cap) | 3/3 | 117 in one test | ~$0.03 |
| per-step action cap, final round fresh | 3/3 | 52 | $0.0137 |
| same, replay | 3/3 | 58 | $0.0185 |
| one positive check added to that test, fresh | 3/3 | 33 | $0.0069 |
| same, replay | 3/3 | 10 | $0.0021 |

Specs whose steps have only negative checks now print a warning saying exactly this.

### Moneywell Town (third app, `examples/moneywell/age-gate.md`, new)
A browser game from `Desktop\Hackathons\WIF Hackathon\wif-team17`, run read-only (its git status
stayed clean). Three tests of its 15+ age gate, written by hand from `src/data/ageCheck.ts` in a few
minutes: an adult reaches the title screen, a child is refused, a two-digit year gets the hint.
Fresh: 3/3, 12 calls, $0.0020, 5 s wall in parallel. Replay: 3/3, **0 calls**. The three tests share
one recorded plan and each replays its own birthday (values are stored by reference, never typed
into the plan).

`jev draft` on its landing page (`drafted.md` beside it) was poor: the landing page embeds the game,
so nearly every check it proposed was already on screen before its step and is marked *weak*. The
drafter now also reads up to 4 same-origin pages the start page links to (GET only), which helps
multi-page apps but not this one-page site. Drafting good checks remains the weakest part.

### Keyboard shortcuts (`examples/traps/keys.md`, new)
A page whose shortcuts have no button and no `aria-keyshortcuts`: `Press "?"` and `Ctrl+K` both
pass (2/2, 1 model call). Before tonight Jev could only press Escape, Enter and page-declared keys.

### Native dialog traps (`examples/traps/dialog.md`)
Delete behind `confirm()` → passes (1.1 s, 1 call). Navigation away from a page with an armed
"Leave site?" prompt → passes.

### Pre-production checklist, drafted automatically (`examples/studycanvas/checklist/`)
23 tests drafted with no human editing from 5 sections of your pre-production checklist
(`scripts/import_checklist.py --align`), run fresh once (23:45-23:57, final round):

| Section | Verified | Without checks (cannot pass) | Model calls | Cost |
|---|---|---|---|---|
| A1-S05 routing | 0/2 | 1 | 50 | $0.0109 |
| A1-S06 keyboard shortcuts | 0/3 | 2 | 107 | $0.0250 |
| A1-S07 home screen | 3/8 | 1 | 115 | $0.0291 |
| A1-S08 search | 1/3 | 1 | 52 | $0.0106 |
| A1-S10 spaces | 1/7 | 2 | 77 | $0.0192 |
| **total** | **5/23** | 7 | 401 | **$0.095** |

Read this as a measure of the drafts, not of the app:
- 7 tests have no quoted expectation in the checklist, so they have no check and report "unknown"
  (never a pass). Each prints suggested checks taken from what its step changed on screen.
- The checklist states expectations once per item, so the first drafts checked things after the step
  that leaves them ("read the 404 page, then click Go home" checked the 404 page on the home page).
  `--align` (one cheap DeepSeek call per item) now places each check on its step: the 404 test's
  checks then passed on the 404 page, and A1-S07's drafts read as a tester would write them.
- A1-S06 is all keyboard shortcuts ("press ?", "Ctrl+F") and ran before key support. Re-run just
  after midnight with it: 1/3, and the 1 is the only A1-S06 test that has checks (open a canvas, press "?",
  read the Canvas group), now passing; 70 calls, $0.012. The same re-run showed Jev typing the
  literal text "a word" for "type a word in the filter"; placeholders are now composed instead.
- Several checks quote copy the app no longer shows (StudyCanvas was being edited tonight, see below).
- After this round, the A1-S05 404 test (counted as a failure) turned out to be a page read that
  timed out on the landing page right after "Go home"; Jev now retries one read (never a click).

The useful signal is that the 16 drafts that do have checks can be fixed by reviewing the drafts
(the failing check, its step and the page excerpt are in each report), and that a full unattended
run of 23 tests costs under 10 cents.

## Things you should know

- **One approval prompt reached your real Chrome at ~22:45.** `jev draft` initially chose the
  dedicated Chrome too late (after the package had attached to your normal Chrome); it waited on
  "Allow remote debugging?" until I killed it. Nothing was approved. Fixed at the root (commit
  959f15a): `jev test`/`jev draft` now select the dedicated Chrome at import, and `JEV_DAEMON_WAIT_S`
  bounds any connection wait.
- **A StudyCanvas tab on port 5201 sits in the dedicated Chrome (:9333)** that none of my runs
  opened (it was there before I wrote any code). Another tool on this machine appears to use port
  9333; I left it alone. If that is yours, consider `JEV_CHROME_PORT` to keep them apart.
- Your other `jev-mcp` servers import this checkout (editable install), so they pick up tonight's
  code. Defaults are unchanged for them (user Chrome, no isolation, no dialog policy); the visible
  differences are the 3 s auto-waiting readback, the content wait after navigation, and that a
  goal naming a key ("press ?", Ctrl+K) now gets that key as an action.
- StudyCanvas was only read and run, never edited. It runs in IndexedDB mode via its own
  `?storage=idb` dev switch, because the folder picker is a native dialog.
- **StudyCanvas rate-limits access-code redeems (10 a minute, 50 an hour).** A suite that signed
  in before every test exhausted it and every later run failed at the gate ("Could not reach
  StudyCanvas"). That is why `## Before all` exists. I restarted the uvicorn on :8000 that I had
  started myself, to clear its in-memory limiter; nothing else in StudyCanvas was touched.
- **Each StudyCanvas suite run makes one Gemini call on the app's own key**: onboarding's last
  button now starts a tutorial that writes a lecture (~9 s). That is why the setup has
  `check_wait: 30`. The app also rate-limits that endpoint to **8 lectures an hour** (local-dev),
  which tonight's repeated runs exceeded twice: the onboarding then waits, Jev takes over after the
  30 s readback and finishes it, and the run still verifies, but slower and with ~7 model calls.
- **Another session was editing StudyCanvas tonight.** Its saves caused hot-reload storms that
  hung my Vite on :5173, so I ran Vite with a config of my own outside the repo (HMR and file
  watching off: `scratchpad\vite.frozen.config.ts`). Runs between 23:00 and 23:30 that failed for
  that reason or for the rate limit are not in the results above.
- **Two bugs found by the runs themselves, fixed:** a cached "abstain" decision was replayed from
  the decision cache and failed a test with zero model calls (refusals are no longer cached); and
  `jev test` workers inherited the dedicated Chrome endpoint without its own daemon name, so one
  could have reused a same-named daemon attached to your normal Chrome.
- **Three code reviews tonight.** An Opus review (16 findings, all high/medium fixed, b53fa85), a
  Sonnet review (6 findings, all fixed, 01bee4c) and a second Sonnet review of keys, slots, crawl and
  replay matching (9 findings, all fixed, dc79de8). Among them: `isolate: none`
  under `## Before all` silently signed every test out, and a Before all without a check blocked
  the whole suite with no explanation (now a parse error). Running the rounds also showed that an
  unchecked step that stalls could still let its test pass if it was the last step; fixed.
- **Leftover processes:** test runs leave one browser daemon per worker name (by design, so a
  warm connection is reused). Tonight's orphans from the daemon-name bug were stopped (52 to 30
  processes); the rest belong to names still in use. A `jev-check-08` lock file could not be removed
  (access denied) and is harmless.
- **Ownership before selling:** this checkout's `origin` is `github.com/browser-use/jev-ultrafast`,
  and the LICENSE says "Copyright (c) 2026 Browser Use", MIT. MIT lets you sell a product built on
  it, but check with Browser Use who owns the product and name before launching under your own
  brand.
- Costs tonight: well under $1 of Jev + DeepSeek in total (per-run figures above).

## How to try it (5 minutes)

```bash
uv tool install --editable .            # once, from this folder (gives `jev`, `jev-mcp`)
jev test examples/studycanvas/core.md   # needs StudyCanvas running on :5173 / :8000
jev draft http://localhost:3000 --about "what the app does"   # any app
jev crawl http://localhost:3000         # broken pages and JS errors, no model calls
python scripts/trap_server.py           # then: jev test examples/traps/keys.md (or dialog.md)
```

MCP: `browser_test {"spec_path": "<abs path>.md"}` then `browser_test_status {"run_id": ...}`.

## Recommended next steps

1. Pick the name (brief recommends **Rehearse**), then publish to PyPI so `uvx <name>` works.
2. GitHub Action that replays committed `.jev/replay` plans and comments on PRs (the paid bridge).
3. Volume agreement with TypeSafe for Jev before selling metered runs.
4. Reliability evidence across 5+ open-source apps, plus a head-to-head against Playwright agents.
5. Improve drafted-check quality (checklist/`jev draft`): positive, post-action checks are what make
   a draft trustworthy.
