"""Backer server daemon."""

import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from backer.server.app import create_app


def setup_logging(data_dir: Path, log_level: str = "INFO") -> None:
    """Configure logging to both console and file."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create log file with date
    log_file = log_dir / f"backer-server-{datetime.now().strftime('%Y-%m-%d')}.log"

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)

    # File handler with more detail
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_format)
    root_logger.addHandler(file_handler)

    # Log startup info
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized. Log file: {log_file}")


class BackerServer:
    """Main server daemon class."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8420,
        data_dir: Path | None = None,
    ):
        self.host = host
        self.port = port
        self.data_dir = data_dir or Path.home() / ".local" / "share" / "backer"

        # Setup logging before creating app
        setup_logging(self.data_dir)

        self.logger = logging.getLogger(__name__)
        self.logger.info(f"Initializing Backer server on {host}:{port}")
        self.logger.info(f"Data directory: {self.data_dir}")

        self.app = create_app(self.data_dir)
        self._server: uvicorn.Server | None = None

    def run(self) -> None:
        """Run the server."""
        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def _handle_signal(self, signum: int, frame: object) -> None:
        """Handle shutdown signals."""
        print(f"\nReceived signal {signum}, shutting down...")
        if self._server:
            self._server.should_exit = True
        sys.exit(0)


def run_server(
    host: str = "0.0.0.0",
    port: int = 8420,
    data_dir: Path | None = None,
) -> None:
    """Convenience function to run the server."""
    server = BackerServer(host=host, port=port, data_dir=data_dir)
    server.run()
