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
# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return True
    # 方法2: 检查 cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """判断环境变量是否为 true。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED

RECAPTCHA_SCRIPT_CACHE_TTL_SECONDS = 86400
RECAPTCHA_SCRIPT_DOWNLOAD_TIMEOUT_SECONDS = 20
RECAPTCHA_ASSET_CACHE_TTL_SECONDS = 86400
RECAPTCHA_CACHE_CLEANUP_MAX_AGE_SECONDS = max(
    RECAPTCHA_SCRIPT_CACHE_TTL_SECONDS,
    RECAPTCHA_ASSET_CACHE_TTL_SECONDS,
) * 3
PERSONAL_RUNTIME_PROFILE_STALE_TTL_SECONDS = 6 * 60 * 60
PERSONAL_PROXY_EXTENSION_STALE_TTL_SECONDS = 6 * 60 * 60
RECAPTCHA_REMOTE_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\)]+", re.IGNORECASE)
RECAPTCHA_CSS_URL_PATTERN = re.compile(r"url\((.*?)\)", re.IGNORECASE)
RECAPTCHA_STATIC_EXTENSIONS = {
    ".js",
    ".css",
    ".png",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
}
RECAPTCHA_STATIC_HOST_ALIASES = {
    "www.gstatic.com": ("www.gstatic.com", "www.gstatic.cn"),
    "www.gstatic.cn": ("www.gstatic.com", "www.gstatic.cn"),
}


def _path_mtime_age_seconds(path: Path, now_value: float) -> Optional[float]:
    try:
        return max(0.0, now_value - float(path.stat().st_mtime))
    except Exception:
        return None


