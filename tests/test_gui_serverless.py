"""Desktop acceptance guardrails; Tk is deliberately required, never skipped."""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "src" / "backer" / "agent" / "gui"


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_gui_tests_actually_ran():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.update()


def test_no_colour_literals_and_tokens_meet_contrast():
    from backer.agent.gui.theme import DARK, LIGHT, resolve_tokens

    source = "\n".join(path.read_text() for path in ROOT.glob("*.py"))
    assert "foreground='" not in source and 'foreground="' not in source
    assert "background='" not in source and 'background="' not in source
    assert resolve_tokens("#101214").mode == "dark"
    assert resolve_tokens("#f5f5f5").mode == "light"
    for tokens in (DARK, LIGHT):
        assert tokens.text != tokens.surface


def test_show_leaves_one_child_and_constructs_no_toplevel():
    source = _source("app.py")
    assert "def _show(" in source
    assert "ttk.Notebook" not in source and "tk.Toplevel" not in source
    assert "pack_forget" in source and "<Escape>" in source


def test_share_listing_does_not_block_the_event_loop():
    source = _source("wizard.py")
    assert "threading.Thread(target=worker, daemon=True).start()" in source
    assert "self._marshal(generation, done)" in source
    assert "generation != self._generation" in source


def test_passphrase_step_requires_confirmation():
    source = _source("wizard.py")
    assert "_generated_passphrase" in source
    assert "I have saved this somewhere other than this computer" in source
    assert "self.primary.configure(state=tk.DISABLED)" in source


def test_passphrase_generation_uses_a_separate_reveal_and_confirmation_frame():
    source = _source("wizard.py")
    assert "passphrase_frames" in source
    assert "_show_passphrase_frame" in source
    assert "Recovery copy" in source
    assert "keystore.backend_name()" in source


def test_1219_actions_use_the_named_connection_without_silent_teardown():
    source = _source("wizard.py")
    assert "connect_existing_serverless" in source
    assert "disconnect_existing_connection" in source
    assert "confirm_remove_repository(connection)" in source


def test_unattended_setup_rejects_interactive_only_smb_repositories():
    from backer.agent.gui.views import unattended_blocker
    from backer.core.config import BackerConfig, RepositoryConfig

    config = BackerConfig(
        repositories={
            "nas": RepositoryConfig(
                name="NAS", type="smb", server="nas", share="backups", username="backup",
                use_existing_session=True,
            )
        }
    )

    assert "interactive-only" in unattended_blocker(config)


def test_home_run_summary_prefers_the_newest_repository_record_and_keeps_size():
    from datetime import UTC, datetime, timedelta

    from backer.agent.gui.views import run_summary
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


def test_settings_save_keeps_registered_credentials_when_only_url_changes(monkeypatch):
    from backer.agent.gui import views
    from backer.agent.gui.views import SettingsView
    from backer.core.config import BackerConfig, ClientConfig

    saved = []
    app = type(
        "App",
        (),
        {
            "config": BackerConfig(
                server=ClientConfig(server_url="http://old", client_id="id", client_secret="secret")
            ),
            "set_status": lambda _self, message, **_kwargs: saved.append(message),
            "apply_theme": lambda *_args: None,
        },
    )()
    instance = object.__new__(SettingsView)
    instance.app = app
    instance.server = type("Value", (), {"get": lambda _self: "http://new"})()
    instance.mode = type("Value", (), {"get": lambda _self: "system"})()
    instance.local_mode = type("Value", (), {"get": lambda _self: False})()
    instance.server_mode = type("Value", (), {"get": lambda _self: True})()
    instance._apply_modes = lambda _previous, updated: setattr(app, "config", updated)
    monkeypatch.setattr(views, "save_config", lambda config: saved.append(config.server.server_url))

    instance._save()

    assert app.config.server.server_url == "http://new:8420"
    assert app.config.server.client_id == "id"
    assert app.config.server.client_secret == "secret"


def test_unified_config_persists_both_scheduled_modes():
    from backer.core.config import BackerConfig

    config = BackerConfig(local_scheduled_mode=True, server_agent_mode=True)

    assert config.model_dump()["local_scheduled_mode"] is True
    assert config.model_dump()["server_agent_mode"] is True


def test_server_url_normalization_adds_scheme_and_default_port():
    from backer.agent.gui.views import normalize_server_url

    assert normalize_server_url("backup-box") == "http://backup-box:8420"
    assert normalize_server_url("https://backup-box") == "https://backup-box:8420"
    assert normalize_server_url("https://backup-box:9443/") == "https://backup-box:9443"


