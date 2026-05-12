import json
from typing import Dict, Any, Optional, List
from src.core.config import config

def _make_t2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "t2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": False,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg

def _make_i2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    min_images: int = 1,
    max_images: int = 2,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "i2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": True,
        "min_images": min_images,
        "max_images": max_images,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg

def _apply_veo_3_1_model_updates():
    """Keep the public aliases aligned with the current Veo 3.1 model families."""
    landscape = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    portrait = "VIDEO_ASPECT_RATIO_PORTRAIT"

    def add_alias(alias: str, target: str):
        MODEL_CONFIG[alias] = dict(MODEL_CONFIG[target])

    # Non-fast/non-lite Veo 3.1 aliases must call Quality upstream keys.
    MODEL_CONFIG["veo_3_1_t2v_landscape"].update({"model_key": "veo_3_1_t2v"})
    MODEL_CONFIG["veo_3_1_t2v_portrait"].update({"model_key": "veo_3_1_t2v_portrait"})
    MODEL_CONFIG["veo_3_1_i2v_s_landscape"].update({"model_key": "veo_3_1_i2v_s_fl"})
    MODEL_CONFIG["veo_3_1_i2v_s_portrait"].update({"model_key": "veo_3_1_i2v_s_portrait_fl"})
    MODEL_CONFIG["veo_3_1_extend"].update({"model_key": "veo_3_1_extend_landscape"})
    MODEL_CONFIG["veo_3_1_extend_portrait"].update({"model_key": "veo_3_1_extend_portrait"})

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # T2V duration variants.
        MODEL_CONFIG[f"veo_3_1_t2v_fast_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_fast_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", portrait
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_landscape"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            landscape,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_portrait"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            portrait,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", portrait
        )

        # I2V duration variants. FL keys are used for 2 images; the single-image path strips "_fl".
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_portrait_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", portrait
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            landscape,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            portrait,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            landscape,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            portrait,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait
        )

        for resolution_name, resolution, upsampler_model_key in (
            ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
            ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ):
            upsample = {"resolution": resolution, "model_key": upsampler_model_key}
            MODEL_CONFIG[f"veo_3_1_t2v_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", portrait, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait, upsample=upsample
            )

    for resolution_name, resolution, upsampler_model_key in (
        ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
    ):
        upsample = {"resolution": resolution, "model_key": upsampler_model_key}
        MODEL_CONFIG[f"veo_3_1_t2v_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v_portrait", portrait, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_fl", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_portrait_fl", portrait, upsample=upsample
        )

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # Explicit landscape names for /v1/models; short landscape names remain compatible.
        add_alias(f"veo_3_1_t2v_fast_landscape_{suffix}", f"veo_3_1_t2v_fast_{suffix}")
        add_alias(f"veo_3_1_t2v_landscape_{suffix}", f"veo_3_1_t2v_{suffix}")
        add_alias(f"veo_3_1_i2v_s_fast_landscape_{suffix}_fl", f"veo_3_1_i2v_s_fast_{suffix}_fl")
        add_alias(f"veo_3_1_i2v_s_landscape_{suffix}", f"veo_3_1_i2v_s_{suffix}")

        add_alias(f"veo_3_1_t2v_lite_landscape_{suffix}", f"veo_3_1_t2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_t2v_lite_portrait_{suffix}", f"veo_3_1_t2v_lite_{suffix}_portrait")
        add_alias(f"veo_3_1_i2v_lite_landscape_{suffix}", f"veo_3_1_i2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_i2v_lite_portrait_{suffix}", f"veo_3_1_i2v_lite_{suffix}_portrait")
        add_alias(
            f"veo_3_1_interpolation_lite_landscape_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_landscape",
        )
        add_alias(
            f"veo_3_1_interpolation_lite_portrait_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_portrait",
        )

        for resolution_name in ("4k", "1080p"):
            add_alias(
                f"veo_3_1_t2v_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_t2v_{suffix}_{resolution_name}",
            )
            add_alias(
                f"veo_3_1_i2v_s_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_i2v_s_{suffix}_{resolution_name}",
            )

    for resolution_name in ("4k", "1080p"):
        add_alias(f"veo_3_1_t2v_landscape_{resolution_name}", f"veo_3_1_t2v_{resolution_name}")
        add_alias(f"veo_3_1_i2v_s_landscape_{resolution_name}", f"veo_3_1_i2v_s_{resolution_name}")

    add_alias("veo_3_1_r2v_fast_landscape", "veo_3_1_r2v_fast")
    add_alias("veo_3_1_r2v_fast_landscape_ultra", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_relaxed", "veo_3_1_r2v_fast_ultra_relaxed")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_4k", "veo_3_1_r2v_fast_ultra_4k")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_1080p", "veo_3_1_r2v_fast_ultra_1080p")

