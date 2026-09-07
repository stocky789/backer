"""The CLI surface the desktop GUI drives: schedule, job set, job run --json, keystore, non-TTY confirmations."""

import json
from pathlib import Path, PureWindowsPath

import pytest
from click.testing import CliRunner

from backer.cli import main
from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig


@pytest.fixture
def local_config(tmp_path):
    path = tmp_path / "config.yaml"
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(id="repo", name="repo", type="local", path=str(tmp_path / "store"))},
        jobs={"backup": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path / "src")))},
    )
    config.save(path)
    return path


def test_desktop_command_surface_resolves():
    runner = CliRunner()
    for command in (
        ("schedule", "pause"),
        ("schedule", "resume"),
        ("schedule", "show"),
        ("schedule", "status"),
        ("job", "set"),
        ("keystore", "status"),
    ):
        result = runner.invoke(main, [*command, "--help"])
        assert result.exit_code == 0, result.output


def test_schedule_pause_show_resume_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(main, ["schedule", "pause", "--until", "2030-01-02T03:04:05Z"]).exit_code == 0
    result = runner.invoke(main, ["schedule", "show", "--json"])
    assert json.loads(result.output) == {"paused": True, "until": "2030-01-02T03:04:05Z"}
    assert json.loads((tmp_path / "schedule-runtime.json").read_text())["pause"]["paused"] is True
    assert runner.invoke(main, ["schedule", "resume"]).exit_code == 0
    result = runner.invoke(main, ["schedule", "show", "--json"])
    assert json.loads(result.output) == {"paused": False, "until": None}


def test_schedule_pause_without_until_pauses_indefinitely(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(main, ["schedule", "pause"]).exit_code == 0
    assert json.loads(runner.invoke(main, ["schedule", "show", "--json"]).output) == {"paused": True, "until": None}


def test_schedule_pause_rejects_a_non_iso_time_without_writing(monkeypatch, tmp_path):
    """A parsed deadline is what stops an indefinite pause being written by accident."""
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["schedule", "pause", "--until", "tomorrow"])
    assert result.exit_code == 2
    assert not (tmp_path / "schedule-runtime.json").exists()


def test_schedule_pause_rolls_back_when_the_state_cannot_be_verified(monkeypatch, tmp_path):
    """Losing the rollback would leave the scheduler in an unverified pause state."""
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(main, ["schedule", "pause"]).exit_code == 0
    before = (tmp_path / "schedule-runtime.json").read_bytes()
    monkeypatch.setattr("backer.serverless.schedule.schedule_pause_matches", lambda *_args, **_kwargs: False)
    result = runner.invoke(main, ["schedule", "resume"])
    assert result.exit_code == 1
    assert (tmp_path / "schedule-runtime.json").read_bytes() == before


def test_schedule_status_json_reports_the_platform_trigger(monkeypatch):
    monkeypatch.setattr(
        "backer.client.windows_service.snapshot_local_scheduler",
        lambda: {"platform": "linux", "state": {"backer-local.timer": {"enabled": True, "running": True}}},
    )
    monkeypatch.setattr("backer.serverless.modes.local_schedule_configured", lambda: True)
    result = CliRunner().invoke(main, ["schedule", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "configured": True,
        "platform": "linux",
        "method": "systemd",
        "scope": "user",
        "enabled": True,
        "active": True,
    }


def test_keystore_status_json_discloses_the_file_fallback(monkeypatch):
    """Hiding the fallback would let a passphrase reach a plaintext file unannounced."""
    monkeypatch.setattr("backer.core.keystore.backend_name", lambda: "protected local files")
    monkeypatch.setattr("backer.core.keystore.file_fallback_required", lambda: True)
    result = CliRunner().invoke(main, ["keystore", "status", "--json"])
    assert json.loads(result.output) == {"backend": "protected local files", "file_fallback": True}


def test_job_set_refuses_when_no_change_was_requested(local_config):
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "set", "backup"])
    assert result.exit_code == 2
    assert "Nothing to change" in result.output


