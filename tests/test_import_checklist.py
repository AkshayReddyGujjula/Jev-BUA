"""The checklist importer drafts specs; --align places each check after the step that shows it."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import_checklist.py"
spec = importlib.util.spec_from_file_location("import_checklist", SCRIPT)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)

ITEM = {
    "id": "S05-01", "title": "Unknown URL shows the 404 page", "gemini": "none",
    "steps": ['Open "http://localhost:5173/workspace/nope".', 'Read the page, then click "Go home".'],
    "expected": ['The page shows "Page not found" and a "Go home" link.'],
}
SECTION = {"id": "S05", "title": "Routing"}


def test_without_align_the_item_is_one_step_with_every_check():
    markdown = importer.to_markdown(SECTION, [ITEM], "", "/workspace?storage=idb", "http://localhost:5173")
    assert "path: /workspace/nope?storage=idb" in markdown
    assert "1. Read the page, then click \"Go home\".\n   - see: Page not found" in markdown


def test_align_puts_a_check_the_step_leaves_before_that_step():
    calls = []

    def fake_call(system, context, **_):
        calls.append(context)
        return {"after": {"Page not found": 0}}, {}

    aligner = lambda steps, phrases: importer.align(steps, phrases, call=fake_call)  # noqa: E731
    markdown = importer.to_markdown(SECTION, [ITEM], "", "", "http://localhost:5173", aligner)
    assert "1. Read the page.\n   - see: Page not found\n2. Read the page, then click" in markdown
    assert calls[0]["phrases"] == ["Page not found"]


def test_a_bad_or_failed_answer_keeps_the_check_on_the_last_step():
    def broken(*_, **__):
        raise TimeoutError

    assert importer.align(["A.", "B."], ["X"], call=broken) == {"X": 2}
    assert importer.align(["A.", "B."], ["X"], call=lambda *a, **k: ({"after": {"X": 9}}, {})) == {"X": 2}
    assert importer.align(["A.", "B."], ["X"], call=lambda *a, **k: ({"after": {"X": True}}, {})) == {"X": 2}
