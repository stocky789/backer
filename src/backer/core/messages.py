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
    "system error 53": "Backer could not find the file server. Check its name and network connection.",
    "system error 67": "Backer could not find the file share. Check the share name.",
    "system error 1219": (
        "Windows already has a connection to this server with different credentials. "
        "Close it or use the same account."
    ),
    "system error 1326": "The file-server sign-in was rejected. Check the account and password.",
    "mount error(13)": "The file server denied access. Check the account and write permission.",
    "no space left on device": "The destination has no free space. Free space before trying again.",
}


def explain_failure(detail: str) -> str:
    """Return the known remedy, retaining unknown engine output for diagnosis."""
    lowered = detail.lower()
    for needle, message in FAILURE_MESSAGES.items():
        if needle in lowered:
            return message
    return "Backer could not finish this backup. The details below are the full output from the backup engine."
