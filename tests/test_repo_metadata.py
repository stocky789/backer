"""Tests for repository metadata functionality."""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from backer.core.repo_metadata import (
    BACKER_METADATA_DIR,
    METADATA_VERSION,
    RepositoryMetadata,
    normalize_path_for_platform,
)


class TestNormalizePathForPlatform:
    """Test path normalization for different platforms."""

    def test_smb_path_on_windows(self):
        """Test SMB paths are converted to backslashes on Windows."""
        with patch.object(sys, 'platform', 'win32'):
            result = normalize_path_for_platform("//192.168.0.1/share/path", "smb")
            assert result == "\\\\192.168.0.1\\share\\path"

    def test_smb_path_on_linux(self):
        """Test SMB paths are unchanged on Linux."""
        with patch.object(sys, 'platform', 'linux'):
            result = normalize_path_for_platform("//192.168.0.1/share/path", "smb")
            assert result == "//192.168.0.1/share/path"

    def test_nfs_path_on_windows(self):
        """Test NFS paths are converted to UNC format on Windows."""
        with patch.object(sys, 'platform', 'win32'):
            result = normalize_path_for_platform("192.168.0.1:/volume/backup", "nfs")
            assert result == "\\\\192.168.0.1\\volume\\backup"

    def test_nfs_path_on_linux(self):
        """Test NFS paths are unchanged on Linux."""
        with patch.object(sys, 'platform', 'linux'):
            result = normalize_path_for_platform("192.168.0.1:/volume/backup", "nfs")
            assert result == "192.168.0.1:/volume/backup"

    def test_local_path_unchanged_on_windows(self):
        """Test local Windows paths with drive letters are unchanged."""
        with patch.object(sys, 'platform', 'win32'):
            result = normalize_path_for_platform("C:\\Users\\test\\backup", "local")
            assert result == "C:\\Users\\test\\backup"

    def test_local_path_unchanged_on_linux(self):
        """Test local Linux paths are unchanged."""
        with patch.object(sys, 'platform', 'linux'):
            result = normalize_path_for_platform("/home/user/backup", "local")
            assert result == "/home/user/backup"