def test_job_set_refuses_an_invalid_cron_before_saving(local_config):
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "set", "backup", "--schedule", "nope"])
    assert result.exit_code == 2
    assert BackerConfig.load(local_config).jobs["backup"].schedule is None


def test_job_set_refuses_an_unknown_job(local_config):
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "set", "other", "--disable"])
    assert result.exit_code == 1


def test_job_set_writes_schedule_retention_excludes_and_enablement(local_config):
    result = CliRunner().invoke(
        main,
        [
            "--config", str(local_config), "job", "set", "backup", "--schedule", "0 2 * * *",
            "--keep-last", "7", "--keep-daily", "3", "--exclude", "*.tmp", "--disable", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schedule"]["cron"] == "0 2 * * *"
    assert payload["retention"]["keep_last"] == 7
    assert payload["source"]["excludes"] == ["*.tmp"]
    assert payload["enabled"] is False
    saved = BackerConfig.load(local_config).jobs["backup"]
    assert saved.schedule.cron == "0 2 * * *"
    assert saved.retention.keep_daily == 3
    assert saved.enabled is False


def test_job_set_keeps_untouched_retention_fields(local_config):
    runner = CliRunner()
    runner.invoke(main, ["--config", str(local_config), "job", "set", "backup", "--keep-last", "5"])
    runner.invoke(main, ["--config", str(local_config), "job", "set", "backup", "--keep-weekly", "2"])
    retention = BackerConfig.load(local_config).jobs["backup"].retention
    assert (retention.keep_last, retention.keep_weekly) == (5, 2)


def test_job_set_clears_schedule_and_excludes(local_config):
    runner = CliRunner()
    runner.invoke(
        main,
        ["--config", str(local_config), "job", "set", "backup", "--schedule", "0 2 * * *", "--exclude", "*.tmp"],
    )
    result = runner.invoke(
        main, ["--config", str(local_config), "job", "set", "backup", "--no-schedule", "--clear-excludes"]
    )
    assert result.exit_code == 0, result.output
    saved = BackerConfig.load(local_config).jobs["backup"]
    assert saved.schedule is None
    assert saved.source.excludes == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["--schedule", "0 2 * * *", "--no-schedule"],
        ["--exclude", "*.tmp", "--clear-excludes"],
    ],
)
def test_job_set_refuses_contradictory_flags(local_config, arguments):
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "set", "backup", *arguments])
    assert result.exit_code == 2


def test_job_set_refuses_a_source_already_owned_by_another_job(local_config, tmp_path):
    config = BackerConfig.load(local_config)
    config.jobs["other"] = JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path / "other")))
    config.save(local_config)
    result = CliRunner().invoke(
        main, ["--config", str(local_config), "job", "set", "backup", "--source", str(tmp_path / "other")]
    )
    assert result.exit_code == 1
    assert BackerConfig.load(local_config).jobs["backup"].source.path == str(tmp_path / "src")


def test_job_run_json_prints_the_run_id_before_the_result(local_config, monkeypatch):
    """The desktop app needs the run id to find progress/<run_id>.json while the run is still going."""
    def run_local_job(_config, _name, *, on_run_id=None, **_kwargs):
        on_run_id("20260902T101010Z-abcdefgh")
        return {"run_id": "20260902T101010Z-abcdefgh", "job_name": "backup", "success": True, "errors": []}

    monkeypatch.setattr("backer.serverless.runs.run_local_job", run_local_job)
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup", "--json"])
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert lines[0] == {"run_id": "20260902T101010Z-abcdefgh"}
    assert lines[-1]["ok"] is True
    assert lines[-1]["run_id"] == "20260902T101010Z-abcdefgh"


