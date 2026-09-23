"""Offline sentinel checks for the model-facing page and trace boundary."""

import shutil
import subprocess
from pathlib import Path

import pytest

from AgentJevUltraFast.privacy import redact


def test_nested_trace_redacts_private_literal():
    secret = "profile-sentinel-48293@example.test"
    trace = {"page": {"text": f"Hello {secret}"}, "history": [{"text": secret}]}
    result = redact(trace, [secret])
    assert secret not in str(result)
    assert result["history"][0]["text"] == "[private]"


def test_browser_snapshot_masks_private_literal_before_python_receives_it():
    if shutil.which("node") is None:
        pytest.skip("Node.js is needed for the snapshot JavaScript contract")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", str(root / "tests" / "fixtures" / "snapshot_privacy.js"),
         str(root / "AgentJevUltraFast" / "snapshot.js")],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
