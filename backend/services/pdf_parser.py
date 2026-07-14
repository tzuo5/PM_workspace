# -*- coding: utf-8 -*-
"""PDF text and coordinate extraction for Document Check.

Uses PyMuPDF (fitz) for structured text extraction with bbox coordinates.
Falls back to pdfplumber when PyMuPDF is unavailable.
Supports OCR via pytesseract only when pages have no usable text layer.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

HAS_FITZ = False
HAS_PDFPLUMBER = False
HAS_OCR = False

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    pass

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    pass

try:
    import pytesseract
    from PIL import Image
    import io
    HAS_OCR = True
except ImportError:
    pass


def normalize_bbox(bbox: Tuple[float, ...], page_width: float, page_height: float) -> Dict[str, float]:
    """Convert absolute bbox to normalized coordinates (0.0-1.0).

    Args:
        bbox: (x0, y0, x1, y1) in page coordinate space
        page_width: page width in points
        page_height: page height in points

    Returns:
        {"x": 0.0-1.0, "y": 0.0-1.0, "width": 0.0-1.0, "height": 0.0-1.0}
    """
    x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    return {
        "x": round(x0 / max(page_width, 1), 4),
        "y": round(y0 / max(page_height, 1), 4),
        "width": round((x1 - x0) / max(page_width, 1), 4),
        "height": round((y1 - y0) / max(page_height, 1), 4),
    }


class SpanInfo:
    """Represents one extracted text span with position info."""
    __slots__ = ("text", "bbox", "font", "font_size", "flags", "page_number",
                 "page_width", "page_height", "normalized_bbox", "block_no", "line_no")

    def __init__(self, text: str, bbox: Tuple[float, ...], font: str = "",
                 font_size: float = 0, flags: int = 0, page_number: int = 1,
                 page_width: float = 612, page_height: float = 792,
                 block_no: int = 0, line_no: int = 0):
        self.text = text
        self.bbox = bbox
        self.font = font
        self.font_size = font_size
        self.flags = flags
        self.page_number = page_number
        self.page_width = page_width
        self.page_height = page_height
        self.normalized_bbox = normalize_bbox(bbox, page_width, page_height)
        self.block_no = block_no
        self.line_no = line_no

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "bbox": list(self.bbox),
            "normalized_bbox": self.normalized_bbox,
            "font": self.font,
            "font_size": self.font_size,
            "flags": self.flags,
            "page_number": self.page_number,
            "page_width": self.page_width,
            "page_height": self.page_height,
            "block_no": self.block_no,
            "line_no": self.line_no,
        }


class PageInfo:
    """Represents extracted info for one PDF page."""
    __slots__ = ("page_number", "spans", "lines", "blocks", "full_text",
                 "page_width", "page_height", "rotation", "has_text_layer",
                 "extraction_method")

    def __init__(self, page_number: int, page_width: float = 612,
                 page_height: float = 792, rotation: int = 0):
        self.page_number = page_number
        self.spans: List[SpanInfo] = []
        self.lines: List[List[SpanInfo]] = []
        self.blocks: List[Dict[str, Any]] = []
        self.full_text = ""
        self.page_width = page_width
        self.page_height = page_height
        self.rotation = rotation
        self.has_text_layer = False
        self.extraction_method = "NONE"


class PDFParseResult:
    """Result of parsing a single PDF file."""
    __slots__ = ("filename", "page_count", "pages", "has_text_layer",
                 "parse_errors", "file_size", "sha256")

    def __init__(self, filename: str = "", page_count: int = 0,
                 file_size: int = 0, sha256: str = ""):
        self.filename = filename
        self.page_count = page_count
        self.pages: List[PageInfo] = []
        self.has_text_layer = False
        self.parse_errors: List[str] = []
        self.file_size = file_size
        self.sha256 = sha256


def parse_pdf_fitz(filepath: str) -> PDFParseResult:
    """Parse PDF using PyMuPDF (fitz) - preferred method."""
    result = PDFParseResult(filename=os.path.basename(filepath))

    try:
        doc = fitz.open(filepath)
        result.page_count = doc.page_count
        result.has_text_layer = True

        for page_num in range(doc.page_count):
            page = doc[page_num]
            page_rect = page.rect
            page_width = page_rect.width
            page_height = page_rect.height
            rotation = page.rotation or 0

            page_info = PageInfo(
                page_number=page_num + 1,
                page_width=page_width,
                page_height=page_height,
                rotation=rotation,
            )
            page_info.extraction_method = "PDF_TEXT"

            # Get structured text dict
            try:
                text_dict = page.get_text("dict")
            except Exception:
                page_info.extraction_method = "OCR"
                page_info.has_text_layer = False
                result.has_text_layer = False
                result.pages.append(page_info)
                continue

            # Check if page has usable text
            blocks_data = text_dict.get("blocks", [])
            total_chars = 0
            block_idx = 0

            for block in blocks_data:
                block_type = block.get("type", -1)
                if block_type != 0:  # Skip images
                    continue

                lines = block.get("lines", [])
                block_lines = []
                block_spans = []

                for line in lines:
                    line_spans = []

                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if not text:
                            continue

                        bbox = tuple(span.get("bbox", [0, 0, 0, 0]))
                        font = span.get("font", "")
                        font_size = span.get("size", 0)
                        flags = span.get("flags", 0)

                        total_chars += len(text)

                        s = SpanInfo(
                            text=text,
                            bbox=bbox,
                            font=font,
                            font_size=font_size,
                            flags=flags,
                            page_number=page_num + 1,
                            page_width=page_width,
                            page_height=page_height,
                            block_no=block_idx,
                            line_no=len(block_lines),
                        )
                        line_spans.append(s)
                        block_spans.append(s)

                    if line_spans:
                        block_lines.append(line_spans)

                if block_lines:
                    page_info.blocks.append({
                        "block_no": block_idx,
                        "lines": [[s.to_dict() for s in line] for line in block_lines],
                    })
                    page_info.lines.extend(block_lines)
                    page_info.spans.extend(block_spans)
                    block_idx += 1

            # Build full text
            page_info.full_text = " ".join(s.text for s in page_info.spans)
            page_info.has_text_layer = total_chars > 20
            result.pages.append(page_info)

        doc.close()

    except Exception as exc:
        result.parse_errors.append(f"PyMuPDF parse error: {exc}")

    return result


def parse_pdf_pdfplumber(filepath: str) -> PDFParseResult:
    """Parse PDF using pdfplumber - fallback method."""
    result = PDFParseResult(filename=os.path.basename(filepath))

    try:
        with pdfplumber.open(filepath) as pdf:
            result.page_count = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages):
                page_width = page.width or 612
                page_height = page.height or 792

                page_info = PageInfo(
                    page_number=page_num + 1,
                    page_width=page_width,
                    page_height=page_height,
                )
                page_info.extraction_method = "PDFPLUMBER_TEXT"

                # Extract words with coordinates
                words = page.extract_words(keep_blank_chars=True)

                if words and len(words) > 5:
                    page_info.has_text_layer = True

                block_idx = 0
                for word in words:
                    text = str(word.get("text", "")).strip()
                    if not text:
                        continue

                    x0 = float(word.get("x0", 0))
                    x1 = float(word.get("x1", 0))
                    top = float(word.get("top", 0))
                    bottom = float(word.get("bottom", 0))
                    bbox = (x0, top, x1, bottom)

                    s = SpanInfo(
                        text=text,
                        bbox=bbox,
                        font="",
                        font_size=0,
                        page_number=page_num + 1,
                        page_width=page_width,
                        page_height=page_height,
                        block_no=block_idx,
                    )
                    page_info.spans.append(s)
                    block_idx += 1

                page_info.full_text = page.extract_text() or ""
                result.pages.append(page_info)

    except Exception as exc:
        result.parse_errors.append(f"pdfplumber parse error: {exc}")

    return result


def parse_pdf(filepath: str, force_ocr: bool = False) -> PDFParseResult:
    """Parse a PDF file, extracting text and coordinates.

    Priority:
    1. PyMuPDF (fitz) structured extraction
    2. pdfplumber as fallback
    3. OCR only if no text layer detected

    Returns a PDFParseResult.
    """
    if not os.path.exists(filepath):
        result = PDFParseResult(filename=os.path.basename(filepath))
        result.parse_errors.append("File not found")
        return result

    if HAS_FITZ and not force_ocr:
        return parse_pdf_fitz(filepath)

    if HAS_PDFPLUMBER:
        return parse_pdf_pdfplumber(filepath)

    result = PDFParseResult(filename=os.path.basename(filepath))
    result.parse_errors.append("No PDF parser available. Install PyMuPDF: pip install PyMuPDF")
    return result


def search_text(spans: List[SpanInfo], pattern: str, case_sensitive: bool = False,
                 regex: bool = False, normalize: bool = True) -> List[SpanInfo]:
    """Search through extracted spans for a text pattern.

    Args:
        spans: List of extracted SpanInfo objects
        pattern: Search pattern
        case_sensitive: Whether to match case
        regex: Whether pattern is a regex
        normalize: Whether to normalize whitespace before comparing

    Returns:
        Matching SpanInfo objects
    """
    results = []
    flags = 0 if case_sensitive else re.IGNORECASE

    for span in spans:
        text = span.text
        if normalize:
            text = re.sub(r"\s+", " ", text).strip()

        if regex:
            if re.search(pattern, text, flags):
                results.append(span)
        else:
            search_text_val = re.sub(r"\s+", " ", pattern).strip() if normalize else pattern
            if case_sensitive:
                if search_text_val in text:
                    results.append(span)
            else:
                if search_text_val.lower() in text.lower():
                    results.append(span)

    return results


def search_text_on_pages(pages: List[PageInfo], pattern: str, case_sensitive: bool = False,
                          regex: bool = False) -> List[SpanInfo]:
    """Search for a pattern across all pages. Returns matching spans."""
    results = []
    for page in pages:
        matches = search_text(page.spans, pattern, case_sensitive, regex)
        for m in matches:
            results.append(m)
    return results


def find_nearest_context(spans: List[SpanInfo], target_span: SpanInfo,
                          max_distance: float = 200, max_lines: int = 5) -> str:
    """Find context text around a target span (lines below the target)."""
    context = []
    for span in spans:
        if span.page_number != target_span.page_number:
            continue
        # Look for spans below the target (within reasonable distance)
        if span.bbox[1] >= target_span.bbox[1] and span.block_no >= target_span.block_no:
            y_diff = span.bbox[1] - target_span.bbox[3]
            if 0 <= y_diff < max_distance and span.text.strip():
                context.append(span.text.strip())
                if len(context) >= max_lines:
                    break
    return " ".join(context)


def save_parse_result(parse_result: PDFParseResult, filepath: str) -> str:
    """Save parse result as JSON alongside the PDF. Returns JSON file path."""
    json_path = filepath + ".spans.json"
    try:
        data = {
            "filename": parse_result.filename,
            "page_count": parse_result.page_count,
            "has_text_layer": parse_result.has_text_layer,
            "pages": [],
        }
        for page in parse_result.pages:
            page_data = {
                "page_number": page.page_number,
                "page_width": page.page_width,
                "page_height": page.page_height,
                "full_text": page.full_text,
                "spans": [s.to_dict() for s in page.spans],
            }
            data["pages"].append(page_data)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
    except Exception:
        pass
    return json_path


def load_parse_result(filepath: str) -> Optional[PDFParseResult]:
    """Load parse result from JSON file. Returns None if not found."""
    json_path = filepath + ".spans.json"
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None

    result = PDFParseResult(
        filename=data.get("filename", ""),
        page_count=data.get("page_count", 0),
    )
    result.has_text_layer = data.get("has_text_layer", False)
    for page_data in data.get("pages", []):
        page = PageInfo(
            page_number=page_data["page_number"],
            page_width=page_data.get("page_width", 612),
            page_height=page_data.get("page_height", 792),
        )
        page.full_text = page_data.get("full_text", "")
        page.has_text_layer = True
        page.extraction_method = "CACHED"
        for sd in page_data.get("spans", []):
            s = SpanInfo(
                text=sd.get("text", ""),
                bbox=tuple(sd.get("bbox", [0, 0, 0, 0])),
                font=sd.get("font", ""),
                font_size=sd.get("font_size", 0),
                flags=sd.get("flags", 0),
                page_number=sd.get("page_number", page.page_number),
                page_width=sd.get("page_width", page.page_width),
                page_height=sd.get("page_height", page.page_height),
                block_no=sd.get("block_no", 0),
                line_no=sd.get("line_no", 0),
            )
            page.spans.append(s)
        result.pages.append(page)
    return result


def find_checkbox_regions(spans: List[SpanInfo], page_width: float,
                          page_height: float) -> List[Dict[str, Any]]:
    """Detect checkbox-like regions from PDF form annotations and text.

    Returns list of checkbox evidence dicts.
    """
    results = []
    checkbox_symbols = {"☑", "☒", "✓", "✔", "√", "■", "●", "◉", "○", "□", "☐",
                       "yes", "no", "是", "否", "[x]", "[X]", "[✓]", "[√]", "[ ]",
                       "(x)", "(X)", "(✓)", "(√)", "( )"}

    for span in spans:
        text = span.text.strip()
        if text in checkbox_symbols or any(cs in text for cs in checkbox_symbols):
            # Check nearby text for context
            nearby_text = []
            for other in spans:
                if other.page_number != span.page_number:
                    continue
                # Check if other span is horizontally aligned and close
                y_dist = abs(other.bbox[1] - span.bbox[1])
                x_dist = other.bbox[0] - span.bbox[2]
                if y_dist < 20 and 0 < x_dist < 200:
                    nearby_text.append(other.text)

            results.append({
                "option_text": " ".join(nearby_text),
                "selected": text not in {"□", "☐", "[ ]", "( )", "no", "否"},
                "confidence": 0.90,
                "page_number": span.page_number,
                "bbox": span.normalized_bbox,
                "method": "symbol_checkbox",
                "raw_text": text,
                "context": " ".join(nearby_text),
            })

    return results


def detect_checkbox_from_text(page_text: str) -> List[Dict[str, Any]]:
    """Detect checkbox states from OCR/text patterns in page text.

    Priority for Incoterm checkbox detection.

    IMPORTANT: In Chinese ABB contracts, the checkbox UI shows BOTH options
    (e.g. "到货价" and "出厂价") as text, but only ONE is actually selected.
    OCR will extract both strings. We MUST NOT mark both as selected.
    Instead, we look for visual checkbox indicators (☑, ☒, ✓, ■, ●) or
    explicit selection markers near the option text.

    When no clear visual checkbox is found, we return a SINGLE best-guess
    result based on heuristics, not both options as selected.
    """
    results = []

    # Step 1: Look for explicit checkbox symbols NEAR incoterm text
    # This is more reliable than just finding the option text itself
    checkbox_near_ddp = False
    checkbox_near_exw = False
    ddp_match_text = ""
    exw_match_text = ""

    # Checkbox symbol patterns (selected state)
    selected_markers = r'[☑☒✓✔√■●◉►]'
    # Checkbox symbol patterns (unselected state)
    unselected_markers = r'[□☐○]'

    # DDP patterns with checkbox proximity
    ddp_option_lines = re.split(r'[\n\r]+', page_text)
    for line in ddp_option_lines:
        line_lower = line.lower()
        # Check if this line contains DDP-related text
        is_ddp_line = any(re.search(p, line, re.I) for p in [
            r"买方工厂的到货价", r"到货价", r"ddp", r"买方.*到货"
        ])
        is_exw_line = any(re.search(p, line, re.I) for p in [
            r"卖方工厂出厂价", r"出厂价", r"exw", r"工厂交货", r"客户自提"
        ])

        has_selected = re.search(selected_markers, line)
        has_unselected = re.search(unselected_markers, line)

        if is_ddp_line:
            ddp_match_text = line.strip()
            if has_selected and not has_unselected:
                checkbox_near_ddp = True
            elif has_unselected:
                checkbox_near_ddp = False  # explicitly unselected

        if is_exw_line:
            exw_match_text = line.strip()
            if has_selected and not has_unselected:
                checkbox_near_exw = True
            elif has_unselected:
                checkbox_near_exw = False  # explicitly unselected

    # Step 2: Decision logic - only ONE can be selected
    if checkbox_near_ddp and not checkbox_near_exw:
        results.append({
            "option_text": "DDP (买方工厂的到货价)",
            "selected": True,
            "confidence": 0.92,
            "match": ddp_match_text or "DDP",
            "method": "checkbox_proximity",
        })
    elif checkbox_near_exw and not checkbox_near_ddp:
        results.append({
            "option_text": "EXW (卖方工厂出厂价)",
            "selected": True,
            "confidence": 0.92,
            "match": exw_match_text or "EXW",
            "method": "checkbox_proximity",
        })
    elif checkbox_near_ddp and checkbox_near_exw:
        # Both have selected markers - unusual, log with lower confidence
        # Prefer DDP as it's more common in Chinese domestic contracts
        results.append({
            "option_text": "DDP (买方工厂的到货价)",
            "selected": True,
            "confidence": 0.55,
            "match": ddp_match_text or "DDP",
            "method": "checkbox_ambiguous_fallback_ddp",
            "note": "Both DDP and EXW have checkbox markers; defaulting to DDP",
        })
    else:
        # Step 3: No clear checkbox found - use structural heuristics
        # In Chinese ABB contracts, the incoterm section typically has:
        # - A transport clause section with checkbox options
        # - "交货地点" (delivery location) field
        # - If delivery location is FILLED, it's likely DDP
        # - If delivery location is EMPTY/BLANK, it's likely EXW

        has_ddp_text = any(re.search(p, page_text, re.I) for p in [
            r"买方工厂的到货价", r"到货价"
        ])
        has_exw_text = any(re.search(p, page_text, re.I) for p in [
            r"卖方工厂出厂价", r"出厂价"
        ])

        # Look for delivery location to help disambiguate
        delivery_location_filled = False
        dl_match = re.search(r'(?:交付地点|交货地点|Delivery\s*Location)[\s:：]*(\S{2,})', page_text, re.I)
        if dl_match and len(dl_match.group(1).strip()) > 1:
            delivery_location_filled = True

        if has_ddp_text and not has_exw_text:
            results.append({
                "option_text": "DDP (买方工厂的到货价)",
                "selected": True,
                "confidence": 0.75,
                "match": "DDP text found",
                "method": "text_only_ddp",
            })
        elif has_exw_text and not has_ddp_text:
            results.append({
                "option_text": "EXW (卖方工厂出厂价)",
                "selected": True,
                "confidence": 0.75,
                "match": "EXW text found",
                "method": "text_only_exw",
            })
        elif has_ddp_text and has_exw_text:
            # Both texts present but no checkbox - this is the normal template
            # Use delivery location as tiebreaker
            if delivery_location_filled:
                results.append({
                    "option_text": "DDP (买方工厂的到货价)",
                    "selected": True,
                    "confidence": 0.65,
                    "match": "DDP inferred from filled delivery location",
                    "method": "template_with_location",
                })
            else:
                results.append({
                    "option_text": "EXW (卖方工厂出厂价)",
                    "selected": True,
                    "confidence": 0.60,
                    "match": "EXW inferred from empty delivery location",
                    "method": "template_without_location",
                })

    return results


def extract_tables_from_page(filepath: str, page_number: int) -> List[List[List[str]]]:
    """Extract tables from a specific PDF page.

    Uses pdfplumber for table extraction.
    """
    tables = []
    if not HAS_PDFPLUMBER:
        return tables

    try:
        with pdfplumber.open(filepath) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                return tables
            page = pdf.pages[page_number - 1]
            extracted = page.extract_tables()
            if extracted:
                tables = extracted
    except Exception:
        pass

    return tables


def get_page_image_region(filepath: str, page_number: int,
                          bbox: Tuple[float, ...]) -> Optional[bytes]:
    """Extract a region from a PDF page as an image (for OCR fallback).

    Returns PNG bytes or None.
    """
    if not HAS_FITZ and not HAS_OCR:
        return None

    try:
        if HAS_FITZ:
            doc = fitz.open(filepath)
            if page_number < 1 or page_number > doc.page_count:
                doc.close()
                return None
            page = doc[page_number - 1]
            clip = fitz.Rect(*bbox)
            pix = page.get_pixmap(clip=clip, dpi=200)
            img_bytes = pix.tobytes("png")
            doc.close()
            return img_bytes
    except Exception:
        pass

    return None


def ocr_page_region(filepath: str, page_number: int,
                    bbox: Tuple[float, ...]) -> Optional[str]:
    """Run OCR on a specific region of a PDF page."""
    if not HAS_OCR:
        return None

    img_bytes = get_page_image_region(filepath, page_number, bbox)
    if not img_bytes:
        return None

    try:
        image = Image.open(io.BytesIO(img_bytes))
        text = pytesseract.image_to_string(image, lang="chi_sim+eng")
        return text.strip()
    except Exception:
        return None