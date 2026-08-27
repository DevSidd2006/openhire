"""
Plain-text extraction from uploaded resume documents (PDF, DOCX), with an
OCR fallback for scanned/image-based documents.

This module does exactly one job: turn a `.pdf`/`.docx` file's bytes into
the plain text `ResumeParserAgent` (agents/resume_parser/agent.py) already
expects as its `resume_text` argument. It is deliberately NOT a resume
parser itself - no field extraction, no LLM call, nothing domain-specific
happens here. `api/routes/candidates.py`'s upload endpoint is the only
caller; everything downstream of `extract_resume_text` is the existing,
unmodified `CandidateService`/`ResumeParserAgent` pipeline. OCR's job here
is IMAGE -> TEXT only; TEXT -> STRUCTURED RESUME DATA stays entirely with
the existing agent.

Supported formats: `.pdf` (via `pypdf`, with `pymupdf` used only to render
pages to images for OCR) and `.docx` (via `python-docx`, with its embedded
images pulled out for OCR) - all established, widely-used libraries. Legacy
`.doc` (the pre-2007 binary Word format) is explicitly NOT supported: there
is no reliable extraction library already in this project's dependencies
for it, and silently mis-extracting garbled text from a `.doc` file would
be worse than refusing it outright.

OCR fallback
------------
Normal extraction runs first, always. OCR (`rapidocr-onnxruntime` - pure
Python, no separate system binary/install required, which is what made it
practical to add in this environment over e.g. Tesseract) only runs when
normal extraction's result does NOT look like meaningful resume text (see
`_looks_meaningful`) - never unconditionally. This keeps a normal,
text-based resume on the fast, cheap path and reserves OCR (slower, and a
lossier re-derivation of the text) for the case it actually exists to
handle: a scanned/image-based document with no real text layer.
"""
from __future__ import annotations

import io
import re
from pathlib import PurePosixPath
from typing import List, Optional, Tuple

from docx import Document
from pypdf import PdfReader
from pypdf.errors import PdfReadError


class UnsupportedResumeFormatError(Exception):
    """The uploaded file's extension is not one this endpoint accepts."""


class ResumeExtractionError(Exception):
    """The file has a supported extension but its content could not be
    turned into usable text (corrupted, encrypted, or genuinely empty -
    including after an OCR fallback attempt)."""


_SUPPORTED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx"}

# A resume is prose: this is a deliberately low bar (not a quality check on
# the resume's CONTENT, just "is this obviously not empty/near-empty
# extraction noise") - it only has to distinguish "pypdf/python-docx found
# real sentences" from "found nothing, or a handful of stray characters
# pulled off a scanned page's incidental vector art/watermark". Real resume
# text clears this by a wide margin; a genuinely image-only page returns
# empty or near-empty from both libraries and fails it easily.
_MIN_MEANINGFUL_CHARS = 40
_MIN_MEANINGFUL_WORDS = 5


def _looks_meaningful(text: Optional[str]) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < _MIN_MEANINGFUL_CHARS:
        return False
    words = re.findall(r"[A-Za-z]{2,}", stripped)
    return len(words) >= _MIN_MEANINGFUL_WORDS


# ---------------------------------------------------------------------
# OCR: image bytes -> text. Nothing else in this module (or anywhere else
# in the codebase) does image-to-text - this is the one place it happens,
# and it is a plain function call, not an agent.
# ---------------------------------------------------------------------

_ocr_engine = None


