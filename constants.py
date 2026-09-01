"""
src/constants.py
=================
Small, stable vocabulary shared by the parser, services, routes, and (via the
JSON API) the frontend. Keeping these as named constants instead of magic
strings scattered around avoids typo bugs like "tesseract" vs "Tesseract".
"""
from __future__ import annotations

# ---- OCR engines -------------------------------------------------------
ENGINE_NATIVE = "native"          # digital text layer, no OCR needed
ENGINE_TESSERACT = "tesseract"
ENGINE_EASYOCR = "easyocr"
ENGINE_LIGHTON = "lighton"        # LightOnOCR-2-1B (handwriting-capable VLM OCR)

OCR_ENGINES = (ENGINE_TESSERACT, ENGINE_EASYOCR, ENGINE_LIGHTON)

# ---- OCR modes -----------------------------------------------------------
OCR_MODE_OFF = "off"
OCR_MODE_AUTO = "auto"
OCR_MODE_FORCE = "force"

# ---- handwriting escalation modes ---------------------------------------
HANDWRITING_OFF = "off"
HANDWRITING_AUTO = "auto"
HANDWRITING_FORCE = "force"

# ---- document processing "mode" shown in the UI -------------------------
# NORMAL  = produced with Tesseract (or native text, no OCR at all)
# ENHANCED = at least one page was produced with LightOnOCR-2-1B
MODE_NORMAL = "normal"
MODE_ENHANCED = "enhanced"


def mode_from_engine(engine: str | None) -> str:
    """Classify a `parse.engine` string (e.g. 'builtin-ocr:tesseract',
    'builtin-ocr:lighton:force', 'native') into the UI-facing mode."""
    if not engine:
        return MODE_NORMAL
    return MODE_ENHANCED if ENGINE_LIGHTON in engine else MODE_NORMAL


# ---- job/file layout ------------------------------------------------------
UPLOAD_SUBDIR = "_upload"
CONVERTED_SUBDIR = "_converted"
RAW_SUBDIR = "_raw"
IMAGES_SUBDIR = "images"

# ---- misc -----------------------------------------------------------------
SUPPORTED_KINDS = ("pdf", "office", "image")
