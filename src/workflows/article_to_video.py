import asyncio
import json
import os
import uuid
import httpx
from pathlib import Path
from typing import AsyncGenerator



from src.core.database import Database
from src.services.generation_handler import GenerationHandler
from src.api.routes import _ensure_generation_handler
from src.api.dubbing import _get_ffmpeg_path

SYSTEM_PROMPT = """Bạn là một đạo diễn và biên kịch xuất sắc. Nhiệm vụ của bạn là chuyển đổi bài báo thành một kịch bản video ngắn.
Bài báo thường không có ảnh nhân vật rõ ràng, nên bạn PHẢI tự sáng tạo ra thiết kế ngoại hình của các nhân vật chính.
TUYỆT ĐỐI KHÔNG sử dụng tên thật của người nổi tiếng, chính trị gia, hoặc nhân vật công chúng có thật trong video_prompt và visual_prompt. Hãy thay thế bằng các danh từ chung chung như "một người đàn ông", "một người phụ nữ", "vị quan chức", "cô gái" để tránh vi phạm chính sách bản quyền hình ảnh (Prominent People Filter).

TẤT CẢ các trường mô tả (visual_prompt, video_prompt) đều PHẢI viết hoàn toàn bằng TIẾNG VIỆT.
QUAN TRỌNG: Trong video_prompt, nếu có lời thoại hoặc giọng nói, bạn PHẢI ghi rõ "nhân vật nói bằng tiếng Việt" hoặc "giọng thuyết minh tiếng Việt". Luôn thêm cụm từ "nói tiếng Việt" hoặc "giọng Việt Nam" vào mỗi video_prompt để đảm bảo video tạo ra có âm thanh tiếng Việt.
TUYỆT ĐỐI KHÔNG yêu cầu hiển thị chữ tiếng Việt CÓ DẤU trên màn hình video vì model tạo video không thể render dấu tiếng Việt chính xác. Nếu cần hiển thị tên địa danh, tỉnh thành, tiêu đề trên video thì PHẢI viết KHÔNG DẤU (ví dụ: "Son La" thay vì "Sơn La", "Ha Noi" thay vì "Hà Nội", "Dong dat tai Son La" thay vì "Động đất tại Sơn La").
VỀ ĐỊA LÝ VIỆT NAM: Model tạo video KHÔNG biết địa lý Việt Nam. KHÔNG BAO GIỜ chỉ ghi tên địa danh mà phải MÔ TẢ CHI TIẾT đặc điểm cảnh quan thực tế. Ví dụ:
- Sơn La/Tây Bắc: "vùng núi cao phía bắc Việt Nam, ruộng bậc thang xanh mướt, nhà sàn gỗ dân tộc Thái, sương mù bao phủ thung lũng"
- Hà Nội: "thành phố cổ kính Đông Nam Á, hồ nước xanh giữa trung tâm, xe máy đông đúc, phố cổ với nhà ống hẹp"
- TP.HCM: "thành phố hiện đại Đông Nam Á, tòa nhà cao tầng, đường phố rộng đông đúc xe máy, nắng nhiệt đới"
- Nông thôn Việt Nam: "cánh đồng lúa xanh bát ngát, nón lá, trâu cày, làng quê yên bình"
Hãy luôn thêm các chi tiết đặc trưng Việt Nam (xe máy, nón lá, ruộng lúa, nhà sàn, phở, áo dài...) để video có bối cảnh đúng.

TRẢ VỀ ĐÚNG ĐỊNH DẠNG JSON (Không chứa Markdown ```json):
{
    "characters": [
        {
            "id": "char_1",
            "name": "Tên nhân vật",
            "visual_prompt": "Mô tả siêu chi tiết ngoại hình bằng tiếng Việt (ví dụ: Một người đàn ông châu Á 30 tuổi, mặc vest xanh, tóc ngắn đen, khuôn mặt chi tiết, ánh sáng điện ảnh, 4k, siêu thực)"
        }
    ],
    "scenes": [
        {
            "scene_number": 1,
            "character_ids": ["char_1"],
            "video_prompt": "Mô tả cảnh quay bằng tiếng Việt, nhân vật nói tiếng Việt (ví dụ: Người đàn ông mặc vest xanh đang nói tiếng Việt với đồng nghiệp trong văn phòng hiện đại, giọng Việt Nam rõ ràng, góc quay tracking điện ảnh.)"
        }
    ]
}"""

def _sse_msg(event_type: str, data: any) -> str:
    """Format message as Server-Sent Event"""
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"

async def extract_article(url: str) -> str:
    import trafilatura
    import httpx
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers, timeout=30.0)
            if resp.status_code != 200:
                raise Exception(f"Lỗi tải trang: HTTP {resp.status_code}")
            downloaded = resp.text
        except Exception as e:
            raise Exception(f"Không thể tải nội dung bài báo: {e}")
            
    if not downloaded:
        raise Exception("Không thể tải nội dung bài báo.")
    text = trafilatura.extract(downloaded)
    if not text:
        raise Exception("Không thể trích xuất văn bản từ bài báo.")
    return text

