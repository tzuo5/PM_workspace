# -*- coding: utf-8 -*-
"""Deterministic rule engine for document cross-checking.

Architecture:
- Each rule is a function that takes extracted fields and returns a CheckResult.
- Rules are organized into categories.
- The rule engine orchestrates all rules and produces the final result.
- LLM is used only for extraction/normalization/semantic comparison, not for final judgment.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import normalizer as norm


class CheckResult:
    """Result of a single rule check."""
    __slots__ = ("rule_id", "category", "label", "status", "is_blocker",
                 "summary", "details", "contract_value", "cqp_value",
                 "ta_value", "confidence", "evidence_refs")

    def __init__(self, rule_id: str, category: str = "GENERAL", label: str = "",
                 status: str = "UNKNOWN", is_blocker: bool = False,
                 summary: str = "", details: str = "",
                 contract_value: str = "", cqp_value: str = "",
                 ta_value: str = "", confidence: float = 0.0,
                 evidence_refs: List[str] = None):
        self.rule_id = rule_id
        self.category = category
        self.label = label
        self.status = status  # PASS, REVIEW, FAIL, UNKNOWN, NOT_APPLICABLE
        self.is_blocker = is_blocker
        self.summary = summary
        self.details = details
        self.contract_value = contract_value
        self.cqp_value = cqp_value
        self.ta_value = ta_value
        self.confidence = confidence
        self.evidence_refs = evidence_refs or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "label": self.label,
            "status": self.status,
            "is_blocker": self.is_blocker,
            "summary": self.summary,
            "details": self.details,
            "values": {
                "contract": self.contract_value,
                "cqp": self.cqp_value,
                "ta": self.ta_value,
            },
            "confidence": self.confidence,
            "evidence_refs": self.evidence_refs,
        }


class ExtractedData:
    """Container for all extracted fields from documents."""
    def __init__(self):
        self.contract_fields: Dict[str, Any] = {}
        self.cqp_fields: Dict[str, Any] = {}
        self.ta_fields: Dict[str, Any] = {}
        self.contract_spans: Any = None  # PDFParseResult
        self.cqp_spans: Any = None
        self.ta_spans: Any = None
        self.has_contract: bool = False
        self.has_cqp: bool = False
        self.has_ta: bool = False
        self.ta_is_embedded: bool = False
        self.ta_contract_pages: Tuple[int, int] = (0, 0)  # start, end page in contract
        self.all_evidence: List[str] = []

    def get(self, source: str, field: str, default: str = "") -> str:
        """Get field value from a source ('contract', 'cqp', 'ta')."""
        if source == "contract":
            return self.contract_fields.get(field, {}).get("value", default) if isinstance(self.contract_fields.get(field), dict) else default
        elif source == "cqp":
            return self.cqp_fields.get(field, {}).get("value", default) if isinstance(self.cqp_fields.get(field), dict) else default
        elif source == "ta":
            return self.ta_fields.get(field, {}).get("value", default) if isinstance(self.ta_fields.get(field), dict) else default
        return default

    def get_ev_refs(self, source: str, field: str) -> List[str]:
        """Get evidence refs for a field."""
        if source == "contract":
            return self.contract_fields.get(field, {}).get("evidence_refs", []) if isinstance(self.contract_fields.get(field), dict) else []
        elif source == "cqp":
            return self.cqp_fields.get(field, {}).get("evidence_refs", []) if isinstance(self.cqp_fields.get(field), dict) else []
        elif source == "ta":
            return self.ta_fields.get(field, {}).get("evidence_refs", []) if isinstance(self.ta_fields.get(field), dict) else []
        return []


# ============================================================
# SOURCE RECOGNITION RULES
# ============================================================

def rule_file_contract_present(data: ExtractedData) -> CheckResult:
    if data.has_contract:
        return CheckResult("FILE_CONTRACT_PRESENT", "SOURCE_RECOGNITION", "Contract File",
                           "PASS", False, "Contract file found", "")
    return CheckResult("FILE_CONTRACT_PRESENT", "SOURCE_RECOGNITION", "Contract File",
                       "FAIL", True, "Contract file missing",
                       "No contract PDF uploaded. Contract is required.",
                       confidence=1.0)


def rule_file_cqp_present(data: ExtractedData) -> CheckResult:
    if data.has_cqp:
        return CheckResult("FILE_CQP_PRESENT", "SOURCE_RECOGNITION", "CQP File",
                           "PASS", False, "CQP file found", "")
    return CheckResult("FILE_CQP_PRESENT", "SOURCE_RECOGNITION", "CQP File",
                       "FAIL", True, "CQP file missing",
                       "No CQP PDF uploaded. CQP is required.",
                       confidence=1.0)


def rule_file_ta_present(data: ExtractedData) -> CheckResult:
    if data.has_ta or data.ta_is_embedded:
        if data.ta_is_embedded:
            return CheckResult("FILE_TA_PRESENT_OR_EMBEDDED", "SOURCE_RECOGNITION",
                               "TA File", "PASS", False,
                               f"TA embedded in Contract (pages {data.ta_contract_pages[0]}-{data.ta_contract_pages[1]})", "")
        return CheckResult("FILE_TA_PRESENT_OR_EMBEDDED", "SOURCE_RECOGNITION", "TA File",
                           "PASS", False, "TA found as independent file", "")
    return CheckResult("FILE_TA_PRESENT_OR_EMBEDDED", "SOURCE_RECOGNITION", "TA File",
                       "UNKNOWN", False,
                       "TA not found as independent file or embedded section. "
                       "Please upload TA or verify contract contains TA annex.",
                       confidence=0.5)


# ============================================================
# SELLER ENTITY RULES
# ============================================================

def rule_seller_prefix_match(data: ExtractedData) -> CheckResult:
    contract_number = data.get("contract", "contract_number", "")
    seller_entity = data.get("contract", "seller_legal_entity", "")

    prefix = norm.detect_contract_prefix(contract_number)

    if not prefix:
        return CheckResult("SELLER_ENTITY_PREFIX_MATCH", "COMMERCIAL", "Seller Entity",
                           "UNKNOWN", False, "Cannot determine contract prefix",
                           f"Contract number '{contract_number}' does not have M or K prefix.",
                           contract_value=seller_entity)

    expected_seller = norm.SELLER_PREFIX_MAP.get(prefix, "")
    normalized_expected = norm.normalize_company_name(expected_seller)
    normalized_actual = norm.normalize_company_name(seller_entity)

    if not seller_entity:
        return CheckResult("SELLER_ENTITY_PREFIX_MATCH", "COMMERCIAL", "Seller Entity",
                           "UNKNOWN", False,
                           "Seller entity not extracted",
                           f"Expected {prefix}-prefix seller: {expected_seller}",
                           contract_value=seller_entity,
                           evidence_refs=data.get_ev_refs("contract", "seller_legal_entity"))

    if normalized_expected == normalized_actual:
        return CheckResult("SELLER_ENTITY_PREFIX_MATCH", "COMMERCIAL", "Seller Entity",
                           "PASS", False,
                           f"Seller entity matches {prefix}-prefix",
                           f"Contract {contract_number} -> {prefix} prefix -> {expected_seller}",
                           contract_value=seller_entity,
                           confidence=0.95,
                           evidence_refs=data.get_ev_refs("contract", "seller_legal_entity"))

    # Partial match - both ABB entities
    if "abb" in normalized_actual or "abb" in normalized_expected:
        return CheckResult("SELLER_ENTITY_PREFIX_MATCH", "COMMERCIAL", "Seller Entity",
                           "REVIEW", True,
                           f"Seller entity mismatch: {seller_entity} vs expected {expected_seller}",
                           f"Contract has {prefix}-prefix but seller entity is {seller_entity}",
                           contract_value=seller_entity,
                           evidence_refs=data.get_ev_refs("contract", "seller_legal_entity"))

    return CheckResult("SELLER_ENTITY_PREFIX_MATCH", "COMMERCIAL", "Seller Entity",
                       "FAIL", True,
                       f"Seller entity mismatch",
                       f"Expected {expected_seller} for {prefix}-prefix, got {seller_entity}",
                       contract_value=seller_entity,
                       evidence_refs=data.get_ev_refs("contract", "seller_legal_entity"))


# ============================================================
# INCOTERM RULES
# ============================================================

def _determine_incoterm(data: ExtractedData) -> Tuple[str, str, float]:
    """Determine incoterm from contract data using checkbox priority.

    Returns (incoterm, evidence_description, confidence).

    Priority:
    1. Explicit checkbox symbol detection (☑, ☒, ✓, ■) near option text
    2. detect_checkbox_from_text result (single best-guess)
    3. Contract incoterm field text
    4. Delivery location heuristics
    5. Fallback CQP cross-reference
    6. Full text presence
    """
    contract_text = ""
    if data.contract_spans:
        for page in data.contract_spans.pages:
            contract_text += page.full_text + "\n"

    contract_incoterm = data.get("contract", "incoterm", "")
    delivery_location = data.get("contract", "delivery_location", "")
    cqp_incoterm = data.get("cqp", "incoterm", "").upper().strip()

    # Priority 1: Check for explicit checkbox symbols in spans
    # This is the most reliable - actual checkbox marks from the PDF
    checkbox_results = []
    if data.contract_spans:
        from . import pdf_parser
        for page in data.contract_spans.pages:
            checkboxes = pdf_parser.find_checkbox_regions(page.spans, page.page_width, page.page_height)
            checkbox_results.extend(checkboxes)

    # Separate selected vs unselected symbol checkboxes
    symbol_ddp = None
    symbol_exw = None
    for c in checkbox_results:
        opt = str(c.get("option_text", "")).lower()
        ctx = str(c.get("context", "")).lower()
        combined = opt + " " + ctx

        is_ddp_related = ("ddp" in opt or "ddp" in ctx or
                          "到货价" in combined or "到货" in combined or
                          "买方" in combined)
        is_exw_related = ("exw" in opt or "exw" in ctx or
                          "出厂价" in combined or "出厂" in combined or
                          "卖方工厂" in combined)

        if is_ddp_related and not is_exw_related:
            symbol_ddp = c
        elif is_exw_related and not is_ddp_related:
            symbol_exw = c

    # If both have symbols: only the SELECTED one wins
    if symbol_ddp and symbol_ddp.get("selected"):
        return ("DDP",
                f"Checkbox symbol selected: {symbol_ddp.get('raw_text', '')} near DDP option '{symbol_ddp.get('option_text', '')}'",
                0.95)
    if symbol_exw and symbol_exw.get("selected"):
        return ("EXW",
                f"Checkbox symbol selected: {symbol_exw.get('raw_text', '')} near EXW option '{symbol_exw.get('option_text', '')}'",
                0.95)

    # Priority 2: detect_checkbox_from_text (now returns single best-guess)
    if data.contract_spans:
        for page in data.contract_spans.pages:
            text_checkboxes = pdf_parser.detect_checkbox_from_text(page.full_text)
            for tc in text_checkboxes:
                if tc.get("selected"):
                    opt = str(tc.get("option_text", "")).lower()
                    if "ddp" in opt or "到货价" in opt:
                        return ("DDP", f"Text checkbox detection: {tc.get('method', '')} - {tc.get('match', '')}", 0.85)
                    if "exw" in opt or "出厂价" in opt:
                        return ("EXW", f"Text checkbox detection: {tc.get('method', '')} - {tc.get('match', '')}", 0.85)

    # Priority 3: Contract transport clause explicit text
    if contract_incoterm:
        incoterm_upper = contract_incoterm.upper().strip()
        if incoterm_upper == "DDP" or incoterm_upper.startswith("DDP"):
            return ("DDP", f"Contract incoterm field: {contract_incoterm}", 0.90)
        if incoterm_upper == "EXW" or incoterm_upper.startswith("EXW"):
            return ("EXW", f"Contract incoterm field: {contract_incoterm}", 0.90)

    # Priority 4: Delivery location heuristics
    if delivery_location and len(delivery_location) > 3:
        ddp_keywords = ["马陆", "嘉定", "博学路", "上海", "buyer", "买方", "到货"]
        if any(k in delivery_location for k in ddp_keywords):
            return ("DDP", f"Delivery location suggests DDP: {delivery_location}", 0.70)

    # Priority 5: Fallback - CQP cross-reference
    cqp_is_ddp = "DDP" in cqp_incoterm
    cqp_is_exw = "EXW" in cqp_incoterm
    dl_empty = not delivery_location or len(delivery_location) < 3

    if cqp_is_ddp and not cqp_is_exw:
        return ("DDP", f"CQP shows DDP, fallback applied (delivery location: {'empty' if dl_empty else 'filled'})", 0.65)
    if cqp_is_exw and not cqp_is_ddp:
        return ("EXW", f"CQP shows EXW, fallback applied (delivery location: {'empty' if dl_empty else 'filled'})", 0.65)

    # Priority 6: Full text presence (last resort)
    contract_lower = contract_text.lower()
    has_ddp_text = "ddp" in contract_lower or "到货价" in contract_text
    has_exw_text = "exw" in contract_lower or "出厂价" in contract_text

    if has_ddp_text and not has_exw_text:
        return ("DDP", "DDP mentioned in contract text (no EXW found)", 0.55)
    if has_exw_text and not has_ddp_text:
        return ("EXW", "EXW mentioned in contract text (no DDP found)", 0.55)

    return ("UNKNOWN", "Cannot determine incoterm automatically", 0.30)


def rule_incoterm_contract_determination(data: ExtractedData) -> CheckResult:
    incoterm, evidence, confidence = _determine_incoterm(data)
    if incoterm == "UNKNOWN":
        return CheckResult("INCOTERM_CONTRACT_DETERMINATION", "COMMERCIAL", "Incoterm (Contract)",
                           "UNKNOWN", False,
                           "Cannot determine incoterm from contract",
                           evidence, confidence=confidence)
    return CheckResult("INCOTERM_CONTRACT_DETERMINATION", "COMMERCIAL", "Incoterm (Contract)",
                       "PASS" if confidence >= 0.70 else "REVIEW", False,
                       f"Contract incoterm: {incoterm}",
                       evidence, contract_value=incoterm,
                       confidence=confidence)


def rule_incoterm_cqp_determination(data: ExtractedData) -> CheckResult:
    cqp_incoterm = data.get("cqp", "incoterm", "")
    if not cqp_incoterm:
        return CheckResult("INCOTERM_CQP_DETERMINATION", "COMMERCIAL", "Incoterm (CQP)",
                           "UNKNOWN", False, "No incoterm found in CQP",
                           confidence=0.0)
    incoterm_upper = cqp_incoterm.upper().strip()
    if "DDP" in incoterm_upper:
        return CheckResult("INCOTERM_CQP_DETERMINATION", "COMMERCIAL", "Incoterm (CQP)",
                           "PASS", False, f"CQP incoterm: DDP",
                           cqp_value=cqp_incoterm, confidence=0.85)
    if "EXW" in incoterm_upper:
        return CheckResult("INCOTERM_CQP_DETERMINATION", "COMMERCIAL", "Incoterm (CQP)",
                           "PASS", False, f"CQP incoterm: EXW",
                           cqp_value=cqp_incoterm, confidence=0.85)
    return CheckResult("INCOTERM_CQP_DETERMINATION", "COMMERCIAL", "Incoterm (CQP)",
                       "REVIEW", False,
                       f"CQP incoterm ambiguous: {cqp_incoterm}",
                       cqp_value=cqp_incoterm, confidence=0.5)


def rule_incoterm_consistency(data: ExtractedData) -> CheckResult:
    contract_incoterm, _, _ = _determine_incoterm(data)
    cqp_incoterm = data.get("cqp", "incoterm", "").upper().strip()
    cqp_ddp = "DDP" in cqp_incoterm
    cqp_exw = "EXW" in cqp_incoterm

    if contract_incoterm == "UNKNOWN" and not cqp_incoterm:
        return CheckResult("INCOTERM_CONSISTENCY", "COMMERCIAL", "Incoterm Consistency",
                           "UNKNOWN", True,
                           "Cannot determine incoterm from either document",
                           confidence=0.0)

    if contract_incoterm == "DDP" and cqp_ddp:
        return CheckResult("INCOTERM_CONSISTENCY", "COMMERCIAL", "Incoterm Consistency",
                           "PASS", False, "Both Contract and CQP: DDP",
                           contract_value="DDP", cqp_value=cqp_incoterm, confidence=0.90)

    if contract_incoterm == "EXW" and cqp_exw:
        return CheckResult("INCOTERM_CONSISTENCY", "COMMERCIAL", "Incoterm Consistency",
                           "PASS", False, "Both Contract and CQP: EXW",
                           contract_value="EXW", cqp_value=cqp_incoterm, confidence=0.90)

    if contract_incoterm in ("DDP", "EXW") and cqp_incoterm and not (cqp_ddp or cqp_exw):
        return CheckResult("INCOTERM_CONSISTENCY", "COMMERCIAL", "Incoterm Consistency",
                           "REVIEW", False,
                           f"Contract: {contract_incoterm}, CQP: {cqp_incoterm} (ambiguous)",
                           contract_value=contract_incoterm, cqp_value=cqp_incoterm,
                           confidence=0.40)

    if contract_incoterm != "UNKNOWN" and cqp_incoterm and (
        (contract_incoterm == "DDP" and cqp_exw) or (contract_incoterm == "EXW" and cqp_ddp)
    ):
        return CheckResult("INCOTERM_CONSISTENCY", "COMMERCIAL", "Incoterm Consistency",
                           "FAIL", True,
                           f"Contract {contract_incoterm} vs CQP {cqp_incoterm} - INCONSISTENT!",
                           f"Incoterm mismatch between Contract and CQP.",
                           contract_value=contract_incoterm, cqp_value=cqp_incoterm,
                           confidence=0.95)

    return CheckResult("INCOTERM_CONSISTENCY", "COMMERCIAL", "Incoterm Consistency",
                       "REVIEW", False,
                       f"Contract: {contract_incoterm}, CQP: {cqp_incoterm or 'N/A'}",
                       contract_value=contract_incoterm, cqp_value=cqp_incoterm,
                       confidence=0.5)


def rule_incoterm2_location(data: ExtractedData) -> CheckResult:
    """Determine Incoterm 2 location.
    
    Rules:
    - EXW: "Shanghai" (ABB factory default)
    - DDP: MUST use the destination explicitly stated in the contract.
      NEVER default to "Shanghai" for DDP.
    - If CQP explicitly states a location (e.g. "DDP Xiamen"), use that.
    - If neither source has a location, mark as REVIEW.
    """
    contract_incoterm, _, _ = _determine_incoterm(data)
    cqp_incoterm_full = data.get("cqp", "incoterm", "")
    delivery_location = data.get("contract", "delivery_location", "")
    ship_to_addr = data.get("contract", "ship_to_address", "")

    # CQP explicit wording (e.g. "DDP Shanghai", "EXW Shenzhen")
    if cqp_incoterm_full:
        parts = cqp_incoterm_full.strip().split()
        if len(parts) > 1:
            location = " ".join(parts[1:])
            return CheckResult("INCOTERM_2_LOCATION", "COMMERCIAL", "Incoterm Location",
                               "PASS", False,
                               f"Incoterm location from CQP: {location}",
                               cqp_value=location, confidence=0.85)

    # DDP: destination from contract
    if contract_incoterm == "DDP":
        location = delivery_location or ship_to_addr
        if location and len(location) > 2:
            return CheckResult("INCOTERM_2_LOCATION", "COMMERCIAL", "Incoterm Location",
                               "PASS", False,
                               f"DDP destination: {location}",
                               contract_value=location, confidence=0.75)
        # DDP without destination is a BLOCKER - must not default
        return CheckResult("INCOTERM_2_LOCATION", "COMMERCIAL", "Incoterm Location",
                           "REVIEW", True,
                           "DDP requires explicit destination. No delivery location or ship-to address found in contract. "
                           "DDP MUST NOT default to Shanghai.",
                           contract_value="MISSING",
                           confidence=0.40)

    # EXW: origin is always the seller's factory
    if contract_incoterm == "EXW":
        location = "Shanghai"  # ABB factory default for EXW
        if delivery_location and len(delivery_location) > 2:
            location = delivery_location
        return CheckResult("INCOTERM_2_LOCATION", "COMMERCIAL", "Incoterm Location",
                           "PASS", False,
                           f"EXW origin: {location}",
                           contract_value=location, confidence=0.70)

    return CheckResult("INCOTERM_2_LOCATION", "COMMERCIAL", "Incoterm Location",
                       "REVIEW", False, "Incoterm location requires manual confirmation",
                       confidence=0.40)


# ============================================================
# BASIC CONSISTENCY RULES
# ============================================================

def rule_contract_number_reference(data: ExtractedData) -> CheckResult:
    cn_contract = data.get("contract", "contract_number", "")
    cn_cqp = data.get("cqp", "contract_number", "")
    if not cn_contract or not cn_cqp:
        return CheckResult("CONTRACT_NUMBER_REFERENCE", "COMMERCIAL", "Contract Number",
                           "UNKNOWN", False, "Contract number not found in one or both documents",
                           contract_value=cn_contract, cqp_value=cn_cqp)
    if norm.compare_normalized(cn_contract, cn_cqp):
        return CheckResult("CONTRACT_NUMBER_REFERENCE", "COMMERCIAL", "Contract Number",
                           "PASS", False, f"Contract number matches: {cn_contract}",
                           contract_value=cn_contract, cqp_value=cn_cqp, confidence=0.98)
    return CheckResult("CONTRACT_NUMBER_REFERENCE", "COMMERCIAL", "Contract Number",
                       "FAIL", True,
                       f"Contract number mismatch: {cn_contract} vs {cn_cqp}",
                       contract_value=cn_contract, cqp_value=cn_cqp, confidence=0.95)


def rule_buyer_consistency(data: ExtractedData) -> CheckResult:
    buyer_c = data.get("contract", "buyer_legal_name", "")
    buyer_q = data.get("cqp", "buyer_legal_name", "")
    if not buyer_c or not buyer_q:
        return CheckResult("BUYER_CONSISTENCY", "COMMERCIAL", "Buyer",
                           "UNKNOWN", False, "Buyer not found in one or both documents",
                           contract_value=buyer_c, cqp_value=buyer_q)
    nc = norm.normalize_company_name(buyer_c)
    nq = norm.normalize_company_name(buyer_q)
    if nc == nq:
        return CheckResult("BUYER_CONSISTENCY", "COMMERCIAL", "Buyer",
                           "PASS", False, f"Buyer matches: {buyer_c}",
                           contract_value=buyer_c, cqp_value=buyer_q, confidence=0.93)
    # Check if one contains the other (e.g., "迅亚" vs "上海迅亚自动化...")
    if nc in nq or nq in nc:
        return CheckResult("BUYER_CONSISTENCY", "COMMERCIAL", "Buyer",
                           "REVIEW", False,
                           f"Buyer names are similar but not exact: {buyer_c} vs {buyer_q}",
                           contract_value=buyer_c, cqp_value=buyer_q, confidence=0.55)
    return CheckResult("BUYER_CONSISTENCY", "COMMERCIAL", "Buyer",
                       "REVIEW", False,
                       f"Buyer names differ: {buyer_c} vs {buyer_q}",
                       contract_value=buyer_c, cqp_value=buyer_q, confidence=0.35)


def rule_robot_model_consistency(data: ExtractedData) -> CheckResult:
    models_c = data.get("contract", "robot_models", "")
    models_q = data.get("cqp", "robot_models", "")
    if not models_c or not models_q:
        return CheckResult("ROBOT_MODEL_CONSISTENCY", "COMMERCIAL", "Robot Models",
                           "UNKNOWN", False, "Robot models not extracted",
                           contract_value=models_c, cqp_value=models_q)
    # Normalize and compare as lists
    if isinstance(models_c, str):
        models_c_list = [m.strip() for m in models_c.split(",") if m.strip()]
    else:
        models_c_list = []
    if isinstance(models_q, str):
        models_q_list = [m.strip() for m in models_q.split(",") if m.strip()]
    else:
        models_q_list = []

    n_c = set(norm.normalize_robot_model(m) for m in models_c_list)
    n_q = set(norm.normalize_robot_model(m) for m in models_q_list)

    if not n_c or not n_q:
        return CheckResult("ROBOT_MODEL_CONSISTENCY", "COMMERCIAL", "Robot Models",
                           "UNKNOWN", False, "Cannot normalize robot models",
                           contract_value=models_c, cqp_value=models_q)

    if n_c == n_q:
        return CheckResult("ROBOT_MODEL_CONSISTENCY", "COMMERCIAL", "Robot Models",
                           "PASS", False, f"Robot models match ({len(n_c)} models)",
                           contract_value=models_c, cqp_value=models_q, confidence=0.95)

    missing_c = n_q - n_c
    missing_q = n_c - n_q
    if missing_c or missing_q:
        return CheckResult("ROBOT_MODEL_CONSISTENCY", "COMMERCIAL", "Robot Models",
                           "FAIL", True,
                           f"Robot model mismatch. Only in CQP: {missing_c or 'none'}. Only in Contract: {missing_q or 'none'}",
                           contract_value=models_c, cqp_value=models_q, confidence=0.90)

    return CheckResult("ROBOT_MODEL_CONSISTENCY", "COMMERCIAL", "Robot Models",
                       "REVIEW", False, "Robot models partially matched",
                       contract_value=models_c, cqp_value=models_q, confidence=0.55)


def rule_robot_quantity_consistency(data: ExtractedData) -> CheckResult:
    qty_c = data.get("contract", "total_quantity", "")
    qty_q = data.get("cqp", "total_quantity", "")
    if not qty_c or not qty_q:
        return CheckResult("ROBOT_QUANTITY_CONSISTENCY", "COMMERCIAL", "Total Quantity",
                           "UNKNOWN", False, "Total quantity not extracted",
                           contract_value=qty_c, cqp_value=qty_q)
    try:
        n_c = int(str(qty_c).strip())
        n_q = int(str(qty_q).strip())
        if n_c == n_q:
            return CheckResult("ROBOT_QUANTITY_CONSISTENCY", "COMMERCIAL", "Total Quantity",
                               "PASS", False, f"Total quantity matches: {n_c}",
                               contract_value=str(n_c), cqp_value=str(n_q), confidence=0.97)
        return CheckResult("ROBOT_QUANTITY_CONSISTENCY", "COMMERCIAL", "Total Quantity",
                           "FAIL", True,
                           f"Quantity mismatch: Contract {n_c} vs CQP {n_q}",
                           contract_value=str(n_c), cqp_value=str(n_q), confidence=0.95)
    except ValueError:
        return CheckResult("ROBOT_QUANTITY_CONSISTENCY", "COMMERCIAL", "Total Quantity",
                           "REVIEW", False,
                           f"Cannot compare quantities: {qty_c} vs {qty_q}",
                           contract_value=qty_c, cqp_value=qty_q, confidence=0.3)


# ============================================================
# DELIVERY TIME
# ============================================================

def rule_delivery_time_consistency(data: ExtractedData) -> CheckResult:
    delivery_c = data.get("contract", "delivery_time", "")
    delivery_q = data.get("cqp", "delivery_time", "")
    if not delivery_c or not delivery_q:
        return CheckResult("DELIVERY_TIME_CONSISTENCY", "COMMERCIAL", "Delivery Time",
                           "UNKNOWN", False, "Delivery time not extracted",
                           contract_value=delivery_c, cqp_value=delivery_q)

    # Normalize for comparison
    n_c = norm.normalize_whitespace(delivery_c.lower())
    n_q = norm.normalize_whitespace(delivery_q.lower())

    if n_c == n_q:
        return CheckResult("DELIVERY_TIME_CONSISTENCY", "COMMERCIAL", "Delivery Time",
                           "PASS", False, "Delivery times match",
                           contract_value=delivery_c, cqp_value=delivery_q, confidence=0.90)

    # Extract weeks for comparison
    import re
    weeks_c = re.findall(r'(\d+)\s*(?:weeks|周|week)', n_c)
    weeks_q = re.findall(r'(\d+)\s*(?:weeks|周|week)', n_q)

    if weeks_c and weeks_q:
        combined_c = sum(int(w) for w in weeks_c)
        combined_q = sum(int(w) for w in weeks_q)
        if combined_c == combined_q:
            return CheckResult("DELIVERY_TIME_CONSISTENCY", "COMMERCIAL", "Delivery Time",
                               "PASS", False, "Total delivery weeks match",
                               contract_value=delivery_c, cqp_value=delivery_q, confidence=0.85)

    # Non-blocker: use contract as authority for BT09
    return CheckResult("DELIVERY_TIME_CONSISTENCY", "COMMERCIAL", "Delivery Time",
                       "REVIEW", False,
                       f"Delivery time differs. Contract: {delivery_c} | CQP: {delivery_q}. "
                       "BT09 will use Contract delivery schedule.",
                       contract_value=delivery_c, cqp_value=delivery_q, confidence=0.70)


# ============================================================
# WARRANTY RULES
# ============================================================

def rule_warranty_classification(data: ExtractedData) -> CheckResult:
    warranty_c = data.get("contract", "warranty_period", "")
    if not warranty_c:
        return CheckResult("WARRANTY_CONTRACT_EXTRACTION", "COMMERCIAL", "Warranty",
                           "UNKNOWN", False, "Warranty period not extracted from contract",
                           confidence=0.0)

    # Parse "18/12" or "24/12" pattern
    import re
    match = re.search(r'(\d+)\s*/\s*(\d+)', str(warranty_c))
    if match:
        months = int(match.group(1))
        base = int(match.group(2))
        classification = "Standard Warranty" if months <= 18 else "Extended Warranty"
        status = "PASS" if months <= 18 else "REVIEW"

        return CheckResult("WARRANTY_CLASSIFICATION", "COMMERCIAL", "Warranty Classification",
                           status, months > 18,
                           f"Contract warranty: {warranty_c} -> {classification}",
                           f"{months}/{base} months. {'Standard' if months <= 18 else 'Extended'} Warranty.",
                           contract_value=warranty_c, confidence=0.90)

    return CheckResult("WARRANTY_CLASSIFICATION", "COMMERCIAL", "Warranty Classification",
                       "UNKNOWN", False,
                       f"Cannot classify warranty: {warranty_c}",
                       contract_value=warranty_c, confidence=0.30)


def rule_warranty_consistency(data: ExtractedData) -> CheckResult:
    warranty_c = data.get("contract", "warranty_period", "")
    warranty_q = data.get("cqp", "warranty_period", "")
    classification_c = data.get("contract", "warranty_classification", "")

    if not warranty_c:
        return CheckResult("WARRANTY_CQP_CONSISTENCY", "COMMERCIAL", "Warranty Consistency",
                           "UNKNOWN", False, "No contract warranty data",
                           contract_value=warranty_c, cqp_value=warranty_q)

    if not warranty_q:
        return CheckResult("WARRANTY_CQP_CONSISTENCY", "COMMERCIAL", "Warranty Consistency",
                           "REVIEW", False, "No CQP warranty data for comparison",
                           contract_value=warranty_c)

    # Compare normalized
    c_text = norm.normalize_whitespace(str(warranty_c).lower().strip())
    q_text = norm.normalize_whitespace(str(warranty_q).lower().strip())

    if c_text == q_text:
        return CheckResult("WARRANTY_CQP_CONSISTENCY", "COMMERCIAL", "Warranty Consistency",
                           "PASS", False, "Contract and CQP warranty terms match",
                           contract_value=warranty_c, cqp_value=warranty_q, confidence=0.90)

    return CheckResult("WARRANTY_CQP_CONSISTENCY", "COMMERCIAL", "Warranty Consistency",
                       "REVIEW", False,
                       f"Warranty differs: Contract {warranty_c}, CQP {warranty_q}",
                       contract_value=warranty_c, cqp_value=warranty_q, confidence=0.50)


# ============================================================
# FINANCIAL RULES
# ============================================================

def rule_amount_consistency(field_name: str, label: str) -> callable:
    """Factory for amount comparison rules."""
    def _check(data: ExtractedData) -> CheckResult:
        val_c = data.get("contract", field_name, "")
        val_q = data.get("cqp", field_name, "")
        if not val_c or not val_q:
            return CheckResult(f"{field_name.upper()}_CONSISTENCY", "FINANCIAL", label,
                               "UNKNOWN", False, f"{label} not extracted",
                               contract_value=val_c, cqp_value=val_q)
        try:
            amt_c = Decimal(norm.normalize_money(val_c))
            amt_q = Decimal(norm.normalize_money(val_q))
            diff = abs(amt_c - amt_q)

            if diff < Decimal("1.00"):
                status = "PASS" if diff == 0 else "PASS"
                summary = f"{label}: Contract {val_c}, CQP {val_q} (match)"
                if diff > 0:
                    summary += f" (diff: {diff})"
                    status = "REVIEW"
                return CheckResult(f"{field_name.upper()}_CONSISTENCY", "FINANCIAL", label,
                                   status, diff >= Decimal("1.00"),
                                   summary,
                                   contract_value=str(amt_c), cqp_value=str(amt_q),
                                   confidence=0.95)

            return CheckResult(f"{field_name.upper()}_CONSISTENCY", "FINANCIAL", label,
                               "FAIL", False if diff < Decimal("100") else True,
                               f"{label}: Contract {val_c}, CQP {val_q} (diff: {diff})",
                               contract_value=str(amt_c), cqp_value=str(amt_q),
                               confidence=0.95)
        except Exception:
            return CheckResult(f"{field_name.upper()}_CONSISTENCY", "FINANCIAL", label,
                               "REVIEW", False,
                               f"Cannot compare: {val_c} vs {val_q}",
                               contract_value=val_c, cqp_value=val_q, confidence=0.30)
    return _check


def rule_vat_rate_valid(data: ExtractedData) -> CheckResult:
    vat_c = data.get("contract", "vat_rate", "")
    vat_q = data.get("cqp", "vat_rate", "")
    vat = vat_c or vat_q

    if not vat:
        return CheckResult("VAT_PRESENT", "FINANCIAL", "VAT Rate",
                           "UNKNOWN", True, "VAT rate not found in either document")

    try:
        rate = Decimal(norm.normalize_vat_rate(vat))
        if Decimal("0.12") <= rate <= Decimal("0.14"):
            return CheckResult("VAT_RATE_VALID", "FINANCIAL", "VAT Rate",
                               "PASS", False,
                               f"VAT rate: {rate * 100}% (matches expected 13%)",
                               contract_value=vat_c, cqp_value=vat_q, confidence=0.95)
        return CheckResult("VAT_RATE_VALID", "FINANCIAL", "VAT Rate",
                           "REVIEW", False,
                           f"VAT rate: {rate * 100}% (expected ~13%)",
                           contract_value=vat_c, cqp_value=vat_q, confidence=0.60)
    except Exception:
        return CheckResult("VAT_RATE_VALID", "FINANCIAL", "VAT Rate",
                           "UNKNOWN", False,
                           f"VAT rate invalid: {vat}",
                           contract_value=vat_c, cqp_value=vat_q, confidence=0.20)


def rule_tax_calculation_valid(data: ExtractedData) -> CheckResult:
    untaxed_c = data.get("contract", "untaxed_amount", "")
    tax_c = data.get("contract", "tax_amount", "")
    total_c = data.get("contract", "tax_included_amount", "")

    if not untaxed_c or not total_c:
        return CheckResult("TAX_CALCULATION_VALID", "FINANCIAL", "Tax Calculation",
                           "UNKNOWN", False, "Insufficient data for tax validation")

    try:
        untaxed = Decimal(norm.normalize_money(untaxed_c))
        total = Decimal(norm.normalize_money(total_c))
        expected_total = untaxed * Decimal("1.13")  # 13% VAT
        diff = abs(total - expected_total)

        if diff < Decimal("2.00"):
            return CheckResult("TAX_CALCULATION_VALID", "FINANCIAL", "Tax Calculation",
                               "PASS", False,
                               f"Tax calculation valid: {untaxed} * 1.13 ≈ {total}",
                               contract_value=total_c, confidence=0.95)
        return CheckResult("TAX_CALCULATION_VALID", "FINANCIAL", "Tax Calculation",
                           "REVIEW", False,
                           f"Tax calculation may have rounding: {untaxed} * 1.13 = {expected_total}, got {total} (diff: {diff})",
                           contract_value=total_c, confidence=0.60)
    except Exception:
        return CheckResult("TAX_CALCULATION_VALID", "FINANCIAL", "Tax Calculation",
                           "UNKNOWN", False, "Could not validate tax calculation")


# ============================================================
# PAYMENT TERMS
# ============================================================

def rule_payment_terms_present(data: ExtractedData) -> CheckResult:
    payment = data.get("contract", "payment_terms", "")
    if not payment or len(payment) < 10:
        return CheckResult("PAYMENT_TERMS_PRESENT", "COMMERCIAL", "Payment Terms",
                           "FAIL", True,
                           "Payment terms not extracted from Contract Annex 2",
                           "Payment terms are required in full from Annex 2 for BT09",
                           confidence=0.95)
    return CheckResult("PAYMENT_TERMS_PRESENT", "COMMERCIAL", "Payment Terms",
                       "PASS", False,
                       f"Payment terms extracted ({len(payment)} chars)",
                       contract_value=payment[:200] + "..." if len(payment) > 200 else payment,
                       confidence=0.85)


# ============================================================
# CUSTOMER ID / GIS
# ============================================================

def rule_customer_id_present(data: ExtractedData) -> CheckResult:
    ship_to = data.get("contract", "ship_to_id", "")
    end_customer = data.get("contract", "end_customer_id", "")
    gis = data.get("contract", "gis_number", "")

    missing = []
    if not ship_to:
        missing.append("Ship-to ID")
    if not end_customer:
        missing.append("End Customer ID")
    if not gis:
        missing.append("GIS")

    if not missing:
        return CheckResult("CUSTOMER_ID_GIS", "COMMERCIAL", "Customer ID / GIS",
                           "PASS", False,
                           "All customer IDs and GIS found",
                           confidence=0.85)

    return CheckResult("CUSTOMER_ID_GIS", "COMMERCIAL", "Customer ID / GIS",
                       "REVIEW", False,
                       f"Missing: {', '.join(missing)}. Manual completion required.",
                       contract_value=f"Ship-to: {ship_to or 'N/A'}, End Cust: {end_customer or 'N/A'}, GIS: {gis or 'N/A'}",
                       confidence=0.90 if missing else 0.85)


# ============================================================
# CONFIGURATION COMPARISON
# ============================================================

def rule_configuration_consistency(data: ExtractedData) -> CheckResult:
    config_c = data.get("contract", "technical_config", "")
    config_q = data.get("cqp", "technical_config", "")
    # This is simplified - full implementation would do line-by-line comparison
    if not config_c and not config_q:
        return CheckResult("CONFIGURATION_CONSISTENCY", "TECHNICAL", "Configuration",
                           "UNKNOWN", False, "Technical configuration not extracted from either document")
    if config_c and not config_q:
        return CheckResult("CONFIGURATION_CONSISTENCY", "TECHNICAL", "Configuration",
                           "REVIEW", False,
                           "Contract has configuration but CQP does not. Cannot compare.",
                           contract_value=config_c[:100])
    if not config_c and config_q:
        return CheckResult("CONFIGURATION_CONSISTENCY", "TECHNICAL", "Configuration",
                           "REVIEW", True,
                           "CQP has configuration but Contract/TA does not. Cannot verify.",
                           cqp_value=config_q[:100])

    # Basic comparison
    return CheckResult("CONFIGURATION_CONSISTENCY", "TECHNICAL", "Configuration",
                       "REVIEW", False,
                       "Configuration extracted from both documents. Detailed comparison requires structured extraction.",
                       contract_value=config_c[:100] + "..." if len(config_c) > 100 else config_c,
                       cqp_value=config_q[:100] + "..." if len(config_q) > 100 else config_q,
                       confidence=0.50)


# ============================================================
# END CUSTOMER
# ============================================================

def rule_end_customer_consistency(data: ExtractedData) -> CheckResult:
    end_c = data.get("contract", "end_customer_name", "")
    end_q = data.get("cqp", "end_customer_name", "")
    if not end_c or not end_q:
        return CheckResult("END_CUSTOMER_CONSISTENCY", "COMMERCIAL", "End Customer",
                           "UNKNOWN", False, "End customer not found",
                           contract_value=end_c, cqp_value=end_q)
    nc = norm.normalize_company_name(end_c)
    nq = norm.normalize_company_name(end_q)
    if nc == nq:
        return CheckResult("END_CUSTOMER_CONSISTENCY", "COMMERCIAL", "End Customer",
                           "PASS", False, f"End customer: {end_c}",
                           contract_value=end_c, cqp_value=end_q, confidence=0.93)
    return CheckResult("END_CUSTOMER_CONSISTENCY", "COMMERCIAL", "End Customer",
                       "REVIEW", False,
                       f"End customer differs: {end_c} vs {end_q}",
                       contract_value=end_c, cqp_value=end_q, confidence=0.40)


# ============================================================
# SHIP-TO
# ============================================================

def rule_ship_to_determination(data: ExtractedData) -> CheckResult:
    """Determine correct ship-to based on incoterm."""
    contract_incoterm, _, _ = _determine_incoterm(data)
    ship_to_name = data.get("contract", "ship_to_name", "")
    ship_to_addr = data.get("contract", "ship_to_address", "")
    buyer_name = data.get("contract", "buyer_legal_name", "")
    buyer_addr = data.get("contract", "buyer_address", "")
    install_site = data.get("contract", "installation_site", "")

    if ship_to_name and ship_to_addr:
        return CheckResult("SHIP_TO_DETERMINATION", "COMMERCIAL", "Ship-to",
                           "PASS", False,
                           f"Ship-to: {ship_to_name}, {ship_to_addr}",
                           contract_value=f"{ship_to_name}, {ship_to_addr}",
                           confidence=0.90)

    if contract_incoterm == "EXW":
        if buyer_name and buyer_addr:
            return CheckResult("SHIP_TO_DETERMINATION", "COMMERCIAL", "Ship-to",
                               "PASS", False,
                               f"EXW Ship-to: {buyer_name}, {buyer_addr} (buyer = ship-to for EXW)",
                               contract_value=f"{buyer_name}, {buyer_addr}",
                               confidence=0.75)
        return CheckResult("SHIP_TO_DETERMINATION", "COMMERCIAL", "Ship-to",
                           "UNKNOWN", False,
                           "EXW: could not determine ship-to (buyer info missing)",
                           confidence=0.30)

    if install_site:
        return CheckResult("SHIP_TO_DETERMINATION", "COMMERCIAL", "Ship-to",
                           "REVIEW", False,
                           f"Ship-to may be installation site: {install_site}. Manual confirmation needed.",
                           contract_value=install_site, confidence=0.40)

    return CheckResult("SHIP_TO_DETERMINATION", "COMMERCIAL", "Ship-to",
                       "UNKNOWN", False,
                       "Ship-to not determined. Manual input required.",
                       confidence=0.20)


# ============================================================
# OVERALL CONCLUSION ENGINE
# ============================================================

ALL_RULES = [
    # Source Recognition
    rule_file_contract_present,
    rule_file_cqp_present,
    rule_file_ta_present,
    # Seller
    rule_seller_prefix_match,
    # Incoterm
    rule_incoterm_contract_determination,
    rule_incoterm_cqp_determination,
    rule_incoterm_consistency,
    rule_incoterm2_location,
    # Basic Consistency
    rule_contract_number_reference,
    rule_buyer_consistency,
    rule_end_customer_consistency,
    rule_robot_model_consistency,
    rule_robot_quantity_consistency,
    # Delivery
    rule_delivery_time_consistency,
    # Warranty
    rule_warranty_classification,
    rule_warranty_consistency,
    # Financial
    rule_amount_consistency("untaxed_amount", "Untaxed Amount"),
    rule_amount_consistency("tax_included_amount", "Tax-included Amount"),
    rule_vat_rate_valid,
    rule_tax_calculation_valid,
    # Payment
    rule_payment_terms_present,
    # Customer ID
    rule_customer_id_present,
    # Ship-to
    rule_ship_to_determination,
    # Configuration
    rule_configuration_consistency,
]


def run_all_rules(data: ExtractedData) -> List[CheckResult]:
    """Run all rules and return results."""
    results = []
    for rule_func in ALL_RULES:
        try:
            result = rule_func(data)
            results.append(result)
        except Exception as exc:
            results.append(CheckResult(
                rule_id=f"ERROR_{rule_func.__name__}",
                category="ERROR",
                label=rule_func.__name__,
                status="UNKNOWN",
                is_blocker=False,
                summary=f"Rule execution error: {exc}",
                confidence=0.0,
            ))
    return results


def compute_overall_conclusion(results: List[CheckResult]) -> Tuple[str, str]:
    """Compute overall conclusion from check results.

    Returns (conclusion, description).
    """
    blocker_fails = [r for r in results if r.is_blocker and r.status == "FAIL"]
    blocker_unknowns = [r for r in results if r.is_blocker and r.status == "UNKNOWN"]
    non_blocker_issues = [r for r in results if not r.is_blocker and r.status in ("REVIEW", "FAIL", "UNKNOWN")]

    if blocker_fails:
        rules = ", ".join(r.rule_id for r in blocker_fails)
        return "BLOCKED", f"BLOCKER issues found: {rules}"

    if blocker_unknowns:
        rules = ", ".join(r.rule_id for r in blocker_unknowns)
        return "PENDING_MANUAL_REVIEW", f"Unresolved BLOCKER items: {rules}"

    if non_blocker_issues:
        rules = ", ".join(r.rule_id for r in non_blocker_issues[:5])
        note = f"Non-BLOCKER issues to review: {rules}"
        if len(non_blocker_issues) > 5:
            note += f" (+{len(non_blocker_issues) - 5} more)"
        return "PASS_WITH_NOTES", note

    return "PASS", "All checks passed."


def get_rule_by_id(rule_id: str) -> Optional[callable]:
    """Get a rule function by its rule_id."""
    for rule_func in ALL_RULES:
        try:
            sample = rule_func(ExtractedData())
            if sample.rule_id == rule_id:
                return rule_func
        except Exception:
            pass
    return None