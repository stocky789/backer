# Task 6f implementation report

## Scope

- Home renders run state and size immediately, then merges local and repository records on a guarded daemon refresh. S3 uses its signed sidecar history; S3 writes that run sidecar during shared metadata persistence.
- Scheduled modes are durable unified-config booleans. Save applies the local schedule only after persisting the desired pair, rolls back the local config on an apply failure, and never starts/stops a transient agent as a toggle side effect.
- Settings normalizes and registers server URLs with the established version header, provides worker-owned service/log/update operations, selected-job scheduled-task verification, and repository passphrase/recovery controls backed by the named reveal confirmation.

## TDD evidence

- RED: configuration mode persistence, URL normalization, scheduled attempt proof, repository details, service-worker dispatch, and S3 sidecar history tests each failed before their helper existed.
- GREEN: `37 passed` config unification; `42 passed, 2 deselected` non-display GUI checks; `1 passed, 13 deselected` S3 metadata check.
- Lint: `ruff check src/backer/core/config.py src/backer/core/runner.py src/backer/agent/gui tests/test_config_unification.py tests/test_gui_serverless.py tests/test_serverless_unattended.py` passed.

## Known environment constraint

- The retained live-Tk checks remain unavailable here because Tcl cannot locate `init.tcl`; they are deliberately not treated as skipped acceptance.

## Fix round 2

- Scheduled test runs use a unique selected-job SYSTEM/root one-shot task/service, pass the exact attempt token through the shared runner, poll only that persisted attempt, and remove the temporary platform unit in `finally` without changing the due scheduler.
- Mode application now stages user and machine config, snapshots machine secrets and both durable files, verifies both readbacks, compensates files/secrets/task on any failed boundary, then updates Tk variables only from the committed result.
- Settings recovery uses the wizard's exact-path plaintext acknowledgement and off-machine warning before worker-owned writing; reveal reads the retained user secret before the machine fallback.
- Focused settings/scheduling/repository checks: `12 passed, 33 deselected`; ruff clean.
