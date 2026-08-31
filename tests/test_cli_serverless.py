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


def test_init_no_tty_names_all_missing_flags():
    result = CliRunner().invoke(main, ["init", "--type", "smb", "--host", "nas.local", "--username", "svc"])
    assert result.exit_code == 2
    assert "--share" in result.output
    assert "--path" in result.output
    assert "--password-stdin" in result.output


def test_generated_passphrase_needs_visible_output_off_tty(tmp_path):
    result = CliRunner().invoke(
        main,
        ["repo", "add", "r1", "--init", "--type", "local", "--path", str(tmp_path), "--generate-passphrase", "--yes"],
    )
    assert result.exit_code == 2
    assert "--passphrase-out FILE or --print-passphrase" in result.output


def test_every_init_step_has_a_flag():
    result = CliRunner().invoke(main, ["init", "--help"])
    for _, flag in INIT_STEPS:
        assert flag in result.output
