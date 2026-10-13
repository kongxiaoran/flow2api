"""Model name resolver - converts simplified model names + generationConfig params to internal MODEL_CONFIG keys.

When upstream services (e.g. New API) send requests with a generic model name
along with generationConfig containing aspectRatio / imageSize, this module
resolves them to the specific internal model name used by flow2api.

Example:
    model = "gemini-3.0-pro-image"
    generationConfig.imageConfig.aspectRatio = "16:9"
    generationConfig.imageConfig.imageSize = "2k"
    → resolved to "gemini-3.0-pro-image-landscape-2k"
"""

import re
from io import BytesIO
from typing import Optional, Dict, Any, Tuple
from ..core.logger import debug_logger

# ──────────────────────────────────────────────
# 简化模型名 → 基础模型名前缀 的映射
# ──────────────────────────────────────────────
IMAGE_BASE_MODELS = {
    # Gemini 3.0 Pro (GEM_PIX_2)
    "gemini-3.0-pro-image": "gemini-3.0-pro-image",
    # Gemini 3.1 Flash (NARWHAL)
    "gemini-3.1-flash-image": "gemini-3.1-flash-image",
    # Imagen 4.0 (IMAGEN_3_5)
    "imagen-4.0-generate-preview": "imagen-4.0-generate-preview",
}

# ──────────────────────────────────────────────
# aspectRatio 转换映射
# 支持 Gemini 原生格式 ("16:9") 和内部格式 ("landscape")
# ──────────────────────────────────────────────
ASPECT_RATIO_MAP = {
    # Gemini 标准 ratio 格式
    "16:9": "landscape",
    "9:16": "portrait",
    "1:1": "square",
    "4:3": "four-three",
    "3:4": "three-four",
    # 英文名直接映射
    "landscape": "landscape",
    "portrait": "portrait",
    "square": "square",
    "four-three": "four-three",
    "three-four": "three-four",
    "four_three": "four-three",
    "three_four": "three-four",
    # 大写形式
    "LANDSCAPE": "landscape",
    "PORTRAIT": "portrait",
    "SQUARE": "square",
}

# 每个基础模型支持的 aspectRatio 列表
# 如果请求的 ratio 不在支持列表中，降级到默认值
MODEL_SUPPORTED_ASPECTS = {
    "gemini-3.0-pro-image": [
        "landscape",
        "portrait",
        "square",
        "four-three",
        "three-four",
    ],
    "gemini-3.1-flash-image": [
        "landscape",
        "portrait",
        "square",
        "four-three",
        "three-four",
    ],
    "imagen-4.0-generate-preview": ["landscape", "portrait"],
}

# 每个基础模型支持的 imageSize（分辨率）列表
MODEL_SUPPORTED_SIZES = {
    "gemini-3.0-pro-image": ["2k", "4k"],
    "gemini-3.1-flash-image": ["2k", "4k"],
    "imagen-4.0-generate-preview": [],  # 不支持放大
}

# imageSize 归一化映射
IMAGE_SIZE_MAP = {
    "1k": "1k",
    "1K": "1k",
    "2k": "2k",
    "2K": "2k",
    "4k": "4k",
    "4K": "4k",
    "1080p": "1080p",
    "1080P": "1080p",
    "": "",
}

# 默认 aspectRatio
DEFAULT_ASPECT = "landscape"

OPENAI_IMAGE_SIZE_RE = re.compile(r"^(?P<w>\d{2,5})\s*[xX]\s*(?P<h>\d{2,5})$")

# OpenAI 常见 quality → imageSize 映射
# - 这里的 imageSize 是 flow2api 的“放大档位”，并不等价于 OpenAI 的像素尺寸；
#   但可用作“画质/清晰度”的近似映射。
OPENAI_QUALITY_MAP = {
    "low": None,
    "standard": None,
    "medium": "2k",
    "high": "4k",
    "hd": "4k",
    "ultra": "4k",
}

# 用于把 OpenAI size（如 1024x1792）映射到最接近的 flow2api aspect 选项
ASPECT_RATIO_FLOAT_MAP = {
    "landscape": 16 / 9,
    "portrait": 9 / 16,
    "square": 1.0,
    "four-three": 4 / 3,
    "three-four": 3 / 4,
}


def _aspect_from_dimensions(
    width: int, height: int, *, video_mode: bool = False
) -> Optional[str]:
    if width <= 0 or height <= 0:
        return None

    ratio = width / height
    inferred = min(
        ASPECT_RATIO_FLOAT_MAP.items(),
        key=lambda item: abs(ratio - item[1]),
    )[0]

    if video_mode and inferred not in {"landscape", "portrait"}:
        return "portrait" if height > width else "landscape"
    return inferred


