"""
src/converters/convert_to_pdf.py
=================================
Normalize any supported input file to a PDF so the rest of the pipeline only
ever has to deal with one input type.

  * Word / PowerPoint / Excel / OpenDocument / RTF  -> PDF   (via LibreOffice)
  * Images (png/jpg/tiff/bmp/gif/webp)              -> image-only "scanned" PDF
  * PDF                                             -> returned as-is (copied)

Images become an image-only PDF ON PURPOSE: it has no text layer, so the
parser classifies it as "scanned" and routes it through OCR.

System requirements:
    LibreOffice (`soffice` on PATH) for Office formats.
    `pip install pillow img2pdf`
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from src.common import ConversionFailedError, UnsupportedFileTypeError, get_logger

log = get_logger("converters.convert_to_pdf")

OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".txt",
              ".ppt", ".pptx", ".odp",
              ".xls", ".xlsx", ".ods", ".csv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff",
             ".bmp", ".gif", ".webp"}

# Kept for backwards-compatible imports; prefer src.common.ConversionFailedError.
ConversionError = ConversionFailedError


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE_EXT:
        return "office"
    if ext in IMAGE_EXT:
        return "image"
    return "unknown"


def _soffice_bin() -> str:
    for name in ("soffice", "libreoffice"):
        if shutil.which(name):
            return name
    raise ConversionFailedError(
        "LibreOffice not found on this server. Install it "
        "(e.g. `sudo apt-get install -y libreoffice`) to convert "
        "Word/PowerPoint/Excel files.", code="libreoffice_missing")


def office_to_pdf(src: Path, out_dir: Path, timeout: int = 180) -> Path:
    """Convert an Office/OpenDocument file to PDF with headless LibreOffice.
    A unique UserInstallation profile is used so concurrent conversions
    don't clash on the same lock file."""
    soffice = _soffice_bin()
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.gettempdir()) / f"lo_{uuid.uuid4().hex}"
    cmd = [soffice, "--headless", "--nologo", "--nofirststartwizard",
           f"-env:UserInstallation=file://{profile}",
           "--convert-to", "pdf", "--outdir", str(out_dir), str(src)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ConversionFailedError(
            f"LibreOffice timed out converting {src.name} "
            f"(> {timeout}s).", code="libreoffice_timeout") from e
    except OSError as e:
        raise ConversionFailedError(
            f"Could not launch LibreOffice: {e}", code="libreoffice_launch_failed") from e
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    produced = out_dir / f"{src.stem}.pdf"
    if produced.exists():
        return produced
    raise ConversionFailedError(
        f"LibreOffice did not produce a PDF for {src.name}.\n"
        f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}",
        code="libreoffice_no_output")


def image_to_pdf(src: Path, out_dir: Path) -> Path:
    """Wrap an image (or multi-frame TIFF/GIF) into an image-only PDF.
    Tries img2pdf (lossless, correct DPI-based page size); falls back to
    Pillow if img2pdf can't handle the file (e.g. has an alpha channel it
    dislikes, or an unusual mode)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}.pdf"

    try:
        import img2pdf
        with open(out, "wb") as f:
            f.write(img2pdf.convert(str(src)))
        return out
    except Exception as e:  # noqa: BLE001 - deliberately broad; img2pdf raises many types
        log.warning("img2pdf failed for %s (%s); falling back to Pillow", src.name, e)

    try:
        from PIL import Image, ImageSequence
        im = Image.open(src)
        frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)]
        if not frames:
            raise ConversionFailedError(
                f"No image frames found in {src.name}", code="empty_image")
        dpi = im.info.get("dpi", (150, 150))
        frames[0].save(out, "PDF", save_all=len(frames) > 1,
                       append_images=frames[1:], resolution=float(dpi[0] or 150))
        return out
    except ConversionFailedError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConversionFailedError(
            f"Could not convert image {src.name} to PDF: {e}",
            code="image_conversion_failed") from e


def convert_to_pdf(src: Path, out_dir: Path) -> Path:
    """Return a PDF path for `src`. PDFs pass through (copied into out_dir)."""
    src = Path(src)
    if not src.exists():
        raise ConversionFailedError(f"File not found: {src}", code="file_not_found")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    k = kind_of(src)
    if k == "pdf":
        dst = out_dir / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return dst
    if k == "office":
        return office_to_pdf(src, out_dir)
    if k == "image":
        return image_to_pdf(src, out_dir)

    raise UnsupportedFileTypeError(
        f"Unsupported file type '{src.suffix}'. Supported: PDF, "
        f"{', '.join(sorted(OFFICE_EXT))}, {', '.join(sorted(IMAGE_EXT))}")
