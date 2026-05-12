import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import ssl
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
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

class FlowClientAuthMixin:
    async def st_to_at(self, st: str) -> dict:
        """ST转AT

        Args:
            st: Session Token

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...}
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        try:
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
            )
        except Exception as e:
            if not self._is_proxy_connection_error(e):
                raise

            debug_logger.log_warning(
                f"[AUTH] ST->AT failed via configured proxy, retrying direct connection: {e}"
            )
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
                force_no_proxy=True,
            )

    async def get_credits(self, at: str) -> dict:
        """查询余额

        Args:
            at: Access Token

        Returns:
            {
                "credits": 920,
                "userPaygateTier": "PAYGATE_TIER_ONE"
            }
        """
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=self._get_control_plane_timeout(),
        )
        return result

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """通知浏览器打码服务执行失败自愈。
        
        Args:
            browser_id: browser 模式使用的浏览器 ID
            project_id: personal 模式使用的 project_id
            error_reason: 已归类的错误原因
            error_message: 原始错误文本
        """
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(
                    browser_id,
                    error_reason=error_reason or error_message or "upstream_error"
                )
            except Exception:
                pass
        elif config.captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService
                service = await ExtensionCaptchaService.get_instance()
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif config.captcha_method == "personal" and project_id:
            try:
                from src.services.browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={"error_reason": error_reason or error_message or "upstream_error"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] 上报 error 失败: {e}")

    async def _notify_browser_captcha_request_finished(self, browser_id: Optional[Union[int, str]] = None):
        """通知有头浏览器：上游图片/视频请求已结束，可关闭对应打码浏览器。"""
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_request_finished(browser_id)
            except Exception:
                pass
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] 上报 finish 失败: {e}")

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser 服务地址未配置")
        if not api_key:
            raise RuntimeError("remote_browser API Key 未配置")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser 服务地址格式错误")

        return base_url, api_key, timeout

    @staticmethod
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

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    async def _stdlib_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_data: Optional[bytes] = None

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_data = json.dumps(payload).encode("utf-8")

        def do_request() -> tuple[int, str]:
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
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        return status_code, FlowClient._parse_json_response_text(text), text

    @staticmethod
    async def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_kwargs: Dict[str, Any] = {
            "headers": req_headers,
            "timeout": FlowClient._build_remote_browser_http_timeout(timeout),
        }

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_kwargs["json"] = payload

        if httpx is None:
            return await FlowClient._stdlib_json_http_request(
                method=method,
                url=url,
                headers=req_headers,
                payload=payload,
                timeout=timeout,
            )

        try:
            # remote_browser 控制面只需要稳定传输 JSON，不需要浏览器指纹伪装。
            # 使用 httpx 可以避免 curl_cffi 在当前环境下 POST body 被吞掉。
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
                response = await session.request(
                    method=request_method,
                    url=url,
                    **request_kwargs,
                )
        except Exception as e:
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = response.text or ""
        parsed = FlowClient._parse_json_response_text(text)

        return status_code, parsed, text

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser 请求失败: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser 返回格式错误")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """让本地 remote_browser 服务提前开始补池，尽量把取 token 等待搬到前面。"""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}|{int(token_id or 0)}"
        now_value = time.monotonic()
        last_sent = float(self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0)
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                    "token_id": token_id,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill 失败: {e}")
            return False

    async def prefill_remote_browser_for_tokens(self, tokens: List[Any], action: str = "IMAGE_GENERATION") -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # 这里只是拿 reCAPTCHA token，不应该跟整条生成链路共用数百秒级超时。
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """获取reCAPTCHA token - 支持多种打码方式
        
        Args:
            project_id: 项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大
            token_id: 当前业务 token id（browser 模式下用于读取 token 级打码代理）
        
        Returns:
            (token, browser_id) 元组。
            - browser 模式: browser_id 为本地浏览器 ID
            - remote_browser 模式: browser_id 为远程 session_id
            - 其他模式: browser_id 为 None
        """
        captcha_method = config.captcha_method
        debug_logger.log_info(f"[reCAPTCHA] 开始获取 token: method={captcha_method}, project_id={project_id}, action={action}")

        if captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService
                service = await ExtensionCaptchaService.get_instance(self.db)
                extension_timeout = 45 if action == "VIDEO_GENERATION" else 25
                token = await service.get_token(
                    project_id,
                    action,
                    timeout=extension_timeout,
                    token_id=token_id
                )
                self._set_request_fingerprint(None)
                return token, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Extension] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None

        # 内置浏览器打码 (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] 使用 personal 模式")
            try:
                from src.services.browser_captcha_personal import BrowserCaptchaService
                debug_logger.log_info(f"[reCAPTCHA] 导入 BrowserCaptchaService 成功")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(f"[reCAPTCHA] 获取服务实例成功，准备调用 get_token")
                get_token_with_metadata = getattr(service, "get_token_with_metadata", None)
                if callable(get_token_with_metadata):
                    token, _slot_id, _cookie_source_token_id = await get_token_with_metadata(
                        project_id,
                        action,
                        token_id=token_id,
                    )
                else:
                    token = await service.get_token(project_id, action, token_id=token_id)
                debug_logger.log_info(f"[reCAPTCHA] get_token 返回: {token[:50] if token else None}...")
                fingerprint = service.get_last_fingerprint() if token else None
                self._set_request_fingerprint(fingerprint if token else None)
                return token, None
            except RuntimeError as e:
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                print(f"[reCAPTCHA] ❌ 内置浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 导入失败: {str(e)}")
                print(f"[reCAPTCHA] ❌ nodriver 未安装，请运行: pip install nodriver")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                import traceback
                with open("debug_error.txt", "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
                debug_logger.log_error(f"[reCAPTCHA Personal] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # 有头浏览器打码 (playwright)
        elif captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                token, browser_id = await service.get_token(project_id, action, token_id=token_id)
                fingerprint = await service.get_fingerprint(browser_id) if token else None
                self._set_request_fingerprint(fingerprint if token else None)
                return token, browser_id
            except RuntimeError as e:
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Browser] {error_msg}")
                print(f"[reCAPTCHA] ❌ 有头浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] 导入失败: {str(e)}")
                print(f"[reCAPTCHA] ❌ playwright 未安装，请运行: pip install playwright && python -m playwright install chromium")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                        "token_id": token_id,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                fingerprint = payload.get("fingerprint") if isinstance(payload.get("fingerprint"), dict) else None
                self._set_request_fingerprint(fingerprint if token else None)
                if not token or not session_id:
                    raise RuntimeError(f"remote_browser 返回缺少 token/session_id: {payload}")
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # API打码服务
        elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
            # 为 API 打码也设置指纹（包含代理），确保 token 获取和后续请求环境一致
            if self.proxy_manager:
                try:
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                    if proxy_url:
                        self._set_request_fingerprint({"proxy_url": proxy_url})
                    else:
                        self._set_request_fingerprint(None)
                except Exception as e:
                    debug_logger.log_warning(f"[reCAPTCHA] Failed to get proxy for API captcha: {e}")
                    self._set_request_fingerprint(None)
            else:
                self._set_request_fingerprint(None)
            token = await self._get_api_captcha_token(captcha_method, project_id, action)
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] 未知的打码方式: {captcha_method}")
            self._set_request_fingerprint(None)
            return None, None

    async def _get_api_captcha_token(self, method: str, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """通用API打码服务
        
        Args:
            method: 打码服务类型
            project_id: 项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)
        """
        # 获取配置
        if method == "yescaptcha":
            client_key = config.yescaptcha_api_key
            base_url = config.yescaptcha_base_url
            # Google Labs uses reCAPTCHA Enterprise — prefer Enterprise task type
            task_type = "RecaptchaV3EnterpriseTask"
            min_score = None
        elif method == "capmonster":
            client_key = config.capmonster_api_key
            base_url = config.capmonster_base_url
            task_type = "RecaptchaV3TaskProxyless"
            min_score = None
        elif method == "ezcaptcha":
            client_key = config.ezcaptcha_api_key
            base_url = config.ezcaptcha_base_url
            task_type = "ReCaptchaV3TaskProxylessS9"
            min_score = None
        elif method == "capsolver":
            client_key = config.capsolver_api_key
            base_url = config.capsolver_base_url
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
            min_score = None
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            return None

        if not client_key:
            debug_logger.log_info(f"[reCAPTCHA] {method} API key not configured, skipping")
            return None

        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        page_action = action

        try:
            # 获取代理配置，让打码API请求也走代理
            # 注意：curl_cffi 对 SOCKS5 使用 proxy 参数，HTTP 代理使用 proxies 参数
            proxy = None
            proxies = None
            if self.proxy_manager:
                try:
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                    if proxy_url:
                        if proxy_url.startswith("socks5://"):
                            # curl_cffi 对 SOCKS5 使用 proxy 参数
                            proxy = proxy_url
                        else:
                            # HTTP/HTTPS 代理使用 proxies 字典
                            proxies = {"http": proxy_url, "https": proxy_url}
                except Exception as e:
                    debug_logger.log_warning(f"[reCAPTCHA {method}] Failed to get proxy: {e}")
            
            async with AsyncSession() as session:
                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action
                    }
                }
                if min_score is not None:
                    create_data["task"]["minScore"] = min_score

                if proxy:
                    result = await session.post(create_url, json=create_data, impersonate="chrome124", proxy=proxy)
                else:
                    result = await session.post(create_url, json=create_data, impersonate="chrome124", proxies=proxies)

                debug_logger.log_info(f"[reCAPTCHA {method}] createTask response status: {result.status_code}")
                result_json = result.json()
                task_id = result_json.get('taskId')

                debug_logger.log_info(f"[reCAPTCHA {method}] created task_id: {task_id}, response: {result_json}")

                if not task_id:
                    error_desc = result_json.get('errorDescription', 'Unknown error')
                    debug_logger.log_error(f"[reCAPTCHA {method}] Failed to create task: {error_desc}")
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {
                        "clientKey": client_key,
                        "taskId": task_id
                    }
                    # 根据代理类型使用不同参数
                    if proxy:
                        result = await session.post(get_url, json=get_data, impersonate="chrome124", proxy=proxy)
                    else:
                        result = await session.post(get_url, json=get_data, impersonate="chrome124", proxies=proxies)
                    result_json = result.json()

                    debug_logger.log_info(f"[reCAPTCHA {method}] polling #{i+1}: {result_json}")

                    status = result_json.get('status')
                    if status == 'ready':
                        solution = result_json.get('solution', {})
                        response = solution.get('gRecaptchaResponse')
                        if response:
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token获取成功")
                            return response

                    await asyncio.sleep(3)

                debug_logger.log_error(f"[reCAPTCHA {method}] Timeout waiting for token")
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            return None