async def generate_script(text: str, gemini_api_key: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"NỘI DUNG BÀI BÁO:\n{text}"}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(url, json=payload, timeout=60.0)
        if resp.status_code != 200:
            raise Exception(f"Lỗi API Gemini: {resp.text}")
        
        data = resp.json()
        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            script_json = json.loads(content)
            return script_json
        except Exception as e:
            raise Exception(f"Không thể parse JSON từ LLM: {e}")

async def call_generation(handler: GenerationHandler, model: str, prompt: str, images: list = None, user_id: int = None) -> str:
    result_json = ""
    async for chunk in handler.handle_generation(model=model, prompt=prompt, images=images, stream=False, user_id=user_id):
        result_json += chunk
    
    try:
        data = json.loads(result_json)
        return data["choices"][0]["message"]["content"]
    except:
        return result_json

def extract_url(text: str) -> str:
    import re
    # Try finding markdown image/video URL format first: (http...)
    match = re.search(r'\((http[s]?://[^\)]+)\)', text)
    if match:
        return match.group(1)
    # Fallback to finding any bare http/https URL in the text
    match = re.search(r'(http[s]?://[^\s\"\']+)', text)
    if match:
        return match.group(1)
    
    raise ValueError(f"Không tìm thấy URL tải xuống hợp lệ trong phản hồi. Raw: {text[:100]}")

async def download_file(url: str, output_path: str):
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        async with client.stream('GET', url) as response:
            with open(output_path, 'wb') as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)

