import os
import sys
import time
import httpx
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_OWNER = "thieuhoangviet"
REPO_NAME = "flow"
GITHUB_API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"

def is_frozen():
    """Check if the application is running as a compiled PyInstaller executable."""
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

def get_current_version() -> str:
    """Read the version from version.txt bundled with the executable."""
    if not is_frozen():
        return "development"
    
    try:
        # PyInstaller extracts to _MEIPASS
        base_path = Path(sys._MEIPASS)
        version_file = base_path / "version.txt"
        if version_file.exists():
            return version_file.read_text().strip()
    except Exception as e:
        logger.error(f"Failed to read version.txt: {e}")
    
    return "unknown"

def cleanup_old_exe():
    """Delete the _old.exe if it exists from a previous update."""
    if not is_frozen():
        return
        
    exe_path = Path(sys.executable)
    old_exe_path = exe_path.with_name(f"{exe_path.stem}_old{exe_path.suffix}")
    
    if old_exe_path.exists():
        try:
            # Give the old process a moment to fully exit if we just restarted
            time.sleep(1)
            old_exe_path.unlink()
            logger.info(f"Cleaned up old executable: {old_exe_path}")
        except Exception as e:
            logger.warning(f"Could not delete old executable {old_exe_path} (might still be running): {e}")

async def check_for_updates() -> dict:
    """Check GitHub releases for a newer version."""
    if not is_frozen():
        return {"update_available": False, "message": "Chạy dưới dạng mã nguồn (development). Tính năng auto-update chỉ khả dụng ở bản EXE."}
        
    current_version = get_current_version()
    if current_version == "unknown":
        return {"update_available": False, "message": "Không thể xác định phiên bản hiện tại."}
        
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GITHUB_API_URL)
            if response.status_code == 404:
                return {"update_available": False, "message": "Chưa có bản release nào trên GitHub."}
            response.raise_for_status()
            release_data = response.json()
            
            latest_version = release_data.get("tag_name", "")
            
            if not latest_version:
                return {"update_available": False, "message": "Release không có tag_name hợp lệ."}
                
            if latest_version == current_version:
                return {"update_available": False, "message": f"Bạn đang dùng bản mới nhất ({current_version}).", "latest_version": latest_version}
            
            # Find the .exe asset
            assets = release_data.get("assets", [])
            exe_url = None
            for asset in assets:
                if asset.get("name", "").endswith(".exe"):
                    exe_url = asset.get("browser_download_url")
                    break
            
            if not exe_url:
                return {"update_available": False, "message": f"Phiên bản {latest_version} có sẵn nhưng không có file .exe để tải về."}
                
            return {
                "update_available": True,
                "message": f"Có phiên bản mới {latest_version} (Hiện tại: {current_version}).",
                "latest_version": latest_version,
                "download_url": exe_url,
                "release_notes": release_data.get("body", "")
            }
            
    except Exception as e:
        logger.error(f"Lỗi kiểm tra cập nhật: {e}")
        return {"update_available": False, "message": f"Lỗi kết nối GitHub: {e}"}

async def perform_update(download_url: str):
    """Download the new executable, rename the old one, and restart."""
    if not is_frozen():
        raise RuntimeError("Chỉ hỗ trợ cập nhật tự động khi chạy bằng file .exe")
        
    exe_path = Path(sys.executable)
    old_exe_path = exe_path.with_name(f"{exe_path.stem}_old{exe_path.suffix}")
    new_exe_path = exe_path.with_name(f"{exe_path.stem}_new{exe_path.suffix}")
    
    logger.info(f"Bắt đầu tải bản cập nhật từ: {download_url}")
    try:
        # 1. Download to a temporary file
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            async with client.stream("GET", download_url) as response:
                response.raise_for_status()
                with open(new_exe_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
        
        logger.info("Tải xuống thành công. Đang tiến hành cài đặt...")
        
        # 2. Rename current running .exe to _old.exe
        if old_exe_path.exists():
            old_exe_path.unlink()  # Remove if leftover from ancient update
            
        os.rename(exe_path, old_exe_path)
        
        # 3. Rename new_exe_path to the original name
        os.rename(new_exe_path, exe_path)
        
        logger.info("Cập nhật hoàn tất. Đang khởi động lại ứng dụng...")
        
        # 4. Restart the process
        # execv replaces the current process. On Windows it behaves slightly differently but usually works.
        # Alternatively, we can use subprocess.Popen and sys.exit()
        import subprocess
        subprocess.Popen([str(exe_path)] + sys.argv[1:], creationflags=subprocess.CREATE_NEW_CONSOLE)
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Lỗi trong quá trình cập nhật: {e}")
        # Cleanup temp file if failed
        if new_exe_path.exists():
            try:
                new_exe_path.unlink()
            except:
                pass
        raise
