
# #!/usr/bin/env python3
# """
# parse_manual.py
# ===============
# Parse structured technical manuals (SCHEMA-ST4-style, numbered sections) with
# OpenDataLoader into a section-oriented JSON built for RAG.

# This version is hardened to handle a WIDE range of PDFs:
#   * normal, born-digital PDFs (text layer present)
#   * multi-column layouts            -> reading_order="xycut" (XY-cut)
#   * complex / borderless tables      -> table_method="cluster"
#   * tagged / structured PDFs         -> use_struct_tree
#   * encrypted PDFs                   -> password
#   * SCANNED / image-only PDFs        -> OCR via the hybrid backend
#   * unstructured PDFs (no numbered headings) -> page/whole-doc fallback chunking

# --------------------------------------------------------------------------------
# IMPORTANT about scanned PDFs / OCR
# --------------------------------------------------------------------------------
# Every page is classified as digital or scanned. Digital pages use the fast native
# text-layer path (unchanged logic). Scanned pages are OCR'd IN-PROCESS and the
# recovered text is merged into the SAME chunks JSON -- no external server needed.

# Built-in OCR engines:
#   * tesseract (default) -- needs the system binary:
#         Ubuntu/Debian : sudo apt-get install -y tesseract-ocr
#         macOS         : brew install tesseract
#         (extra languages: apt-get install tesseract-ocr-deu, etc.)
#         pip install pymupdf pytesseract pillow
#   * easyocr (optional) -- pip only, heavier, downloads models on first run:
#         pip install easyocr
#   * lighton (optional) -- LightOnOCR-2-1B, handwriting-capable VLM OCR:
#         pip install "transformers>=5.0.0" torch pillow

#     python parse_manual.py scan.pdf                         # tesseract, lang=eng
#     python parse_manual.py scan.pdf --ocr-lang deu          # German
#     python parse_manual.py scan.pdf --ocr-engine easyocr --ocr-lang en
#     python parse_manual.py scan.pdf --ocr-engine lighton --handwriting force

# --ocr auto (default) : native for digital pages, OCR for scanned pages.
# --ocr force          : OCR every SCANNED page (native digital-text pages are
#                         never touched, even in force mode).
# --ocr off            : text layer only (scanned pages come out empty; warns).

# Advanced: an external Docling/EasyOCR "hybrid" server can be used instead of the
# built-in OCR by passing --hybrid-url http://localhost:5002 (start it with
# `pip install "opendataloader-pdf[hybrid]" && opendataloader-pdf-hybrid --port 5002`).

# --------------------------------------------------------------------------------
# Output per PDF -> <name>.chunks.json  (unchanged schema, plus a `parse` block)
# --------------------------------------------------------------------------------
#   {
#     doc_id, doc_title, product, subject, doc_type, doc_number, language,
#     revision_date, source_file, total_pages, num_chunks,
#     parse: { engine, mode, reading_order, table_method, ocr, page_classification,
#               pages_scanned, scanned_page_numbers, page_engines, ... },
#     chunks: [
#       { chunk_id, section_number, section_title, text,
#         images: [{image_path, caption}],
#         metadata: { doc_id, product, system, section_path,
#                     page_start, page_end, content_type,
#                     token_estimate, chunk_index } }
#     ]
#   }

# Images extracted to <output_dir>/images/ ; paths are relative ("images/..").

# Requirements:
#     Java 11+ ; pip install -U opendataloader-pdf
#     (for OCR) pip install "opendataloader-pdf[hybrid]" and a running hybrid server

# Usage:
#     python parse_manual.py m1.pdf
#     python parse_manual.py *.pdf -o out/
#     python parse_manual.py scans/ -o out/ --ocr auto --hybrid-url http://localhost:5002
#     python parse_manual.py complex.pdf --table-method cluster --reading-order xycut
#     python parse_manual.py tagged.pdf --use-struct-tree
#     python parse_manual.py secret.pdf --password hunter2
# """

# from __future__ import annotations

# import argparse
# import json
# import re
# import shutil
# import sys
# from pathlib import Path

# import opendataloader_pdf

# from config import settings
# from constants import ENGINE_LIGHTON, MODE_ENHANCED, MODE_NORMAL

# # A heading that starts a section: leading number like "8", "8.3", "2." or "2.1."
# # (trailing dot optional -- academic papers number sections "2." / "2.1."), then a title.
# SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
# # Common UNNUMBERED structural headings (papers/manuals) that should also start a
# # chunk, so e.g. the Abstract isn't merged into the title block.
# KNOWN_HEADINGS = {
#     "abstract", "keywords", "key words", "index terms", "introduction",
#     "references", "bibliography", "acknowledgment", "acknowledgments",
#     "acknowledgement", "acknowledgements", "appendix", "appendices",
#     "conclusion", "conclusions", "summary",
# }
# # TOC lines carry dot leaders ("1 Introduction ....... 7") -> exclude.
# DOTLEADER_RE = re.compile(r"\.{3,}")
# SAFETY_WORDS = ("WARNING", "DANGER", "CAUTION", "NOTICE", "ATTENTION")

# # Container node types whose kids should be walked in reading order but which
# # are not themselves emitted as content. Keeps the walker robust to nesting.
# CONTAINER_TYPES = {"document", "section", "group", "column", "div",
#                    "article", "text block", "textblock"}

# # Heuristic: below this many extracted characters per page, a doc is likely a
# # scan (image-only) with no usable text layer.
# SCANNED_CHARS_PER_PAGE = 90


# # --------------------------------------------------------------------------- #
# # Document header parsing (from the SCHEMA-ST4 title string)
# # --------------------------------------------------------------------------- #
# def parse_doc_header(raw: dict, pdf_path: Path) -> dict:
#     title = (raw.get("title") or "").strip()
#     # Some PDFs report "(anonymous)" / empty metadata; fall back to first heading.
#     if not title or title.lower() in {"(anonymous)", "untitled", "anonymous"}:
#         title = _first_heading_text(raw) or ""
#     title = title.strip()

#     doc_number = None
#     language = None
#     m = re.search(r"\b([A-Z]{2,}\d{4,}\.\d+)\b", title)
#     if m:
#         doc_number = m.group(1)
#     m = re.search(r"\b([a-z]{2}-[A-Z]{2})\b", title)
#     if m:
#         language = m.group(1)
#     # strip the doc number + language to get the human title
#     human = title
#     for tok in (doc_number, language):
#         if tok:
#             human = human.replace(tok, "")
#     human = re.sub(r"\s{2,}", " ", human).strip(" -")
#     product, subject = human, None
#     if " - " in human:
#         product, subject = [s.strip() for s in human.split(" - ", 1)]

#     # revision date from "D:20260325..." -> 2026-03-25
#     rev = None
#     cd = raw.get("creation date") or raw.get("modification date") or ""
#     m = re.search(r"D:(\d{4})(\d{2})(\d{2})", cd)
#     if m:
#         rev = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

#     doc_type = "maintenance_manual" if "mainten" in (subject or human).lower() else "manual"
#     doc_id = doc_number or pdf_path.stem
#     return {
#         "doc_id": doc_id,
#         "doc_title": human or pdf_path.stem,
#         "product": product or None,
#         "subject": subject,
#         "doc_type": doc_type,
#         "doc_number": doc_number,
#         "language": language,
#         "revision_date": rev,
#         "source_file": pdf_path.name,
#         "total_pages": raw.get("number of pages"),
#     }


# def _first_heading_text(raw: dict) -> str | None:
#     for node in iter_nodes(raw):
#         if node.get("type") == "heading":
#             c = (node.get("content") or "").strip()
#             if c and not DOTLEADER_RE.search(c):
#                 return c
#     return None


# # --------------------------------------------------------------------------- #
# # Reading-order tree walker: flatten nested containers into a linear stream.
# # This is what makes multi-column / hybrid / complex output parse correctly:
# # headings & paragraphs nested inside container nodes are still surfaced in order.
# # --------------------------------------------------------------------------- #
# def iter_nodes(root: dict):
#     """
#     Yield content nodes in reading order, descending into container nodes.

#     Emitted (leaf/semantic) nodes: heading, paragraph, caption, list, table,
#     image, and any unknown leaf that carries text content.
#     Container nodes (document/section/text block/...) are transparent: we recurse
#     into their kids instead of emitting them.
#     """
#     for kid in root.get("kids", []) or []:
#         yield from _iter_node(kid)


# def _iter_node(node: dict):
#     t = (node.get("type") or "").lower()
#     if t in CONTAINER_TYPES:
#         # transparent container: descend, do not emit the container itself
#         for kid in node.get("kids", []) or []:
#             yield from _iter_node(kid)
#         return
#     # semantic/leaf node -> emit it (its own extractor handles inner kids)
#     yield node


# def _reorder_columns(nodes: list[dict]) -> list[dict]:
#     """Re-sort one page's nodes into true column reading order (left column
#     top-to-bottom, then right column). Only reorders pages that are *clearly*
#     two-column; single-column pages, forms, and scans are left untouched.

#     Fixes multi-column PDFs where the parser's reading order interleaves columns
#     (e.g. right-column body text emitted before the left-column heading)."""
#     bb = [n.get("bounding box") for n in nodes]
#     if len(nodes) < 4 or any(b is None or len(b) < 4 for b in bb):
#         return nodes
#     pmin = min(b[0] for b in bb); pmax = max(b[2] for b in bb)
#     mid = (pmin + pmax) / 2.0; span = pmax - pmin
#     if span <= 0:
#         return nodes

#     def cls(b):
#         # spans the centre by a clear margin -> full-width (title, wide figure/table)
#         if b[0] < mid - 0.05 * span and b[2] > mid + 0.05 * span:
#             return "full"
#         return "left" if (b[0] + b[2]) / 2 < mid else "right"

#     tags = [cls(b) for b in bb]
#     if sum(t == "left" for t in tags) < 2 or sum(t == "right" for t in tags) < 2:
#         return nodes  # not confidently two-column -> keep original order

#     out, L, R = [], [], []

#     def flush():
#         L.sort(key=lambda n: -n["bounding box"][3])   # bottom-left origin: top = larger y
#         R.sort(key=lambda n: -n["bounding box"][3])
#         out.extend(L); out.extend(R); L.clear(); R.clear()

#     for i in sorted(range(len(nodes)), key=lambda i: -bb[i][3]):
#         if tags[i] == "full":
#             flush(); out.append(nodes[i])
#         elif tags[i] == "left":
#             L.append(nodes[i])
#         else:
#             R.append(nodes[i])
#     flush()
#     return out


# def ordered_nodes(raw: dict) -> list[dict]:
#     """Flatten to a reading-order stream, then fix column order per page."""
#     flat = list(iter_nodes(raw))
#     from collections import OrderedDict
#     pages = OrderedDict()
#     for n in flat:
#         pages.setdefault(n.get("page number"), []).append(n)
#     out = []
#     for pg in sorted(pages, key=lambda p: (p is None, p)):
#         out.extend(_reorder_columns(pages[pg]))
#     return out


# # --------------------------------------------------------------------------- #
# # Text extraction: turn any node (incl. nested text blocks) into readable text
# # --------------------------------------------------------------------------- #
# def table_to_markdown(node: dict) -> str:
#     rows = []
#     for row in node.get("rows", []):
#         cells = []
#         for cell in row.get("cells", []):
#             parts = [k.get("content", "") for k in cell.get("kids", []) if k.get("content")]
#             # recurse for cells that hold nested blocks (e.g. lists inside a cell)
#             if not parts:
#                 inner = " ".join(node_text(k) for k in cell.get("kids", []) if k)
#                 if inner.strip():
#                     parts = [inner]
#             cells.append(" ".join(parts).strip().replace("\n", " "))
#         rows.append(cells)
#     if not rows:
#         return ""
#     ncols = max(len(r) for r in rows)
#     rows = [r + [""] * (ncols - len(r)) for r in rows]
#     head, *body = rows
#     out = ["| " + " | ".join(head) + " |",
#            "| " + " | ".join(["---"] * ncols) + " |"]
#     out += ["| " + " | ".join(r) + " |" for r in body]
#     return "\n".join(out)


# def node_text(node: dict) -> str:
#     """Readable text for a body node, recursing into text blocks / list items."""
#     t = (node.get("type") or "").lower()
#     if t in ("paragraph", "caption", "heading", "title", "text", "footnote"):
#         return (node.get("content") or "").strip()
#     if t == "list":
#         items = []
#         for it in node.get("list items", []):
#             c = (it.get("content") or "").strip()
#             # a list item may itself hold sub-nodes
#             sub = " ".join(node_text(k) for k in it.get("kids", []) if k)
#             items.append((c + (" " + sub if sub.strip() else "")).strip())
#         return "\n".join(i for i in items if i)
#     if t == "table":
#         return table_to_markdown(node)
#     if t in ("text block", "textblock", "section", "group", "column", "div"):
#         return "\n".join(x for x in (node_text(k) for k in node.get("kids", [])) if x.strip())
#     # unknown leaf: fall back to any content string it carries
#     return (node.get("content") or "").strip()


# def is_section_heading(node: dict):
#     """Return (number, title) if this heading opens a section, else None.
#     Matches numbered headings ("8.1 Title") and common unnumbered structural
#     headings (Abstract, References, ...), so e.g. the Abstract becomes its own
#     chunk instead of being merged into the title block."""
#     if (node.get("type") or "").lower() != "heading":
#         return None
#     # OCR'd text (handwriting / scans) is too noisy to trust for section
#     # boundaries -- a misread list item like "3. puppy" must NOT start a section
#     # and drop everything above it. Let OCR pages fall to page/whole-page chunks.
#     if node.get("_ocr"):
#         return None
#     content = (node.get("content") or "").strip()
#     if not content or DOTLEADER_RE.search(content):   # skip TOC lines
#         return None
#     m = SECTION_RE.match(content)
#     if m:
#         return m.group(1), m.group(2).strip()
#     # unnumbered structural heading? (short line whose text is a known section name)
#     key = content.rstrip(":.").strip().lower()
#     if key in KNOWN_HEADINGS and len(content) < 40:
#         return key.replace(" ", "-"), content.rstrip(":.").strip()
#     return None


# # --------------------------------------------------------------------------- #
# # Image handling
# # --------------------------------------------------------------------------- #
# # Filter config (set per-run in parse_one). Drops "blank" images -- e.g. the
# # solid-black alpha-mask (SMask) layers that some PDFs store alongside real
# # images, which otherwise get extracted as junk black/white rectangles.
# _IMG_FILTER = {"drop_blank": True, "std_thresh": 4.0,
#                "uniform_frac": 0.995, "min_side": 16, "dropped": 0}


# def _is_degenerate_image(path: Path) -> bool:
#     """True if the image is effectively blank: near-uniform, all-black, all-white,
#     or a tiny sliver. Uses PIL only; if PIL is unavailable, never drops."""
#     try:
#         from PIL import Image, ImageStat
#     except Exception:
#         return False
#     try:
#         im = Image.open(path)
#         im = im.convert("L")
#     except Exception:
#         return False
#     w, h = im.size
#     if w < _IMG_FILTER["min_side"] or h < _IMG_FILTER["min_side"]:
#         return True
#     if ImageStat.Stat(im).stddev[0] < _IMG_FILTER["std_thresh"]:
#         return True  # near-uniform (solid colour / mask layer)
#     hist = im.histogram()
#     n = sum(hist) or 1
#     white = sum(hist[250:]) / n
#     black = sum(hist[:6]) / n
#     return white > _IMG_FILTER["uniform_frac"] or black > _IMG_FILTER["uniform_frac"]


# def copy_image(node, raw_dir: Path, images_dir: Path, stem: str, idx: int):
#     src_rel = node.get("source")
#     if not src_rel:
#         return None
#     src = (raw_dir / src_rel).resolve()
#     if not src.exists():
#         # some builds emit an absolute path or a different relative root
#         alt = Path(src_rel)
#         if alt.exists():
#             src = alt.resolve()
#         else:
#             return None
#     ext = src.suffix.lower() or ".png"
#     page = node.get("page number")
#     name = f"{stem}_p{page}_{idx}{ext}"
#     dst = (images_dir / name).resolve()
#     shutil.copy2(src, dst)
#     if _IMG_FILTER["drop_blank"] and _is_degenerate_image(dst):
#         try:
#             dst.unlink()
#         except OSError:
#             pass
#         _IMG_FILTER["dropped"] += 1
#         return None
#     return f"images/{name}"


# def image_caption(node) -> str | None:
#     desc = node.get("description")
#     if desc:
#         return desc
#     alt = node.get("alt_source")
#     if alt and str(alt).lower() != "missing":
#         return alt
#     return None


# # --------------------------------------------------------------------------- #
# # Core: segment the reading-order stream into subsection chunks
# # --------------------------------------------------------------------------- #
# def build_chunks(raw: dict, header: dict, raw_dir: Path,
#                  images_dir: Path, stem: str) -> list[dict]:
#     doc_id = header["doc_id"]
#     product = header["product"]
#     path_stack: dict[int, str] = {}       # depth -> "N Title"
#     chunks: list[dict] = []
#     # Start with a "front matter" bucket so content before the first numbered
#     # heading (title, authors, abstract) is captured rather than dropped. TOC
#     # lines (dot leaders) are skipped so they don't pollute it.
#     cur = {
#         "section_number": "0", "section_title": "Front matter", "system": None,
#         "section_path": ["0 Front matter"], "page_start": None, "page_end": None,
#         "_body": [], "images": [], "_regions": [], "_front": True,
#     }
#     img_idx = 0

