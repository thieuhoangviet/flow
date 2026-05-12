from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import datetime
import secrets

from ..core.auth import AuthManager
from ..core.db.manager import Database

router = APIRouter(prefix="/api/auth", tags=["auth"])

# The global DB instance injected from main
_db: Optional[Database] = None

def set_db(db: Database):
    global _db
    _db = db

class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/register")
async def register_user(req: RegisterRequest):
    if not _db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    existing = await _db.get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    
    password_hash = AuthManager.hash_password(req.password)
    # Generate a unique API key for the user upon registration
    new_api_key = f"sk-flow2api-{secrets.token_hex(16)}"
    
    success = await _db.create_user(
        username=req.username,
        password_hash=password_hash,
        api_key=new_api_key,
        role="user",
        expires_at=None,
        gemini_api_key=""
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to create user")
    
    return {"message": "Registration successful. You can now login."}

@router.post("/login")
async def login_user(req: LoginRequest):
    if not _db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    user = await _db.get_user_by_username(req.username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    if not AuthManager.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    # Return user details including API key and expiration
    expires_at = user.get("expires_at")
    is_expired = False
    if expires_at:
        try:
            exp_date = datetime.datetime.fromisoformat(expires_at)
            if datetime.datetime.now() > exp_date:
                is_expired = True
        except:
            pass

    return {
        "message": "Login successful",
        "username": user["username"],
        "api_key": user.get("api_key"),
        "expires_at": expires_at,
        "is_expired": is_expired
    }

from .admin_auth import verify_admin_token

@router.get("/me")
async def get_my_info(auth_data: dict = Depends(verify_admin_token)):
    if auth_data["role"] == "admin":
        from ..core.config import config
        return {
            "role": "admin",
            "username": config.admin_username,
            "api_key": config.api_key,
            "expires_at": None
        }
    else:
        user = auth_data["user"]
        return {
            "role": "user",
            "username": user["username"],
            "api_key": user["api_key"],
            "expires_at": user.get("expires_at")
        }

@router.get("/me/config")
async def get_my_config(auth_data: dict = Depends(verify_admin_token)):
    if auth_data["role"] == "admin":
        # Admin config logic (use global plugin config for Gemini)
        pc = await _db.get_plugin_config()
        return {"success": True, "gemini_api_key": pc.gemini_api_key}
    else:
        user = auth_data["user"]
        return {"success": True, "gemini_api_key": user.get("gemini_api_key", "")}

class UpdateConfigReq(BaseModel):
    gemini_api_key: str

@router.post("/me/config")
async def update_my_config(req: UpdateConfigReq, auth_data: dict = Depends(verify_admin_token)):
    if auth_data["role"] == "admin":
        pc = await _db.get_plugin_config()
        await _db.update_plugin_config(
            connection_token=pc.connection_token,
            auto_enable_on_update=pc.auto_enable_on_update,
            gemini_api_key=req.gemini_api_key
        )
        return {"success": True, "message": "Updated global Gemini API key"}
    else:
        user_id = auth_data["user"]["id"]
        success = await _db.update_user(user_id, gemini_api_key=req.gemini_api_key)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update personal config")
        return {"success": True, "message": "Updated personal Gemini API key"}

class UpdatePasswordReq(BaseModel):
    old_password: str
    new_password: str

@router.post("/me/password")
async def update_my_password(req: UpdatePasswordReq, auth_data: dict = Depends(verify_admin_token)):
    if auth_data["role"] == "admin":
        raise HTTPException(status_code=400, detail="Admin must use the admin password endpoint")
    
    user = auth_data["user"]
    if not AuthManager.verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Mật khẩu cũ không chính xác")
        
    new_hash = AuthManager.hash_password(req.new_password)
    success = await _db.update_user(user["id"], password_hash=new_hash)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update password")
        
    return {"success": True, "message": "Đổi mật khẩu thành công"}

class UpdateApiKeyReq(BaseModel):
    new_api_key: str

@router.post("/me/apikey")
async def update_my_apikey(req: UpdateApiKeyReq, auth_data: dict = Depends(verify_admin_token)):
    if auth_data["role"] == "admin":
        raise HTTPException(status_code=400, detail="Admin must use the admin apikey endpoint")
    
    user_id = auth_data["user"]["id"]
    # Check if API key is already taken by another user or admin
    existing_user = await _db.get_user_by_api_key(req.new_api_key)
    if existing_user and existing_user["id"] != user_id:
        raise HTTPException(status_code=400, detail="API Key này đã tồn tại, vui lòng chọn API Key khác")
        
    from ..core.config import config
    if req.new_api_key == config.api_key:
        raise HTTPException(status_code=400, detail="Không thể sử dụng API Key này")
        
    success = await _db.update_user(user_id, api_key=req.new_api_key)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update API key")
        
    return {"success": True, "message": "Đổi API Key thành công", "new_api_key": req.new_api_key}
