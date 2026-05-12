"""Admin utility / helper functions (no endpoints)."""
import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..core.config import config

try:
    import httpx
except ImportError:
    httpx = None


SUPPORTED_API_CAPTCHA_METHODS = {"yescaptcha", "capmonster", "ezcaptcha", "capsolver"}


def _mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 24:
        return token
    return f"{token[:18]}...{token[-8:]}"


def _truncate_text(text: Any, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit - 3]}..."


def _extract_error_summary(payload: Any) -> str:
    """Extract a human-readable error summary from a response body."""
    if payload is None:
        return ""

    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            return _extract_error_summary(json.loads(raw))
        except Exception:
            return _truncate_text(raw)

    if isinstance(payload, dict):
        for key in ("error_summary", "error_message", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value)

        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "reason", "code"):
                value = error_value.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate_text(value)
        elif isinstance(error_value, str) and error_value.strip():
            return _truncate_text(error_value)

        for nested_key in ("response", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, (dict, list, str)):
                summary = _extract_error_summary(nested)
                if summary:
                    return summary

        return ""

    if isinstance(payload, list):
        for item in payload:
            summary = _extract_error_summary(item)
            if summary:
                return summary
        return ""

    return _truncate_text(payload)


def _guess_client_hints_from_user_agent(user_agent: str) -> Dict[str, str]:
    """Populate common sec-ch-* headers from a User-Agent string."""
    ua = (user_agent or "").strip()
    if not ua:
        return {}

    headers: Dict[str, str] = {}
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    is_mobile = any(token in ua for token in ("Android", "iPhone", "iPad", "Mobile"))
    headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"

    if "Windows" in ua:
        headers["sec-ch-ua-platform"] = '"Windows"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        headers["sec-ch-ua-platform"] = '"macOS"'
    elif "Android" in ua:
        headers["sec-ch-ua-platform"] = '"Android"'
    elif "iPhone" in ua or "iPad" in ua:
        headers["sec-ch-ua-platform"] = '"iOS"'
    elif "Linux" in ua:
        headers["sec-ch-ua-platform"] = '"Linux"'

    if major_match:
        major = major_match.group(1)
        if "Edg/" in ua:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Microsoft Edge";v="{major}", "Chromium";v="{major}"'
            )
        else:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
            )

    return headers


def _guess_impersonate_from_user_agent(user_agent: str) -> str:
    """Pick a curl_cffi impersonate version from the UA."""
    ua = (user_agent or "").strip()
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    if not major_match:
        return "chrome120"

    try:
        major = int(major_match.group(1))
    except Exception:
        return "chrome120"

    if major >= 124:
        return "chrome124"
    if major >= 120:
        return "chrome120"
    return "chrome120"


def _build_proxy_map(proxy_url: str) -> Optional[Dict[str, str]]:
    normalized = (proxy_url or "").strip()
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def _normalize_http_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("远程打码服务地址未配置")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("远程打码服务地址格式错误，必须是 http(s)://host[:port]")

    return normalized


def _get_remote_browser_client_config() -> tuple:
    base_url = _normalize_http_base_url(config.remote_browser_base_url)
    api_key = (config.remote_browser_api_key or "").strip()
    if not api_key:
        raise RuntimeError("远程打码服务 API Key 未配置")
    timeout = max(5, int(config.remote_browser_timeout or 60))
    return base_url, api_key, timeout


def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
    read_value = max(3.0, float(read_timeout))
    write_value = min(10.0, max(3.0, read_value))
    if httpx is None:
        return read_value
    return httpx.Timeout(
        connect=2.5,
        read=read_value,
        write=write_value,
        pool=2.5,
    )


def _parse_json_response_text(text: str) -> Optional[Any]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


async def _stdlib_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_data: Optional[bytes] = None

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_data = json.dumps(payload).encode("utf-8")

    def do_request() -> tuple:
        request = urllib.request.Request(
            url=url,
            data=request_data,
            headers=req_headers,
            method=request_method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return status_code, body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            charset = exc.headers.get_content_charset() if exc.headers else None
            return int(getattr(exc, "code", 0) or 0), body.decode(charset or "utf-8", errors="replace")

    try:
        status_code, text = await asyncio.to_thread(do_request)
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e

    return status_code, _parse_json_response_text(text), text


async def _sync_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_kwargs: Dict[str, Any] = {
        "headers": req_headers,
        "timeout": _build_remote_browser_http_timeout(timeout),
    }

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_kwargs["json"] = payload

    if httpx is None:
        return await _stdlib_json_http_request(
            method=method,
            url=url,
            headers=req_headers,
            payload=payload,
            timeout=timeout,
        )

    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
            response = await session.request(
                method=request_method,
                url=url,
                **request_kwargs,
            )
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e

    status_code = int(getattr(response, "status_code", 0) or 0)
    text = response.text or ""
    parsed = _parse_json_response_text(text)

    return status_code, parsed, text