#     def close(c):
#         if not c:
#             return
#         body = "\n".join(x for x in c["_body"] if x.strip()).strip()
#         if not body and not c["images"]:
#             return  # drop empty container headings (e.g. a chapter with no intro)
#         if c.get("_front") and len(body) < 120 and not c["images"]:
#             return  # skip trivial front matter (e.g. a bare title page)
#         if c.get("_front") or not c["section_number"][:1].isdigit():
#             title_line = c["section_title"]
#         else:
#             title_line = f"{c['section_number']} {c['section_title']}".strip()
#         text = (title_line + "\n" + body).strip() if body else title_line
#         low = text.lower()
#         if re.search(r"(^|\n)\s*\d+\.\s", body):
#             ctype = "procedure"
#         elif any(w.lower() in low for w in SAFETY_WORDS) and len(body) < 400:
#             ctype = "safety"
#         else:
#             ctype = "section"
#         chunks.append({
#             "chunk_id": f"{doc_id}#{c['section_number']}",
#             "section_number": c["section_number"],
#             "section_title": c["section_title"],
#             "text": text,
#             "images": c["images"],
#             "regions": c["_regions"],
#             "metadata": {
#                 "doc_id": doc_id,
#                 "product": product,
#                 "system": c["system"],
#                 "section_path": c["section_path"],
#                 "page_start": c["page_start"],
#                 "page_end": c["page_end"],
#                 "content_type": ctype,
#                 "token_estimate": max(1, len(text) // 4),
#                 "chunk_index": len(chunks),
#             },
#         })

#     def find_nested_images(node):
#         """Walk a node's subtree (list items' kids, table cells' kids, ...) and
#         yield every 'image' leaf found, however deep. node_text() already
#         recurses into these same structures for TEXT, but never surfaces
#         images -- this fills that gap so a figure nested under a numbered
#         step (very common: "step 5, here's its diagram") isn't silently
#         dropped just because it isn't a top-level sibling node."""
#         if not isinstance(node, dict):
#             return
#         t = (node.get("type") or "").lower()
#         if t == "image":
#             yield node
#             return
#         # list: descend into each list item's kids
#         for it in node.get("list items", []) or []:
#             for kid in it.get("kids", []) or []:
#                 yield from find_nested_images(kid)
#         # table: descend into each cell's kids
#         for row in node.get("rows", []) or []:
#             for cell in row.get("cells", []) or []:
#                 for kid in cell.get("kids", []) or []:
#                     yield from find_nested_images(kid)
#         # generic containers (text block, etc.)
#         for kid in node.get("kids", []) or []:
#             yield from find_nested_images(kid)

#     def add_region(c, node):
#         bb = node.get("bounding box")
#         pg = node.get("page number")
#         if not c or not bb or pg is None:
#             return
#         txt = node_text(node)
#         c["_regions"].append({
#             "page": pg,
#             "bbox": [round(float(v), 2) for v in bb[:4]],
#             "type": (node.get("type") or "").lower(),
#             "text": txt[:400],
#         })

#     for node in ordered_nodes(raw):       # reading-order walk + column re-sort
#         sec = is_section_heading(node)
#         page = node.get("page number")
#         if sec:
#             number, title = sec
#             depth = number.count(".") + 1
#             # update the hierarchy stack
#             path_stack[depth] = f"{number} {title}"
#             for d in list(path_stack):
#                 if d > depth:
#                     del path_stack[d]
#             close(cur)
#             system = re.sub(r"^\d+(?:\.\d+)*\s+", "", path_stack.get(1, title))
#             cur = {
#                 "section_number": number,
#                 "section_title": title,
#                 "system": system,
#                 "section_path": [path_stack[d] for d in sorted(path_stack)],
#                 "page_start": page,
#                 "page_end": page,
#                 "_body": [],
#                 "images": [],
#                 "_regions": [],
#             }
#             add_region(cur, node)          # the heading itself is a region
#             continue

#         if cur.get("_front"):
#             # accumulate front matter, but skip table-of-contents dot-leader lines
#             txt = node_text(node)
#             if txt.strip() and not DOTLEADER_RE.search(txt) and \
#                (node.get("type") or "").lower() != "image":
#                 if cur["page_start"] is None:
#                     cur["page_start"] = page
#                 cur["_body"].append(txt)
#                 add_region(cur, node)
#                 if page is not None:
#                     cur["page_end"] = page
#             continue

#         if page is not None:
#             cur["page_end"] = page
#         node_type = (node.get("type") or "").lower()
#         if node_type == "image":
#             img_idx += 1
#             p = copy_image(node, raw_dir, images_dir, stem, img_idx)
#             if p:
#                 cur["images"].append({"image_path": p, "caption": image_caption(node)})
#         else:
#             cur["_body"].append(node_text(node))
#             add_region(cur, node)
#             if node_type in ("list", "table"):
#                 # node_text() already pulled the TEXT out of nested list
#                 # items / table cells; separately pull out any images that
#                 # were hiding in those same nested kids (e.g. the diagram
#                 # for step 5 of a numbered procedure).
#                 for img_node in find_nested_images(node):
#                     img_idx += 1
#                     p = copy_image(img_node, raw_dir, images_dir, stem, img_idx)
#                     if p:
#                         cur["images"].append({"image_path": p, "caption": image_caption(img_node)})

#     close(cur)
#     return chunks


# # --------------------------------------------------------------------------- #
# # Fallback chunking for PDFs with NO numbered headings (scans, brochures, etc.)
# # Groups the reading-order stream by page so RAG still gets usable units.
# # --------------------------------------------------------------------------- #
# def build_fallback_chunks(raw: dict, header: dict, raw_dir: Path,
#                           images_dir: Path, stem: str) -> list[dict]:
#     doc_id = header["doc_id"]
#     product = header["product"]
#     pages: dict[int, dict] = {}
#     img_idx = 0

#     for node in ordered_nodes(raw):
#         page = node.get("page number") or 0
#         bucket = pages.setdefault(page, {"_body": [], "images": [], "regions": []})
#         if (node.get("type") or "").lower() == "image":
#             img_idx += 1
#             p = copy_image(node, raw_dir, images_dir, stem, img_idx)
#             if p:
#                 bucket["images"].append({"image_path": p, "caption": image_caption(node)})
#         else:
#             txt = node_text(node)
#             if txt.strip():
#                 bucket["_body"].append(txt)
#                 bb = node.get("bounding box")
#                 if bb:
#                     bucket["regions"].append({
#                         "page": page,
#                         "bbox": [round(float(v), 2) for v in bb[:4]],
#                         "type": (node.get("type") or "").lower(),
#                         "text": txt[:400],
#                     })

#     chunks: list[dict] = []
#     for page in sorted(pages):
#         b = pages[page]
#         body = "\n".join(x for x in b["_body"] if x.strip()).strip()
#         if not body and not b["images"]:
#             continue
#         sec_num = f"p{page}"
#         text = (f"Page {page}\n" + body).strip() if body else f"Page {page}"
#         chunks.append({
#             "chunk_id": f"{doc_id}#{sec_num}",
#             "section_number": sec_num,
#             "section_title": f"Page {page}",
#             "text": text,
#             "images": b["images"],
#             "regions": b["regions"],
#             "metadata": {
#                 "doc_id": doc_id,
#                 "product": product,
#                 "system": None,
#                 "section_path": [f"Page {page}"],
#                 "page_start": page,
#                 "page_end": page,
#                 "content_type": "page",
#                 "token_estimate": max(1, len(text) // 4),
#                 "chunk_index": len(chunks),
#             },
#         })
#     return chunks


# # --------------------------------------------------------------------------- #
# # Built-in (in-process) OCR: render scanned pages and OCR them locally, then
# # splice the recovered text back into the parse so it lands in the same JSON.
# # No external server required. Engines: tesseract (default), easyocr, or lighton.
# # --------------------------------------------------------------------------- #
# _EASYOCR_READER = None
# _LIGHTON_MODEL = None
# _LIGHTON_PROCESSOR = None
# _LIGHTON_DEVICE = None
# _LIGHTON_DTYPE = None
# # At or below this mean Tesseract word confidence, a page is treated as
# # handwritten / hard and (in auto mode) the WHOLE page is re-OCR'd with
# # LightOnOCR-2-1B. Tunable via --handwriting-threshold.
# HANDWRITING_CONF_THRESHOLD = settings.HANDWRITING_CONF_THRESHOLD
# # Minimum confidently-recognized words before a *low-confidence* escalation is
# # considered (separate from the *zero-words* escalation, which always fires).
# HANDWRITING_MIN_WORDS = settings.HANDWRITING_MIN_WORDS


# def _tesseract_confidence(png: bytes, lang: str) -> tuple[float, int]:
#     """Return (mean word confidence 0-100, number of confident words) for a page.
#     Used to decide whether Tesseract struggled (handwriting / poor scan)."""
#     import io
#     import pytesseract
#     from PIL import Image
#     data = pytesseract.image_to_data(Image.open(io.BytesIO(png)), lang=lang,
#                                      output_type=pytesseract.Output.DICT)
#     confs = []
#     for txt, c in zip(data.get("text", []), data.get("conf", [])):
#         if not (txt or "").strip():
#             continue
#         try:
#             c = float(c)
#         except (TypeError, ValueError):
#             continue
#         if c >= 0:
#             confs.append(c)
#     if not confs:
#         return 0.0, 0
#     return sum(confs) / len(confs), len(confs)


# def _lighton_pick_device() -> str:
#     import torch
#     if torch.cuda.is_available():
#         return "cuda"
#     if torch.backends.mps.is_available():
#         return "mps"
#     return "cpu"


# def _lighton_cpu_dtype(torch):
#     """Choose BF16 only when the host advertises native CPU instructions.

#     Emulated BF16 is slower than FP32, which is why this is feature-gated
#     rather than selected merely because PyTorch exposes ``torch.bfloat16``.
#     """
#     configured = settings.LIGHTON_CPU_DTYPE
#     if configured in {"float32", "fp32"}:
#         return torch.float32
#     if configured in {"bfloat16", "bf16"}:
#         return torch.bfloat16
#     try:
#         flags = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").lower()
#     except OSError:
#         flags = ""
#     native_bf16 = "avx512_bf16" in flags or "sve_bf16" in flags
#     return torch.bfloat16 if native_bf16 else torch.float32


# def _load_lighton_model(device: str | None = None):
#     """Lazily load LightOnOCR-2-1B + its processor once per process."""
#     global _LIGHTON_MODEL, _LIGHTON_PROCESSOR, _LIGHTON_DEVICE, _LIGHTON_DTYPE
#     try:
#         import torch
#         from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor
#     except Exception as e:  # noqa: BLE001
#         raise RuntimeError(
#             "LightOnOCR-2-1B is not installed. Install it with:\n"
#             "  pip install \"transformers>=5.0.0\" torch pillow\n"
#             f"or use --ocr-engine tesseract. [{e}]")

#     resolved_device = device or _lighton_pick_device()
#     if _LIGHTON_MODEL is None or _LIGHTON_DEVICE != resolved_device:
#         if resolved_device == "cpu":
#             n_threads = settings.LIGHTON_CPU_THREADS or torch.get_num_threads()
#             torch.set_num_threads(n_threads)
#             print(f"    ! LightOnOCR-2-1B is running on CPU ({n_threads} thread(s)) -- "
#                   "token-by-token generation at this size is slow (roughly minutes per "
#                   "dense page). If a GPU is available, install a CUDA-enabled torch "
#                   "build to speed this up.", file=sys.stderr)
#         dtype = _lighton_cpu_dtype(torch) if resolved_device == "cpu" else torch.bfloat16
#         # PyTorch dynamic quantization consumes FP32 weights. INT8 is an
#         # explicit alternative to BF16, not an additional conversion layer.
#         if resolved_device == "cpu" and settings.LIGHTON_QUANTIZE_CPU:
#             dtype = torch.float32
#         model = LightOnOcrForConditionalGeneration.from_pretrained(
#             "lightonai/LightOnOCR-2-1B", torch_dtype=dtype
#         ).to(resolved_device)
#         if resolved_device == "cpu" and settings.LIGHTON_QUANTIZE_CPU:
#             print("    lighton: applying dynamic int8 quantization (LIGHTON_QUANTIZE_CPU=1)",
#                   file=sys.stderr)
#             model = torch.quantization.quantize_dynamic(
#                 model, {torch.nn.Linear}, dtype=torch.qint8
#             )
#         model.eval()
#         _LIGHTON_MODEL = model
#         _LIGHTON_PROCESSOR = LightOnOcrProcessor.from_pretrained("lightonai/LightOnOCR-2-1B")
#         _LIGHTON_DEVICE = resolved_device
#         _LIGHTON_DTYPE = dtype
#     return _LIGHTON_MODEL, _LIGHTON_PROCESSOR, _LIGHTON_DEVICE, _LIGHTON_DTYPE


# def _clean_lighton_markdown(text: str) -> str:
#     """LightOnOCR-2-1B transcribes pages as rich Markdown/HTML (tables, div
#     layout wrappers, image placeholders for logos/figures), but this pipeline
#     stores plain OCR text nodes -- it never actually extracts and saves those
#     referenced images. Left as-is, that markup leaks into chunk text as
#     literal '<div ...>', '<table>', '##', '**bold**', and broken
#     '![image](image_1.png)' links pointing at files that don't exist. Turn it
#     into clean plain text instead."""
#     # Dangling image references -- no image is ever actually saved for these,
#     # so the link is always broken. Drop them entirely.
#     text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)

#     # Simple HTML tables -> one readable "cell | cell | cell" line per row.
#     text = re.sub(r"</t[dh]>", " | ", text, flags=re.IGNORECASE)
#     text = re.sub(r"<t[dh][^>]*>", "", text, flags=re.IGNORECASE)
#     text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
#     text = re.sub(r"<tr[^>]*>|</?table[^>]*>|</?thead[^>]*>|</?tbody[^>]*>",
#                   "", text, flags=re.IGNORECASE)

#     # Any other stray HTML (div layout wrappers, <br/>, <sup>, etc.) -> drop
#     # the tags, keep the text between them.
#     text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
#     text = re.sub(r"<[^>]+>", "", text)

#     # Markdown syntax that only makes sense when rendered, not as plain text.
#     text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)   # ## Heading
#     text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                 # **bold**
#     text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)  # *italic*
#     text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)   # --- rules

#     # Trailing " | " left over from a row's last cell, and blank-cell noise.
#     text = re.sub(r"\s*\|\s*$", "", text, flags=re.MULTILINE)
#     text = re.sub(r"^\s*\|\s*", "", text, flags=re.MULTILINE)
#     return text


# def _ocr_lighton(png: bytes, dpi: int, page_h_pt: float | None,
#                  device: str | None = None, max_new_tokens: int = 2048) -> list[dict]:
#     """Run LightOnOCR-2-1B (VLM OCR, good on handwriting) on a page image.
#     Returns line records [{text}] -- LightOnOCR returns full-page transcribed
#     text rather than per-block boxes, so lines carry no bbox (they render as
#     whole-page text instead of individually boxed regions). Raises
#     RuntimeError with a clear message if LightOnOCR-2-1B isn't installed.

#     max_new_tokens trades off completeness vs. speed: generation is
#     token-by-token, so on CPU a higher cap can turn into several minutes for
#     a single dense page (references lists, big tables). 2048 is a middle
#     ground -- raise it (e.g. --lighton-max-tokens 4096) if pages still get
#     cut off, lower it if you'd rather risk truncation than wait."""
#     import io
#     import time

#     from PIL import Image

#     model, processor, resolved_device, dtype = _load_lighton_model(device)

#     image = Image.open(io.BytesIO(png)).convert("RGB")
#     conversation = [{"role": "user", "content": [{"type": "image", "image": image}]}]
#     inputs = processor.apply_chat_template(
#         conversation, add_generation_prompt=True, tokenize=True,
#         return_dict=True, return_tensors="pt",
#     )
#     inputs = {
#         k: v.to(device=resolved_device, dtype=dtype) if v.is_floating_point() else v.to(resolved_device)
#         for k, v in inputs.items()
#     }

#     t0 = time.perf_counter()
#     # Disable autograd metadata and retain the decoder KV cache. Both OCR
#     # entry points arrive here, so this speeds automatic escalation and the
#     # user-triggered Enhance pass equally. `inference_mode` is stronger than
#     # `no_grad` and is safe because this process never trains the model.
#     import torch
#     with torch.inference_mode():
#         output_ids = model.generate(
#             **inputs,
#             max_new_tokens=max_new_tokens,
#             do_sample=False,
#             use_cache=True,
#         )
#     elapsed = time.perf_counter() - t0
#     generated_ids = output_ids[0, inputs["input_ids"].shape[1]:]
#     n_tokens = generated_ids.shape[0]
#     hit_cap = n_tokens >= max_new_tokens
#     print(f"    lighton: {n_tokens} tokens in {elapsed:.1f}s on {resolved_device}"
#           + (" (hit the cap -- page may be truncated, consider raising max_new_tokens)"
#              if hit_cap else ""), file=sys.stderr)
#     text = processor.decode(generated_ids, skip_special_tokens=True)
#     text = _clean_lighton_markdown(text)

#     lines: list[dict] = []
#     for raw_line in text.splitlines():
#         s = raw_line.strip()
#         if s:
#             lines.append({"text": s})
#     return lines


# def _lines_to_nodes(lines: list[dict], page: int, tag: str) -> list[dict]:
#     nodes = []
#     for ln in lines:
#         s = ln["text"]
#         is_head = bool(SECTION_RE.match(s)) and not DOTLEADER_RE.search(s)
#         nd = {"type": "heading" if is_head else "paragraph", "page number": page,
#               "content": s, "_ocr": True, "_engine": tag}
#         if ln.get("bbox"):
#             nd["bounding box"] = ln["bbox"]
#         nodes.append(nd)
#     return nodes


# def _render_page_png(pdf_path: Path, page_number: int, dpi: int) -> bytes:
#     import pymupdf  # lazy: only needed when OCR runs
#     doc = pymupdf.open(str(pdf_path))
#     try:
#         pix = doc[page_number - 1].get_pixmap(dpi=dpi)
#         return pix.tobytes("png")
#     finally:
#         doc.close()


