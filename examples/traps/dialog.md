# Native dialog trap

Self-test for the dialog policy: `python scripts/trap_server.py` (port 8791) serves `/dialog`, where
Delete sits behind a native `confirm()`.

base_url: http://127.0.0.1:8791

## A delete behind a confirm dialog completes
path: /dialog

1. Delete the quarterly report.
   - see: Item deleted
   - not see: Quarterly report
