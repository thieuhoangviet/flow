import datetime
import secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from . import _admin_deps as deps
from .admin_auth import verify_super_admin

router = APIRouter(prefix="/api/admin/users", tags=["admin_users"])

class UpdateUserRequest(BaseModel):
    months: Optional[int] = None # 3, 6, 12 months

@router.get("")
async def list_users(auth_data: dict = Depends(verify_super_admin)):
    users = await deps.db.get_all_users()
    # Mask password_hash
    for user in users:
        user.pop("password_hash", None)
    return {"users": users}

@router.post("/{user_id}/grant")
async def grant_user_access(user_id: int, req: UpdateUserRequest, auth_data: dict = Depends(verify_super_admin)):
    if req.months is None or req.months <= 0:
        raise HTTPException(status_code=400, detail="Invalid months")
        
    api_key = f"user_{secrets.token_hex(16)}"
    expires_at = datetime.datetime.now() + datetime.timedelta(days=30 * req.months)
    
    success = await deps.db.update_user(
        user_id,
        api_key=api_key,
        expires_at=expires_at.isoformat()
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update user")
        
    return {"message": f"Granted {req.months} months access", "api_key": api_key, "expires_at": expires_at.isoformat()}

@router.delete("/{user_id}")
async def delete_user(user_id: int, auth_data: dict = Depends(verify_super_admin)):
    success = await deps.db.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete user")
    return {"message": "User deleted"}
