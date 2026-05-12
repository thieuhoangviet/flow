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

class BrowserLifecycleMixin:
    async def initialize(self):
        """初始化 nodriver 浏览器"""
        self._check_available()

        if (
            self._initialized
            and self.browser
            and not self.browser.stopped
            and self._is_browser_health_fresh()
        ):
            self._mark_runtime_active()
            if self._idle_reaper_task is None or self._idle_reaper_task.done():
                self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
            return

        self._raise_if_browser_launch_cooling_down()

        async with self._browser_lock:
            self._raise_if_browser_launch_cooling_down()
            browser_needs_restart = False
            browser_executable_path = None
            display_value = os.environ.get("DISPLAY", "").strip()
            browser_args = []
            sandbox_enabled = _resolve_personal_browser_sandbox_enabled()

            if self._initialized and self.browser:
                try:
                    if self.browser.stopped:
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器已停止，准备重新初始化...")
                        self._mark_browser_health(False)
                        browser_needs_restart = True
                    elif getattr(self.browser, "_flow2api_runtime_disconnected", False):
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器连接已标记断开，准备重新初始化...")
                        self._mark_browser_health(False)
                        browser_needs_restart = True
                    elif self._is_browser_health_fresh():
                        self._mark_runtime_active()
                        if self._idle_reaper_task is None or self._idle_reaper_task.done():
                            self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                        return
                    elif not await self._probe_browser_runtime():
                        debug_logger.log_warning("[BrowserCaptcha] 浏览器连接已失活，准备重新初始化...")
                        browser_needs_restart = True
                    else:
                        _patch_nodriver_runtime(self.browser)
                        self._mark_runtime_active()
                        if self._idle_reaper_task is None or self._idle_reaper_task.done():
                            self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                        return
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 浏览器状态检查异常，准备重新初始化: {e}")
                    browser_needs_restart = True
            elif self.browser is not None or self._initialized:
                browser_needs_restart = True

            if browser_needs_restart:
                await self._shutdown_browser_runtime_locked(reason="initialize_recovery")

            launch_gate = self._get_global_browser_launch_gate()
            if launch_gate.locked():
                debug_logger.log_info(
                    "[BrowserCaptcha] 浏览器启动排队中，等待全局启动配额以降低 Windows 启动尖峰内存"
                )

            async with launch_gate:
                try:
                    if self.user_data_dir:
                        debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (用户数据目录: {self.user_data_dir})...")
                        os.makedirs(self.user_data_dir, exist_ok=True)
                    else:
                        debug_logger.log_info(
                            "[BrowserCaptcha] 正在启动 nodriver 浏览器 "
                            "(使用独立临时目录，隔离真实资料)..."
                        )

                    browser_executable_path, browser_source = _resolve_browser_executable_path()
                    if browser_executable_path and browser_source == "configured":
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 使用显式配置的浏览器作为 nodriver 浏览器: {browser_executable_path}"
                        )
                    if browser_executable_path:
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 使用指定浏览器可执行文件: {browser_executable_path}"
                        )

                    # 解析代理配置
                    self._cleanup_proxy_extension()
                    self._proxy_url = None
                    protocol, host, port, username, password = await self._resolve_personal_proxy()
                    self._proxy_config_signature = await self._build_proxy_config_signature()
                    proxy_server_arg = None
                    if protocol and host and port:
                        if username and password:
                            self._proxy_ext_dir = _create_proxy_auth_extension(protocol, host, port, username, password)
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Personal 代理需要认证，已创建扩展: {self._proxy_ext_dir}"
                            )
                            debug_logger.log_info(
                                "[BrowserCaptcha] Personal 认证代理改由扩展接管，跳过命令行 --proxy-server，避免浏览器原生认证弹窗"
                            )
                        else:
                            proxy_server_arg = f"--proxy-server={protocol}://{host}:{port}"
                        self._proxy_url = f"{protocol}://{host}:{port}"
                        debug_logger.log_info(f"[BrowserCaptcha] Personal 浏览器代理: {self._proxy_url}")

                    browser_args = _build_personal_browser_args(
                        headless=self.headless,
                        proxy_server_arg=proxy_server_arg,
                        proxy_extension_dir=self._proxy_ext_dir,
                    )
                    if self._requires_virtual_display():
                        browser_args = _tune_personal_browser_args_for_docker_headed(browser_args)
                        debug_logger.log_info(
                            "[BrowserCaptcha] Docker headed 指纹优化已启用，已收敛明显的容器化启动参数"
                        )
                    if self._requires_virtual_display() and '--no-startup-window' in browser_args:
                        browser_args = [
                            arg for arg in browser_args
                            if arg != '--no-startup-window'
                        ]
                        debug_logger.log_info(
                            "[BrowserCaptcha] Docker 有头虚拟显示模式已禁用 --no-startup-window，保留宿主窗口"
                        )
                    browser_args = _normalize_personal_browser_args_for_launch(
                        browser_args,
                        sandbox_enabled=sandbox_enabled,
                    )

                    effective_launch_args = list(browser_args)
                    if self._requires_virtual_display():
                        await self._wait_for_display_ready(display_value)

                    effective_uid = "n/a"
                    if hasattr(os, "geteuid"):
                        try:
                            effective_uid = str(os.geteuid())
                        except Exception:
                            effective_uid = "unknown"

                    launch_kwargs = {
                        "headless": False,  # Luôn headed — stealth hidden dùng offscreen+minimize thay vì headless
                        "user_data_dir": self.user_data_dir,
                        "browser_executable_path": browser_executable_path,
                        "browser_args": browser_args,
                        "sandbox": sandbox_enabled,
                    }
                    launch_config = uc.Config(**launch_kwargs)
                    effective_launch_args = launch_config()
                    debug_logger.log_info(
                        "[BrowserCaptcha] nodriver 启动上下文: "
                        f"docker={IS_DOCKER}, display={display_value or '<empty>'}, "
                        f"uid={effective_uid}, headless={self.headless}, sandbox={sandbox_enabled}, "
                        f"executable={browser_executable_path or '<auto>'}, "
                        f"args={' '.join(effective_launch_args)}"
                    )

                    # 启动 nodriver 浏览器（后台启动，不占用前台）
                    launch_plan: list[tuple[str, Dict[str, Any], Optional[str]]] = [
                        ("nodriver.start", dict(launch_kwargs), None),
                    ]
                    tried_no_sandbox_retry = False
                    tried_fresh_profile_retry = False
                    last_start_error: Optional[Exception] = None
                    self.browser = None

                    while launch_plan:
                        launch_label, current_launch_kwargs, retry_reason = launch_plan.pop(0)
                        current_config = uc.Config(**current_launch_kwargs)
                        effective_launch_args = current_config()
                        if retry_reason:
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 浏览器启动重试 ({retry_reason}): "
                                f"label={launch_label}, profile={current_launch_kwargs.get('user_data_dir') or '<isolated-temp>'}"
                            )
                        try:
                            self.browser = await self._run_with_timeout(
                                uc.start(**current_launch_kwargs),
                                timeout_seconds=30.0,
                                label=launch_label,
                            )
                            self._browser_process_pid = self._get_browser_process_pid(self.browser)
                            break
                        except Exception as start_error:
                            last_start_error = start_error
                            failed_profile_dir = str(current_launch_kwargs.get("user_data_dir") or "").strip()
                            if failed_profile_dir and self._is_runtime_managed_profile_dir(failed_profile_dir):
                                self._managed_runtime_profile_dirs.add(os.path.normpath(failed_profile_dir))
                                self._terminate_browser_processes_for_profile_dirs(
                                    [failed_profile_dir],
                                    reason=f"{launch_label}:failed_start",
                                )

                            if (
                                not tried_no_sandbox_retry
                                and self._should_use_explicit_no_sandbox_retry(start_error)
                            ):
                                tried_no_sandbox_retry = True
                                fallback_browser_args = list(current_launch_kwargs.get("browser_args") or [])
                                if '--no-sandbox' not in fallback_browser_args:
                                    fallback_browser_args.append('--no-sandbox')
                                fallback_kwargs = dict(current_launch_kwargs)
                                fallback_kwargs["browser_args"] = fallback_browser_args
                                fallback_kwargs["sandbox"] = True
                                launch_plan.insert(
                                    0,
                                    (
                                        "nodriver.start.retry_no_sandbox",
                                        fallback_kwargs,
                                        f"explicit_no_sandbox after {type(start_error).__name__}: {start_error}",
                                    ),
                                )

                            if (
                                not tried_fresh_profile_retry
                                and self._is_retryable_browser_launch_error(start_error)
                            ):
                                tried_fresh_profile_retry = True
                                previous_profile_dir = str(current_launch_kwargs.get("user_data_dir") or self.user_data_dir or "").strip()
                                fresh_profile_dir = self._create_fresh_runtime_profile_dir(prefix="launch_retry_profile_")
                                fresh_profile_kwargs = dict(current_launch_kwargs)
                                fresh_profile_kwargs["user_data_dir"] = fresh_profile_dir
                                launch_plan.insert(
                                    0,
                                    (
                                        "nodriver.start.retry_fresh_profile",
                                        fresh_profile_kwargs,
                                        f"fresh_profile after {type(start_error).__name__}: {start_error} "
                                        f"(previous_profile={previous_profile_dir or '<empty>'})",
                                    ),
                                )

                            if launch_plan:
                                await asyncio.sleep(0.8)
                                continue
                            raise

                    if self.browser is None and last_start_error is not None:
                        raise last_start_error

                    _patch_nodriver_runtime(self.browser)
                    live_user_agent, live_product = await self._get_live_browser_runtime_identity()
                    self._refresh_runtime_fingerprint_spoof_seed(
                        user_agent=live_user_agent,
                        product=live_product,
                    )
                    await self._apply_configured_browser_startup_cookie(
                        label="initialize",
                    )
                    if self.headless:
                        try:
                            startup_warmup_tab = await self._ensure_browser_host_page(
                                label="initialize_headless_startup_warmup",
                                timeout_seconds=self._navigation_timeout_seconds,
                            )
                        except Exception as e:
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 创建无头启动预热页失败，跳过启动人类化预热: {e}"
                            )
                        else:
                            await self._simulate_startup_human_warmup(
                                startup_warmup_tab,
                                label="initialize_headless_startup_warmup",
                                duration_seconds=1.0,
                            )
                    if self._proxy_ext_dir:
                        debug_logger.log_info("[BrowserCaptcha] 等待代理认证扩展完成初始化...")
                        await asyncio.sleep(1.5)
                    # Stealth hidden: minimize cửa sổ khi chạy ẩn
                    if self.headless:
                        await self._stealth_minimize_browser_window()
                    if not self.headless:
                        if self._requires_virtual_display():
                            await self._ensure_browser_host_page(
                                label="initialize_host_page",
                                timeout_seconds=self._navigation_timeout_seconds,
                            )
                        else:
                            await self._capture_visible_startup_page()
                    self._initialized = True
                    self._mark_browser_health(True)
                    self._mark_runtime_active()
                    self._reset_browser_launch_failure_state()
                    if self._idle_reaper_task is None or self._idle_reaper_task.done():
                        self._idle_reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
                    profile_label = self.user_data_dir or "<isolated-temp>"
                    debug_logger.log_info(
                        f"[BrowserCaptcha] ✅ nodriver 浏览器已启动 (Profile: {profile_label})"
                    )

                except Exception as e:
                    self.browser = None
                    self._initialized = False
                    self._mark_browser_health(False)
                    if self._is_memory_pressure_browser_launch_error(e):
                        await self.reclaim_runtime_memory(
                            reason="initialize_memory_pressure",
                            aggressive=True,
                        )
                    self._mark_browser_launch_failure(e)
                    debug_logger.log_error(
                        "[BrowserCaptcha] ❌ 浏览器启动失败: "
                        f"{type(e).__name__}: {str(e)} | "
                        f"display={display_value or '<empty>'} | "
                        f"executable={browser_executable_path or '<auto>'} | "
                        f"args={' '.join(effective_launch_args) if effective_launch_args else '<none>'} | "
                        f"cooldown={self._get_browser_launch_cooldown_remaining_seconds():.1f}s"
                    )
                    raise

    async def close(self):
        """关闭浏览器"""
        await self._shutdown_browser_runtime(cancel_idle_reaper=True, reason="service_close")

    async def _shutdown_browser_runtime(self, cancel_idle_reaper: bool = False, reason: str = "shutdown"):
        if cancel_idle_reaper and self._idle_reaper_task and not self._idle_reaper_task.done():
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except asyncio.CancelledError:
                pass
            finally:
                self._idle_reaper_task = None

        async with self._browser_lock:
            try:
                await self._shutdown_browser_runtime_locked(reason=reason)
                debug_logger.log_info(f"[BrowserCaptcha] 浏览器运行态已清理 ({reason})")
            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 清理浏览器运行态异常 ({reason}): {str(e)}")

    async def _shutdown_browser_runtime_locked(self, reason: str):
        """在持有 _browser_lock 的前提下，彻底清理当前浏览器运行态。"""
        browser_instance = self.browser
        self.browser = None
        self._initialized = False
        self._visible_startup_target_id = None
        self._headless_host_target_id = None
        self._refresh_runtime_fingerprint_spoof_seed()
        self._last_fingerprint = None
        self._last_fingerprint_at = 0.0
        self._mark_browser_health(False)
        self._reset_browser_rotation_budget()
        self._reset_local_recaptcha_asset_caches(purge_disk=False)
        self._cleanup_proxy_extension()
        self._proxy_url = None
        await self._cancel_background_runtime_tasks(reason=reason)

        async with self._resident_lock:
            resident_items = list(self._resident_tabs.values())
            self._resident_tabs.clear()
            self._project_resident_affinity.clear()
            self._token_resident_affinity.clear()
            self._resident_error_streaks.clear()
            self._resident_unavailable_slots.clear()
            self._resident_rebuild_tasks.clear()
            self._resident_recovery_tasks.clear()
            self._sync_compat_resident_state()

        custom_items = list(self._custom_tabs.values())
        self._custom_tabs.clear()

        closed_tabs = set()

        async def close_once(tab):
            if not tab:
                return
            tab_key = id(tab)
            if tab_key in closed_tabs:
                return
            closed_tabs.add(tab_key)
            await self._close_tab_quietly(tab)

        for resident_info in resident_items:
            await self._dispose_browser_context_quietly(
                getattr(resident_info, "browser_context_id", None),
                browser_instance=browser_instance,
            )
            await close_once(resident_info.tab)

        for item in custom_items:
            tab = item.get("tab") if isinstance(item, dict) else None
            await close_once(tab)

        if browser_instance:
            for target in list(getattr(browser_instance, "targets", []) or []):
                await self._disconnect_connection_quietly(
                    target,
                    reason=f"{reason}:target_disconnect",
                )
            try:
                await self._stop_browser_process(browser_instance, reason=reason)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 停止浏览器实例失败 ({reason}): {e}"
                )
        await self._cleanup_runtime_profile_dirs_after_shutdown(reason=reason)

    async def _cancel_background_runtime_tasks(self, *, reason: str) -> None:
        current_task = asyncio.current_task()
        tasks_to_cancel: list[asyncio.Task] = []

        async with self._resident_lock:
            candidate_tasks = []
            resident_warmup_task = getattr(self, "_resident_warmup_task", None)
            if resident_warmup_task is not None:
                candidate_tasks.append(resident_warmup_task)
            fresh_restart_task = getattr(self, "_fresh_profile_restart_task", None)
            if fresh_restart_task is not None:
                candidate_tasks.append(fresh_restart_task)
            candidate_tasks.extend(self._resident_rebuild_tasks.values())
            candidate_tasks.extend(self._resident_recovery_tasks.values())

            for task in candidate_tasks:
                if task is None or task.done() or task is current_task:
                    continue
                tasks_to_cancel.append(task)

            self._resident_rebuild_tasks.clear()
            self._resident_recovery_tasks.clear()
            self._resident_warmup_task = None
            if fresh_restart_task is not current_task:
                self._fresh_profile_restart_task = None

        if not tasks_to_cancel:
            return

        for task in tasks_to_cancel:
            task.cancel()

        for task in tasks_to_cancel:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 取消后台任务异常 ({reason}): {type(e).__name__}: {e}"
                )

    async def _stop_browser_process(self, browser_instance, reason: str = "browser_stop"):
        """兼容 nodriver 同步 stop API，安全停止浏览器进程。"""
        if not browser_instance:
            return

        process = getattr(browser_instance, "_process", None)
        browser_pid = self._get_browser_process_pid(browser_instance) or self._browser_process_pid
        profile_dirs = self._collect_runtime_profile_process_targets()
        connection = getattr(browser_instance, "connection", None)
        await self._disconnect_browser_connection_quietly(browser_instance, reason=reason)

        if connection is not None:
            async def _noop_disconnect(_self):
                return None

            try:
                connection.disconnect = types.MethodType(_noop_disconnect, connection)
            except Exception:
                pass
            try:
                connection._listener_task = None
            except Exception:
                pass

        stop_method = getattr(browser_instance, "stop", None)
        if stop_method is not None:
            try:
                result = stop_method()
                if inspect.isawaitable(result):
                    await self._run_with_timeout(
                        result,
                        timeout_seconds=10.0,
                        label="browser.stop",
                    )
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] browser.stop 异常 ({reason}): {e}")

        if process is not None:
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                close_method = getattr(stream, "close", None)
                if callable(close_method):
                    try:
                        close_method()
                    except Exception:
                        pass
            try:
                await self._run_with_timeout(
                    process.wait(),
                    timeout_seconds=5.0,
                    label=f"browser.process.wait:{reason}",
                )
            except Exception:
                pass
        if browser_pid and self._is_pid_running(browser_pid):
            self._terminate_pid_tree(browser_pid, reason=reason)
        self._terminate_browser_processes_for_profile_dirs(profile_dirs, reason=reason)
        self._browser_process_pid = None
        await asyncio.sleep(0.3)

    def _terminate_browser_processes_for_profile_dirs(self, profile_dirs: Iterable[str], *, reason: str) -> int:
        pids = self._find_browser_pids_for_profile_dirs(profile_dirs)
        killed_count = 0
        for pid in pids:
            if self._terminate_pid_tree(pid, reason=reason):
                killed_count += 1
        if killed_count > 0:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 已按 profile 路径兜底回收浏览器进程 ({reason}): {killed_count}/{len(pids)}"
            )
        return killed_count

    def _find_browser_pids_for_profile_dirs(self, profile_dirs: Iterable[str]) -> list[int]:
        normalized_profile_dirs = [
            os.path.normcase(os.path.normpath(str(item or "").strip()))
            for item in profile_dirs
            if str(item or "").strip()
        ]
        if not normalized_profile_dirs:
            return []

        found_pids: set[int] = set()
        browser_names = {"chrome.exe", "chromium.exe", "msedge.exe", "chrome", "chromium", "msedge"}

        if sys.platform.startswith("win"):
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            "Get-CimInstance Win32_Process | "
                            "Where-Object { $_.Name -match '^(chrome|chromium|msedge)\\.exe$' } | "
                            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                output = (result.stdout or "").strip()
                if not output:
                    return []
                payload = json.loads(output)
                if isinstance(payload, dict):
                    payload = [payload]
                for item in payload if isinstance(payload, list) else []:
                    try:
                        pid = int(item.get("ProcessId") or 0)
                    except Exception:
                        continue
                    command_line = os.path.normcase(
                        os.path.normpath(str(item.get("CommandLine") or ""))
                    )
                    if pid > 0 and any(profile_dir in command_line for profile_dir in normalized_profile_dirs):
                        found_pids.add(pid)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 扫描浏览器残留进程失败: {e}")
            return sorted(found_pids)

        proc_dir = Path("/proc")
        if not proc_dir.exists():
            return []
        for child in proc_dir.iterdir():
            if not child.name.isdigit():
                continue
            try:
                pid = int(child.name)
                comm = (child / "comm").read_text(encoding="utf-8", errors="ignore").strip()
                if comm not in browser_names:
                    continue
                command_line = (child / "cmdline").read_bytes().decode(
                    "utf-8",
                    errors="ignore",
                ).replace("\x00", " ")
                normalized_command_line = os.path.normcase(os.path.normpath(command_line))
                if any(profile_dir in normalized_command_line for profile_dir in normalized_profile_dirs):
                    found_pids.add(pid)
            except Exception:
                continue
        return sorted(found_pids)

    def _collect_runtime_profile_process_targets(self) -> list[str]:
        profile_dirs: list[str] = []
        seen: set[str] = set()
        candidates = [
            self.user_data_dir,
            self._runtime_ephemeral_user_data_dir,
            *list(getattr(self, "_managed_runtime_profile_dirs", set()) or set()),
        ]

        for raw_path in candidates:
            normalized_path = str(raw_path or "").strip()
            if not normalized_path or not self._is_runtime_managed_profile_dir(normalized_path):
                continue
            try:
                resolved = os.path.normcase(os.path.normpath(str(Path(normalized_path).resolve())))
            except Exception:
                resolved = os.path.normcase(os.path.normpath(normalized_path))
            if resolved in seen:
                continue
            seen.add(resolved)
            profile_dirs.append(resolved)

        return profile_dirs

    def _terminate_pid_tree(self, pid: Optional[int], *, reason: str) -> bool:
        if not pid:
            return False
        try:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 浏览器进程仍未退出，强制回收进程树 PID={pid} ({reason})"
            )
            if sys.platform.startswith("win"):
                result = subprocess.run(
                    ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                return result.returncode == 0 or not self._is_pid_running(pid)

            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                return True
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not self._is_pid_running(pid):
                    return True
                time.sleep(0.1)
            os.kill(int(pid), signal.SIGKILL)
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 强制回收浏览器进程失败 PID={pid} ({reason}): {e}"
            )
            return False

    def _is_pid_running(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            if sys.platform.startswith("win"):
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                return str(int(pid)) in (result.stdout or "")
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    def _get_browser_process_pid(self, browser_instance) -> Optional[int]:
        if not browser_instance:
            return None
        return self._get_process_pid(getattr(browser_instance, "_process", None))

    @staticmethod
    def _get_process_pid(process: Any) -> Optional[int]:
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except Exception:
            pid = 0
        return pid if pid > 0 else None

    async def _disconnect_browser_connection_quietly(self, browser_instance, reason: str):
        """尽量先关闭 DevTools websocket，减少 nodriver 后台任务在浏览器退场时炸栈。"""
        if not browser_instance:
            return

        await self._disconnect_connection_quietly(
            getattr(browser_instance, "connection", None),
            reason=reason,
        )

    async def _disconnect_connection_quietly(self, connection, *, reason: str):
        """尽量关闭任意 nodriver 连接对象，回收 listener task 与未完成 transaction。"""
        disconnect_method = getattr(connection, "disconnect", None) if connection else None
        if disconnect_method is None:
            return

        listener_task = getattr(connection, "_listener_task", None)
        try:
            result = disconnect_method()
            if inspect.isawaitable(result):
                await self._run_with_timeout(
                    result,
                    timeout_seconds=5.0,
                    label=f"browser.disconnect:{reason}",
                )
        except Exception as e:
            if self._is_browser_runtime_error(e):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 浏览器连接关闭时检测到已断连状态 ({reason}): {e}"
                )
            else:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 浏览器连接关闭异常 ({reason}): {type(e).__name__}: {e}"
                )
        finally:
            mapper = getattr(connection, "mapper", None)
            if isinstance(mapper, dict):
                for transaction in list(mapper.values()):
                    try:
                        if not transaction.done():
                            transaction.cancel()
                    except Exception:
                        pass
                mapper.clear()

            handlers = getattr(connection, "handlers", None)
            clear_handlers = getattr(handlers, "clear", None)
            if callable(clear_handlers):
                try:
                    clear_handlers()
                except Exception:
                    pass

            if isinstance(listener_task, asyncio.Task) and listener_task is not asyncio.current_task():
                if not listener_task.done():
                    try:
                        await self._run_with_timeout(
                            asyncio.shield(listener_task),
                            timeout_seconds=1.0,
                            label=f"browser.listener_drain:{reason}",
                        )
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        listener_task.cancel()
                        try:
                            await listener_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass

                if listener_task.done():
                    try:
                        listener_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        if not (
                            isinstance(e, asyncio.InvalidStateError)
                            or self._is_browser_runtime_error(e)
                        ):
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 浏览器监听任务收尾异常 ({reason}): "
                                f"{type(e).__name__}: {e}"
                            )

            try:
                connection._listener_task = None
            except Exception:
                pass

            await asyncio.sleep(0)

    async def _probe_browser_runtime(self) -> bool:
        """轻量探测当前 nodriver 连接是否仍可用。"""
        if not self.browser:
            self._invalidate_browser_health()
            return False
        if getattr(self.browser, "_flow2api_runtime_disconnected", False):
            self._invalidate_browser_health()
            return False
        if self._is_browser_health_fresh():
            return True

        try:
            from nodriver import cdp

            await self._run_with_timeout(
                self.browser.connection.send(cdp.browser.get_version()),
                timeout_seconds=3.0,
                label="browser.health_probe",
            )
            if self._requires_virtual_display() and not await self._browser_has_page_targets():
                await self._ensure_browser_host_page(
                    label="browser_health_probe",
                    timeout_seconds=3.0,
                )
            self._mark_browser_health(True)
            return True
        except Exception as e:
            self._mark_browser_health(False)
            debug_logger.log_warning(f"[BrowserCaptcha] 浏览器健康检查失败: {e}")
            return False

    async def _recover_browser_runtime(self, project_id: Optional[str] = None, reason: str = "runtime_error") -> bool:
        """浏览器运行态损坏时，优先整颗浏览器重启并恢复 resident 池。"""
        normalized_project_id = str(project_id or "").strip()
        async with self._runtime_recover_lock:
            if self.browser and self._initialized and not getattr(self.browser, "stopped", False):
                try:
                    if await self._probe_browser_runtime():
                        debug_logger.log_info(
                            f"[BrowserCaptcha] 浏览器运行态已被并发协程恢复，直接复用 (project_id={normalized_project_id or '<empty>'}, reason={reason})"
                        )
                        return True
                except Exception:
                    pass

            self._invalidate_browser_health()

            if normalized_project_id:
                try:
                    if await self._restart_browser_for_project_unlocked(normalized_project_id):
                        self._mark_runtime_restart()
                        return True
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 浏览器重启恢复失败 (project_id={normalized_project_id}, reason={reason}): {e}"
                    )

            try:
                await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=f"recover:{reason}")
                await self.initialize()
                self._mark_runtime_restart()
                return True
            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 浏览器运行态恢复失败 ({reason}): {e}")
                return False

    async def _wait_for_browser_work_to_drain(self, *, source: str) -> None:
        warned = False
        while await self._has_active_browser_work():
            if not warned:
                warned = True
                debug_logger.log_warning(
                    f"[BrowserCaptcha] fresh profile 轮换等待当前浏览器任务 drain 完成 (source={source})"
                )
            await asyncio.sleep(0.2)

    async def _maybe_execute_pending_fresh_profile_restart(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        source: str,
    ) -> bool:
        if not self._fresh_profile_restart_pending:
            return False

        threshold = max(0, int(self._fresh_profile_restart_every_n_solves or 0))
        force_restart = bool(getattr(self, "_fresh_profile_restart_force_pending", False))
        if threshold <= 0 and not force_restart:
            self._fresh_profile_restart_pending = False
            self._fresh_profile_restart_force_pending = False
            self._fresh_profile_restart_pending_reason = ""
            return False

        existing_task = getattr(self, "_fresh_profile_restart_task", None)
        if existing_task is not None and not existing_task.done():
            return False

        async def _runner() -> bool:
            try:
                await self._wait_for_browser_work_to_drain(source=source)

                async with self._runtime_recover_lock:
                    if not self._fresh_profile_restart_pending:
                        return False
                    if await self._has_active_browser_work():
                        debug_logger.log_info(
                            "[BrowserCaptcha] fresh profile 后台轮换发现新任务活跃，延后到下一轮 "
                            f"(project_id={project_id}, source={source})"
                        )
                        return False

                    debug_logger.log_warning(
                        "[BrowserCaptcha] 执行计划中的 fresh profile 轮换重启 "
                        f"(project_id={project_id}, source={source}, reason={self._fresh_profile_restart_pending_reason})"
                    )
                    restarted = await self._restart_browser_for_project_unlocked(
                        project_id,
                        token_id=token_id,
                        fresh_profile=True,
                    )
                    if restarted:
                        self._mark_runtime_restart()
                    return restarted
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] fresh profile 后台轮换失败 (project_id={project_id}, source={source}): {e}"
                )
                return False
            finally:
                if self._fresh_profile_restart_task is asyncio.current_task():
                    self._fresh_profile_restart_task = None

        self._fresh_profile_restart_task = asyncio.create_task(_runner())
        debug_logger.log_info(
            f"[BrowserCaptcha] fresh profile 轮换已计划执行 (project_id={project_id}, source={source})"
        )
        return False

    async def _wait_for_pending_fresh_profile_restart_before_solve(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        source: str,
    ) -> bool:
        """达到 fresh 轮换阈值后，阻止新取码继续复用旧 resident tab。"""
        waited = False
        current_task = asyncio.current_task()

        while True:
            existing_task = getattr(self, "_fresh_profile_restart_task", None)
            if existing_task is not None and not existing_task.done() and existing_task is not current_task:
                if not waited:
                    waited = True
                    debug_logger.log_warning(
                        "[BrowserCaptcha] fresh profile 轮换正在执行/等待，"
                        f"当前取码先等待重启完成再分配标签页 (project_id={project_id}, source={source})"
                    )
                try:
                    await asyncio.shield(existing_task)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 等待 fresh profile 轮换任务异常 (project_id={project_id}, source={source}): {e}"
                    )
                if not self._fresh_profile_restart_pending:
                    return True
                await asyncio.sleep(0)
                continue

            if not self._fresh_profile_restart_pending:
                return waited

            threshold = max(0, int(self._fresh_profile_restart_every_n_solves or 0))
            force_restart = bool(getattr(self, "_fresh_profile_restart_force_pending", False))
            if threshold <= 0 and not force_restart:
                self._fresh_profile_restart_pending = False
                self._fresh_profile_restart_force_pending = False
                self._fresh_profile_restart_pending_reason = ""
                return waited

            if not waited:
                waited = True
                debug_logger.log_warning(
                    "[BrowserCaptcha] fresh profile 轮换已到阈值，"
                    f"当前取码先触发并等待重启完成 (project_id={project_id}, source={source}, "
                    f"reason={self._fresh_profile_restart_pending_reason})"
                )

            await self._maybe_execute_pending_fresh_profile_restart(
                project_id,
                token_id=token_id,
                source=source,
            )

            scheduled_task = getattr(self, "_fresh_profile_restart_task", None)
            if scheduled_task is None or scheduled_task.done() or scheduled_task is current_task:
                await asyncio.sleep(0.05)
                continue

            try:
                await asyncio.shield(scheduled_task)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] fresh profile 轮换任务执行异常 (project_id={project_id}, source={source}): {e}"
                )

            if not self._fresh_profile_restart_pending:
                return True
            await asyncio.sleep(0)

    async def shutdown_idle_runtime_if_needed(
        self,
        *,
        idle_ttl_seconds: Optional[int] = None,
        reason: str = "idle_runtime_ttl",
    ) -> bool:
        if self._fresh_profile_restart_pending:
            return False

        try:
            ttl_seconds = max(
                60,
                int(self._idle_tab_ttl_seconds if idle_ttl_seconds is None else idle_ttl_seconds),
            )
        except Exception:
            ttl_seconds = 600

        browser_instance = self.browser
        if not (self._initialized and browser_instance) or getattr(browser_instance, "stopped", False):
            return False
        if self._get_runtime_idle_seconds() < ttl_seconds:
            return False
        if await self._has_active_browser_work():
            return False

        async with self._resident_lock:
            if self._resident_tabs:
                return False

        async with self._custom_lock:
            if self._custom_tabs:
                return False

        await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=reason)
        return True

    async def reclaim_runtime_memory(
        self,
        *,
        reason: str = "manual",
        aggressive: bool = False,
    ) -> dict[str, int]:
        stats = {
            "resident_tabs_closed": 0,
            "runtime_shutdown": 0,
            "profiles_deleted": 0,
            "recaptcha_cache_deleted": 0,
            "proxy_extensions_deleted": 0,
            "python_gc_collected": 0,
        }

        await self._cancel_background_runtime_tasks(reason=f"memory_reclaim:{reason}")

        reclaimable_slot_ids = await self._collect_reclaimable_resident_slot_ids()
        if not aggressive and reclaimable_slot_ids:
            reclaimable_slot_ids = reclaimable_slot_ids[:1]

        for slot_id in reclaimable_slot_ids:
            try:
                await self._close_resident_tab(slot_id)
                stats["resident_tabs_closed"] += 1
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 关闭可回收 resident 失败 (slot={slot_id}, reason={reason}): {e}"
                )

        should_shutdown_runtime = False
        browser_instance = self.browser
        if self._initialized and browser_instance and not getattr(browser_instance, "stopped", False):
            has_active_work = await self._has_active_browser_work()
            async with self._resident_lock:
                has_resident_tabs = bool(self._resident_tabs)
            async with self._custom_lock:
                has_custom_tabs = bool(self._custom_tabs)
            should_shutdown_runtime = not has_active_work and not has_resident_tabs and not has_custom_tabs

        if should_shutdown_runtime:
            await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=f"memory_reclaim:{reason}")
            stats["runtime_shutdown"] += 1

        stale_stats = await self.cleanup_stale_runtime_artifacts(reason=f"memory_reclaim:{reason}")
        for key in ("profiles_deleted", "recaptcha_cache_deleted", "proxy_extensions_deleted"):
            stats[key] = int(stale_stats.get(key, 0) or 0)

        try:
            stats["python_gc_collected"] = max(0, int(gc.collect()))
        except Exception:
            stats["python_gc_collected"] = 0

        if any(int(value or 0) > 0 for value in stats.values()):
            debug_logger.log_info(f"[BrowserCaptcha] 内存回收完成 ({reason}): {stats}")
        return stats

    def _check_available(self):
        """检查服务是否可用"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "检测到 Docker 环境，默认禁用内置浏览器打码。"
                "如需启用请设置环境变量 ALLOW_DOCKER_HEADED_CAPTCHA=true。"
            )
        if self._requires_virtual_display() and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker 内置浏览器打码已启用，但 DISPLAY 未设置。"
                "请设置 DISPLAY（例如 :99）并启动 Xorg/Xdummy 等虚拟显示。"
            )
        if not NODRIVER_AVAILABLE or uc is None:
            raise RuntimeError(
                "nodriver 未安装或不可用。"
                "请手动安装: pip install nodriver"
            )

    def _mark_browser_health(self, healthy: bool):
        self._last_health_probe_at = time.monotonic()
        self._last_health_probe_ok = bool(healthy)

    def _invalidate_browser_health(self):
        self._last_health_probe_at = 0.0
        self._last_health_probe_ok = False

    def _mark_runtime_restart(self):
        self._last_runtime_restart_at = time.time()
        self._mark_runtime_active()

    def _was_runtime_restarted_recently(self, window_seconds: float = 5.0) -> bool:
        if self._last_runtime_restart_at <= 0.0:
            return False
        return (time.time() - self._last_runtime_restart_at) <= max(0.0, window_seconds)

    def _reset_browser_launch_failure_state(self) -> None:
        self._browser_launch_failure_streak = 0
        self._browser_launch_cooldown_until = 0.0
        self._browser_launch_last_error = ""

    def _mark_browser_launch_failure(self, error: Any) -> None:
        self._browser_launch_failure_streak = min(
            8,
            max(0, int(self._browser_launch_failure_streak or 0)) + 1,
        )
        error_text = str(error or "").strip()
        error_lower = error_text.lower()
        base_cooldown_seconds = 2.0
        if isinstance(error, PermissionError) or "winerror 5" in error_lower:
            base_cooldown_seconds = 5.0
        elif any(keyword in error_lower for keyword in ("address already in use", "only one usage", "port")):
            base_cooldown_seconds = 8.0
        cooldown_seconds = min(
            45.0,
            base_cooldown_seconds * (2 ** min(4, self._browser_launch_failure_streak - 1)),
        )
        self._browser_launch_cooldown_until = time.monotonic() + cooldown_seconds
        self._browser_launch_last_error = f"{type(error).__name__}: {error_text or '<empty>'}"

    def _raise_if_browser_launch_cooling_down(self) -> None:
        remaining_seconds = self._get_browser_launch_cooldown_remaining_seconds()
        if remaining_seconds <= 0.0:
            return
        suffix = f", last_error={self._browser_launch_last_error}" if self._browser_launch_last_error else ""
        raise RuntimeError(
            f"浏览器启动冷却中，请 {remaining_seconds:.1f}s 后重试{suffix}"
        )

    async def _restart_browser_for_project(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        fresh_profile: bool = False,
    ) -> bool:
        async with self._runtime_recover_lock:
            if fresh_profile and await self._has_active_browser_work():
                self._mark_fresh_profile_restart_pending(
                    reason=f"fresh_restart_deferred:{project_id}",
                    force=True,
                )
                await self._maybe_execute_pending_fresh_profile_restart(
                    project_id,
                    token_id=token_id,
                    source="fresh_restart_deferred_active_work",
                )
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id} fresh profile 重启已延后到当前并发 drain 后立即执行"
                )
                return True
            if not fresh_profile and self._was_runtime_restarted_recently():
                try:
                    if await self._probe_browser_runtime():
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            return_slot_key=True,
                        )
                        if resident_info is not None and slot_id:
                            self._remember_project_affinity(project_id, slot_id, resident_info)
                            self._remember_token_affinity(token_id, slot_id, resident_info)
                            self._resident_error_streaks.pop(slot_id, None)
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] project_id={project_id} 检测到最近已完成浏览器恢复，复用当前运行态 (slot={slot_id})"
                            )
                            return True
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id} 复用最近恢复运行态失败，继续执行整浏览器重启: {e}"
                    )

            restarted = await self._restart_browser_for_project_unlocked(
                project_id,
                token_id=token_id,
                fresh_profile=fresh_profile,
            )
            if restarted:
                self._mark_runtime_restart()
            return restarted

    async def _restart_browser_for_project_unlocked(
        self,
        project_id: str,
        token_id: Optional[int] = None,
        *,
        fresh_profile: bool = False,
    ) -> bool:
        """重启整个 nodriver 浏览器，仅恢复当前请求所需标签页。"""
        restart_reason = f"restart_project:{project_id}"
        if fresh_profile:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 准备执行 fresh profile 浏览器冷启动"
            )
            restart_reason = f"fresh_restart_project:{project_id}"
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 准备重启 nodriver 浏览器以恢复"
            )

        await self._shutdown_browser_runtime(cancel_idle_reaper=False, reason=restart_reason)
        if fresh_profile:
            self._reset_local_recaptcha_asset_caches(purge_disk=True)
        await self.initialize()

        slot_id, resident_info = await self._ensure_resident_tab(
            project_id,
            token_id=token_id,
            force_create=True,
            return_slot_key=True,
        )
        if resident_info is None or not slot_id:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 浏览器重启后无法定位可用共享标签页")
            return False

        self._remember_project_affinity(project_id, slot_id, resident_info)
        self._remember_token_affinity(token_id, slot_id, resident_info)
        self._resident_error_streaks.pop(slot_id, None)
        if fresh_profile:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 已使用全新无状态浏览器恢复当前共享标签页 "
                f"(active_slot={slot_id}, warmup_disabled=true)"
            )
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 浏览器重启后已恢复当前共享标签页 "
                f"(active_slot={slot_id}, warmup_disabled=true)"
            )
        return True

    def _is_browser_launch_cooldown_active(self) -> bool:
        return self._get_browser_launch_cooldown_remaining_seconds() > 0.0

    def _get_browser_launch_cooldown_remaining_seconds(self) -> float:
        return max(0.0, float(self._browser_launch_cooldown_until or 0.0) - time.monotonic())

    async def _run_with_timeout(self, awaitable, timeout_seconds: float, label: str):
        """统一收口 nodriver 操作超时，避免单次卡死拖住整条请求链路。"""
        effective_timeout = max(0.5, float(timeout_seconds or 0))
        try:
            return await asyncio.wait_for(awaitable, timeout=effective_timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"{label} 超时 ({effective_timeout:.1f}s)") from e

    async def _wait_for_display_ready(self, display_value: str, timeout_seconds: float = 5.0):
        """Docker 有头模式下等待 X display socket 就绪，避免容器重启后立刻拉起浏览器失败。"""
        if not (IS_DOCKER and display_value and display_value.startswith(":") and os.name == "posix"):
            return

        display_suffix = display_value.split(".", 1)[0].lstrip(":")
        if not display_suffix.isdigit():
            return

        socket_path = f"/tmp/.X11-unix/X{display_suffix}"
        deadline = time.monotonic() + max(0.5, float(timeout_seconds or 0))
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                return
            await asyncio.sleep(0.1)

        raise RuntimeError(
            f"DISPLAY={display_value} 对应的 X display socket 未就绪: {socket_path}"
        )

    def _requires_virtual_display(self) -> bool:
        """仅在显式有头模式下要求 Docker/Linux 提供 DISPLAY/虚拟显示。"""
        return bool(IS_DOCKER and os.name == "posix" and not self.headless)

