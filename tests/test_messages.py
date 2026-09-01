import pytest

from backer.core.messages import FAILURE_MESSAGES, explain_failure


def test_known_transport_failure_has_remedy_without_engine_name():
    """Removing the transport mapping would leave a user with opaque output."""
    message = explain_failure("System error 53 from net use")
    assert "file server" in message.lower()
    assert "kopia" not in message.lower()


def test_unknown_failure_keeps_the_raw_output_qualifier():
    """Removing the fallback would hide diagnostic output from an unknown failure."""
    assert "full output" in explain_failure("unrecognised failure").lower()


def test_catalogue_never_uses_an_exclamation_mark():
    assert all("!" not in message for message in FAILURE_MESSAGES.values())
    assert all("kopia" not in message.lower() for message in FAILURE_MESSAGES.values())


@pytest.mark.parametrize(
    "detail",
    [
        "net use failed: system error 53",
        "net use failed: system error 67",
        "net use failed: system error 1219",
        "net use failed: system error 1326",
        "cifs mount error(13)",
        "ENOSPC while writing",
        "invalid repository password",
        "repository not initialized in the provided storage",
        "cannot access storage path",
    ],
)
def test_every_documented_failure_substring_has_an_actionable_mapping(detail):
    """Removing any documented substring would make a known outage opaque."""
    assert "details below" not in explain_failure(detail).lower()


def test_unknown_failure_keeps_sanitized_raw_output():
    """Removing sanitisation would disclose a passphrase in the diagnostic block."""
    from backer.core.messages import failure_details

    rendered = failure_details("unexpected failure: secret-value", "secret-value")
    assert "full output" in rendered.lower()
    assert "secret-value" not in rendered
    assert "***" in rendered


def test_secret_copy_is_qualified_and_catalogue_is_not_engine_branded():
    rendered = " ".join(FAILURE_MESSAGES.values()).lower()
    assert "repository passphrase" in rendered
    assert "file-server sign-in" in rendered
    assert "kopia" not in rendered
