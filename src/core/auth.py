import bcrypt
from typing import Optional
from fastapi import Header, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import datetime
from .config import config

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)

class AuthManager:
    """Authentication manager"""
    _db = None

    @classmethod
    def set_db(cls, db):
        cls._db = db

    @classmethod
    async def verify_api_key(cls, api_key: str) -> bool:
        """Verify API key from config or users table"""
        if api_key == config.api_key:
            return True
            
        if cls._db:
            user = await cls._db.get_user_by_api_key(api_key)
            if user:
                # Check expiration
                expires_at_str = user.get("expires_at")
                if expires_at_str:
                    try:
                        expires_at = datetime.datetime.fromisoformat(expires_at_str)
                        if datetime.datetime.now() > expires_at:
                            return False # Expired
                    except ValueError:
                        pass
                return True
        return False

    @staticmethod
    def verify_admin(username: str, password: str) -> bool:
        """Verify admin credentials"""
        return username == config.admin_username and password == config.admin_password

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password"""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify password"""
        return bcrypt.checkpw(password.encode(), hashed.encode())

    @classmethod
    async def verify_api_key_flexible_dependency(
        cls,
        credentials: Optional[HTTPAuthorizationCredentials] = Security(optional_security),
        x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
        key: Optional[str] = Query(None),
    ) -> str:
        """Dependency for verifying API key from multiple sources."""
        api_key = None
        if credentials is not None:
            api_key = credentials.credentials
        elif x_goog_api_key:
            api_key = x_goog_api_key
        elif key:
            api_key = key

        if not api_key or not await cls.verify_api_key(api_key):
            raise HTTPException(status_code=401, detail="Invalid or expired API key")

        return api_key

async def verify_api_key_header(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    api_key = credentials.credentials
    if not await AuthManager.verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

async def verify_api_key_flexible(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(optional_security),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    key: Optional[str] = Query(None),
) -> str:
    return await AuthManager.verify_api_key_flexible_dependency(credentials, x_goog_api_key, key)
