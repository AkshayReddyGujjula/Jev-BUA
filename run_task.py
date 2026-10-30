"""Compatibility entry point; the CLI lives in AgentJevUltraFast/cli.py (installed as `jev`)."""

import sys

from AgentJevUltraFast.cli import main

if __name__ == "__main__":
    sys.exit(main())
