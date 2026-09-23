# Deliberate decisions

Short records for choices a future agent must not "fix" back. Each one is a
deliberate deviation from the obvious alternative.

## ADR-001 — Human checkpoints stop a slice; they do not serialize a task

A delegated task owns its `Agent`, its browser and its tab inside one MCP server
process. `browser_task` runs a bounded slice and returns either a terminal
result or a checkpoint (`needs_human`, `paused`). `browser_task_resume`
continues the same object after a fresh observation, so the login performed in
Chrome survives. A server restart invalidates the task id; the caller starts a
new task instead. Tasks are single-use and process-nonced.

Why not a persisted task state machine: the safety model depends on a live
browser, a consumed-before-mutation decision, and a freshness check; serializing
half of that across processes would weaken all three for little gain.

## ADR-002 — The anti-loop deadline measures an active segment

The five-second no-progress deadline and the eight-round backstop still bound
every active segment, but a human wait rebases the clock (`started_at`,
`progress_at`, `progress_wall_at`, `stall_rounds`). Without this, any login
checkpoint would end the next tick as `stalled`. A page that stops responding
*during* active work still terminates within the usual window.

## ADR-002a — Model waits are not page stalls (2026-09-23)

The five-second deadline measures browser-side time only: every model wait
(decision, text helper, failure classifier, supervisor) is credited back to
`progress_at`. On 2026-09-23 Jev's median decision latency moved from 0.3 s to
1.8 s for about an hour; under the old clock two slow decisions on a responsive
page ended runs as `stalled`, which is a false diagnosis of the page. Model
waits stay bounded by each call's own timeout, by the eight-round cap, and by a
wall-clock backstop (`progress_wall_at`, `JEV_STALL_HARD_SECONDS`, default 20 s)
that nothing is credited against. A model call that cannot finish inside the
backstop is not started, and observations cap their settle wait at the earlier
of the two deadlines.

## ADR-003 — No secret store; the profile holds non-secret data

`~/.jev-ultrafast/profile/` stores identity fields and site usernames as
plaintext. Password, one-time-code, recovery and payment labels are refused even
when present in the JSON, and the human types them into Chrome. A Windows DPAPI
vault is a separate later feature with its own threat model. This is data
minimization, not confidentiality: a site necessarily receives anything typed
into it.

## ADR-004 — Approval is caller-mediated

`approve_action=true` is a promise by the MCP caller that a human approved the
exact proposed action. The server cannot authenticate who answered. Consequences:
use a trusted orchestrator; the manual demo asks in the terminal; label and risk
heuristics catch common committal controls but can miss an obscure or unlabeled
one, so sensitive work should be inspected live.

## ADR-005 — Jev routing is shadow-first and code-gated

`JEV_ROUTER=enforce` can only take effect with at least six active task classes
and at least thirty human-labelled tasks for the current candidate set, with
coverage, accuracy and per-class precision floors. `new_class` always goes to
the text classifier because Jev cannot generate trustworthy metadata. Rationale:
the calibration data for this taxonomy does not exist yet, and an uncalibrated
router would confidently misroute.

## ADR-006 — One delegated task per MCP server instance

A second concurrent task is refused. Parallel work needs separate server
instances with their own `JEV_MCP_BU_NAME`. Multiple tasks in one process would
reintroduce the cross-tab ownership races the repo has already paid for.

## ADR-007 — Verification requires a live readback

`outcome=verified` needs an `expect` readback performed after the task, in one
JavaScript evaluation, so route changes cannot combine facts from two page
states. If the readback fails, the outcome is `unknown`; a `done` choice is never
proof. A harvest count alone cannot verify a page.