def test_job_run_json_reports_failure_on_the_last_line_and_exits_nonzero(local_config, monkeypatch):
    def run_local_job(_config, _name, *, on_run_id=None, **_kwargs):
        on_run_id("run-1")
        return {"run_id": "run-1", "job_name": "backup", "success": False, "errors": ["boom"]}

    monkeypatch.setattr("backer.serverless.runs.run_local_job", run_local_job)
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output.splitlines()[1])
    assert payload["ok"] is False
    assert payload["errors"] == ["boom"]


def test_job_run_json_reports_lock_contention_as_json(local_config, monkeypatch):
    monkeypatch.setattr("backer.serverless.runs.run_local_job", lambda *_args, **_kwargs: None)
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output.splitlines()[0])["ok"] is False


def test_job_run_json_requires_a_named_local_job(local_config):
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "--due", "--json"])
    assert result.exit_code == 2
    assert "NAME" in result.output


def test_job_run_without_json_keeps_its_human_output(local_config, monkeypatch):
    monkeypatch.setattr(
        "backer.serverless.runs.run_local_job",
        lambda *_args, **_kwargs: {"run_id": "run-1", "success": True, "errors": []},
    )
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup", "--no-progress"])
    assert result.exit_code == 0, result.output
    assert "run_id" not in result.output


def test_run_local_job_publishes_its_run_id_before_any_other_work(monkeypatch, tmp_path):
    """A run id emitted after the work would leave the desktop app blind for the whole backup."""
    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    from backer.serverless import runs

    seen = []
    config = BackerConfig()
    report = runs._run_local_job(config, "missing", on_run_id=seen.append)
    assert seen == [report["run_id"]]
    assert seen[0].endswith(config.agent_id[:8])


def test_repo_rm_accepts_confirm_name_without_a_tty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    BackerConfig(
        repositories={"r1": RepositoryConfig(name="r1", type="local", path=str(tmp_path), passphrase_ref="ref")}
    ).save(config_path)
    monkeypatch.setattr("backer.core.keystore.get", lambda *_args, **_kwargs: "secret")
    deleted = []
    monkeypatch.setattr("backer.core.keystore.delete", lambda *args, **_kwargs: deleted.append(args))
    export = tmp_path / "recovery.json"
    result = CliRunner().invoke(
        main,
        [
            "--config", str(config_path), "repo", "rm", "r1", "--yes", "--confirm-name", "r1",
            "--passphrase-out", str(export),
        ],
    )
    assert result.exit_code == 0, result.output
    assert deleted
    assert json.loads(export.read_text())["passphrase"] == "secret"
    assert "r1" not in BackerConfig.load(config_path).repositories


def test_repo_rm_refuses_a_mismatched_confirm_name(tmp_path, monkeypatch):
    """A mismatched typed name must never remove the only local access to a repository."""
    config_path = tmp_path / "config.yaml"
    BackerConfig(repositories={"r1": RepositoryConfig(name="r1", type="local", path=str(tmp_path))}).save(config_path)
    deleted = []
    monkeypatch.setattr("backer.core.keystore.delete", lambda *args, **_kwargs: deleted.append(args))
    result = CliRunner().invoke(
        main, ["--config", str(config_path), "repo", "rm", "r1", "--yes", "--confirm-name", "r2"]
    )
    assert result.exit_code == 1
    assert deleted == []
    assert "r1" in BackerConfig.load(config_path).repositories


