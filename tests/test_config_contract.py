from datetime import UTC, datetime, timedelta

from backer.core.config import BackerConfig
from backer.serverless.schedule import due_jobs, schedule_pause, schedule_pause_state, scheduling_paused


def test_config_persists_only_the_unified_top_level_contract(tmp_path):
    path = tmp_path / "config.yaml"
    config = BackerConfig(agent_id="agent-1")

    config.save(path)

    assert list(config.model_dump()) == ["agent_id", "server", "repositories", "jobs"]
    assert list(__import__("yaml").safe_load(path.read_text())) == ["agent_id", "repositories", "jobs"]


def test_schedule_pause_lives_in_data_dir_without_mutating_config(tmp_path):
    config = BackerConfig(
        jobs={"nightly": {"repository": "repo", "source": {"path": "/source"}, "schedule": {"cron": "* * * * *"}}}
    )
    now = datetime.now(UTC)

    schedule_pause(tmp_path, True, now + timedelta(hours=1))

    assert scheduling_paused(tmp_path, now)
    assert schedule_pause_state(tmp_path) == (True, now + timedelta(hours=1))
    assert due_jobs(config, now, tmp_path) == []
    assert list(config.model_dump()) == ["agent_id", "server", "repositories", "jobs"]


def test_schedule_pause_preserves_fire_times_and_expiry_resumes(tmp_path):
    now = datetime.now(UTC)
    (tmp_path / "schedule.json").write_text('{"fires": {"nightly": "2026-01-01T00:00:00Z"}}')

    schedule_pause(tmp_path, True, now - timedelta(seconds=1))

    assert not scheduling_paused(tmp_path, now)
    assert __import__("json").loads((tmp_path / "schedule.json").read_text())["fires"] == {
        "nightly": "2026-01-01T00:00:00Z"
    }
    assert schedule_pause_state(tmp_path) == (False, None)


def test_pause_rollback_restores_only_pause_state(monkeypatch, tmp_path):
    from backer.agent.gui import views

    monkeypatch.setattr(views, "get_data_dir", lambda: tmp_path)
    (tmp_path / "schedule.json").write_text('{"fires": {"nightly": "2026-01-01T00:00:00Z"}}')
    snapshot = views.schedule_pause_snapshot()
    views.save_schedule_pause(True, None)

    views.restore_schedule_pause(snapshot)

    assert schedule_pause_state(tmp_path) == (False, None)
    assert __import__("json").loads((tmp_path / "schedule.json").read_text())["fires"] == {
        "nightly": "2026-01-01T00:00:00Z"
    }
