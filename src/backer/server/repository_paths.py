"""Path helpers for repository-backed job storage."""

import re


def get_job_subfolder(job_name: str) -> str:
    """Return a filesystem-safe subfolder name for a backup job."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", job_name)
