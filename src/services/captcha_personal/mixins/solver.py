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
from typing import TYPE_CHECKING, Any, Optional, Dict, Any, Iterable
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

class BrowserSolverMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def _tab_evaluate(
        self,
        tab,
        script: str,
        label: str,
        timeout_seconds: Optional[float] = None,
        *,
        await_promise: bool = False,
        return_by_value: bool = False,
    ):
        result = await self._run_with_timeout(
            tab.evaluate(
                script,
                await_promise=await_promise,
                return_by_value=return_by_value,
            ),
            timeout_seconds or self._command_timeout_seconds,
            label,
        )
        if return_by_value:
            return self._normalize_nodriver_evaluate_result(result)
        return result

    async def _tab_get(self, tab, url: str, label: str, timeout_seconds: Optional[float] = None):
        return await self._run_with_timeout(
            tab.get(url),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )

    async def _browser_get(
        self,
        url: str,
        label: str,
        new_tab: bool = False,
        new_window: bool = False,
        timeout_seconds: Optional[float] = None,
    ):
        target_url = str(url or "").strip() or PERSONAL_COOKIE_PREBIND_URL
        prebind_url = (
            PERSONAL_COOKIE_PREBIND_URL
            if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL
            else target_url
        )
        tab = await self._run_with_timeout(
            self.browser.get(prebind_url, new_tab=new_tab, new_window=new_window),
            timeout_seconds or self._navigation_timeout_seconds,
            label,
        )
        await self._apply_tab_startup_spoofs(
            tab,
            label=label,
            target_url=target_url,
        )
        if target_url.lower() != PERSONAL_COOKIE_PREBIND_URL:
            await self._tab_get(
                tab,
                target_url,
                label=f"{label}:navigate_target",
                timeout_seconds=timeout_seconds,
            )
        return tab

    async def _dispatch_input_command(self, tab, command: Any, *, label: str, timeout_seconds: float = 2.0):
        return await self._run_with_timeout(
            tab.send(command),
            timeout_seconds=timeout_seconds,
            label=label,
        )

    async def _sleep_with_deadline(self, deadline: float, preferred_seconds: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(max(0.0, min(preferred_seconds, remaining)))

    async def _browser_send_command(
        self,
        command: Any,
        label: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        return await self._run_with_timeout(
            self.browser.connection.send(command),
            timeout_seconds or self._command_timeout_seconds,
            label or "browser.command",
        )

    async def _wait_for_document_ready(self, tab, retries: int = 30, interval_seconds: float = 1.0) -> bool:
        """等待页面文档加载完成。"""
        for _ in range(retries):
            try:
                ready_state = await self._tab_evaluate(
                    tab,
                    "document.readyState",
                    label="document.readyState",
                    timeout_seconds=2.0,
                )
                if ready_state == "complete":
                    return True
            except Exception as e:
                if self._is_browser_runtime_error(e):
                    self._mark_browser_health(False)
                    raise
            await asyncio.sleep(interval_seconds)
        return False

    async def _execute_recaptcha_on_tab(self, tab, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token

        Args:
            tab: nodriver 标签页对象
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token 或 None
        """
        execute_timeout_ms = int(max(1000, self._solve_timeout_seconds * 1000))
        execute_result = await self._tab_evaluate(
            tab,
            f"""
                (async () => {{
                    const finishError = (error) => {{
                        const message = error && error.message ? error.message : String(error || 'execute failed');
                        return {{ ok: false, error: message }};
                    }};

                    try {{
                        const token = await new Promise((resolve, reject) => {{
                            let settled = false;
                            const done = (handler, value) => {{
                                if (settled) return;
                                settled = true;
                                handler(value);
                            }};
                            const timer = setTimeout(() => {{
                                done(reject, new Error('execute timeout'));
                            }}, {execute_timeout_ms});

                            try {{
                                grecaptcha.enterprise.ready(() => {{
                                    grecaptcha.enterprise.execute({json.dumps(self.website_key)}, {{action: {json.dumps(action)}}})
                                        .then((token) => {{
                                            clearTimeout(timer);
                                            done(resolve, token);
                                        }})
                                        .catch((error) => {{
                                            clearTimeout(timer);
                                            done(reject, error);
                                        }});
                                }});
                            }} catch (error) {{
                                clearTimeout(timer);
                                done(reject, error);
                            }}
                        }});

                        return {{ ok: true, token }};
                    }} catch (error) {{
                        return finishError(error);
                    }}
                }})()
            """,
            label=f"execute_recaptcha:{action}",
            timeout_seconds=self._solve_timeout_seconds + 2.0,
            await_promise=True,
            return_by_value=True,
        )

        token = execute_result.get("token") if isinstance(execute_result, dict) else None
        if not token:
            error = execute_result.get("error") if isinstance(execute_result, dict) else execute_result
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 错误: {error}")

        if token:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ Token 获取成功 (长度: {len(token)})")
        else:
            debug_logger.log_warning("[BrowserCaptcha] Token 获取失败，交由上层执行标签页恢复")

        return token

    async def _execute_custom_recaptcha_on_tab(
        self,
        tab,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """在指定标签页执行任意站点的 reCAPTCHA。"""
        ts = int(time.time() * 1000)
        token_var = f"_custom_recaptcha_token_{ts}"
        error_var = f"_custom_recaptcha_error_{ts}"
        execute_target = "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"

        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;

                try {{
                    grecaptcha.ready(function() {{
                        {execute_target}('{website_key}', {{action: '{action}'}})
                            .then(function(token) {{
                                window.{token_var} = token;
                            }})
                            .catch(function(err) {{
                                window.{error_var} = err.message || 'execute failed';
                            }});
                    }});
                }} catch (e) {{
                    window.{error_var} = e.message || 'exception';
                }}
            }})()
        """

        await self._tab_evaluate(
            tab,
            execute_script,
            label=f"execute_custom_recaptcha:{action}",
            timeout_seconds=5.0,
        )

        token = None
        for _ in range(30):
            await tab.sleep(0.5)
            token = await self._tab_evaluate(
                tab,
                f"window.{token_var}",
                label=f"poll_custom_recaptcha_token:{action}",
                timeout_seconds=2.0,
            )
            if token:
                break
            error = await self._tab_evaluate(
                tab,
                f"window.{error_var}",
                label=f"poll_custom_recaptcha_error:{action}",
                timeout_seconds=2.0,
            )
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] 自定义 reCAPTCHA 错误: {error}")
                break

        try:
            await self._tab_evaluate(
                tab,
                f"delete window.{token_var}; delete window.{error_var};",
                label="cleanup_custom_recaptcha_temp_vars",
                timeout_seconds=5.0,
            )
        except:
            pass

        if token:
            post_wait_seconds = 3
            try:
                post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            except Exception:
                pass
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 自定义 reCAPTCHA 已完成，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                await tab.sleep(post_wait_seconds)

        return token

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载

        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 注入 reCAPTCHA 脚本...")

        await self._inject_recaptcha_bootstrap_script(
            tab,
            script_path="recaptcha/enterprise.js",
            website_key=self.website_key,
            label="inject_recaptcha_script",
        )

        initial_settle_seconds = 1.0
        if IS_DOCKER:
            initial_settle_seconds = 2.0
        elif self._proxy_url:
            initial_settle_seconds = 1.5
        if initial_settle_seconds > 0:
            await tab.sleep(initial_settle_seconds)

        max_wait_seconds = 12.0 if self.headless else 15.0
        if IS_DOCKER:
            max_wait_seconds = max(max_wait_seconds, 20.0)
        if self._proxy_url:
            max_wait_seconds += 5.0

        poll_interval_seconds = 0.5
        max_attempts = max(1, int(max_wait_seconds / poll_interval_seconds))
        last_bootstrap_state = None

        for i in range(max_attempts):
            try:
                is_ready = await self._tab_evaluate(
                    tab,
                    "typeof grecaptcha !== 'undefined' && "
                    "typeof grecaptcha.enterprise !== 'undefined' && "
                    "typeof grecaptcha.enterprise.execute === 'function'",
                    label="check_recaptcha_ready",
                    timeout_seconds=4.0,
                )

                if is_ready:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] reCAPTCHA 已就绪 "
                        f"(等待了 {initial_settle_seconds + i * poll_interval_seconds:.1f}s)"
                    )
                    return True

                if i in {4, 10, 18, 28}:
                    try:
                        last_bootstrap_state = await self._tab_evaluate(
                            tab,
                            """
                                (() => {
                                    const state = window.__flow2apiRecaptchaBootstrapState;
                                    if (!state || typeof state !== 'object') return null;
                                    return {
                                        status: String(state.status || ''),
                                        url: String(state.url || ''),
                                        error: String(state.error || ''),
                                        attempts: Number(state.attempts || 0),
                                    };
                                })()
                            """,
                            label="read_recaptcha_bootstrap_state",
                            timeout_seconds=3.0,
                            return_by_value=True,
                        )
                    except Exception as state_error:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 读取 reCAPTCHA bootstrap 状态失败: {state_error}"
                        )
                        last_bootstrap_state = None

                    if isinstance(last_bootstrap_state, dict):
                        status = str(last_bootstrap_state.get("status") or "").strip().lower()
                        debug_logger.log_info(
                            "[BrowserCaptcha] reCAPTCHA bootstrap 状态: "
                            f"status={status or '<empty>'}, "
                            f"attempts={last_bootstrap_state.get('attempts')}, "
                            f"url={last_bootstrap_state.get('url') or '<empty>'}, "
                            f"error={last_bootstrap_state.get('error') or '<empty>'}"
                        )
                        if status in {"error", "timeout"}:
                            await self._inject_recaptcha_bootstrap_script(
                                tab,
                                script_path="recaptcha/enterprise.js",
                                website_key=self.website_key,
                                label="inject_recaptcha_script_force_retry",
                                force_remote=True,
                            )

                await tab.sleep(poll_interval_seconds)
            except Exception as e:
                if self._is_browser_runtime_error(e):
                    self._mark_browser_health(False)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 检查 reCAPTCHA 时浏览器运行态断开，停止等待并触发恢复: {e}"
                    )
                    raise
                debug_logger.log_warning(f"[BrowserCaptcha] 检查 reCAPTCHA 时异常: {e}")
                await tab.sleep(0.5)

        if isinstance(last_bootstrap_state, dict):
            debug_logger.log_warning(
                "[BrowserCaptcha] reCAPTCHA 加载超时 "
                f"(bootstrap_status={last_bootstrap_state.get('status') or '<empty>'}, "
                f"attempts={last_bootstrap_state.get('attempts')}, "
                f"url={last_bootstrap_state.get('url') or '<empty>'}, "
                f"error={last_bootstrap_state.get('error') or '<empty>'})"
            )
        else:
            debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _wait_for_custom_recaptcha(
        self,
        tab,
        website_key: str,
        enterprise: bool = False,
    ) -> bool:
        """等待任意站点的 reCAPTCHA 加载，用于分数测试。"""
        debug_logger.log_info("[BrowserCaptcha] 检测自定义 reCAPTCHA...")

        ready_check = (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        ) if enterprise else (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
        )
        script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
        label = "Enterprise" if enterprise else "V3"

        is_ready = await self._tab_evaluate(
            tab,
            ready_check,
            label="check_custom_recaptcha_preloaded",
            timeout_seconds=2.5,
        )
        if is_ready:
            debug_logger.log_info(f"[BrowserCaptcha] 自定义 reCAPTCHA {label} 已加载")
            return True

        debug_logger.log_info("[BrowserCaptcha] 未检测到自定义 reCAPTCHA，注入脚本...")
        await self._inject_recaptcha_bootstrap_script(
            tab,
            script_path=script_path,
            website_key=website_key,
            label="inject_custom_recaptcha_script",
        )

        await tab.sleep(3)
        for i in range(20):
            is_ready = await self._tab_evaluate(
                tab,
                ready_check,
                label="check_custom_recaptcha_ready",
                timeout_seconds=2.5,
            )
            if is_ready:
                debug_logger.log_info(f"[BrowserCaptcha] 自定义 reCAPTCHA {label} 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)

        debug_logger.log_warning("[BrowserCaptcha] 自定义 reCAPTCHA 加载超时")
        return False

    async def _verify_score_on_tab(self, tab, token: str, verify_url: str) -> Dict[str, Any]:
        """直接读取测试页面展示的分数，避免 verify.php 与页面显示口径不一致。"""
        _ = token
        _ = verify_url
        started_at = time.time()
        timeout_seconds = 25.0
        refresh_clicked = False
        last_snapshot: Dict[str, Any] = {}

        try:
            timeout_seconds = float(getattr(config, "browser_score_dom_wait_seconds", 25) or 25)
        except Exception:
            pass

        while (time.time() - started_at) < timeout_seconds:
            try:
                result = await self._tab_evaluate(tab, """
                    (() => {
                        const bodyText = ((document.body && document.body.innerText) || "")
                            .replace(/\\u00a0/g, " ")
                            .replace(/\\r/g, "");
                        const patterns = [
                            { source: "current_score", regex: /Your score is:\\s*([01](?:\\.\\d+)?)/i },
                            { source: "selected_score", regex: /Selected Score Test:[\\s\\S]{0,400}?Score:\\s*([01](?:\\.\\d+)?)/i },
                            { source: "history_score", regex: /(?:^|\\n)\\s*Score:\\s*([01](?:\\.\\d+)?)\\s*;/i },
                        ];
                        let score = null;
                        let source = "";
                        for (const item of patterns) {
                            const match = bodyText.match(item.regex);
                            if (!match) continue;
                            const parsed = Number(match[1]);
                            if (!Number.isNaN(parsed) && parsed >= 0 && parsed <= 1) {
                                score = parsed;
                                source = item.source;
                                break;
                            }
                        }
                        const uaMatch = bodyText.match(/Current User Agent:\\s*([^\\n]+)/i);
                        const ipMatch = bodyText.match(/Current IP Address:\\s*([^\\n]+)/i);
                        return {
                            score,
                            source,
                            raw_text: bodyText.slice(0, 4000),
                            current_user_agent: uaMatch ? uaMatch[1].trim() : "",
                            current_ip_address: ipMatch ? ipMatch[1].trim() : "",
                            title: document.title || "",
                            url: location.href || "",
                        };
                    })()
                """, label="verify_score_dom", timeout_seconds=10.0)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

            if isinstance(result, dict):
                last_snapshot = result
                score = result.get("score")
                if isinstance(score, (int, float)):
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    return {
                        "verify_mode": "browser_page_dom",
                        "verify_elapsed_ms": elapsed_ms,
                        "verify_http_status": None,
                        "verify_result": {
                            "success": True,
                            "score": score,
                            "source": result.get("source") or "antcpt_dom",
                            "raw_text": result.get("raw_text") or "",
                            "current_user_agent": result.get("current_user_agent") or "",
                            "current_ip_address": result.get("current_ip_address") or "",
                            "page_title": result.get("title") or "",
                            "page_url": result.get("url") or "",
                        },
                    }

            if not refresh_clicked and (time.time() - started_at) >= 2:
                refresh_clicked = True
                try:
                    await self._tab_evaluate(tab, """
                        (() => {
                            const nodes = Array.from(
                                document.querySelectorAll('button, input[type="button"], input[type="submit"], a')
                            );
                            const target = nodes.find((node) => {
                                const text = (node.innerText || node.textContent || node.value || "").trim();
                                return /Refresh score now!?/i.test(text);
                            });
                            if (target) {
                                target.click();
                                return true;
                            }
                            return false;
                        })()
                    """, label="verify_score_click_refresh", timeout_seconds=5.0)
                except Exception:
                    pass

            await tab.sleep(0.5)

        elapsed_ms = int((time.time() - started_at) * 1000)
        if not isinstance(last_snapshot, dict):
            last_snapshot = {"raw": last_snapshot}

        return {
            "verify_mode": "browser_page_dom",
            "verify_elapsed_ms": elapsed_ms,
            "verify_http_status": None,
            "verify_result": {
                "success": False,
                "score": None,
                "source": "antcpt_dom_timeout",
                "raw_text": last_snapshot.get("raw_text") or "",
                "current_user_agent": last_snapshot.get("current_user_agent") or "",
                "current_ip_address": last_snapshot.get("current_ip_address") or "",
                "page_title": last_snapshot.get("title") or "",
                "page_url": last_snapshot.get("url") or "",
                "error": last_snapshot.get("error") or "未在页面中读取到分数",
            },
        }

    async def _solve_with_resident_tab(
        self,
        slot_id: str,
        project_id: str,
        resident_info: Optional[ResidentTabInfo],
        action: str,
        *,
        consume_reservation: bool = False,
        success_label: str,
    ) -> Optional[str]:
        """在共享常驻标签页上执行一次打码，并统一更新成功态。"""
        if not resident_info or not resident_info.tab or not resident_info.recaptcha_ready:
            if consume_reservation:
                await self._release_resident_slot_reservation(slot_id, resident_info=resident_info)
            return None

        start_time = time.time()
        async with resident_info.solve_lock:
            if consume_reservation:
                await self._consume_resident_slot_reservation(slot_id, resident_info=resident_info)
            token = await self._run_with_timeout(
                self._execute_recaptcha_on_tab(resident_info.tab, action),
                timeout_seconds=self._solve_timeout_seconds,
                label=f"{success_label}:{slot_id}:{project_id}:{action}",
            )

        if not token:
            return None

        duration_ms = (time.time() - start_time) * 1000
        resident_info.last_used_at = time.time()
        resident_info.use_count += 1
        browser_solve_count = self._record_browser_solve_success(
            source="resident",
            project_id=project_id,
        )
        self._remember_project_affinity(project_id, slot_id, resident_info)
        self._resident_error_streaks.pop(slot_id, None)
        self._mark_browser_health(True)
        if resident_info.fingerprint:
            self._remember_fingerprint(resident_info.fingerprint)
        else:
            resident_info.fingerprint = await self._refresh_last_fingerprint(resident_info.tab)
        debug_logger.log_info(
            "[BrowserCaptcha] ✅ Token生成成功"
            f"（slot={slot_id}, 耗时 {duration_ms:.0f}ms, "
            f"slot_use_count={resident_info.use_count}, "
            f"browser_solve_count={browser_solve_count}"
            "）"
        )
        await self._maybe_execute_pending_fresh_profile_restart(
            project_id,
            source="resident_solve_success",
        )
        return token

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
        """获取 reCAPTCHA token

        使用全局共享打码标签页池。标签页不再按 project_id 一对一绑定，
        谁拿到空闲 tab 就用谁的；只有 Session Token 刷新/故障恢复会优先参考最近一次映射。

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        def finish_result(
            token: Optional[str],
            resolved_slot_id: Optional[str] = None,
        ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
            if return_slot_id:
                return token, (str(resolved_slot_id or "").strip() or None if token else None)
            return token

        debug_logger.log_info(
            f"[BrowserCaptcha] get_token 开始: project_id={project_id}, token_id={token_id}, action={action}, 当前标签页数={len(self._resident_tabs)}/{self._max_resident_tabs}"
        )
        self._mark_runtime_active()

        await self._wait_for_pending_fresh_profile_restart_before_solve(
            project_id,
            token_id=token_id,
            source="get_token_pre_initialize",
        )

        # 确保浏览器已初始化
        await self.initialize()

        await self._wait_for_pending_fresh_profile_restart_before_solve(
            project_id,
            token_id=token_id,
            source="get_token_pre_resident_pick",
        )

        reserved_slot_id: Optional[str] = None

        async def release_reserved_slot():
            nonlocal reserved_slot_id
            if reserved_slot_id:
                await self._release_resident_slot_reservation(reserved_slot_id)
                reserved_slot_id = None

        try:
            debug_logger.log_info(
                f"[BrowserCaptcha] 开始从共享打码池获取标签页 (project: {project_id}, token_id={token_id}, 当前: {len(self._resident_tabs)}/{self._max_resident_tabs})"
            )
            resident_pick_started_at = time.monotonic()
            try:
                slot_id, resident_info = await self._ensure_resident_tab(
                    project_id,
                    token_id=token_id,
                    reserve_for_solve=True,
                    return_slot_key=True,
                )
            except Exception as e:
                if not self._is_browser_runtime_error(e):
                    raise
                self._mark_browser_health(False)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页分配时浏览器运行态断开，立即重启恢复 (project: {project_id}, token_id={token_id}): {e}"
                )
                slot_id, resident_info = None, None
                if await self._recover_browser_runtime(project_id, reason="ensure_resident_tab_runtime_error"):
                    try:
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            reserve_for_solve=True,
                            return_slot_key=True,
                        )
                    except Exception as retry_error:
                        if not self._is_browser_runtime_error(retry_error):
                            raise
                        self._mark_browser_health(False)
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 浏览器恢复后分配共享标签页仍断开 (project: {project_id}, token_id={token_id}): {retry_error}"
                        )
                        slot_id, resident_info = None, None
            reserved_slot_id = slot_id or None
            if resident_info is None or not slot_id:
                if await self._wait_for_active_resident_rebuild(timeout_seconds=min(20.0, self._solve_timeout_seconds)):
                    slot_id, resident_info = await self._ensure_resident_tab(
                        project_id,
                        token_id=token_id,
                        reserve_for_solve=True,
                        return_slot_key=True,
                    )
                    reserved_slot_id = slot_id or None
            if resident_info is None or not slot_id:
                if not await self._probe_browser_runtime():
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页池为空且浏览器疑似失活，尝试重启恢复 (project: {project_id}, token_id={token_id})"
                    )
                    if await self._recover_browser_runtime(project_id, reason="ensure_resident_tab"):
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            reserve_for_solve=True,
                            return_slot_key=True,
                        )
                        reserved_slot_id = slot_id or None

            if resident_info is None or not slot_id:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页池不可用，fallback 到传统模式 (project: {project_id}, token_id={token_id})"
                )
                legacy_token = await self._get_token_legacy(project_id, action, token_id=token_id)
                return finish_result(legacy_token, None)

            debug_logger.log_info(
                "[BrowserCaptcha] 共享标签页已分配 "
                f"(project_id={project_id}, token_id={token_id}, slot={slot_id}, "
                f"pick_elapsed={time.monotonic() - resident_pick_started_at:.3f}s, "
                f"slot_use_count={int(getattr(resident_info, 'use_count', 0) or 0)}, "
                f"pending={int(getattr(resident_info, 'pending_assignment_count', 0) or 0)}, "
                f"ready={bool(getattr(resident_info, 'recaptcha_ready', False))})"
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] ✅ 共享标签页可用 (slot={slot_id}, project={project_id}, token_id={token_id}, use_count={resident_info.use_count})"
            )

            if resident_info and resident_info.tab:
                cookie_bound = await self._ensure_resident_token_binding(
                    resident_info,
                    token_id,
                    label=f"get_token:{slot_id}",
                )
                if not cookie_bound:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页 cookie 绑定校验失败，准备重建 (slot={slot_id}, project={project_id}, token_id={token_id})"
                    )

            if resident_info and resident_info.tab and not resident_info.recaptcha_ready:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 共享标签页未就绪，准备重建 cold slot={slot_id}, project={project_id}, token_id={token_id}"
                )
                await self._mark_resident_slot_unavailable(
                    slot_id,
                    resident_info,
                    reason=f"cold_slot:{project_id}",
                )
                await release_reserved_slot()
                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    reserve_for_solve=True,
                    return_slot_key=True,
                )
                reserved_slot_id = slot_id or None
                if resident_info is None:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] cold slot 重建失败，升级为浏览器级恢复 (slot={slot_id}, project={project_id}, token_id={token_id})"
                    )
                    if await self._recover_browser_runtime(project_id, reason=f"cold_resident_tab:{slot_id or 'unknown'}"):
                        slot_id, resident_info = await self._ensure_resident_tab(
                            project_id,
                            token_id=token_id,
                            reserve_for_solve=True,
                            return_slot_key=True,
                        )
                        reserved_slot_id = slot_id or None

            if resident_info and resident_info.recaptcha_ready and resident_info.tab:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 从共享常驻标签页即时生成 token (slot={slot_id}, project={project_id}, action={action})..."
                )
                runtime_recovered = False
                try:
                    token = await self._solve_with_resident_tab(
                        slot_id,
                        project_id,
                        resident_info,
                        action,
                        consume_reservation=True,
                        success_label="resident_solve",
                    )
                    reserved_slot_id = None
                    if token:
                        return finish_result(token, slot_id)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 共享标签页生成失败 (slot={slot_id}, project={project_id}, token_id={token_id})，尝试重建..."
                    )
                    await self._mark_resident_slot_unavailable(
                        slot_id,
                        resident_info,
                        reason=f"resident_solve_empty:{project_id}",
                    )
                except Exception as e:
                    reserved_slot_id = None
                    debug_logger.log_warning(f"[BrowserCaptcha] 共享标签页异常 (slot={slot_id}): {e}，尝试重建...")
                    await self._mark_resident_slot_unavailable(
                        slot_id,
                        resident_info,
                        reason=f"resident_solve_error:{project_id}",
                    )
                    if self._is_browser_runtime_error(e):
                        runtime_recovered = await self._recover_browser_runtime(
                            project_id,
                            reason=f"resident_solve:{slot_id}",
                        )
                        if runtime_recovered:
                            slot_id, resident_info = await self._ensure_resident_tab(
                                project_id,
                                token_id=token_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None
                            if resident_info and slot_id:
                                try:
                                    token = await self._solve_with_resident_tab(
                                        slot_id,
                                        project_id,
                                        resident_info,
                                        action,
                                        consume_reservation=True,
                                        success_label="resident_solve_after_runtime_recover",
                                    )
                                    reserved_slot_id = None
                                    if token:
                                        return finish_result(token, slot_id)
                                except Exception as retry_error:
                                    reserved_slot_id = None
                                    debug_logger.log_warning(
                                        f"[BrowserCaptcha] 浏览器重启恢复后共享标签页仍失败 (slot={slot_id}): {retry_error}"
                                    )

                if not runtime_recovered:
                    await release_reserved_slot()
                    slot_id, resident_info = await self._rebuild_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=slot_id,
                        reserve_for_solve=True,
                        return_slot_key=True,
                    )
                    reserved_slot_id = slot_id or None
                    if resident_info is None:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 共享标签页重建返回空，升级为浏览器级恢复 (slot={slot_id}, project={project_id}, token_id={token_id})"
                        )
                        if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild_empty:{slot_id or 'unknown'}"):
                            slot_id, resident_info = await self._ensure_resident_tab(
                                project_id,
                                token_id=token_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None

                    if resident_info:
                        needs_secondary_rebuild = False
                        try:
                            token = await self._solve_with_resident_tab(
                                slot_id,
                                project_id,
                                resident_info,
                                action,
                                consume_reservation=True,
                                success_label="resident_resolve_after_rebuild",
                            )
                            reserved_slot_id = None
                            if token:
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Token生成成功 (slot={slot_id})")
                                return finish_result(token, slot_id)
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 重建标签页后未拿到 token (slot={slot_id})，准备执行二次恢复"
                            )
                            needs_secondary_rebuild = True
                        except Exception as rebuild_error:
                            reserved_slot_id = None
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 重建标签页后仍无法打码 (slot={slot_id}): {rebuild_error}"
                            )
                            needs_secondary_rebuild = True
                            if self._is_browser_runtime_error(rebuild_error):
                                if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild:{slot_id}"):
                                    slot_id, resident_info = await self._ensure_resident_tab(
                                        project_id,
                                        token_id=token_id,
                                        reserve_for_solve=True,
                                        return_slot_key=True,
                                    )
                                    reserved_slot_id = slot_id or None
                                    if resident_info and slot_id:
                                        try:
                                            token = await self._solve_with_resident_tab(
                                                slot_id,
                                                project_id,
                                                resident_info,
                                                action,
                                                consume_reservation=True,
                                                success_label="resident_resolve_after_browser_restart",
                                            )
                                            reserved_slot_id = None
                                            if token:
                                                return finish_result(token, slot_id)
                                        except Exception as restart_error:
                                            reserved_slot_id = None
                                            debug_logger.log_warning(
                                                f"[BrowserCaptcha] 浏览器重启后 resident 仍失败 (slot={slot_id}): {restart_error}"
                                            )
                        if needs_secondary_rebuild and slot_id and resident_info:
                            await self._mark_resident_slot_unavailable(
                                slot_id,
                                resident_info,
                                reason=f"resident_rebuild_retry:{project_id}",
                            )
                            debug_logger.log_info(
                                f"[BrowserCaptcha] 重建标签页仍未恢复，开始二次重建 (slot={slot_id}, project={project_id}, token_id={token_id})"
                            )
                            await release_reserved_slot()
                            slot_id, resident_info = await self._rebuild_resident_tab(
                                project_id,
                                token_id=token_id,
                                slot_id=slot_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None
                            if resident_info and slot_id:
                                try:
                                    token = await self._solve_with_resident_tab(
                                        slot_id,
                                        project_id,
                                        resident_info,
                                        action,
                                        consume_reservation=True,
                                        success_label="resident_resolve_after_second_rebuild",
                                    )
                                    reserved_slot_id = None
                                    if token:
                                        debug_logger.log_info(
                                            f"[BrowserCaptcha] ✅ 二次重建后 Token生成成功 (slot={slot_id})"
                                        )
                                        return finish_result(token, slot_id)
                                except Exception as second_rebuild_error:
                                    reserved_slot_id = None
                                    debug_logger.log_warning(
                                        f"[BrowserCaptcha] 二次重建后 resident 仍失败 (slot={slot_id}): {second_rebuild_error}"
                                    )
                    elif not await self._probe_browser_runtime():
                        if await self._recover_browser_runtime(project_id, reason=f"resident_rebuild_empty:{slot_id}"):
                            slot_id, resident_info = await self._ensure_resident_tab(
                                project_id,
                                token_id=token_id,
                                reserve_for_solve=True,
                                return_slot_key=True,
                            )
                            reserved_slot_id = slot_id or None
                            if resident_info and slot_id:
                                try:
                                    token = await self._solve_with_resident_tab(
                                        slot_id,
                                        project_id,
                                        resident_info,
                                        action,
                                        consume_reservation=True,
                                        success_label="resident_resolve_after_empty_recover",
                                    )
                                    reserved_slot_id = None
                                    if token:
                                        return finish_result(token, slot_id)
                                except Exception as empty_recover_error:
                                    reserved_slot_id = None
                                    debug_logger.log_warning(
                                        f"[BrowserCaptcha] 浏览器空恢复后 resident 仍失败 (slot={slot_id}): {empty_recover_error}"
                                    )

            debug_logger.log_warning(
                f"[BrowserCaptcha] 所有常驻方式失败，fallback 到传统模式 (project: {project_id}, token_id={token_id})"
            )
            legacy_token = await self._get_token_legacy(project_id, action, token_id=token_id)
            if legacy_token and slot_id:
                self._resident_error_streaks.pop(slot_id, None)
            return finish_result(legacy_token, None)
        finally:
            await release_reserved_slot()

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        return_slot_id: bool = False,
    ) -> Optional[str] | tuple[Optional[str], Optional[str]]:
        """对外暴露统一取 token 接口，保持单实例与池化 worker 行为一致。"""
        return await self._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=return_slot_id,
        )

    async def get_token_with_metadata(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        token, slot_id = await self._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=True,
        )
        if not token:
            return None, None, None
        return token, slot_id, token_id

    async def invalidate_token(self, project_id: str):
        """当检测到 token 无效时调用，重建当前项目最近映射的共享标签页。

        Args:
            project_id: 项目 ID
        """
        debug_logger.log_warning(
            f"[BrowserCaptcha] Token 被标记为无效 (project: {project_id})，仅重建共享池中的对应标签页，避免清空全局浏览器状态"
        )

        # 重建标签页
        slot_id, resident_info = await self._rebuild_resident_tab(project_id, return_slot_key=True)
        if resident_info and slot_id:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 标签页已重建 (project: {project_id}, slot={slot_id})")
        else:
            debug_logger.log_error(f"[BrowserCaptcha] 标签页重建失败 (project: {project_id})")

    async def _get_token_legacy(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        *,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        max_attempts = 2
        async with self._legacy_lock:
            for attempt in range(max_attempts):
                if not self._initialized or not self.browser:
                    await self.initialize()

                start_time = time.time()
                tab = None
                browser_context_id = None

                try:
                    debug_logger.log_info(
                        "[BrowserCaptcha] [Legacy] 创建独立临时 context 执行验证，"
                        "先绑 cookie 再首跳 labs.google，避免首轮请求丢登录态"
                    )
                    tab, browser_context_id = await self._create_isolated_context_tab(
                        PERSONAL_COOKIE_PREBIND_URL,
                        label=f"legacy_browser_create_context:{project_id}",
                        create_timeout_seconds=self._navigation_timeout_seconds,
                    )
                    browser_context_id = browser_context_id or self._extract_tab_browser_context_id(tab)
                    legacy_info = ResidentTabInfo(
                        tab,
                        slot_id=f"legacy-{project_id}",
                        project_id=project_id,
                        token_id=token_id,
                        browser_context_id=browser_context_id,
                    )
                    await self._apply_token_cookie_binding(
                        legacy_info,
                        token_id,
                        label=f"legacy:{project_id}",
                        force=True,
                    )

                    if not await self._open_labs_bootstrap_page(tab, label=f"legacy:{project_id}"):
                        debug_logger.log_error("[BrowserCaptcha] [Legacy] 打开 labs 引导页失败")
                        return None

                    # 等待 reCAPTCHA 加载
                    recaptcha_ready = await self._wait_for_recaptcha(tab)

                    if not recaptcha_ready:
                        debug_logger.log_error("[BrowserCaptcha] [Legacy] reCAPTCHA 无法加载")
                        return None

                    # 执行 reCAPTCHA
                    debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证 (action: {action})...")
                    token = await self._run_with_timeout(
                        self._execute_recaptcha_on_tab(tab, action),
                        timeout_seconds=self._solve_timeout_seconds,
                        label=f"legacy_solve:{project_id}:{action}",
                    )

                    duration_ms = (time.time() - start_time) * 1000

                    if token:
                        browser_solve_count = self._record_browser_solve_success(
                            source="legacy",
                            project_id=project_id,
                        )
                        self._mark_browser_health(True)
                        await self._refresh_last_fingerprint(tab)
                        debug_logger.log_info(
                            "[BrowserCaptcha] [Legacy] ✅ Token获取成功"
                            f"（耗时 {duration_ms:.0f}ms, browser_solve_count={browser_solve_count}）"
                        )
                        await self._maybe_execute_pending_fresh_profile_restart(
                            project_id,
                            token_id=token_id,
                            source="legacy_solve_success",
                        )
                        return token

                    debug_logger.log_error("[BrowserCaptcha] [Legacy] Token获取失败（返回null）")
                    return None

                except Exception as e:
                    if attempt < (max_attempts - 1) and self._is_browser_runtime_error(e):
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] [Legacy] 浏览器运行态异常，尝试重启恢复后重试: {e}"
                        )
                        await self._recover_browser_runtime(project_id, reason=f"legacy_attempt_{attempt + 1}")
                        continue

                    debug_logger.log_error(f"[BrowserCaptcha] [Legacy] 获取token异常: {str(e)}")
                    return None
                finally:
                    # 关闭 legacy 临时标签页（但保留浏览器）
                    if tab:
                        await self._dispose_browser_context_quietly(browser_context_id)
                        await self._close_tab_quietly(tab)

        return None

    def get_token_pool_status(self) -> Dict[str, Any]:
        return {
            "token_pool_enabled": bool(getattr(config, "token_pool_enabled", False)),
            "token_pool_status": "未启用" if not getattr(config, "token_pool_enabled", False) else "空闲",
            "token_pool_total_ready": 0,
            "token_pool_bucket_count": 0,
            "token_pool_waiting_requests": 0,
            "token_pool_refill_inflight": 0,
            "token_pool_last_refill_at": None,
            "token_pool_last_token_at": None,
            "token_pool_oldest_token_age_seconds": None,
            "token_pool_next_expire_in_seconds": None,
            "token_pool_hit_count": 0,
            "token_pool_miss_count": 0,
            "token_pool_wait_count": 0,
            "token_pool_expired_count": 0,
        }

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """为任意站点执行 reCAPTCHA，用于分数测试等场景。

        与普通 legacy 模式不同，这里会复用同一个常驻标签页，避免每次冷启动新 tab。
        """
        await self.initialize()
        self._mark_runtime_active()
        self._last_fingerprint = None

        cache_key = f"{website_url}|{website_key}|{1 if enterprise else 0}"
        warmup_seconds = float(getattr(config, "browser_score_test_warmup_seconds", 12) or 12)
        per_request_settle_seconds = float(
            getattr(config, "browser_score_test_settle_seconds", 2.5) or 2.5
        )
        max_retries = 2

        async with self._custom_lock:
            for attempt in range(max_retries):
                start_time = time.time()
                custom_info = self._custom_tabs.get(cache_key)
                tab = custom_info.get("tab") if isinstance(custom_info, dict) else None

                try:
                    if tab is None:
                        debug_logger.log_info(f"[BrowserCaptcha] [Custom] 创建常驻测试标签页: {website_url}")
                        tab = await self._browser_get(
                            website_url,
                            label="custom_browser_get",
                            new_tab=True,
                        )
                        custom_info = {
                            "tab": tab,
                            "recaptcha_ready": False,
                            "warmed_up": False,
                            "created_at": time.time(),
                        }
                        self._custom_tabs[cache_key] = custom_info

                    page_loaded = False
                    for _ in range(20):
                        ready_state = await self._tab_evaluate(
                            tab,
                            "document.readyState",
                            label="custom_document_ready",
                            timeout_seconds=2.0,
                        )
                        if ready_state == "complete":
                            page_loaded = True
                            break
                        await tab.sleep(0.5)

                    if not page_loaded:
                        raise RuntimeError("自定义页面加载超时")

                    if not custom_info.get("recaptcha_ready"):
                        recaptcha_ready = await self._wait_for_custom_recaptcha(
                            tab=tab,
                            website_key=website_key,
                            enterprise=enterprise,
                        )
                        if not recaptcha_ready:
                            raise RuntimeError("自定义 reCAPTCHA 无法加载")
                        custom_info["recaptcha_ready"] = True

                    try:
                        await self._tab_evaluate(tab, """
                            (() => {
                                try {
                                    const body = document.body || document.documentElement;
                                    const width = window.innerWidth || 1280;
                                    const height = window.innerHeight || 720;
                                    const x = Math.max(24, Math.floor(width * 0.38));
                                    const y = Math.max(24, Math.floor(height * 0.32));
                                    const moveEvent = new MouseEvent('mousemove', {
                                        bubbles: true,
                                        clientX: x,
                                        clientY: y
                                    });
                                    const overEvent = new MouseEvent('mouseover', {
                                        bubbles: true,
                                        clientX: x,
                                        clientY: y
                                    });
                                    window.focus();
                                    window.dispatchEvent(new Event('focus'));
                                    document.dispatchEvent(moveEvent);
                                    document.dispatchEvent(overEvent);
                                    if (body) {
                                        body.dispatchEvent(moveEvent);
                                        body.dispatchEvent(overEvent);
                                    }
                                    window.scrollTo(0, Math.min(320, document.body?.scrollHeight || 320));
                                } catch (e) {}
                            })()
                        """, label="custom_pre_warm_interaction", timeout_seconds=6.0)
                    except Exception:
                        pass

                    if not custom_info.get("warmed_up"):
                        if warmup_seconds > 0:
                            debug_logger.log_info(
                                f"[BrowserCaptcha] [Custom] 首次预热测试页面 {warmup_seconds:.1f}s 后再执行 token"
                            )
                            try:
                                await self._tab_evaluate(tab, """
                                    (() => {
                                        try {
                                            window.scrollTo(0, Math.min(240, document.body.scrollHeight || 240));
                                            window.dispatchEvent(new Event('mousemove'));
                                            window.dispatchEvent(new Event('focus'));
                                        } catch (e) {}
                                    })()
                                """, label="custom_warmup_interaction", timeout_seconds=6.0)
                            except Exception:
                                pass
                            await tab.sleep(warmup_seconds)
                        custom_info["warmed_up"] = True
                    elif per_request_settle_seconds > 0:
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] 复用测试标签页，执行前额外等待 {per_request_settle_seconds:.1f}s"
                        )
                        await tab.sleep(per_request_settle_seconds)

                    debug_logger.log_info(f"[BrowserCaptcha] [Custom] 使用常驻测试标签页执行验证 (action: {action})...")
                    token = await self._execute_custom_recaptcha_on_tab(
                        tab=tab,
                        website_key=website_key,
                        action=action,
                        enterprise=enterprise,
                    )

                    duration_ms = (time.time() - start_time) * 1000
                    if token:
                        extracted_fingerprint = await self._extract_tab_fingerprint(tab)
                        if not extracted_fingerprint:
                            try:
                                fallback_ua = await self._tab_evaluate(
                                    tab,
                                    "navigator.userAgent || ''",
                                    label="custom_fallback_ua",
                                )
                                fallback_lang = await self._tab_evaluate(
                                    tab,
                                    "navigator.language || ''",
                                    label="custom_fallback_lang",
                                )
                                extracted_fingerprint = {
                                    "user_agent": fallback_ua or "",
                                    "accept_language": fallback_lang or "",
                                    "proxy_url": self._proxy_url,
                                }
                            except Exception:
                                extracted_fingerprint = None
                        self._last_fingerprint = extracted_fingerprint
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] ✅ 常驻测试标签页 Token获取成功（耗时 {duration_ms:.0f}ms）"
                        )
                        return token

                    raise RuntimeError("自定义 token 获取失败（返回 null）")
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] [Custom] 尝试 {attempt + 1}/{max_retries} 失败: {str(e)}"
                    )
                    stale_info = self._custom_tabs.pop(cache_key, None)
                    stale_tab = stale_info.get("tab") if isinstance(stale_info, dict) else None
                    if stale_tab:
                        await self._close_tab_quietly(stale_tab)
                    if attempt >= max_retries - 1:
                        debug_logger.log_error(f"[BrowserCaptcha] [Custom] 获取token异常: {str(e)}")
                        return None

            return None

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """在同一个常驻标签页里获取 token 并直接校验页面分数。"""
        self._mark_runtime_active()
        token_started_at = time.time()
        token = await self.get_custom_token(
            website_url=website_url,
            website_key=website_key,
            action=action,
            enterprise=enterprise,
        )
        token_elapsed_ms = int((time.time() - token_started_at) * 1000)

        if not token:
            return {
                "token": None,
                "token_elapsed_ms": token_elapsed_ms,
                "verify_mode": "browser_page",
                "verify_elapsed_ms": 0,
                "verify_http_status": None,
                "verify_result": {},
            }

        cache_key = f"{website_url}|{website_key}|{1 if enterprise else 0}"
        async with self._custom_lock:
            custom_info = self._custom_tabs.get(cache_key)
            tab = custom_info.get("tab") if isinstance(custom_info, dict) else None
            if tab is None:
                raise RuntimeError("页面分数测试标签页不存在")
            verify_payload = await self._verify_score_on_tab(tab, token, verify_url)

        return {
            "token": token,
            "token_elapsed_ms": token_elapsed_ms,
            **verify_payload,
        }

    async def _resolve_personal_proxy(self):
        """Read proxy config for personal captcha browser.
        Priority: captcha browser_proxy > request proxy."""
        if not self.db:
            return None, None, None, None, None
        try:
            captcha_cfg = await self.db.get_captcha_config()
            browser_proxy_pool = str(getattr(captcha_cfg, "browser_proxy_pool", "") or "").strip()
            if browser_proxy_pool:
                pooled_proxy = await self.db.pick_browser_proxy_from_pool()
                if pooled_proxy:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 使用验证码代理池: {pooled_proxy}")
                    return _parse_proxy_url(pooled_proxy)
            if getattr(captcha_cfg, "browser_proxy_enabled", False) and getattr(captcha_cfg, "browser_proxy_url", None):
                url = str(getattr(captcha_cfg, "browser_proxy_url", "") or "").strip()
                if url:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 使用验证码代理: {url}")
                    return _parse_proxy_url(url)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取验证码代理配置失败: {e}")
        try:
            proxy_cfg = await self.db.get_proxy_config()
            proxy_pool_text = str(getattr(proxy_cfg, "proxy_pool", "") or "")
            proxy_pool_candidates = [item.strip() for item in re.split(r"[\r\n,]+", proxy_pool_text) if item.strip()]
            if proxy_cfg and proxy_cfg.enabled and proxy_pool_candidates:
                pooled_proxy = proxy_pool_candidates[0]
                debug_logger.log_info(f"[BrowserCaptcha] Personal 回退使用请求代理池: {pooled_proxy}")
                return _parse_proxy_url(pooled_proxy)
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                url = proxy_cfg.proxy_url.strip()
                if url:
                    debug_logger.log_info(f"[BrowserCaptcha] Personal 回退使用请求代理: {url}")
                    return _parse_proxy_url(url)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取请求代理配置失败: {e}")

        for candidate_url in _read_windows_internet_settings_proxy_candidates():
            protocol, host, port, username, password = _parse_proxy_url(candidate_url)
            if not protocol or not host or not port:
                continue
            if str(host).strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
                continue
            if not await self._is_tcp_endpoint_reachable(str(host), int(port), timeout_seconds=0.5):
                continue
            debug_logger.log_info(
                f"[BrowserCaptcha] Personal 自动接管本机可用代理: {candidate_url}"
            )
            return protocol, host, port, username, password

        return None, None, None, None, None

    async def _is_tcp_endpoint_reachable(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float = 0.5,
    ) -> bool:
        try:
            connection = asyncio.open_connection(host, int(port))
            reader, writer = await asyncio.wait_for(connection, timeout_seconds)
        except Exception:
            return False

        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        finally:
            del reader

        return True

    def _cleanup_proxy_extension(self):
        """Remove temporary proxy auth extension directory."""
        if self._proxy_ext_dir and os.path.isdir(self._proxy_ext_dir):
            try:
                shutil.rmtree(self._proxy_ext_dir, ignore_errors=True)
            except Exception:
                pass
            self._proxy_ext_dir = None

    def _get_recaptcha_bootstrap_candidate_urls(
        self,
        script_path: str,
        website_key: Optional[str] = None,
    ) -> list[str]:
        """Build the candidate bootstrap URLs for the requested reCAPTCHA script."""
        normalized_path = script_path.lstrip("/")
        # 默认优先 recaptcha.net；google.com 仅作为回退。
        hosts = ["https://www.recaptcha.net", "https://www.google.com"]
        suffix = f"?render={website_key}" if website_key else ""
        return [f"{host}/{normalized_path}{suffix}" for host in hosts]

    async def _resolve_personal_proxy_download_url(self) -> Optional[str]:
        """Resolve the full proxy URL for downloading bootstrap scripts."""
        protocol, host, port, username, password = await self._resolve_personal_proxy()
        return _compose_proxy_url(protocol, host, port, username, password)

    async def _download_recaptcha_asset_bytes(self, remote_url: str) -> tuple[bytes, str]:
        """Download a reCAPTCHA-related static asset through the same proxy path."""
        proxy_url = await self._resolve_personal_proxy_download_url()
        async with AsyncSession() as session:
            response = await session.get(
                remote_url,
                timeout=RECAPTCHA_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS,
                proxy=proxy_url,
                headers={"Accept": "*/*"},
                impersonate="chrome120",
                verify=False,
            )

        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}")

        mime_type = _guess_recaptcha_asset_mime_type(
            remote_url,
            str(response.headers.get("content-type") or ""),
        )
        return bytes(response.content), mime_type

    async def _load_recaptcha_asset_bytes(self, remote_url: str) -> tuple[bytes, str]:
        """Load a static asset from local cache first, then refresh from upstream."""
        cache_path = _get_recaptcha_asset_cache_path(self._recaptcha_asset_cache_dir, remote_url)

        async with self._recaptcha_asset_cache_lock:
            if cache_path.exists():
                try:
                    cached_content = cache_path.read_bytes()
                    cache_age = max(0.0, time.time() - cache_path.stat().st_mtime)
                    if cached_content and cache_age <= RECAPTCHA_ASSET_CACHE_TTL_SECONDS:
                        return cached_content, _guess_recaptcha_asset_mime_type(remote_url)
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 读取本地静态资源缓存失败: path={cache_path.name}, error={e}"
                    )

            try:
                content, mime_type = await self._download_recaptcha_asset_bytes(remote_url)
                _write_binary_cache(cache_path, content)
                return content, mime_type
            except Exception as e:
                if cache_path.exists():
                    try:
                        cached_content = cache_path.read_bytes()
                        if cached_content:
                            debug_logger.log_warning(
                                f"[BrowserCaptcha] 静态资源下载失败，回退使用本地缓存: url={remote_url}, error={e}"
                            )
                            return cached_content, _guess_recaptcha_asset_mime_type(remote_url)
                    except Exception:
                        pass
                raise

    async def _discover_dynamic_recaptcha_static_urls(self, bootstrap_source: str) -> list[str]:
        """Recursively discover reCAPTCHA static assets from bootstrap/JS/CSS content."""
        discovered: list[str] = []
        seen: set[str] = set()
        pending: list[str] = []

        def _queue(remote_url: str):
            normalized = str(remote_url or "").strip()
            if not normalized or not _is_localizable_recaptcha_asset_url(normalized) or normalized in seen:
                return
            seen.add(normalized)
            pending.append(normalized)
            discovered.append(normalized)

        def _queue_text_urls(source_text: str):
            for remote_url in _extract_remote_urls_from_text(source_text):
                _queue(remote_url)
                for companion_url in _iter_recaptcha_release_companion_urls(remote_url):
                    _queue(companion_url)

        _queue_text_urls(bootstrap_source)

        while pending:
            remote_url = pending.pop(0)
            try:
                content, mime_type = await self._load_recaptcha_asset_bytes(remote_url)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 动态发现静态资源失败: url={remote_url}, error={e}"
                )
                continue

            normalized_mime_type = _guess_recaptcha_asset_mime_type(remote_url, mime_type)
            if normalized_mime_type == "text/css":
                css_source = content.decode("utf-8", errors="ignore")
                for child_url in _extract_remote_urls_from_css(css_source, remote_url):
                    _queue(child_url)
            elif normalized_mime_type in {"text/javascript", "application/javascript"}:
                js_source = content.decode("utf-8", errors="ignore")
                _queue_text_urls(js_source)

        return discovered

    async def _build_recaptcha_asset_data_url(self, remote_url: str) -> str:
        """Build a data URL backed by the local asset cache."""
        cached_data_url = self._recaptcha_asset_data_url_cache.get(remote_url)
        if cached_data_url:
            return cached_data_url

        content, mime_type = await self._load_recaptcha_asset_bytes(remote_url)
        normalized_mime_type = _guess_recaptcha_asset_mime_type(remote_url, mime_type)

        if normalized_mime_type == "text/css":
            css_source = content.decode("utf-8", errors="ignore")
            replacements: dict[str, str] = {}
            for child_url in _extract_remote_urls_from_css(css_source, remote_url):
                if not _is_localizable_recaptcha_asset_url(child_url):
                    continue
                replacements[child_url] = await self._build_recaptcha_asset_data_url(child_url)
            localized_css = _rewrite_css_urls_with_local_assets(css_source, remote_url, replacements)
            data_url = _build_data_url(localized_css.encode("utf-8"), "text/css;charset=utf-8")
        else:
            if normalized_mime_type in {"text/javascript", "application/javascript"}:
                js_source = content.decode("utf-8", errors="ignore")
                replacements: dict[str, str] = {}
                for child_url in _extract_remote_urls_from_text(js_source):
                    if not _is_localizable_recaptcha_asset_url(child_url):
                        continue
                    replacements[child_url] = await self._build_recaptcha_asset_data_url(child_url)
                localized_js = _rewrite_text_urls_with_local_assets(js_source, replacements)
                data_url = _build_data_url(localized_js.encode("utf-8"), "text/javascript;charset=utf-8")
            else:
                data_url = _build_data_url(content, normalized_mime_type)

        self._recaptcha_asset_data_url_cache[remote_url] = data_url
        return data_url

    async def _build_recaptcha_local_asset_bundle(
        self,
        bootstrap_source: str,
        bootstrap_candidate_urls: Iterable[str],
    ) -> Dict[str, Any]:
        """Build a compact rewrite bundle for local reCAPTCHA static assets."""
        candidate_url_list = [url for url in bootstrap_candidate_urls if url]
        signature_input = "||".join(candidate_url_list) + "||" + hashlib.md5(
            bootstrap_source.encode("utf-8")
        ).hexdigest()
        signature = hashlib.md5(signature_input.encode("utf-8")).hexdigest()
        if (
            self._recaptcha_asset_bundle_signature == signature
            and self._recaptcha_asset_bundle is not None
        ):
            return self._recaptcha_asset_bundle

        full_map: dict[str, str] = {}
        path_map: dict[str, str] = {}
        data_map: dict[str, str] = {}

        def _register_aliases(remote_url: str, data_key: str):
            for alias_url in _iter_recaptcha_asset_url_aliases(remote_url):
                full_map[alias_url] = data_key
                parsed = urlparse(alias_url)
                if parsed.path:
                    path_key = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    path_map[path_key] = data_key
                    if not parsed.query:
                        path_map[parsed.path] = data_key

        bootstrap_key = "bootstrap"
        data_map[bootstrap_key] = _build_data_url(
            bootstrap_source.encode("utf-8"),
            "text/javascript;charset=utf-8",
        )
        for candidate_url in candidate_url_list:
            _register_aliases(candidate_url, bootstrap_key)

        static_urls = await self._discover_dynamic_recaptcha_static_urls(bootstrap_source)

        asset_index = 0
        for remote_url in static_urls:
            data_key = f"a{asset_index}"
            data_map[data_key] = await self._build_recaptcha_asset_data_url(remote_url)
            _register_aliases(remote_url, data_key)
            asset_index += 1

        bundle = {
            "full": full_map,
            "path": path_map,
            "data": data_map,
        }
        self._recaptcha_asset_bundle_signature = signature
        self._recaptcha_asset_bundle = bundle
        self._recaptcha_asset_hook_source = None
        return bundle

    def _build_recaptcha_local_asset_hook_source(self, bundle: Dict[str, Any]) -> str:
        """Build the browser-side rewrite hook for reCAPTCHA static assets."""
        if self._recaptcha_asset_hook_source is not None:
            return self._recaptcha_asset_hook_source

        hook_source = f"""
            (() => {{
                const bundle = {json.dumps(bundle, separators=(",", ":"))};
                const fullMap = bundle.full || Object.create(null);
                const pathMap = bundle.path || Object.create(null);
                const dataMap = bundle.data || Object.create(null);

                const resolveLocalAsset = (value) => {{
                    if (!value || typeof value !== 'string') return value;
                    if (value.startsWith('data:') || value.startsWith('blob:')) return value;

                    let key = fullMap[value];
                    if (key && dataMap[key]) return dataMap[key];

                    try {{
                        const parsed = new URL(value, window.location.href);
                        key = fullMap[parsed.href]
                            || pathMap[parsed.pathname + parsed.search]
                            || pathMap[parsed.pathname];
                        if (key && dataMap[key]) return dataMap[key];
                    }} catch (error) {{}}

                    return value;
                }};

                const clearIntegrity = (node) => {{
                    if (!node || !node.tagName) return;
                    const tagName = String(node.tagName).toUpperCase();
                    if (tagName !== 'SCRIPT' && tagName !== 'LINK') return;
                    try {{ node.integrity = ''; }} catch (error) {{}}
                    try {{ node.removeAttribute('integrity'); }} catch (error) {{}}
                    try {{ node.crossOrigin = ''; }} catch (error) {{}}
                    try {{ node.removeAttribute('crossorigin'); }} catch (error) {{}}
                }};

                const applyNode = (node) => {{
                    if (!node || typeof node !== 'object') return node;
                    try {{
                        if (typeof node.src === 'string' && node.src) {{
                            const nextSrc = resolveLocalAsset(node.src);
                            if (nextSrc !== node.src) {{
                                clearIntegrity(node);
                                node.src = nextSrc;
                            }}
                        }}
                    }} catch (error) {{}}
                    try {{
                        if (typeof node.href === 'string' && node.href) {{
                            const nextHref = resolveLocalAsset(node.href);
                            if (nextHref !== node.href) {{
                                clearIntegrity(node);
                                node.href = nextHref;
                            }}
                        }}
                    }} catch (error) {{}}
                    return node;
                }};

                window.__flow2apiRecaptchaLocalAssets = bundle;
                if (window.__flow2apiRecaptchaLocalAssetsPatched) {{
                    return Object.keys(fullMap).length;
                }}
                window.__flow2apiRecaptchaLocalAssetsPatched = true;

                const originalSetAttribute = Element.prototype.setAttribute;
                Element.prototype.setAttribute = function(name, value) {{
                    let nextValue = value;
                    if ((name === 'src' || name === 'href') && typeof value === 'string') {{
                        nextValue = resolveLocalAsset(value);
                        if (nextValue !== value) {{
                            clearIntegrity(this);
                        }}
                    }}
                    return originalSetAttribute.call(this, name, nextValue);
                }};

                const patchUrlProperty = (ctorName, propertyName) => {{
                    const ctor = window[ctorName];
                    if (!ctor || !ctor.prototype) return;
                    const descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, propertyName);
                    if (!descriptor || typeof descriptor.set !== 'function' || typeof descriptor.get !== 'function') {{
                        return;
                    }}
                    Object.defineProperty(ctor.prototype, propertyName, {{
                        configurable: true,
                        enumerable: descriptor.enumerable,
                        get() {{
                            return descriptor.get.call(this);
                        }},
                        set(value) {{
                            const nextValue = typeof value === 'string' ? resolveLocalAsset(value) : value;
                            if (nextValue !== value) {{
                                clearIntegrity(this);
                            }}
                            return descriptor.set.call(this, nextValue);
                        }},
                    }});
                }};

                patchUrlProperty('HTMLScriptElement', 'src');
                patchUrlProperty('HTMLLinkElement', 'href');
                patchUrlProperty('HTMLImageElement', 'src');
                patchUrlProperty('HTMLIFrameElement', 'src');

                const originalAppendChild = Node.prototype.appendChild;
                Node.prototype.appendChild = function(node) {{
                    return originalAppendChild.call(this, applyNode(node));
                }};

                const originalInsertBefore = Node.prototype.insertBefore;
                Node.prototype.insertBefore = function(node, referenceNode) {{
                    return originalInsertBefore.call(this, applyNode(node), referenceNode);
                }};

                if (typeof window.fetch === 'function') {{
                    const originalFetch = window.fetch.bind(window);
                    window.fetch = function(input, init) {{
                        if (typeof input === 'string') {{
                            input = resolveLocalAsset(input);
                        }} else if (input && typeof input.url === 'string') {{
                            const nextInput = resolveLocalAsset(input.url);
                            if (nextInput !== input.url) {{
                                input = nextInput;
                            }}
                        }}
                        return originalFetch(input, init);
                    }};
                }}

                if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {{
                    const originalOpen = window.XMLHttpRequest.prototype.open;
                    window.XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
                        return originalOpen.call(this, method, resolveLocalAsset(url), ...rest);
                    }};
                }}

                const workerObjectUrlCache = Object.create(null);
                const toWorkerObjectUrl = (value) => {{
                    if (typeof value !== 'string' || !value.startsWith('data:')) {{
                        return value;
                    }}
                    if (workerObjectUrlCache[value]) {{
                        return workerObjectUrlCache[value];
                    }}

                    try {{
                        const commaIndex = value.indexOf(',');
                        if (commaIndex < 0) return value;
                        const meta = value.slice(5, commaIndex);
                        const payload = value.slice(commaIndex + 1);
                        const mimeType = meta || 'text/javascript;charset=utf-8';
                        const binary = meta.includes(';base64')
                            ? atob(payload)
                            : decodeURIComponent(payload);
                        const bytes = new Uint8Array(binary.length);
                        for (let index = 0; index < binary.length; index += 1) {{
                            bytes[index] = binary.charCodeAt(index);
                        }}
                        const objectUrl = URL.createObjectURL(new Blob([bytes], {{ type: mimeType }}));
                        workerObjectUrlCache[value] = objectUrl;
                        return objectUrl;
                    }} catch (error) {{
                        return value;
                    }}
                }};

                if (typeof window.Worker === 'function') {{
                    const NativeWorker = window.Worker;
                    window.Worker = function(scriptURL, options) {{
                        const nextScriptURL = typeof scriptURL === 'string'
                            ? toWorkerObjectUrl(resolveLocalAsset(scriptURL))
                            : scriptURL;
                        return new NativeWorker(nextScriptURL, options);
                    }};
                    window.Worker.prototype = NativeWorker.prototype;
                }}

                if (typeof window.SharedWorker === 'function') {{
                    const NativeSharedWorker = window.SharedWorker;
                    window.SharedWorker = function(scriptURL, options) {{
                        const nextScriptURL = typeof scriptURL === 'string'
                            ? toWorkerObjectUrl(resolveLocalAsset(scriptURL))
                            : scriptURL;
                        return new NativeSharedWorker(nextScriptURL, options);
                    }};
                    window.SharedWorker.prototype = NativeSharedWorker.prototype;
                }}

                return Object.keys(fullMap).length;
            }})()
        """
        self._recaptcha_asset_hook_source = hook_source
        return hook_source

    async def _inject_local_recaptcha_asset_overrides(
        self,
        tab,
        bootstrap_source: str,
        bootstrap_candidate_urls: Iterable[str],
    ) -> bool:
        """Inject URL rewrite hooks so static reCAPTCHA assets are served from local cache."""
        try:
            bundle = await self._build_recaptcha_local_asset_bundle(
                bootstrap_source,
                bootstrap_candidate_urls,
            )
            hook_source = self._build_recaptcha_local_asset_hook_source(bundle)
            await self._tab_evaluate(
                tab,
                hook_source,
                label="inject_recaptcha_local_assets",
                timeout_seconds=12.0,
            )
            debug_logger.log_info("[BrowserCaptcha] 已注入本地 reCAPTCHA 静态资源映射")
            return True
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 注入本地 reCAPTCHA 静态资源映射失败: {e}")
            return False

    async def _download_recaptcha_bootstrap_source(self, remote_url: str) -> str:
        """Download the reCAPTCHA bootstrap source using the same proxy path as the browser."""
        proxy_url = await self._resolve_personal_proxy_download_url()
        async with AsyncSession() as session:
            response = await session.get(
                remote_url,
                timeout=RECAPTCHA_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS,
                proxy=proxy_url,
                headers={"Accept": "*/*"},
                impersonate="chrome120",
                verify=False,
            )

        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}")

        source = response.content.decode("utf-8", errors="ignore").strip()
        if not source or "grecaptcha" not in source or "gstatic" not in source:
            raise RuntimeError("bootstrap 内容校验失败")
        return source

    async def _load_recaptcha_bootstrap_source(
        self,
        script_path: str,
        candidate_urls: Optional[Iterable[str]] = None,
    ) -> str:
        """Load the bootstrap source from local cache first, then refresh from upstream."""
        urls = list(candidate_urls or self._get_recaptcha_bootstrap_candidate_urls(script_path))
        stale_cache_candidates: list[tuple[str, Path]] = []

        async with self._recaptcha_script_cache_lock:
            for remote_url in urls:
                cache_path = _get_recaptcha_script_cache_path(self._recaptcha_script_cache_dir, remote_url)
                if not cache_path.exists():
                    continue

                try:
                    cached_source = cache_path.read_text(encoding="utf-8").strip()
                    if not cached_source:
                        continue
                    cache_age = max(0.0, time.time() - cache_path.stat().st_mtime)
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 读取本地 reCAPTCHA 缓存失败: path={cache_path.name}, error={e}"
                    )
                    continue

                if cache_age <= RECAPTCHA_SCRIPT_CACHE_TTL_SECONDS:
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 使用本地缓存的 reCAPTCHA bootstrap: {cache_path.name}"
                    )
                    return cached_source

                stale_cache_candidates.append((remote_url, cache_path))

            last_error = None
            for remote_url in urls:
                try:
                    source = await self._download_recaptcha_bootstrap_source(remote_url)
                    cache_path = _get_recaptcha_script_cache_path(self._recaptcha_script_cache_dir, remote_url)
                    _write_text_cache(cache_path, source)
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 已刷新 reCAPTCHA bootstrap 本地缓存: {cache_path.name}"
                    )
                    return source
                except Exception as e:
                    last_error = e
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 下载 reCAPTCHA bootstrap 失败: url={remote_url}, error={e}"
                    )

            for remote_url, cache_path in stale_cache_candidates:
                try:
                    cached_source = cache_path.read_text(encoding="utf-8").strip()
                    if cached_source:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 远程刷新失败，回退使用过期缓存: url={remote_url}, path={cache_path.name}"
                        )
                        return cached_source
                except Exception:
                    continue

        raise RuntimeError(f"无法加载 reCAPTCHA bootstrap: {last_error or '未知错误'}")

    async def _inject_recaptcha_bootstrap_script(
        self,
        tab,
        script_path: str,
        website_key: str,
        label: str,
        *,
        force_remote: bool = False,
    ) -> str:
        """直接注入远程 reCAPTCHA bootstrap 脚本。"""
        candidate_urls = self._get_recaptcha_bootstrap_candidate_urls(
            script_path,
            website_key=website_key,
        )

        await self._tab_evaluate(tab, f"""
            (() => {{
                const forceRemote = {json.dumps(force_remote)};
                const stateKey = '__flow2apiRecaptchaBootstrapState';
                const scriptTimeoutMs = 8000;
                const now = () => Date.now();
                const ensureState = () => {{
                    if (!window[stateKey] || typeof window[stateKey] !== 'object') {{
                        window[stateKey] = {{
                            status: 'idle',
                            url: '',
                            error: '',
                            startedAt: 0,
                            finishedAt: 0,
                            attempts: 0,
                        }};
                    }}
                    return window[stateKey];
                }};
                const state = ensureState();
                const hasReadyApi =
                    typeof grecaptcha !== 'undefined' &&
                    (
                        typeof grecaptcha.execute === 'function' ||
                        (
                            typeof grecaptcha.enterprise !== 'undefined' &&
                            typeof grecaptcha.enterprise.execute === 'function'
                        )
                    );
                if (hasReadyApi) {{
                    state.status = 'ready';
                    state.finishedAt = now();
                    return;
                }}
                if (forceRemote) {{
                    document
                        .querySelectorAll('script[src*="recaptcha"]')
                        .forEach((node) => node.remove());
                    state.status = 'idle';
                    state.url = '';
                    state.error = '';
                    state.startedAt = 0;
                    state.finishedAt = 0;
                }} else if (
                    document.querySelector('script[src*="recaptcha"]') &&
                    state.status === 'loading'
                ) {{
                    return;
                }} else if (document.querySelector('script[src*="recaptcha"]')) {{
                    document
                        .querySelectorAll('script[src*="recaptcha"]')
                        .forEach((node) => node.remove());
                }}
                const urls = {json.dumps(candidate_urls)};
                const parent = document.head || document.documentElement || document.body;
                if (!parent) {{
                    state.status = 'error';
                    state.error = 'missing script parent';
                    state.finishedAt = now();
                    return;
                }}
                const loadScript = (index) => {{
                    if (index >= urls.length) {{
                        state.status = 'error';
                        if (!state.error) {{
                            state.error = 'all candidate urls exhausted';
                        }}
                        state.finishedAt = now();
                        return;
                    }}
                    state.status = 'loading';
                    state.url = urls[index];
                    state.error = '';
                    state.startedAt = now();
                    state.finishedAt = 0;
                    state.attempts = Number(state.attempts || 0) + 1;
                    const script = document.createElement('script');
                    script.src = urls[index];
                    script.async = true;
                    let settled = false;
                    const finish = (status, error) => {{
                        if (settled) return;
                        settled = true;
                        state.status = status;
                        state.error = error ? String(error) : '';
                        state.finishedAt = now();
                    }};
                    const timer = setTimeout(() => {{
                        script.remove();
                        finish('timeout', `script load timeout: ${{urls[index]}}`);
                        loadScript(index + 1);
                    }}, scriptTimeoutMs);
                    script.onload = () => {{
                        clearTimeout(timer);
                        finish('loaded', '');
                    }};
                    script.onerror = () => {{
                        clearTimeout(timer);
                        script.remove();
                        finish('error', `script load error: ${{urls[index]}}`);
                        loadScript(index + 1);
                    }};
                    parent.appendChild(script);
                }};
                loadScript(0);
            }})()
        """, label=label, timeout_seconds=5.0)
        debug_logger.log_info(f"[BrowserCaptcha] 已注入远程 reCAPTCHA bootstrap ({script_path})")
        return "remote"

    async def report_flow_error(
        self,
        project_id: str,
        error_reason: str,
        error_message: str = "",
        token_id: Optional[int] = None,
        slot_id: Optional[str] = None,
    ):
        """上游生成接口异常时，对常驻标签页执行自愈恢复。"""
        if not project_id:
            return

        async with self._resident_lock:
            resolved_slot_id = str(slot_id or "").strip()
            if resolved_slot_id:
                resident_info = self._resident_tabs.get(resolved_slot_id)
                if resident_info is None or not resident_info.tab:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 上游异常回调命中已失效 slot，跳过本次恢复"
                    )
                    return
            else:
                resolved_slot_id, resident_info = self._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)

        if not resolved_slot_id:
            return

        error_text = f"{error_reason or ''} {error_message or ''}".strip()
        error_lower = error_text.lower()
        if self._is_generation_policy_error(error_text):
            debug_logger.log_info(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 收到内容安全拒绝，跳过浏览器自愈: {error_reason}"
            )
            return
        if self._is_external_flow_error(error_text):
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 收到外部链路/鉴权错误，跳过 resident 自愈: {error_reason}"
            )
            return

        streak = self._resident_error_streaks.get(resolved_slot_id, 0) + 1
        self._resident_error_streaks[resolved_slot_id] = streak
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} 收到上游异常，streak={streak}, reason={error_reason}, detail={error_message[:200]}"
        )

        if not self._initialized or not self.browser:
            return

        async def _recover_current_slot():
            if self._is_force_fresh_browser_restart_error(error_text):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} "
                    "命中特定 Flow 风控错误，已标记当前 slot 不再复用；浏览器 fresh profile 轮换会等待当前并发 drain 后立即执行"
                )
                if resident_info is not None:
                    await self._mark_resident_slot_unavailable(
                        resolved_slot_id,
                        resident_info,
                        reason=f"flow_force_fresh:{project_id}:{streak}",
                    )
                self._mark_fresh_profile_restart_pending(
                    reason=f"flow_force_fresh:{project_id}:{resolved_slot_id}:streak={streak}",
                    force=True,
                )
                await self._maybe_execute_pending_fresh_profile_restart(
                    project_id,
                    token_id=token_id,
                    source="flow_force_fresh_error",
                )
                return

            # 403 / reCAPTCHA / unusual activity：浏览器级缓存清理 + 本地静态缓存刷新 + resident 恢复
            if self._is_recaptcha_cache_reset_error(error_text):
                restart_threshold = max(
                    2,
                    int(getattr(config, "browser_personal_recaptcha_restart_threshold", 2) or 2),
                )
                debug_logger.log_warning(
                    f"[BrowserCaptcha] project_id={project_id} 检测到 403/reCAPTCHA/unusual_activity 错误，清理缓存并重建"
                )
                healed = await self._clear_resident_storage_and_reload(
                    project_id,
                    token_id=token_id,
                    slot_id=resolved_slot_id,
                    clear_browser_cache=True,
                    refresh_local_assets=True,
                )
                if healed and streak < restart_threshold:
                    return

                recreated = False
                if not healed:
                    recreated = await self._recreate_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=resolved_slot_id,
                    )
                    if recreated and streak < restart_threshold:
                        return

                if streak >= restart_threshold or (not healed and not recreated):
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] project_id={project_id}, slot={resolved_slot_id} reCAPTCHA 风控连续失败，升级为整浏览器重启恢复"
                    )
                    await self._restart_browser_for_project(project_id, token_id=token_id)
                return

            # 服务端错误：根据连续失败次数决定恢复策略
            if self._is_server_side_flow_error(error_text):
                recreate_threshold = max(2, int(getattr(config, "browser_personal_recreate_threshold", 2) or 2))
                restart_threshold = max(3, int(getattr(config, "browser_personal_restart_threshold", 3) or 3))

                if streak >= restart_threshold:
                    await self._restart_browser_for_project(project_id, token_id=token_id)
                    return
                if streak >= recreate_threshold:
                    await self._recreate_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=resolved_slot_id,
                    )
                    return

                healed = await self._clear_resident_storage_and_reload(
                    project_id,
                    token_id=token_id,
                    slot_id=resolved_slot_id,
                )
                if not healed:
                    await self._recreate_resident_tab(
                        project_id,
                        token_id=token_id,
                        slot_id=resolved_slot_id,
                    )
                return

            # 其他错误：直接重建标签页
            await self._recreate_resident_tab(
                project_id,
                token_id=token_id,
                slot_id=resolved_slot_id,
            )

        await self._run_resident_recovery_task(
            resolved_slot_id,
            _recover_current_slot,
            project_id=project_id,
            error_reason=error_reason or error_message or "upstream_error",
        )

    @staticmethod
    def _should_use_explicit_no_sandbox_retry(error: Any) -> bool:
        if os.name != "posix":
            return False
        error_text = str(error or "").lower()
        return any(
            keyword in error_text
            for keyword in (
                "no_sandbox",
                "no usable sandbox",
                "setuid sandbox",
                "namespace",
                "running as root",
                "you are running as root",
            )
        )

    @staticmethod
    def _is_retryable_browser_launch_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return any(
            keyword in error_text
            for keyword in (
                "failed to connect to browser",
                "connection refused",
                "connection reset",
                "connection closed",
                "websocket is not open",
                "chrome not reachable",
                "browser has been closed",
                "target closed",
            )
        )

    @staticmethod
    def _is_memory_pressure_browser_launch_error(error: Any) -> bool:
        error_text = _flatten_exception_text(error)
        return any(
            keyword in error_text
            for keyword in (
                "0xc000012d",
                "status_commitment_limit",
                "commitment limit",
                "paging file",
                "not enough memory",
                "insufficient system resources",
                "not enough storage is available",
                "out of memory",
                "cannot allocate memory",
            )
        )

    @staticmethod
    def _is_invalid_browser_context_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return (
            "failed to find browser context" in error_text
            or "cannot find context with specified id" in error_text
            or "browser context" in error_text and "-32602" in error_text
        )

    def _is_browser_runtime_error(self, error: Any) -> bool:
        """识别浏览器运行态已损坏/已关闭的典型异常。"""
        return _is_runtime_disconnect_error(error) or self._is_no_browser_window_error(error)

    @staticmethod
    def _is_no_browser_window_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return "no browser is open" in error_text or "failed to open new tab" in error_text

    def _decode_nodriver_object_entries(self, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, list):
            return None

        result: Dict[str, Any] = {}
        for entry in value:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                return None
            key, entry_value = entry
            if not isinstance(key, str):
                return None
            result[key] = self._normalize_nodriver_evaluate_result(entry_value)
        return result

    def _normalize_nodriver_evaluate_result(self, value: Any) -> Any:
        if value is None:
            return None

        deep_serialized_value = getattr(value, "deep_serialized_value", None)
        if deep_serialized_value is not None:
            return self._normalize_nodriver_evaluate_result(deep_serialized_value)

        type_name = getattr(value, "type_", None)
        if type_name is not None and hasattr(value, "value"):
            raw_value = getattr(value, "value", None)
            if type_name == "object":
                object_entries = self._decode_nodriver_object_entries(raw_value)
                if object_entries is not None:
                    return object_entries
            if raw_value is not None:
                return self._normalize_nodriver_evaluate_result(raw_value)
            unserializable_value = getattr(value, "unserializable_value", None)
            if unserializable_value is not None:
                return str(unserializable_value)
            return value

        if isinstance(value, dict):
            typed_value_keys = {"type", "value", "objectId", "weakLocalObjectReference"}
            if "type" in value and set(value.keys()).issubset(typed_value_keys):
                raw_value = value.get("value")
                if value.get("type") == "object":
                    object_entries = self._decode_nodriver_object_entries(raw_value)
                    if object_entries is not None:
                        return object_entries
                return self._normalize_nodriver_evaluate_result(raw_value)
            return {
                key: self._normalize_nodriver_evaluate_result(item)
                for key, item in value.items()
            }

        if isinstance(value, list):
            object_entries = self._decode_nodriver_object_entries(value)
            if object_entries is not None:
                return object_entries
            return [self._normalize_nodriver_evaluate_result(item) for item in value]

        return value

    def _is_server_side_flow_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        if self._is_generation_policy_error(error_text):
            return False
        return any(keyword in error_lower for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            "\"reason\":\"internal\"",
            "server error",
            "upstream error",
        ])

    def _is_external_flow_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "429",
            "too many requests",
            "tls",
            "ssl",
            "econnreset",
            "connection reset",
            "connection aborted",
            "network is unreachable",
            "name or service not known",
            "temporary failure in name resolution",
            "timed out",
            "timeout",
            "proxyerror",
            "proxy error",
            "credentials_missing",
            "missing required authentication credential",
            "login cookie",
            "access token",
            "authorization",
        ])

    def _is_generation_policy_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "public_error_unsafe_generation",
            "unsafe_generation",
            "request contains an invalid ar",
        ])

    def _is_recaptcha_cache_reset_error(self, error_text: str) -> bool:
        """Whether the upstream error should trigger browser cache/storage reset."""
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "403",
            "forbidden",
            "recaptcha evaluation failed",
            "public_error_unusual_activity",
            "unusual_activity",
            "unusual activity",
            "recaptcha",
        ])

    def _is_force_fresh_browser_restart_error(self, error_text: str) -> bool:
        """命中特定 Flow 风控错误时，直接重启为全新无状态浏览器。"""
        error_lower = (error_text or "").lower()
        if "recaptcha evaluation failed" not in error_lower:
            return False

        return any(keyword in error_lower for keyword in [
            "public_error_unusual_activity_too_much_traffic",
            "public_error_unusual_activity",
            "public_error_something_went_wrong",
        ])

    @classmethod
    def _get_global_browser_launch_gate(cls) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        limit = _resolve_browser_launch_parallelism_limit()
        if (
            cls._launch_gate is None
            or cls._launch_gate_loop is not loop
            or cls._launch_gate_limit != limit
        ):
            cls._launch_gate = asyncio.Semaphore(limit)
            cls._launch_gate_loop = loop
            cls._launch_gate_limit = limit
        return cls._launch_gate

    def _apply_browser_instance_identity(self, browser_instance_id: int) -> None:
        normalized_instance_id = max(0, int(browser_instance_id or 0))
        self._browser_instance_id = normalized_instance_id
        self._slot_id_prefix = f"b{normalized_instance_id}-" if normalized_instance_id > 0 else ""

    def apply_pool_worker_settings(
        self,
        *,
        browser_instance_id: Optional[int] = None,
        max_resident_tabs_override: Optional[int] = None,
    ) -> None:
        if browser_instance_id is not None:
            self._apply_browser_instance_identity(browser_instance_id)

        if max_resident_tabs_override is None:
            self._max_resident_tabs_override = None
        else:
            self._max_resident_tabs_override = max(1, min(50, int(max_resident_tabs_override)))

        # pool 调整分片配额时，worker 需要立即更新本地有效 resident 上限；
        # 否则新创建的 worker 会沿用旧值，导致明明配置了多浏览器/多标签，
        # 实际每个实例仍只跑 1 个 resident slot。
        configured_total_tabs = getattr(config, "personal_max_resident_tabs", 5)
        self._max_resident_tabs = self._resolve_personal_max_resident_tabs(configured_total_tabs)

    def _create_fresh_runtime_profile_dir(self, *, prefix: str = "fresh_browser_profile_") -> str:
        PERSONAL_RUNTIME_TMP_DIR.mkdir(parents=True, exist_ok=True)
        fresh_profile_dir = tempfile.mkdtemp(
            prefix=prefix,
            dir=str(PERSONAL_RUNTIME_TMP_DIR),
        )
        normalized_dir = os.path.normpath(str(fresh_profile_dir))
        self._managed_runtime_profile_dirs.add(normalized_dir)
        self._runtime_ephemeral_user_data_dir = normalized_dir
        self.user_data_dir = normalized_dir
        return normalized_dir

    def _resolve_user_data_dir(self, headless: Optional[bool] = None) -> Optional[str]:
        _ = self.headless if headless is None else bool(headless)
        existing_runtime_profile = str(getattr(self, "_runtime_ephemeral_user_data_dir", "") or "").strip()
        if existing_runtime_profile:
            return os.path.normpath(existing_runtime_profile)

        profile_override = os.environ.get("PERSONAL_BROWSER_USER_DATA_DIR", "").strip()
        if profile_override:
            return os.path.normpath(profile_override)

        return self._create_fresh_runtime_profile_dir(prefix="browser_profile_")

    def _default_runtime_profile_dir(self) -> Path:
        return (PERSONAL_RUNTIME_DATA_DIR / "browser_profile").resolve()

    def _is_runtime_managed_profile_dir(self, path_value: Optional[str]) -> bool:
        normalized_path = str(path_value or "").strip()
        if not normalized_path:
            return False

        try:
            resolved_path = Path(normalized_path).resolve()
            runtime_data_dir = PERSONAL_RUNTIME_DATA_DIR.resolve()
            runtime_tmp_dir = PERSONAL_RUNTIME_TMP_DIR.resolve()
        except Exception:
            return False

        return (
            resolved_path == runtime_data_dir
            or runtime_data_dir in resolved_path.parents
            or resolved_path == runtime_tmp_dir
            or runtime_tmp_dir in resolved_path.parents
        )

    def _collect_runtime_profile_cleanup_targets(self) -> list[Path]:
        targets: list[Path] = []
        seen_targets: set[str] = set()

        for raw_path in (
            self.user_data_dir,
            self._runtime_ephemeral_user_data_dir,
            str(self._default_runtime_profile_dir()),
            *list(getattr(self, "_managed_runtime_profile_dirs", set()) or set()),
        ):
            normalized_path = str(raw_path or "").strip()
            if not normalized_path or not self._is_runtime_managed_profile_dir(normalized_path):
                continue
            try:
                resolved_path = Path(normalized_path).resolve()
            except Exception:
                continue
            target_key = os.path.normcase(str(resolved_path))
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)
            targets.append(resolved_path)

        return targets

    async def _purge_runtime_profile_dirs(self, reason: str) -> None:
        current_user_data_dir = str(self.user_data_dir or "").strip()
        cleanup_targets = self._collect_runtime_profile_cleanup_targets()
        if current_user_data_dir and not self._is_runtime_managed_profile_dir(current_user_data_dir):
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_warning(
                "[BrowserCaptcha] 当前 user_data_dir 不在运行时目录下，"
                f"本次恢复改用全新临时 profile: {next_profile_dir} (reason={reason})"
            )
            return

        if not cleanup_targets:
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_warning(
                "[BrowserCaptcha] 未找到可清理的运行时 profile，"
                f"改用新的临时无状态 profile: {next_profile_dir} (reason={reason})"
            )
            return

        for target_dir in cleanup_targets:
            try:
                if target_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, str(target_dir), True)
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 已删除浏览器 profile 目录以执行全新冷启动: {target_dir}"
                    )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 删除浏览器 profile 目录失败 (reason={reason}, path={target_dir}): {e}"
                )

        next_profile_dir = self._create_fresh_runtime_profile_dir()
        debug_logger.log_warning(
            f"[BrowserCaptcha] profile 清理完成，下一次启动将使用全新临时 profile: {next_profile_dir} (reason={reason})"
        )

    async def _cleanup_runtime_profile_dirs_after_shutdown(self, *, reason: str) -> bool:
        current_user_data_dir = str(self.user_data_dir or "").strip()
        cleanup_targets = self._collect_runtime_profile_cleanup_targets()
        if current_user_data_dir and not self._is_runtime_managed_profile_dir(current_user_data_dir):
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_info(
                "[BrowserCaptcha] 关闭后检测到自定义 profile 路径，"
                f"下一次启动改用全新临时 profile: {next_profile_dir} (reason={reason})"
            )
            return False

        if not cleanup_targets:
            next_profile_dir = self._create_fresh_runtime_profile_dir()
            debug_logger.log_info(
                f"[BrowserCaptcha] 关闭后未发现可复用 profile，已准备新的临时 profile: {next_profile_dir} (reason={reason})"
            )
            return False

        for target_dir in cleanup_targets:
            try:
                if target_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, str(target_dir), True)
                    debug_logger.log_info(
                        f"[BrowserCaptcha] 已清理关闭后的运行时 profile 目录: {target_dir} (reason={reason})"
                    )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 清理关闭后的运行时 profile 目录失败 (reason={reason}, path={target_dir}): {e}"
                )

        next_profile_dir = self._create_fresh_runtime_profile_dir()
        debug_logger.log_info(
            f"[BrowserCaptcha] 关闭后已切换到新的临时 profile: {next_profile_dir} (reason={reason})"
        )
        return True

    def _resolve_personal_max_resident_tabs(self, configured_tabs: Optional[int] = None) -> int:
        """计算当前模式下的有效 resident tab 上限。"""
        try:
            resolved_tabs = (
                self._max_resident_tabs_override
                if self._max_resident_tabs_override is not None
                else configured_tabs
            )
            return max(1, min(50, int(resolved_tabs if resolved_tabs is not None else 5)))
        except Exception:
            return 5

    def _reset_local_recaptcha_asset_caches(self, *, purge_disk: bool = False) -> None:
        """重置本地 reCAPTCHA 资源缓存，必要时删除磁盘缓存以强制刷新。"""
        self._recaptcha_asset_data_url_cache.clear()
        self._recaptcha_asset_bundle_signature = None
        self._recaptcha_asset_bundle = None
        self._recaptcha_asset_hook_source = None

        if not purge_disk:
            return

        for cache_dir in (self._recaptcha_script_cache_dir, self._recaptcha_asset_cache_dir):
            try:
                if not cache_dir.exists():
                    continue
                for cache_file in cache_dir.iterdir():
                    if cache_file.is_file():
                        cache_file.unlink(missing_ok=True)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 清理本地 reCAPTCHA 资源缓存失败: dir={cache_dir}, error={e}"
                )

    @classmethod
    def _resolve_configured_browser_count(cls) -> int:
        try:
            configured_value = getattr(config, "browser_count", None)
            if configured_value is None:
                configured_value = config.get_raw_config().get("captcha", {}).get("browser_count", 1)
        except Exception:
            configured_value = 1

        return resolve_effective_browser_count(configured_value)

    @classmethod
    async def cleanup_stale_runtime_artifacts(cls, *, reason: str = "manual") -> dict[str, int]:
        async with cls._lock:
            instances: list[Any] = []
            if cls._instance is not None:
                instances.append(cls._instance)
            if cls._pool_instance is not None:
                instances.append(cls._pool_instance)

        active_runtime_paths: set[str] = set()
        active_proxy_extension_paths: set[str] = set()
        for instance in instances:
            from .pool_service import _PersonalBrowserPoolService
            if isinstance(instance, _PersonalBrowserPoolService):
                workers = list(getattr(instance, "_workers", []) or [])
            else:
                workers = [instance]
            for worker in workers:
                for raw_path in (
                    str(getattr(worker, "user_data_dir", "") or "").strip(),
                    str(getattr(worker, "_runtime_ephemeral_user_data_dir", "") or "").strip(),
                ):
                    if raw_path:
                        active_runtime_paths.add(raw_path)
                proxy_ext_dir = str(getattr(worker, "_proxy_ext_dir", "") or "").strip()
                if proxy_ext_dir:
                    active_proxy_extension_paths.add(proxy_ext_dir)

        stats = await asyncio.to_thread(
            _cleanup_runtime_artifacts_sync,
            active_runtime_paths=active_runtime_paths,
            active_proxy_extension_paths=active_proxy_extension_paths,
        )
        if any(int(value or 0) > 0 for value in stats.values()):
            debug_logger.log_info(
                f"[BrowserCaptcha] 运行时临时文件清理完成 ({reason}): {stats}"
            )
        return stats

    @classmethod
    async def reset_shared_instances(cls) -> None:
        single_instance = None
        pool_instance = None
        async with cls._lock:
            single_instance = cls._instance
            pool_instance = cls._pool_instance
            cls._instance = None
            cls._pool_instance = None

        if pool_instance is not None:
            try:
                await pool_instance.close()
            except Exception:
                pass
        if single_instance is not None:
            try:
                await single_instance.close()
            except Exception:
                pass

    async def reload_config(self):
        """热更新配置（从数据库重新加载）"""
        old_headless = self.headless
        old_max_tabs = self._max_resident_tabs
        old_idle_ttl = self._idle_tab_ttl_seconds
        old_probe_ttl = self._health_probe_ttl_seconds
        old_fingerprint_ttl = self._fingerprint_cache_ttl_seconds
        old_fresh_restart_every = self._fresh_profile_restart_every_n_solves
        old_user_data_dir = self.user_data_dir
        old_runtime_config_signature = self._proxy_config_signature

        self.headless = bool(getattr(config, "personal_headless", False))
        configured_max_tabs = config.personal_max_resident_tabs
        self._max_resident_tabs = self._resolve_personal_max_resident_tabs(configured_max_tabs)
        self._idle_tab_ttl_seconds = config.personal_idle_tab_ttl_seconds
        self._refresh_runtime_tunables()
        self.user_data_dir = self._resolve_user_data_dir(self.headless)
        self._proxy_config_signature = await self._build_proxy_config_signature()
        runtime_config_changed = old_runtime_config_signature != self._proxy_config_signature

        debug_logger.log_info(
            f"[BrowserCaptcha] Personal 配置已热更新: "
            f"headless {old_headless}->{self.headless}, "
            f"max_tabs {old_max_tabs}->{self._max_resident_tabs}, "
            f"idle_ttl {old_idle_ttl}s->{self._idle_tab_ttl_seconds}s, "
            f"probe_ttl {old_probe_ttl}s->{self._health_probe_ttl_seconds}s, "
            f"fingerprint_ttl {old_fingerprint_ttl}s->{self._fingerprint_cache_ttl_seconds}s, "
            f"fresh_restart_every {old_fresh_restart_every}->{self._fresh_profile_restart_every_n_solves}, "
            f"profile {old_user_data_dir or '<isolated-temp>'}->{self.user_data_dir or '<isolated-temp>'}, "
            f"runtime_changed={runtime_config_changed}"
        )
        if (
            (
                old_headless != self.headless
                or old_user_data_dir != self.user_data_dir
                or runtime_config_changed
            )
            and (self._initialized or self.browser)
        ):
            async with self._browser_lock:
                await self._shutdown_browser_runtime_locked(
                    reason="reload_config_runtime_changed"
                )
            debug_logger.log_info(
                "[BrowserCaptcha] personal 运行参数发生变化，已重置浏览器运行态，后续请求将按新 profile/代理/模式重启"
            )
        elif old_max_tabs > self._max_resident_tabs:
            await self._trim_resident_tabs_to_limit()

    async def _trim_resident_tabs_to_limit(self) -> None:
        """在配额缩小时立即裁掉多余的空闲 resident tab，避免内存长期不回落。"""
        while True:
            async with self._resident_lock:
                overflow = len(self._resident_tabs) - max(1, int(self._max_resident_tabs or 1))
                if overflow <= 0:
                    return

                lru_slot_id = None
                lru_last_used = float("inf")
                for slot_id, resident_info in self._resident_tabs.items():
                    if resident_info.solve_lock.locked():
                        continue
                    if int(getattr(resident_info, "pending_assignment_count", 0) or 0) > 0:
                        continue
                    if resident_info.last_used_at < lru_last_used:
                        lru_last_used = resident_info.last_used_at
                        lru_slot_id = slot_id

            if not lru_slot_id:
                debug_logger.log_warning(
                    "[BrowserCaptcha] max_tabs 已缩小，但当前没有可安全裁剪的空闲 resident tab，"
                    f"当前数量={len(self._resident_tabs)}, target={self._max_resident_tabs}"
                )
                return

            await self._close_resident_tab(lru_slot_id)

    async def _build_proxy_config_signature(self) -> str:
        """基于当前数据库配置构建稳定签名，用于判断是否需要重启浏览器 runtime。"""
        if not self.db:
            return ""

        try:
            captcha_cfg = await self.db.get_captcha_config()
        except Exception:
            captcha_cfg = None

        try:
            proxy_cfg = await self.db.get_proxy_config()
        except Exception:
            proxy_cfg = None

        def normalize_pool(value: Any) -> str:
            return "\n".join(
                item.strip()
                for item in re.split(r"[\r\n,]+", str(value or ""))
                if item.strip()
            )

        startup_cookie_enabled = bool(
            getattr(captcha_cfg, "browser_startup_cookie_enabled", False)
        )
        startup_cookie_text = (
            getattr(captcha_cfg, "browser_startup_cookie", "")
            if startup_cookie_enabled
            else ""
        )

        signature_payload = {
            "captcha_browser_proxy_enabled": bool(getattr(captcha_cfg, "browser_proxy_enabled", False)),
            "captcha_browser_proxy_url": str(getattr(captcha_cfg, "browser_proxy_url", "") or "").strip(),
            "captcha_browser_proxy_pool": normalize_pool(getattr(captcha_cfg, "browser_proxy_pool", "")),
            "captcha_browser_startup_cookie_enabled": startup_cookie_enabled,
            "captcha_browser_startup_cookie_signature": build_cookie_signature(startup_cookie_text),
            "request_proxy_enabled": bool(getattr(proxy_cfg, "enabled", False)),
            "request_proxy_url": str(getattr(proxy_cfg, "proxy_url", "") or "").strip(),
            "request_proxy_pool": normalize_pool(getattr(proxy_cfg, "proxy_pool", "")),
            "request_rotation_mode": str(getattr(proxy_cfg, "rotation_mode", "") or "").strip(),
        }
        return json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _refresh_runtime_tunables(self):
        """刷新运行时调优参数，缺省时使用保守的低开销默认值。"""
        try:
            self._health_probe_ttl_seconds = max(
                0.2,
                float(getattr(config, "browser_personal_health_probe_ttl_seconds", 10.0) or 10.0),
            )
        except Exception:
            self._health_probe_ttl_seconds = 10.0

        try:
            self._fingerprint_cache_ttl_seconds = max(
                0.0,
                float(getattr(config, "browser_personal_fingerprint_cache_ttl_seconds", 3600.0) or 3600.0),
            )
        except Exception:
            self._fingerprint_cache_ttl_seconds = 3600.0

        self._fresh_profile_restart_every_n_solves = self._resolve_fresh_profile_restart_every_n_solves()

    def _resolve_fresh_profile_restart_every_n_solves(self) -> int:
        """解析浏览器 fresh profile 轮换阈值，0 表示禁用。"""
        raw_value: Any = None
        env_value = os.environ.get("PERSONAL_BROWSER_FRESH_RESTART_EVERY_N_SOLVES", "").strip()
        if env_value:
            raw_value = env_value
        else:
            try:
                raw_value = config.get_raw_config().get("captcha", {}).get(
                    "browser_personal_fresh_restart_every_n_solves",
                    10,
                )
            except Exception:
                raw_value = 10

        try:
            return max(0, int(raw_value))
        except Exception:
            return 10

    def _reset_browser_rotation_budget(self) -> None:
        self._successful_solves_since_browser_start = 0
        self._fresh_profile_restart_pending = False
        self._fresh_profile_restart_force_pending = False
        self._fresh_profile_restart_pending_reason = ""

    def _mark_runtime_active(self) -> None:
        self._runtime_last_active_at = time.time()

    def _get_runtime_idle_seconds(self) -> float:
        last_active_at = float(getattr(self, "_runtime_last_active_at", 0.0) or 0.0)
        if last_active_at <= 0.0:
            return 0.0
        return max(0.0, time.time() - last_active_at)

    def _record_browser_solve_success(self, *, source: str, project_id: Optional[str] = None) -> int:
        self._successful_solves_since_browser_start = max(
            0,
            int(self._successful_solves_since_browser_start or 0),
        ) + 1

        threshold = max(0, int(self._fresh_profile_restart_every_n_solves or 0))
        current_count = self._successful_solves_since_browser_start
        if threshold > 0 and current_count >= threshold and not self._fresh_profile_restart_pending:
            self._fresh_profile_restart_pending = True
            self._fresh_profile_restart_pending_reason = (
                f"{source}:{project_id or 'global'}:{current_count}/{threshold}"
            )
            debug_logger.log_warning(
                "[BrowserCaptcha] 浏览器成功打码次数达到 fresh profile 轮换阈值，"
                f"后续新取码会先等待当前并发清空并完成全新无状态浏览器重启 "
                f"(count={current_count}, threshold={threshold}, reason={self._fresh_profile_restart_pending_reason})"
            )
        return current_count

    def _mark_fresh_profile_restart_pending(self, *, reason: str, force: bool = False) -> None:
        normalized_reason = str(reason or "manual").strip() or "manual"
        already_pending = bool(self._fresh_profile_restart_pending)
        self._fresh_profile_restart_pending = True
        if force:
            self._fresh_profile_restart_force_pending = True
        self._fresh_profile_restart_pending_reason = normalized_reason
        if not already_pending:
            debug_logger.log_warning(
                "[BrowserCaptcha] 已请求 fresh profile 轮换，"
                f"后续新取码会等待当前并发清空并完成重启 (force={force}, reason={normalized_reason})"
            )

    async def _has_active_browser_work(self) -> bool:
        if (
            self._legacy_lock.locked()
            or self._custom_lock.locked()
            or self._tab_build_lock.locked()
            or self._browser_lock.locked()
        ):
            return True

        async with self._resident_lock:
            for slot_id, resident_info in self._resident_tabs.items():
                if self._is_resident_slot_busy_for_allocation_locked(slot_id, resident_info):
                    return True
        return False

    def _is_browser_health_fresh(self) -> bool:
        if not (self._initialized and self.browser and self._last_health_probe_ok):
            return False
        try:
            if self.browser.stopped or getattr(self.browser, "_flow2api_runtime_disconnected", False):
                return False
        except Exception:
            return False
        ttl_seconds = max(0.0, float(self._health_probe_ttl_seconds or 0.0))
        if ttl_seconds <= 0:
            return False
        return (time.monotonic() - self._last_health_probe_at) < ttl_seconds

