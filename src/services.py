"""
src/services.py
=================
The orchestration layer between routes/cli.py and the core engine
(convert_to_pdf.py + parse_manual.py). Framework-agnostic (no FastAPI
types), so it's callable from routes.py, cli.py, or a future background
worker without changes.

Four responsibilities, merged into one file because they're all small and
tightly related (enhance/export both depend on job path resolution;
ingestion is the thing enhance wraps):

  * job_dir / resolve_doc_dir / ...  -- job/document path resolution
  * ingest_file                      -- convert + parse a single upload
  * enhance_document                 -- force a LightOnOCR-2-1B re-run
  * build_export_zip                 -- zip <doc>.chunks.json + images/
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Callable

from config import settings
from constants import CONVERTED_SUBDIR, IMAGES_SUBDIR, MODE_ENHANCED, \
    UPLOAD_SUBDIR, mode_from_engine
from src.common import (
    AlreadyEnhancedError,
    ConversionFailedError,
    DocumentNotFoundError,
    ExportFailedError,
    OCREngineUnavailableError,
    ParsingFailedError,
    UnsupportedFileTypeError,
    get_logger,
)
from src.convert_to_pdf import convert_to_pdf, kind_of
from src.parse_manual import make_opts, parse_one

log = get_logger("services")

_EXCLUDED_DIR_NAMES = {"_converted", "_upload", "_raw", "_enhanced"}


# --------------------------------------------------------------------------- #
# Job / document path resolution
# --------------------------------------------------------------------------- #
def job_dir(job_id: str) -> Path:
    return settings.DATA_DIR / job_id


def upload_dir(job_id: str) -> Path:
    return job_dir(job_id) / UPLOAD_SUBDIR


def resolve_doc_dir(job_id: str) -> Path:
    """Find the single document output directory inside a job (everything
    that isn't the `_upload` staging folder)."""
    jd = job_dir(job_id)
    if not jd.is_dir():
        raise DocumentNotFoundError(f"No job found with id '{job_id}'.")
    candidates = [p for p in jd.iterdir() if p.is_dir() and p.name != UPLOAD_SUBDIR]
    if not candidates:
        raise DocumentNotFoundError(f"Job '{job_id}' has no processed document.")
    # A job always produces exactly one document directory.
    return candidates[0]


def resolve_original_upload(job_id: str) -> Path:
    """Return the path to the originally-uploaded source file for a job."""
    up_dir = upload_dir(job_id)
    if not up_dir.is_dir():
        raise DocumentNotFoundError(
            f"Job '{job_id}' has no stored original upload; it may have been cleaned up.")
    files = [p for p in up_dir.iterdir() if p.is_file()]
    if not files:
        raise DocumentNotFoundError(f"Job '{job_id}' upload folder is empty.")
    return files[0]


def load_chunks_json(doc_dir: Path) -> dict:
    matches = list(doc_dir.glob("*.chunks.json"))
    if not matches:
        raise DocumentNotFoundError(f"No chunks JSON found in '{doc_dir}'.")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def chunks_json_path(doc_dir: Path) -> Path:
    matches = list(doc_dir.glob("*.chunks.json"))
    if not matches:
        raise DocumentNotFoundError(f"No chunks JSON found in '{doc_dir}'.")
    return matches[0]


# --------------------------------------------------------------------------- #
# Ingestion: convert (if needed) -> parse -> chunks.json
# --------------------------------------------------------------------------- #
def ingest_file(src: Path, out_root: Path, *, render_pages: bool = True,
                ocr: str = "auto", ocr_lang: str = "eng",
                hybrid_url: str | None = None,
                progress: Callable[[int], None] | None = None,
                **opt_overrides) -> dict:
    """Convert (if needed) + parse a single file. Returns a summary dict
    including the path to the produced chunks JSON. Raises
    UnsupportedFileTypeError / ConversionFailedError / ParsingFailedError
    on failure -- callers should let those propagate to the route layer,
    which maps them to HTTP responses.
    """
    src = Path(src)
    out_root = Path(out_root)
    doc_dir = out_root / src.stem
    conv_dir = doc_dir / CONVERTED_SUBDIR
    images_dir = doc_dir / IMAGES_SUBDIR
    images_dir.mkdir(parents=True, exist_ok=True)

    original_kind = kind_of(src)
    if original_kind == "unknown":
        raise UnsupportedFileTypeError(f"Unsupported file type: {src.suffix}")

    # 1) normalize to PDF
    try:
        pdf_path = convert_to_pdf(src, conv_dir)
    except ConversionFailedError:
        raise
    except Exception as e:  # noqa: BLE001 - convert unexpected errors into our type
        raise ConversionFailedError(f"Unexpected conversion error: {e}") from e
    if progress:
        progress(2)  # conversion complete; classification/OCR is now active

    # 2) parse (auto digital/scanned/mixed handling lives in parse_manual)
    # Slide decks read best one-chunk-per-slide, so force page chunking for them.
    if src.suffix.lower() in {".ppt", ".pptx", ".odp"}:
        opt_overrides.setdefault("chunk_mode", "page")

    opts = make_opts(render_pages=render_pages, ocr=ocr,
                     ocr_lang=ocr_lang, hybrid_url=hybrid_url,
                     **opt_overrides)
    try:
        out_json = parse_one(pdf_path, doc_dir, images_dir, opts)
    except Exception as e:  # noqa: BLE001
        log.exception("Parsing failed for %s", src.name)
        raise ParsingFailedError(f"Parsing failed for {src.name}: {e}") from e
    if progress:
        progress(3)  # parse/OCR complete; preparing the inspector response

    # NOTE: we used to `shutil.rmtree(doc_dir / "_raw")` here. That raw
    # docling/opendataloader parse (structure + page classification) is
    # exactly what enhance_document() needs to re-OCR a handful of pages
    # WITHOUT re-running the expensive native conversion pass. Keeping it
    # costs a bit of disk (one JSON per job) but turns "Enhance Mode" from
    # a full document re-ingest into a targeted, page-scoped OCR pass.

    data = json.loads(out_json.read_text(encoding="utf-8"))
    parse_meta = data.get("parse", {}) or {}
    engine = parse_meta.get("engine")
    mode = parse_meta.get("mode") or mode_from_engine(engine)
    page_classification = parse_meta.get("page_classification")
    scanned_page_numbers = parse_meta.get("scanned_page_numbers", []) or []
    page_engines = parse_meta.get("page_engines", {}) or {}
    # A scanned page still "pending" enhancement if it hasn't been through
    # LightOnOCR yet (either never OCR'd, or OCR'd but stuck on Tesseract).
    pending_pages = [p for p in scanned_page_numbers if page_engines.get(str(p)) != "lighton"]

    return {
        "source_file": src.name,
        "original_kind": original_kind,
        "converted": original_kind != "pdf",
        "doc_id": data.get("doc_id"),
        "doc_dir": str(doc_dir),
        "json_path": str(out_json),
        "num_chunks": data.get("num_chunks"),
        "file_size_bytes": src.stat().st_size,
        "page_classification": page_classification,
        "engine": engine,
        "mode": mode,
        "page_engines": page_engines,
        "scanned_page_numbers": scanned_page_numbers,
        "pending_enhance_pages": pending_pages,
        # "Enhanced Mode" is available only if some scanned page hasn't
        # already gone through LightOnOCR -- per-page, not per-document, so
        # a mixed PDF where some pages auto-escalated and others didn't still
        # offers the button, and a fully-LightOnOCR'd document disables it.
        "can_enhance": len(pending_pages) > 0,
    }


# --------------------------------------------------------------------------- #
# Enhance: user-triggered "Enhanced Mode" button
# --------------------------------------------------------------------------- #
def _lighton_is_importable() -> bool:
    try:
        import torch  # noqa: F401
        from transformers import LightOnOcrForConditionalGeneration  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def enhance_document(job_id: str, pages: list[int] | None = None) -> dict:
    """Re-OCR ONLY the requested (or pending) SCANNED pages with
    LightOnOCR-2-1B, in place -- native digital-text pages are never
    touched, and pages already on LightOnOCR are skipped unless explicitly
    requested again. Returns the same summary shape as ingest_file().

    Unlike the old implementation, this does NOT call ingest_file() / ever
    re-run file conversion or the native docling/opendataloader parse pass.
    Those are the expensive, document-wide steps and nothing about them
    changes when you just want better OCR on page 7. Instead we:

      1. Load the raw docling parse that ingest_file() already produced and
         (as of this version) no longer deletes.
      2. Reuse the already-converted PDF sitting in doc_dir/_converted.
      3. Run LightOnOCR-2-1B ONLY on the target pages (run_builtin_ocr),
         splice the new nodes into the cached raw tree, and rebuild chunks.

    On a CPU box, LightOnOCR-2-1B does token-by-token generation per page,
    so this is the single biggest cost in the whole pipeline -- scoping it
    to just the page(s) you clicked "Enhance" on (instead of every scanned
    page in the document) is what actually buys you the speedup.

    Args:
        job_id: the job to enhance.
        pages:  1-based page numbers to force through LightOnOCR-2-1B. If
                None, defaults to every scanned page not yet on LightOnOCR
                (i.e. the old "pending" set) -- still page-scoped, never
                "redo pages that are already fine".

    Raises:
        DocumentNotFoundError    - job/doc/upload missing
        AlreadyEnhancedError     - nothing to do: no scanned pages, or the
                                    requested/pending pages are already on
                                    LightOnOCR
        OCREngineUnavailableError - the Enhanced Mode OCR engine isn't
                                    installed on this server, or it failed
                                    to produce output for the target pages
    """
    from src.parse_manual import (  # local import: keep services.py light for callers that never enhance
        build_chunks, build_fallback_chunks, get_page_sizes, make_opts,
        iter_nodes, parse_doc_header, run_builtin_ocr,
    )

    doc_dir = resolve_doc_dir(job_id)
    stem = doc_dir.name
    current = load_chunks_json(doc_dir)
    current_parse = current.get("parse") or {}
    scanned_page_numbers = current_parse.get("scanned_page_numbers", []) or []
    page_engines: dict[str, str] = dict(current_parse.get("page_engines", {}) or {})
    pending_pages = [p for p in scanned_page_numbers if page_engines.get(str(p)) != "lighton"]

    if pages is not None:
        requested = sorted(set(pages))
        # Only scanned pages can ever be OCR'd -- silently drop anything else
        # (native/digital pages, out-of-range page numbers) rather than error,
        # so a UI that just sends "the pages currently on screen" still works.
        targets = [p for p in requested if p in scanned_page_numbers]
    else:
        targets = pending_pages

    if not targets:
        raise AlreadyEnhancedError(
            "This document has no scanned pages to enhance." if not scanned_page_numbers
            else "The requested page(s) have no scanned content to enhance, or are "
                 "already processed in Enhanced Mode." if pages is not None
            else "This document has no pages left to enhance -- every scanned "
                 "page has already been processed in Enhanced Mode.")

    if not _lighton_is_importable():
        raise OCREngineUnavailableError(
            "Enhanced Mode isn't available right now -- its OCR engine "
            "isn't installed on this server. Contact your administrator.")

    raw_dir = doc_dir / "_raw"
    enhanced_dir = doc_dir / "_enhanced"
    raw_path = raw_dir / f"{stem}.json"
    if not raw_path.exists():
        # Cache missing (older job, predates this change, or was cleaned up) --
        # fall back to a full re-ingest so Enhance still works, just slower.
        return _enhance_via_full_reingest(job_id, targets)

    conv_dir = doc_dir / CONVERTED_SUBDIR
    pdf_candidates = list(conv_dir.glob("*.pdf"))
    if not pdf_candidates:
        return _enhance_via_full_reingest(job_id, targets)
    pdf_path = pdf_candidates[0]

    # Continue from a previous enhanced copy when present; otherwise clone the
    # OCR-enriched normal parse. The normal `_raw` cache is never modified by
    # this action, so normal and enhanced results remain independently usable.
    enhanced_raw_path = enhanced_dir / f"{stem}.json"
    working_path = enhanced_raw_path if enhanced_raw_path.exists() else raw_path
    raw = json.loads(working_path.read_text(encoding="utf-8"))
    page_sizes = get_page_sizes(pdf_path)
    opts = make_opts(handwriting="force", ocr_engine="lighton", lighton_device=None)

    def remove_ocr_for_pages(node, page_set: set[int]) -> None:
        """Remove only generated OCR nodes, preserving native/image nodes."""
        if not isinstance(node, dict):
            return
        kids = node.get("kids")
        if isinstance(kids, list):
            node["kids"] = [
                child for child in kids
                if not (isinstance(child, dict)
                        and child.get("_ocr")
                        and child.get("page number") in page_set)
            ]
            for child in node["kids"]:
                remove_ocr_for_pages(child, page_set)

    target_set = set(targets)
    remove_ocr_for_pages(raw, target_set)

    # Backward compatibility: old jobs cached the native raw JSON before OCR.
    # Recreate only the missing non-target OCR pages once, according to their
    # recorded engine, then store the repaired tree in `_enhanced`.
    present_ocr_pages = {
        n.get("page number") for n in iter_nodes(raw)
        if n.get("_ocr") and n.get("page number") not in target_set
    }
    missing_by_engine: dict[str, list[int]] = {}
    for pg in scanned_page_numbers:
        if pg in target_set or pg in present_ocr_pages:
            continue
        recorded_engine = page_engines.get(str(pg), "tesseract")
        missing_by_engine.setdefault(recorded_engine, []).append(pg)
    for recorded_engine, missing_pages in missing_by_engine.items():
        repair_opts = make_opts(
            handwriting="force" if recorded_engine == "lighton" else "off",
            ocr_engine=recorded_engine,
            lighton_device=None,
        )
        repaired = run_builtin_ocr(
            pdf_path, missing_pages, raw, recorded_engine,
            repair_opts["ocr_lang"], repair_opts["ocr_dpi"], page_sizes, repair_opts,
        )
        page_engines.update({str(k): v for k, v in repaired.items()})

    log.info("Enhancing job=%s doc=%s pages=%s with LightOnOCR-2-1B (targeted)",
             job_id, pdf_path.name, targets)
    new_engines = run_builtin_ocr(pdf_path, targets, raw, "lighton",
                                  opts["ocr_lang"], opts["ocr_dpi"], page_sizes, opts)

    if not new_engines:
        raise OCREngineUnavailableError(
            "Enhanced Mode's OCR engine failed to produce output for the "
            "requested page(s). Check the server logs for details.")

    page_engines.update({str(k): v for k, v in new_engines.items()})

    enhanced_dir.mkdir(parents=True, exist_ok=True)
    normal_result_path = enhanced_dir / f"{stem}.normal.chunks.json"
    if not normal_result_path.exists():
        # Snapshot the pre-enhancement API result once for comparison or
        # rollback; later page enhancements must not overwrite this baseline.
        normal_result_path.write_text(
            json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    enhanced_raw_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    header = parse_doc_header(raw, pdf_path)
    if current_parse.get("chunking") == "page-fallback":
        chunks = build_fallback_chunks(raw, header, doc_dir / "_raw", doc_dir / IMAGES_SUBDIR, stem)
    else:
        chunks = build_chunks(raw, header, doc_dir / "_raw", doc_dir / IMAGES_SUBDIR, stem)
        if not chunks:
            chunks = build_fallback_chunks(raw, header, doc_dir / "_raw", doc_dir / IMAGES_SUBDIR, stem)

    engine = "builtin-ocr:lighton:force" if any(v == "lighton" for v in page_engines.values()) \
        else current_parse.get("engine", "native")

    result = {
        **header,
        "num_chunks": len(chunks),
        "parse": {
            **current_parse,
            "engine": engine,
            "mode": mode_from_engine(engine),
            "page_engines": page_engines,
        },
        "pages": current.get("pages", []),  # unchanged: no re-render needed
        "chunks": chunks,
    }
    out_json = doc_dir / f"{stem}.chunks.json"
    out_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    # Keep a self-contained enhanced JSON alongside its working raw copy. The
    # canonical file above remains for the existing API/UI contract.
    (enhanced_dir / f"{stem}.chunks.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    new_pending = [p for p in scanned_page_numbers if page_engines.get(str(p)) != "lighton"]
    return {
        "job_id": job_id,
        "source_file": current.get("source_file"),
        "original_kind": "pdf",
        "converted": False,
        "doc_id": result.get("doc_id"),
        "doc_dir": str(doc_dir),
        "json_path": str(out_json),
        "num_chunks": result["num_chunks"],
        "file_size_bytes": resolve_original_upload(job_id).stat().st_size,
        "page_classification": current_parse.get("page_classification"),
        "engine": engine,
        "mode": result["parse"]["mode"],
        "page_engines": page_engines,
        "scanned_page_numbers": scanned_page_numbers,
        "pending_enhance_pages": new_pending,
        "can_enhance": len(new_pending) > 0,
    }


def _enhance_via_full_reingest(job_id: str, targets: list[int]) -> dict:
    """Fallback used only when the per-page cache (_raw / _converted) isn't
    available -- e.g. a job created before this change. Slower: re-runs
    conversion + native parse for the whole document, but still forces
    LightOnOCR-2-1B only on scanned pages (never non-scanned ones)."""
    src = resolve_original_upload(job_id)
    out_root = job_dir(job_id)
    log.warning("job=%s has no cached raw/_converted -- falling back to full re-ingest "
                "for Enhance Mode (targets=%s)", job_id, targets)
    summary = ingest_file(
        src, out_root,
        render_pages=True,
        ocr="force",
        ocr_engine="lighton",
        handwriting="force",
        lighton_device=None,
    )
    if summary.get("mode") != MODE_ENHANCED:
        raise OCREngineUnavailableError(
            "Enhanced Mode's OCR engine failed to produce output for this "
            "document; the page(s) fell back to the normal engine. Check "
            "the server logs for details.")
    summary["job_id"] = job_id
    return summary


# --------------------------------------------------------------------------- #
# Export: zip <doc>.chunks.json + images/ for download
# --------------------------------------------------------------------------- #
def build_export_zip(job_id: str) -> Path:
    """Create (or reuse) a zip archive of the job's document output and
    return its path. Raises DocumentNotFoundError if the job/doc is missing,
    or ExportFailedError if writing the archive fails.

    Intermediate build artifacts (_converted/, _upload/, _raw/) are
    intentionally excluded -- they're implementation detail, not part of
    the deliverable.
    """
    doc_dir = resolve_doc_dir(job_id)  # raises DocumentNotFoundError

    settings.EXPORT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = settings.EXPORT_TMP_DIR / f"{job_id}_{doc_dir.name}.zip"

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in doc_dir.rglob("*"):
                if not path.is_file():
                    continue
                if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(doc_dir).parts):
                    continue
                arcname = Path(doc_dir.name) / path.relative_to(doc_dir)
                zf.write(path, arcname=str(arcname))
    except OSError as e:
        raise ExportFailedError(f"Could not build export archive: {e}") from e

    log.info("Built export zip for job=%s -> %s", job_id, zip_path)
    return zip_path
