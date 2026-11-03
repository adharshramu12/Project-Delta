"""Draws Delta's app icon (terracotta tile, white delta) into src/delta/api/static/delta.ico and .png."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "delta" / "api" / "static"


def draw(size: int = 256) -> Image.Image:
    s = size * 4  # draw large, shrink for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.23), fill=(201, 100, 66, 255))
    m, w = s * 0.24, s * 0.085
    top, left, right = (s / 2, m * 0.95), (m, s - m * 1.05), (s - m, s - m * 1.05)
    d.polygon([top, right, left], fill=(255, 255, 255, 255))
    inset = w * 1.9  # hollow centre leaves a bold outlined delta
    d.polygon([(s / 2, top[1] + inset * 1.15), (right[0] - inset, right[1] - w),
               (left[0] + inset, left[1] - w)], fill=(201, 100, 66, 255))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    icon = draw()
    icon.save(OUT / "delta.png")
    icon.save(OUT / "delta.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "delta.ico")
