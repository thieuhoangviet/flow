import datetime
from typing import Optional, List, Dict, Any
from .base import DatabaseBaseMixin

class DatabaseUsersMixin(DatabaseBaseMixin):
    
    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
            row = await cursor.fetchone()
            if row:
                cols = [description[0] for description in cursor.description]
                return dict(zip(cols, row))
            return None

    async def get_user_by_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM users WHERE api_key = ?", (api_key,))
            row = await cursor.fetchone()
            if row:
                cols = [description[0] for description in cursor.description]
                return dict(zip(cols, row))
            return None

    async def create_user(self, username: str, password_hash: str, api_key: str, role: str = 'user', expires_at: Optional[datetime.datetime] = None, gemini_api_key: str = "") -> bool:
        async with self._connect(write=True) as db:
            try:
                await db.execute("""
                    INSERT INTO users (username, password_hash, api_key, role, expires_at, gemini_api_key)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (username, password_hash, api_key, role, expires_at, gemini_api_key))
                await db.commit()
                return True
            except Exception as e:
                print(f"Error creating user: {e}")
                return False

    async def update_user(self, user_id: int, **kwargs) -> bool:
        if not kwargs:
            return True
        
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values())
        values.append(user_id)
        
        async with self._connect(write=True) as db:
            try:
                await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
                await db.commit()
                return True
            except Exception as e:
                print(f"Error updating user: {e}")
                return False

    async def delete_user(self, user_id: int) -> bool:
        async with self._connect(write=True) as db:
            try:
                await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
                await db.commit()
                return True
            except Exception:
                return False

    async def get_all_users(self) -> List[Dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            cols = [description[0] for description in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

