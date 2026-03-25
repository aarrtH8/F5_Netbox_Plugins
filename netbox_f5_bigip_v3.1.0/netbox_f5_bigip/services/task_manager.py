"""Gestionnaire de tâches asynchrones."""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable
from enum import Enum


class TaskStatus(Enum):
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    ERROR = 'ERROR'


@dataclass
class Task:
    id: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    current_step: str = ''
    logs: List[Dict[str, str]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    
    def add_log(self, message: str, level: str = 'info'):
        self.logs.append({
            'message': message,
            'level': level,
            'time': time.time()
        })
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'task_id': self.id,
            'status': self.status.value,
            'progress': self.progress,
            'current_step': self.current_step,
            'logs': self.logs,
            'result': self.result,
            'error': self.error,
        }


class TaskManager:
    """Gestionnaire de tâches en mémoire."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks = {}
                    cls._instance._cleanup_thread = None
        return cls._instance
    
    def create_task(self) -> Task:
        task_id = str(uuid.uuid4())
        task = Task(id=task_id)
        self._tasks[task_id] = task
        self._start_cleanup_if_needed()
        return task
    
    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)
    
    def _start_cleanup_if_needed(self):
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._cleanup_thread = threading.Thread(target=self._cleanup_old_tasks, daemon=True)
            self._cleanup_thread.start()
    
    def _cleanup_old_tasks(self):
        """Nettoyer les tâches terminées après 15 minutes."""
        while True:
            time.sleep(60)
            now = time.time()
            to_remove = []
            for task_id, task in list(self._tasks.items()):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.ERROR):
                    if now - task.created_at > 900:  # 15 minutes
                        to_remove.append(task_id)
            for task_id in to_remove:
                self._tasks.pop(task_id, None)


def run_task_async(task: Task, func: Callable, *args, **kwargs):
    """Exécuter une fonction dans un thread séparé."""
    def wrapper():
        try:
            task.status = TaskStatus.RUNNING
            result = func(*args, **kwargs)
            task.result = result
            task.status = TaskStatus.COMPLETED
            task.progress = 100
        except Exception as e:
            task.error = str(e)
            task.status = TaskStatus.ERROR
            task.add_log(f"Erreur: {e}", 'error')
    
    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    return thread
