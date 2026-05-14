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
from .models import *
from .browser_service import BrowserCaptchaService
class _PersonalBrowserPoolService:
    """多浏览器实例调度层。保留现有单浏览器 worker 逻辑，只负责分发与扩缩容。"""

    def __init__(self, db=None):
        self.db = db
        self.headless = bool(getattr(config, "personal_headless", False))
        self._closing = False
        self._workers: list[BrowserCaptchaService] = []
        self._worker_tab_limits: list[int] = []
        self._reload_lock = asyncio.Lock()
        self._worker_dispatch_lock = asyncio.Lock()
        self._round_robin_index = 0
        self._worker_dispatch_reservations: dict[int, int] = {}
        self._project_worker_affinity: dict[str, int] = {}
        self._token_worker_affinity: dict[str, int] = {}
        self._affinity_cache_limit = 256
        self._last_successful_worker_index: Optional[int] = None
        self._idle_worker_reaper_task: Optional[asyncio.Task] = None
        self._token_pool_lock = asyncio.Lock()
        self._token_pool_queues: dict[str, deque[TokenPoolLease]] = {}
        self._token_pool_conditions: dict[str, asyncio.Condition] = {}
        self._token_pool_waiters: dict[str, int] = {}
        self._token_pool_bucket_meta: dict[str, Dict[str, Any]] = {}
        self._token_pool_refill_inflight: dict[str, int] = {}
        self._token_pool_fill_tasks: set[asyncio.Task] = set()
        self._token_pool_maintainer_task: Optional[asyncio.Task] = None
        self._token_pool_last_refill_at = 0.0
        self._token_pool_last_token_at = 0.0
        self._token_pool_stats: dict[str, int] = {
            "hit_count": 0,
            "miss_count": 0,
            "wait_count": 0,
            "expired_count": 0,
            "produced_count": 0,
            "served_count": 0,
            "dropped_count": 0,
        }

    @staticmethod
    def _format_status_timestamp(timestamp_value: float) -> Optional[str]:
        if timestamp_value <= 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp_value).isoformat(timespec="seconds")
        except Exception:
            return None

    def _is_token_pool_enabled(self) -> bool:
        return bool(getattr(config, "token_pool_enabled", False))

    def _get_token_pool_target_size(self) -> int:
        try:
            return max(1, min(TOKEN_POOL_SIZE_MAX, int(getattr(config, "token_pool_size", 2) or 2)))
        except Exception:
            return 2

    def _get_token_pool_seed_project_id(self) -> str:
        return self._normalize_project_key(getattr(config, "token_pool_seed_project_id", "") or "")

    def _get_token_pool_image_target_size(self) -> int:
        try:
            return max(0, min(TOKEN_POOL_SIZE_MAX, int(getattr(config, "token_pool_image_size", 0) or 0)))
        except Exception:
            return 0

    def _get_token_pool_video_target_size(self) -> int:
        try:
            return max(0, min(TOKEN_POOL_SIZE_MAX, int(getattr(config, "token_pool_video_size", 0) or 0)))
        except Exception:
            return 0

    def _get_token_pool_bucket_target_size(
        self,
        *,
        project_id: Optional[str],
        action: Optional[str],
    ) -> int:
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        image_target_size = self._get_token_pool_image_target_size()
        video_target_size = self._get_token_pool_video_target_size()
        default_target_size = self._get_token_pool_target_size()

        if normalized_action == "IMAGE_GENERATION":
            if image_target_size > 0:
                return image_target_size
            if video_target_size <= 0:
                return default_target_size
            return 0
        if normalized_action == "VIDEO_GENERATION":
            if video_target_size > 0:
                return video_target_size
            if image_target_size <= 0:
                return default_target_size
            return 0

        return default_target_size

    def _get_token_pool_wait_timeout_seconds(self) -> float:
        try:
            return float(max(1, min(300, int(getattr(config, "token_pool_wait_timeout_seconds", 30) or 30))))
        except Exception:
            return 30.0

    def _get_token_pool_refill_parallelism(self, target_size: Optional[int] = None) -> int:
        try:
            configured_browser_count = BrowserCaptchaService._resolve_configured_browser_count()
        except Exception:
            configured_browser_count = 1

        total_tabs = self._resolve_total_resident_tabs()
        worker_limits = self._worker_tab_limits or self._build_worker_tab_limits(
            total_tabs,
            min(configured_browser_count, total_tabs),
        )
        refill_capacity = max(
            1,
            sum(1 for limit in worker_limits if int(limit or 0) > 0),
        )
        if target_size is None:
            return refill_capacity
        return max(1, min(max(1, int(target_size)), refill_capacity))

    def _get_token_pool_bucket_keepalive_seconds(self) -> float:
        return max(float(getattr(config, "token_pool_ttl_seconds", 120) or 120) * 2.0, 300.0)

    def _register_configured_token_pool_buckets_locked(self, *, now_value: float) -> None:
        seed_project_id = self._get_token_pool_seed_project_id()
        if not seed_project_id:
            return

        for action in ("IMAGE_GENERATION", "VIDEO_GENERATION"):
            if self._get_token_pool_bucket_target_size(project_id=seed_project_id, action=action) <= 0:
                continue
            self._register_token_pool_bucket_locked(
                bucket_key=self._build_token_pool_bucket_key(
                    project_id=seed_project_id,
                    action=action,
                    token_id=None,
                ),
                project_id=seed_project_id,
                action=action,
                token_id=None,
                now_value=now_value,
            )

    def _build_token_pool_bucket_key(
        self,
        *,
        project_id: str,
        action: str,
        token_id: Optional[int],
    ) -> str:
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        return normalized_action

    def _get_token_pool_condition_locked(self, bucket_key: str) -> asyncio.Condition:
        condition = self._token_pool_conditions.get(bucket_key)
        if condition is None:
            condition = asyncio.Condition(self._token_pool_lock)
            self._token_pool_conditions[bucket_key] = condition
        return condition

    def _register_token_pool_bucket_locked(
        self,
        *,
        bucket_key: str,
        project_id: str,
        action: str,
        token_id: Optional[int],
        now_value: float,
    ) -> None:
        self._token_pool_queues.setdefault(bucket_key, deque())
        self._get_token_pool_condition_locked(bucket_key)
        self._token_pool_bucket_meta[bucket_key] = {
            "bucket_key": bucket_key,
            "project_id": self._normalize_project_key(project_id),
            "action": str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION",
            "token_id": token_id,
            "last_requested_at": now_value,
        }

    def _prune_token_pool_bucket_locked(self, bucket_key: str, now_value: float) -> int:
        queue = self._token_pool_queues.get(bucket_key)
        if not queue:
            return 0

        expired_count = 0
        while queue and float(queue[0].expires_at or 0.0) <= now_value:
            queue.popleft()
            expired_count += 1

        if expired_count > 0:
            self._token_pool_stats["expired_count"] += expired_count

        return expired_count

    def _pop_ready_token_pool_lease_locked(
        self,
        bucket_key: str,
        *,
        now_value: float,
    ) -> Optional[TokenPoolLease]:
        self._prune_token_pool_bucket_locked(bucket_key, now_value)
        queue = self._token_pool_queues.get(bucket_key)
        if not queue:
            return None
        try:
            return queue.popleft()
        except IndexError:
            return None

    def _cleanup_token_pool_bucket_locked(self, bucket_key: str) -> None:
        if self._token_pool_queues.get(bucket_key):
            return
        if int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0) > 0:
            return
        if int(self._token_pool_waiters.get(bucket_key, 0) or 0) > 0:
            return
        self._token_pool_queues.pop(bucket_key, None)
        self._token_pool_conditions.pop(bucket_key, None)
        self._token_pool_bucket_meta.pop(bucket_key, None)
        self._token_pool_refill_inflight.pop(bucket_key, None)
        self._token_pool_waiters.pop(bucket_key, None)

    def _summarize_token_pool_bucket(
        self,
        bucket_key: str,
        *,
        now_value: Optional[float] = None,
    ) -> Dict[str, Any]:
        current_time = time.time() if now_value is None else now_value
        meta = self._token_pool_bucket_meta.get(bucket_key) or {}
        queue = self._token_pool_queues.get(bucket_key)
        waiting_requests = int(self._token_pool_waiters.get(bucket_key, 0) or 0)
        refill_inflight = int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0)
        action_name = str(meta.get("action") or bucket_key or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"

        ready_count = 0
        oldest_token_age_seconds: Optional[int] = None
        next_expire_in_seconds: Optional[int] = None
        if queue:
            for lease in list(queue):
                if float(lease.expires_at or 0.0) <= current_time:
                    continue
                ready_count += 1
                age_seconds = max(0, int(current_time - float(lease.created_at or current_time)))
                expire_in_seconds = max(0, int(float(lease.expires_at or current_time) - current_time))
                if oldest_token_age_seconds is None or age_seconds > oldest_token_age_seconds:
                    oldest_token_age_seconds = age_seconds
                if next_expire_in_seconds is None or expire_in_seconds < next_expire_in_seconds:
                    next_expire_in_seconds = expire_in_seconds

        return {
            "bucket_key": bucket_key,
            "action": action_name,
            "ready_count": ready_count,
            "waiting_requests": waiting_requests,
            "refill_inflight": refill_inflight,
            "oldest_token_age_seconds": oldest_token_age_seconds,
            "next_expire_in_seconds": next_expire_in_seconds,
        }

    def _discard_finished_token_pool_task(self, task: asyncio.Task) -> None:
        self._token_pool_fill_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            debug_logger.log_warning(f"[BrowserCaptchaPool] token 池后台补货任务异常: {exc}")

    @property
    def _resident_tabs(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for worker in self._workers:
            merged.update(getattr(worker, "_resident_tabs", {}) or {})
        return merged

    @staticmethod
    def _normalize_project_key(project_id: Optional[str]) -> str:
        return str(project_id or "").strip()

    @staticmethod
    def _normalize_token_key(token_id: Optional[int]) -> str:
        return BrowserCaptchaService._normalize_token_key(token_id)

    @staticmethod
    def _resolve_total_resident_tabs(limit: Optional[int] = None) -> int:
        raw_value = config.personal_max_resident_tabs if limit is None else limit
        return resolve_effective_personal_max_resident_tabs(raw_value)

    @staticmethod
    def _build_worker_tab_limits(total_tabs: int, worker_count: int) -> list[int]:
        normalized_worker_count = max(1, min(max(1, total_tabs), int(worker_count or 1)))
        base, remainder = divmod(max(1, int(total_tabs or 1)), normalized_worker_count)
        return [base + (1 if index < remainder else 0) for index in range(normalized_worker_count)]

    @staticmethod
    def _parse_worker_index_from_slot_id(slot_id: Optional[str]) -> Optional[int]:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id.startswith("b"):
            return None
        match = re.match(r"^b(\d+)-", normalized_slot_id)
        if not match:
            return None
        try:
            resolved_index = int(match.group(1)) - 1
        except Exception:
            return None
        return resolved_index if resolved_index >= 0 else None

    def _remember_affinity(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        worker_index: Optional[int] = None,
    ) -> None:
        resolved_worker_index = worker_index
        if resolved_worker_index is None:
            resolved_worker_index = self._parse_worker_index_from_slot_id(slot_id)
        if resolved_worker_index is None or resolved_worker_index < 0:
            return
        if resolved_worker_index >= len(self._workers):
            return

        normalized_project_key = self._normalize_project_key(project_id)
        if normalized_project_key:
            self._project_worker_affinity[normalized_project_key] = resolved_worker_index
            self._trim_affinity_cache(self._project_worker_affinity)

        normalized_token_key = self._normalize_token_key(token_id)
        if normalized_token_key:
            self._token_worker_affinity[normalized_token_key] = resolved_worker_index
            self._trim_affinity_cache(self._token_worker_affinity)

    def _trim_affinity_cache(self, cache: dict[str, int]) -> None:
        while len(cache) > self._affinity_cache_limit:
            try:
                oldest_key = next(iter(cache))
            except StopIteration:
                return
            cache.pop(oldest_key, None)

    def _cleanup_affinity_maps(self) -> None:
        valid_indexes = set(range(len(self._workers)))
        self._project_worker_affinity = {
            key: value
            for key, value in self._project_worker_affinity.items()
            if value in valid_indexes
        }
        self._token_worker_affinity = {
            key: value
            for key, value in self._token_worker_affinity.items()
            if value in valid_indexes
        }

    def _worker_has_project_mapping(
        self,
        worker: BrowserCaptchaService,
        project_id: Optional[str],
    ) -> bool:
        normalized_project_key = self._normalize_project_key(project_id)
        if not normalized_project_key:
            return False
        if normalized_project_key in (getattr(worker, "_project_resident_affinity", {}) or {}):
            return True
        for resident_info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            if str(getattr(resident_info, "project_id", "") or "").strip() == normalized_project_key:
                return True
        return False

    def _worker_has_token_mapping(
        self,
        worker: BrowserCaptchaService,
        token_id: Optional[int],
    ) -> bool:
        normalized_token_key = self._normalize_token_key(token_id)
        if not normalized_token_key:
            return False
        if normalized_token_key in (getattr(worker, "_token_resident_affinity", {}) or {}):
            return True
        for resident_info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            try:
                if int(getattr(resident_info, "token_id", 0) or 0) == int(normalized_token_key):
                    return True
            except Exception:
                continue
        return False

    def _worker_busy_score(self, worker: BrowserCaptchaService) -> int:
        busy_score = 0
        if getattr(worker, "_browser_lock", None) and worker._browser_lock.locked():
            busy_score += 1
        if getattr(worker, "_legacy_lock", None) and worker._legacy_lock.locked():
            busy_score += 1
        if getattr(worker, "_tab_build_lock", None) and worker._tab_build_lock.locked():
            busy_score += 1
        for resident_info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            try:
                if resident_info.solve_lock.locked():
                    busy_score += 1
                if int(getattr(resident_info, "pending_assignment_count", 0) or 0) > 0:
                    busy_score += 1
            except Exception:
                continue
        return busy_score

    @staticmethod
    def _worker_has_live_runtime(worker: BrowserCaptchaService) -> bool:
        browser_instance = getattr(worker, "browser", None)
        return bool(
            getattr(worker, "_initialized", False)
            and browser_instance
            and not getattr(browser_instance, "stopped", False)
            and not getattr(browser_instance, "_flow2api_runtime_disconnected", False)
        )

    @staticmethod
    def _worker_launch_cooldown_remaining_seconds(worker: BrowserCaptchaService) -> float:
        try:
            return max(0.0, float(worker._get_browser_launch_cooldown_remaining_seconds() or 0.0))
        except Exception:
            return 0.0

    def _worker_dispatch_score(
        self,
        worker_index: int,
        worker: BrowserCaptchaService,
        *,
        affinity_preferred: bool = False,
    ) -> tuple[int, int, int, int, int, int]:
        reservations = int(self._worker_dispatch_reservations.get(worker_index, 0) or 0)
        runtime_cold = 0 if self._worker_has_live_runtime(worker) else 1
        launch_cooldown_penalty = 1 if self._worker_launch_cooldown_remaining_seconds(worker) > 0.0 else 0
        resident_cold = 0 if worker.get_resident_count() > 0 else 1
        round_robin_offset = (worker_index - self._round_robin_index) % max(len(self._workers), 1)
        affinity_penalty = 0 if affinity_preferred else 1
        return (
            reservations + self._worker_busy_score(worker),
            runtime_cold,
            launch_cooldown_penalty,
            resident_cold,
            affinity_penalty,
            round_robin_offset,
        )

    def _find_worker_index_for_project(self, project_id: Optional[str]) -> Optional[int]:
        normalized_project_key = self._normalize_project_key(project_id)
        if not normalized_project_key:
            return None

        mapped_index = self._project_worker_affinity.get(normalized_project_key)
        if mapped_index is not None and 0 <= mapped_index < len(self._workers):
            return mapped_index

        for index, worker in enumerate(self._workers):
            if self._worker_has_project_mapping(worker, normalized_project_key):
                self._project_worker_affinity[normalized_project_key] = index
                self._trim_affinity_cache(self._project_worker_affinity)
                return index
        return None

    def _find_worker_index_for_token(self, token_id: Optional[int]) -> Optional[int]:
        normalized_token_key = self._normalize_token_key(token_id)
        if not normalized_token_key:
            return None

        mapped_index = self._token_worker_affinity.get(normalized_token_key)
        if mapped_index is not None and 0 <= mapped_index < len(self._workers):
            if self._worker_has_token_mapping(self._workers[mapped_index], normalized_token_key):
                return mapped_index
            self._token_worker_affinity.pop(normalized_token_key, None)

        for index, worker in enumerate(self._workers):
            if self._worker_has_token_mapping(worker, normalized_token_key):
                self._token_worker_affinity[normalized_token_key] = index
                self._trim_affinity_cache(self._token_worker_affinity)
                return index
        return None

    def _resolve_worker_candidate_indexes(
        self,
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        allow_affinity: bool = True,
    ) -> list[int]:
        worker_count = len(self._workers)
        if worker_count <= 0:
            return []

        preferred_indexes: list[int] = []
        exact_slot_index = self._parse_worker_index_from_slot_id(slot_id)
        if exact_slot_index is not None and 0 <= exact_slot_index < worker_count:
            preferred_indexes.append(exact_slot_index)

        soft_affinity_indexes = []
        if allow_affinity:
            for candidate in (
                self._find_worker_index_for_token(token_id),
                self._find_worker_index_for_project(project_id),
            ):
                if candidate is None or not (0 <= candidate < worker_count):
                    continue
                if candidate not in preferred_indexes and candidate not in soft_affinity_indexes:
                    soft_affinity_indexes.append(candidate)

        preferred_indexes.extend(soft_affinity_indexes)

        remaining_indexes = [index for index in range(worker_count) if index not in preferred_indexes]
        if remaining_indexes:
            rotation_offset = self._round_robin_index % len(remaining_indexes)
            rotated_indexes = remaining_indexes[rotation_offset:] + remaining_indexes[:rotation_offset]
            scored_indexes = sorted(
                enumerate(rotated_indexes),
                key=lambda item: (
                    self._worker_dispatch_score(
                        item[1],
                        self._workers[item[1]],
                        affinity_preferred=item[1] in soft_affinity_indexes,
                    ),
                    item[0],
                ),
            )
            preferred_indexes.extend(index for _, index in scored_indexes)

        return preferred_indexes

    async def _ensure_idle_worker_reaper(self) -> None:
        if self._closing:
            return
        if self._idle_worker_reaper_task is None or self._idle_worker_reaper_task.done():
            self._idle_worker_reaper_task = asyncio.create_task(self._idle_worker_reaper_loop())

    async def _ensure_token_pool_maintainer(self) -> None:
        if self._closing or not self._is_token_pool_enabled():
            return
        if self._token_pool_maintainer_task is None or self._token_pool_maintainer_task.done():
            self._token_pool_maintainer_task = asyncio.create_task(self._token_pool_maintainer_loop())

    async def warmup_resident_tabs(
        self,
        project_ids: Iterable[str],
        limit: Optional[int] = None,
    ) -> list[str]:
        """预热常驻标签页（分发给各 worker）"""
        if self._closing:
            return []
            
        await self._ensure_workers()
            
        all_warmed_slots = []
        project_ids_list = list(project_ids)
        if not project_ids_list:
            return []
            
        async with self._worker_dispatch_lock:
            workers = list(self._workers)
            
        if not workers:
            return []
            
        # Simple delegation: ask each worker to warmup, distributing the limit
        per_worker_limit = max(1, (limit or len(project_ids_list)) // len(workers))
        
        debug_logger.log_info(
            f"[BrowserCaptchaPool] warmup: {len(project_ids_list)} projects, "
            f"{len(workers)} worker(s), per_worker_limit={per_worker_limit}"
        )
        
        tasks = []
        for worker in workers:
            tasks.append(worker.warmup_resident_tabs(project_ids_list, limit=per_worker_limit))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] Worker {i} warmup FAILED: {type(res).__name__}: {res}"
                )
            elif isinstance(res, list):
                all_warmed_slots.extend(res)
                
        return all_warmed_slots

    async def _reclaim_pool_memory_pressure(
        self,
        *,
        reason: str,
        exclude_indexes: Optional[set[int]] = None,
    ) -> dict[str, int]:
        excluded = set(exclude_indexes or set())
        reclaimed = {
            "workers_touched": 0,
            "resident_tabs_closed": 0,
            "runtime_shutdown": 0,
            "profiles_deleted": 0,
            "recaptcha_cache_deleted": 0,
            "proxy_extensions_deleted": 0,
            "python_gc_collected": 0,
        }

        async with self._worker_dispatch_lock:
            workers = [
                (worker_index, worker)
                for worker_index, worker in enumerate(self._workers)
                if worker_index not in excluded
            ]

        for worker_index, worker in workers:
            try:
                worker_stats = await worker.reclaim_runtime_memory(
                    reason=f"{reason}:worker-{worker_index + 1}",
                    aggressive=True,
                )
            except Exception as exc:
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] worker 内存回收失败 (worker={worker_index + 1}, reason={reason}): {exc}"
                )
                continue

            reclaimed["workers_touched"] += 1
            for key in (
                "resident_tabs_closed",
                "runtime_shutdown",
                "profiles_deleted",
                "recaptcha_cache_deleted",
                "proxy_extensions_deleted",
                "python_gc_collected",
            ):
                reclaimed[key] += int(worker_stats.get(key, 0) or 0)

        if any(int(value or 0) > 0 for value in reclaimed.values()):
            debug_logger.log_warning(
                f"[BrowserCaptchaPool] 内存压力回收完成 ({reason}): {reclaimed}"
            )
        return reclaimed

    async def _token_pool_maintainer_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(1.0)
                await self._maintain_token_pool_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                debug_logger.log_warning(f"[BrowserCaptchaPool] token 池维护循环异常: {exc}")

    async def _maintain_token_pool_once(self) -> None:
        spawn_jobs: list[Dict[str, Any]] = []
        if self._closing or not self._is_token_pool_enabled():
            async with self._token_pool_lock:
                self._token_pool_queues.clear()
                self._token_pool_bucket_meta.clear()
                self._token_pool_waiters.clear()
                self._token_pool_refill_inflight.clear()
                self._token_pool_conditions.clear()
            return

        now_value = time.time()
        keepalive_seconds = self._get_token_pool_bucket_keepalive_seconds()
        refill_parallelism = self._get_token_pool_refill_parallelism()

        async with self._token_pool_lock:
            self._register_configured_token_pool_buckets_locked(now_value=now_value)
            bucket_keys = list(
                {
                    *self._token_pool_bucket_meta.keys(),
                    *self._token_pool_queues.keys(),
                    *self._token_pool_refill_inflight.keys(),
                    *self._token_pool_waiters.keys(),
                }
            )
            total_inflight = sum(max(0, int(value or 0)) for value in self._token_pool_refill_inflight.values())

            for bucket_key in bucket_keys:
                self._prune_token_pool_bucket_locked(bucket_key, now_value)
                waiting_count = int(self._token_pool_waiters.get(bucket_key, 0) or 0)
                inflight_count = int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0)
                queue = self._token_pool_queues.get(bucket_key)
                ready_count = len(queue) if queue else 0
                meta = self._token_pool_bucket_meta.get(bucket_key)
                last_requested_at = float((meta or {}).get("last_requested_at") or 0.0)

                if (
                    ready_count <= 0
                    and inflight_count <= 0
                    and waiting_count <= 0
                    and (not last_requested_at or (now_value - last_requested_at) >= keepalive_seconds)
                ):
                    self._cleanup_token_pool_bucket_locked(bucket_key)
                    continue

                if meta is None:
                    continue
                target_size = self._get_token_pool_bucket_target_size(
                    project_id=meta.get("project_id"),
                    action=meta.get("action"),
                )
                if target_size <= 0:
                    if queue:
                        self._token_pool_stats["dropped_count"] += len(queue)
                        queue.clear()
                    if inflight_count <= 0 and waiting_count <= 0:
                        self._cleanup_token_pool_bucket_locked(bucket_key)
                    continue
                if ready_count + inflight_count >= target_size:
                    continue

                while total_inflight < refill_parallelism and (ready_count + inflight_count) < target_size:
                    self._token_pool_refill_inflight[bucket_key] = inflight_count + 1
                    inflight_count += 1
                    total_inflight += 1
                    spawn_jobs.append(dict(meta))

        for job in spawn_jobs:
            if self._closing:
                break
            task = asyncio.create_task(self._token_pool_refill_once(job))
            self._token_pool_fill_tasks.add(task)
            task.add_done_callback(self._discard_finished_token_pool_task)

    async def _token_pool_refill_once(self, bucket_meta: Dict[str, Any]) -> None:
        bucket_key = str(bucket_meta.get("bucket_key") or "").strip()
        project_id = str(bucket_meta.get("project_id") or "").strip()
        action = str(bucket_meta.get("action") or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        token_id = bucket_meta.get("token_id")

        try:
            token, slot_id = await self._get_token_direct(
                project_id,
                action=action,
                token_id=token_id,
                return_slot_id=True,
                allow_affinity=False,
                remember_affinity=False,
            )
            if not token:
                return

            created_at = time.time()
            lease = TokenPoolLease(
                bucket_key=bucket_key,
                token=token,
                project_id=project_id,
                action=action,
                token_id=token_id,
                slot_id=slot_id,
                worker_index=self._parse_worker_index_from_slot_id(slot_id),
                created_at=created_at,
                expires_at=created_at + float(getattr(config, "token_pool_ttl_seconds", 120) or 120),
            )

            async with self._token_pool_lock:
                self._token_pool_last_refill_at = created_at
                self._token_pool_last_token_at = created_at
                self._token_pool_stats["produced_count"] += 1

                current_meta = self._token_pool_bucket_meta.get(bucket_key)
                if current_meta is None or not self._is_token_pool_enabled():
                    self._token_pool_stats["dropped_count"] += 1
                    return

                target_size = self._get_token_pool_bucket_target_size(
                    project_id=current_meta.get("project_id"),
                    action=current_meta.get("action"),
                )
                self._prune_token_pool_bucket_locked(bucket_key, created_at)
                queue = self._token_pool_queues.setdefault(bucket_key, deque())
                if len(queue) < target_size:
                    queue.append(lease)
                    self._get_token_pool_condition_locked(bucket_key).notify_all()
                else:
                    self._token_pool_stats["dropped_count"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_logger.log_warning(
                f"[BrowserCaptchaPool] token 池补货失败 (bucket={bucket_key or '<empty>'}): {exc}"
            )
        finally:
            async with self._token_pool_lock:
                current_value = int(self._token_pool_refill_inflight.get(bucket_key, 0) or 0)
                if current_value <= 1:
                    self._token_pool_refill_inflight.pop(bucket_key, None)
                else:
                    self._token_pool_refill_inflight[bucket_key] = current_value - 1
                condition = self._token_pool_conditions.get(bucket_key)
                if condition is not None:
                    condition.notify_all()
                self._cleanup_token_pool_bucket_locked(bucket_key)

            if not self._closing:
                await self._maintain_token_pool_once()

    async def _wait_for_token_pool_token(
        self,
        *,
        bucket_key: str,
        project_id: str,
        action: str,
        token_id: Optional[int],
    ) -> Optional[TokenPoolLease]:
        if self._closing:
            return None

        now_value = time.time()
        async with self._token_pool_lock:
            self._register_token_pool_bucket_locked(
                bucket_key=bucket_key,
                project_id=project_id,
                action=action,
                token_id=token_id,
                now_value=now_value,
            )
            lease = self._pop_ready_token_pool_lease_locked(bucket_key, now_value=now_value)
            if lease is not None:
                self._token_pool_stats["hit_count"] += 1
                self._token_pool_stats["served_count"] += 1
                return lease
            self._token_pool_stats["miss_count"] += 1
            self._token_pool_stats["wait_count"] += 1
            self._token_pool_waiters[bucket_key] = int(self._token_pool_waiters.get(bucket_key, 0) or 0) + 1

        try:
            await self._ensure_token_pool_maintainer()
            await self._maintain_token_pool_once()

            deadline = time.monotonic() + self._get_token_pool_wait_timeout_seconds()
            while True:
                async with self._token_pool_lock:
                    self._register_token_pool_bucket_locked(
                        bucket_key=bucket_key,
                        project_id=project_id,
                        action=action,
                        token_id=token_id,
                        now_value=time.time(),
                    )
                    lease = self._pop_ready_token_pool_lease_locked(
                        bucket_key,
                        now_value=time.time(),
                    )
                    if lease is not None:
                        self._token_pool_stats["served_count"] += 1
                        return lease

                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        break
                    condition = self._get_token_pool_condition_locked(bucket_key)
                    try:
                        await asyncio.wait_for(condition.wait(), timeout=remaining_seconds)
                    except asyncio.TimeoutError:
                        break

                await self._maintain_token_pool_once()

            bucket_snapshot = self._summarize_token_pool_bucket(bucket_key, now_value=time.time())
            debug_logger.log_warning(
                f"[BrowserCaptchaPool] token 池等待超时，严格池模式下不回退同步获取 "
                f"(action_bucket={bucket_snapshot['action']}, project_id={project_id or '<empty>'}, "
                f"token_id={token_id}, action={action}, ready={bucket_snapshot['ready_count']}, "
                f"waiting={bucket_snapshot['waiting_requests']}, inflight={bucket_snapshot['refill_inflight']})"
            )
            return None
        finally:
            async with self._token_pool_lock:
                current_value = int(self._token_pool_waiters.get(bucket_key, 0) or 0)
                if current_value <= 1:
                    self._token_pool_waiters.pop(bucket_key, None)
                else:
                    self._token_pool_waiters[bucket_key] = current_value - 1
                self._cleanup_token_pool_bucket_locked(bucket_key)

    async def _idle_worker_reaper_loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(30)
                try:
                    idle_ttl_seconds = max(
                        60,
                        int(getattr(config, "personal_idle_tab_ttl_seconds", 600) or 600),
                    )
                except Exception:
                    idle_ttl_seconds = 600

                async with self._worker_dispatch_lock:
                    candidates = [
                        (worker_index, worker)
                        for worker_index, worker in enumerate(self._workers)
                        if int(self._worker_dispatch_reservations.get(worker_index, 0) or 0) <= 0
                    ]

                for worker_index, worker in candidates:
                    if not self._worker_has_live_runtime(worker):
                        continue
                    try:
                        did_shutdown = await worker.shutdown_idle_runtime_if_needed(
                            idle_ttl_seconds=idle_ttl_seconds,
                            reason=f"pool_idle_runtime_ttl_{idle_ttl_seconds}s",
                        )
                    except Exception as e:
                        debug_logger.log_warning(
                            f"[BrowserCaptchaPool] 空闲浏览器实例回收失败 (worker={worker_index + 1}): {e}"
                        )
                        continue
                    if did_shutdown:
                        debug_logger.log_info(
                            f"[BrowserCaptchaPool] 已回收空闲浏览器实例运行态 (worker={worker_index + 1}, idle_ttl={idle_ttl_seconds}s)"
                        )
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptchaPool] 空闲浏览器实例回收循环异常: {e}")

    async def _acquire_worker(
        self,
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        excluded_indexes: Optional[set[int]] = None,
        ensure_workers: bool = True,
        allow_affinity: bool = True,
    ) -> tuple[int, BrowserCaptchaService]:
        if ensure_workers:
            await self._ensure_workers()
        excluded = set(excluded_indexes or set())
        acquire_started_at = time.monotonic()
        async with self._worker_dispatch_lock:
            if not self._workers:
                raise RuntimeError("没有可用的浏览器实例")
            if len(self._workers) <= 1:
                return 0, self._workers[0]

            preferred_affinity_worker_index = None
            if allow_affinity:
                for candidate in (
                    self._find_worker_index_for_token(token_id),
                    self._find_worker_index_for_project(project_id),
                ):
                    if candidate is None or candidate in excluded:
                        continue
                    if not (0 <= candidate < len(self._workers)):
                        continue
                    preferred_affinity_worker_index = candidate
                    break

            candidate_indexes = [
                worker_index
                for worker_index in self._resolve_worker_candidate_indexes(
                    project_id=project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    allow_affinity=allow_affinity,
                )
                if worker_index not in excluded
            ]
            if not candidate_indexes:
                candidate_indexes = [
                    worker_index
                    for worker_index in range(len(self._workers))
                    if worker_index not in excluded
                ] or [0]

            selectable_indexes = [
                worker_index
                for worker_index in candidate_indexes
                if int(getattr(self._workers[worker_index], "_max_resident_tabs", 0) or 0) > 0
            ] or candidate_indexes
            selected_worker_index = min(
                selectable_indexes,
                key=lambda worker_index: self._worker_dispatch_score(
                    worker_index,
                    self._workers[worker_index],
                    affinity_preferred=worker_index == preferred_affinity_worker_index,
                ),
            )
            self._worker_dispatch_reservations[selected_worker_index] = (
                int(self._worker_dispatch_reservations.get(selected_worker_index, 0) or 0) + 1
            )
            normalized_project_key = self._normalize_project_key(project_id)
            if normalized_project_key:
                self._project_worker_affinity[normalized_project_key] = selected_worker_index
                self._trim_affinity_cache(self._project_worker_affinity)
            self._round_robin_index = (selected_worker_index + 1) % max(len(self._workers), 1)
            debug_logger.log_info(
                "[BrowserCaptchaPool] worker 已选中 "
                f"(project_id={project_id or '<empty>'}, token_id={token_id}, "
                f"selected={selected_worker_index + 1}, candidates={[index + 1 for index in candidate_indexes]}, "
                f"selectable={[index + 1 for index in selectable_indexes]}, "
                f"affinity={(preferred_affinity_worker_index + 1) if preferred_affinity_worker_index is not None else None}, "
                f"reservations={self._worker_dispatch_reservations.get(selected_worker_index, 0)}, "
                f"elapsed={time.monotonic() - acquire_started_at:.3f}s)"
            )
            return selected_worker_index, self._workers[selected_worker_index]

    async def _release_worker_reservation(self, worker_index: Optional[int]) -> None:
        if worker_index is None:
            return
        async with self._worker_dispatch_lock:
            current = int(self._worker_dispatch_reservations.get(worker_index, 0) or 0)
            if current <= 1:
                self._worker_dispatch_reservations.pop(worker_index, None)
            else:
                self._worker_dispatch_reservations[worker_index] = current - 1

    def _build_project_buckets_for_workers(
        self,
        project_ids: list[str],
        *,
        worker_limits: list[int],
    ) -> list[list[str]]:
        project_buckets: list[list[str]] = [[] for _ in self._workers]
        if not project_ids or not project_buckets:
            return project_buckets

        for project_id in project_ids:
            preferred_worker_index = self._find_worker_index_for_project(project_id)
            if (
                preferred_worker_index is not None
                and 0 <= preferred_worker_index < len(project_buckets)
                and len(project_buckets[preferred_worker_index]) < worker_limits[preferred_worker_index]
            ):
                project_buckets[preferred_worker_index].append(project_id)
                continue

            candidate_indexes = [
                index
                for index, _ in enumerate(self._workers)
                if len(project_buckets[index]) < worker_limits[index]
            ]
            if not candidate_indexes:
                break

            selected_worker_index = min(
                candidate_indexes,
                key=lambda index: (
                    self._workers[index].get_resident_count() + len(project_buckets[index]),
                    self._worker_busy_score(self._workers[index]),
                    index,
                ),
            )
            project_buckets[selected_worker_index].append(project_id)

        return project_buckets

    async def _ensure_workers(self, *, reload_existing: bool = False) -> None:
        if self._closing:
            return

        extra_workers: list[BrowserCaptchaService] = []
        workers_to_reload: list[BrowserCaptchaService] = []

        async with self._reload_lock:
            async with self._worker_dispatch_lock:
                self.headless = bool(getattr(config, "personal_headless", False))
                configured_browser_count = BrowserCaptchaService._resolve_configured_browser_count()
                total_tabs = self._resolve_total_resident_tabs()
                worker_limits = self._build_worker_tab_limits(
                    total_tabs,
                    min(configured_browser_count, total_tabs),
                )

                current_worker_count = len(self._workers)
                if current_worker_count > len(worker_limits):
                    extra_workers = self._workers[len(worker_limits):]
                    self._workers = self._workers[:len(worker_limits)]

                for index, tab_limit in enumerate(worker_limits):
                    if index >= len(self._workers):
                        worker = BrowserCaptchaService(
                            self.db,
                            browser_instance_id=index + 1,
                            max_resident_tabs_override=tab_limit,
                        )
                        worker._idle_reaper_task = asyncio.create_task(worker._idle_tab_reaper_loop())
                        self._workers.append(worker)
                        continue

                    worker = self._workers[index]
                    worker.db = self.db
                    worker.apply_pool_worker_settings(
                        browser_instance_id=index + 1,
                        max_resident_tabs_override=tab_limit,
                    )
                    if reload_existing:
                        workers_to_reload.append(worker)

                self._worker_tab_limits = list(worker_limits)
                self._cleanup_affinity_maps()
                self._worker_dispatch_reservations = {
                    worker_index: count
                    for worker_index, count in self._worker_dispatch_reservations.items()
                    if 0 <= worker_index < len(self._workers) and count > 0
                }
                self._round_robin_index %= max(len(self._workers), 1)

        for worker in extra_workers:
            try:
                await worker.close()
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptchaPool] 关闭多余浏览器实例失败: {e}")

        if workers_to_reload:
            await asyncio.gather(
                *(worker.reload_config() for worker in workers_to_reload),
                return_exceptions=True,
            )
        if self._closing:
            return

        if self._workers:
            await self._ensure_idle_worker_reaper()
        if self._is_token_pool_enabled():
            await self._ensure_token_pool_maintainer()
            await self._maintain_token_pool_once()
        elif self._token_pool_maintainer_task is not None and not self._token_pool_maintainer_task.done():
            self._token_pool_maintainer_task.cancel()
            try:
                await self._token_pool_maintainer_task
            except asyncio.CancelledError:
                pass
            finally:
                self._token_pool_maintainer_task = None

    async def reload_config(self):
        if self._closing:
            return
        await self._ensure_workers(reload_existing=True)

    async def close(self):
        self._closing = True
        async with self._reload_lock:
            async with self._worker_dispatch_lock:
                idle_worker_reaper_task = self._idle_worker_reaper_task
                self._idle_worker_reaper_task = None
                token_pool_maintainer_task = self._token_pool_maintainer_task
                self._token_pool_maintainer_task = None
                token_pool_fill_tasks = list(self._token_pool_fill_tasks)
                self._token_pool_fill_tasks.clear()
                workers = list(self._workers)
                self._workers = []
                self._worker_tab_limits = []
                self._worker_dispatch_reservations.clear()
                self._project_worker_affinity.clear()
                self._token_worker_affinity.clear()
                self._last_successful_worker_index = None
                self._round_robin_index = 0
        async with self._token_pool_lock:
            self._token_pool_queues.clear()
            self._token_pool_conditions.clear()
            self._token_pool_waiters.clear()
            self._token_pool_bucket_meta.clear()
            self._token_pool_refill_inflight.clear()
        for fill_task in token_pool_fill_tasks:
            if fill_task.done():
                continue
            fill_task.cancel()
        if token_pool_fill_tasks:
            await asyncio.gather(*token_pool_fill_tasks, return_exceptions=True)
        if token_pool_maintainer_task and not token_pool_maintainer_task.done():
            token_pool_maintainer_task.cancel()
            try:
                await token_pool_maintainer_task
            except asyncio.CancelledError:
                pass
                pass
        if idle_worker_reaper_task and not idle_worker_reaper_task.done():
            idle_worker_reaper_task.cancel()
            try:
                await idle_worker_reaper_task
            except asyncio.CancelledError:
                pass
        for worker in workers:
            try:
                await worker.close()
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptchaPool] 关闭浏览器实例失败: {e}")

    async def _get_token_direct(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
        allow_affinity: bool = True,
        remember_affinity: bool = True,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        await self._ensure_workers()
        if not self._workers:
            return (None, None) if return_slot_id else None

        excluded_indexes: set[int] = set()
        max_attempts = min(len(self._workers), 3)

        for _ in range(max_attempts):
            worker_index = None
            worker = None
            try:
                worker_index, worker = await self._acquire_worker(
                    project_id=project_id,
                    token_id=token_id,
                    excluded_indexes=excluded_indexes,
                    ensure_workers=False,
                    allow_affinity=allow_affinity,
                )
                excluded_indexes.add(worker_index)
                token, slot_id = await worker.get_token(
                    project_id,
                    action=action,
                    token_id=token_id,
                    return_slot_id=True,
                )
            except Exception as e:
                worker_label = worker_index + 1 if worker_index is not None else "unknown"
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] 浏览器实例打码失败，尝试切换其他实例 (worker={worker_label}): {e}"
                )
                if BrowserCaptchaService._is_memory_pressure_browser_launch_error(e):
                    await self._reclaim_pool_memory_pressure(
                        reason=f"direct_token:{project_id or '<empty>'}",
                        exclude_indexes=excluded_indexes,
                    )
                continue
            finally:
                await self._release_worker_reservation(worker_index)

            if not token:
                continue

            self._last_successful_worker_index = worker_index
            if remember_affinity:
                self._remember_affinity(
                    project_id=project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    worker_index=worker_index,
                )
            if return_slot_id:
                return token, slot_id
            return token

        return (None, None) if return_slot_id else None

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        token, slot_id, _ = await self.get_token_with_metadata(
            project_id,
            action=action,
            token_id=token_id,
        )
        if return_slot_id:
            return token, slot_id
        return token

    async def get_token_with_metadata(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        if not self._is_token_pool_enabled():
            token, slot_id = await self._get_token_direct(
                project_id,
                action=action,
                token_id=token_id,
                return_slot_id=True,
            )
            return token, slot_id, (token_id if token else None)

        target_size = self._get_token_pool_bucket_target_size(
            project_id=project_id,
            action=action,
        )
        if target_size <= 0:
            token, slot_id = await self._get_token_direct(
                project_id,
                action=action,
                token_id=token_id,
                return_slot_id=True,
            )
            return token, slot_id, (token_id if token else None)

        bucket_key = self._build_token_pool_bucket_key(
            project_id=project_id,
            action=action,
            token_id=token_id,
        )
        lease = await self._wait_for_token_pool_token(
            bucket_key=bucket_key,
            project_id=project_id,
            action=action,
            token_id=token_id,
        )
        if lease is None:
            bucket_snapshot = self._summarize_token_pool_bucket(bucket_key, now_value=time.time())
            raise TokenPoolTimeoutError(
                "token 池等待超时且未命中可用 token "
                f"(action_bucket={bucket_snapshot['action']}, project_id={project_id or '<empty>'}, "
                f"token_id={token_id}, action={action}, ready={bucket_snapshot['ready_count']}, "
                f"waiting={bucket_snapshot['waiting_requests']}, inflight={bucket_snapshot['refill_inflight']})"
            )

        if lease.worker_index is not None:
            self._last_successful_worker_index = lease.worker_index
        self._remember_affinity(
            project_id=project_id,
            token_id=token_id if lease.token_id == token_id else None,
            slot_id=lease.slot_id,
            worker_index=lease.worker_index,
        )
        return lease.token, lease.slot_id, lease.token_id

    async def report_flow_error(
        self,
        project_id: str,
        error_reason: str,
        error_message: str = "",
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
    ):
        await self._ensure_workers()
        exact_worker_index = self._parse_worker_index_from_slot_id(slot_id)
        if exact_worker_index is not None and 0 <= exact_worker_index < len(self._workers):
            await self._workers[exact_worker_index].report_flow_error(
                project_id,
                error_reason,
                error_message=error_message,
                token_id=token_id,
                slot_id=slot_id,
            )
            return

        candidate_indexes: list[int] = []
        for worker_index in (
            self._find_worker_index_for_token(token_id),
            self._find_worker_index_for_project(project_id),
        ):
            if worker_index is None or not (0 <= worker_index < len(self._workers)):
                continue
            if worker_index not in candidate_indexes:
                candidate_indexes.append(worker_index)

        if not candidate_indexes:
            if (
                self._last_successful_worker_index is not None
                and 0 <= self._last_successful_worker_index < len(self._workers)
            ):
                candidate_indexes.append(self._last_successful_worker_index)

        if not candidate_indexes:
            resolved_candidates = self._resolve_worker_candidate_indexes(project_id=project_id, token_id=token_id)
            if resolved_candidates:
                candidate_indexes.append(resolved_candidates[0])

        for worker_index in candidate_indexes:
            await self._workers[worker_index].report_flow_error(
                project_id,
                error_reason,
                error_message=error_message,
                token_id=token_id,
                slot_id=slot_id,
            )

    async def invalidate_token(self, project_id: str):
        await self._ensure_workers()
        candidate_indexes = self._resolve_worker_candidate_indexes(project_id=project_id)
        for worker_index in candidate_indexes:
            await self._workers[worker_index].invalidate_token(project_id)

    async def open_login_window(self):
        await self._ensure_workers()
        if not self._workers:
            raise RuntimeError("没有可用的浏览器实例")
        await self._workers[0].open_login_window()

    async def refresh_session_token(self, project_id: str, token_id: Optional[int] = None) -> Optional[str]:
        await self._ensure_workers()
        excluded_indexes: set[int] = set()
        max_attempts = min(len(self._workers), 3)

        for _ in range(max_attempts):
            worker_index = None
            worker = None
            try:
                worker_index, worker = await self._acquire_worker(
                    project_id=project_id,
                    token_id=token_id,
                    excluded_indexes=excluded_indexes,
                )
                excluded_indexes.add(worker_index)
                session_token = await worker.refresh_session_token(project_id, token_id=token_id)
            except Exception as e:
                worker_label = worker_index + 1 if worker_index is not None else "unknown"
                debug_logger.log_warning(
                    f"[BrowserCaptchaPool] Session Token 刷新失败，尝试切换其他实例 (worker={worker_label}): {e}"
                )
                continue
            finally:
                await self._release_worker_reservation(worker_index)
            if session_token:
                self._last_successful_worker_index = worker_index
                self._remember_affinity(project_id=project_id, token_id=token_id, worker_index=worker_index)
                return session_token
        return None

    async def start_resident_mode(self, project_id: str):
        await self._ensure_workers()
        worker_index = None
        worker = None
        try:
            worker_index, worker = await self._acquire_worker(project_id=project_id)
            await worker.start_resident_mode(project_id)
            self._remember_affinity(project_id=project_id, worker_index=worker_index)
        finally:
            await self._release_worker_reservation(worker_index)

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        await self._ensure_workers()
        if project_id:
            exact_worker_index = self._parse_worker_index_from_slot_id(project_id)
            if exact_worker_index is not None and 0 <= exact_worker_index < len(self._workers):
                await self._workers[exact_worker_index].stop_resident_mode(project_id)
                return
            worker_index = self._find_worker_index_for_project(project_id)
            if worker_index is not None:
                await self._workers[worker_index].stop_resident_mode(project_id)
                return
            return

        await asyncio.gather(
            *(worker.stop_resident_mode(project_id=None) for worker in self._workers),
            return_exceptions=True,
        )

    def is_resident_mode_active(self) -> bool:
        return any(worker.is_resident_mode_active() for worker in self._workers)

    def get_resident_count(self) -> int:
        return sum(worker.get_resident_count() for worker in self._workers)

    def get_resident_project_ids(self) -> list[str]:
        project_ids: list[str] = []
        for worker in self._workers:
            project_ids.extend(worker.get_resident_project_ids())
        return project_ids

    def get_resident_project_id(self) -> Optional[str]:
        for worker in self._workers:
            project_id = worker.get_resident_project_id()
            if project_id:
                return project_id
        return None

    def get_token_pool_status(self) -> Dict[str, Any]:
        if not self._is_token_pool_enabled():
            return {
                "token_pool_enabled": False,
                "token_pool_status": "未启用",
                "token_pool_total_ready": 0,
                "token_pool_bucket_count": 0,
                "token_pool_waiting_requests": 0,
                "token_pool_refill_inflight": 0,
                "token_pool_last_refill_at": None,
                "token_pool_last_token_at": None,
                "token_pool_oldest_token_age_seconds": None,
                "token_pool_next_expire_in_seconds": None,
                "token_pool_bucket_details": [],
                "token_pool_hit_count": int(self._token_pool_stats.get("hit_count", 0) or 0),
                "token_pool_miss_count": int(self._token_pool_stats.get("miss_count", 0) or 0),
                "token_pool_wait_count": int(self._token_pool_stats.get("wait_count", 0) or 0),
                "token_pool_expired_count": int(self._token_pool_stats.get("expired_count", 0) or 0),
            }

        now_value = time.time()
        bucket_keys = sorted(
            {
                *self._token_pool_bucket_meta.keys(),
                *[key for key, queue in self._token_pool_queues.items() if queue],
                *[key for key, value in self._token_pool_waiters.items() if int(value or 0) > 0],
                *[key for key, value in self._token_pool_refill_inflight.items() if int(value or 0) > 0],
            }
        )
        bucket_details = [self._summarize_token_pool_bucket(bucket_key, now_value=now_value) for bucket_key in bucket_keys]
        total_ready = sum(int(detail["ready_count"] or 0) for detail in bucket_details)
        oldest_token_age_seconds: Optional[int] = None
        next_expire_in_seconds: Optional[int] = None
        for detail in bucket_details:
            age_seconds = detail.get("oldest_token_age_seconds")
            expire_in_seconds = detail.get("next_expire_in_seconds")
            if age_seconds is not None and (
                oldest_token_age_seconds is None or int(age_seconds) > oldest_token_age_seconds
            ):
                oldest_token_age_seconds = int(age_seconds)
            if expire_in_seconds is not None and (
                next_expire_in_seconds is None or int(expire_in_seconds) < next_expire_in_seconds
            ):
                next_expire_in_seconds = int(expire_in_seconds)

        waiting_requests = sum(int(detail["waiting_requests"] or 0) for detail in bucket_details)
        refill_inflight = sum(int(detail["refill_inflight"] or 0) for detail in bucket_details)
        bucket_count = len(bucket_details)

        if total_ready > 0:
            status_text = "运行中"
        elif refill_inflight > 0 and waiting_requests > 0:
            status_text = "补货中"
        elif refill_inflight > 0:
            status_text = "预热中"
        elif waiting_requests > 0:
            status_text = "等待中"
        else:
            status_text = "空闲"

        return {
            "token_pool_enabled": True,
            "token_pool_status": status_text,
            "token_pool_total_ready": total_ready,
            "token_pool_bucket_count": bucket_count,
            "token_pool_waiting_requests": waiting_requests,
            "token_pool_refill_inflight": refill_inflight,
            "token_pool_last_refill_at": self._format_status_timestamp(self._token_pool_last_refill_at),
            "token_pool_last_token_at": self._format_status_timestamp(self._token_pool_last_token_at),
            "token_pool_oldest_token_age_seconds": oldest_token_age_seconds,
            "token_pool_next_expire_in_seconds": next_expire_in_seconds,
            "token_pool_bucket_details": bucket_details,
            "token_pool_hit_count": int(self._token_pool_stats.get("hit_count", 0) or 0),
            "token_pool_miss_count": int(self._token_pool_stats.get("miss_count", 0) or 0),
            "token_pool_wait_count": int(self._token_pool_stats.get("wait_count", 0) or 0),
            "token_pool_expired_count": int(self._token_pool_stats.get("expired_count", 0) or 0),
        }

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        if self._last_successful_worker_index is not None and 0 <= self._last_successful_worker_index < len(self._workers):
            fingerprint = self._workers[self._last_successful_worker_index].get_last_fingerprint()
            if fingerprint:
                return fingerprint
        for worker in self._workers:
            fingerprint = worker.get_last_fingerprint()
            if fingerprint:
                return fingerprint
        return None

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        await self._ensure_workers()
        if not self._workers:
            return None
        worker_index = None
        worker = None
        try:
            worker_index, worker = await self._acquire_worker()
            token = await worker.get_custom_token(
                website_url=website_url,
                website_key=website_key,
                action=action,
                enterprise=enterprise,
            )
        finally:
            await self._release_worker_reservation(worker_index)
        if token:
            self._last_successful_worker_index = worker_index
        return token

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        await self._ensure_workers()
        if not self._workers:
            return {
                "token": None,
                "token_elapsed_ms": 0,
                "verify_mode": "browser_page",
                "verify_elapsed_ms": 0,
                "verify_http_status": None,
                "verify_result": {},
            }
        worker_index = None
        worker = None
        try:
            worker_index, worker = await self._acquire_worker()
            result = await worker.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise,
            )
        finally:
            await self._release_worker_reservation(worker_index)
        if result.get("token"):
            self._last_successful_worker_index = worker_index
        return result
