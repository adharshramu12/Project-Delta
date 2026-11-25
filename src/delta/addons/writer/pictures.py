"""Free pictures for decks and chat: photos from Unsplash (with the user's free key) or Openverse (no key), and
3D stickers from Microsoft's Fluent Emoji (MIT).

Every picture keeps its credit. Unsplash's API terms ask for three things, all done here: hotlinked sizes come
from the API's own URLs, the photographer and Unsplash are credited with links, and a download is reported to
the photo's download_location. Openverse returns openly licensed images with a ready attribution line, which is
kept as given.

Safety: only https addresses are fetched; a picture must be a real JPEG or PNG (checked by its first bytes, not
by what the server says) and at most MAX_BYTES; files are written with names made here, never from the web.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import struct
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from delta.system.logging import log

MAX_BYTES = 8_000_000
UNSPLASH = "https://api.unsplash.com"
OPENVERSE = "https://api.openverse.org/v1/images/"
# Fluent Emoji, pinned to a commit so a sticker never changes under the user.
FLUENT = "https://cdn.jsdelivr.net/gh/microsoft/fluentui-emoji@1ffb34c752ecf5d402f04cfb4b392c77f57c54bc/assets"
AGENT = {"User-Agent": "DeltaDesktopAssistant/1.0 (personal desktop app)"}
UTM = "utm_source=delta&utm_medium=referral"


@dataclass
class Picture:
    path: Path
    credit: str  # one line, shown with the picture
    link: str  # where the credit points
    source: str  # unsplash | openverse | fluent-emoji
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        d["name"] = self.path.name
        return d


def image_size(data: bytes) -> tuple[str, int, int] | None:
    """(kind, width, height) for a JPEG or PNG, else None. Read from the bytes, so a mislabelled file is refused."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpeg", w, h
            i += 2 + length
    return None


def _client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers=AGENT)


