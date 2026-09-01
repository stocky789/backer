import shlex
from pathlib import Path

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


def test_noninteractive_first_run_creates_runs_and_lists_one_snapshot(monkeypatch, tmp_path):
    from backer.backends.base import BackupDestination
    config_path = tmp_path / "config.yaml"

    def add_repository(config, path, name, record, passphrase, **_kwargs):
        record = record.model_copy(update={"id": "repo", "passphrase_ref": "pass"})
        config.repositories["repo"] = record
        config.save(path)
        assert passphrase == "ultra-secret"
        return "repo", "test"

    class Backend:
        def list_snapshots(self, destination):
            assert isinstance(destination, BackupDestination)
            return [{"id": "snapshot", "full_id": "snapshot", "timestamp": "2026-09-01", "paths": [str(tmp_path)]}]

    monkeypatch.setattr("backer.serverless.repositories.add_repository", add_repository)
    monkeypatch.setattr("backer.serverless.runs.run_local_job", lambda *_args, **_kwargs: {"success": True})
    monkeypatch.setattr("backer.cli._repository_backend", lambda *_args: (Backend(), "phrase", None))
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(config_path), "repo", "add", "repo", "--init", "--type", "local", "--path",
            str(tmp_path), "--passphrase-stdin", "--headless", "--yes",
        ],
        input="ultra-secret\n",
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main,
        [
            "--config", str(config_path), "job", "create", "backup", "--repo", "repo", "--source",
            str(tmp_path), "--no-schedule",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config_path), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config_path), "snapshots", "--repo", "repo", "--json"])
    assert result.exit_code == 0, result.output
    assert [item["id"] for item in __import__("json").loads(result.output)] == ["snapshot"]


def test_noninteractive_first_run_uses_real_config_keystore_and_kopia_boundary(monkeypatch, request, tmp_path):
    from io import StringIO
    from subprocess import CompletedProcess

    from backer.core import keystore
    from backer.core.config import BackerConfig

    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.core.keystore._secret_tool_available", lambda: False)

    def clean_test_secrets() -> None:
        if config_path.exists():
            for record in BackerConfig.load(config_path).repositories.values():
                for reference in (record.passphrase_ref, record.storage_password_ref):
                    if reference:
                        keystore.delete(reference, machine_scope=record.scope == "machine")

    request.addfinalizer(clean_test_secrets)
    source = tmp_path / "source"
    repository = tmp_path / "repository"
    source.mkdir()
    repository.mkdir()
    commands = []
    created = False

    def run(command, **_kwargs):
        nonlocal created
        commands.append(command)
        if command[1:3] == ["repository", "connect"] and not created:
            return CompletedProcess(command, 1, "", "repository not initialized in the provided storage")
        if command[1:3] == ["repository", "create"]:
            created = True
        if command[1:3] == ["repository", "status"]:
            return CompletedProcess(command, 0, '{"uniqueIDHex":"unique"}', "")
        if command[1:3] == ["snapshot", "list"]:
            payload = [{"id": "snapshot-id", "startTime": "2026-09-01", "source": {"path": str(source)}}]
            return CompletedProcess(command, 0, __import__("json").dumps(payload), "")
        return CompletedProcess(command, 0, "", "")

    class Process:
        stdout = StringIO('{"id":"snapshot-id"}\n')
        stderr = StringIO("")

        def wait(self, timeout=None):
            return 0

        def send_signal(self, _signal):
            pass

        def kill(self):
            pass

    monkeypatch.setattr("backer.backends.kopia.KopiaBackend._get_binary", lambda _self, **_kwargs: Path("kopia"))
    monkeypatch.setattr("backer.backends.kopia.subprocess.run", run)
    monkeypatch.setattr("backer.backends.kopia.subprocess.Popen", lambda *_args, **_kwargs: Process())
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", str(config_path), "repo", "add", "repo", "--init", "--type", "local", "--path",
            str(repository), "--passphrase-stdin", "--headless", "--yes",
        ],
        input="ultra-secret\n",
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main,
        [
            "--config", str(config_path), "job", "create", "backup", "--repo", "repo", "--source",
            str(source), "--no-schedule",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config_path), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--config", str(config_path), "snapshots", "--repo", "repo", "--json"])
    assert result.exit_code == 0, result.output
    assert [item["id"] for item in __import__("json").loads(result.output)] == ["snapshot-id"]
    assert "ultra-secret" not in config_path.read_text(encoding="utf-8")
    assert all("ultra-secret" not in argument for command in commands for argument in command)
    assert keystore._file_dir(False).is_relative_to(tmp_path)
    record = next(iter(BackerConfig.load(config_path).repositories.values()))
    assert keystore.get(record.passphrase_ref or "") == "ultra-secret"
    clean_test_secrets()
    assert not keystore._file_dir(False).exists() or not any(keystore._file_dir(False).iterdir())


