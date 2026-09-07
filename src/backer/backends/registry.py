"""Backend lookup and instantiation."""

from typing import Any

from backer.backends.base import BackendBase, BackendType
from backer.backends.files import FilesBackend
from backer.backends.kopia import KopiaBackend
from backer.backends.proxy import ProxyBackend

_BACKENDS: dict[BackendType, type[BackendBase]] = {
    BackendType.KOPIA: KopiaBackend,
    BackendType.FILES: FilesBackend,
    BackendType.PROXY: ProxyBackend,
}


class BackendRegistry:
    """Registry for backup backends."""

    @classmethod
    def get(cls, backend_type: BackendType, config: dict[str, Any] | None = None) -> BackendBase:
        """Get an instance of a backend by type."""
        if backend_type not in _BACKENDS:
            available = ", ".join(b.value for b in _BACKENDS)
            raise ValueError(
                f"Unknown backend type: {backend_type}. Available backends: {available}"
            )
        return _BACKENDS[backend_type](config)

    @classmethod
    def available_backends(cls) -> list[BackendType]:
        """List all registered backend types."""
        return list(_BACKENDS)

    @classmethod
    def check_all_available(cls) -> dict[BackendType, tuple[bool, str]]:
        """Check availability of all registered backends."""
        results = {}
        for backend_type in _BACKENDS:
            backend = cls.get(backend_type)
            results[backend_type] = backend.check_available()
        return results


def get_backend(backend_type: str | BackendType, config: dict[str, Any] | None = None) -> BackendBase:
    """Convenience function to get a backend instance."""
    if isinstance(backend_type, str):
        backend_type = BackendType(backend_type)
    return BackendRegistry.get(backend_type, config)
