import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Optional, AsyncGenerator, List, Dict, Any
from src.core.logger import debug_logger
from src.core.config import config
from src.core.monitoring import record_generation_result
from src.core.models import Task, RequestLog
from src.core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from src.services.file_cache import FileCache

from .mixins.base import GenerationBaseMixin
from .mixins.helpers import GenerationHelpersMixin
from .mixins.response import GenerationResponseMixin
from .mixins.video import GenerationVideoMixin
from .mixins.image import GenerationImageMixin
from .mixins.core import GenerationCoreMixin

class GenerationHandler(
    GenerationBaseMixin,
    GenerationHelpersMixin,
    GenerationResponseMixin,
    GenerationVideoMixin,
    GenerationImageMixin,
    GenerationCoreMixin
):
    """统一生成处理器"""

    def __init__(self, flow_client, token_manager, load_balancer, db, concurrency_manager, proxy_manager):
        cache_dir = Path(__file__).resolve().parents[2] / "tmp"
        self.flow_client = flow_client
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.db = db
        self.concurrency_manager = concurrency_manager
        self.file_cache = FileCache(
            cache_dir=str(cache_dir),
            default_timeout=config.cache_timeout,
            proxy_manager=proxy_manager,
            flow_client=flow_client,
        )
