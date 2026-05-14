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

class FlowClientVideoMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def _make_video_api_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        timeout: int,
    ) -> Dict[str, Any]:
        """视频 API 加硬截止，避免 curl_cffi 底层偶发卡住导致整条请求悬挂。"""
        try:
            result = await asyncio.wait_for(
                self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=timeout,
                    allow_urllib_fallback=False
                ),
                timeout=timeout + 5
            )
            
            if not result.get("operations"):
                from src.core.logger import debug_logger
                import json as _json
                debug_logger.log_error(f"[VIDEO API FAILED] URL: {url}")
                debug_logger.log_error(f"[VIDEO API FAILED] Request JSON: {_json.dumps(json_data, indent=2, ensure_ascii=False)}")
                debug_logger.log_error(f"[VIDEO API FAILED] Response JSON: {_json.dumps(result, indent=2, ensure_ascii=False)}")
                
            return result
        except asyncio.TimeoutError as exc:
            raise Exception(f"Flow video API request timed out after {timeout}s") from exc

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """视频请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    def _build_video_text_input(self, prompt: str, use_v2_model_config: bool = False) -> Dict[str, Any]:
        if use_v2_model_config:
            return {
                "structuredPrompt": {
                    "parts": [{
                        "text": prompt
                    }]
                }
            }
        return {
            "prompt": prompt
        }

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """文生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_t2v_fast 等
            aspect_ratio: 视频宽高比
            user_paygate_tier: 用户等级

        Returns:
            {
                "operations": [{
                    "operation": {"name": "task_id"},
                    "sceneId": "uuid",
                    "status": "MEDIA_GENERATION_STATUS_PENDING"
                }],
                "remainingCredits": 900
            }
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                "videoModelKey": model_key,
                "metadata": {
                    "sceneId": scene_id
                }
            }
            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }
            if use_v2_model_config:
                json_data["mediaGenerationContext"] = {
                    "batchId": str(uuid.uuid4())
                }
                json_data["useV2ModelConfig"] = True

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout()
                )
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """图生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_r2v_fast_landscape
            aspect_ratio: 视频宽高比
            reference_images: 参考图片列表 [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            batch_id = str(uuid.uuid4())
            scene_id = str(uuid.uuid4())

            json_data = {
                "mediaGenerationContext": {
                    "batchId": batch_id
                },
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "structuredPrompt": {
                            "parts": [{
                                "text": prompt
                            }]
                        }
                    },
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }],
                "useV2ModelConfig": True
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout()
                )
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """收尾帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            end_media_id: 结束帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartAndEndImage"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                "videoModelKey": model_key,
                "startImage": {
                    "mediaId": start_media_id
                },
                "endImage": {
                    "mediaId": end_media_id
                },
                "metadata": {
                    "sceneId": scene_id
                }
            }
            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }
            if use_v2_model_config:
                json_data["mediaGenerationContext"] = {
                    "batchId": str(uuid.uuid4())
                }
                json_data["useV2ModelConfig"] = True

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout()
                )
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """仅首帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl等
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartImage"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                "videoModelKey": model_key,
                "startImage": {
                    "mediaId": start_media_id
                },
                # 注意: 没有endImage字段,只用首帧
                "metadata": {
                    "sceneId": scene_id
                }
            }
            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }
            if use_v2_model_config:
                json_data["mediaGenerationContext"] = {
                    "batchId": str(uuid.uuid4())
                }
                json_data["useV2ModelConfig"] = True

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout()
                )
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_extend(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        video_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """视频续写,基于已生成的视频延伸7秒

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 续写提示词
            model_key: veo_3_1_extend_portrait / veo_3_1_extend 等
            aspect_ratio: 视频宽高比
            video_media_id: 源视频的 mediaGenerationId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text (operations 列表)
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoExtendVideo"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = 3
        last_error = None

        for retry_attempt in range(max_retries):
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 续写",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            workflow_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "mediaGenerationContext": {
                    "batchId": str(uuid.uuid4())
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "structuredPrompt": {
                            "parts": [{"text": prompt}]
                        }
                    },
                    "videoInput": {
                        "mediaId": video_media_id
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "workflowId": workflow_id
                    }
                }],
                "useV2ModelConfig": True
            }

            # Debug: 打印请求体用于调试
            import json as _json
            debug_logger.log_info(f"[VIDEO EXTEND] Request URL: {url}")
            debug_logger.log_info(f"[VIDEO EXTEND] Request JSON: {_json.dumps(json_data, indent=2, ensure_ascii=False)[:2000]}")

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout()
                )
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 续写",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        # 所有重试都失败
        raise last_error

    async def run_concatenation(
        self,
        at: str,
        original_media_id: str,
        extend_media_id: str,
    ) -> dict:
        """
        调用 Google runVideoFxConcatenation API 拼接视频
        
        Args:
            at: 认证 token
            original_media_id: 原始视频的 mediaGenerationId (UUID)
            extend_media_id: 续写视频的 mediaGenerationId (UUID)
        
        Returns:
            包含 operation name 的字典
        """
        url = f"{self.api_base_url}:runVideoFxConcatenation"
        
        json_data = {
            "inputVideos": [
                {
                    "mediaGenerationId": original_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "0s",
                    "endTimeOffset": "8s"
                },
                {
                    "mediaGenerationId": extend_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "1s",
                    "endTimeOffset": "8s"
                }
            ]
        }
        
        debug_logger.log_info(f"[CONCAT] 提交拼接任务: original={original_media_id[:12]}..., extend={extend_media_id[:12]}...")
        
        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )
        debug_logger.log_info(f"[CONCAT] 拼接任务已提交: {json.dumps(result, ensure_ascii=False)[:300]}")
        return result

    async def poll_concatenation_status(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """
        轮询拼接任务状态，直到完成或超时
        
        Args:
            at: 认证 token
            operation_name: 拼接任务的 operation name
            timeout: 超时秒数
            poll_interval: 轮询间隔秒数
        
        Returns:
            包含 outputUri 和 mediaGenerationId 的字典
        """
        url = f"{self.api_base_url}:runVideoFxCheckConcatenationStatus"
        json_data = {
            "operation": {
                "operation": {
                    "name": operation_name
                }
            }
        }
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            result = await self._make_request(
                method="POST",
                url=url,
                json_data=json_data,
                use_at=True,
                at_token=at,
                timeout=300,  # concat API returns base64 video (~14MB), needs longer timeout
            )
            
            status = result.get("status", "")
            output_uri = result.get("outputUri", "")
            encoded_video = result.get("encodedVideo", "")
            
            ev_len = len(encoded_video) if encoded_video else 0
            elapsed = int(time.time() - start_time)
            all_keys = list(result.keys())
            debug_logger.log_info(
                f"[CONCAT] 状态: {status}, outputUri={'yes' if output_uri else 'no'}, "
                f"encodedVideo={ev_len} chars, elapsed={elapsed}s, keys={all_keys}"
            )
            
            # 优先检查 outputUri
            if output_uri:
                debug_logger.log_info(f"[CONCAT] 拼接完成 (outputUri): {output_uri[:120]}")
                return result
            
            # Google API 返回 encodedVideo（base64 编码的 MP4）而不是 outputUri
            if encoded_video and "SUCCESSFUL" in status:
                try:
                    import os
                    video_bytes = base64.b64decode(encoded_video)
                    video_filename = f"concat_{uuid.uuid4().hex[:12]}.mp4"
                    
                    # 保存到 tmp/ 目录（FastAPI 已挂载为 /tmp 静态文件）
                    save_dir = "tmp"
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, video_filename)
                    
                    with open(save_path, "wb") as f:
                        f.write(video_bytes)
                    
                    # 构造 URL：FastAPI 挂载了 /tmp -> /app/tmp/
                    serve_url = f"/tmp/{video_filename}"
                    debug_logger.log_info(f"[CONCAT] 拼接完成 (encodedVideo): 保存 {len(video_bytes)} bytes -> {serve_url}")
                    
                    result["outputUri"] = serve_url
                    result["local_file"] = save_path
                    return result
                except Exception as e:
                    debug_logger.log_error(f"[CONCAT] 解码 encodedVideo 失败: {e}")
                    raise Exception(f"解码拼接视频失败: {e}")
            
            # SUCCESSFUL but neither outputUri nor encodedVideo
            if "SUCCESSFUL" in status:
                debug_logger.log_warning(f"[CONCAT] SUCCESSFUL 但无 outputUri/encodedVideo: {json.dumps(result, ensure_ascii=False)[:300]}")

            if "FAILED" in status or "ERROR" in status:
                debug_logger.log_error(f"[CONCAT] 失败: {status}, 响应: {json.dumps(result, ensure_ascii=False)[:300]}")
                raise Exception(f"视频拼接失败: {status}")
            
            await asyncio.sleep(poll_interval)
        
        debug_logger.log_error(f"[CONCAT] 超时 ({timeout}s)，放弃拼接")
        raise Exception(f"视频拼接超时 ({timeout}s)")

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """视频放大到 4K/1080P，返回 task_id

        Args:
            at: Access Token
            project_id: 项目ID
            video_media_id: 视频的 mediaId
            aspect_ratio: 视频宽高比 VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            resolution: VIDEO_RESOLUTION_4K 或 VIDEO_RESOLUTION_1080P
            model_key: veo_3_1_upsampler_4k 或 veo_3_1_upsampler_1080p

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoUpsampleVideo"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "seed": random.randint(1, 99999),
                    "videoInput": {
                        "mediaId": video_media_id
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }],
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id
                }
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout()
                )
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        raise last_error

    async def check_video_status(self, at: str, operations: List[Dict]) -> dict:
        """查询视频生成状态

        Args:
            at: Access Token
            operations: 操作列表 [{"operation": {"name": "task_id"}, "sceneId": "...", "status": "..."}]

        Returns:
            {
                "operations": [{
                    "operation": {
                        "name": "task_id",
                        "metadata": {...}  # 完成时包含视频信息
                    },
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                }]
            }
        """
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        # 兼容 workflows 格式：如果传入的是 workflows [{"name": "..."}]，需要转换为 {"operation": {"name": "..."}}
        formatted_operations = []
        for op in operations:
            if "name" in op and "operation" not in op:
                formatted_operations.append({"operation": {"name": op["name"]}})
            else:
                formatted_operations.append(op)

        json_data = {
            "operations": formatted_operations
        }
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                return await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_poll_timeout()
                )
            except Exception as e:
                last_error = e
                retry_reason = self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[VIDEO POLL] 状态查询遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("视频状态查询失败")

    async def delete_media(self, st: str, media_names: List[str]):
        """删除媒体

        Args:
            st: Session Token
            media_names: 媒体ID列表
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {
            "json": {
                "names": media_names
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

