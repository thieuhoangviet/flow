import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import ssl
from typing import TYPE_CHECKING, Any, Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote
import urllib.error
import urllib.request
from curl_cffi.requests import AsyncSession
from src.core.logger import debug_logger
from src.core.config import config, get_yescaptcha_min_score
try:
    import httpx
except ImportError:
    pass

class FlowClientBaseMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
        # 使用账号ID作为随机种子，确保同一账号生成相同的UA
        import hashlib
        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # Chrome 版本池 - 匹配真实 Mac mini Chrome 147 环境
        chrome_versions = ["147.0.7727.56", "146.0.7688.92", "145.0.7649.100"]
        ch_version = rng.choice(chrome_versions)
        user_agent = f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ch_version} Safari/537.36"
        
        # 缓存结果
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """设置当前请求链路的浏览器指纹上下文。"""
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """获取当前请求链路绑定的浏览器指纹快照。"""
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def clear_request_fingerprint(self):
        """清理请求链路绑定的浏览器指纹。"""
        self._set_request_fingerprint(None)

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        allow_urllib_fallback: bool = True
    ) -> Dict[str, Any]:
        """统一HTTP请求处理

        Args:
            method: HTTP方法 (GET/POST)
            url: 完整URL
            headers: 请求头
            json_data: JSON请求体
            use_st: 是否使用ST认证 (Cookie方式)
            st_token: Session Token
            use_at: 是否使用AT认证 (Bearer方式)
            at_token: Access Token
            timeout: 自定义超时时间(秒)，不传则使用默认值
            use_media_proxy: 是否使用图片上传/下载代理
            respect_fingerprint_proxy: 是否优先使用打码浏览器指纹里的代理
            allow_urllib_fallback: curl_cffi 网络失败时是否允许 urllib 二次兜底
        """
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if use_media_proxy and hasattr(self.proxy_manager, "get_media_proxy_url"):
                    proxy_url = await self.proxy_manager.get_media_proxy_url()
                elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if respect_fingerprint_proxy and isinstance(fingerprint, dict) and "proxy_url" in fingerprint:
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        # ST认证 - 使用Cookie
        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        # AT认证 - 使用Bearer
        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        # 确定账号标识（优先使用 token 的前16个字符作为标识）
        account_id = None
        if st_token:
            account_id = st_token[:16]  # 使用 ST 的前16个字符
        elif at_token:
            account_id = at_token[:16]  # 使用 AT 的前16个字符

        # 通用请求头 - 优先使用打码浏览器指纹中的 UA
        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        headers.update({
            "Content-Type": "application/json",
            "User-Agent": fingerprint_user_agent or self._generate_user_agent(account_id)
        })

        # 若存在打码浏览器指纹，覆盖关键客户端提示头，保证提交请求与打码时一致。
        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = fingerprint["sec_ch_ua"]
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]

        # Add default Chromium/Android client headers (do not override explicitly provided values).
        for key, value in self._default_client_headers.items():
            headers.setdefault(key, value)

        # Dynamic fix for sec-ch-ua headers when fingerprint is missing to avoid UA/Platform mismatch
        if not isinstance(fingerprint, dict) or not fingerprint.get("sec_ch_ua_platform"):
            ua_lower = headers.get("User-Agent", "").lower()
            if "android" in ua_lower:
                headers["sec-ch-ua-platform"] = "\"Android\""
                headers["sec-ch-ua-mobile"] = "?1"
            elif "mac" in ua_lower:
                headers["sec-ch-ua-platform"] = "\"macOS\""
                headers["sec-ch-ua-mobile"] = "?0"
            elif "linux" in ua_lower or "x11" in ua_lower:
                headers["sec-ch-ua-platform"] = "\"Linux\""
                headers["sec-ch-ua-mobile"] = "?0"
            else:
                headers["sec-ch-ua-platform"] = "\"Windows\""
                headers["sec-ch-ua-mobile"] = "?0"

        # Log request
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] 使用打码浏览器指纹提交请求: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=json_data,
                proxy=proxy_url
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate="chrome124"
                    )
                else:  # POST
                    response = await session.post(
                        url,
                        headers=headers,
                        json=json_data,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate="chrome124"
                    )

                duration_ms = (time.time() - start_time) * 1000

                # Log response
                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms
                    )

                # 检查HTTP错误
                if response.status_code >= 400:
                    # 解析错误响应
                    error_reason = f"HTTP Error {response.status_code}"
                    try:
                        error_body = response.json()
                        # 提取 Google API 错误格式中的 reason
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            # 从 details 中提取 reason
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except:
                        error_reason = f"HTTP Error {response.status_code}: {response.text[:200]}"
                    
                    # 失败时输出请求体和错误内容到控制台
                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(f"[API FAILED] Request Body: {json_data}")
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    
                    raise Exception(error_reason)

                return response.json()

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)

            # 如果不是我们自己抛出的异常，记录日志
            if "HTTP Error" not in error_msg and not any(x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(f"[API FAILED] Request Body: {json_data}")
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            if allow_urllib_fallback and self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"[HTTP FALLBACK] curl_cffi 请求失败，回退 urllib: {method.upper()} {url}"
                )
                try:
                    return await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib 回退也失败: {fallback_error}"
                    )
                    raise Exception(
                        f"Flow API request failed: curl={error_msg}; urllib={fallback_error}"
                    )

            raise Exception(f"Flow API request failed: {error_msg}")

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """判断是否应从 curl_cffi 回退到 urllib。"""
        error_lower = (error_message or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
                "curl: (6)",
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (52)",
                "curl: (56)",
                "connection timed out",
                "could not connect",
                "failed to connect",
                "ssl connect error",
                "tls connect error",
                "network is unreachable",
            ]
        )

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """使用 urllib 执行 JSON 请求，作为 curl_cffi 的网络回退。"""
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")

        data = None
        if method.upper() != "GET" and json_data is not None:
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": proxy_url, "https": proxy_url}
                )
            )

        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url=url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with opener.open(
                request,
                timeout=timeout,
            ) as response:
                payload = response.read()
                status_code = int(response.getcode() or 0)
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status_code = int(getattr(exc, "code", 500) or 500)
            body_text = payload.decode("utf-8", errors="replace")
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise Exception(str(exc)) from exc

        body_text = payload.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}")

        try:
            return json.loads(body_text) if body_text else {}
        except Exception as exc:
            raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc

    def _is_timeout_error(self, error: Exception) -> bool:
        """判断是否为网络超时，便于快速失败重试。"""
        error_lower = str(error).lower()
        return any(keyword in error_lower for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
        ])

    def _is_proxy_connection_error(self, error: Exception) -> bool:
        """识别本地/上游代理不可用导致的连接失败。"""
        error_lower = str(error).lower()
        return any(keyword in error_lower for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
        ])

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """识别可重试的 TLS/连接类网络错误。"""
        error_lower = (error_str or "").lower()
        return any(keyword in error_lower for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
        ])

    def _get_control_plane_timeout(self) -> int:
        """控制轻量控制面请求的超时，避免认证/项目接口长时间挂起。"""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    def _get_video_submit_timeout(self) -> int:
        """视频提交接口应快速返回 operation，避免单次网络挂死拖满整条链路。"""
        return max(30, min(int(self.timeout or 0) or 120, 75))

    def _get_video_poll_timeout(self) -> int:
        """视频状态查询是轻量轮询，请求超时不应超过下一轮轮询太久。"""
        return max(10, min(int(self.timeout or 0) or 120, 45))

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        """统一处理生成链路的重试判定与打码自愈通知。"""
        error_str = str(error)
        retry_reason = self._get_retry_reason(error_str)
        notify_reason = retry_reason or error_str[:120] or type(error).__name__
        await self._notify_browser_captcha_error(
            browser_id=browser_id,
            project_id=project_id,
            error_reason=notify_reason,
            error_message=error_str,
        )
        if not retry_reason:
            return False

        is_terminal_attempt = retry_attempt >= max_retries - 1

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}遇到{retry_reason}，已达到最大重试次数({max_retries})，本次请求失败并执行关闭回收。"
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})..."
        )
        await asyncio.sleep(1)
        return True

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """判断是否需要重试，返回日志提示内容"""
        error_lower = error_str.lower()
        if "403" in error_lower:
            return "403错误"
        if "429" in error_lower or "too many requests" in error_lower:
            return "429限流"
        if self._is_retryable_network_error(error_str):
            return "网络/TLS错误"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA 验证失败"
        if "recaptcha" in error_lower:
            return "reCAPTCHA 错误"
        if any(keyword in error_lower for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            "\"reason\":\"internal\"",
            "server error",
            "upstream error",
        ]):
            return "500/内部错误"
        return None

    def _generate_session_id(self) -> str:
        """生成sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """生成sceneId: UUID"""
        return str(uuid.uuid4())

