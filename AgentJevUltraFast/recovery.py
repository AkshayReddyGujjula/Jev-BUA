"""Last-resort recovery: a vision model proposes one bounded action from a screenshot.

Used only when the loop is demonstrably stuck (repeat-no-op recovery and the stuck
watcher did not help). The proposal vocabulary is closed on purpose: the model can
choose a place to click, a key to press, a scroll, or back — never free-form input.
"""

import json
import os
import time

import httpx

from .questions import RECOVERY_PLANNER

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"
# This call sits inside the loop's anti-loop deadline (JEV_STALL_SECONDS, default 5 s),
# so it must finish well inside that window. The original 45 s allowed one recovery
# attempt to overshoot the whole guarantee nine times over. Keep this below the stall
# limit; a planner that cannot answer in time is worse than no planner.
PLANNER_TIMEOUT = float(os.environ.get("JEV_ESCALATION_TIMEOUT", "4"))

# The closed key vocabulary a recovery plan may use.
KEYS = {
    "escape": {"key": "Escape", "code": "Escape", "vk": 27},
    "enter": {"key": "Enter", "code": "Enter", "vk": 13},
    "space": {"key": " ", "code": "Space", "vk": 32},
    "tab": {"key": "Tab", "code": "Tab", "vk": 9},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "vk": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "vk": 39},
    "pageup": {"key": "PageUp", "code": "PageUp", "vk": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "vk": 34},
    "f": {"key": "f", "code": "KeyF", "vk": 70},
    "c": {"key": "c", "code": "KeyC", "vk": 67},
    "k": {"key": "k", "code": "KeyK", "vk": 75},
    "m": {"key": "m", "code": "KeyM", "vk": 77},
    "j": {"key": "j", "code": "KeyJ", "vk": 74},
    "l": {"key": "l", "code": "KeyL", "vk": 76},
}


def _settings():
    base = os.environ.get("ESCALATION_MODEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    key = os.environ.get("ESCALATION_MODEL_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY")
    model = os.environ.get("ESCALATION_MODEL", DEFAULT_MODEL)
    return base, key, model


def _plan(screenshot, state, goal, history, timeout=None):
    base, key, model = _settings()
    if not key:
        return None, 0
    payload = {
        "model": model,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": RECOVERY_PLANNER},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"goal": goal, "page": state, "recent_actions": history[-6:]}),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot}"}},
                ],
            },
        ],
    }
    started = time.perf_counter()
    response = httpx.post(
        base + "/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "x-opencode-session": os.environ.get("JEV_TEXT_SESSION", "jev-escalation"),
            "User-Agent": "AgentJevUltraFast/0.1",
        },
        timeout=timeout or PLANNER_TIMEOUT,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content), round((time.perf_counter() - started) * 1000)


def propose_action(page, goal, history, screenshot=None, timeout=None):
    """One bounded recovery action from a vision model, or None. Never raises."""
    if not screenshot:
        return None
    try:
        plan, latency = _plan(
            screenshot,
            {
                "url": page.get("url"),
                "title": page.get("title"),
                "text": (page.get("text") or "")[:1500],
                "viewport": {"width": page.get("w"), "height": page.get("h")},
            },
            goal,
            history,
            timeout=timeout,
        )
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    kind = str(plan.get("action", "")).lower()
    reason = str(plan.get("reason", ""))[:160] or "escalation"
    width, height = page.get("w") or 0, page.get("h") or 0
    if kind == "click_xy":
        try:
            x, y = float(plan["x"]), float(plan["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= x <= 1 and 0 <= y <= 1 and width and height):
            return None
        return {
            "action": {"kind": "click_at", "x": round(x * width), "y": round(y * height), "label": "Recovery click"},
            "label": f"Recovery: click at {x:.2f},{y:.2f}",
            "latency_ms": latency,
            "reason": reason,
        }
    if kind == "press_key":
        spec = KEYS.get(str(plan.get("key", "")).lower())
        if not spec:
            return None
        return {
            "action": {"kind": "key", **spec, "label": f"Recovery key {spec['key']}"},
            "label": f"Recovery: press {spec['key']}",
            "latency_ms": latency,
            "reason": reason,
        }
    if kind == "scroll":
        delta = 560 if str(plan.get("direction", "down")).lower() == "down" else -560
        return {
            "action": {"kind": "scroll", "delta": delta, "label": "Recovery scroll"},
            "label": "Recovery: scroll",
            "latency_ms": latency,
            "reason": reason,
        }
    if kind == "back":
        return {
            "action": {"kind": "back", "label": "Recovery back"},
            "label": "Recovery: back",
            "latency_ms": latency,
            "reason": reason,
        }
    return None
