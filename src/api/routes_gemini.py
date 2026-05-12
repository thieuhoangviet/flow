"""Gemini API endpoints (generateContent, streamGenerateContent, model listing)."""
import base64, json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from ..core.auth import verify_api_key_flexible
from ..core.models import GeminiGenerateContentRequest
from .routes_helpers import (
    MARKDOWN_IMAGE_RE, HTML_VIDEO_RE, NormalizedGenerationRequest,
    _coerce_gemini_contents, _extract_text_from_gemini_content,
    _extract_prompt_and_images_from_gemini_contents, _sanitize_media_prompt,
    _should_ignore_media_system_instruction, _resolve_request_model,
    _get_request_base_url, _get_gemini_model_catalog, _build_gemini_model_resource,
    _parse_handler_result, _get_error_status_code, _extract_openai_message_content,
    _extract_url_from_openai_payload, _detect_image_mime_type, _guess_mime_type,
    _decode_data_url, retrieve_image_data,
)
from ..services.generation_handler import MODEL_CONFIG

router = APIRouter()

GEMINI_STATUS_MAP = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 409: "ABORTED", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "UNAVAILABLE", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}

def _build_gemini_error_payload(status_code, message):
    return {"error": {"code": status_code, "message": message, "status": GEMINI_STATUS_MAP.get(status_code, "UNKNOWN")}}

def _build_gemini_error_response_from_handler(payload):
    error = payload.get("error", {}); sc = _get_error_status_code(payload); msg = error.get("message", "Generation failed")
    return JSONResponse(status_code=sc, content=_build_gemini_error_payload(sc, msg))

