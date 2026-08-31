from click.testing import CliRunner

from backer.cli import main


def test_setup_installs_kopia_and_fails_nonzero(monkeypatch) -> None:
    class Manager:
        tools_dir = "/tools"
        downloaded = []

        def get_tool_path(self, tool):
            return None

        def download(self, tool, progress_callback=None):
            self.downloaded.append(tool)
            if tool == "kopia":
                raise RuntimeError("bad checksum")
            return f"/tools/{tool}"

    manager = Manager()
    monkeypatch.setattr("backer.tools.manager.get_tool_manager", lambda: manager)
    result = CliRunner().invoke(main, ["setup", "--quiet"])
    assert result.exit_code != 0
    assert "kopia" in result.output
    assert manager.downloaded == ["kopia"]


def test_job_create_has_no_backend_option() -> None:
    result = CliRunner().invoke(main, ["job", "create", "--help"])
    assert result.exit_code == 0
    assert "--backend" not in result.output
    assert "--repository-password" not in result.output


def test_backends_command_is_absent() -> None:
    result = CliRunner().invoke(main, ["backends", "--help"])
    assert result.exit_code != 0


def test_direct_commands_pass_repository_password_to_kopia(tmp_path, monkeypatch) -> None:
    passwords = []

    class KopiaBackend:
        def __init__(self, config):
            passwords.append(config["repository_password"])

        def check_available(self):
            return True, "Kopia"

        def backup(self, **kwargs):
            return type("Result", (), {"success": True, "files_transferred": 0, "bytes_transferred": 0,
                "duration_seconds": 0, "output": ""})()

        def restore(self, **kwargs):
            return type("Result", (), {"success": True, "errors": []})()

    monkeypatch.setattr("backer.backends.kopia.KopiaBackend", KopiaBackend)
    runner = CliRunner()
    backup = runner.invoke(main, ["backup", str(tmp_path), "repo"], env={"BACKER_REPOSITORY_PASSWORD": "from-env"})
    restore = runner.invoke(
        main, ["restore", "repo", str(tmp_path)], env={"BACKER_REPOSITORY_PASSWORD": "from-restore-env"}
    )

    assert backup.exit_code == 0
    assert restore.exit_code == 0
    assert passwords == ["from-env", "from-restore-env"]