async def _get_image(client: Any, url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("not an https address")
    data = bytearray()
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("picture too large")
    return bytes(data)


def _name(source: str, key: str, kind: str) -> str:
    return f"{source}-{hashlib.sha256(key.encode()).hexdigest()[:12]}.{'png' if kind == 'png' else 'jpg'}"


async def _save(data: bytes, folder: Path, source: str, key: str) -> tuple[Path, int, int]:
    found = image_size(data)
    if found is None:
        raise ValueError("not a JPEG or PNG picture")
    kind, w, h = found
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _name(source, key, kind)
    await asyncio.to_thread(path.write_bytes, data)
    return path, w, h


async def _unsplash(client: Any, key: str, query: str, count: int, orientation: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "per_page": count, "content_filter": "high"}
    if orientation in ("landscape", "portrait", "squarish"):
        params["orientation"] = orientation
    r = await client.get(f"{UNSPLASH}/search/photos", params=params,
                         headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"})
    r.raise_for_status()
    out = []
    for p in r.json().get("results", [])[:count]:
        user = p.get("user") or {}
        raw = (p.get("urls") or {}).get("raw", "")
        if not raw:
            continue
        sep = "&" if "?" in raw else "?"
        out.append({"id": p.get("id", raw), "url": f"{raw}{sep}w=1920&q=80&fm=jpg&fit=max",
                    "credit": f"Photo by {user.get('name') or 'an Unsplash photographer'} on Unsplash",
                    "link": f"{(user.get('links') or {}).get('html', 'https://unsplash.com')}?{UTM}",
                    "track": (p.get("links") or {}).get("download_location", ""), "source": "unsplash"})
    return out


async def _openverse(client: Any, query: str, count: int, orientation: str) -> list[dict[str, Any]]:
    # Wikimedia's robot policy refuses automated downloads, so its images are left out rather than tried.
    params: dict[str, Any] = {"q": query, "page_size": min(20, count * 3), "license_type": "commercial",
                              "mature": "false", "excluded_source": "wikimedia"}
    if orientation == "landscape":
        params["aspect_ratio"] = "wide"
    elif orientation == "portrait":
        params["aspect_ratio"] = "tall"
    r = await client.get(OPENVERSE, params=params)
    r.raise_for_status()
    out = []
    for p in r.json().get("results", []):
        url = str(p.get("url") or "")
        if not url.startswith("https://") or str(p.get("filetype") or "jpg").lower() not in ("jpg", "jpeg", "png"):
            continue
        credit = str(p.get("attribution") or f"Image by {p.get('creator') or 'unknown'} via Openverse")
        credit = credit.split(" To view a copy of this license")[0]
        out.append({"id": p.get("id", url), "url": url, "credit": credit,
                    "link": str(p.get("foreign_landing_url") or "https://openverse.org"), "track": "",
                    "source": "openverse"})
    return out


async def find_photos(query: str, folder: Path, count: int = 1, orientation: str = "landscape",
                      unsplash_key: str | None = None) -> list[Picture]:
    """Up to `count` photos for `query`, saved in `folder`. Unsplash first when there is a key, then Openverse.
    Returns what could be fetched (maybe nothing); never raises for a network problem."""
    query = " ".join(query.split())[:100]
    if not query:
        return []
    found: list[Picture] = []
    async with _client() as client:
        candidates: list[dict[str, Any]] = []
        if unsplash_key:
            try:
                candidates = await _unsplash(client, unsplash_key, query, count, orientation)
            except Exception as e:
                log.info("pictures.unsplash_failed", error=str(e)[:200])
        if len(candidates) < count:
            try:
                candidates += await _openverse(client, query, count, orientation)
            except Exception as e:
                log.info("pictures.openverse_failed", error=str(e)[:200])
        for c in candidates:
            if len(found) >= count:
                break
            try:
                data = await _get_image(client, c["url"])
                path, w, h = await _save(data, folder, c["source"], str(c["id"]))
            except Exception as e:
                log.info("pictures.download_failed", source=c["source"], error=str(e)[:200])
                continue
            if c["track"] and unsplash_key:  # Unsplash asks for this on every download
                try:
                    await client.get(c["track"], headers={"Authorization": f"Client-ID {unsplash_key}"})
                except Exception:
                    log.info("pictures.track_failed")
            found.append(Picture(path, c["credit"], c["link"], c["source"], w, h))
    return found


def _fluent_paths(emoji: str) -> list[str]:
    """Where Fluent Emoji keeps the 3D picture of one emoji: 'Party popper/3D/party_popper_3d.png' (and the
    'Default' skin tone folder for people)."""
    chars = [c for c in emoji if unicodedata.category(c) != "Mn" and c not in "️‍"]
    if len(chars) != 1:
        return []
    try:
        name = unicodedata.name(chars[0]).lower()
    except ValueError:
        return []
    from urllib.parse import quote

    out = []
    # Unicode says 'thumbs up sign' where Fluent says 'Thumbs up'
    for n in dict.fromkeys([name, re.sub(r" sign$", "", name)]):
        base = f"{FLUENT}/{quote(n[:1].upper() + n[1:])}"
        stem = re.sub(r"[^a-z0-9]+", "_", n).strip("_")
        out += [f"{base}/3D/{stem}_3d.png", f"{base}/Default/3D/{stem}_3d_default.png"]
    return out


async def find_stickers(emojis: str, folder: Path, count: int = 3) -> list[Picture]:
    """3D stickers for the emojis given (e.g. '🎉🚀'), one per emoji, up to `count`."""
    found: list[Picture] = []
    seen: set[str] = set()
    async with _client() as client:
        for ch in emojis:
            if len(found) >= count or ch in seen or unicodedata.category(ch) not in ("So", "Sk"):
                continue
            seen.add(ch)
            for url in _fluent_paths(ch):
                try:
                    data = await _get_image(client, url)
                    path, w, h = await _save(data, folder, "sticker", url)
                except Exception:
                    continue
                found.append(Picture(path, "Fluent Emoji by Microsoft (MIT licence)",
                                     "https://github.com/microsoft/fluentui-emoji", "fluent-emoji", w, h))
                break
    return found