class TestRepositoryMetadata:
    """Test RepositoryMetadata class."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary repository directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_is_initialized_empty_repo(self, temp_repo):
        """Test is_initialized returns False for empty repo."""
        repo_meta = RepositoryMetadata(temp_repo)
        assert repo_meta.is_initialized() is False

    def test_initialize_creates_structure(self, temp_repo):
        """Test initialize creates proper directory structure."""
        repo_meta = RepositoryMetadata(temp_repo)
        metadata = repo_meta.initialize(server_id="test-server")

        assert repo_meta.is_initialized() is True
        assert (temp_repo / BACKER_METADATA_DIR).exists()
        assert (temp_repo / BACKER_METADATA_DIR / "metadata.json").exists()
        assert (temp_repo / BACKER_METADATA_DIR / "agents").exists()
        assert (temp_repo / BACKER_METADATA_DIR / "jobs").exists()
        assert (temp_repo / BACKER_METADATA_DIR / "snapshots").exists()

        assert metadata["version"] == METADATA_VERSION
        assert metadata["server_id"] == "test-server"

    def test_save_and_get_agent(self, temp_repo):
        """Test saving and retrieving agent metadata."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        agent_data = {
            "hostname": "test-host",
            "platform": "linux",
            "os_info": "Ubuntu 22.04",
        }
        assert repo_meta.save_agent("agent-123", agent_data) is True

        retrieved = repo_meta.get_agent("agent-123")
        assert retrieved is not None
        assert retrieved["agent_id"] == "agent-123"
        assert retrieved["hostname"] == "test-host"
        assert retrieved["platform"] == "linux"
        assert "first_seen" in retrieved
        assert "updated_at" in retrieved

    def test_list_agents(self, temp_repo):
        """Test listing all agents."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        repo_meta.save_agent("agent-1", {"hostname": "host1"})
        repo_meta.save_agent("agent-2", {"hostname": "host2"})
        repo_meta.save_agent("agent-3", {"hostname": "host3"})

        agents = repo_meta.list_agents()
        assert len(agents) == 3
        hostnames = {a["hostname"] for a in agents}
        assert hostnames == {"host1", "host2", "host3"}

    def test_save_and_get_job(self, temp_repo):
        """Test saving and retrieving job configuration."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        job_config = {
            "source_path": "/home/user/data",
            "backend": "restic",
            "client_id": "agent-1",
        }
        assert repo_meta.save_job("my-backup", job_config) is True

        retrieved = repo_meta.get_job("my-backup")
        assert retrieved is not None
        assert retrieved["job_name"] == "my-backup"
        assert retrieved["config"]["source_path"] == "/home/user/data"
        assert "created_at" in retrieved
        assert "updated_at" in retrieved

    def test_save_job_run(self, temp_repo):
        """Test saving job run records."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        # Save job first
        repo_meta.save_job("test-job", {"source_path": "/data"})

        # Save multiple runs
        for i in range(5):
            run_data = {
                "status": "success",
                "started_at": datetime.now().isoformat(),
                "finished_at": datetime.now().isoformat(),
                "bytes_transferred": i * 1000,
            }
            repo_meta.save_job_run("test-job", f"run_{i}", run_data)

        runs = repo_meta.get_job_runs("test-job")
        assert len(runs) == 5

        latest = repo_meta.get_latest_run("test-job")
        assert latest is not None
        assert latest["status"] == "success"

    def test_save_and_get_snapshot(self, temp_repo):
        """Test saving and retrieving snapshot metadata."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        snapshot_data = {
            "job_name": "test-backup",
            "hostname": "test-host",
            "paths": ["/home/user/data"],
            "time": datetime.now().isoformat(),
        }
        snapshot_id = "abc123def456"
        assert repo_meta.save_snapshot(snapshot_id, snapshot_data) is True

        retrieved = repo_meta.get_snapshot(snapshot_id)
        assert retrieved is not None
        assert retrieved["snapshot_id"] == snapshot_id
        assert retrieved["job_name"] == "test-backup"
        assert retrieved["hostname"] == "test-host"

    def test_list_snapshots(self, temp_repo):
        """Test listing all snapshots."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        for i in range(3):
            repo_meta.save_snapshot(
                f"snapshot_{i}",
                {"time": datetime.now().isoformat(), "hostname": f"host-{i}"}
            )

        snapshots = repo_meta.list_snapshots()
        assert len(snapshots) == 3

    def test_discover_all(self, temp_repo):
        """Test discover_all returns complete summary."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        # Add some data
        repo_meta.save_agent("agent-1", {"hostname": "host1"})
        repo_meta.save_agent("agent-2", {"hostname": "host2"})
        repo_meta.save_job("job-1", {"source_path": "/data1"})
        repo_meta.save_job("job-2", {"source_path": "/data2"})
        repo_meta.save_job_run("job-1", "run-1", {"status": "success"})
        repo_meta.save_job_run("job-1", "run-2", {"status": "success"})
        repo_meta.save_snapshot("snap-1", {"hostname": "host1"})

        discovery = repo_meta.discover_all()

        assert discovery["initialized"] is True
        assert discovery["summary"]["agent_count"] == 2
        assert discovery["summary"]["job_count"] == 2
        assert discovery["summary"]["total_runs"] == 2
        assert discovery["summary"]["snapshot_count"] == 1

    def test_safe_filename(self, temp_repo):
        """Test that problematic characters are sanitized in filenames."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        # Job name with special characters
        job_name = "backup:test/data\\path<>|?*"
        repo_meta.save_job(job_name, {"source_path": "/data"})

        # Should be able to retrieve it
        retrieved = repo_meta.get_job(job_name)
        assert retrieved is not None
        assert retrieved["job_name"] == job_name

    def test_update_agent_merges_data(self, temp_repo):
        """Test that updating agent merges with existing data."""
        repo_meta = RepositoryMetadata(temp_repo)
        repo_meta.initialize()

        # Initial save
        repo_meta.save_agent("agent-1", {"hostname": "host1", "custom_field": "value1"})

        # Update with new data
        repo_meta.save_agent("agent-1", {"platform": "linux"})

        retrieved = repo_meta.get_agent("agent-1")
        assert retrieved["hostname"] == "host1"
        assert retrieved["custom_field"] == "value1"
        assert retrieved["platform"] == "linux"

    def test_empty_repo_discover_all(self, temp_repo):
        """Test discover_all on uninitialized repo."""
        repo_meta = RepositoryMetadata(temp_repo)

        discovery = repo_meta.discover_all()

        assert discovery["initialized"] is False
        assert discovery["agents"] == []
        assert discovery["jobs"] == []
        assert discovery["snapshots"] == []
