"""Risk-aware worker administration API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .admin_auth import verify_admin_token
from ..services.risk.risk_classifier import classify_generation_error
from ..services.risk.worker_manager import WorkerManager

router = APIRouter(prefix="/api/workers", tags=["workers"])
_db = None
_manager: Optional[WorkerManager] = None


def set_db(db):
    global _db, _manager
    _db = db
    _manager = WorkerManager(db)


def get_worker_manager() -> WorkerManager:
    if _manager is None:
        raise HTTPException(status_code=500, detail="Worker manager not initialized")
    return _manager


class CooldownRequest(BaseModel):
    seconds: int = 7200


class ClassifyRequest(BaseModel):
    message: str


@router.get("")
async def list_workers(auth_data: dict = Depends(verify_admin_token)):
    manager = get_worker_manager()
    created = await manager.ensure_default_workers_from_tokens()
    workers = await manager.list_workers()
    return {"success": True, "created": created, "workers": workers}


@router.post("/sync")
async def sync_workers(auth_data: dict = Depends(verify_admin_token)):
    manager = get_worker_manager()
    created = await manager.ensure_default_workers_from_tokens()
    return {"success": True, "created": created, "workers": await manager.list_workers()}


@router.get("/available")
async def get_available_worker(auth_data: dict = Depends(verify_admin_token)):
    manager = get_worker_manager()
    worker = await manager.select_available_worker()
    return {"success": True, "worker": worker}


@router.post("/classify")
async def classify_error(payload: ClassifyRequest, auth_data: dict = Depends(verify_admin_token)):
    return {"success": True, "decision": classify_generation_error(payload.message).to_dict()}


@router.post("/{worker_id}/enable")
async def enable_worker(worker_id: str, auth_data: dict = Depends(verify_admin_token)):
    worker = await get_worker_manager().set_status(worker_id, "active")
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"success": True, "worker": worker}


@router.post("/{worker_id}/disable")
async def disable_worker(worker_id: str, auth_data: dict = Depends(verify_admin_token)):
    worker = await get_worker_manager().set_status(worker_id, "disabled")
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"success": True, "worker": worker}


@router.post("/{worker_id}/cooldown")
async def cooldown_worker(worker_id: str, payload: CooldownRequest, auth_data: dict = Depends(verify_admin_token)):
    seconds = max(60, min(24 * 60 * 60, int(payload.seconds)))
    worker = await get_worker_manager().set_status(worker_id, "cooldown", seconds)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"success": True, "worker": worker}


@router.post("/{worker_id}/reset-risk")
async def reset_worker_risk(worker_id: str, auth_data: dict = Depends(verify_admin_token)):
    worker = await get_worker_manager().reset_risk(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"success": True, "worker": worker}


@router.post("/{worker_id}/simulate-error")
async def simulate_worker_error(worker_id: str, payload: ClassifyRequest, auth_data: dict = Depends(verify_admin_token)):
    result = await get_worker_manager().mark_error(worker_id, payload.message)
    if not result.get("worker"):
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"success": True, **result}
