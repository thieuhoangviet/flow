"""Admin authentication endpoints."""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from typing import Optional

from ..core.auth import AuthManager
from . import _admin_deps as deps

router = APIRouter()

# Store active admin session tokens (in production, use Redis or database)
active_admin_tokens = set()


# ========== Request Models ==========

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


# ========== Auth Middleware ==========

async def verify_admin_token(
    authorization: str = Header(None),
    token: str = Query(None)
):
    """Verify admin session token or user API key"""
    auth_token = None
    if authorization and authorization.startswith("Bearer "):
        auth_token = authorization[7:]
    elif token:
        auth_token = token
        
    if not auth_token:
        raise HTTPException(status_code=401, detail="Missing authorization")

    # Check if token is in active admin session tokens
    if auth_token in active_admin_tokens:
        return {"role": "admin", "token": auth_token}

    # If not admin, check if it's a valid user API key
    user = await deps.db.get_user_by_api_key(auth_token)
    if user:
        from datetime import datetime
        if user["expires_at"]:
            exp = datetime.fromisoformat(user["expires_at"])
            if datetime.now() > exp:
                raise HTTPException(status_code=401, detail="API Key expired")
        return {"role": "user", "token": token, "user": user}

    raise HTTPException(status_code=401, detail="Invalid or expired token")

async def verify_super_admin(auth_data: dict = Depends(verify_admin_token)):
    """Only allow real admins"""
    if auth_data["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth_data


# ========== Auth Endpoints ==========

@router.post("/api/admin/login")
async def admin_login(request: LoginRequest):
    """Admin or User login - returns session token or user API key"""
    admin_config = await deps.db.get_admin_config()

    if AuthManager.verify_admin(request.username, request.password):
        # Admin login
        session_token = f"admin-{secrets.token_urlsafe(32)}"
        active_admin_tokens.add(session_token)
        return {
            "success": True,
            "token": session_token,
            "username": admin_config.username
        }
        
    # Check if regular user
    user = await deps.db.get_user_by_username(request.username)
    if user and AuthManager.verify_password(request.password, user["password_hash"]):
        if not user.get("api_key"):
            raise HTTPException(status_code=403, detail="Tài khoản đang chờ Admin cấp Key")
        
        # Check expiration
        if user.get("expires_at"):
            from datetime import datetime
            try:
                exp = datetime.fromisoformat(user["expires_at"])
                if datetime.now() > exp:
                    raise HTTPException(status_code=403, detail="Tài khoản đã hết hạn")
            except:
                pass
                
        return {
            "success": True,
            "token": user["api_key"],
            "username": user["username"]
        }

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/api/admin/logout")
async def admin_logout(auth_data: dict = Depends(verify_super_admin)):
    """Admin logout - invalidate session token"""
    active_admin_tokens.discard(auth_data["token"])
    return {"success": True, "message": "退出登录成功"}


@router.post("/api/admin/change-password")
async def change_password(
    request: ChangePasswordRequest,
    auth_data: dict = Depends(verify_super_admin)
):
    """Change admin password"""
    admin_config = await deps.db.get_admin_config()

    # Verify old password
    if not AuthManager.verify_admin(admin_config.username, request.old_password):
        raise HTTPException(status_code=400, detail="旧密码错误")

    # Update password and username in database
    update_params = {"password": request.new_password}
    if request.username:
        update_params["username"] = request.username

    await deps.db.update_admin_config(**update_params)

    # Hot reload: sync database config to memory
    await deps.db.reload_config_to_memory()

    # Invalidate all admin session tokens (force re-login for security)
    active_admin_tokens.clear()

    return {"success": True, "message": "密码修改成功,请重新登录"}


# ========== Aliases for Frontend Compatibility ==========

@router.post("/api/login")
async def login(request: LoginRequest):
    """Login endpoint (alias for /api/admin/login)"""
    return await admin_login(request)


@router.post("/api/logout")
async def logout(auth_data: dict = Depends(verify_super_admin)):
    """Logout endpoint (alias for /api/admin/logout)"""
    return await admin_logout(auth_data)


@router.post("/api/admin/password")
async def update_admin_password(
    request: ChangePasswordRequest,
    auth_data: dict = Depends(verify_super_admin)
):
    """Update admin password"""
    return await change_password(request, auth_data)
