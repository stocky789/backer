from click.testing import CliRunner

from backer.cli import main


def test_setup_installs_kopia_and_fails_nonzero(monkeypatch) -> None:
    class Manager:
        tools_dir = "/tools"

        def get_tool_path(self, tool):
            return None

        def download(self, tool, progress_callback=None):
            if tool == "kopia":
                raise RuntimeError("bad checksum")
            return f"/tools/{tool}"

    monkeypatch.setattr("backer.tools.manager.get_tool_manager", lambda: Manager())
    result = CliRunner().invoke(main, ["setup", "--quiet"])
    assert result.exit_code != 0
    assert "kopia" in result.output
