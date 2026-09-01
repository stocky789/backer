import shlex

import click
import pytest
from click.testing import CliRunner

from backer.cli import INIT_STEPS, main


def test_serverless_command_surface_resolves():
    runner = CliRunner()
    for command in (
        ("init",),
        ("repo", "add"),
        ("repo", "list"),
        ("repo", "test"),
        ("repo", "unlock"),
        ("repo", "passphrase"),
        ("repo", "rm"),
        ("repo", "discover"),
        ("repo", "adopt"),
        ("repo", "recover"),
        ("job", "create"),
        ("job", "run"),
        ("job", "list"),
        ("job", "show"),
        ("job", "history"),
        ("job", "rm"),
        ("snapshots",),
        ("restore",),
        ("prune",),
        ("verify",),
        ("status",),
    ):
        result = runner.invoke(main, [*command, "--help"])
        assert result.exit_code == 0, result.output


def test_repository_types_are_the_v1_matrix():
    result = CliRunner().invoke(main, ["repo", "add", "--help"])
    assert "[local|smb|s3]" in result.output
    assert "--backend" not in result.output


def test_init_no_tty_names_all_missing_flags():
    result = CliRunner().invoke(main, ["init", "--type", "smb", "--host", "nas.local", "--username", "svc"])
    assert result.exit_code == 2
    assert "--share" in result.output
    assert "--path" in result.output
    assert "--password-stdin" in result.output


@pytest.mark.parametrize(
    ("arguments", "missing"),
    [
        (["--type", "local"], ["--path", "--source", "--schedule or --no-schedule", "--passphrase-stdin"]),
        (
            ["--type", "smb", "--host", "nas", "--username", "svc"],
            ["--path", "--share", "--password-stdin", "--source"],
        ),
        (
            ["--type", "s3", "--bucket", "b"],
            ["--endpoint", "--region", "--access-key-id", "--secret-key-stdin"],
        ),
    ],
)
def test_init_no_tty_aggregates_missing_flags_for_each_repository_type(arguments, missing):
    result = CliRunner().invoke(main, ["init", *arguments])
    assert result.exit_code == 2
    for flag in missing:
        assert flag in result.output


def test_generated_passphrase_needs_visible_output_off_tty(tmp_path):
    result = CliRunner().invoke(
        main,
        ["repo", "add", "r1", "--init", "--type", "local", "--path", str(tmp_path), "--generate-passphrase", "--yes"],
    )
    assert result.exit_code == 2
    assert "--passphrase-out FILE or --print-passphrase" in result.output


def test_every_init_step_has_a_flag():
    result = CliRunner().invoke(main, ["init", "--help"])
    for step in INIT_STEPS:
        if step.flag.startswith("--"):
            assert step.flag in result.output
        assert step.prompt
        assert callable(step.validator)


def test_init_step_table_covers_every_noninteractive_input():
    keys = {step.key for step in INIT_STEPS}
    assert {
        "repository_type",
        "path",
        "host",
        "share",
        "username",
        "password_stdin",
        "password_file",
        "password_env",
        "bucket",
        "prefix",
        "endpoint",
        "region",
        "access_key_id",
        "secret_key_stdin",
        "secret_key_file",
        "secret_key_env",
        "passphrase_stdin",
        "passphrase_file",
        "generate_passphrase",
        "passphrase_out",
        "print_passphrase",
        "update_password",
        "update_passphrase",
        "source",
        "exclude",
        "schedule",
        "no_schedule",
        "keep_last",
        "keep_daily",
        "keep_weekly",
        "keep_monthly",
        "keep_yearly",
        "repo_name",
        "job_name",
        "install",
    } <= keys


def test_prune_checks_confirmation_before_any_delete(monkeypatch):
    calls = []

    def prune_job(*args, **kwargs):
        calls.append(kwargs)
        return 2, ""

    monkeypatch.setattr("backer.serverless.retention.prune_job", prune_job)
    result = CliRunner().invoke(main, ["prune", "job", "--apply"])
    assert result.exit_code == 2
    assert calls == []


def test_repo_rm_refuses_before_mutating_without_typed_name(tmp_path, monkeypatch):
    from backer.core.config import BackerConfig, RepositoryConfig

    config_path = tmp_path / "config.yaml"
    config = BackerConfig(repositories={"r1": RepositoryConfig(name="r1", type="local", path=str(tmp_path))})
    config.save(config_path)
    monkeypatch.setattr("backer.core.keystore.get", lambda *args, **kwargs: "secret")
    deleted = []
    monkeypatch.setattr("backer.core.keystore.delete", lambda *args, **kwargs: deleted.append(args))
    result = CliRunner().invoke(main, ["--config", str(config_path), "repo", "rm", "r1", "--yes"])
    assert result.exit_code == 2
    assert deleted == []
    assert "r1" in BackerConfig.load(config_path).repositories


