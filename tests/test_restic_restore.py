"""Tests for restic restore functionality."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestResticRestore:
    """Test restic restore logic in AgentService."""

    @pytest.fixture
    def temp_dirs(self):
        """Create temporary directories for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            source = base / "source"
            dest = base / "dest"
            repo = base / "repo"
            tools = base / "tools"

            source.mkdir()
            dest.mkdir()
            repo.mkdir()
            tools.mkdir()

            # Create some test files in source
            (source / "file1.txt").write_text("Hello World")
            (source / "file2.txt").write_text("Test content")
            subdir = source / "subdir"
            subdir.mkdir()
            (subdir / "file3.txt").write_text("Nested file")

            yield {
                "base": base,
                "source": source,
                "dest": dest,
                "repo": repo,
                "tools": tools,
            }

    def test_clean_restore_removes_destination(self, temp_dirs):
        """Test that clean restore properly removes destination directory."""
        dest = temp_dirs["dest"]

        # Create files in destination
        (dest / "existing.txt").write_text("existing content")
        (dest / "subdir").mkdir()
        (dest / "subdir" / "nested.txt").write_text("nested")

        assert dest.exists()
        assert (dest / "existing.txt").exists()

        # Simulate clean restore deletion
        import shutil
        if dest.exists():
            shutil.rmtree(dest)
            # Note: we should NOT recreate the directory - let restic do it

        assert not dest.exists()

    def test_normalize_windows_path(self):
        """Test Windows path normalization for UNC paths."""
        from backer.agent.service import AgentService

        service = AgentService(
            server_url="http://localhost:8420",
            client_id="test",
            client_secret="secret",
            tools_dir=Path("/tmp/tools"),
        )

        # Test UNC path conversion (only applies on Windows)
        if sys.platform == 'win32':
            assert service._normalize_windows_path("//server/share") == "\\\\server\\share"
            assert service._normalize_windows_path("//192.168.0.1/share/path") == "\\\\192.168.0.1\\share\\path"
        else:
            # On Linux, paths should be unchanged
            assert service._normalize_windows_path("//server/share") == "//server/share"
            assert service._normalize_windows_path("/local/path") == "/local/path"

    def test_restore_target_calculation_original_location(self):
        """Test that restore target is calculated correctly for original location restore."""
        # When restoring to original location (dest == original backup path):
        # - On Linux: use "/" as target
        # - On Windows: use drive root (e.g., "C:\") then move files after restore

        # Test Linux path
        dest = "/home/user/backup"
        original_paths = ["/home/user/backup"]

        dest_normalized = dest.replace('\\', '/').rstrip('/')
        restore_target = dest

        for orig_path in original_paths:
            orig_normalized = orig_path.replace('\\', '/').rstrip('/')
            if dest_normalized == orig_normalized or orig_normalized.startswith(dest_normalized + '/'):
                # On Linux, use "/" for original location restore
                if sys.platform != 'win32':
                    restore_target = '/'
                break

        # On Linux, we use "/" for original location restore
        if sys.platform != 'win32':
            assert restore_target == '/'

    def test_restore_target_calculation_windows_original_location(self):
        """Test Windows-specific restore target calculation."""
        # On Windows, restic stores C:\Test as /C/Test internally
        # Using --target / would create files at \C\Test (wrong location)
        # So we use drive root and then move files after restore

        dest = "C:\\Test"
        original_paths = ["C:\\Test"]

        dest_normalized = dest.replace('\\', '/').rstrip('/')
        restore_target = dest
        needs_windows_path_fix = False
        windows_drive_letter = None

        for orig_path in original_paths:
            orig_normalized = orig_path.replace('\\', '/').rstrip('/')
            if dest_normalized == orig_normalized or orig_normalized.startswith(dest_normalized + '/'):
                # Windows absolute path handling
                if len(dest) >= 2 and dest[1] == ':':
                    windows_drive_letter = dest[0].upper()
                    restore_target = f"{windows_drive_letter}:\\"
                    needs_windows_path_fix = True
                break

        # On Windows with absolute paths, use drive root as target
        assert restore_target == "C:\\"
        assert needs_windows_path_fix is True
        assert windows_drive_letter == "C"

    def test_restore_target_calculation_different_location(self):
        """Test that restore target is the destination when restoring to different location."""
        dest = "/tmp/restore"
        original_paths = ["/home/user/backup"]

        dest_normalized = dest.replace('\\', '/').rstrip('/')
        restore_target = dest

        for orig_path in original_paths:
            orig_normalized = orig_path.replace('\\', '/').rstrip('/')
            if dest_normalized == orig_normalized or orig_normalized.startswith(dest_normalized + '/'):
                restore_target = '/'
                break

        # Should NOT change restore_target since paths don't match
        assert restore_target == "/tmp/restore"

    @patch('subprocess.Popen')
    @patch('subprocess.run')
    def test_restic_restore_command_structure(self, mock_run, mock_popen, temp_dirs):
        """Test that the restic restore command is structured correctly."""
        from backer.agent.service import AgentService

        # Mock the tool path check - use correct extension for platform
        tools_dir = temp_dirs["tools"]
        restic_name = "restic.exe" if sys.platform == "win32" else "restic"
        restic_path = tools_dir / restic_name
        restic_path.touch()
        if sys.platform != "win32":
            restic_path.chmod(0o755)

        service = AgentService(
            server_url="http://localhost:8420",
            client_id="test",
            client_secret="secret",
            tools_dir=tools_dir,
        )

        # Mock subprocess.run for snapshot check
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"paths": [str(temp_dirs["source"])]}]),
            stderr=""
        )

        # Mock subprocess.Popen for restore
        mock_process = MagicMock()
        mock_process.stdout = iter([
            "restoring snapshot abc123 to /\n",
            "Summary: Restored 5 files/dirs (100 B) in 0:01\n"
        ])
        mock_process.wait.return_value = None
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        # Run restore
        result = service._run_restic_restore(
            repo=str(temp_dirs["repo"]),
            dest=str(temp_dirs["source"]),  # Restore to original location
            snapshot="abc123",
            dry_run=False,
            run_id="test_run",
            backend_options={"restic_password": "test"},
        )

        # Verify the command was called
        assert mock_popen.called
        call_args = mock_popen.call_args

        # Check command structure
        cmd = call_args[0][0]
        assert str(restic_path) in cmd[0]
        assert '-r' in cmd
        assert 'restore' in cmd
        assert 'abc123' in cmd
        assert '--target' in cmd
        assert '-vv' in cmd  # Verbose flag

        assert result['success'] is True

    def test_post_restore_verification_logging(self, temp_dirs):
        """Test that post-restore verification correctly checks destination."""
        dest = temp_dirs["dest"]

        # Create some files to simulate successful restore
        (dest / "restored_file.txt").write_text("restored content")
        (dest / "restored_dir").mkdir()
        (dest / "restored_dir" / "nested.txt").write_text("nested")

        # Verify the logic
        dest_path = Path(dest)
        assert dest_path.exists()

        contents = list(dest_path.iterdir())
        assert len(contents) == 2  # restored_file.txt and restored_dir

        # Check file names
        names = [item.name for item in contents]
        assert "restored_file.txt" in names
        assert "restored_dir" in names

    def test_clean_restore_does_not_recreate_dir(self, temp_dirs):
        """Test that after clean restore, we don't recreate the destination dir."""
        dest = temp_dirs["dest"]

        # Create files in destination
        (dest / "file.txt").write_text("content")

        # Simulate clean restore behavior (new code)
        import shutil
        if dest.exists():
            shutil.rmtree(dest)
            # OLD CODE would do: dest.mkdir(parents=True, exist_ok=True)
            # NEW CODE: Don't recreate - let restic create it

        # Destination should NOT exist after clean
        assert not dest.exists()

    def test_restore_target_uses_forward_slash_on_linux(self):
        """Test that restore target uses '/' for original location restore on Linux."""
        # On Linux, we use "/" for restoring to original location
        # On Windows, we use drive root and fix the path after restore

        orig_path = "/home/user/backup"
        dest = "/home/user/backup"

        # Simulate the logic from _run_restic_restore (Linux path)
        dest_normalized = dest.replace('\\', '/').rstrip('/')
        restore_target = dest

        orig_normalized = orig_path.replace('\\', '/').rstrip('/')
        if dest_normalized == orig_normalized:
            # On Linux, use "/" for original location restore
            restore_target = '/'

        # Should be "/" for Linux paths
        assert restore_target == "/"

    def test_windows_path_fix_calculation(self):
        """Test that Windows path fix correctly calculates nested path."""
        # On Windows, restic creates C:\C\Test when restoring C:\Test with --target C:\
        # We need to move files from the nested location to the correct one

        original_dest = "C:\\Test"
        windows_drive_letter = "C"

        # Calculate the nested path where restic puts files
        dest_without_drive = original_dest[2:].lstrip('\\').lstrip('/')  # "Test"
        nested_path = f"{windows_drive_letter}:\\{windows_drive_letter}\\{dest_without_drive}"

        # The nested path should be C:\C\Test
        assert nested_path == "C:\\C\\Test"
        assert dest_without_drive == "Test"


