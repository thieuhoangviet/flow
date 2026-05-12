"""Database storage layer for Flow2API"""
import asyncio
import aiosqlite
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from ..config import DEFAULT_YESCAPTCHA_TASK_TYPE, normalize_yescaptcha_task_type
from ..models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig


from .mixins.base import DatabaseBaseMixin
from .mixins.tokens import DatabaseTokensMixin
from .mixins.stats import DatabaseStatsMixin
from .mixins.projects import DatabaseProjectsMixin
from .mixins.config import DatabaseConfigMixin
from .mixins.logs import DatabaseLogsMixin

class Database(
    DatabaseBaseMixin,
    DatabaseTokensMixin,
    DatabaseStatsMixin,
    DatabaseProjectsMixin,
    DatabaseConfigMixin,
    DatabaseLogsMixin
):
    """SQLite database manager"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Store database in data directory
            data_dir = Path(__file__).parent.parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "flow.db")
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._connect_timeout = 30
        self._busy_timeout_ms = 30000

