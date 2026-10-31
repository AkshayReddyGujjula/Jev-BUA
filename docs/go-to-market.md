# Go-to-market brief (draft, 30 Oct 2026)

Research brief written overnight for the decision on packaging, naming, pricing and launch.
Prices are late-Oct-2026 figures; most vendors don't publish prices, so many come from third-party
aggregators and are marked (unverified). Measured numbers are from `examples/studycanvas/core.md`
and `examples/decision-lab/smoke.md` runs recorded in `REPORT.md`.

## 1. Competitive landscape

| Product | Pricing | Target | How it differs |
|---|---|---|---|
| **Momentic** | Free tier, then sales; "from $250/mo" (unverified) | Eng teams | YAML specs + AI step cache (replays locators with no AI on a hit). AI check/extract steps are never cached and can be LLM verdicts. Closest competitor. [docs](https://momentic.ai/docs/reliability/step-cache) |
| **QA Wolf** | Managed, ~$40-44/test/month, ~$8k/mo min (unverified) | Mid-market/enterprise | Humans + AI write and maintain Playwright code. [bug0](https://bug0.com/knowledge-base/qa-wolf-pricing) |
| **Octomind** | Was $89 / $589 per month | SMB devs | **Shut down mid-2026** — hosted AI-QA at mid prices is hard to sustain. |
| **Checksum** | Per maintained workflow, custom (unverified) | Scale-ups | Generates Playwright from real sessions, auto-heals. |
| **mabl** | ~$500/mo starter, $40k+/yr enterprise (unverified) | Enterprise QA | Low-code recorder, cloud credits. |
| **Autify** | ~$120-300/mo (unverified) | QA teams | AI tester + Playwright platform. |
| **Reflect (SmartBear)** | ~$200-500/mo (unverified) | QA teams | No-code recorder with AI steps. |
| **TestSprite** | Credits: free 150/mo, $19, $69 | Devs in AI IDEs, via MCP | MCP-first: the most direct channel overlap. Hosted, opaque per-credit cost. |
| **Meticulous** | Quote only | Frontend teams | Session recording + visual diffs, no authored specs. |
| **Playwright test agents + MCP** | Free OSS + frontier-model tokens | Every developer | Planner/generator/healer write Playwright code; a frontier model runs every planning and healing step. **Biggest threat: free and the default.** |
| **Stagehand / Browserbase** | OSS SDK; hosting $20-99+ (unverified) | Agent builders | General automation SDK with action caching, not a test runner. |
| **browser-use Cloud** | ~$0.006/step + $0.01/task | Agent builders | General agent, LLM per step. |
| **Skyvern** | $29 / $149 credits (unverified) | RPA | Vision agent, not for testing. |
| **Shortest** | OSS + Claude tokens every run | Developers | Plain-English tests, but every run calls Claude and results vary. |

Cache-and-replay alone is not unique (Momentic, Stagehand). The combination is: Markdown specs,
verdicts that are deterministic page readbacks (never LLM opinions), a first run that is also
cheap (a classifier at fractions of a cent per decision), local-first and agent-native.

## 2. Positioning

**Pitch:** plain-English browser tests your coding agent can run, verified on the real page, at about
a penny for a whole suite and about $0 on every rerun.

1. **Verdicts are facts, not opinions.** Every check (`see:`, `not see:`, `url contains:`, `no page
   errors`) is read off the real page; `verified` only when a readback passed.
2. **Cheap enough to run on every change.** StudyCanvas, 5 tests each with an access gate and a
   5-question onboarding: first run $0.012 (55 model calls, 39 of them signing in), third run $0 (0 calls), 5/5.
3. **Made for your coding agent.** MCP (`browser_test`), CLI, specs in the repo, replay plans in
   `.jev/` committed so CI reruns cost almost nothing; a broken step heals with one decision.

**For:** solo devs and small teams shipping web apps with Claude Code / Cursor / Codex.
**Not for:** managed QA (QA Wolf), pixel-diff regression (Meticulous), native mobile.
**Wedge:** "test my app" through Playwright MCP burns frontier tokens and gives a different answer
each time; here it is one MCP line, five bullets in a `.md`, and a deterministic verdict that is free
to rerun.

## 3. Packaging and distribution

Ship one Python package that is both the CLI and the MCP server, installed with `uvx`:

```bash
uvx <name> init                      # writes jev-tests/smoke.md
uvx <name> test jev-tests/smoke.md   # local run, records .jev/ replay plans
claude mcp add <name> -- uvx --from <name> <name>-mcp
```

Then, in order: a Claude Code plugin/skill that teaches the spec format; a GitHub Action that replays
committed plans and comments on the PR (the paid bridge); a hosted runner + dashboard; VS Code last.

## 4. Pricing (open core)

Local CLI/MCP/replay free (BYO keys, or a small included quota through a key proxy). Paid = hosted
runs, CI orchestration, dashboard.

| Tier | Price | Includes |
|---|---|---|
| Local | $0, OSS | Unlimited local/self-hosted runs |
| Pro | $19/mo | 5,000 hosted test-runs, GitHub Action PR comments, 30-day history |
| Team | $99/mo | 50,000 runs, 10 parallel, flake/heal analytics, 5 seats |
| Scale | from $499/mo | SSO, private runner, SLA |

Unit economics (measured model cost, final runs on 30-31 Oct): first authoring run $0.0007-0.004
per test (StudyCanvas $0.0024/test including its sign-in; 23 unreviewed checklist drafts $0.004/test;
a game's age gate $0.0007/test), replay $0-0.0007/test (most suites 0 model calls by the third run).
Hosted Chrome at 3-6 s per test after a one-time sign-in is roughly $0.0001-0.0003 of compute
(estimate). Gross margin >90% at these tiers. **Key risk:** Jev is TypeSafe's model — get a written volume/reseller agreement first.

## 5. Naming (not trademark-searched)

**Rehearse** (recommended; record, replay, rehearsal; `rehearse test`), Shakedown, Dryrun, Walkspec,
Proofwalk, Smokejump, Tripcheck. Avoid: Plaintest, Retest, SpecFlow (existing products). Keep
"powered by Jev" as the ingredient brand.

## 6. Launch plan

- **Landing page:** hero "Your app, tested in plain English. Verified on the real page. Reruns cost
  $0." Proof numbers (above), a real-speed demo (agent writes `smoke.md`, calls `browser_test`, Chrome
  clicks through, cost meter; rerun stays at ~$0.0000), comparison strip, `uvx` CTA.
- **First-run hook:** `uvx <name> crawl http://localhost:3000` needs no spec, no key and no model:
  it opens every page the app links to and lists JS errors and broken pages in seconds. A free
  "found a bug in 5 seconds" moment, then `draft` and `test` take over.
- **Channels:** MCP registries + Claude Code plugin listing first; Show HN ("Plain-English E2E tests
  where the verdict is never an LLM opinion"); X GIF thread; Reddit (r/ClaudeAI, r/cursor,
  r/webdev); Product Hunt after the GitHub Action exists.
- **Before charging:** zero-config install (PyPI name, headless Linux Chrome, no prompts, bundled
  keys); GitHub Action with PR comments and failure traces; safe auth/secrets; public reliability
  evidence across 5+ OSS apps and a head-to-head vs Playwright agents; hosted runner + metering.