def test_settings_update_keeps_credentials_and_both_enabled_modes():
    from backer.agent.gui.views import settings_update
    from backer.core.config import BackerConfig, ClientConfig

    saved = settings_update(
        BackerConfig(server=ClientConfig(server_url="http://old", client_id="id", client_secret="secret")),
        "backup-box",
        local_scheduled_mode=True,
        server_agent_mode=True,
    )

    assert saved.server.server_url == "http://backup-box:8420"
    assert saved.server.client_id == "id"
    assert saved.server.client_secret == "secret"
    assert saved.local_scheduled_mode and saved.server_agent_mode


def test_scheduled_attempt_waits_for_selected_job_and_reports_its_failure():
    from backer.agent.gui.views import wait_for_scheduled_attempt

    attempts = iter([[], [{"run_id": "new", "status": "failed", "error_message": "SYSTEM SMB denied"}]])

    result = wait_for_scheduled_attempt("old", lambda: next(attempts), timeout=1, sleep=lambda _seconds: None)

    assert result == (False, "SYSTEM SMB denied")


def test_scheduled_attempt_ignores_another_job_test_token():
    from backer.agent.gui.views import wait_for_scheduled_attempt

    attempts = iter(
        [
            [{"run_id": "other-token", "status": "success"}],
            [{"run_id": "selected-token", "status": "success"}],
        ]
    )

    assert wait_for_scheduled_attempt(None, lambda: next(attempts), token="selected-token", sleep=lambda _x: None) == (
        True,
        "Scheduled run completed",
    )


