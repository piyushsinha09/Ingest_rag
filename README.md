# Ingestion Universal

Upload any document (PDF / Word / PowerPoint / Excel / image). It's converted
to PDF if needed, parsed into section-oriented chunks (auto digital / scanned /
mixed handling), and shown in an inline inspector with page-image + bounding-box
overlays.

Scanned pages are OCR'd automatically with Tesseract on upload. If Tesseract's
result on a page isn't good enough (handwriting, poor scan quality), the
pipeline **automatically escalates that page to LightOnOCR-2-1B** in-line, as
part of the same upload/ingest pass -- so there's no separate "rerun" visible
to the user, the escalated result is simply what comes back. On top of that,
the UI also has an explicit **Enhanced Mode** button: if the automatic result
still isn't right, the user can force a full LightOnOCR-2-1B re-run for the
whole document with one click.

## Project layout

```
ingestion_universal/
├── main.py                    # web entry point (uvicorn)
├── cli.py                     # batch entry point (same pipeline, no server)
├── requirements.txt
├── config.py                  # USER-FACING settings (paths, tunables, limits)
├── constants.py               # HARDCODED vocabulary (never user-configurable)
├── src/
│   ├── app.py                 # FastAPI app factory + global error handling
│   ├── common.py               # logger + exception hierarchy + pydantic schemas
│   ├── convert_to_pdf.py       # Office/image -> PDF normalization
│   ├── parse_manual.py         # PDF -> structured chunks, OCR + handwriting escalation
│   ├── services.py             # job/ingestion/enhance/export orchestration
│   └── routes.py               # all 5 HTTP endpoints (GET /, /api/ingest, /api/enhance/{id},
│                                #                        /api/export/{id}, /api/health)
├── templates/
│   └── index.html             # SPA shell
├── static/
│   ├── css/style.css
│   └── js/app.js
└── server_data/                # runtime output (per-job folders), gitignored
```

**Why this split:**
- `config.py` holds things a deployer/user might reasonably change (paths, DPI, size limits, thresholds) -- read via `settings.X`. A single file, not a package, since there's only ever one settings object.
- `constants.py` holds fixed vocabulary that's never meant to be edited at runtime (engine name strings, mode enums) -- read via `from constants import ...`.
- `src/common.py` bundles three small, unrelated-but-shared pieces (logging, the exception hierarchy, pydantic schemas) that both `services.py` and `routes.py` depend on but that don't contain behavior of their own.
- `src/services.py` is where the actual work happens (convert -> parse -> OCR -> chunk, plus job/export bookkeeping) and has no FastAPI imports, so it's callable from `cli.py` unchanged. It merges what used to be four separate service files -- job/ingestion/enhance/export -- since together they're one cohesive orchestration layer, each piece under ~100 lines.
- `src/routes.py` merges all 5 endpoint groups (previously 5 files) into one file -- it's the HTTP boundary only: parse the request, call a services.py function, shape the response.
- `src/convert_to_pdf.py` and `src/parse_manual.py` stay standalone files rather than folders -- each is a single cohesive module, and `parse_manual.py` (~1,400 lines) is deliberately not merged into anything else since it's the core of the whole pipeline.

There's no unused `assets/` folder in this layout -- it existed in an earlier version but nothing in the code ever referenced it.

## Setup

```bash
pip install -r requirements.txt
```

System dependencies (not pip-installable):
- **Java 11+** — required by `opendataloader-pdf`
- **LibreOffice** (`soffice` on PATH) — Office → PDF conversion
- **Tesseract OCR** (`tesseract` on PATH) — scanned-page OCR

Optional, for **Enhanced Mode** (LightOnOCR-2-1B, handwriting-capable):
```bash
python -m venv .venv_lighton && source .venv_lighton/bin/activate
pip install "transformers>=5.0.0" torch pillow
```
Without this installed, everything still works — the Enhanced Mode button is
simply disabled server-side (`/api/health` reports `lighton_available: false`,
and `/api/enhance/{job_id}` returns `503 ocr_engine_unavailable`).

## Run

```bash
python main.py                 # -> http://localhost:8000
# or
uvicorn main:app --reload
```

## API

| Method | Path                     | Description                                             |
|--------|--------------------------|----------------------------------------------------------|
| GET    | `/`                      | Inspector UI                                              |
| POST   | `/api/ingest`            | Upload + process a file. Returns chunks JSON + summary.   |
| POST   | `/api/enhance/{job_id}`  | Force a LightOnOCR-2-1B re-run for that job's document.    |
| GET    | `/api/export/{job_id}`   | Download a `.zip` of `<doc>.chunks.json` + `images/`.       |
| GET    | `/api/health`            | Reports which OCR engines are actually available.          |

Every response's `summary.mode` is `"normal"` (Tesseract / native text) or
`"enhanced"` (LightOnOCR-2-1B was used). `summary.can_enhance` tells the frontend
whether the Enhanced Mode button should be enabled.

## Handwriting escalation logic

`src/parse_manual.py::ocr_page_nodes()` escalates a scanned page from
Tesseract to LightOnOCR-2-1B automatically whenever `handwriting="auto"` is in
effect — which is the default for both the web app's `/api/ingest` and the
CLI. This happens in-line, within the single ingest pass, before any result
is returned, so it's not a visible "rerun." The explicit
`/api/enhance/{job_id}` call (Enhanced Mode button) additionally lets the
user force `handwriting="force"` afterward, re-running the *whole* document
through LightOnOCR-2-1B regardless of Tesseract's confidence.

A page escalates automatically when EITHER:
- Tesseract found `>= HANDWRITING_MIN_WORDS` confident words, but their mean
  confidence is `<= HANDWRITING_CONF_THRESHOLD` ("it tried, but wasn't sure"), or
- Tesseract found **zero** usable words at all — the strongest possible signal
  that the page (e.g. cursive handwriting) needs a real handwriting model, not
  a weaker "not enough evidence to escalate" case as it was treated before.

Tunable via `config.py` (`HANDWRITING_CONF_THRESHOLD`,
`HANDWRITING_MIN_WORDS`) or per-call via `--handwriting-threshold` /
`--handwriting-min-words` on the CLI.
