from pathlib import Path


def test_gui_serverless_modules_exist_with_a_token_resolver():
    from backer.agent.gui.theme import resolve_tokens

    root = Path(__file__).parents[1] / "src" / "backer" / "agent" / "gui"
    assert (root / "views.py").exists()
    assert (root / "wizard.py").exists()
    assert resolve_tokens("#101214").mode == "dark"
    assert resolve_tokens("#f5f5f5").mode == "light"


def test_gui_uses_one_container_and_only_named_irreversible_confirmations():
    source = (Path(__file__).parents[1] / "src" / "backer" / "agent" / "gui" / "app.py").read_text()
    assert "def _show(" in source
    assert "views.setdefault" not in source
    assert "ttk.Notebook" not in source
    assert "tk.Toplevel" not in source
    assert source.count("messagebox.") <= 5
