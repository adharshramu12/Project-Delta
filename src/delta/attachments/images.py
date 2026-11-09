"""Pictures: opened safely, read with OCR when possible, and made small before any model sees them.

Opening reads only the header first: a picture whose pixel count is absurd (a decompression bomb) is refused
before it is decoded. Phone photos are turned upright (EXIF orientation). Transparent pictures (PNG, WebP, GIF
with a transparent colour) are put on white: converting them straight to RGB turned every transparent pixel black,
so a screenshot with a transparent background reached the model as a black square. What goes to a model is a
re-encoded JPEG no larger than image_max_side on its longest side and never more than MAX_B64_BYTES; in low-detail
mode it is 512 px and, where the provider supports it (OpenAI), the request asks for low detail, which costs a flat
85 tokens.

iPhone HEIC/HEIF photos open when the optional pillow-heif package is installed (`uv pip install pillow-heif`);
without it the user is told plainly how to send the photo instead.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import io
from pathlib import Path
from typing import Any

from delta.attachments.kinds import AttachmentError
from delta.perception.images import fit, image_tokens

MAX_PIXELS = 60_000_000  # about a 60 megapixel photo; a bigger one is refused before decoding
LOW_SIDE = 512
LOW_TOKENS = 85  # OpenAI's flat cost for a low-detail picture
# The most a picture may weigh once encoded (base64). Groq refuses base64 images over 4 MB and OpenAI's limit is
# higher; at 1024 px a JPEG is usually 60-250 KB, so this only bites on very noisy pictures or a big max side.
MAX_B64_BYTES = 3_500_000
BACKGROUND = (255, 255, 255)


@functools.cache
def heif_support() -> bool:
    """True when pillow-heif is installed: it teaches Pillow to open iPhone HEIC/HEIF photos."""
    try:
        import pillow_heif
    except Exception:
        return False
    with contextlib.suppress(Exception):
        pillow_heif.register_heif_opener()
        return True
    return False


def to_rgb(img: Any) -> Any:
    """An RGB copy, with any transparency put on white (not black) and 16-bit or CMYK pictures handled."""
    from PIL import Image

    if img.mode == "RGB":
        return img
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        # 16/32-bit greyscale: scale to 8 bits instead of clipping everything above 255 to white
        wide = img.convert("I") if img.mode != "F" else img
        top = (wide.getextrema() or (0, 255))[1] or 255
        scale = 255 / top if top > 255 else 1
        img = wide.point(lambda v: v * scale).convert("L")
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, BACKGROUND)
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def open_image(path: Path) -> Any:
    """A decoded, upright RGB picture. AttachmentError (plain words) for damaged or oversized pictures."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    heif_support()  # registers the HEIC opener when pillow-heif is there
    try:
        img = Image.open(path)
    except Image.DecompressionBombError as e:
        raise AttachmentError("That picture is far too large to open safely.") from e
    except (UnidentifiedImageError, OSError) as e:
        raise AttachmentError("That picture could not be opened. It may be damaged or in a format Delta can't "
                              "read (JPG, PNG, WebP and GIF work).") from e
    w, h = img.size
    if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
        img.close()
        raise AttachmentError(f"That picture is {w}x{h} pixels, too large to open safely. Send a smaller copy.")
    try:
        img.seek(0)  # animated GIF / WebP: the first frame
        img.load()
        upright = ImageOps.exif_transpose(img) or img
        return to_rgb(upright)
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise AttachmentError("That picture could not be read. It may be damaged.") from e


def describe_size(img: Any) -> str:
    return f"{img.width}x{img.height}"


def prepare_for_model(img: Any, max_side: int, low: bool) -> tuple[str, int, int]:
    """(base64 JPEG, width, height) of the picture as it will be sent: no bigger than max_side on its longest
    side, and smaller (lower quality, then fewer pixels) until it fits in MAX_B64_BYTES."""
    from PIL import Image

    side = min(max_side, LOW_SIDE) if low else max_side
    img = to_rgb(img)
    size = fit(img.size, side)
    small = img.resize(size, Image.Resampling.LANCZOS) if size != img.size else img
    quality = 65 if low else 72
    while True:
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= MAX_B64_BYTES or max(small.size) <= 256:
            return b64, small.width, small.height
        if quality > 45:
            quality -= 15
            continue
        small = small.resize(fit(small.size, int(max(small.size) * 0.75)), Image.Resampling.LANCZOS)


def tokens_for(width: int, height: int, low_detail: bool) -> int:
    return LOW_TOKENS if low_detail else image_tokens(width, height)


def full_size_tokens(img: Any) -> int:
    """What the picture would have cost sent as it is (for the meter's before / after line)."""
    return int(image_tokens(img.width, img.height))
