"""Image preparation for vision calls, tuned for tokens.

Vision APIs bill a picture by its size: OpenAI-style models cut it into 512 px tiles after fitting it in
2048 px and scaling the short side to 768 px. A 1920x1080 screenshot sent as is costs about 1,100 tokens;
the same picture at 1280 px wide costs the same tiles, while 1024 px wide costs about 765. Marks are drawn
after downscaling, so their numbers stay readable at the size the model sees.

Everything here is Pillow, which Delta already depends on. No new dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
from typing import Any

MAX_SIDE = 1280      # the longest side of a marked picture
STEP_SIDE = 1024     # a quick look during a step
JPEG_QUALITY = 60    # flat UI colours compress well; numbers stay crisp at this quality
BASE_TOKENS = 85     # OpenAI's flat cost per picture
TILE_TOKENS = 170    # and per 512 px tile


def fit(size: tuple[int, int], max_side: int) -> tuple[int, int]:
    """The size after shrinking so the longest side is at most max_side (never enlarged)."""
    w, h = size
    longest = max(w, h)
    if longest <= max_side or longest <= 0:
        return w, h
    scale = max_side / longest
    return max(1, round(w * scale)), max(1, round(h * scale))


def image_tokens(width: int, height: int) -> int:
    """Estimated input tokens for one picture at this size (OpenAI high-detail rule; other providers are
    close enough for a meter, and the estimate is always labelled as one)."""
    if width <= 0 or height <= 0:
        return 0
    w, h = float(width), float(height)
    if max(w, h) > 2048:
        s = 2048 / max(w, h)
        w, h = w * s, h * s
    if min(w, h) > 768:
        s = 768 / min(w, h)
        w, h = w * s, h * s
    tiles = math.ceil(w / 512) * math.ceil(h / 512)
    return BASE_TOKENS + TILE_TOKENS * tiles


def downscale(img: Any, max_side: int = MAX_SIDE) -> Any:
    """A copy no larger than max_side on its longest side, in RGB."""
    from PIL import Image

    out = img.convert("RGB") if img.mode != "RGB" else img
    size = fit(out.size, max_side)
    if size != out.size:
        out = out.resize(size, Image.Resampling.LANCZOS)
    return out


def encode_jpeg(img: Any, quality: int = JPEG_QUALITY) -> str:
    """Base64 JPEG of an RGB picture."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(20, min(95, int(quality))), optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def digest(img: Any, size: int = 64) -> str:
    """A fingerprint of what a picture shows: a small greyscale thumbnail, quantised, then hashed. Blinking
    cursors and anti-aliasing noise do not change it; a new dialog, page or text does."""
    from PIL import Image

    thumb = img.convert("L").resize((size, max(1, round(size * img.height / max(1, img.width)))),
                                    Image.Resampling.BILINEAR)
    data = bytes(v >> 3 for v in thumb.tobytes())  # 32 grey levels
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()