def test_job_run_sigint_exits_130(monkeypatch, tmp_path):
    from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig

    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="repo", type="local", path=str(tmp_path))},
        jobs={"backup": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path)))},
    )
    config_path = tmp_path / "config.yaml"
    config.save(config_path)
    monkeypatch.setattr(
        "backer.serverless.runs.run_local_job", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt)
    )

    result = CliRunner().invoke(main, ["--config", str(config_path), "job", "run", "backup", "--no-progress"])

    assert result.exit_code == 130


def test_job_run_all_sigint_exits_130(monkeypatch, tmp_path):
    from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig

    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="repo", type="local", path=str(tmp_path))},
        jobs={"backup": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path)))},
    )
    config_path = tmp_path / "config.yaml"
    config.save(config_path)
    monkeypatch.setattr(
        "backer.serverless.runs.run_local_job", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt)
    )

    result = CliRunner().invoke(main, ["--config", str(config_path), "job", "run", "--all", "--no-progress"])

    assert result.exit_code == 130


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


def test_generated_passphrase_uses_six_eff_words_and_recovery_record_is_private(tmp_path):
    from backer.cli import _generated_passphrase, _load_recovery_passphrase, _write_recovery_export

    phrase = _generated_passphrase()
    export = tmp_path / "recovery.json"
    _write_recovery_export(export, "office", "repo-id", "/backups", phrase)

    wordlist = Path(__file__).parents[1] / "src/backer/assets/eff_large_wordlist.txt"
    words = {line.split("\t", 1)[1] for line in wordlist.read_text().splitlines()}
    assert len(phrase.split("-")) == 6
    assert set(phrase.split("-")) <= words
    assert _load_recovery_passphrase(export) == phrase
    if __import__("os").name != "nt":
        assert export.stat().st_mode & 0o077 == 0


def test_eff_wordlist_resolves_from_a_frozen_bundle(monkeypatch, tmp_path):
    from backer.cli import _eff_wordlist_path

    bundled = tmp_path / "backer" / "assets"
    bundled.mkdir(parents=True)
    (bundled / "eff_large_wordlist.txt").write_text("11111\tabacus\n", encoding="utf-8")
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert _eff_wordlist_path() == bundled / "eff_large_wordlist.txt"


def test_agent_spec_packages_eff_wordlist():
    spec = (Path(__file__).parents[1] / "backer-agent.spec").read_text(encoding="utf-8")
    assert "src/backer/assets/eff_large_wordlist.txt" in spec
    assert "backer/assets" in spec


def test_s3_recovery_restore_is_memory_only(monkeypatch, tmp_path):
    passphrase = tmp_path / "passphrase"
    secret = tmp_path / "secret"
    passphrase.write_text("repository phrase\n", encoding="utf-8")
    secret.write_text("s3 secret\n", encoding="utf-8")
    seen = {}

    def probe(self, _path):
        seen.update(self.config)
        return "present", "id"

    monkeypatch.setattr("backer.backends.kopia.KopiaBackend.repository_probe", probe)
    monkeypatch.setattr(
        "backer.backends.kopia.KopiaBackend.list_snapshots",
        lambda *_args: [{"full_id": "snapshot", "timestamp": "2026-01-01", "paths": ["/source"]}],
    )
    config_dir = tmp_path / "unwritten-config"
    result = CliRunner().invoke(
        main,
        [
            "restore", "--from", "s3://bucket/prefix", "--passphrase-file", str(passphrase), "--endpoint",
            "https://s3.example", "--region", "au", "--access-key-id", "key", "--secret-key-file", str(secret),
            "--latest", "--destination", str(tmp_path / "restore"), "--dry-run",
        ],
        env={"BACKER_CONFIG_DIR": str(config_dir)},
    )

    assert result.exit_code == 0, result.output
    assert seen["s3"]["secret_access_key"] == "s3 secret"
    assert not config_dir.exists()


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


