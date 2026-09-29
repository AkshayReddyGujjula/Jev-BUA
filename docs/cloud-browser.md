# Browser Use Cloud pilot

This is an optional browser backend for Jev. `jev-cloud` creates one Browser Use Cloud v4 Chrome session, passes its CDP address to the existing Jev decision loop, and stops the session when Jev ends. Jev still chooses from observed, indexed page elements and verifies the result with `--expect`. The hosted Browser Use **Agent** is a separate product; this pilot does not call it.

## Keys and installation

Put each key after the matching name in the repository's Git-ignored `.env` file. [agents.env.example](../agents.env.example) labels their roles. Keep the file private and do not paste keys into chat, CLI arguments, screenshots, or Git:

| Name | Where to get it | What uses it |
| --- | --- | --- |
| `BROWSER_USE_API_KEY` | [Cloud API keys](https://cloud.browser-use.com/settings?tab=api-keys) | Creates and stops the cloud Chrome session. It starts with `bu_`. |
| `TYPESAFE_API_KEY` | Your TypeSafe account | Jev's operation and target choice. |
| `TEXT_MODEL_API_KEY` | Your configured text-model provider | Form text and optional supervision; set the matching `TEXT_MODEL_BASE_URL`, `TEXT_MODEL`, and `TEXT_MODEL_REASONING`. |

Install the optional SDK with `uv sync --extra cloud --group dev`. The `cloud` extra uses `browser-use-sdk` v3 for the Cloud v4 API; it does not install the unrelated open-source `browser-use` agent package. A local Jev run needs no Browser Use key. This checkout also has an ignored `artifacts/cloud-venv` environment, used for the live pilot because the shared `.venv` was locked during sync.

## A bounded run

From this repository, with `.env` filled in:

```powershell
uv run --extra cloud --env-file .env jev-cloud --goal "Open Wikipedia's Python page and read the first paragraph" --url "https://en.wikipedia.org/wiki/Python_(programming_language)" --expect '{"title_contains":"Python"}' --proxy none --timeout-seconds 120
```

On this checkout while the shared `.venv` is locked, use the already installed isolated environment instead: replace `uv run --extra cloud --env-file .env` with `uv run --no-project --env-file .env --python artifacts/cloud-venv/Scripts/python.exe` in that command.

`--proxy none` uses direct traffic. Set a two-letter country, such as `--proxy us`, only when measuring whether a residential proxy helps. `--profile <Cloud profile ID>` can reuse a Cloud-side login profile; this is separate from a local Chrome profile. Do not put a password in the goal. Jev's `status` reports what it did; only a successful independent `--expect` readback makes `outcome: verified`. A `done` without that check is `unknown`.

The launcher gives Chrome a unique Browser Harness name, kills its child at the time limit, and calls the SDK's `browsers.stop(session_id)` in `finally`. Closing the SDK client or the CDP connection alone does **not** end browser billing. Before the child starts, the launcher writes the browser ID, with no key or CDP URL, to ignored `artifacts/cloud/browser.json`. If the stop fails or the launcher is killed, keep that file and stop its session ID manually before any new run:

```powershell
uv run --extra cloud --env-file .env python -c "import json; from pathlib import Path; from browser_use_sdk.v4 import BrowserUse; record = Path('artifacts/cloud/browser.json'); session_id = json.loads(record.read_text(encoding='utf-8'))['session_id']; client = BrowserUse(); client.browsers.stop(session_id); client.close(); record.unlink()"
```

The recovery command reads the ID from the ignored file. Check the dashboard's live sessions if the file is missing after an abnormal exit.

## Trial budget and comparison

Check the [Cloud dashboard](https://cloud.browser-use.com/) balance and keep auto recharge off for a free-credit-only trial. The account spending cap is soft; the launcher has a short timeout and stops each browser, but a process crash can leave a session active until its Cloud timeout. Current [pricing](https://browser-use.com/pricing) lists browser time plus network traffic; residential proxy traffic is more expensive than direct traffic. TypeSafe and the text-model provider remain separate costs.

Compare the same read-only goal and `--expect` on local Jev and Cloud Jev. Record the code-owned outcome, actions, wall time, model calls, and Cloud spend for each. First use a stable public page as a control; then test a public page with login or anti-bot friction. Stop after the free credits or if Cloud does not improve verified outcomes enough to justify its browser and traffic charges. An authenticated test needs a Cloud profile and an explicit, safe test account before it can count as evidence.

### Initial observations (2026-09-29)

| Read-only test | Local Jev | Cloud Jev, direct traffic |
| --- | --- | --- |
| Wikipedia main page → Alan Turing article; `title_contains: Alan Turing` | `verified`, 2 actions, 5.1 s agent time | `verified`, 2 actions, 7.3 s agent time (17.9 s launcher wall time) |
| X landing page → NASA public profile; URL and title assertions | Not run locally | `stalled` / `refuted` on the signed-out login wall; 3 no-progress actions, no profile opened |

The Cloud result proves the CDP integration works. It does not show a speed or accuracy gain on the public control. The X failure is a useful limit of signed-out browsing, not evidence that Cloud's proxy or a saved login profile would succeed. We did not retry the stalled run. After the three short Cloud sessions used during integration, the billing account reported `$14.99812358` in remaining credits and zero active sessions; the figure may update as billing settles.

Sources: [Cloud browser quickstart](https://docs.browser-use.com/cloud/browser/quickstart), [create session](https://docs.browser-use.com/cloud/api-v4/browsers/create-browser-session), [Cloud pricing](https://browser-use.com/pricing).
