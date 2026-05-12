from fastapi import APIRouter, Query, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import os
from pathlib import Path

from src.workflows.article_to_video import stream_article_to_video
from src.core.database import Database
from src.api.admin_auth import verify_admin_token

router = APIRouter(tags=["Article to Video"])

# Ensure output dir exists in tmp so UI can load videos via /tmp/ endpoint
OUTPUT_DIR = Path("tmp/output_article")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

class ArticleRequest(BaseModel):
    url: str
    gemini_key: str

@router.get("/api/article-to-video/stream")
async def stream_article(
    url: str = Query(...),
    auth_data: dict = Depends(verify_admin_token)
):
    """
    Stream the progress of the article to video pipeline using SSE.
    """
    db = Database()
    if auth_data["role"] == "user":
        gemini_key = auth_data["user"].get("gemini_api_key")
        if not gemini_key:
            return StreamingResponse(
                iter(["data: {\"type\": \"log\", \"data\": \"LỖI: Chưa cấu hình Gemini API Key cá nhân! Vui lòng vào Cấu hình hệ thống để điền API Key của bạn.\"}\n\n"]),
                media_type="text/event-stream"
            )
    else:
        plugin_config = await db.get_plugin_config()
        gemini_key = plugin_config.gemini_api_key
        
        if not gemini_key:
            return StreamingResponse(
                iter(["data: {\"type\": \"log\", \"data\": \"LỖI: Chưa cấu hình Gemini API Key hệ thống! Vui lòng vào trang /manage tab Cấu hình hệ thống để điền Gemini API Key.\"}\n\n"]),
                media_type="text/event-stream"
            )

    output_dir = str(OUTPUT_DIR.absolute())
    user_id = 0 if auth_data["role"] == "admin" else auth_data["user"]["id"]
    
    # Return a StreamingResponse using the async generator
    return StreamingResponse(
        stream_article_to_video(url, gemini_key, output_dir, user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
