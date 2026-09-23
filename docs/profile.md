# Local profile for delegated tasks

`browser_task` reads `~/.jev-ultrafast/profile/` (override with
`JEV_PROFILE_DIR` for an isolated test or another account). Create the folder
yourself and put only values you are comfortable keeping as plaintext on this
computer. It is outside the Git repository. No profile file is required to run
the browser.

`identity.json` has fields that may be used on any approved web origin:

```json
{
  "fields": {
    "Full name": "Your Name",
    "Email": "you@example.com",
    "Phone number": "+44 0000 000000"
  }
}
```

`accounts.json` holds site-specific **usernames only**, keyed by an exact web
origin (scheme, host, optional nondefault port):

```json
{
  "https://example.com": {
    "fields": {"Username": "your-account-name"}
  }
}
```

The resolver matches a form's normalized label exactly and only when that label
identifies one fillable control. If the identity and site account files give
different values for the same label, it skips that field. A caller's explicit
`values` take priority. Password, one-time-code, recovery, payment and other
sensitive labels are ignored even if present in JSON. The human enters login
secrets in Chrome.

On the first matching field at a new origin, the task pauses with
`needs_human: confirm_fill`. The caller must show the proposed field and origin
and get the user's approval before resuming with `approve_action=true`.
The browser writes approved origins to `approvals.json`; later matching fields
on that origin fill without another prompt. A changed page invalidates the
pending approval. Delete an origin from `approvals.json` to require approval
again. `profile:false` disables profile use for one delegated task.

Profile literals are kept out of Jev and text-model page snapshots, returned
traces and screenshots. Browser snapshots mask known values before crossing
from Chrome into Python; history and text-call records use redacted text.
Exact-label mode learning and replay do not persist profile fills. This is
best-effort data minimization, not an encrypted vault: the plaintext JSON
files remain readable to your Windows account, a site necessarily receives
any value typed into its form, and a site may transform or echo that value in
a way literal masking cannot recognise. Password storage with Windows DPAPI
is planned separately and is not implemented here.
