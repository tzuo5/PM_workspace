# -*- coding: utf-8 -*-
"""PDF evidence extraction with coordinate-aware text spans.

Uses PyMuPDF (fitz) as primary parser to obtain word-level bounding boxes
and falls back to pdfplumber when PyMuPDF is unavailable.

All coordinates use PDF point space (origin bottom-left).
Frontend must convert these to viewport space via PDF.js
viewport.convertToViewportRectangle().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TextSpan:
    """A single text span with its bounding box in PDF point coordinates."""
    text: str
    bbox: Tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass
class ParsedPage:
    """A parsed PDF page with text, spans, and metadata."""
    page_num: int
    width: float
    height: float
    text: str
    spans: List[TextSpan] = field(default_factory=list)
    tables: List[Any] = field(default_factory=list)


@dataclass
class ParsedPDF:
    """A fully parsed PDF file."""
    filepath: str
    pages: List[ParsedPage] = field(default_factory=list)
    full_text: str = ""


def parse_pdf_with_evidence(filepath: str) -> ParsedPDF:
    """Parse a PDF file extracting both text and coordinate evidence.

    Tries PyMuPDF first (best coordinates), then pdfplumber.
    """
    result = ParsedPDF(filepath=filepath)
    pages: List[ParsedPage] = []
    full_text_parts: List[str] = []

    try:
        import fitz
        doc = fitz.open(filepath)
        for i, page in enumerate(doc, start=1):
            p = _parse_page_pymupdf(page, i)
            pages.append(p)
            full_text_parts.append(p.text)
        doc.close()
        result.pages = pages
        result.full_text = "\n".join(full_text_parts)
        if len(result.full_text.strip()) < 20:
            # Fallback if PyMuPDF returned too little text
            result = _parse_pdf_plumber_evidence(filepath)
    except ImportError:
        result = _parse_pdf_plumber_evidence(filepath)
    except Exception:
        try:
            result = _parse_pdf_plumber_evidence(filepath)
        except Exception:
            pass

    return result


def _parse_page_pymupdf(page, page_num: int) -> ParsedPage:
    """Parse a single page with PyMuPDF, extracting word-level bboxes."""
    text = page.get_text()
    rect = page.rect
    width = rect.width
    height = rect.height

    spans: List[TextSpan] = []
    # get_text("words") returns list of [x0,y0,x1,y1, word, block, line, word_no]
    try:
        words = page.get_text("words")
        for w in words:
            if len(w) >= 5:
                x0, y0, x1, y1, word_text = w[0], w[1], w[2], w[3], w[4]
                spans.append(TextSpan(text=str(word_text), bbox=(x0, y0, x1, y1)))
    except Exception:
        pass

    # Also get tables via PyMuPDF if available
    tables: List[Any] = []
    try:
        tabs = page.find_tables()
        if tabs and tabs.tables:
            for t in tabs.tables:
                rows = []
                for cell_row in t.extract():
                    rows.append([str(c or "") for c in cell_row])
                tables.append(rows)
    except Exception:
        pass

    return ParsedPage(page_num=page_num, width=width, height=height, text=text, spans=spans, tables=tables)


def _parse_pdf_plumber_evidence(filepath: str) -> ParsedPDF:
    """Fallback: parse with pdfplumber extracting character-level bboxes."""
    result = ParsedPDF(filepath=filepath)
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                p = _parse_page_plumber(page, i)
                result.pages.append(p)
        result.full_text = "\n".join(p.text for p in result.pages)
    except Exception:
        pass
    return result


def _parse_page_plumber(page, page_num: int) -> ParsedPage:
    """Parse a single page with pdfplumber, extracting character bboxes."""
    text = page.extract_text() or ""
    width = float(page.width or 612)
    height = float(page.height or 792)

    spans: List[TextSpan] = []
    try:
        chars = page.chars
        if chars:
            # Group chars into words by proximity
            current_word = ""
            current_bbox = None
            for ch in chars:
                ch_text = ch.get("text", "")
                x0 = float(ch.get("x0", 0))
                y0 = float(ch.get("top", 0))
                x1 = float(ch.get("x1", 0))
                y1 = float(ch.get("bottom", 0))

                if ch_text in (" ", "\t", "\n"):
                    if current_word:
                        spans.append(TextSpan(text=current_word, bbox=current_bbox))
                        current_word = ""
                        current_bbox = None
                else:
                    if current_bbox is None:
                        current_bbox = (x0, y0, x1, y1)
                    else:
                        current_bbox = (
                            min(current_bbox[0], x0),
                            min(current_bbox[1], y0),
                            max(current_bbox[2], x1),
                            max(current_bbox[3], y1),
                        )
                    current_word += ch_text
            if current_word:
                spans.append(TextSpan(text=current_word, bbox=current_bbox))
    except Exception:
        pass

    tables = page.extract_tables() or []
    return ParsedPage(page_num=page_num, width=width, height=height, text=text, spans=spans, tables=tables)


def normalize_for_match(text: str) -> str:
    """Normalize text for matching: remove extra spaces, unify chars.

    Handles:
    - Spaces between CJK characters
    - Fullwidth / halfwidth
    - Line breaks
    - Hyphenation
    - Common OCR errors
    - Case differences
    - IRB1200 vs IRB 1200
    """
    if not text:
        return ""

    s = text.lower().strip()

    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s)

    # Remove spaces between CJK characters
    s = re.sub(r'(?<=[\u4e00-\u9fff\u3400-\u4dbf])\s+(?=[\u4e00-\u9fff\u3400-\u4dbf])', '', s)

    # Remove spaces around fullwidth punctuation
    s = re.sub(r'\s*([\uff01-\uff5e])\s*', r'\1', s)

    # Unify common patterns
    s = re.sub(r'irb\s*(\d)', r'irb\1', s)  # IRB1200 vs IRB 1200
    s = re.sub(r'(\d)\s*/\s*(\d)', r'\1/\2', s)  # 7 / 0.7 -> 7/0.7

    # Remove hyphens at line breaks
    s = re.sub(r'-\s+', '', s)

    # Unify quotes
    s = s.replace('\u2018', "'").replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')

    # Collapse spaces again after transformations
    s = re.sub(r'\s+', ' ', s).strip()

    return s


def find_text_spans(
    page: ParsedPage,
    search_text: str,
    max_results: int = 10,
) -> List[Tuple[float, float, float, float]]:
    """Find all bbox rectangles where search_text appears on a page.

    Uses greedy matching within the span list.
    Returns list of (x0, y0, x1, y1) rects.
    """
    if not search_text or not page.spans:
        return []

    normalized_search = normalize_for_match(search_text)
    if not normalized_search:
        return []

    # Build concatenated span text for matching
    words = [s.text for s in page.spans]

    rects: List[Tuple[float, float, float, float]] = []

    # Try exact span match first
    for span in page.spans:
        if normalize_for_match(span.text) == normalized_search:
            rects.append(span.bbox)
            if len(rects) >= max_results:
                return rects

    # Try substring containment matching
    for span in page.spans:
        norm_span = normalize_for_match(span.text)
        if normalized_search in norm_span or norm_span in normalized_search:
            rects.append(span.bbox)
            if len(rects) >= max_results:
                return rects

    # Try greedy multi-span matching
    total_words = len(words)
    search_tokens = normalized_search.split()
    if len(search_tokens) <= 1:
        return rects

    # Sliding window over normalized words
    norm_words = [normalize_for_match(w) for w in words]
    for start in range(total_words):
        remaining = list(search_tokens)
        matched_spans: List[TextSpan] = []
        idx = start
        while remaining and idx < total_words:
            if remaining[0] in norm_words[idx] or norm_words[idx] in remaining[0]:
                remaining.pop(0)
                matched_spans.append(page.spans[idx])
            elif len(remaining[0]) <= 3 and idx + 1 < total_words:
                # Short token may span across words
                pass
            idx += 1
        if not remaining:
            # All tokens matched
            if matched_spans:
                bbox = (
                    min(s.bbox[0] for s in matched_spans),
                    min(s.bbox[1] for s in matched_spans),
                    max(s.bbox[2] for s in matched_spans),
                    max(s.bbox[3] for s in matched_spans),
                )
                rects.append(bbox)
                if len(rects) >= max_results:
                    return rects

    return rects


def build_evidence_entry(
    parsed_pdf: ParsedPDF,
    document_type: str,
    page_num: int,
    label: str,
    quote: str,
    rects: Optional[List[Tuple[float, float, float, float]]] = None,
) -> Dict[str, Any]:
    """Build a standard evidence dict for a review item."""
    entry: Dict[str, Any] = {
        "document_type": document_type,
        "page": page_num,
        "label": label,
        "quote": quote,
        "rects": rects or [],
    }
    return entry


def locate_evidence_for_field(
    parsed_pdf: ParsedPDF,
    document_type: str,
    field_value: str,
    context_patterns: List[str],
    label: str,
) -> Dict[str, Any]:
    """Locate evidence for a single extracted field.

    Searches through all pages for the field_value or context patterns.
    Returns an evidence dict with page, quote, and rects.
    """
    if not field_value and not context_patterns:
        return {
            "document_type": document_type,
            "page": 0,
            "label": label,
            "quote": "",
            "rects": [],
        }

    search_targets = [field_value] if field_value else []
    search_targets.extend(context_patterns)

    for page in parsed_pdf.pages:
        page_text = normalize_for_match(page.text)
        for target in search_targets:
            normalized_target = normalize_for_match(target)
            if not normalized_target:
                continue
            if len(normalized_target) < 3:
                continue
            if normalized_target in page_text:
                rects = find_text_spans(page, target, max_results=5)
                return {
                    "document_type": document_type,
                    "page": page.page_num,
                    "label": label,
                    "quote": target[:200],
                    "rects": rects or [],
                }

    return {
        "document_type": document_type,
        "page": 0,
        "label": label,
        "quote": field_value[:200] if field_value else "",
        "rects": [],
    }