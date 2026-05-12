"""
This file is a lightweight wrapper for backward compatibility.
The actual implementation has been refactored and split into the `generation` package.
"""

from .generation.handler import GenerationHandler
from .generation.utils import MODEL_CONFIG, _make_t2v_config, _make_i2v_config, _apply_veo_3_1_model_updates, _known_video_model_keys, _resolve_tier_two_model_key

__all__ = [
    "MODEL_CONFIG",
    "GenerationHandler",
    "_make_t2v_config",
    "_make_i2v_config",
    "_apply_veo_3_1_model_updates",
    "_known_video_model_keys",
    "_resolve_tier_two_model_key"
]
