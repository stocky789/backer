from pathlib import Path

from backer.agent.service import AgentService


def test_service_backup_uses_shared_agent_executor(tmp_path: Path, monkeypatch) -> None:
    service = AgentService("http://example.test", "agent", "secret", tools_dir=tmp_path / "tools")
    calls: list[bool] = []
    monkeypatch.setattr(service, "_execute_with_shared_agent", lambda _payload, restore: calls.append(restore) or True)

    assert service._execute_backup({"dry_run": False}) is True
    assert calls == [False]


def test_service_redacts_nested_capabilities_and_sequence_tokens(tmp_path: Path) -> None:
    payload = {
        "backend_options": {"proxy_capability": "nested-capability"},
        "targets": [{"token": "list-token"}],
    }

    service = AgentService("http://example.test", "agent", "secret", tools_dir=tmp_path / "tools")
    safe = service._redact_sensitive_data(payload)

    assert "nested-capability" not in repr(safe)
    assert "list-token" not in repr(safe)
    assert payload["backend_options"]["proxy_capability"] == "nested-capability"
