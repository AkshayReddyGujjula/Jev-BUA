# Modes: task-class memory and specialists

A **mode** is a named task class for the browser agent — "internship applications",
"social scrolling" — stored as a folder outside the repository. A mode gives the agent
three things a general run does not have:

1. **Memory** the user owns and edits (personal facts, standing instructions, interests),
2. **Learned field values** reused across runs,
3. **A specialist configuration**: which models answer, how long they may take, how many
   steps the run gets, and which strategy drives the loop (`decide` or `collect`).

A mode is created automatically the first time a task does not match an existing one, by
the text model (`TEXT_MODEL`, currently `deepseek-v4.1-flash`). The creation is visible:
it is printed, recorded in evidence, and written into the mode's own `AGENTS.md`.

Vocabulary is deliberate. The knowledge base already uses "mode" for tab behaviour
(`JEV_TAB_MODE`) and, in prior art, "profile" is browser state while "memory" is facts.
Here **mode** is the user-facing name the operator asked for; internally it is a
profile-shaped namespace that holds memory + config + workspace. Code distinguishes
`tab_mode` from `mode` everywhere.

## Folder layout

Root: `JEV_MODES_DIR`, default `~/.jev-ultrafast/modes` (never inside a git repo — mode
files hold personal facts).

```
<root>/<slug>/
  mode.json        machine config: identity, keywords, strategy, specialist knobs
  AGENTS.md        human-editable memory: about me / standing instructions / interests
  learned.jsonl    machine-learned field label -> value, append-only, last write wins
  harvest/         collected items (collect strategy), one JSONL file per day
    seen.json      dedupe index of harvested titles
  replay/          per-mode descriptor replay plans
```

`mode.json`:

```json
{
  "version": 1,
  "slug": "internship-applications",
  "name": "Internship applications",
  "description": "Filling and tracking internship application forms.",
  "keywords": ["internship", "application", "apply", "cover letter", "graduate scheme"],
  "strategy": "decide",
  "created_at": "2026-09-21T12:00:00+00:00",
  "created_from": "Apply to the software engineering internship at ...",
  "specialist": {
    "text_model": null, "text_base_url": null,
    "decide_timeout_s": null, "max_steps": null, "screenshots": null,
    "cache": true, "replay": true,
    "collect": {"steps": 8, "harvest": false, "instructions": null}
  }
}
```

All `null` means "use the environment default". A corrupted or unreadable mode file makes
the mode absent for matching, never raises into a run.

## Resolution and automatic creation

`modes.resolve(goal, url)` order:

1. **Explicit**: `Agent(mode="<slug>")` / `--mode <slug>` loads it or fails loudly.
2. **Keyword match** against every mode's `keywords` (case-insensitive substring of
   `goal + " " + url`). Highest score wins; a tie goes to the classifier.
3. **Routes cache**: `<root>/routes.json` remembers `sha1(goal|url) -> slug|"general"`.
   `general` entries expire after 24 h so a later, similar task can still become a mode.
4. **Classifier**: the text model receives the goal, the URL and the existing modes and
   returns either `{"durable": true, "slug", "name", "description", "keywords",
   "strategy"}` or `{"durable": false}`. Invalid output means **no mode** — never a
   guessed one. `durable` is the model's own judgement of whether this is a task class
   worth remembering; a one-off errand is not.
5. **Creation**: folder + `mode.json` + empty `AGENTS.md` skeleton + `learned.jsonl` +
   `harvest/`; the slug is sanitized to `[a-z0-9-]{1,40}` and de-duplicated with a `-2`
   suffix. `created_from` records the goal that caused it.

Auto-creation is opt-in per call: `--mode auto`, `JEV_MODES=auto`, or MCP
`mode: "auto"`. Library default is off, so tests and benchmarks stay deterministic.
Every benchmark script forces `JEV_MODES=off`.

Slugs from the model are untrusted: they are sanitized, never used as paths directly, and
`load()` rejects anything outside the slug pattern. Reads and writes stay inside the root.

## Memory injection

Three consumers, three bounds:

- **Decision request** (`model.choose`) gets `state.mode = {name, memory, instructions}`.
  Memory is `AGENTS.md` with HTML comments stripped, capped at 1,500 characters.
- **Text helper** (`model.field_text`) gets the same memory plus the mode's known field
  values for this label or a related one, capped at 3,500 characters total.
- **Fast path** (no model call): an exact normalized-label hit in `learned.jsonl` is typed
  directly. Normalization strips case, whitespace, a trailing `*` or `:` and
  `(required)`. If several labels match, the longest key wins; a tie falls back to the
  text model rather than guessing (same rule as caller-supplied `values`).

Caller-supplied `values` outrank learned memory, which outranks the text model.

