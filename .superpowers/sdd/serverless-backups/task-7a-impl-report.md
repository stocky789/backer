# Phase 7a implementation report

Implemented release metadata, changelog, version automation, and the published support matrix.

- Added `scripts/bump_version.py`: strict SemVer validation, required `CHANGELOG.md` heading, Android code calculation, byte-preserving staged replacement, durable backups and journal recovery.
- `make release VERSION=<version>` now runs the script and stages all version files with the changelog.
- Released 0.9.0 in all four version locations, added machine-checked notes, and rewrote the README support matrix.
- Added bump-version and README matrix tests plus a release target sanity test.

Verification:

- RED: `tests/test_bump_version.py tests/test_docs_matrix.py tests/test_workflow_sanity.py::test_make_release_uses_the_atomic_version_bump_script` failed before implementation (missing script, README headings, and release command).
- `33 passed` for `tests/test_bump_version.py tests/test_docs_matrix.py tests/test_workflow_sanity.py`.
- `python scripts/check_changelog.py` passed.
- `python scripts/bump_version.py 0.9.1` exited 2 without changing version files; `python scripts/bump_version.py 0.9.0` exited 0.
- `ruff check src/ tests/ scripts/bump_version.py` and `git diff --check` passed.

Round 1 review fixes:

- The version rewrite reads and writes bytes, preserving CRLF, unchanged text, modes, and timestamps.
- A journal and durable backups restore all four files after a replace failure or an interrupted process; the next invocation recovers before validating its arguments.
- Injected tests cover staged-write failures, each target replace position, rollback write and replace failures, interruption recovery, CRLF byte counts, unchanged text, modes, and cleanup.
- Docker examples now use `backer:0.9.0`.

Round 2 security fixes:

- Recovery accepts only a schema-versioned, canonical four-entry manifest and verifies every backup and staged file's name, containment, regular-file type, size, SHA-256, and mode before replacing any target.
- The release script takes a nonblocking OS lock before recovery and holds it through staging, rollback, and cleanup. A crashed holder releases the lock through the operating system.
- A journal-less staging directory is removed only before commit starts. Incomplete commit state without a journal fails closed.
- Tests cover malformed, partial, reordered, duplicate, traversal, absolute, symlinked, size/hash/mode-invalid journals, concurrent locking, and stale-lock recovery.

Round 3 symlink and reparse fixes:

- Every journal, transaction artifact, temporary file, and lock boundary uses `lstat`, rejects links and reparse points, and opens regular files with no-follow flags where supported.
- Journal reads use bounded descriptor reads. Journal writes use `mkstemp` for an exclusive unpredictable name, fsync its descriptor, reject a pre-existing journal link, then replace it.
- Cleanup validates regular files before unlinking and rejects reparse or link entries. Tests cover symlinked journal and lock files plus reparse-point metadata; symlink creation remains skipped where Windows policy denies it.

Crash-recovery staging cleanup fix:

- Each verified update staging file is durably recorded in the lock journal before its descriptor closes. Recovery validates and deletes only those recorded, unchanged identities after restoring all originals.
- `tests/test_bump_version.py`: crash on the second target replacement, recover on the next invocation, assert all originals return and no `.backer-version-*` files remain.
- `29 passed, 1 skipped` — `python scripts/check_changelog.py` passed — `ruff check scripts/bump_version.py tests/test_bump_version.py` and `git diff --check` passed.
