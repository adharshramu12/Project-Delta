"""Render chosen PDF pages to pictures with Windows' own PDF engine (Windows.Data.Pdf, part of Windows 10 and 11),
through Windows PowerShell 5.1 like the OCR rung: nothing is installed. Used only for pages with no text layer
(scans) and for protected PDFs Windows can open, and only for the pages that are needed, so they can be read
with Windows OCR or, when the picture matters, shown to a vision model.

On a PC without it (not Windows, PowerShell blocked, a PDF Windows cannot open) it returns no pictures and the
caller says plainly what could not be read.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from delta.system.logging import log

TIMEOUT_S = 60.0
WIDTH = 1600  # pixels: sharp enough for OCR, small enough to be quick

SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
$asTask = ($methods | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ($methods | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
function AwaitAction($op) { $t = $asTaskAction.Invoke($null, @($op)); $t.Wait(-1) | Out-Null }
[Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFolder, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:DELTA_PDF)) ([Windows.Storage.StorageFile])
    $doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
} catch { '{"error":"cannot open"}'; exit 0 }
$folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($env:DELTA_OUT)) `
                ([Windows.Storage.StorageFolder])
$done = @()
foreach ($n in $env:DELTA_PAGES.Split(',')) {
    $i = [int]$n - 1
    if ($i -lt 0 -or $i -ge $doc.PageCount) { continue }
    $page = $doc.GetPage([uint32]$i)
    $out = Await ($folder.CreateFileAsync("page$n.png", [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) `
                 ([Windows.Storage.StorageFile])
    $stream = Await ($out.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) `
                    ([Windows.Storage.Streams.IRandomAccessStream])
    $opt = New-Object Windows.Data.Pdf.PdfPageRenderOptions
    $opt.DestinationWidth = [uint32]$env:DELTA_WIDTH
    AwaitAction ($page.RenderToStreamAsync($stream, $opt))
    $stream.Dispose(); $page.Dispose()
    $done += [int]$n
}
ConvertTo-Json -InputObject ([pscustomobject]@{ pages = $doc.PageCount; rendered = $done }) -Compress
"""


def available() -> bool:
    return sys.platform == "win32" and shutil.which("powershell.exe") is not None


def parse(raw: str) -> list[int]:
    """Page numbers the script rendered, from its JSON (PowerShell flattens a one-item array to a number)."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return []
    if not isinstance(data, dict) or "error" in data:
        return []
    done = data.get("rendered")
    if isinstance(done, int):
        done = [done]
    return [int(n) for n in done or [] if isinstance(n, int | float)]


async def render(pdf: Path, pages: list[int], out_dir: Path | None = None, width: int = WIDTH) -> dict[int, Path]:
    """{page number: PNG} for the pages Windows could render. {} when not available. The caller deletes the
    pictures (they are temporary files)."""
    if not pages or not available():
        return {}
    from delta.perception.ocr import _powershell

    folder = Path(tempfile.mkdtemp(prefix="delta-pdf-", dir=out_dir))
    env = {"DELTA_PDF": str(pdf.resolve()), "DELTA_OUT": str(folder.resolve()),
           "DELTA_PAGES": ",".join(str(int(p)) for p in pages[:20]), "DELTA_WIDTH": str(int(width))}
    try:
        raw = await _powershell(SCRIPT, env, TIMEOUT_S)
    except Exception as e:  # PowerShell blocked, timed out, WinRT missing
        log.info("winpdf.unavailable", error=str(e)[:200])
        shutil.rmtree(folder, ignore_errors=True)
        return {}
    found = {n: folder / f"page{n}.png" for n in parse(raw) if (folder / f"page{n}.png").is_file()}
    if not found:
        shutil.rmtree(folder, ignore_errors=True)
    return found
