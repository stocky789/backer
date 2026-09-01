from backer.core.messages import explain_failure


def test_known_transport_failure_has_remedy_without_engine_name():
    """Removing the transport mapping would leave a user with opaque output."""
    message = explain_failure("System error 53 from net use")
    assert "file server" in message.lower()
    assert "kopia" not in message.lower()


def test_unknown_failure_keeps_the_raw_output_qualifier():
    """Removing the fallback would hide diagnostic output from an unknown failure."""
    assert "full output" in explain_failure("unrecognised failure").lower()


def test_catalogue_never_uses_an_exclamation_mark():
    from backer.core.messages import FAILURE_MESSAGES

    assert all("!" not in message for message in FAILURE_MESSAGES.values())
