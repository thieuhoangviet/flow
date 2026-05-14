import asyncio
import aiosqlite
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional, List, Dict, Any
from pathlib import Path
from ...config import DEFAULT_YESCAPTCHA_TASK_TYPE, normalize_yescaptcha_task_type
from ...models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig

class DatabaseConfigMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            return None

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE admin_config SET {', '.join(updates)} WHERE id = 1"
                await db.execute(query, params)
                await db.commit()

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            return None

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None
    ):
        """Update proxy configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_media_proxy_enabled = (
                    media_proxy_enabled
                    if media_proxy_enabled is not None
                    else current.get("media_proxy_enabled", False)
                )
                new_media_proxy_url = (
                    media_proxy_url
                    if media_proxy_url is not None
                    else current.get("media_proxy_url")
                )

                await db.execute("""
                    UPDATE proxy_config
                    SET enabled = ?, proxy_url = ?,
                        media_proxy_enabled = ?, media_proxy_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url))
            else:
                new_media_proxy_enabled = media_proxy_enabled if media_proxy_enabled is not None else False
                new_media_proxy_url = media_proxy_url
                await db.execute("""
                    INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                    VALUES (1, ?, ?, ?, ?)
                """, (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url))

            await db.commit()

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            return None

    async def update_generation_config(
        self,
        image_timeout: Optional[int] = None,
        video_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """Update generation configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}

            normalized_image_timeout = (
                image_timeout
                if image_timeout is not None
                else current.get("image_timeout", 300)
            )
            normalized_video_timeout = (
                video_timeout
                if video_timeout is not None
                else current.get("video_timeout", 1500)
            )
            try:
                normalized_max_retries = (
                    max(1, int(max_retries))
                    if max_retries is not None
                    else max(1, int(current.get("max_retries", 3)))
                )
            except Exception:
                normalized_max_retries = 3

            if row:
                await db.execute("""
                    UPDATE generation_config
                    SET image_timeout = ?, video_timeout = ?, max_retries = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (normalized_image_timeout, normalized_video_timeout, normalized_max_retries))
            else:
                await db.execute("""
                    INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                    VALUES (1, ?, ?, ?)
                """, (normalized_image_timeout, normalized_video_timeout, normalized_max_retries))
            await db.commit()

    async def get_call_logic_config(self) -> CallLogicConfig:
        """Get token call logic configuration."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM call_logic_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                row_dict = dict(row)
                mode = row_dict.get("call_mode")
                if mode not in ("default", "polling"):
                    row_dict["call_mode"] = "polling" if row_dict.get("polling_mode_enabled") else "default"
                return CallLogicConfig(**row_dict)
            return CallLogicConfig(call_mode="default", polling_mode_enabled=False)

    async def update_call_logic_config(self, call_mode: str):
        """Update token call logic configuration."""
        normalized = "polling" if call_mode == "polling" else "default"
        polling_mode_enabled = normalized == "polling"
        async with self._connect(write=True) as db:
            await db.execute("""
                INSERT OR REPLACE INTO call_logic_config (id, call_mode, polling_mode_enabled, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            """, (normalized, polling_mode_enabled))
            await db.commit()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with self._connect(write=True) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from ...config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)
            config.set_flow_max_retries(generation_config.max_retries)

        # Reload call logic config
        call_logic_config = await self.get_call_logic_config()
        if call_logic_config:
            config.set_call_logic_mode(call_logic_config.call_mode)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Reload captcha config
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method(captcha_config.captcha_method)
            config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
            config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)
            config.set_yescaptcha_task_type(captcha_config.yescaptcha_task_type)
            config.set_capmonster_api_key(captcha_config.capmonster_api_key)
            config.set_capmonster_base_url(captcha_config.capmonster_base_url)
            config.set_ezcaptcha_api_key(captcha_config.ezcaptcha_api_key)
            config.set_ezcaptcha_base_url(captcha_config.ezcaptcha_base_url)
            config.set_capsolver_api_key(captcha_config.capsolver_api_key)
            config.set_capsolver_base_url(captcha_config.capsolver_base_url)
            config.set_remote_browser_base_url(captcha_config.remote_browser_base_url)
            config.set_remote_browser_api_key(captcha_config.remote_browser_api_key)
            config.set_remote_browser_timeout(captcha_config.remote_browser_timeout)
            config.set_browser_count(captcha_config.browser_count)
            config.set_personal_project_pool_size(captcha_config.personal_project_pool_size)
            config.set_personal_max_resident_tabs(captcha_config.personal_max_resident_tabs)
            config.set_browser_personal_fresh_restart_every_n_solves(
                captcha_config.browser_personal_fresh_restart_every_n_solves
            )
            config.set_personal_idle_tab_ttl_seconds(captcha_config.personal_idle_tab_ttl_seconds)
            config.set_personal_headless(captcha_config.personal_headless)

    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # Return default if not found
            return CacheConfig(cache_enabled=False, cache_timeout=7200)

    async def update_cache_config(self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None):
        """Update cache configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("cache_enabled", False)
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 7200)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")

                # If base_url is explicitly set to empty string, treat as None
                if base_url == "":
                    new_base_url = None

                await db.execute("""
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_timeout, new_base_url))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 7200
                new_base_url = base_url if base_url is not None else None

                await db.execute("""
                    INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                    VALUES (1, ?, ?, ?)
                """, (new_enabled, new_timeout, new_base_url))

            await db.commit()

    async def get_debug_config(self) -> 'DebugConfig':
        """Get debug configuration"""
        from ...models import DebugConfig
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return DebugConfig(**dict(row))
            # Return default if not found
            return DebugConfig(enabled=False, log_requests=True, log_responses=True, mask_token=True)

    async def update_debug_config(
        self,
        enabled: bool = None,
        log_requests: bool = None,
        log_responses: bool = None,
        mask_token: bool = None
    ):
        """Update debug configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("enabled", False)
                new_log_requests = log_requests if log_requests is not None else current.get("log_requests", True)
                new_log_responses = log_responses if log_responses is not None else current.get("log_responses", True)
                new_mask_token = mask_token if mask_token is not None else current.get("mask_token", True)

                await db.execute("""
                    UPDATE debug_config
                    SET enabled = ?, log_requests = ?, log_responses = ?, mask_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_log_requests = log_requests if log_requests is not None else True
                new_log_responses = log_responses if log_responses is not None else True
                new_mask_token = mask_token if mask_token is not None else True

                await db.execute("""
                    INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                    VALUES (1, ?, ?, ?, ?)
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))

            await db.commit()

    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CaptchaConfig(**dict(row))
            return CaptchaConfig()

    async def update_captcha_config(
        self,
        captcha_method: str = None,
        yescaptcha_api_key: str = None,
        yescaptcha_base_url: str = None,
        yescaptcha_task_type: str = None,
        capmonster_api_key: str = None,
        capmonster_base_url: str = None,
        ezcaptcha_api_key: str = None,
        ezcaptcha_base_url: str = None,
        capsolver_api_key: str = None,
        capsolver_base_url: str = None,
        remote_browser_base_url: str = None,
        remote_browser_api_key: str = None,
        remote_browser_timeout: int = None,
        browser_proxy_enabled: bool = None,
        browser_proxy_url: str = None,
        browser_count: int = None,
        personal_project_pool_size: int = None,
        personal_max_resident_tabs: int = None,
        browser_personal_fresh_restart_every_n_solves: int = None,
        personal_idle_tab_ttl_seconds: int = None,
        personal_headless: bool = None
    ):
        """Update captcha configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_method = captcha_method if captcha_method is not None else current.get("captcha_method", "yescaptcha")
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else current.get("yescaptcha_api_key", "")
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else current.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                new_yes_task_type = normalize_yescaptcha_task_type(
                    yescaptcha_task_type if yescaptcha_task_type is not None else current.get("yescaptcha_task_type")
                )
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else current.get("capmonster_api_key", "")
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else current.get("capmonster_base_url", "https://api.capmonster.cloud")
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else current.get("ezcaptcha_api_key", "")
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else current.get("ezcaptcha_base_url", "https://api.ez-captcha.com")
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else current.get("capsolver_api_key", "")
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else current.get("capsolver_base_url", "https://api.capsolver.com")
                new_remote_base_url = remote_browser_base_url if remote_browser_base_url is not None else current.get("remote_browser_base_url", "")
                new_remote_api_key = remote_browser_api_key if remote_browser_api_key is not None else current.get("remote_browser_api_key", "")
                new_remote_timeout = remote_browser_timeout if remote_browser_timeout is not None else current.get("remote_browser_timeout", 60)
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else current.get("browser_proxy_enabled", False)
                new_proxy_url = browser_proxy_url if browser_proxy_url is not None else current.get("browser_proxy_url")
                new_browser_count = browser_count if browser_count is not None else current.get("browser_count", 1)
                new_personal_project_pool_size = personal_project_pool_size if personal_project_pool_size is not None else current.get("personal_project_pool_size", 4)
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else current.get("personal_max_resident_tabs", 5)
                new_personal_fresh_restart_every = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else current.get("browser_personal_fresh_restart_every_n_solves", 10)
                )
                new_personal_idle_ttl = personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else current.get("personal_idle_tab_ttl_seconds", 600)
                new_personal_headless = personal_headless if personal_headless is not None else bool(current.get("personal_headless", True))
                new_remote_timeout = max(5, int(new_remote_timeout)) if new_remote_timeout is not None else 60
                new_browser_count = max(1, min(20, int(new_browser_count)))
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))  # 限制1-50
                new_personal_fresh_restart_every = max(0, int(new_personal_fresh_restart_every))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))  # 最少60秒

                await db.execute("""
                    UPDATE captcha_config
                    SET captcha_method = ?, yescaptcha_api_key = ?, yescaptcha_base_url = ?,
                        yescaptcha_task_type = ?,
                        capmonster_api_key = ?, capmonster_base_url = ?,
                        ezcaptcha_api_key = ?, ezcaptcha_base_url = ?,
                        capsolver_api_key = ?, capsolver_base_url = ?,
                        remote_browser_base_url = ?, remote_browser_api_key = ?, remote_browser_timeout = ?,
                        browser_proxy_enabled = ?, browser_proxy_url = ?, browser_count = ?,
                        personal_project_pool_size = ?,
                        personal_max_resident_tabs = ?,
                        browser_personal_fresh_restart_every_n_solves = ?,
                        personal_idle_tab_ttl_seconds = ?,
                        personal_headless = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_method, new_yes_key, new_yes_url, new_yes_task_type,
                      new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url,
                      (new_remote_base_url or "").strip(), (new_remote_api_key or "").strip(), new_remote_timeout,
                      new_proxy_enabled, new_proxy_url, new_browser_count, new_personal_project_pool_size,
                      new_personal_max_tabs, new_personal_fresh_restart_every, new_personal_idle_ttl,
                      new_personal_headless))
            else:
                new_method = captcha_method if captcha_method is not None else "yescaptcha"
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else ""
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else "https://api.yescaptcha.com"
                new_yes_task_type = normalize_yescaptcha_task_type(yescaptcha_task_type)
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else ""
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else "https://api.capmonster.cloud"
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else ""
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else "https://api.ez-captcha.com"
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else ""
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else "https://api.capsolver.com"
                new_remote_base_url = remote_browser_base_url if remote_browser_base_url is not None else ""
                new_remote_api_key = remote_browser_api_key if remote_browser_api_key is not None else ""
                new_remote_timeout = remote_browser_timeout if remote_browser_timeout is not None else 60
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else False
                new_proxy_url = browser_proxy_url
                new_browser_count = browser_count if browser_count is not None else 1
                new_personal_project_pool_size = personal_project_pool_size if personal_project_pool_size is not None else 4
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else 5
                new_personal_fresh_restart_every = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else 10
                )
                new_personal_idle_ttl = personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else 600
                new_personal_headless = personal_headless if personal_headless is not None else True
                new_remote_timeout = max(5, int(new_remote_timeout))
                new_browser_count = max(1, min(20, int(new_browser_count)))
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))
                new_personal_fresh_restart_every = max(0, int(new_personal_fresh_restart_every))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))

                await db.execute("""
                    INSERT INTO captcha_config (id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                        yescaptcha_task_type,
                        capmonster_api_key, capmonster_base_url, ezcaptcha_api_key, ezcaptcha_base_url,
                        capsolver_api_key, capsolver_base_url,
                        remote_browser_base_url, remote_browser_api_key, remote_browser_timeout,
                        browser_proxy_enabled, browser_proxy_url, browser_count,
                        personal_project_pool_size,
                        personal_max_resident_tabs, browser_personal_fresh_restart_every_n_solves,
                        personal_idle_tab_ttl_seconds, personal_headless)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (new_method, new_yes_key, new_yes_url, new_yes_task_type,
                      new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url,
                      (new_remote_base_url or "").strip(), (new_remote_api_key or "").strip(), new_remote_timeout,
                      new_proxy_enabled, new_proxy_url, new_browser_count, new_personal_project_pool_size,
                      new_personal_max_tabs, new_personal_fresh_restart_every, new_personal_idle_ttl,
                      new_personal_headless))

            await db.commit()

    async def get_plugin_config(self) -> PluginConfig:
        """Get plugin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return PluginConfig(**dict(row))
            return PluginConfig()

    async def update_plugin_config(self, connection_token: str, auto_enable_on_update: bool = True, gemini_api_key: str = ""):
        """Update plugin configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                await db.execute("""
                    UPDATE plugin_config
                    SET connection_token = ?, auto_enable_on_update = ?, gemini_api_key = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (connection_token, auto_enable_on_update, gemini_api_key))
            else:
                await db.execute("""
                    INSERT INTO plugin_config (id, connection_token, auto_enable_on_update, gemini_api_key)
                    VALUES (1, ?, ?, ?)
                """, (connection_token, auto_enable_on_update, gemini_api_key))

            await db.commit()