def _known_video_model_keys() -> set[str]:
    return {
        cfg["model_key"]
        for cfg in MODEL_CONFIG.values()
        if cfg.get("type") == "video" and cfg.get("model_key")
    }

def _resolve_tier_two_model_key(model_key: str) -> str:
    """Only upgrade to an ultra key when that exact upstream key is known valid."""
    if "ultra" in model_key:
        return model_key
    if "_fl" in model_key:
        candidate = model_key.replace("_fl", "_ultra_fl")
    else:
        candidate = model_key + "_ultra"
    return candidate if candidate in _known_video_model_keys() else model_key


from typing import Dict, Any
MODEL_CONFIG = {
    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro)
    "gemini-3.0-pro-image-landscape": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.0-pro-image-portrait": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.0-pro-image-square": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.0-pro-image-four-three": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.0-pro-image-three-four": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },

    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro) 2K 放大版
    "gemini-3.0-pro-image-landscape-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-portrait-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-square-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-four-three-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-three-four-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },

    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro) 4K 放大版
    "gemini-3.0-pro-image-landscape-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-portrait-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-square-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-four-three-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-three-four-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # 图片生成 - IMAGEN_3_5 (Imagen 4.0)
    "imagen-4.0-generate-preview-landscape": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "imagen-4.0-generate-preview-portrait": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # 图片生成 - NARWHAL (新版)
    "gemini-3.1-flash-image-landscape": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.1-flash-image-portrait": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.1-flash-image-square": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.1-flash-image-four-three": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.1-flash-image-three-four": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },
    "gemini-3.1-flash-image-landscape-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-portrait-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-square-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-four-three-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-three-four-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-landscape-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-portrait-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-square-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-four-three-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-three-four-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # ========== 文生视频 (T2V - Text to Video) ==========
    # 不支持上传图片，只使用文本提示词生成

    # veo_3_1_t2v_fast_portrait (竖屏)
    # 上游模型名: veo_3_1_t2v_fast_portrait
    "veo_3_1_t2v_fast_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    # veo_3_1_t2v_fast_landscape (横屏)
    # 上游模型名: veo_3_1_t2v_fast
    "veo_3_1_t2v_fast_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v_fast_ultra (横竖屏)
    "veo_3_1_t2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_fast_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v_fast_ultra_relaxed (横竖屏)
    "veo_3_1_t2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v (横竖屏)
    "veo_3_1_t2v_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },
    # veo_3_1_t2v_lite (横竖屏，来自 labs.google.har)
    "veo_3_1_t2v_lite_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_t2v_lite_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },

    # ========== 首尾帧模型 (I2V - Image to Video) ==========
    # 支持1-2张图片：1张作为首帧，2张作为首尾帧

    # veo_3_1_i2v_s_fast_fl (需要新增横竖屏)
    "veo_3_1_i2v_s_fast_portrait_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra (横竖屏)
    "veo_3_1_i2v_s_fast_portrait_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra_relaxed (需要新增横竖屏)
    "veo_3_1_i2v_s_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s (需要新增横竖屏)
    "veo_3_1_i2v_s_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    # veo_3_1_i2v_lite (横竖屏，仅首帧，来自 labs.google.har)
    "veo_3_1_i2v_lite_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 1,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_i2v_lite_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 1,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    # veo_3_1_interpolation_lite (横竖屏，首尾帧，来自 labs.google.har)
    "veo_3_1_interpolation_lite_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_interpolation_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 2,
        "max_images": 2,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_interpolation_lite_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_interpolation_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 2,
        "max_images": 2,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },

    # ========== 多图生成 (R2V - Reference Images to Video) ==========
    # 当前上游协议最多支持 3 张参考图

    # veo_3_1_r2v_fast (横竖屏)
    "veo_3_1_r2v_fast_portrait": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra (横竖屏)
    "veo_3_1_r2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra_relaxed (横竖屏)
    "veo_3_1_r2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # ========== 视频放大 (Video Upsampler) ==========
    # 仅 3.1 支持，需要先生成视频后再放大，可能需要 30 分钟

    # T2V 4K 放大版
    "veo_3_1_t2v_fast_portrait_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # T2V 1080P 放大版
    "veo_3_1_t2v_fast_portrait_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # I2V 4K 放大版
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # I2V 1080P 放大版
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # R2V 4K 放大版
    "veo_3_1_r2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_r2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # R2V 1080P 放大版
    "veo_3_1_r2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_r2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # ========== 视频续写 (Extend - Video Continuation) ==========
    # 基于已生成的视频续写7秒，最多续写20次（最长148秒）
    # 需要提供源视频的 mediaGenerationId

    # VEO 3.1 Extend (横竖屏)
    "veo_3_1_extend_portrait": {
        "type": "video",
        "video_type": "extend",
        "model_key": "veo_3_1_extend_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "requires_video_id": True,
    },
    "veo_3_1_extend": {
        "type": "video",
        "video_type": "extend",
        "model_key": "veo_3_1_extend_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "requires_video_id": True,
    },
}