def _normalize_finish_reason(reason):
    if reason is None: return None
    return {"stop": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY"}.get(reason, "STOP")

async def _build_image_parts_from_uri(uri, generation_handler=None):
    if uri.startswith("data:image"):
        mime_type, _ = _decode_data_url(uri)
        from .routes_helpers import DATA_URL_RE
        match = DATA_URL_RE.match(uri)
        if match: return [{"inlineData": {"mimeType": mime_type, "data": match.group("data")}}]
    image_bytes = await retrieve_image_data(uri, generation_handler)
    if image_bytes:
        mime_type = _detect_image_mime_type(image_bytes, fallback=_guess_mime_type(uri, "image/png"))
        return [{"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}}]
    return [{"fileData": {"mimeType": _guess_mime_type(uri, "image/png"), "fileUri": uri}}, {"text": uri}]

def _build_video_parts_from_uri(uri):
    return [{"fileData": {"mimeType": _guess_mime_type(uri, "video/mp4"), "fileUri": uri}}]

async def _build_gemini_parts_from_output(output, generation_handler=None):
    if not output: return []
    im = MARKDOWN_IMAGE_RE.findall(output)
    if im:
        parts = []
        for uri in im: parts.extend(await _build_image_parts_from_uri(uri, generation_handler))
        return parts
    vm = HTML_VIDEO_RE.findall(output)
    if vm:
        parts = []
        for uri in vm: parts.extend(_build_video_parts_from_uri(uri))
        return parts
    return [{"text": output}]

async def _build_gemini_success_payload(payload, response_model, generation_handler=None):
    output = _extract_openai_message_content(payload)
    return {"candidates": [{"content": {"role": "model", "parts": await _build_gemini_parts_from_output(output, generation_handler)}, "finishReason": "STOP", "index": 0}], "modelVersion": response_model}

async def _convert_openai_stream_chunk_to_gemini_event(payload, response_model, generation_handler=None):
    choices = payload.get("choices", [])
    if not choices: return None
    choice = choices[0]; delta = choice.get("delta", {}); text = delta.get("reasoning_content") or delta.get("content") or ""
    finish_reason = _normalize_finish_reason(choice.get("finish_reason"))
    candidate = {"index": choice.get("index", 0)}
    if text: candidate["content"] = {"role": "model", "parts": await _build_gemini_parts_from_output(text, generation_handler)}
    if finish_reason: candidate["finishReason"] = finish_reason
    if len(candidate) == 1: return None
    return f"data: {json.dumps({'candidates': [candidate], 'modelVersion': response_model}, ensure_ascii=False)}\n\n"

# These functions need access to the generation_handler global from routes.py
# They accept it as a parameter
async def _normalize_gemini_request(model, request, generation_handler=None):
    resolved = _resolve_request_model(model, request)
    prompt, images = await _extract_prompt_and_images_from_gemini_contents(request.contents, generation_handler)
    si = _extract_text_from_gemini_content(request.systemInstruction)
    mc = MODEL_CONFIG.get(resolved); media = bool(mc and mc.get("type") in {"image", "video"})
    if media: prompt = _sanitize_media_prompt(prompt)
    if si:
        if media and _should_ignore_media_system_instruction(si):
            from ..core.logger import debug_logger
            debug_logger.log_warning(f"[GEMINI] 忽略媒体模型的 systemInstruction: model={resolved}, len={len(si)}")
        else:
            if media: si = _sanitize_media_prompt(si)
            prompt = f"{si}\n\n{prompt}".strip()
    return NormalizedGenerationRequest(model=resolved, prompt=prompt, images=images)

async def _iterate_gemini_stream(normalized, response_model, base_url_override, generation_handler):
    from ..core.logger import debug_logger
    async for chunk in generation_handler.handle_generation(model=normalized.model, prompt=normalized.prompt, images=normalized.images if normalized.images else None, stream=True, base_url_override=base_url_override, video_media_id=normalized.video_media_id):
        if chunk.startswith("data: "):
            pt = chunk[6:].strip()
            if pt == "[DONE]": continue
            payload = _parse_handler_result(pt)
            if "error" in payload:
                yield f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"; return
            event = await _convert_openai_stream_chunk_to_gemini_event(payload, response_model, generation_handler)
            if event: yield event
            continue
        payload = _parse_handler_result(chunk)
        if "error" in payload:
            yield f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"; return
        event = await _convert_openai_stream_chunk_to_gemini_event(payload, response_model, generation_handler)
        if event: yield event

# ========== Gemini Endpoints ==========
@router.get("/v1beta/models")
@router.get("/models")
async def list_gemini_models(api_key: str = Depends(verify_api_key_flexible)):
    catalog = _get_gemini_model_catalog()
    return {"models": [_build_gemini_model_resource(mid, desc) for mid, desc in catalog.items()]}

@router.get("/v1beta/models/{model}")
@router.get("/models/{model}")
async def get_gemini_model(model: str, api_key: str = Depends(verify_api_key_flexible)):
    catalog = _get_gemini_model_catalog(); desc = catalog.get(model)
    if not desc: return JSONResponse(status_code=404, content=_build_gemini_error_payload(404, f"Model not found: {model}"))
    return _build_gemini_model_resource(model, desc)

@router.post("/v1beta/models/{model}:generateContent")
@router.post("/models/{model}:generateContent")
async def generate_content(model: str, request: GeminiGenerateContentRequest, raw_request: Request, api_key: str = Depends(verify_api_key_flexible)):
    from . import routes as _routes
    handler = _routes._ensure_generation_handler()
    try:
        normalized = await _normalize_gemini_request(model, request, handler)
        if not normalized.prompt: raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        base_url = _get_request_base_url(raw_request)
        payload = _parse_handler_result(await _routes._collect_non_stream_result(normalized.model, normalized.prompt, normalized.images, base_url_override=base_url, video_media_id=normalized.video_media_id))
        payload = {**payload}; url = _extract_url_from_openai_payload(payload)
        if url and not payload.get("url"): payload["url"] = url
        if "error" in payload: return _build_gemini_error_response_from_handler(payload)
        return JSONResponse(content=await _build_gemini_success_payload(payload, normalized.model, handler))
    except HTTPException as exc: return JSONResponse(status_code=exc.status_code, content=_build_gemini_error_payload(exc.status_code, str(exc.detail)))
    except Exception as exc: return JSONResponse(status_code=500, content=_build_gemini_error_payload(500, str(exc)))

@router.post("/v1beta/models/{model}:streamGenerateContent")
@router.post("/models/{model}:streamGenerateContent")
async def stream_generate_content(model: str, request: GeminiGenerateContentRequest, raw_request: Request, alt: Optional[str] = Query(None), api_key: str = Depends(verify_api_key_flexible)):
    from . import routes as _routes
    handler = _routes._ensure_generation_handler()
    try:
        normalized = await _normalize_gemini_request(model, request, handler)
        if not normalized.prompt: raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        base_url = _get_request_base_url(raw_request)
        return StreamingResponse(_iterate_gemini_stream(normalized, normalized.model, base_url, handler), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    except HTTPException as exc: return JSONResponse(status_code=exc.status_code, content=_build_gemini_error_payload(exc.status_code, str(exc.detail)))
    except Exception as exc: return JSONResponse(status_code=500, content=_build_gemini_error_payload(500, str(exc)))