def test_scheduled_test_context_keeps_adversarial_job_name_out_of_privileged_command(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.agent.gui.views import prepare_scheduled_test
    from backer.core.config import BackerConfig, JobConfig, RepositoryConfig, SourceConfig

    name = 'odd " & % $() name'
    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="E:/repo", passphrase_ref="pass")},
        jobs={name: JobConfig(repository="repo", source=SourceConfig(path="C:/source"))},
    )
    monkeypatch.setattr(views.keystore, "get", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(views.keystore, "put", lambda *_args, **_kwargs: "test")
    monkeypatch.setattr("backer.serverless.scheduled_test.test_directory", lambda _token: tmp_path / "0123456789ab")

    directory, refs = prepare_scheduled_test(config, name, "0123456789ab")

    saved = BackerConfig.load(directory / "config.yaml")
    assert list(saved.jobs) == [name]
    assert refs == ["backer/scheduled-test/0123456789ab/passphrase_ref"]


def test_frozen_scheduled_test_uses_gui_entrypoint_token_and_packages_dispatch(monkeypatch):
    from backer.agent.gui import app
    from backer.client import windows_service
    from backer.serverless import scheduled_test

    calls = []
    monkeypatch.setattr(windows_service, "is_windows", lambda: True)
    monkeypatch.setattr(windows_service, "is_admin", lambda: True)
    monkeypatch.setattr(windows_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(windows_service.sys, "executable", r"C:\\Program Files\\Backer\\backer-agent.exe")
    monkeypatch.setattr(
        windows_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    assert windows_service.create_local_scheduled_test_task("0123456789ab") == (True, "BackerLocalTest-0123456789ab")
    create = next(command for command in calls if command[:2] == ["schtasks", "/create"])
    action = create[create.index("/tr") + 1]
    assert r"backer-agent.exe" in action
    assert "scheduled-test 0123456789ab" in action
    assert "BackerLocalTest" not in action and "&" not in action
    dispatched = []
    monkeypatch.setattr(scheduled_test, "run", lambda token: dispatched.append(token) or 7)
    assert app._scheduled_test_command(["scheduled-test", "0123456789ab"]) == 7
    assert dispatched == ["0123456789ab"]
    spec = Path("backer-agent.spec").read_text(encoding="utf-8")
    assert "backer.serverless.scheduled_test" in spec


def test_mode_apply_returns_one_shape_when_scheduler_snapshot_fails(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.core.config import BackerConfig

    previous = BackerConfig()
    monkeypatch.setattr(views, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: tmp_path / "machine")
    def fail_snapshot():
        raise OSError("read")

    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", fail_snapshot)

    result = views.apply_scheduled_modes(previous, previous)

    assert isinstance(result, views.ModeApplyResult)
    assert result == (False, previous, "read")


def test_mode_apply_restores_real_scheduler_snapshot_after_mutation_failure(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.core.config import BackerConfig

    previous = BackerConfig()
    desired = previous.model_copy(update={"local_scheduled_mode": True})
    restored = []
    monkeypatch.setattr(views, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr(views.sys, "platform", "win32")
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: {"actual": "task xml"})
    monkeypatch.setattr("backer.client.windows_service.create_local_scheduled_task", lambda: (False, "create failed"))
    def restore(snapshot):
        restored.append(snapshot)
        return True, ""

    monkeypatch.setattr("backer.client.windows_service.restore_local_scheduler", restore)

    result = views.apply_scheduled_modes(previous, desired)

    assert result == (False, previous, "create failed")
    assert restored == [{"actual": "task xml"}]


def test_retry_scheduled_test_cleanup_keeps_context_when_stop_is_not_verified(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.core.config import BackerConfig

    directory = tmp_path / "scheduled-tests" / "0123456789ab"
    directory.mkdir(parents=True)
    BackerConfig().save(directory / "config.yaml")
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: tmp_path)
    monkeypatch.setattr(views.sys, "platform", "win32")
    monkeypatch.setattr(
        "backer.client.windows_service.remove_local_scheduled_test_task", lambda _token: (False, "still running")
    )

    assert views.retry_scheduled_test_cleanup() == ["0123456789ab: still running"]
    assert directory.exists()


def test_mode_apply_leaves_durable_config_unchanged_when_scheduler_is_active(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.client.windows_service import SchedulerFreezeResult
    from backer.core.config import BackerConfig

    user, machine = tmp_path / "user", tmp_path / "machine"
    previous = BackerConfig()
    previous.save(user / "config.yaml")
    previous.save(machine / "config.yaml")
    desired = previous.model_copy(update={"local_scheduled_mode": True})
    before = (user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()
    monkeypatch.setattr(views, "get_config_dir", lambda: user)
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: machine)
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

    result = views.apply_scheduled_modes(previous, desired)

    assert result == (False, previous, "Local scheduled backup is running; retry after it finishes")
    assert ((user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()) == before


def test_mode_apply_reports_trigger_rollback_failure_after_race(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.client.windows_service import SchedulerFreezeResult
    from backer.core.config import BackerConfig

    previous = BackerConfig()
    scheduler = {"platform": "windows", "task": {"exists": True, "enabled": True, "running": False}}
    attempts = []
    monkeypatch.setattr(views, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(False, True, "task could not be re-enabled"),
    )
    monkeypatch.setattr(
        "backer.client.windows_service.restore_local_scheduler_trigger",
        lambda snapshot: attempts.append(snapshot) or (False, "enable denied"),
    )

    result = views.apply_scheduled_modes(previous, previous)

    assert result == (False, previous, "task could not be re-enabled; rollback failed: scheduler: enable denied")
    assert attempts == [scheduler]


def test_mode_apply_reports_trigger_restored_on_retry_after_race(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.client.windows_service import SchedulerFreezeResult
    from backer.core.config import BackerConfig

    previous = BackerConfig()
    scheduler = {"platform": "linux"}
    monkeypatch.setattr(views, "get_config_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: tmp_path / "machine")
    monkeypatch.setattr("backer.client.windows_service.snapshot_local_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "backer.client.windows_service.prepare_local_scheduler_mutation",
        lambda _snapshot: SchedulerFreezeResult(False, True, "timer could not be restored"),
    )
    monkeypatch.setattr("backer.client.windows_service.restore_local_scheduler_trigger", lambda _snapshot: (True, ""))

    result = views.apply_scheduled_modes(previous, previous)

    assert result == (False, previous, "timer could not be restored; trigger restored on retry")


def test_mode_apply_rolls_back_config_when_final_freeze_check_detects_reactivation(monkeypatch, tmp_path):
    from backer.agent.gui import views
    from backer.client.windows_service import SchedulerFreezeResult
    from backer.core.config import BackerConfig

    user, machine = tmp_path / "user", tmp_path / "machine"
    previous = BackerConfig()
    previous.save(user / "config.yaml")
    previous.save(machine / "config.yaml")
    desired = previous.model_copy(update={"local_scheduled_mode": True})
    before = (user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()
    scheduler = {"platform": "windows", "task": {"exists": True}}
    monkeypatch.setattr(views, "get_config_dir", lambda: user)
    monkeypatch.setattr(views, "get_machine_config_dir", lambda: machine)
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

    result = views.apply_scheduled_modes(previous, desired)

    assert result == (False, previous, "trigger reactivated")
    assert ((user / "config.yaml").read_bytes(), (machine / "config.yaml").read_bytes()) == before

def test_repository_details_disclose_type_and_keystore_state_without_secret():
    from backer.agent.gui.views import repository_details
    from backer.core.config import BackerConfig, RepositoryConfig

    config = BackerConfig(repositories={"repo": RepositoryConfig(name="Archive", type="local", path="E:/Backup")})

    assert repository_details(config, "repo") == "Archive · local · E:/Backup · passphrase unavailable"


def test_settings_service_probe_is_dispatched_to_a_worker():
    from backer.agent.gui.views import SettingsView

    calls = []
    instance = object.__new__(SettingsView)
    instance._worker = lambda name, work, done=None: calls.append(name)

    instance.service_status()

    assert calls == ["service-status"]


def test_s3_repository_history_uses_the_sidecar_backend(monkeypatch):
    import json

    from backer.agent.gui import views
    from backer.agent.gui.views import repository_history
    from backer.core.config import RepositoryConfig

    record = RepositoryConfig(
        name="Cloud",
        type="s3",
        bucket="bucket",
        prefix="prefix",
        endpoint="https://s3.example",
        storage_password_ref="s3",
    )
    monkeypatch.setattr(
        views.keystore,
        "get",
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
    from backer.agent.gui.wizard import recovery_record

    record = recovery_record(
        "Office NAS", r"\\nas\backups\office", "six-safe-words-for-this-test-only", "2026-09-01T00:00:00Z"
    )

    assert "Repository: Office NAS" in record
    assert r"Location: \\nas\backups\office" in record
    assert "Created (UTC): 2026-09-01T00:00:00Z" in record
    assert "Passphrase: six-safe-words-for-this-test-only" in record
    assert r'kopia repository connect filesystem --path "\\nas\backups\office" --no-persist-credentials' in record


def test_recovery_commands_keep_credentials_external_for_every_storage_type():
    from backer.agent.gui.wizard import RepositoryWizard, recovery_record
    from backer.core.config import RepositoryConfig

    local = RepositoryConfig(name="Local", type="local", path="E:/Backups")
    smb = RepositoryConfig(name="SMB", type="smb", server="nas", share="backups", path="office")
    s3 = RepositoryConfig(
        name="S3", type="s3", bucket="bucket", prefix="office", endpoint="https://s3.example", region="au"
    )

    for record in (local, smb, s3):
        location = RepositoryWizard._recovery_location(record)
        command, instruction = RepositoryWizard._recovery_command(record, location)
        content = recovery_record(record.name, location, "passphrase", "2026-09-01T00:00:00Z", command, instruction)
        assert "--no-persist-credentials" in command
        assert "access-key-value" not in content and "secret-key-value" not in content
    assert "AWS_ACCESS_KEY_ID" in content and "AWS_SECRET_ACCESS_KEY" in content


def test_user_supplied_passphrase_requires_a_matching_masked_confirmation():
    from backer.agent.gui.wizard import valid_supplied_passphrase

    assert valid_supplied_passphrase("careful words", "careful words")
    assert not valid_supplied_passphrase("careful words", "different words")
    assert not valid_supplied_passphrase("", "")


def test_custom_multiword_passphrase_uses_the_same_position_tokens():
    from backer.agent.gui.wizard import confirmation_word, passphrase_words

    assert passphrase_words("one two-three") == ["one", "two", "three"]
    assert confirmation_word("one two-three", 2) == "two"


def test_share_listing_routes_1219_to_the_same_named_action_panel():
    from backer.agent.gui.wizard import RepositoryWizard

    seen = []
    instance = object.__new__(RepositoryWizard)
    instance._generation = 4
    instance.cancel = type("Cancel", (), {"is_set": lambda _self: False})()
    instance.values = {"server": "nas"}
    instance._show_1219 = lambda server, error: seen.append((server, error))

    instance._apply_share_listing(4, False, "system error 1219")

    assert seen == [("nas", "system error 1219")]


def test_out_of_order_attach_probe_cannot_enable_the_newer_candidate():
    from backer.agent.gui.wizard import RepositoryWizard

    calls = []
    candidate = type("Candidate", (), {"get": lambda _self: "wrong-passphrase"})()
    instance = object.__new__(RepositoryWizard)
    instance._generation = 7
    instance._passphrase_probe_token = 2
    instance.cancel = type("Cancel", (), {"is_set": lambda _self: False})()
    instance._attach_candidate = candidate
    instance.primary = type("Button", (), {"configure": lambda _self, **kwargs: calls.append(kwargs)})()

    instance._apply_attach_probe(7, 1, "right-passphrase", "present", "")

    assert calls == []


def test_current_failed_attach_probe_clears_and_refocuses_before_correct_retry():
    from backer.agent.gui.wizard import RepositoryWizard

    calls, statuses = [], []

    class Candidate:
        value = "wrong"

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    candidate = Candidate()
    instance = object.__new__(RepositoryWizard)
    instance._generation = 1
    instance._passphrase_probe_token = 1
    instance.cancel = type("Cancel", (), {"is_set": lambda _self: False})()
    instance._attach_candidate = candidate
    instance._attach_entry = type("Entry", (), {"focus_set": lambda _self: calls.append("focus")})()
    instance.primary = type("Button", (), {"configure": lambda _self, **kwargs: calls.append(kwargs)})()
    status = type("Status", (), {"set": lambda _self, value: statuses.append(value)})()

    instance._apply_attach_probe(1, 1, "wrong", "wrong_passphrase", "Rejected", status)

    assert candidate.value == ""
    assert "focus" in calls and {"state": "normal"} in calls
    assert statuses[-1] == "Rejected"

    candidate.set("correct")
    instance._passphrase_probe_token = 2
    instance._apply_attach_probe(1, 2, "correct", "present", "", status)

    assert calls[-1] == {"state": "normal"}


def test_save_recovery_record_writes_complete_record_and_copy_acknowledges_clipboard(monkeypatch, tmp_path):
    from backer.agent.gui import wizard
    from backer.core.config import RepositoryConfig

    target = tmp_path / "recovery.txt"
    events = []
    instance = object.__new__(wizard.RepositoryWizard)
    instance._record = lambda: RepositoryConfig(name="Office", type="local", path="E:/Backups")
    instance.app = type(
        "App",
        (),
        {
            "root": type(
                "Root",
                (),
                {
                    "clipboard_clear": lambda _self: events.append("clear"),
                    "clipboard_append": lambda _self, value: events.append(value),
            },
            )(),
            "set_status": lambda _self, value, **_kwargs: events.append(value),
        },
    )()
    instance._recovery_target = target
    instance._recovery_ack = type("Ack", (), {"get": lambda _self: True})()

    assert instance._save_recovery_record("six-safe-words") == target
    instance._copy_passphrase("six-safe-words")

    content = target.read_text(encoding="utf-8")
    assert "Repository: Office" in content and "Location: E:/Backups" in content
    assert "Passphrase: six-safe-words" in content and "kopia repository connect filesystem" in content
    assert any("clipboard is not durable" in event.lower() for event in events if isinstance(event, str))


def test_recovery_save_never_invokes_a_messagebox_confirmation(monkeypatch, tmp_path):
    from backer.agent.gui import wizard
    from backer.core.config import RepositoryConfig

    instance = object.__new__(wizard.RepositoryWizard)
    instance._record = lambda: RepositoryConfig(name="Office", type="local", path="E:/Backups")
    instance._recovery_target = tmp_path / "recovery.txt"
    instance._recovery_ack = type("Ack", (), {"get": lambda _self: True})()
    instance.app = type(
        "App",
        (),
        {
            "set_status": lambda *_args, **_kwargs: None,
            "confirm_reveal_passphrase": lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("modal")),
        },
    )()

    assert instance._save_recovery_record("six-safe-words") == instance._recovery_target


def test_recovery_location_requires_an_inline_plaintext_acknowledgement(monkeypatch, tmp_path):
    from backer.agent.gui import wizard

    target = tmp_path / "recovery.txt"
    warning, button = [], []
    instance = object.__new__(wizard.RepositoryWizard)
    instance._recovery_ack = type(
        "Ack", (), {"set": lambda _self, value: warning.append(value), "get": lambda _self: False}
    )()
    instance._recovery_warning = type("Warning", (), {"set": lambda _self, value: warning.append(value)})()
    instance._recovery_save_button = type("Button", (), {"configure": lambda _self, **kwargs: button.append(kwargs)})()
    monkeypatch.setattr(wizard.filedialog, "asksaveasfilename", lambda **_kwargs: str(target))

    instance._choose_recovery_record()

    assert instance._recovery_target == target
    assert warning[0] is False and str(target) in warning[1] and "plain text" in warning[1]
    assert button == [{"state": wizard.tk.DISABLED}]


def test_recovery_save_write_or_chmod_failure_is_statused_and_can_retry(monkeypatch, tmp_path):
    from backer.agent.gui import wizard
    from backer.core.config import RepositoryConfig

    events = []
    target = tmp_path / "recovery.txt"
    instance = object.__new__(wizard.RepositoryWizard)
    instance._record = lambda: RepositoryConfig(name="Office", type="local", path="E:/Backups")
    instance._recovery_target = target
    instance._recovery_ack = type("Ack", (), {"get": lambda _self: True})()
    instance.app = type("App", (), {"set_status": lambda _self, value, **kwargs: events.append((value, kwargs))})()
    monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert instance._save_recovery_record("six-safe-words") is None
    assert instance._recovery_target == target
    assert events == [("Could not save recovery record: disk full", {"error": True})]


def test_recovery_save_chmod_failure_is_statused_and_can_retry(monkeypatch, tmp_path):
    from backer.agent.gui import wizard
    from backer.core.config import RepositoryConfig

    events = []
    target = tmp_path / "recovery.txt"
    instance = object.__new__(wizard.RepositoryWizard)
    instance._record = lambda: RepositoryConfig(name="Office", type="local", path="E:/Backups")
    instance._recovery_target = target
    instance._recovery_ack = type("Ack", (), {"get": lambda _self: True})()
    instance.app = type("App", (), {"set_status": lambda _self, value, **kwargs: events.append((value, kwargs))})()
    monkeypatch.setattr(wizard.os, "name", "posix")
    monkeypatch.setattr(Path, "chmod", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("not permitted")))

    assert instance._save_recovery_record("six-safe-words") is None
    assert instance._recovery_target == target
    assert events == [("Could not save recovery record: not permitted", {"error": True})]


def test_user_supplied_passphrase_route_is_masked_and_enters_the_same_confirmation_flow(monkeypatch):
    from backer.agent.gui import wizard

    buttons, entry_options, values = {}, [], iter(("user supplied phrase", "user supplied phrase"))

    class Variable:
        def __init__(self):
            self.value = next(values)

        def get(self):
            return self.value

    class Widget:
        def __init__(self, *_args, **kwargs):
            entry_options.append(kwargs)

        def pack(self, **_kwargs):
            pass

    class Button(Widget):
        def __init__(self, _parent, *, text, command, **kwargs):
            super().__init__(_parent, **kwargs)
            buttons[text] = command

    instance = object.__new__(wizard.RepositoryWizard)
    instance.body = object()
    instance.step = 4
    instance.values = {}
    instance._clear = lambda: None
    instance._render = lambda: entry_options.append({"rendered": True})
    instance.app = type("App", (), {"set_status": lambda *_args, **_kwargs: None})()
    monkeypatch.setattr(wizard.tk, "StringVar", Variable)
    monkeypatch.setattr(wizard.ttk, "Label", Widget)
    monkeypatch.setattr(wizard.ttk, "Entry", Widget)
    monkeypatch.setattr(wizard.ttk, "Frame", Widget)
    monkeypatch.setattr(wizard.ttk, "Button", Button)

    instance._supplied_passphrase()
    buttons["Continue"]()

    assert instance.values["passphrase"] == "user supplied phrase"
    assert sum(options.get("show") == "*" for options in entry_options) == 2
    assert {"rendered": True} in entry_options


def test_1219_actions_keep_selected_target_and_only_disconnect_the_named_conflict(monkeypatch):
    from backer.agent.gui import wizard

    buttons = {}
    calls = []

    class Frame:
        def __init__(self, *_args, **_kwargs):
            pass

        def pack(self, **_kwargs):
            pass

    class Button(Frame):
        def __init__(self, _parent, *, text, command, **_kwargs):
            buttons[text] = command

    class Manager:
        def _find_existing_connection(self, _server):
            return (r"\\nas\media", "other-user")

        def connect_existing_serverless(self, *args):
            calls.append(("reuse", args))
            return False

        def disconnect_existing_connection(self, connection):
            calls.append(("disconnect", connection))
            return True

    class Thread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class Root:
        def after(self, _delay, callback):
            callback()

    class Tree:
        def selection(self):
            return ("selected",)

        def set(self, _item, _column):
            return "Backer"

    class Share:
        def get(self):
            return "backups"

    class Listing:
        def set(self, value):
            calls.append(("listing", value))

    instance = object.__new__(wizard.RepositoryWizard)
    instance.body = object()
    instance.share = Share()
    instance.tree = Tree()
    instance.listing = Listing()
    instance.values = {"path": "Backer"}
    instance._generation = 1
    instance.cancel = type("Cancel", (), {"is_set": lambda _self: False})()
    instance.app = type(
        "App",
        (),
            {
                "root": Root(),
                "_generations": {"repository": 1},
                "marshal": lambda _self, _token, callback, _current: callback(),
                "confirm_remove_repository": lambda _self, _connection: True,
            "set_status": lambda *_args, **_kwargs: None,
        },
    )()
    instance._probe_selected_location = lambda: calls.append(("probe", None))
    instance._go = lambda step: calls.append(("go", step))

    monkeypatch.setattr(wizard.ttk, "Frame", Frame)
    monkeypatch.setattr(wizard.ttk, "Button", Button)
    monkeypatch.setattr("backer.core.mounts.SMBConnectionManager", Manager)
    monkeypatch.setattr(wizard.threading, "Thread", Thread)

    instance._show_1219("nas", "error 1219")
    buttons["Use existing connection"]()
    buttons["Cancel"]()
    buttons[r"Disconnect \\nas\media"]()

    assert ("reuse", ("nas", "backups", "Backer")) in calls
    assert ("disconnect", r"\\nas\media") in calls
    assert ("go", 2) in calls
    assert instance.values == {"path": "Backer"}


def test_no_percentage_without_a_frame():
    source = _source("app.py")
    assert 'self.bar.configure(mode="indeterminate")' in source
    assert "if total:" in source
    assert "bytes_done" in source


def test_restore_progress_uses_the_shared_determinate_frame():
    from backer.agent.gui.app import RestoreView

    changes = []
    instance = object.__new__(RestoreView)
    instance.progress = type(
        "Bar",
        (),
        {"stop": lambda _self: changes.append(("stop",)), "configure": lambda _self, **kw: changes.append(kw)},
    )()
    instance.status = type("Status", (), {"set": lambda _self, value: changes.append(("status", value))})()

    instance._progress_frame(bytes_processed=2048, total_bytes=4096, files_processed=2, total_files=10)

    assert {"mode": "determinate", "maximum": 4096, "value": 2048} in changes
    assert ("status", "Restoring · 2 of 10 files") in changes


def test_worker_marshal_drops_callbacks_after_shutdown():
    from backer.agent.gui.app import BackerAgentApp

    calls = []

    class Root:
        def after(self, _delay, callback):
            callback()

    app = object.__new__(BackerAgentApp)
    app.root, app.alive, app._generations = Root(), False, {"restore": 1}

    app.marshal(("restore", 1), lambda: calls.append("paint"))

    assert calls == []


def test_cancel_running_never_waits_for_kopia_reap():
    import threading
    import time

    from backer.agent.gui.app import BackerAgentApp

    finished = threading.Event()
    app = object.__new__(BackerAgentApp)
    app.run_cancel = threading.Event()
    app.process_owner = type("Owner", (), {"cancel": lambda _self: (time.sleep(0.1), finished.set())})()

    started = time.monotonic()
    app.cancel_running()

    assert time.monotonic() - started < 0.05
    assert app.run_cancel.is_set() and finished.wait(1)


def test_restore_stop_becomes_enabled_when_operation_starts(monkeypatch):
    import threading

    from backer.agent.gui import app as gui_app
    from backer.agent.gui.app import RestoreView

    changes = []

    class Thread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    class Root:
        def after(self, *_args):
            pass

    instance = object.__new__(RestoreView)
    instance.app = type("App", (), {"running": False, "run_cancel": threading.Event(), "root": Root()})()
    instance.status = type("Status", (), {"set": lambda _self, value: changes.append(("status", value))})()
    instance.primary = type("Button", (), {"configure": lambda _self, **kwargs: changes.append(kwargs)})()
    instance.restore_frame, instance.restore_at = None, 0
    monkeypatch.setattr(gui_app.threading, "Thread", Thread)

    instance._prepared(("restore", 1), "target", "NEW", "", "snapshot", "repo", "source", None)

    assert instance.app.running
    assert any(
        isinstance(change, dict) and change.get("text") == "Stop" and change.get("state") == "normal"
        for change in changes
    )


def test_1219_panel_names_the_conflicting_connection(monkeypatch):
    from backer.agent.gui.wizard import connection_conflict_message

    monkeypatch.setattr(
        "backer.core.mounts.SMBConnectionManager._find_existing_connection",
        lambda _self, _server: ("\\\\nas\\backups", "backup-user"),
    )
    rendered = connection_conflict_message("nas")
    assert "\\\\nas\\backups" in rendered and "backup-user" in rendered


def test_messagebox_sites_are_the_five_irreversible_actions():
    source = _source("app.py")
    assert source.count("messagebox.") == 5
    for name in ("restore_overwrite", "remove_job", "remove_repository", "quit_during_run", "reveal_passphrase"):
        assert f"confirm_{name}" in source


def test_no_engine_control_exists():
    source = "\n".join(path.read_text().lower() for path in ROOT.glob("*.py"))
    assert 'text="kopia"' not in source and "text='kopia'" not in source


def test_rollback_repository_removes_config_and_both_secret_scopes(monkeypatch, tmp_path):
    from backer.agent.gui import wizard
    from backer.agent.gui.wizard import rollback_repository
    from backer.core.config import BackerConfig, RepositoryConfig

    config = BackerConfig(
        repositories={
            "repo": RepositoryConfig(
                name="Repo", type="local", path="x", passphrase_ref="pass", storage_password_ref="store"
            )
        }
    )
    removed = []
    monkeypatch.setattr(wizard.keystore, "delete", lambda key, *, machine_scope: removed.append((key, machine_scope)))
    rollback_repository(config, tmp_path / "config.yaml", "repo")
    assert config.repositories == {}
    assert set(removed) == {("pass", False), ("pass", True), ("store", False), ("store", True)}


def test_rollback_reports_each_failed_secret_or_save_boundary(monkeypatch, tmp_path):
    from backer.agent.gui import wizard
    from backer.agent.gui.wizard import rollback_repository
    from backer.core.config import BackerConfig, RepositoryConfig

    config = BackerConfig(
        repositories={"repo": RepositoryConfig(name="Repo", type="local", path="x", passphrase_ref="pass")}
    )
    monkeypatch.setattr(wizard.keystore, "delete", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(BackerConfig, "save", lambda _self, _path: (_ for _ in ()).throw(OSError("disk full")))
    errors = rollback_repository(config, tmp_path / "config.yaml", "repo")
    assert config.repositories == {}
    assert any("locked" in error for error in errors)
    assert any("disk full" in error for error in errors)


def test_show_is_a_real_retained_single_view(monkeypatch):
    from backer.agent.gui import app as gui_app
    from backer.core.config import BackerConfig

    monkeypatch.setattr(gui_app, "load_config", lambda: BackerConfig())
    monkeypatch.setattr(gui_app, "TRAY_AVAILABLE", False)
    app = gui_app.BackerAgentApp()
    try:
        app._show("home")
        app.root.update()
        packed = [child for child in app.container.winfo_children() if child.winfo_manager() == "pack"]
        assert len(packed) == 1
        app._show("repository")
        app.root.focus_force()
        app.root.event_generate("<Escape>")
        app.root.update()
        assert app.visible == "home"
    finally:
        app.root.destroy()


def test_repository_save_failure_removes_only_new_refs(monkeypatch, tmp_path):
    from backer.core.config import BackerConfig, RepositoryConfig
    from backer.serverless import repositories

    config = BackerConfig(repositories={"unrelated": RepositoryConfig(name="Other", type="local", path="y")})
    monkeypatch.setattr(repositories, "file_fallback_required", lambda: False)
    monkeypatch.setattr(repositories, "probe", lambda *_args: ("present", "existing", ""))
    puts, deleted = [], []
    monkeypatch.setattr(
        repositories.keystore, "put", lambda reference, *_args, **_kwargs: puts.append(reference) or "test"
    )
    monkeypatch.setattr(
        repositories.keystore, "delete", lambda reference, *, machine_scope: deleted.append((reference, machine_scope))
    )
    calls = []
    def save(_self, _path):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("disk full")
    monkeypatch.setattr(BackerConfig, "save", save)
    record = RepositoryConfig(name="New", type="local", path="x")
    try:
        repositories.add_repository(config, tmp_path / "config.yaml", "New", record, "secret", attach=True, init=False)
    except OSError:
        pass
    else:
        raise AssertionError("save failure must reach the caller")
    assert set(config.repositories) == {"unrelated"}
    assert puts and all(reference.startswith("backer/repo/") for reference in puts)
    assert {reference for reference, _scope in deleted} == set(puts)


def test_repository_second_secret_write_is_compensated(monkeypatch, tmp_path):
    from backer.core.config import BackerConfig, RepositoryConfig
    from backer.serverless import repositories

    config = BackerConfig()
    monkeypatch.setattr(repositories, "file_fallback_required", lambda: False)
    monkeypatch.setattr(repositories, "parse_s3_config", lambda values: type("Parsed", (), {"public_config": values})())
    monkeypatch.setattr(repositories, "probe", lambda *_args: ("present", "existing", ""))
    removed, calls = [], []
    def put(reference, *_args, **_kwargs):
        calls.append(reference)
        if len(calls) == 2:
            raise OSError("storage write failed")
        return "test"
    monkeypatch.setattr(repositories.keystore, "put", put)
    monkeypatch.setattr(
        repositories.keystore, "delete", lambda reference, *, machine_scope: removed.append((reference, machine_scope))
    )
    record = RepositoryConfig(name="S3", type="s3", bucket="bucket", endpoint="https://s3.invalid", region="x")
    try:
        repositories.add_repository(
            config, tmp_path / "config.yaml", "S3", record, "secret", attach=True, init=False,
            storage={"access_key_id": "id", "secret_access_key": "key"},
        )
    except OSError:
        pass
    else:
        raise AssertionError("second secret write must fail setup")
    assert config.repositories == {}
    assert {reference for reference, _scope in removed} == set(calls)
