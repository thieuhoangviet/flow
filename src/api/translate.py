"""Translation API endpoint."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _translate_sync(text: str, source: str = "vi", target: str = "en") -> dict:
    """Translate text using Google Translate (blocking, for asyncio.to_thread)."""
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source=source, target=target).translate(text)
        return {"success": True, "translated": translated}
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/translate")
async def translate_text(request: Request):
    """Translate text for video prompts.
    
    Body:
        texts: list of strings to translate
        source: source language (default "vi")
        target: target language (default "en")
    """
    try:
        body = await request.json()
        texts = body.get("texts", [])
        source = body.get("source", "vi")
        target = body.get("target", "en")

        if not texts:
            return JSONResponse({"error": "Cần danh sách texts"}, status_code=400)

        results = []
        for text in texts:
            if not text or not text.strip():
                results.append("")
                continue
            result = await asyncio.to_thread(_translate_sync, text.strip(), source, target)
            if result.get("success"):
                results.append(result["translated"])
            else:
                # If translation fails, use original text
                results.append(text)

        return JSONResponse({"success": True, "translations": results})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
