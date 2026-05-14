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

PERSONAL_COOKIE_PREBIND_URL = "https://labs.google/"
PERSONAL_LABS_BOOTSTRAP_URL = "https://labs.google/fx/api/auth/providers"
PERSONAL_COOKIE_TARGET_URLS = (
    "https://labs.google/",
    "https://www.google.com/",
    "https://www.recaptcha.net/",
)
PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS = (
    "https://www.google.com/",
    "https://www.recaptcha.net/",
)
PERSONAL_HEADLESS_VISIBLE_SPOOF_SOURCE = r"""
(() => {
    const marker = "__personalHeadlessVisibleSpoofInstalled__";
    if (window[marker]) {
        return;
    }
    window[marker] = true;

    const defineGetter = (target, key, getter) => {
        if (!target) {
            return;
        }
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: true,
                get: getter,
            });
        } catch (e) {}
    };

    defineGetter(Document.prototype, "visibilityState", () => "visible");
    defineGetter(document, "visibilityState", () => "visible");
    defineGetter(Document.prototype, "webkitVisibilityState", () => "visible");
    defineGetter(document, "webkitVisibilityState", () => "visible");
    defineGetter(Document.prototype, "hidden", () => false);
    defineGetter(document, "hidden", () => false);
    defineGetter(Document.prototype, "webkitHidden", () => false);
    defineGetter(document, "webkitHidden", () => false);

    try {
        document.hasFocus = () => true;
    } catch (e) {}

    try {
        if (typeof window.focus === "function") {
            window.focus();
        }
    } catch (e) {}

    const emit = (target, type) => {
        try {
            target.dispatchEvent(new Event(type));
        } catch (e) {}
    };

    setTimeout(() => {
        emit(document, "visibilitychange");
        emit(window, "focus");
        emit(window, "pageshow");
    }, 0);
})();
"""
PERSONAL_FINGERPRINT_SURFACE_SPOOF_MARKER = "__personalFingerprintSurfaceSpoofInstalled__"
PERSONAL_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PERSONAL_RUNTIME_TMP_DIR = PERSONAL_RUNTIME_ROOT / "tmp"
PERSONAL_RUNTIME_DATA_DIR = PERSONAL_RUNTIME_ROOT / "data"


