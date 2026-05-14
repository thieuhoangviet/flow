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

class FlowClientImageMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def _make_image_generation_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        attempt_trace: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """图片生成请求使用更短超时，并在网络超时时快速重试。"""
        request_timeout = config.flow_image_request_timeout
        total_attempts = max(1, config.flow_image_timeout_retry_count + 1)
        retry_delay = config.flow_image_timeout_retry_delay

        # 对于浏览器/远程浏览器打码链路，优先保持与打码时一致的出口。
        # 否则在首跳改走媒体代理时，容易触发 reCAPTCHA 校验失败并放大长尾。
        fingerprint = self._request_fingerprint_ctx.get()
        has_fingerprint_context = bool(isinstance(fingerprint, dict) and fingerprint)

        has_media_proxy = False
        if self.proxy_manager and config.flow_image_timeout_use_media_proxy_fallback:
            try:
                has_media_proxy = bool(await self.proxy_manager.get_media_proxy_url())
            except Exception:
                has_media_proxy = False
        prefer_media_first = bool(has_media_proxy and config.flow_image_prefer_media_proxy)

        if has_fingerprint_context and prefer_media_first:
            prefer_media_first = False
            debug_logger.log_info(
                "[IMAGE] 检测到打码浏览器指纹上下文，首跳固定走打码链路；"
                "媒体代理仅在网络超时时作为兜底回退。"
            )

        last_error: Optional[Exception] = None

        for attempt_index in range(total_attempts):
            if has_media_proxy:
                # 两次重试时采用“主链路 + 备链路”策略，避免每次都先卡在错误链路上。
                if attempt_index == 0:
                    prefer_media_proxy = prefer_media_first
                elif attempt_index == 1:
                    prefer_media_proxy = not prefer_media_first
                else:
                    prefer_media_proxy = prefer_media_first
            else:
                prefer_media_proxy = False
            route_label = "媒体代理链路" if prefer_media_proxy else "打码链路"
            http_attempt_started_at = time.time()
            http_attempt_info: Optional[Dict[str, Any]] = None
            if isinstance(attempt_trace, dict):
                http_attempt_info = {
                    "attempt": attempt_index + 1,
                    "route": route_label,
                    "timeout_seconds": request_timeout,
                    "used_media_proxy": bool(prefer_media_proxy),
                }
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=request_timeout,
                    use_media_proxy=prefer_media_proxy,
                    respect_fingerprint_proxy=not prefer_media_proxy,
                )
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = True
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                return result
            except Exception as e:
                last_error = e
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = False
                    http_attempt_info["timeout_error"] = bool(self._is_timeout_error(e))
                    http_attempt_info["error"] = str(e)[:240]
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                if not self._is_timeout_error(e) or attempt_index >= total_attempts - 1:
                    raise

                if has_media_proxy and total_attempts > 1:
                    next_prefer_media_proxy = (
                        not prefer_media_proxy if attempt_index == 0 else prefer_media_proxy
                    )
                else:
                    next_prefer_media_proxy = prefer_media_proxy
                next_route_label = "媒体代理链路" if next_prefer_media_proxy else "打码链路"
                debug_logger.log_warning(
                    f"[IMAGE] 图片生成请求网络超时，准备快速重试 "
                    f"({attempt_index + 2}/{total_attempts})，当前链路={route_label}，"
                    f"下一链路={next_route_label}，timeout={request_timeout}s"
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("图片生成请求失败")

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """图片请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """通过文件头 magic bytes 检测图片 MIME 类型

        Args:
            image_bytes: 图片字节数据

        Returns:
            MIME 类型字符串，默认 image/jpeg
        """
        if len(image_bytes) < 12:
            return "image/jpeg"

        # WebP: RIFF....WEBP
        if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        # PNG: 89 50 4E 47
        if image_bytes[:4] == b'\x89PNG':
            return "image/png"
        # JPEG: FF D8 FF
        if image_bytes[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        # GIF: GIF87a 或 GIF89a
        if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        # BMP: BM
        if image_bytes[:2] == b'BM':
            return "image/bmp"
        # JPEG 2000: 00 00 00 0C 6A 50
        if image_bytes[:6] == b'\x00\x00\x00\x0cjP':
            return "image/jp2"

        return "image/jpeg"

    def _convert_to_jpeg(self, image_bytes: bytes) -> bytes:
        """将图片转换为 JPEG 格式

        Args:
            image_bytes: 原始图片字节数据

        Returns:
            JPEG 格式的图片字节数据
        """
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        # 如果有透明通道，转换为 RGB
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=95)
        return output.getvalue()

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None
    ) -> str:
        """上传图片,返回mediaId

        Args:
            at: Access Token
            image_bytes: 图片字节数据
            aspect_ratio: 图片或视频宽高比（会自动转换为图片格式）
            project_id: 项目ID（新上传接口可使用）

        Returns:
            mediaId
        """
        # 转换视频aspect_ratio为图片aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # 自动检测图片 MIME 类型
        mime_type = self._detect_image_mime_type(image_bytes)

        # 编码为base64 (去掉前缀)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        # 优先尝试新版上传接口: /v1/flow/uploadImage
        # 若失败则自动回退到旧接口,保证兼容
        ext = "png" if "png" in mime_type else "jpg"
        upload_file_name = f"flow2api_upload_{int(time.time() * 1000)}.{ext}"
        new_url = f"{self.api_base_url}/flow/uploadImage"
        normalized_project_id = str(project_id or "").strip()
        new_client_context = {
            "tool": "PINHOLE"
        }
        if normalized_project_id:
            new_client_context["projectId"] = normalized_project_id

        new_json_data = {
            "clientContext": new_client_context,
            "fileName": upload_file_name,
            "imageBytes": image_base64,
            "isHidden": False,
            "isUserUploaded": True,
            "mimeType": mime_type
        }

        # 兼容回退：旧接口 :uploadUserImage
        legacy_url = f"{self.api_base_url}:uploadUserImage"
        legacy_json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER"
            }
        }
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        captcha_method = getattr(config, "captcha_method", "personal")
        if captcha_method == "personal":
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                fingerprint = service.get_last_fingerprint()
                if not fingerprint:
                    await service.get_token(project_id, "uploadUserImage")
                    fingerprint = service.get_last_fingerprint()
                self._set_request_fingerprint(fingerprint)
            except Exception as e:
                debug_logger.log_error(f"[UPLOAD] Failed to pre-fetch fingerprint: {e}")

        for retry_attempt in range(max_retries):
            try:
                new_result = await self._make_request(
                    method="POST",
                    url=new_url,
                    json_data=new_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True
                )
                media_id = (
                    new_result.get("media", {}).get("name")
                    or new_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                )
                if media_id:
                    return media_id
                raise Exception(f"Invalid upload response: missing media id, keys={list(new_result.keys())}")
            except Exception as new_upload_error:
                last_error = new_upload_error
                retry_reason = "网络超时" if self._is_timeout_error(new_upload_error) else self._get_retry_reason(str(new_upload_error))

                # 旧接口不携带 projectId，带项目上下文的上传一旦回退就可能把图片挂到错误项目。
                if normalized_project_id:
                    if retry_reason and retry_attempt < max_retries - 1:
                        debug_logger.log_warning(
                            f"[UPLOAD] Project-scoped upload 遇到{retry_reason}，准备重试新版接口 "
                            f"({retry_attempt + 2}/{max_retries}, project_id={normalized_project_id})..."
                        )
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        "Project-scoped image upload failed via /flow/uploadImage; "
                        "legacy :uploadUserImage fallback is disabled because it may attach media "
                        f"to a different project (project_id={normalized_project_id})."
                    ) from new_upload_error

                debug_logger.log_warning(
                    f"[UPLOAD] New upload API failed, fallback to legacy endpoint: {new_upload_error}"
                )

            try:
                legacy_result = await self._make_request(
                    method="POST",
                    url=legacy_url,
                    json_data=legacy_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True
                )

                media_id = (
                    legacy_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                    or legacy_result.get("media", {}).get("name")
                )
                if media_id:
                    return media_id
                raise Exception(f"Legacy upload response missing media id: keys={list(legacy_result.keys())}")
            except Exception as legacy_upload_error:
                last_error = legacy_upload_error
                retry_reason = self._get_retry_reason(str(legacy_upload_error))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[UPLOAD] 上传遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("上传图片失败")

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        """生成图片(同步返回)

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_name: NARWHAL / GEM_PIX / GEM_PIX_2 / IMAGEN_3_5
            aspect_ratio: 图片宽高比
            image_inputs: 参考图片列表(图生图时使用)

        Returns:
            (result, session_id, perf_trace)
            result: 上游返回的生成结果
            session_id: 本次成功图片生成请求使用的 sessionId
            perf_trace: 生成重试与链路耗时轨迹
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        # 403/reCAPTCHA 重试逻辑
        max_retries = config.flow_max_retries
        last_error = None
        perf_trace: Dict[str, Any] = {
            "max_retries": max_retries,
            "generation_attempts": [],
        }
        
        for retry_attempt in range(max_retries):
            attempt_trace: Dict[str, Any] = {
                "attempt": retry_attempt + 1,
                "recaptcha_ok": False,
            }
            attempt_started_at = time.time()
            # 每次重试都重新获取 reCAPTCHA token
            recaptcha_started_at = time.time()
            if progress_callback is not None:
                await progress_callback("solving_image_captcha", 38)
            launch_gate_acquired = False
            launch_ok, launch_queue_ms, launch_stagger_ms = await self._acquire_image_launch_gate(
                token_id=token_id,
                token_image_concurrency=token_image_concurrency,
            )
            attempt_trace["launch_queue_ms"] = launch_queue_ms
            attempt_trace["launch_stagger_ms"] = launch_stagger_ms
            if not launch_ok:
                last_error = Exception("Image launch queue wait timeout")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="IMAGE_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_image_launch_gate(token_id)
            attempt_trace["recaptcha_ms"] = int((time.time() - recaptcha_started_at) * 1000)
            attempt_trace["recaptcha_ok"] = bool(recaptcha_token)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            if progress_callback is not None:
                await progress_callback("submitting_image", 48)
            session_id = self._generate_session_id()

            # 构建请求 - 新版接口在外层和 requests 内都带 clientContext
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE"
            }

            # 新版图片接口使用结构化提示词 + new media 开关
            request_data = {
                "clientContext": client_context,
                "seed": random.randint(1, 999999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "structuredPrompt": {
                    "parts": [{
                        "text": prompt
                    }]
                },
                "imageInputs": image_inputs or []
            }

            json_data = {
                "clientContext": client_context,
                "mediaGenerationContext": {
                    "batchId": str(uuid.uuid4())
                },
                "useNewMedia": True,
                "requests": [request_data]
            }

            try:
                result = await self._make_image_generation_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    attempt_trace=attempt_trace,
                )
                attempt_trace["success"] = True
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                perf_trace["final_success_attempt"] = retry_attempt + 1
                return result, session_id, perf_trace
            except Exception as e:
                last_error = e
                attempt_trace["success"] = False
                attempt_trace["error"] = str(e)[:240]
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        perf_trace["final_success_attempt"] = None
        raise last_error

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None
    ) -> str:
        """放大图片到 2K/4K

        Args:
            at: Access Token
            project_id: 项目ID
            media_id: 图片的 mediaId (从 batchGenerateImages 返回的 media[0]["name"])
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K 或 UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: 用户等级 (如 PAYGATE_TIER_NOT_PAID / PAYGATE_TIER_ONE)
            session_id: 可选，复用图片生成请求的 sessionId

        Returns:
            base64 编码的图片数据
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 403/reCAPTCHA/500 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None

        for retry_attempt in range(max_retries):
            # 获取 reCAPTCHA token - 使用 IMAGE_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(
                project_id,
                action="IMAGE_GENERATION",
                token_id=token_id
            )
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise last_error
            upsample_session_id = session_id or self._generate_session_id()

            json_data = {
                "mediaId": media_id,
                "targetResolution": target_resolution,
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": upsample_session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                }
            }

            # 4K/2K 放大使用专用超时，因为返回的 base64 数据量很大
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=config.upsample_timeout
                )

                # 返回 base64 编码的图片
                return result.get("encodedImage", "")
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        raise last_error

