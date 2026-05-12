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

from ..constants import *
from ..utils import *
from ..models import *

class BrowserTabsMixin:
    async def _open_visible_browser_tab(
        self,
        url: str,
        *,
        label: str,
        timeout_seconds: Optional[float] = None,
    ):
        """有头模式下复用唯一浏览器窗口。

        规则：
        - 当前没有任何 page target 时，先新建一个窗口；
        - 一旦已有窗口，后续统一只开新标签页，避免继续弹第二个浏览器窗口。
        """
        reusable_startup_tab = await self._take_visible_startup_page()
        if reusable_startup_tab is not None:
            debug_logger.log_info(f"[BrowserCaptcha] 复用浏览器启动页打开目标标签 ({label})")
            await self._apply_tab_startup_spoofs(
                reusable_startup_tab,
                label=f"{label}:reuse_startup_page",
                target_url=url,
            )
            await self._tab_get(
                reusable_startup_tab,
                url,
                label=f"{label}:reuse_startup_page",
                timeout_seconds=timeout_seconds,
            )
            return reusable_startup_tab

        has_page_targets = await self._browser_has_page_targets()
        if not has_page_targets and self._requires_virtual_display():
            try:
                await self._ensure_browser_host_page(
                    label=f"{label}:ensure_host_window",
                    timeout_seconds=timeout_seconds,
                )
                has_page_targets = await self._browser_has_page_targets()
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建有头宿主窗口失败，将直接尝试打开目标标签页 ({label}): {e}"
                )
        return await self._create_default_context_target_tab(
            url,
            label=label,
            timeout_seconds=timeout_seconds,
            prefer_new_tab=has_page_targets,
        )

    async def _create_isolated_context_tab(
        self,
        url: str,
        *,
        label: str,
        create_timeout_seconds: Optional[float] = None,
    ) -> tuple[Any, Any]:
        """通过 CDP 手动创建独立 browser context 与 target，绕过 nodriver.create_context 的 StopIteration 缺陷。"""
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            raise RuntimeError("browser runtime unavailable")

        timeout_seconds = create_timeout_seconds or self._navigation_timeout_seconds
        target_url = str(url or "").strip() or PERSONAL_COOKIE_PREBIND_URL
        initial_url = (
            PERSONAL_COOKIE_PREBIND_URL
            if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL
            else target_url
        )
        if not self.headless:
            # 有头模式下不再为内部打码页创建独立可见 browser context 窗口。
            # 直接复用默认 context 的单一浏览器窗口，通过新标签页承载 resident/legacy 页面。
            tab = await self._open_visible_browser_tab(
                target_url,
                label=f"{label}:headed_tab",
                timeout_seconds=timeout_seconds,
            )
            try:
                tab._browser = browser
            except Exception:
                pass
            return tab, None

        from nodriver import cdp

        if not await self._browser_has_page_targets():
            try:
                await self._ensure_browser_host_page(
                    label=f"{label}:ensure_host_page",
                    timeout_seconds=timeout_seconds,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建独立 context 前补宿主页失败 ({label}): {e}"
                )

        browser_context_id = await self._run_with_timeout(
            browser.connection.send(
                cdp.target.create_browser_context(
                    dispose_on_detach=True,
                )
            ),
            timeout_seconds=timeout_seconds,
            label=f"{label}:create_browser_context",
        )
        target_id = None

        try:
            async def _send_create_target():
                return await self._run_with_timeout(
                    browser.connection.send(
                        cdp.target.create_target(
                            initial_url,
                            browser_context_id=browser_context_id,
                            new_window=True,
                        )
                    ),
                    timeout_seconds=timeout_seconds,
                    label=f"{label}:create_target",
                )

            try:
                target_id = await _send_create_target()
            except Exception as create_target_error:
                if not self._is_no_browser_window_error(create_target_error):
                    raise

                debug_logger.log_warning(
                    f"[BrowserCaptcha] create_target 命中无宿主窗口错误，补宿主页后重试 ({label}): {create_target_error}"
                )
                await self._ensure_browser_host_page(
                    label=f"{label}:recover_host_page",
                    timeout_seconds=timeout_seconds,
                )
                target_id = await _send_create_target()

            for attempt in range(20):
                try:
                    await browser.update_targets()
                except Exception:
                    pass

                tab = next(
                    (
                        item
                        for item in getattr(browser, "targets", [])
                        if getattr(item, "type_", None) == "page"
                        and getattr(item, "target_id", None) == target_id
                    ),
                    None,
                )
                if tab is not None:
                    try:
                        tab._browser = browser
                    except Exception:
                        pass
                    await self._apply_tab_startup_spoofs(
                        tab,
                        label=f"{label}:isolated_context_tab",
                        browser_context_id=browser_context_id,
                        target_url=target_url,
                    )
                    await self._apply_configured_browser_startup_cookie(
                        label=f"{label}:startup_cookie",
                        browser_context_id=browser_context_id,
                        tab=tab,
                    )
                    if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL:
                        await self._tab_get(
                            tab,
                            target_url,
                            label=f"{label}:navigate_target",
                            timeout_seconds=timeout_seconds,
                        )
                    return tab, browser_context_id

                await asyncio.sleep(0.25)

            raise RuntimeError(
                f"target not found after create_target (target_id={target_id}, context_id={browser_context_id})"
            )
        except Exception:
            if browser_context_id is not None:
                await self._dispose_browser_context_quietly(browser_context_id)
            raise

    async def _create_default_context_target_tab(
        self,
        url: str,
        *,
        label: str,
        timeout_seconds: Optional[float] = None,
        prefer_new_tab: bool = True,
    ):
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            raise RuntimeError("browser runtime unavailable")

        from nodriver import cdp

        timeout = timeout_seconds or self._navigation_timeout_seconds
        target_url = str(url or "").strip() or PERSONAL_COOKIE_PREBIND_URL
        initial_url = (
            PERSONAL_COOKIE_PREBIND_URL
            if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL
            else target_url
        )
        attempts = [False, True] if prefer_new_tab else [True, False]
        last_error = None

        for new_window in attempts:
            try:
                target_id = await self._run_with_timeout(
                    browser.connection.send(
                        cdp.target.create_target(
                            initial_url,
                            new_window=new_window,
                            enable_begin_frame_control=True,
                        )
                    ),
                    timeout_seconds=timeout,
                    label=f"{label}:create_target:{'window' if new_window else 'tab'}",
                )
            except Exception as create_error:
                last_error = create_error
                if self._is_no_browser_window_error(create_error) and not new_window:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] CDP 新标签创建提示无宿主窗口，改用新窗口重试 ({label}): {create_error}"
                    )
                    continue
                raise

            for _ in range(20):
                try:
                    await browser.update_targets()
                except Exception as update_error:
                    if self._is_browser_runtime_error(update_error):
                        raise

                tab = next(
                    (
                        item
                        for item in getattr(browser, "targets", []) or []
                        if getattr(item, "type_", None) == "page"
                        and getattr(item, "target_id", None) == target_id
                    ),
                    None,
                )
                if tab is not None:
                    try:
                        tab._browser = browser
                    except Exception:
                        pass
                    await self._apply_tab_startup_spoofs(
                        tab,
                        label=f"{label}:default_context_tab",
                        target_url=target_url,
                    )
                    if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL:
                        await self._tab_get(
                            tab,
                            target_url,
                            label=f"{label}:navigate_target",
                            timeout_seconds=timeout,
                        )
                    return tab

                await asyncio.sleep(0.15)

            last_error = RuntimeError(f"target not found after create_target (target_id={target_id})")

        raise last_error or RuntimeError("failed to create browser target")

    async def _ensure_browser_host_page(
        self,
        *,
        label: str,
        timeout_seconds: Optional[float] = None,
    ):
        """确保当前浏览器存在至少一个可复用的 page target。"""
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            raise RuntimeError("browser runtime unavailable")

        try:
            await browser.update_targets()
        except Exception:
            pass

        if self.headless:
            tracked_host_page = await self._take_headless_host_page()
            if tracked_host_page is not None:
                await self._apply_tab_startup_spoofs(tracked_host_page, label=f"{label}:tracked_host_page")
                return tracked_host_page

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) == "page":
                if self.headless and getattr(getattr(item, "target", None), "browser_context_id", None) is not None:
                    continue
                if self.headless:
                    self._headless_host_target_id = str(getattr(item, "target_id", "") or "").strip() or None
                try:
                    item._browser = browser
                except Exception:
                    pass
                await self._apply_tab_startup_spoofs(item, label=f"{label}:existing_host_page")
                return item

        debug_logger.log_info(f"[BrowserCaptcha] 当前无可用 page target，创建宿主页 ({label})")
        tab = await self._browser_get(
            PERSONAL_COOKIE_PREBIND_URL,
            label=f"{label}:host_page",
            new_tab=False,
            new_window=True,
            timeout_seconds=timeout_seconds,
        )
        try:
            tab._browser = browser
        except Exception:
            pass
        if self.headless:
            self._headless_host_target_id = str(getattr(tab, "target_id", "") or "").strip() or None
        await self._apply_tab_startup_spoofs(tab, label=f"{label}:host_page")
        return tab

    async def _take_visible_startup_page(self):
        target_id = str(self._visible_startup_target_id or "").strip()
        if not target_id:
            return None

        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            self._visible_startup_target_id = None
            return None

        try:
            await browser.update_targets()
        except Exception:
            pass

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) != "page":
                continue
            current_target_id = str(getattr(item, "target_id", "") or "").strip()
            if current_target_id != target_id:
                continue
            current_url = str(getattr(item, "url", "") or "").strip()
            if not self._is_reusable_startup_page_url(current_url):
                debug_logger.log_info(
                    f"[BrowserCaptcha] 启动页已被其他逻辑占用，放弃复用 (target={target_id}, url={current_url or '<empty>'})"
                )
                self._visible_startup_target_id = None
                return None
            self._visible_startup_target_id = None
            try:
                item._browser = browser
            except Exception:
                pass
            return item

        self._visible_startup_target_id = None
        return None

    async def _take_headless_host_page(self):
        target_id = str(self._headless_host_target_id or "").strip()
        if not target_id:
            return None

        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            self._headless_host_target_id = None
            return None

        try:
            await browser.update_targets()
        except Exception:
            pass

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) != "page":
                continue
            current_target_id = str(getattr(item, "target_id", "") or "").strip()
            if current_target_id != target_id:
                continue
            if getattr(getattr(item, "target", None), "browser_context_id", None) is not None:
                break
            try:
                item._browser = browser
            except Exception:
                pass
            return item

        self._headless_host_target_id = None
        return None

    async def _capture_visible_startup_page(self):
        """记录浏览器启动后自带的首个 page target，避免后续先关空页再开业务页。"""
        browser = self.browser
        self._visible_startup_target_id = None
        if browser is None or getattr(browser, "stopped", False):
            return None

        try:
            await browser.update_targets()
        except Exception:
            pass

        for item in getattr(browser, "targets", []):
            if getattr(item, "type_", None) != "page":
                continue
            target_id = str(getattr(item, "target_id", "") or "").strip()
            if not target_id:
                continue
            self._visible_startup_target_id = target_id
            try:
                item._browser = browser
            except Exception:
                pass
            return item
        return None

    @staticmethod
    def _is_reusable_startup_page_url(url: Optional[str]) -> bool:
        normalized_url = str(url or "").strip().lower()
        if not normalized_url:
            return True
        return normalized_url in {
            "about:blank",
            "chrome://newtab/",
            "chrome://new-tab-page/",
            "chrome://new-tab-page",
            "chrome://new-tab-page-third-party/",
        }

    async def _browser_has_page_targets(self) -> bool:
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            return False
        try:
            await browser.update_targets()
        except Exception:
            pass
        return any(
            getattr(item, "type_", None) == "page"
            for item in getattr(browser, "targets", [])
        )

    async def _tab_reload(self, tab, label: str, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            tab.reload(),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )

    async def _cleanup_startup_browser_pages(self):
        """关闭浏览器启动时自动弹出的默认页面，避免有头模式出现额外普通窗口。"""
        browser = self.browser
        if browser is None or getattr(browser, "stopped", False):
            return

        try:
            await browser.update_targets()
        except Exception:
            pass

        page_tabs = [
            item
            for item in getattr(browser, "targets", [])
            if getattr(item, "type_", None) == "page"
        ]
        if not page_tabs:
            return

        for tab in page_tabs:
            try:
                target_id = getattr(tab, "target_id", None)
                tab_url = str(getattr(tab, "url", "") or "")
                debug_logger.log_info(
                    f"[BrowserCaptcha] 清理浏览器启动残留页 "
                    f"(target={target_id}, url={tab_url or '<empty>'})"
                )
                await self._close_tab_quietly(tab)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 清理启动残留页失败: {e}")

    async def _idle_tab_reaper_loop(self):
        """空闲标签页回收循环"""
        while True:
            try:
                await asyncio.sleep(5)  # 每30秒检查一次
                current_time = time.time()
                tabs_to_close = []

                async with self._resident_lock:
                    for slot_id, resident_info in list(self._resident_tabs.items()):
                        if resident_info.solve_lock.locked():
                            continue
                        idle_seconds = current_time - resident_info.last_used_at
                        if idle_seconds >= min(5, self._idle_tab_ttl_seconds):
                            tabs_to_close.append(slot_id)
                            debug_logger.log_info(
                                f"[BrowserCaptcha] slot={slot_id} 空闲 {idle_seconds:.0f}s，准备回收"
                            )

                for slot_id in tabs_to_close:
                    await self._close_resident_tab(slot_id)
                
                # Auto-close browser completely if no resident tabs are left
                async with self._resident_lock:
                    if len(self._resident_tabs) == 0 and getattr(self, '_browser', None) is not None:
                        debug_logger.log_info('[BrowserCaptcha] Khong con tien trinh nao, tu dong dong Chrome.')
                        asyncio.create_task(self._shutdown_browser_runtime(cancel_idle_reaper=False, reason='auto_close_empty'))

            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 空闲标签页回收异常: {e}")

    async def _evict_lru_tab_if_needed(self) -> bool:
        """如果达到共享池上限，使用 LRU 策略淘汰最久未使用的空闲标签页。"""
        async with self._resident_lock:
            if len(self._resident_tabs) < self._max_resident_tabs:
                return True

            lru_slot_id = None
            lru_project_hint = None
            lru_last_used = float('inf')

            for slot_id, resident_info in self._resident_tabs.items():
                if resident_info.solve_lock.locked():
                    continue
                if resident_info.last_used_at < lru_last_used:
                    lru_last_used = resident_info.last_used_at
                    lru_slot_id = slot_id
                    lru_project_hint = resident_info.project_id

        if lru_slot_id:
            debug_logger.log_info(
                f"[BrowserCaptcha] 标签页数量达到上限({self._max_resident_tabs})，"
                f"淘汰最久未使用的 slot={lru_slot_id}, project_hint={lru_project_hint}"
            )
            await self._close_resident_tab(lru_slot_id)
            return True

        debug_logger.log_warning(
            f"[BrowserCaptcha] 标签页数量达到上限({self._max_resident_tabs})，"
            "但当前没有可安全淘汰的空闲标签页"
        )
        return False

    async def _get_reserved_tab_ids(self) -> set[int]:
        """收集当前被 resident/custom 池占用的标签页，legacy 模式不得复用。"""
        reserved_tab_ids: set[int] = set()

        async with self._resident_lock:
            for resident_info in self._resident_tabs.values():
                if resident_info and resident_info.tab:
                    reserved_tab_ids.add(id(resident_info.tab))

        async with self._custom_lock:
            for item in self._custom_tabs.values():
                tab = item.get("tab") if isinstance(item, dict) else None
                if tab:
                    reserved_tab_ids.add(id(tab))

        return reserved_tab_ids

    async def _close_tab_quietly(self, tab):
        if not tab:
            return
        try:
            await self._run_with_timeout(
                tab.close(),
                timeout_seconds=5.0,
                label="tab.close",
            )
        except Exception:
            pass
        await self._disconnect_connection_quietly(tab, reason="tab_close")

    @staticmethod
    def _extract_tab_browser_context_id(tab) -> Any:
        target = getattr(tab, "target", None)
        return getattr(target, "browser_context_id", None) if target else None

    async def _dispose_browser_context_quietly(self, browser_context_id: Any, browser_instance=None):
        target_browser = browser_instance or self.browser
        if browser_context_id is None or not target_browser:
            return
        try:
            from nodriver import cdp

            await self._run_with_timeout(
                target_browser.connection.send(
                    cdp.target.dispose_browser_context(browser_context_id)
                ),
                timeout_seconds=5.0,
                label="target.dispose_browser_context",
            )
        except Exception:
            pass

    def _next_resident_slot_id(self) -> str:
        self._resident_slot_seq += 1
        return f"{self._slot_id_prefix}slot-{self._resident_slot_seq}"

    def _forget_token_affinity_for_slot_locked(
        self,
        slot_id: Optional[str],
        preserve_token_key: Optional[str] = None,
    ):
        if not slot_id:
            return
        stale_tokens = [
            token_key
            for token_key, mapped_slot_id in self._token_resident_affinity.items()
            if mapped_slot_id == slot_id and token_key != preserve_token_key
        ]
        for token_key in stale_tokens:
            self._token_resident_affinity.pop(token_key, None)

    def _forget_project_affinity_for_slot_locked(
        self,
        slot_id: Optional[str],
        preserve_project_id: Optional[str] = None,
    ):
        if not slot_id:
            return
        stale_projects = [
            project_id
            for project_id, mapped_slot_id in self._project_resident_affinity.items()
            if mapped_slot_id == slot_id and project_id != preserve_project_id
        ]
        for project_id in stale_projects:
            self._project_resident_affinity.pop(project_id, None)

    def _resident_slot_has_pending_assignment_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None:
            return False
        return int(getattr(current, "pending_assignment_count", 0) or 0) > 0

    def _is_resident_slot_busy_for_allocation_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None:
            return False
        return current.solve_lock.locked() or self._resident_slot_has_pending_assignment_locked(
            normalized_slot_id,
            current,
        )

    def _reserve_resident_slot_for_solve_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None or not current.tab:
            return False
        if self._is_resident_slot_busy_for_allocation_locked(normalized_slot_id, current):
            return False
        current.pending_assignment_count = int(
            getattr(current, "pending_assignment_count", 0) or 0
        ) + 1
        return True

    def _release_resident_slot_reservation_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        current = resident_info or self._resident_tabs.get(normalized_slot_id)
        if current is None:
            return
        pending_count = int(getattr(current, "pending_assignment_count", 0) or 0)
        current.pending_assignment_count = max(0, pending_count - 1)

    async def _release_resident_slot_reservation(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        async with self._resident_lock:
            self._release_resident_slot_reservation_locked(
                normalized_slot_id,
                resident_info=resident_info,
            )

    async def _consume_resident_slot_reservation(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        await self._release_resident_slot_reservation(
            slot_id,
            resident_info=resident_info,
        )

    def _resolve_token_affinity_slot_locked(
        self,
        token_id: Optional[int],
        *,
        available_only: bool = False,
    ) -> Optional[str]:
        token_key = self._normalize_token_key(token_id)
        if not token_key:
            return None
        slot_id = self._token_resident_affinity.get(token_key)
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if (
                resident_info
                and resident_info.tab
                and slot_id not in self._resident_unavailable_slots
                and resident_info.token_id == int(token_key)
            ):
                if available_only and self._is_resident_slot_busy_for_allocation_locked(
                    slot_id,
                    resident_info,
                ):
                    return None
                return slot_id
            if slot_id not in self._resident_tabs or (
                resident_info is not None and resident_info.token_id != int(token_key)
            ):
                self._token_resident_affinity.pop(token_key, None)
        return None

    def _resolve_affinity_slot_locked(
        self,
        project_id: Optional[str],
        *,
        available_only: bool = False,
    ) -> Optional[str]:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return None
        slot_id = self._project_resident_affinity.get(normalized_project_id)
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if (
                resident_info
                and resident_info.tab
                and slot_id not in self._resident_unavailable_slots
                and resident_info.project_id == normalized_project_id
            ):
                if available_only and self._is_resident_slot_busy_for_allocation_locked(
                    slot_id,
                    resident_info,
                ):
                    return None
                return slot_id
            if slot_id not in self._resident_tabs or (
                resident_info is not None and resident_info.project_id != normalized_project_id
            ):
                self._project_resident_affinity.pop(normalized_project_id, None)
        return None

    def _remember_project_affinity(self, project_id: Optional[str], slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id or not slot_id or resident_info is None:
            return
        self._forget_project_affinity_for_slot_locked(slot_id, preserve_project_id=normalized_project_id)
        self._project_resident_affinity[normalized_project_id] = slot_id
        resident_info.project_id = normalized_project_id

    def _remember_token_affinity(self, token_id: Optional[int], slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
        token_key = self._normalize_token_key(token_id)
        if not token_key or not slot_id or resident_info is None:
            return
        self._forget_token_affinity_for_slot_locked(slot_id, preserve_token_key=token_key)
        self._token_resident_affinity[token_key] = slot_id
        resident_info.token_id = int(token_key)

    def _mark_resident_slot_unavailable_locked(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        self._resident_unavailable_slots.add(normalized_slot_id)
        current = self._resident_tabs.get(normalized_slot_id)
        if current is not None:
            current.recaptcha_ready = False
        elif resident_info is not None:
            resident_info.recaptcha_ready = False

    def _clear_resident_slot_unavailable_locked(self, slot_id: Optional[str]) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        self._resident_unavailable_slots.discard(normalized_slot_id)

    async def _mark_resident_slot_unavailable(
        self,
        slot_id: Optional[str],
        resident_info: Optional[ResidentTabInfo] = None,
        *,
        reason: str,
    ) -> None:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return
        async with self._resident_lock:
            self._mark_resident_slot_unavailable_locked(normalized_slot_id, resident_info=resident_info)
        debug_logger.log_warning(
            f"[BrowserCaptcha] slot={normalized_slot_id} 已标记为不可复用，等待恢复或重建 (reason={reason})"
        )

    async def _wait_for_active_resident_rebuild(
        self,
        slot_id: Optional[str] = None,
        *,
        timeout_seconds: float = 20.0,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        async with self._resident_lock:
            target_task = None
            if normalized_slot_id:
                candidate = self._resident_rebuild_tasks.get(normalized_slot_id)
                if candidate and not candidate.done():
                    target_task = candidate
            if target_task is None:
                for candidate in self._resident_rebuild_tasks.values():
                    if candidate and not candidate.done():
                        target_task = candidate
                        break

        if target_task is None:
            return False

        try:
            await self._run_with_timeout(
                asyncio.shield(target_task),
                timeout_seconds=timeout_seconds,
                label=f"wait_resident_rebuild:{normalized_slot_id or 'any'}",
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 等待共享标签页重建完成失败 (slot={normalized_slot_id or 'any'}): {e}"
            )
            return False

    def _resolve_resident_slot_for_project_locked(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        available_only: bool = False,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        """优先走 token 级映射，其次 project 级映射；没有映射时退化到共享池全局挑选。"""
        slot_id = self._resolve_token_affinity_slot_locked(
            token_id,
            available_only=available_only,
        )
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if resident_info and resident_info.tab:
                return slot_id, resident_info
        slot_id = self._resolve_affinity_slot_locked(
            project_id,
            available_only=available_only,
        )
        if slot_id:
            resident_info = self._resident_tabs.get(slot_id)
            if resident_info and resident_info.tab:
                return slot_id, resident_info
        return self._select_resident_slot_locked(
            project_id,
            token_id=token_id,
            available_only=available_only,
        )

    def _resolve_specific_resident_slot_locked(
        self,
        slot_id: Optional[str],
        *,
        reserve_for_solve: bool = False,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return None, None
        resident_info = self._resident_tabs.get(normalized_slot_id)
        if (
            resident_info is None
            or not resident_info.tab
            or normalized_slot_id in self._resident_unavailable_slots
        ):
            return None, None
        if reserve_for_solve and not self._reserve_resident_slot_for_solve_locked(
            normalized_slot_id,
            resident_info,
        ):
            return None, None
        return normalized_slot_id, resident_info

    async def _wait_for_resident_slot_available(
        self,
        slot_id: Optional[str],
        *,
        timeout_seconds: float = 0.8,
        poll_interval_seconds: float = 0.05,
    ) -> bool:
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return False

        deadline = time.monotonic() + max(0.05, float(timeout_seconds or 0.0))
        poll_interval = max(0.02, float(poll_interval_seconds or 0.0))
        while time.monotonic() < deadline:
            async with self._resident_lock:
                resident_info = self._resident_tabs.get(normalized_slot_id)
                if (
                    resident_info
                    and resident_info.tab
                    and normalized_slot_id not in self._resident_unavailable_slots
                    and not self._is_resident_slot_busy_for_allocation_locked(
                        normalized_slot_id,
                        resident_info,
                    )
                ):
                    return True
            await asyncio.sleep(poll_interval)
        return False

    def _select_resident_slot_locked(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        available_only: bool = False,
    ) -> tuple[Optional[str], Optional[ResidentTabInfo]]:
        candidates = [
            (slot_id, resident_info)
            for slot_id, resident_info in self._resident_tabs.items()
            if resident_info and resident_info.tab and slot_id not in self._resident_unavailable_slots
        ]
        if not candidates:
            return None, None

        normalized_token_key = self._normalize_token_key(token_id)
        if normalized_token_key:
            token_candidates = [
                (slot_id, resident_info)
                for slot_id, resident_info in candidates
                if resident_info.token_id == int(normalized_token_key)
            ]
            if available_only:
                token_candidates = [
                    (slot_id, resident_info)
                    for slot_id, resident_info in token_candidates
                    if not self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
                ]
            if token_candidates:
                token_candidates.sort(
                    key=lambda item: (
                        item[1].last_used_at,
                        item[1].use_count,
                        item[1].created_at,
                        item[0],
                    )
                )
                pick_index = self._resident_pick_index % len(token_candidates)
                self._resident_pick_index = (self._resident_pick_index + 1) % max(len(candidates), 1)
                return token_candidates[pick_index]

        # 共享打码池不再按 project_id 绑定；这里只根据“是否就绪 / 是否空闲 / 使用历史”
        # 做全局选择，避免 4 token/4 project 时把请求硬绑定到固定 tab。
        ready_idle = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if resident_info.recaptcha_ready
            and not self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
        ]
        ready_busy = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if resident_info.recaptcha_ready
            and self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
        ]
        cold_idle = [
            (slot_id, resident_info)
            for slot_id, resident_info in candidates
            if not resident_info.recaptcha_ready
            and not self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info)
        ]

        if available_only:
            pool = ready_idle or cold_idle
        else:
            pool = ready_idle or ready_busy or cold_idle or candidates
        if not pool:
            return None, None
        pool.sort(key=lambda item: (item[1].last_used_at, item[1].use_count, item[1].created_at, item[0]))

        pick_index = self._resident_pick_index % len(pool)
        self._resident_pick_index = (self._resident_pick_index + 1) % max(len(candidates), 1)
        return pool[pick_index]

    async def _ensure_resident_tab(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        force_create: bool = False,
        reserve_for_solve: bool = False,
        return_slot_key: bool = False,
    ):
        """确保共享打码标签页池中有可用 tab。

        逻辑：
        - 优先复用空闲 tab
        - 如果所有 tab 都忙且未到上限，继续扩容
        - 到达上限后允许请求排队等待已有 tab
        """
        def wrap(slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            return wrap_with_state(slot_id, resident_info, already_reserved=False)

        def wrap_with_state(
            slot_id: Optional[str],
            resident_info: Optional[ResidentTabInfo],
            *,
            already_reserved: bool = False,
        ):
            if reserve_for_solve and slot_id and resident_info:
                if not already_reserved and not self._reserve_resident_slot_for_solve_locked(slot_id, resident_info):
                    slot_id = None
                    resident_info = None
            if return_slot_key:
                return slot_id, resident_info
            return resident_info

        preferred_wait_slot_id: Optional[str] = None

        async with self._resident_lock:
            slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                project_id,
                token_id=token_id,
                available_only=reserve_for_solve,
            )
            at_capacity = len(self._resident_tabs) >= self._max_resident_tabs
            if reserve_for_solve and not force_create and at_capacity and (resident_info is None or not slot_id):
                preferred_wait_slot_id = self._resolve_token_affinity_slot_locked(
                    token_id,
                    available_only=False,
                ) or self._resolve_affinity_slot_locked(
                    project_id,
                    available_only=False,
                )
            available_infos = [
                (candidate_slot_id, info)
                for candidate_slot_id, info in self._resident_tabs.items()
                if candidate_slot_id not in self._resident_unavailable_slots
            ]
            if available_infos:
                all_busy = all(
                    self._is_resident_slot_busy_for_allocation_locked(candidate_slot_id, info)
                    for candidate_slot_id, info in available_infos
                )
            else:
                all_busy = True
            token_key = self._normalize_token_key(token_id)
            token_slot_matched = bool(
                token_key and resident_info and resident_info.token_id == int(token_key)
            )

            should_create = (
                force_create
                or not resident_info
                or (token_key and not token_slot_matched)
                or (all_busy and len(self._resident_tabs) < self._max_resident_tabs)
            )
            if not should_create:
                return wrap(slot_id, resident_info)

            if at_capacity:
                if not token_key:
                    return wrap(slot_id, resident_info)

        if preferred_wait_slot_id:
            waited = await self._wait_for_resident_slot_available(
                preferred_wait_slot_id,
                timeout_seconds=0.8,
                poll_interval_seconds=0.05,
            )
            if waited:
                async with self._resident_lock:
                    slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                        project_id,
                        token_id=token_id,
                        available_only=reserve_for_solve,
                    )
                    if slot_id and resident_info:
                        debug_logger.log_info(
                            "[BrowserCaptcha] affinity slot 短等待命中，跳过扩容新 tab "
                            f"(project_id={project_id or '<empty>'}, token_id={token_id}, slot={slot_id})"
                        )
                        return wrap(slot_id, resident_info)

        if self._normalize_token_key(token_id):
            await self._evict_lru_tab_if_needed()

        deferred_wait_slot_id: Optional[str] = None
        created_slot_id: Optional[str] = None
        created_resident_info: Optional[ResidentTabInfo] = None
        async with self._tab_build_lock:
            async with self._resident_lock:
                slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                    project_id,
                    token_id=token_id,
                    available_only=reserve_for_solve,
                )
                available_infos = [
                    (candidate_slot_id, info)
                    for candidate_slot_id, info in self._resident_tabs.items()
                    if candidate_slot_id not in self._resident_unavailable_slots
                ]
                if available_infos:
                    all_busy = all(
                        self._is_resident_slot_busy_for_allocation_locked(candidate_slot_id, info)
                        for candidate_slot_id, info in available_infos
                    )
                else:
                    all_busy = True
                token_key = self._normalize_token_key(token_id)
                token_slot_matched = bool(
                    token_key and resident_info and resident_info.token_id == int(token_key)
                )

                should_create = (
                    force_create
                    or not resident_info
                    or (token_key and not token_slot_matched)
                    or (all_busy and len(self._resident_tabs) < self._max_resident_tabs)
                )
                if not should_create:
                    return wrap(slot_id, resident_info)

                if len(self._resident_tabs) >= self._max_resident_tabs:
                    return wrap(slot_id, resident_info)

                created_slot_id = self._next_resident_slot_id()

            if created_slot_id is not None:
                created_resident_info = await self._create_resident_tab(
                    created_slot_id,
                    project_id=project_id,
                    token_id=token_id,
                )

            if created_slot_id is not None and created_resident_info is None:
                async with self._resident_lock:
                    slot_id, fallback_info = self._resolve_resident_slot_for_project_locked(
                        project_id,
                        token_id=token_id,
                        available_only=reserve_for_solve,
                    )
                return wrap(slot_id, fallback_info)

            if created_slot_id is not None and created_resident_info is not None:
                async with self._resident_lock:
                    self._resident_tabs[created_slot_id] = created_resident_info
                    self._clear_resident_slot_unavailable_locked(created_slot_id)
                    self._remember_token_affinity(token_id, created_slot_id, created_resident_info)
                    self._remember_project_affinity(project_id, created_slot_id, created_resident_info)
                    self._sync_compat_resident_state()
                    return wrap(created_slot_id, created_resident_info)

        if deferred_wait_slot_id:
            waited = await self._wait_for_resident_slot_available(
                deferred_wait_slot_id,
                timeout_seconds=8.0,
                poll_interval_seconds=0.05,
            )
            if waited:
                async with self._resident_lock:
                    slot_id, resident_info = self._resolve_specific_resident_slot_locked(
                        deferred_wait_slot_id,
                        reserve_for_solve=reserve_for_solve,
                    )
                    if slot_id and resident_info:
                        debug_logger.log_info(
                            "[BrowserCaptcha] 热 slot 长等待命中，避免新增 resident tab "
                            f"(project_id={project_id or '<empty>'}, token_id={token_id}, slot={slot_id})"
                        )
                        return wrap_with_state(slot_id, resident_info, already_reserved=True)

            async with self._tab_build_lock:
                async with self._resident_lock:
                    slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                        project_id,
                        token_id=token_id,
                        available_only=reserve_for_solve,
                    )
                    if slot_id and resident_info:
                        return wrap(slot_id, resident_info)
                    new_slot_id = self._next_resident_slot_id()

                resident_info = await self._create_resident_tab(new_slot_id, project_id=project_id, token_id=token_id)
                if resident_info is None:
                    async with self._resident_lock:
                        slot_id, fallback_info = self._resolve_resident_slot_for_project_locked(
                            project_id,
                            token_id=token_id,
                            available_only=reserve_for_solve,
                        )
                    return wrap(slot_id, fallback_info)

                async with self._resident_lock:
                    self._resident_tabs[new_slot_id] = resident_info
                    self._clear_resident_slot_unavailable_locked(new_slot_id)
                    self._remember_token_affinity(token_id, new_slot_id, resident_info)
                    self._remember_project_affinity(project_id, new_slot_id, resident_info)
                    self._sync_compat_resident_state()
                    return wrap(new_slot_id, resident_info)

    async def _rebuild_resident_tab(
        self,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        *,
        slot_id: Optional[str] = None,
        reserve_for_solve: bool = False,
        return_slot_key: bool = False,
    ):
        """重建共享池中的一个标签页。优先重建当前项目最近使用的 slot。"""
        def wrap(actual_slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            if return_slot_key:
                return actual_slot_id, resident_info
            return resident_info

        async def finalize(actual_slot_id: Optional[str], resident_info: Optional[ResidentTabInfo]):
            resolved_slot_id = actual_slot_id
            resolved_resident = resident_info
            if reserve_for_solve and resolved_slot_id and resolved_resident:
                async with self._resident_lock:
                    current_resident = self._resident_tabs.get(resolved_slot_id)
                    if current_resident and current_resident.tab:
                        resolved_resident = current_resident
                    if not self._reserve_resident_slot_for_solve_locked(
                        resolved_slot_id,
                        resolved_resident,
                    ):
                        resolved_slot_id = None
                        resolved_resident = None
            return wrap(resolved_slot_id, resolved_resident)
        pending_task = None
        async with self._resident_lock:
            actual_slot_id = slot_id
            if actual_slot_id is None:
                actual_slot_id, _ = self._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)
            if actual_slot_id:
                existing_task = self._resident_rebuild_tasks.get(actual_slot_id)
                if existing_task and not existing_task.done():
                    pending_task = existing_task
                else:
                    self._mark_resident_slot_unavailable_locked(actual_slot_id)

        if pending_task is not None:
            debug_logger.log_info(
                f"[BrowserCaptcha] slot={actual_slot_id} 已有重建任务，等待复用其结果"
            )
            result = await asyncio.shield(pending_task)
            return await finalize(*result)

        async def _runner(resolved_slot_id: Optional[str]):
            async with self._tab_build_lock:
                async with self._resident_lock:
                    old_resident = self._resident_tabs.pop(resolved_slot_id, None) if resolved_slot_id else None
                    self._forget_token_affinity_for_slot_locked(resolved_slot_id)
                    self._forget_project_affinity_for_slot_locked(resolved_slot_id)
                    if resolved_slot_id:
                        self._resident_error_streaks.pop(resolved_slot_id, None)
                    self._sync_compat_resident_state()

                if old_resident:
                    try:
                        await self._dispose_browser_context_quietly(old_resident.browser_context_id)
                        async with old_resident.solve_lock:
                            await self._close_tab_quietly(old_resident.tab)
                    except Exception:
                        await self._close_tab_quietly(old_resident.tab)

                next_slot_id = resolved_slot_id or self._next_resident_slot_id()
                resident_info = await self._create_resident_tab(next_slot_id, project_id=project_id, token_id=token_id)
                if resident_info is None:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] slot={next_slot_id}, project_id={project_id}, token_id={token_id} 重建共享标签页失败"
                    )
                    return next_slot_id, None

                async with self._resident_lock:
                    self._resident_tabs[next_slot_id] = resident_info
                    self._clear_resident_slot_unavailable_locked(next_slot_id)
                    self._remember_token_affinity(token_id, next_slot_id, resident_info)
                    self._remember_project_affinity(project_id, next_slot_id, resident_info)
                    self._sync_compat_resident_state()
                    return next_slot_id, resident_info

        if actual_slot_id:
            async with self._resident_lock:
                existing_task = self._resident_rebuild_tasks.get(actual_slot_id)
                if existing_task and not existing_task.done():
                    rebuild_task = existing_task
                    created_task = False
                else:
                    rebuild_task = asyncio.create_task(_runner(actual_slot_id))
                    self._resident_rebuild_tasks[actual_slot_id] = rebuild_task
                    created_task = True
            if created_task:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 开始重建共享标签页 (slot={actual_slot_id}, project={project_id}, token_id={token_id})"
                )
            else:
                debug_logger.log_info(
                    f"[BrowserCaptcha] slot={actual_slot_id} 已在重建中，等待复用现有结果"
                )
            try:
                result = await asyncio.shield(rebuild_task)
                if created_task:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 共享标签页重建结束 (slot={actual_slot_id}, project={project_id}, token_id={token_id})"
                    )
            finally:
                if created_task:
                    async with self._resident_lock:
                        if self._resident_rebuild_tasks.get(actual_slot_id) is rebuild_task:
                            self._resident_rebuild_tasks.pop(actual_slot_id, None)
            return await finalize(*result)

        result = await _runner(actual_slot_id)
        return await finalize(*result)

    async def _run_resident_recovery_task(
        self,
        slot_id: str,
        task_factory,
        *,
        project_id: str,
        error_reason: str,
    ):
        """同一 slot 的上游异常恢复任务去重，避免并发重复清缓存/重建。"""
        normalized_slot_id = str(slot_id or "").strip()
        if not normalized_slot_id:
            return await task_factory()

        async with self._resident_lock:
            existing_task = self._resident_recovery_tasks.get(normalized_slot_id)
            if existing_task and not existing_task.done():
                recovery_task = existing_task
                created_task = False
            else:
                recovery_task = asyncio.create_task(task_factory())
                self._resident_recovery_tasks[normalized_slot_id] = recovery_task
                created_task = True

        if not created_task:
            debug_logger.log_info(
                f"[BrowserCaptcha] project_id={project_id}, slot={normalized_slot_id} "
                f"检测到并发恢复任务，等待复用已有恢复结果: {error_reason}"
            )

        try:
            return await asyncio.shield(recovery_task)
        finally:
            if created_task:
                async with self._resident_lock:
                    if self._resident_recovery_tasks.get(normalized_slot_id) is recovery_task:
                        self._resident_recovery_tasks.pop(normalized_slot_id, None)

    def _sync_compat_resident_state(self):
        """同步旧版单 resident 兼容属性。"""
        first_resident = next(iter(self._resident_tabs.values()), None)
        if first_resident:
            self.resident_project_id = first_resident.project_id
            self.resident_tab = first_resident.tab
            self._running = True
            self._recaptcha_ready = bool(first_resident.recaptcha_ready)
        else:
            self.resident_project_id = None
            self.resident_tab = None
            self._running = False
            self._recaptcha_ready = False

    async def _clear_resident_storage_and_reload(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
        *,
        clear_browser_cache: bool = False,
        refresh_local_assets: bool = False,
    ) -> bool:
        """清理常驻标签页的站点数据并刷新，尝试原地自愈。"""
        async with self._resident_lock:
            resolved_slot_id = str(slot_id or "").strip()
            if resolved_slot_id:
                resident_info = self._resident_tabs.get(resolved_slot_id)
            else:
                resolved_slot_id, resident_info = self._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)

        if not resident_info or not resident_info.tab:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id or 'unknown'} 没有可清理的共享标签页"
            )
            return False

        try:
            async with resident_info.solve_lock:
                if clear_browser_cache:
                    await self._clear_browser_cache()
                if refresh_local_assets:
                    self._reset_local_recaptcha_asset_caches(purge_disk=True)
                cleanup_summary = await self._clear_tab_site_storage(resident_info.tab)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 已清理站点存储，准备刷新恢复: {cleanup_summary}"
                )

                resident_info.recaptcha_ready = False
                await self._tab_reload(
                    resident_info.tab,
                    label=f"clear_resident_reload:{resolved_slot_id or project_id}",
                )

                if not await self._wait_for_document_ready(resident_info.tab, retries=30, interval_seconds=1.0):
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理后页面加载超时"
                    )
                    return False

                resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
                if resident_info.recaptcha_ready:
                    resident_info.last_used_at = time.time()
                    async with self._resident_lock:
                        self._clear_resident_slot_unavailable_locked(resolved_slot_id)
                    self._remember_project_affinity(project_id, resolved_slot_id, resident_info)
                    self._remember_token_affinity(token_id, resolved_slot_id, resident_info)
                    self._resident_error_streaks.pop(resolved_slot_id, None)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理后已恢复 reCAPTCHA"
                    )
                    return True

                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理后仍无法恢复 reCAPTCHA"
                )
                return False
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 清理或刷新失败: {e}"
            )
            return False

    async def _recreate_resident_tab(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
    ) -> bool:
        """关闭并重建常驻标签页。"""
        resolved_slot_id, resident_info = await self._rebuild_resident_tab(
            project_id,
            token_id=token_id,
            slot_id=slot_id,
            return_slot_key=True,
        )
        if resident_info is None:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={slot_id or 'unknown'} 重建共享标签页失败"
            )
            return False
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id} 已重建共享标签页 slot={resolved_slot_id}"
        )
        return True

    async def _create_resident_tab(
        self,
        slot_id: str,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Optional[ResidentTabInfo]:
        """创建一个共享常驻打码标签页

        Args:
            slot_id: 共享标签页槽位 ID
            project_id: 触发创建的项目 ID，仅用于日志和最近映射

        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        tab = None
        browser_context_id = None
        try:
            debug_logger.log_info(
                f"[BrowserCaptcha] 创建共享常驻标签页 slot={slot_id}, seed_project={project_id}, token_id={token_id}"
            )

            # 获取或创建标签页
            browser = self.browser
            if browser is None or getattr(browser, "stopped", False):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建共享常驻标签页前浏览器不可用 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                return None

            debug_logger.log_info(f"[BrowserCaptcha] 创建独立 browser context")
            tab, browser_context_id = await self._create_isolated_context_tab(
                PERSONAL_COOKIE_PREBIND_URL,
                label=f"resident_browser_create_context:{slot_id}",
                create_timeout_seconds=self._navigation_timeout_seconds,
            )
            browser_context_id = browser_context_id or self._extract_tab_browser_context_id(tab)

            # 等待页面加载完成（减少等待时间）
            page_loaded = False
            for retry in range(10):  # 减少到10次，最多5秒
                try:
                    await asyncio.sleep(0.5)
                    ready_state = await self._tab_evaluate(
                        tab,
                        "document.readyState",
                        label=f"resident_document_ready:{slot_id}",
                        timeout_seconds=2.0,
                    )
                    if ready_state == "complete":
                        page_loaded = True
                        debug_logger.log_info(f"[BrowserCaptcha] 页面已加载")
                        break
                except Exception as e:
                    if self._is_browser_runtime_error(e):
                        self._mark_browser_health(False)
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 等待页面时浏览器运行态断开 (slot={slot_id}, project={project_id}, token_id={token_id}): {e}"
                        )
                        raise
                    debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/10...")
                    await asyncio.sleep(0.3)  # 减少重试间隔

            if not page_loaded:
                debug_logger.log_error(
                    f"[BrowserCaptcha] 页面加载超时 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
                return None

            resident_info = ResidentTabInfo(
                tab,
                slot_id,
                project_id=project_id,
                token_id=token_id,
                browser_context_id=browser_context_id,
            )

            await self._apply_token_cookie_binding(
                resident_info,
                token_id,
                label=f"resident_init:{slot_id}",
                force=True,
            )

            if not await self._open_labs_bootstrap_page(tab, label=f"resident_init:{slot_id}"):
                debug_logger.log_error(
                    f"[BrowserCaptcha] 打开 labs 引导页失败 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
                return None

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error(
                    f"[BrowserCaptcha] reCAPTCHA 加载失败 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
                return None

            resident_info.recaptcha_ready = True
            resident_info.fingerprint = await self._refresh_last_fingerprint(tab)
            self._mark_browser_health(True)

            debug_logger.log_info(
                f"[BrowserCaptcha] ✅ 共享常驻标签页创建成功 (slot={slot_id}, project={project_id}, token_id={token_id})"
            )
            return resident_info

        except asyncio.CancelledError:
            if tab is not None:
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
            raise
        except Exception as e:
            if tab is not None:
                await self._dispose_browser_context_quietly(browser_context_id)
                await self._close_tab_quietly(tab)
            if self._is_browser_runtime_error(e):
                self._mark_browser_health(False)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 创建共享常驻标签页时浏览器运行态断开 (slot={slot_id}, project={project_id}, token_id={token_id}): {e}"
                )
                raise
            debug_logger.log_error(
                f"[BrowserCaptcha] 创建共享常驻标签页异常 (slot={slot_id}, project={project_id}, token_id={token_id}): {e}"
            )
            return None

    async def _close_resident_tab(self, slot_id: str):
        """关闭指定 slot 的共享常驻标签页

        Args:
            slot_id: 共享标签页槽位 ID
        """
        async with self._resident_lock:
            resident_info = self._resident_tabs.pop(slot_id, None)
            self._forget_token_affinity_for_slot_locked(slot_id)
            self._forget_project_affinity_for_slot_locked(slot_id)
            self._resident_error_streaks.pop(slot_id, None)
            self._clear_resident_slot_unavailable_locked(slot_id)
            self._resident_rebuild_tasks.pop(slot_id, None)
            self._resident_recovery_tasks.pop(slot_id, None)
            self._sync_compat_resident_state()

        if resident_info and resident_info.tab:
            try:
                await self._dispose_browser_context_quietly(resident_info.browser_context_id)
                await self._close_tab_quietly(resident_info.tab)
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭共享常驻标签页 slot={slot_id}")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 关闭标签页时异常: {e}")

    async def start_resident_mode(self, project_id: str):
        """启动常驻模式（初始化浏览器，get_token 会自动创建标签页）"""
        if not str(project_id or "").strip():
            debug_logger.log_warning("[BrowserCaptcha] 启动常驻模式失败：project_id 为空")
            return
        self._mark_runtime_active()
        await self.initialize()
        debug_logger.log_info(f"[BrowserCaptcha] 浏览器已就绪 (project: {project_id})")

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        """停止常驻模式
        
        Args:
            project_id: 指定 project_id 或 slot_id；如果为 None 则关闭所有常驻标签页
        """
        target_slot_id = None
        if project_id:
            async with self._resident_lock:
                target_slot_id = project_id if project_id in self._resident_tabs else self._resolve_affinity_slot_locked(project_id)

        if target_slot_id:
            await self._close_resident_tab(target_slot_id)
            self._resident_error_streaks.pop(target_slot_id, None)
            debug_logger.log_info(f"[BrowserCaptcha] 已关闭共享标签页 slot={target_slot_id} (request={project_id})")
            return

        async with self._resident_lock:
            slot_ids = list(self._resident_tabs.keys())
            resident_items = list(self._resident_tabs.values())
            self._resident_tabs.clear()
            self._project_resident_affinity.clear()
            self._token_resident_affinity.clear()
            self._resident_error_streaks.clear()
            self._resident_unavailable_slots.clear()
            self._resident_rebuild_tasks.clear()
            self._resident_recovery_tasks.clear()
            self._sync_compat_resident_state()

        for resident_info in resident_items:
            if resident_info and resident_info.tab:
                await self._dispose_browser_context_quietly(resident_info.browser_context_id)
                await self._close_tab_quietly(resident_info.tab)
        debug_logger.log_info(f"[BrowserCaptcha] 已关闭所有共享常驻标签页 (共 {len(slot_ids)} 个)")

    async def warmup_resident_tabs(
        self,
        project_ids: Optional[list[str]] = None,
        limit: int = 1,
    ) -> list[Optional[str]]:
        """启动时预热共享常驻标签页。

        对每个 project_id 调用 _ensure_resident_tab 创建标签页，
        达到 limit 数量后停止。返回已预热的 slot_id 列表。
        """
        if not project_ids:
            return []
        warmed: list[Optional[str]] = []
        for pid in project_ids:
            if len(warmed) >= limit:
                break
            try:
                _slot_id, _info = await self._ensure_resident_tab(
                    project_id=pid,
                    force_create=False,
                    return_slot_key=True,
                )
                if _slot_id:
                    warmed.append(_slot_id)
            except Exception:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] warmup_resident_tabs 预热 project={pid} 失败",
                )
        return warmed

    def is_resident_mode_active(self) -> bool:
        """检查是否有任何常驻标签页激活"""
        return len(self._resident_tabs) > 0 or self._running

    def get_resident_count(self) -> int:
        """获取当前常驻标签页数量"""
        return len(self._resident_tabs)

    def get_resident_project_ids(self) -> list[str]:
        """获取所有当前共享常驻标签页的 slot_id 列表。"""
        return list(self._resident_tabs.keys())

    def get_resident_project_id(self) -> Optional[str]:
        """获取当前共享池中的第一个 slot_id（向后兼容）。"""
        if self._resident_tabs:
            return next(iter(self._resident_tabs.keys()))
        return self.resident_project_id

    async def _collect_reclaimable_resident_slot_ids(self) -> list[str]:
        current_time = time.time()
        async with self._resident_lock:
            reclaimable_slot_ids = []
            for slot_id, resident_info in list(self._resident_tabs.items()):
                if resident_info is None:
                    continue
                if resident_info.solve_lock.locked():
                    continue
                if int(getattr(resident_info, "pending_assignment_count", 0) or 0) > 0:
                    continue
                reclaimable_slot_ids.append(
                    (
                        current_time - float(getattr(resident_info, "last_used_at", current_time) or current_time),
                        slot_id,
                    )
                )

        reclaimable_slot_ids.sort(reverse=True)
        return [slot_id for _, slot_id in reclaimable_slot_ids]

    @staticmethod
    def _normalize_token_key(token_id: Optional[int]) -> str:
        try:
            normalized = int(token_id or 0)
        except Exception:
            normalized = 0
        return str(normalized) if normalized > 0 else ""

