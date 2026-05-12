"""Video merge API endpoint for Story Creator."""
import asyncio
import os
import subprocess
import tempfile
import time
import urllib.request
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


def _resolve_video_paths(video_urls: list) -> list:
    """Resolve video URLs to local file paths."""
    local_paths = []
    base_dir = Path("D:/MyFile/tool/flow2api")

    for url in video_urls:
        # Strip http://localhost:PORT prefix
        if url.startswith("http://localhost") or url.startswith("http://127.0.0.1"):
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                url = parsed.path  # e.g., /tmp/hash.mp4
            except Exception:
                pass
        # Local /tmp/ path
        if url.startswith("/tmp/"):
            abs_path = base_dir / url.lstrip("/")
            if abs_path.exists():
                local_paths.append(str(abs_path).replace("\\", "/"))
                continue
        # Other local paths
        if url.startswith("/"):
            abs_path = base_dir / url.lstrip("/")
            if abs_path.exists():
                local_paths.append(str(abs_path).replace("\\", "/"))
                continue
        # Absolute path on disk
        if os.path.exists(url):
            local_paths.append(url.replace("\\", "/"))
            continue
        # Try download
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="flow_dl_"))
            dl_path = tmp_dir / f"dl_{len(local_paths)}.mp4"
            if url.startswith("http"):
                urllib.request.urlretrieve(url, str(dl_path))
            else:
                full_url = f"http://127.0.0.1:8000{url}"
                urllib.request.urlretrieve(full_url, str(dl_path))
            if dl_path.exists() and dl_path.stat().st_size > 0:
                local_paths.append(str(dl_path).replace("\\", "/"))
        except Exception:
            pass

    return local_paths


def _run_ffmpeg_concat(ffmpeg: str, local_paths: list, output_path: str) -> dict:
    """Run FFmpeg concat with re-encoding for seamless joins (meant for asyncio.to_thread).
    
    Always re-encodes to normalize fps, resolution and pixel format so there
    are no visible jumps at clip boundaries.
    """
    concat_file = Path(tempfile.mktemp(suffix=".txt", prefix="concat_"))
    try:
        with open(concat_file, "w", encoding="utf-8") as f:
            for p in local_paths:
                f.write(f"file '{p}'\n")

        # Always re-encode with normalized parameters to avoid stuttering
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-vf", "fps=24,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-vsync", "cfr",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            return {"error": f"FFmpeg lá»—i: {result.stderr[-300:] if result.stderr else 'unknown'}"}

        return {"success": True}

    except subprocess.TimeoutExpired:
        return {"error": "FFmpeg timeout â€” video quÃ¡ lá»›n hoáº·c codec khÃ´ng tÆ°Æ¡ng thÃ­ch"}
    except Exception as e:
        return {"error": f"FFmpeg exception: {str(e)}"}
    finally:
        try:
            concat_file.unlink()
        except Exception:
            pass


def _probe_video_duration(ffmpeg: str, video_path: str) -> float:
    """Probe video duration using ffmpeg (no ffprobe needed)."""
    import re
    try:
        cmd = [ffmpeg, "-i", video_path, "-f", "null", "-"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        # FFmpeg prints duration to stderr: "Duration: 00:00:08.34, ..."
        dur_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+)\.(\d+)', result.stderr)
        if dur_match:
            h, m, s, cs = dur_match.groups()
            return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100
    except Exception:
        pass
    return 8.0  # Default for VideoFX videos


