from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import os
from pathlib import Path

from src.workflows.article_to_video import stream_article_to_video
from src.core.database import Database

router = APIRouter(tags=["Article to Video"])

# Ensure output dir exists in tmp so UI can load videos via /tmp/ endpoint
OUTPUT_DIR = Path("tmp/output_article")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

class ArticleRequest(BaseModel):
    url: str
    gemini_key: str

@router.get("/api/article-to-video/stream")
async def stream_article(
    url: str = Query(...)
):
    """
    Stream the progress of the article to video pipeline using SSE.
    """
    db = Database()
    plugin_config = await db.get_plugin_config()
    gemini_key = plugin_config.gemini_api_key
    
    if not gemini_key:
        return StreamingResponse(
            iter(["data: {\"type\": \"log\", \"data\": \"LỖI: Chưa cấu hình Gemini API Key! Vui lòng vào trang /manage tab Cài Đặt để điền Gemini API Key.\"}\n\n"]),
            media_type="text/event-stream"
        )

    output_dir = str(OUTPUT_DIR.absolute())
    
    # Return a StreamingResponse using the async generator
    return StreamingResponse(
        stream_article_to_video(url, gemini_key, output_dir),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