# def get_page_sizes(pdf_path: Path) -> dict:
#     """Return {page_number: (width_pt, height_pt)} using PyMuPDF, or {} if
#     unavailable. Needed to convert OCR pixel boxes to PDF points and to give the
#     UI the page dimensions for overlay scaling."""
#     try:
#         import pymupdf
#     except Exception:
#         return {}
#     try:
#         doc = pymupdf.open(str(pdf_path))
#     except Exception:
#         return {}
#     try:
#         return {i + 1: (round(doc[i].rect.width, 2), round(doc[i].rect.height, 2))
#                 for i in range(doc.page_count)}
#     finally:
#         doc.close()


# def _ocr_tesseract(png: bytes, lang: str) -> str:
#     import io
#     import pytesseract
#     from PIL import Image
#     return pytesseract.image_to_string(Image.open(io.BytesIO(png)), lang=lang)


# def _ocr_tesseract_lines(png: bytes, lang: str, dpi: int, page_h_pt: float) -> list[dict]:
#     """OCR into line records with bounding boxes in PDF points (bottom-left origin),
#     so scanned pages get the same coordinate space as native pages."""
#     import io
#     import pytesseract
#     from PIL import Image
#     data = pytesseract.image_to_data(Image.open(io.BytesIO(png)), lang=lang,
#                                      output_type=pytesseract.Output.DICT)
#     scale = 72.0 / dpi
#     lines: dict[tuple, dict] = {}
#     n = len(data["text"])
#     for i in range(n):
#         txt = (data["text"][i] or "").strip()
#         if not txt:
#             continue
#         key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
#         L, T, W, H = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
#         rec = lines.setdefault(key, {"words": [], "L": L, "T": T, "R": L + W, "B": T + H})
#         rec["words"].append(txt)
#         rec["L"] = min(rec["L"], L); rec["T"] = min(rec["T"], T)
#         rec["R"] = max(rec["R"], L + W); rec["B"] = max(rec["B"], T + H)
#     out = []
#     for key in sorted(lines):
#         r = lines[key]
#         text = " ".join(r["words"]).strip()
#         if not text:
#             continue
#         x0 = r["L"] * scale
#         x1 = r["R"] * scale
#         y1 = page_h_pt - r["T"] * scale   # top edge -> larger y (bottom-left origin)
#         y0 = page_h_pt - r["B"] * scale
#         out.append({"text": text, "bbox": [round(x0, 2), round(y0, 2),
#                                            round(x1, 2), round(y1, 2)]})
#     return out


# def _ocr_easyocr(png: bytes, lang: str) -> str:
#     import io
#     import numpy as np
#     import easyocr
#     from PIL import Image
#     global _EASYOCR_READER
#     langs = [l.strip() for l in lang.replace("+", ",").split(",") if l.strip()] or ["en"]
#     if _EASYOCR_READER is None:
#         _EASYOCR_READER = easyocr.Reader(langs, gpu=False)
#     arr = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
#     lines = _EASYOCR_READER.readtext(arr, detail=0, paragraph=True)
#     return "\n".join(lines)


# def ocr_page_nodes(pdf_path: Path, page: int, engine: str, lang: str, dpi: int,
#                    page_h_pt: float | None, opts: dict | None = None) -> list[dict]:
#     """OCR one page into parse nodes.
#     - engine 'lighton'           -> always LightOnOCR-2-1B (handwriting-capable)
#     - engine 'easyocr'           -> EasyOCR (text only)
#     - engine 'tesseract' (auto)  -> Tesseract; if handwriting mode is 'auto' and
#       Tesseract confidence is low, escalate that page to LightOnOCR-2-1B; if
#       'force', always use LightOnOCR-2-1B. Falls back to Tesseract if
#       LightOnOCR is unavailable."""
#     opts = opts or {}
#     hw = opts.get("handwriting", "auto")
#     device = opts.get("lighton_device")
#     max_new_tokens = int(opts.get("lighton_max_new_tokens") or settings.LIGHTON_MAX_NEW_TOKENS)
#     png = _render_page_png(pdf_path, page, dpi)

#     if engine == "lighton":
#         return _lines_to_nodes(_ocr_lighton(png, dpi, page_h_pt, device, max_new_tokens), page, "lighton")

#     if engine == "easyocr":
#         return ocr_text_to_nodes(_ocr_easyocr(png, lang), page, engine_tag="easyocr")

#     # --- Tesseract path, with optional handwriting escalation to LightOnOCR-2-1B ---
#     if hw == "force":
#         try:
#             return _lines_to_nodes(_ocr_lighton(png, dpi, page_h_pt, device, max_new_tokens), page, "lighton")
#         except Exception as e:  # noqa: BLE001
#             print(f"    ! p{page}: LightOnOCR-2-1B failed ({e}); using Tesseract", file=sys.stderr)
#     elif hw == "auto":
#         try:
#             conf, nwords = _tesseract_confidence(png, lang)
#         except Exception:  # noqa: BLE001
#             conf, nwords = 100.0, 0
#         threshold = float(opts.get("handwriting_threshold", HANDWRITING_CONF_THRESHOLD))
#         min_words = int(opts.get("handwriting_min_words", HANDWRITING_MIN_WORDS))
#         # Escalate to LightOnOCR-2-1B when EITHER:
#         #   (a) Tesseract found enough words to have an opinion, but that opinion
#         #       is low-confidence (nwords >= min_words and conf <= threshold), OR
#         #   (b) Tesseract found essentially NOTHING (nwords == 0). This used to be
#         #       excluded by the old "nwords >= 3" guard, which meant total OCR
#         #       failure (the worst case -- e.g. cursive handwriting Tesseract can't
#         #       even segment into words) never triggered escalation. Zero words is
#         #       not "nothing to OCR here", it's the strongest possible signal that
#         #       Tesseract is the wrong tool for this page.
#         low_confidence = nwords >= min_words and conf <= threshold
#         total_failure = nwords == 0
#         if low_confidence or total_failure:
#             reason = (f"confidence {conf:.0f} <= {threshold:.0f}" if low_confidence
#                       else "Tesseract found 0 usable words")
#             print(f"    p{page}: {reason} -> whole page to LightOnOCR-2-1B (handwriting)",
#                   file=sys.stderr)
#             try:
#                 return _lines_to_nodes(_ocr_lighton(png, dpi, page_h_pt, device, max_new_tokens), page, "lighton")
#             except Exception as e:  # noqa: BLE001
#                 print(f"    ! p{page}: LightOnOCR-2-1B unavailable ({e}); using Tesseract",
#                       file=sys.stderr)

#     # default Tesseract output (boxed if page size known)
#     if not page_h_pt:
#         return ocr_text_to_nodes(_ocr_tesseract(png, lang), page, engine_tag="tesseract")
#     nodes = []
#     for ln in _ocr_tesseract_lines(png, lang, dpi, page_h_pt):
#         s = ln["text"]
#         is_head = bool(SECTION_RE.match(s)) and not DOTLEADER_RE.search(s)
#         nodes.append({"type": "heading" if is_head else "paragraph",
#                       "page number": page, "content": s,
#                       "bounding box": ln["bbox"], "_ocr": True, "_engine": "tesseract"})
#     return nodes


# def ocr_text_to_nodes(text: str, page: int, engine_tag: str = "tesseract") -> list[dict]:
#     """
#     Turn a page's OCR text into parse nodes. Lines that look like numbered
#     section headings ("9 Cooling System") become heading nodes so they slot
#     into the section hierarchy; everything else becomes paragraph text.
#     """
#     nodes: list[dict] = []
#     buf: list[str] = []

#     def flush():
#         if buf:
#             nodes.append({"type": "paragraph", "page number": page,
#                           "content": " ".join(buf).strip(), "_ocr": True,
#                           "_engine": engine_tag})
#             buf.clear()

#     for raw_line in text.splitlines():
#         s = raw_line.strip()
#         if not s:
#             flush()
#             continue
#         if SECTION_RE.match(s) and not DOTLEADER_RE.search(s):
#             flush()
#             nodes.append({"type": "heading", "page number": page,
#                           "content": s, "_ocr": True, "_engine": engine_tag})
#         else:
#             buf.append(s)
#     flush()
#     return nodes


# def splice_ocr_nodes(raw: dict, page_nodes: dict[int, list[dict]]) -> None:
#     """Insert OCR nodes into raw['kids'] in document (page) order, in place."""
#     kids = raw.get("kids", []) or []
#     out: list[dict] = []
#     done: set[int] = set()

#     def flush_upto(pg):
#         for spg in sorted(page_nodes):
#             if spg in done:
#                 continue
#             if pg is None or spg <= pg:
#                 out.extend(page_nodes[spg])
#                 done.add(spg)

#     for node in kids:
#         pg = node.get("page number")
#         if pg is not None:
#             flush_upto(pg)
#         out.append(node)
#     flush_upto(None)
#     raw["kids"] = out


# def run_builtin_ocr(pdf_path: Path, pages: list[int], raw: dict,
#                     engine: str, lang: str, dpi: int,
#                     page_sizes: dict | None = None, opts: dict | None = None) -> dict[int, str]:
#     """OCR the given page numbers in-process and splice results into raw.
#     Returns {page_number: engine_tag} for pages that yielded text -- tag is
#     whichever engine actually produced that page's text ('tesseract' or
#     'lighton'), which may differ from the requested `engine` when handwriting
#     auto/force escalation kicked in."""
#     page_sizes = page_sizes or {}
#     page_nodes: dict[int, list[dict]] = {}
#     page_engine: dict[int, str] = {}
#     for pg in pages:
#         page_h = page_sizes.get(pg, (None, None))[1]
#         try:
#             nodes = ocr_page_nodes(pdf_path, pg, engine, lang, dpi, page_h, opts)
#         except Exception as e:  # noqa: BLE001
#             print(f"    ! OCR failed on page {pg}: {e}", file=sys.stderr)
#             continue
#         if nodes:
#             page_nodes[pg] = nodes
#             page_engine[pg] = nodes[0].get("_engine", engine)
#     if page_nodes:
#         splice_ocr_nodes(raw, page_nodes)
#     return page_engine


# # --------------------------------------------------------------------------- #
# # Page-level scan analysis: classify each page as digital or scanned so that
# # MIXED PDFs (some real-text pages + some scanned pages) are handled correctly.
# # --------------------------------------------------------------------------- #
# # Below this fraction of a page's area covered by raster images, a page is NOT
# # considered a scan even if its extractable text is sparse -- a decorative
# # logo or a small inline photo shouldn't be enough to trigger a scanned-page
# # classification (and the OCR pass that comes with it) on an otherwise blank
# # digital title/section-divider page.
# SCANNED_IMAGE_COVERAGE = 0.6


# def _pymupdf_page_signals(pdf_path: Path) -> dict[int, dict]:
#     """Independent per-page cross-check via PyMuPDF: real extractable text
#     length and raster-image area coverage (0..1 fraction of the page).
#     Returns {} if PyMuPDF isn't available or the file can't be opened --
#     callers must fall back to the opendataloader-only heuristic in that case.
#     """
#     try:
#         import pymupdf
#     except Exception:
#         return {}
#     try:
#         doc = pymupdf.open(str(pdf_path))
#     except Exception:
#         return {}
#     out: dict[int, dict] = {}
#     try:
#         for i in range(doc.page_count):
#             page = doc[i]
#             text_len = len((page.get_text("text") or "").strip())
#             page_area = page.rect.width * page.rect.height
#             img_area = 0.0
#             if page_area > 0:
#                 for img in page.get_images(full=True):
#                     xref = img[0]
#                     try:
#                         rects = page.get_image_rects(xref)
#                     except Exception:  # noqa: BLE001
#                         rects = []
#                     for r in rects:
#                         img_area += abs(r.width * r.height)
#             coverage = min(1.0, img_area / page_area) if page_area else 0.0
#             out[i + 1] = {"text_len": text_len, "img_coverage": coverage}
#     finally:
#         doc.close()
#     return out


# def analyze_pages(raw: dict, pdf_path: Path | None = None) -> dict:
#     per_page: dict[int, dict] = {}
#     for node in iter_nodes(raw):
#         pg = node.get("page number")
#         if pg is None:
#             continue
#         b = per_page.setdefault(pg, {"chars": 0, "images": 0})
#         if (node.get("type") or "").lower() == "image":
#             b["images"] += 1
#         else:
#             b["chars"] += len(node_text(node))

#     total = raw.get("number of pages") or (max(per_page) if per_page else 1)

#     # Cross-check with PyMuPDF when a pdf_path is supplied: this replaces the
#     # crude "does this page have >= 1 embedded image at all" signal with
#     # "does a raster image cover a SUBSTANTIAL fraction of the page" -- a
#     # small logo/icon on an otherwise sparse title page no longer trips a
#     # scanned-page classification, since it never approaches
#     # SCANNED_IMAGE_COVERAGE. It also cross-checks the char count against
#     # PyMuPDF's own independent text extraction rather than trusting
#     # opendataloader's node text alone.
#     mupdf_info = _pymupdf_page_signals(pdf_path) if pdf_path is not None else {}

#     scanned_pages = []
#     likely_prescanned_with_text = []  # diagnostic only, see note below
#     for pg in range(1, int(total) + 1):
#         b = per_page.get(pg, {"chars": 0, "images": 0})
#         chars = b["chars"]
#         m = mupdf_info.get(pg)
#         if m is not None:
#             is_scanned = (chars < SCANNED_CHARS_PER_PAGE
#                           and m["text_len"] < SCANNED_CHARS_PER_PAGE
#                           and m["img_coverage"] >= SCANNED_IMAGE_COVERAGE)
#             # A page that's mostly covered by one big raster image but STILL
#             # has real extractable text is very likely a scan that already
#             # carries a baked-in (possibly low-quality) OCR text layer from
#             # whatever tool produced the PDF. It's correctly left off
#             # scanned_pages (it has real text, no need to re-OCR by default),
#             # but flagging it separately means a caller could offer "this
#             # looks like a pre-OCR'd scan -- re-run with Enhanced Mode
#             # anyway?" instead of silently trusting text of unknown quality.
#             if (not is_scanned and m["img_coverage"] >= SCANNED_IMAGE_COVERAGE
#                     and chars >= SCANNED_CHARS_PER_PAGE):
#                 likely_prescanned_with_text.append(pg)
#         else:
#             # PyMuPDF unavailable/failed for this file -- fall back to the
#             # original heuristic rather than silently reclassifying everything.
#             is_scanned = chars < SCANNED_CHARS_PER_PAGE and b["images"] >= 1
#         if is_scanned:
#             scanned_pages.append(pg)

#     n_scanned = len(scanned_pages)
#     if n_scanned == 0:
#         classification = "digital"
#     elif n_scanned >= int(total):
#         classification = "scanned"
#     else:
#         classification = "mixed"
#     total_chars = sum(b["chars"] for b in per_page.values())
#     return {
#         "total_pages": int(total),
#         "n_scanned": n_scanned,
#         "scanned_pages": scanned_pages,
#         "classification": classification,
#         "total_chars": total_chars,
#         # diagnostic only, not used to gate OCR: pages that look like a scan
#         # by image coverage but already carry real text of unknown quality.
#         "likely_prescanned_with_text": likely_prescanned_with_text,
#     }


# # --------------------------------------------------------------------------- #
# def _run_convert(pdf_path: Path, raw_dir: Path, opts: dict, hybrid_kwargs: dict | None):
#     """Call opendataloader_pdf.convert with the given options; return parsed JSON."""
#     kw = dict(
#         input_path=[str(pdf_path)],
#         output_dir=str(raw_dir),
#         format="json",
#         image_output=opts["image_output"],
#         image_format=opts["image_format"],
#         reading_order=opts["reading_order"],
#         table_method=opts["table_method"],
#         use_struct_tree=opts["use_struct_tree"],
#         include_header_footer=opts["include_header_footer"],
#         detect_strikethrough=opts["detect_strikethrough"],
#         keep_line_breaks=opts["keep_line_breaks"],
#         quiet=opts["quiet"],
#     )
#     if opts.get("password"):
#         kw["password"] = opts["password"]
#     if opts.get("pages"):
#         kw["pages"] = opts["pages"]
#     if opts.get("content_safety_off"):
#         kw["content_safety_off"] = opts["content_safety_off"]
#     if opts.get("threads"):
#         kw["threads"] = str(opts["threads"])
#     if hybrid_kwargs:
#         kw.update(hybrid_kwargs)
#     opendataloader_pdf.convert(**kw)

#     raw_json = raw_dir / f"{pdf_path.stem}.json"
#     if not raw_json.exists():
#         raise FileNotFoundError(f"Expected raw output missing: {raw_json}")
#     return json.loads(raw_json.read_text(encoding="utf-8"))


# def _hybrid_kwargs(opts: dict, *, full_pages: bool, hancom_force: bool = False) -> dict:
#     """
#     Build hybrid kwargs for an OCR run.

#     Backend default is 'docling-fast' -- that is the server you get from
#     `pip install "opendataloader-pdf[hybrid]"` + `opendataloader-pdf-hybrid`,
#     which runs IBM Docling with EasyOCR by default. The hancom-ai backend is a
#     separate service and takes the hancom-only OCR-strategy flag.
#     """
#     backend = opts.get("hybrid") or "docling-fast"
#     kw = {
#         "hybrid": backend,
#         # 'full' sends every page to the backend (right for a scanned doc);
#         # 'auto' lets the Java pipeline triage which pages need it.
#         "hybrid_mode": opts.get("hybrid_mode") or ("full" if full_pages else "auto"),
#         "hybrid_fallback": opts.get("hybrid_fallback", True),
#     }
#     if backend == "hancom-ai":
#         kw["hybrid_hancom_ai_ocr_strategy"] = "force" if hancom_force else "auto"
#     if opts.get("hybrid_url"):
#         kw["hybrid_url"] = opts["hybrid_url"]
#     if opts.get("hybrid_timeout"):
#         kw["hybrid_timeout"] = str(opts["hybrid_timeout"])
#     return kw


