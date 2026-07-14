# -*- coding: utf-8 -*-
"""Document Check orchestration service.

Coordinates:
1. File ingestion & storage
2. PDF parsing with text+coordinate extraction
3. Document classification (Contract/CQP/TA)
4. Embedded TA detection
5. Structured field extraction
6. Rule engine execution
7. Result aggregation
8. BT09 generation
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from . import document_check_db as dcdb
from . import normalizer as norm
from . import pdf_parser
from .document_check_rules import (
    CheckResult, ExtractedData, run_all_rules, compute_overall_conclusion
)

# Storage paths
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
ATTACHMENT_ROOT = os.path.join(DATA_DIR, "attachments")
DC_STORAGE = os.path.join(ATTACHMENT_ROOT, "document_check")

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_MIMETYPES = {"application/pdf", "application/octet-stream"}
ALLOWED_EXTENSIONS = {".pdf"}

# Processing status steps
PROCESSING_STAGES = [
    "Uploading",
    "Parsing",
    "Extracting",
    "Comparing",
    "Generating result",
    "Completed",
]

# Fields to extract from documents
CONTRACT_FIELDS = [
    "contract_number", "seller_legal_entity", "buyer_legal_name", "buyer_address",
    "end_customer_name", "end_customer_address", "installation_site",
    "ship_to_name", "ship_to_address", "ship_to_id", "end_customer_id",
    "sales_person", "pm", "robot_models", "total_quantity",
    "incoterm", "delivery_location", "delivery_time",
    "payment_terms", "warranty_period", "warranty_classification",
    "vat_rate", "untaxed_amount", "tax_amount", "tax_included_amount",
    "technical_config", "gis_number",
]

CQP_FIELDS = [
    "contract_number", "cqp_number", "cqp_version",
    "buyer_legal_name", "end_customer_name",
    "robot_models", "total_quantity", "quantity_by_model",
    "incoterm", "delivery_time", "warranty_period",
    "untaxed_amount", "tax_included_amount", "vat_rate",
    "technical_config",
]

TA_FIELDS = [
    "robot_models", "quantity_by_model", "technical_config",
    "warranty_period", "warranty_classification",
]


def _sha256_file(filepath: str) -> str:
    """Compute SHA-256 hash of file."""
    digest = hashlib.sha256()
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secure_filename(filename: str) -> str:
    """Generate a secure stored filename."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    return f"{timestamp}_{safe_name}"


def _save_upload(file_content: bytes, original_filename: str) -> Dict[str, Any]:
    """Save uploaded file securely. Returns metadata dict."""
    os.makedirs(DC_STORAGE, exist_ok=True)
    stored_name = _secure_filename(original_filename)
    filepath = os.path.join(DC_STORAGE, stored_name)

    with open(filepath, "wb") as fh:
        fh.write(file_content)

    sha256 = _sha256_file(filepath)
    file_size = os.path.getsize(filepath)

    return {
        "original_filename": original_filename,
        "stored_filename": stored_name,
        "filepath": filepath,
        "sha256": sha256,
        "file_size": file_size,
    }


def _classify_document(filepath: str, parse_result=None) -> str:
    """Classify document type based on filename and content analysis.

    Returns: CONTRACT, CQP, TA, CUSTOMER_MASTER, TEMPLATE, or OTHER
    """
    filename = os.path.basename(filepath).lower()

    # Filename-based heuristics
    if "cqp" in filename and "contract" not in filename:
        return "CQP"
    if "销售合同" in filename or "contract" in filename:
        return "CONTRACT"
    if "ta" in filename or "技术协议" in filename or "technical agreement" in filename:
        return "TA"

    # Content-based heuristics
    if parse_result:
        full_text = " ".join(p.full_text for p in parse_result.pages).lower()
        # CQP has specific formatting with pricing tables
        cqp_markers = ["cqp", "quotation", "报价", "含税金额", "不含税金额", "设备名称"]
        contract_markers = ["合同编号", "卖方", "买方", "合同", "条款", "clause"]
        ta_markers = ["供货范围", "技术协议", "配置代码", "technical agreement",
                      "供货范围及技术协议", "单台配置"]

        cqp_score = sum(1 for m in cqp_markers if m in full_text)
        contract_score = sum(1 for m in contract_markers if m in full_text)
        ta_score = sum(1 for m in ta_markers if m in full_text)

        if cqp_score > contract_score and cqp_score > ta_score:
            return "CQP"
        if ta_score > contract_score and ta_score > cqp_score:
            if contract_score > 0:
                return "CONTRACT"  # TA embedded in contract
            return "TA"
        if contract_score > 0:
            return "CONTRACT"

    return "OTHER"


def _detect_embedded_ta(parse_result) -> Optional[tuple]:
    """Detect if TA is embedded within a Contract PDF.
    Returns (start_page, end_page) or None.
    """
    if not parse_result or parse_result.page_count < 3:
        return None

    ta_markers = [
        "供货范围及技术协议",
        "技术协议",
        "Technical Agreement",
        "附件一",
        "供货范围",
        "单台配置如下",
        "配置代码",
        "机器人型号",
    ]

    ta_start = None
    ta_end = None

    for page in parse_result.pages:
        text = page.full_text.lower()
        # Check if this page has TA markers
        ta_hits = sum(1 for m in ta_markers if m.lower() in text)

        if ta_hits >= 2 or ("技术协议" in text and "供货" in text):
            if ta_start is None:
                ta_start = page.page_number
            ta_end = page.page_number

        # Detect "Page 1 of X" pattern suggesting new section
        if re.search(r'page\s*1\s*(?:of|/)\s*\d+', text, re.I):
            if ta_start is not None:
                # If we already found TA start, this could be partial TA
                pass

    if ta_start is not None:
        # If TA starts but ends near end of document, assume TA goes to end
        if ta_end is None or ta_end >= parse_result.page_count - 1:
            ta_end = parse_result.page_count
        return (ta_start, ta_end)

    return None