def test_repo_destroy_wipes_only_verified_smb_folder_before_local_config(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from backer.serverless import repositories

    share = tmp_path / "share"
    target = share / "repo"
    target.mkdir(parents=True)
    (target / "pack").write_text("backup")
    (share / "keep").write_text("sibling")
    config_path = tmp_path / "config.yaml"
    record = RepositoryConfig(
        id="r1",
        name="r1",
        type="smb",
        server="nas",
        share="backups",
        path="repo",
        username="user",
        unique_id="aabb",
        passphrase_ref="pass",
        storage_password_ref="storage",
    )
    BackerConfig(
        repositories={"r1": record},
        jobs={"backup": JobConfig(repository="r1", source=SourceConfig(path=str(tmp_path)))},
    ).save(config_path)
    machine_dir = tmp_path / "machine"
    machine_path = machine_dir / "config.yaml"
    BackerConfig(
        repositories={"r1": record.model_copy(update={"scope": "machine"})},
        jobs={"backup": JobConfig(repository="r1", source=SourceConfig(path=str(tmp_path)))},
    ).save(machine_path)

    @contextmanager
    def mounted(_record, _storage):
        yield record.model_copy(update={"type": "local", "path": str(target)})

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.core.paths.get_machine_config_dir", lambda: machine_dir)
    monkeypatch.setattr("backer.serverless.modes.local_schedule_configured", lambda: False)
    monkeypatch.setattr(repositories, "repository_operation_context", mounted)
    monkeypatch.setattr(repositories, "probe", lambda *_args: ("present", "aabb", ""))
    monkeypatch.setattr(
        "backer.core.keystore.get",
        lambda reference, **_kwargs: {"pass": "secret", "storage": "smb-secret"}.get(reference),
    )
    deleted = []
    monkeypatch.setattr("backer.core.keystore.delete", lambda reference, **_kwargs: deleted.append(reference))
    monkeypatch.setattr("backer.core.keystore.backend_name", lambda: "Secret Service")

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "repo", "destroy", "r1", "--yes", "--confirm-name", "DELETE r1"],
    )

    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert (share / "keep").read_text() == "sibling"
    saved = BackerConfig.load(config_path)
    assert not saved.repositories and not saved.jobs
    machine_saved = BackerConfig.load(machine_path)
    assert not machine_saved.repositories and not machine_saved.jobs
    assert deleted == ["pass", "storage"]


def test_repo_destroy_identity_mismatch_keeps_storage_config_and_secrets(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from backer.serverless import repositories

    target = tmp_path / "share" / "repo"
    target.mkdir(parents=True)
    (target / "pack").write_text("backup")
    config_path = tmp_path / "config.yaml"
    record = RepositoryConfig(
        id="r1",
        name="r1",
        type="smb",
        server="nas",
        share="backups",
        path="repo",
        username="user",
        unique_id="expected",
        passphrase_ref="pass",
        storage_password_ref="storage",
    )
    BackerConfig(repositories={"r1": record}).save(config_path)

    @contextmanager
    def mounted(_record, _storage):
        yield record.model_copy(update={"type": "local", "path": str(target)})

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("backer.serverless.modes.local_schedule_configured", lambda: False)
    monkeypatch.setattr(repositories, "repository_operation_context", mounted)
    monkeypatch.setattr(repositories, "probe", lambda *_args: ("present", "different", ""))
    monkeypatch.setattr(
        "backer.core.keystore.get",
        lambda reference, **_kwargs: {"pass": "secret", "storage": "smb-secret"}.get(reference),
    )
    deleted = []
    monkeypatch.setattr("backer.core.keystore.delete", lambda reference, **_kwargs: deleted.append(reference))

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "repo", "destroy", "r1", "--yes", "--confirm-name", "DELETE r1"],
    )

    assert result.exit_code == 1
    assert "identity changed" in result.output
    assert (target / "pack").read_text() == "backup"
    assert "r1" in BackerConfig.load(config_path).repositories
    assert deleted == []


def test_repo_destroy_refuses_smb_share_root_before_mount(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    BackerConfig(
        repositories={
            "r1": RepositoryConfig(
                id="r1", name="r1", type="smb", server="nas", share="backups", path="", unique_id="id"
            )
        }
    ).save(config_path)
    monkeypatch.setattr("backer.core.keystore.get", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr("backer.serverless.modes.local_schedule_configured", lambda: False)

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "repo", "destroy", "r1", "--yes", "--confirm-name", "DELETE r1"],
    )

    assert result.exit_code == 1
    assert "non-root SMB repository folder" in result.output
    assert "r1" in BackerConfig.load(config_path).repositories