def _remove_path_quietly(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _cleanup_runtime_artifacts_sync(
    *,
    active_runtime_paths: set[str],
    active_proxy_extension_paths: set[str],
) -> dict[str, int]:
    stats = {
        "profiles_deleted": 0,
        "recaptcha_cache_deleted": 0,
        "proxy_extensions_deleted": 0,
    }
    now_value = time.time()
    normalized_active_runtime_paths = {
        os.path.normcase(os.path.normpath(item))
        for item in active_runtime_paths
        if str(item or "").strip()
    }
    normalized_active_proxy_paths = {
        os.path.normcase(os.path.normpath(item))
        for item in active_proxy_extension_paths
        if str(item or "").strip()
    }

    try:
        if PERSONAL_RUNTIME_TMP_DIR.exists():
            for child in PERSONAL_RUNTIME_TMP_DIR.iterdir():
                child_name = child.name
                normalized_child = os.path.normcase(os.path.normpath(str(child)))
                if child_name.startswith(("browser_profile_", "fresh_browser_profile_", "launch_retry_profile_")):
                    if normalized_child in normalized_active_runtime_paths:
                        continue
                    age_seconds = _path_mtime_age_seconds(child, now_value)
                    if age_seconds is not None and age_seconds >= PERSONAL_RUNTIME_PROFILE_STALE_TTL_SECONDS:
                        if _remove_path_quietly(child):
                            stats["profiles_deleted"] += 1
                elif child_name in {"recaptcha_js", "recaptcha_assets"} and child.is_dir():
                    for cache_file in child.iterdir():
                        if not cache_file.is_file():
                            continue
                        age_seconds = _path_mtime_age_seconds(cache_file, now_value)
                        if age_seconds is None or age_seconds < RECAPTCHA_CACHE_CLEANUP_MAX_AGE_SECONDS:
                            continue
                        if _remove_path_quietly(cache_file):
                            stats["recaptcha_cache_deleted"] += 1
    except Exception:
        pass

    temp_root = Path(tempfile.gettempdir())
    try:
        if temp_root.exists():
            for child in temp_root.iterdir():
                if not child.is_dir() or not child.name.startswith("nodriver_proxy_auth_"):
                    continue
                normalized_child = os.path.normcase(os.path.normpath(str(child)))
                if normalized_child in normalized_active_proxy_paths:
                    continue
                age_seconds = _path_mtime_age_seconds(child, now_value)
                if age_seconds is None or age_seconds < PERSONAL_PROXY_EXTENSION_STALE_TTL_SECONDS:
                    continue
                if _remove_path_quietly(child):
                    stats["proxy_extensions_deleted"] += 1
    except Exception:
        pass

    return stats


# ==================== nodriver 自动安装 ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令
    
    Args:
        package: 包名
        use_mirror: 是否使用国内镜像
    
    Returns:
        是否安装成功
    """
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] 正在安装 {package}...")
        print(f"[BrowserCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} 安装成功")
            print(f"[BrowserCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装异常: {e}")
        return False


def _ensure_nodriver_installed() -> bool:
    """确保 nodriver 已安装
    
    Returns:
        是否安装成功/已安装
    """
    try:
        import nodriver
        debug_logger.log_info("[BrowserCaptcha] nodriver 已安装")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] nodriver 未安装，开始自动安装...")
    print("[BrowserCaptcha] nodriver 未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_pip_install('nodriver', use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('nodriver', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ nodriver 自动安装失败，请手动安装: pip install nodriver")
    print("[BrowserCaptcha] ❌ nodriver 自动安装失败，请手动安装: pip install nodriver")
    return False


def _read_windows_app_path(executable_name: str) -> Optional[str]:
    """读取 Windows App Paths 中注册的浏览器路径。"""
    if os.name != "nt":
        return None

    try:
        import winreg
    except Exception:
        return None

    key_candidates = [
        rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}",
        rf"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}",
    ]

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_path in key_candidates:
            try:
                with winreg.OpenKey(root, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    resolved = str(value or "").strip().strip('"')
                    if resolved and os.path.exists(resolved):
                        return os.path.normpath(resolved)
            except Exception:
                continue
    return None


def _detect_real_browser_executable_path() -> Optional[str]:
    """尽量探测本机已安装的真实 Chromium 浏览器，避免交给 nodriver 自行弹选择。"""
    if os.name != "nt":
        linux_browser_candidates = [
            (
                "Google Chrome",
                [
                    shutil.which("google-chrome"),
                    shutil.which("google-chrome-stable"),
                    "/usr/bin/google-chrome",
                    "/usr/bin/google-chrome-stable",
                ],
            ),
            (
                "Microsoft Edge",
                [
                    shutil.which("microsoft-edge"),
                    shutil.which("microsoft-edge-stable"),
                    "/usr/bin/microsoft-edge",
                    "/usr/bin/microsoft-edge-stable",
                ],
            ),
            (
                "Brave",
                [
                    shutil.which("brave-browser"),
                    shutil.which("brave"),
                    "/usr/bin/brave-browser",
                    "/usr/bin/brave",
                ],
            ),
            (
                "Chromium",
                [
                    shutil.which("chromium"),
                    shutil.which("chromium-browser"),
                    "/usr/bin/chromium",
                    "/usr/bin/chromium-browser",
                ],
            ),
        ]
        for browser_name, candidates in linux_browser_candidates:
            for candidate in candidates:
                resolved = str(candidate or "").strip().strip('"')
                if not resolved or not os.path.exists(resolved):
                    continue
                normalized = os.path.normpath(resolved)
                debug_logger.log_info(
                    f"[BrowserCaptcha] 自动检测到真实浏览器 {browser_name}: {normalized}"
                )
                return normalized
        return None

    browser_candidates = [
        (
            "Google Chrome",
            "chrome.exe",
            [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ],
        ),
        (
            "Microsoft Edge",
            "msedge.exe",
            [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
        ),
        (
            "Brave",
            "brave.exe",
            [
                os.path.join(
                    os.environ.get("LOCALAPPDATA", ""),
                    "BraveSoftware",
                    "Brave-Browser",
                    "Application",
                    "brave.exe",
                ),
                os.path.join(
                    os.environ.get("PROGRAMFILES", ""),
                    "BraveSoftware",
                    "Brave-Browser",
                    "Application",
                    "brave.exe",
                ),
                os.path.join(
                    os.environ.get("PROGRAMFILES(X86)", ""),
                    "BraveSoftware",
                    "Brave-Browser",
                    "Application",
                    "brave.exe",
                ),
            ],
        ),
        (
            "Chromium",
            "chrome.exe",
            [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Chromium", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Chromium", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Chromium", "Application", "chrome.exe"),
            ],
        ),
    ]

    for browser_name, executable_name, candidate_paths in browser_candidates:
        detected_candidates = [
            shutil.which(executable_name),
            _read_windows_app_path(executable_name),
            *candidate_paths,
        ]
        for candidate in detected_candidates:
            resolved = str(candidate or "").strip().strip('"')
            if not resolved or not os.path.exists(resolved):
                continue
            normalized = os.path.normpath(resolved)
            debug_logger.log_info(
                f"[BrowserCaptcha] 自动检测到真实浏览器 {browser_name}: {normalized}"
            )
            return normalized

    return None


def _resolve_browser_executable_path() -> tuple[Optional[str], str]:
    """解析浏览器优先级：环境变量 > auto。"""
    browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
    if browser_executable_path and not os.path.exists(browser_executable_path):
        debug_logger.log_warning(
            f"[BrowserCaptcha] 指定浏览器不存在，改回 nodriver 默认浏览器解析: {browser_executable_path}"
        )
        browser_executable_path = None

    if browser_executable_path:
        normalized = os.path.normpath(browser_executable_path)
        debug_logger.log_info(f"[BrowserCaptcha] 使用环境变量指定浏览器: {normalized}")
        return normalized, "configured"

    return None, "auto"


def _build_personal_browser_args(
    *,
    headless: bool,
    proxy_server_arg: Optional[str] = None,
    proxy_extension_dir: Optional[str] = None,
) -> list[str]:
    """构建 personal 模式浏览器启动参数。

    说明：
    - 始终依赖独立临时 user-data-dir，避免污染系统真实资料。
    - 显式去掉 `--profile-directory=Default` 这种容易误导的配置。
    - 显式加 `--no-startup-window`，避免 Chrome 先弹一个默认普通窗口。
    - 仅在未加载代理认证扩展时附加 `--incognito`，避免扩展在无痕窗口中失效。
    """
    browser_args = [
        '--disable-quic',
        '--disable-features=UseDnsHttpsSvcb,OptimizationHints,AutofillServerCommunication,CertificateTransparencyComponentUpdater,MediaRouter,GlobalMediaControls',
        '--disable-dev-shm-usage',
        '--disable-setuid-sandbox',
        '--disable-breakpad',
        '--disable-client-side-phishing-detection',
        '--disable-gpu',
        '--disable-infobars',
        '--hide-scrollbars',
        '--window-size=1280,720',
        '--disable-background-networking',
        '--disable-component-update',
        '--disable-domain-reliability',
        '--disable-sync',
        '--disable-translate',
        '--disable-default-apps',
        '--metrics-recording-only',
        '--mute-audio',
        '--safebrowsing-disable-auto-update',
        '--no-first-run',
        '--no-default-browser-check',
        '--no-zygote',
    ]

    if headless:
        # Stealth hidden: chạy headed nhưng ẩn cửa sổ hoàn toàn
        # KHÔNG dùng --headless/--no-startup-window để tránh HeadlessChrome UA
        # Google reCAPTCHA sẽ reject nếu phát hiện HeadlessChrome
        # Vẫn giữ nguyên kích thước và vị trí bình thường để tránh BotGuard phát hiện
        browser_args.append('--window-position=40,40')
        browser_args.append('--window-size=1280,720')
    else:
        browser_args.append('--window-position=80,80')

    if proxy_server_arg:
        browser_args.append(proxy_server_arg)

    if proxy_extension_dir:
        # 代理认证扩展在 bwsi/incognito 风格会话下容易失效，保持临时 profile 即可满足隔离需求。
        browser_args.append(f'--load-extension={proxy_extension_dir}')
    else:
        browser_args.append('--bwsi')
        browser_args.append('--disable-extensions')
        browser_args.append('--incognito')

    return browser_args


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_browser_launch_parallelism_limit() -> int:
    default_limit = 1 if os.name == "nt" else 2
    raw_value = os.environ.get("PERSONAL_BROWSER_LAUNCH_PARALLELISM", "").strip()
    try:
        return max(1, min(4, int(raw_value or default_limit)))
    except Exception:
        return default_limit


def _resolve_personal_browser_sandbox_enabled() -> bool:
    """尽量沿用真实浏览器默认沙箱；仅在 root/显式禁用时关闭。"""
    if _env_truthy("PERSONAL_BROWSER_DISABLE_SANDBOX"):
        return False
    if _env_truthy("PERSONAL_BROWSER_FORCE_SANDBOX"):
        return True
    if os.name != "posix":
        return True
    if hasattr(os, "geteuid"):
        try:
            return os.geteuid() != 0
        except Exception:
            return False
    return False


def _normalize_personal_browser_args_for_launch(
    browser_args: list[str],
    *,
    sandbox_enabled: bool,
) -> list[str]:
    normalized_args: list[str] = []
    for arg in browser_args:
        if sandbox_enabled and arg in {"--disable-setuid-sandbox", "--no-sandbox"}:
            continue
        normalized_args.append(arg)
    return normalized_args


def _tune_personal_browser_args_for_docker_headed(
    browser_args: list[str],
) -> list[str]:
    """Make Docker headed Chromium look closer to a regular desktop session."""
    removable_exact = {
        '--disable-dev-shm-usage',
        '--disable-setuid-sandbox',
        '--disable-gpu',
        '--disable-infobars',
        '--hide-scrollbars',
        '--disable-background-networking',
        '--disable-sync',
        '--disable-translate',
        '--disable-default-apps',
        '--bwsi',
        '--incognito',
        '--disable-extensions',
        '--no-zygote',
    }
    removable_prefixes = (
        '--window-size=',
        '--window-position=',
        '--lang=',
        '--use-gl=',
        '--ozone-platform=',
        '--password-store=',
    )

    tuned_args: list[str] = []
    for arg in browser_args:
        if arg in removable_exact:
            continue
        if any(arg.startswith(prefix) for prefix in removable_prefixes):
            continue
        tuned_args.append(arg)

    tuned_args.extend([
        '--window-size=1366,768',
        '--window-position=40,40',
        '--lang=zh-CN',
        '--password-store=basic',
        '--ozone-platform=x11',
        '--use-gl=swiftshader',
    ])
    return tuned_args


# 尝试导入 nodriver
uc = None
NODRIVER_AVAILABLE = False
_NODRIVER_RUNTIME_PATCHED = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] 检测到 Docker 环境，默认禁用内置浏览器打码。"
        "如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true。"
        "personal 模式默认支持无头，不强制依赖 DISPLAY/虚拟显示。"
    )
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，默认禁用内置浏览器打码")
    print("[BrowserCaptcha] 如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker 内置浏览器打码白名单已启用，personal 模式将按 headless 配置决定是否需要 DISPLAY/虚拟显示"
        )
        print("[BrowserCaptcha] ✅ Docker 内置浏览器打码白名单已启用")
    if _ensure_nodriver_installed():
        try:
            import nodriver as uc
            NODRIVER_AVAILABLE = True
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] nodriver 导入失败: {e}")
            print(f"[BrowserCaptcha] ❌ nodriver 导入失败: {e}")


_RUNTIME_ERROR_KEYWORDS = (
    "has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
    "connection lost",
    "connection refused",
    "connection reset",
    "broken pipe",
    "session closed",
    "not attached to an active page",
    "no session with given id",
    "cannot find context with specified id",
    "websocket is not open",
    "no close frame received or sent",
    "cannot call write to closing transport",
    "cannot write to closing transport",
    "cannot call send once a close message has been sent",
    "connectionclosederror",
    "connectionrefusederror",
    "disconnected",
    "errno 111",
)

_NORMAL_CLOSE_KEYWORDS = (
    "connectionclosedok",
    "normal closure",
    "normal_closure",
    "sent 1000 (ok)",
    "received 1000 (ok)",
    "close(code=1000",
)


def _flatten_exception_text(error: Any) -> str:
    """拼接异常链文本，便于统一识别 nodriver 运行态断连。"""
    visited: set[int] = set()
    pending = [error]
    parts: list[str] = []

    while pending:
        current = pending.pop()
        if current is None:
            continue

        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        parts.append(type(current).__name__)

        message = str(current or "").strip()
        if message:
            parts.append(message)

        args = getattr(current, "args", None)
        if isinstance(args, tuple):
            for arg in args:
                arg_text = str(arg or "").strip()
                if arg_text:
                    parts.append(arg_text)

        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))

    return " | ".join(parts).lower()


def _is_runtime_disconnect_error(error: Any) -> bool:
    """识别浏览器 / websocket 运行态断连。"""
    error_text = _flatten_exception_text(error)
    if not error_text:
        return False
    return any(keyword in error_text for keyword in _RUNTIME_ERROR_KEYWORDS) or any(
        keyword in error_text for keyword in _NORMAL_CLOSE_KEYWORDS
    )


def _is_runtime_normal_close_error(error: Any) -> bool:
    """识别 websocket 正常关闭（1000）这类预期退场。"""
    error_text = _flatten_exception_text(error)
    if not error_text:
        return False
    return any(keyword in error_text for keyword in _NORMAL_CLOSE_KEYWORDS)


def _finalize_nodriver_send_task(connection, transaction, tx_id: int, task: asyncio.Task):
    """回收 nodriver websocket.send 的后台异常，避免事件循环打印未检索 task 错误。"""
    try:
        task.result()
    except asyncio.CancelledError:
        connection.mapper.pop(tx_id, None)
        if not transaction.done():
            transaction.cancel()
    except Exception as e:
        connection.mapper.pop(tx_id, None)
        if not transaction.done():
            try:
                transaction.set_exception(e)
            except Exception:
                pass

        if _is_runtime_normal_close_error(e):
            debug_logger.log_info(
                f"[BrowserCaptcha] nodriver websocket 在正常关闭后退出: {type(e).__name__}: {e}"
            )
        elif _is_runtime_disconnect_error(e):
            debug_logger.log_warning(
                f"[BrowserCaptcha] nodriver websocket 发送在断连后退出: {type(e).__name__}: {e}"
            )
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] nodriver websocket 发送异常: {type(e).__name__}: {e}"
            )


def _patch_nodriver_connection_instance(connection_instance):
    """在连接实例级别收口 websocket.send 的后台异常。"""
    if not connection_instance or getattr(connection_instance, "_flow2api_send_patched", False):
        return

    try:
        from nodriver.core import connection as nodriver_connection_module
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] 加载 nodriver.connection 失败，跳过连接补丁: {e}")
        return

    async def patched_send(self, cdp_obj, _is_update=False):
        if self.closed:
            await self.connect()
        if not _is_update:
            await self._register_handlers()

        transaction = nodriver_connection_module.Transaction(cdp_obj)
        tx_id = next(self.__count__)
        transaction.id = tx_id
        self.mapper[tx_id] = transaction

        send_task = asyncio.create_task(self.websocket.send(transaction.message))
        send_task.add_done_callback(
            lambda task, connection=self, tx=transaction, current_tx_id=tx_id:
            _finalize_nodriver_send_task(connection, tx, current_tx_id, task)
        )
        return await transaction

    connection_instance.send = types.MethodType(patched_send, connection_instance)
    connection_instance._flow2api_send_patched = True


def _patch_nodriver_browser_instance(browser_instance):
    """在浏览器实例级别收口 update_targets，并补齐新 target 的连接补丁。"""
    if not browser_instance:
        return

    _patch_nodriver_connection_instance(getattr(browser_instance, "connection", None))
    for target in list(getattr(browser_instance, "targets", []) or []):
        _patch_nodriver_connection_instance(target)

    if getattr(browser_instance, "_flow2api_update_targets_patched", False):
        return

    original_update_targets = browser_instance.update_targets

    async def patched_update_targets(self, *args, **kwargs):
        try:
            result = await original_update_targets(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
                if _is_runtime_disconnect_error(e):
                    try:
                        setattr(self, "_flow2api_runtime_disconnected", True)
                    except Exception:
                        pass
                    try:
                        self.targets = []
                    except Exception:
                        pass
                    log_message = (
                        f"[BrowserCaptcha] nodriver.update_targets 在浏览器断连后退出: "
                        f"{type(e).__name__}: {e}"
                    )
                    if _is_runtime_normal_close_error(e):
                        debug_logger.log_info(log_message)
                    else:
                        debug_logger.log_warning(log_message)
                    return []
                raise

        _patch_nodriver_connection_instance(getattr(self, "connection", None))
        for target in list(getattr(self, "targets", []) or []):
            _patch_nodriver_connection_instance(target)
        try:
            setattr(self, "_flow2api_runtime_disconnected", False)
        except Exception:
            pass
        return result

    browser_instance.update_targets = types.MethodType(patched_update_targets, browser_instance)
    browser_instance._flow2api_update_targets_patched = True


def _patch_nodriver_runtime(browser_instance=None):
    """给 nodriver 当前浏览器实例补一层断连降噪与异常透传。"""
    global _NODRIVER_RUNTIME_PATCHED

    if not NODRIVER_AVAILABLE or uc is None:
        return

    if browser_instance is not None:
        _patch_nodriver_browser_instance(browser_instance)

    if not _NODRIVER_RUNTIME_PATCHED:
        _NODRIVER_RUNTIME_PATCHED = True
        debug_logger.log_info("[BrowserCaptcha] 已启用 nodriver 运行态安全补丁")


def _parse_proxy_url(proxy_url: str):
    """Parse a proxy URL into (protocol, host, port, username, password)."""
    if not proxy_url:
        return None, None, None, None, None
    url = proxy_url.strip()
    if not re.match(r'^(http|https|socks5h?|socks5)://', url):
        url = f"http://{url}"
    m = re.match(r'^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', url)
    if not m:
        return None, None, None, None, None
    protocol, username, password, host, port = m.groups()
    if protocol == "socks5h":
        protocol = "socks5"
    return protocol, host, port, username, password


def _compose_proxy_url(
    protocol: Optional[str],
    host: Optional[str],
    port: Optional[str],
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[str]:
    """Compose a proxy URL from parsed proxy parts."""
    if not protocol or not host or not port:
        return None

    auth = ""
    if username and password:
        auth = f"{username}:{password}@"

    return f"{protocol}://{auth}{host}:{port}"


def _parse_windows_proxy_server_candidates(proxy_server: str) -> list[str]:
    """Parse Windows Internet Settings ProxyServer into normalized proxy URLs."""
    normalized_candidates: list[str] = []
    seen: set[str] = set()

    for raw_item in str(proxy_server or "").split(";"):
        item = str(raw_item or "").strip()
        if not item:
            continue
        if "=" in item:
            _, item = item.split("=", 1)
            item = item.strip()
        if not item:
            continue
        normalized = item if re.match(r"^[a-z]+://", item, re.I) else f"http://{item}"
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized_candidates.append(normalized)

    return normalized_candidates


def _read_windows_internet_settings_proxy_candidates() -> list[str]:
    """Read ProxyServer candidates from Windows Internet Settings."""
    if os.name != "nt":
        return []

    try:
        import winreg
    except Exception:
        return []

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key_path) as key:
                proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except Exception:
            continue

        candidates = _parse_windows_proxy_server_candidates(str(proxy_server or ""))
        if candidates:
            return candidates

    return []


def _get_recaptcha_script_cache_dir() -> Path:
    """Return the persistent cache directory for reCAPTCHA bootstrap scripts."""
    cache_dir = PERSONAL_RUNTIME_TMP_DIR / "recaptcha_js"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_recaptcha_script_cache_path(cache_dir: Path, remote_url: str) -> Path:
    """Map a bootstrap script URL to a stable local cache path."""
    digest = hashlib.md5(remote_url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.js"


def _write_text_cache(cache_path: Path, content: str):
    """Atomically write UTF-8 text content into the cache."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f"{cache_path.suffix}.part")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(temp_path, cache_path)


def _get_recaptcha_asset_cache_dir() -> Path:
    """Return the persistent cache directory for local reCAPTCHA static assets."""
    cache_dir = PERSONAL_RUNTIME_TMP_DIR / "recaptcha_assets"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _guess_recaptcha_asset_mime_type(remote_url: str, response_mime: Optional[str] = None) -> str:
    """Best-effort MIME type detection for cached reCAPTCHA assets."""
    normalized = (response_mime or "").split(";", 1)[0].strip().lower()
    if normalized:
        return normalized

    guessed, _ = mimetypes.guess_type(urlparse(remote_url).path)
    if guessed:
        return guessed

    suffix = Path(urlparse(remote_url).path).suffix.lower()
    if suffix == ".js":
        return "text/javascript"
    if suffix == ".css":
        return "text/css"
    if suffix == ".woff2":
        return "font/woff2"
    if suffix == ".woff":
        return "font/woff"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".ico":
        return "image/x-icon"
    return "application/octet-stream"


def _get_recaptcha_asset_cache_path(cache_dir: Path, remote_url: str) -> Path:
    """Map any reCAPTCHA static asset URL to a stable local cache path."""
    digest = hashlib.md5(remote_url.encode("utf-8")).hexdigest()
    suffix = Path(urlparse(remote_url).path).suffix.lower() or ".bin"
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    return cache_dir / f"{digest}{suffix}"


def _write_binary_cache(cache_path: Path, content: bytes):
    """Atomically write binary content into the cache."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f"{cache_path.suffix}.part")
    with open(temp_path, "wb") as f:
        f.write(content)
    os.replace(temp_path, cache_path)


def _extract_remote_urls_from_text(source: str) -> list[str]:
    """Extract absolute remote URLs from JavaScript/CSS text."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in RECAPTCHA_REMOTE_URL_PATTERN.findall(source or ""):
        normalized = match.strip().rstrip("),;\"'")
        if not normalized.startswith(("http://", "https://")) or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _extract_remote_urls_from_css(css_source: str, base_url: str) -> list[str]:
    """Extract absolute asset URLs referenced from a CSS source."""
    urls: list[str] = []
    seen: set[str] = set()
    for raw_value in RECAPTCHA_CSS_URL_PATTERN.findall(css_source or ""):
        normalized = raw_value.strip().strip("\"'")
        if not normalized or normalized.startswith(("data:", "blob:", "javascript:")):
            continue
        absolute = urljoin(base_url, normalized)
        if not absolute.startswith(("http://", "https://")) or absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
    return urls


def _rewrite_css_urls_with_local_assets(
    css_source: str,
    base_url: str,
    replacements: Dict[str, str],
) -> str:
    """Rewrite CSS url(...) references to their local data URLs."""

    def _replace(match: re.Match[str]) -> str:
        raw_value = match.group(1).strip()
        normalized = raw_value.strip("\"'")
        if not normalized or normalized.startswith(("data:", "blob:", "javascript:")):
            return match.group(0)

        absolute = urljoin(base_url, normalized)
        local_value = replacements.get(absolute)
        if not local_value:
            return match.group(0)
        return f"url('{local_value}')"

    return RECAPTCHA_CSS_URL_PATTERN.sub(_replace, css_source or "")


def _rewrite_text_urls_with_local_assets(
    source_text: str,
    replacements: Dict[str, str],
) -> str:
    """Rewrite literal remote URLs in JS/text content to local data URLs."""
    localized = source_text or ""
    for remote_url in sorted(replacements.keys(), key=len, reverse=True):
        localized = localized.replace(remote_url, replacements[remote_url])
    return localized


def _build_data_url(content: bytes, mime_type: str) -> str:
    """Encode bytes as a data: URL."""
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _is_localizable_recaptcha_asset_url(remote_url: str) -> bool:
    """Check whether the remote URL is a static asset suitable for local mirroring."""
    try:
        parsed = urlparse(remote_url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    path = parsed.path.lower()
    suffix = Path(path).suffix.lower()

    if host in {"www.gstatic.com", "www.gstatic.cn", "fonts.gstatic.com"}:
        return suffix in RECAPTCHA_STATIC_EXTENSIONS or "/recaptcha/" in path or "/api2/" in path

    if host in {"www.google.com", "www.recaptcha.net"}:
        return path.startswith("/recaptcha/") and suffix in {".js", ".css"}

    if host == "labs.google":
        return suffix in RECAPTCHA_STATIC_EXTENSIONS

    return False


def _iter_recaptcha_asset_url_aliases(remote_url: str) -> list[str]:
    """Return equivalent host aliases for gstatic-hosted assets."""
    try:
        parsed = urlparse(remote_url)
    except Exception:
        return [remote_url]

    aliases: list[str] = []
    seen: set[str] = set()
    candidate_hosts = RECAPTCHA_STATIC_HOST_ALIASES.get(parsed.netloc.lower(), (parsed.netloc,))
    for host in candidate_hosts:
        candidate = urlunparse(parsed._replace(netloc=host))
        if candidate in seen:
            continue
        seen.add(candidate)
        aliases.append(candidate)
    return aliases


def _iter_recaptcha_release_companion_urls(remote_url: str) -> list[str]:
    """Derive same-release companion assets from a locale JS URL."""
    try:
        parsed = urlparse(remote_url)
    except Exception:
        return []

    match = re.search(r"/recaptcha/releases/([^/]+)/recaptcha__[^/]+\.js$", parsed.path)
    if not match:
        return []

    release_id = match.group(1)
    locale_match = re.search(r"/recaptcha__([^/]+)\.js$", parsed.path)
    locale = (locale_match.group(1) if locale_match else "en").replace("_", "-")
    companions: list[str] = []
    for css_name in ("styles__ltr.css", "styles__rtl.css"):
        companions.append(
            urlunparse(
                parsed._replace(
                    path=f"/recaptcha/releases/{release_id}/{css_name}",
                    query="",
                    fragment="",
                )
            )
        )
    for host in ("www.recaptcha.net", "www.google.com"):
        companions.append(
            urlunparse(
                parsed._replace(
                    scheme="https",
                    netloc=host,
                    path="/recaptcha/enterprise/webworker.js",
                    query=f"hl={locale}&v={release_id}",
                    fragment="",
                )
            )
        )
    return companions


def _create_proxy_auth_extension(protocol: str, host: str, port: str, username: str, password: str) -> str:
    """Create a temporary Chrome extension directory for proxy authentication.
    Returns the path to the extension directory."""
    ext_dir = tempfile.mkdtemp(prefix="nodriver_proxy_auth_")

    scheme_map = {"http": "http", "https": "https", "socks5": "socks5"}
    scheme = scheme_map.get(protocol, "http")

    manifest = {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Proxy Auth Helper",
        "permissions": [
            "proxy",
            "storage",
            "webRequest",
            "webRequestAuthProvider",
        ],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "108.0.0.0",
    }
    background_js = (
        "const config = {\n"
        '    mode: "fixed_servers",\n'
        "    rules: {\n"
        "        singleProxy: {\n"
        f"            scheme: {json.dumps(scheme)},\n"
        f"            host: {json.dumps(host)},\n"
        f"            port: parseInt({port})\n"
        "        },\n"
        '        bypassList: ["localhost", "127.0.0.1"]\n'
        "    }\n"
        "};\n"
        "function applyProxyConfig() {\n"
        '    chrome.proxy.settings.set({value: config, scope: "regular"}, () => {\n'
        "        if (chrome.runtime.lastError) {\n"
        '            console.warn("proxy.settings.set failed", chrome.runtime.lastError.message);\n'
        "        }\n"
        "    });\n"
        "}\n"
        "chrome.runtime.onInstalled.addListener(applyProxyConfig);\n"
        "chrome.runtime.onStartup.addListener(applyProxyConfig);\n"
        "applyProxyConfig();\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "    (details, callback) => {\n"
        "        if (!details.isProxy) {\n"
        "            callback({});\n"
        "            return;\n"
        "        }\n"
        "        callback({\n"
        "            authCredentials: {\n"
        f"                username: {json.dumps(username)},\n"
        f"                password: {json.dumps(password)}\n"
        "            }\n"
        "        });\n"
        "    },\n"
        '    {urls: ["<all_urls>"]},\n'
        "    ['asyncBlocking']\n"
        ");\n"
    )
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)
    return ext_dir




__all__ = [name for name in globals() if not name.startswith('__') and name not in ['_is_running_in_docker', '_is_truthy_env', '_path_mtime_age_seconds', '_remove_path_quietly', '_cleanup_runtime_artifacts_sync', '_run_pip_install', '_ensure_nodriver_installed', '_read_windows_app_path', '_detect_real_browser_executable_path', '_resolve_browser_executable_path', '_build_personal_browser_args', '_env_truthy', '_resolve_browser_launch_parallelism_limit', '_resolve_personal_browser_sandbox_enabled', '_normalize_personal_browser_args_for_launch', '_tune_personal_browser_args_for_docker_headed', '_flatten_exception_text', '_is_runtime_disconnect_error', '_is_runtime_normal_close_error', '_finalize_nodriver_send_task', '_patch_nodriver_connection_instance', '_patch_nodriver_browser_instance', '_patch_nodriver_runtime', '_parse_proxy_url', '_compose_proxy_url', '_parse_windows_proxy_server_candidates', '_read_windows_internet_settings_proxy_candidates', '_get_recaptcha_script_cache_dir', '_get_recaptcha_script_cache_path', '_write_text_cache', '_get_recaptcha_asset_cache_dir', '_guess_recaptcha_asset_mime_type', '_get_recaptcha_asset_cache_path', '_write_binary_cache', '_extract_remote_urls_from_text', '_extract_remote_urls_from_css', '_rewrite_css_urls_with_local_assets', '_rewrite_text_urls_with_local_assets', '_build_data_url', '_is_localizable_recaptcha_asset_url', '_iter_recaptcha_asset_url_aliases', '_iter_recaptcha_release_companion_urls', '_create_proxy_auth_extension']] + ['_is_running_in_docker', '_is_truthy_env', '_path_mtime_age_seconds', '_remove_path_quietly', '_cleanup_runtime_artifacts_sync', '_run_pip_install', '_ensure_nodriver_installed', '_read_windows_app_path', '_detect_real_browser_executable_path', '_resolve_browser_executable_path', '_build_personal_browser_args', '_env_truthy', '_resolve_browser_launch_parallelism_limit', '_resolve_personal_browser_sandbox_enabled', '_normalize_personal_browser_args_for_launch', '_tune_personal_browser_args_for_docker_headed', '_flatten_exception_text', '_is_runtime_disconnect_error', '_is_runtime_normal_close_error', '_finalize_nodriver_send_task', '_patch_nodriver_connection_instance', '_patch_nodriver_browser_instance', '_patch_nodriver_runtime', '_parse_proxy_url', '_compose_proxy_url', '_parse_windows_proxy_server_candidates', '_read_windows_internet_settings_proxy_candidates', '_get_recaptcha_script_cache_dir', '_get_recaptcha_script_cache_path', '_write_text_cache', '_get_recaptcha_asset_cache_dir', '_guess_recaptcha_asset_mime_type', '_get_recaptcha_asset_cache_path', '_write_binary_cache', '_extract_remote_urls_from_text', '_extract_remote_urls_from_css', '_rewrite_css_urls_with_local_assets', '_rewrite_text_urls_with_local_assets', '_build_data_url', '_is_localizable_recaptcha_asset_url', '_iter_recaptcha_asset_url_aliases', '_iter_recaptcha_release_companion_urls', '_create_proxy_auth_extension']
