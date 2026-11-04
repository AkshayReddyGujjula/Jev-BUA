---
name: jev-bua
description: Explicit-invoke only (/jev-bua in Claude Code and OpenCode, $jev-bua in Codex). Do the user's request in a real Chrome with Jev (Jev Browser Use Agent), through the `jev` CLI, and test web apps with Jev's spec/replay/crawl tools. Never load or use this unless the user invoked it by name.
disable-model-invocation: true
allowed-tools: Bash(jev *) Bash(BU_NAME=jev-bua jev *)
---

# jev-bua - Jev Browser Use Agent (CLI only)

Jev drives the user's real Chrome from one natural-language goal. Repo:
`C:\Users\aksha\Desktop\GitHub\JEV\My Projects\Jev-BUA`.

**Explicit-invoke rule.** Use Jev only because the user invoked this skill. For that request, Jev is
the browser. Never use it for requests that did not invoke it; those use the host's own browser.
Jev's MCP server is intentionally not registered; use the CLI.

## Fast path: do something in the browser

Do not plan, rewrite, split or choose tools. Jev decides each step itself in about 0.3 s, so thinking
first is pure latency. Run one command with the user's words verbatim:

```
BU_NAME=jev-bua jev --brief --tab background "<the user's request, verbatim>"
```

PowerShell: `$env:BU_NAME='jev-bua'; jev --brief --tab background "<request>"`

- `BU_NAME` gives Jev its own Browser Harness daemon, so it never shares the default daemon (and its
  tab) with other tools.
- Tab, from the wording: "show me" / "let me watch" -> `--tab foreground`; "this tab" / "my active tab"
  -> `--tab active` (and no `--url`); otherwise `--tab background`. (Plain `jev` defaults to
  foreground; always pass `--tab` explicitly.)
- `--url <url>` only if the user gave a URL. Otherwise the start page is derived from the goal
  ("open youtube, ...") by code, never a model. If the goal names no site, the CLI errors; ask the user.
- `--values '<json>'` (or `@file.json`) for literal text the user supplied, as `{"Field label": "text"}`.
- `--expect '<json>'` whenever success can be read off the page, e.g.
  `{"url_contains": "/checkout", "text_contains": ["Order placed"], "text_absent": ["Error"]}`.
  Keys: `url_contains`, `url_matches`, `title_contains`, `text_contains`, `text_absent`,
  `selector_count` (`{selector, at_least}`), `value_contains` (`{selector, text}`),
  `harvest_at_least` (pair with a page check). Without `--expect` the outcome is `unknown`.
- `--find "<literal>"` to scroll a long list to a title or row name. `--keep-open` leaves the tab.
- Runs can last minutes (`--max-seconds` default 900, `--max-steps` 500): use a long Bash timeout
  (e.g. 600000 ms) or run it as a background shell and wait for it.

## Reading the result

Stdout is JSON: `{tab_mode, result, trail}`, plus `error` when the run failed. `--brief` keeps only the
last trail entry. `result` holds `status` (what Jev did: done, blocked, stalled...), `outcome`
(code-owned verdict), `checks`, `url`, `title`, `actions`, `history`. Live steps stream on stderr.

- Only `outcome: verified` is proof, and only an `--expect` readback produces it.
- `status: done` with `outcome: unknown` exits 0 but is NOT success: tell the user it is unverified.
- `refuted`: Jev said done but the page disagrees; the page wins. Report it as failed.
- Exit codes: `0` only for `status: done` (any outcome except refuted); `2` for blocked, stalled,
  refuted, step/time budget; `1` for an exception. Non-zero is not a crash: read the JSON.
- `stalled`: the page stopped responding to everything tried. Do not retry. Never re-run a goal that
  may already have acted (no mutation retries): inspect the browser first, then ask the user.

## Testing a web app

Preferred over ad-hoc runs. `jev test`, `jev draft` and `jev crawl` use a dedicated automation Chrome
(no approval prompt, the user's tabs untouched; headless only if `JEV_CHROME_HEADLESS=1`). The app
must already be running.

- `jev test <spec.md> [--workers N] [--fresh] [--no-replay] [--repeat N] [--chrome dedicated|user]`
  runs a Markdown spec. First runs use Jev; verified flows are recorded and replayed with zero model
  calls; it heals one step at a time and reports cost per test. Exit 0 only when every test verified.
  Replay plans live in `<spec dir>/.jev/replay/<spec-stem>/` (commit them); `.jev/evidence` and
  `.jev/reports` are ignored.
- Spec format and rules: read `C:\Users\aksha\Desktop\GitHub\JEV\My Projects\Jev-BUA\skills\jev-test\SKILL.md`
  (do not duplicate it here). `jev init [--url U] [--dir D]` writes a starter spec;
  `jev draft <url> [--about "..."] [--out F]` drafts a spec from the running app (review it).
- `jev crawl <url> [--max-pages N]` sweeps same-origin pages for JS errors and broken pages with
  no model calls (GET only; sign-out/delete links are never followed).
- Parallel checks (up to 10 Chrome instances), run from the repo root:
  `uv run --env-file .env python scripts/run_checks.py <checks.json> --workers 6 --chrome dedicated`
  (without `--chrome dedicated` each worker attaches to the user's real Chrome and may prompt).
  Template: `checks.example.json`. Assertions in `expect` are the verdict, not the agent's opinion.

## Prerequisites and failures

- Keys come from `Jev-BUA\.env` automatically (global `jev` is an editable uv-tool install of the
  repo). Never print, copy or commit `.env`; keep credentials out of goals and logs.
- Real Chrome (default) needs "Allow remote debugging" approved once at `chrome://inspect`
  (per `BU_NAME`). If Jev cannot reach Chrome or is waiting on that approval, STOP and tell the user.
  Inside a /jev-bua request do not silently switch to another browser.
- Limits: no iframes, shadow DOM, canvas, file uploads or new-tab flows. Do not use Jev just to read
  or summarise a page. Do not commit or push repo changes unless the user asks.
