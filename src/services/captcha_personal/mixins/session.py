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

class BrowserSessionMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def _get_browser_cookies(
        self,
        label: str,
        timeout_seconds: Optional[float] = None,
        browser_context_id: Any = None,
    ):
        if browser_context_id is not None and self.browser:
            try:
                from nodriver import cdp

                return await self._run_with_timeout(
                    self.browser.connection.send(
                        cdp.storage.get_cookies(browser_context_id=browser_context_id)
                    ),
                    timeout_seconds or self._command_timeout_seconds,
                    label,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 按 context 读取 cookies 失败，回退全局 cookie jar ({label}): {e}"
                )

        return await self._run_with_timeout(
            self.browser.cookies.get_all(),
            timeout_seconds or self._command_timeout_seconds,
            label,
        )

    @staticmethod
    def _normalize_cookie_signature(cookie_text: Optional[str]) -> Optional[str]:
        signature = build_cookie_signature(cookie_text)
        return signature or None

    @staticmethod
    def _extract_cookie_name_domain(cookie: Any) -> tuple[str, str]:
        """兼容 nodriver cookie 对象与 dict 结构，提取 name/domain 用于日志。"""
        if isinstance(cookie, dict):
            return (
                str(cookie.get("name") or "").strip(),
                str(cookie.get("domain") or "").strip(),
            )
        return (
            str(getattr(cookie, "name", "") or "").strip(),
            str(getattr(cookie, "domain", "") or "").strip(),
        )

    @staticmethod
    def _extract_cookie_scope_host(cookie: Dict[str, Any]) -> str:
        url = str(cookie.get("url") or "").strip()
        if url:
            try:
                parsed = urlparse(url)
                return str(parsed.hostname or "").strip().lower()
            except Exception:
                return ""
        return str(cookie.get("domain") or "").strip().lstrip(".").lower()

    @staticmethod
    def _is_google_family_cookie_host(host: str) -> bool:
        normalized = str(host or "").strip().lower()
        if not normalized:
            return True
        return (
            normalized == "google.com"
            or normalized == "www.google.com"
            or normalized.endswith(".google.com")
            or normalized == "recaptcha.net"
            or normalized == "www.recaptcha.net"
            or normalized.endswith(".recaptcha.net")
        )

    @classmethod
    def _build_personal_cookie_targets(cls, raw_cookie: Optional[str]) -> list[Dict[str, Any]]:
        """为 personal 内置浏览器构建 cookie 注入列表。

        说明：
        - 原始 Cookie 头没有 domain 元数据时，直接扩展到 labs/google/recaptcha 三个目标。
        - 即使 token.cookie 已经带有显式的 google.com 域，也额外镜像一份到
          `www.recaptcha.net`，保证 enterprise reload 首轮请求也能命中 cookie。
        - 对 google/recaptcha 镜像副本强制使用 `SameSite=None`，避免 labs.google
          场景下第三方 anchor/reload 请求继续丢 cookie。
        """
        browser_cookies = build_browser_cookie_targets(
            raw_cookie,
            fallback_urls=list(PERSONAL_COOKIE_TARGET_URLS),
        )
        if not browser_cookies:
            return []

        expanded: list[Dict[str, Any]] = []
        seen: set[str] = set()

        def append_cookie(cookie: Dict[str, Any]) -> None:
            stable_key = json.dumps(cookie, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if stable_key in seen:
                return
            seen.add(stable_key)
            expanded.append(cookie)

        for cookie in browser_cookies:
            scope_host = cls._extract_cookie_scope_host(cookie)
            explicit_domain = str(cookie.get("domain") or "").strip()
            google_family_scope = cls._is_google_family_cookie_host(scope_host)

            if explicit_domain or not google_family_scope:
                append_cookie(cookie)

            if not google_family_scope:
                continue

            mirrored_cookie = dict(cookie)
            mirrored_cookie.pop("domain", None)
            mirrored_cookie["path"] = str(mirrored_cookie.get("path") or "/").strip() or "/"
            mirrored_cookie["sameSite"] = "None"
            mirrored_cookie["secure"] = True
            for target_url in PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS:
                append_cookie({
                    **mirrored_cookie,
                    "url": target_url,
                })

        return expanded

    def _get_configured_browser_startup_cookie_text(self) -> Optional[str]:
        if not bool(getattr(config, "browser_startup_cookie_enabled", False)):
            return None
        cookie_text = normalize_cookie_storage_text(
            getattr(config, "browser_startup_cookie", "")
        )
        return cookie_text or None

    @classmethod
    def _build_configured_browser_cookie_targets(cls, raw_cookie: Optional[str]) -> list[Dict[str, Any]]:
        """构建系统级浏览器启动 cookie，确保 Google / reCAPTCHA 首跳都能命中。"""
        browser_cookies = build_browser_cookie_targets(
            raw_cookie,
            fallback_urls=list(PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS),
        )
        if not browser_cookies:
            return []

        expanded: list[Dict[str, Any]] = []
        seen: set[str] = set()

        def append_cookie(cookie: Dict[str, Any]) -> None:
            stable_key = json.dumps(cookie, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if stable_key in seen:
                return
            seen.add(stable_key)
            expanded.append(cookie)

        for cookie in browser_cookies:
            scope_host = cls._extract_cookie_scope_host(cookie)
            explicit_domain = str(cookie.get("domain") or "").strip()
            google_family_scope = cls._is_google_family_cookie_host(scope_host) or scope_host == "labs.google"

            if explicit_domain or not google_family_scope:
                append_cookie(cookie)

            if not google_family_scope:
                continue

            mirrored_cookie = dict(cookie)
            mirrored_cookie.pop("domain", None)
            mirrored_cookie["path"] = str(mirrored_cookie.get("path") or "/").strip() or "/"
            mirrored_cookie["sameSite"] = "None"
            mirrored_cookie["secure"] = True
            for target_url in PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS:
                append_cookie({
                    **mirrored_cookie,
                    "url": target_url,
                })

        return expanded

    async def _load_token_cookie(self, token_id: Optional[int]) -> Optional[str]:
        token_key = self._normalize_token_key(token_id)
        if not token_key or not self.db:
            return None
        try:
            token = await self.db.get_token(int(token_key))
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取 token cookie 失败 (token_id={token_key}): {e}")
            return None
        cookie_text = str(getattr(token, "cookie", "") or "").strip() if token else ""
        return cookie_text or None

    def _build_cdp_cookie_params(self, browser_cookies: Iterable[Dict[str, Any]]) -> list[Any]:
        from nodriver import cdp

        cookie_params: list[Any] = []
        for cookie in browser_cookies or []:
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue

            cookie_kwargs: Dict[str, Any] = {
                "name": name,
                "value": str(cookie.get("value") or ""),
            }

            url = str(cookie.get("url") or "").strip()
            domain = str(cookie.get("domain") or "").strip()
            path = str(cookie.get("path") or "/").strip() or "/"

            if url:
                cookie_kwargs["url"] = url
            elif domain:
                cookie_kwargs["domain"] = domain
                cookie_kwargs["path"] = path
            else:
                cookie_kwargs["url"] = "https://labs.google/"
                cookie_kwargs["path"] = path

            if "secure" in cookie:
                cookie_kwargs["secure"] = bool(cookie.get("secure"))
            if "httpOnly" in cookie:
                cookie_kwargs["http_only"] = bool(cookie.get("httpOnly"))

            same_site = self._normalize_cdp_same_site(cookie.get("sameSite"))
            if same_site is not None:
                cookie_kwargs["same_site"] = same_site

            expires = cookie.get("expires")
            if expires not in (None, ""):
                try:
                    cookie_kwargs["expires"] = cdp.network.TimeSinceEpoch.from_json(float(expires))
                except Exception:
                    pass

            cookie_params.append(cdp.network.CookieParam(**cookie_kwargs))

        return cookie_params

    async def _set_browser_cookie_targets(
        self,
        browser_cookies: Iterable[Dict[str, Any]],
        *,
        label: str,
        browser_context_id: Any = None,
        timeout_seconds: float = 8.0,
    ) -> int:
        if not self.browser:
            raise RuntimeError("browser runtime unavailable")

        cookie_params = self._build_cdp_cookie_params(browser_cookies)
        if not cookie_params:
            return 0

        from nodriver import cdp

        if browser_context_id is None:
            cookie_command = cdp.storage.set_cookies(cookie_params)
        else:
            cookie_command = cdp.storage.set_cookies(
                cookie_params,
                browser_context_id=browser_context_id,
            )

        await self._run_with_timeout(
            self.browser.connection.send(cookie_command),
            timeout_seconds=timeout_seconds,
            label=label,
        )
        return len(cookie_params)

    async def _apply_configured_browser_startup_cookie(
        self,
        *,
        label: str,
        browser_context_id: Any = None,
        tab=None,
    ) -> bool:
        cookie_text = self._get_configured_browser_startup_cookie_text()
        if not cookie_text:
            return False

        browser_cookies = self._build_configured_browser_cookie_targets(cookie_text)
        if not browser_cookies:
            raise RuntimeError("browser startup cookie enabled but no valid cookie targets were produced")

        cookie_count = await self._set_browser_cookie_targets(
            browser_cookies,
            label=f"storage.set_cookies:{label}:startup_cookie",
            browser_context_id=browser_context_id,
            timeout_seconds=8.0,
        )
        if cookie_count <= 0:
            raise RuntimeError("browser startup cookie enabled but no valid cookie params were produced")

        target_id = getattr(tab, "target_id", None)
        debug_logger.log_info(
            "[BrowserCaptcha] 已注入系统浏览器启动 Cookie "
            f"(label={label}, context={browser_context_id is not None}, target={target_id or '<none>'}, "
            f"cookies={cookie_count})"
        )
        return True

    @staticmethod
    def _normalize_cdp_same_site(value: Any):
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        try:
            from nodriver import cdp

            mapping = {
                "strict": cdp.network.CookieSameSite.STRICT,
                "lax": cdp.network.CookieSameSite.LAX,
                "none": cdp.network.CookieSameSite.NONE,
            }
            return mapping.get(raw)
        except Exception:
            return None

    @staticmethod
    def _normalize_cookie_same_site_text(value: Any) -> Optional[str]:
        raw = str(getattr(value, "name", value) or "").strip()
        if not raw:
            return None
        raw = raw.split(".")[-1].strip().lower()
        if raw == "strict":
            return "Strict"
        if raw == "lax":
            return "Lax"
        if raw == "none":
            return "None"
        return None

    @classmethod
    def _serialize_browser_cookie_for_storage(cls, cookie: Any) -> Optional[Dict[str, Any]]:
        if isinstance(cookie, dict):
            source = dict(cookie)
        else:
            source = {}
            for field in (
                "name",
                "value",
                "domain",
                "url",
                "path",
                "secure",
                "httpOnly",
                "http_only",
                "sameSite",
                "same_site",
                "expires",
            ):
                if hasattr(cookie, field):
                    source[field] = getattr(cookie, field)

        name = str(source.get("name") or "").strip()
        if not name:
            return None

        serialized: Dict[str, Any] = {
            "name": name,
            "value": str(source.get("value") or ""),
        }

        domain = str(source.get("domain") or "").strip()
        url = str(source.get("url") or "").strip()
        path = str(source.get("path") or "/").strip() or "/"

        if url:
            serialized["url"] = url
        elif domain:
            serialized["domain"] = domain
            serialized["path"] = path
        else:
            serialized["url"] = "https://labs.google/"
            serialized["path"] = path

        same_site = cls._normalize_cookie_same_site_text(
            source.get("sameSite", source.get("same_site"))
        )
        if same_site:
            serialized["sameSite"] = same_site

        expires = source.get("expires")
        if expires not in (None, ""):
            if hasattr(expires, "to_json"):
                try:
                    expires = expires.to_json()
                except Exception:
                    pass
            elif hasattr(expires, "value"):
                expires = getattr(expires, "value", expires)
            try:
                serialized["expires"] = float(expires)
            except Exception:
                pass

        secure = source.get("secure")
        if secure is not None:
            serialized["secure"] = bool(secure)

        http_only = source.get("httpOnly", source.get("http_only"))
        if http_only is not None:
            serialized["httpOnly"] = bool(http_only)

        if name.startswith("__Secure-") or name.startswith("__Host-"):
            serialized["secure"] = True

        if name.startswith("__Host-"):
            serialized.pop("domain", None)
            serialized["path"] = "/"
            if "url" not in serialized:
                serialized["url"] = "https://labs.google/"

        return serialized

    async def _persist_context_cookies_to_token(
        self,
        resident_info: Optional[ResidentTabInfo],
        token_id: Optional[int],
        *,
        label: str,
    ) -> bool:
        token_key = self._normalize_token_key(token_id)
        if resident_info is None or not resident_info.tab or not token_key or not self.db:
            return False

        browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(
            resident_info.tab
        )
        resident_info.browser_context_id = browser_context_id
        if browser_context_id is None:
            return False

        try:
            current_cookies = await self._get_browser_cookies(
                label=f"context_cookie_persist_get:{label}",
                browser_context_id=browser_context_id,
            )
            serialized_cookies = [
                normalized_cookie
                for normalized_cookie in (
                    self._serialize_browser_cookie_for_storage(cookie)
                    for cookie in (current_cookies or [])
                )
                if normalized_cookie
            ]
            if not serialized_cookies:
                return False

            previous_cookie_text = await self._load_token_cookie(int(token_key))
            merged_cookie_text = merge_browser_cookie_payloads(previous_cookie_text, serialized_cookies)
            if not merged_cookie_text:
                return False

            previous_storage_text = normalize_cookie_storage_text(previous_cookie_text)
            merged_signature = self._normalize_cookie_signature(merged_cookie_text)

            if previous_storage_text != merged_cookie_text:
                await self.db.update_token(int(token_key), cookie=merged_cookie_text)
                debug_logger.log_info(
                    f"[BrowserCaptcha] 已回填 context cookies 到 token.cookie "
                    f"(slot={resident_info.slot_id}, token_id={token_key}, cookies={len(serialized_cookies)})"
                )
            else:
                debug_logger.log_info(
                    f"[BrowserCaptcha] context cookies 与 token.cookie 一致，跳过写回 "
                    f"(slot={resident_info.slot_id}, token_id={token_key}, cookies={len(serialized_cookies)})"
                )

            if resident_info.token_id == int(token_key):
                resident_info.cookie_signature = merged_signature

            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 回填 context cookies 到 token.cookie 失败 "
                f"(slot={resident_info.slot_id}, token_id={token_key}): {e}"
            )
            return False

    async def _apply_token_cookie_binding(
        self,
        resident_info: Optional[ResidentTabInfo],
        token_id: Optional[int],
        *,
        label: str,
        force: bool = False,
        acquire_lock: bool = True,
    ) -> bool:
        token_key = self._normalize_token_key(token_id)
        if resident_info is None or not resident_info.tab or not token_key:
            return False

        cookie_text = await self._load_token_cookie(int(token_key))
        cookie_signature = self._normalize_cookie_signature(cookie_text)

        if not cookie_signature:
            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            resident_info.cookie_signature = None
            return False

        if (
            not force
            and resident_info.token_id == int(token_key)
            and resident_info.cookie_signature == cookie_signature
        ):
            return True

        browser_cookies = self._build_personal_cookie_targets(cookie_text)
        if not browser_cookies:
            return False

        try:
            browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(resident_info.tab)
            resident_info.browser_context_id = browser_context_id

            async def apply_cookie_update():
                return await self._set_browser_cookie_targets(
                    browser_cookies,
                    label=f"storage.set_cookies:{label}:{token_key}",
                    browser_context_id=browser_context_id,
                    timeout_seconds=8.0,
                )

            cookie_count = 0
            if acquire_lock:
                async with resident_info.solve_lock:
                    cookie_count = await apply_cookie_update()
                    await self._tab_reload(
                        resident_info.tab,
                        label=f"resident_cookie_reload:{label}:{token_key}",
                    )
            else:
                cookie_count = await apply_cookie_update()
                await self._tab_reload(
                    resident_info.tab,
                    label=f"resident_cookie_reload:{label}:{token_key}",
                )

            if cookie_count <= 0:
                return False

            resident_info.token_id = int(token_key)
            resident_info.cookie_signature = cookie_signature
            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            debug_logger.log_info(
                f"[BrowserCaptcha] 已向 context 注入 cookie (slot={resident_info.slot_id}, token_id={token_key}, cookies={cookie_count})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 注入 token cookie 失败 (slot={resident_info.slot_id}, token_id={token_key}): {e}"
            )
            return False

    @staticmethod
    def _is_labs_bootstrap_url(url: str) -> bool:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return False

        try:
            parsed = urlparse(normalized_url)
        except Exception:
            return False

        host = str(parsed.netloc or "").strip().lower()
        path = str(parsed.path or "").strip().lower()
        return host == "labs.google" and path == "/fx/api/auth/providers"

    async def _open_labs_bootstrap_page(self, tab, *, label: str) -> bool:
        """在 cookie 绑定之后再首跳 labs.google，避免首轮 anchor/reload 丢 cookie。"""
        async def _describe_surface(stage: str) -> tuple[str, str]:
            current_url = ""
            ready_state = ""

            try:
                current_url = str(
                    await self._tab_evaluate(
                        tab,
                        "location.href || ''",
                        label=f"labs_bootstrap_surface_url:{label}:{stage}",
                        timeout_seconds=2.0,
                    )
                    or ""
                ).strip()
            except Exception:
                current_url = ""

            try:
                ready_state = str(
                    await self._tab_evaluate(
                        tab,
                        "document.readyState",
                        label=f"labs_bootstrap_surface_ready:{label}:{stage}",
                        timeout_seconds=2.0,
                    )
                    or ""
                ).strip().lower()
            except Exception:
                ready_state = ""

            return current_url, ready_state

        async def _confirm_labs_surface(reason: str, *, stage: str) -> bool:
            current_url, ready_state = await _describe_surface(stage)
            if self._is_labs_bootstrap_url(current_url) and ready_state in {"interactive", "complete"}:
                debug_logger.log_warning(
                    "[BrowserCaptcha] labs 引导页命令超时，但页面已落到目标地址 "
                    f"(label={label}, reason={reason}, url={current_url}, "
                    f"ready_state={ready_state or '<empty>'})"
                )
                return True

            debug_logger.log_warning(
                "[BrowserCaptcha] labs 引导页失败，页面未落到目标地址 "
                f"(label={label}, reason={reason}, url={current_url or '<empty>'}, "
                f"ready_state={ready_state or '<empty>'})"
            )
            return False

        # Log pre-navigation state
        pre_url, pre_ready = await _describe_surface("pre_navigate")
        debug_logger.log_info(
            f"[BrowserCaptcha] labs 引导页开始导航 "
            f"(label={label}, target={PERSONAL_LABS_BOOTSTRAP_URL}, "
            f"pre_url={pre_url or '<empty>'}, pre_ready={pre_ready or '<empty>'})"
        )

        try:
            await self._tab_get(
                tab,
                PERSONAL_LABS_BOOTSTRAP_URL,
                label=f"labs_bootstrap_get:{label}",
                timeout_seconds=self._navigation_timeout_seconds,
            )
        except Exception as e:
            if self._is_browser_runtime_error(e):
                self._mark_browser_health(False)
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 打开 labs 引导页时浏览器运行态断开 ({label}): {e}"
                )
                raise
            debug_logger.log_warning(
                f"[BrowserCaptcha] 打开 labs 引导页导航异常 ({label}): "
                f"{type(e).__name__}: {e}"
            )
            return await _confirm_labs_surface(str(e), stage="navigate_timeout")

        if not await self._wait_for_document_ready(tab, retries=20, interval_seconds=0.5):
            debug_logger.log_warning(f"[BrowserCaptcha] labs 引导页未按时 ready ({label})")
            return await _confirm_labs_surface("document_not_ready", stage="document_not_ready")

        current_url, ready_state = await _describe_surface("document_ready")
        if self._is_labs_bootstrap_url(current_url):
            debug_logger.log_info(
                "[BrowserCaptcha] 已进入 labs 引导页 "
                f"(label={label}, url={current_url}, ready_state={ready_state or '<empty>'})"
            )
            return True

        debug_logger.log_warning(
            "[BrowserCaptcha] labs 引导页 ready 后落点异常 "
            f"(label={label}, url={current_url or '<empty>'}, ready_state={ready_state or '<empty>'})"
        )
        return False

    async def _warmup_google_context_cookies(
        self,
        resident_info: Optional[ResidentTabInfo],
        *,
        label: str,
    ) -> bool:
        """访问一次 Google 首页，让当前 browser context 自行拿到额外站点 cookie。"""
        if resident_info is None or not resident_info.tab:
            return False

        browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(resident_info.tab)
        resident_info.browser_context_id = browser_context_id
        if browser_context_id is None:
            return False

        warmup_url = "https://www.google.com/"
        return_url = "https://labs.google/fx/api/auth/providers"

        try:
            before_cookies = await self._get_browser_cookies(
                label=f"google_warmup_get_cookies_before:{label}",
                browser_context_id=browser_context_id,
            )
            before_pairs = {
                self._extract_cookie_name_domain(cookie)
                for cookie in (before_cookies or [])
            }

            await self._tab_get(
                resident_info.tab,
                warmup_url,
                label=f"google_warmup_get:{label}",
                timeout_seconds=self._navigation_timeout_seconds,
            )
            if not await self._wait_for_document_ready(
                resident_info.tab,
                retries=20,
                interval_seconds=0.5,
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Google 预热页面未按时 ready (slot={resident_info.slot_id}, label={label})"
                )
            await resident_info.tab.sleep(1.0)

            after_cookies = await self._get_browser_cookies(
                label=f"google_warmup_get_cookies_after:{label}",
                browser_context_id=browser_context_id,
            )
            after_pairs = {
                self._extract_cookie_name_domain(cookie)
                for cookie in (after_cookies or [])
            }
            added_pairs = sorted(pair for pair in after_pairs if pair not in before_pairs)
            added_preview = ", ".join(
                f"{name}@{domain or '<host-only>'}"
                for name, domain in added_pairs[:6]
                if name
            )

            await self._tab_get(
                resident_info.tab,
                return_url,
                label=f"google_warmup_back_to_labs:{label}",
                timeout_seconds=self._navigation_timeout_seconds,
            )
            if not await self._wait_for_document_ready(
                resident_info.tab,
                retries=20,
                interval_seconds=0.5,
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Google 预热返回 labs 页面未按时 ready (slot={resident_info.slot_id}, label={label})"
                )

            await self._persist_context_cookies_to_token(
                resident_info,
                resident_info.token_id,
                label=f"{label}:persist",
            )

            debug_logger.log_info(
                f"[BrowserCaptcha] Google 预热完成 "
                f"(slot={resident_info.slot_id}, label={label}, cookies_before={len(before_pairs)}, "
                f"cookies_after={len(after_pairs)}, added={len(added_pairs)}, preview={added_preview or '<none>'})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Google 预热失败 (slot={resident_info.slot_id}, label={label}): {e}"
            )
            try:
                await self._tab_get(
                    resident_info.tab,
                    return_url,
                    label=f"google_warmup_recover_labs:{label}",
                    timeout_seconds=self._navigation_timeout_seconds,
                )
            except Exception:
                pass
            return False

    async def _ensure_resident_token_binding(
        self,
        resident_info: Optional[ResidentTabInfo],
        token_id: Optional[int],
        *,
        label: str,
    ) -> bool:
        token_key = self._normalize_token_key(token_id)
        if resident_info is None or not resident_info.tab:
            return False
        if not token_key:
            return True

        desired_cookie_text = await self._load_token_cookie(int(token_key))
        desired_cookie_signature = self._normalize_cookie_signature(desired_cookie_text)
        current_token_id = resident_info.token_id
        current_cookie_signature = resident_info.cookie_signature

        if (
            current_token_id == int(token_key)
            and current_cookie_signature == desired_cookie_signature
        ):
            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            return True

        if not desired_cookie_signature:
            if current_token_id == int(token_key) and not current_cookie_signature:
                self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
                resident_info.cookie_signature = None
                return True

            try:
                from nodriver import cdp

                browser_context_id = resident_info.browser_context_id or self._extract_tab_browser_context_id(resident_info.tab)
                resident_info.browser_context_id = browser_context_id

                async with resident_info.solve_lock:
                    if browser_context_id is None:
                        clear_cookie_command = cdp.storage.clear_cookies()
                    else:
                        clear_cookie_command = cdp.storage.clear_cookies(browser_context_id=browser_context_id)
                    await self._run_with_timeout(
                        self.browser.connection.send(
                            clear_cookie_command
                        ),
                        timeout_seconds=8.0,
                        label=f"storage.clear_cookies:{label}:{token_key}",
                    )
                    await self._tab_reload(
                        resident_info.tab,
                        label=f"resident_cookie_clear_reload:{label}:{token_key}",
                    )
                    resident_info.recaptcha_ready = False
                    if not await self._wait_for_document_ready(
                        resident_info.tab,
                        retries=30,
                        interval_seconds=0.5,
                    ):
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] token_id={token_key} 清空 context cookies 后页面未能按时 ready (slot={resident_info.slot_id})"
                        )
                        return False

                    resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
                    if not resident_info.recaptcha_ready:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] token_id={token_key} 清空 context cookies 后 reCAPTCHA 未恢复就绪 (slot={resident_info.slot_id})"
                        )
                        return False
            except Exception as e:
                resident_info.recaptcha_ready = False
                debug_logger.log_warning(
                    f"[BrowserCaptcha] token_id={token_key} 清空 context cookies 失败 (slot={resident_info.slot_id}): {e}"
                )
                return False

            self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
            resident_info.cookie_signature = None
            return True

        async with resident_info.solve_lock:
            binding_ok = await self._apply_token_cookie_binding(
                resident_info,
                int(token_key),
                label=label,
                force=True,
                acquire_lock=False,
            )
            if not binding_ok:
                resident_info.recaptcha_ready = False
                return False

            resident_info.recaptcha_ready = False
            if not await self._wait_for_document_ready(
                resident_info.tab,
                retries=30,
                interval_seconds=0.5,
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] token_id={token_key} cookie 注入后页面未能按时 ready (slot={resident_info.slot_id})"
                )
                return False

            resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
            if not resident_info.recaptcha_ready:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] token_id={token_key} cookie 注入后 reCAPTCHA 未恢复就绪 (slot={resident_info.slot_id})"
                )
                return False

        self._remember_token_affinity(int(token_key), resident_info.slot_id, resident_info)
        return True

    async def open_login_window(self):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize()
        self._mark_runtime_active()
        tab = await self._open_visible_browser_tab(
            "https://accounts.google.com/",
            label="open_login_window",
        )
        debug_logger.log_info("[BrowserCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    async def refresh_session_token(self, project_id: str, token_id: Optional[int] = None) -> Optional[str]:
        """从常驻标签页获取最新的 Session Token
        
        复用共享打码标签页，通过刷新页面并从 cookies 中提取
        __Secure-next-auth.session-token
        
        Args:
            project_id: 项目ID，用于定位常驻标签页
            
        Returns:
            新的 Session Token，如果获取失败返回 None
        """
        for attempt in range(2):
            self._mark_runtime_active()
            # 确保浏览器已初始化
            await self.initialize()

            start_time = time.time()
            debug_logger.log_info(
                f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id}, token_id={token_id}, attempt={attempt + 1})..."
            )

            async with self._resident_lock:
                slot_id, resident_info = self._resolve_resident_slot_for_project_locked(
                    project_id,
                    token_id=token_id,
                )

            if resident_info is None or not slot_id:
                slot_id, resident_info = await self._ensure_resident_tab(
                    project_id,
                    token_id=token_id,
                    return_slot_key=True,
                )

            if resident_info is None or not slot_id:
                if attempt == 0 and not await self._probe_browser_runtime():
                    await self._recover_browser_runtime(project_id, reason="refresh_session_prepare")
                    continue
                debug_logger.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 获取共享常驻标签页")
                return None

            if not resident_info or not resident_info.tab:
                debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
                return None

            if not await self._ensure_resident_token_binding(
                resident_info,
                token_id,
                label=f"refresh_session:{slot_id}",
            ):
                debug_logger.log_warning(
                    f"[BrowserCaptcha] 刷新 Session Token 前 cookie 绑定未就绪，尝试重建 (slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                if not resident_info or not slot_id or not resident_info.tab:
                    if attempt == 0 and not await self._probe_browser_runtime():
                        await self._recover_browser_runtime(project_id, reason="refresh_session_rebuild_cookie_binding")
                        continue
                    return None

            tab = resident_info.tab

            try:
                async with resident_info.solve_lock:
                    # 刷新页面以获取最新的 cookies
                    debug_logger.log_info(f"[BrowserCaptcha] 刷新常驻标签页以获取最新 cookies...")
                    resident_info.recaptcha_ready = False
                    await self._run_with_timeout(
                        self._tab_reload(
                            tab,
                            label=f"refresh_session_reload:{slot_id}",
                        ),
                        timeout_seconds=self._session_refresh_timeout_seconds,
                        label=f"refresh_session_reload_total:{slot_id}",
                    )

                    # 等待页面加载完成
                    for _ in range(30):
                        await asyncio.sleep(1)
                        try:
                            ready_state = await self._tab_evaluate(
                                tab,
                                "document.readyState",
                                label=f"refresh_session_ready_state:{slot_id}",
                                timeout_seconds=2.0,
                            )
                            if ready_state == "complete":
                                break
                        except Exception:
                            pass

                    resident_info.recaptcha_ready = await self._wait_for_recaptcha(tab)
                    if not resident_info.recaptcha_ready:
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] 刷新 Session Token 后 reCAPTCHA 未恢复就绪 (slot={slot_id})"
                        )

                    # 额外等待确保 cookies 已设置
                    await asyncio.sleep(2)

                    # 从 cookies 中提取 __Secure-next-auth.session-token
                    session_token = None

                    try:
                        cookies = await self._get_browser_cookies(
                            label=f"refresh_session_get_cookies:{slot_id}",
                            browser_context_id=resident_info.browser_context_id,
                        )

                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                session_token = cookie.value
                                break

                    except Exception as e:
                        debug_logger.log_warning(f"[BrowserCaptcha] 通过 cookies API 获取失败: {e}，尝试从 document.cookie 获取...")

                        try:
                            all_cookies = await self._tab_evaluate(
                                tab,
                                "document.cookie",
                                label=f"refresh_session_document_cookie:{slot_id}",
                            )
                            if all_cookies:
                                for part in all_cookies.split(";"):
                                    part = part.strip()
                                    if part.startswith("__Secure-next-auth.session-token="):
                                        session_token = part.split("=", 1)[1]
                                        break
                        except Exception as e2:
                            debug_logger.log_error(f"[BrowserCaptcha] document.cookie 获取失败: {e2}")

                duration_ms = (time.time() - start_time) * 1000

                if session_token:
                    resident_info.last_used_at = time.time()
                    self._remember_project_affinity(project_id, slot_id, resident_info)
                    self._remember_token_affinity(token_id, slot_id, resident_info)
                    self._resident_error_streaks.pop(slot_id, None)
                    self._mark_browser_health(True)
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ Session Token 获取成功（耗时 {duration_ms:.0f}ms）")
                    return session_token

                debug_logger.log_error(f"[BrowserCaptcha] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None

            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 刷新 Session Token 异常: {str(e)}")

                if attempt == 0 and self._is_browser_runtime_error(e):
                    if await self._recover_browser_runtime(project_id, reason=f"refresh_session:{slot_id}"):
                        continue

                slot_id, resident_info = await self._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                if resident_info and slot_id:
                    try:
                        async with resident_info.solve_lock:
                            cookies = await self._get_browser_cookies(
                                label=f"refresh_session_get_cookies_after_rebuild:{slot_id}",
                                browser_context_id=resident_info.browser_context_id,
                            )
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                resident_info.last_used_at = time.time()
                                self._remember_project_affinity(project_id, slot_id, resident_info)
                                self._remember_token_affinity(token_id, slot_id, resident_info)
                                self._resident_error_streaks.pop(slot_id, None)
                                self._mark_browser_health(True)
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Session Token 获取成功")
                                return cookie.value
                    except Exception as rebuild_error:
                        if attempt == 0 and self._is_browser_runtime_error(rebuild_error):
                            if await self._recover_browser_runtime(project_id, reason=f"refresh_session_rebuild:{slot_id}"):
                                continue

                return None

        return None

    async def _clear_tab_site_storage(self, tab) -> Dict[str, Any]:
        """清理当前站点的本地存储状态，但保留 cookies 登录态。"""
        result = await self._tab_evaluate(tab, """
            (async () => {
                const summary = {
                    local_storage_cleared: false,
                    session_storage_cleared: false,
                    cache_storage_deleted: [],
                    indexed_db_deleted: [],
                    indexed_db_errors: [],
                    service_worker_unregistered: 0,
                };

                try {
                    window.localStorage.clear();
                    summary.local_storage_cleared = true;
                } catch (e) {
                    summary.local_storage_error = String(e);
                }

                try {
                    window.sessionStorage.clear();
                    summary.session_storage_cleared = true;
                } catch (e) {
                    summary.session_storage_error = String(e);
                }

                try {
                    if (typeof caches !== 'undefined') {
                        const cacheKeys = await caches.keys();
                        for (const key of cacheKeys) {
                            const deleted = await caches.delete(key);
                            if (deleted) {
                                summary.cache_storage_deleted.push(key);
                            }
                        }
                    }
                } catch (e) {
                    summary.cache_storage_error = String(e);
                }

                try {
                    if (navigator.serviceWorker) {
                        const registrations = await navigator.serviceWorker.getRegistrations();
                        for (const registration of registrations) {
                            const ok = await registration.unregister();
                            if (ok) {
                                summary.service_worker_unregistered += 1;
                            }
                        }
                    }
                } catch (e) {
                    summary.service_worker_error = String(e);
                }

                try {
                    if (typeof indexedDB !== 'undefined' && typeof indexedDB.databases === 'function') {
                        const dbs = await indexedDB.databases();
                        const names = Array.from(new Set(
                            dbs
                                .map((item) => item && item.name)
                                .filter((name) => typeof name === 'string' && name)
                        ));
                        for (const name of names) {
                            try {
                                await new Promise((resolve) => {
                                    const request = indexedDB.deleteDatabase(name);
                                    request.onsuccess = () => resolve(true);
                                    request.onerror = () => resolve(false);
                                    request.onblocked = () => resolve(false);
                                });
                                summary.indexed_db_deleted.push(name);
                            } catch (e) {
                                summary.indexed_db_errors.push(`${name}: ${String(e)}`);
                            }
                        }
                    } else {
                        summary.indexed_db_unsupported = true;
                    }
                } catch (e) {
                    summary.indexed_db_errors.push(String(e));
                }

                return summary;
            })()
        """, label="clear_tab_site_storage", timeout_seconds=15.0)
        return result if isinstance(result, dict) else {}

    async def _clear_browser_cache(self):
        """清理浏览器全部缓存"""
        if not self.browser:
            return

        try:
            from nodriver import cdp

            debug_logger.log_info("[BrowserCaptcha] 开始清理浏览器缓存...")

            # 使用 Chrome DevTools Protocol 清理缓存
            # 清理所有类型的缓存数据
            await self._browser_send_command(
                cdp.network.clear_browser_cache(),
                label="clear_browser_cache",
            )

            # 清理 Cookies
            await self._browser_send_command(
                cdp.network.clear_browser_cookies(),
                label="clear_browser_cookies",
            )

            # 清理关键站点存储数据（localStorage, sessionStorage, IndexedDB, SW 等）
            origins = (
                "https://www.google.com",
                "https://www.recaptcha.net",
                "https://labs.google",
            )
            for origin in origins:
                try:
                    await self._browser_send_command(
                        cdp.storage.clear_data_for_origin(
                            origin=origin,
                            storage_types="all",
                        ),
                        label=f"clear_browser_origin_storage:{origin}",
                    )
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] 清理 origin 存储失败: origin={origin}, error={e}"
                    )

            debug_logger.log_info("[BrowserCaptcha] ✅ 浏览器缓存已清理")

        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 清理缓存时异常: {e}")

