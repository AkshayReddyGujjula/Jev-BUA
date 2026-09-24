# Faster on the real web

## Google Flights after the fundamentals fixes (2026-09-23, night)

Task: Zürich → London, one-way, Tuesday 20 October 2026 (`JEV_FLIGHTS_DATE=2026-10-20`),
`scripts/measure_flights.py`, background tab with the render keepalive, independent
verification after the run. Jev's median decision latency was ~300 ms in these runs.

| Tree | Runs | Verified | Time | Text-helper wait |
| --- | --- | --- | --- | --- |
| before text prefetch (`71cac0b`) | 1 | 1/1 | 16.8 s | 2 × ~1.35 s on the critical path |
| exact-field-set prefetch | 3 | 3/3 | 13.4–15.2 s | 0.8–1.4 s per field |
| covered-field-set prefetch, drafts kept per goal (`2291375`) | 4 | 4/4 | 10.5–12.8 s | 0 ms per field |
| + hedged decisions, 120 s keepalive, connection prewarm (2026-09-24, date 2026-10-20) | 3 | 3/3 | **10.4–11.7 s** | **0 ms** per field |

Decision time on those three runs was 4.6–5.6 s for 12–14 decisions (first decision
329–415 ms, none over 512 ms, no hedge fired), against 6.4–6.8 s for the same 14 decisions
before the keepalive fix (ADR-014).

Where a 10.5 s run goes: 13 Jev decisions (~300 ms each), ~100–270 ms of browser work
before each action and ~130–300 ms of settling after it, and ~2.5 s after Search while the
results page loads before Jev's DONE (`goal_done` 0.94). Upstream's 7.073 s recording ran
at ~200 ms per Jev call with Mercury as the text helper.

### Matched A/B against upstream `452c1ad` (2026-09-24, foreground tabs, display on)

Arms alternated run by run in the same foreground Chrome, same Jev endpoint, cache off,
upstream's text helper pointed at the same DeepSeek endpoint.

| Date in the goal | Upstream `452c1ad` | HEAD |
| --- | --- | --- |
| 2026-10-20 (needs month navigation) | 0/3 - blocked at 12.8, 18.9, 2.4 s | **3/3 verified** - 12.4, 12.9, 12.6 s |
| 2026-09-28 (visible month, like the 7 s demo) | 0/3 - blocked at 16.5, 2.4, 51.2 s | **4/4 done** (readback shown on the last: verified) - 13.2-15.8 s |

Upstream no longer completes the task on today's Google Flights. Its snapshot does not
surface the options of the opened ticket-type menu: after clicking "Change ticket type.
Round trip" the element list is unchanged, so Jev clicks it again (closing the menu), then
sees an empty table and answers BLOCKED; with the date inside the calendar it instead loops on
the preselected day. HEAD's later snapshot and loop changes get through both. Daytime Jev
latency was higher than overnight (~520-650 ms per decision against ~380 ms), which is where
HEAD's 12-16 s here differs from its 10.4-11.7 s overnight background runs. The 7.07 s
recording is therefore not reproducible with either tree today; it remains the target, not
a comparison point.

Local trap closure after the same changes (`scripts/verify_stall.py --runs 2`): every trap
terminates (worst wall 14.3 s against a 25 s cap); the working-form happy path is `done` in
1.6 s.

## Delegated task interface — cold vs warm (local fixture)

Measured 2026-09-23 on the same machine, Chrome, and Jev API, against
`scripts/trap_server.py` (`/trap?mode=simple`: fill a search field, click, land
on the results page). Three alternating pairs; the only difference between the
arms is the decision cache. Every run ended `outcome=verified`; one warm-up run
populated the cache and is excluded from the table.

| Arm | Runs | Verified | Median wall | Median active | Decisions | Cache hits | Jev input tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| cold (`JEV_CACHE=0`) | 3 | 3/3 | 2.021 s | 1.399 s | 3 | 0 | 23,471 (≈$0.00099 total) |
| warm (cache on) | 3 | 3/3 | 0.897 s | 0.507 s | 3 | 3 | 0 ($0) |

Raw runs live in the gitignored `artifacts/delegation-measurement/measurement.json`;
reproduce with
`uv run --env-file .env python scripts/measure_delegation.py --pairs 3`.