def test_repository_name_resolves_to_its_canonical_config_key():
    from backer.cli import _resolve_job_repository
    from backer.core.config import BackerConfig, RepositoryConfig

    config = BackerConfig(repositories={"canonical-id": RepositoryConfig(name="display-name", type="local", path="x")})
    assert _resolve_job_repository(config, "canonical-id") == "canonical-id"
    assert _resolve_job_repository(config, "display-name") == "canonical-id"


def test_repo_discover_reads_password_only_from_stdin_or_environment(monkeypatch):
    from backer.core.smb_browse import ShareInfo

    monkeypatch.setattr(
        "backer.core.smb_browse.SMBBrowser.list_shares", lambda *_args: (True, [ShareInfo("Backups", "Disk")])
    )
    result = CliRunner().invoke(
        main,
        ["repo", "discover", "--host", "nas", "--username", "svc", "--json"],
        env={"BACKER_SMB_PASSWORD": "not-in-output"},
    )
    assert result.exit_code == 0
    assert "not-in-output" not in result.output


def test_init_forwards_local_parameters_to_shared_commands(monkeypatch, tmp_path):
    calls = []

    def repo_add(**kwargs):
        calls.append(("repo", kwargs))

    def job_create(**kwargs):
        calls.append(("job", kwargs))

    monkeypatch.setattr("backer.cli.repo_add", repo_add)
    monkeypatch.setattr("backer.cli.job_create", job_create)
    result = CliRunner().invoke(
        main,
        [
            "init",
            "--type",
            "local",
            "--path",
            str(tmp_path),
            "--source",
            str(tmp_path),
            "--no-schedule",
            "--passphrase-stdin",
            "--repo-name",
            "r1",
            "--job-name",
            "j1",
            "--exclude",
            "*.tmp",
        ],
        input="passphrase\n",
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1]["name"] == "r1"
    assert calls[0][1]["path"] == str(tmp_path)
    assert calls[1][1]["repository_id"] == "r1"
    assert calls[1][1]["exclude"] == ("*.tmp",)


def test_init_prints_a_reparseable_safe_command(monkeypatch, tmp_path):
    calls = []
    secret_file = tmp_path / "passphrase"
    secret_file.write_text("secret\n", encoding="utf-8")

    monkeypatch.setattr("backer.cli.repo_add", lambda **kwargs: calls.append(("repo", kwargs)))
    monkeypatch.setattr("backer.cli.job_create", lambda **kwargs: calls.append(("job", kwargs)))
    result = CliRunner().invoke(
        main,
        [
            "init",
            "--type",
            "local",
            "--path",
            str(tmp_path),
            "--source",
            str(tmp_path),
            "--no-schedule",
            "--passphrase-file",
            str(secret_file),
            "--repo-name",
            "r1",
            "--job-name",
            "j1",
        ],
    )
    assert result.exit_code == 0, result.output
    command = next(
        line.removeprefix("Run again: ") for line in result.output.splitlines() if line.startswith("Run again: ")
    )
    assert "secret" not in command
    assert "--passphrase-file" in command
    parsed = CliRunner().invoke(main, shlex.split(command.removeprefix("backer ")), input="")
    assert parsed.exit_code == 0, parsed.output
    assert calls[0][1] == calls[2][1]
    assert calls[1][1] == calls[3][1]


def test_init_s3_accepts_an_empty_prefix(monkeypatch, tmp_path):
    passphrase = tmp_path / "passphrase"
    secret_key = tmp_path / "secret-key"
    passphrase.write_text("passphrase\n", encoding="utf-8")
    secret_key.write_text("secret\n", encoding="utf-8")
    monkeypatch.setattr("backer.cli.repo_add", lambda **_kwargs: None)
    monkeypatch.setattr("backer.cli.job_create", lambda **_kwargs: None)
    result = CliRunner().invoke(
        main,
        [
            "init",
            "--type",
            "s3",
            "--bucket",
            "bucket",
            "--endpoint",
            "https://s3.example",
            "--region",
            "au",
            "--access-key-id",
            "key",
            "--secret-key-file",
            str(secret_key),
            "--source",
            str(tmp_path),
            "--no-schedule",
            "--passphrase-file",
            str(passphrase),
        ],
    )
    assert result.exit_code == 0, result.output


def test_restore_include_refuses_traversal():
    from backer.cli import _restore_include_path

    with pytest.raises(click.UsageError):
        _restore_include_path("../outside")