# def build_pages_meta(pdf_path: Path, images_dir: Path, stem: str,
#                      page_sizes: dict, render: bool, render_dpi: int) -> list[dict]:
#     """Per-page dimensions for the UI. If render=True, also rasterize each page to
#     a PNG (for the box-overlay background) and record its pixel size + path."""
#     pages: list[dict] = []
#     if render:
#         try:
#             import pymupdf
#             doc = pymupdf.open(str(pdf_path))
#             subdir = images_dir / f"{stem}_pages"
#             subdir.mkdir(parents=True, exist_ok=True)
#             for i in range(doc.page_count):
#                 pg = doc[i]
#                 pix = pg.get_pixmap(dpi=render_dpi)
#                 rel = f"images/{stem}_pages/page_{i + 1}.png"
#                 pix.save(str(images_dir / f"{stem}_pages/page_{i + 1}.png"))
#                 pages.append({"page": i + 1,
#                               "width_pt": round(pg.rect.width, 2),
#                               "height_pt": round(pg.rect.height, 2),
#                               "width_px": pix.width, "height_px": pix.height,
#                               "dpi": render_dpi, "image": rel})
#             doc.close()
#             return pages
#         except Exception as e:  # noqa: BLE001
#             print(f"    ! page render failed ({e}); emitting sizes only", file=sys.stderr)
#     for pg, (w, h) in sorted(page_sizes.items()):
#         pages.append({"page": pg, "width_pt": w, "height_pt": h})
#     return pages


# def parse_one(pdf_path: Path, output_dir: Path, images_dir: Path, opts: dict) -> Path:
#     raw_dir = output_dir / "_raw"
#     raw_dir.mkdir(parents=True, exist_ok=True)
#     _IMG_FILTER["drop_blank"] = opts.get("drop_blank_images", True)
#     _IMG_FILTER["dropped"] = 0
#     page_sizes = get_page_sizes(pdf_path)   # {page: (w_pt, h_pt)} for boxes/overlay

#     engine = "native"
#     page_info = {"classification": "digital", "n_scanned": 0, "total_pages": None}
#     page_engines: dict[int, str] = {}  # page -> 'tesseract' | 'lighton', OCR'd pages only
#     ocr_mode = opts["ocr"]
#     # Only run hybrid on the FIRST pass if the user explicitly picked a backend.
#     # For --ocr auto we parse natively first, then retry with OCR only if needed.
#     hybrid_explicit = bool(opts.get("_hybrid_explicit"))

#     # ---- Pass 1 --------------------------------------------------------------
#     use_hybrid = bool(opts.get("hybrid_url")) or hybrid_explicit
#     if ocr_mode == "force":
#         if use_hybrid:
#             raw = _run_convert(pdf_path, raw_dir, opts,
#                                _hybrid_kwargs(opts, full_pages=True, hancom_force=True))
#             engine = f"hybrid:{opts.get('hybrid') or 'docling-fast'}:force"
#             page_info = analyze_pages(raw, pdf_path)
#         else:
#             # native parse for structure/page-count, then OCR every SCANNED
#             # page in-process. Digital-text pages are left completely alone --
#             # "force" means "force the OCR engine on pages that need OCR",
#             # not "OCR every page regardless of content". A fully-scanned
#             # document (e.g. one photographed page) has every page in
#             # scanned_pages anyway, so this covers that case too.
#             raw = _run_convert(pdf_path, raw_dir, opts, None)
#             page_info = analyze_pages(raw, pdf_path)
#             targets = page_info["scanned_pages"]
#             print(f"    force OCR: {opts['ocr_engine']} on scanned page(s) {targets} "
#                   f"(lang={opts['ocr_lang']})")
#             page_engines = run_builtin_ocr(pdf_path, targets, raw,
#                                            opts["ocr_engine"], opts["ocr_lang"], opts["ocr_dpi"],
#                                            page_sizes, opts)
#             engine = f"builtin-ocr:{opts['ocr_engine']}:force" if page_engines else "native"
#             # NOTE: intentionally NOT re-running analyze_pages(raw) here --
#             # once OCR splices real text into a scanned page, re-analyzing
#             # would see that text and misclassify the page back to "digital",
#             # which silently disables Enhanced Mode for a page that only
#             # ever had OCR'd (not native) text. The pre-OCR page_info
#             # correctly reflects the page's original nature.
#     else:
#         use_hybrid_now = hybrid_explicit and opts.get("hybrid") not in (None, "off")
#         raw = _run_convert(pdf_path, raw_dir, opts,
#                            _hybrid_kwargs(opts, full_pages=False) if use_hybrid_now else None)
#         if use_hybrid_now:
#             engine = f"hybrid:{opts['hybrid']}"

#         page_info = analyze_pages(raw, pdf_path)
#         cls = page_info["classification"]
#         # ---- Pass 2 (auto-OCR) : only if some page needs it -----------------
#         need_ocr = cls in ("scanned", "mixed")
#         if need_ocr and ocr_mode == "auto" and engine == "native":
#             targets = page_info["scanned_pages"]
#             if use_hybrid:
#                 # --- external Docling/EasyOCR server path -------------------
#                 backend = opts.get("hybrid") or "docling-fast"
#                 full_pages = (cls == "scanned")
#                 scope = "all pages" if full_pages else f"scanned pages {targets}"
#                 print(f"    {cls} PDF ({page_info['n_scanned']}/{page_info['total_pages']} "
#                       f"scanned) -> OCR via {backend} server, {scope}")
#                 try:
#                     raw = _run_convert(pdf_path, raw_dir, opts,
#                                        _hybrid_kwargs(opts, full_pages=full_pages))
#                     engine = f"hybrid:{backend}:{'full' if full_pages else 'triage'}"
#                     page_info = analyze_pages(raw, pdf_path)
#                 except Exception as e:  # noqa: BLE001
#                     print(f"    ! hybrid OCR failed ({e}). Start the server with:\n"
#                           f"        pip install \"opendataloader-pdf[hybrid]\"\n"
#                           f"        opendataloader-pdf-hybrid --port 5002", file=sys.stderr)
#             else:
#                 # --- built-in in-process OCR (no server needed) -------------
#                 print(f"    {cls} PDF ({page_info['n_scanned']}/{page_info['total_pages']} "
#                       f"scanned) -> OCR pages {targets} with {opts['ocr_engine']} "
#                       f"(lang={opts['ocr_lang']})")
#                 page_engines = run_builtin_ocr(pdf_path, targets, raw,
#                                                opts["ocr_engine"], opts["ocr_lang"], opts["ocr_dpi"],
#                                                page_sizes, opts)
#                 if page_engines:
#                     engine = f"builtin-ocr:{opts['ocr_engine']}"
#                     # NOTE: intentionally NOT re-running analyze_pages(raw) here --
#                     # see the matching note in the force branch above.
#         elif need_ocr and ocr_mode == "off":
#             print(f"    ! WARNING: {cls} PDF ({page_info['n_scanned']}/"
#                   f"{page_info['total_pages']} pages scanned). OCR is off, so those pages "
#                   f"have no text. Re-run with --ocr auto to OCR them.", file=sys.stderr)

#     header = parse_doc_header(raw, pdf_path)
#     n_pages = header.get("total_pages") or len(page_sizes) or 1

#     # `_run_convert` writes the native parse before built-in OCR mutates it in
#     # memory. Persist the enriched tree as well: Enhanced Mode can then clone
#     # it and replace one page without losing Tesseract text from other pages.
#     # This deliberately remains the parser cache, not a user-facing result.
#     (raw_dir / f"{pdf_path.stem}.json").write_text(
#         json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
#     )

#     if opts.get("chunk_mode") == "page":
#         # forced page-per-page chunking (e.g. slide decks)
#         chunks = build_fallback_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
#         used_fallback = True
#     else:
#         chunks = build_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
#         used_fallback = False
#         if not chunks:
#             # No sections found at all -> page-based fallback so RAG still works.
#             chunks = build_fallback_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
#             used_fallback = bool(chunks)
#         elif n_pages > 1 and all(c["section_number"] == "0" for c in chunks):
#             # Only a front-matter chunk spanning several pages (e.g. a slide deck
#             # or an untitled multi-page doc) -> chunk per page instead of one blob.
#             chunks = build_fallback_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
#             used_fallback = bool(chunks)

#     # Page metadata for the UI: dimensions (+ rendered page image if requested).
#     pages_meta = build_pages_meta(pdf_path, images_dir, pdf_path.stem,
#                                   page_sizes, opts.get("render_pages", False),
#                                   opts.get("render_dpi", 150))

#     result = {
#         **header,
#         "num_chunks": len(chunks),
#         "parse": {
#             "engine": engine,
#             # Automatic escalation may use LightOn while the requested
#             # document engine remains Tesseract. Page provenance is the truth.
#             "mode": (MODE_ENHANCED if ENGINE_LIGHTON in page_engines.values()
#                      else MODE_NORMAL),
#             "reading_order": opts["reading_order"],
#             "table_method": opts["table_method"],
#             "use_struct_tree": opts["use_struct_tree"],
#             "ocr": ocr_mode,
#             "page_classification": page_info["classification"],
#             "pages_scanned": page_info["n_scanned"],
#             "scanned_page_numbers": page_info.get("scanned_pages", []),
#             "page_engines": {str(k): v for k, v in page_engines.items()},
#             "chunking": "page-fallback" if used_fallback else "sections",
#             "coordinate_origin": "bottom-left",
#         },
#         "pages": pages_meta,
#         "chunks": chunks,
#     }
#     out = output_dir / f"{pdf_path.stem}.chunks.json"
#     out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
#     if _IMG_FILTER["dropped"]:
#         print(f"    dropped {_IMG_FILTER['dropped']} blank/mask image(s)")
#     return out


# def collect_pdfs(inputs):
#     pdfs = []
#     for item in inputs:
#         p = Path(item)
#         if p.is_dir():
#             pdfs.extend(sorted(p.rglob("*.pdf")))
#         elif p.suffix.lower() == ".pdf" and p.exists():
#             pdfs.append(p)
#         else:
#             print(f"  ! skipping (not a PDF / not found): {item}", file=sys.stderr)
#     return pdfs


# def make_opts(**overrides) -> dict:
#     """Build a full opts dict (all keys parse_one expects) with sensible defaults.
#     Lets other modules (ingest.py, the web app) call parse_one without argparse."""
#     opts = {
#         "image_format": "png",
#         "image_output": "external",
#         "drop_blank_images": True,
#         "render_pages": False,
#         "render_dpi": settings.PAGE_PREVIEW_DPI,
#         "chunk_mode": "auto",
#         "reading_order": "xycut",
#         "table_method": "default",
#         "use_struct_tree": False,
#         "include_header_footer": False,
#         "detect_strikethrough": False,
#         "keep_line_breaks": False,
#         "password": None,
#         "pages": None,
#         "content_safety_off": None,
#         "threads": None,
#         "quiet": True,
#         "ocr": "auto",
#         "ocr_engine": "tesseract",
#         "ocr_lang": "eng",
#         "handwriting": "auto",
#         "handwriting_threshold": HANDWRITING_CONF_THRESHOLD,
#         "handwriting_min_words": HANDWRITING_MIN_WORDS,
#         "lighton_device": None,
#         "lighton_max_new_tokens": settings.LIGHTON_MAX_NEW_TOKENS,
#         "ocr_dpi": settings.OCR_RENDER_DPI,
#         "hybrid": None,
#         "hybrid_mode": None,
#         "hybrid_url": None,
#         "hybrid_timeout": None,
#         "hybrid_fallback": False,
#     }
#     opts.update(overrides)
#     opts["_hybrid_explicit"] = opts.get("hybrid") not in (None, "off")
#     if opts.get("hybrid_url") and not opts.get("hybrid"):
#         opts["hybrid"] = "docling-fast"
#     return opts


# def main():
#     ap = argparse.ArgumentParser(
#         description="Parse numbered manuals to section-oriented RAG JSON "
#                     "(scanned/multicolumn/complex/normal PDFs).",
#         formatter_class=argparse.ArgumentDefaultsHelpFormatter,
#     )
#     ap.add_argument("inputs", nargs="+", help="PDF files and/or folders")
#     ap.add_argument("-o", "--output", default="parsed_output")
#     ap.add_argument("--auto", action="store_true",
#                     help="one-flag smart mode: cluster tables + auto-OCR for scans "
#                          "(multi-column is always on via xycut). Combine with --hybrid-url "
#                          "for real OCR of scanned PDFs.")

#     # image extraction
#     ap.add_argument("--image-format", default="png", choices=["png", "jpeg"])
#     ap.add_argument("--image-output", default="external",
#                     choices=["external", "embedded", "off"],
#                     help="external=file refs, embedded=base64, off=no images")
#     ap.add_argument("--keep-blank-images", action="store_true",
#                     help="keep near-blank / solid-colour mask images (dropped by default)")
#     ap.add_argument("--render-pages", action="store_true",
#                     help="rasterize each page to a PNG (for the UI box-overlay background)")
#     ap.add_argument("--render-dpi", type=int, default=150,
#                     help="DPI for --render-pages page images")
#     ap.add_argument("--chunk-mode", default="auto", choices=["auto", "page"],
#                     help="auto=section chunks (fallback to pages); page=one chunk per page")

#     # layout / structure
#     ap.add_argument("--reading-order", default="xycut", choices=["xycut", "off"],
#                     help="xycut handles multi-column layouts")
#     ap.add_argument("--table-method", default="default", choices=["default", "cluster"],
#                     help="cluster = better for borderless/complex tables")
#     ap.add_argument("--use-struct-tree", action="store_true",
#                     help="use tagged-PDF structure tree (best for well-tagged PDFs)")
#     ap.add_argument("--include-header-footer", action="store_true")
#     ap.add_argument("--detect-strikethrough", action="store_true")
#     ap.add_argument("--keep-line-breaks", action="store_true")

#     # access / scope
#     ap.add_argument("--password", default=None, help="password for encrypted PDFs")
#     ap.add_argument("--pages", default=None, help='page range, e.g. "1,3,5-7"')
#     ap.add_argument("--content-safety-off", default=None,
#                     help="disable safety filters: all,hidden-text,off-page,tiny,hidden-ocg")
#     ap.add_argument("--threads", default=None, help="worker threads (>1 experimental)")
#     ap.add_argument("--quiet", action="store_true", help="suppress converter logging")

#     # OCR / hybrid backend (for scanned PDFs)
#     # OCR (built-in, in-process) — no server required
#     ap.add_argument("--ocr", default="auto", choices=["off", "auto", "force"],
#                     help="off=text layer only; auto=native for digital + OCR scanned "
#                          "pages in-process (default); force=OCR every scanned page")
#     ap.add_argument("--ocr-engine", default="tesseract",
#                     choices=["tesseract", "easyocr", "lighton"],
#                     help="built-in OCR engine (tesseract needs the system binary; "
#                          "easyocr is heavier; lighton = LightOnOCR-2-1B, handwriting-capable)")
#     ap.add_argument("--handwriting", default="auto", choices=["off", "auto", "force"],
#                     help="off=Tesseract only; auto=escalate low-confidence pages to "
#                          "LightOnOCR-2-1B (handwriting); force=always LightOnOCR-2-1B")
#     ap.add_argument("--handwriting-threshold", type=float, default=HANDWRITING_CONF_THRESHOLD,
#                     help="in auto mode, escalate a page to LightOnOCR-2-1B when mean Tesseract "
#                          f"confidence <= this (default {HANDWRITING_CONF_THRESHOLD:.0f}; raise to catch more handwriting)")
#     ap.add_argument("--lighton-device", default=None,
#                     help="LightOnOCR-2-1B device, e.g. 'cpu', 'cuda', or 'mps' (default: auto)")
#     ap.add_argument("--lighton-max-tokens", type=int, default=settings.LIGHTON_MAX_NEW_TOKENS,
#                     help="max tokens LightOnOCR-2-1B may generate per page; higher avoids "
#                          "truncation on dense pages but is slower, esp. on CPU (default 2048)")
#     ap.add_argument("--ocr-lang", default="eng",
#                     help="OCR language(s). tesseract: 'eng','deu','eng+deu'. easyocr: 'en','en,hi'")
#     ap.add_argument("--ocr-dpi", type=int, default=settings.OCR_RENDER_DPI,
#                     help="render DPI for scanned pages before OCR")

#     # OCR via external hybrid server (advanced alternative to built-in)
#     ap.add_argument("--hybrid", default=None, choices=["off", "docling-fast", "hancom-ai"],
#                     help="advanced: use an external hybrid OCR server instead of built-in OCR")
#     ap.add_argument("--hybrid-mode", default=None, choices=["auto", "full"])
#     ap.add_argument("--hybrid-url", default=None, help="hybrid server URL, e.g. http://localhost:5002")
#     ap.add_argument("--hybrid-timeout", default=None, help="ms (0 = no timeout)")
#     ap.add_argument("--hybrid-fallback", action="store_true",
#                     help="fall back to Java pipeline if the hybrid backend errors")
#     args = ap.parse_args()

#     # --auto: pick smart defaults unless the user overrode them explicitly.
#     if args.auto:
#         if args.table_method == "default":
#             args.table_method = "cluster"
#         if args.ocr == "off":
#             args.ocr = "auto"

#     output_dir = Path(args.output)
#     images_dir = output_dir / "images"
#     images_dir.mkdir(parents=True, exist_ok=True)

