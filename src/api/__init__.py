"""API modules"""

from .routes import router as api_router
from .admin import router as admin_router
from .merge import router as merge_router
from .translate import router as translate_router
from .dubbing import router as dubbing_router
from .tts import router as tts_router

__all__ = ["api_router", "admin_router", "merge_router", "translate_router", "dubbing_router", "tts_router"]
