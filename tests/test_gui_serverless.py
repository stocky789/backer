"""Desktop acceptance guardrails; Tk is deliberately required, never skipped."""

from pathlib import Path

ROOT = Path(__file__).parents[1] / "src" / "backer" / "agent" / "gui"


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_gui_tests_actually_ran():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.update()
    root.destroy()


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
    assert "self.app.root.after(0, done)" in source
    assert "generation != self._generation" in source


def test_passphrase_step_requires_confirmation():
    source = _source("wizard.py")
    assert "_generated_passphrase" in source
    assert "I have saved this somewhere other than this computer" in source
    assert "self.primary.configure(state=tk.DISABLED)" in source


def test_no_percentage_without_a_frame():
    source = _source("app.py")
    assert 'self.bar.configure(mode="indeterminate")' in source
    assert "if total:" in source
    assert "bytes_done" in source


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
