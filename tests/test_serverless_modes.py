"""GUI-free coverage for the logic lifted out of the retired Tk package."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backer.client.windows_service import SchedulerFreezeResult
from backer.core.config import BackerConfig, ClientConfig, JobConfig, RepositoryConfig, SourceConfig
from backer.serverless import modes, scheduled_test
from backer.serverless.cells import PROVEN_SERVERLESS_CELLS, supported_repository_types
from backer.serverless.history import repository_details, repository_history, repository_location, run_summary
from backer.serverless.repositories import (
    confirmation_word,
    connection_conflict_message,
    passphrase_words,
    recovery_record,
    rollback_repository,
    valid_supplied_passphrase,
)


def test_support_map_only_advertises_the_six_ci_cells():
    assert PROVEN_SERVERLESS_CELLS == {
        (platform, kind) for platform in ("linux", "win32") for kind in ("local", "smb", "s3")
    }
    assert supported_repository_types("win32") == ("local", "smb", "s3")
    assert supported_repository_types("linux") == ("local", "smb", "s3")
    assert supported_repository_types("darwin") == ()


def test_unattended_setup_rejects_interactive_only_smb_repositories():
    config = BackerConfig(
        repositories={
            "nas": RepositoryConfig(
                name="NAS",
                type="smb",
                server="nas",
                share="backups",
                username="backup",
                use_existing_session=True,
            )
        }
    )

    assert "interactive-only" in modes.unattended_blocker(config)


def test_server_url_normalization_adds_scheme_and_default_port():
    assert modes.normalize_server_url("backup-box") == "http://backup-box:8420"
    assert modes.normalize_server_url("https://backup-box") == "https://backup-box:8420"
    assert modes.normalize_server_url("https://backup-box:9443/") == "https://backup-box:9443"


def test_settings_update_keeps_credentials_without_a_mode_key():
    saved = modes.settings_update(
        BackerConfig(server=ClientConfig(server_url="http://old", client_id="id", client_secret="secret")), "backup-box"
    )

    assert saved.server.server_url == "http://backup-box:8420"
    assert saved.server.client_id == "id"
    assert saved.server.client_secret == "secret"


def test_local_schedule_configured_reads_the_installed_platform_trigger(monkeypatch):
    monkeypatch.setattr(
        "backer.client.windows_service.snapshot_local_scheduler",
        lambda: {"platform": "windows", "task": {"exists": True}},
    )
    assert modes.local_schedule_configured() is True
    monkeypatch.setattr(
        "backer.client.windows_service.snapshot_local_scheduler", lambda: {"platform": "linux", "units": {"timer": ""}}
    )
    assert modes.local_schedule_configured() is False


def test_mode_apply_returns_one_shape_when_scheduler_snapshot_fails(monkeypatch, tmp_path):
    previous = BackerConfig()
    monkeypatch.setattr(modes, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: tmp_path / "machine")

    def fail_snapshot():
        raise OSError("read")

    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", fail_snapshot)

    result = modes.apply_scheduled_modes(previous, previous, enable_local_schedule=False)

    assert isinstance(result, modes.ModeApplyResult)
    assert result == (False, previous, "read")


def test_mode_apply_restores_real_scheduler_snapshot_after_mutation_failure(monkeypatch, tmp_path):
    previous = BackerConfig()
    restored = []
    monkeypatch.setattr(modes, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr(modes.sys, "platform", "win32")
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: {"actual": "task xml"})
    monkeypatch.setattr("backer.client.windows_service.create_local_scheduled_task", lambda: (False, "create failed"))
    monkeypatch.setattr(
        "backer.client.windows_service.restore_local_scheduler",
        lambda snapshot: restored.append(snapshot) or (True, ""),
    )

    result = modes.apply_scheduled_modes(previous, previous, enable_local_schedule=True)

    assert result == (False, previous, "create failed")
    assert restored == [{"actual": "task xml"}]


def test_mode_apply_leaves_durable_config_unchanged_when_scheduler_is_active(monkeypatch, tmp_path):
    user, machine = tmp_path / "user", tmp_path / "machine"
    previous = BackerConfig()
    previous.save(user / "config.yaml")
    previous.save(machine / "config.yaml")
    before = (user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()
    monkeypatch.setattr(modes, "get_config_dir", lambda: user)
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: machine)
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: {"platform": "windows"})
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(
            False, False, "Local scheduled backup is running; retry after it finishes"
        ),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.create_local_scheduled_task", lambda: pytest.fail("must not mutate task")
    )

    result = modes.apply_scheduled_modes(previous, previous, enable_local_schedule=True)

    assert result == (False, previous, "Local scheduled backup is running; retry after it finishes")
    assert ((user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()) == before


def test_mode_apply_reports_trigger_rollback_failure_after_race(monkeypatch, tmp_path):
    previous = BackerConfig()
    scheduler = {"platform": "windows", "task": {"exists": True, "enabled": True, "running": False}}
    attempts = []
    monkeypatch.setattr(modes, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(False, True, "task could not be re-enabled"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.restore_local_scheduler_trigger",
        lambda snapshot: attempts.append(snapshot) or (False, "enable denied"),
    )

    result = modes.apply_scheduled_modes(previous, previous, enable_local_schedule=False)

    assert result == (False, previous, "task could not be re-enabled; rollback failed: scheduler: enable denied")
    assert attempts == [scheduler]


def test_mode_apply_reports_trigger_restored_on_retry_after_race(monkeypatch, tmp_path):
    previous = BackerConfig()
    monkeypatch.setattr(modes, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: {"platform": "linux"})
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(False, True, "timer could not be restored"),
    )
    monkeypatch.setattr("backer.client.windows_service.restore_local_scheduler_trigger", lambda _snapshot: (True, ""))

    result = modes.apply_scheduled_modes(previous, previous, enable_local_schedule=False)

    assert result == (False, previous, "timer could not be restored; trigger restored on retry")


def test_mode_apply_rolls_back_config_when_final_freeze_check_detects_reactivation(monkeypatch, tmp_path):
    user, machine = tmp_path / "user", tmp_path / "machine"
    previous = BackerConfig()
    previous.save(user / "config.yaml")
    previous.save(machine / "config.yaml")
    before = (user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()
    scheduler = {"platform": "windows", "task": {"exists": True}}
    monkeypatch.setattr(modes, "get_config_dir", lambda: user)
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: machine)
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(True, False, "frozen"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.verify_local_scheduler_frozen",
        lambda _snapshot: SchedulerFreezeResult(False, False, "trigger reactivated"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.create_local_scheduled_task", lambda: pytest.fail("must not mutate scheduler")
    )
    monkeypatch.setattr(
        "backer.serverless.repositories.rescope_secrets_for_system",
        lambda _config: pytest.fail("must not write secrets"),
    )
    monkeypatch.setattr(BackerConfig, "save", lambda *_args: pytest.fail("must not write config"))

    result = modes.apply_scheduled_modes(previous, previous, enable_local_schedule=True)

    assert result == (False, previous, "trigger reactivated")
    assert ((user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()) == before


def test_agent_install_local_rolls_back_through_the_shared_mode_path(monkeypatch, tmp_path):
    """`agent install --mode local` must keep the freeze/verify/rollback path outside the GUI."""
    from click.testing import CliRunner

    from backer.cli import main

    user, machine = tmp_path / "user", tmp_path / "machine"
    BackerConfig().save(user / "config.yaml")
    BackerConfig().save(machine / "config.yaml")
    before = (user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()
    restored = []
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(user))
    monkeypatch.setattr(modes, "get_config_dir", lambda: user)
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: machine)
    monkeypatch.setattr("backer.client.windows_service.is_windows", lambda: False)
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: {"platform": "linux"})
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(True, False, "frozen"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.verify_local_scheduler_frozen",
        lambda _snapshot: SchedulerFreezeResult(True, False, "frozen"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.create_local_systemd_timer", lambda **_kwargs: (False, "linger denied")
    )
    monkeypatch.setattr(
        "backer.client.windows_service.restore_local_scheduler",
        lambda snapshot: restored.append(snapshot) or (True, ""),
    )

    result = CliRunner().invoke(main, ["agent", "install", "--mode", "local", "--method", "systemd"])

    assert result.exit_code == 1
    assert "linger denied" in result.output
    assert restored == [{"platform": "linux"}]
    assert ((user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()) == before


def test_scheduled_attempt_waits_for_selected_job_and_reports_its_failure():
    attempts = iter([[], [{"run_id": "new", "status": "failed", "error_message": "SYSTEM SMB denied"}]])

    result = scheduled_test.wait_for_scheduled_attempt(
        "old", lambda: next(attempts), timeout=1, sleep=lambda _seconds: None
    )

    assert result == (False, "SYSTEM SMB denied")


def test_scheduled_attempt_ignores_another_job_test_token():
    attempts = iter(
        [
            [{"run_id": "other-token", "status": "success"}],
            [{"run_id": "selected-token", "status": "success"}],
        ]
    )

    assert scheduled_test.wait_for_scheduled_attempt(
        None, lambda: next(attempts), token="selected-token", sleep=lambda _x: None
    ) == (True, "Scheduled run completed")


def test_scheduled_test_context_keeps_adversarial_job_name_out_of_privileged_command(monkeypatch, tmp_path):
    name = 'odd " & % $() name'
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="E:/repo", passphrase_ref="pass")},
        jobs={name: JobConfig(repository="repo", source=SourceConfig(path="C:/source"))},
    )
    monkeypatch.setattr(scheduled_test.keystore, "get", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(scheduled_test.keystore, "put", lambda *_args, **_kwargs: "test")
    monkeypatch.setattr(scheduled_test, "test_directory", lambda _token: tmp_path / "0123456789ab")

    directory, refs = scheduled_test.prepare_scheduled_test(config, name, "0123456789ab")

    saved = BackerConfig.load(directory / "config.yaml")
    assert list(saved.jobs) == [name]
    assert refs == ["backer/scheduled-test/0123456789ab/passphrase_ref"]


def test_retry_scheduled_test_cleanup_keeps_context_when_stop_is_not_verified(monkeypatch, tmp_path):
    directory = tmp_path / "scheduled-tests" / "0123456789ab"
    directory.mkdir(parents=True)
    BackerConfig().save(directory / "config.yaml")
    monkeypatch.setattr(scheduled_test, "get_machine_config_dir", lambda: tmp_path)
    monkeypatch.setattr(scheduled_test.sys, "platform", "win32")
    monkeypatch.setattr(
        "backer.client.windows_service.remove_local_scheduled_test_task", lambda _token: (False, "still running")
    )

    assert scheduled_test.retry_scheduled_test_cleanup() == ["0123456789ab: still running"]
    assert directory.exists()


def test_frozen_scheduled_test_dispatches_through_the_cli_binary(monkeypatch):
    from backer.client import windows_service

    calls = []
    monkeypatch.setattr(windows_service, "is_windows", lambda: True)
    monkeypatch.setattr(windows_service, "is_admin", lambda: True)
    monkeypatch.setattr(windows_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows_service.sys, "executable", r"C:\\Program Files\\Backer\\backer.exe")
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    assert windows_service.create_local_scheduled_test_task("0123456789ab") == (True, "BackerLocalTest-0123456789ab")
    create = next(command for command in calls if command[:2] == ["schtasks", "/create"])
    action = create[create.index("/tr") + 1]
    assert "backer.exe" in action
    assert "agent scheduled-test 0123456789ab" in action
    assert "BackerLocalTest" not in action and "&" not in action


def test_hidden_agent_scheduled_test_command_runs_the_entry_logic(monkeypatch):
    from click.testing import CliRunner

    from backer.cli import main

    dispatched = []
    monkeypatch.setattr(scheduled_test, "run", lambda token: dispatched.append(token) or 7)

    result = CliRunner().invoke(main, ["agent", "scheduled-test", "0123456789ab"])

    assert result.exit_code == 7
    assert dispatched == ["0123456789ab"]
    assert "scheduled-test" not in CliRunner().invoke(main, ["agent", "--help"]).output


def test_run_summary_prefers_the_newest_repository_record_and_keeps_size():
    from datetime import UTC, datetime, timedelta

    from backer.core.job import JobRun, JobStatus

    local = JobRun(
        job_name="photos",
        run_id="local",
        status=JobStatus.SUCCESS,
        started_at=datetime.now(UTC) - timedelta(hours=1),
    )
    repository = {
        "status": "failed",
        "started_at": datetime.now(UTC).isoformat(),
        "bytes_transferred": 2048,
    }

    assert run_summary(local, repository) == ("Failed", "2.0 KiB")
    assert run_summary(None, None) == ("Never run", "—")


def test_repository_details_disclose_type_and_keystore_state_without_secret():
    config = BackerConfig(repositories={"repo": RepositoryConfig(name="Archive", type="local", path="E:/Backup")})

    assert repository_details(config, "repo") == "Archive · local · E:/Backup · passphrase unavailable"
    assert repository_details(config, "missing") == "Repository unavailable"


def test_repository_location_covers_local_and_smb_only():
    assert repository_location(RepositoryConfig(name="L", type="local", path="E:/Backup")) == Path("E:/Backup")
    smb = RepositoryConfig(name="S", type="smb", server="nas", share="backups", path="office")
    assert repository_location(smb) == Path(r"\\nas\backups\office")
    assert repository_location(RepositoryConfig(name="C", type="s3", bucket="b")) is None


def test_s3_repository_history_uses_the_sidecar_backend(monkeypatch):
    record = RepositoryConfig(
        name="Cloud",
        type="s3",
        bucket="bucket",
        prefix="prefix",
        endpoint="https://s3.example",
        storage_password_ref="s3",
    )
    monkeypatch.setattr(
        "backer.serverless.history.keystore.get",
        lambda *_args, **_kwargs: json.dumps({"access_key_id": "id", "secret_access_key": "key"}),
    )

    class Sidecar:
        def __init__(self, *_args):
            pass

        def list(self, _prefix):
            return ["prefix/.backer/jobs/photos/runs/new.json"]

        def get(self, key):
            assert key == ".backer/jobs/photos/runs/new.json"
            return b'{"run_id":"new","status":"success","started_at":"2026-01-01T00:00:00Z","bytes_transferred":42}'

    monkeypatch.setattr("backer.serverless.s3_sidecar.S3Sidecar", Sidecar)

    assert repository_history(record, "photos")[0]["run_id"] == "new"


def test_recovery_record_contains_the_details_needed_on_a_new_machine():
    record = recovery_record(
        "Office NAS", r"\\nas\backups\office", "six-safe-words-for-this-test-only", "2026-09-01T00:00:00Z"
    )

    assert "Repository: Office NAS" in record
    assert r"Location: \\nas\backups\office" in record
    assert "Created (UTC): 2026-09-01T00:00:00Z" in record
    assert "Passphrase: six-safe-words-for-this-test-only" in record
    assert r'kopia repository connect filesystem --path "\\nas\backups\office" --no-persist-credentials' in record


def test_user_supplied_passphrase_requires_a_matching_masked_confirmation():
    assert valid_supplied_passphrase("careful words", "careful words")
    assert not valid_supplied_passphrase("careful words", "different words")
    assert not valid_supplied_passphrase("", "")


def test_custom_multiword_passphrase_uses_the_same_position_tokens():
    assert passphrase_words("one two-three") == ["one", "two", "three"]
    assert confirmation_word("one two-three", 2) == "two"


def test_1219_message_names_the_conflicting_connection(monkeypatch):
    monkeypatch.setattr(
        "backer.core.mounts.SMBConnectionManager._find_existing_connection",
        lambda _self, _server: ("\\\\nas\\backups", "backup-user"),
    )
    rendered = connection_conflict_message("nas")
    assert "\\\\nas\\backups" in rendered and "backup-user" in rendered


def test_rollback_repository_removes_config_and_both_secret_scopes(monkeypatch, tmp_path):
    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="Repo", type="local", path="x", passphrase_ref="pass", storage_password_ref="store"
            )
        }
    )
    removed = []
    monkeypatch.setattr(
        "backer.serverless.repositories.keystore.delete",
        lambda key, *, machine_scope: removed.append((key, machine_scope)),
    )

    rollback_repository(config, tmp_path / "config.yaml", "repo")

    assert config.repositories == {}
    assert set(removed) == {("pass", False), ("pass", True), ("store", False), ("store", True)}


def test_rollback_reports_each_failed_secret_or_save_boundary(monkeypatch, tmp_path):
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="x", passphrase_ref="pass")}
    )
    monkeypatch.setattr(
        "backer.serverless.repositories.keystore.delete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")),
    )
    monkeypatch.setattr(BackerConfig, "save", lambda _self, _path: (_ for _ in ()).throw(OSError("disk full")))

    errors = rollback_repository(config, tmp_path / "config.yaml", "repo")

    assert config.repositories == {}
    assert any("locked" in error for error in errors)
    assert any("disk full" in error for error in errors)


def test_schedule_pause_helpers_default_to_the_resolved_data_dir(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from backer.serverless import schedule

    monkeypatch.setenv("BACKER_DATA_DIR", str(tmp_path))
    until = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = schedule.schedule_pause_snapshot()

    schedule.save_schedule_pause(True, until)

    assert schedule.schedule_pause_consensus() == (True, until)
    assert schedule.schedule_pause_matches(True, until)
    assert not schedule.schedule_pause_snapshot_matches(snapshot)
    schedule.restore_schedule_pause(snapshot)
    assert schedule.schedule_pause_consensus() == (False, None)


def _machine_scope_scenario(monkeypatch, tmp_path, store):
    """Enable local scheduling far enough to rescope secrets, then fail at the scheduler."""
    monkeypatch.setattr(modes, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(modes, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr(modes.keystore, "get", lambda reference, **_kwargs: store.get(reference))
    monkeypatch.setattr(modes.keystore, "put", lambda reference, value, **_kwargs: store.__setitem__(reference, value))
    monkeypatch.setattr(modes.keystore, "delete", lambda reference, **_kwargs: store.pop(reference, None))
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: {"platform": "linux"})
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(True, False, "frozen"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.verify_local_scheduler_frozen",
        lambda _snapshot: SchedulerFreezeResult(True, False, "frozen"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.create_local_systemd_timer", lambda **_kwargs: (False, "linger denied")
    )
    monkeypatch.setattr("backer.client.windows_service.restore_local_scheduler", lambda _snapshot: (True, ""))
    return BackerConfig(
        repositories={
            "r1": RepositoryConfig(
                name="R1", type="local", path=str(tmp_path / "store"), passphrase_ref="backer/repo/r1/passphrase"
            )
        }
    )


def test_mode_apply_refusal_before_any_mutation_never_touches_a_secret(monkeypatch, tmp_path):
    """A pure refusal must not run the secret rollback: on Linux that is the only copy."""
    store = {"backer/repo/r1/passphrase": "the-only-copy"}
    touched = []
    _machine_scope_scenario(monkeypatch, tmp_path, store)
    monkeypatch.setattr(modes.keystore, "put", lambda *args, **_kwargs: touched.append(("put", *args)))
    monkeypatch.setattr(modes.keystore, "delete", lambda *args, **_kwargs: touched.append(("delete", *args)))
    config = BackerConfig(
        repositories={
            "r1": RepositoryConfig(
                name="R1",
                type="smb",
                server="nas",
                share="backups",
                username="backup",
                use_existing_session=True,
                passphrase_ref="backer/repo/r1/passphrase",
            )
        }
    )

    result = modes.apply_scheduled_modes(config, config, enable_local_schedule=True)

    assert result.ok is False
    assert "interactive-only" in result.message
    assert "rollback" not in result.message
    assert touched == []
    assert store == {"backer/repo/r1/passphrase": "the-only-copy"}


def test_mode_apply_rollback_restores_the_previous_secret_instead_of_deleting_it(monkeypatch, tmp_path):
    store = {"backer/repo/r1/passphrase": "the-only-copy"}
    config = _machine_scope_scenario(monkeypatch, tmp_path, store)

    result = modes.apply_scheduled_modes(config, config, enable_local_schedule=True)

    assert result.ok is False
    assert result.message == "linger denied"
    assert store == {"backer/repo/r1/passphrase": "the-only-copy"}


def test_mode_apply_failure_returns_the_configuration_as_it_was_before_the_call(monkeypatch, tmp_path):
    """cli.py passes one object as both previous and desired; the failure result must still be the old state."""
    config = _machine_scope_scenario(monkeypatch, tmp_path, {"backer/repo/r1/passphrase": "the-only-copy"})

    result = modes.apply_scheduled_modes(config, config, enable_local_schedule=True)

    assert config.repositories["r1"].scope == "machine"
    assert result.config.repositories["r1"].scope != "machine"


def test_frozen_local_schedule_task_runs_the_cli_binary_not_the_service_exe(monkeypatch):
    from backer.client import windows_service

    calls = []
    monkeypatch.setattr(windows_service, "is_windows", lambda: True)
    monkeypatch.setattr(windows_service, "is_admin", lambda: True)
    monkeypatch.setattr(windows_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows_service.sys, "executable", r"C:\Program Files\Backer\backer.exe")
    monkeypatch.setattr(
        windows_service,
        "get_service_executable_path",
        lambda: pytest.fail("the service exe ignores argv"),
    )
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    assert windows_service.create_local_scheduled_task()[0] is True
    create = next(command for command in calls if command[:2] == ["schtasks", "/create"])
    action = create[create.index("/tr") + 1]
    assert "backer.exe" in action
    assert "backer-agent-service.exe" not in action
    assert "job run --due --no-progress" in action


def test_python_dash_m_backer_starts_the_cli():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "backer", "--help"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_desktop_cell_list_matches_the_python_support_contract():
    """The C# literal cannot drift from cells.py without failing here."""
    source = Path(__file__).resolve().parents[1] / "desktop" / "Backer.Desktop" / "Services" / "Cells.cs"
    assert source.is_file(), f"{source} moved; update this contract test and Cells.cs together"
    literal = re.search(r"All\s*=\s*\{([^}]*)\}", source.read_text(encoding="utf-8"))
    assert literal, "Cells.cs no longer declares an All array"

    assert tuple(re.findall(r'"([^"]+)"', literal.group(1))) == supported_repository_types("win32")


