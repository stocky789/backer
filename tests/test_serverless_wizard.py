from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner


def test_folder_name_rejects_paths_and_hidden_names() -> None:
    from backer.client.serverless_wizard import valid_folder_name

    assert valid_folder_name("backup-2026")
    assert not valid_folder_name("../backup")
    assert not valid_folder_name("nested/backup")
    assert not valid_folder_name(".hidden")
    assert not valid_folder_name("x" * 256)


def test_wizard_prompt_groups_are_derived_from_init_steps() -> None:
    from backer.client.serverless_wizard import wizard_steps

    assert [step.key for step in wizard_steps(1)] == ["repository_type"]
    assert {step.key for step in wizard_steps(4)} == {"source", "exclude"}
    assert all(step.prompt for step in wizard_steps(5))
    assert wizard_steps(1)[0].validator("local")
    assert not wizard_steps(1)[0].validator("nfs")


def test_wizard_replay_uses_the_saved_passphrase_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from backer.cli import _render_init_command, main

    values = {
        "repository_type": "local", "path": str(tmp_path), "host": None, "share": None, "username": None,
        "password_stdin": False, "password_file": None, "password_env": False, "bucket": None, "prefix": None,
        "endpoint": None, "region": None, "access_key_id": None, "secret_key_stdin": False, "secret_key_file": None,
        "secret_key_env": False, "source": (str(tmp_path),), "exclude": (), "schedule": None, "no_schedule": True,
        "keep_last": None, "keep_daily": None, "keep_weekly": None, "keep_monthly": None, "keep_yearly": None,
        "repo_name": "repository", "job_name": "backup", "passphrase_stdin": False, "passphrase_file": None,
        "generate_passphrase": True, "passphrase_out": tmp_path / "recovery.txt", "print_passphrase": False,
        "update_password": False, "update_passphrase": False, "install": False,
        "_generated_passphrase": "six-safe-words-for-this-test-only",
    }
    values.update(
        repository_type="local",
        path=str(tmp_path),
        source=(str(tmp_path),),
        no_schedule=True,
        repo_name="repository",
        job_name="backup",
        generate_passphrase=True,
    )

    command = _render_init_command(values)

    assert "--passphrase-file" in command
    assert "--generate-passphrase" not in command
    recovery = tmp_path / "recovery.txt"
    recovery.write_text("six-safe-words-for-this-test-only\n", encoding="utf-8")
    calls: list[str] = []
    runner = CliRunner()
    monkeypatch.setattr("backer.cli.repo_add", lambda **_kwargs: calls.append("repo"))
    monkeypatch.setattr("backer.cli.job_create", lambda **_kwargs: calls.append("job"))
    result = runner.invoke(main, shlex.split(command.removeprefix("backer ")))
    assert result.exit_code == 0, result.output
    assert calls == ["repo", "job"]


