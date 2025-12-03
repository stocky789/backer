"""Backer server daemon."""

import signal
import sys
from pathlib import Path

import uvicorn

from backer.server.app import create_app


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