def test_agent_test_schedule_runs_the_prepared_context_and_cleans_up_fail_closed(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from backer.cli import main

    order = []
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path=str(tmp_path / "store"))},
        jobs={"backup": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path / "src")))},
    )
    config.save(tmp_path / "config.yaml")
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.client.windows_service.is_windows", lambda: False)
    monkeypatch.setattr(scheduled_test, "retry_scheduled_test_cleanup", lambda: order.append("retry") or [])
    monkeypatch.setattr(
        scheduled_test,
        "prepare_scheduled_test",
        lambda _config, _name, token: order.append("prepare") or (tmp_path / "context", ["secret"]),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.create_local_systemd_test_service",
        lambda token: order.append("create") or (True, "unit"),
    )
    monkeypatch.setattr(
        scheduled_test,
        "wait_for_scheduled_attempt",
        lambda *_args, **_kwargs: order.append("wait") or (True, "Scheduled run completed"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.remove_local_systemd_test_service",
        lambda token: order.append("stop") or (True, "stopped"),
    )
    monkeypatch.setattr(
        scheduled_test,
        "remove_scheduled_test",
        lambda _directory, refs: order.append(f"remove {refs}") or [],
    )

    result = CliRunner().invoke(main, ["agent", "test-schedule", "backup"])

    assert result.exit_code == 0, result.output
    assert order == ["retry", "prepare", "create", "wait", "stop", "remove ['secret']"]
    assert "Scheduled run completed" in result.output


def test_agent_test_schedule_keeps_the_isolated_secret_when_the_unit_will_not_stop(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from backer.cli import main

    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path=str(tmp_path / "store"))},
        jobs={"backup": JobConfig(repository="repo", source=SourceConfig(path=str(tmp_path / "src")))},
    )
    config.save(tmp_path / "config.yaml")
    monkeypatch.setenv("BACKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("backer.client.windows_service.is_windows", lambda: False)
    cleanups = iter([[], ["token: still active"]])
    monkeypatch.setattr(scheduled_test, "retry_scheduled_test_cleanup", lambda: next(cleanups))
    monkeypatch.setattr(
        scheduled_test,
        "prepare_scheduled_test",
        lambda _config, _name, token: (tmp_path / "context", ["secret"]),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.create_local_systemd_test_service", lambda token: (True, "unit")
    )
    monkeypatch.setattr(
        scheduled_test, "wait_for_scheduled_attempt", lambda *_args, **_kwargs: (True, "Scheduled run completed")
    )
    monkeypatch.setattr(
        "backer.client.windows_service.remove_local_systemd_test_service", lambda token: (False, "still active")
    )
    monkeypatch.setattr(
        scheduled_test, "remove_scheduled_test", lambda *_args: pytest.fail("must not delete an unverified context")
    )

    result = CliRunner().invoke(main, ["agent", "test-schedule", "backup"])

    assert result.exit_code == 1
    assert "still active" in result.output


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT1M</Interval></Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Settings>
    <Enabled>{enabled}</Enabled>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  </Settings>
</Task>
"""


def _schtasks_task_state(monkeypatch, definition):
    import subprocess

    from backer.client import windows_service

    def fake_run(command, **_kwargs):
        stdout = definition if "/xml" in command else "Status: Ready"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(windows_service.subprocess, "run", fake_run)
    return windows_service._windows_task_state("BackerLocalSchedule")


def test_local_scheduled_task_enabled_ignores_the_trigger_enabled_element(monkeypatch):
    """A disabled task with an enabled trigger must read as disabled, or freeze/verify never re-enables."""
    state = _schtasks_task_state(monkeypatch, TASK_XML.format(enabled="false"))

    assert state["enabled"] is False


def test_local_scheduled_task_defaults_to_enabled_when_settings_omit_it(monkeypatch):
    # Task Scheduler omits Settings/Enabled for an enabled task; only the trigger's remains.
    definition = TASK_XML.format(enabled="true").replace("<Enabled>true</Enabled>\n    <Multiple", "<Multiple")

    assert _schtasks_task_state(monkeypatch, definition)["enabled"] is True


def test_unparsable_task_definition_fails_closed(monkeypatch):
    with pytest.raises(OSError):
        _schtasks_task_state(monkeypatch, "not xml at all")