def _infer_aspect_ratio_from_images(
    images: Any,
    *,
    video_mode: bool = False,
) -> Optional[str]:
    if not isinstance(images, (list, tuple)):
        return None

    source_bytes = next(
        (
            bytes(item)
            for item in images
            if isinstance(item, (bytes, bytearray)) and item
        ),
        None,
    )
    if not source_bytes:
        return None

    try:
        from PIL import Image, ImageOps

        with Image.open(BytesIO(source_bytes)) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
    except Exception as exc:
        debug_logger.log_warning(
            f"[MODEL_RESOLVER] 参考图尺寸解析失败，跳过自动比例跟随: {exc}"
        )
        return None

    inferred = _aspect_from_dimensions(width, height, video_mode=video_mode)
    if inferred:
        debug_logger.log_info(
            f"[MODEL_RESOLVER] 未显式指定 aspectRatio，已按参考图尺寸自动推断: {width}x{height} -> {inferred}"
        )
    return inferred


# ──────────────────────────────────────────────
# 视频模型简化名映射
# ──────────────────────────────────────────────
VIDEO_BASE_MODELS = {
    # T2V models
    "veo_3_1_t2v_fast": {
        "landscape": "veo_3_1_t2v_fast_landscape",
        "portrait": "veo_3_1_t2v_fast_portrait",
    },
    "veo_3_1_t2v_fast_4s": {
        "landscape": "veo_3_1_t2v_fast_4s",
        "portrait": "veo_3_1_t2v_fast_portrait_4s",
    },
    "veo_3_1_t2v_fast_6s": {
        "landscape": "veo_3_1_t2v_fast_6s",
        "portrait": "veo_3_1_t2v_fast_portrait_6s",
    },
    "veo_3_1_t2v_fast_8s": {
        "landscape": "veo_3_1_t2v_fast_8s",
        "portrait": "veo_3_1_t2v_fast_portrait_8s",
    },
    "veo_3_1_t2v_fast_ultra": {
        "landscape": "veo_3_1_t2v_fast_ultra",
        "portrait": "veo_3_1_t2v_fast_portrait_ultra",
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "landscape": "veo_3_1_t2v_fast_ultra_relaxed",
        "portrait": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_t2v": {
        "landscape": "veo_3_1_t2v_landscape",
        "portrait": "veo_3_1_t2v_portrait",
    },
    "veo_3_1_t2v_4s": {
        "landscape": "veo_3_1_t2v_4s",
        "portrait": "veo_3_1_t2v_portrait_4s",
    },
    "veo_3_1_t2v_6s": {
        "landscape": "veo_3_1_t2v_6s",
        "portrait": "veo_3_1_t2v_portrait_6s",
    },
    "veo_3_1_t2v_8s": {
        "landscape": "veo_3_1_t2v_8s",
        "portrait": "veo_3_1_t2v_portrait_8s",
    },
    "veo_3_1_t2v_4s_4k": {
        "landscape": "veo_3_1_t2v_4s_4k",
        "portrait": "veo_3_1_t2v_portrait_4s_4k",
    },
    "veo_3_1_t2v_4s_1080p": {
        "landscape": "veo_3_1_t2v_4s_1080p",
        "portrait": "veo_3_1_t2v_portrait_4s_1080p",
    },
    "veo_3_1_t2v_6s_4k": {
        "landscape": "veo_3_1_t2v_6s_4k",
        "portrait": "veo_3_1_t2v_portrait_6s_4k",
    },
    "veo_3_1_t2v_6s_1080p": {
        "landscape": "veo_3_1_t2v_6s_1080p",
        "portrait": "veo_3_1_t2v_portrait_6s_1080p",
    },
    "veo_3_1_t2v_8s_4k": {
        "landscape": "veo_3_1_t2v_8s_4k",
        "portrait": "veo_3_1_t2v_portrait_8s_4k",
    },
    "veo_3_1_t2v_8s_1080p": {
        "landscape": "veo_3_1_t2v_8s_1080p",
        "portrait": "veo_3_1_t2v_portrait_8s_1080p",
    },
    "veo_3_1_t2v_4k": {
        "landscape": "veo_3_1_t2v_4k",
        "portrait": "veo_3_1_t2v_portrait_4k",
    },
    "veo_3_1_t2v_1080p": {
        "landscape": "veo_3_1_t2v_1080p",
        "portrait": "veo_3_1_t2v_portrait_1080p",
    },
    "veo_3_1_t2v_lite": {
        "landscape": "veo_3_1_t2v_lite_landscape",
        "portrait": "veo_3_1_t2v_lite_portrait",
    },
    "veo_3_1_t2v_lite_4s": {
        "landscape": "veo_3_1_t2v_lite_4s_landscape",
        "portrait": "veo_3_1_t2v_lite_4s_portrait",
    },
    "veo_3_1_t2v_lite_6s": {
        "landscape": "veo_3_1_t2v_lite_6s_landscape",
        "portrait": "veo_3_1_t2v_lite_6s_portrait",
    },
    "veo_3_1_t2v_lite_8s": {
        "landscape": "veo_3_1_t2v_lite_8s_landscape",
        "portrait": "veo_3_1_t2v_lite_8s_portrait",
    },
    "omni": {
        "landscape": "omni",
        "portrait": "omni_portrait",
    },
    # I2V models
    "veo_3_1_i2v_s_fast_fl": {
        "landscape": "veo_3_1_i2v_s_fast_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_fl",
    },
    "veo_3_1_i2v_s_fast_4s_fl": {
        "landscape": "veo_3_1_i2v_s_fast_4s_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_4s_fl",
    },
    "veo_3_1_i2v_s_fast_6s_fl": {
        "landscape": "veo_3_1_i2v_s_fast_6s_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_6s_fl",
    },
    "veo_3_1_i2v_s_fast_8s_fl": {
        "landscape": "veo_3_1_i2v_s_fast_8s_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_8s_fl",
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "landscape": "veo_3_1_i2v_s_fast_ultra_fl",
        "portrait": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "landscape": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "portrait": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_i2v_s": {
        "landscape": "veo_3_1_i2v_s_landscape",
        "portrait": "veo_3_1_i2v_s_portrait",
    },
    "veo_3_1_i2v_s_4s": {
        "landscape": "veo_3_1_i2v_s_4s",
        "portrait": "veo_3_1_i2v_s_portrait_4s",
    },
    "veo_3_1_i2v_s_6s": {
        "landscape": "veo_3_1_i2v_s_6s",
        "portrait": "veo_3_1_i2v_s_portrait_6s",
    },
    "veo_3_1_i2v_s_8s": {
        "landscape": "veo_3_1_i2v_s_8s",
        "portrait": "veo_3_1_i2v_s_portrait_8s",
    },
    "veo_3_1_i2v_s_4s_4k": {
        "landscape": "veo_3_1_i2v_s_4s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_4s_4k",
    },
    "veo_3_1_i2v_s_4s_1080p": {
        "landscape": "veo_3_1_i2v_s_4s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_4s_1080p",
    },
    "veo_3_1_i2v_s_6s_4k": {
        "landscape": "veo_3_1_i2v_s_6s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_6s_4k",
    },
    "veo_3_1_i2v_s_6s_1080p": {
        "landscape": "veo_3_1_i2v_s_6s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_6s_1080p",
    },
    "veo_3_1_i2v_s_8s_4k": {
        "landscape": "veo_3_1_i2v_s_8s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_8s_4k",
    },
    "veo_3_1_i2v_s_8s_1080p": {
        "landscape": "veo_3_1_i2v_s_8s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_8s_1080p",
    },
    "veo_3_1_i2v_s_4k": {
        "landscape": "veo_3_1_i2v_s_4k",
        "portrait": "veo_3_1_i2v_s_portrait_4k",
    },
    "veo_3_1_i2v_s_1080p": {
        "landscape": "veo_3_1_i2v_s_1080p",
        "portrait": "veo_3_1_i2v_s_portrait_1080p",
    },
    "veo_3_1_i2v_lite": {
        "landscape": "veo_3_1_i2v_lite_landscape",
        "portrait": "veo_3_1_i2v_lite_portrait",
    },
    "veo_3_1_i2v_lite_4s": {
        "landscape": "veo_3_1_i2v_lite_4s_landscape",
        "portrait": "veo_3_1_i2v_lite_4s_portrait",
    },
    "veo_3_1_i2v_lite_6s": {
        "landscape": "veo_3_1_i2v_lite_6s_landscape",
        "portrait": "veo_3_1_i2v_lite_6s_portrait",
    },
    "veo_3_1_i2v_lite_8s": {
        "landscape": "veo_3_1_i2v_lite_8s_landscape",
        "portrait": "veo_3_1_i2v_lite_8s_portrait",
    },
    "veo_3_1_interpolation_lite": {
        "landscape": "veo_3_1_interpolation_lite_landscape",
        "portrait": "veo_3_1_interpolation_lite_portrait",
    },
    "veo_3_1_interpolation_lite_4s": {
        "landscape": "veo_3_1_interpolation_lite_4s_landscape",
        "portrait": "veo_3_1_interpolation_lite_4s_portrait",
    },
    "veo_3_1_interpolation_lite_6s": {
        "landscape": "veo_3_1_interpolation_lite_6s_landscape",
        "portrait": "veo_3_1_interpolation_lite_6s_portrait",
    },
    "veo_3_1_interpolation_lite_8s": {
        "landscape": "veo_3_1_interpolation_lite_8s_landscape",
        "portrait": "veo_3_1_interpolation_lite_8s_portrait",
    },
    # R2V models
    "veo_3_1_r2v_fast": {
        "landscape": "veo_3_1_r2v_fast",
        "portrait": "veo_3_1_r2v_fast_portrait",
    },
    "veo_3_1_r2v_fast_8s": {
        "landscape": "veo_3_1_r2v_fast_8s",
        "portrait": "veo_3_1_r2v_fast_portrait_8s",
    },
    "veo_3_1_r2v_fast_ultra": {
        "landscape": "veo_3_1_r2v_fast_ultra",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra",
    },
    "veo_3_1_r2v_fast_ultra_8s": {
        "landscape": "veo_3_1_r2v_fast_ultra_8s",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra_8s",
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "landscape": "veo_3_1_r2v_fast_ultra_relaxed",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
    },
    "veo_3_1_r2v_fast_ultra_relaxed_8s": {
        "landscape": "veo_3_1_r2v_fast_ultra_relaxed_8s",
        "portrait": "veo_3_1_r2v_fast_portrait_ultra_relaxed_8s",
    },
    # Extend models (视频续写)
    "veo_3_1_extend": {
        "landscape": "veo_3_1_extend",
        "portrait": "veo_3_1_extend_portrait",
    },
}