def _find_span_for_value(parse_result, search_text: str, value: str):
    """Search all spans for a matched value and return (page_number, bbox) or None."""
    if not parse_result or not value:
        return None
    # Try exact match of the value in spans
    for page in parse_result.pages:
        for span in page.spans:
            if value.strip() in span.text or span.text.strip() in value.strip():
                return (page.page_number, span.normalized_bbox)
    # Fallback: search for the raw search_text
    if search_text and search_text != value:
        for page in parse_result.pages:
            for span in page.spans:
                if search_text.strip() in span.text:
                    return (page.page_number, span.normalized_bbox)
    return None


def _find_spans_for_regex(parse_result, pattern: str) -> List[tuple]:
    """Find all spans matching a regex. Returns [(page_number, normalized_bbox, text), ...]."""
    results = []
    if not parse_result:
        return results
    for page in parse_result.pages:
        for span in page.spans:
            m = re.search(pattern, span.text, re.I)
            if m:
                results.append((page.page_number, span.normalized_bbox, m.group()))
    return results


def _find_spans_for_label(parse_result, labels: List[str]) -> List[tuple]:
    """Find spans that contain a label keyword. Returns [(page_number, normalized_bbox, text), ...]."""
    results = []
    if not parse_result:
        return results
    for page in parse_result.pages:
        for span in page.spans:
            for label in labels:
                if label.lower() in span.text.lower():
                    # Return this span and collect nearby spans as context
                    context = _collect_nearby_spans(page, span, max_distance=300, max_spans=10)
                    combined = span.text + " " + " ".join(context)
                    results.append((page.page_number, span.normalized_bbox, combined))
                    break
    return results


def _collect_nearby_spans(page, target_span, max_distance=300, max_spans=15):
    """Collect text from spans near the target span (below/right)."""
    texts = []
    for span in page.spans:
        if span is target_span:
            continue
        # Spans on the same line or below, within horizontal proximity
        y_diff = span.bbox[1] - target_span.bbox[1]
        x_diff = abs(span.bbox[0] - target_span.bbox[0])
        if 0 <= y_diff < max_distance and x_diff < max_distance:
            if span.text.strip():
                texts.append(span.text.strip())
                if len(texts) >= max_spans:
                    break
    return texts