def test_repo_destroy_refuses_while_local_schedule_exists(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    BackerConfig(
        repositories={
            "r1": RepositoryConfig(
                id="r1", name="r1", type="smb", server="nas", share="backups", path="repo", unique_id="id"
            )
        }
    ).save(config_path)
    monkeypatch.setattr("backer.serverless.modes.local_schedule_configured", lambda: True)

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "repo", "destroy", "r1", "--yes", "--confirm-name", "DELETE r1"],
    )

    assert result.exit_code == 1
    assert "Turn off scheduled backups" in result.output
    assert "r1" in BackerConfig.load(config_path).repositories


def test_verify_repair_index_commits_with_yes_off_a_tty(monkeypatch):
    from datetime import datetime

    from backer.backends.base import BackendResult, OperationType

    commits = []

    class Backend:
        def repair_index(self, *_args, **kwargs):
            commits.append(kwargs["commit"])
            return BackendResult(True, OperationType.CHECK, datetime.now(), datetime.now(), output="preview")

    config = type(
        "Config",
        (),
        {"jobs": {"job": type("Job", (), {"repository": "repo"})()}, "repositories": {"repo": object()}},
    )()
    monkeypatch.setattr("backer.core.config.load_config", lambda _path: config)
    monkeypatch.setattr("backer.cli._repository_backend", lambda *_args, **_kwargs: (Backend(), "secret", None))
    monkeypatch.setattr("backer.cli._repository_destination", lambda _record: "repo")
    result = CliRunner().invoke(main, ["verify", "job", "--repair-index", "--yes"])
    assert result.exit_code == 0, result.output
    assert commits == [False, True]


def test_restore_replace_needs_yes_replace_and_never_runs_without_it(tmp_path, monkeypatch):
    """Dropping this guard would let unattended automation move a user's files aside."""
    from backer.cli import _confirm_replace

    destination = tmp_path / "target"
    destination.mkdir()
    (destination / "file.txt").write_text("data")
    monkeypatch.setattr("backer.cli._interactive", lambda: False)
    with pytest.raises(Exception) as error:
        _confirm_replace(destination, "snapshot", False)
    assert "yes-replace" in str(error.value)
    _confirm_replace(destination, "snapshot", True)
    assert (destination / "file.txt").exists()


def test_restore_replace_discloses_the_move_before_proceeding(tmp_path, monkeypatch, capsys):
    from backer.cli import _confirm_replace

    destination = tmp_path / "target"
    destination.mkdir()
    (destination / "file.txt").write_text("data")
    monkeypatch.setattr("backer.cli._interactive", lambda: False)
    _confirm_replace(destination, "snapshot", True)
    output = capsys.readouterr().out
    assert "1 files" in output
    assert ".replaced-" in output


def test_job_set_source_warns_that_history_stays_with_the_old_source(local_config, tmp_path):
    result = CliRunner().invoke(
        main, ["--config", str(local_config), "job", "set", "backup", "--source", str(tmp_path / "moved")]
    )
    assert result.exit_code == 0, result.output
    assert str(tmp_path / "src") in result.output
    assert "Run history and retention stay scoped to the previous source" in result.output


def test_agent_uninstall_service_only_keeps_config_and_data(monkeypatch, tmp_path):
    """The GUI's 'Remove agent service' must never take config.yaml or the run history with it."""
    config_dir, data_dir = tmp_path / "cfg", tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    BackerConfig(
        repositories={"repo": RepositoryConfig(id="repo", name="repo", type="local", path=str(tmp_path / "store"))}
    ).save(config_dir / "config.yaml")
    (data_dir / "runs").mkdir()
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("BACKER_DATA_DIR", str(data_dir))
    monkeypatch.setattr("backer.client.windows_service.is_windows", lambda: False)
    monkeypatch.setattr("backer.cli._remove_agent_systemd_units", lambda: ["System service removed"])

    result = CliRunner().invoke(main, ["agent", "uninstall", "--mode", "server", "--service-only", "--yes"])

    assert result.exit_code == 0, result.output
    assert (config_dir / "config.yaml").is_file()
    assert (data_dir / "runs").is_dir()


