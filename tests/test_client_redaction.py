from backer.client import agent as client_agent


def test_repository_option_logging_redacts_nested_secrets(capsys) -> None:
    secrets = (
        "repo-password", "proxy-token", "s3-key", "s3-secret", "api-key", "private-key",
        "authorization-value", "credential-value",
    )
    options = {
        "repository_password": secrets[0],
        "proxy_capability": secrets[1],
        "s3": {"access_key_id": secrets[2], "secret_access_key": secrets[3], "bucket": "backups"},
        "api_key": secrets[4],
        "private_key": secrets[5],
        "authorization": secrets[6],
        "credential": secrets[7],
        "retries": 3,
        "targets": [{"name": "primary", "token": "nested-token"}],
    }

    client_agent._log_repository_options("BACKUP", options)

    output = capsys.readouterr().out
    assert all(secret not in output for secret in (*secrets, "nested-token"))
    assert "'bucket': 'backups'" in output
    assert "'retries': 3" in output
    assert options["s3"]["secret_access_key"] == secrets[3]