def _extract_generation_params(request) -> Tuple[Optional[str], Optional[str]]:
    """从请求中提取 aspectRatio 和 imageSize 参数。

    优先级：
    1. request.generationConfig.imageConfig (顶层 Gemini 参数)
    2. extra fields 中的 generationConfig (extra_body 透传)
    3. OpenAI 风格字段（size/quality）兼容：可在 generationConfig/imageConfig 或顶层 extra 中出现

    Returns:
        (aspect_ratio, image_size) 归一化后的值
    """

    def _normalize_str(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text if text else None

    def _read_value(obj: Any, *keys: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            for key in keys:
                if key in obj:
                    return obj.get(key)
            return None

        for key in keys:
            if hasattr(obj, key):
                value = getattr(obj, key, None)
                if value is not None:
                    return value

        extra = getattr(obj, "__pydantic_extra__", None) or {}
        for key in keys:
            if key in extra:
                return extra.get(key)
        return None

    def _normalize_aspect_ratio(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        token = (
            raw.replace("：", ":")
            .replace("/", ":")
            .replace("x", ":")
            .replace("X", ":")
            .replace(" ", "")
            .strip()
        )

        mapped = ASPECT_RATIO_MAP.get(token)
        if mapped:
            return mapped
        mapped = ASPECT_RATIO_MAP.get(token.lower())
        if mapped:
            return mapped
        mapped = ASPECT_RATIO_MAP.get(token.upper())
        if mapped:
            return mapped
        return token

    def _normalize_image_size(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        token = raw.replace(" ", "").strip()
        mapped = IMAGE_SIZE_MAP.get(token)
        if mapped is not None:
            return mapped or None
        mapped = IMAGE_SIZE_MAP.get(token.lower())
        if mapped is not None:
            return mapped or None
        mapped = IMAGE_SIZE_MAP.get(token.upper())
        if mapped is not None:
            return mapped or None
        return token.lower()

    def _aspect_from_openai_size(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        match = OPENAI_IMAGE_SIZE_RE.match(raw)
        if not match:
            return None

        try:
            width = int(match.group("w"))
            height = int(match.group("h"))
        except Exception:
            return None

        return _aspect_from_dimensions(width, height)

    def _image_size_from_openai_quality(value: Any) -> Optional[str]:
        raw = _normalize_str(value)
        if not raw:
            return None

        token = raw.strip().lower()
        if token in IMAGE_SIZE_MAP:
            return _normalize_image_size(token)

        mapped = OPENAI_QUALITY_MAP.get(token)
        if mapped:
            return mapped
        return None

    def _apply_image_config(
        image_config: Any, aspect_ratio: Optional[str], image_size: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        # 显式 aspectRatio/imageSize
        if not aspect_ratio:
            aspect_ratio = _normalize_aspect_ratio(
                _read_value(image_config, "aspectRatio", "aspect_ratio", "aspect")
            )
        if not image_size:
            image_size = _normalize_image_size(
                _read_value(image_config, "imageSize", "image_size", "resolution")
            )

        # OpenAI size/quality
        if not aspect_ratio:
            aspect_ratio = _aspect_from_openai_size(_read_value(image_config, "size"))
        if not image_size:
            image_size = _image_size_from_openai_quality(
                _read_value(image_config, "quality", "imageQuality", "image_quality")
            )

        return aspect_ratio, image_size

    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None

    # 1) 优先从 request.generationConfig 解析
    gen_config = getattr(request, "generationConfig", None)
    if gen_config is not None:
        image_config = _read_value(gen_config, "imageConfig", "image_config")
        if image_config is not None:
            aspect_ratio, image_size = _apply_image_config(
                image_config, aspect_ratio, image_size
            )

        # 有些上游会把字段放在 generationConfig 顶层
        if not aspect_ratio:
            aspect_ratio = _normalize_aspect_ratio(
                _read_value(gen_config, "aspectRatio", "aspect_ratio")
            )
        if not image_size:
            image_size = _normalize_image_size(
                _read_value(gen_config, "imageSize", "image_size")
            )

        if not aspect_ratio:
            aspect_ratio = _aspect_from_openai_size(_read_value(gen_config, "size"))
        if not image_size:
            image_size = _image_size_from_openai_quality(
                _read_value(gen_config, "quality")
            )

    # 2) 顶层没有时，再尝试从 extra fields (Pydantic extra="allow") 中透传的 generationConfig
    if (aspect_ratio is None or image_size is None) and hasattr(
        request, "__pydantic_extra__"
    ):
        extra = request.__pydantic_extra__ or {}
        gen_config_raw = extra.get("generationConfig")
        if not isinstance(gen_config_raw, dict):
            extra_body = extra.get("extra_body") or extra.get("extraBody")
            if isinstance(extra_body, dict):
                gen_config_raw = extra_body.get("generationConfig")

        if isinstance(gen_config_raw, dict):
            image_config_raw = (
                gen_config_raw.get("imageConfig")
                or gen_config_raw.get("image_config")
                or {}
            )
            if image_config_raw:
                aspect_ratio, image_size = _apply_image_config(
                    image_config_raw, aspect_ratio, image_size
                )

            if aspect_ratio is None:
                aspect_ratio = _normalize_aspect_ratio(
                    gen_config_raw.get("aspectRatio")
                    or gen_config_raw.get("aspect_ratio")
                )
            if image_size is None:
                image_size = _normalize_image_size(
                    gen_config_raw.get("imageSize") or gen_config_raw.get("image_size")
                )

            if aspect_ratio is None:
                aspect_ratio = _aspect_from_openai_size(gen_config_raw.get("size"))
            if image_size is None:
                image_size = _image_size_from_openai_quality(
                    gen_config_raw.get("quality")
                )

    # 3) OpenAI 风格 size/quality（顶层 extra）兼容
    if (aspect_ratio is None or image_size is None) and hasattr(
        request, "__pydantic_extra__"
    ):
        extra = request.__pydantic_extra__ or {}
        if aspect_ratio is None:
            aspect_ratio = _aspect_from_openai_size(extra.get("size"))
        if image_size is None:
            image_size = _image_size_from_openai_quality(extra.get("quality"))

        # 一些上游可能直接传 aspect_ratio/image_size
        if aspect_ratio is None:
            aspect_ratio = _normalize_aspect_ratio(
                extra.get("aspect_ratio") or extra.get("aspectRatio")
            )
        if image_size is None:
            image_size = _normalize_image_size(
                extra.get("image_size") or extra.get("imageSize")
            )

    return aspect_ratio, image_size


def resolve_model_name(
    model: str, request=None, model_config: Dict[str, Any] = None, images: Any = None
) -> str:
    """将简化模型名 + generationConfig 参数解析为内部 MODEL_CONFIG key。

    如果 model 已经是有效的 MODEL_CONFIG key，直接返回。
    如果 model 是简化名（基础模型名），则根据 generationConfig 中的
    aspectRatio / imageSize 拼接出完整的内部模型名。

    Args:
        model: 请求中的模型名
        request: ChatCompletionRequest 实例（用于提取 generationConfig）
        model_config: MODEL_CONFIG 字典（用于验证解析后的模型名）

    Returns:
        解析后的内部模型名
    """
    # ────── 图片模型解析 ──────
    if model in IMAGE_BASE_MODELS:
        base = IMAGE_BASE_MODELS[model]
        aspect_ratio, image_size = (
            _extract_generation_params(request) if request else (None, None)
        )

        if not aspect_ratio:
            aspect_ratio = _infer_aspect_ratio_from_images(images)

        # 默认 aspect ratio
        if not aspect_ratio:
            aspect_ratio = DEFAULT_ASPECT

        # 检查支持的 aspect ratio
        supported_aspects = MODEL_SUPPORTED_ASPECTS.get(base, [])
        if aspect_ratio not in supported_aspects and supported_aspects:
            debug_logger.log_warning(
                f"[MODEL_RESOLVER] 模型 {base} 不支持 aspectRatio={aspect_ratio}，"
                f"降级到 {DEFAULT_ASPECT}"
            )
            aspect_ratio = DEFAULT_ASPECT

        # 拼接模型名
        resolved = f"{base}-{aspect_ratio}"

        # 检查支持的 imageSize
        if image_size and image_size != "1k":
            supported_sizes = MODEL_SUPPORTED_SIZES.get(base, [])
            if image_size in supported_sizes:
                resolved = f"{resolved}-{image_size}"
            else:
                debug_logger.log_warning(
                    f"[MODEL_RESOLVER] 模型 {base} 不支持 imageSize={image_size}，忽略"
                )

        # 最终验证
        if model_config and resolved not in model_config:
            debug_logger.log_warning(
                f"[MODEL_RESOLVER] 解析后的模型名 {resolved} 不在 MODEL_CONFIG 中，"
                f"回退到原始模型名 {model}"
            )
            return model

        debug_logger.log_info(
            f"[MODEL_RESOLVER] 模型名转换: {model} → {resolved} "
            f"(aspectRatio={aspect_ratio}, imageSize={image_size or 'default'})"
        )
        return resolved

    # ────── 视频模型解析 ──────
    if model in VIDEO_BASE_MODELS:
        aspect_ratio, image_size = (
            _extract_generation_params(request) if request else (None, None)
        )

        if not aspect_ratio:
            aspect_ratio = _infer_aspect_ratio_from_images(images, video_mode=True)

        # 视频默认横屏
        if not aspect_ratio or aspect_ratio not in ("landscape", "portrait"):
            aspect_ratio = "landscape"

        if (
            image_size in ("4k", "1080p")
            and f"{model}_{image_size}" in VIDEO_BASE_MODELS
        ):
            model = f"{model}_{image_size}"

        orientation_map = VIDEO_BASE_MODELS[model]
        resolved = orientation_map.get(aspect_ratio)

        if resolved and model_config and resolved in model_config:
            debug_logger.log_info(
                f"[MODEL_RESOLVER] 视频模型名转换: {model} → {resolved} "
                f"(aspectRatio={aspect_ratio})"
            )
            return resolved

        debug_logger.log_warning(
            f"[MODEL_RESOLVER] 视频模型 {model} 解析失败 (aspect={aspect_ratio})，"
            f"使用原始模型名"
        )
        return model

    # ────── 公开视频基础模型（veo-3.1-lite / -fast / -quality、omni-1.1-flash）──────
    # Link 补丁（2026-10-13）：基础名 + 参数（aspectRatio / durationSeconds / 图片数量）→ 具体名。
    # 不带任何参数且不带图片时等价于原来的 8 秒横屏别名；具体名（如 veo-3.1-fast-6s-portrait）仍可直接调用。
    if model in PUBLIC_VIDEO_BASE_MODELS:
        resolved_public = _resolve_public_video_model(model, request, model_config, images)
        if resolved_public:
            return resolved_public

    # 如果已经是有效的 MODEL_CONFIG key，直接返回
    if model_config and model in model_config:
        return model

    # 未知模型名，原样返回（由下游 MODEL_CONFIG 校验报错）
    return model


def get_base_model_aliases() -> Dict[str, str]:
    """返回所有简化模型名（别名）及其描述，用于 /v1/models 接口展示。"""
    aliases = {}

    for alias, base in IMAGE_BASE_MODELS.items():
        aspects = MODEL_SUPPORTED_ASPECTS.get(base, [])
        sizes = MODEL_SUPPORTED_SIZES.get(base, [])
        desc_parts = [f"aspects: {', '.join(aspects)}"]
        if sizes:
            desc_parts.append(f"sizes: {', '.join(sizes)}")
        aliases[alias] = f"Image generation (alias) - {'; '.join(desc_parts)}"

    for alias in VIDEO_BASE_MODELS:
        aliases[alias] = (
            "Video generation (alias) - supports landscape/portrait via generationConfig"
        )

    return aliases


# ──────────────────────────────────────────────────────────────
# Link 补丁（2026-10-13）：公开基础模型目录
#
# /v1/models 默认只列“基础模型”，尺寸 / 画幅 / 时长都是请求参数；具体名字
# （gemini-3.1-flash-image-portrait-4k、veo-3.1-fast-6s-portrait 等）仍然可以直接调用，
# 需要时用 /v1/models?full=1 查看完整列表。
# ──────────────────────────────────────────────────────────────
PUBLIC_VIDEO_BASE_MODELS: Dict[str, Dict[str, Any]] = {
    "veo-3.1-lite": {"family": "lite", "label": "Veo 3.1 Lite", "durations": (4, 6, 8)},
    "veo-3.1-fast": {"family": "fast", "label": "Veo 3.1 Fast", "durations": (4, 6, 8)},
    "veo-3.1-quality": {"family": "quality", "label": "Veo 3.1 Quality", "durations": (4, 6, 8)},
    "omni-1.1-flash": {"family": "flash", "label": "Omni 1.1 Flash", "durations": (4, 6, 8, 10)},
}
VIDEO_DEFAULT_DURATION = 8
I2V_DURATIONS = (4, 6)  # 首帧 / 首尾帧（i2v）只有 4s、6s
R2V_FAMILIES = ("lite", "fast")  # 多图参考（r2v）只有 lite / fast，固定 8s
ASPECT_DISPLAY = {
    "landscape": "16:9",
    "portrait": "9:16",
    "square": "1:1",
    "four-three": "4:3",
    "three-four": "3:4",
}
ASPECT_ORDER = ("landscape", "portrait", "square", "four-three", "three-four")

_IMAGE_MODE_ALIASES = {
    "frames": "frames",
    "frame": "frames",
    "first_last": "frames",
    "first-last": "frames",
    "first_frame": "frames",
    "i2v": "frames",
    "reference": "reference",
    "references": "reference",
    "ref": "reference",
    "r2v": "reference",
}


def _param_sources(request: Any) -> list:
    """按优先级收集可能放参数的 dict：generationConfig（含 videoConfig/imageConfig）、顶层 extra、extra_body。"""
    sources: list = []

    def as_dict(obj: Any) -> Optional[Dict[str, Any]]:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj
        dump = getattr(obj, "model_dump", None)
        if callable(dump):
            try:
                return dump()
            except Exception:
                return None
        return None

    def add_with_nested(obj: Any) -> None:
        data = as_dict(obj)
        if not data:
            return
        sources.append(data)
        for key in ("videoConfig", "video_config", "imageConfig", "image_config"):
            nested = as_dict(data.get(key))
            if nested:
                sources.append(nested)

    if request is None:
        return sources
    add_with_nested(getattr(request, "generationConfig", None))
    extra = getattr(request, "__pydantic_extra__", None) or {}
    if isinstance(extra, dict):
        add_with_nested(extra.get("generationConfig"))
        add_with_nested(extra.get("video_config") or extra.get("videoConfig"))
        extra_body = extra.get("extra_body") or extra.get("extraBody")
        if isinstance(extra_body, dict):
            add_with_nested(extra_body.get("generationConfig"))
            add_with_nested(extra_body)
        add_with_nested(extra)
    return sources


def _first_param(sources: list, *keys: str) -> Any:
    for source in sources:
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
    return None


def _normalize_aspect_token(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = (
        value.strip().replace("：", ":").replace("/", ":").replace(" ", "")
    )
    if not token:
        return None
    return (
        ASPECT_RATIO_MAP.get(token)
        or ASPECT_RATIO_MAP.get(token.lower())
        or ASPECT_RATIO_MAP.get(token.upper())
    )


def _parse_duration_seconds(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = int(round(float(value)))
        return seconds if seconds > 0 else None
    if isinstance(value, str):
        match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?\s*$", value, re.IGNORECASE)
        if match:
            seconds = int(round(float(match.group(1))))
            return seconds if seconds > 0 else None
    return None


def _nearest_option(value: int, options: tuple) -> int:
    # 距离相同取较大的（请求 5 秒时给 6 秒，而不是 4 秒）
    return min(options, key=lambda option: (abs(option - value), -option))


def extract_video_params(request: Any) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """从请求里提取视频参数，返回 (aspect, duration_seconds, image_mode)。

    - aspect：landscape / portrait（支持 16:9、9:16、landscape、portrait，以及 OpenAI 风格 size=1280x720）
    - duration_seconds：durationSeconds / duration_seconds / duration / seconds
    - image_mode：frames（首帧/首尾帧）或 reference（多图参考）
    """
    sources = _param_sources(request)

    aspect = _normalize_aspect_token(
        _first_param(sources, "aspectRatio", "aspect_ratio", "aspect")
    )
    if aspect is None:
        size_value = _first_param(sources, "size")
        if isinstance(size_value, str):
            match = OPENAI_IMAGE_SIZE_RE.match(size_value.strip())
            if match:
                aspect = _aspect_from_dimensions(
                    int(match.group("w")), int(match.group("h")), video_mode=True
                )
    if aspect not in ("landscape", "portrait"):
        aspect = "portrait" if aspect in ("three-four",) else ("landscape" if aspect else None)

    duration = _parse_duration_seconds(
        _first_param(sources, "durationSeconds", "duration_seconds", "duration", "seconds")
    )

    mode_raw = _first_param(sources, "imageMode", "image_mode", "referenceMode", "mode")
    image_mode = (
        _IMAGE_MODE_ALIASES.get(str(mode_raw).strip().lower()) if mode_raw else None
    )
    return aspect, duration, image_mode


def _resolve_public_video_model(
    model: str, request: Any, model_config: Optional[Dict[str, Any]], images: Any
) -> Optional[str]:
    meta = PUBLIC_VIDEO_BASE_MODELS[model]
    family = meta["family"]
    durations = meta["durations"]
    aspect, duration, image_mode = extract_video_params(request)

    image_count = len(images) if images else 0
    if aspect is None:
        aspect = _infer_aspect_ratio_from_images(images, video_mode=True) if image_count else None
    if aspect not in ("landscape", "portrait"):
        aspect = "landscape"

    if family == "flash":
        # Omni 同一个具体名同时覆盖文生视频与参考图视频（0～3 张），按时长与画幅选名即可
        seconds = _nearest_option(duration or VIDEO_DEFAULT_DURATION, durations)
        resolved = f"omni-1.1-flash-{seconds}s-{aspect}"
    elif image_count == 0:
        seconds = _nearest_option(duration or VIDEO_DEFAULT_DURATION, durations)
        resolved = f"{model}-{seconds}s-{aspect}"
    elif image_mode == "reference" or image_count >= 3:
        ref_family = family if family in R2V_FAMILIES else "fast"
        resolved = f"veo-3.1-{ref_family}-r2v-8s-{aspect}"
    else:
        seconds = _nearest_option(duration or max(I2V_DURATIONS), I2V_DURATIONS)
        resolved = f"veo-3.1-{family}-i2v-{seconds}s-{aspect}"

    if model_config and resolved not in model_config:
        debug_logger.log_warning(
            f"[MODEL_RESOLVER] 视频模型 {model} 解析出的 {resolved} 不在 MODEL_CONFIG 中，回退到原始模型名"
        )
        return None
    debug_logger.log_info(
        f"[MODEL_RESOLVER] 视频模型名转换: {model} → {resolved} "
        f"(aspect={aspect}, duration={duration or 'default'}, images={image_count}, mode={image_mode or 'auto'})"
    )
    return resolved


def get_public_model_catalog(model_config: Dict[str, Any]) -> list:
    """返回对外公开的基础模型（含参数说明）。只包含当前 MODEL_CONFIG 里真实可用且 listed 的模型。"""

    def available(model_id: str) -> bool:
        cfg = model_config.get(model_id)
        return bool(cfg) and cfg.get("listed", True)

    entries: list = []

    for alias, base in IMAGE_BASE_MODELS.items():
        aspects = [a for a in ASPECT_ORDER if available(f"{base}-{a}")]
        if not aspects:
            continue
        sizes = ["1k"] + [
            size
            for size in MODEL_SUPPORTED_SIZES.get(base, [])
            if available(f"{base}-{aspects[0]}-{size}")
        ]
        ratios = [ASPECT_DISPLAY[a] for a in aspects]
        entries.append(
            {
                "id": alias,
                "type": "image",
                "description": (
                    f"Flow image generation. Params: aspectRatio {'|'.join(ratios)} (default 16:9); "
                    f"imageSize {'|'.join(sizes)} (default 1k); OpenAI size/quality are also accepted. "
                    f"Image input: 0-5 images (image-to-image)."
                ),
                "parameters": {
                    "aspectRatio": ratios,
                    "imageSize": sizes,
                    "images": {"min": 0, "max": 5},
                },
            }
        )

    for alias, meta in PUBLIC_VIDEO_BASE_MODELS.items():
        if not available(f"{alias}-{VIDEO_DEFAULT_DURATION}s-landscape"):
            continue
        durations = [d for d in meta["durations"] if available(f"{alias}-{d}s-landscape")]
        parameters: Dict[str, Any] = {
            "aspectRatio": ["16:9", "9:16"],
            "durationSeconds": durations,
            "images": {"min": 0, "max": 3},
        }
        if meta["family"] == "flash":
            note = "Text-to-video, or reference-image video with 1-3 images."
        else:
            parameters["imageMode"] = ["frames", "reference"]
            note = (
                "Text-to-video; with 1-2 images: first/last-frame video (4|6s); "
                "with 3 images or imageMode=reference: reference video (8s, lite/fast only)."
            )
        entries.append(
            {
                "id": alias,
                "type": "video",
                "description": (
                    f"{meta['label']} video generation. Params: aspectRatio 16:9|9:16 (default 16:9); "
                    f"durationSeconds {'|'.join(str(d) for d in durations)} (default {VIDEO_DEFAULT_DURATION}). {note}"
                ),
                "parameters": parameters,
            }
        )
    return entries
