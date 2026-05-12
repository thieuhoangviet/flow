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

class GenerationHelpersMixin:
    def _create_generation_result(self) -> Dict[str, Any]:
        """????????????????"""
        return dict(success=False, error_message=None, error_emitted=False)

    def _create_response_state(self) -> Dict[str, Any]:
        """为单次请求创建独立的响应状态，避免并发请求互相污染。"""
        return {
            "url": None,
            "generated_assets": None,
            "base_url": None,
        }

    def _mark_generation_failed(self, generation_result: Optional[Dict[str, Any]], error_message: str):
        """????????????????????"""
        if isinstance(generation_result, dict):
            generation_result["success"] = False
            generation_result["error_message"] = error_message
            generation_result["error_emitted"] = True

    def _mark_generation_succeeded(self, generation_result: Optional[Dict[str, Any]]):
        """???????"""
        if isinstance(generation_result, dict):
            generation_result["success"] = True
            generation_result["error_message"] = None
            generation_result["error_emitted"] = False

    def _normalize_error_message(self, error_message: Any, max_length: int = 1000) -> str:
        """归一化错误文本，避免写入超长内容。"""
        text = str(error_message or "").strip() or "未知错误"
        if len(text) <= max_length:
            return text
        return f"{text[:max_length - 3]}..."

    async def _fail_video_task(self, operations: Optional[List[Dict[str, Any]]], error_message: str):
        """将视频任务收口到失败态，避免残留 processing。"""
        if not operations:
            return

        operation = operations[0] if operations else {}
        task_id = (operation.get("operation") or {}).get("name")
        if not task_id:
            return

        try:
            await self.db.update_task(
                task_id,
                status="failed",
                error_message=self._normalize_error_message(error_message),
                completed_at=time.time()
            )
        except Exception as exc:
            debug_logger.log_error(f"[VIDEO] 更新任务失败状态失败: {exc}")

    def _resolve_video_model_key_for_tier(self, model_config: Dict[str, Any], user_tier: str) -> tuple[str, Optional[str]]:
        """根据账号层级调整视频模型 key。"""
        model_key = model_config["model_key"]
        allow_tier_upgrade = bool(model_config.get("allow_tier_upgrade", True))

        if user_tier == "PAYGATE_TIER_TWO":
            if allow_tier_upgrade and "ultra" not in model_key:
                upgraded_model_key = _resolve_tier_two_model_key(model_key)
                if upgraded_model_key != model_key:
                    return upgraded_model_key, f"TIER_TWO 账号自动切换到 ultra 模型: {upgraded_model_key}"
            return model_key, None

        if user_tier == "PAYGATE_TIER_ONE" and "ultra" in model_key:
            model_key = model_key.replace("_ultra_fl", "_fl").replace("_ultra", "")
            return model_key, f"TIER_ONE 账号自动切换到标准模型: {model_key}"

        return model_key, None

    def _get_no_token_error_message(self, generation_type: str) -> str:
        """获取无可用Token时的详细错误信息"""
        if generation_type == "image":
            return "没有可用的Token进行图片生成。所有Token都处于禁用、冷却、锁定或已过期状态。"
        else:
            return "没有可用的Token进行视频生成。所有Token都处于禁用、冷却、配额耗尽或已过期状态。"

