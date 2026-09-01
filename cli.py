#!/usr/bin/env python3
"""
cli.py -- batch-ingest files from the command line
====================================================
Uses the exact same service layer as the web app, so results are identical.

Usage:
    python cli.py mymanual.docx -o out/
    python cli.py slides.pptx scan.jpg report.pdf -o out/
    python cli.py scan.jpg --ocr-engine lighton --handwriting force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.common import IngestionAppError
from src.services import ingest_file


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch ingest: convert to PDF then parse.")
    ap.add_argument("inputs", nargs="+", help="files (pdf/docx/pptx/xlsx/image/...)")
    ap.add_argument("-o", "--output", default="ingested", help="output root directory")
    ap.add_argument("--no-render-pages", action="store_true",
                    help="skip page-image rendering (UI overlay background)")
    ap.add_argument("--ocr", default="auto", choices=["off", "auto", "force"])
    ap.add_argument("--ocr-engine", default="tesseract",
                    choices=["tesseract", "easyocr", "lighton"])
    ap.add_argument("--ocr-lang", default="eng")
    ap.add_argument("--handwriting", default="auto", choices=["off", "auto", "force"])
    ap.add_argument("--hybrid-url", default=None, help="use an external OCR server instead")
    args = ap.parse_args()

    out_root = Path(args.output)
    print(f"Ingesting {len(args.inputs)} file(s) -> {out_root.resolve()}")
    exit_code = 0
    for item in args.inputs:
        src = Path(item)
        print(f"  {src.name} ...")
        try:
            r = ingest_file(src, out_root, render_pages=not args.no_render_pages,
                            ocr=args.ocr, ocr_engine=args.ocr_engine,
                            ocr_lang=args.ocr_lang, handwriting=args.handwriting,
                            hybrid_url=args.hybrid_url)
            tag = "converted->pdf" if r["converted"] else "pdf"
            mode_tag = "ENHANCED" if r["mode"] == "enhanced" else "normal"
            print(f"    [{tag}] {r['page_classification']} | {r['engine']} | "
                  f"mode={mode_tag} | {r['num_chunks']} chunks -> {r['json_path']}")
        except IngestionAppError as e:
            print(f"    FAILED [{e.code}]: {e.message}", file=sys.stderr)
            exit_code = 1
        except Exception as e:  # noqa: BLE001
            print(f"    FAILED: {e}", file=sys.stderr)
            exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
