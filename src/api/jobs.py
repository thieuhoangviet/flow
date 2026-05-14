"""Persistent generation job API for the test dashboard."""
import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.auth import verify_api_key_flexible

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
_db = None


def set_db(db):
    global _db
    _db = db


def _ensure_db():
    if _db is None:
        raise HTTPException(status_code=500, detail="Jobs database not initialized")
    return _db


class JobCreateRequest(BaseModel):
    model: str = ""
    prompt: str = ""
    mode: str = "manual"
    status: str = "queued"
    stage: str = "queued"


class JobUpdateRequest(BaseModel):
    status: Optional[str] = None
    stage: Optional[str] = None
    progress: Optional[int] = None
    error_message: Optional[str] = None
    result_json: Optional[Dict[str, Any]] = None
    media_urls: Optional[list[str]] = None
    log_text: Optional[str] = None


async def _row_to_job(row):
    if not row:
        return None
    keys = [
        "id", "job_id", "mode", "model", "prompt", "status", "stage",
        "progress", "error_message", "result_json", "media_urls", "log_text",
        "created_at", "updated_at", "completed_at",
    ]
    data = dict(zip(keys, row))
    for field, fallback in (("result_json", {}), ("media_urls", [])):
        raw = data.get(field)
        if isinstance(raw, str) and raw:
            try:
                data[field] = json.loads(raw)
            except Exception:
                data[field] = fallback
        elif raw is None:
            data[field] = fallback
    return data


@router.post("")
async def create_job(payload: JobCreateRequest, api_key: str = Depends(verify_api_key_flexible)):
    dbm = _ensure_db()
    job_id = f"job_{uuid.uuid4().hex}"
    now = datetime.utcnow().isoformat()
    async with dbm._connect(write=True) as db:
        await db.execute(
            """
            INSERT INTO generation_jobs (
                job_id, mode, model, prompt, status, stage, progress,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, payload.mode, payload.model, payload.prompt,
                payload.status or "queued", payload.stage or "queued", 0, now, now,
            ),
        )
        await db.commit()
    return {"job_id": job_id, "status": payload.status or "queued", "stage": payload.stage or "queued"}


@router.get("")
async def list_jobs(limit: int = 20, api_key: str = Depends(verify_api_key_flexible)):
    dbm = _ensure_db()
    safe_limit = max(1, min(100, int(limit or 20)))
    async with dbm._connect() as db:
        cursor = await db.execute(
            """
            SELECT id, job_id, mode, model, prompt, status, stage, progress,
                   error_message, result_json, media_urls, log_text,
                   created_at, updated_at, completed_at
            FROM generation_jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        )
        rows = await cursor.fetchall()
    return {"jobs": [await _row_to_job(row) for row in rows]}


@router.get("/{job_id}")
async def get_job(job_id: str, api_key: str = Depends(verify_api_key_flexible)):
    dbm = _ensure_db()
    async with dbm._connect() as db:
        cursor = await db.execute(
            """
            SELECT id, job_id, mode, model, prompt, status, stage, progress,
                   error_message, result_json, media_urls, log_text,
                   created_at, updated_at, completed_at
            FROM generation_jobs WHERE job_id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
    job = await _row_to_job(row)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch("/{job_id}")
async def update_job(job_id: str, payload: JobUpdateRequest, api_key: str = Depends(verify_api_key_flexible)):
    dbm = _ensure_db()
    fields = []
    values = []
    if payload.status is not None:
        fields.append("status = ?")
        values.append(payload.status)
    if payload.stage is not None:
        fields.append("stage = ?")
        values.append(payload.stage)
    if payload.progress is not None:
        fields.append("progress = ?")
        values.append(max(0, min(100, int(payload.progress))))
    if payload.error_message is not None:
        fields.append("error_message = ?")
        values.append(payload.error_message)
    if payload.result_json is not None:
        fields.append("result_json = ?")
        values.append(json.dumps(payload.result_json, ensure_ascii=False))
    if payload.media_urls is not None:
        fields.append("media_urls = ?")
        values.append(json.dumps(payload.media_urls, ensure_ascii=False))
    if payload.log_text is not None:
        fields.append("log_text = ?")
        values.append(payload.log_text)
    if payload.status in ("completed", "failed", "cancelled"):
        fields.append("completed_at = ?")
        values.append(datetime.utcnow().isoformat())
    fields.append("updated_at = ?")
    values.append(datetime.utcnow().isoformat())
    if not fields:
        return await get_job(job_id, api_key)
    values.append(job_id)
    async with dbm._connect(write=True) as db:
        cursor = await db.execute(f"UPDATE generation_jobs SET {', '.join(fields)} WHERE job_id = ?", values)
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job not found")
    return await get_job(job_id, api_key)


@router.get("/{job_id}/events")
async def job_events(job_id: str, api_key: str = Depends(verify_api_key_flexible)):
    async def event_stream():
        last_payload = None
        for _ in range(900):
            try:
                job = await get_job(job_id, api_key)
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
                return
            payload = json.dumps(job, ensure_ascii=False)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if job.get("status") in ("completed", "failed", "cancelled"):
                return
            await asyncio.sleep(1)
        yield f"event: timeout\ndata: {json.dumps({'job_id': job_id, 'time': time.time()})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
