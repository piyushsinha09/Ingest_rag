"""
src/common.py
===============
Shared, cross-cutting pieces used by both `services.py` and `routes.py`:

  * get_logger()   -- one logger configuration for the whole app
  * exceptions     -- typed hierarchy mapping failures to HTTP status codes
  * schemas        -- pydantic response models

Merged into one file because each piece is small (tens of lines) and none
of them contain business logic -- they're contracts/utilities other modules
depend on, not behavior in their own right.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger("ingestion_universal")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(f"ingestion_universal.{name}")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class IngestionAppError(Exception):
    """Base class for all app-raised errors. Not raised directly."""
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None,
                 status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def to_dict(self) -> dict:
        return {"error": self.code, "detail": self.message}


class UnsupportedFileTypeError(IngestionAppError):
    status_code = 415
    code = "unsupported_file_type"


class FileTooLargeError(IngestionAppError):
    status_code = 413
    code = "file_too_large"


class ConversionFailedError(IngestionAppError):
    """Raised when converting an uploaded file to PDF fails (LibreOffice,
    image conversion, etc.)."""
    status_code = 422
    code = "conversion_failed"


class ParsingFailedError(IngestionAppError):
    """Raised when the parser (opendataloader / OCR splice / chunking) fails."""
    status_code = 500
    code = "parsing_failed"


class OCREngineUnavailableError(IngestionAppError):
    """Raised when a requested OCR engine (e.g. LightOnOCR-2-1B) isn't
    installed and there is no safe fallback available for the caller's use case."""
    status_code = 503
    code = "ocr_engine_unavailable"


class DocumentNotFoundError(IngestionAppError):
    status_code = 404
    code = "document_not_found"


class AlreadyEnhancedError(IngestionAppError):
    """Raised when the user asks to enhance a document that is already in
    Enhanced Mode (all relevant pages already used LightOnOCR-2-1B)."""
    status_code = 409
    code = "already_enhanced"


class ExportFailedError(IngestionAppError):
    status_code = 500
    code = "export_failed"


# --------------------------------------------------------------------------- #
# Pydantic response models
# --------------------------------------------------------------------------- #
class ParseSummary(BaseModel):
    engine: Optional[str] = None
    page_classification: Optional[str] = None
    ocr: Optional[str] = None
    pages_scanned: Optional[int] = None
    scanned_page_numbers: list[int] = Field(default_factory=list)


class JobSummary(BaseModel):
    job_id: str
    doc_stem: str
    source_file: str
    original_kind: Optional[str] = None
    converted: bool = False
    doc_id: Optional[str] = None
    num_chunks: Optional[int] = None
    file_size_bytes: Optional[int] = None
    engine: Optional[str] = None
    mode: str = "normal"                 # "normal" | "enhanced"
    can_enhance: bool = False            # whether the "Enhanced Mode" action is available
    page_classification: Optional[str] = None
    scanned_page_numbers: list[int] = Field(default_factory=list)
    pending_enhance_pages: list[int] = Field(default_factory=list)
    page_engines: dict[str, str] = Field(default_factory=dict)  # {"1": "tesseract", "2": "lighton"}


class IngestResponse(BaseModel):
    base_url: str
    summary: JobSummary
    data: dict[str, Any]


class EnhanceResponse(BaseModel):
    base_url: str
    summary: JobSummary
    data: dict[str, Any]


class ErrorResponse(BaseModel):
    error: str
    detail: str


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    lighton_available: bool
    tesseract_available: bool
