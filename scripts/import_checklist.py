"""Draft Jev Markdown specs from a JSON test checklist (title, steps[], expected[] per item).

    python scripts/import_checklist.py CHECKLIST.json --sections A1-S05,A1-S06 \
        --setup examples/studycanvas/setup.md --path "/workspace?storage=idb" \
        --base-url http://localhost:5173 --out examples/studycanvas/checklist

Each checklist item becomes one test: its steps become one goal, and every quoted phrase in its
expected results becomes a `see:` check, except phrases that also appear in the steps (those are
usually the control being pressed, and would pass before anything happened). An item with no
quoted expectation gets no check and is marked for review. The output is a DRAFT: a human or a
coding agent should read it before trusting a pass.

Items that need AI generation, uploads, devices or other browsers are skipped by keyword, so the
draft holds only what a browser agent can run on its own.

With --align, one cheap text-model call per item places each check after the step that should
show it (a checklist lists its expectations once per item, and "read the 404 page, then click Go
home" must check the 404 page before the click, not after it). Each checklist step then becomes
its own numbered step; a phrase visible before any step gets a leading "Read the page." step.
"""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

QUOTED = re.compile(r"[\"“”]([^\"“”]{3,80})[\"“”]")
SKIP = re.compile(
    r"\b(upload|drag|drop a file|pdf|png|jpe?g|image|photo|microphone|voice|camera|safari|firefox|"
    r"mobile|phone|tablet|offline|print|clipboard|terminal|devtools|reload the browser|restart|"
    r"another browser|incognito|second tab|two tabs|folder picker|choose a folder|kv|quota|right-click|"
    r"right click|context menu|hover|tooltip|drag)\b",
    re.IGNORECASE,
)


def checks_for(item):
    steps_text = " ".join(item.get("steps") or []).lower()
    seen = []
    for line in item.get("expected") or []:
        for phrase in QUOTED.findall(line):
            phrase = phrase.strip().rstrip(".")
            if (len(phrase) < 4 or (" " not in phrase and phrase.islower())        # "with", "here"
                    or "<" in phrase or phrase.startswith("/")                    # URL patterns
                    or "{" in phrase or "…" in phrase or "..." in phrase or phrase.lower() in steps_text
                    or phrase in seen):
                continue
            seen.append(phrase)
    return seen


OPEN = re.compile(r"^\s*(?:open|go to|visit|navigate to)\s+[\"“]((?:https?://[^\"”\s]+)|(?:/[^\"”\s<]*))[\"”]\.?\s*",
                  re.IGNORECASE)


def split_open(steps):
    """(start path, remaining steps): an "Open "<url>"" sentence becomes where the test starts.

    Jev has no address bar, so a goal that says "open this URL" cannot be done by choosing
    controls; the runner loads the path instead and Jev does the rest.
    """
    start, rest = None, []
    for step in steps:
        match = OPEN.match(step)
        if match and start is None:
            target = match[1]
            if target.startswith("http"):
                parts = urlsplit(target)
                target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            start = target
            step = step[match.end():].strip()
        if step:
            rest.append(step)
    return start, rest


ALIGN_SYSTEM = """You place the checks of one browser test on the right step.
You get the test's steps, numbered from 1, and phrases the app should show. For each phrase, give the
number of the step after which the phrase is on screen and stays there while that step's own action
is done; use 0 when the phrase is on screen before step 1 and a step's action takes the user away from
it (for example "read the error page, then click Go home"). When unsure, use the last step.
Return JSON: {"after": {"<phrase>": <number>}} with every phrase exactly as given."""