#     opts = {
#         "image_format": args.image_format,
#         "image_output": args.image_output,
#         "drop_blank_images": not args.keep_blank_images,
#         "render_pages": args.render_pages,
#         "render_dpi": args.render_dpi,
#         "chunk_mode": args.chunk_mode,
#         "reading_order": args.reading_order,
#         "table_method": args.table_method,
#         "use_struct_tree": args.use_struct_tree,
#         "include_header_footer": args.include_header_footer,
#         "detect_strikethrough": args.detect_strikethrough,
#         "keep_line_breaks": args.keep_line_breaks,
#         "password": args.password,
#         "pages": args.pages,
#         "content_safety_off": args.content_safety_off,
#         "threads": args.threads,
#         "quiet": args.quiet,
#         "ocr": args.ocr,
#         "ocr_engine": args.ocr_engine,
#         "handwriting": args.handwriting,
#         "handwriting_threshold": args.handwriting_threshold,
#         "lighton_device": args.lighton_device,
#         "lighton_max_new_tokens": args.lighton_max_tokens,
#         "ocr_lang": args.ocr_lang,
#         "ocr_dpi": args.ocr_dpi,
#         "hybrid": args.hybrid,
#         "hybrid_mode": args.hybrid_mode,
#         "hybrid_url": args.hybrid_url,
#         "hybrid_timeout": args.hybrid_timeout,
#         "hybrid_fallback": args.hybrid_fallback,
#     }
#     # Built-in OCR is the default. Only default a hybrid backend when the user
#     # actually points at a server (--hybrid-url) or names a backend (--hybrid).
#     opts["_hybrid_explicit"] = args.hybrid not in (None, "off")
#     if opts["hybrid_url"] and not opts["hybrid"]:
#         opts["hybrid"] = "docling-fast"

#     pdfs = collect_pdfs(args.inputs)
#     if not pdfs:
#         print("No PDFs found.", file=sys.stderr)
#         sys.exit(1)

#     print(f"Found {len(pdfs)} PDF(s). Output -> {output_dir.resolve()}")
#     written = []
#     for pdf in pdfs:
#         print(f"  parsing {pdf} ...")
#         try:
#             out = parse_one(pdf, output_dir, images_dir, opts)
#             written.append(out)
#             print(f"    -> {out.name}")
#         except Exception as e:  # noqa: BLE001
#             print(f"    FAILED: {e}", file=sys.stderr)

#     shutil.rmtree(output_dir / "_raw", ignore_errors=True)
#     print(f"\nDone. {len(written)} file(s) written. Images in {images_dir}/")


# if __name__ == "__main__":
#     main()



















#!/usr/bin/env python3
"""
parse_manual.py
===============
Parse structured technical manuals (SCHEMA-ST4-style, numbered sections) with
OpenDataLoader into a section-oriented JSON built for RAG.

This version is hardened to handle a WIDE range of PDFs:
  * normal, born-digital PDFs (text layer present)
  * multi-column layouts            -> reading_order="xycut" (XY-cut)
  * complex / borderless tables      -> table_method="cluster"
  * tagged / structured PDFs         -> use_struct_tree
  * encrypted PDFs                   -> password
  * SCANNED / image-only PDFs        -> OCR via the hybrid backend
  * unstructured PDFs (no numbered headings) -> page/whole-doc fallback chunking

--------------------------------------------------------------------------------
IMPORTANT about scanned PDFs / OCR
--------------------------------------------------------------------------------
Every page is classified as digital or scanned. Digital pages use the fast native
text-layer path (unchanged logic). Scanned pages are OCR'd IN-PROCESS and the
recovered text is merged into the SAME chunks JSON -- no external server needed.

Built-in OCR engines:
  * tesseract (default) -- needs the system binary:
        Ubuntu/Debian : sudo apt-get install -y tesseract-ocr
        macOS         : brew install tesseract
        (extra languages: apt-get install tesseract-ocr-deu, etc.)
        pip install pymupdf pytesseract pillow
  * easyocr (optional) -- pip only, heavier, downloads models on first run:
        pip install easyocr
  * lighton (optional) -- LightOnOCR-2-1B, handwriting-capable VLM OCR:
        pip install "transformers>=5.0.0" torch pillow

    python parse_manual.py scan.pdf                         # tesseract, lang=eng
    python parse_manual.py scan.pdf --ocr-lang deu          # German
    python parse_manual.py scan.pdf --ocr-engine easyocr --ocr-lang en
    python parse_manual.py scan.pdf --ocr-engine lighton --handwriting force

--ocr auto (default) : native for digital pages, OCR for scanned pages.
--ocr force          : OCR every SCANNED page (native digital-text pages are
                        never touched, even in force mode).
--ocr off            : text layer only (scanned pages come out empty; warns).

Advanced: an external Docling/EasyOCR "hybrid" server can be used instead of the
built-in OCR by passing --hybrid-url http://localhost:5002 (start it with
`pip install "opendataloader-pdf[hybrid]" && opendataloader-pdf-hybrid --port 5002`).

--------------------------------------------------------------------------------
Output per PDF -> <name>.chunks.json  (unchanged schema, plus a `parse` block)
--------------------------------------------------------------------------------
  {
    doc_id, doc_title, product, subject, doc_type, doc_number, language,
    revision_date, source_file, total_pages, num_chunks,
    parse: { engine, mode, reading_order, table_method, ocr, page_classification,
              pages_scanned, scanned_page_numbers, page_engines, ... },
    chunks: [
      { chunk_id, section_number, section_title, text,
        images: [{image_path, caption}],
        metadata: { doc_id, product, system, section_path,
                    page_start, page_end, content_type,
                    token_estimate, chunk_index } }
    ]
  }

Images extracted to <output_dir>/images/ ; paths are relative ("images/..").

Requirements:
    Java 11+ ; pip install -U opendataloader-pdf
    (for OCR) pip install "opendataloader-pdf[hybrid]" and a running hybrid server

Usage:
    python parse_manual.py m1.pdf
    python parse_manual.py *.pdf -o out/
    python parse_manual.py scans/ -o out/ --ocr auto --hybrid-url http://localhost:5002
    python parse_manual.py complex.pdf --table-method cluster --reading-order xycut
    python parse_manual.py tagged.pdf --use-struct-tree
    python parse_manual.py secret.pdf --password hunter2
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import opendataloader_pdf

from config import settings
from constants import ENGINE_LIGHTON, MODE_ENHANCED, MODE_NORMAL

# A heading that starts a section: leading number like "8", "8.3", "2." or "2.1."
# (trailing dot optional -- academic papers number sections "2." / "2.1."), then a title.
SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
# Common UNNUMBERED structural headings (papers/manuals) that should also start a
# chunk, so e.g. the Abstract isn't merged into the title block.
KNOWN_HEADINGS = {
    "abstract", "keywords", "key words", "index terms", "introduction",
    "references", "bibliography", "acknowledgment", "acknowledgments",
    "acknowledgement", "acknowledgements", "appendix", "appendices",
    "conclusion", "conclusions", "summary",
}
# TOC lines carry dot leaders ("1 Introduction ....... 7") -> exclude.
DOTLEADER_RE = re.compile(r"\.{3,}")
SAFETY_WORDS = ("WARNING", "DANGER", "CAUTION", "NOTICE", "ATTENTION")

# Container node types whose kids should be walked in reading order but which
# are not themselves emitted as content. Keeps the walker robust to nesting.
CONTAINER_TYPES = {"document", "section", "group", "column", "div",
                   "article", "text block", "textblock"}

# Heuristic: below this many extracted characters per page, a doc is likely a
# scan (image-only) with no usable text layer.
SCANNED_CHARS_PER_PAGE = 90


# --------------------------------------------------------------------------- #
# Document header parsing (from the SCHEMA-ST4 title string)
# --------------------------------------------------------------------------- #
def parse_doc_header(raw: dict, pdf_path: Path) -> dict:
    title = (raw.get("title") or "").strip()
    # Some PDFs report "(anonymous)" / empty metadata; fall back to first heading.
    if not title or title.lower() in {"(anonymous)", "untitled", "anonymous"}:
        title = _first_heading_text(raw) or ""
    title = title.strip()

    doc_number = None
    language = None
    m = re.search(r"\b([A-Z]{2,}\d{4,}\.\d+)\b", title)
    if m:
        doc_number = m.group(1)
    m = re.search(r"\b([a-z]{2}-[A-Z]{2})\b", title)
    if m:
        language = m.group(1)
    # strip the doc number + language to get the human title
    human = title
    for tok in (doc_number, language):
        if tok:
            human = human.replace(tok, "")
    human = re.sub(r"\s{2,}", " ", human).strip(" -")
    product, subject = human, None
    if " - " in human:
        product, subject = [s.strip() for s in human.split(" - ", 1)]

    # revision date from "D:20260325..." -> 2026-03-25
    rev = None
    cd = raw.get("creation date") or raw.get("modification date") or ""
    m = re.search(r"D:(\d{4})(\d{2})(\d{2})", cd)
    if m:
        rev = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    doc_type = "maintenance_manual" if "mainten" in (subject or human).lower() else "manual"
    doc_id = doc_number or pdf_path.stem
    return {
        "doc_id": doc_id,
        "doc_title": human or pdf_path.stem,
        "product": product or None,
        "subject": subject,
        "doc_type": doc_type,
        "doc_number": doc_number,
        "language": language,
        "revision_date": rev,
        "source_file": pdf_path.name,
        "total_pages": raw.get("number of pages"),
    }


def _first_heading_text(raw: dict) -> str | None:
    for node in iter_nodes(raw):
        if node.get("type") == "heading":
            c = (node.get("content") or "").strip()
            if c and not DOTLEADER_RE.search(c):
                return c
    return None


# --------------------------------------------------------------------------- #
# Reading-order tree walker: flatten nested containers into a linear stream.
# This is what makes multi-column / hybrid / complex output parse correctly:
# headings & paragraphs nested inside container nodes are still surfaced in order.
# --------------------------------------------------------------------------- #
def iter_nodes(root: dict):
    """
    Yield content nodes in reading order, descending into container nodes.

    Emitted (leaf/semantic) nodes: heading, paragraph, caption, list, table,
    image, and any unknown leaf that carries text content.
    Container nodes (document/section/text block/...) are transparent: we recurse
    into their kids instead of emitting them.
    """
    for kid in root.get("kids", []) or []:
        yield from _iter_node(kid)


def _iter_node(node: dict):
    t = (node.get("type") or "").lower()
    if t in CONTAINER_TYPES:
        # transparent container: descend, do not emit the container itself
        for kid in node.get("kids", []) or []:
            yield from _iter_node(kid)
        return
    # semantic/leaf node -> emit it (its own extractor handles inner kids)
    yield node


def _reorder_columns(nodes: list[dict]) -> list[dict]:
    """Re-sort one page's nodes into true column reading order (left column
    top-to-bottom, then right column). Only reorders pages that are *clearly*
    two-column; single-column pages, forms, and scans are left untouched.

    Fixes multi-column PDFs where the parser's reading order interleaves columns
    (e.g. right-column body text emitted before the left-column heading)."""
    bb = [n.get("bounding box") for n in nodes]
    if len(nodes) < 4 or any(b is None or len(b) < 4 for b in bb):
        return nodes
    pmin = min(b[0] for b in bb); pmax = max(b[2] for b in bb)
    mid = (pmin + pmax) / 2.0; span = pmax - pmin
    if span <= 0:
        return nodes

    def cls(b):
        # spans the centre by a clear margin -> full-width (title, wide figure/table)
        if b[0] < mid - 0.05 * span and b[2] > mid + 0.05 * span:
            return "full"
        return "left" if (b[0] + b[2]) / 2 < mid else "right"

    tags = [cls(b) for b in bb]
    if sum(t == "left" for t in tags) < 2 or sum(t == "right" for t in tags) < 2:
        return nodes  # not confidently two-column -> keep original order

    out, L, R = [], [], []

    def flush():
        L.sort(key=lambda n: -n["bounding box"][3])   # bottom-left origin: top = larger y
        R.sort(key=lambda n: -n["bounding box"][3])
        out.extend(L); out.extend(R); L.clear(); R.clear()

    for i in sorted(range(len(nodes)), key=lambda i: -bb[i][3]):
        if tags[i] == "full":
            flush(); out.append(nodes[i])
        elif tags[i] == "left":
            L.append(nodes[i])
        else:
            R.append(nodes[i])
    flush()
    return out


def ordered_nodes(raw: dict) -> list[dict]:
    """Flatten to a reading-order stream, then fix column order per page."""
    flat = list(iter_nodes(raw))
    from collections import OrderedDict
    pages = OrderedDict()
    for n in flat:
        pages.setdefault(n.get("page number"), []).append(n)
    out = []
    for pg in sorted(pages, key=lambda p: (p is None, p)):
        out.extend(_reorder_columns(pages[pg]))
    return out


# --------------------------------------------------------------------------- #
# Text extraction: turn any node (incl. nested text blocks) into readable text
# --------------------------------------------------------------------------- #
def table_to_markdown(node: dict) -> str:
    rows = []
    for row in node.get("rows", []):
        cells = []
        for cell in row.get("cells", []):
            parts = [k.get("content", "") for k in cell.get("kids", []) if k.get("content")]
            # recurse for cells that hold nested blocks (e.g. lists inside a cell)
            if not parts:
                inner = " ".join(node_text(k) for k in cell.get("kids", []) if k)
                if inner.strip():
                    parts = [inner]
            cells.append(" ".join(parts).strip().replace("\n", " "))
        rows.append(cells)
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    head, *body = rows
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join(["---"] * ncols) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def node_text(node: dict) -> str:
    """Readable text for a body node, recursing into text blocks / list items."""
    t = (node.get("type") or "").lower()
    if t in ("paragraph", "caption", "heading", "title", "text", "footnote"):
        return (node.get("content") or "").strip()
    if t == "list":
        items = []
        for it in node.get("list items", []):
            c = (it.get("content") or "").strip()
            # a list item may itself hold sub-nodes
            sub = " ".join(node_text(k) for k in it.get("kids", []) if k)
            items.append((c + (" " + sub if sub.strip() else "")).strip())
        return "\n".join(i for i in items if i)
    if t == "table":
        return table_to_markdown(node)
    if t in ("text block", "textblock", "section", "group", "column", "div"):
        return "\n".join(x for x in (node_text(k) for k in node.get("kids", [])) if x.strip())
    # unknown leaf: fall back to any content string it carries
    return (node.get("content") or "").strip()


def is_section_heading(node: dict):
    """Return (number, title) if this heading opens a section, else None.
    Matches numbered headings ("8.1 Title") and common unnumbered structural
    headings (Abstract, References, ...), so e.g. the Abstract becomes its own
    chunk instead of being merged into the title block."""
    if (node.get("type") or "").lower() != "heading":
        return None
    # OCR'd text (handwriting / scans) is too noisy to trust for section
    # boundaries -- a misread list item like "3. puppy" must NOT start a section
    # and drop everything above it. Let OCR pages fall to page/whole-page chunks.
    if node.get("_ocr"):
        return None
    content = (node.get("content") or "").strip()
    if not content or DOTLEADER_RE.search(content):   # skip TOC lines
        return None
    m = SECTION_RE.match(content)
    if m:
        return m.group(1), m.group(2).strip()
    # unnumbered structural heading? (short line whose text is a known section name)
    key = content.rstrip(":.").strip().lower()
    if key in KNOWN_HEADINGS and len(content) < 40:
        return key.replace(" ", "-"), content.rstrip(":.").strip()
    return None


# --------------------------------------------------------------------------- #
# Image handling
# --------------------------------------------------------------------------- #
# Filter config (set per-run in parse_one). Drops "blank" images -- e.g. the
# solid-black alpha-mask (SMask) layers that some PDFs store alongside real
# images, which otherwise get extracted as junk black/white rectangles.
_IMG_FILTER = {"drop_blank": True, "std_thresh": 4.0,
               "uniform_frac": 0.995, "min_side": 16, "dropped": 0}


def _is_degenerate_image(path: Path) -> bool:
    """True if the image is effectively blank: near-uniform, all-black, all-white,
    or a tiny sliver. Uses PIL only; if PIL is unavailable, never drops."""
    try:
        from PIL import Image, ImageStat
    except Exception:
        return False
    try:
        im = Image.open(path)
        im = im.convert("L")
    except Exception:
        return False
    w, h = im.size
    if w < _IMG_FILTER["min_side"] or h < _IMG_FILTER["min_side"]:
        return True
    if ImageStat.Stat(im).stddev[0] < _IMG_FILTER["std_thresh"]:
        return True  # near-uniform (solid colour / mask layer)
    hist = im.histogram()
    n = sum(hist) or 1
    white = sum(hist[250:]) / n
    black = sum(hist[:6]) / n
    return white > _IMG_FILTER["uniform_frac"] or black > _IMG_FILTER["uniform_frac"]


def copy_image(node, raw_dir: Path, images_dir: Path, stem: str, idx: int):
    src_rel = node.get("source")
    if not src_rel:
        return None
    src = (raw_dir / src_rel).resolve()
    if not src.exists():
        # some builds emit an absolute path or a different relative root
        alt = Path(src_rel)
        if alt.exists():
            src = alt.resolve()
        else:
            return None
    ext = src.suffix.lower() or ".png"
    page = node.get("page number")
    name = f"{stem}_p{page}_{idx}{ext}"
    dst = (images_dir / name).resolve()
    shutil.copy2(src, dst)
    if _IMG_FILTER["drop_blank"] and _is_degenerate_image(dst):
        try:
            dst.unlink()
        except OSError:
            pass
        _IMG_FILTER["dropped"] += 1
        return None
    return f"images/{name}"


def image_caption(node) -> str | None:
    desc = node.get("description")
    if desc:
        return desc
    alt = node.get("alt_source")
    if alt and str(alt).lower() != "missing":
        return alt
    return None


# --------------------------------------------------------------------------- #
# Core: segment the reading-order stream into subsection chunks
# --------------------------------------------------------------------------- #
def build_chunks(raw: dict, header: dict, raw_dir: Path,
                 images_dir: Path, stem: str) -> list[dict]:
    doc_id = header["doc_id"]
    product = header["product"]
    path_stack: dict[int, str] = {}       # depth -> "N Title"
    chunks: list[dict] = []
    # Start with a "front matter" bucket so content before the first numbered
    # heading (title, authors, abstract) is captured rather than dropped. TOC
    # lines (dot leaders) are skipped so they don't pollute it.
    cur = {
        "section_number": "0", "section_title": "Front matter", "system": None,
        "section_path": ["0 Front matter"], "page_start": None, "page_end": None,
        "_body": [], "images": [], "_regions": [], "_front": True,
    }
    img_idx = 0

    def close(c):
        if not c:
            return
        body = "\n".join(x for x in c["_body"] if x.strip()).strip()
        if not body and not c["images"]:
            return  # drop empty container headings (e.g. a chapter with no intro)
        if c.get("_front") and len(body) < 120 and not c["images"]:
            return  # skip trivial front matter (e.g. a bare title page)
        if c.get("_front") or not c["section_number"][:1].isdigit():
            title_line = c["section_title"]
        else:
            title_line = f"{c['section_number']} {c['section_title']}".strip()
        text = (title_line + "\n" + body).strip() if body else title_line
        low = text.lower()
        if re.search(r"(^|\n)\s*\d+\.\s", body):
            ctype = "procedure"
        elif any(w.lower() in low for w in SAFETY_WORDS) and len(body) < 400:
            ctype = "safety"
        else:
            ctype = "section"
        chunks.append({
            "chunk_id": f"{doc_id}#{c['section_number']}",
            "section_number": c["section_number"],
            "section_title": c["section_title"],
            "text": text,
            "images": c["images"],
            "regions": c["_regions"],
            "metadata": {
                "doc_id": doc_id,
                "product": product,
                "system": c["system"],
                "section_path": c["section_path"],
                "page_start": c["page_start"],
                "page_end": c["page_end"],
                "content_type": ctype,
                "token_estimate": max(1, len(text) // 4),
                "chunk_index": len(chunks),
            },
        })

    def find_nested_images(node):
        """Walk a node's subtree (list items' kids, table cells' kids, ...) and
        yield every 'image' leaf found, however deep. node_text() already
        recurses into these same structures for TEXT, but never surfaces
        images -- this fills that gap so a figure nested under a numbered
        step (very common: "step 5, here's its diagram") isn't silently
        dropped just because it isn't a top-level sibling node."""
        if not isinstance(node, dict):
            return
        t = (node.get("type") or "").lower()
        if t == "image":
            yield node
            return
        # list: descend into each list item's kids
        for it in node.get("list items", []) or []:
            for kid in it.get("kids", []) or []:
                yield from find_nested_images(kid)
        # table: descend into each cell's kids
        for row in node.get("rows", []) or []:
            for cell in row.get("cells", []) or []:
                for kid in cell.get("kids", []) or []:
                    yield from find_nested_images(kid)
        # generic containers (text block, etc.)
        for kid in node.get("kids", []) or []:
            yield from find_nested_images(kid)

    def add_region(c, node):
        bb = node.get("bounding box")
        pg = node.get("page number")
        if not c or not bb or pg is None:
            return
        txt = node_text(node)
        c["_regions"].append({
            "page": pg,
            "bbox": [round(float(v), 2) for v in bb[:4]],
            "type": (node.get("type") or "").lower(),
            "text": txt[:400],
        })

    for node in ordered_nodes(raw):       # reading-order walk + column re-sort
        sec = is_section_heading(node)
        page = node.get("page number")
        if sec:
            number, title = sec
            depth = number.count(".") + 1
            # update the hierarchy stack
            path_stack[depth] = f"{number} {title}"
            for d in list(path_stack):
                if d > depth:
                    del path_stack[d]
            close(cur)
            system = re.sub(r"^\d+(?:\.\d+)*\s+", "", path_stack.get(1, title))
            cur = {
                "section_number": number,
                "section_title": title,
                "system": system,
                "section_path": [path_stack[d] for d in sorted(path_stack)],
                "page_start": page,
                "page_end": page,
                "_body": [],
                "images": [],
                "_regions": [],
            }
            add_region(cur, node)          # the heading itself is a region
            continue

        if cur.get("_front"):
            # accumulate front matter, but skip table-of-contents dot-leader lines
            txt = node_text(node)
            if txt.strip() and not DOTLEADER_RE.search(txt) and \
               (node.get("type") or "").lower() != "image":
                if cur["page_start"] is None:
                    cur["page_start"] = page
                cur["_body"].append(txt)
                add_region(cur, node)
                if page is not None:
                    cur["page_end"] = page
            continue

        if page is not None:
            cur["page_end"] = page
        node_type = (node.get("type") or "").lower()
        if node_type == "image":
            img_idx += 1
            p = copy_image(node, raw_dir, images_dir, stem, img_idx)
            if p:
                cur["images"].append({"image_path": p, "caption": image_caption(node)})
        else:
            cur["_body"].append(node_text(node))
            add_region(cur, node)
            if node_type in ("list", "table"):
                # node_text() already pulled the TEXT out of nested list
                # items / table cells; separately pull out any images that
                # were hiding in those same nested kids (e.g. the diagram
                # for step 5 of a numbered procedure).
                for img_node in find_nested_images(node):
                    img_idx += 1
                    p = copy_image(img_node, raw_dir, images_dir, stem, img_idx)
                    if p:
                        cur["images"].append({"image_path": p, "caption": image_caption(img_node)})

    close(cur)
    return chunks


