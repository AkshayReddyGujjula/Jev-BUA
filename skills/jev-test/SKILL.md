---
name: jev-test
description: Test a web app like a real user with plain-English Markdown specs that Jev runs in a real browser - cheap first runs, ~free reruns via recorded replay, verdicts read off the page (never an LLM opinion). Use when asked to "test my app", "check this flow still works", "regression test my change", "find bugs in signup/checkout", "simulate a user", or to verify a UI change on localhost/staging.
---

# Testing a web app with Jev

## 1. Write a spec (Markdown, in the app's repo, e.g. `jev-tests/<area>.md`)

```markdown
# Checkout smoke
base_url: http://localhost:3000
isolate: storage            # wipe the app's storage before each test (sequential, fast)

## Before each               # runs first in every test; recorded once, then replayed free
path: /login
values:
  - Email: test@example.com
  - Password: local-test-password
1. Sign in.
   - see: Dashboard

## Adding to cart updates the badge
path: /products
1. Add the first product to the cart.
   - see: Added to cart
2. Open the cart.
   - see: Subtotal
   - not see: Your cart is empty
   - no page errors
```

Rules that make tests cheap and trustworthy:
- **Steps are goals**, one user intention each ("Create a project named Apollo"), never selectors or code.
- **Every step that matters gets a positive check** (`see:`, `url contains:`, `count:`). A step with
  only `not see:` cannot tell when it has arrived, so it runs until Jev says done or its budget ends.
- Checks: `see: X` (all must appear), `see any: A | B`, `not see: X`, `url contains: X`,
  `url matches: REGEX`, `title contains: X`, `count: CSS >= N`, `value: CSS contains X`, `no page errors`.
- Put literal inputs under `values:` as `- Field label: text`; the label is matched to the field.
  Use local test credentials only; values are never written to replay plans.
- Settings (suite or test level): `base_url`, `path`, `isolate: storage|context`, `max_seconds`,
  `max_steps` (actions per step, default 25), `commit_floor` (owner trust for Save/Delete-style
  clicks on a disposable local app, 0.5-0.9), `check_wait` (seconds the checks keep re-reading,
  default 3; raise it for steps that start slow work such as a model call).
- `## Before all` instead of `## Before each` signs in once per run and the tests start from its
  state: use it when sign-in is slow or rate-limited (tests then run one at a time).
- A step without checks reports suggested `see:` lines from what it changed on screen; adopt the
  right ones (never the ones that were true before the step).

## 2. Run it

- MCP: `browser_test {spec_path: "<abs path>.md"}` returns a `run_id` immediately; poll
  `browser_test_status {run_id}` every 15-30 s. Or pass `spec_markdown` inline (+ `base_url`).
- CLI: `jev test jev-tests/checkout.md [--workers 4] [--fresh]`. Exit 0 only when every test verified.
- The app must already be running. A dedicated automation Chrome is used (no approval prompt, the
  user's own tabs untouched). Replay plans are saved in `jev-tests/.jev/replay/` - commit them.

## 3. Read the result

- `verified: true` only when the page passed every check. `budget_exhausted`/`blocked` with
  `verified: true` still passed: the page is what it is.
- A failure gives `failed_step`, the `failing_checks`, and a `page_excerpt` of what the page showed.
  Decide from the excerpt whether **the app is wrong** (report a bug, with the excerpt) or **the
  spec is wrong** (the app changed on purpose: fix the step or check, say so). Never weaken a check
  just to make a test pass.
- `heals` > 0 means the app changed under a recorded step and Jev re-decided it; the plan was
  re-recorded. Worth a glance when reviewing a UI change.
- Cost is reported per test (`usd`) and per run. A rerun with nothing changed is typically $0.

## 4. Explore for bugs (persona runs)

To simulate a user and hunt for bugs, write a spec whose steps describe what that person tries
("As a first-time visitor on a phone-sized screen, try to sign up with an email that has a typo"),
with checks for what must never happen (`no page errors`, `not see: Something went wrong`). Run it,
read the excerpts of anything that fails, and turn each real bug into a regression test.