def _normalize_inputs(ffmpeg: str, local_paths: list) -> list:
    """Pre-normalize all input clips to same fps/resolution/pixel format.
    
    Ensures every clip has both video AND audio streams (adds silent audio
    if missing) so that acrossfade works reliably.
    Returns list of normalized file paths (temp files).
    """
    normalized = []
    for i, p in enumerate(local_paths):
        norm_path = Path(tempfile.mktemp(suffix=f"_norm{i}.mp4", prefix="xf_"))
        # Try with audio first
        cmd = [
            ffmpeg, "-y",
            "-i", p,
            "-vf", "fps=24,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-vsync", "cfr",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            str(norm_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and norm_path.exists() and norm_path.stat().st_size > 0:
                normalized.append(str(norm_path).replace("\\", "/"))
                continue

            # If failed (likely no audio stream), retry with generated silent audio
            print(f"[NORMALIZE] Clip {i} has no audio, adding silent track...")
            cmd_silent = [
                ffmpeg, "-y",
                "-i", p,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", "fps=24,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-vsync", "cfr",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
                "-shortest",
                str(norm_path)
            ]
            result = subprocess.run(cmd_silent, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and norm_path.exists() and norm_path.stat().st_size > 0:
                normalized.append(str(norm_path).replace("\\", "/"))
            else:
                print(f"[NORMALIZE] Failed clip {i}: {result.stderr[-200:] if result.stderr else 'unknown'}")
                normalized.append(p)
        except Exception as e:
            print(f"[NORMALIZE] Exception clip {i}: {e}")
            normalized.append(p)
    return normalized


def _run_ffmpeg_crossfade(ffmpeg: str, local_paths: list, output_path: str, fade_duration: float = 1.0) -> dict:
    """Run FFmpeg with crossfade transitions between clips (meant for asyncio.to_thread).

    Uses xfade filter for smooth transitions between video clips.
    Pre-normalizes all inputs to same fps/resolution for seamless merging.
    Probes duration using ffmpeg directly (no ffprobe binary needed).
    """
    n = len(local_paths)
    if n < 2:
        return {"error": "Cáº§n Ã­t nháº¥t 2 video"}

    # Pre-normalize all clips to same specs
    norm_paths = _normalize_inputs(ffmpeg, local_paths)
    temp_files = [Path(p) for p in norm_paths if p not in local_paths]

    try:
        # Probe each normalized video duration
        durations = [_probe_video_duration(ffmpeg, p) for p in norm_paths]

        # For only 2 videos, simple xfade
        if n == 2:
            offset = max(0, durations[0] - fade_duration)
            cmd = [
                ffmpeg, "-y",
                "-i", norm_paths[0],
                "-i", norm_paths[1],
                "-filter_complex",
                f"[0:v][1:v]xfade=transition=fade:duration={fade_duration}:offset={offset}[v];"
                f"[0:a][1:a]acrossfade=d={fade_duration}:c1=tri:c2=tri[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path)
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    print(f"[CROSSFADE] xfade failed for 2 videos, stderr: {result.stderr[-300:]}")
                    return _run_ffmpeg_concat(ffmpeg, local_paths, output_path)
                return {"success": True}
            except subprocess.TimeoutExpired:
                return _run_ffmpeg_concat(ffmpeg, local_paths, output_path)
            except Exception as e:
                print(f"[CROSSFADE] Exception: {e}")
                return _run_ffmpeg_concat(ffmpeg, local_paths, output_path)

        # For 3+ videos, chain xfade filters
        try:
            inputs = []
            for p in norm_paths:
                inputs.extend(["-i", p])

            filter_parts = []
            running_duration = durations[0]

            for i in range(n - 1):
                if i == 0:
                    src_a = "[0:v]"
                    src_b = "[1:v]"
                else:
                    src_a = f"[xf{i-1}]"
                    src_b = f"[{i+1}:v]"

                offset = max(0, running_duration - fade_duration)
                out_label = f"[xf{i}]" if i < n - 2 else "[v]"
                filter_parts.append(
                    f"{src_a}{src_b}xfade=transition=fade:duration={fade_duration}:offset={offset}{out_label}"
                )
                running_duration = offset + durations[i + 1]

            # Build audio crossfade chain alongside video
            audio_parts = []
            for i in range(n - 1):
                if i == 0:
                    a_src_a = "[0:a]"
                    a_src_b = "[1:a]"
                else:
                    a_src_a = f"[af{i-1}]"
                    a_src_b = f"[{i+1}:a]"
                a_out_label = f"[af{i}]" if i < n - 2 else "[a]"
                audio_parts.append(
                    f"{a_src_a}{a_src_b}acrossfade=d={fade_duration}:c1=tri:c2=tri{a_out_label}"
                )

            filter_complex = ";".join(filter_parts + audio_parts)

            cmd = [ffmpeg, "-y"] + inputs + [
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                print(f"[CROSSFADE] xfade chain failed for {n} videos, stderr: {result.stderr[-400:]}")
                return _run_ffmpeg_concat(ffmpeg, local_paths, output_path)
            return {"success": True}

        except subprocess.TimeoutExpired:
            return _run_ffmpeg_concat(ffmpeg, local_paths, output_path)
        except Exception as e:
            print(f"[CROSSFADE] Chain exception: {e}")
            return _run_ffmpeg_concat(ffmpeg, local_paths, output_path)

    finally:
        # Clean up normalized temp files
        for tf in temp_files:
            try:
                tf.unlink()
            except Exception:
                pass


@router.post("/api/merge-videos")
async def merge_videos(request: Request):
    """Download video files and merge them into one MP4.
    
    Uses asyncio.to_thread to avoid blocking the event loop.
    """
    try:
        body = await request.json()
        video_urls = body.get("video_urls", [])
        use_crossfade = body.get("crossfade", False)

        if not video_urls or len(video_urls) < 2:
            return JSONResponse({"error": "Cáº§n Ã­t nháº¥t 2 video Ä‘á»ƒ ghÃ©p"}, status_code=400)

        ffmpeg = _get_ffmpeg_path()

        # Output path
        output_dir = Path("D:/MyFile/tool/flow2api/tmp")
        output_dir.mkdir(exist_ok=True)
        output_name = f"merged_{int(time.time())}.mp4"
        output_path = str(output_dir / output_name)

        # Resolve video paths (blocking IO, run in thread)
        local_paths = await asyncio.to_thread(_resolve_video_paths, video_urls)

        if len(local_paths) < 2:
            return JSONResponse(
                {"error": f"Chá»‰ tÃ¬m Ä‘Æ°á»£c {len(local_paths)} video (cáº§n â‰¥2)"},
                status_code=400
            )

        # Run FFmpeg in a separate thread to not block the event loop
        if use_crossfade:
            result = await asyncio.to_thread(
                _run_ffmpeg_crossfade, ffmpeg, local_paths, output_path
            )
        else:
            result = await asyncio.to_thread(
                _run_ffmpeg_concat, ffmpeg, local_paths, output_path
            )

        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=500)

        output_file = Path(output_path)
        if output_file.exists() and output_file.stat().st_size > 0:
            size_mb = round(output_file.stat().st_size / 1024 / 1024, 1)
            return JSONResponse({
                "success": True,
                "url": f"/tmp/{output_name}",
                "size_mb": size_mb,
                "videos_merged": len(local_paths)
            })

        return JSONResponse({"error": "File output khÃ´ng tá»“n táº¡i"}, status_code=500)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/download")
async def download_file(file: str, name: str = "video.mp4"):
    """Force-download a file from /tmp/ with proper Content-Disposition header.
    
    Args:
        file: Path like /tmp/merged_xxx.mp4, /tmp/UUID, or http://localhost:8000/tmp/UUID
        name: Desired download filename (e.g., merged_video.mp4, scene_01.mp4)
    """
    from fastapi.responses import FileResponse as FR
    from urllib.parse import urlparse

    # Handle external proxying
    original_url = file
    if "://" in file:
        parsed = urlparse(file)
        if parsed.netloc not in ("localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"):
            # It's an external URL, proxy it if not found locally
            pass
        file = parsed.path  # e.g., /tmp/UUID

    base_dir = Path("D:/MyFile/tool/flow2api/tmp")
    abs_path = None

    # Try to resolve file path
    if file.startswith("/tmp/"):
        # Direct /tmp/ path
        filename = file[5:]
        candidate_path = base_dir / filename
        if candidate_path.exists():
            abs_path = candidate_path
    
    if not abs_path:
        # Non-/tmp/ path (e.g. /video/UUID from Google) — try to find by basename in tmp/
        import os
        import hashlib
        basename = os.path.basename(file)
        if basename:
            candidate_path = base_dir / basename
            if candidate_path.exists():
                abs_path = candidate_path
            elif (base_dir / f"{basename}.mp4").exists():
                abs_path = base_dir / f"{basename}.mp4"
        
        if not abs_path:
            # Try MD5 hash lookup (file_cache uses MD5 of URL as filename)
            url_hash = hashlib.md5(original_url.encode()).hexdigest()
            for ext in [".mp4", ".webm", ".mov", ""]:
                candidate = base_dir / f"{url_hash}{ext}"
                if candidate.exists():
                    abs_path = candidate
                    break
    
    if not abs_path and "://" in original_url:
        parsed = urlparse(original_url)
        if parsed.netloc not in ("localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"):
            # SSRF Protection: Restrict external downloads to Google domains only
            allowed_domains = ("googleusercontent.com", "googlevideo.com", "googleapis.com", "google.com")
            if not any(parsed.netloc == d or parsed.netloc.endswith("." + d) for d in allowed_domains):
                return JSONResponse({"error": "Forbidden: External domain not allowed"}, status_code=403)

            # Download external URL directly
            from curl_cffi.requests import AsyncSession
            from fastapi.responses import Response
            import mimetypes
            
            media_type, _ = mimetypes.guess_type(name)
            if not media_type:
                media_type = "image/jpeg" if "image" in name.lower() else "application/octet-stream"
                
            headers = {"Content-Disposition": f'attachment; filename="{name}"'}
            
            try:
                async with AsyncSession(impersonate="chrome120") as client:
                    response = await client.get(original_url, timeout=30)
                    if response.status_code == 200:
                        return Response(content=response.content, media_type=media_type, headers=headers)
                    else:
                        return JSONResponse({"error": f"Lá»—i HTTP {response.status_code}"}, status_code=500)
            except Exception as e:
                import logging
                logging.error(f"Download external file failed: {e}")
                return JSONResponse({"error": f"Táº£i tháº¥t báº¡i: {str(e)}"}, status_code=500)

    if not abs_path:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    # Security: prevent path traversal
    try:
        abs_path = abs_path.resolve()
        if not str(abs_path).startswith(str(base_dir.resolve())):
            return JSONResponse({"error": "Invalid path"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    if not abs_path.exists():
        return JSONResponse({"error": f"File khÃ´ng tá»“n táº¡i: {file}"}, status_code=404)

    import mimetypes
    media_type, _ = mimetypes.guess_type(name)
    if not media_type:
        media_type = "video/mp4" if "video" in name.lower() else "application/octet-stream"

    return FR(
        path=str(abs_path),
        filename=name,
        media_type=media_type,
    )

