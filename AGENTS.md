# Backer

## Response style — this overrides default verbosity

Keep every response short. TL;DR first, stop there unless asked for more.

- Lead with the answer or the outcome. One or two sentences.
- Then at most 3-5 bullets. No tables, no section headings, no walkthroughs.
- Don't explain what you're about to do, narrate progress, or recap what you
  just did. Don't restate the task back.
- Don't list what you considered and rejected.
- Report test results as one line: `228 passed, 3 skipped`.
- Long output only when explicitly asked ("explain", "full detail", "walk me
  through", "write the doc").

Never shorten by hiding these — but state them in one line each:

- Something failed, was skipped, or is still broken
- A risk of data loss, or a change to deletion/security behaviour
- A decision I made that the user might disagree with

If a fuller explanation exists, offer it in one line: `Say "detail" for the
full breakdown.` Don't pre-emptively include it.

## Project

Open-source backup tool. FastAPI + SQLite server, Avalonia desktop client
(desktop/, C#) + headless Python agent, Kotlin Android client. Kopia is the only backup engine —
restic, rclone and rsync were removed in `704bd8f`.

- Tests: `./.venv/Scripts/python.exe -m pytest -q tests/`
- Lint: `ruff check src/ tests/`
- The pinned kopia binary lives at `~/.local/share/backer/tools/kopia.exe`.
  Run it to check flags rather than assuming — several bugs have come from
  code calling kopia subcommands and flags that don't exist.

This is a backup product. Fail closed on anything that deletes.