**Learning**: after a `TYPE_TEXT` action executes and its outcome is not a failure, the
mode records `{label, value, url, at}`. Learning never happens for sensitive labels
(password, passcode, OTP, CVV, card, security code, SSN) and never for replay runs.

**Redaction**: sensitive labels are neither learned nor injected. Mode files are local and
outside git, but they are plain text by design — the user can read and delete them, and
the docs say plainly that memory is sent to the configured model providers on each run.

## Specialists

`specialist` values override environment defaults for one mode:

| key | effect |
| --- | --- |
| `text_model`, `text_base_url` | which helper writes field values |
| `decide_timeout_s` | upper bound for one Jev decision (never above `JEV_DECIDE_TIMEOUT`) |
| `max_steps` | per-run action budget (never above `--max-steps`) |
| `screenshots` | default for recording frames |
| `cache` | decision cache on/off for this mode |
| `replay` | per-mode replay plans on/off |
| `collect.steps` | collector scrolls |
| `collect.harvest` | whether the collector stores items |
| `collect.instructions` | extra picking rules for the harvest model |

A "fast" mode (social scrolling) sets a short decide timeout, no cache, and the `collect`
strategy. A "careful" mode (internship) keeps defaults, cache, replay, and full budgets.

## The collect strategy

When `strategy == "collect"`, `tick` runs a code-owned loop instead of a decision per
step. Each tick:

1. stops if the anti-loop deadline passed, the step cap is reached, or two consecutive
   scrolls produced no new page state (feed exhausted);
2. harvests the current page at most once per fingerprint, when enabled:
   the text model receives the visible page text (6,000 chars), the mode's interests
   memory and picking instructions, and must return strict JSON
   `{"items": [{"title", "note"}]}` (1-10 items). Invalid output stores nothing;
3. scrolls down using the page's own offered scroll action. **The collector never clicks
   anything** — no likes, reposts, or follows — and that is a code property, not a rule
   the model could violate.

New items are appended to `harvest/YYYY-MM-DD.jsonl`, deduplicated by normalized title
against `harvest/seen.json` (cap 5,000). The run ends `done`; the verdict still comes from
the same code-owned readback. `expect` gains `harvest_at_least: N` so the collector's
output itself can verify a run, alongside the usual page checks.

Collector bounds: `steps` (default 8), two barren scrolls, one harvest per fingerprint,
harvest calls bounded by the remaining anti-loop room like every other model call. A feed
that stops producing new state stops the run within the same 5-second guarantee as the
decision loop.

## Safety rules

- Modes never bypass the v2 gates: low-confidence mutations are still refused,
  irreversible actions still abstain on a second low reading, submissions are never
  retried.
- The collector dispatches only scroll actions.
- No credentials are stored, learned, or injected. `AGENTS.md` is for facts a form would
  ask for, not for secrets.
- The mode root is outside the repository; `modes/` is also gitignored where a user keeps
  modes in-tree. Nothing personal is committed.
- A mode can disable the cache, but cannot widen the cache key or cache irreversible
  actions.
- Auto-created namespaces are visible: CLI output, evidence (`mode_resolved`,
  `mode_created`), and the mode's own `AGENTS.md` all name the goal that created them.

## Testing

Offline (no paid APIs, `JEV_MODES_DIR` pointed at tmp):

- slug sanitizing and path containment; corrupt mode files degrade to absent
- create / load / list / keyword match / routes cache / classifier stubs
  (durable, not-durable, malformed, slug collision)
- memory text stripping and caps; sensitive labels never learned nor injected
- learned fast path avoids the text model; caller `values` outrank memory; failures are
  not learned
- harvest strict-JSON validation, dedupe, per-day file, `harvest_at_least`
- collect loop against a fake browser: scroll-only, once-per-fingerprint harvest,
  barren stop, done + verified via `harvest_at_least`
- mode context appears in the decision request and the text-helper context
- CLI/MCP flag plumbing; benchmark scripts force `JEV_MODES=off`

Live:

- **Internship applications** (local fixture, fake persona): auto-create the mode from a
  goal, seed memory, fill a multi-field form (dry run verified by a visible "ready"
  state), second run reuses learned values with measurably fewer text calls, third run
  submits and is verified by the receipt page.
- **Social scrolling** (live `x.com/home`, read-only): auto-create a collect mode, seed
  interests, scroll and harvest; verified by `harvest_at_least` plus page text. If X
  gates the run behind sign-in, that is reported as blocked and the deterministic local
  feed fixture proves the collector instead.
- Regressions: full offline suite, smoke, parallel checks, trap verifier.

## Non-goals

- No site-specific selectors or hardcoded field values anywhere in the agent.
- No cross-device sync, encryption, or credential vault in this version; files are local
  plain text with redaction and a documented deletion path.
- No silent creation: a mode folder is never created without the classifier's explicit
  `durable: true` or a user's `--mode`.
