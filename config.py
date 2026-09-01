"""
config.py
==========
Single source of truth for paths and tunables used across the app.
Nothing here talks to the filesystem beyond resolving/creating directories,
so it's safe to import from anywhere (routes, services, parsers).
"""
from __future__ import annotations

from pathlib import Path


class Settings:
    # ---- identity -----------------------------------------------------
    APP_NAME: str = "Parse Anything"
    APP_VERSION: str = "2.0.0"

    # ---- paths ----------------------------------------------------------
    BASE_DIR: Path = Path(__file__).resolve().parent
    SRC_DIR: Path = BASE_DIR / "src"
    STATIC_DIR: Path = BASE_DIR / "static"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"

    DATA_DIR: Path = BASE_DIR / "server_data"          # per-job ingestion output
    EXPORT_TMP_DIR: Path = BASE_DIR / "server_data" / "_exports"  # generated zips

    # ---- upload constraints ---------------------------------------------
    MAX_UPLOAD_BYTES: int = 60 * 1024 * 1024  # 60 MB
    ALLOWED_EXTENSIONS: set[str] = {
        ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt",
        ".ppt", ".pptx", ".odp",
        ".xls", ".xlsx", ".ods", ".csv",
        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp",
    }

    # ---- OCR defaults -----------------------------------------------------
    OCR_MODE_DEFAULT: str = "auto"        # off | auto | force
    OCR_ENGINE_DEFAULT: str = "tesseract"  # tesseract | easyocr | lighton
    OCR_LANG_DEFAULT: str = "eng"
    OCR_RENDER_DPI: int = 150            # dpi used to rasterize pages before OCR
    PAGE_PREVIEW_DPI: int = 96            # dpi used for the inspector's page images
    LIGHTON_RENDER_DPI: int = 96

    # Handwriting escalation (Tesseract -> LightOnOCR-2-1B) tuning.
    # A page escalates to LightOnOCR-2-1B automatically ("auto" handwriting mode) when
    # EITHER of these is true:
    #   * Tesseract found >= HANDWRITING_MIN_WORDS confident words, but their mean
    #     confidence is <= HANDWRITING_CONF_THRESHOLD  (Tesseract "sort of" tried,
    #     but wasn't sure of what it read), OR
    #   * Tesseract found ZERO usable words at all (nwords == 0) on a page that was
    #     already classified as scanned/has-an-image -- i.e. total illegibility to
    #     Tesseract, which is the strongest signal of all that this needs a real
    #     handwriting-capable model.
    HANDWRITING_CONF_THRESHOLD: float = 60
    HANDWRITING_MIN_WORDS: int = 3

    # ---- LightOnOCR-2-1B (Enhanced Mode) tuning --------------------------
    # Generation is token-by-token, so this is the single biggest speed lever
    # on CPU. 2048 was the old default (safe for dense reference/table pages
    # but overkill for a typical text page). Override via env:
    #   LIGHTON_MAX_NEW_TOKENS=800 uvicorn main:app ...
    import os as _os
    LIGHTON_MAX_NEW_TOKENS: int = int(_os.environ.get("LIGHTON_MAX_NEW_TOKENS", "2096"))
    # None = use all physical cores (torch's default). Set explicitly if this
    # box also runs other CPU-bound work and you don't want OCR to hog every
    # core, e.g. LIGHTON_CPU_THREADS=4.
    LIGHTON_CPU_THREADS: int | None = (
        int(_os.environ["LIGHTON_CPU_THREADS"]) if _os.environ.get("LIGHTON_CPU_THREADS") else None
    )
    # Modern x86 CPUs with native AVX-512 BF16 (and recent ARM CPUs with
    # native BF16) can run the model with roughly half the memory traffic of
    # float32.  LightOn generation is memory-bandwidth-heavy, so this improves
    # both the automatic escalation and the explicit Enhance action.  Set to
    # "float32" if a particular CPU/PyTorch build has poor BF16 support.
    LIGHTON_CPU_DTYPE: str = _os.environ.get("LIGHTON_CPU_DTYPE", "auto").lower()
    # Dynamic int8 quantization of the model's Linear layers, applied once at
    # load time, CPU-only. This trades a small amount of accuracy for real
    # wall-clock speedup on CPU (commonly ~2-3x for transformer decoders) --
    # worth trying if generation is compute-bound rather than memory-bound.
    # Opt-in because it does change output slightly. Enable with:
    #   LIGHTON_QUANTIZE_CPU=1
    LIGHTON_QUANTIZE_CPU: bool = _os.environ.get("LIGHTON_QUANTIZE_CPU", "0") == "1"
    del _os

    # ---- job / export -------------------------------------------------------
    JOB_ID_LENGTH: int = 12
    EXPORT_ZIP_TTL_SECONDS: int = 60 * 60  # informational; cleanup is manual/cron

    @classmethod
    def ensure_dirs(cls) -> None:
        for d in (cls.DATA_DIR, cls.EXPORT_TMP_DIR, cls.STATIC_DIR, cls.TEMPLATES_DIR):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
