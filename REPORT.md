# Overnight report — 30/31 Oct 2026

Branch `overnight/jev-qa` (local, not pushed). Everything below was run on this machine against
real apps on localhost; numbers come from the run reports in each example's `.jev/reports/`.

## What you have now

Jev Ultrafast is now a **plain-English test runner for web apps** that any coding agent can drive,
built on the existing Jev loop (one cheap decision per step, no screenshots).

| Piece | What it does | Where |
|---|---|---|
| Markdown specs | `## Test`, `path:`, numbered steps (goals), check bullets (`see:`, `not see:`, `see any:`, `url contains:`, `count:`, `value:`, `no page errors`), `## Before each`, `- open: /path`, `values:` | `AgentJevUltraFast/spec.py` |
| `jev test` | runs a spec or JSON check file; dedicated Chrome, replay on, cost per test; `--repeat N` for flake rate; `--fresh` to re-record | `AgentJevUltraFast/cli.py`, `checks.py` |
| `jev init` / `jev draft` | starter spec; or one page read + one ~$0.0003 DeepSeek call → a draft suite with weak/predicted checks marked | `cli.py`, `draft.py` |
| Zero-model replay | verified runs recorded by meaning (kind, label, role + test id, name, placeholder, href, container); unique-match cascade; 2 s auto-wait; one-step heals with resync; healed plans re-recorded; supplied values by reference only | `replay.py`, `agent.py` |
| Stop rules | a step ends the moment a positive check holds (twice, page non-empty); a DONE the checks contradict is turned into "keep going" (×2); 25 actions per step | `agent.py`, `checks.py` |
| Never-hang browser | `JEV_CHROME=dedicated`: own profile on :9333, no "Allow remote debugging?" prompt, user tabs untouched; `jev test`/`draft` pick it at import; `JEV_DAEMON_WAIT_S`; headless for CI | `chrome.py`, `browser.py` |
| Isolation | `isolate: context` (fresh browser context per test, parallel) or `storage` (wipe the origin, warm cache, sequential) | `browser.py` |
| Robustness | content wait on navigation (SPA dev servers render after ~2-16 s); alert/confirm/prompt answered in tests (accept on local hosts); "Leave site?" accepted on the test's own navigation; page-error recorder; placeholder/value-aware checks | `browser.py`, `assertions.py` |
| MCP | `browser_test` (background run, returns run id) + `browser_test_status` (verdict, cost, failing checks, page excerpt) | `jev_mcp_server.py` |
| Agent skill | teaches any coding agent the spec format and how to read results | `skills/jev-test/SKILL.md` |
| Checklist importer | drafts specs from a JSON checklist (your StudyCanvas pre-production checklist) | `scripts/import_checklist.py` |
| Go-to-market | competitors, positioning, pricing, naming ("Rehearse, powered by Jev"), launch plan; landing page draft | `docs/go-to-market.md`, `docs/site/index.html` |

## Results

### StudyCanvas, hand-written suite (`examples/studycanvas/core.md`)
5 tests, each behind the access gate + 5-question onboarding (`## Before each`).

| Run | Verified | Model calls | Model cost | Steps replayed |
|---|---|---|---|---|
| fresh (nothing recorded) | 5/5 | 61 | $0.0085 | 32 |
| 2nd | 4/5 (one flaky step) | 10 | $0.0011 | 51 |
| 3rd | 5/5 | 3 | $0.00028 | 52 |
| fresh, after the anchor cascade | 5/5 | 31 | $0.0036 | 38 |
| headless Chrome (replay) | 5/5 | 15 | $0.0014 | 40 |

### Decision Lab (second, unrelated app; `examples/decision-lab/smoke.md`)
3/3 verified. A live, self-updating page exposed a cost hole (one test ran 60 s, 117 calls, $0.03,
because its only check was negative); the per-step action cap brought it to 55 calls / $0.012 with
the same verdicts, and specs with only negative checks now print a warning.

### Native dialog traps (`examples/traps/dialog.md`)
Delete behind `confirm()` → passes (1.1 s, 1 call). Navigation away from a page with an armed
"Leave site?" prompt → passes.

### Pre-production checklist, drafted automatically (`examples/studycanvas/checklist/`)
RESULTS_PLACEHOLDER

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
  differences are the 3 s auto-waiting readback and the content wait after navigation.
- StudyCanvas was only read and run, never edited. It runs in IndexedDB mode via its own
  `?storage=idb` dev switch, because the folder picker is a native dialog.
- Costs tonight: well under $1 of Jev + DeepSeek in total (per-run figures above).

## How to try it (5 minutes)

```bash
uv tool install --editable .            # once, from this folder (gives `jev`, `jev-mcp`)
jev test examples/studycanvas/core.md   # needs StudyCanvas running on :5173 / :8000
jev draft http://localhost:3000 --about "what the app does"   # any app
```

MCP: `browser_test {"spec_path": "<abs path>.md"}` then `browser_test_status {"run_id": ...}`.

## Recommended next steps

1. Pick the name (brief recommends **Rehearse**), then publish to PyPI so `uvx <name>` works.
2. GitHub Action that replays committed `.jev/replay` plans and comments on PRs (the paid bridge).
3. Volume agreement with TypeSafe for Jev before selling metered runs.
4. Reliability evidence across 5+ open-source apps, plus a head-to-head against Playwright agents.
5. Improve drafted-check quality (checklist/`jev draft`): positive, post-action checks are what make
   a draft trustworthy.