def _extract_fields_from_document(filepath: str, parse_result, doc_type: str,
                                  case_id: str, doc_id: str) -> List[Dict[str, Any]]:
    """Extract structured fields from a parsed document with span coordinates.

    Uses regex and pattern matching on span text, recording actual page/bbox for each
    found value so the frontend can draw highlight boxes on the PDF.
    """
    if not parse_result:
        return []

    full_text = "\n".join(p.full_text for p in parse_result.pages)
    fields = []

    # ------------------------------------------------------------
    # Helper to create evidence + field, using span coords
    # ------------------------------------------------------------
    def _store_field(field_name: str, value: str, page_number: int = 1,
                     bbox: dict = None, confidence: float = 0.75):
        nonlocal fields
        if bbox is None:
            bbox = {"x": 0, "y": 0, "width": 0, "height": 0}
        ev_id = dcdb.add_evidence(
            case_id=case_id,
            document_id=doc_id,
            page_number=page_number,
            bbox=bbox,
            raw_text=value,
            normalized_text=value,
            extraction_method="PDF_TEXT",
            confidence=confidence,
            document_type=doc_type,
        )
        dcdb.upsert_extracted_field(
            case_id=case_id,
            document_id=doc_id,
            field_name=field_name,
            value=value,
            normalized_value=value,
            confidence=confidence,
            evidence_refs=[ev_id],
        )
        fields.append({"field_name": field_name, "value": value, "doc_type": doc_type})

    # ------------------------------------------------------------
    # Contract field patterns with enhanced Chinese support
    # ------------------------------------------------------------
    if doc_type == "CONTRACT":
        # --- Contract Number ---
        cn_pattern = r'(?:合同编号|Contract\s*No\.?|Contract\s*Number)[\s:：]*([A-Z]{2}\d{2,}|[MK]\d{4}[-—]\d{4}|[A-Z]+[-]\d+)'
        for page in parse_result.pages:
            m = re.search(cn_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("contract_number", val, loc[0], loc[1], 0.90)
                else:
                    _store_field("contract_number", val, page.page_number, confidence=0.85)
                break
        else:
            # Fallback: whole text search
            m = re.search(cn_pattern, full_text, re.I)
            if m:
                val = m.group(1).strip()
                _store_field("contract_number", val, confidence=0.80)

        # --- Seller Legal Entity ---
        seller_labels = ["卖方", "Seller", "供方", "甲方"]
        seller_spans = _find_spans_for_label(parse_result, seller_labels)
        if seller_spans:
            pg, bb, ctx = seller_spans[0]
            # Extract company name from context (usually the pre-formatted name in the contract header)
            name_match = re.search(r'(?:ABB|上海ABB|珠海ABB)[^\n]{4,40}', ctx, re.I)
            if name_match:
                _store_field("seller_legal_entity", name_match.group().strip(), pg, bb, 0.85)
            else:
                _store_field("seller_legal_entity", ctx[:80], pg, bb, 0.60)

        # --- Buyer Legal Name ---
        buyer_labels = ["买方", "Buyer", "需方", "乙方", "买受人"]
        buyer_spans = _find_spans_for_label(parse_result, buyer_labels)
        if buyer_spans:
            pg, bb, ctx = buyer_spans[0]
            # Try to extract the buyer name following the label
            name_match = re.search(r'(?:买方|Buyer)[\s:：]*(.{2,60})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("buyer_legal_name", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("buyer_legal_name", ctx[:80], pg, bb, 0.55)

        # --- End Customer ---
        ec_labels = ["最终用户", "End Customer", "End User", "终端用户", "最终使用方"]
        ec_spans = _find_spans_for_label(parse_result, ec_labels)
        if ec_spans:
            pg, bb, ctx = ec_spans[0]
            name_match = re.search(r'(?:最终用户|End\s*Customer|End\s*User)[\s:：]*(.{2,60})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("end_customer_name", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("end_customer_name", ctx[:80], pg, bb, 0.50)

        # --- Delivery Location ---
        dl_labels = ["交付地点", "交货地点", "Delivery Location", "交货地址", "到货地点"]
        dl_spans = _find_spans_for_label(parse_result, dl_labels)
        if dl_spans:
            pg, bb, ctx = dl_spans[0]
            name_match = re.search(r'(?:交付地点|交货地点|Delivery\s*Location)[\s:：]*(.{2,80})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("delivery_location", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("delivery_location", ctx[:80], pg, bb, 0.55)

        # --- Warranty Period (Clause 5.2) ---
        # Priority 1: Look for "5.2" or "第5.2条" clause specifically
        wp_found = False
        for page in parse_result.pages:
            if re.search(r'(?:5\.2|第\s*5\s*\.\s*2\s*条|质保|Warranty|保修)', page.full_text, re.I):
                # Try to extract the specific 18/12 or 24/12 pattern near clause 5.2
                m = re.search(r'(\d{1,2})\s*[/／]\s*(\d{1,2})\s*(?:个?\s*月|months?)',
                              page.full_text, re.I)
                if m:
                    val = f"{m.group(1)}/{m.group(2)}"
                    loc = _find_span_for_value(parse_result, m.group(0), val)
                    if loc:
                        _store_field("warranty_period", val, loc[0], loc[1], 0.92)
                    else:
                        _store_field("warranty_period", val, page.page_number, confidence=0.88)
                    wp_found = True
                    break
                # Also try generic warranty pattern
                m2 = re.search(r'(?:质保期|保修期|Warranty\s*Period|质量保证)[\s:：]*(\d{1,2}\s*[/／]\s*\d{1,2})',
                               page.full_text, re.I)
                if m2:
                    val = m2.group(1).strip()
                    loc = _find_span_for_value(parse_result, m2.group(0), val)
                    if loc:
                        _store_field("warranty_period", val, loc[0], loc[1], 0.90)
                    else:
                        _store_field("warranty_period", val, page.page_number, confidence=0.85)
                    wp_found = True
                    break

        if not wp_found:
            # Fallback: search entire document
            wp_pattern = r'(?:质保期|保修期|Warranty\s*Period|质量保证)[\s:：]*(\d{1,2}\s*[/／]\s*\d{1,2})'
            m = re.search(wp_pattern, full_text, re.I)
            if m:
                val = m.group(1).strip()
                _store_field("warranty_period", val, 1, confidence=0.75)

        # --- Incoterm ---
        ic_pattern = r'(?:贸易术语|Incoterm|Trade\s*Term|价格术语)[\s:：]*([^\n]{3,40})'
        for page in parse_result.pages:
            m = re.search(ic_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("incoterm", val, loc[0], loc[1], 0.85)
                else:
                    _store_field("incoterm", val, page.page_number, confidence=0.80)
                break

        # --- Ship-to Name ---
        st_labels = ["收货方", "Ship-to", "Ship to", "收货人", "收货单位"]
        st_spans = _find_spans_for_label(parse_result, st_labels)
        if st_spans:
            pg, bb, ctx = st_spans[0]
            name_match = re.search(r'(?:收货方|Ship-to|Ship\s*to|收货人)[\s:：]*(.{2,60})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("ship_to_name", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("ship_to_name", ctx[:80], pg, bb, 0.50)

        # --- Ship-to Address ---
        sta_labels = ["收货地址", "Ship-to Address", "Ship to Address", "交货地址"]
        sta_spans = _find_spans_for_label(parse_result, sta_labels)
        if sta_spans:
            pg, bb, ctx = sta_spans[0]
            name_match = re.search(r'(?:收货地址|Ship-to\s*Address|Ship\s*to\s*Address)[\s:：]*(.{2,80})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("ship_to_address", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("ship_to_address", ctx[:80], pg, bb, 0.50)

        # --- Ship-to ID ---
        stid_pattern = r'(?:Ship-to\s*ID|Ship\s*to\s*ID|收货方\s*ID)[\s:：]*(\d{4,})'
        for page in parse_result.pages:
            m = re.search(stid_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("ship_to_id", val, loc[0], loc[1], 0.90)
                else:
                    _store_field("ship_to_id", val, page.page_number, confidence=0.85)
                break

        # --- End Customer ID ---
        ecid_pattern = r'(?:End\s*Customer\s*ID|End\s*Cust\s*ID|最终用户\s*ID)[\s:：]*(\d{4,})'
        for page in parse_result.pages:
            m = re.search(ecid_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("end_customer_id", val, loc[0], loc[1], 0.90)
                else:
                    _store_field("end_customer_id", val, page.page_number, confidence=0.85)
                break

        # --- GIS Number ---
        gis_pattern = r'(?:GIS|GIS\s*Number|GIS\s*编号)[\s:：]*(\d{4,})'
        for page in parse_result.pages:
            m = re.search(gis_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("gis_number", val, loc[0], loc[1], 0.90)
                else:
                    _store_field("gis_number", val, page.page_number, confidence=0.85)
                break

        # --- Buyer Address ---
        ba_labels = ["买方地址", "Buyer Address", "买方所在地"]
        ba_spans = _find_spans_for_label(parse_result, ba_labels)
        if ba_spans:
            pg, bb, ctx = ba_spans[0]
            name_match = re.search(r'(?:买方地址|Buyer\s*Address)[\s:：]*(.{2,80})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("buyer_address", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("buyer_address", ctx[:80], pg, bb, 0.50)

        # --- Installation Site ---
        is_labels = ["安装地点", "Installation Site", "使用地点"]
        is_spans = _find_spans_for_label(parse_result, is_labels)
        if is_spans:
            pg, bb, ctx = is_spans[0]
            name_match = re.search(r'(?:安装地点|Installation\s*Site)[\s:：]*(.{2,80})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("installation_site", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("installation_site", ctx[:80], pg, bb, 0.50)

    # ------------------------------------------------------------
    # CQP field patterns
    # ------------------------------------------------------------
    elif doc_type == "CQP":
        # CQP Number
        cqp_pattern = r'(?:CQ\d{5,})'
        for page in parse_result.pages:
            m = re.search(cqp_pattern, page.full_text, re.I)
            if m:
                val = m.group(0).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("cqp_number", val, loc[0], loc[1], 0.90)
                else:
                    _store_field("cqp_number", val, page.page_number, confidence=0.85)
                break

        # CQP Version
        cqp_ver_pattern = r'(?:CQP\s*版本|CQP\s*Version|版本号|Version\s*No|Rev\.?|修订)[\s:：]*([A-Za-z0-9\.\-]+)'
        for page in parse_result.pages:
            m = re.search(cqp_ver_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("cqp_version", val, loc[0], loc[1], 0.85)
                else:
                    _store_field("cqp_version", val, page.page_number, confidence=0.75)
                break

        # Contract Number reference in CQP
        cn_pattern = r'(?:Contract\s*No\.?|合同编号|Contract\s*Number)[\s:：]*([A-Z]{2}\d{2,}|[MK]\d{4}[-—]\d{4}|[A-Z]+[-]\d+)'
        for page in parse_result.pages:
            m = re.search(cn_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                if loc:
                    _store_field("contract_number", val, loc[0], loc[1], 0.90)
                else:
                    _store_field("contract_number", val, page.page_number, confidence=0.85)
                break

        # Buyer in CQP
        buyer_labels = ["买方", "Buyer", "客户名称", "Customer Name"]
        buyer_spans = _find_spans_for_label(parse_result, buyer_labels)
        if buyer_spans:
            pg, bb, ctx = buyer_spans[0]
            name_match = re.search(r'(?:买方|Buyer|客户名称|Customer\s*Name)[\s:：]*(.{2,60})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("buyer_legal_name", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("buyer_legal_name", ctx[:80], pg, bb, 0.55)

        # End Customer in CQP
        ec_labels = ["最终用户", "End Customer", "End User"]
        ec_spans = _find_spans_for_label(parse_result, ec_labels)
        if ec_spans:
            pg, bb, ctx = ec_spans[0]
            name_match = re.search(r'(?:最终用户|End\s*Customer|End\s*User)[\s:：]*(.{2,60})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("end_customer_name", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("end_customer_name", ctx[:80], pg, bb, 0.50)

    # ------------------------------------------------------------
    # TA field patterns
    # ------------------------------------------------------------
    elif doc_type == "TA":
        pass  # TA fields handled by general extraction below

    # ------------------------------------------------------------
    # Financial extraction (CONTRACT & CQP)
    # ------------------------------------------------------------
    if doc_type in ("CONTRACT", "CQP"):
        # --- Untaxed Amount ---
        untaxed_pattern = r'(?:不含税金额|未税金额|Untaxed|不含税总价|未税总价)[\s:：]*RMB\s*([\d,]+\.?\d*)'
        for page in parse_result.pages:
            m = re.search(untaxed_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                pg = loc[0] if loc else page.page_number
                bb = loc[1] if loc else None
                _store_field("untaxed_amount", val, pg, bb, 0.85)
                break
        else:
            # Fallback: broader search
            fallback = re.search(r'(?:不含税|未税|Untaxed).*?(?:RMB|CNY|￥)?\s*([\d,]+\.?\d*)', full_text, re.I)
            if fallback:
                _store_field("untaxed_amount", fallback.group(1).strip(), confidence=0.70)

        # --- Tax-included Amount ---
        total_pattern = r'(?:含税金额|价税合计|Total\s*Price|含税总价|合同总价)[\s:：]*RMB\s*([\d,]+\.?\d*)'
        for page in parse_result.pages:
            m = re.search(total_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                pg = loc[0] if loc else page.page_number
                bb = loc[1] if loc else None
                _store_field("tax_included_amount", val, pg, bb, 0.85)
                break
        else:
            fallback = re.search(r'(?:含税|价税合计|Total).*?(?:RMB|CNY|￥)?\s*([\d,]+\.?\d*)', full_text, re.I)
            if fallback:
                _store_field("tax_included_amount", fallback.group(1).strip(), confidence=0.70)

        # --- VAT Rate ---
        vat_patterns = [
            r'(?:增值税税率|VAT\s*Rate|增值税)[\s:：]*(\d{1,2})\s*%',
            r'(?:税率)[\s:：]*(\d{1,2})\s*%',
        ]
        for vp in vat_patterns:
            for page in parse_result.pages:
                m = re.search(vp, page.full_text, re.I)
                if m:
                    val = m.group(1).strip()
                    loc = _find_span_for_value(parse_result, m.group(0), val)
                    pg = loc[0] if loc else page.page_number
                    bb = loc[1] if loc else None
                    _store_field("vat_rate", f"{val}%", pg, bb, 0.90)
                    break
            else:
                continue
            break

        # --- Tax Amount ---
        tax_pattern = r'(?:税额|税金|Tax\s*Amount|增值税额)[\s:：]*RMB\s*([\d,]+\.?\d*)'
        for page in parse_result.pages:
            m = re.search(tax_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                pg = loc[0] if loc else page.page_number
                bb = loc[1] if loc else None
                _store_field("tax_amount", val, pg, bb, 0.85)
                break

        # --- Payment Terms ---
        # Priority: Look for Annex 2 / 附件二 specifically
        # Extract the full text of the payment terms section verbatim
        annex2_found = False
        for page_num, page in enumerate(parse_result.pages):
            if re.search(r'(?:附件二|Annex\s*2|Annex\s*II)', page.full_text, re.I):
                # Found Annex 2 - extract from this page onwards until next major section
                payment_text = page.full_text
                # Also collect next page if it seems to continue the annex
                if page_num + 1 < parse_result.page_count:
                    next_page = parse_result.pages[page_num + 1]
                    if not re.search(r'(?:附件三|Annex\s*3|第[五六七八九]条|Clause\s*\d+)', next_page.full_text, re.I):
                        payment_text += "\n" + next_page.full_text
                _store_field("payment_terms", payment_text[:2000], page.page_number, confidence=0.85)
                annex2_found = True
                break

        if not annex2_found:
            pt_labels = ["付款条件", "Payment Terms", "付款方式", "附件二", "Annex 2", "支付条款"]
            pt_spans = _find_spans_for_label(parse_result, pt_labels)
            if pt_spans:
                pg, bb, ctx = pt_spans[0]
                _store_field("payment_terms", ctx[:800], pg, bb, 0.70)
            else:
                # Search for payment-related sections
                for page in parse_result.pages:
                    if re.search(r'(?:付款|Payment|支付)', page.full_text, re.I):
                        _store_field("payment_terms", page.full_text[:800], page.page_number, confidence=0.50)
                        break

        # --- Sales Person & PM ---
        # Look for sales/PM mentions in the contract header or project info section
        sales_labels = ["销售", "Sales", "销售人员", "Sales Person", "销售代表", "Sales Rep"]
        sales_spans = _find_spans_for_label(parse_result, sales_labels)
        if sales_spans:
            pg, bb, ctx = sales_spans[0]
            # Try to extract name after label
            name_match = re.search(r'(?:销售|Sales|销售人员|Sales\s*Person|销售代表)[\s:：]*(.{2,20})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("sales_person", name_match.group(1).strip(), pg, bb, 0.80)
            else:
                _store_field("sales_person", ctx[:100], pg, bb, 0.50)

        pm_labels = ["项目经理", "PM", "Project Manager", "项目负责人"]
        pm_spans = _find_spans_for_label(parse_result, pm_labels)
        if pm_spans:
            pg, bb, ctx = pm_spans[0]
            # Try to extract name after label
            name_match = re.search(r'(?:项目经理|PM|Project\s*Manager|项目负责人)[\s:：]*(.{2,20})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("pm", name_match.group(1).strip(), pg, bb, 0.80)
            else:
                _store_field("pm", ctx[:100], pg, bb, 0.50)

        # --- Delivery Time ---
        dt_labels = ["交付时间", "Delivery Time", "交货期", "交付周期", "Delivery Schedule"]
        dt_spans = _find_spans_for_label(parse_result, dt_labels)
        if dt_spans:
            pg, bb, ctx = dt_spans[0]
            name_match = re.search(r'(?:交付时间|Delivery\s*Time|交货期)[\s:：]*(.{2,100})', ctx, re.I)
            if name_match and len(name_match.group(1).strip()) > 1:
                _store_field("delivery_time", name_match.group(1).strip(), pg, bb, 0.85)
            else:
                _store_field("delivery_time", ctx[:100], pg, bb, 0.55)

        # --- Total Quantity ---
        qty_pattern = r'(?:总数量|Total\s*Quantity|合计数量|数量合计)[\s:：]*(\d+)\s*(?:台|units|sets|pcs|套)'
        for page in parse_result.pages:
            m = re.search(qty_pattern, page.full_text, re.I)
            if m:
                val = m.group(1).strip()
                loc = _find_span_for_value(parse_result, m.group(0), val)
                pg = loc[0] if loc else page.page_number
                bb = loc[1] if loc else None
                _store_field("total_quantity", val, pg, bb, 0.90)
                break

    # ------------------------------------------------------------
    # Robot model extraction (general)
    # ------------------------------------------------------------
    robot_pattern = r'IRB\s*\d{3,4}[-\s]\d+(?:/\d+(?:\.\d+)?)?(?:\s*(?:Gen\s*)?\d+)?'
    robot_matches_set = set()
    robot_ev_refs = []
    for page in parse_result.pages:
        page_matches = re.findall(robot_pattern, page.full_text, re.I)
        for rm in page_matches:
            robot_matches_set.add(rm.strip())
            # Find the span for this robot model
            loc = _find_span_for_value(parse_result, rm, rm.strip())
            if loc:
                ev_id = dcdb.add_evidence(
                    case_id=case_id, document_id=doc_id,
                    page_number=loc[0], bbox=loc[1],
                    raw_text=rm.strip(), normalized_text=norm.normalize_robot_model(rm),
                    extraction_method="PDF_TEXT", confidence=0.90,
                    document_type=doc_type,
                )
                robot_ev_refs.append(ev_id)

    if robot_matches_set:
        unique_models = list(robot_matches_set)
        dcdb.upsert_extracted_field(case_id, doc_id, "robot_models",
                                    ", ".join(unique_models),
                                    ", ".join(norm.normalize_robot_model(m) for m in unique_models),
                                    0.85,
                                    evidence_refs=robot_ev_refs if robot_ev_refs else None)
        if not any(f["field_name"] == "robot_models" for f in fields):
            fields.append({"field_name": "robot_models", "value": ", ".join(unique_models), "doc_type": doc_type})

    return fields


def process_upload(case_id: str, file_content: bytes, original_filename: str,
                   workspace: str = "A") -> Dict[str, Any]:
    """Process a file upload: save, parse, classify.

    Returns the document record.
    """
    # Validate
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File type '{ext}' not supported. Please upload PDF files.")

    if len(file_content) > MAX_FILE_SIZE:
        raise ValueError(f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)} MB.")

    # Save file
    meta = _save_upload(file_content, original_filename)

    # Check duplicate
    existing = dcdb.find_document_by_sha256(case_id, meta["sha256"])
    if existing:
        # Clean up the duplicate file we just saved
        try:
            os.unlink(meta["filepath"])
        except Exception:
            pass
        return existing

    # Create DB record
    doc = dcdb.add_review_document(
        case_id=case_id,
        original_filename=meta["original_filename"],
        stored_filename=meta["stored_filename"],
        sha256=meta["sha256"],
        workspace=workspace,
        file_size=meta["file_size"],
    )

    # Parse PDF
    dcdb.update_review_case(case_id, {"status": "PARSING"})
    dcdb.update_review_document(doc["id"], {"parse_status": "parsing"})

    # Try to load cached parse result first, else parse fresh
    parse_result = pdf_parser.load_parse_result(meta["filepath"])
    if parse_result is None:
        parse_result = pdf_parser.parse_pdf(meta["filepath"])
        # Cache the parse result for later reuse
        pdf_parser.save_parse_result(parse_result, meta["filepath"])

    if parse_result.parse_errors:
        dcdb.update_review_document(doc["id"], {
            "parse_status": "error",
            "parse_error": "; ".join(parse_result.parse_errors),
        })
    else:
        dcdb.update_review_document(doc["id"], {
            "page_count": parse_result.page_count,
            "parse_status": "done",
        })

    # Classify document
    detected_type = _classify_document(meta["filepath"], parse_result)
    dcdb.update_review_document(doc["id"], {"detected_type": detected_type})

    # Check for embedded TA
    if detected_type == "CONTRACT":
        embedded = _detect_embedded_ta(parse_result)
        if embedded:
            dcdb.update_review_document(doc["id"], {
                "embedded_sections": [{
                    "type": "TA",
                    "start_page": embedded[0],
                    "end_page": embedded[1],
                    "detected_by": "text_pattern",
                    "confidence": 0.80,
                }]
            })

    # Extract fields with span coordinates
    _extract_fields_from_document(
        meta["filepath"], parse_result, detected_type,
        case_id, doc["id"]
    )

    # Update case status
    dcdb.update_review_case(case_id, {"status": "UPLOADED"})

    # Return updated doc
    return dcdb.get_review_document(doc["id"]) or doc


def run_review(case_id: str, progress_callback: Callable = None) -> Dict[str, Any]:
    """Run the full document check review for a case.

    This is the main orchestration function.
    """
    case = dcdb.get_review_case(case_id)
    if not case:
        raise ValueError(f"Case {case_id} not found")

    docs = dcdb.list_review_documents(case_id)
    fields = dcdb.list_extracted_fields(case_id)

    def _update_progress(stage: str):
        if progress_callback:
            progress_callback(stage)
        dcdb.update_review_case(case_id, {"status": stage.upper().replace(" ", "_")[:20]})

    _update_progress("Comparing")

    # Build ExtractedData container
    data = ExtractedData()
    contract_doc = None
    cqp_doc = None
    ta_doc = None

    # Organize fields by document type
    for doc in docs:
        dtype = doc.get("manual_type") or doc.get("detected_type") or "OTHER"
        if dtype == "CONTRACT":
            contract_doc = doc
            data.has_contract = True
            # Parse contract for spans if not already done
            # Check embedded TA
            embedded = doc.get("embedded_sections") or []
            for section in embedded:
                if section.get("type") == "TA":
                    data.ta_is_embedded = True
                    data.ta_contract_pages = (section.get("start_page", 0), section.get("end_page", 0))
        elif dtype == "CQP":
            cqp_doc = doc
            data.has_cqp = True
        elif dtype == "TA":
            ta_doc = doc
            data.has_ta = True

    # Load extracted fields into container
    for f in fields:
        doc_id = f.get("document_id", "")
        doc_type = "OTHER"
        for d in docs:
            if d["id"] == doc_id:
                doc_type = d.get("manual_type") or d.get("detected_type") or "OTHER"
                break

        field_data = {
            "value": f.get("manual_override") and json.loads(f.get("manual_override", "{}")).get("new_value") or f.get("value", ""),
            "normalized_value": f.get("normalized_value", ""),
            "confidence": f.get("confidence", 0.0),
            "evidence_refs": f.get("evidence_refs", []),
        }

        if doc_type == "CONTRACT":
            data.contract_fields[f["field_name"]] = field_data
        elif doc_type == "CQP":
            data.cqp_fields[f["field_name"]] = field_data
        elif doc_type == "TA":
            data.ta_fields[f["field_name"]] = field_data

    # Run all rules
    _update_progress("Running checks")
    results = run_all_rules(data)

    # Store check items
    _update_progress("Storing results")
    for result in results:
        dcdb.upsert_check_item(
            case_id=case_id,
            rule_id=result.rule_id,
            category=result.category,
            label=result.label,
            status=result.status,
            is_blocker=result.is_blocker,
            summary=result.summary,
            details=result.details,
            values={
                "contract": result.contract_value,
                "cqp": result.cqp_value,
                "ta": result.ta_value,
            },
            evidence_refs=result.evidence_refs,
            confidence=result.confidence,
        )

    # Compute overall conclusion
    conclusion, conclusion_desc = compute_overall_conclusion(results)

    _update_progress("Completed")

    dcdb.update_review_case(case_id, {
        "status": "COMPLETED",
        "overall_conclusion": conclusion,
    })

    check_items = dcdb.list_check_items(case_id)

    return {
        "case_id": case_id,
        "overall_conclusion": conclusion,
        "conclusion_description": conclusion_desc,
        "check_items": [item for item in check_items],
        "extracted_fields": fields,
    }


def generate_bt09_draft(case_id: str) -> Dict[str, Any]:
    """Generate BT09 email draft from review results.

    Only generates if conclusion is PASS or PASS_WITH_NOTES.

    Template selection:
    1. Determine DDP or EXW
    2. Determine single delivery or batch delivery
    3. Select corresponding template

    Rules from spec:
    - Delivery time: use Contract value (never CQP)
    - Payment terms: verbatim from Annex 2 (never rewrite)
    - Ship-to EXW: "EXW Shanghai" + buyer name + buyer address
    - Ship-to DDP: fill with destination from contract
    - Incoterm 1: DDP or EXW
    - Incoterm 2 EXW: "Shanghai"; DDP: explicit destination (NEVER default Shanghai)
    - Customer ID / GIS: from contract if available, else leave blank with note
    - GM / NM: leave blank placeholder
    """
    case = dcdb.get_review_case(case_id)
    if not case:
        raise ValueError("Case not found")

    conclusion = case.get("overall_conclusion", "")
    if conclusion in ("BLOCKED", ""):
        return {
            "available": False,
            "reason": "BT09 cannot be generated due to BLOCKER issues. Please resolve blockers first.",
            "draft": "",
        }

    docs = dcdb.list_review_documents(case_id)
    check_items = dcdb.list_check_items(case_id)
    fields = dcdb.list_extracted_fields(case_id)

    def _get_field(name):
        for f in fields:
            if f["field_name"] == name:
                ov = f.get("manual_override")
                if ov and isinstance(ov, str):
                    ov = json.loads(ov)
                if ov and isinstance(ov, dict) and ov.get("new_value"):
                    return ov["new_value"]
                return f.get("value", "")
        return ""

    # Determine incoterm from check items
    incoterm_type = "UNKNOWN"
    incoterm2_location = ""
    for item in check_items:
        if item.get("rule_id") == "INCOTERM_CONTRACT_DETERMINATION":
            vals = item.get("values", {})
            contract_val = vals.get("contract", "")
            if contract_val in ("DDP", "EXW"):
                incoterm_type = contract_val
        if item.get("rule_id") == "INCOTERM_2_LOCATION":
            vals = item.get("values", {})
            incoterm2_location = vals.get("contract", "") or vals.get("cqp", "")

    # Fallback: determine from extracted fields
    if incoterm_type == "UNKNOWN":
        incoterm_raw = _get_field("incoterm")
        if "DDP" in incoterm_raw.upper():
            incoterm_type = "DDP"
        elif "EXW" in incoterm_raw.upper():
            incoterm_type = "EXW"

    if not incoterm2_location:
        if incoterm_type == "EXW":
            incoterm2_location = "Shanghai"
        elif incoterm_type == "DDP":
            incoterm2_location = _get_field("delivery_location") or _get_field("ship_to_address") or "[DDP DESTINATION REQUIRED - DO NOT DEFAULT TO SHANGHAI]"

    buyer = _get_field("buyer_legal_name") or "[BUYER NAME REQUIRED]"
    buyer_addr = _get_field("buyer_address") or "[BUYER ADDRESS REQUIRED]"
    contract_no = _get_field("contract_number") or "[CONTRACT NO REQUIRED]"
    cqp_no = _get_field("cqp_number") or "[CQP NO REQUIRED]"
    models = _get_field("robot_models") or "[ROBOT MODELS REQUIRED]"
    quantity = _get_field("total_quantity") or "[QUANTITY REQUIRED]"
    # DELIVERY TIME: spec says use Contract value, NOT CQP
    delivery = _get_field("delivery_time") or "[DELIVERY TIME REQUIRED - CHECK CONTRACT]"
    # PAYMENT TERMS: spec says verbatim from Annex 2, never rewrite
    payment = _get_field("payment_terms") or "[PAYMENT TERMS REQUIRED - ANNEX 2]"
    end_customer = _get_field("end_customer_name") or "[END CUSTOMER REQUIRED]"
    sales = _get_field("sales_person") or "[SALES REQUIRED]"
    pm = _get_field("pm") or "[PM REQUIRED]"
    ship_to_id = _get_field("ship_to_id") or "[TO BE CONFIRMED]"
    end_cust_id = _get_field("end_customer_id") or "[TO BE CONFIRMED]"
    gis = _get_field("gis_number") or "[TO BE CONFIRMED]"

    # ---- Determine Incoterm 1 (delivery term) ----
    incoterm_1 = incoterm_type if incoterm_type in ("DDP", "EXW") else "[DDP/EXW TO BE CONFIRMED]"

    # ---- Build Ship-to section based on Incoterm ----
    if incoterm_type == "EXW":
        # EXW: "EXW Shanghai" + Buyer Name + Buyer Address
        ship_to_display = f"EXW {incoterm2_location}"
        ship_to_name = buyer
        ship_to_addr = buyer_addr
    elif incoterm_type == "DDP":
        # DDP: fill with explicit destination
        ship_to_display = _get_field("delivery_location") or _get_field("ship_to_address") or "[DDP DESTINATION]"
        ship_to_name = _get_field("ship_to_name") or buyer
        ship_to_addr = _get_field("ship_to_address") or ship_to_display
    else:
        ship_to_display = _get_field("ship_to_name") or buyer
        ship_to_name = _get_field("ship_to_name") or buyer
        ship_to_addr = _get_field("ship_to_address") or buyer_addr

    # ---- Detect batch delivery from delivery time ----
    delivery_lower = delivery.lower()
    is_batch_delivery = any(word in delivery_lower for word in
        ["分批", "batch", "partial", "分批发货", "分期", "多次"])

    # ---- Template selection ----
    if incoterm_type == "DDP":
        incoterm_title = "贸易术语（Incoterm）: DDP"
        delivery_note = f"Incoterm 2 (目的地): {incoterm2_location}"
        template_note = "" if not is_batch_delivery else " (分批交付模板)"
    elif incoterm_type == "EXW":
        incoterm_title = "贸易术语（Incoterm）: EXW"
        delivery_note = f"Incoterm 2 (发货地): {incoterm2_location}"
        template_note = "" if not is_batch_delivery else " (分批交付模板)"
    else:
        incoterm_title = "贸易术语（Incoterm）: [TO BE CONFIRMED]"
        delivery_note = "Incoterm 2: [TO BE CONFIRMED]"
        template_note = ""

    # ---- Build BT09 draft ----
    bt09_draft = f"""BT09 订单创建请求{template_note}

买方（Buyer）: {buyer}
买方地址（Buyer Address）: {buyer_addr}
合同编号（Contract No.）: {contract_no}
CQP 编号: {cqp_no}

-------------- 订单内容 --------------
机器人型号（Robot Models）: {models}
总数量（Total Quantity）: {quantity}

-------------- 商务条款 --------------
{incoterm_title}
{delivery_note}
交付时间（Delivery - 以合同为准）: {delivery}
付款条件（Payment Terms - 合同 Annex 2 原文，逐字照搬）:
{payment}

-------------- 物流信息 --------------
收货方（Ship-to）: {ship_to_name}
收货地址（Ship-to Address）: {ship_to_addr}
Ship-to ID: {ship_to_id}

-------------- 终端客户 --------------
终端客户（End Customer）: {end_customer}
End Customer ID: {end_cust_id}
GIS Number: {gis}

-------------- 项目团队 --------------
销售（Sales）: {sales}
项目经理（PM）: {pm}
GM: [GM TO BE CONFIRMED]
NM: [NM TO BE CONFIRMED]

---
此 BT09 由 Document Check 系统自动生成。
付款条件取自合同附件二原文，未做任何改写。
交付时间以合同为准。
请确认以上所有信息无误后提交。
"""

    missing = []
    if not _get_field("buyer_legal_name"):
        missing.append("buyer_legal_name")
    if not _get_field("contract_number"):
        missing.append("contract_number")
    if not _get_field("robot_models"):
        missing.append("robot_models")
    if not _get_field("total_quantity"):
        missing.append("total_quantity")
    if not _get_field("payment_terms"):
        missing.append("payment_terms")
    if not _get_field("delivery_time"):
        missing.append("delivery_time")
    if incoterm_type == "UNKNOWN":
        missing.append("incoterm_determination")

    return {
        "available": True,
        "draft": bt09_draft,
        "template": {
            "incoterm": incoterm_type,
            "incoterm2": incoterm2_location,
            "is_batch_delivery": is_batch_delivery,
        },
        "missing_fields": missing,
    }


# Background review job management (matching existing sync job pattern)
REVIEW_JOBS: Dict[str, Dict[str, Any]] = {}
REVIEW_LOCK = threading.Lock()


def start_review_job(case_id: str) -> str:
    """Start a background review job. Returns job_id."""
    with REVIEW_LOCK:
        REVIEW_JOBS[case_id] = {
            "case_id": case_id,
            "status": "running",
            "progress": "Starting",
            "started_at": datetime.now().isoformat(),
        }

    def _run():
        try:
            def _progress(stage):
                with REVIEW_LOCK:
                    if case_id in REVIEW_JOBS:
                        REVIEW_JOBS[case_id]["progress"] = stage

            result = run_review(case_id, progress_callback=_progress)
            with REVIEW_LOCK:
                REVIEW_JOBS[case_id].update({
                    "status": "completed",
                    "result": result,
                })
        except Exception as exc:
            with REVIEW_LOCK:
                REVIEW_JOBS[case_id].update({
                    "status": "failed",
                    "error": str(exc),
                })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return case_id


def get_review_job_status(case_id: str) -> Dict[str, Any]:
    """Get status of a review job."""
    with REVIEW_LOCK:
        return REVIEW_JOBS.get(case_id, {"status": "unknown"})