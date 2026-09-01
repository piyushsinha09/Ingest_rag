"""
src/routes.py
===============
All HTTP endpoints. Each is a thin wrapper that parses the request, calls
into services.py, and shapes the response -- no business logic lives here.
Merged into one file (was 5 files under routes/) since each group is only
20-80 lines.

    GET  /                     -- pages_router    (SPA shell)
    POST /api/ingest           -- ingest_router   (upload + convert + parse)
    POST /api/enhance/{job_id} -- enhance_router  ("Enhanced Mode" button)
    GET  /api/export/{job_id}  -- export_router   (download chunks+images zip)
    GET  /api/health           -- health_router   (OCR engine availability)
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import FileResponse

from config import settings
from constants import UPLOAD_SUBDIR
from src.common import (
    EnhanceResponse,
    FileTooLargeError,
    HealthResponse,
    IngestResponse,
    JobSummary,
    UnsupportedFileTypeError,
    get_logger,
)
from src.services import build_export_zip, enhance_document, ingest_file

log = get_logger("routes")


# --------------------------------------------------------------------------- #
# GET / -- SPA shell
# --------------------------------------------------------------------------- #
pages_router = APIRouter(tags=["pages"])


@pages_router.get("/")
def index() -> FileResponse:
    return FileResponse(str(settings.TEMPLATES_DIR / "index.html"))


# --------------------------------------------------------------------------- #
# POST /api/ingest -- upload a file, convert + parse it, return chunks JSON
# --------------------------------------------------------------------------- #
ingest_router = APIRouter(prefix="/api", tags=["ingest"])


def _write_progress(job_dir_path: Path, active_step: int, status: str = "processing") -> None:
    path = job_dir_path / "_progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"active_step": active_step, "status": status}), encoding="utf-8")


@ingest_router.get("/ingest-status/{job_id}")
def api_ingest_status(job_id: str) -> dict:
    if len(job_id) != settings.JOB_ID_LENGTH or any(c not in "0123456789abcdef" for c in job_id):
        return {"active_step": 0, "status": "unknown"}
    path = settings.DATA_DIR / job_id / "_progress.json"
    if not path.exists():
        return {"active_step": 0, "status": "starting"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_step": 0, "status": "starting"}


@ingest_router.post("/ingest", response_model=IngestResponse)
def api_ingest(file: UploadFile = File(...), client_job_id: str | None = Form(None)) -> IngestResponse:
    name = Path(file.filename or "upload").name
    ext = Path(name).suffix.lower()
    if ext and ext not in settings.ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"'{ext}' is not a supported file type. Supported: "
            f"{', '.join(sorted(settings.ALLOWED_EXTENSIONS))}")

    valid_client_id = (client_job_id is not None
                       and len(client_job_id) == settings.JOB_ID_LENGTH
                       and all(c in "0123456789abcdef" for c in client_job_id))
    job_id = client_job_id if valid_client_id else uuid.uuid4().hex[: settings.JOB_ID_LENGTH]
    job_dir_path = settings.DATA_DIR / job_id
    _write_progress(job_dir_path, 0)
    up_dir = job_dir_path / UPLOAD_SUBDIR
    up_dir.mkdir(parents=True, exist_ok=True)
    saved = up_dir / name

    size = 0
    with open(saved, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.MAX_UPLOAD_BYTES:
                f.close()
                saved.unlink(missing_ok=True)
                raise FileTooLargeError(
                    f"File exceeds the {settings.MAX_UPLOAD_BYTES // (1024*1024)} MB limit.")
            f.write(chunk)

    _write_progress(job_dir_path, 1)

    log.info("Ingest job=%s file=%s (%d bytes)", job_id, name, size)
    # handwriting="auto": low-confidence Tesseract pages (handwriting, poor
    # scans) are escalated to LightOnOCR-2-1B automatically, in-line, as part
    # of this single ingest pass -- the result is what gets returned, so
    # there's no separate "rerun" the user sees. The explicit Enhanced Mode
    # button (enhance_document() in services.py) remains available to force a
    # full LightOnOCR-2-1B pass afterward if the auto result still isn't good
    # enough.
    summary = ingest_file(
        saved, job_dir_path, render_pages=True, handwriting="auto",
        progress=lambda step: _write_progress(job_dir_path, step),
    )

    stem = Path(name).stem
    base_url = f"/files/{job_id}/{stem}"
    data = json.loads(Path(summary["json_path"]).read_text(encoding="utf-8"))

    job_summary = JobSummary(
        job_id=job_id,
        doc_stem=stem,
        source_file=summary["source_file"],
        original_kind=summary["original_kind"],
        converted=summary["converted"],
        doc_id=summary["doc_id"],
        num_chunks=summary["num_chunks"],
        file_size_bytes=summary.get("file_size_bytes"),
        engine=summary["engine"],
        mode=summary["mode"],
        can_enhance=summary["can_enhance"],
        page_classification=summary["page_classification"],
        scanned_page_numbers=summary.get("scanned_page_numbers", []),
        pending_enhance_pages=summary.get("pending_enhance_pages", []),
        page_engines=summary.get("page_engines", {}),
    )
    response = IngestResponse(base_url=base_url, summary=job_summary, data=data)
    _write_progress(job_dir_path, 4, "complete")
    return response


# --------------------------------------------------------------------------- #
# POST /api/enhance/{job_id} -- user-triggered "Enhanced Mode" button
# --------------------------------------------------------------------------- #
enhance_router = APIRouter(prefix="/api", tags=["enhance"])


@enhance_router.post("/enhance/{job_id}", response_model=EnhanceResponse)
def api_enhance(job_id: str, pages: list[int] | None = Body(default=None, embed=True)) -> EnhanceResponse:
    # `pages`: optional list of 1-based page numbers to enhance (e.g. the
    # page(s) the user clicked in the UI). Omit it (or POST no body) to fall
    # back to "every scanned page not yet on LightOnOCR" -- still page-scoped,
    # never a full whole-document re-run.
    log.info("Enhance requested for job=%s pages=%s", job_id, pages)
    summary = enhance_document(job_id, pages=pages)

    stem = Path(summary["doc_dir"]).name
    base_url = f"/files/{job_id}/{stem}"
    data = json.loads(Path(summary["json_path"]).read_text(encoding="utf-8"))

    job_summary = JobSummary(
        job_id=job_id,
        doc_stem=stem,
        source_file=summary["source_file"],
        original_kind=summary["original_kind"],
        converted=summary["converted"],
        doc_id=summary["doc_id"],
        num_chunks=summary["num_chunks"],
        file_size_bytes=summary.get("file_size_bytes"),
        engine=summary["engine"],
        mode=summary["mode"],
        can_enhance=summary["can_enhance"],
        page_classification=summary["page_classification"],
        scanned_page_numbers=summary.get("scanned_page_numbers", []),
        pending_enhance_pages=summary.get("pending_enhance_pages", []),
        page_engines=summary.get("page_engines", {}),
    )
    return EnhanceResponse(base_url=base_url, summary=job_summary, data=data)


# --------------------------------------------------------------------------- #
# GET /api/export/{job_id} -- download a .zip of <doc>.chunks.json + images/
# --------------------------------------------------------------------------- #
export_router = APIRouter(prefix="/api", tags=["export"])


@export_router.get("/export/{job_id}")
def api_export(job_id: str) -> FileResponse:
    zip_path = build_export_zip(job_id)
    log.info("Serving export zip for job=%s -> %s", job_id, zip_path.name)
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=zip_path.name,
    )


# --------------------------------------------------------------------------- #
# GET /api/health -- reports whether optional OCR engines are available
# --------------------------------------------------------------------------- #
health_router = APIRouter(prefix="/api", tags=["health"])


def _lighton_available() -> bool:
    try:
        import torch  # noqa: F401
        from transformers import LightOnOcrForConditionalGeneration  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


@health_router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
        lighton_available=_lighton_available(),
        tesseract_available=_tesseract_available(),
    )
