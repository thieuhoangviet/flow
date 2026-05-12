"""Admin configuration, logs, health, and system info endpoints."""
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from curl_cffi.requests import AsyncSession
from ..core.config import config
from ..core.monitoring import build_public_health_snapshot
from . import _admin_deps as deps
from .admin_auth import verify_admin_token
from .admin_utils import _extract_error_summary

router = APIRouter()

class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None

class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15

class GenerationConfigRequest(BaseModel):
    image_timeout: Optional[int] = None
    video_timeout: Optional[int] = None
    max_retries: Optional[int] = None

class CallLogicConfigRequest(BaseModel):
    call_mode: str

class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str

class UpdateDebugConfigRequest(BaseModel):
    enabled: bool

class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int

# ========== Health / Stats / System Info ==========
@router.get("/health")
async def health_check():
    try: return await build_public_health_snapshot(deps.db)
    except Exception: return {"backend_running": True, "has_active_tokens": False}

@router.get("/api/stats")
async def get_stats(auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    return await deps.db.get_dashboard_stats(user_id=user_id)

@router.get("/api/system/info")
async def get_system_info(auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    stats = await deps.db.get_system_info_stats(user_id=user_id)
    return {"success": True, "info": {"total_tokens": stats["total_tokens"], "active_tokens": stats["active_tokens"], "total_credits": stats["total_credits"], "version": "1.0.0"}}

# ========== Logs ==========
@router.get("/api/logs")
async def get_logs(limit: int = 100, auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    limit = max(1, min(limit, 100))
    logs = await deps.db.get_logs(limit=limit, include_payload=False, user_id=user_id)
    result = []
    for log in logs:
        raw_sc = log.get("status_code")
        try: sc = int(raw_sc) if raw_sc is not None else None
        except (TypeError, ValueError): sc = None
        result.append({"id": log.get("id"), "token_id": log.get("token_id"), "token_email": log.get("token_email"), "token_username": log.get("token_username"), "operation": log.get("operation"), "status_code": sc if sc is not None else raw_sc, "duration": log.get("duration"), "status_text": log.get("status_text") or "", "progress": log.get("progress") or 0, "created_at": log.get("created_at"), "updated_at": log.get("updated_at"), "error_summary": _extract_error_summary(log.get("response_body_excerpt")) if sc is not None and sc >= 400 else ""})
    return result

@router.get("/api/logs/{log_id}")
async def get_log_detail(log_id: int, token: str = Depends(verify_admin_token)):
    log = await deps.db.get_log_detail(log_id)
    if not log: raise HTTPException(status_code=404, detail="日志不存在")
    return {"id": log.get("id"), "token_id": log.get("token_id"), "token_email": log.get("token_email"), "token_username": log.get("token_username"), "operation": log.get("operation"), "status_code": log.get("status_code"), "duration": log.get("duration"), "status_text": log.get("status_text") or "", "progress": log.get("progress") or 0, "created_at": log.get("created_at"), "updated_at": log.get("updated_at"), "error_summary": _extract_error_summary(log.get("response_body")), "request_body": log.get("request_body"), "response_body": log.get("response_body")}

@router.delete("/api/logs")
async def clear_logs(auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    try: await deps.db.clear_all_logs(user_id=user_id); return {"success": True, "message": "所有日志已清空"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ========== Proxy Config ==========
@router.get("/api/config/proxy")
async def get_proxy_config(token: str = Depends(verify_admin_token)):
    cfg = await deps.proxy_manager.get_proxy_config()
    return {"success": True, "config": {"enabled": cfg.enabled, "proxy_url": cfg.proxy_url, "media_proxy_enabled": cfg.media_proxy_enabled, "media_proxy_url": cfg.media_proxy_url}}

@router.get("/api/proxy/config")
async def get_proxy_config_alias(token: str = Depends(verify_admin_token)):
    cfg = await deps.proxy_manager.get_proxy_config()
    return {"proxy_enabled": cfg.enabled, "proxy_url": cfg.proxy_url, "media_proxy_enabled": cfg.media_proxy_enabled, "media_proxy_url": cfg.media_proxy_url}

@router.post("/api/proxy/config")
async def update_proxy_config_alias(request: ProxyConfigRequest, token: str = Depends(verify_admin_token)):
    try: await deps.proxy_manager.update_proxy_config(enabled=request.proxy_enabled, proxy_url=request.proxy_url, media_proxy_enabled=request.media_proxy_enabled, media_proxy_url=request.media_proxy_url)
    except ValueError as e: return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}

@router.post("/api/config/proxy")
async def update_proxy_config(request: ProxyConfigRequest, token: str = Depends(verify_admin_token)):
    try: await deps.proxy_manager.update_proxy_config(enabled=request.proxy_enabled, proxy_url=request.proxy_url, media_proxy_enabled=request.media_proxy_enabled, media_proxy_url=request.media_proxy_url)
    except ValueError as e: return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}

@router.post("/api/proxy/test")
async def test_proxy_connectivity(request: ProxyTestRequest, token: str = Depends(verify_admin_token)):
    proxy_input = (request.proxy_url or "").strip(); test_url = (request.test_url or "https://labs.google/").strip()
    timeout_seconds = max(5, min(int(request.timeout_seconds or 15), 60))
    if not proxy_input: return {"success": False, "message": "代理地址为空", "test_url": test_url}
    try: proxy_url = deps.proxy_manager.normalize_proxy_url(proxy_input)
    except ValueError as e: return {"success": False, "message": str(e), "test_url": test_url}
    start_time = time.time()
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession() as session:
            resp = await session.get(test_url, proxies=proxies, timeout=timeout_seconds, impersonate="chrome120", allow_redirects=True, verify=False)
        elapsed_ms = int((time.time() - start_time) * 1000); sc = resp.status_code; ok = 200 <= sc < 400
        return {"success": ok, "message": "代理可用" if ok else f"代理可连通，但目标返回状态码 {sc}", "test_url": test_url, "final_url": str(resp.url), "status_code": sc, "elapsed_ms": elapsed_ms}
    except Exception as e:
        return {"success": False, "message": f"代理测试失败: {str(e)}", "test_url": test_url, "elapsed_ms": int((time.time() - start_time) * 1000)}

# ========== Generation Config ==========
@router.get("/api/config/generation")
async def get_generation_config(token: str = Depends(verify_admin_token)):
    cfg = await deps.db.get_generation_config()
    return {"success": True, "config": {"image_timeout": cfg.image_timeout, "video_timeout": cfg.video_timeout, "max_retries": cfg.max_retries}}

@router.post("/api/config/generation")
async def update_generation_config(request: GenerationConfigRequest, token: str = Depends(verify_admin_token)):
    await deps.db.update_generation_config(image_timeout=request.image_timeout, video_timeout=request.video_timeout, max_retries=request.max_retries)
    await deps.db.reload_config_to_memory()
    return {"success": True, "message": "生成配置更新成功"}

@router.get("/api/generation/timeout")
async def get_generation_timeout(token: str = Depends(verify_admin_token)):
    return await get_generation_config(token)

@router.post("/api/generation/timeout")
async def update_generation_timeout(request: GenerationConfigRequest, token: str = Depends(verify_admin_token)):
    await deps.db.update_generation_config(image_timeout=request.image_timeout, video_timeout=request.video_timeout, max_retries=request.max_retries)
    await deps.db.reload_config_to_memory()
    return {"success": True, "message": "生成配置更新成功"}

# ========== Call Logic ==========
@router.get("/api/call-logic/config")
async def get_call_logic_config(token: str = Depends(verify_admin_token)):
    cfg = await deps.db.get_call_logic_config()
    call_mode = getattr(cfg, "call_mode", None)
    if call_mode not in ("default", "polling"): call_mode = "polling" if getattr(cfg, "polling_mode_enabled", False) else "default"
    return {"success": True, "config": {"call_mode": call_mode, "polling_mode_enabled": call_mode == "polling"}}

@router.post("/api/call-logic/config")
async def update_call_logic_config(request: CallLogicConfigRequest, token: str = Depends(verify_admin_token)):
    call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
    if call_mode is None: raise HTTPException(status_code=400, detail="Invalid call_mode")
    await deps.db.update_call_logic_config(call_mode); await deps.db.reload_config_to_memory()
    return {"success": True, "message": "Token轮询模式保存成功", "config": {"call_mode": call_mode, "polling_mode_enabled": call_mode == "polling"}}

# ========== Admin Config / API Key / Debug ==========
@router.get("/api/admin/config")
async def get_admin_config(token: str = Depends(verify_admin_token)):
    ac = await deps.db.get_admin_config()
    return {"admin_username": ac.username, "api_key": ac.api_key, "error_ban_threshold": ac.error_ban_threshold, "debug_enabled": config.debug_enabled}

@router.post("/api/admin/config")
async def update_admin_config(request: UpdateAdminConfigRequest, token: str = Depends(verify_admin_token)):
    await deps.db.update_admin_config(error_ban_threshold=request.error_ban_threshold)
    return {"success": True, "message": "配置更新成功"}

@router.post("/api/admin/apikey")
async def update_api_key(request: UpdateAPIKeyRequest, token: str = Depends(verify_admin_token)):
    await deps.db.update_admin_config(api_key=request.new_api_key); await deps.db.reload_config_to_memory()
    return {"success": True, "message": "API Key更新成功"}

@router.post("/api/admin/debug")
async def update_debug_config(request: UpdateDebugConfigRequest, token: str = Depends(verify_admin_token)):
    try:
        config.set_debug_enabled(request.enabled)
        return {"success": True, "message": f"Debug mode {'enabled' if request.enabled else 'disabled'}", "enabled": request.enabled}
    except Exception as e: raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")

# ========== AT Auto Refresh ==========
@router.get("/api/token-refresh/config")
async def get_token_refresh_config(token: str = Depends(verify_admin_token)):
    return {"success": True, "config": {"at_auto_refresh_enabled": True}}

@router.post("/api/token-refresh/enabled")
async def update_token_refresh_enabled(token: str = Depends(verify_admin_token)):
    return {"success": True, "message": "Flow2API的AT自动刷新默认启用且无法关闭"}

# ========== Cache Config ==========
async def _sync_runtime_cache_config():
    from . import routes
    if routes.generation_handler and routes.generation_handler.file_cache:
        fc = routes.generation_handler.file_cache; fc.set_timeout(config.cache_timeout); await fc.refresh_cleanup_task()

@router.get("/api/cache/config")
async def get_cache_config(token: str = Depends(verify_admin_token)):
    cc = await deps.db.get_cache_config()
    ebu = cc.cache_base_url if cc.cache_base_url else "http://127.0.0.1:8000"
    return {"success": True, "config": {"enabled": cc.cache_enabled, "timeout": cc.cache_timeout, "base_url": cc.cache_base_url or "", "effective_base_url": ebu}}

@router.post("/api/cache/enabled")
async def update_cache_enabled(request: dict, token: str = Depends(verify_admin_token)):
    enabled = request.get("enabled", False); await deps.db.update_cache_config(enabled=enabled)
    await deps.db.reload_config_to_memory(); await _sync_runtime_cache_config()
    return {"success": True, "message": f"缓存已{'启用' if enabled else '禁用'}"}

@router.post("/api/cache/config")
async def update_cache_config_full(request: dict, token: str = Depends(verify_admin_token)):
    enabled = request.get("enabled"); timeout = request.get("timeout"); base_url = request.get("base_url")
    if timeout is not None:
        try: timeout = int(timeout)
        except (TypeError, ValueError): raise HTTPException(status_code=400, detail="缓存超时时间必须为整数")
        if timeout < 0: raise HTTPException(status_code=400, detail="缓存超时时间不能小于 0")
    await deps.db.update_cache_config(enabled=enabled, timeout=timeout, base_url=base_url)
    await deps.db.reload_config_to_memory(); await _sync_runtime_cache_config()
    return {"success": True, "message": "缓存配置更新成功"}

@router.post("/api/cache/base-url")
async def update_cache_base_url(request: dict, token: str = Depends(verify_admin_token)):
    base_url = request.get("base_url", ""); await deps.db.update_cache_config(base_url=base_url)
    await deps.db.reload_config_to_memory(); await _sync_runtime_cache_config()
    return {"success": True, "message": "缓存Base URL更新成功"}