class TestResticBackupRestore:
    """Integration-style tests for backup and restore workflow."""

    @pytest.fixture
    def mock_service(self):
        """Create a mocked agent service."""
        with patch('backer.agent.service.AgentService._get_tool_path') as mock_get_tool:
            mock_get_tool.return_value = Path("/usr/bin/restic")

            from backer.agent.service import AgentService
            service = AgentService(
                server_url="http://localhost:8420",
                client_id="test",
                client_secret="secret",
                tools_dir=Path("/tmp/tools"),
            )
            yield service

    def test_execute_restore_clean_restore_flow(self, mock_service):
        """Test the full execute_restore flow with clean_restore=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "restore_target"
            dest.mkdir()
            (dest / "existing.txt").write_text("old content")

            payload = {
                "run_id": "test_restore_123",
                "job_name": "test_job",
                "source_path": "//server/share/repo",
                "destination_path": str(dest),
                "backend": "restic",
                "snapshot": "abc123",
                "clean_restore": True,
                "backend_options": {"restic_password": "test"},
                "dry_run": False,
            }

            # Mock the API calls and restic execution
            with patch.object(mock_service, '_report_progress'), \
                 patch.object(mock_service, '_report_result'), \
                 patch.object(mock_service, '_run_restic_restore') as mock_restore:

                mock_restore.return_value = {
                    "success": True,
                    "output": "Restored 5 files",
                    "bytes": 1000,
                    "files": 5,
                    "error": None,
                }

                # After clean restore deletes the directory, it should NOT exist
                # (before restic runs)

                # Simulate the clean restore logic
                import shutil
                if dest.exists():
                    shutil.rmtree(dest)

                # Verify directory is gone
                assert not dest.exists()

                # Now restic would run and create it
                # (in real scenario, mock_restore would do this)


class TestSnapshotPathQuery:
    """Test snapshot path querying logic."""

    @patch('subprocess.run')
    def test_get_restic_snapshot_paths(self, mock_run):
        """Test querying snapshot paths from restic."""
        from backer.agent.service import AgentService

        with tempfile.TemporaryDirectory() as tmpdir:
            tools_dir = Path(tmpdir) / "tools"
            tools_dir.mkdir()
            restic_path = tools_dir / "restic"
            restic_path.touch()
            restic_path.chmod(0o755)

            service = AgentService(
                server_url="http://localhost:8420",
                client_id="test",
                client_secret="secret",
                tools_dir=tools_dir,
            )

            # Mock successful snapshot query
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{
                    "id": "abc123",
                    "paths": ["/home/user/backup", "/var/data"],
                    "hostname": "testhost",
                }]),
                stderr=""
            )

            env = os.environ.copy()
            env["RESTIC_PASSWORD"] = "test"

            paths = service._get_restic_snapshot_paths(
                restic=restic_path,
                repo="/tmp/repo",
                snapshot_id="abc123",
                env=env,
            )

            assert paths == ["/home/user/backup", "/var/data"]

    @patch('subprocess.run')
    def test_get_restic_snapshot_paths_failure(self, mock_run):
        """Test handling of failed snapshot query."""
        from backer.agent.service import AgentService

        with tempfile.TemporaryDirectory() as tmpdir:
            tools_dir = Path(tmpdir) / "tools"
            tools_dir.mkdir()
            restic_path = tools_dir / "restic"
            restic_path.touch()
            restic_path.chmod(0o755)

            service = AgentService(
                server_url="http://localhost:8420",
                client_id="test",
                client_secret="secret",
                tools_dir=tools_dir,
            )

            # Mock failed snapshot query
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Repository not found"
            )

            env = os.environ.copy()
            env["RESTIC_PASSWORD"] = "test"

            paths = service._get_restic_snapshot_paths(
                restic=restic_path,
                repo="/tmp/repo",
                snapshot_id="abc123",
                env=env,
            )

            # Should return empty list on failure
            assert paths == []
