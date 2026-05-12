"""TTS narration API endpoints."""
import asyncio
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _get_ffmpeg_path() -> str:
    """Get ffmpeg binary path from imageio-ffmpeg."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _run_tts_sync(text: str, voice: str, output_path: str, rate: str = "+0%", pitch: str = "+0Hz") -> dict:
    """Generate TTS audio using edge-tts with natural prosody (blocking, for asyncio.to_thread).
    
    Uses SSML-like prosody settings for more natural speech.
    Adds natural pauses between sentences.
    """
    import asyncio as _asyncio

    async def _generate():
        import edge_tts
        # Add natural pauses between sentences
        processed_text = text.replace(". ", ". ... ").replace("! ", "! ... ").replace("? ", "? ... ")
        communicate = edge_tts.Communicate(
            processed_text, 
            voice,
            rate=rate,
            pitch=pitch,
        )
        await communicate.save(output_path)

    # Run in a fresh event loop (we're in a thread)
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(_generate())
    finally:
        loop.close()

    if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
        return {"success": True}
    return {"error": "TTS generation failed"}


def _overlay_tts_on_video(ffmpeg: str, video_path: str, tts_path: str, output_path: str, 
                           original_vol: float = 0.1, tts_vol: float = 1.0,
                           replace_voice: bool = True) -> dict:
    """Mix TTS narration with original video audio (blocking, for asyncio.to_thread).
    
    When replace_voice=True, applies a bandreject filter to suppress human voice 
    frequencies (300-3000Hz) from original audio, keeping only music/ambient sounds.
    This creates a natural voice replacement effect.
    """
    try:
        if replace_voice and original_vol > 0:
            # Suppress voice frequencies from original, keep ambient/music
            filter_complex = (
                f"[0:a]highpass=f=5000,volume={original_vol}[bg_high];"
                f"[0:a]lowpass=f=250,volume={original_vol}[bg_low];"
                f"[bg_high][bg_low]amix=inputs=2:duration=longest[bg];"
                f"[1:a]volume={tts_vol}[narr];"
                f"[bg][narr]amix=inputs=2:duration=first:dropout_transition=2[a]"
            )
        elif original_vol > 0:
            filter_complex = (
                f"[0:a]volume={original_vol}[bg];"
                f"[1:a]volume={tts_vol}[narr];"
                f"[bg][narr]amix=inputs=2:duration=first:dropout_transition=2[a]"
            )
        else:
            # Fully replace: only TTS audio, pad to video length
            filter_complex = (
                f"[1:a]volume={tts_vol}[narr];"
                f"[narr]apad[a]"
            )

        cmd = [
            ffmpeg, "-y",
            "-i", video_path,
            "-i", tts_path,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"error": f"FFmpeg overlay error: {result.stderr[-300:] if result.stderr else 'unknown'}"}
        return {"success": True}
    except Exception as e:
        return {"error": f"Overlay exception: {str(e)}"}


@router.get("/api/tts/voices")
async def list_tts_voices(locale: str = "vi"):
    """List available TTS voices, filtered by locale prefix."""
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
        filtered = [
            {"name": v["ShortName"], "gender": v["Gender"], "locale": v["Locale"]}
            for v in voices
            if v["Locale"].lower().startswith(locale.lower())
        ]
        if not filtered:
            # Fallback: return some common voices
            filtered = [
                v for v in [
                    {"name": v["ShortName"], "gender": v["Gender"], "locale": v["Locale"]}
                    for v in voices
                ]
                if any(loc in v["locale"].lower() for loc in ["vi", "en-us", "en-gb", "ja", "ko", "zh"])
            ]
        return JSONResponse({"voices": filtered})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/tts/narrate")
async def narrate_video(request: Request):
    """Generate TTS narration and overlay onto a merged video.
    
    Body:
        video_url: path to the merged video (e.g., /tmp/merged_xxx.mp4)
        narrations: list of {text: "...", scene_index: N}
        voice: TTS voice name (e.g., "vi-VN-HoaiMyNeural")
        original_volume: 0.0-1.0, volume of original audio (default 0.1)
        rate: TTS speech rate (e.g., "-10%", "+0%", "+10%")
        pitch: TTS pitch (e.g., "-5Hz", "+0Hz", "+5Hz")
        replace_voice: if true, suppress original voice frequencies (default true)
    """
    try:
        body = await request.json()
        video_url = body.get("video_url", "")
        narrations = body.get("narrations", [])
        voice = body.get("voice", "vi-VN-HoaiMyNeural")
        original_volume = float(body.get("original_volume", 0.1))
        rate = body.get("rate", "-5%")  # Slightly slower for natural Vietnamese
        pitch = body.get("pitch", "+0Hz")
        replace_voice = body.get("replace_voice", True)

        if not video_url or not narrations:
            return JSONResponse({"error": "Cần video_url và narrations"}, status_code=400)

        # Resolve video path
        base_dir = Path("D:/MyFile/tool/flow2api")
        if video_url.startswith("/tmp/"):
            video_path = str(base_dir / video_url.lstrip("/"))
        elif video_url.startswith("http"):
            from urllib.parse import urlparse
            video_path = str(base_dir / urlparse(video_url).path.lstrip("/"))
        else:
            video_path = video_url

        if not Path(video_path).exists():
            return JSONResponse({"error": f"Video không tồn tại: {video_url}"}, status_code=404)

        # Combine all narration texts with pauses
        full_text = ". ... ".join(n["text"].strip() for n in narrations if n.get("text", "").strip())
        if not full_text:
            return JSONResponse({"error": "Không có nội dung thuyết minh"}, status_code=400)

        # Generate TTS audio
        tts_path = str(Path(tempfile.mktemp(suffix=".mp3", prefix="tts_")))
        tts_result = await asyncio.to_thread(_run_tts_sync, full_text, voice, tts_path, rate, pitch)
        if "error" in tts_result:
            return JSONResponse({"error": tts_result["error"]}, status_code=500)

        # Overlay TTS on video
        ffmpeg = _get_ffmpeg_path()
        output_dir = Path("D:/MyFile/tool/flow2api/tmp")
        output_name = f"narrated_{int(time.time())}.mp4"
        output_path = str(output_dir / output_name)

        overlay_result = await asyncio.to_thread(
            _overlay_tts_on_video, ffmpeg, video_path, tts_path, output_path, 
            original_volume, 1.0, replace_voice
        )

        # Clean up TTS temp file
        try:
            Path(tts_path).unlink()
        except Exception:
            pass

        if "error" in overlay_result:
            return JSONResponse({"error": overlay_result["error"]}, status_code=500)

        output_file = Path(output_path)
        if output_file.exists() and output_file.stat().st_size > 0:
            size_mb = round(output_file.stat().st_size / 1024 / 1024, 1)
            return JSONResponse({
                "success": True,
                "url": f"/tmp/{output_name}",
                "size_mb": size_mb
            })

        return JSONResponse({"error": "Output file không tồn tại"}, status_code=500)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
