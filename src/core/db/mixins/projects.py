import asyncio
import aiosqlite
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from ...config import DEFAULT_YESCAPTCHA_TASK_TYPE, normalize_yescaptcha_task_type
from ...models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig

class DatabaseProjectsMixin:
    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO projects (project_id, token_id, project_name, tool_name, is_active, owner_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (project.project_id, project.token_id, project.project_name,
                  project.tool_name, project.is_active, project.owner_id))
            await db.commit()
            return cursor.lastrowid

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC",
                (token_id,)
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()

    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO tasks (task_id, token_id, model, prompt, status, progress, scene_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task.task_id, task.token_id, task.model, task.prompt,
                  task.status, task.progress, task.scene_id))
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                task_dict = dict(row)
                # Parse result_urls from JSON
                if task_dict.get("result_urls"):
                    task_dict["result_urls"] = json.loads(task_dict["result_urls"])
                return Task(**task_dict)
            return None

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    # Convert list to JSON string for result_urls
                    if key == "result_urls" and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()

