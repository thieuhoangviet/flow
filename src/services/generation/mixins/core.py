import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Optional, AsyncGenerator, List, Dict, Any
from src.core.logger import debug_logger
from src.core.config import config
from src.core.monitoring import record_generation_result
from src.core.models import Task, RequestLog
from src.core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from src.services.file_cache import FileCache

from src.services.generation.utils import MODEL_CONFIG, _make_t2v_config, _make_i2v_config, _apply_veo_3_1_model_updates, _known_video_model_keys, _resolve_tier_two_model_key

class GenerationCoreMixin:
    async def handle_generation(
        self,
        model: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        stream: bool = False,
        base_url_override: Optional[str] = None,
        video_media_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> AsyncGenerator:
        """统一生成入口

        Args:
            model: 模型名称
            prompt: 提示词
            images: 图片列表 (bytes格式)
            stream: 是否流式输出
        """
        start_time = time.time()
        token = None
        generation_type = None
        pending_token_state = {"active": False}
        request_id = f"gen-{int(start_time * 1000)}-{id(asyncio.current_task())}"
        perf_trace: Dict[str, Any] = {
            "request_id": request_id,
            "model": model,
            "status": "processing",
        }
        generation_result = self._create_generation_result()
        response_state = self._create_response_state()
        response_state["base_url"] = (base_url_override or "").strip().rstrip("/") or None
        request_log_state: Dict[str, Any] = {"id": None, "progress": 0}

        # 防止并发链路复用到上一次请求的指纹上下文
        if hasattr(self.flow_client, "clear_request_fingerprint"):
            self.flow_client.clear_request_fingerprint()

        # 1. 验证模型
        if model not in MODEL_CONFIG:
            error_msg = f"不支持的模型: {model}"
            debug_logger.log_error(error_msg)
            record_generation_result("unknown", "invalid", time.time() - start_time)
            yield self._create_error_response(error_msg, status_code=400)
            return

        model_config = MODEL_CONFIG[model]
        generation_type = model_config["type"]
        video_type_for_op = model_config.get("video_type", "")
        request_operation = "extend_video" if video_type_for_op == "extend" else f"generate_{generation_type}"
        prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
        request_payload = {
            "model": model,
            "prompt": prompt_for_log,
            "has_images": images is not None and len(images) > 0,
        }
        debug_logger.log_info(f"[GENERATION] 开始生成 - 模型: {model}, 类型: {generation_type}, Prompt: {prompt[:50]}...")

        # 向用户展示开始信息
        if stream:
            yield self._create_stream_chunk(
                f"✨ {'视频' if generation_type == 'video' else '图片'}生成任务已启动\n",
                role="assistant"
            )
            request_log_state["id"] = await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={"status": "processing", "status_text": "started", "progress": 0, "request_id": request_id},
                status_code=102,
                duration=0,
                status_text="started",
                progress=0,
                user_id=user_id,
            )

        # 2. 选择Token
        debug_logger.log_info(f"[GENERATION] 正在选择可用Token...")
        token_select_started_at = time.time()

        if generation_type == "image":
            token = await self.load_balancer.select_token(
                for_image_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
                user_id=user_id,
            )
        else:
            token = await self.load_balancer.select_token(
                for_video_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
                user_id=user_id,
            )
        perf_trace["token_select_ms"] = int((time.time() - token_select_started_at) * 1000)

        if not token:
            error_msg = None
            if self.load_balancer and hasattr(self.load_balancer, "get_unavailable_reason"):
                error_msg = await self.load_balancer.get_unavailable_reason(
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                    model=model,
                    user_id=user_id,
                )
            if not error_msg:
                error_msg = self._get_no_token_error_message(generation_type)
            debug_logger.log_error(f"[GENERATION] {error_msg}")
            record_generation_result(generation_type, "no_token", time.time() - start_time)
            await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={"error": error_msg, "performance": perf_trace},
                status_code=503,
                duration=time.time() - start_time,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                user_id=user_id,
            )
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=503)
            return

        debug_logger.log_info(f"[GENERATION] 已选择Token: {token.id} ({token.email})")
        pending_token_state["active"] = True
        await self._update_request_log_progress(
            request_log_state,
            token_id=token.id,
            status_text="token_selected",
            progress=8,
            response_extra={"token_email": token.email},
        )

        try:
            # 3. 确保AT有效
            debug_logger.log_info(f"[GENERATION] 检查Token AT有效性...")
            if stream:
                yield self._create_stream_chunk("初始化生成环境...\n")

            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="token_ready",
                progress=15,
            )
            ensure_at_started_at = time.time()
            token = await self.token_manager.ensure_valid_token(token)
            perf_trace["ensure_at_ms"] = int((time.time() - ensure_at_started_at) * 1000)
            if not token:
                error_msg = "Token AT无效或刷新失败"
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=503)
                return

            # 4. 确保Project存在
            debug_logger.log_info(f"[GENERATION] 检查/创建Project...")

            if not supports_model_for_tier(model, token.user_paygate_tier):
                required_tier = get_required_paygate_tier_for_model(model)
                error_msg = "当前模型需要 " + get_paygate_tier_label(required_tier) + " 账号: " + model
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=403)
                return

            ensure_project_started_at = time.time()
            project_id = await self.token_manager.ensure_project_exists(token.id)
            perf_trace["ensure_project_ms"] = int((time.time() - ensure_project_started_at) * 1000)
            debug_logger.log_info(f"[GENERATION] Project ID: {project_id}")
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="project_ready",
                progress=22,
                response_extra={"project_id": project_id},
            )
            prefill_action = "IMAGE_GENERATION" if generation_type == "image" else "VIDEO_GENERATION"
            await self.flow_client.prefill_remote_browser_pool(
                project_id=project_id,
                action=prefill_action,
                token_id=token.id,
            )

            # 5. 根据类型处理
            generation_pipeline_started_at = time.time()
            if generation_type == "image":
                debug_logger.log_info(f"[GENERATION] 开始图片生成流程...")
                async for chunk in self._handle_image_generation(
                    token, project_id, model_config, prompt, images, stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state
                ):
                    yield chunk
            else:  # video
                debug_logger.log_info(f"[GENERATION] 开始视频生成流程...")
                async for chunk in self._handle_video_generation(
                    token, project_id, model_config, prompt, images, stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                    video_media_id=video_media_id,
                ):
                    yield chunk
            perf_trace["generation_pipeline_ms"] = int((time.time() - generation_pipeline_started_at) * 1000)

            # 6. 记录使用
            if not generation_result.get("success"):
                error_msg = generation_result.get("error_message") or "生成未成功完成"
                debug_logger.log_warning(f"[GENERATION] 生成未成功，不扣次数: {error_msg}")
                if token:
                    await self.token_manager.record_error(token.id)
                duration = time.time() - start_time
                record_generation_result(generation_type, "failed", duration)
                perf_trace["status"] = "failed"
                perf_trace["total_ms"] = int(duration * 1000)
                perf_trace["error"] = error_msg
                prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
                await self._log_request(
                    token.id if token else None,
                    request_operation,
                    request_payload,
                    {"error": error_msg, "performance": perf_trace},
                    500,
                    duration,
                    log_id=request_log_state.get("id"),
                    status_text="failed",
                    progress=request_log_state.get("progress", 0),
                    user_id=user_id,
                )
                if not generation_result.get("error_emitted"):
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=500)
                return

            is_video = (generation_type == "video")
            await self.token_manager.record_usage(token.id, is_video=is_video)

            # 重置错误计数 (请求成功时清空连续错误计数)
            await self.token_manager.record_success(token.id)

            debug_logger.log_info(f"[GENERATION] ✅ 生成成功完成")

            # 7. 记录成功日志
            duration = time.time() - start_time
            record_generation_result(generation_type, "success", duration)
            perf_trace["status"] = "success"
            perf_trace["total_ms"] = int(duration * 1000)
            # 日志中保留更完整的 prompt，避免管理页只看到过短内容
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"

            # 构建响应数据，包含生成的URL
            response_data = {
                "status": "success",
                "model": model,
                "prompt": prompt_for_log,
                "performance": perf_trace
            }

            # 添加生成的URL（如果有）
            if response_state.get("url"):
                response_data["url"] = response_state["url"]
            if response_state.get("generated_assets"):
                response_data["generated_assets"] = response_state["generated_assets"]
            image_perf = perf_trace.get("image_generation", {}) if isinstance(perf_trace, dict) else {}
            video_perf = perf_trace.get("video_generation", {}) if isinstance(perf_trace, dict) else {}
            debug_logger.log_info(
                f"[PERF] [{request_id}] total={perf_trace.get('total_ms', 0)}ms, "
                f"select={perf_trace.get('token_select_ms', 0)}ms, "
                f"ensure_at={perf_trace.get('ensure_at_ms', 0)}ms, "
                f"project={perf_trace.get('ensure_project_ms', 0)}ms, "
                f"pipeline={perf_trace.get('generation_pipeline_ms', 0)}ms, "
                f"slot_wait={image_perf.get('slot_wait_ms', 0)}ms, "
                f"launch_queue={image_perf.get('launch_queue_wait_ms', 0)}ms, "
                f"launch_stagger={image_perf.get('launch_stagger_wait_ms', 0)}ms, "
                f"video_slot_wait={video_perf.get('slot_wait_ms', 0)}ms"
            )

            await self._log_request(
                token.id,
                request_operation,
                request_payload,
                response_data,
                200,
                duration,
                log_id=request_log_state.get("id"),
                status_text="completed",
                progress=100,
                user_id=user_id,
            )

        except asyncio.CancelledError:
            error_msg = "生成已取消: 客户端连接已断开"
            debug_logger.log_warning(f"[GENERATION] ⚠️ {error_msg}")
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "cancelled", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                499,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                user_id=user_id,
            )
            raise
        except Exception as e:
            error_msg = f"生成失败: {str(e)}"
            debug_logger.log_error(f"[GENERATION] ❌ {error_msg}")
            if token:
                # 记录错误（所有错误统一处理，不再特殊处理429）
                await self.token_manager.record_error(token.id)

            # 先将最终失败状态落库，再返回错误响应，避免日志停在 102。
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "failed", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                500,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                user_id=user_id,
            )
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=500)
        finally:
            if pending_token_state.get("active") and token and self.load_balancer:
                await self.load_balancer.release_pending(
                    token.id,
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                )
                pending_token_state["active"] = False