def test_restore_new_target_is_unique_and_never_an_existing_path(tmp_path):
    from backer.cli import _restore_target

    original = tmp_path / "Documents"
    original.mkdir()
    target = _restore_target(None, "NEW", original)
    assert target.parent == tmp_path
    assert not target.exists()
    target.mkdir()
    assert _restore_target(None, "NEW", original) != target


def test_restore_refuses_existing_new_target_before_any_backend_work(tmp_path):
    from backer.cli import _restore_prepare_destination

    target = tmp_path / "already-here"
    target.mkdir()
    with pytest.raises(click.ClickException, match="must not exist"):
        _restore_prepare_destination(target, "NEW", config=type("Config", (), {"repositories": {}})())


def test_restore_dry_merge_never_creates_its_target(tmp_path):
    from backer.cli import _restore_prepare_destination

    target = tmp_path / "would-be-created"
    _restore_prepare_destination(target, "MERGE", config=type("Config", (), {"repositories": {}})(), dry_run=True)
    assert not target.exists()


def test_restore_refuses_repository_containment_in_both_directions(tmp_path):
    from backer.cli import _restore_prepare_destination

    repository = tmp_path / "repository"
    repository.mkdir()
    config = type("Config", (), {"repositories": {}})()
    with pytest.raises(click.ClickException, match="will not restore"):
        _restore_prepare_destination(repository / "restore", "NEW", config=config, repository_paths=(repository,))
    with pytest.raises(click.ClickException, match="will not restore"):
        _restore_prepare_destination(tmp_path, "NEW", config=config, repository_paths=(repository,))


def test_status_message_maps_known_kopia_failure():
    from backer.core.messages import explain_failure

    assert "passphrase" in explain_failure("invalid repository password").lower()


def test_legacy_recovery_literal_has_one_dedicated_home():
    from backer.core.recovery import LEGACY_FIXED_PASSPHRASE

    assert LEGACY_FIXED_PASSPHRASE == "backer-default-password"


@pytest.mark.parametrize(
    ("arguments", "repository_expectation"),
    [
        (
            ["--type", "local", "--path", "repo"],
            {"repository_type": "local", "path": "repo"},
        ),
        (
            ["--type", "smb", "--host", "nas", "--share", "backups", "--path", "laptop", "--username", "svc"],
            {"repository_type": "smb", "server": "nas", "share": "backups", "path": "laptop", "username": "svc"},
        ),
        (
            [
                "--type",
                "s3",
                "--bucket",
                "bucket",
                "--prefix",
                "laptop",
                "--endpoint",
                "https://s3.example",
                "--region",
                "au",
                "--access-key-id",
                "key",
            ],
            {
                "repository_type": "s3",
                "bucket": "bucket",
                "prefix": "laptop",
                "endpoint": "https://s3.example",
                "region": "au",
                "access_key_id": "key",
            },
        ),
    ],
)
def test_init_forwards_each_repository_type_to_shared_commands(
    monkeypatch, tmp_path, arguments, repository_expectation
):
    calls = []
    passphrase = tmp_path / "passphrase"
    storage = tmp_path / "credential"
    passphrase.write_text("passphrase\n", encoding="utf-8")
    storage.write_text("do-not-render\n", encoding="utf-8")
    monkeypatch.setattr("backer.cli.repo_add", lambda **kwargs: calls.append(("repo", kwargs)))
    monkeypatch.setattr("backer.cli.job_create", lambda **kwargs: calls.append(("job", kwargs)))
    credential = (
        ["--secret-key-file", str(storage)]
        if repository_expectation["repository_type"] == "s3"
        else ["--password-file", str(storage)]
        if repository_expectation["repository_type"] == "smb"
        else []
    )
    result = CliRunner().invoke(
        main,
        [
            "init",
            *arguments,
            *credential,
            "--source",
            str(tmp_path),
            "--no-schedule",
            "--passphrase-file",
            str(passphrase),
        ],
    )
    assert result.exit_code == 0, result.output
    for key, value in repository_expectation.items():
        assert calls[0][1][key] == value
    assert calls[1][1]["source"] == (str(tmp_path),)
    if repository_expectation["repository_type"] != "local":
        command = next(
            line.removeprefix("Run again: ") for line in result.output.splitlines() if line.startswith("Run again: ")
        )
        assert "do-not-render" not in command
        parsed = CliRunner().invoke(main, shlex.split(command.removeprefix("backer ")), input="")
        assert parsed.exit_code == 0, parsed.output
        assert calls[0][1] == calls[2][1]
        assert calls[1][1] == calls[3][1]
