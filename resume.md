# Overnight run: resume notes

Deadline: 02:30 on 31 Oct 2026. Branch: `overnight/jev-qa` (local only, never push).
Goal: cheapest browser agent that keeps quality and speed; first real use is repeatable app testing
(Study Canvas pre-production checklist on localhost).

## Rules from the user
- Study Canvas (`C:\Users\aksha\Desktop\Hackathons\Hack London KCL\StudyCanvas`) is read-only. Run it on
  localhost only (backend :8000, frontend :5173); any access code works locally. Test files live in this repo.
- OpenCode: DeepSeek V4.1 Flash only, no spend cap. Jev API: stay under $2.
- At 95% Claude usage (5-hour or weekly): update this file and stop; the cron job (every 20 min at :07/:27/:47)
  wakes the session. Weekly was 69% at 21:33 and resets 03:00, so it is the binding limit.
- Never use the user's real Chrome: launch the separate profile
  `~/.jev-ultrafast/chrome-test-profile` with `--remote-debugging-port=9333` and set `BU_CDP_WS`.
  Verified 21:40: no "Allow remote debugging" prompt.
- Run tests with `.venv\Scripts\python.exe -m pytest` (uv sync fails while a daemon holds the venv).
- Nothing may be Study-Canvas-specific: build generic localhost app testing. Also trial on 1-2 other
  projects in Desktop\GitHub or Desktop\Hackathons (read-only, few API calls).
- Keep RAM under 90% (slow down if near), close stale tabs/processes, prune old artifacts.
- New DeepSeek key works (verified 21:59). OpenCode CLI also works.
- Study Canvas: backend `..\.venv\Scripts\python.exe -m uvicorn main:app --port 8000` in backend\,
  frontend `npm run dev -- --port 5173 --strictPort` in frontend\. Use `?storage=idb` (dev switch, no
  folder picker). One-tab lock: use isolated contexts per check. Logs in scratchpad sc-*.out.

## Research conclusions (done)
- Landscape: record/replay with zero LLM calls (Stagehand, Momentic, Skyvern, workflow-use); semantic
  fingerprint re-match; heal only the broken step + heal log; pre/post conditions; variables for values;
  Markdown specs (Playwright planner style) compiled to expect; never LLM-judged verdicts.
- Audit gaps: JEV_CHROME only honoured if set in the process env (apply_env runs at import, before
  .env loads); run_checks should ensure_chrome once; replay breaks on supplied values, never heals, never
  re-records, skips gates; text_contains is any-of; JS dialogs unhandled; MCP browser_checks 120 s cap;
  reviewer on by default (up to 12 calls/run) and not in cost reports.

## OSS deep-dive (clones in scratchpad\oss; browser-use/stagehand MIT, playwright Apache, workflow-use/skyvern AGPL = ideas only)
Adopt next: record richer anchors (testid, id unless generated, name, placeholder, href path, container
heading/landmark, ordinal+count among same-label) and a tiered unique-match cascade (testid/id ->
role+name -> narrow by container/ordinal -> placeholder/name/href -> fuzzy unique best); log the tier;
idempotent toggle skip; quarantine plan after refuted; `skip`/quarantined test flag; plan trusted only
after 2 identical verified recordings (maybe); miss reasons in report; Playwright planner spec shape.

## Progress log
- 22:18 committed: self-healing replay, check runner in package (AgentJevUltraFast/checks.py), Markdown
  specs (spec.py, `## Before each`), `jev test` / `jev init`, isolation context|storage, cost reporting,
  stop_on_expect (positive checks only), replay auto-wait, commit replay on local hosts.
  StudyCanvas examples/studycanvas/core.md: 5/5 fresh $0.0085 (61 calls) -> rerun $0.00028 (3 calls).
