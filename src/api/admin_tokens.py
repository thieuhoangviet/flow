"""Admin token management endpoints."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from . import _admin_deps as deps
from .admin_auth import verify_admin_token

router = APIRouter()

class AddTokenRequest(BaseModel):
    st: str
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1

class UpdateTokenRequest(BaseModel):
    st: str
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None

class ST2ATRequest(BaseModel):
    st: str

class ImportTokenItem(BaseModel):
    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1

class ImportTokensRequest(BaseModel):
    tokens: List[ImportTokenItem]

def _strip(v): return v.strip() if v is not None else None

@router.get("/api/tokens")
async def get_tokens(auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    token_rows = await deps.db.get_all_tokens_with_stats(user_id=user_id)
    to_iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v
    now = datetime.now(timezone.utc)
    def ndt(v):
        if not v: return None
        if isinstance(v, str):
            try: v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except: return None
        if getattr(v, "tzinfo", None) is None: return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
    return [{
        "id": r.get("id"), "st": r.get("st"), "at": r.get("at"),
        "at_expires": to_iso(r.get("at_expires")) if r.get("at_expires") else None,
        "at_expired": bool(ndt(r.get("at_expires")) and ndt(r.get("at_expires")) <= now),
        "at_expiring_within_1h": bool(ndt(r.get("at_expires")) and ndt(r.get("at_expires")) > now and (ndt(r.get("at_expires")) - now).total_seconds() < 3600),
        "token": r.get("at"), "email": r.get("email"), "name": r.get("name"),
        "remark": r.get("remark"), "is_active": bool(r.get("is_active")),
        "created_at": to_iso(r.get("created_at")) if r.get("created_at") else None,
        "last_used_at": to_iso(r.get("last_used_at")) if r.get("last_used_at") else None,
        "use_count": r.get("use_count"), "credits": r.get("credits"),
        "user_paygate_tier": r.get("user_paygate_tier"),
        "current_project_id": r.get("current_project_id"),
        "current_project_name": r.get("current_project_name"),
        "captcha_proxy_url": r.get("captcha_proxy_url") or "",
        "extension_route_key": r.get("extension_route_key") or "",
        "image_enabled": bool(r.get("image_enabled")), "video_enabled": bool(r.get("video_enabled")),
        "image_concurrency": r.get("image_concurrency"), "video_concurrency": r.get("video_concurrency"),
        "image_count": r.get("image_count", 0), "video_count": r.get("video_count", 0),
        "error_count": r.get("error_count", 0), "today_error_count": r.get("today_error_count", 0),
        "consecutive_error_count": r.get("consecutive_error_count", 0),
        "last_error_at": to_iso(r.get("last_error_at")) if r.get("last_error_at") else None,
        "ban_reason": r.get("ban_reason"),
        "banned_at": to_iso(r.get("banned_at")) if r.get("banned_at") else None,
    } for r in token_rows]

@router.post("/api/tokens")
async def add_token(request: AddTokenRequest, auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    try:
        new_token = await deps.token_manager.add_token(
            st=request.st, project_id=request.project_id, project_name=request.project_name,
            remark=request.remark, captcha_proxy_url=_strip(request.captcha_proxy_url),
            extension_route_key=_strip(request.extension_route_key),
            image_enabled=request.image_enabled, video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency, video_concurrency=request.video_concurrency,
            owner_id=user_id)
        if deps.concurrency_manager:
            await deps.concurrency_manager.reset_token(new_token.id, image_concurrency=new_token.image_concurrency, video_concurrency=new_token.video_concurrency)
        return {"success": True, "message": "Token添加成功", "token": {"id": new_token.id, "email": new_token.email, "credits": new_token.credits, "project_id": new_token.current_project_id, "project_name": new_token.current_project_name}}
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))
    except Exception as e: raise HTTPException(status_code=500, detail=f"添加Token失败: {str(e)}")

@router.put("/api/tokens/{token_id}")
async def update_token(token_id: int, request: UpdateTokenRequest, auth_data: dict = Depends(verify_admin_token)):
    try:
        result = await deps.token_manager.flow_client.st_to_at(request.st)
        at = result["access_token"]; expires = result.get("expires")
        at_expires = None
        if expires:
            try: at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            except: pass
        await deps.token_manager.update_token(token_id=token_id, st=request.st, at=at, at_expires=at_expires,
            project_id=request.project_id, project_name=request.project_name, remark=request.remark,
            captcha_proxy_url=_strip(request.captcha_proxy_url), extension_route_key=_strip(request.extension_route_key),
            image_enabled=request.image_enabled, video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency, video_concurrency=request.video_concurrency)
        if deps.concurrency_manager:
            ut = await deps.token_manager.get_token(token_id)
            if ut: await deps.concurrency_manager.reset_token(token_id, image_concurrency=ut.image_concurrency, video_concurrency=ut.video_concurrency)
        return {"success": True, "message": "Token更新成功"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/tokens/{token_id}")
async def delete_token(token_id: int, auth_data: dict = Depends(verify_admin_token)):
    try:
        await deps.token_manager.delete_token(token_id)
        if deps.concurrency_manager: await deps.concurrency_manager.remove_token(token_id)
        return {"success": True, "message": "Token删除成功"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tokens/{token_id}/enable")
async def enable_token(token_id: int, auth_data: dict = Depends(verify_admin_token)):
    await deps.token_manager.enable_token(token_id); return {"success": True, "message": "Token已启用"}

@router.post("/api/tokens/{token_id}/disable")
async def disable_token(token_id: int, auth_data: dict = Depends(verify_admin_token)):
    await deps.token_manager.disable_token(token_id); return {"success": True, "message": "Token已禁用"}

@router.post("/api/tokens/{token_id}/refresh-credits")
async def refresh_credits(token_id: int, auth_data: dict = Depends(verify_admin_token)):
    try:
        credits = await deps.token_manager.refresh_credits(token_id)
        return {"success": True, "message": "余额刷新成功", "credits": credits}
    except Exception as e: raise HTTPException(status_code=500, detail=f"刷新余额失败: {str(e)}")

@router.post("/api/tokens/{token_id}/refresh-at")
async def refresh_at(token_id: int, auth_data: dict = Depends(verify_admin_token)):
    from ..core.logger import debug_logger; from ..core.config import config
    try:
        success = await deps.token_manager._refresh_at(token_id)
        if success:
            ut = await deps.token_manager.get_token(token_id)
            msg = "AT刷新成功" + ("（支持ST自动刷新）" if config.captcha_method == "personal" else "")
            return {"success": True, "message": msg, "token": {"id": ut.id, "email": ut.email, "at_expires": ut.at_expires.isoformat() if ut.at_expires else None}}
        detail = "AT刷新失败" + (f"（当前打码模式: {config.captcha_method}）" if config.captcha_method != "personal" else "")
        raise HTTPException(status_code=500, detail=detail)
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=f"刷新AT失败: {str(e)}")

@router.post("/api/tokens/st2at")
async def st_to_at(request: ST2ATRequest, auth_data: dict = Depends(verify_admin_token)):
    try:
        result = await deps.token_manager.flow_client.st_to_at(request.st)
        return {"success": True, "message": "ST converted to AT successfully", "access_token": result["access_token"], "email": result.get("user", {}).get("email"), "expires": result.get("expires")}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/tokens/import")
async def import_tokens(request: ImportTokensRequest, auth_data: dict = Depends(verify_admin_token)):
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    added = 0; updated = 0; errors = []
    existing_by_email = {}
    for et in await deps.token_manager.get_all_tokens(user_id=user_id):
        if et.email and et.email not in existing_by_email: existing_by_email[et.email] = et
    for idx, item in enumerate(request.tokens):
        try:
            st = item.session_token
            if not st: errors.append(f"第{idx+1}项: 缺少 session_token"); continue
            try:
                result = await deps.token_manager.flow_client.st_to_at(st)
                at = result["access_token"]; email = result.get("user", {}).get("email"); expires = result.get("expires")
                if not email: errors.append(f"第{idx+1}项: 无法获取邮箱信息"); continue
                at_expires = None; is_expired = False
                if expires:
                    try: at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00')); is_expired = at_expires <= datetime.now(timezone.utc)
                    except: pass
                existing = existing_by_email.get(email)
                if existing:
                    await deps.token_manager.update_token(token_id=existing.id, st=st, at=at, at_expires=at_expires,
                        captcha_proxy_url=_strip(item.captcha_proxy_url), extension_route_key=_strip(item.extension_route_key),
                        image_enabled=item.image_enabled, video_enabled=item.video_enabled, image_concurrency=item.image_concurrency, video_concurrency=item.video_concurrency)
                    if is_expired: await deps.token_manager.disable_token(existing.id)
                    updated += 1
                else:
                    nt = await deps.token_manager.add_token(st=st, captcha_proxy_url=_strip(item.captcha_proxy_url), extension_route_key=_strip(item.extension_route_key),
                        image_enabled=item.image_enabled, video_enabled=item.video_enabled, image_concurrency=item.image_concurrency, video_concurrency=item.video_concurrency, owner_id=user_id)
                    if is_expired: await deps.token_manager.disable_token(nt.id)
                    existing_by_email[email] = nt; added += 1
            except Exception as e: errors.append(f"第{idx+1}项: {str(e)}")
        except Exception as e: errors.append(f"第{idx+1}项: {str(e)}")
    return {"success": True, "added": added, "updated": updated, "errors": errors if errors else None,
            "message": f"导入完成: 新增 {added} 个, 更新 {updated} 个" + (f", {len(errors)} 个失败" if errors else "")}
