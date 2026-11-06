"""Windows' built-in OCR (Windows.Media.Ocr), for apps with no accessibility tree.

Some apps draw their own controls (games, remote desktops, Electron or Java apps with accessibility off):
UI Automation sees one blank pane. Windows 10 and 11 ship an OCR engine for the languages installed on the
PC; Windows PowerShell 5.1 can call it through WinRT, so nothing is installed. The text comes back with a
box per line and per word, which Delta turns into text elements: a cheap model that cannot see pictures
can still act on them (click_text), and set-of-marks uses them as marks with no handle.

On a PC without it (not Windows, no OCR language, PowerShell blocked) every call is a no-op that says so.
The probe runs once, on first use, never at start-up.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delta.system import proctree
from delta.system.logging import log

TIMEOUT_S = 25.0
MAX_SIDE = 4000  # OcrEngine.MaxImageDimension is larger; this keeps a 4K screen readable and fast

_PRELUDE = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
"""

PROBE_SCRIPT = _PRELUDE + r"""
if ($null -eq $engine) { 'no' } else { 'yes ' + $engine.RecognizerLanguage.LanguageTag }
"""

READ_SCRIPT = _PRELUDE + r"""
if ($null -eq $engine) { '{"error":"no OCR language installed"}'; exit 0 }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:DELTA_OCR_IMAGE)) `
              ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) `
                ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) `
                 ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()
$lines = @(foreach ($l in $result.Lines) {
    $words = @(foreach ($w in $l.Words) { $r = $w.BoundingRect
        [pscustomobject]@{ t = $w.Text; x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height } })
    [pscustomobject]@{ text = $l.Text; words = $words }
})
ConvertTo-Json -InputObject ([pscustomobject]@{ lines = $lines }) -Depth 5 -Compress
"""


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: tuple[int, int, int, int]  # x, y, width, height in the picture's pixels
    words: tuple[tuple[str, tuple[int, int, int, int]], ...] = ()


def parse(raw: str) -> list[OcrLine]:
    """OcrLine list from the script's JSON (lines with words and their boxes)."""
    try:
        data: Any = json.loads(raw or "{}")
    except ValueError:
        return []
    out: list[OcrLine] = []
    for line in (data.get("lines") or []) if isinstance(data, dict) else []:
        words_raw = line.get("words") or []
        if isinstance(words_raw, dict):  # PowerShell 5 flattens one-item arrays in some cases
            words_raw = [words_raw]
        words: list[tuple[str, tuple[int, int, int, int]]] = []
        for w in words_raw:
            try:
                words.append((str(w["t"]), (int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"]))))
            except (KeyError, TypeError, ValueError):
                continue
        if not words:
            continue
        x1 = min(b[0] for _, b in words)
        y1 = min(b[1] for _, b in words)
        x2 = max(b[0] + b[2] for _, b in words)
        y2 = max(b[1] + b[3] for _, b in words)
        text = str(line.get("text") or " ".join(t for t, _ in words)).strip()
        if text:
            out.append(OcrLine(text, (x1, y1, x2 - x1, y2 - y1), tuple(words)))
    return out


class WindowsOcr:
    """The OCR engine, probed once. `mode`: auto (use it when Windows has it) or off."""

    def __init__(self, mode: str = "auto", tmp_dir: Path | None = None) -> None:
        self.mode = mode
        self.tmp_dir = tmp_dir
        self._available: bool | None = None
        self.language = ""
        self.calls = 0

    def platform_ok(self) -> bool:
        return self.mode != "off" and sys.platform == "win32" and shutil.which("powershell.exe") is not None

    def available(self) -> bool:
        """Known to work (probed). Before the first probe: whether it can be tried at all on this PC."""
        if self._available is not None:
            return self._available
        return self.platform_ok()

    async def probe(self) -> bool:
        if self._available is not None:
            return self._available
        if not self.platform_ok():
            self._available = False
            return False
        try:
            out = await _powershell(PROBE_SCRIPT, {}, TIMEOUT_S)
        except Exception as e:  # PowerShell blocked by policy, timed out, missing WinRT
            log.info("ocr.unavailable", error=str(e)[:200])
            out = ""
        self._available = out.startswith("yes")
        self.language = out[4:].strip() if self._available else ""
        log.info("ocr.probe", available=self._available, language=self.language)
        return self._available

    async def read(self, image: Any) -> list[OcrLine]:
        """Text lines (with boxes in `image` pixels) of a PIL picture. [] when OCR is not available."""
        if not await self.probe():
            return []
        from PIL import Image

        scale = 1.0
        img = image.convert("RGB")
        if max(img.size) > MAX_SIDE:
            scale = max(img.size) / MAX_SIDE
            img = img.resize((round(img.width / scale), round(img.height / scale)), Image.Resampling.LANCZOS)
        fd, name = tempfile.mkstemp(prefix="delta-ocr-", suffix=".png", dir=self.tmp_dir)
        os.close(fd)
        path = Path(name)
        try:
            await asyncio.to_thread(img.save, path, "PNG")
            self.calls += 1
            raw = await _powershell(READ_SCRIPT, {"DELTA_OCR_IMAGE": str(path.resolve())}, TIMEOUT_S)
        finally:
            path.unlink(missing_ok=True)  # the picture never stays on disk
        if '"error"' in raw[:40]:
            return []
        lines = parse(raw)
        if scale != 1.0:
            lines = [_scaled(line, scale) for line in lines]
        return lines


def _scaled(line: OcrLine, s: float) -> OcrLine:
    def sb(b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return round(b[0] * s), round(b[1] * s), round(b[2] * s), round(b[3] * s)

    return OcrLine(line.text, sb(line.box), tuple((t, sb(b)) for t, b in line.words))


async def _powershell(script: str, env: dict[str, str], timeout_s: float) -> str:
    # -EncodedCommand (UTF-16LE base64): PowerShell drops multi-line scripts piped to stdin.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    proc = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, **env},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proctree.track(getattr(proc, "pid", None))  # ended with its children if the watchdog stops this call
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        raise
    return out.decode("utf-8", "replace").strip()


def find_text(lines: list[OcrLine], want: str) -> tuple[str, tuple[int, int, int, int]] | None:
    """The best box for `want`: an exact line, then a run of words in one line, then a line containing it.
    Case-insensitive; None when nothing matches (never a fuzzy guess)."""
    target = " ".join(want.lower().split())
    if not target:
        return None
    for line in lines:
        if " ".join(line.text.lower().split()) == target:
            return line.text, line.box
    n = len(target.split())
    for line in lines:
        words = list(line.words)
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            if " ".join(t.lower() for t, _ in run) == target:
                x1 = min(b[0] for _, b in run)
                y1 = min(b[1] for _, b in run)
                x2 = max(b[0] + b[2] for _, b in run)
                y2 = max(b[1] + b[3] for _, b in run)
                return " ".join(t for t, _ in run), (x1, y1, x2 - x1, y2 - y1)
    for line in lines:
        if target in line.text.lower():
            return line.text, line.box
    return None
