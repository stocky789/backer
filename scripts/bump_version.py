#!/usr/bin/env python3
"""Fail-closed four-file release version transaction."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ".backer-bump-version.lock"
TARGETS = (
    Path("pyproject.toml"),
    Path("src/backer/_version.py"),
    Path("installer/backer-agent.iss"),
    Path("android/app/build.gradle.kts"),
)
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
MAX_JOURNAL = 8 * 1024 * 1024
MAX_TIME_NS = (1 << 63) - 1
REPARSE_POINT, FILE_ATTRIBUTE_DIRECTORY = 0x400, 0x10
FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_READONLY = 0x80, 0x1
FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS = 0x00200000, 0x02000000
GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING, OPEN_ALWAYS = 0x80000000, 0x40000000, 3, 4
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class TransactionError(RuntimeError):
    pass


def regular(st):
    return stat.S_ISREG(st.st_mode) and not getattr(st, "st_file_attributes", 0) & REPARSE_POINT


def _expected(path):
    return os.path.normcase(os.path.abspath(path))


def _win_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.SetFileTime.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel.SetFileTime.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD)
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
    return kernel


class _ByHandleFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


def _win_final(kernel, handle):
    needed = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not needed:
        raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW")
    buf = ctypes.create_unicode_buffer(needed + 1)
    got = kernel.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
    if not got or got >= len(buf):
        raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW")
    return os.path.normcase(buf.value.removeprefix("\\\\?\\"))


def _win_open(path, access, *, directory=False, create=False):
    kernel = _win_api()
    handle = kernel.CreateFileW(
        str(path),
        access,
        0,
        None,
        OPEN_ALWAYS if create else OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if directory else 0),
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), str(path))
    try:
        info = _ByHandleFileInfo()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle")
        is_directory = bool(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
        if bool(info.dwFileAttributes & REPARSE_POINT) or is_directory != directory:
            raise TransactionError("reparse point or wrong file type")
        if _win_final(kernel, handle) != _expected(path):
            raise TransactionError("path escaped repository")
        return handle
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def _fd_final(fd):
    if os.name == "nt":
        import msvcrt

        return _win_final(_win_api(), msvcrt.get_osfhandle(fd))
    proc = f"/proc/self/fd/{fd}"
    return os.path.normcase(os.path.realpath(proc)) if os.path.exists(proc) else None


def _check_fd_path(fd, expected, *, parent=False):
    final = _fd_final(fd)
    if final is None:
        return
    if parent:
        if os.path.normcase(os.path.dirname(final)) != _expected(expected):
            raise TransactionError("staging file escaped repository")
    elif final != _expected(expected):
        raise TransactionError("path escaped repository")


def _fd_from_handle(handle, flags):
    import msvcrt

    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _win_api().CloseHandle(handle)
        raise


def open_file(path, write=False):
    if os.name == "nt":
        fd = _fd_from_handle(
            _win_open(path, GENERIC_READ | (GENERIC_WRITE if write else 0)),
            os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY),
        )
    else:
        fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW)
    try:
        if not regular(os.fstat(fd)):
            raise TransactionError("not a regular file")
        _check_fd_path(fd, path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_all(fd, size):
    parts = []
    while size:
        part = os.read(fd, size)
        if not part:
            raise TransactionError("short read")
        parts.append(part)
        size -= len(part)
    return b"".join(parts)


def read_file(path):
    fd = open_file(path)
    try:
        state = os.fstat(fd)
        if state.st_size > MAX_JOURNAL:
            raise TransactionError("invalid target")
        return _read_all(fd, state.st_size), state
    finally:
        os.close(fd)


def write_all_fd(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if not isinstance(written, int) or written <= 0:
            raise TransactionError("short write")
        view = view[written:]


def _validate_parent(path):
    if os.name == "nt":
        _win_api().CloseHandle(_win_open(path, GENERIC_READ, directory=True))
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise TransactionError("not a directory")
        _check_fd_path(fd, path)
    finally:
        os.close(fd)


def _set_times(fd, atime_ns, mtime_ns):
    if os.name != "nt":
        os.utime(fd, ns=(atime_ns, mtime_ns))
        return
    import msvcrt

    def filetime(value):
        value = value // 100 + 116444736000000000
        return wintypes.FILETIME(value & 0xFFFFFFFF, value >> 32)

    access, modified = filetime(atime_ns), filetime(mtime_ns)
    if not _win_api().SetFileTime(msvcrt.get_osfhandle(fd), None, ctypes.byref(access), ctypes.byref(modified)):
        raise OSError(ctypes.get_last_error(), "SetFileTime")


def _set_mode(fd, mode):
    if os.name != "nt":
        os.fchmod(fd, stat.S_IMODE(mode))
        return
    import msvcrt

    info = _FileBasicInfo()
    info.FileAttributes = FILE_ATTRIBUTE_NORMAL if mode & 0o222 else FILE_ATTRIBUTE_READONLY
    if not _win_api().SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle")


class ReleaseLock:
    def __enter__(self):
        self.path = ROOT / LOCK
        if os.name == "nt":
            import msvcrt

            self.fd = _fd_from_handle(
                _win_open(self.path, GENERIC_READ | GENERIC_WRITE, create=True), os.O_BINARY | os.O_RDWR
            )
            try:
                if os.fstat(self.fd).st_size == 0:
                    write_all_fd(self.fd, b"0")
                    os.fsync(self.fd)
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            except BaseException:
                os.close(self.fd)
                raise
        else:
            import fcntl

            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                if not regular(os.fstat(self.fd)):
                    raise TransactionError("lock is not regular")
                _check_fd_path(self.fd, self.path)
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.fstat(self.fd).st_size == 0:
                    self.clear()
            except BaseException:
                os.close(self.fd)
                raise
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def clear(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        write_all_fd(self.fd, b"0")
        os.fsync(self.fd)
        os.ftruncate(self.fd, 1)
        os.fsync(self.fd)

    def load(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        size = os.fstat(self.fd).st_size
        if size > MAX_JOURNAL:
            raise TransactionError("corrupt recovery journal")
        raw = _read_all(self.fd, size)
        if raw == b"0":
            return None
        if len(raw) < 2 or raw[:1] != b"1" or len(raw) > MAX_JOURNAL:
            raise TransactionError("corrupt recovery journal")
        try:
            return json.loads(raw[1:])
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise TransactionError("corrupt recovery journal") from exc

    def save(self, journal):
        raw = b"1" + json.dumps(journal, separators=(",", ":"), sort_keys=True).encode()
        if len(raw) > MAX_JOURNAL:
            raise TransactionError("recovery journal too large")
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        write_all_fd(self.fd, raw)
        os.fsync(self.fd)


def stage(target, data, mode, atime_ns, mtime_ns):
    _validate_parent(target.parent)
    fd, name = tempfile.mkstemp(prefix=".backer-version-", dir=target.parent)
    path = Path(name)
    try:
        if not regular(os.fstat(fd)):
            raise TransactionError("bad staging descriptor")
        _check_fd_path(fd, target.parent, parent=True)
        write_all_fd(fd, data)
        os.fsync(fd)
        _set_mode(fd, mode)
        _set_times(fd, atime_ns, mtime_ns)
        os.fsync(fd)
        return path
    finally:
        os.close(fd)


def _metadata(value, upper):
    return type(value) is int and 0 <= value <= upper


def validate(journal):
    if not isinstance(journal, dict) or set(journal) != {"schema", "entries"} or journal["schema"] != 1:
        raise TransactionError("invalid recovery journal")
    entries = journal["entries"]
    if not isinstance(entries, list) or len(entries) != len(TARGETS):
        raise TransactionError("invalid recovery journal")
    checked = []
    for rel, entry in zip(TARGETS, entries, strict=True):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"target", "data", "sha256", "mode", "atime_ns", "mtime_ns"}
            or entry["target"] != rel.as_posix()
            or not isinstance(entry["data"], str)
            or not isinstance(entry["sha256"], str)
            or not _metadata(entry["mode"], 0o7777)
            or not _metadata(entry["atime_ns"], MAX_TIME_NS)
            or not _metadata(entry["mtime_ns"], MAX_TIME_NS)
        ):
            raise TransactionError("invalid recovery journal")
        try:
            data = base64.b64decode(entry["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise TransactionError("invalid recovery data") from exc
        if len(data) > MAX_JOURNAL or len(entry["sha256"]) != 64 or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise TransactionError("invalid recovery data")
        checked.append((ROOT / rel, data, entry["mode"], entry["atime_ns"], entry["mtime_ns"]))
    return checked


def _replace(staged, target):
    _validate_parent(target.parent)
    os.replace(staged, target)


def recover(lock, journal):
    staged = []
    for target, data, mode, atime_ns, mtime_ns in validate(journal):
        staged.append((stage(target, data, mode, atime_ns, mtime_ns), target))
    for source, target in staged:
        _replace(source, target)
    lock.clear()


def version_files(version):
    code = sum(int(x) * n for x, n in zip(version.split("."), (10000, 100, 1), strict=True))
    rules = (
        (rb'^(version\s*=\s*")[^"]+(")', rb"\g<1>" + version.encode() + rb"\g<2>"),
        (rb'^(__version__\s*=\s*")[^"]+(")', rb"\g<1>" + version.encode() + rb"\g<2>"),
        (rb'^(#define\s+MyAppVersion\s+")[^"]+(")', rb"\g<1>" + version.encode() + rb"\g<2>"),
        (rb"^([ \t]*versionCode\s*=\s*)\d+", rb"\g<1>" + str(code).encode()),
    )
    updates, originals = {}, {}
    for rel, rule in zip(TARGETS, rules, strict=True):
        data, state = read_file(ROOT / rel)
        changed, count = re.subn(*rule, data, count=1, flags=re.M)
        if count != 1:
            raise ValueError(f"{rel}: expected one version field")
        updates[ROOT / rel] = changed
        originals[ROOT / rel] = (data, state)
    path = ROOT / TARGETS[-1]
    changed, count = re.subn(
        rb'^([ \t]*versionName\s*=\s*")[^"]+(")',
        rb"\g<1>" + version.encode() + rb"\g<2>",
        updates[path],
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise ValueError("android versionName missing")
    updates[path] = changed
    return updates, originals


def write_all(updates, originals, lock):
    entries, metadata = [], []
    for rel in TARGETS:
        target = ROOT / rel
        data, state = originals[target]
        current, _ = read_file(target)
        if current != data:
            raise TransactionError(f"{rel} changed while preparing release")
        entries.append(
            {
                "target": rel.as_posix(),
                "data": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": stat.S_IMODE(state.st_mode),
                "atime_ns": state.st_atime_ns,
                "mtime_ns": state.st_mtime_ns,
            }
        )
        metadata.append((target, state))
    journal = {"schema": 1, "entries": entries}
    lock.save(journal)
    staged, replaced = [], False
    try:
        for target, state in metadata:
            staged.append((stage(target, updates[target], state.st_mode, state.st_atime_ns, state.st_mtime_ns), target))
        for source, target in staged:
            replaced = True
            _replace(source, target)
    except Exception:
        if replaced:
            recover(lock, journal)
        else:
            lock.clear()
        raise
    lock.clear()


def main():
    try:
        with ReleaseLock() as lock:
            pending = lock.load()
            if pending is not None:
                recover(lock, pending)
            if len(sys.argv) != 2 or not VERSION.fullmatch(sys.argv[1]):
                print("usage: bump_version.py <major.minor.patch>", file=sys.stderr)
                return 2
            version = sys.argv[1]
            if not re.search(rf"^## {re.escape(version)}$", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M):
                print(f"CHANGELOG.md must contain '## {version}' before bumping versions", file=sys.stderr)
                return 2
            updates, originals = version_files(version)
            write_all(updates, originals, lock)
    except (OSError, TransactionError, ValueError) as exc:
        print(f"could not update release versions: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
