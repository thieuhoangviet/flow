"""
This file is a lightweight wrapper for backward compatibility.
The actual implementation has been refactored and split into the `captcha_personal` package.
"""

from .captcha_personal import BrowserCaptchaService
from .captcha_personal.models import ResidentTabInfo, TokenPoolLease, TokenPoolTimeoutError
from .captcha_personal.pool_service import _PersonalBrowserPoolService
from .captcha_personal.utils import (
    IS_DOCKER,
    ALLOW_DOCKER_HEADED,
    DOCKER_HEADED_BLOCKED,
    _ensure_nodriver_installed,
)
from .captcha_personal.constants import (
    PERSONAL_COOKIE_PREBIND_URL,
    PERSONAL_LABS_BOOTSTRAP_URL,
    PERSONAL_COOKIE_TARGET_URLS,
    PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS,
)

__all__ = [
    "BrowserCaptchaService",
    "ResidentTabInfo",
    "TokenPoolLease",
    "TokenPoolTimeoutError",
    "_PersonalBrowserPoolService",
    "IS_DOCKER",
    "ALLOW_DOCKER_HEADED",
    "DOCKER_HEADED_BLOCKED",
    "_ensure_nodriver_installed",
    "PERSONAL_COOKIE_PREBIND_URL",
    "PERSONAL_LABS_BOOTSTRAP_URL",
    "PERSONAL_COOKIE_TARGET_URLS",
    "PERSONAL_GOOGLE_FAMILY_COOKIE_MIRROR_URLS",
]
