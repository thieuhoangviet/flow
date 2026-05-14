"""Risk-aware generation worker registry manager."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .risk_classifier import RiskDecision, classify_generation_error


class WorkerManager:
    """Persist and update worker/account/proxy risk state."""

    def __init__(self, db):
        self.db = db

    async def ensure_default_workers_from_tokens(self) -> int:
        """Create one local worker per active token if missing."""
        tokens = []
        if hasattr(self.db, "get_all_tokens"):
            tokens = await self.db.get_all_tokens()
        created = 0
        async with self.db._connect(write=True) as conn:
            for token in tokens or []:
                token_id = _get(token, "id")
                if token_id is None:
                    continue
                worker_id = f"local-token-{token_id}"
                cursor = await conn.execute("SELECT id FROM generation_workers WHERE worker_id = ?", (worker_id,))
                exists = await cursor.fetchone()
                if exists:
                    continue
                email = _get(token, "email") or _get(token, "name") or f"token-{token_id}"
                project_id = _get(token, "current_project_id")
                proxy_url = _get(token, "captcha_proxy_url")
                now = _now()
                await conn.execute(
                    """
                    INSERT INTO generation_workers (
                        worker_id, label, worker_type, account_label, token_id,
                        proxy_url, project_id, status, created_at, updated_at
                    ) VALUES (?, ?, 'local', ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (worker_id, f"Local Token {token_id}", email, token_id, proxy_url, project_id, now, now),
                )
                created += 1
            await conn.commit()
        return created

    async def release_expired_cooldowns(self) -> int:
        now = _now()
        async with self.db._connect(write=True) as conn:
            cursor = await conn.execute(
                """
                UPDATE generation_workers
                SET status = 'active', cooldown_until = NULL, updated_at = ?
                WHERE status = 'cooldown' AND cooldown_until IS NOT NULL AND cooldown_until <= ?
                """,
                (now, now),
            )
            await conn.commit()
            return cursor.rowcount or 0

    async def list_workers(self) -> List[Dict[str, Any]]:
        await self.release_expired_cooldowns()
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, worker_id, label, worker_type, account_label, token_id,
                       proxy_url, profile_dir, project_id, status, risk_score,
                       cooldown_until, last_error, last_error_code, last_success_at,
                       last_used_at, success_count, error_count, created_at, updated_at
                FROM generation_workers
                ORDER BY status = 'active' DESC, risk_score ASC, id ASC
                """
            )
            rows = await cursor.fetchall()
        return [_row_to_worker(row) for row in rows]

    async def select_available_worker(self) -> Optional[Dict[str, Any]]:
        await self.release_expired_cooldowns()
        now = _now()
        async with self.db._connect(write=True) as conn:
            cursor = await conn.execute(
                """
                SELECT id, worker_id, label, worker_type, account_label, token_id,
                       proxy_url, profile_dir, project_id, status, risk_score,
                       cooldown_until, last_error, last_error_code, last_success_at,
                       last_used_at, success_count, error_count, created_at, updated_at
                FROM generation_workers
                WHERE status = 'active'
                ORDER BY risk_score ASC, last_used_at IS NOT NULL ASC, last_used_at ASC, id ASC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            if not row:
                return None
            worker = _row_to_worker(row)
            await conn.execute(
                "UPDATE generation_workers SET last_used_at = ?, updated_at = ? WHERE worker_id = ?",
                (now, now, worker["worker_id"]),
            )
            await conn.commit()
            return worker

    async def mark_success(self, worker_id: str) -> Optional[Dict[str, Any]]:
        now = _now()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE generation_workers
                SET status = 'active', cooldown_until = NULL, last_error = NULL,
                    last_error_code = NULL, last_success_at = ?, last_used_at = ?,
                    success_count = success_count + 1,
                    risk_score = CASE WHEN risk_score >= 5 THEN risk_score - 5 ELSE 0 END,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                (now, now, now, worker_id),
            )
            await conn.commit()
        return await self.get_worker(worker_id)

    async def mark_error(self, worker_id: str, error: Any) -> Dict[str, Any]:
        decision = classify_generation_error(error)
        cooldown_until = None
        status = "active"
        if decision.cooldown_seconds > 0 and decision.is_risk_error:
            cooldown_until = (datetime.utcnow() + timedelta(seconds=decision.cooldown_seconds)).isoformat()
            status = "cooldown"
        now = _now()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE generation_workers
                SET status = ?, cooldown_until = ?, last_error = ?, last_error_code = ?,
                    error_count = error_count + 1,
                    risk_score = CASE WHEN risk_score + ? > 100 THEN 100 ELSE risk_score + ? END,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                (
                    status,
                    cooldown_until,
                    str(error)[:2000],
                    decision.risk_code,
                    decision.risk_delta,
                    decision.risk_delta,
                    now,
                    worker_id,
                ),
            )
            await conn.commit()
        worker = await self.get_worker(worker_id)
        return {"worker": worker, "decision": decision.to_dict()}

    async def get_worker(self, worker_id: str) -> Optional[Dict[str, Any]]:
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                """
                SELECT id, worker_id, label, worker_type, account_label, token_id,
                       proxy_url, profile_dir, project_id, status, risk_score,
                       cooldown_until, last_error, last_error_code, last_success_at,
                       last_used_at, success_count, error_count, created_at, updated_at
                FROM generation_workers WHERE worker_id = ?
                """,
                (worker_id,),
            )
            row = await cursor.fetchone()
        return _row_to_worker(row) if row else None

    async def set_status(self, worker_id: str, status: str, cooldown_seconds: int = 0) -> Optional[Dict[str, Any]]:
        allowed = {"active", "cooldown", "disabled", "maintenance"}
        if status not in allowed:
            raise ValueError(f"Invalid worker status: {status}")
        cooldown_until = None
        if status == "cooldown" and cooldown_seconds > 0:
            cooldown_until = (datetime.utcnow() + timedelta(seconds=cooldown_seconds)).isoformat()
        now = _now()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE generation_workers SET status = ?, cooldown_until = ?, updated_at = ? WHERE worker_id = ?",
                (status, cooldown_until, now, worker_id),
            )
            await conn.commit()
        return await self.get_worker(worker_id)

    async def reset_risk(self, worker_id: str) -> Optional[Dict[str, Any]]:
        now = _now()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE generation_workers
                SET status = 'active', risk_score = 0, cooldown_until = NULL,
                    last_error = NULL, last_error_code = NULL, updated_at = ?
                WHERE worker_id = ?
                """,
                (now, worker_id),
            )
            await conn.commit()
        return await self.get_worker(worker_id)


def _row_to_worker(row) -> Dict[str, Any]:
    keys = [
        "id", "worker_id", "label", "worker_type", "account_label", "token_id",
        "proxy_url", "profile_dir", "project_id", "status", "risk_score",
        "cooldown_until", "last_error", "last_error_code", "last_success_at",
        "last_used_at", "success_count", "error_count", "created_at", "updated_at",
    ]
    return dict(zip(keys, row))


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _now() -> str:
    return datetime.utcnow().isoformat()
