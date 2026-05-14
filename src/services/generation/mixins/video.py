import asyncio
import base64
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, AsyncGenerator, List, Dict, Any
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

class GenerationVideoMixin:
    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    async def _handle_video_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
        video_media_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """处理视频生成 (异步轮询)"""

        if response_state is None:
            response_state = self._create_response_state()

        video_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            video_trace = perf_trace.setdefault("video_generation", {})
            video_trace["input_image_count"] = len(images) if images else 0

        # 不在本地等待视频硬并发槽位；请求一到就直接向上游提交。
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if video_trace is not None:
            video_trace["slot_wait_ms"] = 0

        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="preparing_video", progress=24)

        try:
            # 获取模型类型和配置
            video_type = model_config.get("video_type")
            supports_images = model_config.get("supports_images", False)
            min_images = model_config.get("min_images", 0)
            max_images = model_config.get("max_images", 0)
            use_v2_model_config = bool(model_config.get("use_v2_model_config", False))

            # 根据账号tier自动调整模型 key
            user_tier = normalized_tier

            original_model_key = model_config["model_key"]
            model_key, tier_message = self._resolve_video_model_key_for_tier(model_config, user_tier)
            if tier_message:
                if stream:
                    yield self._create_stream_chunk(f"{tier_message}\n")
                debug_logger.log_info(f"[VIDEO] 账号层级模型调整: {original_model_key} -> {model_key}")
            elif user_tier == "PAYGATE_TIER_TWO" and original_model_key == model_key:
                debug_logger.log_info(f"[VIDEO] TIER_TWO 账号，未找到有效 ultra 变体，保持模型: {model_key}")

            # 更新 model_config 中的 model_key
            model_config = dict(model_config)  # 创建副本避免修改原配置
            model_config["model_key"] = model_key

            # 图片数量
            image_count = len(images) if images else 0

            # ========== 验证和处理图片 ==========

            # T2V: 文生视频 - 不支持图片
            if video_type == "t2v":
                if image_count > 0:
                    if stream:
                        yield self._create_stream_chunk("⚠️ 文生视频模型不支持上传图片,将忽略图片仅使用文本提示词生成\n")
                    debug_logger.log_warning(f"[T2V] 模型 {model_config['model_key']} 不支持图片,已忽略 {image_count} 张图片")
                images = None  # 清空图片
                image_count = 0

            # I2V: 首尾帧模型 - 需要1-2张图片
            elif video_type == "i2v":
                if image_count < min_images or image_count > max_images:
                    error_msg = f"❌ 首尾帧模型需要 {min_images}-{max_images} 张图片,当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # R2V: 多图生成 - 当前上游协议最多 3 张参考图
            elif video_type == "r2v":
                if max_images is not None and image_count > max_images:
                    error_msg = f"❌ 多图视频模型最多支持 {max_images} 张参考图,当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # ========== 上传图片 ==========
            start_media_id = None
            end_media_id = None
            reference_images = []

            # I2V: 首尾帧处理
            if video_type == "i2v" and images:
                if image_count == 1:
                    # 只有1张图: 仅作为首帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] 仅上传首帧: {start_media_id}")

                elif image_count == 2:
                    # 2张图: 首帧+尾帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧和尾帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    end_media_id = await self.flow_client.upload_image(
                        token.at, images[1], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] 上传首尾帧: {start_media_id}, {end_media_id}")

            # R2V: 多图处理
            elif video_type == "r2v" and images:
                if stream:
                    yield self._create_stream_chunk(f"上传 {image_count} 张参考图片...\n")

                for img in images:
                    media_id = await self.flow_client.upload_image(
                        token.at, img, model_config["aspect_ratio"], project_id=project_id
                    )
                    reference_images.append({
                        "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                        "mediaId": media_id
                    })
                debug_logger.log_info(f"[R2V] 上传了 {len(reference_images)} 张参考图片")

            # ========== 调用生成API ==========
            if stream:
                yield self._create_stream_chunk("提交视频生成任务...\n")
            submit_started_at = time.time()

            # I2V: 首尾帧生成
            if video_type == "i2v" and start_media_id:
                if end_media_id:
                    # 有首尾帧
                    result = await self.flow_client.generate_video_start_end(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        end_media_id=end_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )
                else:
                    # 只有首帧 - 需要去掉 model_key 中的 _fl
                    # 情况1: _fl_ 在中间 (如 veo_3_1_i2v_s_fast_fl_ultra_relaxed -> veo_3_1_i2v_s_fast_ultra_relaxed)
                    # 情况2: _fl 在结尾 (如 veo_3_1_i2v_s_fast_ultra_fl -> veo_3_1_i2v_s_fast_ultra)
                    actual_model_key = model_config["model_key"].replace("_fl_", "_")
                    if actual_model_key.endswith("_fl"):
                        actual_model_key = actual_model_key[:-3]
                    debug_logger.log_info(f"[I2V] 单帧模式，model_key: {model_config['model_key']} -> {actual_model_key}")
                    result = await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=actual_model_key,
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )

            # R2V: 多图生成
            elif video_type == "r2v" and reference_images:
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # Extend: 视频续写
            elif video_type == "extend":
                if not video_media_id:
                    error_msg = "❌ 视频续写需要提供源视频的 mediaGenerationId，请在 image_url 中传入 extend://VIDEO_MEDIA_ID"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

                # nocombine: prefix 表示不要拼接，只返回续写片段
                skip_extend_concat = False
                actual_video_media_id = video_media_id
                if video_media_id.startswith("nocombine:"):
                    skip_extend_concat = True
                    actual_video_media_id = video_media_id[len("nocombine:"):]
                    debug_logger.log_info(f"[EXTEND] nocombine 模式，跳过拼接")

                debug_logger.log_info(f"[EXTEND] 续写视频: {actual_video_media_id}")
                if stream:
                    yield self._create_stream_chunk(f"视频续写任务提交中，源视频: {actual_video_media_id[:8]}...\n")
                result = await self.flow_client.generate_video_extend(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    video_media_id=actual_video_media_id,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # T2V 或 R2V无图: 纯文本生成
            else:
                result = await self.flow_client.generate_video_text(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    use_v2_model_config=use_v2_model_config,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )
            if video_trace is not None:
                video_trace["submit_generation_ms"] = int((time.time() - submit_started_at) * 1000)

            # 获取task_id (兼容 operations 和 workflows/media 两种格式)
            operations = result.get("operations", [])
            workflows = result.get("workflows", [])
            media = result.get("media", [])
            
            if not operations and not workflows and not media:
                import json
                debug_logger.log_error(f"生成任务创建失败，返回值为: {json.dumps(result, ensure_ascii=False)}")
                self._mark_generation_failed(generation_result, "\u751f\u6210\u4efb\u52a1\u521b\u5efa\u5931\u8d25")
                yield self._create_error_response("生成任务创建失败", status_code=502)
                return

            if media:
                operation = media[0]
                task_id = operation.get("name")
                scene_id = operation.get("sceneId")
            elif workflows:
                operation = workflows[0]
                task_id = operation.get("name") or operation.get("workflowId")
                scene_id = operation.get("sceneId")
                if not task_id and "operation" in operation:
                    task_id = operation["operation"].get("name")
            else:
                operation = operations[0]
                task_id = operation["operation"]["name"]
                scene_id = operation.get("sceneId")

            # 保存Task到数据库
            task = Task(
                task_id=task_id,
                token_id=token.id,
                model=model_config["model_key"],
                prompt=prompt,
                status="processing",
                scene_id=scene_id
            )
            await self.db.create_task(task)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="video_submitted",
                progress=45,
                response_extra={"task_id": task_id, "scene_id": scene_id},
            )

            # 轮询结果
            if stream:
                yield self._create_stream_chunk(f"视频生成中...\n")

            # 检查是否需要放大
            upsample_config = model_config.get("upsample")

            # 如果是 extend，传入源视频 media_id 用于后续拼接
            # nocombine 模式下不拼接，只返回续写片段
            if video_type == "extend" and not skip_extend_concat:
                extend_source_id = actual_video_media_id
            else:
                extend_source_id = None
            tracking_items = operations if operations else media if media else workflows
            async for chunk in self._poll_video_result(
                token,
                project_id,
                tracking_items,
                stream,
                upsample_config,
                generation_result,
                response_state,
                request_log_state,
                extend_source_media_id=extend_source_id,
            ):
                yield chunk

        finally:
            pass

    async def _poll_video_result(
        self,
        token,
        project_id: str,
        tracking_items: List[Dict],
        stream: bool,
        upsample_config: Optional[Dict] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        extend_source_media_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """轮询视频生成结果
        
        Args:
            upsample_config: 放大配置 {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
        """

        if response_state is None:
            response_state = self._create_response_state()

        max_attempts = config.max_poll_attempts
        poll_interval = config.poll_interval
        
        # 如果需要放大，轮询次数加倍（放大可能需要 30 分钟）
        if upsample_config:
            max_attempts = max_attempts * 3  # 放大需要更长时间

        consecutive_poll_errors = 0
        last_poll_error: Optional[Exception] = None
        max_consecutive_poll_errors = 3

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            try:
                result = await self.flow_client.check_video_status(token.at, tracking_items)
                checked_operations = result.get("operations", []) or result.get("media", []) or result.get("workflows", [])
                consecutive_poll_errors = 0
                last_poll_error = None

                if not checked_operations:
                    continue

                operation = checked_operations[0]
                
                # Extract status depending on whether it's an operation or a workflow/media
                if "operation" in operation:
                    status = operation.get("status")
                elif "mediaMetadata" in operation:
                    status = operation["mediaMetadata"].get("mediaStatus", {}).get("mediaGenerationStatus")
                elif "mediaStatus" in operation:
                    status = operation["mediaStatus"].get("mediaGenerationStatus")
                else:
                    status = operation.get("status")

                # 状态更新 - 每20秒报告一次 (poll_interval=3秒, 20秒约7次轮询)
                progress_update_interval = 7  # 每7次轮询 = 21秒
                if stream and attempt % progress_update_interval == 0:  # 每20秒报告一次
                    progress = min(int((attempt / max_attempts) * 100), 95)
                    await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="video_polling", progress=max(45, progress), response_extra={"upstream_status": status})
                    yield self._create_stream_chunk(f"生成进度: {progress}%\n")

                # 检查状态
                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    # 成功
                    if "operation" in operation:
                        metadata = operation["operation"].get("metadata", {})
                    elif "mediaMetadata" in operation:
                        metadata = operation["mediaMetadata"]
                    else:
                        metadata = operation
                        
                    video_info = metadata.get("video", {})
                    if "generatedVideo" in video_info:
                        video_info = video_info["generatedVideo"]
                    
                    video_url = metadata.get("video", {}).get("fifeUrl") or video_info.get("fifeUrl")
                    # Extract short UUID from Google Storage URL (e.g., /video/UUID?)
                    # Both extend API and concat API need this short UUID format,
                    # NOT the CAUS base64 mediaGenerationId from video_info
                    import re as _re
                    _uuid_match = _re.search(r'/video/([0-9a-f-]{36})', video_url or '')
                    video_media_id = _uuid_match.group(1) if _uuid_match else video_info.get("mediaGenerationId", "")
                    aspect_ratio = video_info.get("aspectRatio", "VIDEO_ASPECT_RATIO_LANDSCAPE")

                    if not video_url:
                        error_msg = "视频生成失败: 视频URL为空"
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(generation_result, error_msg)
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    # ========== 视频放大处理 ==========
                    if upsample_config and video_media_id:
                        if stream:
                            resolution_name = "4K" if "4K" in upsample_config["resolution"] else "1080P"
                            yield self._create_stream_chunk(f"\n视频生成完成，开始 {resolution_name} 放大处理...（可能需要 30 分钟）\n")
                        
                        try:
                            # 提交放大任务
                            upsample_result = await self.flow_client.upsample_video(
                                at=token.at,
                                project_id=project_id,
                                video_media_id=video_media_id,
                                aspect_ratio=aspect_ratio,
                                resolution=upsample_config["resolution"],
                                model_key=upsample_config["model_key"],
                                token_id=token.id,
                                token_video_concurrency=token.video_concurrency,
                            )
                            
                            upsample_operations = upsample_result.get("operations", [])
                            if upsample_operations:
                                if stream:
                                    yield self._create_stream_chunk("放大任务已提交，继续轮询...\n")
                                
                                # 递归轮询放大结果（不再放大）
                                async for chunk in self._poll_video_result(
                                    token,
                                    project_id,
                                    upsample_operations,
                                    stream,
                                    None,
                                    generation_result,
                                    response_state,
                                    request_log_state,
                                ):
                                    yield chunk
                                return
                            else:
                                if stream:
                                    yield self._create_stream_chunk("⚠️ 放大任务创建失败，返回原始视频\n")
                        except Exception as e:
                            debug_logger.log_error(f"Video upsample failed: {str(e)}")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ 放大失败: {str(e)}，返回原始视频\n")

                    # ========== Extend 视频拼接 ==========
                    if extend_source_media_id and video_media_id:
                        try:
                            if stream:
                                yield self._create_stream_chunk("\n视频续写完成，正在拼接完整视频...\n")
                            debug_logger.log_info(f"[CONCAT] 开始拼接: original={extend_source_media_id[:12]}..., extend={video_media_id[:12]}...")
                            
                            # 提交拼接任务
                            concat_result = await self.flow_client.run_concatenation(
                                at=token.at,
                                original_media_id=extend_source_media_id,
                                extend_media_id=video_media_id,
                            )
                            
                            # 获取 operation name
                            concat_op = concat_result.get("operation", {}).get("operation", {}).get("name", "")
                            if concat_op:
                                if stream:
                                    yield self._create_stream_chunk("拼接任务已提交，等待完成...\n")
                                
                                # 轮询拼接状态
                                concat_status = await self.flow_client.poll_concatenation_status(
                                    at=token.at,
                                    operation_name=concat_op,
                                    timeout=300,
                                    poll_interval=3,
                                )
                                
                                concat_url = concat_status.get("outputUri", "")
                                if concat_url:
                                    # 如果是本地路径（/tmp/xxx.mp4），构造完整 URL
                                    if concat_url.startswith("/tmp/"):
                                        server_host = config.server_host or "0.0.0.0"
                                        server_port = config.server_port or 8000
                                        # 对外使用 localhost
                                        host = "localhost" if server_host == "0.0.0.0" else server_host
                                        concat_url = f"http://{host}:{server_port}{concat_url}"
                                    video_url = concat_url  # 替换为拼接后的完整视频 URL
                                    if stream:
                                        yield self._create_stream_chunk("✅ 视频拼接完成！返回 16s 完整视频\n")
                                    debug_logger.log_info(f"[CONCAT] 拼接成功: {concat_url[:80]}...")
                                else:
                                    if stream:
                                        yield self._create_stream_chunk("⚠️ 拼接完成但无 URL，返回续写片段\n")
                            else:
                                debug_logger.log_warning("[CONCAT] 拼接任务创建失败，返回续写片段")
                                if stream:
                                    yield self._create_stream_chunk("⚠️ 拼接任务创建失败，返回续写片段\n")
                        except Exception as e:
                            import traceback
                            debug_logger.log_error(f"[CONCAT] 拼接失败: {str(e)}")
                            debug_logger.log_error(f"[CONCAT] traceback: {traceback.format_exc()}")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ 拼接失败: {str(e)}，返回续写片段\n")
                            # 拼接失败不影响返回，继续使用 extend 片段的 URL

                    # 缓存视频 (如果启用)
                    local_url = video_url
                    if config.cache_enabled:
                        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="caching_video", progress=92)
                        try:
                            if stream:
                                yield self._create_stream_chunk("正在缓存视频文件...\n")
                            cached_filename = await self.file_cache.download_and_cache(video_url, "video")
                            local_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                            if stream:
                                yield self._create_stream_chunk("✅ 视频缓存成功,准备返回缓存地址...\n")
                        except Exception as e:
                            debug_logger.log_error(f"Failed to cache video: {str(e)}")
                            # 缓存失败不影响结果返回,使用原始URL
                            local_url = video_url
                            if stream:
                                cache_error = self._normalize_error_message(e, max_length=120)
                                yield self._create_stream_chunk(f"⚠️ 缓存失败: {cache_error}\n正在返回源链接...\n")
                    else:
                        if stream:
                            yield self._create_stream_chunk("缓存已关闭,正在返回源链接...\n")

                    # 更新数据库
                    task_id = operation["operation"]["name"]
                    await self.db.update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        result_urls=[local_url],
                        completed_at=time.time()
                    )

                    # 存储URL用于日志记录
                    response_state["url"] = local_url
                    response_state["generated_assets"] = {
                        "type": "video",
                        "final_video_url": local_url,
                        "mediaGenerationId": video_media_id,
                    }

                    # 返回结果
                    self._mark_generation_succeeded(generation_result)

                    if stream:
                        yield self._create_stream_chunk(
                            f"<video src='{local_url}' data-media-id='{video_media_id}' controls style='max-width:100%'></video>",
                            finish_reason="stop"
                        )

                    else:
                        yield self._create_completion_response(
                            local_url,  # 直接传URL,让方法内部格式化
                            media_type="video"
                        )
                    return

                elif status == "MEDIA_GENERATION_STATUS_FAILED":
                    # 生成失败 - 提取错误信息
                    error_info = operation.get("operation", {}).get("error", {})
                    error_code = error_info.get("code", "unknown")
                    error_message = error_info.get("message", "未知错误")
                    
                    # 更新数据库任务状态
                    await self._fail_video_task(
                        checked_operations,
                        f"{error_message} (code: {error_code})"
                    )
                    
                    # 返回友好的错误消息，提示用户重试
                    friendly_error = f"视频生成失败: {error_message}，请重试"
                    self._mark_generation_failed(generation_result, friendly_error)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {friendly_error}\n")
                    yield self._create_error_response(friendly_error, status_code=502)
                    return

                elif status.startswith("MEDIA_GENERATION_STATUS_ERROR"):
                    # ??????
                    error_msg = f"视频生成失败: {status}"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                    
                elif status == "MEDIA_GENERATION_STATUS_ACTIVE" and attempt > 80:
                    # 如果持续4分钟（80次 * 3秒 = 240秒）依然是 ACTIVE 状态，则判定为卡死
                    error_msg = "视频生成超时 (上游卡顿超过4分钟，已自动取消)"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=504)
                    return

            except Exception as e:
                last_poll_error = e
                consecutive_poll_errors += 1
                debug_logger.log_error(f"Poll error: {str(e)}")
                if consecutive_poll_errors >= max_consecutive_poll_errors:
                    error_msg = f"视频状态查询失败: {self._normalize_error_message(e)}"
                    await self._fail_video_task(tracking_items, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                continue

        # 超时
        if last_poll_error is not None:
            error_msg = f"视频状态查询持续失败: {self._normalize_error_message(last_poll_error)}"
        else:
            error_msg = f"视频生成超时 (已轮询 {max_attempts} 次)"
        await self._fail_video_task(tracking_items, error_msg)
        self._mark_generation_failed(generation_result, error_msg)
        yield self._create_error_response(error_msg, status_code=504)