- 22:55 committed since: MCP browser_test/_status, anchors cascade, README section, skills/jev-test,
  headless verified (5/5), step cap 25, dialogs policy (trap /dialog passes), viewport, checklist
  importer (scripts/import_checklist.py -> examples/studycanvas/checklist/*.md), docs/go-to-market.md.
  Running: 5 draft checklist sections (log in scratchpad checklist-run.log).
- 22:58 since then: jev draft, --repeat, DONE veto by checks, stale-replay retry, dialogs/beforeunload,
  accessible-name/placeholder-aware checks, action.yml (untested), landing page docs/site/index.html,
  REPORT.md draft (fill RESULTS_PLACEHOLDER from night-runs.log). Detached script night-runs.ps1
  (pid 22648) runs 5 checklist sections --fresh + core --repeat 3; log scratchpad\night-runs.log.
  Opus code review running in background; apply its verified findings.
  INCIDENT: jev draft hit user's Chrome approval prompt once (~22:45), fixed (959f15a).
- 23:01 StudyCanvas is being edited by ANOTHER session (HMR storms hung my :5173 Vite). Restarted my
  Vite with scratchpad\vite.frozen.config.ts (hmr off, watch ignored; NODE_PATH=frontend\node_modules).
  Night runs restarted (pid 48488). Earlier checklist numbers (8/25) were partly hit by the HMR storm.
- 23:13 Opus review: 16 findings; all high/medium fixed in b53fa85 (439 tests). Night runs straddled
  the fix. NEXT: after night-runs DONE, clean final round on fixed code: core.md --repeat 3, the 5
  checklist sections (regenerate with importer first), decision-lab smoke; then fill REPORT.md.
- 23:15 runtime TypeError from the usage fix (review evidence) found by the night runs, fixed + test.
  FINAL ROUND running: scratchpad\final-runs.ps1 (pid 57028) -> scratchpad\final-runs.log. When DONE,
  fill REPORT.md RESULTS (core fresh/repeat, 5 checklist sections, decision-lab fresh/replay).
- 23:25 StudyCanvas rate-limits /api/auth/redeem (10/min, 50/h): added `## Before all` (da60205), restarted
  my uvicorn :8000 to reset the limiter. Onboarding now ends in a ~9 s Gemini tutorial lecture: added
  `check_wait` (601798e). A cached ABSTAIN failed a test with 0 calls: refusals never cached (601798e).
  Worker daemons lost the -chrome9333 suffix (inherited BU_CDP_URL) -> fixed (25a4ade).
  23:32 FINAL ROUND re-running (pid 57820, scratchpad\final-runs.log); core fresh 5/5.
  After it: stop orphan non-suffixed jev-check-NN daemons (all started tonight; ~1.4 GB), fill REPORT.md.
- 23:45 New since: suggested checks for check-less steps (403260e), importer --align (f943ae7), setup now
  ends the tutorial (it blocked later tests), run cost includes Before all (e25a036). Core fresh 5/5
  ($0.0037 tests + $0.0082 Before all). CLEAN ROUND running: scratchpad\final-runs2.ps1 (pid 46248) ->
  final-runs2.log: core --repeat 3, 5 aligned checklist sections --fresh, decision-lab fresh+replay.
  Unaligned checklist numbers (before tutorial fix): S05 0/2, S06 0/3, S08 1/3.
- 00:05 Clean round DONE: core repeat 5/5 x3 (19 -> 9 -> 0 calls), aligned checklist 5/23 (7 without
  checks; S06 keyboard ran before key support), Decision Lab 3/3 (positive check: 33 fresh / 10 replay).
  Sonnet review: 6 findings, all fixed (01bee4c). Added: goal-named keys (keys.py, /keys trap 2/2),
  run slots for concurrent jev test, one read retry on IPC timeout, unchecked-stalled-step fails.
  REPORT.md results FILLED. NEXT ideas: re-run A1-S06 with keys; fix drafts' failing checks? (draft
  quality, not Jev); final REPORT polish at 02:30, then CronDelete 538f333e.
- 00:07 Since: A1-S06 re-run with keys 1/3 (only checked test passes); placeholder literals fix;
  jev draft reads linked pages; THIRD APP Moneywell (WIF wif-team17 :5190, stopped) age-gate 3/3 $0.002,
  replay 0 calls. REPORT/README updated. NEXT: landing page + GTM refresh; maybe `draft --verify`.
- 00:22 Since: MCP stdio smoke OK (browser_test -> status, 2/2); replay :id routes + twin ordinals +
  fresh stall clock after replay readback timeout -> core --repeat 3 = 0/0/0 calls; `jev crawl` (GET-only,
  0 calls; StudyCanvas 7 pages ok, 404 detected). Weekly usage 79% at 00:17 (get_usage tool works).
  Sonnet review #2 running on 01bee4c..HEAD. NEXT: apply its findings; final REPORT polish at ~02:15;
  02:30 commit + CronDelete 538f333e.
- 00:44 Since: review #2 (9 findings) all fixed (dc79de8); StudyCanvas tutorial endpoint is rate-limited
  8/hour (don't run SC suites more tonight); action.yml injection fix; --write-suggestions (+ example);
  importer skips modifier clicks. REPORT polished (summary paragraph at top). 489+ tests pass.
  FINAL STEPS (after 02:30): run full checks (ruff, pytest, node x2, uv build), update REPORT test count,
  stop my servers? (SC :8000/:5173, DL :5180, trap :8791 - leave running, note in REPORT), commit,
  CronDelete 538f333e.
- Remaining ideas (older): REPORT.md (morning summary), `jev test --repeat N` flake rate, landing page draft,
  maybe Linux CI action.yml.
- OLD NEXT: MCP browser_test tool (async, beat 120 s cap); second project (Nebius Decision Lab ?mock=1,
  run web on another port); anchors cascade; product docs + landing page; REPORT.md.

## Done so far
- chrome.py dedicated Chrome + tests; expect auto-wait (JEV_EXPECT_WAIT_S=3), text_contains_all,
  no_page_errors (page error recorder installed per document).

## Status
- [x] Branch created, WIP snapshot committed (995fee6), 385 tests pass.
- [ ] Research (landscape, Jev cost audit, Study Canvas checklist digest)
- [ ] Harden unattended runs
- [ ] Test scripts + zero-model replay
- [ ] Study Canvas trial
- [ ] Packaging, docs, REPORT.md
