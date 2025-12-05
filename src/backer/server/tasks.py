"""Background task manager for long-running operations.

This module provides a simple task queue for operations that would otherwise
block the web UI (like SMB deletes, repository operations, etc.).
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Status of a background task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """A background task."""

    id: str
    task_type: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    message: str = ""
    result: Any = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": self.id,
            "task_type": self.task_type,
            "description": self.description,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TaskManager:
    """Manages background tasks with a simple thread pool."""

    def __init__(self, max_workers: int = 3):
        self.max_workers = max_workers
        self._tasks: dict[str, Task] = {}
        self._queue: list[str] = []  # Task IDs waiting to run
        self._lock = threading.Lock()
        self._running_count = 0
        self._shutdown = False

        # Start the task processor thread
        self._processor_thread = threading.Thread(
            target=self._process_queue,
            daemon=True,
            name="TaskManager-Processor",
        )
        self._processor_thread.start()

    def submit(
        self,
        task_type: str,
        description: str,
        func: Callable[["Task"], Any],
    ) -> Task:
        """Submit a new task to run in the background.

        Args:
            task_type: Type of task (e.g., "delete_agent", "delete_repository")
            description: Human-readable description for the UI
            func: Function to execute. Receives the Task object for progress updates.

        Returns:
            The created Task object.
        """
        task_id = str(uuid4())[:8]
        task = Task(
            id=task_id,
            task_type=task_type,
            description=description,
        )

        with self._lock:
            self._tasks[task_id] = task
            self._queue.append(task_id)

        logger.info(f"[TASKS] Submitted task {task_id}: {description}")
        return task

    def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    def get_active_tasks(self) -> list[Task]:
        """Get all pending or running tasks."""
        with self._lock:
            return [
                t for t in self._tasks.values()
                if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
            ]

    def get_recent_tasks(self, limit: int = 10) -> list[Task]:
        """Get recent tasks (including completed/failed)."""
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda t: t.created_at,
                reverse=True,
            )
            return tasks[:limit]

    def cleanup_old_tasks(self, max_age_seconds: int = 3600) -> None:
        """Remove completed/failed tasks older than max_age_seconds."""
        cutoff = datetime.now().timestamp() - max_age_seconds
        with self._lock:
            to_remove = [
                task_id
                for task_id, task in self._tasks.items()
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                and task.finished_at
                and task.finished_at.timestamp() < cutoff
            ]
            for task_id in to_remove:
                del self._tasks[task_id]

    def _process_queue(self) -> None:
        """Background thread that processes the task queue."""
        while not self._shutdown:
            task_id = None

            # Get next task if we have capacity
            with self._lock:
                if self._queue and self._running_count < self.max_workers:
                    task_id = self._queue.pop(0)
                    self._running_count += 1

            if task_id:
                task = self._tasks.get(task_id)
                if task:
                    self._run_task(task)
                with self._lock:
                    self._running_count -= 1
            else:
                # No work to do, sleep briefly
                time.sleep(0.1)

    def _run_task(self, task: Task) -> None:
        """Execute a single task."""
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        task.message = "Starting..."

        logger.info(f"[TASKS] Running task {task.id}: {task.description}")

        try:
            # The task function is stored in the task's result temporarily
            # This is a bit of a hack, but keeps the API simple
            func = task.result
            task.result = None

            if callable(func):
                result = func(task)
                task.result = result

            task.status = TaskStatus.COMPLETED
            task.progress = 100
            task.message = "Completed"
            logger.info(f"[TASKS] Task {task.id} completed successfully")

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.message = f"Failed: {e}"
            logger.error(f"[TASKS] Task {task.id} failed: {e}")

        finally:
            task.finished_at = datetime.now()

    def submit_with_func(
        self,
        task_type: str,
        description: str,
        func: Callable[["Task"], Any],
    ) -> Task:
        """Submit a task with its function."""
        task = self.submit(task_type, description, func)
        # Store the function temporarily in result (will be cleared when run)
        task.result = func
        return task

    def shutdown(self) -> None:
        """Shutdown the task manager."""
        self._shutdown = True
        if self._processor_thread.is_alive():
            self._processor_thread.join(timeout=5)


# Global task manager instance
_task_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    """Get or create the global task manager."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager


def shutdown_task_manager() -> None:
    """Shutdown the global task manager."""
    global _task_manager
    if _task_manager:
        _task_manager.shutdown()
        _task_manager = None
