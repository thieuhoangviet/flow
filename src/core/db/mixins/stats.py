import asyncio
import aiosqlite
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional, List, Dict, Any
from pathlib import Path
from ...config import DEFAULT_YESCAPTCHA_TASK_TYPE, normalize_yescaptcha_task_type
from ...models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig

class DatabaseStatsMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def get_dashboard_stats(self, user_id: Optional[int] = None) -> Dict[str, int]:
        """Get dashboard counters with aggregated SQL queries"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()

            token_query = """
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens
                FROM tokens
            """
            token_params = []
            if user_id is not None:
                token_query += " WHERE owner_id = ?"
                token_params.append(user_id)

            token_cursor = await db.execute(token_query, tuple(token_params))
            token_row = await token_cursor.fetchone()

            stats_query = """
                SELECT
                    COALESCE(SUM(ts.image_count), 0) AS total_images,
                    COALESCE(SUM(ts.video_count), 0) AS total_videos,
                    COALESCE(SUM(ts.error_count), 0) AS total_errors,
                    COALESCE(SUM(CASE WHEN ts.today_date = ? THEN ts.today_image_count ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE WHEN ts.today_date = ? THEN ts.today_video_count ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE WHEN ts.today_date = ? THEN ts.today_error_count ELSE 0 END), 0) AS today_errors
                FROM token_stats ts
            """
            stats_params = [today, today, today]
            if user_id is not None:
                stats_query += " JOIN tokens t ON ts.token_id = t.id WHERE t.owner_id = ?"
                stats_params.append(user_id)

            stats_cursor = await db.execute(stats_query, tuple(stats_params))
            stats_row = await stats_cursor.fetchone()

            token_data = dict(token_row) if token_row else {}
            stats_data = dict(stats_row) if stats_row else {}

            return {
                "total_tokens": int(token_data.get("total_tokens") or 0),
                "active_tokens": int(token_data.get("active_tokens") or 0),
                "total_images": int(stats_data.get("total_images") or 0),
                "total_videos": int(stats_data.get("total_videos") or 0),
                "total_errors": int(stats_data.get("total_errors") or 0),
                "today_images": int(stats_data.get("today_images") or 0),
                "today_videos": int(stats_data.get("today_videos") or 0),
                "today_errors": int(stats_data.get("today_errors") or 0)
            }

    async def get_system_info_stats(self, user_id: Optional[int] = None) -> Dict[str, int]:
        """Get lightweight system counters used by admin dashboard"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN credits ELSE 0 END), 0) AS total_credits
                FROM tokens
            """
            params = []
            if user_id is not None:
                query += " WHERE owner_id = ?"
                params.append(user_id)
                
            cursor = await db.execute(query, tuple(params))
            row = await cursor.fetchone()
            data = dict(row) if row else {}
            return {
                "total_tokens": int(data.get("total_tokens") or 0),
                "active_tokens": int(data.get("active_tokens") or 0),
                "total_credits": int(data.get("total_credits") or 0)
            }

    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's image usage.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_video_count = 0,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's video usage.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_image_count = 0,
                        today_video_count = 1,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's error.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_image_count = 0,
                        today_video_count = 0,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment all counters
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        async with self._connect(write=True) as db:
            await db.execute("""
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """, (token_id,))
            await db.commit()

