"""Pictures: opened safely, read with OCR when possible, and made small before any model sees them.

Opening reads only the header first: a picture whose pixel count is absurd (a decompression bomb) is refused
before it is decoded. Phone photos are turned upright (EXIF orientation). What goes to a model is a re-encoded
JPEG no larger than image_max_side on its longest side; in low-detail mode it is 512 px and, where the provider
supports it (OpenAI), the request asks for low detail, which costs a flat 85 tokens.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from delta.attachments.kinds import AttachmentError
from delta.perception.images import fit, image_tokens

MAX_PIXELS = 60_000_000  # about a 60 megapixel photo; a bigger one is refused before decoding
LOW_SIDE = 512
LOW_TOKENS = 85  # OpenAI's flat cost for a low-detail picture


def open_image(path: Path) -> Any:
    """A decoded, upright RGB picture. AttachmentError (plain words) for damaged or oversized pictures."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(path)
    except (UnidentifiedImageError, OSError) as e:
        raise AttachmentError("That picture could not be opened. It may be damaged or in a format Delta can't "
                              "read (JPG, PNG, WebP and GIF work).") from e
    except Image.DecompressionBombError as e:
        raise AttachmentError("That picture is far too large to open safely.") from e
    w, h = img.size
    if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
        img.close()
        raise AttachmentError(f"That picture is {w}x{h} pixels, too large to open safely. Send a smaller copy.")
    try:
        img.seek(0)  # animated GIF / WebP: the first frame
        img.load()
        upright = ImageOps.exif_transpose(img) or img
        return upright.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise AttachmentError("That picture could not be read. It may be damaged.") from e


def describe_size(img: Any) -> str:
    return f"{img.width}x{img.height}"


def prepare_for_model(img: Any, max_side: int, low: bool) -> tuple[str, int, int]:
    """(base64 JPEG, width, height) of the picture as it will be sent."""
    from PIL import Image

    side = min(max_side, LOW_SIDE) if low else max_side
    size = fit(img.size, side)
    small = img.resize(size, Image.Resampling.LANCZOS) if size != img.size else img
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=72 if not low else 65, optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), small.width, small.height


def tokens_for(width: int, height: int, low_detail: bool) -> int:
    return LOW_TOKENS if low_detail else image_tokens(width, height)


def full_size_tokens(img: Any) -> int:
    """What the picture would have cost sent as it is (for the meter's before / after line)."""
    return int(image_tokens(img.width, img.height))
