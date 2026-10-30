"""Keys the goal itself names ("press ?", "Ctrl+K", "use ArrowDown"), offered as key actions.

The page only advertises Escape, Enter and its declared aria-keyshortcuts, so a test that says
"press ? to open the shortcuts sheet" or "move with ArrowDown" had no way to do it. A key named in
the goal is the caller's own instruction, parsed here by code (the same combo grammar as
snapshot.js); the model never invents a key, it can only choose one of these like any other
observed action.
"""

import re

NAMED = {"enter": ("Enter", 13), "return": ("Enter", 13), "escape": ("Escape", 27), "esc": ("Escape", 27),
         "tab": ("Tab", 9), "space": (" ", 32), "spacebar": (" ", 32), "backspace": ("Backspace", 8),
         "delete": ("Delete", 46), "home": ("Home", 36), "end": ("End", 35), "pageup": ("PageUp", 33),
         "pagedown": ("PageDown", 34), "arrowleft": ("ArrowLeft", 37), "arrowup": ("ArrowUp", 38),
         "arrowright": ("ArrowRight", 39), "arrowdown": ("ArrowDown", 40)}
CODES = {" ": "Space"}
PUNCT = {"/": (191, "Slash"), "?": (191, "Slash"), ".": (190, "Period"), ">": (190, "Period"),
         ",": (188, "Comma"), "<": (188, "Comma"), ";": (186, "Semicolon"), "=": (187, "Equal"),
         "-": (189, "Minus"), "[": (219, "BracketLeft"), "]": (221, "BracketRight"), "'": (222, "Quote")}
BITS = {"alt": 1, "option": 1, "ctrl": 2, "control": 2, "cmd": 4, "command": 4, "meta": 4, "shift": 8}
MODS = r"(?:ctrl|control|alt|option|shift|cmd|command|meta)"
COMBO = re.compile(rf"\b{MODS}(?:\s*\+\s*{MODS})*\s*\+\s*[^\s,;)\"”'`]+", re.IGNORECASE)
# Words such as Home, End, Tab, Delete or Enter are ordinary English ("On Home", "End the tutorial",
# "Delete the canvas"): a named key counts only as "press X" (PRESS) or "the X key". Arrow and page
# keys are unambiguous on their own.
NAMED_WORD = re.compile(r"\b(?:arrow\s*(?:up|down|left|right)|page\s*(?:up|down))\b|"
                        r"\b(?:tab|space(?:bar)?|backspace|esc(?:ape)?|enter|return|home|end|delete)(?=\s+key\b)",
                        re.IGNORECASE)
PRESS = re.compile(r"\b(?:press(?:es|ing)?|hit|type the key)\s+(?:the\s+)?[\"“'`]?([^\s\"”'`]{1,12}?)[\"”'`]?"
                   r"(?=[\s,.;)]|$)", re.IGNORECASE)
MAX_KEYS = 6


def combo(spec):
    """'Ctrl+Shift+K' -> key event fields, or None when it is not a key this grammar knows."""
    parts = [p.strip() for p in spec.split("+")]
    key, mods = parts[-1], [m.lower() for m in parts[:-1]]
    if not key or any(m not in BITS for m in mods):
        return None
    modifiers = 0
    for m in mods:
        modifiers |= BITS[m]
    if re.fullmatch(r"[a-z]", key, re.IGNORECASE):
        key = key.upper() if modifiers & 8 else key.lower()
        return {"key": key, "code": "Key" + key.upper(), "vk": ord(key.upper()), "modifiers": modifiers}
    if re.fullmatch(r"[0-9]", key):
        return {"key": key, "code": "Digit" + key, "vk": ord(key), "modifiers": modifiers}
    if key in PUNCT:
        vk, code = PUNCT[key]
        return {"key": key, "code": code, "vk": vk, "modifiers": modifiers}
    named = NAMED.get(re.sub(r"\s+", "", key).lower())
    if named:
        return {"key": named[0], "code": CODES.get(named[0], named[0]), "vk": named[1], "modifiers": modifiers}
    return None


def goal_keys(goal):
    """Key actions for every key the goal names, in order, at most MAX_KEYS."""
    found = []
    text = goal or ""
    specs = [m.group(0) for m in COMBO.finditer(text)]
    specs += [m.group(1) for m in PRESS.finditer(text)]
    specs += [m.group(0) for m in NAMED_WORD.finditer(text)]
    seen = set()
    for spec in specs:
        spec = re.sub(r"\s*\+\s*", "+", spec.strip())
        event = combo(spec)
        if event is None and len(spec) > 1 and spec[-1] in ".,!:?":
            spec = spec[:-1]                  # sentence punctuation: "...with Ctrl+K."
            event = combo(spec)
        if event is None or (event["key"], event["modifiers"]) in seen:
            continue
        seen.add((event["key"], event["modifiers"]))
        slug = re.sub(r"[^a-z0-9]+", "_", spec.lower()).strip("_") or "key"
        found.append({"id": f"key_goal_{slug}_{len(found)}", "kind": "key", **event,
                      # A printable key without Ctrl/Alt/Meta is meant for the page, not a field.
                      "shortcut": len(event["key"]) == 1 and not event["modifiers"] & 7,
                      "label": f"Press {spec} (named in the task)"})
        if len(found) >= MAX_KEYS:
            break
    return found


def with_goal_keys(page, keys):
    """The page's actions plus the goal's keys the page does not already offer."""
    if not keys:
        return page
    offered = {(a.get("key"), int(a.get("modifiers", 0) or 0)) for a in page.get("actions", [])
               if a.get("kind") == "key"}
    extra = [k for k in keys if (k["key"], k["modifiers"]) not in offered]
    if not extra:
        return page
    return {**page, "actions": page["actions"] + extra}
