"""Shared helpers, constants and extraction functions for API routes."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import base64, json, mimetypes, re
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from fastapi import HTTPException
from ..core.logger import debug_logger
from ..core.model_resolver import get_base_model_aliases, resolve_model_name
from ..core.models import ChatMessage, GeminiContent, GeminiGenerateContentRequest
from ..services.generation_handler import MODEL_CONFIG

MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)
MEDIA_PROMPT_TOOL_BLOCK_RE = re.compile(r"<tools>.*?</tools>", re.IGNORECASE | re.DOTALL)
MEDIA_SYSTEM_INSTRUCTION_MARKERS = ("<tools>", "</tools>", "function calling ai model", "function signatures", '"$schema"', '"additionalproperties"')
MEDIA_PROMPT_PREAMBLE_PATTERNS = (
    re.compile(r"^you are a function calling ai model\.?$", re.IGNORECASE),
    re.compile(r"^you are provided with function signatures within .* xml tags\.?$", re.IGNORECASE),
    re.compile(r"^you may call one or more functions to assist with the user query\.?$", re.IGNORECASE),
    re.compile(r"^don't make assumptions about what values to plug into functions\.?$", re.IGNORECASE),
    re.compile(r"^here are the available tools:.*$", re.IGNORECASE),
)

@dataclass
class NormalizedGenerationRequest:
    model: str
    prompt: str
    images: List[bytes]
    messages: Optional[List[ChatMessage]] = None
    video_media_id: Optional[str] = None

def _decode_data_url(data_url: str):
    match = DATA_URL_RE.match(data_url)
    if not match: raise HTTPException(status_code=400, detail="Invalid data URL")
    return match.group("mime"), base64.b64decode(match.group("data"))

def _detect_image_mime_type(image_bytes: bytes, fallback: str = "image/png") -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"): return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP": return "image/webp"
    return fallback

def _guess_mime_type(uri: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(uri).path); return guessed or fallback

async def retrieve_image_data(url: str, generation_handler=None) -> Optional[bytes]:
    file_cache = getattr(generation_handler, "file_cache", None) if generation_handler else None
    try:
        if "/tmp/" in url and file_cache:
            path = urlparse(url).path; filename = path.split("/tmp/")[-1]
            local_file_path = file_cache.cache_dir / filename
            if local_file_path.exists() and local_file_path.is_file():
                data = local_file_path.read_bytes()
                if data: return data
    except Exception as exc: debug_logger.log_warning(f"[CONTEXT] 本地缓存读取失败: {str(exc)}")
    proxy_url = None
    try:
        if file_cache and hasattr(file_cache, "_resolve_download_proxy"):
            proxy_url = await file_cache._resolve_download_proxy("image")
    except Exception as exc: debug_logger.log_warning(f"[CONTEXT] 图片下载代理解析失败: {str(exc)}")
    try:
        async with AsyncSession() as session:
            response = await session.get(url, timeout=60, proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                headers={"Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "Accept-Encoding": "gzip, deflate, br", "Connection": "keep-alive", "Referer": "https://labs.google/"},
                impersonate="chrome120", verify=False)
            if response.status_code == 200 and response.content: return response.content
            debug_logger.log_warning(f"[CONTEXT] 图片下载失败，状态码: {response.status_code}")
    except Exception as exc: debug_logger.log_error(f"[CONTEXT] 图片下载异常: {str(exc)}")
    return None

async def _load_image_bytes_from_uri(uri: str, generation_handler=None) -> bytes:
    if not uri: raise HTTPException(status_code=400, detail="Image URI cannot be empty")
    if uri.startswith("data:image"):
        _, image_bytes = _decode_data_url(uri); return image_bytes
    if uri.startswith("http://") or uri.startswith("https://") or "/tmp/" in uri:
        image_bytes = await retrieve_image_data(uri, generation_handler)
        if image_bytes: return image_bytes
        raise HTTPException(status_code=400, detail=f"Failed to load image from {uri}")
    raise HTTPException(status_code=400, detail=f"Unsupported image URI: {uri}")

def _coerce_gemini_contents(raw_contents):
    contents = []
    for item in raw_contents or []:
        contents.append(item if isinstance(item, GeminiContent) else GeminiContent.model_validate(item))
    return contents

def _extract_text_from_gemini_content(content):
    if content is None: return ""
    text_parts = [part.text.strip() for part in content.parts if part.text]
    return "\n".join(p for p in text_parts if p).strip()

def _should_ignore_media_system_instruction(si: str) -> bool:
    if not si: return False
    if len(si) > 1200: return True
    normalized = si.lower()
    return any(m in normalized for m in MEDIA_SYSTEM_INSTRUCTION_MARKERS)

def _sanitize_media_prompt(prompt: str) -> str:
    if not prompt: return ""
    sanitized = MEDIA_PROMPT_TOOL_BLOCK_RE.sub(" ", prompt.strip())
    cleaned = []
    for raw_line in sanitized.splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned and cleaned[-1] != "": cleaned.append("")
            continue
        if any(p.fullmatch(line) for p in MEDIA_PROMPT_PREAMBLE_PATTERNS): continue
        cleaned.append(line)
    sanitized = "\n".join(cleaned).strip()
    return re.sub(r"\n{3,}", "\n\n", sanitized).strip()

async def _extract_prompt_and_images_from_openai_messages(messages, generation_handler=None):
    last_message = messages[-1]; content = last_message.content
    prompt_parts = []; images = []; video_media_id = None
    if isinstance(content, str): prompt_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            it = item.get("type")
            if it == "text":
                text = item.get("text", "").strip()
                if text: prompt_parts.append(text)
            elif it == "image_url":
                iu = item.get("image_url", {}).get("url", "")
                if iu.startswith("extend-nocombine://"): video_media_id = "nocombine:" + iu[len("extend-nocombine://"):]
                elif iu.startswith("extend://"): video_media_id = iu[len("extend://"):]
                else: images.append(await _load_image_bytes_from_uri(iu, generation_handler))
    return "\n".join(p for p in prompt_parts if p).strip(), images, video_media_id

async def _append_openai_reference_images(model, messages, images, generation_handler=None):
    mc = MODEL_CONFIG.get(model)
    if not mc or mc["type"] != "image" or len(messages) <= 1: return images
    debug_logger.log_info(f"[CONTEXT] 开始查找历史参考图，消息数量: {len(messages)}")
    for msg in reversed(messages[:-1]):
        if msg.role == "assistant" and isinstance(msg.content, str):
            matches = MARKDOWN_IMAGE_RE.findall(msg.content)
            if not matches: continue
            for iu in reversed(matches):
                if not iu.startswith("http") and "/tmp/" not in iu: continue
                try:
                    db = await retrieve_image_data(iu, generation_handler)
                    if db:
                        images.insert(0, db); debug_logger.log_info(f"[CONTEXT] ✅ 添加历史参考图: {iu}"); return images
                    debug_logger.log_warning(f"[CONTEXT] 图片下载失败或为空: {iu}")
                except Exception as exc: debug_logger.log_error(f"[CONTEXT] 处理参考图时出错: {str(exc)}")
    return images

async def _extract_prompt_and_images_from_gemini_contents(contents, generation_handler=None):
    if not contents: raise HTTPException(status_code=400, detail="contents cannot be empty")
    target = next((c for c in reversed(contents) if (c.role or "user") == "user"), contents[-1])
    prompt_parts = []; images = []
    for part in target.parts:
        if part.text:
            t = part.text.strip()
            if t: prompt_parts.append(t)
        elif part.inlineData is not None:
            mt = part.inlineData.mimeType.lower()
            if not mt.startswith("image/"): raise HTTPException(status_code=400, detail=f"Unsupported inlineData mime type: {part.inlineData.mimeType}")
            images.append(base64.b64decode(part.inlineData.data))
        elif part.fileData is not None:
            mt = (part.fileData.mimeType or "").lower()
            if mt and not mt.startswith("image/"): raise HTTPException(status_code=400, detail=f"Unsupported fileData mime type: {part.fileData.mimeType}")
            images.append(await _load_image_bytes_from_uri(part.fileData.fileUri, generation_handler))
    return "\n".join(p for p in prompt_parts if p).strip(), images

def _resolve_request_model(model, request):
    resolved = resolve_model_name(model=model, request=request, model_config=MODEL_CONFIG)
    if resolved != model: debug_logger.log_info(f"[ROUTE] 模型名已转换: {model} → {resolved}")
    return resolved

def _get_request_base_url(request):
    fp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    fh = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = (fh or request.headers.get("host") or "").strip()
    if not host: return None
    proto = fp or request.url.scheme or "http"
    return f"{proto}://{host}"

def _build_model_description(mc):
    desc = f"{mc['type'].capitalize()} generation"
    return desc + (f" - {mc['model_name']}" if mc["type"] == "image" else f" - {mc['model_key']}")

def _get_openai_model_catalog():
    return [{"id": mid, "description": _build_model_description(mc)} for mid, mc in MODEL_CONFIG.items()]

def _get_gemini_model_catalog():
    catalog = {}
    for aid, desc in get_base_model_aliases().items(): catalog[aid] = desc
    for mid, mc in MODEL_CONFIG.items(): catalog.setdefault(mid, _build_model_description(mc))
    return catalog

def _build_gemini_model_resource(model_id, description):
    return {"name": f"models/{model_id}", "displayName": model_id, "description": description, "version": "flow2api", "inputTokenLimit": 0, "outputTokenLimit": 0, "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}

def _parse_handler_result(result):
    try: return json.loads(result)
    except json.JSONDecodeError: return {"result": result}

def _get_error_status_code(payload):
    error = payload.get("error")
    if isinstance(error, dict):
        sc = error.get("status_code")
        if isinstance(sc, int): return sc
        if isinstance(sc, str) and sc.isdigit(): return int(sc)
        return 400
    return 200

def _extract_openai_message_content(payload):
    choices = payload.get("choices", [])
    if not choices: return payload.get("result", "")
    return choices[0].get("message", {}).get("content", "") or ""

def _extract_url_from_openai_payload(payload):
    du = payload.get("url")
    if isinstance(du, str) and du.strip(): return du.strip()
    content = _extract_openai_message_content(payload).strip()
    if not content: return None
    im = MARKDOWN_IMAGE_RE.search(content)
    if im: return im.group(1).strip()
    vm = HTML_VIDEO_RE.search(content)
    if vm: return vm.group(1).strip()
    return None
