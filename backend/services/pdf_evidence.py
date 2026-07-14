# -*- coding: utf-8 -*-
"""Coordinate-aware PDF text extraction used by contract review.

The module is deliberately read-only: it never writes to or modifies the source
PDF. PyMuPDF is used first because its word boxes align reliably with PDF.js
page dimensions. Coordinates are stored in page point space with a top-left
origin, which is also how PyMuPDF exposes text boxes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BBox = Tuple[float, float, float, float]


@dataclass
class TextSpan:
    text: str
    bbox: BBox
    block_no: int = 0
    line_no: int = 0
    word_no: int = 0


@dataclass
class ParsedPage:
    page_num: int
    width: float
    height: float
    text: str
    spans: List[TextSpan] = field(default_factory=list)
    tables: List[Any] = field(default_factory=list)


@dataclass
class ParsedPDF:
    filepath: str
    pages: List[ParsedPage] = field(default_factory=list)
    full_text: str = ""


def parse_pdf_with_evidence(filepath: str) -> ParsedPDF:
    """Read a PDF and extract text plus word coordinates without mutating it."""
    try:
        import fitz  # PyMuPDF

        pages: List[ParsedPage] = []
        with fitz.open(filepath) as doc:
            for page_index, page in enumerate(doc, start=1):
                words = page.get_text("words", sort=True) or []
                spans = [
                    TextSpan(
                        text=str(word[4]),
                        bbox=(float(word[0]), float(word[1]), float(word[2]), float(word[3])),
                        block_no=int(word[5]) if len(word) > 5 else 0,
                        line_no=int(word[6]) if len(word) > 6 else 0,
                        word_no=int(word[7]) if len(word) > 7 else 0,
                    )
                    for word in words
                    if len(word) >= 5 and str(word[4]).strip()
                ]
                tables: List[Any] = []
                try:
                    finder = page.find_tables()
                    for table in getattr(finder, "tables", []) or []:
                        tables.append([[str(cell or "") for cell in row] for row in table.extract()])
                except Exception:
                    pass
                pages.append(
                    ParsedPage(
                        page_num=page_index,
                        width=float(page.rect.width),
                        height=float(page.rect.height),
                        text=page.get_text("text", sort=True) or "",
                        spans=spans,
                        tables=tables,
                    )
                )
        result = ParsedPDF(filepath=filepath, pages=pages, full_text="\n".join(p.text for p in pages))
        if result.full_text.strip():
            return result
    except Exception:
        pass

    return _parse_with_pdfplumber(filepath)


def _parse_with_pdfplumber(filepath: str) -> ParsedPDF:
    result = ParsedPDF(filepath=filepath)
    try:
        import pdfplumber

        with pdfplumber.open(filepath) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                spans: List[TextSpan] = []
                for word_no, word in enumerate(page.extract_words(use_text_flow=True) or []):
                    spans.append(
                        TextSpan(
                            text=str(word.get("text", "")),
                            bbox=(
                                float(word.get("x0", 0)),
                                float(word.get("top", 0)),
                                float(word.get("x1", 0)),
                                float(word.get("bottom", 0)),
                            ),
                            word_no=word_no,
                        )
                    )
                result.pages.append(
                    ParsedPage(
                        page_num=page_index,
                        width=float(page.width or 612),
                        height=float(page.height or 792),
                        text=page.extract_text() or "",
                        spans=spans,
                        tables=page.extract_tables() or [],
                    )
                )
        result.full_text = "\n".join(page.text for page in result.pages)
    except Exception:
        pass
    return result


def normalize_for_match(text: str) -> str:
    """Normalize text while retaining meaningful punctuation and numbers."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).lower()
    value = value.replace("–", "-").replace("—", "-").replace("−", "-")
    value = value.replace("／", "/").replace("％", "%")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", value)
    value = re.sub(r"irb\s+(?=\d)", "irb", value)
    value = re.sub(r"\s*([/,:：()（）%])\s*", r"\1", value)
    return value


def match_key(text: str) -> str:
    """Aggressive key used to align visually equivalent PDF text."""
    value = normalize_for_match(text)
    value = re.sub(r"[\s_]+", "", value)
    value = value.replace("，", ",").replace("。", ".")
    return value


def _merge_rects_by_line(spans: Sequence[TextSpan]) -> List[BBox]:
    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: (span.bbox[1], span.bbox[0]))
    groups: List[List[TextSpan]] = []
    for span in ordered:
        if not groups:
            groups.append([span])
            continue
        current = groups[-1]
        current_y = sum(item.bbox[1] for item in current) / len(current)
        current_h = max(item.bbox[3] - item.bbox[1] for item in current)
        tolerance = max(2.5, current_h * 0.55)
        if abs(span.bbox[1] - current_y) <= tolerance:
            current.append(span)
        else:
            groups.append([span])
    return [
        (
            min(span.bbox[0] for span in group),
            min(span.bbox[1] for span in group),
            max(span.bbox[2] for span in group),
            max(span.bbox[3] for span in group),
        )
        for group in groups
    ]