Boundaries: `wall_s` starts before Agent construction and includes browser attach
and the first observation; `active_seconds` is the task's own active budget;
verification runs after the timed task and is excluded. One fixture, one goal,
one browser profile — not a general benchmark. The orchestrator's own browser
tool was not part of this comparison, and no v2 comparison was run.

## Historical: Google Flights 7.073 s

> These predate the delegation, profile, router, and renamed package. Do not use
> them as current end-to-end delegation benchmarks.

The historical video completes the Google Flights task in **7.073 seconds at 1×**. It starts with one natural-language goal and uses dynamic controls throughout. Jev selects operation + target in one request; Mercury generates the city strings when TYPE_TEXT is selected.

[Video](demo.mp4) · [Recording measurements](flights-measurement.json) · [Matched run measurements](full-speed-measurement.json)

## Matched runtime comparison

Six alternating runs, one task, one existing Chrome profile. Both arms used the same natural-language goal, independent result checker, 1120×780 viewport, TypeSafe `jev-1.13.0`, `inception/mercury-2.5`, disabled text reasoning, and action/request budgets. Initial navigation is excluded in both arms. Each run creates and closes its own tab. All six attempts are included; no provider or verification failures occurred.

| Pair | Original runtime | Optimized runtime | Verified |
| --- | ---: | ---: | --- |
| 1 | 11.214 s | 6.964 s | Both |
| 2 | 8.984 s | 7.913 s | Both |
| 3 | 9.450 s | 7.092 s | Both |
| **Median** | **9.450 s** | **7.092 s** | **3/3 each** |

The optimized runtime was faster in all three pairs. Median task time was **25.0% lower**, median TypeSafe requests fell **22 → 17**, and median browser protocol calls fell **1,092 → 101**. Three pairs are too few for a strong statistical claim (two-sided sign-test p = 0.25). This is a small controlled-input comparison, not a broad agent benchmark; Google, network responses, routing, and browser caches remain live.

The original arm is the frozen source from `68c077bf79caca4e817b8e8a5854b2efa0c81ff6`. Both arms use Mercury so the runtime comparison does not conflate a helper-model change with code changes. Per-run source hashes, model settings, token counts, helper costs, browser version, protocol counts, and verification results are in the measurement JSON.

## v2 re-baseline status (2026-09-21)

The v2 layer (code-owned outcomes, confidence gates, escape hatches, decision cache, descriptor replay) has **not** been measured against the original arm. The published task date, Sunday, September 20, 2026, is now in the past, and Google's date picker no longer offers it: both attempted matched runs ended blocked/stalled after the calendar offered only September 21 onward (`artifacts/benchmark-flights-v2.json`). Replaying the recorded calendar decision through Jev with and without the new `irreversible` question and escape candidates returns the same answer, so that failure is the task's stale date, not the v2 changes. The example now takes `JEV_FLIGHTS_DATE` for live re-runs.

What v2 **was** measured on:

| Check | Result |
| --- | --- |
| Google Flights, Zurich → London, October 20, 2026, same goal shape | `verified`, 27.930 s, 21 actions, 0 skipped; the search URL's `tfs` encodes `2026-10-20` |
| Local trap closure (`scripts/verify_stall.py`) | every trap terminates; worst recorded stall window 5.0 s against the 5 s limit with a 1 s in-flight allowance (loading traps stop at 4.5–4.6 s); artifact is real, see `artifacts/verify_stall.json` |
| Parallel checks self-test (`--serve-traps`) | 5/5 verified, 3.2 s wall / 12.2 s serial (warm second run); 2026-09-24 with the supervisor on: 5/5, 5.9–7.1 s wall / 14–20 s serial - the inert-control check now ends on the supervisor's `needs_human` (~2.6 s consult) instead of BACK to about:blank |
| Local fixture smoke | `verified`, 3.2–4.0 s, 6 actions, 0–1 skipped, 8–9 decisions |

The safety layer was visible in the loop at the time of these measurements: a reversible action below its confidence floor (then 0.5; 0.9 when Jev marks it irreversible) was never executed, was routed around, and one code-owned reveal scroll could be used when refusals exhausted the visible options. The cost was extra decisions while the model was uncertain. Since 2026-09-23 reversible actions are not gated by default (ADR-008); only the 0.9 commit floor remains.

Replay, measured on the local fixture: the recording run made 9 decisions; the replay run executed 4 of its 5 recorded steps without model calls, handed back to Jev when the offscreen result card did not reproduce, finished at 6 decisions, and still verified. Replay is a fast path, not a guarantee.

