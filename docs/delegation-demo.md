# Reproducible delegation demo

This local fixture demonstrates the exact handoff pattern without touching a
real account or publishing anything. It uses real Chrome and the configured
Jev API, so it is a manual demo, not part of `pytest`.

1. Enable Chrome remote debugging at `chrome://inspect` if Browser Harness
   asks, then start the local fixture:

   ```powershell
   uv run python scripts/trap_server.py --port 8791
   ```

2. In another terminal with your own `.env`, run:

   ```powershell
   uv run --env-file .env python scripts/demo_delegation.py
   ```

3. The task should yield `needs_human: login`. Type any nonempty value into
   the fixture's password field in the visible Chrome tab and click **Sign in**.
   Return to the terminal and press Enter. The same task ID and tab continue.

4. The supplied demo project name skips the text helper. Before **Publish demo
   site**, the task should yield a proposed action. Read it, then type `yes`
   in the terminal to approve. The script calls `browser_task_resume`'s
   underlying task method with approval. Any other answer cancels the task.

5. A successful run ends with `phase=complete`, `status=done`, and
   `outcome=verified` because the independent readback finds **Published demo
   site**. Inspect `active_seconds`, `usage`, and the evidence file path in the
   final JSON. A `done` without `verified` is not a pass.

For a portfolio video, record the visible Chrome tab, the terminal's three
checkpoints, and the final JSON at original speed. Report the task's Jev input
tokens and time from that run. Compare at least four alternating cold/warm
runs against the original browser loop and the orchestrator's own browser
tool, using the same fixture and assertions. Save raw runs before publishing
a latency or cost claim. The historical Flights numbers in this repository do
not measure the new task interface.

Known demo limits: the fixture uses a simple same-tab login; OAuth popups and
native passkey dialogs are not covered. The approval answer is obtained by
the manual script; the MCP API itself relies on its caller to relay real user
approval.
