"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：维护全局共享的常驻标签页池，即时生成 token
"""
import asyncio
import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import gc
import inspect
import math
import random
import time
import os
import sys
import re
import signal
import json
import hashlib
import mimetypes
import shutil
import tempfile
import subprocess
import types
from pathlib import Path
from typing import Optional, Dict, Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from src.core.logger import debug_logger
from src.core.config import config
from src.services.browser_cookie_utils import (
    build_browser_cookie_targets,
    build_cookie_signature,
    merge_browser_cookie_payloads,
    normalize_cookie_storage_text,
)

# flow2api 缺少的配置常量和函数，内联定义
TOKEN_POOL_SIZE_MAX = 500

def resolve_effective_browser_count(value) -> int:
    try:
        current = max(1, min(20, int(value or 1)))
    except Exception:
        current = 1
    return current

def resolve_effective_personal_max_resident_tabs(value) -> int:
    try:
        current = max(1, min(50, int(value or 1)))
    except Exception:
        current = 1
    return current

from .constants import *
from .utils import *
from .utils import _get_recaptcha_script_cache_dir, _get_recaptcha_asset_cache_dir
from .models import *
from .mixins.lifecycle import BrowserLifecycleMixin
from .mixins.tabs import BrowserTabsMixin
from .mixins.fingerprint import BrowserFingerprintMixin
from .mixins.session import BrowserSessionMixin
from .mixins.solver import BrowserSolverMixin

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .pool_service import _PersonalBrowserPoolService

class BrowserCaptchaService(
    BrowserLifecycleMixin,
    BrowserTabsMixin,
    BrowserFingerprintMixin,
    BrowserSessionMixin,
    BrowserSolverMixin
):
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 维护全局共享常驻标签页池，谁抢到空闲页谁执行
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """
    _instance: Optional['BrowserCaptchaService'] = None
    _pool_instance: Optional['_PersonalBrowserPoolService'] = None
    _lock = asyncio.Lock()
    _launch_gate: Optional[asyncio.Semaphore] = None
    _launch_gate_loop: Optional[asyncio.AbstractEventLoop] = None
    _launch_gate_limit: int = 0

    def __init__(
        self,
        db=None,
        *,
        browser_instance_id: int = 0,
        max_resident_tabs_override: Optional[int] = None,
    ):
        """初始化服务"""
        self.headless = bool(getattr(config, "personal_headless", False))  # 是否无头由配置控制
        self.browser = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        self._browser_instance_id = 0
        self._slot_id_prefix = ""
        self._max_resident_tabs_override: Optional[int] = None
        self._apply_browser_instance_identity(browser_instance_id)
        self.apply_pool_worker_settings(
            browser_instance_id=browser_instance_id,
            max_resident_tabs_override=max_resident_tabs_override,
        )
        self._runtime_ephemeral_user_data_dir: Optional[str] = None
        self._managed_runtime_profile_dirs: set[str] = set()
        self._browser_process_pid: Optional[int] = None
        self.user_data_dir = self._resolve_user_data_dir(self.headless)
        self._visible_startup_target_id: Optional[str] = None
        self._headless_host_target_id: Optional[str] = None
        self._runtime_fingerprint_spoof_seed = ""
        self._runtime_surface_profile: Dict[str, Any] = {}
        self._refresh_runtime_fingerprint_spoof_seed()

        # 常驻模式相关属性
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # slot_id -> 常驻标签页信息
        self._token_resident_affinity: dict[str, str] = {}  # token_id -> slot_id（优先保证 token 独占 context）
        self._project_resident_affinity: dict[str, str] = {}  # project_id -> slot_id（最近一次使用）
        self._resident_slot_seq = 0
        self._resident_pick_index = 0
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作
        self._browser_lock = asyncio.Lock()  # 保护浏览器初始化/关闭/重启，避免重复拉起实例
        self._runtime_recover_lock = asyncio.Lock()  # 串行化浏览器级恢复，避免并发重启风暴
        self._tab_build_lock = asyncio.Lock()  # 串行化冷启动/重建，降低 nodriver 抖动
        self._legacy_lock = asyncio.Lock()  # 避免 legacy fallback 并发失控创建临时标签页
        configured_total_tabs = getattr(config, "personal_max_resident_tabs", 5)
        self._max_resident_tabs = self._resolve_personal_max_resident_tabs(configured_total_tabs)
        self._idle_tab_ttl_seconds = max(
            60,
            int(getattr(config, "personal_idle_tab_ttl_seconds", 600) or 600),
        )
        self._idle_reaper_task: Optional[asyncio.Task] = None  # 空闲回收任务
        self._command_timeout_seconds = 8.0
        self._navigation_timeout_seconds = 20.0
        self._solve_timeout_seconds = 45.0
        self._session_refresh_timeout_seconds = 45.0
        self._health_probe_ttl_seconds = max(
            0.0,
            float(getattr(config, "browser_personal_health_probe_ttl_seconds", 10.0) or 10.0),
        )
        self._last_health_probe_at = 0.0
        self._last_health_probe_ok = False
        self._fingerprint_cache_ttl_seconds = max(
            0.0,
            float(getattr(config, "browser_personal_fingerprint_ttl_seconds", 300.0) or 300.0),
        )
        self._last_fingerprint_at = 0.0

        # 兼容旧 API（保留 single resident 属性作为别名）
        self.resident_project_id: Optional[str] = None  # 向后兼容
        self.resident_tab = None                         # 向后兼容
        self._running = False                            # 向后兼容
        self._recaptcha_ready = False                    # 向后兼容
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._resident_error_streaks: dict[str, int] = {}
        self._resident_unavailable_slots: set[str] = set()
        self._resident_warmup_task: Optional[asyncio.Task] = None
        self._resident_rebuild_tasks: dict[str, asyncio.Task] = {}
        self._resident_recovery_tasks: dict[str, asyncio.Task] = {}
        self._last_runtime_restart_at = 0.0
        self._runtime_last_active_at = time.time()
        self._successful_solves_since_browser_start = 0
        self._fresh_profile_restart_pending = False
        self._fresh_profile_restart_task: Optional[asyncio.Task] = None
        self._fresh_profile_restart_force_pending = False
        self._browser_launch_failure_streak = 0
        self._browser_launch_cooldown_until = 0.0
        self._browser_launch_last_error = ""
        self._fresh_profile_restart_pending_reason = ""
        self._proxy_url: Optional[str] = None
        self._proxy_ext_dir: Optional[str] = None
        self._proxy_config_signature: str = ""
        self._recaptcha_script_cache_dir = _get_recaptcha_script_cache_dir()
        self._recaptcha_script_cache_lock = asyncio.Lock()
        self._recaptcha_asset_cache_dir = _get_recaptcha_asset_cache_dir()
        self._recaptcha_asset_cache_lock = asyncio.Lock()
        self._recaptcha_asset_data_url_cache: dict[str, str] = {}
        self._recaptcha_asset_bundle_signature: Optional[str] = None
        self._recaptcha_asset_bundle: Optional[Dict[str, Any]] = None
        self._recaptcha_asset_hook_source: Optional[str] = None
        # 自定义站点打码常驻页（用于 score-test）
        self._custom_tabs: dict[str, Dict[str, Any]] = {}
        self._custom_lock = asyncio.Lock()
        self._refresh_runtime_tunables()

    @classmethod
    async def get_instance(cls, db=None):
        """获取单例实例"""
        close_single_instance = None
        close_pool_instance = None
        async with cls._lock:
            use_pool = cls._resolve_configured_browser_count() > 1 or bool(
                getattr(config, "token_pool_enabled", False)
            )

            if use_pool:
                if cls._instance is not None:
                    close_single_instance = cls._instance
                    cls._instance = None
                if cls._pool_instance is None:
                    from .pool_service import _PersonalBrowserPoolService
                    cls._pool_instance = _PersonalBrowserPoolService(db)
                elif db is not None:
                    cls._pool_instance.db = db
                service = cls._pool_instance
            else:
                if cls._pool_instance is not None:
                    close_pool_instance = cls._pool_instance
                    cls._pool_instance = None
                if cls._instance is None:
                    cls._instance = cls(db)
                    cls._instance._idle_reaper_task = asyncio.create_task(
                        cls._instance._idle_tab_reaper_loop()
                    )
                elif db is not None:
                    cls._instance.db = db
                service = cls._instance

        if close_single_instance is not None:
            try:
                await close_single_instance.close()
            except Exception:
                pass
        if close_pool_instance is not None:
            try:
                await close_pool_instance.close()
            except Exception:
                pass

        from .pool_service import _PersonalBrowserPoolService
        if isinstance(service, _PersonalBrowserPoolService):
            await service.reload_config()
        return service