def test_wizard_replay_renders_passphrase_file_while_interactive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backer.cli import _render_init_command, main

    monkeypatch.setattr("backer.cli._interactive", lambda: True)
    values = {
        "repository_type": "local", "path": str(tmp_path), "source": (str(tmp_path),), "exclude": (),
        "no_schedule": True, "schedule": None, "repo_name": "repository", "job_name": "backup",
        "generate_passphrase": True, "passphrase_file": None, "passphrase_out": tmp_path / "recovery.txt",
        "print_passphrase": False, "_generated_passphrase": "six-safe-words-for-this-test-only",
    }
    values.update(
        host=None, share=None, username=None, password_stdin=False, password_file=None, password_env=False,
        bucket=None, prefix=None, endpoint=None, region=None, access_key_id=None, secret_key_stdin=False,
        secret_key_file=None, secret_key_env=False, passphrase_stdin=False, keep_last=None, keep_daily=None,
        keep_weekly=None, keep_monthly=None, keep_yearly=None, update_password=False, update_passphrase=False,
        install=False,
    )

    command = _render_init_command(values)
    assert "--passphrase-file" in command
    (tmp_path / "recovery.txt").write_text("six-safe-words-for-this-test-only\n", encoding="utf-8")
    monkeypatch.setattr("backer.cli._interactive", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr("backer.cli.repo_add", lambda **_kwargs: calls.append("repo"))
    monkeypatch.setattr("backer.cli.job_create", lambda **_kwargs: calls.append("job"))

    result = CliRunner().invoke(main, shlex.split(command.removeprefix("backer ")))

    assert result.exit_code == 0, result.output
    assert calls == ["repo", "job"]


def test_wizard_reprompts_when_shared_validator_rejects_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.client.serverless_wizard import _ask_step, wizard_steps

    answers = iter(("not-a-type", "local"))
    monkeypatch.setattr("backer.client.serverless_wizard.Prompt.ask", lambda *_args, **_kwargs: next(answers))

    assert _ask_step(wizard_steps(1)[0]) == "local"


@pytest.mark.parametrize(
    ("group", "key", "answers", "expected"),
    [
        (5, "repo_name", ("", "repository"), "repository"),
        (5, "job_name", ("", "backup"), "backup"),
        (5, "schedule", ("not cron", "0 2 * * *"), "0 2 * * *"),
        (2, "password_stdin", ("", "file-server-secret"), "file-server-secret"),
        (2, "secret_key_stdin", ("", "s3-secret"), "s3-secret"),
    ],
)
def test_wizard_reprompts_each_shared_validator(
    monkeypatch: pytest.MonkeyPatch, group: int, key: str, answers: tuple[str, str], expected: str
) -> None:
    from backer.client.serverless_wizard import _ask_step, _step

    responses = iter(answers)
    monkeypatch.setattr("backer.client.serverless_wizard.Prompt.ask", lambda *_args, **_kwargs: next(responses))

    assert _ask_step(_step(group, key), password="secret" in key) == expected


def test_aborting_passphrase_export_writes_no_recovery_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from backer.client.serverless_wizard import WizardAbortError, _passphrase

    target = tmp_path / "recovery.txt"
    monkeypatch.setattr("backer.client.serverless_wizard.Prompt.ask", lambda *_args, **_kwargs: "")

    with pytest.raises(WizardAbortError):
        _passphrase({})

    assert not target.exists()


def test_local_entries_mark_directories_and_sort_them_first(tmp_path: Path) -> None:
    from backer.client.serverless_wizard import local_entries

    (tmp_path / "z-file").write_text("x", encoding="utf-8")
    (tmp_path / "a-folder").mkdir()

    entries = local_entries(tmp_path)

    assert [(entry.name, entry.is_dir) for entry in entries] == [("a-folder", True), ("z-file", False)]


def test_smb_entries_use_the_shared_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.client.serverless_wizard import smb_entries
    from backer.core.smb_browse import DirectoryEntry

    monkeypatch.setattr(
        "backer.core.smb_browse.SMBBrowser.list_directory",
        lambda *_args, **_kwargs: (True, [DirectoryEntry("folder", True), DirectoryEntry("file", False)]),
    )

    assert [(entry.name, entry.is_dir) for entry in smb_entries("nas", "backups", "folder", "user", "secret")] == [
        ("folder", True),
        ("file", False),
    ]


def test_smb_browser_filters_and_navigates_without_creating_invalid_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from backer.client.serverless_wizard import Entry, browse_smb_directory

    seen: list[str] = []
    created: list[str] = []

    def entries(_server: str, _share: str, path: str, *_args: str) -> list[Entry]:
        seen.append(path)
        return [Entry("folder", True), Entry("file", False)]

    monkeypatch.setattr("backer.client.serverless_wizard.smb_entries", entries)
    monkeypatch.setattr(
        "backer.client.serverless_wizard.smb_create_directory", lambda *_args: created.append("created")
    )
    answers = iter(("n", "bad/name", "/folder", "1", ""))

    assert browse_smb_directory("nas", "backups", "user", "secret", lambda _prompt: next(answers)) == "folder"
    assert seen == ["", "", "", "folder"]
    assert created == []


def test_browser_abort_writes_nothing(tmp_path: Path) -> None:
    from backer.client.serverless_wizard import WizardAbortError, browse_directory, local_entries

    with pytest.raises(WizardAbortError):
        browse_directory(tmp_path, local_entries, lambda _prompt: "q")

    assert list(tmp_path.iterdir()) == []


def test_browser_creates_only_a_valid_folder(tmp_path: Path) -> None:
    from backer.client.serverless_wizard import browse_directory, local_entries

    answers = iter(("n", "bad/name", "n", "accepted", "1", ""))
    result = browse_directory(tmp_path, local_entries, lambda _prompt: next(answers))

    assert result == tmp_path / "accepted"
    assert (tmp_path / "accepted").is_dir()


def test_init_uses_wizard_values_then_shared_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from backer.cli import main

    values = {
        "repository_type": "local", "path": str(tmp_path), "host": None, "share": None, "username": None,
        "password_stdin": False, "password_file": None, "password_env": False, "bucket": None, "prefix": None,
        "endpoint": None, "region": None, "access_key_id": None, "secret_key_stdin": False, "secret_key_file": None,
        "secret_key_env": False, "source": (str(tmp_path),), "exclude": (), "schedule": None, "no_schedule": True,
        "keep_last": None, "keep_daily": None, "keep_weekly": None, "keep_monthly": None, "keep_yearly": None,
        "repo_name": "r1", "job_name": "j1", "passphrase_stdin": False, "passphrase_file": None,
        "generate_passphrase": True, "passphrase_out": None, "print_passphrase": True, "update_password": False,
        "update_passphrase": False, "install": False,
    }
    calls: list[str] = []
    monkeypatch.setattr("backer.cli._interactive", lambda: True)
    monkeypatch.setattr("backer.client.serverless_wizard.run_wizard", lambda _values: values)
    monkeypatch.setattr("backer.cli.repo_add", lambda **_kwargs: calls.append("repo"))
    monkeypatch.setattr("backer.cli.job_create", lambda **_kwargs: calls.append("job"))

    result = CliRunner().invoke(main, ["init"])

    assert result.exit_code == 0, result.output
    assert calls == ["repo", "job"]


def test_init_passes_wizard_only_secret_to_the_shared_repository_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backer.cli import main

    values = {
        "repository_type": "smb", "path": "folder", "host": "nas", "share": "backups", "username": "user",
        "password_stdin": True, "password_file": None, "password_env": False, "bucket": None, "prefix": None,
        "endpoint": None, "region": None, "access_key_id": None, "secret_key_stdin": False, "secret_key_file": None,
        "secret_key_env": False, "source": (str(tmp_path),), "exclude": (), "schedule": None, "no_schedule": True,
        "keep_last": None, "keep_daily": None, "keep_weekly": None, "keep_monthly": None, "keep_yearly": None,
        "repo_name": "r1", "job_name": "j1", "passphrase_stdin": False, "passphrase_file": None,
        "generate_passphrase": True, "passphrase_out": None, "print_passphrase": True, "update_password": False,
        "update_passphrase": False, "install": False, "_storage_password": "secret",
        "_generated_passphrase": "six-safe-words-for-this-test-only",
    }
    received: dict[str, object] = {}
    monkeypatch.setattr("backer.cli._interactive", lambda: True)
    monkeypatch.setattr("backer.client.serverless_wizard.run_wizard", lambda _values: values)
    monkeypatch.setattr("backer.cli.repo_add", lambda **kwargs: received.update(kwargs))
    monkeypatch.setattr("backer.cli.job_create", lambda **_kwargs: None)

    result = CliRunner().invoke(main, ["init"])

    assert result.exit_code == 0, result.output
    assert received["storage_password"] == "secret"
    assert received["generated_passphrase"] == "six-safe-words-for-this-test-only"