# --------------------------------------------------------------------------- #
# Fallback chunking for PDFs with NO numbered headings (scans, brochures, etc.)
# Groups the reading-order stream by page so RAG still gets usable units.
# --------------------------------------------------------------------------- #
def build_fallback_chunks(raw: dict, header: dict, raw_dir: Path,
                          images_dir: Path, stem: str) -> list[dict]:
    doc_id = header["doc_id"]
    product = header["product"]
    pages: dict[int, dict] = {}
    img_idx = 0

    for node in ordered_nodes(raw):
        page = node.get("page number") or 0
        bucket = pages.setdefault(page, {"_body": [], "images": [], "regions": []})
        if (node.get("type") or "").lower() == "image":
            img_idx += 1
            p = copy_image(node, raw_dir, images_dir, stem, img_idx)
            if p:
                bucket["images"].append({"image_path": p, "caption": image_caption(node)})
        else:
            txt = node_text(node)
            if txt.strip():
                bucket["_body"].append(txt)
                bb = node.get("bounding box")
                if bb:
                    bucket["regions"].append({
                        "page": page,
                        "bbox": [round(float(v), 2) for v in bb[:4]],
                        "type": (node.get("type") or "").lower(),
                        "text": txt[:400],
                    })

    chunks: list[dict] = []
    for page in sorted(pages):
        b = pages[page]
        body = "\n".join(x for x in b["_body"] if x.strip()).strip()
        if not body and not b["images"]:
            continue
        sec_num = f"p{page}"
        text = (f"Page {page}\n" + body).strip() if body else f"Page {page}"
        chunks.append({
            "chunk_id": f"{doc_id}#{sec_num}",
            "section_number": sec_num,
            "section_title": f"Page {page}",
            "text": text,
            "images": b["images"],
            "regions": b["regions"],
            "metadata": {
                "doc_id": doc_id,
                "product": product,
                "system": None,
                "section_path": [f"Page {page}"],
                "page_start": page,
                "page_end": page,
                "content_type": "page",
                "token_estimate": max(1, len(text) // 4),
                "chunk_index": len(chunks),
            },
        })
    return chunks


# --------------------------------------------------------------------------- #
# Built-in (in-process) OCR: render scanned pages and OCR them locally, then
# splice the recovered text back into the parse so it lands in the same JSON.
# No external server required. Engines: tesseract (default), easyocr, or lighton.
# --------------------------------------------------------------------------- #
_EASYOCR_READER = None
_LIGHTON_MODEL = None
_LIGHTON_PROCESSOR = None
_LIGHTON_DEVICE = None
_LIGHTON_DTYPE = None
# At or below this mean Tesseract word confidence, a page is treated as
# handwritten / hard and (in auto mode) the WHOLE page is re-OCR'd with
# LightOnOCR-2-1B. Tunable via --handwriting-threshold.
HANDWRITING_CONF_THRESHOLD = settings.HANDWRITING_CONF_THRESHOLD
# Minimum confidently-recognized words before a *low-confidence* escalation is
# considered (separate from the *zero-words* escalation, which always fires).
HANDWRITING_MIN_WORDS = settings.HANDWRITING_MIN_WORDS


def _tesseract_confidence(png: bytes, lang: str) -> tuple[float, int]:
    """Return (mean word confidence 0-100, number of confident words) for a page.
    Used to decide whether Tesseract struggled (handwriting / poor scan)."""
    import io
    import pytesseract
    from PIL import Image
    data = pytesseract.image_to_data(Image.open(io.BytesIO(png)), lang=lang,
                                     output_type=pytesseract.Output.DICT)
    confs = []
    for txt, c in zip(data.get("text", []), data.get("conf", [])):
        if not (txt or "").strip():
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c >= 0:
            confs.append(c)
    if not confs:
        return 0.0, 0
    return sum(confs) / len(confs), len(confs)


def _lighton_pick_device() -> str:
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _lighton_cpu_dtype(torch):
    """Choose BF16 only when the host advertises native CPU instructions.

    Emulated BF16 is slower than FP32, which is why this is feature-gated
    rather than selected merely because PyTorch exposes ``torch.bfloat16``.
    """
    configured = settings.LIGHTON_CPU_DTYPE
    if configured in {"float32", "fp32"}:
        return torch.float32
    if configured in {"bfloat16", "bf16"}:
        return torch.bfloat16
    try:
        flags = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        flags = ""
    native_bf16 = "avx512_bf16" in flags or "sve_bf16" in flags
    return torch.bfloat16 if native_bf16 else torch.float32


def _load_lighton_model(device: str | None = None):
    """Lazily load LightOnOCR-2-1B + its processor once per process."""
    global _LIGHTON_MODEL, _LIGHTON_PROCESSOR, _LIGHTON_DEVICE, _LIGHTON_DTYPE
    try:
        import torch
        from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "LightOnOCR-2-1B is not installed. Install it with:\n"
            "  pip install \"transformers>=5.0.0\" torch pillow\n"
            f"or use --ocr-engine tesseract. [{e}]")

    resolved_device = device or _lighton_pick_device()
    if _LIGHTON_MODEL is None or _LIGHTON_DEVICE != resolved_device:
        if resolved_device == "cpu":
            n_threads = settings.LIGHTON_CPU_THREADS or torch.get_num_threads()
            torch.set_num_threads(n_threads)
            print(f"    ! LightOnOCR-2-1B is running on CPU ({n_threads} thread(s)) -- "
                  "token-by-token generation at this size is slow (roughly minutes per "
                  "dense page). If a GPU is available, install a CUDA-enabled torch "
                  "build to speed this up.", file=sys.stderr)
        dtype = _lighton_cpu_dtype(torch) if resolved_device == "cpu" else torch.bfloat16
        # PyTorch dynamic quantization consumes FP32 weights. INT8 is an
        # explicit alternative to BF16, not an additional conversion layer.
        if resolved_device == "cpu" and settings.LIGHTON_QUANTIZE_CPU:
            dtype = torch.float32
        model = LightOnOcrForConditionalGeneration.from_pretrained(
            "lightonai/LightOnOCR-2-1B", torch_dtype=dtype
        ).to(resolved_device)
        if resolved_device == "cpu" and settings.LIGHTON_QUANTIZE_CPU:
            print("    lighton: applying dynamic int8 quantization (LIGHTON_QUANTIZE_CPU=1)",
                  file=sys.stderr)
            model = torch.quantization.quantize_dynamic(
                model, {torch.nn.Linear}, dtype=torch.qint8
            )
        model.eval()
        _LIGHTON_MODEL = model
        _LIGHTON_PROCESSOR = LightOnOcrProcessor.from_pretrained("lightonai/LightOnOCR-2-1B")
        _LIGHTON_DEVICE = resolved_device
        _LIGHTON_DTYPE = dtype
    return _LIGHTON_MODEL, _LIGHTON_PROCESSOR, _LIGHTON_DEVICE, _LIGHTON_DTYPE


def _clean_lighton_markdown(text: str) -> str:
    """LightOnOCR-2-1B transcribes pages as rich Markdown/HTML (tables, div
    layout wrappers, image placeholders for logos/figures), but this pipeline
    stores plain OCR text nodes -- it never actually extracts and saves those
    referenced images. Left as-is, that markup leaks into chunk text as
    literal '<div ...>', '<table>', '##', '**bold**', and broken
    '![image](image_1.png)' links pointing at files that don't exist. Turn it
    into clean plain text instead."""
    # Dangling image references -- no image is ever actually saved for these,
    # so the link is always broken. Drop them entirely.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)

    # Simple HTML tables -> one readable "cell | cell | cell" line per row.
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<t[dh][^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<tr[^>]*>|</?table[^>]*>|</?thead[^>]*>|</?tbody[^>]*>",
                  "", text, flags=re.IGNORECASE)

    # Any other stray HTML (div layout wrappers, <br/>, <sup>, etc.) -> drop
    # the tags, keep the text between them.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)

    # Markdown syntax that only makes sense when rendered, not as plain text.
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)   # ## Heading
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                 # **bold**
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)  # *italic*
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)   # --- rules

    # Trailing " | " left over from a row's last cell, and blank-cell noise.
    text = re.sub(r"\s*\|\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|\s*", "", text, flags=re.MULTILINE)
    return text


def _ocr_lighton(png: bytes, dpi: int, page_h_pt: float | None,
                 device: str | None = None, max_new_tokens: int = 2048) -> list[dict]:
    """Run LightOnOCR-2-1B (VLM OCR, good on handwriting) on a page image.
    Returns line records [{text}] -- LightOnOCR returns full-page transcribed
    text rather than per-block boxes, so lines carry no bbox (they render as
    whole-page text instead of individually boxed regions). Raises
    RuntimeError with a clear message if LightOnOCR-2-1B isn't installed.

    max_new_tokens trades off completeness vs. speed: generation is
    token-by-token, so on CPU a higher cap can turn into several minutes for
    a single dense page (references lists, big tables). 2048 is a middle
    ground -- raise it (e.g. --lighton-max-tokens 4096) if pages still get
    cut off, lower it if you'd rather risk truncation than wait."""
    import io
    import time

    from PIL import Image

    model, processor, resolved_device, dtype = _load_lighton_model(device)

    image = Image.open(io.BytesIO(png)).convert("RGB")
    conversation = [{"role": "user", "content": [{"type": "image", "image": image}]}]
    inputs = processor.apply_chat_template(
        conversation, add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt",
    )
    inputs = {
        k: v.to(device=resolved_device, dtype=dtype) if v.is_floating_point() else v.to(resolved_device)
        for k, v in inputs.items()
    }

    t0 = time.perf_counter()
    # Disable autograd metadata and retain the decoder KV cache. Both OCR
    # entry points arrive here, so this speeds automatic escalation and the
    # user-triggered Enhance pass equally. `inference_mode` is stronger than
    # `no_grad` and is safe because this process never trains the model.
    import torch
    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            use_cache=True,
        )
    elapsed = time.perf_counter() - t0
    generated_ids = output_ids[0, inputs["input_ids"].shape[1]:]
    n_tokens = generated_ids.shape[0]
    hit_cap = n_tokens >= max_new_tokens
    print(f"    lighton: {n_tokens} tokens in {elapsed:.1f}s on {resolved_device}"
          + (" (hit the cap -- page may be truncated, consider raising max_new_tokens)"
             if hit_cap else ""), file=sys.stderr)
    text = processor.decode(generated_ids, skip_special_tokens=True)
    text = _clean_lighton_markdown(text)

    lines: list[dict] = []
    for raw_line in text.splitlines():
        s = raw_line.strip()
        if s:
            lines.append({"text": s})
    return lines


def _lines_to_nodes(lines: list[dict], page: int, tag: str) -> list[dict]:
    nodes = []
    for ln in lines:
        s = ln["text"]
        is_head = bool(SECTION_RE.match(s)) and not DOTLEADER_RE.search(s)
        nd = {"type": "heading" if is_head else "paragraph", "page number": page,
              "content": s, "_ocr": True, "_engine": tag}
        if ln.get("bbox"):
            nd["bounding box"] = ln["bbox"]
        nodes.append(nd)
    return nodes


def _render_page_png(pdf_path: Path, page_number: int, dpi: int) -> bytes:
    import pymupdf  # lazy: only needed when OCR runs
    doc = pymupdf.open(str(pdf_path))
    try:
        pix = doc[page_number - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()


def get_page_sizes(pdf_path: Path) -> dict:
    """Return {page_number: (width_pt, height_pt)} using PyMuPDF, or {} if
    unavailable. Needed to convert OCR pixel boxes to PDF points and to give the
    UI the page dimensions for overlay scaling."""
    try:
        import pymupdf
    except Exception:
        return {}
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return {}
    try:
        return {i + 1: (round(doc[i].rect.width, 2), round(doc[i].rect.height, 2))
                for i in range(doc.page_count)}
    finally:
        doc.close()


def _ocr_tesseract(png: bytes, lang: str) -> str:
    import io
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(io.BytesIO(png)), lang=lang)


