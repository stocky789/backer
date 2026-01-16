"""Backend registry for discovering and instantiating backends."""

from typing import Any

from backer.backends.base import BackendBase, BackendType


class BackendRegistry:
    """Registry for backup backends."""

    _backends: dict[BackendType, type[BackendBase]] = {}
    _backends_loaded = False

    @classmethod
    def _ensure_backends_loaded(cls):
        """Ensure all backend modules are imported and registered."""
        if cls._backends_loaded:
            return

        # Import backend modules to register them
        try:
            from . import kopia, proxy, rclone, restic, rsync  # noqa: F401
        except ImportError:
            # Some backends may not be available
            pass

        cls._backends_loaded = True

    @classmethod
    def register(cls, backend_type: BackendType):
        """Decorator to register a backend class."""

        def decorator(backend_class: type[BackendBase]):
            cls._backends[backend_type] = backend_class
            return backend_class

        return decorator

    @classmethod
    def get(cls, backend_type: BackendType, config: dict[str, Any] | None = None) -> BackendBase:
        """Get an instance of a backend by type."""
        cls._ensure_backends_loaded()

        if backend_type not in cls._backends:
            available = ", ".join(b.value for b in cls._backends.keys())
            raise ValueError(
                f"Unknown backend type: {backend_type}. Available backends: {available}"
            )
        return cls._backends[backend_type](config)

    @classmethod
    def available_backends(cls) -> list[BackendType]:
        """List all registered backend types."""
        return list(cls._backends.keys())

    @classmethod
    def check_all_available(cls) -> dict[BackendType, tuple[bool, str]]:
        """Check availability of all registered backends."""
        results = {}
        for backend_type in cls._backends:
            backend = cls.get(backend_type)
            results[backend_type] = backend.check_available()
        return results


def get_backend(backend_type: str | BackendType, config: dict[str, Any] | None = None) -> BackendBase:
    """Convenience function to get a backend instance."""
    if isinstance(backend_type, str):
        backend_type = BackendType(backend_type)
    return BackendRegistry.get(backend_type, config)