The current text-helper configuration is `deepseek-v4.1-flash` (README); the Mercury latencies quoted below belong to the recording's configuration.

## Where the time went

The original loop invalidated decisions on every DOM mutation, including animations. It also read the accessibility tree repeatedly and resolved hundreds of DOM nodes. The new snapshot reads common HTML/ARIA controls in one browser call. Click guards compare the selected target and nearby context, plus document/form state. Current geometry and hit-testing still run before input.

A brief event-based combobox wait lets suggestions arrive before asking Jev to choose from an incomplete popup. Text comes from an actual LLM: the recorded run generated **Zurich in 581 ms** and **London in 346 ms**. Native text replacement was also fixed to issue the browser's select-all command explicitly.

The recording contains **17 Jev requests**, **10 interactions plus one explicit WAIT**, and **two helper calls**. Median Jev latency was **178 ms**. Search executed at **5.217 s**; final verified completion was **7.073 s**. That final interval includes Google's results loading, state changes, and the completion decision. It stays in the video.

Timing begins at the first prediction after initial homepage observation and ends at the accepted DONE choice. It includes text generation, model requests, browser work, stale decisions, and loading. Browser setup, initial navigation, and fresh independent post-run verification are outside the clock. The video contains 186 continuous screencast frames plus the initial screenshot, uses original timestamps, has no opening hold, and adds a 0.5-second final hold. Only the top account/navigation strip is cropped.

The recording reports 90,558 TypeSafe input tokens and 6,325 output tokens across all requests. OpenRouter reported **$0.00006272** for the two text calls. That is the text-helper charge, not total task cost: the TypeSafe responses contain token counts without a billed dollar amount, and browser costs are excluded.

## Other checks

| Task | Time | Independent result |
| --- | ---: | --- |
| Wikipedia: open Gödel’s incompleteness theorems | 2.798 s | Exact article URL |
| Local hotel fixture: search Lisbon, Design, Free cancellation, open Casa Flora | 1.896 s | Property plus all three applied filters |

These are separate smoke checks, not matched speed comparisons. Local browser checks cover moved/replaced/hidden/disabled controls, field and checkbox properties, changed nearby context, overlay blocking, native-select execution, real text replacement, autocomplete arrival, and navigation. Offline tests cover the model contract, stale retries, interrupted mutations, helper validation, and independent trip verification.

After the timed runs, native-select interruption handling was tightened: uncertain mutation results stop instead of being treated as retryable stale reads. Flights does not exercise native SELECT. Its timing and recording hashes are retained unchanged; the final failure path is covered by offline fault injection and local browser checks.

## Development attempts retained

Before freezing the candidate, the original runtime passed once in 9.302 s. Two accessibility-tree/semantic-guard candidates took 9.395 s and 10.157 s. The first direct-DOM candidate took 8.697 s but failed independent verification because name/value extraction was incomplete. Recursive labels and combobox values fixed that failure; subsequent verified diagnostics took 8.051, 8.631, 8.395, 8.385, and 7.741 s. A Mercury diagnostic passed in 7.559 s. These are changed-code development attempts, not the matched comparison above.

A six-call helper probe used the two real flight-field contexts with Gemini 2.5 Flash Lite, Gemini 3.1 Flash Lite, and Mercury 2.5. All returned the correct values in this tiny probe. Mercury then passed the live Flights, Wikipedia, and local filter checks. This does not establish general semantic accuracy. Earlier probes had rejected a model that swapped origin/destination and another that emitted commentary instead of valid JSON.

The previous 11.387-second recording and post-recording 12.898-second policy regression are described in the [original performance report](https://github.com/browser-use/jev-ultrafast/blob/68c077bf79caca4e817b8e8a5854b2efa0c81ff6/docs/performance.md). The older prepared-step prototype remains in [performance-prepared.md](performance-prepared.md). Raw attempts and original-timestamp frames remain in ignored local artifacts.

## Limits

This DOM reader supports common HTML and ARIA controls; it does not implement the full accessible-name algorithm or traverse shadow roots/frames. Scoped click guards deliberately allow unrelated visible updates. Canvas, uploads, new tabs, nested scrolling, and arbitrary keyboard widgets remain unsupported. A valid operation can still be wrong, and DONE is never independent evidence of success.