def _ocr_tesseract_lines(png: bytes, lang: str, dpi: int, page_h_pt: float) -> list[dict]:
    """OCR into line records with bounding boxes in PDF points (bottom-left origin),
    so scanned pages get the same coordinate space as native pages."""
    import io
    import pytesseract
    from PIL import Image
    data = pytesseract.image_to_data(Image.open(io.BytesIO(png)), lang=lang,
                                     output_type=pytesseract.Output.DICT)
    scale = 72.0 / dpi
    lines: dict[tuple, dict] = {}
    n = len(data["text"])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        L, T, W, H = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        rec = lines.setdefault(key, {"words": [], "L": L, "T": T, "R": L + W, "B": T + H})
        rec["words"].append(txt)
        rec["L"] = min(rec["L"], L); rec["T"] = min(rec["T"], T)
        rec["R"] = max(rec["R"], L + W); rec["B"] = max(rec["B"], T + H)
    out = []
    for key in sorted(lines):
        r = lines[key]
        text = " ".join(r["words"]).strip()
        if not text:
            continue
        x0 = r["L"] * scale
        x1 = r["R"] * scale
        y1 = page_h_pt - r["T"] * scale   # top edge -> larger y (bottom-left origin)
        y0 = page_h_pt - r["B"] * scale
        out.append({"text": text, "bbox": [round(x0, 2), round(y0, 2),
                                           round(x1, 2), round(y1, 2)]})
    return out


def _ocr_easyocr(png: bytes, lang: str) -> str:
    import io
    import numpy as np
    import easyocr
    from PIL import Image
    global _EASYOCR_READER
    langs = [l.strip() for l in lang.replace("+", ",").split(",") if l.strip()] or ["en"]
    if _EASYOCR_READER is None:
        _EASYOCR_READER = easyocr.Reader(langs, gpu=False)
    arr = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    lines = _EASYOCR_READER.readtext(arr, detail=0, paragraph=True)
    return "\n".join(lines)


def ocr_page_nodes(pdf_path: Path, page: int, engine: str, lang: str, dpi: int,
                   page_h_pt: float | None, opts: dict | None = None) -> list[dict]:
    """OCR one page into parse nodes.
    - engine 'lighton'           -> always LightOnOCR-2-1B (handwriting-capable)
    - engine 'easyocr'           -> EasyOCR (text only)
    - engine 'tesseract' (auto)  -> Tesseract; if handwriting mode is 'auto' and
      Tesseract confidence is low, escalate that page to LightOnOCR-2-1B; if
      'force', always use LightOnOCR-2-1B. Falls back to Tesseract if
      LightOnOCR is unavailable.

    `dpi` is used for Tesseract's render (and the confidence check that
    decides whether to escalate) -- higher is worth it there since it
    directly improves Tesseract's line/word segmentation. Any call into
    LightOnOCR-2-1B (explicit engine='lighton', handwriting='force', or an
    auto-mode escalation) renders a SEPARATE, independently-configurable
    image at `lighton_dpi` (opts, default settings.LIGHTON_RENDER_DPI)
    instead of reusing Tesseract's render -- LightOnOCR's generation cost
    scales with image size, so there's no reason to pay for Tesseract's
    higher resolution on a page that's actually going to LightOnOCR."""
    opts = opts or {}
    hw = opts.get("handwriting", "auto")
    device = opts.get("lighton_device")
    max_new_tokens = int(opts.get("lighton_max_new_tokens") or settings.LIGHTON_MAX_NEW_TOKENS)
    lighton_dpi = int(opts.get("lighton_dpi") or settings.LIGHTON_RENDER_DPI)

    def _render_for_lighton() -> bytes:
        return _render_page_png(pdf_path, page, lighton_dpi)

    if engine == "lighton":
        # Explicit request for LightOnOCR -- no Tesseract pass needed at all,
        # so render straight at the cheaper lighton_dpi.
        return _lines_to_nodes(
            _ocr_lighton(_render_for_lighton(), lighton_dpi, page_h_pt, device, max_new_tokens),
            page, "lighton")

    if engine == "easyocr":
        png = _render_page_png(pdf_path, page, dpi)
        return ocr_text_to_nodes(_ocr_easyocr(png, lang), page, engine_tag="easyocr")

    # --- Tesseract path: render once at the higher-accuracy `dpi` -----------
    png = _render_page_png(pdf_path, page, dpi)

    # --- Tesseract path, with optional handwriting escalation to LightOnOCR-2-1B ---
    if hw == "force":
        try:
            return _lines_to_nodes(
                _ocr_lighton(_render_for_lighton(), lighton_dpi, page_h_pt, device, max_new_tokens),
                page, "lighton")
        except Exception as e:  # noqa: BLE001
            print(f"    ! p{page}: LightOnOCR-2-1B failed ({e}); using Tesseract", file=sys.stderr)
    elif hw == "auto":
        try:
            conf, nwords = _tesseract_confidence(png, lang)
        except Exception:  # noqa: BLE001
            conf, nwords = 100.0, 0
        threshold = float(opts.get("handwriting_threshold", HANDWRITING_CONF_THRESHOLD))
        min_words = int(opts.get("handwriting_min_words", HANDWRITING_MIN_WORDS))
        # Escalate to LightOnOCR-2-1B when EITHER:
        #   (a) Tesseract found enough words to have an opinion, but that opinion
        #       is low-confidence (nwords >= min_words and conf <= threshold), OR
        #   (b) Tesseract found essentially NOTHING (nwords == 0). This used to be
        #       excluded by the old "nwords >= 3" guard, which meant total OCR
        #       failure (the worst case -- e.g. cursive handwriting Tesseract can't
        #       even segment into words) never triggered escalation. Zero words is
        #       not "nothing to OCR here", it's the strongest possible signal that
        #       Tesseract is the wrong tool for this page.
        low_confidence = nwords >= min_words and conf <= threshold
        total_failure = nwords == 0
        if low_confidence or total_failure:
            reason = (f"confidence {conf:.0f} <= {threshold:.0f}" if low_confidence
                      else "Tesseract found 0 usable words")
            print(f"    p{page}: {reason} -> whole page to LightOnOCR-2-1B (handwriting)",
                  file=sys.stderr)
            try:
                return _lines_to_nodes(
                    _ocr_lighton(_render_for_lighton(), lighton_dpi, page_h_pt, device, max_new_tokens),
                    page, "lighton")
            except Exception as e:  # noqa: BLE001
                print(f"    ! p{page}: LightOnOCR-2-1B unavailable ({e}); using Tesseract",
                      file=sys.stderr)

    # default Tesseract output (boxed if page size known)
    if not page_h_pt:
        return ocr_text_to_nodes(_ocr_tesseract(png, lang), page, engine_tag="tesseract")
    nodes = []
    for ln in _ocr_tesseract_lines(png, lang, dpi, page_h_pt):
        s = ln["text"]
        is_head = bool(SECTION_RE.match(s)) and not DOTLEADER_RE.search(s)
        nodes.append({"type": "heading" if is_head else "paragraph",
                      "page number": page, "content": s,
                      "bounding box": ln["bbox"], "_ocr": True, "_engine": "tesseract"})
    return nodes


def ocr_text_to_nodes(text: str, page: int, engine_tag: str = "tesseract") -> list[dict]:
    """
    Turn a page's OCR text into parse nodes. Lines that look like numbered
    section headings ("9 Cooling System") become heading nodes so they slot
    into the section hierarchy; everything else becomes paragraph text.
    """
    nodes: list[dict] = []
    buf: list[str] = []

    def flush():
        if buf:
            nodes.append({"type": "paragraph", "page number": page,
                          "content": " ".join(buf).strip(), "_ocr": True,
                          "_engine": engine_tag})
            buf.clear()

    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s:
            flush()
            continue
        if SECTION_RE.match(s) and not DOTLEADER_RE.search(s):
            flush()
            nodes.append({"type": "heading", "page number": page,
                          "content": s, "_ocr": True, "_engine": engine_tag})
        else:
            buf.append(s)
    flush()
    return nodes


def splice_ocr_nodes(raw: dict, page_nodes: dict[int, list[dict]]) -> None:
    """Insert OCR nodes into raw['kids'] in document (page) order, in place."""
    kids = raw.get("kids", []) or []
    out: list[dict] = []
    done: set[int] = set()

    def flush_upto(pg):
        for spg in sorted(page_nodes):
            if spg in done:
                continue
            if pg is None or spg <= pg:
                out.extend(page_nodes[spg])
                done.add(spg)

    for node in kids:
        pg = node.get("page number")
        if pg is not None:
            flush_upto(pg)
        out.append(node)
    flush_upto(None)
    raw["kids"] = out


def run_builtin_ocr(pdf_path: Path, pages: list[int], raw: dict,
                    engine: str, lang: str, dpi: int,
                    page_sizes: dict | None = None, opts: dict | None = None) -> dict[int, str]:
    """OCR the given page numbers in-process and splice results into raw.
    Returns {page_number: engine_tag} for pages that yielded text -- tag is
    whichever engine actually produced that page's text ('tesseract' or
    'lighton'), which may differ from the requested `engine` when handwriting
    auto/force escalation kicked in."""
    page_sizes = page_sizes or {}
    page_nodes: dict[int, list[dict]] = {}
    page_engine: dict[int, str] = {}
    for pg in pages:
        page_h = page_sizes.get(pg, (None, None))[1]
        try:
            nodes = ocr_page_nodes(pdf_path, pg, engine, lang, dpi, page_h, opts)
        except Exception as e:  # noqa: BLE001
            print(f"    ! OCR failed on page {pg}: {e}", file=sys.stderr)
            continue
        if nodes:
            page_nodes[pg] = nodes
            page_engine[pg] = nodes[0].get("_engine", engine)
    if page_nodes:
        splice_ocr_nodes(raw, page_nodes)
    return page_engine


# --------------------------------------------------------------------------- #
# Page-level scan analysis: classify each page as digital or scanned so that
# MIXED PDFs (some real-text pages + some scanned pages) are handled correctly.
# --------------------------------------------------------------------------- #
# Below this fraction of a page's area covered by raster images, a page is NOT
# considered a scan even if its extractable text is sparse -- a decorative
# logo or a small inline photo shouldn't be enough to trigger a scanned-page
# classification (and the OCR pass that comes with it) on an otherwise blank
# digital title/section-divider page.
SCANNED_IMAGE_COVERAGE = 0.6


def _pymupdf_page_signals(pdf_path: Path) -> dict[int, dict]:
    """Independent per-page cross-check via PyMuPDF: real extractable text
    length and raster-image area coverage (0..1 fraction of the page).
    Returns {} if PyMuPDF isn't available or the file can't be opened --
    callers must fall back to the opendataloader-only heuristic in that case.
    """
    try:
        import pymupdf
    except Exception:
        return {}
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return {}
    out: dict[int, dict] = {}
    try:
        for i in range(doc.page_count):
            page = doc[i]
            text_len = len((page.get_text("text") or "").strip())
            page_area = page.rect.width * page.rect.height
            img_area = 0.0
            if page_area > 0:
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        rects = page.get_image_rects(xref)
                    except Exception:  # noqa: BLE001
                        rects = []
                    for r in rects:
                        img_area += abs(r.width * r.height)
            coverage = min(1.0, img_area / page_area) if page_area else 0.0
            out[i + 1] = {"text_len": text_len, "img_coverage": coverage}
    finally:
        doc.close()
    return out


def analyze_pages(raw: dict, pdf_path: Path | None = None) -> dict:
    per_page: dict[int, dict] = {}
    for node in iter_nodes(raw):
        pg = node.get("page number")
        if pg is None:
            continue
        b = per_page.setdefault(pg, {"chars": 0, "images": 0})
        if (node.get("type") or "").lower() == "image":
            b["images"] += 1
        else:
            b["chars"] += len(node_text(node))

    total = raw.get("number of pages") or (max(per_page) if per_page else 1)

    # Cross-check with PyMuPDF when a pdf_path is supplied: this replaces the
    # crude "does this page have >= 1 embedded image at all" signal with
    # "does a raster image cover a SUBSTANTIAL fraction of the page" -- a
    # small logo/icon on an otherwise sparse title page no longer trips a
    # scanned-page classification, since it never approaches
    # SCANNED_IMAGE_COVERAGE. It also cross-checks the char count against
    # PyMuPDF's own independent text extraction rather than trusting
    # opendataloader's node text alone.
    mupdf_info = _pymupdf_page_signals(pdf_path) if pdf_path is not None else {}

    scanned_pages = []
    likely_prescanned_with_text = []  # diagnostic only, see note below
    for pg in range(1, int(total) + 1):
        b = per_page.get(pg, {"chars": 0, "images": 0})
        chars = b["chars"]
        m = mupdf_info.get(pg)
        if m is not None:
            is_scanned = (chars < SCANNED_CHARS_PER_PAGE
                          and m["text_len"] < SCANNED_CHARS_PER_PAGE
                          and m["img_coverage"] >= SCANNED_IMAGE_COVERAGE)
            # A page that's mostly covered by one big raster image but STILL
            # has real extractable text is very likely a scan that already
            # carries a baked-in (possibly low-quality) OCR text layer from
            # whatever tool produced the PDF. It's correctly left off
            # scanned_pages (it has real text, no need to re-OCR by default),
            # but flagging it separately means a caller could offer "this
            # looks like a pre-OCR'd scan -- re-run with Enhanced Mode
            # anyway?" instead of silently trusting text of unknown quality.
            if (not is_scanned and m["img_coverage"] >= SCANNED_IMAGE_COVERAGE
                    and chars >= SCANNED_CHARS_PER_PAGE):
                likely_prescanned_with_text.append(pg)
        else:
            # PyMuPDF unavailable/failed for this file -- fall back to the
            # original heuristic rather than silently reclassifying everything.
            is_scanned = chars < SCANNED_CHARS_PER_PAGE and b["images"] >= 1
        if is_scanned:
            scanned_pages.append(pg)

    n_scanned = len(scanned_pages)
    if n_scanned == 0:
        classification = "digital"
    elif n_scanned >= int(total):
        classification = "scanned"
    else:
        classification = "mixed"
    total_chars = sum(b["chars"] for b in per_page.values())
    return {
        "total_pages": int(total),
        "n_scanned": n_scanned,
        "scanned_pages": scanned_pages,
        "classification": classification,
        "total_chars": total_chars,
        # diagnostic only, not used to gate OCR: pages that look like a scan
        # by image coverage but already carry real text of unknown quality.
        "likely_prescanned_with_text": likely_prescanned_with_text,
    }


# --------------------------------------------------------------------------- #
def _run_convert(pdf_path: Path, raw_dir: Path, opts: dict, hybrid_kwargs: dict | None):
    """Call opendataloader_pdf.convert with the given options; return parsed JSON."""
    kw = dict(
        input_path=[str(pdf_path)],
        output_dir=str(raw_dir),
        format="json",
        image_output=opts["image_output"],
        image_format=opts["image_format"],
        reading_order=opts["reading_order"],
        table_method=opts["table_method"],
        use_struct_tree=opts["use_struct_tree"],
        include_header_footer=opts["include_header_footer"],
        detect_strikethrough=opts["detect_strikethrough"],
        keep_line_breaks=opts["keep_line_breaks"],
        quiet=opts["quiet"],
    )
    if opts.get("password"):
        kw["password"] = opts["password"]
    if opts.get("pages"):
        kw["pages"] = opts["pages"]
    if opts.get("content_safety_off"):
        kw["content_safety_off"] = opts["content_safety_off"]
    if opts.get("threads"):
        kw["threads"] = str(opts["threads"])
    if hybrid_kwargs:
        kw.update(hybrid_kwargs)
    opendataloader_pdf.convert(**kw)

    raw_json = raw_dir / f"{pdf_path.stem}.json"
    if not raw_json.exists():
        raise FileNotFoundError(f"Expected raw output missing: {raw_json}")
    return json.loads(raw_json.read_text(encoding="utf-8"))


def _hybrid_kwargs(opts: dict, *, full_pages: bool, hancom_force: bool = False) -> dict:
    """
    Build hybrid kwargs for an OCR run.

    Backend default is 'docling-fast' -- that is the server you get from
    `pip install "opendataloader-pdf[hybrid]"` + `opendataloader-pdf-hybrid`,
    which runs IBM Docling with EasyOCR by default. The hancom-ai backend is a
    separate service and takes the hancom-only OCR-strategy flag.
    """
    backend = opts.get("hybrid") or "docling-fast"
    kw = {
        "hybrid": backend,
        # 'full' sends every page to the backend (right for a scanned doc);
        # 'auto' lets the Java pipeline triage which pages need it.
        "hybrid_mode": opts.get("hybrid_mode") or ("full" if full_pages else "auto"),
        "hybrid_fallback": opts.get("hybrid_fallback", True),
    }
    if backend == "hancom-ai":
        kw["hybrid_hancom_ai_ocr_strategy"] = "force" if hancom_force else "auto"
    if opts.get("hybrid_url"):
        kw["hybrid_url"] = opts["hybrid_url"]
    if opts.get("hybrid_timeout"):
        kw["hybrid_timeout"] = str(opts["hybrid_timeout"])
    return kw


def build_pages_meta(pdf_path: Path, images_dir: Path, stem: str,
                     page_sizes: dict, render: bool, render_dpi: int) -> list[dict]:
    """Per-page dimensions for the UI. If render=True, also rasterize each page to
    a PNG (for the box-overlay background) and record its pixel size + path."""
    pages: list[dict] = []
    if render:
        try:
            import pymupdf
            doc = pymupdf.open(str(pdf_path))
            subdir = images_dir / f"{stem}_pages"
            subdir.mkdir(parents=True, exist_ok=True)
            for i in range(doc.page_count):
                pg = doc[i]
                pix = pg.get_pixmap(dpi=render_dpi)
                rel = f"images/{stem}_pages/page_{i + 1}.png"
                pix.save(str(images_dir / f"{stem}_pages/page_{i + 1}.png"))
                pages.append({"page": i + 1,
                              "width_pt": round(pg.rect.width, 2),
                              "height_pt": round(pg.rect.height, 2),
                              "width_px": pix.width, "height_px": pix.height,
                              "dpi": render_dpi, "image": rel})
            doc.close()
            return pages
        except Exception as e:  # noqa: BLE001
            print(f"    ! page render failed ({e}); emitting sizes only", file=sys.stderr)
    for pg, (w, h) in sorted(page_sizes.items()):
        pages.append({"page": pg, "width_pt": w, "height_pt": h})
    return pages