def _make_t2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "t2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": False,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _make_i2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    min_images: int = 1,
    max_images: int = 2,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "i2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": True,
        "min_images": min_images,
        "max_images": max_images,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _apply_veo_3_1_model_updates():
    """Keep the public aliases aligned with the current Veo 3.1 model families."""
    landscape = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    portrait = "VIDEO_ASPECT_RATIO_PORTRAIT"

    def add_alias(alias: str, target: str):
        MODEL_CONFIG[alias] = dict(MODEL_CONFIG[target])

    # Non-fast/non-lite Veo 3.1 aliases must call Quality upstream keys.
    MODEL_CONFIG["veo_3_1_t2v_landscape"].update({"model_key": "veo_3_1_t2v"})
    MODEL_CONFIG["veo_3_1_t2v_portrait"].update({"model_key": "veo_3_1_t2v_portrait"})
    MODEL_CONFIG["veo_3_1_i2v_s_landscape"].update({"model_key": "veo_3_1_i2v_s_fl"})
    MODEL_CONFIG["veo_3_1_i2v_s_portrait"].update({"model_key": "veo_3_1_i2v_s_portrait_fl"})
    MODEL_CONFIG["veo_3_1_extend"].update({"model_key": "veo_3_1_extend_landscape"})
    MODEL_CONFIG["veo_3_1_extend_portrait"].update({"model_key": "veo_3_1_extend_portrait"})

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # T2V duration variants.
        MODEL_CONFIG[f"veo_3_1_t2v_fast_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_fast_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", portrait
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_landscape"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            landscape,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_portrait"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            portrait,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", portrait
        )

        # I2V duration variants. FL keys are used for 2 images; the single-image path strips "_fl".
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_portrait_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", portrait
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            landscape,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            portrait,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            landscape,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            portrait,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait
        )

        for resolution_name, resolution, upsampler_model_key in (
            ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
            ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ):
            upsample = {"resolution": resolution, "model_key": upsampler_model_key}
            MODEL_CONFIG[f"veo_3_1_t2v_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", portrait, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait, upsample=upsample
            )

    for resolution_name, resolution, upsampler_model_key in (
        ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
    ):
        upsample = {"resolution": resolution, "model_key": upsampler_model_key}
        MODEL_CONFIG[f"veo_3_1_t2v_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v_portrait", portrait, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_fl", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_portrait_fl", portrait, upsample=upsample
        )

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # Explicit landscape names for /v1/models; short landscape names remain compatible.
        add_alias(f"veo_3_1_t2v_fast_landscape_{suffix}", f"veo_3_1_t2v_fast_{suffix}")
        add_alias(f"veo_3_1_t2v_landscape_{suffix}", f"veo_3_1_t2v_{suffix}")
        add_alias(f"veo_3_1_i2v_s_fast_landscape_{suffix}_fl", f"veo_3_1_i2v_s_fast_{suffix}_fl")
        add_alias(f"veo_3_1_i2v_s_landscape_{suffix}", f"veo_3_1_i2v_s_{suffix}")

        add_alias(f"veo_3_1_t2v_lite_landscape_{suffix}", f"veo_3_1_t2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_t2v_lite_portrait_{suffix}", f"veo_3_1_t2v_lite_{suffix}_portrait")
        add_alias(f"veo_3_1_i2v_lite_landscape_{suffix}", f"veo_3_1_i2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_i2v_lite_portrait_{suffix}", f"veo_3_1_i2v_lite_{suffix}_portrait")
        add_alias(
            f"veo_3_1_interpolation_lite_landscape_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_landscape",
        )
        add_alias(
            f"veo_3_1_interpolation_lite_portrait_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_portrait",
        )

        for resolution_name in ("4k", "1080p"):
            add_alias(
                f"veo_3_1_t2v_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_t2v_{suffix}_{resolution_name}",
            )
            add_alias(
                f"veo_3_1_i2v_s_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_i2v_s_{suffix}_{resolution_name}",
            )

    for resolution_name in ("4k", "1080p"):
        add_alias(f"veo_3_1_t2v_landscape_{resolution_name}", f"veo_3_1_t2v_{resolution_name}")
        add_alias(f"veo_3_1_i2v_s_landscape_{resolution_name}", f"veo_3_1_i2v_s_{resolution_name}")

    add_alias("veo_3_1_r2v_fast_landscape", "veo_3_1_r2v_fast")
    add_alias("veo_3_1_r2v_fast_landscape_ultra", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_relaxed", "veo_3_1_r2v_fast_ultra_relaxed")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_4k", "veo_3_1_r2v_fast_ultra_4k")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_1080p", "veo_3_1_r2v_fast_ultra_1080p")


_apply_veo_3_1_model_updates()


def _known_video_model_keys() -> set[str]:
    return {
        cfg["model_key"]
        for cfg in MODEL_CONFIG.values()
        if cfg.get("type") == "video" and cfg.get("model_key")
    }


def _resolve_tier_two_model_key(model_key: str) -> str:
    """Only upgrade to an ultra key when that exact upstream key is known valid."""
    if "ultra" in model_key:
        return model_key
    if "_fl" in model_key:
        candidate = model_key.replace("_fl", "_ultra_fl")
    else:
        candidate = model_key + "_ultra"
    return candidate if candidate in _known_video_model_keys() else model_key



__all__ = ["MODEL_CONFIG", "_make_t2v_config", "_make_i2v_config", "_apply_veo_3_1_model_updates", "_known_video_model_keys", "_resolve_tier_two_model_key"]

for key, cfg in MODEL_CONFIG.items():
    if key.startswith('veo_3_1'):
        cfg['use_v2_model_config'] = True
