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


def test_generated_passphrase_needs_visible_output_off_tty(tmp_path):
    result = CliRunner().invoke(
        main,
        ["repo", "add", "r1", "--init", "--type", "local", "--path", str(tmp_path), "--generate-passphrase", "--yes"],
    )
    assert result.exit_code == 2
    assert "--passphrase-out FILE or --print-passphrase" in result.output


def test_every_init_step_has_a_flag():
    result = CliRunner().invoke(main, ["init", "--help"])
    for _, flag, prompt, validator in INIT_STEPS:
        assert flag in result.output
        assert prompt
        assert callable(validator)


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


def test_repo_discover_reads_password_only_from_stdin_or_environment():
    result = CliRunner().invoke(
        main, ["repo", "discover", "--host", "nas", "--username", "svc", "--json"],
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
            "init", "--type", "local", "--path", str(tmp_path), "--source", str(tmp_path), "--no-schedule",
            "--passphrase-stdin", "--repo-name", "r1", "--job-name", "j1", "--exclude", "*.tmp",
        ],
        input="passphrase\n",
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1]["name"] == "r1"
    assert calls[0][1]["path"] == str(tmp_path)
    assert calls[1][1]["repository_id"] == "r1"
    assert calls[1][1]["exclude"] == ("*.tmp",)
