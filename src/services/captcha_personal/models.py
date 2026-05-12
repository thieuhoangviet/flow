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

class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(
        self,
        tab,
        slot_id: str,
        project_id: Optional[str] = None,
        *,
        token_id: Optional[int] = None,
        browser_context_id: Any = None,
    ):
        self.tab = tab
        self.slot_id = slot_id
        self.project_id = project_id or slot_id
        self.token_id = token_id
        self.browser_context_id = browser_context_id
        self.recaptcha_ready = False
        self.created_at = time.time()
        self.last_used_at = time.time()  # 最后使用时间
        self.use_count = 0  # 使用次数
        self.fingerprint: Optional[Dict[str, Any]] = None
        self.cookie_signature: Optional[str] = None
        self.solve_lock = asyncio.Lock()  # 串行化同一标签页上的执行，降低并发冲突
        self.pending_assignment_count = 0  # 选中但尚未真正进入 solve_lock 的请求数


@dataclass
class TokenPoolLease:
    bucket_key: str
    token: str
    project_id: str
    action: str
    token_id: Optional[int]
    slot_id: Optional[str]
    worker_index: Optional[int]
    created_at: float
    expires_at: float


class TokenPoolTimeoutError(TimeoutError):
    """严格 token 池模式下，请求在等待可用 token 时超时。"""