def test_prune_preview_discloses_that_it_saved_the_source_policy(monkeypatch):
    monkeypatch.setattr("backer.serverless.retention.prune_job", lambda *_args, **_kwargs: (2, "preview", []))

    result = CliRunner().invoke(main, ["prune", "job"])

    assert result.exit_code == 0, result.output
    assert result.output.lower().count("policy was saved") == 1
    assert "remains saved" in result.output.lower()
    assert "no snapshots were deleted" in result.output.lower()

    result = CliRunner().invoke(main, ["prune", "job", "--json"])

    assert result.exit_code == 0, result.output
    assert __import__("json").loads(result.output)["policy_saved"] is True

    result = CliRunner().invoke(main, ["prune", "job", "--apply", "--yes-remove", "2"])

    assert result.exit_code == 0, result.output
    assert "policy was saved" not in result.output.lower()
    assert "no snapshots were deleted" not in result.output.lower()


def test_prune_list_shows_kopia_expired_snapshot_dates_and_json(monkeypatch):
    snapshots = [{"id": "expire", "timestamp": "2026-09-01T01:00:00Z"}]
    monkeypatch.setattr("backer.serverless.retention.prune_job", lambda *_args, **_kwargs: (1, "preview", snapshots))

    result = CliRunner().invoke(main, ["prune", "job", "--list"])

    assert result.exit_code == 0, result.output
    assert "2026-09-01T01:00:00Z" in result.output
    assert "expire" in result.output

    result = CliRunner().invoke(main, ["prune", "job", "--list", "--json"])

    assert result.exit_code == 0, result.output
    assert __import__("json").loads(result.output)["snapshots"] == snapshots


def test_prune_refuses_delete_when_second_preview_changes(monkeypatch):
    calls = []

    def prune_job(*_args, **kwargs):
        calls.append(kwargs)
        return (2 if len(calls) == 1 else 3), "preview", []

    monkeypatch.setattr("backer.serverless.retention.prune_job", prune_job)
    result = CliRunner().invoke(main, ["prune", "job", "--apply", "--yes-remove", "2"])

    assert result.exit_code != 0
    assert len(calls) == 2
    assert all(not call["apply"] for call in calls)


def test_prune_refuses_delete_when_same_count_preview_has_different_snapshots(monkeypatch):
    calls = []

    def prune_job(*_args, **kwargs):
        calls.append(kwargs)
        return 2, "preview", [{"id": "first" if len(calls) == 1 else "second", "timestamp": "2026-09-01"}]

    monkeypatch.setattr("backer.serverless.retention.prune_job", prune_job)
    result = CliRunner().invoke(main, ["prune", "job", "--apply", "--yes-remove", "2"])

    assert result.exit_code != 0
    assert len(calls) == 2
    assert all(not call["apply"] for call in calls)


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


def test_verify_repair_refuses_to_commit_without_a_tty(monkeypatch, tmp_path):
    """Removing the non-TTY guard could mutate an index from unattended automation."""
    from datetime import datetime

    from backer.backends.base import BackendResult, OperationType

    class Backend:
        def repair_index(self, *_args, **kwargs):
            assert kwargs["commit"] is False
            return BackendResult(True, OperationType.CHECK, datetime.now(), datetime.now(), output="preview")

    config = type(
        "Config",
        (),
        {"jobs": {"job": type("Job", (), {"repository": "repo"})()}, "repositories": {"repo": object()}},
    )()
    monkeypatch.setattr("backer.core.config.load_config", lambda _path: config)
    monkeypatch.setattr("backer.cli._repository_backend", lambda *_args, **_kwargs: (Backend(), "secret", None))
    monkeypatch.setattr("backer.cli._repository_destination", lambda _record: "repo")
    result = CliRunner().invoke(main, ["verify", "job", "--repair-index"])
    assert result.exit_code == 2
    assert "preview" in result.output
    assert "interactive" in result.output.lower()