@pytest.mark.parametrize("destination", ["/usr/lib/systemd", "/usr", "/", "/home"])
def test_restore_refuses_system_destinations_and_their_children(destination):
    from backer.cli import _restore_destination_allowed

    with pytest.raises(Exception) as failure:
        _restore_destination_allowed(Path(destination), BackerConfig())
    assert "will not restore" in str(failure.value)


def test_restore_allows_a_normal_linux_destination(tmp_path, monkeypatch):
    from backer.cli import _restore_destination_allowed

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "user"))
    destination = tmp_path / "user" / "restore"
    destination.mkdir(parents=True)

    _restore_destination_allowed(destination, BackerConfig())


@pytest.mark.parametrize("destination", ["/etc/nginx", "/var/lib/postgresql", "/home"])
def test_confirm_destination_unlocks_guarded_system_paths(destination):
    from backer.cli import _restore_destination_allowed

    _restore_destination_allowed(Path(destination), BackerConfig(), confirm_destination=destination)


def test_confirm_destination_unlocks_the_users_own_home(tmp_path, monkeypatch):
    from backer.cli import _restore_destination_allowed

    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    with pytest.raises(Exception, match="will not restore"):
        _restore_destination_allowed(home, BackerConfig())
    _restore_destination_allowed(home, BackerConfig(), confirm_destination=str(home))


def test_refusal_message_names_the_confirm_destination_override():
    from backer.cli import _restore_destination_allowed

    with pytest.raises(Exception) as failure:
        _restore_destination_allowed(Path("/etc/nginx"), BackerConfig())
    assert "--confirm-destination" in str(failure.value)


def test_confirm_destination_mismatch_aborts():
    from backer.cli import _restore_destination_allowed

    with pytest.raises(Exception, match="does not match"):
        _restore_destination_allowed(Path("/etc/nginx"), BackerConfig(), confirm_destination="/etc/other")


def test_confirm_destination_never_unlocks_the_filesystem_root_or_a_repository(tmp_path):
    from backer.cli import _restore_destination_allowed
    from backer.core.config import RepositoryConfig

    with pytest.raises(Exception, match="will not restore"):
        _restore_destination_allowed(Path("/"), BackerConfig(), confirm_destination="/")

    repo = tmp_path / "repo"
    repo.mkdir()
    config = BackerConfig(
        repositories={"r1": RepositoryConfig(id="r1", name="r1", type="local", path=str(repo))}
    )
    with pytest.raises(Exception, match="will not restore"):
        _restore_destination_allowed(repo / "sub", config, confirm_destination=str(repo / "sub"))


class _FakeWindowsPath(PureWindowsPath):
    """Windows path semantics on a POSIX test runner; the CLI only expanduser()/resolve()s."""

    def expanduser(self):
        return self

    def resolve(self):
        return self


@pytest.mark.parametrize("destination", ["C:\\", "D:\\", "C:\\Users", "D:\\users"])
def test_restore_refuses_windows_drive_roots_and_the_users_folder(monkeypatch, destination):
    import os
    import types

    from backer.cli import _restore_destination_allowed

    monkeypatch.setattr("backer.cli.os", types.SimpleNamespace(name="nt", environ=os.environ))

    with pytest.raises(Exception) as failure:
        _restore_destination_allowed(_FakeWindowsPath(destination), BackerConfig())
    assert "will not restore" in str(failure.value)


def test_restore_allows_a_folder_inside_windows_users(monkeypatch):
    import os
    import types

    from backer.cli import _restore_destination_allowed

    monkeypatch.setattr("backer.cli.os", types.SimpleNamespace(name="nt", environ=os.environ))

    _restore_destination_allowed(_FakeWindowsPath("C:\\Users\\someone\\restore"), BackerConfig())


