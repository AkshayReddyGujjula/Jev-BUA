"""Shared setup for the local delegation demo.

The fixture publishes nothing real, so its mode owner accepts a 0.6 floor for
this class of committal clicks. With the default 0.9 floor the confidence gate
correctly withholds the publish (target confidence lands around 0.86), which is
a good safety demonstration but not a completed task. The floor is user config
in `mode.json`, never model-set, and it is clamped to [0.5, 0.9] by the gate.
"""

import json

DEMO_SLUG = "delegation-demo"
DEMO_GOAL = "After I sign in, name the project and publish the demo site."
DEMO_FLOOR = 0.6


def ensure_demo_mode(goal=DEMO_GOAL):
    """Create the demo mode once and return it; never overwrite an existing one."""
    from AgentJevUltraFast import modes

    root = modes.modes_root()
    existing = modes.load(root, DEMO_SLUG)
    if existing is not None:
        return existing
    modes.create(
        root,
        {
            "durable": True,
            "slug": DEMO_SLUG,
            "name": "Delegation demo",
            "description": "Local fixture: sign in, name a project, publish.",
            "keywords": ["delegation demo", "publish demo site"],
            "strategy": "decide",
        },
        goal,
    )
    path = root / DEMO_SLUG / "mode.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("specialist", {})["irreversible_floor"] = DEMO_FLOOR
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return modes.load(root, DEMO_SLUG)
