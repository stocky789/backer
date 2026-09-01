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
REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS = 0x400, 0x00200000, 0x02000000
GENERIC_READ, GENERIC_WRITE, OPEN_ALWAYS = 0x80000000, 0x40000000, 4


class TransactionError(RuntimeError):
    pass


def regular(st):
    return stat.S_ISREG(st.st_mode) and not getattr(st, "st_file_attributes", 0) & REPARSE_POINT


def win_final(h):
    k = ctypes.windll.kernel32
    n = k.GetFinalPathNameByHandleW(h, None, 0, 0)
    b = ctypes.create_unicode_buffer(n + 1)
    if not n or not k.GetFinalPathNameByHandleW(h, b, len(b), 0):
        raise TransactionError("cannot resolve Windows handle")
    return os.path.normcase(b.value.removeprefix("\\\\?\\"))


def win_open(path, access, directory=False):
    k = ctypes.windll.kernel32
    h = k.CreateFileW(
        str(path),
        access,
        0,
        None,
        OPEN_ALWAYS,
        FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if directory else 0),
        None,
    )
    if h == -1:
        raise OSError(ctypes.get_last_error(), str(path))
    if win_final(h) != os.path.normcase(str(path.absolute())):
        k.CloseHandle(h)
        raise TransactionError("path escaped repository")
    return h


def open_file(path, write=False):
    if os.name == "nt":
        import msvcrt

        return msvcrt.open_osfhandle(
            win_open(path, GENERIC_READ | (GENERIC_WRITE if write else 0)),
            os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY),
        )
    fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0))
    if not regular(os.fstat(fd)):
        os.close(fd)
        raise TransactionError("not a regular file")
    return fd


def read_file(path):
    fd = open_file(path)
    try:
        st = os.fstat(fd)
        if not regular(st) or st.st_size > MAX_JOURNAL:
            raise TransactionError("invalid target")
        return os.read(fd, st.st_size), st
    finally:
        os.close(fd)


class ReleaseLock:
    def __enter__(self):
        self.path = ROOT / LOCK
        if os.name == "nt":
            import msvcrt

            self.fd = msvcrt.open_osfhandle(win_open(self.path, GENERIC_READ | GENERIC_WRITE), os.O_BINARY | os.O_RDWR)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not regular(os.fstat(self.fd)):
                raise TransactionError("lock is not regular")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(self.fd).st_size == 0:
            self.clear()
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def clear(self):
        os.lseek(self.fd, 0, 0)
        os.write(self.fd, b"0")
        os.fsync(self.fd)
        os.ftruncate(self.fd, 1)
        os.fsync(self.fd)

    def load(self):
        os.lseek(self.fd, 0, 0)
        raw = os.read(self.fd, MAX_JOURNAL + 2)
        if raw[:1] == b"0":
            return None
        if len(raw) < 2 or raw[:1] != b"1" or len(raw) > MAX_JOURNAL:
            raise TransactionError("corrupt recovery journal")
        try:
            return json.loads(raw[1:])
        except Exception as e:
            raise TransactionError("corrupt recovery journal") from e

    def save(self, j):
        raw = b"1" + json.dumps(j, separators=(",", ":"), sort_keys=True).encode()
        if len(raw) > MAX_JOURNAL:
            raise TransactionError("recovery journal too large")
        os.lseek(self.fd, 0, 0)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, raw)
        os.fsync(self.fd)


def stage(target, data, mode, mtime):
    if os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(win_open(target.parent, GENERIC_READ, True))
    fd, name = tempfile.mkstemp(prefix=".backer-version-", dir=target.parent)
    path = Path(name)
    try:
        if not regular(os.fstat(fd)):
            raise TransactionError("bad staging descriptor")
        os.write(fd, data)
        os.fsync(fd)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IMODE(mode))
        if os.name != "nt":
            os.utime(fd, ns=(mtime, mtime))
    finally:
        os.close(fd)
    return path


def validate(j):
    if (
        not isinstance(j, dict)
        or set(j) != {"schema", "committed", "entries"}
        or j["schema"] != 1
        or not isinstance(j["committed"], int)
        or not 0 <= j["committed"] <= 4
    ):
        raise TransactionError("invalid recovery journal")
    if not isinstance(j["entries"], list) or len(j["entries"]) != 4:
        raise TransactionError("invalid recovery journal")
    for rel, e in zip(TARGETS, j["entries"], strict=True):
        if (
            not isinstance(e, dict)
            or set(e) != {"target", "data", "sha256", "mode", "mtime_ns"}
            or e["target"] != rel.as_posix()
        ):
            raise TransactionError("invalid recovery journal")
        try:
            d = base64.b64decode(e["data"], validate=True)
        except Exception as x:
            raise TransactionError("invalid recovery data") from x
        if len(d) > MAX_JOURNAL or hashlib.sha256(d).hexdigest() != e["sha256"]:
            raise TransactionError("invalid recovery data")
    return j["entries"]


def recover(lock, j):
    for rel, e in zip(TARGETS, validate(j), strict=True):
        p = ROOT / rel
        s = stage(p, base64.b64decode(e["data"]), e["mode"], e["mtime_ns"])
        try:
            os.replace(s, p)
        except Exception:
            raise
    lock.clear()


def version_files(v):
    code = sum(int(x) * n for x, n in zip(v.split("."), (10000, 100, 1), strict=True))
    rules = (
        (r'^(version\s*=\s*")[^"]+(")', rf"\g<1>{v}\g<2>"),
        (r'^(__version__\s*=\s*")[^"]+(")', rf"\g<1>{v}\g<2>"),
        (r'^(#define\s+MyAppVersion\s+")[^"]+(")', rf"\g<1>{v}\g<2>"),
        (r"^([ \t]*versionCode\s*=\s*)\d+", rf"\g<1>{code}"),
    )
    out = {}
    for rel, rule in zip(TARGETS, rules, strict=True):
        d, _ = read_file(ROOT / rel)
        x, n = re.subn(*rule, d.decode(), count=1, flags=re.M)
        if n != 1:
            raise ValueError(f"{rel}: expected one version field")
        out[ROOT / rel] = x.encode()
    p = ROOT / TARGETS[-1]
    x, n = re.subn(r'^([ \t]*versionName\s*=\s*")[^"]+(")', rf"\g<1>{v}\g<2>", out[p].decode(), count=1, flags=re.M)
    if n != 1:
        raise ValueError("android versionName missing")
    out[p] = x.encode()
    return out


def write_all(updates, lock):
    es = []
    for r in TARGETS:
        d, s = read_file(ROOT / r)
        es.append(
            {
                "target": r.as_posix(),
                "data": base64.b64encode(d).decode(),
                "sha256": hashlib.sha256(d).hexdigest(),
                "mode": stat.S_IMODE(s.st_mode),
                "mtime_ns": s.st_mtime_ns,
            }
        )
    j = {"schema": 1, "committed": 0, "entries": es}
    lock.save(j)
    try:
        for i, r in enumerate(TARGETS):
            p = ROOT / r
            _, s = read_file(p)
            t = stage(p, updates[p], s.st_mode, s.st_mtime_ns)
            os.replace(t, p)
    except Exception:
        recover(lock, j)
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
            v = sys.argv[1]
            if not re.search(rf"^## {re.escape(v)}$", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M):
                print(f"CHANGELOG.md must contain '## {v}' before bumping versions", file=sys.stderr)
                return 2
            write_all(version_files(v), lock)
    except (OSError, TransactionError, ValueError) as e:
        print(f"could not update release versions: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