def parse_one(pdf_path: Path, output_dir: Path, images_dir: Path, opts: dict) -> Path:
    raw_dir = output_dir / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _IMG_FILTER["drop_blank"] = opts.get("drop_blank_images", True)
    _IMG_FILTER["dropped"] = 0
    page_sizes = get_page_sizes(pdf_path)   # {page: (w_pt, h_pt)} for boxes/overlay

    engine = "native"
    page_info = {"classification": "digital", "n_scanned": 0, "total_pages": None}
    page_engines: dict[int, str] = {}  # page -> 'tesseract' | 'lighton', OCR'd pages only
    ocr_mode = opts["ocr"]
    # Only run hybrid on the FIRST pass if the user explicitly picked a backend.
    # For --ocr auto we parse natively first, then retry with OCR only if needed.
    hybrid_explicit = bool(opts.get("_hybrid_explicit"))

    # ---- Pass 1 --------------------------------------------------------------
    use_hybrid = bool(opts.get("hybrid_url")) or hybrid_explicit
    if ocr_mode == "force":
        if use_hybrid:
            raw = _run_convert(pdf_path, raw_dir, opts,
                               _hybrid_kwargs(opts, full_pages=True, hancom_force=True))
            engine = f"hybrid:{opts.get('hybrid') or 'docling-fast'}:force"
            page_info = analyze_pages(raw, pdf_path)
        else:
            # native parse for structure/page-count, then OCR every SCANNED
            # page in-process. Digital-text pages are left completely alone --
            # "force" means "force the OCR engine on pages that need OCR",
            # not "OCR every page regardless of content". A fully-scanned
            # document (e.g. one photographed page) has every page in
            # scanned_pages anyway, so this covers that case too.
            raw = _run_convert(pdf_path, raw_dir, opts, None)
            page_info = analyze_pages(raw, pdf_path)
            targets = page_info["scanned_pages"]
            print(f"    force OCR: {opts['ocr_engine']} on scanned page(s) {targets} "
                  f"(lang={opts['ocr_lang']})")
            page_engines = run_builtin_ocr(pdf_path, targets, raw,
                                           opts["ocr_engine"], opts["ocr_lang"], opts["ocr_dpi"],
                                           page_sizes, opts)
            engine = f"builtin-ocr:{opts['ocr_engine']}:force" if page_engines else "native"
            # NOTE: intentionally NOT re-running analyze_pages(raw) here --
            # once OCR splices real text into a scanned page, re-analyzing
            # would see that text and misclassify the page back to "digital",
            # which silently disables Enhanced Mode for a page that only
            # ever had OCR'd (not native) text. The pre-OCR page_info
            # correctly reflects the page's original nature.
    else:
        use_hybrid_now = hybrid_explicit and opts.get("hybrid") not in (None, "off")
        raw = _run_convert(pdf_path, raw_dir, opts,
                           _hybrid_kwargs(opts, full_pages=False) if use_hybrid_now else None)
        if use_hybrid_now:
            engine = f"hybrid:{opts['hybrid']}"

        page_info = analyze_pages(raw, pdf_path)
        cls = page_info["classification"]
        # ---- Pass 2 (auto-OCR) : only if some page needs it -----------------
        need_ocr = cls in ("scanned", "mixed")
        if need_ocr and ocr_mode == "auto" and engine == "native":
            targets = page_info["scanned_pages"]
            if use_hybrid:
                # --- external Docling/EasyOCR server path -------------------
                backend = opts.get("hybrid") or "docling-fast"
                full_pages = (cls == "scanned")
                scope = "all pages" if full_pages else f"scanned pages {targets}"
                print(f"    {cls} PDF ({page_info['n_scanned']}/{page_info['total_pages']} "
                      f"scanned) -> OCR via {backend} server, {scope}")
                try:
                    raw = _run_convert(pdf_path, raw_dir, opts,
                                       _hybrid_kwargs(opts, full_pages=full_pages))
                    engine = f"hybrid:{backend}:{'full' if full_pages else 'triage'}"
                    page_info = analyze_pages(raw, pdf_path)
                except Exception as e:  # noqa: BLE001
                    print(f"    ! hybrid OCR failed ({e}). Start the server with:\n"
                          f"        pip install \"opendataloader-pdf[hybrid]\"\n"
                          f"        opendataloader-pdf-hybrid --port 5002", file=sys.stderr)
            else:
                # --- built-in in-process OCR (no server needed) -------------
                print(f"    {cls} PDF ({page_info['n_scanned']}/{page_info['total_pages']} "
                      f"scanned) -> OCR pages {targets} with {opts['ocr_engine']} "
                      f"(lang={opts['ocr_lang']})")
                page_engines = run_builtin_ocr(pdf_path, targets, raw,
                                               opts["ocr_engine"], opts["ocr_lang"], opts["ocr_dpi"],
                                               page_sizes, opts)
                if page_engines:
                    engine = f"builtin-ocr:{opts['ocr_engine']}"
                    # NOTE: intentionally NOT re-running analyze_pages(raw) here --
                    # see the matching note in the force branch above.
        elif need_ocr and ocr_mode == "off":
            print(f"    ! WARNING: {cls} PDF ({page_info['n_scanned']}/"
                  f"{page_info['total_pages']} pages scanned). OCR is off, so those pages "
                  f"have no text. Re-run with --ocr auto to OCR them.", file=sys.stderr)

    header = parse_doc_header(raw, pdf_path)
    n_pages = header.get("total_pages") or len(page_sizes) or 1

    # `_run_convert` writes the native parse before built-in OCR mutates it in
    # memory. Persist the enriched tree as well: Enhanced Mode can then clone
    # it and replace one page without losing Tesseract text from other pages.
    # This deliberately remains the parser cache, not a user-facing result.
    (raw_dir / f"{pdf_path.stem}.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if opts.get("chunk_mode") == "page":
        # forced page-per-page chunking (e.g. slide decks)
        chunks = build_fallback_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
        used_fallback = True
    else:
        chunks = build_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
        used_fallback = False
        if not chunks:
            # No sections found at all -> page-based fallback so RAG still works.
            chunks = build_fallback_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
            used_fallback = bool(chunks)
        elif n_pages > 1 and all(c["section_number"] == "0" for c in chunks):
            # Only a front-matter chunk spanning several pages (e.g. a slide deck
            # or an untitled multi-page doc) -> chunk per page instead of one blob.
            chunks = build_fallback_chunks(raw, header, raw_dir, images_dir, pdf_path.stem)
            used_fallback = bool(chunks)

    # Page metadata for the UI: dimensions (+ rendered page image if requested).
    pages_meta = build_pages_meta(pdf_path, images_dir, pdf_path.stem,
                                  page_sizes, opts.get("render_pages", False),
                                  opts.get("render_dpi", 150))

    result = {
        **header,
        "num_chunks": len(chunks),
        "parse": {
            "engine": engine,
            # Automatic escalation may use LightOn while the requested
            # document engine remains Tesseract. Page provenance is the truth.
            "mode": (MODE_ENHANCED if ENGINE_LIGHTON in page_engines.values()
                     else MODE_NORMAL),
            "reading_order": opts["reading_order"],
            "table_method": opts["table_method"],
            "use_struct_tree": opts["use_struct_tree"],
            "ocr": ocr_mode,
            "page_classification": page_info["classification"],
            "pages_scanned": page_info["n_scanned"],
            "scanned_page_numbers": page_info.get("scanned_pages", []),
            "page_engines": {str(k): v for k, v in page_engines.items()},
            "chunking": "page-fallback" if used_fallback else "sections",
            "coordinate_origin": "bottom-left",
        },
        "pages": pages_meta,
        "chunks": chunks,
    }
    out = output_dir / f"{pdf_path.stem}.chunks.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if _IMG_FILTER["dropped"]:
        print(f"    dropped {_IMG_FILTER['dropped']} blank/mask image(s)")
    return out


def collect_pdfs(inputs):
    pdfs = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            pdfs.extend(sorted(p.rglob("*.pdf")))
        elif p.suffix.lower() == ".pdf" and p.exists():
            pdfs.append(p)
        else:
            print(f"  ! skipping (not a PDF / not found): {item}", file=sys.stderr)
    return pdfs


def make_opts(**overrides) -> dict:
    """Build a full opts dict (all keys parse_one expects) with sensible defaults.
    Lets other modules (ingest.py, the web app) call parse_one without argparse."""
    opts = {
        "image_format": "png",
        "image_output": "external",
        "drop_blank_images": True,
        "render_pages": False,
        "render_dpi": settings.PAGE_PREVIEW_DPI,
        "chunk_mode": "auto",
        "reading_order": "xycut",
        "table_method": "default",
        "use_struct_tree": False,
        "include_header_footer": False,
        "detect_strikethrough": False,
        "keep_line_breaks": False,
        "password": None,
        "pages": None,
        "content_safety_off": None,
        "threads": None,
        "quiet": True,
        "ocr": "auto",
        "ocr_engine": "tesseract",
        "ocr_lang": "eng",
        "handwriting": "auto",
        "handwriting_threshold": HANDWRITING_CONF_THRESHOLD,
        "handwriting_min_words": HANDWRITING_MIN_WORDS,
        "lighton_device": None,
        "lighton_max_new_tokens": settings.LIGHTON_MAX_NEW_TOKENS,
        "ocr_dpi": settings.OCR_RENDER_DPI,
        "lighton_dpi": settings.LIGHTON_RENDER_DPI,
        "hybrid": None,
        "hybrid_mode": None,
        "hybrid_url": None,
        "hybrid_timeout": None,
        "hybrid_fallback": False,
    }
    opts.update(overrides)
    opts["_hybrid_explicit"] = opts.get("hybrid") not in (None, "off")
    if opts.get("hybrid_url") and not opts.get("hybrid"):
        opts["hybrid"] = "docling-fast"
    return opts


def main():
    ap = argparse.ArgumentParser(
        description="Parse numbered manuals to section-oriented RAG JSON "
                    "(scanned/multicolumn/complex/normal PDFs).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+", help="PDF files and/or folders")
    ap.add_argument("-o", "--output", default="parsed_output")
    ap.add_argument("--auto", action="store_true",
                    help="one-flag smart mode: cluster tables + auto-OCR for scans "
                         "(multi-column is always on via xycut). Combine with --hybrid-url "
                         "for real OCR of scanned PDFs.")

    # image extraction
    ap.add_argument("--image-format", default="png", choices=["png", "jpeg"])
    ap.add_argument("--image-output", default="external",
                    choices=["external", "embedded", "off"],
                    help="external=file refs, embedded=base64, off=no images")
    ap.add_argument("--keep-blank-images", action="store_true",
                    help="keep near-blank / solid-colour mask images (dropped by default)")
    ap.add_argument("--render-pages", action="store_true",
                    help="rasterize each page to a PNG (for the UI box-overlay background)")
    ap.add_argument("--render-dpi", type=int, default=150,
                    help="DPI for --render-pages page images")
    ap.add_argument("--chunk-mode", default="auto", choices=["auto", "page"],
                    help="auto=section chunks (fallback to pages); page=one chunk per page")

    # layout / structure
    ap.add_argument("--reading-order", default="xycut", choices=["xycut", "off"],
                    help="xycut handles multi-column layouts")
    ap.add_argument("--table-method", default="default", choices=["default", "cluster"],
                    help="cluster = better for borderless/complex tables")
    ap.add_argument("--use-struct-tree", action="store_true",
                    help="use tagged-PDF structure tree (best for well-tagged PDFs)")
    ap.add_argument("--include-header-footer", action="store_true")
    ap.add_argument("--detect-strikethrough", action="store_true")
    ap.add_argument("--keep-line-breaks", action="store_true")

    # access / scope
    ap.add_argument("--password", default=None, help="password for encrypted PDFs")
    ap.add_argument("--pages", default=None, help='page range, e.g. "1,3,5-7"')
    ap.add_argument("--content-safety-off", default=None,
                    help="disable safety filters: all,hidden-text,off-page,tiny,hidden-ocg")
    ap.add_argument("--threads", default=None, help="worker threads (>1 experimental)")
    ap.add_argument("--quiet", action="store_true", help="suppress converter logging")

    # OCR / hybrid backend (for scanned PDFs)
    # OCR (built-in, in-process) — no server required
    ap.add_argument("--ocr", default="auto", choices=["off", "auto", "force"],
                    help="off=text layer only; auto=native for digital + OCR scanned "
                         "pages in-process (default); force=OCR every scanned page")
    ap.add_argument("--ocr-engine", default="tesseract",
                    choices=["tesseract", "easyocr", "lighton"],
                    help="built-in OCR engine (tesseract needs the system binary; "
                         "easyocr is heavier; lighton = LightOnOCR-2-1B, handwriting-capable)")
    ap.add_argument("--handwriting", default="auto", choices=["off", "auto", "force"],
                    help="off=Tesseract only; auto=escalate low-confidence pages to "
                         "LightOnOCR-2-1B (handwriting); force=always LightOnOCR-2-1B")
    ap.add_argument("--handwriting-threshold", type=float, default=HANDWRITING_CONF_THRESHOLD,
                    help="in auto mode, escalate a page to LightOnOCR-2-1B when mean Tesseract "
                         f"confidence <= this (default {HANDWRITING_CONF_THRESHOLD:.0f}; raise to catch more handwriting)")
    ap.add_argument("--lighton-device", default=None,
                    help="LightOnOCR-2-1B device, e.g. 'cpu', 'cuda', or 'mps' (default: auto)")
    ap.add_argument("--lighton-max-tokens", type=int, default=settings.LIGHTON_MAX_NEW_TOKENS,
                    help="max tokens LightOnOCR-2-1B may generate per page; higher avoids "
                         "truncation on dense pages but is slower, esp. on CPU (default 2048)")
    ap.add_argument("--ocr-lang", default="eng",
                    help="OCR language(s). tesseract: 'eng','deu','eng+deu'. easyocr: 'en','en,hi'")
    ap.add_argument("--ocr-dpi", type=int, default=settings.OCR_RENDER_DPI,
                    help="render DPI for Tesseract (and its confidence check); higher "
                         f"improves Tesseract accuracy but costs more per page (default {settings.OCR_RENDER_DPI})")
    ap.add_argument("--lighton-dpi", type=int, default=settings.LIGHTON_RENDER_DPI,
                    help="render DPI used ONLY for LightOnOCR-2-1B calls (explicit "
                         "engine, --handwriting force, or an auto escalation) -- kept "
                         "separate from --ocr-dpi since LightOnOCR's generation cost "
                         f"scales with image size (default {settings.LIGHTON_RENDER_DPI})")

    # OCR via external hybrid server (advanced alternative to built-in)
    ap.add_argument("--hybrid", default=None, choices=["off", "docling-fast", "hancom-ai"],
                    help="advanced: use an external hybrid OCR server instead of built-in OCR")
    ap.add_argument("--hybrid-mode", default=None, choices=["auto", "full"])
    ap.add_argument("--hybrid-url", default=None, help="hybrid server URL, e.g. http://localhost:5002")
    ap.add_argument("--hybrid-timeout", default=None, help="ms (0 = no timeout)")
    ap.add_argument("--hybrid-fallback", action="store_true",
                    help="fall back to Java pipeline if the hybrid backend errors")
    args = ap.parse_args()

    # --auto: pick smart defaults unless the user overrode them explicitly.
    if args.auto:
        if args.table_method == "default":
            args.table_method = "cluster"
        if args.ocr == "off":
            args.ocr = "auto"

    output_dir = Path(args.output)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    opts = {
        "image_format": args.image_format,
        "image_output": args.image_output,
        "drop_blank_images": not args.keep_blank_images,
        "render_pages": args.render_pages,
        "render_dpi": args.render_dpi,
        "chunk_mode": args.chunk_mode,
        "reading_order": args.reading_order,
        "table_method": args.table_method,
        "use_struct_tree": args.use_struct_tree,
        "include_header_footer": args.include_header_footer,
        "detect_strikethrough": args.detect_strikethrough,
        "keep_line_breaks": args.keep_line_breaks,
        "password": args.password,
        "pages": args.pages,
        "content_safety_off": args.content_safety_off,
        "threads": args.threads,
        "quiet": args.quiet,
        "ocr": args.ocr,
        "ocr_engine": args.ocr_engine,
        "handwriting": args.handwriting,
        "handwriting_threshold": args.handwriting_threshold,
        "lighton_device": args.lighton_device,
        "lighton_max_new_tokens": args.lighton_max_tokens,
        "ocr_lang": args.ocr_lang,
        "ocr_dpi": args.ocr_dpi,
        "lighton_dpi": args.lighton_dpi,
        "hybrid": args.hybrid,
        "hybrid_mode": args.hybrid_mode,
        "hybrid_url": args.hybrid_url,
        "hybrid_timeout": args.hybrid_timeout,
        "hybrid_fallback": args.hybrid_fallback,
    }
    # Built-in OCR is the default. Only default a hybrid backend when the user
    # actually points at a server (--hybrid-url) or names a backend (--hybrid).
    opts["_hybrid_explicit"] = args.hybrid not in (None, "off")
    if opts["hybrid_url"] and not opts["hybrid"]:
        opts["hybrid"] = "docling-fast"

    pdfs = collect_pdfs(args.inputs)
    if not pdfs:
        print("No PDFs found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(pdfs)} PDF(s). Output -> {output_dir.resolve()}")
    written = []
    for pdf in pdfs:
        print(f"  parsing {pdf} ...")
        try:
            out = parse_one(pdf, output_dir, images_dir, opts)
            written.append(out)
            print(f"    -> {out.name}")
        except Exception as e:  # noqa: BLE001
            print(f"    FAILED: {e}", file=sys.stderr)

    shutil.rmtree(output_dir / "_raw", ignore_errors=True)
    print(f"\nDone. {len(written)} file(s) written. Images in {images_dir}/")


if __name__ == "__main__":
    main()