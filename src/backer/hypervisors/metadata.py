"""Hypervisor backup metadata management for discovery and tracking.

This module provides functionality to store and retrieve hypervisor backup
metadata directly in the repository, enabling:
- Discovery of existing VM/container backups when connecting a new server
- Cross-server backup history
- Disaster recovery of backup records

Metadata Structure in Repository:
    .backer/
    ├── metadata.json                    # Repository info
    ├── hypervisors/
    │   └── {hypervisor_id}.json        # Per-hypervisor metadata
    └── hypervisor_backups/
        └── {vmid}/
            ├── guest.json              # VM/container info
            └── runs/
                └── {run_id}.json       # Individual backup run records
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKER_METADATA_DIR = ".backer"


def safe_filename(name: str) -> str:
    """Convert a name to a safe filename."""
    # Replace unsafe characters with underscores
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Collapse multiple underscores
    safe = re.sub(r'_+', '_', safe)
    # Remove leading/trailing underscores and dots
    safe = safe.strip('_.')
    return safe or "unnamed"


class HypervisorMetadata:
    """Manages hypervisor backup metadata stored within a repository.

    This class handles reading and writing metadata files for hypervisor
    backups (Proxmox VMs/containers), enabling backup discovery.
    """

    def __init__(self, repo_path: str | Path):
        """Initialize hypervisor metadata manager.

        Args:
            repo_path: Path to the repository root (where dump/ folder exists)
        """
        self.repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path
        self.metadata_dir = self.repo_path / BACKER_METADATA_DIR
        logger.debug(f"HypervisorMetadata: path={self.repo_path}")

    def _ensure_dirs(self) -> None:
        """Ensure metadata directory structure exists."""
        try:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            (self.metadata_dir / "hypervisors").mkdir(exist_ok=True)
            (self.metadata_dir / "hypervisor_backups").mkdir(exist_ok=True)
        except OSError as e:
            logger.warning(f"Failed to create metadata directories: {e}")

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        """Read a JSON file safely."""
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read {path}: {e}")
        return None

    def _write_json(self, path: Path, data: dict[str, Any]) -> bool:
        """Write a JSON file safely."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return True
        except OSError as e:
            logger.warning(f"Failed to write {path}: {e}")
            return False

    def is_initialized(self) -> bool:
        """Check if metadata has been initialized in this repository."""
        return (self.metadata_dir / "metadata.json").exists()

    def initialize(self, server_id: str | None = None) -> dict[str, Any]:
        """Initialize metadata directory structure.

        Args:
            server_id: Optional server identifier

        Returns:
            The created metadata dict
        """
        self._ensure_dirs()

        metadata = {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "server_id": server_id,
            "type": "hypervisor_backups",
        }

        self._write_json(self.metadata_dir / "metadata.json", metadata)
        logger.info(f"Initialized hypervisor metadata at {self.metadata_dir}")
        return metadata

    def save_hypervisor(
        self,
        hypervisor_id: str,
        name: str,
        hypervisor_type: str,
        host: str,
        **extra: Any,
    ) -> bool:
        """Save hypervisor information.

        Args:
            hypervisor_id: Unique hypervisor ID
            name: Hypervisor display name
            hypervisor_type: Type (e.g., "proxmox")
            host: Hypervisor host address
            **extra: Additional metadata

        Returns:
            True if saved successfully
        """
        self._ensure_dirs()

        path = self.metadata_dir / "hypervisors" / f"{safe_filename(hypervisor_id)}.json"
        existing = self._read_json(path) or {}

        data = {
            **existing,
            "hypervisor_id": hypervisor_id,
            "name": name,
            "hypervisor_type": hypervisor_type,
            "host": host,
            "updated_at": datetime.now().isoformat(),
            **extra,
        }

        if "first_seen" not in data:
            data["first_seen"] = datetime.now().isoformat()

        return self._write_json(path, data)

    def get_hypervisor(self, hypervisor_id: str) -> dict[str, Any] | None:
        """Get hypervisor metadata by ID."""
        path = self.metadata_dir / "hypervisors" / f"{safe_filename(hypervisor_id)}.json"
        return self._read_json(path)

    def list_hypervisors(self) -> list[dict[str, Any]]:
        """List all hypervisors in metadata."""
        hypervisors_dir = self.metadata_dir / "hypervisors"
        if not hypervisors_dir.exists():
            return []

        result = []
        for path in hypervisors_dir.glob("*.json"):
            data = self._read_json(path)
            if data:
                result.append(data)
        return result

    def save_guest(
        self,
        vmid: int,
        name: str,
        guest_type: str,
        node: str,
        hypervisor_id: str,
        **extra: Any,
    ) -> bool:
        """Save VM/container guest information.

        Args:
            vmid: VM/container ID
            name: Guest display name
            guest_type: Type (qemu or lxc)
            node: Proxmox node name
            hypervisor_id: Parent hypervisor ID
            **extra: Additional metadata

        Returns:
            True if saved successfully
        """
        self._ensure_dirs()

        guest_dir = self.metadata_dir / "hypervisor_backups" / str(vmid)
        guest_dir.mkdir(parents=True, exist_ok=True)

        path = guest_dir / "guest.json"
        existing = self._read_json(path) or {}

        data = {
            **existing,
            "vmid": vmid,
            "name": name,
            "guest_type": guest_type,
            "node": node,
            "hypervisor_id": hypervisor_id,
            "updated_at": datetime.now().isoformat(),
            **extra,
        }

        if "first_seen" not in data:
            data["first_seen"] = datetime.now().isoformat()

        return self._write_json(path, data)

    def get_guest(self, vmid: int) -> dict[str, Any] | None:
        """Get guest metadata by VMID."""
        path = self.metadata_dir / "hypervisor_backups" / str(vmid) / "guest.json"
        return self._read_json(path)

    def list_guests(self) -> list[dict[str, Any]]:
        """List all guests in metadata."""
        backups_dir = self.metadata_dir / "hypervisor_backups"
        if not backups_dir.exists():
            return []

        result = []
        for guest_dir in backups_dir.iterdir():
            if guest_dir.is_dir():
                guest_path = guest_dir / "guest.json"
                data = self._read_json(guest_path)
                if data:
                    result.append(data)
        return result

    def save_backup_run(
        self,
        vmid: int,
        run_id: str,
        status: str,
        backup_file: str,
        started_at: datetime | str,
        finished_at: datetime | str | None = None,
        size_bytes: int | None = None,
        duration_seconds: float | None = None,
        backup_type: str = "full",
        **extra: Any,
    ) -> bool:
        """Save a backup run record.

        Args:
            vmid: VM/container ID
            run_id: Unique run identifier
            status: Run status (success, failed, skipped)
            backup_file: Backup filename (vzdump archive)
            started_at: When backup started
            finished_at: When backup finished
            size_bytes: Backup file size
            duration_seconds: Backup duration
            backup_type: Type of backup (full or skip)
            **extra: Additional metadata

        Returns:
            True if saved successfully
        """
        self._ensure_dirs()

        runs_dir = self.metadata_dir / "hypervisor_backups" / str(vmid) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        path = runs_dir / f"{safe_filename(run_id)}.json"

        data = {
            "run_id": run_id,
            "vmid": vmid,
            "status": status,
            "backup_file": backup_file,
            "backup_type": backup_type,
            "started_at": started_at.isoformat() if isinstance(started_at, datetime) else started_at,
            "finished_at": finished_at.isoformat() if isinstance(finished_at, datetime) else finished_at,
            "size_bytes": size_bytes,
            "duration_seconds": duration_seconds,
            "recorded_at": datetime.now().isoformat(),
            **extra,
        }

        return self._write_json(path, data)

    def get_backup_runs(self, vmid: int, limit: int = 50) -> list[dict[str, Any]]:
        """Get backup runs for a guest.

        Args:
            vmid: VM/container ID
            limit: Maximum number of runs to return

        Returns:
            List of run records, newest first
        """
        runs_dir = self.metadata_dir / "hypervisor_backups" / str(vmid) / "runs"
        if not runs_dir.exists():
            return []

        runs = []
        for path in runs_dir.glob("*.json"):
            data = self._read_json(path)
            if data:
                runs.append(data)

        # Sort by started_at descending
        runs.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return runs[:limit]

    def get_latest_run(self, vmid: int) -> dict[str, Any] | None:
        """Get the most recent backup run for a guest."""
        runs = self.get_backup_runs(vmid, limit=1)
        return runs[0] if runs else None

    def discover_all(self) -> dict[str, Any]:
        """Discover all metadata in this repository.

        Returns:
            Dict containing:
            - initialized: Whether metadata exists
            - hypervisors: List of hypervisor metadata
            - guests: List of guest metadata with their runs
            - summary: Counts and stats
        """
        if not self.is_initialized():
            return {
                "initialized": False,
                "hypervisors": [],
                "guests": [],
                "summary": {
                    "hypervisor_count": 0,
                    "guest_count": 0,
                    "total_runs": 0,
                },
            }

        hypervisors = self.list_hypervisors()
        guests = self.list_guests()

        # Add run counts to each guest
        total_runs = 0
        for guest in guests:
            vmid = guest.get("vmid")
            if vmid:
                runs = self.get_backup_runs(vmid)
                guest["run_count"] = len(runs)
                guest["latest_run"] = runs[0] if runs else None
                total_runs += len(runs)

        return {
            "initialized": True,
            "hypervisors": hypervisors,
            "guests": guests,
            "summary": {
                "hypervisor_count": len(hypervisors),
                "guest_count": len(guests),
                "total_runs": total_runs,
            },
        }

    def discover_from_vzdump_files(self, dump_path: Path | None = None) -> list[dict[str, Any]]:
        """Discover backup files from vzdump naming convention.

        Proxmox vzdump files follow the pattern:
        vzdump-{type}-{vmid}-{date}_{time}.vma.zst

        Args:
            dump_path: Path to dump directory (default: repo_path/dump)

        Returns:
            List of discovered backup info dicts
        """
        if dump_path is None:
            dump_path = self.repo_path / "dump"

        if not dump_path.exists():
            return []

        # Pattern: vzdump-qemu-100-2025_12_06-20_35_59.vma.zst
        pattern = re.compile(
            r"vzdump-(qemu|lxc)-(\d+)-(\d{4}_\d{2}_\d{2})-(\d{2}_\d{2}_\d{2})\.vma(?:\.(zst|gz|lzo))?"
        )

        discovered = []
        for path in dump_path.iterdir():
            if not path.is_file():
                continue

            match = pattern.match(path.name)
            if match:
                guest_type, vmid, date_str, time_str, compression = match.groups()

                # Parse datetime
                try:
                    dt = datetime.strptime(f"{date_str}_{time_str}", "%Y_%m_%d_%H_%M_%S")
                except ValueError:
                    dt = None

                discovered.append({
                    "filename": path.name,
                    "guest_type": guest_type,
                    "vmid": int(vmid),
                    "backup_time": dt.isoformat() if dt else None,
                    "compression": compression or "none",
                    "size_bytes": path.stat().st_size,
                    "path": str(path),
                })

        # Sort by backup time descending
        discovered.sort(key=lambda x: x.get("backup_time") or "", reverse=True)
        return discovered
