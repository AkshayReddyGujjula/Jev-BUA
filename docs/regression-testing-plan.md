# Regression-testing fix plan (2026-11-04)

From a read-only code review. Nothing here is implemented yet. Items 1-6 and 8 are approved to implement; 7 is deferred.

1. **Fail on heals** (S). A repaired step never fails a test today: `checks.py` counts heals but `verified` ignores them. Add a fail-on-heal setting, keep healed plans pending until they are accepted, and report heals clearly.
2. **Isolate tests by default** (S). Tests share cookies and storage because `JEV_ISOLATE` defaults to `"0"` (`checks.py:236`). Make per-test isolation the default, keep an opt-out, and document it.
3. **Lint weak checks** (S). Negative-only steps are accepted, and a positive check that was already true before the step passes. Require a positive check on state-changing steps, and flag any check that is already true before the step runs.
4. **Type into password fields** (M). `snapshot.js:22` drops password and file inputs, yet the README login example depends on them. Confirm this live first, then add a code-owned fill for values the user supplied. The value must never reach a model, the logs or a replay plan.
5. **Reports and artifacts** (S-M). Add JUnit XML, a failure screenshot plus DOM text, `--grep`, and `--retries` that marks a test flaky instead of hiding it. Test `action.yml` on a real app.
6. **Normalize volatile labels** (S-M). Replay matches exact labels, so counts and timestamps break it (e.g. "Show activity ( 3 )"). Strip them at record time and at match time.
7. **Saved login state and network stubs** (M-L, deferred). Save and restore per-suite storage state, and add CDP Fetch stubs for third-party calls.
8. **Export to Playwright** (M). `jev export <spec>` turns verified replay plans into readable Playwright `.spec.ts`, so suites can run in CI with no Jev or model. In current tools the model writes and repairs tests, and a plain script runs them.

Suggested order: 1, 2, 3, 6, 4, 5, 8.