def align(steps, phrases, call=None):
    """{phrase: step number 0..len(steps)}: where each check belongs. Falls back to the last step."""
    last = len(steps)
    placed = {phrase: last for phrase in phrases}
    if not phrases or not steps:
        return placed
    if call is None:
        from AgentJevUltraFast.model import text_call
        call = text_call
    try:
        answer, _ = call(ALIGN_SYSTEM, {"steps": {i: s for i, s in enumerate(steps, 1)}, "phrases": phrases},
                         timeout=30, max_tokens=600, ceiling=30)
        after = answer.get("after") or {}
    except Exception:
        return placed                         # the draft still stands, just unaligned
    if not isinstance(after, dict):
        return placed
    for phrase in phrases:
        value = after.get(phrase)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= last:
            placed[phrase] = value
    return placed


def runnable(item):
    if item.get("retired") or item.get("gemini", "none") != "none":
        return False
    text = " ".join([item.get("title", ""), item.get("where", ""), *(item.get("steps") or []),
                     *(item.get("prereq") or [] if isinstance(item.get("prereq"), list) else
                       [item.get("prereq") or ""])])
    return not SKIP.search(text)


def to_markdown(section, items, setup, path, base_url, aligner=None):
    lines = [f"# {section['id']} {section['title']} (draft from the checklist)", "",
             "Generated by scripts/import_checklist.py. Review before trusting a pass.", "",
             f"base_url: {base_url}", "isolate: storage", "commit_floor: 0.6",
             "max_seconds: 150", ""]
    if setup:
        lines += [setup.strip(), ""]
    for item in items:
        checks = checks_for(item)
        lines.append(f"## {item['id']} {item['title']}" + ("" if checks else " (no quoted check: review)"))
        start, steps = split_open(item.get("steps") or [])
        if start:
            # Keep the app's own query switches (e.g. ?storage=idb) on the opened path.
            query = urlsplit(path or "").query
            start = start + (("&" if "?" in start else "?") + query if query and query not in start else "")
        if start or path:
            lines.append(f"path: {start or path}")
        lines.append("")
        sentences = [step.strip() if step.strip().endswith((".", "?", "!")) else step.strip() + "."
                     for step in steps if step.strip()]
        if aligner and checks:
            placed = aligner(sentences, checks)
            numbered = ([("Read the page.", [p for p in checks if placed[p] == 0])]
                        if any(placed[p] == 0 for p in checks) else [])
            numbered += [(sentence, [p for p in checks if placed[p] == index])
                         for index, sentence in enumerate(sentences, 1)]
        else:
            numbered = [(" ".join(sentences) or "Read the page.", checks)]
        for number, (goal, step_checks) in enumerate(numbered, 1):
            lines.append(f"{number}. {goal}")
            lines.extend(f"   - see: {phrase}" for phrase in step_checks)
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("checklist", help="a JSON file with sections[].items[] (or items[])")
    parser.add_argument("--sections", default="", help="comma-separated section ids (default all)")
    parser.add_argument("--setup", default="",
                        help="a Markdown file holding a '## Before all' or '## Before each' block")
    parser.add_argument("--path", default="", help="route every test starts on")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True, help="output folder for one .md per section")
    parser.add_argument("--align", action="store_true",
                        help="place each check on its step (one cheap text-model call per item)")
    args = parser.parse_args()
    aligner = None
    if args.align:
        from AgentJevUltraFast.checks import load_env
        load_env()
        aligner = align
    data = json.loads(Path(args.checklist).read_text(encoding="utf-8"))
    sections = data.get("sections") or [{"id": "checklist", "title": data.get("title", ""), "items": data["items"]}]
    wanted = {s.strip() for s in args.sections.split(",") if s.strip()}
    setup = Path(args.setup).read_text(encoding="utf-8") if args.setup else ""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for section in sections:
        if wanted and section["id"] not in wanted:
            continue
        items = [item for item in section.get("items", []) if runnable(item)]
        if not items:
            continue
        target = out / f"{section['id']}.md"
        target.write_text(to_markdown(section, items, setup, args.path, args.base_url, aligner),
                          encoding="utf-8")
        checked = sum(1 for item in items if checks_for(item))
        print(f"{target}: {len(items)} tests ({checked} with checks)")


if __name__ == "__main__":
    main()