async def stream_article_to_video(url: str, gemini_api_key: str, output_dir: str, user_id: int = None) -> AsyncGenerator[str, None]:
    """Hàm chạy workflow Article-to-Video trả về dữ liệu stream SSE cho Frontend"""
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # 1. Trích xuất văn bản
        yield _sse_msg("log", f"Đang tải bài báo từ: {url}...")
        article_text = await extract_article(url)
        yield _sse_msg("log", f"Đã trích xuất {len(article_text)} ký tự văn bản.")
        
        # 2. Tạo Kịch Bản
        yield _sse_msg("log", "Đang phân tích bài báo và viết kịch bản bằng Gemini AI...")
        
        import random
        api_keys = [k.strip() for k in gemini_api_key.split(",") if k.strip()]
        if not api_keys:
            raise Exception("Không tìm thấy Gemini API Key hợp lệ.")
            
        script = None
        max_script_retries = 3
        for attempt in range(1, max_script_retries + 1):
            try:
                current_key = random.choice(api_keys)
                script = await generate_script(article_text, current_key)
                break
            except Exception as e:
                if "503" in str(e) or "429" in str(e) or attempt < max_script_retries:
                    if attempt < max_script_retries:
                        yield _sse_msg("log", f"Lỗi Gemini API (Lần {attempt}/{max_script_retries}): Máy chủ Google đang quá tải, thử lại sau 5s...")
                        await asyncio.sleep(5)
                    else:
                        raise e
                else:
                    raise e
        
        with open(os.path.join(output_dir, "script.json"), "w", encoding="utf-8") as f:
            json.dump(script, f, ensure_ascii=False, indent=2)
            
        yield _sse_msg("log", f"Đã tạo kịch bản thành công với {len(script.get('scenes', []))} phân cảnh.")
        yield _sse_msg("script", script)
        yield _sse_msg("log", "Đang tiến hành tạo Video và Hình ảnh song song...")
        yield _sse_msg("progress", 50)

        handler = _ensure_generation_handler()
        
        # 3. Tạo Ảnh Gốc Nhân Vật
        character_images = {}
        for char in script.get("characters", []):
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    yield _sse_msg("log", f"Đang tạo hình ảnh cho nhân vật: {char['name']}... (Lần {attempt}/{max_retries})")
                    result_text = await call_generation(handler, model="gemini-3.0-pro-image-landscape", prompt=char["visual_prompt"], user_id=user_id)
                    
                    # Check if result is an error response
                    if '"error"' in result_text:
                        raise Exception(f"API trả lỗi: {result_text[:200]}")
                    
                    image_url = extract_url(result_text)
                    
                    img_path = os.path.join(output_dir, f"{char['id']}.jpg")
                    await download_file(image_url, img_path)
                    with open(img_path, "rb") as f:
                        character_images[char["id"]] = f.read()
                        
                    yield _sse_msg("log", f"Đã tạo thành công hình ảnh cho {char['name']}.")
                    # Send local url instead of signed url to avoid CORS/loading issues in browser
                    yield _sse_msg("image", {"id": char["id"], "url": f"/tmp/output_article/{char['id']}.jpg"})
                    break  # Thành công, thoát retry
                    
                except Exception as e:
                    yield _sse_msg("log", f"Lỗi tạo ảnh {char['name']} (Lần {attempt}): {str(e)[:150]}")
                    if attempt < max_retries:
                        wait_secs = attempt * 10  # 10s, 20s, 30s
                        yield _sse_msg("log", f"Chờ {wait_secs}s trước khi thử lại...")
                        await asyncio.sleep(wait_secs)
                    else:
                        yield _sse_msg("log", f"Không thể tạo ảnh cho {char['name']} sau {max_retries} lần. Bỏ qua nhân vật này.")
                
        # 4. Sinh Video cho từng cảnh
        scene_videos = []
        for scene in script.get("scenes", []):
            scene_idx = scene["scene_number"]
            
            ref_image = None
            for cid in scene.get("character_ids", []):
                if cid in character_images:
                    ref_image = character_images[cid]
                    break
                    
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    if ref_image:
                        yield _sse_msg("log", f"Đang tiến hành tạo Video cho Cảnh {scene_idx} (Image-to-Video) - Lần {attempt}/{max_retries}...")
                        # I2V Generation (Fast mode)
                        result_text = await call_generation(
                            handler, 
                            model="veo_3_1_i2v_s_fast_fl", 
                            prompt=scene["video_prompt"], 
                            images=[ref_image],
                            user_id=user_id
                        )
                    else:
                        yield _sse_msg("log", f"Đang tiến hành tạo Video cho Cảnh {scene_idx} (Text-to-Video do không có ảnh) - Lần {attempt}/{max_retries}...")
                        # T2V Generation (Fast mode)
                        result_text = await call_generation(
                            handler, 
                            model="veo_3_1_t2v_fast_landscape", 
                            prompt=scene["video_prompt"], 
                            images=None,
                            user_id=user_id
                        )
                        
                    video_url = extract_url(result_text)
                    yield _sse_msg("log", f"Đã sinh xong Video Cảnh {scene_idx}. Đang tải về...")
                    
                    video_path = os.path.join(output_dir, f"scene_{scene_idx}.mp4")
                    await download_file(video_url, video_path)
                    
                    scene_videos.append({
                        "scene": scene_idx,
                        "video": video_path,
                        "url": f"/tmp/output_article/scene_{scene_idx}.mp4"
                    })
                    yield _sse_msg("log", f"Hoàn thành Cảnh {scene_idx}.")
                    yield _sse_msg("scene_preview", {"scene": scene_idx, "video_url": f"/tmp/output_article/scene_{scene_idx}.mp4"})
                    
                    break # Thoát khỏi vòng lặp retry nếu thành công
                    
                except Exception as e:
                    yield _sse_msg("log", f"Lỗi ở Cảnh {scene_idx} (Lần {attempt}): {e}")
                    if attempt == max_retries:
                        yield _sse_msg("log", f"Đã thử {max_retries} lần nhưng vẫn thất bại Cảnh {scene_idx}. Bỏ qua cảnh này.")
                
        # 6. Merge bằng FFmpeg
        if not scene_videos:
            yield _sse_msg("log", "Quá trình thất bại: Không có cảnh nào được tạo thành công.")
            yield _sse_msg("done", {"success": False})
            return
            
        yield _sse_msg("log", "Đang tự động ghép các phân cảnh thành Video hoàn chỉnh...")
        ffmpeg = _get_ffmpeg_path()
        
        concat_file = os.path.join(output_dir, "concat.txt")
        with open(concat_file, "w", encoding="utf-8") as f:
            for sv in scene_videos:
                # Use absolute path with forward slashes for FFmpeg compatibility on Windows
                abs_path = os.path.abspath(sv['video']).replace('\\', '/')
                f.write(f"file '{abs_path}'\n")
                
        final_filename = f"final_article_video_{uuid.uuid4().hex[:8]}.mp4"
        final_output = os.path.join(output_dir, final_filename)
        
        import subprocess
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", final_output],
            capture_output=True, text=True, timeout=120
        )
        
        if result.returncode != 0:
            yield _sse_msg("log", f"Lỗi FFmpeg merge: {result.stderr[-300:] if result.stderr else 'unknown'}")
            yield _sse_msg("done", {"success": False, "error": "FFmpeg merge failed"})
            return
        
        # Auto-copy to Downloads folder
        import shutil
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        downloads_copy = os.path.join(downloads_dir, final_filename)
        try:
            shutil.copy2(final_output, downloads_copy)
            yield _sse_msg("log", f"📁 Đã lưu video vào thư mục Downloads: {final_filename}")
        except Exception as copy_err:
            yield _sse_msg("log", f"⚠ Không thể copy vào Downloads: {copy_err}")
        
        yield _sse_msg("log", f"🎉 HOÀN TẤT! Video cuối cùng đã được lưu thành công.")
        yield _sse_msg("done", {"success": True, "final_video": final_filename})

    except Exception as e:
        yield _sse_msg("log", f"LỖI NGHIÊM TRỌNG: {str(e)}")
        yield _sse_msg("done", {"success": False, "error": str(e)})

