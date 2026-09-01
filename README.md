<div align="center">

# ◆ Ingest_rag

**Parse anything. Understand everything.**

Drop in a scan, a slide deck, a spreadsheet, or a handwritten form — get back clean, structured,
searchable content, with every piece mapped back to exactly where it came from on the page.

</div>

---

## What it does

- **Any format, one drop** — PDF, Word (`.doc`/`.docx`), PowerPoint (`.ppt`/`.pptx`), and images
  (PNG/JPG/TIFF) are all accepted directly, typed or scanned.
- **Typed or scanned — both read fine** — clean digital text and scanned paper pages are handled
  side by side in the same document, classified per page, automatically.
- **Handwriting, picked up too** — handwritten notes and annotations are recognized alongside
  printed text, not skipped.
- **Two OCR engines, matched to the job** —
  [Tesseract](https://github.com/tesseract-ocr/tesseract) handles everyday pages fast, by default,
  and automatically escalates a page to a deeper model if its confidence is low or it finds
  nothing at all. For the pages that still aren't right, **Enhanced Mode** re-runs just the page(s)
  you pick through [LightOnOCR‑2‑1B](https://huggingface.co/lightonai/LightOnOCR-2-1B), a
  vision-language OCR model — targeted at those pages only, not a full re-ingest of the document.
- **Comes out structured, not a blob** — headings, paragraphs, tables, and lists are split into
  labeled, ordered chunks with section numbers and token estimates.
- **Every chunk knows its spot** — click any extracted chunk in the sidebar and jump straight to
  its exact bounding box on the original page.
- **Ready for RAG** — every chunk ships with clean text, a token estimate, and its exact page and
  bounding box, so it can be dropped straight into an embedding index with citations intact.
- **Download & export** — pull the full parsed JSON plus page images as a `.zip` at any time.

## How it's built

| Layer | What it uses |
|---|---|
| Native document parsing | [`opendataloader-pdf`](https://pypi.org/project/opendataloader-pdf/) — structured, reading-order-aware PDF → JSON (Java-backed) |
| Office format conversion | LibreOffice (`soffice`), headless, for Word/PowerPoint → PDF |
| Page rendering | [PyMuPDF](https://pypi.org/project/pymupdf/) |
| Default OCR | [Tesseract](https://github.com/tesseract-ocr/tesseract) via `pytesseract`, with automatic escalation on low-confidence pages |
| Enhanced-Mode OCR | [LightOnOCR‑2‑1B](https://huggingface.co/lightonai/LightOnOCR-2-1B) via `transformers` + `torch` |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| Frontend | Plain HTML / CSS / JS — no framework, no build step |

## Getting started

### Prerequisites (system, not pip-installable)

- **Java 11+** — required by `opendataloader-pdf`
- **LibreOffice** (`soffice` on PATH) — Word/PowerPoint → PDF conversion
- **Tesseract OCR** (`tesseract` on PATH), plus any extra language packs you need

### Install

```bash
pip install -r requirements.txt
```

Enhanced Mode (LightOnOCR-2-1B) is optional. Without it installed, everything else still works —
the Enhanced Mode button is disabled server-side (`/api/health` reports `lighton_available: false`).
To enable it:

```bash
pip install "transformers>=5.0.0" torch pillow
```

### Run

```bash
python main.py                 # -> http://localhost:8000
# or, with live reload:
uvicorn main:app --reload
```

### Tuning Enhanced Mode on CPU

LightOnOCR-2-1B is a vision-language model, so it's naturally slower per page on CPU than
Tesseract. A few environment variables (read in `config.py`) tune the tradeoff:

```bash
LIGHTON_MAX_NEW_TOKENS=500 \   # generation cap per page (default 800) -- the biggest speed lever
LIGHTON_CPU_THREADS=4 \        # cap CPU threads if this box also runs other work
LIGHTON_QUANTIZE_CPU=1 \       # dynamic int8 quantization -- smaller + often 2-3x faster on CPU
python main.py
```

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `POST` | `/api/ingest` | Upload and parse a document. Returns chunks JSON + summary. |
| `GET` | `/api/ingest-status/{job_id}` | Poll ingest progress |
| `POST` | `/api/enhance/{job_id}` | Re-OCR specific pages (`{"pages": [3, 7]}`) — or all pending pages if omitted — in Enhanced Mode |
| `GET` | `/api/export/{job_id}` | Download a `.zip` of `<doc>.chunks.json` + page images |
| `GET` | `/api/health` | Reports which OCR engines are actually available |

`summary.mode` is `"normal"` (Tesseract / native text) or `"enhanced"` (LightOnOCR-2-1B was used on
at least one page). `summary.can_enhance` / `summary.pending_enhance_pages` tell the frontend which
pages still have room for a deeper pass.

## OCR escalation logic

`src/parse_manual.py::ocr_page_nodes()` escalates a scanned page from Tesseract to LightOnOCR-2-1B
automatically whenever `handwriting="auto"` is in effect (the default for `/api/ingest`), so a
typical document only pays the slow-model cost on the pages that actually need it. A page escalates
automatically when either:

- Tesseract found `>= HANDWRITING_MIN_WORDS` confident words, but their mean confidence is
  `<= HANDWRITING_CONF_THRESHOLD` ("it tried, but wasn't sure"), or
- Tesseract found **zero** usable words at all.

Both thresholds are tunable in `config.py`. `Enhance Mode` (`/api/enhance/{job_id}`) is the
explicit, user-triggered version of the same escalation — scoped to whichever pages you ask for
(or all pages still pending), reusing the cached native parse and converted PDF so it doesn't
re-run conversion or re-parsing, only the targeted OCR pass.

## Project layout

```
├── main.py                # web entry point (uvicorn)
├── cli.py                  # batch entry point, same pipeline, no server
├── config.py                # user-facing settings (paths, DPI, thresholds, OCR tuning)
├── constants.py             # fixed vocabulary (engine names, mode enums) -- not runtime-configurable
├── requirements.txt
├── src/
│   ├── app.py                 # FastAPI app factory, static/template mounting, error handling
│   ├── common.py               # logger, exception hierarchy, shared Pydantic schemas
│   ├── convert_to_pdf.py        # Office/image -> PDF normalization
│   ├── parse_manual.py          # native parsing, OCR engines + escalation, chunk building
│   ├── services.py              # ingest / enhance / export orchestration (no FastAPI imports)
│   └── routes.py                # all HTTP endpoints
├── templates/
│   └── index.html              # single-page shell (landing / upload / inspector)
├── static/
│   ├── css/style.css            # design tokens + all UI styling
│   ├── js/app.js                 # inspector UI logic
│   ├── js/landing.js             # landing page slideshow + transitions
│   └── media/hero-flight.webm     # hero animation asset
└── server_data/                # runtime output (per-job folders), gitignored
```

`services.py` has no FastAPI imports, so it's callable unchanged from `cli.py` for batch/offline
use outside the web app.

## Supported formats

`PDF` · `DOCX` / `DOC` · `PPTX` / `PPT` · `PNG` · `JPG` · `TIFF` — typed, scanned, or handwritten.

## License

Add your license here.
