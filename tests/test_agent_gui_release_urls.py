from pathlib import Path


def test_agent_gui_uses_gitea_release_main_installer_url() -> None:
    source = (Path(__file__).parents[1] / "src/backer/agent/gui/app.py").read_text()

    assert "https://github.com/stocky789/backer" not in source
    assert "/releases/latest/download/backer-agent-setup.exe" not in source
    assert 'REPOSITORY_URL = "https://git.stockhome.com.au/stocky789/backer"' in source
    assert 'f"{REPOSITORY_URL}/releases/download/release-main/backer-agent-setup.exe"' in source


def test_windows_updater_uses_inno_unattended_flags() -> None:
    from backer.agent.gui.views import installer_update_command

    assert installer_update_command("C:/temp/backer-agent-setup.exe") == [
        "C:/temp/backer-agent-setup.exe",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
    ]