def test_restore_test_selects_smallest_files_from_snapshot_tree():
    """Changing the snapshot walk would make a health check select current-tree files."""
    from backer.cli import _snapshot_restore_test_files

    class Backend:
        def get_snapshot_files(self, _destination, _snapshot, path=""):
            return {
                "": [{"name": "large.bin", "type": "file", "size": 10}, {"name": "sub", "type": "dir", "size": 0}],
                "sub": [{"name": "small.txt", "type": "file", "size": 1}],
            }[path]

    assert _snapshot_restore_test_files(Backend(), object(), "immutable-id", 1) == [Path("sub/small.txt")]


def test_restore_test_hashes_snapshot_file_and_removes_temporary_target(tmp_path):
    """Removing the hash or cleanup would make restore verification claim more than it proves."""
    from datetime import datetime

    from backer.backends.base import BackendResult, OperationType
    from backer.cli import _run_restore_test

    source = tmp_path / "source"
    source.mkdir()
    (source / "small.txt").write_text("same", encoding="utf-8")
    targets = []

    class Backend:
        def get_snapshot_files(self, _destination, _snapshot, path=""):
            return [{"name": "small.txt", "type": "file", "size": 4}] if not path else []

        def restore(self, _destination, target, **_kwargs):
            targets.append(target)
            (target / "small.txt").write_text("same", encoding="utf-8")
            return BackendResult(True, OperationType.RESTORE, datetime.now(), datetime.now())

    assert _run_restore_test(
        Backend(), object(), {"full_id": "immutable", "paths": [str(source)]}
    ) == (1, 0)
    assert targets and not targets[0].exists()


def test_restore_test_refuses_a_same_named_file_at_the_wrong_relative_path(tmp_path):
    """A basename fallback could compare a different file and falsely pass a restore test."""
    from datetime import datetime

    from backer.backends.base import BackendResult, OperationType
    from backer.cli import _run_restore_test

    source = tmp_path / "source"
    (source / "one").mkdir(parents=True)
    (source / "one" / "same.txt").write_text("same", encoding="utf-8")

    class Backend:
        def get_snapshot_files(self, _destination, _snapshot, path=""):
            return [{"name": "one", "type": "dir", "size": 0}] if not path else [
                {"name": "same.txt", "type": "file", "size": 4}
            ]

        def restore(self, _destination, target, **_kwargs):
            (target / "other").mkdir()
            (target / "other" / "same.txt").write_text("same", encoding="utf-8")
            return BackendResult(True, OperationType.RESTORE, datetime.now(), datetime.now())

    with pytest.raises(click.ClickException, match="did not match"):
        _run_restore_test(Backend(), object(), {"full_id": "immutable", "paths": [str(source)]})


def test_verify_warns_before_starting_a_sampled_content_check(monkeypatch):
    """Moving the warning after check() would start a costly download without notice."""
    from datetime import datetime

    from backer.backends.base import BackendResult, OperationType

    events = []

    class Backend:
        def check(self, *_args, **_kwargs):
            events.append("check")
            return BackendResult(True, OperationType.CHECK, datetime.now(), datetime.now())

    config = type(
        "Config",
        (),
        {"jobs": {"job": type("Job", (), {"repository": "repo"})()}, "repositories": {"repo": object()}},
    )()
    monkeypatch.setattr("backer.core.config.load_config", lambda _path: config)
    monkeypatch.setattr("backer.cli._repository_backend", lambda *_args, **_kwargs: (Backend(), "secret", None))
    monkeypatch.setattr("backer.cli._repository_destination", lambda _record: "repo")
    original_echo = click.echo

    def echo(message=None, **kwargs):
        events.append(str(message))
        return original_echo(message, **kwargs)

    monkeypatch.setattr("backer.cli.click.echo", echo)
    result = CliRunner().invoke(main, ["verify", "job", "--verify-files-percent", "5"])
    assert result.exit_code == 0, result.output
    assert "downloads and rehashes" in events[0].lower()
    assert events.index("check") > 0


def test_legacy_recovery_literal_has_one_dedicated_home():
    from backer.core.recovery import LEGACY_FIXED_PASSPHRASE

    source_root = Path(__file__).parents[1] / "src" / "backer"
    shipped_source = [
        path for path in source_root.rglob("*.py") if "__pycache__" not in path.parts
    ]
    matches = [path for path in shipped_source if LEGACY_FIXED_PASSPHRASE in path.read_text(encoding="utf-8")]
    assert matches == [source_root / "core" / "recovery.py"]


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
