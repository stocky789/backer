"""Small interactive front door for a local Backer installation."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    modified: str = ""


class WizardAbortError(Exception):
    """The user deliberately left setup before it wrote configuration."""


def wizard_steps(group: int):
    """Return the one init-table slice rendered by a compact wizard screen."""
    from backer.cli import INIT_STEPS

    return tuple(step for step in INIT_STEPS if step.wizard_group == group)


def _step(group: int, key: str):
    return next(step for step in wizard_steps(group) if step.key == key)


def _ask_step(step, **kwargs: Any) -> Any:
    """Prompt until the InitStep that owns this field accepts the answer."""
    while True:
        value = Prompt.ask(step.prompt, **kwargs)
        if step.validator(value):
            return value
        console.print(f"[red]{step.prompt} is not valid.[/red]")


def _accepted(step, value: Any) -> Any:
    if not step.validator(value):
        raise ValueError(f"{step.prompt} is not valid")
    return value


def valid_folder_name(name: str) -> bool:
    return bool(name) and len(name) <= 255 and name[0] != "." and name not in {".", ".."} and not any(
        separator in name for separator in ("/", "\\")
    )


def local_entries(path: Path) -> list[Entry]:
    with os.scandir(path) as items:
        entries = [Entry(item.name, item.is_dir(), item.stat().st_size) for item in items]
    return sorted(entries, key=lambda item: (not item.is_dir, item.name.casefold()))


def smb_entries(server: str, share: str, path: str, username: str, password: str) -> list[Entry]:
    """Read one SMB directory through the existing credential-safe browser."""
    from backer.core.smb_browse import SMBBrowser

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console, transient=True
    ) as progress:
        progress.add_task(f"Listing shares on {server}", total=None)
        success, result = SMBBrowser.list_directory(server, share, path, username, password)
    if not success:
        raise ValueError(str(result))
    return [Entry(item.name, item.is_dir, item.size, item.modified) for item in result]


def choose_smb_share(server: str, username: str, password: str) -> str:
    from backer.core.smb_browse import SMBBrowser

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console, transient=True
    ) as progress:
        progress.add_task(f"Listing shares on {server}", total=None)
        success, result = SMBBrowser.list_shares(server, username, password)
    if not success:
        raise ValueError(str(result))
    shares = list(result)
    table = Table(title=f"Shares on {server}")
    table.add_column("#", justify="right")
    table.add_column("Share")
    for number, item in enumerate(shares, start=1):
        table.add_row(str(number), item.name + "/")
    console.print(table)
    selected = Prompt.ask("Share number", default="1")
    if selected == "q":
        raise WizardAbortError()
    if not selected.isdigit() or not 1 <= int(selected) <= len(shares):
        raise ValueError("Choose a share number")
    return shares[int(selected) - 1].name


def _render(path: Path | str, entries: list[Entry], page: int, filter_text: str) -> list[Entry]:
    visible = [entry for entry in entries if filter_text.casefold() in entry.name.casefold()]
    start = page * 20
    table = Table(title=str(path))
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Size", justify="right")
    for number, entry in enumerate(visible[start : start + 20], start=1):
        table.add_row(
            str(number),
            entry.name + ("/" if entry.is_dir else ""),
            "Directory" if entry.is_dir else "File",
            str(entry.size),
        )
    console.print(table)
    console.print("[number] open  [u] up  [Enter] use this folder  [n] new folder  [/] filter  [m] more  [q] quit")
    return visible


def browse_directory(
    start: Path | str,
    list_dir: Callable[[Path | str], list[Entry]],
    read: Callable[[str], str],
    *,
    parent: Callable[[Path | str], Path | str] | None = None,
    child: Callable[[Path | str, str], Path | str] | None = None,
    create: Callable[[Path | str, str], None] | None = None,
) -> Path | str:
    """Browse a local directory without taking over the terminal."""
    current = start
    parent = parent or (lambda value: value.parent)  # type: ignore[union-attr]
    child = child or (lambda value, name: value / name)  # type: ignore[operator]
    create = create or (lambda value, name: (value / name).mkdir())  # type: ignore[operator]
    page = 0
    filter_text = ""
    while True:
        visible = _render(current, list_dir(current), page, filter_text)
        answer = read("> ").strip()
        if answer == "q":
            raise WizardAbortError()
        if not answer:
            return current
        if answer == "u":
            current = parent(current)
            page = 0
            continue
        if answer == "m":
            page = (page + 1) % max(1, (len(visible) + 19) // 20)
            continue
        if answer.startswith("/"):
            filter_text = answer[1:]
            page = 0
            continue
        if answer == "n":
            name = read("Folder name: ").strip()
            if not valid_folder_name(name):
                console.print("[red]Folder name is not valid.[/red]")
                continue
            create(current, name)
            continue
        if answer.isdigit():
            index = page * 20 + int(answer) - 1
            if 0 <= index < len(visible) and visible[index].is_dir:
                current = child(current, visible[index].name)
                page = 0
            else:
                console.print("[red]Choose a directory number.[/red]")
        else:
            console.print("[red]Unknown choice.[/red]")


def browse_smb_directory(
    server: str, share: str, username: str, password: str, read: Callable[[str], str] = Prompt.ask
) -> str:
    """Use the same numbered browser for a remote SMB folder."""
    return str(
        browse_directory(
            "",
            lambda path: smb_entries(server, share, str(path), username, password),
            read,
            parent=lambda path: str(path).rsplit("/", 1)[0],
            child=lambda path, name: "/".join(part for part in (str(path), name) if part),
            create=lambda path, name: smb_create_directory(server, share, str(path), name, username, password),
        )
    )


def smb_create_directory(server: str, share: str, path: str, name: str, username: str, password: str) -> None:
    from backer.core.smb_browse import SMBBrowser

    target = "/".join(part for part in (path, name) if part)
    if not SMBBrowser.make_directory(server, share, target, username, password):
        raise ValueError("Could not create folder")


def _ask_path(step) -> str:
    while True:
        value = Prompt.ask(step.prompt, default="").strip()
        if not value:
            value = str(browse_directory(Path.cwd(), local_entries, Prompt.ask))
        if step.validator(value):
            return value
        console.print(f"[red]{step.prompt} is not valid.[/red]")


def _passphrase(values: dict[str, Any]) -> None:
    from backer.cli import _generated_passphrase

    phrase = _generated_passphrase()
    words = phrase.split("-")
    export = Prompt.ask(_step(3, "passphrase_out").prompt).strip()
    if not export:
        raise WizardAbortError()
    _accepted(_step(3, "passphrase_out"), Path(export))
    console.print(f"[bold]Repository passphrase[/bold]\n{phrase}")
    console.print(f"This will write your passphrase to {export} in plain text after Create.")
    Prompt.ask("Press Enter after saving it", default="")
    console.clear()
    while Prompt.ask("Type words 3 and 6, separated by a space") != f"{words[2]} {words[5]}":
        console.print("[red]That does not match. Words are numbered left to right starting at 1.[/red]")
        if not Confirm.ask("Show the passphrase again?", default=True):
            raise WizardAbortError()
        console.print(phrase)
        Prompt.ask("Press Enter after saving it", default="")
        console.clear()
    values["generate_passphrase"] = True
    values["_generated_passphrase"] = phrase
    values["passphrase_out"] = Path(export)
    values["print_passphrase"] = False


def run_wizard(values: dict[str, Any]) -> dict[str, Any]:
    """Fill the init flag dictionary. Configuration is written only by cli.init."""
    from backer.cli import _interactive

    if not _interactive():
        raise RuntimeError("Interactive setup requires a terminal")
    result = dict(values)
    first = wizard_steps(1)[0]
    console.print(f"[bold]Step 1 of 5[/bold]  {first.prompt}")
    selected = _ask_step(first, choices=("local", "smb", "s3"), default="local")
    result["repository_type"] = selected
    console.print("[bold]Step 2 of 5[/bold]  Repository")
    if result["repository_type"] == "local":
        result["path"] = _ask_path(_step(2, "path"))
    elif result["repository_type"] == "smb":
        result["host"] = _ask_step(_step(2, "host"))
        result["username"] = _ask_step(_step(2, "username"))
        password = Prompt.ask("Password", password=True)
        result["share"] = _accepted(
            _step(2, "share"), choose_smb_share(result["host"], result["username"], password)
        )
        result["path"] = browse_smb_directory(result["host"], result["share"], result["username"], password)
        result["password_stdin"] = _accepted(_step(2, "password_stdin"), True)
        result["_storage_password"] = password
    else:
        keys = {"bucket", "prefix", "endpoint", "region", "access_key_id"}
        fields = [step for step in wizard_steps(2) if step.key in keys]
        for step in fields:
            result[step.key] = _ask_step(step, default="" if step.key == "prefix" else None)
        secret = Prompt.ask(_step(2, "secret_key_stdin").prompt, password=True)
        result["secret_key_stdin"] = _accepted(_step(2, "secret_key_stdin"), True)
        result["_storage_password"] = json.dumps(
            {"access_key_id": result["access_key_id"], "secret_access_key": secret}
        )
    console.print("[bold]Step 3 of 5[/bold]  Encryption")
    _passphrase(result)
    console.print("[bold]Step 4 of 5[/bold]  What to back up")
    result["source"] = (_ask_path(_step(4, "source")),)
    excludes = _ask_step(_step(4, "exclude"), default="")
    result["exclude"] = tuple(item.strip() for item in excludes.split(",") if item.strip())
    console.print("[bold]Step 5 of 5[/bold]  Schedule")
    schedule = Prompt.ask(_step(5, "schedule").prompt, choices=("1", "2", "3", "4"), default="1")
    schedules = {"1": "0 2 * * *", "2": "0 * * * *", "3": None, "4": Prompt.ask("Cron")}
    result["schedule"], result["no_schedule"] = schedules[schedule], schedule == "3"
    _accepted(_step(5, "schedule") if result["schedule"] else _step(5, "no_schedule"), result["schedule"] or True)
    result["repo_name"] = Prompt.ask("Repository name", default=result["repo_name"])
    result["job_name"] = Prompt.ask("Job name", default=result["job_name"])
    if not Confirm.ask("Create this?", default=True):
        raise WizardAbortError()
    return result
