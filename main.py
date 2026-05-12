"""Flow2API - Main Entry Point"""
from src.main import app
import uvicorn

if __name__ == "__main__":
    from src.core.config import config
    try:
        from src.core.updater import cleanup_old_exe
        cleanup_old_exe()
    except Exception:
        pass

    uvicorn.run(
        "src.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=config.debug_enabled
    )
