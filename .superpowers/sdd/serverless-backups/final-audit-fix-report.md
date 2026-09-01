# Final audit fix report

## RED

1. `./.venv/Scripts/python.exe -m pytest -q tests/test_backends.py -k prune_preview_lists_expired_snapshots` failed because the argv lacked `--retention`.
2. `./.venv/Scripts/python.exe -m pytest -q tests/test_cli_serverless.py -k 'repo_add_non_tty or repo_add_does_not_advertise_unused_yes_flag or generated_passphrase_needs_visible'` failed because direct non-TTY `repo add` proceeded and help exposed `--yes`.
3. `./.venv/Scripts/python.exe -m pytest -q tests/test_config_contract.py` failed because schedule state remained wrapped and jobs named `fires`/`pause` collided with control keys.
4. `./.venv/Scripts/python.exe -m pytest -q tests/test_agent_protocol.py -k failed_backup_run_is_recorded_with_its_error` failed because run records lacked `return_code`.

## GREEN

- Added Kopia `snapshot list --json --retention` and verified the pinned binary's `snapshot list --help` documents `--[no-]retention`.
- Direct non-TTY `repo add` now requires `--headless`; headless passphrase generation still requires `--passphrase-out` or `--print-passphrase`. `init` remains explicit headless mode. Removed the unused `repo add --yes` option and all callers.
- `schedule.json` is now the flat fire-time map. Wrapped legacy data migrates once, while pause state is atomically stored in `schedule-runtime.json`. GUI pause rollback restores the runtime file byte-for-byte.
- Repository run records now write `return_code`, a single-line `error`, and `error_stage=backup` on backend failures while retaining `errors`.

## Verification

- `tests/test_backends.py -k prune`: 8 passed, 52 deselected.
- `tests/test_cli_serverless.py`: 47 passed.
- `tests/test_config_contract.py tests/test_serverless_unattended.py`: 31 passed.
- `tests/test_agent_protocol.py`: 31 passed, 1 skipped.
- `tests/test_serverless_e2e.py`: 1 passed, 3 skipped.
- Ruff on all changed Python files: passed.
- `git diff --check`: passed.

## Diff and self-review

Changed `CHANGELOG.md`, five production modules, and five focused test modules. The intentionally untracked `docs/` directory was neither read nor staged. Confirmed the public `repo add --help` has no `--yes`, scheduler fire timestamps and pause state are independent files, and no deletion path changed.
