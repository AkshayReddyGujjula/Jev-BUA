# Task-class routing

`--mode auto` first checks a unique keyword match and the stored route for an
identical goal/origin. On a miss, the existing text classifier chooses an
existing mode, a new durable class, or a one-off task. Once there are at least
six active modes, Jev also predicts one of those same candidates with a single
typed choice. `JEV_ROUTER=shadow` is the default: that prediction is recorded
but the text classifier still controls the run. `JEV_ROUTER=off` is the kill
switch and avoids the additional Jev call.

The shadow log is `routing.jsonl` under `JEV_MODES_DIR`. It stores a hash of the
goal plus origin, candidate-set hash, Jev probabilities, and the text
classifier's route. It does **not** store raw goal text. The classifier route
is a comparison, not a correctness label. Add human-checked labels to
`routing_labels.jsonl`, one JSON object per task:

```json
{"task_key":"<task_key copied from routing.jsonl>","expected":"internship-applications"}
```

`expected` can be an existing mode slug, `new_class`, or `one_off`. A repeated
task key uses its latest label. Labels apply only to the candidate set that
was logged for that run; changing mode names or descriptions resets the gate.

`JEV_ROUTER=enforce` can take effect only with at least six active modes and
30 independently labelled tasks for the current candidate set. At least 70%
of those predictions must clear the code-owned margin (0.20) and confidence
(0.75) floors; accuracy among them must be at least 95%, and every class Jev
chose must have at least three labelled examples with 90% precision. If any
gate fails, the text classifier still decides. Jev can select an existing
mode or `one_off` when the gate passes; `new_class` always goes to the text
classifier because Jev cannot generate trustworthy metadata. Retired modes
are excluded, and `allowed_origins` in a mode's `mode.json` can restrict where
its choice can be enforced.

These thresholds prevent a small or self-labelled shadow log from granting
authority. They are release gates, not a claim that current routing has met
them. Every Jev choice is validated against the offered candidates. Browser
actions still use the ordinary observed-element and outcome gates.
