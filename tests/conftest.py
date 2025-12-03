"""Pytest configuration and fixtures."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def source_dir(temp_dir: Path):
    """Create a source directory with test files."""
    source = temp_dir / "source"
    source.mkdir()

    # Create some test files
    (source / "file1.txt").write_text("File 1 content")
    (source / "file2.txt").write_text("File 2 content")

    # Create a subdirectory
    subdir = source / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("Nested content")

    # Create a file that should be excluded
    (source / "temp.tmp").write_text("Temp file")

    return source


@pytest.fixture
def dest_dir(temp_dir: Path):
    """Create an empty destination directory."""
    dest = temp_dir / "dest"
    dest.mkdir()
    return dest


@pytest.fixture
def mock_rclone_available(mocker):
    """Mock rclone as available."""
    mock_result = mocker.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "rclone v1.68.0\n"
    mock_result.stderr = ""

    mocker.patch("subprocess.run", return_value=mock_result)
    mocker.patch("shutil.which", return_value="/usr/bin/rclone")

    return mock_result


@pytest.fixture
def mock_restic_available(mocker):
    """Mock restic as available."""
    mock_result = mocker.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "restic 0.17.0\n"
    mock_result.stderr = ""

    mocker.patch("subprocess.run", return_value=mock_result)
    mocker.patch("shutil.which", return_value="/usr/bin/restic")

    return mock_result