def find_text_spans(page: ParsedPage, search_text: str, max_results: int = 12) -> List[BBox]:
    """Locate one phrase on a page and return line-level highlight rectangles."""
    target = match_key(search_text)
    if not target or not page.spans:
        return []

    span_keys = [match_key(span.text) for span in page.spans]
    char_stream: List[str] = []
    char_to_span: List[int] = []
    for span_index, key in enumerate(span_keys):
        for char in key:
            char_stream.append(char)
            char_to_span.append(span_index)
    haystack = "".join(char_stream)
    start = haystack.find(target)
    if start >= 0:
        end = start + len(target)
        indexes = sorted(set(char_to_span[start:end]))
        return _merge_rects_by_line([page.spans[index] for index in indexes])[:max_results]

    # Fallback for a phrase whose separators differ in the PDF.
    tokens = [match_key(token) for token in re.split(r"\s+", normalize_for_match(search_text)) if match_key(token)]
    if len(tokens) > 1:
        matched: List[TextSpan] = []
        cursor = 0
        for token in tokens:
            found = False
            while cursor < len(page.spans):
                if token in span_keys[cursor] or span_keys[cursor] in token:
                    matched.append(page.spans[cursor])
                    cursor += 1
                    found = True
                    break
                cursor += 1
            if not found:
                matched = []
                break
        if matched:
            return _merge_rects_by_line(matched)[:max_results]
    return []


def locate_evidence(
    parsed_pdf: Optional[ParsedPDF],
    document_type: str,
    label: str,
    search_terms: Iterable[str],
    *,
    page_hint: Optional[int] = None,
    quote: str = "",
) -> Dict[str, Any]:
    """Find the first reliable source location for any of ``search_terms``.

    When text exists on a page but word coordinates cannot be recovered, the
    returned entry is still page-addressable and has ``location_status`` set to
    ``page_only``. The UI must show “无法确定位置” rather than inventing a box.
    """
    terms = [str(term).strip() for term in search_terms if str(term).strip()]
    if parsed_pdf is None:
        return evidence_entry(document_type, 0, label, quote or (terms[0] if terms else ""), [])

    pages = parsed_pdf.pages
    if page_hint:
        pages = sorted(pages, key=lambda page: 0 if page.page_num == page_hint else 1)

    for term in terms:
        key = match_key(term)
        if not key:
            continue
        for page in pages:
            if key not in match_key(page.text):
                continue
            rects = find_text_spans(page, term)
            return evidence_entry(
                document_type,
                page.page_num,
                label,
                quote or term,
                rects,
                page_width=page.width,
                page_height=page.height,
            )
    return evidence_entry(document_type, 0, label, quote or (terms[0] if terms else ""), [])


def locate_all_evidence(
    parsed_pdf: Optional[ParsedPDF],
    document_type: str,
    label: str,
    search_terms: Iterable[str],
) -> List[Dict[str, Any]]:
    """Return one evidence entry per term, de-duplicating identical locations."""
    entries: List[Dict[str, Any]] = []
    seen = set()
    for term in search_terms:
        entry = locate_evidence(parsed_pdf, document_type, label, [term], quote=str(term))
        key = (entry.get("page"), tuple(tuple(rect) for rect in entry.get("rects", [])), entry.get("quote"))
        if entry.get("page", 0) > 0 and key not in seen:
            entries.append(entry)
            seen.add(key)
    return entries


def evidence_entry(
    document_type: str,
    page_num: int,
    label: str,
    quote: str,
    rects: Optional[Sequence[BBox]],
    *,
    page_width: float = 0,
    page_height: float = 0,
) -> Dict[str, Any]:
    rect_list = [[float(value) for value in rect] for rect in (rects or [])]
    if page_num <= 0:
        status = "unavailable"
    elif rect_list:
        status = "exact"
    else:
        status = "page_only"
    return {
        "document_type": document_type,
        "page": int(page_num or 0),
        "label": label,
        "quote": quote,
        "rects": rect_list,
        "page_width": float(page_width or 0),
        "page_height": float(page_height or 0),
        "coordinate_origin": "top-left",
        "location_status": status,
    }


# Backwards-compatible names used by older code.
def build_evidence_entry(
    parsed_pdf: ParsedPDF,
    document_type: str,
    page_num: int,
    label: str,
    quote: str,
    rects: Optional[List[BBox]] = None,
) -> Dict[str, Any]:
    page = next((candidate for candidate in parsed_pdf.pages if candidate.page_num == page_num), None)
    return evidence_entry(
        document_type,
        page_num,
        label,
        quote,
        rects,
        page_width=page.width if page else 0,
        page_height=page.height if page else 0,
    )


def locate_evidence_for_field(
    parsed_pdf: ParsedPDF,
    document_type: str,
    field_value: str,
    context_patterns: List[str],
    label: str,
) -> Dict[str, Any]:
    return locate_evidence(parsed_pdf, document_type, label, [field_value, *context_patterns], quote=field_value)
