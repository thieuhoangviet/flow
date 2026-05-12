"""API routes — OpenAI-compatible endpoints and WebSocket captcha relay.

Gemini endpoints are in routes_gemini.py.
Shared helpers are in routes_helpers.py.
"""
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.auth import AuthManager, verify_api_key_flexible
from ..core.logger import debug_logger
from ..core.model_resolver import get_base_model_aliases
from ..core.models import ChatCompletionRequest, GeminiGenerateContentRequest
from ..services.generation_handler import MODEL_CONFIG, GenerationHandler
from ..services.browser_captcha_extension import ExtensionCaptchaService

from .routes_helpers import (
    NormalizedGenerationRequest,
    _coerce_gemini_contents,
    _extract_prompt_and_images_from_openai_messages,
    _append_openai_reference_images,
    _resolve_request_model,
    _get_request_base_url,
    _get_openai_model_catalog,
    _parse_handler_result,
    _get_error_status_code,
)
from .routes_gemini import router as gemini_router

router = APIRouter()
# Merge gemini sub-router so main.py only needs routes.router
router.include_router(gemini_router)

# Dependency injection will be set up in main.py
generation_handler: GenerationHandler = None


def set_generation_handler(handler: GenerationHandler):
    """Set generation handler instance."""
    global generation_handler
    generation_handler = handler


def _ensure_generation_handler() -> GenerationHandler:
    if generation_handler is None:
        raise HTTPException(status_code=500, detail="Generation handler not initialized")
    return generation_handler


# ========== OpenAI normalization ==========

async def _normalize_openai_request(request: ChatCompletionRequest) -> NormalizedGenerationRequest:
    handler = _ensure_generation_handler()
    if request.messages:
        from .routes_helpers import _load_image_bytes_from_uri
        prompt, images, video_media_id = await _extract_prompt_and_images_from_openai_messages(
            request.messages, handler
        )
        if request.image and not images:
            images.append(await _load_image_bytes_from_uri(request.image, handler))
        model = _resolve_request_model(request.model, request)
        images = await _append_openai_reference_images(model, request.messages, images, handler)
        return NormalizedGenerationRequest(
            model=model, prompt=prompt, images=images,
            messages=request.messages, video_media_id=video_media_id,
        )

    if request.contents:
        from .routes_gemini import _normalize_gemini_request
        gemini_request = GeminiGenerateContentRequest(
            contents=_coerce_gemini_contents(request.contents),
            generationConfig=request.generationConfig,
        )
        normalized = await _normalize_gemini_request(request.model, gemini_request, handler)
        normalized.messages = request.messages
        return normalized

    raise HTTPException(status_code=400, detail="Messages or contents cannot be empty")


async def _collect_non_stream_result(
    model: str, prompt: str, images: List[bytes],
    base_url_override: Optional[str] = None,
    video_media_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> str:
    handler = _ensure_generation_handler()
    result = None
    async for chunk in handler.handle_generation(
        model=model, prompt=prompt, images=images if images else None,
        stream=False, base_url_override=base_url_override,
        video_media_id=video_media_id, user_id=user_id,
    ):
        result = chunk
    if result is None:
        raise HTTPException(status_code=500, detail="Generation failed: No response")
    return result


def _build_openai_json_response(payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(content=payload, status_code=_get_error_status_code(payload))


async def _iterate_openai_stream(
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str] = None,
    user_id: Optional[int] = None,
):
    handler = _ensure_generation_handler()
    async for chunk in handler.handle_generation(
        model=normalized.model, prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True, base_url_override=base_url_override,
        video_media_id=normalized.video_media_id, user_id=user_id,
    ):
        if chunk.startswith("data: "):
            yield chunk
            continue
        payload = _parse_handler_result(chunk)
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ========== OpenAI Endpoints ==========

@router.get("/v1/models")
async def list_models(api_key: str = Depends(verify_api_key_flexible)):
    """List available models."""
    models = [
        {"id": model["id"], "object": "model", "owned_by": "flow2api", "description": model["description"]}
        for model in _get_openai_model_catalog()
    ]
    return {"object": "list", "data": models}


@router.get("/v1/models/aliases")
async def list_model_aliases(api_key: str = Depends(verify_api_key_flexible)):
    """List simplified model aliases for generationConfig-based resolution."""
    aliases = get_base_model_aliases()
    alias_models = [
        {"id": alias_id, "object": "model", "owned_by": "flow2api", "description": description, "is_alias": True}
        for alias_id, description in aliases.items()
    ]
    return {"object": "list", "data": alias_models}


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    raw_request: Request,
    api_key: str = Depends(verify_api_key_flexible),
):
    """OpenAI-compatible unified generation endpoint."""
    try:
        normalized = await _normalize_openai_request(request)
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = _get_request_base_url(raw_request)

        from ..core.config import config
        user_id = 0
        handler = _ensure_generation_handler()
        if api_key != config.api_key:
            user = await handler.db.get_user_by_api_key(api_key)
            if user:
                user_id = user["id"]

        if request.stream:
            return StreamingResponse(
                _iterate_openai_stream(normalized, request_base_url, user_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )

        payload = _parse_handler_result(
            await _collect_non_stream_result(
                normalized.model, normalized.prompt, normalized.images,
                base_url_override=request_base_url,
                video_media_id=normalized.video_media_id,
                user_id=user_id,
            )
        )
        return _build_openai_json_response(payload)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ========== WebSocket ==========

@router.websocket("/captcha_ws")
async def captcha_websocket_endpoint(websocket: WebSocket):
    api_key = (
        websocket.query_params.get("key")
        or websocket.query_params.get("api_key")
        or websocket.headers.get("x-goog-api-key")
        or ""
    ).strip()
    authorization = (websocket.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        api_key = authorization[7:].strip()

    if not api_key or not AuthManager.verify_api_key(api_key):
        await websocket.close(code=1008)
        return

    service = await ExtensionCaptchaService.get_instance()
    await service.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await service.handle_message(websocket, data)
    except WebSocketDisconnect:
        service.disconnect(websocket)
    except Exception as e:
        debug_logger.log_error(f"WebSocket error: {e}")
        service.disconnect(websocket)