def test_restore_refuses_a_windows_program_files_x86_child(monkeypatch, tmp_path):
    import os
    import types

    from backer.cli import _restore_destination_allowed

    program_files = tmp_path / "Program Files (x86)"
    (program_files / "Backer").mkdir(parents=True)
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files))
    monkeypatch.setattr("backer.cli.os", types.SimpleNamespace(name="nt", environ=os.environ))

    with pytest.raises(Exception) as failure:
        _restore_destination_allowed(program_files / "Backer", BackerConfig())
    assert "will not restore" in str(failure.value)


def test_restore_refuses_a_windows_system_root_child(monkeypatch, tmp_path):
    import os
    import types

    from backer.cli import _restore_destination_allowed

    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    monkeypatch.setenv("WINDIR", str(windows))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    # Only the CLI's view of os.name may change: pathlib reads the real one.
    monkeypatch.setattr("backer.cli.os", types.SimpleNamespace(name="nt", environ=os.environ))

    with pytest.raises(Exception) as failure:
        _restore_destination_allowed(windows / "System32", BackerConfig())
    assert "will not restore" in str(failure.value)


def test_job_run_json_keeps_narration_off_stdout(local_config, monkeypatch):
    """A caller parsing stdout must see exactly the run id line and the result line."""
    def run_local_job(_config, _name, *, on_run_id=None, on_progress=None, **_kwargs):
        on_run_id("20260902T101010Z-abcdefgh")
        print("[BACKUP] Starting job 'backup' with backend 'kopia'")
        on_progress(run_id="r", bytes_processed=10, total_bytes=100)
        print("[METADATA] Writing metadata to repository")
        return {"run_id": "20260902T101010Z-abcdefgh", "job_name": "backup", "success": True, "errors": []}

    monkeypatch.setattr("backer.serverless.runs.run_local_job", run_local_job)
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup", "--json"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 2, lines
    assert json.loads(lines[0]) == {"run_id": "20260902T101010Z-abcdefgh"}
    assert json.loads(lines[1])["ok"] is True
    assert "[BACKUP]" in result.stderr and "[METADATA]" in result.stderr


def test_job_run_still_narrates_on_stdout_without_json(local_config, monkeypatch):
    def run_local_job(_config, _name, **_kwargs):
        print("[BACKUP] Starting job 'backup' with backend 'kopia'")
        return {"run_id": "r", "job_name": "backup", "success": True, "errors": []}

    monkeypatch.setattr("backer.serverless.runs.run_local_job", run_local_job)
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup"])

    assert result.exit_code == 0
    assert "[BACKUP]" in result.stdout


def test_job_run_passes_a_cancel_event_and_exits_130_when_cancelled(local_config, monkeypatch):
    """Ctrl-C must leave a cancelled record behind, not a run nobody can explain."""
    seen = {}

    def run_local_job(_config, _name, *, cancel_event=None, **_kwargs):
        seen["cancel_event"] = cancel_event
        return {"run_id": "r", "job_name": "backup", "success": False, "cancelled": True,
                "errors": ["Backup cancelled"]}

    monkeypatch.setattr("backer.serverless.runs.run_local_job", run_local_job)
    result = CliRunner().invoke(main, ["--config", str(local_config), "job", "run", "backup"])

    assert result.exit_code == 130
    assert seen["cancel_event"] is not None and not seen["cancel_event"].is_set()
    assert "Backup cancelled" in result.stdout


def test_sigint_sets_the_cancel_event_and_raises_keyboard_interrupt():
    import os
    import signal

    from backer.cli import _cancel_on_sigint

    with _cancel_on_sigint() as event:
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGINT)
        assert event.is_set()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_snapshots_names_the_job_repository_confusion(local_config):
    result = CliRunner().invoke(main, ["--config", str(local_config), "snapshots", "repo"])

    assert result.exit_code == 1
    assert "No local job named 'repo'" in result.output
    assert "backer snapshots --repo repo" in result.output