def _get_ocr_engine():
    """Lazily construct the RapidOCR engine (loads its bundled ONNX models
    from disk) on first actual use, not at import time - most requests
    never need OCR at all (see `_looks_meaningful`), and constructing this
    unconditionally would pay that cost on every process start for no
    reason."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_image_bytes(image_bytes: bytes) -> str:
    engine = _get_ocr_engine()
    result, _elapse = engine(image_bytes)
    if not result:
        return ""
    # Each result entry is [box_points, recognized_text, confidence] (see
    # RapidOCR.__call__) - only the text is relevant here, in reading order
    # as RapidOCR's own layout detection already produced it.
    return "\n".join(entry[1] for entry in result if entry[1])


def _ocr_pdf_pages(content: bytes) -> str:
    import pymupdf

    texts: List[str] = []
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        for page in doc:
            # 200 DPI: enough resolution for OCR accuracy on typical resume
            # font sizes without producing an unreasonably large bitmap per
            # page (72 DPI is the PDF default and is often too blurry for
            # reliable OCR).
            pixmap = page.get_pixmap(dpi=200)
            page_text = _ocr_image_bytes(pixmap.tobytes("png"))
            if page_text:
                texts.append(page_text)
    return "\n".join(texts)


def _ocr_docx_images(content: bytes) -> str:
    document = Document(io.BytesIO(content))
    texts: List[str] = []
    for part in document.part.related_parts.values():
        content_type = getattr(part, "content_type", "")
        if not content_type.startswith("image/"):
            continue
        image_text = _ocr_image_bytes(part.blob)
        if image_text:
            texts.append(image_text)
    return "\n".join(texts)


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def extract_resume_text(filename: str, content: bytes) -> Tuple[str, str]:
    """Returns (plain_text, source_format) for a `.pdf` or `.docx` file's
    raw bytes. `source_format` is one of "pdf"/"docx" - the same vocabulary
    `ParsedResume.source_format` (schemas/resume.py) already documents.

    Tries normal text extraction first; if that doesn't look like
    meaningful resume text (`_looks_meaningful`), falls back to OCR over
    the document's rendered pages (PDF) or embedded images (DOCX) and uses
    that instead. Never runs OCR when normal extraction already produced
    real text - see this module's docstring for why.

    Raises `UnsupportedResumeFormatError` for any other extension (including
    legacy `.doc`), and `ResumeExtractionError` for a same-extension file
    that cannot actually be read (corrupted, password-protected, or with no
    extractable text at all even after the OCR fallback).
    """
    suffix = PurePosixPath(filename or "").suffix.lower()

    if suffix == ".doc":
        raise UnsupportedResumeFormatError(
            "Legacy .doc files are not supported - please save your resume "
            "as .docx or .pdf and upload that instead."
        )
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise UnsupportedResumeFormatError(
            f"Unsupported file type {suffix or '(unknown)'!r} - please "
            "upload a .pdf or .docx resume."
        )

    source_format = _SUPPORTED_EXTENSIONS[suffix]
    if source_format == "pdf":
        text = _extract_pdf_text(content)
        if not _looks_meaningful(text):
            text = _ocr_pdf_pages(content).strip()
    else:
        text = _extract_docx_text(content)
        if not _looks_meaningful(text):
            text = _ocr_docx_images(content).strip()

    text = (text or "").strip()
    if not text:
        raise ResumeExtractionError(
            "No extractable text was found in this document, even after "
            "attempting OCR - it may be empty or the scan quality may be "
            "too low to read."
        )
    return text, source_format


def _extract_pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            # An empty-password attempt is the only thing worth trying
            # automatically - anything genuinely password-protected should
            # fail cleanly rather than prompt for a credential this
            # endpoint has no UI for.
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ResumeExtractionError(
                    "This PDF is password-protected and cannot be read."
                ) from exc
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except ResumeExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ResumeExtractionError(
            "This PDF could not be read - it may be corrupted."
        ) from exc


def _extract_docx_text(content: bytes) -> str:
    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        # python-docx raises a mix of its own and zipfile/lxml exceptions
        # for a corrupted/non-DOCX file - none of them are worth
        # distinguishing for the caller, who just needs "unreadable".
        raise ResumeExtractionError(
            "This DOCX file could not be read - it may be corrupted."
        ) from exc

    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts).strip()


__all__ = [
    "ResumeExtractionError",
    "UnsupportedResumeFormatError",
    "extract_resume_text",
]
