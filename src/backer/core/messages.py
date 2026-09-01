"""Short, actionable explanations for errors captured from backup tools."""


FAILURE_MESSAGES = {
    "invalid repository password": (
        "The repository passphrase was rejected. Check it, then run backer repo recover NAME "
        "--passphrase-stdin if this is an older repository."
    ),
    "repository not initialized in the provided storage": (
        "No Backer repository exists at this location. Nothing was created."
    ),
    "cannot access storage path": "Backer cannot access the repository path. Check the server, share and permissions.",
    "error 53": "Backer could not find the file server. Check its name and network connection.",
    "error 67": "Backer could not find the file share. Check the share name.",
    "error 1219": (
        "Windows already has a connection to this server with different credentials. "
        "Close it or use the same account."
    ),
    "error 1326": "The file-server sign-in was rejected. Check the account and password.",
    "mount error(13)": "The file server denied access. Check the account and write permission.",
    "enospc": "The destination has no free space. Free space before trying again.",
}

# These failures cannot complete safely without a person changing credentials,
# choosing a connection, or providing the repository recovery key.
INPUT_NEEDED_FAILURES = frozenset({"invalid repository password", "error 1219", "error 1326", "mount error(13)"})


def failure_needs_input(detail: str) -> bool:
    """Classify only catalogue-backed failures; unknown engine text remains diagnostic."""
    lowered = detail.lower()
    return any(needle in lowered for needle in INPUT_NEEDED_FAILURES)


def explain_failure(detail: str) -> str:
    """Return the known remedy, retaining unknown engine output for diagnosis."""
    lowered = detail.lower()
    for needle, message in FAILURE_MESSAGES.items():
        if needle in lowered:
            return message
    return "Backer could not finish this backup. The details below are the full output from the backup engine."


def failure_details(detail: str, *secrets: str | None) -> str:
    """Pair the actionable explanation with raw output after removing supplied credentials."""
    safe = detail
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "***")
    return f"{explain_failure(safe)}\n{safe}"
