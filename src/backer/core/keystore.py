"""Small OS-backed secret store for local agent credentials."""

import ctypes
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from backer.core.paths import get_data_dir


def _filename(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _file_dir(machine_scope: bool) -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("ProgramData" if machine_scope else "APPDATA", r"C:\ProgramData")) / "Backer"
        return root / "secrets"
    return get_data_dir() / "secrets"


def _file_path(key: str, machine_scope: bool) -> Path:
    return _file_dir(machine_scope) / _filename(key)


def _secret_tool_available() -> bool:
    return bool(shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def _secret_tool(args: list[str], value: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["secret-tool", *args], input=value, capture_output=True, text=True, timeout=15, check=False
    )


def _set_private(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _file_put(key: str, value: str, machine_scope: bool) -> None:
    path = _file_path(key, machine_scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    _set_private(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value, encoding="utf-8")
    _set_private(temporary, 0o600)
    os.replace(temporary, path)
    _set_private(path, 0o600)


def _file_get(key: str, machine_scope: bool) -> str | None:
    path = _file_path(key, machine_scope)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(value: str, machine_scope: bool) -> bytes:
    raw, keepalive = _blob(value.encode("utf-8"))
    encrypted = DataBlob()
    flags = 0x4 if machine_scope else 0
    protected = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(raw), None, None, None, None, flags, ctypes.byref(encrypted)
    )
    if not protected:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(encrypted.pbData, encrypted.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(encrypted.pbData)


def _dpapi_unprotect(value: bytes) -> str | None:
    raw, keepalive = _blob(value)
    plain = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(raw), None, None, None, None, 0, ctypes.byref(plain)):
        return None
    try:
        return ctypes.string_at(plain.pbData, plain.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(plain.pbData)


def _dpapi_put(key: str, value: str, machine_scope: bool) -> None:
    path = _file_path(key, machine_scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    grants = ["/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "/grant:r", "*S-1-5-32-544:(OI)(CI)F"]
    if user:
        grants.extend(["/grant:r", f"{domain}\\{user}:(OI)(CI)F" if domain else f"{user}:(OI)(CI)F"])
    subprocess.run(["icacls", str(path.parent), *grants], capture_output=True, check=False)
    path.write_bytes(_dpapi_protect(value, machine_scope))


def _dpapi_get(key: str, machine_scope: bool) -> str | None:
    try:
        return _dpapi_unprotect(_file_path(key, machine_scope).read_bytes())
    except FileNotFoundError:
        return None


def put(key: str, value: str, *, machine_scope: bool = False) -> str:
    """Store a secret and verify it can be retrieved before returning its backend."""
    if os.name == "nt":
        _dpapi_put(key, value, machine_scope)
        backend = "dpapi"
    elif _secret_tool_available():
        result = _secret_tool(["store", "--label=Backer", "service", "backer", "key", key], value)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "secret-tool store failed")
        backend = "secret-tool"
    else:
        _file_put(key, value, machine_scope)
        backend = "file"
    if get(key, machine_scope=machine_scope) != value:
        delete(key, machine_scope=machine_scope)
        raise RuntimeError("secret could not be read back")
    return backend


def get(key: str, *, machine_scope: bool = False) -> str | None:
    if os.name == "nt":
        return _dpapi_get(key, machine_scope)
    if _secret_tool_available():
        result = _secret_tool(["lookup", "service", "backer", "key", key])
        return result.stdout.rstrip("\r\n") if result.returncode == 0 else None
    return _file_get(key, machine_scope)


def delete(key: str, *, machine_scope: bool = False) -> None:
    if os.name != "nt" and _secret_tool_available():
        _secret_tool(["clear", "service", "backer", "key", key])
        return
    _file_path(key, machine_scope).unlink(missing_ok=True)
