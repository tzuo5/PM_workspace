# -*- coding: utf-8 -*-
"""ABB Contract Review Automation Service — with coordinate evidence.

Consolidated module implementing:
  1. PDF parsing (with OCR fallback + coordinate extraction)
  2. File recognition (Contract / CQP / TA)
  3. Structured data extraction with evidence
  4. Incoterm resolution
  5. Cross-document consistency checking with evidence
  6. Warranty classification & consistency
  7. Configuration comparison
  8. Financial validation
  9. Blocker/non-blocker classification
 10. BT09 email draft generation
 11. Report formatting (old report + new review_items)
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from services.contract_llm_review import run_llm_contract_review
from services.pdf_evidence import (
    ParsedPDF,
    ParsedPage,
    build_evidence_entry,
    locate_evidence_for_field,
    normalize_for_match,
    parse_pdf_with_evidence,
)

# ---------------------------------------------------------------------------
# Constants / Business Rules
# ---------------------------------------------------------------------------

SELLER_PREFIX_MAP = {
    "M": "ABB（上海）机器人投资有限公司",
    "K": "ABB机器人（珠海）有限公司",
}

WARRANTY_CODE_MAP = {
    "438-1": "Standard Warranty (18/12)",
    "438-102": "Lite Warranty (15/12)",
    "438-2": "Extended Warranty 24 months",
}

KNOWN_EQUIVALENCES = {
    "标准质保": "Standard Warranty",
    "侧面布线": "Side Dressed",
    "石墨白": "Graphite White",
}

BLOCKER_RULES = [
    "source_missing",
    "incoterm_inconsistent",
    "incoterm_undetermined",
    "robot_model_mismatch",
    "quantity_mismatch",
    "critical_config_mismatch",
    "vat_missing_or_wrong",
    "payment_terms_missing",
    "warranty_scope_conflict",
    "bt09_essential_missing",
]

NON_BLOCKER_RULES = [
    "delivery_time_diff",
    "rounding_diff_below_1",
    "translation_only_diff",
    "customer_id_not_found",
    "buyer_name_abbreviation",
]

# ---------------------------------------------------------------------------
# Legacy ParsedPage/ParsedPDF (keep for backward compat with extractors)
# These are shadowed by pdf_evidence versions but we keep the old names
# for the existing field extraction functions that don't use coordinates yet.
# ---------------------------------------------------------------------------

# Re-export for any external consumers
from services.pdf_evidence import ParsedPage as _ParsedPage
from services.pdf_evidence import ParsedPDF as _ParsedPDF

# ---------------------------------------------------------------------------
# Module 1: PDF Parser (enhanced with evidence)
# ---------------------------------------------------------------------------

def parse_pdf(filepath: str) -> ParsedPDF:
    """Parse PDF using the evidence-aware parser (PyMuPDF or pdfplumber)."""
    return parse_pdf_with_evidence(filepath)


def _parse_pdf_ocr(filepath: str) -> ParsedPDF:
    """OCR fallback using pytesseract + pdf2image.

    Note: OCR cannot produce reliable word coordinates,
    so spans will be empty for OCR pages.
    """
    result = ParsedPDF(filepath=filepath)
    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(filepath, dpi=200)
        full_text_parts = []
        pages = []
        for i, image in enumerate(images, start=1):
            text = pytesseract.image_to_string(image, lang="chi_sim+eng")
            pages.append(ParsedPage(page_num=i, text=text, width=image.width, height=image.height))
            full_text_parts.append(text)
        result.pages = pages
        result.full_text = "\n".join(full_text_parts)
    except Exception:
        # Last resort: try PyMuPDF
        try:
            import fitz
            doc = fitz.open(filepath)
            full_text_parts = []
            pages = []
            for i, page in enumerate(doc, start=1):
                text = page.get_text()
                pages.append(ParsedPage(page_num=i, text=text, width=page.rect.width, height=page.rect.height))
                full_text_parts.append(text)
            result.pages = pages
            result.full_text = "\n".join(full_text_parts)
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Module 2: File Recognizer
# ---------------------------------------------------------------------------

def recognize_files(parsed_files: List[ParsedPDF]) -> Dict[str, Any]:
    """Identify which file is Contract / CQP / TA."""
    result: Dict[str, Any] = {
        "contract": {"source": None, "status": "not_found"},
        "cqp": {"source": None, "status": "not_found"},
        "ta": {"source": None, "status": "not_found"},
    }

    for pf in parsed_files:
        text = pf.full_text

        # Contract detection
        contract_score = 0
        if re.search(r"销售合同", text):
            contract_score += 3
        if re.search(r"买方", text) and re.search(r"卖方", text):
            contract_score += 2
        if re.search(r"M\d{4}-\d{4}|K\d{4}-\d{4}", text):
            contract_score += 2
        if re.search(r"附件二|付款方式|支付方式", text):
            contract_score += 2

        # CQP detection
        cqp_score = 0
        if re.search(r"CQ\d{7}", text):
            cqp_score += 4
        if re.search(r"Quotation|报价", text, re.IGNORECASE):
            cqp_score += 2
        if re.search(r"Unit Price|单价|Total Price|总价", text, re.IGNORECASE):
            cqp_score += 2

        # TA detection
        ta_score = 0
        if re.search(r"技术协议", text):
            ta_score += 4
        if re.search(r"单台配置如下", text):
            ta_score += 3

        # Assign file
        if contract_score >= cqp_score and contract_score >= ta_score and contract_score >= 2:
            result["contract"] = {"source": pf, "status": "found"}
            # Check for embedded TA
            ta_pages = _find_ta_in_contract(pf)
            if ta_pages:
                result["ta"] = {"source": _make_virtual_pdf(ta_pages, pf.filepath + "_TA"), "status": "embedded"}
        elif cqp_score >= contract_score and cqp_score >= ta_score and cqp_score >= 2:
            result["cqp"] = {"source": pf, "status": "found"}
        elif ta_score >= 2:
            result["ta"] = {"source": pf, "status": "standalone"}

    return result


def _find_ta_in_contract(pf: ParsedPDF) -> List[ParsedPage]:
    """Check if TA pages are embedded within contract PDF."""
    ta_pages = []
    in_ta = False
    for page in pf.pages:
        if re.search(r"技术协议|单台配置如下", page.text):
            in_ta = True
        if in_ta:
            ta_pages.append(page)
    return ta_pages


def _make_virtual_pdf(pages: List[ParsedPage], filepath: str) -> ParsedPDF:
    return ParsedPDF(filepath=filepath, pages=pages,
                     full_text="\n".join(p.text for p in pages))


# ---------------------------------------------------------------------------
# Module 3: Data Extractor (with evidence)
# ---------------------------------------------------------------------------

def extract_contract(file_info: dict) -> Dict[str, Any]:
    """Extract structured fields from contract with evidence."""
    if not file_info.get("source"):
        return {}
    pf: ParsedPDF = file_info["source"]
    text = pf.full_text
    return _extract_contract_fields(text, pf)


def _extract_contract_fields(text: str, pf: Optional[ParsedPDF] = None) -> Dict[str, Any]:
    """Extract all contract fields from text with evidence."""
    result: Dict[str, Any] = {
        "contract_number": _extract_pattern(text, r"[MK]\d{4}-\d{4}"),
        "seller_name": _extract_kv(text, ["卖方：", "卖方:", "卖方"]),
        "buyer_name": _extract_kv(text, ["买方：", "买方:", "买方"]),
        "buyer_address": _extract_kv(text, ["买方地址：", "买方地址:", "地址："]),
        "end_customer_name": _extract_kv(text, ["最终用户：", "最终用户:", "最终客户："]),
        "end_customer_address": _extract_kv(text, ["设备安装地点：", "安装地点：", "安装地址："]),
        "robot_models": _extract_robot_models(text),
        "total_qty": _extract_total_qty(text),
        "incoterm_selection": _extract_incoterm_clause(text),
        "delivery_location": _extract_kv(text, ["交付地点：", "交货地点：", "交付地址："]),
        "delivery_time": _extract_delivery_times(text),
        "payment_terms_annex2": _extract_annex2(text),
        "warranty_clause_5_2": _extract_warranty_clause(text),
        "vat_rate": _extract_vat(text),
        "untaxed_amount": _extract_amount(text, ["未税金额", "不含税金额", "未税总价"]),
        "tax_included_amount": _extract_amount(text, ["含税金额", "含税总价", "税后金额"]),
        "sales_person": _extract_kv(text, ["销售：", "销售经理：", "Sales："]),
        "pm": _extract_kv(text, ["PM：", "项目经理："]),
    }
    # Attach evidence if PDF is available
    if pf:
        result["_evidence"] = _build_contract_evidence(result, pf)
    return result


def _build_contract_evidence(fields: Dict[str, Any], pf: ParsedPDF) -> Dict[str, Any]:
    """Build evidence entries for contract fields."""
    evidence: Dict[str, Any] = {}
    field_map = [
        ("contract_number", "合同编号"),
        ("seller_name", "卖方实体"),
        ("buyer_name", "买方名称"),
        ("end_customer_name", "最终用户"),
        ("robot_models", "机器人型号"),
        ("total_qty", "总数量"),
        ("incoterm_selection", "贸易术语"),
        ("delivery_location", "交付地点"),
        ("delivery_time", "交付周期"),
        ("payment_terms_annex2", "付款条款"),
        ("warranty_clause_5_2", "质保条款"),
        ("vat_rate", "增值税率"),
        ("untaxed_amount", "未税金额"),
        ("tax_included_amount", "含税金额"),
    ]
    for key, label in field_map:
        val = fields.get(key, "")
        str_val = str(val) if val else ""
        if isinstance(val, list):
            str_val = "; ".join(str(v) for v in val)
        ev = locate_evidence_for_field(pf, "contract", str_val, [], label)
        if ev["page"] > 0:
            evidence[key] = ev
    return evidence


def extract_cqp(file_info: dict) -> Dict[str, Any]:
    """Extract structured fields from CQP with evidence."""
    if not file_info.get("source"):
        return {}
    pf: ParsedPDF = file_info["source"]
    text = pf.full_text
    result: Dict[str, Any] = {
        "cqp_number": _extract_pattern(text, r"CQ\d{7}[A-Z]?\d*"),
        "customer_name": _extract_kv(text, ["Customer：", "客户名称：", "客户："]),
        "customer_address": _extract_kv(text, ["Customer Address：", "客户地址："]),
        "end_user": _extract_kv(text, ["End User：", "最终用户："]),
        "delivery_term": _extract_kv(text, ["Delivery Term：", "交货条款：", "交付条款："]),
        "delivery_time": _extract_kv(text, ["Delivery Time：", "交货期：", "交付周期："]),
        "payment_terms": _extract_kv(text, ["Payment Terms：", "付款条款：", "付款方式："]),
        "warranty_terms": _extract_kv(text, ["Warranty：", "质保：", "质保条款："]),
        "robot_models": _extract_cqp_robot_models(text, pf.pages),
        "untaxed_total": _extract_amount(text, ["Total Net", "未税总价", "Net Total"]),
        "vat_rate": _extract_vat(text),
        "tax_included_total": _extract_amount(text, ["Total Gross", "含税总价", "Gross Total"]),
        "configurations": _extract_config_from_pages(pf.pages),
        "warranty_codes": _extract_warranty_codes(text),
    }
    # Attach evidence
    evidence: Dict[str, Any] = {}
    field_map = [
        ("cqp_number", "CQP编号"),
        ("customer_name", "客户名称"),
        ("end_user", "最终用户"),
        ("delivery_term", "交付条款"),
        ("delivery_time", "交付周期"),
        ("payment_terms", "付款条款"),
        ("warranty_terms", "质保条款"),
        ("robot_models", "机器人型号"),
        ("vat_rate", "增值税率"),
        ("untaxed_total", "未税总价"),
        ("tax_included_total", "含税总价"),
    ]
    for key, label in field_map:
        val = result.get(key, "")
        str_val = str(val) if val else ""
        if isinstance(val, list):
            str_val = "; ".join(str(v) for v in val)
        ev = locate_evidence_for_field(pf, "cqp", str_val, [], label)
        if ev["page"] > 0:
            evidence[key] = ev
    result["_evidence"] = evidence
    return result


def extract_ta(file_info: dict) -> Dict[str, Any]:
    """Extract structured fields from TA (standalone or embedded)."""
    if not file_info.get("source") or file_info.get("status") == "not_found":
        return {}
    pf: ParsedPDF = file_info["source"]
    text = pf.full_text
    result: Dict[str, Any] = {
        "robot_models": _extract_robot_models(text),
        "configurations": _extract_config_from_pages(pf.pages),
        "warranty_codes": _extract_warranty_codes(text),
    }
    evidence: Dict[str, Any] = {}
    for key, label in [("robot_models", "机器人型号"), ("warranty_codes", "质保代码")]:
        val = result.get(key, "")
        str_val = str(val) if val else ""
        if isinstance(val, list):
            str_val = "; ".join(str(v) for v in val)
        ev = locate_evidence_for_field(pf, "ta", str_val, [], label)
        if ev["page"] > 0:
            evidence[key] = ev
    result["_evidence"] = evidence
    return result


# ---------------------------------------------------------------------------
# Extraction Helpers
# ---------------------------------------------------------------------------

def _extract_pattern(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    return m.group(0).strip() if m else ""


def _extract_kv(text: str, keys: List[str]) -> str:
    for key in keys:
        for pat in [re.escape(key) + r"\s*[:：]?\s*([^\n]{0,80})",
                     re.escape(key) + r"\s*([^\n]{0,80})"]:
            m = re.search(pat, text)
            if m:
                val = m.group(1).strip()
                val = re.sub(r'[\t\r]', ' ', val).strip()
                if val and not val.startswith('_'):
                    return val
    return ""


def _extract_robot_models(text: str) -> List[Dict[str, Any]]:
    """Extract robot models like IRB 1200, IRB 4600-60/2.05, etc."""
    models = []
    seen = set()
    for m in re.finditer(r'(IRB\s*\d{4}[^\n,，;]{0,30})', text, re.IGNORECASE):
        model = m.group(0).strip()
        if model not in seen:
            models.append({"model": model, "qty": 1})
            seen.add(model)
    for i, entry in enumerate(models):
        qty_match = re.search(
            re.escape(entry["model"]) + r'.{0,50}?(\d+)\s*(?:台|set|unit|套)',
            text, re.IGNORECASE
        )
        if qty_match:
            entry["qty"] = int(qty_match.group(1))
    return models


def _extract_total_qty(text: str) -> int:
    m = re.search(r'(?:共计|总计|总数|Total Qty|Qty)[^\d]*(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0


def _extract_incoterm_clause(text: str) -> str:
    ddp_patterns = ["买方工厂的到货价", "DDP", "到货价", "delivered duty paid"]
    exw_patterns = ["买方工厂出厂价", "EXW", "出厂价", "ex works"]
    ddp_found = any(re.search(p, text, re.IGNORECASE) for p in ddp_patterns)
    exw_found = any(re.search(p, text, re.IGNORECASE) for p in exw_patterns)
    if ddp_found and not exw_found:
        return "买方工厂的到货价(DDP)"
    elif exw_found and not ddp_found:
        return "买方工厂出厂价(EXW)"
    return ""


def _extract_delivery_times(text: str) -> List[Dict[str, Any]]:
    times = []
    for m in re.finditer(r'(\d+)\s*(?:周|week|W)\s*(?:内)?\s*(?:交货|交付|delivery)?', text, re.IGNORECASE):
        weeks = int(m.group(1))
        times.append({"model": "all", "weeks": weeks, "condition": ""})
    return times


def _extract_annex2(text: str) -> str:
    annex_start = -1
    for pattern in ["附件二", "付款方式", "支付条款", "Payment Terms"]:
        idx = text.find(pattern)
        if idx >= 0:
            annex_start = idx
            break
    if annex_start >= 0:
        return text[annex_start:annex_start + 1500]
    return ""


def _extract_warranty_clause(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"standard": "18/12", "special": []}
    m = re.search(r'5\.\s*2.*?质保.*?(\d+)\s*(?:个?月|month).*?(\d+)\s*(?:个?月|month)', text, re.IGNORECASE)
    if m:
        result["standard"] = f"{m.group(1)}/{m.group(2)}"
    special_matches = re.findall(r'(?:特殊|Special|Lite|LPS).*?(\d+)\s*(?:个?月|month).*?(\d+)\s*(?:个?月|month)', text, re.IGNORECASE)
    for sm in special_matches:
        result["special"].append({"model": "LPS", "period": f"{sm[0]}/{sm[1]}"})
    return result


def _extract_vat(text: str) -> float:
    m = re.search(r'(?:增值税|VAT|税率).*?(\d{1,2})(?:\s*%\s*|%)', text, re.IGNORECASE)
    if m:
        return float(m.group(1)) / 100.0
    return 0.13


def _extract_amount(text: str, keywords: List[str]) -> float:
    for kw in keywords:
        m = re.search(re.escape(kw) + r'.{0,30}?([\d,]+\.?\d*)', text, re.IGNORECASE)
        if m:
            val = m.group(1).replace(",", "")
            try:
                return float(val)
            except ValueError:
                pass
    return 0.0


def _extract_cqp_robot_models(text: str, pages: List[ParsedPage]) -> List[Dict[str, Any]]:
    models = _extract_robot_models(text)
    for model in models:
        code_m = re.search(r'(\d{7,9}).{0,30}' + re.escape(model["model"]), text)
        if code_m:
            model["item_code"] = code_m.group(1)
        else:
            model["item_code"] = ""
        price_m = re.search(re.escape(model["model"]) + r'.{0,60}?([\d,]+\.?\d{0,2})', text)
        if price_m:
            try:
                model["unit_price"] = float(price_m.group(1).replace(",", ""))
            except ValueError:
                model["unit_price"] = 0.0
        else:
            model["unit_price"] = 0.0
        model["total_price"] = model["unit_price"] * model.get("qty", 1)
    return models


def _extract_config_from_pages(pages: List[ParsedPage]) -> List[Dict[str, Any]]:
    configs = []
    for page in pages:
        codes = re.findall(r'(\d{3,4}-\d{1,3})\s+(.{0,60})', page.text)
        for code, desc in codes:
            configs.append({"code": code, "description": desc.strip()})
        for table in (page.tables or []):
            for row in table:
                if row and row[0] and re.match(r'\d{3,4}-\d{1,3}', str(row[0])):
                    desc = str(row[1]) if len(row) > 1 else ""
                    configs.append({"code": str(row[0]), "description": desc.strip()})
    return configs


def _extract_warranty_codes(text: str) -> List[str]:
    codes = re.findall(r'(438-\d{1,3})', text)
    return list(set(codes))


# ---------------------------------------------------------------------------
# Module 4: Incoterm Resolver
# ---------------------------------------------------------------------------

def resolve_incoterm(contract_fields: Dict[str, Any], cqp_fields: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "conclusion": "UNDETERMINED",
        "contract_evidence": "",
        "cqp_evidence": "",
        "consistent": True,
        "is_blocker": False,
        "notes": "",
    }
    contract_incoterm = contract_fields.get("incoterm_selection", "")
    delivery_loc = contract_fields.get("delivery_location", "")
    cqp_delivery = cqp_fields.get("delivery_term", "")

    result["contract_evidence"] = contract_incoterm or "(未提取到)"
    result["cqp_evidence"] = cqp_delivery or "(未提取到)"

    if "到货价" in contract_incoterm or "DDP" in contract_incoterm.upper():
        result["conclusion"] = "DDP"
    elif "出厂价" in contract_incoterm or "EXW" in contract_incoterm.upper():
        result["conclusion"] = "EXW"
    elif delivery_loc and len(delivery_loc) > 2:
        result["conclusion"] = "DDP"
        result["notes"] = "基于交付地点推断为DDP"
    elif "DDP" in cqp_delivery.upper():
        result["conclusion"] = "DDP"
    elif "EXW" in cqp_delivery.upper():
        result["conclusion"] = "EXW"

    if result["conclusion"] == "DDP" and "EXW" in cqp_delivery.upper():
        result["consistent"] = False
        result["is_blocker"] = True
        result["notes"] = "Contract indicates DDP but CQP says EXW"
    elif result["conclusion"] == "EXW" and "DDP" in cqp_delivery.upper():
        result["consistent"] = False
        result["is_blocker"] = True
        result["notes"] = "Contract indicates EXW but CQP says DDP"

    if result["conclusion"] == "UNDETERMINED":
        result["is_blocker"] = True
        result["notes"] = "Cannot determine incoterm from any source"

    return result


# ---------------------------------------------------------------------------
# Module 5: Consistency Checker (with evidence)
# ---------------------------------------------------------------------------

def run_consistency_checks(
    contract_data: Dict[str, Any],
    cqp_data: Dict[str, Any],
    ta_data: Dict[str, Any],
    contract_pf: Optional[ParsedPDF] = None,
    cqp_pf: Optional[ParsedPDF] = None,
    ta_pf: Optional[ParsedPDF] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run all cross-document consistency checks with evidence.

    Returns (legacy_checks, review_items).
    """
    checks: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []

    # 1. Contract number linkage
    ck, ci = _check_contract_number_linkage(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 2. Seller entity validation
    ck, ci = _check_seller_entity(contract_data, contract_pf)
    checks.append(ck)
    items.append(ci)

    # 3. Buyer consistency
    ck, ci = _check_buyer_consistency(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 4. End customer consistency
    ck, ci = _check_end_customer_consistency(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 5. Robot model consistency
    ck, ci = _check_robot_model_consistency(contract_data, cqp_data, ta_data, contract_pf, cqp_pf, ta_pf)
    checks.append(ck)
    items.append(ci)

    # 6. Quantity consistency
    ck, ci = _check_quantity_consistency(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 7. Payment terms
    ck, ci = _check_payment_terms(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 8. Delivery location
    ck, ci = _check_delivery_location(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 9. Delivery time
    ck, ci = _check_delivery_time(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 10. VAT rate
    ck, ci = _check_vat_rate(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 11. Untaxed amount
    ck, ci = _check_untaxed_amount(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 12. Tax-included amount
    ck, ci = _check_tax_included_amount(contract_data, cqp_data, contract_pf, cqp_pf)
    checks.append(ck)
    items.append(ci)

    # 13. Warranty consistency
    ck, ci = _check_warranty_review_item(contract_data, cqp_data, ta_data, contract_pf, cqp_pf, ta_pf)
    checks.append(ck)
    items.append(ci)

    # 14. Configuration comparison
    ck, ci = _check_config_review_item(contract_data, cqp_data, ta_data, cqp_pf, ta_pf)
    checks.append(ck)
    items.append(ci)

    return checks, items


def _make_item_id(name: str) -> str:
    """Create a stable, unique ID from Chinese check name."""
    mapping = {
        "合同号关联": "contract_number_linkage",
        "卖方实体校验": "seller_entity",
        "买方一致性": "buyer_consistency",
        "最终客户一致性": "end_customer_consistency",
        "机器人型号一致性": "robot_model_consistency",
        "数量一致性": "quantity_consistency",
        "付款条款": "payment_terms",
        "交付地点": "delivery_location",
        "交付周期": "delivery_time",
        "增值税率": "vat_rate",
        "未税金额": "untaxed_amount",
        "含税金额": "tax_included_amount",
        "质保一致性": "warranty_consistency",
        "CQP与TA配置差异": "config_comparison",
        "贸易术语": "incoterm",
    }
    return mapping.get(name, re.sub(r'[^a-z0-9_]', '_', name.lower()))


def _get_evidence(
    contract_pf: Optional[ParsedPDF],
    cqp_pf: Optional[ParsedPDF],
    ta_pf: Optional[ParsedPDF],
    contract_fields: Optional[Dict[str, Any]] = None,
    cqp_fields: Optional[Dict[str, Any]] = None,
    ta_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[ParsedPDF]]:
    return {
        "contract": contract_pf,
        "cqp": cqp_pf,
        "ta": ta_pf,
    }


def _build_evidence_list(
    specs: List[Tuple[Optional[ParsedPDF], str, int, str, str, List]],
) -> List[Dict[str, Any]]:
    """Build a list of evidence entries from specification tuples."""
    result = []
    for pf, doc_type, page, label, quote, rects in specs:
        if pf is None:
            result.append({
                "document_type": doc_type,
                "page": page,
                "label": label,
                "quote": quote,
                "rects": rects or [],
            })
        else:
            result.append(build_evidence_entry(pf, doc_type, page, label, quote, rects))
    return result


# ---- Individual check functions ----

def _check_contract_number_linkage(contract_data, cqp_data, contract_pf, cqp_pf):
    c_num = contract_data.get("contract_number", "")
    cqp_num = cqp_data.get("cqp_number", "")
    has_contract = bool(c_num)
    has_cqp = bool(cqp_num)
    status = "PASS" if (has_contract and has_cqp) else "WARNING"
    sev = "info" if status == "PASS" else "warning"

    ev_contract = contract_data.get("_evidence", {}).get("contract_number")
    ev_cqp = cqp_data.get("_evidence", {}).get("cqp_number")

    evidence_list = []
    if ev_contract and ev_contract["page"] > 0:
        evidence_list.append(ev_contract)
    if ev_cqp and ev_cqp["page"] > 0:
        evidence_list.append(ev_cqp)

    check = {
        "check_name": "合同号关联",
        "status": status,
        "detail": f"Contract: {c_num or '未提取'} / CQP: {cqp_num or '未提取'}",
        "is_blocker": False,
    }
    item = {
        "id": _make_item_id("合同号关联"),
        "category": "commercial",
        "title": "合同号关联",
        "status": "WARNING" if status == "WARNING" else "PASS",
        "severity": sev,
        "summary": f"Contract: {c_num or '未提取'} / CQP: {cqp_num or '未提取'}",
        "values": {
            "contract": c_num or "(未提取)",
            "cqp": cqp_num or "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_seller_entity(contract_data, contract_pf):
    contract_num = contract_data.get("contract_number", "")
    seller = contract_data.get("seller_name", "")
    prefix = contract_num[0] if contract_num else ""
    expected = SELLER_PREFIX_MAP.get(prefix, "")
    status = "PASS"
    if expected and seller and expected != seller:
        status = "MISMATCH"
    sev = "info" if status == "PASS" else "warning"

    ev = contract_data.get("_evidence", {}).get("seller_name")
    evidence_list = [ev] if ev and ev["page"] > 0 else []

    check = {
        "check_name": "卖方实体校验",
        "status": status,
        "detail": f"合同号前缀: {prefix or '无'} → 预期卖方: {expected or 'N/A'}, 实际: {seller or '未提取'}",
        "is_blocker": False,
    }
    item = {
        "id": _make_item_id("卖方实体校验"),
        "category": "commercial",
        "title": "卖方实体校验",
        "status": status,
        "severity": sev,
        "summary": f"预期: {expected or 'N/A'} / 实际: {seller or '未提取'}",
        "values": {
            "contract": seller or "(未提取)",
            "expected": expected or "N/A",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_buyer_consistency(contract_data, cqp_data, contract_pf, cqp_pf):
    c_buyer = contract_data.get("buyer_name", "")
    cqp_customer = cqp_data.get("customer_name", "")
    status = "PASS" if (not c_buyer or not cqp_customer) else (
        "MISMATCH" if c_buyer != cqp_customer else "PASS"
    )
    sev = "blocker" if status == "MISMATCH" else "info"

    ev1 = contract_data.get("_evidence", {}).get("buyer_name")
    ev2 = cqp_data.get("_evidence", {}).get("customer_name")
    evidence_list = []
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "买方一致性",
        "status": status,
        "detail": f"Contract: {c_buyer or '未提取'} / CQP: {cqp_customer or '未提取'}",
        "is_blocker": status == "MISMATCH",
    }
    item = {
        "id": _make_item_id("买方一致性"),
        "category": "commercial",
        "title": "买方一致性",
        "status": status,
        "severity": sev,
        "summary": f"Contract: {c_buyer or '未提取'} / CQP: {cqp_customer or '未提取'}",
        "values": {
            "contract": c_buyer or "(未提取)",
            "cqp": cqp_customer or "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_end_customer_consistency(contract_data, cqp_data, contract_pf, cqp_pf):
    c_end = contract_data.get("end_customer_name", "")
    cqp_end = cqp_data.get("end_user", "")
    status = "PASS" if (not c_end and not cqp_end) else (
        "MISMATCH" if (c_end and cqp_end and c_end != cqp_end) else "PASS"
    )
    sev = "warning" if status == "MISMATCH" else "info"

    ev1 = contract_data.get("_evidence", {}).get("end_customer_name")
    ev2 = cqp_data.get("_evidence", {}).get("end_user")
    evidence_list = []
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "最终客户一致性",
        "status": status,
        "detail": f"Contract: {c_end or '未提取'} / CQP: {cqp_end or '未提取'}",
        "is_blocker": False,
    }
    item = {
        "id": _make_item_id("最终客户一致性"),
        "category": "commercial",
        "title": "最终客户一致性",
        "status": status,
        "severity": sev,
        "summary": f"Contract: {c_end or '未提取'} / CQP: {cqp_end or '未提取'}",
        "values": {
            "contract": c_end or "(未提取)",
            "cqp": cqp_end or "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_robot_model_consistency(contract_data, cqp_data, ta_data, contract_pf, cqp_pf, ta_pf):
    c_models = {m["model"] for m in contract_data.get("robot_models", [])}
    cqp_models = {m["model"] for m in cqp_data.get("robot_models", [])}
    ta_models = {m["model"] for m in ta_data.get("robot_models", [])}

    all_models = c_models | cqp_models | ta_models
    mismatches = []
    for model in all_models:
        present_in = []
        if model in c_models: present_in.append("Contract")
        if model in cqp_models: present_in.append("CQP")
        if model in ta_models: present_in.append("TA")
        if len(present_in) < 2 and len(present_in) > 0:
            mismatches.append(f"{model} 仅在 {','.join(present_in)} 中出现")

    status = "MISMATCH" if mismatches else "PASS"
    sev = "blocker" if status == "MISMATCH" else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("robot_models")
    ev2 = cqp_data.get("_evidence", {}).get("robot_models")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "机器人型号一致性",
        "status": status,
        "detail": "; ".join(mismatches) if mismatches else "所有型号一致",
        "is_blocker": status == "MISMATCH",
    }
    item = {
        "id": _make_item_id("机器人型号一致性"),
        "category": "technical",
        "title": "机器人型号一致性",
        "status": status,
        "severity": sev,
        "summary": "; ".join(mismatches) if mismatches else "所有型号一致",
        "values": {
            "contract": ", ".join(sorted(c_models)) or "(未提取)",
            "cqp": ", ".join(sorted(cqp_models)) or "(未提取)",
            "ta": ", ".join(sorted(ta_models)) or "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_quantity_consistency(contract_data, cqp_data, contract_pf, cqp_pf):
    c_qty = contract_data.get("total_qty", 0)
    c_model_qty_total = sum(m.get("qty", 0) for m in contract_data.get("robot_models", []))
    cqp_qty = sum(m.get("qty", 0) for m in cqp_data.get("robot_models", []))
    # Use the more reliable figure
    effective_contract_qty = c_qty if c_qty > 0 else c_model_qty_total
    status = "PASS" if effective_contract_qty == cqp_qty or effective_contract_qty == 0 or cqp_qty == 0 else "MISMATCH"
    sev = "blocker" if status == "MISMATCH" else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("total_qty")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    # Add robot model evidence from both
    ev_models_c = contract_data.get("_evidence", {}).get("robot_models")
    ev_models_cqp = cqp_data.get("_evidence", {}).get("robot_models")
    if ev_models_c and ev_models_c["page"] > 0:
        evidence_list.append(ev_models_c)
    if ev_models_cqp and ev_models_cqp["page"] > 0:
        evidence_list.append(ev_models_cqp)

    check = {
        "check_name": "数量一致性",
        "status": status,
        "detail": f"Contract: {effective_contract_qty} (系统提取: Contract {c_model_qty_total}) / CQP: {cqp_qty}",
        "is_blocker": status == "MISMATCH",
    }
    item = {
        "id": _make_item_id("数量一致性"),
        "category": "commercial",
        "title": "数量一致性",
        "status": status,
        "severity": sev,
        "summary": f"Contract: {effective_contract_qty} / CQP: {cqp_qty} (系统提取: Contract {c_model_qty_total} / CQP {cqp_qty})",
        "values": {
            "contract": str(effective_contract_qty),
            "cqp": str(cqp_qty),
            "calculated_contract": str(c_model_qty_total),
            "calculated_cqp": str(cqp_qty),
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_payment_terms(contract_data, cqp_data, contract_pf, cqp_pf):
    annex = contract_data.get("payment_terms_annex2", "")
    cqp_terms = cqp_data.get("payment_terms", "")
    status = "PASS" if annex else "WARNING"
    sev = "warning" if status == "WARNING" else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("payment_terms_annex2")
    ev2 = cqp_data.get("_evidence", {}).get("payment_terms")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "付款条款",
        "status": status,
        "detail": "已提取" if annex else "未提取附件二付款条款",
        "is_blocker": status == "WARNING",
    }
    item = {
        "id": _make_item_id("付款条款"),
        "category": "commercial",
        "title": "付款条款",
        "status": status,
        "severity": sev,
        "summary": f"Contract附件二: {'已提取' if annex else '未提取'} / CQP: {cqp_terms or '未提取'}",
        "values": {
            "contract": (annex[:200] + "..." if len(annex) > 200 else annex) or "(未提取)",
            "cqp": cqp_terms or "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_delivery_location(contract_data, cqp_data, contract_pf, cqp_pf):
    c_loc = contract_data.get("delivery_location", "")
    cqp_term = cqp_data.get("delivery_term", "")
    status = "PASS"
    if (c_loc and cqp_term):
        # Simple check if delivery locations are compatible
        pass
    sev = "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("delivery_location")
    ev2 = cqp_data.get("_evidence", {}).get("delivery_term")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "交付地点",
        "status": status,
        "detail": f"Contract: {c_loc or '未提取'} / CQP: {cqp_term or '未提取'}",
        "is_blocker": False,
    }
    item = {
        "id": _make_item_id("交付地点"),
        "category": "commercial",
        "title": "交付地点",
        "status": status,
        "severity": sev,
        "summary": f"Contract: {c_loc or '未提取'} / CQP: {cqp_term or '未提取'}",
        "values": {
            "contract": c_loc or "(未提取)",
            "cqp": cqp_term or "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_delivery_time(contract_data, cqp_data, contract_pf, cqp_pf):
    c_time = contract_data.get("delivery_time", [])
    cqp_time = cqp_data.get("delivery_time", "")
    c_str = "; ".join(f"{t.get('model','all')}:{t.get('weeks','?')}周" for t in c_time) if c_time else "未提取"
    status = "PASS" if (c_time or cqp_time) else "WARNING"
    sev = "warning" if status == "WARNING" else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("delivery_time")
    ev2 = cqp_data.get("_evidence", {}).get("delivery_time")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "交付周期",
        "status": status,
        "detail": f"Contract: {c_str} / CQP: {cqp_time or '未提取'}",
        "is_blocker": False,
    }
    item = {
        "id": _make_item_id("交付周期"),
        "category": "commercial",
        "title": "交付周期",
        "status": status,
        "severity": sev,
        "summary": f"Contract: {c_str} / CQP: {cqp_time or '未提取'}",
        "values": {
            "contract": c_str,
            "cqp": str(cqp_time) if cqp_time else "(未提取)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_vat_rate(contract_data, cqp_data, contract_pf, cqp_pf):
    cv = contract_data.get("vat_rate", 0.13)
    cqv = cqp_data.get("vat_rate", 0.13)
    status = "PASS" if abs(cv - cqv) < 0.001 else "MISMATCH"
    sev = "blocker" if status == "MISMATCH" else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("vat_rate")
    ev2 = cqp_data.get("_evidence", {}).get("vat_rate")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "增值税率",
        "status": status,
        "detail": f"Contract: {cv*100:.0f}% / CQP: {cqv*100:.0f}%",
        "is_blocker": status == "MISMATCH",
    }
    item = {
        "id": _make_item_id("增值税率"),
        "category": "financial",
        "title": "增值税率",
        "status": status,
        "severity": sev,
        "summary": f"Contract: {cv*100:.0f}% / CQP: {cqv*100:.0f}%",
        "values": {
            "contract": f"{cv*100:.0f}%",
            "cqp": f"{cqv*100:.0f}%",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_untaxed_amount(contract_data, cqp_data, contract_pf, cqp_pf):
    cu = contract_data.get("untaxed_amount", 0.0)
    cqu = cqp_data.get("untaxed_total", 0.0)
    diff = abs(cu - cqu) if (cu and cqu) else 0
    status = "PASS" if diff < 0.01 else "MISMATCH"
    sev = "blocker" if status == "MISMATCH" else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("untaxed_amount")
    ev2 = cqp_data.get("_evidence", {}).get("untaxed_total")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "未税金额",
        "status": status,
        "detail": f"Contract: ¥{cu:,.2f} / CQP: ¥{cqu:,.2f} (差异: ¥{diff:,.2f})",
        "is_blocker": status == "MISMATCH",
    }
    item = {
        "id": _make_item_id("未税金额"),
        "category": "financial",
        "title": "未税金额",
        "status": status,
        "severity": sev,
        "summary": f"Contract: ¥{cu:,.2f} / CQP: ¥{cqu:,.2f} (差异: ¥{diff:,.2f})",
        "values": {
            "contract": f"¥{cu:,.2f}",
            "cqp": f"¥{cqu:,.2f}",
            "diff": f"¥{diff:,.2f}",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_tax_included_amount(contract_data, cqp_data, contract_pf, cqp_pf):
    ct = contract_data.get("tax_included_amount", 0.0)
    cqt = cqp_data.get("tax_included_total", 0.0)
    diff = abs(ct - cqt) if (ct and cqt) else 0
    is_rounding = 0 < diff < 1.0
    status = "PASS" if diff < 0.01 else ("WARNING" if is_rounding else "MISMATCH")
    sev = "blocker" if (status == "MISMATCH" and not is_rounding) else ("warning" if is_rounding else "info")

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("tax_included_amount")
    ev2 = cqp_data.get("_evidence", {}).get("tax_included_total")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "含税金额",
        "status": status,
        "detail": f"Contract: ¥{ct:,.2f} / CQP: ¥{cqt:,.2f} (差异: ¥{diff:,.4f}{' 舍入误差' if is_rounding else ''})",
        "is_blocker": status == "MISMATCH" and not is_rounding,
    }
    item = {
        "id": _make_item_id("含税金额"),
        "category": "financial",
        "title": "含税金额",
        "status": status,
        "severity": sev,
        "summary": f"Contract: ¥{ct:,.2f} / CQP: ¥{cqt:,.2f} (差异: ¥{diff:,.4f}{' 舍入误差' if is_rounding else ''})",
        "values": {
            "contract": f"¥{ct:,.2f}",
            "cqp": f"¥{cqt:,.2f}",
            "diff": f"¥{diff:,.4f}",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_warranty_review_item(contract_data, cqp_data, ta_data, contract_pf, cqp_pf, ta_pf):
    warranty_clause = contract_data.get("warranty_clause_5_2", {})
    standard = warranty_clause.get("standard", "18/12")
    cqp_codes = cqp_data.get("warranty_codes", [])
    ta_codes = ta_data.get("warranty_codes", [])

    try:
        months_a, months_b = map(int, standard.split("/"))
    except (ValueError, AttributeError):
        months_a, months_b = 18, 12

    if months_a > 18:
        classification = "Extended"
    elif months_a == 18 and months_b == 12:
        classification = "Standard"
    elif months_a < 18 or months_b < 12:
        classification = "Lite/Special"
    else:
        classification = "Standard"

    is_consistent = True
    detail = ""
    for code in cqp_codes:
        label = WARRANTY_CODE_MAP.get(code, f"Unknown ({code})")
        if "Lite" in label and classification != "Lite/Special":
            is_consistent = False
            detail = f"CQP质保代码 {code} ({label}) 与合同5.2条款 {classification} 不符"
        elif "Standard" in label and classification not in ("Standard",):
            is_consistent = False
            detail = f"CQP质保代码 {code} ({label}) 与合同5.2条款 {classification} 不符"

    status = "MISMATCH" if not is_consistent else "PASS"
    sev = "blocker" if not is_consistent else "info"

    evidence_list = []
    ev1 = contract_data.get("_evidence", {}).get("warranty_clause_5_2")
    ev2 = cqp_data.get("_evidence", {}).get("warranty_terms")
    if ev1 and ev1["page"] > 0:
        evidence_list.append(ev1)
    if ev2 and ev2["page"] > 0:
        evidence_list.append(ev2)

    check = {
        "check_name": "质保一致性",
        "status": status,
        "detail": detail or "Warranty terms are consistent",
        "is_blocker": not is_consistent,
    }
    item = {
        "id": _make_item_id("质保一致性"),
        "category": "technical",
        "title": "质保一致性",
        "status": status,
        "severity": sev,
        "summary": f"合同: {classification} ({standard}) / CQP质保代码: {', '.join(cqp_codes) or '无'} / TA质保代码: {', '.join(ta_codes) or '无'}",
        "values": {
            "contract_warranty": f"{classification} ({standard})",
            "cqp_codes": ", ".join(cqp_codes) or "(无)",
            "ta_codes": ", ".join(ta_codes) or "(无)",
        },
        "evidence": evidence_list,
    }
    return check, item


def _check_config_review_item(contract_data, cqp_data, ta_data, cqp_pf, ta_pf):
    cqp_configs = cqp_data.get("configurations", [])
    ta_configs = ta_data.get("configurations", [])
    cqp_codes_set = {c["code"] for c in cqp_configs}
    ta_codes_set = {c["code"] for c in ta_configs}

    cqp_only = list(cqp_codes_set - ta_codes_set)
    ta_only = list(ta_codes_set - cqp_codes_set)

    has_diff = bool(cqp_only or ta_only)
    status = "MISMATCH" if has_diff else "PASS"
    sev = "warning" if has_diff else "info"

    summary_parts = []
    if cqp_only:
        summary_parts.append(f"CQP独有: {', '.join(cqp_only[:5])}")
    if ta_only:
        summary_parts.append(f"TA独有: {', '.join(ta_only[:5])}")
    if not summary_parts:
        summary_parts.append("配置代码一致")

    evidence_list = []
    # Include per-code evidence if available
    ev_cqp = cqp_data.get("_evidence", {})
    ev_ta = ta_data.get("_evidence", {})

    check = {
        "check_name": "CQP与TA配置差异",
        "status": status,
        "detail": "; ".join(summary_parts),
        "is_blocker": len(cqp_only) > 0,
    }
    item = {
        "id": _make_item_id("CQP与TA配置差异"),
        "category": "technical",
        "title": "CQP与TA配置差异",
        "status": status,
        "severity": sev,
        "summary": "; ".join(summary_parts),
        "values": {
            "cqp_only_codes": ", ".join(cqp_only[:10]) or "(无)",
            "ta_only_codes": ", ".join(ta_only[:10]) or "(无)",
        },
        "evidence": evidence_list,
        # Sub-items for individual configuration codes
        "sub_items": [
            {
                "code": code,
                "title": f"配置代码 {code}",
                "status": "CQP_ONLY" if code in cqp_codes_set else "TA_ONLY",
                "summary": f"{code} 仅在 {'CQP' if code in cqp_codes_set else 'TA'} 中存在",
            }
            for code in cqp_only[:20] + ta_only[:20]
        ],
    }
    return check, item


# ---------------------------------------------------------------------------
# Module 6: Warranty Checker (legacy)
# ---------------------------------------------------------------------------

def check_warranty(
    contract_fields: Dict[str, Any],
    cqp_fields: Dict[str, Any],
    ta_fields: Dict[str, Any]
) -> Dict[str, Any]:
    warranty_clause = contract_fields.get("warranty_clause_5_2", {})
    standard = warranty_clause.get("standard", "18/12")
    special = warranty_clause.get("special", [])

    try:
        months_a, months_b = map(int, standard.split("/"))
    except (ValueError, AttributeError):
        months_a, months_b = 18, 12

    if months_a > 18:
        classification = "Extended"
    elif months_a == 18 and months_b == 12:
        classification = "Standard"
    elif months_a < 18 or months_b < 12:
        classification = "Lite/Special"
    else:
        classification = "Standard"

    cqp_codes = cqp_fields.get("warranty_codes", [])
    ta_codes = ta_fields.get("warranty_codes", [])

    cqp_classifications = []
    for code in cqp_codes:
        cqp_classifications.append({
            "code": code,
            "label": WARRANTY_CODE_MAP.get(code, f"Unknown ({code})"),
        })

    is_consistent = True
    detail = ""
    for c in cqp_classifications:
        if "Lite" in c["label"] and classification != "Lite/Special":
            is_consistent = False
            detail = f"CQP warranty code {c['code']} implies Lite but Contract 5.2 is {classification}"
        elif "Standard" in c["label"] and classification not in ("Standard",):
            is_consistent = False
            detail = f"CQP warranty code {c['code']} implies Standard but Contract 5.2 is {classification}"

    if special and not is_consistent:
        detail += f"; Special warranty in contract: {special}"

    return {
        "contract_warranty": [
            {"model_scope": "All", "period": standard, "classification": classification}
        ] + [{"model_scope": s.get("model", ""), "period": s.get("period", ""), "classification": "Special"} for s in special],
        "cqp_warranty_codes": cqp_codes,
        "ta_warranty_codes": ta_codes,
        "consistent": is_consistent,
        "is_blocker": not is_consistent,
        "detail": detail or "Warranty terms are consistent",
    }


# ---------------------------------------------------------------------------
# Module 7: Config Comparator
# ---------------------------------------------------------------------------

def compare_configurations(
    cqp_configs: List[Dict[str, Any]],
    ta_configs: List[Dict[str, Any]]
) -> Dict[str, Any]:
    cqp_codes = {c["code"]: c for c in cqp_configs}
    ta_codes = {c["code"]: c for c in ta_configs}

    all_codes = set(cqp_codes.keys()) | set(ta_codes.keys())
    matched = []
    cqp_only = []
    ta_only = []
    desc_mismatches = []

    for code in all_codes:
        in_cqp = code in cqp_codes
        in_ta = code in ta_codes
        if in_cqp and in_ta:
            cqp_desc = cqp_codes[code].get("description", "")
            ta_desc = ta_codes[code].get("description", "")
            if cqp_desc != ta_desc:
                likely_translation = _is_likely_translation(cqp_desc, ta_desc)
                desc_mismatches.append({
                    "code": code,
                    "cqp_desc": cqp_desc,
                    "ta_desc": ta_desc,
                    "likely_translation": likely_translation,
                })
            matched.append(code)
        elif in_cqp:
            cqp_only.append(code)
        else:
            ta_only.append(code)

    overall_consistent = len(cqp_only) == 0 and len(ta_only) == 0 and len(desc_mismatches) == 0
    blocker_mismatches = [c for c in cqp_only if not _is_documentation_only(c, cqp_codes)]

    return {
        "models_compared": [{
            "model": "all",
            "matched_codes": matched,
            "cqp_only_codes": cqp_only,
            "ta_only_codes": ta_only,
            "description_mismatches": desc_mismatches,
        }],
        "overall_consistent": overall_consistent,
        "blocker_mismatches": blocker_mismatches,
    }


def _is_likely_translation(desc1: str, desc2: str) -> bool:
    for cn, en in KNOWN_EQUIVALENCES.items():
        if (cn in desc1 and en in desc2) or (cn in desc2 and en in desc1):
            return True
    return False


def _is_documentation_only(code: str, cqp_codes: Dict) -> bool:
    if code in cqp_codes:
        desc = cqp_codes[code].get("description", "")
        if any(kw in desc for kw in ["手册", "文档", "Manual", "Document"]):
            return True
    return False


# ---------------------------------------------------------------------------
# Module 8: Financial Checker
# ---------------------------------------------------------------------------

def check_financials(
    contract_fields: Dict[str, Any],
    cqp_fields: Dict[str, Any]
) -> Dict[str, Any]:
    contract_vat = contract_fields.get("vat_rate", 0.13)
    cqp_vat = cqp_fields.get("vat_rate", 0.13)

    contract_untaxed = contract_fields.get("untaxed_amount", 0.0)
    cqp_untaxed = cqp_fields.get("untaxed_total", 0.0)

    contract_tax_incl = contract_fields.get("tax_included_amount", 0.0)
    cqp_tax_incl = cqp_fields.get("tax_included_total", 0.0)

    vat_status = "PASS"
    if contract_vat != cqp_vat:
        vat_status = "MISMATCH" if (contract_vat > 0 and cqp_vat > 0) else "WARNING"

    untaxed_diff = abs(contract_untaxed - cqp_untaxed) if (contract_untaxed and cqp_untaxed) else 0
    untaxed_status = "PASS" if untaxed_diff < 0.01 else "MISMATCH"

    tax_diff = abs(contract_tax_incl - cqp_tax_incl) if (contract_tax_incl and cqp_tax_incl) else 0
    is_rounding = 0 < tax_diff < 1.0
    tax_status = "PASS" if tax_diff < 0.01 else ("WARNING" if is_rounding else "MISMATCH")

    is_blocker = (vat_status == "MISMATCH" or untaxed_status == "MISMATCH"
                  or (tax_status == "MISMATCH" and not is_rounding))

    return {
        "vat_check": {
            "status": vat_status,
            "contract_vat": contract_vat,
            "cqp_vat": cqp_vat,
        },
        "untaxed_check": {
            "status": untaxed_status,
            "contract_amt": contract_untaxed,
            "cqp_amt": cqp_untaxed,
            "diff": round(untaxed_diff, 4),
        },
        "tax_included_check": {
            "status": tax_status,
            "contract_amt": contract_tax_incl,
            "cqp_amt": cqp_tax_incl,
            "diff": round(tax_diff, 4),
            "is_rounding": is_rounding,
        },
        "is_blocker": is_blocker,
    }


# ---------------------------------------------------------------------------
# Module 9: Blocker Classifier
# ---------------------------------------------------------------------------

def classify_findings(
    incoterm_result: Dict[str, Any],
    consistency_results: List[Dict[str, Any]],
    warranty_result: Dict[str, Any],
    config_result: Dict[str, Any],
    financial_result: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    blockers: List[Dict[str, Any]] = []
    non_blockers: List[Dict[str, Any]] = []

    if incoterm_result.get("is_blocker"):
        blockers.append({
            "type": "incoterm",
            "detail": incoterm_result.get("notes", "Incoterm inconsistency"),
        })

    for check in consistency_results:
        if check.get("is_blocker"):
            blockers.append({
                "type": check["check_name"],
                "detail": check["detail"],
            })
        elif check.get("status") in ("MISMATCH", "WARNING"):
            non_blockers.append({
                "type": check["check_name"],
                "detail": check["detail"],
            })

    if warranty_result.get("is_blocker"):
        blockers.append({
            "type": "warranty",
            "detail": warranty_result.get("detail", "Warranty inconsistency"),
        })

    if not config_result.get("overall_consistent") and config_result.get("blocker_mismatches"):
        blockers.append({
            "type": "config_mismatch",
            "detail": f"Configuration codes missing in TA: {config_result['blocker_mismatches']}",
        })

    if financial_result.get("is_blocker"):
        blockers.append({
            "type": "financial",
            "detail": _format_financial_blocker(financial_result),
        })
    elif financial_result.get("tax_included_check", {}).get("is_rounding"):
        non_blockers.append({
            "type": "rounding",
            "detail": f"含税金额差异 {financial_result['tax_included_check']['diff']} RMB (舍入误差)",
        })

    return blockers, non_blockers


def _format_financial_blocker(fin_result: Dict) -> str:
    parts = []
    for key, check in fin_result.items():
        if isinstance(check, dict) and check.get("status") == "MISMATCH":
            parts.append(f"{key}: {check}")
    return "; ".join(parts) if parts else "财务校验不通过"


# ---------------------------------------------------------------------------
# Module 10: BT09 Generator
# ---------------------------------------------------------------------------

def generate_bt09(
    contract_fields: Dict[str, Any],
    cqp_fields: Dict[str, Any],
    incoterm_result: Dict[str, Any],
    customer_db_path: str = None,
    template_path: str = None,
) -> str:
    incoterm = incoterm_result.get("conclusion", "UNDETERMINED")

    if incoterm == "EXW":
        ship_to_name = contract_fields.get("buyer_name", "待确认")
        ship_to_address = contract_fields.get("buyer_address", "待确认")
        ship_to_note = "EXW Shanghai, 客户自提"
    else:
        ship_to_name = contract_fields.get("buyer_name", "待确认")
        ship_to_address = contract_fields.get("delivery_location", "待确认")
        ship_to_note = ""

    if incoterm == "EXW":
        incoterm2 = "Shanghai"
    else:
        dest = contract_fields.get("delivery_location", "")
        if not dest:
            dest = _extract_destination_city(cqp_fields.get("delivery_term", ""))
        incoterm2 = dest or "MANUAL_CONFIRM_REQUIRED"

    delivery_times = contract_fields.get("delivery_time", [])
    delivery_str = "; ".join(
        f"{t.get('model', 'all')}: {t.get('weeks', '?')} weeks"
        for t in delivery_times
    ) if delivery_times else "待确认"

    payment = contract_fields.get("payment_terms_annex2", "")
    if payment:
        payment_summary = payment[:300] + ("..." if len(payment) > 300 else "")
    else:
        payment_summary = "待确认"

    lines = [
        "=" * 60,
        "BT09 邮件草稿 - ABB Robot Sales Contract Review",
        "=" * 60,
        "",
        f"Buyer: {contract_fields.get('buyer_name', '待确认')}",
        f"Quantity & Models: {', '.join(m['model'] for m in contract_fields.get('robot_models', [])) or '待确认'}",
        f"Contract Number: {contract_fields.get('contract_number', '待确认')}",
        f"PM / Sales: {contract_fields.get('pm', '待确认')} / {contract_fields.get('sales_person', '待确认')}",
        f"CQP Number: {cqp_fields.get('cqp_number', '待确认')}",
        f"Delivery Time (from Contract): {delivery_str}",
        f"Payment Terms (from Annex 2):",
        f"  {payment_summary}",
        "",
        f"Ship-to:",
        f"  Name: {ship_to_name}",
        f"  Address: {ship_to_address}",
        f"  Note: {ship_to_note or 'N/A'}",
        "",
        f"Incoterm 1: {incoterm}",
        f"Incoterm 2: {incoterm2}",
        f"End Customer: {contract_fields.get('end_customer_name', '待确认')}",
        f"End Customer Address: {contract_fields.get('end_customer_address', '待确认')}",
        "",
        f"Customer ID / GIS: 待确认 (需要客户数据库查询)",
        f"GM / NM: 待确认",
        "",
        "=" * 60,
    ]
    return "\n".join(lines)


def _extract_destination_city(delivery_term: str) -> str:
    m = re.search(r'(?:DDP|EXW)\s+(\S+)', delivery_term, re.IGNORECASE)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Module 11: Report Formatter
# ---------------------------------------------------------------------------

def format_report(
    conclusion: str,
    file_map: Dict[str, Any],
    contract_data: Dict[str, Any],
    cqp_data: Dict[str, Any],
    ta_data: Dict[str, Any],
    incoterm_result: Dict[str, Any],
    consistency_results: List[Dict[str, Any]],
    warranty_result: Dict[str, Any],
    config_result: Dict[str, Any],
    financial_result: Dict[str, Any],
    blockers: List[Dict[str, Any]],
    non_blockers: List[Dict[str, Any]],
    bt09_draft: Optional[str],
    review_items: Optional[List[Dict[str, Any]]] = None,
    llm_review: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Format final structured output as JSON-compatible dict."""
    contract_info = file_map.get("contract", {})
    cqp_info = file_map.get("cqp", {})
    ta_info = file_map.get("ta", {})

    def _safe_file_info(info: dict) -> dict:
        source = info.get("source")
        return {
            "status": info.get("status", "not_found"),
            "source_path": source.filepath if source else "",
            "page_count": len(source.pages) if source else 0,
        }

    # Strip non-serializable evidence from extracted data
    def _strip_evidence(data: dict) -> dict:
        return {k: v for k, v in data.items() if k != "_evidence"}

    report: Dict[str, Any] = {
        "conclusion": conclusion,
        "source_recognition": {
            "contract": _safe_file_info(contract_info),
            "cqp": _safe_file_info(cqp_info),
            "ta": _safe_file_info(ta_info),
        },
        "extracted_data": {
            "contract": _strip_evidence(contract_data),
            "cqp": _strip_evidence(cqp_data),
            "ta": _strip_evidence(ta_data),
        },
        "incoterm": {
            "conclusion": incoterm_result.get("conclusion", "UNDETERMINED"),
            "contract_evidence": incoterm_result.get("contract_evidence", ""),
            "cqp_evidence": incoterm_result.get("cqp_evidence", ""),
            "consistent": incoterm_result.get("consistent", True),
        },
        "key_checks": consistency_results,
        "blockers": blockers,
        "non_blockers": non_blockers,
        "warranty": warranty_result,
        "configuration": {
            "overall_consistent": config_result.get("overall_consistent", True),
            "blocker_mismatches": config_result.get("blocker_mismatches", []),
            "details": config_result.get("models_compared", []),
        },
        "financial": financial_result,
        "bt09_draft": bt09_draft,
        "llm_review": llm_review,
        "review_items": review_items or [],
    }
    return report


# ---------------------------------------------------------------------------
# Module 12: Main Orchestrator
# ---------------------------------------------------------------------------

def run_review(
    pdf_paths: List[str],
    customer_db_path: str = None,
    template_path: str = None,
    file_roles: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run the full contract review workflow.

    Args:
        pdf_paths: List of PDF file paths.
        file_roles: Optional dict mapping role (contract/cqp/ta) to file path.
                    When provided, the file at that path is treated as that role.
    """
    # Step 1: Parse all PDFs with evidence
    parsed_files = [parse_pdf(p) for p in pdf_paths]

    # Resolve file_roles: build mapping from role to parsed PDF
    role_to_pf: Dict[str, ParsedPDF] = {}
    if file_roles:
        for role, path in file_roles.items():
            for pf in parsed_files:
                if os.path.abspath(pf.filepath) == os.path.abspath(path):
                    role_to_pf[role] = pf
                    break

    # Step 2: Recognize files (auto-detect OR use explicit roles)
    file_map: Dict[str, Any] = {
        "contract": {"source": None, "status": "not_found"},
        "cqp": {"source": None, "status": "not_found"},
        "ta": {"source": None, "status": "not_found"},
    }

    if role_to_pf:
        # Use explicit roles
        for role in ("contract", "cqp", "ta"):
            if role in role_to_pf:
                file_map[role] = {"source": role_to_pf[role], "status": "found"}
        # Check for embedded TA
        contract_pf = role_to_pf.get("contract")
        if contract_pf and "ta" not in role_to_pf:
            ta_pages = _find_ta_in_contract(contract_pf)
            if ta_pages:
                file_map["ta"] = {"source": _make_virtual_pdf(ta_pages, contract_pf.filepath + "_TA"), "status": "embedded"}
    else:
        # Auto-detect
        file_map = recognize_files(parsed_files)

    # Step 3: Extract structured data
    contract_data = extract_contract(file_map["contract"])
    cqp_data = extract_cqp(file_map["cqp"])
    ta_data = extract_ta(file_map["ta"])

    # Get parsed PDFs for evidence
    contract_pf: Optional[ParsedPDF] = file_map["contract"].get("source")
    cqp_pf: Optional[ParsedPDF] = file_map["cqp"].get("source")
    ta_pf: Optional[ParsedPDF] = file_map["ta"].get("source")

    # Step 4: Resolve Incoterm
    incoterm_result = resolve_incoterm(contract_data, cqp_data)

    # Step 5: Run consistency checks (with evidence)
    consistency_results, review_items = run_consistency_checks(
        contract_data, cqp_data, ta_data,
        contract_pf, cqp_pf, ta_pf,
    )

    # Step 6: Warranty check
    warranty_result = check_warranty(contract_data, cqp_data, ta_data)

    # Step 7: Config comparison
    cqp_configs = cqp_data.get("configurations", [])
    ta_configs = ta_data.get("configurations", [])
    config_result = compare_configurations(cqp_configs, ta_configs)

    # Step 8: Financial check
    financial_result = check_financials(contract_data, cqp_data)

    # Step 9: Classify blockers
    blockers, non_blockers = classify_findings(
        incoterm_result, consistency_results, warranty_result,
        config_result, financial_result
    )

    # Step 10: Generate BT09 if no blockers
    bt09_draft = None
    if not blockers:
        bt09_draft = generate_bt09(contract_data, cqp_data, incoterm_result, customer_db_path)

    # Step 11: Determine conclusion
    if blockers:
        conclusion = "Blocked"
    elif non_blockers:
        conclusion = "Pass with notes"
    else:
        conclusion = "Pass"

    # Add incoterm review item
    incoterm_item = {
        "id": _make_item_id("贸易术语"),
        "category": "commercial",
        "title": "贸易术语",
        "status": "MISMATCH" if incoterm_result.get("is_blocker") else "PASS",
        "severity": "blocker" if incoterm_result.get("is_blocker") else "info",
        "summary": f"结论: {incoterm_result.get('conclusion', 'UNDETERMINED')} / Contract: {incoterm_result.get('contract_evidence', '')} / CQP: {incoterm_result.get('cqp_evidence', '')}",
        "values": {
            "conclusion": incoterm_result.get("conclusion", "UNDETERMINED"),
            "contract": incoterm_result.get("contract_evidence", ""),
            "cqp": incoterm_result.get("cqp_evidence", ""),
        },
        "evidence": [],
    }
    review_items.append(incoterm_item)

    # Add missing fields check
    missing_fields = _check_missing_fields(contract_data, cqp_data, ta_data)
    if missing_fields:
        review_items.append(missing_fields)

    # Step 10.5: Run DeepSeek LLM review
    llm_review = None
    try:
        llm_review = run_llm_contract_review(
            contract_data=contract_data,
            cqp_data=cqp_data,
            ta_data=ta_data,
            incoterm_result=incoterm_result,
            consistency_results=consistency_results,
            warranty_result=warranty_result,
            config_result=config_result,
            financial_result=financial_result,
        )
    except Exception:
        llm_review = {
            "error": "LLM 审核暂时不可用",
            "overall_assessment": "Unknown",
            "summary": "（AI 审核未完成，请稍后重试或检查 API Key 配置）",
        }

    # Step 12: Format report
    report = format_report(
        conclusion=conclusion,
        file_map=file_map,
        contract_data=contract_data,
        cqp_data=cqp_data,
        ta_data=ta_data,
        incoterm_result=incoterm_result,
        consistency_results=consistency_results,
        warranty_result=warranty_result,
        config_result=config_result,
        financial_result=financial_result,
        blockers=blockers,
        non_blockers=non_blockers,
        bt09_draft=bt09_draft,
        review_items=review_items,
        llm_review=llm_review,
    )

    return report


def _check_missing_fields(
    contract_data: Dict[str, Any],
    cqp_data: Dict[str, Any],
    ta_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Generate a review item for missing critical fields."""
    critical_contract = ["contract_number", "buyer_name", "seller_name", "total_qty"]
    critical_cqp = ["cqp_number", "customer_name"]

    missing = []
    for field in critical_contract:
        if not contract_data.get(field):
            missing.append(f"Contract.{field}")
    for field in critical_cqp:
        if not cqp_data.get(field):
            missing.append(f"CQP.{field}")

    if not missing:
        return None

    return {
        "id": "missing_fields",
        "category": "compliance",
        "title": "缺少字段",
        "status": "UNDETERMINED",
        "severity": "warning",
        "summary": f"以下关键字段未提取到: {', '.join(missing)}",
        "values": {
            "missing": ", ".join(missing),
        },
        "evidence": [],
    }