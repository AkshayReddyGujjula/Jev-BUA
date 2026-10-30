"""Moved to AgentJevUltraFast.checks (so `jev test` ships with the package).

Kept so existing commands keep working:

  uv run --env-file .env python scripts/run_checks.py checks.json --workers 6
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from AgentJevUltraFast.checks import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
