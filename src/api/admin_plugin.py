"""Admin plugin configuration endpoints."""
import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from typing import Optional
from . import _admin_deps as deps
from .admin_auth import verify_admin_token

router = APIRouter()

@router.get("/api/plugin/config")
async def get_plugin_config(request: Request, token: str = Depends(verify_admin_token)):
    pc = await deps.db.get_plugin_config()
    host_header = request.headers.get("host", "")
    if host_header:
        connection_url = f"http://{host_header}/api/plugin/update-token"
    else:
        from ..core.config import config
        sh = config.server_host; sp = config.server_port
        connection_url = f"http://{'127.0.0.1' if sh == '0.0.0.0' else sh}:{sp}/api/plugin/update-token"
    return {"success": True, "config": {"connection_token": pc.connection_token, "connection_url": connection_url, "auto_enable_on_update": pc.auto_enable_on_update, "gemini_api_key": pc.gemini_api_key}}

@router.post("/api/plugin/config")
async def update_plugin_config(request: dict, token: str = Depends(verify_admin_token)):
    ct = request.get("connection_token", ""); ae = request.get("auto_enable_on_update", True)
    gk = request.get("gemini_api_key", "")
    if not ct: ct = secrets.token_urlsafe(32)
    await deps.db.update_plugin_config(connection_token=ct, auto_enable_on_update=ae, gemini_api_key=gk)
    return {"success": True, "message": "插件配置更新成功", "connection_token": ct, "auto_enable_on_update": ae, "gemini_api_key": gk}

@router.post("/api/plugin/update-token")
async def plugin_update_token(request: dict, authorization: Optional[str] = Header(None)):
    pc = await deps.db.get_plugin_config()
    provided_token = None
    if authorization:
        provided_token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if not pc.connection_token or provided_token != pc.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")
    session_token = request.get("session_token")
    if not session_token: raise HTTPException(status_code=400, detail="Missing session_token")
    try:
        result = await deps.token_manager.flow_client.st_to_at(session_token)
        at = result["access_token"]; expires = result.get("expires"); email = result.get("user", {}).get("email", "")
        if not email: raise HTTPException(status_code=400, detail="Failed to get email from session token")
        at_expires = None
        if expires:
            try: at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            except: pass
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=400, detail=f"Invalid session token: {str(e)}")
    existing_token = await deps.db.get_token_by_email(email)
    if existing_token:
        try:
            await deps.token_manager.update_token(token_id=existing_token.id, st=session_token, at=at, at_expires=at_expires)
            if pc.auto_enable_on_update and not existing_token.is_active:
                await deps.token_manager.enable_token(existing_token.id)
                return {"success": True, "message": f"Token updated and auto-enabled for {email}", "action": "updated", "auto_enabled": True}
            return {"success": True, "message": f"Token updated for {email}", "action": "updated"}
        except Exception as e: raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")
    else:
        try:
            nt = await deps.token_manager.add_token(st=session_token, remark="Added by Chrome Extension")
            return {"success": True, "message": f"Token added for {nt.email}", "action": "added", "token_id": nt.id}
        except Exception as e: raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")
