"""Public URL helpers for agent-facing server addresses."""

import socket


def get_default_public_url() -> str:
    """Detect a local URL agents can use, falling back to localhost."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]

        if local_ip and not local_ip.startswith("127."):
            return f"http://{local_ip}:8420"
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        if local_ip and not local_ip.startswith("127."):
            return f"http://{local_ip}:8420"
    except Exception:
        pass

    return "http://localhost:8420"
