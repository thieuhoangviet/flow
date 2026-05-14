import asyncio
from typing import TYPE_CHECKING, Any
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

class BrowserFingerprintMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    def _refresh_runtime_fingerprint_spoof_seed(
        self,
        *,
        user_agent: Optional[str] = None,
        product: Optional[str] = None,
    ) -> None:
        self._runtime_fingerprint_spoof_seed = hashlib.sha256(
            (
                f"runtime:{time.time_ns()}:{os.getpid()}:"
                f"{self._browser_instance_id}:{self.user_data_dir or '<isolated-temp>'}"
            ).encode("utf-8")
        ).hexdigest()
        self._runtime_surface_profile = self._build_runtime_surface_profile(
            user_agent=user_agent,
            product=product,
        )

    async def _get_live_browser_runtime_identity(self) -> tuple[Optional[str], Optional[str]]:
        if not self.browser:
            return None, None

        try:
            from nodriver import cdp

            version_info = await self._run_with_timeout(
                self.browser.connection.send(cdp.browser.get_version()),
                timeout_seconds=5.0,
                label="browser.get_version:runtime_profile",
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取浏览器运行态版本失败，回退默认 runtime profile: {e}")
            return None, None

        user_agent = None
        product = None
        if isinstance(version_info, (list, tuple)):
            if len(version_info) >= 4:
                product = version_info[1]
                user_agent = version_info[3]
        elif isinstance(version_info, dict):
            product = version_info.get("product")
            user_agent = version_info.get("userAgent")
        else:
            product = getattr(version_info, "product", None)
            user_agent = getattr(version_info, "userAgent", None) or getattr(version_info, "user_agent", None)

        normalized_user_agent = str(user_agent or "").strip() or None
        normalized_product = str(product or "").strip() or None
        return normalized_user_agent, normalized_product

    def _get_runtime_surface_profile(self) -> Dict[str, Any]:
        return dict(self._runtime_surface_profile or {})

    @staticmethod
    def _format_runtime_client_hint_brands(items: Iterable[Dict[str, Any]]) -> str:
        formatted: list[str] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").strip()
            version = str(item.get("version") or "").strip()
            if not brand or not version:
                continue
            formatted.append(f'"{brand}";v="{version}"')
        return ", ".join(formatted)

    def _build_runtime_extra_http_headers(self) -> Dict[str, str]:
        runtime_profile = self._get_runtime_surface_profile()
        headers = {
            str(key): str(value)
            for key, value in dict(runtime_profile.get("httpHeaders") or {}).items()
            if str(key or "").strip() and value not in (None, "")
        }
        metadata = dict(runtime_profile.get("userAgentMetadata") or {})
        brands = metadata.get("brands") or []
        full_version_list = metadata.get("fullVersionList") or []
        sec_ch_ua = self._format_runtime_client_hint_brands(brands)
        sec_ch_ua_full_version_list = self._format_runtime_client_hint_brands(full_version_list)
        if sec_ch_ua:
            headers["Sec-CH-UA"] = sec_ch_ua
        headers["Sec-CH-UA-Mobile"] = "?1" if metadata.get("mobile") else "?0"
        for header_name, value in (
            ("Sec-CH-UA-Platform", metadata.get("platform")),
            ("Sec-CH-UA-Platform-Version", metadata.get("platformVersion")),
            ("Sec-CH-UA-Full-Version", metadata.get("fullVersion")),
            ("Sec-CH-UA-Arch", metadata.get("architecture")),
            ("Sec-CH-UA-Bitness", metadata.get("bitness")),
        ):
            normalized = str(value or "").strip()
            if normalized:
                headers[header_name] = f'"{normalized}"'
        if sec_ch_ua_full_version_list:
            headers["Sec-CH-UA-Full-Version-List"] = sec_ch_ua_full_version_list
        return headers

    def _build_runtime_user_agent_metadata(self):
        runtime_profile = self._get_runtime_surface_profile()
        metadata_profile = dict(runtime_profile.get("userAgentMetadata") or {})
        if not metadata_profile:
            return None

        try:
            from nodriver import cdp

            def _build_brand_items(items: Iterable[Dict[str, Any]]) -> list[Any]:
                result = []
                for item in items or ():
                    if not isinstance(item, dict):
                        continue
                    brand = str(item.get("brand") or "").strip()
                    version = str(item.get("version") or "").strip()
                    if not brand or not version:
                        continue
                    result.append(cdp.emulation.UserAgentBrandVersion(brand=brand, version=version))
                return result

            return cdp.emulation.UserAgentMetadata(
                platform=str(metadata_profile.get("platform") or "Windows"),
                platform_version=str(metadata_profile.get("platformVersion") or "10.0.0"),
                architecture=str(metadata_profile.get("architecture") or "x86"),
                model=str(metadata_profile.get("model") or ""),
                mobile=bool(metadata_profile.get("mobile")),
                brands=_build_brand_items(metadata_profile.get("brands") or []),
                full_version_list=_build_brand_items(metadata_profile.get("fullVersionList") or []),
                full_version=str(metadata_profile.get("fullVersion") or ""),
                bitness=str(metadata_profile.get("bitness") or "64"),
                wow64=bool(metadata_profile.get("wow64")),
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 构建 UserAgentMetadata 失败，将跳过 UA-CH runtime 注入: {e}")
            return None

    @staticmethod
    def _parse_runtime_browser_version(user_agent: Optional[str], product: Optional[str] = None) -> str:
        candidates = [
            str(user_agent or "").strip(),
            str(product or "").strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            match = re.search(r"(?:Chrome|Chromium)/(\d+\.\d+\.\d+\.\d+)", candidate)
            if match:
                return match.group(1)
            match = re.search(r"/(\d+\.\d+\.\d+\.\d+)", candidate)
            if match:
                return match.group(1)
        return "135.0.0.0"

    @classmethod
    def _derive_runtime_os_profile(cls, user_agent: Optional[str]) -> Dict[str, Any]:
        ua_text = str(user_agent or "")
        if "Mac OS X" in ua_text or "Macintosh" in ua_text:
            return {
                "ua_ch_platform": "macOS",
                "ua_ch_platform_version": "14.0.0",
                "js_platform": "MacIntel",
                "vendor": "Google Inc.",
                "architecture": "x86",
                "bitness": "64",
                "wow64": False,
            }
        if "Linux" in ua_text and "Android" not in ua_text:
            return {
                "ua_ch_platform": "Linux",
                "ua_ch_platform_version": "6.1.0",
                "js_platform": "Linux x86_64",
                "vendor": "Google Inc.",
                "architecture": "x86",
                "bitness": "64",
                "wow64": False,
            }
        return {
            "ua_ch_platform": "Windows",
            "ua_ch_platform_version": "10.0.0",
            "js_platform": "Win32",
            "vendor": "Google Inc.",
            "architecture": "x86",
            "bitness": "64",
            "wow64": False,
        }

    def _build_runtime_surface_profile(
        self,
        *,
        user_agent: Optional[str] = None,
        product: Optional[str] = None,
    ) -> Dict[str, Any]:
        seed_material = (
            f"{self._runtime_fingerprint_spoof_seed}:{self._browser_instance_id}:runtime-surface"
        ).encode("utf-8")
        digest = hashlib.sha256(seed_material).digest()
        full_version = self._parse_runtime_browser_version(user_agent, product)
        major_version = full_version.split(".", 1)[0]
        effective_user_agent = str(user_agent or "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{full_version} Safari/537.36"
        )
        os_profile = self._derive_runtime_os_profile(effective_user_agent)

        locale_profiles = (
            {
                "locale": "zh-CN",
                "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                "languages": ["zh-CN", "zh", "en"],
                "timezoneId": "Asia/Shanghai",
                "geolocation": {"latitude": 31.2304, "longitude": 121.4737, "accuracy": 18.0},
            },
            {
                "locale": "en-US",
                "acceptLanguage": "en-US,en;q=0.9",
                "languages": ["en-US", "en"],
                "timezoneId": "America/New_York",
                "geolocation": {"latitude": 40.7128, "longitude": -74.0060, "accuracy": 20.0},
            },
            {
                "locale": "en-US",
                "acceptLanguage": "en-US,en;q=0.9",
                "languages": ["en-US", "en"],
                "timezoneId": "America/Los_Angeles",
                "geolocation": {"latitude": 34.0522, "longitude": -118.2437, "accuracy": 20.0},
            },
            {
                "locale": "en-GB",
                "acceptLanguage": "en-GB,en;q=0.9",
                "languages": ["en-GB", "en"],
                "timezoneId": "Europe/London",
                "geolocation": {"latitude": 51.5074, "longitude": -0.1278, "accuracy": 18.0},
            },
            {
                "locale": "ja-JP",
                "acceptLanguage": "ja-JP,ja;q=0.9,en;q=0.7",
                "languages": ["ja-JP", "ja", "en"],
                "timezoneId": "Asia/Tokyo",
                "geolocation": {"latitude": 35.6762, "longitude": 139.6503, "accuracy": 18.0},
            },
        )
        locale_profile = dict(locale_profiles[digest[2] % len(locale_profiles)])
        desktop_profiles = (
            {"width": 1366, "height": 768, "hardwareConcurrency": 4, "deviceMemory": 4},
            {"width": 1440, "height": 900, "hardwareConcurrency": 8, "deviceMemory": 8},
            {"width": 1536, "height": 864, "hardwareConcurrency": 8, "deviceMemory": 8},
            {"width": 1600, "height": 900, "hardwareConcurrency": 10, "deviceMemory": 8},
            {"width": 1680, "height": 1050, "hardwareConcurrency": 12, "deviceMemory": 8},
            {"width": 1920, "height": 1080, "hardwareConcurrency": 12, "deviceMemory": 8},
        )
        base_profile = dict(desktop_profiles[digest[0] % len(desktop_profiles)])
        width = int(base_profile["width"])
        height = int(base_profile["height"])
        taskbar_height = 40 + int(digest[1] % 32)
        avail_width = width
        avail_height = max(640, height - taskbar_height)
        viewport_width = min(1280, max(1100, width - (72 + int(digest[3] % 40))))
        viewport_height = min(720, max(620, avail_height - (52 + int(digest[4] % 28))))
        outer_width = min(width, viewport_width + 16)
        outer_height = min(height, viewport_height + 88)
        device_scale_factor = 1.0
        seed_prefix = hashlib.md5(seed_material).hexdigest()

        runtime_profile = {
            "seed": seed_prefix[:16],
            "userAgent": effective_user_agent,
            "acceptLanguage": str(locale_profile["acceptLanguage"]),
            "locale": {
                "code": str(locale_profile["locale"]),
                "languages": list(locale_profile["languages"]),
            },
            "timezone": {
                "id": str(locale_profile["timezoneId"]),
            },
            "geolocation": {
                "latitude": float(locale_profile["geolocation"]["latitude"]),
                "longitude": float(locale_profile["geolocation"]["longitude"]),
                "accuracy": float(locale_profile["geolocation"]["accuracy"]),
            },
            "permissions": {
                "geolocation": "granted",
                "notifications": "denied",
                "camera": "denied",
                "microphone": "denied",
                "display-capture": "denied",
            },
            "navigator": {
                "userAgent": effective_user_agent,
                "appVersion": effective_user_agent.replace("Mozilla/", "", 1) if effective_user_agent.startswith("Mozilla/") else effective_user_agent,
                "platform": str(os_profile["js_platform"]),
                "vendor": str(os_profile["vendor"]),
                "language": str(locale_profile["locale"]),
                "languages": list(locale_profile["languages"]),
                "hardwareConcurrency": int(base_profile["hardwareConcurrency"]),
                "deviceMemory": int(base_profile["deviceMemory"]),
                "maxTouchPoints": 0,
                "webdriver": False,
            },
            "screen": {
                "width": width,
                "height": height,
                "availWidth": avail_width,
                "availHeight": avail_height,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "window": {
                "innerWidth": viewport_width,
                "innerHeight": viewport_height,
                "outerWidth": outer_width,
                "outerHeight": outer_height,
                "devicePixelRatio": device_scale_factor,
            },
            "userAgentMetadata": {
                "platform": str(os_profile["ua_ch_platform"]),
                "platformVersion": str(os_profile["ua_ch_platform_version"]),
                "architecture": str(os_profile["architecture"]),
                "model": "",
                "mobile": False,
                "fullVersion": full_version,
                "bitness": str(os_profile["bitness"]),
                "wow64": bool(os_profile["wow64"]),
                "brands": [
                    {"brand": "Not.A/Brand", "version": "8"},
                    {"brand": "Chromium", "version": major_version},
                    {"brand": "Google Chrome", "version": major_version},
                ],
                "fullVersionList": [
                    {"brand": "Not.A/Brand", "version": "8.0.0.0"},
                    {"brand": "Chromium", "version": full_version},
                    {"brand": "Google Chrome", "version": full_version},
                ],
            },
            "httpHeaders": {
                "Accept-Language": str(locale_profile["acceptLanguage"]),
                "DNT": "1",
            },
            "mediaDevices": {
                "devices": [
                    {
                        "kind": "audioinput",
                        "deviceId": f"aid-{seed_prefix[0:12]}",
                        "groupId": f"grp-{seed_prefix[12:20]}",
                        "label": "",
                    },
                    {
                        "kind": "videoinput",
                        "deviceId": f"vid-{seed_prefix[20:32]}",
                        "groupId": f"grp-{seed_prefix[12:20]}",
                        "label": "",
                    },
                    {
                        "kind": "audiooutput",
                        "deviceId": f"aod-{seed_prefix[32:44]}",
                        "groupId": f"grp-{seed_prefix[44:52]}",
                        "label": "Default Audio Output",
                    },
                ],
            },
            "webrtc": {
                "candidateMaskIp": "0.0.0.0",
            },
        }
        runtime_profile["signature"] = hashlib.sha256(
            json.dumps(runtime_profile, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return runtime_profile

    def _build_tab_fingerprint_spoof_config(self, tab) -> Dict[str, Any]:
        target_id = str(getattr(tab, "target_id", "") or "").strip() or "unknown-target"
        seed_material = (
            f"{self._runtime_fingerprint_spoof_seed}:{self._browser_instance_id}:{target_id}"
        ).encode("utf-8")
        digest = hashlib.sha256(seed_material).digest()

        def non_zero_byte_delta(index: int) -> int:
            value = (digest[index] % 5) - 2
            return value if value != 0 else 1

        def signed_unit(index: int, scale: float) -> float:
            return round((((digest[index] / 255.0) * 2.0) - 1.0) * scale, 8)

        return {
            "seed": hashlib.md5(seed_material).hexdigest()[:16],
            "runtime": dict(self._runtime_surface_profile or {}),
            "canvas": {
                "rgba": [
                    non_zero_byte_delta(0),
                    non_zero_byte_delta(1),
                    non_zero_byte_delta(2),
                    0,
                ],
                "pixelStep": 13 + (digest[3] % 11),
                "lineShiftX": signed_unit(4, 0.35),
                "lineShiftY": signed_unit(5, 0.35),
            },
            "webgl": {
                "delta": non_zero_byte_delta(6),
                "stride": 19 + (digest[7] % 17),
            },
            "audio": {
                "floatDelta": signed_unit(8, 0.00003),
                "byteDelta": non_zero_byte_delta(9),
                "stride": 17 + (digest[10] % 13),
            },
        }

    def _build_tab_fingerprint_spoof_source(self, tab) -> str:
        config_json = json.dumps(
            self._build_tab_fingerprint_spoof_config(tab),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return (
            """
(() => {
    const marker = __MARKER_JSON__;
    if (window[marker]) {
        return;
    }

    const config = __CONFIG_JSON__;
    window[marker] = config.seed;
    const runtimeProfile = config.runtime || {};

    const setValue = (target, key, value) => {
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: false,
                writable: true,
                value,
            });
        } catch (e) {}
    };

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

    const defineMethod = (target, key, value) => {
        if (!target || typeof value !== "function") {
            return;
        }
        try {
            Object.defineProperty(target, key, {
                configurable: true,
                enumerable: false,
                writable: true,
                value,
            });
        } catch (e) {
            try {
                target[key] = value;
            } catch (err) {}
        }
    };

    const cloneValue = (value) => {
        if (value === null || value === undefined) {
            return value;
        }
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (e) {
            return value;
        }
    };

    const navigatorProfile = runtimeProfile.navigator || {};
    const localeProfile = runtimeProfile.locale || {};
    const permissionsProfile = runtimeProfile.permissions || {};
    const timezoneProfile = runtimeProfile.timezone || {};
    const uaMetadataProfile = runtimeProfile.userAgentMetadata || {};
    const windowProfile = runtimeProfile.window || {};
    const mediaDevicesProfile = runtimeProfile.mediaDevices || {};
    const webRtcProfile = runtimeProfile.webrtc || {};
    const maskedIp = String(webRtcProfile.candidateMaskIp || "0.0.0.0");
    const sanitizeIpText = (input) => String(input || "").replace(/\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b/g, maskedIp);

    const patchNavigatorMetric = (key) => {
        if (navigatorProfile[key] === undefined || navigatorProfile[key] === null) {
            return;
        }
        defineGetter(Navigator.prototype, key, () => cloneValue(navigatorProfile[key]));
        defineGetter(navigator, key, () => cloneValue(navigatorProfile[key]));
    };

    patchNavigatorMetric("userAgent");
    patchNavigatorMetric("appVersion");
    patchNavigatorMetric("platform");
    patchNavigatorMetric("vendor");
    patchNavigatorMetric("language");
    patchNavigatorMetric("languages");
    patchNavigatorMetric("hardwareConcurrency");
    patchNavigatorMetric("deviceMemory");
    patchNavigatorMetric("maxTouchPoints");
    if (navigatorProfile.webdriver !== undefined) {
        defineGetter(Navigator.prototype, "webdriver", () => false);
        defineGetter(navigator, "webdriver", () => false);
    }

    if (uaMetadataProfile && Object.keys(uaMetadataProfile).length > 0) {
        const clonedBrands = cloneValue(uaMetadataProfile.brands || []);
        const highEntropyPayload = {
            architecture: String(uaMetadataProfile.architecture || ""),
            bitness: String(uaMetadataProfile.bitness || ""),
            brands: cloneValue(uaMetadataProfile.brands || []),
            fullVersionList: cloneValue(uaMetadataProfile.fullVersionList || []),
            mobile: Boolean(uaMetadataProfile.mobile),
            model: String(uaMetadataProfile.model || ""),
            platform: String(uaMetadataProfile.platform || ""),
            platformVersion: String(uaMetadataProfile.platformVersion || ""),
            uaFullVersion: String(uaMetadataProfile.fullVersion || ""),
            wow64: Boolean(uaMetadataProfile.wow64),
        };
        const userAgentData = {
            brands: clonedBrands,
            mobile: Boolean(uaMetadataProfile.mobile),
            platform: String(uaMetadataProfile.platform || ""),
            getHighEntropyValues: async (hints) => {
                const result = {};
                const normalizedHints = Array.isArray(hints) ? hints : [];
                for (const hint of normalizedHints) {
                    if (Object.prototype.hasOwnProperty.call(highEntropyPayload, hint)) {
                        result[hint] = cloneValue(highEntropyPayload[hint]);
                    }
                }
                return result;
            },
            toJSON: () => ({
                brands: cloneValue(clonedBrands),
                mobile: Boolean(uaMetadataProfile.mobile),
                platform: String(uaMetadataProfile.platform || ""),
            }),
        };
        defineGetter(Navigator.prototype, "userAgentData", () => userAgentData);
        defineGetter(navigator, "userAgentData", () => userAgentData);
    }

    const screenProfile = runtimeProfile.screen || {};
    const patchScreenMetric = (key) => {
        if (typeof screenProfile[key] !== "number") {
            return;
        }
        defineGetter(Screen.prototype, key, () => screenProfile[key]);
        if (window.screen) {
            defineGetter(window.screen, key, () => screenProfile[key]);
        }
    };
    patchScreenMetric("width");
    patchScreenMetric("height");
    patchScreenMetric("availWidth");
    patchScreenMetric("availHeight");
    patchScreenMetric("colorDepth");
    patchScreenMetric("pixelDepth");

    const patchWindowMetric = (key) => {
        if (typeof windowProfile[key] !== "number") {
            return;
        }
        defineGetter(window, key, () => windowProfile[key]);
        if (window.Window && window.Window.prototype) {
            defineGetter(window.Window.prototype, key, () => windowProfile[key]);
        }
    };
    patchWindowMetric("innerWidth");
    patchWindowMetric("innerHeight");
    patchWindowMetric("outerWidth");
    patchWindowMetric("outerHeight");
    patchWindowMetric("devicePixelRatio");

    const localeCode = String(localeProfile.code || navigatorProfile.language || "");
    const timezoneId = String(timezoneProfile.id || "");
    const patchIntlResolvedOptions = (ctor) => {
        if (!ctor || !ctor.prototype || typeof ctor.prototype.resolvedOptions !== "function") {
            return;
        }
        const original = ctor.prototype.resolvedOptions;
        ctor.prototype.resolvedOptions = function(...args) {
            const resolved = original.apply(this, args) || {};
            if (localeCode) {
                resolved.locale = localeCode;
            }
            if (timezoneId) {
                resolved.timeZone = timezoneId;
            }
            return resolved;
        };
    };
    patchIntlResolvedOptions(window.Intl && window.Intl.DateTimeFormat);
    patchIntlResolvedOptions(window.Intl && window.Intl.NumberFormat);
    patchIntlResolvedOptions(window.Intl && window.Intl.Collator);
    patchIntlResolvedOptions(window.Intl && window.Intl.PluralRules);

    const buildPermissionStatus = (state) => ({
        state,
        onchange: null,
        addEventListener() {},
        removeEventListener() {},
        dispatchEvent() {
            return true;
        },
    });
    if (navigator.permissions) {
        const permissionsTarget = navigator.permissions;
        const permissionsProto = Object.getPrototypeOf(permissionsTarget);
        const originalQuery = typeof permissionsTarget.query === "function"
            ? permissionsTarget.query.bind(permissionsTarget)
            : null;
        const patchedQuery = function(descriptor) {
            const name = String(descriptor && descriptor.name || "").toLowerCase();
            if (name && Object.prototype.hasOwnProperty.call(permissionsProfile, name)) {
                return Promise.resolve(buildPermissionStatus(String(permissionsProfile[name] || "prompt")));
            }
            if (!originalQuery) {
                return Promise.resolve(buildPermissionStatus("prompt"));
            }
            return originalQuery(descriptor);
        };
        defineMethod(permissionsProto, "query", patchedQuery);
        defineMethod(permissionsTarget, "query", patchedQuery);
    }

    const ensureMediaDevices = () => {
        let target = navigator.mediaDevices || null;
        if (!target) {
            target = {};
        }
        const devices = Array.isArray(mediaDevicesProfile.devices) ? mediaDevicesProfile.devices : [];
        const deniedMedia = () => Promise.reject(new DOMException("Permission denied", "NotAllowedError"));
        defineMethod(target, "enumerateDevices", async () => cloneValue(devices));
        defineMethod(target, "getUserMedia", deniedMedia);
        defineMethod(target, "getDisplayMedia", deniedMedia);
        defineGetter(Navigator.prototype, "mediaDevices", () => target);
        defineGetter(navigator, "mediaDevices", () => target);
    };
    ensureMediaDevices();

    const patchRtcSessionDescription = (ctor) => {
        if (!ctor || !ctor.prototype) {
            return;
        }
        const descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, "sdp");
        if (!descriptor || typeof descriptor.get !== "function") {
            return;
        }
        try {
            Object.defineProperty(ctor.prototype, "sdp", {
                configurable: true,
                enumerable: descriptor.enumerable,
                get() {
                    return sanitizeIpText(descriptor.get.call(this));
                },
            });
        } catch (e) {}
    };
    patchRtcSessionDescription(window.RTCSessionDescription);

    if (window.RTCIceCandidate && window.RTCIceCandidate.prototype) {
        const candidateDescriptor = Object.getOwnPropertyDescriptor(window.RTCIceCandidate.prototype, "candidate");
        if (candidateDescriptor && typeof candidateDescriptor.get === "function") {
            try {
                Object.defineProperty(window.RTCIceCandidate.prototype, "candidate", {
                    configurable: true,
                    enumerable: candidateDescriptor.enumerable,
                    get() {
                        return sanitizeIpText(candidateDescriptor.get.call(this));
                    },
                });
            } catch (e) {}
        }
        defineGetter(window.RTCIceCandidate.prototype, "address", () => maskedIp);
        defineGetter(window.RTCIceCandidate.prototype, "relatedAddress", () => maskedIp);
    }

    if (window.RTCPeerConnection && window.RTCPeerConnection.prototype) {
        const wrapAsyncDescriptionMethod = (methodName) => {
            const original = window.RTCPeerConnection.prototype[methodName];
            if (typeof original !== "function") {
                return;
            }
            window.RTCPeerConnection.prototype[methodName] = function(...args) {
                return Promise.resolve(original.apply(this, args)).then((description) => {
                    if (!description || typeof description.sdp !== "string") {
                        return description;
                    }
                    return Object.assign({}, description, {
                        sdp: sanitizeIpText(description.sdp),
                    });
                });
            };
        };
        wrapAsyncDescriptionMethod("createOffer");
        wrapAsyncDescriptionMethod("createAnswer");

        const wrapDescriptionSetter = (methodName) => {
            const original = window.RTCPeerConnection.prototype[methodName];
            if (typeof original !== "function") {
                return;
            }
            window.RTCPeerConnection.prototype[methodName] = function(description, ...args) {
                let nextDescription = description;
                if (description && typeof description.sdp === "string") {
                    nextDescription = Object.assign({}, description, {
                        sdp: sanitizeIpText(description.sdp),
                    });
                }
                return original.call(this, nextDescription, ...args);
            };
        };
        wrapDescriptionSetter("setLocalDescription");
        wrapDescriptionSetter("setRemoteDescription");

        const originalAddIceCandidate = window.RTCPeerConnection.prototype.addIceCandidate;
        if (typeof originalAddIceCandidate === "function") {
            window.RTCPeerConnection.prototype.addIceCandidate = function(candidate, ...args) {
                let nextCandidate = candidate;
                if (candidate && typeof candidate.candidate === "string") {
                    nextCandidate = Object.assign({}, candidate, {
                        candidate: sanitizeIpText(candidate.candidate),
                    });
                }
                return originalAddIceCandidate.call(this, nextCandidate, ...args);
            };
        }
    }

    const applyCanvasNoise = (canvas) => {
        try {
            if (!canvas || !canvas.width || !canvas.height) {
                return canvas;
            }
            const clone = document.createElement("canvas");
            clone.width = canvas.width;
            clone.height = canvas.height;
            const ctx = clone.getContext("2d", { willReadFrequently: true });
            if (!ctx) {
                return canvas;
            }
            ctx.drawImage(canvas, 0, 0);
            const x = Math.max(0, (canvas.width - 1) % config.canvas.pixelStep);
            const y = Math.max(0, (canvas.height - 1) % config.canvas.pixelStep);
            const imageData = ctx.getImageData(x, y, 1, 1);
            const data = imageData.data;
            for (let i = 0; i < 4; i += 1) {
                const nextValue = Number(data[i] || 0) + Number(config.canvas.rgba[i] || 0);
                data[i] = Math.max(0, Math.min(255, nextValue));
            }
            ctx.putImageData(imageData, x, y);
            ctx.save();
            ctx.globalAlpha = 0.01;
            ctx.fillStyle = "rgba(0,0,0,0.01)";
            ctx.fillRect(
                Math.max(0, canvas.width * 0.5 + config.canvas.lineShiftX),
                Math.max(0, canvas.height * 0.5 + config.canvas.lineShiftY),
                1,
                1
            );
            ctx.restore();
            return clone;
        } catch (e) {
            return canvas;
        }
    };

    const patchCanvasExport = (proto, methodName) => {
        if (!proto || typeof proto[methodName] !== "function") {
            return;
        }
        const original = proto[methodName];
        proto[methodName] = function(...args) {
            return original.apply(applyCanvasNoise(this), args);
        };
    };

    patchCanvasExport(HTMLCanvasElement.prototype, "toDataURL");
    patchCanvasExport(HTMLCanvasElement.prototype, "toBlob");

    if (CanvasRenderingContext2D && CanvasRenderingContext2D.prototype) {
        const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        if (typeof originalGetImageData === "function") {
            CanvasRenderingContext2D.prototype.getImageData = function(...args) {
                const result = originalGetImageData.apply(this, args);
                try {
                    if (result && result.data && result.data.length >= 4) {
                        for (let offset = 0; offset < result.data.length; offset += Math.max(4, config.canvas.pixelStep * 4)) {
                            result.data[offset] = Math.max(0, Math.min(255, result.data[offset] + config.canvas.rgba[0]));
                        }
                    }
                } catch (e) {}
                return result;
            };
        }
    }

    const patchWebGL = (proto) => {
        if (!proto || typeof proto.readPixels !== "function") {
            return;
        }
        const originalReadPixels = proto.readPixels;
        proto.readPixels = function(...args) {
            const output = args.find((item) => item && typeof item.length === "number" && typeof item.BYTES_PER_ELEMENT === "number");
            const result = originalReadPixels.apply(this, args);
            try {
                if (output && output.length) {
                    const stride = Math.max(1, Number(config.webgl.stride || 23));
                    const delta = Number(config.webgl.delta || 1);
                    for (let i = 0; i < output.length; i += stride) {
                        const nextValue = Number(output[i] || 0) + delta;
                        output[i] = Math.max(0, Math.min(255, nextValue));
                    }
                }
            } catch (e) {}
            return result;
        };
    };

    patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
    patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);

    if (window.AudioBuffer && AudioBuffer.prototype && typeof AudioBuffer.prototype.getChannelData === "function") {
        const originalGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(...args) {
            const channelData = originalGetChannelData.apply(this, args);
            try {
                const stamp = "__personalAudioSpoof_" + config.seed;
                if (channelData && channelData.length && !channelData[stamp]) {
                    const stride = Math.max(1, Number(config.audio.stride || 23));
                    const delta = Number(config.audio.floatDelta || 0);
                    for (let i = 0; i < channelData.length; i += stride) {
                        channelData[i] = channelData[i] + delta;
                    }
                    setValue(channelData, stamp, true);
                }
            } catch (e) {}
            return channelData;
        };
    }

    if (window.AnalyserNode && AnalyserNode.prototype) {
        const patchAnalyserMethod = (methodName, deltaValue) => {
            const original = AnalyserNode.prototype[methodName];
            if (typeof original !== "function") {
                return;
            }
            AnalyserNode.prototype[methodName] = function(array) {
                const result = original.call(this, array);
                try {
                    if (array && array.length) {
                        const stride = Math.max(1, Number(config.audio.stride || 23));
                        for (let i = 0; i < array.length; i += stride) {
                            array[i] = array[i] + deltaValue;
                        }
                    }
                } catch (e) {}
                return result;
            };
        };

        patchAnalyserMethod("getFloatFrequencyData", Number(config.audio.floatDelta || 0));
        patchAnalyserMethod("getFloatTimeDomainData", Number(config.audio.floatDelta || 0));
        patchAnalyserMethod("getByteFrequencyData", Number(config.audio.byteDelta || 1));
        patchAnalyserMethod("getByteTimeDomainData", Number(config.audio.byteDelta || 1));
    }
})();
"""
            .replace("__MARKER_JSON__", json.dumps(PERSONAL_FINGERPRINT_SURFACE_SPOOF_MARKER))
            .replace("__CONFIG_JSON__", config_json)
        )

    async def _apply_fingerprint_surface_spoof(self, tab, *, label: str) -> bool:
        if tab is None:
            return False

        runtime_signature = str(self._get_runtime_surface_profile().get("signature") or "").strip()
        if getattr(tab, "_personal_fingerprint_surface_spoof_signature", None) == runtime_signature:
            return True

        try:
            from nodriver import cdp

            await self._run_with_timeout(
                tab.send(
                    cdp.page.add_script_to_evaluate_on_new_document(
                        self._build_tab_fingerprint_spoof_source(tab),
                        run_immediately=True,
                    )
                ),
                timeout_seconds=5.0,
                label=f"page.add_script_to_evaluate_on_new_document:fingerprint:{label}",
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] 已注入 Canvas/WebGL/Audio 指纹轻扰动脚本 "
                f"(label={label}, target={getattr(tab, 'target_id', None) or '<none>'})"
            )
            try:
                tab._personal_fingerprint_surface_spoof_signature = runtime_signature
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 注入指纹轻扰动脚本失败 ({label}): {e}"
            )
            return False

    async def _apply_tab_startup_spoofs(
        self,
        tab,
        *,
        label: str,
        browser_context_id: Any = None,
        target_url: Optional[str] = None,
    ) -> None:
        await self._apply_runtime_profile_to_tab(
            tab,
            label=label,
            browser_context_id=browser_context_id,
            target_url=target_url,
        )
        await self._apply_headless_visibility_spoof(tab, label=label)
        await self._apply_fingerprint_surface_spoof(tab, label=label)

    async def _apply_headless_visibility_spoof(self, tab, *, label: str) -> bool:
        if not self.headless or tab is None:
            return False

        if getattr(tab, "_personal_headless_visibility_spoof_applied", None) is True:
            return True

        try:
            from nodriver import cdp

            await self._run_with_timeout(
                tab.send(
                    cdp.page.add_script_to_evaluate_on_new_document(
                        PERSONAL_HEADLESS_VISIBLE_SPOOF_SOURCE,
                        run_immediately=True,
                    )
                ),
                timeout_seconds=5.0,
                label=f"page.add_script_to_evaluate_on_new_document:{label}",
            )
            debug_logger.log_info(
                f"[BrowserCaptcha] 已注入无头可见态伪装脚本 (label={label}, target={getattr(tab, 'target_id', None) or '<none>'})"
            )
            try:
                tab._personal_headless_visibility_spoof_applied = True
            except Exception:
                pass
            
            # Đảm bảo cửa sổ vẫn bị ẩn khi tạo tab mới
            if sys.platform.startswith("win"):
                try:
                    await self._win32_hide_browser_windows()
                except Exception:
                    pass
                    
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] 注入无头可见态伪装脚本失败 ({label}): {e}"
            )
            return False

    async def _stealth_minimize_browser_window(self):
        """Stealth hidden: đẩy cửa sổ ra phía sau (không minimize) để tránh BotGuard phát hiện.
        """
        try:
            if sys.platform.startswith("win"):
                import asyncio
                async def hide_loop():
                    await self._win32_hide_browser_windows()
                asyncio.create_task(hide_loop())
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Stealth hidden setup failed: {e}")

    async def _win32_hide_browser_windows(self):
        """Ẩn tất cả cửa sổ Chrome liên quan đến browser PID khỏi taskbar."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            SW_HIDE = 0
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080

            EnumWindows = user32.EnumWindows
            GetWindowThreadProcessId = user32.GetWindowThreadProcessId
            ShowWindow = user32.ShowWindow
            IsWindowVisible = user32.IsWindowVisible
            SetWindowLongW = user32.SetWindowLongW
            GetWindowLongW = user32.GetWindowLongW

            WNDENUMPROC = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
            )

            browser_pid = self._browser_process_pid
            if not browser_pid:
                return

            # Tìm tất cả PID con của browser process
            target_pids = {int(browser_pid)}
            try:
                import subprocess
                result = subprocess.run(
                    ["wmic", "process", "where",
                     f"ParentProcessId={int(browser_pid)}",
                     "get", "ProcessId"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in (result.stdout or "").strip().splitlines():
                    line = line.strip()
                    if line.isdigit():
                        target_pids.add(int(line))
            except Exception:
                pass

            hidden_count = 0

            @WNDENUMPROC
            def enum_callback(hwnd, _lparam):
                nonlocal hidden_count
                try:
                    pid = wintypes.DWORD()
                    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value in target_pids:
                        # Không dùng SW_HIDE vì sẽ báo cho Chrome biết cửa sổ bị ẩn (BotGuard sẽ phát hiện)
                        # Thay vào đó, đẩy nó xuống dưới cùng (HWND_BOTTOM) và ẩn khỏi taskbar (WS_EX_TOOLWINDOW)
                        HWND_BOTTOM = 1
                        SWP_NOMOVE = 0x0002
                        SWP_NOSIZE = 0x0001
                        SWP_NOACTIVATE = 0x0010
                        
                        user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                        
                        # Thêm style TOOLWINDOW để không hiện trên taskbar
                        ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE)
                        SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TOOLWINDOW)
                        hidden_count += 1
                except Exception:
                    pass
                return True

            EnumWindows(enum_callback, 0)

            if hidden_count > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Stealth hidden: đã ẩn {hidden_count} cửa sổ Chrome khỏi taskbar (Win32 API)"
                )
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Stealth hidden: Win32 hide thất bại: {e}"
            )

    async def _simulate_startup_human_warmup(
        self,
        tab,
        *,
        label: str,
        duration_seconds: float = 1.0,
    ) -> bool:
        if tab is None:
            return False

        deadline = time.monotonic() + max(0.25, float(duration_seconds or 0.0))
        try:
            metrics = await self._tab_evaluate(
                tab,
                """
                (() => ({
                    width: Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0, 1280),
                    height: Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0, 720),
                    scrollHeight: Math.max(
                        document.documentElement?.scrollHeight || 0,
                        document.body?.scrollHeight || 0,
                        window.innerHeight || 0
                    ),
                }))()
                """,
                label=f"startup_human_metrics:{label}",
                timeout_seconds=2.0,
                return_by_value=True,
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 启动预热读取 viewport 失败 ({label}): {e}")
            metrics = {}

        if not isinstance(metrics, dict):
            try:
                metrics = dict(metrics or {})
            except Exception:
                metrics = {}

        viewport_width = max(640.0, float((metrics or {}).get("width") or 1280.0))
        viewport_height = max(480.0, float((metrics or {}).get("height") or 720.0))
        scroll_height = max(viewport_height, float((metrics or {}).get("scrollHeight") or viewport_height))

        rng = random.Random(
            f"{time.time_ns()}:{getattr(self, '_browser_instance_id', 0)}:{label}:{viewport_width:.0f}x{viewport_height:.0f}"
        )

        left = max(28.0, viewport_width * rng.uniform(0.10, 0.16))
        upper = max(22.0, viewport_height * rng.uniform(0.10, 0.18))
        reading_targets = [
            (left, upper),
            (viewport_width * rng.uniform(0.34, 0.42), viewport_height * rng.uniform(0.24, 0.34)),
            (viewport_width * rng.uniform(0.54, 0.66), viewport_height * rng.uniform(0.42, 0.54)),
            (viewport_width * rng.uniform(0.72, 0.84), viewport_height * rng.uniform(0.60, 0.76)),
        ]
        current_point = (
            viewport_width * rng.uniform(0.08, 0.16),
            viewport_height * rng.uniform(0.08, 0.18),
        )

        try:
            from nodriver import cdp

            await self._dispatch_input_command(
                tab,
                cdp.input_.dispatch_mouse_event(
                    "mouseMoved",
                    x=current_point[0],
                    y=current_point[1],
                    pointer_type="mouse",
                ),
                label=f"startup_human_init_move:{label}",
                timeout_seconds=1.5,
            )

            for index, target in enumerate(reading_targets):
                if time.monotonic() >= deadline:
                    break

                steps = rng.randint(5, 8)
                path = self._build_bezier_mouse_path(
                    current_point,
                    target,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    steps=steps,
                    rng=rng,
                )
                for point_index, (x, y) in enumerate(path):
                    if time.monotonic() >= deadline:
                        break
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_mouse_event(
                            "mouseMoved",
                            x=x,
                            y=y,
                            pointer_type="mouse",
                        ),
                        label=f"startup_human_move:{label}:{index}:{point_index}",
                        timeout_seconds=1.5,
                    )
                    if point_index == max(1, len(path) // 2):
                        await self._sleep_with_deadline(
                            deadline,
                            min(0.08, 0.018 + rng.expovariate(18.0)),
                        )
                    else:
                        await self._sleep_with_deadline(
                            deadline,
                            min(0.05, 0.006 + rng.expovariate(32.0)),
                        )
                current_point = target

                if time.monotonic() >= deadline:
                    break

                if index in {1, 2}:
                    wheel_delta = min(
                        180.0,
                        max(36.0, viewport_height * rng.uniform(0.08, 0.18)),
                    )
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_mouse_event(
                            "mouseWheel",
                            x=current_point[0],
                            y=current_point[1],
                            delta_x=rng.uniform(-6.0, 6.0),
                            delta_y=wheel_delta,
                            pointer_type="mouse",
                        ),
                        label=f"startup_human_wheel:{label}:{index}",
                        timeout_seconds=1.5,
                    )
                    await self._sleep_with_deadline(
                        deadline,
                        min(0.06, 0.012 + rng.expovariate(24.0)),
                    )

                if index == 1 or (index == 2 and rng.random() < 0.45):
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_key_event(
                            "keyDown",
                            key="Tab",
                            code="Tab",
                            windows_virtual_key_code=9,
                            native_virtual_key_code=9,
                        ),
                        label=f"startup_human_key_down:{label}:{index}",
                        timeout_seconds=1.5,
                    )
                    await self._sleep_with_deadline(deadline, 0.014 + rng.uniform(0.004, 0.022))
                    await self._dispatch_input_command(
                        tab,
                        cdp.input_.dispatch_key_event(
                            "keyUp",
                            key="Tab",
                            code="Tab",
                            windows_virtual_key_code=9,
                            native_virtual_key_code=9,
                        ),
                        label=f"startup_human_key_up:{label}:{index}",
                        timeout_seconds=1.5,
                    )

            final_scroll = int(min(max(0.0, scroll_height - viewport_height), viewport_height * rng.uniform(0.08, 0.20)))
            if final_scroll > 0 and time.monotonic() < deadline:
                try:
                    await self._tab_evaluate(
                        tab,
                        f"window.scrollTo({{top:{final_scroll},behavior:'auto'}})",
                        label=f"startup_human_scroll:{label}",
                        timeout_seconds=1.5,
                    )
                except Exception:
                    pass

            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 0.08))

            debug_logger.log_info(
                "[BrowserCaptcha] 已完成 fresh browser 启动人类化预热 "
                f"(label={label}, duration_ms={int(max(0.0, duration_seconds) * 1000)})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] fresh browser 启动人类化预热失败 ({label}): {e}"
            )
            return False

    @staticmethod
    def _ease_human_progress(t: float) -> float:
        normalized = max(0.0, min(1.0, t))
        return 0.5 - 0.5 * math.cos(math.pi * normalized)

    @staticmethod
    def _cubic_bezier_point(
        start: tuple[float, float],
        control_a: tuple[float, float],
        control_b: tuple[float, float],
        end: tuple[float, float],
        t: float,
    ) -> tuple[float, float]:
        omt = max(0.0, 1.0 - t)
        x = (
            (omt ** 3) * start[0]
            + 3.0 * (omt ** 2) * t * control_a[0]
            + 3.0 * omt * (t ** 2) * control_b[0]
            + (t ** 3) * end[0]
        )
        y = (
            (omt ** 3) * start[1]
            + 3.0 * (omt ** 2) * t * control_a[1]
            + 3.0 * omt * (t ** 2) * control_b[1]
            + (t ** 3) * end[1]
        )
        return x, y

    def _build_bezier_mouse_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        viewport_width: float,
        viewport_height: float,
        steps: int,
        rng: random.Random,
    ) -> list[tuple[float, float]]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = max(1.0, math.hypot(dx, dy))
        normal_x = -dy / distance
        normal_y = dx / distance
        curvature = min(distance * 0.25, max(24.0, distance * rng.uniform(0.08, 0.18)))
        control_a = (
            start[0] + dx * rng.uniform(0.20, 0.35) + normal_x * curvature * rng.uniform(-1.0, 1.0),
            start[1] + dy * rng.uniform(0.18, 0.32) + normal_y * curvature * rng.uniform(-1.0, 1.0),
        )
        control_b = (
            start[0] + dx * rng.uniform(0.62, 0.82) + normal_x * curvature * rng.uniform(-1.0, 1.0),
            start[1] + dy * rng.uniform(0.60, 0.84) + normal_y * curvature * rng.uniform(-1.0, 1.0),
        )

        path: list[tuple[float, float]] = []
        clamped_steps = max(6, int(steps or 0))
        for step_index in range(1, clamped_steps + 1):
            raw_t = step_index / clamped_steps
            eased_t = self._ease_human_progress(raw_t)
            x, y = self._cubic_bezier_point(start, control_a, control_b, end, eased_t)
            jitter_scale = max(0.6, min(2.8, distance / 180.0))
            jitter_x = rng.gauss(0.0, 0.8 * jitter_scale)
            jitter_y = rng.gauss(0.0, 0.8 * jitter_scale)
            clamped_x = min(max(2.0, x + jitter_x), max(4.0, viewport_width - 2.0))
            clamped_y = min(max(2.0, y + jitter_y), max(4.0, viewport_height - 2.0))
            path.append((clamped_x, clamped_y))
        return path

    async def _extract_tab_fingerprint(self, tab) -> Optional[Dict[str, Any]]:
        """从 nodriver 标签页提取浏览器指纹信息。"""
        try:
            fingerprint = await self._tab_evaluate(tab, """
                () => {
                    const ua = navigator.userAgent || "";
                    const lang = navigator.language || "";
                    const languages = Array.isArray(navigator.languages) ? navigator.languages.slice() : [];
                    const uaData = navigator.userAgentData || null;
                    let secChUa = "";
                    let secChUaMobile = "";
                    let secChUaPlatform = "";

                    if (uaData) {
                        if (Array.isArray(uaData.brands) && uaData.brands.length > 0) {
                            secChUa = uaData.brands
                                .map((item) => `"${item.brand}";v="${item.version}"`)
                                .join(", ");
                        }
                        secChUaMobile = uaData.mobile ? "?1" : "?0";
                        if (uaData.platform) {
                            secChUaPlatform = `"${uaData.platform}"`;
                        }
                    }

                    return {
                        user_agent: ua,
                        accept_language: lang,
                        sec_ch_ua: secChUa,
                        sec_ch_ua_mobile: secChUaMobile,
                        sec_ch_ua_platform: secChUaPlatform,
                        language: lang,
                        languages,
                        timezone: (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || "",
                        platform: navigator.platform || "",
                        vendor: navigator.vendor || "",
                        hardware_concurrency: Number(navigator.hardwareConcurrency || 0),
                        device_memory: Number(navigator.deviceMemory || 0),
                        device_pixel_ratio: Number(window.devicePixelRatio || 0),
                        screen_width: Number(screen.width || 0),
                        screen_height: Number(screen.height || 0),
                        screen_avail_width: Number(screen.availWidth || 0),
                        screen_avail_height: Number(screen.availHeight || 0),
                    };
                }
            """, label="extract_tab_fingerprint", timeout_seconds=8.0)
            if not isinstance(fingerprint, dict):
                return None

            result: Dict[str, Any] = {"proxy_url": self._proxy_url}
            for key in (
                "user_agent",
                "accept_language",
                "sec_ch_ua",
                "sec_ch_ua_mobile",
                "sec_ch_ua_platform",
                "language",
                "timezone",
                "platform",
                "vendor",
            ):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    result[key] = value
            languages = fingerprint.get("languages")
            if isinstance(languages, list):
                normalized_languages = [str(item).strip() for item in languages if str(item).strip()]
                if normalized_languages:
                    result["languages"] = normalized_languages
            for key in (
                "hardware_concurrency",
                "device_memory",
                "device_pixel_ratio",
                "screen_width",
                "screen_height",
                "screen_avail_width",
                "screen_avail_height",
            ):
                value = fingerprint.get(key)
                if isinstance(value, (int, float)) and float(value) > 0:
                    result[key] = int(value) if float(value).is_integer() else float(value)
            return result
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 提取 nodriver 指纹失败: {e}")
            return None

    async def _refresh_last_fingerprint(self, tab) -> Optional[Dict[str, Any]]:
        """缓存最近一次浏览器指纹，避免每次打码成功后都追加一轮 JS 执行。"""
        if self._is_fingerprint_cache_fresh():
            return self._last_fingerprint

        fingerprint = await self._extract_tab_fingerprint(tab)
        self._last_fingerprint = fingerprint
        self._last_fingerprint_at = time.monotonic() if fingerprint else 0.0
        return fingerprint

    def _remember_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        if isinstance(fingerprint, dict) and fingerprint:
            self._last_fingerprint = dict(fingerprint)
            self._last_fingerprint_at = time.monotonic()
        else:
            self._last_fingerprint = None
            self._last_fingerprint_at = 0.0

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """返回最近一次打码时的浏览器指纹快照。"""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)

    def _is_fingerprint_cache_fresh(self) -> bool:
        if not self._last_fingerprint:
            return False
        ttl_seconds = max(0.0, float(self._fingerprint_cache_ttl_seconds or 0.0))
        if ttl_seconds <= 0:
            return False
        return (time.monotonic() - self._last_fingerprint_at) < ttl_seconds

    @staticmethod
    def _normalize_permission_origin(url: Optional[str]) -> Optional[str]:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    def _build_runtime_permission_origins(self, target_url: Optional[str] = None) -> list[str]:
        seen: set[str] = set()
        origins: list[str] = []
        candidate_urls = [
            target_url,
            PERSONAL_LABS_BOOTSTRAP_URL,
            *PERSONAL_COOKIE_TARGET_URLS,
        ]
        for candidate in candidate_urls:
            origin = self._normalize_permission_origin(candidate)
            if not origin or origin in seen:
                continue
            seen.add(origin)
            origins.append(origin)
        return origins

    async def _apply_runtime_profile_permissions(
        self,
        *,
        label: str,
        browser_context_id: Any = None,
        target_url: Optional[str] = None,
    ) -> bool:
        if not self.browser:
            return False

        runtime_profile = self._get_runtime_surface_profile()
        permissions_profile = dict(runtime_profile.get("permissions") or {})
        if not permissions_profile:
            return False

        try:
            from nodriver import cdp

            permission_mapping = {
                "geolocation": "geolocation",
                "notifications": "notifications",
                "camera": "camera",
                "microphone": "microphone",
                "display-capture": "display-capture",
            }
            permission_settings = {
                "granted": cdp.browser.PermissionSetting.GRANTED,
                "denied": cdp.browser.PermissionSetting.DENIED,
                "prompt": cdp.browser.PermissionSetting.PROMPT,
            }
            configured_permissions = [
                (
                    permission_mapping[key],
                    permission_settings[str(value or "").strip().lower()],
                )
                for key, value in permissions_profile.items()
                if key in permission_mapping and str(value or "").strip().lower() in permission_settings
            ]
            if not configured_permissions:
                return False

            normalized_browser_context_id = browser_context_id
            if (
                normalized_browser_context_id is not None
                and not hasattr(normalized_browser_context_id, "to_json")
            ):
                normalized_browser_context_id = cdp.browser.BrowserContextID(
                    str(normalized_browser_context_id)
                )

            applied = False
            for origin in self._build_runtime_permission_origins(target_url=target_url):
                for permission_name, permission_setting in configured_permissions:
                    try:
                        await self._run_with_timeout(
                            self.browser.connection.send(
                                cdp.browser.set_permission(
                                    permission=cdp.browser.PermissionDescriptor(name=permission_name),
                                    setting=permission_setting,
                                    origin=origin,
                                    browser_context_id=normalized_browser_context_id,
                                )
                            ),
                            timeout_seconds=5.0,
                            label=f"browser.set_permission:{label}:{origin}:{permission_name}",
                        )
                    except Exception as permission_error:
                        if (
                            normalized_browser_context_id is None
                            or not self._is_invalid_browser_context_error(permission_error)
                        ):
                            raise
                        await self._run_with_timeout(
                            self.browser.connection.send(
                                cdp.browser.set_permission(
                                    permission=cdp.browser.PermissionDescriptor(name=permission_name),
                                    setting=permission_setting,
                                    origin=origin,
                                )
                            ),
                            timeout_seconds=5.0,
                            label=f"browser.set_permission:{label}:{origin}:{permission_name}:fallback_default_context",
                        )
                    applied = True
            return applied
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 应用 runtime 权限画像失败 ({label}): {e}")
            return False

    async def _apply_runtime_profile_to_tab(
        self,
        tab,
        *,
        label: str,
        browser_context_id: Any = None,
        target_url: Optional[str] = None,
    ) -> bool:
        if tab is None:
            return False

        runtime_profile = self._get_runtime_surface_profile()
        runtime_signature = str(runtime_profile.get("signature") or "").strip()
        target_context_id = browser_context_id if browser_context_id is not None else self._extract_tab_browser_context_id(tab)
        applied_marker = {
            "signature": runtime_signature,
            "browser_context_id": str(target_context_id or ""),
        }
        existing_marker = getattr(tab, "_personal_runtime_profile_marker", None)
        if existing_marker == applied_marker:
            await self._apply_runtime_profile_permissions(
                label=label,
                browser_context_id=target_context_id,
                target_url=target_url,
            )
            return True

        try:
            from nodriver import cdp

            navigator_profile = dict(runtime_profile.get("navigator") or {})
            locale_profile = dict(runtime_profile.get("locale") or {})
            timezone_profile = dict(runtime_profile.get("timezone") or {})
            geolocation_profile = dict(runtime_profile.get("geolocation") or {})
            screen_profile = dict(runtime_profile.get("screen") or {})
            window_profile = dict(runtime_profile.get("window") or {})

            await self._run_with_timeout(
                tab.send(cdp.network.enable()),
                timeout_seconds=5.0,
                label=f"network.enable:{label}",
            )
            await self._run_with_timeout(
                tab.send(
                    cdp.emulation.set_user_agent_override(
                        user_agent=str(runtime_profile.get("userAgent") or navigator_profile.get("userAgent") or ""),
                        accept_language=str(runtime_profile.get("acceptLanguage") or locale_profile.get("code") or ""),
                        platform=str(navigator_profile.get("platform") or ""),
                        user_agent_metadata=self._build_runtime_user_agent_metadata(),
                    )
                ),
                timeout_seconds=5.0,
                label=f"emulation.set_user_agent_override:{label}",
            )
            await self._run_with_timeout(
                tab.send(
                    cdp.network.set_extra_http_headers(
                        cdp.network.Headers(self._build_runtime_extra_http_headers())
                    )
                ),
                timeout_seconds=5.0,
                label=f"network.set_extra_http_headers:{label}",
            )
            await self._run_with_timeout(
                tab.send(
                    cdp.emulation.set_device_metrics_override(
                        width=int(window_profile.get("innerWidth") or screen_profile.get("width") or 1280),
                        height=int(window_profile.get("innerHeight") or screen_profile.get("height") or 720),
                        device_scale_factor=float(window_profile.get("devicePixelRatio") or 1.0),
                        mobile=bool((runtime_profile.get("userAgentMetadata") or {}).get("mobile")),
                        screen_width=int(screen_profile.get("width") or 1280),
                        screen_height=int(screen_profile.get("height") or 720),
                    )
                ),
                timeout_seconds=5.0,
                label=f"emulation.set_device_metrics_override:{label}",
            )
            timezone_id = str(timezone_profile.get("id") or "").strip()
            if timezone_id:
                await self._run_with_timeout(
                    tab.send(cdp.emulation.set_timezone_override(timezone_id=timezone_id)),
                    timeout_seconds=5.0,
                    label=f"emulation.set_timezone_override:{label}",
                )
            locale_code = str(locale_profile.get("code") or "").strip()
            if locale_code:
                await self._run_with_timeout(
                    tab.send(cdp.emulation.set_locale_override(locale=locale_code)),
                    timeout_seconds=5.0,
                    label=f"emulation.set_locale_override:{label}",
                )
            if all(key in geolocation_profile for key in ("latitude", "longitude", "accuracy")):
                await self._run_with_timeout(
                    tab.send(
                        cdp.emulation.set_geolocation_override(
                            latitude=float(geolocation_profile["latitude"]),
                            longitude=float(geolocation_profile["longitude"]),
                            accuracy=float(geolocation_profile["accuracy"]),
                        )
                    ),
                    timeout_seconds=5.0,
                    label=f"emulation.set_geolocation_override:{label}",
                )
            await self._apply_runtime_profile_permissions(
                label=label,
                browser_context_id=target_context_id,
                target_url=target_url,
            )
            try:
                tab._personal_runtime_profile_marker = applied_marker
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 应用 runtime profile 失败 ({label}): {e}")
            return False

