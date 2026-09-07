"""Unattended Windows agent entry point (headless, no desktop client)."""

import logging
import os
import sys
from pathlib import Path

from backer.agent.service import setup_agent_logging
from backer.client.agent import BackerAgent


def main() -> None:
    if getattr(sys, "frozen", False):
        os.environ.setdefault("BACKER_DATA_DIR", str(Path(sys.executable).resolve().parent))
    config_dir = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Backer"
    os.environ.setdefault("BACKER_CONFIG_DIR", str(config_dir))
    log_dir = config_dir / "logs"
    setup_agent_logging(log_dir)
    logging.info("Backer agent service starting")
    BackerAgent.from_config().run()


if __name__ == "__main__":
    main()
